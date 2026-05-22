"""
AzureTraceFitter — learn a diurnal LLM-request shape from an
Azure LLM Inference trace CSV.

What we extract
---------------

- A 24-element rate-shape array ``d[τ]`` indexed by hour-of-day,
  normalised so ``max(d) == 1``. ``rate_shape(τ)`` linearly interpolates
  between hour bins; given a peak request rate ``λ_peak`` (req/s), the
  per-GS rate at local time ``τ`` is ``λ_peak · d(τ)``.

- A per-bucket sample bank of ``(L_in, L_out)`` token-count pairs
  drawn from the trace, indexed by the same hour-of-day. To sample a
  request's prompt/response sizes for a given local time, we pick one
  pair uniformly at random from ``buckets[floor(τ)]``.

  We use empirical sampling rather than kernel-density smoothing because
  the trace's tail is heavy (8k+ token prompts exist but only in a
  handful of samples per bucket); KDE would smear them across implausibly
  small/large values.

The fitter reads the CSV once and keeps everything in memory as numpy
arrays. For Azure's full 1-week trace (~12M rows) that's a couple of
hundred MB; if memory is a concern, ``--max-rows`` caps the read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd


# Field names the fitter expects in the input CSV. We don't accept
# alternate spellings — the spec pins this contract.
COL_TIMESTAMP = "TIMESTAMP"
COL_CONTEXT_TOKENS = "ContextTokens"
COL_GENERATED_TOKENS = "GeneratedTokens"


@dataclass
class FitStats:
    """Diagnostic numbers a fit emits for the generator report."""
    n_rows: int
    n_used: int
    n_dropped_invalid: int
    timestamp_min: pd.Timestamp
    timestamp_max: pd.Timestamp
    span_hours: float
    d_raw: np.ndarray            # shape (24,), raw counts
    d: np.ndarray                # shape (24,), normalised in [0, 1]
    L_in_overall_mean: float
    L_out_overall_mean: float
    per_bucket_size: np.ndarray  # shape (24,), how many samples each bucket has


class AzureTraceFitter:
    """Read an Azure trace CSV, build the diurnal rate shape, and prepare
    per-bucket length samples.

    Use ``fit()`` once after construction. Then call ``rate_shape(τ)`` and
    ``sample_length(τ, rng)`` as many times as needed.
    """

    def __init__(self, trace_csv_path: Path, *, max_rows: Optional[int] = None):
        self.path = Path(trace_csv_path)
        if not self.path.exists():
            raise FileNotFoundError(f"trace CSV not found: {self.path}")
        self.max_rows = max_rows

        # Filled by fit()
        self._d_raw: np.ndarray = np.zeros(24)         # raw counts per hour
        self._d: np.ndarray = np.zeros(24)             # normalised in [0, 1]
        # _buckets[hour] is an (n_samples_in_bucket, 2) int array of
        # (L_in, L_out) pairs.
        self._buckets: list[np.ndarray] = [np.empty((0, 2), dtype=np.int64) for _ in range(24)]
        self._fitted = False
        self.stats: Optional[FitStats] = None

    # ---- fitting -----------------------------------------------------

    def fit(self) -> FitStats:
        """Parse the CSV, drop invalid rows, populate d[] and buckets[]."""
        usecols = [COL_TIMESTAMP, COL_CONTEXT_TOKENS, COL_GENERATED_TOKENS]
        df = pd.read_csv(self.path, usecols=usecols, nrows=self.max_rows)

        n_rows = len(df)

        # Timestamp parsing. Azure's trace uses ISO strings; some other
        # exports might be epoch seconds. Try both.
        try:
            df[COL_TIMESTAMP] = pd.to_datetime(df[COL_TIMESTAMP], utc=True,
                                               errors="coerce")
        except Exception:
            df[COL_TIMESTAMP] = pd.to_datetime(df[COL_TIMESTAMP], unit="s",
                                               utc=True, errors="coerce")

        before = len(df)
        df = df.dropna(subset=[COL_TIMESTAMP])
        # Drop rows with invalid (NaN / <= 0) token counts.
        df = df[(df[COL_CONTEXT_TOKENS] > 0) & (df[COL_GENERATED_TOKENS] > 0)]
        df[COL_CONTEXT_TOKENS] = df[COL_CONTEXT_TOKENS].astype(np.int64)
        df[COL_GENERATED_TOKENS] = df[COL_GENERATED_TOKENS].astype(np.int64)
        n_used = len(df)
        n_dropped = before - n_used

        if n_used == 0:
            raise ValueError(
                f"After dropping invalid rows, {self.path} has no usable "
                f"data. Check the column names and timestamp format."
            )

        ts = df[COL_TIMESTAMP]
        ts_min = ts.min()
        ts_max = ts.max()
        span_h = (ts_max - ts_min).total_seconds() / 3600.0

        hour_of_day = ts.dt.hour.to_numpy()
        L_in = df[COL_CONTEXT_TOKENS].to_numpy()
        L_out = df[COL_GENERATED_TOKENS].to_numpy()

        # Count per hour.
        d_raw = np.bincount(hour_of_day, minlength=24).astype(np.float64)
        d_max = d_raw.max() if d_raw.max() > 0 else 1.0
        d = d_raw / d_max

        # Per-bucket (L_in, L_out) samples. Indexing once is much faster
        # than a Python loop.
        bucket_sizes = np.zeros(24, dtype=np.int64)
        buckets: list[np.ndarray] = []
        for h in range(24):
            mask = hour_of_day == h
            samples = np.column_stack([L_in[mask], L_out[mask]])
            buckets.append(samples)
            bucket_sizes[h] = samples.shape[0]

        self._d_raw = d_raw
        self._d = d
        self._buckets = buckets
        self._fitted = True
        self.stats = FitStats(
            n_rows=n_rows,
            n_used=n_used,
            n_dropped_invalid=n_dropped,
            timestamp_min=ts_min,
            timestamp_max=ts_max,
            span_hours=float(span_h),
            d_raw=d_raw,
            d=d,
            L_in_overall_mean=float(L_in.mean()),
            L_out_overall_mean=float(L_out.mean()),
            per_bucket_size=bucket_sizes,
        )
        return self.stats

    # ---- query API ---------------------------------------------------

    def rate_shape(self, tau: float) -> float:
        """Normalised rate at local time ``tau`` (hours, in [0, 24)).

        Linear interpolation between hour bins, wrapping around 24→0.
        """
        if not self._fitted:
            raise RuntimeError("fit() must be called before rate_shape()")
        t = float(tau) % 24.0
        lo = int(np.floor(t))
        hi = (lo + 1) % 24
        frac = t - lo
        return (1.0 - frac) * self._d[lo] + frac * self._d[hi]

    def sample_length(self, tau: float, rng: np.random.Generator) -> Tuple[int, int]:
        """Sample one (L_in, L_out) pair from the bucket for hour ``floor(tau)``.

        If a bucket is empty (rare but possible at thin hours), fall back to
        the nearest non-empty bucket.
        """
        if not self._fitted:
            raise RuntimeError("fit() must be called before sample_length()")
        h = int(np.floor(float(tau) % 24.0))
        bucket = self._buckets[h]
        if bucket.shape[0] == 0:
            # Walk outward for the nearest non-empty bucket.
            for d in range(1, 13):
                for cand in (h - d, h + d):
                    cand = cand % 24
                    if self._buckets[cand].shape[0] > 0:
                        bucket = self._buckets[cand]
                        break
                if bucket.shape[0] > 0:
                    break
            if bucket.shape[0] == 0:
                # All 24 buckets empty — only possible if fit() rejected
                # everything, in which case fit() would have already raised.
                raise RuntimeError("all buckets are empty (no usable trace data)")
        idx = int(rng.integers(0, bucket.shape[0]))
        L_in, L_out = bucket[idx]
        return int(L_in), int(L_out)

    @property
    def d(self) -> np.ndarray:
        """Return a copy of the normalised 24-hour rate shape."""
        if not self._fitted:
            raise RuntimeError("fit() must be called before .d")
        return self._d.copy()


# ---- synthetic-trace helper ----------------------------------------------


def make_synthetic_azure_trace(
    output_path: Path,
    *,
    n_rows: int = 200_000,
    days: int = 1,
    peak_hour_utc: float = 14.0,
    rng: Optional[np.random.Generator] = None,
) -> None:
    """Generate a small synthetic Azure-format CSV for sanity-testing.

    Real Azure data is multi-GB and not shipped here. This helper writes
    a CSV with the same column names and an approximately-realistic
    diurnal pattern (Gaussian bump centred at ``peak_hour_utc``), so the
    fitter has something to chew on for `--sanity-check` runs.

    Token distributions are log-normal — coarse but enough to verify the
    fitter doesn't lose the shape. The synthetic trace is clearly
    labelled so it's never mistaken for the real thing.
    """
    if rng is None:
        rng = np.random.default_rng(0)

    # Diurnal sampling: weights at each hour follow a Gaussian centred
    # at peak_hour_utc, sigma=4 hours, plus a 0.1 baseline so the trough
    # isn't bone-dry.
    hours = np.arange(24)
    weights = 0.1 + np.exp(-((hours - peak_hour_utc) ** 2) / (2 * 4.0 ** 2))
    weights /= weights.sum()

    # Sample hour-of-day for each row, then a random sub-hour offset.
    chosen_hour = rng.choice(24, size=n_rows, p=weights)
    sub_h = rng.uniform(0, 1, size=n_rows)
    chosen_day = rng.integers(0, days, size=n_rows)
    # Build UTC timestamps starting from a fixed epoch.
    epoch = pd.Timestamp("2024-01-01T00:00:00Z")
    seconds = (chosen_day * 86400 + chosen_hour * 3600 + sub_h * 3600).astype(np.int64)
    ts = epoch + pd.to_timedelta(seconds, unit="s")

    # Token counts: log-normal so we have a heavy tail.
    L_in = rng.lognormal(mean=6.0, sigma=0.8, size=n_rows).astype(np.int64) + 1
    L_out = rng.lognormal(mean=4.5, sigma=0.7, size=n_rows).astype(np.int64) + 1

    df = pd.DataFrame({
        COL_TIMESTAMP: ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        COL_CONTEXT_TOKENS: L_in,
        COL_GENERATED_TOKENS: L_out,
    })
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
