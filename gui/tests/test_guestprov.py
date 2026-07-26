"""Guest-provisioning channel tests (v2 plan D40-D44, section 4.1).

No test here reaches Docker, a container, a socket or a mount: every subprocess
goes through a fake runner, and the X.224 probe is driven by a fake socket.

Nothing retains a password either.  The input-runner fake asserts on the bytes
it is handed *inside* the call and keeps only their length afterwards, and the
passwords below are synthetic fixture strings that exist mainly to be asserted
absent from every argv, transcript and result.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icloud_bridge_gui import envfile, firstrun, guestprov, power  # noqa: E402

RUN_ID = "0123456789abcdef0123456789abcdef"
OTHER_RUN_ID = "fedcba9876543210fedcba9876543210"
SECRET = "synthetic-share-password-42"


# ------------------------------------------------------------------- fakes --

class FakeDocker:
    """One fake behind both runner shapes, sharing a single ordered call log.

    Answers are keyed by a distinctive substring of the joined argv, so a test
    only has to describe the commands whose *result* it cares about; everything
    else succeeds silently.
    """

    def __init__(self, *rules, on_input=None):
        self.rules = list(rules)
        self.calls: list[tuple[str, list[str]]] = []
        self.input_sizes: list[int] = []
        self.on_input = on_input

    def _answer(self, argv):
        joined = " ".join(argv)
        for needle, result in self.rules:
            if needle in joined:
                return result
        return power.RunResult(0, "", "")

    def runner(self, argv, timeout):
        assert isinstance(timeout, (int, float)) and timeout > 0
        self.calls.append(("run", list(argv)))
        return self._answer(argv)

    def input_runner(self, argv, timeout, input_bytes):
        assert isinstance(input_bytes, bytes)
        if self.on_input is not None:
            # Inside the call is the only place the bytes may be looked at.
            self.on_input(list(argv), input_bytes)
        self.calls.append(("input", list(argv)))
        self.input_sizes.append(len(input_bytes))
        return self._answer(argv)

    @property
    def commands(self) -> list[str]:
        return [" ".join(argv) for _, argv in self.calls]

    @property
    def transcript(self) -> str:
        return "\n".join(self.commands)

    def index_of(self, needle: str) -> int:
        for position, command in enumerate(self.commands):
            if needle in command:
                return position
        raise AssertionError(f"no command contained {needle!r}")


DOCKUR_CONF = "[global]\n   workgroup = WORKGROUP\n\n[Data]\n   path = /tmp/smb\n"

READ_CONF = "head -c 262144"
TESTPARM_CANDIDATE = "testparm -s /etc/samba/smb.conf.icloud-bridge-candidate"
DISCARD_CANDIDATE = "rm -f /etc/samba/smb.conf.icloud-bridge-candidate"
ACTIVATE = "mv -f /etc/samba/smb.conf.icloud-bridge-candidate /etc/samba/smb.conf"
VERIFY_PATH = "--parameter-name path"
VERIFY_READ_ONLY = "--parameter-name 'read only'"
READ_STATUS = "f=/tmp/smb/.provision/status.json"
READ_TRIGGER = "f=/run/icloud-bridge-provision/trigger.json"
WRITE_TRIGGER = "cat > .tmp-trigger.json"
WRITE_SECRET = "cat > .tmp-secret"
SECRET_PRESENT = "test -e /run/icloud-bridge-provision/secret"
PROMOTE = 'mv -f ".tmp-$f" "$f"'
RESET_INBOX = "rm -rf /run/icloud-bridge-provision"
WRITE_CANDIDATE = "cat > /etc/samba/smb.conf.icloud-bridge-candidate"


def healthy_channel(*extra, on_input=None) -> FakeDocker:
    """A container whose effective Samba configuration is the required one."""
    return FakeDocker(
        *extra,
        (READ_CONF, power.RunResult(0, DOCKUR_CONF, "")),
        (VERIFY_PATH, power.RunResult(0, guestprov.INBOX_DIR + "\n", "")),
        (VERIFY_READ_ONLY, power.RunResult(0, "Yes\n", "")),
        on_input=on_input,
    )


def status_document(run_id=RUN_ID, *, phase=guestprov.PHASE_INSPECTING, error=None,
                    checks=None, work=(), detail="working"):
    return {
        "version": 1,
        "runId": run_id,
        "phase": phase,
        "detail": detail,
        "updatedAt": "2026-07-27T10:00:00Z",
        "error": error,
        "checks": checks or {key: "pending" for key in guestprov.CHECK_KEYS},
        "work": list(work),
    }


def status_result(document, *, mtime=1000.0, size=None):
    body = document if isinstance(document, str) else json.dumps(document)
    length = len(body.encode("utf-8")) if size is None else size
    return power.RunResult(0, f"{mtime} {length}\n{body}", "")


ABSENT_STATUS = power.RunResult(9, "", "")


def bundle_source(tmp_path, name: str) -> str:
    return str(tmp_path / "bundle" / "provision" / name)


def make_bundle(tmp_path) -> firstrun.Bundle:
    provision = tmp_path / "bundle" / "provision"
    provision.mkdir(parents=True)
    for name in guestprov.PAYLOAD_FILES:
        (provision / name).write_text("# staged\n", encoding="utf-8")
    return firstrun.Bundle(root=str(tmp_path / "bundle"),
                           compose_file=str(tmp_path / "bundle" / "docker-compose.yml"),
                           provision_dir=str(provision),
                           env_example=str(tmp_path / "bundle" / "env.example"),
                           origin="override")


# ------------------------------------------------ the candidate configuration --

def test_the_managed_block_is_appended_to_dockurs_configuration():
    candidate = guestprov.build_candidate_config(DOCKUR_CONF)
    assert DOCKUR_CONF.rstrip("\n") in candidate
    assert candidate.count(guestprov.MARKER_BEGIN) == 1
    assert candidate.rstrip("\n").endswith(guestprov.MARKER_END)
    for setting in ("path = /run/icloud-bridge-provision", "read only = yes",
                    "guest ok = yes", "guest only = yes", "force user = root"):
        assert setting in candidate


def test_rebuilding_over_our_own_block_is_idempotent():
    """dockur regenerates its config on every container start, and the app
    re-runs `ensure_channel` on every provisioning run."""
    once = guestprov.build_candidate_config(DOCKUR_CONF)
    assert guestprov.build_candidate_config(once) == once
    assert guestprov.build_candidate_config(once).count("[Provision]") == 1


def test_a_foreign_provision_stanza_is_a_conflict_not_a_merge():
    conflicting = DOCKUR_CONF + "\n[Provision]\n   path = /srv/somewhere\n"
    with pytest.raises(guestprov.ProvisioningError) as excinfo:
        guestprov.build_candidate_config(conflicting)
    assert "outside this app's managed block" in str(excinfo.value)


def test_a_case_variant_of_the_share_name_is_still_a_conflict():
    """Samba share names are case-insensitive, so `[provision]` is the same share."""
    with pytest.raises(guestprov.ProvisioningError):
        guestprov.build_candidate_config(DOCKUR_CONF + "\n[ provision ]\n")


@pytest.mark.parametrize("broken", [
    guestprov.MARKER_BEGIN + "\n[Provision]\n",                       # no end
    "[global]\n" + guestprov.MARKER_END + "\n",                       # no begin
    guestprov.MARKER_BEGIN + "\n" + guestprov.MARKER_BEGIN + "\n"
    + guestprov.MARKER_END + "\n",                                    # nested
])
def test_an_incomplete_marker_block_fails_closed(broken):
    with pytest.raises(guestprov.ProvisioningError):
        guestprov.build_candidate_config(broken)


# -------------------------------------------------------------- the channel --

def test_ensure_channel_creates_a_root_only_inbox_before_touching_the_config():
    docker = healthy_channel()
    guestprov.ensure_channel(docker.runner, docker.input_runner)
    assert docker.index_of("chmod 700 /run/icloud-bridge-provision") < \
        docker.index_of(READ_CONF)
    assert "chown root:root /run/icloud-bridge-provision" in docker.transcript


def test_the_candidate_travels_on_stdin_and_never_through_shell_source():
    delivered = {}

    def on_input(argv, payload):
        delivered["text"] = payload.decode("utf-8")

    docker = healthy_channel(on_input=on_input)
    guestprov.ensure_channel(docker.runner, docker.input_runner)
    assert "[Provision]" in delivered["text"]
    # `docker exec -i`: without it the child gets no stdin at all.
    assert docker.calls[docker.index_of(WRITE_CANDIDATE)][1][:4] == \
        ["docker", "exec", "-i", "icloud-windows"]
    # The configuration text is data, not shell syntax: no command carries it.
    assert "[Provision]" not in docker.transcript
    assert docker.index_of(TESTPARM_CANDIDATE) > docker.index_of(WRITE_CANDIDATE)


def test_a_candidate_that_fails_testparm_is_discarded_and_never_activated():
    docker = healthy_channel((TESTPARM_CANDIDATE, power.RunResult(1, "", "syntax error")))
    with pytest.raises(guestprov.ProvisioningError) as excinfo:
        guestprov.ensure_channel(docker.runner, docker.input_runner)
    assert "left untouched" in str(excinfo.value)
    assert DISCARD_CANDIDATE in docker.transcript
    assert ACTIVATE not in docker.transcript


def test_the_configuration_is_swapped_by_rename_then_reloaded():
    docker = healthy_channel()
    guestprov.ensure_channel(docker.runner, docker.input_runner)
    assert docker.index_of(TESTPARM_CANDIDATE) < docker.index_of(ACTIVATE)
    assert "smbcontrol smbd reload-config" in docker.transcript


def test_the_effective_share_is_verified_after_the_reload():
    docker = healthy_channel()
    guestprov.ensure_channel(docker.runner, docker.input_runner)
    assert docker.index_of(VERIFY_PATH) > docker.index_of(ACTIVATE)
    assert docker.index_of(VERIFY_READ_ONLY) > docker.index_of(ACTIVATE)


@pytest.mark.parametrize("rule,expected", [
    ((VERIFY_PATH, power.RunResult(0, "/tmp/smb\n", "")), "does not serve"),
    ((VERIFY_PATH, power.RunResult(1, "", "no such section")), "does not serve"),
    ((VERIFY_READ_ONLY, power.RunResult(0, "No\n", "")), "not read-only"),
    ((VERIFY_READ_ONLY, power.RunResult(1, "", "boom")), "not read-only"),
])
def test_an_unverifiable_share_refuses_to_go_further(rule, expected):
    docker = healthy_channel(rule)
    with pytest.raises(guestprov.ProvisioningError) as excinfo:
        guestprov.ensure_channel(docker.runner, docker.input_runner)
    assert expected in str(excinfo.value)


def test_a_conflicting_configuration_is_never_written_to_the_container():
    docker = healthy_channel(
        (READ_CONF, power.RunResult(0, DOCKUR_CONF + "\n[Provision]\n", "")))
    with pytest.raises(guestprov.ProvisioningError):
        guestprov.ensure_channel(docker.runner, docker.input_runner)
    assert WRITE_CANDIDATE not in docker.transcript
    assert ACTIVATE not in docker.transcript


def test_an_unreadable_dockur_configuration_is_an_error_not_an_empty_candidate():
    docker = healthy_channel((READ_CONF, power.RunResult(1, "", "no such file")))
    with pytest.raises(guestprov.ProvisioningError):
        guestprov.ensure_channel(docker.runner, docker.input_runner)
    assert WRITE_CANDIDATE not in docker.transcript


# ------------------------------------------------------------------ run IDs --

def test_a_new_run_id_is_32_lowercase_hex_characters():
    ids = {guestprov.new_run_id() for _ in range(50)}
    assert len(ids) == 50
    for value in ids:
        assert guestprov.validate_run_id(value) == value


@pytest.mark.parametrize("bad", [
    "", "0123456789abcdef", "0123456789ABCDEF0123456789ABCDEF",
    "0123456789abcdef0123456789abcdeg", "0123456789abcdef0123456789abcdef ",
    "../0123456789abcdef0123456789abc", None, 12345, b"0" * 32,
])
def test_anything_but_a_uuid4_hex_run_id_is_rejected(bad):
    with pytest.raises(ValueError):
        guestprov.validate_run_id(bad)


# ------------------------------------------------------------------ staging --

def test_staging_copies_the_allowlist_then_writes_the_trigger_last(tmp_path):
    payloads = {}

    def on_input(argv, payload):
        payloads["trigger"] = json.loads(payload.decode("utf-8"))

    docker = healthy_channel((READ_STATUS, ABSENT_STATUS), on_input=on_input)
    guestprov.stage(make_bundle(tmp_path), RUN_ID, True, docker.runner,
                    docker.input_runner)

    for name in guestprov.PAYLOAD_FILES:
        assert f"docker cp {bundle_source(tmp_path, name)} " \
               f"icloud-windows:/run/icloud-bridge-provision/.tmp-{name}" \
               in docker.transcript
        assert docker.index_of(f".tmp-{name}") < docker.index_of(PROMOTE)
    assert docker.index_of(PROMOTE) < docker.index_of(WRITE_TRIGGER)
    # The trigger's atomic rename is the very last thing that happens: the
    # watcher polls for it and nothing else.
    assert docker.commands[-1] == docker.commands[docker.index_of(WRITE_TRIGGER)]
    assert payloads["trigger"] == {"version": 1, "runId": RUN_ID,
                                   "action": "reconcile",
                                   "resetShareCredential": True}


def test_staging_empties_the_inbox_before_copying_anything(tmp_path):
    docker = healthy_channel((READ_STATUS, ABSENT_STATUS))
    guestprov.stage(make_bundle(tmp_path), RUN_ID, False, docker.runner,
                    docker.input_runner)
    assert docker.index_of(RESET_INBOX) < docker.index_of("docker cp")


def test_the_run_id_never_enters_shell_source_or_argv(tmp_path):
    """It travels inside the trigger document, on stdin."""
    docker = healthy_channel((READ_STATUS, ABSENT_STATUS))
    guestprov.stage(make_bundle(tmp_path), RUN_ID, False, docker.runner,
                    docker.input_runner)
    assert RUN_ID not in docker.transcript


def test_staging_carries_no_env_path_and_no_secret(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"SHARE_PASS={SECRET}\n", encoding="utf-8")
    docker = healthy_channel((READ_STATUS, ABSENT_STATUS))
    guestprov.stage(make_bundle(tmp_path), RUN_ID, False, docker.runner,
                    docker.input_runner)
    assert SECRET not in docker.transcript
    assert str(env) not in docker.transcript
    assert docker.input_sizes == [len(json.dumps(
        guestprov.trigger_document(RUN_ID, False), separators=(",", ":")))]


@pytest.mark.parametrize("value", [1, 0, "true", None, 1.0])
def test_the_reset_credential_flag_must_be_a_real_bool(tmp_path, value):
    """A JSON `1` is not a JSON `true`, and this flag decides whether a working
    share password is reset."""
    docker = healthy_channel((READ_STATUS, ABSENT_STATUS))
    with pytest.raises(ValueError):
        guestprov.stage(make_bundle(tmp_path), RUN_ID, value, docker.runner,
                        docker.input_runner)
    assert WRITE_TRIGGER not in docker.transcript


def test_staging_validates_the_run_id_before_touching_the_container(tmp_path):
    docker = healthy_channel((READ_STATUS, ABSENT_STATUS))
    with pytest.raises(ValueError):
        guestprov.stage(make_bundle(tmp_path), "not-a-run-id", False,
                        docker.runner, docker.input_runner)
    assert docker.calls == []


def test_an_acknowledged_active_run_is_never_staged_over(tmp_path):
    live = status_result(status_document(OTHER_RUN_ID,
                                         phase=guestprov.PHASE_CREATING_SHARE))
    docker = healthy_channel((READ_STATUS, live))
    with pytest.raises(guestprov.ProvisioningError) as excinfo:
        guestprov.stage(make_bundle(tmp_path), RUN_ID, False, docker.runner,
                        docker.input_runner)
    assert OTHER_RUN_ID in str(excinfo.value)
    assert RESET_INBOX not in docker.transcript


@pytest.mark.parametrize("document", [
    status_document(OTHER_RUN_ID, phase=guestprov.PHASE_DONE),
    status_document(OTHER_RUN_ID, phase=guestprov.PHASE_VERIFYING,
                    error="it went wrong"),
    status_document(RUN_ID, phase=guestprov.PHASE_STAGING),
])
def test_a_finished_or_own_run_does_not_block_staging(tmp_path, document):
    """D43 retries a crashed `staging` with the *same* run ID, and a terminal
    run of someone else's is simply over."""
    docker = healthy_channel((READ_STATUS, status_result(document)))
    guestprov.stage(make_bundle(tmp_path), RUN_ID, False, docker.runner,
                    docker.input_runner)
    assert WRITE_TRIGGER in docker.transcript


