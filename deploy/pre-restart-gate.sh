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
SKIP_NODE_SUITES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-self-test) SKIP_SELF_TEST=1; shift ;;
    --skip-node-suites) SKIP_NODE_SUITES=1; shift ;;
    -h|--help)
      cat <<'USAGE'
Usage: deploy/pre-restart-gate.sh [--skip-self-test] [--skip-node-suites]

Steps:
  1) python -m compileall app/mini_hermes
  2) pytest (tests/ excluding smoke_*)
  3) scripts/run_node_suites.sh (pi research relay + installed-pi CLI attachments)
  4) --self-test (skipped with --skip-self-test, or if .env is missing)

Step 3 is the same entry point CI and the host release check use; it fails
closed when node, the installed pi package or network-namespace isolation is
unavailable. --skip-node-suites exists for non-deploy hosts only and must not
be used for a production restart.

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

echo "==> [1/4] compileall"
"$PYTHON" -m compileall -q app/mini_hermes

echo "==> [2/4] pytest"
"$PYTHON" -m pytest tests/ -q --ignore-glob='smoke_*'

if [[ "$SKIP_NODE_SUITES" -eq 1 ]]; then
  echo "==> [3/4] node suites skipped (--skip-node-suites; not valid for production)"
else
  echo "==> [3/4] node suites"
  "$ROOT/scripts/run_node_suites.sh"
fi

SKIPPED_STEPS=()
[[ "$SKIP_NODE_SUITES" -eq 1 ]] && SKIPPED_STEPS+=("node suites")

if [[ "$SKIP_SELF_TEST" -eq 1 ]]; then
  echo "==> [4/4] self-test skipped (--skip-self-test)"
  SKIPPED_STEPS+=("self-test")
elif [[ ! -f "$ROOT/.env" ]]; then
  echo "==> [4/4] self-test skipped (no .env; use on CI or pass --skip-self-test)"
  SKIPPED_STEPS+=("self-test (no .env)")
else
  echo "==> [4/4] self-test"
  "$PYTHON" -m mini_hermes --self-test
fi

if [[ ${#SKIPPED_STEPS[@]} -gt 0 ]]; then
  echo
  echo "pre-restart gate: PARTIAL — 已通过的执行到位，但跳过：${SKIPPED_STEPS[*]}"
  echo "  部分检查通过，不构成生产发布验收；不得据此重启生产服务。"
  exit 0
fi

echo "pre-restart gate OK — safe to restart:"
echo "  systemctl restart mini-hermes-pi mini-hermes"
