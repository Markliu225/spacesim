#!/usr/bin/env python3
"""
World-map visualization of the Phase B LLM workload.

Shows the 60-sat constellation (plane-coloured, compute SATs starred)
and the 5 LLM flows. Path widths scale with the *number of packets*
the flow delivered, so visually heavy flows are wider.

Annotates each compute SAT with the count of received packets:

   C22 (238 pkts)

Output: plots/topology_llm.png
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

import cartopy.io.shapereader as shpreader  # only shapefile reader

HERE = os.path.abspath(os.path.dirname(__file__))
PLOTS = os.path.join(HERE, "plots")
os.makedirs(PLOTS, exist_ok=True)

PHASE_A_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "..", "phase_a"))
HYPATIA_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
SATGENPY = os.path.join(HYPATIA_ROOT, "satgenpy")
for p in (PHASE_A_DIR, SATGENPY):
    if p not in sys.path:
        sys.path.insert(0, p)

from satgen.tles import read_tles  # noqa: E402
from satgen.ground_stations import read_ground_stations_extended  # noqa: E402
from astropy import units as u  # noqa: E402

import analyze_phase_a as ap  # for read_fstate / trace_path  # noqa: E402


NETWORK = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"
STATE_DIR = os.path.join(HERE, "gen_data", NETWORK)
DYN_DIR = os.path.join(STATE_DIR, "dynamic_state_100ms_for_5s")
RUN_LOGS = os.path.join(HERE, "run", "logs_ns3")

GS_NAMES = {60: "Tokyo", 61: "Mumbai", 62: "Shanghai",
            63: "Sao-Paulo", 64: "NY"}
FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]


def sat_subpoint(sat, epoch, t_ns: int):
    sat.compute(str(epoch + t_ns * u.ns))
    return float(sat.sublat) * 180.0 / np.pi, float(sat.sublong) * 180.0 / np.pi


def load_full_fstate_at(target_t):
    state = {}
    files = sorted(int(f.split("_")[1].split(".")[0])
                   for f in os.listdir(DYN_DIR) if f.startswith("fstate_"))
    for t in files:
        if t > target_t: break
        state.update(ap.read_fstate(os.path.join(DYN_DIR, f"fstate_{t}.txt")))
    return state


def load_packet_counts():
    """Return {(src_node, dst_node): rx_pkt_count} from all sink CSVs."""
    counts = defaultdict(int)
    for name in sorted(os.listdir(RUN_LOGS)):
        if name.startswith("llm_workload_sink") and name.endswith(".csv"):
            with open(os.path.join(RUN_LOGS, name)) as f:
                for row in csv.DictReader(f):
                    src = int(row["src_node_id"])
                    dst = int(row["recv_node_id"])
                    counts[(src, dst)] += 1
    return counts


def load_schedule():
    rows = []
    with open(os.path.join(HERE, "llm_workload_schedule.csv")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            rows.append({
                "src": int(parts[0]),
                "dst": int(parts[1]),
                "lambda": float(parts[2]),
                "L_mean": float(parts[3]),
                "start_ns": int(parts[9]),
            })
    return rows


def great_circle(lo1, la1, lo2, la2, n=24):
    dlon = ((lo2 - lo1 + 540) % 360) - 180
    lons = (np.linspace(lo1, lo1 + dlon, n) + 180) % 360 - 180
    lats = np.linspace(la1, la2, n)
    return lons, lats


def main():
    # Snapshot at t = 1 s (well inside the workload window).
    t_snap_ns = 1_000_000_000

    tles = read_tles(os.path.join(STATE_DIR, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    num_sats = len(satellites)
    num_planes = num_sats // 10

    ground_stations = read_ground_stations_extended(
        os.path.join(STATE_DIR, "ground_stations.txt"))
    gs_lat = np.array([float(g["latitude_degrees_str"]) for g in ground_stations])
    gs_lon = np.array([float(g["longitude_degrees_str"]) for g in ground_stations])
    gs_csv_names = [g["name"] for g in ground_stations]

    sat_lat = np.zeros(num_sats); sat_lon = np.zeros(num_sats)
    for sid in range(num_sats):
        sat_lat[sid], sat_lon[sid] = sat_subpoint(satellites[sid], epoch, t_snap_ns)

    schedule = load_schedule()
    pkt_counts = load_packet_counts()
    compute_set = sorted({s["dst"] for s in schedule})

    # Figure setup.
    fig, ax = plt.subplots(figsize=(15, 8))
    ax.set_facecolor("#d6e3f0")
    ax.set_xlim(-180, 180)
    ax.set_ylim(-85, 85)
    ax.set_aspect("equal")
    ax.set_xlabel("longitude (deg)")
    ax.set_ylabel("latitude (deg)")
    ax.grid(True, color="#bbbbbb", linestyle=":", linewidth=0.4)

    # Land + coastline.
    try:
        lp = shpreader.natural_earth(resolution="110m",
                                     category="physical", name="land")
        from matplotlib.patches import Polygon as MplPolygon
        for record in shpreader.Reader(lp).records():
            g = record.geometry
            polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
            for poly in polys:
                xs, ys = poly.exterior.coords.xy
                ax.add_patch(MplPolygon(list(zip(xs, ys)),
                                        facecolor="#f4ede0",
                                        edgecolor="none", zorder=1))
    except Exception:
        pass
    try:
        cp = shpreader.natural_earth(resolution="110m",
                                     category="physical", name="coastline")
        for record in shpreader.Reader(cp).records():
            g = record.geometry
            geoms = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
            for line in geoms:
                xs, ys = line.coords.xy
                ax.plot(xs, ys, color="#5a5a5a", linewidth=0.4, zorder=2)
    except Exception:
        pass

    # Plane colours.
    plane_colors = plt.cm.tab10(np.linspace(0, 1, max(num_planes, 3)))[:num_planes]

    # Transit SATs (small dots).
    for plane_idx in range(num_planes):
        sids = [s for s in range(plane_idx * 10, (plane_idx + 1) * 10)
                if s not in compute_set]
        if sids:
            ax.scatter(sat_lon[sids], sat_lat[sids],
                       s=22, color=plane_colors[plane_idx],
                       edgecolors="white", linewidth=0.4,
                       zorder=3, alpha=0.85)

    # Compute SATs (stars + label with rx-pkt count).
    rx_per_compute = defaultdict(int)
    for (s, d), c in pkt_counts.items():
        rx_per_compute[d] += c
    for sid in compute_set:
        plane_idx = sid // 10
        ax.scatter([sat_lon[sid]], [sat_lat[sid]], s=260, marker="*",
                   color=plane_colors[plane_idx],
                   edgecolors="black", linewidth=1.6, zorder=5)
        ax.text(sat_lon[sid] + 2.5, sat_lat[sid] + 2.5,
                f"C{sid}  ({rx_per_compute[sid]} pkts)",
                fontsize=9, color="black", weight="bold", zorder=6,
                bbox=dict(boxstyle="round,pad=0.2",
                          facecolor="white", edgecolor="none", alpha=0.8))

    # GSs.
    ax.scatter(gs_lon, gs_lat, s=140, marker="s", color="#c0392b",
               edgecolors="white", linewidth=0.8, zorder=6)
    for i, name in enumerate(gs_csv_names):
        ax.text(gs_lon[i] + 2.5, gs_lat[i] - 3, name,
                fontsize=9, color="#7b1d12", weight="bold", zorder=7)

    # 5 flow paths (line width ∝ rx pkt count).
    flow_handles = []
    max_pkts = max((pkt_counts.get((s["src"], s["dst"]), 0) for s in schedule),
                   default=1)
    fstate = load_full_fstate_at(t_snap_ns)
    for i, sched in enumerate(schedule):
        src, dst = sched["src"], sched["dst"]
        try:
            path = ap.trace_path(fstate, src, dst, max_hops=64)
        except Exception as e:
            print(f"  flow {src}->{dst}: trace failed: {e}")
            continue
        color = FLOW_COLORS[i % len(FLOW_COLORS)]
        n_pkts = pkt_counts.get((src, dst), 0)
        # Width: 1.5 to 5.0 mapped from packet count.
        lw = 1.5 + 3.5 * (n_pkts / max_pkts) if max_pkts > 0 else 2.0
        for k in range(len(path) - 1):
            a, b = path[k], path[k + 1]
            la, lo_a = (sat_lat[a], sat_lon[a]) if a < num_sats \
                else (gs_lat[a - num_sats], gs_lon[a - num_sats])
            lb, lo_b = (sat_lat[b], sat_lon[b]) if b < num_sats \
                else (gs_lat[b - num_sats], gs_lon[b - num_sats])
            lons, lats = great_circle(lo_a, la, lo_b, lb, n=24)
            dlons = np.diff(lons)
            starts = [0] + (np.where(np.abs(dlons) > 180)[0] + 1).tolist()
            ends = (np.where(np.abs(dlons) > 180)[0] + 1).tolist() + [len(lons)]
            for s_idx, e_idx in zip(starts, ends):
                if e_idx - s_idx >= 2:
                    ax.plot(lons[s_idx:e_idx], lats[s_idx:e_idx],
                            color=color, linewidth=lw,
                            zorder=8, alpha=0.92)

        flow_handles.append(plt.Line2D(
            [0], [0], color=color, linewidth=lw,
            label=f"{GS_NAMES.get(src, src)} → C{dst}  "
                  f"({len(path) - 1} hops, {n_pkts} pkts, λ={sched['lambda']:.0f})",
        ))

    ax.legend(handles=flow_handles, loc="lower right", fontsize=9,
              framealpha=0.93, title="LLM flows  (line width ∝ packets delivered)")

    ax.set_title(
        "Phase B LLM workload — 5 GS → 5 compute SATs at t=1.0s\n"
        "60 sats (6 planes × 10), 6 compute SATs marked with stars, "
        "243 reqs / 536 pkts delivered",
        fontsize=11,
    )

    fig.tight_layout()
    out = os.path.join(PLOTS, "topology_llm.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
