"""
Parse Phase C's per-run log directory into a per-request DataFrame.

Phase C emits five CSVs under ``<run_dir>/logs_ns3/`` — three of which
are needed to reconstruct a full request lifecycle (the other two are
the workload summary and a list of "stuck" / timed-out requests):

  llm_gather_node<sat>.csv     # per-compute-SAT
      req_id, compute_sat_id, t_first_arrival_ns, t_last_arrival_ns,
      D_gather_ns, total_pkts_expected, total_pkts_received,
      src_node_id, L_in, L_out_expected, t_emit_ns      ← keystone field

  llm_compute_node<sat>.csv    # per-compute-SAT
      req_id, compute_sat_id, t_queue_enter_ns,
      t_compute_start_ns, t_compute_end_ns,
      T_compute_ns, T_queue_wait_ns, L_in, L_out

  llm_response_node<gs>.csv    # per-GS, multiple rows per req_id
      req_id, gs_node_id, response_pkt_id, total_response_pkts,
      t_response_emit_ns, t_response_recv_ns,
      network_return_delay_ns, src_compute_sat_id, L_in, L_out

Joining by ``req_id`` reconstructs the lifecycle. Several columns are
derived (e.g. ``T_uplink_ns``, ``T_total_ns``); see
:func:`build_lifecycle_df` for the full list.

Why we don't trust ``compute_sat_id`` as a join key: a request may be
gathered at one SAT and computed at the same SAT (Phase C does not yet
do compute-side load balancing), so ``compute_sat_id`` is consistent
across logs for now -- but ``req_id`` is the contractually unique key.
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


# --- Loader helpers -------------------------------------------------------


def _glob_csv(logs_dir: Path, prefix: str) -> List[Path]:
    """Find all ``llm_<prefix>_node*.csv`` under ``logs_dir``."""
    return sorted(Path(p) for p in glob.glob(str(logs_dir / f"llm_{prefix}_node*.csv")))


def _read_csv_safe(paths: List[Path]) -> pd.DataFrame:
    """Concat all matching CSVs (drop empties); return one DataFrame.

    Returns an empty DataFrame if none of them have any data rows.
    """
    frames: List[pd.DataFrame] = []
    for p in paths:
        try:
            df = pd.read_csv(p)
        except (pd.errors.EmptyDataError, FileNotFoundError):
            continue
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_logs(logs_dir: Path) -> Dict[str, pd.DataFrame]:
    """Return ``{"gather": df, "compute": df, "response": df}``.

    Empty DataFrames mean either no file or no rows.
    """
    return {
        "gather":   _read_csv_safe(_glob_csv(logs_dir, "gather")),
        "compute":  _read_csv_safe(_glob_csv(logs_dir, "compute")),
        "response": _read_csv_safe(_glob_csv(logs_dir, "response")),
    }


def load_summary(logs_dir: Path) -> Optional[pd.DataFrame]:
    """Read ``llm_workload_summary.csv``; return None if missing."""
    p = logs_dir / "llm_workload_summary.csv"
    if not p.exists():
        return None
    try:
        return pd.read_csv(p)
    except Exception:
        return None


def load_stuck(logs_dir: Path) -> pd.DataFrame:
    """Read all ``llm_stuck_node*.csv`` files; return one concatenated frame."""
    return _read_csv_safe(_glob_csv(logs_dir, "stuck"))


# --- Lifecycle reconstruction --------------------------------------------


def build_lifecycle_df(logs: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Reconstruct per-request lifecycle from the three log frames.

    Returns one row per *fully completed* request, where "complete" means
    every join key matches a row in all three logs. Drops out partial
    requests (e.g. gathered but not computed, or computed but with no
    response) -- they're inspectable separately via
    :func:`enumerate_partial_requests`.

    Join key strategy
    -----------------

    Phase C's ``req_id`` is **per-source-GS**, not globally unique. Two
    different GSes can both have a request with ``req_id == 0`` arriving
    at the same compute SAT. So joining only on ``req_id`` produces a
    cartesian explosion in any multi-GS scenario.

    The actual unique keys per log are:

    - gather:  ``(req_id, src_node_id)``
    - compute: ``(req_id, L_in, L_out)`` (verified — compute log has no
      src_node_id, but ``L_in/L_out`` together are essentially unique
      across requests sampled from continuous distributions)
    - response: ``(req_id, gs_node_id)`` (and ``response_pkt_id`` within
      a request for multi-packet responses)

    Linking gather → compute uses ``(req_id, compute_sat_id, L_in,
    L_out_expected == L_out)``. Linking the result → response uses
    ``(req_id, src_node_id == gs_node_id)``. This gives 1 row per fully
    completed request.

    Columns derived from the join (all ns):

      t_emit_ns               # from gather
      T_uplink_ns             # first arrival - emit
      D_gather_ns             # from gather
      T_queue_wait_ns         # from compute
      T_compute_ns            # from compute
      T_first_response_ns     # first response pkt: recv - emit
      T_return_ns             # last response arrival - compute end
      T_TTFT_ns               # first response arrival - emit
      T_total_ns              # last response arrival - emit
    """
    gather = logs["gather"]
    compute = logs["compute"]
    response = logs["response"]
    if any(df.empty for df in (gather, compute, response)):
        return pd.DataFrame()

    # Aggregate per-packet response log to one row per (req_id, gs_node_id).
    resp_agg = response.groupby(["req_id", "gs_node_id"]).agg(
        t_response_first_recv_ns=("t_response_recv_ns", "min"),
        t_response_last_recv_ns=("t_response_recv_ns", "max"),
        t_response_first_emit_ns=("t_response_emit_ns", "min"),
        total_response_pkts=("total_response_pkts", "max"),
        observed_response_pkts=("response_pkt_id", "count"),
    ).reset_index()

    # gather + compute join key strategy:
    #   - TCP v2 schema (Option B): compute log carries `src_node_id` and
    #     L_out is sampled by the compute SAT, so gather's L_out_expected
    #     is always 0. Join on (req_id, compute_sat_id, src_node_id, L_in).
    #   - Legacy UDP schema: compute log has no src_node_id, but L_out
    #     was preserved from gather's L_out_expected. Join on
    #     (req_id, compute_sat_id, L_in, L_out_expected==L_out).
    if "src_node_id" in compute.columns:
        g_c = gather.merge(
            compute,
            on=["req_id", "compute_sat_id", "src_node_id", "L_in"],
            suffixes=("_g", "_c"),
        )
    else:
        gather_for_join = gather.rename(columns={"L_out_expected": "L_out"})
        g_c = gather_for_join.merge(
            compute,
            on=["req_id", "compute_sat_id", "L_in", "L_out"],
            suffixes=("_g", "_c"),
        )

    # gather+compute → response: link via src_node_id == gs_node_id.
    df = g_c.merge(
        resp_agg,
        left_on=["req_id", "src_node_id"],
        right_on=["req_id", "gs_node_id"],
    )

    df["T_uplink_ns"] = df["t_first_arrival_ns"] - df["t_emit_ns"]
    df["T_first_response_ns"] = (
        df["t_response_first_recv_ns"] - df["t_response_first_emit_ns"]
    )
    df["T_return_ns"] = df["t_response_last_recv_ns"] - df["t_compute_end_ns"]
    df["T_TTFT_ns"] = df["t_response_first_recv_ns"] - df["t_emit_ns"]
    df["T_total_ns"] = df["t_response_last_recv_ns"] - df["t_emit_ns"]

    # Sanity gate: drop rows whose derived times are negative — those
    # indicate the join still hit two unrelated requests (e.g. duplicate
    # L_in/L_out across distinct GSes), and shouldn't be counted as
    # completed lifecycles.
    df = df[(df["T_total_ns"] > 0) & (df["T_uplink_ns"] >= 0)].copy()

    return df.reset_index(drop=True)


