# Todo: Execute the CHANGELOG "Further improvements" backlog

> **Status: item 7 only, ready and blocked on live acceptance.** Items 1-6
> shipped and were archived on 2026-07-27 into
> [`archive/further_improvements.md`](archive/further_improvements.md), which
> keeps their full text, their status records, the decisions they proposed
> (D35-D39), and the context they were written in. Nothing about them is
> outstanding. The one thing this workspace cannot do is run anything on real
> hardware, so every row in
> [`../docs/acceptance-results.md`](../docs/acceptance-results.md) — the record
> item 1 created, and the evidence item 7 waits for — is still `not yet run`,
> and `gui/icloud_bridge_gui/__init__.py::__version__` still reads `0.2.0`.
> The CHANGELOG stays the ledger: when item 7 ships, move its I-007
> candidate in [`../CHANGELOG.md`](../CHANGELOG.md) to **Shipped improvements**
> in the same commit and delete this note.

## 7. Establish the first release boundary (I-007)

> **Status: prepared, bump withheld; renumbered pre-1.0 on 2026-07-26.** The
> CHANGELOG carries the shipped entries for items 1-6 and an I-007 entry saying
> what remains. The `2.x` numbering this item was written against was retired at
> the operator's instruction: nothing has shipped, so the version now reads
> `0.2.0` and the release that names this backlog is `0.3.0`. Read every `2.x`
> below as the `0.x` with the same minor digit. The bump itself is still
> withheld: this item's own precondition is that every release-applicable Phase E
> row is a `pass` or an approved accepted limitation, and all of them are
> `not yet run`. The derivation chain was verified as it stands — `make version`,
> `icloud-bridge-gui --version`, the built filename and `dpkg-deb -I` all agree
> — so the bump is a one-line change once the live rows exist.

Decisions:

- The release that bundles this backlog is **0.3.0**. When items 2–6 have
  landed, the acceptance record contains E12–E15, and every release-applicable
  Phase E row is `pass` or an explicitly approved `accepted limitation`, bump
  `gui/icloud_bridge_gui/__init__.py::__version__` to `"0.3.0"` — it is the
  single source; `Makefile` and `packaging/build-deb.sh` already derive from
  it, so touch nothing else for the number.
- Same commit: CHANGELOG entry mapping 0.3.0 to the shipped items and to the
  acceptance evidence it depends on (the item-1 results file), and move the
  shipped candidates out of **Further improvements**.
- Verify agreement: `make version`, `icloud-bridge-gui --version`, the built
  package filename and its control metadata (`make deb`, then `dpkg-deb -I`).
- **Tagging is not the agent's call.** Per repo rules, never tag without an
  explicit request; the CHANGELOG's own gate (tag only after the live
  acceptance appropriate to the release) means the operator tags `v0.3.0`
  after the relevant item-1 rows are recorded. The agent's deliverable ends at
  the version bump, changelog entry, and verification above.
- This is the point at which the `CONTRIBUTING.md` pre-release policy stops
  being free. Up to the first tag there is nothing to be compatible with; the
  moment a release exists, breaking it becomes a decision with a cost. Nothing
  before this item needs to think about that, and this item is where it gets
  thought about.

Files: `gui/icloud_bridge_gui/__init__.py`, `CHANGELOG.md`, and whatever
`gui/tests/test_cli.py` asserts about the version string.

## Constraints that still apply

- `docs/plan-gui-selective-sync.md` is authoritative and wins on conflict.
- Every item that adds live behavior adds a distinct Phase E acceptance ID to
  `docs/plan-gui-selective-sync.md` and a matching `not yet run` row to
  `docs/acceptance-results.md` in the same commit. Repository tests never turn
  that row into a pass — which is why this item is blocked rather than late.
- No pictographic emoji in docs or comments; keep LF line endings.

## Verification

```bash
make check
make test-all
git diff --check
```

This item changes metadata shipped in the package, so it also runs `make deb`
and inspects the staged tree and package metadata. The checkout can prove
version agreement and nothing else this item needs; the acceptance rows it gates
on come from the real host, and no commit message may claim them from repository
tests alone.
