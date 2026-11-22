"""SQLite access helpers and schema for the bearing exposure service."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = "data/bearings.db"

# Optional instrumentation hook used by tests; left as ``None`` in production.
CONNECTION_FACTORY = None

_init_lock = threading.Lock()
_initialised: dict[str, tuple[int, int] | None] = {}


def get_db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def _file_signature(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino)


SCHEMA = """
CREATE TABLE IF NOT EXISTS bearings (
    serial_number       TEXT PRIMARY KEY,
    batch               TEXT NOT NULL,
    first_install_date  TEXT NOT NULL,
    first_failure_date  TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bearings_batch
    ON bearings (batch);

CREATE TABLE IF NOT EXISTS operation_intervals (
    serial_number   TEXT NOT NULL
                    REFERENCES bearings (serial_number) ON DELETE CASCADE,
    record_id       TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (serial_number, record_id),
    CHECK (start_date <= end_date)
);

CREATE INDEX IF NOT EXISTS idx_intervals_serial
    ON operation_intervals (serial_number);

-- Per-batch, run-length encoded at-risk counts.  A segment [age_start,
-- age_end] means that exactly at_risk_count bearings of the batch were in
-- operation on every age day in that closed range.  No row exists for a day
-- with zero at-risk bearings, and no individual interval is expanded per day.
CREATE TABLE IF NOT EXISTS batch_exposure_segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch           TEXT NOT NULL,
    age_start       INTEGER NOT NULL,
    age_end         INTEGER NOT NULL,
    at_risk_count   INTEGER NOT NULL,
    CHECK (age_start >= 0),
    CHECK (age_start <= age_end),
    CHECK (at_risk_count > 0)
);

CREATE INDEX IF NOT EXISTS idx_segments_batch_age
    ON batch_exposure_segments (batch, age_start, age_end);

-- First-failure events keyed by batch and failure age (day 0 = first
-- installation).  An event is only present when the failure day itself was
-- covered by an in-operation record.
CREATE TABLE IF NOT EXISTS batch_failure_counts (
    batch           TEXT NOT NULL,
    age             INTEGER NOT NULL,
    failure_count   INTEGER NOT NULL,
    PRIMARY KEY (batch, age),
    CHECK (age >= 0),
    CHECK (failure_count > 0)
);
"""


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    factory = CONNECTION_FACTORY or sqlite3.Connection
    conn = sqlite3.connect(
        db_path, timeout=30.0, isolation_level=None, factory=factory
    )
    _configure(conn)
    return conn


def init_db(db_path: str) -> None:
    """Create the schema once per database file.

    The check is keyed on the file identity so that a test or deployment that
    re-points ``DB_PATH`` to a fresh file still gets its schema.
    """

    with _init_lock:
        signature = _file_signature(db_path)
        if signature is not None and _initialised.get(db_path) == signature:
            return
        conn = connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        _initialised[db_path] = _file_signature(db_path)


@contextmanager
def transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """Run one atomic ledger + statistics update.

    ``BEGIN IMMEDIATE`` serialises writers so that the delta computed from the
    bearing's previous state cannot race with another writer.  Any exception
    rolls back both the ledger rows and the batch statistics.
    """

    init_db(db_path)
    conn = connect(db_path)
    began = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        began = True
        yield conn
        conn.execute("COMMIT")
        began = False
    except BaseException:
        if began:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                # The driver may already have rolled back a failed COMMIT.
                pass
        raise
    finally:
        conn.close()


@contextmanager
def reader(db_path: str) -> Iterator[sqlite3.Connection]:
    init_db(db_path)
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_connection_for_path(db_path: str) -> sqlite3.Connection:
    """Compatibility helper used by tests to inspect the raw SQLite file."""

    return connect(db_path)
