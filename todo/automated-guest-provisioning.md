# Todo: App-driven Windows guest provisioning

> **Status: planned, not started.** This note is the investigation record and the
> execution plan. Per `CONTRIBUTING.md` ("Plans own decisions; files own code"),
> the decision rows drafted below MUST be moved into the v2 plan register
> (`docs/plan-gui-selective-sync.md` section 1) in the first implementation
> commit, claiming the next free D numbers at the moment of editing. Do not
> implement against this note alone.

## Goal

Today a fresh VM auto-runs only `01-debloat.ps1` and then leaves a
`NEXT-STEPS.txt` telling a human to run 02, the Apple sign-in, 03 and 04 by
hand. The goal is that the app handles everything except the Apple ID sign-in:
after **Create Windows VM**, the GUI installs iCloud, waits for the operator to
sign in, creates the SMB share, installs the bridge agent, and hands over to
**Check setup and connect** — with the manual script sequence remaining as a
documented fallback. The same machinery must also re-provision an existing VM
(the post-feature-update recovery step, and the current stale-`C:\OEM` VM).

Apple ID + 2FA and the iCloud Drive toggle stay manual. That is locked
(`CONTRIBUTING.md` Scope; `docs/automation-notes.md` "Not worth automating") and
this plan does not touch it.

## What the investigation established

Facts verified in this workspace on 2026-07-27:

- **A guest control channel exists but is the wrong one to productize.**
  `tools/guest-ctl.sh` + `tools/qemu-monitor.py` drive the QEMU human monitor
  (`sendkey`/`screendump`). Injection is blind, needs screenshot verification a
  program cannot do without OCR, is ~0.035 s/char, and typing the share
  password would defeat the no-secret-on-screen rule. `tools/` is also not
  shipped in the package (`packaging/build-deb.sh` stages `provision/`,
  `guest-agent`, compose, env example, acceptance tests only).
- **A file channel into the guest already exists and needs no compose change.**
  dockur's container `smbd` serves `/tmp/smb` to the guest as `\\host.lan\Data`
  (`docs/automation-notes.md` section 3). `docker cp` / `docker exec -i` reach
  it from the host without root.
- **A precedent for an unattended in-guest service exists.** The bridge agent
  is a Task Scheduler logon task running as `icloud` in an infinite loop with
  restart-on-failure (D17, `provision/04-bridge-agent.ps1` step 8). The same
  pattern, but `RunLevel Highest`, gives elevated execution with no UAC prompt
  and no keystroke injection.
- **`provision/` → `C:\OEM` is copied at install time only**, so OEM copies go
  stale the moment the repo moves (observed live: the current VM's `C:\OEM` is
  four commits behind, including the D35 skew detection). Any automation that
  runs OEM copies would install stale code; the channel must stage current
  scripts per run.
- **Scripts 03 and 04 are already idempotent** and 04 already stops the agent
  task, preserves `exclusions.json`, and fails closed on a missing sync root or
  missing `syncshare` account. The orchestrator can simply call them.
- **The GUI already has the right seams.** `lifecycle.py` has a no-CIFS
  `Phase.PROVISIONING` (D31/D39); `firstrun.py` owns readiness and the
  provisioning record; `power.py` provides injected subprocess runners;
  `window.py` has quiesce/pause machinery (`set_io_paused`, `quiesce`) and the
  Setup tab pattern for check rows and copyable commands.
- **The share password boundary is currently "never handled"** — `firstrun.py`
  parses `.env` as text, checks `SHARE_PASS` presence/placeholder only, and its
  docstring states the value never enters the GUI. Automating 03 requires a
  deliberate, narrow amendment of that boundary (drafted as a decision below).

## Design summary

