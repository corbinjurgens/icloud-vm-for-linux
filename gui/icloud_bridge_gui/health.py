"""Host-side health model (v2 plan section 6.2 and D23).

Split in two deliberately:

* ``build_checks`` is pure — it turns already-gathered facts into the list of
  rows the Status tab shows and the severity the tray icon uses.  Everything
  about the precedence rules is testable without docker, a mount, or Qt.
* ``gather`` does the I/O.  It is called from a worker thread because a sick
  CIFS mount can block even ``os.path.ismount``.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from . import bridge, power

GREEN = "green"
YELLOW = "yellow"
RED = "red"
_ORDER = {GREEN: 0, YELLOW: 1, RED: 2}

CONTAINER_NAME = "icloud-windows"
DOCKER_TIMEOUT_SECONDS = 5
#: How often the container check may actually run, independent of the refresh
#: cadence.  See ``ContainerProbe``.
CONTAINER_POLL_INTERVAL_SECONDS = 15

# The v1 health timer refreshes the canary every ten minutes; 15 gives that
# schedule five minutes of slack before we call the guest dead.
CANARY_MAX_AGE_SECONDS = 900
CANARY_MAX_FUTURE_SECONDS = 300
AGENT_MAX_AGE_SECONDS = 90
AGENT_MAX_FUTURE_SECONDS = 60
TREE_MAX_AGE_SECONDS = 1200
REVISION_LAG_GRACE_SECONDS = 300
DISK_FLOOR_BYTES = 20 * 1024 ** 3

PENDING_EXCLUSION_STATES = ("applying", "pending-dehydrate", "not-found")


@dataclass(frozen=True)
class Check:
    name: str
    severity: str
    detail: str


def overall(checks: Iterable[Check]) -> str:
    """Worst severity wins (v2 plan D23)."""
    worst = GREEN
    for check in checks:
        if _ORDER[check.severity] > _ORDER[worst]:
            worst = check.severity
    return worst


def parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 UTC stamp, returning ``None`` for anything unusable."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def human_bytes(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ------------------------------------------------------------ pure evaluators --

def _check_container(running: Any, detail: str) -> Check:
    if running is True:
        return Check("Windows VM", GREEN, f"container {CONTAINER_NAME} is running")
    return Check("Windows VM", RED, detail or f"container {CONTAINER_NAME} is not running")


def _check_mount(label: str, path: str, mounted: bool) -> Check:
    if mounted:
        return Check(label, GREEN, f"{path} mounted")
    return Check(label, RED, f"{path} is not mounted")


def _check_canary(exists: bool, mtime: float | None, now: datetime) -> Check:
    path = os.path.join(bridge.mount_dir(), ".linux-canary")
    if not exists or mtime is None:
        return Check("Guest write canary", RED, f"{path} missing — the health timer has not written it")
    age = now.timestamp() - mtime
    if age < -CANARY_MAX_FUTURE_SECONDS:
        return Check("Guest write canary", RED,
                     f"canary is dated {abs(age) / 60:.0f} min in the future — guest/host clock error")
    if age > CANARY_MAX_AGE_SECONDS:
        return Check("Guest write canary", RED, f"canary is stale ({age / 60:.0f} min old)")
    return Check("Guest write canary", GREEN, f"written {max(0, age):.0f}s ago")


def _check_agent(status: dict | None, error: str | None, now: datetime) -> Check:
    if status is None:
        return Check("Guest agent", YELLOW, error or "status.json is unavailable")
    generated = parse_utc(status.get("generatedAt"))
    if generated is None:
        return Check("Guest agent", YELLOW, "status.json has no valid UTC generatedAt")
    age = (now - generated).total_seconds()
    if age < -AGENT_MAX_FUTURE_SECONDS:
        return Check("Guest agent", YELLOW,
                     f"status.json is dated {abs(age):.0f}s in the future — guest/host clock error")
    if age > AGENT_MAX_AGE_SECONDS:
        return Check("Guest agent", YELLOW, f"status.json is stale ({age:.0f}s old) — is the agent task running?")
    last_error = status.get("lastError")
    if isinstance(last_error, str) and last_error:
        return Check("Guest agent", YELLOW, last_error)
    return Check("Guest agent", GREEN, f"reporting, {max(0, age):.0f}s ago")


def _check_tree(tree: dict | None, error: str | None, now: datetime) -> Check:
    if tree is None:
        return Check("Folder tree", YELLOW, (error or "tree.json is unavailable") + " — selective sync browsing is unavailable")
    generated = parse_utc(tree.get("generatedAt"))
    if generated is None:
        return Check("Folder tree", YELLOW, "tree.json has no valid UTC generatedAt")
    age = (now - generated).total_seconds()
    if age > TREE_MAX_AGE_SECONDS:
        return Check("Folder tree", YELLOW, f"tree.json is stale ({age / 60:.0f} min old)")
    return Check("Folder tree", GREEN, f"refreshed {age / 60:.0f} min ago")


def _check_icloud_client(status: dict | None) -> Check:
    if status is None:
        return Check("iCloud client", YELLOW, "unknown — no status from the guest agent")
    if status.get("icloudClientRunning") is True:
        return Check("iCloud client", GREEN, "iCloud for Windows is running (process liveness only)")
    return Check("iCloud client", YELLOW, "no iCloud process in the guest — sign-in may have been lost")


def _check_exclusions(status: dict | None, last_written_revision: Any,
                      last_write_at: datetime | None, now: datetime) -> Check:
    if status is None:
        return Check("Exclusions", YELLOW, "unknown — no status from the guest agent")
    entries = status.get("exclusions")
    if not isinstance(entries, list):
        entries = []
    counts: dict[str, int] = {}
    for entry in entries:
        if isinstance(entry, dict):
            state = entry.get("state")
            if isinstance(state, str):
                counts[state] = counts.get(state, 0) + 1

    if counts.get("error"):
        return Check("Exclusions", YELLOW, f"{counts['error']} exclusion(s) in error — see the Selective Sync tab")

    applied_revision = status.get("appliedRevision")
    if (isinstance(last_written_revision, int) and last_write_at is not None
            and (not isinstance(applied_revision, int) or applied_revision < last_written_revision)
            and (now - last_write_at).total_seconds() > REVISION_LAG_GRACE_SECONDS):
        return Check("Exclusions", YELLOW,
                     f"the agent is still on revision {applied_revision}; revision {last_written_revision} "
                     f"was written over {REVISION_LAG_GRACE_SECONDS // 60} minutes ago")

    pending = {state: counts[state] for state in PENDING_EXCLUSION_STATES if counts.get(state)}
    if pending:
        summary = ", ".join(f"{count} {state}" for state, count in pending.items())
        if "not-found" in pending:
            summary += " (a not-found path cannot be protected until the item exists)"
        return Check("Exclusions", YELLOW, summary)

    total = len(entries)
    if total == 0:
        return Check("Exclusions", GREEN, "nothing excluded")
    return Check("Exclusions", GREEN, f"{total} exclusion(s) applied")


def _check_disk(status: dict | None) -> Check:
    if status is None:
        return Check("Guest disk", YELLOW, "unknown — no status from the guest agent")
    free = status.get("diskFreeBytes")
    total = status.get("diskTotalBytes")
    summary = f"{human_bytes(free)} free of {human_bytes(total)}"
    sweep = status.get("sweep")
    if not isinstance(sweep, dict):
        sweep = {}
    in_progress = sweep.get("inProgress") is True
    below_floor = sweep.get("belowFloor") is True
    if in_progress:
        return Check("Guest disk", YELLOW,
                     f"{summary} — reclamation in progress; Windows frees dehydrated content asynchronously")
    if below_floor:
        blocked = sweep.get("blockedCount")
        tail = ""
        if isinstance(blocked, int) and blocked > 0:
            tail = f" ({blocked} file(s) held local because they are open, modified or not yet in sync)"
        return Check("Guest disk", YELLOW,
                     f"{summary} — below the {human_bytes(DISK_FLOOR_BYTES)} floor and nothing is eligible to reclaim"
                     f"{tail}; grow the disk or wait for uploads to finish")
    return Check("Guest disk", GREEN, summary)


def build_checks(*, container_running: Any, container_detail: str = "",
                 icloud_mounted: bool, bridge_mounted: bool,
                 canary_exists: bool, canary_mtime: float | None,
                 status: dict | None, status_error: str | None = None,
                 tree: dict | None, tree_error: str | None = None,
                 last_written_revision: Any = None,
                 last_write_at: datetime | None = None,
                 now: datetime | None = None) -> list[Check]:
    """Turn gathered facts into the ordered list of health rows."""
    if now is None:
        now = datetime.now(timezone.utc)
    return [
        _check_container(container_running, container_detail),
        _check_mount("iCloud mount", bridge.mount_dir(), icloud_mounted),
        _check_mount("Bridge mount", bridge.bridge_dir(), bridge_mounted),
        _check_canary(canary_exists, canary_mtime, now),
        _check_agent(status, status_error, now),
        _check_tree(tree, tree_error, now),
        _check_icloud_client(status),
        _check_exclusions(status, last_written_revision, last_write_at, now),
        _check_disk(status),
    ]


# ------------------------------------------------------------------- gathering --

def container_running() -> tuple[Any, str]:
    # Pinned to the native Engine socket for the same reason power.py is: Docker
    # Desktop can leave the desktop user's active context on `desktop-linux`,
    # whose daemon has never heard of `icloud-windows`, and this check would then
    # report a healthy running VM as red (item 3).
    try:
        completed = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT_SECONDS, check=False,
            env=power.docker_env(),
        )
    except FileNotFoundError:
        return None, "docker is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return None, "docker inspect timed out"
    except OSError as exc:
        return None, f"docker inspect failed: {exc}"
    if completed.returncode != 0:
        return False, (completed.stderr or "").strip() or f"container {CONTAINER_NAME} not found"
    return completed.stdout.strip() == "true", ""


class ContainerProbe:
    """Rate-limit ``docker inspect`` independently of the refresh cadence.

    The refresh loop runs every five seconds because a lost mount or a stale
    canary should surface quickly — but those are file stats.  The container
    check is a subprocess plus a Docker API round trip for a state that only
    changes on an explicit power action (v2 plan D30) or a daemon crash, so
    polling it at the same rate is ~17,000 process spawns a day for nothing.
    Fifteen seconds matches the agent's own status cadence and is dwarfed by the
    freshness thresholds above.  ``invalidate()`` is what keeps the states users
    actually cause immediate: every power transition and the Refresh button
    call it, so a check runs on the very next pass.

    The cached value is whatever ``probe`` returns: the health row uses the
    default ``container_running`` tuple, and the D30 power-control
    classification — the other ``docker inspect`` consumer — reuses this class
    with ``probe=power.inspect_container``.  ``clock`` and ``probe`` are
    injectable so the tests need no docker.
    """

    def __init__(self, *, interval: float = CONTAINER_POLL_INTERVAL_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 probe: Callable[[], Any] | None = None) -> None:
        self._interval = interval
        self._clock = clock
        self._probe = probe or container_running
        self._at: float | None = None
        self._value: Any = (None, "")

    def invalidate(self) -> None:
        """Force the next ``read()`` to actually run the check."""
        self._at = None

    def read(self) -> tuple[Any, str]:
        now = self._clock()
        if self._at is None or (now - self._at) >= self._interval:
            self._value = self._probe()
            self._at = now
        return self._value


class DocumentCache:
    """Re-read a bridge document only when it has actually changed.

    ``tree.json`` is regenerated every ten minutes and ``status.json`` every
    fifteen seconds, but the GUI polls every five, so the common case is a full
    SMB read and JSON parse of bytes already known to be identical — and
    ``tree.json`` carries one node per directory of the whole library.  Keying on
    ``(st_mtime_ns, st_size)`` turns the unchanged case into a single QUERY_INFO
    round trip.  The signature is taken before *and* after the read and the value
    is only cached when the two agree, so a document rewritten mid-read is never
    stored under the older file's identity.  The agent replaces both files
    atomically, so any content change moves the signature.

    Cached documents are handed out by reference; treat them as read-only.
    ``stat`` is injectable so the tests never need a real mount.
    """

    def __init__(self, *, stat: Callable[[str], os.stat_result] = os.stat) -> None:
        self._stat = stat
        self._entries: dict[str, tuple[tuple[int, int], Any]] = {}

    def _signature(self, path: str) -> tuple[int, int] | None:
        try:
            info = self._stat(path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def invalidate(self) -> None:
        """Forget every cached document.

        Used when the app can no longer vouch for what it read — D38's
        interrupted transaction, where the helper may have unmounted the share
        the cached signature refers to.
        """
        self._entries.clear()

    def read(self, path: str, reader: Callable[[], Any]) -> Any:
        before = self._signature(path)
        if before is not None:
            cached = self._entries.get(path)
            if cached is not None and cached[0] == before:
                return cached[1]
        try:
            value = reader()
        except Exception:
            # An unreadable or malformed document must not leave a stale copy
            # behind that a later identical signature would serve.
            self._entries.pop(path, None)
            raise
        after = self._signature(path)
        if after is not None and after == before:
            self._entries[path] = (after, value)
        else:
            self._entries.pop(path, None)
        return value


@dataclass
class Snapshot:
    checks: list[Check]
    overall: str
    status: dict | None
    tree: dict | None
    #: The D35 protocol/agent-build classification. Carried explicitly rather
    #: than left as one more "file unavailable" string, because the controller's
    #: central write gate keys off it.
    compatibility: bridge.Compatibility = field(
        default_factory=bridge.Compatibility)


def gather(*, last_written_revision: Any = None, last_write_at: datetime | None = None,
           documents: DocumentCache | None = None,
           container: ContainerProbe | None = None) -> Snapshot:
    """Collect every host-side fact and evaluate it. Call from a worker thread.

    ``documents`` and ``container`` are the caller's long-lived caches; passing
    neither gathers everything afresh, which is what the tests want.
    """
    if documents is None:
        documents = DocumentCache()
    running, detail = (container.read() if container is not None else container_running())

    icloud_mounted = os.path.ismount(bridge.mount_dir())
    bridge_mounted = os.path.ismount(bridge.bridge_dir())

    canary_path = os.path.join(bridge.mount_dir(), ".linux-canary")
    canary_exists = False
    canary_mtime: float | None = None
    try:
        canary_mtime = os.stat(canary_path).st_mtime
        canary_exists = True
    except OSError:
        pass

    # A `ProtocolError` is kept apart from every other read failure: only it
    # may close the D35 write gate, and "the file is missing" must never be
    # mistaken for "the guest speaks a protocol this app does not".
    status: dict | None = None
    status_error: str | None = None
    status_protocol_error: str | None = None
    try:
        status = documents.read(bridge.status_path(), bridge.read_status)
    except bridge.ProtocolError as exc:
        status_error = status_protocol_error = str(exc)
    except bridge.BridgeError as exc:
        status_error = str(exc)

    tree: dict | None = None
    tree_error: str | None = None
    tree_protocol_error: str | None = None
    try:
        tree = documents.read(bridge.tree_path(), bridge.read_tree)
    except bridge.ProtocolError as exc:
        tree_error = tree_protocol_error = str(exc)
    except bridge.BridgeError as exc:
        tree_error = str(exc)

    checks = build_checks(
        container_running=running, container_detail=detail,
        icloud_mounted=icloud_mounted, bridge_mounted=bridge_mounted,
        canary_exists=canary_exists, canary_mtime=canary_mtime,
        status=status, status_error=status_error,
        tree=tree, tree_error=tree_error,
        last_written_revision=last_written_revision, last_write_at=last_write_at,
    )
    compatibility = bridge.classify_compatibility(
        status, status_protocol_error=status_protocol_error,
        tree_protocol_error=tree_protocol_error)
    return Snapshot(checks=checks, overall=overall(checks), status=status, tree=tree,
                    compatibility=compatibility)
