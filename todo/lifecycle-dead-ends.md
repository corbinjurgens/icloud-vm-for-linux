# Todo: Remove the lifecycle dead ends

Written 2026-07-27 after the M5 walkthrough attempt went in circles. Read
`CONTRIBUTING.md` completely before touching anything. This note is
non-authoritative: items 1 and 2 change locked lifecycle behaviour and need
new decisions in `docs/plan-gui-selective-sync.md` §1 before implementation.

## Goal

The operator's directive, verbatim in intent: **the app should smoothly handle
everything.** From any state it finds — no container, a container created
outside the app, a booted guest with no shares, a half-provisioned guest, a
healthy bridge — launching the GUI must land on a screen whose primary action
actually moves the operator forward. No state may require the terminal, and no
state may offer only an action that cannot succeed.

## What happened (2026-07-27, incident record)

The rebuilt VM was created with `make vm-up` instead of through the app, so no
provisioning record was written. The app then classified the running container
as an ordinary bridge, power-on failed on the missing shares, and the operator
was left with a red "The Windows VM did not start." banner whose only action
was Retry — which can never succeed on an unprovisioned guest. There was no
in-app route to provisioning at all:

- The Setup tab appears only on `PROVISION_NEEDED`, which requires **no
  container** (`power.py` `plan_startup`, "absent" branch).
- **Re-run Windows provisioning…** requires `Phase.RUNNING` with a live
  container (`__main__.py` `_can_reprovision`) — which requires the mounts,
  which require the shares, which require provisioning. Circular.
- **Set up Windows automatically** requires `Phase.PROVISIONING`
  (`_can_provision`), reachable only from the Setup tab or a matching record.

The escape was manual: remove the container so the app would re-enter
`PROVISION_NEEDED`. That worked, but it is exactly the kind of terminal
surgery the GUI exists to eliminate. SETUP.md itself steers operators into
this dead end: it says `docker compose up -d` as an operator step in several
places (§6 and elsewhere), and any container created that way is invisible to
the record-driven classifier.

## Root cause

Startup classification is **history-driven, not state-driven**. The app
decides its phase from what *it* previously did (the durable provisioning
record, D39/D43) plus container presence — not from what is actually true of
the guest. Any state the app did not itself create is misclassified as either
"fresh" or "healthy", and both misclassifications strand the operator. The
guest-side inspect-and-reconcile machinery (D44) already knows how to survey
real guest state; the host-side phase model simply has no route into it except
from `PROVISION_NEEDED` or `RUNNING`.

## Items

Numbering is stable; completed items are removed without renumbering.

1. **A start that fails on missing shares must offer Setup, not just Retry.**
   The minimal state-driven fix, and the one that breaks the circle: when
   power-on fails and the mount excerpt shows `mount error(2)` (no such share
   name) — the D45 helper already extracts and classifies this — the
   `START_FAILED` surface should offer **Set up Windows automatically**
   alongside Retry, entering `Phase.PROVISIONING` with `MODE_FIRST_RUN` and
   writing the record at that moment. This makes a container created outside
   the app (or a guest whose shares were lost) recoverable in-app with one
   click. Needs a new decision: it adds a transition
   `START_FAILED -> PROVISIONING` to the D30 lifecycle and widens D39's
   record-writing sites. Consider the same offer from `RUNNING` when both
   mounts are absent and Docker is definitively `running` (the drifted case),
   so `_can_reprovision`'s `Phase.RUNNING` gate stops being circular.

2. **The start-failed banner heading must follow the failure kind.**
   `_fx_show_start_failed_banner` (`__main__.py`) hardcodes "The Windows VM
   did not start." above whatever detail the helper returned — even when the
   helper's own text says the VM *is* running and only the shares are missing.
   That heading is what made a share problem look like a VM problem worth
   rebuilding for. Derive the heading from the D45 classification: VM did not
   start / VM running but shares unavailable / credential rejected. Pairs
   naturally with item 1, since the shares-missing variant is where the Setup
   button belongs.

