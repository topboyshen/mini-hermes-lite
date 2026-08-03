"""Manual MCP data smoke test; calls one read-only market overview tool."""

import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command="/usr/bin/quanters-akshare-mcp",
        args=[],
        env={
            **os.environ,
            "AKSHARE_MAX_ROWS": "20",
            "QUANTERS_USE_AKTOOLS": "false",
        },
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool("get_market_overview_tool", {})
            if result.isError:
                raise RuntimeError(str(result.content))
            text = "\n".join(
                str(getattr(item, "text", item)) for item in result.content
            )
            if not text.strip():
                raise RuntimeError("AKShare MCP returned an empty response")
            print(text[:2000])


if __name__ == "__main__":
    asyncio.run(main())
