#!/usr/bin/env python3
"""
Augment Hypatia's per-timestep fstate_*.txt files with routes whose
destination is a satellite (not a ground station).

Why this exists
---------------

Hypatia's `satgenpy/satgen/dynamic_state/fstate_calculation.py` writes
forwarding-state entries only for ground-station destinations -- both
`calculate_fstate_shortest_path_without_gs_relaying` and the
`with_gs_relaying` variant iterate `for dst_gid in range(num_ground_stations)`.
ns-3's arbiter (`arbiter-single-forward-helper.cc`) is happy to accept
SAT ids as `target_node_id` (the only check is `target_node_id <
m_nodes.GetN()`), but if no row mentions a SAT as dst, the corresponding
slot stays at the (-2,-2,-2) "invalid entry" sentinel and packets get
dropped.

For Phase A (LLM-on-satellite) we need exactly one new capability:
forwarding to a compute satellite. This script reads the state directory
produced by satgenpy and *appends* the missing SAT-dst rows to every
`fstate_<t>.txt`, using the same shortest-path / interface conventions
that satgenpy uses for GS-dst rows. Hypatia's own code is not modified.

Algorithm (mirrors fstate_calculation.py)
-----------------------------------------

For each timestep t already produced by satgenpy:

1. Propagate the satellites to t (SGP-4 via pyephem inside satgen).
2. Build an ISL graph G_isl over satellites with edge weight = current
   inter-satellite distance, filtering edges by `max_isl_length_m`
   (same as satgenpy).
3. Compute ground_station_satellites_in_range[gid] = sorted list of
   (gsl_distance_m, sat_id) for sats within `max_gsl_length_m`.
4. Build `sat_neighbor_to_if` and `num_isls_per_sat` from isls.txt
   in declaration order (matches satgenpy's allocation order).
5. Floyd-Warshall on G_isl -> dist_sat_net.
6. For each dst SAT C (user-supplied):
     For each current node n:
       - If n == C: skip (self-delivery handled by IPv4 stack).
       - If n is a satellite S != C:
            Among S's ISL neighbors find the neighbor N* that minimises
            ISL(S, N*) + dist_sat_net[N*, C]. If no neighbor reaches C
            (inf), write a drop entry (-1,-1,-1).
            next_hop_decision = (N*, sat_neighbor_to_if[S, N*],
                                 sat_neighbor_to_if[N*, S])
       - If n is a GS G:
            Among the satellites in range of G, find S* minimising
            gsl(G,S*) + dist_sat_net[S*, C].
            next_hop_decision = (S*, 0, num_isls_per_sat[S*])
            (GS has only one GSL iface so my_if = 0; the sat GSL iface
            sits *after* its ISL ifaces, hence num_isls_per_sat[S*].)
7. Append the chosen rows to fstate_<t>.txt in the same CSV format
   satgenpy uses:
       current_id, dst_id, next_hop_id, my_if_id, next_if_id

Notes
-----

- We do *not* attempt delta-encoding across time. Every timestep we
  write the full set of SAT-dst rows for every (n, C). ns-3 applies
  each row idempotently via `SetSingleForwardState`, so this is correct,
  just slightly larger on disk. The size for one compute SAT and a
  1584-sat / 100-GS / 50-timestep state is ~5 MB, acceptable.
- We only generate routes for the compute SATs listed via --dst-sats.
  Phase A only routes one flow to one SAT-Y; for Phase B+ pass the
  full type-C set from satellite_roles.txt.
- The script is idempotent at the *line* level: re-running will append
  duplicate rows. Use --rewrite to first strip our previously-added
  SAT-dst lines before appending.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from typing import Dict, List, Set, Tuple

import networkx as nx


# --- Make satgen importable --------------------------------------------------

_HERE = os.path.abspath(os.path.dirname(__file__))
_SATGENPY_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "satgenpy"))
if _SATGENPY_DIR not in sys.path:
    sys.path.insert(0, _SATGENPY_DIR)

import exputil  # noqa: E402
from satgen.tles import read_tles  # noqa: E402
from satgen.isls import read_isls  # noqa: E402
from satgen.ground_stations import read_ground_stations_extended  # noqa: E402
from satgen.distance_tools import (  # noqa: E402
    distance_m_between_satellites,
    distance_m_ground_station_to_satellite,
)
from astropy import units as u  # noqa: E402


# --- Helpers -----------------------------------------------------------------


_SAT_DST_TAG_RE = re.compile(r"#\s*PHASE_A_AUGMENT\b")


def parse_dst_sats(arg: str, roles_path: str | None, num_satellites: int) -> List[int]:
    """Resolve --dst-sats argument into a sorted list of sat IDs.

    Accepts either a comma-separated list of integers, the literal
    string `all-compute` (must be combined with --roles), or a single
    integer.
    """
    if arg == "all-compute":
        if not roles_path:
            raise SystemExit("--dst-sats=all-compute requires --roles")
        compute = []
        with open(roles_path) as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) != 2:
                    raise SystemExit(f"{roles_path}:{ln}: malformed row {line!r}")
                sid, role = int(parts[0]), parts[1].strip()
                if role == "C":
                    compute.append(sid)
        return sorted(compute)
    ids = sorted({int(x) for x in arg.split(",") if x.strip()})
    bad = [x for x in ids if x < 0 or x >= num_satellites]
    if bad:
        raise SystemExit(f"--dst-sats values out of range [0, {num_satellites}): {bad}")
    return ids


def discover_timesteps(dynamic_state_dir: str) -> List[int]:
    """List all fstate timestamps already in the dir, sorted ascending."""
    times = []
    pat = re.compile(r"^fstate_(\d+)\.txt$")
    for name in os.listdir(dynamic_state_dir):
        m = pat.match(name)
        if m:
            times.append(int(m.group(1)))
    times.sort()
    return times


def build_isl_metadata(
    list_isls: List[Tuple[int, int]], num_satellites: int
) -> Tuple[List[int], Dict[Tuple[int, int], int]]:
    """Compute num_isls_per_sat and sat_neighbor_to_if, matching satgenpy."""
    num_isls_per_sat = [0] * num_satellites
    sat_neighbor_to_if: Dict[Tuple[int, int], int] = {}
    for (a, b) in list_isls:
        sat_neighbor_to_if[(a, b)] = num_isls_per_sat[a]
        sat_neighbor_to_if[(b, a)] = num_isls_per_sat[b]
        num_isls_per_sat[a] += 1
        num_isls_per_sat[b] += 1
    return num_isls_per_sat, sat_neighbor_to_if


def strip_previous_augment(fstate_path: str) -> None:
    """Remove any rows we previously appended (marked by trailing tag line)."""
    if not os.path.exists(fstate_path):
        return
    with open(fstate_path) as f:
        lines = f.readlines()
    # Find first augment marker; keep everything before it.
    cut = None
    for i, line in enumerate(lines):
        if _SAT_DST_TAG_RE.search(line):
            cut = i
            break
    if cut is None:
        return
    with open(fstate_path, "w") as f:
        f.writelines(lines[:cut])


def fstate_path_exists_with_augment(fstate_path: str) -> bool:
    """Quick check: has this file already been augmented?"""
    if not os.path.exists(fstate_path):
        return False
    with open(fstate_path) as f:
        for line in f:
            if _SAT_DST_TAG_RE.search(line):
                return True
    return False


# --- Per-timestep computation ------------------------------------------------


def compute_augment_rows(
    *,
    time_since_epoch_ns: int,
    epoch,
    satellites,
    ground_stations,
    list_isls,
    num_isls_per_sat: List[int],
    sat_neighbor_to_if: Dict[Tuple[int, int], int],
    max_isl_length_m: float,
    max_gsl_length_m: float,
    dst_sats: List[int],
) -> List[Tuple[int, int, int, int, int]]:
    """Compute the (curr, dst_sat, next_hop, my_if, next_if) rows.

    Mirrors satgenpy's calculate_fstate_shortest_path_without_gs_relaying
    but with the dst loop iterating over SAT IDs instead of GS IDs.
    """
    num_sats = len(satellites)
    num_gs = len(ground_stations)
    time = epoch + time_since_epoch_ns * u.ns
    epoch_str, time_str = str(epoch), str(time)

    # Build ISL graph with current edge weights
    g_isl = nx.Graph()
    for i in range(num_sats):
        g_isl.add_node(i)
    for (a, b) in list_isls:
        d = distance_m_between_satellites(satellites[a], satellites[b], epoch_str, time_str)
        # Same hard check satgenpy applies; surface clearly if it ever fails.
        if d > max_isl_length_m:
            raise RuntimeError(
                f"ISL ({a},{b}) length {d:.1f}m exceeds max {max_isl_length_m:.1f}m at "
                f"t={time_since_epoch_ns}ns -- this would also crash satgenpy"
            )
        g_isl.add_edge(a, b, weight=d)

    # All-pairs sat distances
    dist_sat_net = nx.floyd_warshall_numpy(g_isl)

    # GS -> sats in range
    gs_in_range: List[List[Tuple[float, int]]] = []
    for gs in ground_stations:
        in_range: List[Tuple[float, int]] = []
        for sid in range(num_sats):
            d = distance_m_ground_station_to_satellite(gs, satellites[sid], epoch_str, time_str)
            if d <= max_gsl_length_m:
                in_range.append((d, sid))
        in_range.sort()
        gs_in_range.append(in_range)

    rows: List[Tuple[int, int, int, int, int]] = []

    for dst_sat in dst_sats:
        # Satellites as src
        for curr in range(num_sats):
            if curr == dst_sat:
                # Self: skip (delivered locally by IPv4 stack on the sat)
                continue
            # Already at destination -> no entry needed
            best = (-1, -1, -1)
            best_d = math.inf
            for nb in g_isl.neighbors(curr):
                d_seg = g_isl.edges[(curr, nb)]["weight"]
                # numpy FW returns float, infinity if unreachable
                d_to = float(dist_sat_net[nb, dst_sat])
                if math.isinf(d_to):
                    continue
                total = d_seg + d_to
                if total < best_d:
                    best_d = total
                    best = (
                        nb,
                        sat_neighbor_to_if[(curr, nb)],
                        sat_neighbor_to_if[(nb, curr)],
                    )
            rows.append((curr, dst_sat, *best))

        # GSs as src
        for gid, in_range in enumerate(gs_in_range):
            gs_node_id = num_sats + gid
            best = (-1, -1, -1)
            best_total = math.inf
            for d_gsl, sid in in_range:
                if sid == dst_sat:
                    # GS adjacent to dst sat via GSL directly
                    total = d_gsl
                    if total < best_total:
                        best_total = total
                        best = (
                            sid,
                            0,                         # GS has only one GSL iface
                            num_isls_per_sat[sid],      # sat GSL iface sits after its ISL ifaces
                        )
                    continue
                d_to = float(dist_sat_net[sid, dst_sat])
                if math.isinf(d_to):
                    continue
                total = d_gsl + d_to
                if total < best_total:
                    best_total = total
                    best = (sid, 0, num_isls_per_sat[sid])
            rows.append((gs_node_id, dst_sat, *best))

    return rows


# --- Driver ------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state-dir", required=True,
                   help="Path to the gen_data/<network>/ directory")
    p.add_argument("--dynamic-state-dir", required=True,
                   help="Path to the dynamic_state_<int_ms>ms_for_<dur_s>s/ subdir")
    p.add_argument("--dst-sats", required=True,
                   help=("Either a comma-separated list of SAT ids, a single id, "
                         "or the literal 'all-compute' (requires --roles)."))
    p.add_argument("--roles", default=None,
                   help="Path to satellite_roles.txt (required iff --dst-sats=all-compute)")
    p.add_argument("--rewrite", action="store_true",
                   help="Strip previously-appended augment rows before appending fresh ones")
    p.add_argument("--max-timesteps", type=int, default=None,
                   help="Process at most N earliest timesteps (debug aid)")
    args = p.parse_args()

    state_dir = os.path.abspath(args.state_dir)
    dyn_dir = os.path.abspath(args.dynamic_state_dir)

    # Load static state -- once, reused across timesteps.
    print(f"loading state from {state_dir}")
    tles = read_tles(os.path.join(state_dir, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    list_isls = read_isls(os.path.join(state_dir, "isls.txt"), len(satellites))
    ground_stations = read_ground_stations_extended(
        os.path.join(state_dir, "ground_stations.txt")
    )
    description = exputil.PropertiesConfig(os.path.join(state_dir, "description.txt"))
    max_isl_length_m = exputil.parse_positive_float(
        description.get_property_or_fail("max_isl_length_m")
    )
    max_gsl_length_m = exputil.parse_positive_float(
        description.get_property_or_fail("max_gsl_length_m")
    )
    num_isls_per_sat, sat_neighbor_to_if = build_isl_metadata(list_isls, len(satellites))
    print(
        f"  satellites={len(satellites)}  GS={len(ground_stations)}  "
        f"ISLs={len(list_isls)}  max_isl={max_isl_length_m:.0f}m  max_gsl={max_gsl_length_m:.0f}m"
    )

    dst_sats = parse_dst_sats(args.dst_sats, args.roles, len(satellites))
    print(f"  routes will be added for {len(dst_sats)} dst sat(s): {dst_sats}")

    timesteps = discover_timesteps(dyn_dir)
    if args.max_timesteps:
        timesteps = timesteps[: args.max_timesteps]
    print(f"  {len(timesteps)} timesteps to process")

    for idx, t in enumerate(timesteps):
        fstate_path = os.path.join(dyn_dir, f"fstate_{t}.txt")
        if args.rewrite:
            strip_previous_augment(fstate_path)
        elif fstate_path_exists_with_augment(fstate_path):
            print(f"  [{idx + 1}/{len(timesteps)}] t={t} ns: already augmented, skipping")
            continue
        rows = compute_augment_rows(
            time_since_epoch_ns=t,
            epoch=epoch,
            satellites=satellites,
            ground_stations=ground_stations,
            list_isls=list_isls,
            num_isls_per_sat=num_isls_per_sat,
            sat_neighbor_to_if=sat_neighbor_to_if,
            max_isl_length_m=max_isl_length_m,
            max_gsl_length_m=max_gsl_length_m,
            dst_sats=dst_sats,
        )
        with open(fstate_path, "a") as f:
            f.write(f"# PHASE_A_AUGMENT begin: {len(rows)} rows for dst_sats={dst_sats}\n")
            for r in rows:
                f.write("%d,%d,%d,%d,%d\n" % r)
        print(f"  [{idx + 1}/{len(timesteps)}] t={t} ns: appended {len(rows)} SAT-dst rows")

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
