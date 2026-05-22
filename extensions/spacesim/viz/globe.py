"""
3D Earth + constellation snapshot for the dashboard's globe tab.

Renders one Plotly figure showing:

  - A semi-transparent unit-radius Earth surface (light blue).
  - Satellites as Scatter3d points coloured by role (compute = orange,
    transit = blue).
  - Ground stations as green Scatter3d points.
  - Optionally, +Grid ISL edges as faint grey line segments.

Everything is in kilometres. Coordinates are ECEF; we use the simple
spherical-Earth approximation (R_E = 6371 km) -- accurate enough for a
visualisation since the user reads positions, not metrology.

For constellations larger than ``MAX_RENDERED_SATS`` (default 2000),
the function downsamples the transit-sat set to keep the plot
responsive. The compute set is always rendered in full.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
import plotly.graph_objects as go

R_E_KM = 6371.0
MAX_RENDERED_SATS = 2000


def latlon_to_ecef_km(
    lat_deg: float, lon_deg: float, alt_km: float = 0.0
) -> Tuple[float, float, float]:
    """Spherical Earth ECEF. Good enough for a dashboard globe."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    r = R_E_KM + alt_km
    return (r * math.cos(lat) * math.cos(lon),
            r * math.cos(lat) * math.sin(lon),
            r * math.sin(lat))


def make_earth_surface(n: int = 36) -> go.Surface:
    """Half-transparent Earth sphere; n*n vertices."""
    u = np.linspace(0, 2 * np.pi, n)
    v = np.linspace(0, np.pi, n)
    x = R_E_KM * np.outer(np.cos(u), np.sin(v))
    y = R_E_KM * np.outer(np.sin(u), np.sin(v))
    z = R_E_KM * np.outer(np.ones_like(u), np.cos(v))
    return go.Surface(
        x=x, y=y, z=z,
        colorscale=[[0, "#9bc6e6"], [1, "#9bc6e6"]],
        opacity=0.45,
        showscale=False,
        hoverinfo="skip",
        name="earth",
    )


def make_satellites_trace(
    ecef_km: np.ndarray, role: Sequence[str], *, sat_ids: Optional[Sequence[int]] = None
) -> List[go.Scatter3d]:
    """One Scatter3d per role colour. role[i] in {"C", "T"}."""
    role = np.asarray(role)
    ids = np.asarray(sat_ids) if sat_ids is not None else np.arange(len(role))
    traces: List[go.Scatter3d] = []
    for label, colour, sym in (("C", "#f08c00", "diamond"), ("T", "#2177b0", "circle")):
        mask = role == label
        if not np.any(mask):
            continue
        pts = ecef_km[mask]
        traces.append(go.Scatter3d(
            x=pts[:, 0], y=pts[:, 1], z=pts[:, 2],
            mode="markers",
            marker=dict(size=3 if label == "T" else 5, color=colour, symbol=sym),
            name="compute SAT" if label == "C" else "transit SAT",
            customdata=ids[mask],
            hovertemplate=(
                ("compute SAT" if label == "C" else "transit SAT")
                + " #%{customdata}<br>x=%{x:.0f} km<br>y=%{y:.0f} km<br>z=%{z:.0f} km<extra></extra>"
            ),
        ))
    return traces


def make_ground_stations_trace(
    gs_records: Sequence[Tuple[int, str, float, float]],
) -> go.Scatter3d:
    """Ground stations as bright green markers.

    Each record: ``(gid, name, lat_deg, lon_deg)``.
    """
    if not gs_records:
        return go.Scatter3d(x=[], y=[], z=[], mode="markers", name="ground stations")
    xs, ys, zs, names = [], [], [], []
    for gid, name, lat, lon in gs_records:
        x, y, z = latlon_to_ecef_km(lat, lon, 0.0)
        xs.append(x); ys.append(y); zs.append(z)
        names.append(f"GS-{gid} {name} ({lat:.2f}, {lon:.2f})")
    return go.Scatter3d(
        x=xs, y=ys, z=zs,
        mode="markers+text",
        marker=dict(size=5, color="#2f9e37", symbol="diamond"),
        text=[n.split(" ")[1] for n in names],
        textposition="top center",
        textfont=dict(size=10, color="#2f9e37"),
        name="ground stations",
        hovertext=names,
        hoverinfo="text",
    )


