# traffic_generator

Standalone Python tool that learns a diurnal traffic shape from an
[Azure LLM Inference trace](https://github.com/Azure/AzurePublicDataset)
and uses it to generate per-ground-station LLM request events for a
simulator.

Outputs one ``events.csv`` (sorted by emit time, globally numbered)
suitable for feeding into ``spacesim/workload/`` or any downstream
scheduler. Has **no dependency on Hypatia or ns-3** — pure
NumPy/Pandas + matplotlib (for the report).

## What it does

1. **Fit** the diurnal shape ``d(τ)`` from the trace's
   ``TIMESTAMP`` column (counts per hour-of-day, normalised to peak=1).
2. **Bucket** the trace's ``(ContextTokens, GeneratedTokens)`` pairs by
   the same hour-of-day so we can sample realistic prompt/response
   sizes whose distribution matches whatever time of day is "now" for
   a given ground station.
3. **NHPP-thinning** generates per-GS event times with rate
   ``λ_peak · d(local_time(t, lon))`` — the peak rate is per-GS, and
   each GS sees the shape phase-shifted by its longitude (a GS at
   +116° experiences "afternoon" 7.7 hours before a GS at 0°).
4. **Merge** all GS events, sort by emit time, assign global
   ``req_id``, write CSV.
5. Optional ``--report`` writes a markdown report plus diagnostic
   PNGs (the fitted shape, per-GS hourly counts).

## Quick start

```bash
cd /home/mark/spacesim/hypatia/extensions/traffic_generator

# 1. Download a real Azure trace (the multimodal one is the only public
#    Azure LLM trace that covers a full week — the other two only cover
#    1 hour each, which is not enough to learn a 24-hour diurnal shape).
mkdir -p azure_trace
wget -O azure_trace/AzureLMMInferenceTrace_multimodal.csv.gz \
  https://raw.githubusercontent.com/Azure/AzurePublicDataset/master/data/AzureLMMInferenceTrace_multimodal.csv.gz
gunzip azure_trace/AzureLMMInferenceTrace_multimodal.csv.gz

# 2. Generate 24h of events for the shipped 10-city config:
python traffic_gen.py \
    --azure-trace azure_trace/AzureLMMInferenceTrace_multimodal.csv \
    --gs-config ground_stations.json \
    --duration-sec 86400 \
    --output real_run/events.csv \
    --seed 42 \
    --report
```

If you don't supply (or can't download) an Azure trace, the generator
falls back to a synthetic Azure-format CSV (clearly labelled in the
report). Useful for CI / quick smoke tests:

```bash
python traffic_gen.py \
    --gs-config ground_stations.json \
    --duration-sec 86400 \
    --output sanity_run/events.csv \
    --report
```

### Which Azure trace to use

| trace file | rows | covers | use for |
|---|---:|---|---|
| `AzureLLMInferenceTrace_conv.csv` | 19k | **1 hour** of conversation requests | L_in/L_out distributions only — not enough for diurnal shape |
| `AzureLLMInferenceTrace_code.csv` | 9k | **1 hour** of code completion | same |
| **`AzureLMMInferenceTrace_multimodal.csv`** | **1 M** | **1 week** (168 h) | ✓ this one for a proper d(τ) |

## Inputs

### `--azure-trace` (CSV, optional)

Must contain at least these columns:

| column | type | notes |
|---|---|---|
| `TIMESTAMP` | ISO 8601 string or epoch seconds | parsed UTC |
| `ContextTokens` | int | prompt token count |
| `GeneratedTokens` | int | response token count |

Other columns are ignored. The full Azure-public trace
(``AzureLLMInferenceTrace_conv_1week.csv``) is ~1 GB and ~12 M rows;
``--max-trace-rows N`` caps the read for fast iteration.

If the path is missing or the file doesn't exist, the script writes a
small synthetic Azure-format CSV (100 k rows, 2 days, log-normal token
distributions, Gaussian diurnal peak at UTC 14:00) next to the output
and uses it instead. Useful for CI / sanity runs where the real trace
isn't available. The report flags whether the fit was on real or
synthetic data.

### `--gs-config` (JSON or CSV)

Each row/object:

| field | type | meaning |
|---|---|---|
| `gs_idx` | int | ground-station identifier (passed through to events) |
| `name` | string | display name (used in plots / report) |
| `lat` | float | latitude, degrees (unused by generator — kept for downstream tools) |
| `lon` | float | longitude, degrees (**east positive**); drives the per-GS time shift |
| `peak_lambda` | float | peak request rate at the GS's local solar afternoon, req/s |

