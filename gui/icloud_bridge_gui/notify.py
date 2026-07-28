"""When a health snapshot deserves a desktop notification (Qt-free).

The tray icon changes colour silently, which is fine while someone is looking at
it and useless when they are not.  This module decides — and only decides —
whether a snapshot should raise a notification, so the whole policy is a pure
table-driven reducer with no Qt, no timers, and no I/O.

The policy is deliberately quiet:

* ``green``/``yellow``/nothing → **red** notifies once and *latches* an incident;
* while latched, further red or yellow snapshots say nothing (a flapping mount
  must not produce a notification storm);
* a latched incident reaching **green** notifies once and clears the latch.

Yellow neither opens nor closes an incident.  It is the "degraded but working"
state — worth a colour, not worth interrupting someone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .health import GREEN, RED

#: Notification kinds; the tray maps these to Warning/Information icons.
FAILURE = "failure"
RECOVERY = "recovery"

#: Informational provisioning moments use the same tray icon as a recovered
#: health incident.  ``TrayIcon`` deliberately has only warning/information
#: presentation, and these moments need the latter without adding Qt here.
PROVISIONING = RECOVERY

TITLE = "iCloud bridge"

#: After a successful power-on the canary is legitimately as old as the bridge
#: was off, so the first snapshots are expected to be red until the host health
#: timer writes a fresh one.  Announcing that as an incident would notify on
#: every normal start.  The grace is bounded rather than open-ended: a bridge
#: that is *really* broken still notifies, just two minutes later.  It applies
#: only to the power-on transition — an already-running bridge whose first
#: snapshot is red (including a minimized launch) notifies immediately.
STARTUP_GRACE_SECONDS = 120.0


@dataclass(frozen=True)
class Notification:
    kind: str
    title: str
    body: str


def _first_red(checks) -> object | None:
    for check in checks:
        if check.severity == RED:
            return check
    return None


class IncidentTracker:
    """Latching red-incident state machine.  One instance per running GUI."""

    def __init__(self) -> None:
        self._latched = False
        self._grace_until: float | None = None

    # -- state the controller drives ----------------------------------------

    def reset(self) -> None:
        """Forget everything.

        Called when the app enters a state whose health is *expected* to look
        wrong — an intentional power-off, or first-run setup — so that leaving
        that state cannot produce a bogus recovery notification, and re-entering
        monitoring starts from a clean slate.
        """
        self._latched = False
        self._grace_until = None

    def begin_startup_grace(self, now: float, seconds: float = STARTUP_GRACE_SECONDS) -> None:
        """Ignore red for a bounded window after a successful power-on."""
        self._latched = False
        self._grace_until = now + seconds

    @property
    def latched(self) -> bool:
        return self._latched

    def in_grace(self, now: float) -> bool:
        return self._grace_until is not None and now < self._grace_until

    # -- the reducer ---------------------------------------------------------

    def observe(self, overall: str, checks, *, now: float = 0.0) -> Notification | None:
        """Fold one snapshot in; return the notification to show, if any."""
        if overall == RED:
            if self.in_grace(now):
                # Expected post-power-on staleness: neither notify nor latch, so
                # a red that outlives the grace still opens an incident.
                return None
            if self._latched:
                return None
            self._latched = True
            self._grace_until = None
            check = _first_red(checks)
            detail = f"{check.name}: {check.detail}" if check is not None else "a check failed"
            return Notification(FAILURE, TITLE, f"The iCloud bridge has a problem — {detail}")

        if overall == GREEN and self._latched:
            self._latched = False
            self._grace_until = None
            return Notification(RECOVERY, TITLE, "The iCloud bridge is healthy again.")

        # Yellow, or green with no incident open: nothing to say.
        return None


class ProvisioningTracker:
    """Remember the operator-relevant moments already announced for each run."""

    def __init__(self) -> None:
        self._announced: dict[str, set[str]] = {}

    def observe(self, run_id: str, phase: str, *, failed: bool = False) -> Notification | None:
        """Return one notification for a new provisioning moment in ``run_id``."""
        if not run_id:
            return None
        if failed:
            moment = FAILURE
            message = Notification(
                FAILURE, TITLE,
                "Windows provisioning needs attention. Open the status window for details.")
        elif phase == "waiting-for-signin":
            moment = "waiting-for-signin"
            message = Notification(
                PROVISIONING, TITLE,
                "Windows provisioning is waiting for you to sign in to iCloud in the VM.")
        elif phase == "done":
            moment = "done"
            message = Notification(
                PROVISIONING, TITLE,
                "Windows provisioning is complete. Open the status window to connect the shares.")
        else:
            return None
        announced = self._announced.setdefault(run_id, set())
        if moment in announced:
            return None
        announced.add(moment)
        return message
