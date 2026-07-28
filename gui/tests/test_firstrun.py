"""First-run assistant tests: resolution, env validation, checks, argv (no Qt).

The env-file section also pins the shared ``SHARE_PASS`` grammar (v2 plan D41)
against ``host/icloud-bridge-configure``: the GUI sets the guest account from
the same file the shell script derives ``/etc/credentials-icloud`` from, so the
two readers agreeing is a correctness requirement, not tidiness.  The passwords
here are synthetic fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import (backup, envfile, firstrun, guestprov,  # noqa: E402
                               lifecycle, power)


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


# ----------------------------------------------------- app configuration --

def test_conventional_configuration_path_uses_xdg_config_home(tmp_path):
    assert firstrun.configuration_path(environ={"XDG_CONFIG_HOME": str(tmp_path)}) == (
        str(tmp_path / "icloud-bridge" / "env"))


def test_create_configuration_is_private_and_uses_the_shared_password_grammar(tmp_path):
    path = firstrun.create_configuration("120G", "3G", "2",
                                         path=str(tmp_path / "config" / "env"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "config").st_mode) == 0o700
    value, problems = envfile.share_pass_problems(open(path, encoding="utf-8").read())
    assert problems == []
    assert len(value) >= 24
    assert value.isalnum()


def test_create_configuration_reuses_an_existing_file_without_rewriting_it(tmp_path):
    path = tmp_path / "config" / "env"
    path.parent.mkdir()
    path.write_text("keep this file\n", encoding="utf-8")
    before = path.read_bytes()
    assert firstrun.create_configuration("120G", "3G", "2", path=str(path)) == str(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("disk,ram,cores", [
    ("", "3G", "2"),
    ("120", "3G", "2"),
    ("120G\nSHARE_PASS=injected", "3G", "2"),
    ("120G", "lots", "2"),
    ("120G", "3G", "two"),
    ("120G", "3G", "0"),
])
def test_create_configuration_rejects_values_outside_the_env_grammar(
        tmp_path, disk, ram, cores):
    with pytest.raises(ValueError):
        firstrun.create_configuration(disk, ram, cores,
                                      path=str(tmp_path / "config" / "env"))
    assert not (tmp_path / "config" / "env").exists()


def test_resource_defaults_are_clamped_to_the_example_floors():
    defaults = firstrun.resource_defaults(cpu_count=1, available_memory_bytes=1,
                                          available_disk_bytes=1)
    assert defaults == firstrun.ResourceDefaults("120G", "3G", "2")


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


def test_quotes_are_stripped_from_compose_keys_and_junk_lines_are_reported(tmp_path):
    path = write_env(tmp_path, 'DISK_SIZE="120G"\nnot a setting\nRAM_SIZE=3G\n'
                               "CPU_CORES=2\nSHARE_PASS=a-very-long-random-secret\n")
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


def test_the_parser_lives_in_one_place_for_all_three_readers():
    """`firstrun` re-exports it; it does not carry a second copy (D41)."""
    assert firstrun.read_env_file is envfile.read_env_file
    assert firstrun.EnvReport is envfile.EnvReport


# ------------------------------------------- the shared SHARE_PASS grammar --
# One physical `SHARE_PASS=` line in column 1; everything after the first `=` is
# the value, so `#` and later `=` are data; no quote processing, no surrounding
# whitespace, no NUL and no CR. Duplicates and quoted forms are rejected rather
# than reinterpreted (v2 plan section 4.1).

ACCEPTED = "accepted"
REJECTED = "rejected"
ABSENT = "absent"

#: (name, file text, verdict, expected value). Every case is run through both
#: the Python parser and the shell function, so the two cannot drift.
GRAMMAR_CASES = [
    ("plain", "DISK_SIZE=120G\nSHARE_PASS=a-very-long-random-secret\n",
     ACCEPTED, "a-very-long-random-secret"),
    ("hash and equals are data", "SHARE_PASS=pa#ss=word\n", ACCEPTED, "pa#ss=word"),
    ("no trailing newline", "SHARE_PASS=abcdefgh", ACCEPTED, "abcdefgh"),
    ("a similarly named key is not this one",
     "MY_SHARE_PASS=other\nSHARE_PASS=chosen\n", ACCEPTED, "chosen"),
    ("internal spaces are kept", "SHARE_PASS=two words\n", ACCEPTED, "two words"),
    ("duplicated", "SHARE_PASS=first\nSHARE_PASS=second\n", REJECTED, ""),
    ("double quoted", 'SHARE_PASS="quoted"\n', REJECTED, ""),
    ("single quoted", "SHARE_PASS='quoted'\n", REJECTED, ""),
    ("leading space in the value", "SHARE_PASS= leading\n", REJECTED, ""),
    ("trailing space in the value", "SHARE_PASS=trailing \n", REJECTED, ""),
    ("indented key", "  SHARE_PASS=indented\n", REJECTED, ""),
    ("spaces around the equals", "SHARE_PASS = spaced\n", REJECTED, ""),
    ("empty value", "SHARE_PASS=\n", REJECTED, ""),
    ("carriage return", "SHARE_PASS=crlf\r\n", REJECTED, ""),
    ("a NUL byte", "SHARE_PASS=nu\x00l\n", REJECTED, ""),
    ("the placeholder", "SHARE_PASS=CHANGE_ME_STRONG_PASSWORD\n", REJECTED, ""),
    ("the other placeholder", "SHARE_PASS=STRONG_PASSWORD_HERE\n", REJECTED, ""),
    ("no such line", "DISK_SIZE=120G\n", ABSENT, ""),
    ("only a comment", "# SHARE_PASS=commented-out\n", ABSENT, ""),
]


@pytest.mark.parametrize("name,text,verdict,expected",
                         GRAMMAR_CASES, ids=[case[0] for case in GRAMMAR_CASES])
def test_the_python_parser_applies_the_grammar(tmp_path, name, text, verdict, expected):
    path = tmp_path / ".env"
    path.write_bytes(text.encode("utf-8"))
    value, problems = envfile.share_pass_problems(text)
    if verdict == ACCEPTED:
        assert problems == []
        assert value == expected
        assert envfile.read_share_pass(str(path)) == expected
    else:
        assert problems
        assert value == ""
        with pytest.raises(envfile.EnvError):
            envfile.read_share_pass(str(path))
    if verdict == ABSENT:
        assert problems == ["SHARE_PASS is missing"]


def test_a_rejection_never_quotes_the_value(tmp_path):
    secret = "correct-horse-battery-staple-42"
    path = tmp_path / ".env"
    path.write_text(f"SHARE_PASS='{secret}'\n", encoding="utf-8")
    with pytest.raises(envfile.EnvError) as excinfo:
        envfile.read_share_pass(str(path))
    assert secret not in str(excinfo.value)


def test_the_report_still_never_carries_the_value(tmp_path):
    secret = "correct-horse-battery-staple-42"
    path = write_env(tmp_path, f"DISK_SIZE=1G\nRAM_SIZE=1G\nCPU_CORES=1\n"
                               f"SHARE_PASS={secret}\n")
    report = firstrun.read_env_file(path)
    assert report.ok
    assert secret not in repr(report) + "".join(report.problems + report.keys)


# ---------------------- the same grammar in host/icloud-bridge-configure --

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIGURE = os.path.join(REPO, "host", "icloud-bridge-configure")


def _extract_function(path: str, name: str) -> str:
    """Pull one top-level shell function out of a script by its brace block.

    The same seam `test_power_helper.py` uses: the script as a whole needs root,
    systemd and a live Docker daemon, but this function is pure text work over
    one file and can be proved here.
    """
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def shell_read_share_pass(path: str):
    """``(exit status, stdout)`` from the script's own ``read_share_pass``."""
    script = _extract_function(CONFIGURE, "read_share_pass") + \
        '\nread_share_pass "$1"\n'
    completed = subprocess.run(["bash", "-c", script, "bash", path],
                               capture_output=True, text=True, check=False)
    return completed.returncode, completed.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
