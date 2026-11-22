from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "bearings.db"
    monkeypatch.setenv("DB_PATH", str(path))
    # Reset the one-time schema cache so each test gets a pristine database.
    from app import db as db_module

    db_module._initialised.clear()
    return str(path)


@pytest.fixture()
def client(db_path):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def register(client, serial, batch, install):
    response = client.post(
        "/bearings",
        json={
            "serial_number": serial,
            "batch": batch,
            "first_install_date": install,
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def add_interval(client, serial, record_id, start, end):
    response = client.put(
        f"/bearings/{serial}/intervals/{record_id}",
        json={"record_id": record_id, "start_date": start, "end_date": end},
    )
    assert response.status_code == 200, response.text
    return response.json()


def set_failure(client, serial, failure_date):
    response = client.put(
        f"/bearings/{serial}/failure",
        json={"first_failure_date": failure_date},
    )
    assert response.status_code == 200, response.text
    return response.json()


def exposure(client, batch, age_start, age_end):
    response = client.get(
        "/exposure",
        params={"batch": batch, "age_start": age_start, "age_end": age_end},
    )
    assert response.status_code == 200, response.text
    return response.json()
