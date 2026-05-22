# Phase C full-lifecycle — four-tier latency report

**Scenario.** Five concurrent LLM inference flows; each request
is decomposed `request → tokens → packets` *twice* (forward at
the GS, response at the compute SAT). Per-flow we measure:

- **per-packet (forward)**: each request UDP packet's `recv_time − t_emit` (1 sample / pkt).
- **per-token (forward)**: same latency, replicated by the packet's token count (token-weighted).
- **per-response-packet**: each response packet's `t_response_recv − t_response_emit` (return-leg network delay).
- **TTFT** (Time To First Token): first response packet's `t_response_recv − request t_emit`. This is what the LLM end-user perceives.
- **T_total**: last response packet's recv − request emit.

## Totals

- tx_request_count = **59**
- tx_request_packets = **128**
- rx_request_packets = **128**
- gather_complete_count = **59**
- gather_timeout_count = **0**
- compute_complete_count = **59**
- response_recv_packets = **66**

## Tier 1 — per-packet (forward, GS → compute SAT)

| flow | n | min | p50 | mean | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 13 | 59.55 | 59.59 | 60.10 | 60.73 | 60.73 | 60.73 |
| Mumbai → C22 | 50 | 68.01 | 69.16 | 69.22 | 71.02 | 71.44 | 71.44 |
| Shanghai → C42 | 7 | 34.45 | 34.45 | 34.61 | 35.59 | 35.59 | 35.59 |
| Sao-Paulo → C32 | 22 | 79.37 | 80.51 | 80.10 | 81.66 | 81.66 | 81.66 |
| NY → C12 | 36 | 42.04 | 43.19 | 42.66 | 43.20 | 43.90 | 43.90 |

## Tier 2 — per-token (forward, token-weighted)

| flow | n | min | p50 | mean | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 3614 | 59.55 | 59.59 | 59.94 | 60.73 | 60.73 | 60.73 |
| Mumbai → C22 | 14746 | 68.01 | 69.15 | 69.00 | 70.30 | 71.29 | 71.44 |
| Shanghai → C42 | 1749 | 34.45 | 34.45 | 34.46 | 34.45 | 35.59 | 35.59 |
| Sao-Paulo → C32 | 6057 | 79.37 | 79.37 | 79.87 | 80.51 | 81.66 | 81.66 |
| NY → C12 | 8848 | 42.04 | 42.06 | 42.43 | 43.20 | 43.90 | 43.90 |

## Tier 3 — per-response-packet (return network delay)

| flow | n | min | p50 | mean | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 7 | 59.55 | 59.57 | 59.57 | 59.59 | 59.59 | 59.59 |
| Mumbai → C22 | 25 | 68.01 | 68.01 | 68.33 | 69.16 | 69.16 | 69.16 |
| Shanghai → C42 | 6 | 34.45 | 34.45 | 34.45 | 34.45 | 34.45 | 34.45 |
| Sao-Paulo → C32 | 10 | 79.37 | 79.37 | 79.37 | 79.37 | 79.37 | 79.37 |
| NY → C12 | 18 | 42.04 | 42.05 | 42.05 | 42.06 | 42.06 | 42.06 |

## Tier 4a — TTFT (request emit → first response packet recv)

| flow | n | min | p50 | mean | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 7 | 161.34 | 179.90 | 177.49 | 191.88 | 191.88 | 191.88 |
| Mumbai → C22 | 18 | 209.25 | 233.58 | 243.12 | 342.47 | 342.47 | 342.47 |
| Shanghai → C42 | 6 | 101.37 | 108.13 | 106.64 | 115.72 | 115.72 | 115.72 |
| Sao-Paulo → C32 | 10 | 218.29 | 229.36 | 230.21 | 252.66 | 252.66 | 252.66 |
| NY → C12 | 18 | 129.20 | 141.53 | 148.87 | 194.10 | 194.10 | 194.10 |

## Tier 4b — T_total (request emit → last response packet recv)

| flow | n | min | p50 | mean | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 7 | 161.34 | 179.90 | 177.49 | 191.88 | 191.88 | 191.88 |
| Mumbai → C22 | 18 | 210.39 | 233.58 | 243.57 | 342.47 | 342.47 | 342.47 |
| Shanghai → C42 | 6 | 101.37 | 108.13 | 106.64 | 115.72 | 115.72 | 115.72 |
| Sao-Paulo → C32 | 10 | 218.29 | 229.36 | 230.21 | 252.66 | 252.66 | 252.66 |
| NY → C12 | 18 | 129.20 | 141.53 | 148.87 | 194.10 | 194.10 | 194.10 |


## Lifecycle stage breakdown (per-flow means, ms)

| flow | reqs | T_forward | D_gather | T_queue | T_compute | T_return | TTFT | T_total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Tokyo → C2 | 7 / 7 | 59.57 | 0.98 | 0.00 | 57.37 | 59.57 | 177.49 | 177.49 |
| Mumbai → C22 | 18 / 18 | 68.17 | 2.03 | 17.84 | 87.07 | 68.01 | 243.12 | 243.57 |
| Shanghai → C42 | 6 / 6 | 34.45 | 0.19 | 0.00 | 37.55 | 34.45 | 106.64 | 106.64 |
| Sao-Paulo → C32 | 10 / 10 | 79.37 | 1.37 | 3.98 | 66.12 | 79.37 | 230.21 | 230.21 |
| NY → C12 | 18 / 18 | 42.09 | 1.14 | 8.52 | 55.07 | 42.05 | 148.87 | 148.87 |

## Interpretation

- **TTFT ≈ T_forward + D_gather + T_queue + T_compute + T_return**
  (within rounding; the gap between TTFT and T_total is the
  serialization of additional response packets — 1.14 ms per
  packet at 10 Mbps GSL).
- **per-packet vs per-token** (forward): when prompt sizes vary,
  the per-token CDF is slightly skewed by which-packets-carry-
  the-most-tokens (last packet of a request often carries fewer).
- **per-response-packet** is roughly equal to forward `T_forward`
  for the same (src, sat) pair: the route is symmetric.
- **T_queue dominates the tail**: when ρ approaches 1, T_total
  inflates by O(queue depth) × O(T_compute).

## Verdict

- compute completion rate : `59 / 59 = 100.0%`
- zero timeouts : `True`

**Verdict: PASS**
