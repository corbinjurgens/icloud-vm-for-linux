"""The D29-D31 lifecycle state machine, as a pure reducer.

The controller used to hold this table as a set of `_enter_*` methods calling
Qt widgets directly, which made the highest-risk part of the app — the rule that
no CIFS access happens until the power helper says both shares are live — only
testable through a running Qt event loop. Here it is a function:

    reduce(model, event) -> Transition

:class:`Model` is the whole lifecycle state (the canonical phase, the power-off
continuation, and the operation token). :class:`Transition` is the next model
plus an ordered tuple of :class:`Effect` tokens. The controller keeps ownership
of *doing*: timers, worker threads, dialogs and widgets. It translates a signal
into an :class:`Event`, calls :func:`reduce`, and applies the effects in order.

This module imports no PySide6, runs no subprocess, and performs no mount I/O.

**Stale completions.** Every valid transition increments ``Model.token``. A
worker callback captures the token that was current when its operation was
dispatched and passes it back; :func:`accepts` returns False once anything else
has happened, and the controller drops the callback *before* reducing. That is
how a power-on result arriving after the operator already pressed something else
becomes harmless without a web of "am I still in the right state" checks.

**Unexpected pairs are not silently swallowed.** A current-token event that the
current phase does not expect returns the model unchanged plus
:data:`Effect.REPORT_INVALID_TRANSITION`, so it stays diagnosable. An unknown
phase raises: the phase set is closed, so reaching one is a bug in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Phase(str, Enum):
    """The canonical lifecycle phases (v2 plan D29/D30/D31).

    These are exactly the ``power.LIFECYCLE_*`` constants; ``test_lifecycle``
    asserts the two spellings stay equal. ``inspect_error`` is a *startup
    classification* that enters :data:`Phase.SETUP`, not a phase of its own, and
    "monitoring" / "setup required" are presentation names for
    :data:`Phase.RUNNING` and :data:`Phase.SETUP`.
    """

    STARTING = "starting"
    RUNNING = "running"
    START_FAILED = "start_failed"
    POWERED_OFF = "powered_off"
    SHUTTING_DOWN = "shutting_down"
    SETUP = "setup"
    PROVISIONING = "provisioning"


#: Phases in which no CIFS access may happen at all: a transition owns the
#: mounts, the bridge is intentionally off, or there is nothing mounted to read
#: (D29 extended by D30/D31). Health polling, bridge reads, `ismount()` on the
#: share paths and opening a mount are all forbidden here.
NO_CIFS_PHASES = frozenset({
    Phase.STARTING,
    Phase.SHUTTING_DOWN,
    Phase.POWERED_OFF,
    Phase.START_FAILED,
    Phase.SETUP,
    Phase.PROVISIONING,
})


def is_no_cifs(phase: Phase) -> bool:
    """Whether this phase forbids every kind of mount and bridge I/O."""
    return phase in NO_CIFS_PHASES


class Event(Enum):
    """Something that happened. Payloads (messages) stay with the controller."""

    # Startup inspection (`power.plan_startup` classifications).
    STARTUP_POWER_ON = "startup_power_on"
    STARTUP_ALREADY_ON = "startup_already_on"
    STARTUP_PROVISION_NEEDED = "startup_provision_needed"
    STARTUP_INSPECT_FAILED = "startup_inspect_failed"

    # The power-on transaction.
    USER_START_BRIDGE = "user_start_bridge"
    USER_RETRY_START = "user_retry_start"
    POWER_ON_SUCCEEDED = "power_on_succeeded"
    POWER_ON_FAILED = "power_on_failed"

    # The power-off transaction, with its two continuations.
    USER_POWER_OFF_CONFIRMED = "user_power_off_confirmed"
    QUIT_CONFIRMED_POWER_OFF = "quit_confirmed_power_off"
    QUIT_CONFIRMED_GUI_ONLY = "quit_confirmed_gui_only"
    DRAIN_COMPLETED = "drain_completed"
    DRAIN_TIMED_OUT = "drain_timed_out"
    POWER_OFF_SUCCEEDED = "power_off_succeeded"
    POWER_OFF_FAILED = "power_off_failed"

    # The first-run assistant (D31).
    VM_CREATED = "vm_created"
    CONNECT_READY = "connect_ready"


class Effect(Enum):
    """An imperative token the controller interprets. Never does work itself."""

    # Polling and bridge I/O.
    STOP_POLLING = "stop_polling"
    START_POLLING = "start_polling"
    FORCE_REFRESH = "force_refresh"
    QUIESCE_IO = "quiesce_io"
    PAUSE_IO = "pause_io"
    RESUME_IO = "resume_io"
    RELOAD_SELECTIVE_SYNC = "reload_selective_sync"

    # Window surfaces.
    CLEAR_HEALTH_ROWS = "clear_health_rows"
    HIDE_BANNER = "hide_banner"
    HIDE_NOTICE = "hide_notice"
    SHOW_STARTING_BANNER = "show_starting_banner"
    SHOW_START_FAILED_BANNER = "show_start_failed_banner"
    SHOW_SHUTDOWN_BANNER = "show_shutdown_banner"
    SHOW_POWERED_OFF_BANNER = "show_powered_off_banner"
    SHOW_ABORT_BANNER = "show_abort_banner"
    SHOW_SETUP_TAB = "show_setup_tab"
    HIDE_SETUP_TAB = "hide_setup_tab"
    SHOW_WINDOW = "show_window"
    SHOW_WINDOW_UNLESS_MINIMIZED = "show_window_unless_minimized"

    # Tray presentation.
    TRAY_STARTING = "tray_starting"
    TRAY_RUNNING = "tray_running"
    TRAY_START_FAILED = "tray_start_failed"
    TRAY_SHUTTING_DOWN = "tray_shutting_down"
    TRAY_POWERED_OFF = "tray_powered_off"
    TRAY_SETUP = "tray_setup"

    # Notifications.
    ENABLE_NOTIFICATIONS = "enable_notifications"
    DISABLE_NOTIFICATIONS = "disable_notifications"
    RESET_INCIDENTS = "reset_incidents"
    BEGIN_STARTUP_GRACE = "begin_startup_grace"
    ANNOUNCE_START_FAILURE = "announce_start_failure"

    # Docker classification the controller caches for `power.available_action`.
    MARK_CONTAINER_RUNNING = "mark_container_running"
    MARK_CONTAINER_STOPPED = "mark_container_stopped"
    SYNC_POWER_CONTROLS = "sync_power_controls"

    # The first-run assistant.
    RUN_SETUP_CHECKS = "run_setup_checks"
    CLEAR_SETUP_CHECKS = "clear_setup_checks"
    RENDER_SETUP = "render_setup"

    # The transactions themselves, and the exit.
    RUN_POWER_ON = "run_power_on"
    RUN_POWER_OFF = "run_power_off"
    BEGIN_DRAIN = "begin_drain"
    STOP_DRAIN = "stop_drain"
    EXIT_APP = "exit_app"

    #: A current-token event the phase did not expect. Bounded, non-mutating,
    #: and reported rather than swallowed.
    REPORT_INVALID_TRANSITION = "report_invalid_transition"


#: Effects that change the world rather than the presentation. Used by the tests
#: that assert an inspection failure or a stale callback can never mutate.
MUTATING_EFFECTS = frozenset({
    Effect.RUN_POWER_ON,
    Effect.RUN_POWER_OFF,
    Effect.EXIT_APP,
})

#: Effects that touch CIFS, or schedule something that will. No phase in
#: :data:`NO_CIFS_PHASES` may produce one of these except by first reaching
#: :data:`Phase.RUNNING`.
CIFS_EFFECTS = frozenset({
    Effect.START_POLLING,
    Effect.FORCE_REFRESH,
    Effect.RESUME_IO,
    Effect.RELOAD_SELECTIVE_SYNC,
})


@dataclass(frozen=True)
class Model:
    """The whole lifecycle state. Everything else the controller holds is a cache."""

    phase: Phase = Phase.STARTING
    #: Whether the power-off transaction currently running should exit the app
    #: afterwards (Quit) or leave it idling (D30's keep-running power off).
    exit_after_power_off: bool = True
    #: Incremented by every valid transition; see the module docstring.
    token: int = 0


@dataclass(frozen=True)
class Transition:
    model: Model
    effects: tuple[Effect, ...] = ()


def accepts(model: Model, token: int) -> bool:
    """Whether a completion carrying ``token`` is still the current operation."""
    return token == model.token


# --------------------------------------------------------------- effect sets --
# One tuple per "enter this phase", transcribed from the controller methods they
# replace so the order of operations is preserved exactly.

_BEGIN_STARTUP = (
    Effect.DISABLE_NOTIFICATIONS,
    Effect.RESET_INCIDENTS,
    Effect.STOP_POLLING,
    # quiesce rather than merely pausing: it also stops the window's
    # request/response poller and drops queued list requests, which matters when
    # Start is pressed from the powered-off state.
    Effect.QUIESCE_IO,
    Effect.SHOW_STARTING_BANNER,
    Effect.SYNC_POWER_CONTROLS,
    Effect.TRAY_STARTING,
    Effect.SHOW_WINDOW_UNLESS_MINIMIZED,
    Effect.RUN_POWER_ON,
)

_ENTER_RUNNING = (
    Effect.MARK_CONTAINER_RUNNING,      # the helper just proved it
    Effect.HIDE_SETUP_TAB,
    Effect.HIDE_BANNER,
    Effect.RESUME_IO,
    Effect.TRAY_RUNNING,
    Effect.SYNC_POWER_CONTROLS,
    # The canary is legitimately as old as the bridge was off, so give the host
    # health timer a bounded window before a red snapshot counts as an incident.
    Effect.BEGIN_STARTUP_GRACE,
    Effect.ENABLE_NOTIFICATIONS,
    Effect.RELOAD_SELECTIVE_SYNC,
    Effect.START_POLLING,
    Effect.FORCE_REFRESH,
)

#: ALREADY_ON: the bridge was up before this process started, so there was no
#: off period to forgive a stale canary for — hence no startup grace.
_ENTER_MONITORING = (
    Effect.RESET_INCIDENTS,
    Effect.ENABLE_NOTIFICATIONS,
    Effect.HIDE_SETUP_TAB,
    Effect.RESUME_IO,
    Effect.HIDE_BANNER,
    Effect.TRAY_RUNNING,
    Effect.SYNC_POWER_CONTROLS,
    Effect.RELOAD_SELECTIVE_SYNC,
    Effect.START_POLLING,
    Effect.FORCE_REFRESH,
)

_ENTER_START_FAILED = (
    Effect.DISABLE_NOTIFICATIONS,
    Effect.RESET_INCIDENTS,
    Effect.STOP_POLLING,
    Effect.PAUSE_IO,
    Effect.SHOW_START_FAILED_BANNER,
    Effect.SYNC_POWER_CONTROLS,
    Effect.TRAY_START_FAILED,
    # The tray's busy flag clears the power action, so re-apply it.
    Effect.SYNC_POWER_CONTROLS,
    Effect.ANNOUNCE_START_FAILURE,
)

_ENTER_SETUP = (
    Effect.DISABLE_NOTIFICATIONS,
    Effect.RESET_INCIDENTS,
    Effect.STOP_POLLING,
    Effect.QUIESCE_IO,
    Effect.CLEAR_HEALTH_ROWS,
    Effect.HIDE_BANNER,
    Effect.HIDE_NOTICE,
    Effect.SHOW_SETUP_TAB,
    Effect.TRAY_SETUP,
    Effect.SYNC_POWER_CONTROLS,
    Effect.SHOW_WINDOW_UNLESS_MINIMIZED,
    Effect.RUN_SETUP_CHECKS,
)

_ENTER_PROVISIONING = (
    Effect.MARK_CONTAINER_RUNNING,
    Effect.STOP_POLLING,
    Effect.QUIESCE_IO,
    Effect.SHOW_SETUP_TAB,
    Effect.CLEAR_SETUP_CHECKS,
    Effect.RENDER_SETUP,
    Effect.SYNC_POWER_CONTROLS,
    Effect.SHOW_WINDOW,
)

_BEGIN_POWER_OFF = (
    Effect.DISABLE_NOTIFICATIONS,
    Effect.RESET_INCIDENTS,
    Effect.STOP_POLLING,
    Effect.QUIESCE_IO,
    Effect.SHOW_SHUTDOWN_BANNER,
    Effect.SYNC_POWER_CONTROLS,
    Effect.TRAY_SHUTTING_DOWN,
    Effect.SHOW_WINDOW,
    Effect.BEGIN_DRAIN,
)

_ENTER_POWERED_OFF = (
    Effect.MARK_CONTAINER_STOPPED,
    Effect.DISABLE_NOTIFICATIONS,
    Effect.RESET_INCIDENTS,
    Effect.STOP_POLLING,
    Effect.HIDE_NOTICE,
    Effect.CLEAR_HEALTH_ROWS,
    Effect.SHOW_POWERED_OFF_BANNER,
    Effect.TRAY_POWERED_OFF,
    Effect.SYNC_POWER_CONTROLS,
)

#: Nothing was torn down, so restore the exact running state: polling, I/O,
#: incident announcements and the power controls all as they were.
_ABORT_SHUTDOWN = (
    Effect.ENABLE_NOTIFICATIONS,
    Effect.SHOW_ABORT_BANNER,
    Effect.RESUME_IO,
    Effect.TRAY_RUNNING,
    Effect.SYNC_POWER_CONTROLS,
    Effect.START_POLLING,
    Effect.FORCE_REFRESH,
    Effect.SHOW_WINDOW,
)


def _starting(model: Model, event: Event) -> Transition | None:
    if event is Event.STARTUP_POWER_ON:
        return Transition(_next(model, Phase.STARTING), _BEGIN_STARTUP)
    if event is Event.STARTUP_ALREADY_ON:
        return Transition(_next(model, Phase.RUNNING), _ENTER_MONITORING)
    if event in (Event.STARTUP_PROVISION_NEEDED, Event.STARTUP_INSPECT_FAILED):
        return Transition(_next(model, Phase.SETUP), _ENTER_SETUP)
    if event is Event.POWER_ON_SUCCEEDED:
        return Transition(_next(model, Phase.RUNNING), _ENTER_RUNNING)
    if event is Event.POWER_ON_FAILED:
        return Transition(_next(model, Phase.START_FAILED), _ENTER_START_FAILED)
    if event is Event.QUIT_CONFIRMED_POWER_OFF:
        return Transition(_next(model, Phase.SHUTTING_DOWN, exit_after=True),
                          _BEGIN_POWER_OFF)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        return Transition(_next(model, Phase.STARTING), (Effect.EXIT_APP,))
    return None


def _running(model: Model, event: Event) -> Transition | None:
    if event is Event.USER_POWER_OFF_CONFIRMED:
        return Transition(_next(model, Phase.SHUTTING_DOWN, exit_after=False),
                          _BEGIN_POWER_OFF)
    if event is Event.QUIT_CONFIRMED_POWER_OFF:
        return Transition(_next(model, Phase.SHUTTING_DOWN, exit_after=True),
                          _BEGIN_POWER_OFF)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        return Transition(_next(model, Phase.RUNNING), (Effect.EXIT_APP,))
    if event is Event.USER_START_BRIDGE:
        # D30 recovery from a container someone stopped by hand. The controller
        # only offers this when `power.available_action` says so, which is the
        # one classification table.
        return Transition(_next(model, Phase.STARTING), _BEGIN_STARTUP)
    return None


def _start_failed(model: Model, event: Event) -> Transition | None:
    if event in (Event.USER_RETRY_START, Event.USER_START_BRIDGE):
        return Transition(_next(model, Phase.STARTING), _BEGIN_STARTUP)
    if event is Event.QUIT_CONFIRMED_POWER_OFF:
        return Transition(_next(model, Phase.SHUTTING_DOWN, exit_after=True),
                          _BEGIN_POWER_OFF)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        return Transition(_next(model, Phase.START_FAILED), (Effect.EXIT_APP,))
    return None


def _powered_off(model: Model, event: Event) -> Transition | None:
    if event is Event.USER_START_BRIDGE:
        return Transition(_next(model, Phase.STARTING), _BEGIN_STARTUP)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        # Already off, durably: the marker and the stopped VM outlive this
        # process, so the helper must not be invoked again (D30).
        return Transition(_next(model, Phase.POWERED_OFF), (Effect.EXIT_APP,))
    return None


def _shutting_down(model: Model, event: Event) -> Transition | None:
    if event is Event.DRAIN_COMPLETED:
        return Transition(_next(model, Phase.SHUTTING_DOWN),
                          (Effect.STOP_DRAIN, Effect.RUN_POWER_OFF))
    if event is Event.DRAIN_TIMED_OUT:
        return Transition(_next(model, Phase.RUNNING),
                          (Effect.STOP_DRAIN,) + _ABORT_SHUTDOWN)
    if event is Event.POWER_OFF_SUCCEEDED:
        if model.exit_after_power_off:
            return Transition(_next(model, Phase.POWERED_OFF), (Effect.EXIT_APP,))
        return Transition(_next(model, Phase.POWERED_OFF), _ENTER_POWERED_OFF)
    if event is Event.POWER_OFF_FAILED:
        return Transition(_next(model, Phase.RUNNING), _ABORT_SHUTDOWN)
    return None


def _setup(model: Model, event: Event) -> Transition | None:
    if event is Event.VM_CREATED:
        return Transition(_next(model, Phase.PROVISIONING), _ENTER_PROVISIONING)
    if event is Event.CONNECT_READY:
        return Transition(_next(model, Phase.STARTING),
                          (Effect.HIDE_SETUP_TAB,) + _BEGIN_STARTUP)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        return Transition(_next(model, Phase.SETUP), (Effect.EXIT_APP,))
    return None


def _provisioning(model: Model, event: Event) -> Transition | None:
    if event is Event.CONNECT_READY:
        return Transition(_next(model, Phase.STARTING),
                          (Effect.HIDE_SETUP_TAB,) + _BEGIN_STARTUP)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        # A half-installed Windows guest must not be torn down by quitting the
        # app that is guiding the install (D31).
        return Transition(_next(model, Phase.PROVISIONING), (Effect.EXIT_APP,))
    return None


_TABLE = {
    Phase.STARTING: _starting,
    Phase.RUNNING: _running,
    Phase.START_FAILED: _start_failed,
    Phase.POWERED_OFF: _powered_off,
    Phase.SHUTTING_DOWN: _shutting_down,
    Phase.SETUP: _setup,
    Phase.PROVISIONING: _provisioning,
}


def _next(model: Model, phase: Phase, *, exit_after: bool | None = None) -> Model:
    """The successor model: new phase, bumped token, continuation carried over."""
    return replace(
        model,
        phase=phase,
        token=model.token + 1,
        exit_after_power_off=(model.exit_after_power_off if exit_after is None
                              else exit_after),
    )


def reduce(model: Model, event: Event) -> Transition:
    """Apply ``event`` to ``model``. Pure: no I/O, no Qt, no side effects."""
    handler = _TABLE.get(model.phase)
    if handler is None:                     # pragma: no cover - closed enum
        raise ValueError(f"no lifecycle transitions defined for phase {model.phase!r}")
    transition = handler(model, event)
    if transition is None:
        return Transition(model, (Effect.REPORT_INVALID_TRANSITION,))
    return transition


# ------------------------------------------------------- quit presentation --

#: Which Quit confirmation the phase calls for. Not a phase, and not an effect:
#: the controller asks before there is an event to reduce.
QUIT_IGNORE = "ignore"              # a transaction is already running
QUIT_ALREADY_OFF = "already_off"    # nothing left for the helper to do
QUIT_NOTHING_MOUNTED = "nothing_mounted"    # setup/provisioning
QUIT_THREE_WAY = "three_way"        # power off, GUI only, or cancel


def quit_kind(phase: Phase) -> str:
    """The Quit confirmation this phase needs, before any event exists."""
    if phase is Phase.SHUTTING_DOWN:
        return QUIT_IGNORE
    if phase is Phase.POWERED_OFF:
        return QUIT_ALREADY_OFF
    if phase in (Phase.SETUP, Phase.PROVISIONING):
        return QUIT_NOTHING_MOUNTED
    return QUIT_THREE_WAY
