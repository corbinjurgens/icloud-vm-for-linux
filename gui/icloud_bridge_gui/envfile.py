"""The one grammar for the operator's env file (v2 plan D31 and D41).

This is a module of its own rather than a corner of :mod:`firstrun` because
three independent readers must agree, byte for byte, on what a ``SHARE_PASS=``
line means:

* :mod:`firstrun`, which reports on the file and must never see the value;
* :mod:`guestprov`, the only GUI module allowed to *return* that value (D41);
* ``host/icloud-bridge-configure``, which writes ``/etc/credentials-icloud``.

Neither of the first two owns the grammar: putting it in ``firstrun`` would put
the value-returning code inside the module documented as never handling the
password, and putting it in ``guestprov`` would make the first-run assistant
depend on the provisioning machinery to validate a file it checks long before
any provisioning run exists.  Sharing one definition is a correctness
requirement rather than tidiness — if the Windows account and the host mount
credential were derived by two slightly different parsers, the bridge would fail
to mount with a password that looks correct at both ends.

The ``SHARE_PASS`` grammar is deliberately tiny (v2 plan section 4, D41):
exactly one physical line beginning ``SHARE_PASS=`` in column 1; every byte
after the first ``=`` is the value, so ``#`` and further ``=`` are data; no
quote processing, no leading or trailing whitespace, no NUL and no CR.
Duplicate or quoted forms are *rejected* rather than reinterpreted, because the
only safe response to an ambiguous password is to refuse it.

Qt-free and free of any I/O except the injected reader; the env file is parsed
as text and never sourced as shell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

#: Keys the compose file interpolates, plus the share password.
REQUIRED_ENV_KEYS = ("DISK_SIZE", "RAM_SIZE", "CPU_CORES", "SHARE_PASS")

#: Values that mean "the operator has not chosen a password yet".  Reported by
#: name only; no code path here ever prints or logs the value itself.
PLACEHOLDER_PASSWORDS = frozenset({"CHANGE_ME_STRONG_PASSWORD", "STRONG_PASSWORD_HERE"})

SHARE_PASS_KEY = "SHARE_PASS"
SHARE_PASS_PREFIX = SHARE_PASS_KEY + "="

#: A line that *means* to set the password but does not match the grammar —
#: indentation, or spaces around the ``=``.  Recognised only to give a precise
#: message; it is still a rejection.  ``host/icloud-bridge-configure`` mirrors
#: this exactly.
_LOOSE_SHARE_PASS_RE = re.compile(r"^[ \t]*" + SHARE_PASS_KEY + r"[ \t]*=")

_MAX_ENV_BYTES = 64 * 1024


class EnvError(Exception):
    """The env file cannot yield a usable ``SHARE_PASS``.

    Its message names the file and the problem, never the value.
    """


@dataclass
class EnvReport:
    """What the operator's env file contains — never *what* the password is."""
    path: str
    problems: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def read_file_text(path: str, *, limit: int = _MAX_ENV_BYTES) -> str:
    """Read a bounded amount of text; the caller handles ``OSError``.

    ``newline=""`` matters: universal-newline translation would turn a CRLF file
    into an acceptable one here while ``host/icloud-bridge-configure``, which
    reads bytes, still rejected it — and a password differing by one invisible
    byte between the guest and the host mount is exactly what this grammar
    exists to prevent.
    """
    with open(path, encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read(limit)


# ------------------------------------------------------ the SHARE_PASS rule --

def share_pass_problems(raw: str) -> tuple[str, list[str]]:
    """``(value, problems)`` for one env file's text.

    The value is returned only so :func:`read_share_pass` can hand it to
    ``guestprov``; every other caller takes the problems and discards it.  A
    non-empty problem list always means the value is unusable.
    """
    lines = raw.split("\n")
    exact = [line for line in lines if line.startswith(SHARE_PASS_PREFIX)]
    if len(exact) > 1:
        return "", [f"{SHARE_PASS_KEY} is set on more than one line; exactly one "
                    "line is allowed"]
    if not exact:
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            if _LOOSE_SHARE_PASS_RE.match(line):
                return "", [f"{SHARE_PASS_KEY} must start in column 1 with no "
                            "spaces around '='"]
        return "", [f"{SHARE_PASS_KEY} is missing"]

    value = exact[0][len(SHARE_PASS_PREFIX):]
    problems: list[str] = []
    if "\x00" in value:
        problems.append(f"{SHARE_PASS_KEY} contains a NUL byte")
    elif "\r" in value:
        problems.append(f"{SHARE_PASS_KEY} contains a carriage return; this file "
                        "must use LF line endings")
    elif not value:
        problems.append(f"{SHARE_PASS_KEY} is empty")
    elif value != value.strip():
        problems.append(f"{SHARE_PASS_KEY} has leading or trailing whitespace; the "
                        "value is used exactly as written")
    elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        problems.append(f"{SHARE_PASS_KEY} is quoted; the quotes would become part "
                        "of the password — remove them")
    elif value in PLACEHOLDER_PASSWORDS:
        problems.append(f"{SHARE_PASS_KEY} is still the placeholder — set a strong "
                        "password (20+ random characters) before creating the share")
    return ("" if problems else value), problems


def read_env_file(path: str, *,
                  read_text: Callable[[str], str] | None = None) -> EnvReport:
    """Validate an env file as *text*.

    Never sourced as shell — that would execute whatever is in it — and no
    value is ever returned.  Only the key names come back, plus problems named
    by key.
    """
    report = EnvReport(path=path)
    try:
        raw = (read_text or read_file_text)(path)
    except OSError as exc:
        report.problems.append(f"cannot read {path}: {exc}")
        return report

    values: dict[str, str] = {}
    for number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            report.problems.append(f"line {number} is not KEY=value")
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            report.problems.append(f"line {number} has an empty key")
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value

    report.keys = sorted(values)
    # The compose-interpolated keys keep Compose's own tolerant reading; only
    # SHARE_PASS is held to the strict shared grammar, because only it has to
    # arrive byte-identical in two different places.
    for key in REQUIRED_ENV_KEYS:
        if key == SHARE_PASS_KEY:
            continue
        if key not in values:
            report.problems.append(f"{key} is missing")
        elif not values[key]:
            report.problems.append(f"{key} is empty")
    report.problems.extend(share_pass_problems(raw)[1])
    return report


def read_share_pass(path: str, *,
                    read_text: Callable[[str], str] | None = None) -> str:
    """The exact ``SHARE_PASS`` value, or raise :class:`EnvError`.

    Called only by :mod:`guestprov`, immediately before the value is streamed
    into the container over stdin (D41).  The caller must not log, store, or
    display the return value.
    """
    try:
        raw = (read_text or read_file_text)(path)
    except OSError as exc:
        raise EnvError(f"cannot read {path}: {exc}") from exc
    value, problems = share_pass_problems(raw)
    if problems:
        raise EnvError(f"{path}: " + "; ".join(problems))
    return value
