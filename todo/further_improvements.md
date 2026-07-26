# Todo: Execute the CHANGELOG "Further improvements" backlog

> **Status: not started.** This note turns the candidates recorded in
> [`../CHANGELOG.md`](../CHANGELOG.md) under **Further improvements**
> (I-001 – I-007) into an ordered, reviewed implementation plan. The CHANGELOG
> stays the ledger: when an item here ships, move its candidate to **Shipped
> improvements** there in the same commit and mark the item done here.

## Goal

Close out the reviewed follow-up work: record the live acceptance baseline,
de-risk the large Qt controllers, detect host/guest version skew, make the
selective-sync configuration recoverable, make failures reportable, make long
operations legible, and establish the first real release boundary.

Items are ordered for execution (dependencies first), not by the CHANGELOG's
value order; each item names its CHANGELOG ID. Numbering is stable so completed
items can be marked done without renumbering or deleting their implementation
and verification record.

## Decisions proposed by this note

The v2 decision register currently ends at D34. This backlog proposes five new
decisions, including one correction to interrupted first-run behavior. This
todo is not itself authoritative: each decision must be added to
`docs/plan-gui-selective-sync.md` §1 **before** its implementation lands (hard
rule 1 in `AGENTS.md`):

- **D35** (item 3): bridge-protocol version validation and agent-build skew
  detection, fail-closed for writes while compatibility is unknown or the
  protocol is unsupported. Exactly one supported protocol version, per
  `AGENTS.md` hard rule 9 — detect and report skew, never accommodate it.
- **D36** (item 4): host-side snapshot of `exclusions.json` with explicit,
  previewed restore; never automatic.
- **D37** (item 5): privacy-safe diagnostic report; redaction-by-default,
  bounded, no new CIFS I/O.
- **D38** (item 6): progress presentation for long transactions via elapsed
  time plus streamed `==> ` phase lines; no new IPC channel, no cancel button.
- **D39** (item 6): a private local provisioning record keeps a GUI-created,
  not-yet-configured VM in D31's no-CIFS **Provisioning Windows** state across
  an app restart. It is cleared only after **Check setup and connect**
  succeeds, or by a separately confirmed local-record discard when Docker
  proves the original container is absent/different.

Items 1, 2 and 7 change no locked behavior and need no new decision. If
implementing any of them turns out to require a behavior change, stop and
report instead of improvising one.

## Constraints that apply to every item

- `docs/plan-gui-selective-sync.md` is authoritative and wins on conflict.
  Every GUI behavior change updates its exact §6.2 specification in the same
  commit; protocol changes update the §2 exact formats and the §3 agent spec.
- `health.py`, `bridge.py`, `power.py`, `autostart.py`, `firstrun.py` and every
  new model module (`lifecycle.py`, `backup.py`, `diagnostics.py`) import no
  PySide6. `power.py`, `autostart.py`, `firstrun.py`, `lifecycle.py`,
  `backup.py` and `diagnostics.py` also perform no mount I/O. `pytest gui/tests`
  must keep passing with and without PySide6 installed.
- The D29–D31 no-CIFS situations (`setup`, `provisioning`, `powered_off`,
  `starting`, `shutting_down`, and a startup inspection failure routed into
  `setup`) stay free of `health.gather()`, `bridge.read_*()`, `ismount()` on
  the share paths, and `xdg-open` of a mount. Nothing in this backlog may add
  CIFS I/O to those situations.
- Every subprocess is asynchronous from the GUI's perspective, bounded by a
  timeout, invoked with an exact argv (never a shell), and returns bounded
  output. Docker calls stay pinned to `unix:///var/run/docker.sock`.
- Never read or emit `.env`, `/etc/credentials-icloud`, `SHARE_PASS`, or
  process environments into any new file, report, or backup.
- `guest-agent/agent.ps1` and `provision/agent.ps1` stay byte-identical
  (`make lint` and the pre-commit hook both check this).
- The project is pre-release (`AGENTS.md` hard rule 9). No item here may add a
  compatibility shim, a deprecation window, or a migration path for an earlier
  build of this code. Change the format, change every reader and writer, delete
  the old path, and record the operator's re-run step in `SETUP.md`. The
  operator's live state — `exclusions.json`, credentials, the VM disk, the
  synced files — is data safety, not compatibility, and is still protected.
- Every item that adds live behavior adds a distinct Phase E acceptance ID to
  `docs/plan-gui-selective-sync.md` and a matching `not yet run` row to
  `docs/acceptance-results.md` in the same commit. Repository tests never turn
  that row into a pass.
- No pictographic emoji in docs or comments; keep LF line endings. Commit after
  each completed item rather than batching the backlog into one commit; the
  pre-commit hook enforces the mechanical half of all of this.

