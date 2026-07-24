# v2 Plan — Host GUI + Tray Icon, and Selective Sync (Exclusions)

**Version:** 1.3 · **Status:** Ready for E0; implementation is gated on it ·
**Audience:** an executor (human or model) who follows instructions literally.
**All implementation decisions are made, subject to E0 passing.** Run E0 before
phases A–D. Do not substitute components, formats, paths, or names unless a step
explicitly offers a fallback. This document amends
`docs/implementation-plan.md` (v1); where they conflict, this document wins, and §7
below lists the exact v1 edits to make.

**v1.2 change note:** retains v1.1's live finding that SMB reads hydrate on
demand, but corrects the implementation plan around it. In particular: E0 now
really runs first; dataless state is detected from `RECALL_ON_DATA_ACCESS`, not
`UNPINNED`; dehydration is treated as asynchronous; old v1 pinning is cleared
once without evicting the cache; ACLs protect excluded items from delete/rename
as well as reads; private agent code/state are no longer SMB-writable; paths and
JSON fail closed; cache reporting uses allocated bytes; and the GUI/provisioning
edge cases are specified.

**v1.3 change note:** three follow-ups to v1.2, no architecture change.
(a) The routine ten-minute scan is now a cheap attribute enumeration reporting
`fullyLocalLogicalBytes`; per-file `CfGetPlaceholderInfo` is confined to three
bounded cases, and the reclamation sweep is two-stage (D26, §2.2, §3.1).
(b) The agent's authority to edit DACLs no longer relies on `icloud` happening
to own every object — provisioning grants an explicit inheritable
`READ_CONTROL`+`WRITE_DAC` ACE, and the agent preflights it (D28, §4 step 4).
(c) The stale README instruction in §7 is corrected. All three came from review
of v1.2 against the live repository.

---

## 0. What is being built (context)

Two features on top of the existing v1 system (Windows VM runs official iCloud for
Windows; guest SMB share `icloud` is mounted on the Linux host at `/mnt/icloud`):

1. **A simple Linux GUI + tray ("menu bar") icon.** The tray icon shows overall
   health at a glance (green/yellow/red) and its menu opens the iCloud folder, the
   status window, and the VM web viewer. The status window shows detailed health and
   hosts the selective-sync UI.

2. **Selective sync.** v1 planned to download *everything* (Files On-Demand off,
   all files pinned); §0.5 removed that requirement. In v2 Files On-Demand stays
   on and nothing is pinned by this project. Cloud placeholders hydrate lazily
   when the host reads them; hydrated and host-created files remain cached until
   disk reclamation needs the space. On top of that, v2 lets the user mark
   folders or individual files as **excluded**:
   - Once protection is applied, excluded items **cannot be hydrated by a host
     read**: the deny ACE makes them unreadable. Any content cached before the
     exclusion is reclaimed asynchronously once it is in sync; the final state
     is a placeholder with no local content allocation beyond metadata.
   - Excluded items are **completely invisible** on the Linux host: they do not
     appear in `ls /mnt/icloud`, and they cannot be opened by path.
   - **Safety:** host reads, writes, deletes, renames, and attempts to create a
     same-named item all fail with permission denied. A guard ACE on the parent
     is required as well as the deny ACE on the item; without it, Windows can
     authorize delete/rename through the parent's `DELETE_CHILD` right.
   - Exclusion **never deletes anything** from iCloud. Re-including an item makes it
     reappear; its content downloads only when read.

### Why this design (do not deviate)

The naive approach — filter the view on the Linux side with a FUSE overlay — was
considered and **rejected**: it adds a custom always-on filesystem daemon in the data
path (performance + reliability risk) and still downloads everything into the VM.
Instead, both hiding and no-download are done **inside the guest**, using two stock
Windows mechanisms verified to work on Windows 11 Pro:

- **Pin state via `attrib`**: iCloud for Windows uses the Windows Cloud Files API.
  `attrib +U -P` requests the online-only state; the provider performs actual
  dehydration asynchronously. `attrib +P -U` would force-keep content on disk
  but is not used (§0.5: pinning is unnecessary for readability). Excluded ⇒
  request online-only; included ⇒ hydrate on first read and remain cached until
  the same online-only request is used by disk reclamation (D26). The `U` bit is
  user intent, **not proof that content is dataless**; §3 uses `RECALL_ON_DATA_ACCESS`
  and allocated size for observation.
- **Hiding + collision safety via NTFS ACL + Access-Based Enumeration (ABE)**: an
  explicit NTFS *deny Full* ACE for the SMB account `syncshare` on each excluded
  path, a *deny Delete Child* guard on its parent, plus
  `Set-SmbShare -FolderEnumerationMode AccessBased` on the `icloud` share. ABE
  hides entries the connecting user cannot read. The target deny blocks opens;
  the parent guard closes Windows' alternative delete/rename authorization path;
  and the existing object makes same-name creation fail case-insensitively. These
  checks are server-side. E2 must still prove the exact Windows 11 + SMB behavior
  before this is trusted with real data.

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
│  /mnt/icloud_bridge  ◄─ cifs ─►  guest ...\icloud-bridge\io          │
│                                     ▲          │                     │
│                                     │          ▼                     │
│                                  agent.ps1 (Task Scheduler loop)     │
│                                   • attrib +U -P (request free space)│
│                                   • target + parent-guard ACLs       │
│                                   • writes status.json, tree.json    │
│                                                                      │
│  /mnt/icloud  ◄─ cifs ─►  guest SMB share "icloud" (ABE ON)          │
│                            excluded items: hidden + create-blocked   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 0.5 Verified findings (2026-07-22/23) — v1's D5 is disproven

Live tests against the running guest (Windows 11 Pro, iCloud for Windows
15.8.118.0, ~101 GB library, all dataless) over the loopback SMB port, using a
read-only test share (`icloudtest`, created by `tools/test-smb-hydration.ps1`;
host side driven by `tools/test-smb-read.sh` / smbclient in a throwaway
container). Two independent sessions reproduced the same results:

| # | Test | Result |
|---|------|--------|
| 1 | Recursive listing of ~6,000 dataless files | Instant; correct logical sizes; entries flagged offline (`O`/`o` attribute) |
| 2 | Read a 697 B dataless placeholder | Hydrated on demand, correct content, ~1.4 s |
| 3 | Read a 1.17 MB dataless placeholder | Hydrated, all 1,169,428 bytes, checksummed, ~2.8 s |
| 4 | Read a 602,670 B dataless placeholder (`2024-08.md`) | Before: attrs `0x401620` (OFFLINE\|SPARSE\|REPARSE\|RECALL_ON_DATA_ACCESS). Fetched in 3.8 s (~160 KB/s, Apple-download-bound), SHA-256 verified. After: attrs `0x420` — fully local |
| 5 | Re-read the same file warm | 12.8 MB/s (~80× the cold read), identical SHA-256; served from local content rather than Apple-bound |
| 6 | Files listed but never read | Stay dataless (verified before/after on neighbors); no content hydration |

**Conclusion:** the Cloud Files filter hydrates on demand for SMB-originated
reads, exactly like OneDrive. D5's claim that dataless placeholders "stall or
fail" over SMB is **wrong** for this setup. Pinning is not required for
correctness; a hydrated file stays local (not pinned) until something dehydrates
it.

**Consequences adopted in this plan (v1.2):**

- Default policy: **Files On-Demand on, nothing pinned, hydrate on read** (D25).
  No upfront content download; metadata population still takes time. Hydrated and
  newly written files form a local cache, so the guest disk no longer has to hold
  the whole library at once.
- The agent no longer pins included items; its hydration sweep is replaced by a
  **disk-reclamation sweep** (`attrib +U -P` on cold cached files, D26), because
  reads hydrate *permanently* and would otherwise eventually fill the disk.
- Exclusions (hide + deny ACE + dehydrate) remain: they are still the only way
  to hide clutter from `/mnt/icloud`, prevent host access from triggering
  hydration, and keep collision safety. They do not sandbox the interactive
  `icloud` user inside the guest.

**What the tests did NOT prove (scope limits — closed by E0 in §8):**

1. **Kernel CIFS is untested.** Tests used `smbclient` (userland, generous
   `timeout 180`). The real mount uses kernel cifs with v1's options
   (`actimeo=1,echo_interval=15`); a slow hydration could surface as EIO or a
   hung task if it exceeds the kernel client's per-request patience.
