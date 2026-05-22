# LLM-on-satellite Dashboard

Streamlit-based dashboard sitting on top of Hypatia Phase A / B / C
(see [`../phase_a/README.md`](../phase_a/README.md),
[`../phase_b/`](../phase_b/), [`../phase_c/`](../phase_c/)). The UI lets
you configure a constellation, an LLM workload, and a compute model,
kick off a Hypatia simulation, and explore per-request lifecycle latency
without leaving the browser.

The dashboard is the **only** new code in this extension — every
generator, runner, and parser is a thin wrapper around the existing
Phase A/B/C contracts. No Phase A/B/C source files are modified.

## What you can do

- Pick constellation parameters (altitude, inclination, planes ×
  sats/plane, phase offset, min elevation, compute SAT ratio) with
  sliders, and see an analytical 3D preview update on the Globe tab.
- Configure the LLM request stream: GS set, total arrival rate Λ,
  prompt/response length distributions, bytes/token, packet payload.
- Configure the inference cost model: ``T_compute = α·L_in + β·L_out + γ``.
- Trigger a full simulation pipeline (topology → schedule → ns-3 →
  parse) from the **Run Simulation** button. Live ``stdout`` streams to
  the Logs tab. Results land in the Results tab.
- Browse any previous Phase B/C run dir (the sidebar's *Inspect a
  previous run* selector). Useful for poking at cached data without
  re-running.
- Save the current sidebar config to ``experiment.json``, or load one
  back.

## Quick start

The fastest way is the one-shot launcher:

```bash
cd /home/mark/spacesim/hypatia/extensions/dashboard
./start.sh                       # http://localhost:8501
./start.sh --port 8888 --open    # custom port + auto-open browser
./start.sh --addr 127.0.0.1      # local-only
./start.sh --help
```

`start.sh` locates the project venv, installs `requirements.txt` if any
dependency is missing, picks the port (failing fast if it's already
taken), launches Streamlit in headless mode, polls
`/_stcore/health` until it reports `200 ok`, then prints the URL.
Ctrl-C cleanly stops the underlying Streamlit subprocess.

If you'd rather drive it yourself:

```bash
source /home/mark/spacesim/venv/bin/activate
cd /home/mark/spacesim/hypatia/extensions/dashboard
pip install -r requirements.txt
streamlit run app.py
```

Without re-running anything, you can immediately use the dashboard with
cached data: pick `extensions/phase_c/runs/llm_run` from the sidebar's
*Inspect a previous run* dropdown and the Results tab will populate
with the 104-request Phase C cached run (TTFT p50 = 266 ms,
p99 = 584 ms).

## Architecture

```
dashboard/
├── app.py                       — Streamlit entry point (sidebar + 3 tabs)
├── requirements.txt             — streamlit / plotly / pandas / numpy / pyyaml
│
├── config/
│   └── schema.py                — ShellConfig / WorkloadConfig / ComputeConfig
│                                  / SimulationConfig / ExperimentConfig.
│                                  to_json / from_json / topology_hash / validate.
│
├── generators/
│   ├── topology.py              — wraps satgenpy (satgen.* directly imported),
│   │                              also drives phase_a/augment_fstate.py.
│   │                              Caches by config.topology_hash().
│   ├── schedule.py              — writes Phase C 15-column llm_workload_schedule.csv
│   └── ns3_config.py            — writes config_ns3.properties (matches Phase C
│                                  field set; no new keys)
│
├── runner/
│   └── hypatia_runner.py        — HypatiaRunner: threaded Popen wrapping
│                                  `./waf --run main_satnet …` with line-
│                                  buffered stdout streaming + timeout.
│
├── parsers/
│   └── results.py               — globs llm_{gather,compute,response}_node*.csv
│                                  from logs_ns3/, joins by req_id, computes
│                                  per-stage delays (uplink, gather, queue,
│                                  compute, return) plus TTFT and total.
│
├── visualizations/
│   ├── globe.py                 — Plotly 3D Earth + sats coloured by role
│   │                              (compute orange / transit blue) + GS green
│   │                              + optional ISL edges
│   ├── latency.py               — CDF and histogram plots
│   └── breakdown.py             — stacked-bar mean-stage breakdown +
│                                  grouped-bar per-stage percentiles
│
├── cache/                       — autogen: cache/<topology_hash>/state/…
│                                  plus satellite_roles.txt
└── runs/                        — autogen: runs/run_<timestamp>_<hash>/…
```

