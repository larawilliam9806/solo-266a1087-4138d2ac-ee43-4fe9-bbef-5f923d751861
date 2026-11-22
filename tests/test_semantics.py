from __future__ import annotations

import sqlite3

import pytest

from app import service, stats
from app import db as db_module
from tests.conftest import add_interval, exposure, register, set_failure


def _instrument(monkeypatch):
    statements: list[tuple[str, tuple]] = []

    class RecordingConnection(sqlite3.Connection):
        def execute(self, sql, params=(), *args, **kwargs):
            statements.append((sql, params))
            return super().execute(sql, params, *args, **kwargs)

    monkeypatch.setattr(db_module, "CONNECTION_FACTORY", RecordingConnection)
    return statements


def test_storage_is_run_length_not_per_day(client, db_path):
    for index in range(50):
        serial = f"S{index}"
        register(client, serial, "B1", "2024-01-01")
        add_interval(client, serial, f"R{index}", "2024-01-01", "2025-12-31")

    assert exposure(client, "B1", 0, 10000)["at_risk_bearing_days"] == 50 * 731

    conn = sqlite3.connect(db_path)
    segment_rows = conn.execute(
        "SELECT COUNT(*) FROM batch_exposure_segments"
    ).fetchone()[0]
    interval_rows = conn.execute(
        "SELECT COUNT(*) FROM operation_intervals"
    ).fetchone()[0]
    conn.close()
    # The whole ten-year span collapses into a single constant-count segment.
    assert segment_rows == 1
    assert interval_rows == 50


def test_query_does_not_read_individual_intervals(client, db_path, monkeypatch):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")

    statements = _instrument(monkeypatch)

    stats_response = exposure(client, "B1", 0, 30)
    assert stats_response["at_risk_bearing_days"] == 10
    assert not any("operation_intervals" in sql.lower() for sql, _ in statements)
    assert not any("bearings" in sql.lower() for sql, _ in statements)


def test_correction_reads_only_that_bearings_records(client, db_path, monkeypatch):
    register(client, "S1", "B1", "2024-01-01")
    register(client, "S2", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    add_interval(client, "S2", "R2", "2024-01-01", "2024-01-10")

    statements = _instrument(monkeypatch)
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-20")

    interval_reads = [
        (sql, params)
        for sql, params in statements
        if "FROM operation_intervals" in sql and params
    ]
    assert interval_reads
    assert all(params and params[0] == "S1" for _, params in interval_reads)
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 30


def test_ledger_and_stats_roll_back_together(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    before = exposure(client, "B1", 0, 30)
    assert before["at_risk_bearing_days"] == 10

    original = stats.reconcile_segments

    def explode(conn, batch, deltas):
        raise RuntimeError("simulated failure while saving statistics")

    stats.reconcile_segments = explode
    try:
        with pytest.raises(RuntimeError):
            service.put_operation_interval(
                db_path,
                "S1",
                "R1",
                __import__("datetime").date(2024, 1, 1),
                __import__("datetime").date(2024, 1, 31),
            )
    finally:
        stats.reconcile_segments = original

    assert exposure(client, "B1", 0, 30) == before
    bearing = client.get("/bearings/S1").json()
    assert bearing["intervals"] == [
        {"record_id": "R1", "start_date": "2024-01-01", "end_date": "2024-01-10"}
    ]


def test_failed_registration_leaves_no_partial_rows(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    response = client.post(
        "/bearings",
        json={
            "serial_number": "S1",
            "batch": "B2",
            "first_install_date": "2024-02-01",
        },
    )
    assert response.status_code == 409
    assert client.get("/bearings/S1").json()["batch"] == "B1"

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM bearings").fetchone()[0] == 1
    conn.close()


def test_rebuild_matches_incremental_statistics(client, db_path):
    for serial, install, start, end, failure in [
        ("S1", "2024-01-01", "2024-01-01", "2024-02-01", "2024-01-20"),
        ("S2", "2024-01-01", "2024-01-05", "2024-01-25", None),
        ("S3", "2024-01-10", "2024-01-08", "2024-01-30", "2024-01-15"),
    ]:
        register(client, serial, "B1", install)
        add_interval(client, serial, f"R{serial}", start, end)
        if failure:
            set_failure(client, serial, failure)

    incremental = exposure(client, "B1", 0, 400)
    service.rebuild_statistics(db_path)
    rebuilt = exposure(client, "B1", 0, 400)
    assert incremental == rebuilt


def test_zero_bearing_days_are_not_stored(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    client.delete("/bearings/S1/intervals/R1")

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM batch_exposure_segments").fetchone()[0] == 0
    conn.close()
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 0


def test_adjacent_identical_segments_are_merged(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    add_interval(client, "S1", "R2", "2024-01-11", "2024-01-20")

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT age_start, age_end, at_risk_count FROM batch_exposure_segments"
    ).fetchall()
    conn.close()
    assert rows == [(0, 19, 1)]


def test_long_span_stays_one_segment(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2124-01-01")

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT age_start, age_end, at_risk_count FROM batch_exposure_segments"
    ).fetchall()
    conn.close()
    assert rows == [(0, 36524, 1)]
