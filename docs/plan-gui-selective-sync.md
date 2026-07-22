# v2 Plan — Host GUI + Tray Icon, and Selective Sync (Exclusions)

**Version:** 1.0 · **Status:** Ready to execute · **Audience:** an executor (human or
model) who follows instructions literally. **All decisions are already made.** Do not
substitute components, formats, paths, or names unless a step explicitly offers a
fallback. This document amends `implementation-plan.md` (v1); where they conflict,
this document wins, and §9 below lists the exact v1 edits to make.

---

## 0. What is being built (context)

Two features on top of the existing v1 system (Windows VM runs official iCloud for
Windows; guest SMB share `icloud` is mounted on the Linux host at `/mnt/icloud`):

1. **A simple Linux GUI + tray ("menu bar") icon.** The tray icon shows overall
   health at a glance (green/yellow/red) and its menu opens the iCloud folder, the
   status window, and the VM web viewer. The status window shows detailed health and
   hosts the selective-sync UI.

2. **Selective sync.** v1 downloads *everything* (Files On-Demand off, all files
   pinned). v2 lets the user mark folders or individual files as **excluded**:
   - Excluded items are **not downloaded** into the VM (they become/stay dataless
     "online-only" placeholders — no guest disk cost beyond metadata).
   - Excluded items are **completely invisible** on the Linux host: they do not
     appear in `ls /mnt/icloud`, and they cannot be opened by path.
   - **Safety:** if anything on the host tries to *create* a file or folder whose
     name collides with a hidden excluded item, the operation **fails** (permission
     denied) instead of silently colliding with or corrupting the cloud copy.
   - Exclusion **never deletes anything** from iCloud. Re-including an item makes it
     reappear and re-download.

### Why this design (do not deviate)

The naive approach — filter the view on the Linux side with a FUSE overlay — was
considered and **rejected**: it adds a custom always-on filesystem daemon in the data
path (performance + reliability risk) and still downloads everything into the VM.
Instead, both hiding and no-download are done **inside the guest**, using two stock
Windows mechanisms verified to work on Windows 11 Pro:

- **Pin state via `attrib`**: iCloud for Windows uses the Windows Cloud Files API.
  `attrib +P` (pinned = always keep on disk) and `attrib +U` (unpinned = online-only,
  dataless) control hydration per file/folder. Excluded ⇒ `+U -P` (dehydrated),
  included ⇒ `+P -U` (fully local).
- **Hiding + collision safety via NTFS ACL + Access-Based Enumeration (ABE)**: an
  NTFS *deny* ACE for the SMB account `syncshare` on each excluded path, plus
  `Set-SmbShare -FolderEnumerationMode AccessBased` on the `icloud` share. ABE hides
  entries the connecting user cannot read, so excluded items vanish from host
  directory listings; and because the item still exists on NTFS with a deny ACE, any
  host attempt to create a same-named item (any letter case — NTFS is
  case-insensitive) is rejected by the Windows SMB server with *access denied*. The
  collision check is therefore enforced **server-side, atomically** — no host-side
  race-prone checking needed.

A small **guest agent** (one PowerShell script run by Task Scheduler) enforces the
exclusion list and reports status. The host GUI talks to it through a second, small
SMB share (the **bridge share**) using plain JSON files — no sockets, no new
protocols, same transport that already works in v1.