2. **Largest file read: 1.17 MB (the detailed before/after sample was 602 KB).**
   The library holds
   multi-GB items. Observed cold throughput on small files was 0.16–0.43 MB/s,
   but that is dominated by per-file latency — the sustained rate on large
   files is unknown. A 2 GB hydration could take minutes to hours and blocks
   the reading process throughout — a timeout and UX risk even if it succeeds.
3. **Write path untested.** The test share was deliberately read-only;
   host→guest→iCloud writes are unproven (they were also unproven in v1).

## 0.6 Primary technical references for v1.2

- Microsoft documents `attrib +U` as an online-only request and explains that,
  without platform auto-dehydration, clearing pinned/setting unpinned is handled
  asynchronously by the sync engine: [Files On-Demand states](https://learn.microsoft.com/en-us/sharepoint/files-on-demand-windows),
  [Cloud Files sync policies](https://learn.microsoft.com/en-us/windows/win32/api/cfapi/ns-cfapi-cf_sync_policies).
- `UNPINNED` means "should not be kept fully present" while
  `RECALL_ON_DATA_ACCESS` means content is not fully local; they are not the same
  state: [file attribute constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants).
- `CfGetPlaceholderInfo` exposes on-disk, modified, pin, and in-sync state without
  modifying the file and needs only `READ_ATTRIBUTES`:
  [CfGetPlaceholderInfo](https://learn.microsoft.com/en-us/windows/win32/api/cfapi/nf-cfapi-cfgetplaceholderinfo).
  Cloud Files rejects dehydration of a placeholder that is not in sync:
  [CfUpdatePlaceholder requirements](https://learn.microsoft.com/en-us/windows/win32/api/cfapi/nf-cfapi-cfupdateplaceholder).
- Windows allows delete/rename with `DELETE` on the object **or** delete-child on
  the parent, which is why D15 needs both deny layers:
  [DeleteFile remarks](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-deletefile).
- Explicit ACEs precede inherited ACEs, and inheritable ACEs propagate to
  existing NTFS children. This is why §4 removes v1's explicit descendant grants:
  [DACL ordering](https://learn.microsoft.com/en-us/windows/win32/secauthz/order-of-aces-in-a-dacl),
  [automatic ACE propagation](https://learn.microsoft.com/en-us/windows/win32/secauthz/automatic-propagation-of-inheritable-aces).
- SMB AccessBased enumeration omits entries the connecting user lacks rights to
  access: [New-SmbShare `FolderEnumerationMode`](https://learn.microsoft.com/en-us/powershell/module/smbshare/new-smbshare).
- An object's owner is implicitly granted `READ_CONTROL` and `WRITE_DAC`, but
  ownership of a new object depends on who creates it, so it is not a dependable
  basis for the agent's ACL authority (D28):
  [owner of a new object](https://learn.microsoft.com/en-us/windows/win32/secauthz/owner-of-a-new-object).
  `icacls` spells those two rights `RC` and `WDAC` in its advanced-rights list:
  [icacls](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls).

---

## 1. Decisions register (locked)

Continues the v1 register (D1–D13).

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D14 | Files On-Demand | **ON** (amends D5). No project-managed pinning: included placeholders hydrate on first host read and cached content remains until reclamation; excluded items receive an online-only request (`attrib +U -P`) | §0.5 disproved D5. `+U -P` is asynchronous; `U` expresses intent and `RECALL_ON_DATA_ACCESS` observes that content is not fully local |
| D15 | Hiding + collision/deletion safety | Explicit deny-Full ACE for `syncshare` on each excluded root; a non-inheriting deny-`DELETE_CHILD` ACE on each excluded root's parent; ABE (`FolderEnumerationMode AccessBased`) on `icloud` | A target deny alone is insufficient because Windows permits delete/rename with either `DELETE` on the target **or** `DELETE_CHILD` on its parent. The paired ACEs block both paths while included siblings remain deletable through their own `DELETE` right. E2 verifies the complete behavior live |
| D16 | Host↔guest control channel | Second guest SMB share `bridge` → `C:\ProgramData\icloud-bridge\io`, mounted on host at **`/mnt/icloud_bridge`** (underscore — avoids systemd unit-name escaping) | Reuses the transport that already exists; JSON files are debuggable with `cat`; D27 keeps executable/private files outside it |
| D17 | Guest agent | Single PowerShell script `agent.ps1`, run as user `icloud` by a Task Scheduler logon task in an infinite loop (restart on failure). Cadence: requests 2 s, status 15 s, enforcement 60 s, tree 10 min | No new runtimes in the guest; Task Scheduler gives auto-start + restart for free |
| D18 | GUI stack | Python 3 + **PySide6** (Qt 6); tray via `QSystemTrayIcon`; autostart via XDG autostart `.desktop` file. Prefer distro PySide6 packages; fallback is a dedicated venv, never a PEP-668-breaking user/system pip install | Qt tray works across KDE/XFCE/GNOME (GNOME needs the AppIndicator extension — documented); the venv keeps fallback dependencies isolated |
| D19 | Exclusion config format | JSON list of canonical paths **relative to the sync root**, forward slashes, compared case-insensitively. Reject rooted/escaping/invalid paths and the root itself. Canonicalize to a minimal antichain: an excluded path removes all descendant entries | Simple and diffable without ambiguous nested exclusions (including while a path is `not-found`); agent-side containment checks treat the SMB-writable JSON as untrusted input |
| D20 | Exclusion semantics | Exclude = deny access immediately, then request dehydration. **Never delete.** Re-include = remove agent-owned deny/guard ACEs only; content remains online-only/cached according to its current provider state and hydrates when read. Dehydration is asynchronous and only allowed by Cloud Files for in-sync placeholders, so a still-local item is `pending-dehydrate`; the agent does not guess that upload is the cause | Data loss is unacceptable. The platform's in-sync gate is documented, but iCloud's behavior must still pass E4 before relying on it |
| D21 | How the user browses items to exclude | The GUI renders `tree.json` produced *by the guest agent* (which sees everything — ABE only filters `syncshare`). The host mount is never used to enumerate excluded items | Excluded items are invisible on the mount by design; the agent is the only full-tree source. Placeholders report their true logical size, so sizes are correct even for never-downloaded items |
| D22 | Safety invariants | (a) agent never deletes user files; (b) the sync root cannot be excluded; (c) GUI has no free-text path entry; (d) agent independently validates canonical containment; (e) apply all new parent guards and target denies before removing obsolete ones, and deny before requesting dehydration; (f) missing/malformed/invalid config fails closed; (g) JSON replacement is atomic | Prevents traversal/config corruption, exposure during transitions, and accidental mass re-inclusion |
| D23 | Status precedence for the tray icon | **red**: container not running, either mount missing, or canary missing/stale > **15 min**/implausibly future-dated · **yellow**: agent timestamp invalid/stale > 90 s/future-dated, tree missing/stale > 20 min, iCloud client liveness false, `lastError` set, any exclusion in `applying`/`pending-dehydrate`/`not-found`/`error`, or reclamation is in progress/free space remains below 20 GB · otherwise **green** | Fifteen minutes gives the v1 ten-minute canary timer scheduling slack; clock anomalies are explicit rather than silently treated fresh; bridge loss is red because both control and status are unavailable |
| D24 | Repo layout for new code | `gui/` (Python package + installer + desktop files), `guest-agent/agent.ps1`, `provision/04-bridge-agent.ps1`, `host/mnt-icloud_bridge.mount` + `.automount` | Mirrors the v1 host/provision split |
| D25 | Default hydration policy | **Files On-Demand on; nothing pinned by this project; hydrate lazily on read.** On first agent run, clear legacy v1 `P` intent with `attrib -P` but do not evict cached data. Initial cloud placeholders stay online-only; reads and host writes remain cached until D26 needs space | Verified for small SMB reads in §0.5, pending E0 for the real mount/large files. Avoids an upfront library download without falsely claiming every included file is always dataless |
| D26 | Disk reclamation | When volume free space < **20 GB**, request online-only (`attrib +U -P`) for included, in-sync files with local content, oldest first, until **requested on-disk bytes** cover the deficit to 30 GB. Dehydration is asynchronous: re-measure on later cycles and continue until free ≥ **30 GB** or no eligible allocation remains. The sweep is **two-stage**: stage 1 considers files **without** `RECALL_ON_DATA_ACCESS` (fully local — the hydrated working set and the bulk of reclaimable bytes); only if stage 1 cannot cover the deficit does stage 2 examine `RECALL` files in bounded, cursor-based batches to find partially hydrated placeholders. Read `OnDiskDataSize`, `ModifiedDataSize`, and `InSyncState` with `CfGetPlaceholderInfo`; use `GetCompressedFileSizeW` only for non-placeholders. Use `LastAccessTime` only when NTFS last-access updates are enabled; otherwise use `LastWriteTime` as an explicitly approximate fallback. Dirty/open/not-in-sync files remain local and are reported, not deleted | Hysteresis prevents thrash. Cloud Files exposes the exact state needed and permits dehydration only for in-sync placeholders; free-space delta reports bytes truly reclaimed. Staging keeps the expensive per-file query proportional to the hydrated working set instead of the whole library, while stage 2 preserves correctness — a partially hydrated file has `RECALL` **and** `OnDiskDataSize > 0`, so the attribute alone cannot rule it out |
| D27 | Bridge privilege boundary | SMB exports only `C:\ProgramData\icloud-bridge\io`; scheduled code is `...\agent.ps1` and private state is `...\state`, both outside the share. `syncshare` gets Modify only on `io`; `icloud` gets Modify on `io` and `state` and read/execute on the script | The host must be able to write requests/config, but SMB credentials must not grant code execution by allowing replacement of the scheduled agent script or its trusted state |
| D28 | Agent ACL authority | The task stays `RunLevel Limited`. Elevated provisioning grants the `icloud` **SID** an inheritable allow ACE of exactly `RC,WDAC` on the sync root (`(OI)(CI)(RC,WDAC)`), repeated explicitly on any protected child DACL that does not inherit. The agent preflights `READ_CONTROL\|WRITE_DAC` on every new target and parent before changing anything, reporting `acl-write-denied` per exclusion on failure | Editing a DACL needs `WRITE_DAC`. An owner gets it implicitly, but ownership of a new object depends on who created it — cloud-created items are owned by `icloud` while items created through SMB may be owned by `syncshare` — so ownership is not a dependable basis. Two rights are the minimum: no `WO`/`D`/data access is granted, and the ACE names `icloud`, never `syncshare`, so it cannot weaken an exclusion deny |

**Known accepted limitations (do not attempt to fix):**

- A brand-new cloud item whose exact path was `not-found` on the exclusion list is
  visible on the host for up to one enforcement cycle (≤ 60 s) before being
  hidden. While the item does not exist, NTFS has no per-name object on which to
  place a collision-blocking ACE; the GUI therefore warns on `not-found`.
- **Cold reads block while content downloads** (D25). For small files this is
  ~1–4 s (§0.5); for multi-GB files it can be minutes to hours, during which the
  reading process is stuck in the read call. Whether the kernel CIFS client
  tolerates arbitrarily long hydrations is unproven until E0 passes. There is no
  progress indication on the host side.
- Apps on the host see "permission denied", not "no such file", if they try to
  create a name colliding with a hidden item. That is the intended safety behavior.
- A rename of an excluded root may preserve its explicit deny ACE or may arrive
  as a new placeholder, depending on how iCloud materializes it. During each
  ten-minute full-tree scan the agent removes orphan agent-owned denies/guards
  that are no longer covered by the wanted paths. Until then a renamed item can
  remain hidden; after reconciliation the old configured path reports
  `not-found` and the new path is included. This is the accepted path-based
  limitation.

---

## 2. Bridge protocol (exact formats)

Protocol files live in the bridge share root
(`C:\ProgramData\icloud-bridge\io` == `/mnt/icloud_bridge`). Private state and
the executable script are deliberately outside this share (D27). All JSON is
UTF-8, no BOM, with timestamps in UTC ISO 8601 (`...Z`). Every writer uses a
unique temporary file in the same directory, flushes/closes it, then atomically
replaces the target. In Windows PowerShell 5.1, do **not** use
`Set-Content -Encoding UTF8` (it emits a BOM); use `UTF8Encoding($false)` and
`[IO.File]::Replace` (or `[IO.File]::Move` for first creation). The GUI uses
`os.replace`. Paths are relative to the sync root and use forward slashes
(`Docs/Big Folder`). The agent hand-serializes its JSON rather than using
`ConvertTo-Json`: 5.1's `ConvertTo-Json` does not reliably render empty arrays
as `[]`, and `tree.json` is full of empty `dirs` arrays. (A consumer that does
use `ConvertTo-Json` must pass `-Depth 20`.)

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
- Provisioning creates revision 0 with an empty list if the file does not yet
  exist. After that, missing, malformed, wrong-version, non-integer-revision, or
  path-invalid config is an error: retain all current denies/guards, do not change
  `appliedRevision`, and set `lastError` (fail closed). An intentional "include
  everything" operation is a valid new revision with `exclusions: []`.
- Persist the canonical-list hash with the last accepted revision in private
  state. A lower revision, or the same revision with different content, is an
  error and fails closed; the GUI's conflict check and monotonic increment are
  the normal recovery path.
- Validate and canonicalize the **whole** config before changing any ACL. Invalid
  entries do not get silently dropped. Reject rooted/UNC/drive/ADS paths, NULs,
  `.` or `..` path segments, the empty/root path, and anything whose
  `[IO.Path]::GetFullPath()` is not beneath the canonical sync-root path with an
  ordinal-ignore-case boundary check. Use literal-path APIs, never wildcard
  expansion. De-duplicate case-insensitively and remove descendants of every
  excluded path (D19). The list-request validator alone may allow the empty root
  path.
- Do not follow an arbitrary junction/symlink out of the tree. When an
  intermediate directory has `REPARSE_POINT`, require
  `CfGetPlaceholderInfo` to identify it as a Cloud Files placeholder with the
  same `SyncRootFileId`; otherwise reject/skip it and report an error. Apply the
  same rule to recursive walks.
- Bound untrusted bridge inputs: `exclusions.json` ≤1 MiB and ≤10,000 entries;
  each request ≤64 KiB. Reject over-limit input fail-closed rather than letting a
  writable share exhaust the agent.

### 2.2 `status.json` — written by agent every 15 s

```json
{
  "version": 1,
  "generatedAt": "2026-07-22T01:15:00Z",
  "agentStartedAt": "2026-07-21T23:00:01Z",
  "syncRoot": "C:/Users/icloud/iCloudDrive",
  "icloudClientRunning": true,
  "diskFreeBytes": 51200000000,
  "diskTotalBytes": 128000000000,
  "appliedRevision": 7,
  "lastEnforcementAt": "2026-07-22T01:14:30Z",
  "lastError": null,
  "exclusions": [
    {"path": "Big Folder", "state": "applied", "detail": "", "logicalBytes": 21474836480, "localAllocatedBytes": 0},
    {"path": "Docs/huge-video.mp4", "state": "pending-dehydrate", "detail": "online-only requested; content is still allocated locally", "logicalBytes": 4294967296, "localAllocatedBytes": 1073741824}
  ],
  "fullyLocalLogicalBytes": 3221225472,
  "scan": {"lastCompletedAt": "2026-07-22T01:10:00Z", "durationMs": 1840, "entries": 103421, "cloudInfoQueries": 37},
  "sweep": {"lastRunAt": "2026-07-22T01:14:30Z", "requestedBytes": 0, "freedBytes": 0, "blockedBytes": 0, "blockedCount": 0, "inProgress": false, "belowFloor": false}
}
```

- `exclusions[].state` ∈ `applying` | `applied` | `pending-dehydrate` | `not-found` |
  `error`. `applied` means: exact target deny and parent guard are present and
  every non-empty regular file reports `OnDiskDataSize == 0`.
  `not-found` means the path does not (yet) exist under the sync root — the agent
  keeps checking each cycle (the item may arrive from the cloud later) and hides it
  the moment it appears. Because a missing object cannot be protected by a named
  NTFS ACE, `not-found` is yellow, not healthy. An `error` whose `detail` begins
  `acl-write-denied:` means the D28 preflight failed for that path — provisioning
  step 4 has not been applied, or that object carries a protected DACL; the item
  is left completely untouched.
- `icloudClientRunning`: true iff a process named `iCloudServices` or `iCloudDrive`
  exists. This is process liveness only; it does not prove Apple-side sync health.
- `lastError` contains the most recent currently unresolved agent/config error and
  clears only after the failing sub-task completes successfully; do not clear it
  merely because a status write succeeded.
- `fullyLocalLogicalBytes`: summed `Length` of regular files **lacking**
  `RECALL_ON_DATA_ACCESS` across the sync root — i.e. content known to be fully
  local. It comes from the cheap attribute enumeration and deliberately
  **excludes** the local part of partially hydrated placeholders, so it is a
  lower bound, not an allocation total. Do not open files to compute it.
  `diskFreeBytes` remains the authoritative whole-volume capacity signal; the GUI
  must never present this field as "space used by iCloud".
- `exclusions[].localAllocatedBytes` is different and **is** exact: it comes from
  `CfGetPlaceholderInfo.OnDiskDataSize` (plus `GetCompressedFileSizeW` for
  non-placeholders), because `applied` requires proving `OnDiskDataSize == 0`
  and `RECALL` alone cannot prove it.
- `scan` describes the last completed routine tree scan. `cloudInfoQueries`
  counts per-file `CfGetPlaceholderInfo` calls made during it and exists to make
  a regression visible: it must stay proportional to exclusions being applied and
  reparse-point checks, **not** to the number of ordinary dataless files (D26).
  Per §3, the only places that open files are pending exclusions, active
  reclamation, and reparse containment checks.
- `sweep.requestedBytes` is allocation for which `+U -P` was requested in the
  latest pass; `freedBytes` is the increase in volume free bytes since that
  reclamation episode began. `blockedBytes`/`blockedCount` report local content
  skipped because it is pinned, modified, not in sync, open, or not yet a cloud
  placeholder. `inProgress` remains true while free space is below
  30 GB and eligible allocation or outstanding requests remain. `belowFloor` is
  simply the latest measured `diskFreeBytes < 20 GB`; both states drive D23
  yellow rather than pretending an asynchronous request already freed space.

### 2.3 `tree.json` — written by agent every 10 min and immediately after each enforcement pass that changed anything

Folders only (files are fetched on demand via §2.4 — a full-file tree could be
100k+ entries).

```json
{
  "version": 1,
  "generatedAt": "2026-07-22T01:10:00Z",
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
- For an effectively excluded folder, `dirs` is truncated to `[]` in JSON, but
  the ten-minute post-order scan still visits its entries to compute current
  logical totals. A root folder's `LastWriteTime` is not a reliable cache key for
  changes deeper in the subtree.

### 2.4 Per-folder file listing (request/response)

- GUI writes `requests/list-<random32hex>.json`:
  `{"path":"Docs","offset":0,"limit":1000}` (empty path = sync root). Limit
  must be 1–1000. Files are sorted by ordinal-ignore-case name plus original name
  as a deterministic tie-breaker before paging.
- Agent (polling `requests/` every 2 s) writes
  `responses/list-<same-id>.json`:

```json
{
  "path": "Docs",
  "error": null,
  "offset": 0,
  "nextOffset": null,
  "files": [
    {"name": "notes.txt", "path": "Docs/notes.txt", "logicalBytes": 1024, "excluded": false, "dataless": false}
  ]
}
```

then deletes the request file. GUI deletes the response after reading. Agent
garbage-collects any request/response older than 10 minutes. On error (path missing
etc.) `error` is a message string and `files` is `[]`. The request path goes
through the same canonical containment validator as exclusions; malformed input
must not let the SMB account enumerate outside the sync root. `dataless` means
the empirical iCloud signal `RECALL_ON_DATA_ACCESS` is set; allocation/completion
logic uses `CfGetPlaceholderInfo` instead. Process only filenames matching
`^list-[0-9a-f]{32}\.json$`; ignore temp/unknown names.
`nextOffset` is the next integer offset when more files remain, otherwise null.

---

## 3. Guest agent — `guest-agent/agent.ps1`

One file, PowerShell 5.1 compatible (ships with Windows). Constants at top:

```powershell
$SyncRoot = "$env:USERPROFILE\iCloudDrive"
$BaseDir  = "C:\ProgramData\icloud-bridge"
$BridgeDir = Join-Path $BaseDir "io"
$StateDir  = Join-Path $BaseDir "state"
$ShareUser = "syncshare"
# Cloud Files / FILE_ATTRIBUTE values
$ATTR_PINNED   = 0x00080000   # FILE_ATTRIBUTE_PINNED
$ATTR_UNPINNED = 0x00100000   # FILE_ATTRIBUTE_UNPINNED
$ATTR_RECALL   = 0x00400000   # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (not fully local)
# Disk reclamation (D26)
$SweepFloorBytes  = 20GB      # sweep when free disk drops below this
$SweepTargetBytes = 30GB      # request coldest-first until this target is covered
```

Main loop: `while ($true) { ... ; Start-Sleep -Seconds 2 }` with counters so that
request-handling runs every iteration (2 s), status every ~15 s, enforcement every
~60 s, tree every ~10 min (also right after an enforcement pass that changed
state and once at startup). Wrap each sub-task in `try/catch`; a failure sets
`lastError` in the next `status.json` but never exits the loop. An outermost fatal
catch exits non-zero so Task Scheduler's restart-on-failure setting can act.
Process at most ten list
requests per iteration so a writable bridge cannot starve enforcement/status.

At startup, compile a small C# interop helper with `Add-Type` for
`CfGetPlaceholderInfo`, `CreateFileW`/`CloseHandle`, and
`GetCompressedFileSizeW`. Open files for `FILE_READ_ATTRIBUTES`, sharing
read/write/delete, without reading their data. Return `OnDiskDataSize`,
`ModifiedDataSize`, `InSyncState`, and `PinState`; for a non-placeholder, return
its allocated size from `GetCompressedFileSizeW` and mark cloud state unknown.
This metadata query must not hydrate content. The Microsoft API explicitly says
`CfGetPlaceholderInfo` requires only `READ_ATTRIBUTES` and does not modify the
file. Keep all counters as `[Int64]`.

**Call this helper only in these three cases** — it costs a handle open per file
and must never run once per library file per cycle (D26):

1. Files under an exclusion in `applying` or `pending-dehydrate`, where `applied`
   requires proving `OnDiskDataSize == 0`.
2. Candidates during an active low-disk reclamation episode, staged per §3.1.
3. Reparse-point containment validation (§2.1).

The routine ten-minute tree scan is attribute-only with no per-file handle
opens — implemented with `FindFirstFileW`/`FindNextFileW` interop rather than
`EnumerateFileSystemEntries`: only the find API also returns the reparse tag,
and since every Cloud Files placeholder directory carries `REPARSE_POINT`, the
§2.1 containment check would otherwise cost a handle open per directory. "No
`RECALL_ON_DATA_ACCESS`" is treated as fully local, and the scan reports
`fullyLocalLogicalBytes` and the `scan` counters (§2.2).

On the first successful startup only, run `attrib -P "$SyncRoot\*" /S /D` to
clear v1's always-available intent without requesting eviction; check its exit
code, then write the private migration marker atomically. Do not run `+U`
globally. A repeat start sees the marker and skips this migration (D25).

### 3.1 Enforcement pass (the core — implement exactly this order)

```text
read exclusions.json -> $wanted (list of relative paths), $revision
validate the entire document per §2.1; on any error, change no ACL/state/revision
canonicalize case-insensitively and collapse descendant entries under wanted dirs
$current = private applied.json roots + guardedParents; verify its explicit ACEs

# --- preflight ACL authority before mutating anything (D28) ---
for each existing wanted root and its parent:
    open with READ_CONTROL | WRITE_DAC; on failure mark that root
    state = error, detail = "acl-write-denied: <path>"
preflight ALL of them first; a root that fails preflight is skipped entirely
below, so D22e (all new protections established before any removal) still holds

# --- establish every newly desired protection before removing any old one ---
for each $p that passed preflight:
    $full = the already validated canonical full path for $p
    if not (Test-Path -LiteralPath $full): state = not-found; continue
    $parentFull = canonical full path of its parent (sync root is allowed)
    Add-ExactDenyRule $parentFull $ShareSid DeleteSubdirectoriesAndFiles NoInheritance
    if directory:
        Add-ExactDenyRule $full $ShareSid FullControl ContainerAndObjectInherit
    else:
        Add-ExactDenyRule $full $ShareSid FullControl NoInheritance

# --- remove obsolete protections only after all desired ones exist ---
for each old root not in $wanted:
    if it still exists: Remove-ExactTargetDenyRule $oldFull $ShareSid
for each old guarded parent no longer needed by any existing wanted root:
    if it still exists: Remove-ExactParentGuardRule $oldParentFull $ShareSid

# --- request dehydration after access is denied ---
for each existing wanted root:
    on first protection (and a bounded retry if still pending), run:
        directory: attrib +U -P "$full" /S /D
        file:      attrib +U -P "$full"
    query placeholder state for its regular files; Cloud Files retains local
        content for modified/not-in-sync/open files even though unpinned intent
        was requested, so report those as pending-dehydrate
    re-query on later passes; state=applied only when target deny + parent guard
        are present and every non-empty file has OnDiskDataSize == 0

atomically persist {roots, guardedParents, appliedRevision, wantedHash} in private applied.json

# --- disk-reclamation sweep of included areas (D26) ---
read free bytes from the volume containing $SyncRoot
if free < $SweepFloorBytes or an earlier reclamation episode is in progress:
    $deficit = max(0, $SweepTargetBytes - current free)

    # stage 1: fully local files (no RECALL) -- the hydrated working set
    walk included files, pruning at wanted roots, from attributes only
    stage-1 set = files WITHOUT $ATTR_RECALL
    sort by LastAccessTime if NTFS updates it; else LastWriteTime (approximate)
    for each, in order: query cloud state/allocation (this is where the handle
        opens are spent); candidates are in-sync, ModifiedDataSize==0,
        OnDiskDataSize>0, and not pinned after the one-time migration
        run attrib +U -P one file at a time, accumulating requested OnDiskDataSize
        stop as soon as the accumulated total >= $deficit

    # stage 2: only if stage 1 could not cover the deficit
    if accumulated < $deficit and stage-1 set is exhausted:
        examine files WITH $ATTR_RECALL in bounded, cursor-based batches
            (persist the cursor in private state; resume next pass -- never
             scan the whole RECALL population in one 60 s cycle)
        a partially hydrated placeholder has RECALL and OnDiskDataSize > 0;
            apply the same eligibility rules and continue accumulating
        do not report "nothing eligible" until stage 2 has exhausted its cursor

    do not busy-wait for allocation/free-space changes; persist the episode and
        re-query on the next 60 s pass because dehydration is asynchronous
    end the episode only at free >= target, or when a full pass has exhausted
        BOTH stages with no eligible or outstanding local allocation; report the
        §2.2 sweep fields
```

Notes for the executor:

- Use `FindFirstFileW`/`FindNextFileW` interop for the walk (attribute bits,
  sizes and the reparse tag in one enumeration, no per-entry handle opens; see
  §3's tree-scan note for why `EnumerateFileSystemEntries` was dropped). Do
  **not** shell out to `attrib` for *reading* state — only for setting it.
  Prune non-Cloud-Files reparse directories per §2.1 instead of traversing
  them.
- The routine ten-minute scan must complete well inside its own interval. It is
  attributes-only by construction (§3 helper rules); if `scan.durationMs`
  approaches 600,000 or `scan.cloudInfoQueries` tracks the library size rather
  than the pending/reclaiming set, something has regressed to per-file opens.
- Resolve `syncshare` to a SID once. Implement the exact target and parent-guard
  rules with `System.Security.AccessControl` and
  `RemoveAccessRuleSpecific`, matching SID, access mask, inheritance, propagation,
  and deny type. Do not use `icacls /remove:d`: it removes every deny for the SID
  and can accidentally remove a target deny when that directory was also an old
  guarded parent during a parent/child transition.
- Logical size is `(Get-Item -LiteralPath $f).Length`; this is metadata and was
  empirically shown not to hydrate placeholders. `UNPINNED` is not a dataless
  test: the §0.5 iCloud placeholders had `RECALL` but not `UNPINNED`. Use
  `CfGetPlaceholderInfo.OnDiskDataSize == 0` for completion and `RECALL` only as
  a cheap "not fully local" indicator.
- Read `NtfsDisableLastAccessUpdate` (or `fsutil behavior query disablelastaccess`)
  to choose the age key; do not infer the setting by comparing timestamps. NTFS
  may defer enabled last-access updates for up to one hour, which is acceptable
  for this approximate LRU policy.
- Never assume a local file means "pending upload." It can be open, dirty,
  partially hydrated, provider-delayed, or an ordinary file not yet converted to
  a placeholder. Report the observed condition in `pending-dehydrate.detail`.
- Use ordinal-ignore-case comparisons, but preserve display casing from the
  filesystem. Check every native process exit code; a nonzero `attrib` result or
  ACL exception produces `error` and must not advance that root to `applied`.
- Run one full-tree ACL reconciliation at startup, then during each ten-minute
  full-tree pass. Inspect explicit deny ACEs for the dedicated `syncshare` SID;
  remove orphan target denies and parent guards not represented by/covered by the
  validated wanted set, then update private state. Reconciliation may remove
  nothing unless the current config has passed full validation (D22f). This
  repairs preserved ACLs after cloud renames or a lost/corrupt private state file.

### 3.2 Files the agent maintains

Shared `io`: `status.json`, `tree.json`, `responses/*`. Private `state`:
`applied.json` and the D25 migration marker. `exclusions.json` and `requests/*`
are host inputs. The script changes attributes and DACLs but never writes file
content or calls delete/move/remove inside `$SyncRoot` (D22a). Deletes are allowed
only for consumed/expired bridge request and response files.

---

## 4. Guest provisioning — `provision/04-bridge-agent.ps1`

Run **as Administrator** in the guest after v1 scripts 01–03. Idempotent (safe to
re-run, like 01/03). Because the script runs elevated, it must not derive the
sync root from the administrator process's profile. Set exact constants first:

```powershell
$SyncRoot = "C:\Users\icloud\iCloudDrive"
$BaseDir = "C:\ProgramData\icloud-bridge"
$IoDir = Join-Path $BaseDir "io"
$StateDir = Join-Path $BaseDir "state"
$AgentUser = "icloud"
$ShareUser = "syncshare"
```

Fail before changing anything if the source script, sync root, or either local
account is missing, or if config is absent while prior-install markers exist as
defined in step 5. Then perform these steps in order:

1. If the `icloud-bridge-agent` task exists, stop it before replacing files.
   `New-Item -ItemType Directory -Force` for
   `C:\ProgramData\icloud-bridge\io\requests`, `...\io\responses`, and
   `...\state`.
2. Copy `C:\OEM\agent.ps1` → `C:\ProgramData\icloud-bridge\agent.ps1`
   (docker-compose already mounts `./provision` at `/oem` → `C:\OEM`; add
   `guest-agent/agent.ps1` to that mount by copying it into `provision/` at build
   time — see §8 task A3 — so it lands in `C:\OEM`).
3. Normalize the **data-root** `syncshare` ACL left by v1. The old
   `03-create-share.ps1` used `/T`, which put explicit allows on descendants;
   explicit allows outrank an inherited folder deny and can make a known child
   path readable. Remove only `syncshare` grant ACEs recursively, then add one
   inheritable Modify grant at the sync root:

   ```powershell
   icacls $SyncRoot /remove:g "syncshare" /T /C /Q
   if ($LASTEXITCODE -ne 0) { throw "failed to normalize syncshare ACLs" }
   icacls $SyncRoot /grant "syncshare:(OI)(CI)M" /Q
   if ($LASTEXITCODE -ne 0) { throw "failed to grant sync-root access" }
   ```

   This preserves every other identity's ACE and any existing agent deny ACE.
   Before any recursive `icacls` walk, scan the tree for junctions and symlinks
   and fail listing them: `/T` follows a link out of the sync root and would
   mutate ACLs on unrelated objects with admin rights. Cloud placeholder
   directories also carry `FILE_ATTRIBUTE_REPARSE_POINT`, but PS 5.1's
   `LinkType` resolves only mount points and symlinks — exactly the two tags
   that redirect traversal — so it is the discriminator to use (the same rule
   §2.1 imposes on the agent's own walks). Then check the tree for protected
   child DACLs that do not inherit; step 4 repairs the agent's authority on
   them and fails with the paths listed instead of resetting unrelated ACLs.
   §7 also removes `/T` from script 03 so later recovery runs do not
   reintroduce the problem.
4. Give the limited agent deterministic ACL authority (D28). The agent must edit
   DACLs on excluded items and on their parents including the sync root itself,
   which requires `WRITE_DAC`. An owner has it implicitly, but ownership depends
   on who created each object — cloud-created items are owned by `icloud`, items
   created through SMB may be owned by `syncshare` — so grant it explicitly by
   SID, with no other rights:

   ```powershell
   $AgentSid = (Get-LocalUser -Name $AgentUser).Sid.Value
   icacls $SyncRoot /grant "*${AgentSid}:(OI)(CI)(RC,WDAC)" /Q
   if ($LASTEXITCODE -ne 0) { throw "failed to grant agent ACL-management rights" }
   ```

   `RC` is `READ_CONTROL`, `WDAC` is `WRITE_DAC`; no `WO`, `D`, or data access is
   granted. `(OI)(CI)` without `(IO)` applies the ACE to the sync root itself as
   well as descendants, which is what lets the agent place the parent guard for a
   top-level exclusion. For each protected child DACL found in step 3, add the
   same explicit `RC,WDAC` ACE to that object only, preserving every unrelated
   ACE and leaving its inheritance protection unchanged; if a targeted repair
   fails, abort and list the affected paths. This ACE names `icloud`, never
   `syncshare`, so it cannot weaken an exclusion deny. In particular, do NOT
   re-grant `syncshare` on a protected object: step 3 stripped its explicit
   allows, and adding one back on an object under an excluded root would
   outrank the inherited exclusion deny. After the agent-ACE repairs, the
   script fails listing the protected paths so the operator restores
   inheritance deliberately (`icacls <path> /inheritance:e`) and re-runs.
5. Set bridge NTFS permissions with the narrow D27 boundary: grant `icloud`
   Modify on `io` and `state`, grant `syncshare` Modify on `io` **only**, and
   grant `icloud` read/execute on `agent.ps1`. Do not grant `syncshare` on the
   base, script, or state directory. First remove any legacy `syncshare` grant
   recursively from `$BaseDir`, then grant it back on `$IoDir` only; reruns must
   repair an earlier broad-share prototype. `Modify` deliberately excludes
   `WRITE_DAC`. Check every `icacls` exit code and verify effective access before
   starting the task.
6. On a genuine first install only, create `io\exclusions.json` as
   `{"version":1,"revision":0,"exclusions":[]}` using BOM-less UTF-8 and the
   atomic helper. If config is absent but a prior task, `bridge` share, private
   `applied.json`, migration marker, or agent script indicates an existing
   installation, fail closed and require recovery of/replacement with an
   explicitly chosen config;
   an idempotent rerun must not silently manufacture an empty list and re-include
   everything. Create/repair the `bridge` share at
   `C:\ProgramData\icloud-bridge\io` with `syncshare` share-level Full access.
   If an existing `bridge` share points at the old base path, remove and recreate
   **that share only** (no file deletion), then verify its path and access list.
7. **Enable ABE on the data share:**
   `Set-SmbShare -Name "icloud" -FolderEnumerationMode AccessBased -Force`
8. Register the agent task with an explicit interactive principal (idempotent —
   `-Force`). The task runs only in the auto-logged-on `icloud` session, stores no
   password, and ignores duplicate starts:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\ProgramData\icloud-bridge\agent.ps1"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "icloud"
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\icloud" `
  -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # no time limit
Register-ScheduledTask -TaskName "icloud-bridge-agent" -Action $action `
  -Trigger $trigger -Principal $principal -Settings $settings -Force
Start-ScheduledTask -TaskName "icloud-bridge-agent"
```

9. Verify the task reaches `Running`, the share path is exactly `...\io`, and a
   fresh `status.json` appears; then print a green "bridge ready" message.

---

## 5. Host-side mount for the bridge share

Two new unit files. The mount-path component is `icloud_bridge` (underscore), so
the only hyphen in `mnt-icloud_bridge.mount` is systemd's normal encoding of the
`/mnt/...` slash; no literal-hyphen escaping is needed (D16):

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
│   │                      #   list-request round-trip; worker-thread I/O
│   ├── tray.py            # QSystemTrayIcon: icon state (D23), menu
│   ├── window.py          # QMainWindow with 2 tabs: Status, Selective Sync
│   └── icons/             # icloud-green.svg, icloud-yellow.svg, icloud-red.svg
│                          #   (simple cloud glyph, solid fill in the state color)
├── icloud-bridge-gui.desktop        # launches the app (window)
├── autostart/icloud-bridge-tray.desktop  # launches the app minimized to tray
└── install-gui.sh
```

### 6.2 Behavior — exact spec

**Health model (`health.py`)** — every check returns
`(severity: green|yellow|red, detail: str)`:

- container: `docker inspect -f '{{.State.Running}}' icloud-windows` == `true`
  (subprocess, 5 s timeout).
- mount: `os.path.ismount('/mnt/icloud')`; same for `/mnt/icloud_bridge`.
- canary: `/mnt/icloud/.linux-canary` exists and mtime age < 900 s (this reuses
  the v1 ten-minute health timer's canary with five minutes of scheduling slack;
  the GUI does not write the canary itself). More than five minutes in the future
  is a red guest/host clock error.
- bridge agent: `status.json` exists, `generatedAt` is valid aware UTC, age <90 s,
  and it is no more than 60 s in the future; timestamp errors are yellow.
- tree: `tree.json.generatedAt` valid and <20 min old; missing/stale/invalid is
  yellow because health data works but selective browsing does not.
- iCloud client: `status.json.icloudClientRunning`.
- exclusions: `applied` ⇒ green; `applying`, `pending-dehydrate`, or `not-found`
  ⇒ yellow; `error`, non-null `lastError`, or `appliedRevision` behind the GUI's
  last written revision for > 5 min ⇒ yellow.
- disk: `sweep.belowFloor` or `sweep.inProgress` ⇒ yellow. Detail distinguishes
  "reclamation in progress" from "nothing eligible; grow the disk or wait for
  uploads/files to close" instead of claiming every below-floor state is final.

Overall state per D23. Refresh every 5 s, with at most one refresh in flight.
Run **all** subprocess and filesystem operations—including `ismount`, CIFS
`stat`/JSON reads, and request/response polling—in a `QThreadPool` worker (or
`QProcess` for commands), because a sick CIFS mount can block metadata calls.
Return results to the GUI thread by signals; never touch widgets from a worker.

**Tray (`tray.py`):**

- Icon = colored SVG per overall state; tooltip = one line per failing check, or
  "iCloud bridge: healthy".
- Left-click and menu item "Open status window" → show/raise the window.
- Menu: **Open iCloud folder** (`xdg-open /mnt/icloud`), **Open status window**,
  **Open VM screen** (`xdg-open http://127.0.0.1:8006`), separator, **Quit**.
- Closing the window hides it when a tray is available; Quit exits. If
  `QSystemTrayIcon.isSystemTrayAvailable()` is false, a normal launch keeps the
  window visible and close exits so the process cannot become unreachable. An
  autostart `--minimized` launch exits with a diagnostic in that case; the GNOME
  install note tells the operator how to enable tray support.

**Status tab:** one row per check above — colored dot, name, detail, plus guest
disk free/total from `status.json` and `fullyLocalLogicalBytes` labelled
**"Fully local content"** with the note *"partially downloaded files are not
counted"*, alongside `scan.lastCompletedAt`. Never label it as total space used
by iCloud — it is a lower bound (§2.2). Buttons: the same three actions as the
tray menu.

**Selective Sync tab:**

- Load the current wanted set/revision from `exclusions.json`; use
  `status.json.exclusions` only for enforcement state/details. If config is
  missing or malformed, disable Apply and show the fail-closed error instead of
  presenting an empty selection.
- `QTreeWidget`, columns: *Name*, *Size* (human-readable from `logicalBytes`),
  *Items* (`fileCount`), *Included*, *State*. Checked = included/visible,
  unchecked = excluded, and partially checked = this folder is included but has
  excluded descendants. Compute partial state from the complete exclusions list,
  not only currently loaded children. "Included" does not mean "downloaded".
- Populated from `tree.json` (folders). Expanding a folder fires a §2.4 list
  request; when the response arrives (poll `responses/` on a 1 s `QTimer`, 15 s
  timeout → show "guest agent not responding"), files are inserted as child rows
  with the same columns. If `nextOffset` is non-null, add a **Load more…** row;
  do not materialize an unbounded single-folder listing at once.
- Excluded rows: greyed text + unchecked box. An item inside an excluded folder is
  not shown with its own checkbox (whole subtree is excluded; `tree.json` doesn't
  recurse there).
- Configured exclusions absent from `tree.json` appear in a separate **Missing
  configured items** group with state `not-found` and a **Remove exclusion**
  action. This is not free-text entry: it only removes an already configured
  path, and is how the user clears a stale path after a rename/delete.
- Unchecking a folder queues that folder and removes queued/configured descendant
  exclusions (D19 antichain). Checking an excluded folder removes that root and
  includes its subtree. Checking a partially checked folder removes every
  descendant exclusion after a confirmation. Nothing is written until the user
  clicks **Apply**. Apply shows one confirmation dialog listing the changes with
  exact wording:
  - for new exclusions: *"These items will disappear from /mnt/icloud on this
    computer. Windows will free their local content after iCloud reports it safe
    to dehydrate; this may not be immediate. They remain in iCloud and on your
    other devices. Nothing will be deleted."*
  - for re-includes: *"These items will reappear in /mnt/icloud. Their content
    is normally online-only after exclusion and downloads when opened; any
    content still cached remains local and uses VM disk space."*
- On confirm: canonicalize to the D19 antichain and write `exclusions.json`
  atomically. Re-read the file immediately before replacement; if its revision
  changed since the UI loaded it, cancel and reload instead of overwriting an
  external edit. Set revision to one greater than the maximum valid revision seen in
  `exclusions.json`, `status.appliedRevision`, and the GUI's last write; never
  reset it to 1 after a restart. The *State*
  column then tracks `status.json` (`applying` → `applied`, etc.).
- The root row cannot be unchecked (D22b). There is no free-text path input
  (D22c).

**Single instance:** the primary process binds abstract Unix socket
`\0icloud-bridge-gui` and integrates accept with `QSocketNotifier`. A second
normal launch sends `show\n` then exits; the primary shows/raises/activates its
window. A second `--minimized` launch just exits. This makes the desktop launcher
useful when the tray instance already exists.

### 6.3 `install-gui.sh` (run as the desktop user, not root)

1. Copy `icloud_bridge_gui/` to `~/.local/share/icloud-bridge-gui/` and install a
   launcher at `~/.local/bin/icloud-bridge-gui`.
2. If `python3 -c 'import PySide6'` fails, first try the Ubuntu packages
   `python3-pyside6.qtwidgets`, `python3-pyside6.qtgui`, and
   `python3-pyside6.qtcore` (plus `python3-pyside6.qtsvg` for SVG icon support).
   These packages exist on the target Ubuntu 26.04 host. If they are unavailable
   on another supported distro, install `python3-venv`,
   create `~/.local/share/icloud-bridge-gui/venv`, and install PySide6 **inside
   that venv**. Do not use `pip install --user` or `--break-system-packages`.
   The launcher records the selected interpreter, changes to the app directory,
   and execs `python -m icloud_bridge_gui "$@"`.
3. Install `icloud-bridge-gui.desktop` to `~/.local/share/applications/` and
   `autostart/icloud-bridge-tray.desktop` to `~/.config/autostart/`. Both use
   the launcher's absolute, install-time-expanded path; the autostart entry adds
   `--minimized`. `.desktop` files do not expand `~` or shell variables.
4. Print a note: *"GNOME users: install the 'AppIndicator and KStatusNotifierItem
   Support' extension for the tray icon to be visible."*

---

## 7. Amendments to v1 documents (make these exact edits)

Apply executable-file and embedded-plan edits together: the AGENTS.md sync rule
still applies. This v2 document is not permission to let
`docs/implementation-plan.md` drift from files it embeds.

1. `docs/implementation-plan.md` decisions/§1/§6/§10–§12:
   - Bring §2's stale layout up to the actual repository plus the v2 paths, and
     add/update the corresponding verbatim sections for every changed/new
     provision script, host unit/script, and health check. This is mandatory
     under the repository sync rule; this v2 plan is design input, not a waiver.
   - Mark D5 disproven by the 2026-07-22/23 live test and superseded by this
     plan's D14/D25/D26. Leave Files On-Demand on, delete the global `+P -U`
     instruction, and say that the first agent run clears any legacy `P` intent
     with `-P` without evicting content.
   - Replace the whole-library sizing formula with: size for Windows plus the
     expected cached working set; 120 GB is the selected starting size for the
     measured 101 GB library, not a promise for every library. D26 begins
     reclaiming below 20 GB and aims for 30 GB, but dirty/open/provider-delayed
     files can prevent the target, so growth remains in the runbook.
   - Replace the pinned-file acceptance check with E0's kernel-CIFS read/write
     gate and an online-only-placeholder read check. Update the full-disk and
     performance limitations for blocking cold reads and asynchronous reclaim.
2. `provision/install.bat`, `README.md`, and `SETUP.md`: replace every instruction
   to disable Files On-Demand or run `attrib +P -U`; keep Files On-Demand on and
   point to E0. In README, also add the bridge-agent/GUI quickstart and GNOME
   note, and document exclusion read/write/delete/rename/collision behavior.
   Update the **existing single** `## Status` section for v2 and do not add a
   second one — the duplicate listed in `AGENTS.md` was already collapsed in the
   working tree (verified 2026-07-23), so there is nothing left to merge.
3. `provision/03-create-share.ps1`: change the sync-root grant from recursive
   `/T` to the single inheritable root grant required by D15. Update its verbatim
   copy in `docs/implementation-plan.md`. Script 04 performs the one-time cleanup
   of old explicit descendant grants (§4).
4. `host/acceptance-tests.sh`: replace its operator-only pinned `P` check with
   the E0/online-only checks; add bridge JSON checks and the duplicate-agent-file
   guard from §8. Preserve its deliberate `set -u` behavior.
5. `tools/icloud-status.ps1` and `tools/watch-sync.sh`: remove claims that
   placeholders must be zero or that it is safe/required to pin after sync.
   Report `RECALL` placeholders as normal Files On-Demand state. Keep the D5 test
   scripts as historical evidence, updating only misleading present-tense usage
   text if needed.
6. `docs/automation-notes.md`: update the workflow and examples from
   Files On-Demand off/global pinning to E0 followed by Files On-Demand on and
   the v2 agent. Preserve any empirical notes that are still true.
7. `AGENTS.md` "Known inconsistencies": delete **only** the `README.md` two-
   `## Status`-sections bullet, which is resolved. Keep the second bullet — the
   plan's §2 layout still predates `provision/install.bat`, `host/setup-*.sh` and
   `host/acceptance-tests.sh` (verified 2026-07-23) and is fixed by item 1 above;
   remove it in the same commit that fixes it, not before. Do not delete the
   section itself while any bullet remains.

---

## 8. Task checklist (execute in order)

### Phase 0 — live architecture gate (existing v1 system; no v2 code required)

- [ ] **E0** **Gate for D25/D6; do this before phase A.** Run
  `sudo ./host/setup-host.sh` and use the real kernel CIFS mount at
  `/mnt/icloud`. For each read candidate, first verify in the guest that it has
  `RECALL_ON_DATA_ACCESS`; precompute its SHA-256 on another trusted Apple device
  or from a separately downloaded copy so the mount read itself is the first
  hydration.
  1. Run `time timeout 30m sha256sum ...` on a dataless file ≥100 MB. It must
     finish without EIO/hang/timeout and match the trusted hash. Record size and
     elapsed time.
  2. Repeat with a multi-GB dataless file, using a documented, deliberately
     generous timeout. It must complete and hash correctly; record sustained
     rate and whether the blocking UX is tolerable.
  3. For upload, create a uniquely named disposable test file on the host mount,
     wait for it to appear on iCloud web/another Apple device and verify its
     hash, then edit that same test file on the host and verify the new hash.
     Allow up to five minutes per upload before failing; do not modify an
     unrelated existing user file. After both hashes are confirmed, delete the
     disposable file from the host and verify that deletion propagates before
     calling cleanup complete.

  If either read fails at kernel CIFS, stop: D25 is not accepted; investigate
  mount/client timeout behavior or reintroduce scoped pinning before implementing
  v2. If upload/edit/delete fails, stop: the bidirectional D6 architecture
  itself is not accepted. `TimeoutSec=30` in the mount unit covers mount
  establishment, not a blanket 30-second limit for later reads; E0 measures
  actual I/O behavior.

### Phase A — guest side

- [ ] **A1** Write `guest-agent/agent.ps1` per §3 (constants, loop cadence,
  enforcement order, bridge files per §2). PowerShell 5.1 syntax only.
- [ ] **A2** Write `provision/04-bridge-agent.ps1` per §4.
- [ ] **A3** Ensure `agent.ps1` reaches `C:\OEM`: commit a copy at
  `provision/agent.ps1`. Put the header comment "`copied by
  04-bridge-agent.ps1; source of truth: guest-agent/agent.ps1`" in **both** files
  so the committed copy remains byte-identical, and add a CI-less guard in
  `host/acceptance-tests.sh` that diffs the two files and fails if they diverge.
- [ ] **A4** Done-when: running 04 in a guest yields — `Get-SmbShare bridge`
  points exactly to `...\io`; `Get-SmbShare icloud | Select
  FolderEnumerationMode` = `AccessBased`; `Get-ScheduledTask
  icloud-bridge-agent` state `Running`; `io\status.json` is fresher than 30 s and
  `io\tree.json` exists;
  `syncshare` has no Modify/Write/WRITE_DAC access on `agent.ps1` or `state`; and
  rerunning 04 preserves exclusions/config while restoring the same state. Also
  verify one known dataless file remains `RECALL_ON_DATA_ACCESS` before/after an
  allocation/tree scan, proving the interop metadata path itself does not hydrate.
  Confirm the D28 grant landed: `icacls "%USERPROFILE%\iCloudDrive"` shows
  `icloud:(OI)(CI)(RC,WDAC)` and nothing more for that SID, and the same rights
  are effective on a newly cloud-created descendant.
- [ ] **A5** Scan-cost done-when: with no exclusion pending and no reclamation
  active, one routine ten-minute scan over the real library reports
  `scan.cloudInfoQueries` in the low tens (bounded by reparse checks), **not**
  proportional to file count, and `scan.durationMs` far below the 600,000 ms
  interval. A scan that opens a handle per dataless file is a failed A5.

### Phase B — host mounts + scripts

- [ ] **B1** Add `host/mnt-icloud_bridge.mount` and `.automount` per §5.
- [ ] **B2** Extend `host/setup-host.sh` to install/enable them (mirror the
  existing mount install code path exactly).
- [ ] **B3** Extend `host/acceptance-tests.sh`: bridge mounted; `status.json`
  age < 90 s; `status.json`, `tree.json`, and `exclusions.json` parse
  (`python3 -m json.tool`); plus the A3 diff guard.
- [ ] **B4** Done-when: `ls /mnt/icloud_bridge/status.json` works on a live
  system and the extended acceptance script passes.

### Phase C — GUI

- [ ] **C1** Scaffold `gui/icloud_bridge_gui/` per §6.1 with the three SVG icons.
- [ ] **C2** Implement `health.py` (§6.2 health model) + unit-testable pure
  functions for state mapping (D23). Add `gui/tests/test_health.py` covering the
  red/yellow/green precedence, 15-minute canary boundary, stale/invalid UTC
  timestamps, and sweep states with fabricated inputs (no docker/mount needed —
  inject check results).
- [ ] **C3** Implement `bridge.py`: `read_exclusions()`, atomic
  `write_exclusions(paths, revision)`, `read_status()`, `read_tree()`,
  `request_listing(path) -> request_id` /
  `poll_response(request_id)`. Add `gui/tests/test_bridge.py` using a tmpdir as a
  fake bridge share (round-trip exclusions write, request/response file dance,
  revision recovery, atomic replacement, and malformed-JSON tolerance).
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

- [ ] **D1** Apply every §7 edit, including the embedded copies in
  `docs/implementation-plan.md` and the README status cleanup.
- [ ] **D2** Add a short `docs/selective-sync.md` user page: what exclusion
  does/doesn't do, `not-found`, asynchronous reclamation, the rename limitation,
  permission-denied read/write/delete/rename/collision behavior, and how to
  re-include. Include E0 and E1–E7 as the deployment checklist.

### Pre-deployment repository verification

- [ ] Run `bash -n host/*.sh` and `docker compose config` exactly as required by
  AGENTS.md. Run `pytest gui/tests` in the GUI development environment.
- [ ] Confirm every published compose port still begins with `127.0.0.1:`,
  `.env` remains ignored, placeholder passwords remain unchanged, all edited
  scripts are LF, and `guest-agent/agent.ps1` is byte-identical to
  `provision/agent.ps1`.
- [ ] Do not claim PowerShell lint/syntax validation unless `pwsh` was actually
  installed and run. The required PowerShell 5.1 and interop validation occurs
  in A4/E1–E5b on the Windows guest.

### Phase E — v2 live acceptance tests (require the real VM)

- [ ] **E1** Create a disposable cloud test folder with several files, confirm it
  exists on another Apple device, hydrate it, then exclude it in the GUI. Within
  two minutes it disappears from `ls /mnt/icloud`; a known direct path cannot be
  read; guest ACL inspection shows the target deny and parent `DELETE_CHILD`
  guard; status progresses `applying` → `pending-dehydrate` → `applied`; and
  volume free space eventually grows. `U` alone is not accepted as proof.
- [ ] **E1b** Ownership independence (D28). Repeat E1's exclude → re-include
  cycle for each of: a cloud-created item (normally owned by `icloud`); an item
  created on the host through the SMB mount (potentially owned by `syncshare`);
  and at least one **top-level** item, which forces the agent to write the parent
  guard onto the sync root itself. All three must reach `applied` and re-include
  cleanly. Any `acl-write-denied` here means provisioning step 4 did not take.
- [ ] **E2** Against that disposable exclusion, verify all of these fail with
  permission denied: read/open by known path, overwrite, `rm`, rename/move,
  `mkdir`/`touch` at the same name, and same-name operations with different
  letter case. Then create and delete/rename an **included sibling** in the same
  parent successfully; this proves the parent guard did not break ordinary
  sibling operations. Do not use valuable user data for destructive probes.
- [ ] **E3** Re-include → within 2 min it reappears (as online-only
  placeholders); reading a file from it hydrates and is byte-identical; item was
  never absent from iCloud web during the whole cycle.
- [ ] **E4** Modify a disposable file on the host, then immediately exclude its
  parent. State remains `pending-dehydrate` while Cloud Files reports modified or
  not-in-sync content; the item is already denied on the host; only after the
  edited hash is present on another Apple device may it become `applied`. The
  cloud copy must contain the edit. If it does not, stop—the D20 safety premise
  failed.
- [ ] **E5** Create a file in an *included* folder from an iPhone → listed on
  the host ≤ 2 min and readable on demand (the read hydrates it). Create an
  item at an excluded path from an iPhone → hidden on the host ≤ 2 min and
  never hydrates.
- [ ] **E5b** Sweep: hydrate disposable/replaceable files until guest free disk
  < 20 GB (or use test-only thresholds) → `sweep.inProgress` becomes true and
  `requestedBytes` grows. Allow multiple enforcement cycles for asynchronous
  dehydration; free disk must eventually reach ≥30 GB when enough in-sync
  allocation exists, `freedBytes` must reflect the actual free-space increase,
  and an evicted file must re-read with the same hash. Separately test an
  open/not-in-sync file is skipped without data loss. Then test **stage 2**
  explicitly: create a deliberately *partially* hydrated placeholder (read only
  the first bytes of a large dataless file so it carries `RECALL` **and**
  `OnDiskDataSize > 0`), leave no stage-1 candidates, and confirm the sweep
  discovers and reclaims it instead of reporting "nothing eligible".
- [ ] **E6** `docker stop icloud-windows` → tray red ≤ 15 s; start again → green.
  Stop the guest scheduled task → tray yellow ≤ 2 min.
- [ ] **E7** Reboot host and guest → agent auto-starts, exclusions still enforced
  (hidden + protected + dataless), no manual action. Launching the desktop entry
  while the tray is already running raises the existing window rather than
  silently exiting.

---

## 9. Out of scope for v2 (explicitly)

- Pattern/glob exclusions (e.g. `*.mp4`) — path-list only.
- Following renames of excluded items.
- A host-side FUSE filter layer.
- Pause/resume sync from the GUI.
- Per-item pinning ("always keep offline") or pre-warming; smarter cache
  policies than D26's coldest-first sweep. `attrib +P` machinery exists if a
  later version wants it.
- Host-side progress indication for in-flight hydrations.
- Any Photos support (unchanged from v1).
