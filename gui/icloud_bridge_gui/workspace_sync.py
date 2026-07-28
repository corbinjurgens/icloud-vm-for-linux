"""One safe, finite Safe Workspace synchronization cycle (D52).

`workspaces.py` owns the configuration model, the path rules and the state
layout; this module owns the only thing that touches the mount or runs a
program. One call to :func:`run_cycle` performs at most one bounded, one-shot
reconciliation of a single workspace and then returns
(`docs/plan-safe-local-workspaces.md` sections 6, 7 and 8):

* **Qt-free, shell-free, unattended.** Every subprocess goes through an injected
  runner as an argv list, exactly as `power.py` invokes the power helper, so the
  argv and the environment are assertable without a Unison binary present.
* **Gated before it reads anything.** The order of section 6 is the point: the
  single-flight lock, then the powered-off marker, then the mount, then the
  remote directory, then the version contract. A powered-off bridge is never
  probed, and a second invocation performs no scan at all.
* **Two matching observations before any write.** A cycle invokes Unison only
  when both endpoints look exactly as they did on the previous poll, and the
  fingerprint deliberately excludes `ctime` — the churn of section 2.1 is a
  `ctime`-only change, and treating it as a change would let the guest's
  metadata drive the synchronizer.
* **Refuse rather than guess.** A symlink, a mount crossing, a socket, a device,
  a FIFO, an ambiguous first run, insufficient free space, or a mass
  disappearance stops that workspace with a specific message and leaves both
  replicas exactly as they are. There is no automatic winner and no override.

Nothing here decides *when* to run: the Qt layer owns the timer, single-flight
scheduling and the drain (section 10). Nothing here is admitted to
`diagnostics.Facts` (section 12) — the caller reports counts, never paths.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Sequence

from . import power, workspaces
from .workspaces import WorkspaceError

UNISON_BIN = "unison"

#: The compatibility line documented upstream (section 7.1). Compared on
#: ``(major, minor)``; the patch level never gates anything.
MIN_VERSION = (2, 52)

#: Section 7.7. Generous next to a settled vault, and short enough that the
#: shutdown drain of section 10 always releases.
TIMEOUT_SECONDS = 120

#: Never a replica: a child's working directory must not pin a mount point.
WORKING_DIRECTORY = "/"

#: Section 7.4, and the whole ignore list — there is no pattern language. Each
#: entry ignores its own subtree.
IGNORED_PATHS = (
    ".obsidian/workspace.json",
    ".obsidian/workspace-mobile.json",
    ".obsidian/cache",
    ".trash",
)

#: Options that select a winner, delete an archive, run continuously or override
#: a lock. Section 7.7 forbids every one of them, under any circumstance.
NEVER_PASSED = (
    "prefer", "preferpartial", "force", "copyonconflict", "repeat",
    "ignorelocks", "ignorearchives", "retry", "silent", "terse",
)

#: Section 7.6.
GUARD_MIN_MISSING = 20
GUARD_MISSING_FRACTION = 0.20

#: Section 7.5: the first copy needs headroom well beyond the payload itself.
FREE_SPACE_MARGIN_BYTES = 1024 ** 3

#: Section 7.9 bounds. `paths` names entries, never content.
STATUS_VERSION = 1
MAX_STATUS_PATHS = 20
MAX_STATUS_PATH_CHARS = 200
MAX_DETAIL_CHARS = 2000
MAX_STATUS_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024

SNAPSHOT_VERSION = 1
BASELINE_VERSION = 1

#: Cycle outcomes (sections 7.8 and 14). Finer than the status state below,
#: because "the mount is gone" and "Unison exited 2" need different retries.
ALREADY_RUNNING = "already-running"
PAUSED = "paused"
UNAVAILABLE = "unavailable"
STABILIZING = "stabilizing"
GUARDED = "guarded"
SYNCHRONIZED = "synchronized"
CONFLICT = "conflict"
FAILED = "failed"
FATAL = "fatal"
TIMEOUT = "timeout"
ERROR = "error"

#: The eight states section 7.9 persists and section 11.1 renders.
STATE_WAITING = "waiting"
STATE_STABILIZING = "stabilizing"
STATE_SYNCING = "syncing"
STATE_UP_TO_DATE = "up-to-date"
STATE_PAUSED = "paused"
STATE_CONFLICT = "conflict"
STATE_GUARDED = "guarded"
STATE_ERROR = "error"

_STATE_FOR_OUTCOME = {
    # A held lock means another cycle owns the workspace right now; this one
    # reports that and writes nothing.
    ALREADY_RUNNING: STATE_SYNCING,
    PAUSED: STATE_PAUSED,
    # Not an error: the bridge is simply not mounted yet, and the next tick
    # retries without the operator doing anything.
    UNAVAILABLE: STATE_WAITING,
    STABILIZING: STATE_STABILIZING,
    GUARDED: STATE_GUARDED,
    SYNCHRONIZED: STATE_UP_TO_DATE,
    CONFLICT: STATE_CONFLICT,
    FAILED: STATE_ERROR,
    FATAL: STATE_ERROR,
    TIMEOUT: STATE_ERROR,
    ERROR: STATE_ERROR,
}

#: Section 7.8, keyed by Unison's own exit code.
_EXIT_OUTCOMES = {0: SYNCHRONIZED, 1: CONFLICT, 2: FAILED, 3: FATAL}

_VERSION_RE = re.compile(r"unison\s+version\s+(\d+)\.(\d+)(?:\.(\d+))?",
                         re.IGNORECASE)

#: Unison's own end-of-run summary lines, the one place it names the paths it
#: refused to touch. Presentation only: nothing parses these into a decision.
_SKIPPED_RE = re.compile(
    r"^(?:skipped|failed):\s+(?P<path>.+?)(?:\s+\([^()]*\))?$")


# --------------------------------------------------------------- the runner --

#: `power.RunResult` is exactly the minimal shape a fake runner needs, so it is
#: reused rather than duplicated. This runner takes the child's environment as
#: well, because each workspace gets its own private ``UNISON`` directory.
RunResult = power.RunResult
Runner = Callable[[list[str], float, Mapping[str, str]], RunResult]


def default_runner(argv: list[str], timeout: float,
                   env: Mapping[str, str]) -> RunResult:
    """Section 7.7's production adapter: no shell, bounded, captured as text."""
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            env=dict(env), cwd=WORKING_DIRECTORY)
    except subprocess.TimeoutExpired as exc:      # normalize for callers/tests
        raise TimeoutError(f"{argv[0]} timed out after {timeout}s") from exc
    return RunResult(completed.returncode, completed.stdout, completed.stderr)


