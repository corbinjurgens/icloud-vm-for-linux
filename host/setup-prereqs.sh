#!/usr/bin/env bash
# setup-prereqs.sh — host prerequisites for the iCloud VM (plan section 1).
#
# Debian/Ubuntu. Installs Docker Engine + cifs-utils, verifies KVM, and creates
# the VM storage dir. Idempotent. Re-login (or newgrp docker) after first run so
# the docker group membership takes effect.
#
#   sudo ./host/setup-prereqs.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

# The unprivileged user to add to the docker group (the human operator).
TARGET_USER="${SUDO_USER:-${TARGET_USER:-}}"

echo "==> Verifying KVM"
apt-get update -qq
apt-get install -y -qq cpu-checker
if ! kvm-ok; then
  echo "KVM is not usable. Enable Intel VT-x / AMD-V in BIOS/UEFI (and nested" >&2
  echo "virtualization if this host is itself a VM), then re-run." >&2
  exit 1
fi

echo "==> Installing native Docker Engine (Docker Desktop cannot pass /dev/kvm)"
# Test for the daemon binary `dockerd`, NOT the `docker` CLI: Docker Desktop ships
# the CLI on the host but runs its daemon inside a LinuxKit VM, so `command -v
# docker` is true even when no host Engine (and no /dev/kvm passthrough) exists.
if ! command -v dockerd >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# If Docker Desktop is also installed, the operator's CLI likely defaults to its
# context. Point it at the native Engine so `docker compose up` reaches the host
# daemon that can pass /dev/kvm. Context selection is per-user, so switch it for
# TARGET_USER (the sudo caller) — the 'desktop-linux' context is theirs, not root's.
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ] \
   && sudo -u "$TARGET_USER" docker context ls 2>/dev/null | grep -q 'desktop-linux'; then
  echo "==> Docker Desktop detected; selecting the native 'default' context for $TARGET_USER"
  sudo -u "$TARGET_USER" docker context use default >/dev/null 2>&1 || true
fi

if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
  echo "==> Adding $TARGET_USER to the docker group (re-login required)"
  usermod -aG docker "$TARGET_USER"
fi

echo "==> Ensuring vhost_net is available (accelerated virtio networking)"
# docker-compose.yml passes /dev/vhost-net so dockur's QEMU can run the guest's
# virtio NIC with vhost=on, moving packet processing for the SMB stream into a
# host kernel thread instead of QEMU's userspace main loop (v2 plan D33). Docker
# needs the device node to exist when the container is created, and nothing else
# on a desktop host loads the module, so load it now and at every boot.
if modprobe vhost_net 2>/dev/null && [ -e /dev/vhost-net ]; then
  echo "vhost_net" > /etc/modules-load.d/icloud-bridge-vhost-net.conf
  echo "    /dev/vhost-net present; module will load at boot"
else
  echo "    WARNING: vhost_net is unavailable on this kernel." >&2
  echo "    Remove the '/dev/vhost-net' line from docker-compose.yml or the" >&2
  echo "    container will fail to start; networking then falls back to" >&2
  echo "    userspace virtio, which works but copies every SMB byte." >&2
fi

echo "==> Installing cifs-utils (host-side SMB mount)"
apt-get install -y -qq cifs-utils

echo "==> Creating VM storage dir on the fastest disk available (NVMe preferred)"
mkdir -p /srv/icloud-vm/storage

cat <<'EOF'

Prerequisites installed. Next:
  1. Log out/in (or run: newgrp docker) so docker works without sudo.
  2. Confirm the CLI targets the native Engine:  docker context ls   (expect 'default *',
     not 'desktop-linux'; switch with:  docker context use default).
  3. cp .env.example .env  and fill in DISK_SIZE / RAM_SIZE / CPU_CORES / SHARE_PASS.
  4. docker compose up -d   then open http://127.0.0.1:8006 and wait for the desktop.
  5. Follow docs/implementation-plan.md sections 5-7 inside the guest.
  6. sudo ./host/setup-host.sh   to mount the share and enable health checks.
EOF
