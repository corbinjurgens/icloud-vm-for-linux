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


def test_inspect_absent_is_not_error():
    runner = FakeRunner(power.RunResult(1, "", "Error: No such object: icloud-windows"))
    status = power.inspect_container(runner)
    assert status.state == "absent"


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
