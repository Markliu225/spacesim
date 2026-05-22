# Phase C — Work Log

Gather + Compute + Return loop, on top of Phase B's request-only pipeline.

## Step 0 — env baseline

- Working root: `/home/mark/spacesim/hypatia/` (≡ task spec's `~/hypatia-repro/hypatia/`).
- Phase B artefacts in place: ns-3 `llm-workload` module built, single-flow
  experiment cached, multi-flow scenario plots cached.
- Phase A `satellite_roles.txt` + augmented fstate_0 for SAT-894 available.
- No state-gen rerun needed.

## Step 1 — `LLMPacketTag::direction`

Added `enum Direction { REQUEST=0, RESPONSE=1 }` + `uint8_t m_direction`.
GetSerializedSize bumped 32 → 33 (task spec said 29 — that's a pre-Phase-B
miscount; Phase B was always 32, +1 for direction = 33).
Serialize/Deserialize updated to round-trip the new byte.
Setter and getter exposed; Phase B's existing call sites (compatible 7-arg
ctor) continue to work — they default direction = REQUEST.

## Step 2 — `LLMRequestApplication` extended

Added four new attributes: LOutMean / LOutStd / LOutMin / LOutMax. At
each `EmitRequest()`:
1. Sample L_in from N(μ, σ²), clip to [LInMin, LInMax].
2. Sample L_out from N(μ, σ²), clip to [LOutMin, LOutMax]. Stored in the
   tag's `L_out_expected` field so the compute SAT knows how long to spend
   on prefill+decode and how big the response burst should be.
3. Slice into N_pkt packets, each carries `LLMPacketTag(... direction=REQUEST)`.

## Step 3 — `ComputeApplication`

Subscribes to gather-complete via a Callback `OnGatherComplete(req_id, L_in,
L_out, src_node_id, t_emit_ns)`. Pushes a `ComputeRequest` into a single
FIFO. Service worker pops, computes
`T_compute = α·L_in + β·L_out + γ`, schedules `OnComputeComplete` via
`Simulator::Schedule(NanoSeconds(T), ...)`. On expiry: writes a row to
`compute_log.csv` (req_id, t_queue_enter, t_compute_start/end, T_compute,
T_queue_wait, L_in, L_out), sends the response burst, pops the next item.

Response burst: `N_pkt_out = ⌈L_out × bpt / payload⌉` UDP packets, each
tagged with `direction=RESPONSE`. Destination IP is resolved via a
`GsIpLookup` Callback the scheduler binds to a free function that closes
over the topology's `NodeContainer`.

## Step 4 — `GatherApplication`

Replaces (does not delete) `LLMSinkApplication`. Maintains
`std::map<req_id, GatherState>`:

  - On first packet of a new req_id: record total_pkts, t_first_arrival,
    L_in, L_out, src_node_id, schedule timeout via `Simulator::Schedule`.
  - On every packet (including the first): insert packet_id into the
    set. If `set.size() == total_pkts`, cancel timeout, write a row to
    `gather_log.csv` (req_id, t_first/last_arrival, D_gather, ...), fire
    `m_on_gather_complete(req_id, L_in, L_out, src_node_id, t_emit_ns)`,
    erase the entry.
  - On timeout: write to `stuck_log.csv`, erase entry (no compute fired).

Per-packet `PeekPacketTag` checks the direction byte and silently skips
non-REQUEST packets — defensive against accidental cross-traffic.

## Step 5 — `LLMResponseSinkApplication`

On GS. Binds UDP 19999. PeekPacketTag, filter direction=RESPONSE, write
one row to `response_log.csv`:
`req_id, gs_node_id, response_pkt_id, total_response_pkts, t_emit, t_recv,
network_return_delay_ns, src_compute_sat_id, L_in, L_out`.

Stop time is intentionally `schedule.stop_time + 5 s` so late response
packets still get logged.

## Step 6 — Scheduler dual-mode

`LlmWorkloadScheduler` now reads `enable_llm_response_loop`. False (Phase
B default) → existing behaviour (request + sink). True (Phase C) →
install `Gather + Compute` on each unique dst compute SAT and
`Request + ResponseSink` on each unique src GS. Gather wires its
`GatherCompleteCallback` to `&ComputeApplication::OnGatherComplete` of
the co-located compute. After all compute apps are created, the scheduler
binds a static-NodeContainer-closed GsIpLookup free function on each
ComputeApplication so it can find the originating GS's IPv4 address at
response time.

Schedule reader extended: rows with 11 fields are still accepted (Phase B
back-compat, L_out defaults to N(200,50) clip [1,1000]); rows with 15
fields carry explicit L_out_*.

## Step 7 — Build

Updated `wscript` to list 6 new .cc / 7 new .h files. `install_module.sh`
rsyncs source into `src/llm-workload/` and runs `./waf configure && ./waf`.
Build clean, ~10 s incremental, no warnings.

## Step 8 — Single-flow experiment

```
GS-Tokyo (1584) → SAT-894 (compute)
λ = 10 req/s, L_in = N(500,100), L_out = N(200,50)
sim 11 s (10 s active workload + 1 s drain)
compute model: α=100us/tok, β=50us/tok, γ=10ms
gather timeout: 30 s
```

