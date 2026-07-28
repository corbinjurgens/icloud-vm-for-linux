#!/usr/bin/env bash
# keep-iso.sh — stop dockur from making you re-download Windows.
#
# Run on the host WHILE the container is downloading (or any time before the
# ISO is deleted). Idempotent; safe to re-run.
#
#   ./tools/keep-iso.sh
#
# WHY: dockur deletes the downloaded ISO as soon as it has prepared the install
# media -- removeImage() at install.sh:1353 runs BEFORE buildImage and long
# before Windows finishes installing. On the first run the whole
# download-to-deletion window was about 10 minutes. A plain `cp` usually loses
# that race, so we use a HARD LINK: dockur's `rm` then only drops one link and
# the data survives. /srv is one filesystem, so the link costs no extra space.
#
# Once you have a copy, a clean reinstall never re-downloads -- see SETUP.md
# section 7 for the custom.iso recipe (a custom ISO also makes dockur stop
# deleting it, via the `[ -n "$CUSTOM" ] && return 0` guard in removeImage).
set -euo pipefail

CONTAINER="${CONTAINER:-icloud-windows}"
TMP_ISO="${TMP_ISO:-/storage/tmp/win11x64.iso}"
KEEP="${KEEP:-/storage/win11x64-keep.iso}"
STASH_DIR="${STASH_DIR:-/srv/isos}"
STASH="${STASH:-$STASH_DIR/win11-x64.iso}"

# 1) Hard-link inside /storage so dockur's rm cannot destroy the data.
docker exec "$CONTAINER" sh -c "ln -f $TMP_ISO $KEEP" 2>/dev/null \
  || echo "note: $TMP_ISO not present (already consumed?) -- checking keep file"
docker exec "$CONTAINER" stat -c '%n inode=%i links=%h size=%s' "$KEEP"

# 2) Link it out of the storage dir too, so wiping /srv/icloud-vm/storage for a
#    clean reinstall cannot take the ISO with it. Uses a throwaway root
#    container so no host sudo is needed.
docker run --rm -v /srv:/srv alpine sh -c "
  mkdir -p $STASH_DIR
  ln -f /srv/icloud-vm/storage/$(basename "$KEEP") $STASH 2>/dev/null || true
  ls -la $STASH_DIR/
"
echo "ISO preserved. Reinstall recipe: SETUP.md section 8."
