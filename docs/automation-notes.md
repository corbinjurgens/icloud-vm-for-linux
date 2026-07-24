# Automation notes — what the first real run actually cost

Record of the **first end-to-end run on real hardware** (2026-07-22, Ubuntu 26.04
on an i7-13700H). Its purpose is to make the *second* run cheap: every manual
step below is listed with its root cause and whether it can be automated.

[`SETUP.md`](../SETUP.md) is the operator runbook ("what to type").
This file is the engineering record ("why it was manual, and how to remove it").

---

## 1. Scoreboard

| # | Step | First run | Automatable? |
|---|---|---|---|
| 1 | Install native Docker Engine, switch context off Docker Desktop | manual, 2 failed attempts | **Yes — done** (`setup-prereqs.sh` fixed) |
| 2 | `newgrp` missing (`util-linux-extra`) | manual | **Yes** — add to `setup-prereqs.sh` |
| 3 | docker group not active in existing session | relogin dance | **Yes — worked around** (`sg docker -c`) |
| 4 | Boot guest, download + install Windows | already automated | Yes (already) |
| 5 | Preserve the Windows ISO | manual, time-critical | **Yes — done** (`tools/keep-iso.sh`) |
| 6 | Detect "guest is ready" | naive check gave a false positive | **Yes — done** (`tools/rdp-ready.py`) |
| 7 | Get files into the guest | painful via noVNC | **Yes — done** (`\\host.lan\Data`) |
| 8 | Run scripts in the guest (execution policy) | blocked, then misdiagnosed | **Yes** — invoke with `-ExecutionPolicy Bypass` |
| 9 | Install iCloud (wrong winget ID) | failed, needed `winget search` | **Yes — done** (ID corrected) |
| 10 | Type commands into the guest | copy/paste hell | **Yes — done** (`tools/guest-ctl.sh`) |
| 11 | **Apple ID sign-in + 2FA** | manual | **No — and must stay manual** |
| 12 | iCloud Drive ON (Files On-Demand stays ON) | GUI toggle | Partly — GUI automation is brittle |
| 13 | Create SMB share (`03-create-share.ps1`) | manual | **Yes** — scriptable, needs the secret |
| 14 | Bridge agent + selective sync (`04-bridge-agent.ps1`) | n/a on the first run | **Yes** — scriptable, no secret |
| 15 | Host mount + acceptance tests | scripted | Yes (already) |

**Net:** everything except #11 (and arguably #12) can be automated. #11 is a hard
stop by design — 2FA cannot be automated and attempting it risks account lockout
(explicitly out of scope in `AGENTS.md`).

Step 12 got *smaller* between v1 and v2. v1 needed two toggles (iCloud Drive on,
Files On-Demand **off**) plus a global `attrib +P -U` pin. Live testing on
2026-07-22/23 showed dataless placeholders hydrate on demand over SMB, so v2
leaves Files On-Demand **on** and pins nothing: one toggle, no pin, no
whole-library download.

---

## 2. The blockers, in detail

### 2.1 Docker Desktop cannot pass `/dev/kvm` (cost: ~40 min)

Desktop runs containers inside its own LinuxKit VM, which has no `/dev/kvm`:

```
docker run --device /dev/kvm alpine …
→ error gathering device information … "/dev/kvm": no such file or directory
```

Two follow-on traps, both now fixed in `host/setup-prereqs.sh`:

1. The Engine-install guard tested `command -v docker`. Docker Desktop ships the
   **CLI** on the host, so the test passed and the Engine install was skipped;
   `systemctl enable --now docker` then failed with *"Unit docker.service does
   not exist"*. **Fix: test for `dockerd`, the daemon binary, which only the
   native Engine installs.**
2. The context switch ran as root and checked *root's* context list, which has no
   `desktop-linux`, so it silently skipped switching the operator's context.
   **Fix: check and switch as `$TARGET_USER`.**

`get.docker.com` warns *"docker command appears to already exist"* and sleeps 20 s
when Desktop is present. That warning is expected; it installs `docker-ce` anyway.

### 2.2 Group membership needs a new login session

`usermod -aG docker` only affects **new logins**. Restarting an editor is not
enough if the editor inherits a login session that predates the change (verified:
VS Code restarted, but its parent `systemd --user` was 2 days old, so it still
lacked gid 973).

Workarounds, cheapest first:

```bash
sg docker -c 'docker ps'    # no relogin needed; needs util-linux-extra
newgrp docker               # fixes one shell only
# full logout/login or reboot -- the only thing that fixes everything
```

