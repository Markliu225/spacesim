#!/usr/bin/env python3
"""
Pick a compute SAT geographically far from a given ground station, so the
TCP flow in the scenario run.sh exercises several ISL hops.

Strategy: at the flow's start_time (1 s after epoch), use SGP-4 to find
each compute sat's sub-satellite point, compute its great-circle
distance to the source GS, and pick the maximum.

Prints the chosen sat ID to stdout. Other info to stderr.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

_HERE = os.path.abspath(os.path.dirname(__file__))
_SATGENPY_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "satgenpy"))
if _SATGENPY_DIR not in sys.path:
    sys.path.insert(0, _SATGENPY_DIR)

from satgen.tles import read_tles  # noqa: E402
from satgen.ground_stations import read_ground_stations_extended  # noqa: E402
from satgen.distance_tools import distance_m_ground_station_to_satellite  # noqa: E402
from astropy import units as u  # noqa: E402

EARTH_R_M = 6371000.0


def read_roles(path: str) -> list[int]:
    """Return list of sat IDs marked 'C' in satellite_roles.txt."""
    compute: list[int] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            sid_str, role = line.split(",")
            if role.strip() == "C":
                compute.append(int(sid_str))
    return compute


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", required=True)
    p.add_argument("--roles", required=True)
    p.add_argument("--src-gs", type=int, default=0,
                   help="Ground-station GID (0-based) the flow starts from (default 0 = Tokyo).")
    p.add_argument("--start-time-ns", type=int, default=1_000_000_000)
    args = p.parse_args()

    tles = read_tles(os.path.join(args.state_dir, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    ground_stations = read_ground_stations_extended(
        os.path.join(args.state_dir, "ground_stations.txt"))
    src_gs = ground_stations[args.src_gs]

    print(f"src GS: id={args.src_gs} name={src_gs['name']} "
          f"lat={src_gs['latitude_degrees_str']} lon={src_gs['longitude_degrees_str']}",
          file=sys.stderr)

    compute_sats = read_roles(args.roles)
    print(f"compute sats: {len(compute_sats)}", file=sys.stderr)

    time = epoch + args.start_time_ns * u.ns
    epoch_str, time_str = str(epoch), str(time)

    # SGP-4 distance from this GS to each compute sat. The
    # `distance_m_ground_station_to_satellite` helper returns the slant
    # range (line-of-sight), but slant-range ordering is monotone with
    # the great-circle ground projection when altitude is fixed -- so
    # it's a fine proxy. Use slant range for simplicity / consistency
    # with augment_fstate.py.
    farthest = None
    farthest_d = -math.inf
    for sid in compute_sats:
        d = distance_m_ground_station_to_satellite(src_gs, satellites[sid], epoch_str, time_str)
        if d > farthest_d:
            farthest_d = d
            farthest = sid

    print(f"farthest compute sat from GS-{args.src_gs}: sat_id={farthest} "
          f"slant_range={farthest_d / 1000:.1f} km", file=sys.stderr)

    # Plane info
    plane = farthest // 22
    in_plane = farthest % 22
    print(f"  plane={plane}, in-plane index={in_plane}", file=sys.stderr)

    print(farthest)  # primary stdout output for downstream scripts
    return 0


if __name__ == "__main__":
    sys.exit(main())
