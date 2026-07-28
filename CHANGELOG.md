# Improvement changelog and investigation log

This is the durable history of improvements to the iCloud bridge: what changed,
why it changed, what should be investigated next, and what was considered but
deliberately not built. It is intentionally broader than a release changelog so
that a later review does not repeat an already-settled experiment.

The authoritative behavior remains
[`docs/implementation-plan.md`](docs/implementation-plan.md) plus
[`docs/plan-gui-selective-sync.md`](docs/plan-gui-selective-sync.md), which wins
where the two conflict. An entry here does not amend a locked decision. A
candidate that changes one must first update the applicable plan and decision
register.

The repository has no release tags yet. The entries below therefore use dates and
commit IDs rather than inventing release boundaries after the fact. The GUI
reported `2.0.0` for most of its short history — a number that read like a
second stable major release of something people were running, which was never
true of anything here. It is now **`0.2.0`**: pre-1.0, with the minor digit
tracking the design line the code implements (`0.2.x` is the v2 plan). The
entries below are unchanged by that renumbering; where an older entry named a
`2.x` number, it means the `0.x` one with the same minor digit.

Because nothing has shipped, **no entry here is a compatibility promise**. Until
the first tag, a change is free to break any format, protocol or interface
outright (the pre-release policy in `CONTRIBUTING.md`); what an entry records is
what changed and why, plus the operator step — usually re-running
`04-bridge-agent.ps1` — that a break requires. Preserving the operator's own
state (exclusions, credentials, the VM disk, the synced files) is a separate and
non-negotiable obligation.

## How to maintain this document

- Add user-visible reliability, safety, performance, setup, packaging, or GUI
  changes under **Shipped improvements** in the same commit as the change.
- Put reviewed follow-up work under **Further improvements**, with evidence and
  a testable completion gate. `Candidate` means investigated, not approved.
- Move an idea to **Deferred** when a specific measurement or live experiment
  is required. Move it to **Closed** when the cost, safety trade-off, or conflict
  with a locked decision is understood.
- Record the date, relevant decision/plan section, and live evidence when a
  status changes. Do not erase a rejected idea; supersede it with the new
  evidence so the reasoning remains visible.
- Keep implementation details in the plans and operator instructions in
  `SETUP.md`. This file records the change and the reason, then links to those
  sources rather than becoming a third specification.

## Further improvements

These are ordered by current value. They are not commitments and must preserve
the security, lifecycle, Files On-Demand, and no-secret contracts in
`CONTRIBUTING.md`.

### I-012 — The inspection scan owes the host a heartbeat

**Status:** Done 2026-07-28. **Evidence:** D44's table states that the bridge-boundary
scan "writes heartbeats", and §4.1 makes 120 s of frozen status mtime during an
active phase a "stalled" warning. The implementation cannot do either:
`Get-GuestChecklist` lives in `guest-state.ps1`, which is deliberately
side-effect-free, so nothing refreshes the status while it walks the library.
Measured on the author's guest during the first app-driven run: `inspecting`
held for **82 s** (09:39:12 → 09:40:34) and `verifying` for **81 s** (09:40:46 →
09:42:07), both silent, on a 60 000-entry library. That is inside the 120 s
threshold with about a third to spare, so nothing warned this time — but the
scan is proportional to library size, and the first symptom on a larger one is
the app calling a healthy run stalled.

The orchestrator now injects a throttled callback into `Get-GuestChecklist`;
both proportional scans invoke it while walking, without adding ambient I/O to
`guest-state.ps1` or its pure reasoning functions. The Linux fixture proves
both callback paths. A fresh run with the committed payload then remained in
`inspecting` from 14:47:14 to 14:56:19 UTC on the author's 60 000-entry live
guest, rewriting valid status about every 25 seconds throughout. The run
therefore exceeded the host's 120-second stall threshold by more than four
times without ever presenting a frozen heartbeat.

### I-001 — Close the live-hardware acceptance matrix

**Status:** Candidate, highest priority  
**Evidence:** The checkout can prove syntax, pure state models, package contents,
and command construction, but it has no KVM guest, systemd instance, CIFS mount,
tray, or Apple device. The v2 plan's Phase 0 and Phase E checks are consequently
still the largest source of uncertainty — and the reviewed follow-up work below
added four more of them (E12–E15).

Run and record the complete matrix on the real host before adding more
data-path behavior: cold hydration and write round trips; exclusion ACL/ABE and
case-collision safety; disk reclamation including partially hydrated files;
clean, busy, failed, and reboot lifecycle paths; manual-container-stop recovery;
notification/tray interaction; and both first-run install routes. Capture the
Windows, iCloud, Docker, dockur image, kernel, and `cifs.ko` versions plus useful
timings so later regressions have a baseline.

Results are recorded in
[`docs/acceptance-results.md`](docs/acceptance-results.md), which also carries the
environment baseline those results are read against. Rows are filled in only from
the real host.

**Start by provisioning the guest, and do not record a performance number until
it is provisioned.** I-008 discovered that `03-create-share.ps1` and
`04-bridge-agent.ps1` have never run on the author's guest, and the 2026-07-27
review confirmed it: no agent build 3, no real scan duration, no exclusion costs,
no representative tree, and the only share that exists is the historical D5
`icloudtest` one. Run the `SETUP.md` §9 sequence, then treat all four of these as
the precondition for every later measurement — `status.json` advancing every 15 s
with `agentBuild: 3`; `scan.lastCompletedAt` non-null with a plausible
`scan.entries` and no `tree`, `scan` or ACL failure in `lastError`; a *second*
ten-minute scan completing, so the first was not startup luck; and the mounted
data share being the production `icloud` share rather than `icloudtest`.
Optimizing against an unprovisioned guest is how this project would waste the
most effort.

