"""TFO preview and explicit publication controls in the existing expiry section."""
import logging

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, Patch, callback, clientside_callback, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from options.vol_calibration.api import (save_tfo_intraday_draft, load_tfo_intraday_draft,
    list_tfo_intraday_drafts, publish_tfo_intraday_draft, tfo_proposal_strip_inputs, intraday_display_curve)
from runtime_config import get_database_engine
from vol_calibration.auth import current_request_identity, authorize, Permission
from vol_calibration.feature_flags import ttf_intraday_writes_enabled, ttf_publication_enabled, calibration_enabled
from vol_trades_workspace import chart_data
import tfo_intraday_jobs as jobs
from vol_trades_market_window import history_identity
import hashlib
import json
from uuid import uuid4
from workspace_cache import WorkspaceLoadCache

_STRIP_INPUTS = WorkspaceLoadCache(max_entries=8)

LOG = logging.getLogger(__name__)


def identity(permission=Permission.VIEW):
    actor = current_request_identity()
    authorize(actor, permission)
    if not actor.subject:
        raise ValueError("An identified trader is required.")
    return actor


def controls():
    return html.Div([
        html.Button("Review intraday", id="tfo-intraday-review", className="brent-vol-history-refresh-button", disabled=True),
        html.Button("Publish intraday", id="tfo-intraday-publish", className="brent-vol-history-refresh-button", disabled=True),
        html.Small(id="tfo-intraday-status", role="status"),
        html.Small(id="tfo-intraday-publication-status", role="status", **{"aria-live":"polite"}),
    ], id="tfo-intraday-controls", className="tfo-intraday-controls", hidden=True)


def layout():
    return html.Div([
        *[dcc.Store(id="tfo-intraday-"+name) for name in ("request", "job", "response", "view", "saved", "activation", "draft-view")],
        dcc.Interval(id="tfo-intraday-poll", interval=1500, disabled=True),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle("TFO intraday surface")),
            dbc.ModalBody([
                html.Div(id="tfo-intraday-context"),
                html.Label("Months to save or publish", htmlFor="tfo-intraday-months"),
                dcc.Checklist(id="tfo-intraday-months", options=[], value=[], className="tfo-intraday-months"),
                html.Label("Inspect month", htmlFor="tfo-intraday-month"),
                dcc.Dropdown(id="tfo-intraday-month", options=[], clearable=False),
                dcc.Graph(id="tfo-intraday-chart", config={"displaylogo":False}),
                html.Div(id="tfo-intraday-evidence"),
                html.Details([html.Summary("Month quality and excluded evidence"),
                    html.Div(id="tfo-intraday-summary", className="tfo-intraday-summary")]),
                html.Details([html.Summary("Saved drafts"),
                    dcc.Dropdown(id="tfo-intraday-drafts", placeholder="Choose a saved draft"),
                    html.Button("Open draft", id="tfo-intraday-open", className="brent-vol-history-refresh-button")]),
            ]),
            dbc.ModalFooter([
                html.Div("Saving and verifying the surface… Please wait.", id="tfo-intraday-action-progress",
                         role="status", hidden=True, className="tfo-intraday-action-status"),
                html.Div(id="tfo-intraday-action-status", role="status", className="tfo-intraday-action-status",
                         **{"aria-live":"polite"}),
                html.Fieldset([
                    html.Button("Save draft", id="tfo-intraday-save", className="brent-vol-history-refresh-button"),
                    html.Button("Publish selected months", id="tfo-intraday-publish-card", className="brent-vol-history-refresh-button"),
                    html.Button("Close", id="tfo-intraday-close", className="brent-vol-history-refresh-button"),
                ], id="tfo-intraday-actions", disabled=False, className="tfo-intraday-actions"),
            ]),
        ], id="tfo-intraday-modal", is_open=False, size="xl", scrollable=True,
           className="tfo-intraday-modal", backdrop_class_name="tfo-intraday-backdrop"),
    ])


@callback(Output("tfo-intraday-request", "data"), Input("brent-vol-history-snapshot", "data"),
          Input("brent-vol-history-trade-window-state", "data"), Input("brent-vol-history-product", "value"),
          State("tfo-intraday-request", "data"))
