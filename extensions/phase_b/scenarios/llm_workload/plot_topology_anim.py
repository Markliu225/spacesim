#!/usr/bin/env python3
"""
Phase B LLM workload — 50-frame animated GIF (real-time, 10 fps).

Each frame is one fstate timestep (100 ms apart):

  - Constellation drift: all 60 sats redrawn at their SGP-4-propagated
    lat/lon at that t.
  - Satellite types are explicitly distinguished:
      ● small dot, plane colour   = transit SAT (T)
      ★ big star,  black edge,
        "C<id>" label              = compute SAT (C)
  - Ground stations are red squares with city names.
  - 5 LLM flow paths are overlaid, sliced through fstate at the current
    time, so packet paths actually change as GSL handovers fire.
  - Two per-frame counters in the title:
      - cumulative req-complete events on each compute SAT (so we
        watch the bar fill as the run progresses)
      - per-frame "active request" count = how many requests are
        currently "in flight" (some packets emitted, not all received)
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
from matplotlib.animation import FuncAnimation, PillowWriter
from matplotlib.patches import Polygon as MplPolygon

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

PLOTS = os.path.join(HERE, "plots")
os.makedirs(PLOTS, exist_ok=True)

NETWORK = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"
STATE_DIR = os.path.join(HERE, "gen_data", NETWORK)
DYN_DIR = os.path.join(STATE_DIR, "dynamic_state_100ms_for_5s")
RUN_LOGS = os.path.join(HERE, "run", "logs_ns3")

GS_NAMES = {60: "Tokyo", 61: "Mumbai", 62: "Shanghai",
            63: "Sao-Paulo", 64: "NY"}
FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
FPS = 10


def sat_subpoint(sat, epoch, t_ns):
    sat.compute(str(epoch + t_ns * u.ns))
    return float(sat.sublat) * 180.0 / np.pi, float(sat.sublong) * 180.0 / np.pi


def great_circle(lo1, la1, lo2, la2, n=22):
    dlon = ((lo2 - lo1 + 540) % 360) - 180
    lons = (np.linspace(lo1, lo1 + dlon, n) + 180) % 360 - 180
    lats = np.linspace(la1, la2, n)
    return lons, lats


def load_fstate_cache():
    """Pre-load all delta fstate files keyed by timestep."""
    cache = []
    files = sorted(int(f.split("_")[1].split(".")[0])
                   for f in os.listdir(DYN_DIR) if f.startswith("fstate_"))
    for t in files:
        delta = ap.read_fstate(os.path.join(DYN_DIR, f"fstate_{t}.txt"))
        cache.append((t, delta))
    return cache


def load_schedule():
    out = []
    with open(os.path.join(HERE, "llm_workload_schedule.csv")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            out.append({"src": int(parts[0]),
                        "dst": int(parts[1]),
                        "lambda": float(parts[2])})
    return out


def load_packet_events():
    """For each packet we know its (req_id, src, dst, t_emit_ns, recv_time_ns,
    total_pkts). Build req-level events: emit_time and complete_time per req."""
    pkts = []
    for name in sorted(os.listdir(RUN_LOGS)):
        if name.startswith("llm_workload_sink") and name.endswith(".csv"):
            with open(os.path.join(RUN_LOGS, name)) as f:
                for row in csv.DictReader(f):
                    pkts.append({k: int(v) for k, v in row.items()})

    # req-level
    grp = defaultdict(list)
    for p in pkts:
        grp[(p["src_node_id"], p["recv_node_id"], p["req_id"])].append(p)
    events = []
    for (src, dst, req_id), ps in grp.items():
        t_emit = ps[0]["t_emit_ns"]
        t_complete = max(p["recv_time_ns"] for p in ps)
        events.append({"src": src, "dst": dst, "req_id": req_id,
                       "t_emit": t_emit, "t_complete": t_complete,
                       "total_pkts": ps[0]["total_pkts"],
                       "received": len(ps)})
    return pkts, events


def main():
    print("loading scenario")
    tles = read_tles(os.path.join(STATE_DIR, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    num_sats = len(satellites)
    num_planes = num_sats // 10
    plane_colors = plt.cm.tab10(np.linspace(0, 1, max(num_planes, 3)))[:num_planes]
    ground_stations = read_ground_stations_extended(
        os.path.join(STATE_DIR, "ground_stations.txt"))
    gs_lat = np.array([float(g["latitude_degrees_str"]) for g in ground_stations])
    gs_lon = np.array([float(g["longitude_degrees_str"]) for g in ground_stations])
    gs_csv_names = [g["name"] for g in ground_stations]

    compute_set = {2, 12, 22, 32, 42, 52}  # known from satellite_roles.txt

    schedule = load_schedule()
    pkts, events = load_packet_events()
    print(f"  {len(pkts)} packets, {len(events)} request events")

    fstate_cache = load_fstate_cache()
    fstate_times = [t for t, _ in fstate_cache]
    print(f"  {len(fstate_times)} fstate timesteps")

    # Coastlines + land.
    print("loading coastlines")
    coast = []
    land = []
    try:
        cp = shpreader.natural_earth(resolution="110m",
                                     category="physical", name="coastline")
        for record in shpreader.Reader(cp).records():
            g = record.geometry
            geoms = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
            for line in geoms:
                xs, ys = line.coords.xy
                coast.append((np.array(xs), np.array(ys)))
    except Exception as e:
        print(f"  coastline failed: {e}")
    try:
        lp = shpreader.natural_earth(resolution="110m",
                                     category="physical", name="land")
        for record in shpreader.Reader(lp).records():
            g = record.geometry
            polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
            for poly in polys:
                xs, ys = poly.exterior.coords.xy
                land.append(list(zip(xs, ys)))
    except Exception as e:
        print(f"  land failed: {e}")

    fig, ax = plt.subplots(figsize=(14, 7))
    ax.set_facecolor("#d6e3f0")
    ax.set_xlim(-180, 180); ax.set_ylim(-85, 85)
    ax.set_aspect("equal")
    ax.set_xlabel("longitude (deg)"); ax.set_ylabel("latitude (deg)")
    ax.grid(True, color="#bbbbbb", linestyle=":", linewidth=0.4)
    for verts in land:
        ax.add_patch(MplPolygon(verts, facecolor="#f4ede0", edgecolor="none", zorder=1))
    for xs, ys in coast:
        ax.plot(xs, ys, color="#5a5a5a", linewidth=0.35, zorder=2)

    # Static GS layer.
    ax.scatter(gs_lon, gs_lat, s=110, marker="s", color="#c0392b",
               edgecolors="white", linewidth=0.6, zorder=6)
    for i, name in enumerate(gs_csv_names):
        ax.text(gs_lon[i] + 2, gs_lat[i] - 3, name,
                fontsize=8, color="#7b1d12", weight="bold", zorder=7)

    # Per-flow path / sat / compute artists are remade each frame.
    state = {"sats": [], "compute": [], "compute_labels": [],
             "paths": [], "compute_counters": [], "title": None}

    state["title"] = ax.text(
        0, 92, "", ha="center", va="bottom", fontsize=11, weight="bold",
        transform=ax.transData)

    # Build a top-level legend explaining sat-type encoding once.
    legend_handles = [
        plt.Line2D([0], [0], marker="*", color="none",
                   markerfacecolor="#bbbbbb", markeredgecolor="black",
                   markeredgewidth=1.0, markersize=14,
                   label="compute SAT (type=C)"),
        plt.Line2D([0], [0], marker="o", color="none",
                   markerfacecolor="#bbbbbb", markeredgecolor="white",
                   markersize=7, label="transit SAT (type=T)"),
        plt.Line2D([0], [0], marker="s", color="none",
                   markerfacecolor="#c0392b", markeredgecolor="white",
                   markersize=9, label="ground station"),
    ] + [
        plt.Line2D([0], [0], color=FLOW_COLORS[i], linewidth=2.0,
                   label=f"{GS_NAMES.get(sched['src'], sched['src'])} → C{sched['dst']}")
        for i, sched in enumerate(schedule)
    ]
    ax.legend(handles=legend_handles, loc="lower left", fontsize=7,
              framealpha=0.9, title="node type / LLM flow", ncol=2)

    # State threaded across frames: cumulative req-complete counter per
    # dst compute SAT. Filled in by frame N from events with
    # t_complete <= t_frame.
    rx_completed_by_dst = defaultdict(int)
    sorted_events = sorted(events, key=lambda e: e["t_complete"])
    ev_idx = [0]  # 'pointer' into sorted_events; mutable via list trick

    def fstate_at(t_ns):
        s = {}
        for tt, delta in fstate_cache:
            if tt > t_ns: break
            s.update(delta)
        return s

    def draw_frame(frame_idx):
        t_ns = fstate_times[frame_idx]
        t_s = t_ns / 1e9

        for grp in ("sats", "compute", "compute_labels",
                    "paths", "compute_counters"):
            for a in state[grp]:
                a.remove()
            state[grp] = []

        # Propagate satellites.
        sat_lat = np.zeros(num_sats); sat_lon = np.zeros(num_sats)
        for sid in range(num_sats):
            sat_lat[sid], sat_lon[sid] = sat_subpoint(satellites[sid], epoch, t_ns)

        # Transit SATs (small dots) coloured by plane.
        for plane_idx in range(num_planes):
            sids = [s for s in range(plane_idx * 10, (plane_idx + 1) * 10)
                    if s not in compute_set]
            if sids:
                sc = ax.scatter(sat_lon[sids], sat_lat[sids],
                                s=18, color=plane_colors[plane_idx],
                                edgecolors="none", zorder=3, alpha=0.85)
                state["sats"].append(sc)

        # Compute SATs (stars + labels).
        for sid in sorted(compute_set):
            plane_idx = sid // 10
            sc = ax.scatter([sat_lon[sid]], [sat_lat[sid]],
                            s=180, marker="*", color=plane_colors[plane_idx],
                            edgecolors="black", linewidth=1.2, zorder=5)
            state["compute"].append(sc)
            t = ax.text(sat_lon[sid] + 2.2, sat_lat[sid] + 2.2,
                        f"C{sid}", fontsize=8, color="black",
                        weight="bold", zorder=6,
                        bbox=dict(boxstyle="round,pad=0.12",
                                  facecolor="white", edgecolor="none", alpha=0.7))
            state["compute_labels"].append(t)

        # Active flows: trace path through fstate at this time.
        fstate = fstate_at(t_ns)
        active_reqs = 0
        for i, sched in enumerate(schedule):
            src, dst = sched["src"], sched["dst"]
            try:
                path = ap.trace_path(fstate, src, dst, max_hops=64)
            except Exception:
                continue
            color = FLOW_COLORS[i % len(FLOW_COLORS)]
            for k in range(len(path) - 1):
                a, b = path[k], path[k + 1]
                la, lo_a = (sat_lat[a], sat_lon[a]) if a < num_sats \
                    else (gs_lat[a - num_sats], gs_lon[a - num_sats])
                lb, lo_b = (sat_lat[b], sat_lon[b]) if b < num_sats \
                    else (gs_lat[b - num_sats], gs_lon[b - num_sats])
                lons, lats = great_circle(lo_a, la, lo_b, lb, n=20)
                dlons = np.diff(lons)
                starts = [0] + (np.where(np.abs(dlons) > 180)[0] + 1).tolist()
                ends   = (np.where(np.abs(dlons) > 180)[0] + 1).tolist() + [len(lons)]
                for s, e in zip(starts, ends):
                    if e - s >= 2:
                        line, = ax.plot(lons[s:e], lats[s:e],
                                        color=color, linewidth=1.8,
                                        zorder=8, alpha=0.92)
                        state["paths"].append(line)

        # Advance the req-complete cursor up to t_ns.
        while ev_idx[0] < len(sorted_events) and \
              sorted_events[ev_idx[0]]["t_complete"] <= t_ns:
            ev = sorted_events[ev_idx[0]]
            rx_completed_by_dst[ev["dst"]] += 1
            ev_idx[0] += 1

        # Count active requests (emitted but not yet completed) at this t.
        for ev in events:
            if ev["t_emit"] <= t_ns < ev["t_complete"]:
                active_reqs += 1

        # Compute SAT counter labels (cumulative req complete).
        for sid in sorted(compute_set):
            n_done = rx_completed_by_dst[sid]
            if n_done > 0:
                t_lab = ax.text(sat_lon[sid] - 2.5, sat_lat[sid] - 5.0,
                                f"{n_done} reqs",
                                fontsize=7, color="#234e0e", weight="bold",
                                ha="right", zorder=7,
                                bbox=dict(boxstyle="round,pad=0.1",
                                          facecolor="#ecffd8",
                                          edgecolor="#90c060", linewidth=0.4))
                state["compute_counters"].append(t_lab)

        total_done = sum(rx_completed_by_dst.values())
        state["title"].set_text(
            f"t = {t_s:4.2f} s   |   {active_reqs} requests in flight   |   "
            f"{total_done} requests gathered (cumulative)"
        )

        return (state["title"], *state["sats"], *state["compute"],
                *state["compute_labels"], *state["paths"],
                *state["compute_counters"])

    print(f"rendering {len(fstate_times)} frames at {FPS} fps")
    anim = FuncAnimation(fig, draw_frame, frames=len(fstate_times),
                         interval=1000 // FPS, blit=False, repeat=False)
    fig.suptitle(
        "Phase B LLM workload — request → token → packet flowing GS → compute SAT\n"
        "60 sats (6 planes × 10), 6 compute SATs (C2/C12/C22/C32/C42/C52), "
        "5 GS, 5 concurrent flows over 5 s",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.92])
    out = os.path.join(PLOTS, "topology_anim.gif")
    writer = PillowWriter(fps=FPS)
    anim.save(out, writer=writer, dpi=100)
    print(f"wrote {out} ({os.path.getsize(out)/1024/1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())