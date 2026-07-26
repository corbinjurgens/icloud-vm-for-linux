# Todo: Quit from the GUI powers off the VM and disconnects the drive

> **Status: implemented.** This review became v2 decision D29 and landed in
> commit `15c3079` on 2026-07-25. It is retained for the design rationale;
> ongoing candidates and ruled-out ideas are tracked in
> [`../CHANGELOG.md`](../CHANGELOG.md).

## Goal

Make an intentional **Quit** from the GUI behave like quitting Google Drive:
stop syncing, cleanly disconnect both CIFS mounts, and power off the Windows VM
instead of leaving it consuming RAM and CPU. Starting the GUI again brings the
existing bridge back.

Closing the status window with its X keeps the current behavior when a tray is
available: hide the window and leave the bridge running. Without a usable tray,
closing the only window is the Quit action and uses the same confirmation flow.

"Disconnect" means that `/mnt/icloud` and `/mnt/icloud_bridge` are no longer
mounts or automount traps. Their empty root-owned mount-point directories remain,
so `ls` returns promptly rather than hanging against a powered-off SMB server.

This changes the v1/v2 always-on assumption. Implementation must add and lock
**D29** in `plan-gui-selective-sync.md` before changing behavior.

## Review conclusions (validated 2026-07-24)

- **The lifecycle belongs in one privileged helper.** The GUI can inspect Docker
  as the desktop user, but unmounting and controlling system units requires
  root. Starting/stopping Docker in the same root-owned helper also makes the
  mount/VM ordering and rollback one serialized transaction rather than two
  independently failing operations.
- **`restart: unless-stopped` has the required Docker semantics.** An explicitly
  stopped container remains stopped across Docker daemon and host restarts,
  while an unexpected exit is restarted. No compose change is needed. See
  [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).
