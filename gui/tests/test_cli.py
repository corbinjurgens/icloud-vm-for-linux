"""CLI surface tests: exact ``--version`` output and parsing, no Qt required."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import __version__, cli  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_version_line_is_exact():
    assert cli.version_line() == f"icloud-bridge-gui {__version__}"


def test_version_action_prints_and_exits(capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"icloud-bridge-gui {__version__}\n"


def test_minimized_flag_still_parses():
    assert cli.build_parser().parse_args(["--minimized"]).minimized is True
    assert cli.build_parser().parse_args([]).minimized is False


def test_make_version_reports_the_same_value():
    """The package version is derived from ``__version__``; nothing to sync."""
    completed = subprocess.run(["make", "--no-print-directory", "version"],
                               cwd=REPO, capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == __version__