## 1. Scaffold the live acceptance record (I-001)

> **Status: done.** `docs/acceptance-results.md` exists with the E0-E11d rows,
> the environment baseline, and the fail/append/maintenance/privacy rules;
> `SETUP.md` and the CHANGELOG point at it; the package ships it under
> `/usr/share/doc/icloud-bridge/docs/`. Every row is still `not yet run` — that
> is operator work on the real host, and this workspace may not change it.

I-001 itself — running the E0–E11d matrix — needs the real KVM host and is
operator work. The workspace part is to make the record exist so results land
somewhere durable instead of in chat logs.

Create `docs/acceptance-results.md` with:

- A short intro: what this file is, that results are only ever recorded from
  the real host, and that the checks themselves are specified in
  `docs/plan-gui-selective-sync.md` Phase 0 (E0) and Phase E (E1 onward; link
  to both). Do not copy the check text — reference the IDs so the plan remains
  the single source.
- An **Environment baseline** section with a fill-in table for: Windows
  edition/build, iCloud for Windows version, Docker Engine version, dockur
  container image ID (`docker --host unix:///var/run/docker.sock inspect
  --format '{{.Image}}' icloud-windows`) plus the image's repo digest when
  available (`docker --host unix:///var/run/docker.sock image inspect --format
  '{{json .RepoDigests}}' dockurr/windows`), host kernel (`uname -r`), `cifs`
  module version (`modinfo -F version cifs` or "in-tree <kernel>"), and useful
  timings (cold boot to green, cold 1 GiB hydration, clean power-off duration).
  Baselines are dated and appended when the environment changes; do not
  overwrite the versions that explain an older result.
- A **Results** table with one row per live-acceptance check ID currently
  listed in the plan (E0 through E11d — enumerate them from the plan when
  writing the file, do not hardcode a count anywhere in prose). Columns:
  `Check`, `Date`,
  `Result` (`not yet run` / `pass` / `fail` / `accepted limitation`),
  `Evidence / notes`. Every row starts as `not yet run`.
- A rule stated in the file: a `fail` row must either become a fix (linked
  commit) or an explicitly worded accepted limitation; silent re-runs do not
  overwrite history — append dated rows instead.
- A maintenance rule: when a later implementation adds a Phase E ID, add its
  initial `not yet run` row here in that same commit rather than waiting for the
  live run.
- A privacy rule: evidence records versions, timings, states and redacted
  diagnostics, never host/user names, Apple IDs, credentials, exclusion paths,
  file contents, or other operator data.

Also add one pointer sentence to `SETUP.md`'s acceptance section and to the
CHANGELOG I-001 entry saying results are recorded in
`docs/acceptance-results.md`. A result is filled in only from the real host,
whether the executor is the operator or an agent attached to that host.

Ship the record with the `.deb` at
`/usr/share/doc/icloud-bridge/docs/acceptance-results.md`, preserving the
repository-relative `docs/acceptance-results.md` link used by SETUP/CHANGELOG,
and update plan §14.3's package-document list.

Files: `docs/acceptance-results.md` (new), `SETUP.md`, `CHANGELOG.md`,
`packaging/build-deb.sh`, `docs/implementation-plan.md`.

## 2. Extract the lifecycle reducer and add offscreen Qt wiring tests (I-006)

> **Status: done.** `lifecycle.py` holds the reducer, the controller is the loop
> around it, and `test_lifecycle.py` / `test_qt_wiring.py` cover the table and
> the wiring. Behavior-preserving as required: no plan contract changed.

`__main__.py` (~1000 lines) and `window.py` (~1300 lines) hold the D29–D31
orchestration in Qt callbacks. Extract the decision logic before items 3–6 add
more states. This is a **behavior-preserving refactor**: no plan contract
changes, no visible behavior changes.

### 2.1 Pure reducer

Add Qt-free, mount-I/O-free, subprocess-free
`gui/icloud_bridge_gui/lifecycle.py`:

- The canonical phase set is exactly the constants the controller already
  uses: `starting`, `running`, `start_failed`, `powered_off`,
  `shutting_down`, `setup`, and `provisioning`. `inspect_error` is a startup
  classification that enters `setup`, not a separate lifecycle phase;
  `monitoring` and `setup_required` are presentation names, not additional
  state strings.
- Shape: `reduce(model: Model, event: Event) -> Transition`. `Model` and
  `Transition` are frozen dataclasses; the model contains the canonical phase
  plus the existing power-off continuation (`exit_after_power_off`) so a later
  success can distinguish Quit from keep-running power-off without hidden
  controller state. `Transition` contains the next model and an ordered tuple
  of effects. Events are things that happen (`power_on_succeeded`,
  `power_off_failed`, `quit_confirmed_power_off`,
  `container_reported_exited`, `user_start_bridge`, …); effects are imperative
  tokens the controller interprets
  (`stop_polling`, `start_polling`, `run_power_on`, `run_power_off`,
  `clear_health_rows`, `show_powered_off_banner`, `exit_app`, …). Derive both
  vocabularies from the existing callbacks and represent events/effects as
  closed enums rather than unchecked free-form strings.
