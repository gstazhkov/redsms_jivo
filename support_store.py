from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class SupportStore:
    """Durable inbox, outbound queue, and operator handoff log."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_events (
                    event_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL,
                    locked_until REAL,
                    received_at REAL NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS outbound_jobs (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL,
                    locked_until REAL,
                    created_at REAL NOT NULL,
                    last_error TEXT,
                    is_handoff INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS operator_handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    sent_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_inbound_due
                    ON inbound_events(status, next_attempt, locked_until);
                CREATE INDEX IF NOT EXISTS ix_outbound_due
                    ON outbound_jobs(status, next_attempt, locked_until);
                CREATE INDEX IF NOT EXISTS ix_handoffs_created
                    ON operator_handoffs(created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def enqueue_event(self, event_id: str, payload: dict[str, Any], dedupe_ttl: int) -> bool:
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT received_at FROM inbound_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row and row["received_at"] > now - dedupe_ttl:
                return False
            connection.execute(
                """
                INSERT INTO inbound_events
                    (event_id, payload, status, attempts, next_attempt, locked_until, received_at, last_error)
                VALUES (?, ?, 'pending', 0, ?, NULL, ?, NULL)
                ON CONFLICT(event_id) DO UPDATE SET
                    payload = excluded.payload,
                    status = 'pending',
                    attempts = 0,
                    next_attempt = excluded.next_attempt,
                    locked_until = NULL,
                    received_at = excluded.received_at,
                    last_error = NULL
                """,
                (event_id, encoded, now, now),
            )
        return True

    def claim_event(self, event_id: str | None = None) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            where_id = " AND event_id = ?" if event_id is not None else ""
            params: tuple[Any, ...] = (now, now, event_id) if event_id is not None else (now, now)
            row = connection.execute(
                """
                SELECT event_id, payload FROM inbound_events
                WHERE ((status = 'pending' AND next_attempt <= ?)
                    OR (status = 'processing' AND locked_until <= ?))
                """ + where_id + " ORDER BY received_at LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE inbound_events
                SET status = 'processing', attempts = attempts + 1, locked_until = ?
                WHERE event_id = ?
                """,
                (now + 120, row["event_id"]),
            )
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def finish_event(self, event_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE inbound_events SET status = 'done', locked_until = NULL, last_error = NULL WHERE event_id = ?",
                (event_id,),
            )

    def retry_event(self, event_id: str, error: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM inbound_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            attempts = row["attempts"] if row else 1
            delay = min(2 ** min(attempts, 8), 300)
            connection.execute(
                """
                UPDATE inbound_events
                SET status = 'pending', next_attempt = ?, locked_until = NULL, last_error = ?
                WHERE event_id = ?
                """,
                (time.time() + delay, error[:1000], event_id),
            )

    def enqueue_outbound(self, job_id: str, payload: dict[str, Any], *, is_handoff: bool = False) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO outbound_jobs
                    (job_id, payload, status, attempts, next_attempt, locked_until, created_at, last_error, is_handoff)
                VALUES (?, ?, 'pending', 0, ?, NULL, ?, NULL, ?)
                """,
                (job_id, json.dumps(payload, ensure_ascii=False), now, now, int(is_handoff)),
            )
            if is_handoff:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO operator_handoffs
                        (handoff_id, client_id, chat_id, status, created_at)
                    VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (job_id, str(payload["client_id"]), str(payload["chat_id"]), now),
                )

    def claim_outbound(self, job_id: str | None = None) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            where_id = " AND job_id = ?" if job_id is not None else ""
            params: tuple[Any, ...] = (now, now, job_id) if job_id is not None else (now, now)
            row = connection.execute(
                """
                SELECT job_id, payload FROM outbound_jobs
                WHERE ((status = 'pending' AND next_attempt <= ?)
                    OR (status = 'sending' AND locked_until <= ?))
                """ + where_id + " ORDER BY created_at LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE outbound_jobs
                SET status = 'sending', attempts = attempts + 1, locked_until = ?
                WHERE job_id = ?
                """,
                (now + 60, row["job_id"]),
            )
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def finish_outbound(self, job_id: str, *, error: str | None = None) -> None:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts, is_handoff FROM outbound_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            if error is None:
                connection.execute(
                    """
                    UPDATE outbound_jobs SET status = 'sent', locked_until = NULL, last_error = NULL
                    WHERE job_id = ?
                    """,
                    (job_id,),
                )
                if row["is_handoff"]:
                    connection.execute(
                        """
                        UPDATE operator_handoffs
                        SET status = 'sent', sent_at = ?, attempts = ?, last_error = NULL
                        WHERE handoff_id = ?
                        """,
                        (now, row["attempts"], job_id),
                    )
                return

            delay = min(2 ** min(row["attempts"], 8), 300)
            connection.execute(
                """
                UPDATE outbound_jobs
                SET status = 'pending', next_attempt = ?, locked_until = NULL, last_error = ?
                WHERE job_id = ?
                """,
                (now + delay, error[:1000], job_id),
            )
            if row["is_handoff"]:
                connection.execute(
                    """
                    UPDATE operator_handoffs SET status = 'pending', attempts = ?, last_error = ?
                    WHERE handoff_id = ?
                    """,
                    (row["attempts"], error[:1000], job_id),
                )

    def recent_handoffs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT handoff_id, client_id, chat_id, status, created_at, sent_at, attempts, last_error
                FROM operator_handoffs ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]
