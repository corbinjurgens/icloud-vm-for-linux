# Selective sync — what excluding an item actually does

Open the tray icon → **Open status window** → **Selective Sync**. Uncheck a
folder or file, press **Apply**, confirm.

This page is the user-facing explanation. The design and its rationale live in
[`../plan-gui-selective-sync.md`](../plan-gui-selective-sync.md); the v1 system
it builds on is in [`implementation-plan.md`](implementation-plan.md).

---

## The short version

| | Excluded | Included |
|---|---|---|
| Visible in `ls /mnt/icloud` | no | yes |
| Downloaded | never | on first read |
| Uses guest disk | no (once reclaimed) | while cached |
| Present in iCloud and on your other devices | **yes, always** | yes |

**Exclusion never deletes anything.** It changes what *this Linux host* can see
and what the Windows guest keeps on disk. Your iPhone, your Mac and iCloud.com
are unaffected.

---

## How it works, in one paragraph

The Windows guest is the only component that sees the whole library. A small
agent inside the guest puts an explicit *deny* permission on each excluded item
for the account the Linux host uses over SMB, plus a *deny delete-child* guard
on that item's parent folder. Windows' Access-Based Enumeration then omits the
item from every directory listing the host asks for, because the host's account
cannot read it. The agent also asks Windows to make the item online-only, so its
content is released from the guest disk. All of this is enforced server-side, by
stock Windows mechanisms — there is no filter driver and no custom filesystem in
the data path.

---

## What you will see on the host

For an excluded item:

| Operation | Result |
|---|---|
| `ls` the parent folder | the item is **not listed** |
| `cat` / open it by its known path | `Permission denied` |
| write to it | `Permission denied` |
| `rm` it | `Permission denied` |
| `mv` / rename it | `Permission denied` |
| `mkdir` or `touch` at the same name | `Permission denied` |
| same name in different letter case | `Permission denied` (NTFS is case-insensitive) |

That last group is the point of the parent guard: the safe answer to "create
something where a hidden item already lives" is a hard failure, not a silent
collision. Apps report *permission denied* rather than *no such file*, which can
look odd — it is intended.

Ordinary items in the same folder are unaffected: you can still create, rename
and delete included siblings normally.

---

## States you may see in the *State* column

| State | Meaning |
|---|---|
| *(blank)* | included |
| `applying` | the deny permissions are being established |
| `pending-dehydrate` | hidden and inaccessible already, but the guest still holds the content |
| `applied` | hidden, inaccessible, and no local content remains |
| `not-found` | the configured path does not exist under the sync root |
| `error` | something went wrong; the detail says what |

### `pending-dehydrate` is normal, and it is not a failure

Access is denied the moment the permissions land — that part is immediate.
Freeing the disk space is not. Windows refuses to dehydrate a file that is open,
that has unsaved local modifications, or that has not finished uploading to
iCloud. The agent reports which of those it observed and re-checks every minute;
the state becomes `applied` on its own once the content is safe to release. The
tray icon shows yellow meanwhile.

This ordering is deliberate: you are never in a window where the item is still
readable from the host but its content is already gone.

### `not-found` is a warning, not an error

A configured exclusion whose path does not exist yet has nothing for Windows to
put a permission on. If an item later appears at that exact path — synced down
from another device, say — the agent hides it within one enforcement cycle
(≤ 60 s). Until then it is briefly visible. That is why `not-found` is yellow.

`not-found` items appear in the GUI under **Missing configured items**, with a
**Remove exclusion** button. That is the only way to type-free clear a stale
path; there is no free-text path entry anywhere in the GUI.

### `error: acl-write-denied: <path>`

The agent could not edit that object's permissions. Almost always this means
provisioning step 4 was not applied — re-run `04-bridge-agent.ps1` as
Administrator in the guest and read its protected-DACL report. The item is left
completely untouched when this happens.

---

## Re-including

Check the item again and press **Apply**. Within about two minutes it reappears
in `/mnt/icloud` as an online-only placeholder. Its content downloads the first
time something reads it — re-including does not trigger a download by itself.

Checking a folder that contains excluded items asks for confirmation first,
because it re-includes the whole subtree at once.

---

## Known limitations (accepted; do not file these as bugs)

- **Renames of an excluded item are not followed.** Depending on how iCloud
  materialises the rename, the item may keep its deny permission (and stay
  hidden under its new name) or arrive as a fresh placeholder (and be visible).
  Within ten minutes the agent reconciles: leftover permissions that no longer
  match the configured list are removed, the old configured path starts
  reporting `not-found`, and the new path is treated as included. Exclude the
  new path and clear the old one from **Missing configured items**.
- **Pattern exclusions (`*.mp4`) are not supported.** The list is paths only.
- **Cold reads block.** This is a property of Files On-Demand, not of exclusion:
  see the README's *Files On-Demand and disk space* section.
- **Exclusion does not sandbox the guest.** It restricts the account the Linux
  host uses. Someone signed in at the Windows desktop still sees everything.
