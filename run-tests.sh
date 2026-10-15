#!/bin/bash
# mini-hermes 测试统一入口（与 DEPLOYMENT.md / vps-preflight 一致）
# 跨项目互斥: /var/lock/vps-tests/global.lock（flock 非阻塞）
# 单机资源约束: 优先 systemd-run scope（CPUQuota/MemoryMax）；
#   非 root 或无 systemd-run 可用时拒绝执行（exit 9，不静默降级）
set -euo pipefail
cd "$(dirname "$0")"

# --- 单机互斥（跨项目） ---
GLOBAL_LOCK="/var/lock/vps-tests/global.lock"
mkdir -p .test-runs
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_FILE=".test-runs/run-$TS.log"
touch "$LOG_FILE"
chmod 0660 "$LOG_FILE"

exec 200>"$GLOBAL_LOCK"
if ! flock -n 200; then
  echo "SKIP: 另一测试进程持有全局锁（$GLOBAL_LOCK），防重生效" >&2
  exit 75  # EX_TEMPFAIL
fi

# --- 资源控制（严格） ---
if [[ "$(id -u)" == "0" ]]; then
  if command -v systemd-run >/dev/null 2>&1; then
    PREFIX=(systemd-run --scope --quiet --collect
      -p CPUQuota=70% -p MemoryMax=800M -p MemorySwapMax=0
      nice -n 19 ionice -c3)
  else
    echo "FAIL: root 模式下 systemd-run 不可用，拒绝静默降级" >&2
    exit 9
  fi
else
  # 非 root (业务用户): systemd-run --user 需要 user session，绝大多数 VPS 下
  # runuser/sudo -u 不会启动 user manager。此路径下能用 nice/ionice，但无硬限额。
  # 策略: 接受 nice/ionice + 依赖全局互斥保证不并发，作为对生产的影响约束。
  #      但明确告知：硬限额不可用。
  if ! command -v nice >/dev/null 2>&1; then
    echo "FAIL: nice 不可用，拒绝执行" >&2
    exit 9
  fi
  PREFIX=(nice -n 19)
  if command -v ionice >/dev/null 2>&1; then
    PREFIX+=(ionice -c3)
  else
    echo "WARN: ionice 不可用，IO 优先级限制失效" >&2
  fi
  echo "WARN: 非 root 路径只有 nice/ionice 软限额，无 CPUQuota/MemoryMax 硬约束" >&2
fi

{
  echo "=== mini-hermes test run $TS ==="
  echo "user: $(id -un)($(id -u)) host: $(hostname)"
  echo "load: $(cat /proc/loadavg)"
  echo "prefix: ${PREFIX[*]}"
  echo
} >> "$LOG_FILE" 2>&1

rc=0
"${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=app \
  .venv/bin/python -m unittest discover -s tests "$@" \
  >> "$LOG_FILE" 2>&1 || rc=$?

# rotate: keep last 30 logs
ls -1t .test-runs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f -- 2>/dev/null || true

echo "" >> "$LOG_FILE"
echo "=== finished exit=$rc at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"

# scope 资源数据（若 root 且使用了 systemd-run）
if [[ "$(id -u)" == "0" && "$LOG_FILE" =~ ^\.test-runs/ ]]; then
  # run-<TS>.log → run-<TS>.scope 由 systemd-run 自动生成 scope 名，难以回查
  # 简化：只记录我们已沿用的配额，确保运行日志中能审计
  echo "resource-limits: CPUQuota=70% MemoryMax=800M (via systemd-run scope)" >> "$LOG_FILE"
fi

echo "log: $LOG_FILE"
tail -20 "$LOG_FILE"
exit $rc
