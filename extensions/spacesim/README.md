# spacesim — LLM-on-satellite simulator

> 中文版本：见 [功能说明.md](功能说明.md)。
> Function-level deep dive: [`docs/TECHNICAL_REPORT.md`](docs/TECHNICAL_REPORT.md).
> 中文深度文档：[`docs/系统概览.md`](docs/系统概览.md).

A unified package on top of Hypatia (Kassing et al., IMC 2020) for
simulating LLM request workloads against compute-equipped LEO
constellations. Wraps satgenpy's topology pipeline, an ns-3
`llm-workload` C++ module, an Azure-trace traffic generator, and a
Streamlit dashboard into one coherent codebase.

The simulator is packet-level (TCP) end-to-end. From the ground
station opening a connection, through ISL hops, through a FIFO-queued
inference SAT, all the way back to the response FIN, every link is
modelled with bandwidth, queueing, and TCP framing.

---

## 1. What's in the box

| Layer | Role | Entry points |
|---|---|---|
| `config/` | Dataclasses + JSON round-trip + cache-busting topology hash | `ExperimentConfig` |
| `topology/` | Walker-Star + Grid via satgenpy; C/T role assignment; fstate augmentation for SAT-as-destination routing; custom-GS support | `generate_topology`, `roles.assign_by_plane`, `fstate_augment` |
| `workload/` | Two schedule writers (synthetic Poisson and Azure trace replay), ns-3 config template, C++ ns-3 module source + installer | `generate_schedule`, `install_events_replay`, `write_ns3_config`, `install_ns3_module.sh` |
| `runner/` | Threaded `subprocess.Popen` around `./waf --run main_satnet` with streamed stdout and timeout | `HypatiaRunner` |
| `analysis/` | 3-CSV join → per-request lifecycle DataFrame; legacy per-phase analyzers under `analysis/legacy/` | `build_lifecycle_df`, `summarise`, `stage_means_ms` |
| `viz/` | Plotly 3D globe (now dynamic — orbital propagation + GSL handover), latency CDFs / histograms, per-stage breakdown | `make_globe_figure`, `plot_cdf`, `plot_stage_breakdown` |
| `dashboard/` | Streamlit UI: config sidebar + 3D globe + Results + Logs | `app.py` |
| `scenarios/` | Pre-built scripted experiments (regression fixtures) | `scenarios/*/run.sh`, `*/build_and_run.py` |
| `tests/` | pytest suite (unit + integration + regression) | `./run_tests.sh` |
| `../traffic_generator/` | Azure LLM Inference Trace → per-GS events CSVs | `traffic_gen.py` |

---

## 2. Quick start

```bash
# 1) Launch the dashboard (interactive, browser-based):
cd /home/mark/spacesim/hypatia/extensions/spacesim
./start.sh                       # http://localhost:8501

# 2) Generate a trace once (optional, for trace-replay mode):
cd ../traffic_generator
python traffic_gen.py \
    --azure-trace azure_trace/AzureLMMInferenceTrace_multimodal.csv \
    --gs-config ground_stations.json \
    --duration-sec 86400 \
    --output-dir real_run --report

# 3) Run the tests:
cd /home/mark/spacesim/hypatia/extensions/spacesim
./run_tests.sh

# 4) Browse cached results without launching anything:
ls runs/
```

In the dashboard, the **"Inspect a previous run"** dropdown
auto-discovers every directory under `runs/` and `scenarios/*/run/`
with `logs_ns3/llm_gather_node*.csv`, so cached demos are one click
away.

---

## 3. Workload modes

The simulator supports two workload sources, selected per run in the
sidebar (`WorkloadConfig.source`):

### 3.1 Synthetic (`source = "synthetic"`)

Each GS application runs a lazy chain of `Simulator::Schedule`:
inter-arrival ~ Exponential(1/λ), L_in ~ Normal(μ, σ²) clamped to
[L_in_min, L_in_max]. The schedule writer emits the legacy
**15-column** CSV. Good for "what if we doubled rate" sweeps without
regenerating any data.

### 3.2 Trace replay (`source = "trace_replay"`)

Each per-GS events file (produced by
`extensions/traffic_generator/traffic_gen.py`) is staged into the run
dir and scheduled verbatim — every `t_emit_ns` and `L_in` comes from
real Azure data. The schedule writer emits a **16-column** CSV whose
trailing field is the events filename. The C++
`LLMRequestApplication::ScheduleEventsFromFile()` loads it at
StartApplication time.

`install_events_replay` does three things you'll want to know about:

