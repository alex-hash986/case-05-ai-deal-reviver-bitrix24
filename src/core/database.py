import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, session_id TEXT NOT NULL,
    response TEXT, cost_rub TEXT NOT NULL DEFAULT '0', cost_complete INTEGER NOT NULL DEFAULT 1,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    processing_time_sec REAL, ab_variant TEXT NOT NULL DEFAULT 'variant_a', mode TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'bitrix24', is_after_hours INTEGER NOT NULL,
    need_human INTEGER, saved_minutes REAL
);
CREATE INDEX IF NOT EXISTS scan_runs_session ON scan_runs(session_id);
CREATE TABLE IF NOT EXISTS scan_cursors (scope TEXT PRIMARY KEY, cursor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revival_items (
    revival_id TEXT PRIMARY KEY, deal_id INTEGER NOT NULL, result TEXT NOT NULL, title TEXT NOT NULL,
    assigned_by_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS revival_deal ON revival_items(deal_id);
CREATE TABLE IF NOT EXISTS touch_history (
    revival_id TEXT PRIMARY KEY, deal_id INTEGER NOT NULL, touched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS touch_deal ON touch_history(deal_id, touched_at);
CREATE TABLE IF NOT EXISTS bitrix_steps (
    operation_id TEXT PRIMARY KEY, revival_id TEXT REFERENCES revival_items(revival_id),
    method TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS live_portal (host TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS live_writes (
    operation_id TEXT PRIMARY KEY REFERENCES bitrix_steps(operation_id)
);
CREATE TABLE IF NOT EXISTS mock_receipts (
    operation_id TEXT PRIMARY KEY REFERENCES bitrix_steps(operation_id), receipt TEXT NOT NULL
);
"""


class Database:
    """Short, explicit transactions. Network I/O happens outside transactions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    def bind_live_portal(self, host: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            bound = connection.execute("SELECT host FROM live_portal").fetchone()
            if bound is not None:
                if bound["host"] != host:
                    raise ValueError("Use a separate database for each Bitrix portal")
                return
            if connection.execute("SELECT 1 FROM scan_runs UNION SELECT 1 FROM touch_history").fetchone():
                raise ValueError("Use a fresh database for live Bitrix")
            connection.execute("INSERT INTO live_portal VALUES (?)", (host,))

    def require_mock(self) -> None:
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM live_portal").fetchone():
                raise ValueError("Use a separate database for mock Bitrix")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

