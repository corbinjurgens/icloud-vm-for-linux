#!/usr/bin/env bash
# tools/install-hooks.sh — point this clone's git hooks at .githooks/.
#
# What:    sets core.hooksPath so the tracked hooks in .githooks/ run, instead
#          of copying them into .git/hooks where they would immediately drift
#          from the versions everyone else has.
# Where:   Linux host, as the developer. Not root — it only touches this
#          clone's git config.
# Invoke:  make hooks  (or ./tools/install-hooks.sh [--uninstall])
# Idempotent: yes — re-running it re-applies the same config value.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "FAIL: run this as your normal user, not root" >&2
  exit 1
fi

repo=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "FAIL: not inside a git repository" >&2
  exit 1
}
cd "$repo"

if [ "${1:-}" = "--uninstall" ]; then
  git config --unset core.hooksPath || true
  echo "==> Removed core.hooksPath; this clone runs no hooks now"
  exit 0
fi

[ -d .githooks ] || { echo "FAIL: .githooks/ is missing" >&2; exit 1; }

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks

echo "==> Installed: core.hooksPath = .githooks"
for hook in .githooks/*; do
  [ -f "$hook" ] || continue
  echo "    $(basename "$hook")"
done

# core.hooksPath wins over .git/hooks entirely, so anything real left in there
# has silently stopped running. Say so rather than letting someone wonder.
shadowed=$(find .git/hooks -maxdepth 1 -type f ! -name '*.sample' 2>/dev/null || true)
if [ -n "$shadowed" ]; then
  echo
  echo "NOTE: these hooks in .git/hooks are now shadowed and will not run:"
  printf '    %s\n' $shadowed
fi

cat <<'EOF'

  pre-commit   hygiene checks + pytest, run against the staged tree
  commit-msg   subject shape, no attribution footer

  Bypass one commit with 'git commit --no-verify'.
  Run the same hygiene checks without committing: make lint
  Undo with ./tools/install-hooks.sh --uninstall
EOF
