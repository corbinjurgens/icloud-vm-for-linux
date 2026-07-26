"""Command-line surface: the argument parser and the ``--version`` string.

Kept out of :mod:`__main__` and free of Qt for one reason — ``--version`` must be
answerable without a display, a tray, or PySide6, and its exact output is worth a
test that runs in the no-Qt suite too.  ``__main__`` owns everything that follows
parsing (the single-instance socket, ``QApplication``); nothing here has side
effects beyond argparse's own ``--version``/``--help`` exit.
"""

from __future__ import annotations

import argparse

from . import __version__

PROGRAM_NAME = "icloud-bridge-gui"
DESCRIPTION = "iCloud bridge status and selective sync"


def version_line() -> str:
    """The single line ``--version`` prints, e.g. ``icloud-bridge-gui 0.2.0``.

    ``__init__.__version__`` is the one version source in this repository — the
    ``Makefile`` and ``packaging/build-deb.sh`` derive the package version from
    it, so there is deliberately nothing here to keep in step.
    """
    return f"{PROGRAM_NAME} {__version__}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM_NAME, description=DESCRIPTION)
    parser.add_argument("--minimized", action="store_true",
                        help="start in the tray without showing the window (autostart)")
    parser.add_argument("--version", action="version", version=version_line(),
                        help="print the version and exit")
    return parser
