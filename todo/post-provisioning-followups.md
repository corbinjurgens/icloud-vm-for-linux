# Todo: Follow-ups after app-driven guest provisioning

Handoff written 2026-07-27, after the automated-guest-provisioning plan was
executed, verified, and archived. Read `CONTRIBUTING.md` completely before
touching anything; it governs every rule referenced below (pathspec commits,
shared-session etiquette, verification, and the plans-own-decisions rule).

**Items 2, 3 and 4 landed on 2026-07-27** (`4bd83e1`, `56c2c32`, `d4caba3`); see
`todo/archive/post-provisioning-followups.md` for what each one did and for two
corrections it turned up. Item 4's push question was answered 2026-07-28 (not
pushed) and is archived alongside it. What remains here is the operator-only
work in item 1.

## Context you need before starting

- The feature landed as commits `3b3d43e..cb569bf` plus `8e03467` (which
  released the held `Makefile` and `SETUP.md` work; that commit also carries a
  parallel session's `DOCKER_HOST` pin and `vm-*` wrappers — deliberate,
  operator-approved).
- The specification of record is `docs/plan-gui-selective-sync.md` D40-D44 and
  its sections 4.1 (protocol) and 4.2 (reconciliation).
  `todo/archive/automated-guest-provisioning.md` is investigation history only —
  except its milestone 6 (M5), which is still the operator-verification
  checklist (partly exercised on 2026-07-27 — see item 1 for what is left).
- Everything provable in this workspace is green and was verified twice
  (once by the executing session, once independently): `make check`,
  `make test-all`, `make lint-ps` (which now auto-runs
  `packaging/test-guest-state.ps1`), `make deb` payload inspection, and a
  mechanical four-vocabulary seam check (check states, check IDs, work IDs,
  phase list and order) across `provision/guest-state.ps1`,
  `provision/guest-setup.ps1`, and `gui/icloud_bridge_gui/guestprov.py`.
- This note originally said nothing Windows-side had ever executed.
  **Corrected 2026-07-27 (evening):** the guest has since executed the OEM
  bootstrap, the watcher, scripts 03/04 and the agent, live — see the Live-host
  facts below. `make lint-ps` still proves PS 7 syntax only, and the fix chain
  that live run produced (`CHANGELOG.md`, "Shipped improvements", 2026-07-27)
  is the standing evidence that green local checks say nothing about PowerShell
  5.1 runtime behaviour.

## Live-host facts (as of 2026-07-27, late — re-verify before relying on them)

This checkout sits on the author's live host, which also runs the
`icloud-windows` container. Confirmed on 2026-07-26/27:

- Host setup IS installed (deb + `icloud-bridge-configure`, done ~00:37):
  systemd units, `/etc/credentials-icloud` (0600 root), the power helper, the
  GUI. The installed GUI carried the provisioning feature by the time of the
  first app-driven run, but `dist/icloud-bridge_0.2.0_all.deb` has been rebuilt
  since (2026-07-27 22:08). Refresh it before further item-1 work; the version
  digits do not move, so use `make reinstall`.
- The guest IS provisioned, through the app-driven D40-D44 path. The first
  app-driven run reached the guest on 2026-07-27 and converged after that day's
  fix chain; each fault and its live evidence is a separate entry under
  "Shipped improvements" in `CHANGELOG.md` (pure-ASCII guest scripts, the
  status-writer UNC accommodation, the `File.Replace` null, the bridge scope
  derived after the share stage, `cldapi.dll`, long paths and the guest clock,
  the iCloud process names, and watcher run-acceptance). Do not re-explain
  those here; read them.
- The agent publishes status. `$AgentBuild` in `guest-agent/agent.ps1` is now
  **7**. The last build confirmed inside a live `status.json` is 4 (the
  `cldapi.dll` entry: both shares mounted, `agentBuild 4`,
  `icloudClientRunning true`, `lastError null`); builds 5, 6 and 7 landed later
  the same evening, with 5 and 6 verified against the live guest and library.
  Check what the guest actually reports before assuming 7.
