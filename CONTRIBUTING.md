# Contributing

This is the canonical guide for everyone changing this repository, including
coding agents. Read it completely before editing, testing, or committing.

## What this repository is

This repository is infrastructure-as-scripts for bridging **iCloud Drive to a
Linux host**. It runs Apple's official *iCloud for Windows* client inside a
`dockur/windows` Windows 11 guest and re-exports the sync root over SMB to
`/mnt/icloud`.

Almost everything is a shell script, a PowerShell/batch script that runs inside
the Windows guest, a systemd unit, a compose file, or documentation. The one
exception is `gui/`, a small PySide6 application with a `pytest` suite. Changes
are judged by whether an operator following them end-to-end on real KVM
hardware succeeds.

## Project status: pre-release

**There are no releases, no tags, and no installed copies other than the
author's own machine.** Nothing in this repository has ever been shipped to
anyone. That is a licence to change things properly, and contributors are
expected to use it. The `__version__` string in
`gui/icloud_bridge_gui/__init__.py` is a package-build stamp, not a compatibility
promise.

There is no backwards compatibility to preserve. Do not add a compatibility
shim, version negotiation, deprecation window, migration path, legacy branch,
or fallback merely to tolerate a version of this code that no longer exists.
When a format, protocol, layout, or interface is wrong:

1. Change it.
2. Update every reader and writer.
3. Delete the old path entirely.
4. Tell the operator in `SETUP.md` what to re-run, usually
   `04-bridge-agent.ps1`.

The bridge protocol has one supported version. The GUI and agent ship together
and are expected to match; a mismatch is an error that fails closed, not a
compatibility matrix.

The exception is the operator's own data and machine state. Preserve
`/etc/credentials-icloud`, `exclusions.json` and its selections,
`/etc/icloud-bridge/config`, the VM disk, and synced files. Safe Workspaces adds
more of it: the configuration at
`$XDG_CONFIG_HOME/icloud-bridge-gui/workspaces.json`, every local replica
directory a workspace points at, the per-workspace state under
`$XDG_STATE_HOME/icloud-bridge-gui/workspaces/<id>/` (Unison archives, baseline,
snapshot, status, log), and the central backups in that directory's `backups/`.
Neither code nor a package removal script may delete any of it — forgetting a
workspace removes its configuration entry and nothing else. Protecting live data
is a data-safety requirement, not backwards compatibility.

This freedom concerns code shape, not care. Refactors must still leave
`make check` green and must honour every locked decision or amend its decision
register explicitly.

## Before adding or changing code

- Read the relevant plan sections, implementation, tests, and recent history
  before deciding how the change should work. The plans record decisions and
  rejected alternatives that are not obvious from the current code.
- Search the tree first with `rg` and `rg --files`. Check for an existing
  helper, model, parser, subprocess wrapper, fixture, script, or documented
  operator step before creating another one.
- Reuse or extend the existing abstraction when its responsibility already
  covers the need. Do not duplicate logic merely because writing a local copy
  looks quicker.
- Follow established boundaries. In particular, keep I/O out of pure models,
  keep Qt out of the Qt-free modules, and put UI wiring in the existing Qt
  layer.
- Prefer the smallest coherent change that fixes the underlying design. Remove
  superseded code instead of leaving dead branches, speculative hooks, or
  parallel implementations.
- Add a new helper or dependency only when the existing code genuinely has no
  suitable home. Explain the new responsibility in the module or script
  documentation, and test it at the lowest practical layer.
- Do not make drive-by changes outside the task. Report unrelated problems
  instead of folding them into a convenient commit.

## Layout and execution boundaries