def test_a_failed_copy_stops_before_the_trigger_exists(tmp_path):
    docker = healthy_channel((READ_STATUS, ABSENT_STATUS),
                             ("docker cp", power.RunResult(1, "", "no such file")))
    with pytest.raises(guestprov.ProvisioningError):
        guestprov.stage(make_bundle(tmp_path), RUN_ID, False, docker.runner,
                        docker.input_runner)
    assert WRITE_TRIGGER not in docker.transcript


def test_staging_refuses_while_the_share_is_not_verified_read_only(tmp_path):
    docker = healthy_channel((VERIFY_READ_ONLY, power.RunResult(0, "No\n", "")))
    with pytest.raises(guestprov.ProvisioningError):
        guestprov.stage(make_bundle(tmp_path), RUN_ID, False, docker.runner,
                        docker.input_runner)
    assert "docker cp" not in docker.transcript


# ------------------------------------------------------ delivering the secret --

def waiting_docker(*extra, on_input=None, secret_present=False):
    waiting = status_result(status_document(
        RUN_ID, phase=guestprov.PHASE_WAITING_FOR_SECRET))
    return healthy_channel(
        *extra,
        (READ_STATUS, waiting),
        (SECRET_PRESENT, power.RunResult(0 if secret_present else 1, "", "")),
        on_input=on_input)


