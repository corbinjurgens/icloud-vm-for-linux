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
from datetime import datetime, timezone
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QSocketNotifier, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import (autostart, backup, bridge, cli, diagnostics, firstrun, health,
               lifecycle, notify, power)
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

#: D38 busy surfaces: the base wording each long operation shows above its
#: elapsed clock and, where there is one, the helper's last `==> ` phase line.
BUSY_STARTING = "starting"
BUSY_SHUTDOWN = "shutdown"
BUSY_CREATING = "creating"
BUSY_PROVISIONING = "provisioning"

BUSY_TEXT = {
    BUSY_STARTING: "Starting the Windows VM…",
    BUSY_SHUTDOWN: ("Shutting down… Do not power off your computer."),
    BUSY_CREATING: ("Creating the Windows VM… this downloads several GB. "
                    "Watch progress on the VM screen once it starts."),
    BUSY_PROVISIONING: "Windows has been installing for",
}
BUSY_BANNER_KIND = {
    BUSY_STARTING: "starting",
    BUSY_SHUTDOWN: "shutdown",
    BUSY_CREATING: "starting",
    BUSY_PROVISIONING: "starting",
}

#: The shutdown work-drain gate waits a little longer than the CIFS 30 s timeout
#: for in-flight mount-touching tasks before it gives up and refuses to unmount.
SHUTDOWN_DRAIN_TIMEOUT_MS = 40000
DRAIN_POLL_MS = 250


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    #: One sanitized output line from a streaming child (v2 plan D38). A signal
    #: rather than a direct call because the worker (or one of its reader
    #: threads) emits it and a widget may only be touched on the GUI thread.
    progress = Signal(str)


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
        #: The D30 lifecycle state, owned by the pure reducer in `lifecycle.py`.
        #: Health colours never change it; only a transition, a definitive Docker
        #: classification, or a user action does. This controller is the loop
        #: around it: translate signal to event, `reduce`, apply effects in order.
        self._model = lifecycle.Model()
        #: Messages that belong to the *next* banner an effect will draw. Effects
        #: are parameterless tokens, so the text the reducer has no business
        #: knowing about is staged here immediately before the dispatch.
        self._start_error = ""
        self._abort_message = ""
        #: Bounded record of unexpected (phase, event) pairs, so a wiring mistake
        #: is diagnosable rather than silent.
        self._invalid_transitions: list[str] = []
        #: D37 report inputs the controller must retain deliberately, because a
        #: report has to work in the states where nothing can be re-read: the
        #: last helper outcome, when bridge facts were last gathered, and the
        #: marker. None of these is reconstructed from a journal.
        self._last_helper_action = ""
        self._last_helper_ok: bool | None = None
        self._last_helper_detail = ""
        self._last_gathered_at = ""
        self._last_snapshot: health.Snapshot | None = None
        self._marker_present: bool | None = None
        #: D38 progress presentation. `_phase_line` is the last `==> ` line the
        #: helper printed, `_busy_since` the monotonic start of the current busy
        #: surface, and `_busy_timer` ticks the elapsed clock. All presentation:
        #: no control decision reads any of them.
        self._phase_line = ""
        self._busy_since: float | None = None
        self._busy_kind = ""
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(1000)
        self._busy_timer.timeout.connect(self._tick_busy)
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
        #: First-run assistant state (D31). All of it is Docker/filesystem facts
        #: about the *installation*; none of it involves a mount.
        self._bundle: firstrun.Bundle | None = None
        self._env_path = ""
        self._setup_checks: list[firstrun.Check] = []
        self._setup_detail = ""
        self._setup_busy = False
        #: D39 interrupted-provisioning record state. `_record` is the saved
        #: record (or None) and `_record_state` its classification against what
        #: Docker reports; the discard action is offered only for a *proved*
        #: absent or different container.
        self._record: firstrun.ProvisioningRecord | None = None
        self._record_state = firstrun.RECORD_ABSENT
        #: Latching red-incident state for desktop notifications. Only the normal
        #: monitoring state feeds it; every transitional and intentional state
        #: resets it so an expected red never announces itself as a fault.
        self._incidents = notify.IncidentTracker()
        self._notify_enabled = False

        self._window = MainWindow(self.run_async)
        self._window.refresh_requested.connect(lambda: self._refresh(force=True))
        self._window.quit_requested.connect(self._on_quit_requested)
        self._window.retry_start_requested.connect(self._on_retry_start_requested)
        self._window.power_off_requested.connect(self._on_power_off_requested)
        self._window.start_requested.connect(self._on_start_requested)
        self._window.setup_recheck_requested.connect(self._run_setup_checks)
        self._window.create_vm_requested.connect(self._on_create_vm_requested)
        self._window.connect_requested.connect(self._on_connect_requested)
        self._window.env_file_selected.connect(self._on_env_file_selected)
        self._window.discard_record_requested.connect(self._on_discard_record)
        self._window.diagnostics_facts = self._diagnostic_facts

        self._tray: TrayIcon | None = None
        if tray_available:
            self._tray = TrayIcon(self)
            self._tray.show_window_requested.connect(self.show_window)
            self._tray.quit_requested.connect(self._on_quit_requested)
            self._tray.retry_start_requested.connect(self._on_retry_start_requested)
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
                  on_error: Callable[[str], None] | None = None,
                  on_progress: Callable[[str], None] | None = None) -> None:
        """Run ``work`` off the GUI thread.

        ``on_progress`` receives streamed output lines **on the GUI thread**;
        ``work`` emits them through ``task.signals.progress`` rather than
        touching a widget itself.
        """
        self._start_task(_Task(work), on_done, on_error, on_progress)

    def _start_task(self, task: _Task,
                    on_done: Callable[[Any], None],
                    on_error: Callable[[str], None] | None = None,
                    on_progress: Callable[[str], None] | None = None) -> None:
        """Track, wire and dispatch a task that has already been constructed."""
        self._active += 1
        self._tasks.add(task)
        if on_progress is not None:
            task.signals.progress.connect(on_progress)

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

    # -------------------------------------------------------- the state loop --

    def _dispatch(self, event: lifecycle.Event, token: int | None = None) -> None:
        """Reduce one event and apply the resulting effects, in order.

        ``token`` is the operation token a worker captured when it was
        dispatched. A completion whose token no longer matches belongs to an
        operation something else has already superseded, and is dropped *before*
        reduction so it cannot resurrect a state the user has left.
        """
        if token is not None and not lifecycle.accepts(self._model, token):
            return
        transition = lifecycle.reduce(self._model, event)
        # The model is current before the effects run, so an effect that starts
        # an operation captures the token that operation must report back with.
        self._model = transition.model
        for effect in transition.effects:
            self._EFFECTS[effect](self)

    def _report_invalid_transition(self) -> None:
        record = f"{self._model.phase.value}: unexpected event"
        if len(self._invalid_transitions) < 32:
            self._invalid_transitions.append(record)
        print(f"icloud-bridge-gui: {record}", file=sys.stderr)

    # ------------------------------------------- the D38 elapsed-time clock --

    def _begin_busy(self, kind: str) -> None:
        """Start (or restart) the elapsed clock for one long operation."""
        self._busy_kind = kind
        self._busy_since = time.monotonic()
        self._phase_line = ""
        self._busy_timer.start()
        self._refresh_busy_text()

    def _end_busy(self) -> None:
        self._busy_timer.stop()
        self._busy_since = None
        self._busy_kind = ""
        self._phase_line = ""

    def _tick_busy(self) -> None:
        if self._busy_since is None:
            self._busy_timer.stop()
            return
        self._refresh_busy_text()

    @staticmethod
    def _elapsed(seconds: float) -> str:
        minutes, secs = divmod(int(max(0.0, seconds)), 60)
        return f"{minutes} m {secs:02d} s" if minutes else f"{secs} s"

    def _busy_text(self) -> str:
        """The banner for the current busy surface: base, elapsed, last phase.

        No percentage and no estimate: the helper cannot know how long a Windows
        boot or an SMB activation will take, and a fake bar would be a lie (D38).
        """
        base = BUSY_TEXT.get(self._busy_kind, "")
        if self._busy_since is not None:
            base = f"{base} {self._elapsed(time.monotonic() - self._busy_since)}"
        if self._phase_line:
            base = f"{base}\n{self._phase_line}"
        return base

    def _refresh_busy_text(self) -> None:
        if not self._busy_kind:
            return
        if self._busy_kind == BUSY_PROVISIONING:
            # No long-lived subprocess here: Compose returned long ago and
            # Windows is installing itself. Elapsed time is all we honestly have.
            self._render_setup()
            return
        self._window.show_banner(self._busy_text(), BUSY_BANNER_KIND[self._busy_kind])

    # ------------------------------------------------ the D37 report inputs --

    def _diagnostic_facts(self) -> diagnostics.Facts:
        """Everything a report may know, copied in explicitly.

        Built from state this controller already holds, so it works unchanged in
        the no-CIFS states: nothing here re-reads the bridge, and `gathered_at`
        is what tells the reader the bridge section is cached rather than
        current. Anything not named here cannot reach the report.
        """
        snapshot = self._last_snapshot
        status = (snapshot.status if snapshot is not None else None) or {}
        tree = (snapshot.tree if snapshot is not None else None) or {}
        compatibility = (snapshot.compatibility if snapshot is not None
                         else bridge.Compatibility())
        revision, paths = self._window.selection_facts()
        try:
            autostart_enabled = autostart.is_enabled()
        except OSError:                     # pragma: no cover - defensive
            autostart_enabled = None

        return diagnostics.Facts(
            lifecycle=self._model.phase.value,
            container_state=self._container_state or "",
            marker_present=self._marker_present,
            install_origin=(self._bundle.origin if self._bundle is not None else ""),
            autostart_enabled=autostart_enabled,
            compatibility=compatibility.state,
            compatibility_detail=compatibility.detail,
            documents=diagnostics.DocumentFacts(
                status_version=status.get("version"),
                status_generated_at=str(status.get("generatedAt") or ""),
                status_applied_revision=status.get("appliedRevision"),
                agent_build=(compatibility.agent_build
                             if compatibility.agent_build is not None
                             else status.get("agentBuild")),
                tree_version=tree.get("version"),
                tree_generated_at=str(tree.get("generatedAt") or ""),
                exclusions_revision=revision,
                exclusions_count=len(paths),
            ),
            health=tuple(diagnostics.HealthRow(check.name, check.severity)
                         for check in (snapshot.checks if snapshot is not None else ())),
            overall=(snapshot.overall if snapshot is not None else ""),
            gathered_at=self._last_gathered_at,
            last_helper_action=self._last_helper_action,
            last_helper_ok=self._last_helper_ok,
            last_helper_detail=self._last_helper_detail,
            exclusion_paths=paths,
        )

    # --------------------------------------------------------- startup flow --

    def _inspect_and_start(self) -> None:
        """Decide, off the GUI thread, whether startup must power the bridge on."""
        token = self._model.token

        def work():
            status = power.inspect_container()
            marker = power.marker_exists()
            # The bundle is local filesystem work, not CIFS, and resolving it
            # once here means a diagnostic report can name the install origin in
            # every state rather than only during first-run setup (D37).
            bundle = firstrun.resolve_bundle()
            # D39: the record and Docker are both consulted *before* any CIFS
            # access, so an app that was closed mid-install resumes the no-CIFS
            # Provisioning state instead of trying to mount a half-built guest.
            record: firstrun.ProvisioningRecord | None = None
            record_error = ""
            try:
                record = firstrun.read_provisioning_record()
            except backup.BackupError as exc:
                record_error = str(exc)
            container_id = (firstrun.inspect_container_id()
                            if record is not None and record.container_id else "")
            record_state = firstrun.classify_record(record, status.state, container_id)
            if record_error:
                record_state = firstrun.RECORD_MALFORMED
            # Keep the classification, not just the plan: it is what decides
            # which power action the controls offer (D30).
            return (status, power.plan_startup(marker, status), marker, bundle,
                    record, record_state, record_error)

        self.run_async(work,
                       lambda result: self._on_plan(result, token),
                       lambda message: self._on_plan_error(message, token))

    def _on_plan(self, result, token: int) -> None:
        if not lifecycle.accepts(self._model, token):
            return
        status, plan, marker, bundle, record, record_state, record_error = result
        self._container_state = status.state
        self._marker_present = marker
        self._bundle = bundle
        self._record = record
        self._record_state = record_state

        # D39 wins over the ordinary startup plan, because it is the only thing
        # that knows a running container may be a *half-provisioned* one.
        if record_state == firstrun.RECORD_MATCHES:
            self._setup_detail = (
                "Setup was interrupted while Windows was installing. Nothing on "
                "the iCloud shares has been read. Continue the guest steps below, "
                "then choose Check setup and connect.")
            self._dispatch(lifecycle.Event.STARTUP_RESUME_PROVISIONING)
            return
        if record_state == firstrun.RECORD_CONTAINER_GONE:
            self._setup_detail = (
                "A VM creation was started but no container exists now. Check the "
                "settings below and create it again, or discard this note.")
            self._dispatch(lifecycle.Event.STARTUP_PROVISION_NEEDED)
            return
        if record_state == firstrun.RECORD_DIFFERENT:
            self._setup_detail = (
                "A VM creation was started earlier, but the container using that "
                "name now is a different one. Nothing on the iCloud shares has "
                "been read. Check the settings below, or discard this note.")
            self._dispatch(lifecycle.Event.STARTUP_INSPECT_FAILED)
            return
        if record_state == firstrun.RECORD_MALFORMED:
            self._setup_detail = (
                f"This app's record of an interrupted setup could not be read "
                f"({record_error}). It has been left alone. Work through the "
                "checks below.")
            self._dispatch(lifecycle.Event.STARTUP_INSPECT_FAILED)
            return

        if plan.kind == power.POWER_ON:
            self._dispatch(lifecycle.Event.STARTUP_POWER_ON)
        elif plan.kind == power.PROVISION_NEEDED:
            # No container: the first-run assistant, with bridge I/O still
            # paused. There is nothing to mount, so nothing may be read (D31).
            self._setup_detail = ""
            self._dispatch(lifecycle.Event.STARTUP_PROVISION_NEEDED)
        elif plan.kind == power.INSPECT_ERROR:
            self._setup_detail = f"Cannot inspect the Windows VM: {plan.detail}"
            self._dispatch(lifecycle.Event.STARTUP_INSPECT_FAILED)
        else:   # ALREADY_ON
            self._dispatch(lifecycle.Event.STARTUP_ALREADY_ON)

    def _on_plan_error(self, message: str, token: int) -> None:
        if not lifecycle.accepts(self._model, token):
            return
        self._setup_detail = f"Startup inspection failed: {message}"
        self._dispatch(lifecycle.Event.STARTUP_INSPECT_FAILED)

    # -------------------------------------------------- first-run assistant --

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

    def _clear_provisioning_record(self) -> None:
        try:
            firstrun.clear_provisioning_record()
        except backup.BackupError:
            pass            # a stale record is a nuisance, never a failure here
        self._record = None
        self._record_state = firstrun.RECORD_ABSENT

    def _on_discard_record(self) -> None:
        """Forget a record Docker has disproved. Deletes nothing else (D39)."""
        if self._record_state not in (firstrun.RECORD_CONTAINER_GONE,
                                      firstrun.RECORD_DIFFERENT):
            return
        if not self._confirm_discard_record():
            return
        self._clear_provisioning_record()
        self._setup_detail = "The interrupted-setup note was discarded."
        self._run_setup_checks()

    def _confirm_discard_record(self) -> bool:
        self.show_window()
        box = QMessageBox(self._window)
        box.setWindowTitle("Discard the setup record?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Forget this app's note about an interrupted setup?")
        box.setInformativeText(
            "This removes a small file this app wrote to remember that a VM "
            "creation was started. It does not delete a container, a virtual "
            "disk, your .env file, or anything in iCloud.")
        discard = box.addButton("Discard note", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is discard

    @staticmethod
    def _default_env_path(bundle: firstrun.Bundle | None) -> str:
        """Pre-select the checkout's own `.env` when there is one to pre-select."""
        if bundle is None or not bundle.source_checkout or bundle.checkout_missing:
            return ""
        candidate = os.path.join(bundle.source_checkout, ".env")
        return candidate if os.path.exists(candidate) else ""

    def _render_setup(self) -> None:
        provisioning = self._model.phase is lifecycle.Phase.PROVISIONING
        bundle = self._bundle
        if bundle is None:
            paths = "Installation files: not found"
        else:
            paths = (f"Compose file: {bundle.compose_file}\n"
                     f"Provisioning scripts: {bundle.provision_dir}")
        if provisioning:
            title = "Provisioning Windows"
            if self._busy_kind == BUSY_PROVISIONING and self._busy_since is not None:
                title = (f"Provisioning Windows — "
                         f"{self._elapsed(time.monotonic() - self._busy_since)}")
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
            detail=self._setup_detail, busy=self._setup_busy,
            show_discard=self._record_state in (firstrun.RECORD_CONTAINER_GONE,
                                                firstrun.RECORD_DIFFERENT))

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
        self._setup_detail = ""
        self._begin_busy(BUSY_CREATING)
        self._render_setup()
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        task_progress: dict[str, Any] = {}

        def work():
            # D39: the record goes down *before* Compose runs, so an app that
            # dies between the two still knows a creation was attempted. It
            # carries no env path and no env content, ever.
            firstrun.write_provisioning_record(
                firstrun.ProvisioningRecord(started_at=started_at, phase="creating"))
            ok, output = firstrun.create_vm(bundle, env_path,
                                            on_line=task_progress.get("emit"))
            container_id = ""
            if ok:
                container_id = firstrun.inspect_container_id()
                firstrun.write_provisioning_record(firstrun.ProvisioningRecord(
                    started_at=started_at, phase="provisioning",
                    container_id=container_id))
            return ok, output, started_at, container_id

        def done(result) -> None:
            self._setup_busy = False
            ok, output, stamp, container_id = result
            if ok:
                self._record = firstrun.ProvisioningRecord(
                    started_at=stamp, phase="provisioning", container_id=container_id)
                self._record_state = firstrun.RECORD_MATCHES
                self._setup_detail = output
                self._dispatch(lifecycle.Event.VM_CREATED)
            else:
                self._end_busy()
                self._setup_detail = f"docker compose up -d failed:\n{output}"
                self._render_setup()

        def failed(message: str) -> None:
            self._setup_busy = False
            self._end_busy()
            self._setup_detail = f"Could not create the VM: {message}"
            self._render_setup()

        self._run_streaming(work, task_progress, done, failed)

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
                self._dispatch(lifecycle.Event.CONNECT_READY)
                return
            self._setup_detail = ("The bridge is not ready to connect yet. Fix the "
                                  "items above, then check again.")
            self._render_setup()

        def failed(message: str) -> None:
            self._setup_busy = False
            self._setup_detail = f"The host setup check could not be run: {message}"
            self._render_setup()

        self.run_async(work, done, failed)

    # ------------------------------------------------------ effect handlers --
    # Each of these does one imperative thing the reducer asked for. They are
    # deliberately dumb: no decisions, no branching on lifecycle state. The
    # order they run in is the reducer's, transcribed from the `_enter_*`
    # methods they replace.

    def _fx_stop_polling(self) -> None:
        self._timer.stop()

    def _fx_start_polling(self) -> None:
        self._timer.start()

    def _fx_force_refresh(self) -> None:
        self._refresh(force=True)

    def _fx_quiesce_io(self) -> None:
        # quiesce() rather than set_io_paused(): it also stops the window's
        # request/response poller and drops queued list requests, which matters
        # when Start is pressed from the powered-off state.
        self._window.quiesce()

    def _fx_pause_io(self) -> None:
        self._window.set_io_paused(True)

    def _fx_resume_io(self) -> None:
        # resume() also restarts the request/response poller quiesce stopped.
        self._window.resume()

    def _fx_reload_selective_sync(self) -> None:
        self._window.reload_selective_sync()

    def _fx_clear_health_rows(self) -> None:
        self._window.clear_health_rows()

    def _fx_hide_banner(self) -> None:
        self._end_busy()
        self._window.hide_banner()

    def _fx_hide_notice(self) -> None:
        self._window.hide_notice()

    def _fx_show_starting_banner(self) -> None:
        self._begin_busy(BUSY_STARTING)

    def _fx_show_start_failed_banner(self) -> None:
        self._end_busy()
        self._window.show_banner(
            f"The Windows VM did not start.\n{self._start_error}\n\n"
            "Open the VM screen to check it, then Retry start.", "error")

    def _fx_show_shutdown_banner(self) -> None:
        self._begin_busy(BUSY_SHUTDOWN)

    def _fx_show_powered_off_banner(self) -> None:
        self._end_busy()
        self._window.show_banner(
            "Bridge is powered off. The Windows VM is stopped and both shares are "
            "disconnected. Choose Start bridge to bring it back.", "off")

    def _fx_show_abort_banner(self) -> None:
        self._end_busy()
        self._window.show_banner(self._abort_message, "error")

    def _fx_show_setup_tab(self) -> None:
        self._window.show_setup_tab()

    def _fx_hide_setup_tab(self) -> None:
        self._window.hide_setup_tab()

    def _fx_show_window(self) -> None:
        self.show_window()

    def _fx_show_window_unless_minimized(self) -> None:
        if not self._minimized:
            self.show_window()

    def _fx_tray_starting(self) -> None:
        if self._tray is None:
            return
        self._tray.set_transition(STARTING, "iCloud bridge: starting the Windows VM…")
        self._tray.set_lifecycle_busy(True, allow_quit=True)
        self._tray.set_bridge_available(False)

    def _fx_tray_running(self) -> None:
        if self._tray is None:
            return
        self._tray.set_lifecycle_busy(False)
        self._tray.set_bridge_available(True)

    def _fx_tray_start_failed(self) -> None:
        if self._tray is None:
            return
        self._tray.set_transition(
            health.RED, f"iCloud bridge: start failed — {self._start_error}")
        self._tray.set_lifecycle_busy(False)
        self._tray.set_bridge_available(False)

    def _fx_tray_shutting_down(self) -> None:
        if self._tray is None:
            return
        self._tray.set_transition(STARTING, "iCloud bridge: shutting down…")
        self._tray.set_lifecycle_busy(True, allow_quit=False)

    def _fx_tray_powered_off(self) -> None:
        if self._tray is None:
            return
        self._tray.set_lifecycle_busy(False)
        self._tray.set_bridge_available(False)
        self._tray.set_transition(
            OFF, "iCloud bridge: powered off — choose Start bridge to reconnect")

    def _fx_tray_setup(self) -> None:
        if self._tray is None:
            return
        self._tray.set_lifecycle_busy(False)
        self._tray.set_bridge_available(False)
        self._tray.set_transition(
            health.RED, "iCloud bridge: setup required — open the status window")

    def _fx_enable_notifications(self) -> None:
        self._notify_enabled = True

    def _fx_disable_notifications(self) -> None:
        self._notify_enabled = False

    def _fx_reset_incidents(self) -> None:
        self._incidents.reset()

    def _fx_begin_startup_grace(self) -> None:
        # The canary is legitimately as old as the bridge was off, so give the
        # host health timer a bounded window to refresh it before a red snapshot
        # counts as an incident.
        self._incidents.begin_startup_grace(time.monotonic())

    def _fx_announce_start_failure(self) -> None:
        if self._minimized and self._tray is not None:
            # A minimized autostart launch has no visible window; use a tray
            # notification rather than an invisible modal.
            self._tray.notify("iCloud bridge",
                              "The Windows VM did not start. Open the tray menu to retry.")
        else:
            self.show_window()

    def _fx_mark_container_running(self) -> None:
        self._container_state = "running"

    def _fx_mark_container_stopped(self) -> None:
        self._container_state = "stopped"

    def _fx_show_unknown_banner(self) -> None:
        self._end_busy()
        action = self._model.desired_action
        self._window.show_banner(
            f"The bridge could not be powered {action} within the time allowed, and "
            "this app cannot tell what state it is in.\n"
            f"{self._abort_message}\n\n"
            "Nothing on the iCloud shares will be read or changed until you choose "
            "Retry. The privileged helper may still be finishing on its own.",
            "error")

    def _fx_tray_transition_unknown(self) -> None:
        if self._tray is None:
            return
        self._tray.set_transition(
            health.RED, "iCloud bridge: the last power operation did not complete")
        self._tray.set_lifecycle_busy(False)
        self._tray.set_bridge_available(False)

    def _fx_invalidate_caches(self) -> None:
        """Every cached answer described a state we can no longer vouch for."""
        self._documents.invalidate()
        self._container_probe.invalidate()
        self._classify_probe.invalidate()
        self._last_snapshot = None

    def _fx_mark_container_unknown(self) -> None:
        # Not "stopped": we genuinely do not know, and `available_action` must
        # offer nothing that assumes we do.
        self._container_state = None

    def _fx_run_setup_checks(self) -> None:
        self._run_setup_checks()

    def _fx_clear_setup_checks(self) -> None:
        self._setup_checks = []

    def _fx_render_setup(self) -> None:
        # D38: no subprocess survives Compose, so the only honest progress for
        # the Windows install is elapsed time since it started.
        self._begin_busy(BUSY_PROVISIONING)
        self._render_setup()

    def _fx_run_power_on(self) -> None:
        token = self._model.token
        task_progress: dict[str, Any] = {}

        def work():
            emit = task_progress.get("emit")
            return power.power_on(on_line=emit)

        self._run_streaming(work, task_progress,
                            lambda result: self._on_start_result(result, token),
                            lambda message: self._on_start_exception(message, token))

    def _run_streaming(self, work, task_progress, on_done, on_error) -> None:
        """`run_async`, with the worker's `progress` signal wired to `on_line`.

        The emitter has to be handed to `work` *after* the task exists, so the
        dict is the seam. Reader threads call it, the signal hops to the GUI
        thread, and `_on_phase_line` updates the label.
        """
        task = _Task(work)
        task_progress["emit"] = task.signals.progress.emit
        self._start_task(task, on_done, on_error, self._on_phase_line)

    def _on_phase_line(self, line: str) -> None:
        """One streamed output line. Presentation only — never a control input.

        For the helper, only `==> ` lines are phases; its other output is noise
        and must not be parsed. Compose has no such convention, so during
        creation the most recent line is shown as-is, elided to one row.
        """
        if self._busy_kind == BUSY_CREATING:
            self._phase_line = line
        else:
            phase = power.phase_of(line)
            if phase is not None:
                self._phase_line = phase
        self._refresh_busy_text()

    def _record_helper(self, action: str, ok: bool, detail: str) -> None:
        """Retain the last helper outcome for D37; the marker follows from it."""
        self._last_helper_action = action
        self._last_helper_ok = ok
        self._last_helper_detail = detail
        if ok:
            # The helper's own transaction sets or clears the marker, so a
            # success tells us its state without another filesystem read.
            self._marker_present = (action == "off")

    def _fx_exit_app(self) -> None:
        self._quit_gui_only()

    def _on_start_result(self, result: power.HelperResult, token: int) -> None:
        if not lifecycle.accepts(self._model, token):
            return
        self._record_helper("on", result.success, result.message)
        if result.success:
            # D39: the only automatic clear. Setup is finished when the bridge
            # has actually powered on, not when Compose returned.
            self._clear_provisioning_record()
            self._dispatch(lifecycle.Event.POWER_ON_SUCCEEDED)
            return
        if result.timed_out:
            self._abort_message = result.message
            self._dispatch(lifecycle.Event.POWER_TRANSITION_UNKNOWN)
            return
        self._start_error = result.message
        self._dispatch(lifecycle.Event.POWER_ON_FAILED)

    def _on_start_exception(self, message: str, token: int) -> None:
        if not lifecycle.accepts(self._model, token):
            return
        self._record_helper("on", False, message)
        self._start_error = f"the power helper could not be run: {message}"
        self._dispatch(lifecycle.Event.POWER_ON_FAILED)

    # ------------------------------------------------- the D30 power controls --

    def _sync_power_controls(self) -> None:
        """Offer the one lifecycle action this state allows — and only that one."""
        action = power.available_action(self._model.phase.value, self._container_state)
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
        if self._model.phase is not lifecycle.Phase.RUNNING:
            return
        if not self._confirm_power_off():
            return
        self._dispatch(lifecycle.Event.USER_POWER_OFF_CONFIRMED)

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
        """D30's **Start bridge**, from the powered-off or recoverable state.

        The guard is `power.available_action` itself, so the enabling rule has
        exactly one implementation: powered off, or running with a container a
        `docker inspect` definitively classified as stopped.
        """
        if power.available_action(self._model.phase.value,
                                  self._container_state) != power.ACTION_START:
            return
        self._dispatch(lifecycle.Event.USER_START_BRIDGE)

    def _on_retry_start_requested(self) -> None:
        if self._model.phase is lifecycle.Phase.TRANSITION_UNKNOWN:
            # Repeat the interrupted transaction, whichever way it was going.
            # `flock` in the helper serializes it against a surviving run.
            self._dispatch(lifecycle.Event.USER_RETRY_TRANSITION)
            return
        self._dispatch(lifecycle.Event.USER_RETRY_START)

    # ----------------------------------------------------------- quit flow ---

    def _on_quit_requested(self) -> None:
        kind = lifecycle.quit_kind(self._model.phase)
        if kind == lifecycle.QUIT_IGNORE:
            return
        if kind == lifecycle.QUIT_ALREADY_OFF:
            # Already off, durably: the marker and the stopped VM outlive this
            # process, so there is nothing left for the helper to do (D30).
            if self._confirm_simple_quit(
                    "The bridge is already powered off, so nothing more will be "
                    "disconnected. It stays off across a reboot; launching this app "
                    "again powers the VM back on."):
                self._dispatch(lifecycle.Event.QUIT_CONFIRMED_GUI_ONLY)
            return
        if kind == lifecycle.QUIT_UNKNOWN:
            if self._confirm_simple_quit(
                    "The last power operation did not finish in time and this app "
                    "cannot tell what state the bridge is in. Quitting changes "
                    "nothing either way; the privileged helper may still be "
                    "finishing on its own. Start this app again to reconcile."):
                self._dispatch(lifecycle.Event.QUIT_CONFIRMED_GUI_ONLY)
            return
        if kind == lifecycle.QUIT_NOTHING_MOUNTED:
            # Nothing is mounted, and a half-installed Windows guest must not be
            # torn down by quitting the app that is guiding the install (D31).
            if self._confirm_simple_quit(
                    "Setup is not finished, so there is nothing mounted to "
                    "disconnect. Any VM that has already been created keeps "
                    "running; start this app again to continue."):
                self._dispatch(lifecycle.Event.QUIT_CONFIRMED_GUI_ONLY)
            return
        choice = self._ask_quit()
        if choice == "off":
            self._dispatch(lifecycle.Event.QUIT_CONFIRMED_POWER_OFF)
        elif choice == "gui":
            self._dispatch(lifecycle.Event.QUIT_CONFIRMED_GUI_ONLY)

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

    # ---------------------------------------- the one power-off transaction ---
    # Quit and the keep-running power off share every step that matters — stop
    # polling, refuse new bridge I/O, drain in-flight mount work, then call the
    # helper — and differ only in what success means. That is why the reducer
    # gives both the same effect tuple and carries the continuation in the model.

    def _fx_begin_drain(self) -> None:
        token = self._model.token
        self._drain_ticks_left = SHUTDOWN_DRAIN_TIMEOUT_MS // DRAIN_POLL_MS
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(DRAIN_POLL_MS)
        self._drain_timer.timeout.connect(lambda: self._check_drain(token))
        self._drain_timer.start()
        # Safe to reduce re-entrantly: BEGIN_DRAIN is the last effect of the
        # transition that produced it, so nothing after this runs against a
        # model the nested dispatch has already replaced.
        self._check_drain(token)

    def _fx_stop_drain(self) -> None:
        if self._drain_timer is not None:
            self._drain_timer.stop()

    def _check_drain(self, token: int) -> None:
        if not lifecycle.accepts(self._model, token):
            self._fx_stop_drain()
            return
        # Never begin the unmount while a task (Apply write, gather, or list poll)
        # is still touching CIFS.
        if self._active == 0 and not self._window.apply_in_flight():
            self._dispatch(lifecycle.Event.DRAIN_COMPLETED, token)
            return
        self._drain_ticks_left -= 1
        if self._drain_ticks_left <= 0:
            self._abort_message = (
                "A file operation on the iCloud mount is still in progress, so the "
                "bridge was not disconnected. Close any open files or transfers "
                "and try again.")
            self._dispatch(lifecycle.Event.DRAIN_TIMED_OUT, token)

    def _fx_run_power_off(self) -> None:
        token = self._model.token
        task_progress: dict[str, Any] = {}

        def work():
            emit = task_progress.get("emit")
            return power.power_off(on_line=emit)

        self._run_streaming(work, task_progress,
                            lambda result: self._on_power_off_result(result, token),
                            lambda message: self._on_power_off_exception(message, token))

    def _on_power_off_result(self, result: power.HelperResult, token: int) -> None:
        if not lifecycle.accepts(self._model, token):
            return
        self._record_helper("off", result.success, result.message)
        if result.success:
            self._dispatch(lifecycle.Event.POWER_OFF_SUCCEEDED)
            return
        self._abort_message = result.message
        if result.timed_out:
            # Never the ordinary abort path: that resumes polling against shares
            # the helper may already have unmounted (D38).
            self._dispatch(lifecycle.Event.POWER_TRANSITION_UNKNOWN)
            return
        self._dispatch(lifecycle.Event.POWER_OFF_FAILED)

    def _on_power_off_exception(self, message: str, token: int) -> None:
        if not lifecycle.accepts(self._model, token):
            return
        self._record_helper("off", False, message)
        self._abort_message = f"the power helper could not be run: {message}"
        self._dispatch(lifecycle.Event.POWER_OFF_FAILED)

    # ------------------------------------------------------------ refreshing --

    def _refresh(self, force: bool = False) -> None:
        # No health polling while a transition owns the tray/mount state, while
        # the bridge is intentionally off, or while there is nothing mounted to
        # gather from (v2 plan D29, extended by D30/D31).
        if lifecycle.is_no_cifs(self._model.phase):
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
        # Retained for D37: a report in a no-CIFS state renders these plus the
        # timestamp that says how old they are.
        self._last_snapshot = snapshot
        self._last_gathered_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        if lifecycle.is_no_cifs(self._model.phase):
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
        if lifecycle.is_no_cifs(self._model.phase):
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

    #: Effect token to handler. Every member of `lifecycle.Effect` must appear
    #: here; `test_qt_wiring` asserts the two stay in step, so adding an effect
    #: without wiring it is a test failure rather than a silent no-op.
    _EFFECTS = {
        lifecycle.Effect.STOP_POLLING: _fx_stop_polling,
        lifecycle.Effect.START_POLLING: _fx_start_polling,
        lifecycle.Effect.FORCE_REFRESH: _fx_force_refresh,
        lifecycle.Effect.QUIESCE_IO: _fx_quiesce_io,
        lifecycle.Effect.PAUSE_IO: _fx_pause_io,
        lifecycle.Effect.RESUME_IO: _fx_resume_io,
        lifecycle.Effect.RELOAD_SELECTIVE_SYNC: _fx_reload_selective_sync,
        lifecycle.Effect.CLEAR_HEALTH_ROWS: _fx_clear_health_rows,
        lifecycle.Effect.HIDE_BANNER: _fx_hide_banner,
        lifecycle.Effect.HIDE_NOTICE: _fx_hide_notice,
        lifecycle.Effect.SHOW_STARTING_BANNER: _fx_show_starting_banner,
        lifecycle.Effect.SHOW_START_FAILED_BANNER: _fx_show_start_failed_banner,
        lifecycle.Effect.SHOW_SHUTDOWN_BANNER: _fx_show_shutdown_banner,
        lifecycle.Effect.SHOW_POWERED_OFF_BANNER: _fx_show_powered_off_banner,
        lifecycle.Effect.SHOW_ABORT_BANNER: _fx_show_abort_banner,
        lifecycle.Effect.SHOW_UNKNOWN_BANNER: _fx_show_unknown_banner,
        lifecycle.Effect.SHOW_SETUP_TAB: _fx_show_setup_tab,
        lifecycle.Effect.HIDE_SETUP_TAB: _fx_hide_setup_tab,
        lifecycle.Effect.SHOW_WINDOW: _fx_show_window,
        lifecycle.Effect.SHOW_WINDOW_UNLESS_MINIMIZED: _fx_show_window_unless_minimized,
        lifecycle.Effect.TRAY_STARTING: _fx_tray_starting,
        lifecycle.Effect.TRAY_RUNNING: _fx_tray_running,
        lifecycle.Effect.TRAY_START_FAILED: _fx_tray_start_failed,
        lifecycle.Effect.TRAY_SHUTTING_DOWN: _fx_tray_shutting_down,
        lifecycle.Effect.TRAY_POWERED_OFF: _fx_tray_powered_off,
        lifecycle.Effect.TRAY_SETUP: _fx_tray_setup,
        lifecycle.Effect.TRAY_TRANSITION_UNKNOWN: _fx_tray_transition_unknown,
        lifecycle.Effect.ENABLE_NOTIFICATIONS: _fx_enable_notifications,
        lifecycle.Effect.DISABLE_NOTIFICATIONS: _fx_disable_notifications,
        lifecycle.Effect.RESET_INCIDENTS: _fx_reset_incidents,
        lifecycle.Effect.BEGIN_STARTUP_GRACE: _fx_begin_startup_grace,
        lifecycle.Effect.ANNOUNCE_START_FAILURE: _fx_announce_start_failure,
        lifecycle.Effect.INVALIDATE_CACHES: _fx_invalidate_caches,
        lifecycle.Effect.MARK_CONTAINER_UNKNOWN: _fx_mark_container_unknown,
        lifecycle.Effect.MARK_CONTAINER_RUNNING: _fx_mark_container_running,
        lifecycle.Effect.MARK_CONTAINER_STOPPED: _fx_mark_container_stopped,
        lifecycle.Effect.SYNC_POWER_CONTROLS: _sync_power_controls,
        lifecycle.Effect.RUN_SETUP_CHECKS: _fx_run_setup_checks,
        lifecycle.Effect.CLEAR_SETUP_CHECKS: _fx_clear_setup_checks,
        lifecycle.Effect.RENDER_SETUP: _fx_render_setup,
        lifecycle.Effect.RUN_POWER_ON: _fx_run_power_on,
        lifecycle.Effect.RUN_POWER_OFF: _fx_run_power_off,
        lifecycle.Effect.BEGIN_DRAIN: _fx_begin_drain,
        lifecycle.Effect.STOP_DRAIN: _fx_stop_drain,
        lifecycle.Effect.EXIT_APP: _fx_exit_app,
        lifecycle.Effect.REPORT_INVALID_TRANSITION: _report_invalid_transition,
    }


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
