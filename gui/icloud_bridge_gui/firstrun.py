"""First-run readiness, resource resolution, and VM creation (Qt-free, D31).

A fresh install has no Windows VM, so almost nothing the rest of the GUI does is
meaningful yet: there is no mount to stat, no bridge share to read, no health to
report.  This module answers the questions that *are* answerable at that point —
can this host run the guest at all, where are the compose file and provisioning
scripts, is the operator's env file usable, does a container exist — and builds
the exact commands for the two mutating steps (``docker compose up -d``, and the
final readiness check before handing over to the privileged power helper).

Deliberate boundaries:

* **No mount I/O.**  Everything here is Docker, `/dev` nodes, and reading files
  from the installed resource bundle.  The controller keeps CIFS paused for the
  whole of setup, so this module must never be the thing that breaks that.
* **No Qt**, and every subprocess/filesystem touch goes through an injected
  adapter, so the whole decision table is unit-testable.
* **The GUI installs nothing privileged.**  Failed checks carry a copyable
  command from SETUP.md; the operator runs package installs, group changes,
  `icloud-bridge-configure` and `setup-host.sh` themselves.
* **The share password is never handled *here*.**  The env file is parsed as
  text, not sourced as shell, by the shared :mod:`envfile` grammar; this module
  receives only an :class:`~.envfile.EnvReport` — key names and problems — so
  the value is never printed, logged, put in argv, copied into a resource
  bundle, or placed on the clipboard by anything in the first-run path.  D41
  narrows D31's original "never handled by the GUI at all": :mod:`guestprov` is
  the one module allowed to return that value, and only to stream it into the
  container over stdin at the moment the guest asks for it.  Carrying it in by
  hand remains the documented `provision/03-create-share.ps1` fallback.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable

from . import backup
# Re-exported deliberately: the env file's grammar is shared with `guestprov`
# and `host/icloud-bridge-configure` (D41), but the first-run assistant is still
# where the operator's file is chosen and reported on.
from .envfile import EnvReport, read_env_file, read_file_text
from .power import (CONTAINER_NAME, HELPER_PATH, RunResult, Runner, default_runner,
                    docker_env, docker_runner, streaming_runner)

# --------------------------------------------------------------- constants --

#: Where a package install puts the compose file, provisioning scripts and the
#: env example.
PACKAGE_BUNDLE = "/usr/share/icloud-bridge"
#: Where `install-gui.sh` copies the same material for a per-user install.
USER_BUNDLE = "~/.local/share/icloud-bridge-gui/resources"
#: Test/development override. Never a cwd fallback: a desktop launcher or an
#: autostart entry has no meaningful working directory.
BUNDLE_ENV = "ICLOUD_BRIDGE_RESOURCES"

#: Written by `install-gui.sh` beside the copied bundle so the assistant can
#: point at the checkout whose `host/setup-host.sh` matches this install.
CHECKOUT_MARKER = "source-checkout"

#: Fixed compose project name. The default would be the bundle directory's
#: basename, which differs between a package install, a per-user install and a
#: checkout — three different projects for one container. Pinning it means a
#: later terminal `docker compose -p icloud-bridge …` addresses the same one.
COMPOSE_PROJECT = "icloud-bridge"

DOCKER_TIMEOUT_SECONDS = 10
#: `compose up -d` pulls a ~2 GB image and creates a 100 GB+ qcow2; the download
#: of Windows itself happens inside the container afterwards.
COMPOSE_TIMEOUT_SECONDS = 1800

OK = "ok"
WARN = "warn"
FAIL = "fail"


# ----------------------------------------------------------------- results --

@dataclass(frozen=True)
class Check:
    """One readiness answer, ready to render."""
    key: str
    name: str
    status: str
    detail: str
    #: A command from SETUP.md the operator can copy, when there is a fix.
    command: str = ""

    @property
    def ok(self) -> bool:
        return self.status != FAIL


@dataclass(frozen=True)
class Bundle:
    """The resolved installation resources; paths are shown in the assistant."""
    root: str
    compose_file: str
    provision_dir: str
    env_example: str
    origin: str                       # override | source | user | package
    source_checkout: str = ""         # recorded by the per-user installer
    checkout_missing: bool = False    # …and no longer where it said


# ------------------------------------------------------ resource resolution --

def _readable_bundle(root: str, *, exists: Callable[[str], bool],
                     isdir: Callable[[str], bool]) -> tuple[str, str] | None:
    """``(compose_file, env_example)`` if ``root`` holds a complete bundle."""
    compose = os.path.join(root, "docker-compose.yml")
    provision = os.path.join(root, "provision")
    if not exists(compose) or not isdir(provision):
        return None
    # The source tree calls it `.env.example`; the installed bundles call it
    # `env.example` (a dotfile in /usr/share is needlessly invisible). Accept
    # either rather than making the two installs behave differently.
    for name in (".env.example", "env.example"):
        candidate = os.path.join(root, name)
        if exists(candidate):
            return compose, candidate
    return None


def _source_root() -> str:
    """The repository root when running from a checkout, else a dead path."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_bundle(*, environ: dict[str, str] | None = None,
                   exists: Callable[[str], bool] = os.path.exists,
                   isdir: Callable[[str], bool] = os.path.isdir,
                   read_text: Callable[[str], str] | None = None) -> Bundle | None:
    """Find the compose/provision/env-example bundle for *this* installation.

    Resolution is deterministic and never consults the working directory:
    the explicit override, then a source checkout relative to ``__file__``, then
    the per-user install, then the package. The chosen paths are shown in the
    assistant so there is no doubt which copy is in play.
    """
    environ = os.environ if environ is None else environ
    candidates = []
    override = environ.get(BUNDLE_ENV)
    if override:
        candidates.append(("override", override))
    candidates.append(("source", _source_root()))
    candidates.append(("user", os.path.expanduser(USER_BUNDLE)))
    candidates.append(("package", PACKAGE_BUNDLE))

    for origin, root in candidates:
        found = _readable_bundle(root, exists=exists, isdir=isdir)
        if found is None:
            continue
        compose, env_example = found
        checkout, missing = _read_checkout(root, exists=exists, read_text=read_text)
        if origin == "source":
            checkout, missing = root, False
        return Bundle(root=root, compose_file=compose,
                      provision_dir=os.path.join(root, "provision"),
                      env_example=env_example, origin=origin,
                      source_checkout=checkout, checkout_missing=missing)
    return None


