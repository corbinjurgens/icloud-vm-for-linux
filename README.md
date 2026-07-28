# iCloud Drive on Linux via a Minimal Windows VM

Run Apple's official **iCloud for Windows** client inside a stripped-down
Windows 11 VM on a Linux host, and expose the synced iCloud Drive folder to the
host as a normal mounted directory at `/mnt/icloud`.

## Which document you want

| If you want to | Read |
|---|---|
| Stand this up on a fresh host | [`SETUP.md`](SETUP.md) — the annotated runbook, with the snags a real machine actually hits |
| Follow the condensed happy path | [Usage](#usage), below |
| Contribute or change the code | [`CONTRIBUTING.md`](CONTRIBUTING.md) — required workflow, design constraints, tests, and commit rules |
| Lint, test or run the code | [Development](#development), below |
| Exclude folders from sync | [`docs/selective-sync.md`](docs/selective-sync.md) |
| Edit an Obsidian vault safely, off the mount | [Safe Workspaces](#safe-workspaces-editing-a-vault-on-local-disk), below, and [`docs/plan-safe-local-workspaces.md`](docs/plan-safe-local-workspaces.md) |
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
│        │                                                              │
│        ◄── unison one-shot cycles ──►  Safe Workspace: an opt-in      │
│                                        local-disk replica an editor   │
│                                        opens instead of the mount     │
│                                                                       │
│  tray icon + GUI: health, selective sync, Safe Workspaces             │
│  systemd timer: health check (mount + write-canary + freshness)       │
└───────────────────────────────────────────────────────────────────────┘
```

The host writes land directly in the guest's Cloud Files sync root and upload
immediately — a live, bidirectional bridge, not a one-way mirror.

`/mnt/icloud` stays exactly that: a direct SMB view of the guest's sync root,
with no copy in between. [Safe Workspaces](#safe-workspaces-editing-a-vault-on-local-disk)
is an opt-in layer *above* it for the one case a live network filesystem serves
badly — an editor autosaving into a vault — and it reads and writes the same
mount to do its work.

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
| Host mount | kernel `cifs` + systemd automount | Mirroring the library onto the host |
| Local editing replicas (opt-in) | [Unison](https://github.com/bcpierce00/unison) 2.52+, in short one-shot runs | A new three-way merge engine, or a FUSE/rclone overlay |

The mount is deliberately not a copy: SMB is the canonical raw transport
because it is the guest's own live view of the sync root, so a host write is an
iCloud write with nothing in between to fall behind or resolve. A Safe
Workspace does add a synchronizer, but only for a directory the operator opts
in, only while the GUI is running, and with `/mnt/icloud` still the remote side
of every cycle — see [`docs/plan-safe-local-workspaces.md`](docs/plan-safe-local-workspaces.md)
(D52) for why that does not reverse the mount decision or the rejection of a
filesystem overlay.

## Repository layout

```
.
├── Makefile               # every entry point; run `make` to list them
├── docker-compose.yml     # the dockur/windows service definition
├── .env.example           # operator-specific values (copy to .env, gitignored)
├── SETUP.md               # annotated real-machine runbook + troubleshooting
├── CONTRIBUTING.md        # canonical rules for contributors and coding agents
├── CHANGELOG.md           # shipped improvements, next candidates, ruled-out ideas
├── packaging/             # .deb build
│   ├── build-deb.sh       # stages a tree, dpkg-deb (no debhelper/fakeroot/root)
│   ├── lint-ps1.ps1       # PowerShell parse + analyzer pass
│   └── deb/               # control.in, maintainer scripts, launcher, overrides
├── provision/             # scripts run INSIDE the Windows guest
│   ├── install.bat        # dockur OEM bootstrap (runs 01, installs the watcher, desktop note)
│   ├── 01-debloat.ps1
│   ├── 02-install-icloud.ps1
│   ├── 03-create-share.ps1
│   ├── 04-bridge-agent.ps1# control share, agent task, ABE, ACL boundaries
│   ├── watcher.ps1        # elevated task: consumes host triggers, runs the orchestrator
│   ├── guest-setup.ps1    # provisioning orchestrator: inspect, repair only drift, verify
│   ├── guest-state.ps1    # side-effect-free guest invariants and work-plan derivation
│   └── agent.ps1          # byte-identical copy of guest-agent/agent.ps1 for the guest payload
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
    ├── plan-safe-local-workspaces.md # Safe Workspaces: the local-replica design (D52)
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
runs automatically via the `/oem` mount, the provisioning watcher registers
itself so the app can drive the rest (step 3), and a `NEXT-STEPS.txt` is left on
the guest desktop. Wait for the Windows desktop to appear before continuing.

### 3. Setup inside the guest (the app does it; the Apple sign-in is yours)

Install the GUI (step 5) and choose **Set up Windows automatically** on its Setup
tab. It installs iCloud for Windows, waits while you sign in, creates the
`syncshare` SMB account and the data share, installs the bridge agent and its
control share, and hands over to **Check setup and connect** — driving the same
scripts an operator would, elevated inside the guest, over a share Windows cannot
write to (v2 plan D40-D44). **Signing in to iCloud — Apple ID, 2FA, and leaving
iCloud Drive and Files On-Demand switched ON — is the only step you perform
inside the VM**; 2FA stays manual by design. Each run inspects a fixed checklist
first and repairs only what is missing or has drifted, so **Re-run Windows
provisioning…** (Status tab, tray menu, and the agent-skew banner) is a safe way
to repair an existing VM later. **Do not pin anything** — see
[Files On-Demand](#files-on-demand-and-disk-space) below.

The full manual sequence — scripts 02, sign-in, 03, 04, run as Administrator on
the guest desktop (§5–§7) — remains supported and is one click away under **Show
manual steps**. It is written out, with the correct script directory and the
one-time bootstrap needed by VMs created before automated provisioning, in
[`SETUP.md` §9](SETUP.md).

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
- **Quit GUI only** — leaves the bridge running (but pauses any Safe Workspace
  propagation, since the cycles live in this process); use it when you just want to
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
after pulling, choose **Re-run Windows provisioning…** if the GUI says the guest
agent no longer matches — it installs the bundled agent and nothing else when
the rest of the VM is healthy.

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
- **Anything that opens file *content* downloads that file.** Listing a folder
  and reading names, sizes and timestamps is metadata and stays cheap, but
  thumbnailers, preview panes, media metadata probes, checksum and backup tools,
  antivirus scanners and desktop content indexers all perform real reads. Point
  one of them at `/mnt/icloud` and it will hydrate the library a file at a time,
  which is easily gigabytes of transfer and guest disk nobody asked for. If your
  file manager or desktop can disable thumbnails, previews and indexing for
  network locations, do that for this mount — this project deliberately changes
  no desktop-wide preference for you. To make a folder permanently unreadable
  instead, exclude it in the GUI.
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

## Safe Workspaces (editing a vault on local disk)

An editor that autosaves continuously — Obsidian is the motivating case — is a
bad fit for a vault opened straight from `/mnt/icloud`: the guest's Cloud Files
filter rewrites file metadata out of band, and the author watched an open note
clear in the editor. A **Safe Workspace** removes that exposure. The editor
opens an ordinary folder on this computer's own disk, and while the GUI is
running the app reconciles that folder with the chosen iCloud directory using
[Unison](https://github.com/bcpierce00/unison) in short one-shot cycles.

It is **opt-in and per directory**. With no workspace configured nothing is
copied anywhere and `/mnt/icloud` behaves exactly as it always has. The design,
including why this neither replaces SMB nor reintroduces a filesystem overlay,
is [`docs/plan-safe-local-workspaces.md`](docs/plan-safe-local-workspaces.md)
(decision D52).

**First, turn off every other bidirectional sync for that vault — including
Obsidian Sync.** Two mechanisms writing the same local folder will fight, and
neither can see the other's intent. Check Obsidian's own Sync settings before
you start; this app cannot detect that for you.

Then, on the GUI's **Safe Workspaces** tab:

1. **Close the vault in Obsidian** while it still points at `/mnt/icloud`. The
   confirmation dialog says the same thing, because opening both copies at once
   is the one way to make this worse rather than better.
2. **Add workspace…** — a display name, the iCloud folder typed as a path
   relative to the mount (`Notes/Tech/Vault`), and a local folder chosen from a
   local-only file dialog (`~/iCloud Workspaces/` is offered as the parent). The
   local folder must be empty or not yet exist, and must sit on an ordinary
   local filesystem (`ext4`, `xfs`, `btrfs`, `bcachefs`, `zfs`); network, FUSE
   and memory-backed filesystems are refused by name. The confirmation states
   the iCloud folder's size and your free space before anything is created.
3. **Wait for the first sync.** The first stable cycle copies the iCloud folder
   down and the row settles on *up to date*. That first pass reads every file,
   so it hydrates the folder exactly like any other host read
   ([Files On-Demand](#files-on-demand-and-disk-space)).
4. **Open the local folder as the vault.** Right after a successful first seed
   the tab offers **Open local workspace in Obsidian**; **Open local folder**
   is always available if no `obsidian://` handler is registered. From then on,
   **never open the `/mnt/icloud` copy again** — that is precisely the situation
   this feature exists to end.

Day to day:

- **Changes take five to ten seconds to leave.** A workspace must look identical
  in two consecutive five-second polls before Unison runs, so a burst of
  autosaves is propagated once it stops, not once per keystroke. Metadata-only
  churn — a changed `ctime` with the same bytes, size and mtime — is invisible to
  that fingerprint by design and never makes a workspace look changed. **Sync
  now** asks for one pass through the same single-flight path the timer uses; it
  is not a second way to run the engine, and it cannot shorten the window.
- **Status is per workspace:** `waiting`, `stabilizing`, `syncing`, `up to
  date`, `paused`, `conflict`, `guarded`, or `error`. A conflict, a guard, or a
  failure turns the **Safe workspaces** health row yellow; it never turns a
  connected bridge red, and a healthy workspace never masks a real bridge
  problem.
- **Conflicts keep both versions.** When the same file changed on both sides
  since the last agreement, nothing is merged, renamed, or overwritten: each
  replica keeps its own version, the row turns yellow, and the tab names the
  affected paths (never their contents). Compare the two, save the version you
  want, and the next stable cycle propagates it. There is no "resolve
  automatically" action, and there never will be.
- **A mass deletion halts instead of propagating.** If an endpoint goes empty,
  or a single cycle would remove at least 20 paths and at least 20 percent of an
  endpoint, the workspace goes `guarded` and Unison is not invoked at all. It
  clears itself if the content comes back — the usual cause is a mount that was
  not fully there.
- **Every overwrite and deletion is backed up first.** Ten versions per path are
  kept under `~/.local/state/icloud-bridge-gui/workspaces/<id>/backups`. To
  recover one: **Pause** the workspace, copy the wanted version out of that
  directory back over the file in your local folder, then **Resume** — the next
  stable cycle propagates the restored content to iCloud. Nothing here is
  pruned on a schedule, so the directory grows with your edit volume.
- **Forgetting a workspace deletes nothing.** It removes this app's entry and
  nothing else; the local folder, the iCloud folder, the sync state and the
  backups all stay where they are, and the dialog names each location. Package
  removal keeps all of it too.

Interruptions are safe, and none of them need an action from you:

- **Quitting only the GUI pauses propagation.** There is no daemon; cycles live
  in the app process. Your local folder is ordinary local disk, so it stays
  fully readable and editable meanwhile, and the next app start picks the
  pending changes up.
- **Powering the bridge off is safe.** An in-flight cycle is counted in the same
  drain the shutdown already waits for, so the app finishes it before
  unmounting — never with a forced or lazy unmount. While the bridge is off, the
  tab stays visible and every action that would touch `/mnt/icloud` is disabled.
- **Losing the network, or the app, mid-cycle costs nothing.** Unison keeps a
  per-workspace record of the last state both replicas agreed on, so an
  interrupted run is simply repeated from that record, and both replicas are
  left intact meanwhile. A cycle that did not finish cleanly never advances the
  record the deletion guard reads.

What this cannot do: it never prevents a conflict created entirely between two
Apple devices while this host is off — that happens inside iCloud, where no
host-side tool can see it.

**Requirements.** Unison 2.52 or newer; the `.deb` declares
`unison (>= 2.52)`, and both `host/setup-prereqs.sh` and `gui/install-gui.sh`
install it through the distro package manager (on a non-apt system the GUI
install still completes and tells you Safe Workspaces stays unavailable until
Unison is present — no binary is ever downloaded). This was developed and
tested against Unison **2.53.8**. If the host package is already installed, run
`make reinstall` to pick this up.

**Verification status.** The engine invocation, conflict retention, the ten
central backups, deletion propagation and metadata-only touches are covered by
integration tests that run against the real Unison binary. Nothing about the
live Windows guest, CIFS, Obsidian, or a second Apple device has been exercised
yet: every row of the acceptance matrix in
[`docs/plan-safe-local-workspaces.md` §15](docs/plan-safe-local-workspaces.md)
is still `unverified`.

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
