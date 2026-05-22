"""
Schedule generator — emits Phase C's 15-column
``llm_workload_schedule.csv``.

Phase C's format (one schedule entry per (src_gs, dst_compute) pair):

  src_gs, dst_compute, lambda_req_per_sec,
  L_in_mean, L_in_std, L_in_min, L_in_max,
  L_out_mean, L_out_std, L_out_min, L_out_max,
  bytes_per_token, packet_payload,
  start_ns, stop_ns

Strategy in v1
--------------

- For each GS in the GS set, emit one entry.
- Per-GS arrival rate = ``lambda_total / num_gs`` (even split).
- ``dst_compute`` = lowest-ID type=C satellite in ``satellite_roles.txt``.
  This is the simplest viable choice; later phases can switch to a
  policy that picks per-GS nearest, or round-robins across compute SATs.
- The traffic window covers most of the simulation: starts at 0.5 s,
  stops at ``duration - 0.5 s`` (in nanoseconds).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Optional


# 15-column schema, preserved verbatim so the C++ side parses cleanly.
COLUMNS = [
    "src_gs", "dst_compute", "lambda",
    "L_in_mean", "L_in_std", "L_in_min", "L_in_max",
    "L_out_mean", "L_out_std", "L_out_min", "L_out_max",
    "bytes_per_token", "packet_payload",
    "start_ns", "stop_ns",
]


def read_compute_sat_ids(roles_path: Path) -> List[int]:
    """Return sat IDs marked ``C`` in a ``satellite_roles.txt`` file."""
    out: List[int] = []
    with open(roles_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) != 2:
                continue
            sid_str, role = parts[0], parts[1].strip()
            if role == "C":
                try:
                    out.append(int(sid_str))
                except ValueError:
                    continue
    return sorted(out)


def generate_schedule(
    config,
    output_path: Path,
    *,
    num_satellites: int,
    num_ground_stations: int,
    roles_path: Path,
    dst_strategy: str = "first_compute",
) -> int:
    """Write a Phase C ``llm_workload_schedule.csv`` and return row count.

    Args:
        config: :class:`ExperimentConfig`.
        output_path: where to write the CSV.
        num_satellites: total sats (used to compute GS node IDs).
        num_ground_stations: number of GS rows to emit schedules for.
        roles_path: ``satellite_roles.txt`` (must contain at least one C).
        dst_strategy: ``"first_compute"`` (lowest-ID C, v1) or
            ``"per_gs_round_robin"`` (cycle through C set).

    Raises:
        ValueError: no compute SATs.
    """
    compute_sats = read_compute_sat_ids(roles_path)
    if not compute_sats:
        raise ValueError(
            f"satellite_roles.txt at {roles_path} contains no type=C "
            f"satellites; cannot build a schedule"
        )

    w = config.workload
    sim_dur_ns = config.simulation.duration_seconds * 1_000_000_000
    start_ns = 500_000_000           # 0.5 s into the sim
    stop_ns = sim_dur_ns - start_ns  # leave 0.5 s tail for late responses

    per_gs_lambda = w.lambda_total / max(num_ground_stations, 1)

    rows: List[List] = []
    for gid in range(num_ground_stations):
        src_gs_node = num_satellites + gid

        if dst_strategy == "per_gs_round_robin":
            dst_sat = compute_sats[gid % len(compute_sats)]
        else:  # first_compute (default)
            dst_sat = compute_sats[0]

        rows.append([
            src_gs_node, dst_sat, per_gs_lambda,
            w.L_in_mean, w.L_in_std, w.L_in_min, w.L_in_max,
            w.L_out_mean, w.L_out_std, w.L_out_min, w.L_out_max,
            w.bytes_per_token, w.packet_payload,
            start_ns, stop_ns,
        ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            # int fields stay int, float fields stay float so the C++
            # parser doesn't get e.g. "10.0" where it expects "10".
            writer.writerow(_format_row(row))
    return len(rows)


def _format_row(row: List) -> List[str]:
    """Format each cell to a stable string.

    Convention matches what Phase C's existing schedule files look like:
    integer fields without decimal; float fields with their natural repr.
    """
    out: List[str] = []
    # column kinds — fields 3, 4, 7, 8 are floats; rest are ints.
    float_indices = {3, 4, 7, 8}   # 0-based: lambda, L_in_mean, L_in_std,
                                    # L_out_mean, L_out_std
    # Actually re-check positions:
    #   0 src_gs (int)
    #   1 dst_compute (int)
    #   2 lambda (float)
    #   3 L_in_mean (float)
    #   4 L_in_std (float)
    #   5 L_in_min (int)
    #   6 L_in_max (int)
    #   7 L_out_mean (float)
    #   8 L_out_std (float)
    #   9 L_out_min (int)
    #  10 L_out_max (int)
    #  11 bytes_per_token (int)
    #  12 packet_payload (int)
    #  13 start_ns (int)
    #  14 stop_ns (int)
    float_indices = {2, 3, 4, 7, 8}
    for i, v in enumerate(row):
        if i in float_indices:
            out.append(f"{float(v):g}")
        else:
            out.append(str(int(v)))
    return out
