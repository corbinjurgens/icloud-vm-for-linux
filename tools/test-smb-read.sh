#!/usr/bin/env bash
# test-smb-read.sh — read a file from the guest SMB share and time it.
#
# HISTORICAL EVIDENCE, kept so the finding can be reproduced. The host-side half
# of the D5 hydration test (see tools/test-smb-hydration.ps1). It asked whether
# an SMB client read of a DATALESS Cloud Files placeholder succeeds, and on
# 2026-07-22/23 the answer was yes: Windows hydrates on demand, so plan D5 is
# disproven and superseded by v2 D14/D25. See docs/plan-gui-selective-sync.md §0.5.
#
# Scope limit that E0 exists to close: this uses userland smbclient with a
# generous timeout, not the kernel cifs client the real /mnt/icloud mount uses.
#
#   ./tools/test-smb-read.sh <SHARE_PASS> [share] [remote-file]
#
# Runs smbclient in a throwaway container on the host network, so it needs no
# root, no cifs mount, and installs nothing on the host. READ-ONLY: it only
# lists and fetches into the container's /tmp, which is discarded.
set -euo pipefail

PASS="${1:?SHARE_PASS required}"
SHARE="${2:-icloudtest}"
REMOTE="${3:-}"
USER_="${USER_:-syncshare}"
HOSTPORT="${HOSTPORT:-10445}"

img=alpine:latest
run() { docker run --rm --network host "$img" sh -c "$1"; }

echo "==> installing smbclient in a throwaway container"
docker run --rm --network host "$img" sh -c 'apk add --no-cache samba-client >/dev/null 2>&1 && smbclient --version'

echo
echo "==> listing //127.0.0.1/$SHARE (metadata only -- should be instant)"
run "apk add --no-cache samba-client >/dev/null 2>&1;
     time smbclient //127.0.0.1/$SHARE -U '$USER_%$PASS' -p $HOSTPORT -c 'recurse ON; ls' 2>&1 | head -40"

if [ -n "$REMOTE" ]; then
  echo
  echo "==> fetching '$REMOTE' -- THIS is the hydration test"
  echo "    (if D5 is right this stalls or errors; if wrong it downloads on demand)"
  run "apk add --no-cache samba-client >/dev/null 2>&1;
       time smbclient //127.0.0.1/$SHARE -U '$USER_%$PASS' -p $HOSTPORT \
            -c 'timeout 120; get \"$REMOTE\" /tmp/out.bin' 2>&1 | tail -20;
       ls -l /tmp/out.bin 2>/dev/null && echo '--- first bytes ---' && head -c 200 /tmp/out.bin | strings | head -5"
fi