def _read_checkout(root: str, *, exists: Callable[[str], bool],
                   read_text: Callable[[str], str] | None) -> tuple[str, bool]:
    """The checkout the per-user installer came from, and whether it moved."""
    marker = os.path.join(root, CHECKOUT_MARKER)
    if not exists(marker):
        return "", False
    try:
        recorded = (read_text or read_file_text)(marker).strip()
    except OSError:
        return "", False
    if not recorded:
        return "", False
    return recorded, not exists(os.path.join(recorded, "host", "setup-host.sh"))


# ------------------------------------------------------------- host checks --

def _run(runner: Runner, argv: list[str], timeout: float = DOCKER_TIMEOUT_SECONDS):
    try:
        return runner(argv, timeout)
    except FileNotFoundError:
        return RunResult(127, "", f"{argv[0]} is not installed or not on PATH")
    except TimeoutError:
        return RunResult(124, "", f"{argv[0]} timed out after {int(timeout)}s")
    except OSError as exc:                          # pragma: no cover - defensive
        return RunResult(1, "", f"{argv[0]} failed: {exc}")


def check_devices(exists: Callable[[str], bool] = os.path.exists,
                  access: Callable[[str, int], bool] = os.access) -> list[Check]:
    """`/dev/kvm` and `/dev/net/tun`, which the guest cannot run without."""
    checks = []
    if not exists("/dev/kvm"):
        checks.append(Check("kvm", "Hardware virtualization", FAIL,
                            "/dev/kvm does not exist — KVM is not available on this "
                            "host (check the BIOS virtualization setting, and that "
                            "you are not inside another VM)",
                            "sudo ./host/setup-prereqs.sh"))
    elif not access("/dev/kvm", os.R_OK | os.W_OK):
        checks.append(Check("kvm", "Hardware virtualization", FAIL,
                            "/dev/kvm exists but this account cannot use it",
                            "sudo usermod -aG kvm $USER   # then log out and back in"))
    else:
        checks.append(Check("kvm", "Hardware virtualization", OK, "/dev/kvm is usable"))

    if exists("/dev/net/tun"):
        checks.append(Check("tun", "Guest networking", OK, "/dev/net/tun is present"))
    else:
        checks.append(Check("tun", "Guest networking", FAIL,
                            "/dev/net/tun does not exist; the guest cannot get a network",
                            "sudo modprobe tun"))
    return checks


