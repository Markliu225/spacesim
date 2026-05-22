# spacesim — LLM-on-satellite simulator

A unified package on top of Hypatia (Kassing et al., IMC 2020) for
simulating LLM request workloads against compute-equipped LEO
constellations. Wraps satgenpy's topology pipeline, an ns-3 ``llm-workload``
application module, and a Streamlit dashboard into one consistent
codebase.

| Layer | What it provides | Entry points |
|---|---|---|
| **`topology/`** | Walker-Star + Grid generator (via satgenpy), satellite-role assignment, fstate augmentation for SAT-as-destination routing, geographically-aware dst picker | `generate_topology`, `roles.assign_by_plane`, `fstate_augment.compute_augment_rows` |
| **`workload/`** | LLM request schedule generator (Phase-C 15-column CSV), ns-3 config template renderer, the C++ ns-3 module source (`ns3_module/`) + installer | `generate_schedule`, `write_ns3_config`, `install_ns3_module.sh` |
| **`runner/`** | Threaded `subprocess.Popen` wrapper around Hypatia's ``./waf --run main_satnet``, with line-buffered stdout streaming + timeout | `HypatiaRunner` |
| **`analysis/`** | Three-CSV log parser + lifecycle reconstruction (`gather`/`compute`/`response` → per-request delays). Legacy per-phase analyzers preserved under `analysis/legacy/` | `build_lifecycle_df`, `summarise`, `stage_means_ms` |
| **`viz/`** | Plotly 3D globe, latency CDF / histogram, per-stage breakdown bars | `make_globe_figure`, `plot_cdf`, `plot_stage_breakdown` |
| **`config/`** | Shell / workload / compute / simulation dataclasses; JSON round-trip; SHA-1 topology hash for cache keys | `ExperimentConfig` |
| **`dashboard/`** | Streamlit UI — sidebar config + 3D globe + Results + Logs tabs | `app.py` |
| **`scenarios/`** | Canonical pre-built experiments (TCP-grid mixed topology, two LLM workload variants) with their state, run output, and plotting scripts | `scenarios/*/run.sh` |
| **`tests/`** | 57-case pytest suite (unit + integration + cached-run regression) | `./run_tests.sh` |

## Quick start

```bash
# Dashboard (recommended — interactive UI):
cd /home/mark/spacesim/hypatia/extensions/spacesim
./start.sh                       # http://localhost:8501

# Or run the tests:
./run_tests.sh                   # 57 tests, ~5 s

# Or browse cached results without launching anything:
ls runs/                         # tokyo_to_sat894/, llm_workload_phase_b/, llm_workload_phase_c/
```

The dashboard's sidebar **"Inspect a previous run"** dropdown
auto-discovers every directory under `runs/` and `scenarios/*/run/`
that contains a Phase-C-style `logs_ns3/llm_gather_node*.csv`, so all
the cached demos are one click away.

## Package layout

```
spacesim/
├── README.md / 功能说明.md / requirements.txt / pytest.ini
├── start.sh                       — one-click launcher
├── run_tests.sh                   — pytest entry
├── .gitignore                     — ignores cache/ runs/ __pycache__/
│
├── config/schema.py               — dataclasses (Shell / Workload / Compute / Simulation / Experiment)
│
├── topology/
│   ├── roles.py                   — satellite_roles.txt generator (by_plane | random)
│   ├── fstate_augment.py          — append SAT-dst rows to satgenpy fstate
│   ├── dst_picker.py              — pick farthest type=C SAT from a given GS
│   └── build.py                   — drives satgenpy + augment, returns TopologyResult
│
├── workload/
│   ├── schedule.py                — emit Phase-C 15-col llm_workload_schedule.csv
│   ├── ns3_config.py              — render config_ns3.properties from ExperimentConfig
│   ├── install_ns3_module.sh      — rsync ns3_module/ into ns-3 tree + waf build
│   └── ns3_module/                — C++ ns-3 module source (compute / gather / request / response apps)
│
├── runner/hypatia.py              — HypatiaRunner: threaded waf-subprocess wrapper
│
├── analysis/
│   ├── lifecycle.py               — load_logs + build_lifecycle_df + summarise + stage_means_ms
│   └── legacy/                    — analyze_phase_{a,b,c}.py reference analyzers (kept for back-compat)
│
├── viz/
│   ├── globe.py                   — Plotly 3D Earth + sat positions coloured by role
│   ├── latency.py                 — CDF / histogram
│   └── breakdown.py               — stacked-bar mean per-stage + grouped percentile bars
│
├── dashboard/app.py               — Streamlit app (sidebar config + 3D globe + Results + Logs)
│
├── scenarios/                     — canonical, repeatable experiments
│   ├── mixed_topology/            — 5×5 grid, mixed TCP flows GS→SAT + SAT→GS (was Phase A's smoke test)
│   ├── llm_workload/              — LLM request workload, sink only (was Phase B)
│   └── llm_full_lifecycle/        — full gather + compute + response lifecycle (was Phase C)
│
├── runs/                          — cached run outputs (regenerable; .gitignore'd)
│   ├── tokyo_to_sat894/           — single-flow GS→SAT regression (was phase_a/runs/gs0_to_compute_sat)
│   ├── llm_workload_phase_b/      — LLM sink-only run
│   └── llm_workload_phase_c/      — full-lifecycle run (104 requests, used by tests)
│
├── cache/                         — topology cache keyed by ExperimentConfig.topology_hash()
│
├── tests/                         — pytest suite (57 cases)
│
└── docs/
    └── history/                   — frozen per-phase logs + result.md + old READMEs
        ├── phase_a_log.md / phase_a_result.md / phase_a_README.md / phase_a_功能说明.md
        ├── phase_b_log.md / phase_b_result.md
        ├── phase_c_log.md / phase_c_result.md
        ├── dashboard_README.md
        └── extensions_使用手册.md
```

