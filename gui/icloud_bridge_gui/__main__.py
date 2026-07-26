"""Entry point: QApplication, tray, window and the single-instance lock.

    python3 -m icloud_bridge_gui [--minimized]

Two environment overrides exist so the app can be pointed at a fake bridge for
development and testing (v2 plan C6):

    ICLOUD_BRIDGE_DIR   default /mnt/icloud_bridge
    ICLOUD_MOUNT_DIR    default /mnt/icloud
"""

from __future__ import annotations

import os
import socket
import sys
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QSocketNotifier, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import cli, firstrun, health, notify, power
from .tray import OFF, STARTING, TrayIcon, load_icon
from .window import MainWindow

#: Abstract Unix socket (leading NUL): no filesystem path, no stale lock file.
SINGLE_INSTANCE_ADDRESS = "\0icloud-bridge-gui"
REFRESH_INTERVAL_MS = 5000

#: Shown while Windows installs itself (D31). Everything here is deliberately
#: manual: the Store/winget install needs an interactive session, Apple's 2FA
#: cannot be automated, and the share password must not pass through this app.
PROVISIONING_INTRO = (
    "The Windows VM is being created. The first install downloads Windows and "
    "takes 20–40 minutes — watch it on the VM screen.\n\n"
    "When the Windows desktop appears, run these in the guest, in order "
    "(they are in {provision}):\n"
    "  1. 02-install-icloud.ps1 — installs iCloud for Windows\n"
    "  2. sign in to iCloud in the guest, including two-factor authentication\n"
    "  3. 03-create-share.ps1 — creates the SMB share (edit the placeholder "
    "password first; it must match SHARE_PASS in your .env)\n"
    "  4. 04-bridge-agent.ps1 — installs the bridge agent, as Administrator\n\n"
    "Then, on this computer:\n"
    "  {host_command}\n\n"
    "Finally choose “Check setup and connect”. Nothing on the iCloud mounts is "
    "read until that succeeds."
)

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
        #: The D30 lifecycle state. Health colours never change it; only a
        #: transition, a definitive Docker classification, or a user action does.
        self._lifecycle = power.LIFECYCLE_STARTING
        #: The last definitive `docker inspect` classification, or None.
        self._container_state: str | None = None
        #: Long-lived polling caches. The five-second loop exists for the cheap
        #: file stats; these keep it from re-reading unchanged bridge documents
        #: over CIFS and from spawning `docker inspect` every tick. At most one
        #: gather worker is in flight at a time (`_refresh` defers a forced pass
        #: until the running one lands rather than dispatching a second), so the
        #: caches are only ever touched from that worker, and `invalidate()` runs
        #: on the GUI thread only while no gather is in flight.
        self._documents = health.DocumentCache()
        self._container_probe = health.ContainerProbe()
        #: The D30 classification is the other `docker inspect` consumer; it gets
        #: the same 15 s rate limit (D34) so a persistent red does not spawn an
        #: inspect per snapshot. `_classifying` keeps it to one worker in flight.
        self._classify_probe = health.ContainerProbe(probe=power.inspect_container)
        self._classifying = False
        #: A Refresh that arrives while a gather is in flight is honored after it
        #: lands instead of racing it (see `_refresh`).
        self._force_pending = False
        self._drain_timer: QTimer | None = None
        self._drain_ticks_left = 0
        #: Whether the power-off transition currently running should exit the app
        #: afterwards (Quit) or leave it idling (D30's keep-running power off).
        self._exit_after_power_off = True
        #: First-run assistant state (D31). All of it is Docker/filesystem facts
        #: about the *installation*; none of it involves a mount.
        self._bundle: firstrun.Bundle | None = None
        self._env_path = ""
        self._setup_checks: list[firstrun.Check] = []
        self._setup_detail = ""
        self._setup_busy = False
        #: Latching red-incident state for desktop notifications. Only the normal
        #: monitoring state feeds it; every transitional and intentional state
        #: resets it so an expected red never announces itself as a fault.
        self._incidents = notify.IncidentTracker()
        self._notify_enabled = False

        self._window = MainWindow(self.run_async)
        self._window.refresh_requested.connect(lambda: self._refresh(force=True))
        self._window.quit_requested.connect(self._on_quit_requested)
        self._window.retry_start_requested.connect(self._begin_startup)
        self._window.power_off_requested.connect(self._on_power_off_requested)
        self._window.start_requested.connect(self._on_start_requested)
        self._window.setup_recheck_requested.connect(self._run_setup_checks)
        self._window.create_vm_requested.connect(self._on_create_vm_requested)
        self._window.connect_requested.connect(self._on_connect_requested)
        self._window.env_file_selected.connect(self._on_env_file_selected)

        self._tray: TrayIcon | None = None
        if tray_available:
            self._tray = TrayIcon(self)
            self._tray.show_window_requested.connect(self.show_window)
            self._tray.quit_requested.connect(self._on_quit_requested)
            self._tray.retry_start_requested.connect(self._begin_startup)
            self._tray.power_off_requested.connect(self._on_power_off_requested)
            self._tray.start_requested.connect(self._on_start_requested)
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
            status = power.inspect_container()
            # Keep the classification, not just the plan: it is what decides
            # which power action the controls offer (D30).
            return status, power.plan_startup(power.marker_exists(), status)
        self.run_async(work, self._on_plan, self._on_plan_error)

    def _on_plan(self, result: tuple[power.DockerStatus, power.StartupPlan]) -> None:
        status, plan = result
        self._container_state = status.state
        if plan.kind == power.POWER_ON:
            self._begin_startup()
        elif plan.kind == power.PROVISION_NEEDED:
            # No container: the first-run assistant, with bridge I/O still
            # paused. There is nothing to mount, so nothing may be read (D31).
            self._enter_setup()
        elif plan.kind == power.INSPECT_ERROR:
            self._enter_setup(f"Cannot inspect the Windows VM: {plan.detail}")
        else:   # ALREADY_ON
            self._enter_monitoring()

    def _on_plan_error(self, message: str) -> None:
        self._enter_setup(f"Startup inspection failed: {message}")

    # -------------------------------------------------- first-run assistant --

    def _enter_setup(self, detail: str = "") -> None:
        """Setup required: no CIFS, no health polling, no selective sync (D31).

        Both entry reasons — no container at all, and an inspection we could not
        trust — have the same property: there is no evidence a mount exists, so
        touching one could block on a dead CIFS handle for the whole timeout.
        """
        self._lifecycle = power.LIFECYCLE_SETUP
        self._setup_detail = detail
        self._notify_enabled = False
        self._incidents.reset()
        self._timer.stop()
        self._window.quiesce()
        self._window.clear_health_rows()
        self._window.hide_banner()
        self._window.hide_notice()
        self._window.show_setup_tab()
        if self._tray is not None:
            self._tray.set_lifecycle_busy(False)
            self._tray.set_bridge_available(False)
            self._tray.set_transition(
                health.RED, "iCloud bridge: setup required — open the status window")
        self._sync_power_controls()
        if not self._minimized:
            self.show_window()
        self._run_setup_checks()

    def _run_setup_checks(self) -> None:
        """Re-run the readiness checks and the Docker inspection — nothing else."""
        if self._setup_busy:
            return
        self._setup_busy = True
        env_path = self._env_path

        def work():
            bundle = firstrun.resolve_bundle()
            chosen = env_path or self._default_env_path(bundle)
            report = firstrun.read_env_file(chosen) if chosen else None
            status = power.inspect_container()
            checks = firstrun.gather_checks(bundle=bundle, env=report,
                                            container_state=status.state)
            return bundle, chosen, checks, status

        def done(result) -> None:
            self._setup_busy = False
            bundle, chosen, checks, status = result
            self._bundle = bundle
            self._env_path = chosen
            self._container_state = status.state
            self._setup_checks = checks
            self._render_setup()
            self._sync_power_controls()

        def failed(message: str) -> None:
            self._setup_busy = False
            self._setup_detail = f"The readiness checks could not be run: {message}"
            self._render_setup()

        self.run_async(work, done, failed)

    @staticmethod
    def _default_env_path(bundle: firstrun.Bundle | None) -> str:
        """Pre-select the checkout's own `.env` when there is one to pre-select."""
        if bundle is None or not bundle.source_checkout or bundle.checkout_missing:
            return ""
        candidate = os.path.join(bundle.source_checkout, ".env")
        return candidate if os.path.exists(candidate) else ""

    def _render_setup(self) -> None:
        provisioning = self._lifecycle == power.LIFECYCLE_PROVISIONING
        bundle = self._bundle
        if bundle is None:
            paths = "Installation files: not found"
        else:
            paths = (f"Compose file: {bundle.compose_file}\n"
                     f"Provisioning scripts: {bundle.provision_dir}")
        if provisioning:
            title = "Provisioning Windows"
            intro = PROVISIONING_INTRO.format(
                provision=bundle.provision_dir if bundle else "the provision directory",
                host_command=self._host_setup_command())
        else:
            title = "Setup required"
            intro = ("There is no Windows VM yet. Work down these checks, then create "
                     "the VM. Nothing on the iCloud mounts is read until the bridge is "
                     "actually running.")
        self._window.update_setup(
            title=title, intro=intro, checks=self._setup_checks, paths=paths,
            env_path=self._env_path,
            can_create=(not provisioning
                        and firstrun.can_create_vm(self._setup_checks,
                                                   self._container_state or "")),
            show_connect=provisioning,
            detail=self._setup_detail, busy=self._setup_busy)

    def _host_setup_command(self) -> str:
        """The host-side command that matches how this GUI was installed."""
        bundle = self._bundle
        env = self._env_path or "./.env"
        if bundle is not None and bundle.origin == "package":
            return f'sudo icloud-bridge-configure --user "$USER" --env-file "{env}"'
        if bundle is not None and bundle.source_checkout and not bundle.checkout_missing:
            return f"cd {bundle.source_checkout} && sudo ./host/setup-host.sh"
        return "sudo ./host/setup-host.sh    # from your repository checkout"

    def _on_env_file_selected(self, path: str) -> None:
        self._env_path = path
        self._run_setup_checks()

    def _on_create_vm_requested(self) -> None:
        """D31's one mutating setup action, and only with a container absent."""
        if self._bundle is None or self._setup_busy:
            return
        if self._container_state != "absent":
            return          # never create beside an existing container
        if not firstrun.can_create_vm(self._setup_checks, self._container_state):
            return
        if not self._confirm_create_vm():
            return
        bundle, env_path = self._bundle, self._env_path
        self._setup_busy = True
        self._setup_detail = ("Creating the Windows VM… this downloads several GB and "
                              "can take a long time. You can watch progress on the VM "
                              "screen once it starts.")
        self._render_setup()

        def work():
            return firstrun.create_vm(bundle, env_path)

        def done(result) -> None:
            self._setup_busy = False
            ok, output = result
            if ok:
                self._enter_provisioning(output)
            else:
                self._setup_detail = f"docker compose up -d failed:\n{output}"
                self._render_setup()

        def failed(message: str) -> None:
            self._setup_busy = False
            self._setup_detail = f"Could not run docker compose: {message}"
            self._render_setup()

        self.run_async(work, done, failed)

    def _confirm_create_vm(self) -> bool:
        self.show_window()
        box = QMessageBox(self._window)
        box.setWindowTitle("Create the Windows VM?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Create the Windows VM now?")
        box.setInformativeText(
            "This downloads several gigabytes of Windows installation media and can take "
            "20–40 minutes, sometimes longer. It creates a long-lived virtual machine "
            "that keeps running until you power it off from this app.\n\n"
            f"Command: {' '.join(firstrun.compose_argv(self._bundle, self._env_path))}")
        create = box.addButton("Create Windows VM", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is create

    def _enter_provisioning(self, detail: str = "") -> None:
        """The container exists and Windows is installing itself.

        Emphatically **not** ``_begin_startup()``: the initial Windows install
        legitimately keeps SMB unavailable for far longer than the helper's
        five-minute readiness deadline, so calling `on` here would fail and leave
        the operator staring at a start error during a normal install (D31).
        """
        self._lifecycle = power.LIFECYCLE_PROVISIONING
        self._container_state = "running"
        self._setup_detail = detail
        self._timer.stop()
        self._window.quiesce()
        self._window.show_setup_tab()
        self._setup_checks = []
        self._render_setup()
        self._sync_power_controls()
        self.show_window()

    def _on_connect_requested(self) -> None:
        """Verify the host half is installed, then hand over to the power helper."""
        if self._setup_busy:
            return
        self._setup_busy = True
        self._setup_detail = "Checking the host setup…"
        self._render_setup()

        def work():
            status = power.inspect_container()
            return firstrun.check_host_setup(), status

        def done(result) -> None:
            self._setup_busy = False
            checks, status = result
            self._container_state = status.state
            self._setup_checks = checks + firstrun.check_container(status.state)
            if all(check.ok for check in self._setup_checks) and status.state != "absent":
                # The helper's own CIFS activation is the only honest
                # mountability test; nothing here touches a mount.
                self._setup_detail = ""
                self._window.hide_setup_tab()
                self._begin_startup()
                return
            self._setup_detail = ("The bridge is not ready to connect yet. Fix the "
                                  "items above, then check again.")
            self._render_setup()

        def failed(message: str) -> None:
            self._setup_busy = False
            self._setup_detail = f"The host setup check could not be run: {message}"
            self._render_setup()

        self.run_async(work, done, failed)

    def _begin_startup(self) -> None:
        """Run the power-on transition, showing the distinct 'starting' state.

        Reached from process start, from **Retry start**, and from D30's **Start
        bridge** — all three pause every kind of new I/O first, because CIFS
        must not be touched until the helper says both shares are live.
        """
        self._lifecycle = power.LIFECYCLE_STARTING
        self._notify_enabled = False
        self._incidents.reset()
        self._timer.stop()
        # quiesce() rather than set_io_paused(): it also stops the window's
        # request/response poller and drops queued list requests, which matters
        # when Start is pressed from the powered-off state.
        self._window.quiesce()
        self._window.show_banner(
            "Starting the Windows VM… this can take a few minutes.", "starting")
        self._sync_power_controls()
        if self._tray is not None:
            self._tray.set_transition(STARTING, "iCloud bridge: starting the Windows VM…")
            self._tray.set_lifecycle_busy(True, allow_quit=True)
            self._tray.set_bridge_available(False)
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
        """Both shares are live: resume every kind of monitoring and I/O."""
        self._lifecycle = power.LIFECYCLE_RUNNING
        self._container_state = "running"   # the helper just proved it
        self._window.hide_setup_tab()
        self._window.hide_banner()
        # resume() also restarts the request/response poller that quiesce stopped.
        self._window.resume()
        if self._tray is not None:
            self._tray.set_lifecycle_busy(False)
            self._tray.set_bridge_available(True)
        self._sync_power_controls()
        # The canary is legitimately as old as the bridge was off, so give the
        # host health timer a bounded window to refresh it before a red snapshot
        # counts as an incident (item 4).
        self._incidents.begin_startup_grace(time.monotonic())
        self._notify_enabled = True
        self._window.reload_selective_sync()
        self._timer.start()
        self._refresh(force=True)

    def _enter_start_failed(self, message: str) -> None:
        # Keep mount work paused and do NOT auto-retry every five seconds; wait
        # for the operator to fix the VM and press Retry (v2 plan D29).
        self._lifecycle = power.LIFECYCLE_START_FAILED
        self._notify_enabled = False
        self._incidents.reset()
        self._timer.stop()
        self._window.set_io_paused(True)
        self._window.show_banner(
            f"The Windows VM did not start.\n{message}\n\n"
            "Open the VM screen to check it, then Retry start.", "error")
        self._sync_power_controls()
        if self._tray is not None:
            self._tray.set_transition(health.RED, f"iCloud bridge: start failed — {message}")
            self._tray.set_lifecycle_busy(False)
            self._tray.set_bridge_available(False)
            self._sync_power_controls()     # set_lifecycle_busy cleared it
        if self._minimized and self._tray is not None:
            # A minimized autostart launch has no visible window; use a tray
            # notification rather than an invisible modal.
            self._tray.notify("iCloud bridge",
                              "The Windows VM did not start. Open the tray menu to retry.")
        else:
            self.show_window()

    def _enter_monitoring(self) -> None:
        """The bridge is up already (ALREADY_ON): resume normal monitoring."""
        self._lifecycle = power.LIFECYCLE_RUNNING
        self._incidents.reset()
        self._notify_enabled = True
        self._window.hide_setup_tab()
        self._window.resume()
        self._window.hide_banner()
        if self._tray is not None:
            self._tray.set_lifecycle_busy(False)
            self._tray.set_bridge_available(True)
        self._sync_power_controls()
        self._window.reload_selective_sync()
        self._timer.start()
        self._refresh(force=True)

    # ------------------------------------------------- the D30 power controls --

    def _sync_power_controls(self) -> None:
        """Offer the one lifecycle action this state allows — and only that one."""
        action = power.available_action(self._lifecycle, self._container_state)
        self._window.set_power_action(action)
        if self._tray is not None:
            self._tray.set_power_action(action)

    def _classify_container(self) -> None:
        """Refresh the Docker classification behind the power controls.

        Health going red is not evidence the bridge is off — it can equally mean
        a stale canary or bad JSON — so the Start action is enabled only by a
        definitive `docker inspect`. This is Docker-only and touches no mount,
        which is why it is safe in every state. The answer comes through a 15 s
        probe cache (D34): the container state only changes on a power action or
        a daemon crash, so a persistent red must not turn back into one inspect
        per five-second snapshot.
        """
        if self._classifying:
            return
        self._classifying = True

        def done(status: power.DockerStatus) -> None:
            self._classifying = False
            self._container_state = status.state
            self._sync_power_controls()

        def failed(_message: str) -> None:
            self._classifying = False
            # Unknown, not "stopped": leave the previous answer and offer nothing
            # new until an inspection actually succeeds.
            self._container_state = "error"
            self._sync_power_controls()

        self.run_async(self._classify_probe.read, done, failed)

    # ---------------------------------------------- power off, keep running ---

    def _on_power_off_requested(self) -> None:
        """D30's **Power off bridge**: the Quit transaction without the exit."""
        if self._lifecycle != power.LIFECYCLE_RUNNING:
            return
        if not self._confirm_power_off():
            return
        self._begin_power_off(then_exit=False)

    def _confirm_power_off(self) -> bool:
        self.show_window()
        box = QMessageBox(self._window)
        box.setWindowTitle("Power off the bridge?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Power off the iCloud bridge?")
        box.setInformativeText(
            "This stops syncing and disconnects /mnt/icloud and /mnt/icloud_bridge, "
            "then powers off the Windows VM. Changes not yet uploaded to iCloud will "
            "resume the next time the bridge starts — this cannot confirm the upload "
            "queue is empty. This app keeps running, and Start bridge brings "
            "everything back.")
        off_btn = box.addButton("Power off bridge", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        return box.clickedButton() is off_btn

    def _on_start_requested(self) -> None:
        """D30's **Start bridge**, from the powered-off or recoverable state."""
        if self._lifecycle == power.LIFECYCLE_POWERED_OFF:
            self._begin_startup()
            return
        if (self._lifecycle == power.LIFECYCLE_RUNNING
                and self._container_state == "stopped"):
            self._begin_startup()

    def _enter_powered_off(self) -> None:
        """Idle in-process after a successful power-off: no CIFS, no polling."""
        self._lifecycle = power.LIFECYCLE_POWERED_OFF
        self._container_state = "stopped"
        self._notify_enabled = False
        self._incidents.reset()
        self._timer.stop()
        # quiesce() already ran as part of the transaction; keep it that way.
        self._window.hide_notice()
        self._window.clear_health_rows()
        self._window.show_banner(
            "Bridge is powered off. The Windows VM is stopped and both shares are "
            "disconnected. Choose Start bridge to bring it back.", "off")
        if self._tray is not None:
            self._tray.set_lifecycle_busy(False)
            self._tray.set_bridge_available(False)
            self._tray.set_transition(
                OFF, "iCloud bridge: powered off — choose Start bridge to reconnect")
        self._sync_power_controls()

    # ----------------------------------------------------------- quit flow ---

    def _on_quit_requested(self) -> None:
        if self._lifecycle == power.LIFECYCLE_SHUTTING_DOWN:
            return
        if self._lifecycle == power.LIFECYCLE_POWERED_OFF:
            # Already off, durably: the marker and the stopped VM outlive this
            # process, so there is nothing left for the helper to do (D30).
            if self._confirm_simple_quit(
                    "The bridge is already powered off, so nothing more will be "
                    "disconnected. It stays off across a reboot; launching this app "
                    "again powers the VM back on."):
                self._quit_gui_only()
            return
        if self._lifecycle in (power.LIFECYCLE_SETUP, power.LIFECYCLE_PROVISIONING):
            # Nothing is mounted, and a half-installed Windows guest must not be
            # torn down by quitting the app that is guiding the install (D31).
            if self._confirm_simple_quit(
                    "Setup is not finished, so there is nothing mounted to "
                    "disconnect. Any VM that has already been created keeps "
                    "running; start this app again to continue."):
                self._quit_gui_only()
            return
        choice = self._ask_quit()
        if choice == "off":
            self._begin_power_off(then_exit=True)
        elif choice == "gui":
            self._quit_gui_only()

    def _confirm_simple_quit(self, informative: str) -> bool:
        """Quit confirmation for the states with nothing to tear down."""
        self.show_window()
        box = QMessageBox(self._window)
        box.setWindowTitle("Quit iCloud bridge")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Quit the iCloud bridge?")
        box.setInformativeText(informative)
        quit_btn = box.addButton("Quit", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(quit_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        return box.clickedButton() is quit_btn

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

    def _begin_power_off(self, *, then_exit: bool) -> None:
        """The one power-off transaction, with two possible continuations (D30).

        Quit and the keep-running power off share every step that matters — stop
        polling, refuse new bridge I/O, drain in-flight mount work, then call the
        helper — and differ only in what success means. Duplicating the ordering
        for the second caller is exactly how the two would drift apart.
        """
        self._lifecycle = power.LIFECYCLE_SHUTTING_DOWN
        self._exit_after_power_off = then_exit
        self._notify_enabled = False
        self._incidents.reset()
        self._timer.stop()
        # Stop scheduling bridge I/O and let in-flight work drain first.
        self._window.quiesce()
        self._window.show_banner(
            "Shutting down… this can take about three minutes. "
            "Do not power off your computer.", "shutdown")
        self._sync_power_controls()
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
                "and try again.")

    def _run_power_off(self) -> None:
        self.run_async(power.power_off, self._on_power_off_result, self._on_power_off_exception)

    def _on_power_off_result(self, result: power.HelperResult) -> None:
        if not result.success:
            self._abort_shutdown(result.message)
        elif self._exit_after_power_off:
            self._quit_gui_only()
        else:
            self._enter_powered_off()

    def _on_power_off_exception(self, message: str) -> None:
        self._abort_shutdown(f"the power helper could not be run: {message}")

    def _abort_shutdown(self, message: str) -> None:
        # Helper failure or a busy drain: nothing was torn down, so restore the
        # exact running state — polling, I/O, incident announcements and the
        # power controls all as they were.
        self._lifecycle = power.LIFECYCLE_RUNNING
        self._notify_enabled = True
        self._window.show_banner(message, "error")
        self._window.resume()
        if self._tray is not None:
            self._tray.set_lifecycle_busy(False)
            self._tray.set_bridge_available(True)
        self._sync_power_controls()
        self._timer.start()
        self._refresh(force=True)
        self.show_window()

    # ------------------------------------------------------------ refreshing --

    #: States that own the mounts or deliberately have none: no health polling,
    #: no CIFS, no bridge reads (v2 plan D29 lifecycle rule, extended by D30).
    _PAUSED_STATES = frozenset({
        power.LIFECYCLE_STARTING,
        power.LIFECYCLE_SHUTTING_DOWN,
        power.LIFECYCLE_POWERED_OFF,
        power.LIFECYCLE_START_FAILED,
        # D31: setup and provisioning have no mount to gather from, and a dead
        # CIFS handle would block the worker for the whole timeout.
        power.LIFECYCLE_SETUP,
        power.LIFECYCLE_PROVISIONING,
    })

    def _refresh(self, force: bool = False) -> None:
        # No health polling while a transition owns the tray/mount state, or
        # while the bridge is intentionally off.
        if self._lifecycle in self._PAUSED_STATES:
            return
        if self._refreshing:
            # Never dispatch a second concurrent gather — the caches are
            # single-worker by design. A forced pass runs once this one lands.
            if force:
                self._force_pending = True
            return
        # A forced refresh is either the Refresh button or the first pass after a
        # lifecycle transition, and in both cases the rate-limited container
        # checks must not answer from their caches.
        if force:
            self._container_probe.invalidate()
            self._classify_probe.invalidate()
        self._refreshing = True
        revision, written_at = self._window.last_write_info()
        self.run_async(
            lambda: health.gather(last_written_revision=revision, last_write_at=written_at,
                                  documents=self._documents, container=self._container_probe),
            self._on_snapshot,
            self._on_snapshot_failed,
        )

    def _on_snapshot(self, snapshot: health.Snapshot) -> None:
        self._refreshing = False
        if self._lifecycle in self._PAUSED_STATES:
            # A transition owns the state now; it does its own forced pass.
            self._force_pending = False
            return
        self._window.apply_snapshot(snapshot)
        if self._tray is not None:
            self._tray.update_state(snapshot.overall, snapshot.checks)
        self._announce(snapshot.overall, snapshot.checks)
        # Red is not evidence the bridge is off — reclassify Docker before the
        # power controls change (D30).
        if snapshot.overall != health.GREEN:
            self._classify_container()
        elif self._container_state != "running":
            self._container_state = "running"
            self._sync_power_controls()
        if self._force_pending:
            self._force_pending = False
            self._refresh(force=True)

    def _on_snapshot_failed(self, message: str) -> None:
        self._refreshing = False
        if self._lifecycle in self._PAUSED_STATES:
            self._force_pending = False
            return
        checks = [health.Check("GUI", health.RED, f"health check failed: {message}")]
        # The window must show the failure too: without a tray it is the only
        # surface, and with one the status tab would otherwise keep stale
        # all-green rows while the tray sits red.
        self._window.apply_snapshot(
            health.Snapshot(checks=checks, overall=health.RED, status=None, tree=None))
        if self._tray is not None:
            self._tray.update_state(health.RED, checks)
        # A gather exception is a red snapshot like any other: same latch, same
        # single notification.
        self._announce(health.RED, checks)
        self._classify_container()
        if self._force_pending:
            self._force_pending = False
            self._refresh(force=True)

    def _announce(self, overall: str, checks) -> None:
        """Raise a desktop notification when this snapshot opens/closes an incident."""
        if not self._notify_enabled:
            return
        message = self._incidents.observe(overall, checks, now=time.monotonic())
        if message is None:
            return
        if self._tray is not None:
            self._tray.notify(message.title, message.body, level=message.kind)
        elif message.kind == notify.FAILURE:
            # Without a tray the window is the only surface; it has its own
            # notice line so this cannot overwrite a lifecycle banner.
            self._window.show_notice(message.body)
        else:
            self._window.hide_notice()


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
    # Parse first: `--version`/`--help` exit here, before the single-instance
    # socket is claimed or a QApplication is constructed, so asking a running
    # tray instance's binary for its version never disturbs it.
    args = cli.build_parser().parse_args(argv)

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
