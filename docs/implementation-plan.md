# iCloud Drive on Linux via Minimal Windows VM — Implementation Handoff

**Version:** 1.0 · **Status:** Ready to execute · **Audience:** an executor (human or model) who follows instructions literally. All decisions are already made. Do not substitute components unless a step explicitly offers a fallback.

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
| D5 | Files On-Demand | **Disabled**, folder pinned with `attrib +P` | Placeholders (dataless files) served over SMB stall or fail; host must see real bytes |
| D6 | Guest→host export | SMB share **from the guest**, mounted on host via `cifs` + systemd automount | Only live-bidirectional option: host writes land directly in the Cloud Files sync root and upload immediately. (A robocopy mirror to a host folder was considered and rejected: one-way, polling delay, dangerous for bidirectional) |
| D7 | Sync root location | Guest-local NTFS virtual disk (default `%USERPROFILE%\iCloudDrive`) | Cloud Files API requires local NTFS with reparse points; cannot point it at a network path |
| D8 | Share account | Dedicated local Windows user `syncshare` (SMB only), separate from the auto-logon user | Auto-logon user has a blank password (required for unattended logon); SMB must be password-protected |
| D9 | Port exposure | All ports bound to `127.0.0.1` on the host only | VM holds an authenticated Apple session; never expose to LAN |
| D10 | Resources | 2 vCPU, 3 GB RAM, disk = 40 GB + iCloud data size (see §2) | Measured floor for debloated Win11 + headroom |
| D11 | Defender | Keep enabled; **exclude** the iCloud folder; disable scheduled scans | Full disable fights Tamper Protection; exclusion captures ~all of the CPU win |
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
```

**Sizing rule (D10):** `DISK_SIZE = 40 GB + (total iCloud Drive data) rounded up to the next 20 GB`. Example: 70 GB of iCloud data → `DISK_SIZE: "120G"`. The qcow2 image grows on demand, so oversizing costs nothing upfront. RAM: 3 GB allocated (do not go below 2.5 GB; Windows update servicing needs it). Host must therefore have ≥ 3 GB free RAM permanently.

---

## 2. Project repository layout

Create this exact structure (a git repo is recommended):

```
icloud-vm/
├── docker-compose.yml
├── .env                      # operator-specific values (gitignore this)
├── provision/                # scripts to run INSIDE the Windows guest
│   ├── 01-debloat.ps1
│   ├── 02-install-icloud.ps1
│   └── 03-create-share.ps1
├── host/
│   ├── mnt-icloud.mount
│   ├── mnt-icloud.automount
│   ├── icloud-health.sh
│   ├── icloud-health.service
│   └── icloud-health.timer
└── README.md                 # copy of this document
```

`.env` contents (operator fills in; `SHARE_PASS` must be strong — 20+ random chars):

```
DISK_SIZE=120G
RAM_SIZE=3G
CPU_CORES=2
SHARE_PASS=CHANGE_ME_STRONG_PASSWORD
```

---

## 3. docker-compose.yml (verbatim)

```yaml
services:
  windows:
    image: dockurr/windows
    container_name: icloud-windows
    environment:
      VERSION: "11"            # Windows 11 Pro, auto-downloaded from Microsoft
      RAM_SIZE: "${RAM_SIZE}"
      CPU_CORES: "${CPU_CORES}"
      DISK_SIZE: "${DISK_SIZE}"
      USERNAME: "icloud"       # auto-logon desktop user (blank password by design)
      LANGUAGE: "English"
      REGION: "en-US"          # adjust if the Apple ID region requires it
      KEYBOARD: "en-US"
    devices:
      - /dev/kvm
      - /dev/net/tun
    cap_add:
      - NET_ADMIN
    ports:
      - "127.0.0.1:8006:8006"          # web viewer (noVNC) — install & login only
      - "127.0.0.1:3389:3389/tcp"      # RDP — admin access
      - "127.0.0.1:3389:3389/udp"
      - "127.0.0.1:10445:445"          # guest SMB share → host mount
    volumes:
      - /srv/icloud-vm/storage:/storage
    stop_grace_period: 2m
    restart: unless-stopped
