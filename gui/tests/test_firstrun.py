"""First-run assistant tests: resolution, env validation, checks, argv (no Qt)."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import backup, firstrun, power  # noqa: E402


class FakeRunner:
    """Answers per command, keyed by the first two argv words."""

    def __init__(self, answers=None, *, raises=None):
        self.answers = answers or {}
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return self.answers.get(" ".join(argv[:3]), power.RunResult(0, "", ""))


def make_bundle(tmp_path, *, env_example=".env.example", checkout=None):
    root = tmp_path / "bundle"
    (root / "provision").mkdir(parents=True)
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / env_example).write_text("SHARE_PASS=CHANGE_ME_STRONG_PASSWORD\n")
    if checkout is not None:
        (root / firstrun.CHECKOUT_MARKER).write_text(str(checkout) + "\n")
    return root


# ------------------------------------------------------ resource resolution --

def test_the_override_wins_and_is_reported_as_such(tmp_path):
    root = make_bundle(tmp_path)
    bundle = firstrun.resolve_bundle(environ={firstrun.BUNDLE_ENV: str(root)})
    assert bundle.origin == "override"
    assert bundle.root == str(root)
    assert bundle.compose_file == str(root / "docker-compose.yml")
    assert bundle.provision_dir == str(root / "provision")


def test_the_installed_env_example_name_is_accepted_too(tmp_path):
    root = make_bundle(tmp_path, env_example="env.example")
    bundle = firstrun.resolve_bundle(environ={firstrun.BUNDLE_ENV: str(root)})
    assert bundle.env_example == str(root / "env.example")


def test_an_incomplete_bundle_is_not_used(tmp_path):
    root = tmp_path / "half"
    root.mkdir()
    (root / "docker-compose.yml").write_text("services: {}\n")   # no provision/
    assert firstrun.resolve_bundle(
        environ={firstrun.BUNDLE_ENV: str(root)},
        # Stop the fallbacks from finding the real checkout or an installed copy.
        exists=lambda p: os.path.exists(p) and str(root) in p,
        isdir=lambda p: os.path.isdir(p) and str(root) in p) is None


def test_the_working_directory_is_never_consulted(tmp_path, monkeypatch):
    """A launcher or autostart entry has no meaningful cwd."""
    root = make_bundle(tmp_path)
    monkeypatch.chdir(root)
    seen = []

    def exists(path):
        seen.append(path)
        return False

    assert firstrun.resolve_bundle(environ={}, exists=exists, isdir=lambda p: False) is None
    assert all(os.path.isabs(path) for path in seen)


def test_a_recorded_checkout_that_moved_is_reported(tmp_path):
    root = make_bundle(tmp_path, checkout=tmp_path / "gone")
    bundle = firstrun.resolve_bundle(environ={firstrun.BUNDLE_ENV: str(root)})
    assert bundle.source_checkout == str(tmp_path / "gone")
    assert bundle.checkout_missing is True
    warn = [c for c in firstrun.check_bundle(bundle) if c.key == "checkout"]
    assert warn and warn[0].status == firstrun.WARN


def test_a_recorded_checkout_that_still_exists_is_not_a_warning(tmp_path):
    checkout = tmp_path / "repo"
    (checkout / "host").mkdir(parents=True)
    (checkout / "host" / "setup-host.sh").write_text("#!/bin/sh\n")
    root = make_bundle(tmp_path, checkout=checkout)
    bundle = firstrun.resolve_bundle(environ={firstrun.BUNDLE_ENV: str(root)})
    assert bundle.checkout_missing is False
    assert [c for c in firstrun.check_bundle(bundle) if c.key == "checkout"] == []


def test_no_bundle_at_all_fails_with_a_command():
    checks = firstrun.check_bundle(None)
    assert checks[0].status == firstrun.FAIL
    assert "install-gui.sh" in checks[0].command


# -------------------------------------------------------------- env parsing --

def write_env(tmp_path, text) -> str:
    path = tmp_path / ".env"
    path.write_text(text)
    return str(path)


def test_a_good_env_file_reports_its_keys_and_no_problems(tmp_path):
    path = write_env(tmp_path, "# comment\nDISK_SIZE=120G\nRAM_SIZE=3G\n"
                               "CPU_CORES=2\nSHARE_PASS=a-very-long-random-secret\n")
    report = firstrun.read_env_file(path)
    assert report.ok
    assert report.keys == ["CPU_CORES", "DISK_SIZE", "RAM_SIZE", "SHARE_PASS"]


def test_the_share_password_value_is_never_returned(tmp_path):
    secret = "correct-horse-battery-staple-42"
    path = write_env(tmp_path, f"DISK_SIZE=120G\nRAM_SIZE=3G\nCPU_CORES=2\n"
                               f"SHARE_PASS={secret}\n")
    report = firstrun.read_env_file(path)
    rendered = repr(report) + "".join(report.problems) + "".join(report.keys)
    assert secret not in rendered


def test_the_placeholder_password_is_rejected_by_name_only(tmp_path):
    path = write_env(tmp_path, "DISK_SIZE=120G\nRAM_SIZE=3G\nCPU_CORES=2\n"
                               "SHARE_PASS=CHANGE_ME_STRONG_PASSWORD\n")
    report = firstrun.read_env_file(path)
    assert not report.ok
    assert any("placeholder" in problem for problem in report.problems)


def test_missing_and_empty_keys_are_both_problems(tmp_path):
    path = write_env(tmp_path, "DISK_SIZE=\nRAM_SIZE=3G\n")
    report = firstrun.read_env_file(path)
    assert "DISK_SIZE is empty" in report.problems
    assert "CPU_CORES is missing" in report.problems
    assert "SHARE_PASS is missing" in report.problems


def test_quotes_are_stripped_and_junk_lines_are_reported(tmp_path):
    path = write_env(tmp_path, 'DISK_SIZE="120G"\nnot a setting\nRAM_SIZE=3G\n'
                               "CPU_CORES=2\nSHARE_PASS='secret-value-here'\n")
    report = firstrun.read_env_file(path)
    assert any("line 2" in problem for problem in report.problems)
    # The quoted values still counted as present, so nothing else complains.
    assert not any("DISK_SIZE" in problem for problem in report.problems)


def test_an_unreadable_env_file_is_a_problem_not_a_crash(tmp_path):
    report = firstrun.read_env_file(str(tmp_path / "nope.env"))
    assert not report.ok
    assert "cannot read" in report.problems[0]


def test_the_env_file_is_never_executed(tmp_path):
    """Parsed as text: a command substitution is data, not something to run."""
    path = write_env(tmp_path, "DISK_SIZE=$(touch /tmp/should-not-exist)\n"
                               "RAM_SIZE=3G\nCPU_CORES=2\nSHARE_PASS=long-enough-secret\n")
    report = firstrun.read_env_file(path)
    assert report.ok
    assert not os.path.exists("/tmp/should-not-exist")


# ------------------------------------------------------------- host checks --

def test_devices_pass_when_present():
    checks = firstrun.check_devices(exists=lambda p: True, access=lambda p, m: True)
    assert all(c.status == firstrun.OK for c in checks)


def test_a_missing_kvm_node_fails_with_guidance():
    checks = firstrun.check_devices(exists=lambda p: p != "/dev/kvm",
                                    access=lambda p, m: True)
    kvm = [c for c in checks if c.key == "kvm"][0]
    assert kvm.status == firstrun.FAIL
    assert "BIOS" in kvm.detail


def test_an_unusable_kvm_node_suggests_the_group_not_a_reinstall():
    checks = firstrun.check_devices(exists=lambda p: True, access=lambda p, m: False)
    kvm = [c for c in checks if c.key == "kvm"][0]
    assert kvm.status == firstrun.FAIL
    assert "usermod -aG kvm" in kvm.command


def test_docker_checks_report_engine_compose_and_context():
    runner = FakeRunner({
        "docker version -f": power.RunResult(0, "28.1.1\n", ""),
        "docker compose version": power.RunResult(0, "v2.29.0\n", ""),
        "docker context show": power.RunResult(0, "default\n", ""),
    })
    checks = firstrun.check_docker(runner)
    assert [c.status for c in checks] == [firstrun.OK, firstrun.OK]
    assert "28.1.1" in checks[0].detail


def test_a_desktop_context_is_a_warning_not_a_failure():
    runner = FakeRunner({
        "docker version -f": power.RunResult(0, "28.1.1\n", ""),
        "docker compose version": power.RunResult(0, "v2.29.0\n", ""),
        "docker context show": power.RunResult(0, "desktop-linux\n", ""),
    })
    context = [c for c in firstrun.check_docker(runner) if c.key == "context"][0]
    assert context.status == firstrun.WARN
    assert "native socket" in context.detail


def test_a_socket_permission_error_explains_the_session_requirement():
    runner = FakeRunner({
        "docker version -f": power.RunResult(
            1, "", "permission denied while trying to connect to the Docker daemon socket"),
    })
    engine = firstrun.check_docker(runner)[0]
    assert engine.status == firstrun.FAIL
    assert "log out and back in" in engine.detail
    # No point checking Compose against a daemon we cannot reach.
    assert len(firstrun.check_docker(runner)) == 1


def test_a_missing_docker_binary_is_a_failure_not_an_exception():
    engine = firstrun.check_docker(FakeRunner(raises=FileNotFoundError()))[0]
    assert engine.status == firstrun.FAIL


# ------------------------------------------------------- container presence --

def test_an_absent_container_is_the_expected_pre_create_state():
    check = firstrun.check_container("absent")[0]
    assert check.status == firstrun.OK
    assert "Create Windows VM" in check.detail


def test_an_unclassifiable_container_fails():
    assert firstrun.check_container("error")[0].status == firstrun.FAIL


# ----------------------------------------------------------- create gating --

def ok_check(key="x"):
    return firstrun.Check(key, key, firstrun.OK, "")


def test_create_needs_an_absent_container_and_no_failures():
    checks = [ok_check(), firstrun.Check("c", "c", firstrun.WARN, "")]
    assert firstrun.can_create_vm(checks, "absent") is True


@pytest.mark.parametrize("state", ["running", "stopped", "error"])
def test_create_is_never_offered_beside_an_existing_container(state):
    assert firstrun.can_create_vm([ok_check()], state) is False


def test_one_failing_check_blocks_create():
    checks = [ok_check(), firstrun.Check("bad", "bad", firstrun.FAIL, "")]
    assert firstrun.can_create_vm(checks, "absent") is False


# ------------------------------------------------------------- compose argv --

def test_compose_argv_pins_the_project_file_and_env_file(tmp_path):
    root = make_bundle(tmp_path)
    bundle = firstrun.resolve_bundle(environ={firstrun.BUNDLE_ENV: str(root)})
    argv = firstrun.compose_argv(bundle, "/home/alice/.env")
    assert argv == ["docker", "compose",
                    "-p", "icloud-bridge",
                    "-f", str(root / "docker-compose.yml"),
                    "--env-file", "/home/alice/.env",
                    "up", "-d"]


def test_create_vm_reports_a_bounded_diagnostic(tmp_path):
    root = make_bundle(tmp_path)
    bundle = firstrun.resolve_bundle(environ={firstrun.BUNDLE_ENV: str(root)})
    noisy = "\n".join(f"line {i}" for i in range(500))
    runner = FakeRunner({"docker compose -p": power.RunResult(1, noisy, "boom")})
    ok, output = firstrun.create_vm(bundle, "/home/alice/.env", runner=runner)
    assert ok is False
    assert len(output.splitlines()) <= 40
    assert "boom" in output


def test_create_vm_uses_the_docker_timeout_not_the_inspect_one(tmp_path):
    root = make_bundle(tmp_path)
    bundle = firstrun.resolve_bundle(environ={firstrun.BUNDLE_ENV: str(root)})
    calls = []

    def runner(argv, timeout):
        calls.append(timeout)
        return power.RunResult(0, "done", "")

    firstrun.create_vm(bundle, "/home/alice/.env", runner=runner)
    assert calls == [firstrun.COMPOSE_TIMEOUT_SECONDS]


# --------------------------------------------------------- host readiness ---

def test_host_setup_checks_both_argument_exact_sudo_grants():
    runner = FakeRunner()
    firstrun.check_host_setup(runner=runner, exists=lambda p: True)
    specs = [" ".join(argv) for argv in runner.calls]
    assert "sudo -n -l /usr/local/bin/icloud-bridge-power on" in specs
    assert "sudo -n -l /usr/local/bin/icloud-bridge-power off" in specs


def test_host_setup_passes_when_everything_is_installed():
    checks = firstrun.check_host_setup(runner=FakeRunner(), exists=lambda p: True)
    assert all(check.ok for check in checks)


def test_a_missing_sudo_grant_fails_with_the_configure_command():
    runner = FakeRunner({"sudo -n -l": power.RunResult(1, "", "a password is required")})
    checks = firstrun.check_host_setup(runner=runner, exists=lambda p: True)
    failed = [c for c in checks if not c.ok]
    assert failed
    assert all("icloud-bridge-configure" in c.command for c in failed)


def test_a_missing_helper_fails_and_a_missing_config_only_warns():
    def exists(path):
        return path not in (firstrun.HELPER_PATH, firstrun.HOST_CONFIG)

    checks = {c.key: c for c in firstrun.check_host_setup(
        runner=FakeRunner(), exists=exists)}
    assert checks["helper"].status == firstrun.FAIL
    assert checks["config"].status == firstrun.WARN


def test_host_setup_touches_no_mount_path():
    """The only honest mountability test is the helper's own CIFS activation."""
    looked_at = []

    def exists(path):
        looked_at.append(path)
        return True

    firstrun.check_host_setup(runner=FakeRunner(), exists=exists)
    assert not any(path.startswith("/mnt/") for path in looked_at)


