# Todo: Performance and resource review, 2026-07-27

> **Status as this review closed (2026-07-27, morning): the checkout-executable
> findings have shipped; the rest need the operator and a provisioned guest.**
> The review itself changed no VM, guest, host, or application setting. The live
> VM was intentionally powered off through the bridge lifecycle while the
> read-only measurements were in progress, so it was left off and no later live
> test was attempted that day. `CHANGELOG.md` is the durable ledger and now
> carries every item below.
>
> - **Shipped** (see "A second read-only review" under 2026-07-27 in
>   `CHANGELOG.md`): **P1**, the
>   content-preview hydration warning in `README.md`, `SETUP.md` §10 and
>   `docs/selective-sync.md`; **P2**, the GUI response poll armed on demand
>   instead of ticking for the whole session; and the read-only sampler **F2**
>   asked for, `tools/profile-windows-idle.ps1`, which is written and parsed but
>   has never run on Windows.
> - **Recorded, not implemented:** P5 became `DFR-006`, P3 became `DFR-007`, and
>   P4 became `DFR-008`; P6 remains the tail of `I-009`. Each needs a numerator
>   this workspace cannot produce.
> - **Still entirely the operator's:** F1 (provision the guest — folded into
>   `I-001`, which now carries its four acceptance conditions), F2's actual runs
>   and F3's PowerShell 5.1 proof (`I-010`, `I-009`), and F4's `halt_poll_ns`
>   A/B (`DFR-003`).
>
> **Update, 2026-07-27, later the same day: the premise has expired.** The guest
> was provisioned through the app-driven path, and the agent — build 7 after
> that day's fix chain — now publishes `status.json` and completes scans of the
> real library: a first full pass of **60,154 entries ending `lastError: none`
> in 29 s**. See the 2026-07-27 shipped entries in `CHANGELOG.md` (the
> long-paths entry, `agentBuild` 5 → 6, carries those figures; the
> liveness-probe entry, 6 → 7, is the last build change), and `I-012` for the
> same app-driven run's inspection timings.
>
> Nothing below is written against an unprovisioned guest any more: the
> F-items now have obtainable numerators, and what separates them from done is
> running the measurements and recording them, not waiting for the guest.

## Goal

Follow up the 2026-07-26/27 live-host findings, find remaining work that can
materially improve performance or reduce recurring CPU, memory, network, or disk
use, and avoid reopening already-settled unsafe tuning ideas.

The main conclusion when this was written was **do not optimize around an
unprovisioned guest**: the VM had never run `03-create-share.ps1` or
`04-bridge-agent.ps1`, so it could not produce the kernel-CIFS E0 result, a real
agent scan duration, exclusion costs, or a representative GUI tree.

**That premise expired later on 2026-07-27**, when the app-driven provisioning
ran and the agent began scanning the real library. What survives is the reason
behind it, now as an instruction rather than a blocker: those measurements have
much more value than another speculative QEMU or Windows debloat setting, and
they are finally takeable.

## Live snapshot

Read-only observations on 2026-07-27, before the bridge was powered off:

| Area | Observation | Consequence |
|---|---|---|
| Idle CPU | `tools/vcpu-profile.py --seconds 60`: 11.47 core-seconds / 60 s = **19.1% of one core** | Consistent with the earlier 16–18% samples; this short run is evidence, not the ≥300 s acceptance baseline |
| CPU split | guest 8.45 s (73.7%), host kernel 2.83 s (24.7%), QEMU 0.19 s (1.7%) | Windows attribution remains the first performance task; all host-side tuning together is capped at **4.7% of one core** in this sample |
| Idle I/O | 0.4 KiB/s container reads, 18.5 KiB/s writes | Below the 200 KiB/s acceptance ceiling and below the earlier 66 KiB/s sample |
| QEMU helpers | `docker-proxy`, nginx, websocketd, dnsmasq, Samba, wsddn, and dockur shell helpers each rounded to 0.00% CPU over a 10 s `pidstat` sample | No idle saving exists in deleting or bypassing these maintenance paths |
| Memory | cgroup `memory.current` 4,368,961,536 bytes; about 4.34 GB was anonymous; no cgroup OOM, throttle, or swap event | The 4 GB guest is genuinely resident, but this 61 GiB host had 43 GiB available and no swap use; ballooning would solve no present pressure |
| Disk path | QEMU 10.0.11 uses a sparse raw image with `cache=none,aio=native,discard=unmap,detect-zeroes=on` and a virtio-SCSI iothread | The safe high-performance/discard path is already active; there is no qcow2 or writeback-cache tax to remove |
| Disk allocation | `data.img` is 120 GiB logical and about **14.8 GiB allocated**; the storage filesystem is local ext4 on NVMe | Sparse discard is working |
| Disk fragmentation | `filefrag` reported 12,887 extents for `data.img` | Expected from repeated sparse allocation and hole punching; not a tuning target without measured guest-disk latency, especially after R-026 bounded the block path at 0.23% of lifetime CPU |
| Cached installer | the retained Windows ISO is 7.9 GiB, hard-linked between storage and `/srv/isos` | One physical copy, intentionally trading host disk for avoiding an 8 GB rebuild download; deletion is an operator trade-off already documented in `SETUP.md` §8 |
| Image version | the container is dockur/windows **v6.02**, revision `9caf1a9`, QEMU base image current for that release | v6.02 was also the current upstream release during this review; there is no upgrade gap to claim as a performance fix |
| vhost | QEMU command line had `vhost=on,vhostfd=...` | D33 remains applied |
| THP/KSM | host THP is `madvise`; KSM is enabled but shares zero pages | Matches the already-settled DFR-003 THP result; Windows guest memory is not currently deduplicated |
| Shutdown | the container received SIGTERM, completed ACPI shutdown in about 5 s, exited 143 with `OOMKilled=false`, and the powered-off marker appeared | This was a clean explicit lifecycle transition, not a crash; the VM was not restarted by this review |

