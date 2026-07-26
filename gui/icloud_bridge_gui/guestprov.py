"""Host side of app-driven Windows guest provisioning (v2 plan D40-D44, §4.1).

The guest is configured by staging the repository's own provisioning scripts on
a **read-only-to-the-guest** Samba share inside dockur's container, writing a
trigger last, and then watching the effects an elevated in-guest watcher
publishes as JSON.  This module owns the whole host half of that channel:
building the share, staging a run, delivering the share password at exactly the
moment the guest asks for it, reading status, and cleaning up.

Deliberate boundaries:

* **No Qt and no mount I/O.**  Everything here is `docker exec`/`docker cp`
  against the container, plus reading files from the installed bundle and the
  operator's chosen env file.  Provisioning runs with CIFS paused (D31/D43), so
  this module must never be what breaks that.
* **Every subprocess goes through an injected adapter**, so the whole decision
  table is unit-testable with fakes and no test needs Docker.
* **Nothing runtime-valued is interpolated into shell source.**  Each `sh -c`
  program is a module constant built only from other module constants; run IDs,
  bundle paths, status text, config text and the password travel as argv
  elements or over stdin, never as shell syntax.
* **The password (D41).**  This is the only GUI module permitted to *return*
  the `SHARE_PASS` value, narrowing D31's "never handled" boundary as tightly as
  the feature allows: it is read from the explicitly selected env file only
  after the guest reports `waiting-for-secret`, streamed over `docker exec -i`
  stdin into a mode-0600 temporary file, and atomically renamed to `secret`.
  It is never in argv, never in the environment, never in a host temporary
  file, never logged, never in a status, and never persisted.
* **The status channel is untrusted.**  It is guest-writable by design, so
  every field is validated against the fixed enums of §4.1/§4.2 before anything
  is believed, a mismatched run ID is "no acknowledgement yet" rather than
  progress, and a malformed document is a distinct unreadable result — never a
  crash and never progress.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from . import envfile
from .power import (CONTAINER_NAME, RunResult, Runner, docker_env, docker_runner,
                    sanitize_line)

if TYPE_CHECKING:                            # pragma: no cover - typing only
    from .firstrun import Bundle


class ProvisioningError(Exception):
    """The host could not establish, stage, or clean up the guest channel."""


# --------------------------------------------------------------- constants --

#: Executable inbox: host-writable through Docker, read-only to the guest.
INBOX_DIR = "/run/icloud-bridge-provision"
TRIGGER_NAME = "trigger.json"
SECRET_NAME = "secret"

#: Status outbox: dockur's existing guest-writable `Data` share. Never an
#: execution source, and never written by the host.
STATUS_DIR = "/tmp/smb/.provision"
STATUS_PATH = STATUS_DIR + "/status.json"

#: dockur generates this file at container start; `ensure_channel` reconstructs
#: only its own marker block inside it (D40).
SMB_CONF = "/etc/samba/smb.conf"
#: The candidate lives in the same directory so the swap is an atomic rename.
SMB_CONF_CANDIDATE = "/etc/samba/smb.conf.icloud-bridge-candidate"

SHARE_NAME = "Provision"

#: The six files D42 stages on every trigger. The watcher copies exactly this
#: allowlist into its protected per-run directory and executes only those
#: copies.
PAYLOAD_FILES = (
    "03-create-share.ps1",
    "04-bridge-agent.ps1",
    "agent.ps1",
    "guest-state.ps1",
    "guest-setup.ps1",
    "watcher.ps1",
)

#: Prefix for the temporary name each staged file is written under before its
#: atomic rename, so a half-copied payload is never visible to the watcher.
STAGING_PREFIX = ".tmp-"

TRIGGER_VERSION = 1
STATUS_VERSION = 1
TRIGGER_ACTION = "reconcile"

DOCKER_TIMEOUT_SECONDS = 20
#: `docker cp` moves a few tens of kilobytes, but the daemon can be busy.
COPY_TIMEOUT_SECONDS = 60

#: §4.1: a status read is capped at 64 KiB, and `detail`/`error` at 500
#: characters after control-character removal (`power.sanitize_line`).
MAX_STATUS_BYTES = 64 * 1024

# ------------------------------------------------------------------ phases --

PHASE_STAGING = "staging"
PHASE_INSPECTING = "inspecting"
PHASE_INSTALLING_ICLOUD = "installing-icloud"
PHASE_LAUNCHING_ICLOUD = "launching-icloud"
PHASE_WAITING_FOR_SIGNIN = "waiting-for-signin"
PHASE_WAITING_FOR_SECRET = "waiting-for-secret"
PHASE_CREATING_SHARE = "creating-share"
PHASE_INSTALLING_BRIDGE_BOUNDARY = "installing-bridge-boundary"
PHASE_INSTALLING_AGENT = "installing-agent"
PHASE_VERIFYING = "verifying"
PHASE_DONE = "done"

#: In the order they occur when the corresponding work exists (§4.1).
PHASES = (
    PHASE_STAGING, PHASE_INSPECTING, PHASE_INSTALLING_ICLOUD,
    PHASE_LAUNCHING_ICLOUD, PHASE_WAITING_FOR_SIGNIN, PHASE_WAITING_FOR_SECRET,
    PHASE_CREATING_SHARE, PHASE_INSTALLING_BRIDGE_BOUNDARY,
    PHASE_INSTALLING_AGENT, PHASE_VERIFYING, PHASE_DONE,
)

#: Locally assigned classifications that are *not* guest phases. They can never
#: collide with one, because the guest's phase is validated against `PHASES`.
PHASE_ABSENT = "absent"          # no status file yet: the watcher has not run
PHASE_UNREADABLE = "unreadable"  # malformed, oversized, or unparseable
PHASE_STALE = "stale"            # a valid document for a different run ID

#: The two phases that wait on a human. `waiting-for-signin` is the manual Apple
#: sign-in and has no deadline at all; `waiting-for-secret` waits for this app.
#: Both still expect the orchestrator's 30 s heartbeat.
WAITING_PHASES = frozenset({PHASE_WAITING_FOR_SIGNIN, PHASE_WAITING_FOR_SECRET})

#: §4.1 host-side deadlines. Store readiness makes the winget phase legitimately
#: slow; everything else gets five minutes.
PHASE_DEADLINE_SECONDS = {
    PHASE_INSTALLING_ICLOUD: 600.0,
    PHASE_WAITING_FOR_SIGNIN: None,
    PHASE_WAITING_FOR_SECRET: None,
}
DEFAULT_PHASE_DEADLINE_SECONDS = 300.0
#: The orchestrator rewrites a heartbeat at least every 30 s, which is what
#: makes this much silence meaningful rather than guaranteed noise.
HEARTBEAT_STALL_SECONDS = 120.0

# ----------------------------------------------- the fixed status checklist --

#: §4.2, in render order. The complete set must be present in every status.
CHECK_KEYS = (
    "icloudPackage", "syncRoot", "shareAccount", "shareCredential",
    "dataShare", "bridgeBoundary", "agentInstall", "agentRuntime",
)
CHECK_STATES = frozenset({
    "pending", "ok", "missing", "drifted", "blocked", "unknown", "unverifiable",
})
WORK_IDS = (
    "install-icloud", "wait-for-signin", "create-share-account",
    "reset-share-credential", "repair-data-share", "repair-bridge-boundary",
    "update-agent",
)

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ------------------------------------------------------- the Samba stanza --
# Fixed text: no runtime value appears here, which is why it is safe to embed
# in the candidate configuration at all.

MARKER_BEGIN = "# BEGIN icloud-bridge provisioning share (v2 plan D40) - managed block"
MARKER_END = "# END icloud-bridge provisioning share (v2 plan D40)"

PROVISION_STANZA = "\n".join((
    MARKER_BEGIN,
    "[" + SHARE_NAME + "]",
    "   path = " + INBOX_DIR,
    "   read only = yes",
    "   guest ok = yes",
    "   guest only = yes",
    "   force user = root",
    MARKER_END,
))

_SECTION_RE = re.compile(r"^\s*\[\s*([^\]]*?)\s*\]")


# ------------------------------------------------------- fixed shell source --
# Every one of these is a module constant assembled only from other module
# constants. Nothing a run, an operator, the bundle, the guest or the env file
# supplies is ever interpolated into shell source (§4.1).

_ENSURE_INBOX_COMMAND = (
    "set -e\n"
    f"mkdir -p {INBOX_DIR}\n"
    f"chown root:root {INBOX_DIR}\n"
    f"chmod 700 {INBOX_DIR}\n"
)

_READ_SMB_CONF_COMMAND = f"head -c 262144 -- {SMB_CONF}\n"

_WRITE_CANDIDATE_COMMAND = (
    "set -e\n"
    "umask 022\n"
    f"cat > {SMB_CONF_CANDIDATE}\n"
    f"chmod 644 {SMB_CONF_CANDIDATE}\n"
)

_TEST_CANDIDATE_COMMAND = f"testparm -s {SMB_CONF_CANDIDATE} >/dev/null\n"

_DISCARD_CANDIDATE_COMMAND = f"rm -f {SMB_CONF_CANDIDATE}\n"

#: The rename is the swap; the reload is best effort because the verification
#: below reads the effective configuration rather than trusting this exit code.
_ACTIVATE_CANDIDATE_COMMAND = (
    "set -e\n"
    f"mv -f {SMB_CONF_CANDIDATE} {SMB_CONF}\n"
    "smbcontrol smbd reload-config >/dev/null 2>&1 || pkill -HUP smbd || true\n"
)

_VERIFY_PATH_COMMAND = (
    f"testparm -s --section-name {SHARE_NAME} --parameter-name path {SMB_CONF} "
    "2>/dev/null\n"
)
_VERIFY_READ_ONLY_COMMAND = (
    f"testparm -s --section-name {SHARE_NAME} --parameter-name 'read only' "
    f"{SMB_CONF} 2>/dev/null\n"
)

_RESET_INBOX_COMMAND = (
    "set -e\n"
    f"rm -rf {INBOX_DIR}\n"
    f"mkdir -p {INBOX_DIR}\n"
    f"chown root:root {INBOX_DIR}\n"
    f"chmod 700 {INBOX_DIR}\n"
)

#: `$f` is a shell variable over the fixed allowlist, not an interpolation.
_PROMOTE_PAYLOAD_COMMAND = (
    "set -e\n"
    f"cd {INBOX_DIR}\n"
    "for f in " + " ".join(PAYLOAD_FILES) + "; do\n"
    f'  test -f "{STAGING_PREFIX}$f"\n'
    f'  mv -f "{STAGING_PREFIX}$f" "$f"\n'
    "done\n"
)

_WRITE_TRIGGER_COMMAND = (
    "set -e\n"
    "umask 022\n"
    f"cd {INBOX_DIR}\n"
    f"cat > {STAGING_PREFIX}{TRIGGER_NAME}\n"
    f"mv -f {STAGING_PREFIX}{TRIGGER_NAME} {TRIGGER_NAME}\n"
)

_WRITE_SECRET_COMMAND = (
    "set -e\n"
    "umask 077\n"
    f"cd {INBOX_DIR}\n"
    f"cat > {STAGING_PREFIX}{SECRET_NAME}\n"
    f"chmod 600 {STAGING_PREFIX}{SECRET_NAME}\n"
    f"mv -f {STAGING_PREFIX}{SECRET_NAME} {SECRET_NAME}\n"
)

_SECRET_PRESENT_COMMAND = f"test -e {INBOX_DIR}/{SECRET_NAME}\n"

_READ_TRIGGER_COMMAND = (
    f"f={INBOX_DIR}/{TRIGGER_NAME}\n"
    '[ -f "$f" ] || exit 9\n'
    'head -c 65536 -- "$f"\n'
)

#: Exit 9 means "no status yet", which is the ordinary pre-acknowledgement
#: state and not an error. The byte cap must stay `MAX_STATUS_BYTES`; a test
#: pins the two together.
_READ_STATUS_COMMAND = (
    f"f={STATUS_PATH}\n"
    '[ -f "$f" ] || exit 9\n'
    "stat -c '%Y %s' -- \"$f\" || exit 8\n"
    f'head -c {MAX_STATUS_BYTES} -- "$f"\n'
)

#: Cleanup removes executable inbox content and any secret. It deliberately
#: names no path under the status outbox: the matching terminal status is the
#: evidence D43 needs to resume after a crash, and only clearing the record may
#: discard it.
_CLEANUP_INBOX_COMMAND = (
    f"cd {INBOX_DIR} 2>/dev/null || exit 0\n"
    f"rm -f {TRIGGER_NAME} {SECRET_NAME} " + " ".join(PAYLOAD_FILES) + "\n"
    f"rm -f {STAGING_PREFIX}* 2>/dev/null\n"
    "exit 0\n"
)


# ------------------------------------------------------------- the runners --

#: A runner that can deliver bytes on the child's stdin.  `power.Runner` has no
#: stdin parameter and is not going to grow one: the password must reach the
#: container without ever appearing in argv, so this is a separate, narrow
#: adapter rather than a widened version of the existing one.
InputRunner = Callable[[list[str], float, bytes], RunResult]


def default_input_runner(argv: list[str], timeout: float,
                         input_bytes: bytes) -> RunResult:
    """Run ``argv`` against the native Docker socket, feeding it ``input_bytes``.

    Binary in, text out: the payload is written to the child's stdin exactly as
    given (no encoding, no added newline), while the captured output is decoded
    leniently because it is only ever shown as a diagnostic.
    """
    try:
        completed = subprocess.run(argv, input=input_bytes, capture_output=True,
                                   timeout=timeout, check=False, env=docker_env())
    except subprocess.TimeoutExpired as exc:     # normalize for callers/tests
        raise TimeoutError(f"{argv[0]} timed out after {timeout}s") from exc
    return RunResult(completed.returncode,
                     completed.stdout.decode("utf-8", errors="replace"),
                     completed.stderr.decode("utf-8", errors="replace"))


def _exec_argv(command: str) -> list[str]:
    return ["docker", "exec", CONTAINER_NAME, "sh", "-c", command]


def _exec_input_argv(command: str) -> list[str]:
    return ["docker", "exec", "-i", CONTAINER_NAME, "sh", "-c", command]


def _run(runner: Runner, argv: list[str], timeout: float) -> RunResult:
    try:
        return runner(argv, timeout)
    except FileNotFoundError as exc:
        raise ProvisioningError(f"{argv[0]} is not installed or not on PATH") from exc
    except TimeoutError as exc:
        raise ProvisioningError(f"{argv[0]} timed out after {int(timeout)}s") from exc
    except OSError as exc:                       # pragma: no cover - defensive
        raise ProvisioningError(f"{argv[0]} failed: {exc}") from exc


def _run_input(input_runner: InputRunner, argv: list[str], timeout: float,
               payload: bytes) -> RunResult:
    try:
        return input_runner(argv, timeout, payload)
    except FileNotFoundError as exc:
        raise ProvisioningError(f"{argv[0]} is not installed or not on PATH") from exc
    except TimeoutError as exc:
        raise ProvisioningError(f"{argv[0]} timed out after {int(timeout)}s") from exc
    except OSError as exc:                       # pragma: no cover - defensive
        raise ProvisioningError(f"{argv[0]} failed: {exc}") from exc


def _detail(result: RunResult) -> str:
    """A bounded, display-safe diagnostic from a failed container command."""
    return sanitize_line((result.stderr or result.stdout or "").strip()) or \
        f"exit {result.returncode}"


# -------------------------------------------------------------- run IDs --

def new_run_id() -> str:
    """A fresh run ID: 32 lowercase hex characters (§4.1)."""
    return uuid.uuid4().hex


def validate_run_id(run_id: object) -> str:
    """Return ``run_id`` if it is exactly 32 lowercase hex characters.

    Validated on both sides and used as a path component in the guest, so a
    timestamp, a truncated UUID, or anything with a separator in it is refused
    rather than sanitized.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError("a run ID must be 32 lowercase hexadecimal characters")
    return run_id


