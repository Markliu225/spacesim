# Phase A — Compute-satellite endpoint support for Hypatia

This directory contains everything needed to make a **compute satellite** a
legal traffic endpoint in a Hypatia ns-3 simulation. It is the first phase
of a larger LLM-on-satellite project. Phase A's goal is intentionally
narrow: prove that an end-to-end TCP flow from a ground station to a
satellite completes, walking real ISL hops along the way.

The previous work log lives in [`phase_a_log.md`](phase_a_log.md); the
final result of the headline experiment is in
[`phase_a_result.md`](phase_a_result.md). This README is the steady-state
reference — what each file does and how to use it.

## Result at a glance

| | |
|---|---|
| Topology | Starlink-550 first shell (72 planes × 22 sats = 1584 sats), 100 ground stations |
| Compute-SAT ratio | 11.1 % (176 of 1584 sats, by-plane strategy planes 0, 8, …, 56) |
| Source | GS-0 = Tokyo (`node_id = 1584`) |
| Destination | SAT-894 (plane 40, slant range 13,253 km from Tokyo) |
| Flow | 1 MB TCP New Reno, 10 Mbps ISL/GSL, 100-pkt buffers |
| Wall-clock | < 1 min total for the actual simulation (2.5 s simulated) |
| Path | 1 GSL + 11 ISL hops, total length **21,913 km** |
| Geometric RTT lower bound | **146.2 ms** |
| Measured min RTT | **147.0 ms** (margin +0.8 ms ✓) |
| Flow completed | **YES** in 2.064 s |

## Why this needs custom code at all

Hypatia is built around ground-station endpoints. Two things in upstream
Hypatia hard-code that assumption, and Phase A unblocks both:

1. **`satgenpy/satgen/dynamic_state/fstate_calculation.py`** — both
   `calculate_fstate_shortest_path_without_gs_relaying` and
   `calculate_fstate_shortest_path_with_gs_relaying` iterate
   `for dst_gid in range(num_ground_stations)`. The fstate they emit
   has no rows with a satellite as `target_node_id`. ns-3's arbiter
   accepts SAT-as-dst at runtime (see `InitialEmptyForwardingState`),
   but the slot stays at `(-2,-2,-2)` invalid and any packet is dropped.

   **Phase A solution.** [`augment_fstate.py`](augment_fstate.py)
   reads the satgenpy-emitted state and *appends* SAT-dst rows to every
   `fstate_<t>.txt`. Logic mirrors fstate_calculation.py exactly — same
   ISL graph, same Floyd-Warshall, same interface-index conventions —
   only the dst loop is over the user's `--dst-sats` list instead of
   ground stations. **No Hypatia source is changed.**

2. **`ns3-sat-sim/.../topology-satellite-network.cc`** — the constructor
   builds `m_endpoints` by iterating *only* over ground stations.
   `IsValidEndpoint(node_id)` is consulted by the TCP-flow schedule
   reader (and the UDP-burst one); any schedule entry whose `from` or
   `to` is a satellite is rejected with `Invalid to-endpoint for a
   schedule entry based on topology: <id>`.

   **Phase A solution.** A ~25-line patch to that file makes it read
   `<run_dir>/satellite_roles.txt` after the GS-endpoints loop and add
   every type=C satellite to `m_endpoints`. **Existing runs are
   unaffected** because the patch is a no-op when the roles file is
   absent. Patch is in-tree; rebuild with `./waf` in `ns3-sat-sim/simulator/`.

The runtime forwarding plane, IP stack, routing arbiter, and SGP-4
propagation are **untouched**.

## File inventory

