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


def subtract_ranges(base: Sequence[Range], other: Sequence[Range]) -> list[Range]:
    """Set difference ``base - other`` for sorted, disjoint, inclusive ranges.

    The result stays run-length encoded: its size is bounded by the number of
    range endpoints on both sides, never by the number of days the ranges
    span.  Both inputs are expected to be already merged.
    """

    result: list[Range] = []
    j = 0
    other_size = len(other)
    for start, end in base:
        # Ranges ending before this base range can never intersect it or any
        # later (sorted) base range.
        while j < other_size and other[j][1] < start:
            j += 1
        cursor = start
        k = j
        while k < other_size and other[k][0] <= end:
            other_start, other_end = other[k]
            if other_start > cursor:
                result.append((cursor, min(end, other_start - 1)))
            cursor = max(cursor, other_end + 1)
            if cursor > end:
                break
            k += 1
        if cursor <= end:
            result.append((cursor, end))
    return merge_ranges(result)


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
    """Replace every segment of a batch.

    Used only by the full rebuild path; ordinary corrections go through
    :func:`reconcile_segments`, which rewrites just the affected rows.
    """

    conn.execute("DELETE FROM batch_exposure_segments WHERE batch = ?", (batch,))
    conn.executemany(
        """
        INSERT INTO batch_exposure_segments
            (batch, age_start, age_end, at_risk_count)
        VALUES (?, ?, ?, ?)
        """,
        [(batch, start, end, count) for start, end, count in _normalise(segments)],
    )


def _load_local_segments(
    conn: sqlite3.Connection,
    batch: str,
    window_start: int,
    window_end: int,
) -> list[tuple[int, Segment]]:
    """Load segments overlapping the window plus its immediate neighbours.

    The neighbours are needed so adjacent segments with the same resulting
    count can be merged.  Only at most three index-backed lookups are issued,
    regardless of how many unrelated segments the batch has elsewhere.
    """

    rows: list[sqlite3.Row] = []
    left = conn.execute(
        """
        SELECT id, age_start, age_end, at_risk_count
        FROM batch_exposure_segments
        WHERE batch = ? AND age_end < ?
        ORDER BY age_end DESC
        LIMIT 1
        """,
        (batch, window_start),
    ).fetchall()
    middle = conn.execute(
        """
        SELECT id, age_start, age_end, at_risk_count
        FROM batch_exposure_segments
        WHERE batch = ? AND age_end >= ? AND age_start <= ?
        ORDER BY age_start, age_end
        """,
        (batch, window_start, window_end),
    ).fetchall()
    right = conn.execute(
        """
        SELECT id, age_start, age_end, at_risk_count
        FROM batch_exposure_segments
        WHERE batch = ? AND age_start > ?
        ORDER BY age_start ASC
        LIMIT 1
        """,
        (batch, window_end),
    ).fetchall()
    for row in left + middle + right:
        rows.append(
            (
                int(row["id"]),
                (
                    int(row["age_start"]),
                    int(row["age_end"]),
                    int(row["at_risk_count"]),
                ),
            )
        )
    # The window queries can return the same boundary row more than once.
    deduped: dict[int, Segment] = {}
    for row_id, segment in rows:
        deduped[row_id] = segment
    return sorted(deduped.items(), key=lambda item: item[1][0])


def reconcile_segments(
    conn: sqlite3.Connection,
    batch: str,
    deltas: Sequence[tuple[int, int, int]],
) -> None:
    """Apply signed range deltas to saved segments without a batch-wide rewrite.

    ``deltas`` are inclusive age ranges with a ``+1``/``-1`` sign.  Only the
    segments intersecting those ranges (and their immediate neighbours) are
    read and rewritten; every unrelated row keeps its primary key and is not
    touched at all.  Adjacent segments ending up with the same count are
    merged.  The work is bounded by the number of range endpoints and local
    segments, never by the number of days involved.
    """

    changes = [(start, end, delta) for start, end, delta in deltas if start <= end and delta]
    if not changes:
        return

    window_start = min(start for start, _, _ in changes)
    window_end = max(end for _, end, _ in changes)

    touched = _load_local_segments(conn, batch, window_start, window_end)
    local_segments = [segment for _, segment in touched]
    for start, end, delta in changes:
        local_segments = _add_range(local_segments, start, end, delta)
    local_segments = _normalise(local_segments)

    # Rows whose (start, end, count) triple survives are left byte-for-byte
    # untouched; everything else in the local area is replaced.
    remaining = dict(touched)
    to_delete: list[int] = []
    to_insert: list[Segment] = []
    for segment in local_segments:
        match = next(
            (row_id for row_id, existing in remaining.items() if existing == segment),
            None,
        )
        if match is None:
            to_insert.append(segment)
        else:
            del remaining[match]
    to_delete = list(remaining.keys())

    for offset in range(0, len(to_delete), 500):
        chunk = to_delete[offset : offset + 500]
        conn.execute(
            f"DELETE FROM batch_exposure_segments WHERE id IN ({','.join('?' for _ in chunk)})",
            chunk,
        )
    conn.executemany(
        """
        INSERT INTO batch_exposure_segments
            (batch, age_start, age_end, at_risk_count)
        VALUES (?, ?, ?, ?)
        """,
        [(batch, start, end, count) for start, end, count in to_insert],
    )


def coverage_delta(
    old_coverage: Sequence[Range],
    new_coverage: Sequence[Range],
) -> list[tuple[int, int, int]]:
    """Net per-age change between two merged coverage sets, as signed ranges.

    Ages present only in ``new_coverage`` are returned with ``+1`` and ages
    present only in ``old_coverage`` with ``-1``.  Ages covered by both carry
    no delta, so an unchanged coverage yields an empty list without ever
    iterating over individual days.
    """

    added = subtract_ranges(new_coverage, old_coverage)
    removed = subtract_ranges(old_coverage, new_coverage)
    return [(start, end, 1) for start, end in added] + [
        (start, end, -1) for start, end in removed
    ]


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
    """Fold one bearing's contribution change into saved batch statistics.

    The exposure segments are patched from the *net* coverage change: ages
    whose at-risk status did not change generate no segment work at all, and
    only the segments touched by the changed ages (plus mergeable neighbours)
    are rewritten.  The failure count is accounted for independently, so a
    failure-date correction that leaves coverage untouched still moves the
    failure event without touching the exposure segments.
    """

    old_coverage = removed.coverage if removed is not None else ()
    new_coverage = added.coverage if added is not None else ()
    deltas = coverage_delta(old_coverage, new_coverage)
    if deltas:
        reconcile_segments(conn, batch, deltas)

    old_failure_age = removed.failure_age if removed is not None else None
    new_failure_age = added.failure_age if added is not None else None
    if old_failure_age != new_failure_age:
        if old_failure_age is not None:
            _adjust_failure_count(conn, batch, old_failure_age, -1)
        if new_failure_age is not None:
            _adjust_failure_count(conn, batch, new_failure_age, 1)


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
