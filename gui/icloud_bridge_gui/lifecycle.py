"""The D29-D31 lifecycle state machine, as a pure reducer.

The controller used to hold this table as a set of `_enter_*` methods calling
Qt widgets directly, which made the highest-risk part of the app — the rule that
no CIFS access happens until the power helper says both shares are live — only
testable through a running Qt event loop. Here it is a function:

    reduce(model, event) -> Transition

:class:`Model` is the whole lifecycle state (the canonical phase, the power-off
continuation, the guest-provisioning mode, and the operation token).
:class:`Transition` is the next model plus an ordered tuple of :class:`Effect`
tokens. The controller keeps ownership
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
    #: D38. The outer subprocess timeout fired, so we killed an unprivileged
    #: `sudo` — which is no evidence the root helper stopped. The bridge may be
    #: half-reconciled, so everything stays quiesced and cached state is
    #: invalidated until an explicit Retry proves otherwise.
    TRANSITION_UNKNOWN = "transition_unknown"


#: Phases in which no CIFS access may happen at all: a transition owns the
#: mounts, the bridge is intentionally off, or there is nothing mounted to read
#: (D29 extended by D30/D31). Health polling, bridge reads, `ismount()` on the
#: share paths and opening a mount are all forbidden here.
NO_CIFS_PHASES = frozenset({
    Phase.STARTING,
    Phase.TRANSITION_UNKNOWN,
    Phase.SHUTTING_DOWN,
    Phase.POWERED_OFF,
    Phase.START_FAILED,
    Phase.SETUP,
    Phase.PROVISIONING,
})


def is_no_cifs(phase: Phase) -> bool:
    """Whether this phase forbids every kind of mount and bridge I/O."""
    return phase in NO_CIFS_PHASES


#: What a guest-provisioning run is for (v2 plan D43). The same run mechanics
#: serve both, and only the *continuation* differs, so this is carried in the
#: model rather than expressed as a second provisioning phase. It is also the
#: spelling stored in the private record, which is why `firstrun.py` imports
#: these constants instead of writing its own.
MODE_FIRST_RUN = "first-run"
MODE_REPROVISION = "reprovision"
PROVISIONING_MODES = (MODE_FIRST_RUN, MODE_REPROVISION)


class Event(Enum):
    """Something that happened. Payloads (messages) stay with the controller."""

    # Startup inspection (`power.plan_startup` classifications).
    STARTUP_POWER_ON = "startup_power_on"
    STARTUP_ALREADY_ON = "startup_already_on"
    STARTUP_PROVISION_NEEDED = "startup_provision_needed"
    STARTUP_INSPECT_FAILED = "startup_inspect_failed"
    #: D39: a provisioning record matches a live container, so the app re-enters
    #: the no-CIFS Provisioning Windows state it was interrupted in.
    STARTUP_RESUME_PROVISIONING = "startup_resume_provisioning"

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
    #: The outer timeout fired: we do not know what the helper did (D38).
    POWER_TRANSITION_UNKNOWN = "power_transition_unknown"
    USER_RETRY_TRANSITION = "user_retry_transition"

    # The first-run assistant (D31).
    VM_CREATED = "vm_created"
    CONNECT_READY = "connect_ready"

    # App-driven guest provisioning (D40-D44). No new phase: a run reuses the
    # no-CIFS `Phase.PROVISIONING`, entered from Setup for a first run and from
    # Monitoring for a re-provision, and the mode decides only where success
    # goes. These events are the *only* way in: the Qt layer never sets the
    # phase itself.
    PROVISION_BEGIN_FIRST_RUN = "provision_begin_first_run"
    PROVISION_BEGIN_REPROVISION = "provision_begin_reprovision"
    PROVISION_SUCCEEDED = "provision_succeeded"
    PROVISION_FAILED = "provision_failed"


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
    SHOW_UNKNOWN_BANNER = "show_unknown_banner"
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
    TRAY_TRANSITION_UNKNOWN = "tray_transition_unknown"

    # Notifications.
    ENABLE_NOTIFICATIONS = "enable_notifications"
    DISABLE_NOTIFICATIONS = "disable_notifications"
    RESET_INCIDENTS = "reset_incidents"
    BEGIN_STARTUP_GRACE = "begin_startup_grace"
    ANNOUNCE_START_FAILURE = "announce_start_failure"

    # Docker classification the controller caches for `power.available_action`.
    INVALIDATE_CACHES = "invalidate_caches"
    MARK_CONTAINER_UNKNOWN = "mark_container_unknown"
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
    #: Which transaction D38's `transition_unknown` would retry: "on" or "off".
    #: Meaningless in every other phase, and deliberately carried rather than
    #: re-derived, because the phase alone cannot say what was interrupted.
    desired_action: str = "on"
    #: What the current guest-provisioning run is for (D43). Meaningless outside
    #: :data:`Phase.PROVISIONING`, and carried for the same reason
    #: ``desired_action`` is: the phase alone cannot say whether success hands
    #: over to **Check setup and connect** or returns to monitoring.
    provisioning_mode: str = MODE_FIRST_RUN
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

#: Entering a run from **monitoring** (D43's `reprovision`). The bridge is up
#: and its mounts stay mounted, so this is the teardown of the *app's* view of
#: it: notifications off, polling stopped, I/O quiesced, health rows cleared and
#: every cached document/classification dropped, because elevated guest scripts
#: are about to rewrite the share, its ACLs and the agent task. Then exactly the
#: presentation `_ENTER_PROVISIONING` uses.
_BEGIN_REPROVISIONING = (
    Effect.MARK_CONTAINER_RUNNING,
    Effect.DISABLE_NOTIFICATIONS,
    Effect.RESET_INCIDENTS,
    Effect.STOP_POLLING,
    Effect.QUIESCE_IO,
    Effect.CLEAR_HEALTH_ROWS,
    Effect.HIDE_BANNER,
    Effect.HIDE_NOTICE,
    Effect.INVALIDATE_CACHES,
    Effect.SHOW_SETUP_TAB,
    Effect.CLEAR_SETUP_CHECKS,
    Effect.RENDER_SETUP,
    Effect.SYNC_POWER_CONTROLS,
    Effect.SHOW_WINDOW,
)

#: A run that ended without leaving the state: a failure in either mode, or a
#: first-run success. It changes what the Setup tab shows and nothing else —
#: CIFS stays paused, because in `first-run` mode the only honest mountability
#: test is still the operator's explicit **Check setup and connect** (D31).
_RENDER_PROVISIONING_RESULT = (
    Effect.RENDER_SETUP,
    Effect.SYNC_POWER_CONTROLS,
    Effect.SHOW_WINDOW_UNLESS_MINIMIZED,
)

#: D43's other continuation: a `reprovision` run succeeded, so the bridge this
#: app powered on and never unmounted goes back to being monitored. The cache
#: invalidation is what forces the fresh gather that re-checks the protocol and
#: the agent build against the newly installed agent (D35).
_PROVISION_SUCCEEDED_REPROVISION = (Effect.INVALIDATE_CACHES,) + _ENTER_MONITORING

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


#: D38. Nothing here mutates or reads: the helper may still hold its `flock` and
#: still be reconciling mounts, so every cached answer is dropped and the only
#: control left is Retry.
_ENTER_TRANSITION_UNKNOWN = (
    Effect.DISABLE_NOTIFICATIONS,
    Effect.RESET_INCIDENTS,
    Effect.STOP_POLLING,
    Effect.STOP_DRAIN,
    Effect.QUIESCE_IO,
    Effect.CLEAR_HEALTH_ROWS,
    Effect.HIDE_NOTICE,
    Effect.INVALIDATE_CACHES,
    Effect.MARK_CONTAINER_UNKNOWN,
    Effect.SHOW_UNKNOWN_BANNER,
    Effect.TRAY_TRANSITION_UNKNOWN,
    Effect.SYNC_POWER_CONTROLS,
    Effect.SHOW_WINDOW,
)


def _starting(model: Model, event: Event) -> Transition | None:
    if event is Event.STARTUP_POWER_ON:
        return Transition(_next(model, Phase.STARTING), _BEGIN_STARTUP)
    if event is Event.STARTUP_ALREADY_ON:
        return Transition(_next(model, Phase.RUNNING), _ENTER_MONITORING)
    if event in (Event.STARTUP_PROVISION_NEEDED, Event.STARTUP_INSPECT_FAILED):
        return Transition(_next(model, Phase.SETUP), _ENTER_SETUP)
    if event is Event.STARTUP_RESUME_PROVISIONING:
        return Transition(_next(model, Phase.PROVISIONING), _ENTER_PROVISIONING)
    if event is Event.POWER_ON_SUCCEEDED:
        return Transition(_next(model, Phase.RUNNING), _ENTER_RUNNING)
    if event is Event.POWER_ON_FAILED:
        return Transition(_next(model, Phase.START_FAILED), _ENTER_START_FAILED)
    if event is Event.POWER_TRANSITION_UNKNOWN:
        return Transition(_next(model, Phase.TRANSITION_UNKNOWN, desired="on"),
                          _ENTER_TRANSITION_UNKNOWN)
    if event is Event.QUIT_CONFIRMED_POWER_OFF:
        return Transition(_next(model, Phase.SHUTTING_DOWN, exit_after=True),
                          _BEGIN_POWER_OFF)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        return Transition(_next(model, Phase.STARTING), (Effect.EXIT_APP,))
    return None


def _running(model: Model, event: Event) -> Transition | None:
    if event is Event.PROVISION_BEGIN_REPROVISION:
        # D35's recovery and the post-feature-update repair. The mounts stay
        # mounted — the app cannot police another process using them — but this
        # app stops reading them for the duration.
        return Transition(_next(model, Phase.PROVISIONING, mode=MODE_REPROVISION),
                          _BEGIN_REPROVISIONING)
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
    if event is Event.POWER_TRANSITION_UNKNOWN:
        # Emphatically not the abort path: that resumes polling against shares
        # the helper may already have unmounted.
        return Transition(_next(model, Phase.TRANSITION_UNKNOWN, desired="off"),
                          _ENTER_TRANSITION_UNKNOWN)
    return None


def _setup(model: Model, event: Event) -> Transition | None:
    if event in (Event.VM_CREATED, Event.PROVISION_BEGIN_FIRST_RUN):
        # A VM this app just created has nothing configured in it, so both doors
        # out of Setup lead to the same first run.
        return Transition(_next(model, Phase.PROVISIONING, mode=MODE_FIRST_RUN),
                          _ENTER_PROVISIONING)
    if event is Event.CONNECT_READY:
        return Transition(_next(model, Phase.STARTING),
                          (Effect.HIDE_SETUP_TAB,) + _BEGIN_STARTUP)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        return Transition(_next(model, Phase.SETUP), (Effect.EXIT_APP,))
    return None


def _provisioning(model: Model, event: Event) -> Transition | None:
    # Beginning a run from here is the ordinary case, not an oddity: the first
    # run is offered on this very screen once the VM exists, and **Try
    # inspection and repair again** starts a fresh run after a failure. The
    # controller replays the mode the record carries, so a retried
    # re-provisioning does not silently become a first run.
    if event is Event.PROVISION_BEGIN_FIRST_RUN:
        return Transition(_next(model, Phase.PROVISIONING, mode=MODE_FIRST_RUN),
                          _ENTER_PROVISIONING)
    if event is Event.PROVISION_BEGIN_REPROVISION:
        return Transition(_next(model, Phase.PROVISIONING, mode=MODE_REPROVISION),
                          _ENTER_PROVISIONING)
    if event is Event.PROVISION_FAILED:
        return Transition(_next(model, Phase.PROVISIONING),
                          _RENDER_PROVISIONING_RESULT)
    if event is Event.PROVISION_SUCCEEDED:
        if model.provisioning_mode == MODE_REPROVISION:
            return Transition(_next(model, Phase.RUNNING),
                              _PROVISION_SUCCEEDED_REPROVISION)
        # First run: `done` means the guest is configured, not that the host
        # half is. The operator's **Check setup and connect** still runs the
        # helper, and its real CIFS activation is still the only proof.
        return Transition(_next(model, Phase.PROVISIONING),
                          _RENDER_PROVISIONING_RESULT)
    if event is Event.CONNECT_READY:
        return Transition(_next(model, Phase.STARTING),
                          (Effect.HIDE_SETUP_TAB,) + _BEGIN_STARTUP)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        # A half-installed Windows guest must not be torn down by quitting the
        # app that is guiding the install (D31).
        return Transition(_next(model, Phase.PROVISIONING), (Effect.EXIT_APP,))
    return None


def _transition_unknown(model: Model, event: Event) -> Transition | None:
    """Quiesced until an explicit Retry, or a result that resolves the doubt."""
    if event is Event.USER_RETRY_TRANSITION:
        if model.desired_action == "off":
            return Transition(_next(model, Phase.SHUTTING_DOWN), _BEGIN_POWER_OFF)
        return Transition(_next(model, Phase.STARTING), _BEGIN_STARTUP)
    if event is Event.QUIT_CONFIRMED_GUI_ONLY:
        return Transition(_next(model, Phase.TRANSITION_UNKNOWN), (Effect.EXIT_APP,))
    return None


_TABLE = {
    Phase.STARTING: _starting,
    Phase.RUNNING: _running,
    Phase.START_FAILED: _start_failed,
    Phase.POWERED_OFF: _powered_off,
    Phase.SHUTTING_DOWN: _shutting_down,
    Phase.SETUP: _setup,
    Phase.PROVISIONING: _provisioning,
    Phase.TRANSITION_UNKNOWN: _transition_unknown,
}


def _next(model: Model, phase: Phase, *, exit_after: bool | None = None,
          desired: str | None = None, mode: str | None = None) -> Model:
    """The successor model: new phase, bumped token, continuations carried over."""
    return replace(
        model,
        phase=phase,
        token=model.token + 1,
        exit_after_power_off=(model.exit_after_power_off if exit_after is None
                              else exit_after),
        desired_action=(model.desired_action if desired is None else desired),
        provisioning_mode=(model.provisioning_mode if mode is None else mode),
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
#: D38: the helper's outcome is unknown, so offering to run it again from a Quit
#: dialog would be a second guess on top of the first. Quitting is allowed;
#: quitting *and powering off* is not.
QUIT_UNKNOWN = "unknown_transition"


def quit_kind(phase: Phase) -> str:
    """The Quit confirmation this phase needs, before any event exists.

    :data:`Phase.PROVISIONING` answers :data:`QUIT_NOTHING_MOUNTED` in **both**
    modes: a guest whose share, ACLs and agent task are being rewritten must not
    be powered off from a Quit dialog any more than a half-installed one may be
    (D31/D43). During a `reprovision` the shares *are* still mounted, so the
    wording the Qt layer puts on this token has to come from the model's mode,
    not from the token's name.
    """
    if phase is Phase.SHUTTING_DOWN:
        return QUIT_IGNORE
    if phase is Phase.POWERED_OFF:
        return QUIT_ALREADY_OFF
    if phase is Phase.TRANSITION_UNKNOWN:
        return QUIT_UNKNOWN
    if phase in (Phase.SETUP, Phase.PROVISIONING):
        return QUIT_NOTHING_MOUNTED
    return QUIT_THREE_WAY
