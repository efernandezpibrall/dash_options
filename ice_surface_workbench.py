"""Compact progressive review. Shared analytics owns every price and fit."""
import math
from uuid import uuid4

import dash_ag_grid as dag
import pandas as pd
import plotly.graph_objects as go
from dash import ClientsideFunction, Input, Output, State, callback, clientside_callback, dcc, html, no_update, ctx
from dash.exceptions import PreventUpdate
from options.vol_calibration.api import (compose_quote_surface_workspace, quote_surface_review_identity,
    quote_surface_controls, quote_surface_fit_selection, save_quote_surface_draft,
    list_quote_surface_drafts, load_quote_surface_draft, publish_quote_surface_draft, quote_surface_vol_metrics,
    quote_surface_delivery_options, quote_surface_control_view)
from snapshot_cache import snapshot_store, SnapshotReferenceError
from runtime_config import get_database_engine
from vol_calibration.auth import current_request_identity, authorize, Permission, AuthenticationError
from vol_calibration.feature_flags import ttf_intraday_writes_enabled, jkm_writes_enabled, ttf_publication_enabled, jkm_publication_enabled
from ice_quote_tape import amount, gst
import ice_surface_jobs as jobs

CONTROL_NAMES=('level','downside_skew','upside_skew','curvature')
MODES=(('joint','Joint fit','#2563a6'),('level','Parallel','#0f766e'),('downside_skew','Downside','#7c3aed'),('upside_skew','Upside','#d97706'),('curvature','Smile','#b34788'))


def _access(product, *, publish=False):
    identity=current_request_identity()
    authorize(identity,Permission.PUBLISH if publish else Permission.CREATE_DRAFT,
              resource_creator=identity.subject if publish else None)
    allowed=(ttf_publication_enabled() if publish else ttf_intraday_writes_enabled()) if product=='TTF' else (jkm_publication_enabled() if publish else jkm_writes_enabled()) if product=='JKM' else False
    if not allowed:raise PermissionError('Surface editing is disabled for this product.')
    return identity


def _pin(workspace,identity):
    revision=quote_surface_review_identity(workspace)+':'+workspace['anchor_event_id']
    ns=jobs.namespace(identity.subject)+':workspace:'+revision
    snapshot=snapshot_store.publish(ns,revision,workspace,ttl_seconds=3600)
    return {**{k:workspace[k] for k in ('product','anchor_event_id','base_publication_id','months','surface_cob_date')},'snapshot':snapshot}


def _workspace(ref,anchor):
    if not ref or ref.get('anchor_event_id')!=anchor:raise ValueError('Quote context changed. Refresh options data.')
    identity=_access(ref['product'])
    ns=ref.get('snapshot',{}).get('namespace','')
    if not ns.startswith(jobs.namespace(identity.subject)+':workspace:'):raise PermissionError('Review unavailable for this user.')
    try:workspace=snapshot_store.resolve(ref['snapshot'],expected_namespace=ns)
    except SnapshotReferenceError as exc:raise ValueError('Review expired. Refresh options data.') from exc
    if any(ref[k]!=workspace[k] for k in ('anchor_event_id','product','base_publication_id')):raise ValueError('Review identity changed.')
    return identity,workspace


def _selection(rows):return [{'event_id':r['event_id'],'side':r.get('target'),'priority':r.get('priority',False)} for r in rows or [] if r.get('use') is True]


def _signature(specs):return sorted([{'event_id':s['event_id'],'side':s['side'],'priority':s['priority']} for s in specs],key=lambda r:r['event_id'])


def _matches(view,ref,values,selection,mode):
    m=(view or {}).get('manifest',{})
    return bool(view and ref and view.get('workspace_id')==ref['snapshot']['snapshot_id'] and view.get('mode')==mode
        and m.get('base_publication_id')==ref['base_publication_id'] and m.get('product')==ref['product']
        and all(v is not None and m.get('controls',{}).get(k)==v for k,v in zip(CONTROL_NAMES,values))
        and _signature(m.get('joint_fit',{}).get('targets',[]))==_signature(selection))


def _display_controls(coordinates):
    # Display precision does not change the retained fitted candidate. Edits are
    # offsets from these visible coordinates, avoiding invisible rounding moves.
    return [round(value,4) for value in coordinates['values']]


def _current_candidate(identity,workspace,view,ref,values,selection,mode,*,displayed,delivery):
    value=jobs.candidate(view['candidate_ref'],identity.subject)
    specs=quote_surface_fit_selection(workspace,selection)
    if (value['candidate_id']!=view['candidate_id'] or value.get('fit_mode')!=mode
        or value.get('review_identity')!=quote_surface_review_identity(workspace,specs)
        or not _matches(_view(value,ref,mode,view['candidate_ref']),ref,values,selection,mode)):
        raise ValueError('Candidate changed. Wait for the current fit and save it again.')
    coordinates=quote_surface_control_view(workspace,value,delivery)
    if any(not isinstance(shown,(float,int)) or isinstance(shown,bool) or not math.isfinite(shown)
           or not math.isclose(shown,base+offset,rel_tol=0,abs_tol=1e-9)
           for shown,base,offset in zip(displayed,_display_controls(coordinates),values)):
        raise ValueError('Wait for the displayed adjustment to finish fitting.')
    return value


