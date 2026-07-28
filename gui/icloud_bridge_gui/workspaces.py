"""Safe Workspaces configuration, path rules, and state layout (D52).

A Safe Workspace pairs a directory under the iCloud CIFS mount with an ordinary
directory on the host's own disk, so an editor never opens a file whose metadata
a cloud filter rewrites out of band (`docs/plan-safe-local-workspaces.md` §2).

This module is the model half of the feature and nothing else:

* **Qt-free, mount-free, subprocess-free.** It touches local disk only — the XDG
  config and state directories plus `/proc/self/mountinfo`. It never reads
  `/mnt/icloud`, never runs Unison, and never runs anything at all.
  `workspace_sync.py` owns every one of those.
* **Validation is separable from creation** (plan §4.3). :func:`validate_fields`
  applies every rule that needs no filesystem access, so the GUI can run it
  synchronously while the operator types; :func:`check_local_state` and
  :func:`check_local_occupancy` stat the filesystem from a worker; and only
  :func:`create_local_root` creates anything.
* **Fail closed.** A missing configuration means "no workspaces". A malformed,
  oversized, or unsupported-version one is an error that stops every cycle and
  is never rewritten or silently repaired — there is one schema version and no
  tolerant reader (`CONTRIBUTING.md`, pre-release policy).
* **Refuse rather than follow.** Symlinked directories, symlinked destinations,
  and non-regular files are refused everywhere, exactly as `backup.py` refuses
  them; its 0700-directory/atomic-0600-file helpers are reused here rather than
  reimplemented, as `firstrun.py` already reuses them.

Remote paths are compared case-insensitively because the endpoint is Windows;
local paths are compared exactly because the endpoint is Linux.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import unicodedata
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from . import backup, bridge

CONFIG_VERSION = 1

#: The same per-application directory name under both XDG bases.
APP_DIR_NAME = backup.APP_DIR_NAME
CONFIG_NAME = "workspaces.json"

#: Per-workspace state lives in `<XDG_STATE_HOME>/<app>/workspaces/<id>/`.
STATE_SUBDIR = "workspaces"
UNISON_SUBDIR = "unison"
BACKUPS_SUBDIR = "backups"
SNAPSHOT_NAME = "snapshot.json"
BASELINE_NAME = "baseline.json"
STATUS_NAME = "status.json"
LOG_NAME = "sync.log"
LOCK_NAME = "lock"

DIR_MODE = 0o700
FILE_MODE = 0o600

#: The local replica is created private regardless of the process umask (§7.5).
LOCAL_ROOT_MODE = 0o700

#: Bounds (§4.1, §5.1). The file is local and hand-editable, so read it warily.
MAX_CONFIG_BYTES = 1024 * 1024
MAX_WORKSPACES = 32
MAX_NAME_CHARS = 80
MAX_REMOTE_CHARS = 1024
MAX_SEGMENT_BYTES = 255
MAX_LOCAL_CHARS = 4096

#: Local disks only. A replica on a network, FUSE, overlay or memory-backed
#: filesystem would reintroduce exactly what the feature exists to escape.
ALLOWED_FILESYSTEMS = ("ext4", "xfs", "btrfs", "bcachefs", "zfs")

MOUNTINFO_PATH = "/proc/self/mountinfo"

#: :func:`save` outcomes.
SAVED = "saved"
UNCHANGED = "unchanged"

_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_WORKSPACE_FIELDS = frozenset({"id", "name", "remote", "local", "enabled"})
_DOCUMENT_FIELDS = frozenset({"version", "workspaces"})


class WorkspaceError(Exception):
    """A workspace configuration, path, or filesystem rule was violated."""


@dataclass(frozen=True)
class Workspace:
    """One configured pairing. Every field is already normalized."""

    id: str
    name: str
    remote: str
    local: str
    enabled: bool = True

    @property
    def remote_key(self) -> str:
        """The case-insensitive comparison form of :attr:`remote`."""
        return remote_key(self.remote)

    def as_document(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "remote": self.remote,
            "local": self.local,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class WorkspaceConfig:
    """A validated version-1 `workspaces.json`, in the file's own order."""

    workspaces: tuple[Workspace, ...] = ()

    def as_document(self) -> dict:
        return {
            "version": CONFIG_VERSION,
            "workspaces": [item.as_document() for item in self.workspaces],
        }

    def get(self, workspace_id: str) -> Workspace | None:
        for item in self.workspaces:
            if item.id == workspace_id:
                return item
        return None

    def scheduled(self) -> tuple[Workspace, ...]:
        """The enabled workspaces, in the deterministic id order of §10."""
        return tuple(sorted((w for w in self.workspaces if w.enabled),
                            key=lambda w: w.id))


