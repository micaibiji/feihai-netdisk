from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JobStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL UNIQUE,
                    auto_intake INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_resources (
                    fingerprint TEXT PRIMARY KEY,
                    subscription_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def create(
        self,
        *,
        kind: str,
        provider: str,
        title: str,
        status: str,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs(kind, provider, title, status, detail, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, provider, title, status, json.dumps(detail, ensure_ascii=False), now, now),
            )
            job_id = int(cursor.lastrowid)
        return self.get(job_id)

    def get(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._serialize(row)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def create_subscription(self, keyword: str, auto_intake: bool = True) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subscriptions(keyword, auto_intake, enabled, created_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(keyword) DO UPDATE SET auto_intake=excluded.auto_intake, enabled=1
                """,
                (keyword.strip(), int(auto_intake), now),
            )
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE keyword = ?", (keyword.strip(),)
            ).fetchone()
        return self._serialize_subscription(row)

    def list_subscriptions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM subscriptions ORDER BY id DESC"
            ).fetchall()
        return [self._serialize_subscription(row) for row in rows]

    def mark_checked(self, subscription_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE subscriptions SET last_checked_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), subscription_id),
            )

    def mark_seen(self, subscription_id: int, fingerprint: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO seen_resources(fingerprint, subscription_id, created_at)
                VALUES (?, ?, ?)
                """,
                (fingerprint, subscription_id, datetime.now(UTC).isoformat()),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        return item

    @staticmethod
    def _serialize_subscription(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["auto_intake"] = bool(item["auto_intake"])
        item["enabled"] = bool(item["enabled"])
        return item