def make_isl_lines_trace(
    ecef_km: np.ndarray, isls: Iterable[Tuple[int, int]],
    *, max_edges: int = 1000,
) -> go.Scatter3d:
    """Faint grey segments between sat positions for the given ISL list."""
    xs: List[float] = []
    ys: List[float] = []
    zs: List[float] = []
    count = 0
    for a, b in isls:
        if a >= len(ecef_km) or b >= len(ecef_km):
            continue
        xs.extend([ecef_km[a, 0], ecef_km[b, 0], None])
        ys.extend([ecef_km[a, 1], ecef_km[b, 1], None])
        zs.extend([ecef_km[a, 2], ecef_km[b, 2], None])
        count += 1
        if count >= max_edges:
            break
    return go.Scatter3d(
        x=xs, y=ys, z=zs,
        mode="lines",
        line=dict(color="rgba(180,180,180,0.35)", width=1),
        name=f"ISLs (showing {count})",
        hoverinfo="skip",
    )


def make_globe_figure(
    sat_ecef_km: np.ndarray,
    roles: Sequence[str],
    gs_records: Sequence[Tuple[int, str, float, float]],
    *,
    isls: Optional[Sequence[Tuple[int, int]]] = None,
    title: Optional[str] = None,
    downsample_transit: bool = True,
) -> go.Figure:
    """Compose all traces into one figure with a sensible camera."""
    sat_ecef_km = np.asarray(sat_ecef_km, dtype=np.float64)
    if sat_ecef_km.ndim != 2 or sat_ecef_km.shape[1] != 3:
        raise ValueError(f"sat_ecef_km must be (N, 3); got {sat_ecef_km.shape}")
    if len(roles) != len(sat_ecef_km):
        raise ValueError(
            f"roles length {len(roles)} != sats {len(sat_ecef_km)}")

    plot_ecef = sat_ecef_km
    plot_roles = list(roles)
    plot_ids: Optional[Sequence[int]] = None
    if downsample_transit and len(plot_ecef) > MAX_RENDERED_SATS:
        compute_idx = [i for i, r in enumerate(plot_roles) if r == "C"]
        transit_idx = [i for i, r in enumerate(plot_roles) if r != "C"]
        keep_n = max(0, MAX_RENDERED_SATS - len(compute_idx))
        # Even-stride downsample for visual coverage.
        if keep_n < len(transit_idx) and keep_n > 0:
            step = len(transit_idx) / keep_n
            kept_transit = [transit_idx[int(i * step)] for i in range(keep_n)]
        else:
            kept_transit = transit_idx
        keep = sorted(compute_idx + kept_transit)
        plot_ecef = sat_ecef_km[keep]
        plot_roles = [roles[i] for i in keep]
        plot_ids = list(keep)

    traces: List[go.BaseTraceType] = [make_earth_surface()]
    traces.extend(make_satellites_trace(plot_ecef, plot_roles, sat_ids=plot_ids))
    traces.append(make_ground_stations_trace(gs_records))
    if isls:
        traces.append(make_isl_lines_trace(plot_ecef, isls))

    fig = go.Figure(data=traces)
    fig.update_layout(
        title=title or "Constellation snapshot",
        scene=dict(
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            zaxis=dict(visible=False),
            aspectmode="data",
            camera=dict(eye=dict(x=1.4, y=1.4, z=1.0)),
            bgcolor="#fafafa",
        ),
        margin=dict(l=0, r=0, t=40, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=0.0, x=0.0),
        height=560,
    )
    return fig