def check_docker(runner: Runner = docker_runner) -> list[Check]:
    """The native Engine, reached on its own socket, plus the Compose plugin."""
    checks = []
    server = _run(runner, ["docker", "version", "-f", "{{.Server.Version}}"])
    if server.returncode == 0 and server.stdout.strip():
        checks.append(Check("engine", "Docker Engine", OK,
                            f"native Engine {server.stdout.strip()} on "
                            "unix:///var/run/docker.sock"))
    else:
        detail = (server.stderr or server.stdout or "").strip()
        command = "sudo ./host/setup-prereqs.sh"
        if "permission denied" in detail.lower():
            # Group membership only takes effect in a *new* session; a fresh
            # `usermod` in the current one looks identical to not being a member.
            detail += " — a docker-group entry does not apply until you log out and back in"
            command = "sudo usermod -aG docker $USER   # then log out and back in"
        checks.append(Check("engine", "Docker Engine", FAIL,
                            detail or "the Docker daemon did not answer", command))
        return checks

    compose = _run(runner, ["docker", "compose", "version", "--short"])
    if compose.returncode == 0:
        checks.append(Check("compose", "Compose plugin", OK,
                            f"docker compose {compose.stdout.strip()}"))
    else:
        checks.append(Check("compose", "Compose plugin", FAIL,
                            (compose.stderr or "docker compose is unavailable").strip(),
                            "sudo apt install docker-compose-plugin"))

    context = _run(runner, ["docker", "context", "show"])
    active = context.stdout.strip()
    if active and active != "default":
        # Only a warning: every command this app runs is pinned to the native
        # socket, so the active context cannot misdirect them (item 3).
        checks.append(Check("context", "Docker context", WARN,
                            f"your active context is '{active}'; this app always uses "
                            "the native socket, but your own terminal commands will not",
                            "docker context use default"))
    return checks


def check_bundle(bundle: Bundle | None) -> list[Check]:
    checks = []
    if bundle is None:
        checks.append(Check("bundle", "Installation files", FAIL,
                            "could not find docker-compose.yml and provision/ in "
                            f"{PACKAGE_BUNDLE}, {USER_BUNDLE}, or a source checkout",
                            "./gui/install-gui.sh   # or install the .deb"))
        return checks
    checks.append(Check("bundle", "Installation files", OK,
                        f"{bundle.root} ({bundle.origin})"))
    if bundle.checkout_missing:
        checks.append(Check("checkout", "Source checkout", WARN,
                            f"this GUI was installed from {bundle.source_checkout}, "
                            "which no longer contains host/setup-host.sh — run the "
                            "host setup from wherever the repository is now",
                            "sudo ./host/setup-host.sh"))
    return checks


def check_env(report: EnvReport | None) -> list[Check]:
    if report is None:
        return [Check("env", "Configuration file", FAIL,
                      "choose the .env file that holds DISK_SIZE, RAM_SIZE, "
                      "CPU_CORES and SHARE_PASS",
                      "cp .env.example .env   # then edit it")]
    if report.ok:
        return [Check("env", "Configuration file", OK,
                      f"{report.path} ({len(report.keys)} settings)")]
    return [Check("env", "Configuration file", FAIL,
                  f"{report.path}: " + "; ".join(report.problems))]


def check_container(status_state: str) -> list[Check]:
    """The container's own state.  Absence is the expected pre-create state."""
    if status_state == "absent":
        return [Check("container", "Windows VM", OK,
                      f"no '{CONTAINER_NAME}' container yet — that is what "
                      "Create Windows VM is for")]
    if status_state == "running":
        return [Check("container", "Windows VM", OK,
                      f"'{CONTAINER_NAME}' already exists and is running")]
    if status_state == "stopped":
        return [Check("container", "Windows VM", OK,
                      f"'{CONTAINER_NAME}' already exists and is stopped")]
    return [Check("container", "Windows VM", FAIL,
                  "could not determine whether the container exists")]


def gather_checks(*, bundle: Bundle | None, env: EnvReport | None,
                  container_state: str,
                  runner: Runner = docker_runner,
                  exists: Callable[[str], bool] = os.path.exists,
                  access: Callable[[str, int], bool] = os.access) -> list[Check]:
    """Every pre-creation check, in the order the assistant shows them."""
    return [
        *check_devices(exists, access),
        *check_docker(runner),
        *check_bundle(bundle),
        *check_env(env),
        *check_container(container_state),
    ]


