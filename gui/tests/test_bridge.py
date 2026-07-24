"""Bridge-share tests against a temporary directory standing in for the mount."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import bridge  # noqa: E402


@pytest.fixture
def share(tmp_path, monkeypatch):
    """A fake bridge share with the directory layout the agent maintains."""
    root = tmp_path / "bridge"
    (root / "requests").mkdir(parents=True)
    (root / "responses").mkdir(parents=True)
    monkeypatch.setenv("ICLOUD_BRIDGE_DIR", str(root))
    (root / "exclusions.json").write_text(
        json.dumps({"version": 1, "revision": 0, "exclusions": []}), encoding="utf-8")
    return root


# ------------------------------------------------------------- path rules ---

def test_validate_relpath_accepts_normal_paths():
    assert bridge.validate_relpath("Docs") == "Docs"
    assert bridge.validate_relpath("Docs/notes.txt") == "Docs/notes.txt"
    assert bridge.validate_relpath("Docs\\notes.txt") == "Docs/notes.txt"
    assert bridge.validate_relpath("", allow_root=True) == ""


@pytest.mark.parametrize("bad", [
    "", "/etc/passwd", "..", "../escape", "Docs/../..", "Docs//notes",
    "C:/Windows", "Docs/notes.txt:stream", "Docs/\x00", "Docs/trailing ",
    "Docs/trailing.", "a?b", "a*b", "a|b", 'a"b', "a<b", "a>b",
])
def test_validate_relpath_rejects_bad_paths(bad):
    with pytest.raises(ValueError):
        bridge.validate_relpath(bad)


def test_root_is_rejected_unless_explicitly_allowed():
    with pytest.raises(ValueError):
        bridge.validate_relpath("")


def test_canonicalize_builds_an_antichain():
    assert bridge.canonicalize(["Docs", "Docs/Big", "Other"]) == ["Docs", "Other"]
    assert bridge.canonicalize(["Docs/Big", "Docs"]) == ["Docs"]
    # Case-insensitive de-duplication keeps the first spelling seen.
    assert bridge.canonicalize(["Docs", "docs"]) == ["Docs"]
    assert bridge.canonicalize(["Docs", "DOCS/Big"]) == ["Docs"]
    # A common prefix that is not a path boundary must survive.
    assert bridge.canonicalize(["Docs", "Docs2"]) == ["Docs", "Docs2"]


def test_is_under():
    assert bridge.is_under("Docs/Big", ["Docs"])
    assert bridge.is_under("Docs", ["Docs"])
    assert bridge.is_under("DOCS/Big", ["docs"])
    assert not bridge.is_under("Docs2", ["Docs"])
    assert not bridge.is_under("Docs", [])


# ------------------------------------------------------- reading the config --

def test_read_exclusions_round_trip(share):
    assert bridge.read_exclusions() == {"version": 1, "revision": 0, "exclusions": []}
    revision = bridge.write_exclusions(["Docs/Big", "Docs"], expect_revision=0)
    assert revision == 1
    assert bridge.read_exclusions() == {"version": 1, "revision": 1, "exclusions": ["Docs"]}


@pytest.mark.parametrize("payload", [
    "not json at all",
    json.dumps([1, 2, 3]),
    json.dumps({"version": 2, "revision": 0, "exclusions": []}),
    json.dumps({"version": 1, "revision": "0", "exclusions": []}),
    json.dumps({"version": 1, "revision": -1, "exclusions": []}),
    json.dumps({"version": 1, "revision": 0}),
    json.dumps({"version": 1, "revision": 0, "exclusions": [1]}),
    json.dumps({"version": 1, "revision": 0, "exclusions": ["../escape"]}),
])
def test_malformed_config_fails_closed(share, payload):
    (share / "exclusions.json").write_text(payload, encoding="utf-8")
    with pytest.raises(bridge.BridgeError):
        bridge.read_exclusions()


def test_missing_config_fails_closed(share):
    (share / "exclusions.json").unlink()
    with pytest.raises(bridge.BridgeError):
        bridge.read_exclusions()


def test_a_bom_is_tolerated(share):
    (share / "exclusions.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"version": 1, "revision": 3, "exclusions": []}).encode())
    assert bridge.read_exclusions()["revision"] == 3


def test_oversized_config_is_rejected(share, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_CONFIG_BYTES", 32)
    (share / "exclusions.json").write_text(
        json.dumps({"version": 1, "revision": 0, "exclusions": ["x" * 100]}), encoding="utf-8")
    with pytest.raises(bridge.BridgeError):
        bridge.read_exclusions()


# ------------------------------------------------------ writing the config --

def test_write_is_atomic_and_leaves_no_temp_files(share):
    bridge.write_exclusions(["Docs"], expect_revision=0)
    leftovers = [p.name for p in share.iterdir() if p.name != "exclusions.json" and p.is_file()]
    assert leftovers == []


def test_write_detects_a_concurrent_edit(share):
    (share / "exclusions.json").write_text(
        json.dumps({"version": 1, "revision": 9, "exclusions": []}), encoding="utf-8")
    with pytest.raises(bridge.RevisionConflict):
        bridge.write_exclusions(["Docs"], expect_revision=0)
    # The external edit survived untouched.
    assert bridge.read_exclusions()["revision"] == 9


def test_revision_never_resets_after_a_gui_restart(share):
    # A fresh GUI has no memory, but status.appliedRevision does.
    revision = bridge.write_exclusions(["Docs"], expect_revision=0, applied_revision=41)
    assert revision == 42


def test_next_revision_ignores_junk():
    assert bridge.next_revision(None, "5", -1, True, 7) == 8
    assert bridge.next_revision() == 0
    assert bridge.next_revision(None) == 0


def test_write_refuses_an_invalid_path(share):
    with pytest.raises(ValueError):
        bridge.write_exclusions(["../escape"], expect_revision=0)


# ------------------------------------------------- request/response dance ---

def test_request_and_response_round_trip(share):
    request_id = bridge.request_listing("Docs", offset=0, limit=100)
    request_path = share / "requests" / f"list-{request_id}.json"
    assert json.loads(request_path.read_text()) == {"path": "Docs", "offset": 0, "limit": 100}
    assert bridge.poll_response(request_id) is None

    # The agent answers and removes the request.
    request_path.unlink()
    response = {"path": "Docs", "error": None, "offset": 0, "nextOffset": None,
                "files": [{"name": "notes.txt", "path": "Docs/notes.txt",
                           "logicalBytes": 1024, "excluded": False, "dataless": False}]}
    (share / "responses" / f"list-{request_id}.json").write_text(json.dumps(response), encoding="utf-8")

    got = bridge.poll_response(request_id)
    assert got == response
    # Consumed exactly once: the GUI deletes the response after reading it.
    assert bridge.poll_response(request_id) is None


def test_request_id_shape_is_enforced(share):
    request_id = bridge.request_listing("", offset=0, limit=1)
    assert len(request_id) == 32 and all(c in "0123456789abcdef" for c in request_id)
    with pytest.raises(ValueError):
        bridge.poll_response("../../etc/passwd")
    with pytest.raises(ValueError):
        bridge.poll_response("ZZZZ")


def test_request_rejects_bad_paging(share):
    for offset, limit in ((-1, 10), (0, 0), (0, 1001)):
        with pytest.raises(ValueError):
            bridge.request_listing("Docs", offset=offset, limit=limit)


def test_request_rejects_a_path_outside_the_sync_root(share):
    with pytest.raises(ValueError):
        bridge.request_listing("../etc")


def test_malformed_response_is_reported_and_consumed(share):
    request_id = bridge.request_listing("Docs")
    (share / "responses" / f"list-{request_id}.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(bridge.BridgeError):
        bridge.poll_response(request_id)
    # A poisoned response must not stay behind and re-raise forever.
    assert not (share / "responses" / f"list-{request_id}.json").exists()


def test_cancel_request_removes_both_sides(share):
    request_id = bridge.request_listing("Docs")
    (share / "responses" / f"list-{request_id}.json").write_text("{}", encoding="utf-8")
    bridge.cancel_request(request_id)
    assert not (share / "requests" / f"list-{request_id}.json").exists()
    assert not (share / "responses" / f"list-{request_id}.json").exists()


# ------------------------------------------------------------- status/tree --

def test_read_status_and_tree(share):
    (share / "status.json").write_text(json.dumps({"version": 1, "generatedAt": "x"}), encoding="utf-8")
    (share / "tree.json").write_text(json.dumps({"version": 1, "root": {"dirs": []}}), encoding="utf-8")
    assert bridge.read_status()["version"] == 1
    assert bridge.read_tree()["root"] == {"dirs": []}


def test_read_status_missing_is_a_bridge_error(share):
    with pytest.raises(bridge.BridgeError):
        bridge.read_status()


def test_read_status_non_utf8_is_a_bridge_error(share):
    # A UTF-16/garbage status file must surface as BridgeError, not a raw
    # UnicodeDecodeError -- health.gather() only catches BridgeError.
    (share / "status.json").write_bytes(b"\xff\xfe{\x00}\x00")
    with pytest.raises(bridge.BridgeError):
        bridge.read_status()


def test_bridge_dir_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ICLOUD_BRIDGE_DIR", str(tmp_path))
    assert bridge.bridge_dir() == str(tmp_path)
    monkeypatch.delenv("ICLOUD_BRIDGE_DIR")
    assert bridge.bridge_dir() == bridge.DEFAULT_BRIDGE_DIR
    monkeypatch.setenv("ICLOUD_MOUNT_DIR", "/tmp/fake-icloud")
    assert bridge.mount_dir() == "/tmp/fake-icloud"
