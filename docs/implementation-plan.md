# iCloud Drive on Linux via Minimal Windows VM — Implementation Handoff

**Version:** 1.1 · **Status:** Ready to execute · **Audience:** an executor (human or model) who follows instructions literally. All decisions are already made. Do not substitute components unless a step explicitly offers a fallback.

**v1.1 change note (2026-07-23).** This document is amended by
[`plan-gui-selective-sync.md`](plan-gui-selective-sync.md) ("the v2 plan"),
which adds a host GUI, a guest bridge agent, and selective sync. Where the two
disagree, **v2 wins**. The changes folded in here are:

- **D5 is disproven and superseded** — see the D5 row and §6. Files On-Demand
  stays **on**, nothing is pinned, and placeholders hydrate on read.
- The sizing rule (§1) no longer assumes the whole library is resident.
- §2's layout is the real repository, including the v2 paths.
- §7's share script grants at the root only, not `/T`.
- §8 gains the bridge control mount.
- §11's pinned-file check is replaced by the E0 gate.
- §13 is new: it lists the v2 artifacts and where each one's truth lives.

**v1.2 change note (2026-07-26).** §3–§9 no longer embed copies of
`docker-compose.yml`, the provisioning scripts, the systemd units or
`icloud-health.sh`. Each file is now the single source of truth and this
document keeps only the reasoning and the operator sequence around it. Seven of
the eleven embedded copies had already drifted from their files, and every edit
had to be made twice — which also made the plan the worst merge-conflict point
in the repository when more than one agent is working. No decision changed.

---

## 0. Purpose and success criteria

Run Apple's official **iCloud for Windows** client inside a stripped-down Windows 11 VM on a Linux host, and expose the synced iCloud Drive folder to the Linux host as a mounted directory.

Why this approach (context for the executor; do not deviate): every native-Linux iCloud tool relies on reverse-engineered web APIs, requires Advanced Data Protection (ADP) to be **off**, and suffers 30–60 day session expiry. The official Windows client is a trusted Apple client: ADP can stay **on**, sessions last months, and Apple maintains the sync engine.

**Success =** all acceptance tests in §11 pass.

### Decisions register (locked)

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | Hypervisor | KVM/QEMU | Near-native performance, native to Linux |
| D2 | VM management | `dockur/windows` container | Automated unattended install, VirtIO drivers injected, reproducible via one compose file |
| D3 | Windows edition | Stock **Windows 11 Pro** (`VERSION: "11"`), debloated post-install by script | Avoids building a custom ISO (tiny11builder requires a Windows machine to run DISM — chicken-and-egg). Runtime debloat recovers most of the savings |
| D4 | iCloud install method | `winget` from the `msstore` source; manual Store install as fallback | Scriptable |
| D5 | Files On-Demand | ~~**Disabled**, folder pinned with `attrib +P`~~ — **DISPROVEN 2026-07-22/23, superseded by v2 D14/D25/D26.** Files On-Demand stays **ON** and this project pins nothing | The premise ("placeholders served over SMB stall or fail") is false. Live tests against the running guest hydrated dataless placeholders on demand for SMB reads, with correct content and checksums, exactly like OneDrive. Evidence: v2 plan §0.5; scripts `tools/test-smb-hydration.ps1` and `tools/test-smb-read.sh`. Pinning is therefore unnecessary, and avoiding it removes the requirement to hold the entire library on the guest disk. The first run of the guest bridge agent clears any legacy `P` intent with `attrib -P`, **without** requesting eviction of content already on disk. It never runs a global `+U`. Cold reads block for the duration of the download — that trade is gated by E0 (§11) |
| D6 | Guest→host export | SMB share **from the guest**, mounted on host via `cifs` + systemd automount | Only live-bidirectional option: host writes land directly in the Cloud Files sync root and upload immediately. (A robocopy mirror to a host folder was considered and rejected: one-way, polling delay, dangerous for bidirectional) |
| D7 | Sync root location | Guest-local NTFS virtual disk (default `%USERPROFILE%\iCloudDrive`) | Cloud Files API requires local NTFS with reparse points; cannot point it at a network path |
| D8 | Share account | Dedicated local Windows user `syncshare` (SMB only), separate from the auto-logon user | Auto-logon user has a blank password (required for unattended logon); SMB must be password-protected |
| D9 | Port exposure | All ports bound to `127.0.0.1` on the host only | VM holds an authenticated Apple session; never expose to LAN |
| D10 | Resources | 2 vCPU, 3 GB RAM, disk = 40 GB + iCloud data size (see §2) | Measured floor for debloated Win11 + headroom |
| D11 | Defender | Keep enabled; **exclude** the iCloud folder and (amended by v2) the bridge control directory `C:\ProgramData\icloud-bridge\io`; disable scheduled scans. Never exclude `powershell.exe` as a process | Full disable fights Tamper Protection; exclusion captures ~all of the CPU win. The bridge directory is rewritten every 15 s by the agent and written over SMB by the host, so scanning each write is pure overhead; it holds only JSON, and the executable agent plus its private state live outside it (v2 D27). The guest holds a live Apple session, so real-time protection and interpreter coverage stay on |
| D12 | Windows Update | Notify-only, no auto-reboot | An unattended reboot mid-sync is the top availability risk |
| D13 | Monitoring | Host-side systemd timer: mount check + write-canary + freshness check | Simple, no external dependencies |

