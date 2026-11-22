"""Domain operations for the bearing exposure ledger."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Sequence

from . import stats
from .db import transaction


class ServiceError(Exception):
    """Base class for errors that map onto HTTP responses."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class InvalidRequest(ServiceError):
    status_code = 400


@dataclass(frozen=True)
class BearingRecord:
    serial_number: str
    batch: str
    first_install_date: date
    first_failure_date: date | None
    intervals: tuple[tuple[str, date, date], ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_date(value: str) -> date:
    return date.fromisoformat(value)


def _load_bearing(conn: sqlite3.Connection, serial_number: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT serial_number, batch, first_install_date, first_failure_date
        FROM bearings
        WHERE serial_number = ?
        """,
        (serial_number,),
    ).fetchone()
    if row is None:
        raise NotFound(f"bearing {serial_number!r} not found")
    return row


def get_bearing(conn: sqlite3.Connection, serial_number: str) -> BearingRecord:
    row = _load_bearing(conn, serial_number)
    interval_rows = conn.execute(
        """
        SELECT record_id, start_date, end_date
        FROM operation_intervals
        WHERE serial_number = ?
        ORDER BY start_date, end_date, record_id
        """,
        (serial_number,),
    ).fetchall()
    return BearingRecord(
        serial_number=row["serial_number"],
        batch=row["batch"],
        first_install_date=_as_date(row["first_install_date"]),
        first_failure_date=_as_date(row["first_failure_date"])
        if row["first_failure_date"]
        else None,
        intervals=tuple(
            (interval["record_id"], _as_date(interval["start_date"]), _as_date(interval["end_date"]))
            for interval in interval_rows
        ),
    )


def _contribution(record: BearingRecord) -> stats.Contribution:
    return stats.bearing_contribution(
        record.first_install_date,
        record.first_failure_date,
        [(start, end) for _, start, end in record.intervals],
    )


def list_bearings(conn: sqlite3.Connection) -> list[BearingRecord]:
    serials = [
        row[0]
        for row in conn.execute(
            "SELECT serial_number FROM bearings ORDER BY serial_number"
        ).fetchall()
    ]
    return [get_bearing(conn, serial) for serial in serials]


def register_bearing(
    db_path: str,
    serial_number: str,
    batch: str,
    first_install_date: date,
    first_failure_date: date | None = None,
) -> tuple[BearingRecord, bool]:
    """Register a bearing, or confirm an identical existing registration.

    Batch and first installation date are immutable once registered, because
    changing them would silently reshape every historical age.  A first failure
    date may be supplied up front; on a repeat registration it is applied (or
    corrected) only when it differs.  The returned flag is True when a new
    bearing was created.
    """

    serial_number = serial_number.strip()
    batch = batch.strip()
    if not serial_number:
        raise InvalidRequest("serial_number must not be empty")
    if not batch:
        raise InvalidRequest("batch must not be empty")
    if first_failure_date is not None and first_failure_date < first_install_date:
        raise InvalidRequest("first_failure_date must not precede first_install_date")

    with transaction(db_path) as conn:
        existing = conn.execute(
            """
            SELECT serial_number, batch, first_install_date, first_failure_date
            FROM bearings
            WHERE serial_number = ?
            """,
            (serial_number,),
        ).fetchone()
        if existing is not None:
            if (
                existing["batch"] != batch
                or existing["first_install_date"] != first_install_date.isoformat()
            ):
                raise Conflict(
                    f"bearing {serial_number!r} already registered with a different "
                    "batch or first installation date"
                )
            record = get_bearing(conn, serial_number)
            if (
                first_failure_date is not None
                and first_failure_date != record.first_failure_date
            ):
                record = _apply_failure_date(conn, record, first_failure_date)
            return record, False

        now = _now()
        conn.execute(
            """
            INSERT INTO bearings
                (serial_number, batch, first_install_date, first_failure_date,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                serial_number,
                batch,
                first_install_date.isoformat(),
                first_failure_date.isoformat() if first_failure_date else None,
                now,
                now,
            ),
        )
        record = get_bearing(conn, serial_number)
        if record.first_failure_date is not None:
            stats.apply_contribution_delta(
                conn,
                record.batch,
                added=_contribution(record),
                removed=None,
            )
        return record, True


def _apply_failure_date(
    conn: sqlite3.Connection,
    old_record: BearingRecord,
    first_failure_date: date | None,
) -> BearingRecord:
    """Write a new first failure date and fold the difference into statistics."""

    install_date = old_record.first_install_date
    if first_failure_date is not None and first_failure_date < install_date:
        raise InvalidRequest("first_failure_date must not precede first_install_date")
    if first_failure_date == old_record.first_failure_date:
        return old_record

    conn.execute(
        """
        UPDATE bearings
        SET first_failure_date = ?, updated_at = ?
        WHERE serial_number = ?
        """,
        (
            first_failure_date.isoformat() if first_failure_date else None,
            _now(),
            old_record.serial_number,
        ),
    )

    new_record = BearingRecord(
        serial_number=old_record.serial_number,
        batch=old_record.batch,
        first_install_date=old_record.first_install_date,
        first_failure_date=first_failure_date,
        intervals=old_record.intervals,
    )
    stats.apply_contribution_delta(
        conn,
        old_record.batch,
        added=_contribution(new_record),
        removed=_contribution(old_record),
    )
    return new_record


def set_first_failure_date(
    db_path: str,
    serial_number: str,
    first_failure_date: date | None,
) -> BearingRecord:
    """Report, correct, or clear a bearing's first failure date."""

    with transaction(db_path) as conn:
        old_record = get_bearing(conn, serial_number)
        _apply_failure_date(conn, old_record, first_failure_date)
        return get_bearing(conn, serial_number)


def put_operation_interval(
    db_path: str,
    serial_number: str,
    record_id: str,
    start_date: date,
    end_date: date,
) -> tuple[BearingRecord, bool]:
    """Register or correct one operation record (upsert by record id)."""

    record_id = record_id.strip()
    if not record_id:
        raise InvalidRequest("record_id must not be empty")
    if end_date < start_date:
        raise InvalidRequest("end_date must not precede start_date")

    with transaction(db_path) as conn:
        old_record = get_bearing(conn, serial_number)

        owner = conn.execute(
            """
            SELECT 1 FROM operation_intervals
            WHERE serial_number = ? AND record_id = ?
            """,
            (serial_number, record_id),
        ).fetchone()

        is_new = owner is None
        now = _now()
        if is_new:
            conn.execute(
                """
                INSERT INTO operation_intervals
                    (serial_number, record_id, start_date, end_date,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    serial_number,
                    record_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE operation_intervals
                SET start_date = ?, end_date = ?, updated_at = ?
                WHERE serial_number = ? AND record_id = ?
                """,
                (
                    start_date.isoformat(),
                    end_date.isoformat(),
                    now,
                    serial_number,
                    record_id,
                ),
            )

        conn.execute(
            "UPDATE bearings SET updated_at = ? WHERE serial_number = ?",
            (now, serial_number),
        )

        new_intervals = list(old_record.intervals)
        if is_new:
            new_intervals.append((record_id, start_date, end_date))
        else:
            new_intervals = [
                (rid, start_date, end_date) if rid == record_id else (rid, start, end)
                for rid, start, end in old_record.intervals
            ]

        new_record = BearingRecord(
            serial_number=old_record.serial_number,
            batch=old_record.batch,
            first_install_date=old_record.first_install_date,
            first_failure_date=old_record.first_failure_date,
            intervals=tuple(sorted(new_intervals, key=lambda item: (item[1], item[2], item[0]))),
        )
        stats.apply_contribution_delta(
            conn,
            old_record.batch,
            added=_contribution(new_record),
            removed=_contribution(old_record),
        )
        return get_bearing(conn, serial_number), is_new


def delete_operation_interval(
    db_path: str,
    serial_number: str,
    record_id: str,
) -> BearingRecord:
    with transaction(db_path) as conn:
        old_record = get_bearing(conn, serial_number)
        if not any(rid == record_id for rid, _, _ in old_record.intervals):
            raise NotFound(
                f"operation record {record_id!r} not found for bearing {serial_number!r}"
            )

        conn.execute(
            """
            DELETE FROM operation_intervals
            WHERE serial_number = ? AND record_id = ?
            """,
            (serial_number, record_id),
        )
        conn.execute(
            "UPDATE bearings SET updated_at = ? WHERE serial_number = ?",
            (_now(), serial_number),
        )

        new_record = BearingRecord(
            serial_number=old_record.serial_number,
            batch=old_record.batch,
            first_install_date=old_record.first_install_date,
            first_failure_date=old_record.first_failure_date,
            intervals=tuple(item for item in old_record.intervals if item[0] != record_id),
        )
        stats.apply_contribution_delta(
            conn,
            old_record.batch,
            added=_contribution(new_record),
            removed=_contribution(old_record),
        )
        return get_bearing(conn, serial_number)


def query_batch_stats(
    conn: sqlite3.Connection,
    batch: str,
    age_start: int,
    age_end: int,
) -> tuple[int, int]:
    return stats.query_exposure(conn, batch, age_start, age_end)


def query_batch_series(
    conn: sqlite3.Connection,
    batch: str,
    age_start: int,
    age_end: int,
) -> list[tuple[int, int, int]]:
    return stats.query_exposure_series(conn, batch, age_start, age_end)


def rebuild_statistics(db_path: str) -> None:
    with transaction(db_path) as conn:
        stats.rebuild_all(conn)


def list_batches(conn: sqlite3.Connection) -> Sequence[str]:
    return [
        row[0]
        for row in conn.execute("SELECT DISTINCT batch FROM bearings ORDER BY batch").fetchall()
    ]