| Path | Runs on | Notes |
|---|---|---|
| `Makefile` | Linux host | Every development/operator entry point; `make` alone lists them. Targets needing a real host are labelled `HOST:` |
| `.githooks/` | Linux host | `pre-commit` and `commit-msg`, activated by `make hooks`; not optional |
| `tools/hygiene-checks.sh` | Linux host | Mechanical rules shared by `make lint` and the pre-commit hook |
| `tools/install-hooks.sh` | Linux host, **not** root | What `make hooks` runs; `--uninstall` reverses it |
| `tools/*` (rest) | Linux host/guest | Operator and debugging helpers, not installed |
| `packaging/build-deb.sh` | Linux host, **not** root | Stages the tree and runs `dpkg-deb --root-owner-group --build` |
| `packaging/deb/` | — | Package metadata, maintainer scripts, launcher, and overrides |
| `packaging/lint-ps1.ps1` | Linux host, under PS 7 | PowerShell parser and advisory PSScriptAnalyzer pass |
| `docker-compose.yml` | Linux host | `dockur/windows`; published ports must remain on `127.0.0.1` |
| `.env.example` | — | Template; the real `.env` is ignored and holds `SHARE_PASS` |
| `provision/install.bat` | Windows guest | Dockur OEM bootstrap, run as Administrator |
| `provision/01-debloat.ps1` | Windows guest | Automatic; no network or secrets |
| `provision/02-install-icloud.ps1` | Windows guest | Operator-run; Store/winget may require an interactive session |
| `provision/03-create-share.ps1` | Windows guest | Operator-run; contains an intentional placeholder password |
| `provision/04-bridge-agent.ps1` | Windows guest, elevated | Bridge share, agent task, ABE, and ACL boundaries |
| `provision/agent.ps1` | — | Byte-identical deployment copy of `guest-agent/agent.ps1` |
| `guest-agent/agent.ps1` | Windows guest, as `icloud` | Source of truth for the scheduled, unelevated agent |
| `gui/icloud_bridge_gui/` | Linux desktop user | Tray, status window, lifecycle, selective sync, and Safe Workspaces |
| `gui/icloud_bridge_gui/workspaces.py` | Linux desktop user | Qt-free Safe Workspace configuration, XDG paths, validation, and path rejection; no CIFS, no subprocess |
| `gui/icloud_bridge_gui/workspace_sync.py` | Linux desktop user | Qt-free; the only module that reads the mount for a workspace and the only one that runs Unison |
| `gui/tests/` | Linux host | `pytest` suite; must pass with and without PySide6 |
| `gui/install-gui.sh` | Linux host, **not** root | Per-user install; preserves an existing `Hidden=true` preference |
| `host/setup-prereqs.sh` | Linux host, root | Docker, CIFS utilities, and KVM check |
| `host/setup-host.sh` | Linux host, root | Installs units/helper and delegates to `icloud-bridge-configure` |
| `host/icloud-bridge-configure` | Linux host, root | Credentials, mount ownership, sudoers grant, and persistent host config |
| `host/icloud-bridge-power` | Linux host, root | Serialized power transaction used by the GUI through `sudo -n` |
| `host/acceptance-tests.sh` | Linux host | Host-checkable subset of plan section 11 |
| `host/*.mount\|*.automount\|*.service\|*.timer` | Linux host | Installed into `/etc/systemd/system/` |
| `host/icloud-health.sh` | Linux host | Installed into `/usr/local/bin/` and driven by the timer |
| `docs/implementation-plan.md` | — | Authoritative v1 design and D1-D13 register |
| `docs/plan-gui-selective-sync.md` | — | V2 plan and later decisions; amends v1 and wins on conflict |
| `docs/plan-safe-local-workspaces.md` | — | Authoritative Safe Workspaces design, locked by D52 in the v2 register |
| `docs/selective-sync.md` | — | User-facing exclusions documentation |
| `docs/automation-notes.md` | — | Record of unavoidable manual first-run work |
| `README.md` | — | Overview, usage, and development entry points |
| `SETUP.md` | — | Annotated real-machine runbook |
| `CHANGELOG.md` | — | Improvements, investigations, and next work |
| `todo/` | — | Non-authoritative working notes |
| `CONTRIBUTING.md` | — | This canonical contributor guide |

The Qt boundary is part of the design. `tray.py`, `window.py`, and
`__main__.py` are the PySide6 layer. `health.py`, `bridge.py`, `power.py`,
`lifecycle.py`, `backup.py`, `diagnostics.py`, `firstrun.py`, `guestprov.py`,
`envfile.py`, `autostart.py`, `filtering.py`, `listing.py`, `sizes.py`,
`notify.py`, `workspaces.py`, `workspace_sync.py`, and `cli.py` import no Qt and
own the logic.

