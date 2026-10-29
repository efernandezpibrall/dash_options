/* Joint quote fits: canonical targets, debounce, and exact-revision acceptance. */
(function () {
    'use strict';
    const names = ['level', 'downside_skew', 'upside_skew', 'curvature'];
    const skip = () => window.dash_clientside.no_update;
    let pending = null, lastRequest = null;
    function selection(rows) {
        return (rows || []).filter(r => r.use === true).map(r => ({event_id: r.event_id, side: r.target, priority: r.priority === true})).sort((a,b) => a.event_id.localeCompare(b.event_id));
    }
    function specs(view) {
        return ((view && view.manifest && view.manifest.joint_fit && view.manifest.joint_fit.targets) || []).map(r => ({event_id:r.event_id, side:r.side, priority:r.priority})).sort((a,b) => a.event_id.localeCompare(b.event_id));
    }
    function key(values, rows, ref, anchor, mode, refresh) {
        return JSON.stringify([ref && ref.snapshot && ref.snapshot.snapshot_id, anchor, mode, refresh || 0, values, selection(rows)]);
    }
    function valid(values, rows, ref, anchor) {
        return ref && ref.anchor_event_id === anchor && values.every(v => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 20) && selection(rows).length > 0 && selection(rows).every(r => ['Bid','Mid','Offer','Trade'].includes(r.side));
    }
    function matches(view, values, rows, ref, anchor, mode) {
        const m = view && view.manifest;
        return Boolean(valid(values,rows,ref,anchor) && m && view.mode === mode && view.workspace_id === ref.snapshot.snapshot_id && m.product === ref.product && m.base_publication_id === ref.base_publication_id && names.every((n,i) => m.controls[n] === values[i]) && JSON.stringify(specs(view)) === JSON.stringify(selection(rows)));
    }
    function displayMatches(shown, offsets, origin, ref, mode, delivery) {
        return Boolean(origin && ref && origin.workspace_id === ref.snapshot.snapshot_id && origin.mode === mode && origin.delivery === delivery &&
            shown.every((v,i) => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 10 &&
                typeof offsets[i] === 'number' && Math.abs(v-origin.values[i]-offsets[i]) < 1e-9));
    }
    window.dash_clientside = Object.assign({}, window.dash_clientside, {iceSurface: {
        editRows: function (events, rows) {
            if (!events) return skip();
            let result = (rows || []).map(r => Object.assign({},r));
            for (const event of Array.isArray(events) ? events : [events]) {
                if (!event.data || !['use','target','priority'].includes(event.colId)) continue;
                result = result.map(r => {
                    if (r.event_id !== event.data.event_id || !r.eligible) return r;
                    // Installed AG Grid emits `value`; newer schemas use `newValue`.
                    const value = Object.prototype.hasOwnProperty.call(event,'newValue') ? event.newValue : event.value;
                    if (event.colId === 'target' && !(r.choices || []).includes(value)) return r;
                    if (event.colId !== 'target' && typeof value !== 'boolean') return r;
                    const next = Object.assign({},r,{[event.colId]:value});
                    if (event.colId !== 'use') next.use = true;
                    if (event.colId === 'target') {next.target_price = (r.target_prices || {})[value];next.market_iv=(r.market_ivs || {})[value];next.model=null;next.residual=null;next.residual_ticks=null;next.matched=null;next.fit_status='Pending';}
                    return next;
                });
            }
            return result;
        },
        paintRows: function (view, status, rows, ref) {
            if (status) {
                const pending=(rows || []).map(r => !r.use ? r : Object.assign({},r,{
                    model:null,residual:null,residual_ticks:null,matched:null,
                    fit_status:status==='Fitting…' ? 'Updating…' : 'Unavailable',fit_tooltip:status}));
                return JSON.stringify(pending)===JSON.stringify(rows) ? skip() : pending;
            }
            if (!view || !ref || view.workspace_id !== ref.snapshot.snapshot_id || JSON.stringify(specs(view)) !== JSON.stringify(selection(rows))) return skip();
            const comparisons = new Map(view.comparisons.map(c => [c.event_id,c]));
            const updated=(rows || []).map(r => {
                const c = r.use && comparisons.get(r.event_id);
                return Object.assign({},r,{model:c ? c.value : null,residual:c ? c.residual : null,residual_ticks:c ? c.residual_ticks : null,matched:c ? c.matched : null,target_price:c ? c.target_price : r.target_price,
                    surface_iv:c ? c.surface_iv : r.surface_iv,vol_tooltip:c ? c.vol_tooltip : r.vol_tooltip,
                    fit_status:c ? (c.matched ? 'OK' : 'Miss')+(c.conditional ? '*' : '') : (r.reason || 'Not selected'),
                    fit_tooltip:c ? 'Current adjustment '+(c.matched ? 'is within' : 'is outside')+' one tick of the selected '+c.target_side+' target.'+(c.conditional ? ' * Quote convention remains conditional.' : '') : r.reason});
            });
            return JSON.stringify(updated)===JSON.stringify(rows) ? skip() : updated;
        },
        request: function (level,down,up,curvature,rows,ref,anchor,mode,refresh,editor) {
            const values=[level,down,up,curvature], current=key(values,rows,ref,anchor,mode,refresh);
            if (pending && pending.key === current) return skip();
            if (pending) {clearTimeout(pending.timer);pending.resolve(skip());pending=null;}
            if (!valid(values,rows,ref,anchor) || current === lastRequest) return skip();
            return new Promise(resolve => { pending={key:current,resolve,timer:setTimeout(() => {
                pending=null;lastRequest=current;resolve({key:current,values,selection:selection(rows),ref,anchor,mode,editor});
            },200)}; });
        },
        accept: function (response,level,down,up,curvature,rows,ref,anchor,mode,refresh,shownLevel,shownDown,shownUp,shownCurve,origin,delivery,prior) {
            const values=[level,down,up,curvature];
            if (!ref || ref.anchor_event_id !== anchor) return [skip(),''];
            if (!valid(values,rows,ref,anchor)) return [skip(),selection(rows).length ? 'Enter total moves between −10 and +10.' : 'Select a quote.'];
            if (response && response.error && response.key === key(values,rows,ref,anchor,mode,refresh)) return [skip(),response.error];
            if (!displayMatches([shownLevel,shownDown,shownUp,shownCurve],values,origin,ref,mode,delivery)) return [skip(),'Fitting…'];
            if (response && response.key === key(values,rows,ref,anchor,mode,refresh)) {
                if (response.error) return [skip(),response.error];
                if (matches(response.view,values,rows,ref,anchor,mode)) return [response.view,''];
            }
            return [skip(),matches(prior,values,rows,ref,anchor,mode) ? '' : 'Fitting…'];
        },
        actions: function (view,level,down,up,curvature,rows,ref,receipt,confirmation,anchor,status,mode,shownLevel,shownDown,shownUp,shownCurve,origin,delivery) {
            const ready=!status && matches(view,[level,down,up,curvature],rows,ref,anchor,mode) &&
                displayMatches([shownLevel,shownDown,shownUp,shownCurve],[level,down,up,curvature],origin,ref,mode,delivery);
            return [!ready,!(ready && receipt && receipt.candidate_id === view.candidate_id && (confirmation || []).includes('confirm'))];
        },
        clearConfirmation: function () {return [[],''];},
        chartView: function (event,ref,month,anchor,prior) {
            if (!event || !ref) return skip();
            const scope=[ref.base_publication_id,anchor,month].join(':');
            const view=prior && prior.scope===scope ? Object.assign({},prior) : {scope};
            let changed=false;
            for (const axis of ['xaxis','yaxis']) {
                if (event[axis+'.autorange']) {delete view[axis];changed=true;}
                else if (event[axis+'.range[0]'] !== undefined && event[axis+'.range[1]'] !== undefined) {view[axis]=[event[axis+'.range[0]'],event[axis+'.range[1]']];changed=true;}
                else if (Array.isArray(event[axis+'.range'])) {view[axis]=event[axis+'.range'];changed=true;}
            }
            return changed ? view : skip();
        }
    }});
}());
