"""Tests for the offline path-tracing logic in analyze_phase_a.py.

The IO-heavy parts of analyze_phase_a (flow-summary parsing, RTT loading)
are covered by the regression test which runs the real analyzer on the
Phase A run output. Here we focus on the pure path-tracing function with
synthetic small fstate dicts, including edge cases.
"""

from __future__ import annotations

import pytest

import analyze_phase_a as ap


def test_trace_path_direct_neighbours():
    fstate = {
        (0, 2): (1, 0, 0),
        (1, 2): (2, 0, 0),
    }
    assert ap.trace_path(fstate, 0, 2) == [0, 1, 2]


def test_trace_path_single_hop():
    fstate = {(0, 1): (1, 0, 0)}
    assert ap.trace_path(fstate, 0, 1) == [0, 1]


def test_trace_path_src_equals_dst_returns_singleton():
    """If src == dst we never enter the loop -> path is just [src]."""
    assert ap.trace_path({}, 5, 5) == [5]


def test_trace_path_raises_on_drop():
    fstate = {(0, 1): (-1, -1, -1)}
    with pytest.raises(RuntimeError, match="drop entry"):
        ap.trace_path(fstate, 0, 1)


def test_trace_path_raises_on_missing_entry():
    """fstate lookup miss = packet has no route = trace fails."""
    fstate = {(0, 5): (1, 0, 0)}  # only the src -> next-hop edge
    with pytest.raises(RuntimeError, match="no fstate entry"):
        ap.trace_path(fstate, 0, 5)  # after step 1, (1, 5) is missing


def test_trace_path_raises_on_loop():
    """0 -> 1 -> 0 cycles back."""
    fstate = {
        (0, 2): (1, 0, 0),
        (1, 2): (0, 0, 0),
    }
    with pytest.raises(RuntimeError, match="loop detected"):
        ap.trace_path(fstate, 0, 2)


def test_trace_path_max_hops_guard():
    """Path that doesn't loop but exceeds max_hops should still fail."""
    # 0 -> 1 -> 2 -> 3 -> 4 -> 5 (5 hops, dst=99 never reached)
    fstate = {(i, 99): (i + 1, 0, 0) for i in range(20)}
    with pytest.raises(RuntimeError, match="max_hops"):
        ap.trace_path(fstate, 0, 99, max_hops=5)


# ---- read_fstate parses our augmented files correctly ---------------------


def test_read_fstate_basic(tmp_path):
    f = tmp_path / "fstate_0.txt"
    f.write_text("0,5,1,0,0\n1,5,2,0,0\n2,5,5,0,3\n")
    fstate = ap.read_fstate(str(f))
    assert fstate[(0, 5)] == (1, 0, 0)
    assert fstate[(1, 5)] == (2, 0, 0)
    assert fstate[(2, 5)] == (5, 0, 3)


def test_read_fstate_skips_comments_and_blanks(tmp_path):
    """Defensive: even though augment shouldn't write '#' lines anymore,
    the offline analyzer is the one tool that *should* tolerate them."""
    f = tmp_path / "fstate_0.txt"
    f.write_text(
        "# this is a comment\n"
        "\n"
        "0,5,1,0,0\n"
        "1,5,2,0,0\n"
        "  \n"
    )
    fstate = ap.read_fstate(str(f))
    assert len(fstate) == 2
    assert fstate[(0, 5)] == (1, 0, 0)
    assert fstate[(1, 5)] == (2, 0, 0)


def test_read_fstate_skips_malformed_rows(tmp_path):
    """A row with fewer than 5 columns is ignored, not fatal."""
    f = tmp_path / "fstate_0.txt"
    f.write_text("0,5,1,0,0\nthis,is,bad\n1,5,2,0,0\n")
    fstate = ap.read_fstate(str(f))
    assert len(fstate) == 2


# ---- read_schedule --------------------------------------------------------


def test_read_schedule(tmp_path):
    f = tmp_path / "schedule.csv"
    f.write_text("0,1584,894,1000000,200000000,,phase_a_gs0_to_compute\n")
    flow_id, src, dst, start_ns = ap.read_schedule(str(f))
    assert (flow_id, src, dst, start_ns) == (0, 1584, 894, 200000000)


def test_read_schedule_skips_comments(tmp_path):
    f = tmp_path / "schedule.csv"
    f.write_text("# header\n0,1584,894,1000000,200000000,,note\n")
    flow_id, src, dst, start_ns = ap.read_schedule(str(f))
    assert dst == 894


def test_read_schedule_empty_raises(tmp_path):
    f = tmp_path / "schedule.csv"
    f.write_text("\n# comment only\n")
    with pytest.raises(SystemExit, match="empty schedule"):
        ap.read_schedule(str(f))