**Completion gate:** E0 and every applicable Phase E case in
[`docs/plan-gui-selective-sync.md`](docs/plan-gui-selective-sync.md#phase-e--v2-live-acceptance-tests-require-the-real-vm)
has a dated result in
[`docs/acceptance-results.md`](docs/acceptance-results.md), with failures turned
into fixes or explicit accepted limitations.

### I-008 — Apply D33 to the running guest, and re-confirm D32

**Status:** Done 2026-07-27 (see the shipped entry of that date)  
**Evidence:** Measured on the author's live host on 2026-07-26; remediated and
re-measured there on 2026-07-27. The container had been running since
2026-07-25, and commit `26d29ac` — `/dev/vhost-net` in `docker-compose.yml` for
D33 and the signing assertions in `03-create-share.ps1` for D32 — could not
reach a container or guest that already existed, and **neither drift was
detectable by any check in the repository** until `80947be` taught
`acceptance-tests.sh` to ask the container and QEMU instead of the host.

How it ended, with the full record in
[`docs/acceptance-results.md`](docs/acceptance-results.md):

- **D33 applied.** The container was gracefully recreated and `make acceptance`
  now reports the device and `vhost=on` checks green. The before/after read
  benchmark showed **no measurable throughput change** at warm-20 MB-read scale
  through userland `smbclient`; the honest measurement of the data path remains
  E0's kernel-cifs read, which requires provisioning this guest first.
- **D32 resolved: signing is not required.** A real SMB client refusing to sign
  was accepted before the recreate and after a cold boot. The four raw-packet
  "signing required" readings of 2026-07-26 were the probe's artifact. The
  in-guest `Get-SmbServerConfiguration` check folds into the next run of
  `03-create-share.ps1` — which, it turned out, has **never been run on this
  guest** (only the historical D5 test share exists), so the remaining work is
  I-001's provisioning runbook, not remediation.

### I-009 — Reduce the guest agent's per-entry walk cost

**Status:** Main item shipped 2026-07-26 (see the shipped entry); the guest-side
proofs the gate demanded were recorded 2026-07-28 (see "F3 executed on the
guest" under Shipped improvements) — what remains open here is only the two
excluded-subtree candidates below, which are P6 of the performance review and
still need a D34 amendment plus an exclusion-cost numerator  
**Evidence:** Follows the same review that produced the serializer work already
shipped below, and is the remainder of it. The per-entry `Join-Path` calls and
the per-directory scriptblock sorts are gone — measured 61.5 us -> 0.84 us of
overhead per entry under PowerShell 7, and the rewrite also fixed a real crash;
both are described in the shipped entry. What a Linux checkout cannot prove
remains open: `Join-Path`-equivalence of the concatenated paths on the guest
(argued from the startup `TrimEnd('\')` invariant, not demonstrated), Windows
PowerShell 5.1 timings, and the actual pass-duration change on the operator's
library.

Two further items from the same review, both **worth nothing on a guest with no
exclusions configured** and both requiring a locked row to be amended first, so
they stayed out of the shipped change:

- Skip the per-entry DACL read inside a validated excluded root during
  `Invoke-FullScan` reconciliation, where the target-deny removal condition is
  provably false. Extends D34(a) with a second condition; must be gated on
  `$ConfigValid` exactly as the existing fast path is, must not advance
  `$aclState.last`, and must be evaluated *before* the resume-cursor comparison
  or it trades a DACL read for a string split. The consequence to write into the
  D34 row is that a stale non-inheritable guard inside the subtree heals on
  **re-inclusion**, not spontaneously.
- Deduplicate the two walks of excluded subtrees (`Measure-SubtreeCheap` from
  enforcement, and `Build-Node`, which recurses into them and only drops them
  from the output). **Correct the staleness claim before writing this up:** the
  enforcement block runs before the tree block on the same tick, so consuming the
  tree's measurement pushes an `applied` label's worst-case staleness to ~20 min,
  double what D34(b) sanctions in writing. Either amend that number or reorder
  the loop.

**Not to be confused with** DFR-001 (the `attrib` -> `SetFileAttributesW`
substitution, which hard rule 6 and D14/D26 pin) or R-023 (caching the
`exclusions.json` containment validation, which is a live safety check). Neither
is reopened here.

**Completion gate:** emission order stays exactly OrdinalIgnoreCase-then-Ordinal
— `Compare-RelPathDfs` must match the walk's comparator or the ACL resume cursor
can skip never-visited subtrees — now proven by `tools/test-agent-walk.ps1`
under `make test-ps`. Still open: path-concatenation equivalence to `Join-Path`
is confirmed **on the guest**, because a Linux host cannot prove it, and the
next agent restart there shows a `tree` pass completing on the real library.

### I-010 — Attribute the guest's idle CPU burn

**Status:** Candidate; needs the operator present  
**Evidence:** `tools/vcpu-profile.py` (shipped below) measured the idle guest at
18.3% of one core, stable across 180 s and 600 s windows, split 68.5% guest mode
/ 28.6% host kernel / 2.9% QEMU userspace. That **bounds every host-side tuning
knob at about 5% of one core** and puts the rest inside Windows. Separately, the
guest writes ~66 KiB/s in bursts — roughly 5-8 GB/day of host SSD writes while
doing no user work — and no acceptance criterion covers that in any form.

Nothing in this repository can name the Windows process responsible. The channel
exists, though, and is already checked in: `tools/guest-ctl.sh` +
`tools/qemu-monitor.py` drive the guest through QEMU's monitor socket, and
[`docs/automation-notes.md`](docs/automation-notes.md) documents delivering a
script through the container's `\\host.lan\Data` share. The safe shape is
therefore: `docker cp` a read-only sampler into that share, type one short
command to launch it, and have it write a two-sample CPU **delta** back as text
— not blind keystrokes, and not lifetime CPU.

**The sampler now exists.** `tools/profile-windows-idle.ps1` shipped on
2026-07-27 (entry below) and implements exactly that shape. What is still
missing is a *run*: three of it after the desktop has settled, against three
matching `tools/vcpu-profile.py --seconds 300` host samples. The 2026-07-27
review also narrowed the target — 73.7% of the sampled QEMU CPU was guest mode,
so 4.7% of one core is all that host-side tuning can reach.

**Expect a documented cost, not a saving.** R-012 and hard rule 5 close the
Store/AppX/WebView2/servicing stack; R-022 closes Defender real-time protection,
WNS and memory compression; R-020 keeps ScheduledDefrag; R-021 keeps the input
services; R-019 keeps last-access. That is most of what such a sample will name.
Anyone returning from this proposing "disable Defender" has re-run R-022.

**Completion gate:** the idle burn is attributed to named processes with a dated
figure in [`docs/acceptance-results.md`](docs/acceptance-results.md), and each
one is either fixed, or recorded as an accepted recurring cost with its share.
It also needs the operator watching: it types into a live, Apple-signed-in
desktop, and every command must be read-only.

### I-011 — Replace section 11.3's guest-idle acceptance criterion

**Status:** Shipped 2026-07-26 (see the shipped entry)  
**Evidence:** The criterion read "guest idles < 5% host CPU (check
`docker stats`)". Followed exactly as its own parenthetical instructed, `docker
stats` reports percent-of-one-core on Linux, so the author's guest read ~18% and
the criterion **failed** — it could not pass either way. Nobody noticed because
it sits in the un-run MANUAL block and every row of `docs/acceptance-results.md`
still says `not yet run`. It is now absolute — core-seconds of container CPU
per wall second plus a block-write ceiling, both reported by
`tools/vcpu-profile.py` — with idle-cost rows in the environment baseline table,
and plan section 8.1's `halt_poll_ns` bullet no longer recommends `docker stats`
for a benchmark that tool cannot resolve. Filling the baseline rows in still
needs the real host at true idle (I-001).

### I-007 — Establish release boundaries

**Status:** Ready; blocked on live acceptance  
**Evidence:** The reviewed backlog in
[`todo/archive/further_improvements.md`](todo/archive/further_improvements.md)
has landed, so
there is a coherent body of work to name. What is missing is the evidence, not
the decision — every row in
[`docs/acceptance-results.md`](docs/acceptance-results.md) is still
`not yet run`, and a development checkout is structurally unable to change that.

The numbering itself is now settled: `__version__` reads **`0.2.0`**, and the
release that names the shipped backlog is **`0.3.0`**. Pre-1.0 is the accurate
statement of where this stands — no tags, no installed copies but the author's,
and hard rule 9 explicitly disclaiming compatibility. `1.0.0` is reserved for the
first build that has passed live acceptance on hardware other than the author's.
Bumping the minor digit is a one-line change; `Makefile` and
`packaging/build-deb.sh` already derive from that single source.

**Completion gate:** the applicable E0–E15 rows are recorded on the real host as
`pass` or an explicitly approved accepted limitation; then bump `__version__` to
`0.3.0`, add the release entry here mapping it to those rows, and confirm
`make version`, `icloud-bridge-gui --version`, the built package filename and
`dpkg-deb -I` all agree. Tagging `v0.3.0` remains the operator's call and
happens after that, never before.

## Shipped improvements

### 2026-07-28 — F3 executed on the guest: the PowerShell 5.1 proofs are recorded

The I-009 completion gate's guest half ran on the live guest under Windows
PowerShell 5.1.26100.7920 (Desktop edition), driven from the host over the
qemu-monitor keystroke channel with results written to the Data share.
`tools/test-agent-walk.ps1` passes in full under 5.1 (exit 0): SortByName
reproduces the retired scriptblock comparator's captured order, tolerates
null/scalar/`Object[]` inputs, and the DFS-preorder emission agrees with
`Compare-RelPathDfs` including the ancestors-before-descendants and
flat-compare-disagrees guards. The `Join-Path`-equivalence question is settled
with real-library data: over 2,000 entries of the operator's library (55 names
with spaces, 1,572 with non-ASCII characters, longest full path 175 chars),
concatenation matched `Join-Path` on **all 2,000** plain paths — and diverged
on **all 2,000** `\\?\`-prefixed forms, which is the stronger finding: the
long-path form the ACL reconciliation depends on must never go through
`Join-Path`, and the agent's native helper indeed builds it by concatenation
(`agent.ps1` `ToExtended`). Zero-entry, one-entry and ordinary directories
enumerate correctly (0/1/54). The formal pass durations stand recorded: first
real-library pass 29 s at 60,154 entries (2026-07-27, build 6); subsequent
full passes 30.162 s and 52.228 s at 69,620 entries (2026-07-28, build 9),
`lastError: none` throughout. F3's section in the performance todo note moves
to `todo/archive/`.

### 2026-07-28 — The data mount stops vetoing writes the guest allows

Creating a file or a directory at the **root** of `/mnt/icloud` failed with
`EACCES` that never reached the guest. The iCloud sync root carries the DOS
read-only attribute — normal for a Windows folder with a custom icon, and
re-applied by the shell if cleared — and the host's cifs client maps that
attribute onto mode `0555`, then refuses the create itself; subdirectories keep
`dir_mode`'s `0775` and worked throughout. The server was always willing: the
same create and delete at the share root succeed over `smbclient` with the same
`syncshare` credential (live host, 2026-07-28). So the operator could not make a
top-level item at all, and the section-5 acceptance canary — which writes exactly
there — reported a bare `FAIL` for a client-side artifact.

New decision **D50** (v2 plan register) makes the share's own ACLs the only
permission authority on that mount: `host/mnt-icloud.mount` gains `noperm`, so
every request is issued and the guest decides it. That removes no protection —
the mount forces one `uid`/`gid` and constant `file_mode`/`dir_mode`, so the mode
the client was testing is one the unit file invented rather than a report of the
NTFS ACL, which the guest enforces on every request regardless (D15's deny ACEs
and ABE are untouched, as is D8 authentication). Two consequences are stated
rather than hidden: the root keeps *displaying* as `0555` while writes to it now
succeed, and a DOS read-only file anywhere on the share is now refused by the
guest instead of pre-refused by the host. `host/mnt-icloud_bridge.mount`
deliberately does not get the option — its exported directory carries no
read-only attribute and its root is writable today.

`host/acceptance-tests.sh` section 5 keeps the root canary and now separates the
two causes: a root that stats as `0555` while a subdirectory is writable names
the client-side veto and the missing `noperm`; anything else is reported as a
genuine refusal by the guest or the share. The same script now prefers
`unix:///var/run/docker.sock` when `DOCKER_HOST` is unset, because a shell whose
Docker context points at Docker Desktop false-failed the two container checks on
a healthy host in the run that found all of this.

Applying this live is the operator path, not an automatic one: reinstall the
package (or copy the unit over `/etc/systemd/system/mnt-icloud.mount`),
`systemctl daemon-reload`, and remount. `SETUP.md`'s troubleshooting table now
carries the symptom and that fix. Nothing here is proven by the repository
checks, which cannot mount CIFS; the live acceptance re-run that confirms the
canary passes belongs to the operator.

### 2026-07-28 — F1 closed: the provisioned guest's recorded confirmations

The three confirmations F1 of
`todo/performance-resource-review-2026-07-27.md` still owed are recorded, so
performance results may now be trusted against this guest. A second and third
full scan completed on 2026-07-28 (01:42:31Z in 30,162 ms and 01:59:08Z in
52,228 ms; 69,620 entries each, `lastError: none` — the library grew from
60,154 entries since the 2026-07-27 first pass). The mounted data share is the
production one: `/proc/mounts` shows `//127.0.0.1/icloud` at `/mnt/icloud`
(`vers=3.1.1`, `rasize=16777216`), and no `icloudtest` mount exists. And the
GUI agrees with the guest: the D37 diagnostic export at 02:14Z reports protocol
compatibility "current", agent build 9, no update banner, every health row
green, and 11 exclusions at revision 1 — which is also the first live exercise
of the diagnostic export in the healthy state (one of E14's four states). F1's
section in the todo note moves to `todo/archive/`.

### 2026-07-28 — M5 live remainder: the share-boundary proof and the host acceptance run

Two of the operator-only M5 sub-steps from
`todo/post-provisioning-followups.md` ran against the live host and guest.

Sub-step (a), the `Provision` share read-only proof: `testparm` reports
`read only = Yes` for `[Provision]` and `read only = No` for `[Data]`, and the
behavioral check from the guest's own network position (an smbclient in the
container's network namespace against the internal Samba on 20.20.20.1)
confirmed it — `ls` on `Provision` succeeds, while both a new-file `put` and an
overwrite of `trigger.json` fail with `NT_STATUS_ACCESS_DENIED`; the same
client's write-then-delete through `Data\.provision` succeeds, and the agent's
15-second `status.json` cadence through `Data` is continuous live evidence of
the writable half.

Sub-step (e), `./host/acceptance-tests.sh`: 28 of 29 automated checks pass
(with `DOCKER_HOST=unix:///var/run/docker.sock`; a shell whose Docker context
points at Docker Desktop false-fails the two container checks). The one real
failure is the section-5 round-trip canary: creating a file at the root of
`/mnt/icloud` is refused by the *host kernel's* CIFS client, because the sync
root carries the DOS read-only attribute (normal for a customized Windows
folder) and the client maps it to mode `0555` and rejects the create locally.
The guest itself allows it — the same create and delete at the share root
succeed over smbclient with the same credential — and subdirectory writes work
throughout, so the data path is healthy and the failure is a client-side
mode-mapping artifact. Follow-up: either the canary moves off the root or the
mount options get a considered `noperm`-style decision; recorded in the run
notes, not silently patched.

The same session also found that the app-generated configuration at
`~/.config/icloud-bridge/env` holds a `SHARE_PASS` the guest does not accept
(the repo `.env` one authenticates; today's re-provision preserved the existing
password and delivered no secret, so nothing misbehaved live). Because
`_default_env_path` prefers that file when it exists, a future secret delivery
would default to the wrong credential. Left open deliberately: the M5 (g)
password-reset exercise later the same day is the natural place to converge the
guest onto the app-managed credential, and its record should say which way it
was resolved.

### 2026-07-28 — The Create-VM dialog is honest when the installer is cached

todo/lifecycle-dead-ends.md item 6: the confirmation always warned "several
gigabytes" and "20-40 minutes" even when `custom.iso` was already present in
`/srv/icloud-vm/storage`, where the real cost is a few minutes and zero
download. `firstrun.cached_windows_install_media()` now owns the cheap check
and `_confirm_create_vm` words the dialog accordingly; the dialog also
describes the automatic continuation (the item-8 auto-start) and names the
iCloud sign-in as the one step the operator does themselves. Alongside: the Qt
test fixture's teardown now drains a second worker generation, fixing an
intermittent cross-test leak where a late forced-refresh worker from one test
called the next test's health fakes (seen as a spurious
no-CIFS-resume failure roughly every other combined run).

### 2026-07-28 — Create Windows VM flows straight into the first provisioning run

todo/lifecycle-dead-ends.md item 8: after **Create Windows VM** the app sat
silent — the operator polled the VM screen by hand and then clicked **Set up
Windows automatically** themselves, although both halves were already built.
The `VM_CREATED` continuation now begins the first run automatically through
the same `_start_first_provisioning_run()` core the manual action uses (the
Create Windows VM confirmation already described the whole end-to-end
sequence; the click remains for re-entry after an interruption), and the
existing guest-ready probe waits through the Windows install as before. The
complement: `notify.ProvisioningTracker` sends one desktop notification per
run for each moment that needs the operator — waiting for the iCloud sign-in,
a failure (including a guest-reported error status), and completion — because
provisioning is exactly when the operator has tabbed away, and health-incident
notifications are deliberately paused there. D31's assistant prose records the
auto-start.

### 2026-07-28 — The app creates its own configuration; choosing a .env is now the advanced path

todo/lifecycle-dead-ends.md item 7: the Setup tab used to block on "choose the
.env file", and `check_env`'s hint sent the operator to a terminal to invent a
machine-to-machine credential the app already transports safely. **Create
configuration** is now the default Setup action: it writes an exclusive-create
0600 file at `$XDG_CONFIG_HOME/icloud-bridge/env` (0700 directory) containing a
`secrets`-generated 32-character alphanumeric `SHARE_PASS` — which also retires
the placeholder-not-replaced failure mode — plus `DISK_SIZE`/`RAM_SIZE`/
`CPU_CORES` defaults derived conservatively from the machine, editable on the
tab and validated against the env grammar before anything is written. An
existing file at the conventional path is reused, never rewritten, and resume
and reselect moments now find that conventional file instead of asking again;
**Use an existing .env** remains the manual path. Recorded as D49, narrowly
amending D41's "never persists": the GUI writes the generated secret exactly
once, at creation, and still never logs, displays, or re-reads it outside the
existing delivery channel.

### 2026-07-28 — The app can tell "no watcher" from "the guest is busy"

The positive half of todo/lifecycle-dead-ends.md item 9: the only way the host
learned nobody was listening used to be staging a run, counting, and showing a
bootstrap hint after 90 s. The watcher now writes a small untrusted beacon
(`watcher.json`: task name, bundled agent build, registered-at) to the Data
outbox at `-Install` time and refreshes it at task start, best-effort and
atomic; `guestprov.read_watcher_beacon()` validates it defensively before
staging, and when it is absent the app leads with the bootstrap hint
immediately. The 90-second heuristic stays as the fallback for pre-beacon
watchers, and presence is only ever a hint — never proof the watcher is
healthy. The hint itself is now typeable through noVNC: it leads with
`powershell -ep bypass -File C:/OEM/watcher.ps1 -Install` (no backslash, no
paste), keeps the UNC form only for pre-feature VMs with no `C:\OEM` payload,
and points at RDP on 127.0.0.1:3389 for a real clipboard and keyboard layout.
Section 4.1's protocol table and D40 record the beacon. A seam test pins
watcher.ps1's `$AgentBuild` copy to `bridge.AGENT_BUILD`. Live confirmation of
the beacon writes on a real guest remains the operator's.

**Live pass, 2026-07-28.** After `make reinstall` and a GUI relaunch, the
operator ran the manual **Re-run Windows provisioning** route against the
provisioned guest (no skew banner: guest and checkout both at `agentBuild` 9).
Run `e2478e4a` reached `done` with every check `ok` (`shareCredential`
`unverifiable` by construction, D44), preserved the existing share password,
and proposed no repair work. About a minute later the redeployed watcher wrote
the first live beacon — `watcher.json` with the task name, `agentBuild` 9 and
`registeredAt 2026-07-28T02:02:05Z` — to the Data outbox at task start, where
the host validates it. The `-Install`-time write and a fresh-VM pass remain for
the OEM rebuild exercise.

### 2026-07-28 — A start that fails on missing shares now offers Setup, not just Retry

The first two lifecycle dead ends recorded in todo/lifecycle-dead-ends.md
(items 1-2, from the 2026-07-27 incident): a container created outside the app
failed power-on on the missing shares and left a red "The Windows VM did not
start." banner whose only action, Retry, could never succeed. The controller
now classifies D45's bounded helper excerpt before dispatching: the banner
heading follows the failure kind (VM did not start / VM running but its iCloud
shares are unavailable / share credential rejected), and the `mount error(2)`
case exposes **Set up Windows automatically** alongside Retry, entering no-CIFS
first-run provisioning and writing the D39 intent record at that moment. The
same recovery is offered from RUNNING when a health snapshot shows both mounts
absent and Docker definitively running, so `_can_reprovision`'s healthy-mounts
gate is no longer circular. Recorded as D48 (amending D30 and D39);
`lifecycle.py` stays a pure reducer — it receives only the classified events.
Repository tests cover the reducer transitions and Qt wiring; the live pass on
the real host (a genuinely share-less guest) remains the operator's.

### 2026-07-28 — SETUP.md is app-first

Every place the runbook had the operator run `docker compose up -d` by hand
created the dead end recorded in todo/lifecycle-dead-ends.md: a hand-created
container has no provisioning record, so the app misclassifies it and strands
the operator (no Setup tab, a Retry that cannot succeed). SETUP.md now installs
the GUI before any VM exists (§6) and documents **Create Windows VM** as the way
to bring the container up (§7); the bare compose commands survive only in the
reinstall and data-path-catch-up appendices, each behind a warning that a
hand-created container currently needs removal for the app to recover. The
`DOCKER_HOST` per-command pin, the `make vm-*` wrappers, and the
`-p icloud-bridge` project-name note moved into the recovery context they serve.
Cross-references to the renumbered sections were updated in `README.md`,
`docs/automation-notes.md`, `docs/implementation-plan.md`, `tools/keep-iso.sh`,
the live todo notes, and the one live reference above; historical entries below
keep the numbering that was true when they were written.

### 2026-07-28 — `install-gui.sh` can uninstall what it installed

The per-user GUI install had no removal path, so removing it by hand left
`~/.config/autostart/icloud-bridge-tray.desktop` pointing at a deleted
`~/.local/bin/icloud-bridge-gui`; XDG resolves autostart user-dir-first, so the
orphan shadowed the package's working `/etc/xdg/autostart` entry and the tray
silently never started at login (todo/lifecycle-dead-ends.md item 4).
`gui/install-gui.sh --uninstall` now mirrors `tools/install-hooks.sh
--uninstall`: it removes the launcher, the applications and autostart desktop
entries, and the installed app tree (which contains the icon and venv), and
succeeds quietly when they are already absent.

### 2026-07-28 — Boot-rewritten `desktop.ini` no longer holds exclusions yellow (`agentBuild` 8 → 9)

Live follow-up to D46 (`9cac9c6`, recorded as D47). With everything else
released, three app-container roots stayed at `pending-dehydrate` because
their only remaining allocation was `desktop.ini` — rewritten by the shell at
boot (after the iCloud client's startup scan) and left flagged "modified" by
the client for over an hour, with re-requests and a fresh change event both
failing to move it. Every reboot would have re-opened that window.

A cloud placeholder named `desktop.ini` carrying `HIDDEN`+`SYSTEM` within the
one-cluster residue bound now passes the D46 sync gate and is reported as
residue instead of blocking `applied`. The gate exists so `applied` never
hides the only good copy of user data; `desktop.ini` is per-machine
folder-view configuration the shell regenerates on its own, so nothing a user
made can be lost. All other names keep the full in-sync requirement.

**Operator step: redeploy the agent** (`agentBuild` 9) and reinstall the
rebuilt package so the GUI matches.

### 2026-07-28 — A healthy provisioning run walked the full library four times

The provisioning checklist scanned the iCloud tree once for traversal links
and then enumerated it again while reading every descendant DACL for protected
ACLs and legacy `syncshare` grants. It unconditionally repeated the complete
checklist in `verifying`, even when the first inspection selected no repair.
On the live 60 000-entry guest this made the no-op run spend minutes in active
metadata work; reducing heartbeat intervals could only make the same scan
report more often, not finish sooner.

D44 now treats a complete initial checklist that schedules no repair as the
run's verification. Traversal-link detection shares the required DACL walk, and
script 04 reuses that same pass for its preflight and protected-DACL list. A
healthy no-op run therefore performs one full-tree pass instead of four. Any run
that changes guest state still re-evaluates the complete checklist afterwards,
and traversal links, protected DACLs and legacy explicit grants remain
fail-closed checks.

### 2026-07-28 — Two silent iCloudHome crashes, and the forensics that were not there

Two modal `iCloudHome.exe - System Error ... overrun of a stack-based buffer`
boxes appeared on the guest desktop at 10:19:44 and 10:20:39 UTC on 2026-07-27.
Nothing recorded them beyond the boxes themselves: they exist only as System-log
Application Popup Id 26 events (records 1021 and 1023). Both dying instances
were toast/COM notification activations — command-line suffix
`----AppNotificationActivated: -Embedding`, AppModel-Runtime launch events Id
211, PIDs 3360 and 1064 — and each died 40-60 ms after writing its startup
banner, which is why each popup is paired with a DCOM 10010 timeout for CLSID
`{71E3DFFE-A4AD-4A16-A15B-7C85A2111AD8}`: a package-registered activator that
died before it could register. The long-lived client (PID 5056, started
09:22:58) survived, as did `iCloudDrive`, `iCloudCKKS` and `ApplePhotoStreams`,
and nothing recurred in the following half hour. App version 15.9.60.0, its
dependencies (VCLibs, WindowsAppRuntime.1.8) all `Status Ok`, and WebView2
Evergreen 150.0.4078.99 with its six processes ran undisturbed through both
crashes.

**Every forensic trace was missing, and this repository is what turned them
off.** `01-debloat.ps1` listed `WerSvc` among the services it disabled, and the
live system key also carried `Disabled=1`, so the crashes produced no
Application Event 1000, nothing in `ReportArchive` or `ReportQueue`, and no
dump — while the kernel hard-error path still parked a modal box on a desktop
nobody is watching. Apple's own per-launch logs stop at the banner. That is the
wrong trade for an unattended appliance: it suppressed no UI that mattered and
cost the only evidence.

**The best-supported cause is an aftershock of the clock correction, and it can
no longer be proven.** The 09:21 reboot moved the guest clock backwards by
roughly six and a half to seven hours — the first-boot RTC/Pacific-TZ skew from
the earlier *a guest clock seven hours out* entry, whose `RealTimeIsUniversal`
fix only takes effect at the following boot. iCloudHome logged
`TrayWnd::ThreadRegisterDevice: Last registration time from bag is in the
future!` twice at 09:23:01, the notification platform database
(`wpndatabase.db-wal`) was last written at crash time, and the deaths were on
the notification-activation path. Meanwhile `w32tm /query /status` answered
*The service has not been started* (0x80070426): debloat's in-session resync
nudge had been failing silently against a stopped `W32Time`, so the session
stayed skewed until Windows re-read the RTC. External research found no public
report of iCloudHome failing this way and no fix to copy — 0xC0000409 is
`__fastfail`, a deliberate abort rather than an actual buffer overrun, and the
closest documented classes (WinAppSDK bootstrapper/deployment failures, the
KB5072911 first-logon XAML registration race) are fresh-image phenomena. The
hypothesis is unfalsifiable in retrospect precisely because crash recording was
off, which is the part worth acting on.

What changed:

- **Crashes are now recorded silently instead of not at all** (`edd4412`).
  `WerSvc` returns to `Manual` — it is trigger-started, so it costs no resident
  RAM in the 3 GB guest — the live key is forced to `Disabled=0` with
  `DontShowUI=1`, and per-app `LocalDumps` keys for `iCloudHome.exe`,
  `iCloudDrive.exe`, `iCloudCKKS.exe` and `ApplePhotoStreams.exe` request a
  minidump (`DumpType=1`, `DumpCount=3`) into the crashing user's default
  `%LOCALAPPDATA%\CrashDumps`. There is no global `LocalDumps` key and no
  `DumpFolder`, and the `QueueReporting` task stays disabled, so capture is
  purely local and nothing is uploaded.
- **The time service is started before it is asked to resync** (`fe30377`).
  The RTC block sets `W32Time` to `Automatic` and starts it before
  `w32tm /resync /force`, and surfaces a nonzero exit as a warning carrying
  w32tm's own reason instead of discarding it.
- **A client that dies before sign-in is relaunched** (`dfa492f`).
  `guest-setup.ps1` waits up to two minutes for the MSIX registration to settle
  before the first activation, and during the unbounded sign-in wait relaunches
  a vanished client at most once per five minutes (`Start-IcloudClient`,
  `Test-IcloudClientRunning`). Recorded in the v2 plan §4.1 and in the v1
  runbook row *New files on host not uploading*.

**Applied to the running guest**, which was provisioned long before any of this
existed, by an elevated one-shot script staged over the bridge share. The
policy-override check found no
`HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Error Reporting` key at all,
so nothing outranks the live setting and nothing had to be removed. Afterwards:
`WerSvc` `Stopped`/`Manual` (correct for a trigger-started service), `W32Time`
`Running`/`Automatic`, `w32tm /resync /force` exiting 0 with `Source:
time.windows.com,0x9` and a real `Last Successful Sync Time` in place of the old
0x80070426, the WER root reading `Disabled=0` `DontShowUI=1`, and all four
`LocalDumps` keys reading `DumpType=1` `DumpCount=3`. The guest's UTC now agrees
with the host's to within a second.

What is still unproven: dump capture is configured but has never fired, since no
crash has been induced and the next real one is its first test; and the settle
probe and relaunch loop only execute during a future provisioning run.

### 2026-07-27 — A large exclusion can finally reach `applied` (`agentBuild` 7 → 8)

Two independent reasons a real exclusion could sit at `pending-dehydrate`
forever, both fixed in `ddcb273` and both recorded as D46 in the v2 plan.

- **Verification is now an episode that spans passes.** The old check restarted
  at the top of the tree every minute and gave up after 5 000 placeholder
  queries, while `applied` demanded a complete clean walk — so a 231 GB root of
  ~10^5 files reported a capped measurement indefinitely, no matter how long ago
  its content was released. The agent now keeps an in-memory cursor and spends a
  bounded number of queries per pass, resuming where it stopped; only an episode
  that reaches the end of its candidate list may decide. A large root therefore
  reaches `applied` a few minutes after the content is really gone, reporting
  `verifying remaining content: <checked> of <total> file(s) checked` meanwhile.
  This is the same answer D26/D34 already gave the reclamation sweep: bounded
  steady-state work, label freshness allowed to lag, safety unchanged.
- **Resident residue no longer blocks `applied`.** Three app-container roots
  held a few hundred bytes each in in-sync, unmodified placeholders that NTFS
  stores inside the MFT record, which no dehydration can release — so the agent
  re-requested an impossible dehydration forever and held the exclusion (and the
  D23 tray) yellow with it. Such files are now reported rather than blocking:
  `localAllocatedBytes` carries their bytes and the detail counts them. The
  tolerance is bounded to one NTFS cluster per file, and a modified,
  not-in-sync, non-placeholder or uninspectable file still blocks `applied`
  whatever its size — its only good copy may be the local one (D20/D22).

Enforcement is untouched: the deny and parent guard are still asserted every
pass, and access is denied throughout regardless of state.
`docs/selective-sync.md` explains both behaviours for users.

**Operator step: redeploy the agent.** This is `agentBuild` 8, so a GUI built
from this tree running against a build-7 guest reports skew; take the banner's
**Re-run Windows provisioning…** action (or re-run `04-bridge-agent.ps1`
elevated in the guest) once.

### 2026-07-27 — The watcher restarts itself into new code, and `-Install` can actually replace it

The stuck-provisioning state had two halves, and only one of them was the
`File.Replace` bug. The other was structural: D42 refreshed the installed
`watcher.ps1` "for its next task start", which in practice meant the next
**logon**, so a watcher that was alive but running superseded code stayed that
way indefinitely — and the app's own remedy could not clear it either.
`Install-Watcher` registered over the running instance with `-Force` and then
called `Start-ScheduledTask`, which `MultipleInstances IgnoreNew` refuses while
the old instance is still running. The documented recovery was a no-op against
precisely the state it existed to repair.

Both halves are fixed, and the mechanism was chosen by measurement rather than
by reading the documentation:

- **The keep-alive is a repetition trigger, not `RestartCount`.** Task
  Scheduler's restart-on-failure does not fire when the action itself exits
  non-zero: a scratch task exiting 3 under `RestartCount 3` with a one-minute
  interval was never relaunched in three minutes. A one-minute repetition with
  `MultipleInstances IgnoreNew` does fire — restarting a cleanly exiting scratch
  task at 09:56:03, 09:57:03 and 09:58:02 — and costs nothing while the watcher
  is running. It also means the watcher now survives a crash or a kill, which
  the previous definition did not: before this, a dead watcher stayed dead until
  the next logon.
- **The watcher exits when its own installed copy changes**, at the top of a
  poll pass where no run is in flight, and the keep-alive starts the new copy.
  Verified live twice on the running guest: modifying the installed script at
  10:03:24 relaunched the watcher at 10:04:01 (PID 8060 → 2268), and restoring
  it relaunched again at 10:07:01 (→ 3424). Roughly 40 s, with no logon, no
  reboot and no operator step.
- **`-Install` stops a running instance first**, so the command the app offers
  is a real reinstall. Verified live: it replaced the running watcher (PID 8520
  → 8060) instead of silently leaving it.
- **Existing guests self-upgrade.** The task definition is written only by
  `-Install`, so a guest registered before the keep-alive would never gain it;
  the watcher now adds the missing repetition to its own task at startup.
- The app's unacknowledged-run hint named only one of the two causes ("a VM
  created before automated provisioning has no watcher task"). It now names both
  — absent watcher, or a watcher that cannot start the request — since the one
  command fixes both.

§1's D42 row records the amendment: envelope currency still requires the
explicit bootstrap, but code currency no longer waits for a logon.

### 2026-07-27 — A watcher that cannot record acceptance now says so

The first app-driven provisioning run staged, and then nothing: the app polled a
run the guest never mentioned. The watcher was alive and could see the trigger;
it had accepted the run, created `runs/<id>`, copied the payload and refreshed
its own installed copy — and then `Set-AcceptedToken` threw, because the
*running* watcher process still held the pre-fix `File.Replace` code from before
its restart, and `accepted-run.txt` already existed. The exception unwound into
the poll loop's catch, which only warns, so 30 s later the trigger was still
unconsumed and the whole sequence repeated indefinitely.

The `$null` cause is already fixed, but the failure mode it exposed is not
specific to it: acceptance is the one step that cannot be "marked consumed" when
it fails, because the marker is what failed. It now publishes a watcher error
status for the run before returning, so the app shows the guest's reason instead
of polling forever — the state that is otherwise indistinguishable from a guest
with no watcher at all, which is precisely what the app cannot diagnose on its
own.

**Known gap, deliberately not improvised here:** D42 refreshes the installed
`watcher.ps1` for its *next task start*, so a watcher fix reaches the running
process only after a logon or an explicit
`Stop-ScheduledTask`/`Start-ScheduledTask icloud-bridge-provision`. Every
watcher change therefore needs that step, and a self-restart on detecting a
changed installed copy would amend D42 rather than implement it.

### 2026-07-27 — The iCloud liveness probe watched a process that no longer exists (`agentBuild` 6 → 7)

A diagnostic report from the working bridge showed every row green except
**iCloud client**, which held the overall status at yellow. The dip was real —
all four Apple processes restarted together between 09:22:58 and 09:23:01, and
the status written at 09:22:30 correctly saw none of them — but enumerating the
guest's processes showed the probe was also watching the wrong thing:

```
iCloudHome  5056   9:22:58     iCloudCKKS         6156   9:23:00
iCloudDrive 8340   9:23:01     ApplePhotoStreams  7560   9:23:00
```

There is no `iCloudServices` process in the shipping Store client; that is the
old Win32 client's name. Liveness therefore rested entirely on `iCloudDrive`,
the Cloud Files sync engine — so an engine restart, which is routine, reads as
"the client is gone" and turns the tray yellow. The probe now also accepts
`iCloudHome`, the app's own process, keeping `iCloudServices` only so an older
install still answers. §2 of the v2 plan records the observed process set.

### 2026-07-27 — Long paths in a real library, and a guest clock seven hours out (`agentBuild` 5 → 6)

Two faults that only a real library and a real guest could show, both found by
watching the first agent that could actually publish status.

**ACL reconciliation stopped at 277 characters.** The first full scan (60 154
entries) ended with `lastError: ACL reconciliation failed on '…' : Invalid
name`. .NET Framework's path-based `GetAccessControl`/`SetAccessControl` apply
the legacy `MAX_PATH` check, so any item whose full path exceeds 260 characters
— ordinary in a synced library — could not be read or written, which means an
exclusion on such an item would never have been enforced. The agent's native
enumerator has always used the `\\?\` form; the ACL helpers now use the same
one, through the existing `Long()` helper rather than a second copy of the rule.
The documented escape hatches do not work here: `UseLegacyPathHandling` and
`BlockLongPaths` were both tried live and PowerShell 5.1 has already cached the
legacy behavior. Verified in the guest first — get and set, long directory and
long file, at 274 and 280 characters — then confirmed on the live library:
`lastError: none`, 60 154 entries in 29 s.

**The guest's UTC was seven hours ahead of the host's.** QEMU presents the RTC
in UTC and Windows assumed it was local time, so a guest Setup had placed in
Pacific time computed a UTC hours ahead. The desktop clock coincidentally
matched the host's UTC, which is what makes this easy to miss, but every stamp
the agent and orchestrator publish was future-dated — and D23 correctly refuses
to call a future-dated status fresh. `01-debloat.ps1` now sets
`RealTimeIsUniversal` and the UTC zone at install, and the running guest was
corrected in place.

### 2026-07-27 — Every JSON writer could publish exactly once (`agentBuild` 4 → 5)

With a working agent finally running, the guest rebooted, the agent restarted,
burned CPU — and `status.json` never changed again. Probing `File.Replace` in
the live guest, on a plain local path, produced the same "The path is not of a
legal form" that the earlier entry below blamed on UNC:

```
[IO.File]::Replace($src, $dst, $null)                 -> FAILS (local and UNC)
[IO.File]::Replace($src, $dst, "backup.bak")          -> ok
[IO.File]::Replace($src, $dst, [NullString]::Value)   -> ok (local and UNC)
```

The cause is PowerShell, not the filesystem: `$null` bound to a `[string]`
parameter marshals to the **empty string**, and `File.Replace` rejects `""` as a
backup path. Every `Write-JsonAtomic` in this repository passed `$null`, so
every document — `status.json`, `tree.json`, the agent's private state, the
watcher's accepted-run token, script 04's config — could be written exactly once
(the file-absent `Move` branch) and threw on every write afterwards. The agent
swallowed the exception into a `status` subtask error, which is only reportable
through the file it could not write, so a live agent looked identical to a dead
one.

**This corrects the entry below.** UNC was never the constraint: `File.Replace`
works fine on `\\host.lan\Data` with a real null. That earlier diagnosis fitted
the symptom (first write succeeds, all later writes throw) because the symptom
was the same bug seen through the one writer that happened to be on a UNC path.
All five writers now pass `[NullString]::Value`, the delete-then-rename UNC
accommodation is deleted, and §4.1 of the v2 plan records the real constraint.
Verified live: after the fix the agent publishes a fresh `status.json` every
pass.

### 2026-07-27 — The guest agent imported a DLL that does not exist (`agentBuild` 3 → 4)

With the bridge finally built on the live guest, the agent task registered,
started, and exited with code 1 before writing a single byte of status — so
`04-bridge-agent.ps1` failed its runtime verification with "the task did not
reach Running". Running the agent by hand named the cause exactly: `Unable to
load DLL 'cfapi.dll': The specified module could not be found`
(`0x8007007E`), thrown from the first `CfGetPlaceholderInfo` call.

The module name was simply wrong. `cfapi.h` is the header and CfApi is the
API's name, but the binary Windows ships in `System32` is **`CldApi.dll`** —
the guest has `cldapi.dll` and no `cfapi.dll` at all, and `CldApi.dll` is what
exports `CfGetPlaceholderInfo`. .NET resolves a `DllImport` module at the first
call rather than at load, so this was not a startup or lint failure anywhere: it
was a guaranteed crash on the agent's first placeholder probe, on every Windows
guest, for as long as the native enumerator has existed. `make lint-ps` cannot
see it (PowerShell 7 on Linux never resolves the import) and no host-side test
can either.

`guest-agent/agent.ps1` now imports `cldapi.dll`, `provision/agent.ps1` carries
the byte-identical copy, and `$AgentBuild`/`bridge.py`'s `AGENT_BUILD` move to
`4` per D35. Verified live: the fixed agent was deployed into the running guest,
`04-bridge-agent.ps1 -Scope Agent` reported `task icloud-bridge-agent (Running,
restarts on failure)`, and the host now mounts both shares with
`/mnt/icloud_bridge/status.json` reporting `agentBuild 4`, `icloudClientRunning
true` and `lastError null`.

### 2026-07-27 — A first run now builds the bridge, instead of dispatching an agent-only repair

With the status writer fixed, the first complete live run got all the way to
`verifying` and stopped there: `bridgeBoundary=missing; agentInstall=missing;
agentRuntime=missing`, after a phase that had announced
`04-bridge-agent.ps1 -Scope Agent`. Root cause: `guest-setup.ps1` derived the
dispatch its bridge step executes **before** script 03 created the `syncshare`
account. At that moment the boundary probe's dependency was unmet, so it
answered `pending` — which correctly schedules nothing — while the agent probes
answered `missing`. The resulting work plan therefore contained `update-agent`
and no `repair-bridge-boundary`, and an agent-only scope is precisely what
script 04 refuses on a guest whose `exclusions.json` the boundary scope has
never written. Nothing installed, and the run's own convergence check was the
first thing to notice.

`guest-setup.ps1` now re-derives the checklist, the published work plan and the
dispatch after the share stage as well as after the package/sign-in stage; the
two sites share one `Update-Dispatch` helper, which also consumes the
operator's credential-reset intent exactly once so a completed reset cannot look
like a component that refused to converge. On a first run the bridge step now
receives `-Scope All` and builds both halves. This is what §4.2 of the v2 plan
already required — work is dependency ordered and a `pending` check is re-probed
once its dependency converges — so the plan text is clarified rather than
amended, and `packaging/test-guest-state.ps1` gains the three inspections a real
first run performs, which is the fixture the existing matrix lacked: it fed each
check independently and so never produced the intermediate state that made the
wrong scope look right.

### 2026-07-27 — The guest status writer no longer dies on its second write

The first live provisioning run acknowledged, wrote one status, and then went
silent forever. Root cause: `Write-JsonAtomic` in `guest-setup.ps1` and
`watcher.ps1` uses `[IO.File]::Replace`, and .NET Framework's `File.Replace`
rejects UNC paths ("The path is not of a legal form") — and the status file
lives on `\\host.lan\Data`. The very first write ever succeeded because the
file did not exist yet (the `Move` branch); every subsequent write, including
`Stop-WithError`'s attempt to publish the failure, threw. The run was not
stalled; it was crashing every time it tried to report progress, invisibly.

The two UNC writers now delete-then-rename when the destination is a UNC path,
keeping the same-directory temp file. The host reads a momentarily missing
status as "not readable yet", never as an error, so the sub-second non-atomic
window is harmless; the agent's and script 04's copies write local NTFS paths
only and keep the fully atomic `Replace`. §4.1 of the v2 plan records the
accommodation. Diagnosed end-to-end from the host via `tools/guest-ctl.sh`
keystroke injection with output captured over `\\host.lan\Data` — the loop the
tool was built for.

### 2026-07-27 — Guest scripts are pure ASCII; PS 5.1 was parsing them as ANSI

The first live OEM install failed to register the provisioning watcher, and the
first bootstrap attempt failed the same way: a PowerShell **parser error** in
`watcher.ps1`. Root cause: the guest scripts contained UTF-8 em dashes, and
Windows PowerShell 5.1 (and cmd.exe) read BOM-less files as the ANSI codepage,
not UTF-8 — the em dash's trailing `0x94` byte is a curly closing quote in
CP1252, which terminates a double-quoted string early and breaks the whole
parse. `make lint-ps` could never catch it, because PowerShell 7 reads the same
bytes as UTF-8; this is exactly the 5.1-compatibility gap CONTRIBUTING warns
about, now with a live specimen.

Every em dash (and one `§`) in `provision/*.ps1`, `provision/install.bat` and
`guest-agent/agent.ps1` is replaced with ASCII; text-only, no behaviour change.
`tools/hygiene-checks.sh` gains a guest-scripts-are-ASCII check so the class of
bug is mechanically extinct, and CONTRIBUTING's LF rule now states the ASCII
requirement and why the PowerShell linter cannot enforce it.

### 2026-07-27 — The v1 plan's recovery guidance now leads with the app

`docs/implementation-plan.md` still told the operator to recover by hand — edit
and run `C:\OEM\03-create-share.ps1`, "re-run scripts 01, 03 and 04", re-run
`04-bridge-agent.ps1` elevated — in rows people actually follow when something
breaks. Since the app provisions the guest, the v2 plan wins on conflict and
those routes go through **Re-run Windows provisioning…** (D35, D42, D44), with
the manual sequence kept as a documented fallback. §5, §7 and the two §10 runbook
rows now lead with the app action and demote the scripts, cross-referencing
SETUP.md §8 rather than restating it.

Two corrections fall out of writing it down, both checked against the code
rather than assumed:

- **Debloat is not covered by a re-provisioning run.** The §4.2 checklist
  reconciles the client, share, bridge boundary and agent; it does not model
  trimmed inbox apps. §4 and the feature-update runbook row now say so and keep
  `01-debloat.ps1` manual.
- **Scripts 01 and 02 are not in the refreshed bundle.** `$ProvisionPayload`
  (`provision/guest-state.ps1`) stages only 03, 04, `agent.ps1`, `guest-state`,
  `guest-setup` and `watcher`, so "run it from
  `C:\ProgramData\icloud-bridge-provision\current`" is right for 03/04 and wrong
  for 01/02, which exist only in `C:\OEM`. The sections say which applies.

Documentation only; no behaviour changed. Historical records (earlier entries
here, `docs/acceptance-results.md`) are left as written.

### 2026-07-27 — A rejected share password no longer looks like a slow guest

`icloud-bridge-power on` gives up after five minutes with "the shares did not
become usable"; until now that was the *whole* message, and the CIFS error that
actually explains it — a rejected credential, a share the guest does not export,
an unreachable server — stayed in the `mnt-icloud.mount` /
`mnt-icloud_bridge.mount` journals where the app cannot look. So a wrong
`syncshare` password and a guest that is merely slow produced identical text,
and the GUI's **Retry and reset share password…** route, which matches
authentication wording in the helper's stderr, could almost never fire.

The helper now appends a filtered excerpt of those two units' recent journal to
that failure message (D45). Only lines carrying a mount or CIFS diagnosis
survive; systemd's own restatements and ordinary chatter are dropped, a
password-bearing `key=value` loses its value, and what is quoted is a few short
tail lines per unit. Collecting it is read-only, bounded by `timeout`, and never
fatal — no journal, or nothing that passes the filter, simply leaves the message
as it was. The transaction itself is untouched: same exit status, same marker
and teardown, no new `==> ` phase line, and no control decision derived from an
exit code. Nothing weakens the rule that only an authentication failure may
offer a password reset; a timeout or a missing share still shows the generic
failure and leaves a working password alone.

Verified in this checkout: `sanitize_journal_excerpt` is a pure function of one
string, like `classify_inspect_output`, and `gui/tests/test_power_helper.py`
extracts and runs it under bash for the credential, missing-share, chatter,
ordering, bounding and redaction cases; a Qt wiring test drives a readiness
timeout carrying a quoted `mount error(13)` through to the reset offer. The
helper as a whole still needs root, systemd and real mounts, so the excerpt's
behaviour against a live journal remains operator-verified. **The installed
`/usr/local/bin/icloud-bridge-power` is stale until the operator reinstalls the
package.**

### 2026-07-27 — The app now provisions the Windows guest, and the docs say so

The implementation of the decisions recorded in the entry below, plus the
operator documentation for it. After **Create Windows VM**, the Setup tab offers
**Set up Windows automatically**: the app installs iCloud for Windows, waits
while the operator signs in, creates the SMB share, installs the bridge agent,
and hands over to **Check setup and connect**. The Apple ID sign-in and the
iCloud Drive toggle are what remain manual, and that is unchanged and deliberate.

- **One action repairs an existing VM.** **Re-run Windows provisioning…** is on
  the Status tab, in the tray menu, and behind the D35 skew and
  protocol-incompatible banners — one implementation with one enablement rule,
  deliberately still available while the bridge protocol is `skewed` or
  `incompatible`, because that is what those banners point at. Its confirmation
  says plainly that `/mnt/icloud` and `/mnt/icloud_bridge` **stay mounted**: the
  app pauses its own bridge reads but cannot police another program's use of the
  mounts, so the operator closes files and shells under `/mnt/icloud*` first.
- **A run reconciles rather than replays** (D44). It inspects a fixed checklist,
  repairs only the components that are missing or drifted, stops before mutating
  anything that reads as blocked or unknown, and re-checks everything afterwards.
  An agent-build mismatch on an otherwise healthy VM therefore plans exactly
  *Update bridge agent*: no env file is requested, no password is reset, no ACLs
  are rewritten. The **Reset share password from an env file** option on the
  re-run confirmation is unchecked, and only a first run or a missing account
  asks for the secret without it.
- **The share credential is never shown green.** It reads *reset during this run*
  or *preserved*, each qualified with why the app cannot do better: Windows never
  reveals an account password, so connecting is the proof. That honesty is the
  point — a green row there would be a claim about something nothing on the host
  can verify.
- **No active recovery instruction names `C:\OEM` any more.** It is written once
  at install time and goes stale — the author's live VM was four commits behind,
  missing the very skew detection it was supposed to have. The troubleshooting
  rows in `SETUP.md`, the pre-2026-07-26 data-path catch-up section, and
  `README.md`'s pre-release note now route through the re-provision action; the
  by-hand sequence is retained as an explicitly labelled fallback and points at
  the administrator-protected `C:\ProgramData\icloud-bridge-provision\current`
  copy that each run refreshes (D42). Historical entries in this file and in
  `docs/acceptance-results.md` are left exactly as they were written.
- **The one-time bootstrap is documented where it is needed.** A VM created
  before this feature has no watcher task, so a staged run is never acknowledged
  — not an error. The app keeps polling and, after 90 s, shows the single
  elevated command that installs the watcher; the already-staged request is then
  picked up with no further click on the host. `SETUP.md` §8 carries the same
  command, and `docs/automation-notes.md`'s scoreboard now records steps 9, 13
  and 14 (scripts 02, 03 and 04) as automated rather than as operator work.

**Nothing here has been run against a real guest.** The checkout has no KVM, no
Windows VM and no Docker guest container, so the whole live matrix — the
read-only enforcement on the `Provision` share, `RunLevel Highest` behaviour,
Windows PowerShell 5.1 runtime behaviour of the new scripts, Store readiness
timing, the bootstrap on the author's pre-feature VM, and every reconciliation
outcome — is still the milestone after this one, and is unproven. `make check`
and `make lint-ps` prove pure state models, command construction, package
contents and syntax; they do not prove any of the above.

### 2026-07-27 — App-driven guest provisioning: the decisions, before any code

The plan half of the work described in
[`todo/archive/automated-guest-provisioning.md`](todo/archive/automated-guest-provisioning.md).
No behaviour changed here — this entry records the decisions that the
implementation must now be judged against, because `CONTRIBUTING.md` requires a
todo's proposed decisions to reach a plan register before they are implemented.
Today a fresh VM auto-runs only `01-debloat.ps1` and leaves a `NEXT-STEPS.txt`
telling a human to run 02, the Apple sign-in, 03 and 04 by hand; the goal is that
the app does all of that except the sign-in, and can re-provision an existing VM.

- **D40-D42 give the app a way into the guest that does not weaken it.** The host
  stages the current scripts on a new Samba share that is **read-only to the
  guest**, and an elevated scheduled task inside Windows copies that payload into
  an administrator-only directory before running it. dockur's existing `Data`
  share is `writable`/`guest only`/`force user = root`, so anything staged there
  can be replaced by any guest process — it stays the status channel and is never
  an execution source, and neither is the `C:\OEM` copy, which is written once at
  install time and was found four commits stale on the live VM. Keystroke
  injection through the QEMU monitor was rejected as the product mechanism: it is
  blind without OCR, and it would type the share password.
- **D41 narrows D31's "the password never passes through the GUI".** It still
  does not, except through one Qt-free module, in one direction, at one moment:
  after the guest says it is waiting, the value is streamed over `docker exec`
  stdin to a run-scoped file and consumed immediately. Never argv, environment,
  a host temp file, a log, the status channel or the clipboard.
- **D43 extends D39's interrupted-provisioning record** to cover re-provisioning
  and to carry the container's `State.StartedAt` token, the guest run ID and the
  last phase, so a GUI restart mid-run reattaches to *its* run instead of
  guessing — while still never storing the env-file path.
- **D44 makes a run a reconciliation rather than a replay.** It inspects a fixed
  checklist, repairs only what is missing or drifted, stops before mutating
  anything when a check is blocked or unknown, and re-verifies afterwards. An
  agent-build mismatch therefore updates the agent and nothing else — no password
  reset, no ACL rewrite — and the credential check is honestly reported as
  unverifiable rather than shown green.
- **D35's recovery action changed shape.** The skew banner used to hand the
  operator an elevated `C:\OEM\04-bridge-agent.ps1` command line. It now enters
  the confirmed **Re-run Windows provisioning…** action, in both the skewed and
  the incompatible state, through the same controller action as the Status tab.
  The two properties behind the old instruction are unchanged: the GUI holds no
  guest-admin credentials, and guest code is never updated silently — elevation
  lives in the guest watcher task and the update needs an explicit confirmation.
  E12 now tests the button instead of a hand-run of script 04.

The v2 plan gained §4.1 (channel, `trigger.json`/`status.json`, the check-state
and work-ID enums, the ordered phases, and the rules that must not be
improvised around) and §4.2 (the fixed inspection checklist, how work is derived
from it, and the explicit strange-state policy). Nothing in this entry has been
run against a guest: the scripts, the GUI module and the whole live matrix are
the milestones after this one.

### 2026-07-27 — A second read-only review, and the three things a checkout could act on

A follow-up performance and resource review of the live host, recorded in
[`todo/performance-resource-review-2026-07-27.md`](todo/performance-resource-review-2026-07-27.md).
It changed no VM, guest, host or application setting: the container was powered
off through the bridge lifecycle while the read-only measurements were running,
so it was left off and no later live test was attempted. Its main conclusion is
that **this guest is still unprovisioned**, so most of what it found cannot be
acted on until I-001 supplies a real scan, a real library and a real E0 result.
Three findings did not depend on that, and they are what shipped here.

**What was measured.** Idle CPU 11.47 core-seconds per 60 s — 19.1% of one core,
consistent with the earlier 16-18% samples — split guest 73.7% / host kernel
24.7% / QEMU userspace 1.7%. That tightens I-010's ceiling: every host-side knob
*combined* is capped at 2.83 core-seconds per minute, **4.7% of one core**, and
the rest is inside Windows. Idle container I/O was 0.4 KiB/s read and 18.5 KiB/s
written, comfortably inside I-011's ceiling and well below the earlier 66 KiB/s.
cgroup `memory.current` was 4.37 GB against the 4 GB guest, with 43 GiB
available on the host and no OOM, throttle or swap event. `data.img` had 14.8
GiB allocated of 120 GiB logical on local ext4/NVMe, already running
`cache=none,aio=native,discard=unmap,detect-zeroes=on` behind a virtio-SCSI
iothread. The image was dockur/windows v6.02 — the current upstream release that
day — with `vhost=on` still applied from I-008, host THP at `madvise`, and KSM
sharing zero pages. Every dockur helper (`docker-proxy`, nginx, websocketd,
dnsmasq, Samba, wsddn) rounded to 0.00% CPU over a 10 s `pidstat` sample.

- **Nothing warned the operator that a content previewer hydrates the library.**
  `rasize=16777216` does not over-hydrate a small read — that was cleared during
  the 2026-07-26 review — but a thumbnailer, preview pane, media metadata probe,
  checksum or backup tool, antivirus scanner or desktop content indexer reading
  under `/mnt/icloud` performs *real* reads and downloads real content. Only a
  loose remark at the end of this file recorded it. `README.md`, `SETUP.md` §9
  and [`docs/selective-sync.md`](docs/selective-sync.md) now state the
  distinction where it is needed: directory enumeration and metadata are free,
  opening content is not, turn previews and indexing off for network locations,
  and exclude a folder to make the question moot. None of them claims metadata
  alone hydrates anything, and no script touches a desktop-wide preference —
  those belong to the user, not to this project.
- **The GUI's list-response poll no longer ticks when there is nothing to poll
  for.** `MainWindow` started a 1 s `QTimer` at construction and kept it running
  for the whole tray session, so a window that never expanded a folder still woke
  86 400 times a day to iterate an empty list. It is now armed by a dispatched
  request and stopped once no request and no response poll remain, with the I/O
  pause as a third input; `_sync_poll_timer` is the single place that decides.
  The one-second cadence *while a listing is in flight* is unchanged, D17's guest
  tick is untouched, and the D29/D30 teardown still stops it outright. Qt wiring
  tests cover an idle window recording zero callbacks, arming on dispatch,
  disarming on the answer, re-arming for a continuation page, and staying stopped
  through a guest error, a timeout with its cancel, a dispatch failure, a reload
  and a quiesce/resume pair. Honestly measured, this is a small saving; it is
  here because it is certain and cheap, not because it moves the 19.1%.
- **`tools/profile-windows-idle.ps1` gives I-010 the sampler it described.**
  Read-only, guest-side, unelevated where WMI permits: two
  `Win32_PerfRawData_PerfProc_Process` samples around an idle interval (300 s by
  default, matching `tools/vcpu-profile.py --seconds 300`), reported as **deltas**
  — CPU core-seconds, working set, private bytes, disk read and write — with the
  `_Total` minus `Idle` aggregate as a cross-check so exited processes and
  counter resets show up as an explicit unattributed remainder instead of
  vanishing. Service Host rows carry their hosted service names. It records
  process names, PIDs, service names and numbers, and nothing else: no command
  lines, environment, file paths, window titles, user names or file contents,
  matching D37's boundary. Its header says plainly that R-012 and R-019 to R-022
  already accept most of what it will name, so a row is a measurement rather than
  a licence to disable anything.

**Reconfirmed, not reopened.** The review re-derived and left closed: more
dockur/QEMU flags (R-013), every disk-model change (R-014/R-015/R-026), a smaller
guest (R-036), balloon/KSM/hugetlbfs (R-017), disabling dockur's helper daemons
or the Docker proxy (R-025), more Windows debloat without a numerator (R-027),
and fewer vCPUs (R-039). Two new ideas were examined and *not* implemented:
defragmenting the 12 887-extent sparse image, which is not a performance result
and adds data-safety risk to a path already measured at 0.23% of lifetime CPU,
and deleting the cached 7.9 GiB ISO automatically, which is an operator
trade-off `SETUP.md` §7 already documents rather than recurring waste.

**Not verified here, and not claimed:** Windows process attribution, any Windows
PowerShell 5.1 execution, agent build 3 against a real library, the production
CIFS mounts, E0 throughput, exclusion costs, GUI tree/list behaviour at scale,
`halt_poll_ns=0`, and CPU-affinity variants. The new sampler has been parsed by
PowerShell 7 and exercised end to end against a stubbed counter provider on
Linux; it has never run on Windows.

### 2026-07-27 — I-008 executed live: D33 applied, D32 disproven, and an honest null result

The remediation the 2026-07-26 review left for the operator, executed on the
live host, with the evidence recorded in
[`docs/acceptance-results.md`](docs/acceptance-results.md). No repository code
changed for the remediation itself; what changed is the record, `SETUP.md`'s
guidance, and what the project now knows.

- **The container was gracefully recreated and now runs `vhost=on`.** The D29
  power helper has never been installed on this machine (neither have the
  mounts or units), so the ordered teardown legitimately reduced to
  `docker compose down`, which honored the 2-minute ACPI grace; Windows shut
  down and booted cleanly, `RestartCount` stayed 0, and all guest services
  (SMB, noVNC, RDP) came back. `make acceptance` now passes all three D33
  checks it failed the day before.
- **The predicted D33 throughput win did not materialize at the scale that was
  measurable**: warm 20 MB reads via userland `smbclient` measured median
  652.6 MB/s before vs 425.6 MB/s after, with widely overlapping distributions
  — recorded as *no demonstrated change*, not a regression. Transfers that
  finish in tens of milliseconds are scheduling-noise-bound, `docker-proxy`
  and `smbclient` sit in the measured path, and the E0 kernel-cifs read of a
  large file remains the measurement that matters. `SETUP.md` now tempers the
  expectation instead of promising a number.
- **D32 is settled, in the opposite direction from the original scare:** the
  guest **does not require SMB signing**. A real client with
  `client signing=disabled` was accepted before the recreate and after the
  cold boot — evidence of a kind the malformed raw-packet probe could never
  give. `SETUP.md`'s "do not take it on faith" passage now records this and
  keeps `Get-SmbServerConfiguration` as the in-guest confirmation.
- **Discovery: this guest was never production-provisioned.** Only the
  historical D5 `icloudtest` share exists; `03-create-share.ps1` and
  `04-bridge-agent.ps1` have never run here. The "re-run script 03" framing of
  I-008 was wrong on this machine — the remaining work is I-001's first-run
  runbook. This also means the agent — including yesterday's serializer and
  today's walk rewrite — has still never executed on a real guest.
- **Post-recreate idle measured 16.2% of one core** (120 s window) with
  14.5 KiB/s of container writes — both below the 2026-07-26 figures (18.1-
  18.4%, ~66 KiB/s), unattributed, possibly just the fresh boot. With vhost on,
  the virtio worker shows up as a `vhost-<pid>` thread row in
  `tools/vcpu-profile.py` on this kernel, so the profiler keeps seeing the
  network cost D33 moved out of QEMU's main loop.

**Tried and blocked this session, so it is not retried blind:** the
`halt_poll_ns=0` benchmark DFR-003 demands needs root to write
`/sys/module/kvm/parameters/halt_poll_ns`, and this session's passwordless
sudo covers only `/usr/sbin/ip`. The procedure stays fully documented in
`SETUP.md`; the ceiling on it re-measured at 4.8-5.5% of one core (the
kernel column at idle). DFR-003 is unchanged.

### 2026-07-26 — The agent's walks: 73x less overhead, and a crash found under them

The main item of I-009, plus a bug the rewrite surfaced that turned out to
matter more than the speed.

- **Every ordered walk now sorts in the compiled helper and builds child paths
  by concatenation.** `Get-SweepCandidates` and `Build-Node` re-created a
  scriptblock comparator per directory and paid a delegate dispatch per
  comparison; all four recursive walks (`Measure-SubtreeCheap`,
  `Measure-ExclusionAllocation` included) called provider-aware `Join-Path` per
  entry. The comparator moved into `IcloudBridgeNative.SortByName` — same
  OrdinalIgnoreCase-then-Ordinal order, now asserted by the new
  `tools/test-agent-walk.ps1` under `make test-ps`, whose expectations were
  captured from the retired scriptblock comparator first — and child paths are
  `$Full + '\' + $e.Name`, valid because `$SyncRootFull` is `TrimEnd('\')`-ed at
  startup. Modeled at 20 entries/directory under PowerShell 7: 61.5 us -> 0.84 us
  overhead per entry, ~6.2 s -> ~0.08 s per 100k-entry pass. The fixture also
  pins the trap the DFS cursor comparator exists for: siblings with characters
  below `/` sort differently as flat strings than the walk emits them.
- **The old code crashed on directories with zero or one entry — every pass.**
  Discovered while proving the rewrite safe, not by a report. PowerShell
  collects `Get-Entries`' output, so a single-entry directory yields a bare
  scalar and an empty one yields `$null`; `List[object].AddRange` throws on
  both, and the agent runs under `$ErrorActionPreference = 'Stop'`. Any such
  directory anywhere in the library aborted the entire ten-minute `tree.json`
  pass (caught and reported only as a `tree` subtask error) and the reclamation
  sweep's candidate walk the same way. Real libraries contain single-entry
  directories almost by definition, so the ten-minute pass has plausibly never
  completed on the operator's guest — nothing host-side can see that without a
  mount, which is itself I-001/I-008 territory. `SortByName` accepts and
  returns all three shapes (`tools/test-agent-walk.ps1` asserts the null,
  scalar and `Object[]` cases), and the accumulator-only walks were already
  `foreach`-safe.
- `$AgentBuild` 2 -> 3 with the matching bump in `bridge.py`, so a guest still
  running the old agent shows the D35 re-run banner rather than being silently
  slower (and silently broken on such directories). The operator step is the
  usual one: re-run `C:\OEM\04-bridge-agent.ps1` elevated.

**Verified here:** `make check` (452 tests without Qt), `make test-all`,
`make lint-ps`, `make test-ps` including the new fixture. **Not verified here:**
Windows PowerShell 5.1 execution, `Join-Path` equivalence on a real `C:` path,
and the crash fix against a real library — the checkout has no guest (I-001).

### 2026-07-26 — An idle acceptance criterion that can actually pass

I-011, shipped. Plan section 11.3's idle criterion said "guest idles < 5% host
CPU (check `docker stats`)", and `docker stats` reports percent-of-one-core on
Linux, where a healthy measured guest reads ~18-20%. A criterion that fails on a
healthy system is worse than none — it either blocks acceptance or trains the
operator to wave red through. It now reads: at most **0.30 core-seconds of
container CPU per wall second** and at most **200 KiB/s of container block
writes** over a ≥ 300 s window with no operator activity, both from one run of
`tools/vcpu-profile.py`. The thresholds sit above the author's measured idle
floor (0.18 core-s/s, ~66 KiB/s) with headroom for background maintenance.

- `tools/vcpu-profile.py` now samples the container cgroup's `io.stat` across
  its window and prints read/write KiB/s (or cumulative GiB with `--lifetime`),
  so the write-churn half of the criterion comes from the same command as the
  CPU half. `/proc/<pid>/io` was deliberately not used: it needs ptrace rights
  over a root-owned QEMU; the cgroup file is world-readable.
- `docs/acceptance-results.md` gained "Idle CPU cost" and "Idle write churn"
  environment-baseline rows naming the command and the thresholds. They are
  blank until measured at true idle on the real host (I-001).
- Plan section 8.1's `halt_poll_ns` bullet pointed the operator at
  `docker stats` for a benchmark that only the profiler's `kernel` column can
  resolve; it now points at the profiler, agreeing with `SETUP.md`.

### 2026-07-26 — First measurements from a real guest, and what they changed

The first review in this project's history conducted with a **live Windows guest
running on the same machine as the checkout**. Every earlier performance entry —
including the whole-path review of the same date below, and the R-012..R-024 rows
it closed — reasoned from source and specification because no guest was
reachable. The measurements changed the conclusions, and in one case reversed the
priority order outright.

**What the guest actually costs, measured.** Idle CPU 18.1% of one core over
180 s and 18.4% over 600 s (25.1% over the container's 26 h lifetime), with block
reads near zero. Of that, 68.5% is guest mode, 28.6% host kernel, 2.9% QEMU
userspace; the four vCPU threads account for essentially all of it, while the
block iothread contributed 54 s of 23 800 lifetime core-seconds and the QEMU main
loop 18 s. **The cost is inside Windows, not in host emulation and not in I/O**,
which bounds every host-side knob at roughly 5% of one core. Writes run
~66 KiB/s in bursts, about 5-8 GB/day, on a guest doing nothing.

- **Two shipped decisions were found unapplied to the running container, and no
  check in the repository could see it** (`80947be`). `acceptance-tests.sh` asked
  the *host* whether `/dev/vhost-net` existed and printed PASS while the
  container did not have it. It now asks the container's device list and greps
  the guest QEMU command line for `vhost=on`, which is ground truth. The
  remediation is the operator's and is tracked as I-008 — it is drift, not a
  performance improvement, and is deliberately not counted as one.
- **`tools/vcpu-profile.py`** (`80947be`). Splits the guest's CPU by execution
  mode from `/proc`, so an aggregate `docker stats` percentage stops being the
  only evidence — that number cannot distinguish "the guest is busy" from "we are
  burning host CPU emulating it", and those have different fixes. Three traps are
  documented in it because each cost real time: QEMU is a *grandchild* of
  `.State.Pid`, `comm` must be split on the **last** `)` because the vCPU threads
  are named `CPU 0/KVM` with a space, and `utime - gtime` needs a >=60 s window
  and a clamp at zero.
- **The agent's JSON serializer is 5.3x faster, byte for byte** (`82500af`).
  `tree.json` was escaped by a per-character PowerShell loop through an
  eight-branch chain with a `StringBuilder` allocated per call, and every level of
  the recursion materialized each child as a complete string before `-join`ing
  them — recopying every byte once per level of nesting, for the whole tree every
  ten minutes, forever. Escaping moved into the compiled native helper with a
  fast path for the overwhelmingly common no-escape case, and a document is now
  appended into a single `StringBuilder`. Measured 3.07 s -> 0.58 s on a
  5 461-node / 796 KB tree under PowerShell 7. `make test-ps` is new and asserts
  the **exact output bytes**; its expectations were captured from the old
  implementation before the rewrite, so it is a regression test rather than a
  restatement of current behaviour. `$AgentBuild` 1 -> 2, so a guest still running
  the old agent is reported (D35) instead of being silently slower.
- **The GUI stops re-rendering an unchanged state column** (`7594d6e`). The 5 s
  tick called `apply_snapshot` unconditionally and every non-rebuild pass walked
  every tree row — including while the window was hidden in the tray, against a
  `status.json` the agent rewrites only every 15 s. Measured at 5 219 rows:
  `apply_snapshot` 12.07-13.43 ms per tick, of which the state column was
  12.4-12.7 ms; with the early-out, 0.016-0.034 ms. Small against the guest's
  burn, but linear in library size, and it removes a periodic block of the GUI
  thread. Extends D34's host-side rule from the document *parse* to the *render*.
  A row epoch is part of the memo key so a newly listed file cannot inherit a
  "nothing changed" decision and keep an empty state cell.
- **The version is pre-1.0** (`37a6fab`). `2.0.0` claimed a stability history
  that never existed; it is now `0.2.0`, and the withheld release boundary moves
  to `0.3.0`. See I-007.

**Two things were attempted and withdrawn**, recorded so they are not retried
blind:

- An automated SMB posture probe, and with it the confidence in this review's
  **D32 finding**. A hand-rolled unauthenticated SMB2 NEGOTIATE returned security
  mode `0x0003` — signing required — four consecutive times early in the session,
  each a well-formed response with `STATUS_SUCCESS` and coherent capabilities and
  I/O sizes. Later in the same session the identical packet stopped being
  answered, and it has not been answered since, **including after a full host
  restart that cold-booted the guest**.

  What the follow-up established, so the next reader does not repeat it. The
  guest's SMB server is healthy and behaving correctly: a connection that sends
  nothing stays open for at least 6 s, deliberate garbage is closed, and the
  guest answers ping on the QEMU network with the DNAT rules unchanged. So the
  server is **rejecting this probe's packet as malformed, which is the correct
  response to it** — the probe was wrong, the guest was not. Two claims made
  earlier in this session were consequently withdrawn: that repeated handshakes
  had put the server into a refusing state (a cold boot would have cleared that,
  and did not), and that a 40-byte dialect offset was reliably accepted (it is
  now rejected too).

  **What this means for D32: treat "signing is required on this guest" as
  unconfirmed.** Four coherent readings are not nothing, but a measurement that
  cannot be reproduced is not evidence to act on, and the mechanism that produced
  those four answers is not understood. Re-confirm before spending anything on
  it. The honest check is not a raw packet at all: run `03-create-share.ps1`,
  which is idempotent and asserts the setting directly, or read
  `Get-SmbServerConfiguration` in the guest. A host-side check would need to
  capture what the kernel `cifs` client actually puts on the wire during a real
  mount and copy that, rather than being written from the specification.

  The D33 half of I-008 is unaffected and was re-verified after the restart:
  `docker inspect` still lists only `/dev/kvm` and `/dev/net/tun`, and the guest
  QEMU command line still has no `vhost=on`. Docker applies a device list only
  when it **creates** a container, so a restart — even across a host reboot —
  cannot pick it up. That is exactly why the new checks ask the container and
  QEMU rather than the host.
- Nothing else. The remaining reviewed candidates are I-009 to I-011 above; the
  ideas rejected outright are R-025 onward below.

**Verified here:** `make check`, `make test-all` (517 with PySide6, 452 without),
`make lint-ps` (all ten `.ps1` files parse), `make test-ps`, `make deb`, and the
new acceptance checks run against the live container. **Not verified here:** the
agent has still never been *executed* — the serializer figures come from
PowerShell 7 on Linux against synthetic data, and Windows PowerShell 5.1 remains
an inference. One note for whoever sees it next:
`test_a_matching_record_resumes_provisioning_without_any_cifs` flaked once under
load and passed on rerun and in isolation; its `pump(2.0, until=...)` deadline is
timing-sensitive.

### 2026-07-26 — Reviewed follow-up work: skew, backup, diagnostics, progress

The reviewed backlog recorded in
[`todo/archive/further_improvements.md`](todo/archive/further_improvements.md)
landed as the
commits below. None of it is tagged; see I-007 for why it carries no release
number of its own.

- **Live acceptance record** (`46c83c0`). `docs/acceptance-results.md` now exists
  with a row per live check, an environment baseline, and the rules that a
  failure becomes a fix or a stated limitation, that history is appended rather
  than overwritten, and that evidence never carries operator data. It ships in
  the package. Every row is `not yet run`: filling one in needs the real host.
- **Lifecycle reducer** (`4d58f03`). The D29–D31 state machine moved out of Qt
  callbacks into a pure `lifecycle.py` reducer, with an exhaustive transition
  table test and a new offscreen PySide6 wiring suite. Behavior-preserving: no
  plan contract changed. Stale worker completions are now dropped by an
  operation token instead of ad-hoc state checks.
- **Protocol and agent-build skew** (`da62339`, v2 plan D35). All three bridge
  document kinds carry `"version": 1`, `status.json` carries `agentBuild`, and
  the GUI reports skew with a copyable "re-run script 04" banner. An unsupported
  or not-yet-verified protocol fails closed: Apply and list requests are refused
  and `exclusions.json` is left untouched.
- **Selective-sync backup and restore** (`693cf64`, v2 plan D36). A mode-0600
  host-side snapshot is written after every validated read and Apply, and a
  rebuilt VM's empty revision-0 config can no longer overwrite it. Recovery is an
  explicit, previewed **Restore from backup…** rather than an assumption that the
  operator kept their own copy.
- **Diagnostic report** (`01fb4ef`, v2 plan D37). **Copy diagnostics** and **Save
  diagnostic report…** build a report from an allowlisted fact set, so a field
  nobody deliberately copied in cannot leak. Folder names are placeholders unless
  opted in; secrets, environments and file contents have no opt-in at all. It
  works in every lifecycle state, including the ones with no mount.
- **Progress and interrupted first runs** (`6c7aaba`, v2 plan D38/D39). Long
  transactions now show elapsed time and the helper's own streamed `==> ` phase
  lines — no new IPC channel, no fake percentages, no cancel button. An outer
  timeout enters a quiesced `transition_unknown` state instead of resuming
  polling against shares that may already be gone, and a first run interrupted
  mid-install resumes its no-CIFS provisioning state instead of guessing.

Everything above is proven by `make check` and `make test-all` only. Real
KVM/Windows/CIFS/tray behavior — including the new E12–E15 checks — still
requires I-001.

### 2026-07-26 — In-session lifecycle, first run, usability and performance

Commit `26d29ac` implemented the eight-item
[`todo/archive/gui_improvements.md`](todo/archive/gui_improvements.md) review
and the performance
review now locked as v2 decisions D30–D34.

- Added explicit **Power off bridge** / **Start bridge** controls without
  quitting the GUI, including recovery from a definitively stopped container.
  Health colour alone never authorizes a lifecycle mutation.
- Added a no-CIFS **Setup required** state, deterministic resource-bundle
  discovery, host/env readiness checks, confirmed VM creation, and a separate
  long-running Windows-provisioning handoff.
- Pinned GUI and helper Docker calls to the native Engine socket and made
  absent-container matching work across Docker's capitalization changes.
- Added incident/recovery notifications, selective-sync filtering, retryable
  paged file listing, an honest logical-size summary, and visible CLI/UI version
  output.
- Reduced steady-state work with document and Docker-probe caches, safe agent
  walk elision and reclamation candidate reuse.
- Improved the data path with `/dev/vhost-net`, data-mount readahead, and
  deliberately disabled redundant SMB signing/sealing on the host-only
  transport (D32/D33).
- Reduced guest RAM and disk churn without touching the Store, servicing,
  WebView2, Cloud Files, Defender real-time protection, or update path.

The checkout gained pure tests for first-run checks, filtering, listing state,
notifications, size aggregation, command-line versioning, Docker targeting, and
power-helper error classification. Real KVM/Windows/CIFS/tray behavior still
requires I-001.

### 2026-07-25 — Reproducible build, package and configuration paths

Commit `f72b10d` added the Makefile entry points, unprivileged `.deb` build,
PowerShell lint entry point, package maintainer scripts, and one shared
`icloud-bridge-configure` path for source and package installs. Machine-specific
mount ownership and sudoers choices are recorded and replayed on upgrade rather
than being clobbered by package defaults.

### 2026-07-25 — GUI-managed bridge lifecycle

Commit `15c3079` implemented D29 from
[`todo/archive/gui_close_vm.md`](todo/archive/gui_close_vm.md).

- Confirmed Quit can quiesce GUI mount work, disarm health and automounts,
  cleanly unmount both shares, then gracefully stop the VM.
- A durable desired-off marker makes that state survive reboot; all six units
  are gated by the same marker.
- Busy mounts abort shutdown with the VM left running. Lazy/forced unmount,
  `docker kill`, and implicit shutdown on crash/logout/window close remain
  forbidden.
- Relaunch completes power-on and a real CIFS activation before any GUI bridge
  read. Autostart became an explicit user setting.

### 2026-07-24 — Selective sync and the first desktop app

Commit `5aed4e2` added the guest agent, private bridge protocol, ACL/ABE exclusion
enforcement, disk reclamation, bridge/data automounts, tray/status GUI, and
selective-sync UI.

The post-review fixes are part of the improvement: path and JSON handling fail
closed; provisioning refuses unsafe junction/symlink trees; exclusion denies
cannot be outranked by recursive `syncshare` grants; agent ACL repair is
ownership-independent and resumable; stale GUI edits survive tree rewrites; and
malformed/non-UTF-8 bridge files surface as errors instead of silently changing
selection.

### 2026-07-21 — Host checks and operator path

Commit `90115f3` added idempotent host prerequisite setup, loopback/mount/health
acceptance checks, and the first end-to-end operator quickstart.

### 2026-07-21 — Initial runnable bridge

Commit `e995088` turned the design into a runnable compose/provision/systemd
scaffold: loopback-only published ports, secret-bearing `.env` kept out of Git,
Windows provisioning scripts, the CIFS automount, and the health canary.
Commit `c60bc65` established the architecture and locked decision register.

The intervening `e20dc8f` commit only extended ignore rules and is omitted from
the improvement history.

## Visited ideas: closed

These ideas have already been reviewed. Re-open one only when its stated premise
has materially changed, and record that evidence here before implementation.

### Architecture, data safety and lifecycle

| ID | Idea | Why it is closed |
|---|---|---|
| R-001 | Replace the official Windows client with a native reverse-engineered iCloud client | Loses the trusted Apple-client behavior this project exists to preserve: ADP compatibility and long-lived sessions. See v1 D1/D2. |
| R-002 | Export a robocopy mirror instead of the live guest SMB root | One-way polling is not a safe bidirectional filesystem; host writes would not land directly in the Cloud Files sync root. Rejected by v1 D6. |
| R-003 | Add a host-side FUSE filtering layer | Adds an always-on custom filesystem daemon in the data path and still does not prevent the Windows guest downloading content. Rejected in v2 §0. |
| R-004 | Turn Files On-Demand off or globally pin with `attrib +P -U` | Live 2026-07-22/23 tests disproved the original premise: dataless placeholders hydrate on demand over SMB. Global pinning wastes disk and contradicts D14/D25. |
| R-005 | Hide exclusions only in the GUI or weaken NTFS deny/parent-guard/ABE enforcement | A hidden known path would remain readable, writable, deletable, renameable, or collision-prone. D15's server-side target deny, parent guard and ABE are one safety boundary. |
| R-006 | Automatically restart/power-cycle whenever health turns red | Red is not proof the VM is stopped; it also covers stale canaries, mounts and malformed JSON. D30 permits mutation only from explicit user action plus definitive lifecycle classification. |
| R-007 | Timed automatic power-on retries or implicit power-off on crash, logout, signal, `aboutToQuit`, or ordinary tray-window close | These actions can fight maintenance or interrupt work without consent. D29/D30 require explicit retry and explicit confirmed power-off. |
| R-008 | Lazy/forced unmount, `docker kill`, or stopping the VM before CIFS teardown | Risks detached stale mounts, interrupted file operations, and unclean guest shutdown. A busy mount must abort with the VM still running. |
| R-009 | Automate Apple sign-in/2FA or pass `SHARE_PASS` through the GUI/clipboard/guest argv | Risks account lockout or secret disclosure. Apple authentication and the guest share-password handoff remain manual by design. |
| R-010 | Publish VM/SMB/RDP ports beyond `127.0.0.1` | The guest contains an authenticated Apple session. The host is the security boundary (D9), and acceptance tests enforce loopback-only bindings. |
| R-011 | Build a custom Windows ISO | Adds a Windows image maintenance/security burden; the project deliberately uses a stock image plus idempotent runtime provisioning (D3). |
| R-040 | Add a host->guest execution channel — QEMU guest agent virtio-serial plus guest-side `qemu-ga` (SYSTEM-level `guest-exec`), or WinRM/SSH with a stored admin credential — so the host can drive the guest directly instead of only bootstrapping the watcher by hand | Operator-rejected 2026-07-28. Every such channel widens the host's power over a guest holding a live Apple session, turning the deliberately pull-only surface (the guest fetches from the host's shares; the host executes nothing in Windows and holds only the low-privilege share credential) into one where the host can execute code, or holds an admin credential, inside that session. That cost is not hypothetical: the entire 2026-07-27 fix chain (see "Shipped improvements") was diagnosed and shipped end to end over the existing pull-only surface, which is direct ROI evidence against adding a channel. The 2026-07-28 watcher-presence beacon (`2214697`) further narrows the gap a channel would close, by detecting a missing watcher before staging instead of discovering it by timeout. And a channel installed at OEM time can only be relied on by VMs whose OEM step already worked — the same class whose watcher registration already works — so its marginal value is repairing broken watchers on established VMs, not first bootstrap. Monitor-socket keystroke injection was rejected out of hand within the same evaluation: blind typing into whatever has focus. Full evaluation in `todo/archive/lifecycle-dead-ends.md`. |

### Performance and guest footprint

The 2026-07-26 whole-path review in
[`docs/plan-gui-selective-sync.md` §8.1](docs/plan-gui-selective-sync.md#81-performance-and-resource-posture-review-of-2026-07-26)
closed the following:

| ID | Idea | Why it is closed |
|---|---|---|
| R-012 | Windows LTSC/Enterprise | LTSC lacks the Store required by the locked iCloud install/update path; Enterprise offers no useful reduction over the debloated Pro guest. |
| R-013 | Add QEMU `ARGUMENTS` for Hyper-V enlightenments | dockur/qemus already applies the relevant Windows enlightenment and timer settings upstream. |
| R-014 | `DISK_CACHE=writeback` | Trades NTFS crash consistency for throughput the loopback workload does not need. |
| R-015 | `ALLOCATE=Y` disk preallocation | Consumes the entire virtual disk immediately and defeats the sparse/discard posture without a measured benefit. |
| R-016 | Container `mem_limit` | A cgroup OOM kill would bypass every graceful D29/D30 teardown and marker guarantee. |
| R-017 | Enable virtio ballooning or hugetlbfs | Ballooning is an unvalidated behavior change; hugetlbfs permanently pins RAM and requires unsupported QEMU argument surgery. Transparent huge pages already cover the normal case. |
| R-018 | SMB multichannel | One NATed virtio NIC provides no independent reachable channels; it adds complexity without removing the actual copy path. |
| R-019 | Disable NTFS last-access updates | D26 uses last access for coldest-first reclamation; disabling it silently degrades eviction ordering. |
| R-020 | Disable ScheduledDefrag | On the SSD-presented guest it performs retrim, returning freed blocks to the sparse qcow2 image. |
| R-021 | Disable tablet/text-input services | Breaks keyboard input in the Windows 11 surfaces required for Store/iCloud sign-in. |
| R-022 | Disable WNS, memory compression, or Defender real-time protection | The small savings do not justify breaking Store notification plumbing, increasing pagefile I/O in a 3 GB guest, or weakening a machine with a live Apple session. |
| R-023 | Cache `exclusions.json` containment validation across passes | Parsing is cheap; the containment walk is the runtime check that detects a formerly safe path whose parent became an unsafe reparse point. |
| R-024 | Lengthen only one part of the health-canary cadence | The timer, script freshness threshold and GUI threshold are coupled. A partial change creates false stale reports; the current cadence favors faster hung-guest detection. |

### Closed by the live-host review of 2026-07-26

These are the first rows in this table closed against **measurements from a
running guest** rather than reasoning alone, which is why several of them close
by showing the ceiling is too low to bother with. The measurements are in the
shipped entry above.

| ID | Idea | Why it is closed |
|---|---|---|
| R-025 | Optimize or bypass `docker-proxy` on the SMB data path | `SETUP.md` already states the mechanism and names the benchmark, and the proxy on the SMB port had used 00:00:00 of CPU in 95 259 s of uptime because no CIFS mount has ever existed on this host. Routing around it by mounting the container IP stays forbidden by hard rule 3, R-010 and acceptance section 3. |
| R-026 | Change the guest disk model (`DISK_IO`, `DISK_CACHE`, `ALLOCATE`) | The QEMU block iothread used 54 s of 23 800 lifetime core-seconds — 0.23% — and the sampled idle window did 0.7 KiB/s of reads. Any disk-model change is provably capped at a quarter of one percent. Adds the measurement R-014 and R-015 lacked; supersedes neither. |
| R-027 | Add more inbox apps to `$bloat`, or set Edge `BackgroundModeEnabled=0` | Legal under hard rule 5's inbox-only carve-out, but the measured residual is episodic on a multi-minute scale rather than a resident constant, so not one byte of the 18% is attributable to them. Micro-tweaks without a numerator. |
| R-028 | Disable `ProactiveScan` or Automatic Maintenance | Maintenance is the umbrella that runs the retrim R-020 exists to protect, and it is an episodic daily event, not a sustained load. |
| R-029 | Switch the guest to the High Performance power scheme | Plausible on bare metal, but with `+hypervisor` and `hv_passthrough` Windows cedes P-state management to the hypervisor. No measurable mechanism in this configuration. |
| R-030 | Remove `usb-tablet` / `qemu-xhci` to cut idle vmexits | dockur-supplied, would need the `ARGUMENTS` surgery section 8.1 forbids, and would break the VNC mouse input Apple sign-in needs. Empirically moot: QEMU userspace non-guest time is ~0.15% of a core at idle. |
| R-031 | Lengthen the agent's 2 s tick, the 15 s `Get-Process` in `Write-Status`, SMB `echo_interval`, or the GUI's per-tick mount stats | Each measures between 0.01% and 0.3% of a core — three to four orders of magnitude below the measured idle burn. D17 locks the tick cadence independently. |
| R-032 | Stream `tree.json` straight to disk instead of building the node tree in memory | Each node's rolled-up totals precede its `dirs` array in the required key order, so streaming needs a key reorder or a two-pass build. Not worth the churn now that the `StringBuilder` serializer has removed the O(size x depth) recopying. |
| R-033 | Skip the `tree.json` write when the serialized bytes are unchanged | `generatedAt` is what D23's tree-staleness rule reads, so an unchanged document must still be re-stamped. Splitting freshness into a separate `walkedAt` field is DFR-002's precondition, not an independent win. |
| R-034 | Merge the health and D30-classification `ContainerProbe` instances into one `docker inspect` | Saves ~0.13% of a core, and only while health is red. D34 specifies one probe per consumer; not worth an amendment. |
| R-035 | Populate tree items lazily on expand instead of materializing every directory | The ~3.9 KiB of host RSS per directory is real, but `_filterable_paths` iterates materialized items and the filter's contract is "folders plus files loaded this session". A design change, not a tuning change, and it needs the operator's real directory count first. |
| R-036 | Reduce `RAM_SIZE` from the live 4G toward D10's 3G | cgroup memory is already 3.49 GiB against a 4 GiB guest, and R-022's own reasoning keeps memory compression *because* of pagefile I/O in a small guest. Shrinking trades CPU for exactly the I/O R-022 protects. `.env` is operator machine state and D10's figure is a floor, not a cap. |
| R-037 | Replace `os.path.ismount` with a `/proc/self/mountinfo` scan in the GUI's health gather | Removes two CIFS round trips per tick and would not block on a sick mount, but the canary `stat` in the same gather still hangs, so the responsiveness gain is partial and the CPU gain is seconds per day. |
| R-038 | Add an automated check that the running `-smp`/`-m` match D10's literal values | It passes on the live 4-vCPU configuration it was meant to catch, and keyed to D10's literal 2 it would fail every operator who legitimately sized up — contradicting plan section 3 and `SETUP.md`, which make these operator values. A MANUAL note is the correct form. |
| R-039 | Reduce `CPU_CORES` from 4 as a compliance fix | Not a compliance question at all: D10's "2 vCPU, 3 GB" is a **measured floor**, plan section 3 classes `CPU_CORES` as an operator value from `.env`, and `.env` is gitignored machine state. Worth *measuring* during the I-008 container recreate, but with no predicted win — the per-vCPU spread (143/86/85/79 min) is a boot-CPU-heavy profile, and the plausible idle consumers redistribute under fewer vCPUs rather than vanishing. |

Two adjacent concerns cleared rather than closed, recorded because they are
otherwise written down nowhere. `rasize=16777216` does **not** over-hydrate: the
kernel's readahead ramps from a small initial window and only reaches `ra_pages`
on sustained sequential reads, so a thumbnailer reading 64 KB of a dataless
placeholder does not pull 16 MiB — which is D33's stated intent. But **a desktop
file manager thumbnailing `/mnt/icloud` will hydrate real content**, and at the
time this was written no entry here or in the docs warned about it. The
2026-07-27 entry above closes that gap in `README.md`, `SETUP.md` and
`docs/selective-sync.md`. Separately, `restart: unless-stopped` is
consistent with D29: an explicit `docker stop` from the power helper is not
restarted, including across a host reboot, so it cannot fight the marker.

## Visited ideas: deferred pending evidence

These are not rejected, but they are not implementation-ready.

| ID | Idea | Evidence required before reopening |
|---|---|---|
| DFR-001 | Replace per-file `attrib +U -P` processes with `SetFileAttributesW` | Amend D14/D26 and reproduce the §0.5 live test proving the native call causes iCloud/Cloud Files to dehydrate safely on the actual guest. |
| DFR-002 | Gate the ten-minute tree walk with a `FileSystemWatcher` dirty flag | Measure event rates on a live Cloud Files root first; provider metadata churn may make the watcher permanently dirty. Design a separate `walkedAt` signal so `tree.json` freshness remains honest. |
| DFR-003 | Install host-wide KVM/Docker/THP tuning | Benchmark first. `halt_poll_ns=0` and Docker `userland-proxy=false` are host-global and version-sensitive; THP is usually already suitable. Keep these operator choices, not installer mutations. |
| DFR-004 | Pattern exclusions, rename-following, per-item pinning/pre-warming, or hydration progress | These are explicitly outside v2. Each needs a separate design and live data-safety/performance evidence; none should be treated as an incidental GUI enhancement. |
| DFR-005 | Pause only iCloud sync while leaving the VM and mounts up | D30 controls the whole bridge, not the Apple client's private sync engine. Revisit only if Apple exposes a reliable supported control and observable queue state. |
| DFR-006 | Pin the container's vCPU threads to a CPU tier on a hybrid host | Measured on the author's i7-13700H (P-cores 0-11, E-cores 12-19, no cpuset): about 1 100 `se.nr_migrations` across four vCPU threads in 10 s. That is a numerator, not proof of waste — Linux's scheduler is capacity-aware and pinning can crowd helpers, hurt latency or force work onto power-hungry P-cores. Needs a controlled host-only override and a graceful lifecycle recreate, then unpinned vs P-tier vs E-tier compared on three ≥300 s idle profiles each, migration counts, cold boot to green, E0 cold hydration plus a warm transfer, and host responsiveness under another workload. A win becomes an optional operator benchmark in `SETUP.md`, never a compose default: this machine's CPU numbers are not portable. |
| DFR-007 | Build only the requested page of a folder listing in the guest agent | Every §2.4 page request enumerates the folder, creates a PowerShell object per file, sorts the whole list through a scriptblock comparator, then returns at most 1 000 of them; the next page repeats all four steps. Reusing `IcloudBridgeNative.SortByName()` on the `NativeEntry[]` and materializing only the requested slice would keep the OrdinalIgnoreCase-then-Ordinal order and the offset semantics while removing the comparator and most allocations. Needs the operator's real folder sizes first (I-001): with a few hundred files per folder there is nothing to win. Verify byte-for-byte name/order/`nextOffset` equivalence against the current comparator, plus the 999/1000/1001 page boundaries, before believing any speed number. |
| DFR-008 | Replace `Test-IsUnderAny`'s linear containment scan with a segment-aware matcher | The protocol admits up to 10 000 exclusion paths and the predicate runs per visited entry in the full scan, ACL reconciliation, sweep walk and list response, so the worst supported shape is ~10^9 prefix comparisons per pass; the GUI's `bridge.is_under()` and both antichain builders have the same shape. A per-configuration OrdinalIgnoreCase trie would fix it — a trie rather than a sorted-string predecessor search, because sibling punctuation can sort between an ancestor and its descendant and segment boundaries are security-significant. Measure realistic exclusion counts first: at ten roots this is invisible. Any implementation must be property-tested against the current predicate and must not touch D19 canonicalization or D22 containment safety. |

**Where the 2026-07-26 live-host review left these rows.** Both DFR-002 and
DFR-003 survived a deliberate attempt to close them, and the attempts are worth
recording so they are not repeated:

- **DFR-002 stays deferred verbatim.** Two independent proposals in that review
  converged on closing it with host-side CPU data, and neither goes near its
  stated condition, which is *watcher event rates on a live Cloud Files root*.
  Worse, the walk cannot be isolated from outside the guest even in principle:
  `$TreeEverySeconds`, `$SweepCooldownSeconds` and `$RequestTtlSeconds` are all
  600, and `Invoke-FullScan` carries up to 120 s of DACL reads, so any periodic
  hump is an upper bound on the whole ten-minute pass rather than on the walk.
  Measured 5 s-bucket noise was mean 0.927 core-s with sd 0.331 (CV 36%). A null
  result would not be understanding, and this table's own rule is that a row moves
  to Closed when the cost *is* understood.
- **DFR-003 does not move, but its THP leg is settled** by direct observation:
  transparent hugepages are already `madvise` on this host, so there is nothing
  to install for that third. The `halt_poll_ns` benchmark it demands is still
  unrun — and note the ceiling, which is new: halt polling spins inside `KVM_RUN`
  in kernel mode, so `halt_poll_ns=0` can only ever recover part of the host-kernel
  share, measured at ~5% of one core. `userland-proxy` is untouched and
  `tools/vcpu-profile.py` cannot see it, because `docker-proxy` is a separate host
  process outside the sampled QEMU process. The 2026-07-27 review re-derived that
  ceiling at **4.7% of one core** from an independent sample and left the
  benchmark itself still unrun; when it is run, compare at least three 300 s idle
  windows each way and check an interactive SMB latency and an E0 throughput
  figure alongside, so a CPU win is not quietly bought with a latency
  regression.
- **DFR-001, DFR-004 and DFR-005** were untouched by anything measured.

Photos, Passwords, Mail/Contacts/Calendar, Apple-session automation, and custom
Windows-image work remain outside this repository's scope rather than hidden
backlog items.
