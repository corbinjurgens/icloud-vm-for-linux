"""Qt-free power model for the GUI-managed bridge lifecycle (v2 plan D29).

This module answers two questions for the controller and performs no mount I/O:

1. *Should startup turn the bridge on?*  It inspects Docker as the desktop user
   and reads the durable desired-off marker, then classifies the situation into
   one of a small set of plans.  Deciding is separated from acting so the whole
   decision table is unit-testable without Docker, sudo, systemd, or Qt.
2. *Turn the bridge on/off* by invoking the privileged helper with an exact argv
   list (``sudo -n /usr/local/bin/icloud-bridge-power on|off``) — never a shell —
   and returning the helper's own human-readable result.

The controller, not this module, enforces the "only power on at process start"
rule: it calls :func:`plan_startup` once during construction and never again.
A container that a user stops by hand mid-session is reported red, not restarted.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

CONTAINER_NAME = "icloud-windows"
HELPER_PATH = "/usr/local/bin/icloud-bridge-power"
MARKER_PATH = "/var/lib/icloud-bridge/powered-off"

#: The bridge's container always lives on the native Engine's socket.  Docker
#: Desktop can point the desktop user's *active context* at `desktop-linux`,
#: whose daemon knows nothing about `icloud-windows` — `docker inspect` then
#: reports "no such object" and the GUI concludes the VM was never created.
#: Pinning the socket for the container calls makes the answer independent of
#: whatever context the user's shell or Desktop last selected.
DOCKER_SOCKET = "unix:///var/run/docker.sock"

DOCKER_TIMEOUT_SECONDS = 5
# The helper's own deadlines are ~200 s (off) and ~300 s plus boot (on); give the
# subprocess call generous headroom above those so we surface the helper's error
# rather than our own timeout.
POWER_TIMEOUT_SECONDS = 600

_STOPPED_STATES = frozenset({"exited", "created", "dead"})
_RUNNING_STATES = frozenset({"running", "restarting", "paused"})


@dataclass(frozen=True)
class RunResult:
    """The minimal result the runner returns, so a fake runner is trivial."""
    returncode: int
    stdout: str = ""
    stderr: str = ""


#: A runner takes an argv list and a timeout and returns a :class:`RunResult`.
#: It may raise ``FileNotFoundError`` (command absent) or ``TimeoutError``.
Runner = Callable[[list[str], float], RunResult]


def _run(argv: list[str], timeout: float, env: dict[str, str] | None) -> RunResult:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired as exc:      # normalize for callers/tests
        raise TimeoutError(f"{argv[0]} timed out after {timeout}s") from exc
    return RunResult(completed.returncode, completed.stdout, completed.stderr)


def default_runner(argv: list[str], timeout: float) -> RunResult:
    """Run a command with this process's environment untouched.

    Used for ``sudo``: the helper resolves its own Docker configuration as root,
    and forcing a ``DOCKER_HOST`` onto unrelated commands would be a side effect
    with no upside.
    """
    return _run(argv, timeout, None)


def docker_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment with ``DOCKER_HOST`` pinned to the native socket.

    A *copy* with a single override, never a replacement: the CLI still needs
    ``HOME``, ``PATH`` and the proxy variables it was launched with.
    """
    env = dict(os.environ if environ is None else environ)
    env["DOCKER_HOST"] = DOCKER_SOCKET
    return env


def docker_runner(argv: list[str], timeout: float) -> RunResult:
    """Run a ``docker`` command against the native Engine socket (item 3)."""
    return _run(argv, timeout, docker_env())


# ------------------------------------------------------- streaming a command --
# v2 plan D38. The helper already prints one `==> ` line per step; streamed live,
# that stdout *is* the progress feed. There is no separate progress channel, no
# file under /run, and no socket.

#: Phase lines are presentation only. Their wording may change freely, so nothing
#: may parse past this prefix into a control decision.
PHASE_PREFIX = "==> "

#: Bounds on what a child can push at us. The tail is what an error dialog and a
#: diagnostic report get to quote.
MAX_TAIL_LINES = 50
MAX_TAIL_BYTES = 64 * 1024
MAX_LINE_CHARS = 500

