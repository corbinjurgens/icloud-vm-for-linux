"""The lifecycle reducer's whole transition table, asserted pair by pair.

No Qt, no Docker, no mount: the reducer is a pure function, so every D29-D31
rule it encodes can be checked here rather than through an event loop. The
expected transitions are written out in full — a table you can read against the
plan — instead of being computed from the implementation, which would only
assert that the code equals itself.
"""

from __future__ import annotations

import itertools
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import lifecycle, power  # noqa: E402

E = lifecycle.Event
F = lifecycle.Effect
P = lifecycle.Phase


def model(phase, *, exit_after=True, token=0, mode=lifecycle.MODE_FIRST_RUN):
    return lifecycle.Model(phase=phase, exit_after_power_off=exit_after, token=token,
                           provisioning_mode=mode)


# --------------------------------------------------------- the phase names --

def test_phase_values_match_the_power_constants():
    """One spelling of each phase, so the controller and `available_action` agree."""
    assert P.STARTING.value == power.LIFECYCLE_STARTING
    assert P.RUNNING.value == power.LIFECYCLE_RUNNING
    assert P.START_FAILED.value == power.LIFECYCLE_START_FAILED
    assert P.POWERED_OFF.value == power.LIFECYCLE_POWERED_OFF
    assert P.SHUTTING_DOWN.value == power.LIFECYCLE_SHUTTING_DOWN
    assert P.SETUP.value == power.LIFECYCLE_SETUP
    assert P.PROVISIONING.value == power.LIFECYCLE_PROVISIONING


def test_the_phase_set_is_exactly_the_canonical_ones():
    assert {p.value for p in P} == {
        "starting", "running", "start_failed", "powered_off",
        "shutting_down", "setup", "provisioning", "transition_unknown",
    }


def test_transition_unknown_matches_its_power_constant():
    assert P.TRANSITION_UNKNOWN.value == power.LIFECYCLE_TRANSITION_UNKNOWN


def test_every_phase_has_transitions_defined():
    """An unknown phase fails closed; reaching one is a bug in the table."""
    for phase in P:
        # Any event at all must reduce without raising.
        assert lifecycle.reduce(model(phase), E.POWER_ON_SUCCEEDED) is not None


# --------------------------------------------------- the exhaustive table --
# (phase, event) -> (next phase, effects). Every pair not listed here is an
# invalid transition, and `test_unlisted_pairs_are_invalid` asserts exactly that.