Result: **104 requests, 100.00% complete the full lifecycle, 0 timeouts.**

Per-stage latency (ms, n=104):

| stage | min | p50 | mean | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| T_forward (emit → gather first arrival) | 86.63 | 86.72 | 86.75 | 86.81 | 87.63 | 88.40 |
| D_gather  (first → last arrival)        | 0.00  | 1.14  | 1.03  | 1.14  | 1.14  | 1.14  |
| T_queue_wait                            | 0.00  | 25.69 | 66.57 | 261.09 | 348.08 | 348.22 |
| T_compute (compute service)             | 42.45 | 68.25 | 67.81 | 84.85 | 87.70 | 95.10 |
| T_return  (response emit → first recv)  | 86.63 | 86.71 | 86.72 | 86.80 | 86.81 | 86.81 |
| **T_total (emit → first resp recv)**    | 219.12| 266.41| **308.89** | 498.45 | 585.15 | 599.99 |

Compute queue depth at enqueue: mean 1.36, p95 4, max 6 — λ=10 / service
mean=67.8 ms gives ρ ≈ 0.68 utilization, so non-trivial queueing builds
even on a single-flow scenario.

## Findings

1. **Routing is symmetric**: T_forward (86.75 ms) ≈ T_return (86.72 ms),
   confirming the augmented fstate routes packets the same number of ISL
   hops both ways for this (GS, compute_SAT) pair.

2. **T_compute matches analytic to within 2.2 ms**: analytic
   `α·L̄_in + β·L̄_out + γ = 100us·500 + 50us·200 + 10ms = 70 ms`;
   simulator mean = 67.81 ms. The small gap comes from variance in the
   *actual* sampled L_in / L_out per request — `E[T_compute]` is not
   exactly `T_compute(E[L_in], E[L_out])` because clipping at the lower
   bound bites slightly more than at the upper bound.

3. **Queue wait dominates the tail**: T_compute p99 = 87.7 ms but
   T_queue_wait p99 = 348 ms. The 4× higher tail latency comes purely
   from FIFO contention — a confirming sign that the single-FIFO Phase C
   abstraction is non-trivially modeling the bottleneck.

4. **Gather barrier = 1.14 ms always**: same finding as Phase B — the
   first→last spread inside a request is just the GSL serialization
   delay of one extra packet at 10 Mbps.

## Issues encountered + fixes

### Issue 1 — config path off-by-one

First run: `aborted ... File tles.txt could not be opened`. The new run
dir is `extensions/phase_c/runs/llm_run/`, one level deeper than the
Phase B run dir (`extensions/phase_b/runs/llm_run/`) — the relative
path to the Hypatia paper state dir needed `../../../../` not `../../../`.
Fix: bumped to 4 levels in `config_ns3_phase_c.properties`.

### Issue 2 — ns-3 Callback can't wrap lambdas with captures

First attempt at the GS-IP lookup used `MakeCallback<...>(+[](){...})`
with a capture-lambda. ns-3's `Callback` template only accepts function
pointers / member functions / no-capture lambdas (which decay to function
pointers). Fix: use a static `NodeContainer` and a `+[](uint32_t)`
no-capture lambda that closes over the static variable.

### Issue 3 — sink keeps running past schedule.stop

LLMResponseSinkApplication's StopTime was originally set to schedule.stop;
but the compute SAT keeps emitting response bursts for ~70 ms (+ network
return ~90 ms) past the last request emission, so late responses were
being dropped on the floor. Fix: extend response-sink stop to
`schedule.stop + 5 s`.

(No third compile/run failure with a different signature — the "3 errors
of different kinds" stop condition was not triggered.)

## Phase D entry point

The data + control plane for Phase D ("policy layer") is now in place:

1. **The compute SAT is the policy enforcement point.** GatherApplication
   already buffers (req_id, L_in, L_out) into a queue; ComputeApplication
   picks the head. Today it's strict FIFO. Phase D's "policy" replaces
   the queue with a min-heap / multi-queue / SLO-aware scheduler. Hook:
   `ComputeApplication::StartNextCompute()` decides which queued request
   to serve next.

2. **GS-side admission control / load shedding** would live in
   LLMRequestApplication (drop a request if "compute is overloaded" — but
   we'd need a control channel back, see point 4).

3. **Multi-compute placement decisions** (which compute SAT to send each
   request to) live in `LlmWorkloadScheduler::InstallPhaseC` when it
   resolves the request's destination IP. Today this is statically fixed
   in the CSV; Phase D could replace it with a callback that picks the
   best compute SAT per request based on real-time queue depth observed
   via a side channel.

4. **Control plane**: there is no feedback from compute SAT to GS today.
   Phase D could either piggyback on the response burst (add a queue-depth
   byte to the response Tag) or open a separate small UDP control
   channel. The Tag is already 33 B with one byte of headroom each side
   of the natural alignment, so a few new fields are cheap.

The simplest "policy" demo for Phase D: replace the single-FIFO with two
FIFOs (interactive / batch) keyed off `L_out`, watch tail latency split
between the two classes. Should be 30 lines of code on top of
ComputeApplication, all within `llm-workload/`.