#: ANSI CSI/OSC escapes, plus any remaining C0/C1 control character. A helper
#: running under a pty-less pipe should not emit these, but `docker compose`
#: certainly does, and they would render as mojibake in a Qt label.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_line(text: str) -> str:
    """One display-safe, bounded line: no escapes, no control characters."""
    cleaned = _CONTROL_RE.sub("", _ANSI_RE.sub("", text)).strip()
    if len(cleaned) > MAX_LINE_CHARS:
        return cleaned[:MAX_LINE_CHARS] + "…"
    return cleaned


def phase_of(line: str) -> str | None:
    """The human phase text of a `==> ` line, or ``None`` for anything else."""
    if line.startswith(PHASE_PREFIX):
        return line[len(PHASE_PREFIX):].strip() or None
    return None


def _trim(lines: list[str]) -> None:
    """Keep the tail within both bounds, oldest first."""
    while len(lines) > MAX_TAIL_LINES:
        lines.pop(0)
    total = sum(len(line) + 1 for line in lines)
    while lines and total > MAX_TAIL_BYTES:
        total -= len(lines.pop(0)) + 1


def _iter_lines(stream):                     # pragma: no cover - thread body
    """Yield decoded lines, breaking on newline **and** carriage return.

    `docker compose` reports pull progress by rewriting one line with `\\r`;
    splitting on `\\n` alone would buffer the whole download into one giant line.
    """
    buffer = bytearray()
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        if chunk in (b"\n", b"\r"):
            yield buffer.decode("utf-8", errors="replace")
            buffer.clear()
            continue
        buffer.extend(chunk)
        if len(buffer) > MAX_LINE_CHARS * 4:
            yield buffer.decode("utf-8", errors="replace")
            buffer.clear()
    if buffer:
        yield buffer.decode("utf-8", errors="replace")


