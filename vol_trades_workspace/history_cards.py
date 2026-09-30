"""History cards for the Vol Trades workspace."""

from __future__ import annotations

from vol_trades_workspace import chart_data, history_charts

from typing import Any
import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import (
    dcc,
    html,
)
import vol_trades_data as market_data

def _settlement_exclusion_note(
    exclusions: pd.DataFrame,
    *,
    product: str,
):
    total = len(exclusions)
    expiry_values = pd.to_datetime(
        exclusions["underlying_contract_month"], errors="coerce"
    ).dt.normalize()
    expiry_count = int(expiry_values.nunique())
    reason_items = []
    for (_, reason), rows in exclusions.groupby(
        ["reason_code", "reason"], sort=False, dropna=False
    ):
        count = len(rows)
        reason_items.append(
            html.Div(
                [
                    html.Span(
                        className="brent-vol-history-exclusion-reason-marker",
                        **{"aria-hidden": "true"},
                    ),
                    html.Span(
                        str(reason),
                        className="brent-vol-history-exclusion-reason-label",
                    ),
                    html.Span(
                        f"{count:,}",
                        className="brent-vol-history-exclusion-reason-count",
                        title=(
                            f"{count:,} excluded "
                            f"observation{'s' if count != 1 else ''}"
                        ),
                    ),
                ],
                className="brent-vol-history-exclusion-reason-row",
            )
        )
    expiry_groups = []
    working = exclusions.assign(_contract_month=expiry_values)
    for contract_month, rows in working.groupby("_contract_month", sort=True):
        side_groups = []
        for side, side_name in (("P", "Puts"), ("C", "Calls")):
            side_rows = rows.loc[
                rows["put_call"].astype(str).str.strip().str.upper().eq(side)
            ].sort_values("strike")
            if side_rows.empty:
                continue
            strike_chips = []
            for row in side_rows.itertuples():
                strike_value = market_data._numeric_or_none(row.strike)
                strike_label = f"{strike_value:g}" if strike_value is not None else "—"
                strike_chips.append(
                    html.Span(
                        strike_label,
                        className="brent-vol-history-exclusion-strike-chip",
                        title=f"{strike_label}{side}",
                        **{"aria-label": f"{strike_label} {side_name.lower()}"},
                    )
                )
            side_groups.append(
                html.Div(
                    [
                        html.Div(
                            [
                                html.Span(
                                    side,
                                    className=(
                                        "brent-vol-history-exclusion-side-code "
                                        f"brent-vol-history-exclusion-side-{side.lower()}"
                                    ),
                                    **{"aria-hidden": "true"},
                                ),
                                html.Span(side_name),
                            ],
                            className="brent-vol-history-exclusion-side-label",
                        ),
                        html.Div(
                            strike_chips,
                            className="brent-vol-history-exclusion-strike-list",
                        ),
                    ],
                    className="brent-vol-history-exclusion-side-row",
                )
            )
        row_count = len(rows)
        expiry_groups.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Strong(
                                pd.Timestamp(contract_month).strftime("%b-%y")
                            ),
                            html.Span(
                                f"{row_count:,} strike{'s' if row_count != 1 else ''}"
                            ),
                        ],
                        className="brent-vol-history-exclusion-expiry-heading",
                    ),
                    html.Div(
                        side_groups,
                        className="brent-vol-history-exclusion-expiry-sides",
                    ),
                ],
                className="brent-vol-history-exclusion-expiry-group",
            )
        )
    pricing_future_label = market_data._product_spec(product)["underlying_label"]
    observation_word = "observation" if total == 1 else "observations"
    expiry_word = "expiry" if expiry_count == 1 else "expiries"
    return html.Aside(
        [
            html.Div(
                [
                    html.Span(
                        "IV",
                        className="brent-vol-history-exclusion-badge",
                        **{"aria-hidden": "true"},
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H3(
                                        "Excluded from settlement IV charts",
                                        id=(
                                            "brent-vol-history-settlement-"
                                            "exclusion-title"
                                        ),
                                    ),
                                    html.Div(
                                        [
                                            html.Span(
                                                f"{total:,} excluded",
                                                className=(
                                                    "brent-vol-history-exclusion-"
                                                    "metric brent-vol-history-"
                                                    "exclusion-metric-primary"
                                                ),
                                            ),
                                            html.Span(
                                                f"{expiry_count:,} {expiry_word}",
                                                className=(
                                                    "brent-vol-history-exclusion-"
                                                    "metric"
                                                ),
                                            ),
                                        ],
                                        className=(
                                            "brent-vol-history-exclusion-metrics"
                                        ),
                                        **{"aria-label": (
                                            f"{total:,} excluded OTM strike "
                                            f"{observation_word} across "
                                            f"{expiry_count:,} {expiry_word}"
                                        )},
                                    ),
                                ],
                                className="brent-vol-history-exclusion-title-row",
                            ),
                            html.P(
                                "A strict OTM quality gate keeps non-resolvable "
                                "observations out of the smile. Official premiums remain "
                                "available in Option-chain detail.",
                                className="brent-vol-history-exclusion-summary",
                            ),
                        ],
                        className="brent-vol-history-exclusion-intro",
                    ),
                ],
                className="brent-vol-history-exclusion-header",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H4("Quality gate"),
                            html.Div(
                                reason_items,
                                className="brent-vol-history-exclusion-reasons",
                            ),
                        ],
                        className=(
                            "brent-vol-history-exclusion-panel "
                            "brent-vol-history-exclusion-quality"
                        ),
                    ),
                    html.Div(
                        [
                            html.H4("Affected strikes"),
                            html.Div(
                                expiry_groups,
                                className="brent-vol-history-exclusion-expiries",
                            ),
                        ],
                        className=(
                            "brent-vol-history-exclusion-panel "
                            "brent-vol-history-exclusion-strikes"
                        ),
                    ),
                ],
                className="brent-vol-history-exclusion-body",
            ),
            html.P(
                [
                    html.Span(
                        className="brent-vol-history-exclusion-footnote-marker",
                        **{"aria-hidden": "true"},
                    ),
                    f"OTM convention: puts below {pricing_future_label}; calls at or "
                    "above it. An unresolved OTM option is never replaced by an "
                    "intrinsic-value ITM option.",
                ],
                className="brent-vol-history-exclusion-footnote",
            ),
        ],
        className="brent-vol-history-settlement-exclusion-note",
        **{
            "aria-labelledby": "brent-vol-history-settlement-exclusion-title",
            "role": "note",
        },
    )


