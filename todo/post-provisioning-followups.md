# Todo: Follow-ups after app-driven guest provisioning

Handoff written 2026-07-27, after the automated-guest-provisioning plan was
executed, verified, and archived. Read `CONTRIBUTING.md` completely before
touching anything; it governs every rule referenced below (pathspec commits,
shared-session etiquette, verification, and the plans-own-decisions rule).

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

Suggested order: 4 (minutes, any time) → 2 → 3 (both workspace-only) → rebuild
the deb once → 1 (needs the operator present). Landing 2 before 1 means the
operator installs once and the M5 run also exercises the improved failure
surfacing.

1. **M5 — operator verification on this host.** The one substantive piece the
   plan could not do. Interactive: the GUI needs the operator's display and the
   guest needs their eyes.
   - First refresh the installed copy: `make deb` (shared build state — do not
     run while another session is building), then have the operator install it
     and re-run `sudo icloud-bridge-configure` if it asks
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

2. **Surface credential-specific mount failures from the power helper.** The
   "Retry and reset share password..." route almost cannot fire today.
   - The gap: `gui/icloud_bridge_gui/__main__.py` matches
     `CREDENTIAL_FAILURE_MARKERS` (NT_STATUS_LOGON_FAILURE, "permission
     denied", ...) against the power helper's stderr to raise
     `PROVISION_CONNECT_FAILED` (`lifecycle.py`, the third door into a
     provisioning run's state). But `host/icloud-bridge-power`'s only
     mount-failure message is the generic ready-deadline text ("shares did not
     become usable within Ns"); the real CIFS error stays in the journals of
     `mnt-icloud.mount` / `mnt-icloud_bridge.mount`. So a wrong password looks
     identical to a slow guest, and the GUI shows the generic red banner
     instead of the credential route.
   - Fix shape: when the ready deadline expires, harvest a short, sanitized
     excerpt of the two mount units' recent journal entries and append it to
     the `die` message. Keep the exit-status contract (the GUI shows stderr
     and does not parse exit codes), keep the transaction semantics, and make
     sure no secret can appear in the excerpt.
   - Process: this is a behavioural change to a root helper on the privileged
     power path. Update the v2 plan in the same commit (the D29/lifecycle
     contract section and §4.2's "connecting is the proof" discussion), and if
     the surfacing policy warrants a decision row, claim the next free D-number
     at edit time and re-check it before commit. Extend
     `gui/tests/test_power_helper.py` and the connect-failure routing tests.
   - After landing, the installed helper is stale until the operator
     reinstalls — coordinate with item 1.

3. **Point the v1 plan's troubleshooting at app-driven re-provisioning.**
   `docs/implementation-plan.md` still instructs manual recovery in live
   guidance rows: ~line 217 (re-run `C:\OEM\01-debloat.ps1` after a feature
   update), ~304-305 (edit/run 03 from `C:\OEM`), ~425 ("Re-run scripts 01, 03
   and 04"), ~429 (re-run 04 elevated). The v2 plan wins on conflict and D35/D42
   route recovery through **Re-run Windows provisioning...**, with the manual
   sequence as a documented fallback run from
   `C:\ProgramData\icloud-bridge-provision\current` (not stale `C:\OEM`) once
   the app has provisioned the VM.
   - Update those rows to lead with the app action and demote the manual path
     to the fallback, cross-referencing SETUP.md §8 and D42 rather than
     duplicating them.
   - Leave historical records (`CHANGELOG.md` entries,
     `docs/acceptance-results.md`) as written. Add a changelog entry for the
     doc change.

4. **Housekeeping.**
   - Add `.claude/` to `.gitignore` (it holds `settings.local.json` and the
     plan-run ledger; both are machine/session-local, matching the existing
     `.mcp.json` / `.idea` entries). Commit the one-line change by pathspec.
   - Delete `dist/icloud-bridge_2.0.0_all.deb` — a stale artifact from before
     the deliberate 2.0.0 -> 0.2.0 renumber (`37a6fab`). `dist/` is ignored;
     this is disk cleanup only.
   - `master` is ~40 commits ahead of `origin/master`. Never push without the
     operator's explicit request — ask whether they want it pushed.