def _vol_cells(metrics):
    compact=[];detail=[]
    for leg in metrics.get('legs',[]):
        label=f"{leg['strike']:g}{leg['option_type']}"
        before=leg['published_iv']*100;after=leg.get('candidate_iv')
        text=f"{label} {before:.2f}"
        if after is not None:text+=f" → {after*100:.2f} ({leg['change_pp']:+.2f})"
        if leg['month_count']>1:text+=' eq'
        compact.append(text)
        detail.append(f"{'Long' if leg['direction']>0 else 'Short'} {leg['ratio']:g} × {text}; {leg['month_count']} month(s)")
    return {'surface_iv':' / '.join(compact) or '—',
            'vol_tooltip':' | '.join(detail) or metrics.get('reason',''),
            'vol_metrics':metrics}


def _rows(workspace,selection=None,visible_ids=None,row_choices=None):
    selected={s['event_id']:s for s in selection or []};choices={r['event_id']:r for r in workspace['fit_inputs']}
    preferences={r['event_id']:{'side':r.get('target'),'priority':r.get('priority',False)} for r in row_choices or []}
    visible=set(visible_ids or []) | set(selected)
    if visible_ids is None and not visible:visible={workspace['anchor_event_id']}
    rows=[]
    for r in workspace['records']:
        event_id=str(r['event_id'])
        if event_id not in visible:continue
        choice=choices[event_id];pick=selected.get(event_id,preferences.get(event_id,{}))
        labels=choice['targets'];broker=' / '.join(amount(labels[k]) for k in ('Bid','Offer') if k in labels) or amount(labels.get('Trade'))
        contract=r.get('strip_label') or str(r.get('contract_month') or '')[:10]
        structure=r.get('structure_label') or f"{amount(r.get('strike'),2)}{r.get('option_type') or ''}"
        side=pick.get('side') if pick.get('side') in labels else choice.get('default_target')
        tooltip=f"{contract} · {structure} · {r.get('sender_handle') or ''} · {gst(r.get('observed_at') or r.get('received_at'),True)} · F {amount(r.get('forward'))}"
        if choice['reason']:tooltip+=' · '+choice['reason']
        elif choice.get('conditional'):tooltip+=' · Conditional convention'
        unit=r.get('price_unit_label') or '/'.join(str(r.get(k) or '') for k in ('currency_code','price_unit')).strip('/')
        market_kind={'strike':'Strike IV','strip_equivalent':'Premium-equivalent strip IV',
                     'straddle':'Common straddle IV'}.get(choice['broker_vols'].get('kind'),'')
        rows.append({'event_id':event_id,'quote':f'{contract} · {structure}','time':gst(r.get('observed_at') or r.get('received_at')),
            'broker':broker,'target':side,'choices':list(labels),'target_prices':labels,'target_price':labels.get(side),
            'price_unit':unit,'price_tooltip':f'{unit} · Broker {broker}',
            'use':event_id in selected and not choice['reason'],'priority':pick.get('priority',False),
            'model':None,'residual':None,'residual_ticks':None,'matched':None,'eligible':not bool(choice['reason']),
            'reason':choice['reason'],'tooltip':tooltip,'conditional':choice.get('conditional',False),
            'market_ivs':{m['side']:m['iv']*100 for m in choice['broker_vols']['marks']},
            'market_iv':next((m['iv']*100 for m in choice['broker_vols']['marks'] if m['side']==side),None),
            'market_iv_reason':choice['broker_vols']['reason'] or market_kind,
            'fit_status':choice['reason'] or 'Pending',
            **_vol_cells(choice['vol_metrics'])})
    return rows