- **Disk reclamation is asynchronous and may be unable to reach its target.**
  If enough content is open, modified, or still uploading, free space stays
  below the floor and the tray stays yellow until that clears — or until you
  grow the disk.

---

## Deployment checklist

Run these against a real guest. E0 gates everything else: it is the check the
2026-07-22/23 evidence could not make, because that testing used userland
`smbclient` rather than the kernel CIFS client the real mount uses.

### E0 — kernel-CIFS read/write gate (do this first)

With `/mnt/icloud` mounted by `host/setup-host.sh`:

1. Pick a file the guest reports as online-only (`RECALL_ON_DATA_ACCESS` —
   `tools/icloud-status.ps1` counts them) of at least 100 MB, whose SHA-256 you
   know from another Apple device or a separately downloaded copy, so the mount
   read is genuinely the first hydration. Run
   `time timeout 30m sha256sum /mnt/icloud/<file>`. It must finish without EIO,
   hang or timeout, and the hash must match. Record the size and elapsed time.
2. Repeat with a multi-GB online-only file and a deliberately generous timeout.
   It must complete and hash correctly. Record the sustained rate, and decide
   whether a read that blocks that long is tolerable for your workflow.
3. Write a uniquely named disposable file on the mount. Confirm it appears on
   iCloud.com or another Apple device and that its hash matches. Edit that same
   file on the host and confirm the new hash. Then delete it from the host and
   confirm the deletion propagates. Allow up to five minutes per step. Do not
   use an existing file of yours for this.

If either read fails at the kernel CIFS layer, **stop** — investigate mount and
client timeout behaviour before going further. If the write, edit or delete
fails, **stop**: the bidirectional design itself is not working.

### E1–E7 — selective sync

Use a disposable folder of test files, never real data, for anything
destructive.

- **E1** Create a disposable cloud folder with several files, confirm it exists
  on another Apple device, read it once so it hydrates, then exclude it. Within
  two minutes it vanishes from `ls /mnt/icloud`; a known direct path cannot be
  read; the guest shows both the target deny and the parent delete-child guard;
  the state progresses `applying` → `pending-dehydrate` → `applied`; guest free
  space eventually grows.
- **E1b** Repeat the exclude → re-include cycle for each of: an item created in
  the cloud, an item created on the host through the mount, and at least one
  **top-level** item (which forces the guard onto the sync root itself). All
  three must reach `applied` and re-include cleanly. Any `acl-write-denied` here
  means provisioning step 4 did not take.
- **E2** Against that exclusion, confirm every one of read, overwrite, `rm`,
  rename, `mkdir`/`touch` at the same name, and the same name in a different
  letter case fails with permission denied. Then create and delete an *included*
  sibling in the same parent successfully — proof the guard did not break
  ordinary operations.
- **E3** Re-include → within two minutes it reappears as online-only
  placeholders; reading a file hydrates it byte-identically; the item was never
  absent from iCloud.com during the cycle.
- **E4** Modify a disposable file on the host, then immediately exclude its
  parent. The state must stay `pending-dehydrate` while Windows reports the
  content modified or not in sync, and must only become `applied` after the
  edited hash is present on another Apple device. If the edit is lost, **stop**.
- **E5** Create a file from an iPhone in an *included* folder → listed on the
  host within two minutes and readable on demand. Create one at an *excluded*
  path → hidden within two minutes, never hydrated.
- **E5b** Fill the guest disk with disposable hydrated files until free space
  drops below 20 GB (or use test-only thresholds). `sweep.inProgress` becomes
  true and `requestedBytes` grows; over several cycles free space reaches 30 GB
  and `freedBytes` reflects the real increase; an evicted file re-reads with the
  same hash. Separately confirm an open or not-in-sync file is skipped without
  data loss. Then test stage 2 explicitly: create a *partially* hydrated file by
  reading only the first bytes of a large online-only file, leave no fully local
  candidates, and confirm the sweep still finds and reclaims it rather than
  reporting nothing eligible.
- **E6** `docker stop icloud-windows` → tray red within 15 s; start again →
  green. Stop the guest scheduled task → tray yellow within 2 min.
- **E7** Reboot host and guest → the agent auto-starts and exclusions are still
  enforced (hidden, protected, no local content) with no manual action.
  Launching the desktop entry while the tray is already running raises the
  existing window instead of silently doing nothing.

---

## Where things live

| | |
|---|---|
| Exclusion list | `/mnt/icloud_bridge/exclusions.json` (written by the GUI) |
| Agent status | `/mnt/icloud_bridge/status.json` (every 15 s) |
| Folder tree | `/mnt/icloud_bridge/tree.json` (every 10 min) |
| Agent script (guest) | `C:\ProgramData\icloud-bridge\agent.ps1` |
| Agent private state (guest) | `C:\ProgramData\icloud-bridge\state\` — not shared over SMB |
| Scheduled task (guest) | `icloud-bridge-agent`, runs as `icloud`, unelevated |

All three JSON files are plain UTF-8 and safe to `cat` when something looks
wrong. Editing `exclusions.json` by hand works, but the revision number must
increase on every change or the agent will refuse it and report an error.
