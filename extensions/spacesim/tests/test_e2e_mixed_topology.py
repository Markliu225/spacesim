"""End-to-end smoke test for the mixed compute/transit topology scenario.

Marked ``slow`` because it runs ns-3 (~30 s) and depends on the
scenario being pre-built (state-gen ~1 s + augment ~1 s on the tiny
constellation, but we don't rebuild here -- we just verify the cached
run).

Run with::

    ./run_tests.sh -m slow
    # or
    pytest -m slow tests/test_e2e_mixed_topology.py
"""

from __future__ import annotations

import os
import subprocess

import pytest

HERE = os.path.abspath(os.path.dirname(__file__))
SPACESIM = os.path.abspath(os.path.join(HERE, ".."))
SCENARIO = os.path.join(SPACESIM, "scenarios", "mixed_topology")
RUN_LOGS = os.path.join(SCENARIO, "run", "logs_ns3")
NETWORK_NAME = "tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls"


pytestmark = pytest.mark.slow


# ---- Helpers ---------------------------------------------------------------


def _read_flows():
    path = os.path.join(RUN_LOGS, "tcp_flows.csv")
    if not os.path.exists(path):
        pytest.skip(f"no cached scenario run at {path} -- run bash run.sh first")
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) >= 9:
                rows.append(parts)
    return rows


# ---- Tests ----------------------------------------------------------------


def test_scenario_dirs_exist():
    """Scenario must be set up: state, roles, manifest, schedule, config."""
    assert os.path.isdir(SCENARIO), f"missing scenario dir {SCENARIO}"
    must_exist = [
        os.path.join(SCENARIO, "satellite_roles.txt"),
        os.path.join(SCENARIO, "schedule.csv"),
        os.path.join(SCENARIO, "config_ns3.properties"),
        os.path.join(SCENARIO, "gen_data", NETWORK_NAME, "tles.txt"),
        os.path.join(SCENARIO, "gen_data", NETWORK_NAME, "isls.txt"),
        os.path.join(SCENARIO, "gen_data", NETWORK_NAME, "ground_stations.txt"),
        os.path.join(SCENARIO, "gen_data", NETWORK_NAME, "dynamic_state_100ms_for_5s",
                     ".phase_a_augment.json"),
    ]
    missing = [p for p in must_exist if not os.path.exists(p)]
    if missing:
        pytest.skip(f"scenario not fully built; missing: {missing}")


def test_all_five_flows_completed():
    flows = _read_flows()
    assert len(flows) == 5, f"expected 5 flows, got {len(flows)}: {flows}"
    for row in flows:
        flow_id, src, dst, size, start, end, dur, sent, completed = row[:9]
        assert completed == "YES", (
            f"flow {flow_id} did not complete: completed={completed!r}, "
            f"sent={sent}, size={size}"
        )
        assert int(sent) == int(size), (
            f"flow {flow_id} sent {sent} bytes but should be {size}"
        )


def test_pattern_coverage():
    """The scenario must exercise GS->SAT, SAT->GS, and GS->GS patterns."""
    num_sats = 60
    flows = _read_flows()
    gs_to_sat = sum(1 for r in flows
                    if int(r[1]) >= num_sats and int(r[2]) < num_sats)
    sat_to_gs = sum(1 for r in flows
                    if int(r[1]) < num_sats and int(r[2]) >= num_sats)
    gs_to_gs = sum(1 for r in flows
                   if int(r[1]) >= num_sats and int(r[2]) >= num_sats)
    assert gs_to_sat >= 1, "no GS->SAT flow -- Phase A primary case not exercised"
    assert sat_to_gs >= 1, "no SAT->GS flow -- response-direction case not exercised"
    assert gs_to_gs >= 1, "no GS->GS flow -- baseline regression case not exercised"


def test_rtt_csvs_present_for_each_flow():
    for flow_id in range(5):
        path = os.path.join(RUN_LOGS, f"tcp_flow_{flow_id}_rtt.csv")
        assert os.path.exists(path), f"missing {path}"
        with open(path) as f:
            non_empty_lines = [l for l in f.read().splitlines() if l.strip()]
        assert non_empty_lines, f"{path} has no RTT samples"


def test_verify_script_passes():
    """Run the scenario's own verify.py as the canonical truth."""
    proc = subprocess.run(
        ["/home/mark/spacesim/venv/bin/python",
         os.path.join(SCENARIO, "verify.py")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"verify.py exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )


def test_run_dir_has_satellite_roles_symlink():
    """The C++ patch reads <run_dir>/satellite_roles.txt; without it the
    SAT-as-endpoint check would reject our SAT->GS flow."""
    roles_link = os.path.join(SCENARIO, "run", "satellite_roles.txt")
    if not os.path.exists(roles_link):
        pytest.skip("scenario run dir not materialised; run.sh must have created it")
    # Either it's a symlink to the scenario's roles file, or a copy --
    # both fine as long as the file is present and parseable.
    with open(roles_link) as f:
        compute_lines = [l for l in f.read().splitlines() if l.strip().endswith(",C")]
    assert len(compute_lines) == 6, (
        f"expected 6 compute sats in run dir's satellite_roles.txt, "
        f"got {len(compute_lines)}"
    )