```
phase_a/
├── README.md                       ← this file
├── phase_a_log.md                  ← chronological work log (history)
├── phase_a_result.md               ← experiment result (PASS verdict)
│
├── satellite_roles.py              ← role assignment tool (by_plane | random)
├── satellite_roles.txt             ← the file the C++ patch reads at runtime;
│                                     also the single source of truth used by
│                                     pick_dst_sat.py and analyze_phase_a.py
│
├── augment_fstate.py               ← post-processes the satgenpy fstate dir
│                                     to add SAT-dst forwarding rows; tracks
│                                     processed (dst, t) in a sidecar manifest
│                                     `.phase_a_augment.json` inside the
│                                     dynamic-state dir
│
├── pick_dst_sat.py                 ← given a state dir + roles file + source
│                                     GS, prints the compute-SAT with the
│                                     greatest SGP-4 slant range at t=1s.
│                                     Used by run_phase_a_experiment.sh to
│                                     pick a deliberately-far target.
│
├── schedule_gs_to_compute.csv      ← TCP flow schedule (1 row): GS-0 → SAT-894,
│                                     1 MB, start at 200 ms
│
├── config_ns3_phase_a.properties   ← ns-3 config (sim 2.5 s, interval 5 s
│                                     to skip past truncated fstate files in
│                                     the broken state-gen, 10 Mbps links)
│
├── run_phase_a_experiment.sh       ← orchestrator: prereq checks → run-dir
│                                     materialisation (symlinks for config /
│                                     schedule / satellite_roles.txt) →
│                                     `./waf --run "main_satnet --run_dir=..."`
│
├── analyze_phase_a.py              ← offline analysis: reads flow CSV +
│                                     RTT samples, traces path through the
│                                     augmented fstate, computes the
│                                     geometric RTT lower bound, writes
│                                     phase_a_result.md, returns 0 iff PASS
│
├── tests/                          ← pytest suite (run via ./run_tests.sh)
│   ├── conftest.py                 ←   fixtures + sys.path setup
│   ├── test_satellite_roles.py     ←   strategies, plane bounds, determinism
│   ├── test_augment_fstate.py      ←   parsing, strip, manifest, ISL meta,
│   │                                   + integration on reduced Kuiper-630
│   ├── test_pick_dst_sat.py        ←   deterministic picker, role filter
│   ├── test_analyze_phase_a.py     ←   trace_path edge cases (drop / loop /
│   │                                   missing entry / max_hops)
│   └── test_phase_a_regression.py  ←   assert cached run still completes
│
├── run_tests.sh                    ← test entry point
│
├── runs/                           ← ns-3 outputs (gitignored — regenerable)
│   └── gs0_to_compute_sat/
│       ├── config_ns3.properties → ../../config_ns3_phase_a.properties
│       ├── schedule.csv          → ../../schedule_gs_to_compute.csv
│       ├── satellite_roles.txt   → ../../satellite_roles.txt
│       └── logs_ns3/
│           ├── tcp_flows.csv        ← flow_id,src,dst,size,start,end,
│           │                          duration,sent,completed,metadata
│           ├── tcp_flows.txt        ← same, human-readable
│           ├── tcp_flow_0_rtt.csv   ← per-ACK RTT samples (flow_id,t_ns,rtt_ns)
│           ├── tcp_flow_0_cwnd.csv  ← cwnd timeline
│           ├── tcp_flow_0_progress.csv
│           ├── isl_utilization.csv  ← per-ISL link utilization snapshots
│           ├── console.txt
│           ├── finished.txt         ← "Yes" iff sim completed cleanly
│           └── timing_results.{csv,txt}
│
└── logs/                           ← gitignored, miscellaneous logs
```

The patched C++ source file lives outside phase_a/:

```
hypatia/ns3-sat-sim/simulator/contrib/satellite-network/model/topology-satellite-network.cc
                                                                  ↑
                                            ~25-line block added after the
                                            "Only ground stations are valid
                                            endpoints" loop
```

## How to run the full pipeline from scratch

This assumes the Hypatia checkout at `/home/mark/spacesim/hypatia/` is
already built (i.e. `bash hypatia_build.sh` has been run, the venv at
`/home/mark/spacesim/venv` exists with all Python deps, and the patched
`topology-satellite-network.cc` has been compiled in by `./waf`).

```bash
source /home/mark/spacesim/venv/bin/activate
cd /home/mark/spacesim/hypatia/extensions/phase_a

# 1. Generate the constellation state (only if not already present).
#    For the Starlink-550 first shell, this is the long step (~tens of
#    minutes if it succeeds — the run we shipped was actually broken
#    midway; see `phase_a_log.md` for the workaround).
cd ../../paper/satellite_networks_state
python main_starlink_550.py 10 100 isls_plus_grid ground_stations_top_100 \
    algorithm_free_one_only_over_isls 2
cd -

# 2. Generate the role file (by-plane is the spec default).
STATE_DIR=../../paper/satellite_networks_state/gen_data/starlink_550_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls
python satellite_roles.py \
    --tles "$STATE_DIR/tles.txt" \
    --output satellite_roles.txt

# 3. Pick the destination compute SAT.
DST_SAT=$(python pick_dst_sat.py \
    --state-dir "$STATE_DIR" \
    --roles satellite_roles.txt \
    --src-gs 0 --start-time-ns 200000000)
echo "DST_SAT=$DST_SAT"

# 4. Write the schedule.
echo "0,1584,$DST_SAT,1000000,200000000,,phase_a_gs0_to_compute" \
    > schedule_gs_to_compute.csv

# 5. Augment the fstate files with SAT-dst routes for our chosen dst.
python augment_fstate.py \
    --state-dir "$STATE_DIR" \
    --dynamic-state-dir "$STATE_DIR/dynamic_state_100ms_for_10s" \
    --dst-sats "$DST_SAT"

# 6. Run ns-3. The script symlinks config / schedule / satellite_roles.txt
#    into a run dir and invokes waf.
bash run_phase_a_experiment.sh

# 7. Analyze.
python analyze_phase_a.py \
    --run-dir runs/gs0_to_compute_sat \
    --state-dir "$STATE_DIR" \
    --dynamic-state-dir "$STATE_DIR/dynamic_state_100ms_for_10s" \
    --out phase_a_result.md
```

