#!/bin/bash
# mini-hermes 测试统一入口（与 DEPLOYMENT.md / vps-preflight 一致）
# 防重: flock 非阻塞锁
# 资源限制: nice + ionice + 可选 systemd-run scope (70% CPU / 800M mem)
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p .test-runs
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_FILE=".test-runs/run-$TS.log"
LOCK_FILE=".test-runs/.lock"

# 非阻塞锁 — 并发直接退出
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "SKIP: 另一个测试进程持有锁，防重生效（见 $LOG_FILE）" >&2
  exit 75
fi

# 包装命令记录
{
  echo "=== mini-hermes test run $TS ==="
  echo "load: $(cat /proc/loadavg)"
  echo
} >> "$LOG_FILE" 2>&1

# 资源限制命令前缀
PREFIX=(nice -n 19 ionice -c3 -n7)
if command -v systemd-run >/dev/null 2>&1 && [[ "$(id -u)" == "0" ]]; then
  # root 用 systemd-run scope
  PREFIX=(systemd-run --scope --quiet --collect \
    -p CPUQuota=70% -p MemoryMax=800M -p MemorySwapMax=0 \
    "${PREFIX[@]}")
fi

rc=0
"${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=app \
  .venv/bin/python -m unittest discover -s tests "$@" \
  >> "$LOG_FILE" 2>&1 || rc=$?

# rotate logs: keep last 30
ls -1t .test-runs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f -- 2>/dev/null || true

echo "" >> "$LOG_FILE"
echo "=== finished exit=$rc at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"
echo "log: $LOG_FILE"
tail -20 "$LOG_FILE"
exit $rc