- Give each lifecycle operation a monotonically increasing controller token;
  completion callbacks capture it and are discarded before reduction when it
  no longer matches. That is how stale worker signals become harmless. Do not
  make every unknown pair silently disappear: an unexpected current-token
  event returns no mutating effects plus a bounded `report_invalid_transition`
  effect so it remains diagnosable. An unknown phase fails closed and is a test
  failure.
- The controller keeps ownership of *doing* (timers, workers, widgets) and
  becomes a thin loop: translate signal → event, call `reduce`, apply effects
  in order. Keep `power.plan_startup` and `power.available_action` as the
  existing pure Docker/start-action classifiers (or call them from the new
  model); do not create a second subtly different classification table.

Test with an exhaustive table in `gui/tests/test_lifecycle.py`: every expected
(model, event) pair asserted against its exact `Transition`, plus stale-token
discard and invalid-transition reporting. Key invariants to assert explicitly:
no path reaches a polling/health effect from a no-CIFS state without passing
through `power_on_succeeded`; `quit` from powered-off produces `exit_app`
without `run_power_off`; a failed power-off restores the running state's
effects; inspect errors never produce a mutating effect.

### 2.2 Thin Qt integration layer

Add `gui/tests/test_qt_wiring.py`:

- Module-level `pytest.importorskip("PySide6")` and set
  `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` before any Qt
  import, so the no-Qt leg skips it and the Qt leg needs no display.
- Monkeypatch `power`, `bridge` and `health` entry points with recording fakes;
  never touch real docker/sudo/mounts.
- Use one explicit `QApplication` fixture, replace modal dialogs with fakes,
  drive queued signals with bounded event-loop waits, and drain/clear timers
  and thread-pool work at teardown so one test cannot leak callbacks into the
  next.
- Cover the wiring the pure suite cannot: startup schedules no bridge/health
  work before the (faked) power-on resolves; failed power-off resumes polling;
  Quit while powered off never invokes the helper fake; setup-required
  performs no bridge reads; a stale listing response arriving after Reload is
  discarded; window close hides with a tray present and routes to the quit
  confirmation without one.

`make test-all` already runs the suite with and without Qt; no Makefile change
should be needed — verify the Qt leg actually collects the new file.

Update `docs/plan-gui-selective-sync.md` §6.1 (layout row for `lifecycle.py`)
and add one sentence to §6.2 noting the state machine is implemented as a pure
reducer; add the module to the `AGENTS.md` layout table with the Qt-free rule.

Files: `gui/icloud_bridge_gui/lifecycle.py` (new),
`gui/icloud_bridge_gui/__main__.py`, `gui/icloud_bridge_gui/window.py`,
`gui/tests/test_lifecycle.py` (new), `gui/tests/test_qt_wiring.py` (new),
`docs/plan-gui-selective-sync.md`, `AGENTS.md`.

## 3. Detect protocol and agent-version skew (D35, I-003)

> **Status: done.** D35 is in the register; all three document kinds carry
> `"version": 1`; `status.json` carries `agentBuild`; `bridge.py` validates and
> classifies; the controller gates Apply and list dispatch on it. The agent side
> is **unexecuted here** — `make lint-ps` parses it, but nothing in this
> workspace can run Windows PowerShell against a guest. E12 covers that.

The §2 formats already carry `"version": 1` in `status.json` and `tree.json`,
but the GUI accepts both as arbitrary JSON, and list responses have no version
at all. A package upgrade ships a newer `agent.ps1` into the host bundle but
cannot replace `C:\ProgramData\icloud-bridge\agent.ps1`, so GUI/agent skew is
silent today.

This item is deliberately small, because `AGENTS.md` hard rule 9 removes the
part that would have been large: the project is pre-release, so there is exactly
**one** supported protocol and the GUI and agent are expected to match. Skew is
something to *detect and report*, not something to be compatible with. No
`legacy`/`older`/`newer` matrix, no reserved `capabilities` field, no
conditional exception for documents an older agent used to emit.

Decisions:

- The per-document `version` field **is** the protocol version. Do not add a
  second one. Add `"version": 1` to list response files as well (§2.4), so all
  three document kinds are checked the same way.
