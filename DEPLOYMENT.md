# DediRock 部署记录

- 主机：`dedirock` (`23.95.122.93`)
- 系统：Debian 12，Python 3.11，systemd 252
- 安装目录：`/opt/mini-hermes`
- 服务账户：`mini-hermes`（无登录 shell）
- systemd：`/etc/systemd/system/mini-hermes.service`
- 日志：`journalctl -u mini-hermes` 与 `/var/log/mini-hermes/mini-hermes.log`
- 数据库：`/opt/mini-hermes/data/memory.db`
- MCP：`quanters-akshare-mcp@0.2.0`，自检发现 21 个工具

## 首次启用

用 SSH 登录并编辑 `/opt/mini-hermes/.env`，至少填写：

1. `TELEGRAM_TOKEN`
2. `MODEL=pi:grok-4.6`，并在隔离的 pi-channel 中完成 xAI 账号授权；此模式不需要主 Bot 的模型 API Key。

若选择独立 API 提供商，才使用其对应的 API Key。普通聊天与 A 股研究是不同
模型入口：只改变 pi 的默认模型不会自动改变 `.env` 中的 `MODEL`。

首次可让 `TELEGRAM_ALLOWED_USER_IDS` 留空，然后执行：

```bash
systemctl enable --now mini-hermes
```

## pi Telegram channel

pi channel 使用第二个 systemd 服务和 Unix Socket：

```bash
systemctl enable --now mini-hermes-pi
systemctl restart mini-hermes
```

运行身份和路径：

- Broker 用户：`pi-channel`
- Socket：`/run/mini-hermes-pi/pi.sock`
- 隔离工作区：`/opt/mini-hermes/workspace`
- 会话：`/var/lib/mini-hermes-pi/sessions`
- pi 配置：`/var/lib/mini-hermes-pi/agent`

不要把 `mini-hermes-pi.service` 改成 root；Telegram channel 不应获得 `/root`、SSH
密钥或系统配置的写权限。

向 Bot 发送 `/whoami`，把返回的 ID 写入 `TELEGRAM_ALLOWED_USER_IDS`，再执行：

```bash
systemctl restart mini-hermes
```

白名单为空时，Bot 只响应 `/start` 和 `/whoami`，不会调用模型。

## 验证与排错

### 交互升级的离线候选验收

图片/文本入口与 broker 协议需成套部署。先在无 `.env`、无业务数据库、无账号
文件的临时候选目录测试，显式指定候选 `PYTHONPATH`，避免误用生产源码。
用独立网络/挂载隔离隐藏生产目录、pi 账号与 socket；保留临时 Unix socket
供回归测试使用。只执行 `tests/test_*.py` 与模拟 Node 测试，不执行真实模型、
Telegram、审批接口或 `smoke_*`。原有默认 `--self-test` 会初始化数据库并探测
MCP，不能当作完全只读/离线检查。

本轮无需改 `.env`、systemd、OAuth、模型默认配置，也无需新增外部依赖。
部署前确认没有普通聊天或研究请求在跑，记录干净的 Git 提交，再重启主 Bot 和
pi broker。重启后验证 socket、模型固定值、服务状态及部署检查；真实附件效果
需用户在 Telegram 发一份非敏感样例确认，不应伪造成功记录。

Grok 研究中继要求 pi 0.85.1、Node >=22.19、`PI_CHANNEL_MODEL=xai/grok-4.6`。
`PI_CHANNEL_THINKING=low` 保留为服务默认及历史维护档位；Telegram 两类任务
通过 `/thinking` 独立选择四个允许档位，无需改 unit、模型基线或 OAuth。
启动时幂等新增 `thinking_preferences` 表；不改旧表或聊天记录。旧版本忽略该表，
回退源码不应删除已保存的偏好。菜单/主 Bot/模型中继需要成套上线。
当前 Python 适配器依照已锁定的 PydanticAI 2.22.0
消息接口实现；升级依赖后必须重跑多轮工具/签名回放测试。

