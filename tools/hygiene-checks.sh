#!/usr/bin/env bash
# tools/hygiene-checks.sh — the mechanical half of the AGENTS.md rules.
#
# What:    every repo rule a machine can decide on its own — no live secret and
#          no intact-placeholder regressions (hard rule 2), loopback-only
#          published ports (hard rule 3), LF endings and no BOM (hard rule 8),
#          no decorative emoji, no leftover conflict markers, the two agent.ps1
#          copies byte-identical, and shell/Python syntax.
# Where:   Linux host. Deliberately takes the tree to check as an argument so
#          the pre-commit hook can point it at a snapshot of the *index* — what
#          is actually being committed — while `make lint` points it at the
#          working tree. Nothing here touches docker, a mount or the network.
# Invoke:  tools/hygiene-checks.sh <tree-root> [relative-path ...]
#          With no paths it checks every tracked file under <tree-root>, or
#          every file it can find there if that is not a git repository.
#          ICLOUD_HYGIENE_ENV=/path/to/.env enables the live-secret scan; the
#          file is named separately because .env is never inside the tree.
# Idempotent: yes — read-only, no side effects.
#
# Reports every failure rather than stopping at the first, then exits 1, the
# same way host/acceptance-tests.sh does. Hence `set -u` without `-e`.
set -uo pipefail

ROOT=${1:-}
if [ -z "$ROOT" ] || [ ! -d "$ROOT" ]; then
  echo "usage: $0 <tree-root> [relative-path ...]" >&2
  exit 2
fi
shift

PYTHON=${PYTHON:-python3}
fail=0
bad() { echo "FAIL: $*"; fail=1; }

# ------------------------------------------------------------- file list ----