EXPECTED: dict[tuple[P, E], tuple[P, tuple[F, ...]]] = {
    (P.STARTING, E.STARTUP_POWER_ON): (P.STARTING, lifecycle._BEGIN_STARTUP),
    (P.STARTING, E.STARTUP_ALREADY_ON): (P.RUNNING, lifecycle._ENTER_MONITORING),
    (P.STARTING, E.STARTUP_PROVISION_NEEDED): (P.SETUP, lifecycle._ENTER_SETUP),
    (P.STARTING, E.STARTUP_INSPECT_FAILED): (P.SETUP, lifecycle._ENTER_SETUP),
    (P.STARTING, E.STARTUP_RESUME_PROVISIONING): (P.PROVISIONING,
                                                  lifecycle._ENTER_PROVISIONING),
    (P.STARTING, E.POWER_TRANSITION_UNKNOWN): (P.TRANSITION_UNKNOWN,
                                               lifecycle._ENTER_TRANSITION_UNKNOWN),
    (P.STARTING, E.POWER_ON_SUCCEEDED): (P.RUNNING, lifecycle._ENTER_RUNNING),
    (P.STARTING, E.POWER_ON_FAILED): (P.START_FAILED, lifecycle._ENTER_START_FAILED),
    (P.STARTING, E.QUIT_CONFIRMED_POWER_OFF): (P.SHUTTING_DOWN, lifecycle._BEGIN_POWER_OFF),
    (P.STARTING, E.QUIT_CONFIRMED_GUI_ONLY): (P.STARTING, (F.EXIT_APP,)),

    (P.RUNNING, E.USER_POWER_OFF_CONFIRMED): (P.SHUTTING_DOWN, lifecycle._BEGIN_POWER_OFF),
    (P.RUNNING, E.QUIT_CONFIRMED_POWER_OFF): (P.SHUTTING_DOWN, lifecycle._BEGIN_POWER_OFF),
    (P.RUNNING, E.QUIT_CONFIRMED_GUI_ONLY): (P.RUNNING, (F.EXIT_APP,)),
    (P.RUNNING, E.USER_START_BRIDGE): (P.STARTING, lifecycle._BEGIN_STARTUP),
    (P.RUNNING, E.PROVISION_BEGIN_REPROVISION): (P.PROVISIONING,
                                                 lifecycle._BEGIN_REPROVISIONING),

    (P.START_FAILED, E.USER_RETRY_START): (P.STARTING, lifecycle._BEGIN_STARTUP),
    (P.START_FAILED, E.USER_START_BRIDGE): (P.STARTING, lifecycle._BEGIN_STARTUP),
    (P.START_FAILED, E.QUIT_CONFIRMED_POWER_OFF): (P.SHUTTING_DOWN,
                                                   lifecycle._BEGIN_POWER_OFF),
    (P.START_FAILED, E.QUIT_CONFIRMED_GUI_ONLY): (P.START_FAILED, (F.EXIT_APP,)),

    (P.POWERED_OFF, E.USER_START_BRIDGE): (P.STARTING, lifecycle._BEGIN_STARTUP),
    (P.POWERED_OFF, E.QUIT_CONFIRMED_GUI_ONLY): (P.POWERED_OFF, (F.EXIT_APP,)),

    (P.SHUTTING_DOWN, E.DRAIN_COMPLETED): (P.SHUTTING_DOWN,
                                           (F.STOP_DRAIN, F.RUN_POWER_OFF)),
    (P.SHUTTING_DOWN, E.DRAIN_TIMED_OUT): (P.RUNNING,
                                           (F.STOP_DRAIN,) + lifecycle._ABORT_SHUTDOWN),
    (P.SHUTTING_DOWN, E.POWER_OFF_FAILED): (P.RUNNING, lifecycle._ABORT_SHUTDOWN),
    (P.SHUTTING_DOWN, E.POWER_TRANSITION_UNKNOWN): (P.TRANSITION_UNKNOWN,
                                                    lifecycle._ENTER_TRANSITION_UNKNOWN),

    (P.TRANSITION_UNKNOWN, E.QUIT_CONFIRMED_GUI_ONLY): (P.TRANSITION_UNKNOWN,
                                                        (F.EXIT_APP,)),
    # POWER_OFF_SUCCEEDED depends on the continuation; asserted separately.

    (P.SETUP, E.VM_CREATED): (P.PROVISIONING, lifecycle._ENTER_PROVISIONING),
    (P.SETUP, E.PROVISION_BEGIN_FIRST_RUN): (P.PROVISIONING,
                                             lifecycle._ENTER_PROVISIONING),
    (P.SETUP, E.CONNECT_READY): (P.STARTING,
                                 (F.HIDE_SETUP_TAB,) + lifecycle._BEGIN_STARTUP),
    (P.SETUP, E.QUIT_CONFIRMED_GUI_ONLY): (P.SETUP, (F.EXIT_APP,)),

    (P.PROVISIONING, E.PROVISION_BEGIN_FIRST_RUN): (P.PROVISIONING,
                                                    lifecycle._ENTER_PROVISIONING),
    (P.PROVISIONING, E.PROVISION_BEGIN_REPROVISION): (P.PROVISIONING,
                                                      lifecycle._ENTER_PROVISIONING),
    (P.PROVISIONING, E.PROVISION_FAILED): (P.PROVISIONING,
                                           lifecycle._RENDER_PROVISIONING_RESULT),
    (P.PROVISIONING, E.CONNECT_READY): (P.STARTING,
                                        (F.HIDE_SETUP_TAB,) + lifecycle._BEGIN_STARTUP),
    (P.PROVISIONING, E.QUIT_CONFIRMED_GUI_ONLY): (P.PROVISIONING, (F.EXIT_APP,)),
}

#: The continuation-dependent pairs, kept out of the table above.
_CONTINUATION_PAIRS = {
    (P.SHUTTING_DOWN, E.POWER_OFF_SUCCEEDED),
    # D38's Retry repeats whichever direction was interrupted.
    (P.TRANSITION_UNKNOWN, E.USER_RETRY_TRANSITION),
    # D43's success continuation depends on what the run was for.
    (P.PROVISIONING, E.PROVISION_SUCCEEDED),
}


@pytest.mark.parametrize("pair", sorted(EXPECTED, key=lambda k: (k[0].value, k[1].value)))
def test_expected_transition(pair):
    phase, event = pair
    next_phase, effects = EXPECTED[pair]
    transition = lifecycle.reduce(model(phase, token=7), event)
    assert transition.model.phase is next_phase
    assert transition.effects == effects
    # Every valid transition advances the token, which is what makes a
    # superseded worker completion droppable.
    assert transition.model.token == 8


