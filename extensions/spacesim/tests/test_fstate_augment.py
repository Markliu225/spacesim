"""Unit + integration tests for augment_fstate.py.

The unit tests cover the small pure-Python helpers (parsing, manifest IO,
stripping, ISL metadata). The integration test runs the whole augment
algorithm on a single timestep of the reduced Kuiper-630 state. It is
relatively cheap (<30 seconds) but does invoke SGP-4, so we keep it as a
single timestep.
"""

from __future__ import annotations

import json
import os

import pytest

from spacesim.topology import fstate_augment as af


# ---- parse_dst_sats --------------------------------------------------------


def test_parse_dst_sats_single():
    assert af.parse_dst_sats("894", None, 1584) == [894]


def test_parse_dst_sats_multiple_sorted_unique():
    assert af.parse_dst_sats("100,50,50", None, 1584) == [50, 100]


def test_parse_dst_sats_rejects_out_of_range():
    with pytest.raises(SystemExit, match="out of range"):
        af.parse_dst_sats("1584", None, 1584)
    with pytest.raises(SystemExit, match="out of range"):
        af.parse_dst_sats("-1", None, 1584)


def test_parse_dst_sats_all_compute_requires_roles():
    with pytest.raises(SystemExit, match="requires --roles"):
        af.parse_dst_sats("all-compute", None, 100)


def test_parse_dst_sats_all_compute(tmp_path):
    roles = tmp_path / "roles.txt"
    roles.write_text("0,C\n1,T\n2,C\n5,C\n")
    assert af.parse_dst_sats("all-compute", str(roles), 100) == [0, 2, 5]


def test_parse_dst_sats_skips_blank_and_comment_lines(tmp_path):
    roles = tmp_path / "roles.txt"
    roles.write_text("\n# header\n0,C\n\n1,T\n")
    assert af.parse_dst_sats("all-compute", str(roles), 100) == [0]


# ---- strip_lines_for_dsts --------------------------------------------------


def test_strip_lines_removes_target_and_comments(tmp_path):
    f = tmp_path / "fstate_0.txt"
    f.write_text(
        "1,5,7,0,0\n"
        "# comment\n"
        "3,894,9,1,0\n"
        "# PHASE_A_AUGMENT begin: 1683 rows\n"
        "5,7,9,0,1\n"
    )
    removed = af.strip_lines_for_dsts(str(f), {894})
    assert removed == 3  # 2 comments + 1 dst=894
    remaining = f.read_text().splitlines()
    assert remaining == ["1,5,7,0,0", "5,7,9,0,1"]


def test_strip_lines_no_op_when_nothing_matches(tmp_path):
    f = tmp_path / "fstate_0.txt"
    original = "1,5,7,0,0\n3,8,9,1,0\n"
    f.write_text(original)
    removed = af.strip_lines_for_dsts(str(f), {999})
    assert removed == 0
    assert f.read_text() == original


def test_strip_lines_missing_file_is_zero(tmp_path):
    assert af.strip_lines_for_dsts(str(tmp_path / "nonexistent.txt"), {1}) == 0


def test_strip_lines_preserves_unparseable_rows(tmp_path):
    """If a row's 2nd field isn't an int, leave it alone (don't crash)."""
    f = tmp_path / "fstate.txt"
    f.write_text("1,not_an_int,7,0,0\n2,5,3,0,0\n")
    removed = af.strip_lines_for_dsts(str(f), {5})
    assert removed == 1
    assert "not_an_int" in f.read_text()


# ---- manifest IO ----------------------------------------------------------


def test_manifest_round_trips(tmp_path):
    m = {894: [0, 100_000_000], 12: [0]}
    af.save_manifest(str(tmp_path), m)
    loaded = af.load_manifest(str(tmp_path))
    assert loaded == m


def test_manifest_load_missing_returns_empty(tmp_path):
    assert af.load_manifest(str(tmp_path)) == {}


def test_manifest_load_corrupt_returns_empty(tmp_path):
    (tmp_path / af.MANIFEST_FILENAME).write_text("definitely not json")
    assert af.load_manifest(str(tmp_path)) == {}


def test_manifest_has():
    m = {894: [0, 100], 12: [0]}
    assert af.manifest_has(m, 894, 0)
    assert af.manifest_has(m, 894, 100)
    assert not af.manifest_has(m, 894, 50)
    assert not af.manifest_has(m, 5, 0)


# ---- build_isl_metadata ---------------------------------------------------


def test_build_isl_metadata_simple_triangle():
    """Three sats fully connected, ISLs declared as (0,1),(1,2),(0,2)."""
    isls = [(0, 1), (1, 2), (0, 2)]
    num_isls, nbr_to_if = af.build_isl_metadata(isls, 3)
    assert num_isls == [2, 2, 2]
    # (0,1) is the first ISL touching either node -> idx 0 on both
    assert nbr_to_if[(0, 1)] == 0
    assert nbr_to_if[(1, 0)] == 0
    # (1,2) is the 2nd ISL on node 1 (-> idx 1) and 1st on node 2 (-> idx 0)
    assert nbr_to_if[(1, 2)] == 1
    assert nbr_to_if[(2, 1)] == 0
    # (0,2) is 2nd ISL on node 0 and 2nd on node 2
    assert nbr_to_if[(0, 2)] == 1
    assert nbr_to_if[(2, 0)] == 1


