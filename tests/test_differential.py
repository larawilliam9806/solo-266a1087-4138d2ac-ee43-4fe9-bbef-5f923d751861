"""Randomised differential test against a naive day-by-day reference.

The reference expands each bearing's intervals into calendar days.  It lives
only in the test suite; the service under test must never do so.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from tests.conftest import add_interval, exposure, register, set_failure


def _naive(bearings, batch, age_start, age_end):
    bearing_days = 0
    failures = 0
    for bearing in bearings.values():
        if bearing["batch"] != batch:
            continue
        install = bearing["install"]
        failure = bearing["failure"]
        days = set()
        for start, end in bearing["intervals"].values():
            current = max(start, install)
            limit = min(end, failure) if failure else end
            while current <= limit:
                days.add((current - install).days)
                current += timedelta(days=1)
        bearing_days += sum(1 for age in days if age_start <= age <= age_end)
        if failure is not None:
            age = (failure - install).days
            if age in days and age_start <= age <= age_end:
                failures += 1
    return bearing_days, failures


def test_random_ledger_matches_naive_reference(client):
    rng = random.Random(20261122)
    todays = date(2024, 1, 1)
    bearings: dict[str, dict] = {}

    for index in range(12):
        serial = f"S{index}"
        install = todays + timedelta(days=rng.randint(0, 20))
        register(client, serial, "B1", install.isoformat())
        bearings[serial] = {
            "batch": "B1",
            "install": install,
            "failure": None,
            "intervals": {},
        }

    for _ in range(160):
        serial = rng.choice(list(bearings))
        record = rng.choice([f"R{n}" for n in range(6)])
        bearing = bearings[serial]
        start = bearing["install"] + timedelta(days=rng.randint(-3, 60))
        end = start + timedelta(days=rng.randint(0, 25))
        action = rng.random()

        if action < 0.15 and record in bearing["intervals"]:
            response = client.delete(f"/bearings/{serial}/intervals/{record}")
            assert response.status_code == 200, response.text
            del bearing["intervals"][record]
        elif action < 0.3:
            failure = start + timedelta(days=rng.randint(0, 10))
            set_failure(client, serial, failure.isoformat())
            bearing["failure"] = failure
        elif action < 0.36:
            set_failure(client, serial, None)
            bearing["failure"] = None
        else:
            add_interval(client, serial, record, start.isoformat(), end.isoformat())
            bearing["intervals"][record] = (start, end)

        age_start = rng.randint(0, 70)
        age_end = age_start + rng.randint(0, 35)
        expected = _naive(bearings, "B1", age_start, age_end)
        actual = exposure(client, "B1", age_start, age_end)
        assert (actual["at_risk_bearing_days"], actual["failure_count"]) == expected, (
            f"mismatch at age [{age_start}, {age_end}]"
        )
