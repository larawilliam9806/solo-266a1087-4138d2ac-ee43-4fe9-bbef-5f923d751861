"""Interval arithmetic for bearing exposure statistics.

The unit of computation is a half-open-free, inclusive integer range of ages
(days since first installation).  A bearing contributes at most one at-risk
unit on any age day, no matter how many overlapping operation records cover
that day.  Contributions are folded into per-batch run-length encoded segments,
so neither computation nor storage expands an individual interval day by day.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

Range = tuple[int, int]
Segment = tuple[int, int, int]
Interval = tuple[date, date]


@dataclass(frozen=True)
class Contribution:
    """What one bearing adds to its batch statistics.

    ``coverage`` holds disjoint, sorted, inclusive age ranges on which the
    bearing is at risk.  ``failure_age`` is set only when the first failure day
    is itself covered by an operation record.
    """

    coverage: tuple[Range, ...]
    failure_age: int | None


def merge_ranges(ranges: Iterable[Range]) -> list[Range]:
    ordered = sorted(ranges)
    merged: list[Range] = []
    for start, end in ordered:
        if start > end:
            continue
        if merged and start <= merged[-1][1] + 1:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def bearing_contribution(
    first_install_date: date,
    first_failure_date: date | None,
    intervals: Sequence[Interval],
) -> Contribution:
    """Translate calendar intervals into age ranges for one bearing.

    Age 0 is the first installation day.  Operation records are intersected
    with ``[0, first_failure_age]`` (the failure day itself is still at risk);
    without a failure they run to their own end.  The first failure counts only
    if that age is covered by an operation record.
    """

    failure_age: int | None = None
    upper: int | None = None
    if first_failure_date is not None:
        failure_age = (first_failure_date - first_install_date).days
        upper = failure_age

    raw: list[Range] = []
    for start, end in intervals:
        start_age = (start - first_install_date).days
        end_age = (end - first_install_date).days
        if start_age < 0:
            start_age = 0
        if upper is not None and end_age > upper:
            end_age = upper
        if start_age <= end_age:
            raw.append((start_age, end_age))

    coverage = tuple(merge_ranges(raw))
    counted_failure: int | None = None
    if failure_age is not None and failure_age >= 0:
        if any(start <= failure_age <= end for start, end in coverage):
            counted_failure = failure_age
    return Contribution(coverage=coverage, failure_age=counted_failure)


def _normalise(segments: Iterable[Segment]) -> list[Segment]:
    ordered = sorted((a, b, c) for a, b, c in segments if a <= b)
    merged: list[Segment] = []
    for start, end, count in ordered:
        if count < 0:
            raise RuntimeError("exposure count would become negative")
        if count == 0:
            continue
        if merged:
            last_start, last_end, last_count = merged[-1]
            if last_count == count and start <= last_end + 1:
                merged[-1] = (last_start, max(last_end, end), count)
                continue
            if start <= last_end:
                raise RuntimeError("overlapping exposure segments with different counts")
        merged.append((start, end, count))
    return merged


def _add_range(segments: Sequence[Segment], start: int, end: int, delta: int) -> list[Segment]:
    """Add ``delta`` to every age in ``[start, end]`` without day expansion."""

    if end < start or delta == 0:
        return list(segments)

    result: list[Segment] = []
    cursor = start
    for seg_start, seg_end, count in segments:
        if seg_end < start or seg_start > end:
            result.append((seg_start, seg_end, count))
            continue

        # Part of the segment before the modified range keeps its count.
        if seg_start < start:
            result.append((seg_start, start - 1, count))

        overlap_start = max(seg_start, start)
        overlap_end = min(seg_end, end)

        # A gap inside the modified range was previously uncovered.
        if cursor < overlap_start:
            result.append((cursor, overlap_start - 1, delta))
        cursor = max(cursor, overlap_end + 1)

        new_count = count + delta
        if new_count < 0:
            raise RuntimeError("exposure count would become negative")
        if new_count > 0:
            result.append((overlap_start, overlap_end, new_count))

        # Part of the segment after the modified range keeps its count.
        if seg_end > end:
            result.append((end + 1, seg_end, count))

    if cursor <= end:
        result.append((cursor, end, delta))

    return _normalise(result)


def load_segments(conn: sqlite3.Connection, batch: str) -> list[Segment]:
    rows = conn.execute(
        """
        SELECT age_start, age_end, at_risk_count
        FROM batch_exposure_segments
        WHERE batch = ?
        ORDER BY age_start, age_end
        """,
        (batch,),
    ).fetchall()
    return [(int(row[0]), int(row[1]), int(row[2])) for row in rows]


def store_segments(conn: sqlite3.Connection, batch: str, segments: Sequence[Segment]) -> None:
    conn.execute("DELETE FROM batch_exposure_segments WHERE batch = ?", (batch,))
    conn.executemany(
        """
        INSERT INTO batch_exposure_segments
            (batch, age_start, age_end, at_risk_count)
        VALUES (?, ?, ?, ?)
        """,
        [(batch, start, end, count) for start, end, count in _normalise(segments)],
    )


def _adjust_failure_count(conn: sqlite3.Connection, batch: str, age: int, delta: int) -> None:
    if delta == 0:
        return
    if delta > 0:
        conn.execute(
            """
            INSERT INTO batch_failure_counts (batch, age, failure_count)
            VALUES (?, ?, ?)
            ON CONFLICT (batch, age) DO UPDATE
                SET failure_count = failure_count + excluded.failure_count
            """,
            (batch, age, delta),
        )
        return

    row = conn.execute(
        "SELECT failure_count FROM batch_failure_counts WHERE batch = ? AND age = ?",
        (batch, age),
    ).fetchone()
    if row is None:
        raise RuntimeError("failure count to remove does not exist")
    remaining = int(row[0]) + delta
    if remaining < 0:
        raise RuntimeError("failure count would become negative")
    if remaining == 0:
        conn.execute(
            "DELETE FROM batch_failure_counts WHERE batch = ? AND age = ?",
            (batch, age),
        )
    else:
        conn.execute(
            """
            UPDATE batch_failure_counts
            SET failure_count = ?
            WHERE batch = ? AND age = ?
            """,
            (remaining, batch, age),
        )


def apply_contribution_delta(
    conn: sqlite3.Connection,
    batch: str,
    added: Contribution | None,
    removed: Contribution | None,
) -> None:
    """Fold one bearing's contribution change into saved batch statistics."""

    if removed is not None:
        segments = load_segments(conn, batch)
        for start, end in removed.coverage:
            segments = _add_range(segments, start, end, -1)
        store_segments(conn, batch, segments)
        if removed.failure_age is not None:
            _adjust_failure_count(conn, batch, removed.failure_age, -1)

    if added is not None:
        segments = load_segments(conn, batch)
        for start, end in added.coverage:
            segments = _add_range(segments, start, end, 1)
        store_segments(conn, batch, segments)
        if added.failure_age is not None:
            _adjust_failure_count(conn, batch, added.failure_age, 1)


