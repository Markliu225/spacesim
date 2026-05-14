#!/usr/bin/env python3
"""Plot the mixed-topology scenario on a world map with all 5 flow paths.

What gets drawn (PlateCarree projection):

  - 60 satellites propagated to t = 200 ms (first flow's start time)
      * compute SATs (6) as filled blue circles
      * transit SATs (54) as small gray dots
  - 120 ISLs as light gray polylines (some wrap around the antimeridian
    -- those are clipped, not bridged across +-180 deg)
  - 5 ground stations as red squares with labels
  - For each of the 5 flows, the path traced from src to dst (via fstate
    at the flow's start_time, accumulated from t=0) is overlaid as a
    coloured polyline with arrowheads.

Output: ``plots/topology_paths.png``.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# NOTE: we deliberately do NOT use cartopy's GeoAxes here. Cartopy 0.18
# (which our venv ships, because it's the last version that builds against
# PROJ 6.3.1 on Ubuntu 20.04) is incompatible with matplotlib >= 3.7 at the
# native level -- using its GeoAxes triggers `free(): invalid size` from
# the GEOS shared library mid-render. Instead we plot in plain lat/lon
# Cartesian coords (which is exactly the Plate Carree projection) and
# manually overlay coastlines from cartopy's bundled Natural Earth
# shapefile. That uses only the shapereader + shapely paths, which work.
import cartopy.io.shapereader as shpreader

# Make satgen + phase_a tools importable.
HERE = os.path.abspath(os.path.dirname(__file__))
PHASE_A = os.path.abspath(os.path.join(HERE, "..", ".."))
HYPATIA = os.path.abspath(os.path.join(PHASE_A, "..", ".."))
SATGENPY = os.path.join(HYPATIA, "satgenpy")
for p in (PHASE_A, SATGENPY):
    if p not in sys.path:
        sys.path.insert(0, p)

from satgen.tles import read_tles  # noqa: E402
from satgen.isls import read_isls  # noqa: E402
from satgen.ground_stations import read_ground_stations_extended  # noqa: E402
from astropy import units as u  # noqa: E402

import analyze_phase_a as ap  # for read_fstate / trace_path  # noqa: E402

# (we only use cartopy's shapereader for the coastline data; no GeoAxes)

NETWORK_NAME = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"
STATE_DIR = os.path.join(HERE, "gen_data", NETWORK_NAME)
DYN_DIR = os.path.join(STATE_DIR, "dynamic_state_100ms_for_5s")
RUN_LOGS = os.path.join(HERE, "run", "logs_ns3")
OUT_PNG = os.path.join(HERE, "plots", "topology_paths.png")

NUM_SATS = 60
SNAPSHOT_NS = 200_000_000   # t = 200 ms, first flow's start

FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]


def sat_lat_lon(sat, epoch_str: str, t_str: str) -> Tuple[float, float]:
    """Return (lat_deg, lon_deg) of the sub-satellite point at given time."""
    sat.compute(t_str)
    # ephem returns radians; convert to degrees.
    return float(sat.sublat) * 180.0 / np.pi, float(sat.sublong) * 180.0 / np.pi


def read_compute_set(roles_path: str) -> set:
    compute = set()
    with open(roles_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sid_str, role = line.split(",")
            if role.strip() == "C":
                compute.add(int(sid_str))
    return compute


def read_schedule(path: str) -> List[Dict[str, int]]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            out.append({
                "id": int(parts[0]),
                "from": int(parts[1]),
                "to": int(parts[2]),
                "size": int(parts[3]),
                "start_ns": int(parts[4]),
                "metadata": parts[6] if len(parts) > 6 else "",
            })
    return out


def load_full_fstate_at(dyn_dir: str, target_t: int):
    state = {}
    ts = sorted(int(n.split("_")[1].split(".")[0])
                for n in os.listdir(dyn_dir) if n.startswith("fstate_"))
    for t in ts:
        if t > target_t:
            break
        state.update(ap.read_fstate(os.path.join(dyn_dir, f"fstate_{t}.txt")))
    return state


def _great_circle_polyline(lon1: float, lat1: float, lon2: float, lat2: float,
                            n: int = 30) -> Tuple[np.ndarray, np.ndarray]:
    """Sample a great-circle path between two (lon, lat) points.

    Used for ISLs / flow path segments so they curve along the surface
    instead of cutting straight in a Plate-Carree plot. Cartopy's
    `transform=ccrs.Geodetic()` would do this too, but we want full
    control over splitting at the antimeridian.
    """
    # Convert to radians.
    p1 = np.radians([lon1, lat1])
    p2 = np.radians([lon2, lat2])
    # Spherical interpolation.
    x1 = np.array([np.cos(p1[1]) * np.cos(p1[0]),
                   np.cos(p1[1]) * np.sin(p1[0]),
                   np.sin(p1[1])])
    x2 = np.array([np.cos(p2[1]) * np.cos(p2[0]),
                   np.cos(p2[1]) * np.sin(p2[0]),
                   np.sin(p2[1])])
    dot = float(np.clip(np.dot(x1, x2), -1.0, 1.0))
    omega = np.arccos(dot)
    if omega < 1e-9:
        return np.array([lon1, lon2]), np.array([lat1, lat2])
    t = np.linspace(0, 1, n)
    so = np.sin(omega)
    a = np.sin((1 - t) * omega) / so
    b = np.sin(t * omega) / so
    xs = a[:, None] * x1[None, :] + b[:, None] * x2[None, :]
    lons = np.degrees(np.arctan2(xs[:, 1], xs[:, 0]))
    lats = np.degrees(np.arcsin(xs[:, 2]))
    return lons, lats


def main() -> int:
    os.makedirs(os.path.dirname(OUT_PNG), exist_ok=True)

    tles = read_tles(os.path.join(STATE_DIR, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    list_isls = read_isls(os.path.join(STATE_DIR, "isls.txt"), len(satellites))
    ground_stations = read_ground_stations_extended(
        os.path.join(STATE_DIR, "ground_stations.txt"))

    compute_set = read_compute_set(os.path.join(HERE, "satellite_roles.txt"))
    schedule = read_schedule(os.path.join(HERE, "schedule.csv"))

    # Propagate every sat to t = SNAPSHOT_NS.
    snap_time = epoch + SNAPSHOT_NS * u.ns
    t_str = str(snap_time)
    sat_lat = np.zeros(len(satellites))
    sat_lon = np.zeros(len(satellites))
    for i, sat in enumerate(satellites):
        sat_lat[i], sat_lon[i] = sat_lat_lon(sat, str(epoch), t_str)

    # GS positions (satgenpy stores lat/lon as decimal strings)
    gs_lat = np.array([float(g["latitude_degrees_str"]) for g in ground_stations])
    gs_lon = np.array([float(g["longitude_degrees_str"]) for g in ground_stations])
    gs_names = [g["name"] for g in ground_stations]

    # Plain matplotlib figure in lat/lon coords (Plate Carree). Cartopy
    # 0.18's GeoAxes is unstable on Ubuntu 20.04 + matplotlib 3.7, so we
    # render the coastlines manually from cartopy's bundled Natural Earth
    # shapefile and let matplotlib handle the rest as a regular 2D plot.
    fig, ax = plt.subplots(figsize=(15, 8))
    ax.set_facecolor("#d6e3f0")  # ocean
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_xlabel("longitude (deg)")
    ax.set_ylabel("latitude (deg)")
    ax.set_aspect("equal")
    ax.grid(True, color="#bbbbbb", linestyle=":", linewidth=0.4)

    # Coastlines from Natural Earth 110 m (bundled with cartopy).
    coast_path = shpreader.natural_earth(resolution="110m",
                                         category="physical",
                                         name="coastline")
    try:
        reader = shpreader.Reader(coast_path)
        for record in reader.records():
            geom = record.geometry
            geoms = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
            for line in geoms:
                xs, ys = line.coords.xy
                ax.plot(xs, ys, color="#5a5a5a", linewidth=0.4, zorder=2)
    except Exception as e:
        print(f"  warning: coastline overlay failed: {e}")

    # Decorate land polygons (filled) for context.
    try:
        land_path = shpreader.natural_earth(resolution="110m",
                                            category="physical",
                                            name="land")
        from matplotlib.patches import Polygon as MplPolygon
        reader = shpreader.Reader(land_path)
        for record in reader.records():
            geom = record.geometry
            polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
            for poly in polys:
                xs, ys = poly.exterior.coords.xy
                ax.add_patch(MplPolygon(list(zip(xs, ys)),
                                        facecolor="#f4ede0",
                                        edgecolor="none", zorder=1))
    except Exception as e:
        print(f"  warning: land fill failed: {e}")

    # Draw ISLs first (lowest z-order).
    for (a, b) in list_isls:
        lon1, lat1 = sat_lon[a], sat_lat[a]
        lon2, lat2 = sat_lon[b], sat_lat[b]
        lons, lats = _great_circle_polyline(lon1, lat1, lon2, lat2, n=30)
        # Split where the polyline crosses the antimeridian to avoid horizontal
        # lines wrapping across the globe.
        dlons = np.diff(lons)
        seg_starts = [0] + (np.where(np.abs(dlons) > 180)[0] + 1).tolist()
        seg_ends = (np.where(np.abs(dlons) > 180)[0] + 1).tolist() + [len(lons)]
        for s, e in zip(seg_starts, seg_ends):
            if e - s < 2:
                continue
            ax.plot(lons[s:e], lats[s:e], color="#cccccc", linewidth=0.5,
                    zorder=1)

    # Draw satellites: colour each by its orbital plane to make the
    # constellation structure visible, then overlay compute SATs with a
    # larger star marker + thick black outline + bold "C<id>" label.
    # Transit SATs intentionally don't get text labels -- 54 of them
    # would be unreadable.
    NUM_PLANES = NUM_SATS // 10  # 6 planes x 10 sats
    plane_colors = plt.cm.tab10(np.linspace(0, 1, max(NUM_PLANES, 3)))[:NUM_PLANES]

    transit_mask = np.array([i not in compute_set for i in range(NUM_SATS)])
    compute_mask = ~transit_mask

    # Transit sats: small filled dots, plane colour.
    for plane_idx in range(NUM_PLANES):
        plane_sat_ids = [s for s in range(plane_idx * 10, (plane_idx + 1) * 10)
                          if s not in compute_set]
        if not plane_sat_ids:
            continue
        ax.scatter(sat_lon[plane_sat_ids], sat_lat[plane_sat_ids],
                   s=22, color=plane_colors[plane_idx],
                   edgecolors="white", linewidth=0.4,
                   zorder=3, alpha=0.85)

    # Compute sats: big star, plane colour fill, black outline, bold C<id>
    # label. Drawn after transit dots so they sit on top.
    for sid in sorted(compute_set):
        plane_idx = sid // 10
        ax.scatter([sat_lon[sid]], [sat_lat[sid]],
                   s=240, marker="*", color=plane_colors[plane_idx],
                   edgecolors="black", linewidth=1.4, zorder=5)
        ax.text(sat_lon[sid] + 2.0, sat_lat[sid] + 2.0, f"C{sid}",
                fontsize=10, color="black", weight="bold",
                zorder=6,
                bbox=dict(boxstyle="round,pad=0.15",
                          facecolor="white", edgecolor="none", alpha=0.7))

    # Build a legend mapping plane colour -> "plane k (compute = C<id>)".
    plane_handles = []
    for plane_idx in range(NUM_PLANES):
        compute_in_plane = sorted(s for s in compute_set
                                  if plane_idx * 10 <= s < (plane_idx + 1) * 10)
        compute_label = ", ".join(f"C{s}" for s in compute_in_plane) or "—"
        plane_handles.append(plt.Line2D(
            [0], [0], marker="o", color="none",
            markerfacecolor=plane_colors[plane_idx],
            markeredgecolor="white", markersize=8,
            label=f"plane {plane_idx} (compute: {compute_label})",
        ))
    # Generic type legend entries -- separate from per-plane colours.
    type_handles = [
        plt.Line2D([0], [0], marker="*", color="none",
                   markerfacecolor="#bbbbbb", markeredgecolor="black",
                   markeredgewidth=1.2, markersize=14,
                   label="compute SAT (type=C)"),
        plt.Line2D([0], [0], marker="o", color="none",
                   markerfacecolor="#bbbbbb", markeredgecolor="white",
                   markersize=6, label="transit SAT (type=T)"),
    ]

    # Draw GSs.
    ax.scatter(gs_lon, gs_lat, s=120, marker="s",
               color="#c0392b", edgecolors="white", linewidth=0.8,
               zorder=6,
               label=f"ground stations ({len(gs_lat)})")
    for i, name in enumerate(gs_names):
        ax.text(gs_lon[i] + 2.0, gs_lat[i] - 2.0, name,
                fontsize=9, color="#7b1d12", weight="bold",
                zorder=7)

    # Overlay each flow path.
    def node_lat_lon(n: int) -> Tuple[float, float]:
        if n < NUM_SATS:
            return sat_lat[n], sat_lon[n]
        return gs_lat[n - NUM_SATS], gs_lon[n - NUM_SATS]

    legend_lines = []
    for fl in schedule:
        fstate = load_full_fstate_at(DYN_DIR, fl["start_ns"])
        try:
            path = ap.trace_path(fstate, fl["from"], fl["to"], max_hops=64)
        except Exception as e:
            print(f"  flow {fl['id']}: trace failed: {e}")
            continue
        color = FLOW_COLORS[fl["id"] % len(FLOW_COLORS)]
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            la, lo_a = node_lat_lon(a)
            lb, lo_b = node_lat_lon(b)
            lons, lats = _great_circle_polyline(lo_a, la, lo_b, lb, n=30)
            dlons = np.diff(lons)
            seg_starts = [0] + (np.where(np.abs(dlons) > 180)[0] + 1).tolist()
            seg_ends = (np.where(np.abs(dlons) > 180)[0] + 1).tolist() + [len(lons)]
            for s, e in zip(seg_starts, seg_ends):
                if e - s < 2:
                    continue
                ax.plot(lons[s:e], lats[s:e], color=color, linewidth=2.4,
                        zorder=8,
                        alpha=0.9)
        # Mark src with hollow ring, dst with arrowhead.
        src_lat, src_lon = node_lat_lon(fl["from"])
        dst_lat, dst_lon = node_lat_lon(fl["to"])
        ax.scatter([src_lon], [src_lat], s=180, facecolors="none",
                   edgecolors=color, linewidth=2.0,
                   zorder=9)
        ax.scatter([dst_lon], [dst_lat], s=180, marker="*",
                   color=color, edgecolors="white", linewidth=0.6,
                   zorder=9)

        meta = fl["metadata"]
        legend_lines.append(plt.Line2D([0], [0], color=color, linewidth=2.4,
                                       label=f"flow {fl['id']} ({len(path)-1} hops): {meta}"))

    # Three legends so the type / plane / flow encoding is all explicit:
    #   - top-left   : satellite type marker (compute star vs transit dot)
    #   - bottom-left: per-orbital-plane colour swatch with its compute SAT id
    #   - bottom-right: flow path colours with src/dst metadata
    gs_handle = plt.Line2D([0], [0], marker="s", color="none",
                           markerfacecolor="#c0392b", markeredgecolor="white",
                           markersize=10, label="ground station (GS)")
    leg_type = ax.legend(handles=type_handles + [gs_handle],
                         loc="upper left", fontsize=8,
                         framealpha=0.92, title="node type")
    ax.add_artist(leg_type)
    leg_plane = ax.legend(handles=plane_handles, loc="lower left",
                          fontsize=7, framealpha=0.92,
                          title=f"orbital planes ({NUM_PLANES})")
    ax.add_artist(leg_plane)
    if legend_lines:
        ax.legend(handles=legend_lines, loc="lower right", fontsize=8,
                  framealpha=0.92, title="flow paths (GS → compute = main test)")

    compute_id_list = "/".join(f"C{s}" for s in sorted(compute_set))
    ax.set_title(
        f"Mixed-topology smoke test — primary measurement: GS → compute SAT latency\n"
        f"60 sats (6 planes × 10), 6 compute = {compute_id_list}, 120 ISLs, 5 GS    "
        f"·    snapshot at t = {SNAPSHOT_NS/1e6:.0f} ms",
        fontsize=11,
    )
    plt.tight_layout()
    fig.savefig(OUT_PNG, dpi=160, bbox_inches="tight")
    print(f"wrote {OUT_PNG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())