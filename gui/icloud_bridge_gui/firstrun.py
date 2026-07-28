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
* **The durable record is not only about the first run.**  D43 widens D39's
  interrupted-provisioning record to cover an explicit re-provisioning too, and
  adds what reattachment needs: the mode, the container's `State.StartedAt`
  token, the guest run ID, the last guest phase, and the non-secret
  `resetShareCredential` intent.  :func:`classify_resume` is the whole decision
  table for the next launch and is pure, so it can be proved without Docker.
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
import re
import secrets
import stat
from dataclasses import dataclass, replace
from typing import Callable

from . import backup, guestprov
# Re-exported deliberately: the env file's grammar is shared with `guestprov`
# and `host/icloud-bridge-configure` (D41), but the first-run assistant is still
# where the operator's file is chosen and reported on.
from .envfile import EnvReport, read_env_file, read_file_text
# One spelling of the two run modes, shared with the reducer that branches on
# them (D43). `lifecycle` imports nothing, so this direction has no cycle.
from .lifecycle import MODE_FIRST_RUN, MODE_REPROVISION, PROVISIONING_MODES
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

CONFIG_DIR_NAME = "icloud-bridge"
CONFIG_FILE_NAME = "env"
CONFIG_DIR_MODE = 0o700
CONFIG_FILE_MODE = 0o600
MIN_DISK_GB = 120
MIN_RAM_GB = 3
MIN_CPU_CORES = 2


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


@dataclass(frozen=True)
class ResourceDefaults:
    """Editable, conservative VM resources derived from this host."""
    disk_size: str
    ram_size: str
    cpu_cores: str


