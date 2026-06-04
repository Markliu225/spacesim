"""
LLM-on-satellite Dashboard — Streamlit entry point.

Launch:
    cd /home/mark/spacesim/hypatia/extensions/dashboard
    streamlit run app.py

The page is laid out as a permanent sidebar (config form) plus three
main tabs: 3D Globe / Results / Logs. All user input lives in
``st.session_state['config']`` so re-renders don't drop it.

Heavy operations (topology generation, ns-3 simulation) run via the
generators and runner modules so that the Streamlit main thread stays
responsive. The runner is threaded; the UI polls it in a short loop.

Stub fallback: if no simulation has been run in this session yet, the
Results tab can still load any existing Phase C-style run directory the
user picks from a file selector. This makes the dashboard immediately
useful with cached data.
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --- Make the spacesim package importable ---------------------------------
#
# Streamlit launches this file as ``__main__`` from a CWD that may not
# include the project root. We resolve relative to ``__file__`` and
# prepend the ``extensions/`` dir so ``import spacesim.*`` works.

_APP_FILE = Path(__file__).resolve()
_SPACESIM_DIR = _APP_FILE.parent.parent                   # extensions/spacesim/
_EXTENSIONS_DIR = _SPACESIM_DIR.parent                    # extensions/
_HYPATIA_ROOT = _EXTENSIONS_DIR.parent                    # hypatia/
if str(_EXTENSIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_EXTENSIONS_DIR))

import numpy as np
import pandas as pd
import streamlit as st

from spacesim.config.schema import ExperimentConfig
from spacesim.workload.ns3_config import write_ns3_config
from spacesim.workload.schedule import generate_schedule
from spacesim.workload.events_replay import (discover_per_gs_files,
                                             install_events_replay)
from spacesim.topology.build import generate_topology
from spacesim.analysis.lifecycle import (build_lifecycle_df,
                                         enumerate_partial_requests,
                                         load_logs, load_summary,
                                         stage_means_ms, summarise)
from spacesim.runner.hypatia import HypatiaRunner, RunResult
from spacesim.viz.breakdown import (plot_stage_breakdown,
                                    plot_stage_percentiles)
from spacesim.viz.globe import make_globe_figure
from spacesim.viz.latency import plot_cdf, plot_histogram

# --- Page setup ----------------------------------------------------------

st.set_page_config(
    page_title="spacesim — LLM-on-satellite simulator",
    page_icon=":satellite:",
    layout="wide",
    initial_sidebar_state="expanded",
)

CACHE_DIR = _SPACESIM_DIR / "cache"
RUNS_ROOT = _SPACESIM_DIR / "runs"
CACHE_DIR.mkdir(exist_ok=True)
RUNS_ROOT.mkdir(exist_ok=True)


# --- Session-state helpers ----------------------------------------------


def _init_session() -> None:
    if "config" not in st.session_state:
        st.session_state.config = ExperimentConfig()
    else:
        # Upgrade old sessions: if WorkloadConfig was created before the
        # trace_replay fields existed, its attributes default to the
        # dataclass's current defaults via __init__, but a stored
        # instance that predates a default change will keep the OLD
        # value. Force a few critical defaults that we changed recently.
        cfg = st.session_state.config
        # Was: first_compute. New default per_gs_round_robin (avoid
        # queue saturation by funnelling everything to SAT 0).
        if getattr(cfg.workload, "dst_strategy", None) not in (
            "first_compute", "per_gs_round_robin"
        ):
            cfg.workload.dst_strategy = "per_gs_round_robin"
        if not hasattr(cfg.workload, "trace_start_offset_sec"):
            cfg.workload.trace_start_offset_sec = 0.0
    if "last_result" not in st.session_state:
        st.session_state.last_result = None     # type: Optional[RunResult]
    if "last_run_dir" not in st.session_state:
        st.session_state.last_run_dir = None    # type: Optional[Path]
    if "last_topology" not in st.session_state:
        st.session_state.last_topology = None
    if "live_log" not in st.session_state:
        st.session_state.live_log = []
    if "external_run_dir" not in st.session_state:
        st.session_state.external_run_dir = ""


_init_session()


# --- Sidebar -------------------------------------------------------------


def _sidebar() -> None:
    cfg: ExperimentConfig = st.session_state.config

    with st.sidebar:
        st.title("LLM-on-satellite")
        st.caption("Phase A/B/C dashboard — Hypatia simulator")

        with st.expander("Constellation", expanded=True):
            shell = cfg.shells[0]
            shell.altitude_km = st.slider(
                "Altitude h (km)", 300, 2000, int(shell.altitude_km), 10)
            shell.inclination_deg = st.slider(
                "Inclination i (°)", 0, 180, int(shell.inclination_deg), 1)
            col1, col2 = st.columns(2)
            with col1:
                shell.num_planes = int(st.number_input(
                    "Planes N_P", 1, 100, value=int(shell.num_planes)))
            with col2:
                shell.sats_per_plane = int(st.number_input(
                    "Sats/plane N_S", 1, 100, value=int(shell.sats_per_plane)))
            shell.phase_offset = int(st.number_input(
                "Phase offset F", 0, max(shell.num_planes - 1, 0),
                value=min(int(shell.phase_offset), max(shell.num_planes - 1, 0))))
            shell.min_elevation_deg = st.slider(
                "Min elevation ε_min (°)", 5, 40, int(shell.min_elevation_deg), 1)
            shell.isl_pattern = st.selectbox(
                "ISL pattern", ["+Grid"], index=0)
            shell.compute_ratio = (st.slider(
                "Compute ratio (%)", 1, 100, int(shell.compute_ratio * 100), 1)
                / 100.0)
            st.caption(f"Total sats: **{shell.total_sats}** "
                       f"({shell.num_planes} planes × {shell.sats_per_plane})")

        with st.expander("Ground traffic", expanded=False):
            w = cfg.workload

            # GS source: preset OR custom JSON file. The custom mode
            # lets the user share one GS config with traffic_generator
            # so events_gs<N>_*.csv and topology GS node IDs stay
            # aligned by construction.
            gs_mode_options = ["Preset (Hypatia top-N)", "Custom JSON file"]
            current_gs_mode = "Custom JSON file" if w.gs_config_path else "Preset (Hypatia top-N)"
            gs_mode = st.radio(
                "Ground stations",
                gs_mode_options,
                index=gs_mode_options.index(current_gs_mode),
                horizontal=True,
                help="Preset: first N of Hypatia's bundled top-100. "
                     "Custom JSON: any file with [{name, lat, lon, ...}] — "
                     "share it with traffic_generator/traffic_gen.py for "
                     "automatic gs_idx alignment.",
            )
            if gs_mode == "Custom JSON file":
                default_gs_json = str(
                    _HYPATIA_ROOT / "extensions" / "traffic_generator"
                    / "ground_stations.json"
                )
                w.gs_config_path = st.text_input(
                    "GS config JSON",
                    value=w.gs_config_path or default_gs_json,
                    help="Path to a JSON list. Each entry needs "
                         "`name`, `lat`, `lon` (and may include "
                         "`gs_idx`, `peak_lambda`, `elevation_m`).",
                )
                _render_custom_gs_preview(w.gs_config_path)
            else:
                # Clear the custom path so topology_hash collapses
                # back to preset mode.
                if w.gs_config_path:
                    w.gs_config_path = ""
                w.gs_set = st.selectbox(
                    "GS set (which cities the topology will place)",
                    ["top_5_cities", "top_20_cities", "top_100_cities"],
                    index=["top_5_cities", "top_20_cities", "top_100_cities"]
                    .index(w.gs_set),
                    help="First N cities of Hypatia's top-100 file. "
                         "The traffic_generator's default ground_stations.json "
                         "matches this ordering (Tokyo, Delhi, …) "
                         "for indices 0..9.",
                )

            source_options = ["synthetic", "trace_replay"]
            source_labels = {
                "synthetic": "Synthetic — Poisson(λ) + Normal(L_in)",
                "trace_replay": "Trace replay — per-GS events CSV",
            }
            w.source = st.radio(
                "Workload source",
                source_options,
                index=source_options.index(w.source),
                format_func=lambda s: source_labels[s],
                help="Synthetic samples arrivals at ns-3 runtime. "
                     "Trace replay reads them from per-GS event CSVs "
                     "produced by extensions/traffic_generator/traffic_gen.py.",
            )

            # Routing strategy is critical for queue behaviour — show it
            # in the main flow, not buried in Advanced. With
            # `first_compute` all GS funnel to one SAT and queue
            # saturates fast; `per_gs_round_robin` spreads load.
            dst_options = ["per_gs_round_robin", "first_compute"]
            w.dst_strategy = st.selectbox(
                "GS → compute SAT routing",
                dst_options,
                index=dst_options.index(w.dst_strategy)
                       if w.dst_strategy in dst_options else 0,
                help="per_gs_round_robin (recommended): each GS pinned "
                     "to a distinct compute SAT — load is spread. "
                     "first_compute: every GS sends to the lowest-ID "
                     "C SAT — useful for stress-testing queue, but "
                     "expect huge T_queue_wait at moderate loads.",
            )
            if w.dst_strategy == "first_compute":
                st.warning(
                    "⚠ All GS will funnel to a single compute SAT — "
                    "queue will saturate quickly under any nontrivial "
                    "load. Switch to per_gs_round_robin unless you "
                    "specifically want to study this regime."
                )

            if w.source == "synthetic":
                w.lambda_total = float(st.slider(
                    "Λ_total (req/s, summed across GS)", 1, 1000,
                    int(w.lambda_total), 1))

                st.markdown("**L_in (prompt tokens)**")
                cols = st.columns(4)
                w.L_in_mean = float(cols[0].number_input("mean", value=float(w.L_in_mean), key="lin_mean"))
                w.L_in_std  = float(cols[1].number_input("std",  value=float(w.L_in_std),  key="lin_std"))
                w.L_in_min  = int(cols[2].number_input("min", value=int(w.L_in_min), step=1, key="lin_min"))
                w.L_in_max  = int(cols[3].number_input("max", value=int(w.L_in_max), step=1, key="lin_max"))

                st.markdown("**L_out (response tokens)**")
                cols = st.columns(4)
                w.L_out_mean = float(cols[0].number_input("mean", value=float(w.L_out_mean), key="lout_mean"))
                w.L_out_std  = float(cols[1].number_input("std",  value=float(w.L_out_std),  key="lout_std"))
                w.L_out_min  = int(cols[2].number_input("min", value=int(w.L_out_min), step=1, key="lout_min"))
                w.L_out_max  = int(cols[3].number_input("max", value=int(w.L_out_max), step=1, key="lout_max"))
            else:
                # ─── trace_replay — minimal surface area ───────────
                default_trace = str(
                    _HYPATIA_ROOT / "extensions" / "traffic_generator"
                    / "real_run" / "per_gs"
                )
                w.trace_per_gs_dir = st.text_input(
                    "Per-GS events directory",
                    value=w.trace_per_gs_dir or default_trace,
                    help="Directory of events_gs<N>_<city>.csv files "
                         "(traffic_generator/traffic_gen.py output). The "
                         "GS list and lat/lon come from Hypatia's top-100 "
                         "file — the first N cities must match this "
                         "trace's GS list.",
                )
                _render_trace_preview(w.trace_per_gs_dir)
                _render_trace_fit_check(
                    w.trace_per_gs_dir,
                    cfg.simulation.duration_seconds,
                    w.trace_start_offset_sec,
                )
                # Offset slider — let user pick which slice of the trace
                # to simulate without regenerating the trace.
                w.trace_start_offset_sec = float(st.number_input(
                    "Trace start offset (sec from trace t=0)",
                    min_value=0.0, value=float(w.trace_start_offset_sec),
                    step=3600.0,
                    help="Trace is typically 24h. Sim only runs "
                         "`duration_seconds`. Set offset=50400 (14:00 UTC) "
                         "to simulate the afternoon-peak slice. 0 = "
                         "start at the trace's beginning.",
                ))

            st.divider()
            show_advanced = st.checkbox(
                "Show advanced workload options",
                value=False,
                key="show_workload_advanced",
                help="Routing strategy, stage mode, byte sizing — "
                     "defaults are fine for most experiments.",
            )
            if show_advanced:
                if w.source == "trace_replay":
                    w.trace_stage_mode = st.radio(
                        "Stage events into run dir as",
                        ["copy", "symlink"],
                        index=["copy", "symlink"].index(w.trace_stage_mode),
                        horizontal=True,
                    )
                    st.caption("L_in is replayed from the trace; clamps "
                               "below still apply.")
                    cols = st.columns(2)
                    w.L_in_min = int(cols[0].number_input(
                        "L_in min (clamp)", value=int(w.L_in_min), step=1,
                        key="lin_min_replay"))
                    w.L_in_max = int(cols[1].number_input(
                        "L_in max (clamp)", value=int(w.L_in_max), step=1,
                        key="lin_max_replay"))

                    st.markdown("**L_out distribution (ns-3 samples from "
                                "this; the trace's L_out is currently "
                                "ignored — see roadmap)**")
                    cols = st.columns(4)
                    w.L_out_mean = float(cols[0].number_input("mean", value=float(w.L_out_mean), key="lout_mean_r"))
                    w.L_out_std  = float(cols[1].number_input("std",  value=float(w.L_out_std),  key="lout_std_r"))
                    w.L_out_min  = int(cols[2].number_input("min", value=int(w.L_out_min), step=1, key="lout_min_r"))
                    w.L_out_max  = int(cols[3].number_input("max", value=int(w.L_out_max), step=1, key="lout_max_r"))

                cols = st.columns(2)
                w.bytes_per_token = int(cols[0].number_input(
                    "bytes/token", min_value=1, value=int(w.bytes_per_token)))
                w.packet_payload = int(cols[1].number_input(
                    "packet payload", min_value=64, value=int(w.packet_payload)))

        with st.expander("Compute model", expanded=False):
            c = cfg.compute
            c.alpha_ns_per_input_token = int(st.slider(
                "α (ns/input token)", 1_000, 1_000_000,
                int(c.alpha_ns_per_input_token), 1_000))
            c.beta_ns_per_output_token = int(st.slider(
                "β (ns/output token)", 1_000, 1_000_000,
                int(c.beta_ns_per_output_token), 1_000))
            c.gamma_ns = int(st.slider(
                "γ (ns)", 0, 100_000_000,
                int(c.gamma_ns), 1_000_000))
            avg_compute_ms = (
                c.alpha_ns_per_input_token * cfg.workload.L_in_mean
                + c.beta_ns_per_output_token * cfg.workload.L_out_mean
                + c.gamma_ns
            ) / 1e6
            st.caption(f"T_compute = α·L_in + β·L_out + γ; at means ≈ {avg_compute_ms:.1f} ms")

        with st.expander("Simulation", expanded=False):
            s = cfg.simulation
            # Two-tier picker: minute-scale via slider (the common case)
            # plus a "long sim" override for hour-scale experiments.
            dur_mode = st.radio(
                "Duration scale",
                ["short (5–600s)", "long (1–120 min)"],
                index=0 if s.duration_seconds <= 600 else 1,
                horizontal=True,
                key="dur_mode",
                help="Long sims (>10 min) can take tens of minutes of "
                     "wallclock and use much larger update_interval_ms "
                     "to keep the fstate file count manageable.",
            )
            if dur_mode.startswith("short"):
                s.duration_seconds = int(st.slider(
                    "Duration (s)", 5, 600,
                    int(min(s.duration_seconds, 600)), 5))
            else:
                s.duration_seconds = int(st.slider(
                    "Duration (min)", 1, 120,
                    int(max(1, s.duration_seconds // 60)), 1)) * 60
            try:
                default_dt = datetime.fromisoformat(s.epoch_iso.replace("Z", "+00:00"))
            except ValueError:
                default_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
            picked_date = st.date_input("Epoch date (UTC)", default_dt.date())
            picked_time = st.time_input("Epoch time (UTC)", default_dt.time())
            s.epoch_iso = (datetime.combine(picked_date, picked_time)
                           .replace(tzinfo=timezone.utc).isoformat()
                           .replace("+00:00", "Z"))
            s.update_interval_ms = int(st.selectbox(
                "fstate update interval (ms)",
                options=[100, 250, 500, 1000, 2000, 5000, 10000, 30000],
                index=[100, 250, 500, 1000, 2000, 5000, 10000, 30000].index(
                    s.update_interval_ms) if s.update_interval_ms in
                    [100, 250, 500, 1000, 2000, 5000, 10000, 30000] else 3,
                help="How often Hypatia rebuilds the routing forwarding "
                     "table to reflect satellite motion. Smaller = more "
                     "accurate handovers, but proportionally more "
                     "fstate files. For long sims pick ≥ 1000 ms.",
            ))
            # Cost preview: fstate count + a rough wallclock estimate.
            n_fstate = (s.duration_seconds * 1000) // max(s.update_interval_ms, 1)
            # Empirical: 30 s sim of moderate trace replay ≈ 10 s wallclock
            # on this box. Scale linearly with duration; tighten when
            # interval is small (augment dominates).
            est_wallclock_s = (s.duration_seconds / 30.0) * 10.0
            est_wallclock_s += n_fstate * 0.05  # augment + IO overhead per fstate
            badge = "✓" if n_fstate <= 1000 else ("⚠" if n_fstate <= 5000 else "✗")
            st.caption(
                f"{badge} fstate files = **{int(n_fstate)}** (cap 5000). "
                f"Rough wallclock estimate: **~{int(est_wallclock_s)} s** "
                f"(~{est_wallclock_s/60:.1f} min)."
            )

        st.divider()

        run_clicked = st.button("Run Simulation", type="primary",
                                use_container_width=True)
        cols = st.columns(2)
        save_clicked = cols[0].button("Save Config", use_container_width=True)
        load_clicked = cols[1].button("Load Config", use_container_width=True)

        if save_clicked:
            payload = cfg.to_json()
            st.download_button(
                "Download experiment.json",
                payload, file_name="experiment.json",
                mime="application/json", use_container_width=True,
            )
        if load_clicked:
            uploaded = st.file_uploader(
                "Upload experiment.json", type=["json"],
                accept_multiple_files=False)
            if uploaded is not None:
                try:
                    st.session_state.config = ExperimentConfig.from_json(
                        uploaded.read().decode("utf-8"))
                    st.success("Config loaded.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"failed to load: {exc}")

        st.divider()
        st.markdown("##### Inspect a previous run")
        candidates = _discover_run_dirs()
        options = ["(none)"] + [str(p) for p in candidates]
        cur = st.session_state.external_run_dir or "(none)"
        try:
            cur_idx = options.index(cur)
        except ValueError:
            cur_idx = 0
        choice = st.selectbox("Pick a run dir to load",
                              options=options, index=cur_idx)
        st.session_state.external_run_dir = "" if choice == "(none)" else choice

        errs = cfg.validate()
        if errs:
            with st.container():
                for e in errs:
                    st.error(e)
        st.session_state["__run_clicked__"] = run_clicked


def _render_custom_gs_preview(gs_config_path: str) -> None:
    """Show the contents of a custom GS JSON, or surface a useful error.

    Renders nothing if the path is empty. If the file exists and
    parses, shows a small table (name + lat + lon + optional
    peak_lambda) so the user can confirm what's about to be placed.
    """
    if not gs_config_path:
        st.caption("Pick a JSON file.")
        return
    p = Path(gs_config_path).expanduser()
    if not p.is_file():
        st.warning(f"not a file: {p}")
        return
    import json as _json
    try:
        with open(p) as f:
            data = _json.load(f)
    except Exception as exc:
        st.warning(f"JSON parse error: {exc}")
        return
    if not isinstance(data, list) or not data:
        st.warning("JSON root must be a non-empty list of dicts.")
        return
    rows = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            st.warning(f"row {i} is not a dict; skipping.")
            continue
        rows.append({
            "gs_idx": entry.get("gs_idx", i),
            "name": entry.get("name", f"(row {i})"),
            "lat": entry.get("lat"),
            "lon": entry.get("lon"),
            "peak_lambda": entry.get("peak_lambda", "—"),
        })
    st.caption(f"Loaded **{len(rows)}** ground stations from {p.name}.")
    st.dataframe(pd.DataFrame(rows), use_container_width=True,
                 hide_index=True)


def _render_trace_fit_check(trace_dir_str: str, sim_duration_sec: int,
                            trace_start_offset_sec: float) -> None:
    """Show how many trace events will actually fire in the sim window.

    This is the single most important sanity check for trace_replay: the
    trace typically covers 24h but the sim only runs N seconds. Without
    this widget users see "tx_request_count=1076" against "expected
    millions" and assume the simulator silently skipped work.
    """
    if not trace_dir_str:
        return
    p = Path(trace_dir_str).expanduser()
    if not p.is_dir():
        return
    try:
        files = discover_per_gs_files(p)
    except Exception:
        return
    sim_dur_ns = int(sim_duration_sec) * 1_000_000_000
    offset_ns = int(round(trace_start_offset_sec * 1_000_000_000))
    lo, hi = offset_ns, offset_ns + sim_dur_ns
    n_total = 0
    n_kept = 0
    trace_max = 0
    for gs_idx, fp in files.items():
        try:
            df = pd.read_csv(fp, usecols=["t_emit_ns"])
        except Exception:
            continue
        n_total += len(df)
        t = df["t_emit_ns"].to_numpy()
        n_kept += int(((t >= lo) & (t < hi)).sum())
        if len(t):
            trace_max = max(trace_max, int(t.max()))
    if n_total == 0:
        return
    frac = 100.0 * n_kept / n_total if n_total else 0.0
    arrival_rate = n_kept / max(sim_duration_sec, 1)
    span_h = trace_max / 3.6e12
    if n_kept == 0:
        st.error(
            f"⚠ Sim window [{trace_start_offset_sec:.0f}s, "
            f"{trace_start_offset_sec + sim_duration_sec:.0f}s] is "
            f"outside the trace's coverage (trace ends at "
            f"{trace_max/1e9:.0f}s ≈ {span_h:.1f}h). "
            f"Either lower the offset or regenerate a longer trace."
        )
    else:
        msg = (f"Trace covers ~{span_h:.1f}h. Sim window will fire "
               f"**{n_kept:,}** of {n_total:,} events ({frac:.3f}%), "
               f"avg **{arrival_rate:.1f} req/s** across "
               f"{len(files)} GS.")
        if frac < 5.0:
            st.warning(msg + " Most of the trace will be ignored — "
                       "consider increasing `duration_seconds` or "
                       "regenerating a shorter trace.")
        else:
            st.info(msg)


def _render_trace_preview(trace_dir_str: str) -> None:
    """Show a quick summary of an Azure-style per-GS events directory.

    Counts files and totals events without parsing the whole CSV — uses
    ``wc -l``-style line counts which are accurate enough for sanity
    checks. Failures are surfaced as warnings, not errors, so the
    sidebar still lets the user keep editing the path.
    """
    if not trace_dir_str:
        st.caption("No directory chosen.")
        return
    p = Path(trace_dir_str).expanduser()
    if not p.is_dir():
        st.warning(f"not a directory: {p}")
        return
    try:
        files = discover_per_gs_files(p)
    except Exception as exc:
        st.warning(f"trace dir lookup failed: {exc}")
        return
    rows = []
    for gs_idx in sorted(files):
        fp = files[gs_idx]
        try:
            with open(fp) as fh:
                # Subtract 1 for header. Iterate in chunks to avoid loading
                # all 600k+ rows for big traces.
                n_rows = sum(1 for _ in fh) - 1
                n_rows = max(n_rows, 0)
        except OSError:
            n_rows = -1
        rows.append({"gs_idx": gs_idx, "file": fp.name, "events": n_rows})
    df = pd.DataFrame(rows)
    st.caption(f"Found **{len(df)}** per-GS files, "
               f"**{int(df['events'].clip(lower=0).sum())}** total events.")
    st.dataframe(df, use_container_width=True, hide_index=True)


def _discover_run_dirs() -> list[Path]:
    """Find run dirs with parseable Phase-C-style llm logs.

    Searches the package's own ``runs/`` plus any scenario-local
    ``run/logs_ns3/`` directory. Both forms — top-level and scenario-
    local — are picked up so the user can browse cached demos shipped
    with the repo as well as freshly produced runs.
    """
    candidates: list[Path] = []
    seen = set()
    search_roots: list[Path] = [RUNS_ROOT]
    scenarios_dir = _SPACESIM_DIR / "scenarios"
    if scenarios_dir.exists():
        for scenario in scenarios_dir.iterdir():
            run_dir = scenario / "run"
            if run_dir.exists():
                search_roots.append(run_dir)
    for root in search_roots:
        if not root.exists():
            continue
        for run in root.iterdir():
            logs = run / "logs_ns3"
            if logs.exists() and any(logs.glob("llm_gather_node*.csv")):
                if run not in seen:
                    candidates.append(run)
                    seen.add(run)
        # Also check root itself if it directly has logs_ns3/.
        logs = root / "logs_ns3"
        if logs.exists() and any(logs.glob("llm_gather_node*.csv")):
            if root not in seen:
                candidates.append(root)
                seen.add(root)
    return sorted(candidates)


# --- Tabs ----------------------------------------------------------------


def _tab_globe() -> None:
    cfg: ExperimentConfig = st.session_state.config
    shell = cfg.shells[0]

    col_intro, col_action = st.columns([3, 1])
    with col_intro:
        st.subheader("Constellation snapshot (analytical preview)")
        st.caption("Satellites propagate on circular orbits at the "
                   "configured altitude/inclination; the Earth rotates "
                   "underneath. GSLs are drawn from each GS to the "
                   "nearest satellite above the min elevation. The "
                   "actual simulator uses SGP-4 from real TLEs (close "
                   "but not identical to this preview).")
    with col_action:
        show_isls = st.checkbox(
            "Draw +Grid ISLs", value=True,
            help="Cross-plane wraparound is dropped (Walker-Star seam) "
                 "and any chord that would pass through Earth is "
                 "filtered out.")
        show_gsl = st.checkbox(
            "Draw GSL (active links)", value=True,
            help="For each GS, the line points at the nearest "
                 "in-view sat above the configured min elevation.")
    # Time slider — drives orbital propagation + Earth rotation.
    max_t = max(int(cfg.simulation.duration_seconds), 60)
    if max_t <= 60:
        # Fine-grained step in short sims so motion is visible.
        time_sec = st.slider(
            "Time within sim window (s)", 0, max_t, 0, 1, key="globe_t")
    else:
        time_sec = st.slider(
            "Time within sim window (s)", 0, max_t, 0,
            max(1, max_t // 200), key="globe_t")
    fig = _build_preview_globe(cfg, time_sec=int(time_sec),
                                show_isls=show_isls, show_gsl=show_gsl)
    st.plotly_chart(fig, use_container_width=True)
    # Orbital period at this altitude, just so the slider's motion has
    # a frame of reference.
    import math
    R_E_km = 6378.135
    mu_km3_s2 = 398600.4418
    a_km = R_E_km + shell.altitude_km
    period_s = 2 * math.pi * math.sqrt(a_km ** 3 / mu_km3_s2)
    st.caption(
        f"Total sats: {shell.total_sats}  |  "
        f"Compute ratio: {shell.compute_ratio * 100:.0f}%  |  "
        f"Min elevation: {shell.min_elevation_deg}°  |  "
        f"Orbital period: {period_s/60:.1f} min  |  "
        f"Earth rotates {360.0 * time_sec / 86400.0:.2f}° in this slice"
    )


def _build_preview_globe(cfg: ExperimentConfig, *, time_sec: int = 0,
                          show_isls: bool = True, show_gsl: bool = True):
    """Dynamic analytical preview for the globe tab.

    Computes ECEF satellite positions at ``time_sec`` by propagating
    each sat through its orbit and rotating the inertial frame back
    to Earth-fixed (so the GS stay stationary). For ``time_sec=0``
    this matches the static preview.

    Geometry shortcuts:
      - Walker-Star: plane p uses inertial RAAN = p · π / N_P.
      - Orbital mean motion = √(μ / a³).
      - Earth rotation rate = 2π / 86 400 s (solar day; close enough
        for visualization, sidereal would shift by ~4 min/day).
      - In each plane, sat j starts at anomaly 2π j / N_S + Walker
        phase shift, and advances by mean_motion · t.

    The real ns-3 simulator uses SGP-4 on the cached TLEs, which
    accounts for J2 oblateness and other perturbations not modelled
    here. For preview the simpler propagator is good enough.
    """
    import math

    shell = cfg.shells[0]
    R_E_km = 6378.135
    R_sat = R_E_km + shell.altitude_km
    incl = math.radians(shell.inclination_deg)
    n_p, n_s = shell.num_planes, shell.sats_per_plane
    n_total = shell.total_sats

    # Orbital + Earth dynamics
    mu_km3_s2 = 398600.4418
    mean_motion_rad_s = math.sqrt(mu_km3_s2 / (R_sat ** 3))
    earth_rot_rad_s = 2.0 * math.pi / 86400.0
    earth_rotation = earth_rot_rad_s * float(time_sec)

    pts = np.zeros((n_total, 3))
    for p in range(n_p):
        # ECEF RAAN = inertial RAAN − ω⊕ · t  (Earth has spun under the orbit).
        raan_inertial = p * math.pi / max(n_p, 1)
        raan = raan_inertial - earth_rotation
        cos_O, sin_O = math.cos(raan), math.sin(raan)
        for j in range(n_s):
            anomaly_0 = (2 * math.pi * j / max(n_s, 1)
                         + 2 * math.pi * (p * shell.phase_offset)
                         / max(n_p * n_s, 1))
            anomaly = anomaly_0 + mean_motion_rad_s * float(time_sec)
            x_orb = R_sat * math.cos(anomaly)
            y_orb = R_sat * math.sin(anomaly)
            x_inc = x_orb
            y_inc = y_orb * math.cos(incl)
            z_inc = y_orb * math.sin(incl)
            x = cos_O * x_inc - sin_O * y_inc
            y = sin_O * x_inc + cos_O * y_inc
            pts[p * n_s + j] = (x, y, z_inc)

    from spacesim.topology.build import _assign_compute_planes
    planes_C = set(_assign_compute_planes(n_p, shell.compute_ratio))
    roles = ["C" if (i // max(n_s, 1)) in planes_C else "T"
             for i in range(n_total)]

    # ISLs: same +Grid topology, dropping the cross-plane seam wraparound
    # AND any segment whose chord midpoint dips below Earth (extra guard
    # for edge inclinations / phase offsets).
    isls: list[tuple[int, int]] = []
    if show_isls:
        for p in range(n_p):
            for j in range(n_s):
                a = p * n_s + j
                isls.append((a, p * n_s + ((j + 1) % n_s)))
                if p + 1 < n_p:
                    isls.append((a, (p + 1) * n_s + j))
        kept: list[tuple[int, int]] = []
        for u, v in isls:
            mid = 0.5 * (pts[u] + pts[v])
            if float((mid * mid).sum()) ** 0.5 > R_E_km:
                kept.append((u, v))
        isls = kept

    gs_records = _load_gs_records(cfg.workload.gs_set,
                                  cfg.workload.gs_config_path)

    # GSLs: each GS connects to the nearest sat above min_elevation_deg.
    # Changes as sats move in/out of view → motion is visible without
    # the user needing to inspect a fstate file.
    gsl_links: list[tuple] = []
    if show_gsl and gs_records:
        gsl_links = _compute_gsl_links(
            pts, gs_records, shell.min_elevation_deg
        )

    title = (f"t = {time_sec}s  |  {n_p}×{n_s} = {n_total} sats "
             f"@ {shell.altitude_km:.0f} km  |  "
             f"compute = {sum(1 for r in roles if r == 'C')}, "
             f"active GSL = {len(gsl_links)}/{len(gs_records)}")
    return make_globe_figure(
        pts, roles, gs_records, isls=isls, gsl_links=gsl_links,
        title=title,
    )


def _compute_gsl_links(sat_positions_km, gs_records,
                       min_elevation_deg: float):
    """For each GS, return (gs_idx, sat_idx, distance_km) for the
    nearest sat above the minimum elevation angle. Empty list of
    visibility check fails for that GS.
    """
    import math
    min_elev_rad = math.radians(float(min_elevation_deg))
    out: list[tuple[int, int, float]] = []
    for k, rec in enumerate(gs_records):
        _gs_id, _name, lat_deg, lon_deg = rec[:4]
        gs_xyz = _latlon_to_ecef_km(lat_deg, lon_deg)
        gs_norm = float(np.linalg.norm(gs_xyz))
        if gs_norm == 0:
            continue
        best_idx = -1
        best_d = float("inf")
        for s_idx in range(len(sat_positions_km)):
            dx = sat_positions_km[s_idx] - gs_xyz
            d = float(np.linalg.norm(dx))
            if d == 0:
                continue
            # Elevation = arcsin(dot(gs_unit, dx) / d). Above min_elev
            # iff that ratio > sin(min_elev).
            cos_zenith = float(np.dot(gs_xyz, dx)) / (gs_norm * d)
            if cos_zenith < math.sin(min_elev_rad):
                continue
            if d < best_d:
                best_d = d
                best_idx = s_idx
        if best_idx >= 0:
            out.append((k, best_idx, best_d))
    return out


def _latlon_to_ecef_km(lat_deg: float, lon_deg: float):
    """Convert geodetic lat/lon (degrees, alt=0) to ECEF km.

    Spherical Earth approximation (good enough for preview; the
    flattening at lat 30° contributes ~10 km out of 6378 km, well
    inside the visual fidelity of the globe).
    """
    import math
    R_E_km = 6378.135
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    cl = math.cos(lat)
    return np.array([R_E_km * cl * math.cos(lon),
                     R_E_km * cl * math.sin(lon),
                     R_E_km * math.sin(lat)])


@st.cache_data(show_spinner=False)
def _load_gs_records(gs_set: str, gs_config_path: str = ""):
    """Return ``[(gs_idx, name, lat, lon), …]`` for the globe preview.

    Two sources, mirroring the topology builder:

    - ``gs_config_path`` set: read the same JSON the topology will
      use. Keeps preview and the actual cached topology aligned.
    - Otherwise: take the first N rows from Hypatia's top-100 file.

    Cached on inputs; if the user edits the JSON, change the path
    or restart the dashboard.
    """
    out = []
    if gs_config_path:
        import json as _json
        p = Path(gs_config_path).expanduser()
        if not p.is_file():
            return out
        try:
            with open(p) as f:
                data = _json.load(f)
        except Exception:
            return out
        if not isinstance(data, list):
            return out
        for i, entry in enumerate(data):
            if not isinstance(entry, dict):
                continue
            try:
                gs_idx = int(entry.get("gs_idx", i))
                name = str(entry["name"])
                lat = float(entry["lat"])
                lon = float(entry["lon"])
            except (KeyError, ValueError, TypeError):
                continue
            out.append((gs_idx, name, lat, lon))
        return out

    src = (
        _HYPATIA_ROOT
        / "paper" / "satellite_networks_state" / "input_data"
        / "ground_stations_cities_sorted_by_estimated_2025_pop_top_100.basic.txt"
    )
    counts = {"top_5_cities": 5, "top_20_cities": 20, "top_100_cities": 100}
    n = counts.get(gs_set, 5)
    if not src.exists():
        return out
    with open(src) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            try:
                out.append((int(parts[0]), parts[1],
                            float(parts[2]), float(parts[3])))
            except ValueError:
                continue
            if len(out) >= n:
                break
    return out


def _tab_results() -> None:
    st.subheader("Results")
    df, summary_df, partial_df, source_label = _load_active_results()
    if df is None or df.empty:
        st.info("No results yet. Run a simulation, or pick a previous run dir "
                "from the sidebar.")
        if partial_df is not None and not partial_df.empty:
            st.warning(
                f"Found {len(partial_df)} partial requests but zero full "
                f"lifecycles — log files may be truncated.")
            st.dataframe(partial_df, use_container_width=True)
        return

    st.caption(f"Source: `{source_label}` | Completed requests: **{len(df)}**")

    summary = summarise(df)
    cols = st.columns(4)
    cols[0].metric("Completed", f"{summary['n']}")
    cols[1].metric("TTFT mean (ms)", f"{summary['mean_TTFT_ms']:.1f}",
                   f"p50 {summary['p50_TTFT_ms']:.1f}")
    cols[2].metric("Total mean (ms)", f"{summary['mean_total_ms']:.1f}",
                   f"p99 {summary['p99_total_ms']:.1f}")
    if summary_df is not None and not summary_df.empty:
        row = summary_df.iloc[0]
        try:
            tx = float(row["tx_request_count"])
            ok = float(row["compute_complete_count"])
            cols[3].metric("Compute completion ratio",
                           f"{(ok / max(tx, 1)) * 100:.1f}%",
                           f"{int(ok)}/{int(tx)}")
        except (KeyError, ValueError):
            pass

    if partial_df is not None and not partial_df.empty:
        with st.expander(f"⚠ {len(partial_df)} partial / stuck requests"):
            st.dataframe(partial_df, use_container_width=True)

    col_cdf, col_hist = st.columns(2)
    with col_cdf:
        st.plotly_chart(
            plot_cdf(df, "T_TTFT_ns", title="TTFT (time to first response) CDF"),
            use_container_width=True)
        st.plotly_chart(
            plot_cdf(df, "T_total_ns", title="End-to-end latency CDF"),
            use_container_width=True)
    with col_hist:
        st.plotly_chart(
            plot_histogram(df, "T_TTFT_ns", title="TTFT distribution"),
            use_container_width=True)
        st.plotly_chart(
            plot_histogram(df, "T_total_ns", title="Total latency distribution"),
            use_container_width=True)

    st.plotly_chart(plot_stage_breakdown(stage_means_ms(df)),
                    use_container_width=True)
    st.plotly_chart(plot_stage_percentiles(df), use_container_width=True)

    st.markdown("##### Key metrics (ms)")
    rows = []
    for label, col in (("uplink", "T_uplink_ns"), ("gather", "D_gather_ns"),
                       ("queue", "T_queue_wait_ns"), ("compute", "T_compute_ns"),
                       ("return", "T_return_ns"), ("TTFT", "T_TTFT_ns"),
                       ("total", "T_total_ns")):
        v = df[col].to_numpy() / 1e6
        rows.append({
            "stage": label,
            "mean": float(v.mean()),
            "p50":  float(np.percentile(v, 50)),
            "p90":  float(np.percentile(v, 90)),
            "p99":  float(np.percentile(v, 99)),
            "max":  float(v.max()),
        })
    st.dataframe(pd.DataFrame(rows).round(2), use_container_width=True)


def _load_active_results():
    candidate: Optional[Path] = None
    label = "(none)"
    if st.session_state.last_result is not None and st.session_state.last_result.success:
        candidate = st.session_state.last_run_dir
        label = f"latest run: {candidate}"
    elif st.session_state.external_run_dir:
        candidate = Path(st.session_state.external_run_dir)
        label = f"selected: {candidate}"

    if candidate is None:
        return None, None, None, label
    logs_dir = candidate / "logs_ns3"
    if not logs_dir.exists():
        return None, None, None, f"{label} (no logs_ns3/)"
    logs = load_logs(logs_dir)
    df = build_lifecycle_df(logs)
    return df, load_summary(logs_dir), enumerate_partial_requests(logs), label


def _tab_logs() -> None:
    st.subheader("Logs")
    live = st.session_state.live_log
    if live:
        st.markdown("**Live stdout (last 80 lines)**")
        st.code("\n".join(live[-80:]), language="text")
    else:
        st.info("No active simulation in this session. Live log will appear "
                "here once you click *Run Simulation*.")

    candidate: Optional[Path] = None
    if st.session_state.last_run_dir is not None:
        candidate = st.session_state.last_run_dir
    elif st.session_state.external_run_dir:
        candidate = Path(st.session_state.external_run_dir)
    if candidate is None:
        return
    logs_dir = candidate / "logs_ns3"
    if not logs_dir.exists():
        st.warning(f"no logs_ns3/ under {candidate}")
        return

    st.markdown(f"**CSV previews from `{logs_dir}`**")
    for prefix in ("gather", "compute", "response", "summary", "stuck"):
        glob_pat = ("llm_workload_summary.csv" if prefix == "summary"
                    else f"llm_{prefix}_node*.csv")
        matches = sorted(logs_dir.glob(glob_pat))
        if not matches:
            continue
        with st.expander(f"{prefix} ({len(matches)} file(s))"):
            for p in matches:
                try:
                    head = pd.read_csv(p, nrows=100)
                except Exception as exc:
                    st.error(f"{p}: {exc}")
                    continue
                st.caption(str(p))
                st.dataframe(head, use_container_width=True)


# --- Pipeline ------------------------------------------------------------


def _do_run_simulation() -> None:
    cfg: ExperimentConfig = st.session_state.config
    errs = cfg.validate()
    if errs:
        st.error("Config has errors; please fix them in the sidebar first.")
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_ROOT / f"run_{timestamp}_{cfg.topology_hash()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    progress = st.empty()
    log_box = st.empty()

    def push(msg: str) -> None:
        st.session_state.live_log.append(msg)
        log_box.code("\n".join(st.session_state.live_log[-80:]), language="text")

    st.session_state.live_log = []
    push(f"== Dashboard run started at {timestamp} ==")
    push(f"   run_dir: {run_dir}")
    push(f"   topology hash: {cfg.topology_hash()}")
    try:
        progress.info("Step 1/4 — generating topology (may be cached)…")
        topo = generate_topology(cfg, CACHE_DIR, on_progress=push)
        st.session_state.last_topology = topo.__dict__
        push(f"topology ok: cache_hit={topo.cache_hit}, "
             f"sats={topo.num_satellites}, gs={topo.num_ground_stations}")

        progress.info("Step 2/4 — writing schedule…")
        schedule_path = run_dir / "llm_workload_schedule.csv"
        w = cfg.workload
        if w.source == "trace_replay":
            push(f"workload source: trace_replay (dir={w.trace_per_gs_dir})")
            summary = install_events_replay(
                cfg,
                per_gs_dir=Path(w.trace_per_gs_dir),
                run_dir=run_dir,
                num_satellites=topo.num_satellites,
                num_ground_stations=topo.num_ground_stations,
                roles_path=topo.roles_path,
                dst_strategy=w.dst_strategy,
                stage_mode=w.trace_stage_mode,
                l_out_mean=w.L_out_mean, l_out_std=w.L_out_std,
                l_out_min=w.L_out_min, l_out_max=w.L_out_max,
                trace_start_offset_sec=w.trace_start_offset_sec,
                on_log=push,
            )
            n_sched = summary["rows"]
            push(f"schedule (replay): {n_sched} GS -> {schedule_path}")
            if summary["events_kept"] == 0:
                raise RuntimeError(
                    f"events_replay produced 0 events in the sim window. "
                    f"trace covers {summary['trace_span_sec']:.0f}s but "
                    f"sim window is [{w.trace_start_offset_sec:.0f}s, "
                    f"{w.trace_start_offset_sec + cfg.simulation.duration_seconds:.0f}s]."
                )
        else:
            push("workload source: synthetic")
            n_sched = generate_schedule(
                cfg, schedule_path,
                num_satellites=topo.num_satellites,
                num_ground_stations=topo.num_ground_stations,
                roles_path=topo.roles_path,
                dst_strategy=w.dst_strategy,
            )
            push(f"schedule: {n_sched} flows -> {schedule_path}")

        link = run_dir / "satellite_roles.txt"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(topo.roles_path.resolve())
        push(f"symlinked satellite_roles.txt -> {topo.roles_path}")

        progress.info("Step 3/4 — writing ns3 config…")
        cfg_path = write_ns3_config(
            cfg, run_dir,
            state_dir=topo.state_dir,
            dynamic_state_dir=topo.dynamic_state_dir,
        )
        push(f"config: {cfg_path}")

        progress.info("Step 4/4 — running Hypatia simulator…")
        runner = HypatiaRunner(run_dir, timeout_seconds=3600)
        runner.start()
        while not runner.is_done():
            for line in runner.consume_new_lines():
                push(line)
            time.sleep(0.5)
        for line in runner.consume_new_lines():
            push(line)
        result = runner.finalise()
        # ns-3 has a known cosmetic null-Ptr crash at clean-up time
        # (SIGIOT after `STORE LLM WORKLOAD RESULTS`). The summary CSV
        # and per-stage logs are flushed *before* the crash, so a
        # non-zero returncode with a real summary file means the data
        # is intact. Treat that as a success and surface a warning.
        summary_path = run_dir / "logs_ns3" / "llm_workload_summary.csv"
        results_intact = summary_path.exists() and summary_path.stat().st_size > 0
        if results_intact and not result.success:
            push(f"note: rc={result.returncode} but summary CSV exists "
                 f"— treating as success (ns-3 cleanup crash is cosmetic).")
        st.session_state.last_result = result
        st.session_state.last_run_dir = run_dir
        progress.empty()
        if result.timed_out:
            st.error(f"Simulation timed out after {result.duration_seconds:.1f}s.")
        elif result.success or results_intact:
            msg = (f"Simulation completed in {result.duration_seconds:.1f}s. "
                   "See the *Results* tab.")
            if not result.success:
                msg += (f"  (ns-3 returned rc={result.returncode} at "
                        "cleanup; results CSVs are intact.)")
            st.success(msg)
        else:
            st.error(f"Simulation failed (rc={result.returncode}). "
                     "See *Logs* tab.")
    except Exception as exc:
        progress.empty()
        push(f"[ERROR] {exc}")
        push(traceback.format_exc())
        st.error(f"Pipeline error: {exc}")


# --- Main ----------------------------------------------------------------


def main() -> None:
    _sidebar()

    tabs = st.tabs(["🌍 3D Globe", "📊 Results", "📜 Logs"])
    with tabs[0]:
        _tab_globe()
    with tabs[1]:
        _tab_results()
    with tabs[2]:
        _tab_logs()

    if st.session_state.get("__run_clicked__", False):
        st.session_state["__run_clicked__"] = False
        _do_run_simulation()


if __name__ == "__main__":
    main()
