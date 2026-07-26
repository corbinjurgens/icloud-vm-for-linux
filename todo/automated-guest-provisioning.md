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
Every run first inventories a fixed set of guest invariants, renders that
checklist in the app, repairs only missing or drifted components, and verifies
the complete desired state again. A strange or ambiguous state fails closed
with a specific recovery action; it is never guessed past.

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
administrator-only directory and runs an orchestrator that inventories the
VM, installs or repairs only the components whose fixed invariants are not
satisfied, waits for sign-in or a share password only when the selected work
actually requires them, verifies the resulting desired state, and reports the
checklist and progress through the existing writable `Data` share.

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

Claim the next free numbers when editing (D40-D44 if still free; re-check at
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
  `agent.ps1`, `guest-state.ps1`, `guest-setup.ps1`, and `watcher.ps1` into
  the read-only inbox before the trigger. The watcher copies that allowlist to
  a protected per-run directory before execution; `04-bridge-agent.ps1`
  resolves its sibling `agent.ps1` from `$PSScriptRoot` rather than trusting
  `C:\OEM`. After validation, the orchestrator transactionally refreshes an
  administrator-only `current` directory for the documented
  manual fallback; `C:\OEM` may also be refreshed for operator
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
  guest phase, mode, and `resetShareCredential` intent — never the env path or
  password. It is written before the trigger and updated only after a matching
  status is parsed. A restart
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
- **D44 — Inspect, reconcile, verify.** A provisioning run is a desired-state
  reconciliation, not an unconditional replay of scripts 03 and 04. Before
  changing guest configuration, the protected orchestrator evaluates the
  fixed checklist and publishes its observations plus a fixed-enum work plan.
  `ok` components are skipped. Safely repairable `missing` or `drifted`
  components invoke only their owning repair scope; `blocked` or `unknown`
  observations stop before mutation rather than being treated as absence.
  After the selected repairs, the orchestrator evaluates the full checklist
  again and reports `done` only when every required invariant is `ok` (the
  password is the one explicitly labelled `unverifiable` exception below).
  First-run requests reset/create the share credential; ordinary
  reprovisioning preserves an existing credential unless the account is
  missing or the operator explicitly selects **Reset share password**. The
  app renders the checklist and proposed/completed work, but the elevated
  scripts independently re-probe every precondition and never authorize a
  mutation from guest-writable status JSON. Rationale: an agent update must not
  reset a working SMB password or rewrite unrelated ACLs, while a partial
  or manually altered VM still converges safely and reports exactly what
  prevented convergence.

Also amend, in the same commit:

- the D31 register row (its "password never passes through the GUI" sentence
  gains "except as provided by D41");
- the D39 register row, per D43;
- a new desired-state reconciliation subsection and D44 register row;
- the D35 register row and its E12 acceptance item (v2 plan section 8):
  recovery for skew and incompatibility becomes an unconditional entry into
  the confirmed **Re-run Windows provisioning…** action (see GUI wiring) —
  not a decision about which guest script path exists, and never a `C:\OEM`
  instruction. Rewrite the row's "stays a copyable instruction" rationale:
  the GUI still holds no guest-admin credentials and still never updates
  guest code silently — elevation lives in the guest watcher, and the update
  happens only through this explicit confirmed action;
- the `firstrun.py` module docstring;
- v2 plan section 4 gains a subsection specifying the protocol below (plans
  own decisions; this todo is not the spec of record once that lands).

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
{"version":1,"runId":"<32 lowercase UUID4 hex characters>",
 "action":"reconcile","resetShareCredential":false}
```

`action` is exactly `reconcile`; no arbitrary command or repair-step list
crosses the elevation boundary. `resetShareCredential` is a JSON boolean:
`true` for first-run, or when the operator explicitly chooses **Reset share
password**; otherwise it is `false`. The protected orchestrator, not the host,
derives the work list from a fresh inspection. Adding an inspect-only action is
not required for this feature: every confirmed setup/re-provision run exposes
its inspection before and during repair, and ordinary bridge monitoring remains
the cheap continuous health probe.

`status.json`, written by the guest orchestrator with the same atomic
temp-then-rename pattern as `Write-JsonAtomic` in 04:

```json
{"version": 1, "runId": "<echoed>", "phase": "<see list>",
 "detail": "<one bounded human line>", "updatedAt": "<ISO-8601 UTC>",
 "error": null,
 "checks": {
   "icloudPackage": "<check state>", "syncRoot": "<check state>",
   "shareAccount": "<check state>", "shareCredential": "<check state>",
   "dataShare": "<check state>", "bridgeBoundary": "<check state>",
   "agentInstall": "<check state>", "agentRuntime": "<check state>"
 },
 "work": ["<zero or more fixed work IDs>"]}
```

Check states are exactly `pending`, `ok`, `missing`, `drifted`, `blocked`,
`unknown`, or `unverifiable`. Work IDs are exactly `install-icloud`,
`wait-for-signin`, `create-share-account`, `reset-share-credential`,
`repair-data-share`, `repair-bridge-boundary`, and `update-agent`. The GUI
validates the complete key set, states, work IDs, types, and size before
rendering locally owned labels. Missing/extra keys or impossible combinations
make the status unreadable; guest-provided `detail` is never used to decide
what code runs.

Phases, in order when the corresponding work exists: `staging` (payload copied
to the protected run directory), `inspecting`, `installing-icloud`,
`launching-icloud`, `waiting-for-signin` (polling for the sync root every 15 s,
unbounded — this is the manual step), `waiting-for-secret` (only when account
creation or an explicit credential reset needs the atomically delivered
run-scoped secret), `creating-share`, `installing-bridge-boundary`,
`installing-agent`, `verifying`, `done`. A skipped component never gets a fake
busy phase: its check remains `ok` and its work ID is absent. On failure,
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
- The watcher executes only protected local copies of the six allowlisted
  files. Nothing under `Data` or `C:\OEM` is an execution source.
- `checks` and `work` are explanatory output from an untrusted status channel,
  never capabilities or instructions. The elevated orchestrator derives and
  revalidates its in-memory work plan; it accepts no host-supplied phase list,
  path, command, script name, account name, or share name.
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
- Re-triggering is always safe: reconciliation re-probes instead of trusting
  the prior phase; 03/04 repair scopes are idempotent; `installing-icloud`
  skips when `Get-AppxPackage AppleInc.iCloud` already answers; and the watcher
  consumes triggers one at a time. A new run is not staged over an acknowledged
  active run; after a reboot, error, or explicit retry it gets a new UUID and
  its own protected run directory.
- `detail` and `error` are single-line strings capped at 500 characters after
  control-character removal. Status reads are capped at 64 KiB. The host keeps
  the matching terminal status until the D43 record has been cleared; cleanup
  removes executable inbox content and any secret, not the only evidence
  needed to resume safely after a GUI crash.

## Desired-state inspection and reconciliation

Inspection is implemented once in `guest-setup.ps1` and reused before and
after repair. It is read-only: even creating a missing directory counts as
repair and happens later. The checklist is deliberately fixed so the GUI can
render stable local labels and tests can exhaust its state matrix:

| Check | `ok` means | Repair owner |
|---|---|---|
| `icloudPackage` | The exact `AppleInc.iCloud` AppX package is registered for the `icloud` user. | Install through winget; never remove an unexpected package. |
| `syncRoot` | The exact `C:\Users\icloud\iCloudDrive` path is an accessible directory. | Launch iCloud and wait for the operator's sign-in/toggle; a wrong-type or inaccessible object is `blocked`, not deleted. |
| `shareAccount` | Local `syncshare` exists, is enabled, does not expire, has the required password/account flags, and has the hidden-logon registry value. | Script 03 account scope; account creation requires the secret, while non-secret property drift does not. |
| `shareCredential` | Never inferred from Windows account metadata. | Always `unverifiable`; first-run, a missing account, or explicit **Reset share password** schedules a reset. Otherwise preserve it. A later authenticated host connection is separate corroboration, not a recovered password. |
| `dataShare` | `icloud` points at the exact sync root with the expected `syncshare` share access; LanmanServer state/startup, firewall rules, signing/encryption settings, and the root `syncshare` ACE match the plan. | Script 03 share scope. A wrong share path is safely recreated after inspection without deleting its target or contents. |
| `bridgeBoundary` | The exclusions safety preflight passes; bridge paths/share, ABE, D27/D28 ACL boundaries, and the full read-only traversal-link/protected-DACL/legacy-explicit-allow scan pass. | Script 04 boundary scope. This potentially long metadata scan writes heartbeats and runs only during provisioning, never in ordinary background status polling. |
| `agentInstall` | Installed `agent.ps1` hashes to the staged source and the task's action, principal, run level, trigger, restart policy, and protected paths match exactly. | Script 04 agent scope; it does not walk or normalize the iCloud tree. |
| `agentRuntime` | The exact task is running and a fresh bridge status reports the one supported protocol and staged `agentBuild`. | Start the already-correct task or run script 04 agent scope, then wait up to the existing 90 s verification window. |

Rules for deriving work:

- Compute the complete checklist before the first mutation. If any check is
  `blocked` or `unknown`, publish the whole checklist and stop before changing
  configuration. The only exceptions are expected absence (`missing`) and
  enumerated drift with a named repair owner. `pending` is allowed only when an
  unmet earlier dependency makes a downstream probe meaningless (for example,
  bridge-boundary checks before the sync root exists); it is not healthy, is
  re-probed after the dependency converges, and may not remain at `done`.
- Work is dependency ordered, not blindly script ordered. Package/sign-in work
  precedes share work; share-account/data-share work precedes the bridge
  boundary; the agent is last. Re-inspect downstream dependencies after a wait
  such as sign-in because the VM may have changed while the app was waiting.
- `ok` means verified by current effect, not by a marker alone. Markers and
  hashes may establish bundle identity, but share paths, task definitions,
  service state, ACL boundaries, and runtime freshness are probed directly.
- An agent-only mismatch produces only `update-agent`: it neither requests the
  secret, resets `syncshare`, reruns data-share setup, nor performs the full
  boundary repair. The inspection's boundary scan remains read-only. A
  non-credential data-share drift with an existing account uses script 03's
  preserve-credential mode.
- Each component gets at most one repair pass in a run. The `verifying` pass
  re-evaluates every check. Residual drift becomes a terminal, specifically
  classified error with the protected manual fallback; it does not enter an
  automatic repair loop.
- The `shareCredential` check may remain `unverifiable` at `done`; the GUI must
  say whether it was **reset this run** or **preserved**, never show a green
  claim that Windows revealed or validated it. Setup still proceeds to the
  existing authenticated **Check setup and connect** step, which is the
  end-to-end proof.
- Guest-writable status can make the GUI display a false warning, so the host
  validates it defensively. It cannot cause elevated execution: the protected
  orchestrator owns the probes, dependency graph, fixed paths, and repair
  dispatch.

Explicit strange-state policy:

- Missing `exclusions.json` alongside any existing bridge marker, an
  unexpected file at the sync-root path, a traversal link that could redirect
  an elevated walk, protected child DACLs, an unparseable task/share/account
  object, or failure to enumerate a security boundary is `blocked`. Preserve
  data and show the exact diagnosis; do not reinterpret it as a fresh install.
- A missing package, account, share, task, or agent file is ordinary
  `missing`. A known object at the wrong fixed path or with wrong fixed
  properties is `drifted` only where the table names a non-destructive repair.
- Stale, malformed, mismatched-run, or unknown-version status never alters
  this classification. It is a host/watcher communication condition handled by
  D43, not evidence that a guest component is absent.

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
  copy the six fixed payload filenames to a new protected
  `runs\<runId>` directory, verify every copy exists, atomically record the run
  ID locally, refresh the protected installed watcher for the next task start,
  and execute the protected `guest-setup.ps1` with the run ID and strict
  reset-credential boolean. It accepts no host-supplied work list. The remote
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
   directory, plus the strict `resetShareCredential` boolean passed by the
   watcher. Write `status.json` (`staging`, echoing the run ID passed by the
   watcher and a complete `pending` checklist). Reuse the atomic-write pattern
   from 04, plus the protocol's bounds and control-character stripping.
2. `inspecting`: run the fixed read-only checklist above, derive the complete
   dependency-ordered work list, and publish both. Stop before mutation if any
   component is `blocked`/`unknown`. The implementation uses typed probe
   results and fixed repair dispatch, not `Invoke-Expression`, status text, or
   strings supplied by the host.
3. After a non-blocked inspection, transactionally refresh the protected
   `current` directory used by the
   manual fallback: build and verify a sibling temporary directory, swap it
   with rollback so failure retains the prior complete bundle, then prune the
   old copy. Optionally refresh convenience
   copies of
   `03-create-share.ps1`, `04-bridge-agent.ps1`, `agent.ps1`,
   `guest-state.ps1`, `watcher.ps1` and `guest-setup.ps1` under `C:\OEM` for
   inspection, but execute only the
   siblings under the protected run directory. `current` is the protected
   manual fallback for a diagnosed provisioning failure — something a failure
   report may tell the operator to elevate by hand — not something the GUI
   detects or references: D35 recovery routes through the app's re-provision
   action, and no active recovery text names `C:\OEM` or any unprotected
   copy.
4. `installing-icloud`, only for `install-icloud`: `winget install --id
   9PKTQ5699M62 --source msstore
   --accept-package-agreements --accept-source-agreements`, retried up to 5
   times 120 s apart (Store readiness is flaky at first boot — plan section 5).
   Check the native exit code explicitly; a non-zero `$LASTEXITCODE` is not a
   PowerShell exception. Bound each attempt to 10 min. On exhaustion, error
   status naming the manual Store fallback from 02's header.
5. `launching-icloud`/`waiting-for-signin`, only for `wait-for-signin`: if the
   sync root is missing, launch the client non-elevated from this elevated
   context with
   `explorer.exe shell:AppsFolder\AppleInc.iCloud_nzyj5cx40ttqa!iCloud`
   (documented working method, automation-notes section 3), then poll the exact
   directory every 15 s forever, writing a heartbeat each pass. Sign-in plus
   the iCloud Drive toggle is the operator's only guest interaction. Re-run the
   downstream inspection after the directory appears.
6. Enter `waiting-for-secret` only when the plan includes
   `create-share-account` or `reset-share-credential`; otherwise do not request,
   transmit, or touch the password. Poll
   `\\host.lan\Provision\secret` every 5 s, writing a heartbeat each pass. It is
   not an error for the file to be absent after a GUI exit/restart. After it
   appears, copy it without text decoding to a protected local temporary file,
   then advance to `creating-share` (the host's acknowledgement to remove the
   remote copy).
7. For share-account/data-share work, run the protected sibling
   `03-create-share.ps1` in one of two explicit modes: `-PasswordFile
   <protected-local-path>` creates/resets the account and reconciles the share;
   `-PreserveCredential` requires an existing account and reconciles only its
   non-secret properties plus the data share. The switches are mutually
   exclusive. Script 03 immediately deletes a supplied local secret. If host
   cleanup wins before the copy, the guest stays in `waiting-for-secret`; if
   the phase advances, the full atomically-renamed value is already protected
   locally. Wrap all remaining work in an outer `finally` that deletes this
   local secret even when 03 cannot be launched.
8. For bridge work, run the protected sibling `04-bridge-agent.ps1 -Scope
   Boundary` for `repair-bridge-boundary` and `-Scope Agent` for
   `update-agent`; if both are needed, `-Scope All` performs their shared
   preflight once. `Agent` must not traverse or normalize the iCloud data tree.
   Each scope verifies its own result, and the orchestrator still performs the
   complete postflight.
9. Run winget and both child PowerShell scripts through one helper that starts
   the child with stdout/stderr redirected to protected per-run temporary
   files, rewrites heartbeat status while it is active, checks the exit code,
   reads only a bounded sanitized tail on failure, and deletes the temporary
   output. Do not use `&` and assume a native non-zero exit becomes a
   terminating PowerShell error.
10. `verifying`: repeat the complete read-only inspection. Report `done` only
    under D44's convergence rule; otherwise emit the exact residual check as a
    terminal error without another automatic repair pass.

Changed `provision/03-create-share.ps1`: add a mutually exclusive parameter
set for `-PasswordFile <path>` and `-PreserveCredential`, plus
`$ErrorActionPreference = 'Stop'`. When provided, read the whole BOM-less UTF-8
file without adding/removing a newline, reject NUL/CR/LF or an empty value,
build the SecureString, and delete the file in `finally` immediately after the
read. `-PreserveCredential` refuses a missing account and never constructs or
sets a password. With neither automation switch, keep the existing embedded
`STRONG_PASSWORD_HERE` manual-fallback path (the hygiene check requires the
literal placeholder to survive). Factor the script's account and share repairs
so it changes only drifted properties within the selected mode; in particular,
an already-correct share is not recreated. Add an elevation/sync-root
preflight, check every native `icacls` exit code, and verify the resulting local
account, share path/access, service state and SMB settings before returning
zero. These checks are required: the current script contains non-terminating
cmdlets/native calls, so merely launching it from an orchestrator cannot prove
`creating-share` succeeded.

New `provision/guest-state.ps1`: a dot-sourced, side-effect-free library owning
the fixed guest constants, check-state/work enums, typed probe helpers, and
dependency/work-plan derivation used by `guest-setup.ps1`, 03, and 04. It
performs no repair and emits no status itself. Keeping one definition prevents
the orchestrator from deciding a component is healthy under weaker rules than
the script that repairs and verifies it.

Changed `provision/04-bridge-agent.ps1`: add the fixed `Agent`, `Boundary`, and
`All` scopes described above and consume `guest-state.ps1` rather than
duplicating its invariant definitions. `Agent`
updates/verifies only the protected agent file, its ACL/task, and runtime;
`Boundary` owns the sync-root/bridge ACL scan, bridge share, ABE, and exclusions
safety; `All` preserves today's full manual fallback. Also resolve the source
`agent.ps1` beside the invoked script (`Join-Path $PSScriptRoot 'agent.ps1'`)
rather than hard-code `C:\OEM\agent.ps1`. This behaves identically for the
manual fallback and lets automation execute a protected, coherent payload.

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
- `stage(bundle, run_id, reset_share_credential, runner, input_runner)`:
  validate the supplied ID, require a real boolean (integers are rejected), and
  refuse to replace an acknowledged nonterminal run; empty and recreate the
  fixed container inbox, `docker cp` the six allowlisted files from
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
  `ensure_channel()` + `stage(..., run_id,
  reset_share_credential=True, ...)` in a worker, and start polling `poll()`
  every 3 s, with at most one Docker poll worker in flight. There must be no
  trigger-without-record crash window. First-run deliberately establishes the
  selected credential even if a partly configured VM already has an account.
  Render the fixed checklist plus planned/completed work, and render phase and
  detail as the busy line (D38 conventions: elapsed time, no percentages, no
  cancel of the guest run). Use locally owned labels/icons: `ok`, work needed,
  waiting for operator, blocked, and password reset/preserved-but-unverifiable
  must be visually distinct. If no acknowledgement within
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
  `done` leads into the existing **Check setup and connect** success flow.
  If that authenticated connection reports a credential-specific failure while
  the guest checklist otherwise converged, explain that Windows cannot read
  back the account password and offer **Retry and reset share password…**,
  preselecting the reset option but still requiring the operator's env-file
  choice and confirmation. Do not automatically reset on a generic timeout,
  DNS, mount, or Windows-not-ready failure.
  The manual instructions the state shows today remain reachable (collapsed
  or behind "Show manual steps").
- Monitoring state: add a confirmed menu/Status-tab action **Re-run Windows
  provisioning…**, enabled whenever the container classification is running
  and no provisioning or power transaction is in flight — deliberately
  including while the bridge protocol is `skewed` or `incompatible`. The D35
  gate keeps ordinary bridge writes (Apply, Restore, list requests) closed in
  the incompatible state; this explicit recovery action is exempt because it
  is what the gate's banner points at. It quiesces bridge I/O and pauses
  GUI polling for the duration (existing `quiesce`/`set_io_paused` machinery),
  writes a `reprovision` record, and runs the same stage/poll flow. The
  confirmation must accurately say that the systemd mounts remain mounted and
  the app cannot police another process using them; the operator must close
  files and shells under `/mnt/icloud*` before continuing. Do not call this
  full host-I/O quiescence. Its confirmation includes an unchecked **Reset
  share password from an env file** option. The normal D35/agent-repair path
  stages `resetShareCredential=false`; it requests no secret when account/share
  checks are healthy. Selecting the option stores only the boolean in D43 and
  asks for the env file later at `waiting-for-secret`. On success, verify a
  compatible status and the bundled agent build through a fresh bridge gather,
  then clear the record, invalidate cached documents and refresh. This is the
  one-click path for the post-feature-update recovery step and for the
  currently stale VM.
- `provisioning.json` (D39/D43): bump the schema and add `mode`,
  `containerStartedAt`, `guestRunId`, `guestPhase`, and the non-secret
  `resetShareCredential` boolean; preserve its existing private atomic-write
  rules. The run ID is required to reattach safely after a GUI restart, while
  `containerStartedAt` distinguishes a container restart from a merely quiet
  watcher. A `reprovision` record is subject to the same startup-before-CIFS
  gate as a first-run record; otherwise a restarted app could mount while
  03/04 are changing guest state.
- D35 recovery flow: the skew/incompatible banner becomes an unconditional
  entry into the same re-provision flow, via a banner button that invokes the
  **same** controller action as the Status-tab/menu command — one
  implementation, one set of enablement rules. Replace
  `UPDATE_AGENT_INSTRUCTION` and `SKEW_BANNER` in
  `gui/icloud_bridge_gui/bridge.py` (and the "never an automated guest-side
  update" comment above them, which D35's amended rationale supersedes) with
  wording along the lines of: "The guest agent does not match this app.
  Choose **Re-run Windows provisioning…** to install the bundled agent. VMs
  created before automated provisioning may require the one-time elevated
  bootstrap step." The flow probes no guest state and works for both VM
  generations: a post-feature VM's watcher acknowledges and proceeds; a
  pre-feature VM reaches the no-acknowledgement hint, and once the operator
  runs the bootstrap one-liner the watcher consumes the already-staged
  trigger — the operator does not click again. No active recovery UI may
  contain a `C:\OEM` instruction. With a healthy share/boundary and only an
  agent build mismatch, the visible plan is just **Update bridge agent** and
  the run never enters `waiting-for-secret`.
- Failure rendering: `error` statuses show the failing phase, the bounded
  message, the complete last trustworthy checklist, and the protected manual
  fallback for that component. Offer **Try inspection and repair again** after
  the operator fixes a blocked condition; that creates a new D43 run and
  re-probes everything rather than resuming after the failed instruction.
  Preserve **Show manual steps** and diagnostics access so an unusual VM never
  becomes a dead-end app state. A mismatched run ID never replaces the saved
  run or clears the record.

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
  ("Yes — done" pointing at D40-D44) and strike the now-implemented items
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
- **Agent-only skew**: inspection schedules only `update-agent`; no secret is
  requested, script 03 is not launched, and script 04's agent scope does not
  traverse the iCloud tree. Postflight still verifies the complete checklist.
- **Partly configured share**: if the account exists, deterministic
  account/share/service drift is repaired while preserving the credential. If
  the account is absent, the work plan explicitly requests the secret and
  creates it. An operator-selected password reset uses the same latter path.
- **Ambiguous or unsafe guest state**: publish `blocked` with the exact fixed
  checklist item, preserve all data, keep normal D35 writes closed when
  applicable, and expose manual steps plus a fresh retry. "Handled" means the
  app diagnoses, contains, and gives a convergent route; it does not delete or
  overwrite operator data merely to make the checklist green.
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
   side-effect-free `provision/guest-state.ps1`,
   `provision/guest-setup.ps1`, the 03 credential-preserving/password-file
   parameter sets, 04 `Agent`/`Boundary`/`All` scopes and `$PSScriptRoot`
   source, protected watcher install, heartbeat child runner, and `install.bat`
   registration + NEXT-STEPS rewrite. Keep probe normalization and work-plan
   derivation pure; add a dependency-free PowerShell fixture test under
   `packaging/` that exhausts the fixed check-state/work-plan matrix and proves
   an agent-only plan dispatches neither 03 nor the boundary scope. Wire it
   into `make lint-ps`; it runs under local PowerShell 7, while M5 remains the
   PowerShell 5.1/Windows proof. Verify: `make check` and `make lint-ps`
   (state explicitly that Windows cmdlet and 5.1 behaviour remain unproven
   locally). Commit the named guest scripts, test harness, Makefile target, and
   packaging lint driver together.
3. **M2 — `guestprov.py`, env parsing, and tests.** Add
   `gui/tests/test_guestprov.py`; update `firstrun` tests and
   `host/icloud-bridge-configure` together so password syntax stays identical.
   Fake-runner tests cover safe Samba candidate/rollback/verification, staging
   order (trigger last), UUID validation, refusal to overwrite an active run,
   deferred atomic secret-over-stdin (assert the value is absent from every
   argv/result/log), run-ID matching, phase classification, heartbeat/deadline
   behavior, malformed status, strict checklist/work validation, the
   reset-credential boolean, and the X.224 probe against a fake socket. Verify:
   `make check`. Commit the module, parser/configurer and tests together.
4. **M3 — GUI wiring.** Provisioning-state button and status rendering,
   re-run action with accurately scoped GUI-I/O quiesce, D43 record/resumption,
   reducer events, tray/window text, fixed reconciliation checklist rendering,
   credential-reset/preserved wording, blocked-state retry/manual-step escape,
   and no false green state for an unverifiable credential. D35 recovery rewiring:
   `UPDATE_AGENT_INSTRUCTION`/`SKEW_BANNER` and their comment in `bridge.py`,
   the banner button invoking the canonical re-provision controller action,
   and enablement during `skewed`/`incompatible`. Update
   `gui/tests/test_bridge.py`
   (`test_the_recovery_instruction_names_script_04` becomes a test of the new
   wording) and `gui/tests/test_qt_wiring.py`
   (`test_a_skewed_agent_still_dispatches_and_shows_the_banner`), and add
   wiring tests that (a) no active recovery UI contains a `C:\OEM`
   instruction and (b) an incompatible bridge keeps ordinary writes disabled
   while the explicit re-provision action stays available. Add the agent-only
   scenario asserting no env chooser appears, plus missing-account,
   preserve-credential share repair, explicit password reset, postflight
   residual drift, malformed checklist, blocked/retry, and
   credential-specific-connect-failure routing scenarios. Extend lifecycle,
   first-run and Qt wiring tests with faked guestprov, including restart
   during first-run and reprovision plus env re-selection at
   `waiting-for-secret`. Verify: `make check`, ideally `make test-all`.
   Commit.
5. **M4 — Docs.** SETUP.md, automation-notes scoreboard, README. Every
   current troubleshooting instruction for agent skew or an incompatible
   protocol must point at the re-provision workflow rather than `C:\OEM`;
   historical entries in `CHANGELOG.md` and `docs/acceptance-results.md` stay
   as written, and a new changelog entry describes the change. Verify:
   `make check`. Commit.
6. **M5 — Operator verification (not possible in this workspace).** On the
   real host, in this order: (a) stage once, confirm `testparm -s` shows
   `Provision` read-only, and prove that creating/replacing a file through
   `\\host.lan\Provision` fails while status writes through `Data` work;
   (b) on the pre-feature VM, start **Re-run Windows provisioning…** first
   (from the D35 banner button if one is showing, else the Status tab), let
   it reach the no-acknowledgement bootstrap hint, then paste the bootstrap
   one-liner into the VM's elevated PowerShell and confirm the watcher
   consumes the already-staged trigger with no further host-side click;
   (c) watch the phases and checklist;
   interrupt the GUI once during `waiting-for-signin` and once immediately
   after secret delivery to prove D43/re-delivery; (d) confirm shares `icloud`
   and `bridge` exist, `status.json` shows
   `agentBuild` matching the bundle, no D35 banner; (e)
   `./host/acceptance-tests.sh` passes; (f) create a controlled agent-build/task
   drift and prove the proposed work contains only `update-agent`, no env
   chooser appears, the boundary **repair scope** does not run, and postflight
   returns every other check unchanged; (g) create one safely repairable share
   drift and prove it is repaired without a password reset, then exercise the
   explicit reset option; (h) create one blocked fixture without risking user
   data, prove no mutation occurs, correct it, and use the app's retry to
   converge; (i) later, prove the OEM path with a
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
