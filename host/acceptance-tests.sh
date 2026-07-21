#!/usr/bin/env bash
# acceptance-tests.sh — host-side subset of the plan's acceptance tests (section 11).
#
# Runs the checks that can be verified from the Linux host alone. Tests that
# require an iPhone/Mac (round-trip DOWN) or eyeballing the guest tray icon are
# listed at the end as MANUAL and are not automated here.
#
#   ./host/acceptance-tests.sh
set -u
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

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

echo
echo "== MANUAL tests (cannot be automated from the host) =="
cat <<'EOF'
  - Guest idle < 5% host CPU and RSS ~= RAM_SIZE (docker stats).
  - iCloud tray icon shows signed-in, sync complete (open http://127.0.0.1:8006).
  - attrib in guest: files under iCloudDrive show P (pinned), not U/O.
  - Round-trip DOWN: create a file on iPhone/Mac -> appears in /mnt/icloud < 2 min.
  - Host reboot: container auto-starts, automount works on first ls, health green.
EOF

echo
echo "Automated result: $PASS passed, $FAIL failed."
[ "$FAIL" -eq 0 ]
