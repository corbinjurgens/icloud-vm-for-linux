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

8. **Creating the VM should flow straight into the first provisioning run.**
   Operator observation, 2026-07-27: after **Create Windows VM** there is no
   indication of when the install finishes and no cue that anything can be
   done meanwhile — the operator polled the VM screen by hand, then clicked
   **Set up Windows automatically** manually. Both halves are already built;
   they are just not joined. The reducer's own comment says "both doors out
   of Setup lead to the same first run" (`lifecycle.py`, `_setup`,
   `VM_CREATED`), and `_begin_provisioning_run` -> `_probe_guest_os` already
   tolerates an installing guest: it polls until the container runs *and*
   Windows answers, shows "Windows is still installing", and stages the run
   by itself the moment the guest is ready. But the `VM_CREATED` dispatch
   (`__main__.py` `done` in the create-VM path) only changes phase — nothing
   begins the run, so the app sits silent until the click. Fix: after
   `VM_CREATED`, begin the first run automatically (the click remains for
   re-entry after an interruption). The confirmation the operator already
   gave for Create Windows VM covers the whole flow — its dialog text
   (`_confirm_create_vm`) and the provisioning explainer both describe one
   end-to-end sequence. Complement: desktop notifications for the moments a
   run needs the operator (`waiting-for-signin`, a failure, done) —
   `notify.IncidentTracker` currently serves only RUNNING-phase health
   incidents, and notifications are explicitly disabled through provisioning,
   which is exactly when the operator has tabbed away to wait.

9. **The app needs a positive signal that the watcher exists.**
   Observed live 2026-07-27 (fresh OEM guest whose `install.bat` ran to
   completion): a staged run sat unacknowledged 3+ minutes with the elapsed
   clock ticking, and the host had no way to distinguish "the guest is busy"
   from "nobody is listening" — the only signal is the 90-second timeout
   heuristic behind the bootstrap hint. The watcher could write a small
   beacon (task name, agent build, registered-at) to the Data outbox at
   `-Install` time and refresh it at task start; the app could then say
   *before* staging whether a watcher is present and, when absent, lead with
   the one-liner instead of a counter and a delayed hint. Needs the §4.1
   protocol table updated in the same commit. Check
   `C:\OEM\watcher-install.log` on the live guest first: if OEM registration
   failed there, that failure is the primary bug and this item is its
   detection.

   Amendment from the live walkthrough: the bootstrap hint's command must be
   *typeable through the web viewer*. The operator could neither paste into
   noVNC nor type a backslash (host keyboard layout vs the guest's `en-US`),
   so the UNC form `\\host.lan\Provision\watcher.ps1` was effectively
   unusable. On any OEM-built VM the identical script already sits at
   `C:\OEM\watcher.ps1`, and Windows accepts forward slashes, so the hint
   should lead with
   `powershell -ep bypass -File C:/OEM/watcher.ps1 -Install`
   (short, no backslash, no paste needed) and keep the UNC form only as the
   fallback for pre-feature VMs with no `C:\OEM` payload. Mention RDP
   (published on 127.0.0.1:3389 for exactly this) as the comfortable route:
   a real RDP client gives working clipboard and the operator's own
   keyboard layout.

10. **Evaluate a host->guest execution channel (QEMU guest agent) as a
    deliberate decision — or reject it in the register.** Operator question,
    2026-07-27, from living through item 9's failure mode: "can the app not
    handle the watcher bootstrap itself?" Today it cannot, structurally: the
    host->guest surface is deliberately pull-only (the guest fetches from the
    host's shares; the host executes nothing in Windows and holds only the
    low-privilege share credential). Verified on the live VM: QEMU runs with
    a human-facing `-monitor` socket only — no `org.qemu.guest_agent.0`
    virtio-serial channel — so there is no existing hook. Closing the gap
    would mean adding one of: a qga channel (compose `ARGUMENTS` +
    guest-side qemu-ga service, giving SYSTEM-level `guest-exec`), WinRM/SSH
    with a stored admin credential, or monitor-socket keystroke injection
    (rejected out of hand: blind typing into whatever has focus). All of
    them widen the host's power over a guest that holds a live Apple
    session, which is why this needs a register decision either way. Honest
    ROI note: a channel whose guest half is installed at OEM time can only
    be relied on by VMs whose OEM step worked — the same class whose watcher
    registration already works — so its marginal value is repairing broken
    watchers on established VMs, not first bootstrap. Items 8-9 (verified
    OEM registration, loud immediate detection, one-liner led with) may be
    the better spend; decide once the `watcher-install.log` from the live
    failure is known.

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