- `status.json` additionally gains `"agentBuild": <int>` — a non-negative,
  monotonically increasing integer constant near the top of `agent.ps1`, bumped
  in any commit that changes agent behavior. `bridge.py` carries the bundled
  build constant, and a static test checks it against the PowerShell literal so
  the two cannot drift unnoticed. A date alone is not a build ID: more than one
  agent change can land on the same day.
- Protocol validation, implemented in `bridge.py` next to each reader and unit
  tested: `version` must be the integer `1` in `status.json`, `tree.json` and
  every list response. Missing, boolean, non-integer, or any other value is
  incompatible — there is no tolerated older form.
- Represent the result explicitly (a frozen compatibility dataclass plus a
  distinct `ProtocolError`) and propagate it through `health.Snapshot` to the
  controller. Do not collapse an unsupported protocol into the same generic
  "file unavailable" string, or the controller cannot enforce the central gate.
- Build comparison is equality. `agentBuild` equal to the bundled constant is
  `current`; anything else — lower, higher, missing, or malformed — is `skewed`.
- Behavior on `skewed`: everything still works (the protocol matched), plus a
  persistent yellow banner: "The guest agent does not match this app. In the VM,
  re-run C:\\OEM\\04-bridge-agent.ps1 (elevated) to update it." Copyable text.
  A newer agent gets the same treatment as an older one; under hard rule 9 the
  only supported configuration is the matching pair.
- Behavior on `incompatible` (fail-closed for writes): one central controller
  compatibility gate disables Apply and Restore, prevents all list-request
  dispatch, and leaves the current `exclusions.json` untouched. An unsupported
  status/tree document is not fed into normal health/tree rendering merely
  because its JSON parsed; show only the supported exclusion config plus a red
  protocol diagnostic and the appropriate update recovery.
- Until a current status document has established `current` or `skewed`,
  compatibility is `unknown` and the same write/list gate stays closed. A
  transient missing status must not make the GUI guess which agent is running.
  An explicitly unsupported status **or** tree version makes the overall gate
  incompatible; a merely missing or stale tree keeps browsing unavailable but
  does not override a compatible status classification.
- `read_exclusions` keeps its existing version check; this item does not change
  the exclusions format.

Agent side: emit the two new fields in the status writer and `"version": 1` in
each list response. Edit `guest-agent/agent.ps1`, then copy byte-identically to
`provision/agent.ps1`.

Plan updates in the same commit: D35 row in §1; §2.2, §2.3, §2.4 exact-format
blocks and notes; §3 spec (status writer, list responder); §6.2 (banner and
fail-closed rules).

Tests: `gui/tests/test_bridge.py` covers ok/skewed/incompatible/malformed for
status, tree and list responses. A controller-level test proves Apply, Restore,
and list dispatch are refused under protocol incompatibility while a build skew
on a matching protocol remains usable.

Because the agent and the GUI change together and nothing has shipped, this
lands as one commit pair with no transitional state: after it, an agent that
predates it is simply `incompatible` and the operator re-runs script 04. Add
Phase E **E12**: confirm the banner appears against a deliberately stale
`agentBuild`, re-run script 04 and observe `current`, then exercise an
unsupported-version fixture through both `ICLOUD_BRIDGE_DIR` and
`ICLOUD_MOUNT_DIR` overrides and confirm every write surface is disabled without
touching the real config or mount. Add E12 to the acceptance-results file as
`not yet run`. State plainly in the commit that the agent side is unexecuted
here (`make lint-ps` parses but cannot run the Windows-specific code).

Files: `guest-agent/agent.ps1`, `provision/agent.ps1`,
`gui/icloud_bridge_gui/bridge.py`, `gui/icloud_bridge_gui/__main__.py`,
`gui/icloud_bridge_gui/window.py`, `gui/tests/test_bridge.py`,
`docs/plan-gui-selective-sync.md`.

## 4. Back up and explicitly restore selective-sync choices (D36, I-004)

> **Status: done.** `backup.py` holds the snapshot rules, `write_exclusions`
> gained `minimum_revision`, and the Selective Sync tab gained the previewed
> **Restore from backup…** action plus the "not backed up" warning. D36 and E13
> are recorded; the live half is E13 on the real host.

`exclusions.json` is the one piece of unique configuration living inside the
disposable VM, and the fail-closed provisioning rule (correctly) refuses to
manufacture an empty one after loss. Give the operator a host-side copy and an
explicit way back.

Decisions:

- New Qt-free, mount-I/O-free `gui/icloud_bridge_gui/backup.py`. Backup path:
  `$XDG_STATE_HOME/icloud-bridge-gui/exclusions-backup.json`, defaulting
  `XDG_STATE_HOME` to `~/.local/state`; the base directory is an injectable
  parameter for tests. Create or tighten the directory to mode 0700 and reject
  a non-directory/symlink target. Write atomically with a unique temp file in
  the same directory, mode 0600 before `os.replace`; reject a symlink or
  non-regular existing destination rather than following it. Tighten an
  existing regular backup to 0600 even when its content is unchanged and the
  write would otherwise be skipped.
