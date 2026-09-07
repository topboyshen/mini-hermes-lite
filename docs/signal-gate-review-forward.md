# Signal Gate 审批按钮接入记录

此文档记录现有接入，不改变审批规则、授权策略或交易行为。

## 文件与职责

- `app/mini_hermes/gateway/telegram/bot.py`：在 `_register_handlers()` 中注册 `^sg:(ok|no):\d+$` 回调；主 Bot 保持 Telegram `getUpdates` 所有权。
- `app/mini_hermes/gateway/telegram/signal_gate_review_forward.py`：将回调包装为 `callback_query`，转发给本机 Signal Gate review 服务。
- `tests/test_signal_gate_review_forward.py`：仅验证注册、传输结构、可选认证头和现有异常处理，不执行真实审批。

审批身份验证、批准/拒绝、去重和后续 Quant 请求属于接收端职责；转发函数本身不调用主 Bot 的 `_authorized()`。单元测试通过不能证明接收端的真实授权或审批链路已验收。

## 配置与数据边界

`SIGNAL_GATE_REVIEW_FORWARD_URL` 指向接收端，现有默认目标是本机 `127.0.0.1:8096` 的回调路径。

`SIGNAL_GATE_REVIEW_FORWARD_SECRET` 非空时作为 `X-Review-Forward-Secret` 请求头发送；为空时按现有行为不发送该头。本次版本收尾不改变这一策略。真实密钥、`.env`、用户标识、真实回调样例、审批数据库和消息内容均不应纳入 Git。

现有转发载荷含用户身份、回调数据和原始消息文本；现有日志还记录回调数据及接收端响应的前 200 个字符。排障材料不要直接公开原始日志或真实载荷。本次只记录这一隐私边界，不将其误称为已完成脱敏改造。

## 离线回归

从项目根目录执行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=app .venv/bin/python -m unittest discover -s tests -p test_signal_gate_review_forward.py -v
```

测试使用合成用户、卡片、回调和密钥；`urllib.request.urlopen`、线程转发及 Telegram 回答全部 mock。不读取生产数据库，不 POST 到接收端，不调用 Telegram、Quant 或模型。完整部署前仍应运行现有项目全套离线测试。

## 版本与运行进程一致性

应一起纳入版本管理：上述两个已有接入源文件、新增回归测试和本文档；保留接入内容，不用旧版本覆盖用户的未提交改动。

仅创建 Git 提交不会更新 Python 已加载的模块。核对源码、测试和没有正在执行的业务后，若需要确保主 Bot 运行的是提交后的文件，应按部署流程重启 `mini-hermes`。仅此转发接入的版本收尾不需要重启 `mini-hermes-pi`。重启后的服务状态和版本检查属于部署验证；不要为了“验证”而自动发送真实审批回调或 Quant 请求。
