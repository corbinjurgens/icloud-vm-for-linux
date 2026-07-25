#!/usr/bin/env bash
# setup-host.sh — install the host-side CIFS mount + health monitoring.
#
# Run from the repo root as root (sudo), AFTER the guest is provisioned and the
# SMB share exists (provision/03-create-share.ps1). Idempotent.
#
#   sudo ./host/setup-host.sh
#
# Reads SHARE_PASS from ./.env to build /etc/credentials-icloud.
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

# --- resolve the desktop operator (v2 plan D29) -------------------------------
# The GUI power helper's sudoers grant and the mount ownership both key off this
# account. Running `sudo ./host/setup-host.sh` fills SUDO_USER; a bare root shell
# must pass TARGET_USER so we never grant the rule to root or guess wrong.
TARGET_USER="${SUDO_USER:-${TARGET_USER:-}}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
  echo "Cannot determine the desktop user. Run this with sudo as that user, or set TARGET_USER=<name>." >&2
  exit 1
fi
if ! id "$TARGET_USER" >/dev/null 2>&1; then
  echo "TARGET_USER '$TARGET_USER' is not a valid account." >&2
  exit 1
fi
MOUNT_UID="${MOUNT_UID:-$(id -u "$TARGET_USER")}"
MOUNT_GID="${MOUNT_GID:-$(id -g "$TARGET_USER")}"

# --- credentials from .env ---
if [ ! -f "$repo_root/.env" ]; then
  echo "Missing $repo_root/.env (copy .env.example and fill SHARE_PASS)." >&2
  exit 1
fi
# shellcheck disable=SC1090
SHARE_PASS="$(grep -E '^SHARE_PASS=' "$repo_root/.env" | cut -d= -f2-)"
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
# v2 bridge control share (plan D16); same credentials, syncshare reaches both.
mkdir -p /mnt/icloud_bridge

# --- install units, patching uid/gid ---
sed -e "s/uid=1000/uid=$MOUNT_UID/" -e "s/gid=1000/gid=$MOUNT_GID/" \
  "$here/mnt-icloud.mount" > /etc/systemd/system/mnt-icloud.mount
install -m 0644 "$here/mnt-icloud.automount" /etc/systemd/system/mnt-icloud.automount
sed -e "s/uid=1000/uid=$MOUNT_UID/" -e "s/gid=1000/gid=$MOUNT_GID/" \
  "$here/mnt-icloud_bridge.mount" > /etc/systemd/system/mnt-icloud_bridge.mount
install -m 0644 "$here/mnt-icloud_bridge.automount" /etc/systemd/system/mnt-icloud_bridge.automount
install -m 0644 "$here/icloud-health.service" /etc/systemd/system/icloud-health.service
install -m 0644 "$here/icloud-health.timer"   /etc/systemd/system/icloud-health.timer
install -m 0755 "$here/icloud-health.sh"       /usr/local/bin/icloud-health.sh

# --- GUI-managed lifecycle: power helper, marker dir, sudoers (v2 plan D29) ----
# The marker directory only — never the marker itself; a marker here would leave
# the bridge disarmed. The helper owns the marker's lifecycle.
install -d -o root -g root -m 0755 /var/lib/icloud-bridge
install -o root -g root -m 0755 "$here/icloud-bridge-power" /usr/local/bin/icloud-bridge-power

# Grant the operator only the exact `on`/`off` argument forms. A bare command
# path would permit arbitrary arguments, so the arguments are part of the spec
# (sudoers matches command arguments exactly). Render, validate the render, and
# only then swap it in atomically; never replace a valid policy with a bad one.
SUDOERS_DST=/etc/sudoers.d/icloud-bridge
sudoers_tmp="$(mktemp "${SUDOERS_DST}.tmp.XXXXXX")"
cat > "$sudoers_tmp" <<EOF
# Installed by host/setup-host.sh (v2 plan D29). Passwordless, argument-exact use
# of the bridge power helper by the desktop operator; nothing else.
$TARGET_USER ALL=(root) NOPASSWD: /usr/local/bin/icloud-bridge-power on, /usr/local/bin/icloud-bridge-power off
EOF
chown root:root "$sudoers_tmp"
chmod 0440 "$sudoers_tmp"
if ! visudo -cf "$sudoers_tmp" >/dev/null; then
  rm -f "$sudoers_tmp"
  echo "Generated sudoers policy failed validation; left the existing policy untouched." >&2
  exit 1
fi
# A temp name in /etc/sudoers.d contains dots, so sudo/visudo ignore it during
# the scan; the mv into the final dotless name is the atomic swap.
mv -f "$sudoers_tmp" "$SUDOERS_DST"
if ! visudo -c >/dev/null; then
  rm -f "$SUDOERS_DST"
  echo "Overall sudoers policy became invalid; removed $SUDOERS_DST." >&2
  exit 1
fi

systemctl daemon-reload
# If the desired-off marker is present (an intentional GUI power-off), enabling
# these units keeps them enabled but their ConditionPathExists leaves them
# inactive, so this rerun does not silently re-arm the bridge.
systemctl enable --now mnt-icloud.automount
systemctl enable --now mnt-icloud_bridge.automount
systemctl enable --now icloud-health.timer

echo "Done. Verify with:  ls /mnt/icloud  &&  ls /mnt/icloud_bridge"
