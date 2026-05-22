#!/usr/bin/env python3
"""
Augment Hypatia's per-timestep ``fstate_<t>.txt`` files with routes whose
destination is a satellite (not a ground station).

Why this exists
---------------

Hypatia's ``satgenpy/satgen/dynamic_state/fstate_calculation.py`` writes
forwarding-state entries only for ground-station destinations -- both
``calculate_fstate_shortest_path_without_gs_relaying`` and the
``with_gs_relaying`` variant iterate ``for dst_gid in range(num_ground_stations)``.
ns-3's arbiter (``arbiter-single-forward-helper.cc``) accepts SAT ids as
``target_node_id`` (the only check is ``target_node_id < m_nodes.GetN()``),
but if no row mentions a SAT as dst, the slot stays at the (-2,-2,-2)
"invalid" sentinel and packets get dropped.

For Phase A (LLM-on-satellite) we need exactly one new capability:
forwarding to a compute satellite. This script reads the state directory
produced by satgenpy and *appends* SAT-dst rows to every ``fstate_<t>.txt``
using the same shortest-path / interface conventions satgenpy uses.

Algorithm (mirrors fstate_calculation.py for the dst loop)
----------------------------------------------------------

For each timestep ``t`` already present in the dynamic-state dir:

1. Propagate the satellites to ``t`` (SGP-4 via pyephem inside satgen).
2. Build an ISL graph ``G_isl`` over satellites with edge weight = current
   inter-satellite distance, filtering edges by ``max_isl_length_m`` (same
   as satgenpy).
3. Compute ``ground_station_satellites_in_range[gid]`` = sorted list of
   ``(gsl_distance_m, sat_id)`` for sats within ``max_gsl_length_m``.
4. Floyd-Warshall on ``G_isl`` → ``dist_sat_net``.
5. For each ``dst_sat`` (user-supplied):
   - For each current node ``n``:
     - If ``n == dst_sat``: skip (self-delivery by the IPv4 stack).
     - If ``n`` is a satellite ``S``:
       Among ``S``'s ISL neighbours pick ``N*`` minimising
       ``ISL(S, N*) + dist_sat_net[N*, dst_sat]``.
       If unreachable, write a drop entry ``(-1, -1, -1)``.
     - If ``n`` is a GS ``G``:
       Among the satellites in range of ``G`` pick ``S*`` minimising
       ``gsl(G, S*) + dist_sat_net[S*, dst_sat]``.
6. Append the chosen rows to ``fstate_<t>.txt`` in the same 5-column CSV
   format satgenpy uses: ``current, dst, next_hop, my_if, next_if``.

Why this file is **comment-free**
---------------------------------

ns-3's ``arbiter-single-forward-helper.cc`` parses each line as
``split_string(line, ",", 5)`` and aborts on any line whose comma-split
isn't length 5 (including a line that starts with ``#``). So the augment
output goes straight into the file with **no marker line**. To record
which (dst_sat, t) pairs have been augmented, this script writes a
sidecar ``.phase_a_augment.json`` next to the fstate files. Detection of
"already augmented" reads that manifest first and falls back to a CSV
probe (``$2 == dst_sat``) for migration from old runs.

Notes
-----

- No delta encoding across time: each timestep gets the full set of
  (n → dst_sat) rows. 1 dst-sat × ~1684 src nodes × 50 timesteps ≈ 84k
  extra rows total -- a few MB, easy to manage. For Phase B+ with the
  full type-C set we may want delta encoding.
- ``--rewrite`` strips previously-added SAT-dst rows (by ``$2 in dst_sats``
  match) before appending fresh ones. Removes both manifest entries *and*
  the migrated comment-line residue ``^#`` from old runs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Dict, List, Set, Tuple

import networkx as nx


# --- Make satgen importable -------------------------------------------------

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


MANIFEST_FILENAME = ".phase_a_augment.json"


# --- Manifest helpers -------------------------------------------------------


def manifest_path(dyn_dir: str) -> str:
    return os.path.join(dyn_dir, MANIFEST_FILENAME)


def load_manifest(dyn_dir: str) -> Dict[int, List[int]]:
    """Return {dst_sat: sorted list of timesteps augmented}."""
    p = manifest_path(dyn_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    # JSON keys are strings; canonicalise back to int.
    return {int(k): sorted(int(t) for t in v) for k, v in raw.items()}


def save_manifest(dyn_dir: str, manifest: Dict[int, List[int]]) -> None:
    p = manifest_path(dyn_dir)
    with open(p, "w") as f:
        json.dump({str(k): sorted(set(int(t) for t in v))
                   for k, v in manifest.items()},
                  f, indent=2, sort_keys=True)


def manifest_has(manifest: Dict[int, List[int]], dst_sat: int, t: int) -> bool:
    return t in manifest.get(dst_sat, [])


# --- Static-state helpers ---------------------------------------------------


def discover_timesteps(dynamic_state_dir: str) -> List[int]:
    """List all fstate timestamps in dyn-dir, ascending."""
    pat = re.compile(r"^fstate_(\d+)\.txt$")
    out = []
    for name in os.listdir(dynamic_state_dir):
        m = pat.match(name)
        if m:
            out.append(int(m.group(1)))
    out.sort()
    return out


def build_isl_metadata(
    list_isls: List[Tuple[int, int]], num_satellites: int
) -> Tuple[List[int], Dict[Tuple[int, int], int]]:
    """Compute num_isls_per_sat and sat_neighbor_to_if, mirroring satgenpy.

    Interface indices are allocated in the order ISLs appear in isls.txt.
    The same ordering is used by ns-3 when it builds NetDevice indices,
    which is why fstate output must follow this convention exactly.
    """
    num_isls_per_sat = [0] * num_satellites
    sat_neighbor_to_if: Dict[Tuple[int, int], int] = {}
    for (a, b) in list_isls:
        sat_neighbor_to_if[(a, b)] = num_isls_per_sat[a]
        sat_neighbor_to_if[(b, a)] = num_isls_per_sat[b]
        num_isls_per_sat[a] += 1
        num_isls_per_sat[b] += 1
    return num_isls_per_sat, sat_neighbor_to_if


def parse_dst_sats(arg: str, roles_path: str | None, num_satellites: int) -> List[int]:
    """Resolve --dst-sats argument to a sorted list of sat IDs."""
    if arg == "all-compute":
        if not roles_path:
            raise SystemExit("--dst-sats=all-compute requires --roles")
        compute = []
        with open(roles_path) as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
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


# --- Strip helpers (migration + --rewrite) ----------------------------------


def strip_lines_for_dsts(fstate_path: str, dst_sats: Set[int]) -> int:
    """Strip rows where field-2 (dst) is in ``dst_sats``. Also strip any
    ``^#`` lines (residue from pre-fix augment_fstate.py runs).
    Returns the number of lines removed.
    """
    if not os.path.exists(fstate_path):
        return 0
    kept: List[str] = []
    removed = 0
    with open(fstate_path) as f:
        for line in f:
            s = line.rstrip("\n")
            if not s:
                kept.append(line)
                continue
            if s.startswith("#"):
                removed += 1
                continue
            parts = s.split(",", 2)
            if len(parts) >= 2:
                try:
                    dst_field = int(parts[1])
                except ValueError:
                    kept.append(line)
                    continue
                if dst_field in dst_sats:
                    removed += 1
                    continue
            kept.append(line)
    if removed:
        with open(fstate_path, "w") as f:
            f.writelines(kept)
    return removed


# --- Per-timestep computation -----------------------------------------------


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
    """Return the (curr, dst_sat, next_hop, my_if, next_if) rows.

    Mirrors satgenpy's ``calculate_fstate_shortest_path_without_gs_relaying``
    with the inner dst loop iterating over SAT IDs instead of GS IDs.
    """
    num_sats = len(satellites)
    time = epoch + time_since_epoch_ns * u.ns
    epoch_str, time_str = str(epoch), str(time)

    # ISL graph with current edge weights.
    g_isl = nx.Graph()
    for i in range(num_sats):
        g_isl.add_node(i)
    for (a, b) in list_isls:
        d = distance_m_between_satellites(
            satellites[a], satellites[b], epoch_str, time_str
        )
        if d > max_isl_length_m:
            raise RuntimeError(
                f"ISL ({a},{b}) length {d:.1f}m exceeds max "
                f"{max_isl_length_m:.1f}m at t={time_since_epoch_ns}ns -- "
                f"this would also crash satgenpy"
            )
        g_isl.add_edge(a, b, weight=d)

    dist_sat_net = nx.floyd_warshall_numpy(g_isl)

    # GS -> sats in range.
    gs_in_range: List[List[Tuple[float, int]]] = []
    for gs in ground_stations:
        in_range: List[Tuple[float, int]] = []
        for sid in range(num_sats):
            d = distance_m_ground_station_to_satellite(
                gs, satellites[sid], epoch_str, time_str
            )
            if d <= max_gsl_length_m:
                in_range.append((d, sid))
        in_range.sort()
        gs_in_range.append(in_range)

    rows: List[Tuple[int, int, int, int, int]] = []

    for dst_sat in dst_sats:
        # Satellites as src.
        for curr in range(num_sats):
            if curr == dst_sat:
                continue
            best = (-1, -1, -1)
            best_d = math.inf
            for nb in g_isl.neighbors(curr):
                d_seg = g_isl.edges[(curr, nb)]["weight"]
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

        # GSs as src.
        for gid, in_range in enumerate(gs_in_range):
            gs_node_id = num_sats + gid
            best = (-1, -1, -1)
            best_total = math.inf
            for d_gsl, sid in in_range:
                if sid == dst_sat:
                    total = d_gsl
                    if total < best_total:
                        best_total = total
                        best = (
                            sid,
                            0,                          # GS has 1 GSL iface
                            num_isls_per_sat[sid],      # sat GSL iface after ISLs
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


# --- Driver ----------------------------------------------------------------


def append_rows(fstate_path: str, rows: List[Tuple[int, int, int, int, int]]) -> None:
    """Append rows in the basic-sim 5-column CSV format. No comment line."""
    with open(fstate_path, "a") as f:
        for r in rows:
            f.write("%d,%d,%d,%d,%d\n" % r)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--state-dir", required=True,
                   help="Path to the gen_data/<network>/ directory")
    p.add_argument("--dynamic-state-dir", required=True,
                   help="Path to the dynamic_state_<int_ms>ms_for_<dur_s>s/ subdir")
    p.add_argument("--dst-sats", required=True,
                   help=("Comma-separated SAT ids, a single id, or the literal "
                         "'all-compute' (requires --roles)."))
    p.add_argument("--roles", default=None,
                   help="Path to satellite_roles.txt (needed for --dst-sats=all-compute)")
    p.add_argument("--rewrite", action="store_true",
                   help=("Strip any existing rows whose dst is in --dst-sats "
                         "(and any '^#' comment lines from old runs) before "
                         "re-appending."))
    p.add_argument("--max-timesteps", type=int, default=None,
                   help="Process at most N earliest timesteps (debug aid).")
    args = p.parse_args()

    state_dir = os.path.abspath(args.state_dir)
    dyn_dir = os.path.abspath(args.dynamic_state_dir)

    print(f"loading state from {state_dir}")
    tles = read_tles(os.path.join(state_dir, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    list_isls = read_isls(os.path.join(state_dir, "isls.txt"), len(satellites))
    ground_stations = read_ground_stations_extended(
        os.path.join(state_dir, "ground_stations.txt")
    )
    description = exputil.PropertiesConfig(
        os.path.join(state_dir, "description.txt")
    )
    max_isl_length_m = exputil.parse_positive_float(
        description.get_property_or_fail("max_isl_length_m"))
    max_gsl_length_m = exputil.parse_positive_float(
        description.get_property_or_fail("max_gsl_length_m"))
    num_isls_per_sat, sat_neighbor_to_if = build_isl_metadata(
        list_isls, len(satellites))
    print(
        f"  satellites={len(satellites)}  GS={len(ground_stations)}  "
        f"ISLs={len(list_isls)}  max_isl={max_isl_length_m:.0f}m  "
        f"max_gsl={max_gsl_length_m:.0f}m"
    )

    dst_sats = parse_dst_sats(args.dst_sats, args.roles, len(satellites))
    print(f"  routes will be added for {len(dst_sats)} dst sat(s): {dst_sats}")

    timesteps = discover_timesteps(dyn_dir)
    if args.max_timesteps:
        timesteps = timesteps[: args.max_timesteps]
    print(f"  {len(timesteps)} timesteps to process")

    manifest = load_manifest(dyn_dir)
    dst_set = set(dst_sats)

    processed = 0
    skipped = 0
    stripped_total = 0
    for idx, t in enumerate(timesteps):
        fstate_path = os.path.join(dyn_dir, f"fstate_{t}.txt")

        if args.rewrite:
            removed = strip_lines_for_dsts(fstate_path, dst_set)
            stripped_total += removed
            # And drop these (dst, t) from manifest -- they're being redone.
            for ds in dst_sats:
                if t in manifest.get(ds, []):
                    manifest[ds] = [x for x in manifest[ds] if x != t]
        else:
            # If every requested dst at this t is in the manifest, skip.
            if all(manifest_has(manifest, ds, t) for ds in dst_sats):
                skipped += 1
                print(f"  [{idx + 1}/{len(timesteps)}] t={t} ns: already in manifest, skipping")
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
        append_rows(fstate_path, rows)
        # Update manifest.
        for ds in dst_sats:
            manifest.setdefault(ds, [])
            if t not in manifest[ds]:
                manifest[ds].append(t)
            manifest[ds].sort()
        processed += 1
        print(f"  [{idx + 1}/{len(timesteps)}] t={t} ns: appended {len(rows)} SAT-dst rows")

    save_manifest(dyn_dir, manifest)
    print(f"manifest at {manifest_path(dyn_dir)}")
    print(
        f"summary: processed={processed} skipped={skipped} "
        f"stripped_rows={stripped_total} total_timesteps={len(timesteps)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())