def test_unlisted_pairs_are_invalid_and_report_themselves():
    """No pair silently disappears: the ones not in the table are diagnosable."""
    for phase, event in itertools.product(P, E):
        if (phase, event) in EXPECTED or (phase, event) in _CONTINUATION_PAIRS:
            continue
        before = model(phase, token=3)
        transition = lifecycle.reduce(before, event)
        assert transition.effects == (F.REPORT_INVALID_TRANSITION,), (phase, event)
        assert transition.model == before, (phase, event)
        # An invalid pair must not advance the token, or it would invalidate a
        # perfectly good in-flight operation.
        assert transition.model.token == 3


# ----------------------------------------------- the power-off continuation --

def test_power_off_success_exits_when_quit_started_it():
    transition = lifecycle.reduce(
        model(P.SHUTTING_DOWN, exit_after=True), E.POWER_OFF_SUCCEEDED)
    assert transition.model.phase is P.POWERED_OFF
    assert transition.effects == (F.EXIT_APP,)


def test_power_off_success_idles_when_keep_running_started_it():
    transition = lifecycle.reduce(
        model(P.SHUTTING_DOWN, exit_after=False), E.POWER_OFF_SUCCEEDED)
    assert transition.model.phase is P.POWERED_OFF
    assert transition.effects == lifecycle._ENTER_POWERED_OFF
    assert F.EXIT_APP not in transition.effects


def test_the_continuation_is_recorded_when_the_transaction_starts():
    keep = lifecycle.reduce(model(P.RUNNING), E.USER_POWER_OFF_CONFIRMED)
    assert keep.model.exit_after_power_off is False
    quit_off = lifecycle.reduce(model(P.RUNNING, exit_after=False),
                                E.QUIT_CONFIRMED_POWER_OFF)
    assert quit_off.model.exit_after_power_off is True


def test_the_continuation_survives_the_drain():
    """A model that reaches the helper still knows which ending it wanted."""
    started = lifecycle.reduce(model(P.RUNNING), E.USER_POWER_OFF_CONFIRMED).model
    drained = lifecycle.reduce(started, E.DRAIN_COMPLETED).model
    assert drained.exit_after_power_off is False
    done = lifecycle.reduce(drained, E.POWER_OFF_SUCCEEDED)
    assert done.effects == lifecycle._ENTER_POWERED_OFF


# --------------------------------------------------------- the stale token --

def test_accepts_only_the_current_token():
    start = model(P.STARTING, token=4)
    assert lifecycle.accepts(start, 4)
    assert not lifecycle.accepts(start, 3)
    assert not lifecycle.accepts(start, 5)


def test_a_superseded_operation_is_no_longer_accepted():
    """Press Start, then have the old power-on land: its token is stale."""
    off = model(P.POWERED_OFF, token=11)
    started = lifecycle.reduce(off, E.USER_START_BRIDGE).model
    assert not lifecycle.accepts(started, 11)
    assert lifecycle.accepts(started, 12)


# -------------------------------------------------------- the D29-D31 rules --

def test_the_interrupted_transaction_retries_the_direction_it_was_going():
    """D38: Retry means "do that again", not "power on"."""
    off = lifecycle.reduce(model(P.SHUTTING_DOWN, exit_after=False),
                           E.POWER_TRANSITION_UNKNOWN).model
    assert off.desired_action == "off"
    retried = lifecycle.reduce(off, E.USER_RETRY_TRANSITION)
    assert retried.model.phase is P.SHUTTING_DOWN
    assert retried.effects == lifecycle._BEGIN_POWER_OFF

    on = lifecycle.reduce(model(P.STARTING), E.POWER_TRANSITION_UNKNOWN).model
    assert on.desired_action == "on"
    assert lifecycle.reduce(on, E.USER_RETRY_TRANSITION).effects == lifecycle._BEGIN_STARTUP


def test_an_unknown_transition_never_resumes_io_and_drops_its_caches():
    """The whole point: a killed sudo is no proof the root helper stopped."""
    effects = lifecycle._ENTER_TRANSITION_UNKNOWN
    assert not (set(effects) & lifecycle.CIFS_EFFECTS)
    assert not (set(effects) & lifecycle.MUTATING_EFFECTS)
    assert F.INVALIDATE_CACHES in effects
    assert F.MARK_CONTAINER_UNKNOWN in effects
    assert F.STOP_DRAIN in effects


