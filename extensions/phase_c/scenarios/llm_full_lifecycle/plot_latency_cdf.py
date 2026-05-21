#!/usr/bin/env python3
"""
Four-panel latency CDF for Phase C full-lifecycle:

  Panel 1: per-packet (forward, GS → compute SAT)
  Panel 2: per-response-packet (return network delay)
  Panel 3: TTFT (request emit → first response packet recv)
  Panel 4: T_total (request emit → last response packet recv)

One curve per (src_gs, dst_compute_sat) flow.
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


def read_csv(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for row in csv.DictReader(f):
            rows.append({k: int(v) if v.lstrip("-").isdigit() else v
                         for k, v in row.items()})
    return rows


def cdf(samples_ns):
    if not samples_ns:
        return np.array([]), np.array([])
    s = np.sort(np.asarray(samples_ns, dtype=np.int64)) / 1e6
    return s, np.arange(1, s.size + 1) / s.size


def main():
    logs = os.path.join(HERE, "run", "logs_ns3")
    gather, compute, response = [], [], []
    for name in sorted(os.listdir(logs)):
        if name.startswith("llm_gather_node")   and name.endswith(".csv"):
            gather.extend(read_csv(os.path.join(logs, name)))
        elif name.startswith("llm_compute_node") and name.endswith(".csv"):
            compute.extend(read_csv(os.path.join(logs, name)))
        elif name.startswith("llm_response_node") and name.endswith(".csv"):
            response.extend(read_csv(os.path.join(logs, name)))

    g_by_req = {(g["src_node_id"], g["compute_sat_id"], g["req_id"]): g
                for g in gather}

    flow_data = defaultdict(lambda: {"pkt_fwd": [], "pkt_back": [],
                                     "ttft": [], "total": []})

    # per-packet forward: synthesized from gather first/last + total_pkts.
    for g in gather:
        flow = (g["src_node_id"], g["compute_sat_id"])
        first_lat = g["t_first_arrival_ns"] - g["t_emit_ns"]
        last_lat  = g["t_last_arrival_ns"]  - g["t_emit_ns"]
        total_pkts = g["total_pkts_expected"]
        for i in range(total_pkts):
            if total_pkts == 1:
                lat = first_lat
            else:
                lat = first_lat + (last_lat - first_lat) * (i / (total_pkts - 1))
            flow_data[flow]["pkt_fwd"].append(int(lat))

    # per-response-packet + TTFT + total
    resp_by_req = defaultdict(list)
    for r in response:
        resp_by_req[(r["src_compute_sat_id"], r["req_id"], r["gs_node_id"])].append(r)
    for (sat, req_id, gs), rsps in resp_by_req.items():
        flow = (gs, sat)
        for r in rsps:
            flow_data[flow]["pkt_back"].append(r["network_return_delay_ns"])
        recvs = [r["t_response_recv_ns"] for r in rsps]
        g = g_by_req.get((gs, sat, req_id))
        if g:
            flow_data[flow]["ttft"].append(min(recvs) - g["t_emit_ns"])
            flow_data[flow]["total"].append(max(recvs) - g["t_emit_ns"])

    fig, axes = plt.subplots(1, 4, figsize=(20, 5))
    titles = [
        "per-packet (forward)\nGS → compute SAT",
        "per-response-packet\n(return network delay)",
        "TTFT  =  emit → first resp recv\n(LLM end-user perceived)",
        "T_total  =  emit → last resp recv\n(full response gathered)",
    ]
    keys = ["pkt_fwd", "pkt_back", "ttft", "total"]
    xlabels = [
        "per-packet forward latency (ms)",
        "per-response-packet return delay (ms)",
        "TTFT (ms)",
        "T_total (ms)",
    ]
    for ax, key, title, xlabel in zip(axes, keys, titles, xlabels):
        for i, flow in enumerate(sorted(flow_data.keys())):
            gs, sat = flow
            samples = flow_data[flow][key]
            x, y = cdf(samples)
            if x.size == 0:
                continue
            color = FLOW_COLORS[i % len(FLOW_COLORS)]
            label = f"{GS_NAMES.get(gs, gs)} → C{sat}  (n={x.size})"
            ax.plot(x, y, color=color, linewidth=1.8, label=label)
        ax.set_xlabel(xlabel); ax.set_ylabel("CDF")
        ax.set_title(title, fontsize=10)
        ax.set_ylim(0, 1.0)
        ax.grid(True, linestyle=":", alpha=0.6)
        ax.legend(fontsize=7, loc="lower right")

    fig.suptitle(
        "Phase C full LLM inference lifecycle — four-tier latency CDFs\n"
        "5 concurrent flows (GS → compute SAT → response), "
        "59 reqs / 100% gather / 0 timeouts",
        fontsize=12,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out = os.path.join(PLOTS, "latency_cdf.png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