def can_create_vm(checks: list[Check], container_state: str) -> bool:
    """Whether **Create Windows VM** may be offered at all.

    Never when a container with the fixed name already exists, whatever its
    state, and never while any check is failing. Warnings do not block.
    """
    if container_state != "absent":
        return False
    return all(check.ok for check in checks)


# ------------------------------------------------------------ creating the VM --

def compose_argv(bundle: Bundle, env_file: str, *, action: str = "up") -> list[str]:
    """The exact argv used to create the VM — and documented for the operator.

    The project name is pinned so a later terminal command addresses the same
    project, and ``--env-file`` points at the operator's own file, which stays
    outside the resource bundle (it holds the share password).
    """
    argv = ["docker", "compose",
            "-p", COMPOSE_PROJECT,
            "-f", bundle.compose_file,
            "--env-file", env_file]
    if action == "up":
        argv += ["up", "-d"]
    else:
        argv.append(action)
    return argv


def create_vm(bundle: Bundle, env_file: str, *,
              runner: Runner | None = None,
              timeout: float = COMPOSE_TIMEOUT_SECONDS,
              on_line: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """Run ``docker compose up -d``; returns ``(ok, bounded diagnostic)``.

    ``on_line`` streams Compose's own output (v2 plan D38). Note what `up -d`
    covers: the image pull and container creation, **not** the 20-40 minute
    Windows installation that follows — that has no subprocess to stream at all.
    """
    if runner is None:
        runner = (docker_runner if on_line is None
                  else streaming_runner(on_line, env=docker_env()))
    result = _run(runner, compose_argv(bundle, env_file), timeout)
    output = "\n".join(part for part in
                       ((result.stdout or "").strip(), (result.stderr or "").strip())
                       if part)
    return result.returncode == 0, _tail(output)


def _tail(text: str, *, lines: int = 40, chars: int = 4000) -> str:
    """Bounded output: a wedged pull can otherwise produce megabytes."""
    trimmed = "\n".join(text.splitlines()[-lines:])
    if len(trimmed) > chars:
        trimmed = "…" + trimmed[-chars:]
    return trimmed


# --------------------------------------------- readiness for the power helper --

#: Both exact command specs the sudoers grant must carry; a bare path would
#: permit arbitrary arguments, so `icloud-bridge-configure` installs them
#: argument-exact and this checks for exactly that (v2 plan §5.1).
SUDO_SPECS = (f"{HELPER_PATH} on", f"{HELPER_PATH} off")

HOST_CONFIG = "/etc/icloud-bridge/config"
UNIT_PATH = "/etc/systemd/system/mnt-icloud.mount"


def check_host_setup(*, runner: Runner = default_runner,
                     exists: Callable[[str], bool] = os.path.exists) -> list[Check]:
    """Whether the privileged half is installed, without touching a mount.

    This is deliberately not a mountability test: the only honest one is the
    power helper's own CIFS activation, which runs next.
    """
    checks = []
    if exists(HELPER_PATH):
        checks.append(Check("helper", "Power helper", OK, f"{HELPER_PATH} is installed"))
    else:
        checks.append(Check("helper", "Power helper", FAIL,
                            f"{HELPER_PATH} is missing — the host half is not installed",
                            "sudo ./host/setup-host.sh"))

    if exists(UNIT_PATH):
        checks.append(Check("units", "Mount units", OK, "systemd units are installed"))
    else:
        checks.append(Check("units", "Mount units", FAIL,
                            f"{UNIT_PATH} is missing", "sudo ./host/setup-host.sh"))

    if exists(HOST_CONFIG):
        checks.append(Check("config", "Host configuration", OK,
                            f"{HOST_CONFIG} records this machine's settings"))
    else:
        checks.append(Check("config", "Host configuration", WARN,
                            f"{HOST_CONFIG} is absent; mount ownership and the "
                            "credentials file may not have been configured",
                            "sudo icloud-bridge-configure --env-file ./.env"))

    # `sudo -n -l <cmd> <arg>` answers without prompting: it prints the command
    # if this account may run it, and fails otherwise.
    for spec in SUDO_SPECS:
        argv = ["sudo", "-n", "-l", *spec.split()]
        result = _run(runner, argv)
        action = spec.rsplit(" ", 1)[-1]
        if result.returncode == 0:
            checks.append(Check(f"sudo-{action}", f"Permission to power {action}", OK,
                                "granted without a password"))
        else:
            checks.append(Check(f"sudo-{action}", f"Permission to power {action}", FAIL,
                                (result.stderr or "not permitted").strip(),
                                "sudo icloud-bridge-configure --user $USER"))
    return checks


# ------------------------------- the interrupted-provisioning record (D39) --
# The GUI can be closed, crash, or be logged out during the 20-40 minutes a
# Windows install takes. Without a record, the next launch sees a running
# container with no configuration and has to guess; D31's no-CIFS Provisioning
# state is exactly what must survive that gap.
#
# The record is private local state, never CIFS, and deliberately holds *no*
# env-file path and no env-file content: it must be safe to read and must never
# become a second place a share password can live.

PROVISIONING_RECORD = "provisioning.json"
PROVISIONING_VERSION = 1

#: Classifications of a record against what Docker currently reports.
RECORD_ABSENT = "absent"            # no record; ordinary startup rules apply
RECORD_MATCHES = "matches"          # same container: resume Provisioning Windows
RECORD_CONTAINER_GONE = "gone"      # the container is not there: back to Setup
RECORD_DIFFERENT = "different"      # a different container owns the name
RECORD_MALFORMED = "malformed"      # unreadable: report it, never silently drop


@dataclass(frozen=True)
class ProvisioningRecord:
    started_at: str
    phase: str = "creating"
    container_id: str = ""


def provisioning_path(base: str | None = None) -> str:
    return os.path.join(backup.app_dir(base), PROVISIONING_RECORD)


def write_provisioning_record(record: ProvisioningRecord,
                              base: str | None = None) -> None:
    """Record the intent **before** Compose runs, atomically and mode 0600."""
    backup.ensure_app_dir(base)
    path = provisioning_path(base)
    backup.check_destination(path)
    backup.write_json_atomic(path, {
        "version": PROVISIONING_VERSION,
        "startedAt": record.started_at,
        "phase": record.phase,
        "containerId": record.container_id,
    })


def read_provisioning_record(base: str | None = None) -> ProvisioningRecord | None:
    """The saved record, or ``None`` when there is none.

    Raises :class:`backup.BackupError` for a record that exists but cannot be
    trusted — a malformed record enters Setup with a diagnostic and is never
    silently deleted, nor treated as proof that a VM is configured.
    """
    path = provisioning_path(base)
    if os.path.islink(path):
        raise backup.BackupError(f"{path} is a symlink; refusing to read through it")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(64 * 1024)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise backup.BackupError(f"cannot read {path}: {exc}") from exc
    try:
        document = json.loads(raw.decode("utf-8-sig", errors="strict"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise backup.BackupError(f"{path} is not readable: {exc}") from exc
    if not isinstance(document, dict):
        raise backup.BackupError(f"{path} is not a JSON object")
    if document.get("version") != PROVISIONING_VERSION:
        raise backup.BackupError(f"{path} has an unsupported version")
    started = document.get("startedAt")
    if not isinstance(started, str) or not started:
        raise backup.BackupError(f'{path} has no usable "startedAt"')
    phase = document.get("phase")
    container_id = document.get("containerId")
    return ProvisioningRecord(
        started_at=started,
        phase=phase if isinstance(phase, str) and phase else "creating",
        container_id=container_id if isinstance(container_id, str) else "")


def clear_provisioning_record(base: str | None = None) -> None:
    """Remove the record. Only ever after a successful connect, or a confirmed
    discard — never as a side effect of failing to understand it."""
    try:
        os.unlink(provisioning_path(base))
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise backup.BackupError(f"cannot remove the record: {exc}") from exc


def classify_record(record: ProvisioningRecord | None, container_state: str,
                    container_id: str) -> str:
    """Match a record against what Docker reports right now.

    A pre-Compose record has no container id yet, so the fixed container *name*
    existing is the only evidence available and is accepted. Once the id is
    recorded, it must match: a different container under the same name is a
    stale record, and nothing may be read from its mounts.
    """
    if record is None:
        return RECORD_ABSENT
    if container_state == "absent":
        return RECORD_CONTAINER_GONE
    if not record.container_id:
        return RECORD_MATCHES
    if container_id and container_id != record.container_id:
        return RECORD_DIFFERENT
    return RECORD_MATCHES


def inspect_container_id(runner: Runner = docker_runner,
                         *, name: str = CONTAINER_NAME) -> str:
    """The container's full id, or an empty string when it cannot be read."""
    result = _run(runner, ["docker", "inspect", "-f", "{{.Id}}", name])
    return (result.stdout or "").strip() if result.returncode == 0 else ""