def base_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """This process's environment, minus any inherited ``UNISON``.

    A *copy* with overrides, never a replacement: the child still needs ``PATH``
    and ``HOME``. Dropping ``UNISON`` here means no caller-supplied archive
    directory can reach Unison except through :func:`build_env`.
    """
    env = dict(os.environ if environ is None else environ)
    env.pop("UNISON", None)
    env["NO_COLOR"] = "1"
    return env


def build_env(unison_directory: str,
              environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child environment for a workspace's own Unison invocation (7.7)."""
    env = base_env(environ)
    env["UNISON"] = unison_directory
    return env


# ------------------------------------------------------- the version contract --

@dataclass(frozen=True)
class VersionCheck:
    """Whether this host's Unison may drive a cycle at all (section 7.1)."""

    ok: bool
    version: str = ""
    detail: str = ""


def parse_version(text: str) -> tuple[int, int, str] | None:
    """``(major, minor, printable)`` from ``unison -version`` output, or None."""
    match = _VERSION_RE.search(text or "")
    if match is None:
        return None
    printable = ".".join(part for part in match.groups() if part is not None)
    return int(match.group(1)), int(match.group(2)), printable


_REQUIRED = ".".join(str(part) for part in MIN_VERSION)

_version_lock = threading.Lock()
_version_check: VersionCheck | None = None


def _probe_version(runner: Runner,
                   environ: Mapping[str, str] | None) -> VersionCheck:
    argv = [UNISON_BIN, "-version"]
    try:
        result = runner(argv, TIMEOUT_SECONDS, base_env(environ))
    except FileNotFoundError:
        return VersionCheck(
            False, detail=f"unison is not installed; Safe Workspaces needs "
                          f"version {_REQUIRED} or newer.")
    except TimeoutError:
        return VersionCheck(False, detail="unison -version timed out.")
    except OSError as exc:                        # pragma: no cover - defensive
        return VersionCheck(False, detail=f"could not run unison: {exc}")
    parsed = parse_version(f"{result.stdout}\n{result.stderr}")
    if parsed is None:
        return VersionCheck(
            False, detail="unison did not report a version this app can read, "
                          f"so Safe Workspaces needs {_REQUIRED} or newer.")
    major, minor, printable = parsed
    if (major, minor) < MIN_VERSION:
        return VersionCheck(
            False, printable,
            f"unison {printable} is installed; Safe Workspaces needs "
            f"{_REQUIRED} or newer.")
    return VersionCheck(True, printable)


def check_version(runner: Runner = default_runner, *,
                  environ: Mapping[str, str] | None = None,
                  refresh: bool = False) -> VersionCheck:
    """The cached-once-per-process version verdict (section 7.1).

    A refusal is cached too: installing Unison mid-session is a restart, not a
    condition to re-probe every five seconds.
    """
    global _version_check
    with _version_lock:
        if _version_check is None or refresh:
            _version_check = _probe_version(runner, environ)
        return _version_check


def reset_version_cache() -> None:
    """Forget the cached verdict. For tests and for an explicit re-check."""
    global _version_check
    with _version_lock:
        _version_check = None


# ----------------------------------------------------------------- snapshots --

#: One entry: ``(relative path, kind, size, mtime_ns)`` — and deliberately
#: nothing else (section 7.3).
Entry = tuple[str, str, int, int]
Snapshot = tuple[Entry, ...]

KIND_FILE = "file"
KIND_DIR = "dir"


def is_ignored(relative: str) -> bool:
    """Whether section 7.4 excludes this path, or an ancestor of it."""
    return any(relative == item or relative.startswith(item + "/")
               for item in IGNORED_PATHS)


def scan(root: str) -> Snapshot:
    """Fingerprint one replica, refusing anything that is not plain data.

    Symlinks are never followed and filesystem boundaries are never crossed
    (section 7.2). A special file or a crossing raises rather than being skipped
    silently, because "quietly did not copy that" is how a replica loses a file
    without anyone noticing.
    """
    base = os.path.normpath(root)
    try:
        info = os.lstat(base)
    except OSError as exc:
        raise WorkspaceError(f"cannot read {base}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(f"{base} is not a directory")
    device = info.st_dev

    entries: list[Entry] = []
    pending = [("", base)]
    while pending:
        relative, path = pending.pop()
        try:
            listing = list(os.scandir(path))
        except OSError as exc:
            raise WorkspaceError(
                f"cannot read {relative or base}: {exc}") from exc
        for item in listing:
            child = f"{relative}/{item.name}" if relative else item.name
            if is_ignored(child):
                continue
            try:
                child_stat = item.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceError(f"cannot inspect {child}: {exc}") from exc
            mode = child_stat.st_mode
            if stat.S_ISLNK(mode):
                raise WorkspaceError(
                    f"{child} is a symbolic link; a workspace never follows or "
                    "copies one")
            if child_stat.st_dev != device:
                raise WorkspaceError(
                    f"{child} is on a different filesystem; a workspace never "
                    "crosses a mount point")
            if stat.S_ISDIR(mode):
                entries.append((child, KIND_DIR, 0, child_stat.st_mtime_ns))
                pending.append((child, os.path.join(path, item.name)))
            elif stat.S_ISREG(mode):
                entries.append((child, KIND_FILE, child_stat.st_size,
                                child_stat.st_mtime_ns))
            else:
                raise WorkspaceError(
                    f"{child} is not a regular file or directory, so this "
                    "workspace cannot be synchronized safely")
    return tuple(sorted(entries))


def paths_of(snapshot: Snapshot) -> frozenset[str]:
    return frozenset(entry[0] for entry in snapshot)


def logical_size(snapshot: Snapshot) -> int:
    """The sum of the file sizes, which is what a first copy has to fit."""
    return sum(entry[2] for entry in snapshot if entry[1] == KIND_FILE)


def _snapshot_document(local: Snapshot, remote: Snapshot,
                       observed_at: str) -> dict:
    return {
        "version": SNAPSHOT_VERSION,
        "observedAt": observed_at,
        "local": [list(entry) for entry in local],
        "remote": [list(entry) for entry in remote],
    }


def _snapshot_from_document(value: object) -> Snapshot | None:
    if not isinstance(value, list):
        return None
    entries: list[Entry] = []
    for item in value:
        if (not isinstance(item, list) or len(item) != 4
                or not isinstance(item[0], str) or not isinstance(item[1], str)
                or isinstance(item[2], bool) or not isinstance(item[2], int)
                or isinstance(item[3], bool) or not isinstance(item[3], int)):
            return None
        entries.append((item[0], item[1], item[2], item[3]))
    return tuple(entries)


def read_snapshot(workspace_id: str,
                  base: str | None = None) -> tuple[Snapshot, Snapshot] | None:
    """The previous poll's pair, or ``None`` when there is not a usable one.

    A missing or damaged snapshot is not an error: it only means this poll has
    nothing to compare against, so the cycle stabilizes for one more interval.
    """
    document = _read_json(workspaces.snapshot_path(workspace_id, base),
                          MAX_SNAPSHOT_BYTES)
    if not isinstance(document, dict) or document.get("version") != SNAPSHOT_VERSION:
        return None
    local = _snapshot_from_document(document.get("local"))
    remote = _snapshot_from_document(document.get("remote"))
    if local is None or remote is None:
        return None
    return local, remote


# ------------------------------------------------------ the destructive guard --

@dataclass(frozen=True)
class GuardVerdict:
    """Section 7.6's decision, with the counts the operator needs to see."""

    tripped: bool
    endpoint: str = ""
    missing: int = 0
    baseline_count: int = 0
    examples: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        if not self.tripped:
            return ""
        return (f"{self.missing} of {self.baseline_count} path(s) last seen in "
                f"the {self.endpoint} folder are missing, so this workspace is "
                "on hold and nothing was synchronized. Restore the folder, or "
                "forget and re-add the workspace once both sides agree.")


def evaluate_guard(baseline_paths: Iterable[str], current_paths: Iterable[str],
                   endpoint: str) -> GuardVerdict:
    """One endpoint's half of the guard. Pure: no I/O, no thresholds hidden."""
    known = frozenset(baseline_paths)
    present = frozenset(current_paths)
    missing = sorted(known - present)
    if not known:
        return GuardVerdict(False, endpoint, len(missing), len(known))
    emptied = not present
    bulk = (len(missing) >= GUARD_MIN_MISSING
            and len(missing) >= GUARD_MISSING_FRACTION * len(known))
    verdict = GuardVerdict(
        emptied or bulk, endpoint, len(missing), len(known),
        tuple(missing[:MAX_STATUS_PATHS]))
    return verdict


def _baseline_document(local: Snapshot, remote: Snapshot,
                       recorded_at: str) -> dict:
    return {
        "version": BASELINE_VERSION,
        "recordedAt": recorded_at,
        "local": sorted(paths_of(local)),
        "remote": sorted(paths_of(remote)),
    }


def _baseline_paths(document: object, endpoint: str) -> frozenset[str] | None:
    if not isinstance(document, dict) or document.get("version") != BASELINE_VERSION:
        return None
    value = document.get(endpoint)
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        return None
    return frozenset(value)


def read_baseline(workspace_id: str, base: str | None = None) -> dict | None:
    """The last successful cycle's path sets, or ``None``."""
    document = _read_json(workspaces.baseline_path(workspace_id, base),
                          MAX_SNAPSHOT_BYTES)
    if not isinstance(document, dict):
        return None
    if _baseline_paths(document, "local") is None:
        return None
    if _baseline_paths(document, "remote") is None:
        return None
    return document


# ------------------------------------------------------- the Unison invocation --

def build_argv(local_root: str, remote_root: str, *, backups: str,
               logfile: str) -> list[str]:
    """Section 7.7's command, one argv element per option. No shell, ever.

    The value-bearing booleans are written ``-name=value`` because Unison's own
    parser treats a bare ``-times`` as the flag and a following ``true`` as a
    third root ("unison was invoked incorrectly (too many roots)"). The option
    set and every value are exactly the plan's.
    """
    argv = [UNISON_BIN, local_root, remote_root,
            "-batch",
            "-auto",
            "-fastcheck", "false",
            "-times=true",
            "-perms", "0",
            "-dontchmod=true",
            "-owner=false",
            "-group=false",
            "-xattrs=false",
            "-acl=false",
            "-confirmbigdel=true",
            "-backup", "Name *",
            "-backupcurr", "Name *",
            "-backuploc", "central",
            "-backupdir", backups,
            "-maxbackups", "10"]
    for ignored in IGNORED_PATHS:
        argv += ["-ignore", f"Path {ignored}"]
    argv += ["-logfile", logfile, "-color", "false"]
    return argv


def classify_exit(code: int) -> str:
    """Section 7.8. An unknown code is a failure, never a success."""
    return _EXIT_OUTCOMES.get(code, FAILED)


def sanitize_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """At most 20 relative paths, each elided to 200 characters (section 7.9)."""
    out: list[str] = []
    for path in paths[:MAX_STATUS_PATHS]:
        text = power.sanitize_line(path)
        if len(text) > MAX_STATUS_PATH_CHARS:
            text = text[:MAX_STATUS_PATH_CHARS - 1] + "…"
        if text:
            out.append(text)
    return tuple(out)


def summarize(result: RunResult) -> str:
    """A single-line, bounded, control-character-free excerpt of the engine's
    own message (section 7.9). Never file content — Unison prints names only."""
    lines = [power.sanitize_line(line)
             for line in f"{result.stdout}\n{result.stderr}".splitlines()]
    text = " | ".join(line for line in lines if line)[-MAX_DETAIL_CHARS:]
    return text


def skipped_paths(result: RunResult) -> tuple[str, ...]:
    """The relative paths Unison itself reported as skipped or failed."""
    found: list[str] = []
    for line in f"{result.stdout}\n{result.stderr}".splitlines():
        match = _SKIPPED_RE.match(power.sanitize_line(line))
        if match is None:
            continue
        path = match.group("path").strip()
        if path and path not in found:
            found.append(path)
    return sanitize_paths(found)


# ------------------------------------------------------------ small local I/O --

def _read_json(path: str, max_bytes: int) -> object:
    """A bounded read of one of this workspace's own state documents.

    These files are caches this app wrote itself; an unreadable one means "start
    again", not "stop the workspace", so every failure collapses to ``None``.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        return json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def free_bytes(path: str) -> int:
    """Available bytes on the filesystem holding ``path``'s nearest parent."""
    target = workspaces.nearest_existing(path)
    try:
        info = os.statvfs(target)
    except OSError as exc:
        raise WorkspaceError(f"cannot check free space on {target}: {exc}") from exc
    return info.f_bavail * info.f_frsize


def _truncate_log(path: str) -> None:
    """Keep `sync.log` bounded (section 7.9).

    Truncated *before* the run, so the log always holds the most recent cycle
    rather than being emptied right after producing the interesting part.
    """
    try:
        if os.path.getsize(path) > MAX_LOG_BYTES:
            with open(path, "w", encoding="utf-8"):
                pass
    except OSError:
        return


@contextlib.contextmanager
def workspace_lock(path: str):
    """Yield whether the non-blocking exclusive lock was taken (section 6).

    ``flock`` is held by the open file description, so a second invocation in
    this same process contends exactly like one in another process would.
    """
    try:
        handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
                         workspaces.FILE_MODE)
    except OSError as exc:
        raise WorkspaceError(f"cannot open {path}: {exc}") from exc
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(handle)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(now: Callable[[], datetime]) -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ the status file --

def read_status(workspace_id: str, base: str | None = None) -> dict | None:
    """The persisted status document, or ``None`` when there is not one yet."""
    document = _read_json(workspaces.status_path(workspace_id, base),
                          MAX_STATUS_BYTES)
    if not isinstance(document, dict) or document.get("version") != STATUS_VERSION:
        return None
    return document


def status_document(*, state: str, updated_at: str, last_success_at: str,
                    last_exit: int | None, local_paths: int, remote_paths: int,
                    conflicts: int, missing_from_baseline: int,
                    paths: Sequence[str], detail: str) -> dict:
    """Section 7.9's document, already bounded."""
    return {
        "version": STATUS_VERSION,
        "state": state,
        "updatedAt": updated_at,
        "lastSuccessAt": last_success_at,
        "lastExit": last_exit,
        "counts": {
            "localPaths": local_paths,
            "remotePaths": remote_paths,
            "conflicts": conflicts,
            "missingFromBaseline": missing_from_baseline,
        },
        "paths": list(sanitize_paths(list(paths))),
        "detail": detail[:MAX_DETAIL_CHARS],
    }


def _write_status(path: str, document: dict) -> bool:
    """Write the status atomically unless only its timestamp would move.

    Returns whether anything was written. A cycle runs every five seconds
    forever; rewriting an identical document that often would be a pointless
    `fsync` per tick, and `workspaces.save` already treats "the same document"
    as nothing to write.
    """
    existing = _read_json(path, MAX_STATUS_BYTES)
    if isinstance(existing, dict):
        before = dict(existing)
        after = dict(document)
        before.pop("updatedAt", None)
        after.pop("updatedAt", None)
        if before == after:
            return False
    workspaces.write_json_atomic(path, document)
    return True


# ----------------------------------------------------------------- the cycle --

@dataclass(frozen=True)
class CycleResult:
    """What one call to :func:`run_cycle` did, and what the GUI should show.

    ``outcome`` is the fine-grained classification of sections 7.8 and 14;
    ``state`` is the coarser token section 7.9 persists and section 11.1
    renders. Everything here is already bounded and free of file content.
    """

    workspace_id: str
    outcome: str
    state: str
    detail: str = ""
    exit_code: int | None = None
    local_paths: int = 0
    remote_paths: int = 0
    conflicts: int = 0
    missing_from_baseline: int = 0
    paths: tuple[str, ...] = ()
    updated_at: str = ""
    last_success_at: str = ""
    ran_unison: bool = False
    wrote_status: bool = False

    @property
    def ok(self) -> bool:
        """Whether the workspace is in a state that needs no attention."""
        return self.state not in (STATE_CONFLICT, STATE_GUARDED, STATE_ERROR)


@dataclass(frozen=True)
class _Cycle:
    """The resolved inputs of one cycle, so the steps below stay readable."""

    workspace: workspaces.Workspace
    runner: Runner
    base: str | None
    environ: Mapping[str, str] | None
    marker_path: str
    is_mount: Callable[[str], bool]
    mountinfo_path: str
    now: Callable[[], datetime]
    local_snapshot: Snapshot = ()
    remote_snapshot: Snapshot = ()

    @property
    def identifier(self) -> str:
        return self.workspace.id


def _result(cycle: _Cycle, outcome: str, detail: str = "", *,
            exit_code: int | None = None, conflicts: int = 0,
            missing: int = 0, paths: Sequence[str] = (),
            ran_unison: bool = False, success: bool = False,
            persist: bool = True) -> CycleResult:
    """Classify, persist the status document, and return the result."""
    state = _STATE_FOR_OUTCOME[outcome]
    updated_at = _timestamp(cycle.now)
    previous = read_status(cycle.identifier, cycle.base)
    last_success = ""
    if isinstance(previous, dict) and isinstance(previous.get("lastSuccessAt"), str):
        last_success = previous["lastSuccessAt"]
    if success:
        last_success = updated_at
    bounded = sanitize_paths(list(paths))
    wrote = False
    if persist:
        document = status_document(
            state=state, updated_at=updated_at, last_success_at=last_success,
            last_exit=exit_code, local_paths=len(cycle.local_snapshot),
            remote_paths=len(cycle.remote_snapshot), conflicts=conflicts,
            missing_from_baseline=missing, paths=bounded, detail=detail)
        wrote = _write_status(workspaces.status_path(cycle.identifier, cycle.base),
                              document)
    return CycleResult(
        workspace_id=cycle.identifier, outcome=outcome, state=state,
        detail=detail[:MAX_DETAIL_CHARS], exit_code=exit_code,
        local_paths=len(cycle.local_snapshot),
        remote_paths=len(cycle.remote_snapshot), conflicts=conflicts,
        missing_from_baseline=missing, paths=bounded, updated_at=updated_at,
        last_success_at=last_success, ran_unison=ran_unison, wrote_status=wrote)


def run_cycle(workspace: workspaces.Workspace, *,
              runner: Runner = default_runner,
              base: str | None = None,
              environ: Mapping[str, str] | None = None,
              marker_path: str = power.MARKER_PATH,
              is_mount: Callable[[str], bool] = os.path.ismount,
              mountinfo_path: str = workspaces.MOUNTINFO_PATH,
              now: Callable[[], datetime] | None = None) -> CycleResult:
    """Run at most one finite cycle for one workspace, and report what happened.

    Safe to call on any thread and from any lifecycle state: every precondition
    of section 6 is re-checked here, in that order, and the first one that fails
    returns without touching the mount. The call is bounded by the 120-second
    subprocess timeout, so the shutdown drain of section 10 always completes.
    """
    cycle = _Cycle(workspace=workspace, runner=runner, base=base,
                   environ=environ, marker_path=marker_path, is_mount=is_mount,
                   mountinfo_path=mountinfo_path,
                   now=_utc_now if now is None else now)
    # 4. A paused workspace stays configured and visible, and does no I/O at
    #    all -- not even creating its state directory.
    if not workspace.enabled:
        return CycleResult(workspace_id=workspace.id, outcome=PAUSED,
                           state=STATE_PAUSED,
                           detail="this workspace is paused",
                           updated_at=_timestamp(cycle.now))
    try:
        workspaces.ensure_state_dir(workspace.id, base)
    except WorkspaceError as exc:
        return CycleResult(workspace_id=workspace.id, outcome=ERROR,
                           state=STATE_ERROR, detail=str(exc)[:MAX_DETAIL_CHARS],
                           updated_at=_timestamp(cycle.now))
    # 5.1. The lock comes first, so a second invocation performs no scan.
    try:
        with workspace_lock(workspaces.lock_path(workspace.id, base)) as held:
            if not held:
                return CycleResult(
                    workspace_id=workspace.id, outcome=ALREADY_RUNNING,
                    state=STATE_SYNCING,
                    detail="a cycle for this workspace is already running",
                    updated_at=_timestamp(cycle.now))
            try:
                return _run_locked(cycle)
            except WorkspaceError as exc:
                return _result(cycle, ERROR, str(exc))
    except WorkspaceError as exc:
        # The lock file or the status file itself could not be written. Report
        # it; a worker thread must never see this call raise.
        return CycleResult(workspace_id=workspace.id, outcome=ERROR,
                           state=STATE_ERROR, detail=str(exc)[:MAX_DETAIL_CHARS],
                           updated_at=_timestamp(cycle.now))


def _run_locked(cycle: _Cycle) -> CycleResult:
    workspace = cycle.workspace
    # 5.2. Before any mount touch at all.
    if os.path.exists(cycle.marker_path):
        return _result(cycle, PAUSED,
                       "the bridge is powered off, so nothing was read or "
                       "changed. Local editing stays safe.")
    # 5.3 and 5.4. Now, and only now, the mount may be looked at.
    mount = os.path.normpath(workspaces.mount_root(cycle.environ))
    if not cycle.is_mount(mount):
        return _result(cycle, UNAVAILABLE, f"{mount} is not mounted yet.")
    remote_root = workspaces.remote_root(workspace.remote, cycle.environ)
    try:
        remote_info = os.lstat(remote_root)
    except OSError:
        return _result(cycle, UNAVAILABLE,
                       f"{remote_root} is not available yet. A workspace never "
                       "creates an iCloud folder.")
    if stat.S_ISLNK(remote_info.st_mode) or not stat.S_ISDIR(remote_info.st_mode):
        return _result(cycle, UNAVAILABLE, f"{remote_root} is not a directory.")
    # 5.5. The version contract, probed once per process.
    version = check_version(cycle.runner, environ=cycle.environ)
    if not version.ok:
        return _result(cycle, ERROR, version.detail)

    # The local half: a symlinked root, a non-directory, or a filesystem outside
    # the allowlist stops the workspace before anything is copied.
    workspaces.check_local_state(workspace.local,
                                 mountinfo_path=cycle.mountinfo_path)

    remote_snapshot = scan(remote_root)
    local_exists = os.path.isdir(workspace.local)
    local_snapshot = scan(workspace.local) if local_exists else ()
    cycle = replace(cycle, local_snapshot=local_snapshot,
                    remote_snapshot=remote_snapshot)

    baseline = read_baseline(workspace.id, cycle.base)
    archives = _has_archive(workspaces.unison_dir(workspace.id, cycle.base))
    first_run = baseline is None and not archives
    if first_run:
        refusal = _check_first_run(cycle, remote_root)
        if refusal is not None:
            return refusal

    # Stability: two identical observations, or nothing else happens.
    previous = read_snapshot(workspace.id, cycle.base)
    current = (local_snapshot, remote_snapshot)
    if previous != current:
        # Only a changed pair is written back, so `observedAt` records when this
        # pair was first seen rather than when it was last confirmed.
        workspaces.write_json_atomic(
            workspaces.snapshot_path(workspace.id, cycle.base),
            _snapshot_document(local_snapshot, remote_snapshot,
                               _timestamp(cycle.now)))
        return _result(cycle, STABILIZING,
                       "waiting for both folders to stay unchanged.")

    verdict = GuardVerdict(False)
    if baseline is not None:
        verdict = _guard(baseline, local_snapshot, remote_snapshot)
        if verdict.tripped:
            return _result(cycle, GUARDED, verdict.detail,
                           missing=verdict.missing, paths=verdict.examples)

    if first_run:
        workspaces.create_local_root(workspace.local)

    return _invoke(cycle, remote_root, local_snapshot, remote_snapshot,
                   verdict.missing)


def _has_archive(unison_directory: str) -> bool:
    """Whether Unison has ever recorded a common state for this workspace."""
    try:
        return any(name.startswith("ar") for name in os.listdir(unison_directory))
    except OSError:
        return False


def _check_first_run(cycle: _Cycle, remote_root: str) -> CycleResult | None:
    """Section 7.5's three refusals, or ``None`` when the first run may proceed."""
    workspace = cycle.workspace
    if not cycle.remote_snapshot:
        return _result(cycle, ERROR,
                       f"{remote_root} is empty, so there is nothing to copy "
                       "into a new workspace yet.")
    try:
        workspaces.check_local_occupancy(workspace.local)
    except WorkspaceError:
        return _result(
            cycle, ERROR,
            f"{workspace.local} already holds {len(cycle.local_snapshot)} "
            f"item(s) and {remote_root} holds {len(cycle.remote_snapshot)}. A "
            "first sync will not merge two populated folders; empty one side "
            "yourself, or choose a different local folder.")
    needed = logical_size(cycle.remote_snapshot) + FREE_SPACE_MARGIN_BYTES
    available = free_bytes(workspace.local)
    if available <= needed:
        return _result(
            cycle, ERROR,
            f"{workspace.local} has {available} byte(s) free; the first copy "
            f"needs more than {needed} (the iCloud folder plus a 1 GiB "
            "margin). Nothing was created.")
    return None


def _guard(baseline: dict, local: Snapshot, remote: Snapshot) -> GuardVerdict:
    """Section 7.6 over both endpoints; the worse endpoint's verdict wins.

    The untripped verdict is returned too, because its ``missing`` count is what
    the status document reports when the thresholds were nowhere near.
    """
    worst = GuardVerdict(False)
    for endpoint, snapshot in (("local", local), ("remote", remote)):
        known = _baseline_paths(baseline, endpoint) or frozenset()
        verdict = evaluate_guard(known, paths_of(snapshot), endpoint)
        if verdict.tripped:
            return verdict
        if verdict.missing > worst.missing:
            worst = verdict
    return worst


def _invoke(cycle: _Cycle, remote_root: str, local: Snapshot,
            remote: Snapshot, missing: int) -> CycleResult:
    workspace = cycle.workspace
    logfile = workspaces.log_path(workspace.id, cycle.base)
    _truncate_log(logfile)
    argv = build_argv(
        workspace.local, remote_root,
        backups=workspaces.backups_dir(workspace.id, cycle.base),
        logfile=logfile)
    env = build_env(workspaces.unison_dir(workspace.id, cycle.base),
                    cycle.environ)
    try:
        result = cycle.runner(argv, TIMEOUT_SECONDS, env)
    except FileNotFoundError:
        return _result(cycle, ERROR,
                       f"unison is not installed; Safe Workspaces needs "
                       f"version {_REQUIRED} or newer.", missing=missing)
    except TimeoutError:
        return _result(cycle, TIMEOUT,
                       f"unison did not finish within {TIMEOUT_SECONDS}s and "
                       "was stopped. Both folders were left as they are.",
                       missing=missing)
    except OSError as exc:                            # pragma: no cover - defensive
        return _result(cycle, ERROR, f"could not run unison: {exc}",
                       missing=missing)

    outcome = classify_exit(result.returncode)
    if outcome == SYNCHRONIZED:
        # The baseline advances only here (section 7.6), and only after the
        # engine itself reported a clean run.
        workspaces.write_json_atomic(
            workspaces.baseline_path(workspace.id, cycle.base),
            _baseline_document(local, remote, _timestamp(cycle.now)))
        return _result(cycle, SYNCHRONIZED, exit_code=result.returncode,
                       missing=missing, ran_unison=True, success=True)
    paths = skipped_paths(result)
    detail = summarize(result)
    if outcome == CONFLICT:
        return _result(cycle, CONFLICT,
                       "Both folders kept their own version of the paths "
                       "below. " + (detail or "some paths were skipped."),
                       exit_code=result.returncode, conflicts=len(paths),
                       missing=missing, paths=paths, ran_unison=True)
    return _result(cycle, outcome,
                   detail or f"unison exited {result.returncode}.",
                   exit_code=result.returncode, missing=missing, paths=paths,
                   ran_unison=True)
