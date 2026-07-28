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
root-run setup script for your login, so select the Engine yourself:

```bash
docker context use default
docker context ls          # confirm: default *
```

**But do not rely on that staying selected.** Docker Desktop reclaims the active
context every time it starts, silently rewriting `currentContext` back to
`desktop-linux` in `~/.docker/config.json`. Any command you run afterwards
answers from Desktop's daemon, which has never heard of `icloud-windows`.

The GUI pins the native socket internally (`gui/icloud_bridge_gui/power.py`), so
it is unaffected by the active context. Use its **Create Windows VM** action for
the first container; the troubleshooting and appendix commands below are only
for diagnosed recovery work.

When you do run one of those recovery commands, pin the socket per command
rather than switching the global context — that leaves Desktop free to be your
default for every *other* project:

```bash
DOCKER_HOST=unix:///var/run/docker.sock docker compose up -d
```

The `Makefile` exports exactly that variable, so its wrappers are immune without
you typing it:

```bash
make vm-up      # start the guest        make vm-ps     # container state
make vm-down    # stop and remove it     make vm-logs   # follow its logs
```

Note the GUI's **Create Windows VM** runs compose as project `icloud-bridge`
(`-p icloud-bridge`); add that same flag to terminal compose commands aimed at
the GUI-created container so they address the same project.

### Warning: this project's Engine will absorb your other Docker work

Everything above describes one direction of the conflict — Desktop reclaiming the
context and hiding `icloud-windows` from you. **The mirror case is worse, because
it does not look like a failure.**

Installing this project leaves a native Engine running permanently (systemd
`docker.service`, enabled at boot). Whenever Docker Desktop is *not* running, the
active context falls back to `default` — and `default` is not a dead endpoint the
way it is on macOS, where Desktop's VM is the only daemon and closing it makes
`docker` fail outright. Here it is a second, fully working daemon that will
happily accept anything you send it:

```bash
# Desktop closed. In some unrelated project:
docker compose up -d
# → succeeds. Builds images, creates containers, binds ports.
#   All of it on the Engine this project reserved for the Windows VM.
```

Nothing warns you. You get a complete duplicate stack on the wrong daemon, and
from then on `docker ps` answers differently depending on whether Desktop happens
to be running. The usual way people notice is a port collision on the *next*
`up` — by which time containers with identical names exist on both daemons.

The real risk is not the confusion, it is shared bind mounts. If the duplicated
stack bind-mounts a database data directory from the host, the same files are now
reachable by two independent engines. Port conflicts are usually what stops both
copies from running at once; that is a coincidence, not a safeguard, and two
database servers opening one data directory will corrupt it.

**Recommended fix — pin the context in your shell profile**, so the fallback can
never happen silently:

```bash
# ~/.bashrc  (or ~/.zshrc)
export DOCKER_CONTEXT=desktop-linux
```

`DOCKER_CONTEXT` outranks the `currentContext` value in `~/.docker/config.json`
that Desktop rewrites on every start and quit, so Desktop can no longer redirect
your shell in either direction. With Desktop closed you now get a connection
error — the macOS behaviour — instead of a silent second stack.

This is safe for this project because **both** of its entry points address the
Engine explicitly rather than through the active context, and `DOCKER_HOST`
outranks `DOCKER_CONTEXT`:

- `host/icloud-bridge-power` — exports `DOCKER_HOST` (runs as root via `sudo -n`)
- `gui/icloud_bridge_gui/power.py` — pins `DOCKER_SOCKET` in `docker_env()`

Verify both halves after adding the pin:

```bash
docker ps                                          # Desktop only
DOCKER_HOST=unix:///var/run/docker.sock docker ps   # icloud-windows
```

Note this cannot be tightened into true isolation by dropping yourself from the
`docker` group. The root helper would survive it, but `power.py` runs its
`docker inspect` status check as your own user, so the GUI would lose its VM
state readout. On Linux the other daemon can be made unreachable *by default*,
but not made to not exist.

