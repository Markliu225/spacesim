"""
NHPP (non-homogeneous Poisson process) event generation via thinning.

For a time-varying rate ``λ(t)`` on ``[0, T]`` we:
  1. Estimate ``λ_max`` = max over ``[0, T]`` of ``λ(·)``, with a small
     safety multiplier so we don't undercount.
  2. Generate candidate event times from a homogeneous Poisson process
     with rate ``λ_max`` (i.e. exponential inter-arrivals with mean
     ``1 / λ_max``).
  3. For each candidate ``t*``, keep it with probability
     ``λ(t*) / λ_max``.

The kept times are an exact realisation of the NHPP with rate ``λ(·)``.
Thinning is preferred over time-rescaling here because our rate
function is cheap to evaluate (an interpolated hourly shape) and the
acceptance ratio is high (≥ 1/24 trough-to-peak on the Azure trace).
"""

from __future__ import annotations

from typing import Callable, List

import numpy as np


def estimate_lambda_max(
    rate_func: Callable[[float], float],
    duration_sec: float,
    *,
    n_probe: int = 1000,
    safety: float = 1.1,
) -> float:
    """Probe ``rate_func`` at ``n_probe`` evenly-spaced points and return
    ``safety * max(probes)`` as the homogeneous-Poisson dominant rate.

    A few hundred probes are enough for a smooth diurnal shape (24
    interpolation segments over 86 400 s). The safety multiplier guards
    against the probe grid missing a sharper peak between two samples.
    """
    if duration_sec <= 0:
        return 0.0
    ts = np.linspace(0.0, duration_sec, n_probe)
    probes = np.array([rate_func(float(t)) for t in ts])
    if probes.size == 0:
        return 0.0
    peak = float(probes.max())
    if peak <= 0:
        return 0.0
    return safety * peak


def generate_nhpp_events(
    duration_sec: float,
    rate_func: Callable[[float], float],
    rng: np.random.Generator,
    *,
    n_probe: int = 1000,
    safety: float = 1.1,
) -> List[float]:
    """Return a sorted list of event times in ``[0, duration_sec]`` whose
    inter-arrival distribution matches a NHPP with rate ``rate_func``.

    Empty list if ``λ_max`` rounds to zero (i.e. the rate is effectively
    zero everywhere).
    """
    lambda_max = estimate_lambda_max(
        rate_func, duration_sec, n_probe=n_probe, safety=safety
    )
    if lambda_max <= 0.0:
        return []

    out: List[float] = []
    t = 0.0
    # Exponential inter-arrivals at rate lambda_max.
    while True:
        # rng.exponential takes mean = 1/lambda, returns positive float.
        gap = rng.exponential(scale=1.0 / lambda_max)
        t = t + float(gap)
        if t >= duration_sec:
            break
        # Thin: accept with probability rate(t) / lambda_max.
        prob = rate_func(t) / lambda_max
        # Clamp for safety — if the probe grid missed a peak, prob could
        # exceed 1.0; clamp so we always accept rather than reject
        # silently (we lose some independence but never underrepresent).
        if prob >= 1.0 or rng.random() < prob:
            out.append(t)
    return out
