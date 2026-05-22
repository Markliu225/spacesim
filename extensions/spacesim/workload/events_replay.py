"""
events_replay.py — wire pre-generated per-GS event traces into the
simulator's run dir as a 16-column ``llm_workload_schedule.csv``.

The companion ``traffic_generator`` tool produces one CSV per ground
station under ``<dir>/per_gs/events_gs<idx>_<slug>.csv`` with columns:

    req_id, src_gs_idx, t_emit_ns, L_in, L_out

This adapter:

1. Stages those files into the run dir (copies by default; symlinks if
   the caller asks) so the schedule reader can resolve them relative to
   ``GetRunDir()``.
2. Emits a 16-column schedule, one row per (src_gs → dst_compute) pair,
   where the trailing ``events_filename`` column points the C++
   ``LLMRequestApplication`` at the staged trace. The distribution
   columns (lambda, L_in_*) are still written but the C++ side ignores
   them in events-replay mode.

The L_out distribution columns DO matter — ``ComputeApplication``
samples L_out from them. Pass values that reflect the trace, or accept
the defaults that match the Azure trace fitter's order-of-magnitude.
"""

from __future__ import annotations

import csv
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence


SCHEDULE_COLUMNS = [
    "src_gs", "dst_compute", "lambda",
    "L_in_mean", "L_in_std", "L_in_min", "L_in_max",
    "L_out_mean", "L_out_std", "L_out_min", "L_out_max",
    "bytes_per_token", "packet_payload",
    "start_ns", "stop_ns",
    "events_filename",
]

# Default L_out distribution (used by Compute when L_out columns are not
# overridden). Matches the Azure trace fitter's central tendency.
_DEFAULT_L_OUT_MEAN = 200.0
_DEFAULT_L_OUT_STD = 100.0
_DEFAULT_L_OUT_MIN = 1
_DEFAULT_L_OUT_MAX = 2000

_EVENTS_FILE_RE = re.compile(r"^events_gs(\d+)(?:_.*)?\.csv$")


def discover_per_gs_files(per_gs_dir: Path) -> Dict[int, Path]:
    """Return ``{gs_idx: path}`` for every ``events_gs<N>*.csv`` found."""
    out: Dict[int, Path] = {}
    if not per_gs_dir.is_dir():
        raise FileNotFoundError(f"per_gs dir not found: {per_gs_dir}")
    for entry in sorted(per_gs_dir.iterdir()):
        if not entry.is_file():
            continue
        m = _EVENTS_FILE_RE.match(entry.name)
        if not m:
            continue
        idx = int(m.group(1))
        if idx in out:
            raise ValueError(
                f"duplicate events file for gs_idx={idx}: "
                f"{out[idx].name} vs {entry.name}"
            )
        out[idx] = entry
    if not out:
        raise FileNotFoundError(
            f"no events_gs*.csv files in {per_gs_dir}"
        )
    return out