### Recovering a stack that ran on the wrong Engine

Moving a bind-mounted data directory back to Desktop is not just `compose up` —
expect permission failures, because the two daemons write host files differently:

- **Native Engine** runs containers directly, so files land with the container's
  own ids (e.g. `999:999` for `mysql`).
- **Desktop** shares host paths through a `virtiofsd` running as *your* user, so
  anything your account cannot read is invisible to every container (it appears
  as `-?????????`), and anything your account cannot *write* is read-only to
  them — regardless of the uid inside the container.

So a data directory written by the Engine typically has to be made group-owned by
you and group-writable before Desktop can serve it:

```bash
# from the project holding the bind mount; run via the Engine, which has real root
# note the double quotes: $(id -g) must expand on the host, not in the container
docker --context default run --rm -u 0 -v "$PWD/<data-dir>:/d" alpine \
  sh -c "chgrp -R $(id -g) /d && chmod -R g+w /d"
```

Purely derived files can fight the fix even after their metadata looks right
(a stale mapping keeps presenting them as `root:root`). Delete rather than repair
those — for MySQL, `ib_buffer_pool` is a warm-start hint that is rewritten on
every clean shutdown.

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

## 5. Configuration

The Setup assistant creates the normal configuration by default at
`$XDG_CONFIG_HOME/icloud-bridge/env` (usually `~/.config/icloud-bridge/env`).
It generates the share credential, chooses conservative machine-derived VM
sizes that you can edit before creation, and keeps the file private. Install
the GUI in §6 and choose **Create configuration** there.

Creating `.env` yourself is the advanced/manual path:

```bash
cp .env.example .env
$EDITOR .env
```

Set:

- `SHARE_PASS` — **generate a strong 20+ char random password.** This is the SMB
  account password. The app delivers this same value into the guest during setup
  (§9), from the configuration in use — the app-created file, or an env file you
  select at that moment; the manual fallback pastes it by hand instead. `.env`
  is gitignored — never commit it, and never put the real value in this file or
  any other tracked file.
- `DISK_SIZE` / `RAM_SIZE` / `CPU_CORES` — size per `.env.example` comments. The
  qcow2 grows on demand, so oversizing `DISK_SIZE` is free. Don't drop `RAM_SIZE`
  below 2.5G (Windows servicing needs it).

Sanity-check interpolation before booting:

```bash
docker compose config      # should print the resolved service with your .env values
```

---

## 6. Install the host GUI and tray icon

Install the GUI before a VM exists. It records the creation attempt and is the
documented way to create the Windows container.

**If you installed the package later in §10, the GUI is already there** — it
ships in the same `.deb`, at `/usr/bin/icloud-bridge-gui`. Skip to the GNOME note
below.

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

Run `./gui/install-gui.sh --uninstall` to remove this per-user install.

**GNOME users:** install the *AppIndicator and KStatusNotifierItem Support*
extension, or the tray icon will not be visible.

On a host with no VM yet, the GUI opens a Setup assistant (v2 plan D31) instead
of the status view: it re-checks the §2/§4 prerequisites, shows which
`docker-compose.yml` and `provision/` copy it resolved (package, per-user, or
this checkout — never the working directory), finds the conventional
configuration when it exists, and otherwise offers **Create configuration** with
editable VM sizes. **Use an existing .env** keeps the manual path available. It
then validates the file without ever reading the password out and offers
**Create Windows VM**. Select that action to create the VM; do not create its
container by hand.

The assistant then waits through the Windows install — it does not try to mount
anything — offers **Set up Windows automatically** to drive the §9 guest sequence
for you (the manual list stays behind **Show manual steps**), and hands back to
the power helper when you choose **Check setup and connect**.

