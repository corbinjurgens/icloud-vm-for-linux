# Todo: Remove the lifecycle dead ends

Written 2026-07-27 after the M5 walkthrough attempt went in circles. Read
`CONTRIBUTING.md` completely before touching anything. This note is
non-authoritative.

**Status 2026-07-28:** items 1, 2, 4, 5, 6, 7, 8 and 9 shipped and were
archived with their commit hashes into
[`archive/lifecycle-dead-ends.md`](archive/lifecycle-dead-ends.md), which also
keeps the incident record and root-cause analysis they resolved (D48/D49 in
`docs/plan-gui-selective-sync.md` are the decisions they added). Items 1-2 and
item 9's beacon still await their live pass on the real host. What remains
below is item 3 (operator chose live instrumentation before any fix, asked
2026-07-28) and item 10 (a decision only the operator can record).

## Goal

The operator's directive, verbatim in intent: **the app should smoothly handle
everything.** From any state it finds — no container, a container created
outside the app, a booted guest with no shares, a half-provisioned guest, a
healthy bridge — launching the GUI must land on a screen whose primary action
actually moves the operator forward. No state may require the terminal, and no
state may offer only an action that cannot succeed.

## Items

Numbering is stable; completed items are removed without renumbering.

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
   Operator decision 2026-07-28: reproduce live first; no defensive fix before
   instrumentation.

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
    watchers on established VMs, not first bootstrap.

    The wait condition is met and this item is ready to be decided. The
    2026-07-27 fix chain was diagnosed and shipped entirely over the pull-only
    surface (see the archive), which is direct evidence for this item's own
    ROI argument against adding a channel. The accept-or-reject call is not
    this note's to make (plans own decisions): accepting means a new register
    row in `docs/plan-gui-selective-sync.md`; rejecting means a **Closed**
    entry in `CHANGELOG.md` recording the cost and the widened host privilege
    over a guest holding a live Apple session. Until that entry exists the
    question is open, not answered. Note item 9's beacon (shipped 2026-07-28,
    `2214697`) further narrows the gap this channel would close: the app now
    detects a missing watcher before staging instead of discovering it by
    timeout.

## Constraints

- All the D29-D31 hard rules in CONTRIBUTING.md hold: no CIFS in no-CIFS
  phases, only explicit actions power off, `power.py` stays Qt-free.
- Verification: `make check` plus targeted `gui/tests` additions per item;
  item 3 also needs a live pass on the real host, which only the operator
  can run. State that limitation in each commit.
