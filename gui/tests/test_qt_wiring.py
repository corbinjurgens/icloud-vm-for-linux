"""Thin PySide6 integration tests: the signal wiring the pure suite cannot see.

`test_lifecycle.py` proves the transition table. What it cannot prove is that the
controller *applies* it — that startup really schedules no CIFS work before the
power-on resolves, that a stale worker signal really is discarded, that closing
the window really routes where it should. Those need widgets, timers and a
thread pool, so they live here.

Everything expensive or dangerous is faked: `power`, `bridge` and `health` are
monkeypatched, the modal dialogs are replaced, and no docker, sudo or mount is
ever touched. The whole file is skipped when PySide6 is absent, which is what
keeps `CONTRIBUTING.md`'s with-and-without-Qt rule satisfiable.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")

# Must be set before the first Qt import, or Qt binds to a real display that
# does not exist on a build machine.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDeadlineTimer, QThreadPool, QTimer   # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

from icloud_bridge_gui import __main__ as app_module              # noqa: E402
from icloud_bridge_gui import window as window_module             # noqa: E402

_RealMessageBox = window_module.QMessageBox
from icloud_bridge_gui import (backup, bridge, diagnostics, firstrun, health,  # noqa: E402
                               lifecycle, listing, power)


# ------------------------------------------------------------------ fakes --

class Recorder:
    """A callable that records its calls and returns a canned result."""

    def __init__(self, result=None, *, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple] = []
        self.blocked = False

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.blocked:
            raise AssertionError("this call must not happen in this state")
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def count(self) -> int:
        return len(self.calls)


class FakeRunner:
    """A bounded runner for the D37 collector: no systemctl, no sudo, no docker."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        return power.RunResult(0, "active\n", "")


class FakeTray:
    """Enough of `TrayIcon` for the controller, with no platform tray at all."""

    def __init__(self):
        self.notifications: list[tuple[str, str]] = []
        self.transitions: list[tuple] = []
        self.busy: list[tuple] = []
        self.available: list[bool] = []
        self.actions: list[str] = []
        self.states: list[tuple] = []
        self.shown = False

    def show(self):
        self.shown = True

    def hide(self):
        self.shown = False

    def notify(self, title, body, level=None):
        self.notifications.append((title, body))

    def set_transition(self, icon, tip):
        self.transitions.append((icon, tip))

    def set_lifecycle_busy(self, busy, *, allow_quit=False):
        self.busy.append((busy, allow_quit))

    def set_bridge_available(self, available):
        self.available.append(available)

    def set_power_action(self, action):
        self.actions.append(action)

    def update_state(self, overall, checks):
        self.states.append((overall, checks))


def green_snapshot():
    return health.Snapshot(checks=[health.Check("Container", health.GREEN, "running")],
                           overall=health.GREEN, status=None, tree=None)


class FakeMessageBox:
    """Stands in for `QMessageBox` so no modal dialog can ever open.

    Offscreen Qt still runs a real modal event loop, so one unstubbed
    `QMessageBox.question` hangs the whole suite. Replacing the name in the
    window module's namespace removes that possibility by construction rather
    than by remembering to patch each call site.
    """

    StandardButton = _RealMessageBox.StandardButton
    Icon = _RealMessageBox.Icon
    ButtonRole = _RealMessageBox.ButtonRole

    #: What the next `question` returns; tests set this.
    answer = _RealMessageBox.StandardButton.Cancel
    calls: list[tuple[str, tuple]] = []

    @classmethod
    def reset(cls):
        cls.answer = _RealMessageBox.StandardButton.Cancel
        cls.clicked_index = -1
        cls.calls = []

    @classmethod
    def count(cls, kind: str) -> int:
        return sum(1 for name, _ in cls.calls if name == kind)

    @classmethod
    def question(cls, *args, **kwargs):
        cls.calls.append(("question", args))
        return cls.answer

    @classmethod
    def information(cls, *args, **kwargs):
        cls.calls.append(("information", args))

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(("warning", args))

    @classmethod
    def critical(cls, *args, **kwargs):
        cls.calls.append(("critical", args))

    # --- the instance form, used by the controller's own confirmations ---
    #: Index of the button `exec` should report as clicked; tests set it.
    clicked_index = -1                  # -1 means the last (usually Cancel)

    def __init__(self, parent=None):
        self._buttons: list[object] = []
        type(self).calls.append(("dialog", ()))

    def setWindowTitle(self, _title):
        pass

    def setIcon(self, _icon):
        pass

    def setText(self, _text):
        pass

    def setInformativeText(self, _text):
        pass

    def addButton(self, label, _role):
        button = object()
        self._buttons.append(button)
        return button

    def setDefaultButton(self, _button):
        pass

    def setEscapeButton(self, _button):
        pass

    def exec(self):
        return 0

    def clickedButton(self):
        return self._buttons[type(self).clicked_index] if self._buttons else None


