#!/usr/bin/env bash
# acceptance-tests.sh — host-side subset of the acceptance tests.
#
# Runs the checks that can be verified from the Linux host alone (v1 plan
# section 11 plus the v2 bridge checks of v2 plan B3). Tests that require an
# iPhone/Mac, the guest desktop, or a deliberate large hydration are listed at
# the end as MANUAL and are not automated here.
#
#   ./host/acceptance-tests.sh
#
# Deliberately `set -u` only, not `set -e`: every check must run so the report
# lists all failures, and the script still exits non-zero if any failed.
set -u
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
BRIDGE="${ICLOUD_BRIDGE_DIR:-/mnt/icloud_bridge}"

echo "== 1. KVM acceleration usable =="
if command -v kvm-ok >/dev/null 2>&1; then
  kvm-ok >/dev/null 2>&1 && ok "kvm-ok" || bad "kvm-ok reports KVM unusable"
else
  [ -e /dev/kvm ] && ok "/dev/kvm present (kvm-ok not installed)" || bad "/dev/kvm missing"
fi

echo "== 2. Container running =="
docker inspect -f '{{.State.Running}}' icloud-windows 2>/dev/null | grep -q true \
  && ok "icloud-windows running" || bad "icloud-windows not running"

# The compose file passes /dev/vhost-net so QEMU runs the virtio NIC with
# vhost=on (v2 plan D33). Without the node a container created from the current
# compose file cannot start at all, so this reports the cause rather than leaving
# a bare "not running" above.
if [ -e /dev/vhost-net ]; then
  ok "/dev/vhost-net present on the host (accelerated virtio networking)"
else
  bad "/dev/vhost-net missing — run host/setup-prereqs.sh (modprobe vhost_net)"
fi

# ...but the host node existing proves nothing about the container that is
# actually running. A container created before the compose file gained the device
# keeps running happily without it: dockur silently falls back to userspace
# virtio, and QEMU then copies every SMB byte through its own main loop. That is
# precisely the state the author's own guest was found in on 2026-07-26, with the
# host check above passing green. Ask the container and then ask QEMU.
if docker inspect -f '{{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' \
     icloud-windows 2>/dev/null | grep -q '/dev/vhost-net'; then
  ok "the running container was given /dev/vhost-net"
else
  bad "the running container has NO /dev/vhost-net — it predates the compose file;
        recreate it with 'docker compose up -d' (power the bridge off first)"
fi

# Ground truth: vhost=on only appears if QEMU could actually open the node.
QEMU_ARGS=$(docker exec icloud-windows sh -c \
  'tr "\0" " " < /proc/$(pgrep -f qemu-system-x86_64 | head -1)/cmdline' 2>/dev/null)
if [ -z "$QEMU_ARGS" ]; then
  echo "  INFO: could not read the guest QEMU command line (container not exec-able)"
elif echo "$QEMU_ARGS" | grep -q 'vhost=on'; then
  ok "QEMU is running the virtio NIC with vhost=on"
else
  bad "QEMU has no vhost=on — virtio packet processing is in userspace (v2 plan D33)"
fi

echo "== 3. Published ports answer only on 127.0.0.1 =="
for p in 8006 3389 10445; do
  lines=$(ss -tlnH "sport = :$p" 2>/dev/null)
  if [ -z "$lines" ]; then
    bad "port $p not listening"
  elif echo "$lines" | awk '{print $4}' | grep -qvE '^127\.0\.0\.1:|^\[::1\]:'; then
    bad "port $p bound beyond loopback:"; echo "$lines" | sed 's/^/      /'
  else
    ok "port $p loopback-only"
  fi
done

echo "== 4. Mount lists iCloud contents =="
if mountpoint -q /mnt/icloud; then
  if [ -n "$(ls -A /mnt/icloud 2>/dev/null)" ]; then ok "/mnt/icloud non-empty"; else bad "/mnt/icloud mounted but empty"; fi
else
  bad "/mnt/icloud not mounted"
fi

echo "== 5. Round-trip UP: host write reaches the guest NTFS sync root =="
CANARY="/mnt/icloud/.acceptance-canary"
if date -Is > "$CANARY" 2>/dev/null && [ -f "$CANARY" ]; then
  ok "wrote and read back $CANARY (verify it appears on an Apple device manually)"
  rm -f "$CANARY" 2>/dev/null
else
  bad "cannot write into /mnt/icloud"
fi

echo "== 6. Health timer active and last run succeeded =="
if systemctl is-active --quiet icloud-health.timer; then
  ok "icloud-health.timer active"
  if systemctl show -p ExecMainStatus --value icloud-health.service 2>/dev/null | grep -q '^0$'; then
    ok "last icloud-health.service run exit 0"
  else
    echo "  INFO: health.service has not run cleanly yet (may not have fired)"
  fi
else
  bad "icloud-health.timer not active"
fi

echo "== 7. Bridge control share (v2) =="
if mountpoint -q "$BRIDGE"; then
  ok "$BRIDGE mounted"
else
  bad "$BRIDGE not mounted"
fi

for f in status.json tree.json exclusions.json; do
  if [ -f "$BRIDGE/$f" ]; then
    if python3 -m json.tool "$BRIDGE/$f" >/dev/null 2>&1; then
      ok "$f parses as JSON"
    else
      bad "$f is not valid JSON"
    fi
  else
    bad "$BRIDGE/$f missing"
  fi
done

