"""BRENT preview and explicit publication controls in the existing expiry section."""
import logging
from dash_options.features.vol_trades.intraday import controller
from dash_options.features.vol_trades.intraday.controller import identity

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, Patch, callback, clientside_callback, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from options.vol_calibration.api import (save_brent_intraday_draft, load_brent_intraday_draft,
    list_brent_intraday_drafts, publish_brent_intraday_draft)
from dash_options.core.config import get_database_engine
from dash_options.calibration.auth import authorize, Permission
from dash_options.calibration.feature_flags import (
    brent_intraday_writes_enabled,
    brent_intraday_publication_enabled,
    calibration_enabled,
)
from dash_options.features.vol_trades import chart_data
from dash_options.features.vol_trades.intraday.jobs import brent_jobs as jobs
from dash_options.features.vol_trades.market_window import history_identity
import hashlib
import json
from uuid import uuid4

LOG = logging.getLogger(__name__)




def controls():
    return html.Div([
        html.Button("Review intraday", id="brent-intraday-review", className="brent-vol-history-refresh-button", disabled=True),
        html.Button("Publish intraday", id="brent-intraday-publish", className="brent-vol-history-refresh-button", disabled=True),
        html.Small(id="brent-intraday-status", role="status"),
        html.Small(id="brent-intraday-publication-status", role="status", **{"aria-live":"polite"}),
    ], id="brent-intraday-controls", className="tfo-intraday-controls", hidden=True)


def layout():
    return html.Div([
        *[dcc.Store(id="brent-intraday-"+name) for name in ("request", "job", "response", "view", "saved", "activation", "draft-view")],
        dcc.Interval(id="brent-intraday-poll", interval=1500, disabled=True),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle("Brent intraday surface")),
            dbc.ModalBody([
                html.Div(id="brent-intraday-context"),
                html.Label("Months to save or publish", htmlFor="brent-intraday-months"),
                dcc.Checklist(id="brent-intraday-months", options=[], value=[], className="tfo-intraday-months"),
                html.Label("Inspect month", htmlFor="brent-intraday-month"),
                dcc.Dropdown(id="brent-intraday-month", options=[], clearable=False),
                dcc.Graph(id="brent-intraday-chart", config={"displaylogo":False}),
                html.Div(id="brent-intraday-evidence"),
                html.Details([html.Summary("Month quality and excluded evidence"),
                    html.Div(id="brent-intraday-summary", className="tfo-intraday-summary")]),
                html.Details([html.Summary("Saved drafts"),
                    dcc.Dropdown(id="brent-intraday-drafts", placeholder="Choose a saved draft"),
                    html.Button("Open draft", id="brent-intraday-open", className="brent-vol-history-refresh-button")]),
            ]),
            dbc.ModalFooter([
                html.Div("Saving and verifying the surface… Please wait.", id="brent-intraday-action-progress",
                         role="status", hidden=True, className="tfo-intraday-action-status"),
                html.Div(id="brent-intraday-action-status", role="status", className="tfo-intraday-action-status",
                         **{"aria-live":"polite"}),
                html.Fieldset([
                    html.Button("Save draft", id="brent-intraday-save", className="brent-vol-history-refresh-button"),
                    html.Button("Publish selected months", id="brent-intraday-publish-card", className="brent-vol-history-refresh-button"),
                    html.Button("Close", id="brent-intraday-close", className="brent-vol-history-refresh-button"),
                ], id="brent-intraday-actions", disabled=False, className="tfo-intraday-actions"),
            ]),
        ], id="brent-intraday-modal", is_open=False, size="xl", scrollable=True,
           className="tfo-intraday-modal", backdrop_class_name="tfo-intraday-backdrop"),
    ])


@callback(Output("brent-intraday-request", "data"), Input("brent-vol-history-snapshot", "data"),
          Input("brent-vol-history-trade-window-state", "data"), Input("brent-vol-history-product", "value"),
          State("brent-intraday-request", "data"))
