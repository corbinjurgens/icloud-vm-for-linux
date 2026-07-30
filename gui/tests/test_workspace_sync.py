"""One Safe Workspace synchronization cycle: gating, snapshots, argv, results.

Two layers, both required by `docs/plan-safe-local-workspaces.md`:

* the unit-level cases run with an **injected runner** and no Unison binary at
  all, so the exact argv, the environment, the gating order, the stability rule
  and every refusal are assertable on any machine;
* the integration cases at the bottom drive the **real** `unison` between two
  temporary local directories — never `/mnt/icloud` — and prove seeding, both
  directions, a metadata-only touch, backup retention, deletion propagation and
  a divergent conflict. They skip, with a message naming what stays unverified,
  only where Unison 2.52 or newer is absent.

Nothing here imports Qt, Docker, sudo, or a real mount: the mount check, the
powered-off marker, the mountinfo classification and the clock are injected.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import power, workspace_sync, workspaces  # noqa: E402


VERSION_LINE = "unison version 2.53.8 (ocaml 5.3.0)\n"

#: One mountinfo line is enough: everything under it classifies as ext4, which
#: is what lets these tests run from a tmpfs-backed temporary directory.
MOUNTINFO = "36 35 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"


class Env:
    """The injected XDG/mount layout, marker and mountinfo one test runs against."""

    def __init__(self, tmp_path):
        self.root = str(tmp_path)
        self.home = str(tmp_path / "home" / "user")
        self.config = str(tmp_path / "config")
        self.state = str(tmp_path / "state")
        self.mount = str(tmp_path / "mnt" / "icloud")
        self.share = str(tmp_path / "mnt" / "icloud_bridge")
        self.remote = "Documents/Vault"
        self.remote_root = os.path.join(self.mount, "Documents", "Vault")
        self.local = str(tmp_path / "home" / "user" / "Workspaces" / "Vault")
        self.marker = str(tmp_path / "powered-off")
        self.mountinfo = str(tmp_path / "mountinfo")
        self.id = "9f14c0a7b3e25d68"


@pytest.fixture(autouse=True)
def forget_the_version_cache():
    """The version verdict is per process; no test may inherit another's."""
    workspace_sync.reset_version_cache()
    yield
    workspace_sync.reset_version_cache()


@pytest.fixture
def env(tmp_path, monkeypatch):
    layout = Env(tmp_path)
    os.makedirs(layout.home, exist_ok=True)
    os.makedirs(layout.remote_root, exist_ok=True)
    with open(layout.mountinfo, "w", encoding="utf-8") as handle:
        handle.write(MOUNTINFO)
    monkeypatch.setenv("HOME", layout.home)
    monkeypatch.setenv("XDG_CONFIG_HOME", layout.config)
    monkeypatch.setenv("XDG_STATE_HOME", layout.state)
    monkeypatch.setenv("ICLOUD_MOUNT_DIR", layout.mount)
    monkeypatch.setenv("ICLOUD_BRIDGE_DIR", layout.share)
    return layout


class FakeRunner:
    """Records every argv, answers ``-version`` itself, cans the sync result."""

    def __init__(self, result=None, *, raises=None, version=VERSION_LINE):
        self.result = power.RunResult(0) if result is None else result
        self.raises = raises
        self.version = version
        self.calls: list[tuple[list[str], float, dict]] = []

    def __call__(self, argv, timeout, env):
        self.calls.append((list(argv), timeout, dict(env)))
        if argv[1:] == ["-version"]:
            return power.RunResult(0, self.version, "")
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def syncs(self) -> list[tuple[list[str], float, dict]]:
        return [call for call in self.calls if call[0][1:] != ["-version"]]


def workspace(env, **overrides) -> workspaces.Workspace:
    fields = {"id": env.id, "name": "Vault", "remote": env.remote,
              "local": env.local, "enabled": True}
    fields.update(overrides)
    return workspaces.Workspace(**fields)


def explode(*args, **kwargs):
    raise AssertionError("the mount was probed when it must not have been")


def cycle(env, *, runner=None, item=None, **overrides):
    """Run one cycle against the fixture's injected environment."""
    options = {
        "runner": FakeRunner() if runner is None else runner,
        "marker_path": env.marker,
        "is_mount": lambda path: path == env.mount,
        "mountinfo_path": env.mountinfo,
    }
    options.update(overrides)
    return workspace_sync.run_cycle(
        workspace(env) if item is None else item, **options)


def until_engine(env, **kwargs):
    """Poll until a cycle does something other than wait for stability."""
    for _ in range(6):
        result = cycle(env, **kwargs)
        if result.outcome != workspace_sync.STABILIZING:
            return result
    raise AssertionError("the replicas never settled")