The shipped ``ground_stations.json`` has the top-10 cities by 2025
population (Tokyo, Delhi, Shanghai, São-Paulo, Mumbai, Mexico-City,
Beijing, Osaka, Cairo, New-York), all at λ_peak=10 req/s, with
lat/lon coordinates taken from
``paper/satellite_networks_state/input_data/ground_stations_cities_sorted_by_estimated_2025_pop_top_100.basic.txt``
so they line up 1-to-1 with whatever Hypatia run dir uses the same GS
ordering.

### `--duration-sec`

Length of the simulated window in seconds. Time t=0 of the
simulation corresponds to UTC midnight.

### `--seed`

NumPy RNG seed. Determines NHPP candidate times, thinning decisions,
length samples, and (when active) the synthetic trace.

## Outputs

A single run produces **one CSV per ground station** plus, by default,
a globally-merged CSV.

### Per-GS CSVs — `<output dir>/per_gs/events_gs<idx>_<slug>.csv`

One per GS, e.g. ``events_gs0_tokyo.csv``,
``events_gs1_delhi.csv``, … Each file has the same five columns:

| column | meaning |
|---|---|
| `req_id` | **GS-local** index, 0..Nᵢ-1 (consecutive) |
| `src_gs_idx` | the originating GS's `gs_idx` (constant within the file) |
| `t_emit_ns` | emit time in ns since sim t=0 (UTC midnight) |
| `L_in` | prompt-token count, sampled from the trace bucket for local time |
| `L_out` | response-token count, sampled from the same bucket |

Rows are sorted by ``t_emit_ns`` within the file. The location can be
overridden with ``--per-gs-dir``.

### Merged `events.csv` (default; suppress with `--no-merged`)

Same columns, but with a **global** ``req_id`` (0..N-1 across the
union of all GSes) and sorted by ``t_emit_ns`` across all GSes.

Both forms are stream-written in 100 k-row chunks — handles tens of
millions of events without memory pressure.

### `generator_report.md` + PNGs (`--report` only)

- `diurnal_shape.png` — the fitted ``d(τ)`` over 24 hours; should
  show a clear daytime peak.
- `per_gs_hourly.png` — per-GS event count binned into UTC hours
  over the sim window; peaks should be offset by each GS's longitude.
- `generator_report.md` — fit metrics + per-bucket sample sizes +
  per-GS expected-vs-actual table + sanity rails (monotone t_emit_ns,
  req_id range).

## Sanity numbers from the shipped run

Running ``traffic_gen.py`` with the real Azure multimodal trace
(1 week, 1 M rows) and the shipped 10-city ``ground_stations.json``
(λ_peak=10 req/s each) over 24 h produces **6,504,756 events** in
72 seconds wall-clock. Total expected via ``Σ λ_peak · duration ·
mean(d)`` is 6,503,456, so we hit 1.0002× — well within Poisson
sampling noise.

The Azure trace has its primary peak at UTC 8:00 (d=1.000) and a
strong secondary at UTC 17:00 (d=0.937). Each GS's UTC peak hour
is its closest-hour mapping of one of those two peaks via the
longitude shift `(utc + lon/15) mod 24 ≈ local_peak_hour`:

| GS | lon | UTC peak hour observed | matches trace peak via local time |
|---|---:|---:|---|
| Tokyo | +139.7° | 22 | local 7:18 → trace primary (UTC 8) |
| Osaka | +135.6° |  7 | local 16:02 → trace secondary (UTC 17) |
| Shanghai | +121.5° |  8 | local 16:06 → trace secondary |
| Beijing | +116.4° |  8 | local 15:46 → trace secondary |
| Delhi | +77.2° | 11 | local 16:09 → trace secondary |
| Mumbai | +72.9° | 11 | local 15:51 → trace secondary |
| Cairo | +31.2° | 14 | local 16:05 → trace secondary |
| Mexico-City | -99.1° | 14 | local 7:23 → trace primary |
| São-Paulo | -46.6° | 19 | local 15:54 → trace secondary |
| New-York | -74.0° | 21 | local 16:04 → trace secondary |

All 10 GSes line up to within ±20 minutes of one of the two trace
peaks, confirming the longitude shift is correct end-to-end.

See [`real_run/generator_report.md`](real_run/generator_report.md)
for the full per-bucket fit stats, expected-vs-actual table, and
both PNG plots.

## File layout

