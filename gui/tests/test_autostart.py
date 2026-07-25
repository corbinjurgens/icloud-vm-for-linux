"""Autostart-entry tests against a tmpdir home; no Qt import."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import autostart  # noqa: E402


def read(home) -> str:
    with open(autostart.autostart_path(home=str(home)), encoding="utf-8") as handle:
        return handle.read()


def test_missing_file_is_disabled(tmp_path):
    assert autostart.is_enabled(home=str(tmp_path)) is False


def test_enable_creates_entry_pointing_at_launcher(tmp_path):
    autostart.set_enabled(True, home=str(tmp_path))
    assert autostart.is_enabled(home=str(tmp_path)) is True
    text = read(tmp_path)
    assert f"Exec={autostart.launcher_path(home=str(tmp_path))} --minimized" in text
    assert "Hidden=false" in text
    assert "X-GNOME-Autostart-enabled=true" in text


def test_disable_sets_hidden_true(tmp_path):
    autostart.set_enabled(True, home=str(tmp_path))
    autostart.set_enabled(False, home=str(tmp_path))
    assert autostart.is_enabled(home=str(tmp_path)) is False
    text = read(tmp_path)
    assert "Hidden=true" in text
    assert "X-GNOME-Autostart-enabled=false" in text


def test_hidden_true_reads_as_disabled_even_with_gnome_enabled(tmp_path):
    path = autostart.autostart_path(home=str(tmp_path))
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("[Desktop Entry]\nExec=/x --minimized\n"
                     "X-GNOME-Autostart-enabled=true\nHidden=true\n")
    assert autostart.is_enabled(home=str(tmp_path)) is False


def test_toggle_preserves_existing_exec(tmp_path):
    path = autostart.autostart_path(home=str(tmp_path))
    os.makedirs(os.path.dirname(path))
    custom = "/opt/custom/icloud-bridge-gui --minimized"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"[Desktop Entry]\nType=Application\nExec={custom}\n"
                     "Icon=/opt/custom/icon.svg\nHidden=true\n")
    autostart.set_enabled(True, home=str(tmp_path))
    text = read(tmp_path)
    assert f"Exec={custom}" in text
    assert "Icon=/opt/custom/icon.svg" in text
    assert autostart.is_enabled(home=str(tmp_path)) is True


def test_toggle_does_not_duplicate_flag_lines(tmp_path):
    autostart.set_enabled(True, home=str(tmp_path))
    autostart.set_enabled(False, home=str(tmp_path))
    autostart.set_enabled(True, home=str(tmp_path))
    text = read(tmp_path)
    assert text.count("Hidden=") == 1
    assert text.count("X-GNOME-Autostart-enabled=") == 1
