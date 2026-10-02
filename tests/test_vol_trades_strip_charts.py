import copy
from datetime import date

import pandas as pd
from dash import dcc

from options.option_expiry_engine import calculate_option_expiry
from options.options_library import black_76_futures_style
from options.ttf_volatility import year_fraction
from vol_trades_market_window import history_identity
from vol_trades_ice_quotes import prepare_strip_events
from vol_trades_workspace.strip_charts import render_strip_charts


HISTORY = {
    "product": "TFO",
    "snapshot_id": "snapshot",
    "business_date": "2026-10-01",
    "calibration": {"publication_id": "publication", "cob_date": "2026-10-01"},
}
WINDOW = {
    "history_key": history_identity(HISTORY),
    "mode": "snapshot",
    "start_at": "2026-10-02T00:00:00Z",
    "cutoff_at": "2026-10-02T12:00:00Z",
}


def row(**overrides):
    return {
        "event_id": "event",
        "observed_at": "2026-10-02T06:00:00Z",
        "product_code": "TFM",
        "contract_month": "2027-01-01",
        "strip_months": ["2027-01-01", "2027-02-01", "2027-03-01"],
        "strip_label": "Q1-27",
        "option_type": "C",
        "strike": 75.0,
        "forward": 70.0,
        "sender_handle": "broker",
        "source_channel": "chat",
        "structure_code": "",
        "valuation_status": "valued",
        "bid": 10.0,
        "offer": 11.0,
        **overrides,
    }


def loader(history, months):
    surface, forwards = [], []
    for month in months:
        month = pd.Timestamp(month).date()
        for strike in (20.0, 40.0, 60.0, 80.0, 100.0, 130.0):
            surface.append(
                dict(
                    contract_date=month,
                    option_expiration_date=calculate_option_expiry("ICE_TTF_TFO_71085679_EXPIRY", month),
                    strike=strike,
                    volatility=0.65,
                    put_call="C",
                    publication_id="publication",
                )
            )
        forwards.append(dict(contract_month=month, cob=date(2026, 10, 1), currency="EUR", units="MWh", value=70.0))
    return {
        "surface": pd.DataFrame(surface),
        "forwards": pd.DataFrame(forwards),
        "publication_id": "publication",
        "cob_date": "2026-10-01",
    }


def render(rows, **kwargs):
    return render_strip_charts(
        {"rows": rows, "market_context": WINDOW},
        HISTORY,
        WINDOW,
        kwargs.pop("x_axis", "strike"),
        kwargs.pop("layers", None),
        input_loader=kwargs.pop("input_loader", loader),
        settlement_loader=kwargs.pop("settlement_loader", lambda *_: None),
        **kwargs,
    )


def test_periods_append_chronologically_without_new_controls_or_summaries():
    winter = [value.strftime("%Y-%m-%d") for value in pd.date_range("2027-10-01", periods=6, freq="MS")]
    rows = [row(event_id="winter", strip_months=winter, contract_month=winter[0], strip_label="W27"), row()]
    panels = render(rows)
    assert [panel.children[0].children.children for panel in panels] == ["Q1-27", "Winter-27"]
    assert all(len(panel.children) == 2 and isinstance(panel.children[1], dcc.Graph) for panel in panels)
    assert all(panel.children[1].className == "brent-vol-history-graph" for panel in panels)
    assert all(
        panel.children[1].figure.layout.height == 308 and not panel.children[1].figure.layout.showlegend
        for panel in panels
    )
    assert panels[1].children[1].figure.layout.meta["months"][-1] == "2028-03-01"


def test_shared_layers_and_delta_mode_apply_to_strip_charts():
    figure = render([row()], x_axis="delta", layers=["ice-bid"])[0].children[1].figure
    assert list(figure.layout.xaxis.range) == [0, 1]
    assert all(trace.visible == (trace.meta["legend_layer"] == "ice-bid") for trace in figure.data)
    assert all(0 <= value <= 1 for trace in figure.data for value in trace.x)


def test_latest_blocked_update_cannot_resurrect_previous_quote():
    assert (
        prepare_strip_events(
            [row(), row(event_id="blocked", observed_at="2026-10-02T07:00:00Z", valuation_status="blocked")],
            "TFO",
            WINDOW,
        )
        == {}
    )
    assert (
        len(
            prepare_strip_events([row(), row(event_id="other", sender_handle="other")], "TFO", WINDOW)[
                "quarter:2027-Q1"
            ]
        )
        == 2
    )


def test_structures_monthlies_wrong_product_and_wrong_window_do_not_become_strip_points():
    assert render([row(structure_code="CALLSPR")]) == []
    assert render([row(strip_months=["2027-01-01"], strip_label="Jan-27")]) == []
    assert render([row(product_code="B")]) == []
    assert render([row(observed_at="2026-10-01T06:00:00Z")]) == []
    assert render([row()], contract="Q2-27") == []
    store = {"rows": [row()], "market_context": {**WINDOW, "history_key": "old"}}
    assert render_strip_charts(store, HISTORY, WINDOW, "strike", None, input_loader=loader) == []


def test_incomplete_period_remains_an_explicit_chart_without_partial_mark():
    def partial(history, months):
        data = loader(history, months)
        data["surface"] = data["surface"].loc[data["surface"].contract_date.ne(date(2027, 3, 1))]
        return data

    figure = render([row()], input_loader=partial)[0].children[1].figure
    assert not any(len(trace.x) for trace in figure.data)
    assert "Missing complete inputs for Mar-27" in figure.layout.annotations[0].text


