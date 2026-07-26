"""Health-model tests: pure severity mapping, no docker/mount/Qt required."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import health  # noqa: E402

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def stamp(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def good_status(**overrides) -> dict:
    status = {
        "version": 1,
        "generatedAt": stamp(5),
        "syncRoot": "C:/Users/icloud/iCloudDrive",
        "icloudClientRunning": True,
        "diskFreeBytes": 51_200_000_000,
        "diskTotalBytes": 128_000_000_000,
        "appliedRevision": 7,
        "lastError": None,
        "exclusions": [{"path": "Big Folder", "state": "applied", "detail": ""}],
        "fullyLocalLogicalBytes": 3_221_225_472,
        "scan": {"lastCompletedAt": stamp(60), "durationMs": 1840, "entries": 103421, "cloudInfoQueries": 37},
        "sweep": {"lastRunAt": stamp(30), "requestedBytes": 0, "freedBytes": 0,
                  "blockedBytes": 0, "blockedCount": 0, "inProgress": False, "belowFloor": False},
    }
    status.update(overrides)
    return status


def good_tree(**overrides) -> dict:
    tree = {"version": 1, "generatedAt": stamp(120), "root": {"dirs": []}}
    tree.update(overrides)
    return tree


def checks(**overrides):
    kwargs = dict(
        container_running=True,
        icloud_mounted=True,
        bridge_mounted=True,
        canary_exists=True,
        canary_mtime=(NOW - timedelta(seconds=30)).timestamp(),
        status=good_status(),
        tree=good_tree(),
        now=NOW,
    )
    kwargs.update(overrides)
    return health.build_checks(**kwargs)


def severity_of(rows, name):
    for row in rows:
        if row.name == name:
            return row.severity
    raise AssertionError(f"no check named {name!r} in {[r.name for r in rows]}")


# --------------------------------------------------------------- baseline ---

def test_everything_healthy_is_green():
    rows = checks()
    assert health.overall(rows) == health.GREEN
    assert all(row.severity == health.GREEN for row in rows)


# ------------------------------------------------------------- red sources ---

def test_container_down_is_red():
    rows = checks(container_running=False, container_detail="container not found")
    assert severity_of(rows, "Windows VM") == health.RED
    assert health.overall(rows) == health.RED


def test_docker_unavailable_is_red():
    rows = checks(container_running=None, container_detail="docker is not installed")
    assert severity_of(rows, "Windows VM") == health.RED


def test_either_mount_missing_is_red():
    assert severity_of(checks(icloud_mounted=False), "iCloud mount") == health.RED
    assert severity_of(checks(bridge_mounted=False), "Bridge mount") == health.RED


def test_missing_canary_is_red():
    rows = checks(canary_exists=False, canary_mtime=None)
    assert severity_of(rows, "Guest write canary") == health.RED


def test_canary_boundary_is_fifteen_minutes():
    just_inside = checks(canary_mtime=(NOW - timedelta(seconds=899)).timestamp())
    assert severity_of(just_inside, "Guest write canary") == health.GREEN
    just_outside = checks(canary_mtime=(NOW - timedelta(seconds=901)).timestamp())
    assert severity_of(just_outside, "Guest write canary") == health.RED


def test_future_dated_canary_is_a_red_clock_error():
    rows = checks(canary_mtime=(NOW + timedelta(seconds=600)).timestamp())
    assert severity_of(rows, "Guest write canary") == health.RED
    # Five minutes of skew is tolerated, not treated as a fault.
    rows = checks(canary_mtime=(NOW + timedelta(seconds=60)).timestamp())
    assert severity_of(rows, "Guest write canary") == health.GREEN


# ---------------------------------------------------------- yellow sources ---

def test_missing_status_is_yellow_not_red():
    rows = checks(status=None, status_error="status.json is unavailable")
    assert severity_of(rows, "Guest agent") == health.YELLOW
    assert health.overall(rows) == health.YELLOW


def test_stale_agent_timestamp_is_yellow():
    assert severity_of(checks(status=good_status(generatedAt=stamp(89))), "Guest agent") == health.GREEN
    assert severity_of(checks(status=good_status(generatedAt=stamp(91))), "Guest agent") == health.YELLOW


def test_invalid_and_future_agent_timestamps_are_yellow():
    for bad in ("", "not-a-date", "2026-07-23 12:00:00", None, 17):
        rows = checks(status=good_status(generatedAt=bad))
        assert severity_of(rows, "Guest agent") == health.YELLOW, bad
    future = (NOW + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert severity_of(checks(status=good_status(generatedAt=future)), "Guest agent") == health.YELLOW


def test_naive_timestamp_is_rejected():
    assert health.parse_utc("2026-07-23T12:00:00") is None
    assert health.parse_utc("2026-07-23T12:00:00Z") == NOW
    assert health.parse_utc("2026-07-23T14:00:00+02:00") == NOW


def test_last_error_is_yellow():
    rows = checks(status=good_status(lastError="exclusions.json unreadable"))
    assert severity_of(rows, "Guest agent") == health.YELLOW


def test_stale_tree_is_yellow_but_health_still_works():
    rows = checks(tree=good_tree(generatedAt=stamp(1201)))
    assert severity_of(rows, "Folder tree") == health.YELLOW
    assert severity_of(rows, "Guest agent") == health.GREEN
    rows = checks(tree=None, tree_error="tree.json is unavailable")
    assert severity_of(rows, "Folder tree") == health.YELLOW


def test_icloud_client_not_running_is_yellow():
    rows = checks(status=good_status(icloudClientRunning=False))
    assert severity_of(rows, "iCloud client") == health.YELLOW


def test_pending_exclusion_states_are_yellow():
    for state in ("applying", "pending-dehydrate", "not-found", "error"):
        rows = checks(status=good_status(exclusions=[{"path": "X", "state": state, "detail": ""}]))
        assert severity_of(rows, "Exclusions") == health.YELLOW, state
    rows = checks(status=good_status(exclusions=[{"path": "X", "state": "applied", "detail": ""}]))
    assert severity_of(rows, "Exclusions") == health.GREEN


def test_not_found_detail_explains_why_it_is_not_healthy():
    rows = checks(status=good_status(exclusions=[{"path": "X", "state": "not-found", "detail": ""}]))
    for row in rows:
        if row.name == "Exclusions":
            assert "not-found" in row.detail


def test_revision_lag_is_yellow_only_after_the_grace_period():
    fresh = checks(status=good_status(appliedRevision=7), last_written_revision=8,
                   last_write_at=NOW - timedelta(seconds=60))
    assert severity_of(fresh, "Exclusions") == health.GREEN
    stale = checks(status=good_status(appliedRevision=7), last_written_revision=8,
                   last_write_at=NOW - timedelta(seconds=400))
    assert severity_of(stale, "Exclusions") == health.YELLOW


def test_sweep_states_are_yellow():
    sweeping = good_status()
    sweeping["sweep"] = dict(sweeping["sweep"], inProgress=True)
    assert severity_of(checks(status=sweeping), "Guest disk") == health.YELLOW

    stuck = good_status()
    stuck["sweep"] = dict(stuck["sweep"], belowFloor=True, blockedCount=3)
    row = [r for r in checks(status=stuck) if r.name == "Guest disk"][0]
    assert row.severity == health.YELLOW
    assert "nothing is eligible" in row.detail

    assert severity_of(checks(status=good_status()), "Guest disk") == health.GREEN


# ------------------------------------------------------------- precedence ---

def test_red_beats_yellow():
    rows = checks(container_running=False, status=None, tree=None)
    assert health.overall(rows) == health.RED


def test_overall_of_empty_is_green():
    assert health.overall([]) == health.GREEN


def test_human_bytes():
    assert health.human_bytes(0) == "0 B"
    assert health.human_bytes(1536) == "1.5 KB"
    assert health.human_bytes(20 * 1024 ** 3) == "20.0 GB"
    assert health.human_bytes(None) == "-"
    assert health.human_bytes(-1) == "-"


# --------------------------------------------------- the docker adapter ------

def test_container_running_pins_the_native_socket(monkeypatch):
    """The container check must not follow a Docker Desktop context (item 3)."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///home/alice/.docker/desktop/docker.sock")
    monkeypatch.setenv("ICLOUD_TEST_SENTINEL", "kept")
    seen = {}

    class Completed:
        returncode = 0
        stdout = "true\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return Completed()

    monkeypatch.setattr(health.subprocess, "run", fake_run)
    assert health.container_running() == (True, "")
    assert seen["argv"][:2] == ["docker", "inspect"]
    assert seen["env"]["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    # A copy of the environment, not a replacement.
    assert seen["env"]["ICLOUD_TEST_SENTINEL"] == "kept"


# ----------------------------------------- polling caches (v2 plan D34) ------

class FakeStat:
    def __init__(self, mtime_ns: int, size: int) -> None:
        self.st_mtime_ns = mtime_ns
        self.st_size = size


class Stats:
    """A settable stat table plus a call counter, so no mount is needed."""

    def __init__(self) -> None:
        self.table: dict[str, FakeStat] = {}
        self.calls = 0

    def __call__(self, path: str) -> FakeStat:
        self.calls += 1
        try:
            return self.table[path]
        except KeyError:
            raise OSError(2, "No such file or directory", path) from None


def test_document_cache_reparses_only_when_the_signature_moves():
    stats = Stats()
    stats.table["/b/tree.json"] = FakeStat(100, 10)
    reads = []

    def reader():
        reads.append(1)
        return {"generatedAt": len(reads)}

    cache = health.DocumentCache(stat=stats)
    assert cache.read("/b/tree.json", reader) == {"generatedAt": 1}
    # Unchanged file: served from the cache, reader never runs again.
    for _ in range(5):
        assert cache.read("/b/tree.json", reader) == {"generatedAt": 1}
    assert len(reads) == 1

    stats.table["/b/tree.json"] = FakeStat(200, 10)      # same size, new mtime
    assert cache.read("/b/tree.json", reader) == {"generatedAt": 2}
    stats.table["/b/tree.json"] = FakeStat(200, 11)      # same mtime, new size
    assert cache.read("/b/tree.json", reader) == {"generatedAt": 3}
    assert len(reads) == 3


def test_document_cache_does_not_store_a_document_rewritten_mid_read():
    stats = Stats()
    stats.table["/b/status.json"] = FakeStat(100, 10)

    def reader():
        # Simulate the agent replacing the file while we were reading it.
        stats.table["/b/status.json"] = FakeStat(101, 12)
        return {"n": 1}

    cache = health.DocumentCache(stat=stats)
    assert cache.read("/b/status.json", reader) == {"n": 1}

    calls = []

    def reader2():
        calls.append(1)
        return {"n": 2}

    assert cache.read("/b/status.json", reader2) == {"n": 2}
    assert calls == [1]


def test_document_cache_drops_the_entry_when_a_read_fails():
    stats = Stats()
    stats.table["/b/status.json"] = FakeStat(100, 10)
    cache = health.DocumentCache(stat=stats)
    assert cache.read("/b/status.json", lambda: {"n": 1}) == {"n": 1}

    def boom():
        raise health.bridge.BridgeError("status.json is not valid JSON")

    # The reader only runs when the signature moved, so move it: the agent wrote
    # a document this end cannot parse.
    stats.table["/b/status.json"] = FakeStat(300, 40)
    try:
        cache.read("/b/status.json", boom)
    except health.bridge.BridgeError:
        pass
    else:                                                # pragma: no cover
        raise AssertionError("the reader error must propagate")
    # Back to the failing document's own signature: nothing stale may be served,
    # and the failure must be re-reported rather than papered over.
    calls = []

    def again():
        calls.append(1)
        raise health.bridge.BridgeError("still bad")

    for _ in range(2):
        try:
            cache.read("/b/status.json", again)
        except health.bridge.BridgeError:
            pass
    assert calls == [1, 1]


def test_document_cache_missing_file_always_calls_the_reader():
    stats = Stats()
    cache = health.DocumentCache(stat=stats)
    calls = []

    def boom():
        calls.append(1)
        raise health.bridge.BridgeError("cannot stat")

    for _ in range(3):
        try:
            cache.read("/b/gone.json", boom)
        except health.bridge.BridgeError:
            pass
    assert calls == [1, 1, 1]


def test_container_probe_rate_limits_and_invalidates():
    now = {"t": 1000.0}
    calls = []

    def probe():
        calls.append(1)
        return (True, "")

    p = health.ContainerProbe(interval=15, clock=lambda: now["t"], probe=probe)
    assert p.read() == (True, "")
    now["t"] += 5
    assert p.read() == (True, "")
    now["t"] += 5
    p.read()
    assert len(calls) == 1                    # still inside the window

    now["t"] += 6                             # 16 s since the last real check
    p.read()
    assert len(calls) == 2

    p.invalidate()                            # a power action or Refresh
    p.read()
    assert len(calls) == 3


def test_gather_without_caches_probes_every_time(monkeypatch, tmp_path):
    """The default (no caches passed) keeps the old always-fresh behaviour."""
    monkeypatch.setenv("ICLOUD_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setenv("ICLOUD_MOUNT_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(health, "container_running", lambda: (calls.append(1), (True, ""))[1])
    health.gather()
    health.gather()
    assert len(calls) == 2
