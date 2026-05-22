"""
Pytest fixtures shared by the Phase A test suite.

The phase_a/ modules import satgenpy (which lives at
``hypatia/satgenpy``) and assume Python's working dir does *not* include
their own directory by default. We fix both here so individual tests
can simply ``import augment_fstate`` etc.

Fixtures
--------

- ``reduced_kuiper_state``: path to the small (17 sat + 2 GS) state dir
  produced by ``integration_tests/test_manila_dalian_over_kuiper``.
  Tests that need a real satgenpy state without paying for a fresh
  Starlink-550 generation rely on this.
- ``reduced_kuiper_dyn``: the matching ``dynamic_state_100ms_for_200s/``
  subdir, with the full set of 2000 fstate files.
- ``tmp_dyn_dir``: a tmp_path-backed copy of just ``fstate_0.txt`` and
  ``gsl_if_bandwidth_0.txt`` so tests that mutate fstate don't trample
  the shared integration-test state.
- ``phase_a_run_dir``: path to the Phase A ns-3 run dir
  (``runs/gs0_to_compute_sat/``). Used by regression tests to assert the
  cached flow completed.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

HERE = os.path.abspath(os.path.dirname(__file__))
PHASE_A_DIR = os.path.abspath(os.path.join(HERE, ".."))
HYPATIA_ROOT = os.path.abspath(os.path.join(PHASE_A_DIR, "..", ".."))
SATGENPY_DIR = os.path.join(HYPATIA_ROOT, "satgenpy")

# Put the phase_a/ scripts and satgenpy on sys.path so tests can import
# them directly.
for p in (PHASE_A_DIR, SATGENPY_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(scope="session")
def reduced_kuiper_state() -> str:
    """Path to the integration-test's reduced Kuiper-630 state dir."""
    path = os.path.join(
        HYPATIA_ROOT,
        "integration_tests",
        "test_manila_dalian_over_kuiper",
        "temp",
        "gen_data",
        "reduced_kuiper_630_algorithm_free_one_only_over_isls",
    )
    if not os.path.isdir(path):
        pytest.skip(
            f"reduced Kuiper state not found at {path} -- run the "
            f"integration test (bash hypatia_run_tests.sh) first"
        )
    return path


@pytest.fixture(scope="session")
def reduced_kuiper_dyn(reduced_kuiper_state: str) -> str:
    """Path to dynamic_state_100ms_for_200s under the reduced Kuiper state."""
    path = os.path.join(reduced_kuiper_state, "dynamic_state_100ms_for_200s")
    if not os.path.isdir(path):
        pytest.skip(f"dynamic_state dir missing under {reduced_kuiper_state}")
    return path


@pytest.fixture
def tmp_dyn_dir(tmp_path, reduced_kuiper_dyn: str) -> str:
    """Tmp dir holding fstate_0.txt + gsl_if_bandwidth_0.txt copied from
    the reduced Kuiper state. Tests mutate this freely without affecting
    the shared integration-test artefacts.
    """
    dst = tmp_path / "dyn"
    dst.mkdir()
    for f in ("fstate_0.txt", "gsl_if_bandwidth_0.txt"):
        shutil.copy(os.path.join(reduced_kuiper_dyn, f), dst / f)
    return str(dst)


@pytest.fixture(scope="session")
def phase_a_run_dir() -> str:
    """Path to the cached Phase A run output."""
    return os.path.join(PHASE_A_DIR, "runs", "gs0_to_compute_sat")