@pytest.mark.parametrize("name,text,verdict,expected",
                         GRAMMAR_CASES, ids=[case[0] for case in GRAMMAR_CASES])
def test_the_shell_parser_applies_the_same_grammar(tmp_path, name, text, verdict,
                                                   expected):
    """A divergence here would configure Windows and /etc/credentials-icloud
    with different passwords, and the bridge would fail to mount with a value
    that looks correct at both ends."""
    path = tmp_path / ".env"
    path.write_bytes(text.encode("utf-8"))
    status, stdout = shell_read_share_pass(str(path))
    if verdict == ACCEPTED:
        assert status == 0
        assert stdout == expected + "\n"
    elif verdict == REJECTED:
        assert status == 1
        assert stdout == ""
    else:
        # 2 is "this file simply does not set it": keep looking at the next
        # candidate rather than failing the whole run.
        assert status == 2
        assert stdout == ""


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


# -------------------- the durable provisioning record (v2 plan D39, D43) --

@pytest.fixture
def state_base(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return str(tmp_path / "state")


RUN_ID = "0123456789abcdef0123456789abcdef"
OTHER_RUN_ID = "fedcba9876543210fedcba9876543210"


def record_of(**overrides):
    base = dict(started_at="2026-07-26T12:00:00Z", phase="creating", container_id="")
    base.update(overrides)
    return firstrun.ProvisioningRecord(**base)


def document_of(**overrides):
    """A complete, valid on-disk document; each test spoils one field."""
    base = {
        "version": firstrun.PROVISIONING_VERSION,
        "mode": firstrun.MODE_FIRST_RUN,
        "startedAt": "2026-07-26T12:00:00Z",
        "phase": firstrun.RECORD_PHASE_STAGING,
        "containerId": "abc123",
        "containerStartedAt": "2026-07-26T11:59:00.1Z",
        "guestRunId": RUN_ID,
        "guestPhase": "",
        "resetShareCredential": True,
    }
    base.update(overrides)
    return json.dumps(base)


def test_the_record_round_trips(state_base):
    record = record_of(container_id="abc123", mode=firstrun.MODE_REPROVISION,
                       phase=firstrun.RECORD_PHASE_TRIGGERED,
                       container_started_at="2026-07-26T11:59:00.123456789Z",
                       guest_run_id=RUN_ID, guest_phase="inspecting",
                       reset_share_credential=True)
    firstrun.write_provisioning_record(record)
    assert firstrun.read_provisioning_record() == record


def test_the_record_defaults_to_a_first_run_with_no_guest_run(state_base):
    """The pre-Compose write of D39 is still exactly what it was."""
    firstrun.write_provisioning_record(record_of())
    loaded = firstrun.read_provisioning_record()
    assert loaded.mode == firstrun.MODE_FIRST_RUN
    assert loaded.guest_run_id == ""
    assert loaded.reset_share_credential is False


def test_the_record_is_mode_0600_in_a_0700_directory(state_base):
    firstrun.write_provisioning_record(record_of())
    path = firstrun.provisioning_path()
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(os.path.dirname(path)).st_mode) == 0o700


