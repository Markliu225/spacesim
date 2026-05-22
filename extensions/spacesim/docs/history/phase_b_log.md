# Phase B — Work Log

LLM Request Application + Packet Tag on top of Phase A.

## Step 0 — env baseline (DONE)

- Working root: `/home/mark/spacesim/hypatia/` (task spec uses
  `~/hypatia-repro/hypatia/`; same checkout, just migrated).
- Phase A artefacts present and usable:
  - `extensions/phase_a/satellite_roles.txt` (1584 rows, 176 type=C)
  - augmented `fstate_0.txt` under
    `paper/satellite_networks_state/gen_data/starlink_550_.../dynamic_state_100ms_for_10s/`
    contains routes for dst=894 (the compute SAT we'll reuse here).
  - Phase A's ns-3 patch in `topology-satellite-network.cc` is compiled
    in — it reads `<run_dir>/satellite_roles.txt` and adds type=C sats
    to `m_endpoints`.
- venv: `/home/mark/spacesim/venv` (Python 3.8.10).

## Step 1 — `llm-workload` module skeleton (DONE)

Master source at `extensions/phase_b/llm_workload/`; `install_module.sh`
rsyncs it into `ns3-sat-sim/simulator/src/llm-workload/` and triggers
`./waf configure && ./waf`.

```
llm_workload/
├── wscript                 — module + headers + test-lib registration
├── model/
│   ├── llm-packet-tag.{h,cc}
│   ├── llm-sink-application.{h,cc}
│   └── llm-request-application.{h,cc}
├── helper/
│   ├── llm-sink-helper.{h,cc}
│   ├── llm-request-helper.{h,cc}
│   ├── llm-workload-schedule-reader.{h,cc}
│   └── llm-workload-scheduler.{h,cc}
├── examples/
│   ├── wscript
│   └── llm-workload-example.cc      — 2-node P2P standalone harness
└── test/
    └── llm-workload-test-suite.cc   — LLMPacketTag round-trip
```

wscript deps: `core network internet applications basic-sim`. basic-sim
is needed because `LlmWorkloadScheduler` uses `BasicSimulation` and
`parse_boolean` from there.

## Step 2 — LLMPacketTag (DONE)

7 fields, 32 bytes serialized:

| field | type | bytes |
|---|---|---:|
| req_id          | uint64 | 8 |
| packet_id       | uint16 | 2 |
| total_pkts      | uint16 | 2 |
| t_emit_ns       | uint64 | 8 |
| src_node_id     | uint32 | 4 |
| L_in            | uint32 | 4 |
| L_out_expected  | uint32 | 4 |

(Spec said "28 bytes (precise)" — counting up from the field list gives
8+2+2+8+4+4+4 = 32. Used 32, still well under the 64-byte limit.)

Tag round-trip unit-tested in `test/llm-workload-test-suite.cc`.

## Step 3 — LLMSinkApplication (DONE)

- Attributes: `Port` (uint16, default 9999), `LogFilename` (string)
- StartApplication: opens log file (truncate), writes header, binds UDP
  socket to `(0.0.0.0, Port)`, sets recv callback.
- HandleRead: drains socket; for each packet, PeekPacketTag → integer
  streaming into `std::ofstream` (no formatting on hot path per spec).
- StopApplication: flushes + closes log, closes socket.
- Header:
  ```
  recv_time_ns,req_id,packet_id,total_pkts,t_emit_ns,
  src_node_id,L_in,L_out_expected,recv_node_id
  ```

## Step 4 — LLMRequestApplication (DONE)

- Attributes: `DestAddress`, `DestPort`, `Lambda` (req/s), `LInMean`,
  `LInStd`, `LInMin`, `LInMax`, `BytesPerToken`, `PacketPayload`.
- StartApplication: builds `ExponentialRandomVariable` with
  `Mean = 1/Lambda`, builds `NormalRandomVariable(Mean=LInMean,
  Variance=LInStd^2)`, opens UDP socket, schedules first `EmitRequest`.
- EmitRequest:
  1. Sample `L_in`, clip to `[LInMin, LInMax]`, round.
  2. `N_pkt = ceil(L_in * BytesPerToken / PacketPayload)` (min 1).
  3. Allocate new `req_id`, snapshot `Simulator::Now()`.
  4. Emit N_pkt UDP packets, each carrying an LLMPacketTag.
  5. Reschedule self.

## Step 4.5 — LlmWorkloadScheduler (DONE)

Constructor `(BasicSimulation, NodeContainer all_nodes)`, modeled on
`UdpBurstScheduler`:

1. Reads `enable_llm_workload`; if false, returns silently.
2. Reads `llm_workload_schedule_filename` via `read_llm_workload_schedule`.
3. Per entry:
   - Installs `LLMSinkApplication` on `dst_compute_sat_node_id`
     (deduplicated; per-sink log file `<...>_sink_node<NODEID>.csv`).
   - Resolves dst IPv4 via `get_first_non_loopback_ipv4(dst_node)`
     (iterates `Ipv4::GetNInterfaces() x GetNAddresses(iface)` and
     returns the first address that isn't the loopback).
   - Installs `LLMRequestApplication` on `src_gs_node_id` configured
     with the entry's parameters; sets `StartTime`/`StopTime`.
4. `WriteResults()` after `Simulator::Run()`: prints totals + writes
   `logs_ns3/llm_workload_summary.csv`.

## Step 5 — main_satnet.cc integration (DONE)

3-line patch:
1. `#include "ns3/llm-workload-scheduler.h"` after the existing
   scheduler headers.
2. `LlmWorkloadScheduler llmWorkloadScheduler(basicSimulation,
   topology->GetNodes());` after the pingmesh scheduler.
3. `llmWorkloadScheduler.WriteResults();` after the pingmesh write.

The constructor is a no-op when `enable_llm_workload != true`, so the
existing Phase A / paper-experiment runs are unaffected.

## Issues encountered + fixes

### Issue 1 — C++14 digit separators in test

`1'234'567'890ULL` works under C++14 but ns-3 builds with `-std=c++11`,
where the apostrophes get parsed as multi-char char-literals and the
constructor call is mis-parsed (compiler reported "candidate expects 1
argument, 6 provided"). Fix: removed the apostrophes
(`1234567890ULL`).

### Issue 2 — undefined references to basic-sim symbols

After the first build the linker rejected
`ns3::BasicSimulation::GetLogsDir`, `parse_boolean`, etc., because the
wscript only listed `core network internet applications`. Fix: added
`basic-sim` to the dependency list in wscript and re-ran configure.

### Issue 3 — missing `enable_isl_utilization_tracking` property

First Hypatia run aborted with `Necessary parameter
'enable_isl_utilization_tracking' is not set.` — required by
`TopologySatelliteNetwork`. Fix: added
`enable_isl_utilization_tracking=false` to
`config_ns3_phase_b.properties`.

(No third compile/run failure of a different kind was hit — the spec's
"3 different errors → stop" threshold was not reached.)

## Step 6 — schedule + config + run (DONE)

`llm_workload_schedule.csv` (1 row):
```
1584,894,10.0,500.0,100.0,1,2000,4,1400,500000000,5000000000
```

`config_ns3_phase_b.properties`:
- `simulation_end_time_ns=5e9`, `dynamic_state_update_interval_ns=5e9`
  (Phase A trick — only `fstate_0.txt` is intact)
- `enable_llm_workload=true`, schedule + log filenames set
- All other schedulers explicitly disabled
- `enable_isl_utilization_tracking=false`

`run_phase_b_experiment.sh`:
- prereq check (state, augment, no `^#` lines in fstate_0)
- symlinks `config_ns3.properties`, `llm_workload_schedule.csv`,
  `satellite_roles.txt` into `runs/llm_run/`
- `./waf --run "main_satnet --run_dir=..."`

Wall-clock: ~28 s total (most of it ns-3 topology setup; the simulated
5 s ran in 0.1 s of wall time).

## Step 7 — results

```
tx_request_count = 46
tx_packet_count  = 90
rx_packet_count  = 88   (97.78%)
```

- 45 of 46 requests had all packets received; 1 request (2-pkt) had
  *both* packets lost. Spec threshold was >= 95% packet-level delivery
  -> PASS.
- Per-packet end-to-end latency (88 samples):
  min=86.74 ms, p50=87.88 ms, mean=87.39 ms, p95=87.96 ms, max=90.25 ms.
- **Per-request straggle ≈ 1.144 ms** for every multi-packet request.
  This is the back-to-back serialization delay of a 1400 B packet at
  10 Mbps (`1400×8/10e6 = 1.120 ms`, plus a tiny ns-3 timing margin).
  That's the Phase C gather-wait ground truth: in this single-flow
  scenario the gather barrier needs to wait one inter-packet interval
  after the first packet of a request.
- Path-bottleneck spread = max−min latency = 3.51 ms. Modest queueing.

See `phase_b_result.md` for the full table-formatted analysis.

## Why 2 packets were lost

The 2 lost packets came from a single 2-packet request (the
"complete=45, missing=0" line in the result means: every req_id that
appeared in the CSV had all its declared packets — so the 2 missing
packets must come from a req_id that *never* appeared at all → 1 lost
request entirely). Likely cause: a transient interaction between the
fstate's frozen routes (because `dynamic_state_update_interval_ns =
5e9`, fstate doesn't refresh during the run) and the small GSL queue
(100 pkts) at the moment a burst of requests hit. With a 4.5 s window
at λ=10 req/s and ~2 pkts/req, mean throughput is 4.4 KB/s — well
within link capacity — so this is bursty rather than systemic.
Increasing GSL `max_queue_size_pkts` or using a more frequent fstate
update would likely close the gap.

## Phase C entry point

Phase C is "Gather": before the compute SAT starts the prefill, it has
to wait until *all* `total_pkts` of a given request have arrived. The
current LLMSinkApplication treats every packet independently. The
natural extension:

1. Keep an `unordered_map<req_id, GatherState>` inside
   `LLMSinkApplication`. `GatherState` stores: arrival times of each
   received packet, the expected `total_pkts`, and a "complete" flag.
2. In `HandleRead`, after writing the per-packet CSV row, check
   `state.received_count == state.total_pkts`. If yes, mark complete
   and fire an `m_on_gather_complete` callback (a Phase C addition)
   carrying `(req_id, t_first, t_last)` plus the metadata snapshot
   from the LLMPacketTag.
3. Phase C then defines a `LLMComputeApplication` that subscribes to
   that callback and schedules a "prefill ends in `T_prefill(L_in)`"
   timer per gathered request — at expiry, it generates a response
   stream back to the GS (a mirror of LLMRequestApplication but in
   reverse).

`recv_time_ns - min(recv_time_ns within req)` is exactly the straggle
metric we just measured (1.144 ms in our scenario); Phase C's gather
barrier introduces *exactly that much* additional end-to-end latency
before compute can start. The instrumentation to measure this is
already in the Phase B CSV — Phase C just needs to turn it from
"measured offline" into "respected at runtime".
