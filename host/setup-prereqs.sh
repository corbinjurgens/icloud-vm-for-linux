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

echo "==> Installing Docker Engine (not Docker Desktop — it cannot pass /dev/kvm)"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
  echo "==> Adding $TARGET_USER to the docker group (re-login required)"
  usermod -aG docker "$TARGET_USER"
fi

echo "==> Installing cifs-utils (host-side SMB mount)"
apt-get install -y -qq cifs-utils

echo "==> Creating VM storage dir on the fastest disk available (NVMe preferred)"
mkdir -p /srv/icloud-vm/storage

cat <<'EOF'

Prerequisites installed. Next:
  1. Log out/in (or run: newgrp docker) so docker works without sudo.
  2. cp .env.example .env  and fill in DISK_SIZE / RAM_SIZE / CPU_CORES / SHARE_PASS.
  3. docker compose up -d   then open http://127.0.0.1:8006 and wait for the desktop.
  4. Follow docs/implementation-plan.md sections 5-7 inside the guest.
  5. sudo ./host/setup-host.sh   to mount the share and enable health checks.
EOF