def write_env(tmp_path, value=SECRET) -> str:
    path = tmp_path / ".env"
    path.write_text(f"DISK_SIZE=120G\nRAM_SIZE=3G\nCPU_CORES=2\n"
                    f"SHARE_PASS={value}\n", encoding="utf-8")
    return str(path)


def test_the_secret_goes_over_stdin_and_appears_in_no_argv(tmp_path):
    checked = {}

    def on_input(argv, payload):
        # Asserted here, inside the call: nothing outside keeps these bytes.
        assert payload == SECRET.encode("utf-8")
        assert not any(SECRET in part for part in argv)
        checked["value"] = True

    docker = waiting_docker(on_input=on_input)
    assert guestprov.deliver_secret(write_env(tmp_path), RUN_ID,
                                    docker.input_runner, runner=docker.runner) is True
    assert checked["value"]
    assert WRITE_SECRET in docker.transcript
    assert SECRET not in docker.transcript


def test_the_delivered_bytes_are_exact_utf8_with_no_added_newline(tmp_path):
    value = "pässwörd with spaces#and=signs"
    docker = waiting_docker()
    guestprov.deliver_secret(write_env(tmp_path, value), RUN_ID,
                             docker.input_runner, runner=docker.runner)
    assert docker.input_sizes == [len(value.encode("utf-8"))]