def test_build_isl_metadata_no_isls():
    num_isls, nbr_to_if = af.build_isl_metadata([], 4)
    assert num_isls == [0, 0, 0, 0]
    assert nbr_to_if == {}


# ---- discover_timesteps ----------------------------------------------------


def test_discover_timesteps_sorts_numerically(tmp_path):
    """ASCII sort would put 2000000 before 100000000; integer sort must not."""
    for t in (100_000_000, 0, 2_000_000_000, 1_000_000_000):
        (tmp_path / f"fstate_{t}.txt").write_text("")
    (tmp_path / "fstate_NOT_A_NUMBER.txt").write_text("")  # ignored
    assert af.discover_timesteps(str(tmp_path)) == [
        0, 100_000_000, 1_000_000_000, 2_000_000_000
    ]


# ---- append_rows produces no comment lines --------------------------------


def test_append_rows_writes_only_csv(tmp_path):
    f = tmp_path / "fstate_0.txt"
    af.append_rows(str(f), [(0, 894, 1, 0, 0), (1, 894, 2, 1, 3)])
    content = f.read_text()
    assert content == "0,894,1,0,0\n1,894,2,1,3\n"
    assert "#" not in content


# ---- Integration test on reduced Kuiper-630 -------------------------------


def test_compute_augment_rows_on_reduced_kuiper(reduced_kuiper_state):
    """Run the full augment algorithm against one timestep of real state.

    reduced Kuiper-630 = 17 satellites + 2 ground stations. Manila (GS 0)
    and Dalian (GS 1) are at node IDs 17 and 18 respectively. We add
    routes to dst SAT 5 (an arbitrary sat) and assert:
      - row count = (17 sats - 1 self) + 2 GSs = 18 rows
      - every row's 2nd column equals 5
      - exactly one row per src node id
      - GS rows use my_if=0 (GS GSL) and next_if=num_isls_per_sat[next_hop]
    """
    from satgen.tles import read_tles
    from satgen.isls import read_isls
    from satgen.ground_stations import read_ground_stations_extended
    import exputil

    tles = read_tles(os.path.join(reduced_kuiper_state, "tles.txt"))
    satellites = tles["satellites"]
    epoch = tles["epoch"]
    list_isls = read_isls(
        os.path.join(reduced_kuiper_state, "isls.txt"), len(satellites))
    ground_stations = read_ground_stations_extended(
        os.path.join(reduced_kuiper_state, "ground_stations.txt"))
    desc = exputil.PropertiesConfig(
        os.path.join(reduced_kuiper_state, "description.txt"))
    max_isl = exputil.parse_positive_float(
        desc.get_property_or_fail("max_isl_length_m"))
    max_gsl = exputil.parse_positive_float(
        desc.get_property_or_fail("max_gsl_length_m"))
    num_isls_per_sat, nbr_to_if = af.build_isl_metadata(
        list_isls, len(satellites))

    rows = af.compute_augment_rows(
        time_since_epoch_ns=0,
        epoch=epoch,
        satellites=satellites,
        ground_stations=ground_stations,
        list_isls=list_isls,
        num_isls_per_sat=num_isls_per_sat,
        sat_neighbor_to_if=nbr_to_if,
        max_isl_length_m=max_isl,
        max_gsl_length_m=max_gsl,
        dst_sats=[5],
    )

    # Count: 17 sats - 1 self + 2 GSs = 18.
    assert len(rows) == 18

    # Every row targets sat 5.
    assert {r[1] for r in rows} == {5}

    # Exactly one row per src node id.
    src_ids = [r[0] for r in rows]
    assert sorted(src_ids) == [i for i in range(17) if i != 5] + [17, 18]
    assert len(set(src_ids)) == len(src_ids)

    # GS row sanity checks.
    by_src = {r[0]: r for r in rows}
    for gs_node_id in (17, 18):
        curr, dst, nh, my_if, next_if = by_src[gs_node_id]
        assert dst == 5
        # GS has 1 GSL iface (idx 0). next_if = num_isls on the receiving sat.
        if nh != -1:  # not a drop
            assert my_if == 0
            assert next_if == num_isls_per_sat[nh]
        # The next_hop must be a satellite or -1 (drop), never another GS.
        assert nh == -1 or nh < 17

    # Every sat row's my_if/next_if should be valid ISL slot indices.
    for r in rows:
        curr, dst, nh, my_if, next_if = r
        if curr < 17 and nh != -1:
            assert my_if < num_isls_per_sat[curr]
            assert next_if < num_isls_per_sat[nh]
            # nbr_to_if must agree.
            assert nbr_to_if[(curr, nh)] == my_if
            assert nbr_to_if[(nh, curr)] == next_if