- The first full scan of the real library completed: **60 154 entries**, ending
  `lastError: none` in 29 s once ACL reconciliation used the `\\?\` form.
- Shares `icloud` and `bridge` exist and the host mounts both; the earlier
  `mount error(2)` ("no such share name") state is gone.
- The watcher task is registered and running, and self-restarts into refreshed
  code. Its inability to *record* run acceptance — the failure that made the
  first run look like a guest with no watcher at all — was fixed the same day.
  This VM is no longer the "pre-feature VM" bootstrap case (SETUP.md §9).
- The guest clock was corrected in place (its UTC had been seven hours ahead of
  the host's); `01-debloat.ps1` now sets `RealTimeIsUniversal` and the UTC zone
  at install.
- Apple iCloud is signed in inside the guest, so a re-provisioning run should
  find the sync root present and skip the sign-in wait.
- `.env`'s `SHARE_PASS` authenticates as `syncshare` against the guest
  (verified 2026-07-27), and matches the host credentials file.
- Sudo limits: passwordless sudo covers only `/usr/sbin/ip`. Every other root
  command must be typed by the operator — suggest it as `! sudo ...` so it runs
  in-session.
- SMB probing recipe (no samba-client on the host): throwaway alpine container,
  `--network host`, `apk add samba-client`,
  `smbclient -L //127.0.0.1 -p 10445 -U "syncshare%$PW"` with the password via
  environment, never argv.

## Items

1. **M5 — the operator-only remainder.** The first app-driven provisioning run
   completed against the live guest on 2026-07-27, after the day's fix chain
   (each fault and its live evidence is a 2026-07-27 entry under "Shipped
   improvements" in `CHANGELOG.md`; I-012 records the run's own phase timings —
   `inspecting` 82 s, `verifying` 81 s, on the 60 000-entry library). That
   covers milestone 6's steps (b) and (d) of
   `todo/archive/automated-guest-provisioning.md`: the bootstrap one-liner was
   exercised, and both shares plus a status-publishing agent were confirmed from
   the host.

   The sub-steps below have **no recorded evidence of having run** and are
   therefore still operator work. Some of them may in fact have been exercised
   during the live session; nothing was written down, so please confirm which,
   rather than letting an unrecorded pass read as a gap:
   - (a) the `Provision` share read-only proof: `testparm -s` showing it
     read-only, a create/replace through `\\host.lan\Provision` failing, and a
     status write through `Data` succeeding.
   - (c) the two deliberate GUI interruptions — one during `waiting-for-signin`,
     one immediately after secret delivery — proving D43 resumption and
     re-delivery.
   - (e) `./host/acceptance-tests.sh` passing.
   - (f) a controlled agent-only drift: the proposed work contains only
     `update-agent`, no env chooser appears, the boundary repair scope does not
     run, and postflight returns every other check unchanged.
   - (g) one safely repairable share drift repaired without a password reset,
     then the explicit reset option exercised.
   - (h) one blocked fixture that risks no user data, proving no mutation
     occurs, then corrected and converged through the app's retry.
   - (i) the full OEM rebuild path, as its own later exercise. The first live
     OEM install did run (it is what surfaced the PS 5.1 ASCII parse failure),
     but no run since the fixes proves the path end to end.

   The guest's state has moved under the old expectations: a further run still
   skips `wait-for-signin` (already signed in), but no longer needs the watcher
   bootstrap hint, because the watcher is registered — step (b) can only be
   replayed on a rebuilt VM.

   D45's failure surfacing is built and documented (the "rejected share
   password" entry in `CHANGELOG.md`), but whether the operator has seen either
   route fire live is unrecorded: a start that times out should show the mount
   units' own error under the generic sentence, and a deliberately wrong
   `SHARE_PASS` should reach the credential route rather than the generic red
   banner. Confirm or exercise both against a live journal.

   Record results in `CHANGELOG.md` (append, do not reflow) and the
   `docs/automation-notes.md` scoreboard.