```

Notes for the executor:
- dockur forwards published container ports to the guest VM, which is how host `127.0.0.1:10445` reaches the guest's SMB service on 445.
- Do **not** add `privileged: true` unless the container reports `/dev/kvm` missing despite `kvm-ok` passing.
- First start: `docker compose up -d`, then open `http://127.0.0.1:8006` in a browser (SSH port-forward `-L 8006:127.0.0.1:8006` if the host is remote). Installation is fully automatic; wait until a Windows desktop is visible (typically 15–40 min depending on bandwidth).

---

## 4. Guest provisioning — script 01: debloat

Open the web viewer (or RDP as user `icloud`, blank password). Start **PowerShell as Administrator** (right-click Start → Terminal (Admin)). Run the following as `provision/01-debloat.ps1` (paste whole block):

```powershell
# ============ 01-debloat.ps1 — run as Administrator ============
$ErrorActionPreference = "Continue"

# --- Services not needed on a sync appliance ---
$services = @(
  "WSearch",        # Search indexer: the classic CPU/RAM hog over big sync folders
  "SysMain",        # Superfetch
  "DiagTrack",      # Telemetry
  "WMPNetworkSvc",  # Media sharing
  "MapsBroker",
  "Fax",
  "RemoteRegistry"
)
foreach ($s in $services) {
  Stop-Service $s -Force -ErrorAction SilentlyContinue
  Set-Service  $s -StartupType Disabled -ErrorAction SilentlyContinue
}

# --- Defender: exclude the sync root; kill scheduled scans (D11) ---
$icloudPath = "$env:USERPROFILE\iCloudDrive"
Add-MpPreference -ExclusionPath $icloudPath
Add-MpPreference -ExclusionProcess "iCloudServices.exe","iCloudDrive.exe","secd.exe"
Set-MpPreference -ScanScheduleDay 8            # 8 = never
Set-MpPreference -DisableCatchupFullScan  $true
Set-MpPreference -DisableCatchupQuickScan $true

# --- Windows Update: notify-only, never auto-reboot (D12) ---
$au = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
New-Item -Path $au -Force | Out-Null
Set-ItemProperty $au -Name AUOptions -Value 2 -Type DWord                       # notify before download
Set-ItemProperty $au -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord

# --- Disable VBS/HVCI (RAM + perf win under KVM) ---
$dg = "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard"
Set-ItemProperty $dg -Name EnableVirtualizationBasedSecurity -Value 0 -Type DWord -ErrorAction SilentlyContinue

# --- Power: never sleep, never hibernate, display off is fine ---
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off

# --- Misc quieting ---
# Edge preload
$edge = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
New-Item -Path $edge -Force | Out-Null
Set-ItemProperty $edge -Name "StartupBoostEnabled" -Value 0 -Type DWord
# Content delivery / suggested apps
$cdm = "HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
Set-ItemProperty $cdm -Name "SilentInstalledAppsEnabled" -Value 0 -Type DWord -ErrorAction SilentlyContinue

# --- Remove obvious inbox bloat (safe list only; do NOT touch Store/AppX infra, D3) ---
$bloat = @("Microsoft.XboxApp","Microsoft.XboxGamingOverlay","Microsoft.ZuneMusic",
           "Microsoft.ZuneVideo","Microsoft.BingNews","Microsoft.BingWeather",
           "Microsoft.GamingApp","Microsoft.People","Microsoft.Todos",
           "MicrosoftTeams","Microsoft.549981C3F5F10")   # last = Cortana
foreach ($b in $bloat) {
  Get-AppxPackage -AllUsers $b | Remove-AppxPackage -ErrorAction SilentlyContinue
}

Write-Host "`nDebloat complete. Reboot now with: Restart-Computer" -ForegroundColor Green
# ================================================================
```

Then run `Restart-Computer` and wait for the desktop to return (auto-logon).

**Hard rule:** never remove the Microsoft Store, the AppX/MSIX stack, WebView2, or Windows servicing components. iCloud installation and updates depend on them (D3).

---

## 5. Guest provisioning — script 02: install iCloud

PowerShell as Administrator again:

```powershell
# ============ 02-install-icloud.ps1 ============
winget install --id AppleInc.iCloud --source msstore `
  --accept-package-agreements --accept-source-agreements
```

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
   - Open iCloud Drive options and **disable Files On-Demand** (checkbox may be labeled "Files On-Demand" or similar). If the running version has no such toggle, proceed — step 7 pins everything regardless. **(D5 — this is the single most important setting in the whole document.)**
   - Photos, Passwords, Bookmarks, Mail/Contacts/Calendar: **OFF** (out of scope; Photos especially would bloat the disk).
