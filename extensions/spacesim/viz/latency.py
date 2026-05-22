"""
Latency CDF and histogram plots.

Inputs are the per-request DataFrame returned by
``parsers.results.build_lifecycle_df``. Columns are all in nanoseconds;
we convert to milliseconds at plot time.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go


_PERCENTILE_TICKS = (50, 90, 99)


def plot_cdf(
    df: pd.DataFrame,
    column: str,
    *,
    title: Optional[str] = None,
    x_label: str = "latency (ms)",
) -> go.Figure:
    """Empirical CDF of ``df[column]`` (converted ns → ms).

    Annotates p50/p90/p99 with vertical dotted lines + a label.
    """
    if df.empty or column not in df.columns:
        return _empty_figure(f"{title or column}: no data")

    ms = (df[column].to_numpy() / 1e6)
    ms.sort()
    cdf_y = np.arange(1, len(ms) + 1) / len(ms)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ms, y=cdf_y, mode="lines",
        line=dict(color="#2177b0", width=2),
        name=column,
        hovertemplate="latency=%{x:.2f} ms<br>CDF=%{y:.3f}<extra></extra>",
    ))

    # Annotate percentiles.
    for pct in _PERCENTILE_TICKS:
        v = float(np.percentile(ms, pct))
        fig.add_vline(x=v, line=dict(color="grey", width=1, dash="dot"))
        fig.add_annotation(
            x=v, y=pct / 100,
            text=f"p{pct}={v:.1f}",
            showarrow=False, xanchor="left", yanchor="bottom",
            font=dict(size=10, color="#555"),
        )

    fig.update_layout(
        title=title or column,
        xaxis_title=x_label,
        yaxis_title="CDF",
        yaxis_range=[0, 1.02],
        template="plotly_white",
        margin=dict(l=40, r=20, t=40, b=40),
        height=360,
    )
    return fig


def plot_histogram(
    df: pd.DataFrame,
    column: str,
    *,
    bins: int = 30,
    title: Optional[str] = None,
) -> go.Figure:
    """Distribution of ``df[column]`` in milliseconds."""
    if df.empty or column not in df.columns:
        return _empty_figure(f"{title or column}: no data")

    ms = df[column].to_numpy() / 1e6
    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=ms, nbinsx=bins,
        marker=dict(color="#2177b0", line=dict(color="white", width=0.5)),
        hovertemplate="bin=%{x:.1f} ms<br>count=%{y}<extra></extra>",
    ))
    fig.update_layout(
        title=title or column,
        xaxis_title="latency (ms)",
        yaxis_title="requests",
        template="plotly_white",
        margin=dict(l=40, r=20, t=40, b=40),
        height=320,
        bargap=0.02,
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
        height=300,
    )
    return fig
