#!/usr/bin/env python3
"""
Gantt-style timeline of LLM requests.

For every (src GS → dst compute) flow, draw each request as a horizontal
segment from `t_emit` (first packet's emit time) to `max recv_time`
(last packet arrived). The vertical position is the row number within
the flow.

Output: plots/request_timeline.png — five-row figure, one row per flow,
shared X axis (simulated time).
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


def load_packets(run_logs_dir):
    out = []
    for name in sorted(os.listdir(run_logs_dir)):
        if name.startswith("llm_workload_sink") and name.endswith(".csv"):
            with open(os.path.join(run_logs_dir, name)) as f:
                for row in csv.DictReader(f):
                    out.append({k: int(v) for k, v in row.items()})
    return out


def main():
    run_logs = os.path.join(HERE, "run", "logs_ns3")
    packets = load_packets(run_logs)

    flows_pkts = defaultdict(list)
    for p in packets:
        flows_pkts[(p["src_node_id"], p["recv_node_id"])].append(p)

    flow_keys = sorted(flows_pkts.keys())
    n_flows = len(flow_keys)

    fig, axes = plt.subplots(n_flows, 1, figsize=(13, 1.6 * n_flows + 0.6),
                             sharex=True)
    if n_flows == 1:
        axes = [axes]

    for fi, (src, dst) in enumerate(flow_keys):
        ax = axes[fi]
        ps = flows_pkts[(src, dst)]
        by_req = defaultdict(list)
        for p in ps:
            by_req[p["req_id"]].append(p)
        # Per-request: t_emit (uniform across packets in the request) and
        # the recv times of each packet.
        reqs = []
        for req_id, lst in sorted(by_req.items()):
            t_emit_s = lst[0]["t_emit_ns"] / 1e9
            recv_s = sorted(p["recv_time_ns"] / 1e9 for p in lst)
            reqs.append((req_id, t_emit_s, recv_s,
                         lst[0]["total_pkts"], lst[0]["L_in"]))

        color = FLOW_COLORS[fi % len(FLOW_COLORS)]
        for y, (req_id, t_emit, recvs, totalpkt, L_in) in enumerate(reqs):
            t_last = recvs[-1]
            # Travel segment: emit → last packet's recv.
            ax.plot([t_emit, t_last], [y, y], color=color,
                    linewidth=1.4, alpha=0.7, solid_capstyle="butt")
            # First packet recv = dot.
            ax.scatter([recvs[0]], [y], s=10, color=color, zorder=4)
            # Last packet recv = larger marker if multi-pkt.
            if len(recvs) > 1:
                ax.scatter([t_last], [y], s=24, marker="s",
                           edgecolor="black", facecolor=color,
                           linewidths=0.4, zorder=5)

        gs_name = GS_NAMES.get(src, f"node{src}")
        ax.set_ylabel(f"{gs_name}\n→ C{dst}\n({len(reqs)} req)",
                      rotation=0, ha="right", va="center", fontsize=9)
        ax.set_ylim(-0.5, max(len(reqs), 1) - 0.5)
        ax.grid(True, axis="x", linestyle=":", alpha=0.5)
        ax.set_yticks([])

    axes[-1].set_xlabel("simulated time (s)")
    axes[0].set_title(
        "Phase B LLM workload — request lifelines  "
        "(line = emit→last-pkt-recv;  ● = first pkt recv;  ■ = last pkt recv if N_pkt>1)",
        fontsize=10,
    )

    fig.tight_layout()
    out = os.path.join(PLOTS, "request_timeline.png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