def market_request(history, window, product, previous=None):
    if (product != "TFO" or not history or history.get("snapshot_kind") != "INTRADAY"
            or not window or window.get("history_key") != history_identity(history)):
        return None
    end = min(pd.Timestamp(history["observed_at"]), pd.Timestamp(window["cutoff_at"]))
    start = max(pd.Timestamp(window["start_at"]), end-pd.Timedelta(hours=1))
    if pd.isna(start) or pd.isna(end) or start >= end:
        return None
    request = {"snapshot_id": history["snapshot_id"], "start": start.isoformat(), "end": end.isoformat(),
               "scope": (previous or {}).get("scope") or str(uuid4()),
               "display_publication_id": (history.get("calibration") or {}).get("publication_id")}
    request["key"] = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    return no_update if previous and previous.get("key") == request["key"] else request

clientside_callback("""function(request,response) {
    return request&&response&&request.key===response.request_key?response:null;
}""", Output("tfo-intraday-view", "data"), Input("tfo-intraday-request", "data"), Input("tfo-intraday-response", "data"))


@callback(Output("tfo-intraday-job", "data"), Input("tfo-intraday-request", "data"))
def submit(request):
    if not request or not calibration_enabled():
        return None
    try:
        actor = identity()
        return jobs.submit(get_database_engine(required=True), request, actor.subject)
    except Exception:
        LOG.exception("Intraday preview submission failed")
        return {"request_key":request["key"], "error":"Intraday preview unavailable. Check source access."}


@callback(Output("tfo-intraday-response", "data"),
          Input("tfo-intraday-job", "data"), Input("tfo-intraday-poll", "n_intervals"),
          State("tfo-intraday-request", "data"))
def poll(ticket, tick, request):
    if not request or not ticket or ticket["request_key"]!=request["key"]:
        return no_update
    if ticket.get("error"):
        return ticket
    try:
        actor = identity()
        state = jobs.poll(ticket, actor.subject)
        return {"request_key":request["key"], **{k:state[k] for k in ("status", "ref", "error") if k in state}}
    except Exception:
        LOG.exception("Intraday preview polling failed")
        return {"request_key":request["key"], "error":"Preview expired. Reload the market snapshot."}


clientside_callback("""function(request,ticket,response) {
    if(!request||!ticket||request.key!==ticket.request_key||ticket.error) return true;
    return !!(response&&response.request_key===request.key&&(response.error||response.status==='complete'||response.status==='failed'));
}""", Output("tfo-intraday-poll", "disabled"), Input("tfo-intraday-request", "data"),
    Input("tfo-intraday-job", "data"), Input("tfo-intraday-response", "data"))


def candidate(view, request):
    if not request or not view or view.get("request_key")!=request["key"] or not view.get("ref"):
        raise ValueError("Wait for the current snapshot's proposal.")
    actor = identity()
    value = jobs.resolve(view["ref"], actor.subject)
    manifest = value["manifest"]
    if (manifest["snapshot_id"]!=request["snapshot_id"] or pd.Timestamp(manifest["cutoff"])!=pd.Timestamp(request["end"])
            or pd.Timestamp(manifest["window_start"])!=pd.Timestamp(request["start"])):
        raise ValueError("Candidate belongs to a different market window.")
    return actor, value


def review_candidate(view, request, draft):
    if draft and request and draft.get("request_key") == request["key"]:
        actor = identity()
        return actor, jobs.resolve(draft["ref"], actor.subject)
    return candidate(view, request)


def proposal_strip_inputs(view, request):
    if not view or not view.get("ref"):
        return None
    actor, value = candidate(view, request)
    if not value["surface"]:
        return None
    return _STRIP_INPUTS.get_or_load((actor.subject, value["candidate_id"]),
        lambda:tfo_proposal_strip_inputs(get_database_engine(required=True), value),
        force_refresh=False, degraded=lambda _:False, healthy_ttl_seconds=3600, degraded_ttl_seconds=5)


