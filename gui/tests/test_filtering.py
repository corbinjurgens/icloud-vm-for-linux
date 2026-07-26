"""Selective-sync filter tests: matching and visibility, no Qt required."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import filtering  # noqa: E402

PATHS = [
    "Docs",
    "Docs/Taxes",
    "Docs/Taxes/2025",
    "Docs/report.pdf",
    "Photos",
    "Photos/Trip",
]


def test_no_filter_is_distinct_from_matching_everything():
    """None means 'restore what the operator had', not 'show all'."""
    assert filtering.visible_paths("", PATHS) is None
    assert filtering.visible_paths("   ", PATHS) is None


def test_a_name_match_brings_its_ancestors_along():
    visible = filtering.visible_paths("taxes", PATHS)
    assert visible == {"docs", "docs/taxes", "docs/taxes/2025"}


def test_matching_is_case_insensitive():
    assert filtering.visible_paths("TRIP", PATHS) == {"photos", "photos/trip"}
    assert filtering.visible_paths("trip", PATHS) == {"photos", "photos/trip"}


def test_a_path_fragment_matches_too():
    visible = filtering.visible_paths("docs/tax", PATHS)
    assert "docs/taxes" in visible
    assert "photos" not in visible


def test_a_parent_match_does_not_drag_in_its_children():
    # "Docs" matches; its descendants are only shown when they match too.
    visible = filtering.visible_paths("docs", PATHS)
    assert visible == {"docs", "docs/taxes", "docs/taxes/2025", "docs/report.pdf"}


def test_no_match_hides_everything():
    assert filtering.visible_paths("nothing-here", PATHS) == set()


def test_a_loaded_file_participates():
    assert "docs/report.pdf" in filtering.visible_paths("report", PATHS)


def test_a_missing_configured_item_matches_by_its_full_path():
    """Missing items only ever have a path, never a tree row to match by name."""
    visible = filtering.visible_paths("Old/Archive", ["Old/Archive"])
    assert visible == {"old", "old/archive"}


def test_ancestors_of_a_top_level_path_is_empty():
    assert filtering.ancestors("Docs") == []
    assert filtering.ancestors("Docs/Taxes/2025") == ["docs", "docs/taxes"]


def test_backslashes_normalize_like_bridge_is_under():
    assert filtering.normalize("Docs\\Taxes") == "docs/taxes"
    assert filtering.matches("docs/taxes", "Docs\\Taxes") is True


def test_non_string_and_empty_entries_are_skipped():
    assert filtering.visible_paths("x", ["", None, 5, "xylophone"]) == {"xylophone"}
