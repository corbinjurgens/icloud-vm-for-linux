"""Qt-free tests for the inconsequential window presentation state."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import backup, uistate  # noqa: E402


@pytest.fixture
def base(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return str(state)


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_round_trip_is_private_and_atomic(base):
    assert uistate.save(1000, 700, 40, 50, "Selective Sync", base=base)
    assert uistate.load(base) == uistate.WindowState(1000, 700, 40, 50,
                                                      "Selective Sync")
    assert mode(backup.app_dir(base)) == 0o700
    assert mode(uistate.path(base)) == 0o600
    assert os.listdir(backup.app_dir(base)) == [uistate.STATE_NAME]


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps([]),
    json.dumps({"width": "wide", "height": 620, "x": 0, "y": 0, "tab": "Status"}),
    json.dumps({"width": 880, "height": True, "x": 0, "y": 0, "tab": "Status"}),
    json.dumps({"width": 880, "height": 620, "x": 0, "y": 0, "tab": "Unknown"}),
    json.dumps({"width": 1, "height": 620, "x": 0, "y": 0, "tab": "Status"}),
    json.dumps({"width": 880, "height": 620, "x": 99999, "y": 0, "tab": "Status"}),
])
def test_corruption_returns_defaults_without_raising(base, payload):
    backup.ensure_app_dir(base)
    with open(uistate.path(base), "w", encoding="utf-8") as handle:
        handle.write(payload)
    assert uistate.load(base) == uistate.DEFAULT


def test_absent_file_returns_defaults(base):
    assert uistate.load(base) == uistate.DEFAULT


def test_oversized_file_returns_defaults(base):
    backup.ensure_app_dir(base)
    with open(uistate.path(base), "wb") as handle:
        handle.write(b"{" + b"x" * uistate.MAX_STATE_BYTES)
    assert uistate.load(base) == uistate.DEFAULT


def test_save_clamps_size_to_the_window_bounds(base):
    assert uistate.save(1, 99999, 0, 0, "Status", base=base)
    saved = uistate.load(base)
    assert saved.width == uistate.MIN_WIDTH
    assert saved.height == uistate.MAX_HEIGHT


def test_a_symlinked_state_file_is_not_followed_on_read(base, tmp_path):
    """The read side refuses the link too, so it cannot be aimed at a secret."""
    backup.ensure_app_dir(base)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(
        json.dumps({"width": 1000, "height": 700, "x": 0, "y": 0,
                    "tab": "Status"}), encoding="utf-8")
    os.symlink(elsewhere, uistate.path(base))
    assert uistate.load(base) == uistate.DEFAULT


def test_symlinked_destination_is_refused_without_touching_its_target(base, tmp_path):
    backup.ensure_app_dir(base)
    victim = tmp_path / "victim.json"
    victim.write_text("keep", encoding="utf-8")
    os.symlink(victim, uistate.path(base))
    assert not uistate.save(880, 620, 0, 0, "Status", base=base)
    assert victim.read_text(encoding="utf-8") == "keep"