def surface_workbench_launcher(event_id):
    if not event_id:return None
    definitions=[
        {'field':'use','headerName':'','width':38,'minWidth':38,'maxWidth':38,'pinned':'left','editable':{'function':'params.data.eligible'},'cellRenderer':'agCheckboxCellRenderer','cellEditor':'agCheckboxCellEditor','headerTooltip':'Use for fitting'},
        {'field':'quote','headerName':'Quote','width':165,'minWidth':165,'pinned':'left','tooltipField':'tooltip'},
        {'field':'target','headerName':'Target','width':85,'editable':{'function':'params.data.eligible && params.data.choices.length > 1'},'cellEditor':'agSelectCellEditor','cellEditorParams':{'function':'({values: params.data.choices})'},'cellClass':'ice-fit-target'},
        {'field':'target_price','headerName':'Price','width':90,'valueFormatter':{'function':"params.value == null ? '—' : Number(params.value).toFixed(4)"},'cellClass':'ice-fit-number','tooltipField':'price_tooltip'},
        {'field':'price_unit','headerName':'Unit','width':115,'tooltipField':'price_tooltip'},
        {'field':'market_iv','headerName':'Market IV %','width':122,'valueFormatter':{'function':"params.value == null ? '—' : Number(params.value).toFixed(2)"},'cellClass':'ice-fit-number','tooltipField':'market_iv_reason'},
        {'field':'surface_iv','headerName':'Published → adjusted IV % (Δ vol pts)','minWidth':280,'flex':1,'tooltipField':'vol_tooltip','cellClass':'ice-fit-vol'},
        {'field':'residual_ticks','headerName':'Gap · ticks','width':105,'valueFormatter':{'function':"params.value == null ? '—' : (params.value > 0 ? '+' : '') + Number(params.value).toFixed(2)"},'headerTooltip':'Final candidate price minus selected target, divided by the contract tick. Matched within one tick.','cellClassRules':{'ice-fit-miss':'params.data.use && params.data.matched === false','ice-fit-match':'params.data.use && params.data.matched === true'}},
        {'field':'fit_status','headerName':'Target check','width':125,'tooltipField':'fit_tooltip','cellClassRules':{'ice-fit-miss':'params.data.use && params.data.matched === false','ice-fit-match':'params.data.use && params.data.matched === true'}},
        {'field':'priority','headerName':'Priority','width':94,'sortable':False,'editable':{'function':'params.data.eligible'},'cellRenderer':'agCheckboxCellRenderer','cellEditor':'agCheckboxCellEditor','headerTooltip':'Give this target 25 times the fitting weight'},
    ]
    return html.Section([
        dcc.Store(id='ice-surface-anchor',data=event_id),dcc.Store(id='ice-surface-editor-id',data=str(uuid4())),
        dcc.Store(id='ice-surface-workspace-ref'),dcc.Store(id='ice-surface-tape-ids',data=[]),
        dcc.Store(id='ice-surface-control-origin'),
        *[dcc.Store(id='ice-surface-edit-'+k,data=0) for k in CONTROL_NAMES],
        dcc.Store(id='ice-surface-preview'),dcc.Store(id='ice-surface-request'),dcc.Store(id='ice-surface-response'),
        dcc.Store(id='ice-surface-job'),dcc.Store(id='ice-surface-mode-data'),dcc.Store(id='ice-surface-saved-draft'),
        dcc.Store(id='ice-surface-chart-view'),dcc.Store(id='ice-surface-drafts-loaded',data=False),dcc.Interval(id='ice-surface-poll',interval=500,disabled=False),
        html.Div(id='ice-surface-load-status',role='status',className='ice-surface-auto-status'),
        dag.AgGrid(id='ice-surface-evidence-grid',rowData=[],columnDefs=definitions,getRowId='params.data.event_id',
            defaultColDef={'sortable':True,'resizable':True,'filter':False,'suppressHeaderMenuButton':True},
            dashGridOptions={'rowHeight':30,'headerHeight':32,'singleClickEdit':True,'stopEditingWhenCellsLoseFocus':True,
                'animateRows':False,'ariaLabel':'Selected quote calibration targets',
                'rowClassRules':{'ice-fit-unavailable':'!params.data.eligible','ice-fit-selected':'params.data.use'}},
            className='ag-theme-alpine ice-fit-grid',style={'height':'128px'}),
        html.Div([html.Span(id='ice-surface-target-result',role='status'),html.Span(id='ice-surface-preview-status',role='status'),
            html.Div([html.Label('Delivery',htmlFor='ice-surface-editor-month'),dcc.Dropdown(id='ice-surface-editor-month',options=[],value=None,clearable=False)],className='ice-surface-delivery')],className='ice-fit-summary'),
        dcc.RadioItems(id='ice-surface-mode',options=[{'label':'Working fit','value':'joint'}],value='joint',className='ice-fit-modes',inline=True),
        dcc.Graph(id='ice-surface-editor-chart',figure=go.Figure(),config={'displaylogo':False,'responsive':True},className='ice-surface-editor-chart'),
        html.Div([html.Span(id='ice-surface-control-scope',className='ice-surface-control-scope'),
            *[html.Div([html.Label(label,htmlFor='ice-surface-control-'+key),dcc.Input(id='ice-surface-control-'+key,type='number',min=-10,max=10,step=.0001,value=0,disabled=True)],title='Total move from published surface in vol points. Editing applies the change to all selected months, preserving fitted monthly differences.')
            for key,label in zip(CONTROL_NAMES,('Level','Downside','Upside','Curvature'))]],className='ice-surface-edit-controls'),
        html.Details([html.Summary('Leg detail',id='ice-surface-leg-toggle',n_clicks=0),html.Div(id='ice-surface-leg-detail')],id='ice-surface-leg-disclosure',className='ice-fit-leg-detail'),
        html.Div([dcc.Input(id='ice-surface-draft-notes',type='text',maxLength=2000,placeholder='Draft notes',value=''),
            html.Button('Save draft',id='ice-surface-save-btn',n_clicks=0,disabled=True),
            dcc.Checklist(id='ice-surface-publish-confirm',options=[{'label':'Activate','value':'confirm'}],value=[]),
            html.Button('Publish',id='ice-surface-publish-btn',n_clicks=0,disabled=True)],className='ice-fit-actions'),
        html.Div(id='ice-surface-save-status',role='status'),html.Div(id='ice-surface-publish-status',role='status'),
        html.Details([html.Summary('Saved drafts'),html.Div([
            dcc.Dropdown(id='ice-surface-draft-picker',options=[],placeholder='Select draft'),html.Button('Open',id='ice-surface-open-draft',n_clicks=0)],className='ice-surface-editor-toolbar')],id='ice-surface-drafts-disclosure',className='ice-fit-drafts')],className='ice-surface-editor')


@callback(Output('ice-surface-workspace-ref','data'),Output('ice-surface-evidence-grid','rowData'),
    Output('ice-surface-evidence-grid','style'),Output('ice-surface-tape-ids','data'),Output('ice-surface-load-status','children'),
    Output('ice-surface-editor-month','options'),Output('ice-surface-editor-month','value'),
    Input('refresh-options-data','n_clicks'),Input('ice-surface-anchor','data'),Input('ice-chat-quote-grid','selectedRows'),
    Input('ice-chat-quote-snapshot','data'),Input('ice-chat-service-snapshot','data'),Input('ice-surface-poll','n_intervals'),
    State('ice-surface-workspace-ref','data'),State('ice-surface-evidence-grid','rowData'),State('ice-surface-tape-ids','data'),State('ice-surface-editor-month','value'))
