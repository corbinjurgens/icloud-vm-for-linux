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
import subprocess
from dataclasses import dataclass
from typing import Callable

CONTAINER_NAME = "icloud-windows"
HELPER_PATH = "/usr/local/bin/icloud-bridge-power"
MARKER_PATH = "/var/lib/icloud-bridge/powered-off"

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


def _default_runner(argv: list[str], timeout: float) -> RunResult:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:      # normalize for callers/tests
        raise TimeoutError(f"{argv[0]} timed out after {timeout}s") from exc
    return RunResult(completed.returncode, completed.stdout, completed.stderr)


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


def inspect_container(runner: Runner = _default_runner,
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
        if "No such object" in stderr or "No such container" in stderr:
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


# --------------------------------------------------------- invoking the helper --

@dataclass(frozen=True)
class HelperResult:
    """Outcome of a helper invocation, ready for the controller to display."""
    success: bool
    exit_code: int | None
    message: str


def _run_helper(action: str, runner: Runner, timeout: float) -> HelperResult:
    argv = ["sudo", "-n", HELPER_PATH, action]
    try:
        result = runner(argv, timeout)
    except FileNotFoundError:
        return HelperResult(False, None, "sudo is not available on this system.")
    except TimeoutError:
        return HelperResult(
            False, None,
            f"Timed out after {int(timeout)}s waiting for the bridge to turn {action}.")
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


def power_on(runner: Runner = _default_runner,
             *, timeout: float = POWER_TIMEOUT_SECONDS) -> HelperResult:
    """Run ``sudo -n icloud-bridge-power on``."""
    return _run_helper("on", runner, timeout)


def power_off(runner: Runner = _default_runner,
              *, timeout: float = POWER_TIMEOUT_SECONDS) -> HelperResult:
    """Run ``sudo -n icloud-bridge-power off``."""
    return _run_helper("off", runner, timeout)