- Backup content:

  ```json
  {"version": 1, "savedAt": "<UTC ISO-8601>", "source": "read",
   "revision": 7, "exclusions": ["Big Folder", "Docs/huge-video.mp4"]}
  ```

  `source` is `"read"` or `"apply"`. `exclusions` is the canonical list
  (`bridge.canonicalize` output). An empty list is a **valid** backup meaning
  "include everything".
- When to write: after every successful validated `read_exclusions` during
  monitoring and after every successful Apply. Skip the write when the
  canonical list and revision both match the existing backup, so steady state
  does not churn the file. A read may replace the backup only when its revision
  is greater than the saved revision, or when it is identical. A lower revision
  must not replace it: that is the normal signature of a rebuilt VM with a new
  revision-0 empty config, and overwriting there would destroy the copy needed
  for recovery. The same revision with different content is a conflict and is
  also retained/reported. A successful explicit Apply has a newly incremented
  revision and may replace the backup, including with an empty list. Backup
  writes happen on the same worker thread as the read/Apply that produced the
  data, never on the GUI thread — the backup file itself is local disk, not
  CIFS.
- Treat the bridge operation and local snapshot as two results. If a validated
  read succeeds but its backup write fails, keep the loaded selection and show
  a persistent yellow "configuration is not backed up" warning. If Apply writes
  `exclusions.json` successfully but the subsequent backup fails, Apply still
  succeeded — update the loaded revision/selection and warn; never route that
  case through today's "Nothing was changed" failure dialog.
- Restore is explicit and previewed, never automatic. A **Restore from
  backup…** button on the Selective Sync tab, enabled only in normal
  monitoring with a loaded snapshot, no staged unapplied selection, and a
  compatible protocol. If the selection is dirty, require Apply or Reload
  first rather than silently discarding it.
  Flow: read backup → validate (JSON shape, `version == 1`, canonicalize,
  reject on any invalid path) → preview dialog listing additions and removals
  relative to the loaded on-disk selection → on confirm, write through the
  existing `write_exclusions` path. Extend that function with an explicit
  `minimum_revision` candidate rather than overloading `last_written`; the new
  revision must be strictly greater than `status.appliedRevision`, the loaded
  exclusions revision, the GUI's last-written revision, and the backup's own
  revision.
- A missing or corrupt backup is an error dialog, never interpreted as an
  empty list, and never blocks normal operation. A well-formed old backup is
  not rejected merely because its revision is stale; stale content is exactly
  what a restore is for, and the newly written revision makes it monotonic.

Plan updates: D36 row; §6.2 paragraph. Docs: `docs/selective-sync.md` and
`SETUP.md` recovery sections now describe disposable-VM recovery as: rebuild
the VM, re-provision, then use Restore from backup — instead of assuming the
operator kept their own copy.

Tests (`gui/tests/test_backup.py`, no Qt): atomic write leaves no temp file,
0600/0700 modes, symlink/non-regular rejection, skip-on-unchanged, malformed
snapshots rejected, a lower-revision automatic read cannot replace the saved
copy, a stale valid snapshot restores at a revision strictly above all observed
values, case-insensitive duplicates collapse, empty-list backup round-trips,
and bridge-read/Apply success remains correctly represented when only the local
backup write fails.
A controller/offscreen-Qt test proves dirty staged UI state blocks Restore.
Add Phase E **E13** using disposable paths only. Prefer a disposable VM; on the
production VM, simulate a reset non-destructively by closing the app and
installing a validated backup whose revision is deliberately higher than the
current config and whose paths name only a disposable test folder. Relaunch,
prove the lower current read does not overwrite that backup, preview/restore
it, and confirm the test exclusion reaches `applied`. Never rewrite the live
guest config by hand, destroy, or re-provision the operator's production VM
merely to run this case. Add E13 to the acceptance-results file as `not yet
run`.

Files: `gui/icloud_bridge_gui/backup.py` (new),
`gui/icloud_bridge_gui/bridge.py`,
`gui/icloud_bridge_gui/window.py`, `gui/icloud_bridge_gui/__main__.py`,
`gui/tests/test_backup.py` (new), `gui/tests/test_qt_wiring.py`,
`docs/plan-gui-selective-sync.md`,
`docs/selective-sync.md`, `SETUP.md`, `AGENTS.md`.

## 5. Export a privacy-safe diagnostic report (D37, I-002)

