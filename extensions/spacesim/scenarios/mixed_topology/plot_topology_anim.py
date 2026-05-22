#!/usr/bin/env python3
"""
Animated GIF of the mixed-topology scenario.

Renders one frame per state-gen timestep (100 ms apart, 50 frames over
5 s of simulated time) showing:

  - all 60 satellites at their SGP-4-propagated lat/lon, plane-coloured
  - 6 compute SATs as black-edged stars (with C<id> on first frame for
    the eye to anchor, then nameless to keep frames clean)
  - 5 ground stations as red squares with city names
  - all flows currently in flight at this t, traced via fstate

Playback: 10 fps -> 5 seconds of sim time = 5 seconds of GIF (real-time).

The fast-poll rendering (50 frames * SGP-4 prop + path trace) takes ~30 s
end-to-end. Output ~3-5 MB depending on detail.

Pillow writer is used (cross-platform, no ffmpeg dependency).
"""

from __future__ import annotations

import csv
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FuncAnimation, PillowWriter

import cartopy.io.shapereader as shpreader  # only shapefile reader, not GeoAxes


_HERE = os.path.abspath(os.path.dirname(__file__))
_PHASE_A_DIR = os.path.abspath(os.path.join(_HERE, "..", ".."))
_HYPATIA_ROOT = os.path.abspath(os.path.join(_PHASE_A_DIR, "..", ".."))
_SATGENPY = os.path.join(_HYPATIA_ROOT, "satgenpy")
for p in (_PHASE_A_DIR, _SATGENPY):
    if p not in sys.path:
        sys.path.insert(0, p)

from satgen.tles import read_tles  # noqa: E402
from satgen.ground_stations import read_ground_stations_extended  # noqa: E402
from astropy import units as u  # noqa: E402

import analyze_phase_a as ap  # noqa: E402


NETWORK = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"
STATE_DIR = os.path.join(_HERE, "gen_data", NETWORK)
DYN_DIR = os.path.join(STATE_DIR, "dynamic_state_100ms_for_5s")
RUN_DIR = os.path.join(_HERE, "run")
PLOTS_DIR = os.path.join(_HERE, "plots")
os.makedirs(PLOTS_DIR, exist_ok=True)

FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
FPS = 10  # one frame per 100 ms = real-time playback


def sat_subpoint(sat, epoch, t_ns: int):
    time = epoch + t_ns * u.ns
    sat.compute(str(time))
    return float(sat.sublat) * 180.0 / np.pi, float(sat.sublong) * 180.0 / np.pi


def load_fstate_at_ascending(t_ns: int, _cache={}):
    """Cache-aware accumulator. Walks the dynamic-state dir once."""
    if not _cache:
        # Pre-load all fstate files into a list of (t, delta_dict).
        files = sorted(int(f.split("_")[1].split(".")[0])
                       for f in os.listdir(DYN_DIR) if f.startswith("fstate_"))
        _cache["files"] = files
        _cache["deltas"] = []
        for t in files:
            delta = {}
            with open(os.path.join(DYN_DIR, f"fstate_{t}.txt")) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(",")
                    if len(parts) != 5:
                        continue
                    delta[(int(parts[0]), int(parts[1]))] = (
                        int(parts[2]), int(parts[3]), int(parts[4]))
            _cache["deltas"].append((t, delta))

    state: dict = {}
    for t, delta in _cache["deltas"]:
        if t > t_ns:
            break
        state.update(delta)
    return state


def load_flows():
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
                "metadata": row[6] if len(row) > 6 else "",
            })
    return flows


