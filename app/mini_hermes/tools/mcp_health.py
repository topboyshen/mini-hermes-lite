from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ValueError(f"Missing environment variable: {name}")

    return _ENV_PATTERN.sub(replace, value)


async def _probe_stdio(name: str, config: dict[str, Any]) -> dict[str, Any]:
    env = {**os.environ}
    env.update({key: _expand(str(value)) for key, value in config.get("env", {}).items()})
    params = StdioServerParameters(
        command=_expand(str(config["command"])),
        args=[_expand(str(value)) for value in config.get("args", [])],
        env=env,
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.list_tools()
            names = [tool.name for tool in result.tools]
            return {"name": name, "status": "ok", "tool_count": len(names), "tools": names}


async def probe_mcp_servers(config_path: Path, timeout_seconds: int = 30) -> list[dict[str, Any]]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for name, config in data.get("mcpServers", {}).items():
        if "command" not in config:
            results.append({"name": name, "status": "configured", "transport": "http"})
            continue
        try:
            result = await asyncio.wait_for(
                _probe_stdio(name, config), timeout=timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - health check must report all failures
            result = {"name": name, "status": "error", "error": str(exc)}
        results.append(result)
    return results