**Licensing note (tell the operator, then proceed):** dockur/windows downloads official ISOs and uses Microsoft's generic trial keys; it does not activate Windows. Unactivated Windows 11 runs indefinitely with a watermark and personalization limits — functionally fine for this headless use. The operator is responsible for license compliance and may enter a real Pro key later.

---

## 1. Host prerequisites

Target host: any x86-64 Linux with KVM. Commands below assume Debian/Ubuntu; adapt package manager only.

```bash
# 1. Verify KVM is available (must print an "acceleration can be used" style OK)
sudo apt-get install -y cpu-checker
kvm-ok

# 2. Docker Engine (NOT Docker Desktop — Desktop cannot pass /dev/kvm)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # re-login after this

# 3. CIFS mount support
sudo apt-get install -y cifs-utils

# 4. Storage: choose the FASTEST disk available (NVMe preferred) for the VM image.
#    The iCloud mirror's small-file writes during sync bursts are the only
#    stressed I/O in this system.
sudo mkdir -p /srv/icloud-vm/storage

# 5. Accelerated virtio networking (v2 plan D33). §3 passes /dev/vhost-net into
#    the container; the node must exist before `docker compose up`.
sudo modprobe vhost_net
echo vhost_net | sudo tee /etc/modules-load.d/icloud-bridge-vhost-net.conf
```

**Sizing rule (D10, amended by v2 D25/D26):** size the disk for **Windows plus
the cached working set you expect**, not for the whole library. With Files
On-Demand on and nothing pinned, files occupy guest disk only after something
reads them, and the reclamation sweep releases the coldest in-sync content when
free space drops below 20 GB, aiming for 30 GB.

`DISK_SIZE: "120G"` is the selected starting size for the measured 101 GB
library — it is a starting point, not a promise that 120 GB suits every library.
Reclamation cannot free content that is open, modified, or still uploading, so
a disk that is too small for the live working set will sit below the floor and
report yellow. Growing the disk (§10) stays in the runbook.

The qcow2 image grows on demand, so oversizing costs nothing upfront. RAM: 3 GB allocated (do not go below 2.5 GB; Windows update servicing needs it). Host must therefore have ≥ 3 GB free RAM permanently.

---

## 2. Project repository layout

This is the actual repository, including the v2 additions (v2 plan D24):

```
icloud-vm-for-linux/
├── Makefile                      # dev/operator entry points; `make` lists them (§14)
├── docker-compose.yml
├── .env                          # operator-specific values (gitignored)
├── .env.example
├── CONTRIBUTING.md               # canonical contributor and coding-agent rules
├── README.md                     # overview, usage, selective-sync summary
├── SETUP.md                      # annotated real-machine runbook
├── CHANGELOG.md                  # improvement + investigation log
├── .githooks/                    # installed by `make hooks` (core.hooksPath)
│   ├── pre-commit                # hygiene + pytest over the staged tree
│   └── commit-msg                # subject shape, no attribution footer
├── packaging/                    # .deb build (§14); no debhelper needed
│   ├── build-deb.sh              # stages the tree, dpkg-deb --root-owner-group
│   ├── lint-ps1.ps1              # PS7 parse + analyzer pass, run by `make lint-ps`
│   └── deb/                      # control.in, postinst, prerm, postrm,
│                                 #   the /usr/bin launcher, lintian-overrides
├── provision/                    # run INSIDE the Windows guest
│   ├── install.bat               # dockur OEM bootstrap (auto-runs 01)
│   ├── 01-debloat.ps1            # auto-run; no network, no secrets
│   ├── 02-install-icloud.ps1     # operator-run
│   ├── 03-create-share.ps1       # operator-run; placeholder password by design
│   ├── 04-bridge-agent.ps1       # operator-run; bridge share, agent task, ABE, ACLs
│   └── agent.ps1                 # byte-identical copy of guest-agent/agent.ps1
├── guest-agent/
│   └── agent.ps1                 # source of truth for the guest agent
├── gui/                          # host GUI + tray icon (PySide6)
│   ├── icloud_bridge_gui/        # health.py, bridge.py, power.py, autostart.py,
│   │                            #   tray.py, window.py, __main__.py, icons/
│   ├── tests/                    # pytest: test_health/bridge/power/autostart.py
│   ├── install-gui.sh
│   ├── icloud-bridge-gui.desktop
│   └── autostart/icloud-bridge-tray.desktop
├── host/                         # Linux host
│   ├── setup-prereqs.sh          # docker + cifs-utils + KVM check (§1)
│   ├── setup-host.sh             # places units (§8–§9), power helper + marker,
│   │                            #   then delegates to icloud-bridge-configure
│   ├── icloud-bridge-configure   # the machine-specific half, shared by the
│   │                            #   from-source and .deb install paths (§14)
│   ├── acceptance-tests.sh       # host-checkable subset of §11
│   ├── icloud-bridge-power       # root helper: on/off the whole bridge (D29)
│   ├── mnt-icloud.mount
│   ├── mnt-icloud.automount
│   ├── mnt-icloud_bridge.mount   # v2 control share (v2 plan D16)
│   ├── mnt-icloud_bridge.automount
│   ├── icloud-health.sh
│   ├── icloud-health.service
│   └── icloud-health.timer
│   #  all six units carry ConditionPathExists=!/var/lib/icloud-bridge/powered-off
├── tools/                        # host-side helpers for driving/verifying the guest
│   ├── hygiene-checks.sh         # the mechanical AGENTS rules; `make lint` + hook
│   ├── install-hooks.sh          # `make hooks` — sets core.hooksPath
│   ├── guest-ctl.sh, qemu-monitor.py, rdp-ready.py, keep-iso.sh
│   ├── watch-sync.sh, icloud-status.ps1, icloud-folders.ps1
│   └── test-smb-hydration.ps1, test-smb-read.sh   # the D5 evidence
└── docs/
    ├── implementation-plan.md    # this document
    ├── plan-gui-selective-sync.md # the v2 plan (amends this document; v2 wins)
    ├── selective-sync.md         # user page + deployment checklist
    └── automation-notes.md       # first-run record: what was manual and why
```

