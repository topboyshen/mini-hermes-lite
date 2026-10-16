#!/bin/bash
# mini-hermes 测试执行体 (v5)
# 由受控启动层调用. 关键变更 (v4 → v5):
#   - RUNNER_HOLDLOCK=1 不再作为保护生效证据, 仅作"父进程意图"提示
#   - 启动时实际验证:
#       A) 本进程或祖先持有全局锁 (/proc/locks 检查)
#       B) 当前 cgroup 有真实 cpu.max / memory.max 硬限额 (读 /sys/fs/cgroup/<cg>/)
#     任一验证失败 → exit 9, 不进入测试执行
#   - 业务用户锁外裸跑拒 (exit 2); root 锁外裸跑拒 (exit 9)

set -euo pipefail
cd "$(dirname "$0")"

GLOBAL_LOCK="/var/lock/vps-tests/global.lock"
mkdir -p .test-runs
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_FILE=".test-runs/run-$TS.log"
touch "$LOG_FILE"; chmod 0660 "$LOG_FILE"

# --- 保护层实证 (v5) ---
PROTECT_LIB="${PROTECT_LIB_PATH:-/usr/local/lib/vpstest/protection-lib.sh}"
[[ -r "$PROTECT_LIB" ]] || { echo "FAIL: $PROTECT_LIB 不可读 (protection 库未部署)" >&2; exit 9; }
# shellcheck disable=SC1090
source "$PROTECT_LIB"

if ! verify_lock_held_by_self_or_ancestor "$GLOBAL_LOCK"; then
  echo "FAIL: 本进程或祖先未持有全局锁 $GLOBAL_LOCK (/proc/locks 验证)" >&2
  echo "      请经 /usr/local/bin/run-tests-launcher 受控启动, 或 vps-preflight-safe 内运行" >&2
  exit 9
fi

# cgroup 期望: CPU ≤ 70%, memory ≤ 800MiB (允许更严, 不允许更松)
if ! verify_cgroup_limits_or_die 70 $((800*1024*1024)); then
  echo "FAIL: 当前 cgroup 缺少硬限额或超过预算" >&2
  echo "      请经 /usr/local/bin/run-tests-launcher 受控启动 (建立 systemd-run scope)" >&2
  exit 9
fi

# 通过实证后:
PREFIX=(nice -n 19)
LOCK_EVIDENCE="flock verified via /proc/locks (holder pid in self/ancestor lineage)"
CGROUP_EVIDENCE="$(cat /sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/cpu.max 2>/dev/null)/$(cat /sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.max 2>/dev/null)"

{
  echo "=== mini-hermes test run $TS ==="
  echo "user: $(id -un)($(id -u)) host: $(hostname)"
  echo "load: $(cat /proc/loadavg)"
  echo "lock-evidence: $LOCK_EVIDENCE"
  echo "cgroup-evidence: $CGROUP_EVIDENCE"
  echo
} >> "$LOG_FILE" 2>&1

rc=0
"${PREFIX[@]}" env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=app \
  .venv/bin/python -m unittest discover -s tests "$@" \
  >> "$LOG_FILE" 2>&1 || rc=$?

ls -1t .test-runs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f -- 2>/dev/null || true
echo "" >> "$LOG_FILE"
echo "=== finished exit=$rc at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"
echo "lock-evidence: $LOCK_EVIDENCE" >> "$LOG_FILE"
echo "cgroup-evidence: $CGROUP_EVIDENCE" >> "$LOG_FILE"
echo "log: $LOG_FILE"
tail -25 "$LOG_FILE"
exit "${rc:-0}"
