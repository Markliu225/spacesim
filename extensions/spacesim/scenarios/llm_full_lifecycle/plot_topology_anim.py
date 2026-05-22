#!/usr/bin/env python3
"""
Phase C topology animation — 49-frame real-time GIF.

Each frame is one fstate timestep (100 ms). Shows:
  - Constellation drift (60 sats, plane-coloured)
  - Compute SAT (★) and transit SAT (●) types clearly distinguished
  - 5 GS as red squares with city names
  - For each compute SAT, a *live counter* of (gather completed,
    compute completed, responses returned) up to that frame.
  - Active request lifecycle phases summarised in the title:
      "t = X.XX s | E emitted | G gathered | C computed | R returned"
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


NETWORK = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"
STATE_DIR = os.path.join(HERE, "gen_data", NETWORK)
DYN_DIR = os.path.join(STATE_DIR, "dynamic_state_100ms_for_5s")
RUN_LOGS = os.path.join(HERE, "run", "logs_ns3")
PLOTS = os.path.join(HERE, "plots")
os.makedirs(PLOTS, exist_ok=True)

GS_NAMES = {60: "Tokyo", 61: "Mumbai", 62: "Shanghai",
            63: "Sao-Paulo", 64: "NY"}
FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
FPS = 10


def sat_subpoint(sat, epoch, t_ns):
    sat.compute(str(epoch + t_ns * u.ns))
    return float(sat.sublat) * 180 / np.pi, float(sat.sublong) * 180 / np.pi


def great_circle(lo1, la1, lo2, la2, n=22):
    dlon = ((lo2 - lo1 + 540) % 360) - 180
    lons = (np.linspace(lo1, lo1 + dlon, n) + 180) % 360 - 180
    lats = np.linspace(la1, la2, n)
    return lons, lats


def load_fstate_cache():
    cache = []
    for f in sorted(os.listdir(DYN_DIR)):
        if f.startswith("fstate_") and f.endswith(".txt"):
            t = int(f.split("_")[1].split(".")[0])
            cache.append((t, ap.read_fstate(os.path.join(DYN_DIR, f))))
    cache.sort()
    return cache


def load_logs():
    g, c, r = [], [], []
    for name in sorted(os.listdir(RUN_LOGS)):
        if name.startswith("llm_gather_node") and name.endswith(".csv"):
            with open(os.path.join(RUN_LOGS, name)) as f:
                for row in csv.DictReader(f):
                    g.append({k: int(v) if v.lstrip("-").isdigit() else v
                              for k, v in row.items()})
        elif name.startswith("llm_compute_node") and name.endswith(".csv"):
            with open(os.path.join(RUN_LOGS, name)) as f:
                for row in csv.DictReader(f):
                    c.append({k: int(v) if v.lstrip("-").isdigit() else v
                              for k, v in row.items()})
        elif name.startswith("llm_response_node") and name.endswith(".csv"):
            with open(os.path.join(RUN_LOGS, name)) as f:
                for row in csv.DictReader(f):
                    r.append({k: int(v) if v.lstrip("-").isdigit() else v
                              for k, v in row.items()})
    return g, c, r


def load_schedule():
    rows = []
    with open(os.path.join(HERE, "llm_workload_schedule.csv")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            rows.append({"src": int(parts[0]), "dst": int(parts[1])})
    return rows


def main():
    print("loading scenario")
    tles = read_tles(os.path.join(STATE_DIR, "tles.txt"))
    satellites = tles["satellites"]; epoch = tles["epoch"]
    num_sats = len(satellites); num_planes = num_sats // 10
    plane_colors = plt.cm.tab10(np.linspace(0, 1, max(num_planes, 3)))[:num_planes]
    ground_stations = read_ground_stations_extended(
        os.path.join(STATE_DIR, "ground_stations.txt"))
    gs_lat = np.array([float(g["latitude_degrees_str"]) for g in ground_stations])
    gs_lon = np.array([float(g["longitude_degrees_str"]) for g in ground_stations])
    gs_csv_names = [g["name"] for g in ground_stations]
    compute_set = {2, 12, 22, 32, 42, 52}

    schedule = load_schedule()
    gather, compute, response = load_logs()
    fstate_cache = load_fstate_cache()
    fstate_times = [t for t, _ in fstate_cache]
    print(f"  {len(gather)} gather, {len(compute)} compute, {len(response)} response events")

    # Pre-sort lifecycle events by their relevant time so we can
    # advance per-frame counters cheaply.
    emit_events    = sorted([(g["t_emit_ns"],          g["compute_sat_id"]) for g in gather])
    gather_events  = sorted([(g["t_last_arrival_ns"],  g["compute_sat_id"]) for g in gather])
    compute_events = sorted([(c["t_compute_end_ns"],   c["compute_sat_id"]) for c in compute])
    response_events_first = defaultdict(list)
    for r in response:
        response_events_first[(r["src_compute_sat_id"], r["req_id"])].append(r["t_response_recv_ns"])
    return_events = sorted([
        (min(ts), sat) for (sat, _), ts in response_events_first.items()
    ])

    # Coastline + land.
    coast = []; land = []
    try:
        cp = shpreader.natural_earth(resolution="110m",
                                     category="physical", name="coastline")
        for record in shpreader.Reader(cp).records():
            g = record.geometry
            geoms = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
            for line in geoms:
                xs, ys = line.coords.xy
                coast.append((np.array(xs), np.array(ys)))
    except Exception: pass
    try:
        lp = shpreader.natural_earth(resolution="110m", category="physical", name="land")
        for record in shpreader.Reader(lp).records():
            g = record.geometry
            polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
            for poly in polys:
                xs, ys = poly.exterior.coords.xy
                land.append(list(zip(xs, ys)))
    except Exception: pass

    fig, ax = plt.subplots(figsize=(14, 7))
    ax.set_facecolor("#d6e3f0")
    ax.set_xlim(-180, 180); ax.set_ylim(-85, 85); ax.set_aspect("equal")
    ax.set_xlabel("longitude (deg)"); ax.set_ylabel("latitude (deg)")
    ax.grid(True, color="#bbbbbb", linestyle=":", linewidth=0.4)
    for verts in land:
        ax.add_patch(MplPolygon(verts, facecolor="#f4ede0", edgecolor="none", zorder=1))
    for xs, ys in coast:
        ax.plot(xs, ys, color="#5a5a5a", linewidth=0.35, zorder=2)
    ax.scatter(gs_lon, gs_lat, s=100, marker="s", color="#c0392b",
               edgecolors="white", linewidth=0.6, zorder=6)
    for i, name in enumerate(gs_csv_names):
        ax.text(gs_lon[i] + 2, gs_lat[i] - 3, name,
                fontsize=7, color="#7b1d12", weight="bold", zorder=7)

    state = {"sats": [], "compute": [], "labels": [],
             "paths_fwd": [], "paths_back": [], "title": None}
    state["title"] = ax.text(0, 92, "", ha="center", va="bottom",
                             fontsize=11, weight="bold",
                             transform=ax.transData)

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
        plt.Line2D([0], [0], color="gray", linewidth=1.8, label="— REQUEST"),
        plt.Line2D([0], [0], color="gray", linewidth=1.8,
                   linestyle="--", label="-- RESPONSE"),
    ]
    ax.legend(handles=legend_handles, loc="lower left", fontsize=7,
              framealpha=0.9, title="lifecycle legend", ncol=2)

    def fstate_at(t_ns):
        s = {}
        for tt, delta in fstate_cache:
            if tt > t_ns: break
            s.update(delta)
        return s

    def cumulative_at(t_ns, events):
        # events is sorted [(t, dst), ...]; return per-sat dict of count
        out = defaultdict(int)
        for t, sat in events:
            if t > t_ns: break
            out[sat] += 1
        return out

    print(f"rendering {len(fstate_times)} frames @ {FPS} fps")

    def draw_frame(idx):
        t_ns = fstate_times[idx]; t_s = t_ns / 1e9
        for grp in ("sats", "compute", "labels", "paths_fwd", "paths_back"):
            for a in state[grp]:
                a.remove()
            state[grp] = []
        # Satellites at this time.
        sat_lat = np.zeros(num_sats); sat_lon = np.zeros(num_sats)
        for sid in range(num_sats):
            sat_lat[sid], sat_lon[sid] = sat_subpoint(satellites[sid], epoch, t_ns)
        for pi in range(num_planes):
            sids = [s for s in range(pi*10, (pi+1)*10) if s not in compute_set]
            if sids:
                sc = ax.scatter(sat_lon[sids], sat_lat[sids],
                                s=18, color=plane_colors[pi],
                                edgecolors="none", zorder=3, alpha=0.85)
                state["sats"].append(sc)
        emit_cnt   = cumulative_at(t_ns, emit_events)
        gather_cnt = cumulative_at(t_ns, gather_events)
        compute_cnt= cumulative_at(t_ns, compute_events)
        return_cnt = cumulative_at(t_ns, return_events)
        for sid in sorted(compute_set):
            pi = sid // 10
            sc = ax.scatter([sat_lon[sid]], [sat_lat[sid]],
                            s=180, marker="*", color=plane_colors[pi],
                            edgecolors="black", linewidth=1.0, zorder=5)
            state["compute"].append(sc)
            text = f"C{sid}  R{return_cnt[sid]}/C{compute_cnt[sid]}/G{gather_cnt[sid]}"
            t = ax.text(sat_lon[sid] + 1.6, sat_lat[sid] + 1.6, text,
                        fontsize=6, color="black", weight="bold", zorder=6,
                        bbox=dict(boxstyle="round,pad=0.12",
                                  facecolor="white", edgecolor="none", alpha=0.7))
            state["labels"].append(t)

        # Active paths (forward solid + return dashed).
        fstate = fstate_at(t_ns)
        active_flows = []
        for i, sched in enumerate(schedule):
            src, dst = sched["src"], sched["dst"]
            color = FLOW_COLORS[i % len(FLOW_COLORS)]
            # Forward
            try:
                path = ap.trace_path(fstate, src, dst, max_hops=64)
                for k in range(len(path) - 1):
                    a, b = path[k], path[k+1]
                    la, lo_a = ((sat_lat[a], sat_lon[a]) if a < num_sats
                                else (gs_lat[a-num_sats], gs_lon[a-num_sats]))
                    lb, lo_b = ((sat_lat[b], sat_lon[b]) if b < num_sats
                                else (gs_lat[b-num_sats], gs_lon[b-num_sats]))
                    lons, lats = great_circle(lo_a, la, lo_b, lb, n=20)
                    dlons = np.diff(lons)
                    starts = [0] + (np.where(np.abs(dlons) > 180)[0] + 1).tolist()
                    ends   = (np.where(np.abs(dlons) > 180)[0] + 1).tolist() + [len(lons)]
                    for s_i, e_i in zip(starts, ends):
                        if e_i - s_i >= 2:
                            ln, = ax.plot(lons[s_i:e_i], lats[s_i:e_i],
                                          color=color, linewidth=1.6, zorder=8, alpha=0.9)
                            state["paths_fwd"].append(ln)
                active_flows.append(i)
            except Exception:
                pass
            # Return
            try:
                path = ap.trace_path(fstate, dst, src, max_hops=64)
                for k in range(len(path) - 1):
                    a, b = path[k], path[k+1]
                    la, lo_a = ((sat_lat[a], sat_lon[a]) if a < num_sats
                                else (gs_lat[a-num_sats], gs_lon[a-num_sats]))
                    lb, lo_b = ((sat_lat[b], sat_lon[b]) if b < num_sats
                                else (gs_lat[b-num_sats], gs_lon[b-num_sats]))
                    lons, lats = great_circle(lo_a, la, lo_b, lb, n=20)
                    dlons = np.diff(lons)
                    starts = [0] + (np.where(np.abs(dlons) > 180)[0] + 1).tolist()
                    ends   = (np.where(np.abs(dlons) > 180)[0] + 1).tolist() + [len(lons)]
                    for s_i, e_i in zip(starts, ends):
                        if e_i - s_i >= 2:
                            ln, = ax.plot(lons[s_i:e_i], lats[s_i:e_i],
                                          color=color, linewidth=1.6, linestyle="--",
                                          zorder=8, alpha=0.85)
                            state["paths_back"].append(ln)
            except Exception:
                pass

        E = sum(emit_cnt.values())
        G = sum(gather_cnt.values())
        C = sum(compute_cnt.values())
        R = sum(return_cnt.values())
        state["title"].set_text(
            f"t = {t_s:4.2f} s | E={E} emitted | G={G} gathered | C={C} computed | R={R} returned"
        )
        return (state["title"], *state["sats"], *state["compute"],
                *state["labels"], *state["paths_fwd"], *state["paths_back"])

    anim = FuncAnimation(fig, draw_frame, frames=len(fstate_times),
                         interval=1000//FPS, blit=False, repeat=False)
    fig.suptitle(
        "Phase C — full LLM inference lifecycle (request → token → packet "
        "→ compute → response)\n"
        "60 sats / 5 GS / 6 compute SATs; per-compute counters R/C/G "
        "= returned / computed / gathered cumulative",
        fontsize=10)
    fig.tight_layout(rect=[0, 0, 1, 0.92])
    out = os.path.join(PLOTS, "topology_anim.gif")
    anim.save(out, writer=PillowWriter(fps=FPS), dpi=100)
    print(f"wrote {out} ({os.path.getsize(out)/1024/1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