> **Status: done.** `diagnostics.py` takes an allowlisted `Facts` dataclass and
> runs only the six `systemctl is-active` probes and the two `sudo -n -l`
> checks; the Status tab gained **Copy diagnostics** and **Save diagnostic
> report…**. D37 and E14 are recorded.

Support currently means hand-collecting GUI rows, Docker output, systemd state
and journal fragments. Add **Copy diagnostics** and **Save diagnostic
report…** to the Status tab.

Decisions:

- New Qt-free, mount-I/O-free `gui/icloud_bridge_gui/diagnostics.py` with two
  functions: `collect(facts, runner) -> report` and `render(report) -> str`
  (plain text). `facts` is a typed, allowlisted dataclass, not a raw controller
  `dict`, `status.json`, `tree.json`, exception object, or process result. The
  controller copies in only data it already holds or can derive from safe local
  state: app `__version__` and coarse install origin (`package` / `per-user` /
  `source` / `override`, normalizing firstrun's `user` value to `per-user` and
  never including a checkout/home path), lifecycle state and container
  classification (cached inspect), marker state, health names/severities,
  bridge document versions/timestamps/revisions, agent build and skew
  classification (item 3), and autostart state. Add explicit bounded controller
  fields for the last helper result and last successful gather time; those are
  not retained today and must not be reconstructed from a journal. Unknown
  fields cannot accidentally become report content.
- `collect` may run only these subprocesses through the injected bounded
  runner, none of which touch a mount: `systemctl is-active` on the six units
  (the health timer is already one of the six), and the two argument-exact
  `sudo -n -l /usr/local/bin/icloud-bridge-power on|off` probes (reuse the
  existing firstrun/power helpers for both). No journal reads, no docker
  calls of its own, no CIFS.
- Redaction is default-on: real exclusion paths, listing names and any other
  explicitly supplied operator paths are represented only by stable
  placeholders (`<path-1>`, `<path-2>`, …) consistent within one report. When
  the operator chooses **Include folder names**, a separate validated path list
  may render those real values. Do not promise that arbitrary prose can be
  perfectly recognized as a filename: raw agent `lastError`, raw health
  details, and unfiltered subprocess environments are therefore not admitted
  to `facts`. Raw `systemctl`/`sudo -l` output is not rendered either; only the
  classified unit/authorization result is. The report never contains
  `.env`, `/etc/credentials-icloud`, `SHARE_PASS`, environment variables,
  Apple identity data, or file contents — there is no opt-in for those.
- Bounds: each captured field truncated to 2000 characters with a literal
  `[truncated]` suffix; the rendered report is capped at 64 KiB.
- No-CIFS states produce a report from cached data only, each section labelled
  with the timestamp of its last successful bridge gather (or "not gathered").
  The safe `systemctl`/`sudo -n -l` host probes above may still run in every
  state; "cached" applies to bridge/CIFS facts, not all subprocesses. The export
  buttons work in every state because failure states are exactly when reports
  matter.
- UI: **Copy diagnostics** puts the rendered text on the clipboard;
  **Save diagnostic report…** opens a save dialog defaulting to
  `icloud-bridge-diagnostics-<yyyymmdd-hhmmss>.txt` in the user's home and
  writes it mode 0600 (including when replacing an existing chosen file).
  Refuse a symlink/non-regular destination rather than following it.

Plan updates: D37 row; §6.2. `SETUP.md` troubleshooting gains one line telling
the operator to attach a saved report.

Tests (`gui/tests/test_diagnostics.py`, no Qt): seed sentinel secrets and real
paths into extra/unapproved inputs and assert the allowlist cannot render them;
known path omission/opt-in and placeholder stability; key/value credential
pattern sanitization on the bounded helper diagnostic; per-field truncation and
the whole-report cap; exact allowed runner argv in every lifecycle state; no
Docker/journal/CIFS calls; mode-0600 save; and every coarse install origin
renders. An offscreen wiring test proves collection stays on a worker and only
the explicit Copy action reaches the clipboard. Add
Phase E **E14**: export in normal, setup, powered-off, and helper-failure
states; verify the report is useful and contains no secret, environment, file
content, or folder name without opt-in. Add E14 to the acceptance-results file
as `not yet run`.

Files: `gui/icloud_bridge_gui/diagnostics.py` (new),
`gui/icloud_bridge_gui/window.py`, `gui/icloud_bridge_gui/__main__.py`,
`gui/tests/test_diagnostics.py` (new), `gui/tests/test_qt_wiring.py`,
`docs/plan-gui-selective-sync.md`,
`SETUP.md`, `AGENTS.md`.

## 6. Show progress and preserve provisioning state (D38/D39, I-005)

VM creation plus Windows provisioning can span 20–40+ minutes, and power-on
retries real CIFS activation for up to five minutes; today the operator sees a
static busy message.

