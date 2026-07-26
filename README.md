# iCloud Drive on Linux via a Minimal Windows VM

Run Apple's official **iCloud for Windows** client inside a stripped-down
Windows 11 VM on a Linux host, and expose the synced iCloud Drive folder to the
host as a normal mounted directory at `/mnt/icloud`.

## Which document you want

| If you want to | Read |
|---|---|
| Stand this up on a fresh host | [`SETUP.md`](SETUP.md) — the annotated runbook, with the snags a real machine actually hits |
| Follow the condensed happy path | [Usage](#usage), below |
| Lint, test or run the code | [Development](#development), below |
| Exclude folders from sync | [`docs/selective-sync.md`](docs/selective-sync.md) |
| Fix something that broke | [`SETUP.md` troubleshooting](SETUP.md#troubleshooting-quick-reference) |
| See what improved, what is next, or what was already ruled out | [`CHANGELOG.md`](CHANGELOG.md) |
| Understand why it is built this way, or what was already tried and rejected | [`docs/implementation-plan.md`](docs/implementation-plan.md) (v1) and [`docs/plan-gui-selective-sync.md`](docs/plan-gui-selective-sync.md) (v2 — amends v1 and wins on conflict) |

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
┌───────────────────────────── Linux host ──────────────────────────────┐
│                                                                       │
│ dockur/windows container  ──►  Windows 11 guest                       │
│   (KVM/QEMU, unattended         • iCloud for Windows (official)       │
│    install, VirtIO)             • Files On-Demand ON, nothing pinned  │
│                                 • SMB share of the sync root          │
│                                 • bridge agent (exclusions, status)   │
│                                      │          │                     │
│  /mnt/icloud         ◄── cifs ───────┘          │  (127.0.0.1:10445)  │
│  /mnt/icloud_bridge  ◄── cifs ──────────────────┘                     │
│   (systemd automounts)                                                │
│                                                                       │
│  tray icon + GUI: health at a glance, selective sync                  │
│  systemd timer: health check (mount + write-canary + freshness)       │
└───────────────────────────────────────────────────────────────────────┘
```

The host writes land directly in the guest's Cloud Files sync root and upload
immediately — a live, bidirectional bridge, not a one-way mirror.

Files are **not** downloaded up front. Everything appears in `/mnt/icloud` at
its real size as an online-only placeholder, and the content is fetched the
first time something reads it. A cold read therefore blocks for as long as the
download takes, with no host-side progress indication; after that the file stays
cached until the guest needs the space back.

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
├── Makefile               # every entry point; run `make` to list them
├── docker-compose.yml     # the dockur/windows service definition
├── .env.example           # operator-specific values (copy to .env, gitignored)
├── SETUP.md               # annotated real-machine runbook + troubleshooting
├── CHANGELOG.md           # shipped improvements, next candidates, ruled-out ideas
├── packaging/             # .deb build
│   ├── build-deb.sh       # stages a tree, dpkg-deb (no debhelper/fakeroot/root)
│   ├── lint-ps1.ps1       # PowerShell parse + analyzer pass
│   └── deb/               # control.in, maintainer scripts, launcher, overrides
├── provision/             # scripts run INSIDE the Windows guest
│   ├── install.bat        # dockur OEM bootstrap (auto-runs 01, writes desktop note)
│   ├── 01-debloat.ps1
│   ├── 02-install-icloud.ps1
│   ├── 03-create-share.ps1
│   ├── 04-bridge-agent.ps1# control share, agent task, ABE, ACL boundaries
│   └── agent.ps1          # byte-identical copy of guest-agent/agent.ps1 for C:\OEM
├── guest-agent/
│   └── agent.ps1          # THE guest agent (source of truth): exclusions, status, reclaim
├── gui/                   # host GUI + tray icon (PySide6)
│   ├── icloud_bridge_gui/ # the app; health/bridge/power/autostart stay Qt-free
│   ├── tests/             # pytest; must pass with AND without PySide6 installed
│   ├── install-gui.sh     # per-user install (launcher, .desktop, autostart)
│   ├── icloud-bridge-gui.desktop
│   └── autostart/icloud-bridge-tray.desktop
├── tools/                 # host-side helpers for driving/verifying the guest
│   ├── guest-ctl.sh       # type into / screenshot the guest (QEMU monitor)
│   ├── qemu-monitor.py    # sendkey + screendump client (runs in the container)
│   ├── rdp-ready.py       # real RDP handshake -- is Windows actually up?
│   ├── keep-iso.sh        # preserve the downloaded ISO (avoid re-download)
│   ├── watch-sync.sh      # wait for the initial population to plateau
│   ├── icloud-status.ps1  # in-guest hydration report
│   └── test-smb-*.{ps1,sh}# the 2026-07-22/23 hydration evidence
├── host/                  # host-side setup, systemd units + health check
│   ├── setup-prereqs.sh   # install docker + cifs-utils, verify KVM
│   ├── setup-host.sh      # place units, helper and marker dir, then configure
│   ├── icloud-bridge-configure # credentials, mount uid/gid, sudoers grant
│   ├── acceptance-tests.sh# host-side subset of the acceptance tests
│   ├── mnt-icloud.mount / .automount
│   ├── mnt-icloud_bridge.mount / .automount
│   ├── icloud-health.sh
│   ├── icloud-health.service
│   └── icloud-health.timer
└── docs/
    ├── implementation-plan.md   # v1: full, authoritative build handoff
    ├── plan-gui-selective-sync.md # v2: GUI, bridge protocol, selective sync
    ├── selective-sync.md        # what exclusion does, and the deployment checklist
    └── automation-notes.md      # first-run record: what was manual and why
```

## Usage

Setup runs in numbered phases below: prepare the host once, boot the Windows
guest, do the one-time in-guest setup, mount the shares on the host, then
install the GUI. Everything except the Apple ID sign-in and 2FA is scripted.
Section references (§) point at [`docs/implementation-plan.md`](docs/implementation-plan.md).

For an annotated, real-machine runbook — including the Docker Desktop vs native
Engine gotcha and other snags hit on a first run — see [`SETUP.md`](SETUP.md).

### 1. Prepare the host (once)

```bash
sudo ./host/setup-prereqs.sh   # Docker Engine + cifs-utils, KVM check, creates /srv/icloud-vm/storage (§1)
```

Then **log out and back in** (or `newgrp docker`) so your user picks up the
`docker` group and can run `docker` without `sudo`. Requirements: a Linux host
with working KVM (`kvm-ok` must pass) — a bare-metal box, or a VM with nested
virtualization enabled.

### 2. Configure and boot the guest

```bash
cp .env.example .env && $EDITOR .env   # set SHARE_PASS (20+ random chars) and DISK_SIZE / RAM_SIZE / CPU_CORES
docker compose up -d                   # boot the guest
docker compose logs -f                 # follow the build (Ctrl-C stops the log tail, not the container)
```

The **first** `up` downloads a multi-gigabyte Windows 11 ISO and runs an unattended
install — typically **20–40 minutes**. Watch it live at
**http://127.0.0.1:8006** (noVNC). The debloat step (`provision/01-debloat.ps1`)
runs automatically via the `/oem` mount, and a `NEXT-STEPS.txt` is left on the
guest desktop. Wait for the Windows desktop to appear before continuing.

### 3. One-time setup inside the guest (manual)

On the guest desktop (via the web viewer, or RDP to `127.0.0.1:3389`), open
**PowerShell as Administrator** and (§5–§7):

1. Install the client — Windows blocks `.ps1` files by default, so the
   `-ExecutionPolicy Bypass` prefix is required:
   ```powershell
   powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\02-install-icloud.ps1
   ```
   Or install "iCloud" from the Microsoft Store if winget/msstore errors.
2. Launch iCloud, **sign in with your Apple ID + 2FA**. Turn **iCloud Drive ON**,
   **leave Files On-Demand ON**, and leave Photos/Mail/Contacts/Calendar **OFF**.
   Wait for the initial metadata population to settle. **Do not pin anything** —
   see [Files On-Demand](#files-on-demand-and-disk-space) below.
3. Edit `C:\OEM\03-create-share.ps1`, set `$pass` to the **same** `SHARE_PASS`
   value from your host `.env`, then run it as Administrator (same bypass):
   ```powershell
   powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\03-create-share.ps1
   ```
   This creates the dedicated `syncshare` SMB account and shares the sync root.
4. Install the bridge agent and selective sync (Administrator, no secret needed):
   ```powershell
   powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\04-bridge-agent.ps1
   ```
   It creates the `bridge` control share, registers the `icloud-bridge-agent`
   scheduled task, and turns on Access-Based Enumeration for the data share.

### 4. Mount on the host and verify

```bash
sudo ./host/setup-host.sh    # credentials from .env, both CIFS mounts + health timer,
                             #   and the GUI power helper + sudoers grant (D29)
./host/acceptance-tests.sh   # host-side acceptance checks
ls /mnt/icloud               # your iCloud files
```

Files written under `/mnt/icloud` land in the guest's sync root and upload to
iCloud automatically. Run `setup-host.sh` with `sudo` from your desktop account
so it can grant *that* account the power-helper permission; if you run it from a
root shell instead, pass `TARGET_USER=<name>`. The mount owner defaults to that
account's uid/gid; override with `MOUNT_UID`/`MOUNT_GID`.

**Before trusting the mount with real work, run the E0 gate** — a cold read of a
large online-only file through the kernel CIFS client, plus a write/edit/delete
round trip. Steps and pass/fail criteria are in
[`docs/selective-sync.md`](docs/selective-sync.md#deployment-checklist).

### 5. Install the GUI and tray icon

```bash
./gui/install-gui.sh         # run as your desktop user, NOT root
```

The tray icon shows overall health at a glance (green/yellow/red) and its menu
opens the iCloud folder, the status window, and the VM screen. The status window
hosts the selective-sync UI.

**If you install the GUI first, it can walk you through the rest.** With no
Windows VM yet, the window opens on a **Setup** tab that checks what this host
needs — `/dev/kvm`, `/dev/net/tun`, the native Docker socket reachable by *this
login session*, the Compose plugin, the installed compose/provision files, and
your `.env` — and shows the exact command to fix anything that fails. It never
runs those commands for you. Once the checks pass it offers **Create Windows
VM**, then waits while Windows installs and lists the in-guest steps above plus
the host command to run, before connecting the mounts for the first time. It
reads nothing from `/mnt/icloud` until the bridge is genuinely up.

**GNOME users:** install the *AppIndicator and KStatusNotifierItem Support*
extension, or the tray icon will not be visible.

**Starting and quitting (the GUI is the on/off switch).** Launching the GUI is
how you turn the bridge on: if the Windows VM is stopped, it powers it on, waits
for the shares to mount (a blue *Starting* icon), and only then reads iCloud.
The tray's **Quit** offers three choices:

- **Quit and power off VM** — like quitting Google Drive: it stops syncing,
  cleanly disconnects `/mnt/icloud` and `/mnt/icloud_bridge`, and powers the VM
  off so it stops using RAM and CPU. Starting the GUI again brings it back.
  Unuploaded changes resume on the next start (this cannot prove Apple's upload
  queue is empty). If a file on the mount is still open, the shutdown aborts and
  leaves the VM running rather than force-unmounting.
- **Quit GUI only** — leaves the bridge running; use it when you just want to
  restart or upgrade the GUI.
- **Cancel.**

**Turning the bridge off without quitting.** The tray menu and the Status tab
also carry a single power action for the current state:

- **Power off bridge (keep this app running)** — the same clean teardown as
  *Quit and power off VM*, including the same refusal to unmount a busy share,
  but the app stays in the tray showing a grey *powered off* icon. Nothing is
  read from the mounts while it is off.
- **Start bridge** — brings it all back. It also appears if the container turns
  out to be stopped mid-session (for instance because you ran `docker stop`),
  which is the in-app way to recover without restarting the GUI.

The button never appears merely because health went red: a red icon can equally
mean a running VM with a stale canary or an unreadable file, so the app asks
Docker directly before offering to start anything. Quitting while already
powered off simply exits — the bridge stays off, including across a reboot.

Closing the window with its **X** only hides it when a tray is present; the
bridge keeps running. A checkable **Start when the computer starts** item
controls whether the GUI (and therefore the bridge) comes up automatically at
login. This needs a one-time `sudo ./host/setup-host.sh` so the GUI may run the
privileged power helper without a password.

## Installing as a package instead

The steps above install from the checkout. `make` builds a `.deb` that places the
same files at the same paths, so the two are interchangeable:

```bash
make deb          # -> dist/icloud-bridge_<version>_all.deb
make install      # apt install ./dist/icloud-bridge_*.deb
make configure    # sudo icloud-bridge-configure --env-file ./.env
```

`make configure` is not optional and cannot be folded into the package: the share
password lives in the gitignored `.env`, and the mount ownership and the sudo
grant for the power helper both key off whichever desktop account will run the
GUI. None of that is knowable when the package is built. It is idempotent — re-run
it after changing the desktop user or the share password.

The package includes the tray GUI, so `./gui/install-gui.sh` is not needed
alongside it. Both may be installed: a per-user install shadows the system one by
`PATH` and XDG precedence, so the tray cannot end up launched twice. The per-user
installer remains the right choice on a release whose archives lack the
`python3-pyside6` packages, since it can fall back to a dedicated venv.

`make uninstall` removes the package but keeps your credentials and sudoers grant;
`make purge` removes those too.

## Development

```bash
make            # list every target
make hooks      # once per clone: install the pre-commit and commit-msg hooks
make check      # lint + tests -- everything provable without a VM
make test-all   # run the suite both with and without PySide6
make run        # launch the GUI from the source tree, nothing installed
make lint-ps    # fetch PowerShell 7 and parse the .ps1 files
```

`make hooks` points `core.hooksPath` at `.githooks/`. The pre-commit hook runs
`tools/hygiene-checks.sh` and the pytest suite against a snapshot of the staged
tree -- no live secrets, loopback-only published ports, LF endings, the two
`agent.ps1` copies identical, syntax, and the full pytest suite, in about a
second. `make lint` runs the same checker over the working tree if you want it
without committing.

This project is **pre-release**: there are no tags and no installed copies
beyond the author's. Formats and interfaces change without a migration path, so
after pulling, re-run `04-bridge-agent.ps1` in the guest if the GUI says the
agent no longer matches.

`make test` creates `.venv` on first use; there is no system pytest and PEP 668
blocks `pip install --user`, so a venv is the supported route. `make run` and
`make test-qt` also want `.venv-qt`, which `make venv-qt` builds (a large PySide6
download); `make run` falls back to your system `python3` when that venv is
absent, which works only if PySide6 is installed system-wide.

**What `make run` gives you without a VM.** The tray appears and, with no guest
yet, the window opens on its **Setup** tab — the checks there (`/dev/kvm`, the
Docker socket, the compose and provision files, `.env`) are exactly what a
first-run operator sees, and the checkout itself supplies the bundle they point
at. What you cannot exercise is anything behind an install: health and selective
sync read as unavailable with nothing mounted, and the power controls fail,
because they call the `icloud-bridge-power` helper that the host install places.
`make install-gui` does the per-user install — the same thing as running
`./gui/install-gui.sh` directly.

Targets that need real hardware — `deps`, `install`, `configure`, `acceptance` —
are labelled `HOST:` in `make` output and cannot be validated from a checkout
alone. See [plan §14](docs/implementation-plan.md#14-build-packaging-and-developer-entry-points)
for how the packaging is put together and why it ships where it does.

## Files On-Demand and disk space

Files On-Demand stays **on** and this project pins nothing. v1 planned the
opposite, on the assumption that dataless placeholders would stall over SMB;
live testing on 2026-07-22/23 disproved it — placeholders hydrate on demand for
SMB reads, exactly like OneDrive. What that means day to day:

- Listing the whole library is instant and shows real sizes; nothing is
  downloaded until it is read.
- A **cold read blocks for the whole download**. Small files take seconds; a
  multi-GB file can take much longer, and there is no progress indication on the
  host side. `cp` a large file rather than opening it from an editor.
- Read files stay cached in the VM. When guest free space drops below 20 GB the
  agent asks Windows to make the coldest in-sync files online-only again until
  30 GB is free. Dehydration is asynchronous, and files that are open, modified,
  or not yet uploaded are skipped rather than touched.
- Size the guest disk for Windows plus the working set you expect to have
  cached, not for the whole library.

## Selective sync (excluding folders)

Uncheck a folder or file in the GUI's **Selective Sync** tab and Apply. From
then on, that item:

- **disappears** from `/mnt/icloud` — it is not listed and cannot be opened by
  path (SMB Access-Based Enumeration plus an explicit deny for the share
  account);
- **never downloads** — a host read cannot trigger hydration, and any content
  already cached is released once iCloud reports it safe to do so;
- **fails closed** for writes, deletes, renames, and attempts to create
  something with the same name: those return *permission denied*, not *no such
  file*. That is deliberate — it stops a host-side write from colliding with a
  hidden item. Included siblings in the same folder stay fully usable.

Excluding **never deletes anything**. The item stays in iCloud and on your other
devices; re-including it makes it reappear, and its content downloads when
something reads it. Full details, including the `not-found` state and the rename
limitation, are in [`docs/selective-sync.md`](docs/selective-sync.md).

## Status

All host and guest code is written and committed: provisioning scripts, the
guest bridge agent, both CIFS mounts, health monitoring, and the GUI (whose
`pytest gui/tests` suite passes). What remains is a real end-to-end run on KVM
hardware plus the manual Apple ID sign-in — the Usage steps above, the **E0
gate**, and then the acceptance tests. Nothing in this repository has been
verified against a running Windows guest yet.

The authoritative design documents are
[`docs/implementation-plan.md`](docs/implementation-plan.md) (v1: components,
provisioning, mount, health, runbook) and
[`docs/plan-gui-selective-sync.md`](docs/plan-gui-selective-sync.md) (v2: the
GUI, the bridge protocol, and selective sync; where the two disagree, v2 wins).

## Security posture

The guest holds an authenticated Apple session. All published ports bind to
`127.0.0.1` only and are never exposed to the LAN. Both SMB shares use one
dedicated, password-protected Windows account separate from the auto-logon user.

The bridge is a control channel, so its privileges are drawn narrowly: SMB
exports only the JSON exchange directory. The scheduled agent script and its
trusted private state live *outside* the share, so possession of the SMB
credential cannot replace the code the guest runs. The agent itself runs
unelevated and is granted exactly two extra rights on the sync root —
`READ_CONTROL` and `WRITE_DAC`, enough to edit permissions and nothing else.

## Scope

In scope: **iCloud Drive** files, bidirectional. Out of scope (by design):
Photos, Passwords, Mail/Contacts/Calendar. Do not place git repos, build trees,
or SQLite databases on the mount — SMB and iCloud sync semantics make those
unsafe. See the plan's "Known limitations" for details.