The GUI is the bridge's on/off switch (v2 plan D29/D30). Launching it powers the
Windows VM on and mounts the shares; the tray's **Quit → Quit and power off VM**
cleanly disconnects both mounts and powers the VM off (leaving unuploaded changes
to resume next start). **Quit GUI only** leaves the bridge running. **Power off
bridge (keep this app running)** does the same teardown without exiting, and
**Start bridge** brings it back — that action also appears when the container is
found stopped mid-session, so a manual `docker stop` is recoverable from the app.
The tray's **Start when the computer starts** toggles the autostart entry. A reboot
while powered off stays off (a marker plus systemd conditions suppress the mounts
and health timer) until you log in with autostart enabled, or run
`sudo /usr/local/bin/icloud-bridge-power on`.

---

## 7. Create Windows VM (first run downloads Windows)

In the Setup assistant, choose **Create Windows VM** and then watch the install
live at **http://127.0.0.1:8006** (noVNC). The first creation downloads a
multi-gigabyte Windows 11 ISO and runs an unattended install — typically
**20–40 min**. The debloat step runs automatically via the `/oem` mount,
registers the provisioning watcher the app talks to (§9), and drops a
`NEXT-STEPS.txt` on the guest desktop. Wait for the Windows desktop to appear.

> **Do this now if you might ever rebuild the guest.** dockur deletes the ISO it
> downloaded partway through the install, so the only chance to keep it is
> *while the download is still running* — by the time this install finishes it
> is already gone. Run the hard-link rescue in §8 now; the reasoning can wait:
>
> ```bash
> docker exec icloud-windows ln -f /storage/tmp/win11x64.iso /storage/win11x64-keep.iso
> ```
>
> If it reports *no such file*, the download has not started yet — wait a moment
> and repeat. Repeating is safe, and §8 explains why you may need to: a restarted
> download lands on a new inode and leaves your link holding a stale partial.
> Skip all this only if you accept re-downloading the whole ISO.

Quick pre-boot KVM passthrough check (should print the device, not an error):

```bash
docker run --rm --device /dev/kvm alpine ls -l /dev/kvm
```

---

## 8. Keep the downloaded Windows ISO (avoid re-downloading)

**You normally never re-download.** The installed guest lives in
`/srv/icloud-vm/storage`, so ordinary GUI power changes boot the existing disk —
no reinstall, no download. A re-download only happens if you **wipe that storage
directory** to redo the install from scratch.

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

### Appendix: Reusing it for a clean reinstall

> **Warning:** this is a manual recovery procedure, not the normal setup path.
> A container created by hand has no GUI provisioning record, so the app can
> misclassify it and strand you. If a start fails on missing shares, use the
> app's Setup offer when available; until that recovery route lands, remove the
> hand-created container so the app can return to its Setup tab.

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

## 9. In-guest setup (the app drives it; the Apple sign-in is yours)

Install the GUI first (§6) if you have not already — it is what performs this
section. With the VM created and Windows installed, its **Setup** tab offers

> **Set up Windows automatically**

which installs iCloud for Windows, waits while you sign in, creates the SMB
share, installs the bridge agent, and then hands over to **Check setup and
connect**. The host stages the app's own current copies of the provisioning
scripts on a share the guest cannot write to, and an elevated task inside Windows
runs them; nothing is typed into the VM and no password is ever displayed
(v2 plan D40-D42, §4.1).

**Signing in to iCloud — Apple ID, two-factor authentication, and the iCloud
Drive toggle — is the only step you perform inside the guest.** It stays manual
deliberately: 2FA cannot be automated without risking an account lockout
(`CONTRIBUTING.md` Scope).

Every run **inspects before it changes anything**. It evaluates a fixed checklist
of guest invariants, repairs only the components that are missing or have
drifted, and evaluates the whole checklist again afterwards; a component it
cannot classify safely stops the run before any mutation and reports the exact
diagnosis instead of being guessed past (v2 plan D44, §4.2). Re-running it is
therefore always safe, and a healthy component is skipped rather than replayed.

