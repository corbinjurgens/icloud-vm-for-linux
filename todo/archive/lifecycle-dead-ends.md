# Archive: remove the lifecycle dead ends

Records items completed from `todo/lifecycle-dead-ends.md`. The live note keeps
only what is still outstanding (item 3 as of 2026-07-28).

## 2026-07-28 — items 1, 2, 4, 5, 6, 7, 8 and 9 (checkout-executable work)

Executed as one scheduled run (`.claude/plan-runs/todo-backlog-2026-07-28/`,
baseline `e55920e`); each item's CHANGELOG entry under 2026-07-28 carries the
detail. Items 1-2 and the beacon half of item 9 still need their live pass on
the real host — repository tests cannot substitute for it, and each commit says
so.

- **Items 1+2** — `549ddf5`. The controller classifies D45's bounded helper
  excerpt before dispatching (`POWER_ON_FAILED_VM_NOT_STARTED` /
  `_SHARES_UNAVAILABLE` / `_CREDENTIAL_REJECTED`); the banner heading follows
  the failure kind, and the `mount error(2)` case offers **Set up Windows
  automatically** from `START_FAILED` (and from `RUNNING` when both mounts are
  absent with Docker definitively running), entering first-run provisioning and
  writing the D39 intent record at that moment. Recorded as D48, amending
  D30/D39. `lifecycle.py` stayed a pure reducer.
- **Item 4** — `5ab702d`. `gui/install-gui.sh --uninstall` removes the
  launcher, both desktop entries, the autostart entry, and the app tree
  (containing the icon and venv); idempotent. Supersedes the interim
  `rm ~/.config/autostart/icloud-bridge-tray.desktop` fix.
- **Item 5** — `bf5dbcd`. SETUP.md is app-first: GUI install is §6, **Create
  Windows VM** is §7, bare compose commands survive only in warned recovery
  appendices; live cross-references to the renumbered sections were fixed in
  the same commit (historical CHANGELOG entries and this archive keep the
  numbering that was true when written).
- **Item 6** — `eabe756`. `firstrun.cached_windows_install_media()` checks
  `/srv/icloud-vm/storage/custom.iso` and `_confirm_create_vm` words the cost
  honestly (cached: a few minutes, no download).
- **Item 7** — `7380fb6`. **Create configuration** is the default Setup action:
  exclusive-create 0600 file at `$XDG_CONFIG_HOME/icloud-bridge/env` with a
  generated 32-character alphanumeric `SHARE_PASS` and validated,
  machine-derived, editable sizes; an existing conventional file is found and
  reused, never rewritten. Recorded as D49, narrowly amending D41's "never
  persists". **Use an existing .env** remains the manual path.
- **Item 8** — `cca5a3b`. `VM_CREATED` flows into
  `_start_first_provisioning_run()` automatically (the click remains for
  re-entry), and `notify.ProvisioningTracker` sends one notification per run at
  waiting-for-signin, failure, and completion.
- **Item 9** — `2214697`. The watcher writes an untrusted `watcher.json` beacon
  (task name, agent build, registered-at) to the Data outbox at `-Install` and
  task start; `guestprov.read_watcher_beacon()` validates it before staging and
  the app leads with the bootstrap hint immediately when it is absent, keeping
  the 90 s fallback for pre-beacon watchers. The hint leads with the
  noVNC-typeable `powershell -ep bypass -File C:/OEM/watcher.ps1 -Install`,
  keeps the UNC form for pre-feature VMs, and mentions RDP on 127.0.0.1:3389.
  §4.1's protocol table documents the beacon; a seam test pins watcher.ps1's
  `$AgentBuild` copy to `bridge.AGENT_BUILD`.

Corrections the run turned up, worth remembering:

- Codex-generated units twice reported Qt-wiring tests as passing that had
  never executed (the ordinary venv skips them without PySide6). Always run
  `.venv-qt/bin/python -m pytest gui/tests/test_qt_wiring.py` before believing
  a wiring claim.
- The Qt fixture teardown drained only one worker generation; a done-callback
  could schedule one more worker (a pending forced refresh does), which then
  ran into the next test's fakes — seen as an intermittent failure of the
  no-CIFS-resume test in combined runs. Fixed in `eabe756` by draining twice.

## 2026-07-28 — item 10 (decided: rejected)

The operator rejected adding a host->guest execution channel (QEMU guest
agent virtio-serial plus guest-side `qemu-ga` giving SYSTEM-level
`guest-exec`, or WinRM/SSH with a stored admin credential). Recorded as R-040
under "Visited ideas: closed" -> "Architecture, data safety and lifecycle" in
`CHANGELOG.md`; no code or plan-register change, since rejecting needs only
the Closed entry the item itself specified.

Rejection rationale, as recorded in `CHANGELOG.md`: every such channel widens
the host's power over a guest holding a live Apple session, converting the
deliberately pull-only surface into one where the host can execute code, or
holds an admin credential, inside that session. The 2026-07-27 fix chain was
diagnosed and shipped entirely over the pull-only surface, which is direct
ROI evidence against adding a channel. The 2026-07-28 watcher-presence beacon
(`2214697`) further narrows the gap a channel would close, by detecting a
missing watcher before staging instead of discovering it by timeout. And a
channel installed at OEM time can only be relied on by VMs whose OEM step
already worked — the same class whose watcher registration already works —
so its marginal value is repairing broken watchers on established VMs, not
first bootstrap.
