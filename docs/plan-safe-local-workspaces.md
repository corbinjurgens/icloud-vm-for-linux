# Safe Workspaces — local editing replicas above the raw iCloud mount

**Status:** design, not yet implemented · **Audience:** an executor (human or
model) who follows instructions literally · **Register row:** D52 in
`docs/plan-gui-selective-sync.md` §1.

This document is the authoritative design for the **Safe Workspaces** feature.
Everything the implementation needs to agree on — schemas, path rules, gating,
the exact synchronizer invocation, thresholds, strings, privacy boundaries,
install paths, and the live acceptance matrix — is pinned here. It amends
nothing in v1 or v2 except through D52 and the v2 §9 amendment that D52 names.

`CONTRIBUTING.md` remains the canonical contributor guide; where this document
and it appear to disagree about workflow, verification, or commit rules,
`CONTRIBUTING.md` wins.

---

## 1. What this is, and what it is not

A **Safe Workspace** is an opt-in, per-directory pairing of

- a **remote root**: a directory under the existing `/mnt/icloud` CIFS mount,
  which Apple's iCloud for Windows client owns inside the guest; and
- a **local root**: an ordinary directory on the Linux host's own persistent
  filesystem, which the operator's editor (Obsidian, in the motivating case)
  opens instead of the mount.

While the GUI is running and the bridge lifecycle is in normal monitoring, the
host reconciles the two roots bidirectionally with **Unison**, in finite
one-shot cycles scheduled through the GUI's existing worker pool.

It **is not**:

- a replacement for `/mnt/icloud`. The raw mount keeps working exactly as it
  does today, for browsing, for arbitrary file access, and as the source and
  destination of every remote-side cycle (D6, unchanged).
- a filesystem overlay. No FUSE, rclone, NFS, SSHFS, WebDAV, 9p, or VirtioFS is
  introduced, and the mount options — `cache=strict`, `actimeo=1`, `noperm`,
  `rasize` (D33, D50) — are untouched.
- a change to hydration policy. Files On-Demand stays on and nothing is pinned
  (D14, D25). A workspace's first cycle reads the remote root, which hydrates
  what it reads exactly like any other host read.
- a pause switch for Apple's sync. iCloud for Windows keeps syncing whenever
  the guest is up; only the project's own replicator can be paused (v2 §9, as
  amended by D52).
- a way to prevent conflicts created entirely between Apple devices. Two Apple
  devices editing the same note while this host is off is an iCloud-side
  conflict that this feature never sees.

## 2. Why a replica, and why Unison

### 2.1 The problem

The operator observed an Obsidian vault, opened directly from `/mnt/icloud`,
clearing an open note's text in the editor. The supporting evidence collected
afterwards, for one note in that vault:

- its content hash, logical size, inode number, and modification time were all
  **unchanged** across the observation window, while
- its `ctime` changed **twice** after the save.

The trace began after the operator had already seen the editor clear, so this is
**supporting evidence of metadata-only churn**, not a captured cause-and-effect
sequence for the clearing itself. It is recorded because it is exactly the shape
of event a naive "the file changed, reload it" watcher misinterprets: nothing
about the bytes moved, only the inode's status-change timestamp, which a CIFS
client can move for reasons that originate in the guest's Cloud Files filter
rather than in any write.