def great_circle(lo1, la1, lo2, la2, n=20):
    dlon = ((lo2 - lo1 + 540) % 360) - 180
    lons = (np.linspace(lo1, lo1 + dlon, n) + 180) % 360 - 180
    lats = np.linspace(la1, la2, n)
    return lons, lats


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
    gs_lat = np.array([float(g["latitude_degrees_str"]) for g in ground_stations])
    gs_lon = np.array([float(g["longitude_degrees_str"]) for g in ground_stations])
    gs_names = [g["name"] for g in ground_stations]

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

    # Precompute coastlines once.
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

    # Precompute land polygons (faster to re-use than re-read every frame).
    land_polygons = []
    try:
        lp = shpreader.natural_earth(resolution="110m",
                                     category="physical", name="land")
        reader = shpreader.Reader(lp)
        from matplotlib.patches import Polygon as MplPolygon
        for record in reader.records():
            g = record.geometry
            polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
            for poly in polys:
                xs, ys = poly.exterior.coords.xy
                land_polygons.append(list(zip(xs, ys)))
    except Exception as e:
        print(f"  land polygon load failed: {e}")

    # Discover frames (one per fstate timestep).
    fstate_times = sorted(
        int(f.split("_")[1].split(".")[0])
        for f in os.listdir(DYN_DIR) if f.startswith("fstate_"))
    print(f"animation: {len(fstate_times)} frames at {FPS} fps "
          f"({len(fstate_times) / FPS:.1f} s playback)")

    # Set up figure.
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.set_facecolor("#d6e3f0")
    ax.set_xlim(-180, 180)
    ax.set_ylim(-85, 85)
    ax.set_aspect("equal")
    ax.set_xlabel("longitude (deg)")
    ax.set_ylabel("latitude (deg)")
    ax.grid(True, color="#bbbbbb", linestyle=":", linewidth=0.4)

    # Static layers: land + coastlines.
    from matplotlib.patches import Polygon as MplPolygon
    for verts in land_polygons:
        ax.add_patch(MplPolygon(verts, facecolor="#f4ede0", edgecolor="none",
                                 zorder=1))
    for xs, ys in coastline_paths:
        ax.plot(xs, ys, color="#999999", linewidth=0.3, zorder=2)

    # Static GS layer.
    ax.scatter(gs_lon, gs_lat, s=80, marker="s", color="#c0392b",
               edgecolors="white", linewidth=0.6, zorder=6)
    for i, name in enumerate(gs_names):
        ax.text(gs_lon[i] + 2, gs_lat[i] - 3, name,
                fontsize=7, color="#7b1d12", weight="bold", zorder=7)

    # Mutable layers (re-drawn each frame): sats + flow paths + title.
    # We keep handles in a dict so we can call .remove() on them.
    artists = {"sats": [], "compute": [], "compute_labels": [],
               "paths": [], "title": None}

    title = ax.text(
        0, 92,  # axes coord-ish, above plot
        "",
        ha="center", va="bottom", fontsize=11, weight="bold",
        transform=ax.transData,
    )
    artists["title"] = title

    def draw_frame(frame_idx):
        t_ns = fstate_times[frame_idx]
        t_s = t_ns / 1e9

        # Remove last frame's mutable artists.
        for grp in ("sats", "compute", "compute_labels", "paths"):
            for a in artists[grp]:
                a.remove()
            artists[grp] = []

        # Propagate all sats.
        sat_lat = np.zeros(num_sats)
        sat_lon = np.zeros(num_sats)
        for sid in range(num_sats):
            sat_lat[sid], sat_lon[sid] = sat_subpoint(satellites[sid], epoch, t_ns)

        # Transit sats: small dots, plane colour.
        for plane_idx in range(num_planes):
            sids = [s for s in range(plane_idx * 10, (plane_idx + 1) * 10)
                    if s not in compute_set]
            if sids:
                sc = ax.scatter(sat_lon[sids], sat_lat[sids],
                                s=18, color=plane_colors[plane_idx],
                                edgecolors="none", zorder=3, alpha=0.85)
                artists["sats"].append(sc)

        # Compute sats: large star.
        for sid in sorted(compute_set):
            plane_idx = sid // 10
            sc = ax.scatter([sat_lon[sid]], [sat_lat[sid]],
                            s=140, marker="*", color=plane_colors[plane_idx],
                            edgecolors="black", linewidth=1.0, zorder=5)
            artists["compute"].append(sc)
            t = ax.text(sat_lon[sid] + 1.6, sat_lat[sid] + 1.6, f"C{sid}",
                        fontsize=7, color="black", weight="bold", zorder=6,
                        bbox=dict(boxstyle="round,pad=0.1",
                                  facecolor="white", edgecolor="none", alpha=0.6))
            artists["compute_labels"].append(t)

        # Active flows.
        active = []
        if t_ns > 0:
            fstate = load_fstate_at_ascending(t_ns)
            for fl in flows:
                if fl["start_ns"] > t_ns:
                    continue
                if fl["end_ns"] > 0 and t_ns > fl["end_ns"]:
                    continue
                try:
                    path = ap.trace_path(fstate, fl["from"], fl["to"], max_hops=64)
                except Exception:
                    continue
                active.append(fl["id"])
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
                            line, = ax.plot(lons[s:e], lats[s:e],
                                            color=color, linewidth=2.0,
                                            zorder=8, alpha=0.95)
                            artists["paths"].append(line)

        # Title.
        active_str = ", ".join(f"f{i}" for i in active) if active else "(none)"
        artists["title"].set_text(
            f"t = {t_s:5.2f} s    {len(active)} flows in flight: {active_str}"
        )

        return (artists["title"], *artists["sats"], *artists["compute"],
                *artists["compute_labels"], *artists["paths"])

    print("rendering frames (~30 s)...")
    anim = FuncAnimation(
        fig, draw_frame, frames=len(fstate_times),
        blit=False, repeat=False, interval=1000 // FPS,
    )

    # Outer title (kept static; per-frame info goes in the in-plot title).
    fig.suptitle(
        "Mixed topology animation — GS → compute primary measurement\n"
        "60 sats (6 planes × 10), 6 compute = C2/C12/C22/C32/C42/C52, 5 GS, 5 TCP flows",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.93])

    out_gif = os.path.join(PLOTS_DIR, "topology_anim.gif")
    writer = PillowWriter(fps=FPS)
    anim.save(out_gif, writer=writer, dpi=100)
    print(f"wrote {out_gif}")

    # Print size for sanity.
    sz = os.path.getsize(out_gif) / 1024 / 1024
    print(f"  size: {sz:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())