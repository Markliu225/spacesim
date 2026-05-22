# Phase B LLM workload — three-tier latency report

**Scenario** GS → compute-SAT inference traffic. Each LLM inference
request is decomposed `request → tokens → packets`:

- 1 request = `L_in` tokens (sampled from clipped Normal at the GS app)
- 1 token   = `bytes_per_token` bytes (4 B)
- 1 packet payload = 1400 B = 350 tokens
- N_pkt per request = `ceil(L_in × 4 / 1400)`

Three latency tiers are reported per flow:

| tier | meaning |
|---|---|
| **per-packet** | `recv_time_ns − t_emit_ns` for each UDP packet |
| **per-token**  | the per-packet latency of the carrying packet, expanded by token count (each packet contributes `tokens_in_packet` samples) |
| **per-request**| `max(recv_time over all packets in request) − t_emit_ns`. This is when the compute SAT has the *full* request — Phase C's gather completion. |

Latencies in **ms**.

## Totals

- tx_request_count = **243**
- tx_packet_count  = **538**
- rx_packet_count  = **536**  (99.63% delivered)

## Per-packet latency  (one sample per UDP packet)

| flow | n_samples | min | p50 | mean | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 79 | 59.52 | 59.61 | 60.19 | 61.44 | 62.77 |
| Mumbai → C22 | 238 | 68.01 | 69.16 | 69.19 | 70.47 | 72.57 |
| Shanghai → C42 | 26 | 34.45 | 34.45 | 34.58 | 35.59 | 35.59 |
| Sao-Paulo → C32 | 83 | 79.37 | 80.51 | 80.30 | 81.66 | 83.51 |
| NY → C12 | 110 | 42.04 | 42.06 | 42.63 | 43.20 | 44.42 |

## Per-token latency  (per-packet latency × tokens-in-packet)

| flow | n_samples | min | p50 | mean | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 20738 | 59.52 | 59.58 | 60.00 | 61.44 | 62.77 |
| Mumbai → C22 | 66982 | 68.01 | 69.15 | 68.91 | 70.31 | 72.57 |
| Shanghai → C42 | 6832 | 34.45 | 34.45 | 34.45 | 34.45 | 35.59 |
| Sao-Paulo → C32 | 21850 | 79.37 | 79.37 | 80.03 | 81.66 | 83.51 |
| NY → C12 | 28330 | 42.04 | 42.05 | 42.41 | 43.20 | 44.42 |

## Per-request completion latency  (gather complete on compute SAT)

| flow | n_samples | min | p50 | mean | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 42 | 59.54 | 60.69 | 60.66 | 60.76 | 62.77 |
| Mumbai → C22 | 84 | 69.15 | 70.30 | 70.19 | 71.44 | 72.57 |
| Shanghai → C42 | 23 | 34.45 | 34.45 | 34.59 | 35.59 | 35.59 |
| Sao-Paulo → C32 | 36 | 80.51 | 80.51 | 81.02 | 81.77 | 83.51 |
| NY → C12 | 57 | 42.04 | 43.19 | 43.13 | 43.20 | 44.42 |

## Within-request gather wait  (max − min recv within req; Phase C barrier ground truth)

| flow | n_samples | min | p50 | mean | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 37 | 1.14 | 1.14 | 1.14 | 1.14 | 1.14 |
| Mumbai → C22 | 84 | 1.14 | 2.29 | 2.10 | 3.43 | 3.43 |
| Shanghai → C42 | 3 | 1.14 | 1.14 | 1.14 | 1.14 | 1.14 |
| Sao-Paulo → C32 | 36 | 1.14 | 1.14 | 1.49 | 2.29 | 2.29 |
| NY → C12 | 52 | 1.14 | 1.14 | 1.17 | 1.14 | 2.29 |

## Headline numbers per flow

| flow | λ (req/s) | L̄_in | reqs | pkts | pkt mean | tok mean | **req mean** | req p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 10 | 500 | 42 | 79 | 60.19 | 60.00 | **60.66** | 60.76 |
| Mumbai → C22 | 15 | 800 | 84 | 238 | 69.19 | 68.91 | **70.19** | 71.44 |
| Shanghai → C42 | 5 | 300 | 23 | 26 | 34.58 | 34.45 | **34.59** | 35.59 |
| Sao-Paulo → C32 | 8 | 600 | 36 | 83 | 80.30 | 80.03 | **81.02** | 81.77 |
| NY → C12 | 12 | 500 | 57 | 110 | 42.63 | 42.41 | **43.13** | 43.20 |

## Interpretation

- **per-packet vs per-token**: when L_in is balanced across requests, the two CDFs lie on top of each other up to a constant factor (token count). When requests vary a lot in length, the token CDF up-weights latencies seen by tokens of *bigger* requests (because those requests contribute more tokens).
- **per-packet vs per-request**: the gap between these two CDFs is exactly the **gather wait** — how much extra time the compute SAT has to wait after the first packet before the whole request is in. In this scenario the gap is ~1.14 ms × (N_pkt − 1) — GSL serialization at 10 Mbps dominates.
- **per-request** is the latency the LLM application *actually* experiences: the prefill stage can't begin until every token is ashore. That's the number to optimize against.

## Verdict

- delivery ≥ 95% ? : `99.63%` → `True`
- at least one packet received? : `True`

**Verdict: PASS**