@callback(Output("tfo-intraday-controls", "hidden"), Output("tfo-intraday-review", "disabled"),
          Output("tfo-intraday-publish", "disabled"), Output("tfo-intraday-status", "children"),
          Output("tfo-intraday-context", "children"), Output("tfo-intraday-months", "options"), Output("tfo-intraday-months", "value"),
          Output("tfo-intraday-month", "options"), Output("tfo-intraday-month", "value"), Output("tfo-intraday-summary", "children"),
          Output("tfo-intraday-save", "disabled"), Output("tfo-intraday-publish-card", "disabled"),
          Input("tfo-intraday-view", "data"), Input("tfo-intraday-request", "data"), Input("tfo-intraday-draft-view", "data"))
def render(view, request, draft):
    empty = (not bool(request), True, True, "Calculating intraday proposal…" if request else "", "", [], [], [], None, "", True, True)
    if not calibration_enabled():
        return (True, True, True, "Intraday calibration is disabled.", *empty[4:])
    if not request or not view:
        return empty
    if view.get("error"):
        return (*empty[:3], view["error"], *empty[4:])
    if not view.get("ref"):
        return empty
    try:
        actor, value = review_candidate(view, request, draft)
        summaries = value["summaries"]
        months = [{"value":s["month"], "label":pd.Timestamp(s["month"]).strftime("%b-%y")+" · "+s["status"]} for s in summaries if s["eligible"]]
        selected = [s["month"] for s in summaries if s["eligible"]]
        if draft and draft.get("request_key") == request["key"]:
            selected = [month for month in selected if month in draft["selected_months"]]
        inspection = [{"value":s["month"], "label":pd.Timestamp(s["month"]).strftime("%b-%y")} for s in summaries]
        manifest = value["manifest"]
        cutoff = pd.Timestamp(manifest["cutoff"]).tz_convert("Asia/Dubai")
        start = pd.Timestamp(manifest["window_start"]).tz_convert("Asia/Dubai")
        context = f"Fit window {start:%d %b %H:%M}–{cutoff:%H:%M} GST · base COB {manifest['settlement_cob']}. Unselected months keep the base surface."
        fresh = pd.Timestamp.now(tz="UTC") <= pd.Timestamp(value["publish_before"])
        if not fresh:
            context += " This preview can be saved; refresh intraday before publishing."
        coverage = "; ".join(sorted({str(r.get("coverage_status")) for r in manifest["tape"]}))
        rows = [html.Tr([html.Td(pd.Timestamp(s["month"]).strftime("%b-%y")), html.Td(s["status"]), html.Td(s["strikes"]), html.Td(s.get("reason") or f"{s.get('matched_count',0)}/{s['observations']} within 1 tick · max gap {s.get('max_gap_ticks',0):.2f} ticks")]) for s in summaries]
        summary = [html.P("Tape coverage: "+coverage+". Package and unverified block legs are excluded from automatic targets."),
                   html.Table([html.Thead(html.Tr([html.Th(x) for x in ("Month", "Fit", "Strikes", "Quality")])), html.Tbody(rows)])]
        writable = False
        try:
            authorize(actor, Permission.CREATE_DRAFT)
            writable = ttf_intraday_writes_enabled()
        except PermissionError:
            pass
        try:
            authorize(actor, Permission.PUBLISH)
            publish = writable and ttf_publication_enabled() and fresh
        except PermissionError:
            publish = False
        return (False, False, not(selected and publish), f"{len(selected)} months ready", context,
                months, selected, inspection, (selected or [s["month"] for s in summaries] or [None])[0], summary,
                not(selected and writable), not(selected and publish))
    except Exception:
        LOG.exception("Intraday proposal rendering failed")
        return (*empty[:3], "Proposal unavailable. Reload the snapshot.", *empty[4:])


@callback(Output("tfo-intraday-modal", "is_open"), Output("tfo-intraday-draft-view", "data"),
          Output("tfo-intraday-action-status", "children", allow_duplicate=True),
          Input("tfo-intraday-review", "n_clicks"), Input("tfo-intraday-publish", "n_clicks"),
          Input("tfo-intraday-close", "n_clicks"), prevent_initial_call=True)
def toggle(review, publish, close):
    return ctx.triggered_id!="tfo-intraday-close", None, ""