# ------------------------------------------------------------------- paths --

def home_dir(environ: Mapping[str, str] | None = None) -> str:
    """The desktop user's home, ignoring a relative ``HOME``."""
    env = os.environ if environ is None else environ
    value = env.get("HOME") or ""
    if not os.path.isabs(value):
        return os.path.normpath(os.path.expanduser("~"))
    return os.path.normpath(value)


def config_base(environ: Mapping[str, str] | None = None) -> str:
    """``$XDG_CONFIG_HOME``, or the spec's default when unset or relative."""
    env = os.environ if environ is None else environ
    value = env.get("XDG_CONFIG_HOME") or ""
    if not os.path.isabs(value):
        return os.path.join(home_dir(environ), ".config")
    return value


def state_base(environ: Mapping[str, str] | None = None) -> str:
    """``$XDG_STATE_HOME``, with the same defensive rule as :mod:`backup`.

    Delegated rather than duplicated so one place in the tree decides what a
    relative or absent XDG base means.
    """
    return backup.state_base(environ)


def mount_root(environ: Mapping[str, str] | None = None) -> str:
    """Where the iCloud data share is mounted (`bridge.mount_dir`'s rule)."""
    if environ is None:
        return bridge.mount_dir()
    return environ.get("ICLOUD_MOUNT_DIR") or bridge.DEFAULT_MOUNT_DIR


def bridge_share_root(environ: Mapping[str, str] | None = None) -> str:
    """Where the guest's control share is mounted (`bridge.bridge_dir`'s rule)."""
    if environ is None:
        return bridge.bridge_dir()
    return environ.get("ICLOUD_BRIDGE_DIR") or bridge.DEFAULT_BRIDGE_DIR