The orchestrator (`run_phase_a_experiment.sh`) does prereq sanity
checks before invoking ns-3:

- `dynamic_state_update_interval_ns` and `simulation_end_time_ns` are
  read from the config to compute the set of `fstate_<t>.txt` files
  ns-3 will *actually* read.
- For each such timestep, both `fstate_<t>.txt` and
  `gsl_if_bandwidth_<t>.txt` must exist.
- Either the manifest (`<dyn_dir>/.phase_a_augment.json`) lists
  `(dst, t)` as augmented, or the file contains at least one row whose
  2nd column equals the schedule's `to`. The CSV-probe fallback exists
  so old (pre-manifest) runs still pass.
- No fstate file may contain a `#` line — ns-3's parser SIGIOTs on
  anything whose comma-split isn't 5 fields.

## Knobs you'll likely want to touch

| Where | Knob | Effect |
|---|---|---|
| `satellite_roles.py --strategy` | `by_plane` (default) / `random` | how to pick compute SATs |
| `satellite_roles.py --planes` | comma-separated plane indices | which planes are compute under by_plane |
| `satellite_roles.py --ratio` | float in (0, 1) | random-strategy ratio |
| `satellite_roles.py --seed` | int | random-strategy seed |
| `schedule_gs_to_compute.csv` | flow id, from, to, size, start, params, metadata | the whole flow |
| `config_ns3_phase_a.properties` | `simulation_end_time_ns` | how long to simulate (ns) |
| `config_ns3_phase_a.properties` | `dynamic_state_update_interval_ns` | how often ns-3 re-reads fstate |
| `config_ns3_phase_a.properties` | `isl_data_rate_megabit_per_s`, `gsl_data_rate_megabit_per_s` | per-link bandwidth |
| `config_ns3_phase_a.properties` | `isl_max_queue_size_pkts`, `gsl_max_queue_size_pkts` | per-link buffer depth |
| `config_ns3_phase_a.properties` | `tcp_socket_type` | `TcpNewReno` / `TcpVegas` / `TcpCubic` / `TcpBbr` (all built into ns-3) |
| `pick_dst_sat.py --start-time-ns` | int | which SGP-4 moment to use when measuring "farthest" |

## Running the tests

```bash
cd /home/mark/spacesim/hypatia/extensions/phase_a
./run_tests.sh          # full suite: 52 testcases in ~5 s
./run_tests.sh -k roles # subset matching a substring
./run_tests.sh -v       # verbose
```

The tests come in three flavours:

- **Unit** (pure-Python, instant): role-assignment strategies, the
  parsing/manifest/strip helpers, ISL metadata, path-tracing edge cases.
- **Integration** (SGP-4-backed, ~3-5 s): runs `compute_augment_rows`
  on a single timestep of the reduced Kuiper-630 state we already keep
  for the upstream Manila→Dalian integration test.
- **Regression**: asserts the cached run under `runs/gs0_to_compute_sat/`
  still reports `completed=YES`. Catches accidental breakage of the
  ns-3 patch or the augment pipeline.

Tests are skipped (not failed) if the reduced Kuiper state or the
cached run dir are absent.

## The C++ patch in detail

In
`hypatia/ns3-sat-sim/simulator/contrib/satellite-network/model/topology-satellite-network.cc`,
right after the original GS-endpoints loop:

```cpp
// Ground stations are always valid endpoints.
for (uint32_t i = 0; i < m_groundStations.size(); i++) {
    m_endpoints.insert(m_satelliteNodes.GetN() + i);
}

// Phase A extension (LLM-on-satellite):
// If the run dir ships a `satellite_roles.txt` file, also accept any
// satellite marked as type=C (compute) as a flow endpoint. Format is
// one row per satellite: `<sat_id>,<C|T>`. Missing file or absent
// rows -> no extra endpoints, so behaviour is identical to upstream
// Hypatia for runs that don't opt in.
std::string roles_path = m_basicSimulation->GetRunDir() + "/satellite_roles.txt";
if (file_exists(roles_path)) {
    std::ifstream rf(roles_path);
    std::string line;
    size_t added = 0;
    while (std::getline(rf, line)) {
        if (line.empty() || line[0] == '#') continue;
        ...
        if (k < line.size() && line[k] == 'C') {
            m_endpoints.insert(sat_id);
            ++added;
        }
    }
    std::cout << "  > Compute SATs from satellite_roles.txt added as endpoints: "
              << added << std::endl;
}
```

