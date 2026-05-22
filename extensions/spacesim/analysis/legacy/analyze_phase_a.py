#!/usr/bin/env python3
"""
Phase A — analyse the GS -> compute-SAT TCP flow run.

What this script extracts from a Hypatia run dir
-------------------------------------------------

  - Flow outcome: was the 1 MB delivered? when?
  - RTT samples from tcp_flow_0_rtt.csv (basic-sim format: flow_id, time_ns, rtt_ns)
  - The forwarding path at t = start_time + half-RTT, traced offline by
    walking the augmented fstate. (ns-3 itself doesn't dump packet paths.)
  - The geometric propagation lower bound for the round trip:
        2 * (GSL_up_length + sum_ISL_hop_lengths) / c
    (The destination is a satellite so there is no GSL_down on the return.
    Actually return is symmetric -- same path, same hops, same GSL.)

Why bother tracing the path offline
-----------------------------------

main_satnet doesn't log per-packet hops at the level of node IDs. The
fstate file is itself the routing oracle: at any timestep, for any
(curr, dst) the next-hop is deterministic. So we replicate ns-3's
behaviour with a tiny Python loop, starting from `src_gs` and following
`fstate[(curr, dst_sat)]` until we either reach `dst_sat` or hit a drop.

Outputs
-------

Writes phase_a_result.md with the findings. Returns non-zero exit if the
flow did not complete or the traced path is bogus (e.g. loop).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

# Make satgen importable so we can use the same SGP-4 propagation as
# augment_fstate.py for distance sanity checks.
_HERE = os.path.abspath(os.path.dirname(__file__))
_SATGENPY_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..", "satgenpy"))
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

C_M_PER_NS = 299792458.0 / 1e9   # speed of light, m / ns


def read_fstate(path: str) -> Dict[Tuple[int, int], Tuple[int, int, int]]:
    """Parse fstate_<t>.txt (skipping our PHASE_A_AUGMENT comment line)."""
    f: Dict[Tuple[int, int], Tuple[int, int, int]] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) != 5:
                continue
            curr, dst, nh, my_if, nh_if = (int(x) for x in parts)
            f[(curr, dst)] = (nh, my_if, nh_if)
    return f


def trace_path(
    fstate: Dict[Tuple[int, int], Tuple[int, int, int]],
    src: int,
    dst: int,
    max_hops: int = 64,
) -> List[int]:
    """Follow fstate[(curr, dst)] from src until reaching dst (or drop/loop)."""
    path = [src]
    seen = {src}
    curr = src
    while curr != dst:
        entry = fstate.get((curr, dst))
        if entry is None:
            raise RuntimeError(f"no fstate entry for ({curr}, {dst}) -- path broken")
        nh = entry[0]
        if nh == -1:
            raise RuntimeError(f"drop entry at ({curr}, {dst})")
        if nh in seen:
            raise RuntimeError(f"loop detected: revisiting {nh} from {curr}")
        path.append(nh)
        seen.add(nh)
        curr = nh
        if len(path) > max_hops:
            raise RuntimeError(f"path exceeds max_hops={max_hops}")
    return path


def load_rtt_ns(rtt_csv: str) -> np.ndarray:
    """basic-sim tcp_flow_<id>_rtt.csv = (flow_id, time_ns, rtt_ns)."""
    if not os.path.exists(rtt_csv):
        return np.array([], dtype=np.int64)
    rows = np.loadtxt(rtt_csv, delimiter=",", dtype=np.int64)
    if rows.size == 0:
        return np.array([], dtype=np.int64)
    if rows.ndim == 1:
        rows = rows.reshape(1, -1)
    return rows[:, 2]


def parse_flow_summary(run_dir: str) -> Dict[str, str]:
    """Read tcp_flows.csv (one row per flow) if present.

    Format from basic-sim's tcp-flow-scheduler:
      flow_id,from,to,size_byte,start_ns,end_ns,duration_ns,
      bytes_sent,completed,metadata
    """
    path = os.path.join(run_dir, "logs_ns3", "tcp_flows.csv")
    if not os.path.exists(path):
        # Some versions of basic-sim use tcp_flows.txt for a human-readable
        # variant; fall back to that if needed.
        for fallback in ("tcp_flows.txt",):
            alt = os.path.join(run_dir, "logs_ns3", fallback)
            if os.path.exists(alt):
                path = alt
                break
        else:
            return {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 9:
                continue
            return {
                "flow_id": parts[0],
                "from": parts[1],
                "to": parts[2],
                "size_byte": parts[3],
                "start_ns": parts[4],
                "end_ns": parts[5],
                "duration_ns": parts[6],
                "bytes_sent": parts[7],
                "completed": parts[8],
                "raw": line,
            }
    return {}


def read_schedule(path: str) -> Tuple[int, int, int, int]:
    """Return (flow_id, src, dst, start_time_ns) for the (only) flow."""
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[4])
    raise SystemExit(f"empty schedule: {path}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", required=True,
                   help="ns-3 run dir (contains config_ns3.properties, schedule.csv, logs_ns3/)")
    p.add_argument("--state-dir", required=True,
                   help="Path to the gen_data/<network>/ directory")
    p.add_argument("--dynamic-state-dir", required=True,
                   help="Path to the dynamic_state_<int_ms>ms_for_<dur_s>s/ subdir")
    p.add_argument("--out", default=None,
                   help="Path to phase_a_result.md (default: <run-dir>/../phase_a_result.md)")
    args = p.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    state_dir = os.path.abspath(args.state_dir)
    dyn_dir = os.path.abspath(args.dynamic_state_dir)
    out_path = args.out or os.path.join(os.path.dirname(run_dir.rstrip("/")), "..", "phase_a_result.md")
    out_path = os.path.abspath(out_path)

    print(f"run_dir   = {run_dir}")
    print(f"state_dir = {state_dir}")
    print(f"dyn_dir   = {dyn_dir}")
    print(f"out       = {out_path}")

    # Read schedule
    sched = read_schedule(os.path.join(run_dir, "schedule.csv"))
    flow_id, src, dst, start_ns = sched
    print(f"flow: id={flow_id}  src={src}  dst={dst}  start_ns={start_ns}")

    # Read flow summary
    summary = parse_flow_summary(run_dir)
    completed = summary.get("completed", "?")
    duration_ns = int(summary["duration_ns"]) if summary.get("duration_ns", "-1").lstrip("-").isdigit() else None
    bytes_sent = summary.get("bytes_sent", "?")

    # Read RTT samples
    rtt_ns_arr = load_rtt_ns(os.path.join(run_dir, "logs_ns3", f"tcp_flow_{flow_id}_rtt.csv"))
    if rtt_ns_arr.size > 0:
        rtt_stats = {
            "n": int(rtt_ns_arr.size),
            "min_ms": float(rtt_ns_arr.min()) / 1e6,
            "p50_ms": float(np.percentile(rtt_ns_arr, 50)) / 1e6,
            "mean_ms": float(rtt_ns_arr.mean()) / 1e6,
            "p95_ms": float(np.percentile(rtt_ns_arr, 95)) / 1e6,
            "max_ms": float(rtt_ns_arr.max()) / 1e6,
        }
    else:
        rtt_stats = None

    # Load constellation static state
    tles = read_tles(os.path.join(state_dir, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    list_isls = read_isls(os.path.join(state_dir, "isls.txt"), len(satellites))
    ground_stations = read_ground_stations_extended(os.path.join(state_dir, "ground_stations.txt"))
    num_sats = len(satellites)

    # Find a fstate file at or just after the flow start time to trace from.
    # Files exist at multiples of `dynamic_state_update_interval_ns`.
    fstate_times = sorted(int(name.split("_")[1].split(".")[0])
                          for name in os.listdir(dyn_dir) if name.startswith("fstate_"))
    trace_t = max(t for t in fstate_times if t <= start_ns) if any(t <= start_ns for t in fstate_times) else fstate_times[0]
    fstate = read_fstate(os.path.join(dyn_dir, f"fstate_{trace_t}.txt"))
    print(f"using fstate_{trace_t}.txt for path trace (start_ns={start_ns})")

    path = trace_path(fstate, src, dst)
    print(f"path: {path}")

    # Annotate path with node type and per-hop length
    def node_kind(n: int) -> str:
        return "SAT" if n < num_sats else f"GS-{n - num_sats}"

    time_at_trace = epoch + trace_t * u.ns
    epoch_str = str(epoch)
    t_str = str(time_at_trace)

    hop_rows: List[Tuple[str, str, float, str]] = []
    total_m = 0.0
    isl_hops = 0
    gsl_hops = 0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        if a < num_sats and b < num_sats:
            d_m = distance_m_between_satellites(satellites[a], satellites[b], epoch_str, t_str)
            kind = "ISL"
            isl_hops += 1
        elif a < num_sats and b >= num_sats:
            d_m = distance_m_ground_station_to_satellite(ground_stations[b - num_sats], satellites[a], epoch_str, t_str)
            kind = "GSL"
            gsl_hops += 1
        elif a >= num_sats and b < num_sats:
            d_m = distance_m_ground_station_to_satellite(ground_stations[a - num_sats], satellites[b], epoch_str, t_str)
            kind = "GSL"
            gsl_hops += 1
        else:
            d_m = math.nan
            kind = "??"
        hop_rows.append((node_kind(a), node_kind(b), d_m, kind))
        total_m += d_m

    # Geometric one-way and RTT lower bounds
    one_way_ns = total_m / C_M_PER_NS
    rtt_ns_geom = 2 * one_way_ns
    print(f"path total length: {total_m / 1000:.1f} km  => one-way {one_way_ns / 1e6:.3f} ms, "
          f"RTT geom {rtt_ns_geom / 1e6:.3f} ms")

    # Determine destination compute SAT plane (informational)
    dst_plane = None
    try:
        with open(os.path.join(state_dir, "tles.txt")) as fh:
            hdr = fh.readline().split()
            sats_per_plane = int(hdr[1])
        if dst < num_sats:
            dst_plane = dst // sats_per_plane
    except Exception:
        pass

    # Compose phase_a_result.md
    lines: List[str] = []
    lines.append("# Phase A — Result\n")
    lines.append("## Flow\n")
    lines.append(f"- flow_id: `{flow_id}`")
    lines.append(f"- src    : node `{src}` ({node_kind(src)})")
    if dst_plane is not None:
        lines.append(f"- dst    : node `{dst}` (compute SAT, plane {dst_plane})")
    else:
        lines.append(f"- dst    : node `{dst}` ({node_kind(dst)})")
    lines.append(f"- start_time_ns: `{start_ns}`  (= {start_ns / 1e9:.3f} s)")
    lines.append("")

    lines.append("## Outcome\n")
    if summary:
        lines.append(f"- completed   : **{completed}**")
        lines.append(f"- bytes_sent  : {bytes_sent}")
        if duration_ns is not None:
            lines.append(f"- duration_ns : {duration_ns} (= {duration_ns / 1e9:.3f} s)")
        lines.append(f"- raw row     : `{summary.get('raw', '?')}`")
    else:
        lines.append("- No tcp_flows.csv / tcp_flows.txt found in logs_ns3/.")
    lines.append("")

    if rtt_stats:
        lines.append("## RTT samples\n")
        lines.append("| stat | value |")
        lines.append("|---|---|")
        for k, v in rtt_stats.items():
            if k == "n":
                lines.append(f"| count | {v} |")
            else:
                lines.append(f"| {k} | {v:.3f} |")
        lines.append("")

    lines.append(f"## Path (traced from `fstate_{trace_t}.txt`)\n")
    lines.append("| hop | from | to | kind | length (km) |")
    lines.append("|---|---|---|---|---|")
    for i, (a, b, d, kind) in enumerate(hop_rows):
        lines.append(f"| {i} | {a} | {b} | {kind} | {d / 1000:.2f} |")
    lines.append(f"\n- ISL hops : **{isl_hops}**")
    lines.append(f"- GSL hops : **{gsl_hops}**")
    lines.append(f"- total path length: **{total_m / 1000:.2f} km**")
    lines.append("")

    lines.append("## Geometric propagation lower bound\n")
    lines.append(f"- one-way (length / c) : **{one_way_ns / 1e6:.3f} ms**")
    lines.append(f"- RTT  (2 * one-way)   : **{rtt_ns_geom / 1e6:.3f} ms**")
    if rtt_stats:
        margin = rtt_stats["min_ms"] - rtt_ns_geom / 1e6
        lines.append(f"- measured min RTT     : {rtt_stats['min_ms']:.3f} ms "
                     f"(margin over geometric: {margin:+.3f} ms; should be > 0)")
        if margin < -0.1:
            lines.append("  ⚠ measured RTT below geometric lower bound -- something is off.")
    lines.append("")

    lines.append("## Verdict\n")
    ok_completion = (completed.lower() == "yes" or completed.lower() == "true")
    ok_isl = isl_hops > 0
    ok_geom = (rtt_stats is None) or (rtt_stats["min_ms"] >= rtt_ns_geom / 1e6 - 0.1)
    verdict = "PASS" if ok_completion and ok_isl and ok_geom else "FAIL"
    lines.append(f"- flow completed?         : `{ok_completion}`")
    lines.append(f"- path traversed ISLs?    : `{ok_isl}` ({isl_hops} hops)")
    lines.append(f"- RTT >= geometric bound? : `{ok_geom}`")
    lines.append(f"\n**Verdict: {verdict}**\n")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {out_path}")

    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
