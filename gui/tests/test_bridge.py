"""Bridge-share tests against a temporary directory standing in for the mount."""

from __future__ import annotations

import json
import os
import re
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
    response = {"version": 1, "path": "Docs", "error": None, "offset": 0,
                "nextOffset": None,
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


# ------------------------------------- protocol and agent-build skew (D35) ---

def write_status(share, **overrides):
    doc = {"version": 1, "agentBuild": bridge.AGENT_BUILD, "generatedAt": "2026-07-26T00:00:00Z"}
    doc.update(overrides)
    (share / "status.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc


def write_tree(share, **overrides):
    doc = {"version": 1, "generatedAt": "2026-07-26T00:00:00Z", "root": {"dirs": []}}
    doc.update(overrides)
    (share / "tree.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc


def test_the_bundled_agent_build_matches_the_powershell_literal():
    """The two ends carry the same number, or skew detection is a lie.

    Compared against the source of truth; `make lint` separately proves
    `provision/agent.ps1` is byte-identical to it.
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    text = open(os.path.join(repo, "guest-agent", "agent.ps1"), encoding="utf-8").read()
    builds = re.findall(r"^\$AgentBuild\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    versions = re.findall(r"^\$ProtocolVersion\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    assert builds == [str(bridge.AGENT_BUILD)]
    assert versions == [str(bridge.PROTOCOL_VERSION)]


@pytest.mark.parametrize("version", [None, "1", 1.0, True, 0, 2, 99, [1], {"v": 1}])
def test_status_with_an_unsupported_version_is_a_protocol_error(share, version):
    doc = {"agentBuild": bridge.AGENT_BUILD}
    if version is not None:
        doc["version"] = version
    (share / "status.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(bridge.ProtocolError):
        bridge.read_status()


@pytest.mark.parametrize("version", [None, "1", True, 2])
def test_tree_with_an_unsupported_version_is_a_protocol_error(share, version):
    doc = {"root": {"dirs": []}}
    if version is not None:
        doc["version"] = version
    (share / "tree.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(bridge.ProtocolError):
        bridge.read_tree()


@pytest.mark.parametrize("version", [None, "1", True, 2])
def test_list_response_with_an_unsupported_version_is_a_protocol_error(share, version):
    request_id = bridge.request_listing("Docs")
    doc = {"path": "Docs", "error": None, "offset": 0, "nextOffset": None, "files": []}
    if version is not None:
        doc["version"] = version
    (share / "responses" / f"list-{request_id}.json").write_text(
        json.dumps(doc), encoding="utf-8")
    with pytest.raises(bridge.ProtocolError):
        bridge.poll_response(request_id)


def test_a_protocol_error_is_also_a_bridge_error(share):
    """So no existing `except BridgeError` path loses its handling."""
    (share / "status.json").write_text(json.dumps({"version": 2}), encoding="utf-8")
    with pytest.raises(bridge.BridgeError):
        bridge.read_status()


def test_a_matching_build_is_current(share):
    doc = write_status(share)
    assert bridge.read_status() == doc
    compat = bridge.classify_agent_build(doc)
    assert compat.state == bridge.COMPAT_CURRENT
    assert compat.agent_build == bridge.AGENT_BUILD
    assert compat.writable


@pytest.mark.parametrize("build", [0, bridge.AGENT_BUILD + 1, bridge.AGENT_BUILD + 99])
def test_any_other_build_is_skewed_in_both_directions(build):
    """A newer agent is treated exactly like an older one (hard rule 9)."""
    compat = bridge.classify_agent_build({"version": 1, "agentBuild": build})
    assert compat.state == bridge.COMPAT_SKEWED
    assert compat.agent_build == build
    # Skew still works: the protocol matched, so writes stay allowed.
    assert compat.writable


@pytest.mark.parametrize("build", [None, "1", True, -1, 1.5, [], {}])
def test_a_missing_or_malformed_build_is_skewed(build):
    doc = {"version": 1}
    if build is not None:
        doc["agentBuild"] = build
    compat = bridge.classify_agent_build(doc)
    assert compat.state == bridge.COMPAT_SKEWED
    assert compat.agent_build is None
    assert compat.writable


def test_an_absent_status_leaves_compatibility_unknown_and_closed():
    compat = bridge.classify_compatibility(None)
    assert compat.state == bridge.COMPAT_UNKNOWN
    assert not compat.writable


def test_an_unsupported_status_version_closes_the_gate():
    compat = bridge.classify_compatibility(
        None, status_protocol_error="status.json reports version 2")
    assert compat.state == bridge.COMPAT_INCOMPATIBLE
    assert not compat.writable
    assert "version 2" in compat.detail


def test_an_unsupported_tree_version_closes_the_gate_too():
    compat = bridge.classify_compatibility(
        {"version": 1, "agentBuild": bridge.AGENT_BUILD},
        tree_protocol_error="tree.json reports version 7")
    assert compat.state == bridge.COMPAT_INCOMPATIBLE
    assert not compat.writable


def test_a_merely_missing_tree_does_not_override_a_good_status():
    """Browsing is unavailable, but that is not a reason to refuse writes."""
    compat = bridge.classify_compatibility(
        {"version": 1, "agentBuild": bridge.AGENT_BUILD})
    assert compat.state == bridge.COMPAT_CURRENT
    assert compat.writable


def test_the_recovery_instruction_names_script_04():
    assert "04-bridge-agent.ps1" in bridge.UPDATE_AGENT_INSTRUCTION
    assert "04-bridge-agent.ps1" in bridge.SKEW_BANNER


def test_read_exclusions_still_uses_its_own_version_check(share):
    """This item does not change the exclusions format."""
    (share / "exclusions.json").write_text(
        json.dumps({"version": 2, "revision": 0, "exclusions": []}), encoding="utf-8")
    with pytest.raises(bridge.BridgeError):
        bridge.read_exclusions()


def test_minimum_revision_lifts_the_written_revision(share):
    """A restore of an old snapshot still writes something strictly newer."""
    revision = bridge.write_exclusions(["Docs"], expect_revision=0,
                                       applied_revision=2, last_written=3,
                                       minimum_revision=30)
    assert revision == 31
    assert bridge.read_exclusions()["revision"] == 31


def test_minimum_revision_never_lowers_the_result(share):
    (share / "exclusions.json").write_text(
        json.dumps({"version": 1, "revision": 50, "exclusions": []}), encoding="utf-8")
    revision = bridge.write_exclusions(["Docs"], expect_revision=50, minimum_revision=2)
    assert revision == 51