One sentence: the host stages current provisioning scripts plus a one-shot
secret file into the container's `/tmp/smb/.provision/` directory and writes a
trigger file; an elevated watcher task inside the guest (registered at OEM
install time, or once by hand on an existing VM) consumes the trigger, copies
the staged scripts over `C:\OEM`, and runs an orchestrator that installs
iCloud, waits for the sign-in to produce the sync root, runs 03 and 04, and
reports progress through a status file the GUI polls.

Why this shape and not keystroke injection: every step is verified by effect
(files and JSON, not screenshots), no secret is ever typed or displayed, the
watcher is registered by code that is already running elevated (OEM
`install.bat`), and the only remaining manual guest interaction is the Apple
sign-in itself.

Trust argument (goes into the decision rationale): the container already has
total control of the guest through the QEMU monitor socket and delivered
`C:\OEM` at install time; the host root/docker-group user is the security
boundary (v1 D9). A watcher that executes host-staged scripts adds no
capability an attacker on that path does not already have. The exclusion
boundary is untouched: nothing here grants `syncshare` anything (D15/D27/D28
invariants preserved by reusing 03/04 unchanged in that respect).

## Decision rows to move into the v2 plan register

Claim the next free numbers when editing (D40-D42 if still free; re-check at
commit time per `CONTRIBUTING.md`). Draft text:

- **D40 — Guest provisioning channel.** An elevated scheduled task
  `icloud-bridge-provision` (principal `icloud`, `LogonType Interactive`,
  `RunLevel Highest`, at-logon, infinite loop with restart, `IgnoreNew`) runs
  `C:\OEM\watcher.ps1`, which polls `\\host.lan\Data\.provision\trigger.json`
  every 30 s. On a trigger it deletes the trigger first (consume-once), copies
  the staged `guest-setup.ps1` to `C:\OEM`, and executes it; the orchestrator
  writes atomic progress JSON to `\\host.lan\Data\.provision\status.json`.
  `install.bat` registers the task at OEM time; on a VM installed before this
  feature, the operator pastes one elevated bootstrap command (below) once.
  QEMU-monitor keystroke injection remains a `tools/` debugging aid and is
  never installed or invoked by the app. Rationale: verified-by-effect,
  secretless typing surface, reuses the D17 task pattern and the existing Data
  share; the container already controls the guest (monitor socket, OEM
  delivery), so host root remains the boundary per D9.
- **D41 — SHARE_PASS delivery (amends the D31 "never handled" boundary).** A
  new Qt-free, mount-I/O-free module `guestprov.py` is the only code permitted
  to read the `SHARE_PASS` value. It streams the value over `docker exec -i`
  stdin into `/tmp/smb/.provision/secret` (never argv, never environment,
  never a host temp file, never logged, never in the status file, never on the
  clipboard, never persisted by the GUI). The guest orchestrator reads the
  file into memory, deletes it immediately, and passes it to
  `03-create-share.ps1` via a new `-PasswordFile` parameter; the host deletes
  the staged copy best-effort on completion, failure, and abort. The manual
  edit-the-placeholder path in 03 remains for the fallback sequence.
  Rationale: the password already crosses this exact transport every time the
  host authenticates SMB; the exposure window (a file readable inside the
  single-purpose guest for seconds) is strictly smaller than the operator
  pasting it into an editor inside the same guest.
- **D42 — Provisioning script currency.** Every trigger re-stages the
  installed bundle's current `03-create-share.ps1`, `04-bridge-agent.ps1`,
  `agent.ps1`, `guest-setup.ps1`, and `watcher.ps1` into
  `/tmp/smb/.provision/`, and the orchestrator copies them over `C:\OEM`
  before running any of them. `C:\OEM` therefore tracks the bundle at every
  provisioning run instead of the install-time snapshot, which is the
  permanent fix for the observed stale-OEM skew (the D35 banner's re-run
  instruction now runs current code by construction). The watcher itself is
  deliberately minimal (poll, consume, copy, exec, version-check) so its own
  staleness cannot matter.