FILES=("$@")
if [ ${#FILES[@]} -eq 0 ]; then
  if [ -d "$ROOT/.git" ] && command -v git >/dev/null 2>&1; then
    mapfile -t FILES < <(git -C "$ROOT" ls-files)
  else
    mapfile -t FILES < <(cd "$ROOT" && find . -type f \
      -not -path './.git/*' -not -path './build/*' -not -path './dist/*' \
      -not -path './.venv*/*' -not -path '*/__pycache__/*' \
      -printf '%P\n')
  fi
fi

# Keep only files that exist in this tree and are not binary; every check below
# reads text. `grep -Iq .` is false for binary *and* for empty files, and an
# empty file has nothing any of these checks can fail on.
TEXT=()
for f in "${FILES[@]}"; do
  [ -f "$ROOT/$f" ] || continue
  grep -Iq . "$ROOT/$f" 2>/dev/null || continue
  TEXT+=("$f")
done

has() { # has <relative-path> — is this path part of the set being checked?
  local want=$1 f
  for f in "${TEXT[@]}"; do [ "$f" = "$want" ] && return 0; done
  return 1
}

# ------------------------------------------------- encoding and endings -----

echo "==> line endings and encoding"
endings=0
for f in "${TEXT[@]}"; do
  if grep -qU $'\r' "$ROOT/$f" 2>/dev/null; then
    bad "$f: CRLF line ending — this repo is LF only (hard rule 8)"
    endings=1
  fi
  if [ "$(head -c 3 "$ROOT/$f" | od -An -tx1 | tr -d ' \n')" = "efbbbf" ]; then
    bad "$f: UTF-8 BOM"
    endings=1
  fi
done
[ $endings -eq 0 ] && echo "PASS: ${#TEXT[@]} text files are LF, no BOM"

# ------------------------------------------------------ conflict markers ----

echo "==> merge conflict markers"
markers=0
for f in "${TEXT[@]}"; do
  if grep -nE '^(<<<<<<< |>>>>>>> )' "$ROOT/$f" >/dev/null 2>&1; then
    bad "$f: unresolved merge conflict marker"
    markers=1
  fi
done
[ $markers -eq 0 ] && echo "PASS: no conflict markers"

# ---------------------------------------------------------------- emoji -----

# Pictographic emoji are banned in docs and comments; plain monochrome symbols
# (arrows, check, ballot-x, em-dash) are fine, so the class below is the
# picture-style blocks and the emoji-presentation selector only, not all of
# U+2600-27BF. A genuinely user-facing string that needs one is the documented
# reason to commit with --no-verify.
EMOJI='[\x{1F000}-\x{1FAFF}\x{FE0F}\x{2705}\x{274C}\x{274E}\x{2757}\x{2753}\x{2764}\x{2B50}\x{26A0}\x{2B55}]'
echo "==> decorative emoji"
if echo | grep -qP '' 2>/dev/null; then
  emoji=0
  for f in "${TEXT[@]}"; do
    hits=$(grep -nP "$EMOJI" "$ROOT/$f" 2>/dev/null)
    if [ -n "$hits" ]; then
      bad "$f: pictographic emoji in documentation or code"
      printf '  %s\n' "$hits" | head -5
      emoji=1
    fi
  done
  [ $emoji -eq 0 ] && echo "PASS: no pictographic emoji"
else
  echo "SKIP: grep has no -P support, cannot scan for emoji"
fi

# -------------------------------------------------------------- secrets -----

echo "==> secrets and placeholders"
secrets=0
if has .env; then
  bad ".env is in the tree being committed — it is gitignored for a reason"
  secrets=1
fi
if has provision/03-create-share.ps1 &&
   ! grep -q 'STRONG_PASSWORD_HERE' "$ROOT/provision/03-create-share.ps1"; then
  bad "provision/03-create-share.ps1: the STRONG_PASSWORD_HERE placeholder is gone"
  secrets=1
fi
if has .env.example &&
   ! grep -q 'CHANGE_ME_STRONG_PASSWORD' "$ROOT/.env.example"; then
  bad ".env.example: the CHANGE_ME_STRONG_PASSWORD placeholder is gone"
  secrets=1
fi
# The operator's real values, if we were told where they live. Only the keys
# that name a credential, and only values long enough that a match means
# something — RAM_SIZE=8G would hit half the documentation.
if [ -n "${ICLOUD_HYGIENE_ENV:-}" ] && [ -f "${ICLOUD_HYGIENE_ENV}" ]; then
  while IFS='=' read -r key value; do
    case $key in *PASS*|*PASSWORD*|*SECRET*|*TOKEN*|*KEY*) ;; *) continue ;; esac
    value=${value%\"}; value=${value#\"}
    value=${value%\'}; value=${value#\'}
    [ ${#value} -ge 6 ] || continue
    case $value in STRONG_PASSWORD_HERE|CHANGE_ME_STRONG_PASSWORD) continue ;; esac
    for f in "${TEXT[@]}"; do
      if grep -qF -- "$value" "$ROOT/$f" 2>/dev/null; then
        bad "$f: contains the live value of $key from $ICLOUD_HYGIENE_ENV"
        secrets=1
      fi
    done
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ICLOUD_HYGIENE_ENV")
fi
[ $secrets -eq 0 ] && echo "PASS: no live secrets, placeholders intact"

# ------------------------------------------------------- loopback ports -----

# Hard rule 3: the guest holds an authenticated Apple session, so every
# published port stays on 127.0.0.1. host/acceptance-tests.sh section 3 checks
# the running container; this checks the source before it can ever run.
echo "==> published ports"
if has docker-compose.yml; then
  offenders=$(awk '
    /^[[:space:]]*ports:[[:space:]]*$/ { inports = 1; next }
    inports && /^[[:space:]]*-[[:space:]]/ {
      line = $0
      sub(/^[[:space:]]*-[[:space:]]*/, "", line)
      sub(/[[:space:]]*#.*$/, "", line)
      gsub(/"/, "", line)
      if (line !~ /^127\.0\.0\.1:/) print NR ": " line
      next
    }
    inports && /^[[:space:]]*[A-Za-z_]+:/ { inports = 0 }
  ' "$ROOT/docker-compose.yml")
  if [ -n "$offenders" ]; then
    bad "docker-compose.yml publishes a port off 127.0.0.1 (hard rule 3)"
    printf '  %s\n' "$offenders"
  else
    echo "PASS: every published port is bound to 127.0.0.1"
  fi
else
  echo "SKIP: docker-compose.yml is not in the checked set"
fi

# ------------------------------------------------------- agent.ps1 copies ---

# provision/agent.ps1 exists only so dockur drops the agent into C:\OEM; it must
# stay byte-identical to the source of truth in guest-agent/.
echo "==> agent.ps1 copies"
if [ -f "$ROOT/guest-agent/agent.ps1" ] && [ -f "$ROOT/provision/agent.ps1" ]; then
  if cmp -s "$ROOT/guest-agent/agent.ps1" "$ROOT/provision/agent.ps1"; then
    echo "PASS: guest-agent/agent.ps1 == provision/agent.ps1"
  else
    bad "agent.ps1 copies have diverged — edit guest-agent/ then copy to provision/"
  fi
else
  echo "SKIP: one of the agent.ps1 copies is absent from this tree"
fi

# --------------------------------------------------------------- syntax -----

# A script committed without its executable bit fails at the worst moment: the
# .deb maintainer scripts and the git hooks are simply skipped, silently.
echo "==> executable bits"
execbits=0
for f in "${TEXT[@]}"; do
  read -r shebang < "$ROOT/$f" || continue
  case $shebang in '#!'*) ;; *) continue ;; esac
  if [ ! -x "$ROOT/$f" ]; then
    bad "$f: has a shebang but is not executable (chmod +x)"
    execbits=1
  fi
done
[ $execbits -eq 0 ] && echo "PASS: every script with a shebang is executable"

# Dispatch on the shebang rather than the extension so the extensionless host
# helpers and the .deb maintainer scripts are covered without a hardcoded list.
echo "==> shell syntax"
shells=0
shellfail=0
for f in "${TEXT[@]}"; do
  read -r shebang < "$ROOT/$f" || continue
  case $shebang in
    '#!'*bash*)
      shells=$((shells + 1))
      bash -n "$ROOT/$f" || { bad "$f: bash syntax"; shellfail=1; } ;;
    '#!'*/sh|'#!'*env\ sh)
      shells=$((shells + 1))
      sh -n "$ROOT/$f" || { bad "$f: sh syntax"; shellfail=1; } ;;
  esac
done
[ $shellfail -eq 0 ] && echo "PASS: $shells shell scripts parse"

echo "==> python syntax"
pyfiles=()
for f in "${TEXT[@]}"; do
  case $f in *.py) pyfiles+=("$ROOT/$f") ;; esac
done
if [ ${#pyfiles[@]} -eq 0 ]; then
  echo "SKIP: no Python in the checked set"
elif command -v "$PYTHON" >/dev/null 2>&1; then
  if "$PYTHON" -m compileall -q "${pyfiles[@]}" >/dev/null; then
    echo "PASS: ${#pyfiles[@]} Python files compile"
  else
    bad "python syntax"
  fi
else
  bad "$PYTHON not found, cannot check Python syntax"
fi

exit $fail
