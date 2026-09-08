"""Allowlisted model failure codes and fixed, credential-free user messages."""
from __future__ import annotations

import re


PI_ERRORS = {
    "auth_required": "模型账号授权不可用或已过期，请重新授权后重试。",
    "quota": "模型账号额度不足或订阅已到期，请检查订阅状态。",
    "rate_limited": "模型请求过于频繁，请稍后重试。",
    "timeout": "请求超时，请缩小任务范围后重试。",
    "upstream_error": "模型服务暂时不可用，请稍后重试。",
    "invalid_request": "请求格式不受支持，请简化任务后重试。",
    "model_unavailable": "模型暂不可用或配置不一致，请检查模型配置。",
    "output_limit": "模型输出达到长度限制，请缩小任务范围后重试。",
    "cancelled": "任务已取消。",
    "channel_unavailable": "pi 通道连接不可用，请稍后重试。",
    "channel_busy": "当前有 pi 任务正在运行，请完成后或用 /abort 中止后再新建会话。",
    "invalid_response": "模型未返回有效的最终答复，请稍后重试。",
}


def normalize_error_code(code: object) -> str:
    return code if isinstance(code, str) and code in PI_ERRORS else "upstream_error"


def error_message(code: object) -> str:
    return PI_ERRORS[normalize_error_code(code)]


def classify_pi_failure(
    error_text: str = "", stop_reason: str = "", exit_code: int | None = None
) -> str:
    """Classify *failure metadata only*, never user input or assistant answers.

    The input is bounded and is never returned, logged, or included in messages.
    HTTP 429 can mean billing exhaustion, so explicit quota clues take precedence.
    """
    text = error_text[-32768:].casefold() if isinstance(error_text, str) else ""
    reason = stop_reason.casefold() if isinstance(stop_reason, str) else ""
    if reason in {"aborted", "cancelled", "canceled"} or text.strip() in {
        "retry cancelled", "retry canceled", "任务已取消", PI_ERRORS["cancelled"],
    }:
        return "cancelled"
    if reason in {"length", "max_tokens", "output_limit"} or re.search(
        r"max(?:imum)? output tokens|output.{0,24}(?:limit|truncat)|finish.reason.{0,12}length|输出.{0,12}(?:长度|限制)", text
    ):
        return "output_limit"
    short_window_limit = re.search(
        r"per[_ -](?:minute|second)|(?:minute|second)[_ -]quota|\b(?:rpm|tpm|rps)\b", text
    )
    billing = re.search(
        r"insufficient[_ ](?:quota|credits?|balance)|(?:credit|billing|payment|subscription).{0,55}(?:exhaust|insufficient|past due|expired|inactive|limit|unpaid)|(?:exhausted|insufficient).{0,24}(?:credits?|balance)|payment required|\b402\b|额度不足|订阅.{0,8}(?:到期|过期)|余额不足|欠费", text
    )
    quota = re.search(r"quota.{0,24}(?:exceed|exhaust|deplet)|(?:exceed|exhaust|deplet).{0,24}quota", text)
    if billing or (quota and not short_window_limit):
        return "quota"
    if short_window_limit or re.search(r"\b429\b|rate[_ -]?limit|too many requests|限流|请求过于频繁", text):
        return "rate_limited"
    if re.search(
        r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|not authenticated|authentication|(?:oauth|authorization|api[_ -]?key|access token|refresh token|credentials?).{0,40}(?:invalid|expired|missing|revoked|unavailable|not found|required)|(?:invalid|expired|missing|no).{0,24}(?:api[_ -]?key|access token|refresh token|credentials?)|授权.{0,12}(?:不可用|过期|失效)|未授权", text
    ):
        return "auth_required"
    if reason == "timeout" or re.search(
        r"timed?\s*out|timeout|etimedout|deadline exceeded|请求超时|研究请求超时", text
    ):
        return "timeout"
    if re.search(
        r"model.{0,40}(?:not found|not available|unavailable|unknown|unsupported|does not exist)|unknown model|model_not_found|模型.{0,18}(?:不存在|不可用|不一致)", text
    ):
        return "model_unavailable"
    if re.search(
        r"invalid[_ ](?:request|argument|schema)|bad request|context[_ ](?:length|window)|maximum context|\b400\b|\b422\b|请求格式|请求参数", text
    ):
        return "invalid_request"
    if re.search(
        r"channel unavailable|connection refused|econnrefused|socket.{0,30}(?:missing|unavailable|not found)|通道连接不可用", text
    ):
        return "channel_unavailable"
    if text or reason == "error" or exit_code not in {None, 0}:
        return "upstream_error"
    return "invalid_response"