def test_a_power_off_timeout_does_not_take_the_abort_path():
    """Aborting resumes polling against shares that may already be gone."""
    timed_out = lifecycle.reduce(model(P.SHUTTING_DOWN), E.POWER_TRANSITION_UNKNOWN)
    assert timed_out.model.phase is P.TRANSITION_UNKNOWN
    assert timed_out.effects != lifecycle._ABORT_SHUTDOWN
    assert F.START_POLLING not in timed_out.effects


def test_quitting_an_unknown_transition_never_calls_the_helper():
    assert lifecycle.quit_kind(P.TRANSITION_UNKNOWN) == lifecycle.QUIT_UNKNOWN
    transition = lifecycle.reduce(model(P.TRANSITION_UNKNOWN), E.QUIT_CONFIRMED_GUI_ONLY)
    assert transition.effects == (F.EXIT_APP,)
    assert lifecycle.reduce(model(P.TRANSITION_UNKNOWN),
                            E.QUIT_CONFIRMED_POWER_OFF).effects == (
        F.REPORT_INVALID_TRANSITION,)


def test_no_cifs_phases_are_exactly_everything_but_running():
    assert lifecycle.NO_CIFS_PHASES == set(P) - {P.RUNNING}
    assert not lifecycle.is_no_cifs(P.RUNNING)
    for phase in lifecycle.NO_CIFS_PHASES:
        assert lifecycle.is_no_cifs(phase)


@pytest.mark.parametrize("phase", [P.SETUP, P.PROVISIONING, P.POWERED_OFF,
                                   P.START_FAILED, P.TRANSITION_UNKNOWN])
def test_no_cifs_phase_never_reaches_polling_without_a_successful_power_on(phase):
    """The central D29 rule: nothing schedules bridge I/O from a no-CIFS state.

    `STARTING` and `SHUTTING_DOWN` are excluded because they are the two states
    that legitimately end in `RUNNING` — `POWER_ON_SUCCEEDED` and an aborted
    teardown that restores what was already mounted. `PROVISIONING` is checked
    in its `first-run` mode here, where the helper has never run; its one
    `reprovision` exit is asserted separately below.
    """
    for event in E:
        transition = lifecycle.reduce(model(phase), event)
        assert not (set(transition.effects) & lifecycle.CIFS_EFFECTS), (phase, event)


def test_starting_reaches_cifs_effects_only_through_power_on_succeeded():
    reaching = {event for event in E
                if set(lifecycle.reduce(model(P.STARTING), event).effects)
                & lifecycle.CIFS_EFFECTS}
    # ALREADY_ON is the startup classification for a bridge that was already up
    # before this process existed; the helper is not involved and none is needed.
    assert reaching == {E.POWER_ON_SUCCEEDED, E.STARTUP_ALREADY_ON}


def test_quit_from_powered_off_exits_without_invoking_the_helper():
    transition = lifecycle.reduce(model(P.POWERED_OFF), E.QUIT_CONFIRMED_GUI_ONLY)
    assert transition.effects == (F.EXIT_APP,)
    assert F.RUN_POWER_OFF not in transition.effects
    # And there is no way to ask for one from here at all.
    assert lifecycle.reduce(model(P.POWERED_OFF),
                            E.QUIT_CONFIRMED_POWER_OFF).effects == (
        F.REPORT_INVALID_TRANSITION,)


@pytest.mark.parametrize("event", [E.DRAIN_TIMED_OUT, E.POWER_OFF_FAILED])
def test_a_failed_power_off_restores_the_running_state(event):
    transition = lifecycle.reduce(model(P.SHUTTING_DOWN, exit_after=True), event)
    assert transition.model.phase is P.RUNNING
    assert F.RESUME_IO in transition.effects
    assert F.START_POLLING in transition.effects
    assert F.ENABLE_NOTIFICATIONS in transition.effects
    assert F.TRAY_RUNNING in transition.effects
    # Nothing was torn down, so the app must not exit even though Quit asked for it.
    assert F.EXIT_APP not in transition.effects


def test_inspect_failure_produces_no_mutating_effect():
    transition = lifecycle.reduce(model(P.STARTING), E.STARTUP_INSPECT_FAILED)
    assert transition.model.phase is P.SETUP
    assert not (set(transition.effects) & lifecycle.MUTATING_EFFECTS)
    assert not (set(transition.effects) & lifecycle.CIFS_EFFECTS)


