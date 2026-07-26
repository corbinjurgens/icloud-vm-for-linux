"""Shell-level coverage for one factored seam of ``host/icloud-bridge-power``.

The helper as a whole needs root, systemd, CIFS mounts and a Docker daemon, so
it cannot run here.  ``classify_inspect_output`` is deliberately a pure function
of one string, which lets these tests extract it and run it under plain bash —
that is the only part of the helper a checkout can prove.  Everything else about
the helper remains operator-verified on the real host.
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


def classify(message: str) -> str:
    script = _extract_function("classify_inspect_output") + '\nclassify_inspect_output "$1"\n'
    completed = subprocess.run(["bash", "-c", script, "bash", message],
                               capture_output=True, text=True, check=True)
    return completed.stdout.strip()


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