6. Wait for the initial sync to complete. Progress is visible in the iCloud tray icon. For large libraries this takes hours and is Apple-server-bound; leave it running.
7. Pin all content as always-local (belt and braces for D5). PowerShell (normal user is fine):

```powershell
attrib +P -U "$env:USERPROFILE\iCloudDrive\*" /S /D
```

8. Note the exact sync root path for §7: it is `C:\Users\icloud\iCloudDrive` under this document's compose settings.

---

## 7. Guest provisioning — script 03: SMB share

PowerShell as Administrator:

```powershell
# ============ 03-create-share.ps1 ============
# Replace STRONG_PASSWORD_HERE with the value of SHARE_PASS from .env — must match exactly.
$pass = ConvertTo-SecureString "STRONG_PASSWORD_HERE" -AsPlainText -Force

# D8: dedicated password-protected account, SMB use only, hidden from logon
New-LocalUser -Name "syncshare" -Password $pass -PasswordNeverExpires `
  -AccountNeverExpires -Description "SMB access for Linux host"
# Hide from the Windows login screen
$wl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
New-Item -Path $wl -Force | Out-Null
New-ItemProperty -Path $wl -Name "syncshare" -Value 0 -PropertyType DWord -Force | Out-Null

# Filesystem permission for syncshare on the sync root
$root = "C:\Users\icloud\iCloudDrive"
icacls $root /grant "syncshare:(OI)(CI)M" /T /Q

# The SMB share itself
New-SmbShare -Name "icloud" -Path $root -FullAccess "syncshare"

# SMB service + firewall (guest firewall only sees the container network; keep scope tight anyway)
Set-Service -Name LanmanServer -StartupType Automatic
Start-Service LanmanServer
Enable-NetFirewallRule -DisplayGroup "File and Printer Sharing"

Write-Host "Share ready: \\<guest>\icloud as user syncshare" -ForegroundColor Green
# ===============================================
```

---

## 8. Host-side mount (systemd)

Create `/etc/credentials-icloud` (root-owned, mode 600):

```
username=syncshare
password=STRONG_PASSWORD_HERE
```

```bash
sudo chmod 600 /etc/credentials-icloud
sudo mkdir -p /mnt/icloud
```

`host/mnt-icloud.mount` → copy to `/etc/systemd/system/mnt-icloud.mount`:

```ini
[Unit]
Description=iCloud Drive via Windows VM (CIFS)
Requires=docker.service
After=docker.service

[Mount]
What=//127.0.0.1/icloud
Where=/mnt/icloud
Type=cifs
Options=credentials=/etc/credentials-icloud,port=10445,vers=3.1.1,uid=1000,gid=1000,file_mode=0664,dir_mode=0775,actimeo=1,echo_interval=15,_netdev
TimeoutSec=30

[Install]
WantedBy=multi-user.target
```

(`uid`/`gid` 1000 = the primary desktop user; adjust to the operator's `id -u`/`id -g`. `actimeo=1` keeps metadata fresh so remote-side changes appear within ~1 s of listing.)

`host/mnt-icloud.automount` → `/etc/systemd/system/mnt-icloud.automount`:

```ini
[Unit]
Description=Automount for iCloud Drive

[Automount]
Where=/mnt/icloud
TimeoutIdleSec=0

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mnt-icloud.automount
ls /mnt/icloud     # must list iCloud Drive contents
```

**Usage rules for the mount (document these in README):** normal document/media workflows only. Do not place git repositories, build trees, or SQLite databases inside it — both SMB semantics and iCloud's own sync semantics make those unsafe. For read-heavy bulk work, rsync out to a local directory first.

---

## 9. Health monitoring

`host/icloud-health.sh` → `/usr/local/bin/icloud-health.sh`, `chmod +x`:

```bash
#!/usr/bin/env bash
# Exit non-zero on any failure; systemd records it. Wire alerts later if desired.
set -u
FAIL=0

