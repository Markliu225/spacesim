# spacesim — LLM-on-Satellite Simulator: Technical Report

This document is a function-level map of the spacesim simulator: the
satellite-network topology generator, the ns-3 LLM-workload module, the
Azure-trace traffic generator, the Streamlit dashboard, the analysis
pipeline, and how all of it composes into a single run. It is meant for
someone who has read the README and now needs to understand or modify
the code.

The codebase lives at `/home/mark/spacesim/hypatia/`. Hypatia (a third-
party LEO-network simulator on top of ns-3) is at `hypatia/ns3-sat-sim/`
and `hypatia/paper/`. Everything spacesim adds is under
`hypatia/extensions/`:

```
hypatia/extensions/
├── spacesim/                    # main package (Python + ns-3 module)
│   ├── analysis/                # lifecycle reconstruction, summary stats
│   ├── cache/                   # topology gen_data, by topology hash
│   ├── config/                  # ExperimentConfig dataclass
│   ├── dashboard/               # Streamlit UI (app.py)
│   ├── docs/                    # this report + history/
│   ├── runner/                  # HypatiaRunner: subprocess wrapper for waf
│   ├── runs/                    # per-run dirs created by the dashboard
│   ├── scenarios/               # scripted "demo" runs (CLI, not UI)
│   ├── tests/                   # pytest suite
│   ├── topology/                # role assignment, dst picker, build()
│   ├── viz/                     # Plotly figures (globe + latency + breakdown)
│   └── workload/                # schedule writers + ns-3 module source
└── traffic_generator/           # Azure-trace-driven event generator
    ├── azure_trace/             # downloaded Azure CSVs
    ├── ground_stations.json     # 10-city default config
    ├── nhpp_generator.py        # NHPP thinning
    ├── real_run/per_gs/         # output of `python traffic_gen.py …`
    ├── trace_fitter.py          # Azure trace → diurnal shape + L_in/L_out
    └── traffic_gen.py           # CLI orchestrator
```