Decisions — deliberately minimal, preserving every D29/D30 guarantee:

- **No new IPC channel.** The helper already prints one `==> ` line per step;
  that stdout, streamed live, is the progress feed. No progress files under
  `/run` and no sockets. Treat only bounded, sanitized stdout lines beginning
  `==> ` as helper phases; human wording after that prefix may evolve and must
  not be parsed into control decisions. The current helper already announces
  the major marker/health/mount/container phases and one overall readiness
  wait; do not add one line per ten-second readiness attempt and flood the UI.
  Change `host/icloud-bridge-power` only if the audit finds a major phase with
  no useful line, and keep v2 §5.1 in step with any addition.
- **No cancel button** on any transaction, no fake percentages, and all
  existing timeouts unchanged. On timeout or failure, the error dialog now
  includes the last phase seen. If the outer caller timeout ever fires, do not
  assume killing the unprivileged `sudo` process stopped a root helper:
  keep all bridge I/O paused in a new D38 `transition_unknown` phase, invalidate
  cached state, and report that the helper may still be reconciling. Only an
  explicit Retry may invoke the same desired action again; `flock` prevents
  overlap and may report its bounded lock wait as busy while a surviving helper
  still owns it. Re-inspect/reconcile successfully before re-enabling ordinary
  controls. A power-off timeout must never fall through today's generic failure
  path that resumes polling against possibly unmounted shares.
- In `transition_unknown`, retain the desired action (`on` or `off`) and the
  existing Quit/keep-running continuation. The only mutating control is
  **Retry**, which repeats that desired transaction; **Open VM screen** and
  diagnostic export remain read-only. A successful retry follows the ordinary
  success continuation, while another busy/unknown result stays quiesced.
- `power.py` gains a streaming runner based on `subprocess.Popen`. Drain stdout
  and stderr concurrently so either pipe cannot deadlock, enforce the monotonic
  deadline even when the child prints nothing, strip ANSI/control characters,
  cap each delivered line, and retain a bounded combined tail (last 50 lines,
  total cap 64 KiB) for the result. `power_on`/`power_off` accept an optional
  `on_line` callback; their result and error precedence without a callback stay
  compatible with today. Callback exceptions cannot abort the transaction.
  `power.py` remains Qt-free.
- The current `_TaskSignals` has only `done` and `failed`; add an explicit
  `progress = Signal(str)` and an optional GUI-thread `on_progress` callback to
  `run_async`. The worker callback emits that signal rather than touching a
  widget.
- The GUI busy surfaces (starting banner, shutting-down progress, Create VM,
  provisioning wait) show elapsed time ticking from a Qt timer
  ("Starting bridge… 2 m 10 s") plus the most recent phase line. The
  provisioning-Windows wait state, which has no long-lived subprocess after
  Compose returns, shows elapsed time since creation only.
- Compose `up -d` for Create VM gets the same streaming treatment, showing its
  last sanitized output line elided to one row. Handle both newline and carriage
  return progress on stderr; a detached `up -d` covers image pull/container
  creation, not the subsequent 20–40 minute Windows installation.
- Fix the interrupted-first-run hole while adding that elapsed clock. Before
  invoking Compose, atomically write a private mode-0600 record at
  `$XDG_STATE_HOME/icloud-bridge-gui/provisioning.json` containing only
  `version`, `startedAt`, and phase (never the env path or its contents). On
  success, add the inspected container ID. If the app exits after the record is
  written, a later launch checks it and Docker before any CIFS access: a
  matching container — or the fixed-name container when the pre-Compose record
  has no ID yet — re-enters **Provisioning Windows**; an absent container
  returns to Setup with retry guidance; a different container ID shows a
  stale-record warning and performs no CIFS I/O. Clear the record only
  after **Check setup and connect** completes `power_on` successfully. Also
  offer **Discard failed setup record** only for a confirmed absent/different
  container, with confirmation; it removes this local record and never deletes
  a container, VM disk, env file, or bundle. A running container with no record
  retains existing startup behavior, so externally created and
  already-configured installs are not reclassified. Since the env-file path is
  deliberately not persisted, a restarted package install may ask the operator
  to select that file again before showing the final host configuration command.
  Apply item 4's same 0700 app-directory, atomic-replace and
  symlink/non-regular rejection rules; a malformed/unsupported record enters
  Setup with a diagnostic and is never silently deleted or treated as proof a
  VM is configured.

Plan updates: D38 and D39 rows; §5.1 (bounded `==> ` lines are presentation
only, never control input); §6.2 including `transition_unknown` and
interrupted-provisioning startup.

