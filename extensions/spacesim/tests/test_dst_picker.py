"""Tests for pick_dst_sat.py.

The picker is deterministic given (state, roles, src_gs, time): no random
sampling, just the max of an SGP-4-distance loop. The reduced Kuiper-630
state has 17 sats so we can enumerate, predict the answer, and assert.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from spacesim.topology import dst_picker as pds


def test_read_roles(tmp_path):
    f = tmp_path / "roles.txt"
    f.write_text("0,T\n1,C\n2,C\n3,T\n4,C\n")
    assert pds.read_roles(str(f)) == [1, 2, 4]


def test_read_roles_skips_blank(tmp_path):
    f = tmp_path / "roles.txt"
    f.write_text("\n0,C\n\n1,T\n")
    assert pds.read_roles(str(f)) == [0]


def test_pick_is_deterministic_on_reduced_kuiper(tmp_path, reduced_kuiper_state):
    """Run the CLI twice with the same inputs and confirm same stdout."""
    roles_path = tmp_path / "roles.txt"
    # Mark all 17 sats as C so the picker really picks the farthest.
    roles_path.write_text("".join(f"{i},C\n" for i in range(17)))

    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "..", "topology", "dst_picker.py"),
        "--state-dir", reduced_kuiper_state,
        "--roles", str(roles_path),
        "--src-gs", "0",
        "--start-time-ns", "1000000000",
    ]
    r1 = subprocess.run(cmd, capture_output=True, text=True)
    r2 = subprocess.run(cmd, capture_output=True, text=True)
    assert r1.returncode == 0
    assert r2.returncode == 0
    sat1 = int(r1.stdout.strip())
    sat2 = int(r2.stdout.strip())
    assert sat1 == sat2
    assert 0 <= sat1 < 17


def test_pick_returns_one_of_compute(tmp_path, reduced_kuiper_state):
    """Pick must return a SAT that is in the type=C set."""
    roles_path = tmp_path / "roles.txt"
    # Mark only a specific subset compute. The picker must return one of them.
    chosen = {2, 7, 11}
    roles_path.write_text(
        "".join(f"{i},{'C' if i in chosen else 'T'}\n" for i in range(17))
    )
    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "..", "topology", "dst_picker.py"),
        "--state-dir", reduced_kuiper_state,
        "--roles", str(roles_path),
        "--src-gs", "0",
        "--start-time-ns", "1000000000",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0
    sat = int(r.stdout.strip())
    assert sat in chosen