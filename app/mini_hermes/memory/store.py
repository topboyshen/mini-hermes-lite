from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sqlite3


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_chat_id INTEGER,
                investment_style TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, symbol),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_user_id
                ON conversations(user_id, id DESC);
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """
        )
        self._connection.commit()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("MemoryStore is not initialized")
        return self._connection

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    async def touch_user(
        self, user_id: int, username: str | None, first_name: str | None, chat_id: int
    ) -> None:
        timestamp = _now()
        async with self._lock:
            self._db().execute(
                """
                INSERT INTO users(user_id, username, first_name, last_chat_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_chat_id=excluded.last_chat_id,
                    updated_at=excluded.updated_at
                """,
                (user_id, username, first_name, chat_id, timestamp, timestamp),
            )
            self._db().commit()

    async def add_watch(self, user_id: int, symbol: str) -> bool:
        async with self._lock:
            cursor = self._db().execute(
                "INSERT OR IGNORE INTO watchlist(user_id, symbol, created_at) VALUES (?, ?, ?)",
                (user_id, symbol, _now()),
            )
            self._db().commit()
            return cursor.rowcount > 0

    async def remove_watch(self, user_id: int, symbol: str) -> bool:
        async with self._lock:
            cursor = self._db().execute(
                "DELETE FROM watchlist WHERE user_id=? AND symbol=?", (user_id, symbol)
            )
            self._db().commit()
            return cursor.rowcount > 0

    async def list_watch(self, user_id: int) -> list[str]:
        async with self._lock:
            rows = self._db().execute(
                "SELECT symbol FROM watchlist WHERE user_id=? ORDER BY created_at", (user_id,)
            ).fetchall()
            return [str(row["symbol"]) for row in rows]

    async def set_style(self, user_id: int, style: str) -> None:
        async with self._lock:
            self._db().execute(
                "UPDATE users SET investment_style=?, updated_at=? WHERE user_id=?",
                (style, _now(), user_id),
            )
            self._db().commit()

    async def get_style(self, user_id: int) -> str:
        async with self._lock:
            row = self._db().execute(
                "SELECT investment_style FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return str(row["investment_style"]) if row else ""

    async def has_user(self, user_id: int) -> bool:
        async with self._lock:
            row = self._db().execute(
                "SELECT 1 FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return row is not None

    async def get_last_chat_id(self, user_id: int) -> int | None:
        async with self._lock:
            row = self._db().execute(
                "SELECT last_chat_id FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None or row["last_chat_id"] is None:
                return None
            return int(row["last_chat_id"])

    async def recent_messages(self, user_id: int, limit: int) -> list[tuple[str, str]]:
        async with self._lock:
            rows = self._db().execute(
                "SELECT role, content FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [(str(row["role"]), str(row["content"])) for row in reversed(rows)]

    async def append_turn(self, user_id: int, user_text: str, assistant_text: str) -> None:
        timestamp = _now()
        async with self._lock:
            self._db().executemany(
                "INSERT INTO conversations(user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [
                    (user_id, "user", user_text, timestamp),
                    (user_id, "assistant", assistant_text, timestamp),
                ],
            )
            self._db().execute(
                """
                DELETE FROM conversations WHERE user_id=? AND id NOT IN (
                    SELECT id FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 100
                )
                """,
                (user_id, user_id),
            )
            self._db().commit()

    async def set_subscription(self, user_id: int, chat_id: int, enabled: bool) -> None:
        async with self._lock:
            self._db().execute(
                """
                INSERT INTO subscriptions(user_id, chat_id, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (user_id, chat_id, int(enabled), _now()),
            )
            self._db().commit()

    async def subscriptions(self) -> list[tuple[int, int]]:
        async with self._lock:
            rows = self._db().execute(
                "SELECT user_id, chat_id FROM subscriptions WHERE enabled=1"
            ).fetchall()
            return [(int(row["user_id"]), int(row["chat_id"])) for row in rows]

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            result: dict[str, int] = {}
            for table in ("users", "watchlist", "conversations", "subscriptions"):
                row = self._db().execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                result[table] = int(row["count"])
            return result
