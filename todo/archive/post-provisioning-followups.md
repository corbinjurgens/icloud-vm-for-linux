# Archive: follow-ups after app-driven guest provisioning

Records items completed from `todo/post-provisioning-followups.md`. The live
note keeps only what is still outstanding.

## 2026-07-27 — items 2, 3 and 4 (workspace-only work)

Executed in the order the handoff suggested: 4, then 2, then 3, then one deb
rebuild. Item 1 (M5 operator verification) is untouched and remains in the live
note; it needs the operator's display and eyes.

### Item 4 — housekeeping (`4bd83e1`)

- `.claude/` added to `.gitignore`. It was already excluded through
  `.git/info/exclude`, which is local to this clone only; the `.gitignore` entry
  makes it shared, matching the existing `.mcp.json` / `.idea` lines. That local
  exclude was left alone (it also carries `AGENTS.md` and `CLAUDE.md`, which are
  another session's business).
- `dist/icloud-bridge_2.0.0_all.deb` deleted — stale artifact from before the
  2.0.0 -> 0.2.0 renumber (`37a6fab`). `dist/` is ignored; disk cleanup only.
- The push question is **not** resolved and stays in the live note.

### Item 2 — credential-specific mount failures (`56c2c32`, decision D45)

`icloud-bridge-power`'s readiness timeout now appends a filtered excerpt of the
`mnt-icloud.mount` / `mnt-icloud_bridge.mount` journals to its `die` message, so
the GUI's `CREDENTIAL_FAILURE_MARKERS` can see the real CIFS error and the
**Retry and reset share password…** route can actually fire.

- `sanitize_journal_excerpt` is a pure function of one string, deliberately
  shaped like `classify_inspect_output` so `gui/tests/test_power_helper.py` can
  extract and run it under bash. Allowlist keeps only mount/CIFS diagnoses;
  systemd's restatements ("Failed to mount ...", "Failed with result ...") are
  excluded so one attempt's noise cannot push out the next attempt's diagnosis.
  Tail-preferred, 4 lines x 200 chars per unit, password-bearing `key=value`
  redacted case-insensitively.
- `mount_failure_excerpt` collects it: `command -v journalctl` guarded, bounded
  by `timeout`, read-only. Both command substitutions carry `|| var=""` so
  `set -e` cannot let a diagnostic abort the transaction it is explaining.
- Unchanged on purpose: `EXIT_READINESS`, the marker/teardown sequence, no new
  `==> ` phase line (so D38's paragraph needed no edit), and no GUI code change —
  the existing marker matching already routes it.
- Plan updated in the same commit: D45 row, §5.1's `on` bullet, and a new §4.2
  bullet under the "connecting is the proof" discussion.
- Tests: six new bash-level cases plus a structural one in
  `test_power_helper.py`, and a Qt wiring test driving a readiness timeout that
  quotes `mount error(13)` through to the reset offer. The existing generic
  connect-failure test is untouched and still proves the negative.

### Item 3 — v1 plan troubleshooting rows (`d4caba3`)

`docs/implementation-plan.md` §5, §7 and the two §10 rows now lead with
**Re-run Windows provisioning…** and demote the manual scripts to the documented
fallback, cross-referencing SETUP.md §8 and D42 instead of duplicating them. §7's
"which is the whole reason this step is not automated (D10)" was corrected to
note D41's amendment. Historical records left as written.

Two findings from checking the claims against the code rather than the handoff:

- **A re-provisioning run does not cover debloat.** The §4.2 checklist has no
  entry for trimmed inbox apps, and `guest-setup.ps1` dispatches only 03 and 04.
  §4 and the feature-update runbook row say so and keep `01-debloat.ps1` manual.
- **Scripts 01 and 02 are not in the refreshed bundle.** `$ProvisionPayload`
  (`provision/guest-state.ps1`) is 03, 04, `agent.ps1`, `guest-state.ps1`,
  `guest-setup.ps1`, `watcher.ps1`. So "run from
  `C:\ProgramData\icloud-bridge-provision\current`" is correct for 03/04 and
  wrong for 01/02, which live only in `C:\OEM`. An earlier draft of the §4/§5
  edits made that wrong claim and was corrected before commit.

Note for whoever revisits SETUP.md: its §8 says `C:\OEM` "is never updated
afterwards". `guest-setup.ps1` does now refresh `C:\OEM` with `$ProvisionPayload`
for inspection only (never as an execution source, D42). The sentence is still
true of 01/02 and of a VM the app has never provisioned, but it is imprecise for
03/04. Not corrected here — outside this item's scope.

### Verification

`make check`, `make test-all` (804 tests, with and without PySide6) and
`make deb` all green after each commit; the rebuilt
`dist/icloud-bridge_0.2.0_all.deb` carries the updated helper. `make lint-ps` was
not re-run: no PowerShell file changed. Nothing Windows-side executed, and the
excerpt's behaviour against a live journal is still operator-verified only.