## Two persistent contracts (don't break)

1. **`satellite_roles.txt`** — file in the run dir, format
   `<sat_id>,<C|T>` per row. The C++ patch in
   `hypatia/ns3-sat-sim/simulator/contrib/satellite-network/model/topology-satellite-network.cc`
   reads this file at simulation start to add type=C sats to the
   ``m_endpoints`` set (so the schedule reader accepts SAT-as-endpoint).
   `topology/roles.py` writes this format.

2. **Phase-C log schema** — three CSV families produced by the ns-3
   ``llm-workload`` module:
   - `llm_gather_node<sat>.csv` (has the keystone `t_emit_ns` field)
   - `llm_compute_node<sat>.csv`
   - `llm_response_node<gs>.csv` (multi-row per `req_id` for multi-packet responses)

   `analysis/lifecycle.py::build_lifecycle_df` joins them; the join
   keys are explained in that file's docstring (`req_id` is **per-GS**,
   not global, so naive merging produces a cartesian explosion).

## Pipeline at a glance

```
ExperimentConfig (from sidebar / JSON)
        │
        ▼
topology.build.generate_topology()
   ├── satgen.generate_tles_from_scratch_manual()
   ├── satgen.generate_plus_grid_isls()
   ├── satgen.help_dynamic_state()              ← slowest step
   ├── roles.assign_by_plane()                  → satellite_roles.txt
   └── fstate_augment.py (subprocess)           → adds SAT-dst routes
        │ (cached under cache/<topology_hash>/)
        ▼
workload.schedule.generate_schedule()           → llm_workload_schedule.csv
workload.ns3_config.write_ns3_config()           → config_ns3.properties
        │
        ▼
runner.hypatia.HypatiaRunner.start/finalise()
   └── ./waf --run "main_satnet --run_dir=…"     ← Hypatia ns-3 simulation
        │
        ▼ logs_ns3/llm_*_node*.csv
analysis.lifecycle.build_lifecycle_df()         → per-request DataFrame
        │
        ▼
viz.globe / viz.latency / viz.breakdown          → Plotly figures
        │
        ▼ Streamlit dashboard
```

## Known constraints

These come from upstream Hypatia / satgenpy; the package documents
them rather than silently working around them.

1. **`max_isl_length_m`** is set to 99 % of the orbital diameter
   (so any +Grid edge geometrically fits) rather than the classic
   ~5 016 km atmospheric floor. Trade-off: ISLs are not guaranteed to
   clear the 80 km atmosphere — fine for the dashboard's experimentation
   semantics, would need revisiting for production-grade RF studies.
   See `topology/build.py::_max_isl_length_m`.
2. **satgenpy needs ≥ 10 fstate timesteps**. Upstream
   `generate_dynamic_state.py` has a `floor(N/10)` divisor in its
   progress logger that is zero for N<10. `topology/build.py` refuses
   to launch a run that would produce fewer timesteps with a clear
   error message; we don't patch satgenpy.
3. **fstate is comma-strict**. ns-3's parser does
   `split_string(",", 5)` and aborts on any line whose split length
   isn't 5 (e.g. `#`-prefixed comment lines). `fstate_augment.py`
   never writes comments; it tracks augmented `(dst, t)` pairs via a
   sidecar `.phase_a_augment.json` manifest.
4. **`req_id` is per-source-GS, not global**. The
   `build_lifecycle_df` join uses `(req_id, L_in, L_out)` for gather→compute
   and `(req_id, src_node_id == gs_node_id)` for response, to avoid a
   cartesian explosion in multi-GS scenarios.
5. **Dashboard renders 3D globe analytically** (circular Walker orbits)
   not via SGP-4. The simulation uses SGP-4 via satgenpy. Visually
   consistent at t=0, may drift over long durations.
6. **One shell only** — the schema supports a list of shells but
   `generate_topology` materialises only `config.shells[0]` and warns
   if more were provided.

## History

This package is the consolidation of three preceding modules:

- **Phase A** — `compute satellite` as a legal traffic endpoint. The
  C++ patch in `topology-satellite-network.cc`, the
  `satellite_roles.txt` contract, and `fstate_augment.py` originated here.
- **Phase B** — the C++ ``llm-workload`` ns-3 application module
  (request + sink only, no return path).
- **Phase C** — full LLM request lifecycle: gather + compute + response.

The original work logs, result.md files, and per-phase READMEs are
preserved verbatim under [`docs/history/`](docs/history/).

The Streamlit dashboard was built on top of all three. After
consolidation everything lives in this single package and the per-phase
directory naming is retired.
