"""Which tree rows a Selective Sync filter should leave visible (Qt-free).

Filtering is presentation only: it never changes the selection, the check
states, or the in-memory tree.  Separating the calculation from the widget keeps
that honest and makes the interesting cases — ancestors, path matches, missing
items, no match at all — testable without Qt.

Matching is case-insensitive over both the folder/file *name* and its full
relative path, using the same comparison rule as ``bridge.is_under`` (lowercase,
forward slashes).  A row is visible when it matches, or when it is an ancestor
of something that matches; everything else is hidden, so an unrelated branch
collapses out of the way instead of being merely deselected.
"""

from __future__ import annotations

from typing import Iterable


def normalize(path: str) -> str:
    """The comparison form: forward slashes, lowercase (D19's rule)."""
    return path.replace("\\", "/").lower()


def ancestors(path: str) -> list[str]:
    """Every normalized ancestor of ``path``, nearest last, root excluded."""
    normalized = normalize(path)
    segments = [s for s in normalized.split("/") if s]
    return ["/".join(segments[:i]) for i in range(1, len(segments))]


def matches(query: str, path: str) -> bool:
    """Whether one relative path matches the query by name or by path."""
    needle = normalize(query).strip()
    if not needle:
        return True
    normalized = normalize(path)
    name = normalized.rsplit("/", 1)[-1]
    return needle in name or needle in normalized


def visible_paths(query: str, paths: Iterable[str]) -> set[str] | None:
    """Normalized paths to keep visible, or ``None`` when no filter is active.

    ``None`` and "everything is visible" are deliberately different answers: the
    caller restores the operator's own expanded/collapsed state for the former
    and does not touch the tree at all.
    """
    if not query.strip():
        return None
    visible: set[str] = set()
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        if matches(query, path):
            visible.add(normalize(path))
            visible.update(ancestors(path))
    return visible