def reserved_roots(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Locations a local root may neither sit inside nor contain (§5.2)."""
    roots = (mount_root(environ), bridge_share_root(environ),
             state_base(environ), config_base(environ))
    return tuple(dict.fromkeys(os.path.normpath(root) for root in roots))


def config_dir(base: str | None = None) -> str:
    """The application directory under ``base`` (an XDG *config* base)."""
    return os.path.join(config_base() if base is None else base, APP_DIR_NAME)


def config_path(base: str | None = None) -> str:
    return os.path.join(config_dir(base), CONFIG_NAME)


def state_root(base: str | None = None) -> str:
    """Where every workspace's private state lives (``base`` is a state base)."""
    return os.path.join(state_base() if base is None else base, APP_DIR_NAME,
                        STATE_SUBDIR)


def state_dir(workspace_id: str, base: str | None = None) -> str:
    """One workspace's state directory. The id is re-validated as a guard.

    Every other state path is built from this one, so validating here is what
    stops a hand-edited id from addressing a directory outside the state root.
    """
    return os.path.join(state_root(base), validate_id(workspace_id))


def unison_dir(workspace_id: str, base: str | None = None) -> str:
    """The workspace-private ``UNISON`` archive/profile directory (§7.7)."""
    return os.path.join(state_dir(workspace_id, base), UNISON_SUBDIR)


def backups_dir(workspace_id: str, base: str | None = None) -> str:
    """The central backup directory Unison is pointed at (§8)."""
    return os.path.join(state_dir(workspace_id, base), BACKUPS_SUBDIR)


def snapshot_path(workspace_id: str, base: str | None = None) -> str:
    """The persisted snapshot pair used by the stability rule (§7.3)."""
    return os.path.join(state_dir(workspace_id, base), SNAPSHOT_NAME)


def baseline_path(workspace_id: str, base: str | None = None) -> str:
    """The last successful cycle's path sets, for the guard (§7.6)."""
    return os.path.join(state_dir(workspace_id, base), BASELINE_NAME)


def status_path(workspace_id: str, base: str | None = None) -> str:
    """The per-workspace status document (§7.9)."""
    return os.path.join(state_dir(workspace_id, base), STATUS_NAME)


def log_path(workspace_id: str, base: str | None = None) -> str:
    """Unison's own log for this workspace (§7.9)."""
    return os.path.join(state_dir(workspace_id, base), LOG_NAME)


def lock_path(workspace_id: str, base: str | None = None) -> str:
    """The single-flight ``flock`` file taken before any mount access (§6)."""
    return os.path.join(state_dir(workspace_id, base), LOCK_NAME)


def _ensure_directory(path: str) -> str:
    """Create or tighten one directory to 0700, refusing a surprise.

    `backup.ensure_app_dir`'s rule, generalized to an arbitrary path: a symlink
    here would let anything the desktop user can write to redirect files we are
    about to create 0600 inside it.
    """
    if os.path.islink(path):
        raise WorkspaceError(f"{path} is a symlink; refusing to use it")
    if os.path.exists(path) and not os.path.isdir(path):
        raise WorkspaceError(f"{path} exists and is not a directory")
    try:
        os.makedirs(path, mode=DIR_MODE, exist_ok=True)
        os.chmod(path, DIR_MODE)
    except OSError as exc:
        raise WorkspaceError(f"cannot prepare {path}: {exc}") from exc
    return path


def ensure_config_dir(base: str | None = None) -> str:
    """Create or tighten the application's XDG config directory."""
    return _ensure_directory(config_dir(base))


def ensure_state_dir(workspace_id: str, base: str | None = None) -> str:
    """Create one workspace's state layout, every level 0700.

    Each level is created explicitly rather than through one ``makedirs`` so the
    intermediate directories get 0700 too, not the process umask.
    """
    _ensure_directory(os.path.join(state_base() if base is None else base,
                                   APP_DIR_NAME))
    _ensure_directory(state_root(base))
    path = _ensure_directory(state_dir(workspace_id, base))
    _ensure_directory(unison_dir(workspace_id, base))
    _ensure_directory(backups_dir(workspace_id, base))
    return path


# --------------------------------------------------------------- path rules --

def _is_within(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` itself or lives under it."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def new_id() -> str:
    """A fresh workspace id: 16 lowercase hexadecimal characters (§4.2)."""
    return secrets.token_hex(8)


def validate_id(value: object) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise WorkspaceError(
            "a workspace id must be exactly 16 lowercase hexadecimal characters")
    return value


def normalize_name(name: object) -> str:
    """The display name: stripped, bounded, and free of control characters."""
    if not isinstance(name, str):
        raise WorkspaceError("the workspace name is not a string")
    text = name.strip()
    if not text:
        raise WorkspaceError("the workspace needs a name")
    if len(text) > MAX_NAME_CHARS:
        raise WorkspaceError(
            f"the workspace name is longer than {MAX_NAME_CHARS} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise WorkspaceError(
            "the workspace name contains a control character or a line break")
    return text


def normalize_remote(remote: object) -> str:
    """One mount-relative directory path, normalized per §5.1, or raise.

    Storage keeps the operator's casing; :func:`remote_key` is the comparison
    form. Nothing here touches the mount — this is string work only.
    """
    if not isinstance(remote, str):
        raise WorkspaceError("the iCloud folder is not a string")
    if len(remote) > MAX_REMOTE_CHARS:
        raise WorkspaceError(
            f"the iCloud folder is longer than {MAX_REMOTE_CHARS} characters")
    if "\x00" in remote:
        raise WorkspaceError("the iCloud folder contains a NUL byte")
    if "\\" in remote:
        raise WorkspaceError(
            "the iCloud folder must use forward slashes, not backslashes")
    if any(unicodedata.category(ch) == "Cc" for ch in remote):
        raise WorkspaceError("the iCloud folder contains a control character")
    if remote.startswith("/"):
        # An absolute-looking value means the operator pasted a host path;
        # stripping the root would silently rename the folder they asked for.
        raise WorkspaceError(
            "the iCloud folder is relative to the iCloud mount, so it cannot "
            "start with /")
    text = re.sub(r"/+", "/", unicodedata.normalize("NFC", remote)).strip("/")
    if not text:
        raise WorkspaceError(
            "the iCloud folder cannot be empty or the whole sync root")
    for segment in text.split("/"):
        if not segment:
            raise WorkspaceError("the iCloud folder has an empty path segment")
        if segment in (".", ".."):
            raise WorkspaceError(
                'the iCloud folder cannot contain a "." or ".." segment')
        if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
            raise WorkspaceError(
                f"an iCloud folder name exceeds {MAX_SEGMENT_BYTES} bytes")
    return text


def remote_key(remote: str) -> str:
    """The comparison form: NFC segments, case-folded (the endpoint is Windows)."""
    normalized = unicodedata.normalize("NFC", remote)
    return "/".join(segment.casefold() for segment in normalized.split("/"))


def remote_root(remote: str, environ: Mapping[str, str] | None = None) -> str:
    """The absolute path of a workspace's remote root under the mount.

    Returning it is *not* a promise that it exists; a cycle checks that, and
    never creates one (§6).
    """
    normalized = normalize_remote(remote)
    mount = os.path.normpath(mount_root(environ))
    root = os.path.normpath(os.path.join(mount, normalized))
    if root == mount or not _is_within(root, mount):
        raise WorkspaceError(
            f"the iCloud folder {remote!r} resolves outside {mount}")
    return root


def normalize_local(local: object,
                    environ: Mapping[str, str] | None = None) -> str:
    """One absolute local root, normalized per §5.2, or raise.

    Symlinks are deliberately *not* resolved: the stored value is what the
    operator chose, and :func:`check_local_state` refuses a symlink outright
    rather than quietly following it somewhere else.
    """
    if not isinstance(local, str):
        raise WorkspaceError("the local folder is not a string")
    if "\x00" in local:
        raise WorkspaceError("the local folder contains a NUL byte")
    if len(local) > MAX_LOCAL_CHARS:
        raise WorkspaceError(
            f"the local folder is longer than {MAX_LOCAL_CHARS} characters")
    if not local.strip():
        raise WorkspaceError("the workspace needs a local folder")
    if not os.path.isabs(local):
        raise WorkspaceError("the local folder must be an absolute path")
    path = os.path.normpath(local)
    if path in ("/", "/home"):
        raise WorkspaceError(f"{path} cannot be a workspace folder")
    if path == home_dir(environ):
        raise WorkspaceError(
            "the home directory itself cannot be a workspace folder")
    for root in reserved_roots(environ):
        if _is_within(path, root):
            raise WorkspaceError(f"a workspace folder cannot be inside {root}")
        if _is_within(root, path):
            raise WorkspaceError(f"a workspace folder cannot contain {root}")
    return path


def check_collisions(workspaces: Sequence[Workspace]) -> None:
    """Reject duplicate ids and duplicate or overlapping roots (§4.2)."""
    if len(workspaces) > MAX_WORKSPACES:
        raise WorkspaceError(f"more than {MAX_WORKSPACES} workspaces")
    seen: set[str] = set()
    for item in workspaces:
        if item.id in seen:
            raise WorkspaceError(f"two workspaces share the id {item.id}")
        seen.add(item.id)
    for index, first in enumerate(workspaces):
        for second in workspaces[index + 1:]:
            if (_is_within(first.local, second.local)
                    or _is_within(second.local, first.local)):
                raise WorkspaceError(
                    "two workspaces would use overlapping local folders: "
                    f"{first.local} and {second.local}")
            if (_is_within(first.remote_key, second.remote_key)
                    or _is_within(second.remote_key, first.remote_key)):
                raise WorkspaceError(
                    "two workspaces would use overlapping iCloud folders: "
                    f"{first.remote} and {second.remote}")


# --------------------------------------------------- validation, in stages --

def validate_fields(name: object, remote: object, local: object, *,
                    workspace_id: str | None = None, enabled: bool = True,
                    existing: Sequence[Workspace] = (),
                    environ: Mapping[str, str] | None = None) -> Workspace:
    """Every rule that needs no filesystem access (§4.3, first kind).

    Side-effect-free and cheap enough for the GUI to run on every keystroke.
    ``existing`` is the already-configured set the candidate must not collide
    with. Returns the normalized candidate; raises :class:`WorkspaceError` with
    a specific message otherwise.
    """
    identifier = new_id() if workspace_id is None else validate_id(workspace_id)
    candidate = Workspace(
        id=identifier,
        name=normalize_name(name),
        remote=normalize_remote(remote),
        local=normalize_local(local, environ),
        enabled=bool(enabled),
    )
    remote_root(candidate.remote, environ)
    check_collisions([*existing, candidate])
    return candidate


def check_local_directory(local: str) -> None:
    """Refuse a symlinked local root, a symlinked ancestor, or a non-directory."""
    path = os.path.normpath(local)
    parts = path.split("/")
    for depth in range(1, len(parts)):
        ancestor = "/".join(parts[:depth]) or "/"
        if os.path.islink(ancestor):
            raise WorkspaceError(
                f"{ancestor} is a symlink; refusing to use a workspace folder "
                "underneath it")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise WorkspaceError(f"{path} is a symlink; refusing to use it")
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(f"{path} exists and is not a directory")


def check_local_occupancy(local: str) -> None:
    """§5.3: a new workspace may target only a nonexistent or empty directory.

    The remote half of the first-run rule needs the mount and belongs to
    `workspace_sync`; this is the local half, which the add dialog's worker can
    answer without touching CIFS at all.
    """
    try:
        entries = os.listdir(local)
    except FileNotFoundError:
        return
    except NotADirectoryError as exc:
        raise WorkspaceError(f"{local} is not a directory") from exc
    except OSError as exc:
        raise WorkspaceError(f"cannot inspect {local}: {exc}") from exc
    if entries:
        raise WorkspaceError(
            f"{local} already contains {len(entries)} item(s); a new workspace "
            "needs an empty or nonexistent folder")


def check_local_state(local: str, *,
                      mountinfo_path: str = MOUNTINFO_PATH) -> str:
    """The filesystem checks (§4.3, second kind). Returns the filesystem type."""
    check_local_directory(local)
    return check_filesystem(local, mountinfo_path=mountinfo_path)


def create_local_root(local: str) -> str:
    """Create the local replica root 0700, umask-independent (§7.5, third kind).

    Only the root itself is forced to 0700; any parent this has to create keeps
    the ordinary umask, because a shared parent such as ``~/iCloud Workspaces``
    is not this workspace's private state.
    """
    check_local_directory(local)
    try:
        os.makedirs(local, mode=LOCAL_ROOT_MODE, exist_ok=True)
        os.chmod(local, LOCAL_ROOT_MODE)
    except OSError as exc:
        raise WorkspaceError(f"cannot create {local}: {exc}") from exc
    return local


# ------------------------------------------------- filesystem classification --

def _unescape_mountinfo(field: str) -> str:
    """Undo mountinfo's octal escaping of space, tab, newline and backslash."""
    out: list[str] = []
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and field[index + 1:index + 4].isdigit() \
                and len(field[index + 1:index + 4]) == 3:
            try:
                out.append(chr(int(field[index + 1:index + 4], 8)))
            except ValueError:
                out.append(char)
                index += 1
                continue
            index += 4
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_mountinfo(text: str) -> tuple[tuple[str, str], ...]:
    """``(mount point, filesystem type)`` for every usable line, in file order.

    The optional-fields section has a variable length, so the filesystem type is
    found relative to the ``-`` separator rather than at a fixed index.
    Unparseable lines are skipped: one of them must not hide the rest.
    """
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 10 or "-" not in fields:
            continue
        separator = fields.index("-")
        if separator < 6 or separator + 1 >= len(fields):
            continue
        mount_point = _unescape_mountinfo(fields[4])
        if not mount_point.startswith("/"):
            continue
        entries.append((os.path.normpath(mount_point), fields[separator + 1]))
    return tuple(entries)


def read_mountinfo(path: str = MOUNTINFO_PATH) -> tuple[tuple[str, str], ...]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise WorkspaceError(f"cannot read {path}: {exc}") from exc
    return parse_mountinfo(text)


def nearest_existing(path: str) -> str:
    """``path`` itself, or the closest ancestor that exists (``lstat``-based)."""
    current = os.path.normpath(path)
    while True:
        if os.path.lexists(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent


def filesystem_type(path: str, *,
                    mountinfo_path: str = MOUNTINFO_PATH) -> str:
    """The filesystem holding ``path``, or its nearest existing parent (§5.2).

    The longest matching mount point wins, and a later line beats an earlier one
    of the same length because that is the mount currently on top.
    """
    target = nearest_existing(path)
    best_point = ""
    best_type = ""
    for mount_point, kind in read_mountinfo(mountinfo_path):
        if _is_within(target, mount_point) and len(mount_point) >= len(best_point):
            best_point, best_type = mount_point, kind
    if not best_type:
        raise WorkspaceError(
            f"cannot tell which filesystem holds {path}, so it is not usable "
            "as a workspace folder")
    return best_type


def check_filesystem(local: str, *, mountinfo_path: str = MOUNTINFO_PATH) -> str:
    """Refuse anything outside the local-disk allowlist, naming what was found."""
    kind = filesystem_type(local, mountinfo_path=mountinfo_path)
    if kind not in ALLOWED_FILESYSTEMS:
        raise WorkspaceError(
            f"{local} is on a {kind} filesystem; a workspace must live on a "
            f"local disk ({', '.join(ALLOWED_FILESYSTEMS)})")
    return kind


# ------------------------------------------------------- reading and writing --

def _parse_workspace(entry: object,
                     environ: Mapping[str, str] | None = None) -> Workspace:
    if not isinstance(entry, dict):
        raise WorkspaceError("a workspace entry is not a JSON object")
    unknown = sorted(set(entry) - _WORKSPACE_FIELDS)
    if unknown:
        raise WorkspaceError(
            f"a workspace entry has unknown field(s): {', '.join(unknown)}")
    missing = sorted(_WORKSPACE_FIELDS - set(entry))
    if missing:
        raise WorkspaceError(
            f"a workspace entry is missing field(s): {', '.join(missing)}")
    enabled = entry["enabled"]
    if not isinstance(enabled, bool):
        raise WorkspaceError('a workspace "enabled" flag is not true or false')
    return Workspace(
        id=validate_id(entry["id"]),
        name=normalize_name(entry["name"]),
        remote=normalize_remote(entry["remote"]),
        local=normalize_local(entry["local"], environ),
        enabled=enabled,
    )


def parse(document: object,
          environ: Mapping[str, str] | None = None) -> WorkspaceConfig:
    """Validate a whole configuration document, or raise.

    Unknown fields are rejected at both levels: there is one schema version, so
    a field this app does not know is a file it does not understand.
    """
    if not isinstance(document, dict):
        raise WorkspaceError(f"{CONFIG_NAME} is not a JSON object")
    unknown = sorted(set(document) - _DOCUMENT_FIELDS)
    if unknown:
        raise WorkspaceError(
            f"{CONFIG_NAME} has unknown field(s): {', '.join(unknown)}")
    version = document.get("version")
    if isinstance(version, bool) or version != CONFIG_VERSION:
        raise WorkspaceError(
            f"{CONFIG_NAME} reports version {version!r}; this app supports only "
            f"version {CONFIG_VERSION}")
    entries = document.get("workspaces")
    if not isinstance(entries, list):
        raise WorkspaceError(f'{CONFIG_NAME} has no "workspaces" list')
    if len(entries) > MAX_WORKSPACES:
        raise WorkspaceError(
            f"{CONFIG_NAME} holds more than {MAX_WORKSPACES} workspaces")
    parsed = tuple(_parse_workspace(entry, environ) for entry in entries)
    check_collisions(parsed)
    return WorkspaceConfig(workspaces=parsed)


def load(base: str | None = None,
         environ: Mapping[str, str] | None = None) -> WorkspaceConfig:
    """The configured workspaces, or raise :class:`WorkspaceError`.

    A missing file means "none configured" and is not an error. Anything else
    that cannot be read and validated is one, and the file is left exactly as it
    is: a malformed configuration must never be silently replaced by an empty
    one, which would forget the operator's workspaces without saying so.
    """
    path = config_path(base)
    if os.path.islink(path):
        raise WorkspaceError(f"{path} is a symlink; refusing to read through it")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return WorkspaceConfig()
    except OSError as exc:
        raise WorkspaceError(f"cannot inspect {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(f"{path} is not a regular file")
    if info.st_size > MAX_CONFIG_BYTES:
        raise WorkspaceError(f"{path} exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        document = json.loads(raw.decode("utf-8-sig", errors="strict"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise WorkspaceError(
            f"{path} is not readable as a workspace configuration: {exc}") from exc
    return parse(document, environ)


def write_json_atomic(path: str, document: dict) -> None:
    """Unique same-directory temporary file, ``fsync``, then ``os.replace``.

    `backup.py`'s helper, which already guarantees the 0600 mode and removes its
    temporary file when the write fails; only the error type is translated.
    """
    try:
        backup.write_json_atomic(path, document)
    except backup.BackupError as exc:
        raise WorkspaceError(str(exc)) from exc


def _existing_document(path: str) -> object | None:
    """What is on disk right now, or ``None`` when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read(MAX_CONFIG_BYTES + 1)
                              .decode("utf-8-sig", errors="strict"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def save(config: WorkspaceConfig, base: str | None = None) -> str:
    """Write the configuration atomically. Returns :data:`SAVED` or
    :data:`UNCHANGED`.

    The collision rules are applied again here, so a configuration this app
    would refuse to read is never one it writes.
    """
    if not isinstance(config, WorkspaceConfig):
        raise WorkspaceError("refusing to save something that is not a configuration")
    check_collisions(config.workspaces)
    directory = ensure_config_dir(base)
    path = os.path.join(directory, CONFIG_NAME)
    try:
        present = backup.check_destination(path)
    except backup.BackupError as exc:
        raise WorkspaceError(str(exc)) from exc
    document = config.as_document()
    if present and _existing_document(path) == document:
        # Nothing to write, but a file left group- or world-readable is still
        # worth tightening (§4.1).
        try:
            os.chmod(path, FILE_MODE)
        except OSError as exc:
            raise WorkspaceError(f"cannot tighten {path}: {exc}") from exc
        return UNCHANGED
    write_json_atomic(path, document)
    return SAVED


# ------------------------------------------------------------ edit helpers --

def with_workspace(config: WorkspaceConfig,
                   workspace: Workspace) -> WorkspaceConfig:
    """``config`` plus one workspace, or raise if it collides (§4.2, edit time)."""
    updated = (*config.workspaces, workspace)
    check_collisions(updated)
    return WorkspaceConfig(workspaces=updated)


def without_workspace(config: WorkspaceConfig,
                      workspace_id: str) -> WorkspaceConfig:
    """``config`` minus one workspace. Only the entry goes; nothing is deleted."""
    if config.get(workspace_id) is None:
        raise WorkspaceError(f"there is no workspace {workspace_id}")
    return WorkspaceConfig(
        workspaces=tuple(w for w in config.workspaces if w.id != workspace_id))


def with_enabled(config: WorkspaceConfig, workspace_id: str,
                 enabled: bool) -> WorkspaceConfig:
    """Pause or resume one workspace. A paused one stays configured and visible."""
    if config.get(workspace_id) is None:
        raise WorkspaceError(f"there is no workspace {workspace_id}")
    return WorkspaceConfig(workspaces=tuple(
        replace(w, enabled=bool(enabled)) if w.id == workspace_id else w
        for w in config.workspaces))