def load_workspace(clicks,anchor,tape,source,service,tick,ref,rows,prior_tape,month):
    if not anchor:raise PreventUpdate
    tape_ids=[str(r['event_id']) for r in tape or []]
    if anchor not in tape_ids:raise PreventUpdate
    try:
        authorize(current_request_identity(),Permission.VIEW)
        source_ref=(source or {}).get('review_ref')
        if not source_ref:return *(no_update,)*4,(service or {}).get('review_error') or 'Loading quotes…',no_update,no_update
        product_publication=(service or {}).get(('ttf' if ref and ref.get('product')=='TTF' else 'jkm')+'_publication_id')
        if ref and ref.get('source_snapshot')==source_ref.get('snapshot_id') and ref.get('base_publication_id')==product_publication and tape_ids==(prior_tape or []) and ctx.triggered_id!='refresh-options-data':
            raise PreventUpdate
        records=snapshot_store.resolve(source_ref,expected_namespace='ice-quote-evidence-v2')
        selection=_selection(rows) if ref else []
        if ctx.triggered_id in ('ice-chat-quote-grid','ice-surface-anchor') or not ref:
            selection=[s for s in selection if s['event_id'] not in (prior_tape or []) or s['event_id'] in tape_ids]
            existing={s['event_id'] for s in selection}
            additions=[i for i in tape_ids if i not in (prior_tape or [])] if ref else tape_ids
            selection.extend({'event_id':i,'side':None,'priority':False} for i in additions if i not in existing)
        visible=list(dict.fromkeys(tape_ids+[s['event_id'] for s in selection]))
        selected_records=[r for r in records if str(r['event_id']) in visible]
        anchor_record=next(r for r in selected_records if str(r['event_id'])==anchor)
        product={'TFM':'TTF','JKM':'JKM'}.get(anchor_record['product_code'])
        identity=_access(product)
        publication_id=(service or {}).get(product.lower()+'_publication_id')
        if not publication_id:raise ValueError('Current governed surface unavailable.')
        published=jobs.base(publication_id)
        if published is None:
            jobs.prewarm(get_database_engine(),product,publication_id)
            return *(no_update,)*4,'Loading surface…',no_update,no_update
        workspace=compose_quote_surface_workspace(published,selected_records,anchor,evidence_asof=(source or {}).get('loaded_at'))
        newref={**_pin(workspace,identity),'source_snapshot':source_ref.get('snapshot_id')}
        if ref and newref['snapshot']==ref['snapshot'] and tape_ids==(prior_tape or []) and ctx.triggered_id!='refresh-options-data':
            raise PreventUpdate
        newrows=_rows(workspace,selection,tape_ids,rows)
        deliveries=quote_surface_delivery_options(workspace);months=[d['value'] for d in deliveries]
        anchor_strip='strip:'+anchor
        chosen=month if month in months else anchor_strip if anchor_strip in months else months[0] if months else None
        notice='Surface changed; recalculating selected targets.' if ref and ref.get('base_publication_id')!=newref['base_publication_id'] else ''
        return newref,newrows,{'height':f'{min(300,max(128,len(newrows)*30+53))}px'},tape_ids,notice if any(r['eligible'] for r in newrows) else ' · '.join(dict.fromkeys(r['reason'] for r in newrows)),deliveries,chosen
    except PreventUpdate:raise
    except (ValueError,PermissionError,AuthenticationError,SnapshotReferenceError) as exc:
        return *(no_update,)*4,str(exc),no_update,no_update
    except Exception:
        return *(no_update,)*4,'Review unavailable. Refresh options data.',no_update,no_update


def _view(candidate,ref,mode,candidate_ref):
    return {'workspace_id':ref['snapshot']['snapshot_id'],'mode':mode,'candidate_ref':candidate_ref,
        'candidate_id':candidate['candidate_id'],'manifest':candidate['manifest'],'comparisons':[{**c,**_vol_cells(c.get('vol_metrics',{})),
            'fit_status':c.get('fit_status','Matched' if c['matched'] else 'Target missed')+(' · conditional' if c.get('conditional') else '')}
            for c in candidate['comparisons']]}


def _all_targets_matched(entry):
    summary=entry.get('summary') or {}
    return bool(entry.get('candidate') and summary.get('total',0)>0 and summary.get('matched')==summary['total'])


def _mode_options(state,mode,selected=None):
    options=[]
    for key,label,color in MODES:
        entry=state['modes'].get(key,{})
        if key!=mode and not _all_targets_matched(entry):continue
        summary=entry.get('summary')
        if key==mode and selected:
            summary=selected['manifest']['joint_fit']['summary']
            if any(selected['manifest']['controls'].values()):label='Manual'
        if key==mode and label!='Manual' and not _all_targets_matched(entry):label='Working fit'
        text=f"{summary['matched']}/{summary['total']} matched" if summary else 'Fitting…' if state['status']=='running' else entry.get('status','Unavailable')
        options.append({'value':key,'label':html.Span([html.Span('●',style={'color':color}),f' {label} · {text}']),'disabled':False})
    return options


@callback(Output('ice-surface-job','data'),Output('ice-surface-poll','disabled',allow_duplicate=True),
    Input('ice-surface-request','data'),State('ice-surface-preview','data'),prevent_initial_call=True)
def submit_fit(request,prior):
    if not request:raise PreventUpdate
    try:
        identity,workspace=_workspace(request['ref'],request['anchor'])
        seed=None
        if prior and prior.get('candidate_ref'):
            try:seed=jobs.candidate(prior['candidate_ref'],identity.subject)
            except (ValueError,SnapshotReferenceError):pass
        ticket=jobs.submit(workspace,request['selection'],identity.subject,request['editor'],seed)
        return {'ticket':ticket,'request':request},False
    except (ValueError,PermissionError,AuthenticationError) as exc:return {'error':str(exc),'request':request},True