def market_request(history, window, product, previous=None):
    if (product != "BRENT" or not history or history.get("snapshot_kind") != "INTRADAY"
            or not window or window.get("history_key") != history_identity(history)):
        return None
    end = pd.Timestamp(history["observed_at"])
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
}""", Output("brent-intraday-view", "data"), Input("brent-intraday-request", "data"), Input("brent-intraday-response", "data"))


@callback(Output("brent-intraday-job", "data"), Input("brent-intraday-request", "data"))
def submit(request):
    return controller.submit(request, jobs=jobs, identity=identity, get_database_engine=get_database_engine, calibration_enabled=calibration_enabled, log=LOG)


@callback(Output("brent-intraday-response", "data"),
          Input("brent-intraday-job", "data"), Input("brent-intraday-poll", "n_intervals"),
          State("brent-intraday-request", "data"))
def poll(ticket, tick, request):
    return controller.poll(ticket, request, jobs=jobs, identity=identity, log=LOG)


clientside_callback("""function(request,ticket,response) {
    if(!request||!ticket||request.key!==ticket.request_key||ticket.error) return true;
    return !!(response&&response.request_key===request.key&&(response.error||response.status==='complete'||response.status==='failed'));
}""", Output("brent-intraday-poll", "disabled"), Input("brent-intraday-request", "data"),
    Input("brent-intraday-job", "data"), Input("brent-intraday-response", "data"))


def candidate(view, request):
    return controller.candidate(view, request, jobs=jobs, identity=identity)


def review_candidate(view, request, draft):
    return controller.review_candidate(view, request, draft, jobs=jobs, identity=identity)


@callback(Output("brent-intraday-controls", "hidden"), Output("brent-intraday-review", "disabled"),
          Output("brent-intraday-publish", "disabled"), Output("brent-intraday-status", "children"),
          Output("brent-intraday-context", "children"), Output("brent-intraday-months", "options"), Output("brent-intraday-months", "value"),
          Output("brent-intraday-month", "options"), Output("brent-intraday-month", "value"), Output("brent-intraday-summary", "children"),
          Output("brent-intraday-save", "disabled"), Output("brent-intraday-publish-card", "disabled"),
          Input("brent-intraday-view", "data"), Input("brent-intraday-request", "data"), Input("brent-intraday-draft-view", "data"))
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
        coverage = "Executable Bloomberg quotes; trade prints are context only"
        rows = [html.Tr([html.Td(pd.Timestamp(s["month"]).strftime("%b-%y")), html.Td(s["status"]), html.Td(s["strikes"]), html.Td(s.get("reason") or f"Mean IV error {s.get('mean_iv_error_points',0):.2f} points")]) for s in summaries]
        summary = [html.P(coverage+". Settlement publication remains unchanged."),
                   html.Table([html.Thead(html.Tr([html.Th(x) for x in ("Month", "Fit", "Strikes", "Quality")])), html.Tbody(rows)])]
        writable = False
        try:
            authorize(actor, Permission.CREATE_DRAFT)
            writable = brent_intraday_writes_enabled()
        except PermissionError:
            pass
        try:
            authorize(actor, Permission.PUBLISH)
            publish = writable and brent_intraday_publication_enabled() and fresh
        except PermissionError:
            publish = False
        return (False, False, not(selected and publish), f"{len(selected)} months ready", context,
                months, selected, inspection, (selected or [s["month"] for s in summaries] or [None])[0], summary,
                not(selected and writable), not(selected and publish))
    except Exception:
        LOG.exception("Intraday proposal rendering failed")
        return (*empty[:3], "Proposal unavailable. Reload the snapshot.", *empty[4:])


@callback(Output("brent-intraday-modal", "is_open"), Output("brent-intraday-draft-view", "data"),
          Output("brent-intraday-action-status", "children", allow_duplicate=True),
          Input("brent-intraday-review", "n_clicks"), Input("brent-intraday-publish", "n_clicks"),
          Input("brent-intraday-close", "n_clicks"), prevent_initial_call=True)
def toggle(review, publish, close):
    return ctx.triggered_id!="brent-intraday-close", None, ""


@callback(Output("brent-intraday-chart", "figure"), Output("brent-intraday-evidence", "children"),
          Input("brent-intraday-month", "value"), Input("brent-intraday-view", "data"),
          Input("brent-intraday-draft-view", "data"), State("brent-intraday-request", "data"),
          Input("brent-intraday-modal", "is_open"))
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
            display = {"strike": points.strike, "iv": points.volatility}
            fig.add_scatter(x=display['strike'], y=[100*v for v in display['iv']], name="Intraday proposal", line={"color":"#a855f7"})
        import dash_options.features.vol_trades.data as vol_trades_data
        base = vol_trades_data._cached_calibrated_surface_points(value["manifest"]["base_publication_id"], (pd.Timestamp(month).date(),))
        fig.add_scatter(x=base.strike, y=base.volatility*100, name="Published base", line={"color":"#64748b", "dash":"dash"})
        rows = summary.get("evidence", [])
        for kind, label, color, symbol in (("quote","Bloomberg quotes","#059669","circle"),
                                         ("trade","Bloomberg trades","#d97706","diamond-open")):
            evidence = [r for r in rows if r['kind']==kind]
            if not evidence:
                continue
            xs=[r['strike'] for r in evidence]
            fig.add_scatter(x=xs, y=[100*r['iv'] for r in evidence],mode="markers",name=label,
                marker={"color":color,"symbol":symbol},
                customdata=[[f"{r['strike']:g}{r['side']}",r.get('age_minutes',0)] for r in evidence],
                hovertemplate="%{customdata[0]} · %{y:.2f}% IV · %{customdata[1]:.1f} min old<extra>%{fullData.name}</extra>",
                error_y={"type":"data", "symmetric":False,
                    "array":[max(0,100*(r.get('ask_iv',r['iv'])-r['iv'])) for r in evidence],
                    "arrayminus":[max(0,100*(r['iv']-r.get('bid_iv',r['iv']))) for r in evidence],"thickness":1})
        fig.update_layout(template="plotly_white", height=340, margin={"l":45,"r":15,"t":20,"b":40}, xaxis_title="Strike · USD/bbl", yaxis_title="IV %")
        if rows and not points.empty:
            # Show the complete published support; never crop away unquoted wings.
            left, right = float(points.strike.min()), float(points.strike.max())
            quoted = [r['strike'] for r in rows if r['kind']=='quote']
            fig.update_xaxes(range=[left,right])
            if quoted:
                for start,end in ((left,min(quoted)),(max(quoted),right)):
                    if end>start:
                        fig.add_vrect(x0=start,x1=end,fillcolor='#e2e8f0',opacity=.35,
                                      line_width=0,layer='below',
                                      annotation_text='Extrapolated',annotation_position='top left')
        quality = (f"SVI density, wing and reconstruction checks passed · Mean IV error {summary.get('mean_iv_error_points',0):.2f} points"
                   if summary.get('eligible') else summary.get('reason', 'No accepted Brent fit'))
        detail = html.Small("Actual-strike executable quote fit · American futures-style pricing · USD/bbl")

        return fig, html.Div([html.P(quality),detail,
            html.Small("Excluded: "+"; ".join(f"{reason}: {count}" for reason,count in summary['excluded'].items()))])

    except Exception:
        LOG.exception("Intraday chart unavailable")
        return fig, "Candidate or base unavailable. Reload the snapshot."


@callback(Output("brent-intraday-saved", "data"), Output("brent-intraday-action-status", "children"),
          Output("brent-intraday-activation", "data"), Output("brent-intraday-modal", "is_open", allow_duplicate=True),
          Input("brent-intraday-save", "n_clicks"), Input("brent-intraday-publish-card", "n_clicks"),
          State("brent-intraday-view", "data"), State("brent-intraday-request", "data"), State("brent-intraday-months", "value"),
          State("brent-intraday-draft-view", "data"), prevent_initial_call=True,
          running=[(Output("brent-intraday-action-progress", "hidden"), False, True),
                   (Output("brent-intraday-action-status", "hidden"), True, False),
                   (Output("brent-intraday-actions", "disabled"), True, False)])
def save_or_publish(save, card, view, request, months, draft):
    try:
        actor, value = review_candidate(view, request, draft)
        publishing = ctx.triggered_id!="brent-intraday-save"
        authorize(actor, Permission.CREATE_DRAFT)
        if not brent_intraday_writes_enabled():
            raise ValueError("Intraday draft writes are disabled.")
        if publishing:
            authorize(actor, Permission.PUBLISH)
            if not brent_intraday_publication_enabled():
                raise ValueError("Intraday publication is disabled.")
        engine = get_database_engine(required=True)
        # Expected pointer was captured with the preview, not trusted browser state.
        expected = value["manifest"]["expected_intraday_id"]
        saved = save_brent_intraday_draft(engine, value, months, actor=actor.subject)
        if not publishing:
            return saved, "Draft saved and verified. Active surfaces are unchanged.", no_update, no_update
        receipt = publish_brent_intraday_draft(engine, saved["draft_id"], actor=actor.subject, expected_intraday_id=expected)
        activation = {**receipt, "selected_months":saved["months"]}
        return saved, "Intraday publication verified · "+receipt["publication_id"], activation, False
    except (ValueError, PermissionError) as exc:
        return no_update, str(exc), no_update, no_update
    except Exception:
        LOG.exception("Intraday write failed")
        return no_update, "Write unavailable. Check storage and permissions before retrying.", no_update, no_update


@callback(Output("brent-intraday-drafts", "options"), Input("brent-intraday-modal", "is_open"), Input("brent-intraday-saved", "data"))
def drafts(opened, saved):
    if not opened:
        raise PreventUpdate
    try:
        rows = list_brent_intraday_drafts(get_database_engine(required=True), actor=identity().subject)
        return [{"value":r["draft_id"], "label":str(r["created_at"])+" · "+", ".join(r["months"])} for r in rows]
    except Exception:
        LOG.exception("Intraday draft list unavailable")
        return []


@callback(Output("brent-intraday-draft-view", "data", allow_duplicate=True), Output("brent-intraday-action-status", "children", allow_duplicate=True),
          Input("brent-intraday-open", "n_clicks"), State("brent-intraday-drafts", "value"), State("brent-intraday-request", "data"), prevent_initial_call=True)
def open_draft(clicks, draft_id, request):
    if not clicks or not draft_id:
        raise PreventUpdate
    try:
        actor = identity()
        saved = load_brent_intraday_draft(get_database_engine(required=True), draft_id, actor=actor.subject)
    except Exception:
        LOG.exception("Saved intraday draft unavailable")
        return no_update, "Saved draft unavailable for this trader. Reload the draft list."
    value = saved["candidate"]
    if not request:
        return no_update, "Select BRENT intraday before reviewing a saved draft."
    return {"request_key":request["key"], "status":"complete", "ref":jobs.retain(value, actor.subject), "selected_months":saved["months"]}, "Saved candidate reopened without recalibration."


@callback(Output("vol-trades-publication-revision", "data", allow_duplicate=True),
          Input("brent-intraday-activation", "data"), prevent_initial_call=True)
def publication_changed(receipt):
    if not receipt:
        raise PreventUpdate
    return {"commodity":receipt["commodity"], "cob_date":receipt["publication_date"],
            "publication_id":receipt["publication_id"]}


@callback(Output("brent-intraday-publication-status", "children"),
          Input("brent-intraday-activation", "data"), Input("brent-vol-history-snapshot", "data"),
          Input("brent-vol-history-product", "value"))
def publication_feedback(receipt, history, product):
    if not receipt or product != "BRENT":
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
          Input("brent-intraday-view", "data"), Input("brent-vol-history-expiry-layer-manifest", "data"),
          State("brent-intraday-request", "data"), State("brent-vol-history-x-axis", "value"),
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
        if (contract.get("product") != "BRENT" or not trace
                or contract.get("generation")!=graph.get("generation")
                or contract.get("snapshot_id") != (request or {}).get("snapshot_id")):
            patches.append(no_update)
            continue
        selected = points if points.empty else points[pd.to_datetime(points.contract_date).dt.strftime("%Y-%m-%d")==graph["expiry"]]
        patch = Patch()
        x = [] if selected.empty else (1-selected.delta if axis==chart_data.X_AXIS_DELTA else selected.strike).tolist()
        patch["data"][trace["index"]]["x"] = x
        patch["data"][trace["index"]]["y"] = [] if selected.empty else (selected.volatility*100).tolist()
        patches.append(patch)
    return patches
