"""Regression test against the cached Phase A run.

These assertions hold for the run produced by ``bash run_phase_a_experiment.sh``
that we keep checked-in under ``runs/gs0_to_compute_sat/``. They catch:

  - Anyone accidentally deleting / corrupting the artefacts.
  - Future changes to augment_fstate / topology-satellite-network that
    cause the flow to behave differently.

If the artefacts are absent (e.g. fresh clone), the tests are skipped
rather than failed -- they're regressions, not preconditions.
"""

from __future__ import annotations

import csv
import os

import pytest


def _flow_row(run_dir: str):
    path = os.path.join(run_dir, "logs_ns3", "tcp_flows.csv")
    if not os.path.exists(path):
        pytest.skip(f"no cached run output at {path}")
    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if row and not row[0].startswith("#"):
                return row
    pytest.skip("tcp_flows.csv has no flow rows")


def test_cached_flow_completed(phase_a_run_dir):
    row = _flow_row(phase_a_run_dir)
    # row = flow_id,from,to,size_byte,start_ns,end_ns,duration_ns,bytes_sent,completed,metadata
    assert row[8] == "YES", f"flow did not complete: {row}"


def test_cached_flow_endpoints(phase_a_run_dir):
    """Phase A specifically validates GS-0 (Tokyo) -> SAT-894 (compute)."""
    row = _flow_row(phase_a_run_dir)
    assert row[1] == "1584", f"unexpected src: {row}"   # GS-0 = node 1584
    # We don't pin dst to a literal because pick_dst_sat may legitimately
    # change if the seed/timing changes, but it must be in the compute set
    # and be a satellite (< 1584).
    dst = int(row[2])
    assert 0 <= dst < 1584, f"dst {dst} is not a satellite node id"


def test_cached_flow_size_was_1mb(phase_a_run_dir):
    row = _flow_row(phase_a_run_dir)
    assert int(row[3]) == 1_000_000
    assert int(row[7]) == 1_000_000  # bytes_sent equals size


def test_cached_flow_metadata_marker(phase_a_run_dir):
    row = _flow_row(phase_a_run_dir)
    assert "phase_a" in row[9].lower(), f"unexpected metadata: {row[9]!r}"


def test_cached_rtt_csv_has_samples(phase_a_run_dir):
    path = os.path.join(phase_a_run_dir, "logs_ns3", "tcp_flow_0_rtt.csv")
    if not os.path.exists(path):
        pytest.skip(f"no rtt csv at {path}")
    with open(path) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert len(lines) >= 50, f"too few RTT samples: {len(lines)}"
    # First column = flow id = 0.
    first_col = {row.split(",", 1)[0] for row in lines}
    assert first_col == {"0"}, f"unexpected flow ids in rtt csv: {first_col}"