@callback(Output("tfo-intraday-chart", "figure"), Output("tfo-intraday-evidence", "children"),
          Input("tfo-intraday-month", "value"), Input("tfo-intraday-view", "data"),
          Input("tfo-intraday-draft-view", "data"), State("tfo-intraday-request", "data"),
          Input("tfo-intraday-modal", "is_open"))
def chart(month, view, draft, request, is_open=False):
    if not is_open:
        return no_update, no_update
    fig = go.Figure()
    if not month or not view or not view.get("ref"):
        return fig, ""
    try:
        _, value = review_candidate(view, request, draft)
        summary = next(s for s in value["summaries"] if s["month"]==month)
        points = pd.DataFrame(value["surface"])
        if not points.empty:
            points = points[pd.to_datetime(points.contract_date).dt.strftime("%Y-%m-%d")==month]
            display = intraday_display_curve(value, month)
            fig.add_scatter(x=display['strike'], y=[100*v for v in display['iv']], name="Intraday proposal", line={"color":"#a855f7"})
        import vol_trades_data
        base = vol_trades_data._cached_calibrated_surface_points(value["manifest"]["base_publication_id"], (pd.Timestamp(month).date(),))
        fig.add_scatter(x=base.strike, y=base.volatility*100, name="Published base", line={"color":"#64748b", "dash":"dash"})
        rows = summary.get("evidence", [])
        for kind, label, color, symbol in (("quote","Bloomberg quotes","#059669","circle"),
                                         ("trade","Bloomberg trades","#d97706","diamond-open")):
            evidence = [r for r in rows if r['kind']==kind]
            if not evidence:
                continue
            # Trades use historical forwards. Plot them at equivalent current
            # moneyness so the plotted IV gap agrees with the pricing comparison.
            xs=[r['strike']/r['forward']*summary.get('forward',r['forward']) for r in evidence]
            fig.add_scatter(x=xs, y=[100*r['iv'] for r in evidence],mode="markers",name=label,
                marker={"color":color,"symbol":symbol},
                customdata=[[f"{r['strike']:g}{r['side']}",r.get('age_minutes',0)] for r in evidence],
                hovertemplate="%{customdata[0]} · %{y:.2f}% IV · %{customdata[1]:.1f} min old<extra>%{fullData.name}</extra>",
                error_y={"type":"data", "symmetric":False,
                    "array":[max(0,100*(r.get('ask_iv',r['iv'])-r['iv'])) for r in evidence],
                    "arrayminus":[max(0,100*(r['iv']-r.get('bid_iv',r['iv']))) for r in evidence],"thickness":1})
        fig.update_layout(template="plotly_white", height=340, margin={"l":45,"r":15,"t":20,"b":40}, xaxis_title="Strike · EUR/MWh", yaxis_title="IV %")
        if rows and not points.empty:
            # Show the complete published support; never crop away unquoted wings.
            left, right = float(points.strike.min()), float(points.strike.max())
            quoted = [r['strike']/r['forward']*summary['forward'] for r in rows if r['kind']=='quote']
            fig.update_xaxes(range=[left,right])
            if quoted:
                for start,end in ((left,min(quoted)),(max(quoted),right)):
                    if end>start:
                        fig.add_vrect(x0=start,x1=end,fillcolor='#e2e8f0',opacity=.35,
                                      line_width=0,layer='below',
                                      annotation_text='Extrapolated',annotation_position='top left')
        if summary.get('eligible') and summary.get('validation',{}).get('continuous_price_constraints'):
            quote_rows = [r for r in rows if r['kind']=='quote']
            trade_rows = [r for r in rows if r['kind']=='trade']
            wing_status = "Full wings checked · Tails rise" if summary.get("validation",{}).get("full_smile_shape",{}).get("is_valid") and summary["validation"]["full_smile_shape"].get("tails_rise") else "Wing shape not verified"
            quality = f"Strike-arbitrage checks passed · {wing_status} · {sum(r['matched'] for r in quote_rows)}/{len(quote_rows)} quotes within band + 1 tick"
            if trade_rows:
                quality += f" · {sum(r['matched'] for r in trade_rows)}/{len(trade_rows)} trades within 1 tick"
            if summary.get('front_month'):
                quality += " · Front-month fit"
            gap_rows = [html.Tr([
                html.Td(f"{r['strike']:g}{r['side']}"), html.Td('Trade' if r['kind']=='trade' else 'Bid / offer'),
                html.Td(f"{r['low']:.4f}" if r['kind']=='trade' else f"{r['low']:.4f} / {r['high']:.4f}"),
                html.Td(f"{r['fitted_price']:.4f}"),html.Td(f"{100*r['fitted_iv']:.2f}%"),
                html.Td(f"{r['gap_ticks']:+.2f}",style={'color':'#18794e' if r['matched'] else '#b45309'}),
                html.Td(f"{r['iv_gap_points']:+.2f}"),
                html.Td(f"{r.get('age_minutes',0):.1f}"),
            ]) for r in rows]
            detail = html.Details([html.Summary('Fit gaps · quotes and trades'),
                html.Table([html.Thead(html.Tr([html.Th(x) for x in ('Option','Target','Market','Fitted','IV','Gap · ticks','Δ IV · points','Age · min')])),
                            html.Tbody(gap_rows)],className='table table-sm table-striped'),
                html.Small('Gaps use actual quoted bands and trade prices. Older Bloomberg trades carry less weight; repeated prints cannot dominate a strike. Markers use equivalent current moneyness.')])
        else:
            quality = summary.get('reason','No accepted smooth fit')
            detail = None
        return fig, html.Div([html.P(quality),detail,
            html.Small("Excluded: "+"; ".join(f"{reason}: {count}" for reason,count in summary['excluded'].items()))])

    except Exception:
        LOG.exception("Intraday chart unavailable")
        return fig, "Candidate or base unavailable. Reload the snapshot."