def build_plot_cards(
    chain: pd.DataFrame,
    published: pd.DataFrame,
    x_axis: str = chart_data.X_AXIS_STRIKE,
    trade_tape: pd.DataFrame | None = None,
    prior_settlement_chain: pd.DataFrame | None = None,
    product: str | None = None,
    calibrated: pd.DataFrame | None = None,
):
    x_axis = chart_data._normalize_x_axis(x_axis)
    resolved_product = market_data._normalize_product(
        product
        or (
            chain["product"].iloc[0]
            if chain is not None and not chain.empty and "product" in chain.columns
            else market_data.PRODUCT
        )
    )
    product_label = market_data._product_spec(resolved_product)["label"]
    if chain is None or chain.empty:
        if resolved_product == "JKM":
            return [
                dbc.Alert(
                    "No exact-COB ICAP JKM smile with ICE_JKM_MO forwards is available.",
                    color="secondary",
                )
            ]
        return [
            dbc.Alert(
                f"No complete Bloomberg {product_label} option-chain snapshot is available.",
                color="secondary",
            )
        ]
    # Settlement calibration suitability is carried in the authoritative
    # Bloomberg-point hover; it is not rendered as a second smile.
    prepared = pd.DataFrame()
    expiries = sorted(
        pd.to_datetime(chain["underlying_contract_month"], errors="coerce")
        .dropna()
        .dt.normalize()
        .unique()
    )
    cob_date = pd.to_datetime(chain["business_date"], errors="coerce").dropna().iloc[0]
    forward_by_contract = {
        pd.Timestamp(row.underlying_contract_month).normalize(): float(row.underlying_price)
        for row in chain[["underlying_contract_month", "underlying_price"]]
        .dropna()
        .drop_duplicates()
        .itertuples(index=False)
    }
    published_nodes = (
        pd.DataFrame() if resolved_product in market_data.EXACT_COB_SURFACE_PRODUCTS
        else market_data.published_strike_nodes(published, forward_by_contract, cob_date)
    )
    calibrated_nodes = calibrated if calibrated is not None else pd.DataFrame()
    cards = []
    if resolved_product in {"LNE", "ON"} and calibrated_nodes.empty:
        missing = calibrated_nodes.attrs.get("publication_status") == "no_publication"
        cards.append(dbc.Alert(
            (
                f"No HH surface published for this settlement date ({cob_date:%Y-%m-%d})."
                if missing else
                f"HH surface unavailable for {cob_date:%Y-%m-%d}; publication data could not be loaded."
            ),
            color="secondary" if missing else "warning",
        ))
    for expiry_value in expiries:
        expiry = pd.Timestamp(expiry_value).normalize()
        figure = history_charts.build_expiry_figure(
            chain,
            prepared,
            published_nodes,
            expiry,
            x_axis=x_axis,
            trade_tape=trade_tape,
            prior_settlement_chain=prior_settlement_chain,
            product=resolved_product,
            calibrated_nodes=calibrated_nodes,
        )
        label = expiry.strftime("%b-%y")
        quality = dict(figure.layout.meta or {}).get("quality") or {}
        quality_summary = (
            html.Div(
                [
                    dbc.Badge(
                        quality["status"],
                        color=quality["color"],
                        pill=True,
                        className="brent-vol-history-quality-badge",
                    ),
                    html.Span(
                        quality["detail"],
                        className="brent-vol-history-quality-detail",
                    ),
                ],
                className="brent-vol-history-card-quality",
                role="status",
                title=quality["detail"],
            )
            if quality
            else None
        )
        cards.append(
            html.Section(
                [
                    html.Header(
                        [
                            html.H3(
                                label,
                                className="brent-vol-history-card-title",
                            ),
                            quality_summary,
                        ],
                        className="brent-vol-history-card-header",
                    ),
                    dcc.Graph(
                        id={
                            "type": "brent-vol-history-expiry-graph",
                            "expiry": expiry.date().isoformat(),
                            "generation": f"preview-{resolved_product}-{x_axis}",
                        },
                        figure=figure,
                        config={
                            "displaylogo": False,
                            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                            "responsive": True,
                        },
                        className="brent-vol-history-graph",
                    ),
                ],
                className="brent-vol-history-expiry-card",
                role="region",
                **{
                    "aria-label": (
                        f"{label} {product_label} smile, volume, and open interest"
                    )
                },
            )
        )
    exclusions = chart_data._settlement_chart_exclusions(chain, x_axis)
    if not exclusions.empty:
        cards.append(
            _settlement_exclusion_note(
                exclusions,
                product=resolved_product,
            )
        )
    return cards