1. **Clips and shifts**: only events in
   `[trace_start_offset_sec, trace_start_offset_sec + duration_seconds)`
   are kept; their `t_emit_ns` is shifted to start at sim t=0. This
   prevents ns-3 from scheduling millions of dead events.
2. **Reports a fit summary** (`{events_total, events_kept, kept_fraction,
   per_gs}`) that the dashboard renders before the user clicks Run.
3. **Aligns GS by index**: with `num_satellites=N` and a topology of
   M ground stations, gs_idx 0..M-1 from the trace dir become node IDs
   N..N+M-1. Extra files (gs_idx ≥ M) are skipped with a warning, so a
   10-city trace can drive a 5-GS topology safely.

---

## 4. Ground stations

Two ways to declare them, picked in the sidebar:

| Mode | Source | When to use |
|---|---|---|
| Preset | Hypatia's bundled top-100 cities file. `gs_set ∈ {top_5_cities, top_20_cities, top_100_cities}` picks the first N | Quick experiments; reproducible baselines |
| Custom JSON | A user file matching `traffic_generator/ground_stations.json`: a list of dicts with `name / lat / lon` (+ optional `gs_idx`, `peak_lambda`, `elevation_m`) | Custom city list; sharing one file between trace generation and topology |

When custom JSON is set, `topology_hash()` includes a SHA-1 of the
file's content, so swapping GS sets correctly busts the topology
cache. `_select_gs_basic_subset` writes Hypatia's
`<id>,<name>,<lat>,<lon>,<elev>` format from either source. The same
function backs both the globe preview (`_load_gs_records`) and the
real topology placement, so what you see in the 3D tab is what the
simulator places.

---

## 5. Long simulations (1h, 2h, …)

The duration slider has two modes:

- **short** (5–600 s): fine for trace fragments and rate sweeps
- **long** (1–120 min): for diurnal slices and handover studies

When duration grows, you must raise `update_interval_ms` accordingly
to keep the fstate file count under control. The schema validator
caps the count at 5000 files and surfaces a suggested interval:

```
3600 s × 100 ms = 36 000 files       (rejected)
3600 s × 5000 ms = 720 files         (OK)
```

The sidebar shows a wallclock estimate (`~N seconds`) before you click
Run, derived from "10 s wallclock per 30 s of sim under moderate trace
replay" plus a per-fstate IO overhead. Empirically: 1 h of trace
replay against 10 GS lands around 20 minutes of wallclock on this box;
2 h around 40 min.

---

## 6. Dynamic 3D globe

The 3D Globe tab now has a **time slider** that re-propagates the
constellation in real time:

- Satellite positions: analytical Kepler propagation
  (`mean_motion = √(μ/a³)`), then the inertial frame is rotated back
  to ECEF by `−ω⊕ · t` so the Earth appears stationary while orbits
  move.
- ISLs: `+Grid` neighbours (in-plane next, cross-plane next; the
  Walker-Star seam wraparound is dropped, and any chord whose midpoint
  dips below Earth radius is filtered out).
- GSLs: per GS, the closest satellite above `min_elevation_deg`.
  As time advances, the green line snaps from one satellite to the
  next — the visible handovers match the GSL changes the real
  simulator sees in its fstate file (within 1–2 update intervals).

Visual proof images live in [`docs/dynamic_proof/`](docs/dynamic_proof/):
- `three_frame_evidence.png` — 2D world map at t=0, 1500 s, 3000 s,
  with red lines for the GSLs that just handed over
- `evidence_composite.png` — 6-panel world map plus a GS-vs-time
  satellite-assignment trajectory chart spanning 50 min at 30-s steps

Measured handover behaviour for Walker-Star 10×10 @ 550 km with
`ground_stations.json` (10 cities):

```
50 min × 10 GS × 30-s sampling → 92 handovers total
mean dwell time per (GS, sat) pair ≈ 326 s ≈ 5.4 min
```

That 5.4 min matches the typical Starlink-class single-satellite
visibility window from a fixed GS — the model behaves physically.

---

## 7. Package layout