@pytest.fixture(autouse=True)
def dialogs(monkeypatch):
    """Every test in this file gets the fake; none can block on a modal."""
    FakeMessageBox.reset()
    monkeypatch.setattr(window_module, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(app_module, "QMessageBox", FakeMessageBox)
    return FakeMessageBox


# --------------------------------------------------------------- fixtures --

@pytest.fixture(scope="module")
def qapp():
    """One QApplication for the module; Qt forbids constructing a second."""
    existing = QApplication.instance()
    application = existing or QApplication(["test"])
    yield application
    QThreadPool.globalInstance().waitForDone(5000)


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    """Replace every subprocess, mount and dialog the controller can reach."""
    # The D36 backup is real local-disk work inside the read/Apply workers, so
    # point XDG state at a tmpdir before anything can touch the real one.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state = type("Fakes", (), {})()
    state.state_home = tmp_path / "state"
    state.inspect = Recorder(power.DockerStatus("running", raw="running"))
    state.marker = Recorder(False)
    state.power_on = Recorder(power.HelperResult(True, 0, "bridge on"))
    state.power_off = Recorder(power.HelperResult(True, 0, "bridge off"))
    state.gather = Recorder(green_snapshot())
    state.read_exclusions = Recorder({"revision": 1, "exclusions": []})
    state.read_status = Recorder({})
    state.read_tree = Recorder({})
    state.request_listing = Recorder("req-1")
    state.poll_response = Recorder(None)
    state.cancel_request = Recorder(None)
    #: D39 reads the container id to match a record. Faked like every other
    #: Docker call: a checkout that happens to have a real `icloud-windows`
    #: container must not change what these tests prove.
    state.container_id = Recorder("abc123")

    monkeypatch.setattr(power, "inspect_container", state.inspect)
    monkeypatch.setattr(power, "marker_exists", state.marker)
    monkeypatch.setattr(power, "power_on", state.power_on)
    monkeypatch.setattr(power, "power_off", state.power_off)
    monkeypatch.setattr(health, "gather", state.gather)
    monkeypatch.setattr(health, "container_running", Recorder((True, "running")))
    monkeypatch.setattr(bridge, "read_exclusions", state.read_exclusions)
    monkeypatch.setattr(bridge, "read_status", state.read_status)
    monkeypatch.setattr(bridge, "read_tree", state.read_tree)
    monkeypatch.setattr(bridge, "request_listing", state.request_listing)
    monkeypatch.setattr(bridge, "poll_response", state.poll_response)
    monkeypatch.setattr(bridge, "cancel_request", state.cancel_request)
    monkeypatch.setattr(firstrun, "inspect_container_id", state.container_id)

    # The modal confirmations: default to "yes, do it", overridable per test.
    state.answers = {"quit": "gui", "simple_quit": True, "power_off": True,
                     "create_vm": True}
    monkeypatch.setattr(app_module.Application, "_ask_quit",
                        lambda self: state.answers["quit"])
    monkeypatch.setattr(app_module.Application, "_confirm_simple_quit",
                        lambda self, informative: state.answers["simple_quit"])
    monkeypatch.setattr(app_module.Application, "_confirm_power_off",
                        lambda self: state.answers["power_off"])
    monkeypatch.setattr(app_module.Application, "_confirm_create_vm",
                        lambda self: state.answers["create_vm"])

    state.quits = []
    monkeypatch.setattr(app_module.Application, "_quit_gui_only",
                        lambda self: state.quits.append(True))
    return state


def pump(seconds: float = 2.0, until=None) -> bool:
    """Spin the event loop until ``until`` is true or the deadline passes.

    Worker results arrive as queued signals, so the test has to give Qt a chance
    to deliver them. Bounded so a wiring mistake fails rather than hangs.
    """
    deadline = QDeadlineTimer(int(seconds * 1000))
    while not deadline.hasExpired():
        QApplication.processEvents()
        if until is None:
            continue
        if until():
            return True
    QApplication.processEvents()
    return bool(until()) if until is not None else True


@pytest.fixture
def controller(qapp, fakes, request):
    """Build the controller, then tear its timers and workers down cleanly."""
    tray = FakeTray() if getattr(request, "param", "tray") == "tray" else None
    built = {}

    def make(*, minimized=False, with_tray=True):
        instance = app_module.Application.__new__(app_module.Application)
        # Construct for real, but hand it a fake tray instead of a platform one.
        original = app_module.TrayIcon
        try:
            if with_tray:
                app_module.TrayIcon = lambda _parent: _connectable(tray)
            instance.__init__(qapp, minimized=minimized, tray_available=with_tray)
        finally:
            app_module.TrayIcon = original
        built["instance"] = instance
        return instance

    yield make

    instance = built.get("instance")
    if instance is not None:
        instance._timer.stop()
        if instance._drain_timer is not None:
            instance._drain_timer.stop()
        instance._window.quiesce()
        instance._window.close()
        # Let every in-flight worker land before the next test's fakes replace
        # the ones it captured, or a late callback runs against a dead window.
        QThreadPool.globalInstance().waitForDone(5000)
        pump(0.2)


def _connectable(tray):
    """Give the fake tray the signal objects the controller connects to."""
    for name in ("show_window_requested", "quit_requested", "retry_start_requested",
                 "power_off_requested", "start_requested"):
        setattr(tray, name, _Signalish())
    return tray


class _Signalish:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self):
        for slot in list(self.slots):
            slot()


# ------------------------------------------------------------------ tests --

def test_every_effect_has_a_handler():
    """Adding an effect without wiring it is a test failure, not a silent no-op."""
    assert set(lifecycle.Effect) == set(app_module.Application._EFFECTS)


def test_startup_schedules_no_bridge_work_before_power_on_resolves(controller, fakes):
    """The central D29 rule, proved through the real signal path."""
    fakes.inspect.result = power.DockerStatus("stopped", raw="exited")
    release = threading.Event()

    def slow_power_on(*args, **kwargs):
        fakes.power_on.calls.append((args, kwargs))
        release.wait(timeout=10)
        return power.HelperResult(True, 0, "bridge on")

    power.power_on = slow_power_on
    try:
        app = controller()
        pump(2.0, until=lambda: fakes.power_on.count == 1)
        assert fakes.power_on.count == 1
        assert app._model.phase is lifecycle.Phase.STARTING
        # Nothing that touches CIFS may have run yet.
        assert fakes.gather.count == 0
        assert fakes.read_exclusions.count == 0
        assert not app._timer.isActive()
        release.set()
        pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    finally:
        release.set()
        power.power_on = fakes.power_on
    assert app._model.phase is lifecycle.Phase.RUNNING
    assert app._timer.isActive()
    pump(1.0, until=lambda: fakes.gather.count >= 1)
    assert fakes.gather.count >= 1


def test_already_running_bridge_starts_monitoring_without_the_helper(controller, fakes):
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    assert fakes.power_on.count == 0
    assert app._timer.isActive()


