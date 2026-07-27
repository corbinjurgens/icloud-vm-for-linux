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
| D29 | GUI-managed bridge lifecycle | An explicit GUI **Quit** confirms and then records a durable desired-off state (`/var/lib/icloud-bridge/powered-off`), quiesces health and CIFS activity, unmounts both shares, and gracefully stops `icloud-windows` (`docker stop --timeout 130`); starting a new GUI process automatically restores an existing stopped bridge before doing any CIFS I/O. All of this runs through one root helper, `host/icloud-bridge-power on\|off`, serialized by `flock`. The six mount/automount/health units gain `ConditionPathExists=!/var/lib/icloud-bridge/powered-off` so the marker survives a reboot without disabling the units. Window-close only hides when a tray exists; without a tray, close routes through the same confirmation. The confirmation retains **Quit GUI only** for maintenance. A checkable tray item **Start when the computer starts** toggles the XDG autostart entry (`Hidden=`), making start-at-login a user setting rather than an installer constant. `restart: unless-stopped` gives Docker the matching semantics, so no compose change is needed. | Makes the GUI the normal on/off boundary without confusing window management with shutdown. The marker keeps automounts and health checks off across reboot, while ordered teardown (health -> automount -> mount, then container) avoids stale CIFS mounts and refuses to interrupt open files. Power-on retries a real CIFS activation because a published-port TCP connect is not SMB readiness. Only the explicit confirmed action powers off — logout, signals, crashes, and `aboutToQuit` do not. Amends the v1/v2 always-on assumption. **Amended by D30**, which adds an equally explicit in-session power off/on that does not exit the app; the "only a user action powers off" rule is unchanged |
| D30 | In-session bridge power control (amends D29) | The GUI offers **Power off bridge (keep this app running)** and **Start bridge** as tray items and one Status-tab button, driven by an explicit lifecycle state machine (`power.available_action`), never by a health colour. Power-off runs the *same* transaction as Quit — stop polling, refuse new bridge I/O, drain in-flight mount work and Apply, call `icloud-bridge-power off` — and differs only in its success continuation: idle in-process instead of exiting. The idle state clears the health rows, shows a grey **Bridge is powered off** icon/banner, keeps every mount-touching control disabled, and stops polling until **Start bridge** or process exit. Start reuses the D29 power-on path in full. Quit while already off never calls the helper again. | A temporary stop should not require quitting and relaunching, and a container stopped by hand mid-session had no in-app recovery at all. Sharing one transaction is what keeps the teardown ordering from drifting between the two callers. The state machine exists because **red is not evidence the bridge is off** — it equally means a running VM with a stale canary, a missing mount, or bad JSON — so only a definitive `docker inspect` (`exited`/`created`/`dead`) may enable Start. This is a whole-bridge power operation, **not** pausing iCloud sync, which stays out of scope (§9) |
| D31 | First-run assistant (supersedes D29's "`provision_needed` never runs `docker compose up`") | A dedicated **Setup required** state — reached when no container exists *or* the inspection failed — performs **no** CIFS I/O at all: no `health.gather()`, no `bridge.read_*()`, no `ismount()`, no `xdg-open` of the mount. It shows read-only readiness checks from a Qt-free, mount-I/O-free `firstrun.py` (KVM/tun nodes, native Engine socket reachable by this session, Compose plugin, active-context warning, a complete resource bundle, a syntactically valid env file whose `SHARE_PASS` is not the placeholder, and whether the container is absent/running/stopped). Only with the container **absent** and no failing check does it offer a confirmed **Create Windows VM**, which runs `docker compose -p icloud-bridge -f <bundle>/docker-compose.yml --env-file <chosen> up -d` in a worker. Success enters **Provisioning Windows** — *not* `_begin_startup()` — which keeps I/O paused, offers the VM screen, and presents the manual guest sequence (02, sign-in, 03, 04) plus the matching host command. **Check setup and connect** then verifies the helper, both argument-exact `sudo -n -l` grants, the installed units and host config, and the Docker state, and only then calls the existing privileged `power_on()`. | v2's §6.2 said both "startup precedes all CIFS I/O" and "preserve today's red first-run state", and the old code resolved that the wrong way: `provision_needed` and `inspect_error` called `_enter_monitoring()`, which unpaused I/O and immediately scheduled selective-sync reads against a mount that does not exist. The safe reading is now the only one. Creation is a **confirmed user action** with a fixed project name and an explicitly chosen env file, which is a different thing from the helper silently manufacturing a container — `icloud-bridge-power on` still never does that. Windows' initial install legitimately leaves SMB unavailable for far longer than the helper's five-minute readiness deadline, so the provisioning state waits for the operator instead of failing a power-on. The password never passes through the GUI, **except as provided by D41**: the env file is parsed as text, never sourced, its value never printed, never in argv, never copied into a bundle or the clipboard |
| D32 | SMB wire protection | **Off, deliberately.** `03-create-share.ps1` sets `RequireSecuritySignature $false` and asserts `EncryptData $false` / `RejectUnencryptedAccess $false`; neither mount unit asks for `sign` or `seal` | Since 24H2 a stock Windows 11 Pro requires SMB signing by default, so every hydration byte would be HMAC/GMAC-signed (and, if encryption ever flips on, AES-GCM'd) on both ends of a path that is host loopback → docker-proxy → container NAT → QEMU tap. Anyone positioned on that path already has root on the host, so there is no confidentiality or integrity property to lose — D9 already makes the host the security boundary. Authentication (D8) and the exclusion model (D15: ACLs + ABE) are untouched, and SMB 3.1.1 pre-auth integrity still protects negotiation. The settings are idempotent assertions, so the documented post-feature-update re-run of script 03 (v1 plan §10) is also the correction path for a future Microsoft default-flip |
| D33 | Host I/O path | Pass `/dev/vhost-net` into the container (`host/setup-prereqs.sh` loads the module and persists it; `acceptance-tests.sh` checks the node). Add `rasize=16777216` to the **data** mount only. Everything else on the path keeps its default, and those defaults are recorded as load-bearing in the unit files: `cache=strict`, `actimeo=1`, negotiated `rsize`/`wsize`, no `mfsymlinks`, no `max_channels`, no `sign`/`seal` | dockur enables `vhost=on` only if it can open `/dev/vhost-net`; the default docker device cgroup denies that, so without the passthrough QEMU copies every SMB byte through its userspace main loop. `rasize` decouples readahead from the negotiated I/O size, which is what shortens a cold hydration — one long sequential read that blocks the reader. The recorded defaults are not incidental: `cache=strict` is what forces a placeholder read to reach the guest and trigger CfAPI hydration, `mfsymlinks` would create files iCloud syncs as junk, and multichannel cannot apply to one NATed virtio NIC |
| D34 | Steady-state work elision | The agent may skip **reporting and discovery** work when it is provably redundant, never enforcement work: (a) the per-entry DACL reads of full-tree reconciliation are skipped while the wanted set and private state are empty and the previous pass completed clean, gated by a persisted flag that defaults to false and is cleared *before* any ACL write; (b) the re-verification walk of an already-applied exclusion runs every pass for its first few passes, then every ~10th, and immediately on a config change; (c) a reclamation episode reuses its candidate lists for ~5 passes but may never *end* on a stale list. On the host side the GUI keeps its 5 s cadence for file stats but re-parses `status.json`/`tree.json` only when `(mtime, size)` moves, and runs `docker inspect` — for the health row and the D30 power-control classification alike — at most every 15 s per consumer, with an explicit invalidation on every power transition and on Refresh | These are the three recurring costs that scale with library size rather than with anything the user did, and none of them is what makes the system safe. D15's denies and parent guards are still asserted on every 60 s pass; every dehydration candidate is still re-checked with `CfGetPlaceholderInfo` immediately before its request (D26); the cadences of D17 are unchanged. What degrades is label freshness — an `applied` exclusion's reported size, and the health rows' view of the container — by an interval far inside the thresholds D23 already uses |
| D35 | Bridge protocol and agent-build skew | Every bridge document — `status.json`, `tree.json` and **every list response** — carries `"version": 1`, which **is** the protocol version; there is no second version field and no capability set. `status.json` additionally carries `"agentBuild": <non-negative int>`, a constant near the top of `agent.ps1` bumped in any commit that changes agent behavior; `bridge.py` carries the same number and a test compares the two literals. `bridge.py` validates `version == 1` next to each reader and raises a distinct `ProtocolError`, propagated to the controller through `health.Snapshot.compatibility` rather than collapsed into a generic "file unavailable" string. Build comparison is **equality**: anything but the bundled constant — lower, higher, missing or malformed — is `skewed`, which still works but shows a persistent yellow banner. Recovery in **both** the `skewed` and the `incompatible` state is an unconditional entry into the confirmed **Re-run Windows provisioning…** action (D40-D44) — never a `C:\OEM` instruction, and never a different code path per state: the banner button and the Status-tab/menu command invoke the same controller action with the same enablement rules. An unsupported `status.json` **or** `tree.json` version makes the channel `incompatible`: one central gate disables Apply and Restore, dispatches no list request, and leaves the current `exclusions.json` untouched; the document is not rendered merely because its JSON parsed. That gate deliberately exempts the re-provision action, which is what the banner points at and requires no guest state of its own. Until a status document has established `current` or `skewed`, compatibility is `unknown` and that gate stays **closed**. A merely missing or stale `tree.json` keeps browsing unavailable without overriding a compatible status. `exclusions.json` keeps its own existing version check. | A package upgrade ships a newer `agent.ps1` into the host bundle but cannot replace `C:\ProgramData\icloud-bridge\agent.ps1`, so GUI/agent skew was previously silent. The project is pre-release (the policy in `CONTRIBUTING.md`), so there is exactly **one** supported protocol and the pair is expected to match: skew is to be **detected and reported**, never accommodated with a legacy/older/newer matrix. Failing closed while compatibility is unknown is what stops the GUI guessing which agent is running and writing a config an unknown agent will misread. Recovery became one explicit confirmed action rather than a copyable instruction, and the two constraints behind that instruction are intact: the GUI still holds no guest-admin credentials and still never updates guest code silently — elevation lives in the guest watcher task (D40) and the update happens only through this explicit confirmed action. This does not amend D23: the protocol classification drives its own banner and the write gate, not the tray colour precedence |
| D36 | Host-side backup of the selective-sync choices | A mode-0600 snapshot at `$XDG_STATE_HOME/icloud-bridge-gui/exclusions-backup.json` (default base `~/.local/state`), in a 0700 app directory, written atomically through a unique temp file in the same directory; a symlink or non-regular directory/destination is refused rather than followed, and an existing regular backup is tightened to 0600 even when the write is skipped. Content is `{version, savedAt, source, revision, exclusions}` with `source` ∈ `read`|`apply` and `exclusions` the canonical D19 list; an **empty list is a valid backup** meaning include-everything. Written after every validated `read_exclusions` and every successful Apply, on the same worker thread as the operation that produced the data. An automatic **read** may only move the snapshot forward: a *lower* revision is kept (that is a rebuilt VM's fresh revision-0 config), and the same revision with different content is a retained, reported conflict; an explicit **Apply** may always replace. The bridge operation and the local snapshot are **two results**: a read or Apply that succeeded stays succeeded when only the backup failed, and shows a persistent yellow warning instead. Restore is **explicit and previewed, never automatic**: a *Restore from backup…* button enabled only in normal monitoring, with a loaded snapshot, no staged unapplied selection, and a compatible protocol; it validates and canonicalizes, previews additions/removals against the loaded selection, and writes through `write_exclusions(..., minimum_revision=<backup revision>)` so the result is strictly above `status.appliedRevision`, the loaded revision, the GUI's last write **and** the backup's own. A missing or corrupt backup is an error dialog, never an empty list, and never blocks normal operation; an old-but-valid backup is not rejected for being old | `exclusions.json` is the only unique configuration inside the otherwise disposable VM, and the fail-closed provisioning rule correctly refuses to manufacture an empty one after loss — which leaves recovery assuming the operator kept their own copy. The lower-revision rule is the whole point: without it the first automatic read after a VM rebuild would overwrite the snapshot with the empty config that rebuild produced, destroying exactly what recovery needs. Restoring is previewed rather than automatic because re-including or excluding folders moves real data; `minimum_revision` exists as its own parameter because "the revision I must beat" and "the revision I last wrote" are different facts and overloading one of them is how a later reader gets it wrong |
| D37 | Privacy-safe diagnostic report | **Copy diagnostics** and **Save diagnostic report…** on the Status tab, backed by Qt-free, mount-I/O-free `diagnostics.py` (`collect(facts, runner) -> Report`, `render(report) -> str`). `facts` is a typed **allowlist** dataclass the controller fills in explicitly — app version and coarse install origin (`package`/`per-user`/`source`/`override`, never a checkout or home path), lifecycle phase and cached container classification, marker state, health row **names and severities only**, bridge document versions/timestamps/revisions, agent build and skew classification, autostart state, and bounded last-helper-result / last-successful-gather fields the controller retains for this purpose. `collect` may run only `systemctl is-active` on the six units and the two argument-exact `sudo -n -l /usr/local/bin/icloud-bridge-power on|off` probes, through the injected bounded runner: no journal, no docker of its own, no CIFS — so the export works in **every** lifecycle state, which is when reports matter. Only the classified result of those probes is rendered, never their output. Redaction is default-on: operator paths become stable `<path-N>` placeholders, real names only on an explicit **Include folder names** tick. Raw agent `lastError`, raw health detail and unfiltered environments are **not admitted to `facts` at all**. Each field is bounded to 2000 characters with a `[truncated]` suffix and the whole report to 64 KiB. Save writes mode 0600, refusing a symlink or non-regular destination | Support currently means hand-collecting rows from the GUI, Docker, systemd and the journal — hardest in exactly the failure states where it is needed. Making the input an allowlisted dataclass rather than a filter is the point: a field nobody copied in cannot leak, whereas a redactor over free prose cannot reliably tell a sentence containing a filename from one that does not — which is why raw `lastError`, raw health detail and raw subprocess output are excluded outright rather than scrubbed. `.env`, `/etc/credentials-icloud`, `SHARE_PASS`, command environments, Apple identity data and file contents have **no opt-in** |
| D38 | Progress for long transactions | **No new IPC channel.** `host/icloud-bridge-power` already prints one `==> ` line per step; streamed live, that stdout *is* the progress feed — no progress file under `/run`, no socket. `power.stream_command` drains stdout and stderr on their own threads (a child filling one while we read the other would deadlock), enforces the monotonic deadline on `wait()` so a silent child still times out, splits on newline **and** carriage return, strips ANSI/control characters, caps each line, and keeps a bounded tail (50 lines / 64 KiB). A callback that raises cannot abort the transaction. `power_on`/`power_off` take an optional `on_line`; their result and error precedence **without** one are unchanged, and an explicitly passed runner still wins. `_TaskSignals` gains `progress = Signal(str)` and `run_async` an optional GUI-thread `on_progress`. Busy surfaces show elapsed time ticking from a Qt timer plus the most recent phase; **no percentages, no estimates, no cancel button**, and every existing timeout unchanged. Only bounded, sanitized `==> ` lines are treated as helper phases and the wording after the prefix is **presentation only, never a control input**; Compose has no such convention, so creation shows its most recent line elided to one row. The provisioning wait has no subprocess at all and shows elapsed time only. On an **outer** timeout the GUI enters `transition_unknown`: killing an unprivileged `sudo` is no evidence the root helper stopped, so all bridge I/O stays paused, cached documents/classifications are invalidated, the container is marked *unknown* rather than stopped, and the only mutating control is **Retry**, which repeats the same desired action (`flock` serializes it against a survivor). **Open VM screen** and the diagnostic export stay available; Quit is allowed but quitting-and-powering-off is not | Creation plus Windows provisioning spans 20-40+ minutes and power-on retries CIFS activation for up to five, against a static busy message. The helper's own stdout is a feed that already exists, is already bounded, and cannot go stale the way a file under `/run` can. The `transition_unknown` phase exists because the ordinary failure path resumes polling — against shares a surviving helper may already have unmounted. A cancel button is refused outright: interrupting the D29/D30 transaction halfway is exactly what the marker-then-ordered-teardown design exists to prevent |
| D39 | Interrupted-provisioning record | Before invoking Compose, the GUI atomically writes a private mode-0600 record at `$XDG_STATE_HOME/icloud-bridge-gui/provisioning.json` containing only `version`, `startedAt`, `phase` and (after success) the inspected container id — **never the env-file path and never its contents**. A later launch reads it *and* Docker before any CIFS access: a matching container — or the fixed-name container when the pre-Compose record has no id yet — re-enters D31's no-CIFS **Provisioning Windows** state; an absent container returns to Setup with retry guidance; a different container id shows a stale-record warning and performs no CIFS I/O; a malformed or unsupported record enters Setup with a diagnostic and is **never** silently deleted or treated as proof a VM is configured. A running container with **no** record keeps the existing startup behavior. The record is cleared only after **Check setup and connect** completes `power_on` successfully, or by a separately confirmed **Discard failed setup record** offered only when Docker has proved the container absent or different — which removes this local file and nothing else: no container, no VM disk, no env file, no bundle. Same 0700 directory, atomic-replace and symlink/non-regular rules as D36. **Amended by D43**, which makes the same record cover `reprovision` as well as `first-run` and adds the container's Docker `State.StartedAt` token, the guest run ID, the last guest phase, the mode, and the non-secret `resetShareCredential` intent; the container id stays, and the exclusion of the env-file path and its contents is unchanged | The app can be closed, crash or be logged out during the 20-40 minutes Windows takes to install. Without the record the next launch sees a running container with no configuration and has to guess — and the wrong guess is the one that touches a mount belonging to a half-built guest. Not persisting the env path is deliberate: it must never become a second place a share password can be reached from, which is why a restarted package install may ask the operator to select that file again before showing the final host configuration command |
| D40 | Guest provisioning channel | The host stages provisioning code on a new **read-only-to-the-guest** Samba share `Provision` (container `/run/icloud-bridge-provision`, mode 0700, root-owned; guest `\\host.lan\Provision`), installed as a marker-delimited `[Provision]` stanza in dockur's generated configuration — `read only = yes`, `guest ok = yes`, `guest only = yes`, `force user = root` — validated with `testparm` in a temporary copy, atomically replaced, then `smbd` reloaded. dockur's guest-writable `Data` path is never edited. An elevated scheduled task `icloud-bridge-provision` (principal `icloud`, `LogonType Interactive`, **`RunLevel Highest`**, at-logon, infinite loop with restart, `IgnoreNew`) runs the hardened installed `C:\ProgramData\icloud-bridge-provision\watcher.ps1`, polls `\\host.lan\Provision\trigger.json` every 30 s, validates it, copies the fixed payload allowlist into an administrator-only per-run directory, atomically records the accepted run ID locally, and executes **only** that protected copy. Because the share is read-only the host, not the watcher, removes the trigger during cleanup; the local accepted-run marker gives consume-once semantics. Progress JSON is written atomically to the separate guest-writable `\\host.lan\Data\.provision\status.json` and is always parsed as untrusted input. `install.bat` registers the task at OEM time; a VM installed before this feature takes one elevated bootstrap command once (§4.1). QEMU-monitor keystroke injection stays a `tools/` debugging aid and is never installed or invoked by the app | Every step is verified by effect — files and JSON, not screenshots — and nothing is typed, so there is no blind injection to confirm by OCR and no secret on a keystroke path or a screen. It reuses D17's task pattern rather than adding a runtime, listener, port or firewall rule to a guest holding an Apple session. Above all it creates no guest-local elevation path: dockur serves `Data` `writable`/`guest only`/`force user = root`, so any guest process can replace a script staged there, and executing from it would promote the deliberately `RunLevel Limited` D28 agent to a silent administrator. Only the host root/docker-group user can write executable input, which is the boundary v1 D9 already assumes and which the monitor socket and OEM delivery already grant |
| D41 | `SHARE_PASS` delivery to the guest (narrows D31's "never handled") | A new Qt-free, mount-I/O-free `guestprov.py` is the only GUI code permitted to return the `SHARE_PASS` value, and the trigger carries **no secret**. Only after the guest reports `waiting-for-secret` does it re-read the explicitly selected env file and stream the exact UTF-8 bytes over `docker exec -i` **stdin** into a run-scoped temporary file in the container inbox, atomically renamed to `secret` — never argv, never environment, never a host temp file, never logged, never in status, never on the clipboard, never persisted by the GUI. The elevated orchestrator copies it without text decoding to a protected local file and only then advances to `creating-share`; that transition is the acknowledgement that lets the host delete the remote copy. `03-create-share.ps1 -PasswordFile` reads the local copy once and deletes it in a `finally` before changing the account, the orchestrator deletes any residue in an outer `finally`, and watcher start plus every new-run preflight removes a local `secret` stranded by a reboot. Host cleanup may delete an unacknowledged remote secret on completion, failure, explicit exit and before a new run: that merely leaves the guest in `waiting-for-secret`, so a restarted GUI asks for the env file again and re-delivers. 03's manual placeholder path remains the fallback | D31's boundary was deliberate, so automating script 03 amends it deliberately and as narrowly as possible: one module, one direction, one moment. Deferring delivery until the guest is provably waiting is what keeps a secret out of the guest across the unbounded Apple sign-in wait, and stdin plus an atomic rename is what keeps it out of every surface — argv, environment, logs, host temp files, screenshots — that D37's redaction rules could not scrub after the fact. A deleted secret is recoverable by re-delivery, which is why cleanup may be aggressive |
| D42 | Provisioning script currency | Every trigger re-stages the installed bundle's current `03-create-share.ps1`, `04-bridge-agent.ps1`, `agent.ps1`, `guest-state.ps1`, `guest-setup.ps1` and `watcher.ps1` into the read-only inbox **before** writing the trigger; the watcher copies exactly that allowlist into a protected per-run directory and executes only those copies, and `04-bridge-agent.ps1` resolves its sibling `agent.ps1` from `$PSScriptRoot` instead of hard-coding `C:\OEM\agent.ps1` (§4 step 2). After a non-blocked inspection the orchestrator transactionally refreshes an administrator-only `current` directory for the documented manual fallback and refreshes the installed watcher, which takes effect **within a minute**: at the top of a poll pass, where no run is in flight, the watcher compares the installed copy against the one it started from and exits if they differ, and a one-minute keep-alive repetition on its own task starts the new copy. That repetition — not `RestartCount` — is also what makes the watcher survive a crash or a kill. `C:\OEM` may also be refreshed for operator inspection but is never an elevated execution source. The watcher envelope — `version: 1`, the fixed filenames, the UUID run ID — is deliberately frozen; changing it requires re-running the documented bootstrap | `provision/` is copied to `C:\OEM` at install time only, so an OEM copy goes stale the moment the repo moves: the live VM's copy was four commits behind, missing D35's own skew detection, and automation running it would install stale code. Staging per run fixes that without opening the time-of-check/time-of-use hole that executing from a guest-writable or weakly-ACL'd directory would create (D40). The envelope limit is stated rather than papered over: a running old watcher cannot safely upgrade the protocol that authenticates its own replacement, so that one upgrade stays an explicit operator step. Code currency is a different question from envelope currency, and leaving it until the next logon proved to be the expensive half: a watcher that is alive but running superseded code can accept a run, fail on the new path and retry silently every 30 s, which the host cannot distinguish from a guest that has no watcher at all. The keep-alive is a repetition trigger because Task Scheduler's restart-on-failure does not fire when the action exits non-zero — measured in the guest, where a task exiting 3 under `RestartCount 3` was never relaunched, while a one-minute repetition restarted a cleanly exiting task at 09:56:03, 09:57:03 and 09:58:02. `-Install` stops a running instance before registering, because `IgnoreNew` otherwise makes its `Start-ScheduledTask` a silent no-op against exactly the wedged watcher it exists to repair |
| D43 | Durable guest-provisioning transactions (amends D39) | The private record of D39 covers both `first-run` and `reprovision` and additionally stores the container id and its Docker `State.StartedAt` token, the guest run ID, the last guest phase, the mode, and the non-secret `resetShareCredential` intent — still **never** the env-file path and never its contents. It is written before the trigger and updated only after a matching status is parsed. A restart with a live matching container and start token plus an active run re-enters D31's no-CIFS `Phase.PROVISIONING` and polls the recorded run ID, asking the operator to reselect the env file if the guest is waiting for a secret (D41). A changed start token together with a missing or stale status offers a confirmed new idempotent run rather than adopting an uncorrelated status; without restart evidence, a missing status keeps polling and requires an explicit **Abandon and start a new run** confirmation. A record still in host `staging` with no acknowledged status retries `ensure_channel()`/`stage()` with the **same** saved run ID, covering a crash before the trigger's atomic rename. First-run success continues into **Check setup and connect**; reprovision success verifies the current bridge protocol and bundled agent build through a fresh gather, clears the record, invalidates caches and returns to monitoring. No status is trusted merely for being the newest file | D39 exists because Windows installs for 20-40 minutes; provisioning adds a second window, in which elevated guest scripts are rewriting the share, its ACLs and the agent task, and the wrong guess mounts CIFS into the middle of that. The run ID is what makes reattachment a match rather than an assumption, and `State.StartedAt` is what separates "the container restarted" from "the watcher is merely quiet" — two conditions whose correct responses are opposite. Keeping the env path out of the record is unchanged from D39, which is precisely why re-selection, not recovery, is the restart path for a pending secret |
| D44 | Inspect, reconcile, verify | A provisioning run is a desired-state reconciliation, not an unconditional replay of scripts 03 and 04. Before changing any guest configuration the protected orchestrator evaluates the fixed checklist of §4.2 and publishes its observations plus a fixed-enum work plan. `ok` components are skipped; a safely repairable `missing` or `drifted` component invokes only its owning repair scope; a `blocked` or `unknown` observation stops the run before mutation instead of being read as absence. After any selected repair the orchestrator evaluates the complete checklist again and reports `done` only when every required invariant is `ok` — `shareCredential`, which Windows cannot read back, is the one explicitly `unverifiable` exception. If the initial complete checklist selects no repair, that same current observation is the verification and the run does not repeat it. First-run and an explicit **Reset share password** request the credential (D41); ordinary reprovisioning preserves an existing credential unless the account is missing. The app renders the checklist and the proposed/completed work, but the elevated scripts independently re-probe every precondition and never authorize a mutation from guest-writable status JSON | An agent-build update must not reset a working SMB password, rerun share setup, or rewrite unrelated ACLs, while a partly built or hand-modified VM must still converge and say exactly what stopped it — requirements that are only compatible if the run inspects first and repairs by owner. Refusing to guess past `blocked`/`unknown` is the fail-closed rule D22(f) and D35 already apply: an ambiguous guest is diagnosed and contained, never overwritten to make a checklist green. Re-verifying after mutation makes `done` a claim about the VM rather than about which scripts exited zero; repeating an identical full-tree observation after no mutation strengthens no claim and only doubles the proportional work |
| D45 | Surfacing why a mount never came up | When `on`'s readiness wait expires, `icloud-bridge-power` appends an excerpt of the two `.mount` units' recent journal to its failure message. The excerpt is built by `sanitize_journal_excerpt`, a pure function of one string (like `classify_inspect_output`, and separately tested the same way): an allowlist keeps only lines carrying a mount/CIFS diagnosis, systemd's own restatements and ordinary unit chatter are dropped, a password-bearing `key=value` loses its value, and the tail is kept, bounded to a few short lines per unit. Collecting it is read-only, bounded by `timeout`, and never fatal — no journal or nothing that passes the filter simply leaves the message as it was. Everything else about the transaction is unchanged: the same `EXIT_READINESS`, the same marker and teardown, no new `==> ` phase line, and the GUI still shows stderr and derives no control decision from an exit code | §4.2 makes an authenticated connection the only proof that the host's share password matches the guest's, and the app acts on that proof by matching authentication wording in the helper's stderr to offer **Retry and reset share password…**. But the helper's only mount-failure text was the generic ready-deadline sentence, while the real CIFS error — a rejected credential, a share the guest does not export, an unreachable server — stayed in the mount units' journal, where the GUI cannot look. A wrong password was therefore indistinguishable from a slow guest and the credential route could almost never fire. The excerpt is filtered rather than quoted raw because this is a root helper on the privileged power path: it may explain a failure, never become a log pipe, and never carry a secret |
| D46 | Episodic exclusion verification and resident residue | Verifying a pending root is an **episode** that may span enforcement passes. Stage 1 is the uncapped attribute-only walk: any file lacking `RECALL` and longer than `$ResidualFileMaxBytes` (4096, one NTFS cluster) means content is still plainly local, so the root reports `pending-dehydrate` with those files' summed `Length`, drops any in-flight episode, and spends no queries. Otherwise stage 2 queries the candidates that could still hold bytes — `RECALL` files and files no longer than the bound — ordered ordinal-ignore-case, resuming strictly after an in-memory cursor and spending at most `$MaxPlaceholderQueriesPerPass` queries per pass; mid-episode passes report `<checked> of <total> file(s) checked`. Only an episode that reaches the end of its list may decide, and only with zero blocking allocation and zero unreadable items. An in-sync, unmodified placeholder no longer than the bound whose `OnDiskDataSize` is nonzero is **residue**: reported in `detail` and `localAllocatedBytes`, not blocking. A not-in-sync, modified or non-placeholder file is never residue, whatever its size. Episode state is memory-only (like the dehydrate-request throttle) and is dropped when the root leaves the wanted set, when the configuration changes, and by stage 1 above. The D34 re-verification of an applied root likewise ignores files at or below the bound and carries the episode's residue figures forward rather than re-measuring them, which is what stops an applied-with-residue root oscillating. Enforcement is untouched: the deny and parent guard are still asserted every pass, and request throttling, the sweep and the status schema are unchanged | Both halves fix roots that could never leave `pending-dehydrate`. The measurement restarted from the top of the tree every pass and stopped after 5000 placeholder queries, and `applied` demanded an uncapped clean walk — so a 231 GB root of ~10^5 files reported "measurement capped" forever, because a dehydrated placeholder keeps its directory-entry `Length` and still costs a query. A cursor is the same answer D26/D34 already gave the reclamation sweep for the same shape of problem: bounded steady-state work, label freshness allowed to lag, safety unaffected. Separately, three app-container roots held a few hundred bytes each in in-sync, unmodified placeholders that iCloud will never dehydrate because NTFS stores data that small resident in the MFT record; without a tolerance those roots hold the exclusion (and the D23 tray) yellow forever while the agent re-requests a dehydration Windows cannot perform. The tolerance is bounded to one cluster per file so the misreport stays trivial, and its carve-outs are data safety, not tidiness: a non-placeholder's content may exist nowhere but this disk, and a not-in-sync placeholder's local copy is the newest one, so calling either "applied" would hide the only good copy (D20/D22) |
| D47 | Boot-rewritten `desktop.ini` passes the residue sync gate | A stage-2 verification candidate that is a cloud placeholder named `desktop.ini` (ordinal-ignore-case), carries both `HIDDEN` and `SYSTEM`, and is no longer than `$ResidualFileMaxBytes` counts as D46 residue even when Cloud Files reports it modified or not in sync, and never contributes to the modified/not-in-sync detail. Everything else about D46 is unchanged: the size bound, the non-placeholder refusal, the reporting of residue bytes in `detail` and `localAllocatedBytes`, and the in-sync requirement for every other name | Windows rewrites a folder's `desktop.ini` at boot, after the iCloud client's startup scan, and the client was observed (2026-07-28, live host) leaving all three app-container copies "modified" for over an hour with re-requests and a fresh change event failing to move it -- so each reboot re-opened an indefinite `pending-dehydrate` window (yellow tray, D23) on roots whose real content was long released. The D46 sync gate exists so `applied` never hides the only good copy of user data; `desktop.ini` is per-machine folder-view configuration the shell itself generates and regenerates, so no user data can be lost by exempting it. The guards keep the exemption narrow: exact name, both attribute bits the shell always sets on it, one-cluster size cap, and placeholder-only |

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

What is pinned here is the **output**, not the implementation. Since 2026-07-26
the string escaping lives in the compiled `IcloudBridgeNative::JsonString` and a
whole document is appended into one `StringBuilder`, replacing a per-character
PowerShell loop and a per-level `List[string]` plus `-join` that recopied every
byte once per level of nesting. Measured 5.3x faster on a 5 461-node tree
(3.07 s to 0.58 s under PowerShell 7). Any future rewrite is equally free, on one
condition: `tools/test-bridge-json.ps1` (`make test-ps`) must still pass. It
asserts the exact bytes for control characters, quotes, backslashes, `[]` for
empty collections, key order, integer and double formatting, and the depth guard.

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
  "agentBuild": 1,
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
  `error`. `applied` means: exact target deny and parent guard are present and a
  verification episode ran to completion (D46) finding no blocking allocation and
  nothing it could not inspect. The one tolerated exception is residue: an
  in-sync, unmodified placeholder no larger than one NTFS cluster
  (`$ResidualFileMaxBytes`, 4096) whose `OnDiskDataSize` is nonzero because NTFS
  keeps data that small resident in the MFT record, which no dehydration can
  release. Such files are *reported* — `localAllocatedBytes` carries their bytes
  and `detail` names them — instead of blocking the transition. A hidden+system
  placeholder named `desktop.ini` within the same size bound is residue even
  when modified or not in sync (D47): Windows rewrites it at boot and the shell
  regenerates it on loss, so the sync gate would hold the root yellow forever
  for a file that carries no user content. Any other file that is modified, not
  in sync, or not a cloud placeholder blocks `applied` whatever its
  size. Verification of a large root is spread across passes (at most
  `$MaxPlaceholderQueriesPerPass` placeholder queries each), so after the content
  is really gone the label can still take ceil(files / that budget) further
  passes to advance, reporting its progress in `detail` meanwhile.
  `not-found` means the path does not (yet) exist under the sync root — the agent
  keeps checking each cycle (the item may arrive from the cloud later) and hides it
  the moment it appears. Because a missing object cannot be protected by a named
  NTFS ACE, `not-found` is yellow, not healthy. An `error` whose `detail` begins
  `acl-write-denied:` means the D28 preflight failed for that path — provisioning
  step 4 has not been applied, or that object carries a protected DACL; the item
  is left completely untouched.
- An `applied` entry's `state` and `logicalBytes` may be up to ~10 minutes stale
  (D34): once a root has settled, the recursive walk that re-confirms it runs
  every ~10th pass rather than every one. Only the label lags — the deny and
  parent guard behind it are re-asserted on every 60 s pass, so access never
  does. `applying`, `pending-dehydrate` and `not-found` are measured every pass
  as before, and any configuration change re-measures everything immediately.
- `icloudClientRunning`: true iff a process named `iCloudHome`, `iCloudDrive` or
  `iCloudServices` exists. This is process liveness only; it does not prove
  Apple-side sync health. The shipping Store client runs `iCloudHome`,
  `iCloudDrive`, `iCloudCKKS` and `ApplePhotoStreams`, and no `iCloudServices`
  at all — that name belongs to the older Win32 client and is kept only so an
  older install still answers. Watching the app process as well as the sync
  engine is deliberate: the client's processes restart together as a group, and
  a probe holding only the engine's name reports a routine restart as an outage.
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
- `exclusions[].localAllocatedBytes` is different and **is** exact once the
  episode has queried the file: it comes from
  `CfGetPlaceholderInfo.OnDiskDataSize` (plus `GetCompressedFileSizeW` for
  non-placeholders), because `applied` requires proving `OnDiskDataSize == 0`
  and `RECALL` alone cannot prove it. Two states report less precisely on
  purpose (D46): while the attribute walk still finds fully local files, the
  field carries their summed `Length` rather than spending queries on a tree
  that is visibly still dehydrating, and mid-episode it carries the running
  total of what has been queried so far.
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

- `version` **is** the bridge protocol version and is exactly `1` (D35). Missing,
  boolean, non-integer or any other value makes the whole channel incompatible;
  the GUI then writes nothing and dispatches no list request. `agentBuild` is a
  non-negative integer identifying the agent build; the GUI compares it to its own
  bundled constant by **equality** and reports any difference, in either
  direction, as skew.

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
  "version": 1,
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
The response carries `"version": 1` like the other two documents (D35), including
on the error path — a failure the GUI can read is more useful than one it has to
reject as unversioned.

---

## 3. Guest agent — `guest-agent/agent.ps1`

One file, PowerShell 5.1 compatible (ships with Windows). Constants at top:

```powershell
$SyncRoot = "$env:USERPROFILE\iCloudDrive"
$BaseDir  = "C:\ProgramData\icloud-bridge"
$BridgeDir = Join-Path $BaseDir "io"
$StateDir  = Join-Path $BaseDir "state"
$ShareUser = "syncshare"
# Bridge protocol identity (D35). One supported protocol version; $AgentBuild is
# bumped in any commit that changes this script's behavior, and bridge.py carries
# the same number so the GUI can report skew.
$ProtocolVersion = 1
$AgentBuild      = 1
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

The status writer emits `version = $ProtocolVersion` and `agentBuild = $AgentBuild`; the tree writer emits `version = $ProtocolVersion`; and the list responder sets `version = $ProtocolVersion` on the response object **before** its `try` block, so an error response carries it too (D35).

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

1. Candidate files under an exclusion in `applying` or `pending-dehydrate`, where
   `applied` requires proving `OnDiskDataSize == 0` (or one cluster of residue,
   D46), staged per §3.1 so a pass spends a bounded number of these calls.
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

    # verification is an episode that may span passes (D46)
    # stage 1: attribute-only, uncapped, opens no handle, runs every pass
    walk the root's files from attributes alone; a file WITHOUT $ATTR_RECALL and
        longer than $ResidualFileMaxBytes is fully local content the request has
        not moved yet
    if any such file exists: state = pending-dehydrate,
        localAllocatedBytes = their summed Length, drop any in-flight episode,
        and spend no placeholder queries this pass
    a directory the walk cannot enumerate counts as unreadable and blocks
        `applied` the same way an unqueryable file does: it hides whatever it
        holds, and a root is never applied over a subtree nothing inspected
    otherwise collect the candidates worth a query -- non-empty files that carry
        $ATTR_RECALL (possibly still partly hydrated) or are no longer than
        $ResidualFileMaxBytes (possibly resident residue) -- ordered
        ordinal-ignore-case by relative path, the same comparator the sweep's
        stage-2 cursor uses

    # stage 2: bounded placeholder queries, resumed from the episode cursor
    process candidates strictly after the cursor, at most
        $MaxPlaceholderQueriesPerPass per pass, advancing the cursor over each:
        placeholder, OnDiskDataSize > 0, in sync, ModifiedDataSize == 0 and
            Length <= $ResidualFileMaxBytes -> residue: count its bytes and
            report them, but do not let them block (NTFS keeps data this small
            resident in the MFT record, so no dehydration can free it and iCloud
            never completes the request)
        placeholder named desktop.ini carrying both HIDDEN and SYSTEM, within
            the same size bound -> residue even when modified or not in sync
            (D47): the shell rewrites it at boot, the client can leave it
            "modified" indefinitely, and it holds machine-generated view
            configuration the shell regenerates on loss -- it also never counts
            towards the modified/not-in-sync detail
        any other placeholder with OnDiskDataSize > 0 -> blocking allocation;
            also count modified/not-in-sync for the detail
        non-placeholder -> blocking if it has allocation, and never residue
            whatever its size: its content may exist nowhere but this disk, so
            calling it applied would hide the only copy (D20/D22)
        unreadable -> blocking, reported as could-not-inspect
    budget exhausted before the end: keep the episode, state = pending-dehydrate,
        detail reports "<checked> of <total> file(s) checked"; the next pass
        resumes at the cursor instead of restarting the tree
    end of the list reached: decide from the episode totals and drop it.
        state=applied only when target deny + parent guard are present and the
        completed episode found zero blocking allocation and zero unreadable
        items; residue (if any) goes in detail and localAllocatedBytes.
        Otherwise pending-dehydrate, and a fresh episode starts next pass
    an episode is also dropped when its root leaves the wanted set and whenever
        the configuration changes; it lives in memory only, so an agent restart
        merely re-measures

    once applied, the cheap re-verification walk that keeps that label honest is
        decimated (D34): every pass for the first few passes after the transition,
        then every ~10th, and immediately on any configuration change. It is
        reporting only -- the deny and guard above are re-asserted every pass --
        so a label (and its logicalBytes) may lag by up to ~10 min, matching the
        tree.json cadence. That walk ignores files at or below
        $ResidualFileMaxBytes -- it cannot re-measure residue without opening
        handles, so the episode's residue is carried forward instead, and a root
        applied with residue does not oscillate back to pending. Content
        reappearing above that bound drops the root back to per-pass measurement

atomically persist {roots, guardedParents, appliedRevision, wantedHash} in private
applied.json, alongside the reconciliation cursor, the D34 "nothing to reconcile"
flag, and the sweep episode fields

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

    the two candidate walks are the dominant recurring cost of an episode, and an
        episode spans many passes: cache each stage's list and re-walk only every
        ~5th pass (D34). Correctness does not depend on freshness -- every
        candidate is re-checked with CfGetPlaceholderInfo immediately before its
        request -- but an episode may NOT end on a stale list: "nothing eligible"
        and stage-2 exhaustion both force a re-walk first. Iteration restarts from
        the coldest entry each pass rather than consuming the list, so a request
        that Windows has not yet honoured still counts towards the deficit instead
        of pulling in further files

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
- The per-entry DACL read that reconciliation performs is the expensive part of
  that pass, and it is skippable in exactly one state (D34): the validated wanted
  set is empty, private state holds no roots and no guarded parents, no resume
  cursor is outstanding, and the previous pass ran to completion having removed
  nothing and hit no error. An orphan agent-owned deny cannot exist under those
  conditions. Persist that "nothing to reconcile" conclusion in private state as
  a boolean, defaulting to **false** so a missing or corrupt state file forces a
  real pass. The enforcement pass must clear and persist the flag **before** its
  first ACL write, never after: a crash between adding a deny and persisting
  state would otherwise leave an orphan no later pass looks for. Any exclusion,
  any removal, and any reconciliation error also clear it, and **agent startup
  clears it unconditionally** — the flag may only ever be re-armed by a pass this
  process ran itself, so the startup reconciliation above still happens in full
  and anything that changed while the agent was stopped is still caught. The
  attribute-only walk that produces `tree.json` is unaffected and still runs
  every ten minutes.

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
2. Copy the **sibling** `agent.ps1` — `Join-Path $PSScriptRoot 'agent.ps1'` — to
   `C:\ProgramData\icloud-bridge\agent.ps1` (D42). For the manual fallback that
   sibling is `C:\OEM\agent.ps1`, because docker-compose mounts `./provision` at
   `/oem` → `C:\OEM` and `guest-agent/agent.ps1` is copied into `provision/` at
   build time (§8 task A3); for an automated run it is the protected per-run
   payload directory of §4.1. The script must not hard-code either path.
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

### 4.1 Automated provisioning — channel and protocol (D40-D42)

Everything above stays exactly as written: it is what the operator runs by hand,
and it is what the automated path executes on their behalf. The app never invents
a second way to configure the guest — it stages the same scripts, elevated, and
watches their effects. Apple ID sign-in and the iCloud Drive toggle remain manual
(`CONTRIBUTING.md` Scope).

Two directories, with opposite trust:

| Role | Container path | Guest path | Writable by |
|---|---|---|---|
| Executable inbox | `/run/icloud-bridge-provision/` | `\\host.lan\Provision\` | host only (share is `read only = yes`) |
| Status outbox | `/tmp/smb/.provision/` | `\\host.lan\Data\.provision\` | guest (dockur's existing `Data`) |

Nothing reachable through a guest-writable path is ever executed elevated (D40).

`guestprov.ensure_channel()` reconstructs **only** its marker-delimited
`[Provision]` stanza in a temporary copy of dockur's generated `smb.conf`,
validates that candidate with `testparm`, atomically replaces the config, and
reloads `smbd`; a failure leaves dockur's working configuration untouched. A
same-named stanza outside its own complete marker block is a conflict and fails
closed — it is never merged or overridden. Every shell program and path in the
command is a fixed constant: no env value, run ID, bundle path, status text or
password is interpolated into shell source. The inbox is mode 0700 and root-owned
in the container. The host refuses to stage unless the effective Samba
configuration reports the exact share path and `read only = Yes`.

`trigger.json` is written by the host **last**, after the six payload files of
D42 are in place, through a temporary name and an atomic rename. The secret is
deliberately absent from it (D41):

```json
{"version":1,"runId":"<32 lowercase UUID4 hex characters>",
 "action":"reconcile","resetShareCredential":false}
```

`action` is exactly `reconcile`: no arbitrary command and no repair-step list
crosses the elevation boundary. `resetShareCredential` is a JSON boolean — `true`
for first run or when the operator explicitly chooses **Reset share password**,
otherwise `false`. The protected orchestrator, not the host, derives the work
list from a fresh inspection (D44). An inspect-only action is deliberately not
offered: every confirmed run publishes its inspection before and during repair,
and ordinary bridge monitoring is already the cheap continuous health probe.

`status.json` is written by the guest orchestrator with the same
temp-then-rename pattern §4 step 6 uses in the guest, and every writer —
including the one on `\\host.lan\Data` — keeps the fully atomic `File.Replace`.
Its third argument must be `[NullString]::Value`, never `$null`: PowerShell
marshals `$null` to the empty string when binding a `[string]` parameter, and
`File.Replace` rejects `""` as a backup path with "The path is not of a legal
form" on every destination, local or UNC. `$null` therefore let each document be
written exactly once — the run that created it, which takes the file-absent
`Move` branch — and threw on every write after that. UNC is not the constraint
here, and no writer needs a delete-then-rename accommodation:

```json
{"version": 1, "runId": "<echoed>", "phase": "<see list>",
 "detail": "<one bounded human line>", "updatedAt": "<ISO-8601 UTC>",
 "error": null,
 "checks": {
   "icloudPackage": "<check state>", "syncRoot": "<check state>",
   "shareAccount": "<check state>", "shareCredential": "<check state>",
   "dataShare": "<check state>", "bridgeBoundary": "<check state>",
   "agentInstall": "<check state>", "agentRuntime": "<check state>"
 },
 "work": ["<zero or more fixed work IDs>"]}
```

Check states are exactly `pending`, `ok`, `missing`, `drifted`, `blocked`,
`unknown`, `unverifiable`. Work IDs are exactly `install-icloud`,
`wait-for-signin`, `create-share-account`, `reset-share-credential`,
`repair-data-share`, `repair-bridge-boundary`, `update-agent`. The GUI validates
the complete key set, the states, the work IDs, the types and the size before
rendering **locally owned** labels; missing or extra keys and impossible
combinations make the status unreadable, and guest-provided `detail` never
decides what code runs.

Phases, in order where the corresponding work exists: `staging` (payload copied
into the protected run directory), `inspecting`, `installing-icloud`,
`launching-icloud`, `waiting-for-signin`, `waiting-for-secret`, `creating-share`,
`installing-bridge-boundary`, `installing-agent`, `verifying`, `done`. A skipped
component never gets a fake busy phase: its check stays `ok` and its work ID is
absent. On failure `phase` stays at the failing phase and `error` becomes a
bounded message. An unknown trigger version or action, an invalid run ID, a
missing payload file or a copy failure makes the watcher write an error status
for that run and mark it consumed locally, so it never loops on the same bad
trigger. The GUI treats an unknown phase or version, or a malformed field, as an
error and never as progress.

Rules a weaker model must not improvise around:

- The host matches `runId` before trusting a status file. A stale or mismatched
  `runId` means "no acknowledgement yet", not progress.
- `runId` is `uuid.uuid4().hex`, validated as exactly 32 lowercase hex characters
  on both sides and stored in D43's record. A timestamp is neither unique enough
  nor an acceptable path component.
- The watcher executes only protected local copies of the six allowlisted files
  (D42). Nothing under `Data` or `C:\OEM` is an execution source.
- `checks` and `work` are explanatory output from an untrusted channel, never
  capabilities or instructions. The elevated orchestrator derives and revalidates
  its own in-memory work plan and accepts no host-supplied phase list, path,
  command, script name, account name or share name.
- The secret file is never mentioned in `status.json`, in `detail`, or in any log.
- The secret is exact UTF-8 with no added newline, and its grammar is
  deliberately small: exactly one physical line beginning `SHARE_PASS=` in column
  1, every byte after the first `=` being value (so `#` and later `=` are data),
  with no quote processing, no surrounding whitespace, and no NUL or CR/LF.
  Duplicate or quoted forms are rejected rather than interpreted. `firstrun.py`,
  `guestprov.py` and `host/icloud-bridge-configure` must apply that one rule, so
  the guest account and `/etc/credentials-icloud` cannot silently receive
  different passwords.
- `waiting-for-signin` has no timeout — it is the manual step, polled every 15 s,
  unbounded. Every other phase carries a host-side deadline (10 minutes for
  `installing-icloud`, 5 minutes for each of the rest). A deadline, or a status
  mtime frozen for more than 120 s during an active phase, surfaces a "stalled"
  warning with the manual fallback while polling continues; the guest may merely
  be slow. The orchestrator rewrites a heartbeat at least every 30 s during both
  waits and while each child process runs, which is what makes 120 s of silence
  meaningful. The two waiting phases have the heartbeat check and no elapsed
  deadline. The read-only checklist injects that same heartbeat into both
  proportional bridge-boundary walks, so a large library remains visibly live
  during `inspecting` and `verifying` without putting ambient status I/O into
  `guest-state.ps1`.
- The sign-in wait is unbounded but not passive. `winget` returns before the
  MSIX registration is necessarily usable, so the orchestrator waits up to two
  minutes for the package to report itself installed before it activates the
  client for the first time, and launches anyway on timeout — a visible failed
  launch is more diagnosable than an abort. During the wait itself, an absent
  client process means the operator has nothing to sign into while the heartbeat
  keeps saying otherwise, so the client is relaunched, at most once every five
  minutes. The rate limit is what keeps a client that fast-crashes on every
  activation from becoming a restart loop. Neither addition puts a deadline on
  the wait: it still ends only when the sync root appears.
- Re-triggering is always safe: reconciliation re-probes instead of trusting the
  previous phase, the 03/04 repair scopes are idempotent, `installing-icloud`
  skips when `Get-AppxPackage AppleInc.iCloud` already answers, and the watcher
  consumes one trigger at a time. A new run is never staged over an acknowledged
  active run; after a reboot, an error or an explicit retry it takes a new UUID
  and its own protected run directory.
- `detail` and `error` are single-line strings capped at 500 characters after
  control-character removal, and a status read is capped at 64 KiB. The host
  keeps the matching terminal status until D43's record has been cleared:
  cleanup removes executable inbox content and any secret, never the only
  evidence needed to resume safely after a GUI crash.

On a VM created before this feature there is no watcher task, so a staged
trigger is simply never acknowledged — which is not an error. The GUI keeps
polling and shows the one-time elevated bootstrap, run once in the guest:

```
powershell -ExecutionPolicy Bypass -NoProfile -File \\host.lan\Provision\watcher.ps1 -Install
```

The installer asserts its own elevation and that `icloud` is a member of the
built-in Administrators SID `S-1-5-32-544` — dockur currently places its
configured user there, but the image is unpinned, so that is an upstream detail
and not a substitute for the runtime check. Once it runs, the watcher consumes
the already-staged trigger with no further host-side click.

### 4.2 Desired-state inspection and reconciliation (D44)

Inspection is implemented once, in the side-effect-free `guest-state.ps1`, and
reused after any repair. It is read-only: even creating a missing directory
counts as repair and happens later. A no-op run uses that initial complete
inspection as its verification rather than repeating it. The checklist is
deliberately fixed, so the GUI can render stable local labels and tests can
exhaust its state matrix:

| Check | `ok` means | Repair owner |
|---|---|---|
| `icloudPackage` | The exact `AppleInc.iCloud` AppX package is registered for the `icloud` user | Install through `winget --source msstore` (v1 D4), with bounded retries for Store readiness and v1 script 02's manual Store fallback named on exhaustion; never remove an unexpected package |
| `syncRoot` | The exact `C:\Users\icloud\iCloudDrive` path is an accessible directory | Launch iCloud and wait for the operator's sign-in and Drive toggle; a wrong-type or inaccessible object is `blocked`, never deleted |
| `shareAccount` | Local `syncshare` exists, is enabled, does not expire, carries the required password/account flags, and has the hidden-logon registry value | Script 03 account scope. Creating the account needs the secret (D41); non-secret property drift does not |
| `shareCredential` | Never inferred from Windows account metadata | Always `unverifiable`. First run, a missing account, or an explicit **Reset share password** schedules a reset; otherwise the credential is preserved. A later authenticated host connection is separate corroboration, not a recovered password |
| `dataShare` | Share `icloud` points at the exact sync root with the expected `syncshare` access; LanmanServer state/startup, firewall rules, the D32 signing/encryption settings, and the root `syncshare` ACE match this plan | Script 03 share scope. A wrong share path is recreated after inspection without deleting its target or its contents |
| `bridgeBoundary` | The §4 step 6 exclusions safety preflight passes, and the bridge paths/share, ABE, the D27/D28 ACL boundaries and the single-pass full read-only traversal-link / protected-DACL / legacy-explicit-allow scan all pass | Script 04 boundary scope. This long metadata scan writes heartbeats and runs only during provisioning, never in background status polling |
| `agentInstall` | The installed `agent.ps1` hashes to the staged source and the task's action, principal, run level, trigger, restart policy and protected paths match exactly | Script 04 agent scope; it does not walk or normalize the iCloud tree |
| `agentRuntime` | The exact task is running and a fresh `status.json` reports the one supported protocol version and the staged `agentBuild` (D35) | Start the already-correct task, or run script 04's agent scope, then wait out §4 step 9's existing verification window |

Rules for deriving work:

- Compute the complete checklist before the first mutation. If any check is
  `blocked` or `unknown`, publish the whole checklist and stop before changing
  configuration. The only things that authorize a mutation are expected absence
  (`missing`) and enumerated drift with a named repair owner. `pending` is
  allowed only where an unmet earlier dependency makes a downstream probe
  meaningless — bridge-boundary checks before the sync root exists, for
  example. It is not healthy, it is re-probed once the dependency converges, and
  it may not survive to `done`.
- Work is dependency ordered, not blindly script ordered: package and sign-in
  work precede share work, share-account and data-share work precede the bridge
  boundary, and the agent is last. Re-inspect downstream dependencies after a
  wait such as sign-in, because the VM may have changed while the app waited —
  and equally after any stage that satisfies another check's dependency. The
  share stage is what makes the bridge-boundary probe answerable, so the scope
  script 04 runs is derived after script 03 has run, never from the plan that
  was current while the account was still missing.
- `ok` means verified by current effect, not by a marker alone. Markers and
  hashes may establish bundle identity, but share paths, task definitions,
  service state, ACL boundaries and runtime freshness are probed directly.
- An agent-only mismatch produces only `update-agent`: it does not request the
  secret, reset `syncshare`, rerun data-share setup, or perform the boundary
  repair, and the inspection's own boundary scan stays read-only. A
  non-credential data-share drift with an existing account uses script 03's
  preserve-credential mode.
- Each component gets at most one repair pass per run. After one or more repairs,
  the `verifying` pass re-evaluates every check; residual drift becomes a
  terminal, specifically classified error naming the protected manual fallback,
  never an automatic repair loop. If no repair was selected, the already-current
  initial checklist is sufficient and the duplicate pass is elided.
- `shareCredential` may still be `unverifiable` at `done`. The GUI must say
  whether it was **reset this run** or **preserved**, and must never show a
  green claim that Windows revealed or validated it. The existing authenticated
  **Check setup and connect** step remains the end-to-end proof.
- That proof only counts if its verdict reaches the app. The host learns the
  credential was rejected from the kernel's CIFS error, which lands in the
  `.mount` units' journal rather than in the power helper's own output, so the
  helper quotes a filtered excerpt of it when its readiness wait expires (D45).
  Without that, a wrong password and a slow guest produce the same sentence and
  the **Retry and reset share password…** route can never be offered. The
  matching stays wording-based and one-directional: naming an authentication
  failure offers a reset, and anything else — a timeout, an unexported share, an
  unreachable server — must keep the generic failure and leave a working
  password alone.
- Guest-writable status can make the GUI display a false warning, so the host
  validates it defensively (§4.1). It cannot cause elevated execution: the
  protected orchestrator owns the probes, the dependency graph, the fixed paths
  and the repair dispatch.

Explicit strange-state policy:

- A missing `exclusions.json` alongside any existing bridge marker (the §4 step 6
  fail-closed condition), an unexpected file at the sync-root path, a traversal
  link that could redirect an elevated walk, a protected child DACL, an
  unparseable task/share/account object, or a failure to enumerate a security
  boundary is `blocked`. Preserve the data and show the exact diagnosis; do not
  reinterpret any of it as a fresh install.
- A missing package, account, share, task or agent file is ordinary `missing`. A
  known object at the wrong fixed path or with wrong fixed properties is
  `drifted` **only** where the table above names a non-destructive repair.
- Stale, malformed, mismatched-run or unknown-version status never alters this
  classification. That is a host/watcher communication condition, handled by
  D43 — not evidence that a guest component is absent.

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
ConditionPathExists=!/var/lib/icloud-bridge/powered-off

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
`Where=/mnt/icloud_bridge`. It carries the same `ConditionPathExists` gate (D29).

`host/setup-host.sh` changes: also `mkdir -p /mnt/icloud_bridge`, install both new
units, `systemctl enable --now mnt-icloud_bridge.automount`. Same credentials file
(the `syncshare` user has access to both shares).

### 5.1 GUI-managed lifecycle: helper, marker, and sudoers (D29)

The durable desired-off state is `/var/lib/icloud-bridge/powered-off`: readable,
root-only writable, meaning "host bridge services must remain disarmed" (during
an interrupted transition the VM itself may still be running). `setup-host.sh`
creates the directory `root:root` 0755 but **never the marker**; the helper owns
its lifecycle. All six units — both `.mount`, both `.automount`,
`icloud-health.service`, and `icloud-health.timer` — carry
`ConditionPathExists=!/var/lib/icloud-bridge/powered-off` in `[Unit]`. Gating the
timer *and* its service closes the race where a timer job was already queued when
shutdown began; gating the mounts as well as the automounts holds the invariant
even if some other unit starts a mount directly. Conditions are checked on start
and do not stop active units, so the helper stops them explicitly and the units
stay enabled (an idempotent `setup-host.sh` rerun does not undo an intentional
off state).

`host/icloud-bridge-power` (installed `root:root` 0755 at
`/usr/local/bin/icloud-bridge-power`) is the single privileged helper: `set
-euo pipefail`, a fixed trusted `PATH`, `id -u` root check, `on`/`off` only, a
bounded `flock` under `/run/lock` serializing every transition, `==> ` progress,
and distinct exit codes for usage / busy / container / readiness / unit / lock /
ambiguous. It never uses `umount -l`, `umount -f`, `docker kill`, or `systemctl
disable`. It exports `DOCKER_HOST=unix:///var/run/docker.sock` alongside its fixed
`PATH`, and classifies a failed `docker inspect` through the pure, separately
testable `classify_inspect_output`, which lowercases before matching "no such
object"/"no such container" (both per §6.2 **Docker targeting**).

- **off**: create the marker (recording whether it pre-existed); stop the health
  timer then service; stop both automounts; stop both mounts while the guest SMB
  server is still live. A mount that will not stop (open file, a shell `cwd`
  inside it, an active copy) aborts before the container is touched, rolls back to
  the entry desired state, and reports files in use — never a lazy/forced unmount.
  Then `docker stop --timeout 130 icloud-windows` (a missing/stopped container is
  already success). If stop errors, the container's *actual* final state decides:
  stopped is success, running rolls back to on, unknown keeps everything disarmed
  and fails.
- **on**: a missing container fails without disturbing the marker (creation stays
  the explicit `docker compose up -d` step). Otherwise start the container if
  needed, then for up to five minutes retry a **real** CIFS activation — clear
  failed state, drop the marker, start both automounts, and require a bounded
  **directory read** (`ls -A`) of each mount point to *succeed*, plus the
  `.mount` units active plus `mountpoint` — because a published-port TCP connect
  is not SMB readiness. The read must succeed rather than merely trigger the
  automount (D30): after a manual `docker stop` the previous mount is often still
  active and still a mountpoint while its server is gone, and a `ls -d` of the
  directory entry can answer from cache, which would declare readiness and arm
  the health timer against a dead share. Between failed attempts re-arm the
  marker and tear down partial jobs — which is also how a stale mount is cleanly
  (never lazily) removed before the next attempt. On success the marker is
  absent and the health timer starts; on timeout the VM is left running for
  inspection and the timer is never armed against an unverified mount, and the
  failure message carries a filtered, secret-free excerpt of the two `.mount`
  units' journal so the app can tell a rejected credential from a slow guest
  (D45).

`setup-host.sh` resolves the operator as `TARGET_USER="${SUDO_USER:-${TARGET_USER:-}}"`,
failing if that is empty or root, validating with `id`, and deriving the mount
UID/GID from it (deliberate `MOUNT_UID`/`MOUNT_GID` overrides still win). It
installs a `root:root` 0440 `/etc/sudoers.d/icloud-bridge` granting exactly:

```sudoers
alice ALL=(root) NOPASSWD: /usr/local/bin/icloud-bridge-power on, /usr/local/bin/icloud-bridge-power off
```

The `on`/`off` arguments are part of the command specs because a bare path would
permit arbitrary arguments (sudoers matches arguments exactly). The render is
validated with `visudo -cf`, installed atomically, and the whole policy
re-validated with `visudo -c`; a valid installed policy is never replaced with an
invalid render. Helper, marker directory, units, and sudoers are all installed
before `daemon-reload`/`enable --now`.

---


**The `==> ` lines are presentation only (D38).** The GUI streams the helper's
stdout live and shows the most recent `==> ` line beside an elapsed clock. It
matches the four-character prefix and nothing else: the human wording after it
may change freely, and no control decision may ever be derived from it. Lines
are bounded and sanitized before they reach a widget. The helper announces the
marker, health, automount, mount and container phases plus the single overall
readiness wait — deliberately **not** one line per ten-second readiness attempt,
which would flood the surface with noise. Adding a phase line here means
updating this paragraph in the same commit.

## 6. Host GUI — `gui/`

### 6.1 Layout

```
gui/
├── icloud_bridge_gui/
│   ├── __init__.py
│   ├── __main__.py        # QApplication + tray + window wiring; single-instance lock
│   ├── cli.py             # Qt-free: argument parser and the --version string
│   ├── notify.py          # Qt-free: latching red-incident notification policy
│   ├── listing.py         # Qt-free: per-folder idle/loading/loaded request state
│   ├── filtering.py       # Qt-free: which tree rows a filter leaves visible
│   ├── sizes.py           # Qt-free: the honest excluded-space aggregation
│   ├── firstrun.py        # Qt-free, no mount I/O: readiness checks, resource
│   │                      #   resolution, compose argv, host-setup verification (D31)
│   ├── envfile.py         # Qt-free, no I/O but an injected reader: the one
│   │                      #   SHARE_PASS grammar its three readers share (D41)
│   ├── guestprov.py       # Qt-free, no mount I/O: host half of the provisioning
│   │                      #   channel — share, staging, secret, status (D40-D44)
│   ├── health.py          # host-side checks (no bridge): container/mount/canary
│   ├── bridge.py          # bridge share I/O: read status/tree, write exclusions,
│   │                      #   list-request round-trip; worker-thread I/O
│   ├── power.py           # Qt-free: docker inspect + marker read, startup plan,
│   │                      #   and `sudo -n icloud-bridge-power on|off` (D29)
│   ├── lifecycle.py       # Qt-free, no I/O of any kind: the D29-D31 state
│   │                      #   machine as a pure reducer (model, event) -> effects
│   ├── autostart.py       # Qt-free: read/toggle the XDG autostart entry (D29)
│   ├── backup.py          # Qt-free, never CIFS: the local exclusions snapshot,
│   │                      #   its revision-monotonicity and restore preview (D36)
│   ├── diagnostics.py     # Qt-free, no mount I/O: the allowlisted, redacted
│   │                      #   support report over `Facts` (D37)
│   ├── tray.py            # QSystemTrayIcon: icon state (D23), menu, autostart item
│   ├── window.py          # QMainWindow with 2 tabs: Status, Selective Sync
│   └── icons/             # icloud-green.svg, icloud-yellow.svg, icloud-red.svg,
│                          #   icloud-starting.svg (blue disc, the D29 transition)
├── icloud-bridge-gui.desktop        # launches the app (window)
├── autostart/icloud-bridge-tray.desktop  # launches the app minimized to tray
└── install-gui.sh
```

### 6.2 Behavior — exact spec

**Health model (`health.py`)** — every check returns
`(severity: green|yellow|red, detail: str)`:

- container: `docker inspect -f '{{.State.Running}}' icloud-windows` == `true`
  (subprocess, 5 s timeout), run with `DOCKER_HOST=unix:///var/run/docker.sock`
  — see **Docker targeting** below.
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

**Docker targeting.** Every unprivileged `docker` call the GUI makes — the health
container check, `power.inspect_container()`, and the first-run/Compose flow —
sets `DOCKER_HOST=unix:///var/run/docker.sock`. The bridge's container only ever
exists on the native Engine, but Docker Desktop can leave the desktop user's
active *context* on `desktop-linux`, whose daemon has never heard of
`icloud-windows`; unpinned, a running VM then reports "no such object" and the
GUI concludes it was never created. The override is applied as a copy of
`os.environ` with that one key replaced — never a wholesale environment
replacement, and never on `sudo` or unrelated helpers, which have their own
reasons to keep the session environment. The root helper sets the same variable
for consistency and future-proofing only: `sudo` already resets the environment,
so the helper reads root's Docker configuration, and switching the desktop user's
context does **not** reproduce a failure in its unpinned form.

**No-such-container matching is case-insensitive everywhere** — `power.py` and
`host/icloud-bridge-power` both lowercase before matching. The CLI changed the
message's casing between major versions (`Error: No such object:` up to 28,
`error: no such object:` on 29); a literal match demotes a first-run host to
`inspect_error` in the GUI and breaks the helper's documented "a missing
container is already off" idempotency of `off`.

Overall state per D23. Refresh every 5 s, with at most one refresh in flight.
Run **all** subprocess and filesystem operations—including `ismount`, CIFS
`stat`/JSON reads, and request/response polling—in a `QThreadPool` worker (or
`QProcess` for commands), because a sick CIFS mount can block metadata calls.
Return results to the GUI thread by signals; never touch widgets from a worker.

**Tray (`tray.py`):**

- Icon = colored SVG per overall state; tooltip = one line per failing check, or
  "iCloud bridge: healthy".
- Left-click and menu item "Open status window" → show/raise the window.
- **Health notifications (`notify.py`, Qt-free).** A colour change is silent, so
  a desktop notification marks the edges of a fault. The policy is a pure
  latching reducer: `green`/`yellow`/nothing → **red** notifies once (body = the
  first red check's name and detail) and latches the incident; further red or
  yellow snapshots say nothing; a latched incident reaching **green** notifies
  once and clears the latch. Yellow neither opens nor closes an incident. A
  gather exception, represented by the synthetic red `GUI` check, takes the same
  path. `TrayIcon.notify` selects Warning for failure and **Information** for
  recovery — a recovery must not carry a fault icon. Without a tray the window
  is the surface.
  Only normal monitoring feeds the reducer: the starting, shutting-down,
  provisioning and intentionally-powered-off states reset it and stop feeding
  it, so an expected red never announces itself. After a successful `power_on`
  a **bounded** startup grace (two minutes) suppresses the expected stale
  canary without latching, so a bridge that is really broken still notifies once
  the grace expires. The grace is specific to that transition — an
  already-running bridge whose first snapshot is red, including a minimized
  launch, notifies immediately.
- Menu: **Open iCloud folder** (`xdg-open /mnt/icloud`), **Open status window**,
  **Open VM screen** (`xdg-open http://127.0.0.1:8006`), the single D30 power
  action for the current state (**Retry start** / **Power off bridge (keep this
  app running)** / **Start bridge**, at most one visible), **Start when the
  computer starts**, separator, **Quit**.
- Mount-touching items are gated by *two* independent conditions: a transition
  is in progress (`set_lifecycle_busy`), or the bridge is intentionally down
  (`set_bridge_available`). An idle powered-off bridge is not busy — Quit,
  autostart and Start bridge stay usable — but **Open iCloud folder** would open
  a bare mount point, so it is disabled.
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
tray menu. `Version <__version__>` sits at the bottom as selectable text.

**Version.** `icloud_bridge_gui/__init__.py::__version__` is the single version
source; the `Makefile` and `packaging/build-deb.sh` derive the package version
from it, and no second packaging version is introduced to "keep in step". The
parser lives in the Qt-free `cli.py` so `--version` — which prints exactly
`icloud-bridge-gui <version>` — is testable in the no-Qt suite. `main()` parses
before it claims the single-instance socket or constructs `QApplication`, so
asking the binary for its version never disturbs a running tray instance.

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
  do not materialize an unbounded single-folder listing at once. That `QTimer`
  is armed by a dispatched request and stopped again once none is outstanding —
  the cadence while a listing is in flight is unchanged, and a window that is
  merely open polls nothing.
- **Listing state (`listing.py`, Qt-free).** Each folder carries an explicit
  `idle` / `loading` / `loaded` state rather than a "requested" marker, because
  a marker set before dispatch leaves a failed folder permanently empty with no
  way to retry. Expansion moves `idle → loading` only when the request is
  accepted for dispatch; another expansion while `loading` queues nothing; only
  a successful first-page response — **including a valid empty one** — reaches
  `loaded`. Paused I/O, dispatch failure, guest error, malformed response,
  cancellation, timeout, and a response that arrives after a Reload all return
  the folder to `idle`, which is exactly the state that permits a retry.
  Requests are tagged with the current tree generation so a completion from a
  prior Reload cannot mutate the rebuilt tree.
- **Load more…** is styled link-like (underlined, link colour) and activates on
  a single click, Enter/Space, or a double click; the handler is idempotent
  because one gesture can emit several of those signals. While its request is in
  flight the row shows *Loading…* and is disabled; every failure restores the
  same offset in place so the operator can retry, and success replaces it with
  the fetched page plus a fresh continuation row if there is more.
- **Filter (`filtering.py`, Qt-free).** A filter field above the tree matches
  folder names and relative folder paths case-insensitively with the same
  normalization as `bridge.is_under`, showing each match and its ancestor chain
  and hiding unrelated branches. It searches the folder snapshot plus the files
  already loaded in this session, and says so — unloaded files were not
  searched. Missing configured items participate by their full relative path.
  Filtering changes nothing but visibility: not `_wanted`, not check states, not
  selection semantics, not the in-memory tree. Ancestor expansion is performed
  with the expansion handler suppressed so it fires no list requests, and the
  operator's own expanded/collapsed state is saved when a filter starts and
  restored when it is cleared.
- **Excluded-space summary (`sizes.py`, Qt-free)** sits under the introduction
  and is recomputed whenever `_wanted`, a tree/list response, `status.json`, or
  a Reload changes something: *"Excluded: 3 roots, about 42 GB logical (1 size
  unknown)"*. It is never called disk space saved and never claims the content
  is already online-only — `logicalBytes` is logical content size and
  dehydration is asynchronous, so an exclusion can sit at `pending-dehydrate`.
  Sizes come from `tree.json` (folders, recursive), this session's list
  responses (files), and `status.json.exclusions[].logicalBytes` for an exact,
  **still-configured** root; anything else is reported as an unknown count
  rather than silently counted as zero. Each canonical root is summed once —
  D19's antichain prevents legitimate parent/child double counting, and the
  aggregator re-derives it defensively so malformed input cannot inflate the
  total. A short note says exclusions are hidden from Linux and requested
  online-only, with reclamation reported separately on the Status tab.
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

**Bridge lifecycle (`power.py`, `autostart.py`, and the controller) — D29:**

- `power.py` is Qt-free and does no mount I/O. `inspect_container()` classifies
  `docker inspect` output (5 s timeout) into `absent` / `stopped` / `running` /
  `error`, on the pinned native socket and with the case-insensitive
  no-such-container match described under **Docker targeting** above.
  `marker_exists()` reads `/var/lib/icloud-bridge/powered-off` through an
  injectable path; `plan_startup(marker, status)` returns one of **power_on**
  (marker set, or a cleanly stopped container — including a running container that
  is still marked off, which `on` reconciles), **already_on**, **provision_needed**
  (no container), or **inspect_error**. `provision_needed` and `inspect_error`
  both enter the D31 **Setup required** state: red *presentation* — a tray colour
  and the assistant's own diagnostics — but **no health rows gathered from the
  mounts**, because there is no evidence a mount exists and touching a dead CIFS
  handle blocks the worker for the whole timeout. `icloud-bridge-power on` still
  never manufactures a missing container; D31's confirmed **Create Windows VM**
  is a separate, explicit user action. `power_on()`/`power_off()` run the
  exact argv `sudo -n /usr/local/bin/icloud-bridge-power on|off` — never a shell —
  and return the helper's own stderr for display.
- **Startup precedes all CIFS I/O.** The controller pauses bridge I/O, runs the
  inspection in a worker, and only then acts. **power_on** shows a dedicated
  **Starting Windows VM…** state — the fourth `icloud-starting.svg` tray icon,
  distinct from the three health colours because a multi-minute boot must not read
  as the yellow "degraded" fault — disables the file/selective-sync actions, keeps
  **Open VM screen**, and runs `power_on()` asynchronously. On success it enables
  bridge I/O, loads selective-sync, starts the 5 s refresh, and gathers. On failure
  it does **not** retry every five seconds: it shows the helper error, keeps **Open
  VM screen** and **Retry start**, and leaves mount work paused (a minimized launch
  uses a tray notification, not an invisible modal).
- **Quit** presents a three-way confirmation: **Quit and power off VM** (default),
  **Quit GUI only (leave bridge running)**, **Cancel**. The message says quitting
  stops syncing and disconnects `/mnt/icloud`, that unuploaded changes resume next
  start, and does **not** claim the Apple upload queue is empty (v1 §9). Power-off
  stops the 5 s timer and the request/response poller, rejects new reads/writes/
  Apply/list, waits asynchronously for in-flight mount-touching tasks to drain
  within a bound slightly longer than the CIFS 30 s timeout (aborting with an
  "operation still in progress" message rather than a lazy unmount, and never while
  an Apply write is in flight), then runs `power_off()` behind a non-cancellable
  **Shutting down… about three minutes** progress state. It exits only on helper
  success; on failure it shows the error and keeps running.
- **First-run assistant (`firstrun.py`, Qt-free and mount-I/O-free) — D31.**
  A **Setup** tab appears in front of the others while the lifecycle is `setup`
  or `provisioning`; health polling, selective sync and every bridge read stay
  stopped for the whole of both. Two stages, because one checklist cannot both
  require a container and offer to create it:

  1. **Read-only readiness.** `/dev/kvm` and `/dev/net/tun` exist and are usable;
     the native socket `unix:///var/run/docker.sock` answers *this desktop
     session* (a `docker` group entry that has not taken effect in this session
     is called out by name, since it looks identical to not being a member); the
     Compose plugin is present; an active `desktop-linux` context is a **warning
     only**, because every command this app runs is socket-pinned; one complete
     resource bundle is readable; the operator's env file parses, has non-empty
     `DISK_SIZE`/`RAM_SIZE`/`CPU_CORES`/`SHARE_PASS`, and its password is not the
     placeholder; and the container is absent (the *expected* pre-create state,
     not a failure), running, or stopped. Each failure carries a copyable command
     from SETUP.md — the GUI itself never installs packages, changes groups, or
     runs `icloud-bridge-configure` or any other sudo command.
  2. **Creation and handoff.** **Create Windows VM** appears only with the
     container absent and no failing check, confirms the multi-gigabyte download,
     the 20–40 minute install and a long-lived VM, and runs the argv above in a
     worker with a bounded
     diagnostic. Success enters **Provisioning Windows**, which presents the
     manual in-guest sequence and the host command matching this install
     (`sudo icloud-bridge-configure --user … --env-file …` for a package,
     `sudo ./host/setup-host.sh` from the recorded checkout for a source
     install). **Check setup and connect** verifies the helper, both
     argument-exact `sudo -n -l` grants, the installed units and
     `/etc/icloud-bridge/config`, and the Docker state — none of which touches a
     mount — and then calls `power_on()`, whose real CIFS activation is the only
     honest mountability test. On failure it stays in the assistant with the
     error and the VM link.

  **Resource resolution is deterministic and never uses the working directory:**
  the `ICLOUD_BRIDGE_RESOURCES` test/development override, then a source checkout
  relative to `__file__`, then `~/.local/share/icloud-bridge-gui/resources`, then
  `/usr/share/icloud-bridge`. The chosen paths are shown in the assistant. Both
  installed bundles carry the env example as `env.example`; the resolver also
  accepts the source tree's `.env.example`. The per-user installer records the
  checkout it ran from in `resources/source-checkout`, and the assistant warns if
  that checkout no longer contains `host/setup-host.sh`.
- **Progress and interrupted transactions (D38).** Each busy surface — the
  starting banner, the shutting-down banner, Create VM, and the
  provisioning wait — shows elapsed time from a Qt timer ("Starting the Windows
  VM… 2 m 10 s") plus the most recent phase the child printed. No percentage is
  ever shown, there is no cancel control, and no existing timeout changes. If
  the *outer* subprocess timeout fires, the GUI does **not** take the ordinary
  failure path: it enters `transition_unknown`, keeps every kind of bridge I/O
  paused, drops its cached documents and Docker classification, and offers only
  **Retry**, which repeats the same desired direction. A helper that answered —
  even with a failure — is not unknown and still takes the ordinary path.
- **Resuming an interrupted first run (D39).** The record written before Compose
  is consulted at the next launch alongside Docker, before any CIFS access, so
  an app that was closed mid-install returns to **Provisioning Windows** with its
  original elapsed time rather than trying to mount a half-built guest. Its four
  outcomes — matching, container gone, different container, malformed — are all
  no-CIFS, and only a successful **Check setup and connect** clears it.
- **Exporting a diagnostic report (D37).** The Status tab's **Copy
  diagnostics** and **Save diagnostic report…** are enabled in every lifecycle
  state — a report is most valuable when something is broken. Collection runs on
  a worker (it spawns `systemctl` and `sudo -n -l`, though it touches no mount),
  and only the explicit Copy action reaches the clipboard. In the no-CIFS states
  the bridge facts come from the last successful gather and every such section
  is labelled with its timestamp, or "not gathered" when there has never been
  one; the host-unit and authorization probes still run, because they are safe
  everywhere. Folder names are placeholders unless **Include folder names** is
  ticked; nothing else is ever revealed by that tick.
- **Backing up and restoring the choices (D36).** After every validated
  `exclusions.json` read and every successful Apply, the window writes a
  mode-0600 snapshot to the desktop user's XDG state directory, on the same
  worker thread as the operation that produced the data. It is local disk, never
  CIFS, and it is a **second result**: a read that loaded fine or an Apply that
  wrote fine stays a success when only the snapshot failed, and the Selective
  Sync tab shows a persistent yellow "not backed up" warning rather than the
  Apply failure dialog. An automatic read never replaces a *higher* revision —
  that is a rebuilt VM's fresh empty config, and overwriting there would destroy
  the copy recovery needs — and the same revision with different content is
  kept and reported as a conflict. **Restore from backup…** is explicit and
  previewed: enabled only in normal monitoring with a loaded snapshot, no staged
  unapplied selection and a compatible protocol; a dirty selection prompts to
  Apply or Reload first rather than being discarded. The preview lists what
  would be excluded and re-included with the same warnings Apply uses, and the
  write goes through the ordinary `write_exclusions` path at a revision strictly
  above everything observed, including the backup's own.
- **Version skew and the write gate (D35).** Every snapshot carries a
  compatibility classification derived from the bridge documents' `version` and
  `status.agentBuild`: `current`, `skewed`, `incompatible`, or `unknown`. Only
  `current` and `skewed` open the write gate. `skewed` changes nothing else — the
  protocol matched, so Apply, Restore and browsing all keep working — but a
  persistent yellow banner says the guest agent does not match this app and
  offers the confirmed **Re-run Windows provisioning…** action (D35/D40-D44) —
  the same controller action the Status tab and menu invoke, never a `C:\OEM`
  instruction, and available in the `incompatible` state too. `incompatible`
  shows a red diagnostic instead and one central gate disables Apply and Restore,
  refuses every list-request dispatch, and leaves `exclusions.json` exactly as it
  is; a document with an unsupported version is not fed into normal health or
  tree rendering merely because its JSON parsed. `unknown` — no status document
  yet — shows no banner (the Guest agent health row already reports it) but keeps
  the gate closed, so a transient missing status can never make the GUI guess
  which agent is running. Powering the bridge off returns the classification to
  `unknown`, because it describes an agent that is no longer reachable.
- **In-session power control (D30).** The controller holds one lifecycle state —
  `starting`, `running`, `start_failed`, `powered_off`, `shutting_down`,
  `setup`, `provisioning`. That state machine is implemented as a **pure reducer**
  in `lifecycle.py`: `reduce(model, event) -> Transition` returns the next model
  plus an ordered tuple of effect tokens, and the controller does nothing but
  translate a signal into an event, reduce, and apply the effects in order. Every
  valid transition advances an operation token, so a worker completion belonging
  to a superseded operation is dropped before it is reduced; an unexpected
  (phase, event) pair mutates nothing and reports itself. This is where the "no
  CIFS until the helper says both shares are live" rule is actually enforced, and
  `gui/tests/test_lifecycle.py` asserts the whole table without Qt.
  `power.available_action(lifecycle, container)` maps that state, together
  with the last **definitive** `docker inspect` classification, to the single
  action offered as a tray item and one Status-tab button:
  - `running` + container `running` → **Power off bridge (keep this app running)**;
  - `running` + container `stopped` (`exited`/`created`/`dead`) → **Start bridge**,
    the in-app recovery for a container stopped by hand;
  - `powered_off` → **Start bridge**;
  - `start_failed` → **Retry start**, with D29's existing wording and diagnostics;
  - `setup` + container `absent` → the first-run assistant, never Start;
  - an inspect error, an unrecognized state, or any transition → **no mutating
    action at all**.

  An ordinary red/yellow/green health result never changes the action; when a
  snapshot is not green the controller re-runs the Docker-only inspection (no
  mount I/O, safe in every state) and the action follows *that*.

  Power-off asks for confirmation carrying the same upload-queue caveat as Quit,
  then runs the **same transaction** `_begin_power_off` as Quit — stop the 5 s
  timer and the request/response poller, refuse new reads/writes/Apply/list,
  drain in-flight mount work within the bounded gate, then `power_off()` — and
  differs only in the continuation: `then_exit=False` enters the idle state
  instead of quitting. The ordering is never duplicated for the second caller.
  On helper failure or a busy drain, both callers restore the exact running
  state, polling included.

  The idle `powered_off` state does **not** exit: it clears the stale health
  rows, shows a distinct grey **Bridge is powered off** banner and
  `icloud-off.svg` tray icon (grey, because an intentional off state is not a
  fault), keeps every mount-touching control disabled, and stops health polling
  entirely. Start pauses all new I/O, then reuses the D29 `power_on` path in
  full: on success it restarts the controller's health timer and lets the
  window's request/response poller run again — it re-arms on the next list
  request, since the quiesce dropped the ones it had — reloads selective sync
  and gathers a fresh snapshot; on failure it stays paused on the existing
  Retry/Open VM screen surface.
  The idle state lasts until **Start bridge** or process exit. Plain
  Quit while off leaves the durable marker and the stopped VM alone and never
  calls the helper again; a later process start still power-ons automatically
  per D29.
- **Close vs Quit.** With a tray, `closeEvent` hides with no prompt. Without a
  tray, close emits a quit request routed through the same confirmation, and
  `QuitOnLastWindowClosed` is off so it cannot bypass the controller. OS logout,
  signals, crashes and `aboutToQuit` never power off the bridge.
- **Autostart.** A checkable **Start when the computer starts** item sits after
  **Open VM screen** and before the Quit separator. `autostart.py` (Qt-free)
  toggles `Hidden=`/`X-GNOME-Autostart-enabled=` in
  `~/.config/autostart/icloud-bridge-tray.desktop` rather than deleting it, so the
  installer's absolute `Exec` survives, and recreates the file from the launcher
  path if missing. The checkbox reflects the file on menu open; absent or
  `Hidden=true` reads as unchecked.

The privileged half of D29 — the `icloud-bridge-power` helper, the marker, the
unit `ConditionPathExists` gates, and the `sudoers` grant — is specified with the
host side in §5.1 and installed by `host/setup-host.sh`.

### 6.3 `install-gui.sh` (run as the desktop user, not root)

1. Copy `icloud_bridge_gui/` to `~/.local/share/icloud-bridge-gui/` and install a
   launcher at `~/.local/bin/icloud-bridge-gui`. Also copy the D31 resource
   bundle — `docker-compose.yml`, the whole `provision/` directory, and
   `.env.example` **renamed to `env.example`** — into
   `~/.local/share/icloud-bridge-gui/resources/`, matching the package's
   `/usr/share/icloud-bridge` layout, and record the source checkout in
   `resources/source-checkout`. The operator's real `.env` is never copied: it
   holds the share password and stays where it is.
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

Apply executable-file and plan edits together: the `CONTRIBUTING.md` sync rule
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
   second one — the previously recorded duplicate was already collapsed in the
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
7. Contributor guidance: delete **only** the `README.md` two-`## Status`-
   sections inconsistency, which is resolved. Keep the second inconsistency — the
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
- [ ] **B5** (D29) Add the marker `ConditionPathExists` to all six units, the
  `host/icloud-bridge-power` helper (§5.1), and the `setup-host.sh`
  target-user/marker-dir/helper/sudoers install. Extend
  `host/acceptance-tests.sh` with the installed ownership/mode, unit-condition,
  marker-dir, and non-mutating `sudo -n -l /usr/local/bin/icloud-bridge-power
  on`/`off` checks. Done-when: `sudo ./host/setup-host.sh` installs a
  `visudo`-clean policy; acceptance passes with the bridge on; `icloud-bridge-power
  off` then `on` round-trips on a live host.

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
- [ ] **C7** (D29) Add Qt-free `power.py` and `autostart.py` with
  `test_power.py`/`test_autostart.py` (fake runner, injected marker, tmpdir home;
  no Qt/docker/sudo import). Wire the controller so power inspection precedes all
  CIFS I/O, add the `icloud-starting.svg` transition icon, worker-drain quiescing,
  the three-way Quit confirmation with progress/error/Retry surfaces, the no-tray
  close routing, and the autostart checkbox; make `install-gui.sh` preserve an
  existing `Hidden=true`. Note the `run_async` fix: keep a reference to each
  `_Task` until its signal fires, or the queued completion callback is dropped.
  Done-when: `pytest gui/tests` passes with **and** without PySide6 installed.

### Phase D — docs

- [ ] **D1** Apply every §7 edit, including the embedded copies in
  `docs/implementation-plan.md` and the README status cleanup.
- [ ] **D2** Add a short `docs/selective-sync.md` user page: what exclusion
  does/doesn't do, `not-found`, asynchronous reclamation, the rename limitation,
  permission-denied read/write/delete/rename/collision behavior, and how to
  re-include. Include E0 and E1–E7 as the deployment checklist.

### Pre-deployment repository verification

- [ ] Run `bash -n host/*.sh host/icloud-bridge-power` and `docker compose config`
  exactly as required by `CONTRIBUTING.md`. Run `pytest gui/tests` in the GUI
  development environment.
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
- [ ] **E8** (D29) **Clean Quit/relaunch:** with both mounts active, choose **Quit
  and power off VM**. Both mounts/automounts and the health service/timer go
  inactive, the marker appears, the container stops, and `ls /mnt/icloud` returns
  promptly on the empty mount-point; the journal shows no intentional-off
  failures. Relaunch: a transitional state shows without early CIFS access, the VM
  boots, both mounts activate, the marker disappears, the timer starts, and health
  goes green. **Quit GUI only** exits immediately leaving everything up.
- [ ] **E9** (D29) **Busy refusal:** repeat E8's power-off with (a) an open file,
  (b) a shell `cwd` under the mount, and (c) a large host write in flight.
  Shutdown aborts, the VM stays running, nothing is lazily detached; after
  releasing the holder, retry succeeds.
- [ ] **E10** (D29) **Reboot semantics + failure paths:** power off through the
  GUI, reboot without logging in → container, automounts, and health timer stay
  off with no FAIL spam; log in → autostart restores the bridge. Separately leave
  the bridge on and reboot → existing auto-recovery still works. Then deny helper
  authorization, and test a missing container and an SMB-readiness timeout: the
  GUI stays responsive, performs no repeated automatic retries, and never arms the
  timer against a dead mount. Confirm the installed dockur image received SIGTERM
  and completed ACPI shutdown rather than reaching its force-kill fallback.
- [ ] **E11d** (D31) **First run on a clean host, both install routes:** on a
  KVM host with no `icloud-windows` container, launch the GUI from a per-user
  install and again from the `.deb`. Each time: the Setup tab appears, no CIFS
  access happens at all (confirm nothing touches `/mnt/icloud*`; health polling
  and selective sync stay stopped), the resolved compose/provision paths name the
  right bundle for that install, and a deliberately broken env file (missing key,
  placeholder `SHARE_PASS`) blocks **Create Windows VM**. Fix it, create the VM,
  and confirm the initial Windows install — longer than the helper's five-minute
  readiness deadline — leaves the GUI in **Provisioning Windows** without a start
  error. Run the manual guest sequence and the host command the assistant printed,
  then **Check setup and connect**: with a missing sudo grant it must report that
  and stay put; once configured it must power on and reach normal monitoring.
  Also confirm **Create Windows VM** is unavailable whenever a container of that
  name already exists, and that the share password appears nowhere in the UI,
  the process argv, the resource bundle, or the clipboard.
- [ ] **E11b** (D30) **Power off and start again without quitting:** with both
  mounts active, choose **Power off bridge (keep this app running)**. The same
  teardown as E8 happens — units inactive, marker present, container stopped —
  but the app keeps running, shows the grey powered-off icon/banner, clears the
  health rows, disables every mount-touching control, and stops polling. Confirm
  no health FAIL spam and no CIFS access while off. **Start bridge** brings
  everything back: VM boots, both mounts activate, the marker disappears, health
  polling resumes, expanding a folder still returns its listing (which is what
  proves the request/response poller runs again), selective sync reloads,
  and health reaches green. Repeat the E9 busy matrix against **Power off
  bridge**: it must abort identically, leave the VM running, and restore polling.
  Then **Quit** while powered off — it must not invoke the helper again, and the
  bridge must still be off after the process exits.
- [ ] **E11c** (D30) **Recovery from a manual container stop:** with the bridge
  up, `docker stop icloud-windows` by hand. The GUI must go red, classify the
  container as stopped, and offer **Start bridge** (never merely because health
  is red). Press it: the helper must recover the host units and mounts left in
  that state and reach a live CIFS activation without any lazy or forced
  unmount. This is what the `ls -A` readiness probe is for — verify from the
  journal that readiness was not declared while the old mount was still stale.
- [ ] **E12** (D35) **Version skew and the fail-closed write gate:** with the
  bridge running and healthy, edit `C:\ProgramData\icloud-bridge\agent.ps1` in
  the guest to report a deliberately different `$AgentBuild`, let a status cycle
  pass, and confirm the GUI shows the yellow skew banner offering **Re-run
  Windows provisioning…** — with no `C:\OEM` instruction anywhere in the active
  recovery UI — **while Apply, Restore and folder browsing keep working**. Use
  that banner button (not a hand-run of script 04), confirm the proposed work is
  only `update-agent`, that no env-file chooser appears and no password reset is
  requested, and that the banner clears to `current` when it finishes. Then,
  using `ICLOUD_BRIDGE_DIR` and `ICLOUD_MOUNT_DIR` overrides against a
  throwaway fixture directory — never the real share — serve a `status.json` and
  a `tree.json` with an unsupported `version`, and confirm every write surface is
  disabled, no list request is dispatched, the real `exclusions.json` and the
  real mount are untouched throughout, and the **Re-run Windows provisioning…**
  action is still offered, since it is what the red diagnostic points at.
- [ ] **E13** (D36) **Backup and restore, on disposable paths only.** Prefer a
  disposable VM. On the production VM, simulate a reset **non-destructively**:
  close the app, and install a validated backup at
  `$XDG_STATE_HOME/icloud-bridge-gui/exclusions-backup.json` whose revision is
  deliberately higher than the current config's and whose paths name **only a
  disposable test folder**. Relaunch and confirm (a) the lower-revision
  automatic read does not overwrite that backup, (b) **Restore from backup…**
  previews exactly the expected additions/removals, and (c) after confirming,
  the test exclusion reaches `applied` and the written revision is above every
  previously observed one. Confirm the button is unavailable while a staged
  selection is unapplied. Never hand-edit the live guest config, and never
  destroy or re-provision the operator's production VM to run this case.
- [ ] **E14** (D37) **Diagnostic export in four states.** Export a report in
  normal monitoring, in the **Setup required** state, while powered off, and
  after a deliberately failed power helper (revoke the sudoers grant). Each time,
  read the whole report and confirm it is enough to follow the matching
  `SETUP.md` recovery path, and that it contains **no** share password,
  credentials-file content, command environment, file content, or folder name.
  Then tick **Include folder names**, export again, and confirm only the folder
  names changed. Save a report and check it is mode 0600.
- [ ] **E15** (D38/D39) **Real progress, and a restart mid-install.** Watch a
  real **Start bridge** and a real **Power off bridge**: confirm the elapsed
  clock ticks, the helper's `==> ` phases appear in order, and the wording
  matches what the helper actually printed. Create a VM and confirm Compose's
  own output appears elided to one row during the pull. Then **quit the app
  while Windows is still installing**, relaunch, and confirm it returns to
  **Provisioning Windows** showing the original elapsed time, with nothing
  touching `/mnt/icloud*`. Finish the guest sequence and **Check setup and
  connect**; confirm the record file is gone afterwards. Separately, with the VM
  absent, confirm **Discard failed setup record** appears, asks for
  confirmation, and removes only that file.
- [ ] **E11** (D29) **No-tray + autostart toggle:** with the tray unavailable, the
  window X presents the same three-way Quit dialog; with a tray it only hides.
  Untick **Start when the computer starts**, log out/in → the GUI does not launch
  and (if powered off) the bridge stays off; re-run `install-gui.sh` while
  unticked and confirm the choice survives. Tick it again, log out/in → the GUI
  starts minimized and restores the bridge.

---

## 8.1 Performance and resource posture (review of 2026-07-26)

A review of the whole path — guest OS, hypervisor, SMB transport, agent and GUI —
produced the changes locked as D32/D33/D34 plus the guest debloat additions in v1
plan §4. Everything below was examined in the same pass and **deliberately not
done**; record any future proposal against this list before re-opening it.

**Closed avenues.**

- **Windows LTSC/Enterprise (`VERSION: 11l` / `11e`).** dockur takes them as plain
  config values, so no custom ISO is involved — but LTSC ships without the
  Microsoft Store, and D4's locked install path is `winget --source msstore`.
  Making iCloud installable there means sideloading the Store and its AppX
  dependencies, i.e. touching exactly the stack hard rule 5/D3 forbids, and
  leaves iCloud without its update channel. Enterprise has the Store but is
  functionally identical to the already-debloated Pro for this workload.
- **QEMU/`ARGUMENTS` tuning for Hyper-V enlightenments.** Already done upstream:
  qemus/qemu applies `hv_passthrough` and `kvm-pit.lost_tick_policy=discard` for
  Windows guests, which is what cuts idle timer-tick exits. Verify with
  `docker exec icloud-windows ps aux | grep qemu`; do not add `ARGUMENTS`.
- **`DISK_CACHE=writeback`.** Trades crash-consistency of the NTFS sync root for
  throughput this loopback workload does not need.
- **`ALLOCATE=Y` / disk preallocation.** Would consume the whole `DISK_SIZE`
  immediately for no benefit; sparse growth plus `discard=unmap` is the shape
  D25/D26 assumes.
- **`mem_limit` on the container.** A cgroup OOM kill of QEMU is precisely the
  unclean, non-explicit guest poweroff the D29/D30 lifecycle exists to prevent,
  and it would bypass the marker and teardown ordering entirely. The protection
  it offers against a hypothetical QEMU leak does not pay for that new kill path.
- **virtio-balloon give-back.** dockur allocates the full `RAM_SIZE` by default;
  ballooning is opt-in, so `docker compose pull` alone changes nothing, and
  enabling it is an unvalidated compose change on this install.
- **hugetlbfs (`-mem-path`).** Permanently pins the guest's RAM on the host and
  needs the `ARGUMENTS` surgery this design avoids. THP in `madvise` (the distro
  default) already gives QEMU 2 MB mappings.
- **SMB multichannel (`max_channels`).** The guest has one virtio NIC behind
  dockur's NAT and advertises an address the host cannot reach; extra channels
  would multiplex the same path, whose cost is per-byte copying.
- **`fsutil behavior set disablelastaccess 1`.** Would silently degrade the D26
  sweep's LRU ordering to `LastWriteTime`; the agent checks that very setting.
- **Disabling `ScheduledDefrag`.** On an SSD-presented volume that task performs
  retrim, which is what hands blocks freed by the D26 sweep back to the sparse
  qcow2 image. Keeping it is a storage win, not a cost.
- **Disabling `TabletInputService`/`TextInputManagementService`.** On Windows 11
  this breaks keyboard entry into Start, Settings and UWP apps — and iCloud is a
  Store app whose sign-in the operator must type into.

**Deferred, pending evidence this workspace cannot produce.**

- **Replacing the sweep's per-file `attrib.exe` spawn with `SetFileAttributesW`.**
  Factually sound — `attrib` is a thin wrapper over `Get`/`SetFileAttributes`, the
  script already declares both attribute constants, and thousands of process
  creations per pass would become microsecond calls. But D14/D26 and hard rule 6
  pin the mechanism *as* `attrib +U -P`, so this is a mechanism substitution that
  needs an explicit plan amendment, plus a §0.5-style live verification that
  Cloud Files really dehydrates on the native call. D5 is the standing reminder
  of what happens when a plausible assumption is not tested against the guest.
- **Gating the ten-minute walk on a `FileSystemWatcher` dirty flag.** Feasible
  inside the existing process (D17 intact), but a Cloud Files root generates
  watcher events from the sync engine's own hydration and metadata churn, so on
  a live library the skip may almost never fire. It also changes what
  `tree.json`'s `generatedAt` attests — the host's staleness check leans on it —
  so it needs a separate `walkedAt` field. Measure event rates on the real guest
  before building it.
- **Caching `exclusions.json` parsing across passes.** The parse and SHA-256 are
  cheap; the expensive part is `Test-PathContainment`'s per-pass
  `CfGetPlaceholderInfo` walk over intermediate directories, and that is not
  config validation — it is the D19 runtime re-check that catches a
  previously-valid path whose parent later became a non-cloud reparse point.
  Caching it away would narrow a locked guarantee for a low-value saving.

**Optional host knobs — operator's call, measured, not installed by any script.**
Documented in `SETUP.md`; none of them is a repository file.

- `halt_poll_ns=0` stops KVM spinning on idle vCPU halts, but it is host-global,
  adaptive by design, and unmeasured here. Benchmark with `tools/vcpu-profile.py`
  first: halt polling can only appear in its `kernel` column, and an aggregate
  `docker stats` percentage cannot show whether there is anything to recover.
- `"userland-proxy": false` in `/etc/docker/daemon.json` removes the userspace
  copy on loopback-published ports, but it is daemon-wide, version-sensitive, and
  needs a daemon restart — do that only with the bridge powered off via the D29
  helper, and only if `pidstat` shows `docker-proxy` actually matters. Never
  route around it by mounting the container IP: that abandons the published-port
  topology `acceptance-tests.sh` §3 asserts.
- Transparent hugepages: confirm `madvise` or `always` in
  `/sys/kernel/mm/transparent_hugepage/enabled`. Most distros already are.

**Accepted recurring cost, documented rather than changed.** The v1 health timer
writes `/mnt/icloud/.linux-canary` every ten minutes, and that path is *inside*
the sync root — so iCloud uploads a new version of it 144 times a day, forever,
with server-side version history. The upload is not what the check proves (the
script's own header says so); it proves the host→guest→NTFS path. Lengthening the
interval would have to move three coupled values in lockstep — the timer's
`OnUnitActiveSec`, `icloud-health.sh`'s 300 s freshness check, and `health.py`'s
`CANARY_MAX_AGE_SECONDS` — plus the v1 plan §8/§9 verbatim copies, and a partial
change would produce false "canary stale" reports. The tight cadence catches a
hung guest fastest, so it stays as it is.

---

## 9. Out of scope for v2 (explicitly)

- Pattern/glob exclusions (e.g. `*.mp4`) — path-list only.
- Following renames of excluded items.
- A host-side FUSE filter layer.
- Pause/resume **sync** from the GUI — leaving the VM and mounts up while iCloud
  stops syncing. D30's Power off/Start bridge is a different thing: a whole-bridge
  power operation that stops the VM and disconnects both shares.
- Per-item pinning ("always keep offline") or pre-warming; smarter cache
  policies than D26's coldest-first sweep. `attrib +P` machinery exists if a
  later version wants it.
- Host-side progress indication for in-flight hydrations.
- Any Photos support (unchanged from v1).
