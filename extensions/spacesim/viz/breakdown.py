"""
Per-stage latency breakdown plots.

The "stage" decomposition mirrors what ``parsers.results.stage_means_ms``
returns: uplink → gather wait → compute queue wait → compute → return.
We render this as either:

  - A horizontal stacked bar (one bar = "average request"), so each
    coloured segment is the mean time spent in that stage.
  - A grouped bar of percentiles per stage (p50 / mean / p99).
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go


# Stage names in the order they happen in the request lifecycle.
STAGE_ORDER: Sequence[str] = (
    "T_uplink",
    "D_gather",
    "T_queue_wait",
    "T_compute",
    "T_return",
)

STAGE_COLORS: Dict[str, str] = {
    "T_uplink":     "#2177b0",   # blue: ground → first SAT, network heavy
    "D_gather":     "#80007F",   # purple: gather window
    "T_queue_wait": "#fc7f2b",   # orange: compute queue
    "T_compute":    "#d42a2d",   # red: actual inference
    "T_return":     "#2f9e37",   # green: back over network
}


def plot_stage_breakdown(stage_means_ms: Dict[str, float], *, title: Optional[str] = None) -> go.Figure:
    """Horizontal stacked bar of mean per-stage latency (ms)."""
    if not stage_means_ms:
        return _empty_figure(title or "Stage breakdown: no data")

    fig = go.Figure()
    cumulative = 0.0
    for stage in STAGE_ORDER:
        v = float(stage_means_ms.get(stage, 0.0))
        if v <= 0:
            continue
        fig.add_trace(go.Bar(
            x=[v], y=["avg request"], orientation="h",
            name=stage, marker=dict(color=STAGE_COLORS.get(stage, "#999")),
            text=[f"{v:.1f}"], textposition="inside", insidetextanchor="middle",
            hovertemplate=f"{stage}=%{{x:.2f}} ms<extra></extra>",
        ))
        cumulative += v

    fig.update_layout(
        title=title or f"Mean per-stage latency (total {cumulative:.1f} ms)",
        barmode="stack",
        xaxis_title="latency (ms)",
        yaxis=dict(showticklabels=False),
        template="plotly_white",
        margin=dict(l=10, r=20, t=40, b=40),
        height=180,
        legend=dict(orientation="h", yanchor="top", y=-0.2, x=0),
    )
    return fig


def plot_stage_percentiles(df: pd.DataFrame, *, title: Optional[str] = None) -> go.Figure:
    """Grouped bar of p50 / mean / p99 per stage from the lifecycle df."""
    if df.empty:
        return _empty_figure(title or "Stage percentiles: no data")

    stats = []
    for stage in STAGE_ORDER:
        col = f"{stage}_ns"
        if col not in df.columns:
            continue
        ms = df[col].to_numpy() / 1e6
        stats.append({
            "stage": stage,
            "p50": float(np.percentile(ms, 50)),
            "mean": float(ms.mean()),
            "p99": float(np.percentile(ms, 99)),
        })
    if not stats:
        return _empty_figure(title or "Stage percentiles: no columns matched")

    s = pd.DataFrame(stats)
    fig = go.Figure()
    for metric, colour in (("p50", "#2177b0"), ("mean", "#fc7f2b"), ("p99", "#d42a2d")):
        fig.add_trace(go.Bar(
            x=s["stage"], y=s[metric],
            name=metric,
            marker=dict(color=colour),
            text=[f"{v:.1f}" for v in s[metric]],
            textposition="outside",
            hovertemplate=f"{metric}=%{{y:.2f}} ms<extra></extra>",
        ))
    fig.update_layout(
        title=title or "Per-stage latency percentiles",
        yaxis_title="latency (ms)",
        template="plotly_white",
        barmode="group",
        margin=dict(l=40, r=20, t=40, b=40),
        height=380,
    )
    return fig


def _empty_figure(title: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        title=title,
        template="plotly_white",
        annotations=[dict(
            text="no data yet — run a simulation first",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color="grey"),
        )],
        height=180,
    )
    return fig