### While it runs

The app shows the checklist and the work it plans to do, and follows the run
phase by phase. Two moments need you:

- **Sign in.** When the app says so, open the VM screen, launch iCloud, sign in
  with your Apple ID + 2FA, turn **iCloud Drive ON**, **leave Files On-Demand
  ON**, and leave Photos/Mail/Contacts/Calendar **OFF**. The app polls for the
  sync root and simply continues once it appears — there is no timeout on this
  step and nothing to click afterwards. (`./tools/watch-sync.sh` from the host is
  a cheap proxy for "the initial metadata population has stopped growing".)

  **Do not pin the library.** v1 told you to disable Files On-Demand and run
  `attrib +P -U`; live testing on 2026-07-22/23 disproved the premise behind
  that instruction — dataless placeholders *do* hydrate on demand over SMB — so
  v2 keeps Files On-Demand on and pins nothing. If you already pinned during an
  earlier run, the bridge agent clears that intent once on its first start,
  without evicting any content.
- **The share password**, but only when the run actually has to set it — a first
  run, a missing `syncshare` account, or an explicit reset. The app offers its
  conventional configuration when found, or lets you choose the advanced manual
  env file, then streams `SHARE_PASS` straight into the guest at that moment; it
  is never shown or put on a command line (v2 plan D41/D49). An ordinary repair
  never asks for it at all.

The share-credential row is **never green**. It reads *reset during this run* or
*preserved*, both qualified with the reason: Windows never reveals a password, so
this app cannot confirm it; connecting is the proof. That proof is the existing
**Check setup and connect** step, which is where a successful first run leads.

If you deliberately select a *different* password from the one this host already
mounts with, the app tells you so and prints the matching
`sudo icloud-bridge-configure --env-file …` follow-up (§10) — it cannot read or
write root's `/etc/credentials-icloud` itself.

### Re-running it later

**Re-run Windows provisioning…** is on the Status tab and in the tray menu, and
is also what the agent-skew and protocol-incompatible banners point at. It is
one action with one enablement rule, deliberately available while the bridge
protocol is `skewed` or `incompatible` — that is exactly when you need it
(v2 plan D35).

Its confirmation, *Re-run Windows provisioning?*, carries an **unchecked**
**Reset share password from an env file** option. Leave it off unless the
password itself is wrong: the ordinary repair keeps the working credential and
never asks for your `.env`. The confirmation also states plainly that
`/mnt/icloud` and `/mnt/icloud_bridge` **stay mounted** — the app pauses its own
bridge reads, but it does not unmount them and cannot stop another program from
using them. **Close files, transfers and shells under `/mnt/icloud*` before
continuing.**

Because the run reconciles rather than replays, an agent-build mismatch on an
otherwise healthy VM renders exactly `Planned: Update bridge agent`: no env file
is requested, no password is reset, and the share and ACL boundaries are left
alone.

### One-time bootstrap on a VM created before automated provisioning

A VM installed before this feature has no watcher task inside it, so a staged run
is simply never picked up. That is **not an error** — the app checks the watcher
beacon before staging (and keeps a 90-second acknowledgement fallback for
pre-beacon watchers), and when no watcher is present it shows this command to
run **once**, in an elevated PowerShell inside the guest:

```
powershell -ep bypass -File C:/OEM/watcher.ps1 -Install
```

The forward slashes are deliberate: Windows accepts them, and they stay typeable
through noVNC on a mismatched keyboard layout. On a pre-feature VM with no
`C:\OEM` payload, use the share copy instead:

```
powershell -ExecutionPolicy Bypass -NoProfile -File \\host.lan\Provision\watcher.ps1 -Install
```

RDP is the comfortable route for either: connect a real RDP client to
127.0.0.1:3389 for a working clipboard and your own keyboard layout.

