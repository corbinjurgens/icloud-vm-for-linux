#!/usr/bin/env bash
# watch-sync.sh — wait for the iCloud Drive initial sync to settle.
#
# Runs on the Linux host. Uses growth of the guest disk image as a cheap,
# non-invasive proxy for sync progress (no need to drive the guest UI), and
# exits 0 once growth has plateaued -- i.e. the initial metadata population has
# finished or stalled and the library is ready to use.
#
# Under v2 (Files On-Demand ON, nothing pinned -- v2 plan D14/D25) the plateau
# is reached after the placeholders are created, not after a full download, so
# it settles much sooner and at a much smaller image size than v1 expected.
# Nothing needs pinning afterwards; run the E0 gate instead.
#
#   ./tools/watch-sync.sh            # defaults: 120s interval, 4h cap
#   INTERVAL=60 QUIET_CYCLES=5 ./tools/watch-sync.sh
#
# Exits: 0 plateaued, 1 timed out, 3 container gone.
set -u

CONTAINER="${CONTAINER:-icloud-windows}"
IMG="${IMG:-/storage/data.img}"
INTERVAL="${INTERVAL:-120}"
QUIET_CYCLES="${QUIET_CYCLES:-3}"      # consecutive quiet samples => plateaued
QUIET_BYTES="${QUIET_BYTES:-52428800}" # <50 MB of growth counts as quiet
DEADLINE=$((SECONDS + ${MAX_SECONDS:-14400}))
LOG="${LOG:-/tmp/icloud-sync-progress.log}"

: > "$LOG"
prev=0
quiet=0

while [ "$SECONDS" -lt "$DEADLINE" ]; do
  state=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null | tr -d '\r')
  if [ "$state" != "running" ]; then
    echo "$(date +%H:%M:%S) container not running ($state)" >> "$LOG"; exit 3
  fi

  # Actual (sparse-aware) bytes consumed by the guest disk.
  cur=$(docker exec "$CONTAINER" du -sb "$IMG" 2>/dev/null | cut -f1)
  [ -z "$cur" ] && cur=0
  delta=$((cur - prev))
  [ "$prev" -eq 0 ] && delta=0

  human=$(docker exec "$CONTAINER" du -sh "$IMG" 2>/dev/null | cut -f1)
  echo "$(date +%H:%M:%S) size=$human delta=$((delta/1048576))MB quiet=$quiet/$QUIET_CYCLES" >> "$LOG"

  if [ "$prev" -ne 0 ] && [ "$delta" -lt "$QUIET_BYTES" ]; then
    quiet=$((quiet + 1))
    if [ "$quiet" -ge "$QUIET_CYCLES" ]; then
      echo "$(date +%H:%M:%S) ==> PLATEAUED at $human -- initial population settled" >> "$LOG"
      exit 0
    fi
  else
    quiet=0
  fi

  prev=$cur
  sleep "$INTERVAL"
done

echo "$(date +%H:%M:%S) ==> TIMEOUT (still growing)" >> "$LOG"
exit 1
