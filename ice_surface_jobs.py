"""Bounded review jobs. Immutable results are shared; editor subscriptions are isolated."""
import hashlib
import json
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from options.vol_calibration.api import (QuoteFitCancelled, implementation_fingerprint, quote_surface_review_identity,
    quote_surface_fit_selection, fit_quote_surface_selection, adjust_quote_surface_fit, quote_surface_display_curve,
    prefer_quote_surface_candidate)
from snapshot_cache import snapshot_store, SnapshotReferenceError

MODES=('joint','level','downside_skew','upside_skew','curvature')
_POOL=ThreadPoolExecutor(max_workers=2,thread_name_prefix='quote-review')
_FUTURES={}
_GUARD=threading.RLock()
TTL=1800
BASE_NAMESPACE="ice-surface-base-v3:"+implementation_fingerprint()+":"


def namespace(actor):return 'ice-surface-fast-v4:'+hashlib.sha256(actor.encode()).hexdigest()


def _get(key):return snapshot_store.cache.get(key)


def _put(key,value):snapshot_store.cache.set(key,value,expire=TTL,retry=True)


def _change(key,fn):
    with snapshot_store.lock(key,expire=10):
        value=_get(key) or {};fn(value);_put(key,value)
        return value


def candidate(ref,actor):
    if not isinstance(ref,dict) or not ref.get('namespace','').startswith(namespace(actor)+':candidate:'):
        raise ValueError('Candidate reference unavailable for this user.')
    return snapshot_store.resolve(ref,expected_namespace=ref['namespace'])


def _retained(ref,actor):
    try:candidate(ref,actor);return True
    except (ValueError,SnapshotReferenceError):return False


def _store_candidate(value,actor,identity,mode,controls=None,review_identity=None):
    label=json.dumps([identity,mode,controls],sort_keys=True,separators=(',',':'))
    ns=namespace(actor)+':candidate:'+hashlib.sha256(label.encode()).hexdigest()
    return snapshot_store.publish(ns,value['candidate_id'],{**value,'review_identity':review_identity or identity.split(':job:')[-1],'fit_mode':mode},ttl_seconds=TTL)


def _owner_alive(job):
    try:os.kill(job.get('owner_pid',0),0);return True
    except (ProcessLookupError,TypeError):return False


def _launch(key,fn,*args):
    with _GUARD:
        if len(_FUTURES)>=8:raise ValueError('Review queue is busy. Try again shortly.')
        future=_POOL.submit(fn,*args);_FUTURES[key]=future
        def clean(done):
            with _GUARD:
                if _FUTURES.get(key) is done:_FUTURES.pop(key,None)
        future.add_done_callback(clean)
        return future


def _fit_bundle(key,run_id,workspace,specs,actor,prior):
    last_check=0.;stopped=False;started=time.monotonic()
    def cancelled():
        nonlocal last_check,stopped
        now=time.monotonic()
        if now-last_check>.2:
            last_check=now
            job=_get(key) or {}
            stopped=job.get('id')!=run_id or not any(expiry>time.time() for expiry in job.get('subscribers',{}).values()) or now-started>180
        return stopped
    def update(fn):
        return _change(key,lambda j:fn(j) if j.get('id')==run_id else None)
    try:
        for mode in MODES:
            if cancelled():raise QuoteFitCancelled('Superseded fit.')
            current=(_get(key) or {}).get('modes',{}).get(mode,{})
            if current.get('candidate'):continue
            seed=prior if mode=='joint' else None
            mode_started=time.monotonic()
            try:
                value=fit_quote_surface_selection(workspace,[{k:s[k] for k in ('event_id','side','priority')} for s in specs],
                    factor=None if mode=='joint' else mode, initial_candidate=seed,
                    cancel_check=lambda:cancelled() or time.monotonic()-mode_started>15)
            except QuoteFitCancelled:
                if cancelled():raise
                update(lambda j:j['modes'].update({mode:{'status':'Incomplete',
                    'reason':'Calculation budget reached; no conclusion about feasibility.'}}))
                continue
            except ValueError as exc:
                reason = str(exc)
                update(lambda j:j['modes'].update({mode:{'status':'Unavailable','reason':reason}}))
                continue
            summary=value['manifest']['joint_fit']['summary']
            status=value['manifest']['joint_fit']['diagnostics']['status']
            ref=_store_candidate(value,actor,key,mode)
            update(lambda j:j['modes'].update({mode:{'status':status,'candidate':ref,'summary':summary}}))
            if mode!='joint':
                joint_ref=(_get(key) or {}).get('modes',{}).get('joint',{}).get('candidate')
                joint=candidate(joint_ref,actor) if joint_ref else None
                preferred=prefer_quote_surface_candidate(joint,value)
                if preferred is not joint:
                    joint_ref=_store_candidate(preferred,actor,key,'joint')
                    fit=preferred['manifest']['joint_fit']
                    update(lambda j:j['modes'].update({'joint':{'status':fit['diagnostics']['status'],
                        'candidate':joint_ref,'summary':fit['summary']}}))
        update(lambda j:j.update(status='complete'))
    except QuoteFitCancelled:
        update(lambda j:j.update(status='cancelled'))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'Fit unavailable. Refresh options data.'
        update(lambda j:j.update(status='failed',error=reason))