### Data contracts (immutable)

The dashboard treats Phase A/B/C contracts as fixed:

- **Schedule (Phase C, 15 columns)**:
  ```
  src_gs, dst_compute, lambda,
  L_in_mean, L_in_std, L_in_min, L_in_max,
  L_out_mean, L_out_std, L_out_min, L_out_max,
  bytes_per_token, packet_payload, start_ns, stop_ns
  ```
  Written by ``generators/schedule.py``.
- **Logs (Phase C, three CSV families)**:
  - ``llm_gather_node<sat>.csv`` — per-compute-SAT, has ``t_emit_ns``
    (keystone field for end-to-end timing).
  - ``llm_compute_node<sat>.csv`` — per-compute-SAT.
  - ``llm_response_node<gs>.csv`` — per-GS, multiple rows per req_id
    (one per response packet).
  All consumed by ``parsers/results.py``.
- **ns3 config keys**: only fields already accepted by Phase C's
  ``config_ns3_phase_c.properties`` are emitted; no new keys.

### Run pipeline

When **Run Simulation** is clicked, the dashboard runs these steps in
order, streaming progress to the Logs tab:

1. **Topology** (cached). ``generators.topology.generate_topology()``
   calls ``satgen.generate_tles_from_scratch_manual``,
   ``satgen.generate_plus_grid_isls``,
   ``satgen.help_dynamic_state``, and then ``phase_a/augment_fstate.py``
   to add compute-SAT-as-dst routes. Output lands in
   ``cache/<topology_hash>/``.
2. **Schedule**. ``generators.schedule.generate_schedule()`` reads the
   roles file produced in step 1 and emits the 15-col CSV.
3. **ns3 config**. ``generators.ns3_config.write_ns3_config()`` renders
   ``config_ns3.properties`` using the configured links, compute model,
   and durations.
4. **Hypatia**. ``runner.hypatia_runner.HypatiaRunner`` shells out to
   ``./waf --run main_satnet --run_dir=…`` and streams stdout into the
   UI's live log. 1-hour hard timeout.
5. **Parse**. ``parsers.results.build_lifecycle_df()`` joins the three
   CSV families by ``req_id`` and produces a per-request DataFrame with
   ``T_uplink_ns``, ``D_gather_ns``, ``T_queue_wait_ns``, ``T_compute_ns``,
   ``T_return_ns``, ``T_TTFT_ns``, ``T_total_ns``.

## Known limitations (v1)

1. **One shell only**. The schema supports multiple shells but the
   topology generator materialises only ``config.shells[0]`` — the
   sidebar grows / removes shells but the second-and-onward are ignored
   with a warning. Multi-shell needs satgen's grid generator to handle
   multiple TLE blocks, which is a bigger change.
2. **Atmospheric ISL floor not enforced**. Phase A/B/C use Starlink-550
   (72×22) where adjacent +Grid ISLs are ~2 000 km — comfortably above
   the 80 km atmospheric floor (cap ≈ 5 016 km at h=550). For the
   dashboard's parameterised constellations, Walker-Star's
   ``phase_diff=True`` plus inclination put cross-plane neighbours
   further apart than naive analytical estimates suggest (10×10 measured
   5 713 km — 14 % over the atmospheric cap). The dashboard sets
   ``max_isl_length_m`` to 99 % of the orbital diameter instead, which
   always accommodates legitimate +Grid edges but does NOT enforce the
   atmospheric clearance check. See
   ``generators/topology.py::_max_isl_length_m``.