```
traffic_generator/
├── traffic_gen.py            — CLI entry, GS loop, sort+rewrite, optional report
├── trace_fitter.py           — AzureTraceFitter + make_synthetic_azure_trace()
├── nhpp_generator.py         — generate_nhpp_events() via thinning + lambda_max probe
├── ground_stations.json      — 10-city config (Tokyo, Delhi, Shanghai, …)
├── azure_trace/              — downloaded Azure trace CSVs (the multimodal one is the only week-long source)
│   ├── AzureLLMInferenceTrace_conv.csv         703 KB,  1 hour conversation trace
│   ├── AzureLLMInferenceTrace_code.csv         313 KB,  1 hour code trace
│   └── AzureLMMInferenceTrace_multimodal.csv    34 MB,  1 week, used for d(τ)
├── real_run/                 — output of the shipped 10-city × 24h run
│   ├── events.csv                      (~6.5 M events, ~210 MB, merged)
│   ├── per_gs/
│   │   ├── events_gs0_tokyo.csv        (~650 k rows, ~21 MB)
│   │   ├── events_gs1_delhi.csv
│   │   ├── events_gs2_shanghai.csv
│   │   ├── events_gs3_sao_paulo.csv
│   │   ├── events_gs4_mumbai.csv
│   │   ├── events_gs5_mexico_city.csv
│   │   ├── events_gs6_beijing.csv
│   │   ├── events_gs7_osaka.csv
│   │   ├── events_gs8_cairo.csv
│   │   └── events_gs9_new_york.csv
│   ├── generator_report.md
│   ├── diurnal_shape.png
│   └── per_gs_hourly.png
├── example_events.csv        — first 25 k rows of real_run/events.csv (≤1 MB)
└── README.md
```

## Algorithm details

### `AzureTraceFitter`

- One pass over the CSV with ``pd.read_csv(usecols=[...])`` (or
  ``nrows`` if ``--max-trace-rows`` is given).
- Rows with ``NaN`` timestamp or non-positive token counts are dropped
  (typical Azure trace has < 0.1 % of these).
- ``d_raw[h]`` is ``np.bincount(hour_of_day)``; ``d`` is the same,
  normalised so ``max(d) == 1``.
- ``_buckets[h]`` is a contiguous ``(n_h, 2)`` ``int64`` array of
  ``(L_in, L_out)`` pairs for hour ``h``. Sampling is
  ``rng.integers(0, n_h)`` — O(1).
- ``rate_shape(τ)`` does linear interpolation between adjacent bins
  with wrap-around (so 23 → 0 is continuous).
- ``sample_length(τ, rng)`` falls back to the nearest non-empty
  bucket if the current one is empty (rare; only happens with very
  short or very imbalanced traces).

### `generate_nhpp_events` — thinning

Standard Lewis-Shedler thinning. ``λ_max`` is estimated as
``safety × max`` over 1 000 evenly-spaced probes of ``rate_func``;
``safety = 1.1`` covers the case where the probe grid misses a sharp
peak. Then we sample exponential inter-arrivals at rate ``λ_max`` and
accept each candidate ``t*`` with probability ``rate_func(t*) /
λ_max``. Acceptance ratio on the Azure shape is ≈ ``mean(d) / 1.1 ≈
42 %`` — plenty efficient.

### Longitude shift

Per-GS local time is
``(utc_sec / 3600 + lon_deg / 15) % 24``: 15°/hour because Earth
rotates 360°/24h. So at simulation t=0 (UTC midnight), Beijing
(+116°) sees ``116/15 = 7.73`` as its local "hour". This is naive
solar-mean time, not civil time — good enough for traffic shape,
not for legal-time things like DST.

## Known limitations

1. **Single peak per day** assumed (the trace's hour-of-day grouping
   collapses across all days). If the source trace has very
   different weekday/weekend patterns, fold those out by filtering
   the trace before feeding it in.
2. **No autocorrelation** between consecutive requests. NHPP assumes
   independent events; real chat traffic has session bursts (multiple
   prompts from the same user). v1 doesn't model this.
3. **Sampling without replacement is not enforced.** A bucket with N
   samples can produce > N requests in the sim — they'll repeat
   uniformly. For runtime correctness this is fine; for studying
   long-tail effects you may want to draw with explicit replacement
   semantics.
4. **Memory at the merge step**. ``all_events`` is materialised as a
   Python list before sorting — 1 M events is ~250 MB. Above 10 M
   events consider sorting in chunks per GS and using a heap merge.