def submit(workspace,selection,actor,editor,prior=None):
    specs=quote_surface_fit_selection(workspace,selection)
    identity=quote_surface_review_identity(workspace,specs)
    key=namespace(actor)+':job:'+identity
    subscription=namespace(actor)+':editor:'+editor
    old=_get(subscription)
    if old and old!=key:
        previous=_change(old,lambda j:j.get('subscribers',{}).pop(editor,None))
        with _GUARD:
            future=_FUTURES.get(old)
            if future and not previous.get('subscribers') and future.cancel():_change(old,lambda j:j.update(status='cancelled'))
    _put(subscription,key)
    with snapshot_store.lock(key,expire=10):
        job=_get(key)
        expired=job and any(entry.get('candidate') and not _retained(entry['candidate'],actor) for entry in job.get('modes',{}).values())
        if not job or expired or job['status'] in ('cancelled','failed') or (job['status']=='running' and (not _owner_alive(job) or time.time()-job['created_at']>180)):
            job={'id':str(uuid4()),'identity':identity,'status':'running','owner_pid':os.getpid(),
                 'created_at':time.time(),'modes':{},'subscribers':{editor:time.time()+120}}
            _put(key,job)
            try:_launch(key,_fit_bundle,key,job['id'],workspace,specs,actor,prior)
            except ValueError:
                snapshot_store.cache.delete(key);raise
        else:
            job['subscribers'][editor]=time.time()+120;_put(key,job)
    return {'key':key,'id':job['id'],'editor':editor,'identity':identity}


def poll(ref,actor):
    if not ref or not ref.get('key','').startswith(namespace(actor)+':job:'):raise ValueError('Review job unavailable for this user.')
    job=_get(ref['key'])
    if not job or job.get('id')!=ref['id']:raise ValueError('Review expired. Refresh options data.')
    _change(ref['key'],lambda j:j['subscribers'].update({ref['editor']:time.time()+120}))
    if job['status']=='running':
        if not _owner_alive(job):raise ValueError('Review worker restarted. Refresh options data.')
    return job


def adjusted(job_ref,mode,controls,actor):
    job=poll(job_ref,actor);entry=job['modes'].get(mode,{})
    if not entry.get('candidate'):return None
    if not any(controls.values()):return entry['candidate']
    key=namespace(actor)+':adjust:'+hashlib.sha256(json.dumps([job_ref['identity'],mode,controls],sort_keys=True).encode()).hexdigest()
    saved=_get(key)
    if saved and saved.get('candidate'):return saved['candidate']
    if saved and saved.get('error'):raise ValueError(saved['error'])
    return None


