#!/usr/bin/env python3
"""
Three-panel CDF: per-packet / per-token / per-request latency.

Each panel overlays 5 curves, one per (src_gs → dst_compute_sat) flow.

  Panel 1 (per-packet)   : recv_time_ns − t_emit_ns per UDP packet.
                            One sample per packet.
  Panel 2 (per-token)    : same latency, but each packet contributes
                            `tokens_in_packet` samples. Token-weighted —
                            tokens of bigger requests show up more.
  Panel 3 (per-request)  : max(recv_time over packets in request) −
                            t_emit_ns. One sample per request. This is
                            the latency the LLM application sees.

The vertical dashed line on panel 3 marks 1400B@10Mbps = 1.144 ms — the
serialization gap that the request-completion latency has to absorb
once per extra packet.
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


def tokens_in_packet(packet_id, total_pkts, L_in,
                     bytes_per_token=4, packet_payload=1400):
    per_full = packet_payload // bytes_per_token
    if packet_id < total_pkts - 1:
        return per_full
    return max(L_in - per_full * (total_pkts - 1), 0)


def load_packets(run_logs_dir):
    out = []
    for name in sorted(os.listdir(run_logs_dir)):
        if name.startswith("llm_workload_sink") and name.endswith(".csv"):
            with open(os.path.join(run_logs_dir, name)) as f:
                for row in csv.DictReader(f):
                    out.append({k: int(v) for k, v in row.items()})
    return out


def cdf_data(samples_ns):
    if not samples_ns:
        return np.array([]), np.array([])
    s = np.sort(np.asarray(samples_ns, dtype=np.int64))
    return s / 1e6, np.arange(1, s.size + 1) / s.size


def main():
    run_logs = os.path.join(HERE, "run", "logs_ns3")
    pkts = load_packets(run_logs)
    if not pkts:
        print("no packets", file=sys.stderr); return 1

    by_flow = defaultdict(lambda: defaultdict(list))
    for p in pkts:
        by_flow[(p["src_node_id"], p["recv_node_id"])][p["req_id"]].append(p)

    fig, (ax_p, ax_t, ax_r) = plt.subplots(1, 3, figsize=(17, 5))

    flows = sorted(by_flow.keys())
    for i, (src, dst) in enumerate(flows):
        reqs = by_flow[(src, dst)]
        pkt_lat = []
        tok_lat = []
        req_lat = []
        for req_id, ps in reqs.items():
            t_emit = ps[0]["t_emit_ns"]
            total_pkts = ps[0]["total_pkts"]
            L_in = ps[0]["L_in"]
            recvs = [p["recv_time_ns"] for p in ps]
            for p in ps:
                lat = p["recv_time_ns"] - t_emit
                pkt_lat.append(lat)
                tok_lat.extend([lat] * tokens_in_packet(
                    p["packet_id"], total_pkts, L_in))
            req_lat.append(max(recvs) - t_emit)

        c = FLOW_COLORS[i % len(FLOW_COLORS)]
        label = f"{GS_NAMES.get(src, f'n{src}')} → C{dst}"

        x, y = cdf_data(pkt_lat)
        ax_p.plot(x, y, color=c, linewidth=1.8, label=f"{label}  (n={x.size})")

        x, y = cdf_data(tok_lat)
        ax_t.plot(x, y, color=c, linewidth=1.8, label=f"{label}  (n={x.size:,})")

        x, y = cdf_data(req_lat)
        ax_r.plot(x, y, color=c, linewidth=1.8, label=f"{label}  (n={x.size})")

    for ax, title, xlabel in [
        (ax_p, "Per-packet latency",  "per-packet end-to-end (ms)"),
        (ax_t, "Per-token latency\n(per-pkt × tokens_in_packet)", "per-token end-to-end (ms)"),
        (ax_r, "Per-request completion\n(max pkt recv − t_emit)", "per-request completion (ms)"),
    ]:
        ax.set_xlabel(xlabel); ax.set_ylabel("CDF")
        ax.set_title(title, fontsize=10)
        ax.set_ylim(0, 1.0)
        ax.grid(True, linestyle=":", alpha=0.6)
        ax.legend(fontsize=8, loc="lower right")

    # Reference: GSL serialization gap.
    ax_r.axvline(1.144, color="grey", linestyle="--", alpha=0.6,
                 linewidth=0.8, label="_nolegend_")

    fig.suptitle(
        "Phase B LLM workload — request → token → packet latency CDFs\n"
        "5 concurrent flows, 243 reqs / 538 tx pkts / 536 rx pkts "
        "(99.63% delivered)",
        fontsize=11,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out = os.path.join(PLOTS, "latency_cdf.png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())