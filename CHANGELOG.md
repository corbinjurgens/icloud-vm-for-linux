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

**Completion gate:** E0 and every applicable Phase E case in
[`docs/plan-gui-selective-sync.md`](docs/plan-gui-selective-sync.md#phase-e--v2-live-acceptance-tests-require-the-real-vm)
has a dated result in
[`docs/acceptance-results.md`](docs/acceptance-results.md), with failures turned
into fixes or explicit accepted limitations.

### I-008 — Apply D32 and D33 to the running guest

**Status:** Ready; operator action only, no code  
**Evidence:** Measured on the author's live host on 2026-07-26. The container had
been running since 2026-07-25, and commit `26d29ac` — which added
`/dev/vhost-net` to `docker-compose.yml` for D33 and the signing assertions to
`03-create-share.ps1` for D32 — landed on 2026-07-26. Both were therefore
unapplied to the guest that was actually running, and **neither was detectable by
any check in the repository**:

- `docker inspect` listed only `/dev/kvm` and `/dev/net/tun`, and the guest
  QEMU command line carried a plain `tap,...` netdev with no `vhost=on`. Every
  SMB byte was being copied through QEMU's userspace main loop.
- An unauthenticated SMB2 NEGOTIATE to `127.0.0.1:10445` returned security mode
  `0x0003` — signing **required** — so `03-create-share.ps1` had not been re-run
  since that assertion was added.

`host/acceptance-tests.sh` tested `[ -e /dev/vhost-net ]`, a *host* fact, and
printed PASS while the container lacked the device. That gap is now closed (see
the shipped entry below); the remediation itself is still outstanding.

**What the operator runs**, in this order: power the bridge down through the D29
helper (never `docker rm`/`docker kill` a live bridge); `docker compose up -d` to
recreate the container with the device; then re-run `C:\OEM\03-create-share.ps1`
elevated in the guest. `make acceptance` then confirms the first half.

**Completion gate:** `make acceptance` reports the container device and
`vhost=on` checks green, and a before/after of `tools/test-smb-read.sh` is
recorded in [`docs/acceptance-results.md`](docs/acceptance-results.md). That
before/after is the only honest measurement of what D32 and D33 are worth, and it
needs `SHARE_PASS`, so it is the operator's to run.

### I-009 — Reduce the guest agent's per-entry walk cost

**Status:** Candidate, measured under PowerShell 7 on Linux  
**Evidence:** Follows the same review that produced the serializer work already
shipped below, and is the remainder of it. All four recursive walks call
`Join-Path` per entry (`agent.ps1` `Measure-SubtreeCheap`,
`Measure-ExclusionAllocation`, `Get-SweepCandidates`, `Build-Node`) — a
provider-aware cmdlet, measured at 31.4 us/call against 1.16 us for string
concatenation — and two of them additionally build a `List[object]` and sort it
with a **scriptblock** comparator per directory, ~54 us/entry at 20 entries per
directory. Aggregate ~84 us/entry, so ~8.4 s per pass at 100k entries.

Two further items from the same review, both **worth nothing on a guest with no
exclusions configured** and both requiring a locked row to be amended first, so
they rank behind the above:

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
can skip never-visited subtrees — proven by a fixture under `make test-ps`; and
path-concatenation equivalence to `Join-Path` is confirmed **on the guest**,
because a Linux host cannot prove it.

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
command to launch it, and have it write a two-sample `Get-Process` CPU **delta**
back as text — not blind keystrokes, and not lifetime CPU.

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

**Status:** Ready  
**Evidence:** The criterion reads "guest idles < 5% host CPU (check
`docker stats`)". Followed exactly as its own parenthetical instructs, `docker
stats` reports percent-of-one-core on Linux, so the author's guest reads ~18% and
this criterion **fails** — it does not pass either way. Nobody noticed because it
sits in the un-run MANUAL block and every row of `docs/acceptance-results.md`
still says `not yet run`. Make it absolute: core-seconds of container CPU per
wall second from the cgroup's `cpu.stat` over a stated window, which is what
`tools/vcpu-profile.py` now reports. Add the write-churn figure in the same pass.

**Completion gate:** the criterion names a measurement that can pass, an idle-cost
row exists in the environment baseline table, and `SETUP.md`'s performance
section and plan section 8.1 agree with it (they currently both say
`docker stats`, so they move together or they drift).

### I-007 — Establish release boundaries

**Status:** Ready; blocked on live acceptance  
**Evidence:** The reviewed backlog in
[`todo/further_improvements.md`](todo/further_improvements.md) has landed, so
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

- An automated SMB posture probe. The D32 finding above is real and was
  reproduced several times, but a checked-in tool that runs a raw
  unauthenticated NEGOTIATE proved unshippable. The layout every specification
  and implementation agrees on — 36 bytes of fixed fields, matching the Linux
  kernel's own `smb2_negotiate_req` — is answered by this guest only when the
  dialect array is written at offset 40 instead; and after roughly 25 abandoned
  handshakes the guest's SMB server began closing *every* connection for minutes
  at a time, including framings that had worked moments earlier. An acceptance
  check that can produce false failures, and that has to poke a live guest to
  run, is worse than none. The guest was verified healthy afterwards (container
  up, no restarts, CPU normal, web viewer and TCP fine). Explaining the framing
  — ideally by capturing what the kernel `cifs` client puts on the wire during a
  real mount and diffing — is open work. Until then the D32 check is the
  operator's, via `03-create-share.ps1` being idempotent.
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
[`todo/further_improvements.md`](todo/further_improvements.md) landed as the
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
[`todo/gui_improvements.md`](todo/gui_improvements.md) review and the performance
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
[`todo/gui_close_vm.md`](todo/gui_close_vm.md).

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
file manager thumbnailing `/mnt/icloud` will hydrate real content**, and no entry
here or in the docs warns about it. Separately, `restart: unless-stopped` is
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
  process outside the sampled QEMU process.
- **DFR-001, DFR-004 and DFR-005** were untouched by anything measured.

Photos, Passwords, Mail/Contacts/Calendar, Apple-session automation, and custom
Windows-image work remain outside this repository's scope rather than hidden
backlog items.