def query_exposure(
    conn: sqlite3.Connection,
    batch: str,
    age_start: int,
    age_end: int,
) -> tuple[int, int]:
    """Read only the saved aggregate segments and failure counts."""

    rows = conn.execute(
        """
        SELECT age_start, age_end, at_risk_count
        FROM batch_exposure_segments
        WHERE batch = ? AND age_end >= ? AND age_start <= ?
        """,
        (batch, age_start, age_end),
    ).fetchall()

    bearing_days = 0
    for row in rows:
        overlap_start = max(age_start, int(row["age_start"]))
        overlap_end = min(age_end, int(row["age_end"]))
        if overlap_start <= overlap_end:
            bearing_days += (overlap_end - overlap_start + 1) * int(row["at_risk_count"])

    failure_row = conn.execute(
        """
        SELECT COALESCE(SUM(failure_count), 0)
        FROM batch_failure_counts
        WHERE batch = ? AND age >= ? AND age <= ?
        """,
        (batch, age_start, age_end),
    ).fetchone()
    return bearing_days, int(failure_row[0])


def query_exposure_series(
    conn: sqlite3.Connection,
    batch: str,
    age_start: int,
    age_end: int,
) -> list[tuple[int, int, int]]:
    """Expand the *saved segments* into a per-age series for output only.

    This expands the requested response range, never an individual bearing's
    operation interval, and does not write anything back to storage.
    """

    rows = conn.execute(
        """
        SELECT age_start, age_end, at_risk_count
        FROM batch_exposure_segments
        WHERE batch = ? AND age_end >= ? AND age_start <= ?
        ORDER BY age_start
        """,
        (batch, age_start, age_end),
    ).fetchall()

    counts: dict[int, int] = {}
    for row in rows:
        lower = max(age_start, int(row["age_start"]))
        upper = min(age_end, int(row["age_end"]))
        for age in range(lower, upper + 1):
            counts[age] = int(row["at_risk_count"])

    failures = {
        int(row["age"]): int(row["failure_count"])
        for row in conn.execute(
            """
            SELECT age, failure_count
            FROM batch_failure_counts
            WHERE batch = ? AND age >= ? AND age <= ?
            """,
            (batch, age_start, age_end),
        ).fetchall()
    }
    return [
        (age, counts.get(age, 0), failures.get(age, 0))
        for age in range(age_start, age_end + 1)
    ]


def rebuild_all(conn: sqlite3.Connection) -> None:
    """Rebuild batch statistics from the ledger.

    Intended for maintenance and verification only; normal corrections use
    contribution deltas and read just the affected bearing's records.
    """

    conn.execute("DELETE FROM batch_exposure_segments")
    conn.execute("DELETE FROM batch_failure_counts")

    bearings = conn.execute(
        """
        SELECT serial_number, batch, first_install_date, first_failure_date
        FROM bearings
        ORDER BY serial_number
        """
    ).fetchall()

    segments_by_batch: dict[str, list[Segment]] = {}
    failures: dict[tuple[str, int], int] = {}
    for bearing in bearings:
        intervals = conn.execute(
            """
            SELECT start_date, end_date
            FROM operation_intervals
            WHERE serial_number = ?
            ORDER BY start_date, end_date
            """,
            (bearing["serial_number"],),
        ).fetchall()
        contribution = bearing_contribution(
            date.fromisoformat(bearing["first_install_date"]),
            date.fromisoformat(bearing["first_failure_date"])
            if bearing["first_failure_date"]
            else None,
            [
                (date.fromisoformat(row["start_date"]), date.fromisoformat(row["end_date"]))
                for row in intervals
            ],
        )
        batch = bearing["batch"]
        segments = segments_by_batch.setdefault(batch, [])
        for start, end in contribution.coverage:
            segments = _add_range(segments, start, end, 1)
        segments_by_batch[batch] = segments
        if contribution.failure_age is not None:
            key = (batch, contribution.failure_age)
            failures[key] = failures.get(key, 0) + 1

    for batch, segments in segments_by_batch.items():
        store_segments(conn, batch, segments)
    for (batch, age), count in failures.items():
        conn.execute(
            """
            INSERT INTO batch_failure_counts (batch, age, failure_count)
            VALUES (?, ?, ?)
            """,
            (batch, age, count),
        )