Also amend, in the same commit: the D31 register row (its "password never
passes through the GUI" sentence gains "except as provided by D41"), the
`firstrun.py` module docstring, and v2 plan section 4 gains a subsection
specifying the protocol below (plans own decisions; this todo is not the
spec of record once that lands).

## Protocol (exact formats)

Directory: container `/tmp/smb/.provision/`, guest `\\host.lan\Data\.provision\`.
The host creates it with `docker exec mkdir -p` and empties it before staging.

`trigger.json`, written by the host, last (after scripts and secret are in
place):

```json
{"version": 1, "runId": "<host-generated opaque string>", "action": "provision"}
```

`status.json`, written by the guest orchestrator with the same atomic
temp-then-rename pattern as `Write-JsonAtomic` in 04:

```json
{"version": 1, "runId": "<echoed>", "phase": "<see list>",
 "detail": "<one bounded human line>", "updatedAt": "<ISO-8601 UTC>",
 "error": null}
```

Phases, in order: `staging` (scripts copied to `C:\OEM`),
`installing-icloud`, `launching-icloud`, `waiting-for-signin` (polling for the
sync root every 15 s, unbounded — this is the manual step), `creating-share`,
`installing-bridge`, `done`. On failure: `phase` stays at the failing phase
and `error` becomes a bounded message. Unknown `version` in the trigger makes
the watcher write an error status and stop; the GUI treats any unknown phase
or version as an error, never as progress.

Rules a weaker model must not improvise around:

- The host matches `runId` before trusting a status file; a stale or
  mismatched `runId` is "no acknowledgement yet", not progress.
- The secret file never has a mention in `status.json`, `detail`, or any log.
- `waiting-for-signin` has no timeout. Every other phase gets a per-phase
  deadline on the host side (10 min for `installing-icloud`, 5 min for each
  other phase); a deadline or a status mtime frozen for over 120 s during an
  active phase surfaces a "stalled" warning with the manual fallback
  commands, while polling continues — the guest may merely be slow.
- Re-triggering is always safe: 03/04 are idempotent, `installing-icloud`
  skips when `Get-AppxPackage AppleInc.iCloud` already answers, and the
  watcher consumes triggers one at a time.

## Guest-side work

New `provision/watcher.ps1` (unnumbered: machinery, like `agent.ps1`, not an
operator sequence step):

- Header per repo style (what/where/how/idempotent). LF endings.
- `-Install` switch: copies itself to `C:\OEM\watcher.ps1`, registers the
  `icloud-bridge-provision` task exactly as D40 specifies (mirror the
  Register-ScheduledTask block in 04 step 8, changing principal RunLevel to
  Highest and the target script), starts it, exits. Idempotent via `-Force`.
- Default mode: infinite loop, 30 s sleep; on trigger with `version == 1`:
  delete trigger, copy `\\host.lan\Data\.provision\guest-setup.ps1` to
  `C:\OEM\guest-setup.ps1`, run it with
  `powershell -NoProfile -ExecutionPolicy Bypass -File`, loop again. Any
  other version: write an error `status.json` and loop.

New `provision/guest-setup.ps1` (the orchestrator; elevated; idempotent):

1. Write `status.json` (`staging`, echoing the trigger's `runId` — the watcher
   passes the runId as a parameter). Reuse the atomic-write pattern from 04.
2. Copy staged `03-create-share.ps1`, `04-bridge-agent.ps1`, `agent.ps1`,
   `watcher.ps1` over `C:\OEM\`.
3. `installing-icloud`: skip if `Get-AppxPackage AppleInc.iCloud` returns a
   package; else `winget install --id 9PKTQ5699M62 --source msstore
   --accept-package-agreements --accept-source-agreements`, retried up to 5
   times 120 s apart (Store readiness is flaky at first boot — plan section
   5). On exhaustion, error status naming the manual Store fallback from 02's
   header.
4. `launching-icloud`: if the sync root `C:\Users\icloud\iCloudDrive` is
   missing, launch the client non-elevated from this elevated context with
   `explorer.exe shell:AppsFolder\AppleInc.iCloud_nzyj5cx40ttqa!iCloud`
   (documented working method, automation-notes section 3).
5. `waiting-for-signin`: poll `Test-Path` for the sync root every 15 s,
   forever. (Sign-in plus the iCloud Drive toggle is the operator's only job.)
6. `creating-share`: read `\\host.lan\Data\.provision\secret` into memory,
   delete the file, run `C:\OEM\03-create-share.ps1 -PasswordFile` — see 03
   change below. Fail the phase if the secret file is absent.
7. `installing-bridge`: run `C:\OEM\04-bridge-agent.ps1`. Its own preflight
   and 90 s verification are the success criteria; capture its terminating
   error message into the status file on failure.
8. `done`.

Changed `provision/03-create-share.ps1`: add
`param([string]$PasswordFile = "")` at the top; when provided, read the single
line, build the SecureString from it, and delete the file; otherwise keep the
existing embedded `STRONG_PASSWORD_HERE` placeholder path byte-for-byte (the
hygiene check requires the literal placeholder to survive). Nothing else in
the script changes.

Changed `provision/install.bat`: after the debloat step, run
`powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\watcher.ps1 -Install
> C:\OEM\watcher-install.log 2>&1`, and rewrite the `NEXT-STEPS.txt` block:
the app now drives setup; the operator's steps are (1) open the app on the
host, (2) sign in to iCloud when the app says so, leaving iCloud Drive ON and
Files On-Demand ON; the full manual sequence stays listed underneath as the
fallback with the existing wording.

Existing-VM bootstrap (goes in SETUP.md and in the GUI's no-acknowledgement
hint): in an elevated guest PowerShell, once:

```
powershell -ExecutionPolicy Bypass -NoProfile -File \\host.lan\Data\.provision\watcher.ps1 -Install
```

Before writing guest scripts, confirm `icloud` is in the local Administrators
group (`net localgroup administrators` in the guest, or dockur documentation)
— `RunLevel Highest` silently runs unelevated otherwise. If it is not,
`install.bat` (already elevated at OEM time) must add it, and the bootstrap
command documentation must note it. Record the finding in this note's
follow-ups; the operator must check the live VM.

## Host-side work

New `gui/icloud_bridge_gui/guestprov.py` — Qt-free, mount-I/O-free, subprocess
only through injected `Runner`s (import the helpers from `power.py`, as
`firstrun.py` does). Responsibilities:

- `stage(bundle, env_path, runner) -> str`: empty and recreate
  `/tmp/smb/.provision` (`docker exec`), `docker cp` the provision files from
  `bundle.provision_dir`, stream the secret (parse `SHARE_PASS` from the env
  file text locally — reuse the tolerant parsing already in
  `firstrun.read_env_file`, extended to optionally return this one value to
  this module only), write `trigger.json` last. Returns the generated
  `runId` (`provision-<epoch seconds>` is sufficient). The secret goes over
  `docker exec -i <container> sh -c 'umask 077; cat > /tmp/smb/.provision/secret'`
  with the value written to the child's stdin and nowhere else.
- `poll(runner, run_id) -> Status`: `docker exec cat .../status.json`, bounded
  read, JSON-validate, classify into a small frozen dataclass
  (`phase`, `detail`, `error`, `acknowledged: bool` via runId match). Malformed
  JSON is a distinct "unreadable" result, never a crash and never progress.
- `cleanup(runner)`: best-effort deletion of the secret file (and on success,
  the whole `.provision` staging content). Called from a `finally`.
- `guest_os_ready(host, port) -> bool`: the X.224/TPKT probe, reimplemented
  here (~30 lines, stdlib socket). `tools/rdp-ready.py` stays untouched as the
  standalone operator tool; add a cross-reference comment in both places. The
  GUI uses this to distinguish "Windows still installing" from "watcher not
  answering".
- Constants: container name from `power.CONTAINER_NAME`, phase names, the
  per-phase deadlines from the protocol section.

Everything is unit-testable with fake runners; no test may touch Docker.

## GUI wiring

No new lifecycle phases. Auto-provisioning is an activity inside the existing
`Phase.PROVISIONING` (fresh VM) and, for re-runs, inside normal monitoring
with I/O quiesced. The `lifecycle.py` reducer is not edited; wiring lives in
the controller/window layer like every other worker.

- Provisioning state (D31/D39 screens): add **Set up Windows automatically**.
  On click (confirmed): verify container running, then `guest_os_ready`
  gating — if the probe fails, show "Windows is still installing" and retry
  on a timer. Then `stage()` in a worker and start polling `poll()` every 3 s.
  Render phase and detail as the busy line (D38 conventions: elapsed time, no
  percentages, no cancel of the guest run). If no acknowledgement within
  90 s, keep polling but show the one-line bootstrap command (copyable, Setup
  tab row style) with text explaining it is needed once on VMs created before
  this feature. `waiting-for-signin` renders as an instruction card: open the
  VM screen (existing button), sign in, leave iCloud Drive ON and Files
  On-Demand ON — and the GUI simply continues when the phase advances.
  `done` leads into the existing **Check setup and connect** flow unchanged.
  The manual instructions the state shows today remain reachable (collapsed
  or behind "Show manual steps").
- Monitoring state: add a confirmed menu/Status-tab action **Re-run Windows
  provisioning…**, enabled only when the container classification is running
  and no other transaction is in flight. It quiesces bridge I/O and pauses
  polling for the duration (existing `quiesce`/`set_io_paused` machinery),
  runs the same stage/poll flow, then invalidates cached documents and
  refreshes. This is the one-click path for the post-feature-update recovery
  step and for the currently stale VM.
- `provisioning.json` (D39): the record's `phase` field may additionally carry
  the last observed guest phase for display after a GUI restart; its
  classification rules do not change, and it still never stores the env path.
- Failure rendering: `error` statuses show the failing phase, the bounded
  message, and the copyable manual fallback for that phase (03/04 command
  lines from SETUP.md).

## Packaging and docs

- `packaging/build-deb.sh` already ships everything under `provision/`
  (verified: it globs the directory), so `watcher.ps1` and `guest-setup.ps1`
  travel automatically; 0644 is fine because nothing executes them from the
  Linux filesystem. No packaging change expected — verify with `make deb`.
- `SETUP.md`: rewrite the guest-provisioning section around the automated
  flow; keep the full manual sequence as the fallback subsection; add the
  existing-VM bootstrap one-liner.
- `docs/automation-notes.md`: update the scoreboard rows for 02/03/04
  ("Yes — done" pointing at D40-D42) and strike the now-implemented items
  from "Worth doing next".
- `README.md`: one-paragraph mention in the setup overview.
- `CHANGELOG.md`: append per repo convention.
- `docs/plan-gui-selective-sync.md`: register rows, D31 amendment, and the
  protocol subsection under section 4 (see the decisions section above).

## Edge cases already decided

- **Trigger with no watcher** (pre-feature VM): no acknowledgement; GUI shows
  the bootstrap one-liner and keeps polling. Not an error.
- **Reboot mid-run**: the watcher restarts at logon; the consumed trigger is
  gone, so nothing resumes automatically. The operator presses the button
  again; every phase is idempotent.
- **Concurrent agent activity**: 04 already stops the agent task before
  changing anything. No extra coordination.
- **Secret lifetime**: written last-but-one (before the trigger), read and
  deleted by the guest in `creating-share`, deleted by the host in `finally`
  and on the next staging. Never in argv, logs, status, or screenshots
  (nothing is typed or captured in this design).
- **Store/winget failure**: bounded retries, then an error status that names
  the manual Store install fallback; the run can be re-triggered afterwards
  and skips the already-installed client.
- **`icloudtest` leftover share**: out of scope here; 03/04 neither depend on
  nor remove it. Removing it is a separate one-line operator action.
- **Powered-off bridge**: provisioning actions require the container running;
  the existing D30 power classification gates the buttons.
- **Malformed status.json**: rendered as "unreadable", polling continues,
  never treated as progress or success.

## Rejected alternatives (do not reopen)

- **Keystroke injection as the product mechanism**: blind, unverifiable
  without OCR, would type the secret, and `tools/` is unshipped. Stays a
  debugging aid.
- **Baking SHARE_PASS into the image or `install.bat`**: forbidden
  (`CONTRIBUTING.md` "Never commit secrets"), and unchanged.
- **Automating the Apple sign-in / iCloud Drive toggle**: locked out of
  scope; risks account lockout; coordinate-based GUI automation breaks on
  client updates.
- **A guest-side agent2 with its own network listener (WinRM/SSH)**: new
  attack surface on a guest holding an Apple session; the Data share and a
  scheduled task need no new listeners, ports, or firewall changes.
- **Having the existing bridge agent run provisioning**: it is deliberately
  `RunLevel Limited` (D28) and must stay unprivileged; elevation lives only
  in the separate watcher task.

## Milestones (execute in order; commit each with explicit pathspecs)

Another session has `todo/performance-resource-review-2026-07-27.md` staged;
never sweep it into a commit — always name paths.

1. **M0 — Plans.** Add the register rows (claim free D numbers), the D31
   amendment, and the section 4 protocol subsection to
   `docs/plan-gui-selective-sync.md`; append to `CHANGELOG.md`. Verify:
   `make check`. Commit: plan + changelog (+ this note if updated).
2. **M1 — Guest scripts.** `provision/watcher.ps1`,
   `provision/guest-setup.ps1`, the 03 `-PasswordFile` parameter,
   `install.bat` registration + NEXT-STEPS rewrite. Verify: `make check` and
   `make lint-ps` (parse-level only — state that 5.1 behaviour is unproven
   locally). Commit `provision/*` together.
3. **M2 — `guestprov.py` + `gui/tests/test_guestprov.py`.** Fake-runner tests
   for staging order (trigger written last), secret-over-stdin (assert the
   value is absent from every argv), runId matching, phase classification,
   deadlines, malformed-status handling, and the X.224 probe against a fake
   socket. Verify: `make check`. Commit module + tests.
4. **M3 — GUI wiring.** Provisioning-state button and status rendering,
   re-run action with quiesce, D39 record display field, tray/window text.
   Extend `test_qt_wiring.py` with faked guestprov. Verify: `make check`,
   ideally `make test-all`. Commit.
5. **M4 — Docs.** SETUP.md, automation-notes scoreboard, README. Verify:
   `make check`. Commit.
6. **M5 — Operator verification (not possible in this workspace).** On the
   real host, in this order: (a) paste the bootstrap one-liner into the
   existing VM's elevated PowerShell; (b) use **Re-run Windows
   provisioning…** (or the Provisioning-state button) and watch the phases;
   (c) confirm shares `icloud` and `bridge` exist, `status.json` shows
   `agentBuild` matching the bundle, no D35 banner; (d)
   `./host/acceptance-tests.sh` passes; (e) later, prove the OEM path with a
   full VM rebuild (preserve `custom.iso` per SETUP.md, expect the Apple
   sign-in wait as the only manual guest step). Record results in
   `CHANGELOG.md` and update the scoreboard.

## What cannot be verified in this workspace

No KVM, no Windows guest, no Docker guest container here. Everything in M5,
the `RunLevel Highest`/Administrators-membership assumption, Store readiness
timing, SMB behaviour of `\\host.lan\Data` under the `icloud` vs SYSTEM
account, and Windows PowerShell 5.1 runtime behaviour of the new scripts can
only be proven on the operator's host. `make lint-ps` proves syntax only.
