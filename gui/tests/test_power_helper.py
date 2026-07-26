"""Shell-level coverage for the factored seams of ``host/icloud-bridge-power``.

The helper as a whole needs root, systemd, CIFS mounts and a Docker daemon, so
it cannot run here.  ``classify_inspect_output`` and ``sanitize_journal_excerpt``
are deliberately pure functions of one string, which lets these tests extract
them and run them under plain bash — that is the whole of what a checkout can
prove.  Everything else about the helper remains operator-verified on the real
host.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HELPER = os.path.join(REPO, "host", "icloud-bridge-power")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def _extract_function(name: str) -> str:
    """Pull one top-level shell function out of the helper by its brace block."""
    with open(HELPER, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _run_function(name: str, argument: str) -> str:
    """Run one extracted helper function under the helper's own shell options."""
    script = f"set -euo pipefail\n{_extract_function(name)}\n{name} \"$1\"\n"
    completed = subprocess.run(["bash", "-c", script, "bash", argument],
                               capture_output=True, text=True, check=True)
    return completed.stdout


def classify(message: str) -> str:
    return _run_function("classify_inspect_output", message).strip()


def excerpt(journal: str) -> str:
    return _run_function("sanitize_journal_excerpt", journal)


#: One mount unit's journal for a single failed attempt, in the order systemd
#: and mount.cifs actually emit it.
def _attempt(error: str) -> str:
    return (
        "Mounting /mnt/icloud...\n"
        f"{error}\n"
        "Refer to the mount.cifs(8) manual page (e.g. man mount.cifs) and "
        "kernel log messages (dmesg)\n"
        "mnt-icloud.mount: Mount process exited, code=exited, status=32/n/a\n"
        "mnt-icloud.mount: Failed with result 'exit-code'.\n"
        "Failed to mount /mnt/icloud.\n"
        "Unmounted /mnt/icloud.\n"
    )


@pytest.mark.parametrize("message", [
    # Docker <= 28 capitalized both words; 29 lowercases the whole line. Both
    # must classify as absent, or `off` loses its documented "a missing
    # container is already off" idempotency on Docker 29.
    "Error: No such object: icloud-windows",
    "error: no such object: icloud-windows",
    "Error: No such container: icloud-windows",
    "ERROR: NO SUCH OBJECT: ICLOUD-WINDOWS",
])
def test_absent_is_recognized_whatever_the_casing(message):
    assert classify(message) == "absent"


def test_daemon_failure_keeps_its_message_and_is_not_absent():
    message = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    assert classify(message) == f"error:{message}"


def test_the_helper_pins_the_native_socket():
    with open(HELPER, encoding="utf-8") as handle:
        body = handle.read()
    assert "DOCKER_HOST=unix:///var/run/docker.sock" in body
    assert "export DOCKER_HOST" in body


# ------------------------------------- the readiness-failure excerpt (D45) --

def test_a_rejected_credential_survives_into_the_excerpt():
    """The whole point: the GUI can only route what reaches its stderr.

    `mount error(13): Permission denied` is what a wrong share password looks
    like from the host, and "permission denied" is one of the controller's
    CREDENTIAL_FAILURE_MARKERS.  Losing it here would leave a wrong password
    indistinguishable from a slow guest.
    """
    text = excerpt(_attempt("mount error(13): Permission denied"))
    assert "mount error(13): Permission denied" in text
    assert "permission denied" in text.lower()


def test_a_missing_share_stays_a_generic_failure():
    """The unprovisioned-guest case must not read as a credential failure."""
    text = excerpt(_attempt("mount error(2): No such file or directory"))
    assert "mount error(2)" in text
    assert "permission denied" not in text.lower()


def test_ordinary_unit_chatter_is_dropped():
    text = excerpt(_attempt("mount error(13): Permission denied"))
    for noise in ("Mounting /mnt/icloud...", "manual page", "Unmounted",
                  "Failed with result", "Failed to mount"):
        assert noise not in text


def test_the_newest_attempts_win_over_older_ones():
    """After five minutes of retries, the last diagnoses are the real ones.

    Two attempts fit; a third pushes the oldest out rather than truncating the
    newest, which is why the excerpt keeps its tail and not its head.
    """
    text = excerpt(_attempt("mount error(2): No such file or directory")
                   + _attempt("mount error(13): Permission denied") * 2)
    assert "mount error(13): Permission denied" in text
    assert "mount error(2)" not in text


def test_the_excerpt_is_bounded_in_lines_and_width():
    lines = excerpt(_attempt("mount error(13): Permission denied") * 20).splitlines()
    assert 0 < len(lines) <= 4
    assert all(len(line) <= 204 for line in lines)      # four-space indent + 200


def test_a_password_bearing_option_is_redacted_whatever_its_case():
    text = excerpt("mount error(13): opts=user=syncshare,PassWord=hunter2,vers=3.1.1")
    assert "hunter2" not in text
    assert "<redacted>" in text
    assert "user=syncshare" in text


def test_nothing_diagnostic_produces_no_excerpt():
    assert excerpt("Mounting /mnt/icloud...\nUnmounted /mnt/icloud.\n") == ""
    assert excerpt("") == ""


def test_the_readiness_failure_quotes_the_excerpt():
    """The pure function is only useful if the timeout path actually uses it."""
    with open(HELPER, encoding="utf-8") as handle:
        body = handle.read()
    assert 'detail=$(mount_failure_excerpt) || detail=""' in body
    assert 'unverified mount.${detail}"' in body
