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
tray, or Apple device. The v2 plan's E0–E11d checks are consequently still the
largest source of uncertainty.

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

**Completion gate:** E0 and every applicable E1–E11d case in
[`docs/plan-gui-selective-sync.md`](docs/plan-gui-selective-sync.md#phase-e--v2-live-acceptance-tests-require-the-real-vm)
has a dated result, with failures turned into fixes or explicit accepted
limitations.

### I-002 — Export a privacy-safe diagnostic report

**Status:** Candidate  
**Evidence:** The app now classifies lifecycle and health failures, but support
still requires the operator to collect details manually from several GUI rows,
Docker, systemd, the bridge JSON, and the journal. Long startup and mount
failures are especially hard to report after the fact.

Add **Copy diagnostics** and **Save diagnostic report** actions. A report should
contain the app/install version, lifecycle and container classification, marker
state, host-unit state, helper authorization result, health rows, bridge
document versions/timestamps, and bounded recent helper errors. It must never
read or include `.env`, `/etc/credentials-icloud`, `SHARE_PASS`, command
environments, Apple identity data, or file contents. Exclusion paths and other
filenames should be omitted by default and require a separate explicit opt-in.
Setup/powered-off reports must retain D31/D29's no-CIFS rule.

**Completion gate:** Qt-free collector/redaction tests prove secrets and
unbounded output cannot enter a report; package and per-user installs both
identify their origin; a real failure report is sufficient to follow the
matching `SETUP.md` recovery path.

### I-003 — Detect host/guest protocol and agent-version skew

**Status:** Candidate  
**Evidence:** `exclusions.json` has a checked version, but `status.json` and
`tree.json` are currently accepted as arbitrary JSON objects. A package upgrade
installs a newer `agent.ps1` into the host resource bundle but cannot replace
`C:\ProgramData\icloud-bridge\agent.ps1`; the operator must re-run
`04-bridge-agent.ps1` inside the guest. A newer GUI and older guest agent can
therefore coexist without a clear upgrade warning.

Add an explicit agent build/protocol version and capability set to status, then
validate the versions of status, tree, and list responses. The GUI should
distinguish “guest agent needs updating” from malformed data and refuse
incompatible writes fail-closed while preserving the current exclusion list.
The recovery action remains a copyable instruction to re-run script 04; the GUI
must not gain guest-admin credentials or silently update scheduled code.

**Completion gate:** compatibility tests cover older, current, newer, malformed,
and missing-version documents, and a live package upgrade demonstrates a clear
warning followed by recovery without losing exclusions.

### I-004 — Back up and explicitly restore selective-sync choices

**Status:** Candidate  
**Evidence:** iCloud is the canonical copy of file content, but
`exclusions.json` is unique configuration stored inside the otherwise
disposable VM. The existing fail-closed provisioning rule correctly refuses to
manufacture an empty config after loss, yet the documented recovery currently
assumes the operator already has a copy to restore.

After each successfully validated read or Apply, keep a mode-0600 snapshot in
the desktop user's XDG state directory, including revision and source metadata.
Offer an explicit restore preview that validates and canonicalizes the whole
list, shows additions/removals, and writes a revision higher than every observed
revision. Never interpret a missing or corrupt backup as an empty exclusion
list, and never restore automatically.

**Completion gate:** tests cover atomic backup, permissions, malformed and stale
snapshots, revision monotonicity, case-insensitive duplicates, and an
include-everything backup; a disposable-VM recovery preserves the selected
exclusions.

### I-005 — Show progress for long VM operations

**Status:** Candidate  
**Evidence:** VM creation may run for an hour and power-on may spend five minutes
retrying real CIFS activation. Both run off the GUI thread, but the operator
mostly sees a static busy message until the subprocess returns.

Show elapsed time and safe phase-level progress such as container creation,
Windows installation, guest boot, data-share readiness, bridge-share readiness,
and host services armed. Do not fake percentage completion, expose subprocess
environments, touch CIFS early, or add a cancel button that can interrupt the
D29/D30 transaction halfway through. Any helper progress channel needs a
bounded, stale-safe format tied to the serialized transaction.

**Completion gate:** the UI remains responsive through a deliberately slow
create/start, reports the phase that timed out, and preserves all rollback,
marker, and no-early-I/O behavior.

### I-006 — Extract lifecycle state and add thin Qt integration tests

**Status:** Candidate  
**Evidence:** The recent usability pass successfully moved reusable logic into
Qt-free modules, but `__main__.py` and `window.py` are now roughly 1,000 and
1,300 lines. The highest-risk D29–D31 orchestration still lives in Qt callbacks,
while the automated suite exercises model modules rather than complete
controller transitions.

Extract a pure lifecycle reducer and presentation model before adding more
states. Add a small optional PySide6/offscreen layer that verifies signal wiring
for startup-before-CIFS, failed power-off rollback, powered-off Quit, setup
gating, stale listing responses, and tray/no-tray close behavior. The ordinary
no-Qt suite must continue to pass and must not require a display.

**Completion gate:** the transition table is exhaustively tested without Qt,
the with-Qt run covers the wiring above, and no behavior or plan contract
changes during the refactor.

### I-007 — Establish release boundaries

**Status:** Candidate  
**Evidence:** Selective sync, lifecycle control, packaging, first-run setup, and
the performance pass all currently report the same `2.0.0` version. That makes
operator bug reports and package comparisons less precise even though
`__version__` is already the correct single source.

Before publishing another package, choose and document the first release
boundary, bump `__version__`, tag only after the live acceptance appropriate to
that release, and add the resulting version/date to this file. Do not
retroactively label untagged commits as releases.

**Completion gate:** the GUI, `--version`, `make version`, package filename and
package metadata agree, and the changelog maps the tag to its acceptance
evidence.

## Shipped improvements

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