`lifecycle.py` is stricter still: it is a pure reducer with no I/O, subprocess,
or clock. `envfile.py` performs no I/O of its own either; it holds the single
`SHARE_PASS` grammar that `firstrun.py`, `guestprov.py`, and
`host/icloud-bridge-configure` must agree on (D41). `power.py`, `backup.py`,
`diagnostics.py`, `firstrun.py`, `guestprov.py`, `workspaces.py`, and
`autostart.py` must remain free of mount I/O. Consult their module docstrings and
the v2 plan before changing those boundaries.

Keep the security and data-safety boundaries of those modules intact:

- `power.py` may inspect Docker and read the powered-off marker, then invoke
  only `sudo -n icloud-bridge-power on|off`.
- `backup.py` writes only to local `$XDG_STATE_HOME`, using an atomic 0600 file
  in a 0700 directory. Keep its revision-monotonicity and restore-preview rules;
  it must never use CIFS.
- `diagnostics.py` renders only its allowlisted `Facts` fields. It may run only
  `systemctl is-active` and `sudo -n -l` for the power helper. Redaction remains
  mandatory, with no secrets opt-in.
- `firstrun.py` owns readiness, resource resolution, compose arguments,
  host-setup verification, and the interrupted-provisioning record. It never
  persists an env-file path or its contents.
- `guestprov.py` is the only GUI module allowed to handle the share password,
  which is the whole of D41's narrow amendment to D31. It may read `SHARE_PASS`
  from the selected env file only once the guest has reported
  `waiting-for-secret`, and may deliver it only over `docker exec` stdin. The
  value never appears in argv, the environment, a host temporary file, a log, a
  status, or the clipboard, and is never persisted.
- `autostart.py` only reads and toggles the XDG entry's `Hidden=` value.
- `workspaces.py` touches only local `$XDG_CONFIG_HOME`/`$XDG_STATE_HOME` and
  `/proc/self/mountinfo`, using the same atomic 0600-in-0700 discipline as
  `backup.py`. It runs no subprocess and never reads the mount, so the GUI can
  load workspace configuration before the bridge is up. Its path rules — what a
  remote path may be, what a local root may be, and which filesystems are
  allowed — are safety checks, not cosmetics: keep validation separable from
  creation and keep the rejections fail-closed (D52, plan §4-§5).
- `workspace_sync.py` owns one finite cycle and nothing persistent. It
  short-circuits on the powered-off marker *before* any CIFS path is touched,
  runs every subprocess through an injected runner with no shell and a bounded
  timeout, and builds the Unison argv exactly as plan §7.7 pins it. Never add an
  option that picks a winner, deletes an archive, runs continuously, or
  overrides a lock; never advance the baseline after a guarded, conflicted,
  failed, or timed-out cycle.

## Hard rules

### Plans own decisions; files own code

No document contains a copy of a file that also exists on disk. Plans hold
reasoning, decisions, and operator sequence, then point to the implementation.
Move specifications together, not duplicate text:

- A behavioural change to `guest-agent/agent.ps1` or
  `provision/04-bridge-agent.ps1` updates v2 plan section 3 or 4 in the same
  commit.
- A change to a locked decision updates that decision's register row before it
  lands.
- A todo proposing a decision must move the decision into a plan register
  before implementation.

Decisions D1-D13 in the v1 plan and D14 onward in the v2 plan are locked. Do
not silently substitute components. SMB rather than a robocopy mirror is D6;
the v2 plan section 0 rejects a host-side FUSE filter.

The GUI-managed lifecycle is a contract:

- Keep the `icloud-bridge-power` shutdown ordering: marker, health, automount,
  mount, then container.
- Never use a lazy or forced unmount.
- Keep all six units gated by
  `ConditionPathExists=!/var/lib/icloud-bridge/powered-off`.
- Keep `power.py` and `autostart.py` Qt-free and free of mount I/O.
- Never let logout, a signal, crash, `aboutToQuit`, or window-close-with-tray
  power off the bridge. Only an explicit action may do so.
- Complete startup power-on before any CIFS access.

### Never commit secrets

`.env` stays ignored. The literal `STRONG_PASSWORD_HERE` in
`provision/03-create-share.ps1` and `CHANGE_ME_STRONG_PASSWORD` in
`.env.example` are intentional placeholders. Never replace them with live
values or bake `SHARE_PASS` into `install.bat` or the compose file.

### Published ports stay on loopback

The guest holds an authenticated Apple session. Every published port must bind
to `127.0.0.1`. Do not weaken the hygiene or acceptance checks enforcing this.

