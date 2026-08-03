# Mini Hermes Lite v0.1

低资源、长期在线、Telegram 驱动的 A 股研究 Agent。它使用 PydanticAI 调用
`quanters-akshare-mcp`，SQLite 保存用户偏好、自选股与短期对话，Telegram JobQueue
（APScheduler）在每天 08:30 推送晨报。

## 命令

- `/stock 600875`：个股研究
- `/market`：市场概览
- `/report`：立即生成晨报
- `/watch add|remove|list`：管理自选股
- `/style 稳健中线`：保存投资风格
- `/subscribe`、`/unsubscribe`：管理 08:30 晨报
- `/status`：CPU、内存、MCP、API 状态
- `/whoami`：显示 Telegram 用户 ID

### pi 完整交互 channel

- `/pi`：进入持续 pi 会话，后续普通消息都交给 pi
- `/pi <任务>`：立即执行任务并进入 pi 模式
- `/new`：新建空白 pi 会话
- `/abort`：中止当前 pi 任务
- `/pistatus`：查看 channel 模型和忙碌状态
- `/hermes`：退出 pi 模式，返回 A 股 Agent

pi 以独立的 `pi-channel` 系统用户运行，拥有完整的编码 Agent 工具，但写入范围限制在
`/opt/mini-hermes/workspace`。会话保存在 `/var/lib/mini-hermes-pi/sessions`，Telegram
Token、Mini Hermes `.env` 和 `/root` 不对 pi channel 开放。

## 安全默认值

`.env` 中未配置 `TELEGRAM_ALLOWED_USER_IDS` 时，Bot 只响应 `/start` 和
`/whoami`，不会调用模型。模型密钥、Telegram Token 不进入代码或 SQLite。

## 运维

```bash
systemctl status mini-hermes
systemctl status mini-hermes-pi
journalctl -u mini-hermes -f
journalctl -u mini-hermes-pi -f
tail -f /var/log/mini-hermes/mini-hermes.log
/opt/mini-hermes/.venv/bin/python -m mini_hermes --self-test
```

服务配置文件为 `/opt/mini-hermes/.env`。修改后执行：

```bash
systemctl restart mini-hermes
systemctl restart mini-hermes-pi mini-hermes
```
