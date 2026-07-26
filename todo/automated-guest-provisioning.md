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
- **A file channel into the guest already exists, but it is not safe as an
  elevated-code channel.** dockur's container `smbd` serves `/tmp/smb` to the
  guest as `\\host.lan\Data` (`docs/automation-notes.md` section 3), and
  `docker cp` / `docker exec -i` reach it from the host without root. However,
  dockur configures that share `writable = yes`, `guest only = yes`, and
  `force user = root` (verified in
  [`src/samba.sh`](https://github.com/dockur/windows/blob/bada80331d67b2d2b13b57ff2a4004a3f2be507d/src/samba.sh)
  at upstream commit `bada80331d67b2d2b13b57ff2a4004a3f2be507d`,
  2026-07-27). Therefore any guest
  process can replace a script staged there. An elevated watcher MUST NOT
  execute code from `Data`; doing so would turn the limited D28 agent (or any
  process in the `icloud` session) into a silent administrator.
- **A precedent for an unattended in-guest service exists.** The bridge agent
  is a Task Scheduler logon task running as `icloud` in an infinite loop with
  restart-on-failure (D17, `provision/04-bridge-agent.ps1` step 8). The same
  pattern, but `RunLevel Highest`, gives elevated execution with no UAC prompt
  and no keystroke injection. dockur's current
  [Windows 11 answer file](https://github.com/dockur/windows/blob/bada80331d67b2d2b13b57ff2a4004a3f2be507d/assets/win11x64.xml)
  puts the configured local user in `Administrators`, but the image is
  unpinned, so the installer still has to assert both group membership and that
  it is currently elevated before registering the task.
- **`provision/` → `C:\OEM` is copied at install time only**, so OEM copies go
  stale the moment the repo moves (observed live: the current VM's `C:\OEM` is
  four commits behind, including the D35 skew detection). Any automation that
  runs OEM copies would install stale code; the channel must stage current
  scripts per run.
- **Scripts 03 and 04 are logically idempotent**, and 04 already stops the
  agent task, preserves `exclusions.json`, and fails closed on a missing sync
  root or `syncshare` account. Script 03 is not yet a machine-checkable
  automation boundary: it lacks terminating-error/native-exit handling and a
  final verification, so it must be tightened before an orchestrator may treat
  exit zero as success.
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

One sentence: the host creates a dedicated **read-only-to-the-guest** Samba
share named `Provision`, stages current scripts there, and writes a trigger
last; an elevated watcher task inside the guest (registered at OEM install
time, or once by hand on an existing VM) copies the payload into an
administrator-only directory and runs an orchestrator that installs iCloud,
waits for sign-in, asks the host for the password only when it is ready to run
03, runs 03 and 04, and reports progress through the existing writable `Data`
share.

Why this shape and not keystroke injection: every step is verified by effect
(files and JSON, not screenshots), no secret is ever typed or displayed, the
watcher is registered by code that is already running elevated (OEM
`install.bat`), and the only remaining manual guest interaction is the Apple
sign-in itself.

Trust argument (goes into the decision rationale): the container already has
total control of the guest through the QEMU monitor socket and delivered
`C:\OEM` at install time; the host root/docker-group user is the security
boundary (v1 D9). The new `Provision` share exposes a fixed container directory
with `read only = yes`; its writable container-side path is reachable only
through Docker. Status remains on `Data` and is treated as untrusted input, but
no file reachable through the guest-writable path is ever executed elevated.
The watcher and per-run payload live under a hardened
`C:\ProgramData\icloud-bridge-provision`, not `C:\OEM`. The exclusion boundary
is untouched: nothing here grants `syncshare` anything (D15/D27/D28 invariants
remain enforced by 03/04).

## Decision rows to move into the v2 plan register

Claim the next free numbers when editing (D40-D43 if still free; re-check at
commit time per `CONTRIBUTING.md`). Draft text:

- **D40 — Guest provisioning channel.** Before staging, the host idempotently
  creates `/run/icloud-bridge-provision` inside the container and installs a
  fixed `[Provision]` stanza in dockur's generated Samba configuration:
  `path = /run/icloud-bridge-provision`, `read only = yes`, `guest ok = yes`,
  `guest only = yes`, `force user = root`. It validates the candidate with
  `testparm`, atomically replaces the config, and reloads `smbd`; it never
  edits the guest-writable `Data` path. An elevated scheduled task
  `icloud-bridge-provision` (principal `icloud`, `LogonType Interactive`,
  `RunLevel Highest`, at-logon, infinite loop with restart, `IgnoreNew`) runs
  the hardened installed
  `C:\ProgramData\icloud-bridge-provision\watcher.ps1`, polling
  `\\host.lan\Provision\trigger.json` every 30 s. It validates the trigger,
  copies the fixed payload allowlist into an administrator-only per-run
  directory, atomically records the accepted run ID locally, and executes that
  protected copy. Because the share is read-only, the trigger is removed by
  the host during cleanup rather than by the watcher; the local accepted-run
  marker provides consume-once semantics. Progress JSON is written atomically
  to the separate guest-writable
  `\\host.lan\Data\.provision\status.json` and is always parsed as untrusted
  input. `install.bat` registers the task at OEM time; on a VM installed before
  this feature, the operator pastes one elevated bootstrap command (below)
  once. QEMU-monitor keystroke injection remains a `tools/` debugging aid and
  is never installed or invoked by the app. Rationale: verified-by-effect,
  secretless typing surface, reuses the D17 task pattern, and does not create a
  guest-local elevation path; only host root/docker-group can write executable
  input, preserving D9 and D28.
- **D41 — SHARE_PASS delivery (amends the D31 "never handled" boundary).** A
  new Qt-free, mount-I/O-free module `guestprov.py` is the only GUI code
  permitted to return the `SHARE_PASS` value. The initial trigger carries
  **no secret**.
  After the guest reports `waiting-for-secret`, `guestprov.py` re-reads the
  explicitly selected env file and streams the exact UTF-8 value over
  `docker exec -i` stdin to a run-scoped temporary file in the container
  inbox, then atomically renames it to `secret` (never argv, never environment,
  never a host temp file, never logged, never in status, never on the
  clipboard, never persisted by the GUI). The guest waits while the file is
  absent; when it appears, the elevated orchestrator copies it without decoding
  it to a protected local temporary file and only then advances to
  `creating-share`. That phase transition acknowledges the copy, so the host
  deletes the read-only remote secret. `03-create-share.ps1 -PasswordFile`
  reads the protected local copy once and deletes it in a `finally` block before
  changing the account. The orchestrator never parses or rewrites the password
  itself. Host cleanup may delete an unacknowledged remote secret on completion,
  failure, explicit app exit, and before a new run; deleting it merely leaves
  the guest in `waiting-for-secret`, so a restarted GUI can ask for the env file
  and deliver it again. The orchestrator deletes its protected local copy in an
  outer `finally`, and every watcher task start/new-run preflight removes stale
  local `secret` files left by a reboot before 03 could consume them. The manual
  edit-the-placeholder path in 03 remains for fallback. Rationale: delivery is
  atomic and occurs immediately before use instead of leaving a secret in the
  guest across an unbounded Apple sign-in wait.
- **D42 — Provisioning script currency.** Every trigger re-stages the
  installed bundle's current `03-create-share.ps1`, `04-bridge-agent.ps1`,
  `agent.ps1`, `guest-setup.ps1`, and `watcher.ps1` into
  the read-only inbox before the trigger. The watcher copies that allowlist to
  a protected per-run directory before execution; `04-bridge-agent.ps1`
  resolves its sibling `agent.ps1` from `$PSScriptRoot` rather than trusting
  `C:\OEM`. After validation, the orchestrator transactionally refreshes an
  administrator-only `current` directory for the documented
  manual fallback and D35 banner; `C:\OEM` may also be refreshed for operator
  inspection but is never the elevated execution source. The installed watcher
  is refreshed in its protected directory for its **next task start**. The tiny
  watcher envelope (`version: 1`, fixed filenames, UUID run ID) is deliberately
  stable; changing that envelope requires re-running the documented bootstrap,
  because a currently running old watcher cannot safely upgrade the protocol
  that authenticates its own replacement. Rationale: current code is used
  without introducing a `C:\OEM` time-of-check/time-of-use elevation hole, and
  the limit of watcher self-update is explicit rather than claiming its
  staleness can never matter.
- **D43 — Durable guest-provisioning transactions (amends D39).** The private
  provisioning record covers both `first-run` and `reprovision` and additionally
  stores the container ID and Docker `State.StartedAt` token, guest run ID, last
  guest phase, and mode — never the env path or password. It is written before
  the trigger and updated only after a matching status is parsed. A restart
  with a matching live container/start token and active run re-enters the
  existing no-CIFS `Phase.PROVISIONING` and polls the recorded run ID; if the
  guest is waiting for a secret, the operator reselects the env file. A changed
  container start token plus missing/stale status offers a confirmed new
  idempotent run rather than adopting an uncorrelated status. Without restart
  evidence, a missing status keeps polling and requires an explicit
  **Abandon and start a new run** confirmation; it never silently overwrites a
  possibly live wait. A record still in host `staging` with no acknowledged
  status retries `ensure_channel()`/`stage()` using the **same** saved run ID,
  covering a crash before the trigger's atomic rename.
  First-run success continues to **Check setup and connect**; reprovision
  success verifies the current bridge protocol/agent build, clears the record,
  invalidates caches, and returns to monitoring. No status is trusted merely
  because it is the newest file.

Also amend, in the same commit: the D31 register row (its "password never
passes through the GUI" sentence gains "except as provided by D41"), the D39
row per D43, the `firstrun.py` module docstring, and v2 plan section 4 gains a
subsection specifying the protocol below (plans own decisions; this todo is not
the spec of record once that lands).

## Protocol (exact formats)

Executable inbox (host-writable, guest-read-only): container
`/run/icloud-bridge-provision/`, guest `\\host.lan\Provision\`.

Status outbox (guest-writable, never executable): container
`/tmp/smb/.provision/`, guest `\\host.lan\Data\.provision\`.

`guestprov.ensure_channel()` reconstructs only its marker-delimited
`[Provision]` stanza in a temporary copy of dockur's generated `smb.conf`,
validates that candidate with `testparm`, atomically replaces the config, and
reloads `smbd`. A same-named stanza outside its own complete marker block is a
conflict and fails closed, not something to merge or override. All shell
programs and paths in this command are fixed constants; no env value, run ID,
bundle path, status text, or password is interpolated into shell source. The
inbox is mode 0700 and root-owned in the container. The host refuses to stage
unless the effective Samba configuration reports the exact share path and
`read only = Yes`.

`trigger.json`, written by the host last after the scripts are in place (the
secret is deliberately absent at this point):

```json
{"version":1,"runId":"<32 lowercase UUID4 hex characters>","action":"provision"}
```

`status.json`, written by the guest orchestrator with the same atomic
temp-then-rename pattern as `Write-JsonAtomic` in 04:

```json
{"version": 1, "runId": "<echoed>", "phase": "<see list>",
 "detail": "<one bounded human line>", "updatedAt": "<ISO-8601 UTC>",
 "error": null}
```

Phases, in order: `staging` (payload copied to the protected run directory),
`installing-icloud`, `launching-icloud`, `waiting-for-signin` (polling for the
sync root every 15 s, unbounded — this is the manual step),
`waiting-for-secret` (polling for the atomically delivered run-scoped secret,
also unbounded), `creating-share`, `installing-bridge`, `done`. On failure:
`phase` stays at the failing phase and `error` becomes a bounded message.
Unknown trigger version/action, an invalid run ID, a missing payload file, or a
copy failure makes the watcher write an error status for that run and mark it
consumed locally; it never loops on the same bad trigger. The GUI treats any
unknown status phase/version or malformed field as an error, never as progress.

Rules a weaker model must not improvise around:

- The host matches `runId` before trusting a status file; a stale or
  mismatched `runId` is "no acknowledgement yet", not progress.
- `runId` is generated with `uuid.uuid4().hex`, validated as exactly 32
  lowercase hex characters on both sides, and stored in D43's private record.
  A timestamp is neither unique enough nor an acceptable path component.
- The watcher executes only protected local copies of the five allowlisted
  files. Nothing under `Data` or `C:\OEM` is an execution source.
- The secret file never has a mention in `status.json`, `detail`, or any log.
- The secret is exact UTF-8 with no added newline. Its grammar is deliberately
  small: exactly one physical line beginning `SHARE_PASS=` in column 1; every
  byte after the first `=` is value (so `#` and later `=` are data), with no
  surrounding quote processing, leading/trailing whitespace, NUL or CR/LF.
  Duplicate or quoted forms are rejected rather than interpreted differently.
  The same rule must be used by `firstrun.py`, `guestprov.py`, and
  `host/icloud-bridge-configure`, so the guest account and
  `/etc/credentials-icloud` cannot silently receive different passwords.
- `waiting-for-signin` has no timeout. Every other phase gets a per-phase
  deadline on the host side (10 min for `installing-icloud`, 5 min for each
  other phase); a deadline or a status mtime frozen for over 120 s during an
  active phase surfaces a "stalled" warning with the manual fallback commands,
  while polling continues — the guest may merely be slow. The orchestrator
  rewrites a heartbeat status at least every 30 s during both waits and while
  each child PowerShell/winget process is running, so 120 s of silence is
  meaningful rather than guaranteed false-positive noise. The two waiting
  phases have no elapsed deadline, only the heartbeat check.
- Re-triggering is always safe: 03/04 are idempotent, `installing-icloud`
  skips when `Get-AppxPackage AppleInc.iCloud` already answers, and the
  watcher consumes triggers one at a time. A new run is not staged over an
  acknowledged active run; after a reboot, error, or explicit retry it gets a
  new UUID and its own protected run directory.
- `detail` and `error` are single-line strings capped at 500 characters after
  control-character removal. Status reads are capped at 64 KiB. The host keeps
  the matching terminal status until the D43 record has been cleared; cleanup
  removes executable inbox content and any secret, not the only evidence
  needed to resume safely after a GUI crash.

## Guest-side work

New `provision/watcher.ps1` (unnumbered: machinery, like `agent.ps1`, not an
operator sequence step):

- Header per repo style (what/where/how/idempotent). LF endings.
- `-Install` switch: require an elevated token; resolve `icloud` by SID and
  assert that it is a member of the built-in Administrators SID
  `S-1-5-32-544` (do not depend on the localized group name). Create
  `C:\ProgramData\icloud-bridge-provision`, replace inherited permissions with
  SYSTEM + built-in Administrators only, copy itself there, register the
  `icloud-bridge-provision` task exactly as D40 specifies (mirror the
  Register-ScheduledTask block in 04 step 8, changing principal RunLevel to
  Highest and the target to the protected installed script), start it, exit.
  Idempotent via `-Force`; fail with a useful message rather than silently
  adding `icloud` to Administrators.
- Default mode: infinite loop, 30 s sleep. Parse at most 64 KiB of
  `\\host.lan\Provision\trigger.json`; validate its complete schema and compare
  the run ID to the protected local accepted-run marker. For a new valid run,
  copy the five fixed payload filenames to a new protected
  `runs\<runId>` directory, verify every copy exists, atomically record the run
  ID locally, refresh the protected installed watcher for the next task start,
  and execute the protected `guest-setup.ps1` with the run ID. The remote
  trigger is read-only and remains until host cleanup. Wrap every trigger
  attempt so malformed JSON, unsupported versions/actions and copy/launch
  failures produce bounded error status instead of terminating the watcher. At
  task start and before accepting a new UUID, remove stale protected local
  `secret` files left by a reboot; while `guest-setup.ps1` is running, the
  watcher is synchronously blocked and must not touch its active secret. After
  a later run has been promoted to `current` and its local secret is gone,
  prune superseded run directories; never delete the active or current payload.

New `provision/guest-setup.ps1` (the orchestrator; elevated; idempotent):

1. Validate the run ID and that `$PSScriptRoot` is the matching protected run
   directory. Write `status.json` (`staging`, echoing the run ID passed by the
   watcher). Reuse the atomic-write pattern from 04, plus the protocol's bounds
   and control-character stripping.
2. Transactionally refresh the protected `current` directory used by the
   manual fallback: build and verify a sibling temporary directory, swap it
   with rollback so failure retains the prior complete bundle, then prune the
   old copy. Optionally refresh convenience
   copies of
   `03-create-share.ps1`, `04-bridge-agent.ps1`, `agent.ps1`, `watcher.ps1` and
   `guest-setup.ps1` under `C:\OEM` for inspection, but execute only the
   siblings under the protected run directory. Documentation and the D35
   banner must never tell the operator to elevate an unprotected convenience
   copy once the protected `current` bundle exists.
3. `installing-icloud`: skip if `Get-AppxPackage AppleInc.iCloud` returns a
   package; else `winget install --id 9PKTQ5699M62 --source msstore
   --accept-package-agreements --accept-source-agreements`, retried up to 5
   times 120 s apart (Store readiness is flaky at first boot — plan section 5).
   Check the native exit code explicitly; a non-zero `$LASTEXITCODE` is not a
   PowerShell exception. Bound each attempt to 10 min. On exhaustion, error
   status naming the manual Store fallback from 02's header.
4. `launching-icloud`: if the sync root `C:\Users\icloud\iCloudDrive` is
   missing, launch the client non-elevated from this elevated context with
   `explorer.exe shell:AppsFolder\AppleInc.iCloud_nzyj5cx40ttqa!iCloud`
   (documented working method, automation-notes section 3).
5. `waiting-for-signin`: poll `Test-Path` for the sync root every 15 s,
   forever, writing a heartbeat each pass. (Sign-in plus the iCloud Drive
   toggle is the operator's only guest interaction.)
6. `waiting-for-secret`: poll
   `\\host.lan\Provision\secret` every 5 s, writing a heartbeat each pass. It is
   not an error for the file to be absent after a GUI exit/restart.
7. After the remote secret appears, copy it without text decoding to a
   protected local temporary file, then write `creating-share` status (the
   host's acknowledgement to remove the remote copy) and run the protected
   sibling `03-create-share.ps1 -PasswordFile <protected-local-path>`. Script
   03 reads and immediately deletes the local file. If host cleanup wins before
   the copy, the guest stays in `waiting-for-secret`; if the phase advances,
   the full atomically-renamed value is already protected locally. Wrap all
   remaining work in an outer `finally` that deletes this local secret even
   when 03 cannot be launched.
8. `installing-bridge`: run the protected sibling
   `04-bridge-agent.ps1`. Its own preflight and 90 s verification are the
   success criteria.
9. Run winget and both child PowerShell scripts through one helper that starts
   the child with stdout/stderr redirected to protected per-run temporary
   files, rewrites heartbeat status while it is active, checks the exit code,
   reads only a bounded sanitized tail on failure, and deletes the temporary
   output. Do not use `&` and assume a native non-zero exit becomes a
   terminating PowerShell error.
10. `done`.

Changed `provision/03-create-share.ps1`: add
`param([string]$PasswordFile = "")` at the top and
`$ErrorActionPreference = 'Stop'`. When provided, read the whole BOM-less UTF-8
file without adding/removing a newline, reject NUL/CR/LF or an empty value,
build the SecureString, and delete the file in `finally` immediately after the
read; otherwise keep the existing embedded `STRONG_PASSWORD_HERE` placeholder
path (the hygiene check requires the literal placeholder to survive). Add an
elevation/sync-root preflight, check every native `icacls` exit code, and verify
the resulting local account, share path/access, service state and SMB settings
before returning zero. These checks are required: the current script contains
non-terminating cmdlets/native calls, so merely launching it from an
orchestrator cannot prove `creating-share` succeeded.

Changed `provision/04-bridge-agent.ps1`: resolve the source `agent.ps1` beside
the invoked script (`Join-Path $PSScriptRoot 'agent.ps1'`) rather than hard-code
`C:\OEM\agent.ps1`. This behaves identically for the manual fallback and lets
automation execute a protected, coherent payload.

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
powershell -ExecutionPolicy Bypass -NoProfile -File \\host.lan\Provision\watcher.ps1 -Install
```

The installer performs the SID-based membership and elevation checks itself.
Current dockur puts its configured local user in Administrators, but that
upstream implementation detail is not a substitute for the runtime assertion.

## Host-side work

New `gui/icloud_bridge_gui/guestprov.py` — Qt-free, mount-I/O-free, subprocess
only through injected adapters. The existing `power.Runner` has no stdin
parameter, so do **not** claim it can deliver the password: define a narrow
`InputRunner(argv, timeout, input_bytes) -> RunResult` beside the ordinary
runner, with a real implementation using `subprocess.run(..., input=bytes)`
and fakes that assert on the bytes inside the call and retain only a byte count.
Responsibilities:

- `ensure_channel(runner)`: install/verify D40's marker-delimited read-only
  Samba share using fixed shell input, after validating a temporary config with
  `testparm`; atomically replace and reload only on success. A failure leaves
  dockur's working config untouched. Verify the effective share path and
  read-only setting after reload.
- `new_run_id() -> str`: return `uuid.uuid4().hex`. The controller obtains the
  ID and durably records it **before** any trigger can exist.
- `stage(bundle, run_id, runner, input_runner)`: validate the supplied ID and
  refuse to replace an acknowledged nonterminal run; empty and recreate the
  fixed container inbox, `docker cp` the five allowlisted files from
  `bundle.provision_dir` to temporary names, atomically rename them, then stream
  the non-secret trigger JSON through the input runner to a temporary file and
  atomically rename it last. There is no env path and no secret in this
  operation.
- `deliver_secret(env_path, run_id, input_runner)`: re-read and validate the
  explicitly selected env file, extract only `SHARE_PASS`, and stream its exact
  UTF-8 bytes over
  `docker exec -i <container> sh -c <fixed atomic-write command>`. The command
  writes a mode-0600 temporary file and renames it to `secret`; the value is in
  stdin only. Delivery is allowed only for a matching `waiting-for-secret`
  status. It never overwrites an already-present secret for that run: after a
  GUI crash, the app either waits for the guest's acknowledgement or explicitly
  cleans up before re-delivery.
- `poll(runner, run_id) -> Status`: use a fixed `docker exec` command to obtain
  the status file's container-side mtime/size and bounded content, JSON-validate,
  and classify into a small frozen dataclass
  (`phase`, `detail`, `error`, `acknowledged: bool` via runId match). Malformed
  JSON is a distinct "unreadable" result, never a crash and never progress.
- `cleanup(runner, run_id)`: best-effort deletion of the matching inbox
  trigger/payload and remote secret. Never delete a mismatched/newer run or the
  matching terminal status before D43's record is cleared. Call it after
  acknowledgement, failure, explicit exit, and before a confirmed retry; a
  process crash is handled by the next staging pass, not by pretending
  `finally` is guaranteed.
- `guest_os_ready(host, port) -> bool`: the X.224/TPKT probe, reimplemented
  here (~30 lines, stdlib socket). `tools/rdp-ready.py` stays untouched as the
  standalone operator tool; add a cross-reference comment in both places. The
  GUI uses this to distinguish "Windows still installing" from "watcher not
  answering".
- Constants: container name from `power.CONTAINER_NAME`, phase names, the
  per-phase deadlines from the protocol section.

Move env parsing to one Qt-free helper that returns an `EnvReport` plus the
secret only to `guestprov`; `firstrun` receives the report without the value.
Update `host/icloud-bridge-configure` to accept exactly the same normalized
syntax (including duplicate/quote rejection) before writing
`/etc/credentials-icloud`. This is a correctness requirement, not parser
cleanup: automation must not configure Windows with a different password from
the host mount. Everything is unit-testable with fake runners; no test may
touch Docker or retain/print the secret.

## GUI wiring

No new lifecycle **phase**, but the reducer must be extended rather than
bypassed. Add explicit begin/success/failure events that enter the existing
`Phase.PROVISIONING` from both Setup and Monitoring, keep CIFS work paused, and
return according to D43's `first-run`/`reprovision` mode. The controller owns
Docker/file I/O; `lifecycle.py` remains a pure reducer.

- Provisioning state (D31/D39 screens): add **Set up Windows automatically**.
  On click (confirmed): verify container running, then `guest_os_ready`
  gating — if the probe fails, show "Windows is still installing" and retry
  on a timer. Then generate the run ID, write it in D43's record, run
  `ensure_channel()` + `stage(..., run_id, ...)` in a worker, and start polling
  `poll()` every 3 s, with at most one Docker poll worker in flight. There must
  be no trigger-without-record crash window.
  Render phase and detail as the busy line (D38 conventions: elapsed time, no
  percentages, no cancel of the guest run). If no acknowledgement within
  90 s, keep polling but show the one-line bootstrap command (copyable, Setup
  tab row style) with text explaining it is needed once on VMs created before
  this feature. `waiting-for-signin` renders as an instruction card: open the
  VM screen (existing button), sign in, leave iCloud Drive ON and Files
  On-Demand ON — and the GUI simply continues when the phase advances. On
  `waiting-for-secret`, require an env selection in this process, run
  `deliver_secret()` once, and keep polling; after restart, explain why the env
  file must be selected again rather than recovering a stored path. The chooser
  states that this value resets the guest `syncshare` password and must match
  `/etc/credentials-icloud`; the GUI cannot read that root-only file, so if the
  operator intentionally selects a changed password it gives the exact
  `sudo icloud-bridge-configure --env-file ...` follow-up rather than claiming
  the host credential was changed automatically.
  `done` leads into the existing **Check setup and connect** flow unchanged.
  The manual instructions the state shows today remain reachable (collapsed
  or behind "Show manual steps").
- Monitoring state: add a confirmed menu/Status-tab action **Re-run Windows
  provisioning…**, enabled only when the container classification is running
  and no other transaction is in flight. It quiesces bridge I/O and pauses
  GUI polling for the duration (existing `quiesce`/`set_io_paused` machinery),
  writes a `reprovision` record, and runs the same stage/poll flow. The
  confirmation must accurately say that the systemd mounts remain mounted and
  the app cannot police another process using them; the operator must close
  files and shells under `/mnt/icloud*` before continuing. Do not call this
  full host-I/O quiescence. On success, verify a compatible status and the
  bundled agent build through a fresh bridge gather, then clear the record,
  invalidate cached documents and refresh. This is the one-click path for the
  post-feature-update recovery step and for the currently stale VM.
- `provisioning.json` (D39/D43): bump the schema and add `mode`,
  `containerStartedAt`, `guestRunId`, and `guestPhase`; preserve its existing
  private atomic-write rules. The run ID is required to reattach safely after
  a GUI restart, while `containerStartedAt` distinguishes a container restart
  from a merely quiet watcher. A `reprovision` record is subject to the same
  startup-before-CIFS gate as a first-run record; otherwise a restarted app
  could mount while 03/04 are changing guest state.
- Failure rendering: `error` statuses show the failing phase, the bounded
  message, and the copyable manual fallback for that phase (03/04 command
  lines from SETUP.md). A mismatched run ID never replaces the saved run or
  clears the record.

## Packaging and docs

- `packaging/build-deb.sh` already ships everything under `provision/`
  (verified: it globs the directory), so `watcher.ps1` and `guest-setup.ps1`
  travel automatically; 0644 is fine because nothing executes them from the
  Linux filesystem. `guestprov.py` is included with the existing Python package.
  No packaging-path change expected — still verify with `make deb`.
- `SETUP.md`: rewrite the guest-provisioning section around the automated
  flow; keep the full manual sequence as the fallback subsection; add the
  existing-VM bootstrap one-liner.
- `docs/automation-notes.md`: update the scoreboard rows for 02/03/04
  ("Yes — done" pointing at D40-D43) and strike the now-implemented items
  from "Worth doing next".
- `README.md`: one-paragraph mention in the setup overview.
- `CHANGELOG.md`: append per repo convention.
- `docs/plan-gui-selective-sync.md`: register rows, D31/D39 amendments, and the
  protocol subsection under section 4 (see the decisions section above).

## Edge cases already decided

- **Trigger with no watcher** (pre-feature VM): no acknowledgement; GUI shows
  the bootstrap one-liner and keeps polling. Not an error.
- **Reboot mid-run**: the watcher restarts at logon; the consumed trigger is
  still marked accepted locally, so the same run does not execute twice.
  Status may have disappeared when dockur rebuilt `/tmp/smb`; the GUI offers a
  confirmed new UUID/run, and every phase is idempotent.
- **Concurrent agent activity**: 04 already stops the agent task before
  changing anything. No extra coordination.
- **Secret lifetime**: absent during install/sign-in; atomically written only
  for `waiting-for-secret`, copied into the protected run directory, then
  removed remotely by the host when `creating-share` acknowledges the copy and
  locally by 03 immediately after reading (or by the orchestrator's outer
  `finally`). Watcher startup removes a local copy stranded by a guest reboot.
  Best-effort cleanup runs on every terminal/exit/retry path. Never in argv,
  environment, logs, status, a host temp file, or screenshots (nothing is
  typed or captured in this design).
- **Store/winget failure**: bounded retries, then an error status that names
  the manual Store install fallback; the run can be re-triggered afterwards
  and skips the already-installed client.
- **`icloudtest` leftover share**: out of scope here; 03/04 neither depend on
  nor remove it. Removing it is a separate one-line operator action.
- **Powered-off bridge**: provisioning actions require the container running;
  the existing D30 power classification gates the buttons.
- **Malformed status.json**: rendered as "unreadable", polling continues,
  never treated as progress or success.
- **Guest attempts to replace provisioning code**: `Data` remains writable but
  is status-only; `Provision` is read-only in the effective Samba config and
  the task executes only its administrator-protected local copy. Failure to
  establish or verify that property blocks staging.
- **GUI restart during a re-run**: D43's record enters no-CIFS Provisioning
  before ordinary startup, matches only its saved run ID, and either reattaches
  or offers a new run. It never resumes normal mount polling just because the
  container is running.

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
  attack surface on a guest holding an Apple session; the existing Samba
  daemon plus a scheduled task need no new listeners, ports, or firewall
  changes.
- **Having the existing bridge agent run provisioning**: it is deliberately
  `RunLevel Limited` (D28) and must stay unprivileged; elevation lives only
  in the separate watcher task.
- **Executing from dockur's existing `Data` share or from `C:\OEM`**: both are
  writable from a non-elevated guest context or have an insufficiently strong
  ACL contract for an elevation boundary. `Data` is status-only and `C:\OEM`
  is a convenience copy; neither is trusted code input.

## Milestones (execute in order; commit each with explicit pathspecs)

The repository may contain other sessions' staged or unstaged work; never
sweep it into a commit — always name paths.

1. **M0 — Plans.** Add the register rows (claim free D numbers), the D31/D39
   amendments, and the section 4 protocol subsection to
   `docs/plan-gui-selective-sync.md`; append to `CHANGELOG.md`. Verify:
   `make check`. Commit: plan + changelog (+ this note if updated).
2. **M1 — Guest scripts.** `provision/watcher.ps1`,
   `provision/guest-setup.ps1`, the 03 `-PasswordFile` parameter,
   04 `$PSScriptRoot` source, protected watcher install, heartbeat child runner,
   and `install.bat` registration + NEXT-STEPS rewrite. Verify: `make check`
   and `make lint-ps` (parse-level only — state that 5.1 behaviour is unproven
   locally). Commit `provision/*` together.
3. **M2 — `guestprov.py`, env parsing, and tests.** Add
   `gui/tests/test_guestprov.py`; update `firstrun` tests and
   `host/icloud-bridge-configure` together so password syntax stays identical.
   Fake-runner tests cover safe Samba candidate/rollback/verification, staging
   order (trigger last), UUID validation, refusal to overwrite an active run,
   deferred atomic secret-over-stdin (assert the value is absent from every
   argv/result/log), run-ID matching, phase classification, heartbeat/deadline
   behavior, malformed status and the X.224 probe against a fake socket. Verify:
   `make check`. Commit the module, parser/configurer and tests together.
4. **M3 — GUI wiring.** Provisioning-state button and status rendering,
   re-run action with accurately scoped GUI-I/O quiesce, D43 record/resumption,
   reducer events, tray/window text. Extend lifecycle, first-run and Qt wiring
   tests with faked guestprov, including restart during first-run and
   reprovision plus env re-selection at `waiting-for-secret`. Verify:
   `make check`, ideally `make test-all`. Commit.
5. **M4 — Docs.** SETUP.md, automation-notes scoreboard, README. Verify:
   `make check`. Commit.
6. **M5 — Operator verification (not possible in this workspace).** On the
   real host, in this order: (a) stage once, confirm `testparm -s` shows
   `Provision` read-only, and prove that creating/replacing a file through
   `\\host.lan\Provision` fails while status writes through `Data` work;
   (b) paste the bootstrap one-liner into the existing VM's elevated
   PowerShell; (c) use **Re-run Windows
   provisioning…** (or the Provisioning-state button) and watch the phases;
   interrupt the GUI once during `waiting-for-signin` and once immediately
   after secret delivery to prove D43/re-delivery; (d) confirm shares `icloud`
   and `bridge` exist, `status.json` shows
   `agentBuild` matching the bundle, no D35 banner; (e)
   `./host/acceptance-tests.sh` passes; (f) later, prove the OEM path with a
   full VM rebuild (preserve `custom.iso` per SETUP.md, expect the Apple
   sign-in wait as the only manual guest step). Record results in
   `CHANGELOG.md` and update the scoreboard.

## What cannot be verified in this workspace

No KVM, no Windows guest, no Docker guest container here. Everything in M5,
the `RunLevel Highest` behavior, Store readiness timing, the live container's
Samba reload/read-only enforcement, SMB access to `Provision`/`Data` from the
elevated interactive task, and Windows PowerShell 5.1 runtime behavior of the
new scripts can only be proven on the operator's host. Current dockur upstream
confirms that its configured local user is placed in Administrators, but the
runtime assertion and M5 remain required because this repository uses an
unpinned image. `make lint-ps` proves syntax only.