3. **The quit gate must not wedge on provisioning probes.**
   Observed live: "A file operation on the icloud mount is still in progress"
   with nothing in flight, GUI unquittable, resolved only by signal. Plausible
   mechanism (unproven — instrument before fixing): `_fx_stop_polling` stops
   only `self._timer`, while `_prov_timer` (3 s) and `_prov_probe_timer`
   (15 s) keep firing Docker probes through `run_async` during the shutdown
   drain, each incrementing the same `_active` counter the drain waits on.
   Reproduce, then stop all periodic sources when leaving their phases, and
   consider excluding non-CIFS probe work from the drain gate entirely — the
   gate exists to protect mount I/O (D29), not `docker inspect`.

4. **`gui/install-gui.sh` needs `--uninstall`.**
   It has no removal path, so the per-user install on the author's machine was
   removed by hand and left `~/.config/autostart/icloud-bridge-tray.desktop`
   pointing at a deleted `~/.local/bin/icloud-bridge-gui`. XDG resolves
   autostart user-dir-first, so the orphan shadows the package's working
   `/etc/xdg/autostart` entry and the tray silently never starts at login.
   Mirror `tools/install-hooks.sh --uninstall`: remove the launcher, the
   desktop entries, the autostart entry, and the icon. On the live host,
   `rm ~/.config/autostart/icloud-bridge-tray.desktop` is the interim fix.

5. **SETUP.md must be app-first.**
   Every place the runbook has the operator run `docker compose up -d` by
   hand creates the item-1 dead end (no record, no Setup tab). Reorder so the
   GUI is installed first and **Create Windows VM** is the documented way to
   bring the container up; keep the bare compose commands only in the
   troubleshooting/appendix sections, with a warning that a hand-created
   container needs item 1's recovery route (or, until item 1 lands, container
   removal). This is the documentation half of the same root cause.

6. **First-run dialog wording when media is cached.**
   The Create-VM confirmation always warns "several gigabytes" and "20-40
   minutes". When `custom.iso` is already present in the storage directory
   the real cost is a few minutes and zero download. Cheap check, honest
   dialog. Cosmetic next to the others, so last.

7. **The app should create the env file itself; choosing one becomes the
   advanced path.** The Setup tab currently blocks on "choose the .env file",
   and `check_env`'s failure hint is `cp .env.example .env  # then edit it` —
   terminal work at the exact moment the GUI is supposed to be taking over.
   Operator observation, 2026-07-27: allow picking a file up front for those
   who have one, but by default the app should just make one. Nothing in the
   file needs a human: `SHARE_PASS` is a machine-to-machine credential — the
   app already delivers it to the guest over `docker exec` stdin (D41) and
   `icloud-bridge-configure` reads it from the file, so the operator never
   types or sees the value, and a generated high-entropy password is strictly
   better than a hand-chosen one (and removes the placeholder-not-replaced
   failure mode entirely). `DISK_SIZE`/`RAM_SIZE`/`CPU_CORES` can default from
   the machine (and become editable fields on the Setup tab). Sketch: a
   "Create configuration" default action writes a 0600 file at a fixed XDG
   location (e.g. `~/.config/icloud-bridge/env`), which also retires the
   ask-again-on-resume dance, since a conventional location can be found
   rather than remembered; "Use an existing .env…" remains for the manual
   SETUP.md flow. Needs a decision: D41's "never persists" language and
   `firstrun`'s no-env-path-persistence rule were written for the
   operator-owned-file world and must be amended deliberately — the GUI would
   now write the secret exactly once, at creation, and still never log,
   display, or re-read it outside the existing D41 channel.

## Constraints

- Items 1-2 first: they are the operator-facing circle-breakers, and item 2's
  heading logic feeds item 1's button placement.
- `lifecycle.py` stays a pure reducer; the share-missing classification is
  computed in the controller from the D45 excerpt and dispatched as an event.
- All the D29-D31 hard rules in CONTRIBUTING.md hold: no CIFS in no-CIFS
  phases, only explicit actions power off, `power.py` stays Qt-free.
- Verification: `make check` plus targeted `gui/tests` additions per item;
  items 1-3 also need a live pass on the real host, which only the operator
  can run. State that limitation in each commit.
