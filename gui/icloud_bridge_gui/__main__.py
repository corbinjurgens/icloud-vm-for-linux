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
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import health, power
from .tray import STARTING, TrayIcon, load_icon
from .window import MainWindow

#: Abstract Unix socket (leading NUL): no filesystem path, no stale lock file.
SINGLE_INSTANCE_ADDRESS = "\0icloud-bridge-gui"
REFRESH_INTERVAL_MS = 5000

#: The shutdown work-drain gate waits a little longer than the CIFS 30 s timeout
#: for in-flight mount-touching tasks before it gives up and refuses to unmount.
SHUTDOWN_DRAIN_TIMEOUT_MS = 40000
DRAIN_POLL_MS = 250


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
        self._minimized = minimized
        #: Live worker tasks. A QRunnable's signals object is destroyed with the
        #: Python wrapper, so we must keep a reference until its queued done/failed
        #: slot has fired, or the completion callback is silently dropped.
        self._tasks: set[_Task] = set()
        #: Count of worker tasks in flight, so the shutdown gate can wait for
        #: mount-touching work to drain before it lets the helper unmount.
        self._active = 0
        self._starting = False
        self._shutting_down = False
        self._drain_timer: QTimer | None = None
        self._drain_ticks_left = 0

        self._window = MainWindow(self.run_async)
        self._window.refresh_requested.connect(lambda: self._refresh(force=True))
        self._window.quit_requested.connect(self._on_quit_requested)
        self._window.retry_start_requested.connect(self._begin_startup)

        self._tray: TrayIcon | None = None
        if tray_available:
            self._tray = TrayIcon(self)
            self._tray.show_window_requested.connect(self.show_window)
            self._tray.quit_requested.connect(self._on_quit_requested)
            self._tray.retry_start_requested.connect(self._begin_startup)
            self._tray.show()
            self._window.hide_on_close = True

        # The controller owns every exit path (v2 plan D29): OS session logout,
        # a stray last-window-close, or aboutToQuit must never power off the
        # bridge or bypass the confirmation. Only the explicit action does.
        app.setQuitOnLastWindowClosed(False)

        if not minimized:
            self.show_window()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)

        # Startup must precede any CIFS access: pause bridge I/O until the power
        # inspection (and, if needed, the power-on transition) completes.
        self._window.set_io_paused(True)
        self._window.show_banner("Checking the Windows VM…", "starting")
        self._inspect_and_start()

    # ------------------------------------------------------------------------

    def run_async(self, work: Callable[[], Any],
                  on_done: Callable[[Any], None],
                  on_error: Callable[[str], None] | None = None) -> None:
        self._active += 1
        task = _Task(work)
        self._tasks.add(task)

        def done(result: Any) -> None:
            self._active -= 1
            self._tasks.discard(task)
            on_done(result)

        def failed(message: str) -> None:
            self._active -= 1
            self._tasks.discard(task)
            if on_error is not None:
                on_error(message)

        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        self._pool.start(task)

    def show_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    # --------------------------------------------------------- startup flow --

    def _inspect_and_start(self) -> None:
        """Decide, off the GUI thread, whether startup must power the bridge on."""
        def work():
            return power.plan_startup(power.marker_exists(), power.inspect_container())
        self.run_async(work, self._on_plan, self._on_plan_error)

    def _on_plan(self, plan: power.StartupPlan) -> None:
        if plan.kind == power.POWER_ON:
            self._begin_startup()
        elif plan.kind == power.PROVISION_NEEDED:
            self._enter_monitoring(
                "No Windows VM exists yet. Create it once with "
                "'docker compose up -d' (first-time provisioning); the status below "
                "is read-only until it does.", "error")
        elif plan.kind == power.INSPECT_ERROR:
            self._enter_monitoring(f"Cannot inspect the Windows VM: {plan.detail}", "error")
        else:   # ALREADY_ON
            self._enter_monitoring()

    def _on_plan_error(self, message: str) -> None:
        self._enter_monitoring(f"Startup inspection failed: {message}", "error")

    def _begin_startup(self) -> None:
        """Run the power-on transition, showing the distinct 'starting' state."""
        self._starting = True
        self._timer.stop()
        self._window.set_io_paused(True)
        self._window.show_retry_start(False)
        self._window.show_banner(
            "Starting the Windows VM… this can take a few minutes.", "starting")
        if self._tray is not None:
            self._tray.set_transition(STARTING, "iCloud bridge: starting the Windows VM…")
            self._tray.set_lifecycle_busy(True, allow_quit=True)
        if not self._minimized:
            self.show_window()
        self.run_async(power.power_on, self._on_start_result, self._on_start_exception)

    def _on_start_result(self, result: power.HelperResult) -> None:
        if result.success:
            self._enter_running()
        else:
            self._enter_start_failed(result.message)

    def _on_start_exception(self, message: str) -> None:
        self._enter_start_failed(f"the power helper could not be run: {message}")

    def _enter_running(self) -> None:
        self._starting = False
        self._window.hide_banner()
        self._window.set_io_paused(False)
        self._window.show_retry_start(False)
        if self._tray is not None:
            self._tray.set_lifecycle_busy(False)
            self._tray.show_retry(False)
        self._window.reload_selective_sync()
        self._timer.start()
        self._refresh()

    def _enter_start_failed(self, message: str) -> None:
        # Keep mount work paused and do NOT auto-retry every five seconds; wait
        # for the operator to fix the VM and press Retry (v2 plan D29).
        self._starting = False
        self._timer.stop()
        self._window.set_io_paused(True)
        self._window.show_banner(
            f"The Windows VM did not start.\n{message}\n\n"
            "Open the VM screen to check it, then Retry start.", "error")
        self._window.show_retry_start(True)
        if self._tray is not None:
            self._tray.set_transition(health.RED, f"iCloud bridge: start failed — {message}")
            self._tray.set_lifecycle_busy(False)
            self._tray.show_retry(True)
        if self._minimized and self._tray is not None:
            # A minimized autostart launch has no visible window; use a tray
            # notification rather than an invisible modal.
            self._tray.notify("iCloud bridge",
                              "The Windows VM did not start. Open the tray menu to retry.")
        else:
            self.show_window()

    def _enter_monitoring(self, note: str | None = None, banner_kind: str = "starting") -> None:
        """The bridge is (or should be) up already: resume normal monitoring."""
        self._starting = False
        self._window.set_io_paused(False)
        self._window.show_retry_start(False)
        if note:
            self._window.show_banner(note, banner_kind)
        else:
            self._window.hide_banner()
        if self._tray is not None:
            self._tray.set_lifecycle_busy(False)
            self._tray.show_retry(False)
        self._window.reload_selective_sync()
        self._timer.start()
        self._refresh()

    # ----------------------------------------------------------- quit flow ---

    def _on_quit_requested(self) -> None:
        if self._shutting_down:
            return
        choice = self._ask_quit()
        if choice == "off":
            self._begin_shutdown()
        elif choice == "gui":
            self._quit_gui_only()

    def _ask_quit(self) -> str:
        """The three-way Quit confirmation; returns 'off', 'gui', or 'cancel'."""
        self.show_window()
        box = QMessageBox(self._window)
        box.setWindowTitle("Quit iCloud bridge")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Quit the iCloud bridge?")
        box.setInformativeText(
            "Quitting stops syncing and disconnects /mnt/icloud and "
            "/mnt/icloud_bridge. Changes not yet uploaded to iCloud will resume "
            "the next time the bridge starts — this cannot confirm the upload "
            "queue is empty. Starting the app again powers the VM back on.")
        off_btn = box.addButton("Quit and power off VM", QMessageBox.ButtonRole.AcceptRole)
        gui_btn = box.addButton("Quit GUI only (leave bridge running)",
                                QMessageBox.ButtonRole.ActionRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(off_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is off_btn:
            return "off"
        if clicked is gui_btn:
            return "gui"
        return "cancel"

    def _quit_gui_only(self) -> None:
        if self._tray is not None:
            self._tray.hide()
        self._app.quit()

    def _begin_shutdown(self) -> None:
        self._shutting_down = True
        self._timer.stop()
        # Stop scheduling bridge I/O and let in-flight work drain first.
        self._window.quiesce()
        self._window.show_banner(
            "Shutting down… this can take about three minutes. "
            "Do not power off your computer.", "shutdown")
        if self._tray is not None:
            self._tray.set_transition(STARTING, "iCloud bridge: shutting down…")
            self._tray.set_lifecycle_busy(True, allow_quit=False)
        self.show_window()

        self._drain_ticks_left = SHUTDOWN_DRAIN_TIMEOUT_MS // DRAIN_POLL_MS
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(DRAIN_POLL_MS)
        self._drain_timer.timeout.connect(self._check_drain)
        self._drain_timer.start()
        self._check_drain()

    def _check_drain(self) -> None:
        # Never begin the unmount while a task (Apply write, gather, or list poll)
        # is still touching CIFS.
        if self._active == 0 and not self._window.apply_in_flight():
            if self._drain_timer is not None:
                self._drain_timer.stop()
            self._run_power_off()
            return
        self._drain_ticks_left -= 1
        if self._drain_ticks_left <= 0:
            if self._drain_timer is not None:
                self._drain_timer.stop()
            self._abort_shutdown(
                "A file operation on the iCloud mount is still in progress, so the "
                "bridge was not disconnected. Close any open files or transfers "
                "and try Quit again.")

    def _run_power_off(self) -> None:
        self.run_async(power.power_off, self._on_power_off_result, self._on_power_off_exception)

    def _on_power_off_result(self, result: power.HelperResult) -> None:
        if result.success:
            self._quit_gui_only()
        else:
            self._abort_shutdown(result.message)

    def _on_power_off_exception(self, message: str) -> None:
        self._abort_shutdown(f"the power helper could not be run: {message}")

    def _abort_shutdown(self, message: str) -> None:
        self._shutting_down = False
        self._window.show_banner(message, "error")
        self._window.resume()
        if self._tray is not None:
            self._tray.set_lifecycle_busy(False)
        self._timer.start()
        self._refresh()
        self.show_window()

    # ------------------------------------------------------------ refreshing --

    def _refresh(self, force: bool = False) -> None:
        # No health polling while a transition owns the tray/mount state.
        if self._starting or self._shutting_down:
            return
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
        if self._starting or self._shutting_down:
            return
        self._window.apply_snapshot(snapshot)
        if self._tray is not None:
            self._tray.update_state(snapshot.overall, snapshot.checks)

    def _on_snapshot_failed(self, message: str) -> None:
        self._refreshing = False
        if self._starting or self._shutting_down:
            return
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
