"""Bookkeeping for the per-folder file listings (Qt-free).

Expanding a folder in the Selective Sync tab fires a §2.4 list request whose
answer arrives seconds later through the bridge share.  Getting the bookkeeping
wrong is not cosmetic: mark a folder "loaded" before its request is even
dispatched and a failure leaves it permanently empty with no way to retry, since
re-expanding it does nothing.

So folders carry an explicit state — ``idle`` → ``loading`` → ``loaded`` — and
every failure path (paused I/O, dispatch failure, guest error, malformed
response, cancellation, timeout, a response from before a Reload) returns them
to ``idle``, which is exactly the state that permits another attempt.  Only a
successful first-page response, *including a valid empty one*, reaches
``loaded``.

Requests are tagged with the tree generation that created them so a completion
arriving after Reload cannot mutate the rebuilt tree.  All of it is plain data,
so the regression cases run without Qt.
"""

from __future__ import annotations

from dataclasses import dataclass

IDLE = "idle"
LOADING = "loading"
LOADED = "loaded"

#: Request kinds. The first page owns the folder's state; a later page is a
#: continuation whose only UI is its own "Load more…" row.
FIRST_PAGE = "first"
MORE = "more"


@dataclass(frozen=True)
class PendingRequest:
    request_id: str
    path: str
    offset: int
    kind: str
    generation: int
    deadline: float

    @property
    def is_first_page(self) -> bool:
        return self.kind == FIRST_PAGE


class FolderRequests:
    """The state of every folder listing and every in-flight list request."""

    def __init__(self) -> None:
        self._state: dict[str, str] = {}
        self._pending: dict[str, PendingRequest] = {}
        self._generation = 0

    # -- tree lifecycle ------------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    def reset(self) -> int:
        """Start a new tree generation: forget all states and pending requests."""
        self._state.clear()
        self._pending.clear()
        self._generation += 1
        return self._generation

    # -- folder state --------------------------------------------------------

    def state(self, path: str) -> str:
        return self._state.get(path.lower(), IDLE)

    def begin_first_page(self, path: str) -> bool:
        """Claim a folder for a first-page request.

        Returns ``False`` when one is already in flight or the folder is loaded,
        so a second expansion cannot queue a duplicate request.
        """
        if self.state(path) != IDLE:
            return False
        self._state[path.lower()] = LOADING
        return True

    def release(self, path: str) -> None:
        """Return a folder to ``idle`` so the operator can retry by re-expanding."""
        self._state.pop(path.lower(), None)

    def mark_loaded(self, path: str) -> None:
        self._state[path.lower()] = LOADED

    # -- in-flight requests --------------------------------------------------

    def dispatched(self, request_id: str, path: str, offset: int, kind: str,
                   deadline: float) -> PendingRequest:
        """Record a request the bridge accepted, tagged with this generation."""
        pending = PendingRequest(request_id, path, offset, kind, self._generation, deadline)
        self._pending[request_id] = pending
        return pending

    def pending_ids(self) -> list[str]:
        return list(self._pending)

    def get(self, request_id: str) -> PendingRequest | None:
        return self._pending.get(request_id)

    def take(self, request_id: str) -> PendingRequest | None:
        """Consume a request, or return ``None`` if it is unknown or stale.

        A stale request — one tagged with an earlier tree generation — is
        discarded rather than applied, because the tree it described no longer
        exists.
        """
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.generation != self._generation:
            return None
        return pending

    def fail(self, request_id: str) -> PendingRequest | None:
        """Drop a request and return its folder to ``idle`` if it owned the state.

        Returns the request so the caller can restore the UI (a "Load more…" row
        at the same offset) — the whole point of failing back rather than
        silently forgetting.
        """
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return None
        if pending.generation != self._generation:
            return None
        if pending.is_first_page:
            self.release(pending.path)
        return pending

    def expired(self, now: float) -> list[PendingRequest]:
        """Every in-flight request past its deadline (still recorded)."""
        return [p for p in self._pending.values() if now > p.deadline]
