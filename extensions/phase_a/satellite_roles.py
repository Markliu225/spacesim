#!/usr/bin/env python3
"""
Generate a satellite_roles.txt for a Hypatia constellation.

Each row is `<sat_id>,<C|T>` where C=compute, T=transit. Sat IDs are
0-indexed and match the order of TLE entries in tles.txt (which is the
same as the ns-3 node-id convention for satellites).

Two role-assignment strategies are supported:

  - by_plane: mark every satellite in the listed orbital planes as
    compute. Plane k contains sat IDs [k * sats_per_plane,
    (k + 1) * sats_per_plane). This is the default and matches the
    Walker-Star layout produced by Hypatia's main_*.py generators.

  - random: pick `int(ratio * N)` sat IDs uniformly at random with
    the given seed.

Usage examples
--------------

    # Default: Starlink-550 first shell, 8 evenly-spaced planes -> 11.1 %
    python satellite_roles.py \\
        --tles <state_dir>/tles.txt \\
        --output satellite_roles.txt

    # Random 10 % with seed 42
    python satellite_roles.py \\
        --tles <state_dir>/tles.txt \\
        --strategy random --ratio 0.10 --seed 42 \\
        --output satellite_roles.txt

    # by_plane with custom plane list
    python satellite_roles.py \\
        --tles <state_dir>/tles.txt \\
        --strategy by_plane --planes 0,12,24,36,48,60 \\
        --output satellite_roles.txt
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from typing import Iterable, List, Tuple


def read_tles_header(tles_path: str) -> Tuple[int, int, int]:
    """Return (num_orbital_planes, sats_per_plane, total_sat_count).

    Hypatia's tles.txt first line is `<num_planes> <sats_per_plane>`.
    Total satellite count equals their product, and the remaining lines
    are groups of three (name + 2 TLE lines).
    """
    with open(tles_path) as f:
        header = f.readline().split()
        if len(header) != 2:
            raise SystemExit(
                f"tles.txt header malformed: expected `<planes> <sats_per_plane>`, "
                f"got {header!r}"
            )
        num_planes = int(header[0])
        sats_per_plane = int(header[1])
        total = num_planes * sats_per_plane
        # Sanity-check against actual line count
        remaining = sum(1 for _ in f)
        if remaining != 3 * total:
            print(
                f"WARNING: tles.txt declared {num_planes}*{sats_per_plane}={total} "
                f"sats but body has {remaining} lines (expected {3 * total}).",
                file=sys.stderr,
            )
    return num_planes, sats_per_plane, total


def assign_by_plane(
    sats_per_plane: int, num_planes: int, planes: Iterable[int]
) -> List[str]:
    """Return a list of 'C' or 'T' of length num_planes*sats_per_plane."""
    planes_set = set(int(p) for p in planes)
    bad = [p for p in planes_set if p < 0 or p >= num_planes]
    if bad:
        raise SystemExit(
            f"planes {sorted(bad)} out of range [0, {num_planes})"
        )
    total = num_planes * sats_per_plane
    roles = ["T"] * total
    for p in planes_set:
        for j in range(sats_per_plane):
            roles[p * sats_per_plane + j] = "C"
    return roles


def assign_random(total: int, ratio: float, seed: int) -> List[str]:
    if not 0.0 < ratio < 1.0:
        raise SystemExit(f"--ratio must be in (0, 1), got {ratio}")
    rng = random.Random(seed)
    k = int(round(ratio * total))
    compute_ids = set(rng.sample(range(total), k))
    return ["C" if i in compute_ids else "T" for i in range(total)]


def write_roles(path: str, roles: List[str]) -> None:
    with open(path, "w") as f:
        for sid, r in enumerate(roles):
            f.write(f"{sid},{r}\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--tles", required=True, help="Path to tles.txt")
    p.add_argument("--output", required=True, help="Path to satellite_roles.txt")
    p.add_argument(
        "--strategy",
        choices=("by_plane", "random"),
        default="by_plane",
    )
    p.add_argument(
        "--planes",
        default="0,8,16,24,32,40,48,56",
        help=(
            "by_plane only: comma-separated plane indices to mark as compute. "
            "Default targets ~11%% of Starlink-550 first shell (72 planes)."
        ),
    )
    p.add_argument(
        "--ratio",
        type=float,
        default=0.10,
        help="random only: fraction of satellites to mark compute (default 0.10)",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="random only: PRNG seed for reproducibility (default 42)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    num_planes, sats_per_plane, total = read_tles_header(args.tles)
    print(
        f"constellation from {args.tles}: {num_planes} planes x "
        f"{sats_per_plane} sats = {total} satellites"
    )

    if args.strategy == "by_plane":
        planes = [int(p) for p in args.planes.split(",") if p.strip()]
        roles = assign_by_plane(sats_per_plane, num_planes, planes)
        print(
            f"strategy=by_plane, planes={planes} "
            f"-> {roles.count('C')} compute sats ({roles.count('C') / total * 100:.1f}%)"
        )
    else:
        roles = assign_random(total, args.ratio, args.seed)
        print(
            f"strategy=random ratio={args.ratio} seed={args.seed} "
            f"-> {roles.count('C')} compute sats ({roles.count('C') / total * 100:.1f}%)"
        )

    write_roles(args.output, roles)
    print(f"wrote {args.output} ({len(roles)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