def install_events_replay(
    config,
    *,
    per_gs_dir: Path,
    run_dir: Path,
    num_satellites: int,
    num_ground_stations: Optional[int] = None,
    gs_idx_to_node_id: Optional[Callable[[int], int]] = None,
    dst_strategy: str = "first_compute",
    roles_path: Optional[Path] = None,
    dst_compute_sat_ids: Optional[Sequence[int]] = None,
    schedule_filename: str = "llm_workload_schedule.csv",
    stage_mode: str = "copy",
    staged_subdir: str = "events",
    l_out_mean: float = _DEFAULT_L_OUT_MEAN,
    l_out_std: float = _DEFAULT_L_OUT_STD,
    l_out_min: int = _DEFAULT_L_OUT_MIN,
    l_out_max: int = _DEFAULT_L_OUT_MAX,
    trace_start_offset_sec: float = 0.0,
    on_log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Stage per-GS event files into ``run_dir`` and write a 16-col schedule.

    Args:
        config: :class:`ExperimentConfig` (used for L_in clamps, bytes/token,
            packet payload, and the sim duration window).
        per_gs_dir: directory containing ``events_gs<N>_*.csv`` files
            (produced by ``traffic_generator/traffic_gen.py``).
        run_dir: the simulator run dir; the schedule and staged events go
            here.
        num_satellites: total satellites in the topology; GS node ids
            default to ``num_satellites + gs_idx`` unless
            ``gs_idx_to_node_id`` is supplied.
        num_ground_stations: optional cap on which events_gs<N>_*.csv
            files to install. When set, only ``gs_idx < num_ground_stations``
            rows are kept; the rest are listed in the log as skipped.
            This prevents writing schedule rows referencing GS node ids
            that don't exist in the topology (e.g. when the trace has
            10 cities but the topology was built with ``top_5_cities``).
        gs_idx_to_node_id: optional override mapping ``gs_idx -> node_id``.
        on_log: optional ``str → None`` sink for human-readable status
            lines (e.g. the dashboard's live log).
        dst_strategy: ``"first_compute"`` (default) or ``"per_gs_round_robin"``.
        roles_path: ``satellite_roles.txt``; required when
            ``dst_compute_sat_ids`` is omitted.
        dst_compute_sat_ids: explicit list of compute-sat node ids.
            Overrides ``roles_path`` if set.
        schedule_filename: name of the schedule file to write into ``run_dir``.
        stage_mode: ``"copy"`` (default, portable) or ``"symlink"``
            (cheap; fails on cross-device).
        staged_subdir: subdir under ``run_dir`` for staged event files.
        l_out_mean, l_out_std, l_out_min, l_out_max: L_out distribution
            written to each row (consumed by ``ComputeApplication``).
        trace_start_offset_sec: lets you simulate a slice of a long trace
            without regenerating it. The events file is filtered to the
            window ``[offset, offset + duration_seconds)``; each kept
            event's t_emit_ns is shifted by ``-offset`` so the first
            in-window event lines up with sim t=0. Set to 50400 (14:00)
            to simulate Tokyo's local-afternoon peak from a 24h trace,
            etc. Default 0 = use the trace's beginning.

    Returns:
        A summary dict::

            {
              "rows": int,                 # schedule rows written
              "events_total": int,         # rows in the source CSVs
              "events_kept": int,          # events inside [offset, offset+dur)
              "events_dropped": int,       # events outside the window
              "kept_fraction": float,      # events_kept / events_total
              "per_gs": {gs_idx: (name, n_kept, n_total)},
              "trace_span_sec": float,     # max(t_emit) - min(t_emit) in source
              "sim_duration_sec": float,
            }

        Use this to surface "X of Y events will fire" diagnostics in
        callers (e.g. the dashboard).
    """
    if dst_compute_sat_ids is None:
        if roles_path is None:
            raise ValueError(
                "install_events_replay: pass either dst_compute_sat_ids "
                "or roles_path"
            )
        from .schedule import read_compute_sat_ids
        dst_compute_sat_ids = read_compute_sat_ids(roles_path)
    if not dst_compute_sat_ids:
        raise ValueError("install_events_replay: no compute satellites available")

    if gs_idx_to_node_id is None:
        gs_idx_to_node_id = lambda i: num_satellites + i  # noqa: E731

    files = discover_per_gs_files(per_gs_dir)

    if num_ground_stations is not None:
        keep = {i: p for i, p in files.items() if i < num_ground_stations}
        skipped = sorted(i for i in files if i >= num_ground_stations)
        if skipped:
            msg = (f"install_events_replay: topology has {num_ground_stations} "
                   f"GS but trace dir has {len(files)} files; skipping "
                   f"gs_idx={skipped} ({len(skipped)} files).")
            if on_log is not None:
                on_log(msg)
            else:
                # Surface to console in non-dashboard callers.
                print(msg)
        if not keep:
            raise ValueError(
                f"install_events_replay: no events files within "
                f"gs_idx < {num_ground_stations}; trace dir has indices "
                f"{sorted(files)}"
            )
        files = keep

    run_dir.mkdir(parents=True, exist_ok=True)
    staged_dir = run_dir / staged_subdir
    staged_dir.mkdir(parents=True, exist_ok=True)

    sim_dur_sec = config.simulation.duration_seconds
    sim_dur_ns = sim_dur_sec * 1_000_000_000
    start_ns = 0
    stop_ns = sim_dur_ns

    w = config.workload

    # ---- Clip + shift each per-GS file -------------------------------
    # The C++ side schedules every row in the events file relative to
    # sim t=0, so events with t_emit_ns > sim_end never fire. Pre-clip
    # here to (a) report an accurate "X of Y events will fire" number,
    # (b) avoid scheduling millions of dead events that just pad the
    # ns-3 event queue, and (c) optionally fast-forward into the trace
    # via ``trace_start_offset_sec``.
    offset_ns = int(round(trace_start_offset_sec * 1_000_000_000))
    window_lo_ns = offset_ns
    window_hi_ns = offset_ns + sim_dur_ns

    per_gs: dict = {}
    events_total = 0
    events_kept = 0
    trace_min_ns = None
    trace_max_ns = None

    rows: List[List] = []
    for gs_idx in sorted(files):
        src_file = files[gs_idx]
        staged_path = staged_dir / src_file.name
        if staged_path.exists() or staged_path.is_symlink():
            staged_path.unlink()

        # Stream the source CSV, write only events in [offset, offset+dur),
        # subtracting offset_ns so the first kept event's sim time starts
        # near 0.
        n_total = 0
        n_kept = 0
        with open(src_file, "r") as src, open(staged_path, "w", newline="") as dst:
            header = src.readline()
            if header.startswith("req_id"):
                dst.write(header)
            else:
                # No header in source — write our canonical one and
                # treat the first line as data.
                dst.write("req_id,src_gs_idx,t_emit_ns,L_in,L_out\n")
                if header.strip():
                    src = _Prepend(header, src)  # type: ignore[assignment]
            for line in src:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 5:
                    continue
                try:
                    t_emit = int(parts[2])
                except ValueError:
                    continue
                n_total += 1
                if trace_min_ns is None or t_emit < trace_min_ns:
                    trace_min_ns = t_emit
                if trace_max_ns is None or t_emit > trace_max_ns:
                    trace_max_ns = t_emit
                if not (window_lo_ns <= t_emit < window_hi_ns):
                    continue
                shifted = t_emit - offset_ns
                dst.write(f"{parts[0]},{parts[1]},{shifted},{parts[3]},{parts[4]}\n")
                n_kept += 1

        events_total += n_total
        events_kept += n_kept
        per_gs[gs_idx] = (src_file.stem, n_kept, n_total)

        rel_events = os.path.relpath(staged_path, run_dir)

        src_node = gs_idx_to_node_id(gs_idx)
        if dst_strategy == "per_gs_round_robin":
            dst_sat = dst_compute_sat_ids[gs_idx % len(dst_compute_sat_ids)]
        else:
            dst_sat = dst_compute_sat_ids[0]

        rows.append([
            src_node, dst_sat,
            # lambda is ignored in events-replay mode but the reader still
            # parses it. Use a positive sentinel just in case any future
            # path checks it; the C++ side skips the > 0 enforcement when
            # events_filename is non-empty.
            1.0,
            w.L_in_mean, w.L_in_std, w.L_in_min, w.L_in_max,
            l_out_mean, l_out_std, l_out_min, l_out_max,
            w.bytes_per_token, w.packet_payload,
            start_ns, stop_ns,
            rel_events,
        ])

    schedule_path = run_dir / schedule_filename
    # LF-only line endings — the C++ schedule reader splits on '\n' and
    # does not strip trailing '\r', so CRLF would leave '\r' on the last
    # field (here: events_filename), making the path unopenable.
    with open(schedule_path, "w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        for row in rows:
            writer.writerow(_format_row(row))

    # ---- Surface a fit-check summary ---------------------------------
    trace_span_sec = ((trace_max_ns - trace_min_ns) / 1e9
                      if trace_min_ns is not None else 0.0)
    summary = {
        "rows": len(rows),
        "events_total": events_total,
        "events_kept": events_kept,
        "events_dropped": events_total - events_kept,
        "kept_fraction": (events_kept / events_total) if events_total else 0.0,
        "per_gs": per_gs,
        "trace_span_sec": trace_span_sec,
        "sim_duration_sec": float(sim_dur_sec),
        "trace_start_offset_sec": float(trace_start_offset_sec),
    }
    if on_log is not None:
        on_log(
            f"events_replay: trace span {trace_span_sec/3600:.2f}h, "
            f"sim window [{trace_start_offset_sec:.0f}s, "
            f"{trace_start_offset_sec + sim_dur_sec:.0f}s] "
            f"({sim_dur_sec:.0f}s). Kept "
            f"{events_kept:,}/{events_total:,} events "
            f"({100*summary['kept_fraction']:.4f}%); "
            f"avg arrival ~ {events_kept/max(sim_dur_sec,1):.1f} req/s "
            f"across {len(per_gs)} GS."
        )
        if events_kept == 0 and events_total > 0:
            on_log(
                "WARNING: trace_start_offset_sec puts the sim window "
                "outside the trace's coverage. Adjust offset or "
                "regenerate the trace."
            )
    return summary


class _Prepend:
    """File-like that yields ``first`` once, then ``rest``'s remaining lines."""
    def __init__(self, first: str, rest):
        self._first = first
        self._rest = rest
    def __iter__(self):
        yield self._first
        for line in self._rest:
            yield line


def _format_row(row: List) -> List[str]:
    """Stable string formatting for the 16-column schedule."""
    # 0 src_gs (int), 1 dst_compute (int), 2 lambda (float),
    # 3 L_in_mean (float), 4 L_in_std (float),
    # 5 L_in_min (int), 6 L_in_max (int),
    # 7 L_out_mean (float), 8 L_out_std (float),
    # 9 L_out_min (int), 10 L_out_max (int),
    # 11 bytes_per_token (int), 12 packet_payload (int),
    # 13 start_ns (int), 14 stop_ns (int),
    # 15 events_filename (string)
    float_indices = {2, 3, 4, 7, 8}
    string_indices = {15}
    out: List[str] = []
    for i, v in enumerate(row):
        if i in string_indices:
            out.append(str(v))
        elif i in float_indices:
            out.append(f"{float(v):g}")
        else:
            out.append(str(int(v)))
    return out
