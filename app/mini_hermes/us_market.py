"""美股三大指数隔夜行情抓取 (新浪财经国际指数接口).

数据源: https://hq.sinajs.cn/list=int_dji,int_nasdaq,int_sp500
GBK 编码, 需 Referer. 返回格式: 名称,最新价,涨跌额,涨跌幅%
用于晨报「美股隔夜总结」板块, 把真实数据注入 prompt, 避免模型凭记忆编造.
"""
from __future__ import annotations

import logging

import httpx

LOGGER = logging.getLogger(__name__)

_SINA_URL = "https://hq.sinajs.cn/list=int_dji,int_nasdaq,int_sp500"
_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 mini-hermes/0.1",
}
# 代码 -> 中文名 (接口名是 GBK, 用固定名更稳)
_INDICES = {
    "int_dji": "道琼斯工业指数",
    "int_nasdaq": "纳斯达克综合指数",
    "int_sp500": "标普500指数",
}


async def fetch_us_overview() -> str:
    """返回美股三大指数文本块, 失败返回空串 (调用方按空降级)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_SINA_URL, headers=_SINA_HEADERS)
            raw = resp.content.decode("gbk", errors="replace")
    except Exception as e:  # noqa: BLE001
        LOGGER.warning("us_overview_fetch_failed: %s", e)
        return ""

    lines: list[str] = []
    for code, name in _INDICES.items():
        token = f'var hq_str_{code}="'
        i = raw.find(token)
        if i < 0:
            continue
        start = i + len(token)
        end = raw.find('"', start)
        if end < 0:
            continue
        body = raw[start:end]
        if not body:
            continue
        parts = body.split(",")
        try:
            if len(parts) >= 4:
                price, chg, pct = parts[1], parts[2], parts[3]
                sign = "+" if float(chg) >= 0 else ""
                lines.append(f"- {name}: {price} 点, 涨跌 {sign}{chg}, {sign}{pct}%")
            elif len(parts) >= 2:
                lines.append(f"- {name}: {parts[1]} 点")
        except (ValueError, IndexError):
            continue
    return "\n".join(lines)