# Phase A — Result

## Flow

- flow_id: `0`
- src    : node `1584` (GS-0)
- dst    : node `894` (compute SAT, plane 40)
- start_time_ns: `200000000`  (= 0.200 s)

## Outcome

- completed   : **YES**
- bytes_sent  : 1000000
- duration_ns : 2064150950 (= 2.064 s)
- raw row     : `0,1584,894,1000000,200000000,2264150950,2064150950,1000000,YES,phase_a_gs0_to_compute`

## RTT samples

| stat | value |
|---|---|
| count | 352 |
| min_ms | 147.000 |
| p50_ms | 188.115 |
| mean_ms | 197.478 |
| p95_ms | 257.337 |
| max_ms | 269.938 |

## Path (traced from `fstate_200000000.txt`)

| hop | from | to | kind | length (km) |
|---|---|---|---|---|
| 0 | GS-0 | SAT | GSL | 976.21 |
| 1 | SAT | SAT | ISL | 1961.35 |
| 2 | SAT | SAT | ISL | 1961.17 |
| 3 | SAT | SAT | ISL | 1960.77 |
| 4 | SAT | SAT | ISL | 1960.22 |
| 5 | SAT | SAT | ISL | 1959.53 |
| 6 | SAT | SAT | ISL | 1958.86 |
| 7 | SAT | SAT | ISL | 1958.32 |
| 8 | SAT | SAT | ISL | 1958.00 |
| 9 | SAT | SAT | ISL | 1342.05 |
| 10 | SAT | SAT | ISL | 1958.13 |
| 11 | SAT | SAT | ISL | 1958.56 |

- ISL hops : **11**
- GSL hops : **1**
- total path length: **21913.17 km**

## Geometric propagation lower bound

- one-way (length / c) : **73.094 ms**
- RTT  (2 * one-way)   : **146.189 ms**
- measured min RTT     : 147.000 ms (margin over geometric: +0.811 ms; should be > 0)

## Verdict

- flow completed?         : `True`
- path traversed ISLs?    : `True` (11 hops)
- RTT >= geometric bound? : `True`

**Verdict: PASS**
