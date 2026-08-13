from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """Versioned SQLite store. The fh_ prefix isolates the rewritten application."""

    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS fh_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fh_accounts (
                    provider TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'disconnected',
                    account_label TEXT NOT NULL DEFAULT '', credential_kind TEXT NOT NULL DEFAULT '',
                    risk_status TEXT NOT NULL DEFAULT 'unknown', last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fh_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    provider TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fh_temp_media (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, title TEXT NOT NULL,
                    share_url TEXT NOT NULL, extraction_code TEXT NOT NULL DEFAULT '',
                    cloud_file_id TEXT NOT NULL, cloud_parent_id TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL, mime_type TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
                    direct_hint TEXT NOT NULL DEFAULT '{}', last_played_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fh_last_directories (
                    provider TEXT PRIMARY KEY, folder_id TEXT NOT NULL DEFAULT '',
                    folder_path TEXT NOT NULL DEFAULT '/', updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fh_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL DEFAULT 'tv', year INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fh_operation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            for provider in ("baidu", "quark", "115", "china_mobile"):
                db.execute(
                    "INSERT OR IGNORE INTO fh_accounts(provider,updated_at) VALUES (?,?)",
                    (provider, utc_now()),
                )

    def settings(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute("SELECT key,value FROM fh_settings").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def save_settings(self, values: dict[str, Any]) -> None:
        with self.connect() as db:
            for key, value in values.items():
                db.execute(
                    "INSERT INTO fh_settings(key,value,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), utc_now()),
                )

    def accounts(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fh_accounts").fetchall()
        return [dict(row) for row in rows]

    def update_account(self, provider: str, **values: Any) -> dict[str, Any]:
        allowed = {"state", "account_label", "credential_kind", "risk_status", "last_error"}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["updated_at"] = utc_now()
        with self.connect() as db:
            db.execute(
                f"UPDATE fh_accounts SET {','.join(f'{key}=?' for key in clean)} WHERE provider=?",
                (*clean.values(), provider),
            )
            row = db.execute("SELECT * FROM fh_accounts WHERE provider=?", (provider,)).fetchone()
        return dict(row)

    def create_job(self, kind: str, provider: str, title: str, detail: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO fh_jobs(kind,provider,title,status,detail,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (kind, provider, title, "queued", json.dumps(detail, ensure_ascii=False), now, now),
            )
            job_id = int(cursor.lastrowid)
        return self.job(job_id)

    def job(self, job_id: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM fh_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        return item

    def update_job(self, job_id: int, **values: Any) -> dict[str, Any]:
        allowed = {"status", "progress", "stage", "detail", "error", "retry_count"}
        clean = {key: value for key, value in values.items() if key in allowed}
        if "detail" in clean:
            clean["detail"] = json.dumps(clean["detail"], ensure_ascii=False)
        clean["updated_at"] = utc_now()
        with self.connect() as db:
            db.execute(
                f"UPDATE fh_jobs SET {','.join(f'{key}=?' for key in clean)} WHERE id=?",
                (*clean.values(), job_id),
            )
        return self.job(job_id)

    def jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fh_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            output.append(item)
        return output

    def save_last_directory(self, provider: str, folder_id: str, folder_path: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO fh_last_directories(provider,folder_id,folder_path,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET folder_id=excluded.folder_id,folder_path=excluded.folder_path,updated_at=excluded.updated_at",
                (provider, folder_id, folder_path, utc_now()),
            )

    def last_directories(self) -> dict[str, dict[str, str]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fh_last_directories").fetchall()
        return {row["provider"]: dict(row) for row in rows}

    def add_temp(self, item: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO fh_temp_media(
                id,provider,title,share_url,extraction_code,cloud_file_id,cloud_parent_id,
                file_name,mime_type,size,direct_hint,last_played_at,expires_at,state,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["id"], item["provider"], item["title"], item["share_url"], item.get("extraction_code", ""),
                 item["cloud_file_id"], item.get("cloud_parent_id", ""), item["file_name"], item.get("mime_type", ""),
                 int(item.get("size", 0)), json.dumps(item.get("direct_hint", {}), ensure_ascii=False),
                 item["last_played_at"], item["expires_at"], item.get("state", "ready"), item["created_at"]),
            )
        return self.temp(item["id"])

    def temp(self, temp_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM fh_temp_media WHERE id=?", (temp_id,)).fetchone()
        if not row:
            raise KeyError(temp_id)
        item = dict(row)
        item["direct_hint"] = json.loads(item["direct_hint"])
        return item

    def touch_temp(self, temp_id: str, last_played_at: str, expires_at: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE fh_temp_media SET last_played_at=?,expires_at=? WHERE id=?", (last_played_at, expires_at, temp_id))

    def expired_temps(self, now: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fh_temp_media WHERE state='ready' AND expires_at<=?", (now,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["direct_hint"] = json.loads(item["direct_hint"])
            output.append(item)
        return output

    def temps(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM fh_temp_media ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["direct_hint"] = json.loads(item["direct_hint"])
            output.append(item)
        return output

    def find_ready_temp(self, provider: str, share_url: str, file_name: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM fh_temp_media WHERE provider=? AND share_url=? AND file_name=? "
                "AND state='ready' ORDER BY created_at DESC LIMIT 1",
                (provider, share_url, file_name),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["direct_hint"] = json.loads(item["direct_hint"])
        return item

    def find_temp(self, provider: str, share_url: str, file_name: str, states: tuple[str, ...] = ("preparing",)) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in states)
        with self.connect() as db:
            row = db.execute(
                f"SELECT * FROM fh_temp_media WHERE provider=? AND share_url=? AND file_name=? "
                f"AND state IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
                (provider, share_url, file_name, *states),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["direct_hint"] = json.loads(item["direct_hint"])
        return item

    def update_temp(self, temp_id: str, *, state: str | None = None, direct_hint: dict[str, Any] | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if state is not None:
            values["state"] = state
        if direct_hint is not None:
            values["direct_hint"] = json.dumps(direct_hint, ensure_ascii=False)
        if values:
            with self.connect() as db:
                db.execute(
                    f"UPDATE fh_temp_media SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                    (*values.values(), temp_id),
                )
        return self.temp(temp_id)

    def set_temp_state(self, temp_id: str, state: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE fh_temp_media SET state=? WHERE id=?", (state, temp_id))

    def add_subscription(self, title: str, media_type: str, year: int | None) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO fh_subscriptions(title,media_type,year,created_at,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(title) DO UPDATE SET media_type=excluded.media_type,year=excluded.year,enabled=1,updated_at=excluded.updated_at",
                (title, media_type, year, now, now),
            )
            row = db.execute("SELECT * FROM fh_subscriptions WHERE title=?", (title,)).fetchone()
        return dict(row)

    def subscriptions(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fh_subscriptions ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def remove_subscription(self, subscription_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM fh_subscriptions WHERE id=?", (subscription_id,))

    def add_history(self, action: str, provider: str, summary: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO fh_operation_history(action,provider,summary,created_at) VALUES (?,?,?,?)",
                (action, provider, summary, utc_now()),
            )

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM fh_operation_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def export_portable(self) -> dict[str, Any]:
        with self.connect() as db:
            settings = [dict(row) for row in db.execute("SELECT * FROM fh_settings").fetchall()]
            accounts = [dict(row) for row in db.execute("SELECT * FROM fh_accounts").fetchall()]
            subscriptions = [dict(row) for row in db.execute("SELECT * FROM fh_subscriptions").fetchall()]
            directories = [dict(row) for row in db.execute("SELECT * FROM fh_last_directories").fetchall()]
        return {"settings": settings, "accounts": accounts, "subscriptions": subscriptions, "directories": directories}

    def restore_portable(self, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            for row in payload.get("settings", []):
                if isinstance(row, dict) and row.get("key") and "value" in row:
                    db.execute(
                        "INSERT INTO fh_settings(key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (str(row["key"]), str(row["value"]), utc_now()),
                    )
            for row in payload.get("accounts", []):
                if isinstance(row, dict) and row.get("provider") in {"baidu", "quark", "115", "china_mobile"}:
                    db.execute(
                        "UPDATE fh_accounts SET state=?,account_label=?,credential_kind=?,risk_status=?,last_error=?,updated_at=? WHERE provider=?",
                        (str(row.get("state") or "disconnected"), str(row.get("account_label") or ""),
                         str(row.get("credential_kind") or ""), str(row.get("risk_status") or "unknown"),
                         str(row.get("last_error") or ""), utc_now(), str(row["provider"])),
                    )
            for row in payload.get("subscriptions", []):
                if isinstance(row, dict) and row.get("title"):
                    now = utc_now()
                    db.execute(
                        "INSERT INTO fh_subscriptions(title,media_type,year,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(title) DO UPDATE SET media_type=excluded.media_type,year=excluded.year,enabled=1,updated_at=excluded.updated_at",
                        (str(row["title"]), str(row.get("media_type") or "tv"), row.get("year"), now, now),
                    )
            for row in payload.get("directories", []):
                if isinstance(row, dict) and row.get("provider"):
                    db.execute(
                        "INSERT INTO fh_last_directories(provider,folder_id,folder_path,updated_at) VALUES (?,?,?,?) ON CONFLICT(provider) DO UPDATE SET folder_id=excluded.folder_id,folder_path=excluded.folder_path,updated_at=excluded.updated_at",
                        (str(row["provider"]), str(row.get("folder_id") or ""), str(row.get("folder_path") or "/"), utc_now()),
                    )