def configuration_path(*, environ: dict[str, str] | None = None) -> str:
    """The one conventional env file location, resolved through XDG."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, CONFIG_DIR_NAME, CONFIG_FILE_NAME)


def resource_defaults(*, cpu_count: int | None = None,
                      available_memory_bytes: int | None = None,
                      available_disk_bytes: int | None = None) -> ResourceDefaults:
    """Return floor-clamped VM settings without imposing a second env grammar."""
    if cpu_count is None:
        cpu_count = os.cpu_count()
    cores = max(MIN_CPU_CORES, (cpu_count or MIN_CPU_CORES) // 2)
    if available_memory_bytes is None:
        try:
            available_memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
        except (AttributeError, OSError, ValueError):
            available_memory_bytes = 0
    ram_gb = max(MIN_RAM_GB, min(8, available_memory_bytes // (1024 ** 3) // 2))
    if available_disk_bytes is None:
        try:
            available_disk_bytes = os.statvfs(os.path.expanduser("~")).f_bavail * os.statvfs(
                os.path.expanduser("~")).f_frsize
        except OSError:
            available_disk_bytes = 0
    free_gb = available_disk_bytes // (1024 ** 3)
    disk_gb = max(MIN_DISK_GB, ((free_gb // 2 + 19) // 20) * 20)
    return ResourceDefaults(f"{disk_gb}G", f"{ram_gb}G", str(cores))


#: The values come from free-text fields, and anything written next to the
#: generated secret must be inert in the env-file grammar: one token, no
#: whitespace, no chance of smuggling another KEY=VALUE line.
_SIZE_PATTERN = re.compile(r"^[0-9]+[GM]$")
_CORES_PATTERN = re.compile(r"^[0-9]+$")


def create_configuration(disk_size: str, ram_size: str, cpu_cores: str, *,
                         path: str | None = None) -> str:
    """Atomically create the conventional 0600 env file, never overwriting it."""
    disk_size, ram_size = disk_size.strip(), ram_size.strip()
    cpu_cores = cpu_cores.strip()
    if not _SIZE_PATTERN.match(disk_size):
        raise ValueError('DISK_SIZE must be a number followed by G or M, like "120G"')
    if not _SIZE_PATTERN.match(ram_size):
        raise ValueError('RAM_SIZE must be a number followed by G or M, like "3G"')
    if not _CORES_PATTERN.match(cpu_cores) or int(cpu_cores) < 1:
        raise ValueError('CPU_CORES must be a whole number of cores, like "2"')
    path = configuration_path() if path is None else path
    directory = os.path.dirname(path)
    if os.path.islink(directory):
        raise OSError("configuration directory is a symlink")
    os.makedirs(directory, mode=CONFIG_DIR_MODE, exist_ok=True)
    if not os.path.isdir(directory) or os.path.islink(directory):
        raise OSError("configuration directory is not a regular directory")
    os.chmod(directory, CONFIG_DIR_MODE)
    if os.path.exists(path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or os.path.islink(path):
            raise OSError("configuration file is not a regular file")
        return path
    share_pass = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz"
                                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
                         for _ in range(32))
    content = (f"DISK_SIZE={disk_size}\nRAM_SIZE={ram_size}\n"
               f"CPU_CORES={cpu_cores}\nSHARE_PASS={share_pass}\n")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, CONFIG_FILE_MODE)
    except FileExistsError:
        return path
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.chmod(path, CONFIG_FILE_MODE)
    return path


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
                      "create configuration in the app, or choose an existing .env "
                      "file holding DISK_SIZE, RAM_SIZE, CPU_CORES and SHARE_PASS",
                      "Use Create configuration in the app; manual fallback: cp .env.example .env")]
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


# --------------------------- the durable provisioning record (D39, D43) --
# The GUI can be closed, crash, or be logged out during the 20-40 minutes a
# Windows install takes, and again during the second window in which elevated
# guest scripts rewrite the share, its ACLs and the agent task. Without a
# record, the next launch sees a running container with no configuration and has
# to guess; D31's no-CIFS Provisioning state is exactly what must survive both
# gaps, so the same record covers a `first-run` and a `reprovision` alike.
#
# The record is private local state, never CIFS, and deliberately holds *no*
# env-file path and no env-file content: it must be safe to read and must never
# become a second place a share password can live. That exclusion is why
# re-selection — not recovery — is the restart path for a pending secret (D41).

PROVISIONING_RECORD = "provisioning.json"
#: Bumped by D43's added fields. There is no migration and no tolerated older
#: form (`CONTRIBUTING.md`): a version-1 document on disk reads as malformed,
#: which enters Setup with a diagnostic and deletes nothing.
PROVISIONING_VERSION = 2

#: Classifications of a record against what Docker currently reports.
RECORD_ABSENT = "absent"            # no record; ordinary startup rules apply
RECORD_MATCHES = "matches"          # same container: resume Provisioning Windows
RECORD_CONTAINER_GONE = "gone"      # the container is not there: back to Setup
RECORD_DIFFERENT = "different"      # a different container owns the name
RECORD_MALFORMED = "malformed"      # unreadable: report it, never silently drop

#: How far the **host** got, which is a different question from the guest phase
#: the watcher publishes in ``guest_phase`` — the two vocabularies are separate,
#: and `staging` existing in both is a coincidence of English, not one state.
#: This one is load-bearing: host `staging` means the trigger's atomic rename
#: may not have happened, so the saved run ID may never have reached the guest.
RECORD_PHASE_CREATING = "creating"          # `docker compose up -d` is running
RECORD_PHASE_PROVISIONING = "provisioning"  # the VM exists; the guest work is next
RECORD_PHASE_STAGING = "staging"            # ensure_channel/stage in flight
RECORD_PHASE_TRIGGERED = "triggered"        # the trigger landed; poll for the run
RECORD_PHASES = (RECORD_PHASE_CREATING, RECORD_PHASE_PROVISIONING,
                 RECORD_PHASE_STAGING, RECORD_PHASE_TRIGGERED)


@dataclass(frozen=True)
class ProvisioningRecord:
    """What must survive a crash to reattach to a run instead of guessing.

    ``guest_run_id`` is what makes reattachment a *match*; ``container_started_at``
    is Docker's own `State.StartedAt` token, which separates "the container
    restarted" from "the watcher is merely quiet" — two conditions whose correct
    responses are opposite (D43).
    """

    started_at: str
    phase: str = RECORD_PHASE_CREATING
    container_id: str = ""
    mode: str = MODE_FIRST_RUN
    container_started_at: str = ""
    guest_run_id: str = ""
    #: The last guest phase parsed from a status carrying this run ID. Never a
    #: phase read from a status belonging to some other run.
    guest_phase: str = ""
    #: The non-secret intent staged in the trigger. The value it eventually
    #: delivers is not here and never will be.
    reset_share_credential: bool = False


def provisioning_path(base: str | None = None) -> str:
    return os.path.join(backup.app_dir(base), PROVISIONING_RECORD)


def write_provisioning_record(record: ProvisioningRecord,
                              base: str | None = None) -> None:
    """Record the intent **before** the mutating step, atomically and mode 0600.

    "Before" means before Compose creates the VM, and before the trigger that
    authorizes a guest run: there must be no window in which a trigger exists
    with no record naming its run.

    The mode and run ID are validated here rather than at construction so every
    writer is covered by one check. A malformed run ID is a programming error —
    it is a path component in the guest — and raises rather than being sanitized.
    """
    if record.mode not in PROVISIONING_MODES:
        raise ValueError(f"unknown provisioning mode {record.mode!r}")
    if record.phase not in RECORD_PHASES:
        raise ValueError(f"unknown record phase {record.phase!r}")
    if record.guest_run_id:
        guestprov.validate_run_id(record.guest_run_id)
    backup.ensure_app_dir(base)
    path = provisioning_path(base)
    backup.check_destination(path)
    backup.write_json_atomic(path, {
        "version": PROVISIONING_VERSION,
        "mode": record.mode,
        "startedAt": record.started_at,
        "phase": record.phase,
        "containerId": record.container_id,
        "containerStartedAt": record.container_started_at,
        "guestRunId": record.guest_run_id,
        "guestPhase": record.guest_phase,
        "resetShareCredential": record.reset_share_credential,
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
    mode = document.get("mode")
    if mode not in PROVISIONING_MODES:
        raise backup.BackupError(f'{path} has no usable "mode"')
    phase = document.get("phase")
    if phase not in RECORD_PHASES:
        raise backup.BackupError(f'{path} has no usable "phase"')
    run_id = document.get("guestRunId")
    if not isinstance(run_id, str):
        raise backup.BackupError(f'{path} has an unusable "guestRunId"')
    if run_id:
        try:
            guestprov.validate_run_id(run_id)
        except ValueError as exc:
            raise backup.BackupError(f'{path} has an unusable "guestRunId": {exc}') \
                from exc
    reset = document.get("resetShareCredential")
    # A JSON `1` is not the same document as a JSON `true`, and this boolean
    # decides whether a run resets a working share password.
    if not isinstance(reset, bool):
        raise backup.BackupError(f'{path} has an unusable "resetShareCredential"')
    container_id = document.get("containerId")
    started_token = document.get("containerStartedAt")
    guest_phase = document.get("guestPhase")
    return ProvisioningRecord(
        started_at=started,
        phase=phase,
        container_id=container_id if isinstance(container_id, str) else "",
        mode=mode,
        container_started_at=(started_token if isinstance(started_token, str) else ""),
        guest_run_id=run_id,
        guest_phase=guest_phase if isinstance(guest_phase, str) else "",
        reset_share_credential=reset)


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

    A `reprovision` record is classified exactly like a `first-run` one, which
    is what subjects it to the same startup-before-CIFS gate (D43): otherwise a
    restarted app would mount while scripts 03/04 are changing guest state.
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


def inspect_container_started_at(runner: Runner = docker_runner,
                                 *, name: str = CONTAINER_NAME) -> str:
    """Docker's ``State.StartedAt`` token, or ``""`` when it cannot be read.

    Opaque on purpose: it is compared for equality with the saved copy and never
    parsed as a time. Any restart gives a new value, which is the whole signal —
    the inbox, the `[Provision]` stanza dockur regenerates at container start,
    and anything else living in the container's `/run` do not survive one.
    """
    result = _run(runner, ["docker", "inspect", "-f", "{{.State.StartedAt}}", name])
    return (result.stdout or "").strip() if result.returncode == 0 else ""


# ------------------------------- resuming a guest provisioning run (D43) --

#: What a saved run needs on the next launch. Every one of these is a no-CIFS
#: outcome: they choose between polling, re-staging and asking, never between
#: mounting and not mounting.
RESUME_NO_RUN = "no-run"            # the record names no guest run
RESUME_POLL = "poll"                # reattach to the saved run and keep polling
RESUME_NEEDS_SECRET = "needs-secret"  # poll, and ask for the env file again
RESUME_RETRY_STAGE = "retry-stage"  # re-stage the *same* run ID; nothing consumed it
RESUME_CONFIRM_NEW_RUN = "confirm-new-run"  # restarted: offer a confirmed new run
RESUME_POLL_OR_ABANDON = "poll-or-abandon"  # keep polling; abandoning is explicit
RESUME_DONE = "done"                # the saved run finished
RESUME_FAILED = "failed"            # the saved run published an error


def container_restarted(record: ProvisioningRecord, container_started_at: str) -> bool:
    """Whether Docker says the container restarted since the record was written.

    Only a *known* difference counts. An unrecorded or unreadable token is not
    evidence of a restart, and answering "yes" on missing evidence is what would
    let the app abandon a live run without asking (D43).
    """
    return bool(record.container_started_at) and bool(container_started_at) \
        and container_started_at != record.container_started_at


def classify_resume(record: ProvisioningRecord | None,
                    status: guestprov.Status | None, *,
                    container_started_at: str = "") -> str:
    """What to do with a saved run, given one poll of the guest's status (D43).

    Pure, so the whole branch table is testable without Docker: the controller
    supplies the classified :class:`guestprov.Status` — already run-ID matched
    by :func:`guestprov.poll` — and the container's current start token.

    The ordering encodes the rules that must not be improvised around:

    * an *acknowledged* status for this run is believed over any restart
      evidence, because the watcher records the accepted run locally and
      survives a reboot on the same run;
    * an unacknowledged run still in host `staging` is re-staged under the
      **same** run ID. It cannot have been consumed — nothing acknowledged it —
      so this needs no confirmation and covers a crash between the record write
      and the trigger's atomic rename;
    * only then does a changed start token turn "no status" into an offer of a
      confirmed new run, rather than adopting an uncorrelated one;
    * with no restart evidence, a missing status keeps polling. Abandoning a
      possibly live run is an explicit confirmation, never a default.
    """
    if record is None or not record.guest_run_id:
        return RESUME_NO_RUN
    if status is not None and status.acknowledged:
        if status.failed:
            return RESUME_FAILED
        if status.phase == guestprov.PHASE_DONE:
            return RESUME_DONE
        if status.phase == guestprov.PHASE_WAITING_FOR_SECRET:
            # The env-file path is deliberately not in the record, so the
            # operator selects it again; the secret itself is re-deliverable.
            return RESUME_NEEDS_SECRET
        return RESUME_POLL
    if record.phase == RECORD_PHASE_STAGING:
        return RESUME_RETRY_STAGE
    if container_restarted(record, container_started_at):
        return RESUME_CONFIRM_NEW_RUN
    return RESUME_POLL_OR_ABANDON


def record_after_status(record: ProvisioningRecord,
                        status: guestprov.Status) -> ProvisioningRecord:
    """The record updated from a status — only when the status is *ours*.

    D43 updates the record after a matching status is parsed, and only then: a
    mismatched, stale, absent or malformed status never replaces the saved run
    ID, never rewrites the phase, and never clears anything.
    """
    if not status.acknowledged or status.run_id != record.guest_run_id:
        return record
    # An acknowledgement is proof the trigger landed, whatever the host thought
    # it was still doing.
    return replace(record, phase=RECORD_PHASE_TRIGGERED, guest_phase=status.phase)