```
spacesim/
├── README.md / 功能说明.md / requirements.txt / pytest.ini
├── start.sh                       — one-click launcher
├── run_tests.sh                   — pytest entry
│
├── config/schema.py               — Shell / Workload / Compute / Simulation / Experiment dataclasses
│
├── topology/
│   ├── roles.py                   — satellite_roles.txt writer
│   ├── fstate_augment.py          — add SAT-dst rows to satgenpy fstate
│   ├── dst_picker.py              — CLI: pick farthest C SAT from a GS
│   └── build.py                   — drives satgenpy + augment; supports custom GS JSON;
│                                    cache-partial-hit when only (duration, interval) changes
│
├── workload/
│   ├── schedule.py                — write 15-column synthetic schedule
│   ├── events_replay.py           — stage per-GS events + write 16-column replay schedule
│   ├── ns3_config.py              — render config_ns3.properties
│   ├── install_ns3_module.sh      — rsync ns3_module/ into ns-3 tree + waf build
│   └── ns3_module/                — C++ ns-3 module source
│       ├── model/llm-header.h
│       ├── model/llm-request-application.{cc,h}    — GS-side TCP client; trace-replay mode
│       ├── model/gather-application.{cc,h}         — SAT-side prompt collector
│       ├── model/compute-application.{cc,h}        — FIFO queue + α·L_in+β·L_out+γ
│       └── helper/                                  — TypeId helpers + scheduler
│
├── runner/hypatia.py              — threaded waf-subprocess wrapper + RunResult
│
├── analysis/
│   ├── lifecycle.py               — load_logs + build_lifecycle_df + summarise + stage_means_ms
│   └── legacy/                    — analyze_phase_{a,b,c}.py reference analyzers
│
├── viz/
│   ├── globe.py                   — Plotly 3D Earth + dynamic sats + GSL/ISL traces
│   ├── latency.py                 — CDF / histogram (T_TTFT_ns, T_total_ns)
│   └── breakdown.py               — stacked-bar mean per-stage + grouped percentiles
│
├── dashboard/app.py               — Streamlit app (sidebar + 3D Globe + Results + Logs)
│
├── scenarios/                     — scripted regression / demo runs
│   ├── mixed_topology/            — minimal viable constellation (60 sats + 5 GS)
│   ├── llm_workload/              — multi-GS sink-only (Phase B era)
│   ├── llm_full_lifecycle/        — full TCP request/gather/compute/response demo
│   └── llm_events_replay/         — trace-replay verification (5 cities × 5 s window)
│
├── tests/                         — pytest suite (unit + integration + regression)
│
└── docs/
    ├── TECHNICAL_REPORT.md         — function-level deep dive (English)
    ├── 系统概览.md                  — function-level deep dive (Chinese)
    ├── dynamic_proof/              — generated PNGs proving GSL handover
    └── history/                    — frozen per-phase logs from earlier development
```

---

## 8. Two persistent contracts (don't break)

1. **`satellite_roles.txt`** — one row per satellite, `<sat_id>,<C|T>`.
   The C++ patch in `topology-satellite-network.cc` reads this at
   simulation start to add C SATs to the `m_endpoints` set, so the
   schedule reader accepts SAT-as-endpoint. Written by
   `topology/roles.py::write_roles`, read by both
   `LlmWorkloadScheduler` and (for compute-SAT identification) the
   fstate augmenter.

2. **LLM log schema** — three CSV families produced by the ns-3
   module, joined by `analysis/lifecycle.py`:

   - `llm_gather_node<sat>.csv` — per-SAT prompt collection events
     (`t_first_arrival_ns`, `t_last_arrival_ns`, `D_gather_ns`,
     `src_node_id`, `L_in`, `t_emit_ns`).
   - `llm_compute_node<sat>.csv` — per-SAT inference events
     (`t_queue_enter_ns`, `t_compute_start_ns`, `t_compute_end_ns`,
     `T_compute_ns`, `T_queue_wait_ns`, `L_in`, `L_out`).
   - `llm_response_node<gs>.csv` — **two rows per request**:
     `response_pkt_id=0` (first byte recv), `response_pkt_id=1` (last
     byte recv). The min/max group-by in `build_lifecycle_df`
     recovers TTFT and total without parser changes.

   The join is `(req_id, compute_sat_id, src_node_id, L_in)` for
   gather × compute, then `(req_id, src_node_id == gs_node_id)` for
   response. Compound keys avoid req_id collisions across GSes.

---

## 9. Transport: TCP end-to-end

- Each LLM request opens a *fresh* TCP socket from GS → compute SAT.
- The first 24 bytes on the stream are an `LLMHeader`
  (`t_emit_ns`/`req_id`/`src_node_id`/`L_in`/`reserved`); the next
  `L_in × bytes_per_token` bytes are prompt payload. `ShutdownSend`
  on the GS side signals end-of-prompt.
- `GatherApplication` collects the header + payload, then hands the
  socket off to `ComputeApplication` via a callback.
- `ComputeApplication` enqueues the request, runs FIFO. Service time
  is `T_compute = α·L_in + β·L_out + γ` in ns. L_out is sampled by
  the compute SAT itself (`Normal(LOutMean, LOutStd²)` clamped to
  `[LOutMin, LOutMax]`).
- Response is sent back over the **same TCP socket**; when the SAT
  half-closes, the GS sees FIN and writes two rows
  (`response_pkt_id ∈ {0, 1}`) to its response log.

