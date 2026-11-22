"""Tests for the local, range-based segment update chain.

A correction must fold the *net* per-bearing coverage change into the batch
run-length segments without rewriting unrelated rows: unchanged coverage skips
the segments entirely, failure counts are accounted independently, and the
read/write work stays bounded by the number of changed endpoints instead of
growing with the number of unrelated segments.
"""

from __future__ import annotations

import sqlite3

from app import db as db_module
from app import stats
from tests.conftest import add_interval, exposure, register, set_failure


def _instrument(monkeypatch):
    statements: list[tuple[str, tuple]] = []

    class RecordingConnection(sqlite3.Connection):
        def execute(self, sql, params=(), *args, **kwargs):
            statements.append((sql, params))
            return super().execute(sql, params, *args, **kwargs)

    monkeypatch.setattr(db_module, "CONNECTION_FACTORY", RecordingConnection)
    return statements


def _seed_background_segments(db_path, batch, count):
    """Insert disjoint, varied-count segments far away from age zero."""

    conn = db_module.get_connection_for_path(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO batch_exposure_segments
                (batch, age_start, age_end, at_risk_count)
            VALUES (?, ?, ?, ?)
            """,
            [
                (batch, 10000 + 3 * k, 10000 + 3 * k + 1, 1 + k % 3)
                for k in range(count)
            ],
        )
    finally:
        conn.close()


def _snapshot_segments(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            """
            SELECT id, batch, age_start, age_end, at_risk_count
            FROM batch_exposure_segments
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()


def test_subtract_ranges_and_coverage_delta_are_endpoint_bounded():
    # A one-hundred-thousand-day span stays a single range; no day expansion.
    huge = [(0, 99999)]
    assert stats.subtract_ranges(huge, []) == [(0, 99999)]
    assert stats.subtract_ranges(huge, huge) == []
    assert stats.subtract_ranges(
        [(0, 100)], [(3, 5), (7, 8), (100, 120)]
    ) == [(0, 2), (6, 6), (9, 99)]

    old_coverage = [(0, 9), (20, 29)]
    new_coverage = [(0, 10), (20, 29)]
    assert stats.coverage_delta(old_coverage, new_coverage) == [(10, 10, 1)]
    assert stats.coverage_delta(new_coverage, old_coverage) == [(10, 10, -1)]
    assert stats.coverage_delta(old_coverage, old_coverage) == []

    # Overlapping edits on the same ages net out to nothing.
    assert stats.coverage_delta([(0, 50)], [(0, 50)]) == []


def test_unchanged_coverage_skips_segment_writes_but_moves_failure(
    client, db_path, monkeypatch
):
    register(client, "S1", "B1", "2024-01-01")
    # Coverage is ages 0..9; a failure on the last covered age is counted.
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    set_failure(client, "S1", "2024-01-10")  # age 9
    before = exposure(client, "B1", 0, 30)
    assert before == {
        "batch": "B1",
        "age_start": 0,
        "age_end": 30,
        "at_risk_bearing_days": 10,
        "failure_count": 1,
    }

    segment_ids_before = [row[0] for row in _snapshot_segments(db_path)]

    calls: list[tuple] = []
    original_reconcile = stats.reconcile_segments

    def recorder(conn, batch, deltas):
        calls.append((batch, list(deltas)))
        return original_reconcile(conn, batch, deltas)

    monkeypatch.setattr(stats, "reconcile_segments", recorder)

    # Failure moves one day past the covered union: coverage [0, 9] is
    # identical, but the event is no longer on a covered day.
    set_failure(client, "S1", "2024-01-11")  # age 10, uncounted
    assert calls == []
    stats_response = exposure(client, "B1", 0, 30)
    assert stats_response["at_risk_bearing_days"] == 10
    assert stats_response["failure_count"] == 0

    # The ledger was still saved normally.
    assert client.get("/bearings/S1").json()["first_failure_date"] == "2024-01-11"
    assert [row[0] for row in _snapshot_segments(db_path)] == segment_ids_before

    # Moving it back restores the failure count without segment work either.
    set_failure(client, "S1", "2024-01-10")
    assert calls == []
    stats_response = exposure(client, "B1", 0, 30)
    assert (
        stats_response["at_risk_bearing_days"],
        stats_response["failure_count"],
    ) == (10, 1)


def test_clearing_an_uncounted_failure_touches_no_statistics(client, monkeypatch):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    set_failure(client, "S1", "2024-01-20")  # age 19, beyond the covered union

    calls: list[tuple] = []
    original_reconcile = stats.reconcile_segments

    def recorder(conn, batch, deltas):
        calls.append((batch, list(deltas)))
        return original_reconcile(conn, batch, deltas)

    monkeypatch.setattr(stats, "reconcile_segments", recorder)
    set_failure(client, "S1", None)
    assert calls == []
    assert exposure(client, "B1", 0, 30)["failure_count"] == 0


def test_unrelated_segments_keep_their_rows_and_primary_keys(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")  # ages 0..9
    _seed_background_segments(db_path, "B1", 200)

    before = _snapshot_segments(db_path)
    background_before = [row for row in before if row[2] >= 10000]
    local_before = [row for row in before if row[2] < 10000]
    assert len(local_before) == 1 and local_before[0][2:] == (0, 9, 1)

    # Add one in-operation day: age 10.
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-11")

    after = _snapshot_segments(db_path)
    background_after = [row for row in after if row[2] >= 10000]
    local_after = [row for row in after if row[2] < 10000]

    # Every unrelated row is byte-for-byte identical, primary key included.
    assert background_after == background_before

    # Only the single local segment was replaced, and it absorbed the new day.
    assert len(local_after) == 1
    assert local_after[0][2:] == (0, 10, 1)
    assert local_after[0][0] != local_before[0][0]
    assert len(after) == len(before)

    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 11
    # The distant window keeps exactly the seeded totals.
    distant = exposure(client, "B1", 10000, 10600)
    assert distant["at_risk_bearing_days"] == sum(
        2 * (1 + k % 3) for k in range(200)
    )


def test_segment_work_does_not_grow_with_unrelated_segments(client, db_path, monkeypatch):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")

    counts: dict[int, int] = {}
    far_ids: dict[int, set[int]] = {}
    statements: list[tuple[str, tuple]] = []
    for n in (50, 200, 1000):
        # Rebuild the background area from scratch for each scale.
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM batch_exposure_segments WHERE age_start >= 10000")
        conn.commit()
        conn.close()
        _seed_background_segments(db_path, "B1", n)

        statements = _instrument(monkeypatch)
        add_interval(client, "S1", "R1", "2024-01-01", "2024-01-11")
        segment_statements = [
            sql.upper()
            for sql, _ in statements
            if "BATCH_EXPOSURE_SEGMENTS" in sql.upper()
        ]
        counts[n] = len(segment_statements)
        far_ids[n] = {
            row[0]
            for row in _snapshot_segments(db_path)
            if row[2] >= 10000
        }
        # Reset the local correction for the next round.
        add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")

    # Same three local lookups plus one local delete regardless of scale; the
    # batch never undergoes a full delete/rewrite.
    assert counts[50] == counts[200] == counts[1000]
    assert counts[1000] <= 5
    assert not any(
        "DELETE FROM BATCH_EXPOSURE_SEGMENTS WHERE BATCH = ?" in sql
        for sql, _ in statements
    )

    # All thousand unrelated rows still carry their original identities.
    assert len(far_ids[1000]) == 1000


def test_adjacent_segments_with_equal_counts_are_merged_locally(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-05")  # ages 0..4

    conn = db_module.get_connection_for_path(db_path)
    try:
        # A one-day gap at age 5, then an equally counted neighbour.
        conn.execute(
            """
            INSERT INTO batch_exposure_segments
                (batch, age_start, age_end, at_risk_count)
            VALUES ('B1', 6, 8, 1)
            """
        )
    finally:
        conn.close()

    # Filling age 5 must merge the local area into one segment.
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-06")  # ages 0..5

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT age_start, age_end, at_risk_count
            FROM batch_exposure_segments
            ORDER BY age_start
            """
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(0, 8, 1)]
