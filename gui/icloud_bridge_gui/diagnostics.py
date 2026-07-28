"""A privacy-safe diagnostic report for support (v2 plan D37).

Collecting a bug report by hand today means reading GUI rows, `docker inspect`,
`systemctl`, the bridge JSON and the journal, and typing the interesting bits
somewhere. This module produces that report in one action — without ever
producing something the operator would regret pasting into an issue.

The safety property is structural, not a filter. :func:`collect` takes a typed,
allowlisted :class:`Facts` dataclass, so a field the controller does not
explicitly copy in **cannot** reach the report:

* no raw `status.json`, `tree.json`, exception object or process result;
* no `.env`, `/etc/credentials-icloud`, `SHARE_PASS` or process environment —
  there is no opt-in for those;
* no raw `lastError`, raw health detail or raw subprocess output, because none
  of those can be reliably scanned for filenames after the fact. Only the
  *classified* result is rendered;
* real paths are replaced by stable `<path-N>` placeholders unless the operator
  explicitly asks for **Include folder names**;
* no Safe Workspace name, local folder, iCloud folder, relative file path,
  Unison output, log excerpt or `status.json` field. That feature contributes
  **counts and one timestamp** and nothing else, excluded at the dataclass
  rather than scrubbed downstream (`docs/plan-safe-local-workspaces.md` §12.2).

It is Qt-free and performs no mount I/O. The only subprocesses it may run are
`systemctl is-active` on the bridge's own units and the two argument-exact
`sudo -n -l` authorization probes, none of which touch CIFS — which is why a
report still works in the no-CIFS states, where reports matter most.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import __version__
from .power import HELPER_PATH, RunResult, Runner, default_runner

#: Per-field and whole-report bounds.
MAX_FIELD_CHARS = 2000
TRUNCATION_SUFFIX = "[truncated]"
MAX_REPORT_BYTES = 64 * 1024

SYSTEMCTL_TIMEOUT_SECONDS = 5

#: The six units the bridge installs, in the order the helper drives them.
UNITS = (
    "mnt-icloud.automount",
    "mnt-icloud.mount",
    "mnt-icloud_bridge.automount",
    "mnt-icloud_bridge.mount",
    "icloud-health.timer",
    "icloud-health.service",
)

#: The two argument-exact authorization probes, matching the sudoers grant.
SUDO_SPECS = (f"{HELPER_PATH} on", f"{HELPER_PATH} off")

#: Coarse install origins. `firstrun` calls its per-user case `user`; the report
#: says `per-user`, and neither ever carries a checkout or home path.
ORIGIN_PACKAGE = "package"
ORIGIN_PER_USER = "per-user"
ORIGIN_SOURCE = "source"
ORIGIN_OVERRIDE = "override"
ORIGIN_UNKNOWN = "unknown"

_ORIGIN_MAP = {
    "package": ORIGIN_PACKAGE,
    "user": ORIGIN_PER_USER,
    "per-user": ORIGIN_PER_USER,
    "source": ORIGIN_SOURCE,
    "override": ORIGIN_OVERRIDE,
}

#: Anything shaped like `key=secret` / `password: secret` in the one free-text
#: field that is admitted (the bounded helper diagnostic, which is the helper's
#: own unlocalized message). Belt and braces on top of the allowlist.
#: The leading `\w*` matters: the name to catch is often a compound such as
#: `SHARE_PASS`, where a plain word boundary would not match at all.
_CREDENTIAL_RE = re.compile(
    r"(?i)\w*(?:password|passwd|pass|secret|token|credentials?|key)\s*[:=]\s*\S+")


def normalize_origin(value: object) -> str:
    """Map an install origin to its coarse, path-free name."""
    if isinstance(value, str):
        return _ORIGIN_MAP.get(value.strip().lower(), ORIGIN_UNKNOWN)
    return ORIGIN_UNKNOWN


# ------------------------------------------------------------------- facts --

@dataclass(frozen=True)
class HealthRow:
    """One health row, reduced to what is safe: its name and its severity.

    The `detail` string is deliberately absent. It is human prose assembled from
    agent output, and no rule can reliably decide whether a given sentence
    contains a filename.
    """

    name: str
    severity: str


@dataclass(frozen=True)
class DocumentFacts:
    """Versions, timestamps and revisions of the bridge documents. No content."""

    status_version: object = None
    status_generated_at: str = ""
    status_applied_revision: object = None
    agent_build: object = None
    tree_version: object = None
    tree_generated_at: str = ""
    exclusions_revision: object = None
    exclusions_count: object = None


@dataclass(frozen=True)
class Facts:
    """Everything the report may know. The controller fills this in explicitly.

    Adding a field here is a deliberate act with a matching test; anything not
    listed cannot accidentally become report content.
    """

    lifecycle: str = ""
    container_state: str = ""
    marker_present: object = None
    install_origin: str = ORIGIN_UNKNOWN
    autostart_enabled: object = None
    compatibility: str = ""
    compatibility_detail: str = ""
    documents: DocumentFacts = field(default_factory=DocumentFacts)
    health: tuple[HealthRow, ...] = ()
    overall: str = ""
    #: When bridge facts above were last gathered successfully (UTC ISO-8601),
    #: or empty when they never were. In a no-CIFS state this is what tells the
    #: reader the section is cached rather than current.
    gathered_at: str = ""
    #: The last power-helper outcome. Bounded, sanitized, and classified — the
    #: helper's message is its own unlocalized text, not agent or file data.
    last_helper_action: str = ""
    last_helper_ok: object = None
    last_helper_detail: str = ""
    #: Operator paths, supplied explicitly. Rendered as placeholders unless
    #: `include_paths` is set on the report request.
    exclusion_paths: tuple[str, ...] = ()
    #: Safe Workspaces, as counts and one UTC ISO-8601 timestamp (§12.2). There
    #: is deliberately no field for a name, a folder, a relative path, a status
    #: `detail` or any Unison output, with or without an opt-in.
    workspaces_configured: int = 0
    workspaces_enabled: int = 0
    workspaces_conflicted: int = 0
    workspaces_guarded: int = 0
    workspaces_failed: int = 0
    workspaces_last_success: str = ""


@dataclass(frozen=True)
class Report:
    """The rendered-ready result: ordered sections of (label, value) lines."""

    sections: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    include_paths: bool = False


# -------------------------------------------------------------- redaction --

def _redact_credentials(text: str) -> str:
    """Blank the value half of anything shaped like `password=…`."""
    return _CREDENTIAL_RE.sub(
        lambda match: re.sub(r"\s*[:=]\s*\S+$", "=<redacted>", match.group(0)), text)


def _bounded(value: object) -> str:
    """One field, as a single-line string, truncated to the field bound."""
    text = "" if value is None else str(value)
    text = _redact_credentials(text.replace("\r", " ").replace("\n", " ").strip())
    if len(text) > MAX_FIELD_CHARS:
        return text[:MAX_FIELD_CHARS] + " " + TRUNCATION_SUFFIX
    return text


def _yes_no(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


class Placeholders:
    """Stable `<path-N>` names, consistent within one report."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def name(self, path: str) -> str:
        key = path.lower()
        if key not in self._names:
            self._names[key] = f"<path-{len(self._names) + 1}>"
        return self._names[key]


