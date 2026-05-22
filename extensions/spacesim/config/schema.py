"""
Config schema for the LLM-on-satellite dashboard.

All knobs the user can twist in the sidebar map onto one of these
``@dataclass`` types. The top-level :class:`ExperimentConfig` aggregates
the four sub-configs and is the only thing that gets stored in
``st.session_state`` / serialised to disk.

Two operations are non-trivial and worth flagging:

- :meth:`ExperimentConfig.hash` returns a stable hex digest of *only* the
  shell list. Topology generation (the expensive step) caches its output
  under this digest, so re-running with a different workload or compute
  model reuses the cached constellation.

- :meth:`ExperimentConfig.to_json` / :meth:`from_json` round-trip the
  config to JSON. Used by the Save/Load buttons.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal


# ---------- Sub-configs ----------------------------------------------------


@dataclass
class ShellConfig:
    """One orbital shell. Walker-Star layout, +Grid ISLs."""

    altitude_km: float = 550.0
    inclination_deg: float = 53.0
    num_planes: int = 10
    sats_per_plane: int = 10
    phase_offset: int = 0
    min_elevation_deg: float = 25.0
    isl_pattern: Literal["+Grid"] = "+Grid"
    # Fraction of satellites tagged as compute (C). With 10 planes and
    # the default plane-based assignment, 0.20 → 2 compute planes (20
    # compute SATs), enough to absorb a 5-GS trace without queue runaway.
    compute_ratio: float = 0.20  # fraction in (0, 1]

    @property
    def total_sats(self) -> int:
        return self.num_planes * self.sats_per_plane

    @property
    def mean_motion_rev_per_day(self) -> float:
        """Derive Walker-Star mean motion from altitude.

        Uses the standard circular-orbit formula T = 2π √(a³ / μ) and
        converts to revolutions per day. μ for Earth = 3.986e14 m³/s².
        """
        import math
        mu = 3.986004418e14
        r_earth_m = 6_378_135.0
        a = r_earth_m + self.altitude_km * 1_000.0
        period_s = 2 * math.pi * math.sqrt(a ** 3 / mu)
        return 86_400.0 / period_s


@dataclass
class WorkloadConfig:
    """LLM request stream parameters, fed straight into the schedule CSV.

    Two ``source`` modes:

    - ``"synthetic"`` (default): the dashboard writes a 15-column Phase-C
      schedule and ns-3 generates each GS's request stream from the
      Poisson(λ_total / N_gs) + Normal(L_in_mean, L_in_std) sample. All
      L_in / λ fields below are honored.
    - ``"trace_replay"``: a per-GS events CSV (produced by
      ``extensions/traffic_generator``) is staged into the run dir and
      the simulator replays every emit time and L_in verbatim. The λ /
      L_in_mean / L_in_std fields are then ignored; only the L_in clamps
      and the L_out distribution still matter.
    """

    gs_set: Literal["top_5_cities", "top_20_cities", "top_100_cities"] = "top_5_cities"
    source: Literal["synthetic", "trace_replay"] = "synthetic"
    # Path to a per_gs/ directory containing events_gs<N>_*.csv files
    # (relative or absolute). Only consulted when source == "trace_replay".
    trace_per_gs_dir: str = ""
    # How to stage the events files into the run dir.
    trace_stage_mode: Literal["copy", "symlink"] = "copy"
    # Where in the trace to start. The trace covers e.g. 24h; the sim
    # only runs `duration_seconds`. Setting offset=50400 (14:00) replays
    # the local-afternoon-peak slice. Default 0 = trace beginning.
    trace_start_offset_sec: float = 0.0
    # GS → compute-SAT routing policy. Defaults to round-robin so a
    # trace with many GS doesn't queue up on a single compute SAT.
    dst_strategy: Literal["first_compute", "per_gs_round_robin"] = "per_gs_round_robin"

    lambda_total: float = 10.0  # requests / second, summed across GS
    L_in_mean: float = 500.0    # prompt tokens
    L_in_std: float = 100.0
    L_in_min: int = 1
    L_in_max: int = 2000
    L_out_mean: float = 200.0   # response tokens
    L_out_std: float = 50.0
    L_out_min: int = 1
    L_out_max: int = 1000
    bytes_per_token: int = 4
    packet_payload: int = 1400


@dataclass
class ComputeConfig:
    """LLM inference cost model: T_compute = α·L_in + β·L_out + γ."""

    alpha_ns_per_input_token: int = 100_000
    beta_ns_per_output_token: int = 50_000
    gamma_ns: int = 10_000_000


@dataclass
class SimulationConfig:
    duration_seconds: int = 30
    epoch_iso: str = "2024-01-01T00:00:00Z"
    update_interval_ms: int = 1000


# ---------- Top-level ------------------------------------------------------


@dataclass
class ExperimentConfig:
    """The whole sidebar in one object."""

    shells: List[ShellConfig] = field(default_factory=lambda: [ShellConfig()])
    workload: WorkloadConfig = field(default_factory=WorkloadConfig)
    compute: ComputeConfig = field(default_factory=ComputeConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)

    # ---- serialization --------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperimentConfig":
        """Be tolerant of missing keys (use defaults) and unknown keys (ignore)."""
        shells = [ShellConfig(**{k: v for k, v in s.items()
                                 if k in ShellConfig.__dataclass_fields__})
                  for s in d.get("shells", [{}])]
        wl_dict = d.get("workload", {})
        cmp_dict = d.get("compute", {})
        sim_dict = d.get("simulation", {})
        return cls(
            shells=shells or [ShellConfig()],
            workload=WorkloadConfig(**{k: v for k, v in wl_dict.items()
                                       if k in WorkloadConfig.__dataclass_fields__}),
            compute=ComputeConfig(**{k: v for k, v in cmp_dict.items()
                                     if k in ComputeConfig.__dataclass_fields__}),
            simulation=SimulationConfig(**{k: v for k, v in sim_dict.items()
                                           if k in SimulationConfig.__dataclass_fields__}),
        )

    @classmethod
    def from_json(cls, s: str) -> "ExperimentConfig":
        return cls.from_dict(json.loads(s))

    # ---- hash for topology cache ---------------------------------------

    def topology_hash(self) -> str:
        """Stable digest based *only* on the shell list.

        Workload, compute params, and simulation duration don't affect
        the generated constellation, so they're excluded so that tweaking
        them doesn't bust the (expensive) satgenpy cache.
        """
        shells_dict = [dataclasses.asdict(s) for s in self.shells]
        payload = json.dumps(shells_dict, sort_keys=True).encode("utf-8")
        return hashlib.sha1(payload).hexdigest()[:16]

    # ---- validation -----------------------------------------------------

    def validate(self) -> List[str]:
        """Return a list of human-readable error strings, empty iff valid."""
        errors: List[str] = []
        for i, s in enumerate(self.shells):
            if not (300 <= s.altitude_km <= 2000):
                errors.append(f"shell {i}: altitude {s.altitude_km} km outside [300, 2000]")
            if not (0 <= s.inclination_deg <= 180):
                errors.append(f"shell {i}: inclination {s.inclination_deg}° outside [0, 180]")
            if not (1 <= s.num_planes <= 100):
                errors.append(f"shell {i}: num_planes {s.num_planes} outside [1, 100]")
            if not (1 <= s.sats_per_plane <= 100):
                errors.append(f"shell {i}: sats_per_plane {s.sats_per_plane} outside [1, 100]")
            if not (0 <= s.phase_offset < max(s.num_planes, 1)):
                errors.append(f"shell {i}: phase_offset {s.phase_offset} must be in [0, num_planes)")
            if not (5 <= s.min_elevation_deg <= 40):
                errors.append(f"shell {i}: min_elevation {s.min_elevation_deg}° outside [5, 40]")
            if not (0 < s.compute_ratio <= 1):
                errors.append(f"shell {i}: compute_ratio {s.compute_ratio} outside (0, 1]")
        w = self.workload
        if w.source == "synthetic":
            if w.lambda_total <= 0:
                errors.append(f"workload: lambda_total must be > 0 (got {w.lambda_total})")
        elif w.source == "trace_replay":
            import os.path
            if not w.trace_per_gs_dir:
                errors.append("workload: trace_per_gs_dir must be set when source=trace_replay")
            elif not os.path.isdir(w.trace_per_gs_dir):
                errors.append(f"workload: trace_per_gs_dir is not a directory: {w.trace_per_gs_dir}")
        else:
            errors.append(f"workload: unknown source {w.source!r}")
        if w.L_in_min > w.L_in_max:
            errors.append(f"workload: L_in_min {w.L_in_min} > L_in_max {w.L_in_max}")
        if w.L_out_min > w.L_out_max:
            errors.append(f"workload: L_out_min {w.L_out_min} > L_out_max {w.L_out_max}")
        s = self.simulation
        if not (5 <= s.duration_seconds <= 600):
            errors.append(f"simulation: duration {s.duration_seconds}s outside [5, 600]")
        return errors

    @property
    def total_sats(self) -> int:
        return sum(s.total_sats for s in self.shells)
