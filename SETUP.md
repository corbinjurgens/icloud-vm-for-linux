# SETUP.md — real-world first-run runbook

This is the annotated, battle-tested setup log for standing up the iCloud-on-Linux
bridge on a **fresh host**. It records not just the happy path (which lives in the
[README](README.md#usage) Usage section) but the actual snags hit on a real machine
and how they were resolved. If you are rebuilding on a new box, follow this file
top to bottom.

`docs/implementation-plan.md` remains the authoritative design doc; this file is
the operational "what you actually type, and what bites you" companion. If you
are changing the code rather than standing up a host, you want
[README Development](README.md#development) instead — `make check` and
`make run` need neither a guest nor root.

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
- loads the `vhost_net` kernel module and records it in
  `/etc/modules-load.d/` — `docker-compose.yml` passes `/dev/vhost-net` into the
  container so QEMU runs the guest's virtio NIC with `vhost=on` instead of
  copying every SMB byte through its userspace loop (v2 plan D33). If your kernel
  has no `vhost_net`, the script warns and you must delete that one `devices:`
  line or the container will not start,
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

> **Do this now if you might ever rebuild the guest.** dockur deletes the ISO it
> downloaded partway through the install, so the only chance to keep it is
> *while the download is still running* — by the time this install finishes it
> is already gone. Run the hard-link rescue in §7 now; the reasoning can wait:
>
> ```bash
> docker exec icloud-windows ln -f /storage/tmp/win11x64.iso /storage/win11x64-keep.iso
> ```
>
> If it reports *no such file*, the download has not started yet — wait a moment
> and repeat. Repeating is safe, and §7 explains why you may need to: a restarted
> download lands on a new inode and leaves your link holding a stale partial.
> Skip all this only if you accept re-downloading the whole ISO.

The **first** `up` downloads a multi-gigabyte Windows 11 ISO and runs an unattended
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
later wipe storage to retest, you pay the whole download again.

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
sudo ./host/setup-host.sh    # places the units, helpers and marker dir, then configures this machine
./host/acceptance-tests.sh   # host-side acceptance checks
ls /mnt/icloud               # your iCloud files
ls /mnt/icloud_bridge        # status.json, tree.json, exclusions.json
```

`setup-host.sh` runs in two halves. It **places** the mount/automount units, the
health script and timer, `/usr/local/bin/icloud-bridge-power` and the marker
directory; then it hands off to **`icloud-bridge-configure`**, which applies
everything specific to this machine: `/etc/credentials-icloud` from your `.env`,
the mount `uid`/`gid`, and the `sudoers` grant that lets the GUI power the bridge
on and off (v2 plan D29).

Run it with `sudo` from your desktop account: the mount owner and that `sudoers`
grant are both derived from it (`SUDO_USER`). From a root shell instead, set
`TARGET_USER=<name>`. Override the mount owner with `MOUNT_UID`/`MOUNT_GID` if it
should differ.

`icloud-bridge-configure` is idempotent and can be re-run on its own whenever the
desktop user or the share password changes — you do not need to re-run the whole
setup script:

```bash
sudo icloud-bridge-configure --env-file "$PWD/.env"     # or --user <name>
```

### Installing from the package instead

`make` builds a `.deb` that places exactly the same files at the same paths, so
the two routes are interchangeable and `acceptance-tests.sh` passes against
either:

```bash
make deb          # -> dist/icloud-bridge_<version>_all.deb
make install      # apt install ./dist/icloud-bridge_*.deb
make configure    # the same icloud-bridge-configure step, which is NOT optional
```

The package cannot configure itself: the share password lives in the gitignored
`.env`, and the mount ownership and `sudoers` grant name whichever desktop
account will run the GUI. None of that is knowable at build time, which is why
the configure step is separate rather than a `postinst`.

**Run the E0 gate before trusting the mount with real work.** It checks that a
cold, online-only file reads back correctly through the *kernel* CIFS client
(the earlier evidence used userland `smbclient`), and that host writes reach
iCloud. Steps and pass/fail criteria are in
[`docs/selective-sync.md`](docs/selective-sync.md#deployment-checklist).

Record the outcome of E0 — and of every other live check — in
[`docs/acceptance-results.md`](docs/acceptance-results.md), which is the durable
result table. Only a run on this real host may fill in a row there.

---

## 10. Install the host GUI and tray icon

**If you installed the package in §9, the GUI is already there** — it ships in
the same `.deb`, at `/usr/bin/icloud-bridge-gui`. Skip to the GNOME note below.

Otherwise, install it for your user:

```bash
./gui/install-gui.sh         # run as your desktop user, NOT root
```

Installs into `~/.local/share/icloud-bridge-gui/`, adds a launcher at
`~/.local/bin/icloud-bridge-gui`, an application entry, and an autostart entry
that starts the tray minimised. PySide6 comes from the distro packages when they
exist and from a dedicated venv otherwise (never `pip install --user`).

Both may be installed at once: a per-user install shadows the system one by
`PATH`, and `~/.local/share/applications` and `~/.config/autostart` override
their `/usr/share` and `/etc/xdg` counterparts by basename, so the tray cannot
end up launched twice. The per-user installer stays the right choice on a release
whose archives lack the `python3-pyside6` packages, since only it falls back to a
dedicated venv.

**GNOME users:** install the *AppIndicator and KStatusNotifierItem Support*
extension, or the tray icon will not be visible.

**On a host with no VM yet, the GUI opens a Setup assistant** (v2 plan D31)
instead of the status view: it re-checks the §2/§4 prerequisites, shows which
`docker-compose.yml` and `provision/` copy it resolved (package, per-user, or
this checkout — never the working directory), validates your `.env` without ever
reading the password out, and offers **Create Windows VM**, which runs

```bash
docker compose -p icloud-bridge -f <bundle>/docker-compose.yml --env-file <your .env> up -d
```

Use that same `-p icloud-bridge` project name for later terminal commands so
they address the same project. The assistant then waits through the Windows
install — it does not try to mount anything — lists the §8 in-guest steps, and
hands back to the power helper when you choose **Check setup and connect**.

The GUI is the bridge's on/off switch (v2 plan D29/D30). Launching it powers the
Windows VM on and mounts the shares; the tray's **Quit → Quit and power off VM**
cleanly disconnects both mounts and powers the VM off (leaving unuploaded changes
to resume next start). **Quit GUI only** leaves the bridge running. **Power off
bridge (keep this app running)** does the same teardown without exiting, and
**Start bridge** brings it back — that action also appears when the container is
found stopped mid-session, so a manual `docker stop` is recoverable from the app.
The tray's **Start when the computer starts** toggles the autostart entry. A reboot while
powered off stays off (a marker plus systemd conditions suppress the mounts and
health timer) until you log in with autostart enabled, or run
`sudo /usr/local/bin/icloud-bridge-power on`.

---

## Taking the data-path work onto a guest built before 2026-07-26

**Read this if your container predates that date.** Two performance decisions —
D32 (SMB signing off) and D33 (`/dev/vhost-net`) — shipped on 2026-07-26, and
neither can reach a container or a guest that already exists:

- `docker-compose.yml` gained the `/dev/vhost-net` device, but Docker only
  applies a device list when it **creates** a container. A container started
  before that line keeps running without it, and dockur silently falls back to
  userspace virtio, so QEMU copies every SMB byte through its own main loop.
- `03-create-share.ps1` gained `RequireSecuritySignature $false`, but that runs
  inside the guest. Until you re-run it, Windows keeps signing every byte on both
  ends of a path where there is nothing to protect (the host is the security
  boundary, D9, and the ports are loopback-only).

Do it in this order. Do **not** `docker rm` or `docker kill` a live bridge —
that is what the ordered teardown exists to prevent:

```bash
sudo icloud-bridge-power off       # or Quit from the GUI; unmounts first, then stops the VM
docker compose up -d               # recreates the container, now with /dev/vhost-net
./host/acceptance-tests.sh         # section 2 must report the device AND vhost=on
```

Then, in the guest (elevated PowerShell), re-run the share script — it is
idempotent, which is why this is safe:

```powershell
C:\OEM\03-create-share.ps1
```

`make acceptance` proves the first half from the host. The second half has no
host-side check: an automated probe was attempted and withdrawn (see the
2026-07-26 entry in `CHANGELOG.md` for why), so re-running the script is the
mechanism. If you want the throughput number, run `tools/test-smb-read.sh` before
and after — that is the only honest measurement of what these two are worth, and
it needs `SHARE_PASS`.

## Optional host tuning (measure first, none of this is installed for you)

Everything the project needs is already configured by the scripts above. The
knobs below were examined in the 2026-07-26 performance review (v2 plan §8.1) and
deliberately left to you, because each is **host-global** — it affects every
container or VM on this machine, not just the bridge — and none was benchmarked
on real hardware. Skip them unless you have a measurement that says otherwise.

**KVM halt polling.** KVM spins the host CPU briefly every time a vCPU halts,
betting a wakeup is imminent. A mostly-idle Windows guest halts constantly, so
this can show as steady host CPU against the `qemu` process even when the guest
reports idle:

```bash
./tools/vcpu-profile.py --seconds 120  # measure BEFORE changing anything
echo 0 | sudo tee /sys/module/kvm/parameters/halt_poll_ns     # revertible at runtime
# to persist: echo 'options kvm halt_poll_ns=0' | sudo tee /etc/modprobe.d/kvm.conf
```

Use the profiler rather than `docker stats`: halt polling spins inside `KVM_RUN`
in **kernel** mode, so it can only ever show up in the profiler's `kernel` column,
and an aggregate percentage cannot tell you whether there is anything there to
recover. On the author's host that column is about 5% of one core, which is the
absolute ceiling on this knob and on every other host-side knob.

The cost is microseconds of extra wakeup latency, irrelevant at loopback-SMB
timescales. The gain may well be nil — KVM's polling is adaptive and the guest
already gets Hyper-V timer enlightenments — so measure both ways.

**Docker's userland proxy.** Ports published on `127.0.0.1` are serviced by a
`docker-proxy` process that copies every byte between host userspace and the
container. If `pidstat -p $(pgrep -f 'docker-proxy.*10445') 1` shows it consuming
real CPU during a large cold read, `"userland-proxy": false` in
`/etc/docker/daemon.json` makes Docker use iptables instead. Two warnings: it is
daemon-wide and can break other containers' localhost publishes on some Docker
versions, and applying it needs a daemon restart — **power the bridge off first**
(GUI, or `sudo icloud-bridge-power off`) so the CIFS mounts come down cleanly.
Do not "fix" this by mounting the container's IP directly; that abandons the
loopback-published-port topology the acceptance tests assert.

**Transparent hugepages.** QEMU asks for 2 MB mappings, so `madvise` (the default
almost everywhere) is already what you want:

```bash
cat /sys/kernel/mm/transparent_hugepage/enabled     # expect [madvise] or [always]
```

Do not go further to preallocated hugetlbfs: it pins the guest's whole RAM
allocation on the host permanently.

---

## Troubleshooting quick reference

**Before reporting any of these, attach a diagnostic report.** The GUI's Status
tab has **Save diagnostic report…** (and **Copy diagnostics** for pasting).
It records versions, lifecycle and container state, host-unit and helper
authorization results, and the bridge document versions and timestamps. Folder
names are replaced with placeholders unless you tick **Include folder names**,
and it never contains your share password, `/etc/credentials-icloud`, command
environments, Apple account data, or any file contents (v2 plan D37). It works
in every state, including setup and powered-off.

| Symptom | Cause | Fix |
|---|---|---|
| `error gathering device information … "/dev/kvm": no such file or directory` on `docker run` | Docker Desktop context active | §2 — install native Engine, `docker context use default` |
| `error gathering device information … "/dev/vhost-net"` on `docker compose up` | The `vhost_net` module is not loaded; the compose file passes that device through (D33) | `sudo modprobe vhost_net` and re-run `host/setup-prereqs.sh` so it persists. No `vhost_net` in your kernel? Delete the `/dev/vhost-net` line from `docker-compose.yml` |
| `mount error(22)` / *bad option* from `mount.cifs` after an upgrade | The kernel's cifs module does not know `rasize` (D33 assumes 5.15+) | Drop `rasize=16777216` from `Options=` in `/etc/systemd/system/mnt-icloud.mount`, `systemctl daemon-reload`, remount |
| `Failed to enable unit: Unit docker.service does not exist` from `setup-prereqs.sh` | Desktop's CLI present so Engine install was skipped; no host daemon | Use the fixed script (tests `dockerd`, not `docker`); or install Engine via `get.docker.com` then re-run |
| `permission denied … unix:///var/run/docker.sock` | Not in `docker` group in this session | §4 — relogin (or `newgrp docker` for one shell) |
| `newgrp: command not found` | `newgrp` not installed (minimal Ubuntu) | `sudo apt install util-linux-extra` |
| In the guest: *"running scripts is disabled on this system"* | PowerShell execution policy is `Restricted` by default | Launch via `powershell -ExecutionPolicy Bypass -NoProfile -File <script>` (§8) |
| `docker version` server shows the wrong number (Desktop's, not Engine's) | Context still `desktop-linux` | `docker context use default` |
| Guest install extremely slow / no KVM in logs | KVM not passed through (Desktop, or no `/dev/kvm`) | Confirm §2 + `kvm-ok`; run the KVM passthrough check in §6 |
| `04-bridge-agent.ps1`: *"exclusions.json is missing but this looks like an existing install"* | The config was lost while the task/share/state survived; writing an empty list would silently re-include everything | Finish provisioning, start the GUI, and use **Restore from backup…** on the Selective Sync tab (v2 plan D36) — the host keeps a copy at `~/.local/state/icloud-bridge-gui/exclusions-backup.json`. Failing that, write an explicitly chosen `C:\ProgramData\icloud-bridge\io\exclusions.json` and re-run |
| Selective Sync warns *"choices are not backed up on this computer"* | The bridge read or Apply worked; only the local D36 snapshot failed to write | Check permissions on `~/.local/state/icloud-bridge-gui/` and free space on `$HOME`. Nothing on the bridge is affected |
| Selective Sync says the saved copy is *newer* than the VM's configuration | Normal after a VM rebuild: the fresh guest reports revision 0 and the host deliberately keeps the better copy | **Restore from backup…** to push your choices back into the rebuilt VM |
| Tray icon shows yellow, `status.json` stale | The guest scheduled task is not running (it only runs in the logged-on `icloud` session) | Open `:8006`, confirm auto-logon happened; `Start-ScheduledTask icloud-bridge-agent` |
| An exclusion is stuck at `pending-dehydrate` | Cloud Files refuses to dehydrate content that is open, modified, or not yet uploaded | Wait for the upload; the item is already hidden and inaccessible from the host. See `docs/selective-sync.md` |
| An exclusion reports `acl-write-denied` | Provisioning step 4 (the agent's `RC,WDAC` grant) did not take, or that object has a protected DACL | Re-run `04-bridge-agent.ps1` as Administrator and read its protected-DACL report |
| *"The guest agent does not match this app"* (yellow banner) | The GUI was updated but `C:\ProgramData\icloud-bridge\agent.ps1` was not — a package upgrade cannot reach inside the guest (v2 plan D35). Everything still works | Re-run `04-bridge-agent.ps1` as Administrator; it copies the bundled agent over the installed one. Your exclusions are untouched |
| *"not speaking this app's bridge protocol"* (red banner); Apply and browsing disabled | Same cause, but the guest agent predates the version check entirely, so nothing will be written to it | Re-run `04-bridge-agent.ps1` as Administrator. The current `exclusions.json` is deliberately left exactly as it is until the versions agree |
| GUI shows *"could not be powered on/off within the time allowed"* and offers only **Retry** | The outer timeout fired. Killing this app's `sudo` is no proof the root helper stopped, so nothing is read or changed until you retry (v2 plan D38) | Give the helper a moment, then press **Retry** — `flock` serializes it against any surviving run. `journalctl -u icloud-health.timer` and the diagnostic report show what actually happened |
| After restarting the GUI mid-install it says *"Setup was interrupted while Windows was installing"* | The D39 record survived the restart, so the app resumed the no-CIFS Provisioning state instead of guessing | Continue the guest steps and choose **Check setup and connect**; the note clears itself once the bridge powers on |
| Setup offers **Discard failed setup record** | This app noted that a VM creation was started, but Docker says that container is absent or is a different one | Use it if you gave up on that attempt. It removes only this app's note — no container, no virtual disk, no `.env` |
| No tray icon on GNOME | GNOME has no built-in tray | Install the *AppIndicator and KStatusNotifierItem Support* extension |
| **Quit and power off VM** (or **Power off bridge**) aborts: *"a file operation … is still in progress"* | A mount is busy — an open file, a shell `cwd` inside `/mnt/icloud[_bridge]`, or a running copy; teardown refuses a lazy unmount | Close the holder (`lsof /mnt/icloud`, `fuser -m /mnt/icloud`) and try again; the VM stayed up the whole time |
| Health went red and no **Start bridge** action appeared | Red is not evidence the bridge is off — it also covers a stale canary, a missing mount, or unreadable JSON. Start is offered only when `docker inspect` definitively reports the container exited/created/dead | Read the failing check on the Status tab. If the VM really is stopped, the action appears within a refresh cycle |
| GUI stuck on *Starting Windows VM…* or shows a start error | The VM did not boot or its SMB was not ready within five minutes | Open `:8006`, confirm iCloud is signed in, then **Retry start**. The GUI never auto-retries or arms health against a dead mount |
| After a reboot the bridge is off and `/mnt/icloud` is empty | The GUI powered it off; the marker + unit conditions keep it down (intended, D29) | Launch the GUI (autostart does this at login), or `sudo /usr/local/bin/icloud-bridge-power on` |
| GUI: *"sudo: a password is required"* when starting/quitting | The power-helper `sudoers` grant names a different account, or was never installed | `sudo icloud-bridge-configure --user <your account>` — this works whether you installed from the repo or the package, and needs no `.env` if credentials already exist |
| GUI: *"Cannot inspect the Windows VM: … no such object …"* instead of the create-the-VM message | Pre-fix `power.py` matched Docker's error casing literally, so Docker 29's lowercase text was read as an inspect failure rather than "no container yet" | Update to a build containing the case-insensitive match; the guest itself is fine — run `docker compose up -d` if you have not created it yet |
| Setup assistant: *"could not find docker-compose.yml and provision/"* | The GUI cannot see an installation bundle — it never guesses from the working directory, because a desktop launcher has none | Re-run `./gui/install-gui.sh` (it copies the bundle to `~/.local/share/icloud-bridge-gui/resources`) or install the `.deb`, which ships `/usr/share/icloud-bridge` |
| Setup assistant: *"this GUI was installed from … which no longer contains host/setup-host.sh"* | The checkout recorded at install time was moved or deleted, so the printed `setup-host.sh` path would be wrong | Run the host setup from wherever the repository is now, or re-run `./gui/install-gui.sh` from there |
| Setup assistant: **Create Windows VM** stays greyed out | A check is failing, or a container named `icloud-windows` already exists — the assistant never creates one beside an existing container | Fix the red rows and press **Re-check**; if the container exists, close the assistant and let the app start it |
| The GUI says the VM does not exist (or health is red) while `docker ps` clearly shows `icloud-windows` running | Docker Desktop reset your active context to `desktop-linux`, and that daemon has never heard of this container | Fixed in the app: every GUI `docker` call now pins `DOCKER_HOST=unix:///var/run/docker.sock`. For your own shell, `docker context use default` |