`sg`/`newgrp` live in **`util-linux-extra`** on recent Ubuntu and are not
installed by default. **Automate: add `util-linux-extra` to `setup-prereqs.sh`.**

### 2.3 The Windows ISO gets deleted (cost: a 5 GB re-download per rebuild)

`removeImage()` (`install.sh:1353`) deletes the downloaded ISO **before**
`buildImage`, i.e. minutes after the download finishes and long before Windows
installs. The window observed on the first run was ~10 minutes (22:07 → 22:17).

A copy usually loses that race. A **hard link** cannot: `rm` drops one link and
the inode survives. Automated in `tools/keep-iso.sh`.

Permanent fix for rebuilds: supply the ISO as `custom.iso` in `$STORAGE`.
`detectCustom()` sets `CUSTOM`, and `removeImage()` starts with
`[ -n "$CUSTOM" ] && return 0` — so it is reused *and* never deleted.

Observed: `Win11_25H2_English_x64_v2.iso`, 8,471,603,200 bytes (7.9 GB).

### 2.4 A published port is not a live service (cost: one wrong "ready" call)

`docker-proxy` accepts TCP on the host even when nothing listens in the guest, so
`/dev/tcp/127.0.0.1/3389` succeeded **30 seconds into the ISO download**. Any
readiness check against a *published* port must speak the protocol.
`tools/rdp-ready.py` sends a real X.224 CR and requires a TPKT reply.

The same applies to SMB on `127.0.0.1:10445` — it will "connect" long before
`03-create-share.ps1` has ever run.

### 2.5 PowerShell execution policy (cost: ~20 min, partly misdiagnosis)

Windows 11 defaults to `Restricted`; `.ps1` files will not run. `01-debloat.ps1`
only worked because `install.bat` invokes it as
`powershell -ExecutionPolicy Bypass -NoProfile -File …`. Nothing documented that
for scripts 02 and 03 — now fixed in `install.bat`'s desktop note, plan §5/§7,
`README.md` and `SETUP.md`.

**Diagnostic lesson:** the reported symptom ("still says scripts are disabled")
and the actual state disagreed. A screenshot settled it in seconds — the bypass
*had* worked and produced a **winget** error; the policy error came from separate
runs without the prefix. When guest state is ambiguous, screenshot before
theorising.

### 2.6 Wrong winget package ID (cost: ~15 min)

`--source msstore` matches on the **Store product ID**, not the AppX package
name. The script passed `AppleInc.iCloud` (the AppX name, correct only on the
`Get-AppxPackage` verify line) and failed with *"No package found matching input
criteria"*.

```
winget search iCloud
  iCloud            9PKTQ5699M62   msstore   <-- correct
  iCloud (Legacy)   Apple.iCloud   winget    <-- legacy standalone, NOT this design
```

Fixed in `provision/02-install-icloud.ps1` and plan §5. Installed result:
`AppleInc.iCloud 15.8.118.0`, PFN `AppleInc.iCloud_nzyj5cx40ttqa`.

---

## 3. Guest control channel (the big unlock)

There is **no** qemu-guest-agent, no WinRM/SSH, and RDP is unusable
non-interactively (the auto-logon `icloud` account has a blank password by
design — plan D8 — and Windows refuses blank-password network logons).

But dockur starts QEMU with a human monitor socket:

```
-monitor unix:/dev/shm/monitor.sock,server,wait=off
```

which gives `sendkey` (type) and `screendump` (see). That is enough to drive the
guest end to end. Wrapped in `tools/guest-ctl.sh` + `tools/qemu-monitor.py`:

```bash
./tools/guest-ctl.sh shot                  # screenshot -> /tmp/guest-screen.png
./tools/guest-ctl.sh type "winget list"    # type, do NOT execute
./tools/guest-ctl.sh shot                  # verify what landed
./tools/guest-ctl.sh enter                 # execute
./tools/guest-ctl.sh run "whoami" 8        # type + enter + wait + screenshot
```

Hard-won details:

- **Never drain the monitor reply per keystroke.** Reading after each `sendkey`
  costs a full socket timeout (~1.5 s/char); a 100-char command took >120 s. Send
  `sendkey` fire-and-forget, and only read for commands whose output you need.
- **Verify before Enter.** Injection is blind; a dropped key silently corrupts
  the command. Always `type` → `shot` → `enter`.
- **Store apps must launch non-elevated.** From an admin prompt use
  `explorer.exe shell:AppsFolder\<PFN>!<AppId>`; find the AppId via
  `Get-StartApps`. (`AppleInc.iCloud_nzyj5cx40ttqa!iCloud`.)