The remainder of this document is organised by subsystem, with a
function-level index at the end (see [Function index](#function-index)).

---

## 1. Architecture at a glance

A spacesim run is a four-stage pipeline:

```
                    ┌─────────────────────────────────────────┐
                    │            ExperimentConfig             │
                    │  shells • workload • compute • simulation│
                    └────────┬────────────────────────┬────────┘
                             │                        │
              topology hash  ▼                        ▼
       ┌──────────────────────────┐         ┌───────────────────┐
       │  Topology generator      │         │  Schedule writer  │
       │  (cached by shells only) │         │  schedule.py OR   │
       │                          │         │  events_replay.py │
       │  satgenpy → state/       │         └───────────────────┘
       │  topology/roles.py → C/T │                   │
       │  fstate_augment.py       │                   │
       └─────────────┬────────────┘                   ▼
                     │                ┌─────────────────────────┐
                     ▼                │ ns3 config writer       │
              run_dir/                │ ns3_config.py           │
              ├── config_ns3.properties────────────────┘
              ├── llm_workload_schedule.csv            │
              ├── satellite_roles.txt                  │
              ├── events/*.csv (replay mode only)      │
              └── logs_ns3/                            ▼
                                          ┌─────────────────────┐
                                          │ HypatiaRunner       │
                                          │ ./waf --run main_satnet
                                          │ → ns-3 LLM workload │
                                          └──────────┬──────────┘
                                                     │
                                                     ▼
                                        per-stage CSVs in logs_ns3/
                                        analysis/lifecycle.py
                                        viz/ → dashboard Results tab
```

A single request flows through five physical stages once the simulator
is running:

```
GS                          compute SAT
 │  ─── TCP CONNECT ───►       │
 │  ─── LLMHeader (24B) ─►     │  ┐
 │  ─── prompt payload ──►     │  ├ Gather (D_gather_ns)
 │  ─── ShutdownSend ────►     │  ┘
 │                             │      ─── enter FIFO queue
 │                             │           T_queue_wait_ns
 │                             │      ─── start inference
 │                             │           T_compute_ns
 │                             │      = α·L_in + β·L_out + γ
 │  ◄── response payload ──    │
 │  ◄── FIN ──────────────     │
```

Plus a prologue (`T_uplink_ns = t_first_arrival_ns - t_emit_ns`,
network-only latency before the request reaches the compute SAT) and an
epilogue (`T_return_ns = t_response_last_recv_ns - t_compute_end_ns`,
the response trip back to the GS). End-to-end latency `T_total_ns =
t_response_last_recv_ns - t_emit_ns` decomposes additively:

```
T_total = T_uplink + D_gather + T_queue_wait + T_compute + T_return
```

`T_TTFT` (time-to-first-token) replaces `t_response_last_recv_ns` with
`t_response_first_recv_ns`. These per-stage values are derived in
[`analysis/lifecycle.py`](#5-analysislifecyclepy).

---

## 2. Configuration: `config/schema.py`

The dashboard, the topology generator, the schedule writers, and the
ns-3 config writer all read from a single `ExperimentConfig` dataclass.
The schema is the contract between the UI and the pipeline.

[`config/schema.py`](../config/schema.py):

### `ShellConfig` (one orbital shell)
- `altitude_km` (float, default 550): GEO not supported; validated to
  `[300, 2000]`.
- `inclination_deg` (float, default 53).
- `num_planes`, `sats_per_plane` (int, default 10 each): total sats =
  `num_planes × sats_per_plane`.
- `phase_offset` (int, default 0): Walker-Star inter-plane phase shift,
  validated to `[0, num_planes)`.
- `min_elevation_deg` (float, default 25): minimum elevation a GS needs
  to a satellite to consider it visible.
- `isl_pattern` (literal `"+Grid"`): only +Grid (in-plane neighbour +
  cross-plane neighbour) is supported.
- `compute_ratio` (float in `(0, 1]`, default 0.10): fraction of
  satellites tagged `C` (compute) — the rest are `T` (transit).
- `total_sats` property and `mean_motion_rev_per_day` (derived from
  altitude via `T = 2π·√(a³/μ)`).

### `WorkloadConfig`

| field | default | meaning |
|---|---|---|
| `gs_set` | `"top_5_cities"` | choice of `top_5_cities` / `top_20_cities` / `top_100_cities`; selects how many of Hypatia's pre-defined GS the topology generator places. |
| `source` | `"synthetic"` | `"synthetic"` (Poisson arrival, Normal L_in, both sampled at runtime by ns-3) or `"trace_replay"` (every request comes from a per-GS events CSV produced by the traffic_generator). |
| `trace_per_gs_dir` | `""` | absolute path to a directory of `events_gs<N>_*.csv` files. Honoured only when `source == "trace_replay"`. |
| `trace_stage_mode` | `"copy"` | `"copy"` or `"symlink"` — how the events files end up in the run dir. |
| `dst_strategy` | `"first_compute"` | how each GS chooses its compute SAT: `"first_compute"` (lowest-ID C satellite, all GS converge) or `"per_gs_round_robin"` (each GS pinned to a different C, modulo the C set). |
| `lambda_total` | 10 req/s | total arrival rate summed across all GS, used only in synthetic mode. Per-GS λ = `lambda_total / num_gs`. |
| `L_in_mean / std / min / max` | 500 / 100 / 1 / 2000 | prompt-token distribution. In synthetic mode L_in ~ Normal clamped to `[min, max]`; in trace_replay the mean / std are ignored and the clamps still apply. |
| `L_out_mean / std / min / max` | 200 / 50 / 1 / 1000 | response-token distribution. Always sampled inside `ComputeApplication` (Option B from the design). |
| `bytes_per_token` | 4 | payload byte count per token, both directions. |
| `packet_payload` | 1400 | legacy Phase B / UDP knob; still written to the schedule so the C++ reader stays compatible. |

### `ComputeConfig`
LLM inference cost model: `T_compute = α·L_in + β·L_out + γ`.
- `alpha_ns_per_input_token` (default 100 µs/token).
- `beta_ns_per_output_token` (default 50 µs/token).
- `gamma_ns` (default 10 ms — model load / KV-cache warmup overhead).

### `SimulationConfig`
- `duration_seconds` (int, default 30, validated to `[5, 600]`).
- `epoch_iso` (default `2024-01-01T00:00:00Z`): UTC time corresponding
  to sim t=0. Used by satgenpy to compute initial satellite positions.
- `update_interval_ms` (int from `{100, 250, 500, 1000, 2000, 5000}`):
  forwarding-state grid spacing. Lower interval = more accurate
  handovers but proportionally more `.fstate` files to generate.

### `ExperimentConfig` (top-level)

Aggregates the four sub-configs. Methods:

- `to_dict()`, `to_json(indent=2)` — `dataclasses.asdict` + JSON dump.
- `from_dict(d)`, `from_json(s)` — tolerant of missing/unknown keys.
- `topology_hash()` — SHA-1 (first 16 hex chars) of `shells` only;
  workload/compute/simulation deliberately excluded so that twiddling
  request rate or sim duration reuses the cached constellation.
- `validate()` — returns a list of human-readable error strings; the
  dashboard refuses to run if non-empty. Notable rules: trace_replay
  mode requires a real existing directory, synthetic mode requires
  `lambda_total > 0`, both modes require `L_in_min ≤ L_in_max` and
  `L_out_min ≤ L_out_max`.

---

## 3. Topology pipeline: `topology/`

The topology layer takes a shell description (Walker-Star params) and a
GS set, and produces everything the ns-3 simulator needs to model the
constellation: TLEs, ISLs, GSL parameters, dynamic forwarding state,
and the role file (which satellites are compute endpoints). Output is
cached under `cache/<topology_hash>/`.

### 3.1 `topology/build.py` (Walker-Star generator)

Public entry: `generate_topology(config, cache_dir, on_progress=None)`.
Inspects `cache/<hash>/.dashboard_topology_ok`; if present, returns the
cached `TopologyResult` (`state_dir`, `dynamic_state_dir`, `roles_path`,
`num_satellites`, `num_ground_stations`, `cache_hit=True`).

On a cache miss it runs satgenpy in sequence:

1. **TLE generation** — `paper/satellite_networks_state/main_starlink_550.py` style invocation, but with the shell's altitude / inclination / planes / sats-per-plane / phase. Writes `tles.txt`.
2. **ISL pattern** — `+Grid` produces `2 × num_planes × sats_per_plane` ISL edges (each sat has 4 neighbours: ±1 plane, ±1 in-plane).
3. **GSL interfaces** — top-K cities from `paper/.../ground_stations_cities_sorted_by_estimated_2025_pop_top_100.basic.txt`, K from `WorkloadConfig.gs_set`. Each GS gets `num_gsl_interfaces=1`.
4. **Dynamic forwarding state** — `algorithm_free_one_only_over_isls` (Hypatia's "shortest-path over ISLs, GS-side single GSL") generates one `fstate_<t>.txt` per `update_interval_ms` for the whole sim window.
5. **Role assignment** — `_assign_compute_planes(num_planes, compute_ratio)` picks `ceil(compute_ratio × num_planes)` planes evenly spaced (e.g. ratio 0.10 over 10 planes → plane 0; ratio 0.20 → planes 0 and 5). Every satellite in a chosen plane is `C`, others `T`. Result written via [`topology/roles.py:write_roles()`](#33-topologyrolespy) to `<cache>/satellite_roles.txt`.
6. **fstate augmentation** — for each compute SAT, calls an external `fstate_augment.py` subprocess that rewrites each `fstate_<t>.txt` to add destination routes ending at every compute SAT (Hypatia's default fstate covers only sat→GS forwarding). Without augmentation, the GS→C TCP flows would be silently dropped at the first hop. A `.phase_a_augment.json` manifest records which dst sats are augmented.

Internal helpers worth noting:

- `_assign_compute_planes(num_planes, ratio)` (lines ~80–105): chooses planes evenly spaced. Returns a sorted list of plane indices.
- `_write_roles(roles, path)` proxies to `roles.write_roles`.

`TopologyResult` is a dataclass that the runner inspects to find the
state dirs to point ns-3 at.

### 3.2 `topology/dst_picker.py` (CLI utility)

Standalone CLI used by some scripted scenarios:

`read_roles(roles_path) → List[int]` returns sat IDs marked `C` from
`satellite_roles.txt`.

The `main()` runs SGP-4 (via Skyfield) at `start_time_ns`, computes
great-circle distance from a given source GS to each `C` SAT, and
prints the *farthest* one. Used by demo scripts that intentionally
exercise multi-hop ISL paths.

### 3.3 `topology/roles.py` (C/T tagging)

- `read_tles_header(path)`: parses the first line of `tles.txt`
  (`<num_planes> <sats_per_plane>`).
- `assign_by_plane(num_planes, sats_per_plane, compute_planes)`: yields
  a list of `(sat_id, role)` tuples. Default `compute_planes` covers
  every 8th plane (`[0, 8, 16, 24, 32, 40, 48, 56]`), giving ~11% C on
  a Starlink-550-shaped grid.
- `assign_random(num_planes, sats_per_plane, ratio, seed)`: samples
  `int(ratio × total)` random sat IDs and marks them C.
- `write_roles(roles, output_path)`: outputs CSV `<sat_id>,<role>`,
  sorted by sat_id.

The dashboard always uses plane-based assignment for predictability;
the random variant is exposed as a fall-back for one-off scenarios.

---

## 4. Workload: schedule writers + ns-3 module

Workload lives under [`workload/`](../workload/). Two layers:

- **Python schedule writers** — turn `ExperimentConfig.workload` into
  the schedule CSV the ns-3 module consumes.
- **C++ ns-3 module** — `LLMRequestApplication` (TCP client),
  `GatherApplication` (TCP server, prompt collection),
  `ComputeApplication` (queue + inference + response sender),
  `LlmWorkloadScheduler` (binds apps to nodes from the schedule).

### 4.1 Schedule CSV formats

The schedule reader accepts **three column counts**, all consumed by
the same `LlmWorkloadScheduler::InstallSchedule()` loop.

[`workload/ns3_module/helper/llm-workload-schedule-reader.h`](../workload/ns3_module/helper/llm-workload-schedule-reader.h)
defines `LlmWorkloadEntry`. Columns:

| # | name | mode | notes |
|---:|---|---|---|
| 0 | `src_gs_node_id` | all | int, ns-3 node id of the source GS |
| 1 | `dst_compute_sat_node_id` | all | int, ns-3 node id of the target compute SAT |
| 2 | `lambda_req_per_sec` | synthetic | float; ignored when `events_filename` non-empty |
| 3–6 | `L_in_{mean,std,min,max}` | synthetic | `mean/std` ignored in replay mode; clamps `min/max` still apply |
| 7–10 | `L_out_{mean,std,min,max}` | 15- or 16-col only | always honoured by `ComputeApplication` |
| 11 | `bytes_per_token` | all | int |
| 12 | `packet_payload` | all | legacy Phase B knob; unused in TCP path |
| 13 | `start_time_ns` | all | nanoseconds since sim t=0 when the app starts |
| 14 | `stop_time_ns` | all | clamped to `simulation_end_time_ns` |
| 15 | `events_filename` | 16-col only | path relative to the run dir; when non-empty, switches the source app to trace-replay mode |

The legacy **11-column** rows are Phase B; the reader fills L_out
defaults (mean=200, std=50, [1, 1000]) so old fixtures still load. The
**15-column** rows are the standard synthetic format. The **16-column**
rows are the trace-replay format.

[`workload/ns3_module/helper/llm-workload-schedule-reader.cc`](../workload/ns3_module/helper/llm-workload-schedule-reader.cc):

- `split_comma(line) → vector<string>` — naïve `getline` on `,`.
- `read_llm_workload_schedule(path, simulation_end_time_ns) → vector<LlmWorkloadEntry>`:
  - Strips trailing `\r` from each line (CRLF tolerance — Python's
    `csv.writer` defaults to CRLF; without this, `\r` ends up appended
    to the last field, which silently breaks `events_filename` as a
    file path).
  - Skips blank lines and lines starting with `#`.
  - Validates: `lambda > 0` (synthetic only — skipped when
    `events_filename` non-empty), `start_time < stop_time`,
    `L_in_max ≥ L_in_min`.
  - Clamps `stop_time_ns` to `simulation_end_time_ns`.

### 4.2 Synthetic schedule writer: `workload/schedule.py`

`generate_schedule(config, output_path, *, num_satellites, num_ground_stations, roles_path, dst_strategy="first_compute") → int`:

1. Reads compute SAT IDs via `read_compute_sat_ids(roles_path)` (a small
   helper that filters `satellite_roles.txt` for `role == "C"`).
2. Per GS, computes `src_gs_node = num_satellites + gs_idx` (Hypatia
   numbers GSes after all satellites).
3. Picks dst SAT per strategy:
   - `"first_compute"` — every GS → lowest-ID C SAT.
   - `"per_gs_round_robin"` — `compute_sats[gid % len(compute_sats)]`.
4. Per-GS λ = `lambda_total / max(num_gs, 1)`.
5. Window: `start_ns = 500 ms`, `stop_ns = sim_duration - 500 ms`
   (the 0.5 s leading edge gives Hypatia's routing time to settle; the
   trailing edge leaves room for late responses).
6. Writes a 15-column CSV via `_format_row()`, which forces integer
   fields to integers and float fields to `f"{v:g}"` so the C++ parser
   sees the canonical Phase C format.

Returns the row count.

### 4.3 Trace-replay adapter: `workload/events_replay.py`

`install_events_replay(config, *, per_gs_dir, run_dir, num_satellites, gs_idx_to_node_id=None, dst_strategy="first_compute", roles_path=None, dst_compute_sat_ids=None, schedule_filename="llm_workload_schedule.csv", stage_mode="copy", staged_subdir="events", l_out_mean=200.0, l_out_std=100.0, l_out_min=1, l_out_max=2000) → int`:

1. **Discover** per-GS files via `discover_per_gs_files(per_gs_dir)`,
   which globs `events_gs<N>_*.csv` and returns `{gs_idx: Path}`.
2. **Stage** each file into `<run_dir>/<staged_subdir>/`. Two modes:
   - `"copy"`: portable, immune to source dir going away mid-run.
   - `"symlink"`: cheap, useful for 100 MB+ traces during development.
3. **Map** `gs_idx → node_id`. Default `num_satellites + gs_idx` matches
   Hypatia's GS numbering (the traffic_generator's `ground_stations.json`
   and Hypatia's top-100 GS file share the same first 10 entries:
   Tokyo, Delhi, Shanghai, Sao-Paulo, Mumbai, Mexico-City, Beijing,
   Osaka, Cairo, New-York).
4. **Pick** dst SAT per `dst_strategy` (same options as the synthetic
   writer).
5. **Emit** a 16-column row per GS. `lambda` is set to `1.0` (a
   non-zero sentinel — the reader skips the > 0 check in replay mode,
   but other consumers might not). `L_out_*` columns reflect the
   distribution `ComputeApplication` will sample from, NOT the trace's
   L_out (which is ignored end-to-end; see §4.6).
6. Writes the schedule with `lineterminator="\n"` to avoid CRLF
   pollution of the last field.

`discover_per_gs_files()` raises if there are duplicate gs_idx (two
files matching the same N) or if the directory is empty.

### 4.4 ns-3 config writer: `workload/ns3_config.py`

`write_ns3_config(config, run_dir, *, state_dir, dynamic_state_dir, schedule_filename="llm_workload_schedule.csv", log_filename="llm.csv", seed=24681012) → Path`:

Renders `<run_dir>/config_ns3.properties`, a `basic-sim`
`PropertiesConfig`-compatible file. Key entries:

- `simulation_end_time_ns`, `simulation_seed`.
- `satellite_network_dir`, `satellite_network_routes_dir` — **must be
  paths relative to the run dir**; an absolute value silently becomes
  `<run_dir>/<abs_path>` (Hypatia bug) and the simulator aborts.
- `dynamic_state_update_interval_ns`.
- Link constants (`isl_data_rate_megabit_per_s=10.0`, etc.).
- `tcp_socket_type=TcpNewReno`.
- `enable_llm_workload=true` + `llm_workload_schedule_filename` +
  `llm_workload_log_filename`.
- `compute_alpha_ns_per_input_token`, `compute_beta_...`, `compute_gamma_ns`.
- `enable_tcp_flow_scheduler=false`, `enable_udp_burst_scheduler=false`,
  `enable_pingmesh_scheduler=false` — make sure no other Hypatia
  scheduler is active concurrently.

### 4.5 ns-3 C++ — LLM wire format: `llm-header.h`

24-byte little-endian struct, sent at the start of every TCP stream.

```cpp
struct LLMHeader {
    static constexpr uint32_t SIZE_BYTES = 24;
    uint64_t t_emit_ns;     // bytes  0..7  : GS wall-clock emit time
    uint32_t req_id;        // bytes  8..11 : per-source counter
    uint32_t src_node_id;   // bytes 12..15 : originating GS ns-3 node id
    uint32_t L_in;          // bytes 16..19 : prompt token count
    uint32_t reserved;      // bytes 20..23 : always 0
    void Pack  (uint8_t buf[24]) const;
    void Unpack(const uint8_t buf[24]);
};
```

The header is in-band — `LLMRequestApplication` writes it as the first
24 bytes of the stream, `GatherApplication` accumulates exactly 24
bytes for phase 1 before any payload byte counts toward phase 2.

### 4.6 ns-3 C++ — Request side: `LLMRequestApplication`

[`workload/ns3_module/model/llm-request-application.h/cc`](../workload/ns3_module/model/llm-request-application.cc).

Inherits `ns3::Application`. Behaviour: on each scheduled emit it opens
a fresh TCP socket to the destination compute SAT, sends an LLMHeader
followed by `L_in × bytes_per_token` zero-valued bytes, half-closes the
send side, drains incoming response bytes, and writes a row to the
response log on FIN.

#### TypeId attributes

| name | type | default | meaning |
|---|---|---|---|
| `DestAddress` | Address | — | destination compute SAT IPv4 |
| `DestPort` | uint16 | 9999 | destination TCP port |
| `Lambda` | double | 10.0 | request rate (req/s); synthetic mode only |
| `LInMean / LInStd` | double | 500 / 100 | Normal sampler params; synthetic mode only |
| `LInMin / LInMax` | uint32 | 1 / 2000 | clamps; honoured in both modes |
| `BytesPerToken` | uint32 | 4 | payload sizing |
| `ResponseLogFilename` | string | — | per-GS response CSV path |
| `EventsFilename` | string | `""` | when non-empty, switches to trace-replay mode |

#### Lifecycle (synthetic mode)

`StartApplication()`:
- Opens the response log and writes its header.
- If `m_events_filename.empty()`:
  - Constructs `m_iat_rv` (Exponential, mean `1/m_lambda`).
  - Constructs `m_L_in_rv` (Normal, mean `m_L_in_mean`, variance `std²`).
  - Calls `ScheduleNext()`.

`ScheduleNext()` schedules `EmitRequest()` after `m_iat_rv->GetValue()` seconds.

`EmitRequest()`:
- Samples L_in, clamps to `[LInMin, LInMax]`, rounds.
- Calls `EmitRequestWithLIn(L_in)`.
- Calls `ScheduleNext()` (lazy chain).

#### Lifecycle (trace-replay mode)

`StartApplication()`:
- Opens the response log, same as above.
- If `m_events_filename.non_empty()`, calls `ScheduleEventsFromFile()`.

`ScheduleEventsFromFile()`:
- Opens the events CSV (path is absolute, set by the scheduler from
  `GetRunDir() + "/" + events_filename`).
- Skips a leading non-numeric line (header tolerance).
- For each row, parses `(req_id, src_gs_idx, t_emit_ns, L_in, L_out)`.
  The `req_id` and `src_gs_idx` are not used by the app (the app
  generates its own monotonic `req_id` per emit); `L_in` is clamped
  and used; `L_out` is ignored (compute samples its own).
- Schedules `EmitRequestWithLIn(L_in)` at
  `Simulator::Schedule(NanoSeconds(t_emit_ns - now), ...)`.
- Logs `LLMRequest node X replay: scheduled=Y skipped=Z` so a single
  glance at the console confirms the file was consumed.

Events with `t_emit_ns < now` (the StartTime moved past them) are
counted as skipped.

#### Per-emit TCP work: `EmitRequestWithLIn(L_in)`

Common to both modes.

1. Allocates a `ReqConn` record holding `(req_id, L_in, t_emit_ns,
   total_send_bytes = 24 + L_in × bytes_per_token, bytes_sent=0,
   header_sent=false, finished_send=false, bytes_received=0,
   t_first_byte_ns=0, t_last_byte_ns=0, logged=false)`.
2. Creates a TCP socket, sets four callbacks
   (`OnConnectSuccess` / `OnConnectFail`, `OnClosedNormal` /
   `OnClosedError`), one recv callback (`OnRecv`), one send callback
   (`OnDataSent`).
3. `Bind()` + `Connect()` to `(DestAddress, DestPort)`.
4. Increments `m_tx_pkt_count`.

#### Send pipeline

`PumpSend(sock)` is the workhorse:
- Writes the 24-byte LLMHeader first (if not already).
- Then loops, writing chunks of `min(remaining, 65000)` bytes until
  either `bytes_sent == total_send_bytes` or `Send()` returns short.
- When fully sent, calls `sock->ShutdownSend()` and marks
  `finished_send = true`.

`OnConnectSuccess()` calls `PumpSend()` (kick off).
`OnDataSent(sock, txAvail)` calls `PumpSend()` (resume after TCP
backpressure releases the send buffer).
`OnConnectFail()` removes the conn entry — the request is silently
dropped; this only happens in pathological topology states.

#### Response handling

`OnRecv(sock)`: drains `RecvFrom` in a loop, accumulating
`bytes_received`, capturing `t_first_byte_ns` on the first byte and
updating `t_last_byte_ns` on each.

`OnClosedNormal(sock)`: the compute SAT half-closed (`ShutdownSend`)
its end of the socket — response is done. Recovers L_out as
`bytes_received / bytes_per_token` (a *recovered* value; the truth
is in the compute log, but this lets the response log stand alone).
Writes **two** rows to the response log: `response_pkt_id=0` with
`t_response_recv_ns = t_first_byte_ns` and `response_pkt_id=1` with
`t_response_recv_ns = t_last_byte_ns`. This dual-row shape is a
deliberate compat hack so the existing `lifecycle.py` join (which
aggregates `min/max` per req_id) recovers TTFT / total without parser
changes.

`OnClosedError(sock)`: rare; logs nothing.

#### Response-log columns

```
req_id,gs_node_id,response_pkt_id,total_response_pkts,
t_response_emit_ns,t_response_recv_ns,network_return_delay_ns,
src_compute_sat_id,L_in,L_out
```

The `src_compute_sat_id` is `-1` (the GS doesn't know which compute
SAT a response came from — that's only knowable on the compute side;
the lifecycle reconstruction fills it in from the compute log). The
emit/recv ns and L_in/L_out come straight from `ReqConn` state.

### 4.7 ns-3 C++ — Gather side: `GatherApplication`

TCP server on each compute SAT. Two-phase per connection: 24-byte
LLMHeader, then L_in × bytes_per_token bytes of prompt payload. When
both phases finish, fires a callback (set by `LlmWorkloadScheduler`) to
hand the in-progress socket off to `ComputeApplication` together with
the parsed header.

#### Attributes

| name | type | default |
|---|---|---|
| `Port` | uint16 | 9999 |
| `BytesPerToken` | uint32 | 4 |
| `LogFilename` | string | — |
| `StuckLogFilename` | string | — |

#### Key callbacks

- `HandleAccept(sock, from)` accepts every connection and installs
  recv / close callbacks on the new socket. Creates a `ConnState`
  record `{req_id=0 (filled later), expected_payload=0, header_buf,
  header_bytes=0, payload_bytes=0, t_first_arrival_ns, t_last_arrival_ns,
  src_node_id, L_in, t_emit_ns, header_done=false}`.
- `HandleRead(sock)`:
  - Drains the socket in a loop.
  - Phase 1: until 24 header bytes are buffered, copies into
    `header_buf` and increments `header_bytes`.
  - On reaching 24, `LLMHeader::Unpack(header_buf, hdr)` extracts
    `(t_emit_ns, req_id, src_node_id, L_in)`; sets
    `expected_payload = L_in × bytes_per_token` and `header_done = true`.
  - Phase 2: increments `payload_bytes` by the byte count not absorbed
    by phase 1.
  - When `payload_bytes >= expected_payload`, fires
    `m_gather_complete_cb(sock, hdr, t_first_arrival_ns,
    t_last_arrival_ns)` and detaches local recv callbacks (the socket
    ownership transfers to `ComputeApplication`).
- `HandlePeerClose(sock)` / `HandlePeerError(sock)`: writes a row to
  the stuck log if the conn never completed; cleans up local state.

#### Gather log columns

```
req_id,compute_sat_id,t_first_arrival_ns,t_last_arrival_ns,
D_gather_ns,total_pkts_expected,total_pkts_received,
src_node_id,L_in,L_out_expected,t_emit_ns
```

- `D_gather_ns = t_last_arrival_ns - t_first_arrival_ns`.
- `total_pkts_*` are hardcoded to 2 (legacy from the UDP era; for TCP
  these are not meaningful — keep them stable for the parser).
- `L_out_expected = 0` (Option B: the SAT samples its own L_out
  separately, this column is no longer filled).

### 4.8 ns-3 C++ — Compute side: `ComputeApplication`

The compute SAT's inference simulator. Single FIFO queue, single
server. `OnGatherComplete` is invoked by `GatherApplication` once a
prompt is fully received and the parsed header is available.

#### Attributes

| name | type | default | meaning |
|---|---|---|---|
| `AlphaNsPerInputToken` | uint64 | 100 000 | prefill coefficient α |
| `BetaNsPerOutputToken` | uint64 | 50 000 | decode coefficient β |
| `GammaNs` | uint64 | 10 000 000 | fixed overhead γ |
| `BytesPerToken` | uint32 | 4 | response payload sizing |
| `LOutMean / LOutStd / LOutMin / LOutMax` | mixed | 200 / 50 / 1 / 1000 | Normal L_out sampler |
| `LogFilename` | string | — | compute CSV path |

#### Lifecycle

`OnGatherComplete(sock, hdr, t_first_arrival_ns, t_last_arrival_ns)`:
- Samples L_out from Normal(LOutMean, LOutStd²), clamps, rounds.
- Enqueues `(sock, hdr, L_out, t_queue_enter_ns = now)`.
- If the server is idle, calls `StartNextCompute()`.

`StartNextCompute()`:
- Pops the queue front; computes
  `T_compute_ns = α·L_in + β·L_out + γ`.
- Records `t_compute_start_ns = now`, `T_queue_wait_ns = now -
  t_queue_enter_ns`.
- Schedules `OnComputeComplete(slot)` after `T_compute_ns`.

`OnComputeComplete(slot)`:
- Records `t_compute_end_ns = now`, `T_compute_ns = now -
  t_compute_start_ns`.
- Writes a row to the compute log.
- Calls `SendResponse(slot)` to hand the socket back to the network.
- Calls `StartNextCompute()` if the queue is non-empty.

`SendResponse(slot)`:
- Computes `total_response_bytes = L_out × bytes_per_token`.
- Allocates a `RespState` and starts pumping with `PumpResponse()`.

`PumpResponse(sock)`: same chunk-loop pattern as `LLMRequestApplication`.
When `bytes_sent == total_response_bytes`, calls `ShutdownSend()`. The
GS will then see EOF on the response side and write its response log.

#### Compute log columns

```
req_id,compute_sat_id,src_node_id,t_queue_enter_ns,
t_compute_start_ns,t_compute_end_ns,
T_compute_ns,T_queue_wait_ns,L_in,L_out
```

### 4.9 ns-3 C++ — Helpers & scheduler

`LLMRequestHelper`, `GatherHelper`, `ComputeHelper` are thin
`ObjectFactory` wrappers that expose `SetAttribute(name, value)` and
`Install(node) → ApplicationContainer`. Nothing tricky.

[`LlmWorkloadScheduler`](../workload/ns3_module/helper/llm-workload-scheduler.cc)
is the binding layer between the schedule CSV and the C++ apps.

`LlmWorkloadScheduler(basicSimulation, all_nodes)`:
- Reads `enable_llm_workload`; bails out if false.
- Reads the schedule file path (`llm_workload_schedule_filename`) and
  parses it via `read_llm_workload_schedule()`.
- Reads `compute_alpha_ns_per_input_token`, `compute_beta_...`,
  `compute_gamma_ns`. These are *global* (not per-row) — the first
  GS entry's `bytes_per_token` and L_out_* are also adopted as the
  per-compute-SAT defaults; later rows targeting the same SAT inherit
  them silently.
- Resolves the log path template
  `<logs_dir>/<basename>` (default `llm.csv`).
- Calls `InstallSchedule()`.

`InstallSchedule()`:
- For each entry, ensures GS and compute-SAT node ids are valid.
- **Per compute SAT (once):** installs one `ComputeApplication` and
  one `GatherApplication` on the SAT node, wired so that
  `GatherApplication`'s completion callback invokes
  `ComputeApplication::OnGatherComplete`. Both apps have
  `StartTime = entry.start_time_ns` and `StopTime =
  simulation_end_time_ns` (i.e. they outlive the request workload so
  late responses still log).
- **Per schedule row:** installs an `LLMRequestApplication` on the
  source GS node. Routing logic:
  - Always sets `LInMin`, `LInMax`, `BytesPerToken`,
    `ResponseLogFilename`.
  - If `entry.events_filename.empty()`: sets `Lambda`, `LInMean`,
    `LInStd` (synthetic mode).
  - Else: resolves `events_path = GetRunDir() + "/" +
    entry.events_filename` and sets `EventsFilename` (trace-replay
    mode). Distribution attributes are deliberately not set so any
    default-vs-row mismatch is caught.

A `WriteResults()` method runs at sim end. It accumulates
`tx_request_count`, `tx_request_packets`, `gather_complete_count`, and
`compute_complete_count` across all installed apps and writes
`logs_ns3/llm_workload_summary.csv`. This file is the "did the
workload actually run" sanity check.

### 4.10 Build & install: `install_ns3_module.sh`

`workload/install_ns3_module.sh` rsyncs `workload/ns3_module/` into
`ns3-sat-sim/simulator/src/llm-workload/` (with `--delete` so removed
files at the source vanish at the destination), then runs:

```
./waf configure --build-profile=debug --enable-mpi --enable-examples \
                --enable-tests --enable-gcov --out=build/debug_all
./waf
```

The script verifies the module is listed in `build/debug_all/c4che/_cache.py`
and that the example binary exists. Re-run this after any change to
the C++ source.

---

## 5. `analysis/lifecycle.py`

Joins the three per-stage CSVs into a single per-request DataFrame and
derives the timing breakdown.

[`analysis/lifecycle.py`](../analysis/lifecycle.py).

### Public entry points

- `load_logs(logs_dir) → LogBundle`: reads `llm_gather_node*.csv`,
  `llm_compute_node*.csv`, `llm_response_node*.csv` into three
  concatenated DataFrames. Returns a dataclass with attributes
  `gather`, `compute`, `response` and the raw `summary`.
- `load_summary(logs_dir) → DataFrame`: reads
  `llm_workload_summary.csv` (a single-row CSV with the per-stage
  counters from `LlmWorkloadScheduler::WriteResults()`).
- `build_lifecycle_df(logs) → DataFrame`: the main join. Steps:
  1. **Aggregate response** — for each `(req_id, gs_node_id)`,
     compute `t_response_first_emit_ns = min(t_response_emit_ns)`,
     `t_response_first_recv_ns = min(t_response_recv_ns)`,
     `t_response_last_recv_ns = max(t_response_recv_ns)`. This is what
     collapses the deliberate two-row-per-request response log into
     one row per request.
  2. **Inner join** `gather ∪ compute` on
     `(req_id, compute_sat_id, src_node_id, L_in)`. The compound key
     handles multi-GS scenarios where `req_id` is only unique
     per-source.
  3. **Inner join** the result with the aggregated response on
     `(req_id, src_node_id == gs_node_id)`.
  4. Compute derived columns:
     - `T_uplink_ns = t_first_arrival_ns - t_emit_ns`
     - `T_first_response_ns = t_response_first_recv_ns -
       t_response_first_emit_ns`
     - `T_return_ns = t_response_last_recv_ns - t_compute_end_ns`
     - `T_TTFT_ns = t_response_first_recv_ns - t_emit_ns`
     - `T_total_ns = t_response_last_recv_ns - t_emit_ns`
  5. Returns the DataFrame, fully reconstructed.
- `enumerate_partial_requests(logs)`: returns the rows where one of
  the three stages saw a `req_id` that the others did not — used by
  the Results tab to surface drops / stuck requests.
- `summarise(df) → dict`: returns `{n, mean_TTFT_ms, p50_TTFT_ms,
  p99_TTFT_ms, mean_total_ms, p50_total_ms, p99_total_ms}` (ns → ms
  via division by 1e6).
- `stage_means_ms(df) → dict`: returns
  `{uplink, gather, queue, compute, return}` in ms — the inputs to
  the stacked-bar breakdown plot.

### Implementation notes

- The compound join key (`req_id` + `compute_sat_id` + `src_node_id` +
  `L_in`) costs one extra column-match versus a naïve `req_id` join,
  but removes a class of subtle bugs where two GS happened to land the
  same `req_id` value on the same compute SAT within a sim window.
- Missing logs are tolerated — `build_lifecycle_df` returns an empty
  DataFrame if any of the three stages has zero rows. The dashboard
  then surfaces "no completed requests" rather than crashing.

---

## 6. Visualization: `viz/`

Three Plotly figure factories, all consumed by the dashboard's Results
tab.

### `viz/breakdown.py`

- `plot_stage_breakdown(stage_means_ms) → Figure`: horizontal stacked
  bar with one stack per stage (uplink / gather / queue / compute /
  return) in fixed colors (blue / purple / orange / red / green). Text
  labels show the millisecond value inside each segment. The sum
  appears at the right.
- `plot_stage_percentiles(df) → Figure`: grouped bar showing p50 /
  mean / p99 of each stage's per-request distribution. Useful for
  spotting "tail-bound" stages where the mean tracks p50 but p99 is
  10× higher (typically compute, occasionally queue under load).

### `viz/latency.py`

- `plot_cdf(df, col, title=None) → Figure`: empirical CDF of `df[col]`
  converted ns → ms. Annotates p50 / p90 / p99 with vertical dotted
  lines and labels. Used for both `T_TTFT_ns` and `T_total_ns`.
- `plot_histogram(df, col, title=None, bins=30) → Figure`: same column
  in ms as a 30-bin histogram.

### `viz/globe.py`

- `make_earth_surface()`: semi-transparent sphere at R_E = 6371 km.
- `make_satellites_trace(positions_ecef, roles)`: Scatter3d traces,
  one for `C` (orange diamond) and one for `T` (blue circle).
- `make_ground_stations_trace(gs_records)`: green diamond markers
  with labels.
- `make_isl_lines_trace(positions_ecef, isls, max_edges=1000)`:
  faint grey line segments for ISL edges; clips to 1000 edges so a
  full Starlink-1500 doesn't render to a black sphere.
- `make_globe_figure(positions_ecef, roles, gs_records, *, isls=None,
  title=None)`: composes the traces. If total sats > 2000, downsamples
  the transit set (keeps all compute) so Plotly stays responsive.

---

## 7. Runner: `runner/hypatia.py`

`HypatiaRunner(run_dir, *, simulator_dir=..., timeout_seconds=3600)`:
- `start()` spawns `./waf --run "main_satnet --run_dir=<run_dir>"` in
  a subprocess (via `subprocess.Popen`), captures stdout+stderr in a
  background thread that buffers lines.
- `consume_new_lines() → list[str]`: pops the buffer (the dashboard
  calls this in a 500 ms loop to stream live output).
- `is_done() → bool`: returns True once the subprocess has exited.
- `finalise() → RunResult`: waits for completion, returns a dataclass
  `{success, timed_out, returncode, duration_seconds, console_text}`.

The runner deliberately does not interpret ns-3's output — it's a
plumbing layer. The dashboard does its own pattern matching for
progress milestones.

---

## 8. Dashboard: `dashboard/app.py`

Single-file Streamlit app, ~700 LOC.

[`dashboard/app.py`](../dashboard/app.py).

### Layout

Sidebar form + three main tabs:

```
┌── sidebar ──────────────────┐  ┌── tabs ──────────────────────┐
│ Constellation               │  │ 🌍 3D Globe                  │
│ Ground traffic              │  │ 📊 Results                   │
│   ├─ GS set                 │  │ 📜 Logs                      │
│   ├─ Workload source        │  └──────────────────────────────┘
│   │     synthetic | replay  │
│   ├─ GS→C routing strategy  │
│   ├─ (synthetic only)       │
│   │     λ_total, L_in_*     │
│   ├─ (replay only)          │
│   │     trace dir + preview │
│   └─ L_out_*, bytes/token   │
│ Compute model               │
│ Simulation                  │
│ [Run / Save / Load]         │
│ Inspect a previous run      │
└─────────────────────────────┘
```

### Core helpers

- `_init_session()`: creates `st.session_state.config` (default
  `ExperimentConfig`), plus run-state slots (`last_result`,
  `last_run_dir`, `last_topology`, `live_log`, `external_run_dir`).
- `_sidebar()`: renders the form. Each widget writes directly into
  `cfg.workload.<field>` / `cfg.compute.<field>` / `cfg.simulation.<field>` —
  there is no shadow state-key indirection. Widget set adapts to
  `cfg.workload.source`:
  - synthetic: λ_total slider, L_in_{mean,std,min,max} number_inputs.
  - trace_replay: text input for the per_gs directory, stage_mode
    radio, a live `_render_trace_preview` table (file count + event
    counts), plus L_in min/max clamp inputs.
- `_render_trace_preview(trace_dir_str)`: walks the directory via
  `discover_per_gs_files`, line-counts each CSV (header-aware), and
  displays a small table with file name and event count. Errors are
  rendered as warnings, not exceptions, so the user can keep editing
  the path.
- `_build_preview_globe(cfg, *, show_isls)`: analytical (non-SGP-4)
  positioning for the 3D globe tab. Faster than full satgenpy
  generation and good enough for a "what would this constellation
  look like" preview.
- `_load_gs_records(gs_set)`: cached read of Hypatia's top-100 GS
  file, returning the first 5 / 20 / 100 entries.
- `_load_active_results()`: chooses between the latest run dir (from
  the current session) and the user-selected "previous run" dir
  (sidebar dropdown), then loads logs.

### Pipeline trigger: `_do_run_simulation()`

The big one. Runs after the "Run Simulation" button is clicked:

1. Validate `cfg`; bail with errors if any.
2. Mkdir `RUNS_ROOT / run_<timestamp>_<topology_hash>/`.
3. **Step 1/4 — topology** (`generate_topology(cfg, CACHE_DIR)`).
   Streams progress lines into the Logs tab live log via the
   `on_progress` callback.
4. **Step 2/4 — schedule**. Branches on `cfg.workload.source`:
   - `"synthetic"` → `generate_schedule(cfg, schedule_path, …)`.
   - `"trace_replay"` → `install_events_replay(cfg, per_gs_dir=…,
     run_dir=…, …)`, which stages event files into the run dir and
     emits a 16-column schedule.
   The two branches are mutually exclusive — the schedule reader on
   the C++ side accepts both column counts so the rest of the
   pipeline does not need to know which it is.
5. Symlink `<run_dir>/satellite_roles.txt` to the cached roles file.
6. **Step 3/4 — ns3 config** (`write_ns3_config`).
7. **Step 4/4 — simulate**. Constructs a `HypatiaRunner`, calls
   `start()`, polls `is_done()` in a 500 ms loop while draining
   `consume_new_lines()` into the live log, then `finalise()`. Records
   the result on `st.session_state.last_result`.

Errors are caught and surfaced as a red banner; the traceback goes
into the live log for forensics.

### Results tab: `_tab_results()`

Uses `_load_active_results()` to get a lifecycle DataFrame, then:
- Top metrics row: completed-request count, TTFT mean (+ p50), total
  mean (+ p99), compute completion ratio.
- CDF + histogram of `T_TTFT_ns` and `T_total_ns` (4 plots in a 2×2
  grid).
- Stacked-bar stage breakdown (`plot_stage_breakdown`).
- Grouped-bar stage percentiles (`plot_stage_percentiles`).
- A wide table of per-stage `(mean, p50, p90, p99, max)` in ms.

### Logs tab: `_tab_logs()`

Live stdout (last 80 lines) + per-prefix CSV previews (gather /
compute / response / summary / stuck) for the current run dir. Each
CSV is loaded with `pd.read_csv(p, nrows=100)`.

---

## 9. Traffic generator: `traffic_generator/`

The traffic generator turns a real Azure trace into per-GS event CSVs
that the simulator can replay verbatim. It lives outside the spacesim
package because it is an independent tool — you can run it once and
re-use the output across many simulator runs.

### 9.1 Conceptual model

We model each ground station as a non-homogeneous Poisson process
(NHPP) whose rate is `λ_peak × d(τ)`, where:

- `λ_peak` is the GS's configured peak request rate (req/s).
- `d(τ) ∈ [0, 1]` is a *trace-derived* normalized rate shape at local
  hour-of-day `τ`. `d(τ)` peaks at 1 during the busiest hour and
  drops near 0 at quiet hours. The shape is learned from a real
  Azure trace.
- `τ = (utc_sec/3600 + lon_deg/15) mod 24` — local solar time, so
  Tokyo's peak (UTC ≈ 8) and Sao-Paulo's peak (UTC ≈ 17) are both
  near *local* 14–17h.

For each accepted event time `t`, we sample `(L_in, L_out)` from the
trace's per-hour empirical distribution (not a parametric fit — see
below). The resulting events are saved one CSV per GS.

### 9.2 `trace_fitter.py`

Class `AzureTraceFitter(trace_csv_path, *, max_rows=None)`:

- `fit() → FitStats`: reads `TIMESTAMP / ContextTokens /
  GeneratedTokens`, drops invalid rows, computes `d[24]` (histogram
  normalized to peak=1) and `_buckets[24]` (each is an
  `(n_in_bucket, 2) int64` array of `(L_in, L_out)` samples).
- `rate_shape(tau)`: linear interpolation between hour bins, wrapping
  24→0. Cheap; called once per NHPP probe.
- `sample_length(tau, rng)`: picks a `(L_in, L_out)` pair uniformly
  from the bucket for `floor(tau)`. Falls back to the nearest
  non-empty bucket if a bucket is empty (rare but possible at thin
  hours in short traces).

Empirical sampling is preferred over KDE here because the trace has a
heavy tail (a few 8k+ token prompts per hour). KDE would smear them
across implausible ranges.

`make_synthetic_azure_trace(output_path, *, n_rows=200000, days=1,
peak_hour_utc=14.0, rng=None)`: emits a CSV with the same three
columns and an approximately realistic diurnal shape (Gaussian bump,
σ=4 h, 0.1 baseline). Log-normal token counts. Used when no real
Azure trace is available — the generator silently falls back to it.

### 9.3 `nhpp_generator.py`

- `estimate_lambda_max(rate_func, duration_sec, *, n_probe=1000,
  safety=1.1) → float`: probes `rate_func` at 1000 evenly-spaced
  points, takes the max, multiplies by 1.1 as a safety margin.
- `generate_nhpp_events(duration_sec, rate_func, rng, *, n_probe=1000,
  safety=1.1) → list[float]`: Lewis-Shedler thinning. Generates
  candidate events from a homogeneous Poisson process at `lambda_max`,
  accepts each with probability `rate_func(t) / lambda_max`. The
  probability is clamped at 1.0 (always accept) in the rare case the
  probe grid missed a sharper peak.

Thinning beats time-rescaling here because `rate_func` is cheap and
the acceptance ratio is high (≥ 1/24 trough-to-peak on the Azure
trace).

### 9.4 `traffic_gen.py` (CLI)

`generate(azure_trace, gs_config, duration_sec, output_dir, *,
seed=42, max_trace_rows=None, report=False) → dict`:

1. If `azure_trace` missing/empty, generate a synthetic one in the
   output dir.
2. Construct `AzureTraceFitter` and call `fit()`.
3. For each GS in `gs_config`:
   - Define `rate_func(t) = gs.peak_lambda × fitter.rate_shape(local_time(t, gs.lon))`.
   - Call `generate_nhpp_events(duration_sec, rate_func, rng)`.
   - For each event time, sample `(L_in, L_out)` via
     `fitter.sample_length(local_time(t, gs.lon), rng)`.
   - Sort by `t_emit_ns`, assign GS-local `req_id` 0..N-1.
   - Stream-write to `<output_dir>/per_gs/events_gs<idx>_<slug>.csv`.
4. If `--report`, render a markdown report plus two diagnostic PNGs
   (`diurnal_shape.png`, `per_gs_hourly.png`).

GS-local `req_id` matters because the simulator-side
`LLMRequestApplication` generates its own monotonic req_id per emit;
the trace's req_id is informational only.

### 9.5 `ground_stations.json`

Default 10-city config used by the traffic generator. Ordering and
gs_idx values match Hypatia's `ground_stations_cities_sorted_…_top_100`:
0 Tokyo, 1 Delhi, 2 Shanghai, 3 Sao-Paulo, 4 Mumbai, 5 Mexico-City,
6 Beijing, 7 Osaka, 8 Cairo, 9 New-York. All have `peak_lambda=10.0`
by default — change per-GS in this file if you want city-specific
load.

Because the indices are aligned, `gs_idx_to_node_id = lambda i:
num_satellites + i` is correct as long as the dashboard's `gs_set`
covers at least the first N cities the trace targets. For partial
overlap, pass a custom `gs_idx_to_node_id` to `install_events_replay`.

### 9.6 Operating the generator

```
cd hypatia/extensions/traffic_generator
python traffic_gen.py \
    --azure-trace azure_trace/AzureLMMInferenceTrace_multimodal.csv \
    --gs-config ground_stations.json \
    --duration-sec 86400 \
    --output-dir real_run \
    --report
```

Produces:

```
real_run/
├── per_gs/
│   ├── events_gs0_tokyo.csv
│   ├── events_gs1_delhi.csv
│   ├── …
│   └── events_gs9_new_york.csv
├── diurnal_shape.png
├── per_gs_hourly.png
└── generator_report.md
```

Then in the dashboard sidebar set **Workload source → Trace replay**
and point at `…/traffic_generator/real_run/per_gs`. The runner stages
the files into the run dir and writes the 16-column schedule.

---

## 10. Scenarios: `scenarios/`

A handful of scripted, non-UI demo runs. They predate the dashboard
and are kept for regression coverage. Most relevant:

- `scenarios/llm_workload/` — Phase B-era multi-GS, multi-compute
  scenario. The `run.sh` script symlinks `gen_data/` from Phase A,
  validates fstate augmentation, and invokes waf. `analyze.py`
  builds a lifecycle DataFrame and writes `result.md`.
- `scenarios/llm_full_lifecycle/` — same shape, but exercises full
  TCP request → gather → compute → response. Used as the canonical
  "this is what the data looks like" demo.
- `scenarios/llm_events_replay/` — created in this task. Demonstrates
  trace-replay end-to-end with a trimmed slice of the Azure trace
  (5 cities × 5 s window, ~35 events each).
- `scenarios/mixed_topology/` — Phase A constellation generator
  (60 sats, 5 GS); the gen_data here is what every other scenario
  symlinks into its own `gen_data/`.

Each scenario directory contains a `run.sh` and a `make_plots.sh`
plus its own `analyze.py`. The dashboard's "Inspect a previous run"
selector also picks them up so demo data is browsable in the UI.

---

## 11. Run-directory layout

After a single run, you get:

```
runs/run_20260522-002500_2af946877431c077/
├── config_ns3.properties           # ns3_config.write_ns3_config
├── llm_workload_schedule.csv       # schedule.generate_schedule or install_events_replay
├── satellite_roles.txt             # symlink → cache/<hash>/satellite_roles.txt
├── events/                         # (only when source=trace_replay)
│   ├── events_gs0_tokyo.csv
│   └── …
└── logs_ns3/
    ├── console.txt                 # captured ns-3 stdout (from runner)
    ├── finished.txt                # basic-sim's "ok" sentinel
    ├── llm_workload_summary.csv    # per-stage counters
    ├── llm_gather_node<sat>.csv    # one per compute SAT
    ├── llm_stuck_node<sat>.csv     # one per compute SAT (mostly empty under TCP)
    ├── llm_compute_node<sat>.csv   # one per compute SAT
    └── llm_response_node<gs>.csv   # one per source GS
```

The `logs_ns3/` subdirectory is the contract the analysis layer reads.
The "node IDs" follow Hypatia's convention: sat IDs `0..total_sats-1`,
GS IDs `total_sats..total_sats+num_gs-1`.

---

## 12. Tests: `tests/`

Pytest, runnable via `cd extensions/spacesim && bash run_tests.sh` or
`pytest -x`. The interesting coverage:

- `test_fstate_augment.py` — exercises the per-compute-SAT augmentation
  with a tiny constellation (3 sats + 2 GS) and asserts the manifest
  layout.
- `test_dst_picker.py` — geometry sanity for `dst_picker`.
- `test_roles.py` — round-trips `assign_by_plane` and `write_roles`.
- `test_analyze_legacy.py` — protects the lifecycle-DataFrame shape
  (column names + a few dtype invariants) against accidental
  regressions.
- `test_e2e_mixed_topology.py` — slow path, builds a small
  constellation and runs the simulator end-to-end. Tagged `slow`.
- `test_regression.py` — pins per-stage means of a checked-in scenario
  so a future change to compute defaults / Hypatia routing surfaces
  loudly.

`conftest.py` provides a project-root path fixture plus a `tmpdir_with_cache`
helper that copies a minimal pre-built topology cache into a tmp dir.

---

## 13. Cost model: choosing α, β, γ

`T_compute = α·L_in + β·L_out + γ` is a deliberately simple linear
model. Defaults (100 µs/token in, 50 µs/token out, 10 ms overhead)
roughly match the published characteristics of mid-size LLMs (e.g.
Llama-2 7B on a single A100) but should be tuned per-model:

- Pre-fill (`α·L_in`) is the cost of *reading* the prompt. For most
  transformer LLMs it grows linearly with L_in (per-layer attention
  matrices) for the typical L_in range we care about.
- Decode (`β·L_out`) is autoregressive token generation. β tends to
  be lower than α because each decode step touches the KV cache
  rather than the full attention matrix.
- Overhead (`γ`) absorbs model load / KV-cache warmup / scheduler
  latency at the inference server.

The model is enforced by `ComputeApplication::StartNextCompute` in
absolute ns. The compute log records `T_compute_ns` after the fact, so
post-hoc you can fit (α, β, γ) per real workload trace and feed those
back into the dashboard.

---

## 14. End-to-end example: a trace-replay run

1. Generate a per-GS trace (one-time, ~60 s for 24 h × 10 cities):
   ```
   cd hypatia/extensions/traffic_generator
   python traffic_gen.py \
       --azure-trace azure_trace/AzureLMMInferenceTrace_multimodal.csv \
       --gs-config ground_stations.json \
       --duration-sec 86400 \
       --output-dir real_run --report
   ```
2. Launch the dashboard:
   ```
   cd hypatia/extensions/spacesim
   bash start.sh
   ```
3. In the sidebar:
   - Constellation: e.g. 53° / 10 planes × 10 sats / 550 km.
   - Ground traffic → Workload source → **Trace replay**.
   - Per-GS events directory → defaults to
     `…/traffic_generator/real_run/per_gs` (the sidebar pre-fills
     this for you). The preview table shows 10 files and the total
     event count.
   - GS → C routing → `per_gs_round_robin` (so each city pins to a
     different compute SAT, exercising more of the network).
   - Stage mode → `copy` (default).
   - L_out distribution → keep the defaults (200 ± 100, [1, 2000])
     unless you have model-specific numbers.
   - Compute model → default α/β/γ.
   - Simulation → `duration_seconds` ≥ 30 (you want at least a few
     compute completions per GS to populate the latency CDFs).
4. Click **Run Simulation**.
5. After ~1–2 min wallclock (topology cached after the first run; first
   run is 20–60 s for topology generation), the Results tab populates
   with latency CDFs and the stacked-bar stage breakdown.

---

## 15. Recent changes (relevant to this report)

The codebase recently absorbed two intertwined features. Their imprint
runs through this document:

1. **Trace-replay mode** — three layers added/modified:
   - C++ (`LlmWorkloadEntry` gains `events_filename`; schedule reader
     accepts 11/15/16-column rows + strips trailing `\r`;
     `LLMRequestApplication` adds an `EventsFilename` attribute and
     `ScheduleEventsFromFile()` method; `LlmWorkloadScheduler`
     resolves events paths relative to the run dir and skips
     distribution attributes in replay mode).
   - Python adapter (`workload/events_replay.py`:
     `discover_per_gs_files`, `install_events_replay`).
   - Dashboard integration (`config/schema.py` gains
     `source/trace_per_gs_dir/trace_stage_mode/dst_strategy` fields;
     `dashboard/app.py` adds a source toggle + path picker + preview
     + branched pipeline call).

2. **CRLF safety** — Python's `csv.writer` defaults to CRLF. The C++
   schedule reader now strips trailing `\r` from every line, *and*
   `install_events_replay` writes LF-only files. Both fixes are needed
   so neither side becomes the new single point of failure.

The dashboard's synthetic-mode behaviour is unchanged; the existing
schedule writer + 15-column CSV path is preserved verbatim.

---

## Function index

| where | function | role |
|---|---|---|
| `config/schema.py` | `ExperimentConfig.{to,from}_{dict,json}` | round-trip config to JSON |
| `config/schema.py` | `ExperimentConfig.topology_hash` | cache key (shells only) |
| `config/schema.py` | `ExperimentConfig.validate` | sidebar gate |
| `topology/build.py` | `generate_topology` | satgenpy + roles + fstate augment |
| `topology/roles.py` | `assign_by_plane`, `assign_random`, `write_roles` | C/T tagging |
| `topology/dst_picker.py` | `read_roles`, `main` | CLI: pick farthest C SAT |
| `workload/schedule.py` | `read_compute_sat_ids` | filter `satellite_roles.txt` for `C` |
| `workload/schedule.py` | `generate_schedule` | write 15-col synthetic schedule |
| `workload/events_replay.py` | `discover_per_gs_files` | glob events files in a dir |
| `workload/events_replay.py` | `install_events_replay` | stage events + write 16-col schedule |
| `workload/ns3_config.py` | `write_ns3_config` | render `config_ns3.properties` |
| `workload/.../llm-workload-schedule-reader.cc` | `read_llm_workload_schedule` | parse 11/15/16-col schedule (CRLF tolerant) |
| `workload/.../llm-workload-scheduler.cc` | `LlmWorkloadScheduler::InstallSchedule` | bind apps to nodes per row |
| `workload/.../llm-workload-scheduler.cc` | `WriteResults` | per-stage counters → summary CSV |
| `workload/.../llm-request-application.cc` | `StartApplication` | branch on EventsFilename |
| `workload/.../llm-request-application.cc` | `ScheduleEventsFromFile` | trace replay scheduling |
| `workload/.../llm-request-application.cc` | `ScheduleNext` / `EmitRequest` | synthetic Poisson + Normal sampling |
| `workload/.../llm-request-application.cc` | `EmitRequestWithLIn` | actual TCP emit (common path) |
| `workload/.../llm-request-application.cc` | `PumpSend` | header + payload TCP send pipeline |
| `workload/.../llm-request-application.cc` | `OnRecv` / `OnClosedNormal` | response capture + 2-row response log |
| `workload/.../gather-application.cc` | `HandleAccept` / `HandleRead` | two-phase prompt collection |
| `workload/.../compute-application.cc` | `OnGatherComplete` / `StartNextCompute` / `OnComputeComplete` | FIFO queue + linear T_compute |
| `workload/.../compute-application.cc` | `SendResponse` / `PumpResponse` | response TCP send pipeline |
| `traffic_generator/trace_fitter.py` | `AzureTraceFitter.fit` / `rate_shape` / `sample_length` | trace ingestion + lookup |
| `traffic_generator/trace_fitter.py` | `make_synthetic_azure_trace` | fallback when no real trace |
| `traffic_generator/nhpp_generator.py` | `estimate_lambda_max` / `generate_nhpp_events` | Lewis-Shedler thinning |
| `traffic_generator/traffic_gen.py` | `generate` | CLI orchestrator (10 GS × 24 h ≈ 6.5 M events in ~60 s) |
| `traffic_generator/traffic_gen.py` | `write_events_csv` / `write_report` | per-GS CSV + markdown report |
| `runner/hypatia.py` | `HypatiaRunner.{start,is_done,consume_new_lines,finalise}` | subprocess wrapper |
| `analysis/lifecycle.py` | `load_logs` / `build_lifecycle_df` | per-stage CSVs → joined DataFrame |
| `analysis/lifecycle.py` | `summarise` / `stage_means_ms` / `enumerate_partial_requests` | dashboard inputs |
| `viz/breakdown.py` | `plot_stage_breakdown` / `plot_stage_percentiles` | latency breakdown plots |
| `viz/latency.py` | `plot_cdf` / `plot_histogram` | latency distribution plots |
| `viz/globe.py` | `make_*_trace` / `make_globe_figure` | 3D constellation viz |
| `dashboard/app.py` | `_sidebar` / `_render_trace_preview` | form + trace dir preview |
| `dashboard/app.py` | `_do_run_simulation` | the 4-step pipeline (branched on workload source) |
| `dashboard/app.py` | `_tab_globe` / `_tab_results` / `_tab_logs` | tab renderers |

---

## Glossary

| term | meaning |
|---|---|
| **C / T** | Compute / Transit satellite role. Set by `satellite_roles.txt`. |
| **D_gather_ns** | `t_last_arrival_ns - t_first_arrival_ns` — time the prompt spent arriving at the compute SAT (TCP framing). |
| **fstate** | A snapshot of the forwarding table, indexed by time. Hypatia generates one per `update_interval_ms`. |
| **GSL / ISL** | Ground-Sat Link / Inter-Sat Link. |
| **L_in / L_out** | Prompt-token count / response-token count. |
| **NHPP** | Non-homogeneous Poisson process. Used to model diurnal request rate. |
| **req_id** | Per-source request counter. Combined with `src_node_id` to be globally unique. |
| **TTFT** | Time-to-first-token: `t_response_first_recv_ns - t_emit_ns`. |
| **T_total_ns** | End-to-end latency: `t_response_last_recv_ns - t_emit_ns`. |
| **synthetic / trace_replay** | The two workload sources. Synthetic samples requests at ns-3 runtime; trace_replay reads them from a per-GS CSV. |