```
┌──────────────────────────── Linux host ─────────────────────────────┐
│  Tray icon + GUI (Python/PySide6)                                    │
│    │ writes exclusions.json / list requests                          │
│    │ reads  status.json / tree.json / list responses                 │
│    ▼                                                                 │
│  /mnt/icloud_bridge  ◄─ cifs ─►  guest C:\ProgramData\icloud-bridge  │
│                                     ▲          │                     │
│                                     │          ▼                     │
│                                  agent.ps1 (Task Scheduler loop)     │
│                                   • attrib +P / +U  (hydrate/free)   │
│                                   • icacls deny/allow syncshare      │
│                                   • writes status.json, tree.json    │
│                                                                      │
│  /mnt/icloud  ◄─ cifs ─►  guest SMB share "icloud" (ABE ON)          │
│                            excluded items: hidden + create-blocked   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 1. Decisions register (locked)

Continues the v1 register (D1–D13).

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D14 | Files On-Demand | **ON** (amends D5), with pin state managed per-item by the guest agent: included ⇒ pinned (`attrib +P -U`), excluded ⇒ online-only (`attrib +U -P`) | Only way to *not* download excluded data. D5's real requirement — "the host must never read a dataless file" — is preserved differently: included items are always pinned/hydrated, excluded items are unreachable from the host at all |
| D15 | Hiding + collision safety | NTFS deny ACE for `syncshare` on each excluded path + ABE (`FolderEnumerationMode AccessBased`) on the `icloud` share | Server-side, atomic, zero host-side software in the data path. Verified available on Windows 11 Pro client SKU |
| D16 | Host↔guest control channel | Second guest SMB share `bridge` → `C:\ProgramData\icloud-bridge`, mounted on host at **`/mnt/icloud_bridge`** (underscore — avoids systemd unit-name escaping) | Reuses the transport that already exists; JSON files are debuggable with `cat` |
| D17 | Guest agent | Single PowerShell script `agent.ps1`, run as user `icloud` by a Task Scheduler logon task in an infinite loop (restart on failure). Cadence: requests 2 s, status 15 s, enforcement 60 s, tree 10 min | No new runtimes in the guest; Task Scheduler gives auto-start + restart for free |
| D18 | GUI stack | Python 3 + **PySide6** (Qt 6); tray via `QSystemTrayIcon`; autostart via XDG autostart `.desktop` file | Qt tray works across KDE/XFCE/GNOME (GNOME needs the AppIndicator extension — documented, not worked around); PySide6 is apt-installable on Ubuntu 24.04+ (`python3-pyside6.*`), pip fallback |
| D19 | Exclusion config format | JSON list of **paths relative to the sync root**, forward slashes, matched **case-insensitively**, exact-path match (an excluded folder covers its whole subtree via ACL/attrib inheritance, not via prefix matching in code) | Simple, diffable, atomic to replace |
| D20 | Exclusion semantics | Exclude = dehydrate + hide. **Never delete.** Re-include = un-hide + re-hydrate. A file with un-uploaded local changes is *not* force-dehydrated: the agent retries until iCloud reports it uploaded (attrib `+U` on a dirty file is a no-op / fails safely — the Cloud Files API only dehydrates in-sync files) and reports state `pending-upload` meanwhile | Data loss is unacceptable; Apple's engine gates dehydration for us |
| D21 | How the user browses items to exclude | The GUI renders `tree.json` produced *by the guest agent* (which sees everything — ABE only filters `syncshare`). The host mount is never used to enumerate excluded items | Excluded items are invisible on the mount by design; the agent is the only full-tree source. Placeholders report their true logical size, so sizes are correct even for never-downloaded items |
| D22 | Safety invariants | (a) agent never deletes user files; (b) the sync root itself cannot be excluded; (c) GUI only offers paths that exist in `tree.json` — no free-text path entry; (d) on re-include, the deny ACE is removed **before** pinning; on exclude, the deny ACE is added **before** dehydrating; (e) all JSON files written atomically (temp file + rename) | Ordering (d) guarantees the host can never observe a dataless-but-visible file |
| D23 | Status precedence for the tray icon | **red**: mount missing, container not running, or canary stale > 10 min · **yellow**: agent status stale > 90 s, iCloud client process not running, or any exclusion in `error`/`pending` state · **green**: everything else | Single unambiguous mapping so the icon is implementable without judgment |
| D24 | Repo layout for new code | `gui/` (Python package + installer + desktop files), `guest-agent/agent.ps1`, `provision/04-bridge-agent.ps1`, `host/mnt-icloud_bridge.mount` + `.automount` | Mirrors the v1 host/provision split |

**Known accepted limitations (do not attempt to fix):**

- Exclusions are **path-based**. If another Apple device *renames* an excluded
  folder, the new path is no longer excluded and will hydrate + appear on the host.
  Document this; the GUI's status view makes it visible.
- A brand-new cloud item whose path is on the exclusion list is visible on the host
  for up to one enforcement cycle (≤ 60 s) before being hidden.
- A brand-new cloud file inside an *included* folder is dataless for a short window
  before pin-inheritance/agent hydrates it; a host read during that window blocks
  until download completes (SMB read triggers hydration). This is the residual of
  the v1 D5 concern, shrunk to a ≤ 60 s window.
- Apps on the host see "permission denied", not "no such file", if they try to
  create a name colliding with a hidden item. That is the intended safety behavior.

---

## 2. Bridge protocol (exact formats)

All files live in the bridge share root (`C:\ProgramData\icloud-bridge` ==
`/mnt/icloud_bridge`). All JSON is UTF-8, no BOM. Every writer writes to
`<name>.tmp` in the same directory then renames over the target (D22e). Paths in
JSON are relative to the sync root and use forward slashes (`Docs/Big Folder`).

### 2.1 `exclusions.json` — written by GUI, read by agent

```json
{
  "version": 1,
  "revision": 7,
  "exclusions": ["Big Folder", "Docs/huge-video.mp4"]
}
```

- `revision`: integer, incremented by the GUI on every write. The agent echoes the
  last revision it finished processing in `status.json.appliedRevision` so the GUI
  can show "applying…" vs "up to date".
- If the file is absent or unparseable, the agent treats the list as empty **but**,
  if it is unparseable (vs absent), also sets `status.json.lastError` and does NOT
  remove existing deny ACEs that cycle (fail safe: a corrupt file must not
  mass-re-include).

### 2.2 `status.json` — written by agent every 15 s

```json
{
  "version": 1,
  "generatedAt": "2026-07-22T10:15:00+09:00",
  "agentStartedAt": "2026-07-22T08:00:01+09:00",
  "syncRoot": "C:/Users/icloud/iCloudDrive",
  "icloudClientRunning": true,
  "diskFreeBytes": 51200000000,
  "diskTotalBytes": 128000000000,
  "appliedRevision": 7,
  "lastEnforcementAt": "2026-07-22T10:14:30+09:00",
  "lastError": null,
  "exclusions": [
    {"path": "Big Folder", "state": "applied", "detail": "", "logicalBytes": 21474836480},
    {"path": "Docs/huge-video.mp4", "state": "pending-upload", "detail": "waiting for iCloud to finish uploading before freeing space", "logicalBytes": 4294967296}
  ],
  "pendingHydrationCount": 0
}
```

- `exclusions[].state` ∈ `applying` | `applied` | `pending-upload` | `not-found` |
  `error`. `applied` means: deny ACE present **and** item fully dataless.
  `not-found` means the path does not (yet) exist under the sync root — the agent
  keeps checking each cycle (the item may arrive from the cloud later) and hides it
  the moment it appears.
- `icloudClientRunning`: true iff a process named `iCloudServices` or `iCloudDrive`
  exists.
- `pendingHydrationCount`: number of files in *included* areas still dataless
  (attribute Unpinned/RecallOnDataAccess set).

### 2.3 `tree.json` — written by agent every 10 min and immediately after each enforcement pass that changed anything

Folders only (files are fetched on demand via §2.4 — a full-file tree could be
100k+ entries).

```json
{
  "version": 1,
  "generatedAt": "2026-07-22T10:10:00+09:00",
  "root": {
    "dirs": [
      {
        "name": "Docs",
        "path": "Docs",
        "logicalBytes": 1073741824,
        "fileCount": 240,
        "dirCount": 3,
        "excluded": false,
        "dirs": [ {"name": "Big Folder", "path": "Docs/Big Folder", "logicalBytes": 21474836480, "fileCount": 900, "dirCount": 12, "excluded": true, "dirs": []} ]
      }
    ]
  }
}
```

- `logicalBytes` is the recursive logical size (placeholder logical size counts —
  i.e. the size the data *would* occupy), so the GUI can show "exclude this 20 GB
  folder" before anything is downloaded.
- For an `excluded` folder, `dirs` may be truncated to `[]` (the agent does not need
  to recurse inside excluded subtrees; sizes for them are computed once when first
  seen and cached in memory — recompute only if the folder's `LastWriteTime`
  changed).

### 2.4 Per-folder file listing (request/response)

- GUI writes `requests/list-<random8hex>.json`: `{"path": "Docs"}` (empty string =
  sync root).
- Agent (polling `requests/` every 2 s) writes
  `responses/list-<same-id>.json`:

```json
{
  "path": "Docs",
  "error": null,
  "files": [
    {"name": "notes.txt", "path": "Docs/notes.txt", "logicalBytes": 1024, "excluded": false, "dataless": false}
  ]
}
```

then deletes the request file. GUI deletes the response after reading. Agent
garbage-collects any request/response older than 10 minutes. On error (path missing
etc.) `error` is a message string and `files` is `[]`.

---

## 3. Guest agent — `guest-agent/agent.ps1`

One file, PowerShell 5.1 compatible (ships with Windows). Constants at top:

```powershell
$SyncRoot  = "$env:USERPROFILE\iCloudDrive"
$BridgeDir = "C:\ProgramData\icloud-bridge"
$ShareUser = "syncshare"
# Cloud Files / FILE_ATTRIBUTE values
$ATTR_PINNED   = 0x00080000   # FILE_ATTRIBUTE_PINNED
$ATTR_UNPINNED = 0x00100000   # FILE_ATTRIBUTE_UNPINNED
$ATTR_RECALL   = 0x00400000   # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (dataless)
```

Main loop: `while ($true) { ... ; Start-Sleep -Seconds 2 }` with counters so that
request-handling runs every iteration (2 s), status every ~15 s, enforcement every
~60 s, tree every ~10 min (also right after an enforcement pass that changed
state). Wrap each sub-task in `try/catch`; a failure sets `lastError` in the next
`status.json` but never exits the loop.

### 3.1 Enforcement pass (the core — implement exactly this order)

```text
read exclusions.json -> $wanted (list of relative paths), $revision
normalize: trim, convert "/" to "\", drop empties, drop "." and anything
           containing "..", drop "" (root) [D22b/c]
