# Phase B — Result

## Totals

- tx_request_count : **46**
- tx_packet_count  : **90**
- rx_packet_count  : **88**  (97.78% delivered)

## Per-request completeness

- requests with all packets received : **45**
- requests missing one or more packets : **0**

## Single-packet end-to-end latency

(`recv_time_ns - t_emit_ns` per packet)

| stat | ns | ms |
|---|---:|---:|
| count | 88 | — |
| min | 86,737,766 | 86.738 ms |
| p50 | 87,881,767 | 87.882 ms |
| mean | 87,391,876 | 87.392 ms |
| p95 | 87,960,476 | 87.960 ms |
| max | 90,247,738 | 90.248 ms |

## Per-request straggle (Phase C gather-wait ground truth)

(`max(recv_time_ns) - min(recv_time_ns)` over packets in same request)

| stat | ns | ms |
|---|---:|---:|
| requests with >=2 pkts | 43 | — |
| min | 1,143,999 | 1.144 ms |
| p50 | 1,144,000 | 1.144 ms |
| mean | 1,144,000 | 1.144 ms |
| p95 | 1,144,000 | 1.144 ms |
| max | 1,144,001 | 1.144 ms |

## Path-bottleneck proxy

(`max per-packet latency − min per-packet latency`; large spread indicates queueing or route change along the path)

- bottleneck range : **3.510 ms**

## Verdict

- delivery >= 95% ? : `97.78%`  -> `True`
- at least one packet received? : `True`

**Verdict: PASS**
