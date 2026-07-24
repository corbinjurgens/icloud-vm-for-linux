"""Entry point: QApplication, tray, window and the single-instance lock.

    python3 -m icloud_bridge_gui [--minimized]

Two environment overrides exist so the app can be pointed at a fake bridge for
development and testing (v2 plan C6):

    ICLOUD_BRIDGE_DIR   default /mnt/icloud_bridge
    ICLOUD_MOUNT_DIR    default /mnt/icloud
"""

from __future__ import annotations

import argparse
import socket
import sys
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QSocketNotifier, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import health
from .tray import TrayIcon, load_icon
from .window import MainWindow

#: Abstract Unix socket (leading NUL): no filesystem path, no stale lock file.
SINGLE_INSTANCE_ADDRESS = "\0icloud-bridge-gui"
REFRESH_INTERVAL_MS = 5000


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    """Run one callable off the GUI thread and deliver the result by signal."""

    def __init__(self, work: Callable[[], Any]) -> None:
        super().__init__()
        self._work = work
        self.signals = _TaskSignals()

    def run(self) -> None:   # pragma: no cover - thread body
        try:
            result = self._work()
        except Exception as exc:                      # noqa: BLE001 - reported to the UI
            self.signals.failed.emit(str(exc))
            return
        self.signals.done.emit(result)


class Application(QObject):
    def __init__(self, app: QApplication, *, minimized: bool, tray_available: bool) -> None:
        super().__init__()
        self._app = app
        self._pool = QThreadPool.globalInstance()
        self._refreshing = False

        self._window = MainWindow(self.run_async)
        self._window.refresh_requested.connect(lambda: self._refresh(force=True))

        self._tray: TrayIcon | None = None
        if tray_available:
            self._tray = TrayIcon(self)
            self._tray.show_window_requested.connect(self.show_window)
            self._tray.quit_requested.connect(self._quit)
            self._tray.show()
            self._window.hide_on_close = True
            app.setQuitOnLastWindowClosed(False)
        else:
            # Without a tray the window is the only way back in, so closing it
            # must end the process rather than orphan it.
            app.setQuitOnLastWindowClosed(True)

        if not minimized:
            self.show_window()

        self._window.reload_selective_sync()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    # ------------------------------------------------------------------------

    def run_async(self, work: Callable[[], Any],
                  on_done: Callable[[Any], None],
                  on_error: Callable[[str], None] | None = None) -> None:
        task = _Task(work)
        task.signals.done.connect(on_done)
        if on_error is not None:
            task.signals.failed.connect(on_error)
        self._pool.start(task)

    def show_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _quit(self) -> None:
        if self._tray is not None:
            self._tray.hide()
        self._app.quit()

    def _refresh(self, force: bool = False) -> None:
        if self._refreshing and not force:
            return
        self._refreshing = True
        revision, written_at = self._window.last_write_info()
        self.run_async(
            lambda: health.gather(last_written_revision=revision, last_write_at=written_at),
            self._on_snapshot,
            self._on_snapshot_failed,
        )

    def _on_snapshot(self, snapshot: health.Snapshot) -> None:
        self._refreshing = False
        self._window.apply_snapshot(snapshot)
        if self._tray is not None:
            self._tray.update_state(snapshot.overall, snapshot.checks)

    def _on_snapshot_failed(self, message: str) -> None:
        self._refreshing = False
        checks = [health.Check("GUI", health.RED, f"health check failed: {message}")]
        # The window must show the failure too: without a tray it is the only
        # surface, and with one the status tab would otherwise keep stale
        # all-green rows while the tray sits red.
        self._window.apply_snapshot(
            health.Snapshot(checks=checks, overall=health.RED, status=None, tree=None))
        if self._tray is not None:
            self._tray.update_state(health.RED, checks)


def _claim_single_instance() -> socket.socket | None:
    """Bind the abstract socket, or return ``None`` if another instance owns it."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(SINGLE_INSTANCE_ADDRESS)
    except OSError:
        listener.close()
        return None
    listener.listen(8)
    listener.setblocking(False)
    return listener


def _signal_primary(minimized: bool) -> bool:
    """Ask the running instance to surface. True when it was reached."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(2.0)
        client.connect(SINGLE_INSTANCE_ADDRESS)
        if not minimized:
            client.sendall(b"show\n")
        return True
    except OSError:
        return False
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="icloud-bridge-gui",
                                     description="iCloud bridge status and selective sync")
    parser.add_argument("--minimized", action="store_true",
                        help="start in the tray without showing the window (autostart)")
    args = parser.parse_args(argv)

    listener = _claim_single_instance()
    if listener is None:
        # A second launch is a request to surface the window, not a new process.
        if _signal_primary(args.minimized):
            return 0
        print("icloud-bridge-gui: another instance holds the lock but is not answering",
              file=sys.stderr)
        return 1

    app = QApplication(sys.argv[:1])
    app.setApplicationName("iCloud bridge")
    app.setDesktopFileName("icloud-bridge-gui")
    app.setWindowIcon(load_icon(health.GREEN))

    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    if not tray_available and args.minimized:
        print("icloud-bridge-gui: no system tray is available, so --minimized would "
              "start an invisible process. On GNOME, install the 'AppIndicator and "
              "KStatusNotifierItem Support' extension, then log back in.", file=sys.stderr)
        return 1

    controller = Application(app, minimized=args.minimized, tray_available=tray_available)

    notifier = QSocketNotifier(listener.fileno(), QSocketNotifier.Type.Read)

    def _on_connection(_fd: int) -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            try:
                connection.settimeout(1.0)
                data = connection.recv(64)
            except OSError:
                return
        if b"show" in data:
            controller.show_window()

    notifier.activated.connect(_on_connection)

    try:
        return app.exec()
    finally:
        notifier.setEnabled(False)
        listener.close()


if __name__ == "__main__":
    sys.exit(main())
