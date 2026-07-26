"""Tray ("menu bar") icon (v2 plan section 6.2, D29).

The icon colour is the overall health state from :mod:`.health`; the tooltip
lists whatever is failing.  A fourth **starting** icon (distinct blue disc) marks
the multi-minute Windows-boot transition so it never reads as the yellow
"degraded" fault state.  The menu opens the files, the status window and the VM
screen, toggles start-at-login, and quits.
"""

from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import autostart, bridge, health, power
from . import notify as notify_model

VM_VIEWER_URL = "http://127.0.0.1:8006"

#: A transition state that is neither of the three health colours (v2 plan D29):
#: a Windows boot must not look like a fault.
STARTING = "starting"
#: Deliberately powered off (D30): grey, because an intentional off state is not
#: a fault either — and the app is still running, so the icon must not vanish.
OFF = "off"

_ICON_FILES = {
    health.GREEN: "icloud-green.svg",
    health.YELLOW: "icloud-yellow.svg",
    health.RED: "icloud-red.svg",
    STARTING: "icloud-starting.svg",
    OFF: "icloud-off.svg",
}
_FALLBACK_COLORS = {
    health.GREEN: "#2e9e4f",
    health.YELLOW: "#d99b1a",
    health.RED: "#c8402c",
    STARTING: "#3a7bd5",
    OFF: "#8b8e91",
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
    retry_start_requested = Signal()
    #: D30: power the whole bridge off/on without quitting the app.
    power_off_requested = Signal()
    start_requested = Signal()
    #: D35/D40-D44: inspect the guest and repair what no longer matches this
    #: app. The same controller action the Status tab and the skew banner
    #: invoke, with the same enablement rule.
    reprovision_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._busy = False
        self._bridge_available = True
        self._icon = QSystemTrayIcon(load_icon(STARTING), self)
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

        # Shown only after a failed power-on; a dead-end otherwise (v2 plan D29).
        self._action_retry = QAction("Retry start", menu)
        self._action_retry.triggered.connect(self.retry_start_requested.emit)
        self._action_retry.setVisible(False)
        menu.addAction(self._action_retry)

        # Exactly one of these is ever visible, and only when the lifecycle
        # state says so — never because health went red (D30).
        self._action_power_off = QAction("Power off bridge (keep this app running)", menu)
        self._action_power_off.triggered.connect(self.power_off_requested.emit)
        self._action_power_off.setVisible(False)
        menu.addAction(self._action_power_off)

        self._action_start = QAction("Start bridge", menu)
        self._action_start.triggered.connect(self.start_requested.emit)
        self._action_start.setVisible(False)
        menu.addAction(self._action_start)

        # Guest repair (D35/D40-D44). Confirmed by the controller, and offered
        # by exactly the same rule as the Status-tab button — including while
        # the bridge protocol is skewed or incompatible.
        self._action_reprovision = QAction("Re-run Windows provisioning…", menu)
        self._action_reprovision.triggered.connect(self.reprovision_requested.emit)
        self._action_reprovision.setVisible(False)
        menu.addAction(self._action_reprovision)

        # First run: the assistant is a tab, so the tray's job is only to lead
        # there — never to offer Start for a VM that does not exist (D31).
        self._action_setup = QAction("Finish setting up…", menu)
        self._action_setup.triggered.connect(self.show_window_requested.emit)
        self._action_setup.setVisible(False)
        menu.addAction(self._action_setup)

        # Start-at-login is a user setting, not an installer constant: this toggles
        # the XDG autostart entry's Hidden flag (v2 plan D29).
        self._action_autostart = QAction("Start when the computer starts", menu)
        self._action_autostart.setCheckable(True)
        self._action_autostart.toggled.connect(self._on_autostart_toggled)
        menu.addAction(self._action_autostart)

        menu.addSeparator()
        self._action_quit = QAction("Quit", menu)
        self._action_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(self._action_quit)

        self._menu = menu           # keep a reference; QMenu has no parent here
        # Reflect the on-disk autostart state each time the menu opens, so an
        # external edit or a fresh install shows through.
        menu.aboutToShow.connect(self._sync_autostart_check)
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._on_activated)

    # -- autostart checkbox ---------------------------------------------------

    def _sync_autostart_check(self) -> None:
        enabled = autostart.is_enabled()
        # Set without re-firing the toggle handler.
        self._action_autostart.blockSignals(True)
        self._action_autostart.setChecked(enabled)
        self._action_autostart.blockSignals(False)

    def _on_autostart_toggled(self, checked: bool) -> None:
        try:
            autostart.set_enabled(checked)
        except OSError as exc:
            self._icon.showMessage(
                "iCloud bridge",
                f"Could not update the autostart setting: {exc}",
                QSystemTrayIcon.MessageIcon.Warning)
            self._sync_autostart_check()   # revert to the true on-disk state

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window_requested.emit()

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def notify(self, title: str, message: str, *, level: str = notify_model.FAILURE) -> None:
        """Surface a message via a desktop notification.

        A recovery message must not carry a warning icon — it would read as a
        second fault — so the level selects Information instead.
        """
        icon = (QSystemTrayIcon.MessageIcon.Information if level == notify_model.RECOVERY
                else QSystemTrayIcon.MessageIcon.Warning)
        self._icon.showMessage(title, message, icon)

    def update_state(self, state: str, checks) -> None:
        self._icon.setIcon(load_icon(state))
        self._icon.setToolTip(tooltip_for(checks))

    def set_transition(self, state: str, tooltip: str) -> None:
        """Show a fixed transitional icon/tooltip (starting, shutting down, failed)."""
        self._icon.setIcon(load_icon(state))
        self._icon.setToolTip(tooltip)

    def set_power_action(self, action: str) -> None:
        """Show the one lifecycle action the current state allows (D30)."""
        self._action_power_off.setVisible(action == power.ACTION_POWER_OFF)
        self._action_start.setVisible(action == power.ACTION_START)
        self._action_retry.setVisible(action == power.ACTION_RETRY)
        self._action_setup.setVisible(action == power.ACTION_SETUP)

    def set_reprovision_available(self, available: bool) -> None:
        """Whether the guest-repair action is offered at all (D35/D40-D44)."""
        self._action_reprovision.setVisible(available)

    def set_lifecycle_busy(self, busy: bool, *, allow_quit: bool = False) -> None:
        """Disable the actions that must not run mid-transition; keep VM screen.

        ``allow_quit`` keeps Quit usable during the (interruptible) startup
        transition while a power-off transition disables it outright.
        """
        self._busy = busy
        self._action_autostart.setEnabled(not busy)
        self._action_quit.setEnabled(not busy or allow_quit)
        self._sync_mount_actions()
        if busy:
            # A transition owns the bridge; offer no way to start another.
            self.set_power_action(power.ACTION_NONE)
            self.set_reprovision_available(False)

    def set_bridge_available(self, available: bool) -> None:
        """Whether the mounts are up: gates the actions that touch CIFS (D30).

        Distinct from :meth:`set_lifecycle_busy` because an intentionally
        powered-off bridge is not busy — Quit, autostart and **Start bridge**
        all stay usable — but "Open iCloud folder" would open a bare mount point.
        """
        self._bridge_available = available
        self._sync_mount_actions()

    def _sync_mount_actions(self) -> None:
        self._action_open_files.setEnabled(self._bridge_available and not self._busy)