# --------------------------------------------------------- the Samba share --

def build_candidate_config(current: str) -> str:
    """dockur's configuration with exactly one managed ``[Provision]`` block.

    Pure text work, so the whole policy is testable: the existing managed block
    (if any) is removed and rebuilt, which makes repeated runs idempotent, and a
    ``[Provision]`` section *outside* a complete managed block is a conflict
    that fails closed.  Merging into or silently overriding a stanza this app
    did not write would be taking ownership of a share whose meaning we do not
    know.
    """
    kept: list[str] = []
    depth = 0
    for line in current.split("\n"):
        if line.strip() == MARKER_BEGIN:
            if depth:
                raise ProvisioningError(
                    f"{SMB_CONF} contains a nested provisioning marker block; "
                    "refusing to guess what it means")
            depth = 1
            continue
        if line.strip() == MARKER_END:
            if not depth:
                raise ProvisioningError(
                    f"{SMB_CONF} contains a provisioning end marker with no "
                    "start marker; refusing to guess what it means")
            depth = 0
            continue
        if depth:
            continue
        kept.append(line)
    if depth:
        raise ProvisioningError(
            f"{SMB_CONF} has an unterminated provisioning marker block; "
            "refusing to replace a configuration we cannot parse")

    for line in kept:
        match = _SECTION_RE.match(line)
        if match and match.group(1).casefold() == SHARE_NAME.casefold():
            raise ProvisioningError(
                f"{SMB_CONF} already defines a [{SHARE_NAME}] share outside this "
                "app's managed block; provisioning will not merge with or "
                "override it")

    body = "\n".join(kept).rstrip("\n")
    return (body + "\n\n" if body else "") + PROVISION_STANZA + "\n"


