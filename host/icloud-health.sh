#!/usr/bin/env bash
# Copy to /usr/local/bin/icloud-health.sh and chmod +x.
# Exit non-zero on any failure; systemd records it. Wire alerts later if desired.
#
# Limitation: the canary proves the share and guest are alive; it cannot prove
# Apple-side upload succeeded (the client exposes no API for that). If sync stops
# while this passes, open http://127.0.0.1:8006 and check the iCloud tray icon
# for a re-login prompt.
set -u
FAIL=0

# 1. Container running?
docker inspect -f '{{.State.Running}}' icloud-windows 2>/dev/null | grep -q true \
  || { echo "FAIL: container not running"; FAIL=1; }

# 2. Mount alive?
mountpoint -q /mnt/icloud \
  || { echo "FAIL: /mnt/icloud not mounted"; FAIL=1; }

# 3. Write canary: proves host->guest->NTFS path works end to end.
CANARY=/mnt/icloud/.linux-canary
date -Is > "$CANARY" 2>/dev/null \
  || { echo "FAIL: cannot write canary (share read-only or session dead?)"; FAIL=1; }

# 4. Freshness: canary mtime must be recent (also catches a hung guest).
if [ -f "$CANARY" ]; then
  AGE=$(( $(date +%s) - $(stat -c %Y "$CANARY") ))
  [ "$AGE" -lt 300 ] || { echo "FAIL: canary stale (${AGE}s)"; FAIL=1; }
fi

exit $FAIL
