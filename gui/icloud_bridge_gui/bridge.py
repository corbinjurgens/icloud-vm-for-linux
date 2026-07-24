"""Bridge-share I/O for the host GUI.

The control channel is a small SMB share exported by the Windows guest at
``C:\\ProgramData\\icloud-bridge\\io`` and mounted here (v2 plan D16).  Nothing
in this module imports Qt: it is plain filesystem work so it can be unit tested
against a temporary directory and run from a worker thread.

Every write replaces the target atomically via a unique temporary file in the
same directory (v2 plan section 2).  Every read is bounded, because the bridge
is a two-way channel and a malformed or oversized file must not wedge the GUI.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from typing import Any, Iterable

DEFAULT_BRIDGE_DIR = "/mnt/icloud_bridge"
DEFAULT_MOUNT_DIR = "/mnt/icloud"

# Mirrors the agent's own bounds so the two ends agree on what is acceptable.
MAX_CONFIG_BYTES = 1024 * 1024
MAX_CONFIG_ENTRIES = 10_000
MAX_STATUS_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_LIST_LIMIT = 1000

_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_BAD_SEGMENT_CHARS = set('<>:"|?*')


class BridgeError(Exception):
    """The bridge share is unreachable, or its contents are unusable."""


class RevisionConflict(BridgeError):
    """exclusions.json changed underneath us; reload before writing again."""


def bridge_dir() -> str:
    """Where the guest's control share is mounted.

    Read from the environment on every call so tests (and ``ICLOUD_BRIDGE_DIR``
    on a developer machine) can point the whole GUI at a fake share.
    """
    return os.environ.get("ICLOUD_BRIDGE_DIR", DEFAULT_BRIDGE_DIR)


def mount_dir() -> str:
    """Where the guest's iCloud Drive data share is mounted."""
    return os.environ.get("ICLOUD_MOUNT_DIR", DEFAULT_MOUNT_DIR)


# --------------------------------------------------------------- path rules --

def validate_relpath(path: str, *, allow_root: bool = False) -> str:
    """Canonicalise one sync-root-relative path, or raise ``ValueError``.

    The agent validates independently (v2 plan D22d); this copy exists so the
    GUI never *sends* something it knows to be invalid.
    """
    if not isinstance(path, str):
        raise ValueError("path is not a string")
    if "\x00" in path:
        raise ValueError("path contains NUL")
    if len(path) > 4096:
        raise ValueError("path too long")
    text = path.replace("\\", "/")
    if text == "":
        if allow_root:
            return ""
        raise ValueError("the sync root itself cannot be used here")
    if text.startswith("/"):
        raise ValueError("rooted or UNC path")
    segments = text.split("/")
    for segment in segments:
        if segment == "":
            raise ValueError("empty path segment")
        if segment in (".", ".."):
            raise ValueError("relative path segment")
        if _BAD_SEGMENT_CHARS & set(segment):
            raise ValueError("invalid character in path segment")
        if segment.endswith(" ") or segment.endswith("."):
            raise ValueError("path segment ends with a space or dot")
    return "/".join(segments)


def canonicalize(paths: Iterable[str]) -> list[str]:
    """Reduce a set of exclusions to the D19 minimal antichain.

    Case-insensitive de-duplication, then every path that lives under another
    entry is dropped: an excluded folder already covers everything inside it.
    """
    seen: dict[str, str] = {}
    for raw in paths:
        canonical = validate_relpath(raw)
        seen.setdefault(canonical.lower(), canonical)
    ordered = sorted(seen.values(), key=lambda p: (p.lower(), p))
    result: list[str] = []
    for path in ordered:
        lowered = path.lower()
        if any(lowered.startswith(kept.lower() + "/") for kept in result):
            continue
        result.append(path)
    return result


def is_under(path: str, roots: Iterable[str]) -> bool:
    """True when ``path`` is one of ``roots`` or lives inside one of them."""
    lowered = path.lower()
    for root in roots:
        low_root = root.lower()
        if lowered == low_root or lowered.startswith(low_root + "/"):
            return True
    return False


# ------------------------------------------------------------------- reading --

def _read_json(path: str, max_bytes: int) -> Any:
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise BridgeError(f"cannot stat {path}: {exc}") from exc
    if size > max_bytes:
        raise BridgeError(f"{os.path.basename(path)} exceeds {max_bytes} bytes")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        raise BridgeError(f"cannot read {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig", errors="strict")
        return json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BridgeError(f"{os.path.basename(path)} is not valid JSON: {exc}") from exc


def read_status() -> dict:
    """The agent's 15-second health/enforcement report."""
    data = _read_json(os.path.join(bridge_dir(), "status.json"), MAX_STATUS_BYTES)
    if not isinstance(data, dict):
        raise BridgeError("status.json is not a JSON object")
    return data