$current = paths that currently carry a syncshare deny ACE (persisted by the agent
           in C:\ProgramData\icloud-bridge\state\applied.json after each pass —
           list of paths it has applied; also self-heals: verify with icacls)

# --- re-includes first (paths in $current but not in $wanted) ---
for each $p:
    icacls "$SyncRoot\$p" /remove:d $ShareUser            # un-hide FIRST (D22d)
    if directory: attrib +P -U "$SyncRoot\$p" /S /D       # then re-hydrate
    else:         attrib +P -U "$SyncRoot\$p"
    remove $p from applied.json

# --- excludes (paths in $wanted) ---
for each $p:
    if not (Test-Path "$SyncRoot\$p"): state = not-found; continue
    if directory:
        icacls "$SyncRoot\$p" /deny "${ShareUser}:(OI)(CI)F"   # hide FIRST (D22d)
        attrib +U -P "$SyncRoot\$p" /S /D                      # then dehydrate
    else:
        icacls "$SyncRoot\$p" /deny "${ShareUser}:F"
        attrib +U -P "$SyncRoot\$p"
    add $p to applied.json
    # verify dehydration: file(s) still lacking ATTR_RECALL and having size-on-disk
    # > 0 mean iCloud hasn't freed them yet (usually: upload not finished).
    state = applied | pending-upload accordingly

