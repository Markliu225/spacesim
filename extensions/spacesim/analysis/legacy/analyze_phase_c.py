#!/usr/bin/env python3
"""
Phase C analyser — per-request lifecycle breakdown.

Joins gather_log / compute_log / response_log by req_id and reports:

  T_forward    = t_first_arrival_ns - t_emit_ns
  D_gather     = t_last_arrival - t_first_arrival
  T_queue_wait = t_compute_start - t_queue_enter
  T_compute    = t_compute_end - t_compute_start
  T_return     = t_response_recv - t_response_emit (per packet; first arrival used)
  T_total      = t_response_first_recv - t_emit_ns
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import defaultdict


def read_csv(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for row in csv.DictReader(f):
            rows.append({k: int(v) if v.lstrip("-").isdigit() else v
                         for k, v in row.items()})
    return rows


def stat(samples_ns):
    if not samples_ns:
        return {"n": 0, "min": None, "p50": None, "mean": None,
                "p95": None, "p99": None, "max": None}
    s = sorted(samples_ns)
    n = len(s)
    return {
        "n":    n,
        "min":  s[0],
        "p50":  s[n // 2],
        "mean": sum(s) / n,
        "p95":  s[min(int(0.95 * n), n - 1)],
        "p99":  s[min(int(0.99 * n), n - 1)],
        "max":  s[-1],
    }


def ms(ns):
    return None if ns is None else f"{ns / 1e6:.2f}"


def stat_row(name, st):
    return (f"| {name} | {st['n']} | {ms(st['min'])} | {ms(st['p50'])} | "
            f"{ms(st['mean'])} | {ms(st['p95'])} | {ms(st['p99'])} | "
            f"{ms(st['max'])} |")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--schedule", default=None,
                   help="Optional: llm_workload_schedule.csv for analytic comparison")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    logs = os.path.join(run_dir, "logs_ns3")
    out_path = args.out or os.path.join(
        os.path.dirname(run_dir.rstrip("/")), "..", "phase_c_result.md")
    out_path = os.path.abspath(out_path)

    # Discover per-node CSVs (compute_sat / GS may have multiple).
    gather_rows  = []
    compute_rows = []
    response_rows = []
    stuck_rows   = []
    for name in sorted(os.listdir(logs)):
        if name.startswith("llm_gather_node")   and name.endswith(".csv"):
            gather_rows.extend(read_csv(os.path.join(logs, name)))
        if name.startswith("llm_compute_node")  and name.endswith(".csv"):
            compute_rows.extend(read_csv(os.path.join(logs, name)))
        if name.startswith("llm_response_node") and name.endswith(".csv"):
            response_rows.extend(read_csv(os.path.join(logs, name)))
        if name.startswith("llm_stuck_node")    and name.endswith(".csv"):
            stuck_rows.extend(read_csv(os.path.join(logs, name)))

    summary = {}
    sp = os.path.join(logs, "llm_workload_summary.csv")
    if os.path.exists(sp):
        with open(sp) as f:
            r = list(csv.DictReader(f))
            if r: summary = r[0]

    # Index gather and compute by req_id (one row each per request).
    gather_by_req  = {r["req_id"]: r for r in gather_rows}
    compute_by_req = {r["req_id"]: r for r in compute_rows}

    # Group response packets by req_id; pick first/last recv per req.
    resp_by_req = defaultdict(list)
    for r in response_rows:
        resp_by_req[r["req_id"]].append(r)

    # Per-request lifecycle.
    forward_ns   = []
    gather_ns    = []
    queue_wait   = []
    compute_ns   = []
    return_ns    = []   # first response pkt's network return delay
    total_ns     = []   # emit → first response pkt recv
    total_full   = []   # emit → last response pkt recv (full burst arrived)
    L_in_obs     = []
    L_out_obs    = []
    n_complete   = 0
    n_with_resp  = 0
    for req_id, g in gather_by_req.items():
        c = compute_by_req.get(req_id)
        rsps = resp_by_req.get(req_id, [])
        if c is None or not rsps:
            continue
        n_complete += 1
        if rsps: n_with_resp += 1
        t_emit = g["t_emit_ns"]
        forward_ns.append(g["t_first_arrival_ns"] - t_emit)
        gather_ns.append(g["D_gather_ns"])
        queue_wait.append(c["T_queue_wait_ns"])
        compute_ns.append(c["T_compute_ns"])
        recv_times = [r["t_response_recv_ns"] for r in rsps]
        emit_resp  = rsps[0]["t_response_emit_ns"]
        return_ns.append(min(recv_times) - emit_resp)
        total_ns.append(min(recv_times) - t_emit)
        total_full.append(max(recv_times) - t_emit)
        L_in_obs.append(g["L_in"])
        L_out_obs.append(g["L_out_expected"])

    # Compute queue depth statistics (probe each request's queue depth at enter).
    # We can derive: at the time req X enters the queue, depth = number of
    # requests with t_queue_enter <= X.t_queue_enter < their t_compute_end.
    queue_depths = []
    sorted_c = sorted(compute_rows, key=lambda r: r["t_queue_enter_ns"])
    for i, r in enumerate(sorted_c):
        t_in = r["t_queue_enter_ns"]
        depth = sum(1 for s in sorted_c[:i]
                    if s["t_compute_end_ns"] > t_in)
        queue_depths.append(depth)

    # Analytic comparison.
    sched_rows = []
    if args.schedule and os.path.exists(args.schedule):
        with open(args.schedule) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 15:
                    sched_rows.append({
                        "L_in_mean":  float(parts[3]),
                        "L_out_mean": float(parts[7]),
                    })

    md = []
    md.append("# Phase C — full LLM request lifecycle result\n")
    md.append("End-to-end: GS LLMRequestApplication → fstate-routed UDP →\n"
              "compute-SAT GatherApplication → ComputeApplication FIFO →\n"
              "UDP response burst → GS LLMResponseSinkApplication.\n")

    md.append("## Totals\n")
    for k in ("tx_request_count", "tx_request_packets", "rx_request_packets",
              "gather_complete_count", "gather_timeout_count",
              "compute_complete_count", "response_recv_packets"):
        md.append(f"- {k} = **{summary.get(k, '?')}**")
    md.append("")

    md.append(f"### Completion: {n_complete} requests have all of "
              "(gather, compute, response) — i.e. completed the full lifecycle.")
    if int(summary.get("tx_request_count", 0)) > 0:
        rate = 100.0 * n_complete / int(summary["tx_request_count"])
        md.append(f"Completion rate: **{rate:.2f}%**")
    md.append("")

    md.append("## Per-stage latency  (ms)\n")
    md.append("| stage | n | min | p50 | mean | p95 | p99 | max |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    md.append(stat_row("T_forward (emit → gather first)",   stat(forward_ns)))
    md.append(stat_row("D_gather (gather first → last)",     stat(gather_ns)))
    md.append(stat_row("T_queue_wait (queue enter → svc)",   stat(queue_wait)))
    md.append(stat_row("T_compute (compute service)",        stat(compute_ns)))
    md.append(stat_row("T_return (resp emit → resp recv)",   stat(return_ns)))
    md.append(stat_row("T_total (emit → first resp recv)",   stat(total_ns)))
    md.append(stat_row("T_total_full (emit → last resp recv)", stat(total_full)))
    md.append("")

    md.append("## Compute queue depth (snapshot at each enqueue)\n")
    if queue_depths:
        sq = stat(queue_depths)
        md.append(f"- mean = **{sq['mean']:.2f}**")
        md.append(f"- p95  = {sq['p95']}")
        md.append(f"- max  = **{sq['max']}**")
        md.append(f"- (n_enqueues = {sq['n']})")
    md.append("")

    md.append("## Analytic estimate vs simulator\n")
    if sched_rows:
        r = sched_rows[0]
        L_in_m = r["L_in_mean"]; L_out_m = r["L_out_mean"]
        # From config (defaults).
        alpha = 100_000;  beta = 50_000;  gamma = 10_000_000
        # T_compute analytic.
        t_comp_an_ms = (alpha * L_in_m + beta * L_out_m + gamma) / 1e6
        md.append(f"- L_in_mean = {L_in_m},  L_out_mean = {L_out_m}")
        md.append(f"- analytic T_compute = α·L_in + β·L_out + γ = "
                  f"{alpha/1000:.0f}us·{L_in_m:.0f} + {beta/1000:.0f}us·{L_out_m:.0f} + "
                  f"{gamma/1e6:.1f}ms = **{t_comp_an_ms:.2f} ms**")
        if compute_ns:
            md.append(f"- simulator T_compute mean = "
                      f"**{sum(compute_ns)/len(compute_ns)/1e6:.2f} ms** "
                      f"(driven by *actual* sampled L_in / L_out per request)")
        # T_forward ≈ T_return because the route is the same and queuing is small.
        if forward_ns and return_ns:
            md.append(f"- mean T_forward = {sum(forward_ns)/len(forward_ns)/1e6:.2f} ms,  "
                      f"mean T_return = {sum(return_ns)/len(return_ns)/1e6:.2f} ms  "
                      "(should be ≈, same path forward + back)")
        # End-to-end analytic floor (no queueing, no GSL serialization).
        if forward_ns and return_ns and compute_ns:
            t_total_an = (sum(forward_ns) + sum(return_ns)) / (2*len(forward_ns)) + t_comp_an_ms * 1e6
            t_total_an_ms = t_total_an / 1e6
            mean_total = sum(total_ns) / len(total_ns) / 1e6
            md.append(f"- analytic T_total ≈ T_forward + T_compute + T_return ≈ "
                      f"**{t_total_an_ms:.2f} ms**")
            md.append(f"- simulator T_total mean (first resp recv) = **{mean_total:.2f} ms**  "
                      f"(gap = queueing wait)")
    else:
        md.append("(no schedule supplied; skip analytic comparison)")
    md.append("")

    md.append("## Stuck (timed-out) requests\n")
    md.append(f"- stuck count: **{len(stuck_rows)}**")
    if stuck_rows:
        md.append("First few stuck:")
        for r in stuck_rows[:5]:
            md.append(f"  - {r}")
    md.append("")

    ok = (n_complete == int(summary.get("tx_request_count", 0))
          and int(summary.get("gather_timeout_count", 0)) == 0)
    md.append("## Verdict\n")
    md.append(f"- every tx request completed full lifecycle? : `{n_complete} == "
              f"{summary.get('tx_request_count')} → {ok}`")
    md.append(f"- zero timeouts? : `{int(summary.get('gather_timeout_count', 0)) == 0}`")
    md.append(f"\n**Verdict: {'PASS' if ok else 'FAIL'}**\n")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(md))
    print(f"wrote {out_path}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
