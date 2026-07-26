"""Diagnostic-report tests (v2 plan D37): what may enter it, and what may not.

No Qt, no mount, no docker. The runner is a fake, so the exact argv the
collector is allowed to run is asserted rather than assumed.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import __version__, diagnostics  # noqa: E402
from icloud_bridge_gui.power import RunResult  # noqa: E402

#: Sentinels that must never appear in a report, whatever is done to the inputs.
SECRET = "hunter2-do-not-leak"
REAL_PATH = "Tax Returns 2019"


class FakeRunner:
    """Records argv and answers everything successfully."""

    def __init__(self, *, returncode=0, stdout="active\n", raises=None):
        self.returncode = returncode
        self.stdout = stdout
        self.raises = raises
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        if self.raises is not None:
            raise self.raises
        return RunResult(self.returncode, self.stdout, "")


def facts(**overrides) -> diagnostics.Facts:
    base = dict(
        lifecycle="running",
        container_state="running",
        marker_present=False,
        install_origin="package",
        autostart_enabled=True,
        compatibility="current",
        compatibility_detail="",
        documents=diagnostics.DocumentFacts(
            status_version=1, status_generated_at="2026-07-26T12:00:00Z",
            status_applied_revision=7, agent_build=1, tree_version=1,
            tree_generated_at="2026-07-26T11:55:00Z",
            exclusions_revision=7, exclusions_count=2),
        health=(diagnostics.HealthRow("Windows VM", "green"),
                diagnostics.HealthRow("Guest agent", "yellow")),
        overall="yellow",
        gathered_at="2026-07-26T12:00:05Z",
        exclusion_paths=(REAL_PATH, "Docs/Big Folder"),
    )
    base.update(overrides)
    return diagnostics.Facts(**base)


def report_of(**overrides) -> str:
    include = overrides.pop("include_paths", False)
    return diagnostics.report_text(facts(**overrides), FakeRunner(),
                                   include_paths=include)


# ------------------------------------------------------- the allowed argv --

def test_only_the_documented_commands_are_run():
    runner = FakeRunner()
    diagnostics.collect(facts(), runner)
    assert tuple(runner.calls) == diagnostics.allowed_argvs()


def test_no_docker_journal_or_mount_command_is_ever_run():
    runner = FakeRunner()
    diagnostics.collect(facts(), runner)
    for argv in runner.calls:
        # Only these two binaries, and nothing that reads the journal, asks
        # Docker anything, or names a mount point. (`mnt-icloud.mount` is a unit
        # name handed to systemd, not a path anyone opens.)
        assert argv[0] in ("systemctl", "sudo")
        joined = " ".join(argv)
        assert "docker" not in joined
        assert "journalctl" not in joined
        assert "/mnt/" not in joined


@pytest.mark.parametrize("lifecycle", ["setup", "provisioning", "powered_off",
                                       "starting", "shutting_down", "start_failed",
                                       "running"])
def test_the_same_commands_run_in_every_lifecycle_state(lifecycle):
    """Reports matter most in the failure states, and none of these touch CIFS."""
    runner = FakeRunner()
    diagnostics.collect(facts(lifecycle=lifecycle), runner)
    assert tuple(runner.calls) == diagnostics.allowed_argvs()


def test_the_sudo_probes_are_argument_exact_and_non_mutating():
    runner = FakeRunner()
    diagnostics.collect(facts(), runner)
    sudo_calls = [c for c in runner.calls if c[0] == "sudo"]
    assert sudo_calls == [
        ("sudo", "-n", "-l", "/usr/local/bin/icloud-bridge-power", "on"),
        ("sudo", "-n", "-l", "/usr/local/bin/icloud-bridge-power", "off"),
    ]


def test_a_missing_systemctl_degrades_instead_of_raising():
    text = diagnostics.render(
        diagnostics.collect(facts(), FakeRunner(raises=FileNotFoundError())))
    assert "unavailable" in text
    assert "could not be checked" in text


def test_raw_probe_output_is_not_rendered():
    """Only the classified word, never what the command printed."""
    runner = FakeRunner(stdout="active\nsecret-account-name-leak\n")
    text = diagnostics.render(diagnostics.collect(facts(), runner))
    assert "secret-account-name-leak" not in text
    assert "active" in text


def test_a_denied_sudo_probe_reads_as_not_granted():
    text = diagnostics.render(
        diagnostics.collect(facts(), FakeRunner(returncode=1, stdout="")))
    assert "not granted" in text


# ------------------------------------------------------------- redaction --

def test_folder_names_are_placeholders_by_default():
    text = report_of()
    assert REAL_PATH not in text
    assert "Big Folder" not in text
    assert "<path-1>" in text and "<path-2>" in text


def test_folder_names_appear_only_on_explicit_opt_in():
    text = report_of(include_paths=True)
    assert REAL_PATH in text
    assert "<path-1>" not in text


def test_placeholders_are_stable_within_one_report():
    names = diagnostics.redact_paths(["Docs", "Other", "docs"], include=False)
    assert names == ("<path-1>", "<path-2>", "<path-1>")


def test_no_configured_exclusions_says_so_without_inventing_a_path():
    text = report_of(exclusion_paths=())
    assert "none configured" in text


def test_health_details_are_not_admitted_at_all():
    """A row is a name and a severity; its prose could contain anything."""
    row = diagnostics.HealthRow("Guest agent", "yellow")
    assert not hasattr(row, "detail")
    text = report_of(health=(row,))
    assert "Guest agent" in text and "yellow" in text


def test_an_unknown_field_cannot_reach_the_report():
    """The allowlist is the dataclass itself."""
    with pytest.raises(TypeError):
        diagnostics.Facts(share_password=SECRET)


def test_a_credential_shaped_helper_message_is_sanitized():
    text = report_of(last_helper_detail=f"mount failed: password={SECRET} rejected")
    assert SECRET not in text
    assert "<redacted>" in text


@pytest.mark.parametrize("prose", [
    f"SHARE_PASS={SECRET}",
    f"secret: {SECRET}",
    f"token = {SECRET}",
    f"credentials={SECRET}",
])
def test_credential_patterns_are_caught_in_the_bounded_field(prose):
    assert SECRET not in report_of(last_helper_detail=prose)


def test_a_report_never_names_the_credentials_files():
    text = report_of(last_helper_detail="ok")
    for forbidden in ("SHARE_PASS", "/etc/credentials-icloud", ".env"):
        assert forbidden not in text


# ---------------------------------------------------------------- bounds --

def test_each_field_is_truncated_to_the_field_bound():
    text = report_of(last_helper_detail="x" * (diagnostics.MAX_FIELD_CHARS * 3))
    assert diagnostics.TRUNCATION_SUFFIX in text
    longest = max(len(line) for line in text.splitlines())
    assert longest < diagnostics.MAX_FIELD_CHARS + 200


def test_the_whole_report_is_capped():
    rows = tuple(diagnostics.HealthRow("y" * 100, "z" * 100) for _ in range(5000))
    text = report_of(health=rows)
    assert len(text.encode("utf-8")) <= diagnostics.MAX_REPORT_BYTES
    assert text.rstrip().endswith(diagnostics.TRUNCATION_SUFFIX)


def test_a_multiline_value_cannot_break_the_layout():
    text = report_of(compatibility_detail="line one\nline two\rline three")
    assert "line one line two line three" in text


# ------------------------------------------------------- content and shape --

@pytest.mark.parametrize("raw,expected", [
    ("package", diagnostics.ORIGIN_PACKAGE),
    ("user", diagnostics.ORIGIN_PER_USER),
    ("per-user", diagnostics.ORIGIN_PER_USER),
    ("source", diagnostics.ORIGIN_SOURCE),
    ("override", diagnostics.ORIGIN_OVERRIDE),
    ("/home/someone/checkout", diagnostics.ORIGIN_UNKNOWN),
    (None, diagnostics.ORIGIN_UNKNOWN),
])
def test_every_coarse_install_origin_renders_without_a_path(raw, expected):
    text = report_of(install_origin=raw)
    assert expected in text
    assert "/home/" not in text


def test_the_app_version_is_reported():
    assert __version__ in report_of()


def test_ungathered_bridge_facts_are_labelled_not_gathered():
    """A no-CIFS state has nothing current to report, and must say so."""
    text = report_of(lifecycle="setup", gathered_at="")
    assert "not gathered" in text


def test_gathered_facts_carry_their_timestamp():
    assert "2026-07-26T12:00:05Z" in report_of()


def test_the_helper_result_is_classified_not_raw():
    assert "not run" in report_of(last_helper_ok=None)
    assert "succeeded" in report_of(last_helper_ok=True)
    assert "failed" in report_of(last_helper_ok=False)


def test_the_report_states_its_own_redaction_policy():
    assert diagnostics.REDACTION_NOTE in report_of()
    assert diagnostics.DISCLOSURE_NOTE in report_of(include_paths=True)


def test_every_section_appears():
    text = report_of()
    for title in ("Application", "Lifecycle", "Bridge documents", "Health",
                  "Host units", "Helper authorization", "Selective sync"):
        assert title in text


def test_all_six_units_are_probed_and_reported():
    text = report_of()
    for unit in diagnostics.UNITS:
        assert unit in text
    assert len(diagnostics.UNITS) == len(set(diagnostics.UNITS))


def test_the_default_filename_is_safe():
    assert diagnostics.default_filename("20260726-120000") == \
        "icloud-bridge-diagnostics-20260726-120000.txt"
    # A hostile stamp cannot introduce a path separator or a traversal.
    name = diagnostics.default_filename("../../etc/passwd")
    assert "/" not in name and ".." not in name