def build_jkm_official_cards(
    market: pd.DataFrame,
    calibrated: pd.DataFrame | None,
    *,
    x_axis: str,
) -> list[Any]:
    """Render JKM official nodes in the established Vol Trades card system."""

    if market is None or market.empty:
        return [
            dbc.Alert(
                "No exact-COB ICAP JKM smile with ICE_JKM_MO forwards is available.",
                color="secondary",
            )
        ]
    cards: list[Any] = [
        dbc.Alert(
            [
                html.Strong("Official-source market view: "),
                "ICAP JKM nodes and exact-COB ICE_JKM_MO forwards. "
                "No Bloomberg chain or trade tape is configured.",
            ],
            color="info",
            className="brent-vol-history-jkm-source-note",
        )
    ]
    active = calibrated if isinstance(calibrated, pd.DataFrame) else pd.DataFrame()
    for expiry, group in market.groupby("expiry", sort=True):
        expiry_ts = pd.Timestamp(expiry).normalize()
        ordered = group.sort_values("delta")
        x_column = "strike" if chart_data._normalize_x_axis(x_axis) == chart_data.X_AXIS_STRIKE else "delta"
        figure = go.Figure()
        figure.add_trace(
            go.Scatter(
                x=ordered[x_column],
                y=100.0 * ordered["iv"],
                mode="markers",
                name="Official ICAP nodes",
                opacity=0.65,
                marker={"color": "#0b5cab", "size": 2, "symbol": "circle"},
                line={"color": "#0b5cab", "width": 0.9},
                meta={"role": "official-market", "layer": "settlement"},
                customdata=np.column_stack(
                    [ordered["forward"], ordered["delta"], ordered["strike"]]
                ),
                hovertemplate=(
                    "Official ICAP JKM<br>Forward %{customdata[0]:.4f}"
                    "<br>Call delta %{customdata[1]:.2f}"
                    "<br>Strike %{customdata[2]:.4f}"
                    "<br>IV %{y:.2f}%<extra></extra>"
                ),
            )
        )
        if not active.empty and "contract_date" in active.columns:
            active_period = pd.to_datetime(
                active["contract_date"], errors="coerce"
            ).dt.to_period("M")
            published = active.loc[active_period.eq(expiry_ts.to_period("M"))].copy()
            if not published.empty:
                published_x = (
                    pd.to_numeric(published["strike"], errors="coerce")
                    if x_column == "strike"
                    else pd.to_numeric(published["delta"], errors="coerce")
                )
                figure.add_trace(
                    go.Scatter(
                        x=published_x,
                        y=100.0 * pd.to_numeric(
                            published["volatility"], errors="coerce"
                        ),
                        mode="lines",
                        name="Active JKM publication",
                        opacity=0.50,
                        line={"color": "#7c3aed", "width": 2},
                        meta={"role": "calibrated", "layer": "calibrated"},
                        hovertemplate="Active publication<br>IV %{y:.2f}%<extra></extra>",
                    )
                )
        figure.update_layout(
            template="plotly_white",
            margin={"l": 45, "r": 18, "t": 18, "b": 42},
            height=330,
            xaxis_title="Strike" if x_column == "strike" else "Call delta",
            yaxis_title="Implied volatility (%)",
            legend={"orientation": "h", "y": 1.12, "x": 0},
            meta={"expiry": expiry_ts.date().isoformat(), "quality": {
                "status": "Official",
                "color": "success",
                "detail": "Exact-COB ICAP nodes with ICE_JKM_MO forward",
            }},
        )
        label = expiry_ts.strftime("%b-%y")
        cards.append(
            html.Section(
                [
                    html.Header(
                        [
                            html.H3(label, className="brent-vol-history-card-title"),
                            dbc.Badge(
                                "Official",
                                color="success",
                                pill=True,
                                className="brent-vol-history-quality-badge",
                            ),
                        ],
                        className="brent-vol-history-card-header",
                    ),
                    dcc.Graph(
                        id={
                            "type": "brent-vol-history-expiry-graph",
                            "expiry": expiry_ts.date().isoformat(),
                            "generation": f"jkm-{expiry_ts.date().isoformat()}-{x_axis}",
                        },
                        figure=figure,
                        config={"displaylogo": False, "responsive": True},
                        className="brent-vol-history-graph",
                    ),
                ],
                className="brent-vol-history-expiry-card",
                role="region",
                **{"aria-label": f"{label} JKM official volatility smile"},
            )
        )
    return cards


