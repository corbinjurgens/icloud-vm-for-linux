"""Host-side snapshot of the selective-sync choices (v2 plan D36).

`exclusions.json` is the one piece of unique configuration that lives inside the
otherwise disposable Windows VM. The provisioning rule that refuses to
manufacture an empty config after loss is right — writing `[]` would silently
re-include everything — but it leaves the operator with nothing to restore *from*
unless they happened to keep their own copy. This module is that copy.

Design constraints, all of them deliberate:

* **Qt-free and mount-I/O-free.** The backup file is local disk in the desktop
  user's XDG state directory. Nothing here ever touches `/mnt/icloud*`.
* **Never automatic on the way back.** :func:`save` runs by itself after a
  validated read or a successful Apply; :func:`load` is only ever called by an
  explicit, previewed **Restore from backup** action.
* **A lower revision never overwrites a higher one.** A rebuilt VM comes up with
  a fresh revision-0 empty config, and the first automatic read after that would
  otherwise destroy the very copy the operator needs. Same revision with
  different content is a conflict: also retained, and reported.
* **A missing or corrupt backup is an error, never an empty list.** Same
  fail-closed rule as the config itself.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from . import bridge

BACKUP_VERSION = 1
APP_DIR_NAME = "icloud-bridge-gui"
BACKUP_NAME = "exclusions-backup.json"

#: What produced the snapshot: a validated periodic read, or an explicit Apply.
SOURCE_READ = "read"
SOURCE_APPLY = "apply"

#: :func:`save` outcomes.
SAVED = "saved"              # the backup now holds this list at this revision
UNCHANGED = "unchanged"      # identical content and revision; only mode tightened
KEPT_NEWER = "kept-newer"    # the saved copy is ahead; a rebuilt VM, most likely
CONFLICT = "conflict"        # same revision, different content: kept and reported

DIR_MODE = 0o700
FILE_MODE = 0o600

#: Bounds. The backup mirrors a config the agent already caps, and it is read
#: back from a file the desktop user could have edited by hand.
MAX_BACKUP_BYTES = 1024 * 1024


class BackupError(Exception):
    """The backup could not be read, written, or trusted."""


@dataclass(frozen=True)
class Backup:
    """One validated snapshot. ``exclusions`` is already canonical."""

    revision: int
    exclusions: tuple[str, ...]
    source: str = SOURCE_READ
    saved_at: str = ""

    def as_document(self) -> dict:
        return {
            "version": BACKUP_VERSION,
            "savedAt": self.saved_at,
            "source": self.source,
            "revision": self.revision,
            "exclusions": list(self.exclusions),
        }


# ------------------------------------------------------------------- paths --

def state_base(environ: dict | None = None) -> str:
    """``$XDG_STATE_HOME``, or the spec's default when it is unset or relative."""
    env = os.environ if environ is None else environ
    value = env.get("XDG_STATE_HOME") or ""
    if not os.path.isabs(value):
        return os.path.join(os.path.expanduser("~"), ".local", "state")
    return value


def app_dir(base: str | None = None) -> str:
    return os.path.join(state_base() if base is None else base, APP_DIR_NAME)


def backup_path(base: str | None = None) -> str:
    return os.path.join(app_dir(base), BACKUP_NAME)


def ensure_app_dir(base: str | None = None) -> str:
    """Create or tighten the app state directory to 0700, refusing a surprise.

    A symlink here would let anything the desktop user can write to redirect a
    file we are about to chmod 0600 and replace, so it is refused outright
    rather than followed.
    """
    path = app_dir(base)
    if os.path.islink(path):
        raise BackupError(f"{path} is a symlink; refusing to use it")
    if os.path.exists(path) and not os.path.isdir(path):
        raise BackupError(f"{path} exists and is not a directory")
    try:
        os.makedirs(path, mode=DIR_MODE, exist_ok=True)
        os.chmod(path, DIR_MODE)
    except OSError as exc:
        raise BackupError(f"cannot prepare {path}: {exc}") from exc
    return path


