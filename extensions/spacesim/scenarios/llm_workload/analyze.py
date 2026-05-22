#!/usr/bin/env python3
"""
Phase B LLM workload analyser — three-tier latency report.

The conceptual model is request -> token -> packet:

  - One LLM inference request decomposes into L_in tokens.
  - Each token is `bytes_per_token` bytes (default 4 B).
  - Tokens are packed into UDP packets of `packet_payload` bytes
    (default 1400 B).  One packet carries floor(1400 / 4) = 350 whole
    tokens, and a request with L_in tokens slices into
        N_pkt = ceil(L_in * 4 / 1400)
    packets. The last packet may carry fewer than 350 tokens.

Three latency tiers, computed per (src_gs, dst_compute_sat) flow:

  1. **per-packet latency** = recv_time_ns - t_emit_ns
        (one sample per UDP packet)

  2. **per-token latency** = the per-packet latency of the carrying
     packet, expanded by token count. Each packet contributes
     `tokens_in_packet` samples to the per-token distribution.
     If all requests have the same L_in this CDF is just per-packet
     scaled; if L_in varies, this CDF is *weighted by where the tokens
     actually live*.

  3. **per-request completion latency** = max(recv_time over all
     packets in the request) - t_emit_ns. This is "the moment the
     compute SAT has every token of the request in hand" -- exactly
     the trigger condition for Phase C's gather barrier + prefill.

Outputs (under <scenario>/):
  - result.md  — three tables + verdict + Phase C entry-point note
  - flows.csv  — flat per-flow stats for downstream tooling
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import defaultdict


GS_NAMES = {60: "Tokyo", 61: "Mumbai", 62: "Shanghai",
            63: "Sao-Paulo", 64: "NY"}


def load_summary(run_dir):
    p = os.path.join(run_dir, "logs_ns3", "llm_workload_summary.csv")
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        rows = list(csv.DictReader(f))
    return rows[0] if rows else {}


def load_packets(run_dir):
    out = []
    sink_dir = os.path.join(run_dir, "logs_ns3")
    for name in sorted(os.listdir(sink_dir)):
        if name.startswith("llm_workload_sink") and name.endswith(".csv"):
            with open(os.path.join(sink_dir, name)) as f:
                for row in csv.DictReader(f):
                    out.append({k: int(v) for k, v in row.items()})
    return out


def load_schedule(scenario_dir):
    """Pull (lambda, L_mean, bytes_per_token, packet_payload) per (src,dst)."""
    rows = {}
    with open(os.path.join(scenario_dir, "llm_workload_schedule.csv")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            src = int(parts[0]); dst = int(parts[1])
            rows[(src, dst)] = {
                "lambda":          float(parts[2]),
                "L_mean":          float(parts[3]),
                "bytes_per_token": int(parts[7]),
                "packet_payload":  int(parts[8]),
            }
    return rows


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


def fmt_ms(ns):
    if ns is None or (isinstance(ns, float) and math.isnan(ns)):
        return "—"
    return f"{ns / 1e6:.2f}"


def fmt_row(name, stats):
    if stats is None:
        return f"| {name} | — | — | — | — | — | — |"
    return (f"| {name} | {stats['n']} | "
            f"{fmt_ms(stats['min'])} | {fmt_ms(stats['p50'])} | "
            f"{fmt_ms(stats['mean'])} | {fmt_ms(stats['p95'])} | "
            f"{fmt_ms(stats['max'])} |")


def tokens_in_packet(packet_id, total_pkts, L_in, bytes_per_token, packet_payload):
    """Number of whole tokens carried by this packet.

    First (total_pkts-1) packets each carry floor(packet_payload /
    bytes_per_token) tokens; the last packet carries the remainder.
    """
    per_full = packet_payload // bytes_per_token
    if packet_id < total_pkts - 1:
        return per_full
    return max(L_in - per_full * (total_pkts - 1), 0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario-dir", required=True,
                   help="Path to scenarios/llm_workload/")
    p.add_argument("--out", default=None,
                   help="Output markdown path (default: <scenario-dir>/result.md)")
    args = p.parse_args()

    scenario_dir = os.path.abspath(args.scenario_dir)
    run_dir = os.path.join(scenario_dir, "run")
    out_path = args.out or os.path.join(scenario_dir, "result.md")
    flows_csv = os.path.join(scenario_dir, "flows.csv")

    summary  = load_summary(run_dir)
    packets  = load_packets(run_dir)
    schedule = load_schedule(scenario_dir)

    tx_req = int(summary.get("tx_request_count", 0))
    tx_pkt = int(summary.get("tx_packet_count",  0))
    rx_pkt = int(summary.get("rx_packet_count",  0))
    delivery = rx_pkt / tx_pkt if tx_pkt > 0 else 0.0

    by_flow = defaultdict(lambda: defaultdict(list))
    for pkt in packets:
        key = (pkt["src_node_id"], pkt["recv_node_id"])
        by_flow[key][pkt["req_id"]].append(pkt)

    rows_md = []
    flow_csv_rows = []
    flow_order = sorted(by_flow.keys())

    for (src, dst) in flow_order:
        reqs = by_flow[(src, dst)]
        sched = schedule.get((src, dst), {})
        bpt = sched.get("bytes_per_token", 4)
        ppl = sched.get("packet_payload",  1400)

        per_pkt_lat = []
        per_tok_lat = []
        per_req_lat = []
        gather_wait = []

        for req_id, pkts in reqs.items():
            t_emit = pkts[0]["t_emit_ns"]
            total_pkts = pkts[0]["total_pkts"]
            L_in = pkts[0]["L_in"]
            recv_times = [p["recv_time_ns"] for p in pkts]
            for pk in pkts:
                lat = pk["recv_time_ns"] - t_emit
                per_pkt_lat.append(lat)
                tok_count = tokens_in_packet(pk["packet_id"], total_pkts,
                                             L_in, bpt, ppl)
                per_tok_lat.extend([lat] * tok_count)
            per_req_lat.append(max(recv_times) - t_emit)
            if len(recv_times) >= 2:
                gather_wait.append(max(recv_times) - min(recv_times))

        sb_pkt = stat_block(per_pkt_lat)
        sb_tok = stat_block(per_tok_lat)
        sb_req = stat_block(per_req_lat)
        sb_gw  = stat_block(gather_wait)

        rows_md.append({
            "flow_label": f"{GS_NAMES.get(src, f'node{src}')} → C{dst}",
            "src": src, "dst": dst,
            "lambda": sched.get("lambda"),
            "L_mean": sched.get("L_mean"),
            "n_req":  len(reqs),
            "n_pkts": sum(len(v) for v in reqs.values()),
            "sb_pkt": sb_pkt, "sb_tok": sb_tok,
            "sb_req": sb_req, "sb_gw":  sb_gw,
        })

        flow_csv_rows.append({
            "src": src, "dst": dst,
            "src_name": GS_NAMES.get(src, f"node{src}"),
            "lambda":   sched.get("lambda"),
            "L_mean":   sched.get("L_mean"),
            "n_req":    len(reqs),
            "n_pkts":   sum(len(v) for v in reqs.values()),
            "pkt_mean_ms": (sb_pkt["mean"] / 1e6) if sb_pkt else None,
            "pkt_p95_ms":  (sb_pkt["p95"]  / 1e6) if sb_pkt else None,
            "tok_mean_ms": (sb_tok["mean"] / 1e6) if sb_tok else None,
            "req_mean_ms": (sb_req["mean"] / 1e6) if sb_req else None,
            "req_p95_ms":  (sb_req["p95"]  / 1e6) if sb_req else None,
            "gather_max_ms": (sb_gw["max"] / 1e6) if sb_gw else None,
        })

    md = []
    md.append("# Phase B LLM workload — three-tier latency report\n")
    md.append("**Scenario** GS → compute-SAT inference traffic. Each LLM inference\n"
              "request is decomposed `request → tokens → packets`:\n")
    md.append("- 1 request = `L_in` tokens (sampled from clipped Normal at the GS app)")
    md.append("- 1 token   = `bytes_per_token` bytes (4 B)")
    md.append("- 1 packet payload = 1400 B = 350 tokens")
    md.append("- N_pkt per request = `ceil(L_in × 4 / 1400)`")
    md.append("")
    md.append("Three latency tiers are reported per flow:")
    md.append("")
    md.append("| tier | meaning |")
    md.append("|---|---|")
    md.append("| **per-packet** | `recv_time_ns − t_emit_ns` for each UDP packet |")
    md.append("| **per-token**  | the per-packet latency of the carrying packet, "
              "expanded by token count (each packet contributes `tokens_in_packet` samples) |")
    md.append("| **per-request**| `max(recv_time over all packets in request) − t_emit_ns`. "
              "This is when the compute SAT has the *full* request — Phase C's gather completion. |")
    md.append("")
    md.append("Latencies in **ms**.\n")

    md.append("## Totals\n")
    md.append(f"- tx_request_count = **{tx_req}**")
    md.append(f"- tx_packet_count  = **{tx_pkt}**")
    md.append(f"- rx_packet_count  = **{rx_pkt}**  ({delivery * 100:.2f}% delivered)")
    md.append("")

    def section(title, key, header):
        md.append(f"## {title}\n")
        md.append(header)
        md.append("|---|---:|---:|---:|---:|---:|---:|")
        for r in rows_md:
            md.append(fmt_row(r["flow_label"], r[key]))
        md.append("")

    head = "| flow | n_samples | min | p50 | mean | p95 | max |"
    section("Per-packet latency  (one sample per UDP packet)",
            "sb_pkt", head)
    section("Per-token latency  (per-packet latency × tokens-in-packet)",
            "sb_tok", head)
    section("Per-request completion latency  (gather complete on compute SAT)",
            "sb_req", head)
    section("Within-request gather wait  (max − min recv within req; "
            "Phase C barrier ground truth)",
            "sb_gw", head)

    md.append("## Headline numbers per flow\n")
    md.append("| flow | λ (req/s) | L̄_in | reqs | pkts | "
              "pkt mean | tok mean | **req mean** | req p95 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows_md:
        md.append(
            f"| {r['flow_label']} | {r['lambda']:.0f} | {r['L_mean']:.0f} | "
            f"{r['n_req']} | {r['n_pkts']} | "
            f"{fmt_ms(r['sb_pkt']['mean'])} | {fmt_ms(r['sb_tok']['mean'])} | "
            f"**{fmt_ms(r['sb_req']['mean'])}** | {fmt_ms(r['sb_req']['p95'])} |"
        )
    md.append("")

    md.append("## Interpretation\n")
    md.append("- **per-packet vs per-token**: when L_in is balanced across requests, "
              "the two CDFs lie on top of each other up to a constant factor "
              "(token count). When requests vary a lot in length, the token CDF "
              "up-weights latencies seen by tokens of *bigger* requests "
              "(because those requests contribute more tokens).")
    md.append("- **per-packet vs per-request**: the gap between these two CDFs is "
              "exactly the **gather wait** — how much extra time the compute SAT "
              "has to wait after the first packet before the whole request is in. "
              "In this scenario the gap is ~1.14 ms × (N_pkt − 1) — GSL "
              "serialization at 10 Mbps dominates.")
    md.append("- **per-request** is the latency the LLM application *actually* "
              "experiences: the prefill stage can't begin until every token is "
              "ashore. That's the number to optimize against.")
    md.append("")

    ok = delivery >= 0.95 and rx_pkt > 0
    md.append("## Verdict\n")
    md.append(f"- delivery ≥ 95% ? : `{delivery * 100:.2f}%` → `{delivery >= 0.95}`")
    md.append(f"- at least one packet received? : `{rx_pkt > 0}`")
    md.append(f"\n**Verdict: {'PASS' if ok else 'FAIL'}**\n")

    with open(out_path, "w") as f:
        f.write("\n".join(md))
    print(f"wrote {out_path}")

    with open(flows_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(flow_csv_rows[0].keys()))
        writer.writeheader()
        for r in flow_csv_rows:
            writer.writerow(r)
    print(f"wrote {flows_csv}")

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())