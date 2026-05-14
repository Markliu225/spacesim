#!/usr/bin/env python3
"""
Phase B analyzer.

Inputs (under <run_dir>/logs_ns3/):
  - llm_workload_summary.csv             (tx/rx totals from the scheduler)
  - llm_workload_sink_sink_node<N>.csv   (one per sink, per-packet log)
  - llm_workload_schedule.csv            (in <run_dir>/, for tx expectation)

Outputs (written to --out, default = phase_b_result.md):
  - Totals: tx_request_count, tx_packet_count, rx_packet_count, delivery ratio
  - Completeness: for each req_id, did we receive all `total_pkts` packets?
  - Per-packet latency = recv_time_ns - t_emit_ns
      summary stats (min / p50 / mean / p95 / max)
  - First/last packet straggle per request = (max - min) of recv_time_ns
    among packets sharing the same req_id
      summary stats and discussion (this is the Phase C gather wait time)
  - Bottleneck estimate: max latency - min latency

Exit code:
  0 if at least 95% packets delivered and at least one request received,
  non-zero otherwise.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import defaultdict
from statistics import mean


def load_summary(run_dir):
    p = os.path.join(run_dir, "logs_ns3", "llm_workload_summary.csv")
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        rows = list(csv.DictReader(f))
    return rows[0] if rows else {}


def load_sink_csvs(run_dir):
    """Load all sink CSVs in the run dir. Returns list[dict] (one per packet)."""
    sink_dir = os.path.join(run_dir, "logs_ns3")
    all_rows = []
    for name in sorted(os.listdir(sink_dir)):
        if name.startswith("llm_workload_sink") and name.endswith(".csv"):
            with open(os.path.join(sink_dir, name)) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    for k in row:
                        try:
                            row[k] = int(row[k])
                        except ValueError:
                            pass
                    all_rows.append(row)
    return all_rows


def stat_block(samples):
    if not samples:
        return None
    s = sorted(samples)
    n = len(s)
    return {
        "n":    n,
        "min":  s[0],
        "p50":  s[n // 2],
        "mean": sum(s) / n,
        "p95":  s[min(int(0.95 * n), n - 1)],
        "max":  s[-1],
    }


def fmt_ns(x_ns):
    """ns -> human-readable ms with 3 digits."""
    if x_ns is None or (isinstance(x_ns, float) and math.isnan(x_ns)):
        return "—"
    return f"{x_ns / 1e6:.3f} ms"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True,
                   help="Path to the Phase B run directory (contains "
                        "logs_ns3/, config_ns3.properties, "
                        "llm_workload_schedule.csv).")
    p.add_argument("--out", default=None,
                   help="Output markdown path (default: <run-dir>/../phase_b_result.md)")
    args = p.parse_args()
    run_dir = os.path.abspath(args.run_dir)
    out_path = args.out or os.path.join(
        os.path.dirname(run_dir.rstrip("/")), "..", "phase_b_result.md")
    out_path = os.path.abspath(out_path)

    summary = load_summary(run_dir)
    rows = load_sink_csvs(run_dir)

    tx_req = int(summary.get("tx_request_count", 0))
    tx_pkt = int(summary.get("tx_packet_count", 0))
    rx_pkt = int(summary.get("rx_packet_count", 0))
    delivery = rx_pkt / tx_pkt if tx_pkt > 0 else 0.0

    # Group packets by req_id.
    by_req = defaultdict(list)
    for r in rows:
        by_req[r["req_id"]].append(r)

    # Per-request completeness (recv count vs declared total_pkts).
    complete_reqs = 0
    incomplete_reqs = 0
    missing_pkts = []
    for req_id, pkts in by_req.items():
        total = pkts[0]["total_pkts"]
        if len(pkts) == total:
            complete_reqs += 1
        else:
            incomplete_reqs += 1
            received_ids = {p["packet_id"] for p in pkts}
            for i in range(total):
                if i not in received_ids:
                    missing_pkts.append((req_id, i, total))

    # Per-packet latency = recv_time - t_emit (ns).
    per_pkt_latencies = [r["recv_time_ns"] - r["t_emit_ns"] for r in rows]
    pp_stats = stat_block(per_pkt_latencies)

    # Per-request straggle = max(recv) - min(recv) within a req_id.
    straggles = []
    for req_id, pkts in by_req.items():
        if len(pkts) < 2:
            continue  # 1-packet request: no straggle
        ts = [p["recv_time_ns"] for p in pkts]
        straggles.append(max(ts) - min(ts))
    str_stats = stat_block(straggles)

    # Bottleneck = max packet latency - min packet latency.
    bottleneck_ns = (max(per_pkt_latencies) - min(per_pkt_latencies)
                     if per_pkt_latencies else 0)

    # ---- Compose markdown ----
    lines = []
    lines.append("# Phase B — Result\n")
    lines.append("## Totals\n")
    lines.append(f"- tx_request_count : **{tx_req}**")
    lines.append(f"- tx_packet_count  : **{tx_pkt}**")
    lines.append(f"- rx_packet_count  : **{rx_pkt}**  ({delivery * 100:.2f}% delivered)")
    lines.append("")

    lines.append("## Per-request completeness\n")
    lines.append(f"- requests with all packets received : **{complete_reqs}**")
    lines.append(f"- requests missing one or more packets : **{incomplete_reqs}**")
    if missing_pkts:
        lines.append("")
        lines.append("Missing (req_id, packet_id, total_pkts):")
        for m in missing_pkts[:20]:
            lines.append(f"  - {m}")
        if len(missing_pkts) > 20:
            lines.append(f"  ... and {len(missing_pkts) - 20} more")
    lines.append("")

    lines.append("## Single-packet end-to-end latency\n")
    lines.append("(`recv_time_ns - t_emit_ns` per packet)")
    lines.append("")
    if pp_stats:
        lines.append("| stat | ns | ms |")
        lines.append("|---|---:|---:|")
        for key in ("n", "min", "p50", "mean", "p95", "max"):
            v = pp_stats[key]
            if key == "n":
                lines.append(f"| count | {v} | — |")
            else:
                lines.append(f"| {key} | {int(v):,} | {fmt_ns(v)} |")
    lines.append("")

    lines.append("## Per-request straggle (Phase C gather-wait ground truth)\n")
    lines.append("(`max(recv_time_ns) - min(recv_time_ns)` over packets in same request)")
    lines.append("")
    if str_stats:
        lines.append("| stat | ns | ms |")
        lines.append("|---|---:|---:|")
        for key in ("n", "min", "p50", "mean", "p95", "max"):
            v = str_stats[key]
            if key == "n":
                lines.append(f"| requests with >=2 pkts | {v} | — |")
            else:
                lines.append(f"| {key} | {int(v):,} | {fmt_ns(v)} |")
    else:
        lines.append("_All requests had a single packet (L_in too small)._")
    lines.append("")

    lines.append("## Path-bottleneck proxy\n")
    lines.append("(`max per-packet latency − min per-packet latency`; large spread "
                 "indicates queueing or route change along the path)")
    lines.append("")
    lines.append(f"- bottleneck range : **{fmt_ns(bottleneck_ns)}**")
    lines.append("")

    lines.append("## Verdict\n")
    ok = delivery >= 0.95 and rx_pkt > 0
    verdict = "PASS" if ok else "FAIL"
    lines.append(f"- delivery >= 95% ? : `{delivery * 100:.2f}%`  -> `{delivery >= 0.95}`")
    lines.append(f"- at least one packet received? : `{rx_pkt > 0}`")
    lines.append(f"\n**Verdict: {verdict}**\n")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {out_path}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
