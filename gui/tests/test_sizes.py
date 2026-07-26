"""Excluded-space aggregation tests: source precedence and honest unknowns."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import sizes  # noqa: E402

GB = 1024 ** 3


def test_nothing_excluded():
    summary = sizes.summarize([])
    assert summary.roots == 0
    assert summary.text() == "Nothing is excluded."


def test_a_folder_root_uses_the_recursive_tree_size():
    summary = sizes.summarize(["Docs/Big"], folder_sizes={"docs/big": 20 * GB})
    assert summary.roots == 1
    assert summary.known_bytes == 20 * GB
    assert summary.unknown == 0
    assert summary.text() == "Excluded: 1 root, about 20.0 GB logical"


def test_a_file_root_uses_the_size_loaded_this_session():
    summary = sizes.summarize(["Docs/huge.mp4"], file_sizes={"docs/huge.mp4": 4 * GB})
    assert summary.known_bytes == 4 * GB
    assert summary.unknown == 0


def test_status_supplies_the_last_applied_size_for_a_configured_root():
    summary = sizes.summarize(["Old Folder"],
                              status_sizes={"old folder": 3 * GB},
                              configured=["Old Folder"])
    assert summary.known_bytes == 3 * GB


def test_status_is_not_used_for_a_freshly_staged_root():
    """A staged exclusion has no applied size; claiming one would be a fiction."""
    summary = sizes.summarize(["Old Folder"],
                              status_sizes={"old folder": 3 * GB},
                              configured=[])
    assert summary.known_bytes == 0
    assert summary.unknown == 1


def test_a_staged_re_include_drops_out_of_the_total():
    summary = sizes.summarize(["Keep"],                       # "Drop" was unticked
                              folder_sizes={"keep": GB, "drop": 50 * GB},
                              configured=["Keep", "Drop"])
    assert summary.roots == 1
    assert summary.known_bytes == GB


def test_lookups_are_case_insensitive():
    summary = sizes.summarize(["DOCS/BIG"], folder_sizes={"docs/big": 2 * GB})
    assert summary.known_bytes == 2 * GB


def test_an_unknown_root_is_counted_not_treated_as_zero():
    summary = sizes.summarize(["Gone", "Docs/Big"], folder_sizes={"docs/big": GB})
    assert summary.roots == 2
    assert summary.unknown == 1
    assert summary.text() == "Excluded: 2 roots, about 1.0 GB logical (1 size unknown)"


def test_all_unknown_says_so_rather_than_about_zero_bytes():
    summary = sizes.summarize(["Gone", "AlsoGone"])
    assert summary.text() == "Excluded: 2 roots, size unknown"


def test_several_unknown_roots_pluralize():
    summary = sizes.summarize(["A", "B", "C"], folder_sizes={"a": GB})
    assert summary.text() == "Excluded: 3 roots, about 1.0 GB logical (2 sizes unknown)"


def test_malformed_sizes_are_unknown_not_summed():
    summary = sizes.summarize(["A", "B", "C", "D"],
                              folder_sizes={"a": "20GB", "b": -1, "c": True, "d": GB})
    assert summary.known_bytes == GB
    assert summary.unknown == 3


def test_a_child_beside_its_parent_is_not_double_counted():
    """D19 already forbids this; the aggregator must still fail safely."""
    summary = sizes.summarize(["Docs", "Docs/Big"],
                              folder_sizes={"docs": 30 * GB, "docs/big": 20 * GB})
    assert summary.roots == 1
    assert summary.known_bytes == 30 * GB


def test_duplicate_casings_collapse_to_one_root():
    summary = sizes.summarize(["Docs", "DOCS"], folder_sizes={"docs": GB})
    assert summary.roots == 1
    assert summary.known_bytes == GB


def test_non_string_entries_are_ignored():
    summary = sizes.summarize([None, 7, "", "Docs"], folder_sizes={"docs": GB})
    assert summary.roots == 1


def test_folder_size_wins_over_a_stale_status_size():
    summary = sizes.summarize(["Docs"],
                              folder_sizes={"docs": 10 * GB},
                              status_sizes={"docs": 99 * GB},
                              configured=["Docs"])
    assert summary.known_bytes == 10 * GB