`agent.ps1` exists twice on purpose: `guest-agent/agent.ps1` is the source of
truth, and `provision/agent.ps1` is the copy dockur places in `C:\OEM` at install
time. `host/acceptance-tests.sh` §8 fails if the two diverge.

`.env` contents (operator fills in; `SHARE_PASS` must be strong — 20+ random chars):

```
DISK_SIZE=120G
RAM_SIZE=3G
CPU_CORES=2
SHARE_PASS=CHANGE_ME_STRONG_PASSWORD
```

---

## 3. docker-compose.yml

**Source of truth: [`docker-compose.yml`](../docker-compose.yml).** It carries a
comment for every non-obvious setting; what follows is the part that belongs to
the plan rather than to the file.

- `dockur/windows` rather than a hand-written QEMU command line or a
  purpose-built image (D2): unattended install, VirtIO drivers injected, one
  file to reproduce the whole guest.
- **Every published port is bound to `127.0.0.1`** (D9). The guest holds an
  authenticated Apple session, so nothing here may be reachable off the host.
  `tools/hygiene-checks.sh` rejects a commit that changes this and
  `host/acceptance-tests.sh` §3 re-checks the running container.
- `/dev/vhost-net` moves virtio packet processing into a host kernel thread
  instead of QEMU's main loop, which is what makes SMB throughput acceptable
  (v2 plan D33). Without the host module the container will not start and the
  line can be removed.
- `./provision:/oem` is how the guest gets `C:\OEM`: dockur copies the folder in
  and runs `install.bat` as Administrator at the end of installation, so §4 runs
  unattended. §5 and §7 stay operator-run — one needs the Store, the other needs
  a secret (D10).
- Operator values (`DISK_SIZE`, `RAM_SIZE`, `CPU_CORES`, and `SHARE_PASS` for
  §7) come from `.env`. The compose file never contains a credential.

Notes for the executor:
- dockur forwards published container ports to the guest VM, which is how host `127.0.0.1:10445` reaches the guest's SMB service on 445.
- Do **not** add `privileged: true` unless the container reports `/dev/kvm` missing despite `kvm-ok` passing.
- First start: `docker compose up -d`, then open `http://127.0.0.1:8006` in a browser (SSH port-forward `-L 8006:127.0.0.1:8006` if the host is remote). Installation is fully automatic; wait until a Windows desktop is visible (typically 15–40 min depending on bandwidth).

---

## 4. Guest provisioning — script 01: debloat

