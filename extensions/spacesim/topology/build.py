"""
Topology generator — wraps satgenpy + Phase A's fstate augmenter.

Given an :class:`ExperimentConfig`, produces a ready-to-use Hypatia
state directory and a ``satellite_roles.txt`` file, both placed under
``cache/<topology_hash>/``. Subsequent calls with the same shell config
(but different workload / compute / simulation knobs) hit the cache.

What's actually produced under ``cache/<hash>/``:

  ground_stations.basic.txt    # subset (top-N) of the upstream top-100 file
  state/<network_name>/
      tles.txt
      isls.txt
      ground_stations.txt
      gsl_interfaces_info.txt
      description.txt
      dynamic_state_<int_ms>ms_for_<dur_s>s/
          fstate_<t>.txt           ← already augmented for all compute SATs
          gsl_if_bandwidth_<t>.txt
          .phase_a_augment.json    ← manifest
  satellite_roles.txt

V1 limitations (deliberate — see README's "known limitations"):

  - Multi-shell: only the first shell is materialised. The schema supports
    a list, but the v1 generator picks ``config.shells[0]``. Documented
    upstream so the UI can show a warning if ``len(shells) > 1``.
  - Compute-sat selection: by-plane fingerprint with even spacing,
    targeting ``compute_ratio`` of the total satellites. Random / explicit
    plane lists are Phase B+ extensions.
  - GS set: derived from the bundled
    ``ground_stations_cities_sorted_by_estimated_2025_pop_top_100.basic.txt``
    by taking the first N rows. top_5 / top_20 / top_100 are the only
    presets surfaced in the UI.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

# --- Path setup ------------------------------------------------------------
#
# This file lives at ``extensions/spacesim/topology/build.py``. Two
# locations matter:
#   - upstream Hypatia's ``satgenpy/`` (importable as ``satgen.*``)
#   - the sibling ``fstate_augment.py`` invoked as a subprocess after
#     state generation, so SAT-dst routes get appended to every
#     ``fstate_<t>.txt``.

_THIS_FILE = Path(__file__).resolve()
_SPACESIM_DIR = _THIS_FILE.parent.parent                 # extensions/spacesim/
_HYPATIA_ROOT = _SPACESIM_DIR.parent.parent              # hypatia/
_SATGENPY_DIR = _HYPATIA_ROOT / "satgenpy"
_PAPER_INPUT_DATA = _HYPATIA_ROOT / "paper" / "satellite_networks_state" / "input_data"
_FSTATE_AUGMENT_SCRIPT = _SPACESIM_DIR / "topology" / "fstate_augment.py"

if str(_SATGENPY_DIR) not in sys.path:
    sys.path.insert(0, str(_SATGENPY_DIR))

import satgen  # noqa: E402

# Sibling module — same package — used here for its by-plane role
# assignment helpers (not via subprocess).
from . import roles as _roles_mod  # noqa: E402


# --- Public types ----------------------------------------------------------


ProgressFn = Callable[[str], None]


@dataclass
class TopologyResult:
    """What :func:`generate_topology` hands back to the caller."""

    state_dir: Path             # contains tles.txt, isls.txt, ground_stations.txt, etc.
    dynamic_state_dir: Path     # state_dir / "dynamic_state_<int_ms>ms_for_<dur_s>s"
    roles_path: Path            # satellite_roles.txt
    num_satellites: int
    num_ground_stations: int
    network_name: str           # used as `satellite_network_dir` basename
    cache_hit: bool


# --- Helpers ---------------------------------------------------------------


def _max_gsl_length_m(altitude_km: float, min_elevation_deg: float) -> float:
    """Max slant range of a satellite at altitude h above a GS at min elevation."""
    altitude_m = altitude_km * 1000.0
    cone_radius_m = altitude_m / math.tan(math.radians(min_elevation_deg))
    return math.sqrt(cone_radius_m ** 2 + altitude_m ** 2)


def _max_isl_length_m(
    altitude_km: float,
    num_planes: int,
    sats_per_plane: int,
) -> float:
    """Pick a ``max_isl_length_m`` that's compatible with the topology.

    Why this isn't just the classic "stay above 80 km atmosphere" formula:

    - For a dense Starlink-550-class constellation (72 × 22), the
      atmospheric floor (~5 016 km at h=550) is a sensible cap because
      adjacent +Grid neighbours are only ~2 000 km apart.
    - For sparse grids the geometric distance between cross-plane
      neighbours can exceed the atmospheric value. Even worse, Walker-Star's
      ``phase_diff=True`` puts the cross-plane sat at a *half-sat-spacing*
      anomaly offset, which combined with the inclination produces
      distances bigger than the naive ``2·(R+h)·sin(π/N_p)`` estimate.
      Computing the actual distance exactly requires running SGP-4 on
      both endpoints — what satgen is about to do anyway.
    - Earlier versions tried "use atmospheric floor unless geometry
      forces a relax". That branch passed for 10×10 (geometric estimate
      ≈ 4 277 km < atm 5 016 km), but the actual phase-shifted distance
      came out 5 713 km and satgen refused the graph.

    So the cap is always the orbital-diameter ceiling: any straight line
    between two satellites at this altitude is by construction
    ≤ ``2·(R+h)``. We park the cap at 99 % of that so distance checks
    still flag bona-fide gross errors. Trade-off: ISLs are *not*
    guaranteed to clear the 80 km atmospheric floor — the dashboard
    documents this in the README's "known limitations".
    """
    earth_r_m = 6_378_135.0
    altitude_m = altitude_km * 1000.0
    r_sat = earth_r_m + altitude_m
    return 2 * r_sat * 0.99


def _select_gs_basic_subset(
    gs_set: str, dst: Path, _progress: ProgressFn,
    *, gs_config_path: str = "",
) -> int:
    """Materialise a Hypatia-format ``ground_stations.basic.txt`` at
    ``dst`` and return the number of GS written.

    Two modes:

    - ``gs_config_path`` empty: take the first N rows of Hypatia's
      bundled top-100 file (``gs_set`` decides N). Backwards-compatible
      with the preset-only world.
    - ``gs_config_path`` non-empty: read a custom JSON file (same
      shape as ``traffic_generator/ground_stations.json``: a list of
      dicts with at least ``name``, ``lat``, ``lon``). Each row
      becomes one GS in the order it appears in the file; gs_idx is
      the list index (the JSON's own ``gs_idx`` is honoured if
      present but must equal the list index to keep ordering sane).
      ``elevation`` defaults to 0 m if missing.

    The Hypatia basic format is:
        ``<id>,<name>,<lat>,<lon>,<elev_m>``

    Sanity: empty / non-list / missing-required-key JSON raises with
    a clear message. The validation in :class:`ExperimentConfig`
    already catches these before reaching here, but we re-check to
    keep this helper standalone.
    """
    if gs_config_path:
        import json as _json
        with open(gs_config_path) as f:
            data = _json.load(f)
        if not isinstance(data, list) or not data:
            raise ValueError(
                f"gs_config_path {gs_config_path}: expected a non-empty "
                f"list of dicts, got {type(data).__name__}"
            )
        rows: List[str] = []
        for i, entry in enumerate(data):
            for key in ("name", "lat", "lon"):
                if key not in entry:
                    raise ValueError(
                        f"gs_config_path row {i}: missing required key "
                        f"{key!r} (have {sorted(entry.keys())})"
                    )
            name = str(entry["name"])
            lat = float(entry["lat"])
            lon = float(entry["lon"])
            elev_m = int(round(float(entry.get("elevation_m", 0))))
            # Hypatia parses lat/lon as floats so float repr is fine.
            rows.append(f"{i},{name},{lat},{lon},{elev_m}")
        with open(dst, "w") as f:
            for row in rows:
                f.write(row + "\n")
        return len(rows)

    src = _PAPER_INPUT_DATA / "ground_stations_cities_sorted_by_estimated_2025_pop_top_100.basic.txt"
    if not src.exists():
        raise FileNotFoundError(
            f"upstream GS file not found at {src}; cannot derive {gs_set}"
        )
    counts = {"top_5_cities": 5, "top_20_cities": 20, "top_100_cities": 100}
    if gs_set not in counts:
        raise ValueError(f"unknown gs_set {gs_set!r}; choices: {sorted(counts)}")
    n = counts[gs_set]

    rows = []
    with open(src) as f:
        for line in f:
            if not line.strip():
                continue
            rows.append(line.rstrip("\n"))
            if len(rows) >= n:
                break
    if len(rows) < n:
        raise RuntimeError(
            f"upstream GS file at {src} only has {len(rows)} rows, need {n}"
        )
    with open(dst, "w") as f:
        for row in rows:
            f.write(row + "\n")
    return n


def _assign_compute_planes(num_planes: int, ratio: float) -> List[int]:
    """Pick the planes whose satellites will be marked compute.

    Returns a sorted list of plane indices. Strategy: evenly space
    ``round(ratio * num_planes)`` planes around the ring. Always at
    least 1 plane.
    """
    k = max(1, round(ratio * num_planes))
    # Even spacing: pick every (num_planes / k)-th index, rounded.
    step = num_planes / k
    planes = sorted({int(round(i * step)) % num_planes for i in range(k)})
    return planes


def _write_roles(
    output_path: Path, total_sats: int, sats_per_plane: int, compute_planes: List[int]
) -> int:
    """Write satellite_roles.txt; returns count of compute sats."""
    roles = _roles_mod.assign_by_plane(
        sats_per_plane=sats_per_plane,
        num_planes=total_sats // sats_per_plane,
        planes=compute_planes,
    )
    _roles_mod.write_roles(str(output_path), roles)
    return roles.count("C")


def _generate_dynamic_state_and_augment(
    *,
    config,
    state_dir: Path,
    dyn_dir: Path,
    roles_path: Path,
    network_name: str,
    max_gsl: float,
    max_isl: float,
    progress: ProgressFn,
) -> None:
    """Generate fstate_<t>.txt + gsl_if_bandwidth_<t>.txt and then
    augment them with compute-SAT-dst routes.

    Shared between the full-rebuild path and the cache-partial-hit
    path (where only ``duration_seconds`` / ``update_interval_ms``
    changed and we need a new dyn_subdir but the static state is
    fine).
    """
    # satgenpy/satgen/dynamic_state/generate_dynamic_state.py ~line 59
    # does `i % int(math.floor(total_iterations) / 10.0)`. With < 10
    # iterations per thread the divisor is 0 and the worker raises
    # ZeroDivisionError. We can't patch satgenpy here, so (a) compute
    # the iteration count, (b) pick num_threads so each thread runs
    # ≥ 10 iters, (c) bail with a clear error below 10 total.
    total_iter = max(
        1,
        int(round(
            config.simulation.duration_seconds * 1000
            / max(config.simulation.update_interval_ms, 1)
        )),
    )
    if total_iter < 10:
        raise ValueError(
            f"satgenpy needs ≥ 10 fstate timesteps to avoid a "
            f"divide-by-zero in its progress logger; current config "
            f"produces only {total_iter}. Either raise "
            f"simulation.duration_seconds (currently "
            f"{config.simulation.duration_seconds}s) or lower "
            f"simulation.update_interval_ms (currently "
            f"{config.simulation.update_interval_ms}ms)."
        )
    num_threads = max(1, min(2, total_iter // 10))
    progress(
        f"generating fstate for {config.simulation.duration_seconds}s @ "
        f"{config.simulation.update_interval_ms}ms "
        f"({total_iter} timesteps, {num_threads} thread(s))"
    )
    # satgen.help_dynamic_state mutates cwd to find state files by
    # network name, so chdir to state_dir.parent first.
    cwd_was = os.getcwd()
    try:
        os.chdir(state_dir.parent)   # state/
        satgen.help_dynamic_state(
            ".",
            num_threads,
            network_name,
            config.simulation.update_interval_ms,
            config.simulation.duration_seconds,
            max_gsl,
            max_isl,
            "algorithm_free_one_only_over_isls",
            False,                            # verbose
        )
    finally:
        os.chdir(cwd_was)

    # Augment for compute-SAT destinations.
    _run_augment_fstate(state_dir, dyn_dir, roles_path, progress)


def _regenerate_dynamic_state(
    *,
    config,
    state_dir: Path,
    dyn_dir: Path,
    roles_path: Path,
    network_name: str,
    progress: ProgressFn,
) -> None:
    """Cache-partial-hit entry. Re-derive max_gsl/max_isl from the
    cached shell config and call the shared generator.
    """
    shell = config.shells[0]
    max_gsl = _max_gsl_length_m(shell.altitude_km, shell.min_elevation_deg)
    max_isl = _max_isl_length_m(
        shell.altitude_km, shell.num_planes, shell.sats_per_plane,
    )
    _generate_dynamic_state_and_augment(
        config=config, state_dir=state_dir, dyn_dir=dyn_dir,
        roles_path=roles_path, network_name=network_name,
        max_gsl=max_gsl, max_isl=max_isl, progress=progress,
    )


def _run_augment_fstate(
    state_dir: Path,
    dynamic_state_dir: Path,
    roles_path: Path,
    on_progress: ProgressFn,
) -> None:
    """Invoke ``topology/fstate_augment.py`` via subprocess.

    Adds SAT-dst forwarding rows for every type=C satellite to every
    ``fstate_<t>.txt`` in ``dynamic_state_dir``. Uses the role file to
    drive ``--dst-sats=all-compute``.

    Subprocess (not import) because (a) ``fstate_augment`` rebuilds the
    satgenpy graph from scratch every call and that's expensive and
    deserves its own process, (b) it then writes its own SAT-dst rows
    to fstate files which is side-effect-heavy and easier to isolate
    from this driver.
    """
    cmd = [
        sys.executable,
        str(_FSTATE_AUGMENT_SCRIPT),
        "--state-dir", str(state_dir),
        "--dynamic-state-dir", str(dynamic_state_dir),
        "--dst-sats", "all-compute",
        "--roles", str(roles_path),
        "--rewrite",
    ]
    on_progress(f"running augment_fstate: {' '.join(cmd[1:])}")
    # 30-minute hard cap; if we hit this we have a much bigger problem.
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if res.returncode != 0:
        raise RuntimeError(
            f"augment_fstate.py exited {res.returncode}\n"
            f"--- stdout (last 30 lines) ---\n"
            + "\n".join(res.stdout.splitlines()[-30:])
            + f"\n--- stderr (last 30 lines) ---\n"
            + "\n".join(res.stderr.splitlines()[-30:])
        )
    on_progress("augment_fstate complete")


# --- Main entry point ------------------------------------------------------


def generate_topology(
    config,
    cache_root: Path,
    *,
    on_progress: Optional[ProgressFn] = None,
    force_regenerate: bool = False,
) -> TopologyResult:
    """Materialise a Hypatia state dir for the given config.

    Args:
        config: :class:`ExperimentConfig`. Only ``shells[0]`` and
            ``simulation`` are honoured for state generation.
        cache_root: parent dir of per-hash caches (``dashboard/cache/``).
        on_progress: optional status callback (one-line strings).
        force_regenerate: skip the cache hit and rebuild from scratch.

    Returns:
        :class:`TopologyResult` with paths and metadata.
    """
    progress = on_progress or (lambda _msg: None)

    if not config.shells:
        raise ValueError("config has no shells")
    shell = config.shells[0]
    if len(config.shells) > 1:
        progress(f"WARNING: only shell[0] materialised (v1 limit); "
                 f"{len(config.shells) - 1} shell(s) ignored")

    topo_hash = config.topology_hash()
    out_root = cache_root / topo_hash
    network_name = (
        f"dashboard_h{int(shell.altitude_km)}_i{int(shell.inclination_deg)}"
        f"_n{shell.num_planes}x{shell.sats_per_plane}_{topo_hash[:6]}"
    )
    state_dir = out_root / "state" / network_name
    dyn_subdir = (
        f"dynamic_state_{config.simulation.update_interval_ms}ms_"
        f"for_{config.simulation.duration_seconds}s"
    )
    dyn_dir = state_dir / dyn_subdir
    roles_path = out_root / "satellite_roles.txt"
    sentinel = out_root / ".dashboard_topology_ok"

    if sentinel.exists() and not force_regenerate:
        # topology_hash only covers `shells`. Changing simulation.duration
        # or update_interval_ms re-uses the cached TLEs/ISLs but needs a
        # NEW dynamic_state directory (its name encodes both numbers).
        # If the requested dyn_dir doesn't have an `fstate_0.txt`, fall
        # through to a partial regen: keep tles/isls/gsl/roles, just
        # re-build dynamic_state for the new (duration, interval) pair.
        dyn_fstate_zero = dyn_dir / "fstate_0.txt"
        if dyn_fstate_zero.exists():
            progress(f"cache hit: {out_root}")
            return TopologyResult(
                state_dir=state_dir,
                dynamic_state_dir=dyn_dir,
                roles_path=roles_path,
                num_satellites=shell.total_sats,
                num_ground_stations=int(_count_lines(roles_path.parent / "ground_stations.basic.txt")),
                network_name=network_name,
                cache_hit=True,
            )
        progress(
            f"cache partial hit: tles/isls present but dyn_state "
            f"({dyn_subdir}) missing. Regenerating dynamic state only."
        )
        # Skip ahead to step 7 below by calling the shared helper.
        _regenerate_dynamic_state(
            config=config, state_dir=state_dir,
            dyn_dir=dyn_dir, roles_path=roles_path,
            network_name=network_name, progress=progress,
        )
        return TopologyResult(
            state_dir=state_dir,
            dynamic_state_dir=dyn_dir,
            roles_path=roles_path,
            num_satellites=shell.total_sats,
            num_ground_stations=int(_count_lines(roles_path.parent / "ground_stations.basic.txt")),
            network_name=network_name,
            cache_hit=True,   # tles/isls came from cache; only fstate is fresh
        )

    if force_regenerate and out_root.exists():
        progress(f"force_regenerate: wiping {out_root}")
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    # 1. Ground stations (basic format), then extend with ECEF.
    progress(f"selecting GS subset: {config.workload.gs_set}")
    gs_basic_path = out_root / "ground_stations.basic.txt"
    num_gs = _select_gs_basic_subset(
        config.workload.gs_set, gs_basic_path, progress,
        gs_config_path=config.workload.gs_config_path,
    )
    progress(f"  -> {num_gs} ground stations")

    progress("extending GS with ECEF coordinates")
    satgen.extend_ground_stations(
        str(gs_basic_path),
        str(state_dir / "ground_stations.txt"),
    )

    # 2. TLEs.
    progress(f"generating TLEs for {shell.total_sats} satellites")
    satgen.generate_tles_from_scratch_manual(
        str(state_dir / "tles.txt"),
        network_name,
        shell.num_planes,
        shell.sats_per_plane,
        True,                                # phase_diff (Walker-Star)
        shell.inclination_deg,
        0.0000001,                           # eccentricity (~circular)
        0.0,                                 # arg of perigee
        shell.mean_motion_rev_per_day,
    )

    # 3. ISLs (+grid).
    progress(f"generating +grid ISLs for {shell.num_planes}x{shell.sats_per_plane}")
    satgen.generate_plus_grid_isls(
        str(state_dir / "isls.txt"),
        shell.num_planes,
        shell.sats_per_plane,
        isl_shift=0,
        idx_offset=0,
    )

    # 4. Description (geometric limits).
    max_gsl = _max_gsl_length_m(shell.altitude_km, shell.min_elevation_deg)
    max_isl = _max_isl_length_m(
        shell.altitude_km, shell.num_planes, shell.sats_per_plane,
    )
    progress(f"description: max_gsl={max_gsl:.0f}m, max_isl={max_isl:.0f}m")
    satgen.generate_description(
        str(state_dir / "description.txt"),
        max_gsl,
        max_isl,
    )

    # 5. GSL interface info. 1 GSL per sat, 1 GSL per GS, unit bw.
    progress("generating GSL interface info")
    satgen.generate_simple_gsl_interfaces_info(
        str(state_dir / "gsl_interfaces_info.txt"),
        shell.total_sats,
        num_gs,
        1, 1, 1, 1,
    )

    # 6. satellite_roles.txt (drives both the C++ patch and augment_fstate).
    compute_planes = _assign_compute_planes(shell.num_planes, shell.compute_ratio)
    num_compute = _write_roles(
        roles_path, shell.total_sats, shell.sats_per_plane, compute_planes
    )
    progress(
        f"roles: {num_compute}/{shell.total_sats} compute sats "
        f"(planes {compute_planes})"
    )

    # 7+8. Dynamic state + augment fstate. Shared with the
    # "cache-partial-hit" path above (when only the duration / interval
    # changed and we need a different dyn_subdir).
    _generate_dynamic_state_and_augment(
        config=config, state_dir=state_dir, dyn_dir=dyn_dir,
        roles_path=roles_path, network_name=network_name,
        max_gsl=max_gsl, max_isl=max_isl, progress=progress,
    )

    # Mark cache valid.
    sentinel.touch()
    progress(f"topology cached at {out_root}")

    return TopologyResult(
        state_dir=state_dir,
        dynamic_state_dir=dyn_dir,
        roles_path=roles_path,
        num_satellites=shell.total_sats,
        num_ground_stations=num_gs,
        network_name=network_name,
        cache_hit=False,
    )


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path) as f:
        return sum(1 for _ in f)