Tests: extend `gui/tests/test_power.py` with deterministic child/fake-pipe tests
proving stdout/stderr are drained without deadlock, silent-child timeout, line
delivery order and sanitization, bounded tails, callback-exception isolation,
and unchanged no-callback result semantics. Lifecycle/controller tests cover
the new `transition_unknown` timeout path without resuming I/O.
Controller/first-run tests cover record-before-create, restart with
absent/running/different container, no early CIFS, record persistence on failed
connect, confirmed discard scope, and clearing after successful connect. Add
Phase E **E15**: observe real helper and Compose progress, restart the app during
Windows provisioning and confirm it resumes that no-CIFS state with the original
elapsed time, then finish setup and confirm the record clears. Add E15 to the
acceptance-results file as `not yet run`.

Files: `gui/icloud_bridge_gui/power.py`, `gui/icloud_bridge_gui/__main__.py`,
`gui/icloud_bridge_gui/window.py`, `gui/icloud_bridge_gui/firstrun.py`,
`gui/icloud_bridge_gui/lifecycle.py`,
`host/icloud-bridge-power` only if its phase audit requires a change,
`gui/tests/test_power.py`, `gui/tests/test_firstrun.py`,
`gui/tests/test_lifecycle.py`, `gui/tests/test_qt_wiring.py`,
`docs/plan-gui-selective-sync.md`, `AGENTS.md`.

## 7. Establish the first release boundary (I-007)

Everything so far reports `2.0.0`. Decisions:

- The release that bundles this backlog is **2.1.0**. When items 2–6 have
  landed, the acceptance record contains E12–E15, and every release-applicable
  Phase E row is `pass` or an explicitly approved `accepted limitation`, bump
  `gui/icloud_bridge_gui/__init__.py::__version__` to `"2.1.0"` — it is the
  single source; `Makefile` and `packaging/build-deb.sh` already derive from
  it, so touch nothing else for the number.
- Same commit: CHANGELOG entry mapping 2.1.0 to the shipped items and to the
  acceptance evidence it depends on (the item-1 results file), and move the
  shipped candidates out of **Further improvements**.
- Verify agreement: `make version`, `icloud-bridge-gui --version`, the built
  package filename and its control metadata (`make deb`, then `dpkg-deb -I`).
- **Tagging is not the agent's call.** Per repo rules, never tag without an
  explicit request; the CHANGELOG's own gate (tag only after the live
  acceptance appropriate to the release) means the operator tags `v2.1.0`
  after the relevant item-1 rows are recorded. The agent's deliverable ends at
  the version bump, changelog entry, and verification above.
- This is the point at which `AGENTS.md` hard rule 9 stops being free. Up to
  the first tag there is nothing to be compatible with; the moment a release
  exists, breaking it becomes a decision with a cost. Nothing before this item
  needs to think about that, and this item is where it gets thought about.

Files: `gui/icloud_bridge_gui/__init__.py`, `CHANGELOG.md`, and whatever
`gui/tests/test_cli.py` asserts about the version string.

## Explicitly not doing

- Filling in any `docs/acceptance-results.md` row from this workspace; there
  is no KVM guest, systemd instance, CIFS mount, tray, or Apple device here.
- Automatic restore of a backup, or treating a missing/corrupt backup as an
  empty exclusion list (item 4 is explicit-and-previewed only).
- Replacing a useful higher-revision backup with a rebuilt VM's automatic
  revision-0 read, or rejecting an otherwise valid backup merely because it is
  old.
- A cancel control that can interrupt the D29/D30 transaction, percentage
  progress, or a helper-side progress file/socket (item 6 decides against).
- Any opt-in that could put `SHARE_PASS`, credentials files, environments, or
  file contents into a diagnostic report.
- Re-opening anything in the CHANGELOG's closed register (R-001 – R-024) or
  deferred list (DFR-001 – DFR-005) — including pause-only-sync and pattern
  exclusions — without first recording new evidence there.
- Guest-admin credentials in the GUI or silent updating of the scheduled agent
  code; the skew recovery action stays a copyable re-run of script 04.

## Verification

For every item:

```bash
make check
make test-all
git diff --check
```

Item 3 additionally: `make lint-ps` and confirm `cmp guest-agent/agent.ps1
provision/agent.ps1` (already inside `make lint`) still passes. Every item in
this backlog changes documentation, GUI code, guest material, or metadata
shipped in the package, so each item runs `make deb` and inspects the staged
tree / package metadata.

The checkout can prove: reducer transition tables, offscreen Qt wiring,
version-skew classification, backup atomicity/permissions/monotonicity,
report redaction and bounds, streaming-runner behavior, and version agreement.
It cannot prove: desktop notification/tray behavior, real helper streaming
under sudo, live skew against a running guest, CIFS/systemd transitions, or
Windows provisioning — those fold into the item-1 matrix on the real host,
and no commit message may claim them from repository tests alone.
