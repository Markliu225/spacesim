"""
Pytest fixtures for the ``spacesim`` test suite.

Sets up ``sys.path`` so tests can import the package as
``spacesim.*`` and provides the small set of shared fixtures: a real
satgenpy state from the upstream Manila→Dalian integration test, a
mutable copy for fstate-mutation tests, and a cached run dir for
regression assertions.

The fixtures are skip-not-fail: if the upstream integration test
hasn't been run, the dependent tests skip instead of failing — the
unit tests stay green on a fresh clone.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

# --- sys.path setup -------------------------------------------------------

HERE = os.path.abspath(os.path.dirname(__file__))
SPACESIM_DIR = os.path.abspath(os.path.join(HERE, ".."))            # extensions/spacesim/
EXTENSIONS_DIR = os.path.abspath(os.path.join(SPACESIM_DIR, ".."))   # extensions/
HYPATIA_ROOT = os.path.abspath(os.path.join(EXTENSIONS_DIR, ".."))   # hypatia/
SATGENPY_DIR = os.path.join(HYPATIA_ROOT, "satgenpy")

# Prepend ``extensions/`` so ``import spacesim.topology.roles`` works,
# and satgenpy so the topology helpers can import satgen.* directly.
for p in (EXTENSIONS_DIR, SATGENPY_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


# --- Shared fixtures ------------------------------------------------------


@pytest.fixture(scope="session")
def reduced_kuiper_state() -> str:
    """Path to the upstream integration test's reduced Kuiper-630 state dir.

    The state has 17 satellites + 2 ground stations (Manila, Dalian) —
    small enough for fast SGP-4 calls in tests, large enough to exercise
    real ISL graph + GSL lookup paths.
    """
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
            f"reduced Kuiper state not found at {path} -- "
            f"run `bash hypatia_run_tests.sh` first"
        )
    return path


@pytest.fixture(scope="session")
def reduced_kuiper_dyn(reduced_kuiper_state: str) -> str:
    """Path to ``dynamic_state_100ms_for_200s/`` under the reduced Kuiper state."""
    path = os.path.join(reduced_kuiper_state, "dynamic_state_100ms_for_200s")
    if not os.path.isdir(path):
        pytest.skip(f"dynamic_state dir missing under {reduced_kuiper_state}")
    return path


@pytest.fixture
def tmp_dyn_dir(tmp_path, reduced_kuiper_dyn: str) -> str:
    """Tmp dir with fstate_0 + gsl_if_bandwidth_0 copied from the reduced state.

    Use this whenever a test mutates fstate so the shared integration-test
    artefacts stay untouched.
    """
    dst = tmp_path / "dyn"
    dst.mkdir()
    for f in ("fstate_0.txt", "gsl_if_bandwidth_0.txt"):
        shutil.copy(os.path.join(reduced_kuiper_dyn, f), dst / f)
    return str(dst)


@pytest.fixture(scope="session")
def phase_a_run_dir() -> str:
    """Path to the cached Tokyo→SAT-894 run output.

    Used by the regression tests to assert that the canonical "the
    runtime pipeline still produces a valid lifecycle" run is
    untouched. The fixture name is historical (originated as a Phase A
    artefact); the underlying directory now lives under
    ``spacesim/runs/tokyo_to_sat894``.
    """
    return os.path.join(SPACESIM_DIR, "runs", "tokyo_to_sat894")
