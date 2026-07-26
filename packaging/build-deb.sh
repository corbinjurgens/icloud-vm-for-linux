#!/usr/bin/env bash
# build-deb.sh — stage the repo into a binary package tree and build the .deb.
#
# Runs on the Linux host as an ordinary user (never root). Invoked by `make deb`;
# usable directly for debugging the staged tree:
#
#   ./packaging/build-deb.sh [--keep]     # --keep leaves build/deb/ in place
#
# Uses `dpkg-deb --build --root-owner-group` over a staged directory rather than
# debhelper/dpkg-buildpackage: it needs no debhelper, no fakeroot and no root, so
# a plain `make deb` works on a bare host. Idempotent — the staging tree is
# rebuilt from scratch every run.
#
# The layout deliberately matches host/setup-host.sh exactly (including the
# /usr/local and /etc/systemd/system paths) so the package and the from-source
# install are interchangeable. See packaging/deb/lintian-overrides for why.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
keep=false
[ "${1:-}" = "--keep" ] && keep=true

PKG=icloud-bridge
STAGE="$repo_root/build/deb"
DIST="$repo_root/dist"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' \
  "$repo_root/gui/icloud_bridge_gui/__init__.py")"
if [ -z "$VERSION" ]; then
  echo "Could not read __version__ from gui/icloud_bridge_gui/__init__.py" >&2
  exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
  echo "Do not build packages as root — --root-owner-group already sets ownership." >&2
  exit 1
fi

echo "==> Staging $PKG $VERSION into $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$DIST"

# --- the GUI application ------------------------------------------------------
# Icons resolve relative to __file__ (gui/icloud_bridge_gui/tray.py), so the
# package needs no code change to work from /usr/lib.
install -d "$STAGE/usr/lib/icloud-bridge-gui"
cp -r "$repo_root/gui/icloud_bridge_gui" "$STAGE/usr/lib/icloud-bridge-gui/icloud_bridge_gui"
find "$STAGE/usr/lib/icloud-bridge-gui" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/usr/lib/icloud-bridge-gui" -type f -exec chmod 0644 {} +
find "$STAGE/usr/lib/icloud-bridge-gui" -type d -exec chmod 0755 {} +

install -D -m 0755 "$here/deb/icloud-bridge-gui.launcher" "$STAGE/usr/bin/icloud-bridge-gui"

# Themed icon: an Icon= name (not an absolute path) so the desktop theme can pick
# a better size later without the .desktop files changing.
install -D -m 0644 "$repo_root/gui/icloud_bridge_gui/icons/icloud-green.svg" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps/icloud-bridge.svg"

# --- desktop entries ----------------------------------------------------------
# Same templates the per-user installer uses; only the substitutions differ.
sed -e "s|__LAUNCHER__|/usr/bin/icloud-bridge-gui|" -e "s|__ICON__|icloud-bridge|" \
  "$repo_root/gui/icloud-bridge-gui.desktop" > "$STAGE/tmp-app.desktop"
install -D -m 0644 "$STAGE/tmp-app.desktop" \
  "$STAGE/usr/share/applications/icloud-bridge-gui.desktop"
sed -e "s|__LAUNCHER__|/usr/bin/icloud-bridge-gui|" -e "s|__ICON__|icloud-bridge|" \
  "$repo_root/gui/autostart/icloud-bridge-tray.desktop" > "$STAGE/tmp-autostart.desktop"
# /etc/xdg/autostart is the system-wide counterpart of ~/.config/autostart; a
# per-user file of the same basename overrides this one, so the two installs
# cannot double-launch the tray.
install -D -m 0644 "$STAGE/tmp-autostart.desktop" \
  "$STAGE/etc/xdg/autostart/icloud-bridge-tray.desktop"
rm -f "$STAGE/tmp-app.desktop" "$STAGE/tmp-autostart.desktop"

# --- host helpers and units ---------------------------------------------------
install -D -m 0755 "$repo_root/host/icloud-bridge-power" "$STAGE/usr/local/bin/icloud-bridge-power"
install -D -m 0755 "$repo_root/host/icloud-health.sh"    "$STAGE/usr/local/bin/icloud-health.sh"
install -D -m 0755 "$repo_root/host/icloud-bridge-configure" \
  "$STAGE/usr/local/sbin/icloud-bridge-configure"