This script runs unattended: dockur copies `./provision` to `C:\OEM` and `install.bat` invokes it as Administrator at the end of installation (see §3's `/oem` volume). Nothing to do by hand on a normal install; to re-run it after a Windows feature update (§10), open the web viewer (or RDP as user `icloud`, blank password), start **PowerShell as Administrator** (right-click Start → Terminal (Admin)) and run `C:\OEM\01-debloat.ps1`, then `Restart-Computer` and wait for the
desktop to return (auto-logon).

**Source of truth: [`provision/01-debloat.ps1`](../provision/01-debloat.ps1).**
It is commented item by item, including a "deliberately left alone" list that
records what must *not* be trimmed later. The decisions behind it:

- Everything it removes targets one of two costs: resident RAM in a small guest
  whose desktop session stays logged on forever, and background disk churn that
  inflates the thin-provisioned qcow2 on the host (v2 plan §8.1).
- **Never remove the Microsoft Store, the AppX/MSIX stack, WebView2, or Windows
  servicing components** (D3). iCloud is a Store app; its installation and its
  updates depend on all of them. Additions to the script's `$bloat` list must be
  inbox apps only.
- OneDrive is removed as an *app* only. `cldflt.sys`, the Cloud Files filter
  driver, is inbox and is what iCloud's own placeholders run on — never disable
  `CldFlt`.
- Defender real-time protection stays on (D11).

---

## 5. Guest provisioning — script 02: install iCloud

PowerShell as Administrator again. Windows 11 ships with the PowerShell execution
policy set to `Restricted`, so launching a `.ps1` directly fails with *"running
scripts is disabled on this system"*. Invoke it with an explicit bypass — the same
way `install.bat` runs script 01:

```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\02-install-icloud.ps1
```

(Or set it for the current window only: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force`.
This step is a single command, so you can also just paste the `winget` line from
the script.)

**Source of truth: [`provision/02-install-icloud.ps1`](../provision/02-install-icloud.ps1)** —
one `winget` line plus a verification. With `--source msstore` the `--id` is the
Store *product* ID, not the AppX package name: `AppleInc.iCloud` there fails with
"No package found matching input criteria".

Confirmed on the first real run (2026-07-22): `winget search iCloud` lists
`iCloud → 9PKTQ5699M62 → msstore`, and the install reports
`Successfully installed` with `AppleInc.iCloud 15.8.118.0`. The `winget` source
also carries `Apple.iCloud`, but that is the **legacy** standalone build
(7.21.x) — not the Store client this design assumes.

**Fallback (if winget/msstore errors, e.g. region or Store-source issues):** open Microsoft Store from the Start menu, search "iCloud", install manually. No Microsoft account sign-in is required for free apps — if prompted, choose the option to proceed without signing in.

Verify: `Get-AppxPackage AppleInc.iCloud` returns a package.

---

## 6. Apple ID login and iCloud configuration (manual, one-time)

Do this via the **web viewer** (`http://127.0.0.1:8006`) so 2FA prompts are visible.

1. Launch **iCloud** from the Start menu.
2. Sign in with the Apple ID and password.
3. A 2FA code is pushed to the operator's trusted Apple devices; enter it. (If ADP is enabled on the account, additional trusted-device approval prompts may appear — approve them on the phone/Mac.)
4. When asked to trust this browser/device, choose **Trust**.
5. In the iCloud app settings:
   - **iCloud Drive: ON.**
   - **Leave Files On-Demand ON.** (This reverses v1's instruction — see the D5
     row. Do not turn it off, and do not pin anything.)
   - Photos, Passwords, Bookmarks, Mail/Contacts/Calendar: **OFF** (out of scope; Photos especially would bloat the disk).
6. Wait for the initial **metadata population** to settle: the client creates
   online-only placeholders for the whole library, which is far faster and far
   smaller than downloading it. `./tools/watch-sync.sh` on the host watches the
   guest image stop growing and is a good enough proxy. Progress is also visible
   in the iCloud tray icon.
7. **Do not pin.** There is no pinning step in v2. If a guest was provisioned
   under v1 and already carries `P` intent, the bridge agent's first run clears
   it with `attrib -P` — which drops the always-keep request without evicting
   content already on disk — and records a marker so it never repeats. It never
   runs a global `+U`.
8. Note the exact sync root path for §7: it is `C:\Users\icloud\iCloudDrive` under this document's compose settings.

---

## 7. Guest provisioning — script 03: SMB share

PowerShell as Administrator. As in §5, the execution policy blocks a direct
`.ps1` launch — edit the file first to set the password, then invoke it with a
bypass:

```powershell
notepad C:\OEM\03-create-share.ps1     # set $pass to SHARE_PASS from .env
powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\03-create-share.ps1
```

**Source of truth: [`provision/03-create-share.ps1`](../provision/03-create-share.ps1).**
The file ships with `STRONG_PASSWORD_HERE` as a deliberate placeholder — it is
never replaced in the repository, which is the whole reason this step is not
automated (D10). What it does and why:

- Creates a dedicated local `syncshare` account for SMB only, hidden from the
  logon screen (D8). The auto-logon desktop user has a blank password by
  necessity, so it cannot be the share account.
- Grants `syncshare` **one inheritable ACE at the sync root only, never `/T`**
  (v2 plan D15). A recursive grant stamps explicit allow ACEs on every
  descendant, and an explicit allow outranks an inherited folder deny — which
  would let a known child path stay readable straight through a v2 exclusion.
  Script 04 cleans up explicit descendant grants left by earlier runs.
- Turns SMB signing and encryption **off** on this transport (v2 plan D32). The
  whole path is host loopback → docker-proxy → container NAT → QEMU tap; anyone
  positioned on it already has root on the host, so signing and sealing buy
  nothing and cost a per-byte pass on both ends of every hydration read. Since
  24H2 a stock Windows 11 Pro requires signing by default, so this must be
  turned off explicitly. Authentication (D8) and the exclusion model (D15) are
  untouched, and SMB 3.1.1 pre-auth integrity still protects negotiation.
- Is idempotent, so re-running it is the documented recovery after a Windows
  feature update (§10).

---

## 8. Host-side mount (systemd)

**Source of truth: the unit files in [`host/`](../host).** They are installed
verbatim, so the plan lists them rather than copying them:

| File | Installed as | Purpose |
|---|---|---|
| `host/mnt-icloud.mount` | `/etc/systemd/system/mnt-icloud.mount` | the iCloud Drive data share |
| `host/mnt-icloud.automount` | `/etc/systemd/system/mnt-icloud.automount` | mounts it on first access |
| `host/mnt-icloud_bridge.mount` | `/etc/systemd/system/mnt-icloud_bridge.mount` | the GUI/agent control channel (v2 plan D16) |
| `host/mnt-icloud_bridge.automount` | `/etc/systemd/system/mnt-icloud_bridge.automount` | mounts it on first access |

Credentials come from `/etc/credentials-icloud` (root-owned, mode 600), which
`icloud-bridge-configure` writes from `.env`:

```
username=syncshare
password=<SHARE_PASS from .env>
```

**The CIFS option list is load-bearing, not incidental** (v2 plan D33).
`mnt-icloud.mount` carries the full reasoning per option; the short version is
that `actimeo=1` keeps metadata fresh enough that remote-side changes and
hydration progress appear promptly, the default `cache=strict` is what makes a
read of a dataless placeholder always reach the guest and hydrate, and
`rasize=16777216` pipelines the one long sequential read that a cold hydration
is. There is no `sign`/`seal` (the guest turns both off, D32), no `mfsymlinks`
(it would create files iCloud syncs as junk) and no `max_channels` (one NATed
virtio NIC). `rasize` needs a kernel whose cifs module knows it (5.15+ assumed);
on an older one the mount fails with *bad option* and the option can be dropped.
The bridge share uses the same options minus `rasize`: it only ever carries
small JSON documents.

`uid`/`gid` are the primary desktop user's. The files ship with `1000`;
`icloud-bridge-configure` patches them from `MOUNT_UID`/`MOUNT_GID`, so do not
hand-edit them on an installed host.

The mount-path component is `icloud_bridge` with an underscore, so the only
hyphen in that unit name is systemd's encoding of the `/mnt/…` slash — no
literal-hyphen escaping is needed.

Enable (`host/setup-host.sh` does all of this, then hands off to
`icloud-bridge-configure`; `make deb && make install && make configure` places
the same files and runs the same configure step — see §14):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mnt-icloud.automount
sudo systemctl enable --now mnt-icloud_bridge.automount
ls /mnt/icloud            # must list iCloud Drive contents
ls /mnt/icloud_bridge     # status.json, tree.json, exclusions.json
```

**Usage rules for the mount (document these in README):** normal document/media workflows only. Do not place git repositories, build trees, or SQLite databases inside it — both SMB semantics and iCloud's own sync semantics make those unsafe. For read-heavy bulk work, rsync out to a local directory first.

---

## 9. Health monitoring

**Source of truth: [`host/icloud-health.sh`](../host/icloud-health.sh)** plus
`host/icloud-health.service` and `host/icloud-health.timer`. The script makes
four checks and exits non-zero if any fails, so systemd records the failure:
the container is running, `/mnt/icloud` is a mount point, a write canary lands,
and that canary's mtime is recent (which also catches a hung guest). The timer
runs it every 10 minutes, 5 minutes after boot.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now icloud-health.timer
systemctl status icloud-health.service   # after first run
```

All six units carry `ConditionPathExists=!/var/lib/icloud-bridge/powered-off`
(v2 plan D29): the GUI's **Quit and power off VM** writes that marker so the
mounts and health checks stay disarmed across a reboot without disabling the
units. `host/setup-host.sh` additionally installs the `icloud-bridge-power`
helper and creates the marker directory, and `icloud-bridge-configure` installs
the `sudoers` grant that lets the desktop operator run it; see v2 plan §5.1.

**Limitation to note:** the canary proves the share and guest are alive; it cannot prove Apple-side upload succeeded (the client exposes no API for that). The Apple-session check is manual: if sync stops while health checks pass, open `:8006` and check the iCloud tray icon for a re-login prompt.

---

## 10. Runbook — failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Health check: container not running | Host reboot without docker enabled, or crash | `docker compose up -d`; `systemctl enable docker` |
| Health check: mount failed, container fine | Guest rebooting (Windows Update) or SMB service down | Wait 5 min; else RDP in, `Start-Service LanmanServer` |
| Canary writes but files stop appearing on other Apple devices | **Apple session expired** (expected every few months) | Open `http://127.0.0.1:8006`, click the iCloud tray icon, sign in + 2FA again. No other changes needed |
| Guest disk full | iCloud data grew past DISK_SIZE | Stop container; edit `DISK_SIZE` in compose; `docker compose up -d` (dockur expands the disk); extend the partition in guest Disk Management |
| New files on host not uploading | File written into the share while guest's iCloud app crashed | RDP in, relaunch iCloud from Start menu; consider adding it to `shell:startup` (do this during provisioning if desired) |
| Everything broken after Windows feature update | Update reset a setting | Re-run scripts 01, 03 and 04 (all idempotent) |
| Mount hangs / IO errors on host | Guest hard-crashed mid-operation | `sudo systemctl restart mnt-icloud.automount` after guest is back (same for `mnt-icloud_bridge.automount`) |
| Tray yellow, `status.json` stale | The agent task is not running — it only runs in the logged-on `icloud` session | Check auto-logon at `:8006`, then `Start-ScheduledTask icloud-bridge-agent` |
| Guest disk below the floor and staying there | Reclamation has nothing eligible: content open, modified, or still uploading | Wait for uploads to finish, or grow `DISK_SIZE` as above |
| An exclusion reports `acl-write-denied` | §4's agent `RC,WDAC` grant did not take, or that object has a protected DACL | Re-run `04-bridge-agent.ps1` elevated and read its protected-DACL report |
| Excluded item reappeared under a new name | Renames of excluded items are not followed (accepted limitation) | Exclude the new path; clear the old one from *Missing configured items* |
| Bridge stays off after a reboot; `ls /mnt/icloud` empty, no automount | The GUI's **Quit and power off VM** left `/var/lib/icloud-bridge/powered-off`, and the unit `ConditionPathExists` gates keep everything down (v2 plan D29). Intended | Launch the GUI (autostart does this at login) — it runs `icloud-bridge-power on`. To reconcile by hand: `sudo /usr/local/bin/icloud-bridge-power on` |
| **Quit and power off VM** aborts saying a file is in use | A mount is busy (open file, a shell `cwd` inside `/mnt/icloud[_bridge]`, or an active copy); teardown refuses a lazy unmount by design | Close the holder — `lsof /mnt/icloud` / `fuser -m` — then Quit again. The VM stayed running the whole time |
| GUI is stuck on **Starting Windows VM…** or shows a start error | The VM did not boot, or its SMB never became ready within five minutes | Open the VM screen (`:8006`), confirm iCloud is signed in, then **Retry start**. The GUI never auto-retries or arms health against a dead mount |
| `docker compose up` fails with an error about `/dev/vhost-net` | The host's `vhost_net` module is not loaded (§3 passes the device through for accelerated virtio networking, D33) | `sudo modprobe vhost_net`, and re-run `host/setup-prereqs.sh` so it persists via `/etc/modules-load.d`. If the kernel has no `vhost_net` at all, delete that one `devices:` line — networking falls back to userspace virtio, which works but copies every SMB byte |
| `mount.cifs` reports **bad option** / the mount unit fails immediately after an upgrade | The host kernel's cifs module does not know `rasize` (D33; assumed 5.15+) | Remove `rasize=16777216` from the `Options=` line of `/etc/systemd/system/mnt-icloud.mount`, `systemctl daemon-reload`, and remount. Nothing else depends on it |

**Desired-on vs desired-off reboot.** With the bridge **on** (no marker), the
enabled units and `restart: unless-stopped` restore the container, both mounts,
and the health timer automatically on reboot — v1 behaviour, unchanged. With the
bridge **off** (marker present from a GUI power-off), the container stays stopped
and every unit's `ConditionPathExists` suppresses it before login, with no FAIL
spam; XDG autostart powers the bridge back on when the operator logs in, or it
stays off until the GUI is launched if autostart is unticked.

**Backup stance:** the VM is disposable *except* for the Apple session; the data's canonical copy is iCloud itself. For belt-and-braces, an optional host cron `rsync -a --delete /mnt/icloud/ /srv/icloud-backup/` onto snapshotted storage gives point-in-time recovery that iCloud's own trash does not.

---

## 11. Acceptance tests (all must pass)

`host/acceptance-tests.sh` automates the host-checkable subset; the rest are
manual. **E0 (test 8) gates the others** — run it before trusting the mount with
real work.

1. `kvm-ok` reports acceleration usable.
2. `docker compose up -d` from a clean state reaches a Windows desktop on `:8006` with no manual intervention before §4.
3. After provisioning, over a ≥ 300 s window with no operator activity:
   `./tools/vcpu-profile.py --seconds 300` reports **at most 0.30 core-seconds
   of container CPU per wall second** and **at most 200 KiB/s of container
   block writes**, with the exact figures recorded in the environment baseline
   of `docs/acceptance-results.md`; and RSS of the container ≈ RAM_SIZE
   (`docker stats` is fine for memory). The author's 2026-07-26 idle floor was
   0.18 core-s/s and ~66 KiB/s of writes; the thresholds carry headroom for
   background maintenance, not for regressions. The retired form of this
   criterion — "idles < 5% host CPU (check `docker stats`)" — could not pass:
   `docker stats` reports percent of one core on Linux, and the measured idle
   guest reads ~18% there while being healthy.
4. iCloud tray icon shows signed-in and syncing.
5. `ls /mnt/icloud` on the host lists the operator's real iCloud files, at their
   real sizes, as online-only placeholders.
6. **Round-trip down:** create a note file in iCloud Drive from an iPhone/Mac → appears in `/mnt/icloud` within 2 minutes.
7. **Round-trip up:** `echo test > /mnt/icloud/linux-up.txt` on the host → visible on iPhone/Mac Files app within 2 minutes.
8. **E0 — kernel-CIFS gate (replaces v1's pinned-`P` check).** The prior
   hydration evidence used userland `smbclient`; the real mount uses the kernel
   cifs client with `actimeo=1,echo_interval=15`, and the largest file read was
   1.17 MB. So:
   1. On a file the guest reports as `RECALL_ON_DATA_ACCESS` and ≥ 100 MB, whose
      SHA-256 is known from another Apple device,
      `time timeout 30m sha256sum /mnt/icloud/<file>` completes without EIO,
      hang or timeout and the hash matches. Record size and elapsed time.
   2. The same for a multi-GB online-only file, with a documented generous
      timeout. Record the sustained rate and judge whether a read that blocks
      that long is acceptable.
   3. A uniquely named disposable file written on the host appears on another
      Apple device with a matching hash; editing it on the host produces a
      matching new hash; deleting it on the host propagates. Never use an
      existing user file for this.

   Failing 8.1 or 8.2 means D25 is not accepted — investigate mount/client
   timeout behaviour or reintroduce scoped pinning. Failing 8.3 means the
   bidirectional D6 architecture itself is not accepted. `TimeoutSec=30` in the
   mount unit bounds mount *establishment*, not later reads.
9. **Online-only placeholders read correctly and stay online-only otherwise.**
   In the guest, a file never read from the host still reports
   `RECALL_ON_DATA_ACCESS` after a full agent tree scan and allocation report —
   proving the metadata path does not hydrate. A file that *was* read reports
   fully local afterwards. `attrib` showing `O`/`o` is the expected steady
   state; `P` is not.
10. Health timer green: `systemctl list-timers | grep icloud` and last service run exit 0.
11. Host reboot test: after `reboot`, container auto-starts, both automounts work on first `ls`, health check passes within 10 minutes — no human action.
12. All published ports answer only on `127.0.0.1` (verify with `ss -tlnp | grep -E '8006|3389|10445'`).
13. Bridge: `/mnt/icloud_bridge` is mounted, `status.json` is under 90 s old, and
    `status.json`, `tree.json` and `exclusions.json` all parse as JSON.
14. Selective sync: v2 plan E1–E7, reproduced for operators in
    [`selective-sync.md`](selective-sync.md#deployment-checklist).
15. GUI-managed lifecycle (v2 plan D29): `host/acceptance-tests.sh` checks that
    `/var/lib/icloud-bridge` exists, `icloud-bridge-power` is `root:root` 0755,
    `/etc/sudoers.d/icloud-bridge` is `root:root` 0440, all six units carry the
    `ConditionPathExists` marker gate, and `sudo -n -l icloud-bridge-power on`/`off`
    are permitted. The live on/off/busy/reboot behaviour is v2 plan E8–E11,
    reproduced in [`selective-sync.md`](selective-sync.md#deployment-checklist).

---

## 12. Known limitations (accepted, do not attempt to fix here)

- **Server-side semantics:** iCloud resolves conflicts last-writer-wins with timestamps; concurrent edits on two devices can silently produce duplicates or drop one version. This is Apple-side behavior every client inherits (see arXiv 2602.19433 for the full failure catalogue). Mitigation is the backup rsync in §10.
- Session re-login is manual by design (2FA cannot be automated and attempting to would risk account lockout).
- **Cold reads block for the whole download.** With Files On-Demand on (D5 as
  amended), the first read of a file fetches it from Apple while the reading
  process sits in the read call: seconds for small files, potentially far longer
  for multi-GB ones, with no progress indication on the host side. E0 (§11)
  measures whether this is tolerable on the target host before deployment.
- **Disk reclamation is asynchronous and can fail to reach its target.** The
  agent asks Windows to make cold, in-sync files online-only when free space
  drops below 20 GB, aiming for 30 GB, but Cloud Files refuses to dehydrate
  content that is open, modified, or not yet uploaded. Free space can therefore
  stay below the floor; the tray reports it and the fix is to wait or grow the
  disk (§10), not to delete anything.
- SMB metadata operations are slower than local disk; bulk `find`-style workloads should rsync out first.
- **The health canary is itself synced.** §9 writes `/mnt/icloud/.linux-canary`
  every ten minutes, and that path is inside the sync root, so iCloud uploads a
  new version 144 times a day with server-side version history. That is the price
  of proving the host→guest→NTFS path end to end, and it is accepted: the
  alternative (a longer interval) slows dead-guest detection and would have to
  move the timer, the script's own freshness check and the GUI's
  `CANARY_MAX_AGE_SECONDS` in lockstep. See v2 plan §8.1.
- Windows base image is ~15–20 GB even debloated; that is the floor for the stock-ISO path (D3). An LTSC/Enterprise `VERSION` was examined in v2 §8.1 and is closed: LTSC has no Microsoft Store, which is D4's locked install path.
- Photos sync is deliberately out of scope (separate, larger problem; icloudpd covers it better).

---

## 13. v2 artifacts (GUI, bridge agent, selective sync)

Specified in [`plan-gui-selective-sync.md`](plan-gui-selective-sync.md).
Summary of what exists and where:

| Artifact | Runs on | Purpose |
|---|---|---|
| `guest-agent/agent.ps1` | Windows guest, as `icloud`, unelevated | Enforces the exclusion list (deny ACE on each excluded item + `DELETE_CHILD` guard on its parent, then an online-only request), reclaims disk below the 20 GB floor, publishes `status.json` / `tree.json`, answers per-folder list requests |
| `provision/agent.ps1` | — | Byte-identical copy so dockur places it in `C:\OEM`; guarded by `host/acceptance-tests.sh` §8 |
| `provision/04-bridge-agent.ps1` | Windows guest, elevated | Creates the `bridge` share over `C:\ProgramData\icloud-bridge\io`, normalises the `syncshare` ACL, grants the agent `RC,WDAC`, sets the D27 privilege boundary, turns on Access-Based Enumeration for the `icloud` share, registers the scheduled task |
| `host/mnt-icloud_bridge.{mount,automount}` | Linux host | §8 above |
| `host/icloud-bridge-power` | Linux host, root (via the GUI's `sudo -n`) | Serialized `on`/`off` of the whole bridge as one transaction: durable off marker, ordered CIFS teardown (never lazy), graceful `docker stop --timeout 130`, and a real CIFS-readiness retry on startup (v2 plan D29) |
| `gui/` | Linux host, desktop user | Tray icon + status window + selective-sync UI; Qt-free `power.py`/`autostart.py` drive the D29 lifecycle (power-on before any CIFS I/O, three-way Quit, autostart toggle); `pytest gui/tests` covers health precedence, the bridge protocol, the power model, and the autostart entry |

**Where the truth lives.** For every artifact in this table, and for every file
§3–§9 point at, **the file itself is the source of truth**. This document holds
the reasoning, the decisions and the operator sequence; it does not hold copies
of the code. Until 2026-07-26 §3–§9 embedded their files verbatim and `AGENTS.md`
required the copies to move together — by then seven of the eleven had drifted
anyway, so the rule was removed rather than made stricter (see the change note at
the top). What still has to be kept in step is *specification*, not text: v2 plan
§3 for `guest-agent/agent.ps1` and §4 for `provision/04-bridge-agent.ps1`.

---

## 14. Build, packaging and developer entry points

`make` with no arguments lists every target. Nothing here is compiled — "build"
means staging files into a package tree — so the Makefile is a thin, discoverable
front end over the scripts that already existed rather than a new build system.

| Target | What it does | Needs a real host? |
|---|---|---|
| `make venv` | Creates `.venv` with pytest. PEP 668 forbids `pip install --user` on this class of system and `install-gui.sh` already refuses `--break-system-packages` (v2 plan D18), so a venv is the only correct route | no |
| `make venv-qt` | Same plus PySide6, for the with-Qt half of the suite | no |
| `make test` / `test-qt` / `test-all` | Runs `pytest gui/tests` without Qt, with Qt, or both — `CONTRIBUTING.md` requires the suite to pass either way, and `test-all` is what actually proves it | no |
| `make lint` | `bash -n` over every shell script, `sh -n` over the maintainer scripts, `compileall` over the Python, `cmp` of the two `agent.ps1` copies, and `docker compose config`. Prints `SKIP:` for absent optional linters rather than passing silently | no |
| `make lint-ps` | Fetches PowerShell 7 into `build/pwsh` and runs `packaging/lint-ps1.ps1`: parse check plus a PSScriptAnalyzer pass | no |
| `make check` | `lint` + `test`; the whole of what a checkout can prove | no |
| `make deb` | Builds `dist/icloud-bridge_<version>_all.deb` | no |
| `make install` / `uninstall` / `purge` | `apt` the built package in or out | yes |
| `make configure` | `sudo icloud-bridge-configure --env-file ./.env` | yes |
| `make install-gui` | The per-user `$HOME` install, unchanged | no |
| `make run` | Launches the GUI straight from the checkout, preferring `.venv-qt`. The developer inner loop: no install, no root, and the first-run Setup tab resolves its bundle from the source tree (v2 plan §6.2), so the operator's first screen is reachable without a guest. Health and selective sync read unavailable with nothing mounted, and the power buttons need the host-installed `icloud-bridge-power` | no |
| `make acceptance` | `host/acceptance-tests.sh` | yes |

The version is read from `gui/icloud_bridge_gui/__init__.py`; it is the one place
it is written down.

### 14.1 Two install paths, one configured result

`sudo ./host/setup-host.sh` and `make deb && make install` place the **same files
at the same paths**, and both then run **`icloud-bridge-configure`**. Keeping them
interchangeable is the whole point: an operator can move between them without
producing a half-configured hybrid, and `host/acceptance-tests.sh` passes against
either.

`icloud-bridge-configure` owns everything that cannot be known before the machine
is in front of you, which is exactly why it is a separate command and not a
`postinst`:

- `/etc/credentials-icloud`, built from `SHARE_PASS` in the gitignored `.env`;
- the `uid=`/`gid=` in the two `.mount` units, which must match the desktop user;
- `/etc/sudoers.d/icloud-bridge`, the argument-exact D29 grant, which names the
  operator account.

It also writes `/etc/icloud-bridge/config` recording those choices. A package
upgrade unpacks pristine units carrying `uid=1000,gid=1000`, so the `postinst`
replays the recorded ownership from that file; without it, every upgrade would
silently re-own the mounts away from the operator.

### 14.2 Why the package ships to `/usr/local` and `/etc/systemd/system`

Debian policy reserves `/usr/local` for the local administrator and prefers
`/lib/systemd/system` for packaged units. This package deliberately does neither,
because three things pin the paths harder than packaging convention does:

- `gui/icloud_bridge_gui/power.py` hardcodes
  `HELPER_PATH = "/usr/local/bin/icloud-bridge-power"`;
- the sudoers grant matches that absolute path **with its arguments**, so a
  different location silently revokes the operator's ability to power the bridge
  off (D29); and
- `host/acceptance-tests.sh` asserts both that path and `/etc/systemd/system/<unit>`.

Relocating would mean editing a locked D29 contract detail to satisfy a lint
category, for a package that is built and installed locally and never uploaded to
an archive. The tags are suppressed in `packaging/deb/lintian-overrides`, with
that reasoning recorded next to them.

### 14.3 Packaging mechanics

`packaging/build-deb.sh` stages a tree and calls `dpkg-deb --root-owner-group
--build`. It deliberately avoids `debhelper`/`dpkg-buildpackage`: those pull a
build-dependency chain and, without `--root-owner-group`, `fakeroot`. The staged
approach needs neither, so `make deb` works on a bare host and never runs as root.

PySide6 is a `Recommends`, not a `Depends`. The package carries the host half as
well as the GUI, and a hard dependency would make it uninstallable on a release
whose archive lacks the `python3-pyside6` packages; `/usr/bin/icloud-bridge-gui`
checks for the import at startup and prints the exact `apt` line if it is missing.
Docker is only a `Suggests`, because §1 installs Docker Engine from
`get.docker.com` and a stronger relationship would invite `apt` to pull the
conflicting `docker.io`.

The GUI package lands in `/usr/lib/icloud-bridge-gui/` and needs no code change to
run from there: `tray.py` resolves its icons relative to `__file__`. The system
autostart entry goes to `/etc/xdg/autostart/`, whose basename a per-user
`~/.config/autostart/` entry overrides, so a package install and a
`gui/install-gui.sh` install cannot double-launch the tray. The package also
ships `README.md`, `SETUP.md`, `CHANGELOG.md`, and
`docs/acceptance-results.md` under `/usr/share/doc/icloud-bridge/`, so the
operator retains the runbook, the improvement/decision history and the live
acceptance record without a source checkout. The acceptance record keeps its
`docs/` subdirectory there so the repository-relative links to it in `SETUP.md`
and `CHANGELOG.md` resolve from the installed copies too.

### 14.4 What none of this proves

`make check` runs in a checkout with no VM. It does not exercise the guest agent,
the CIFS mounts, the power transaction, or the package's `postinst`/`prerm` on a
live system — the package has to be installed on the real host for that, and
`make acceptance` is what reports the result.
