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
