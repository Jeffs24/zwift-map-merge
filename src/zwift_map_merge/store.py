"""SQLite state: detected pairs and what happened to them."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs (
    target_id    INTEGER PRIMARY KEY,   -- watch activity (original id)
    source_id    INTEGER NOT NULL,      -- Zwift activity
    target_name  TEXT,
    source_name  TEXT,
    start_utc    TEXT,
    overlap      REAL,
    status       TEXT NOT NULL,         -- detected | simulated | merged | failed | ignored
    message      TEXT,
    new_id       INTEGER,               -- merged activity id on Garmin
    position_ratio REAL,
    source_file  TEXT,                  -- uploaded Zwift FIT (source_id = 0)
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zwift_seen (
    zwift_id     TEXT PRIMARY KEY,
    name         TEXT,
    start_utc    TEXT,
    target_id    INTEGER,              -- watch activity it was paired with, if any
    result       TEXT,
    updated_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def upsert_detected(self, target_id: int, source_id: int, target_name: str, source_name: str,
                        start_utc: str, overlap: float, source_file: str | None = None) -> bool:
        """Record a pair; return False when the watch activity is already handled."""
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO pairs (target_id, source_id, target_name, source_name, start_utc,
                                      overlap, status, source_file, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'detected', ?, ?)
                   ON CONFLICT(target_id) DO UPDATE SET
                     source_id=excluded.source_id, overlap=excluded.overlap,
                     target_name=excluded.target_name, source_name=excluded.source_name,
                     source_file=excluded.source_file,
                     status=CASE WHEN excluded.source_file IS NOT NULL THEN 'detected' ELSE pairs.status END,
                     message=CASE WHEN excluded.source_file IS NOT NULL THEN NULL ELSE pairs.message END
                   WHERE (pairs.status IN ('detected', 'simulated')
                          AND (pairs.source_file IS NULL OR excluded.source_file IS NOT NULL))
                      OR (pairs.status = 'failed' AND excluded.source_file IS NOT NULL)""",
                (target_id, source_id, target_name, source_name, start_utc, overlap,
                 source_file, _now()),
            )
            return cur.rowcount > 0

    def set_status(self, target_id: int, status: str, message: str | None = None,
                   new_id: int | None = None, position_ratio: float | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE pairs SET status=?, message=?,
                     new_id=COALESCE(?, new_id), position_ratio=COALESCE(?, position_ratio),
                     updated_at=? WHERE target_id=?""",
                (status, message, new_id, position_ratio, _now(), target_id),
            )

    def get(self, target_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM pairs WHERE target_id=?", (target_id,)).fetchone()
        return dict(row) if row else None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM pairs ORDER BY start_utc DESC").fetchall()
        return [dict(r) for r in rows]

    def zwift_seen(self, zwift_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM zwift_seen WHERE zwift_id=?", (zwift_id,)).fetchone()
        return dict(row) if row else None

    def mark_zwift_seen(self, zwift_id: str, name: str, start_utc: str | None,
                        target_id: int | None, result: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO zwift_seen (zwift_id, name, start_utc, target_id, result, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(zwift_id) DO UPDATE SET target_id=excluded.target_id,
                     result=excluded.result, updated_at=excluded.updated_at""",
                (zwift_id, name, start_utc, target_id, result, _now()),
            )

    def handled_ids(self) -> set[int]:
        """Activity ids that must not be paired again (merged, ignored, or produced by us)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT target_id, source_id, new_id FROM pairs WHERE status IN ('merged', 'ignored')"
            ).fetchall()
        ids: set[int] = set()
        for r in rows:
            ids.update(v for v in (r["target_id"], r["source_id"], r["new_id"]) if v)
        return ids
