from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    """Small SQLite repository. Migrations are additive so NAS upgrades keep data."""

    def __init__(self, database_path: Path):
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL, provider TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL UNIQUE, auto_intake INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    last_checked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS seen_resources (
                    fingerprint TEXT PRIMARY KEY, subscription_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscription_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL, provider TEXT NOT NULL,
                    share_url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    season INTEGER NOT NULL DEFAULT 1, episode INTEGER NOT NULL DEFAULT 0,
                    quality TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                    risk_status TEXT NOT NULL DEFAULT 'unknown', active INTEGER NOT NULL DEFAULT 0,
                    discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(subscription_id, share_url)
                );
                CREATE TABLE IF NOT EXISTS provider_accounts (
                    provider TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'disconnected',
                    account_mask TEXT NOT NULL DEFAULT '', capacity TEXT NOT NULL DEFAULT '',
                    risk_status TEXT NOT NULL DEFAULT 'unknown', auth_method TEXT NOT NULL DEFAULT '',
                    expires_at TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                    level TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_auth_sessions (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, method TEXT NOT NULL,
                    state TEXT NOT NULL, public_payload TEXT NOT NULL DEFAULT '{}',
                    secret_key TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL, share_url TEXT NOT NULL, extraction_code TEXT NOT NULL DEFAULT '',
                    raw_title TEXT NOT NULL DEFAULT '', normalized_title TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT '',
                    season INTEGER, episode INTEGER, quality TEXT NOT NULL DEFAULT '',
                    recognition_state TEXT NOT NULL DEFAULT 'pending', tmdb_id INTEGER,
                    validation_state TEXT NOT NULL DEFAULT 'pending', validation_reason TEXT NOT NULL DEFAULT '',
                    checked_at TEXT, recheck_after TEXT, discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_resources_visible
                    ON resources(validation_state, recognition_state, updated_at DESC);
                CREATE TABLE IF NOT EXISTS operation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                    target_type TEXT NOT NULL, target_id TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL, reversible INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )
            job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            job_additions = {
                "progress": "INTEGER NOT NULL DEFAULT 0",
                "stage": "TEXT NOT NULL DEFAULT ''",
                "error_code": "TEXT NOT NULL DEFAULT ''",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in job_additions.items():
                if name not in job_columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(subscriptions)")}
            additions = {
                "media_type": "TEXT NOT NULL DEFAULT 'tv'",
                "year": "INTEGER",
                "season": "INTEGER NOT NULL DEFAULT 1",
                "current_episode": "INTEGER NOT NULL DEFAULT 0",
                "selected_source_id": "INTEGER",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {definition}")
            for provider in ("115", "baidu", "quark", "china_mobile"):
                connection.execute(
                    "INSERT OR IGNORE INTO provider_accounts(provider, updated_at) VALUES (?, ?)",
                    (provider, utc_now()),
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def create(self, *, kind: str, provider: str, title: str, status: str, detail: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs(kind,provider,title,status,detail,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (kind, provider, title, status, json.dumps(detail, ensure_ascii=False), now, now),
            )
            job_id = int(cursor.lastrowid)
        return self.get(job_id)

    def get(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._serialize_job(row)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [self._serialize_job(row) for row in rows]

    def update_job(self, job_id: int, *, status: str | None = None, progress: int | None = None,
                   stage: str | None = None, detail: dict[str, Any] | None = None,
                   error_code: str | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {"updated_at": utc_now()}
        if status is not None:
            values["status"] = status
        if progress is not None:
            values["progress"] = max(0, min(100, progress))
        if stage is not None:
            values["stage"] = stage
        if detail is not None:
            values["detail"] = json.dumps(detail, ensure_ascii=False)
        if error_code is not None:
            values["error_code"] = error_code
        assignments = ",".join(f"{key}=?" for key in values)
        with self._connect() as connection:
            connection.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values.values(), job_id))
        return self.get(job_id)

    def create_subscription(self, keyword: str, auto_intake: bool = True, media_type: str = "tv", year: int | None = None) -> dict[str, Any]:
        keyword = keyword.strip()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO subscriptions(keyword,auto_intake,enabled,created_at,media_type,year)
                   VALUES (?,?,1,?,?,?)
                   ON CONFLICT(keyword) DO UPDATE SET auto_intake=excluded.auto_intake,
                   enabled=1, media_type=excluded.media_type, year=COALESCE(excluded.year,subscriptions.year)""",
                (keyword, int(auto_intake), utc_now(), media_type, year),
            )
            row = connection.execute("SELECT * FROM subscriptions WHERE keyword=?", (keyword,)).fetchone()
        return self._serialize_subscription(row)

    def list_subscriptions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM subscriptions ORDER BY id DESC").fetchall()
            output = []
            for row in rows:
                item = self._serialize_subscription(row)
                sources = connection.execute(
                    "SELECT * FROM subscription_sources WHERE subscription_id=? ORDER BY active DESC, episode DESC, updated_at DESC",
                    (row["id"],),
                ).fetchall()
                item["sources"] = [self._serialize_source(source) for source in sources]
                output.append(item)
        return output

    def add_subscription_source(self, subscription_id: int, source: dict[str, Any], priority: tuple[str, ...]) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO subscription_sources(
                     subscription_id,provider,share_url,title,season,episode,quality,source,risk_status,discovered_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(subscription_id,share_url) DO UPDATE SET
                     title=excluded.title,season=excluded.season,episode=excluded.episode,
                     quality=excluded.quality,source=excluded.source,updated_at=excluded.updated_at""",
                (subscription_id, source["provider"], source["url"], source.get("title", ""),
                 int(source.get("season", 1)), int(source.get("episode", 0)), source.get("quality", ""),
                 source.get("source", ""), source.get("risk_status", "unknown"), now, source.get("datetime") or now),
            )
            candidates = connection.execute(
                "SELECT * FROM subscription_sources WHERE subscription_id=? AND risk_status NOT IN ('blocked','invalid')",
                (subscription_id,),
            ).fetchall()
            order = {name: index for index, name in enumerate(priority)}
            best = max(
                candidates,
                key=lambda row: (row["season"], row["episode"], row["updated_at"], -order.get(row["provider"], 99)),
                default=None,
            )
            connection.execute("UPDATE subscription_sources SET active=0 WHERE subscription_id=?", (subscription_id,))
            if best is not None:
                connection.execute("UPDATE subscription_sources SET active=1 WHERE id=?", (best["id"],))
                connection.execute(
                    "UPDATE subscriptions SET selected_source_id=?,season=?,current_episode=? WHERE id=?",
                    (best["id"], best["season"], best["episode"], subscription_id),
                )
                row = connection.execute("SELECT * FROM subscription_sources WHERE id=?", (best["id"],)).fetchone()
                return self._serialize_source(row)
        raise ValueError("没有可用来源")

    def set_subscription_enabled(self, subscription_id: int, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE subscriptions SET enabled=? WHERE id=?", (int(enabled), subscription_id))

    def mark_checked(self, subscription_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE subscriptions SET last_checked_at=? WHERE id=?", (utc_now(), subscription_id))

    def mark_seen(self, subscription_id: int, fingerprint: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO seen_resources(fingerprint,subscription_id,created_at) VALUES (?,?,?)",
                (fingerprint, subscription_id, utc_now()),
            )
        return cursor.rowcount == 1

    def provider_accounts(self) -> list[dict[str, Any]]:
        order = "CASE provider WHEN '115' THEN 1 WHEN 'baidu' THEN 2 WHEN 'quark' THEN 3 ELSE 4 END"
        with self._connect() as connection:
            rows = connection.execute(f"SELECT * FROM provider_accounts ORDER BY {order}").fetchall()
        return [dict(row) for row in rows]

    def update_provider(self, provider: str, **values: Any) -> dict[str, Any]:
        allowed = {"state", "account_mask", "capacity", "risk_status", "auth_method", "expires_at"}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in clean)
        with self._connect() as connection:
            connection.execute(f"UPDATE provider_accounts SET {assignments} WHERE provider=?", (*clean.values(), provider))
            row = connection.execute("SELECT * FROM provider_accounts WHERE provider=?", (provider,)).fetchone()
        return dict(row)

    def add_risk_event(self, provider: str, level: str, event_type: str, message: str, action: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO risk_events(provider,level,event_type,message,action,created_at) VALUES (?,?,?,?,?,?)",
                (provider, level, event_type, message, action, utc_now()),
            )
            row = connection.execute("SELECT * FROM risk_events WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def list_risk_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def save_settings(self, values: dict[str, Any]) -> None:
        with self._connect() as connection:
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), utc_now()),
                )

    def load_settings(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key,value FROM app_settings").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def create_auth_session(self, *, session_id: str, provider: str, method: str, state: str,
                            public_payload: dict[str, Any], secret_key: str = "",
                            expires_at: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO provider_auth_sessions(
                   id,provider,method,state,public_payload,secret_key,expires_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (session_id, provider, method, state, json.dumps(public_payload, ensure_ascii=False),
                 secret_key, expires_at, now, now),
            )
        return self.get_auth_session(session_id, include_secret=False)

    def get_auth_session(self, session_id: str, *, include_secret: bool = False) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM provider_auth_sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        item = dict(row)
        item["public_payload"] = json.loads(item["public_payload"])
        if not include_secret:
            item.pop("secret_key", None)
        return item

    def update_auth_session(self, session_id: str, *, state: str, public_payload: dict[str, Any] | None = None,
                            error: str = "") -> dict[str, Any]:
        values: dict[str, Any] = {"state": state, "error": error[:500], "updated_at": utc_now()}
        if public_payload is not None:
            values["public_payload"] = json.dumps(public_payload, ensure_ascii=False)
        assignments = ",".join(f"{key}=?" for key in values)
        with self._connect() as connection:
            connection.execute(f"UPDATE provider_auth_sessions SET {assignments} WHERE id=?", (*values.values(), session_id))
        return self.get_auth_session(session_id, include_secret=False)

    def upsert_resource(self, resource: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO resources(
                   fingerprint,provider,share_url,extraction_code,raw_title,normalized_title,source,
                   media_type,season,episode,quality,recognition_state,tmdb_id,validation_state,
                   validation_reason,checked_at,recheck_after,discovered_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                   raw_title=excluded.raw_title,source=excluded.source,season=excluded.season,
                   episode=excluded.episode,quality=excluded.quality,updated_at=excluded.updated_at""",
                (resource["fingerprint"], resource["provider"], resource["url"], resource.get("extraction_code", ""),
                 resource.get("title", ""), resource.get("normalized_title", ""), resource.get("source", ""),
                 resource.get("media_type", ""), resource.get("season"), resource.get("episode"),
                 resource.get("quality", ""), resource.get("recognition_state", "pending"), resource.get("tmdb_id"),
                 resource.get("validation_state", "pending"), resource.get("validation_reason", ""),
                 resource.get("checked_at"), resource.get("recheck_after"), resource.get("datetime") or now, now),
            )
            row = connection.execute("SELECT * FROM resources WHERE fingerprint=?", (resource["fingerprint"],)).fetchone()
        return dict(row)

    def update_resource_validation(self, fingerprint: str, *, state: str, reason: str,
                                   checked_at: str, recheck_after: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """UPDATE resources SET validation_state=?,validation_reason=?,checked_at=?,
                   recheck_after=?,updated_at=? WHERE fingerprint=?""",
                (state, reason[:500], checked_at, recheck_after, utc_now(), fingerprint),
            )
            row = connection.execute("SELECT * FROM resources WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row is None:
            raise KeyError(fingerprint)
        return dict(row)

    def list_resources(self, *, visible_only: bool = True, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if visible_only:
            where.extend(("validation_state='valid'", "recognition_state='recognized'"))
        if query:
            where.append("(normalized_title LIKE ? OR raw_title LIKE ?)")
            term = f"%{query}%"
            params.extend((term, term))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM resources {clause} ORDER BY updated_at DESC LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def add_operation(self, *, action: str, target_type: str, target_id: str,
                      summary: str, reversible: bool = False) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO operation_history(action,target_type,target_id,summary,reversible,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (action, target_type, target_id, summary, int(reversible), utc_now()),
            )
            row = connection.execute("SELECT * FROM operation_history WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    @staticmethod
    def _serialize_job(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        return item

    @staticmethod
    def _serialize_subscription(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["auto_intake"] = bool(item["auto_intake"])
        item["enabled"] = bool(item["enabled"])
        return item

    @staticmethod
    def _serialize_source(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["active"] = bool(item["active"])
        return item
