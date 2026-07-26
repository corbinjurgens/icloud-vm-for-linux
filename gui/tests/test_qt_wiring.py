"""Thin PySide6 integration tests: the signal wiring the pure suite cannot see.

`test_lifecycle.py` proves the transition table. What it cannot prove is that the
controller *applies* it — that startup really schedules no CIFS work before the
power-on resolves, that a stale worker signal really is discarded, that closing
the window really routes where it should. Those need widgets, timers and a
thread pool, so they live here.

Everything expensive or dangerous is faked: `power`, `bridge` and `health` are
monkeypatched, the modal dialogs are replaced, and no docker, sudo or mount is
ever touched. The whole file is skipped when PySide6 is absent, which is what
keeps `AGENTS.md`'s with-and-without-Qt rule satisfiable.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")

# Must be set before the first Qt import, or Qt binds to a real display that
# does not exist on a build machine.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDeadlineTimer, QThreadPool, QTimer   # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

from icloud_bridge_gui import __main__ as app_module              # noqa: E402
from icloud_bridge_gui import bridge, health, lifecycle, listing, power  # noqa: E402


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


# --------------------------------------------------------------- fixtures --

@pytest.fixture(scope="module")
def qapp():
    """One QApplication for the module; Qt forbids constructing a second."""
    existing = QApplication.instance()
    application = existing or QApplication(["test"])
    yield application
    QThreadPool.globalInstance().waitForDone(5000)


@pytest.fixture
def fakes(monkeypatch):
    """Replace every subprocess, mount and dialog the controller can reach."""
    state = type("Fakes", (), {})()
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
    app = controller()
    pump(2.0, until=lambda: app._model.phase is lifecycle.Phase.RUNNING)
    window = app._window
    # A loaded selection with a staged change, so Apply would otherwise be live.
    window._loaded_wanted = []
    window._loaded_revision = 3
    window._config_error = None
    window._wanted = ["Docs"]
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
