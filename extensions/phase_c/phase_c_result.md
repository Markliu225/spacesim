# Phase C — full LLM request lifecycle result

End-to-end: GS LLMRequestApplication → fstate-routed UDP →
compute-SAT GatherApplication → ComputeApplication FIFO →
UDP response burst → GS LLMResponseSinkApplication.

## Totals

- tx_request_count = **104**
- tx_request_packets = **198**
- rx_request_packets = **198**
- gather_complete_count = **104**
- gather_timeout_count = **0**
- compute_complete_count = **104**
- response_recv_packets = **105**

### Completion: 104 requests have all of (gather, compute, response) — i.e. completed the full lifecycle.
Completion rate: **100.00%**

## Per-stage latency  (ms)

| stage | n | min | p50 | mean | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| T_forward (emit → gather first) | 104 | 86.63 | 86.72 | 86.75 | 86.81 | 87.63 | 88.40 |
| D_gather (gather first → last) | 104 | 0.00 | 1.14 | 1.03 | 1.14 | 1.14 | 1.14 |
| T_queue_wait (queue enter → svc) | 104 | 0.00 | 25.69 | 66.57 | 261.09 | 348.08 | 348.22 |
| T_compute (compute service) | 104 | 42.45 | 68.25 | 67.81 | 84.85 | 87.70 | 95.10 |
| T_return (resp emit → resp recv) | 104 | 86.63 | 86.71 | 86.72 | 86.80 | 86.81 | 86.81 |
| T_total (emit → first resp recv) | 104 | 219.12 | 266.41 | 308.89 | 498.45 | 585.15 | 599.99 |
| T_total_full (emit → last resp recv) | 104 | 219.12 | 266.41 | 308.90 | 498.45 | 585.15 | 599.99 |

## Compute queue depth (snapshot at each enqueue)

- mean = **1.36**
- p95  = 4
- max  = **6**
- (n_enqueues = 104)

## Analytic estimate vs simulator

- L_in_mean = 500.0,  L_out_mean = 200.0
- analytic T_compute = α·L_in + β·L_out + γ = 100us·500 + 50us·200 + 10.0ms = **70.00 ms**
- simulator T_compute mean = **67.81 ms** (driven by *actual* sampled L_in / L_out per request)
- mean T_forward = 86.75 ms,  mean T_return = 86.72 ms  (should be ≈, same path forward + back)
- analytic T_total ≈ T_forward + T_compute + T_return ≈ **156.73 ms**
- simulator T_total mean (first resp recv) = **308.89 ms**  (gap = queueing wait)

## Stuck (timed-out) requests

- stuck count: **0**

## Verdict

- every tx request completed full lifecycle? : `104 == 104 → True`
- zero timeouts? : `True`

**Verdict: PASS**
