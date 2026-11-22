from __future__ import annotations

import sqlite3
import threading

import pytest

from app import db as db_module
from app import service
from tests.conftest import add_interval, exposure, register, set_failure


def test_concurrent_interval_writes_stay_consistent(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            add_interval(
                client, "S1", f"R{index}", "2024-01-01", f"2024-01-{index:02d}"
            )
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(1, 21)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    # Every record starts on day 0; the single bearing is still counted once.
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 20

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT age_start, age_end, at_risk_count
        FROM batch_exposure_segments ORDER BY age_start
        """
    ).fetchall()
    conn.close()
    # No overlapping segments with differing counts survive.
    previous_end = -1
    for start, end, _ in rows:
        assert start > previous_end
        previous_end = end
    assert all(1 <= count <= 20 for _, _, count in rows)


def test_concurrent_failure_updates_are_serialised(client, db_path):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-12-31")

    errors: list[BaseException] = []

    def update(failure_date: str) -> None:
        try:
            set_failure(client, "S1", failure_date)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=update, args=(f"2024-02-{day:02d}",))
        for day in range(1, 11)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    stats = exposure(client, "B1", 0, 365)
    assert stats["failure_count"] == 1

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT age, failure_count FROM batch_failure_counts"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == 1


def test_commit_failure_rolls_back_ledger_and_stats(client, db_path, monkeypatch):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    before = exposure(client, "B1", 0, 30)

    original_connect = db_module.connect

    class CommitFails(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("COMMIT"):
                raise sqlite3.OperationalError("simulated disk failure")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(path):
        conn = sqlite3.connect(
            path, timeout=30.0, isolation_level=None, factory=CommitFails
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    monkeypatch.setattr(db_module, "connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError):
        service.put_operation_interval(
            db_path,
            "S1",
            "R1",
            __import__("datetime").date(2024, 1, 1),
            __import__("datetime").date(2024, 3, 1),
        )
    monkeypatch.setattr(db_module, "connect", original_connect)

    assert exposure(client, "B1", 0, 30) == before
    assert client.get("/bearings/S1").json()["intervals"][0]["end_date"] == "2024-01-10"


def test_large_age_range_query_is_cheap(client, db_path):
    for index in range(200):
        serial = f"S{index}"
        register(client, serial, "B1", "2024-01-01")
        add_interval(
            client, serial, f"R{index}", "2024-01-01", "2064-01-01"
        )

    stats = exposure(client, "B1", 0, 100000)
    assert stats["at_risk_bearing_days"] == 200 * 14611

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM batch_exposure_segments"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM operation_intervals"
    ).fetchone()[0] == 200
    conn.close()