def test_setup_required_performs_no_bridge_reads(controller, fakes):
    """D31: an absent container means no mount exists, so nothing may be read."""
    fakes.inspect.result = power.DockerStatus("absent", detail="no such object")
    fakes.gather.blocked = True
    fakes.read_exclusions.blocked = True
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.SETUP)
    assert app._model.phase is lifecycle.Phase.SETUP
    assert not app._timer.isActive()
    assert fakes.power_on.count == 0
    # Give the (blocked) fakes a chance to be called wrongly.
    pump(0.3)
    assert fakes.gather.count == 0
    assert fakes.read_exclusions.count == 0


def test_inspection_failure_routes_into_setup_without_mutating(controller, fakes):
    fakes.inspect.result = power.DockerStatus("error", detail="daemon down")
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.SETUP)
    assert fakes.power_on.count == 0
    assert fakes.power_off.count == 0
    assert "daemon down" in app._setup_detail


def test_failed_power_off_resumes_polling(controller, fakes):
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    fakes.power_off.result = power.HelperResult(False, 1, "target is busy")

    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING
         and fakes.power_off.count == 1)
    assert fakes.power_off.count == 1
    assert app._model.phase is lifecycle.Phase.RUNNING
    assert app._timer.isActive()
    assert app._notify_enabled is True
    assert app._abort_message == "target is busy"
    assert fakes.quits == []            # nothing was torn down, so no exit


def test_successful_keep_running_power_off_stops_everything(controller, fakes):
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.POWERED_OFF)
    assert app._model.phase is lifecycle.Phase.POWERED_OFF
    assert not app._timer.isActive()
    assert fakes.quits == []
    assert app._container_state == "stopped"


def test_quit_while_powered_off_never_invokes_the_helper(controller, fakes):
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.POWERED_OFF)
    before = fakes.power_off.count

    app._on_quit_requested()
    pump(0.3)
    assert fakes.power_off.count == before      # the helper was not called again
    assert fakes.quits == [True]


def test_quit_gui_only_from_running_leaves_the_bridge_up(controller, fakes):
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    fakes.answers["quit"] = "gui"
    app._on_quit_requested()
    pump(0.3)
    assert fakes.power_off.count == 0
    assert fakes.quits == [True]


def test_a_stale_power_on_result_cannot_resurrect_a_left_state(controller, fakes):
    """A superseded worker signal is dropped before it reaches the reducer."""
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    stale_token = app._model.token
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.POWERED_OFF)

    app._on_start_result(power.HelperResult(True, 0, "bridge on"), stale_token)
    pump(0.2)
    assert app._model.phase is lifecycle.Phase.POWERED_OFF
    assert not app._timer.isActive()


def test_a_stale_listing_response_after_reload_is_discarded(controller, fakes):
    """A Reload starts a new tree generation; the old answer must not be applied."""
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    window = app._window
    requests = window._requests

    assert requests.begin_first_page("Folder")
    requests.dispatched("req-stale", "Folder", 0, listing.FIRST_PAGE, 0.0)
    assert requests.pending_ids()

    window.reload_selective_sync()
    pump(1.0, until=lambda: not requests.pending_ids())

    # The response arrives after the rebuild: `take` no longer knows the id, so
    # `_on_response` returns without touching the tree.
    window._on_response(("req-stale", {"entries": [{"name": "x", "dir": False}]}))
    pump(0.2)
    assert requests.take("req-stale") is None
    assert "req-stale" not in window._polls_in_flight
    assert not window._poll_timer.isActive()


# --------------------------------------- the response poll is armed on demand --

def _arm_a_request(window, fakes, path="Docs", offset=0, kind=listing.FIRST_PAGE):
    """Dispatch one list request and wait for the response poll to arm."""
    window._request_files(path, offset, kind)
    assert pump(2.0, until=lambda: window._poll_timer.isActive())
    return window


def test_an_idle_window_never_wakes_for_the_response_poll(controller, fakes):
    """Nothing to poll for means no 1 Hz tick at all, for the whole session."""
    app, window = running_controller(controller, fakes)
    ticks: list[int] = []
    window._poll_timer.timeout.connect(lambda: ticks.append(1))

    assert not window._poll_timer.isActive()
    pump(1.5)
    assert ticks == []


def test_a_dispatched_request_arms_the_poll_and_its_answer_stops_it(controller, fakes):
    app, window = running_controller(controller, fakes)
    _arm_a_request(window, fakes)

    fakes.poll_response.result = {"files": []}
    assert pump(3.0, until=lambda: not window._poll_timer.isActive())
    assert not window._requests.pending_ids()
    assert not window._polls_in_flight


def test_a_continuation_page_arms_the_poll_again(controller, fakes):
    app, window = running_controller(controller, fakes)
    _arm_a_request(window, fakes)
    fakes.poll_response.result = {"files": []}
    assert pump(3.0, until=lambda: not window._poll_timer.isActive())

    fakes.poll_response.result = None
    _arm_a_request(window, fakes, offset=1000, kind=listing.MORE)


def test_a_guest_error_stops_the_poll(controller, fakes):
    app, window = running_controller(controller, fakes)
    _arm_a_request(window, fakes)

    fakes.poll_response.result = {"error": "listing failed"}
    assert pump(3.0, until=lambda: not window._poll_timer.isActive())
    assert not window._requests.pending_ids()


def test_a_timed_out_request_stops_the_poll_after_cancelling(controller, fakes):
    app, window = running_controller(controller, fakes)
    # Deadline in the past: the first tick expires it.
    window._requests.dispatched("req-old", "Docs", 0, listing.FIRST_PAGE, 0.0)
    window._sync_poll_timer()
    assert window._poll_timer.isActive()

    assert pump(3.0, until=lambda: not window._poll_timer.isActive())
    assert not window._requests.pending_ids()
    # The cancel runs on a worker, so it lands just after the timer stops.
    assert pump(2.0, until=lambda: bool(fakes.cancel_request.calls))