def test_the_record_never_carries_the_env_path_or_its_contents(state_base):
    firstrun.write_provisioning_record(
        record_of(container_id="abc", guest_run_id=RUN_ID,
                  phase=firstrun.RECORD_PHASE_STAGING, reset_share_credential=True))
    with open(firstrun.provisioning_path(), encoding="utf-8") as handle:
        document = json.load(handle)
    assert set(document) == {"version", "mode", "startedAt", "phase", "containerId",
                             "containerStartedAt", "guestRunId", "guestPhase",
                             "resetShareCredential"}


def test_rewriting_the_record_replaces_it_atomically(state_base):
    firstrun.write_provisioning_record(record_of())
    firstrun.write_provisioning_record(record_of(phase=firstrun.RECORD_PHASE_TRIGGERED,
                                                 guest_run_id=RUN_ID))
    path = firstrun.provisioning_path()
    assert firstrun.read_provisioning_record().guest_run_id == RUN_ID
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    # The unique temp file is renamed over the target, never left behind.
    assert os.listdir(os.path.dirname(path)) == [firstrun.PROVISIONING_RECORD]


def test_a_malformed_run_id_or_mode_is_refused_before_it_reaches_the_guest(state_base):
    """The run ID is a path component in the guest; it is never sanitized."""
    with pytest.raises(ValueError):
        firstrun.write_provisioning_record(record_of(guest_run_id="run-1"))
    with pytest.raises(ValueError):
        firstrun.write_provisioning_record(record_of(mode="whatever"))
    with pytest.raises(ValueError):
        firstrun.write_provisioning_record(record_of(phase="somewhere"))
    assert firstrun.read_provisioning_record() is None


