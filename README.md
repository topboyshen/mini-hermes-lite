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

### 日常交互升级（2026-09-08）

发送 `/start` 或 `/help` 显示常用按钮：功能帮助、任务状态、海外晨报、本周简报。
按钮不会改变订阅或触发信号审批；晨报/周报按钮与原有查询命令相同。

- 私聊中直接发送一张 JPG、PNG 或 WebP 图片（最多 1 MiB），或一个 UTF-8
  TXT、MD、CSV、JSON 文本文件（最多 128 KiB），可附上要问的问题；不附说明时
  默认描述图片或概括文本。相册、多附件、PDF、Office、压缩包、语音暂不支持。
- 图片/文件总是进入普通 pi 会话，不因附件中的股票代码自动切换 A 股研究。
- pi 会话最近有效用量超过 48,000 token（包括缓存输入）时，在下一次请求前
  通过原生 RPC 整理旧历史；默认保留约 20,000 token 近期内容，并追加简短摘要，
  不删除原始会话。整理可能暂时增加该次请求耗时，后续请求上下文会更轻。
  失败后仍使用原历史，1 小时内不重复自动尝试；可用 `/abort` 中止整理。
  内部 `compact` 维护操作仅允许 root/主 Bot 用户发起，并与 pi 请求共用互斥锁。
- Telegram 的“正在输入”提示独立、最多等待 2 秒，不再阻塞模型请求。
- iPhone 实况照片会提取 Telegram 提供的静态画面，沿用上述大小与内容校验；
  不下载动态视频。没有静态画面时明确提示改发普通照片或截图，不再静默忽略。
  附件接收/就绪日志只记录固定类型，不记录说明、文件 ID、原名或下载地址。
  查询实时 A 股数据仍使用 `/stock`、`/market` 或纯文字股票代码。
- 下载仅允许 Telegram HTTPS 文件入口，不跟随重定向、不接收压缩传输；按实际
  读取字节硬性限量。文件名被替换为固定名称，不把路径、原文件名或内容写入日志。
- 附件经 Unix socket 传输字节，broker 独立验证 MIME、大小、UTF-8 或图片结构/
  像素边界，再建立 0700 临时目录/0600 临时文件。原始临时文件在 pi 读取后或
  异常退出时清理；接入层不自动执行附件，不共享 `/tmp`、不扩大服务文件权限。
  这不是完整的图片像素解码验证，图片仍由 pi 自带处理器解码。
- 附件内容属于用户输入，不能提升为系统指令。原附件不复制到主 Bot 的 SQLite；
  pi 仍按原有会话规则保存用户消息（可能包含图片/文本内容），并非阅后即焚。
- 长任务在一条消息中显示实际阶段和耗时，最多每 8 秒更新一次，不显示猜测的
  完成百分比、工具参数或模型内部推理；更新失败不阻断结果。
- 同时最多接收两个交互长任务，额外请求明确提示稍后再试，不建无限队列。
  `/abort` 与任务状态查询不占长任务名额；`/abort` 只中止本用户当前运行的
  pi 任务，不撤销排队任务或 A 股研究。忙碌时 `/new` 立即提示，避免卡住控制命令。

模型仍为 Grok 4.6，账号授权和 pi 0.85.1 保留；晨报、周报、A 股模型中继和
Signal Gate 审批路径不因本轮交互升级而改变。

### Telegram 单通道

- 六位 A 股代码、`分析 600875`：进入 A 股研究，与 `/stock` 使用同一模型和行情工具
- 其余普通私聊消息：进入持续的 pi 会话，无需切换模式
- `/new`：新建空白 pi 会话
- `/abort`：中止当前 pi 任务
- `/pistatus`：查看 channel 模型和忙碌状态

`/stock`、`/market` 等 A 股命令始终进入 Mini Hermes；`/report` 与定时报告不调用模型、
不挂载 AKShare，也不读取自选股和对话记忆。晨报覆盖美股三大指数（含开高低与 52 周
位置）、亚太欧指数（日经、恒生、富时100）、VIX、美国 3M/2Y/10Y/30Y 国债收益率、
美元指数、COMEX 黄金与白银（含金银比）、WTI 原油和 BTC，并显示跨资产项目的数据时间
与完整度。
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

普通 pi 聊天失败会明确区分授权失效、额度/订阅不足、限流、超时、连接故障和无效响应。
错误提示和日志仅包含固定脱敏类别，不转发供应商原始诊断。主动取消不计入失败。
JSON 事件会一直读取到进程结束，保留 pi 自带的重试机会；末尾错误不会被先前的半截
文字或工具前言掩盖。空白回答同样视为失败，不再显示含糊的“没有最终答案”。

每次聊天记录唯一 `request_id` 和一次 `pi_request_finished`，含 `outcome`、
`error_code`、耗时及文本长度，不记录提问/回答内容。服务器业务监控据此统计连续
失败：两次不同请求失败即可告警，不要求四次请求；没有新请求或用户取消都不表示恢复。
后续成功才形成恢复证据，具体通知去重和状态持久化由基础设施监控负责。

## 配置备注

`MODEL=pi:grok-4.6` 让 A 股研究复用 pi-channel 中已有的 Grok 账号授权。
模型中继只执行一次原生模型请求，不运行 pi 编程 Agent、扩展或 shell 工具；
原有 AKShare MCP 工具、研究指令、自选及 Hermes 历史仍由主 Bot 管理。
账号凭据不离开 pi-channel，不复制到主 Bot、配置模板或数据库。
此模式显式忽略残留的 `OPENAI_BASE_URL`/`OPENAI_API_KEY`，不会失败后回退到 Ollama。

中继使用既有 Unix socket，仅允许主 Bot 与 root 调用；固定 Grok 4.6 / low，
研究请求单独限流、限时和取消，不终止普通聊天。单次输入/输出最多 2MiB，
默认最多 4096 输出 token；只支持文本与函数工具，不支持图片或结构化输出模式。
OAuth 失效、额度、限流等错误返回脱敏类别，不回显模型原始错误或凭据。

`REPORT_MODEL` 不是有效 Settings 键（晨报不调用模型）；可从生产 `.env` 删除。详见 `.env.example`。

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

更新代码后先跑部署闸门，再重启（详见 `DEPLOYMENT.md`）：

```bash
./deploy/pre-restart-gate.sh
systemctl restart mini-hermes-pi mini-hermes
```

服务配置文件为 `/opt/mini-hermes/.env`。只改配置时可：

```bash
systemctl restart mini-hermes
systemctl restart mini-hermes-pi mini-hermes
```
