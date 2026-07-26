# Todo: GUI ease-of-use improvements

## Goal

Make the GUI feel like a consumer sync client (the Google Drive standard the
D29 work aimed at): after first provisioning, the operator should almost never
need a terminal, failures should announce themselves, and the app should handle
every lifecycle situation it can classify safely.

This note records the 2026-07-25 review of `gui/`, the implemented D29 lifecycle,
and SETUP.md's real-world friction log. Items are ordered broadly by operator
value; dependencies and decision gates are called out explicitly. Numbering is
stable so completed items can be removed without renumbering the rest.

## Review findings and constraints

These apply to every item below:

- `docs/plan-gui-selective-sync.md` is authoritative and wins on conflict.
  Every implemented GUI behavior change must update its exact §6.2 specification
  in the same commit. Items 1 and 2 also reverse or extend locked lifecycle
  behavior, so they need new decisions D30 and D31 before implementation, not
  only a prose edit after the code lands.
- A lifecycle state is not a health colour. In particular, **red** can mean a
  running VM with a stale canary, a missing mount, or bad JSON; it is not evidence
  that the bridge is off and must never by itself enable a Start action.
- Startup must complete its allowed power-on path before any CIFS access.
  `provision_needed`, `inspect_error`, intentionally powered-off, starting, and
  shutting-down states therefore keep bridge I/O paused. They must not call
  `health.gather()`, `bridge.read_*()`, `os.path.ismount()` on the share paths, or
  `xdg-open /mnt/icloud`. Docker-only inspection is safe in those states.
- `health.py`, `bridge.py`, `power.py`, `autostart.py`, and any new
  `firstrun.py` remain importable without PySide6. `health.py` and `bridge.py`
  intentionally perform mount I/O in workers; only `power.py`, `autostart.py`,
  and `firstrun.py` must remain free of mount I/O.
- Every subprocess is asynchronous from the GUI's perspective, bounded by a
  timeout, invoked with an exact argv (never a shell), and returns bounded output
  suitable for display. No worker touches a widget.
- The app must not discover install assets from its current working directory.
  Desktop/autostart launchers and package installs do not have a meaningful
  operator checkout as their cwd.

## 1. Power the bridge on/off without quitting the GUI

Today the only way to power the bridge off is **Quit → Quit and power off VM**,
and the only ways to power it on are launching the app or the post-failure
**Retry start**. That makes a temporary stop require quitting and relaunching,
and a definitely stopped container discovered mid-session has no in-app recovery.

Add a tray action and matching Status-tab button, but drive them from an explicit
lifecycle state machine:

- **running** → **Power off bridge (keep this app running)**;
- **powered_off** (the app's own successful `off`) → **Start bridge**;
- **recoverable_stopped** (Docker inspection definitively says
  `exited`/`created`/`dead`) → **Start bridge**;
- **start_failed** → keep the existing **Retry start** wording and diagnostics;
- **provision_needed** → open the first-run assistant from item 2, not Start;
- **inspect_error** or an unrecognized Docker state → show the error and no
  mutating power action;
- an ordinary red/yellow/green health result does not change the action.

Power-off requires a confirmation with the same upload-queue caveat as Quit,
then reuses the D29 ordering: stop new health/CIFS work, stop the list poller,
drain all in-flight mount work and Apply, and call `power.power_off()`. On helper
failure or a busy drain, restore the exact running state and polling. On success,
do not exit: clear stale health rows, show a distinct **Bridge is powered off**
banner/tooltip, keep all mount-touching controls disabled, and either stop polling
or perform only a reduced Docker inspection.

Start pauses all new I/O before invoking `power.power_on()`. On success it must
restart both the controller health timer and the window's request/response poller,
reload selective sync, and gather a fresh snapshot. On failure it stays paused
and uses the existing Retry/Open VM screen surface. Refactor the current Quit
flow so "power off then exit" and "power off then idle" share the drain/helper
transaction but have different success continuations; do not duplicate the
ordering.

Do not assume controller wiring alone recovers an externally stopped container.
Live-test the helper's `on` reconciliation with the host units/mounts left in the
state produced by a manual `docker stop`; amend `host/icloud-bridge-power` if its
existing readiness loop cannot recover that state without a lazy/forced unmount.

D30 must amend v2 plan D29, §6.2, §8 acceptance, and §9's current
"Pause/resume sync from the GUI" exclusion. The boundary should be explicit:
this is a manual whole-bridge power operation, not pausing only iCloud sync.
Only a user action may power off; logout, signals, crashes, `aboutToQuit`, and
window close with a tray still must not. The in-process idle state lasts until
**Start bridge** or process exit. Plain Quit leaves the durable marker/bridge off,
and a later process start retains D29's automatic power-on behavior. When already
off, Quit never calls the helper again.

Files: `docs/plan-gui-selective-sync.md`, `gui/icloud_bridge_gui/__main__.py`,
`gui/icloud_bridge_gui/power.py`, `gui/icloud_bridge_gui/tray.py`,
`gui/icloud_bridge_gui/window.py`, possibly `host/icloud-bridge-power`, targeted
tests, `README.md`, and `SETUP.md`.

## 2. Safe first-run assistant

A fresh install currently reports a missing container and says to run
`docker compose up -d`. Worse, the current `provision_needed` and
`inspect_error` controller branches call `_enter_monitoring()`, which unpauses
I/O and immediately schedules selective-sync/CIFS reads.

The authoritative v2 text is in tension here: its lifecycle headline says
**Startup precedes all CIFS I/O**, while the `provision_needed` entry says to
"preserve today's red first-run state," which can be read as preserving the
current monitoring behavior. This todo resolves the ambiguity in the safer
direction. The first increment must explicitly replace that phrase in v2 §6.2
with a dedicated **Setup required** state whose red presentation is a banner or
diagnostic, not health rows gathered from the mounts. `inspect_error` must use
the same no-CIFS rule. Do not leave the old phrase alongside the new behavior.

Implement the assistant as two distinct stages rather than one impossible
checklist that requires a container before offering to create it.

### 2.1 Read-only host readiness and provisioning state

Add a Qt-free, mount-I/O-free `firstrun.py` with injected command/filesystem
adapters and unit tests. It reports structured checks; `window.py` only renders
them. Before VM creation, check:

- `/dev/kvm` and `/dev/net/tun` exist and are usable by the native Engine;
- the native socket `unix:///var/run/docker.sock` is reachable by this desktop
  session (a docker-group entry without refreshed session membership is not
  enough);
- the server reached through that socket is native Docker Engine, the Compose
  plugin is available, and the current Desktop context is at most a warning
  because project commands are socket-pinned by item 3;
- one complete resource bundle is readable: `docker-compose.yml`,
  the env example, and the whole `provision/` directory;
- the operator has selected an env file and the known keys are syntactically
  valid, required values are non-empty, and `SHARE_PASS` is not the placeholder;
- whether `icloud-windows` is absent, running, or stopped. Absence is the expected
  pre-create state, not a failing prerequisite.

Resolve resources deterministically from the running installation, with an
explicit test/development override. The package bundle is
`/usr/share/icloud-bridge`; the per-user installer must copy an equivalent
compose/provision/example bundle under its application data directory; a source
run can resolve the repository relative to `__file__`. Never guess from cwd.
Show the chosen paths in the assistant. The resolver must account for the current
source name `.env.example` versus installed name `env.example`, or normalize the
installed bundles to one documented name. The per-user installer must also
record the source checkout used for the later `setup-host.sh` instruction and
report clearly if that checkout has since moved.

Parsing the env file must not source it as shell, print `SHARE_PASS`, place the
password in argv, copy it into an installed resource bundle, or put it on the
clipboard. The handoff to `provision/03-create-share.ps1` remains manual.
Each failed check gives a copyable command from SETUP.md, but the GUI does not
run package installation, group changes, `icloud-bridge-configure`, or arbitrary
sudo commands.

Keep the controller in a dedicated non-I/O **Setup required** state while these
checks fail or the container is absent. It may re-run only the readiness checks
and Docker inspection; health and selective-sync polling remain stopped.

### 2.2 Explicit VM creation and handoff

Only when the container is absent and the pre-VM checks pass, offer **Create
Windows VM**. Confirm that it downloads roughly 8 GB, can take 20–40 minutes,
and starts a long-lived VM. Invoke the resolved compose file with the selected
env file, a stable project name, and the native socket in a worker; capture a
bounded diagnostic. The exact compose argv/project-name convention must also be
used in README/SETUP so later terminal `compose` commands address the same
project. Never offer Create when a container with the fixed name already exists.

Successful `compose up -d` enters a **Provisioning Windows** state; it must not
immediately call `_begin_startup()`. During the initial Windows install SMB can
legitimately be unavailable for much longer than the helper's five-minute
readiness deadline. Keep I/O paused, expose **Open VM screen**, and present the
manual in-guest sequence (02, Apple sign-in, 03, 04) followed by the appropriate
host command:

- package install: `sudo icloud-bridge-configure --user … --env-file …`;
- source install: `sudo ./host/setup-host.sh` from the known checkout.

After the operator clicks **Check setup and connect**, verify the helper, both
argument-exact `sudo -n -l` grants, installed units/config metadata, and Docker
state without touching a mount. If ready, call the existing privileged
`power_on()` transaction; that helper's real CIFS activation is the only
mountability test. On success enter normal monitoring. On failure remain in the
assistant with the helper error and VM link.

The read-only assistant can land without a new decision once §6.2's ambiguous
"preserve today's red first-run state" wording is replaced as specified above.
The Create button cannot: the other half of the same sentence explicitly says
`provision_needed` never runs `docker compose up`. D31 must expressly supersede
that prohibition for this confirmed GUI action before §2.2 is implemented,
while preserving the separate rule that `icloud-bridge-power on` never
manufactures a missing container.

Item 3 is a prerequisite for §2.2. Files:
`docs/plan-gui-selective-sync.md`, new
`gui/icloud_bridge_gui/firstrun.py`, `gui/icloud_bridge_gui/__main__.py`,
`gui/icloud_bridge_gui/window.py`, `gui/install-gui.sh`,
`packaging/build-deb.sh`, tests, `README.md`, and `SETUP.md`.

## 3. Make Docker targeting and absence detection consistent

The observed failure is in the unprivileged GUI, which inherits the desktop
user's Docker context in both `power.py`'s runner and `health.py`'s container
check. Docker Desktop can reset the active context to `desktop-linux`, making
the native `icloud-windows` container appear absent even while it is running.

Pin the unprivileged calls in `power.py`, `health.py`, and item 2's
first-run/Compose flow to `DOCKER_HOST=unix:///var/run/docker.sock`; this is the
fix for the reproducible Desktop-context flip. For Python subprocesses, copy
`os.environ` and override only `DOCKER_HOST`; do not replace the environment or
apply the override indiscriminately to `sudo` and unrelated helpers.

Set the same socket explicitly inside the root power helper as
defense-in-depth and consistency, not as an observed bug fix. With normal sudo
environment reset, the helper uses root's Docker configuration, which ordinarily
has no `desktop-linux` context. The pin protects against a future root context or
environment change, but the implementation/acceptance notes must not claim that
switching the desktop user's context reproduces a helper failure.

Keep no-such-container classification case-insensitive everywhere. The pending
`power.py` fix covers Docker 28's `Error: No such object` and Docker 29's
`error: no such object`, but `host/icloud-bridge-power` still matches the old
capitalization. That breaks `off`'s documented "missing container is already
off" idempotency on Docker 29 and must be fixed too.

Test the actual default subprocess adapters (not only injected fake runners) to
assert that they preserve a sentinel environment value and override
`DOCKER_HOST`. Add shell-level coverage for the helper's capitalized/lowercase
classification at a factored testable seam if practical; otherwise call out the
live absent-container case explicitly.

Files: `docs/plan-gui-selective-sync.md` §5.1/§6.2,
`gui/icloud_bridge_gui/power.py`, `gui/icloud_bridge_gui/health.py`,
`gui/tests/test_power.py`, `gui/tests/test_health.py`,
`host/icloud-bridge-power`, and any first-run files from item 2.

## 4. Notify on health incidents and recovery

The tray silently changes colour. Emit a desktop notification when health first
enters red, including on the first completed snapshot of a minimized launch, and
emit one recovery notification when a red incident eventually reaches green.
Yellow neither starts an incident nor clears a latched red incident:

- `green/yellow/none → red`: notify once and latch the incident;
- `red → red/yellow`: no repeat;
- latched incident → green: notify once and clear the latch.

Use the first red check's name and detail in the failure body. A gather exception
represented by the synthetic red GUI check follows the same path. Reset the
notification state deliberately when entering an intentional powered-off or
first-run state so those expected states do not produce health alerts.

Keep this decision as a pure Qt-free reducer with table-driven tests.
Starting/shutdown/provisioning/powered-off transitions do not feed it snapshots.
After a successful `power_on`, do not announce an expected pre-power-off stale
canary as a new incident while the health service gets its first chance to
refresh it; use a bounded startup grace or refresh the canary before enabling
notifications. This exception must be specific to that transition—an
already-running minimized launch whose first snapshot is red still notifies.
Extend `TrayIcon.notify` to select a Warning icon for failure and Information
for recovery; the existing wrapper always uses Warning and is not sufficient for
both messages. Without a tray, the window remains the notification surface.

Files: `docs/plan-gui-selective-sync.md` §6.2,
`gui/icloud_bridge_gui/__main__.py`, `gui/icloud_bridge_gui/health.py` (or a
small Qt-free model module), `gui/icloud_bridge_gui/tray.py`, and tests.

## 5. Selective Sync filter

Add a filter field above the tree. Match folder names and relative folder paths
case-insensitively using the same normalization as `bridge.is_under`; show each
match and its ancestor chain, and hide unrelated branches. Search only the
folder snapshot plus file rows already loaded in this session—do not imply that
unloaded files were searched.

Filtering must not change `_wanted`, check states, selection semantics, or the
in-memory tree. Programmatic expansion of ancestors must not fire list requests
for every visible folder; suppress `_on_item_expanded` during filter-driven
expansion. Preserve the user's pre-filter expanded/collapsed state and restore it
when the filter is cleared. Missing configured items participate by their full
relative path.

Put the matching/visible-path calculation in a pure helper and test empty,
case-insensitive, path, ancestor, missing-item, and no-match cases without Qt.
Manually smoke-test the expansion-state and no-extra-request behavior with Qt.

Files: `docs/plan-gui-selective-sync.md` §6.2,
`gui/icloud_bridge_gui/window.py`, a Qt-free model helper if needed, and tests.

## 6. Make file listing retryable and make “Load more…” discoverable

Two current bookkeeping choices make folder listings appear permanently empty:
the `files-loaded` marker is set before a request is dispatched, and a failed
pagination request removes its **Load more…** row.

Use explicit per-folder `idle` / `loading` / `loaded` state:

- expansion changes `idle → loading` when the async request is accepted for
  dispatch, preventing another expansion from queuing a duplicate; a dispatch
  failure returns it to `idle`;
- another expansion while `loading` does not create a duplicate request;
- only a successful first-page response changes `loading → loaded`, including a
  valid empty response;
- paused I/O, dispatch failure, guest error, malformed response, cancellation,
  timeout, or stale response after Reload returns the folder to `idle`;
- tag requests with the current tree generation so a completion from a prior
  Reload cannot mutate the rebuilt tree.

For pagination, make **Load more…** visually link-like and activate it by one
mouse click or keyboard activation. Its handler must be idempotent when Qt emits
both clicked/activated signals. While the request is pending, replace or disable
the row with a loading state; on every failure restore the same offset so the
operator can retry, and on success append files and the next continuation row.

Extract request-state transitions where practical so the regression cases run
without Qt; manually verify one-click/keyboard behavior.

Files: `docs/plan-gui-selective-sync.md` §6.2,
`gui/icloud_bridge_gui/window.py`, and tests.

## 7. Show an honest excluded-space summary

Show a summary near the Selective Sync introduction and update it whenever
`_wanted` changes, a tree/list response arrives, status changes, or Reload
completes. Use wording such as:

> Excluded: 3 roots, about 42 GB logical (1 size unknown)

Do not call this disk space saved or promise that it already “stays online-only.”
`logicalBytes` is logical content size, while dehydration is asynchronous and
may remain `pending-dehydrate`.

The size sources are incomplete and must be combined carefully:

- `tree.json` contains recursive sizes for folders only;
- list responses contain sizes for files loaded in this GUI session;
- `status.json.exclusions` can supply the last applied size for an exact,
  still-configured root;
- a missing path or newly staged unloaded file can have unknown size.

Sum each canonical exclusion root once; the D19 antichain prevents legitimate
parent/child double counting, but the aggregator should still fail safely on
bad input. Report the count of unknown roots rather than silently treating them
as zero. Add a short note that exclusions are hidden from Linux and requested
online-only, with reclamation status shown separately in the State/Status UI.

Keep the aggregation pure and test folder roots, loaded files, status fallback,
unknown/missing roots, staged re-includes, case-insensitive lookup, and malformed
sizes.

Files: `docs/plan-gui-selective-sync.md` §6.2,
`gui/icloud_bridge_gui/window.py`, a Qt-free model helper, and tests.

## 8. Surface the app version

`gui/icloud_bridge_gui/__init__.py::__version__` is already the single version
source: `Makefile` and `packaging/build-deb.sh` derive the package version from
it. Do not introduce a second packaging version to “keep in step.”

Show `Version <__version__>` as selectable text at the bottom of the Status tab
and add argparse's `--version` output:

```text
icloud-bridge-gui 2.0.0
```

`--version` must exit before claiming the single-instance socket or constructing
`QApplication`. Test the exact output and confirm `make version` reports the
same value.

Files: `docs/plan-gui-selective-sync.md` §6.2,
`gui/icloud_bridge_gui/__main__.py`, `gui/icloud_bridge_gui/window.py`, and
tests.

## Explicitly not doing

- Automating Apple sign-in/2FA, exposing `SHARE_PASS` in the UI/clipboard, or
  carrying it into the guest automatically.
- Automatically restarting a container merely because health is red. Item 1
  adds explicit user actions based on classified lifecycle state.
- Automatically retrying failed power-on on a timer; Retry remains explicit.
- Pausing only iCloud sync while leaving the VM/mounts up. Item 1 is a
  whole-bridge off/on operation and needs D30.
- A host-side FUSE filter, robocopy mirror, per-item pinning, or Photos support.

## Verification

For every implementation item:

```bash
make check
make test-all
git diff --check
```

Run `make deb` for item 2 or any other change to packaging/install paths, and
inspect the staged resource bundle. The no-Qt test run must import every Qt-free
model; the Qt run must not require a display merely to exercise model tests.

Workspace tests can prove state reducers, command construction/environment,
resource/env validation, size aggregation, and request bookkeeping. They cannot
prove desktop notification delivery, tray/menu interaction, real Docker Desktop
coexistence, CIFS behavior, systemd transitions, or Windows provisioning.

Manual verification therefore remains required:

- items 4–8: a desktop smoke test with a real tray (including filter expansion,
  one-click/keyboard Load more, and notification failure/recovery);
- item 3: a host with both Docker Desktop and native Engine, with the desktop
  user's active context deliberately switched to `desktop-linux`; verify the
  unprivileged GUI remains on the native socket. Check the helper's explicit
  socket separately, without claiming the user-context flip should break its
  unpinned form;
- item 1: the D29 clean/busy/no-forced-unmount matrix plus keep-running idle,
  restart, plain Quit while off, and recovery after a manual container stop;
- item 2: both package and per-user installs on a clean KVM host, including
  absent-container setup, a full initial Windows install longer than five
  minutes, manual guest provisioning, host configure, and final helper-driven
  mount activation.

There is no KVM/Windows guest in this workspace, so none of those live results
may be claimed from repository tests alone.
