#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="${FMD_UV:-uv}"
VENV="${FMD_VENV:-$ROOT/.venv}"
PYTHON_VERSION="${FMD_PYTHON:-3.13}"
EXTRAS=(dev generation factual)
RECREATE=0
VERIFY_ONLY=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage:
  scripts/bootstrap_env.sh [--venv DIR] [--python VERSION] [--recreate] [--dry-run]
  scripts/bootstrap_env.sh --verify-only [--venv DIR]

Rebuilds <repo>/.venv with `uv venv` and `uv sync --frozen --extra dev
--extra generation --extra factual` from uv.lock. --verify-only runs `uv sync --frozen
--check` against the existing environment and changes nothing. --dry-run
prints the commands without executing them.
EOF
}

fail() {
  echo "bootstrap_env: $*" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --venv) [ $# -ge 2 ] || fail "--venv needs a directory"; VENV="$2"; shift 2 ;;
    --python) [ $# -ge 2 ] || fail "--python needs a version"; PYTHON_VERSION="$2"; shift 2 ;;
    --recreate) RECREATE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown argument: $1" ;;
  esac
done

case "$VENV" in
  /*) ;;
  *) VENV="$PWD/$VENV" ;;
esac

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  fail "uv is required but was not found (looked for '$UV_BIN'); install uv, then rerun"
fi
[ -f "$ROOT/pyproject.toml" ] || fail "missing $ROOT/pyproject.toml"
[ -f "$ROOT/uv.lock" ] || fail "missing $ROOT/uv.lock (uv sync --frozen needs the lock)"

declared_extras="$(awk '
  /^\[/ { in_section = ($0 == "[project.optional-dependencies]"); next }
  in_section && /^[A-Za-z0-9_.-]+[[:space:]]*=/ { sub(/[[:space:]]*=.*/, ""); print }
' "$ROOT/pyproject.toml")"
EXTRA_ARGS=()
for extra in "${EXTRAS[@]}"; do
  if ! printf '%s\n' "$declared_extras" | grep -qx "$extra"; then
    fail "extra '$extra' is not declared under [project.optional-dependencies] in pyproject.toml"
  fi
  EXTRA_ARGS+=(--extra "$extra")
done

run() {
  echo "+ $*"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

print_interpreter() {
  if [ ! -x "$VENV/bin/python" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "python: (dry run; $VENV/bin/python was not created)"
      return 0
    fi
    echo "bootstrap_env: environment is missing: $VENV/bin/python" >&2
    return 1
  fi
  "$VENV/bin/python" -c 'import sys; print("python", sys.version.split()[0], sys.executable)'
}

export UV_PROJECT_ENVIRONMENT="$VENV"
echo "uv: $(command -v "$UV_BIN") ($("$UV_BIN" --version))"
echo "project: $ROOT"
echo "environment: $VENV"
echo "extras: ${EXTRAS[*]}"

if [ "$VERIFY_ONLY" -eq 1 ]; then
  print_interpreter || exit 1
  if run "$UV_BIN" sync --frozen --check --project "$ROOT" "${EXTRA_ARGS[@]}"; then
    echo "bootstrap_env: environment matches uv.lock with extras: ${EXTRAS[*]}"
    exit 0
  fi
  echo "bootstrap_env: environment differs from uv.lock (rerun without --verify-only to sync it)" >&2
  exit 1
fi

if [ -d "$VENV" ] && [ "$RECREATE" -eq 1 ]; then
  run "$UV_BIN" venv --clear --python "$PYTHON_VERSION" "$VENV"
elif [ ! -d "$VENV" ]; then
  run "$UV_BIN" venv --python "$PYTHON_VERSION" "$VENV"
else
  echo "reusing the existing environment at $VENV (pass --recreate to rebuild it)"
fi
run "$UV_BIN" sync --frozen --project "$ROOT" "${EXTRA_ARGS[@]}"
print_interpreter || exit 1
run "$UV_BIN" sync --frozen --check --project "$ROOT" "${EXTRA_ARGS[@]}"
echo "bootstrap_env: environment synced from uv.lock with extras: ${EXTRAS[*]}"