@callback(Output('ice-surface-response','data'),Output('ice-surface-mode-data','data'),Output('ice-surface-mode','options'),
    Output('ice-surface-poll','disabled'),Input('ice-surface-poll','n_intervals'),Input('ice-surface-job','data'),
    State('ice-surface-workspace-ref','data'),State('ice-surface-anchor','data'),State('ice-surface-mode','value'),
    *[State('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],State('ice-surface-mode-data','data'),State('ice-surface-response','data'))
def poll_fit(tick,job,ref,anchor,mode,level,down,up,curvature,prior_bundle,prior_response):
    if not job:return no_update,no_update,no_update,not not ref
    request=job['request']
    if job.get('error'):return {'key':request['key'],'error':job['error']},no_update,no_update,True
    if not ref or request['ref']['snapshot']!=ref['snapshot']:return no_update,no_update,no_update,False
    if request['mode']!=mode or request['values']!=[level,down,up,curvature]:return no_update,no_update,no_update,False
    try:
        identity,workspace=_workspace(ref,anchor)
        state=jobs.poll(job['ticket'],identity.subject)
        value=None
        adjustment_error=None
        candidate_ref=None
        try:
            controls=quote_surface_controls(dict(zip(CONTROL_NAMES,(level,down,up,curvature))),limit=20)
            jobs.submit_adjustment(job['ticket'],mode,controls,identity.subject,workspace)
            candidate_ref=jobs.adjusted(job['ticket'],mode,controls,identity.subject)
        except ValueError as exc:adjustment_error=str(exc)
        ready=candidate_ref is not None
        if adjustment_error:response={'key':request['key'],'error':adjustment_error}
        elif ready:
            value=jobs.candidate(candidate_ref,identity.subject)
            response={'key':request['key'],'view':_view(value,ref,mode,candidate_ref)}
        elif state['status'] in ('failed','cancelled'):response={'key':request['key'],'error':state.get('error','Fit superseded. Refresh options data.')}
        elif state['status']=='complete':response={'key':request['key'],'error':state['modes'].get(mode,{}).get('status','Candidate unavailable.')} if not any(controls.values()) else no_update
        else:response=no_update
        bundle={'workspace_id':ref['snapshot']['snapshot_id'],'modes':state['modes']}
        unchanged=bundle==prior_bundle
        return no_update if response==prior_response else response,no_update if unchanged else bundle,_mode_options(state,mode,value),state['status'] in ('complete','failed','cancelled') and (ready or adjustment_error is not None or not any(controls.values()))
    except (ValueError,PermissionError,AuthenticationError,SnapshotReferenceError) as exc:
        return {'key':request['key'],'error':str(exc)},no_update,no_update,True


clientside_callback(ClientsideFunction(namespace='iceSurface',function_name='editRows'),Output('ice-surface-evidence-grid','rowData',allow_duplicate=True),
    Input('ice-surface-evidence-grid','cellValueChanged'),State('ice-surface-evidence-grid','rowData'),prevent_initial_call=True)
clientside_callback(ClientsideFunction(namespace='iceSurface',function_name='paintRows'),Output('ice-surface-evidence-grid','rowData',allow_duplicate=True),
    Input('ice-surface-preview','data'),Input('ice-surface-preview-status','children'),State('ice-surface-evidence-grid','rowData'),State('ice-surface-workspace-ref','data'),prevent_initial_call=True)
clientside_callback(ClientsideFunction(namespace='iceSurface',function_name='request'),Output('ice-surface-request','data'),
    *[Input('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],Input('ice-surface-evidence-grid','rowData'),Input('ice-surface-workspace-ref','data'),Input('ice-surface-anchor','data'),Input('ice-surface-mode','value'),Input('refresh-options-data','n_clicks'),State('ice-surface-editor-id','data'))
clientside_callback(ClientsideFunction(namespace='iceSurface',function_name='accept'),Output('ice-surface-preview','data'),Output('ice-surface-preview-status','children'),
    Input('ice-surface-response','data'),*[Input('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],Input('ice-surface-evidence-grid','rowData'),Input('ice-surface-workspace-ref','data'),Input('ice-surface-anchor','data'),Input('ice-surface-mode','value'),Input('refresh-options-data','n_clicks'),
    *[Input('ice-surface-control-'+k,'value') for k in CONTROL_NAMES],Input('ice-surface-control-origin','data'),Input('ice-surface-editor-month','value'),State('ice-surface-preview','data'))
clientside_callback(ClientsideFunction(namespace='iceSurface',function_name='actions'),Output('ice-surface-save-btn','disabled'),Output('ice-surface-publish-btn','disabled'),
    Input('ice-surface-preview','data'),*[Input('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],Input('ice-surface-evidence-grid','rowData'),Input('ice-surface-workspace-ref','data'),Input('ice-surface-saved-draft','data'),Input('ice-surface-publish-confirm','value'),Input('ice-surface-anchor','data'),Input('ice-surface-preview-status','children'),Input('ice-surface-mode','value'),
    *[Input('ice-surface-control-'+k,'value') for k in CONTROL_NAMES],Input('ice-surface-control-origin','data'),Input('ice-surface-editor-month','value'))
clientside_callback(ClientsideFunction(namespace='iceSurface',function_name='clearConfirmation'),Output('ice-surface-publish-confirm','value',allow_duplicate=True),Output('ice-surface-save-status','children',allow_duplicate=True),
    *[Input('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],*[Input('ice-surface-control-'+k,'value') for k in CONTROL_NAMES],Input('ice-surface-evidence-grid','cellValueChanged'),Input('ice-surface-mode','value'),prevent_initial_call=True)
clientside_callback(ClientsideFunction(namespace='iceSurface',function_name='chartView'),Output('ice-surface-chart-view','data'),Input('ice-surface-editor-chart','relayoutData'),State('ice-surface-workspace-ref','data'),State('ice-surface-editor-month','value'),State('ice-surface-anchor','data'),State('ice-surface-chart-view','data'),prevent_initial_call=True)


@callback(Output('ice-surface-editor-chart','figure'),Output('ice-surface-target-result','children'),
    Input('ice-surface-workspace-ref','data'),Input('ice-surface-mode-data','data'),Input('ice-surface-preview','data'),Input('ice-surface-editor-month','value'),Input('ice-surface-mode','value'),State('ice-surface-anchor','data'),State('ice-surface-chart-view','data'))
def render_candidate(ref,bundle,view,month,mode,anchor,chart_view):
    if not ref or not month:raise PreventUpdate
    identity,workspace=_workspace(ref,anchor)
    def curve(surface,candidate_id=None):return jobs.display_curve(workspace,surface,month,identity.subject,candidate_id)
    source=curve(workspace['surface']);figure=go.Figure()
    if not source['strike']:raise PreventUpdate
    figure.add_scatter(x=source['strike'],y=[None if v is None else v*100 for v in source['iv']],name='Published',uid='published',line={'color':'#475569','width':2})
    entries=(bundle or {}).get('modes',{}) if (bundle or {}).get('workspace_id')==ref['snapshot']['snapshot_id'] else {}
    selected=None
    for key,label,color in MODES:
        entry=entries.get(key,{})
        if key!=mode and not _all_targets_matched(entry):continue
        candidate_ref=view.get('candidate_ref') if key==mode and view and view.get('workspace_id')==ref['snapshot']['snapshot_id'] and view.get('mode')==mode else entry.get('candidate')
        if not candidate_ref:continue
        value=jobs.candidate(candidate_ref,identity.subject);adjusted=curve(value['surface'],value['candidate_id'])
        if key==mode:
            if any(value['manifest']['controls'].values()):label='Manual'
            elif not _all_targets_matched(entry):label='Working fit'
        if not adjusted['strike']:continue
        figure.add_scatter(x=adjusted['strike'],y=[None if v is None else v*100 for v in adjusted['iv']],name=label,uid=key,
            line={'color':color,'width':2.8 if key==mode else 1.4,'dash':'solid' if key in ('joint',mode) else 'dash'})
        if key==mode:selected=value
    comparisons=selected['comparisons'] if selected else []
    marks=[m for c in comparisons for m in c.get('broker_vols',{}).get('marks',[]) if m['side']==c['target_side']]
    forward=source['forward']
    if source['equivalent']:
        marks=[{**m,'contract_month':month} for m in marks if m.get('event_id')==source['event_id']]
    else:marks=[m for m in marks if m.get('contract_month')==month]
    if ref['product']=='JKM' and not source['equivalent']:marks=[{**m,'original_strike':m['strike'],'strike':m['strike']*forward/m['forward']} for m in marks]
    from ice_surface_components import add_broker_vol_marks
    add_broker_vol_marks(figure,marks,month)
    strikes=source.get('quote_strikes',[])+[m['strike'] for m in marks if m['contract_month']==month]
    lo=min([forward*.7,*[k*.9 for k in strikes]]);hi=max([forward*1.3,*[k*1.1 for k in strikes]])
    figure.update_xaxes(range=[lo,hi])
    visible=[y for trace in figure.data for x,y in zip(trace.x,trace.y) if y is not None and lo<=x<=hi]
    if visible:figure.update_yaxes(range=[min(visible)-2,max(visible)+2])
    revision=':'.join([ref['base_publication_id'],anchor,month])
    if chart_view and chart_view.get('scope')==revision:
        for axis in ('xaxis','yaxis'):
            if axis in chart_view:figure.update_layout(**{axis:{'range':chart_view[axis],'autorange':False}})
    figure.update_layout(template='plotly_white',height=350,margin={'l':45,'r':12,'t':12,'b':70},xaxis_title='Strike at surface forward' if ref['product']=='JKM' and not source['equivalent'] else 'Strike',yaxis_title='Equivalent IV (%)' if source['equivalent'] else 'IV (%)',legend={'orientation':'h','y':-.25},uirevision=revision)
    text='Published'
    if selected:
        summary=selected['manifest']['joint_fit']['summary']
        text=f"{summary['matched']}/{summary['total']} matched · Max gap {summary['max_abs_ticks']:.2f} ticks"
        if summary['conditional']:text+=f" · {summary['conditional']} conditional"
    return figure,text


def _draft_options(engine,actor,product):
    return [{'label':f"{gst(r['created_at'],True)} · {r.get('notes') or 'Draft'}",'value':str(r['run_id'])} for r in list_quote_surface_drafts(engine,actor=actor,commodity=product)]


@callback(Output('ice-surface-draft-picker','options'),Output('ice-surface-drafts-loaded','data'),
    Input('ice-surface-drafts-disclosure','n_clicks'),Input('ice-surface-saved-draft','data'),
    State('ice-surface-drafts-loaded','data'),State('ice-surface-workspace-ref','data'),State('ice-surface-anchor','data'),prevent_initial_call=True)
def draft_options(clicks,receipt,loaded,ref,anchor):
    if not clicks or (loaded and ctx.triggered_id!='ice-surface-saved-draft'):raise PreventUpdate
    identity,_=_workspace(ref,anchor)
    return _draft_options(get_database_engine(),identity.subject,ref['product']),True


@callback(Output('ice-surface-saved-draft','data'),Output('ice-surface-save-status','children'),Input('ice-surface-save-btn','n_clicks'),
    State('ice-surface-preview','data'),State('ice-surface-workspace-ref','data'),State('ice-surface-anchor','data'),
    *[State('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],State('ice-surface-evidence-grid','rowData'),State('ice-surface-mode','value'),State('ice-surface-draft-notes','value'),
    *[State('ice-surface-control-'+k,'value') for k in CONTROL_NAMES],State('ice-surface-editor-month','value'),prevent_initial_call=True)
def save_draft(clicks,view,ref,anchor,level,down,up,curvature,rows,mode,notes,shown_level,shown_down,shown_up,shown_curve,delivery):
    if not clicks:raise PreventUpdate
    try:
        identity,workspace=_workspace(ref,anchor)
        if not _matches(view,ref,(level,down,up,curvature),_selection(rows),mode):raise ValueError('Wait for the current fit.')
        value=_current_candidate(identity,workspace,view,ref,(level,down,up,curvature),_selection(rows),mode,
            displayed=(shown_level,shown_down,shown_up,shown_curve),delivery=delivery)
        receipt=save_quote_surface_draft(get_database_engine(),value,actor=identity.subject,notes=notes or '')
        return {**receipt,'mode':mode},'Draft saved.'
    except (ValueError,PermissionError,AuthenticationError,SnapshotReferenceError) as exc:return None,str(exc)


@callback(Output('ice-surface-publish-status','children'),Input('ice-surface-publish-btn','n_clicks'),State('ice-surface-saved-draft','data'),State('ice-surface-publish-confirm','value'),
    State('ice-surface-preview','data'),State('ice-surface-workspace-ref','data'),State('ice-surface-anchor','data'),
    *[State('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],State('ice-surface-evidence-grid','rowData'),State('ice-surface-mode','value'),
    *[State('ice-surface-control-'+k,'value') for k in CONTROL_NAMES],State('ice-surface-editor-month','value'),prevent_initial_call=True)
def publish_draft(clicks,receipt,confirm,view,ref,anchor,level,down,up,curvature,rows,mode,shown_level,shown_down,shown_up,shown_curve,delivery):
    if not clicks:raise PreventUpdate
    try:
        if not receipt or not view or receipt.get('candidate_id')!=view.get('candidate_id') or receipt.get('mode')!=mode or 'confirm' not in (confirm or []):raise ValueError('Save this candidate and confirm activation.')
        if not _matches(view,ref,(level,down,up,curvature),_selection(rows),mode):raise ValueError('Save the current fit first.')
        identity,workspace=_workspace(ref,anchor)
        value=_current_candidate(identity,workspace,view,ref,(level,down,up,curvature),_selection(rows),mode,
            displayed=(shown_level,shown_down,shown_up,shown_curve),delivery=delivery)
        saved=load_quote_surface_draft(get_database_engine(),receipt['draft_id'],actor=identity.subject)
        if saved['configuration']['candidate_id']!=value['candidate_id']:raise ValueError('Saved draft identity changed.')
        identity=_access(ref['product'],publish=True)
        result=publish_quote_surface_draft(get_database_engine(),receipt['draft_id'],actor=identity.subject)
        return 'Published · '+str(result['publication_id'])
    except (ValueError,PermissionError,AuthenticationError,SnapshotReferenceError) as exc:return str(exc)


@callback(Output('ice-surface-workspace-ref','data',allow_duplicate=True),Output('ice-surface-evidence-grid','rowData',allow_duplicate=True),
    Output('ice-surface-preview','data',allow_duplicate=True),Output('ice-surface-saved-draft','data',allow_duplicate=True),
    Output('ice-surface-mode','value'),*[Output('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],
    Output('ice-surface-draft-notes','value'),Output('ice-surface-save-status','children',allow_duplicate=True),
    Output('ice-surface-editor-month','options',allow_duplicate=True),Output('ice-surface-editor-month','value',allow_duplicate=True),
    Input('ice-surface-open-draft','n_clicks'),State('ice-surface-draft-picker','value'),State('ice-surface-workspace-ref','data'),State('ice-surface-anchor','data'),State('ice-surface-editor-id','data'),State('ice-surface-editor-month','value'),prevent_initial_call=True)
def open_draft(clicks,draft_id,ref,anchor,editor,month):
    if not clicks:raise PreventUpdate
    try:
        identity,workspace=_workspace(ref,anchor)
        draft=load_quote_surface_draft(get_database_engine(),draft_id,actor=identity.subject)
        value=draft['configuration'];manifest=value['manifest']
        if manifest['base_publication_id']!=ref['base_publication_id']:raise ValueError('Draft uses a different publication. Refresh options data.')
        if 'joint_fit' not in manifest:raise ValueError('Earlier manual draft. Select quotes for a new fit.')
        if manifest['joint_fit'].get('model_implementation')!=jobs.implementation_fingerprint():
            raise ValueError('Draft uses a different model implementation. Refit the selected quotes.')
        records={str(r['event_id']):r for r in workspace['records']};records.update(value['quote_records'])
        restored=compose_quote_surface_workspace(jobs.base(ref['base_publication_id']),list(records.values()),anchor,evidence_asof=workspace['evidence_asof'])
        newref={**_pin(restored,identity),'source_snapshot':ref.get('source_snapshot')};selection=_signature(manifest['joint_fit']['targets'])
        mode=manifest['joint_fit'].get('fitted_diagnostics',manifest['joint_fit']['diagnostics']).get('factor') or 'joint'
        value={**value,'surface':pd.DataFrame(value['surface'])}
        if manifest.get('identity_scope')=='selected_months':
            value['surface']=value['surface'].loc[pd.to_datetime(value['surface'].contract_date).dt.strftime('%Y-%m-%d').isin(manifest['months'])].reset_index(drop=True)
        _,candidate_ref=jobs.seed(restored,selection,identity.subject,editor,value,mode)
        return newref,_rows(restored,selection,[s['event_id'] for s in selection]),_view(value,newref,mode,candidate_ref),{'draft_id':str(draft['run_id']),'candidate_id':value['candidate_id'],'mode':mode},mode,*[manifest['controls'][k] for k in CONTROL_NAMES],draft.get('notes') or '','Draft opened.',quote_surface_delivery_options(restored),month if month in {d['value'] for d in quote_surface_delivery_options(restored)} else restored['months'][0]
    except (ValueError,PermissionError,AuthenticationError,SnapshotReferenceError) as exc:
        return *(no_update,)*10,str(exc),no_update,no_update


@callback(Output('ice-surface-leg-detail','children'),
    Input('ice-surface-leg-toggle','n_clicks'),Input('ice-surface-evidence-grid','cellClicked'),
    Input('ice-surface-preview','data'),State('ice-surface-workspace-ref','data'),State('ice-surface-anchor','data'))
def leg_detail(toggle_clicks,clicked,view,ref,anchor):
    if not toggle_clicks or toggle_clicks % 2 == 0 or not ref:raise PreventUpdate
    identity,workspace=_workspace(ref,anchor)
    event_id=(clicked or {}).get('rowId') or anchor
    record=next((r for r in workspace['records'] if str(r['event_id'])==event_id),None)
    if record is None:raise PreventUpdate
    candidate=None
    if view and view.get('workspace_id')==ref['snapshot']['snapshot_id']:
        candidate=jobs.candidate(view['candidate_ref'],identity.subject)['surface']
    metrics=quote_surface_vol_metrics(record,workspace['surface'],candidate,include_months=True)
    if metrics['reason']:return metrics['reason']
    rows=[]
    for leg in metrics['legs']:
        for month in leg['months']:
            after=month.get('candidate_iv')
            rows.append(html.Tr([html.Td(f"{leg['strike']:g}{leg['option_type']}"),html.Td(month['month']),
                html.Td(f"{month['coefficient']:+.4g}"),html.Td(amount(month['forward'])),
                html.Td(f"{month['published_iv']*100:.2f}"),html.Td('—' if after is None else f"{after*100:.2f}")]))
    return html.Div([html.Div(f"{record.get('sender_handle') or ''} · {gst(record.get('observed_at') or record.get('received_at'),True)}"),
        html.Table([html.Thead(html.Tr([html.Th(x) for x in ('Leg','Delivery','Signed weight','Forward','Published IV %','Adjusted IV %')])),html.Tbody(rows)])],className='ice-fit-leg-table')


@callback(*[Output('ice-surface-control-'+k,'value') for k in CONTROL_NAMES],
    *[Output('ice-surface-control-'+k,'disabled') for k in CONTROL_NAMES],
    *[Output('ice-surface-edit-'+k,'data',allow_duplicate=True) for k in CONTROL_NAMES],
    Output('ice-surface-control-origin','data'),Output('ice-surface-control-scope','children'),
    *[Input('ice-surface-control-'+k,'value') for k in CONTROL_NAMES],
    Input('ice-surface-mode-data','data'),Input('ice-surface-mode','value'),
    Input('ice-surface-editor-month','value'),Input('ice-surface-workspace-ref','data'),
    *[State('ice-surface-edit-'+k,'data') for k in CONTROL_NAMES],
    State('ice-surface-control-origin','data'),State('ice-surface-anchor','data'),prevent_initial_call=True)
def control_coordinates(level,down,up,curvature,bundle,mode,delivery,ref,offset_level,offset_down,offset_up,offset_curve,origin,anchor):
    unchanged=(no_update,)*14
    if not ref or not delivery:return unchanged
    entry=(bundle or {}).get('modes',{}).get(mode,{})
    if (bundle or {}).get('workspace_id')!=ref['snapshot']['snapshot_id'] or not entry.get('candidate'):
        return *(no_update,)*4,*(True,)*4,*(no_update,)*5,'Waiting for fit…'
    identity,workspace=_workspace(ref,anchor)
    value=jobs.candidate(entry['candidate'],identity.subject)
    try:coordinates=quote_surface_control_view(workspace,value,delivery)
    except ValueError as exc:return *(no_update,)*4,*(True,)*4,*(no_update,)*5,str(exc)
    neworigin={'workspace_id':ref['snapshot']['snapshot_id'],'mode':mode,'delivery':delivery,
               'candidate_id':value['candidate_id'],'values':_display_controls(coordinates)}
    offsets=[offset_level,offset_down,offset_up,offset_curve]
    if neworigin!=origin:
        if origin and origin.get('mode')!=mode:offsets=[0.]*4
        displayed=[v+(o or 0) for v,o in zip(neworigin['values'],offsets)]
        return *displayed,*(False,)*4,*offsets,neworigin,coordinates['label']
    if str(ctx.triggered_id).startswith('ice-surface-control-'):
        displayed=[level,down,up,curvature]
        try:
            values=quote_surface_controls(dict(zip(CONTROL_NAMES,displayed)))
            if any(v is None for v in displayed):raise ValueError('Enter four finite moves.')
            offsets=[values[k]-base for k,base in zip(CONTROL_NAMES,neworigin['values'])]
        except (ValueError,TypeError):offsets=[None]*4
        return *(no_update,)*8,*offsets,no_update,no_update
    return unchanged
