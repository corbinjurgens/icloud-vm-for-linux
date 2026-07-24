# SETUP.md — real-world first-run runbook

This is the annotated, battle-tested setup log for standing up the iCloud-on-Linux
bridge on a **fresh host**. It records not just the happy path (which lives in the
[README](README.md#usage) Usage section) but the actual snags hit on a real machine
and how they were resolved. If you are rebuilding on a new box, follow this file
top to bottom.

`docs/implementation-plan.md` remains the authoritative design doc; this file is
the operational "what you actually type, and what bites you" companion.

---

## 0. Host baseline

First host this was run on:

- MSI laptop (i7-13700H), bare metal (not itself a VM), 61 GB RAM, ~240 GB free NVMe.
- **Ubuntu 26.04** ("resolute"), apt-based.
- Working KVM: `/dev/kvm` present, `kvm-ok` passes, 20 vmx-capable threads.
  (Note: `grep -c vmx /proc/cpuinfo` double-counts — the kernel emits both a
  `flags` and a `vmx flags` line per CPU.)
- **Docker Desktop was already installed** — this turned out to be the single
  biggest gotcha (see §2).

Minimum requirements for any host:

- Linux with **working KVM** (`kvm-ok` must pass). Bare metal, or a VM with
  nested virtualization enabled.
- **Native Docker Engine** — *not* Docker Desktop (see §2 for why, and the fix).
- Enough free disk for the guest qcow2: 40 GB + your iCloud Drive size, rounded
  up to the next 20 GB (plan D10).

---

## 1. Missing base tools (minimal installs)

On a minimal/fresh Ubuntu, some tools this runbook uses are not present:

- `newgrp` (used to refresh group membership without a full logout) lives in a
  separate package on recent Ubuntu:

  ```bash
  sudo apt install util-linux-extra
  ```

`kvm-ok` (`cpu-checker`) and `cifs-utils` are installed for you by
`host/setup-prereqs.sh` in §3, so you don't need to install those by hand.

---

## 2. Docker Engine vs Docker Desktop  ← the important one

**Symptom / why it matters.** This project boots a Windows guest that needs the
host's real `/dev/kvm` passed into the container. **Docker Desktop cannot do
this** — Desktop runs every container inside its own LinuxKit VM, which does not
expose `/dev/kvm`. A container launched under Desktop fails with:

```
docker run --device /dev/kvm alpine …
→ error gathering device information while adding custom device "/dev/kvm": no such file or directory
```

You need the **native Docker Engine** (host `dockerd` on `/var/run/docker.sock`),
which runs containers directly on the host and can pass `/dev/kvm`. Engine and
Desktop can coexist; you pick between them with `docker context`.

**How to tell which you have:**

```bash
docker context ls
# 'desktop-linux *'  → Docker Desktop is active (WRONG for this project)
# 'default *'        → native Engine is active (CORRECT)

docker info | grep 'Operating System'
# 'Operating System: Docker Desktop'  → Desktop
```

**Fix.** Install native Engine and switch your context to it. Note two traps we
hit on the first run:

- `host/setup-prereqs.sh` installs Engine for you, but only if it can't already
  find the daemon. It tests for the **`dockerd`** binary specifically, *not* the
  `docker` CLI — because Docker Desktop puts the CLI on the host even though its
  daemon lives in a VM. (An earlier version tested `command -v docker`, saw
  Desktop's CLI, and wrongly skipped the Engine install. Fixed.)
- The convenience installer (`get.docker.com`) prints
  *"the docker command appears to already exist … press Ctrl+C to abort"* and
  sleeps 20 s. **This is expected** when Desktop is present — let it continue; it
  installs `docker-ce` + the systemd `docker.service` anyway.

Context selection is **per-user** and does not take effect from inside the
root-run setup script for your login, so switch it yourself:

```bash
docker context use default
docker context ls          # confirm: default *
```

---

## 3. Host prerequisites (scripted)

From the repo root, as root:

```bash
sudo ./host/setup-prereqs.sh
```

This is idempotent and does all of:

- verifies KVM (`kvm-ok`),
- installs native Docker Engine if `dockerd` is missing (see §2),
- selects the `default` context for your user if Docker Desktop is detected,
- installs `cifs-utils` (host-side SMB mount),
- adds your user to the `docker` group,
- creates `/srv/icloud-vm/storage` for the guest disk image.

Expected tail of a good run: `==> Installing cifs-utils …` followed by
`==> Creating VM storage dir …` with **no** `docker.service does not exist`
error. If you see that error, you're on the pre-fix script or Engine didn't
install — revisit §2.

---

## 4. Refresh your docker group (relogin)

Adding your user to the `docker` group only affects **new login sessions**. Until
you refresh, `docker ps` against the native socket fails with:

```
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

Confirm the group assignment landed, then refresh:

```bash
getent group docker        # should list your user, e.g. docker:x:973:corbin

# Quick single-shell test (needs util-linux-extra from §1):
newgrp docker
docker ps                  # empty table, no permission error
docker version --format '{{.Server.Version}}'   # native Engine version, e.g. 29.6.2
exit
```

`newgrp` only fixes the one shell. For the group to apply everywhere — including
any tooling (editors, Claude Code, etc.) that will drive `docker` — **fully log
out and back in, or reboot.** A new terminal in the same old session is not
enough.

After relogin, verify from a clean shell:

```bash
docker context ls                                 # default *
docker ps                                         # empty, no sudo, no error
docker version --format '{{.Server.Version}}'     # native Engine version
```

---

## 5. Configure `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Set:

- `SHARE_PASS` — **generate a strong 20+ char random password.** This is the SMB
  account password; you'll paste the *same* value into the guest in §7. `.env` is
  gitignored — never commit it, and never put the real value in this file or any
  other tracked file.
- `DISK_SIZE` / `RAM_SIZE` / `CPU_CORES` — size per `.env.example` comments. The
  qcow2 grows on demand, so oversizing `DISK_SIZE` is free. Don't drop `RAM_SIZE`
  below 2.5G (Windows servicing needs it).

Sanity-check interpolation before booting:

```bash
docker compose config      # should print the resolved service with your .env values
```

---

## 6. Boot the guest (first run downloads Windows)

```bash
docker compose up -d
docker compose logs -f     # Ctrl-C stops the log tail, not the container
```

The **first** `up` downloads a Windows 11 ISO (~5 GB) and runs an unattended
install — typically **20–40 min**. Watch live at **http://127.0.0.1:8006**
(noVNC). The debloat step runs automatically via the `/oem` mount and drops a
`NEXT-STEPS.txt` on the guest desktop. Wait for the Windows desktop to appear.

Quick pre-boot KVM passthrough check (should print the device, not an error):

```bash
docker run --rm --device /dev/kvm alpine ls -l /dev/kvm
```

---

## 7. Keep the downloaded Windows ISO (avoid re-downloading)

**You normally never re-download.** The installed guest lives in
`/srv/icloud-vm/storage`, so `docker compose stop/start/down/up` just boots the
existing disk — no reinstall, no download. A re-download only happens if you
**wipe that storage directory** to redo the install from scratch.

**The catch:** dockur *deletes the ISO it downloaded* as soon as it has prepared
the install media — `removeImage()` is called at `install.sh:1353`, which runs
**before** `buildImage` and long before Windows finishes installing. So if you
later wipe storage to retest, you pay the ~5 GB download again.

**The escape hatch** is in `removeImage()` itself:

```sh
removeImage() {
  local iso="$1"
  [ ! -f "$iso" ] && return 0
  [ -n "$CUSTOM" ] && return 0     # <-- a custom ISO is never deleted
  rm -f "$iso" ...
```

`detectCustom()` sets `CUSTOM` when it finds a file named **`custom.iso`** or
**`boot.iso`** at depth 1 in `/` or in `$STORAGE`. So an ISO you supply yourself
is kept, reused, and never re-fetched.

### Preserving the ISO during a live first run

dockur downloads with `wget -O /storage/tmp/win11x64.iso --continue` (writing in
place), so the safest rescue is a **hard link**, not a copy: it is instantaneous,
and dockur's `rm` then only drops one link while the data survives. A copy would
usually lose the race, since deletion happens minutes after the download.

```bash
# while the container is running, before/while it downloads:
docker exec icloud-windows ln -f /storage/tmp/win11x64.iso /storage/win11x64-keep.iso
docker exec icloud-windows stat -c 'inode=%i links=%h size=%s' \
    /storage/tmp/win11x64.iso /storage/win11x64-keep.iso   # same inode, links=2
```

Caveat: if the download restarts into a *new* inode, an existing link is left
holding a stale partial file — re-run the `ln -f` if the inodes ever diverge.

### Stash it outside the storage dir

The kept file starts out *inside* the directory you would wipe for a clean
reinstall, so link it somewhere safer. `/srv` is one filesystem, so this is
another hard link — instant and free:

```bash
docker run --rm -v /srv:/srv alpine sh -c \
  'mkdir -p /srv/isos && ln -f /srv/icloud-vm/storage/win11x64-keep.iso /srv/isos/win11-25h2-x64.iso'
```

(Using a throwaway root container avoids needing host `sudo`.) Verified on the
first run — one inode, two names, zero extra disk:

```
/srv/isos/win11-25h2-x64.iso              inode=5898275 links=2 size=8471603200
/srv/icloud-vm/storage/win11x64-keep.iso  inode=5898275 links=2 size=8471603200
```

Actual ISO as downloaded on 2026-07-22: `Win11_25H2_English_x64_v2.iso`,
**8,471,603,200 bytes (7.9 GB)**.

### Reusing it for a clean reinstall

To rebuild the guest from scratch **without** re-downloading:

```bash
docker compose down
sudo rm -rf /srv/icloud-vm/storage/*          # drop disk image, state files, rebuilt media
sudo cp -l /srv/isos/win11-25h2-x64.iso /srv/icloud-vm/storage/custom.iso   # hard link, free
docker compose up -d                          # installs from custom.iso, no download
```

Because `custom.iso` sets `CUSTOM`, dockur reuses it *and* never deletes it — so
from then on the ISO is permanently cached.

Note: after a successful install `/storage` also holds `win11x64.iso` — that is
dockur's **rebuilt** boot media (autounattend + the `/oem` payload injected), a
different file from the pristine download. Don't mistake it for the cached ISO.

---

## 8. One-time in-guest setup (manual — needs your Apple ID)

On the guest desktop (web viewer, or RDP to `127.0.0.1:3389`), open **PowerShell
as Administrator** and follow plan §5–§7:

> **Scripts are blocked by default.** Windows 11 sets the PowerShell execution
> policy to `Restricted`, so launching a `.ps1` directly fails with *"running
> scripts is disabled on this system"*. Prefix each launch below with
> `powershell -ExecutionPolicy Bypass -NoProfile -File`, or run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force` once per
> window. (`01-debloat.ps1` ran unattended only because `install.bat` already
> invokes it that way.)

1. Install the client:
   ```powershell
   powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\02-install-icloud.ps1
   ```
   It's a single command, so pasting it directly works just as well:
   ```powershell
   winget install --id 9PKTQ5699M62 --source msstore --accept-package-agreements --accept-source-agreements
   ```
   (`9PKTQ5699M62` is iCloud's Store product ID. The msstore source only matches
   on that ID, not the `AppleInc.iCloud` moniker — verified 2026-07-22.)
   Or install "iCloud" from the Microsoft Store if winget/msstore errors.
2. Launch iCloud, **sign in + 2FA**. Turn **iCloud Drive ON**, **leave Files
   On-Demand ON**, leave Photos/Mail/Contacts/Calendar **OFF**. Wait for the
   initial metadata population to settle (`./tools/watch-sync.sh` from the host
   is a cheap proxy for "it has stopped growing").

   **Do not pin the library.** v1 told you to disable Files On-Demand and run
   `attrib +P -U`; live testing on 2026-07-22/23 disproved the premise behind
   that instruction — dataless placeholders *do* hydrate on demand over SMB — so
   v2 keeps Files On-Demand on and pins nothing. If you already pinned during an
   earlier run, the bridge agent clears that intent once on its first start,
   without evicting any content.
3. Edit `C:\OEM\03-create-share.ps1`, set `$pass` to the **same** `SHARE_PASS`
   from your host `.env`, then run it (same bypass):
   ```powershell
   notepad C:\OEM\03-create-share.ps1
   powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\03-create-share.ps1
   ```
   Creates the `syncshare` account and shares the sync root over SMB.
4. Install the bridge agent, the control share, and Access-Based Enumeration on
   the data share:
   ```powershell
   powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\04-bridge-agent.ps1
   ```
   It prints `bridge ready` once the scheduled task is running and a fresh
   `status.json` has appeared. `C:\OEM\agent.ps1` must be present — it ships in
   `provision/`, so a guest built from this repo already has it. On a guest
   built before that file existed, deliver it through `\\host.lan\Data` first
   (see `docs/automation-notes.md` §3).

---

## 9. Mount on the host and verify

```bash
sudo ./host/setup-host.sh    # builds /etc/credentials-icloud from .env, installs both mounts + health timer
./host/acceptance-tests.sh   # host-side acceptance checks
ls /mnt/icloud               # your iCloud files
ls /mnt/icloud_bridge        # status.json, tree.json, exclusions.json
```

If the mount owner should be someone other than uid/gid 1000, pass
`MOUNT_UID`/`MOUNT_GID` to `setup-host.sh`.

**Run the E0 gate before trusting the mount with real work.** It checks that a
cold, online-only file reads back correctly through the *kernel* CIFS client
(the earlier evidence used userland `smbclient`), and that host writes reach
iCloud. Steps and pass/fail criteria are in
[`docs/selective-sync.md`](docs/selective-sync.md#deployment-checklist).

---

## 10. Install the host GUI and tray icon

```bash
./gui/install-gui.sh         # run as your desktop user, NOT root
```

Installs into `~/.local/share/icloud-bridge-gui/`, adds a launcher at
`~/.local/bin/icloud-bridge-gui`, an application entry, and an autostart entry
that starts the tray minimised. PySide6 comes from the distro packages when they
exist and from a dedicated venv otherwise (never `pip install --user`).

**GNOME users:** install the *AppIndicator and KStatusNotifierItem Support*
extension, or the tray icon will not be visible.

---

## Troubleshooting quick reference

| Symptom | Cause | Fix |
|---|---|---|
| `error gathering device information … "/dev/kvm": no such file or directory` on `docker run` | Docker Desktop context active | §2 — install native Engine, `docker context use default` |
| `Failed to enable unit: Unit docker.service does not exist` from `setup-prereqs.sh` | Desktop's CLI present so Engine install was skipped; no host daemon | Use the fixed script (tests `dockerd`, not `docker`); or install Engine via `get.docker.com` then re-run |
| `permission denied … unix:///var/run/docker.sock` | Not in `docker` group in this session | §4 — relogin (or `newgrp docker` for one shell) |
| `newgrp: command not found` | `newgrp` not installed (minimal Ubuntu) | `sudo apt install util-linux-extra` |
| In the guest: *"running scripts is disabled on this system"* | PowerShell execution policy is `Restricted` by default | Launch via `powershell -ExecutionPolicy Bypass -NoProfile -File <script>` (§8) |
| `docker version` server shows the wrong number (Desktop's, not Engine's) | Context still `desktop-linux` | `docker context use default` |
| Guest install extremely slow / no KVM in logs | KVM not passed through (Desktop, or no `/dev/kvm`) | Confirm §2 + `kvm-ok`; run the KVM passthrough check in §6 |
| `04-bridge-agent.ps1`: *"exclusions.json is missing but this looks like an existing install"* | The config was lost while the task/share/state survived; writing an empty list would silently re-include everything | Restore `C:\ProgramData\icloud-bridge\io\exclusions.json`, or write an explicitly chosen one, then re-run |
| Tray icon shows yellow, `status.json` stale | The guest scheduled task is not running (it only runs in the logged-on `icloud` session) | Open `:8006`, confirm auto-logon happened; `Start-ScheduledTask icloud-bridge-agent` |
| An exclusion is stuck at `pending-dehydrate` | Cloud Files refuses to dehydrate content that is open, modified, or not yet uploaded | Wait for the upload; the item is already hidden and inaccessible from the host. See `docs/selective-sync.md` |
| An exclusion reports `acl-write-denied` | Provisioning step 4 (the agent's `RC,WDAC` grant) did not take, or that object has a protected DACL | Re-run `04-bridge-agent.ps1` as Administrator and read its protected-DACL report |
| No tray icon on GNOME | GNOME has no built-in tray | Install the *AppIndicator and KStatusNotifierItem Support* extension |
