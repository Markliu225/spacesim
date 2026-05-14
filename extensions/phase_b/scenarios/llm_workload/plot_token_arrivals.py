#!/usr/bin/env python3
"""
Time-series visualization of LLM tokens arriving at each compute SAT.

Two-panel figure:

  Top panel: cumulative tokens delivered at each compute SAT over time.
             Five monotonically-increasing curves, one per compute SAT.
             Slope ≈ tokens / second received from the corresponding GS.

  Bottom panel: instantaneous "tokens-per-100-ms" rate at each compute
             SAT (binned histogram). Reveals burstiness — a Poisson
             request arrival process at λ req/s produces an
             exponentially-distributed inter-arrival time, so the
             tokens-per-bin curve is jittery, not flat.

Both panels share an x-axis (simulated time, s).

Output: plots/token_arrivals.png
"""

from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


HERE = os.path.abspath(os.path.dirname(__file__))
PLOTS = os.path.join(HERE, "plots")
os.makedirs(PLOTS, exist_ok=True)

GS_NAMES = {60: "Tokyo", 61: "Mumbai", 62: "Shanghai",
            63: "Sao-Paulo", 64: "NY"}
FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]


def load_schedule():
    rows = []
    with open(os.path.join(HERE, "llm_workload_schedule.csv")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            rows.append({
                "src":  int(parts[0]),
                "dst":  int(parts[1]),
                "bpt":  int(parts[7]),
                "ppl":  int(parts[8]),
                "lam":  float(parts[2]),
                "Lm":   float(parts[3]),
            })
    return rows


def tokens_in_packet(L_in, packet_id, total_pkts, bpt, ppl):
    max_tpp = ppl // bpt
    if packet_id < total_pkts - 1:
        return max_tpp
    return L_in - (total_pkts - 1) * max_tpp


def load_packets():
    out = []
    log_dir = os.path.join(HERE, "run", "logs_ns3")
    for name in sorted(os.listdir(log_dir)):
        if name.startswith("llm_workload_sink") and name.endswith(".csv"):
            with open(os.path.join(log_dir, name)) as f:
                for row in csv.DictReader(f):
                    out.append({k: int(v) for k, v in row.items()})
    return out


def main():
    schedule = load_schedule()
    sched_by_pair = {(s["src"], s["dst"]): s for s in schedule}
    packets = load_packets()

    # Compute (time, token_count) events per flow.
    events_by_flow = defaultdict(list)
    for p in packets:
        key = (p["src_node_id"], p["recv_node_id"])
        sch = sched_by_pair.get(key)
        if sch is None:
            continue
        tok = tokens_in_packet(p["L_in"], p["packet_id"],
                                p["total_pkts"], sch["bpt"], sch["ppl"])
        events_by_flow[key].append((p["recv_time_ns"] / 1e9, tok))
    for k in events_by_flow:
        events_by_flow[k].sort()

    flows = sorted(events_by_flow.keys())
    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(13, 7), sharex=True)

    # === Top: cumulative tokens ===
    max_t = 5.0
    for i, key in enumerate(flows):
        src, dst = key
        evs = events_by_flow[key]
        if not evs:
            continue
        times = [e[0] for e in evs]
        cum   = np.cumsum([e[1] for e in evs])
        # Prepend a zero point so the curve starts at the origin.
        times = [0.0] + times
        cum_arr = np.concatenate(([0], cum))
        color = FLOW_COLORS[i % len(FLOW_COLORS)]
        gs_name = GS_NAMES.get(src, str(src))
        sch = sched_by_pair[key]
        ax_top.step(times, cum_arr, where="post", color=color, linewidth=1.8,
                    label=f"{gs_name} → C{dst}  (λ={sch['lam']:.0f}, "
                          f"L̄={sch['Lm']:.0f} → {cum[-1]:,} tokens)")
        # final-value annotation at the right edge.
        ax_top.text(times[-1] + 0.03, cum_arr[-1], f"{int(cum_arr[-1]):,}",
                    color=color, fontsize=8, va="center")

    ax_top.set_ylabel("cumulative tokens delivered\nat compute SAT")
    ax_top.set_title(
        "Top: cumulative tokens delivered — each curve grows by the token "
        "count of every packet arriving at its compute SAT",
        fontsize=11,
    )
    ax_top.grid(True, linestyle=":", alpha=0.55)
    ax_top.legend(fontsize=8, loc="upper left")

    # === Bottom: tokens per 100 ms bin ===
    bin_size = 0.1  # s
    bins = np.arange(0, max_t + bin_size, bin_size)
    bottom_lines = []
    for i, key in enumerate(flows):
        evs = events_by_flow[key]
        ts = np.array([e[0] for e in evs])
        toks = np.array([e[1] for e in evs])
        hist, _ = np.histogram(ts, bins=bins, weights=toks)
        color = FLOW_COLORS[i % len(FLOW_COLORS)]
        src, dst = key
        ax_bot.step(bins[:-1], hist, where="post", color=color,
                    linewidth=1.5, alpha=0.85,
                    label=f"{GS_NAMES.get(src, src)} → C{dst}")
        bottom_lines.append(hist)

    ax_bot.set_xlabel("simulated time (s)")
    ax_bot.set_ylabel("tokens / 100 ms bin")
    ax_bot.set_title(
        "Bottom: instantaneous arrival rate (tokens delivered per 100 ms "
        "window) — burstiness reflects Poisson request arrivals",
        fontsize=11,
    )
    ax_bot.grid(True, linestyle=":", alpha=0.55)
    ax_bot.legend(fontsize=8, loc="upper left", ncol=2)

    ax_bot.set_xlim(0, max_t)

    fig.suptitle(
        "Phase B — token arrivals at compute SATs over the 5 s window\n"
        "(243 reqs / 538 tx pkts / 536 rx pkts / 144,732 tokens delivered)",
        fontsize=11,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.94])

    out = os.path.join(PLOTS, "token_arrivals.png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
