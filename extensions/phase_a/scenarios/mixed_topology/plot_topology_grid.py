#!/usr/bin/env python3
"""
Multi-panel snapshot grid of the mixed-topology scenario.

Renders 6 small world maps at t = 0, 1, 2, 3, 4, 5 seconds, each showing:

  - all 60 satellites at their SGP-4-propagated lat/lon, colour-coded by
    orbital plane (6 distinct colours)
  - the 6 compute SATs marked with a black-edged star + plane colour fill
  - the 5 ground stations as red squares (city names on the first panel only)
  - all flows that are *currently in flight* at that t, traced offline
    through the augmented fstate

A flow is "active" at time t iff its start_ns <= t <= end_ns. Use the
panels to see how the constellation moves (~7.6 km/s -> ~38 km per panel
on the ground track) and how paths shift across plane hand-overs.

Companion to:
  - plot_topology_paths.py (single snapshot at t=200 ms, the headline image)
  - plot_topology_anim.py (50-frame GIF animation)
"""

from __future__ import annotations

import csv
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import cartopy.io.shapereader as shpreader  # only the shapefile reader, not GeoAxes

_HERE = os.path.abspath(os.path.dirname(__file__))
_PHASE_A_DIR = os.path.abspath(os.path.join(_HERE, "..", ".."))
_HYPATIA_ROOT = os.path.abspath(os.path.join(_PHASE_A_DIR, "..", ".."))
_SATGENPY = os.path.join(_HYPATIA_ROOT, "satgenpy")
for p in (_PHASE_A_DIR, _SATGENPY):
    if p not in sys.path:
        sys.path.insert(0, p)

from satgen.tles import read_tles  # noqa: E402
from satgen.isls import read_isls  # noqa: E402
from satgen.ground_stations import read_ground_stations_extended  # noqa: E402
from astropy import units as u  # noqa: E402

import analyze_phase_a as ap  # noqa: E402


# --- Constants -------------------------------------------------------------

NETWORK = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"
STATE_DIR = os.path.join(_HERE, "gen_data", NETWORK)
DYN_DIR = os.path.join(STATE_DIR, "dynamic_state_100ms_for_5s")
RUN_DIR = os.path.join(_HERE, "run")
PLOTS_DIR = os.path.join(_HERE, "plots")
os.makedirs(PLOTS_DIR, exist_ok=True)

# Snapshot times chosen to capture the interesting window:
# - 0.2 s: flow 0 alive (started 0.1), flow 1 just started (0.2)
# - 0.5 s: flows 0/1/2/3 all alive, flow 4 has not yet started
# - 1.0 s: peak activity -- flows 1/2/3/4 (flow 0 ended just before)
# - 1.3 s: flow 3 (834 ms duration) just ended; 1/2/4 still in flight
# - 1.7 s: flow 4 winding down (ended 1.935 s); 2 still has ~few hundred ms
# - 2.5 s: everything done; pure constellation drift relative to t=0
SNAPSHOT_TIMES_S = [0.2, 0.5, 1.0, 1.3, 1.7, 2.5]

FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]


# --- Helpers ---------------------------------------------------------------


def sat_subpoint(sat, epoch, t_ns: int):
    """Use pyephem (via the satgen TLE objects) to return (lat_deg, lon_deg)."""
    time = epoch + t_ns * u.ns
    sat.compute(str(time))
    return float(sat.sublat) * 180.0 / np.pi, float(sat.sublong) * 180.0 / np.pi


def load_fstate_at(t_ns: int):
    """Accumulate delta-encoded fstate up to and including timestep t_ns."""
    state: dict = {}
    files = sorted(int(f.split("_")[1].split(".")[0])
                   for f in os.listdir(DYN_DIR) if f.startswith("fstate_"))
    for t in files:
        if t > t_ns:
            break
        with open(os.path.join(DYN_DIR, f"fstate_{t}.txt")) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) != 5:
                    continue
                state[(int(parts[0]), int(parts[1]))] = (
                    int(parts[2]), int(parts[3]), int(parts[4]))
    return state


