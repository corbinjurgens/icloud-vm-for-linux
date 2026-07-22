# iCloud Drive on Linux via a Minimal Windows VM

Run Apple's official **iCloud for Windows** client inside a stripped-down
Windows 11 VM on a Linux host, and expose the synced iCloud Drive folder to the
host as a normal mounted directory at `/mnt/icloud`.

## Why this approach

Every native-Linux iCloud tool relies on reverse-engineered web APIs. Those
require Advanced Data Protection (ADP) to be **off** and suffer 30–60 day
session expiry. The official Windows client is a trusted Apple client:

- **ADP can stay on.**
- Sessions last **months**, not weeks.
- Apple maintains the sync engine — we inherit correctness, not reimplement it.

The cost is running a small Windows guest. We keep that cost low by building on
existing pieces instead of rolling our own (see below).

## How it works

```
┌─────────────────────────── Linux host ───────────────────────────┐
│                                                                    │
│  dockur/windows container  ──►  Windows 11 guest                   │
│    (KVM/QEMU, unattended         • iCloud for Windows (official)    │
│     install, VirtIO)             • Files On-Demand OFF (real bytes) │
│                                  • SMB share of the sync root       │
│                                       │                             │
│   /mnt/icloud  ◄── cifs mount ────────┘  (127.0.0.1:10445 → :445)   │
│    (systemd automount)                                              │
│                                                                    │
│   systemd timer: health check (mount + write-canary + freshness)   │
└────────────────────────────────────────────────────────────────────┘
```

The host writes land directly in the guest's Cloud Files sync root and upload
immediately — a live, bidirectional bridge, not a one-way mirror.

## Reused building blocks (don't reinvent these)

| Concern | We use | Instead of |
|---|---|---|
| Hypervisor | KVM/QEMU | Building VM tooling |
| VM lifecycle, unattended install, VirtIO drivers | [`dockur/windows`](https://github.com/dockur/windows) | A hand-rolled QEMU wrapper |
| Windows image | Stock Win11 ISO, debloated at runtime | A custom-built ISO |
| iCloud client | Apple's official iCloud for Windows | Reverse-engineered APIs |
| Host mount | kernel `cifs` + systemd automount | A custom sync daemon |

## Repository layout

```
.
├── docker-compose.yml     # the dockur/windows service definition
├── .env.example           # operator-specific values (copy to .env, gitignored)
├── provision/             # scripts run INSIDE the Windows guest
│   ├── 01-debloat.ps1
│   ├── 02-install-icloud.ps1
│   └── 03-create-share.ps1
├── host/                  # host-side setup, systemd units + health check
│   ├── setup-prereqs.sh   # install docker + cifs-utils, verify KVM (plan §1)
│   ├── setup-host.sh      # build credentials from .env, install mount + timer
│   ├── acceptance-tests.sh# host-side subset of the acceptance tests (plan §11)
│   ├── mnt-icloud.mount
│   ├── mnt-icloud.automount
│   ├── icloud-health.sh
│   ├── icloud-health.service
│   └── icloud-health.timer
└── docs/
    ├── implementation-plan.md        # full, authoritative build handoff (v1)
    └── plan-gui-selective-sync.md    # v2 plan: host GUI + tray icon, selective sync
```

## Quickstart

```bash
# On the Linux host:
sudo ./host/setup-prereqs.sh          # docker + cifs-utils + KVM check (§1)
# log out/in so docker works without sudo
cp .env.example .env && $EDITOR .env  # set DISK_SIZE / RAM_SIZE / CPU_CORES / SHARE_PASS
docker compose up -d                  # boots the guest; watch http://127.0.0.1:8006
```

Then, once the Windows desktop is up (debloat has already run unattended via the
`/oem` mount), do the one-time in-guest steps — details in the plan §5–§7:

1. `C:\OEM\02-install-icloud.ps1` (or install "iCloud" from the Store).
2. Launch iCloud, sign in + 2FA, turn **iCloud Drive ON**, **disable Files
   On-Demand**, wait for initial sync, then pin: `attrib +P -U "%USERPROFILE%\iCloudDrive\*" /S /D`.
3. Edit `C:\OEM\03-create-share.ps1` (set `SHARE_PASS`) and run it as Administrator.

Back on the host:

```bash
sudo ./host/setup-host.sh             # mount the share + enable health checks (§8–§9)
./host/acceptance-tests.sh            # verify (§11)
ls /mnt/icloud                        # your iCloud files
```

## Status

Build scaffolding is **complete**; all host and guest automation is written and
committed. What remains is a real end-to-end run on KVM hardware plus the manual
Apple ID sign-in — i.e. executing the Quickstart above and passing the
acceptance tests in [`docs/implementation-plan.md`](docs/implementation-plan.md)
§11. Everything that can be prepared without your Apple session and physical
host is in place.

## Status

Groundwork in progress. The authoritative, step-by-step build instructions —
all component decisions, provisioning scripts, host mount config, health
monitoring, acceptance tests, and the failure runbook — live in
[`docs/implementation-plan.md`](docs/implementation-plan.md).

## Security posture

The guest holds an authenticated Apple session. All published ports bind to
`127.0.0.1` only and are never exposed to the LAN. The SMB share uses a
dedicated, password-protected Windows account separate from the auto-logon user.

## Scope

In scope: **iCloud Drive** files, bidirectional. Out of scope (by design):
Photos, Passwords, Mail/Contacts/Calendar. Do not place git repos, build trees,
or SQLite databases on the mount — SMB and iCloud sync semantics make those
unsafe. See the plan's "Known limitations" for details.