3. **satgenpy needs ≥ 10 fstate timesteps**. The upstream code has a
   ``floor(N/10)`` divisor in its progress logger which is zero for
   N<10. The dashboard refuses to start a run when the chosen duration
   × interval doesn't clear that threshold and prints a clear message.
   We don't patch satgenpy (Phase A/B/C constraint).
4. **dst_compute is the lowest-ID type=C SAT**. ``generators/schedule.py``
   doesn't pick per-GS-nearest. The strategies hook is there
   (``dst_strategy="per_gs_round_robin"`` is implemented but not
   surfaced in the UI); proper geographic-nearest matching is left to
   future phases.
5. **No analytical / closed-form comparison**. v1 only runs the
   simulator. The "compare against analytical model" feature in the spec
   is deferred.
6. **3D globe doesn't move**. The Globe tab shows positions at the
   epoch t=0, not animated over time. Wiring up the time slider to
   propagate sats is a v2 feature.
7. **Topology preview uses analytical orbits, not SGP-4**. The 3D
   globe places sats on perfect circular orbits using the configured
   altitude / inclination / phase. The *simulation* uses SGP-4 via
   satgenpy. Visually consistent for the user but the two may drift
   apart over long durations.
8. **Compute completion ratio reporting**. The metric card needs the
   ``llm_workload_summary.csv`` file; if Phase C didn't emit it, the
   card is hidden.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| ``streamlit: command not found`` | venv not activated | ``source /home/mark/spacesim/venv/bin/activate`` |
| ``ImportError: cannot import name 'satgen'`` | running outside dashboard dir | ``cd extensions/dashboard && streamlit run app.py`` |
| Run hangs on "generating fstate" | satgenpy is single-threading a long sim | normal; expect ~1 s / timestep / 1000 sats. Cancel and lower duration if needed. |
| ``ZeroDivisionError`` from satgenpy | < 10 timesteps total | bump ``duration_seconds`` or shorten ``update_interval_ms`` |
| ``Invalid to-endpoint`` from ns-3 | Phase A C++ patch not built in | rebuild: ``cd ns3-sat-sim/simulator && ./waf`` |
| ``no fstate entry for (X, Y)`` from analyze | augment_fstate didn't add a row for that dst | re-run augment with ``--dst-sats=all-compute --rewrite`` |
| TTFT / total are negative in the table | join hit two unrelated rows | inspect ``partial requests`` expander on the Results tab |

## Files produced per run

```
runs/run_<timestamp>_<topology_hash>/
├── config_ns3.properties          ← written by ns3_config.py
├── llm_workload_schedule.csv      ← written by schedule.py
├── satellite_roles.txt -> ../../cache/<hash>/satellite_roles.txt
│                                   (symlink, read by the C++ patch)
└── logs_ns3/                      ← produced by Hypatia
    ├── console.txt
    ├── finished.txt
    ├── timing_results.{csv,txt}
    ├── llm_gather_node<sat>.csv
    ├── llm_compute_node<sat>.csv
    ├── llm_response_node<gs>.csv
    ├── llm_workload_summary.csv
    └── llm_stuck_node<sat>.csv
```

The dashboard never writes inside the Hypatia main tree — only
``dashboard/cache/`` and ``dashboard/runs/``.

## Verified components

Quick sanity check matrix at the time of writing:

| Component | Verified |
|---|---|
| All Python imports + schema JSON round-trip | ✓ |
| Streamlit serves at ``http://localhost:8501`` (health 200) | ✓ |
| Parser on cached Phase C run (104 requests, p50 TTFT 266 ms) | ✓ |
| Topology gen on 4×5 sat constellation (1.8 s end-to-end) | ✓ |
| Topology cache hit (second call: 0.00 s) | ✓ |
| All 6 viz functions render to valid Plotly HTML | ✓ |

## Future work (deferred — explicitly out of v1 scope)

- Multi-shell topology
- Per-GS nearest compute SAT
- Configurable π / φ policy (depends on Phase D)
- Single-request path animation
- Analytical model side-by-side
- Compare-multiple-saved-configs view
- Live latency updates (stream stats as the sim runs)
- Shannon-FSPL bandwidth model
- Multi-path routing
