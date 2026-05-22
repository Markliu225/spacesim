"""Unit tests for satellite_roles.py."""

from __future__ import annotations

import os

import pytest

from spacesim.topology import roles as sr


# ---- assign_by_plane -------------------------------------------------------


def test_by_plane_default_starlink_550_layout():
    """8 planes x 22 sats = 176 compute on the spec's Starlink-550 grid."""
    roles = sr.assign_by_plane(22, 72, [0, 8, 16, 24, 32, 40, 48, 56])
    assert len(roles) == 1584
    assert roles.count("C") == 176
    assert roles.count("T") == 1584 - 176


def test_by_plane_marks_correct_satellites():
    roles = sr.assign_by_plane(22, 72, [0, 8])
    # plane 0 = sat 0..21, plane 8 = sat 176..197
    for i in range(22):
        assert roles[i] == "C"
        assert roles[176 + i] == "C"
    # plane 1 entirely transit
    for i in range(22, 44):
        assert roles[i] == "T"


def test_by_plane_dedups_planes():
    """Repeated plane indices should not double-mark anything."""
    roles = sr.assign_by_plane(10, 5, [0, 0, 0, 2])
    assert roles.count("C") == 20  # 2 distinct planes x 10 sats


def test_by_plane_rejects_out_of_range():
    with pytest.raises(SystemExit, match="out of range"):
        sr.assign_by_plane(22, 72, [72])

    with pytest.raises(SystemExit, match="out of range"):
        sr.assign_by_plane(22, 72, [-1])


def test_by_plane_empty_planes_yields_all_transit():
    roles = sr.assign_by_plane(10, 5, [])
    assert roles == ["T"] * 50


# ---- assign_random ---------------------------------------------------------


def test_random_is_deterministic_for_same_seed():
    r1 = sr.assign_random(1000, 0.1, seed=42)
    r2 = sr.assign_random(1000, 0.1, seed=42)
    assert r1 == r2


def test_random_differs_for_different_seeds():
    r1 = sr.assign_random(1000, 0.1, seed=42)
    r2 = sr.assign_random(1000, 0.1, seed=43)
    assert r1 != r2


def test_random_respects_ratio():
    roles = sr.assign_random(1000, 0.1, seed=7)
    assert roles.count("C") == 100  # int(round(0.1 * 1000))


def test_random_rejects_invalid_ratio():
    for bad in (0.0, 1.0, -0.5, 1.5):
        with pytest.raises(SystemExit, match="in \\(0, 1\\)"):
            sr.assign_random(100, bad, seed=0)


# ---- read_tles_header ------------------------------------------------------


def test_read_tles_header_on_reduced_kuiper(reduced_kuiper_state):
    """The reduced Kuiper state has 1 orbital plane × 17 sats."""
    tles_path = os.path.join(reduced_kuiper_state, "tles.txt")
    num_planes, sats_per_plane, total = sr.read_tles_header(tles_path)
    assert num_planes == 1
    assert sats_per_plane == 17
    assert total == 17


# ---- write_roles + CLI form -----------------------------------------------


def test_write_roles_round_trips(tmp_path):
    roles_in = ["C", "T", "T", "C"]
    out = tmp_path / "roles.txt"
    sr.write_roles(str(out), roles_in)
    content = out.read_text().strip().splitlines()
    assert content == ["0,C", "1,T", "2,T", "3,C"]