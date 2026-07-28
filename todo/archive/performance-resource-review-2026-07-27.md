# Archive: Performance and resource review, 2026-07-27

Completed items from
[`../performance-resource-review-2026-07-27.md`](../performance-resource-review-2026-07-27.md),
recorded as they finished. The live note keeps only what is left.

## 2026-07-28 — F1: provision the guest before drawing more conclusions (done)

Written as the review's highest-priority item: do not optimize around an
unprovisioned guest. The guest was provisioned through the app-driven D40-D44
path later on 2026-07-27 (agent build 7 after that day's fix chain; first full
pass of the real library: 60,154 entries, `lastError: none`, 29 s). What
remained were the three recorded confirmations, all closed on 2026-07-28
(CHANGELOG entry "F1 closed" carries the evidence):

- **Second (and third) completed scan:** after `make reinstall` and the
  watcher redeploy, the agent (build 9) completed full scans at
  2026-07-28T01:42:31Z (30,162 ms) and 01:59:08Z (52,228 ms), 69,620 entries
  each, `lastError: none`. The library had grown from 60,154 entries since the
  first pass.
- **Share identity:** `/proc/mounts` shows `//127.0.0.1/icloud` mounted at
  `/mnt/icloud` (`vers=3.1.1`, `rasize=16777216`); the historical `icloudtest`
  share is nowhere mounted.
- **GUI check:** the D37 diagnostic export at 2026-07-28T02:14Z reports
  protocol compatibility "current", agent build 9, no update banner, every
  health row green, 11 exclusions at revision 1.

The item's manual first-run sequence (03/04 elevated, D29-D31 host path) was
superseded by the app-driven path and is preserved in the original note text in
git history; the four acceptance conditions above are the record of what F1
required.

## 2026-07-28 — F3: the remaining I-009 guest proof (done)

All four required checks ran on the live guest under Windows PowerShell
5.1.26100.7920, driven over the qemu-monitor keystroke channel with results
written to the Data share (the F3 driver and its `results.txt` lived under
`\\host.lan\Data\f3\`; the full numbers are in the 2026-07-28 "F3 executed on
the guest" CHANGELOG entry):

- `tools/test-agent-walk.ps1` passes in full under 5.1 (exit 0).
- Join-Path vs concatenation over 2,000 real-library entries (55 spaced,
  1,572 non-ASCII names, longest path 175 chars): equal on all 2,000 plain
  paths; divergent on all 2,000 `\\?\`-prefixed forms — proving the long-path
  ACL work must (and does) use concatenation, never `Join-Path`.
- Zero-entry, one-entry and ordinary directories enumerate correctly (0/1/54).
- Formal pass durations: 29 s at 60,154 entries (2026-07-27, build 6);
  30.162 s and 52.228 s at 69,620 entries (2026-07-28, build 9),
  `lastError: none` throughout.
