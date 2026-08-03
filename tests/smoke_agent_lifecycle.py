"""Manual integration smoke test; no model request is made."""

import asyncio
from pathlib import Path

from mini_hermes.agent import AgentService
from mini_hermes.config import Settings
from mini_hermes.memory import MemoryStore


async def main() -> None:
    settings = Settings.from_env(Path("/opt/mini-hermes/.env"))
    memory = MemoryStore(settings.database_path)
    await memory.initialize()
    agent = AgentService(settings, memory)
    await agent.start()
    assert agent.ready
    assert agent.toolset_count == 1
    await agent.stop()
    await memory.close()
    print("agent lifecycle: ok")


if __name__ == "__main__":
    asyncio.run(main())