def load_flows():
    """Return list of {id, from, to, size, start_ns, end_ns, completed, metadata}."""
    flows = []
    sched_path = os.path.join(_HERE, "schedule.csv")
    summary_path = os.path.join(RUN_DIR, "logs_ns3", "tcp_flows.csv")

    summary = {}
    if os.path.exists(summary_path):
        with open(summary_path) as fh:
            for row in csv.reader(fh):
                if not row or row[0].startswith("#"):
                    continue
                summary[int(row[0])] = {
                    "end_ns": int(row[5]),
                    "completed": row[8],
                }

    with open(sched_path) as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#"):
                continue
            fid = int(row[0])
            s = summary.get(fid, {})
            flows.append({
                "id": fid,
                "from": int(row[1]),
                "to": int(row[2]),
                "size": int(row[3]),
                "start_ns": int(row[4]),
                "end_ns": s.get("end_ns", -1),
                "completed": s.get("completed", "?"),
                "metadata": row[6] if len(row) > 6 else "",
            })
    return flows


def great_circle(lo1, la1, lo2, la2, n=20):
    """Simple linear-in-lat/lon interpolation (good enough for visualisation)."""
    dlon = ((lo2 - lo1 + 540) % 360) - 180  # shortest path around the globe
    lons = (np.linspace(lo1, lo1 + dlon, n) + 180) % 360 - 180
    lats = np.linspace(la1, la2, n)
    return lons, lats


def draw_panel(ax, t_s, satellites, epoch, ground_stations, flows, compute_set,
               num_sats, num_planes, plane_colors, coastline_paths,
               show_gs_names=False):
    t_ns = int(round(t_s * 1e9))
    ax.set_facecolor("#d6e3f0")
    ax.set_xlim(-180, 180)
    ax.set_ylim(-85, 85)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])

    # Coastlines (cached as a list of (xs, ys) ndarrays at module load).
    for xs, ys in coastline_paths:
        ax.plot(xs, ys, color="#999999", linewidth=0.3, zorder=2)

    # Satellite positions at this t.
    sat_lat = np.zeros(num_sats)
    sat_lon = np.zeros(num_sats)
    for sid in range(num_sats):
        sat_lat[sid], sat_lon[sid] = sat_subpoint(satellites[sid], epoch, t_ns)

    # Transit SATs by plane.
    for plane_idx in range(num_planes):
        sids = [s for s in range(plane_idx * 10, (plane_idx + 1) * 10)
                if s not in compute_set]
        if sids:
            ax.scatter(sat_lon[sids], sat_lat[sids],
                       s=8, color=plane_colors[plane_idx],
                       edgecolors="none", zorder=3, alpha=0.85)

    # Compute SATs as small stars (skip text labels in grid panels).
    for sid in sorted(compute_set):
        plane_idx = sid // 10
        ax.scatter([sat_lon[sid]], [sat_lat[sid]],
                   s=70, marker="*", color=plane_colors[plane_idx],
                   edgecolors="black", linewidth=0.8, zorder=5)

    # GS as small red squares.
    gs_lat = np.array([float(g["latitude_degrees_str"]) for g in ground_stations])
    gs_lon = np.array([float(g["longitude_degrees_str"]) for g in ground_stations])
    gs_names = [g["name"] for g in ground_stations]
    ax.scatter(gs_lon, gs_lat, s=40, marker="s", color="#c0392b",
               edgecolors="white", linewidth=0.5, zorder=6)
    if show_gs_names:
        for i, name in enumerate(gs_names):
            ax.text(gs_lon[i] + 2, gs_lat[i] - 3, name,
                    fontsize=6, color="#7b1d12", weight="bold", zorder=7)

    # Active flows: a flow is active at t iff start_ns <= t_ns and (end_ns
    # < 0 (= unknown) or t_ns <= end_ns).
    active_count = 0
    if t_ns > 0:
        fstate = load_fstate_at(t_ns)
        for fl in flows:
            if fl["start_ns"] > t_ns:
                continue
            if fl["end_ns"] > 0 and t_ns > fl["end_ns"]:
                continue
            try:
                path = ap.trace_path(fstate, fl["from"], fl["to"], max_hops=64)
            except Exception:
                continue
            active_count += 1
            color = FLOW_COLORS[fl["id"] % len(FLOW_COLORS)]
            for i in range(len(path) - 1):
                a, b = path[i], path[i + 1]
                la, lo_a = (sat_lat[a], sat_lon[a]) if a < num_sats \
                    else (gs_lat[a - num_sats], gs_lon[a - num_sats])
                lb, lo_b = (sat_lat[b], sat_lon[b]) if b < num_sats \
                    else (gs_lat[b - num_sats], gs_lon[b - num_sats])
                lons, lats = great_circle(lo_a, la, lo_b, lb, n=20)
                dlons = np.diff(lons)
                starts = [0] + (np.where(np.abs(dlons) > 180)[0] + 1).tolist()
                ends = (np.where(np.abs(dlons) > 180)[0] + 1).tolist() + [len(lons)]
                for s, e in zip(starts, ends):
                    if e - s >= 2:
                        ax.plot(lons[s:e], lats[s:e],
                                color=color, linewidth=1.6, zorder=8, alpha=0.95)

    ax.set_title(f"t = {t_s:.1f} s  ({active_count} flows in flight)",
                 fontsize=10)