def read_tree() -> dict:
    """The agent's ten-minute folder tree (folders only; files via §2.4)."""
    data = _read_json(os.path.join(bridge_dir(), "tree.json"), MAX_TREE_BYTES)
    if not isinstance(data, dict):
        raise BridgeError("tree.json is not a JSON object")
    return data


def read_exclusions() -> dict:
    """The current configuration, validated.

    Fails closed exactly like the agent does: a malformed document is an error,
    never "no exclusions", so the GUI cannot present an empty selection that
    would silently re-include everything on the next Apply.
    """
    path = os.path.join(bridge_dir(), "exclusions.json")
    data = _read_json(path, MAX_CONFIG_BYTES)
    if not isinstance(data, dict):
        raise BridgeError("exclusions.json is not a JSON object")
    if data.get("version") != 1:
        raise BridgeError('exclusions.json has an unsupported "version"')
    revision = data.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise BridgeError('exclusions.json "revision" is not a non-negative integer')
    entries = data.get("exclusions")
    if not isinstance(entries, list):
        raise BridgeError('exclusions.json has no "exclusions" list')
    if len(entries) > MAX_CONFIG_ENTRIES:
        raise BridgeError(f"exclusions.json has more than {MAX_CONFIG_ENTRIES} entries")
    for entry in entries:
        if not isinstance(entry, str):
            raise BridgeError("exclusions.json contains a non-string entry")
        try:
            validate_relpath(entry)
        except ValueError as exc:
            raise BridgeError(f"invalid exclusion path {entry!r}: {exc}") from exc
    return {"version": 1, "revision": revision, "exclusions": list(entries)}


# ------------------------------------------------------------------- writing --

def _write_json_atomic(path: str, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    directory = os.path.dirname(path) or "."
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix="." + os.path.basename(path) + ".", suffix=".tmp", delete=False,
        )
    except OSError as exc:
        raise BridgeError(f"cannot write {path}: {exc}") from exc
    temp_name = handle.name
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise BridgeError(f"cannot write {path}: {exc}") from exc


def next_revision(*candidates: Any) -> int:
    """One more than the highest valid revision seen anywhere.

    Callers pass the on-disk revision, ``status.appliedRevision`` and the GUI's
    own last write, so a restarted GUI never resets the counter to 1 and the
    agent's monotonic guard never trips (v2 plan section 6.2).
    """
    best = -1
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            best = max(best, value)
    return best + 1


def write_exclusions(paths: Iterable[str], *, expect_revision: int | None = None,
                     applied_revision: Any = None, last_written: Any = None) -> int:
    """Publish a new exclusion set; returns the revision actually written.

    ``expect_revision`` is the revision the UI loaded.  If the file has moved on
    since then, someone else edited it and we raise instead of clobbering.
    """
    wanted = canonicalize(paths)
    if len(wanted) > MAX_CONFIG_ENTRIES:
        raise BridgeError(f"refusing to write more than {MAX_CONFIG_ENTRIES} exclusions")

    current_revision: Any = None
    try:
        current = read_exclusions()
        current_revision = current["revision"]
    except BridgeError:
        # A missing or unreadable config is only tolerable when the caller did
        # not claim to have loaded one.
        if expect_revision is not None:
            raise
    if expect_revision is not None and current_revision != expect_revision:
        raise RevisionConflict(
            f"exclusions.json moved from revision {expect_revision} to {current_revision}; reload first"
        )

    revision = next_revision(current_revision, applied_revision, last_written)
    payload = {"version": 1, "revision": revision, "exclusions": wanted}
    _write_json_atomic(os.path.join(bridge_dir(), "exclusions.json"), payload)
    return revision


# -------------------------------------------------- per-folder file listings --

def request_listing(path: str, offset: int = 0, limit: int = MAX_LIST_LIMIT) -> str:
    """Ask the agent to enumerate one folder; returns the request id."""
    canonical = validate_relpath(path, allow_root=True)
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    request_id = secrets.token_hex(16)
    target = os.path.join(bridge_dir(), "requests", f"list-{request_id}.json")
    _write_json_atomic(target, {"path": canonical, "offset": offset, "limit": limit})
    return request_id


def poll_response(request_id: str) -> dict | None:
    """Consume the agent's reply, or ``None`` while it has not answered yet."""
    if not _REQUEST_ID_RE.match(request_id):
        raise ValueError("malformed request id")
    path = os.path.join(bridge_dir(), "responses", f"list-{request_id}.json")
    if not os.path.exists(path):
        return None
    try:
        data = _read_json(path, MAX_RESPONSE_BYTES)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not isinstance(data, dict):
        raise BridgeError("list response is not a JSON object")
    return data


def cancel_request(request_id: str) -> None:
    """Drop an abandoned request; the agent also expires them after 10 minutes."""
    if not _REQUEST_ID_RE.match(request_id):
        raise ValueError("malformed request id")
    for sub in ("requests", "responses"):
        try:
            os.unlink(os.path.join(bridge_dir(), sub, f"list-{request_id}.json"))
        except OSError:
            pass