def test_no_record_reads_as_none(state_base):
    assert firstrun.read_provisioning_record() is None


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps([1, 2]),
    # No migration path: the D39 schema is simply not this one (CONTRIBUTING).
    json.dumps({"version": 1, "startedAt": "x", "phase": "creating",
                "containerId": ""}),
    json.dumps({"startedAt": "x"}),
    document_of(version=3),
    document_of(startedAt=""),
    document_of(startedAt=7),
    document_of(mode="reprovisioning"),
    document_of(phase="finished"),
    document_of(guestRunId="RUN"),
    document_of(guestRunId=RUN_ID.upper()),
    document_of(guestRunId=7),
    document_of(resetShareCredential=1),
    document_of(resetShareCredential="true"),
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


@pytest.mark.parametrize("mode", [firstrun.MODE_FIRST_RUN, firstrun.MODE_REPROVISION])
def test_both_modes_take_the_same_startup_before_cifs_gate(mode):
    """D43: otherwise a restarted app mounts while 03/04 rewrite guest state."""
    record = record_of(container_id="abc123", mode=mode, guest_run_id=RUN_ID,
                       phase=firstrun.RECORD_PHASE_TRIGGERED)
    assert firstrun.classify_record(record, "running", "abc123") == \
        firstrun.RECORD_MATCHES


# -------------------------- resuming an interrupted run (v2 plan D43) --
# Every status here is built by `guestprov.classify_status`, the real validator,
# from a document the guest could have written. Nothing in this section runs
# Docker: the run's whole restart decision table is pure.

TOKEN = "2026-07-26T11:59:00.123456789Z"
NEW_TOKEN = "2026-07-27T08:00:00.987654321Z"


def guest_status(phase, *, run_id=RUN_ID, for_run=RUN_ID, error=None):
    document = {
        "version": 1, "runId": run_id, "phase": phase, "detail": "",
        "updatedAt": "2026-07-26T12:01:00Z", "error": error,
        "checks": {key: "pending" for key in guestprov.CHECK_KEYS},
        "work": [],
    }
    return guestprov.classify_status(document, for_run)


def live_record(**overrides):
    base = dict(container_id="abc123", container_started_at=TOKEN,
                guest_run_id=RUN_ID, phase=firstrun.RECORD_PHASE_TRIGGERED)
    base.update(overrides)
    return record_of(**base)


def test_a_record_from_before_this_feature_has_no_run_to_resume():
    assert firstrun.classify_resume(record_of(), None) == firstrun.RESUME_NO_RUN
    assert firstrun.classify_resume(None, None) == firstrun.RESUME_NO_RUN


@pytest.mark.parametrize("mode", [firstrun.MODE_FIRST_RUN, firstrun.MODE_REPROVISION])
def test_a_live_container_and_token_and_active_run_just_keeps_polling(mode):
    """Restart during a first run, and during a re-provision: same answer."""
    status = guest_status(guestprov.PHASE_INSTALLING_ICLOUD)
    assert firstrun.classify_resume(live_record(mode=mode), status,
                                    container_started_at=TOKEN) == firstrun.RESUME_POLL


def test_a_guest_waiting_for_the_secret_needs_the_env_file_again():
    """The path was never stored, so re-selection is the recovery (D41)."""
    status = guest_status(guestprov.PHASE_WAITING_FOR_SECRET)
    assert firstrun.classify_resume(live_record(), status,
                                    container_started_at=TOKEN) == \
        firstrun.RESUME_NEEDS_SECRET


def test_an_acknowledged_run_is_believed_over_the_restart_evidence():
    """The watcher records the accepted run locally and survives a reboot."""
    status = guest_status(guestprov.PHASE_CREATING_SHARE)
    assert firstrun.classify_resume(live_record(), status,
                                    container_started_at=NEW_TOKEN) == \
        firstrun.RESUME_POLL


def test_a_finished_or_failed_run_is_reported_as_such():
    done = guest_status(guestprov.PHASE_DONE)
    assert firstrun.classify_resume(live_record(), done,
                                    container_started_at=TOKEN) == firstrun.RESUME_DONE
    failed = guest_status(guestprov.PHASE_CREATING_SHARE, error="the share is not there")
    assert firstrun.classify_resume(live_record(), failed,
                                    container_started_at=TOKEN) == \
        firstrun.RESUME_FAILED


