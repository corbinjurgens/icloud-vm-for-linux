"""Notification-policy tests: the latching incident reducer, no Qt required."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import health, notify  # noqa: E402


def rows(*severities) -> list[health.Check]:
    return [health.Check(f"check{i}", sev, f"detail {i}")
            for i, sev in enumerate(severities)]


def observe(tracker, overall, *severities, now=0.0):
    return tracker.observe(overall, rows(*severities), now=now)


# ------------------------------------------------------------ the core table --

@pytest.mark.parametrize("sequence,expected", [
    # (overall states in order) -> (notification kind per step, None = silent)
    ([health.GREEN], [None]),
    ([health.YELLOW], [None]),
    ([health.RED], [notify.FAILURE]),
    ([health.GREEN, health.RED], [None, notify.FAILURE]),
    ([health.YELLOW, health.RED], [None, notify.FAILURE]),
    # red -> red / red -> yellow never repeats
    ([health.RED, health.RED], [notify.FAILURE, None]),
    ([health.RED, health.YELLOW], [notify.FAILURE, None]),
    ([health.RED, health.YELLOW, health.RED], [notify.FAILURE, None, None]),
    # only green clears the latch, and only once
    ([health.RED, health.GREEN], [notify.FAILURE, notify.RECOVERY]),
    ([health.RED, health.GREEN, health.GREEN], [notify.FAILURE, notify.RECOVERY, None]),
    # a second incident after recovery notifies again
    ([health.RED, health.GREEN, health.RED],
     [notify.FAILURE, notify.RECOVERY, notify.FAILURE]),
    # yellow alone never opens an incident, so green after it says nothing
    ([health.YELLOW, health.GREEN], [None, None]),
])
def test_incident_sequences(sequence, expected):
    tracker = notify.IncidentTracker()
    seen = []
    for overall in sequence:
        message = observe(tracker, overall, overall)
        seen.append(None if message is None else message.kind)
    assert seen == expected


def test_failure_body_names_the_first_red_check():
    tracker = notify.IncidentTracker()
    checks = [health.Check("Guest agent", health.YELLOW, "stale"),
              health.Check("iCloud mount", health.RED, "/mnt/icloud is not mounted"),
              health.Check("Guest write canary", health.RED, "canary is stale")]
    message = tracker.observe(health.RED, checks)
    assert message.kind == notify.FAILURE
    assert message.title == "iCloud bridge"
    assert "iCloud mount: /mnt/icloud is not mounted" in message.body
    assert "canary is stale" not in message.body


def test_failure_body_survives_an_empty_check_list():
    message = notify.IncidentTracker().observe(health.RED, [])
    assert message.kind == notify.FAILURE
    assert "a check failed" in message.body


def test_recovery_body_is_not_a_second_warning():
    tracker = notify.IncidentTracker()
    observe(tracker, health.RED, health.RED)
    message = observe(tracker, health.GREEN, health.GREEN)
    assert message.kind == notify.RECOVERY
    assert "healthy again" in message.body


# ------------------------------------------------------------------ resets ----

def test_reset_drops_a_latched_incident_without_a_recovery():
    """Entering powered-off/setup must not later announce a bogus recovery."""
    tracker = notify.IncidentTracker()
    observe(tracker, health.RED, health.RED)
    tracker.reset()
    assert tracker.latched is False
    assert observe(tracker, health.GREEN, health.GREEN) is None


def test_reset_lets_the_next_red_notify_again():
    tracker = notify.IncidentTracker()
    observe(tracker, health.RED, health.RED)
    tracker.reset()
    assert observe(tracker, health.RED, health.RED).kind == notify.FAILURE


# ------------------------------------------------------------ startup grace ---

def test_grace_suppresses_the_expected_post_power_on_red():
    tracker = notify.IncidentTracker()
    tracker.begin_startup_grace(now=1000.0)
    assert observe(tracker, health.RED, health.RED, now=1000.0) is None
    assert observe(tracker, health.RED, health.RED, now=1100.0) is None
    # Nothing was latched, so a red that outlives the grace still opens one.
    assert tracker.latched is False


def test_red_outliving_the_grace_still_notifies():
    tracker = notify.IncidentTracker()
    tracker.begin_startup_grace(now=1000.0)
    observe(tracker, health.RED, health.RED, now=1000.0)
    later = 1000.0 + notify.STARTUP_GRACE_SECONDS + 1
    assert observe(tracker, health.RED, health.RED, now=later).kind == notify.FAILURE


def test_green_during_grace_clears_it_without_a_recovery_message():
    tracker = notify.IncidentTracker()
    tracker.begin_startup_grace(now=1000.0)
    assert observe(tracker, health.GREEN, health.GREEN, now=1010.0) is None
    assert tracker.in_grace(1010.0) is True   # green is silent, grace still runs


def test_no_grace_means_a_first_red_snapshot_notifies():
    """An already-running bridge (e.g. a minimized launch) is not in grace."""
    tracker = notify.IncidentTracker()
    assert observe(tracker, health.RED, health.RED, now=1000.0).kind == notify.FAILURE