def test_the_secret_file_is_written_0600_and_renamed_into_place():
    assert "umask 077" in guestprov._WRITE_SECRET_COMMAND
    assert "chmod 600" in guestprov._WRITE_SECRET_COMMAND
    assert guestprov._WRITE_SECRET_COMMAND.rstrip().endswith(
        f"mv -f .tmp-secret {guestprov.SECRET_NAME}")


@pytest.mark.parametrize("phase", [
    guestprov.PHASE_INSPECTING, guestprov.PHASE_WAITING_FOR_SIGNIN,
    guestprov.PHASE_CREATING_SHARE, guestprov.PHASE_DONE,
])
def test_the_secret_is_refused_unless_the_guest_is_waiting_for_it(tmp_path, phase):
    docker = healthy_channel(
        (READ_STATUS, status_result(status_document(RUN_ID, phase=phase))))
    with pytest.raises(guestprov.ProvisioningError):
        guestprov.deliver_secret(write_env(tmp_path), RUN_ID, docker.input_runner,
                                 runner=docker.runner)
    assert docker.input_sizes == []


def test_a_secret_for_another_run_is_never_delivered(tmp_path):
    docker = healthy_channel((READ_STATUS, status_result(status_document(
        OTHER_RUN_ID, phase=guestprov.PHASE_WAITING_FOR_SECRET))))
    with pytest.raises(guestprov.ProvisioningError):
        guestprov.deliver_secret(write_env(tmp_path), RUN_ID, docker.input_runner,
                                 runner=docker.runner)
    assert docker.input_sizes == []


