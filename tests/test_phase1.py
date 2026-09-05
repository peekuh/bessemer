"""Phase 1 checks: the data landed and the views answer correctly."""

from __future__ import annotations

import pytest

from app.config import BUSINESS_UNIT, COVER_SHIFT_TYPE, DEMO_DATE, OFFICE, SHIFT_TYPE
from app.db import query, query_one


def test_dataset_tables_populated():
    counts = {
        t: query_one(f"SELECT count(*) AS n FROM {t}")["n"]
        for t in ("trips", "rider_legs", "trip_alerts")
    }
    assert counts["trips"] > 600_000
    assert counts["rider_legs"] > 1_600_000
    assert counts["trip_alerts"] > 50_000


def test_login_legs_view_scoped_to_our_shift():
    row = query_one(
        """
        SELECT count(*) AS legs, count(DISTINCT stwid) AS riders,
               count(DISTINCT trip_date) AS days
        FROM v_login_legs
        WHERE business_unit = %s AND office = %s AND shift_type = %s
        """,
        (BUSINESS_UNIT, OFFICE, SHIFT_TYPE),
    )
    assert row["legs"] > 25_000
    assert row["riders"] > 500
    assert row["days"] >= 60


def test_login_legs_view_excludes_non_riders():
    """Escorts, ad-hoc rides and the 'Non Shift' label must not survive."""
    row = query_one(
        """
        SELECT
            count(*) FILTER (WHERE stwid = 0)                    AS placeholders,
            count(*) FILTER (WHERE emp_role NOT IN ('employee','projectmgr')) AS non_staff,
            count(*) FILTER (WHERE shift_type !~ '^[0-9]{2}:[0-9]{2}$')       AS bad_shift
        FROM v_login_legs
        """
    )
    assert row == {"placeholders": 0, "non_staff": 0, "bad_shift": 0}


def test_travel_time_view_has_buckets_for_our_morning_window():
    """Half-hours 15-18 cover 07:30 to 09:29 pickups, where our shift lives."""
    rows = query(
        """
        SELECT trip_nodal, pickup_halfhour, n, excess_p75
        FROM v_travel_time
        WHERE office = %s AND pickup_halfhour BETWEEN 15 AND 18
        """,
        (OFFICE,),
    )
    assert len(rows) >= 8
    assert all(r["n"] > 50 for r in rows)
    # Typical overshoot against the rider's own plan is minutes, not hours.
    assert all(0 < float(r["excess_p75"]) < 30 for r in rows)


def test_shift_baseline_has_every_weekday():
    rows = query(
        """
        SELECT dow, days, late_share FROM v_shift_baseline
        WHERE office = %s AND shift_type = %s
        """,
        (OFFICE, SHIFT_TYPE),
    )
    assert {r["dow"] for r in rows} == {"Mon", "Tue", "Wed", "Thu", "Fri"}
    assert all(0 < float(r["late_share"]) < 1 for r in rows)


def test_roster_seeded_both_roles_and_queues():
    rows = query("SELECT role, queue, count(*) AS n FROM roster GROUP BY 1, 2")
    seen = {(r["role"], r["queue"]): r["n"] for r in rows}
    assert seen[("primary", "billing")] == 12
    assert seen[("primary", "techsupport")] == 12
    assert seen[("cover", "billing")] == 12
    assert seen[("cover", "techsupport")] == 12


def test_cover_pool_rides_the_earlier_shift():
    rows = query("SELECT DISTINCT shift_type FROM roster WHERE role = 'cover'")
    assert [r["shift_type"] for r in rows] == [COVER_SHIFT_TYPE]


def test_nobody_covers_for_themselves():
    row = query_one(
        """
        SELECT count(*) AS n FROM roster a JOIN roster b USING (stwid)
        WHERE a.role = 'primary' AND b.role = 'cover'
        """
    )
    assert row["n"] == 0


@pytest.mark.parametrize("role,expected", [("primary", 24), ("cover", 22)])
def test_roster_day_gives_one_leg_per_rider(role, expected):
    """Two cover riders take a second inbound leg; the view must pick one.

    Pinned to the real 11 June, because that is where the two second legs
    live. The designed story day has none by construction.
    """
    row = query_one(
        """
        SELECT count(*) AS rows, count(DISTINCT stwid) AS riders
        FROM v_roster_day WHERE trip_date = %s AND role = %s
        """,
        ("2026-06-11", role),
    )
    assert row["rows"] == row["riders"] == expected


def test_demo_day_is_a_bad_day_for_billing():
    """The demo hangs on this: billing is far below strength at shift start."""
    row = query_one(
        """
        SELECT
            count(*) FILTER (WHERE actual_drop <= shift_start + interval '5 min') AS on_time,
            count(*)                                                              AS rostered
        FROM v_roster_day
        WHERE trip_date = %s AND role = 'primary' AND queue = 'billing'
        """,
        (DEMO_DATE,),
    )
    assert row["rostered"] == 12
    assert row["on_time"] / row["rostered"] < 0.75


def test_cover_pool_is_actually_on_the_floor_by_shift_start():
    row = query_one(
        """
        SELECT count(*) AS n FROM v_roster_day
        WHERE trip_date = %s AND role = 'cover'
          AND actual_drop <= (%s::date + %s::time)
        """,
        (DEMO_DATE, DEMO_DATE, SHIFT_TYPE),
    )
    assert row["n"] >= 15