def test_provisioning_calls_the_helper_only_on_the_explicit_connect():
    """D31: the initial Windows install outlasts the helper's readiness deadline.

    So nothing about *entering* or *waiting in* this state may call `on`. The one
    exception is the operator pressing **Check setup and connect**, which is the
    statement that the guest sequence is finished.
    """
    for event in E:
        effects = lifecycle.reduce(model(P.PROVISIONING), event).effects
        assert F.RUN_POWER_OFF not in effects
        if event is not E.CONNECT_READY:
            assert F.RUN_POWER_ON not in effects, event
    assert F.RUN_POWER_ON not in lifecycle._ENTER_PROVISIONING


# ------------------------------ app-driven guest provisioning (D40-D44) --

def test_a_run_can_be_entered_from_setup_and_from_monitoring():
    """One state, two doors: D31's assistant and D35's recovery action."""
    from_setup = lifecycle.reduce(model(P.SETUP), E.PROVISION_BEGIN_FIRST_RUN)
    assert from_setup.model.phase is P.PROVISIONING
    assert from_setup.model.provisioning_mode == lifecycle.MODE_FIRST_RUN

    from_monitoring = lifecycle.reduce(model(P.RUNNING), E.PROVISION_BEGIN_REPROVISION)
    assert from_monitoring.model.phase is P.PROVISIONING
    assert from_monitoring.model.provisioning_mode == lifecycle.MODE_REPROVISION


def test_beginning_a_run_from_monitoring_stops_reading_the_bridge():
    """Elevated scripts are about to rewrite the share, its ACLs and the agent."""
    effects = lifecycle.reduce(model(P.RUNNING), E.PROVISION_BEGIN_REPROVISION).effects
    assert not (set(effects) & lifecycle.CIFS_EFFECTS)
    assert not (set(effects) & lifecycle.MUTATING_EFFECTS)
    for expected in (F.STOP_POLLING, F.QUIESCE_IO, F.DISABLE_NOTIFICATIONS,
                     F.RESET_INCIDENTS, F.CLEAR_HEALTH_ROWS, F.INVALIDATE_CACHES):
        assert expected in effects
    assert effects.index(F.STOP_POLLING) < effects.index(F.QUIESCE_IO)


@pytest.mark.parametrize("mode", [lifecycle.MODE_FIRST_RUN, lifecycle.MODE_REPROVISION])
def test_a_run_in_flight_keeps_cifs_paused_in_both_modes(mode):
    """Only a *finished* reprovision may resume I/O; nothing else in here may."""
    for event in (E.PROVISION_BEGIN_FIRST_RUN, E.PROVISION_BEGIN_REPROVISION,
                  E.PROVISION_FAILED):
        effects = lifecycle.reduce(model(P.PROVISIONING, mode=mode), event).effects
        assert not (set(effects) & lifecycle.CIFS_EFFECTS), event
        assert not (set(effects) & lifecycle.MUTATING_EFFECTS), event


def test_a_first_run_success_waits_for_the_explicit_connect():
    """D31/D43: `done` says the guest is configured, not that the host half is."""
    transition = lifecycle.reduce(
        model(P.PROVISIONING, mode=lifecycle.MODE_FIRST_RUN), E.PROVISION_SUCCEEDED)
    assert transition.model.phase is P.PROVISIONING
    assert transition.effects == lifecycle._RENDER_PROVISIONING_RESULT
    assert not (set(transition.effects) & lifecycle.CIFS_EFFECTS)
    assert F.RUN_POWER_ON not in transition.effects
    # And the way on is still the same explicit action it always was.
    assert lifecycle.reduce(transition.model, E.CONNECT_READY).model.phase is P.STARTING


def test_a_reprovision_success_returns_to_monitoring():
    transition = lifecycle.reduce(
        model(P.PROVISIONING, mode=lifecycle.MODE_REPROVISION), E.PROVISION_SUCCEEDED)
    assert transition.model.phase is P.RUNNING
    assert transition.effects == lifecycle._PROVISION_SUCCEEDED_REPROVISION
    # The fresh gather that re-checks the protocol and the agent build (D35)
    # only means anything against dropped caches.
    assert transition.effects[0] is F.INVALIDATE_CACHES
    for expected in (F.HIDE_SETUP_TAB, F.RESUME_IO, F.START_POLLING, F.FORCE_REFRESH,
                     F.ENABLE_NOTIFICATIONS, F.TRAY_RUNNING):
        assert expected in transition.effects
    # It never calls the helper: the bridge was never powered off.
    assert not (set(transition.effects) & lifecycle.MUTATING_EFFECTS)


