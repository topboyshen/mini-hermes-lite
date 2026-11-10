from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import uuid

from mini_hermes.thinking import DEFAULT_THINKING, validate_scope, validate_thinking
from mini_hermes.vix_evidence import encode_report_evidence


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _valid_report_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


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
        self._connection.execute("PRAGMA busy_timeout=5000")
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
            CREATE TABLE IF NOT EXISTS thinking_preferences (
                user_id INTEGER NOT NULL,
                scope TEXT NOT NULL CHECK (scope IN ('chat', 'research')),
                level TEXT NOT NULL CHECK (level IN ('low', 'medium', 'high', 'xhigh')),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, scope),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'hermes',
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
            CREATE TABLE IF NOT EXISTS market_reports (
                report_date TEXT PRIMARY KEY,
                report_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_market_reports_date
                ON market_reports(report_date DESC);
            CREATE TABLE IF NOT EXISTS market_report_details (
                report_id TEXT PRIMARY KEY,
                report_date TEXT NOT NULL,
                report_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_market_report_details_created
                ON market_report_details(created_at DESC);
            CREATE TABLE IF NOT EXISTS market_report_evidence (
                evidence_sha256 TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                report_date TEXT NOT NULL,
                report_sha256 TEXT NOT NULL,
                trigger TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (report_id) REFERENCES market_report_details(report_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_market_report_evidence_report
                ON market_report_evidence(report_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS report_deliveries (
                report_kind TEXT NOT NULL CHECK (report_kind IN ('daily', 'weekly')),
                report_date TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                part_count INTEGER NOT NULL CHECK (part_count > 0),
                created_at TEXT NOT NULL,
                PRIMARY KEY (report_kind, report_date, chat_id)
            );
            CREATE TABLE IF NOT EXISTS report_delivery_parts (
                report_kind TEXT NOT NULL,
                report_date TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                part_index INTEGER NOT NULL CHECK (part_index >= 0),
                content_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('sending', 'confirmed', 'unknown', 'failed')),
                claim_id TEXT NOT NULL,
                message_id INTEGER,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (report_kind, report_date, chat_id, part_index),
                FOREIGN KEY (report_kind, report_date, chat_id)
                    REFERENCES report_deliveries(report_kind, report_date, chat_id)
            );
            """
        )
        self._migrate_conversation_channel()
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversations_user_channel
                ON conversations(user_id, channel, id DESC)
            """
        )
        self._connection.commit()

        # A previous process may have exited after Telegram accepted a part but
        # before its acknowledgement was committed. Never resend such a part.
        with self._connection:
            self._connection.execute(
                "UPDATE report_delivery_parts SET status='unknown' WHERE status='sending'"
            )

    async def claim_report_part(
        self, report_kind: str, report_date: str, chat_id: int,
        content_sha256: str, part_count: int, part_index: int, part_sha256: str,
    ) -> dict[str, object]:
        if report_kind not in {'daily', 'weekly'}:
            raise ValueError('invalid report kind')
        if datetime.fromisoformat(report_date).date().isoformat() != report_date:
            raise ValueError('invalid report date')
        if (type(chat_id) is not int or type(part_count) is not int or part_count <= 0
                or type(part_index) is not int or not 0 <= part_index < part_count
                or any(len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
                       for value in (content_sha256, part_sha256))):
            raise ValueError('invalid report delivery identity')
        identity = (report_kind, report_date, chat_id)
        async with self._lock:
            db = self._db()
            # The first INSERT acquires SQLite's write lock. The identity and
            # part claim are checked in the same transaction across processes.
            with db:
                db.execute(
                    'INSERT OR IGNORE INTO report_deliveries VALUES (?, ?, ?, ?, ?, ?)',
                    (*identity, content_sha256, part_count, _now()),
                )
                delivery = db.execute(
                    'SELECT content_sha256, part_count FROM report_deliveries '
                    'WHERE report_kind=? AND report_date=? AND chat_id=?', identity,
                ).fetchone()
                if delivery['content_sha256'] != content_sha256 or delivery['part_count'] != part_count:
                    raise ValueError('scheduled report content changed')
                row = db.execute(
                    'SELECT * FROM report_delivery_parts WHERE report_kind=? AND report_date=? '
                    'AND chat_id=? AND part_index=?', (*identity, part_index),
                ).fetchone()
                if row is not None:
                    if row['content_sha256'] != part_sha256:
                        raise ValueError('scheduled report part changed')
                    return dict(row)
                claim_id = uuid.uuid4().hex
                db.execute(
                    'INSERT INTO report_delivery_parts '
                    '(report_kind,report_date,chat_id,part_index,content_sha256,status,claim_id,updated_at) '
                    "VALUES (?,?,?,?,?,'sending',?,?)",
                    (*identity, part_index, part_sha256, claim_id, _now()),
                )
            return {'status': 'claimed', 'claim_id': claim_id, 'message_id': None}

    async def finish_report_part(
        self, report_kind: str, report_date: str, chat_id: int, part_index: int,
        claim_id: str, status: str, message_id: int | None = None,
    ) -> None:
        if status not in {'confirmed', 'unknown', 'failed'}:
            raise ValueError('invalid report delivery outcome')
        if status == 'confirmed' and (type(message_id) is not int or message_id <= 0):
            raise ValueError('invalid Telegram acknowledgement')
        if status != 'confirmed' and message_id is not None:
            raise ValueError('message id requires confirmation')
        async with self._lock:
            db = self._db()
            with db:
                cursor = db.execute(
                    'UPDATE report_delivery_parts SET status=?,message_id=?,updated_at=? '
                    'WHERE report_kind=? AND report_date=? AND chat_id=? AND part_index=? '
                    "AND claim_id=? AND status IN ('sending','unknown')",
                    (status, message_id, _now(), report_kind, report_date, chat_id, part_index, claim_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError('report delivery acknowledgement conflicts with durable state')


    def _migrate_conversation_channel(self) -> None:
        """Add channel column to pre-existing SQLite DBs; default existing rows to hermes."""
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(conversations)")
        }
        if "channel" not in columns:
            self._connection.execute(
                "ALTER TABLE conversations ADD COLUMN channel TEXT NOT NULL DEFAULT 'hermes'"
            )

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

    async def get_thinking(self, user_id: int, scope: str) -> str:
        validate_scope(scope)
        async with self._lock:
            row = self._db().execute(
                "SELECT level FROM thinking_preferences WHERE user_id=? AND scope=?",
                (user_id, scope),
            ).fetchone()
            return validate_thinking(row["level"]) if row else DEFAULT_THINKING

    async def set_thinking(self, user_id: int, scope: str, level: str) -> None:
        validate_scope(scope)
        validate_thinking(level)
        async with self._lock:
            # Only this preference changes; no provider, history or subscription writes.
            with self._db():
                self._db().execute(
                    """INSERT INTO thinking_preferences(user_id, scope, level, updated_at)
                       VALUES (?, ?, ?, ?) ON CONFLICT(user_id, scope) DO UPDATE SET
                       level=excluded.level, updated_at=excluded.updated_at""",
                    (user_id, scope, level, _now()),
                )

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

    async def recent_messages(
        self, user_id: int, limit: int, *, channel: str = "hermes"
    ) -> list[tuple[str, str]]:
        async with self._lock:
            rows = self._db().execute(
                """
                SELECT role, content FROM conversations
                WHERE user_id=? AND channel=?
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, channel, limit),
            ).fetchall()
            return [(str(row["role"]), str(row["content"])) for row in reversed(rows)]

    async def append_turn(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        *,
        channel: str = "hermes",
    ) -> None:
        timestamp = _now()
        async with self._lock:
            self._db().executemany(
                """
                INSERT INTO conversations(user_id, role, content, channel, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (user_id, "user", user_text, channel, timestamp),
                    (user_id, "assistant", assistant_text, channel, timestamp),
                ],
            )
            self._db().execute(
                """
                DELETE FROM conversations WHERE user_id=? AND channel=? AND id NOT IN (
                    SELECT id FROM conversations
                    WHERE user_id=? AND channel=?
                    ORDER BY id DESC LIMIT 100
                )
                """,
                (user_id, channel, user_id, channel),
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

    async def save_market_report(
        self, report_date: str, report_text: str, *,
        vix_evidence: Mapping[str, object] | None = None,
        trigger: str = "unspecified",
    ) -> None:
        """Atomically store a report and optional bounded, report-bound evidence.

        Legacy callers remain valid and do not manufacture evidence for old
        reports. An observation is immutable and may coexist with other
        observations of identical rendered text.
        """
        if not report_date or not report_text.strip():
            raise ValueError("report_date and report_text are required")
        evidence_json = (
            encode_report_evidence(vix_evidence, report_date, report_text, trigger)
            if vix_evidence is not None else None
        )
        report_sha256 = sha256(report_text.encode("utf-8")).hexdigest()
        report_id = report_sha256[:32]
        timestamp = _now()
        async with self._lock:
            db = self._db()
            # A failure in any INSERT or retention step rolls back every write,
            # including an update of the current daily report.
            with db:
                db.execute(
                    """
                    INSERT INTO market_reports(report_date, report_text, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(report_date) DO UPDATE SET
                        report_text=excluded.report_text,
                        updated_at=excluded.updated_at
                    """,
                    (report_date, report_text, timestamp, timestamp),
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO market_report_details(
                        report_id, report_date, report_text, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (report_id, report_date, report_text, timestamp),
                )
                if evidence_json is not None:
                    detail = db.execute(
                        "SELECT report_date, report_text FROM market_report_details "
                        "WHERE report_id=?", (report_id,),
                    ).fetchone()
                    if detail["report_date"] != report_date or detail["report_text"] != report_text:
                        raise ValueError("report evidence conflicts with immutable report detail")
                    db.execute(
                        """
                        INSERT OR IGNORE INTO market_report_evidence(
                            evidence_sha256, report_id, report_date, report_sha256,
                            trigger, evidence_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (sha256(evidence_json.encode("utf-8")).hexdigest(), report_id,
                         report_date, report_sha256, trigger, evidence_json, timestamp),
                    )
                # Preserve the existing report/detail retention windows. The
                # evidence FK deletes only observations of retired details.
                db.execute(
                    """
                    DELETE FROM market_reports WHERE report_date NOT IN (
                        SELECT report_date FROM market_reports
                        ORDER BY report_date DESC LIMIT 40
                    )
                    """
                )
                db.execute(
                    """
                    DELETE FROM market_report_details WHERE report_id NOT IN (
                        SELECT report_id FROM market_report_details
                        ORDER BY created_at DESC, rowid DESC LIMIT 120
                    )
                    """
                )
                db.execute(
                    """
                    DELETE FROM market_report_evidence WHERE evidence_sha256 NOT IN (
                        SELECT evidence_sha256 FROM market_report_evidence
                        ORDER BY created_at DESC, rowid DESC LIMIT 360
                    )
                    """
                )

    async def market_report_evidence(self, report_id: str) -> list[dict[str, object]]:
        """Return only report-bound market evidence, never report/chat text."""
        if not _valid_report_id(report_id):
            return []
        async with self._lock:
            rows = self._db().execute(
                "SELECT evidence_sha256, report_id, report_date, report_sha256, "
                "trigger, evidence_json, created_at FROM market_report_evidence "
                "WHERE report_id=? ORDER BY created_at DESC, rowid DESC LIMIT 360",
                (report_id,),
            ).fetchall()
            result: list[dict[str, object]] = []
            for row in rows:
                item = dict(row)
                item["evidence"] = json.loads(item.pop("evidence_json"))
                result.append(item)
            return result

    async def market_reports_between(
        self, start_date: str, end_date: str
    ) -> list[tuple[str, str]]:
        async with self._lock:
            rows = self._db().execute(
                """
                SELECT report_date, report_text FROM market_reports
                WHERE report_date BETWEEN ? AND ?
                ORDER BY report_date
                """,
                (start_date, end_date),
            ).fetchall()
            return [
                (str(row["report_date"]), str(row["report_text"]))
                for row in rows
            ]

    async def market_report(self, report_date: str) -> str | None:
        async with self._lock:
            row = self._db().execute(
                "SELECT report_text FROM market_reports WHERE report_date=?",
                (report_date,),
            ).fetchone()
            return str(row["report_text"]) if row is not None else None

    async def market_report_detail(self, report_id: str) -> str | None:
        if not _valid_report_id(report_id):
            return None
        async with self._lock:
            row = self._db().execute(
                "SELECT report_text FROM market_report_details WHERE report_id=?",
                (report_id,),
            ).fetchone()
            return str(row["report_text"]) if row is not None else None

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            result: dict[str, int] = {}
            for table in ("users", "watchlist", "conversations", "subscriptions"):
                row = self._db().execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                result[table] = int(row["count"])
            return result