def test_an_already_present_secret_is_never_overwritten(tmp_path):
    docker = waiting_docker(secret_present=True)
    assert guestprov.deliver_secret(write_env(tmp_path), RUN_ID,
                                    docker.input_runner, runner=docker.runner) is False
    assert docker.input_sizes == []
    assert WRITE_SECRET not in docker.transcript


@pytest.mark.parametrize("text", [
    "SHARE_PASS=a\nSHARE_PASS=b\n",
    "SHARE_PASS='quoted'\n",
    "SHARE_PASS= leading\n",
    "SHARE_PASS=\n",
    "DISK_SIZE=1G\n",
])
def test_a_rejected_env_file_delivers_nothing(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    docker = waiting_docker()
    with pytest.raises(envfile.EnvError):
        guestprov.deliver_secret(str(path), RUN_ID, docker.input_runner,
                                 runner=docker.runner)
    assert docker.input_sizes == []


def test_a_failed_delivery_is_reported_without_quoting_the_value(tmp_path):
    docker = waiting_docker((WRITE_SECRET, power.RunResult(1, "", "no space left")))
    with pytest.raises(guestprov.ProvisioningError) as excinfo:
        guestprov.deliver_secret(write_env(tmp_path), RUN_ID, docker.input_runner,
                                 runner=docker.runner)
    assert "no space left" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


# ------------------------------------------------------------------ polling --

def test_the_status_read_is_bounded_to_the_documented_size():
    assert str(guestprov.MAX_STATUS_BYTES) in guestprov._READ_STATUS_COMMAND
    assert guestprov.MAX_STATUS_BYTES == 64 * 1024


def test_no_status_yet_is_absent_not_an_error():
    docker = FakeDocker((READ_STATUS, ABSENT_STATUS))
    status = guestprov.poll(docker.runner, RUN_ID)
    assert status.phase == guestprov.PHASE_ABSENT
    assert status.acknowledged is False
    assert status.readable is False


def test_a_matching_status_is_classified_with_its_checklist_and_work():
    checks = {key: "ok" for key in guestprov.CHECK_KEYS}
    checks["shareCredential"] = "unverifiable"
    checks["agentInstall"] = "drifted"
    document = status_document(RUN_ID, phase=guestprov.PHASE_INSTALLING_AGENT,
                               checks=checks, work=["update-agent"],
                               detail="installing the bridge agent")
    docker = FakeDocker((READ_STATUS, status_result(document, mtime=4242.0)))
    status = guestprov.poll(docker.runner, RUN_ID)
    assert status.phase == guestprov.PHASE_INSTALLING_AGENT
    assert status.acknowledged is True
    assert status.detail == "installing the bridge agent"
    assert status.checks["agentInstall"] == "drifted"
    assert status.work == ("update-agent",)
    assert status.mtime == 4242.0
    assert status.terminal is False


def test_a_mismatched_run_id_is_no_acknowledgement_rather_than_progress():
    document = status_document(OTHER_RUN_ID, phase=guestprov.PHASE_DONE)
    docker = FakeDocker((READ_STATUS, status_result(document)))
    status = guestprov.poll(docker.runner, RUN_ID)
    assert status.phase == guestprov.PHASE_STALE
    assert status.acknowledged is False
    assert status.terminal is False
    # The other run's phase is kept only so `stage` can see it is still live.
    assert status.document_phase == guestprov.PHASE_DONE
    assert status.checks == {}
    assert status.work == ()


def test_a_done_status_is_terminal_and_an_error_status_is_a_failure():
    docker = FakeDocker((READ_STATUS, status_result(
        status_document(RUN_ID, phase=guestprov.PHASE_DONE))))
    assert guestprov.poll(docker.runner, RUN_ID).terminal is True

    failed = FakeDocker((READ_STATUS, status_result(status_document(
        RUN_ID, phase=guestprov.PHASE_CREATING_SHARE, error="icacls failed"))))
    status = guestprov.poll(failed.runner, RUN_ID)
    assert status.failed is True
    assert status.terminal is True
    assert status.error == "icacls failed"
    assert status.phase == guestprov.PHASE_CREATING_SHARE


@pytest.mark.parametrize("body", [
    "not json at all",
    "[1, 2, 3]",
    "null",
    '{"version": 1}',
])
def test_malformed_json_is_unreadable_never_a_crash_and_never_progress(body):
    docker = FakeDocker((READ_STATUS, status_result(body)))
    status = guestprov.poll(docker.runner, RUN_ID)
    assert status.phase == guestprov.PHASE_UNREADABLE
    assert status.acknowledged is False
    assert status.readable is False
    assert status.reason


def test_an_oversized_status_is_unreadable_without_being_parsed():
    docker = FakeDocker((READ_STATUS, status_result(
        status_document(RUN_ID), size=guestprov.MAX_STATUS_BYTES + 1)))
    status = guestprov.poll(docker.runner, RUN_ID)
    assert status.phase == guestprov.PHASE_UNREADABLE
    assert "exceeds" in status.reason


def test_unreadable_metadata_does_not_become_a_status():
    docker = FakeDocker((READ_STATUS, power.RunResult(0, "garbage\n{}", "")))
    assert guestprov.poll(docker.runner, RUN_ID).phase == guestprov.PHASE_UNREADABLE


def test_a_container_command_failure_is_unreadable_not_an_exception():
    docker = FakeDocker((READ_STATUS, power.RunResult(1, "", "container gone")))
    assert guestprov.poll(docker.runner, RUN_ID).phase == guestprov.PHASE_UNREADABLE

    class Exploding:
        def __call__(self, argv, timeout):
            raise TimeoutError("docker timed out")

    assert guestprov.poll(Exploding(), RUN_ID).phase == guestprov.PHASE_UNREADABLE


def mutate(**changes):
    document = status_document(RUN_ID)
    document.update(changes)
    return document


@pytest.mark.parametrize("document", [
    mutate(version=2),
    mutate(version="1"),
    mutate(version=True),
    mutate(runId="short"),
    mutate(runId=None),
    mutate(phase="doing-something-new"),
    mutate(phase=None),
    mutate(detail=None),
    mutate(detail=["a"]),
    mutate(error=7),
    mutate(updatedAt=None),
    mutate(checks={"icloudPackage": "ok"}),                       # incomplete
    mutate(checks={**{k: "ok" for k in guestprov.CHECK_KEYS},
                   "extra": "ok"}),                               # extra key
    mutate(checks={k: "excellent" for k in guestprov.CHECK_KEYS}),  # bad state
    mutate(checks="ok"),
    mutate(work="update-agent"),
    mutate(work=["reinstall-windows"]),
    mutate(work=["update-agent", "update-agent"]),
    mutate(work=["update-agent"] * 9),
])
def test_the_complete_key_set_states_work_ids_and_types_are_validated(document):
    assert guestprov.classify_status(document, RUN_ID).phase == \
        guestprov.PHASE_UNREADABLE


def test_every_documented_phase_and_check_state_is_accepted():
    for phase in guestprov.PHASES:
        for state in sorted(guestprov.CHECK_STATES):
            document = status_document(
                RUN_ID, phase=phase,
                checks={key: state for key in guestprov.CHECK_KEYS},
                work=list(guestprov.WORK_IDS))
            status = guestprov.classify_status(document, RUN_ID)
            assert status.phase == phase
            assert status.work == guestprov.WORK_IDS


def test_guest_supplied_text_is_sanitized_and_bounded():
    document = status_document(RUN_ID, detail="a" * 900 + "\x1b[31mred",
                               error="b\x00c" + "d" * 900)
    status = guestprov.classify_status(document, RUN_ID)
    assert len(status.detail) <= power.MAX_LINE_CHARS + 1
    assert "\x1b" not in status.detail
    assert len(status.error) <= power.MAX_LINE_CHARS + 1
    assert "\x00" not in status.error


def test_polling_validates_the_run_id_it_is_asked_about():
    docker = FakeDocker()
    with pytest.raises(ValueError):
        guestprov.poll(docker.runner, "nonsense")
    assert docker.calls == []


# --------------------------------------------------- deadlines and heartbeat --

def test_each_phase_carries_the_documented_deadline():
    assert guestprov.phase_deadline(guestprov.PHASE_INSTALLING_ICLOUD) == 600.0
    assert guestprov.phase_deadline(guestprov.PHASE_WAITING_FOR_SIGNIN) is None
    assert guestprov.phase_deadline(guestprov.PHASE_WAITING_FOR_SECRET) is None
    for phase in guestprov.PHASES:
        if phase in guestprov.WAITING_PHASES:
            continue
        if phase == guestprov.PHASE_INSTALLING_ICLOUD:
            continue
        assert guestprov.phase_deadline(phase) == 300.0


def test_a_frozen_heartbeat_warns_in_any_active_phase_including_the_waits():
    for phase in (guestprov.PHASE_INSPECTING, guestprov.PHASE_WAITING_FOR_SIGNIN,
                  guestprov.PHASE_WAITING_FOR_SECRET):
        assert guestprov.stall_reason(phase, phase_elapsed=10.0,
                                      since_update=121.0)
        assert guestprov.stall_reason(phase, phase_elapsed=10.0,
                                      since_update=119.0) == ""


def test_the_sign_in_wait_never_times_out_on_elapsed_time():
    """It is the operator's manual step; only silence is meaningful."""
    assert guestprov.stall_reason(guestprov.PHASE_WAITING_FOR_SIGNIN,
                                  phase_elapsed=86_400.0, since_update=5.0) == ""


def test_an_overrunning_phase_warns_but_only_past_its_own_deadline():
    assert guestprov.stall_reason(guestprov.PHASE_INSTALLING_ICLOUD,
                                  phase_elapsed=599.0, since_update=1.0) == ""
    assert guestprov.stall_reason(guestprov.PHASE_INSTALLING_ICLOUD,
                                  phase_elapsed=601.0, since_update=1.0)
    assert guestprov.stall_reason(guestprov.PHASE_VERIFYING,
                                  phase_elapsed=301.0, since_update=1.0)


def test_a_finished_or_unclassifiable_run_is_never_called_stalled():
    for phase in (guestprov.PHASE_DONE, guestprov.PHASE_ABSENT,
                  guestprov.PHASE_UNREADABLE, guestprov.PHASE_STALE):
        assert guestprov.stall_reason(phase, phase_elapsed=99_999.0,
                                      since_update=99_999.0) == ""


# ------------------------------------------------------------------ cleanup --

def trigger_result(run_id=RUN_ID):
    return power.RunResult(0, json.dumps(guestprov.trigger_document(run_id, False)), "")


def test_cleanup_removes_the_trigger_payload_and_secret():
    docker = FakeDocker((READ_TRIGGER, trigger_result()))
    assert guestprov.cleanup(docker.runner, RUN_ID) is True
    removal = docker.commands[-1]
    assert f"rm -f {guestprov.TRIGGER_NAME} {guestprov.SECRET_NAME}" in removal
    for name in guestprov.PAYLOAD_FILES:
        assert name in removal


def test_cleanup_never_touches_the_status_outbox():
    """D43 needs the matching terminal status to survive a GUI crash."""
    docker = FakeDocker((READ_TRIGGER, trigger_result()))
    guestprov.cleanup(docker.runner, RUN_ID)
    assert guestprov.STATUS_DIR not in docker.transcript


def test_cleanup_leaves_another_runs_inbox_completely_alone():
    docker = FakeDocker((READ_TRIGGER, trigger_result(OTHER_RUN_ID)))
    assert guestprov.cleanup(docker.runner, RUN_ID) is False
    assert "rm -f" not in docker.transcript


def test_cleanup_proceeds_when_there_is_no_trigger_left():
    docker = FakeDocker((READ_TRIGGER, power.RunResult(9, "", "")))
    assert guestprov.cleanup(docker.runner, RUN_ID) is True


def test_an_unparseable_trigger_does_not_stop_cleanup():
    docker = FakeDocker((READ_TRIGGER, power.RunResult(0, "{not json", "")))
    assert guestprov.cleanup(docker.runner, RUN_ID) is True


def test_cleanup_is_best_effort_and_never_raises():
    class Exploding:
        def __call__(self, argv, timeout):
            raise FileNotFoundError("docker")

    assert guestprov.cleanup(Exploding(), RUN_ID) is False


# ------------------------------------------------------------ the RDP probe --

TPKT_REPLY = bytes([0x03, 0x00, 0x00, 0x13]) + bytes(15)


class FakeSocket:
    def __init__(self, reply=TPKT_REPLY, error=None):
        self.reply = reply
        self.error = error
        self.sent = b""
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, payload):
        if self.error is not None:
            raise self.error
        self.sent += payload

    def recv(self, size):
        return self.reply[:size]

    def close(self):
        self.closed = True


