#!/usr/bin/env bash
# guest-ctl.sh — host-side wrapper for driving the Windows guest.
#
# Runs on the Linux host. Copies tools/qemu-monitor.py into the running
# dockur container and invokes it, so the guest can be scripted without any
# copy/paste through the noVNC viewer. See docs/automation-notes.md.
#
#   ./tools/guest-ctl.sh shot                 # capture screen -> /tmp/guest-screen.png
#   ./tools/guest-ctl.sh type "winget list"   # type WITHOUT pressing Enter
#   ./tools/guest-ctl.sh enter                # press Enter
#   ./tools/guest-ctl.sh key ctrl-c           # send a named key/chord
#   ./tools/guest-ctl.sh run "whoami" 8       # type + Enter + wait Ns + screenshot
#
# Requires: docker access to the `icloud-windows` container, python3 with PIL on
# the host (for PPM -> PNG). If your user is not yet in the docker group, prefix
# with `sg docker -c "..."` (see SETUP.md section 4).
#
# ALWAYS verify a typed command with `shot` before `enter` -- keystroke
# injection is blind and a dropped key silently corrupts the command.
set -euo pipefail

CONTAINER="${CONTAINER:-icloud-windows}"
OUT="${OUT:-/tmp/guest-screen.png}"
here="$(cd "$(dirname "$0")" && pwd)"

need_tool() {
  docker cp "$here/qemu-monitor.py" "$CONTAINER:/tmp/qemu-monitor.py" >/dev/null
}

capture() {
  docker exec "$CONTAINER" python3 /tmp/qemu-monitor.py --shot /tmp/screen.ppm >/dev/null
  docker cp "$CONTAINER:/tmp/screen.ppm" /tmp/guest-screen.ppm >/dev/null 2>&1
  python3 - "$OUT" <<'PY'
import sys
from PIL import Image
Image.open("/tmp/guest-screen.ppm").convert("RGB").save(sys.argv[1])
print("wrote", sys.argv[1])
PY
}

type_text() {
  printf '%s' "$1" > /tmp/guest-cmd.txt
  docker cp /tmp/guest-cmd.txt "$CONTAINER:/tmp/guest-cmd.txt" >/dev/null
  docker exec "$CONTAINER" python3 /tmp/qemu-monitor.py --textfile /tmp/guest-cmd.txt >/dev/null
}

case "${1:-}" in
  shot)  need_tool; capture ;;
  type)  need_tool; type_text "${2:?text required}"; echo "typed (not executed)" ;;
  enter) need_tool; docker exec "$CONTAINER" python3 /tmp/qemu-monitor.py --enter >/dev/null; echo "enter sent" ;;
  key)   need_tool; docker exec "$CONTAINER" python3 /tmp/qemu-monitor.py --key "${2:?key required}" >/dev/null; echo "key sent" ;;
  run)
    need_tool
    type_text "${2:?text required}"
    docker exec "$CONTAINER" python3 /tmp/qemu-monitor.py --enter >/dev/null
    sleep "${3:-8}"
    capture
    ;;
  *)
    sed -n '2,20p' "$0"; exit 1 ;;
esac
