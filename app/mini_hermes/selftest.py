from __future__ import annotations

import json

from mini_hermes.config import Settings
from mini_hermes.memory import MemoryStore
from mini_hermes.tools import probe_mcp_servers


async def run_selftest(settings: Settings) -> int:
    checks: dict[str, object] = {
        "version": "0.1.0",
        "database": "pending",
        "mcp": [],
        "runtime_missing": settings.missing_runtime_settings(),
    }
    store = MemoryStore(settings.database_path)
    try:
        await store.initialize()
        await store.stats()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"
    finally:
        await store.close()

    try:
        checks["mcp"] = await probe_mcp_servers(settings.mcp_config)
    except Exception as exc:  # noqa: BLE001
        checks["mcp"] = [{"status": "error", "error": str(exc)}]

    print(json.dumps(checks, ensure_ascii=False, indent=2))
    mcp_ok = all(
        isinstance(item, dict) and item.get("status") in {"ok", "configured"}
        for item in checks["mcp"]  # type: ignore[union-attr]
    )
    return 0 if checks["database"] == "ok" and mcp_ok else 1