Properties:

- **Backwards compatible.** With no `satellite_roles.txt` in the run
  dir the constructor behaves exactly as upstream.
- **Best-effort tolerant.** Malformed rows are silently skipped rather
  than aborting; out-of-range sat IDs are silently skipped.
- **One source of truth.** The same `satellite_roles.txt` that drives
  this is read by `augment_fstate.py` (for `--dst-sats=all-compute`)
  and by `pick_dst_sat.py`. Future phases keep using it.
- **No header change**, no new public method. `m_endpoints` is already
  a `std::set<int64_t>`; we just add more ints to it.

Rebuild after editing:

```bash
cd /home/mark/spacesim/hypatia/ns3-sat-sim/simulator
PATH=/home/mark/spacesim/venv/bin:$PATH ./waf
```

In our run-of-the-mill measurement this incremental rebuild was 13 s.

## Caveats and known gotchas

These bit us during Phase A; they're written here so they bite less hard
next time.

1. **`fstate_<t>.txt` is comma-strict in ns-3.** The parser does
   `split_string(line, ",", 5)` and aborts with `std::invalid_argument`
   on any line whose comma-split length isn't 5. **Never** write
   `#`-prefixed comment lines into fstate. Use the sidecar
   `.phase_a_augment.json` manifest as the "augmented?" marker.

2. **Truncated fstate files are silent landmines.** In our state-gen
   run, 28 of 30 fstate files in the 0–2.9 s range were truncated to a
   handful of rows. `wc -l` is the fastest sanity check; the canonical
   row count is `(num_satellites + num_ground_stations - 1) × num_ground_stations`,
   e.g. 1683 × 100 = 168 300 for Starlink-550 + top-100 GS.

3. **`dynamic_state_update_interval_ns` is also the file-read cadence.**
   ns-3 reads `fstate_<t>.txt` and `gsl_if_bandwidth_<t>.txt` at every
   multiple of the interval. To skip past a sea of broken files, raise
   the interval beyond `simulation_end_time_ns` so only `t=0` is read.
   Trade-off: forwarding state is frozen during the run.

4. **GSL handover may matter at long simulation horizons.** Starlink-550
   sats move 7.6 km/s. Over a 2.5 s window the GSL anchor may rotate
   once. Our Phase A flow's start-to-end RTT distribution shows a
   single discrete RTT regime, but Phase B+ should not assume that.

## Phase B preview — what's already in place

The data contract for the rest of the project is now stable:

- `satellite_roles.txt` is the single source of truth for "is sat X
  compute?". Read by the C++ patch (drives `m_endpoints`), by
  `augment_fstate.py --dst-sats=all-compute`, and by `pick_dst_sat.py`.
- `schedule_gs_to_compute.csv` is the workload contract. Phase B will
  programmatically generate it with multiple flows.
- The augmented fstate (one set of SAT-dst rows per type=C SAT) is
  the routing contract. Once augmented for *all* type=C SATs, any flow
  that ends at any type=C SAT will route correctly, with no further
  C++ or augment changes.

The recommended Phase B entry point:

1. **Augment for the full type-C set, once.**
   ```bash
   python augment_fstate.py \
       --state-dir <state> --dynamic-state-dir <dyn> \
       --dst-sats all-compute --roles satellite_roles.txt
   ```
   The manifest tracks progress so partial reruns are cheap.
2. **Write a workload generator** that produces a multi-row
   `schedule.csv` with realistic prompt-and-response sizes and inter-
   arrival times. No new ns-3 code needed — basic-sim's TCP flow
   scheduler already supports concurrent flows.
3. **Defer the `LlmRequestApp` C++ application** (SAT-side compute
   time modelled as an in-flow stall) to Phase C. Phase B can fake it
   by writing two schedule rows per LLM request: a GS→SAT prompt flow
   and a SAT→GS response flow with start time
   `t_prompt + prompt_size / B + think_time`.

The deliberate choice here is to keep Phase B in schedule-and-Python
territory, so the next C++ change happens at the moment we genuinely
need new application semantics.

## See also

- [`phase_a_log.md`](phase_a_log.md) — chronological log including the
  two stop-and-decide moments and how they were resolved.
- [`phase_a_result.md`](phase_a_result.md) — the headline result,
  generated by `analyze_phase_a.py`.
- [`../../../使用手册.md`](../../../使用手册.md) — Chinese-language
  manual for the broader Hypatia checkout in `/home/mark/spacesim/`.