def test_a_dispatch_failure_never_arms_the_poll(controller, fakes):
    app, window = running_controller(controller, fakes)
    fakes.request_listing.error = RuntimeError("bridge share unwritable")

    window._request_files("Docs", 0, listing.FIRST_PAGE)
    pump(1.0, until=lambda: bool(fakes.request_listing.calls))
    pump(0.3)
    assert not window._poll_timer.isActive()
    assert not window._requests.pending_ids()


def test_quiesce_stops_the_poll_and_resume_leaves_it_stopped(controller, fakes):
    """D29: nothing outstanding survives a quiesce, so nothing restarts."""
    app, window = running_controller(controller, fakes)
    _arm_a_request(window, fakes)

    window.quiesce()
    assert not window._poll_timer.isActive()
    window.resume()
    assert not window._poll_timer.isActive()


def test_a_reload_stops_the_poll(controller, fakes):
    app, window = running_controller(controller, fakes)
    _arm_a_request(window, fakes)

    window.reload_selective_sync()
    assert pump(2.0, until=lambda: not window._poll_timer.isActive())
    assert not window._requests.pending_ids()


def test_window_close_hides_when_a_tray_exists(controller, fakes):
    app = controller(with_tray=True)
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    assert app._window.hide_on_close is True
    app._window.show()
    app._window.close()
    pump(0.2)
    assert not app._window.isVisible()
    assert fakes.quits == []            # hiding is not quitting


def test_window_close_without_a_tray_routes_to_the_quit_confirmation(controller, fakes):
    app = controller(with_tray=False)
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    assert app._window.hide_on_close is False
    fakes.answers["quit"] = "gui"
    app._window.show()
    app._window.close()
    pump(0.5, until=lambda: bool(fakes.quits))
    assert fakes.quits == [True]
    assert fakes.power_off.count == 0


def test_start_bridge_is_refused_while_the_container_is_running(controller, fakes):
    """Health colour never authorizes a mutation; only the Docker answer does."""
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    app._container_state = "running"
    app._on_start_requested()
    pump(0.2)
    assert fakes.power_on.count == 0
    assert app._model.phase is lifecycle.Phase.RUNNING


def test_start_bridge_recovers_a_definitively_stopped_container(controller, fakes):
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    app._container_state = "stopped"
    app._on_start_requested()
    pump(2.0, until=lambda: fakes.power_on.count == 1)
    assert fakes.power_on.count == 1


def test_an_unexpected_event_is_reported_not_swallowed(controller, fakes):
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    before = app._model
    app._dispatch(lifecycle.Event.DRAIN_COMPLETED)
    assert app._model == before
    assert app._invalid_transitions


def test_the_drain_timer_is_stopped_once_the_helper_is_called(controller, fakes):
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.POWERED_OFF)
    assert isinstance(app._drain_timer, QTimer)
    assert not app._drain_timer.isActive()


# ------------------------------------- the D35 compatibility gate (controller) --

def snapshot_with(compat):
    return health.Snapshot(
        checks=[health.Check("Guest agent", health.GREEN, "reporting")],
        overall=health.GREEN, status={"version": 1}, tree=None, compatibility=compat)


def running_controller(controller, fakes):
    """A controller in normal monitoring, with its startup work fully drained.

    Draining matters: entering `running` schedules a selective-sync reload, and
    a test that stubs something out while that reload is still in flight would
    be racing its own fixture.
    """
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    pump(1.0, until=lambda: app._active == 0)
    window = app._window
    # A verified protocol, so the D35 gate is not the thing under test here.
    window.apply_snapshot(
        snapshot_with(bridge.Compatibility(bridge.COMPAT_CURRENT, bridge.AGENT_BUILD)))
    # A loaded selection with a staged change, so Apply would otherwise be live.
    window._loaded_wanted = []
    window._loaded_revision = 3
    window._config_error = None
    window._wanted = ["Docs"]
    window._set_backup_warning("")
    return app, window


@pytest.mark.parametrize("compat,enabled", [
    (bridge.Compatibility(bridge.COMPAT_CURRENT, bridge.AGENT_BUILD), True),
    (bridge.Compatibility(bridge.COMPAT_SKEWED, 99, "build 99"), True),
    (bridge.Compatibility(bridge.COMPAT_INCOMPATIBLE, None, "version 2"), False),
    (bridge.Compatibility(bridge.COMPAT_UNKNOWN), False),
])
def test_apply_is_enabled_only_by_a_verified_protocol(controller, fakes, compat, enabled):
    """Skew stays usable; an unsupported or unverified protocol does not."""
    app, window = running_controller(controller, fakes)
    window.apply_snapshot(snapshot_with(compat))
    assert window._apply_button.isEnabled() is enabled


def test_an_incompatible_protocol_refuses_the_apply_write(controller, fakes, monkeypatch):
    app, window = running_controller(controller, fakes)
    written = Recorder(4)
    monkeypatch.setattr(bridge, "write_exclusions", written)
    window.apply_snapshot(
        snapshot_with(bridge.Compatibility(bridge.COMPAT_INCOMPATIBLE, None, "version 2")))

    window._apply()
    pump(0.3)
    assert written.count == 0                   # exclusions.json left untouched
    # The Selective Sync tab is not the current tab, so `isVisible` is False for
    # a reason unrelated to this; `isHidden` is the per-widget question.
    assert not window._sync_error.isHidden()
    assert "04-bridge-agent.ps1" in window._sync_error.text()


def test_an_incompatible_protocol_dispatches_no_list_request(controller, fakes):
    app, window = running_controller(controller, fakes)
    window.apply_snapshot(
        snapshot_with(bridge.Compatibility(bridge.COMPAT_INCOMPATIBLE, None, "version 2")))

    window._request_files("Docs", 0, listing.FIRST_PAGE)
    pump(0.3)
    assert not any(call[0][:1] == ("Docs",) for call in fakes.request_listing.calls)


