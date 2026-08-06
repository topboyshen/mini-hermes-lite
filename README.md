# Mini Hermes Lite v0.1

低资源、长期在线、Telegram 驱动的 AI Agent 节点。普通私聊消息进入 pi coding
agent；A 股命令由 PydanticAI 调用 `quanters-akshare-mcp` 完成，海外市场晨报采用
隔离的行情与判定路径。SQLite
保存用户偏好、自选股与短期对话，Telegram JobQueue（APScheduler）在每天 08:30
推送晨报。

## 命令

- `/stock 600875`：个股研究
- `/market`：市场概览
- `/report`：立即生成海外市场晨报
- `/watch add|remove|list`：管理自选股
- `/style 稳健中线`：保存投资风格
- `/subscribe`、`/unsubscribe`：管理 08:30 晨报
- `/status`：CPU、内存、MCP、API 状态
- `/whoami`：显示 Telegram 用户 ID

### Telegram 单通道

- 普通私聊消息：直接进入持续的 pi 会话，无需切换模式
- `/new`：新建空白 pi 会话
- `/abort`：中止当前 pi 任务
- `/pistatus`：查看 channel 模型和忙碌状态

`/stock`、`/market` 等 A 股命令始终进入 Mini Hermes；`/report` 与定时报告不调用模型、
不挂载 AKShare，也不读取自选股和对话记忆。晨报覆盖美股三大指数、VIX、美国 2Y/10Y
国债收益率、美元指数、COMEX 黄金、WTI 原油和 BTC，并显示跨资产项目的数据时间与完整度。
所有数字、强弱排序、固定观察阈值和最终文字均由程序确定性生成，不经过模型。任一外部
数据源失败时只省略对应项目，不会阻断整份报告。Telegram 输出采用移动端纯文本信息卡，
无需 Markdown 渲染。定时报告在美股交易日收盘后的上海时间
08:30 推送，通常为周二至周六。旧的 `/pi` 和 `/hermes` 命令仅保留兼容提示，不再改变
路由状态。

美股三大指数使用带源时间戳的行情，并换算为美东交易日期。三项数据必须来自同一个、
符合 NYSE 常规交易日历的最近已完成交易日；日期缺失、不一致、过期或仍处于美股盘中时，
报告会明确降级并停止生成指数方向、排序、温度及阈值结论。

pi 以独立的 `pi-channel` 系统用户运行，拥有完整的编码 Agent 工具，但写入范围限制在
`/opt/mini-hermes/workspace`。会话保存在 `/var/lib/mini-hermes-pi/sessions`，Telegram
Token、Mini Hermes `.env` 和 `/root` 不对 pi channel 开放。远程执行仅允许白名单
用户在 Bot 私聊中触发，群聊请求会被拒绝。

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