# ------------------------ the interrupted-provisioning record (v2 plan D39) --

@pytest.fixture
def state_base(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return str(tmp_path / "state")


def record_of(**overrides):
    base = dict(started_at="2026-07-26T12:00:00Z", phase="creating", container_id="")
    base.update(overrides)
    return firstrun.ProvisioningRecord(**base)


def test_the_record_round_trips(state_base):
    firstrun.write_provisioning_record(record_of(container_id="abc123"))
    loaded = firstrun.read_provisioning_record()
    assert loaded == record_of(container_id="abc123")


def test_the_record_is_mode_0600_in_a_0700_directory(state_base):
    firstrun.write_provisioning_record(record_of())
    path = firstrun.provisioning_path()
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(os.path.dirname(path)).st_mode) == 0o700


def test_the_record_never_carries_the_env_path_or_its_contents(state_base):
    firstrun.write_provisioning_record(record_of(container_id="abc"))
    with open(firstrun.provisioning_path(), encoding="utf-8") as handle:
        document = json.load(handle)
    assert set(document) == {"version", "startedAt", "phase", "containerId"}


def test_no_record_reads_as_none(state_base):
    assert firstrun.read_provisioning_record() is None


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps([1, 2]),
    json.dumps({"version": 2, "startedAt": "x"}),
    json.dumps({"startedAt": "x"}),
    json.dumps({"version": 1}),
    json.dumps({"version": 1, "startedAt": ""}),
    json.dumps({"version": 1, "startedAt": 7}),
])
def test_a_malformed_record_raises_and_is_left_on_disk(state_base, payload):
    backup.ensure_app_dir()
    path = firstrun.provisioning_path()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    with pytest.raises(backup.BackupError):
        firstrun.read_provisioning_record()
    # Never silently deleted, and never treated as proof a VM is configured.
    assert os.path.exists(path)