def test_a_failure_stays_in_the_state_so_the_run_can_be_retried():
    failed = lifecycle.reduce(
        model(P.PROVISIONING, mode=lifecycle.MODE_REPROVISION), E.PROVISION_FAILED)
    assert failed.model.phase is P.PROVISIONING
    # A retry replays the mode the record carries, so a retried re-provisioning
    # does not silently become a first run.
    retried = lifecycle.reduce(failed.model, E.PROVISION_BEGIN_REPROVISION)
    assert retried.model.provisioning_mode == lifecycle.MODE_REPROVISION
    assert lifecycle.reduce(retried.model, E.PROVISION_SUCCEEDED).model.phase is P.RUNNING


def test_the_mode_survives_events_that_do_not_set_it():
    """It is carried, not re-derived: the phase alone cannot say what a run is for."""
    begun = lifecycle.reduce(model(P.RUNNING), E.PROVISION_BEGIN_REPROVISION).model
    assert lifecycle.reduce(begun, E.PROVISION_FAILED).model.provisioning_mode == \
        lifecycle.MODE_REPROVISION


def test_creating_the_vm_is_always_a_first_run():
    assert lifecycle.reduce(
        model(P.SETUP, mode=lifecycle.MODE_REPROVISION),
        E.VM_CREATED).model.provisioning_mode == lifecycle.MODE_FIRST_RUN


@pytest.mark.parametrize("phase", [p for p in P if p not in (P.SETUP, P.PROVISIONING)])
def test_a_first_run_cannot_be_started_from_anywhere_else(phase):
    assert lifecycle.reduce(model(phase), E.PROVISION_BEGIN_FIRST_RUN).effects == (
        F.REPORT_INVALID_TRANSITION,)


@pytest.mark.parametrize("phase", [p for p in P if p not in (P.RUNNING, P.PROVISIONING)])
def test_a_reprovision_cannot_be_started_from_anywhere_else(phase):
    assert lifecycle.reduce(model(phase), E.PROVISION_BEGIN_REPROVISION).effects == (
        F.REPORT_INVALID_TRANSITION,)


@pytest.mark.parametrize("event", [E.PROVISION_SUCCEEDED, E.PROVISION_FAILED])
@pytest.mark.parametrize("phase", [p for p in P if p is not P.PROVISIONING])
def test_a_run_result_outside_the_provisioning_state_is_invalid(phase, event):
    """A result can only belong to a run, and a run is only ever that state."""
    assert lifecycle.reduce(model(phase), event).effects == (
        F.REPORT_INVALID_TRANSITION,)


def test_the_provisioning_modes_are_the_two_the_record_stores():
    assert lifecycle.PROVISIONING_MODES == (lifecycle.MODE_FIRST_RUN,
                                            lifecycle.MODE_REPROVISION)


def test_the_power_on_transaction_always_pauses_io_before_running_it():
    """Whichever door it comes through, the helper is called after quiescing."""
    effects = lifecycle._BEGIN_STARTUP
    assert effects.index(F.QUIESCE_IO) < effects.index(F.RUN_POWER_ON)
    assert effects.index(F.STOP_POLLING) < effects.index(F.QUIESCE_IO)
    assert effects[-1] is F.RUN_POWER_ON


def test_the_power_off_transaction_drains_before_it_unmounts():
    effects = lifecycle._BEGIN_POWER_OFF
    assert F.RUN_POWER_OFF not in effects       # only DRAIN_COMPLETED starts it
    assert effects.index(F.QUIESCE_IO) < effects.index(F.BEGIN_DRAIN)
    assert effects[-1] is F.BEGIN_DRAIN


# ------------------------------------------------------ quit presentation --

@pytest.mark.parametrize("phase,expected", [
    (P.SHUTTING_DOWN, lifecycle.QUIT_IGNORE),
    (P.POWERED_OFF, lifecycle.QUIT_ALREADY_OFF),
    (P.SETUP, lifecycle.QUIT_NOTHING_MOUNTED),
    (P.PROVISIONING, lifecycle.QUIT_NOTHING_MOUNTED),
    (P.RUNNING, lifecycle.QUIT_THREE_WAY),
    (P.STARTING, lifecycle.QUIT_THREE_WAY),
    (P.START_FAILED, lifecycle.QUIT_THREE_WAY),
])
def test_quit_kind(phase, expected):
    assert lifecycle.quit_kind(phase) == expected
