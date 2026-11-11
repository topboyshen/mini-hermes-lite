from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
_report_model_warned = False


def load_env_file(path: Path) -> None:
    """Load a simple KEY=VALUE file without overwriting exported variables."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _int_set(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


def _warn_orphan_report_model() -> None:
    """REPORT_MODEL is unused (reports are deterministic); warn once if present."""
    global _report_model_warned
    if _report_model_warned:
        return
    if "REPORT_MODEL" not in os.environ:
        return
    _report_model_warned = True
    _LOGGER.warning(
        "REPORT_MODEL is set in the environment but unused; "
        "overseas reports are deterministic and do not call a model. "
        "Safe to remove from .env."
    )


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    model: str
    timezone: str
    daily_report_time: str
    agent_timeout_seconds: int
    max_history_messages: int
    max_concurrent_agent_runs: int
    pi_channel_socket: Path
    pi_channel_timeout_seconds: int
    data_dir: Path
    log_dir: Path
    mcp_config: Path

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        if env_file is not None:
            load_env_file(env_file)
        _warn_orphan_report_model()
        return cls(
            telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
            allowed_user_ids=_int_set(os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")),
            model=os.getenv("MODEL", "openai:gpt-5-mini").strip(),
            timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai").strip(),
            daily_report_time=os.getenv("DAILY_REPORT_TIME", "08:20").strip(),
            agent_timeout_seconds=int(os.getenv("AGENT_TIMEOUT_SECONDS", "120")),
            max_history_messages=int(os.getenv("MAX_HISTORY_MESSAGES", "8")),
            max_concurrent_agent_runs=int(os.getenv("MAX_CONCURRENT_AGENT_RUNS", "2")),
            pi_channel_socket=Path(
                os.getenv("PI_CHANNEL_SOCKET", "/run/mini-hermes-pi/pi.sock")
            ),
            pi_channel_timeout_seconds=int(os.getenv("PI_CHANNEL_TIMEOUT_SECONDS", "600")),
            data_dir=Path(os.getenv("DATA_DIR", "/opt/mini-hermes/data")),
            log_dir=Path(os.getenv("LOG_DIR", "/var/log/mini-hermes")),
            mcp_config=Path(os.getenv("MCP_CONFIG", "/opt/mini-hermes/config/mcp.json")),
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "memory.db"

    def missing_runtime_settings(self) -> list[str]:
        missing: list[str] = []
        if not self.telegram_token:
            missing.append("TELEGRAM_TOKEN")
        key_by_provider = {
            "openai": "OPENAI_API_KEY",
            "openai-chat": "OPENAI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        provider = self.model.partition(":")[0].lower()
        key_name = key_by_provider.get(provider)
        if key_name and not os.getenv(key_name, "").strip():
            missing.append(key_name)
        return missing
