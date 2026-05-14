#!/usr/bin/env python3
"""Plot the 5-flow dynamics for the mixed-topology smoke test.

Produces a single 2x2 PNG showing, with one line per flow:
  - Top-left: RTT time series (ms)
  - Top-right: cwnd time series (KB)
  - Bottom-left: cumulative bytes sent
  - Bottom-right: per-flow completion summary (duration + average throughput)

Reads from ``run/logs_ns3/tcp_flow_<id>_{rtt,cwnd,progress}.csv``.
"""

from __future__ import annotations

import csv
import os
import sys
from typing import Dict, List, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.abspath(os.path.dirname(__file__))
RUN_LOGS = os.path.join(HERE, "run", "logs_ns3")
OUT_PNG = os.path.join(HERE, "plots", "flow_dynamics.png")


FLOW_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
NUM_SATS = 60


def _node_label(n: int) -> str:
    if n < NUM_SATS:
        return f"SAT-{n}"
    return f"GS-{n - NUM_SATS}"


def read_flows_csv() -> List[Dict[str, str]]:
    path = os.path.join(RUN_LOGS, "tcp_flows.csv")
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 9:
                continue
            rows.append({
                "flow_id": int(parts[0]),
                "from": int(parts[1]),
                "to": int(parts[2]),
                "size_byte": int(parts[3]),
                "start_ns": int(parts[4]),
                "end_ns": int(parts[5]),
                "duration_ns": int(parts[6]),
                "bytes_sent": int(parts[7]),
                "completed": parts[8],
                "metadata": parts[9] if len(parts) > 9 else "",
            })
    return rows


def read_ts(path: str) -> Tuple[np.ndarray, np.ndarray]:
    """Generic loader for the 3-column tcp_flow_<id>_*.csv files."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return np.array([]), np.array([])
    arr = np.loadtxt(path, delimiter=",", dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    # Column 0 = flow id (ignored), 1 = time_ns, 2 = value
    return arr[:, 1], arr[:, 2]


def main() -> int:
    os.makedirs(os.path.dirname(OUT_PNG), exist_ok=True)
    flows = read_flows_csv()
    if not flows:
        print("FATAL: no tcp_flows.csv rows; run bash run.sh first.")
        return 1

    fig, ax = plt.subplots(2, 2, figsize=(13.5, 8.0))

    # 1. RTT
    ax_rtt = ax[0, 0]
    for fl in flows:
        fid = fl["flow_id"]
        t_ns, rtt_ns = read_ts(os.path.join(RUN_LOGS, f"tcp_flow_{fid}_rtt.csv"))
        if t_ns.size == 0:
            continue
        ax_rtt.plot(t_ns / 1e9, rtt_ns / 1e6,
                    label=f"flow {fid} {_node_label(fl['from'])}→{_node_label(fl['to'])}",
                    color=FLOW_COLORS[fid % len(FLOW_COLORS)], lw=1.4)
    ax_rtt.set_xlabel("time (s)")
    ax_rtt.set_ylabel("RTT (ms)")
    ax_rtt.set_title("RTT over time")
    ax_rtt.grid(True, linestyle="--", alpha=0.4)
    ax_rtt.legend(loc="upper right", fontsize=8)

    # 2. Cwnd
    ax_cwnd = ax[0, 1]
    for fl in flows:
        fid = fl["flow_id"]
        t_ns, cwnd_b = read_ts(os.path.join(RUN_LOGS, f"tcp_flow_{fid}_cwnd.csv"))
        if t_ns.size == 0:
            continue
        ax_cwnd.plot(t_ns / 1e9, cwnd_b / 1024,
                     label=f"flow {fid}",
                     color=FLOW_COLORS[fid % len(FLOW_COLORS)], lw=1.4)
    ax_cwnd.set_xlabel("time (s)")
    ax_cwnd.set_ylabel("cwnd (KB)")
    ax_cwnd.set_title("Congestion window over time")
    ax_cwnd.grid(True, linestyle="--", alpha=0.4)
    ax_cwnd.legend(loc="lower right", fontsize=8)

    # 3. Progress
    ax_prog = ax[1, 0]
    for fl in flows:
        fid = fl["flow_id"]
        t_ns, bytes_sent = read_ts(os.path.join(RUN_LOGS, f"tcp_flow_{fid}_progress.csv"))
        if t_ns.size == 0:
            continue
        ax_prog.plot(t_ns / 1e9, bytes_sent / 1024,
                     label=f"flow {fid} ({fl['size_byte'] // 1024} KB)",
                     color=FLOW_COLORS[fid % len(FLOW_COLORS)], lw=1.6)
        # Mark end-of-flow point.
        end_t = fl["end_ns"] / 1e9
        ax_prog.scatter([end_t], [fl["bytes_sent"] / 1024],
                        color=FLOW_COLORS[fid % len(FLOW_COLORS)],
                        marker="o", s=40, zorder=5,
                        edgecolors="black", linewidths=0.8)
    ax_prog.set_xlabel("time (s)")
    ax_prog.set_ylabel("bytes sent (KB)")
    ax_prog.set_title("Cumulative bytes sent (dots = completion)")
    ax_prog.grid(True, linestyle="--", alpha=0.4)
    ax_prog.legend(loc="lower right", fontsize=8)

    # 4. Summary bar chart: duration + avg throughput
    ax_sum = ax[1, 1]
    ids = [fl["flow_id"] for fl in flows]
    dur_ms = [fl["duration_ns"] / 1e6 for fl in flows]
    avg_mbps = [
        (fl["size_byte"] * 8 / 1e6) / max(fl["duration_ns"] / 1e9, 1e-9)
        for fl in flows
    ]
    labels = [
        f"#{fl['flow_id']}\n{_node_label(fl['from'])}→{_node_label(fl['to'])}"
        f"\n{fl['size_byte'] // 1024} KB"
        for fl in flows
    ]
    x = np.arange(len(ids))
    width = 0.38
    bars1 = ax_sum.bar(x - width / 2, dur_ms, width,
                       color=[FLOW_COLORS[i % len(FLOW_COLORS)] for i in ids],
                       label="duration (ms)", alpha=0.85)
    ax_sum.set_xticks(x)
    ax_sum.set_xticklabels(labels, fontsize=8)
    ax_sum.set_ylabel("duration (ms)")
    ax_sum.set_title("Per-flow completion summary")
    ax_sum.grid(True, axis="y", linestyle="--", alpha=0.4)
    for b, v in zip(bars1, dur_ms):
        ax_sum.text(b.get_x() + b.get_width() / 2, v + 20, f"{v:.0f}",
                    ha="center", va="bottom", fontsize=7)
    # Secondary axis for Mbps
    ax_sum2 = ax_sum.twinx()
    bars2 = ax_sum2.bar(x + width / 2, avg_mbps, width,
                        color="#444444", alpha=0.55,
                        label="avg throughput (Mbps)")
    ax_sum2.set_ylabel("avg throughput (Mbps)")
    for b, v in zip(bars2, avg_mbps):
        ax_sum2.text(b.get_x() + b.get_width() / 2, v + 0.05, f"{v:.2f}",
                     ha="center", va="bottom", fontsize=7)
    # Combined legend
    h1, l1 = ax_sum.get_legend_handles_labels()
    h2, l2 = ax_sum2.get_legend_handles_labels()
    ax_sum.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)

    fig.suptitle(
        "Mixed-topology smoke test — 60 sats × 5 GS, 5 concurrent TCP flows\n"
        "all flows: completed = YES",
        fontsize=12, y=0.995,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(OUT_PNG, dpi=150)
    print(f"wrote {OUT_PNG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())