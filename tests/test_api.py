from __future__ import annotations

import pytest

from tests.conftest import add_interval, exposure, register, set_failure


def test_closed_range_and_overlap_counted_once(client):
    register(client, "S1", "B1", "2024-01-01")
    # Overlapping records still describe one physical bearing.
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    add_interval(client, "S1", "R2", "2024-01-05", "2024-01-20")

    assert exposure(client, "B1", 0, 0)["at_risk_bearing_days"] == 1
    assert exposure(client, "B1", 0, 9)["at_risk_bearing_days"] == 10
    assert exposure(client, "B1", 0, 19)["at_risk_bearing_days"] == 20
    assert exposure(client, "B1", 4, 4)["at_risk_bearing_days"] == 1
    # Outside the intervals.
    assert exposure(client, "B1", 20, 30)["at_risk_bearing_days"] == 0
    assert exposure(client, "B1", 0, 100)["at_risk_bearing_days"] == 20


def test_gap_between_records_is_not_at_risk(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-03")
    add_interval(client, "S1", "R2", "2024-01-06", "2024-01-08")

    assert exposure(client, "B1", 0, 9)["at_risk_bearing_days"] == 6
    assert exposure(client, "B1", 3, 4)["at_risk_bearing_days"] == 0
    assert exposure(client, "B1", 2, 5)["at_risk_bearing_days"] == 2


def test_failure_day_counts_and_stops_later_accrual(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-31")
    set_failure(client, "S1", "2024-01-11")  # age 10

    stats = exposure(client, "B1", 0, 30)
    assert stats["at_risk_bearing_days"] == 11
    assert stats["failure_count"] == 1

    # A failure at day 10 counts only with an in-operation record that day.
    assert exposure(client, "B1", 9, 10)["failure_count"] == 1
    assert exposure(client, "B1", 0, 9)["failure_count"] == 0
    assert exposure(client, "B1", 11, 30)["at_risk_bearing_days"] == 0


def test_failure_without_operation_record_on_that_day_is_not_counted(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-05")
    set_failure(client, "S1", "2024-01-20")  # no record that day

    stats = exposure(client, "B1", 0, 30)
    assert stats["at_risk_bearing_days"] == 5
    assert stats["failure_count"] == 0

    # Correcting the interval so it covers the failure day makes it count.
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-25")
    stats = exposure(client, "B1", 0, 30)
    assert stats["at_risk_bearing_days"] == 20
    assert stats["failure_count"] == 1


def test_failure_before_install_is_rejected(client):
    register(client, "S1", "B1", "2024-01-10")
    response = client.put(
        "/bearings/S1/failure", json={"first_failure_date": "2024-01-09"}
    )
    assert response.status_code == 400
    assert exposure(client, "B1", 0, 30)["failure_count"] == 0


def test_interval_before_install_is_clipped(client):
    register(client, "S1", "B1", "2024-01-10")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-12")

    stats = exposure(client, "B1", 0, 30)
    assert stats["at_risk_bearing_days"] == 3
    assert exposure(client, "B1", 0, 2)["at_risk_bearing_days"] == 3


def test_separate_batches_are_independent(client):
    register(client, "S1", "B1", "2024-01-01")
    register(client, "S2", "B2", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    add_interval(client, "S2", "R2", "2024-01-01", "2024-01-03")
    set_failure(client, "S2", "2024-01-03")

    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 10
    assert exposure(client, "B1", 0, 30)["failure_count"] == 0
    assert exposure(client, "B2", 0, 30)["at_risk_bearing_days"] == 3
    assert exposure(client, "B2", 0, 30)["failure_count"] == 1


def test_multiple_bearings_accumulate(client):
    register(client, "S1", "B1", "2024-01-01")
    register(client, "S2", "B1", "2024-01-05")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-06")
    add_interval(client, "S2", "R2", "2024-01-05", "2024-01-09")

    # Ages 0..5 for S1, ages 0..4 for S2.
    assert exposure(client, "B1", 0, 0)["at_risk_bearing_days"] == 2
    assert exposure(client, "B1", 4, 5)["at_risk_bearing_days"] == 3
    assert exposure(client, "B1", 0, 100)["at_risk_bearing_days"] == 11


def test_correcting_failure_date_moves_the_event(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-31")
    set_failure(client, "S1", "2024-01-11")
    stats = exposure(client, "B1", 0, 30)
    assert (stats["at_risk_bearing_days"], stats["failure_count"]) == (11, 1)

    set_failure(client, "S1", "2024-01-16")
    stats = exposure(client, "B1", 0, 30)
    assert (stats["at_risk_bearing_days"], stats["failure_count"]) == (16, 1)
    assert exposure(client, "B1", 10, 10)["failure_count"] == 0
    assert exposure(client, "B1", 15, 15)["failure_count"] == 1

    # Clearing the failure restores full accrual.
    set_failure(client, "S1", None)
    stats = exposure(client, "B1", 0, 30)
    assert (stats["at_risk_bearing_days"], stats["failure_count"]) == (31, 0)


def test_late_reported_failure_reduces_exposure(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-31")
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 31

    set_failure(client, "S1", "2024-01-11")
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 11


def test_correcting_interval_updates_statistics(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 10

    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-20")
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 20

    add_interval(client, "S1", "R1", "2024-01-06", "2024-01-20")
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 15
    assert exposure(client, "B1", 0, 4)["at_risk_bearing_days"] == 0


def test_deleting_interval_removes_contribution(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    add_interval(client, "S1", "R2", "2024-01-20", "2024-01-25")
    set_failure(client, "S1", "2024-01-22")

    response = client.delete("/bearings/S1/intervals/R2")
    assert response.status_code == 200
    stats = exposure(client, "B1", 0, 30)
    assert stats["at_risk_bearing_days"] == 10
    assert stats["failure_count"] == 0


def test_failure_after_last_interval_still_counts_once_the_day_is_covered(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-09")
    set_failure(client, "S1", "2024-01-10")
    stats = exposure(client, "B1", 0, 30)
    assert stats["failure_count"] == 0

    add_interval(client, "S1", "R2", "2024-01-10", "2024-01-12")
    stats = exposure(client, "B1", 0, 30)
    assert stats["at_risk_bearing_days"] == 10
    assert stats["failure_count"] == 1


def test_failure_on_install_day(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-01")
    set_failure(client, "S1", "2024-01-01")
    stats = exposure(client, "B1", 0, 30)
    assert stats["at_risk_bearing_days"] == 1
    assert stats["failure_count"] == 1


def test_identical_registration_is_idempotent(client):
    first = register(client, "S1", "B1", "2024-01-01")
    second = register(client, "S1", "B1", "2024-01-01")
    assert first["created"] is True
    assert second["created"] is False

    conflict = client.post(
        "/bearings",
        json={
            "serial_number": "S1",
            "batch": "B2",
            "first_install_date": "2024-01-01",
        },
    )
    assert conflict.status_code == 409


def test_record_ids_are_scoped_per_bearing(client):
    register(client, "S1", "B1", "2024-01-01")
    register(client, "S2", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-10")
    response = client.put(
        "/bearings/S2/intervals/R1",
        json={"record_id": "R1", "start_date": "2024-01-01", "end_date": "2024-01-05"},
    )
    assert response.status_code == 200
    assert exposure(client, "B1", 0, 30)["at_risk_bearing_days"] == 15


def test_unknown_bearing_returns_404(client):
    assert client.get("/bearings/nope").status_code == 404
    response = client.put(
        "/bearings/nope/intervals/R1",
        json={"record_id": "R1", "start_date": "2024-01-01", "end_date": "2024-01-02"},
    )
    assert response.status_code == 404


def test_invalid_range_is_rejected(client):
    response = client.get("/exposure", params={"batch": "B1", "age_start": 5, "age_end": 2})
    assert response.status_code == 400
    response = client.get("/exposure", params={"batch": "B1", "age_start": -1, "age_end": 2})
    assert response.status_code == 422


def test_unknown_batch_query_is_empty(client):
    stats = exposure(client, "missing", 0, 100)
    assert stats["at_risk_bearing_days"] == 0
    assert stats["failure_count"] == 0


def test_failure_date_can_be_reported_at_registration(client):
    response = client.post(
        "/bearings",
        json={
            "serial_number": "S1",
            "batch": "B1",
            "first_install_date": "2024-01-01",
            "first_failure_date": "2024-01-08",
        },
    )
    assert response.status_code == 201
    assert response.json()["first_failure_date"] == "2024-01-08"

    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-31")
    stats = exposure(client, "B1", 0, 30)
    assert (stats["at_risk_bearing_days"], stats["failure_count"]) == (8, 1)


def test_registration_failure_before_install_is_rejected_atomically(client):
    response = client.post(
        "/bearings",
        json={
            "serial_number": "S1",
            "batch": "B1",
            "first_install_date": "2024-01-10",
            "first_failure_date": "2024-01-05",
        },
    )
    assert response.status_code == 400
    assert client.get("/bearings/S1").status_code == 404


def test_repeat_registration_with_new_failure_updates_statistics(client):
    register(client, "S1", "B1", "2024-01-01")
    add_interval(client, "S1", "R1", "2024-01-01", "2024-01-31")

    response = client.post(
        "/bearings",
        json={
            "serial_number": "S1",
            "batch": "B1",
            "first_install_date": "2024-01-01",
            "first_failure_date": "2024-01-11",
        },
    )
    assert response.status_code == 200
    assert response.json()["created"] is False
    stats = exposure(client, "B1", 0, 30)
    assert (stats["at_risk_bearing_days"], stats["failure_count"]) == (11, 1)
