#!/usr/bin/env python3
"""
Build a small Walker-Star LEO constellation suitable for a Phase A E2E
smoke test. The point is to exercise the full pipeline (state-gen ->
augment_fstate -> ns-3 multi-flow -> verify) end-to-end with a topology
that is *small enough to be inspectable* but *real enough to involve
multiple ISL hops and GS handovers*.

Constellation
-------------

  - 4 orbital planes x 10 satellites per plane = 40 satellites
  - 550 km altitude (Starlink-ish)
  - 53 degree inclination
  - Circular orbits (epsilon = 1e-7)
  - Walker-Star phasing across planes
  - Grid ISLs: each sat is connected to +1 / -1 in-plane neighbour and
    the "above" / "below" sat in the adjacent plane (4 ISLs per sat)

  The "10 per plane" lower bound is geometric: at 550 km altitude the
  orbital radius is 6928 km, so two satellites at in-plane angular
  separation theta are 2 * 6928 * sin(theta/2) km apart. Five per plane
  produces a ~8100 km ISL which satgenpy correctly rejects with
  ValueError. Even 10 per plane (cross-plane at 45 deg plane spacing)
  exceeds the 550-km-altitude geometric maximum, so we follow the same
  trick Hypatia's legacy ``main_25x25.py`` uses: set
  ``MAX_ISL_LENGTH_M`` to a huge value (effectively disabling the
  length check). Hypatia models ISL propagation delay as `distance / c`
  regardless, so the simulation is still self-consistent -- it's just
  that some declared ISLs are physically over-the-horizon. For a
  pipeline smoke test that's fine.

Ground stations
---------------

  Five real cities (subset of Hypatia's top-100 list):
    GID 0 = Tokyo,         node id = 40
    GID 1 = Delhi,         node id = 41
    GID 2 = Shanghai,      node id = 42
    GID 3 = Sao-Paulo,     node id = 43
    GID 4 = New-York-Newark, node id = 44

Output
------

  ``scenarios/mixed_topology/gen_data/<network_name>/``
    tles.txt
    isls.txt
    ground_stations.txt
    gsl_interfaces_info.txt
    description.txt
    dynamic_state_<int_ms>ms_for_<dur_s>s/
      fstate_<t>.txt
      gsl_if_bandwidth_<t>.txt

Run
---

  python build_state.py        # builds with defaults (5 s sim, 100 ms interval)
  python build_state.py -d 10 -i 100 -j 2
"""

from __future__ import annotations

import argparse
import math
import os
import sys

# satgenpy must be importable.
_HERE = os.path.abspath(os.path.dirname(__file__))
_PHASE_A_DIR = os.path.abspath(os.path.join(_HERE, "..", ".."))
_HYPATIA_ROOT = os.path.abspath(os.path.join(_PHASE_A_DIR, "..", ".."))
_SATGENPY = os.path.join(_HYPATIA_ROOT, "satgenpy")
for p in (_SATGENPY,):
    if p not in sys.path:
        sys.path.insert(0, p)

import satgen  # noqa: E402


# --- Constellation parameters ----------------------------------------------

BASE_NAME = "tiny_walker_1500"
NICE_NAME = "Tiny-Walker-1500"

# Earth WGS-72 radius (consistent with Hypatia's main_*.py scripts).
EARTH_RADIUS_M = 6378135.0
# Altitude 1500 km: at 30 deg elevation the GSL cone has a 2598 km
# ground radius, so each sat covers ~21 M km^2. With 60 sats that gives
# ~1.7x Earth-surface coverage -- enough redundancy that every GS in
# the +-53 deg band sees at least one sat almost always at epoch.
ALTITUDE_M = 1_500_000
INCLINATION_DEGREE = 53.0
ECCENTRICITY = 1e-7
ARG_OF_PERIGEE_DEGREE = 0.0
PHASE_DIFF = True
# 12.67 rev/day at 1500 km. (Period = 2*pi*sqrt(R_orb^3 / mu); mu = 398600 km^3/s^2)
MEAN_MOTION_REV_PER_DAY = 12.67

NUM_ORBS = 6
NUM_SATS_PER_ORB = 10

# Min ground-station elevation = 30 degrees (Hypatia default for 550 km shell).
SATELLITE_CONE_RADIUS_M = ALTITUDE_M / math.tan(math.radians(30.0))
MAX_GSL_LENGTH_M = math.sqrt(SATELLITE_CONE_RADIUS_M ** 2 + ALTITUDE_M ** 2)

