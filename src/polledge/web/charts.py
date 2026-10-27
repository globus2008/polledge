"""Altair charts for the web app (palette validated with the dataviz validator)."""

from __future__ import annotations

import altair as alt
import pandas as pd

# Fixed categorical order; a series keeps its colour because it is assigned by the
# candidate's position in the election config, not by rank.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
MAX_SERIES = len(SERIES_COLORS)


def series_colors(candidate_order: list[str], shown: list[str]) -> dict[str, str]:
    ordered = [c for c in candidate_order if c in shown]
    return {c: SERIES_COLORS[i] for i, c in enumerate(ordered[:MAX_SERIES])}


def poll_trend(df: pd.DataFrame, colors: dict[str, str], x_title: str, y_title: str, series_title: str) -> alt.Chart:
    """Individual polls as dots, a rolling average as a 2px line; one series per candidate.

    df columns: date, label (candidate name), share (percent), pollster.
    """
    domain = list(colors)
    scale = alt.Scale(domain=domain, range=[colors[d] for d in domain])
    base = alt.Chart(df).encode(
        x=alt.X("date:T", title=x_title),
        y=alt.Y("share:Q", title=y_title, scale=alt.Scale(zero=True)),
        color=alt.Color("label:N", title=series_title, scale=scale, sort=domain),
    )
    dots = base.mark_circle(size=64, opacity=0.35).encode(
        tooltip=[
            alt.Tooltip("date:T", title=x_title),
            alt.Tooltip("label:N", title=series_title),
            alt.Tooltip("share:Q", title=y_title, format=".1f"),
            alt.Tooltip("pollster:N"),
        ]
    )
    trend = base.transform_window(
        trend="mean(share)", frame=[-4, 0], groupby=["label"], sort=[alt.SortField("date")]
    ).mark_line(strokeWidth=2, interpolate="monotone").encode(y="trend:Q")
    return (dots + trend).properties(height=380).interactive(bind_y=False)