def test_a_skewed_agent_still_dispatches_and_shows_the_banner(controller, fakes):
    app, window = running_controller(controller, fakes)
    window.apply_snapshot(
        snapshot_with(bridge.Compatibility(bridge.COMPAT_SKEWED, 99, "the guest agent is build 99")))
    assert window._protocol.isVisible()
    assert "04-bridge-agent.ps1" in window._protocol.text()

    def asked_for_docs():
        return any(call[0][:1] == ("Docs",) for call in fakes.request_listing.calls)

    window._request_files("Docs", 0, listing.FIRST_PAGE)
    pump(1.0, until=asked_for_docs)
    assert asked_for_docs()


def test_powering_off_reverts_the_gate_to_unknown(controller, fakes):
    """A classification about an agent that is no longer reachable is not kept."""
    app, window = running_controller(controller, fakes)
    window.apply_snapshot(
        snapshot_with(bridge.Compatibility(bridge.COMPAT_CURRENT, bridge.AGENT_BUILD)))
    assert window._compatibility.writable

    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.POWERED_OFF)
    assert window._compatibility.state == bridge.COMPAT_UNKNOWN
    assert not window._compatibility.writable
    assert not window._protocol.isVisible()


# ------------------------------------------ the D36 backup/restore (controller) --

def test_a_validated_read_writes_the_backup_on_the_worker(controller, fakes):
    fakes.read_exclusions.result = {"revision": 5, "exclusions": ["Docs/Big"]}
    app, window = running_controller(controller, fakes)
    window.reload_selective_sync()
    pump(2.0, until=lambda: window._loaded_revision == 5)

    saved = backup.load(str(fakes.state_home))
    assert saved.revision == 5
    assert saved.exclusions == ("Docs/Big",)
    assert saved.source == backup.SOURCE_READ
    assert window._backup_warning.isHidden()


def test_a_read_whose_backup_fails_still_loads_the_selection(controller, fakes,
                                                             monkeypatch):
    """Two results: the bridge read succeeded, only the local snapshot did not."""
    fakes.read_exclusions.result = {"revision": 5, "exclusions": ["Docs/Big"]}
    app, window = running_controller(controller, fakes)
    monkeypatch.setattr(backup, "save",
                        Recorder(error=backup.BackupError("disk full")))

    window.reload_selective_sync()
    pump(2.0, until=lambda: window._loaded_revision == 5)
    assert window._loaded_wanted == ["Docs/Big"]        # the selection loaded
    assert not window._backup_warning.isHidden()
    assert "not backed up" in window._backup_warning.text()


def test_an_apply_whose_backup_fails_is_still_an_apply(controller, fakes, monkeypatch, dialogs):
    """It must never route through the "Nothing was changed" failure dialog."""
    app, window = running_controller(controller, fakes)
    monkeypatch.setattr(bridge, "write_exclusions", Recorder(9))
    monkeypatch.setattr(backup, "save",
                        Recorder(error=backup.BackupError("disk full")))
    dialogs.answer = dialogs.StandardButton.Ok

    window._apply()
    pump(2.0, until=lambda: window._loaded_revision == 9)
    assert window._loaded_revision == 9
    assert window._loaded_wanted == ["Docs"]
    assert dialogs.count("warning") == 0
    assert not window._backup_warning.isHidden()


def test_a_dirty_staged_selection_blocks_restore(controller, fakes, monkeypatch, dialogs):
    app, window = running_controller(controller, fakes)
    window.apply_snapshot(
        snapshot_with(bridge.Compatibility(bridge.COMPAT_CURRENT, bridge.AGENT_BUILD)))
    assert window._selection_is_dirty()
    assert not window._can_restore()
    assert not window._restore_button.isEnabled()

    loaded = Recorder(None)
    monkeypatch.setattr(backup, "load", loaded)

    window._restore_from_backup()
    pump(0.3)
    assert loaded.count == 0            # the backup was not even read
    assert dialogs.count("information") == 1


def test_restore_is_offered_once_the_selection_is_settled(controller, fakes):
    app, window = running_controller(controller, fakes)
    window._wanted = list(window._loaded_wanted)         # settle it
    window.apply_snapshot(
        snapshot_with(bridge.Compatibility(bridge.COMPAT_CURRENT, bridge.AGENT_BUILD)))
    assert window._can_restore()
    assert window._restore_button.isEnabled()


def test_restore_is_refused_while_the_protocol_is_incompatible(controller, fakes):
    app, window = running_controller(controller, fakes)
    window._wanted = list(window._loaded_wanted)
    window.apply_snapshot(
        snapshot_with(bridge.Compatibility(bridge.COMPAT_INCOMPATIBLE, None, "version 2")))
    assert not window._can_restore()
    assert not window._restore_button.isEnabled()


def test_restore_writes_above_every_observed_revision(controller, fakes, monkeypatch, dialogs):
    app, window = running_controller(controller, fakes)
    window._wanted = list(window._loaded_wanted)
    window._loaded_revision = 3
    window._last_written_revision = 4
    window._status = {"appliedRevision": 2}

    written = Recorder(31)
    monkeypatch.setattr(bridge, "write_exclusions", written)
    dialogs.answer = dialogs.StandardButton.Ok

    window._confirm_and_restore(backup.Backup(revision=30, exclusions=("Docs/Big",),
                                              saved_at="2026-07-26T12:00:00Z"))
    pump(2.0, until=lambda: written.count == 1)
    args, kwargs = written.calls[0]
    assert args[0] == ["Docs/Big"]
    assert kwargs["expect_revision"] == 3
    assert kwargs["applied_revision"] == 2
    assert kwargs["last_written"] == 4
    assert kwargs["minimum_revision"] == 30


def test_a_restore_that_would_change_nothing_says_so(controller, fakes, monkeypatch,
                                                    dialogs):
    app, window = running_controller(controller, fakes)
    window._loaded_wanted = ["Docs/Big"]
    window._wanted = ["Docs/Big"]
    written = Recorder(5)
    monkeypatch.setattr(bridge, "write_exclusions", written)
    window._confirm_and_restore(backup.Backup(revision=2, exclusions=("Docs/Big",)))
    pump(0.3)
    assert written.count == 0
    assert dialogs.count("information") == 1


