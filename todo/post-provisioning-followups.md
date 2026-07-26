# Todo: Follow-ups after app-driven guest provisioning

Handoff written 2026-07-27, after the automated-guest-provisioning plan was
executed, verified, and archived. Read `CONTRIBUTING.md` completely before
touching anything; it governs every rule referenced below (pathspec commits,
shared-session etiquette, verification, and the plans-own-decisions rule).

**Items 2, 3 and 4 landed on 2026-07-27** (`4bd83e1`, `56c2c32`, `d4caba3`); see
`todo/archive/post-provisioning-followups.md` for what each one did and for two
corrections it turned up. What remains here is the operator-only work, plus one
unanswered question.

## Context you need before starting

- The feature landed as commits `3b3d43e..cb569bf` plus `8e03467` (which
  released the held `Makefile` and `SETUP.md` work; that commit also carries a
  parallel session's `DOCKER_HOST` pin and `vm-*` wrappers — deliberate,
  operator-approved).
- The specification of record is `docs/plan-gui-selective-sync.md` D40-D44 and
  its sections 4.1 (protocol) and 4.2 (reconciliation).
  `todo/archive/automated-guest-provisioning.md` is investigation history only —
  except its milestone 6 (M5), which is still the pending operator-verification
  checklist.
- Everything provable in this workspace is green and was verified twice
  (once by the executing session, once independently): `make check`,
  `make test-all`, `make lint-ps` (which now auto-runs
  `packaging/test-guest-state.ps1`), `make deb` payload inspection, and a
  mechanical four-vocabulary seam check (check states, check IDs, work IDs,
  phase list and order) across `provision/guest-state.ps1`,
  `provision/guest-setup.ps1`, and `gui/icloud_bridge_gui/guestprov.py`.
- Nothing Windows-side has ever executed. `make lint-ps` proves PS 7 syntax
  only.

## Live-host facts (as of 2026-07-27 — re-verify before relying on them)

This checkout sits on the author's live host, which also runs the
`icloud-windows` container. Confirmed on 2026-07-26/27:

- Host setup IS installed (deb + `icloud-bridge-configure`, done ~00:37):
  systemd units, `/etc/credentials-icloud` (0600 root), the power helper, the
  GUI. The installed copy predates the provisioning feature and must be
  refreshed before item 1.
- The guest is UNPROVISIONED: `smbclient -L` on 127.0.0.1:10445 shows only
  ADMIN$/C$/icloudtest/IPC$ — no `icloud`, no `bridge` share. Scripts 03/04
  never ran there. Both mount units therefore fail with `mount error(2)`
  ("no such share name"); that is expected until provisioning succeeds.
- Apple iCloud is already signed in inside the guest, so a provisioning run
  should find the sync root present and skip the sign-in wait.
- The guest has no watcher task: this VM is exactly the "pre-feature VM"
  bootstrap case (SETUP.md §8, the 90-second no-acknowledgement hint).
- `.env`'s `SHARE_PASS` already authenticates as `syncshare` against the guest
  (verified 2026-07-27), so the host credentials file will work the moment the
  shares exist.
- Sudo limits: passwordless sudo covers only `/usr/sbin/ip`. Every other root
  command must be typed by the operator — suggest it as `! sudo ...` so it runs
  in-session.
- SMB probing recipe (no samba-client on the host): throwaway alpine container,
  `--network host`, `apk add samba-client`,
  `smbclient -L //127.0.0.1 -p 10445 -U "syncshare%$PW"` with the password via
  environment, never argv.

## Items

1. **M5 — operator verification on this host.** The one substantive piece the
   plan could not do. Interactive: the GUI needs the operator's display and the
   guest needs their eyes.
   - `dist/icloud-bridge_0.2.0_all.deb` was rebuilt 2026-07-27 at 11:49 and
     carries the D45 helper; rebuild only if the tree has moved since. Have the
     operator install it and re-run `sudo icloud-bridge-configure` if it asks
     (`! sudo apt install ./dist/icloud-bridge_0.2.0_all.deb`).
   - Then walk milestone 6 of `todo/archive/automated-guest-provisioning.md`
     steps (a)-(i) in order: Provision-share read-only proof, the pre-feature
     bootstrap one-liner, phase/checklist watching with two deliberate GUI
     interruptions (D43 resumption), share existence + `agentBuild` match +
     no D35 banner, `./host/acceptance-tests.sh`, a controlled agent-only
     drift, a safe share repair without password reset plus one explicit
     reset, one blocked fixture proving no mutation, and — later, as its own
     exercise — the full OEM rebuild path.
   - Expect this VM to skip `wait-for-signin` (already signed in) and to need
     the watcher bootstrap hint (no watcher registered).
   - Record results in `CHANGELOG.md` (append, do not reflow) and the
     `docs/automation-notes.md` scoreboard.

   - The M5 run now also exercises D45's failure surfacing: a start that times
     out should show the mount units' own error under the generic sentence, and
     a deliberately wrong `SHARE_PASS` should reach the credential route rather
     than the generic red banner. That path has never run against a live
     journal.

4. **Housekeeping — one question left.** `master` is ~40 commits ahead of
   `origin/master`. Never push without the operator's explicit request — ask
   whether they want it pushed. (Asked 2026-07-27; unanswered.)