def stream_command(argv: list[str], timeout: float,
                   on_line: Callable[[str], None] | None = None,
                   *, env: dict[str, str] | None = None) -> RunResult:
    """Run ``argv``, delivering each output line as it arrives.

    Both pipes are drained by their own thread, because a child that fills one
    while we read the other would deadlock. The monotonic deadline is enforced
    on ``wait()``, so a child that prints nothing at all still times out. A
    callback that raises cannot abort the transaction — the operator's power-off
    must not fail because a label refused to update.
    """
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env)
    except FileNotFoundError:
        raise
    except OSError as exc:                          # pragma: no cover - defensive
        raise OSError(f"could not run {argv[0]}: {exc}") from exc

    out: list[str] = []
    err: list[str] = []

    def drain(stream, sink: list[str], deliver: bool) -> None:   # pragma: no cover
        try:
            for raw in _iter_lines(stream):
                line = sanitize_line(raw)
                if not line:
                    continue
                sink.append(line)
                _trim(sink)
                if deliver and on_line is not None:
                    try:
                        on_line(line)
                    except Exception:               # noqa: BLE001 - never fatal
                        pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    readers = [
        threading.Thread(target=drain, args=(process.stdout, out, True), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, err, True), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join(timeout=1.0)
        raise TimeoutError(f"{argv[0]} timed out after {timeout}s")
    for reader in readers:
        reader.join(timeout=5.0)
    return RunResult(process.returncode, "\n".join(out), "\n".join(err))


def streaming_runner(on_line: Callable[[str], None] | None,
                     *, env: dict[str, str] | None = None) -> Runner:
    """A :data:`Runner` that streams its child's output to ``on_line``."""
    def run(argv: list[str], timeout: float) -> RunResult:
        return stream_command(argv, timeout, on_line, env=env)
    return run


# ------------------------------------------------------------ docker inspect --

@dataclass(frozen=True)
class DockerStatus:
    """Classified container state.

    ``state`` is one of ``absent`` (no such container), ``stopped``
    (created/exited/dead — a clean off state), ``running`` (running/restarting/
    paused), or ``error`` (docker missing, daemon down, or an unrecognized state
    we must not mistake for stopped).  ``raw`` keeps the exact status word and
    ``detail`` carries any error text.
    """
    state: str
    raw: str = ""
    detail: str = ""


def inspect_container(runner: Runner = docker_runner,
                      *, name: str = CONTAINER_NAME) -> DockerStatus:
    """Classify the container without ever mutating it."""
    argv = ["docker", "inspect", "-f", "{{.State.Status}}", name]
    try:
        result = runner(argv, DOCKER_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return DockerStatus("error", detail="docker is not installed or not on PATH")
    except TimeoutError:
        return DockerStatus("error", detail="docker inspect timed out")
    except OSError as exc:                          # pragma: no cover - defensive
        return DockerStatus("error", detail=f"docker inspect failed: {exc}")

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # Match case-insensitively: the CLI changed this message's casing between
        # major versions ("Error: No such object:" up to 28, "error: no such
        # object:" on 29). Getting it wrong is not merely cosmetic — an absent
        # container would be classified as an inspect error, replacing the
        # first-run "create it with docker compose up -d" guidance with a raw
        # daemon message at exactly the moment a new operator needs the guidance.
        lowered = stderr.lower()
        if "no such object" in lowered or "no such container" in lowered:
            return DockerStatus("absent", detail=stderr)
        return DockerStatus("error", detail=stderr or "docker inspect returned non-zero")

    raw = (result.stdout or "").strip()
    if raw in _RUNNING_STATES:
        return DockerStatus("running", raw=raw)
    if raw in _STOPPED_STATES:
        return DockerStatus("stopped", raw=raw)
    return DockerStatus("error", raw=raw,
                        detail=f"unrecognized container state {raw!r}")


# --------------------------------------------------------------- the marker --

def marker_exists(path: str = MARKER_PATH,
                  *, exists: Callable[[str], bool] = os.path.exists) -> bool:
    """Whether the durable desired-off marker is present (injectable for tests)."""
    return exists(path)


# ------------------------------------------------------------ launch decision --

# Startup plan kinds:
POWER_ON = "power_on"            # run `on`: marker set, or a cleanly stopped VM
ALREADY_ON = "already_on"        # VM running, no marker: continue normal init
PROVISION_NEEDED = "provision_needed"   # no container: today's red first-run state
INSPECT_ERROR = "inspect_error"  # inspect failed/ambiguous: show, do not mutate


@dataclass(frozen=True)
class StartupPlan:
    kind: str
    detail: str = ""


def plan_startup(marker: bool, status: DockerStatus) -> StartupPlan:
    """Decide what startup should do.  Pure — no side effects.

    * inspect error or ambiguous state -> show it, never mutate.
    * no container -> preserve the red first-provisioning state; the helper's
      ``on`` would only fail and leave the marker anyway, so do not run it.
    * container running -> normal init, unless the marker says it was meant to be
      off (started by hand while disarmed); then ``on`` reconciles marker + mounts.
    * cleanly stopped, or marker present -> power on.
    """
    if status.state == "error":
        return StartupPlan(INSPECT_ERROR, status.detail)
    if status.state == "absent":
        return StartupPlan(PROVISION_NEEDED, status.detail)
    if status.state == "running":
        if marker:
            return StartupPlan(POWER_ON, "container is running but marked off; reconciling")
        return StartupPlan(ALREADY_ON)
    # stopped
    return StartupPlan(POWER_ON, f"container is {status.raw or 'stopped'}")


# ---------------------------------------------------- in-session lifecycle --
# D30. The controller is in exactly one of these at any moment. They are *not*
# health colours: red can mean a running VM with a stale canary, a missing mount
# or bad JSON, and must never by itself offer to start anything.

LIFECYCLE_STARTING = "starting"          # power-on transition in flight
LIFECYCLE_RUNNING = "running"            # normal monitoring
LIFECYCLE_START_FAILED = "start_failed"  # power-on failed; wait for Retry
LIFECYCLE_POWERED_OFF = "powered_off"    # this app powered it off and kept running
LIFECYCLE_SHUTTING_DOWN = "shutting_down"
LIFECYCLE_SETUP = "setup"                # no container yet, or inspection failed
#: D31: the container exists and Windows is installing itself. SMB can be
#: legitimately absent for far longer than the helper's five-minute readiness
#: deadline, so this state waits for the operator rather than calling `on`.
LIFECYCLE_PROVISIONING = "provisioning"
#: D38: the outer subprocess timeout fired, so the helper's outcome is unknown.
#: Killing an unprivileged `sudo` is no evidence the root helper stopped.
LIFECYCLE_TRANSITION_UNKNOWN = "transition_unknown"

#: What the tray/Status-tab lifecycle control offers, if anything.
ACTION_NONE = "none"
ACTION_POWER_OFF = "power_off"
ACTION_START = "start"
ACTION_RETRY = "retry"
#: D38: repeat the interrupted transaction, whichever direction it was going.
ACTION_RETRY_TRANSITION = "retry_transition"
ACTION_SETUP = "setup"


def available_action(lifecycle: str, container: str | None) -> str:
    """The single power action to offer, from the lifecycle and Docker state.

    ``container`` is the last :func:`inspect_container` classification, or
    ``None`` when none has been made yet.  Deciding here — rather than from a
    health colour — is the point of D30: only a *definitive* Docker answer
    enables a mutating action.
    """
    if lifecycle == LIFECYCLE_POWERED_OFF:
        return ACTION_START
    if lifecycle == LIFECYCLE_START_FAILED:
        return ACTION_RETRY
    if lifecycle == LIFECYCLE_TRANSITION_UNKNOWN:
        # The only mutating control this state offers. Everything read-only
        # (Open VM screen, the diagnostic export) stays available.
        return ACTION_RETRY_TRANSITION
    if lifecycle in (LIFECYCLE_SETUP, LIFECYCLE_PROVISIONING):
        # No container to start, or we cannot tell: offer setup, never power.
        # The assistant itself is the surface here, not a one-click action.
        return ACTION_SETUP if container == "absent" else ACTION_NONE
    if lifecycle != LIFECYCLE_RUNNING:
        return ACTION_NONE          # a transition owns the state
    if container == "running":
        return ACTION_POWER_OFF
    if container == "stopped":
        # Definitively exited/created/dead: recoverable without a restart of
        # this app. Anything else — absent, an inspect error, an unrecognized
        # state — offers nothing.
        return ACTION_START
    return ACTION_NONE


# --------------------------------------------------------- invoking the helper --

@dataclass(frozen=True)
class HelperResult:
    """Outcome of a helper invocation, ready for the controller to display.

    ``timed_out`` is not just another failure: we killed our own unprivileged
    `sudo`, which says nothing about whether the root helper stopped. The
    controller routes it to D38's `transition_unknown` rather than to the
    ordinary failure path, which would resume polling against shares the helper
    may already have unmounted.
    """
    success: bool
    exit_code: int | None
    message: str
    timed_out: bool = False


def _run_helper(action: str, runner: Runner, timeout: float) -> HelperResult:
    argv = ["sudo", "-n", HELPER_PATH, action]
    try:
        result = runner(argv, timeout)
    except FileNotFoundError:
        return HelperResult(False, None, "sudo is not available on this system.")
    except TimeoutError:
        return HelperResult(
            False, None,
            f"Timed out after {int(timeout)}s waiting for the bridge to turn {action}. "
            "The privileged helper may still be running and reconciling the "
            "mounts, so nothing here will be read or changed until you retry.",
            timed_out=True)
    except OSError as exc:                          # pragma: no cover - defensive
        return HelperResult(False, None, f"could not run the power helper: {exc}")

    if result.returncode == 0:
        message = (result.stdout or "").strip().splitlines()[-1:] or [f"bridge {action}"]
        return HelperResult(True, 0, message[0])
    # Prefer the helper's stderr — it is the human-readable, unlocalized error the
    # GUI is meant to show verbatim (v2 plan D29 forbids parsing systemctl output).
    detail = (result.stderr or "").strip() or (result.stdout or "").strip()
    if not detail:
        detail = f"icloud-bridge-power {action} failed (exit {result.returncode})."
    return HelperResult(False, result.returncode, detail)


def _resolve_runner(runner: Runner | None,
                    on_line: Callable[[str], None] | None) -> Runner:
    """The caller's runner, or a streaming/plain default to match ``on_line``.

    Passing an explicit runner still wins, so every existing fake keeps working
    and the no-callback result and error precedence are unchanged.
    """
    if runner is not None:
        return runner
    return default_runner if on_line is None else streaming_runner(on_line)


def power_on(runner: Runner | None = None,
             *, timeout: float = POWER_TIMEOUT_SECONDS,
             on_line: Callable[[str], None] | None = None) -> HelperResult:
    """Run ``sudo -n icloud-bridge-power on``, optionally streaming its output."""
    return _run_helper("on", _resolve_runner(runner, on_line), timeout)


def power_off(runner: Runner | None = None,
              *, timeout: float = POWER_TIMEOUT_SECONDS,
              on_line: Callable[[str], None] | None = None) -> HelperResult:
    """Run ``sudo -n icloud-bridge-power off``, optionally streaming its output."""
    return _run_helper("off", _resolve_runner(runner, on_line), timeout)
