# Live acceptance results

This file is the durable record of the **live** acceptance checks — the ones that
need the real KVM host, a running Windows guest, the CIFS mounts, systemd, a
desktop session and an Apple device. It holds results only. The checks themselves
are specified once, in the v2 plan:

- **E0** — the kernel-CIFS read/write gate, in
  [`plan-gui-selective-sync.md` Phase 0](plan-gui-selective-sync.md#phase-0--live-architecture-gate-existing-v1-system-no-v2-code-required).
- **E1 onward** — in
  [`plan-gui-selective-sync.md` Phase E](plan-gui-selective-sync.md#phase-e--v2-live-acceptance-tests-require-the-real-vm).

Do not copy a check's text into this file. Reference its ID so the plan remains
the single source of what the check is; this file only says what happened when it
was run.

**A result is filled in only from the real host.** That applies whether the
executor is the operator or an agent attached to that machine. The development
checkout has no KVM guest, no systemd instance, no CIFS mount, no tray and no
Apple device, so nothing `make check` or `make test-all` proves may turn a row
below into a `pass`. Repository tests are not evidence here.

## Rules for this file

**Failures.** A `fail` row must end in one of two places: a fix, recorded with
the commit that made it, or an explicitly worded accepted limitation that says
what does not work and what the operator should do instead. A `fail` that is
merely re-run until it passes is not resolved.

**History is appended, never overwritten.** A later run of the same check adds a
new dated row underneath the old one. Do not edit an earlier row to reflect a
newer result; the sequence of results is the useful part.

**Maintenance.** When an implementation adds a new Phase E ID to the plan, add
its initial `not yet run` row here in that same commit, rather than waiting for
somebody to run it.

**Privacy.** Evidence records versions, timings, states, counts and redacted
diagnostics. It never records host names, user names, Apple IDs, credentials,
exclusion paths, file or folder names, file contents, or any other operator data.
Where a check inherently concerns a specific path, refer to it as "the disposable
test folder" rather than naming it.

## Environment baseline

Baselines are **dated and appended**. When the environment changes, add a new
column or a new dated block below; never overwrite the versions that explain an
older result, because a regression is usually only legible against the versions
it regressed from.

| Fact | How to get it | Value | Recorded |
|---|---|---|---|
| Windows edition and build | guest: `winver`, or `Get-ComputerInfo \| Select WindowsProductName, WindowsVersion, OsBuildNumber` | | |
| iCloud for Windows version | guest: Settings > Apps, or `Get-AppxPackage *iCloud* \| Select Name, Version` | | |
| Docker Engine version | `docker --host unix:///var/run/docker.sock version --format '{{.Server.Version}}'` | | |
| dockur container image ID | `docker --host unix:///var/run/docker.sock inspect --format '{{.Image}}' icloud-windows` | | |
| dockur image repo digest | `docker --host unix:///var/run/docker.sock image inspect --format '{{json .RepoDigests}}' dockurr/windows` | | |
| Host kernel | `uname -r` | | |
| `cifs` module version | `modinfo -F version cifs`, or `in-tree <kernel>` when it reports nothing | | |
| Idle CPU cost | `./tools/vcpu-profile.py --seconds 300` with no operator activity (plan §11.3: ≤ 0.30 core-s/s) | | |
| Idle write churn | same run, the "container block I/O" line from cgroup `io.stat` (plan §11.3: ≤ 200 KiB/s writes) | | |
| Cold boot to green | time from **Start bridge** to a green tray | | |
| Cold 1 GiB hydration | `time sha256sum` of a dataless file of that size over the mount (E0 method) | | |
| Clean power-off duration | time from confirming power-off to the container being stopped | | |

## Results

Every row starts as `not yet run`. `Result` is one of `not yet run`, `pass`,
`fail`, or `accepted limitation`.

| Check | Date | Result | Evidence / notes |
|---|---|---|---|
| E0 | | not yet run | |
| E1 | | not yet run | |
| E1b | | not yet run | |
| E2 | | not yet run | |
| E3 | | not yet run | |
| E4 | | not yet run | |
| E5 | | not yet run | |
| E5b | | not yet run | |
| E6 | | not yet run | |
| E7 | | not yet run | |
| E8 | | not yet run | |
| E9 | | not yet run | |
| E10 | | not yet run | |
| E11 | | not yet run | |
| E11b | | not yet run | |
| E11c | | not yet run | |
| E11d | | not yet run | |
| E12 | | not yet run | |
| E13 | | not yet run | |
| E14 | | not yet run | |
| E15 | | not yet run | |
| E16 | | not yet run | |
| E17 | | not yet run | |
| E18 | | not yet run | |

## Remediation evidence outside the Phase E matrix

### 2026-07-28 — F2: the Windows idle CPU is attributed

Method: three 300 s guest-internal delta samples
(`tools/profile-windows-idle.ps1`, first-ever completed runs, elevated, WMI
`Win32_PerfRawData_PerfProc_Process`) concurrent with three 300 s host samples
(`tools/vcpu-profile.py`). Aggregate cross-checks closed (unattributed
remainder -0.98/-0.48/0.00 core-s), so the process rows account for what the
guest kernel charged to processes.

- Guest-internal non-Idle totals: **11.0% / 22.2% / 8.7%** of one core.
  Concurrent host view: 27.5% / 37.4% / 22.1% (guest share 82.2/87.6/80.6%,
  kernel 17.6/12.3/19.3%, QEMU ~0.2%). An untouched pre-run baseline the same
  hour (display asleep, no sampler): 15.4% / 21.0% / 24.4%. The host-minus-WMI
  gap is guest kernel/interrupt time no process owns, plus sampling overhead.
- **WindowsTerminal: 6.4 / 6.1 / 6.3% of one core, constant** — the largest
  single share. It hosts the provisioning watcher's console: the scheduled
  task passes `-WindowStyle Hidden`, but Windows 11's default-terminal
  delegation to Windows Terminal does not honor it, so a visible static
  console renders forever in a GPU-less VM. Disposition: **fix** (D51).
- **Bridge agent (`powershell`, watcher-launched): 0.4-0.5% baseline**, one
  window at **14.9%** containing a periodic full scan — consistent with
  I-012's measured scan timings against 69,620 entries. Disposition: accepted
  baseline; scan cost is P6/p3 follow-up territory.
- **iCloudDrive: ~0.5%** but with **~2.3 MB/s sustained disk reads** in two of
  three windows; **iCloudHome: 1.8%** in one window, near-zero after.
  Disposition: accepted (Apple's client, R-012 class); the read stream is
  worth watching if DFR-002 ever gets event-rate evidence.
- Defender (`MsMpEng` + core service): ~0.2-0.8%; `System` 0.4-0.8%; `dwm`
  0.1-0.3%; every dockur helper and remaining service row rounds to zero.
  Disposition: accepted (R-012/R-019-R-022 remain closed).

F2's completion gate stays open until the D51 fix lands and a re-measure
shows the WindowsTerminal share gone; the attribution itself is done.

### 2026-07-27 — I-008: D33 applied to the running container; D32 disproven

Executed on the author's live host. The bridge host setup (mounts, units,
power helper) has never been installed on this machine, so the D29 helper did
not exist to run; with no CIFS mounts and no units, the ordered teardown
reduces to stopping the container, done with a graceful `docker compose down`
(the 2-minute ACPI grace was honored and Windows shut down cleanly).

- **D33 applied.** `docker compose up -d` recreated the container;
  `docker inspect` now lists `/dev/vhost-net` and the guest QEMU command line
  carries `vhost=on,vhostfd=…`. `make acceptance` reports all three D33 checks
  `PASS` (host device, container device, `vhost=on`). The mount/unit/helper
  checks still `FAIL` because host setup was never installed here — that is
  I-001 territory, not new drift.
- **Throughput before/after: no measurable change at this scale.** Method: a
  20.0 MB already-hydrated file fetched repeatedly by userland `smbclient` in a
  throwaway container through the loopback published port; per-get rate from
  `smbclient`; n=12 each. Before (userspace virtio, guest up ~1.5 h at 20.0%
  idle): median 652.6 MB/s, min 399.6, max 699.3. After (`vhost=on`, guest
  settled at 16.2%): median 425.6 MB/s, min 167.3, max 753.0. The distributions
  overlap widely — 20 MB transfers finish in tens of milliseconds and are
  scheduling-noise-bound — so this records *no demonstrated win*, not a
  regression. The honest measurement remains E0's kernel-cifs read of a large
  file, which needs scripts 03/04 to have been run first.
- **D32 disproven host-side.** A real SMB client with
  `client signing=disabled` was accepted before the recreate and again after
  the cold boot. The server therefore does not require signing; the 2026-07-26
  raw-packet probe's four "signing required" readings were the probe's own
  artifact. The in-guest `Get-SmbServerConfiguration` confirmation folds into
  the next provisioning run.
- **Guest provisioning state discovered en route:** only the historical D5
  test share exists on this guest. `03-create-share.ps1` (the `icloud` share)
  and `04-bridge-agent.ps1` (the bridge share and agent) have never been run
  here, which is consistent with no CIFS mount ever having existed on this
  host.
- **Post-recreate idle:** 16.2% of one core over 120 s (67.6% guest / 2.9%
  QEMU / 29.6% kernel), container writes 14.5 KiB/s — both below the
  2026-07-26 pre-recreate figures (18.1-18.4%, ~66 KiB/s). Not attributed;
  could be the reboot rather than the device. With `vhost=on` the virtio
  worker appears as a `vhost-<pid>` thread row in `tools/vcpu-profile.py`
  (0.45 core-s during a 60 s 40-read burst), so its cost stays visible to the
  profiler on this kernel (7.0.0 `vhost_task`).
- **Not measured:** `CPU_CORES` stayed at the operator's 4 (R-039 predicted no
  win and `.env` is operator state; changing it was not this session's call).

## Safe Workspaces acceptance matrix (A1-A14)

Safe Workspaces (register **D52**, design in
[`plan-safe-local-workspaces.md`](plan-safe-local-workspaces.md)) has its own
live acceptance matrix, specified once in that plan's
[section 15](plan-safe-local-workspaces.md#15-live-acceptance-matrix) as
A1-A14. As with the Phase E rules above, this section records only what
happened when a check was run; it references scenario IDs rather than
repeating their expected results.

**No A-scenario has been executed.** This checkout has no Windows guest, no
CIFS mount, no Obsidian, and no Apple device wired to it, so nothing here can
read `pass` yet. Every row is `unverified`, and that is an honest zero rather
than partial credit.

### Environment baseline

| Fact | Value | Recorded |
|---|---|---|
| Package version | 0.3.0 | 2026-07-29 |
| Unison version available on this host | 2.53.8 (ocaml 5.3.0) | 2026-07-29 |

### What is proven locally, and what is not

Covered by integration tests that exercise the real Unison binary (2.53.8 on
this host): the exact Unison invocation, conflict retention with both
diverging versions left intact on their own replicas, ten retained central
backups, deletion propagation, and metadata-only touches that replace no
content. Covered separately by the unit and Qt-wiring suites: cycle gating,
scheduling, the drain gate, health-status precedence, and diagnostics privacy.

None of that is evidence about the live Windows guest, real CIFS behavior,
Obsidian, or a second Apple device. Only running the rows below against those
can turn them from `unverified` into anything else.

### Results

| Check | Disposition | Executing it requires |
|---|---|---|
| A1 | unverified | a disposable copy of the observed vault, Obsidian closed, and an empty local workspace root |
| A2 | unverified | hash captures of both replicas, then a Windows/iCloud metadata-only change (no byte change) held across two cycles |
| A3 | unverified | Obsidian open against the disposable local vault, with at least two minutes of continuous typing |
| A4 | unverified | a single local note edit in the disposable local vault, observed through one stability window |
| A5 | unverified | a second Apple device (Mac or iPhone) editing a different note in the disposable remote vault |
| A6 | unverified | the same note edited differently on Linux and an Apple device before either side converges |
| A7 | unverified | deliberate loss of the guest's external networking during a local edit, then reconnection |
| A8 | unverified | triggering bridge power-off mid-cycle and observing the quiesce, drain, and unmount sequence |
| A9 | unverified | quitting only the GUI, editing locally while it is closed, and restarting it |
| A10 | unverified | killing the app mid-cycle and relaunching it |
| A11 | unverified | an ordinary deletion followed by a restore from its central backup |
| A12 | unverified | an endpoint made empty, and separately a burst of at least 20 paths and 20 percent of an endpoint deleted |
| A13 | unverified | `make reinstall` run against the disposable workspace's configuration and state, then confirming survival |
| A14 | unverified | only after A1-A13 pass: closing the raw vault in Obsidian, creating the real local workspace, validating counts and hashes, then reopening it as the vault |

### Prerequisites and ordering for the operator's run

1. Start from a **disposable copy** of the observed vault, not the real one,
   and keep an **independent backup** of that copy before touching it.
2. Run A1 through A13 against the disposable copy only. Do not open the real
   vault through `/mnt/icloud` while initializing or testing a workspace.
3. Do not cut the real vault over (A14) until every disposable-vault scenario
   (A1-A13) has an actual `pass` recorded, not merely an attempt.
4. Refresh the installed host package with `make reinstall` before starting,
   so the code under test matches this record's package version.
5. Record results in this table by appending the run's date and changing the
   affected row's disposition; do not overwrite this baseline entry — add
   beneath it, the same way the Phase E rows above are appended.