def test_a_symlinked_record_is_not_followed(state_base, tmp_path):
    backup.ensure_app_dir()
    victim = tmp_path / "victim.json"
    victim.write_text("do not touch", encoding="utf-8")
    os.symlink(str(victim), firstrun.provisioning_path())
    with pytest.raises(backup.BackupError):
        firstrun.read_provisioning_record()
    with pytest.raises(backup.BackupError):
        firstrun.write_provisioning_record(record_of())
    assert victim.read_text(encoding="utf-8") == "do not touch"


def test_clearing_is_idempotent(state_base):
    firstrun.clear_provisioning_record()
    firstrun.write_provisioning_record(record_of())
    firstrun.clear_provisioning_record()
    assert firstrun.read_provisioning_record() is None


@pytest.mark.parametrize("state,container_id,expected", [
    ("running", "abc123", firstrun.RECORD_MATCHES),
    ("running", "different", firstrun.RECORD_DIFFERENT),
    ("stopped", "abc123", firstrun.RECORD_MATCHES),
    ("absent", "", firstrun.RECORD_CONTAINER_GONE),
])
def test_classify_a_record_with_a_known_container_id(state, container_id, expected):
    assert firstrun.classify_record(
        record_of(container_id="abc123"), state, container_id) == expected


def test_a_pre_compose_record_accepts_the_fixed_container_name():
    """Before Compose returns there is no id, so the name is all we have."""
    assert firstrun.classify_record(record_of(), "running", "") == firstrun.RECORD_MATCHES
    assert firstrun.classify_record(record_of(), "absent", "") == \
        firstrun.RECORD_CONTAINER_GONE