def ensure_channel(runner: Runner = docker_runner,
                   input_runner: InputRunner = default_input_runner) -> None:
    """Install and verify the read-only ``Provision`` share (D40).

    The candidate configuration is built here, in Python, and delivered over
    **stdin**: it must not be interpolated into shell source, and building it
    here is also what makes the conflict and idempotency rules testable.  The
    replacement is an atomic rename inside `/etc/samba`, so a failed `testparm`
    leaves dockur's working configuration exactly as it was.

    ``input_runner`` is a parameter rather than an implementation detail for the
    same reason it exists at all: only an injected adapter can be proved, by
    test, to have carried the text on stdin.
    """
    result = _run(runner, _exec_argv(_ENSURE_INBOX_COMMAND), DOCKER_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ProvisioningError(
            f"could not create {INBOX_DIR} in the container: {_detail(result)}")

    result = _run(runner, _exec_argv(_READ_SMB_CONF_COMMAND), DOCKER_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ProvisioningError(
            f"could not read {SMB_CONF} in the container: {_detail(result)}")
    candidate = build_candidate_config(result.stdout or "")

    written = _run_input(input_runner, _exec_input_argv(_WRITE_CANDIDATE_COMMAND),
                         DOCKER_TIMEOUT_SECONDS, candidate.encode("utf-8"))
    if written.returncode != 0:
        _discard_candidate(runner)
        raise ProvisioningError(
            f"could not write the candidate Samba configuration: {_detail(written)}")

    checked = _run(runner, _exec_argv(_TEST_CANDIDATE_COMMAND), DOCKER_TIMEOUT_SECONDS)
    if checked.returncode != 0:
        _discard_candidate(runner)
        raise ProvisioningError(
            "the candidate Samba configuration failed testparm, so dockur's "
            f"configuration was left untouched: {_detail(checked)}")

    activated = _run(runner, _exec_argv(_ACTIVATE_CANDIDATE_COMMAND),
                     DOCKER_TIMEOUT_SECONDS)
    if activated.returncode != 0:
        _discard_candidate(runner)
        raise ProvisioningError(
            f"could not activate the provisioning share: {_detail(activated)}")

    verify_channel(runner)


def _discard_candidate(runner: Runner) -> None:
    """Best effort: a leftover candidate must never be mistaken for the config."""
    try:
        _run(runner, _exec_argv(_DISCARD_CANDIDATE_COMMAND), DOCKER_TIMEOUT_SECONDS)
    except ProvisioningError:                    # pragma: no cover - defensive
        pass


def verify_channel(runner: Runner = docker_runner) -> None:
    """Refuse to go further unless the *effective* share is the read-only one.

    Checked after every reload and before staging: if the guest could write
    here, the elevated watcher would be executing guest-writable code, which is
    exactly the elevation path D40 exists to prevent.
    """
    path = _run(runner, _exec_argv(_VERIFY_PATH_COMMAND), DOCKER_TIMEOUT_SECONDS)
    if path.returncode != 0 or (path.stdout or "").strip() != INBOX_DIR:
        raise ProvisioningError(
            f"the effective Samba configuration does not serve [{SHARE_NAME}] from "
            f"{INBOX_DIR}; refusing to stage provisioning code")
    read_only = _run(runner, _exec_argv(_VERIFY_READ_ONLY_COMMAND),
                     DOCKER_TIMEOUT_SECONDS)
    if read_only.returncode != 0 or \
            (read_only.stdout or "").strip().casefold() != "yes":
        raise ProvisioningError(
            f"[{SHARE_NAME}] is not read-only in the effective Samba "
            "configuration; refusing to stage provisioning code")


# ---------------------------------------------------------------- staging --

def trigger_document(run_id: str, reset_share_credential: bool) -> dict:
    """The exact non-secret trigger of §4.1.  The password is never in it."""
    return {
        "version": TRIGGER_VERSION,
        "runId": validate_run_id(run_id),
        "action": TRIGGER_ACTION,
        "resetShareCredential": reset_share_credential,
    }


def stage(bundle: "Bundle", run_id: str, reset_share_credential: bool,
          runner: Runner = docker_runner,
          input_runner: InputRunner = default_input_runner) -> None:
    """Stage one provisioning run: payload first, trigger last (D42, §4.1).

    Ordering is the whole protocol.  The watcher polls only for the trigger, so
    a trigger that appears before its payload — or before an atomic rename has
    completed — would authorize a run against files that are missing or
    half-written.  Everything therefore lands under a temporary name and is
    renamed into place, and the trigger's own rename is the last thing to
    happen.

    There is no env-file path and no secret anywhere in this operation (D41).
    """
    validate_run_id(run_id)
    if not isinstance(reset_share_credential, bool):
        # `True`-like integers are rejected on purpose: this boolean decides
        # whether the run resets a working share password, and JSON `1` is not
        # the same document as JSON `true`.
        raise ValueError("reset_share_credential must be a bool, not "
                         f"{type(reset_share_credential).__name__}")

    verify_channel(runner)

    active = poll(runner, run_id)
    if active.phase == PHASE_STALE and active.active_run:
        raise ProvisioningError(
            f"another provisioning run ({active.run_id}) is still at "
            f"'{active.document_phase}'; refusing to stage over it")

    result = _run(runner, _exec_argv(_RESET_INBOX_COMMAND), DOCKER_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ProvisioningError(
            f"could not prepare {INBOX_DIR}: {_detail(result)}")

    for name in PAYLOAD_FILES:
        source = os.path.join(bundle.provision_dir, name)
        target = f"{CONTAINER_NAME}:{INBOX_DIR}/{STAGING_PREFIX}{name}"
        copied = _run(runner, ["docker", "cp", source, target],
                      COPY_TIMEOUT_SECONDS)
        if copied.returncode != 0:
            raise ProvisioningError(
                f"could not stage {name}: {_detail(copied)}")

    promoted = _run(runner, _exec_argv(_PROMOTE_PAYLOAD_COMMAND),
                    DOCKER_TIMEOUT_SECONDS)
    if promoted.returncode != 0:
        raise ProvisioningError(
            f"could not publish the staged payload: {_detail(promoted)}")

    document = trigger_document(run_id, reset_share_credential)
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    written = _run_input(input_runner, _exec_input_argv(_WRITE_TRIGGER_COMMAND),
                         DOCKER_TIMEOUT_SECONDS, payload)
    if written.returncode != 0:
        raise ProvisioningError(f"could not write the trigger: {_detail(written)}")


# ------------------------------------------------------ delivering the secret --

def deliver_secret(env_path: str, run_id: str,
                   input_runner: InputRunner = default_input_runner,
                   *, runner: Runner = docker_runner,
                   status: "Status | None" = None) -> bool:
    """Stream ``SHARE_PASS`` into the container inbox (D41).  Returns whether
    this call delivered it.

    Deferred deliberately: the value crosses into the guest only once the guest
    is provably waiting for it, so no secret sits in the VM across the unbounded
    Apple sign-in wait.  The bytes exist in this process only as the argument to
    one runner call — never in argv, never in the environment, never in a host
    temporary file, never in a log, never in a status, never persisted.

    An already-present ``secret`` for this run is never overwritten: after a GUI
    crash the app either waits for the guest's acknowledgement or explicitly
    cleans up first, because replacing a file the orchestrator may be reading is
    how a half-written password reaches `03-create-share.ps1`.
    """
    validate_run_id(run_id)
    if status is None:
        status = poll(runner, run_id)
    if not status.acknowledged or status.phase != PHASE_WAITING_FOR_SECRET:
        raise ProvisioningError(
            "the guest is not waiting for the share password "
            f"(status: {status.phase}); nothing was sent")

    present = _run(runner, _exec_argv(_SECRET_PRESENT_COMMAND), DOCKER_TIMEOUT_SECONDS)
    if present.returncode == 0:
        return False

    result = _run_input(
        input_runner, _exec_input_argv(_WRITE_SECRET_COMMAND),
        DOCKER_TIMEOUT_SECONDS,
        # Exact UTF-8, no added newline. The expression is deliberately inline:
        # binding it to a name would leave the value in a frame Python may keep
        # alive far longer than this call.
        envfile.read_share_pass(env_path).encode("utf-8"))
    if result.returncode != 0:
        raise ProvisioningError(
            f"could not deliver the share password: {_detail(result)}")
    return True


# ----------------------------------------------------------------- polling --

@dataclass(frozen=True)
class Status:
    """One classified reading of the guest's status document.

    ``phase`` is the *host's* classification: a guest phase from `PHASES` only
    when the document is valid **and** its run ID matches, otherwise one of
    `PHASE_ABSENT`, `PHASE_UNREADABLE`, or `PHASE_STALE`.  Keeping the raw guest
    phase in ``document_phase`` instead means no caller can accidentally render
    another run's progress as this run's.
    """
    phase: str
    detail: str = ""
    error: str = ""
    acknowledged: bool = False
    run_id: str = ""
    checks: dict = field(default_factory=dict)
    work: tuple = ()
    updated_at: str = ""
    mtime: float = 0.0
    reason: str = ""
    document_phase: str = ""

    @property
    def readable(self) -> bool:
        return self.phase not in (PHASE_ABSENT, PHASE_UNREADABLE)

    @property
    def failed(self) -> bool:
        return bool(self.error)

    @property
    def terminal(self) -> bool:
        """Whether the run is over — successfully or not."""
        return self.phase == PHASE_DONE or (self.acknowledged and self.failed)

    @property
    def active_run(self) -> bool:
        """A valid, non-terminal run — whether or not it is *our* run.

        `stage` uses this to refuse to overwrite an acknowledged run that is
        still working; the answer must not depend on whose run it is.
        """
        return (self.document_phase in PHASES and self.document_phase != PHASE_DONE
                and not self.error)


def _unreadable(reason: str) -> Status:
    return Status(PHASE_UNREADABLE, reason=sanitize_line(reason))


def poll(runner: Runner, run_id: str) -> Status:
    """Read and classify the guest's status document.  Never raises.

    Everything here treats the document as hostile input: it is written through
    dockur's guest-writable `Data` share, so a compromised or merely buggy guest
    can put anything in it.  It can make this app show a wrong *warning*; it can
    never make it run something.
    """
    validate_run_id(run_id)
    try:
        result = _run(runner, _exec_argv(_READ_STATUS_COMMAND), DOCKER_TIMEOUT_SECONDS)
    except ProvisioningError as exc:
        return _unreadable(str(exc))
    if result.returncode == 9:
        return Status(PHASE_ABSENT)
    if result.returncode != 0:
        return _unreadable(f"cannot read the guest status: {_detail(result)}")

    head, _, body = (result.stdout or "").partition("\n")
    fields = head.split()
    if len(fields) != 2:
        return _unreadable("the guest status metadata is unreadable")
    try:
        mtime = float(fields[0])
        size = int(fields[1])
    except ValueError:
        return _unreadable("the guest status metadata is unreadable")
    if size > MAX_STATUS_BYTES:
        return _unreadable(f"the guest status exceeds {MAX_STATUS_BYTES} bytes")

    try:
        document = json.loads(body)
    except ValueError as exc:
        return _unreadable(f"the guest status is not valid JSON: {exc}")
    return classify_status(document, run_id, mtime=mtime)


def classify_status(document: object, run_id: str, *, mtime: float = 0.0) -> Status:
    """Validate one parsed status document against §4.1 and classify it.

    Separated from the I/O so the whole validation matrix — every key, state,
    work ID, type and bound — is exhaustible in tests without a container.
    """
    validate_run_id(run_id)
    if not isinstance(document, dict):
        return _unreadable("the guest status is not a JSON object")
    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int) \
            or version != STATUS_VERSION:
        return _unreadable("the guest status reports an unsupported version")

    reported = document.get("runId")
    if not isinstance(reported, str) or not _RUN_ID_RE.match(reported):
        return _unreadable("the guest status has no usable run ID")

    phase = document.get("phase")
    if phase not in PHASES:
        return _unreadable("the guest status reports an unknown phase")

    detail = document.get("detail")
    if not isinstance(detail, str):
        return _unreadable("the guest status has an unusable 'detail'")
    error = document.get("error")
    if error is not None and not isinstance(error, str):
        return _unreadable("the guest status has an unusable 'error'")
    updated_at = document.get("updatedAt")
    if not isinstance(updated_at, str):
        return _unreadable("the guest status has an unusable 'updatedAt'")

    checks = document.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(CHECK_KEYS):
        return _unreadable("the guest status does not carry the exact checklist")
    for key in CHECK_KEYS:
        if checks[key] not in CHECK_STATES:
            return _unreadable(f"the guest status reports an unknown state for {key}")

    work = document.get("work")
    if not isinstance(work, list) or len(work) > len(WORK_IDS):
        return _unreadable("the guest status has an unusable work list")
    for item in work:
        if item not in WORK_IDS:
            return _unreadable("the guest status names unknown work")
    if len(set(work)) != len(work):
        return _unreadable("the guest status repeats a work item")

    # Mismatch is "no acknowledgement yet", never progress: the run this app is
    # waiting on has not written anything, and another run's phase is not ours
    # to render.
    if reported != run_id:
        return Status(PHASE_STALE, run_id=reported, mtime=mtime,
                      document_phase=phase,
                      error=sanitize_line(error) if error else "",
                      reason="the guest status belongs to a different run")

    return Status(
        phase=phase,
        detail=sanitize_line(detail),
        error=sanitize_line(error) if error else "",
        acknowledged=True,
        run_id=reported,
        checks={key: checks[key] for key in CHECK_KEYS},
        work=tuple(work),
        updated_at=updated_at,
        mtime=mtime,
        document_phase=phase,
    )


def phase_deadline(phase: str) -> float | None:
    """The host-side deadline for a phase, or ``None`` where there is none."""
    if phase in PHASE_DEADLINE_SECONDS:
        return PHASE_DEADLINE_SECONDS[phase]
    if phase in PHASES:
        return DEFAULT_PHASE_DEADLINE_SECONDS
    return None


def stall_reason(phase: str, *, phase_elapsed: float, since_update: float) -> str:
    """Why this run looks stalled, or ``""`` (§4.1).

    A warning, never a decision: polling continues either way, because the guest
    may merely be slow.  The heartbeat check applies to every active phase —
    including the two waits, which have no elapsed deadline but do promise a
    30 s heartbeat — while the elapsed deadline applies only where §4.1 sets one.
    """
    if phase not in PHASES or phase == PHASE_DONE:
        return ""
    if since_update > HEARTBEAT_STALL_SECONDS:
        return (f"the guest has not updated its status for {int(since_update)}s "
                f"while in '{phase}'")
    deadline = phase_deadline(phase)
    if deadline is not None and phase_elapsed > deadline:
        return (f"'{phase}' has been running for {int(phase_elapsed)}s, longer "
                f"than the expected {int(deadline)}s")
    return ""


# ---------------------------------------------------------------- cleanup --

def cleanup(runner: Runner, run_id: str) -> bool:
    """Best-effort removal of this run's inbox content.  Returns whether it ran.

    Never touches the status outbox: D43 needs the matching terminal status to
    survive until the private record has been cleared, or a GUI that crashed
    between the two would have no evidence of how the run ended.  A trigger
    naming a *different* run is left completely alone — that run may be live,
    and its payload is not ours to delete.
    """
    validate_run_id(run_id)
    try:
        result = _run(runner, _exec_argv(_READ_TRIGGER_COMMAND), DOCKER_TIMEOUT_SECONDS)
    except ProvisioningError:
        return False
    if result.returncode == 0:
        try:
            document = json.loads(result.stdout or "")
        except ValueError:
            document = None
        if isinstance(document, dict) and document.get("runId") != run_id:
            return False
    try:
        _run(runner, _exec_argv(_CLEANUP_INBOX_COMMAND), DOCKER_TIMEOUT_SECONDS)
    except ProvisioningError:                    # pragma: no cover - defensive
        return False
    return True


# ------------------------------------------------------------ is Windows up --
# The same X.224/TPKT probe as `tools/rdp-ready.py`, deliberately reimplemented
# here: that script is the standalone operator tool and is not installed with
# the package, and the GUI must not shell out to a checkout it may not have.
# Keep the two in step if the handshake ever changes.

RDP_HOST = "127.0.0.1"
RDP_PORT = 3389
RDP_TIMEOUT_SECONDS = 5.0

#: TPKT + X.224 Connection Request with an RDP negotiation request. A plain TCP
#: connect proves nothing: docker-proxy accepts it while Windows is still
#: downloading, which once reported "guest ready" 30 seconds into a 5 GB ISO.
_X224_CONNECTION_REQUEST = bytes([
    0x03, 0x00, 0x00, 0x13, 0x0E, 0xE0, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x08, 0x00, 0x03, 0x00, 0x00, 0x00,
])


def guest_os_ready(host: str = RDP_HOST, port: int = RDP_PORT, *,
                   timeout: float = RDP_TIMEOUT_SECONDS,
                   connect: Callable = socket.create_connection) -> bool:
    """Whether Windows itself is up, as opposed to still installing.

    Used to tell "Windows is still installing" apart from "the watcher is not
    answering" — two situations whose correct advice is completely different.
    """
    try:
        sock = connect((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        sock.settimeout(timeout)
        sock.sendall(_X224_CONNECTION_REQUEST)
        data = sock.recv(19)
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:                          # pragma: no cover - defensive
            pass
    return len(data) >= 2 and data[0] == 0x03 and data[1] == 0x00
