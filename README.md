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
├── host/                  # host-side systemd units + health check
│   ├── mnt-icloud.mount
│   ├── mnt-icloud.automount
│   ├── icloud-health.sh
│   ├── icloud-health.service
│   └── icloud-health.timer
└── docs/
    └── implementation-plan.md   # full, authoritative build handoff
```

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
