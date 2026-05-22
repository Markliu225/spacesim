#!/usr/bin/env python3
"""
Topology map showing the full request lifecycle on a single frame:

  - 60 satellites at SGP-4 lat/lon at t = 1.5 s, plane-coloured.
    Compute SATs (★ + bold C<id>), transit SATs (●), GS (■ + city name).
  - Forward paths (REQUEST: GS → compute) overlaid as solid coloured lines.
  - Return paths (RESPONSE: compute → GS) overlaid as dashed coloured
    lines on the same colour as the forward leg.

Annotates each compute SAT with its TTFT statistics
(mean / max in ms) and the number of completed requests gathered there.
"""

from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import cartopy.io.shapereader as shpreader

HERE = os.path.abspath(os.path.dirname(__file__))
PHASE_A_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "..", "phase_a"))
HYPATIA_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
SATGENPY = os.path.join(HYPATIA_ROOT, "satgenpy")
for p in (PHASE_A_DIR, SATGENPY):
    if p not in sys.path:
        sys.path.insert(0, p)

from satgen.tles import read_tles  # noqa: E402
from satgen.ground_stations import read_ground_stations_extended  # noqa: E402
from astropy import units as u  # noqa: E402
import analyze_phase_a as ap  # noqa: E402


NETWORK = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"
STATE_DIR = os.path.join(HERE, "gen_data", NETWORK)
DYN_DIR = os.path.join(STATE_DIR, "dynamic_state_100ms_for_5s")
RUN_LOGS = os.path.join(HERE, "run", "logs_ns3")
PLOTS = os.path.join(HERE, "plots")
os.makedirs(PLOTS, exist_ok=True)

GS_NAMES = {60: "Tokyo", 61: "Mumbai", 62: "Shanghai",
            63: "Sao-Paulo", 64: "NY"}
FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]


def sat_subpoint(sat, epoch, t_ns):
    sat.compute(str(epoch + t_ns * u.ns))
    return float(sat.sublat) * 180 / np.pi, float(sat.sublong) * 180 / np.pi


def great_circle(lo1, la1, lo2, la2, n=24):
    dlon = ((lo2 - lo1 + 540) % 360) - 180
    lons = (np.linspace(lo1, lo1 + dlon, n) + 180) % 360 - 180
    lats = np.linspace(la1, la2, n)
    return lons, lats


def load_full_fstate_at(target_t):
    state = {}
    files = sorted(int(f.split("_")[1].split(".")[0])
                   for f in os.listdir(DYN_DIR) if f.startswith("fstate_"))
    for t in files:
        if t > target_t: break
        state.update(ap.read_fstate(os.path.join(DYN_DIR, f"fstate_{t}.txt")))
    return state


def load_csvs():
    g = []; resp = []
    for name in sorted(os.listdir(RUN_LOGS)):
        if name.startswith("llm_gather_node") and name.endswith(".csv"):
            with open(os.path.join(RUN_LOGS, name)) as f:
                for r in csv.DictReader(f):
                    g.append({k: int(v) if v.lstrip("-").isdigit() else v
                              for k, v in r.items()})
        elif name.startswith("llm_response_node") and name.endswith(".csv"):
            with open(os.path.join(RUN_LOGS, name)) as f:
                for r in csv.DictReader(f):
                    resp.append({k: int(v) if v.lstrip("-").isdigit() else v
                                 for k, v in r.items()})
    return g, resp


