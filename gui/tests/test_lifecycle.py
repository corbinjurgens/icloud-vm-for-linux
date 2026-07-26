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


def model(phase, *, exit_after=True, token=0):
    return lifecycle.Model(phase=phase, exit_after_power_off=exit_after, token=token)


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


def test_the_phase_set_is_exactly_the_seven_canonical_ones():
    assert {p.value for p in P} == {
        "starting", "running", "start_failed", "powered_off",
        "shutting_down", "setup", "provisioning",
    }


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
    (P.STARTING, E.POWER_ON_SUCCEEDED): (P.RUNNING, lifecycle._ENTER_RUNNING),
    (P.STARTING, E.POWER_ON_FAILED): (P.START_FAILED, lifecycle._ENTER_START_FAILED),
    (P.STARTING, E.QUIT_CONFIRMED_POWER_OFF): (P.SHUTTING_DOWN, lifecycle._BEGIN_POWER_OFF),
    (P.STARTING, E.QUIT_CONFIRMED_GUI_ONLY): (P.STARTING, (F.EXIT_APP,)),

    (P.RUNNING, E.USER_POWER_OFF_CONFIRMED): (P.SHUTTING_DOWN, lifecycle._BEGIN_POWER_OFF),
    (P.RUNNING, E.QUIT_CONFIRMED_POWER_OFF): (P.SHUTTING_DOWN, lifecycle._BEGIN_POWER_OFF),
    (P.RUNNING, E.QUIT_CONFIRMED_GUI_ONLY): (P.RUNNING, (F.EXIT_APP,)),
    (P.RUNNING, E.USER_START_BRIDGE): (P.STARTING, lifecycle._BEGIN_STARTUP),

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
    # POWER_OFF_SUCCEEDED depends on the continuation; asserted separately.

    (P.SETUP, E.VM_CREATED): (P.PROVISIONING, lifecycle._ENTER_PROVISIONING),
    (P.SETUP, E.CONNECT_READY): (P.STARTING,
                                 (F.HIDE_SETUP_TAB,) + lifecycle._BEGIN_STARTUP),
    (P.SETUP, E.QUIT_CONFIRMED_GUI_ONLY): (P.SETUP, (F.EXIT_APP,)),

    (P.PROVISIONING, E.CONNECT_READY): (P.STARTING,
                                        (F.HIDE_SETUP_TAB,) + lifecycle._BEGIN_STARTUP),
    (P.PROVISIONING, E.QUIT_CONFIRMED_GUI_ONLY): (P.PROVISIONING, (F.EXIT_APP,)),
}

#: The continuation-dependent pair, kept out of the table above.
_CONTINUATION_PAIRS = {(P.SHUTTING_DOWN, E.POWER_OFF_SUCCEEDED)}


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

def test_no_cifs_phases_are_exactly_everything_but_running():
    assert lifecycle.NO_CIFS_PHASES == set(P) - {P.RUNNING}
    assert not lifecycle.is_no_cifs(P.RUNNING)
    for phase in lifecycle.NO_CIFS_PHASES:
        assert lifecycle.is_no_cifs(phase)


@pytest.mark.parametrize("phase", [P.SETUP, P.PROVISIONING, P.POWERED_OFF,
                                   P.START_FAILED])
def test_no_cifs_phase_never_reaches_polling_without_a_successful_power_on(phase):
    """The central D29 rule: nothing schedules bridge I/O from a no-CIFS state.

    `STARTING` and `SHUTTING_DOWN` are excluded because they are the two states
    that legitimately end in `RUNNING` — `POWER_ON_SUCCEEDED` and an aborted
    teardown that restores what was already mounted.
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