# --- hydration sweep of included areas ---
recursive walk from $SyncRoot, PRUNING at any path in $wanted:
    any item with ($ATTR_UNPINNED or not $ATTR_PINNED) -> attrib +P -U <item>
    count items still dataless -> pendingHydrationCount
```

Notes for the executor:

- Use `[System.IO.Directory]::EnumerateFileSystemEntries` + `File.GetAttributes`
  for the walk (fast, gives raw attribute bits). Do **not** shell out to
  `attrib` for *reading* state — only for setting it.
- Logical size of a dataless file: `(Get-Item $f).Length` (correct for
  placeholders). Size-on-disk (to verify dehydration) via
  `fsutil file layout` is overkill — checking the `$ATTR_RECALL`/`$ATTR_UNPINNED`
  bits is sufficient: treat "has UNPINNED bit and has RECALL bit" as dataless.
- `attrib +U` on a file whose local changes are not yet uploaded does not lose
  data — the Cloud Files API dehydrates only in-sync files. The agent just
  re-checks next cycle and reports `pending-upload` until it becomes dataless
  (D20).
- Case-insensitive path comparisons everywhere (`.ToLowerInvariant()` on both
  sides) — NTFS is case-insensitive (D19).

### 3.2 Files the agent maintains

`status.json`, `tree.json`, `responses/*`, `state/applied.json` (its own memory of
applied deny ACEs). It never writes inside `$SyncRoot` and never deletes anything
inside `$SyncRoot` (D22a — enforce by simply having no delete call in the script).

---

## 4. Guest provisioning — `provision/04-bridge-agent.ps1`

Run **as Administrator** in the guest after v1 scripts 01–03. Idempotent (safe to
re-run, like 01/03). Contents, in order:

1. `New-Item -ItemType Directory -Force` for `C:\ProgramData\icloud-bridge`,
   `...\requests`, `...\responses`, `...\state`.
2. Copy `C:\OEM\agent.ps1` → `C:\ProgramData\icloud-bridge\agent.ps1`
   (docker-compose already mounts `./provision` at `/oem` → `C:\OEM`; add
   `guest-agent/agent.ps1` to that mount by copying it into `provision/` at build
   time — see §8 task A3 — so it lands in `C:\OEM`).
3. `icacls C:\ProgramData\icloud-bridge /grant "syncshare:(OI)(CI)M" /T`
4. `New-SmbShare -Name "bridge" -Path "C:\ProgramData\icloud-bridge" -FullAccess "syncshare"`
   (wrap in `if (-not (Get-SmbShare -Name bridge -ErrorAction SilentlyContinue))`).
5. **Enable ABE on the data share:**
   `Set-SmbShare -Name "icloud" -FolderEnumerationMode AccessBased -Force`
6. Register the agent task (idempotent — `-Force`):

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\ProgramData\icloud-bridge\agent.ps1"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "icloud"
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # no time limit
Register-ScheduledTask -TaskName "icloud-bridge-agent" -Action $action `
  -Trigger $trigger -Settings $settings -User "icloud" -Force
Start-ScheduledTask -TaskName "icloud-bridge-agent"
```

7. Print a green "bridge ready" message.

---

## 5. Host-side mount for the bridge share

Two new unit files (names contain an underscore, **not** a hyphen, so no systemd
escaping is needed — D16):

`host/mnt-icloud_bridge.mount` → `/etc/systemd/system/mnt-icloud_bridge.mount`

```ini
[Unit]
Description=iCloud bridge control share (CIFS)
Requires=docker.service
After=docker.service

[Mount]
What=//127.0.0.1/bridge
Where=/mnt/icloud_bridge
Type=cifs
Options=credentials=/etc/credentials-icloud,port=10445,vers=3.1.1,uid=1000,gid=1000,file_mode=0664,dir_mode=0775,actimeo=1,echo_interval=15,_netdev
TimeoutSec=30

[Install]
WantedBy=multi-user.target
```

`host/mnt-icloud_bridge.automount` — same as the v1 automount but
`Where=/mnt/icloud_bridge`.

`host/setup-host.sh` changes: also `mkdir -p /mnt/icloud_bridge`, install both new
units, `systemctl enable --now mnt-icloud_bridge.automount`. Same credentials file
(the `syncshare` user has access to both shares).

---

## 6. Host GUI — `gui/`

### 6.1 Layout

```
gui/
├── icloud_bridge_gui/
│   ├── __init__.py
│   ├── __main__.py        # QApplication + tray + window wiring; single-instance lock
│   ├── health.py          # host-side checks (no bridge): container/mount/canary
│   ├── bridge.py          # bridge share I/O: read status/tree, write exclusions,
│   │                      #   list-request round-trip (async via QTimer polling)
│   ├── tray.py            # QSystemTrayIcon: icon state (D23), menu
│   ├── window.py          # QMainWindow with 2 tabs: Status, Selective Sync
│   └── icons/             # icloud-green.svg, icloud-yellow.svg, icloud-red.svg
│                          #   (simple cloud glyph, solid fill in the state color)
├── icloud-bridge-gui.desktop        # launches the app (window)
├── autostart/icloud-bridge-tray.desktop  # launches the app minimized to tray
└── install-gui.sh
```

### 6.2 Behavior — exact spec

**Health model (`health.py`)** — every check returns `(ok: bool, detail: str)`:

- container: `docker inspect -f '{{.State.Running}}' icloud-windows` == `true`
  (subprocess, 5 s timeout).
- mount: `os.path.ismount('/mnt/icloud')`; same for `/mnt/icloud_bridge`.
- canary: `/mnt/icloud/.linux-canary` mtime age < 600 s (this reuses the v1 health
  timer's canary; the GUI does not write the canary itself).
- bridge agent: `status.json` exists and `generatedAt` age < 90 s.
- iCloud client: `status.json.icloudClientRunning`.
- exclusions: all states == `applied`/`not-found` ⇒ ok; any
  `applying`/`pending-upload` ⇒ warn; any `error` or `appliedRevision` <
  GUI's last written revision for > 5 min ⇒ warn.

Overall state per D23. Refresh every 5 s on a `QTimer` (all checks are cheap; run
the `docker inspect` subprocess with a thread or `QProcess` so the UI never
blocks).

**Tray (`tray.py`):**

- Icon = colored SVG per overall state; tooltip = one line per failing check, or
  "iCloud bridge: healthy".
- Left-click and menu item "Open status window" → show/raise the window.
- Menu: **Open iCloud folder** (`xdg-open /mnt/icloud`), **Open status window**,
  **Open VM screen** (`xdg-open http://127.0.0.1:8006`), separator, **Quit**.
- Closing the window hides it (app keeps running in tray); Quit exits.

**Status tab:** one row per check above — colored dot, name, detail, plus guest
disk free/total from `status.json`. Buttons: the same three actions as the tray
menu.

**Selective Sync tab:**

- `QTreeWidget`, columns: *Name*, *Size* (human-readable from `logicalBytes`),
  *Items* (`fileCount`), *Synced* (checkbox: checked = synced, unchecked =
  excluded), *State* (from `status.json.exclusions`, empty for synced items).
- Populated from `tree.json` (folders). Expanding a folder fires a §2.4 list
  request; when the response arrives (poll `responses/` on a 1 s `QTimer`, 15 s
  timeout → show "guest agent not responding"), files are inserted as child rows
  with the same columns.
- Excluded rows: greyed text + unchecked box. An item inside an excluded folder is
  not shown with its own checkbox (whole subtree is excluded; `tree.json` doesn't
  recurse there).
- Unchecking an item queues an exclusion; checking removes it. Nothing is written
  until the user clicks **Apply**. Apply shows one confirmation dialog listing the
  changes with exact wording:
  - for new exclusions: *"These items will disappear from /mnt/icloud on this
    computer and their space will be freed in the VM. They remain safe in iCloud
    and on your other devices. Nothing will be deleted."*
  - for re-includes: *"These items will re-download into the VM (uses VM disk
    space) and reappear in /mnt/icloud."*
- On confirm: write `exclusions.json` atomically with `revision + 1`; the *State*
  column then tracks `status.json` (`applying` → `applied`, etc.).
- The root row cannot be unchecked (D22b). There is no free-text path input
  (D22c).

**Single instance:** on startup, try to bind an abstract Unix socket
(`\0icloud-bridge-gui`); if taken, exit 0 (the autostart entry and the .desktop
entry may both fire).

### 6.3 `install-gui.sh` (run as the desktop user, not root)

1. Dependencies: `sudo apt-get install -y python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore 2>/dev/null || pip install --user PySide6`.
2. Copy `icloud_bridge_gui/` to `~/.local/share/icloud-bridge-gui/`.
3. Install `icloud-bridge-gui.desktop` to `~/.local/share/applications/` and
   `autostart/icloud-bridge-tray.desktop` to `~/.config/autostart/`. Both use
   `Exec=python3 -m icloud_bridge_gui` with
   `Path=~/.local/share/icloud-bridge-gui` (expand `$HOME` at install time —
   `.desktop` files do not expand `~`).
4. Print a note: *"GNOME users: install the 'AppIndicator and KStatusNotifierItem
   Support' extension for the tray icon to be visible."*

---

## 7. Amendments to v1 documents (make these exact edits)

1. `docs/implementation-plan.md` §6 step 5: change "**disable Files On-Demand**"
   to "**leave Files On-Demand ON** (v2 manages per-item pinning — see
   `plan-gui-selective-sync.md`, D14)". Delete step 7 (the global
   `attrib +P` pin) and renumber. Add one row to the decisions register: *"D5
   amended by D14 (see plan-gui-selective-sync.md)"*.
2. `docs/implementation-plan.md` §1 sizing rule: append *"Excluded folders (v2
   selective sync) do not count toward iCloud data size — placeholders are
   metadata-only."*
3. `README.md`: add the two v2 features to the feature description, add the
   `04-bridge-agent.ps1` step and `install-gui.sh` step to the Quickstart, add the
   GNOME tray note, and add to the usage-rules paragraph: *"Excluded items are
   hidden, not gone — attempts to create a file or folder with the same name as an
   excluded item will fail with 'permission denied'. Un-exclude it in the GUI
   instead."* Also remove the duplicated "## Status" section (pre-existing defect:
   README currently has two).

---

## 8. Task checklist (execute in order)

### Phase A — guest side

- [ ] **A1** Write `guest-agent/agent.ps1` per §3 (constants, loop cadence,
  enforcement order, bridge files per §2). PowerShell 5.1 syntax only.
- [ ] **A2** Write `provision/04-bridge-agent.ps1` per §4.
- [ ] **A3** Ensure `agent.ps1` reaches `C:\OEM`: add a build step to
  `provision/install.bat` or simply commit a copy at `provision/agent.ps1` with a
  header comment "`copied by 04-bridge-agent.ps1; source of truth:
  guest-agent/agent.ps1`" — **chosen: commit the copy**, and add a CI-less
  guard: a line in `host/acceptance-tests.sh` that diffs the two files and fails
  if they diverge.
- [ ] **A4** Done-when: running 04 in a guest yields — `Get-SmbShare bridge`
  exists; `Get-SmbShare icloud | Select FolderEnumerationMode` = `AccessBased`;
  `Get-ScheduledTask icloud-bridge-agent` state `Running`; `status.json` fresher
  than 30 s in `C:\ProgramData\icloud-bridge`.

### Phase B — host mounts + scripts

- [ ] **B1** Add `host/mnt-icloud_bridge.mount` and `.automount` per §5.
- [ ] **B2** Extend `host/setup-host.sh` to install/enable them (mirror the
  existing mount install code path exactly).
- [ ] **B3** Extend `host/acceptance-tests.sh`: bridge mounted; `status.json`
  age < 90 s; `tree.json` parses (`python3 -m json.tool`); plus the A3 diff guard.
- [ ] **B4** Done-when: `ls /mnt/icloud_bridge/status.json` works on a live
  system and the extended acceptance script passes.

### Phase C — GUI

- [ ] **C1** Scaffold `gui/icloud_bridge_gui/` per §6.1 with the three SVG icons.
- [ ] **C2** Implement `health.py` (§6.2 health model) + unit-testable pure
  functions for state mapping (D23). Add `gui/tests/test_health.py` covering the
  red/yellow/green precedence with fabricated inputs (no docker/mount needed —
  inject check results).
- [ ] **C3** Implement `bridge.py`: atomic `write_exclusions(paths, revision)`,
  `read_status()`, `read_tree()`, `request_listing(path) -> request_id` /
  `poll_response(request_id)`. Add `gui/tests/test_bridge.py` using a tmpdir as a
  fake bridge share (round-trip exclusions write, request/response file dance,
  malformed-JSON tolerance).
- [ ] **C4** Implement `tray.py` + `window.py` + `__main__.py` per §6.2 (tabs,
  tree with checkboxes, Apply dialog with the exact wording, single-instance).
- [ ] **C5** `install-gui.sh` + both `.desktop` files per §6.3.
- [ ] **C6** Done-when: `python3 -m icloud_bridge_gui` on a machine with a fake
  bridge dir (point it via env var `ICLOUD_BRIDGE_DIR`, default
  `/mnt/icloud_bridge`; also `ICLOUD_MOUNT_DIR` default `/mnt/icloud` — implement
  these env overrides, they are also what the tests use) shows the window, the
  tray icon, and a working exclude→Apply→`exclusions.json` flow; `pytest gui/tests`
  passes.

### Phase D — docs

- [ ] **D1** Apply the §7 edits to `implementation-plan.md` and `README.md`.
- [ ] **D2** Add a short `docs/selective-sync.md` user page: what exclusion
  does/doesn't do, the rename limitation, the permission-denied collision
  behavior, how to re-include.

### Phase E — live acceptance tests (require real VM; run at deployment, keep as a checklist in `docs/selective-sync.md`)

- [ ] **E1** Exclude a test folder in the GUI → within 2 min it disappears from
  `ls /mnt/icloud` and in the guest `attrib` shows `U` on it; guest disk free
  space grows by roughly its size.
- [ ] **E2** `mkdir "/mnt/icloud/<ExcludedName>"` and `touch` of an excluded file
  name both fail with permission denied — including with different letter case.
- [ ] **E3** Re-include → within 2 min it reappears, `attrib` shows `P`, contents
  readable and byte-identical; item was never absent from iCloud web during the
  whole cycle.
- [ ] **E4** Modify a file on the host, then immediately exclude its parent →
  state shows `pending-upload` until the edit is on iCloud web, then `applied`;
  the edited content is what's in iCloud.
- [ ] **E5** Create a file in an *included* folder from an iPhone → hydrated and
  readable on the host ≤ 2 min. Create an item at an excluded path from an
  iPhone → hidden on the host ≤ 2 min.
- [ ] **E6** `docker stop icloud-windows` → tray red ≤ 15 s; start again → green.
  Stop the guest scheduled task → tray yellow ≤ 2 min.
- [ ] **E7** Reboot host and guest → agent auto-starts, exclusions still enforced
  (hidden + dataless), no manual action.

---

## 9. Out of scope for v2 (explicitly)

- Pattern/glob exclusions (e.g. `*.mp4`) — path-list only.
- Following renames of excluded items.
- A host-side FUSE filter layer.
- Pause/resume sync from the GUI.
- Any Photos support (unchanged from v1).