Upstream references checked during this pass:

- [dockur/windows v6.02 release](https://github.com/dockur/windows/releases/tag/v6.02)
- [Linux KVM halt-polling documentation](https://www.kernel.org/doc/html/latest/virt/kvm/halt-polling.html)
- [QEMU KVM paravirtualized features](https://www.qemu.org/docs/master/system/i386/kvm-pv.html)

## Follow up first

### F1 — Provision this guest before drawing more conclusions

**Priority: highest; operator and guest desktop required.**

**Status, 2026-07-27 (later the same day): substantively achieved — the guest is
provisioned and scanning.** It went through the app-driven path rather than the
manual sequence below, and the agent (build 7) completed a first full pass of
the real library: 60,154 entries, `lastError: none`, 29 s. What remains open is
not the provisioning but the recorded confirmations listed below — a second
completed scan, the identity of the mounted data share, and the GUI check. The
sequence below stands as the manual route and as the record of what those
conditions were.

Run the existing first-run sequence rather than a special performance setup:

1. Run `C:\OEM\03-create-share.ps1` elevated with the intended share password.
2. Run `C:\OEM\04-bridge-agent.ps1` elevated. This installs the agent build this
   checkout carries.
3. Complete/reconcile the host setup and mounts through the documented D29–D31
   path.
4. Confirm the GUI sees protocol 1 / that agent build and no update banner.

Do not record a performance result until all of the following are true:

- `status.json` advances every 15 seconds with the checkout's `agentBuild`
  — **done**, at build 7;
- `scan.lastCompletedAt` becomes non-null, `scan.entries` is plausible for the
  real library, and `lastError` does not report `tree`, `scan`, or ACL failure
  — **done**, at 60,154 entries with `lastError: none`;
- a *second* full scan completes, proving the result was not only startup luck
  — **still open**. The ten-minute pass this note originally assumed was wrong
  by more than an order of magnitude: the real first pass took 29 s, so a second
  one costs nothing to obtain;
- the mounted data share is the production `icloud` share, not the historical
  `icloudtest` test share — **still open as a recorded confirmation**;
- the GUI shows protocol 1 with no update banner — **still open as a recorded
  confirmation**.

This was the most important prior discovery: until it was done, neither the old
agent nor either agent optimization had ever run against the real library. The
2026-07-27 provisioning closed that gap, and the long-path ACL failure it
immediately exposed is what a synthetic library would never have shown.

### F2 — Attribute the Windows idle CPU before changing Windows

**Priority: highest after F1; operator must watch the guest desktop.**

The 60-second sample again put 73.7% of QEMU CPU time in guest mode. Repository
code cannot name the Windows process behind it. The sampler this section asked
for, `tools/profile-windows-idle.ps1`, shipped on 2026-07-27 and **has still
never run**; it is read-only rather than a long ad-hoc command, and it must:

- take process CPU, working-set, private-byte, read-byte, and write-byte
  snapshots around a 300-second idle interval;
- report **deltas**, not lifetime totals;
- include `System`, Defender, iCloud, DWM, Service Host, Store/update, and the
  bridge agent without assuming any of them is the cause;
- record aggregate processor time as a cross-check so missing/exited processes
  are visible;
- write bounded plain text or JSON to dockur's `\\host.lan\Data` share;
- collect no command lines, environment, file paths, Apple identity, or file
  contents.

Run it three times after the guest has settled, alongside three matching
`vcpu-profile.py --seconds 300` host samples. A process is actionable only when
its delta is repeatable and large enough to explain a useful fraction of the
8–9 guest core-seconds per minute. Closed rows R-012 and R-019–R-022 still apply:
attribution to Defender, WNS, memory compression, ScheduledDefrag, or a required
input/servicing component records an accepted cost; it does not license
disabling it.

If process deltas leave a large unattributed `System` share, use a Windows
Performance Recorder idle trace as the next diagnostic. Do not jump from that
gap to another service-disable list.

**Completion gate:** named process/service shares account for the recurring
guest-mode CPU in `docs/acceptance-results.md`, with each material share fixed
or explicitly accepted.

### F3 — Close the remaining I-009 guest proof

**Priority: alongside F2.**

**Status, 2026-07-27 (later the same day): partially satisfied.** A real-library
pass has completed under the guest's Windows PowerShell 5.1 — the host the agent
actually runs on — at 60,154 entries in 29 s with `lastError: none`. Still open,
exactly as `I-009` records it: the `Join-Path`-versus-concatenation comparison
on the guest, `tools/test-agent-walk.ps1` under PowerShell 5.1, and first/second
scan durations written down as formal results rather than read out of a
changelog entry.

On Windows PowerShell 5.1:

- run `tools/test-agent-walk.ps1`;
- compare representative `Join-Path` results with `$Full + '\' + $Name` under
  the actual `C:\Users\icloud\iCloudDrive` root, including spaces, non-ASCII,
  and long paths;
- verify zero-entry, one-entry, and ordinary directories;
- record the first and second real full-scan durations and entry counts.

The test must prove path equivalence and a completed real-library pass. A
synthetic PowerShell 7 speed result is not enough.

### F4 — Run the already-designed `halt_poll_ns=0` A/B test

**Priority: lower than Windows attribution; root required.**

The live sample narrows the maximum possible saving to 2.83 core-seconds per
minute, or 4.7% of one core. Linux documents halt polling as an intentional
latency-for-CPU trade and confirms the module parameter is host-wide. Keep the
existing `SETUP.md` procedure and compare at least three 300-second idle windows
before/after, with an interactive SMB latency and E0 throughput check so a CPU
win is not bought with an obvious latency regression.

Do not install the setting from this repository. Revert to 200000 after the
experiment unless the repeated result is material and the operator explicitly
chooses the host-wide trade.

## New implementation candidates

These are findings, not approved decisions. Preserve every locked decision and
move an accepted item to the CHANGELOG before implementation.

### P1 — Warn that content previewers hydrate the library

**Value: potentially high network and guest-disk saving; documentation-only.**

`rasize=16777216` does not itself over-hydrate a small read, but a file manager
thumbnailer, media metadata extractor, indexer, backup scanner, or checksum tool
that opens content under `/mnt/icloud` causes genuine Cloud Files hydration.
The CHANGELOG notes the thumbnail case, but `README.md`, `SETUP.md`, and
`docs/selective-sync.md` do not warn the operator.

Document:

- directory enumeration and metadata reads are safe;
- thumbnails, previews, media probing, checksums, and content indexing are real
  reads and may download content;
- disable previews for this network mount if the desktop offers that setting;
- do not mutate GNOME/KDE/indexer preferences automatically, since they are
  desktop-wide user policy.

This can avoid gigabytes of accidental transfer and cache growth, which dwarfs
the remaining host micro-tuning ceiling.

**Completion gate:** the warning is visible in setup and daily-use docs and
does not claim metadata alone hydrates files.

### P2 — Stop the GUI's response timer when there is nothing to poll

**Value: small but certain idle wakeup reduction; low risk.**

`MainWindow` starts a 1-second `_poll_timer` unconditionally at construction and
keeps it running for the whole tray session. In the common case
`FolderRequests.pending_ids()` is empty, so the callback wakes 86,400 times per
day only to iterate an empty list.

Arm it when `dispatched()` records the first request; stop it when there are no
pending requests and no response polls in flight. `resume()` should not start it
unless pending work exists. Quiesce/reload must still cancel/reset exactly as
today.

This does not change the one-second response cadence while a listing is active
and does not change D17's guest-agent tick.

**Verification:**

- Qt wiring test: zero timer callbacks during an idle interval;
- first request starts polling and receives a response;
- timeout, guest error, dispatch failure, reload, quiesce, and stale completion
  all leave the timer stopped once no request remains;
- a continuation page re-arms it.

**Completion gate:** identical request/timeout behavior with no steady-state
1 Hz timer.

### P3 — Page a listing without rebuilding every file object

**Value: medium for folders with thousands of files; no idle change.**

For every page request the guest currently:

1. enumerates the folder;
2. creates a PowerShell object for every file;
3. sorts the entire object list through a PowerShell scriptblock comparator;
4. returns at most 1,000 objects.

A second page repeats all four steps. Reuse
`IcloudBridgeNative.SortByName()` on the `NativeEntry[]`, skip directories in
that already-sorted stream, count file offsets, and create response objects only
for the requested page. This preserves the current
OrdinalIgnoreCase-then-Ordinal order and offset semantics while removing the
scriptblock comparator and most allocations.

Enumeration and sorting still repeat per page; introducing a persistent listing
snapshot would add invalidation/protocol semantics and is not justified yet.

**Verification:**

- byte-for-byte names/order/offset/`nextOffset` equivalence against expectations
  captured from the current comparator before the rewrite;
- mixed files/directories, zero/one entry, case ties, Unicode, and pages at
  999/1000/1001 boundaries;
- synthetic 100,000-file allocation/time comparison under PowerShell 7, then a
  Windows PowerShell 5.1 measurement on the guest — obtainable since the
  2026-07-27 provisioning, so this no longer waits on F1.

**Completion gate:** unchanged responses and a demonstrated large-folder
allocation or latency reduction.

### P4 — Replace repeated linear exclusion containment with a segment matcher

**Value: potentially high only for large exclusion sets; measure first.**

The protocol accepts up to 10,000 exclusion paths. `Test-IsUnderAny` linearly
checks every root and is called for entries in the full scan, ACL
reconciliation, sweep walk, and list response. In the worst supported shape,
100,000 visited entries and 10,000 independent exclusions imply up to one
billion prefix comparisons per pass. The GUI has the same linear
`bridge.is_under()` primitive in row-refresh paths, and both GUI/agent
antichain builders are quadratic in the number of retained roots.

Build one segment-aware, OrdinalIgnoreCase matcher per validated configuration
and reuse it for the pass. A trie is safer than a plain sorted-string
predecessor: sibling punctuation can sort between an ancestor and descendant,
and path-segment boundaries are security-significant. Keep exact-case strings
only for display; matching remains case-insensitive.

Do not build this before measuring realistic exclusion counts. A user with ten
folder roots will not notice it. Since the 2026-07-27 provisioning, that count
can be read from the operator's live `exclusions.json` and a real scan instead
of assumed: measuring is now the cheap step rather than the blocked one.

**Verification:**

- property tests comparing the matcher with the current simple implementation;
- case variants, Unicode, punctuation below `/`, ancestor/self/sibling
  boundaries, root refusal, and the 10,000-path limit;
- synthetic matrix for 10/100/1,000/10,000 roots and 100,000 queried paths;
- no change to D19 canonicalization or D22 containment safety.

**Completion gate:** identical answers and a measured large-set improvement;
otherwise close it as unsupported-scale theory.

### P5 — Benchmark hybrid-host CPU placement; do not ship affinity

**Value: unknown; host-specific deferred experiment.**

The i7-13700H host has P-core logical CPUs 0–11 and E-core CPUs 12–19. The
container has no cpuset. Twelve quick samples showed vCPU threads moving across
both tiers; `se.nr_migrations` increased by about **1,100 migrations across four
vCPU threads in 10 seconds**.

That is a numerator for an affinity experiment, not proof of waste. Linux's
scheduler is capacity-aware, and pinning can reduce flexibility, crowd helper
threads, worsen latency, or increase power by forcing work onto P-cores.

If tested, use a controlled host-only override and a graceful lifecycle
recreate. Compare unpinned, P-tier-only, and E-tier-only placement with:

- three ≥300 s idle CPU/I/O profiles per variant;
- vCPU migration counts;
- cold boot to green;
- E0 cold hydration and a warm transfer;
- host responsiveness during another workload.

Do not commit this machine's CPU numbers to compose. Close the idea if it does
not consistently reduce total core-seconds or materially improve E0 without a
regression. A win would become an optional operator benchmark, not an installer
default.

### P6 — Finish the two I-009 walk candidates only after real scan data

**Value: depends on exclusions; locked D34 amendment required.**

The earlier review already identified both:

1. skip per-entry DACL reads inside a validated excluded root, where a target
   deny removal is impossible; and
2. avoid walking an excluded subtree once for enforcement measurement and again
   for `tree.json`.

Do not implement either before F1 supplies scan duration and exclusion sizes.
F1 has since supplied the first half: a 2026-07-27 full pass of 60,154 entries
in **29 s**, which is a small budget to optimize and argues for measuring the
exclusion-walk share before amending a locked row for it. The exclusion sizes
are still missing, and both items remain worth nothing on a guest with no
exclusions configured.

For (1), amend D34 and retain the exact `$ConfigValid` gate; evaluate the skip
before the resume-cursor comparison. For (2), resolve the ordering/staleness
problem first: enforcement currently precedes the tree pass, so consuming the
tree measurement can make an `applied` label about 20 minutes stale unless the
loop is reordered or D34 explicitly accepts that interval.

**Completion gate:** a plan amendment states the recovery/staleness consequence,
walk ordering remains exact, and the real guest demonstrates a meaningful
duration reduction.

## Reconfirmed non-candidates

| Idea | Result of this review |
|---|---|
| More dockur/QEMU flags | Current QEMU already has KVM, Hyper-V passthrough, lost-tick discard, vhost, native async disk I/O, discard, and detect-zeroes. Upstream v6.02 is current. |
| Disk cache/preallocation/model changes | R-014/R-015/R-026 remain closed. The live command line is already the safe fast path and the block thread was previously 0.23% of lifetime CPU. |
| Smaller guest RAM | R-036 remains closed. The 4 GB guest is almost fully resident, but the host is not under memory pressure; shrinking would trade memory for compression/pagefile CPU and I/O. |
| Balloon/KSM/hugetlbfs | No current pressure or shared pages. Ballooning still needs Windows-driver/live validation and does not reduce Windows's own CPU. hugetlbfs would pin rather than save RAM. |
| Defragment/copy the sparse image | 12,887 extents are not a performance result. Copying or `e4defrag` adds data-safety and temporary-space risk for a path already shown negligible at idle. |
| Delete the cached ISO automatically | It would recover 7.9 GiB but force an 8 GB download on a clean rebuild. This is already an explicit operator choice, not recurring waste to silently remove. |
| Disable dockur helper daemons or Docker proxy | All rounded to 0.00% CPU in the live sample and are maintenance/provisioning paths. |
| More Windows debloat | Still lacks a named process numerator. F2 must come first; R-022's security and servicing boundaries remain locked. |
| Fewer vCPUs as a compliance fix | R-039 remains closed. Four vCPUs are an operator sizing choice, not drift from D10. P5 tests placement, not core-count reduction. |
| FileSystemWatcher gating | DFR-002 still needs event-rate evidence from the provisioned Cloud Files root and a separate honest `walkedAt`. |
| Native dehydration call | DFR-001 still needs a decision amendment and a live proof that `SetFileAttributesW` causes safe iCloud dehydration. |

## Recommended order

The order below is unchanged; what changed on 2026-07-27 is that step 1 is
mostly behind us, so steps 2 onward are unblocked rather than queued.

1. F1: provisioning is done (app-driven, 2026-07-27) and one real tree pass has
   completed. Obtain the second, and record the share-identity and GUI checks.
2. Run E0 and record the actual kernel-CIFS data-path baseline.
3. F2/F3: attribute Windows idle CPU and finish the PowerShell 5.1 proof of the
   agent rewrite.
4. P1: add the content-preview hydration warning.
5. F4 and P5: controlled host A/B tests, one variable at a time.
6. Use real scan/list/exclusion counts to decide P2–P6. P2 is safe regardless
   but intentionally low value; P3/P4/P6 need a numerator, and the guest can now
   produce one — which is a reason to go and measure, not a licence to skip the
   measurement.

## What this workspace could and could not verify

Verified read-only on the live host: container/QEMU command line, image version,
vhost state, cgroup CPU/memory/I/O, sparse allocation, host KVM/THP/KSM state,
hybrid CPU topology and migration counts, helper-process idle CPU, and the
graceful final shutdown state.

Not verified during this pass: Windows process attribution, Windows PowerShell
5.1 execution, the agent against the real library, production CIFS mounts, E0
throughput, exclusion costs, tree/list GUI scale, `halt_poll_ns=0`, or affinity
variants. The guest was not restarted after the powered-off marker appeared.

**Superseded later on 2026-07-27:** the guest was restarted and provisioned
through the app-driven path, and the agent (build 7) ran against the real
library, so the agent, the real library and Windows PowerShell 5.1 execution are
no longer unknowns. The remaining items on that list stand until the operator
runs them.
