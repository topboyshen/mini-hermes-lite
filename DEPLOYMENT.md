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
2. `MODEL` 和对应的 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 或 `ANTHROPIC_API_KEY`

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

```bash
cd /opt/mini-hermes
PYTHONPATH=/opt/mini-hermes/app .venv/bin/python -m mini_hermes --self-test
systemctl status mini-hermes --no-pager
journalctl -u mini-hermes -n 100 --no-pager
```

## 更新代码后重启（必做闸门）

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