def submit_adjustment(job_ref,mode,controls,actor,workspace):
    subscription=namespace(actor)+':adjust-editor:'+job_ref['editor']
    old=_get(subscription)
    def retire_previous(new_key):
        _put(subscription,new_key)
        if old and old!=new_key:
            previous=_change(old,lambda j:j.get('subscribers',{}).pop(job_ref['editor'],None))
            with _GUARD:
                future=_FUTURES.get(old)
                if future and not previous.get('subscribers') and future.cancel():
                    snapshot_store.cache.delete(old)
    if not any(controls.values()):
        retire_previous(None);return
    job=poll(job_ref,actor);entry=job['modes'].get(mode,{})
    if not entry.get('candidate'):return
    key=namespace(actor)+':adjust:'+hashlib.sha256(json.dumps([job_ref['identity'],mode,controls],sort_keys=True).encode()).hexdigest()
    retire_previous(key)
    with snapshot_store.lock(key,expire=10):
        saved=_get(key)
        if saved and ((saved.get('candidate') and _retained(saved['candidate'],actor)) or saved.get('error')):return
        if saved and saved.get('status')=='running' and _owner_alive(saved) and time.time()-saved.get('created_at',0)<180:
            saved.setdefault('subscribers',{})[job_ref['editor']]=time.time()+120
            _put(key,saved);return
        run_id=str(uuid4())
        _put(key,{'id':run_id,'status':'running','owner_pid':os.getpid(),'created_at':time.time(),
                  'subscribers':{job_ref['editor']:time.time()+120}})
    def cancelled():
        saved=_get(key) or {}
        if saved.get('id')!=run_id or time.time()-saved.get('created_at',0)>180:return True
        return not any(expiry>time.time()
                       and _get(namespace(actor)+':editor:'+editor)==job_ref['key']
                       and _get(namespace(actor)+':adjust-editor:'+editor)==key
                       for editor,expiry in saved.get('subscribers',{}).items())
    def finish(result):
        _change(key,lambda saved:saved.update(result) if saved.get('id')==run_id else None)
    def run():
        try:
            if cancelled():raise QuoteFitCancelled('Superseded adjustment.')
            value=adjust_quote_surface_fit(workspace,candidate(entry['candidate'],actor),controls,cancel_check=cancelled)
            if cancelled():raise QuoteFitCancelled('Superseded adjustment.')
            finish({'status':'complete','candidate':_store_candidate(value,actor,key,mode,controls,job_ref['identity'])})
        except QuoteFitCancelled:finish({'status':'cancelled'})
        except Exception as exc:finish({'status':'failed','error':str(exc) if isinstance(exc,ValueError) else 'Adjustment unavailable.'})
    try:_launch(key,run)
    except ValueError:
        snapshot_store.cache.delete(key);raise


def base(publication_id):
    ns=BASE_NAMESPACE+str(publication_id)
    ref=snapshot_store.latest(ns)
    if not ref:return None
    try:return snapshot_store.resolve(ref,expected_namespace=ns)
    except SnapshotReferenceError:return None


def display_curve(workspace,surface,delivery,actor,candidate_id=None):
    """Cache only the displayed delivery; original strip inputs are part of its key."""
    identity=[quote_surface_review_identity(workspace),candidate_id or workspace['base_publication_id'],delivery]
    key=namespace(actor)+':chart:'+hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    cached=_get(key)
    if cached is not None:return cached
    with snapshot_store.lock(key,expire=30):
        cached=_get(key)
        if cached is None:
            cached=quote_surface_display_curve(workspace,surface,delivery)
            _put(key,cached)
    return cached


def prewarm(engine,product,publication_id):
    from options.vol_calibration.api import load_quote_surface_base
    key=BASE_NAMESPACE+str(publication_id)
    if base(publication_id) is not None:return
    if _get(key+':error'):raise ValueError('Published surface unavailable. Refresh options data.')
    with _GUARD:
        if key in _FUTURES:return
        def run():
            try:
                with snapshot_store.lock(key,expire=120):
                    if base(publication_id) is None:
                        value=load_quote_surface_base(engine,product)
                        if str(value['publication']['publication_id'])==str(publication_id):
                            snapshot_store.publish(key,str(publication_id),value,ttl_seconds=3600)
                        else:raise ValueError('Publication changed while preparing the surface.')
            except Exception:
                snapshot_store.cache.set(key+':error',True,expire=30,retry=True)
            finally:
                with _GUARD:_FUTURES.pop(key,None)
        _launch(key,run)


def seed(workspace,selection,actor,editor,value,mode):
    ticket=submit(workspace,selection,actor,editor,value)
    baseline=adjust_quote_surface_fit(workspace,value,{})
    fitted_ref=_store_candidate(baseline,actor,ticket['key'],mode)
    ref=_store_candidate(value,actor,ticket['key'],mode,value['manifest']['controls'])
    summary=baseline['manifest']['joint_fit']['summary']
    _change(ticket['key'],lambda j:j['modes'].update({mode:{'status':'Matched' if summary['matched']==summary['total'] else 'Target missed','candidate':fitted_ref,'summary':summary}}))
    return ticket,ref