离线回归（不读真实聊天，不调用模型、不发送 Telegram）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=app .venv/bin/python -m unittest discover -s tests -v
```

Node 套件（研究中继 16 项 + 已安装 pi 的 CLI 附件 8 项）由单一入口执行，
CI 与生产发布检查调用同一脚本，套件清单不会两边漂移：

```bash
sudo scripts/run_node_suites.sh
```

该脚本要求已安装 pi（默认 `/usr/lib/node_modules/@earendil-works/pi-coding-agent`，
可用 `PI_NATIVE_PACKAGE_DIR` 指向 CI/沙箱副本）、`unshare`，并在 `unshare --net`
网络隔离下运行附件套件；环境不满足时以退出码 2 明确 BLOCKED，不静默跳过。

部署时先验收候选，再同步代码，把生产 `.env` 的 `MODEL` 改为 `pi:grok-4.6`，
确认无正在执行的研究/聊天后重启 `mini-hermes-pi` 和 `mini-hermes`。
只有修改 `.env` 而未重启主 Bot 时，旧模型仍留在运行进程里。
不要复制 `auth.json` 或放宽其权限；原有 OAuth 刷新由 pi 的模型运行时负责。
回退应用代码时也须恢复与旧代码兼容的 MODEL；不能把已到期的 Ollama 回退称为恢复可用。

```bash
cd /opt/mini-hermes
PYTHONPATH=/opt/mini-hermes/app .venv/bin/python -m mini_hermes --self-test
systemctl status mini-hermes --no-pager
journalctl -u mini-hermes -n 100 --no-pager
```

## 更新代码后重启（必做闸门）

### Python 依赖安全基线（2026-09-10）

`genai-prices` 间接引入的 `httpx2` / `httpcore2` 固定为 `2.12.0`，
与 `requirements-frozen.txt` 保持一致。不要将二者与 Telegram 使用的
`httpx==0.28.1` / `httpcore==1.0.9` 混淆或替换。
此版本覆盖本仓库 Dependabot #1–#6 的修复，包括 SOCKS 下安全 WebSocket
的 TLS、SSE 缓冲、请求头校验和流式解压内存边界；见
[上游发布说明](https://github.com/pydantic/httpx2/releases/tag/v2.12.0)。

新环境和 CI 使用相同的完整版本清单，保留顶层依赖的 extras：

```bash
.venv/bin/python -m pip install -r requirements.lock -r requirements-frozen.txt
.venv/bin/python -m pip check
```

已有生产环境本次只更新上述两个包。先在不含 `.env`、账号或业务数据的
独立环境安装完整清单，运行离线部署闸门及依赖安全回归测试；验收通过后，
确认 pi 聊天与研究均空闲，再安装相同的两个已验收版本并重启两个 Python
服务。不要只修改锁文件而保留线上旧包，也不要为关闭告警而手动忽略漏洞。
更新锁定版本时同步维护依赖安全回归用例；该用例检查已知安全下限与两份清单
的一致性，不替代 Dependabot 对未来漏洞的检测。

在 `systemctl restart` **之前**于 `/opt/mini-hermes` 执行：

```bash
./deploy/pre-restart-gate.sh
# 无 .env 的环境（如纯 CI）可：
# ./deploy/pre-restart-gate.sh --skip-self-test
systemctl restart mini-hermes-pi mini-hermes
```

闸门顺序：`compileall` → `pytest` → `--self-test`（有 `.env` 时）。任一步失败则不要重启。

## 内存上限（1.9Gi 主机）

unit 默认 `MemoryMax`：`mini-hermes` **600M**，`mini-hermes-pi` **700M**，为 nginx 等其它进程留余量。改 unit 后需：

```bash
cp deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart mini-hermes-pi mini-hermes
```

## Telegram 网络与错误日志

`python-telegram-bot` Application 已设置 connect/read/write/pool 超时；长轮询
`getUpdates` 的瞬时网络错误由 PTB 自身重试。业务 `error_handler` 对
`NetworkError` / `TimedOut` / `RetryAfter`（及 httpx Read/Connect）按类型限流：
默认同一错误类最多每 5 分钟打一次完整堆栈，其余为单行 WARNING，避免 journal 被刷爆。