def test_a_missing_backup_is_an_error_dialog_and_changes_nothing(controller, fakes,
                                                                 monkeypatch, dialogs):
    app, window = running_controller(controller, fakes)
    window._wanted = list(window._loaded_wanted)
    written = Recorder(5)
    monkeypatch.setattr(bridge, "write_exclusions", written)
    # The startup reload already wrote one; a missing backup is the case here.
    os.unlink(backup.backup_path(str(fakes.state_home)))

    window._restore_from_backup()
    pump(2.0, until=lambda: dialogs.count("critical") == 1)
    assert dialogs.count("critical") == 1
    assert written.count == 0
    assert window._loaded_wanted == []


# ---------------------------------------- the D37 diagnostic export (wiring) --

def test_collection_runs_on_a_worker_and_only_copy_reaches_the_clipboard(
        controller, fakes, monkeypatch):
    app, window = running_controller(controller, fakes)
    threads = []
    real_report_text = diagnostics.report_text

    def recording_report_text(facts, *args, **kwargs):
        threads.append(threading.current_thread())
        return real_report_text(facts, FakeRunner(), **kwargs)

    monkeypatch.setattr(diagnostics, "report_text", recording_report_text)
    clipboard = QApplication.clipboard()
    clipboard.setText("untouched")

    # Building the report alone must not reach the clipboard.
    window._run_diagnostics(lambda text: None)
    pump(2.0, until=lambda: bool(threads))
    pump(0.3)
    assert clipboard.text() == "untouched"
    assert threads and threads[0] is not threading.main_thread()

    window._copy_diagnostics()
    pump(2.0, until=lambda: clipboard.text() != "untouched")
    assert diagnostics.HEADER in clipboard.text()


def test_the_report_carries_the_controller_facts_and_no_folder_names(controller, fakes):
    app, window = running_controller(controller, fakes)
    window._loaded_wanted = ["Tax Returns 2019"]
    window._loaded_revision = 7
    facts = app._diagnostic_facts()
    assert facts.lifecycle == "running"
    assert facts.exclusion_paths == ("Tax Returns 2019",)
    assert facts.documents.exclusions_revision == 7

    text = diagnostics.report_text(facts, FakeRunner())
    assert "Tax Returns 2019" not in text
    assert "<path-1>" in text


def test_a_no_cifs_state_reports_from_cache_and_says_so(controller, fakes):
    """Setup has no mount to gather from; the report must still be useful."""
    fakes.inspect.result = power.DockerStatus("absent", detail="no such object")
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.SETUP)
    facts = app._diagnostic_facts()
    assert facts.lifecycle == "setup"
    assert facts.gathered_at == ""
    text = diagnostics.report_text(facts, FakeRunner())
    assert "not gathered" in text
    assert fakes.gather.count == 0           # and nothing was read to build it


def test_the_last_helper_result_is_retained_for_the_report(controller, fakes):
    app, window = running_controller(controller, fakes)
    fakes.power_off.result = power.HelperResult(False, 1, "target is busy")
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._last_helper_ok is False)

    facts = app._diagnostic_facts()
    assert facts.last_helper_action == "off"
    assert facts.last_helper_ok is False
    assert "target is busy" in facts.last_helper_detail


def test_a_successful_power_off_records_the_marker_without_another_read(
        controller, fakes):
    app, window = running_controller(controller, fakes)
    assert app._marker_present is False
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.POWERED_OFF)
    assert app._marker_present is True
    assert app._diagnostic_facts().marker_present is True


def test_saving_a_report_writes_it_mode_0600(controller, fakes, tmp_path, monkeypatch):
    app, window = running_controller(controller, fakes)
    target = tmp_path / "report.txt"
    monkeypatch.setattr(window_module.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    monkeypatch.setattr(diagnostics, "report_text",
                        lambda facts, *a, **k: "report body\n")

    window._save_diagnostics()
    pump(2.0, until=target.exists)
    assert target.read_text(encoding="utf-8") == "report body\n"
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o600


def test_saving_over_a_loose_mode_file_tightens_it(controller, fakes, tmp_path,
                                                   monkeypatch):
    app, window = running_controller(controller, fakes)
    target = tmp_path / "report.txt"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)
    monkeypatch.setattr(window_module.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    monkeypatch.setattr(diagnostics, "report_text",
                        lambda facts, *a, **k: "fresh\n")

    window._save_diagnostics()
    pump(2.0, until=lambda: target.read_text(encoding="utf-8") == "fresh\n")
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o600


def test_saving_refuses_a_symlink_destination(controller, fakes, tmp_path, monkeypatch,
                                              dialogs):
    app, window = running_controller(controller, fakes)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", encoding="utf-8")
    link = tmp_path / "report.txt"
    link.symlink_to(victim)
    monkeypatch.setattr(window_module.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(link), "")))
    monkeypatch.setattr(diagnostics, "report_text",
                        lambda facts, *a, **k: "fresh\n")

    window._save_diagnostics()
    pump(2.0, until=lambda: dialogs.count("warning") == 1)
    assert victim.read_text(encoding="utf-8") == "do not touch"


def test_the_export_buttons_work_in_every_lifecycle_state(controller, fakes):
    """A failure state is exactly when a report matters, so nothing gates these."""
    fakes.inspect.result = power.DockerStatus("absent", detail="no such object")
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.SETUP)
    window = app._window
    assert window._diag_copy.isEnabled()
    assert window._diag_save.isEnabled()


# ------------------------------- D38: the interrupted transaction (controller) --