def redact_paths(paths: Iterable[str], *, include: bool) -> tuple[str, ...]:
    """Real names only on explicit opt-in; stable placeholders otherwise."""
    placeholders = Placeholders()
    return tuple(_bounded(path) if include else placeholders.name(path)
                 for path in paths)


# -------------------------------------------------------------- collecting --

def _probe_units(runner: Runner) -> tuple[tuple[str, str], ...]:
    """`systemctl is-active` per unit, reduced to its classified word.

    Raw output is never rendered: only one of `systemd`'s own state words, or
    `unavailable` when the command itself could not run.
    """
    rows = []
    for unit in UNITS:
        argv = ["systemctl", "is-active", unit]
        try:
            result = runner(argv, SYSTEMCTL_TIMEOUT_SECONDS)
        except (FileNotFoundError, TimeoutError, OSError):
            rows.append((unit, "unavailable"))
            continue
        state = (result.stdout or "").strip().splitlines()
        rows.append((unit, state[0][:32] if state else "unavailable"))
    return tuple(rows)


def _probe_sudo(runner: Runner) -> tuple[tuple[str, str], ...]:
    """The two authorization probes, reduced to granted / not granted.

    `sudo -n -l <cmd> <arg>` answers without prompting and mutates nothing. Its
    output is not rendered — it would echo the local account name.
    """
    rows = []
    for spec in SUDO_SPECS:
        argv = ["sudo", "-n", "-l", *spec.split()]
        action = spec.rsplit(" ", 1)[-1]
        try:
            result = runner(argv, SYSTEMCTL_TIMEOUT_SECONDS)
        except (FileNotFoundError, TimeoutError, OSError):
            rows.append((action, "could not be checked"))
            continue
        rows.append((action, "granted" if result.returncode == 0 else "not granted"))
    return tuple(rows)