The installer checks its own elevation and that the `icloud` account really is an
administrator before registering anything. Once it has run, the watcher picks up
the request that is already staged — you do not click anything on the host again.
VMs created from this repo afterwards register the watcher during the OEM
install, so this step never applies to them.

### Fallback: the manual script sequence

The scripts are still exactly what the automated run executes, and running them
by hand is still supported — for a diagnosed provisioning failure, or on a guest
you would rather configure yourself. The Setup tab keeps them one click away
under **Show manual steps**.

> **Scripts are blocked by default.** Windows 11 sets the PowerShell execution
> policy to `Restricted`, so launching a `.ps1` directly fails with *"running
> scripts is disabled on this system"*. Prefix each launch below with
> `powershell -ExecutionPolicy Bypass -NoProfile -File`, or run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force` once per
> window. (`01-debloat.ps1` ran unattended only because `install.bat` already
> invokes it that way.)

**Use the right copy.** Every provisioning run refreshes an administrator-only
`C:\ProgramData\icloud-bridge-provision\current` with the bundle this app is
shipping, so that directory is the one to run from once the app has provisioned
this VM at least once. `C:\OEM` is the copy dockur made at install time and is
never updated afterwards — the author's live VM was found four commits behind,
missing the skew detection it was supposed to have — so treat it as the starting
point on a VM the app has never provisioned, and nothing more (v2 plan D42).

On the guest desktop (web viewer, or RDP to `127.0.0.1:3389`), open **PowerShell
as Administrator**, set `$P` to whichever of those two directories applies, and
follow plan §5–§7:

1. Install the client. This is a single command, so paste it directly:
   ```powershell
   winget install --id 9PKTQ5699M62 --source msstore --accept-package-agreements --accept-source-agreements
   ```
   (`9PKTQ5699M62` is iCloud's Store product ID. The msstore source only matches
   on that ID, not the `AppleInc.iCloud` moniker — verified 2026-07-22.)
   Or install "iCloud" from the Microsoft Store if winget/msstore errors.
2. Launch iCloud, **sign in + 2FA**. Turn **iCloud Drive ON**, **leave Files
   On-Demand ON**, leave Photos/Mail/Contacts/Calendar **OFF**, and do not pin
   anything (see the note above).
3. Edit `03-create-share.ps1`, replace the `STRONG_PASSWORD_HERE` placeholder
   with the **same** `SHARE_PASS` from your host `.env`, then run it:
   ```powershell
   notepad $P\03-create-share.ps1
   powershell -ExecutionPolicy Bypass -NoProfile -File $P\03-create-share.ps1
   ```
   Creates the `syncshare` account and shares the sync root over SMB.
4. Install the bridge agent, the control share, and Access-Based Enumeration on
   the data share:
   ```powershell
   powershell -ExecutionPolicy Bypass -NoProfile -File $P\04-bridge-agent.ps1
   ```
   It prints `bridge ready` once the scheduled task is running and a fresh
   `status.json` has appeared. It takes `agent.ps1` from beside itself, so run it
   from a directory that holds both — either of the two above does.

---

## 10. Mount on the host and verify

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

**Before you browse `/mnt/icloud` in a desktop file manager**, decide what may
read file *content* there. Enumerating directories and reading names, sizes and
timestamps is metadata and does not download anything. Opening content does, and
plenty of software opens content without being asked to: thumbnailers and
preview panes, media metadata probes, checksum and backup tools, antivirus
scanners, and desktop content indexers (`tracker-miner-fs`, `baloo`, and the
like). Any of those walking the mount hydrates the library file by file, over
the network, into the guest disk. Turn thumbnails, previews and indexing off for
network locations if your desktop offers that, and exclude folders you never
want fetched. These are desktop-wide user preferences, so nothing in this
project changes them for you.

---

## Appendix: Taking the data-path work onto a guest built before 2026-07-26

**Read this if your container predates that date.** Two performance decisions —
D32 (SMB signing off) and D33 (`/dev/vhost-net`) — shipped on 2026-07-26, and
neither can reach a container or a guest that already exists:

- `docker-compose.yml` gained the `/dev/vhost-net` device, but Docker only
  applies a device list when it **creates** a container. A container started
  before that line keeps running without it, and dockur silently falls back to
  userspace virtio, so QEMU copies every SMB byte through its own main loop.
- `03-create-share.ps1` gained `RequireSecuritySignature $false`, but that runs
  inside the guest, so re-running it is the only way an existing guest gets it.
  While signing is on, Windows HMACs every byte on both ends of a path where
  there is nothing to protect (the host is the security boundary, D9, and the
  ports are loopback-only). Whether your guest currently has signing on is worth
  checking rather than assuming — see the note below the commands.

Do it in this order. Do **not** `docker rm` or `docker kill` a live bridge —
that is what the ordered teardown exists to prevent:

> **Warning:** these are manual recovery commands, not the setup path. A
> hand-created container has no GUI provisioning record, so the app can
> misclassify it and strand you. If a start fails on missing shares, use the
> app's Setup offer when available; until that recovery route lands, remove the
> hand-created container so the app can return to its Setup tab.

```bash
sudo icloud-bridge-power off       # or Quit from the GUI; unmounts first, then stops the VM
docker compose up -d               # recreates the container, now with /dev/vhost-net
./host/acceptance-tests.sh         # section 2 must report the device AND vhost=on
```

Then reconcile the guest half from the app: **Re-run Windows provisioning…**
(Status tab or tray menu). The data-share check covers the D32 signing and
encryption settings, so a guest that still has signing on is repaired as ordinary
drift — with the credential preserved, and without touching the agent or the ACL
boundaries if they are already correct (§9). The manual equivalent, if you prefer
it, is step 3 of the fallback sequence in §9; it is idempotent, which is why
either route is safe.

`make acceptance` proves the D33 half from the host, and it is the half that is
definitely worth doing: a container that predates the compose line provably
cannot have the device, because Docker sets devices only at create time.

The D32 half has **no scripted host-side check**, but it does have a real one:
connect with a genuine SMB client that refuses to sign. On 2026-07-27 the
author's guest accepted `smbclient --option='client signing=disabled'` sessions
both before and after a cold boot, which means the server does **not** require
signing — and retires the raw-packet probe's four "signing required" readings
(2026-07-26 entry in `CHANGELOG.md`) as the probe's own artifact. The direct
form of the question is still the guest's to answer:

```powershell
Get-SmbServerConfiguration | Select-Object RequireSecuritySignature, EncryptData, RejectUnencryptedAccess
```

All three should read `False` (D32: off, deliberately — do not "harden" them).
If any is `True`, `03-create-share.ps1` sets them. Temper the throughput
expectation: on the author's host the recreate produced **no measurable change**
in warm 20 MB reads through userland `smbclient` (overlapping distributions,
medians 653 vs 426 MB/s — see the 2026-07-27 entry in `CHANGELOG.md`). The
measurement that matters is the kernel-cifs E0 read on large files, which needs
the real mount.

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
| `Permission denied` creating a file or folder at the **top level** of `/mnt/icloud`, while subfolders accept writes | The sync root carries the DOS read-only attribute, which the cifs client maps to mode `0555` and then uses to refuse the create locally — the guest never sees it (v2 plan D50) | Confirm `Options=` in `/etc/systemd/system/mnt-icloud.mount` contains `noperm`; if not, reinstall the package (or copy the repo's unit over it), `systemctl daemon-reload`, then remount. Do **not** clear the attribute in Windows: the shell re-applies it |
| `Failed to enable unit: Unit docker.service does not exist` from `setup-prereqs.sh` | Desktop's CLI present so Engine install was skipped; no host daemon | Use the fixed script (tests `dockerd`, not `docker`); or install Engine via `get.docker.com` then re-run |
| `permission denied … unix:///var/run/docker.sock` | Not in `docker` group in this session | §4 — relogin (or `newgrp docker` for one shell) |
| `newgrp: command not found` | `newgrp` not installed (minimal Ubuntu) | `sudo apt install util-linux-extra` |
| In the guest: *"running scripts is disabled on this system"* | PowerShell execution policy is `Restricted` by default | Launch via `powershell -ExecutionPolicy Bypass -NoProfile -File <script>` (§9) |
| `docker version` server shows the wrong number (Desktop's, not Engine's) | Context still `desktop-linux` | `docker context use default` |
| Guest install extremely slow / no KVM in logs | KVM not passed through (Desktop, or no `/dev/kvm`) | Confirm §2 + `kvm-ok`; run the KVM passthrough check in §7 |
| `04-bridge-agent.ps1`: *"exclusions.json is missing but this looks like an existing install"* | The config was lost while the task/share/state survived; writing an empty list would silently re-include everything | Finish provisioning, start the GUI, and use **Restore from backup…** on the Selective Sync tab (v2 plan D36) — the host keeps a copy at `~/.local/state/icloud-bridge-gui/exclusions-backup.json`. Failing that, write an explicitly chosen `C:\ProgramData\icloud-bridge\io\exclusions.json` and re-run |
| Selective Sync warns *"choices are not backed up on this computer"* | The bridge read or Apply worked; only the local D36 snapshot failed to write | Check permissions on `~/.local/state/icloud-bridge-gui/` and free space on `$HOME`. Nothing on the bridge is affected |
| Selective Sync says the saved copy is *newer* than the VM's configuration | Normal after a VM rebuild: the fresh guest reports revision 0 and the host deliberately keeps the better copy | **Restore from backup…** to push your choices back into the rebuilt VM |
| Tray icon shows yellow, `status.json` stale | The guest scheduled task is not running (it only runs in the logged-on `icloud` session) | Open `:8006`, confirm auto-logon happened; `Start-ScheduledTask icloud-bridge-agent` |
| An exclusion is stuck at `pending-dehydrate` | Cloud Files refuses to dehydrate content that is open, modified, or not yet uploaded | Wait for the upload; the item is already hidden and inaccessible from the host. See `docs/selective-sync.md` |
| An exclusion reports `acl-write-denied` | Provisioning step 4 (the agent's `RC,WDAC` grant) did not take, or that object has a protected DACL | **Re-run Windows provisioning…** — the bridge-boundary repair re-applies the grant, and a protected child DACL is reported as blocked with the exact paths so you restore inheritance deliberately (§9) |
| *"The guest agent does not match this app"* (yellow banner) | The GUI was updated but `C:\ProgramData\icloud-bridge\agent.ps1` was not — a package upgrade cannot reach inside the guest (v2 plan D35). Everything still works | Use the banner's **Re-run Windows provisioning…** button. On a healthy VM the plan is just *Update bridge agent*: no password is asked for and your exclusions are untouched. A VM created before automated provisioning needs the one-time bootstrap in §9 first |
| *"not speaking this app's bridge protocol"* (red banner); Apply and browsing disabled | Same cause, but the guest agent predates the version check entirely, so nothing will be written to it | The same **Re-run Windows provisioning…** action — it stays available in this state precisely because it is the way out. The current `exclusions.json` is deliberately left exactly as it is until the versions agree |
| Provisioning runs but the VM never acknowledges it (the app shows a one-line bootstrap command — immediately when no watcher beacon exists, or after ~90 s on a pre-beacon watcher) | The VM was created before automated provisioning, so it has no watcher task. Not an error — the app is still polling | Run that command once in an elevated PowerShell inside the VM (§9). The already-staged request is then picked up with no further click on the host |
| GUI shows *"could not be powered on/off within the time allowed"* and offers only **Retry** | The outer timeout fired. Killing this app's `sudo` is no proof the root helper stopped, so nothing is read or changed until you retry (v2 plan D38) | Give the helper a moment, then press **Retry** — `flock` serializes it against any surviving run. `journalctl -u icloud-health.timer` and the diagnostic report show what actually happened |
| After restarting the GUI mid-install it says *"Setup was interrupted while Windows was installing"* | The D39 record survived the restart, so the app resumed the no-CIFS Provisioning state instead of guessing | Continue the guest steps and choose **Check setup and connect**; the note clears itself once the bridge powers on |
| Setup offers **Discard failed setup record** | This app noted that a VM creation was started, but Docker says that container is absent or is a different one | Use it if you gave up on that attempt. It removes only this app's note — no container, no virtual disk, no `.env` |
| No tray icon on GNOME | GNOME has no built-in tray | Install the *AppIndicator and KStatusNotifierItem Support* extension |
| **Quit and power off VM** (or **Power off bridge**) aborts: *"a file operation … is still in progress"* | A mount is busy — an open file, a shell `cwd` inside `/mnt/icloud[_bridge]`, or a running copy; teardown refuses a lazy unmount | Close the holder (`lsof /mnt/icloud`, `fuser -m /mnt/icloud`) and try again; the VM stayed up the whole time |
| Health went red and no **Start bridge** action appeared | Red is not evidence the bridge is off — it also covers a stale canary, a missing mount, or unreadable JSON. Start is offered only when `docker inspect` definitively reports the container exited/created/dead | Read the failing check on the Status tab. If the VM really is stopped, the action appears within a refresh cycle |
| GUI stuck on *Starting Windows VM…* or shows a start error | The VM did not boot or its SMB was not ready within five minutes | Open `:8006`, confirm iCloud is signed in, then **Retry start**. The GUI never auto-retries or arms health against a dead mount |
| After a reboot the bridge is off and `/mnt/icloud` is empty | The GUI powered it off; the marker + unit conditions keep it down (intended, D29) | Launch the GUI (autostart does this at login), or `sudo /usr/local/bin/icloud-bridge-power on` |
| GUI: *"sudo: a password is required"* when starting/quitting | The power-helper `sudoers` grant names a different account, or was never installed | `sudo icloud-bridge-configure --user <your account>` — this works whether you installed from the repo or the package, and needs no `.env` if credentials already exist |
| GUI: *"Cannot inspect the Windows VM: … no such object …"* instead of the create-the-VM message | Pre-fix `power.py` matched Docker's error casing literally, so Docker 29's lowercase text was read as an inspect failure rather than "no container yet" | Update to a build containing the case-insensitive match; the guest itself is fine — use **Create Windows VM** if you have not created it yet |
| Setup assistant: *"could not find docker-compose.yml and provision/"* | The GUI cannot see an installation bundle — it never guesses from the working directory, because a desktop launcher has none | Re-run `./gui/install-gui.sh` (it copies the bundle to `~/.local/share/icloud-bridge-gui/resources`) or install the `.deb`, which ships `/usr/share/icloud-bridge` |
| Setup assistant: *"this GUI was installed from … which no longer contains host/setup-host.sh"* | The checkout recorded at install time was moved or deleted, so the printed `setup-host.sh` path would be wrong | Run the host setup from wherever the repository is now, or re-run `./gui/install-gui.sh` from there |
| Setup assistant: **Create Windows VM** stays greyed out | A check is failing, or a container named `icloud-windows` already exists — the assistant never creates one beside an existing container | Fix the red rows and press **Re-check**; if the container exists, close the assistant and let the app start it |
| The GUI says the VM does not exist (or health is red) while `docker ps` clearly shows `icloud-windows` running | Docker Desktop reset your active context to `desktop-linux`, and that daemon has never heard of this container | Fixed in the app: every GUI `docker` call now pins `DOCKER_HOST=unix:///var/run/docker.sock`. For your own shell, `docker context use default` |