# Disable satgenpy's per-timestep ISL length check by setting a huge
# threshold (the legacy trick used by Hypatia's main_25x25.py). Hypatia
# always uses the real `distance / c` for propagation delay, so this
# only affects which ISLs satgenpy *declares*, not the simulation
# physics. With only 4 planes at 550 km some declared ISLs will be
# over-the-horizon -- acceptable for a pipeline smoke test.
MAX_ISL_LENGTH_M = 1_000_000_000


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-d", "--duration-s", type=int, default=5,
                   help="simulation duration in seconds (default 5)")
    p.add_argument("-i", "--interval-ms", type=int, default=100,
                   help="dynamic-state update interval in ms (default 100)")
    p.add_argument("-j", "--threads", type=int, default=2,
                   help="state-gen worker threads (default 2)")
    p.add_argument(
        "--algorithm",
        default="algorithm_free_one_only_over_isls",
        choices=("algorithm_free_one_only_over_isls",
                 "algorithm_free_one_only_gs_relays"),
    )
    args = p.parse_args()

    gen_data_dir = os.path.join(_HERE, "gen_data")
    name = f"{BASE_NAME}_isls_plus_grid_5cities_{args.algorithm}"
    out_dir = os.path.join(gen_data_dir, name)
    os.makedirs(out_dir, exist_ok=True)
    print(f"output: {out_dir}")
    print(f"constellation: {NUM_ORBS} planes x {NUM_SATS_PER_ORB} sats = "
          f"{NUM_ORBS * NUM_SATS_PER_ORB} satellites at {ALTITUDE_M/1000:.0f} km, "
          f"{INCLINATION_DEGREE}deg")
    print(f"max_gsl={MAX_GSL_LENGTH_M:.0f} m  max_isl={MAX_ISL_LENGTH_M:.0f} m")

    # Ground stations.
    print("[1/6] ground stations")
    satgen.extend_ground_stations(
        os.path.join(_HERE, "input_data", "ground_stations.basic.txt"),
        os.path.join(out_dir, "ground_stations.txt"),
    )

    # TLEs.
    print("[2/6] TLEs (Walker-Star)")
    satgen.generate_tles_from_scratch_manual(
        os.path.join(out_dir, "tles.txt"),
        NICE_NAME,
        NUM_ORBS,
        NUM_SATS_PER_ORB,
        PHASE_DIFF,
        INCLINATION_DEGREE,
        ECCENTRICITY,
        ARG_OF_PERIGEE_DEGREE,
        MEAN_MOTION_REV_PER_DAY,
    )

    # ISLs (grid).
    print("[3/6] ISLs (plus-grid)")
    satgen.generate_plus_grid_isls(
        os.path.join(out_dir, "isls.txt"),
        NUM_ORBS,
        NUM_SATS_PER_ORB,
        isl_shift=0,
        idx_offset=0,
    )

    # Description.
    print("[4/6] description")
    satgen.generate_description(
        os.path.join(out_dir, "description.txt"),
        MAX_GSL_LENGTH_M,
        MAX_ISL_LENGTH_M,
    )

    # GSL interface info. algorithm_free_one_only_over_isls implies 1 GSL/sat.
    print("[5/6] GSL interfaces info")
    ground_stations = satgen.read_ground_stations_extended(
        os.path.join(out_dir, "ground_stations.txt")
    )
    satgen.generate_simple_gsl_interfaces_info(
        os.path.join(out_dir, "gsl_interfaces_info.txt"),
        NUM_ORBS * NUM_SATS_PER_ORB,
        len(ground_stations),
        1,        # gsl_interfaces_per_satellite
        1,        # gsl_interfaces_per_ground_station
        1.0,      # aggregate max bandwidth satellite (unit-less)
        1.0,      # aggregate max bandwidth ground station
    )

    # Dynamic forwarding state.
    print(f"[6/6] dynamic state: {args.duration_s} s @ {args.interval_ms} ms")
    satgen.help_dynamic_state(
        gen_data_dir,
        args.threads,
        name,
        args.interval_ms,
        args.duration_s,
        MAX_GSL_LENGTH_M,
        MAX_ISL_LENGTH_M,
        args.algorithm,
        True,  # enable_verbose_logs
    )

    print()
    print(f"done. state at {out_dir}")
    print(f"network name: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())