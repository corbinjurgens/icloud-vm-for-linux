"""Tray ("menu bar") icon (v2 plan section 6.2).

The icon colour is the overall health state from :mod:`.health`; the tooltip
lists whatever is failing.  The menu is the shortest useful set of actions:
open the files, open the status window, open the VM screen, quit.
"""

from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import bridge, health

VM_VIEWER_URL = "http://127.0.0.1:8006"

_ICON_FILES = {
    health.GREEN: "icloud-green.svg",
    health.YELLOW: "icloud-yellow.svg",
    health.RED: "icloud-red.svg",
}
_FALLBACK_COLORS = {
    health.GREEN: "#2e9e4f",
    health.YELLOW: "#d99b1a",
    health.RED: "#c8402c",
}


def open_externally(target: str) -> None:
    """Hand a path or URL to the desktop, without blocking the GUI thread."""
    try:
        subprocess.Popen(["xdg-open", target],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def load_icon(state: str) -> QIcon:
    """The SVG for a state, or a plain coloured disc if Qt has no SVG support."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons",
                        _ICON_FILES.get(state, _ICON_FILES[health.RED]))
    icon = QIcon(path)
    if not icon.isNull() and icon.availableSizes():
        return icon
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(_FALLBACK_COLORS.get(state, "#c8402c")))
    painter.setPen(QColor(0, 0, 0, 0))
    painter.drawEllipse(6, 6, 52, 52)
    painter.end()
    return QIcon(pixmap)


def tooltip_for(snapshot_checks) -> str:
    failing = [c for c in snapshot_checks if c.severity != health.GREEN]
    if not failing:
        return "iCloud bridge: healthy"
    lines = ["iCloud bridge:"]
    lines.extend(f"  {c.name}: {c.detail}" for c in failing)
    return "\n".join(lines)


class TrayIcon(QObject):
    """Thin wrapper so the window and the tray share one state update path."""

    show_window_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._icon = QSystemTrayIcon(load_icon(health.RED), self)
        self._icon.setToolTip("iCloud bridge: starting…")

        menu = QMenu()
        self._action_open_files = QAction("Open iCloud folder", menu)
        self._action_open_files.triggered.connect(lambda: open_externally(bridge.mount_dir()))
        menu.addAction(self._action_open_files)

        action_status = QAction("Open status window", menu)
        action_status.triggered.connect(self.show_window_requested.emit)
        menu.addAction(action_status)

        action_vm = QAction("Open VM screen", menu)
        action_vm.triggered.connect(lambda: open_externally(VM_VIEWER_URL))
        menu.addAction(action_vm)

        menu.addSeparator()
        action_quit = QAction("Quit", menu)
        action_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(action_quit)

        self._menu = menu           # keep a reference; QMenu has no parent here
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window_requested.emit()

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def update_state(self, state: str, checks) -> None:
        self._icon.setIcon(load_icon(state))
        self._icon.setToolTip(tooltip_for(checks))