def enumerate_partial_requests(logs: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return requests that appear in some logs but not all three.

    Each row has ``req_id`` and three boolean ``in_<log>`` columns. Used
    by the dashboard's Logs tab to call out completion gaps.
    """
    g_ids = set(logs["gather"]["req_id"]) if not logs["gather"].empty else set()
    c_ids = set(logs["compute"]["req_id"]) if not logs["compute"].empty else set()
    r_ids = set(logs["response"]["req_id"]) if not logs["response"].empty else set()
    all_ids = sorted(g_ids | c_ids | r_ids)
    rows = []
    for rid in all_ids:
        in_g = rid in g_ids
        in_c = rid in c_ids
        in_r = rid in r_ids
        if in_g and in_c and in_r:
            continue
        rows.append({
            "req_id": rid,
            "in_gather": in_g,
            "in_compute": in_c,
            "in_response": in_r,
        })
    return pd.DataFrame(rows)


# --- Headline summary ----------------------------------------------------


def summarise(df: pd.DataFrame) -> Dict[str, float]:
    """Top-level numbers for the metric cards on the Results tab.

    All times in milliseconds. Returns a dict with ``n``, ``mean_TTFT_ms``,
    ``p50_TTFT_ms``, ``p99_TTFT_ms``, ``mean_total_ms``, ``p50_total_ms``,
    ``p99_total_ms``.
    """
    if df.empty:
        return {
            "n": 0,
            "mean_TTFT_ms": float("nan"), "p50_TTFT_ms": float("nan"),
            "p99_TTFT_ms": float("nan"),
            "mean_total_ms": float("nan"), "p50_total_ms": float("nan"),
            "p99_total_ms": float("nan"),
        }
    return {
        "n": int(df.shape[0]),
        "mean_TTFT_ms":  float(df["T_TTFT_ns"].mean() / 1e6),
        "p50_TTFT_ms":   float(np.percentile(df["T_TTFT_ns"], 50) / 1e6),
        "p99_TTFT_ms":   float(np.percentile(df["T_TTFT_ns"], 99) / 1e6),
        "mean_total_ms": float(df["T_total_ns"].mean() / 1e6),
        "p50_total_ms":  float(np.percentile(df["T_total_ns"], 50) / 1e6),
        "p99_total_ms":  float(np.percentile(df["T_total_ns"], 99) / 1e6),
    }


def stage_means_ms(df: pd.DataFrame) -> Dict[str, float]:
    """Mean per-stage delay in ms; used by the breakdown bar chart."""
    if df.empty:
        return {}
    return {
        "T_uplink":         float(df["T_uplink_ns"].mean() / 1e6),
        "D_gather":         float(df["D_gather_ns"].mean() / 1e6),
        "T_queue_wait":     float(df["T_queue_wait_ns"].mean() / 1e6),
        "T_compute":        float(df["T_compute_ns"].mean() / 1e6),
        "T_return":         float(df["T_return_ns"].mean() / 1e6),
    }
