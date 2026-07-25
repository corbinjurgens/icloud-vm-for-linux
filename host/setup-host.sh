#!/usr/bin/env bash
# setup-host.sh — install the host-side CIFS mount + health monitoring.
#
# Run from the repo root as root (sudo), AFTER the guest is provisioned and the
# SMB share exists (provision/03-create-share.ps1). Idempotent.
#
#   sudo ./host/setup-host.sh
#
# This is the from-source install path; `make deb && make install` places exactly
# the same files. Both then run `icloud-bridge-configure`, which owns everything
# machine-specific (credentials from ./.env, mount uid/gid, the D29 sudoers grant).
#
# The mount UID/GID and the sudoers grant default to the desktop user who ran
# `sudo` (SUDO_USER); override with TARGET_USER=<name> when running as root
# directly, or with MOUNT_UID/MOUNT_GID to decouple mount ownership from it.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

if [ ! -f "$repo_root/.env" ]; then
  echo "Missing $repo_root/.env (copy .env.example and fill SHARE_PASS)." >&2
  exit 1
fi

# --- place the files ----------------------------------------------------------
# Units go in unpatched (uid=1000,gid=1000); icloud-bridge-configure rewrites the
# ownership below, identically to the way the package's postinst replays it.
echo "==> Installing units and helpers"
for unit in mnt-icloud.mount mnt-icloud.automount \
            mnt-icloud_bridge.mount mnt-icloud_bridge.automount \
            icloud-health.service icloud-health.timer; do
  install -m 0644 "$here/$unit" "/etc/systemd/system/$unit"
done
install -m 0755 "$here/icloud-health.sh" /usr/local/bin/icloud-health.sh

# The marker directory only — never the marker itself; a marker here would leave
# the bridge disarmed. The helper owns the marker's lifecycle.
install -d -o root -g root -m 0755 /var/lib/icloud-bridge
install -o root -g root -m 0755 "$here/icloud-bridge-power" /usr/local/bin/icloud-bridge-power
install -o root -g root -m 0755 "$here/icloud-bridge-configure" /usr/local/sbin/icloud-bridge-configure

# --- apply the machine-specific configuration ---------------------------------
echo "==> Configuring for this machine"
exec /usr/local/sbin/icloud-bridge-configure --env-file "$repo_root/.env"