### Scripts are idempotent

Re-running provisioning after a Windows feature update is a documented recovery
step. Guard creates with the relevant existence checks, use `-Force` and
`-ErrorAction SilentlyContinue` where appropriate, and use `mkdir -p` or
`install -m` instead of bare copies on the host.

Script 04 must not manufacture an empty `exclusions.json` when other install
markers exist. Doing so would silently re-include the operator's data.

### Preserve the Windows servicing stack

Do not touch the Store, AppX, WebView2, or servicing stack in the debloat
script. iCloud installation and updates depend on it (plan D3). Additions to
the `$bloat` list must be inbox applications only.

### Files On-Demand stays on and nothing is pinned

V2 decisions D14 and D25 supersede v1 D5. Dataless placeholders hydrate over
SMB. Never restore a global `attrib +P -U`.

The only `+U -P` calls belong to exclusion enforcement and disk reclamation;
the only standalone `-P` is the agent's one-time clearing of old pin intent.
Excluded items remain hidden and protected by NTFS deny ACEs plus Access-Based
Enumeration. Never weaken either protection or grant `syncshare` a permission
that can outrank an exclusion deny.

### Host shell conventions

Host shell scripts use `#!/usr/bin/env bash`. Setup scripts use
`set -euo pipefail`; `acceptance-tests.sh` and `icloud-health.sh` deliberately
use `set -u` so they report all failures. Root-requiring scripts check `id -u`
up front and exit 1 with a message.

### Keep LF line endings, and keep guest scripts pure ASCII

The batch and PowerShell files run correctly from LF under dockur. Do not
introduce CRLF or a whole-file line-ending diff.

Everything the guest executes — `provision/*.ps1`, `provision/*.bat`, and
`guest-agent/*.ps1` — must contain only ASCII. Windows PowerShell 5.1 and
cmd.exe read BOM-less files as the ANSI codepage, not UTF-8, so a multi-byte
character parses as CP1252 garbage; an em dash inside a double-quoted string
ends the string early and breaks the whole parse. `make lint-ps` cannot catch
this (PowerShell 7 reads the same bytes as UTF-8), so the hygiene checker
enforces it mechanically.

## Working alongside other sessions

Assume another session is working in this same folder, on this same branch,
right now. Sessions share every file, `.git`, the staging area, and build
output.

### Commit by path, and treat paths as the isolation boundary

A bare `git commit -m "..."` commits the whole shared index. Always name the
paths owned by the current change:

```bash
git diff HEAD -- gui/icloud_bridge_gui/thing.py gui/tests/test_thing.py
git commit -m "Add the thing" -- \
    gui/icloud_bridge_gui/thing.py gui/tests/test_thing.py
```

For an untracked file, stage that file explicitly first:

```bash
git add -- gui/icloud_bridge_gui/new_thing.py
git commit -m "Add the thing" -- gui/icloud_bridge_gui/new_thing.py
```

Do not run `git add` for an already-tracked file merely to prepare a pathspec
commit; the commit reads that named path from the working tree. Unnecessary
staging mutates the shared index.

Pathspec commits isolate files, not hunks. Immediately before committing,
review `git diff HEAD -- <paths>` for every named path. If a file contains
another session's edits, do not commit it. Let that session land first, then
re-read and commit the remaining diff. Serialize work on shared contention
files rather than trying to divide their hunks through the shared index.

The pre-commit hook follows Git's temporary index for a pathspec commit, so it
checks HEAD plus exactly the named paths. Unrelated work staged by another
session remains staged and uncommitted.

Never use `git add -A`, `git add .`, or `git commit -a`. Check
`git status --short` and the exact diff before every commit.

### Never destroy shared state

- Do not run `git checkout <branch>` or `git switch`.
- Do not run `git stash`, `git reset`, `git restore`, `git clean`, or
  `git checkout -- <path>` for a path you did not create.
- Do not amend, rebase, or rewrite history.
- Do not revert or fix up another session's commit or edit. Report it.

If one of these operations is genuinely necessary, stop and let the operator
decide. They are the only person who can see all sessions.

### Edit defensively

- Stay within the files required for the task.
- Re-read a file immediately before editing it.
- If a tool reports that a file changed underneath you, assume another session
  changed it intentionally. Re-read and re-decide.
- Never revert an edit you did not make.