`T_uplink_ns` therefore includes the 3-way handshake (~1 RTT) and TCP
slow-start ramp — these are first-class effects in the simulator.

---

## 10. Pipeline at a glance

```
ExperimentConfig (sidebar / scenario script / JSON)
        │
        ▼
topology.build.generate_topology()
   ├── if cache_hit AND fstate_0.txt exists → return immediately
   ├── if cache_hit but missing dyn_dir → regenerate dynamic state only
   └── full path (cache miss):
       satgen.generate_tles_from_scratch_manual()
       satgen.generate_plus_grid_isls()
       _assign_compute_planes() → roles.assign_by_plane → satellite_roles.txt
       satgen.help_dynamic_state() …………… slowest step
       fstate_augment.py (subprocess) → adds SAT-dst routes
        │
        ▼ cached under cache/<topology_hash>/
        │
   workload source?
       synthetic   → workload.schedule.generate_schedule()
                          → 15-col llm_workload_schedule.csv
       trace_replay → workload.events_replay.install_events_replay()
                          → clip+shift trace, stage events/, write 16-col CSV
        │
        ▼
workload.ns3_config.write_ns3_config() → config_ns3.properties
        │
        ▼
runner.hypatia.HypatiaRunner.start/finalise()
   └── ./waf --run "main_satnet --run_dir=…"
        │
        ▼ logs_ns3/llm_*_node*.csv  +  llm_workload_summary.csv
        │
analysis.lifecycle.build_lifecycle_df()  → per-request DataFrame
        │
        ▼
viz.{globe, latency, breakdown}  → Plotly figures  → Streamlit
```

---

## 11. Known constraints

1. **`max_isl_length_m`** is `2·(R⊕+h)·0.99` (orbital-diameter ceiling),
   not the classic ~5 016 km atmospheric floor. ISLs are not
   guaranteed to clear the 80 km atmosphere — fine for dashboard
   experimentation, would need revisiting for production RF studies.
   See `topology/build.py::_max_isl_length_m`.
2. **satgenpy needs ≥ 10 fstate timesteps**. We refuse a config that
   produces fewer with a clear error.
3. **fstate is comma-strict**. ns-3's parser does `split_string(",", 5)`
   and aborts on lines that don't split to 5 fields.
   `fstate_augment.py` never writes comments; it tracks augmented
   `(dst, t)` pairs via a sidecar `.phase_a_augment.json`.
4. **`req_id` is per-source-GS, not global**. Compound joins in
   `build_lifecycle_df` (see §8) handle this.
5. **Dashboard 3D globe uses Kepler, not SGP-4**. The simulation
   itself uses SGP-4 via satgenpy. The preview matches qualitatively
   but a 24-h slider would drift a few degrees from real SGP-4.
6. **One shell only** — schema is a list but `generate_topology`
   materialises `shells[0]`; multi-shell warns and is dropped.
7. **L_out is sampled by Compute, not read from trace**. Even in
   trace replay, `ComputeApplication` samples L_out from
   `Normal(LOutMean, LOutStd²)`. The trace's `L_out` column is
   parsed but not used. Listed under "roadmap" in the technical
   report.
8. **ns-3 cleanup-time SIGIOT**. ns-3 has a known null `Ptr` deref
   in its teardown that fires after `STORE LLM WORKLOAD RESULTS`
   completes. Logs are intact. The dashboard treats `rc≠0 +
   `llm_workload_summary.csv` exists` as success.

---

## 12. History

This package is the consolidation of three preceding modules:

- **Phase A** — `compute satellite` as a legal traffic endpoint. The
  C++ patch in `topology-satellite-network.cc`, the
  `satellite_roles.txt` contract, and `fstate_augment.py` originated
  here.
- **Phase B** — the C++ `llm-workload` ns-3 application module
  (request + sink only, no return path).
- **Phase C** — full LLM request lifecycle: gather + compute + response.

Subsequent additions (post-consolidation):

- **Azure trace integration** (`traffic_generator/`, `events_replay.py`,
  16-column schedule, `LLMRequestApplication::ScheduleEventsFromFile`).
- **Custom GS lists** (`gs_config_path` field, JSON-driven topology).
- **Long-duration support** (slider 60 s → 7200 s, fstate count guard).
- **Dynamic 3D globe** (time slider + Kepler propagation + GSL
  handover visualization).
- **Cache partial-hit** (regenerate dynamic state when only
  `(duration, interval)` change; reuse TLEs/ISLs).

Per-phase work logs, result.md files, and old READMEs are preserved
under [`docs/history/`](docs/history/).