def main() -> int:
    print(f"loading scenario from {STATE_DIR}")
    tles = read_tles(os.path.join(STATE_DIR, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    num_sats = len(satellites)
    num_planes = num_sats // 10
    plane_colors = plt.cm.tab10(np.linspace(0, 1, max(num_planes, 3)))[:num_planes]
    ground_stations = read_ground_stations_extended(
        os.path.join(STATE_DIR, "ground_stations.txt"))

    # Roles
    compute_set = set()
    with open(os.path.join(_HERE, "satellite_roles.txt")) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sid, role = line.split(",")
            if role.strip() == "C":
                compute_set.add(int(sid))

    flows = load_flows()

    # Precompute coastline paths (avoid re-reading the shapefile in each panel).
    print("loading coastline shapefile")
    coastline_paths = []
    try:
        path = shpreader.natural_earth(resolution="110m",
                                       category="physical",
                                       name="coastline")
        reader = shpreader.Reader(path)
        for record in reader.records():
            g = record.geometry
            geoms = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
            for line in geoms:
                xs, ys = line.coords.xy
                coastline_paths.append((np.array(xs), np.array(ys)))
    except Exception as e:
        print(f"  coastline load failed: {e}")

    # Render 2x3 panel grid.
    fig, axes = plt.subplots(2, 3, figsize=(15, 7.5))
    for ax_idx, t_s in enumerate(SNAPSHOT_TIMES_S):
        ax = axes.flat[ax_idx]
        print(f"  panel t = {t_s:.1f} s")
        draw_panel(ax, t_s, satellites, epoch, ground_stations, flows,
                   compute_set, num_sats, num_planes, plane_colors,
                   coastline_paths,
                   show_gs_names=(ax_idx == 0))

    # Outer figure title + legend.
    compute_list = ", ".join(f"C{s}" for s in sorted(compute_set))
    fig.suptitle(
        f"Topology over 5 s — 60 sats (6 planes × 10), 6 compute = {compute_list}, 5 GS\n"
        "constellation drifts ~38 km on the ground per panel; active flow paths overlaid",
        fontsize=11,
    )

    # Bottom-of-figure legend for flow colours.
    legend_handles = []
    for fl in flows:
        color = FLOW_COLORS[fl["id"] % len(FLOW_COLORS)]
        legend_handles.append(plt.Line2D(
            [0], [0], color=color, linewidth=1.8,
            label=f"flow {fl['id']}: {fl['metadata']}",
        ))
    if legend_handles:
        fig.legend(handles=legend_handles, loc="lower center",
                   ncol=len(legend_handles), fontsize=8,
                   bbox_to_anchor=(0.5, -0.02))

    fig.tight_layout(rect=[0, 0.04, 1, 0.94])
    out_pdf = os.path.join(PLOTS_DIR, "topology_grid.png")
    fig.savefig(out_pdf, dpi=150, bbox_inches="tight")
    print(f"wrote {out_pdf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())