def test_a_power_off_timeout_quiesces_instead_of_resuming_polling(controller, fakes):
    """Killing our own sudo is no proof the root helper stopped."""
    app, window = running_controller(controller, fakes)
    fakes.power_off.result = power.HelperResult(
        False, None, "Timed out after 600s…", timed_out=True)

    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.TRANSITION_UNKNOWN)
    assert app._model.phase is lifecycle.Phase.TRANSITION_UNKNOWN
    assert not app._timer.isActive()
    assert app._container_state is None          # not "stopped": we do not know
    assert app._last_snapshot is None            # caches dropped
    assert app._model.desired_action == "off"


def test_retry_repeats_the_interrupted_direction(controller, fakes):
    app, window = running_controller(controller, fakes)
    fakes.power_off.result = power.HelperResult(
        False, None, "Timed out…", timed_out=True)
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.TRANSITION_UNKNOWN)

    before_on, before_off = fakes.power_on.count, fakes.power_off.count
    fakes.power_off.result = power.HelperResult(True, 0, "bridge off")
    app._on_retry_start_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.POWERED_OFF)
    assert fakes.power_off.count == before_off + 1
    assert fakes.power_on.count == before_on          # never the wrong direction


def test_the_only_action_offered_after_an_unknown_transition_is_retry(controller, fakes):
    app, window = running_controller(controller, fakes)
    fakes.power_off.result = power.HelperResult(
        False, None, "Timed out…", timed_out=True)
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.TRANSITION_UNKNOWN)
    assert power.available_action(app._model.phase.value, app._container_state) == \
        power.ACTION_RETRY_TRANSITION


def test_quitting_an_unknown_transition_does_not_call_the_helper(controller, fakes):
    app, window = running_controller(controller, fakes)
    fakes.power_off.result = power.HelperResult(
        False, None, "Timed out…", timed_out=True)
    app._on_power_off_requested()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.TRANSITION_UNKNOWN)
    before = fakes.power_off.count

    app._on_quit_requested()
    pump(0.3)
    assert fakes.power_off.count == before
    assert fakes.quits == [True]


def test_an_ordinary_power_off_failure_still_takes_the_abort_path(controller, fakes):
    """Only a *timeout* is unknown; a helper that answered is not."""
    app, window = running_controller(controller, fakes)
    fakes.power_off.result = power.HelperResult(False, 1, "target is busy")
    app._on_power_off_requested()
    # Wait for the *result*, not merely for the call to have started.
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING
         and fakes.power_off.count == 1)
    assert app._model.phase is lifecycle.Phase.RUNNING
    assert app._timer.isActive()


def test_a_streamed_phase_line_reaches_the_banner(controller, fakes):
    app, window = running_controller(controller, fakes)
    app._begin_busy(app_module.BUSY_STARTING)
    app._on_phase_line("==> Waiting for the guest SMB server")
    assert app._phase_line == "Waiting for the guest SMB server"

    # Non-phase output from the helper is not parsed into anything.
    app._on_phase_line("some incidental warning")
    assert app._phase_line == "Waiting for the guest SMB server"


def test_the_busy_banner_shows_elapsed_time_without_a_percentage(controller, fakes):
    app, window = running_controller(controller, fakes)
    app._begin_busy(app_module.BUSY_STARTING)
    app._busy_since = time.monotonic() - 130
    app._on_phase_line("==> Starting the Windows VM")
    text = app._busy_text()
    assert "2 m 10 s" in text
    assert "Starting the Windows VM" in text
    assert "%" not in text


# ------------------------ D39: the interrupted-provisioning record (controller) --

def test_a_matching_record_resumes_provisioning_without_any_cifs(controller, fakes):
    firstrun.write_provisioning_record(
        firstrun.ProvisioningRecord(started_at="2026-07-26T12:00:00Z",
                                    phase="provisioning", container_id="abc123"))
    fakes.gather.blocked = True
    fakes.read_exclusions.blocked = True
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.PROVISIONING)
    assert app._model.phase is lifecycle.Phase.PROVISIONING
    assert fakes.power_on.count == 0
    pump(0.3)
    assert fakes.gather.count == 0
    assert fakes.read_exclusions.count == 0


def test_a_record_whose_container_vanished_returns_to_setup(controller, fakes):
    firstrun.write_provisioning_record(
        firstrun.ProvisioningRecord(started_at="2026-07-26T12:00:00Z"))
    fakes.inspect.result = power.DockerStatus("absent", detail="no such object")
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.SETUP)
    assert app._record_state == firstrun.RECORD_CONTAINER_GONE
    assert "create it again" in app._setup_detail


def test_a_different_container_is_a_stale_record_with_no_cifs(controller, fakes,
                                                              monkeypatch):
    firstrun.write_provisioning_record(
        firstrun.ProvisioningRecord(started_at="2026-07-26T12:00:00Z",
                                    container_id="original"))
    monkeypatch.setattr(firstrun, "inspect_container_id", Recorder("someone-else"))
    fakes.gather.blocked = True
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.SETUP)
    assert app._record_state == firstrun.RECORD_DIFFERENT
    pump(0.3)
    assert fakes.gather.count == 0


def test_a_malformed_record_enters_setup_and_is_not_deleted(controller, fakes,
                                                            tmp_path):
    backup.ensure_app_dir()
    path = firstrun.provisioning_path()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.SETUP)
    assert app._record_state == firstrun.RECORD_MALFORMED
    assert os.path.exists(path)              # never silently dropped


def test_a_running_container_with_no_record_keeps_existing_behavior(controller, fakes):
    """Externally created, already-configured installs are not reclassified."""
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    assert app._record_state == firstrun.RECORD_ABSENT


