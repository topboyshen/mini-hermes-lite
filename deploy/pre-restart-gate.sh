#!/usr/bin/env bash
# Run BEFORE systemctl restart on deploy hosts.
# Fails closed: any step non-zero aborts so a broken tree never restarts into production.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  else
    PYTHON="$(command -v python3)"
  fi
fi

export PYTHONPATH="${PYTHONPATH:-$ROOT/app}"

SKIP_SELF_TEST=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-self-test) SKIP_SELF_TEST=1; shift ;;
    -h|--help)
      cat <<'USAGE'
Usage: deploy/pre-restart-gate.sh [--skip-self-test]

Steps:
  1) python -m compileall app/mini_hermes
  2) pytest (tests/ excluding smoke_*)
  3) --self-test (skipped with --skip-self-test, or if .env is missing)

Exit non-zero on any failure.
USAGE
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

echo "==> [1/3] compileall"
"$PYTHON" -m compileall -q app/mini_hermes

echo "==> [2/3] pytest"
"$PYTHON" -m pytest tests/ -q --ignore-glob='smoke_*'

if [[ "$SKIP_SELF_TEST" -eq 1 ]]; then
  echo "==> [3/3] self-test skipped (--skip-self-test)"
elif [[ ! -f "$ROOT/.env" ]]; then
  echo "==> [3/3] self-test skipped (no .env; use on CI or pass --skip-self-test)"
else
  echo "==> [3/3] self-test"
  "$PYTHON" -m mini_hermes --self-test
fi

echo "pre-restart gate OK — safe to restart:"
echo "  systemctl restart mini-hermes-pi mini-hermes"