def collect(facts: Facts, runner: Runner = default_runner, *,
            include_paths: bool = False) -> Report:
    """Build the report from ``facts`` plus the two allowed host probes."""
    gathered = facts.gathered_at or "not gathered"

    app = (
        ("App version", _bounded(__version__)),
        ("Install origin", _bounded(normalize_origin(facts.install_origin))),
        ("Start with the computer", _yes_no(facts.autostart_enabled)),
    )

    lifecycle = (
        ("Lifecycle state", _bounded(facts.lifecycle)),
        ("Container", _bounded(facts.container_state or "unknown")),
        ("Desired-off marker", _yes_no(facts.marker_present)),
        ("Last helper action", _bounded(facts.last_helper_action or "none this session")),
        ("Last helper result",
         "not run" if facts.last_helper_ok is None
         else ("succeeded" if facts.last_helper_ok else "failed")),
        ("Last helper message", _bounded(facts.last_helper_detail)),
    )

    documents = facts.documents
    bridge_rows = (
        ("Facts gathered at", _bounded(gathered)),
        ("Protocol compatibility", _bounded(facts.compatibility or "unknown")),
        ("Compatibility detail", _bounded(facts.compatibility_detail)),
        ("Agent build", _bounded(documents.agent_build)),
        ("status.json version", _bounded(documents.status_version)),
        ("status.json generated", _bounded(documents.status_generated_at)),
        ("Applied revision", _bounded(documents.status_applied_revision)),
        ("tree.json version", _bounded(documents.tree_version)),
        ("tree.json generated", _bounded(documents.tree_generated_at)),
        ("Exclusions revision", _bounded(documents.exclusions_revision)),
        ("Exclusions configured", _bounded(documents.exclusions_count)),
    )

    health_rows = (("Overall", _bounded(facts.overall or "unknown")),
                   ("Rows gathered at", _bounded(gathered))) + tuple(
        (_bounded(row.name), _bounded(row.severity)) for row in facts.health)

    unit_rows = _probe_units(runner)
    sudo_rows = tuple((f"May power {action}", state) for action, state in _probe_sudo(runner))

    paths = redact_paths(facts.exclusion_paths, include=include_paths)
    path_rows = tuple((f"Exclusion {index}", value)
                      for index, value in enumerate(paths, start=1))
    if not path_rows:
        path_rows = (("Exclusions", "none configured"),)

    workspace_rows = (
        ("Configured", _bounded(facts.workspaces_configured)),
        ("Enabled", _bounded(facts.workspaces_enabled)),
        ("In conflict", _bounded(facts.workspaces_conflicted)),
        ("Guarded", _bounded(facts.workspaces_guarded)),
        ("Failed", _bounded(facts.workspaces_failed)),
        ("Last successful sync", _bounded(facts.workspaces_last_success or "never")),
    )

    return Report(
        sections=(
            ("Application", app),
            ("Lifecycle", lifecycle),
            ("Bridge documents", bridge_rows),
            ("Health", health_rows),
            ("Host units", unit_rows),
            ("Helper authorization", sudo_rows),
            ("Selective sync", path_rows),
            ("Safe workspaces", workspace_rows),
        ),
        include_paths=include_paths,
    )


# --------------------------------------------------------------- rendering --

HEADER = "iCloud bridge diagnostic report"

REDACTION_NOTE = (
    "Folder names are replaced with placeholders. This report contains no "
    "passwords, credentials files, command environments, Apple identity data, "
    "or file contents.")

DISCLOSURE_NOTE = (
    "Folder names are included at the operator's explicit request. This report "
    "still contains no passwords, credentials files, command environments, "
    "Apple identity data, or file contents.")


def render(report: Report) -> str:
    """Plain text, capped at the whole-report bound."""
    lines = [HEADER, "=" * len(HEADER), ""]
    lines.append(DISCLOSURE_NOTE if report.include_paths else REDACTION_NOTE)
    lines.append("")
    for title, rows in report.sections:
        lines.append(title)
        lines.append("-" * len(title))
        width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            lines.append(f"  {label.ljust(width)}  {value}")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_REPORT_BYTES:
        return text
    # Cut on a character boundary, then say plainly that it was cut.
    marker = f"\n{TRUNCATION_SUFFIX}\n"
    budget = MAX_REPORT_BYTES - len(marker.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + marker


def report_text(facts: Facts, runner: Runner = default_runner, *,
                include_paths: bool = False) -> str:
    """The one call the GUI needs: collect, then render."""
    return render(collect(facts, runner, include_paths=include_paths))


def default_filename(stamp: str) -> str:
    """`icloud-bridge-diagnostics-<yyyymmdd-hhmmss>.txt`, with a safe stamp."""
    safe = re.sub(r"[^0-9A-Za-z-]", "", stamp)[:32] or "report"
    return f"icloud-bridge-diagnostics-{safe}.txt"


def allowed_argvs() -> tuple[tuple[str, ...], ...]:
    """Exactly the commands :func:`collect` may run. Used by the tests."""
    return tuple(
        [("systemctl", "is-active", unit) for unit in UNITS]
        + [("sudo", "-n", "-l", *spec.split()) for spec in SUDO_SPECS])


__all__ = [
    "Facts", "DocumentFacts", "HealthRow", "Report", "RunResult",
    "collect", "render", "report_text", "redact_paths", "normalize_origin",
    "default_filename", "allowed_argvs", "UNITS", "SUDO_SPECS",
    "MAX_FIELD_CHARS", "MAX_REPORT_BYTES", "TRUNCATION_SUFFIX",
]