def test_a_successful_connect_clears_the_record(controller, fakes, monkeypatch):
    firstrun.write_provisioning_record(
        firstrun.ProvisioningRecord(started_at="2026-07-26T12:00:00Z",
                                    container_id="abc123"))
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.PROVISIONING)
    assert firstrun.read_provisioning_record() is not None

    monkeypatch.setattr(firstrun, "check_host_setup", Recorder([]))
    monkeypatch.setattr(firstrun, "check_container", Recorder([]))
    app._on_connect_requested()
    pump(3.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    assert app._model.phase is lifecycle.Phase.RUNNING
    assert firstrun.read_provisioning_record() is None


def test_a_failed_connect_keeps_the_record(controller, fakes, monkeypatch):
    firstrun.write_provisioning_record(
        firstrun.ProvisioningRecord(started_at="2026-07-26T12:00:00Z",
                                    container_id="abc123"))
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.PROVISIONING)
    fakes.power_on.result = power.HelperResult(False, 1, "no sudo grant")
    monkeypatch.setattr(firstrun, "check_host_setup", Recorder([]))
    monkeypatch.setattr(firstrun, "check_container", Recorder([]))

    app._on_connect_requested()
    pump(3.0, until=lambda: app._model.phase is lifecycle.Phase.START_FAILED)
    assert firstrun.read_provisioning_record() is not None


def test_discard_is_confirmed_removes_only_the_record_and_is_scoped(
        controller, fakes, monkeypatch, dialogs):
    firstrun.write_provisioning_record(
        firstrun.ProvisioningRecord(started_at="2026-07-26T12:00:00Z"))
    fakes.inspect.result = power.DockerStatus("absent", detail="no such object")
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.SETUP)

    # Refused without confirmation.
    monkeypatch.setattr(app_module.Application, "_confirm_discard_record",
                        lambda self: False)
    app._on_discard_record()
    assert firstrun.read_provisioning_record() is not None

    monkeypatch.setattr(app_module.Application, "_confirm_discard_record",
                        lambda self: True)
    app._on_discard_record()
    pump(1.0, until=lambda: firstrun.read_provisioning_record() is None)
    assert firstrun.read_provisioning_record() is None
    # It never removes a container.
    assert all("rm" not in " ".join(call[0][:1]) for call in fakes.inspect.calls)


def test_discard_is_not_offered_while_the_record_still_matches(controller, fakes):
    firstrun.write_provisioning_record(
        firstrun.ProvisioningRecord(started_at="2026-07-26T12:00:00Z",
                                    container_id="abc123"))
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.PROVISIONING)
    app._on_discard_record()
    assert firstrun.read_provisioning_record() is not None


def test_every_effect_handler_runs_without_raising(controller, fakes):
    """The table having a key proves nothing; the handler has to work.

    A handler that raises aborts the rest of its transition's effect list
    halfway, leaving the model in a state the reducer never intended — and Qt
    swallows the traceback, so nothing says so. This is the guard for that.
    """
    app, window = running_controller(controller, fakes)
    for effect, handler in app._EFFECTS.items():
        try:
            handler(app)
        except Exception as exc:                # noqa: BLE001 - that is the test
            raise AssertionError(f"{effect.name} handler raised: {exc!r}") from exc
    app._timer.stop()
    app._end_busy()


# ------------------------------------------- state-column memo (D34 host side) --

def _dir_row(window, path: str):
    """Add a bare directory row the way the real tree builder would."""
    from PySide6.QtWidgets import QTreeWidgetItem
    item = QTreeWidgetItem(window._tree_widget, [path, "", "", "", ""])
    item.setData(0, window_module.ROLE_PATH, path)
    item.setData(0, window_module.ROLE_KIND, "dir")
    window._row_epoch += 1
    return item


def test_state_column_skips_the_walk_when_nothing_feeding_it_moved(controller, fakes):
    """The 5 s tick must not re-render an identical state column.

    Proven by clobbering a cell and showing the refresh leaves it alone: if the
    per-row walk ran, it would overwrite the clobbered text.
    """
    app, window = running_controller(controller, fakes)
    item = _dir_row(window, "Docs")           # "Docs" is the staged exclusion
    window._refresh_state_column()
    rendered = item.text(window_module.COL_STATE)
    assert rendered, "a staged exclusion must render some state text"

    item.setText(window_module.COL_STATE, "clobbered")
    window._refresh_state_column()
    assert item.text(window_module.COL_STATE) == "clobbered"


def test_state_column_re_renders_when_the_selection_changes(controller, fakes):
    app, window = running_controller(controller, fakes)
    item = _dir_row(window, "Docs")
    window._refresh_state_column()
    item.setText(window_module.COL_STATE, "clobbered")

    window._wanted = []                        # the operator un-excluded it
    window._refresh_state_column()
    assert item.text(window_module.COL_STATE) != "clobbered"


def test_state_column_re_renders_when_the_status_changes(controller, fakes):
    app, window = running_controller(controller, fakes)
    item = _dir_row(window, "Docs")
    window._loaded_wanted = ["Docs"]           # already applied, so status drives it
    window._refresh_state_column()
    item.setText(window_module.COL_STATE, "clobbered")

    window._status = {"version": 1,
                      "exclusions": [{"path": "Docs", "state": "applied"}]}
    window._refresh_state_column()
    assert item.text(window_module.COL_STATE) != "clobbered"


def test_a_new_row_is_never_left_with_a_stale_memoized_state(controller, fakes):
    """The failure mode the row epoch exists to prevent.

    A row added after a memoized render must still get its own state cell; if
    the memo key ignored the row set, this cell would stay empty forever.
    """
    app, window = running_controller(controller, fakes)
    _dir_row(window, "Docs")
    window._refresh_state_column()             # memo now armed

    late = _dir_row(window, "Docs/Sub")        # under the excluded root
    window._refresh_state_column()
    assert late.text(window_module.COL_STATE) == "excluded (parent)"


def test_every_row_mutating_path_bumps_the_row_epoch(controller, fakes):
    """Each path that adds or removes rows must invalidate the memo."""
    app, window = running_controller(controller, fakes)
    parent = _dir_row(window, "Docs")

    before = window._row_epoch
    window._more_row(parent, "Docs", 100)
    assert window._row_epoch > before, "_more_row adds a row"

    before = window._row_epoch
    window._rebuild_tree()
    assert window._row_epoch > before, "_rebuild_tree replaces every row"
