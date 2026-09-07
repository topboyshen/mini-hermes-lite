# Mini Hermes 部署指南

- 系统：Debian 12，Python 3.11，systemd 252
- 安装目录：`/opt/mini-hermes`
- 服务账户：`mini-hermes`（无登录 shell）
- systemd：`/etc/systemd/system/mini-hermes.service`
- 日志：`journalctl -u mini-hermes` 与 `/var/log/mini-hermes/mini-hermes.log`
- 数据库：`/opt/mini-hermes/data/memory.db`
- MCP：`quanters-akshare-mcp@0.2.0`，自检发现 21 个工具

## 安装或刷新 systemd unit

部署副本和 systemd 目标都必须由 root 持有，且不可被普通本机用户修改：

```bash
chmod 0644 /opt/mini-hermes/deploy/mini-hermes.service \
  /opt/mini-hermes/deploy/mini-hermes-pi.service
install -o root -g root -m 0644 \
  /opt/mini-hermes/deploy/mini-hermes.service \
  /etc/systemd/system/mini-hermes.service
install -o root -g root -m 0644 \
  /opt/mini-hermes/deploy/mini-hermes-pi.service \
  /etc/systemd/system/mini-hermes-pi.service
systemd-analyze verify /etc/systemd/system/mini-hermes.service \
  /etc/systemd/system/mini-hermes-pi.service
systemctl daemon-reload
```

执行安装前先比较 deploy 副本与当前 live unit，并备份 `/etc/systemd/system/`
中的现有目标；unit 内容发生变化时，需按变更范围单独安排服务重启和验收。

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