Two further conditions were present and are **not** established causes: Wi-Fi
reassociations coincided with the observed periods (SMB here is loopback, but
the guest's Apple connection is not), and an earlier period logged CIFS
`Close unmatched open` messages. Dehydration pressure was ruled out: the guest
had ample free space, reclamation was inactive, and the vault's content was
already local in the guest.

The design conclusion does not depend on resolving the root cause. An editor
that keeps an open, continuously autosaving document on a network filesystem
whose metadata is rewritten out-of-band by a cloud filter is exposed to a class
of failure that no mount option removes. Moving the editor onto local disk and
putting a conflict-aware synchronization boundary in front of the mount removes
the exposure regardless of which of the above was the trigger.

### 2.2 Why this engine

Unison maintains two replicas plus a remembered common state (an *archive*),
detects divergent edits on both sides, refuses to guess a winner, survives
interrupted runs, and works while one replica is offline. It is a stock Debian
package, runs as an ordinary user, and holds no daemon in the data path. The
required line is **Unison 2.52 or newer** — the compatibility line documented
upstream; this host currently has **2.53.8 (ocaml 5.3.0)** at `/usr/bin/unison`.

Rejected alternatives:

- **`rclone mount --vfs-cache-mode full`** — can defer write-back to close, but
  its own documentation warns that an out-of-band remote length change during
  attribute caching can expose truncated or garbage content. That is the same
  failure class being escaped.
- **A new three-way merge engine in this repository** — the safety properties
  wanted here (archive-based update detection, conflict retention, restartable
  runs) are the ones a mature synchronizer already has, and getting them subtly
  wrong loses user data.
- **`rsync` in both directions, or Syncthing/Mutagen** — one-way tools cannot
  express "both sides changed", and adding another always-on sync daemon
  reintroduces the daemon-in-the-data-path objection that v2 §0 raised against
  FUSE.

### 2.3 Upstream references

- Unison: <https://github.com/bcpierce00/unison>, command reference at
  <https://man.archlinux.org/man/extra/unison/unison.1.en>.
- The same symptom reported for Obsidian on iCloud for Windows, and the
  reporter's local-vault-plus-history fix:
  <https://www.reddit.com/r/ObsidianMD/comments/1pkh724/i_finally_fixed_obsidian_icloud_sync_on_windows/>,
  <https://github.com/gursimar/obsidian-icloud-windows-sync>.
- Obsidian recommends iCloud only on Apple operating systems and warns that
  iCloud Drive on Windows can duplicate or corrupt files:
  <https://obsidian.md/help/sync-notes>.
- SMB change notification is not dependable for a vault, including nested
  folders:
  <https://forum.obsidian.md/t/subfolder-file-changes-not-detected-in-smb-share-vaults/59326>
  — which is why this design **polls** rather than subscribing to notifications.
- `rclone mount`, for the rejection above: <https://rclone.org/commands/rclone_mount/>.

### 2.4 The one thing the operator must not do

Two active bidirectional mechanisms over the same local vault will fight. If
Obsidian Sync (or any other bidirectional sync) is enabled for a vault, it must
be disabled before that vault becomes a Safe Workspace. The operator
documentation (task 7) states this; the GUI states the Obsidian half of it in
the add-workspace confirmation (§11.3).

---

## 3. Module boundaries

The Qt boundary in `CONTRIBUTING.md` is load-bearing and this feature does not
bend it.

| Module | Qt | Owns |
|---|---|---|
| `gui/icloud_bridge_gui/workspaces.py` | Qt-free, no CIFS I/O, no subprocess | the configuration model, XDG paths, validation, path normalization and rejection, state-directory layout |
| `gui/icloud_bridge_gui/workspace_sync.py` | Qt-free | one finite synchronization cycle: gating checks, snapshots, stability, first-run rules, the destructive guard, the Unison invocation through an injected runner, exit classification, status persistence |
| `gui/icloud_bridge_gui/__main__.py` | Qt | the 5-second timer, single-flight scheduling through `run_async`, `_active` accounting, and every stop/quiesce path |
| `gui/icloud_bridge_gui/window.py` | Qt | the **Safe Workspaces** tab, its dialogs and strings |
| `gui/icloud_bridge_gui/health.py` | Qt-free | the synthetic **Safe workspaces** row |
| `gui/icloud_bridge_gui/diagnostics.py` | Qt-free | the counts-only facts of §12 |

`workspaces.py` performs local filesystem I/O only (XDG config and state, plus
`/proc/self/mountinfo` classification). It never touches `/mnt/icloud` and never
runs a subprocess. `workspace_sync.py` is the only module that reads the mount
for this feature, and the only one that executes Unison.

Nothing in the Windows guest changes. `guest-agent/agent.ps1` and
`provision/agent.ps1` are untouched; there is no bridge-protocol change and no
`agentBuild` bump (D35 is unaffected).

---

## 4. Configuration: `workspaces.json`

### 4.1 Location and file rules

`$XDG_CONFIG_HOME/icloud-bridge-gui/workspaces.json`, defaulting to
`~/.config/icloud-bridge-gui/workspaces.json`. Every XDG helper accepts an
injected environment mapping for tests, and a **relative** `XDG_CONFIG_HOME` or
`XDG_STATE_HOME` is rejected in favour of the specification's home-based
default — the same defensive shape `backup.py` already uses.

- The application directory is created mode **0700**; the file is a **0600**
  regular file.
- Writes go through a unique same-directory temporary file, `fsync`, then
  `os.replace`. A failed write leaves the previous file intact and removes its
  temporary file.
- A symlinked directory, a symlinked destination, or a non-regular destination
  is **refused**, never followed.
- An existing regular file is tightened to 0600 even when the write is skipped.
- Maximum file size read: **1 MiB**. Maximum workspaces: **32**.
- A missing file means "no workspaces" and is not an error. A malformed,
  unreadable, or unsupported-version file is an **error that fails closed**: no
  workspace runs, the GUI shows the error, and the file is never rewritten or
  silently replaced.

### 4.2 Schema (version 1)

```json
{
  "version": 1,
  "workspaces": [
    {
      "id": "9f14c0a7b3e25d68",
      "name": "Vault",
      "remote": "Documents/Vault",
      "local": "/home/user/iCloud Workspaces/Vault",
      "enabled": true
    }
  ]
}
```

| Field | Type | Rules |
|---|---|---|
| `version` | int | must equal `1`; anything else fails closed |
| `workspaces` | list | 0-32 entries, order preserved on write; scheduling order is by `id` |
| `id` | str | exactly `^[0-9a-f]{16}$`, generated with `secrets.token_hex(8)`; unique within the file |
| `name` | str | display only; 1-80 characters after stripping; no control characters, no NUL, no newline; not required to be unique |
| `remote` | str | normalized per §5.1 |
| `local` | str | normalized per §5.2 |
| `enabled` | bool | `false` means configured but paused; the GUI still shows it |

**Unknown keys are rejected**, at the document level and per workspace. This is
pre-release code (`CONTRIBUTING.md`); there is one schema version, no migration
path, and no tolerant reader. Readers and writers change together.

Rejected at load and at edit time, with a specific message each:

- duplicate `id`;
- duplicate or overlapping `local` roots (equal, or one a path ancestor of
  another after normalization);
- duplicate or overlapping `remote` roots (§5.1 comparison rules);
- any per-field rule above.

### 4.3 Validation is separable from creation

`workspaces.py` exposes side-effect-free validation — string and containment
rules that need no filesystem access — separately from the checks that stat the
filesystem, and separately again from creating anything. The GUI needs the first
kind synchronously while the operator types, the second kind in a worker, and
the third only after a confirmation.

---

## 5. Path normalization and rejection

### 5.1 Remote paths

A remote path is stored as a **non-empty, forward-slash-separated, relative**
path naming a directory under the iCloud mount. Normalization: strip leading and
trailing `/`, collapse repeated `/`, apply Unicode **NFC** to the whole string,
then store.

Rejected outright:

- empty, or empty after normalization;
- any segment equal to `.` or `..`;
- an empty segment, or a segment that is only whitespace (`a//b` normalizes; a
  leading `/` is stripped, but a rooted path such as `/mnt/icloud/x` given as
  `remote` is rejected because its first segment would be `mnt` only after
  stripping a root the user did not mean — reject any input whose original
  form starts with `/`);
- a NUL byte, a backslash, or any C0 control character;
- a segment containing a character Windows cannot use in a folder name, or a
  segment ending in a space or a dot — the same shapes `bridge.validate_relpath`
  rejects for the same mount-relative namespace (D22d); `normalize_remote`
  reuses that function's character set rather than a second copy of it;
- a path whose resolution would escape the configured mount root;
- the mount root itself (a workspace may not be the whole sync root);
- anything longer than 1024 characters, or any segment longer than 255 bytes.

The remote endpoint is Windows, so **comparison is case-insensitive**: overlap
and duplicate detection compare NFC-normalized segments with `str.casefold()`.
Storage preserves the operator's casing.

The remote root resolves to `os.path.join(bridge.mount_dir(), remote)`, so the
existing `ICLOUD_MOUNT_DIR` override (default `/mnt/icloud`) is respected. The
remote directory must already exist; a workspace never creates one.

### 5.2 Local paths

A local root must be:

- **absolute**, and normalized without resolving symlinks in the stored value;
- **not** inside the iCloud mount (`ICLOUD_MOUNT_DIR`, default
  `bridge.DEFAULT_MOUNT_DIR`), the bridge control share (`ICLOUD_BRIDGE_DIR`,
  default `bridge.DEFAULT_BRIDGE_DIR`), `$XDG_STATE_HOME`, or
  `$XDG_CONFIG_HOME`, and not an ancestor of any of them;
- **not** a symlink (the final component is checked with `lstat`), and no
  existing ancestor component may be a symlink;
- **not** `/`, `/home`, the user's home directory itself, or any existing
  path that already contains another workspace's local root;
- on an allowlisted filesystem: **`ext4`, `xfs`, `btrfs`, `bcachefs`, `zfs`**.

Filesystem classification parses `/proc/self/mountinfo` and matches the longest
mount point that is a prefix of the local root — or, when the root does not yet
exist, of its **nearest existing parent**. Network, FUSE, 9p, overlay, and
memory-backed filesystems (`cifs`, `nfs`, `nfs4`, `fuse.*`, `9p`, `virtiofs`,
`overlay`, `tmpfs`, `ramfs`, ...) are rejected with a message naming the
detected type. An unclassifiable path is rejected, not assumed local.

The default parent offered by the GUI is `~/iCloud Workspaces`; nothing enforces
it, and no real vault path, note name, or private path is a product default
anywhere in this feature.

### 5.3 First-run occupancy rule

A new workspace may target only a **nonexistent** local directory or an
**empty** one. The first stable cycle seeds it from the remote directory. If
both sides have content at first run, the workspace **refuses to start**: that
is an ambiguous merge the operator must resolve deliberately, not a job for a
first sync. The refusal names both endpoints' file counts and changes nothing.

---

## 6. Lifecycle gating

A cycle may begin **only** when all of the following hold:

1. the GUI/tray process is running (there is no daemon, no timer outside the
   process, and no cycle survives the process);
2. the lifecycle phase is **normal monitoring** — not setup, not provisioning,
   not powered off, not `transition_unknown`, not shutting down;
3. bridge I/O is not paused;
4. the workspace is `enabled`;
5. no cycle is already running, globally or for that workspace.

Inside the worker, before **any** access to a CIFS path, the cycle re-checks in
this order:

1. non-blocking `flock` on `<state>/lock`; a second invocation returns
   `already-running` and performs **no scan**;
2. `/var/lib/icloud-bridge/powered-off` — if the marker exists, return `paused`
   immediately. This short-circuit precedes every mount touch, so a powered-off
   bridge is never probed;
3. `os.path.ismount(mount_dir)` — if false, return `unavailable`;
4. the remote root exists and is a directory (`lstat`, symlink not followed) —
   if not, return `unavailable`. A cycle **never creates** a remote directory;
5. the Unison version check of §7.1 (cached once per app process).

Quitting only the GUI therefore pauses synchronization while leaving local
editing entirely safe; the next app start resumes pending propagation from the
archives. Bridge power-off drains the active cycle through the existing
`_active` accounting and `MainWindow.quiesce()` before `icloud-bridge-power off`
runs — no workspace process outlives an unmount, and no forced or lazy unmount
is ever used.

---

## 7. One cycle

### 7.1 Version contract

Once per application process, `unison -version` is executed through the injected
runner and its `unison version X.Y.Z` output parsed. A version below **2.52**,
or an unparseable one, disables Safe Workspace cycles with an actionable result
naming the found version and the required one. The project never downloads,
vendors, or builds a Unison binary.

### 7.2 Snapshots

Both roots are walked recursively **without following symlinks** and **without
crossing filesystem boundaries**. For each entry the snapshot records:

```
(relative path, kind, size, mtime_ns)
```

where `kind` is `file` or `dir`, `size` is the logical size (0 for directories),
and `mtime_ns` is `st_mtime_ns`. The four ignore paths of §7.4 are excluded from
the walk.

Entries that are neither a regular file nor a directory — sockets, devices,
FIFOs, symlinks — and any mount-point crossing **stop the workspace with an
error naming the relative path**. They are never copied and never silently
skipped.

### 7.3 Stability

The observed snapshot pair is persisted atomically to `<state>/snapshot.json`
after every poll. Unison runs **only when both endpoints present a snapshot
identical to the immediately preceding poll**. Otherwise the cycle returns
`stabilizing` and does nothing else.

With the 5-second poll interval this produces a **5-10 second** settling window:
a save observed just after a poll waits up to one interval to be seen, then one
full interval to be confirmed unchanged.

The fingerprint is exactly `(relative path, kind, size, mtime_ns)` for every
non-ignored entry, on both sides. Deliberately **excluded**: `ctime`,
permissions, ownership, allocated size, inode number, and extended attributes.
`ctime` is excluded on purpose — the observed evidence of §2.1 is precisely a
`ctime`-only change with identical content, size, inode, and mtime, and treating
that as a change would make the guest's metadata churn drive the synchronizer.

### 7.4 Ignored paths

Exactly four, and no pattern language:

```
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.obsidian/cache
.trash
```

These are per-device UI state, per-device caches, and a local trash bin. Notes,
attachments, plugins, themes, snippets, and the rest of `.obsidian/` are
synchronized. Ignoring a directory ignores its whole subtree.

### 7.5 First run and free space

On the first cycle for a workspace (no archive, no baseline):

- the remote root must be **non-empty**;
- the local root must be **nonexistent or empty**;
- available local bytes (`statvfs` on the nearest existing parent) must exceed
  the remote **logical** size plus **1 GiB**;
- only after every path, filesystem, and free-space check passes is the local
  root created, mode 0700 by default (`umask`-independent), owned by the desktop
  user.

Any failure leaves both sides untouched and reports the specific refusal.

### 7.6 Destructive-change guard

`<state>/baseline.json` holds the path sets (relative paths only, per endpoint)
observed by the **last successful** cycle. Before invoking Unison, the current
stable snapshot's path sets are compared with it. The cycle **halts without
invoking Unison** if, for either endpoint:

- the endpoint is now **empty** and was **non-empty** in the baseline; or
- the number of baseline paths now absent is **at least 20** *and* at least
  **20 percent** of that endpoint's baseline path count.

A halt sets state `guarded`, records the counts and up to 20 example relative
paths, and leaves both replicas and the baseline untouched. Cycles for that
workspace stop until either

- a later stable snapshot no longer trips the thresholds (the content came
  back — a slow mount, an interrupted download, an accidental move that the
  operator undid), or
- the operator resolves it outside the app, using the surviving replica and the
  central backups.

There is deliberately **no "accept and delete anyway" button**. A genuine mass
deletion is performed by the operator on both replicas, or by forgetting and
re-adding the workspace after the endpoints agree. Automatic mass deletion is
out of the question, and a one-click override is the same thing with a dialog in
front of it.

The baseline is updated **only** after a cycle that exits 0. A guarded,
timed-out, conflicted, or failed cycle never advances it.

### 7.7 The Unison invocation

One argument per option, built as an argv list. **No shell**, ever. Both roots
are absolute. The command is exactly:

```
unison <local-root> <remote-root>
  -batch
  -auto
  -fastcheck false
  -times=true
  -perms 0
  -dontchmod=true
  -owner=false
  -group=false
  -xattrs=false
  -acl=false
  -confirmbigdel=true
  -backup "Name *"
  -backupcurr "Name *"
  -backuploc central
  -backupdir <state>/backups
  -maxbackups 10
  -ignore "Path .obsidian/workspace.json"
  -ignore "Path .obsidian/workspace-mobile.json"
  -ignore "Path .obsidian/cache"
  -ignore "Path .trash"
  -logfile <state>/sync.log
  -color false
```

Each quoted string above is **one** argv element (`Name *`,
`Path .obsidian/cache`, and so on); the quotes are documentation, not shell
syntax.

The seven `-name=value` options are written that way because Unison's parser
treats each of them as a flag taking no separate argument: given `-times true`
it reads `true` as a third root and exits 1 with "unison was invoked incorrectly
(too many roots)". Verified against 2.53.8. The options that do take a separate
argument — `-fastcheck`, `-perms`, `-backup`, `-backupcurr`, `-backuploc`,
`-backupdir`, `-maxbackups`, `-ignore`, `-logfile`, `-color` — are passed as two
argv elements, as written. Do not "normalise" the two forms into one.

Why each option, briefly: `-fastcheck false` makes update detection
content-based rather than trusting a CIFS-reported mtime; `-times true` keeps
modification times meaningful across the boundary; `-perms 0`, `-dontchmod`,
`-owner`, `-group`, `-xattrs`, `-acl` stop Unix metadata being invented for a
Windows-backed replica and stop `chmod` being called at all; `-confirmbigdel
true` makes a whole-replica deletion abort in batch mode instead of propagating
(a second line of defence behind §7.6); the backup options are §8; `-color
false` and the explicit `-logfile` keep the output parseable and bounded.

**Never passed, under any circumstance:**

`prefer`, `preferpartial`, `force`, `copyonconflict`, `repeat`, `ignorelocks`,
`ignorearchives`, `retry`, `silent`, `terse`, or any option that selects a
winner, deletes an archive, runs continuously, or overrides a lock.

Execution environment (production adapter):

- `subprocess.run` with a **120-second** timeout, `capture_output=True`,
  `text=True`, no shell, and no `cwd` inside either replica;
- `UNISON=<state>/unison` — a workspace-private archive/profile directory
  (mode 0700), so a user-wide `~/.unison` profile can never change behaviour;
- `NO_COLOR=1`;
- every subprocess call goes through an **injected runner** so unit tests assert
  the exact argv and environment without a Unison binary.

### 7.8 Exit classification

| Unison exit | Result state | Meaning |
|---|---|---|
| 0 | `synchronized` | everything propagated; the baseline advances |
| 1 | `conflict` | some paths were skipped or conflicted; **both endpoints keep their own content** |
| 2 | `failed` | non-fatal failure; nothing is assumed about partial progress |
| 3 | `fatal` | fatal error |
| other / signal | `failed` | classified with the raw code in the bounded detail |
| timeout (120 s) | `timeout` | the process is killed; the baseline does not advance |

A `conflict`, `failed`, `fatal`, or `timeout` cycle leaves the baseline and the
last-success timestamp unchanged. Only exit 0 sets `lastSuccessAt`.

### 7.9 Per-workspace status document

`<state>/status.json`, written atomically as a 0600 file, version 1:

```json
{
  "version": 1,
  "state": "up-to-date",
  "updatedAt": "2026-07-29T09:41:02Z",
  "lastSuccessAt": "2026-07-29T09:41:02Z",
  "lastExit": 0,
  "counts": {
    "localPaths": 837,
    "remotePaths": 837,
    "conflicts": 0,
    "missingFromBaseline": 0
  },
  "paths": [],
  "detail": ""
}
```

`state` is one of `waiting`, `stabilizing`, `syncing`, `up-to-date`, `paused`,
`conflict`, `guarded`, `error`. `paths` holds at most **20** relative paths,
each elided to 200 characters, naming conflicted or guard-relevant entries.
`detail` is a sanitized, single-line, **2000-character** bounded excerpt of the
engine's own message — control characters stripped, never raw file content.

The full Unison log stays in `<state>/sync.log`, truncated when it exceeds
**1 MiB** so it cannot grow without bound. Nothing from either file is ever
admitted to `diagnostics.Facts` (§12).

---

## 8. Backups and retention

Central backups live in `<state>/backups`, created 0700. `-backup Name *` and
`-backupcurr Name *` mean every path is backed up **before an overwrite or a
deletion**, and a copy of the current common version is retained as well;
`-maxbackups 10` keeps ten versions per path.

Consequences that are stated rather than hidden:

- the backup directory grows with edit volume and is pruned only by Unison's own
  `maxbackups` rule. Nothing in this project deletes it on a schedule.
- **Forgetting a workspace removes only its configuration entry.** The local
  replica, the remote iCloud directory, the Unison archives, the state directory,
  the log, and the backups all remain exactly where they are. The forget dialog
  states each retained location (§11.5).
- **Package removal deletes none of it.** Workspace configuration, replicas,
  state, archives, logs, and backups are operator data under the
  `CONTRIBUTING.md` preservation rule.

---

## 9. Conflicts

When Unison exits 1 the two replicas are left as they are: each side keeps its
own version of every divergent path, and the central backups hold the previous
common version. Nothing is merged, nothing is renamed, and no winner is chosen.

Presentation:

- the workspace's status becomes `conflict` and its row in the tab turns yellow;
- the tab names the affected **relative paths**, bounded to 20 entries of 200
  characters, and points at the local root, the remote root, and the backup
  directory;
- **note contents are never displayed**, quoted, or logged by the GUI;
- recovery is the operator's explicit act: compare the two versions, save the
  intended content, and the next stable cycle propagates it. The app offers no
  "resolve automatically" action.

Repeated conflicts on the same path are not escalated or auto-resolved; they
stay yellow until the operator acts.

---

## 10. Scheduling

- One **5-second** `QTimer`, owned by `Application`, started only alongside
  normal health polling once startup has reached monitoring, and stopped on
  every existing stop-polling, setup, powered-off, `transition_unknown`, and
  quiesce path.
- Configuration is loaded **without any CIFS access**; enabled workspaces are
  queued in deterministic `id` order.
- **At most one cycle globally** is in flight, executed through `run_async` and
  counted in `_active`. No detached thread, no detached process. Shutdown waits
  for it, and the 120-second subprocess timeout guarantees the drain eventually
  releases.
- A timer tick while a cycle is active records **one** pending pass instead of
  queueing another. The pending pass runs after completion only if monitoring is
  still active and I/O is not paused.
- Configuration is reloaded after each completed pass and after any GUI edit, so
  a removed or disabled workspace never gets another cycle from a stale queue.
- Results are cached locally only. Nothing about workspaces enters the raw
  bridge health JSON, and the guest agent is not modified.

---

## 11. GUI

### 11.1 The tab

A persistent **Safe Workspaces** tab, placed after **Selective Sync**. It stays
visible while the bridge is powered off — local paths and last status remain
inspectable — but every action that would touch `/mnt/icloud` is disabled in
that state.

Each row shows: name, local folder, iCloud-relative folder, enabled state, last
successful sync, and one classified status rendered as
`waiting`, `stabilizing`, `syncing`, `up to date`, `paused`, `conflict`,
`guarded`, or `error` (the machine token `up-to-date` displays as `up to date`).

### 11.2 Actions

**Add workspace…**, **Sync now**, **Pause**, **Resume**, **Open local folder**,
**Open iCloud folder**, **Forget workspace…**.

- Local paths open through the existing `tray.open_externally` helper.
- A file dialog is **never** pointed at CIFS, and no CIFS enumeration happens on
  the GUI thread. The remote directory is entered as text.
- **Pause** only clears the configuration's `enabled` flag and waits for an
  active cycle to finish; it never kills a running Unison.
- **Sync now** requests one pass through the same single-flight path as the
  timer; it is not a second execution route.

### 11.3 The add dialog

Fields: display name, iCloud-relative directory (text), local directory (chosen
from a **local-only** file dialog). String rules validate synchronously as the
operator types; filesystem state is validated in a worker; then a confirmation
states the remote logical size, the local free space, what the first copy will
do, and **this exact warning text**:

```
Close the iCloud copy of this vault in Obsidian before continuing. After the first sync, open only the local workspace in Obsidian.
```

### 11.4 After the first seed

On successful initial seeding the app offers **Open local workspace in
Obsidian**, using a correctly percent-encoded
`obsidian://open?path=<absolute path>` URL (`urllib.parse.quote(path, safe="")`).
Because an `obsidian://` handler is not guaranteed to be registered, the plain
**Open local folder** action is always retained alongside it.

### 11.5 Forget

**Forget workspace…** confirms with the exact retained locations before removing
only the configuration entry:

```
Forgetting a workspace removes only its entry in this app.

These are kept, exactly as they are:
  Local workspace: <local root>
  iCloud folder:   <mount>/<remote>
  Sync state:      <state dir>
  Backups:         <state dir>/backups

Nothing is deleted from this computer or from iCloud.
```

### 11.6 Quit GUI only

The **Quit GUI only (leave bridge running)** confirmation gains this exact
sentence:

```
Safe Workspace synchronization pauses while this app is closed. Your local workspace files stay on this computer and remain safe to edit; changes propagate after the next start.
```

Window close, logout, signals, crashes, and `aboutToQuit` still never power off
the bridge (D29).

---

## 12. Health and privacy

### 12.1 Health row

One synthetic **Safe workspaces** row, appended only when at least one workspace
is configured.

- **Yellow** when any workspace is in `conflict`, `guarded`, or `error`.
- **Green** otherwise (including `waiting`, `stabilizing`, `syncing`, `paused`).
- **Never red.** A workspace condition never turns a connected bridge red, and a
  healthy workspace never lowers an existing yellow or red bridge state — the
  row participates in the existing worst-of computation and nothing else. A
  disconnected raw bridge keeps its red precedence (D23).

### 12.2 Diagnostics

`diagnostics.Facts` gains **counts and one timestamp, nothing else**:

```
workspaces_configured: int
workspaces_enabled:    int
workspaces_conflicted: int
workspaces_guarded:    int
workspaces_failed:     int
workspaces_last_success: str   # UTC ISO-8601, or "" when there has never been one
```

Not admitted to `Facts`, with or without an opt-in: workspace names, local
paths, remote paths, relative file paths, note contents, Unison output, log
excerpts, `status.json` `detail` or `paths`, environment values, and Apple
identity data. D37's rule stands — a field nobody copied in cannot leak, so
these are excluded at the dataclass rather than scrubbed downstream.

---

## 13. Install paths and packaging

Both supported installation paths are updated together.

- `packaging/deb/control.in`: `Depends` gains `unison (>= 2.52)`.
- `host/setup-prereqs.sh`: installs `unison` through the existing
  Debian/Ubuntu prerequisite flow.
- `gui/install-gui.sh`: installs the distro `unison` package through its
  existing apt path. On a non-apt system the GUI install still completes and
  prints that Safe Workspaces is unavailable until Unison 2.52 or newer is
  installed. **No unverified binary is ever downloaded.**
- `host/acceptance-tests.sh`: a **non-mutating** presence and version check.
  The generic acceptance script creates, modifies, and deletes nothing in the
  operator's vault.
- `gui/icloud_bridge_gui/__init__.py`: `__version__ = "0.3.0"` — a new design
  line rather than a patch to the v2 selective-sync UI. The Makefile and the
  package builder already derive the package version from that single source.
- Package removal scripts are **not** extended to delete XDG workspace
  configuration, replicas, state, archives, logs, or backups.

The new modules ship automatically: `packaging/build-deb.sh` copies the whole
`gui/icloud_bridge_gui` directory, and `gui/install-gui.sh` copies the same
package into the per-user install.

When the operator must refresh the installed host package, the documented
command is `make reinstall`.

---

## 14. Failure handling

| Condition | Result | Effect |
|---|---|---|
| powered-off marker present | `paused` | no CIFS access at all |
| mount absent | `unavailable` | no remote access; retried next tick |
| remote directory missing | `unavailable` | never created |
| lock held | `already-running` | no scan, no engine |
| Unison < 2.52 or unparseable | `error` | cycles disabled for the process, actionable message |
| special file / mount crossing in a replica | `error` | workspace stops; nothing copied |
| local filesystem outside the allowlist | `error` | workspace stops |
| insufficient free space | `error` | local root not created |
| content on both sides at first run | `error` | ambiguous merge refused |
| destructive guard tripped | `guarded` | Unison not invoked; both replicas preserved |
| exit 1 | `conflict` | both versions retained; yellow |
| exit 2 / 3 / other | `error` | baseline unchanged |
| 120-second timeout | `error` | process killed; baseline unchanged |
| CIFS operation hangs | bounded timeout expires | reported; **never** a forced or lazy unmount |

Every one of these leaves both replicas intact. The rule behind the table: when
in doubt, stop that workspace and preserve both sides.

---

## 15. Live acceptance matrix

These cannot be proved by unit tests. They require the real host, the Windows
guest, CIFS, Obsidian, and a second Apple device, and they run first against a
**disposable copy** of a vault with an independent backup. The real vault is cut
over only after every disposable-vault row passes.

Task 8 records the date, package version, Unison version, Obsidian version,
mount options, and test-vault size/count in `docs/acceptance-results.md`, and
marks one disposition per row here. A failed or unexecuted row stays visibly
incomplete; its expected result is never weakened to make it pass.

| # | Scenario | Expected result | Disposition |
|---|---|---|---|
| A1 | Initial seed: Obsidian closed, disposable remote vault, empty local root | first stable cycle creates a complete local replica and reports up to date | unverified |
| A2 | Metadata-only echo: change Windows/iCloud metadata without changing bytes | after two cycles both replicas' hashes and local mtimes are unchanged and no conflict appears | unverified |
| A3 | Obsidian continuous typing for at least two minutes in the local vault | text never clears; the remote copy lags during active saves and converges after the stability window | unverified |
| A4 | Local save propagates | a single local note edit reaches the remote replica after the stability window | unverified |
| A5 | Inbound edit from a Mac or iPhone to a different note | the local vault receives the real content change after iCloud plus the stability window | unverified |
| A6 | Concurrent conflict: same note edited differently on Linux and an Apple device before convergence | workspace turns yellow/conflicted; both distinct versions survive on their own replicas; central backups exist; **no automatic winner** | unverified |
| A7 | Wi-Fi loss during a local edit, then reconnect | the local note stays intact and later converges without an iCloud-metadata-driven editor reload | unverified |
| A8 | Bridge power-off during a cycle | the GUI quiesces new cycles, waits for the active worker, unmounts normally (never forced or lazy); local editing stays available while powered off | unverified |
| A9 | GUI-only quit, local edit, restart | the edit propagates after the next stable cycle | unverified |
| A10 | Restart recovery: kill the app mid-cycle, relaunch | no archive corruption; the next cycle completes; nothing is lost on either side | unverified |
| A11 | Ordinary deletion plus backup restoration | the deletion propagates and the prior version is restorable from the central backup | unverified |
| A12 | Guarded mass deletion: an endpoint made empty, and a 20-path/20-percent burst | both halt behind the guard without invoking Unison; both replicas preserved | unverified |
| A13 | Package reinstall (`make reinstall`) | configuration, replicas, state, archives, and backups all survive; cycles resume | unverified |
| A14 | Real-vault cutover, only after A1-A13 pass | raw vault closed in Obsidian, real local workspace created, file counts and hashes validated, local path opened as the vault | unverified |

---

## 16. Out of scope

- Pausing Apple's own iCloud sync. It stays out of scope (v2 §9, as amended by
  D52); only the project-managed replicator can be paused.
- Pattern or glob ignore rules. The four ignore paths of §7.4 are the whole
  list.
- Merging conflicting versions, three-way text merge, or any automatic winner.
- A background daemon, a systemd unit, or any cycle outside the GUI process.
- Following symlinks or crossing filesystem boundaries inside a replica.
- Preventing conflicts created entirely between Apple devices while this host is
  off.
- Photos, Passwords, Mail/Contacts/Calendar — unchanged from v1.

---

## 17. Related documents

- `docs/implementation-plan.md` — v1 design and the D1-D13 register; **D6** is
  the SMB-rather-than-robocopy-mirror decision this feature layers above.
- `docs/plan-gui-selective-sync.md` — v2 plan; §0 rejects a host-side FUSE
  filter, §1 holds the decision register including **D52**, §9 lists what is out
  of scope for v2 and carries this feature's amendment.
- `CONTRIBUTING.md` — Qt boundary, pre-release policy, shared-worktree rules,
  and the operator data that must survive package removal.
- `docs/acceptance-results.md` — where the live results of §15 are recorded.