# Units ship unpatched (uid=1000,gid=1000); icloud-bridge-configure rewrites the
# ownership, and the postinst replays it after an upgrade.
for unit in mnt-icloud.mount mnt-icloud.automount \
            mnt-icloud_bridge.mount mnt-icloud_bridge.automount \
            icloud-health.service icloud-health.timer; do
  install -D -m 0644 "$repo_root/host/$unit" "$STAGE/etc/systemd/system/$unit"
done

# --- guest-side and operator material ----------------------------------------
# This is also the D31 resource bundle: the first-run assistant resolves
# /usr/share/icloud-bridge for the compose file, the provisioning scripts and
# the env example, so it never has to guess from a working directory. Keep the
# three of them together, and keep the env example named `env.example` — the
# per-user installer copies an identical layout under its app data directory.
#
# Not executable here by design: these run inside the Windows guest.
for f in "$repo_root"/provision/*; do
  install -D -m 0644 "$f" "$STAGE/usr/share/icloud-bridge/provision/$(basename "$f")"
done
install -D -m 0644 "$repo_root/guest-agent/agent.ps1" \
  "$STAGE/usr/share/icloud-bridge/guest-agent/agent.ps1"
install -D -m 0644 "$repo_root/docker-compose.yml" \
  "$STAGE/usr/share/icloud-bridge/docker-compose.yml"
install -D -m 0644 "$repo_root/.env.example" \
  "$STAGE/usr/share/icloud-bridge/env.example"
install -D -m 0755 "$repo_root/host/acceptance-tests.sh" \
  "$STAGE/usr/share/icloud-bridge/acceptance-tests.sh"

install -D -m 0644 "$repo_root/README.md" "$STAGE/usr/share/doc/$PKG/README.md"
install -D -m 0644 "$repo_root/SETUP.md"  "$STAGE/usr/share/doc/$PKG/SETUP.md"
install -D -m 0644 "$repo_root/CHANGELOG.md" \
  "$STAGE/usr/share/doc/$PKG/CHANGELOG.md"
# Under docs/ so the repository-relative `docs/acceptance-results.md` links in
# SETUP.md and CHANGELOG.md still resolve from the installed copies.
install -D -m 0644 "$repo_root/docs/acceptance-results.md" \
  "$STAGE/usr/share/doc/$PKG/docs/acceptance-results.md"
install -D -m 0644 "$here/deb/lintian-overrides" \
  "$STAGE/usr/share/lintian/overrides/$PKG"

# --- normalize permissions ----------------------------------------------------
# `install -d` and `cp -r` inherit the builder's umask, which on Ubuntu is 002 —
# that would ship root-owned group-writable directories. Package directories are
# always 0755; file modes were set explicitly above.
find "$STAGE" -type d -exec chmod 0755 {} +

# --- control metadata ---------------------------------------------------------
size_kb="$(du -ks --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" "$here/deb/control.in" > "$STAGE/DEBIAN/control"
# Installed-Size goes before the multi-line Description, which must stay last.
sed -i "/^Homepage:/a Installed-Size: $size_kb" "$STAGE/DEBIAN/control"

for script in postinst prerm postrm; do
  install -m 0755 "$here/deb/$script" "$STAGE/DEBIAN/$script"
done

# md5sums over the payload only, paths relative to / and without the leading ./
( cd "$STAGE" && find . -type f -not -path './DEBIAN/*' -printf '%P\0' \
  | sort -z | xargs -0 md5sum > DEBIAN/md5sums )

# --- build --------------------------------------------------------------------
DEB="$DIST/${PKG}_${VERSION}_all.deb"
echo "==> Building $DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$DEB" >/dev/null

if command -v lintian >/dev/null 2>&1; then
  echo "==> lintian"
  lintian --no-tag-display-limit "$DEB" || true
else
  echo "==> lintian not installed; skipping package checks"
fi

if [ "$keep" = false ]; then
  rm -rf "$STAGE"
fi

echo
dpkg-deb --info "$DEB" | sed -n '1,3p'
echo "Built: $DEB"
