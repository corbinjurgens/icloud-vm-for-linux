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

The repository has no release tags yet, and the GUI has reported version
`2.0.0` throughout its short history. The entries below therefore use dates and
commit IDs rather than inventing release boundaries after the fact.

Because nothing has shipped, **no entry here is a compatibility promise**. Until
the first tag, a change is free to break any format, protocol or interface
outright (`AGENTS.md` hard rule 9); what an entry records is what changed and
why, plus the operator step — usually re-running `04-bridge-agent.ps1` — that a
break requires. Preserving the operator's own state (exclusions, credentials,
the VM disk, the synced files) is a separate and non-negotiable obligation.

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
the security, lifecycle, Files On-Demand, and no-secret contracts in `AGENTS.md`.

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

### I-007 — Establish release boundaries

**Status:** Ready; blocked on live acceptance  
**Evidence:** The reviewed backlog in
[`todo/further_improvements.md`](todo/further_improvements.md) has landed, so
there is a coherent body of work to name. The release is chosen: **2.1.0**. What
is missing is the evidence, not the decision — every row in
[`docs/acceptance-results.md`](docs/acceptance-results.md) is still
`not yet run`, and a development checkout is structurally unable to change that.

`__version__` in `gui/icloud_bridge_gui/__init__.py` therefore still reads
`2.0.0`. Bumping it to `2.1.0` is a one-line change, and it is the *only*
remaining step; `Makefile` and `packaging/build-deb.sh` already derive from that
single source.

**Completion gate:** the applicable E0–E15 rows are recorded on the real host as
`pass` or an explicitly approved accepted limitation; then bump `__version__` to
`2.1.0`, add the release entry here mapping it to those rows, and confirm
`make version`, `icloud-bridge-gui --version`, the built package filename and
`dpkg-deb -I` all agree. Tagging `v2.1.0` remains the operator's call and
happens after that, never before.

## Shipped improvements

### 2026-07-26 — Reviewed follow-up work: skew, backup, diagnostics, progress

The reviewed backlog recorded in
[`todo/further_improvements.md`](todo/further_improvements.md) landed as the
commits below. None of it is tagged; see I-007 for why the version still reads
`2.0.0`.

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

## Visited ideas: deferred pending evidence

These are not rejected, but they are not implementation-ready.

| ID | Idea | Evidence required before reopening |
|---|---|---|
| DFR-001 | Replace per-file `attrib +U -P` processes with `SetFileAttributesW` | Amend D14/D26 and reproduce the §0.5 live test proving the native call causes iCloud/Cloud Files to dehydrate safely on the actual guest. |
| DFR-002 | Gate the ten-minute tree walk with a `FileSystemWatcher` dirty flag | Measure event rates on a live Cloud Files root first; provider metadata churn may make the watcher permanently dirty. Design a separate `walkedAt` signal so `tree.json` freshness remains honest. |
| DFR-003 | Install host-wide KVM/Docker/THP tuning | Benchmark first. `halt_poll_ns=0` and Docker `userland-proxy=false` are host-global and version-sensitive; THP is usually already suitable. Keep these operator choices, not installer mutations. |
| DFR-004 | Pattern exclusions, rename-following, per-item pinning/pre-warming, or hydration progress | These are explicitly outside v2. Each needs a separate design and live data-safety/performance evidence; none should be treated as an incidental GUI enhancement. |
| DFR-005 | Pause only iCloud sync while leaving the VM and mounts up | D30 controls the whole bridge, not the Apple client's private sync engine. Revisit only if Apple exposes a reliable supported control and observable queue state. |

Photos, Passwords, Mail/Contacts/Calendar, Apple-session automation, and custom
Windows-image work remain outside this repository's scope rather than hidden
backlog items.
