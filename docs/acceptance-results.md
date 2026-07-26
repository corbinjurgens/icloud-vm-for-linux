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
