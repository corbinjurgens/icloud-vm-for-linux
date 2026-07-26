"""Power-model tests: exact argv, launch decisions, and helper results.

Nothing here imports Qt, Docker, systemd, sudo, or a real mount path — the runner
is a fake and the marker is an injected boolean/path.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import power  # noqa: E402


class FakeRunner:
    """Records the argv it was handed and returns a canned result (or raises)."""

    def __init__(self, result=None, *, raises=None):
        self.result = result
        self.raises = raises
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.raises is not None:
            raise self.raises
        return self.result


# ----------------------------------------------------------- inspect states ---

def test_inspect_running():
    runner = FakeRunner(power.RunResult(0, "running\n", ""))
    status = power.inspect_container(runner)
    assert status.state == "running"
    assert runner.calls[0][0] == [
        "docker", "inspect", "-f", "{{.State.Status}}", "icloud-windows"]


@pytest.mark.parametrize("word", ["exited", "created", "dead"])
def test_inspect_stopped_variants(word):
    status = power.inspect_container(FakeRunner(power.RunResult(0, word + "\n", "")))
    assert status.state == "stopped"
    assert status.raw == word


@pytest.mark.parametrize("stderr", [
    # Docker <= 28 capitalized both words; 29 lowercases the whole line. Both
    # must classify as absent, or a first-run host with no container yet is
    # reported as an inspect error instead of "create the VM first".
    "Error: No such object: icloud-windows",
    "error: no such object: icloud-windows",
    "Error: No such container: icloud-windows",
])
def test_inspect_absent_is_not_error(stderr):
    status = power.inspect_container(FakeRunner(power.RunResult(1, "", stderr)))
    assert status.state == "absent"
    assert status.detail == stderr


def test_inspect_daemon_error():
    runner = FakeRunner(power.RunResult(1, "", "Cannot connect to the Docker daemon"))
    assert power.inspect_container(runner).state == "error"


def test_inspect_unknown_state_is_error_not_stopped():
    status = power.inspect_container(FakeRunner(power.RunResult(0, "removing\n", "")))
    assert status.state == "error"


def test_inspect_missing_docker():
    assert power.inspect_container(
        FakeRunner(raises=FileNotFoundError())).state == "error"


def test_inspect_timeout():
    status = power.inspect_container(FakeRunner(raises=TimeoutError()))
    assert status.state == "error"
    assert "timed out" in status.detail


# ------------------------------------------------------------- marker read ---

def test_marker_exists_uses_injected_path(tmp_path):
    marker = tmp_path / "powered-off"
    assert power.marker_exists(str(marker)) is False
    marker.write_text("")
    assert power.marker_exists(str(marker)) is True


# ------------------------------------------------------------ launch plans ---

def test_plan_running_no_marker_is_already_on():
    plan = power.plan_startup(False, power.DockerStatus("running", "running"))
    assert plan.kind == power.ALREADY_ON


def test_plan_running_with_marker_reconciles():
    plan = power.plan_startup(True, power.DockerStatus("running", "running"))
    assert plan.kind == power.POWER_ON


def test_plan_stopped_powers_on():
    plan = power.plan_startup(False, power.DockerStatus("stopped", "exited"))
    assert plan.kind == power.POWER_ON


def test_plan_stopped_with_marker_powers_on():
    plan = power.plan_startup(True, power.DockerStatus("stopped", "exited"))
    assert plan.kind == power.POWER_ON


def test_plan_absent_is_provision_needed_even_with_marker():
    # A missing container is never auto-created, marker or not.
    assert power.plan_startup(
        True, power.DockerStatus("absent")).kind == power.PROVISION_NEEDED
    assert power.plan_startup(
        False, power.DockerStatus("absent")).kind == power.PROVISION_NEEDED


def test_plan_error_never_mutates():
    plan = power.plan_startup(True, power.DockerStatus("error", detail="daemon down"))
    assert plan.kind == power.INSPECT_ERROR
    assert plan.detail == "daemon down"


# ---------------------------------------------------- in-session lifecycle ---

@pytest.mark.parametrize("container,expected", [
    ("running", power.ACTION_POWER_OFF),
    # Definitively exited/created/dead: recoverable in-session.
    ("stopped", power.ACTION_START),
    # Never offer to start what we cannot see or cannot classify.
    ("absent", power.ACTION_NONE),
    ("error", power.ACTION_NONE),
    (None, power.ACTION_NONE),
])
def test_running_lifecycle_follows_the_docker_classification(container, expected):
    assert power.available_action(power.LIFECYCLE_RUNNING, container) == expected


def test_powered_off_always_offers_start():
    # Whatever Docker says, this app powered it off and owns the way back.
    for container in ("stopped", "running", "error", None):
        assert power.available_action(
            power.LIFECYCLE_POWERED_OFF, container) == power.ACTION_START


def test_start_failed_keeps_retry_wording():
    assert power.available_action(
        power.LIFECYCLE_START_FAILED, "running") == power.ACTION_RETRY


def test_setup_offers_setup_only_for_a_definitely_absent_container():
    assert power.available_action(power.LIFECYCLE_SETUP, "absent") == power.ACTION_SETUP
    assert power.available_action(power.LIFECYCLE_SETUP, "error") == power.ACTION_NONE
    assert power.available_action(power.LIFECYCLE_SETUP, None) == power.ACTION_NONE


def test_transitions_offer_nothing():
    for lifecycle in (power.LIFECYCLE_STARTING, power.LIFECYCLE_SHUTTING_DOWN):
        for container in ("running", "stopped", "absent", "error", None):
            assert power.available_action(lifecycle, container) == power.ACTION_NONE


def test_an_unrecognized_docker_state_never_enables_a_mutating_action():
    """`inspect_container` reports unknown words as `error`; belt and braces."""
    assert power.available_action(power.LIFECYCLE_RUNNING, "removing") == power.ACTION_NONE


# ------------------------------------------------------------ helper calls ---

def test_power_on_exact_argv():
    runner = FakeRunner(power.RunResult(0, "==> Bridge is on\n", ""))
    result = power.power_on(runner, timeout=42)
    assert result.success is True
    assert runner.calls[0][0] == ["sudo", "-n", "/usr/local/bin/icloud-bridge-power", "on"]
    assert runner.calls[0][1] == 42
    assert result.message == "==> Bridge is on"


def test_power_off_exact_argv():
    runner = FakeRunner(power.RunResult(0, "==> Bridge is off\n", ""))
    result = power.power_off(runner)
    assert runner.calls[0][0] == ["sudo", "-n", "/usr/local/bin/icloud-bridge-power", "off"]
    assert result.success is True


def test_helper_nonzero_surfaces_stderr():
    runner = FakeRunner(power.RunResult(
        3, "", "A filesystem operation is still using an iCloud share"))
    result = power.power_off(runner)
    assert result.success is False
    assert result.exit_code == 3
    assert "still using an iCloud share" in result.message


def test_helper_nonzero_without_stderr_has_fallback():
    result = power.power_on(FakeRunner(power.RunResult(5, "", "")))
    assert result.success is False
    assert "exit 5" in result.message


def test_helper_missing_sudo():
    result = power.power_on(FakeRunner(raises=FileNotFoundError()))
    assert result.success is False
    assert "sudo" in result.message


def test_helper_timeout():
    result = power.power_off(FakeRunner(raises=TimeoutError()), timeout=130)
    assert result.success is False
    assert "Timed out" in result.message


# ------------------------------------------------------- the real adapters ---
# The injected fake runners above prove the decision logic but say nothing about
# what the *default* adapters actually hand to subprocess. These exercise them
# for real, using `env` as the observable output so no Docker daemon is needed.

def _print_env(name: str) -> list[str]:
    return [sys.executable, "-c",
            f"import os,sys; sys.stdout.write(os.environ.get({name!r}, '<unset>'))"]


def test_docker_runner_pins_socket_and_keeps_the_environment(monkeypatch):
    monkeypatch.setenv("ICLOUD_TEST_SENTINEL", "kept")
    monkeypatch.setenv("DOCKER_HOST", "unix:///home/alice/.docker/desktop/docker.sock")

    pinned = power.docker_runner(_print_env("DOCKER_HOST"), 30)
    assert pinned.stdout == power.DOCKER_SOCKET

    # A copy with one override — not a replacement: the CLI still needs HOME,
    # PATH and any proxy settings it was launched with.
    sentinel = power.docker_runner(_print_env("ICLOUD_TEST_SENTINEL"), 30)
    assert sentinel.stdout == "kept"


def test_default_runner_leaves_docker_host_alone(monkeypatch):
    """`sudo` and other helpers must not inherit the Docker override."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///home/alice/.docker/desktop/docker.sock")
    result = power.default_runner(_print_env("DOCKER_HOST"), 30)
    assert result.stdout == "unix:///home/alice/.docker/desktop/docker.sock"


def test_docker_env_does_not_mutate_the_process_environment(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert power.docker_env()["DOCKER_HOST"] == power.DOCKER_SOCKET
    assert "DOCKER_HOST" not in os.environ


def test_default_runner_normalizes_a_timeout():
    with pytest.raises(TimeoutError):
        power.default_runner([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)