def _check_destination(path: str) -> bool:
    """Whether a usable regular backup already exists. Refuses anything else."""
    if os.path.islink(path):
        raise BackupError(f"{path} is a symlink; refusing to write through it")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BackupError(f"cannot inspect {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BackupError(f"{path} is not a regular file; refusing to replace it")
    return True


# ----------------------------------------------------------------- reading --

def parse(document: object) -> Backup:
    """Validate a backup document, or raise. Canonicalizes as it goes.

    Deliberately strict about everything except *age*: a stale snapshot is
    exactly what a restore is for, so an old revision is not a reason to reject.
    """
    if not isinstance(document, dict):
        raise BackupError("the backup is not a JSON object")
    if document.get("version") != BACKUP_VERSION:
        raise BackupError('the backup has an unsupported "version"')
    revision = document.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise BackupError('the backup "revision" is not a non-negative integer')
    entries = document.get("exclusions")
    if not isinstance(entries, list):
        raise BackupError('the backup has no "exclusions" list')
    if len(entries) > bridge.MAX_CONFIG_ENTRIES:
        raise BackupError(
            f"the backup has more than {bridge.MAX_CONFIG_ENTRIES} entries")
    for entry in entries:
        if not isinstance(entry, str):
            raise BackupError("the backup contains a non-string entry")
    try:
        canonical = bridge.canonicalize(entries)
    except ValueError as exc:
        raise BackupError(f"the backup contains an invalid path: {exc}") from exc
    source = document.get("source")
    saved_at = document.get("savedAt")
    return Backup(
        revision=revision,
        exclusions=tuple(canonical),
        source=source if source in (SOURCE_READ, SOURCE_APPLY) else SOURCE_READ,
        saved_at=saved_at if isinstance(saved_at, str) else "",
    )


def load(base: str | None = None) -> Backup:
    """Read and validate the saved snapshot, or raise :class:`BackupError`.

    An absent or unreadable backup is an error the caller reports. It is never
    interpreted as "no exclusions" — that would turn a lost file into a silent
    re-include of everything.
    """
    path = backup_path(base)
    if os.path.islink(path):
        raise BackupError(f"{path} is a symlink; refusing to read through it")
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        raise BackupError(
            "There is no saved copy of your selective-sync choices yet. One is "
            "written automatically whenever the configuration is read or "
            "applied.") from None
    except OSError as exc:
        raise BackupError(f"cannot read {path}: {exc}") from exc
    if size > MAX_BACKUP_BYTES:
        raise BackupError(f"{path} exceeds {MAX_BACKUP_BYTES} bytes")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BACKUP_BYTES + 1)
        document = json.loads(raw.decode("utf-8-sig", errors="strict"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise BackupError(f"{path} is not readable as a backup: {exc}") from exc
    return parse(document)


def _load_quietly(base: str | None) -> Backup | None:
    """The saved snapshot, or ``None`` when there is not a usable one."""
    try:
        return load(base)
    except BackupError:
        return None


# ----------------------------------------------------------------- writing --

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def save(exclusions: Iterable[str], revision: int, source: str, *,
         base: str | None = None,
         now: Callable[[], datetime] = _utc_now) -> str:
    """Record the current selection, honouring the D36 replacement rules.

    Returns one of :data:`SAVED`, :data:`UNCHANGED`, :data:`KEPT_NEWER` or
    :data:`CONFLICT`. Raises :class:`BackupError` only when the *local* write
    genuinely failed — the caller treats that as a second, separate result from
    whatever bridge operation produced the data.
    """
    if source not in (SOURCE_READ, SOURCE_APPLY):
        raise BackupError(f"unknown backup source {source!r}")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise BackupError("refusing to save a snapshot without a valid revision")
    try:
        canonical = tuple(bridge.canonicalize(exclusions))
    except ValueError as exc:
        raise BackupError(f"refusing to save an invalid selection: {exc}") from exc

    directory = ensure_app_dir(base)
    path = os.path.join(directory, BACKUP_NAME)
    existing_present = _check_destination(path)
    existing = _load_quietly(base) if existing_present else None

    outcome = SAVED
    if existing is not None:
        same_content = existing.exclusions == canonical
        if existing.revision == revision and same_content:
            outcome = UNCHANGED
        elif source == SOURCE_READ:
            # An automatic read may only move the snapshot forward. A lower
            # revision is the signature of a rebuilt VM; the same revision with
            # different content means somebody else changed the config.
            if existing.revision > revision:
                outcome = KEPT_NEWER
            elif existing.revision == revision:
                outcome = CONFLICT

    if outcome != SAVED:
        # Nothing to write, but a backup left group- or world-readable is still
        # worth tightening even when its content did not change.
        if existing_present:
            try:
                os.chmod(path, FILE_MODE)
            except OSError as exc:
                raise BackupError(f"cannot tighten {path}: {exc}") from exc
        return outcome

    document = Backup(revision=revision, exclusions=canonical, source=source,
                      saved_at=now().strftime("%Y-%m-%dT%H:%M:%SZ")).as_document()
    _write_atomic(path, document)
    return SAVED


def _write_atomic(path: str, document: dict) -> None:
    """Unique temp file in the same directory, 0600, then ``os.replace``."""
    directory = os.path.dirname(path) or "."
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix="." + BACKUP_NAME + ".", suffix=".tmp", delete=False)
    except OSError as exc:
        raise BackupError(f"cannot write {path}: {exc}") from exc
    temp_name = handle.name
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Before the rename, so the file is never briefly world-readable at its
        # real name. NamedTemporaryFile already creates it 0600; this is belt
        # and braces against a future change of that default.
        os.chmod(temp_name, FILE_MODE)
        os.replace(temp_name, path)
    except OSError as exc:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise BackupError(f"cannot write {path}: {exc}") from exc


# ----------------------------------------------------------------- restore --

@dataclass(frozen=True)
class RestorePreview:
    """What a restore would change, relative to the loaded on-disk selection."""

    backup: Backup
    additions: tuple[str, ...]
    removals: tuple[str, ...]

    @property
    def changes_anything(self) -> bool:
        return bool(self.additions or self.removals)


def preview(saved: Backup, current: Iterable[str]) -> RestorePreview:
    """Diff the snapshot against what is on the bridge right now.

    Case-insensitive, like every other comparison of these paths (D19).
    """
    loaded = bridge.canonicalize(current)
    loaded_keys = {path.lower() for path in loaded}
    backup_keys = {path.lower() for path in saved.exclusions}
    additions = tuple(p for p in saved.exclusions if p.lower() not in loaded_keys)
    removals = tuple(p for p in loaded if p.lower() not in backup_keys)
    return RestorePreview(backup=saved, additions=additions, removals=removals)
