"""Best-effort presentation state for the desktop window.

This small XDG-state document remembers only the window position, size, and
selected tab.  It is presentation state, never operator data: losing it must
never matter and it must never gate application startup.  This Qt-free module
does no CIFS or subprocess work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import backup

STATE_NAME = "window.json"
MIN_WIDTH = 760
MIN_HEIGHT = 520
MAX_WIDTH = 7680
MAX_HEIGHT = 4320
MAX_STATE_BYTES = 4096
TABS = ("Status", "Selective Sync", "Safe Workspaces", "Setup")


@dataclass(frozen=True)
class WindowState:
    width: int = 880
    height: int = 620
    x: int = 0
    y: int = 0
    tab: str = "Status"


DEFAULT = WindowState()


def path(base: str | None = None) -> str:
    """The one presentation-state document under the private app directory."""
    return os.path.join(backup.app_dir(base), STATE_NAME)


def _integer(value: object, minimum: int, maximum: int) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if not minimum <= value <= maximum:
        return None
    return value


def _parse(document: object) -> WindowState | None:
    if not isinstance(document, dict):
        return None
    width = _integer(document.get("width"), MIN_WIDTH, MAX_WIDTH)
    height = _integer(document.get("height"), MIN_HEIGHT, MAX_HEIGHT)
    x = _integer(document.get("x"), -MAX_WIDTH, MAX_WIDTH)
    y = _integer(document.get("y"), -MAX_HEIGHT, MAX_HEIGHT)
    tab = document.get("tab")
    if None in (width, height, x, y) or tab not in TABS:
        return None
    return WindowState(width, height, x, y, tab)


def load(base: str | None = None) -> WindowState:
    """Return saved state, or defaults for every local-file failure."""
    saved = path(base)
    try:
        if os.path.islink(saved):
            return DEFAULT
        if os.path.getsize(saved) > MAX_STATE_BYTES:
            return DEFAULT
        with open(saved, "rb") as handle:
            document = json.loads(handle.read(MAX_STATE_BYTES + 1).decode(
                "utf-8", errors="strict"))
    except (OSError, ValueError, UnicodeDecodeError):
        return DEFAULT
    return _parse(document) or DEFAULT


def _clamped(value: object, minimum: int, maximum: int) -> int | None:
    """A size the window could actually have, or ``None`` for a non-number."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return max(minimum, min(maximum, value))


def save(width: int, height: int, x: int, y: int, tab: str,
         *, base: str | None = None) -> bool:
    """Atomically record valid presentation state; failures are inconsequential."""
    size = (_clamped(width, MIN_WIDTH, MAX_WIDTH),
            _clamped(height, MIN_HEIGHT, MAX_HEIGHT))
    position = (_integer(x, -MAX_WIDTH, MAX_WIDTH),
                _integer(y, -MAX_HEIGHT, MAX_HEIGHT))
    if None in size or None in position or tab not in TABS:
        return False
    try:
        directory = backup.ensure_app_dir(base)
        destination = os.path.join(directory, STATE_NAME)
        backup.check_destination(destination)
        backup.write_json_atomic(destination, {
            "width": size[0], "height": size[1],
            "x": position[0], "y": position[1], "tab": tab,
        })
    except Exception:
        # Deliberately everything: this is the window's remembered size, and the
        # callers are `closeEvent` and the quit path. Nothing about the bridge,
        # the operator's data or the lifecycle depends on it, so no failure here
        # may propagate into either of those.
        return False
    return True