def test_loader_failure_is_visible_and_has_no_sensitive_error_text():
    def failed(*args):
        raise RuntimeError("password=secret")

    figure = render([row()], input_loader=failed)[0].children[1].figure
    assert "unavailable" in figure.layout.annotations[0].text
    assert "secret" not in figure.layout.annotations[0].text


def test_hover_is_escaped_and_inputs_are_not_mutated():
    original = row(sender_handle="<script>x</script>")
    before = copy.deepcopy(original)
    figure = render([original])[0].children[1].figure
    assert "&lt;script&gt;" in figure.data[1].text[0]
    assert original == before


def settlement_loader(history, months):
    data = loader(history, months)
    chain = data["surface"].rename(
        columns={"contract_date": "underlying_contract_month", "volatility": "implied_volatility"}
    )
    chain["snapshot_id"] = "bbg-snapshot"
    chain["business_date"] = date(2026, 10, 1)
    chain["product"] = "TFO"
    chain["currency"] = "EUR"
    chain["price_unit"] = "EUR/MWH"
    chain["snapshot_kind"] = "SETTLEMENT"
    chain["underlying_price"] = 70.0
    chain["put_call"] = chain.strike.map(lambda strike: "P" if strike < 70 else "C")
    chain["settlement_price"] = chain.apply(
        lambda row: black_76_futures_style(
            row.put_call,
            70.0,
            row.strike,
            year_fraction(date(2026, 10, 1), row.option_expiration_date),
            row.implied_volatility,
        )[0],
        axis=1,
    )
    return {"chain": chain, "snapshot_id": "bbg-snapshot", "cob_date": "2026-10-01"}


def test_bloomberg_settlement_is_independent_of_calibration_and_uses_shared_toggle():
    def unavailable(*args):
        raise RuntimeError("calibration unavailable")

    figure = render([row()], input_loader=unavailable, settlement_loader=settlement_loader)[0].children[1].figure
    trace = next(trace for trace in figure.data if trace.meta["legend_layer"] == "bloomberg-settlement")
    assert trace.meta["derived"] and trace.meta["cob_date"] == "2026-10-01"
    assert len(trace.x) == 6 and all(value is not None for value in trace.y)
    assert "Exact monthly settlement strikes" in trace.text[0]
    assert "expiry" in trace.text[0] and "weight" in trace.text[0]
    hidden = render([row()], layers=["calibrated"], settlement_loader=settlement_loader)[0].children[1].figure
    assert next(trace for trace in hidden.data if trace.meta["legend_layer"] == "bloomberg-settlement").visible is False
    delta = render([row()], x_axis="delta", settlement_loader=settlement_loader)[0].children[1].figure
    assert all(0 <= value <= 1 for trace in delta.data for value in trace.x if value is not None)


def test_same_snapshot_corrected_monthly_values_invalidate_settlement_projection():
    def corrected(history, months):
        data = settlement_loader(history, months)
        data["chain"]["settlement_price"] *= 1.03
        return data

    before = render([row()], settlement_loader=settlement_loader)[0].children[1].figure.data[-1]
    after = render([row()], settlement_loader=corrected)[0].children[1].figure.data[-1]
    assert before.y != after.y


def test_intraday_uses_the_existing_prior_settlement_toggle():
    history = {**HISTORY, "snapshot_kind": "INTRADAY"}
    window = {**WINDOW, "history_key": history_identity(history)}
    figure = (
        render_strip_charts(
            {"rows": [row()], "market_context": window},
            history,
            window,
            "strike",
            ["prior-settlement"],
            input_loader=loader,
            settlement_loader=settlement_loader,
        )[0]
        .children[1]
        .figure
    )
    assert figure.data[-1].meta["legend_layer"] == "prior-settlement"
    assert figure.data[-1].visible


def test_settlement_loader_keeps_exact_snapshot_and_loads_all_months(monkeypatch):
    import vol_trades_strip_data as data

    selected = []

    def chain(snapshot_id, **kwargs):
        selected.append((snapshot_id, kwargs["snapshot_kind"]))
        return pd.DataFrame({"underlying_contract_month": [date(2030, 1, 1)]})

    monkeypatch.setattr(data, "load_chain_snapshot", chain)
    result = data.load_strip_settlement_inputs(HISTORY, [date(2027, 1, 1)], engine=object())
    assert result["snapshot_id"] == "snapshot" and result["cob_date"] == "2026-10-01"
    assert result["chain"].iloc[0].underlying_contract_month == date(2030, 1, 1)
    monkeypatch.setattr(
        data,
        "load_available_snapshots",
        lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "snapshot_id": "same-day",
                    "snapshot_kind": "SETTLEMENT",
                    "business_date": "2026-10-02",
                    "observed_at": "2026-10-02T20:00:00Z",
                },
                {
                    "snapshot_id": "prior",
                    "snapshot_kind": "SETTLEMENT",
                    "business_date": "2026-10-01",
                    "observed_at": "2026-10-01T20:00:00Z",
                },
                {
                    "snapshot_id": "older",
                    "snapshot_kind": "SETTLEMENT",
                    "business_date": "2026-09-30",
                    "observed_at": "2026-09-30T20:00:00Z",
                },
            ]
        ),
    )
    result = data.load_strip_settlement_inputs(
        {**HISTORY, "snapshot_kind": "INTRADAY", "business_date": "2026-10-02"}, [], engine=object()
    )
    assert result["snapshot_id"] == "prior" and result["cob_date"] == "2026-10-01"
    assert selected == [("snapshot", "SETTLEMENT"), ("prior", "SETTLEMENT")]