def load_schedule():
    rows = []
    with open(os.path.join(HERE, "llm_workload_schedule.csv")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            rows.append({"src": int(parts[0]), "dst": int(parts[1]),
                         "lambda": float(parts[2])})
    return rows


def main():
    t_snap_ns = 1_500_000_000
    tles = read_tles(os.path.join(STATE_DIR, "tles.txt"))
    satellites = tles["satellites"]; epoch = tles["epoch"]
    num_sats = len(satellites)
    num_planes = num_sats // 10
    plane_colors = plt.cm.tab10(np.linspace(0, 1, max(num_planes, 3)))[:num_planes]
    ground_stations = read_ground_stations_extended(
        os.path.join(STATE_DIR, "ground_stations.txt"))
    gs_lat = np.array([float(g["latitude_degrees_str"]) for g in ground_stations])
    gs_lon = np.array([float(g["longitude_degrees_str"]) for g in ground_stations])
    gs_csv_names = [g["name"] for g in ground_stations]
    sat_lat = np.zeros(num_sats); sat_lon = np.zeros(num_sats)
    for sid in range(num_sats):
        sat_lat[sid], sat_lon[sid] = sat_subpoint(satellites[sid], epoch, t_snap_ns)

    gather, response = load_csvs()
    schedule = load_schedule()
    compute_set = sorted({s["dst"] for s in schedule})

    # Per-compute aggregations.
    rx_per_sat = defaultdict(int)
    ttft_per_sat = defaultdict(list)
    g_by_req = {(g["src_node_id"], g["compute_sat_id"], g["req_id"]): g
                for g in gather}
    resp_by_req = defaultdict(list)
    for r in response:
        resp_by_req[(r["src_compute_sat_id"], r["req_id"], r["gs_node_id"])].append(r)
    for (sat, req_id, gs), rsps in resp_by_req.items():
        gkey = (gs, sat, req_id)
        if gkey not in g_by_req: continue
        rx_per_sat[sat] += 1
        recv0 = min(r["t_response_recv_ns"] for r in rsps)
        ttft_per_sat[sat].append(recv0 - g_by_req[gkey]["t_emit_ns"])

    fig, ax = plt.subplots(figsize=(15, 8))
    ax.set_facecolor("#d6e3f0")
    ax.set_xlim(-180, 180); ax.set_ylim(-85, 85); ax.set_aspect("equal")
    ax.set_xlabel("longitude (deg)"); ax.set_ylabel("latitude (deg)")
    ax.grid(True, color="#bbbbbb", linestyle=":", linewidth=0.4)
    try:
        lp = shpreader.natural_earth(resolution="110m", category="physical", name="land")
        from matplotlib.patches import Polygon as MplPolygon
        for record in shpreader.Reader(lp).records():
            g = record.geometry
            polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
            for poly in polys:
                xs, ys = poly.exterior.coords.xy
                ax.add_patch(MplPolygon(list(zip(xs, ys)),
                                        facecolor="#f4ede0", edgecolor="none", zorder=1))
    except Exception: pass
    try:
        cp = shpreader.natural_earth(resolution="110m", category="physical", name="coastline")
        for record in shpreader.Reader(cp).records():
            g = record.geometry
            geoms = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
            for line in geoms:
                xs, ys = line.coords.xy
                ax.plot(xs, ys, color="#5a5a5a", linewidth=0.4, zorder=2)
    except Exception: pass

    # transit sats
    for pi in range(num_planes):
        sids = [s for s in range(pi*10, (pi+1)*10) if s not in compute_set]
        if sids:
            ax.scatter(sat_lon[sids], sat_lat[sids], s=22,
                       color=plane_colors[pi], edgecolors="white", linewidth=0.4,
                       zorder=3, alpha=0.85)
    # compute sats + lifecycle annotations
    for sid in compute_set:
        pi = sid // 10
        ax.scatter([sat_lon[sid]], [sat_lat[sid]], s=240, marker="*",
                   color=plane_colors[pi], edgecolors="black",
                   linewidth=1.4, zorder=5)
        ttfts = ttft_per_sat.get(sid, [])
        if ttfts:
            mean_ms = sum(ttfts) / len(ttfts) / 1e6
            max_ms  = max(ttfts) / 1e6
            label = (f"C{sid}\n{rx_per_sat[sid]} reqs\n"
                     f"TTFT μ={mean_ms:.0f}/max={max_ms:.0f}ms")
        else:
            label = f"C{sid}\n(idle)"
        ax.text(sat_lon[sid] + 2.5, sat_lat[sid] + 2.5, label,
                fontsize=8, color="black", weight="bold", zorder=6,
                bbox=dict(boxstyle="round,pad=0.2",
                          facecolor="white", edgecolor="none", alpha=0.85))

    # GS
    ax.scatter(gs_lon, gs_lat, s=140, marker="s", color="#c0392b",
               edgecolors="white", linewidth=0.8, zorder=6)
    for i, name in enumerate(gs_csv_names):
        ax.text(gs_lon[i] + 2.5, gs_lat[i] - 3, name,
                fontsize=9, color="#7b1d12", weight="bold", zorder=7)

    # flow paths (forward solid + return dashed)
    fstate = load_full_fstate_at(t_snap_ns)
    flow_handles = []
    for i, sched in enumerate(schedule):
        src, dst = sched["src"], sched["dst"]
        color = FLOW_COLORS[i % len(FLOW_COLORS)]
        for style, (a_start, a_end) in [("-", (src, dst)), ("--", (dst, src))]:
            try:
                path = ap.trace_path(fstate, a_start, a_end, max_hops=64)
            except Exception:
                continue
            for k in range(len(path) - 1):
                a, b = path[k], path[k+1]
                la, lo_a = ((sat_lat[a], sat_lon[a]) if a < num_sats
                            else (gs_lat[a-num_sats], gs_lon[a-num_sats]))
                lb, lo_b = ((sat_lat[b], sat_lon[b]) if b < num_sats
                            else (gs_lat[b-num_sats], gs_lon[b-num_sats]))
                lons, lats = great_circle(lo_a, la, lo_b, lb, n=24)
                dlons = np.diff(lons)
                starts = [0] + (np.where(np.abs(dlons) > 180)[0] + 1).tolist()
                ends   = (np.where(np.abs(dlons) > 180)[0] + 1).tolist() + [len(lons)]
                for s, e in zip(starts, ends):
                    if e - s >= 2:
                        ax.plot(lons[s:e], lats[s:e], color=color,
                                linewidth=1.8, linestyle=style,
                                zorder=8, alpha=0.92)
        flow_handles.append(plt.Line2D([0], [0], color=color, linewidth=1.8,
            label=f"{GS_NAMES.get(src, src)} → C{dst}"))
    flow_handles.append(plt.Line2D([0], [0], color="gray", linewidth=1.8,
                                   label="— REQUEST (GS → compute)"))
    flow_handles.append(plt.Line2D([0], [0], color="gray", linewidth=1.8,
                                   linestyle="--",
                                   label="-- RESPONSE (compute → GS)"))
    ax.legend(handles=flow_handles, loc="lower right", fontsize=8,
              framealpha=0.93, title="LLM lifecycle flows")
    ax.set_title(
        "Phase C — request → token → packet → compute → response\n"
        "60 sats / 5 GS / 6 compute SATs, t = 1.5 s snapshot; "
        f"59 reqs / {sum(rx_per_sat.values())} completed gather+compute+response",
        fontsize=11,
    )
    fig.tight_layout()
    out = os.path.join(PLOTS, "topology_lifecycle.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