@pytest.mark.parametrize("status", [
    guestprov.Status(guestprov.PHASE_ABSENT),
    guestprov.Status(guestprov.PHASE_UNREADABLE, reason="not JSON"),
    guest_status(guestprov.PHASE_VERIFYING, run_id=OTHER_RUN_ID),
])
def test_a_restarted_container_with_no_usable_status_offers_a_new_run(status):
    """A changed start token is never a reason to adopt an uncorrelated status."""
    assert firstrun.classify_resume(live_record(), status,
                                    container_started_at=NEW_TOKEN) == \
        firstrun.RESUME_CONFIRM_NEW_RUN


@pytest.mark.parametrize("status", [
    guestprov.Status(guestprov.PHASE_ABSENT),
    guest_status(guestprov.PHASE_VERIFYING, run_id=OTHER_RUN_ID),
])
def test_without_restart_evidence_a_missing_status_keeps_polling(status):
    """Abandoning a possibly live wait is an explicit confirmation, not a default."""
    assert firstrun.classify_resume(live_record(), status,
                                    container_started_at=TOKEN) == \
        firstrun.RESUME_POLL_OR_ABANDON


@pytest.mark.parametrize("observed", ["", TOKEN])
def test_an_unreadable_start_token_is_not_evidence_of_a_restart(observed):
    record = live_record(container_started_at="")
    assert firstrun.container_restarted(record, observed) is False
    assert firstrun.classify_resume(record, guestprov.Status(guestprov.PHASE_ABSENT),
                                    container_started_at=observed) == \
        firstrun.RESUME_POLL_OR_ABANDON


@pytest.mark.parametrize("observed", [TOKEN, NEW_TOKEN, ""])
def test_a_crash_while_staging_retries_the_same_run_id(observed):
    """The trigger's atomic rename may never have happened; nothing consumed it."""
    record = live_record(phase=firstrun.RECORD_PHASE_STAGING)
    assert firstrun.classify_resume(record, guestprov.Status(guestprov.PHASE_ABSENT),
                                    container_started_at=observed) == \
        firstrun.RESUME_RETRY_STAGE
    # …with the *same* saved run ID: the decision never mints a new one.
    assert record.guest_run_id == RUN_ID


def test_an_acknowledged_run_leaves_the_staging_retry_behind():
    status = guest_status(guestprov.PHASE_INSPECTING)
    record = live_record(phase=firstrun.RECORD_PHASE_STAGING)
    assert firstrun.classify_resume(record, status,
                                    container_started_at=TOKEN) == firstrun.RESUME_POLL


def test_a_matching_status_advances_the_recorded_guest_phase():
    record = live_record(phase=firstrun.RECORD_PHASE_STAGING)
    updated = firstrun.record_after_status(
        record, guest_status(guestprov.PHASE_LAUNCHING_ICLOUD))
    assert updated.guest_phase == guestprov.PHASE_LAUNCHING_ICLOUD
    assert updated.phase == firstrun.RECORD_PHASE_TRIGGERED
    assert updated.guest_run_id == RUN_ID
    assert updated.mode == record.mode


@pytest.mark.parametrize("status", [
    guest_status(guestprov.PHASE_DONE, run_id=OTHER_RUN_ID),
    guestprov.Status(guestprov.PHASE_ABSENT),
    guestprov.Status(guestprov.PHASE_UNREADABLE, reason="not JSON"),
])
def test_a_mismatched_status_never_replaces_the_saved_run(status):
    record = live_record(guest_phase=guestprov.PHASE_INSPECTING)
    assert firstrun.record_after_status(record, status) == record


def test_the_modes_have_one_spelling_shared_with_the_reducer():
    assert firstrun.MODE_FIRST_RUN == lifecycle.MODE_FIRST_RUN
    assert firstrun.MODE_REPROVISION == lifecycle.MODE_REPROVISION
    assert firstrun.PROVISIONING_MODES == lifecycle.PROVISIONING_MODES


def test_inspect_container_started_at_uses_an_exact_argv():
    runner = FakeRunner({"docker inspect -f": power.RunResult(0, TOKEN + "\n", "")})
    assert firstrun.inspect_container_started_at(runner) == TOKEN
    assert runner.calls[0] == ["docker", "inspect", "-f", "{{.State.StartedAt}}",
                               "icloud-windows"]


def test_an_unreadable_start_token_reads_as_empty_not_a_guess():
    runner = FakeRunner({"docker inspect -f": power.RunResult(1, "", "boom")})
    assert firstrun.inspect_container_started_at(runner) == ""


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