# 1. Container running?
docker inspect -f '{{.State.Running}}' icloud-windows 2>/dev/null | grep -q true \
  || { echo "FAIL: container not running"; FAIL=1; }

# 2. Mount alive?
mountpoint -q /mnt/icloud \
  || { echo "FAIL: /mnt/icloud not mounted"; FAIL=1; }

# 3. Write canary: proves host→guest→NTFS path works end to end.
CANARY=/mnt/icloud/.linux-canary
date -Is > "$CANARY" 2>/dev/null \
  || { echo "FAIL: cannot write canary (share read-only or session dead?)"; FAIL=1; }

# 4. Freshness: canary mtime must be recent (also catches a hung guest).
if [ -f "$CANARY" ]; then
  AGE=$(( $(date +%s) - $(stat -c %Y "$CANARY") ))
  [ "$AGE" -lt 300 ] || { echo "FAIL: canary stale (${AGE}s)"; FAIL=1; }
fi

exit $FAIL
```

`icloud-health.service` and `icloud-health.timer` → `/etc/systemd/system/`:

```ini
# icloud-health.service
[Unit]
Description=iCloud VM health check
[Service]
Type=oneshot
ExecStart=/usr/local/bin/icloud-health.sh
```

```ini
# icloud-health.timer
[Unit]
Description=Run iCloud VM health check every 10 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now icloud-health.timer
systemctl status icloud-health.service   # after first run
```

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
| Everything broken after Windows feature update | Update reset a setting | Re-run scripts 01 and 03 (both are idempotent) |
| Mount hangs / IO errors on host | Guest hard-crashed mid-operation | `sudo systemctl restart mnt-icloud.automount` after guest is back |

**Backup stance:** the VM is disposable *except* for the Apple session; the data's canonical copy is iCloud itself. For belt-and-braces, an optional host cron `rsync -a --delete /mnt/icloud/ /srv/icloud-backup/` onto snapshotted storage gives point-in-time recovery that iCloud's own trash does not.

---

## 11. Acceptance tests (all must pass)

1. `kvm-ok` reports acceleration usable.
2. `docker compose up -d` from a clean state reaches a Windows desktop on `:8006` with no manual intervention before §4.
3. After provisioning: guest idles < 5% host CPU and RSS of the container ≈ RAM_SIZE (check `docker stats`).
4. iCloud tray icon shows signed-in, sync complete.
5. `ls /mnt/icloud` on the host lists the operator's real iCloud files.
6. **Round-trip down:** create a note file in iCloud Drive from an iPhone/Mac → appears in `/mnt/icloud` within 2 minutes.
7. **Round-trip up:** `echo test > /mnt/icloud/linux-up.txt` on the host → visible on iPhone/Mac Files app within 2 minutes.
8. `attrib` check in guest: files under `iCloudDrive` show `P` (pinned), not `U`/`O` (online-only).
9. Health timer green: `systemctl list-timers | grep icloud` and last service run exit 0.
10. Host reboot test: after `reboot`, container auto-starts, automount works on first `ls /mnt/icloud`, health check passes within 10 minutes — no human action.
11. All four published ports answer only on `127.0.0.1` (verify with `ss -tlnp | grep -E '8006|3389|10445'`).

---

## 12. Known limitations (accepted, do not attempt to fix here)

- **Server-side semantics:** iCloud resolves conflicts last-writer-wins with timestamps; concurrent edits on two devices can silently produce duplicates or drop one version. This is Apple-side behavior every client inherits (see arXiv 2602.19433 for the full failure catalogue). Mitigation is the backup rsync in §10.
- Session re-login is manual by design (2FA cannot be automated and attempting to would risk account lockout).
- SMB metadata operations are slower than local disk; bulk `find`-style workloads should rsync out first.
- Windows base image is ~15–20 GB even debloated; that is the floor for the stock-ISO path (D3). Optional future optimization: rebuild on an LTSC IoT ISO — out of scope for v1.
- Photos sync is deliberately out of scope (separate, larger problem; icloudpd covers it better).