def write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def mode_of(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def status_of(env) -> dict:
    return workspace_sync.read_status(env.id)


# ---------------------------------------------------------- the invocation --

def test_the_argv_is_exactly_the_documented_command():
    assert workspace_sync.build_argv(
        "/home/user/Vault", "/mnt/icloud/Documents/Vault",
        backups="/state/backups", logfile="/state/sync.log") == [
        "unison", "/home/user/Vault", "/mnt/icloud/Documents/Vault",
        "-batch", "-auto",
        "-fastcheck", "false",
        "-times=true",
        "-perms", "0",
        "-dontchmod=true",
        "-owner=false",
        "-group=false",
        "-xattrs=false",
        "-acl=false",
        "-confirmbigdel=true",
        "-backup", "Name *",
        "-backupcurr", "Name *",
        "-backuploc", "central",
        "-backupdir", "/state/backups",
        "-maxbackups", "10",
        "-ignore", "Path .obsidian/workspace.json",
        "-ignore", "Path .obsidian/workspace-mobile.json",
        "-ignore", "Path .obsidian/cache",
        "-ignore", "Path .trash",
        "-logfile", "/state/sync.log",
        "-color", "false",
    ]


@pytest.mark.parametrize("option", workspace_sync.NEVER_PASSED)
def test_no_option_that_picks_a_winner_or_overrides_a_lock_is_ever_passed(option):
    argv = workspace_sync.build_argv("/a", "/b", backups="/c", logfile="/d")
    assert f"-{option}" not in argv
    assert not any(item.startswith(f"-{option}=") for item in argv)


def test_the_child_environment_is_a_copy_with_a_private_unison_directory():
    given = {"PATH": "/usr/bin", "HOME": "/home/user", "UNISON": "/home/user/.unison"}
    built = workspace_sync.build_env("/state/9f14/unison", given)
    assert built["UNISON"] == "/state/9f14/unison"
    assert built["NO_COLOR"] == "1"
    assert built["PATH"] == "/usr/bin" and built["HOME"] == "/home/user"
    # The caller's own profile directory must not survive into the child.
    assert workspace_sync.base_env(given).get("UNISON") is None


def test_the_cycle_hands_the_runner_that_argv_and_that_environment(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    assert cycle(env, runner=runner).outcome == workspace_sync.STABILIZING
    assert runner.syncs == []
    result = cycle(env, runner=runner)
    assert result.outcome == workspace_sync.SYNCHRONIZED
    argv, timeout, child_env = runner.syncs[0]
    assert argv == workspace_sync.build_argv(
        env.local, env.remote_root,
        backups=workspaces.backups_dir(env.id), logfile=workspaces.log_path(env.id))
    assert timeout == workspace_sync.TIMEOUT_SECONDS == 120
    assert child_env["UNISON"] == workspaces.unison_dir(env.id)
    assert child_env["NO_COLOR"] == "1"
    assert runner.calls[0][0] == ["unison", "-version"]


def test_the_default_runner_never_goes_through_a_shell_and_never_sits_in_a_replica():
    env = workspace_sync.base_env()
    literal = "$HOME && touch /tmp/should-not-exist; echo *"
    assert workspace_sync.default_runner(
        ["/bin/echo", literal], 30, env).stdout.strip() == literal
    assert workspace_sync.default_runner(
        ["/bin/pwd"], 30, env).stdout.strip() == workspace_sync.WORKING_DIRECTORY


def test_the_default_runner_reports_a_timeout_rather_than_hanging():
    with pytest.raises(TimeoutError):
        workspace_sync.default_runner(["/bin/sleep", "5"], 0.2,
                                      workspace_sync.base_env())


def test_the_default_runner_replaces_invalid_utf8_instead_of_raising():
    # A filename sliced mid-character by Unison's 80-column progress line
    # leaves orphaned continuation bytes (0xaa, 0x8b) in real captured output.
    result = workspace_sync.default_runner(
        [sys.executable, "-c",
         "import sys; sys.stdout.buffer.write(b'ab\\xaa\\x8bcd')"],
        30, workspace_sync.base_env())
    assert result.stdout == "ab��cd"


def test_the_default_runner_replaces_invalid_utf8_on_stderr_too():
    result = workspace_sync.default_runner(
        [sys.executable, "-c",
         "import sys\n"
         "sys.stdout.buffer.write(b'out\\xaa')\n"
         "sys.stderr.buffer.write(b'err\\x8b')\n"],
        30, workspace_sync.base_env())
    assert result.stdout == "out�"
    assert result.stderr == "err�"


# ------------------------------------------------------------ the gating order --

def test_a_paused_workspace_does_no_io_at_all(env):
    runner = FakeRunner()
    result = cycle(env, runner=runner, item=workspace(env, enabled=False),
                   is_mount=explode)
    assert (result.outcome, result.state) == (workspace_sync.PAUSED, "paused")
    assert runner.calls == []
    assert not os.path.exists(workspaces.state_dir(env.id))


def test_a_powered_off_bridge_short_circuits_before_any_mount_io(env):
    write(env.marker, "")
    runner = FakeRunner()
    # Both would raise if the cycle reached them: the mount probe explodes and
    # the remote root no longer exists.
    os.rmdir(env.remote_root)
    result = cycle(env, runner=runner, is_mount=explode)
    assert (result.outcome, result.state) == (workspace_sync.PAUSED, "paused")
    assert runner.calls == []
    assert not os.path.exists(workspaces.snapshot_path(env.id))
    assert status_of(env)["state"] == "paused"


def test_a_second_invocation_reports_already_running_and_performs_no_scan(env):
    # A FIFO in the remote root makes any scan raise, so a plain
    # `already-running` proves nothing was walked.
    workspaces.ensure_state_dir(env.id)
    os.mkfifo(os.path.join(env.remote_root, "pipe"))
    runner = FakeRunner()
    with workspace_sync.workspace_lock(workspaces.lock_path(env.id)) as held:
        assert held
        result = cycle(env, runner=runner)
    assert result.outcome == workspace_sync.ALREADY_RUNNING
    assert result.state == "syncing"
    assert runner.calls == []
    assert not os.path.exists(workspaces.snapshot_path(env.id))
    assert not os.path.exists(workspaces.status_path(env.id))


def test_an_absent_mount_is_waiting_and_not_an_error(env):
    result = cycle(env, is_mount=lambda path: False)
    assert result.outcome == workspace_sync.UNAVAILABLE
    assert result.state == "waiting"
    assert env.mount in result.detail


def test_a_missing_remote_directory_is_reported_and_never_created(env):
    os.rmdir(env.remote_root)
    result = cycle(env)
    assert result.outcome == workspace_sync.UNAVAILABLE
    assert not os.path.exists(env.remote_root)


def test_a_symlinked_remote_directory_is_refused(env, tmp_path):
    os.rmdir(env.remote_root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), env.remote_root)
    assert cycle(env).outcome == workspace_sync.UNAVAILABLE


def test_a_local_root_on_a_rejected_filesystem_stops_the_workspace(env):
    with open(env.mountinfo, "a", encoding="utf-8") as handle:
        handle.write(f"40 36 0:44 / {env.home} rw,relatime - tmpfs tmpfs rw\n")
    runner = FakeRunner()
    result = cycle(env, runner=runner)
    assert result.state == "error"
    assert "tmpfs" in result.detail
    assert runner.syncs == []


# ----------------------------------------------------------- version contract --

@pytest.mark.parametrize("text,expected", [
    ("unison version 2.53.8 (ocaml 5.3.0)", (2, 53, "2.53.8")),
    ("unison version 2.52.0", (2, 52, "2.52.0")),
    ("Unison version 3.0 (ocaml 5.3.0)", (3, 0, "3.0")),
])
def test_version_parsing(text, expected):
    assert workspace_sync.parse_version(text) == expected


@pytest.mark.parametrize("text", ["", "unison", "unison version two", "2.53.8"])
def test_unparseable_version_output(text):
    assert workspace_sync.parse_version(text) is None


def test_an_old_or_unreadable_unison_disables_cycles(env):
    for version in ("unison version 2.51.5\n", "no idea\n"):
        workspace_sync.reset_version_cache()
        runner = FakeRunner(version=version)
        result = cycle(env, runner=runner)
        assert result.state == "error"
        assert "2.52" in result.detail
        assert runner.syncs == []


def test_a_missing_unison_binary_is_an_actionable_error(env):
    def absent(argv, timeout, child_env):
        raise FileNotFoundError(argv[0])

    check = workspace_sync.check_version(absent)
    assert not check.ok
    assert "not installed" in check.detail


def test_the_version_is_probed_once_per_process(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    for _ in range(3):
        cycle(env, runner=runner)
    assert [call[0] for call in runner.calls].count(["unison", "-version"]) == 1


# ---------------------------------------------------------------- snapshots --

def test_a_snapshot_records_only_path_kind_size_and_mtime(tmp_path):
    root = str(tmp_path / "replica")
    write(os.path.join(root, "note.md"), "hello\n")
    os.makedirs(os.path.join(root, "sub"))
    write(os.path.join(root, "sub", "deep.md"), "deep\n")
    snapshot = workspace_sync.scan(root)
    assert [entry[:3] for entry in snapshot] == [
        ("note.md", "file", 6), ("sub", "dir", 0), ("sub/deep.md", "file", 5)]
    assert all(isinstance(entry[3], int) for entry in snapshot)


def test_a_ctime_only_change_leaves_the_fingerprint_identical(tmp_path):
    root = str(tmp_path / "replica")
    target = write(os.path.join(root, "note.md"), "hello\n")
    before = workspace_sync.scan(root)
    os.chmod(target, 0o600)
    os.chown(target, os.getuid(), os.getgid())
    assert workspace_sync.scan(root) == before


def test_the_ignored_paths_are_excluded_with_their_subtrees(tmp_path):
    root = str(tmp_path / "replica")
    write(os.path.join(root, ".obsidian", "workspace.json"), "{}")
    write(os.path.join(root, ".obsidian", "workspace-mobile.json"), "{}")
    write(os.path.join(root, ".obsidian", "cache", "deep", "blob"), "x")
    write(os.path.join(root, ".trash", "gone.md"), "x")
    write(os.path.join(root, ".obsidian", "plugins", "one", "main.js"), "x")
    write(os.path.join(root, "note.md"), "hello\n")
    assert [entry[0] for entry in workspace_sync.scan(root)] == [
        ".obsidian", ".obsidian/plugins", ".obsidian/plugins/one",
        ".obsidian/plugins/one/main.js", "note.md"]


@pytest.mark.parametrize("relative", [
    ".obsidian/cache", ".obsidian/cache/x", ".trash", ".trash/a/b",
    ".obsidian/workspace.json", ".obsidian/workspace-mobile.json",
])
def test_ignored_paths(relative):
    assert workspace_sync.is_ignored(relative)


@pytest.mark.parametrize("relative", [
    ".obsidian", ".obsidian/plugins/x", ".trashcan", "notes/.trash-like",
])
def test_paths_that_are_not_ignored(relative):
    assert not workspace_sync.is_ignored(relative)


def test_a_symlink_inside_a_replica_stops_the_workspace(tmp_path):
    root = str(tmp_path / "replica")
    write(os.path.join(root, "note.md"), "hello\n")
    os.symlink("/etc/passwd", os.path.join(root, "link.md"))
    with pytest.raises(workspaces.WorkspaceError) as excinfo:
        workspace_sync.scan(root)
    assert "link.md" in str(excinfo.value)


def test_a_fifo_or_a_socket_inside_a_replica_stops_the_workspace(tmp_path):
    root = str(tmp_path / "replica")
    os.makedirs(root)
    os.mkfifo(os.path.join(root, "pipe"))
    with pytest.raises(workspaces.WorkspaceError) as excinfo:
        workspace_sync.scan(root)
    assert "pipe" in str(excinfo.value)
    os.unlink(os.path.join(root, "pipe"))
    endpoint = socket.socket(socket.AF_UNIX)
    endpoint.bind(os.path.join(root, "sock"))
    try:
        with pytest.raises(workspaces.WorkspaceError) as excinfo:
            workspace_sync.scan(root)
    finally:
        endpoint.close()
    assert "sock" in str(excinfo.value)


def test_a_mount_point_crossing_stops_the_workspace(tmp_path, monkeypatch):
    root = str(tmp_path / "replica")
    write(os.path.join(root, "note.md"), "hello\n")
    real_lstat = os.lstat

    class Elsewhere:
        """The root's own stat, reporting a device its children are not on."""

        def __init__(self, info):
            self.st_mode = info.st_mode
            self.st_dev = info.st_dev + 1
            self.st_size = info.st_size
            self.st_mtime_ns = info.st_mtime_ns

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return Elsewhere(info) if os.fspath(path) == root else info

    monkeypatch.setattr(workspace_sync.os, "lstat", lstat)
    with pytest.raises(workspaces.WorkspaceError) as excinfo:
        workspace_sync.scan(root)
    assert "filesystem" in str(excinfo.value)


def test_the_snapshot_pair_is_persisted_after_a_changed_poll(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    assert cycle(env).outcome == workspace_sync.STABILIZING
    document = json.loads(read(workspaces.snapshot_path(env.id)))
    assert document["version"] == 1
    assert document["local"] == []
    assert document["remote"] == [["note.md", "file", 6,
                                   os.lstat(os.path.join(env.remote_root,
                                                         "note.md")).st_mtime_ns]]
    assert workspace_sync.read_snapshot(env.id) == (
        (), (("note.md", "file", 6, document["remote"][0][3]),))


def test_a_damaged_snapshot_only_costs_one_more_settling_interval(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    cycle(env)
    write(workspaces.snapshot_path(env.id), "{not json")
    assert workspace_sync.read_snapshot(env.id) is None
    runner = FakeRunner()
    assert cycle(env, runner=runner).outcome == workspace_sync.STABILIZING
    assert runner.syncs == []


# ---------------------------------------------------------------- stability --

def test_unison_runs_only_after_two_identical_observations(env):
    note = write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    assert cycle(env, runner=runner).outcome == workspace_sync.STABILIZING
    write(note, "hello again\n")
    assert cycle(env, runner=runner).outcome == workspace_sync.STABILIZING
    assert runner.syncs == []
    assert cycle(env, runner=runner).outcome == workspace_sync.SYNCHRONIZED
    assert len(runner.syncs) == 1


def test_a_metadata_only_change_does_not_restart_the_settling_window(env):
    note = write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    assert cycle(env, runner=runner).outcome == workspace_sync.STABILIZING
    os.chmod(note, 0o600)
    result = cycle(env, runner=runner)
    assert result.outcome == workspace_sync.SYNCHRONIZED
    assert len(runner.syncs) == 1


def test_a_stabilizing_cycle_records_its_state(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    result = cycle(env)
    assert result.state == "stabilizing"
    assert status_of(env)["state"] == "stabilizing"
    assert status_of(env)["counts"]["remotePaths"] == 1


# ----------------------------------------------------------- first-run rules --

def test_an_empty_icloud_folder_is_refused_before_anything_is_created(env):
    runner = FakeRunner()
    result = cycle(env, runner=runner)
    assert result.state == "error"
    assert "empty" in result.detail
    assert not os.path.exists(env.local)
    assert runner.syncs == []


def test_content_on_both_sides_at_first_run_names_both_counts(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    write(os.path.join(env.remote_root, "other.md"), "hello\n")
    write(os.path.join(env.local, "mine.md"), "hello\n")
    result = cycle(env)
    assert result.state == "error"
    assert "1 item(s)" in result.detail and "holds 2" in result.detail
    assert read(os.path.join(env.local, "mine.md")) == "hello\n"


def test_a_first_run_refuses_without_the_free_space_margin(env, monkeypatch):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    monkeypatch.setattr(workspace_sync, "free_bytes", lambda path: 1024)
    runner = FakeRunner()
    result = cycle(env, runner=runner)
    assert result.state == "error"
    assert "free" in result.detail
    assert not os.path.exists(env.local)
    assert runner.syncs == []


def test_the_local_root_is_created_0700_only_once_everything_has_passed(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    assert cycle(env, runner=runner).outcome == workspace_sync.STABILIZING
    assert not os.path.exists(env.local)
    assert cycle(env, runner=runner).outcome == workspace_sync.SYNCHRONIZED
    assert mode_of(env.local) == 0o700
    assert len(runner.syncs) == 1


def test_the_first_run_rules_stop_applying_once_a_baseline_exists(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    cycle(env, runner=runner)
    assert cycle(env, runner=runner).outcome == workspace_sync.SYNCHRONIZED
    # Content on both sides is only ambiguous at first run; afterwards it is
    # the ordinary case, and the occupancy rule must not fire again.
    write(os.path.join(env.local, "mine.md"), "local\n")
    assert until_engine(env, runner=runner).outcome == workspace_sync.SYNCHRONIZED


def test_an_emptied_icloud_folder_after_the_first_run_is_guarded_not_seeded(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    cycle(env, runner=runner)
    assert cycle(env, runner=runner).outcome == workspace_sync.SYNCHRONIZED
    os.unlink(os.path.join(env.remote_root, "note.md"))
    assert until_engine(env, runner=runner).outcome == workspace_sync.GUARDED
    assert len(runner.syncs) == 1


# -------------------------------------------------------- destructive guard --

@pytest.mark.parametrize("baseline,current,tripped", [
    (10, 0, True),                       # emptied, whatever the proportion
    (100, 81, False),                    # 19 missing: under the count floor
    (100, 80, True),                     # 20 missing and exactly 20 percent
    (200, 175, False),                   # 25 missing but only 12.5 percent
    (200, 160, True),                    # 40 missing and 20 percent
    (0, 0, False),                       # an empty baseline never trips
])
def test_the_guard_thresholds(baseline, current, tripped):
    known = [f"note-{index}.md" for index in range(baseline)]
    verdict = workspace_sync.evaluate_guard(known, known[:current], "local")
    assert verdict.tripped is tripped
    assert verdict.missing == baseline - current
    assert len(verdict.examples) <= 20


def seed_baseline(env, local, remote):
    workspaces.ensure_state_dir(env.id)
    workspaces.write_json_atomic(workspaces.baseline_path(env.id), {
        "version": 1, "recordedAt": "2026-07-29T00:00:00Z",
        "local": sorted(local), "remote": sorted(remote)})


def test_a_guarded_cycle_never_invokes_unison_and_changes_nothing(env):
    names = [f"note-{index}.md" for index in range(40)]
    for name in names:
        write(os.path.join(env.local, name), "x")
    write(os.path.join(env.remote_root, names[0]), "x")
    seed_baseline(env, names, names)
    runner = FakeRunner()
    cycle(env, runner=runner)
    result = cycle(env, runner=runner)
    assert (result.outcome, result.state) == ("guarded", "guarded")
    assert result.missing_from_baseline == 39
    assert len(result.paths) == 20
    assert runner.syncs == []
    assert len(os.listdir(env.local)) == 40
    assert json.loads(read(workspaces.baseline_path(env.id)))["remote"] == sorted(names)
    assert status_of(env)["state"] == "guarded"


def test_an_endpoint_that_came_back_clears_the_guard(env):
    names = [f"note-{index}.md" for index in range(40)]
    for name in names:
        write(os.path.join(env.local, name), "x")
    seed_baseline(env, names, names)
    runner = FakeRunner()
    cycle(env, runner=runner)
    assert cycle(env, runner=runner).outcome == workspace_sync.GUARDED
    for name in names:
        write(os.path.join(env.remote_root, name), "x")
    assert until_engine(env, runner=runner).outcome == workspace_sync.SYNCHRONIZED


# ------------------------------------------------------- exit classification --

@pytest.mark.parametrize("code,outcome,state", [
    (0, workspace_sync.SYNCHRONIZED, "up-to-date"),
    (1, workspace_sync.CONFLICT, "conflict"),
    (2, workspace_sync.FAILED, "error"),
    (3, workspace_sync.FATAL, "error"),
    (9, workspace_sync.FAILED, "error"),
    (-15, workspace_sync.FAILED, "error"),
])
def test_every_exit_code_is_classified(env, code, outcome, state):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner(power.RunResult(code, "  skipped: note.md (reason)\n", ""))
    cycle(env, runner=runner)
    result = cycle(env, runner=runner)
    assert (result.outcome, result.state, result.exit_code) == (outcome, state, code)


def test_only_a_clean_exit_advances_the_baseline_and_the_success_stamp(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    cycle(env, runner=runner)
    good = cycle(env, runner=runner)
    assert good.last_success_at
    baseline = read(workspaces.baseline_path(env.id))

    write(os.path.join(env.remote_root, "second.md"), "hello\n")
    failing = FakeRunner(power.RunResult(2, "", "it did not work"))
    cycle(env, runner=failing)
    bad = cycle(env, runner=failing)
    assert bad.state == "error"
    assert bad.last_success_at == good.last_success_at
    assert read(workspaces.baseline_path(env.id)) == baseline
    assert status_of(env)["lastSuccessAt"] == good.last_success_at


def test_a_conflict_names_the_paths_and_keeps_both_sides(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    output = ("changed  <-?-> changed    note.md\n"
              "0 items will be synced, 1 skipped\n"
              "  skipped: note.md (contents changed on both sides)\n")
    runner = FakeRunner(power.RunResult(1, output, ""))
    cycle(env, runner=runner)
    result = cycle(env, runner=runner)
    assert result.state == "conflict"
    assert result.paths == ("note.md",)
    assert result.conflicts == 1
    assert status_of(env)["counts"]["conflicts"] == 1
    assert not os.path.exists(workspaces.baseline_path(env.id))


def test_a_timeout_is_its_own_outcome_and_leaves_the_baseline_alone(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner(raises=TimeoutError("unison timed out after 120s"))
    cycle(env, runner=runner)
    result = cycle(env, runner=runner)
    assert (result.outcome, result.state) == (workspace_sync.TIMEOUT, "error")
    assert "120" in result.detail
    assert not os.path.exists(workspaces.baseline_path(env.id))


def test_an_absent_unison_at_run_time_is_reported_not_raised(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner(raises=FileNotFoundError())
    cycle(env, runner=runner)
    assert cycle(env, runner=runner).state == "error"


# ------------------------------------------------------------ bounded output --

def test_the_detail_and_paths_are_bounded_sanitized_and_single_line(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    noisy = "".join(f"\x1b[31mskipped: {'d' * 400}/note-{index}.md (why)\x00\n"
                    for index in range(200))
    runner = FakeRunner(power.RunResult(1, noisy, "x" * 50_000))
    cycle(env, runner=runner)
    result = cycle(env, runner=runner)
    assert len(result.detail) <= workspace_sync.MAX_DETAIL_CHARS
    assert "\n" not in result.detail and "\x00" not in result.detail
    assert "\x1b" not in result.detail
    assert len(result.paths) == 20
    assert all(len(path) <= 200 for path in result.paths)
    document = status_of(env)
    assert len(document["detail"]) <= workspace_sync.MAX_DETAIL_CHARS
    assert len(document["paths"]) == 20


def test_the_log_is_truncated_once_it_passes_a_megabyte(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    cycle(env, runner=runner)
    workspaces.ensure_state_dir(env.id)
    write(workspaces.log_path(env.id), "x" * (workspace_sync.MAX_LOG_BYTES + 1))
    cycle(env, runner=runner)
    assert os.path.getsize(workspaces.log_path(env.id)) == 0


# ------------------------------------------------------------- status writes --

def test_the_status_document_is_the_documented_shape(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    runner = FakeRunner()
    cycle(env, runner=runner)
    result = cycle(env, runner=runner)
    document = status_of(env)
    assert sorted(document) == ["counts", "detail", "lastExit", "lastSuccessAt",
                                "paths", "state", "updatedAt", "version"]
    assert document["version"] == 1
    assert document["state"] == "up-to-date"
    assert document["lastExit"] == 0
    assert document["updatedAt"] == result.updated_at
    assert document["updatedAt"].endswith("Z")
    assert sorted(document["counts"]) == ["conflicts", "localPaths",
                                          "missingFromBaseline", "remotePaths"]
    assert document["counts"]["remotePaths"] == 1
    assert document["paths"] == [] and document["detail"] == ""


def test_the_status_file_is_a_0600_file_written_atomically(env):
    write(os.path.join(env.remote_root, "note.md"), "hello\n")
    cycle(env)
    path = workspaces.status_path(env.id)
    assert mode_of(path) == 0o600
    assert mode_of(os.path.dirname(path)) == 0o700
    leftovers = [name for name in os.listdir(os.path.dirname(path))
                 if name.startswith(".") and name.endswith(".tmp")]
    assert leftovers == []


def test_an_unchanged_status_is_not_rewritten_every_five_seconds(env):
    write(env.marker, "")
    first = cycle(env, is_mount=explode)
    assert first.wrote_status
    before = os.lstat(workspaces.status_path(env.id)).st_ino
    second = cycle(env, is_mount=explode)
    assert not second.wrote_status
    assert os.lstat(workspaces.status_path(env.id)).st_ino == before
    assert status_of(env)["state"] == "paused"


def test_an_unusable_state_directory_is_reported_rather_than_raised(env):
    write(workspaces.state_dir(env.id), "not a directory")
    result = cycle(env)
    assert (result.state, result.wrote_status) == ("error", False)
    assert workspaces.state_dir(env.id) in result.detail


def test_a_status_that_cannot_be_written_is_reported_rather_than_raised(
        env, monkeypatch):
    def full(path, document):
        raise workspaces.WorkspaceError("no space left on device")

    monkeypatch.setattr(workspaces, "write_json_atomic", full)
    write(env.marker, "")
    result = cycle(env, is_mount=explode)
    assert result.state == "error"
    assert "no space left" in result.detail


def test_a_clock_can_be_injected_for_a_deterministic_stamp(env):
    from datetime import datetime, timezone
    moment = datetime(2026, 7, 29, 9, 41, 2, tzinfo=timezone.utc)
    write(env.marker, "")
    result = cycle(env, is_mount=explode, now=lambda: moment)
    assert result.updated_at == "2026-07-29T09:41:02Z"
    assert status_of(env)["updatedAt"] == "2026-07-29T09:41:02Z"


# --------------------------------------------------------------- integration --
# The real engine, between two ordinary temporary directories. `/mnt/icloud` is
# never touched: the fixture's "mount" is a directory under `tmp_path` and the
# mount probe is injected.

def _installed_version():
    try:
        completed = subprocess.run([workspace_sync.UNISON_BIN, "-version"],
                                   capture_output=True, text=True, timeout=30,
                                   check=False)
    except (OSError, subprocess.SubprocessError):       # pragma: no cover
        return None
    return workspace_sync.parse_version(completed.stdout + completed.stderr)

_VERSION = _installed_version()

requires_unison = pytest.mark.skipif(
    _VERSION is None or _VERSION[:2] < workspace_sync.MIN_VERSION,
    reason=("unison 2.52 or newer is not installed, so the end-to-end cases are "
            "unverified here: initial seeding, local-to-remote and "
            "remote-to-local propagation, a metadata-only touch that must not "
            "replace content, ten retained central backups, deletion "
            "propagation, and a divergent conflict that must leave both "
            "versions intact."))


def sync(env):
    """Poll the real engine until it acts, and return that cycle's result."""
    return until_engine(env, runner=workspace_sync.default_runner)


def seed(env):
    write(os.path.join(env.remote_root, "note.md"), "first\n")
    write(os.path.join(env.remote_root, "sub", "deep.md"), "deep\n")
    write(os.path.join(env.remote_root, ".obsidian", "workspace.json"), "{}")
    write(os.path.join(env.remote_root, ".obsidian", "plugins", "p", "main.js"), "x")
    return sync(env)


@requires_unison
def test_live_initial_seeding(env):
    result = seed(env)
    assert (result.outcome, result.state) == ("synchronized", "up-to-date")
    assert result.exit_code == 0
    assert read(os.path.join(env.local, "note.md")) == "first\n"
    assert read(os.path.join(env.local, "sub", "deep.md")) == "deep\n"
    # Plugins are synchronized; per-device UI state is not.
    assert os.path.exists(os.path.join(env.local, ".obsidian", "plugins", "p",
                                       "main.js"))
    assert not os.path.exists(os.path.join(env.local, ".obsidian",
                                           "workspace.json"))
    assert mode_of(env.local) == 0o700
    assert status_of(env)["state"] == "up-to-date"
    assert status_of(env)["lastSuccessAt"] == result.updated_at
    assert os.path.exists(workspaces.baseline_path(env.id))
    assert os.path.getsize(workspaces.log_path(env.id)) > 0


@requires_unison
def test_live_edits_propagate_in_both_directions(env):
    seed(env)
    write(os.path.join(env.local, "note.md"), "edited on linux\n")
    write(os.path.join(env.local, "brand-new.md"), "made locally\n")
    assert sync(env).outcome == "synchronized"
    assert read(os.path.join(env.remote_root, "note.md")) == "edited on linux\n"
    assert read(os.path.join(env.remote_root, "brand-new.md")) == "made locally\n"

    write(os.path.join(env.remote_root, "note.md"), "edited on the mac\n")
    write(os.path.join(env.remote_root, "inbound.md"), "from icloud\n")
    assert sync(env).outcome == "synchronized"
    assert read(os.path.join(env.local, "note.md")) == "edited on the mac\n"
    assert read(os.path.join(env.local, "inbound.md")) == "from icloud\n"


@requires_unison
def test_live_a_metadata_only_touch_does_not_replace_content(env):
    seed(env)
    remote = os.path.join(env.remote_root, "note.md")
    local = os.path.join(env.local, "note.md")
    before = os.lstat(remote)
    # The shape of section 2.1: times move, bytes do not.
    os.utime(local, ns=(1893492000_000000000, 1893492000_000000000))
    result = sync(env)
    assert result.outcome == "synchronized"
    after = os.lstat(remote)
    assert read(remote) == "first\n"
    assert after.st_ino == before.st_ino          # rewritten would be a new file
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == 1893492000_000000000

    # A permission-only change is not a change at all: -perms 0 and -dontchmod.
    os.chmod(local, 0o600)
    quiet = sync(env)
    assert quiet.outcome == "synchronized"
    assert os.lstat(remote).st_ino == before.st_ino


@requires_unison
def test_live_ten_central_backups_are_retained(env):
    seed(env)
    for revision in range(13):
        write(os.path.join(env.local, "note.md"), f"revision {revision}\n")
        assert sync(env).outcome == "synchronized"
    backups = workspaces.backups_dir(env.id)
    versions = sorted(name for name in os.listdir(backups)
                      if name.endswith("note.md") and name.startswith(".bak."))
    assert len(versions) == 10
    assert read(os.path.join(env.remote_root, "note.md")) == "revision 12\n"


@requires_unison
def test_live_a_deletion_propagates_and_leaves_a_backup(env):
    seed(env)
    os.unlink(os.path.join(env.local, "note.md"))
    assert sync(env).outcome == "synchronized"
    assert not os.path.exists(os.path.join(env.remote_root, "note.md"))
    backups = workspaces.backups_dir(env.id)
    assert any(name.endswith("note.md") for name in os.listdir(backups))


@requires_unison
def test_live_a_divergent_conflict_keeps_both_versions(env):
    first = seed(env)
    write(os.path.join(env.local, "note.md"), "written on linux\n")
    write(os.path.join(env.remote_root, "note.md"), "written on the mac\n")
    result = sync(env)
    assert (result.outcome, result.state) == ("conflict", "conflict")
    assert result.exit_code == 1
    assert "note.md" in result.paths
    assert result.conflicts >= 1
    # Neither side was overwritten, and no winner was chosen.
    assert read(os.path.join(env.local, "note.md")) == "written on linux\n"
    assert read(os.path.join(env.remote_root, "note.md")) == "written on the mac\n"
    document = status_of(env)
    assert document["state"] == "conflict"
    assert document["lastSuccessAt"] == first.updated_at
    assert "note.md" in document["paths"]