- **Quoting.** Pass text via `--textfile`, not as a shell argument — nested
  `sg docker -c "docker exec … sh -c '…'"` quoting broke a monitor helper and
  made a healthy ISO look missing.
- Typing runs ~0.035 s/char, so ~4 s for a 100-char command.
- **The mouse is relative, not absolute — prefer the keyboard.** `info mice`
  reports `QEMU HID Tablet (absolute)` as active, but human-monitor `mouse_move`
  still delivers **relative deltas**. Sending absolute 0..32767 coordinates
  parked the pointer in the bottom-right corner and clicked *Show desktop*,
  minimising every window mid-setup. `move()` now homes at (0,0) first and steps
  out, but Windows "Enhance pointer precision" scales relative motion
  non-linearly, so it stays approximate. **Use keyboard navigation for anything
  that matters**, and never aim a blind click near a destructive control — the
  iCloud Drive on/off row sits ~60 px from the chevron that opens its settings.
- `screendump` does **not** render the mouse cursor, so you cannot verify
  pointer position from a screenshot — only its *effects*. This is what made the
  bad click hard to spot: the giveaway was a "Show desktop" tooltip in a corner.

### A second, simpler channel: the container's Samba share

dockur runs `smbd` in the container serving `[Data] path=/tmp/smb`, which the
guest sees as **`\\host.lan\Data`** — already connected, no compose change, no
restart. To hand a file to the guest:

```bash
docker cp file.txt icloud-windows:/tmp/smb/
# in the guest:  copy \\host.lan\Data\file.txt "%USERPROFILE%\Desktop\"
```

Use CRLF line endings for text files. Note `./provision` → `C:\OEM` is copied
**at install time only**, so editing `provision/` does not update an already
installed guest — this share is the way to deliver files afterwards. That is
also how `agent.ps1` and `04-bridge-agent.ps1` reach a guest that was built
before those files existed:

```bash
docker cp provision/agent.ps1           icloud-windows:/tmp/smb/
docker cp provision/04-bridge-agent.ps1 icloud-windows:/tmp/smb/
# in the guest:
#   copy \\host.lan\Data\agent.ps1           C:\OEM\
#   copy \\host.lan\Data\04-bridge-agent.ps1 C:\OEM\
```

---

## 4. What a fully automated re-run would look like

```bash
sudo ./host/setup-prereqs.sh            # engine, cifs-utils, context, groups
cp .env.example .env && $EDITOR .env    # SHARE_PASS + sizing
sudo cp -l /srv/isos/win11-25h2-x64.iso /srv/icloud-vm/storage/custom.iso   # no download
docker compose up -d
until python3 tools/rdp-ready.py; do sleep 60; done                    # real check
./tools/guest-ctl.sh run 'winget install --id 9PKTQ5699M62 --source msstore --accept-package-agreements --accept-source-agreements' 120
./tools/guest-ctl.sh run 'explorer.exe shell:AppsFolder\AppleInc.iCloud_nzyj5cx40ttqa!iCloud' 15
#  >>> MANUAL: Apple ID + 2FA, iCloud Drive ON (leave Files On-Demand ON) <<<
./tools/watch-sync.sh                                                  # placeholders settled
#  03-create-share.ps1 with SHARE_PASS  (see SETUP.md section 8)
#  04-bridge-agent.ps1                  (no secret; see SETUP.md section 8)
sudo ./host/setup-host.sh && ./host/acceptance-tests.sh
#  >>> GATE: run E0 before trusting the mount (docs/selective-sync.md) <<<
./gui/install-gui.sh                                                   # as the desktop user
```

Remaining manual surface: **the Apple sign-in, and the E0 gate.**

### Not worth automating

- **Apple ID / 2FA** — out of scope, risks account lockout.
- **iCloud GUI toggle (#12)** — possible via `sendkey`/mouse, but coordinate-
  based GUI automation breaks on every client update. One checkbox, once per
  rebuild; do it by hand. There is no pin step to fall back on any more, so
  confirm the toggle from a screenshot rather than assuming it took.
- **E0** — it deliberately needs a hash known from a *different* Apple device,
  and its whole point is measuring real-world behaviour a script would paper over.

### Worth doing next

- Add `util-linux-extra` to `setup-prereqs.sh`.
- Have `setup-prereqs.sh` fail loudly if the active context is `desktop-linux`.
- Fold `tools/keep-iso.sh` into the boot path so the ISO is preserved by default.
- Teach `acceptance-tests.sh` to use `tools/rdp-ready.py` instead of a bare
  port check.
