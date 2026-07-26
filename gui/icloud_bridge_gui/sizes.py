"""How much is excluded, stated honestly (Qt-free).

The Selective Sync tab shows a one-line summary of the current selection.  The
temptation is to call it "disk space saved"; this module deliberately does not,
and the caller must not either.  `logicalBytes` is *logical content size* — what
the data would occupy — while dehydration is asynchronous and an exclusion can
sit at `pending-dehydrate` indefinitely.  The honest claim is "about N GB
logical", plus how many roots have no size at all.

The size sources are incomplete and only make sense combined:

* `tree.json` carries recursive `logicalBytes` for **folders** only;
* list responses carry sizes for **files** loaded in this GUI session;
* `status.json.exclusions[].logicalBytes` is the size the agent last applied,
  usable only for an exact, still-configured root;
* anything else — a path that does not exist, or a file staged for exclusion
  whose folder was never expanded — is genuinely **unknown**, and is reported as
  such rather than silently counted as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .health import human_bytes


def _valid_size(value: Any) -> int | None:
    """A usable byte count, or ``None`` for missing/malformed/negative input."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def canonical_roots(paths: Iterable[Any]) -> list[str]:
    """De-duplicate case-insensitively and drop anything under another entry.

    D19 already guarantees the configured set is a minimal antichain, so this is
    a defensive re-derivation: the summary must not double-count if a malformed
    or hand-edited config slips a child in beside its parent.
    """
    seen: dict[str, str] = {}
    for path in paths:
        if isinstance(path, str) and path:
            seen.setdefault(path.lower(), path)
    ordered = sorted(seen.values(), key=lambda p: (p.lower(), p))
    roots: list[str] = []
    for path in ordered:
        lowered = path.lower()
        if any(lowered.startswith(kept.lower() + "/") for kept in roots):
            continue
        roots.append(path)
    return roots


@dataclass(frozen=True)
class ExclusionSummary:
    roots: int
    known_bytes: int
    unknown: int

    def text(self) -> str:
        """The label shown above the tree."""
        if self.roots == 0:
            return "Nothing is excluded."
        roots = "1 root" if self.roots == 1 else f"{self.roots} roots"
        if self.known_bytes == 0 and self.unknown == self.roots:
            return f"Excluded: {roots}, size unknown"
        line = f"Excluded: {roots}, about {human_bytes(self.known_bytes)} logical"
        if self.unknown:
            line += f" ({self.unknown} size{'s' if self.unknown > 1 else ''} unknown)"
        return line


def summarize(wanted: Iterable[Any], *,
              folder_sizes: Mapping[str, Any] | None = None,
              file_sizes: Mapping[str, Any] | None = None,
              status_sizes: Mapping[str, Any] | None = None,
              configured: Iterable[Any] = ()) -> ExclusionSummary:
    """Total the currently selected exclusions from whichever source knows them.

    ``wanted`` is the live selection (staged edits included), so a re-include
    drops out of the total the moment its box is ticked.  ``configured`` is the
    set already in ``exclusions.json``; only those may fall back to
    ``status_sizes``, because the agent's reported size describes what it
    actually applied, not something the operator just staged.

    All lookups are case-insensitive, matching D19's comparison rule.
    """
    folder_sizes = folder_sizes or {}
    file_sizes = file_sizes or {}
    status_sizes = status_sizes or {}
    applied = {p.lower() for p in configured if isinstance(p, str)}

    roots = canonical_roots(wanted)
    known = 0
    unknown = 0
    for root in roots:
        lowered = root.lower()
        size = _valid_size(folder_sizes.get(lowered))
        if size is None:
            size = _valid_size(file_sizes.get(lowered))
        if size is None and lowered in applied:
            size = _valid_size(status_sizes.get(lowered))
        if size is None:
            unknown += 1
        else:
            known += size
    return ExclusionSummary(roots=len(roots), known_bytes=known, unknown=unknown)