@callback(Output("tfo-intraday-saved", "data"), Output("tfo-intraday-action-status", "children"),
          Output("tfo-intraday-activation", "data"), Output("tfo-intraday-modal", "is_open", allow_duplicate=True),
          Input("tfo-intraday-save", "n_clicks"), Input("tfo-intraday-publish-card", "n_clicks"),
          State("tfo-intraday-view", "data"), State("tfo-intraday-request", "data"), State("tfo-intraday-months", "value"),
          State("tfo-intraday-draft-view", "data"), prevent_initial_call=True,
          running=[(Output("tfo-intraday-action-progress", "hidden"), False, True),
                   (Output("tfo-intraday-action-status", "hidden"), True, False),
                   (Output("tfo-intraday-actions", "disabled"), True, False)])
def save_or_publish(save, card, view, request, months, draft):
    try:
        actor, value = review_candidate(view, request, draft)
        publishing = ctx.triggered_id!="tfo-intraday-save"
        authorize(actor, Permission.CREATE_DRAFT)
        if not ttf_intraday_writes_enabled():
            raise ValueError("Intraday draft writes are disabled.")
        if publishing:
            authorize(actor, Permission.PUBLISH)
            if not ttf_publication_enabled():
                raise ValueError("Intraday publication is disabled.")
        engine = get_database_engine(required=True)
        # Expected pointer was captured with the preview, not trusted browser state.
        expected = value["manifest"]["expected_intraday_id"]
        saved = save_tfo_intraday_draft(engine, value, months, actor=actor.subject)
        if not publishing:
            return saved, "Draft saved and verified. Active surfaces are unchanged.", no_update, no_update
        receipt = publish_tfo_intraday_draft(engine, saved["draft_id"], actor=actor.subject, expected_intraday_id=expected)
        activation = {**receipt, "selected_months":saved["months"]}
        return saved, "Intraday publication verified · "+receipt["publication_id"], activation, False
    except (ValueError, PermissionError) as exc:
        return no_update, str(exc), no_update, no_update
    except Exception:
        LOG.exception("Intraday write failed")
        return no_update, "Write unavailable. Check storage and permissions before retrying.", no_update, no_update


@callback(Output("tfo-intraday-drafts", "options"), Input("tfo-intraday-modal", "is_open"), Input("tfo-intraday-saved", "data"))
def drafts(opened, saved):
    if not opened:
        raise PreventUpdate
    try:
        rows = list_tfo_intraday_drafts(get_database_engine(required=True), actor=identity().subject)
        return [{"value":r["draft_id"], "label":str(r["created_at"])+" · "+", ".join(r["months"])} for r in rows]
    except Exception:
        LOG.exception("Intraday draft list unavailable")
        return []