- **The current dockur shutdown path is graceful, but the image is not pinned.**
  Current upstream traps `SIGTERM`, sends an ACPI shutdown, waits up to 105
  seconds, and then force-terminates QEMU. The compose file gives the container
  120 seconds; the helper should use `docker stop --timeout 130` so Docker does
  not pre-empt dockur's handler. This is verified against dockur/windows commit
  `99ac4035c7ee48af3538ed79fdf6be1eef722a9e`; live acceptance must still confirm
  it because `image: dockurr/windows` follows upstream. See
  [dockur's power handler](https://github.com/dockur/windows/blob/99ac4035c7ee48af3538ed79fdf6be1eef722a9e/src/power.sh)
  and [Docker stop timeouts](https://docs.docker.com/reference/cli/docker/container/stop/).
- **Unmount before stopping the VM.** First prevent new health/mount activity,
  then stop the automount units, then the active mount units, and only then stop
  the container. This gives CIFS a live SMB peer for clean teardown. A busy
  mount aborts shutdown; never use lazy or forced unmount for this workflow.
- **Stopping enabled units is not persistent.** Merely stopping the automounts
  and health timer is insufficient: they return after a host reboot while
  `unless-stopped` keeps the VM down. A durable desired-off marker plus systemd
  `ConditionPathExists=!…` gates is required. Conditions preserve the units'
  enabled state, so `setup-host.sh` can remain idempotent without undoing an
  intentional off state. See
  [systemd unit conditions](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#ConditionPathExists=).
- **The GUI must quiesce itself before unmounting.** Health gathers, bridge JSON
  reads/writes, and list-response polls all touch CIFS from worker threads. Quit
  must stop scheduling them and let in-flight work drain before invoking the
  helper, or the GUI can make its own unmount fail as "busy".
- **Startup must precede bridge I/O.** Today `MainWindow.reload_selective_sync()`
  and the first health gather run during construction. When the VM is off, the
  controller must first run the power-on transition; it must not touch either
  mount until that transition succeeds.
- **A TCP connect is not an SMB readiness check.** `docs/automation-notes.md`
  records that Docker's published-port plumbing accepts connections before a
  guest service is listening. Power-on must retry a real CIFS mount/probe rather
  than treating a connection to `127.0.0.1:10445` as readiness.

## Locked decision to add as D29

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D29 | GUI-managed bridge lifecycle | An explicit GUI **Quit** confirms and then records a durable desired-off state, quiesces health and CIFS activity, unmounts both shares, and gracefully stops `icloud-windows`; starting a new GUI process automatically restores an existing stopped bridge before doing any CIFS I/O. Window-close only hides when a tray exists. The confirmation retains **Quit GUI only** for maintenance. A checkable tray item **Start when the computer starts** toggles the XDG autostart entry, making start-at-login a user setting rather than an installer constant. | Makes the GUI the normal on/off boundary without confusing window management with shutdown. The marker keeps automounts and health checks off across reboot, while ordered teardown avoids stale CIFS mounts and refuses to interrupt open files. |

## Host design

### Durable desired-off state

`setup-host.sh` creates `/var/lib/icloud-bridge` as `root:root` 0755. The power
helper owns the marker:

```text
/var/lib/icloud-bridge/powered-off
```

The marker is intentionally readable but only root-writable. It means "host
bridge services must remain disarmed"; during an interrupted or failed
transition the VM itself may still be running. Create it as `root:root` 0644.

Add this line to the `[Unit]` section of both `.mount` units, both `.automount`
units, `icloud-health.service`, and `icloud-health.timer`:

```ini
ConditionPathExists=!/var/lib/icloud-bridge/powered-off
```

Gating both the timer and service closes the race where a timer job was already
queued when shutdown began. Gating mount as well as automount units makes the
marker invariant hold even if another unit tries to start a mount directly.
Conditions are checked on start and do not stop already-active units, so the
helper must still stop them explicitly.

### Privileged helper

Add `host/icloud-bridge-power`, installed root-owned and mode 0755 at
`/usr/local/bin/icloud-bridge-power`.

The Bash script has the repository-standard header, `set -euo pipefail`, a
fixed trusted `PATH`, `==> ` progress output, useful errors on stderr, and no
user-controlled command construction. It checks `id -u` up front and exits 1
with a clear message unless run as root. It accepts only `on` and `off`;
anything else prints usage and exits non-zero. Serialize all transitions with
a bounded `flock` under `/run/lock` so concurrent GUI/manual requests cannot
interleave.

Use distinct documented exit statuses at least for usage, busy/unmount failure,
missing container/start failure, SMB-readiness timeout, and host-unit failure.
The GUI should present the helper's human-readable stderr, not parse localized
`systemctl` output.

#### `icloud-bridge-power off`

1. Acquire the transition lock.
2. Record whether the desired-off marker already existed, then create it before
   stopping anything. This prevents a timer or automount restart during the
   transition and makes interruption fail safe.
3. Stop `icloud-health.timer`, then stop any running
   `icloud-health.service`.
4. Stop `mnt-icloud.automount` and `mnt-icloud_bridge.automount`.
5. Stop `mnt-icloud.mount` and `mnt-icloud_bridge.mount` while the guest SMB
   server is still available.
6. If either mount cannot stop (open file, process working directory, or stuck
   GUI/external I/O), abort before touching the container. Roll back to the
   entry desired state: when this call began in the on state, remove the marker
   and restart the automounts and timer; do not remove a marker that pre-dated
   the call. Report that files or filesystem operations are still in use.
7. If the container exists and is running, run
   `docker stop --timeout 130 icloud-windows`. A missing or already-stopped
   container already satisfies the off state and is not an error.
8. Confirm the container is absent or stopped, both mount and automount units
   are inactive, and the health timer is inactive before returning success.

Do not use `umount -l`, `umount -f`, `docker kill`, or `systemctl disable`.
Systemctl/Docker calls and lock acquisition need bounded waits so the GUI cannot
remain in a progress state forever. If container stop returns an error, inspect
the final container state: a confirmed stopped container is success; a
confirmed running container rolls back to on; an unknown state keeps the marker
and mounts disarmed and reports failure.

The helper is idempotent: repeated `off` reconciles toward the same state and
succeeds once it is reached. If the process is killed mid-transition, the
marker deliberately remains; a later `on` or `off` reconciles the partial state.

#### `icloud-bridge-power on`

1. Acquire the same transition lock.
2. Inspect `icloud-windows`. If it does not exist, fail without removing the
   marker: first-time creation remains the explicit `docker compose up -d`
   provisioning step.
3. Create/retain the marker and stop the health timer/service so no host check
   races startup. Start the container if it is not already running.
4. For up to five minutes, ensure the container remains running and retry an
   **actual CIFS activation**: clear failed mount/automount state, temporarily
   remove the marker, start both automount units, trigger each with a bounded
   metadata access, and require `mountpoint` to confirm `/mnt/icloud` and
   `/mnt/icloud_bridge`. A bare TCP connection to the published port is not
   evidence that guest SMB is ready.
5. Between failed attempts, recreate the marker, stop partial mount/automount
   jobs, and wait before retrying. Keep all GUI mount I/O paused throughout.
6. After both mounts work, leave the marker absent and start
   `icloud-health.timer`.
7. Confirm the container, both mounts/automounts, and timer are active before
   returning success.

If readiness or mount activation fails, recreate/retain the marker, stop any
partially activated host units, and leave the VM running so the operator can
inspect its web screen and retry. Report the partial state plainly. Never start
the health timer against an unverified mount.

`on` is also idempotent. It repairs the marker/units if the VM was started
manually while the bridge remained marked off.

### Installer and sudoers

`setup-host.sh` must resolve the desktop operator as
`TARGET_USER="${SUDO_USER:-${TARGET_USER:-}}"`. If it was invoked directly as
root and `TARGET_USER` is absent or resolves to root, fail with an instruction
to provide the real desktop username. Validate the account with `id`; derive
the default mount UID/GID from it while retaining deliberate `MOUNT_UID` and
`MOUNT_GID` overrides.

Install a root-owned 0440 `/etc/sudoers.d/icloud-bridge` that grants only these
two exact command-plus-argument forms:

```sudoers
# Render the validated username; "alice" is an example.
alice ALL=(root) NOPASSWD: /usr/local/bin/icloud-bridge-power on, /usr/local/bin/icloud-bridge-power off
```

A bare command path would permit arbitrary arguments, so the `on` and `off`
arguments must be present in the sudoers command specs. Render to a temporary
file, validate it with `visudo -cf`, install it atomically, and validate the
complete policy with `visudo -c`. Do not replace a valid installed policy with
an invalid render. Sudoers command arguments match exactly; see the
[sudoers manual](https://www.sudo.ws/docs/man/sudoers.man/).

Install the helper, marker directory, unit files, and sudoers policy before
`daemon-reload`/`enable --now`. If the marker exists during an idempotent
`setup-host.sh` rerun, the units remain enabled but their conditions keep them
inactive.

## GUI design

### Qt-free power model

Add `gui/icloud_bridge_gui/power.py`, importing no PySide6 and performing no
mount I/O. It should:

- inspect structured Docker state with a five-second subprocess timeout;
- pin every docker invocation to the native Engine
  (`DOCKER_HOST=unix:///var/run/docker.sock` in the subprocess environment) so
  a Docker Desktop context flip back to `desktop-linux` cannot point lifecycle
  decisions at the wrong daemon — `docs/automation-notes.md` §2.1 records that
  Desktop coexists on the operator host, and against Desktop's daemon the
  container "does not exist", which would misread as the first-provisioning
  state. Apply the same pinning to `health.py`'s existing `docker inspect`;
- distinguish "daemon unreachable/permission denied" from "container absent",
  and word the unreachable error to name the likely fixes (docker not running,
  user not in the docker group);
- distinguish absent, `created`/`exited`, running, and ambiguous/error states;
- read the desired-off marker through an injectable path/existence function;
- decide whether startup should invoke `on`;
- run exact argv lists (never `shell=True`):
  `sudo -n /usr/local/bin/icloud-bridge-power on|off`;
- return structured success/error results suitable for the controller.

Startup invokes `on` when the marker exists or an existing container is cleanly
stopped (`created`/`exited`). A missing container preserves today's red
first-provisioning state. An inspect permission/daemon/ambiguous-state error is
shown but must not trigger a mutation. Do not auto-restart a container that is
manually stopped while this GUI process is already running; automatic power-on
is a process-start action only.

Unit-test this in `gui/tests/test_power.py` with a fake runner and injected
marker state. Cover exact argv, all launch-decision states, timeouts, non-zero
helper results, and the rule that inspect errors do not mutate anything. The
test must not import Qt, Docker, systemd, sudo, or real mount paths.

### Startup flow

Power inspection must occur before the initial health gather,
`reload_selective_sync()`, response polling, or any other CIFS access.

- If already on, continue with the current initialization.
- If power-on is needed, run it asynchronously. Show a dedicated **Starting
  Windows VM…** transitional state: a fourth tray icon distinct from the three
  health colours (new SVG in `icons/`, same plain-disc fallback path as the
  others), because yellow already means "degraded" and a multi-minute Windows
  boot must not look like a fault. Set the tooltip and a window banner to the
  starting message, disable file/selective-sync actions, but keep **Open VM
  screen** available for diagnosis.
- On success, enable bridge I/O, load selective-sync state, start periodic
  refresh, and gather immediately.
- On failure, do not retry every five seconds. Show the helper error, keep the
  VM-screen action and a **Retry start** action available, and leave mount-based
  work paused. A minimized autostart launch uses a tray notification/state
  rather than an invisible modal.

This avoids the current constructor racing the boot by touching both automount
paths before Windows SMB is ready.

### Quit flow

The tray's **Quit** action presents a custom confirmation dialog:

- Message: quitting stops syncing and disconnects `/mnt/icloud`; changes not
  yet uploaded to iCloud will resume the next time the bridge starts. Do not
  claim that the Apple-side upload queue is empty—the canary cannot prove that
  (v1 plan §9).
- **Quit and power off VM** — the default intentional action.
- **Quit GUI only (leave bridge running)** — immediate current behavior, kept
  for GUI upgrades/restarts.
- **Cancel** — Escape/cancel action.

For **Quit and power off VM**:

1. Stop the five-second health timer and the window's request/response poller;
   reject new bridge reads, writes, Apply operations, and list requests.
2. Track `run_async` work and wait asynchronously for existing mount-touching
   tasks to finish. If they do not drain within a bounded interval slightly
   longer than the CIFS 30-second timeout, abort shutdown and explain that a GUI
   filesystem operation is still blocked. Never begin unmount while an Apply
   write is in flight.
3. Run `sudo -n /usr/local/bin/icloud-bridge-power off` in a worker. Keep the
   event loop responsive and show non-cancellable **Shutting down… This can
   take about three minutes** progress. Lock lifecycle actions and file/sync
   controls; keep the status/progress surface reachable.
4. Exit only after helper success. On failure, show its error and keep the app
   running. Resume polling only after the work gate is safe; a busy external
   process is reported as such rather than hidden by lazy unmount.

### Autostart checkbox ("Start when the computer starts")

The tray menu gains a checkable action **Start when the computer starts**,
placed after **Open VM screen** and before the Quit separator.

- Mechanism: the existing XDG autostart entry
  `~/.config/autostart/icloud-bridge-tray.desktop` (installed by
  `install-gui.sh`). Toggling writes `Hidden=true`/`Hidden=false` (and keeps
  `X-GNOME-Autostart-enabled` in step) in that file rather than deleting it,
  so the rewritten absolute `Exec` path survives. If the file is missing,
  recreate it pointing at the installed launcher
  (`~/.local/bin/icloud-bridge-gui --minimized`).
- The checkbox reflects the file on menu construction and after each toggle:
  absent or `Hidden=true` means unchecked.
- Logic lives in a new Qt-free `gui/icloud_bridge_gui/autostart.py` (read,
  toggle, injectable home/paths), unit-tested in `gui/tests/test_autostart.py`
  against a tmpdir with no Qt import.
- `install-gui.sh` re-runs must preserve a disabled choice: carry an existing
  `Hidden=true` over when rewriting the autostart entry, instead of silently
  re-enabling autostart on every upgrade.
- This is the mechanism behind the desired-off reboot scenario's "with GUI
  autostart disabled, it remains off until the GUI is launched".

When a tray exists, `MainWindow.closeEvent` continues to hide with no prompt.
Without a tray, it must emit a quit request and ignore the close until the
controller completes confirmation/transition; `QuitOnLastWindowClosed` must not
bypass the controller. OS session logout, process signals, crashes, and generic
`QApplication.aboutToQuit` do **not** power off the bridge—only the explicit
confirmed action does.

## Files to touch during implementation

- `host/icloud-bridge-power` — new serialized, idempotent helper.
- `host/setup-host.sh` — target-user resolution, marker directory, helper and
  sudoers installation.
- `host/acceptance-tests.sh` — installed ownership/mode, unit conditions,
  marker, and separate non-mutating
  `sudo -n -l /usr/local/bin/icloud-bridge-power on` and `... off` checks.
  `setup-host.sh`, which is already root, owns full `visudo` validation. Normal
  acceptance still requires the bridge to be on.
- `host/mnt-icloud.mount`, `host/mnt-icloud.automount`,
  `host/mnt-icloud_bridge.mount`, `host/mnt-icloud_bridge.automount`,
  `host/icloud-health.service`, and `host/icloud-health.timer` — marker
  conditions.
- `gui/icloud_bridge_gui/power.py` and `gui/tests/test_power.py` — new.
- `gui/icloud_bridge_gui/health.py` — pin its `docker inspect` to the native
  Engine socket (same rationale as `power.py`).
- `gui/icloud_bridge_gui/autostart.py` and `gui/tests/test_autostart.py` — new.
- `gui/icloud_bridge_gui/icons/` — new "starting" tray icon SVG.
- `gui/icloud_bridge_gui/__main__.py`, `tray.py`, and `window.py` — startup,
  I/O quiescing, confirmation, progress/error UI, no-tray close routing,
  starting-state icon, autostart checkbox.
- `gui/install-gui.sh` — preserve an existing `Hidden=true` autostart choice
  across re-runs.
- `plan-gui-selective-sync.md` — D29; §6.1 layout; §6.2 lifecycle behavior;
  implementation checklist and live acceptance.
- `docs/implementation-plan.md` — repository tree, §8/§9 unit copies
  (verbatim), setup description, §10 operations, §11 acceptance, §13 artifact
  summary. Both desired-on and desired-off reboot behavior must be documented.
- `README.md`, `SETUP.md`, and `docs/selective-sync.md` — day-to-day GUI
  lifecycle and updated manual acceptance.
- `AGENTS.md` — add the helper and `power.py` to the repository layout/rules so
  later changes preserve the lifecycle contract.

No change is expected in `docker-compose.yml`, `icloud-health.sh`, or the
Windows guest scripts. If implementation proves otherwise,
apply the repository's embedded-plan sync rule in the same commit.

## Implementation order

1. Add D29 and the durable-state/GUI contract to the authoritative plans.
2. Add marker conditions, helper, installer/sudoers changes, and host checks.
3. Add and test the Qt-free power model.
4. Wire startup before all CIFS I/O, then implement worker quiescing and Quit.
5. Update operator docs and run repository verification.
6. Perform live on/off, busy-mount, failed-start, and reboot acceptance on the
   real KVM host before calling the feature complete.

## Risks and required behavior

- **Pending upload:** ACPI shutdown lets iCloud/Windows close cleanly, but there
  is no Apple upload-queue API. The confirmation promises only resume-on-next-
  start, not complete cloud sync.
- **Hung guest:** current dockur eventually force-terminates QEMU after its
  105-second ACPI wait. This is equivalent to power loss and must be called out
  in the live result; it is a recovery fallback, not a successful graceful test.
- **Busy/stuck CIFS:** fail with the VM still up. No lazy unmount. Test an open
  file, a process whose cwd is inside the mount, and a large active copy.
- **Host reboot while marked off:** the container remains stopped and unit
  conditions suppress automounts and health checks before desktop login. XDG
  autostart powers the bridge on when the operator logs in. With GUI autostart
  disabled, it remains off until the GUI is launched.
- **Host reboot while on:** marker absent, so existing enabled units and
  `unless-stopped` preserve today's automatic recovery.
- **GUI crash/logout without confirmed Quit:** the VM keeps running and syncing.
  This is the safe direction.
- **Manual `docker stop`:** the running GUI reports red but does not fight it.
  A later fresh GUI launch starts the existing stopped container.
- **Missing container:** startup never runs `docker compose up` and never
  creates a VM. It shows the provisioning error; the helper's `on` leaves the
  marker and units disarmed.
- **Partial helper failure:** marker semantics are fail safe and both commands
  reconcile partial state on retry. Errors must identify whether the VM is
  running and whether mounts/timer are armed.
- **Privileges:** the sudoers rule names exact arguments and the installed
  helper remains root-owned. The desktop operator is already in the
  root-equivalent Docker group, but that is not a reason to grant broad
  passwordless systemctl/umount access.

## Verification

### In this workspace

```bash
bash -n host/*.sh host/icloud-bridge-power gui/install-gui.sh
docker compose config
pytest gui/tests
cmp guest-agent/agent.ps1 provision/agent.ps1
git diff --check
```

Also verify:

- all six edited unit files and their verbatim plan copies contain the marker
  condition;
- the helper and sudoers command paths/arguments match byte-for-byte;
- the sudoers render is validated before installation;
- `power.py` and its tests import without PySide6;
- no port binding or secret-bearing file changed.

This workspace has no live systemd/KVM/Windows guest. Shell syntax, Python
tests, compose rendering, and static unit/policy review do not prove runtime
shutdown, CIFS teardown, sudo authorization, Windows boot, or iCloud recovery.

### On the real host

1. **Clean Quit:** with both mounts active, choose power-off. Both mounts and
   automounts become inactive, health service/timer inactive, marker present,
   container stopped, and `ls /mnt/icloud` returns promptly on the empty
   mount-point directory. Journal has no periodic intentional-off failures.
2. **Relaunch:** start the GUI. It shows a transitional state without touching
   CIFS early; the VM boots, both mounts activate, marker disappears, timer
   starts, selective-sync data loads, and health becomes green.
3. **Quit GUI only:** GUI exits immediately while container, mounts, and timer
   remain active.
4. **Busy refusal:** repeat with (a) an open file, (b) a shell cwd under the
   mount, and (c) a large host write. Shutdown aborts, VM remains running, and
   no mount is lazily detached. After releasing the holder, retry succeeds.
5. **No-tray mode:** X presents the same three-way Quit dialog. With a tray, X
   only hides and changes no host state.
6. **Desired-off reboot:** power off through the GUI, reboot without logging in,
   and confirm container, automounts, and health timer remain off with no FAIL
   spam. Log in; GUI autostart restores the bridge.
7. **Desired-on reboot:** leave the bridge on and reboot. Existing automatic
   container/mount/timer recovery still works.
8. **Failure paths:** temporarily deny helper authorization and test a missing
   container and SMB-readiness timeout. The GUI stays responsive, performs no
   repeated automatic retries, shows actionable state, and never arms the timer
   against a dead mount.
9. **Upstream shutdown:** inspect logs/timing to confirm the installed
   dockur/windows image received SIGTERM and completed ACPI shutdown rather than
   reaching its force-kill fallback.
10. **Autostart toggle:** untick **Start when the computer starts**, log out
    and back in — the GUI does not launch and (if powered off beforehand) the
    bridge stays off. Tick it again, log out and in — the GUI starts minimized
    and restores the bridge. Re-run `install-gui.sh` while unticked and confirm
    the choice survives.