def _oi_methodology_children(product: str):
    resolved_product = market_data._normalize_product(product)
    if resolved_product == "JKM":
        title = "JKM official-source scope"
        copy = (
            "The JKM view is sourced from official ICAP volatility nodes and the "
            "exact-COB ICE_JKM_MO forward curve. Bloomberg chain, trade-tape, "
            "volume and open-interest fields are intentionally unavailable."
        )
        link_label = "ICE JKM LNG average-price option specification"
        href = "https://www.ice.com/api/productguide/spec/71090519/pdf"
    elif resolved_product == "TFO":
        title = "ICE Endex open-interest timing"
        copy = (
            "Official TFO open interest can be published after the option settlement. "
            "This page keeps the Bloomberg effective date, marks earlier observations "
            "as stale, and never carries settlement OI into the intraday view. Delayed "
            "OI publication does not change the recorded option or TZT futures settlement."
        )
        link_label = "ICE Dutch TTF Natural Gas options"
        href = "https://www.ice.com/products/71085679/Dutch-TTF-Natural-Gas-Options"
    elif resolved_product in {"ON", "LNE"}:
        title = "CME Henry Hub open-interest timing"
        copy = (
            "Bloomberg reports CME Henry Hub open interest with its effective date. "
            "This page marks earlier observations as stale and never carries settlement "
            "OI into the intraday view; delayed OI does not revise the recorded option "
            "premium or NG futures settlement."
        )
        link_label = "CME Henry Hub options"
        href = (
            "https://www.cmegroup.com/markets/energy/natural-gas/"
            "natural-gas.contractSpecs.options.html"
        )
    else:
        title = "ICE open-interest timing"
        copy = (
            "ICE Futures Europe calculates official open interest from positions held "
            "at the previous trading day’s close after the 10:00 UK position-maintenance "
            "cutoff on the next trading day. Until Bloomberg publishes a value for the "
            "selected business date, this page shows OI as pending and does not carry "
            "forward a prior-day figure. This routine OI process does not revise settlement "
            "prices or reported volume."
        )
        link_label = "ICE Futures Europe position-maintenance guidance"
        href = (
            "https://www.ice.com/publicdocs/futures/"
            "ICE_Futures_Europe_Position_Maintenance_Cut_Off_times.pdf"
        )
    return [
        html.Span(
            "OI",
            className="brent-vol-history-methodology-badge",
            **{"aria-hidden": "true"},
        ),
        html.Div(
            [
                html.H2(title, id="brent-vol-history-oi-methodology-title"),
                html.P(copy),
                html.A(
                    link_label,
                    href=href,
                    target="_blank",
                    rel="noopener noreferrer",
                ),
            ],
            className="brent-vol-history-methodology-copy",
        ),
    ]