def connector(sock):
    def connect(address, timeout=None):
        connect.address = address
        connect.timeout = timeout
        return sock
    return connect


def test_a_tpkt_response_means_windows_is_up():
    sock = FakeSocket()
    assert guestprov.guest_os_ready(connect=connector(sock)) is True
    # A real X.224 connection request, not a bare TCP connect: docker-proxy
    # accepts the connection while Windows is still installing.
    assert sock.sent == guestprov._X224_CONNECTION_REQUEST
    assert sock.closed is True


@pytest.mark.parametrize("reply", [b"", b"\x03", b"HTTP/1.1 400", b"\x04\x00\x00\x13"])
def test_anything_that_is_not_tpkt_means_not_ready(reply):
    sock = FakeSocket(reply=reply)
    assert guestprov.guest_os_ready(connect=connector(sock)) is False
    assert sock.closed is True


def test_a_refused_or_timed_out_connection_is_simply_not_ready():
    def refuse(address, timeout=None):
        raise ConnectionRefusedError("nobody is listening")

    assert guestprov.guest_os_ready(connect=refuse) is False
    sock = FakeSocket(error=TimeoutError("no answer"))
    assert guestprov.guest_os_ready(connect=connector(sock)) is False


def test_the_probe_targets_loopback_by_default():
    sock = FakeSocket()
    connect = connector(sock)
    guestprov.guest_os_ready(connect=connect)
    assert connect.address == ("127.0.0.1", 3389)
    assert sock.timeout == guestprov.RDP_TIMEOUT_SECONDS
