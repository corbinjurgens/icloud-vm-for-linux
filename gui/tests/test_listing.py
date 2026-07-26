"""File-listing bookkeeping tests: the idle/loading/loaded machine, no Qt."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import listing  # noqa: E402


def dispatch(requests, request_id, path, offset=0, kind=listing.FIRST_PAGE, deadline=100.0):
    return requests.dispatched(request_id, path, offset, kind, deadline)


# ----------------------------------------------------------- folder states ---

def test_a_fresh_folder_is_idle():
    assert listing.FolderRequests().state("Docs") == listing.IDLE


def test_expansion_claims_a_folder_and_blocks_a_duplicate():
    requests = listing.FolderRequests()
    assert requests.begin_first_page("Docs") is True
    assert requests.state("Docs") == listing.LOADING
    # Re-expanding while loading must not queue a second request.
    assert requests.begin_first_page("Docs") is False


def test_folder_state_is_case_insensitive():
    requests = listing.FolderRequests()
    requests.begin_first_page("Docs")
    assert requests.state("DOCS") == listing.LOADING
    assert requests.begin_first_page("docs") is False


def test_a_dispatch_failure_returns_the_folder_to_idle():
    requests = listing.FolderRequests()
    requests.begin_first_page("Docs")
    requests.release("Docs")
    assert requests.state("Docs") == listing.IDLE
    assert requests.begin_first_page("Docs") is True     # retryable


def test_only_a_successful_first_page_reaches_loaded():
    requests = listing.FolderRequests()
    requests.begin_first_page("Docs")
    dispatch(requests, "a" * 32, "Docs")
    assert requests.take("a" * 32) is not None
    requests.mark_loaded("Docs")
    assert requests.state("Docs") == listing.LOADED
    # A loaded folder is not re-requested by another expansion.
    assert requests.begin_first_page("Docs") is False


def test_an_empty_response_still_counts_as_loaded():
    """A folder with no files must not ask again on every expansion."""
    requests = listing.FolderRequests()
    requests.begin_first_page("Empty")
    dispatch(requests, "b" * 32, "Empty")
    requests.take("b" * 32)
    requests.mark_loaded("Empty")
    assert requests.state("Empty") == listing.LOADED


# ------------------------------------------------------------- failure paths --

def test_failure_frees_a_first_page_folder_and_returns_the_request():
    requests = listing.FolderRequests()
    requests.begin_first_page("Docs")
    dispatch(requests, "c" * 32, "Docs")
    failed = requests.fail("c" * 32)
    assert failed.path == "Docs"
    assert requests.state("Docs") == listing.IDLE
    assert requests.get("c" * 32) is None


def test_failure_of_a_continuation_reports_its_offset_for_retry():
    requests = listing.FolderRequests()
    requests.mark_loaded("Docs")
    dispatch(requests, "d" * 32, "Docs", offset=1000, kind=listing.MORE)
    failed = requests.fail("d" * 32)
    assert failed.offset == 1000
    assert failed.is_first_page is False
    # The folder itself stays loaded; only the continuation row is restored.
    assert requests.state("Docs") == listing.LOADED


def test_failing_an_unknown_request_is_harmless():
    assert listing.FolderRequests().fail("e" * 32) is None


def test_expired_reports_requests_past_their_deadline():
    requests = listing.FolderRequests()
    dispatch(requests, "f" * 32, "Docs", deadline=100.0)
    dispatch(requests, "g" * 32, "Other", deadline=300.0)
    expired = requests.expired(now=200.0)
    assert [p.request_id for p in expired] == ["f" * 32]


# --------------------------------------------------------- tree generations ---

def test_reset_clears_state_and_bumps_the_generation():
    requests = listing.FolderRequests()
    requests.begin_first_page("Docs")
    dispatch(requests, "h" * 32, "Docs")
    before = requests.generation
    assert requests.reset() == before + 1
    assert requests.state("Docs") == listing.IDLE
    assert requests.pending_ids() == []


def test_a_response_from_before_a_reload_is_discarded():
    """A worker that completes after Reload must not mutate the rebuilt tree."""
    requests = listing.FolderRequests()
    requests.begin_first_page("Docs")
    dispatch(requests, "i" * 32, "Docs")
    requests.reset()
    # The tree was rebuilt and the folder re-requested under a new id.
    requests.begin_first_page("Docs")
    dispatch(requests, "j" * 32, "Docs")
    assert requests.take("i" * 32) is None
    assert requests.take("j" * 32) is not None


def test_a_stale_failure_does_not_free_the_new_generations_folder():
    requests = listing.FolderRequests()
    requests.begin_first_page("Docs")
    stale = requests.dispatched("k" * 32, "Docs", 0, listing.FIRST_PAGE, 100.0)
    requests.reset()
    requests.begin_first_page("Docs")
    assert requests.fail(stale.request_id) is None
    assert requests.state("Docs") == listing.LOADING