if [ -f "$BRIDGE/status.json" ]; then
  AGE=$(( $(date +%s) - $(stat -c %Y "$BRIDGE/status.json") ))
  if [ "$AGE" -lt 90 ]; then
    ok "status.json is ${AGE}s old (< 90s)"
  else
    bad "status.json is stale (${AGE}s) — the guest agent task may be stopped"
  fi
fi

echo "== 8. Repository guard: the two agent.ps1 copies are identical =="
# provision/agent.ps1 is the copy that dockur places in C:\OEM; the source of
# truth is guest-agent/agent.ps1 (v2 plan A3).
if [ -f "$repo_root/guest-agent/agent.ps1" ] && [ -f "$repo_root/provision/agent.ps1" ]; then
  if cmp -s "$repo_root/guest-agent/agent.ps1" "$repo_root/provision/agent.ps1"; then
    ok "guest-agent/agent.ps1 == provision/agent.ps1"
  else
    bad "guest-agent/agent.ps1 and provision/agent.ps1 have diverged (copy the source of truth over the OEM copy)"
  fi
else
  bad "one of guest-agent/agent.ps1 / provision/agent.ps1 is missing"
fi

echo "== 9. GUI-managed lifecycle install (v2 plan D29) =="
# The durable desired-off marker directory (never the marker itself here).
if [ -d /var/lib/icloud-bridge ]; then
  ok "/var/lib/icloud-bridge exists"
else
  bad "/var/lib/icloud-bridge is missing (setup-host.sh creates it)"
fi
if [ -e /var/lib/icloud-bridge/powered-off ]; then
  echo "  INFO: the desired-off marker is present — the bridge was intentionally"
  echo "        powered off from the GUI, so the on-state checks above are expected"
  echo "        to fail until it is started again."
fi

# The power helper: installed, root-owned, 0755.
HELPER=/usr/local/bin/icloud-bridge-power
if [ -x "$HELPER" ]; then
  meta=$(stat -c '%U:%G %a' "$HELPER" 2>/dev/null)
  if [ "$meta" = "root:root 755" ]; then
    ok "$HELPER is root:root 0755"
  else
    bad "$HELPER has wrong ownership/mode: $meta (want root:root 755)"
  fi
else
  bad "$HELPER is missing or not executable"
fi

# The sudoers grant: installed, root-owned, 0440.
SUDOERS=/etc/sudoers.d/icloud-bridge
if [ -f "$SUDOERS" ]; then
  meta=$(stat -c '%U:%G %a' "$SUDOERS" 2>/dev/null)
  if [ "$meta" = "root:root 440" ]; then
    ok "$SUDOERS is root:root 0440"
  else
    bad "$SUDOERS has wrong ownership/mode: $meta (want root:root 440)"
  fi
else
  bad "$SUDOERS is missing (setup-host.sh installs it)"
fi

# Every installed unit carries the desired-off condition.
for u in mnt-icloud.mount mnt-icloud.automount mnt-icloud_bridge.mount \
         mnt-icloud_bridge.automount icloud-health.service icloud-health.timer; do
  f="/etc/systemd/system/$u"
  if grep -qF 'ConditionPathExists=!/var/lib/icloud-bridge/powered-off' "$f" 2>/dev/null; then
    ok "$u carries the desired-off condition"
  else
    bad "$u is missing the desired-off condition (see $f)"
  fi
done

# Non-mutating sudo authorization check: the operator may invoke exactly the two
# argument forms without a password, and nothing runs. setup-host.sh (root) owns
# the full `visudo` validation; this only confirms the effective grant.
for arg in on off; do
  if sudo -n -l "$HELPER" "$arg" >/dev/null 2>&1; then
    ok "sudo -n permits 'icloud-bridge-power $arg'"
  else
    bad "sudo -n does not permit 'icloud-bridge-power $arg' for $(id -un) — check $SUDOERS"
  fi
done

echo
echo "== MANUAL tests (cannot be automated from the host) =="
cat <<'EOF'
  - Guest idle < 5% host CPU and RSS ~= RAM_SIZE (docker stats).
  - iCloud tray icon shows signed-in and syncing (open http://127.0.0.1:8006).
  - E0 kernel-CIFS gate (v2 plan section 8, phase 0) — run this BEFORE trusting
    the mount with real work, and record the numbers:
      * pick a file the guest reports as RECALL_ON_DATA_ACCESS (online-only),
        with a hash known from another Apple device;
      * `time timeout 30m sha256sum /mnt/icloud/<file>` for a >=100 MB file:
        must finish without EIO/hang and match;
      * repeat for a multi-GB file with a deliberately generous timeout;
      * write a uniquely named disposable file on the mount, confirm it and its
        hash on another Apple device, edit it, confirm the new hash, then delete
        it and confirm the deletion propagates.
    A cold read blocks for the whole download; there is no host-side progress.
  - Online-only placeholders are the normal state now: Files On-Demand stays ON
    and this project pins nothing (v2 plan D14/D25). Seeing `O`/`o` in the guest
    is expected, not a fault.
  - Round-trip DOWN: create a file on iPhone/Mac -> appears in /mnt/icloud < 2 min.
  - Host reboot: container auto-starts, both automounts work on first ls, health green.
  - Selective sync (v2 plan E1-E7): exclude a disposable folder in the GUI and
    verify it disappears from `ls /mnt/icloud`, that read/write/rm/rename/mkdir
    at that name all fail with permission denied, and that re-including it makes
    it reappear.
EOF

echo
echo "Automated result: $PASS passed, $FAIL failed."
[ "$FAIL" -eq 0 ]
