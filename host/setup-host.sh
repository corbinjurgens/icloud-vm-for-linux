#!/usr/bin/env bash
# setup-host.sh — install the host-side CIFS mount + health monitoring.
#
# Run from the repo root as root (sudo), AFTER the guest is provisioned and the
# SMB share exists (provision/03-create-share.ps1). Idempotent.
#
#   sudo ./host/setup-host.sh
#
# Reads SHARE_PASS from ./.env to build /etc/credentials-icloud.
# Adjust MOUNT_UID/MOUNT_GID for the user who should own the mounted files.
set -euo pipefail

MOUNT_UID="${MOUNT_UID:-1000}"
MOUNT_GID="${MOUNT_GID:-1000}"

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

# --- credentials from .env ---
if [ ! -f "$repo_root/.env" ]; then
  echo "Missing $repo_root/.env (copy .env.example and fill SHARE_PASS)." >&2
  exit 1
fi
# Take the last assignment; strip CRLF and optional surrounding quotes so the
# credentials file never silently carries a stray \r or quote into the password.
SHARE_PASS="$(grep -E '^SHARE_PASS=' "$repo_root/.env" | tail -n1 | cut -d= -f2- | tr -d '\r')"
SHARE_PASS="${SHARE_PASS#\"}"; SHARE_PASS="${SHARE_PASS%\"}"
if [ -z "${SHARE_PASS:-}" ] || [ "$SHARE_PASS" = "CHANGE_ME_STRONG_PASSWORD" ]; then
  echo "SHARE_PASS is unset or still the placeholder in .env." >&2
  exit 1
fi

umask 077
cat > /etc/credentials-icloud <<EOF
username=syncshare
password=$SHARE_PASS
EOF
chmod 600 /etc/credentials-icloud
mkdir -p /mnt/icloud

# --- install units, patching uid/gid ---
sed -e "s/uid=1000/uid=$MOUNT_UID/" -e "s/gid=1000/gid=$MOUNT_GID/" \
  "$here/mnt-icloud.mount" > /etc/systemd/system/mnt-icloud.mount
install -m 0644 "$here/mnt-icloud.automount" /etc/systemd/system/mnt-icloud.automount
install -m 0644 "$here/icloud-health.service" /etc/systemd/system/icloud-health.service
install -m 0644 "$here/icloud-health.timer"   /etc/systemd/system/icloud-health.timer
install -m 0755 "$here/icloud-health.sh"       /usr/local/bin/icloud-health.sh

systemctl daemon-reload
systemctl enable --now mnt-icloud.automount
systemctl enable --now icloud-health.timer

echo "Done. Verify with:  ls /mnt/icloud"