Known contention points include `CHANGELOG.md`, plan decision registers,
`CONTRIBUTING.md`, `README.md`, `Makefile`,
`gui/icloud_bridge_gui/__init__.py`, and
`docs/acceptance-results.md`.

Append to `CHANGELOG.md`; do not reflow surrounding text. Claim the next free
decision number at the moment of editing and check it again before commit.

`guest-agent/agent.ps1` is the source of truth and
`provision/agent.ps1` is its byte-identical copy. Edit the first, copy it to the
second, and commit both together. The hook rejects differing copies.

### Shared build state

`make deb`, `make venv`, `make venv-qt`, and `make lint-ps` write shared build
state. Do not run them concurrently with another session. `make check`,
`make lint`, and `make test` are safe because they only read the tree.

Do not start a long-lived foreground process another session may use.
`make run` holds a Qt event loop and requires a display; it is not verification.

## Verification

Run everything this checkout can prove before claiming a change works:

```bash
make hooks
make check
make deb    # additionally, when packaging or install paths changed
```

`make hooks` is a once-per-clone setup. It configures `core.hooksPath`.

`make check` runs the shared hygiene checks, Docker Compose validation, optional
linters, and `pytest gui/tests`. The hygiene checker covers secrets and
placeholders, loopback ports, LF/no-BOM, guest-script ASCII, conflict markers,
agent-copy equality, executable bits, and shell/Python syntax. Missing optional
tools print an explicit `SKIP:`.

The pre-commit hook lists the paths being committed and checks a snapshot of
the exact tree being committed. The commit-message hook enforces the subject
shape and rejects attribution footers. Bypassing either hook is for a deliberate
exception, not a red gate.

`make test` creates `.venv` when necessary. Do not use
`pip install --user` or `--break-system-packages`. `make test-all` runs the
suite both without Qt and with PySide6, proving the with-and-without invariant.

The ordinary test suite needs no Qt, Docker, mounts, or display. The deliberate
exception is `gui/tests/test_qt_wiring.py`, which skips without PySide6 and uses
the offscreen platform with faked power, bridge, health, and dialogs. It must
never reach Docker, sudo, or a mount.

`make lint-ps` downloads PowerShell 7 into shared `build/pwsh`, parses the
PowerShell files, and runs advisory PSScriptAnalyzer checks. PowerShell 7 on
Linux catches syntax errors but does not prove Windows PowerShell 5.1
compatibility and cannot execute Windows-specific cmdlets or interop.

Do not claim shellcheck or PowerShell validation unless the corresponding tool
actually ran.

There is no KVM/Windows guest in the development workspace. The acceptance
script, `/mnt/icloud`, `/mnt/icloud_bridge`, `docker compose up`, systemd units,
and the guest agent as a whole can only be validated by the operator on the real
host. State this limitation plainly.

## Committing

Commit each completed phase without waiting to be asked. A phase is a coherent
slice that leaves `make check` green, not a half-finished edit.

- Use an imperative, capitalized subject of at most 72 characters with no
  trailing period.
- Add a short body when a multi-area change or its reason is not obvious.
- Do not add `Co-Authored-By`, tool attribution, generated-by text, or emoji.
- Always commit with explicit pathspecs.
- Never push, open a pull request, or tag without an explicit request.
- Never rewrite an existing commit.

Done means `make check` is green; plans and `CHANGELOG.md` are updated when the
change affects a decision or users; the work is committed; and the final report
states what could not be verified locally.

## Style

- Scripts have a header describing what they are, where they run, how to invoke
  them, and whether they are idempotent.
- Cross-reference plan decisions instead of duplicating their rationale.
- User-facing script output uses `==> ` for progress and `PASS:`/`FAIL:` for
  checks.
- Python modules explain their purpose and architectural boundary in the module
  docstring. Comments explain constraints and non-obvious choices rather than
  narrating the code.
- Match the surrounding naming, error handling, and testing style. Reuse the
  repository's existing helpers and fixtures before introducing another
  pattern.

## Scope

In scope: bidirectional iCloud Drive files.

Out of scope by design: Photos, Passwords, Mail/Contacts/Calendar,
Apple-session automation, and custom Windows ISOs. Two-factor authentication
cannot be automated safely. Do not add features in these areas without an
explicit request and a corresponding design decision.
