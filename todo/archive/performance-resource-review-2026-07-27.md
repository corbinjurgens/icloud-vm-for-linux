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