def test_no_record_leaves_startup_alone():
    """A running container with no record keeps the existing startup behavior."""
    assert firstrun.classify_record(None, "running", "abc") == firstrun.RECORD_ABSENT
    assert firstrun.classify_record(None, "absent", "") == firstrun.RECORD_ABSENT


def test_inspect_container_id_uses_an_exact_argv():
    runner = FakeRunner({"docker inspect -f": power.RunResult(0, "sha256:abc\n", "")})
    assert firstrun.inspect_container_id(runner) == "sha256:abc"
    assert runner.calls[0] == ["docker", "inspect", "-f", "{{.Id}}", "icloud-windows"]


def test_an_unreadable_container_id_is_empty_not_a_guess():
    runner = FakeRunner({"docker inspect -f": power.RunResult(1, "", "boom")})
    assert firstrun.inspect_container_id(runner) == ""


# ----------------------------------------- streaming compose output (D38) ---

def test_create_vm_streams_its_output_when_asked(tmp_path):
    bundle = firstrun.Bundle(
        root=str(tmp_path), compose_file=str(tmp_path / "docker-compose.yml"),
        provision_dir=str(tmp_path / "provision"),
        env_example=str(tmp_path / "env.example"), origin="source")
    seen = []
    runner = FakeRunner(
        {"docker compose -p": power.RunResult(0, "Container icloud-windows Created\n", "")})
    ok, output = firstrun.create_vm(bundle, "/tmp/.env", runner=runner,
                                    on_line=seen.append)
    assert ok
    assert "Created" in output
    # An explicit runner still wins, so existing fakes keep working unchanged.
    assert seen == []