@callback(Output("tfo-intraday-draft-view", "data", allow_duplicate=True), Output("tfo-intraday-action-status", "children", allow_duplicate=True),
          Input("tfo-intraday-open", "n_clicks"), State("tfo-intraday-drafts", "value"), State("tfo-intraday-request", "data"), prevent_initial_call=True)
def open_draft(clicks, draft_id, request):
    if not clicks or not draft_id:
        raise PreventUpdate
    try:
        actor = identity()
        saved = load_tfo_intraday_draft(get_database_engine(required=True), draft_id, actor=actor.subject)
    except Exception:
        LOG.exception("Saved intraday draft unavailable")
        return no_update, "Saved draft unavailable for this trader. Reload the draft list."
    value = saved["candidate"]
    if not request:
        return no_update, "Select TFO intraday before reviewing a saved draft."
    return {"request_key":request["key"], "status":"complete", "ref":jobs.retain(value, actor.subject), "selected_months":saved["months"]}, "Saved candidate reopened without recalibration."


@callback(Output("vol-trades-publication-revision", "data", allow_duplicate=True),
          Input("tfo-intraday-activation", "data"), prevent_initial_call=True)
def publication_changed(receipt):
    if not receipt:
        raise PreventUpdate
    return {"commodity":receipt["commodity"], "cob_date":receipt["publication_date"],
            "publication_id":receipt["publication_id"]}


@callback(Output("tfo-intraday-publication-status", "children"),
          Input("tfo-intraday-activation", "data"), Input("brent-vol-history-snapshot", "data"),
          Input("brent-vol-history-product", "value"))
def publication_feedback(receipt, history, product):
    if not receipt or product != "TFO":
        return ""
    published_at = pd.Timestamp(receipt["published_at"]).tz_convert("Asia/Dubai")
    months = ", ".join(pd.Timestamp(month).strftime("%b-%y") for month in receipt["selected_months"])
    prefix = f"Published {months} · {published_at:%d %b %H:%M:%S} GST"
    current = history or {}
    if not current or current.get("calibration_status") == "query_error":
        return prefix + " · charts could not reload. Reload the page; publication is already saved."
    if current.get("business_date") != receipt["publication_date"] or current.get("snapshot_kind") != "INTRADAY":
        return prefix + " · select the published intraday date to view it."
    if current.get("publication_id") == receipt["publication_id"]:
        return prefix + " · expiry panels updated."
    return prefix + " · refreshing expiry panels…"


@callback(Output({"type":"brent-vol-history-expiry-graph", "expiry":ALL, "generation":ALL}, "figure", allow_duplicate=True),
          Input("tfo-intraday-view", "data"), Input("brent-vol-history-expiry-layer-manifest", "data"),
          State("tfo-intraday-request", "data"), State("brent-vol-history-x-axis", "value"),
          State({"type":"brent-vol-history-expiry-graph", "expiry":ALL, "generation":ALL}, "id"), prevent_initial_call=True)
def overlay(view, manifest, request, axis, graph_ids):
    points = pd.DataFrame()
    try:
        if view and view.get("ref"):
            _, value = candidate(view, request)
            points = pd.DataFrame(value["surface"])
    except Exception:
        LOG.exception("Intraday overlay unavailable")
    patches = []
    for graph in graph_ids or []:
        contract = (manifest or {}).get("graphs", {}).get(graph["expiry"], {})
        trace = next((t for t in contract.get("traces", []) if t["layer"]=="intraday-proposal"), None)
        if not trace or contract.get("generation")!=graph.get("generation"):
            patches.append(no_update)
            continue
        selected = points if points.empty else points[pd.to_datetime(points.contract_date).dt.strftime("%Y-%m-%d")==graph["expiry"]]
        patch = Patch()
        x = [] if selected.empty else (1-selected.delta if axis==chart_data.X_AXIS_DELTA else selected.strike).tolist()
        patch["data"][trace["index"]]["x"] = x
        patch["data"][trace["index"]]["y"] = [] if selected.empty else (selected.volatility*100).tolist()
        patches.append(patch)
    return patches
