"""One designed morning, in the dataset's own schema.

The real 11 June gave a good story but a muddy one: both queues opened
near-identical alerts with the same cause, and neither ever reached the two
pathways that matter most for the pitch, a vendor pattern worth escalating and
a day that is lost before anyone arrives. This morning is built so that each
checkpoint shows a different capability.

Everything here is fabricated and says so. The columns, enums and joins are the
dataset's; the rows are ours. The date is one the dataset does not cover, so
nothing real is touched and the three months of real history still feed every
benchmark. Trip ids sit in a reserved band, like the synthetic night shift.

The morning, by design:

  07:30  Quiet. One tech agent on booked leave. Nothing to say.
  08:05  A shared cab from one vendor has not left its depot. Four billing
         riders go CAB_LATE, coverage drops to 67%, and the alert opens on
         cause CAB_NOT_STARTED with a vendor pattern worth escalating.
  08:30  Two tech riders' cab came and went without them. Confirmed no-shows,
         on a queue with one agent of headroom. The day cannot recover, and
         the agent says so an hour before the shift starts.
  08:55  Billing at 67%, service level 15%, four priced options. The click.
  09:30  Billing's cab finally lands and the alert resolves itself. Tech
         support is at 9 of 12 and will stay there.

Run:  uv run python -m db.seed_story [--remove]
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta

from app.config import BUSINESS_UNIT, COVER_SHIFT_TYPE, OFFICE, SHIFT_TYPE
from app.db import connect, query

STORY_DATE = date(2026, 8, 6)  # a Thursday, after the dataset ends
TRIP_ID_BASE = 9_000_001

# Vendors are the dataset's own anonymised names, so the escalation drafts read
# like the rest of the system.
VENDOR_LATE = "Karan Mikhailov Travel"
VENDOR_NOSHOW = "Amit Mikhailov Travel"


def at(hhmm: str) -> datetime:
    return datetime.combine(STORY_DATE, time.fromisoformat(hhmm))


def plus(base: str, minutes: int) -> datetime:
    return at(base) + timedelta(minutes=minutes)


def roster_by_name() -> dict[str, dict]:
    rows = query(
        "SELECT stwid, display_name, queue, role FROM roster WHERE office = %s", (OFFICE,)
    )
    return {r["display_name"]: r for r in rows}


class Morning:
    """Accumulates trips and legs, then writes them in one transaction."""

    def __init__(self) -> None:
        self.trips: list[dict] = []
        self.legs: list[dict] = []
        self._next_trip = TRIP_ID_BASE

    def cab(
        self,
        vendor: str,
        shift: str,
        planned_start: str,
        actual_start: str | None,
        planned_end: str,
        actual_end: str | None,
        nodal: str,
        riders: list[dict],
        delay_reason: str = "NODELAY",
        delay_minutes: int = 0,
    ) -> None:
        """One shared cab and everyone on it.

        Each rider dict: name, planned_pickup, actual_pickup, planned_drop,
        actual_drop, and optionally boarding ('Boarded' default), reason, km.
        """
        trip_id = self._next_trip
        self._next_trip += 1
        boarded = [r for r in riders if r.get("boarding", "Boarded") == "Boarded"]
        self.trips.append(
            {
                "trip_id": trip_id,
                "business_unit": BUSINESS_UNIT,
                "office": OFFICE,
                "product_type": "CAB",
                "trip_date": STORY_DATE,
                "shift_type": shift,
                "trip_direction": "LOGIN",
                "vendor_id": vendor,
                "planned_start": at(planned_start),
                "planned_end": at(planned_end),
                "actual_start": at(actual_start) if actual_start else None,
                "actual_end": at(actual_end) if actual_end else None,
                "delay_reason": delay_reason,
                "delay_minutes": delay_minutes,
                "trip_nodal": nodal,
                "planned_cnt": len(riders),
                "actual_cnt": len(boarded),
                "noshow_cnt": sum(1 for r in riders if r.get("reason") == "NO_SHOW"),
            }
        )
        for i, r in enumerate(riders):
            person = ROSTER[r["name"]]
            self.legs.append(
                {
                    "trip_id": trip_id,
                    "stwid": person["stwid"],
                    "business_unit": BUSINESS_UNIT,
                    "office": OFFICE,
                    "trip_date": STORY_DATE,
                    "shift_type": shift,
                    "planned_pickup": at(r["planned_pickup"]),
                    "planned_drop": at(r["planned_drop"]),
                    "actual_pickup": at(r["actual_pickup"]) if r.get("actual_pickup") else None,
                    "actual_drop": at(r["actual_drop"]) if r.get("actual_drop") else None,
                    "planned_km": r.get("km", 12.0),
                    "traveled_km": r.get("km", 12.0) * (1.05 if r.get("actual_drop") else None or 1.0) if r.get("actual_drop") else None,
                    "signintype": "Planned",
                    "gender": "FEMALE" if i % 2 else "MALE",
                    "emp_role": "employee",
                    "boarding_status": r.get("boarding", "Boarded"),
                    "not_boarding_reason": r.get("reason"),
                    "is_no_show": r.get("reason") == "NO_SHOW",
                }
            )

    def write(self) -> tuple[int, int]:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM rider_legs WHERE trip_id >= %s AND trip_date = %s",
                (TRIP_ID_BASE, STORY_DATE),
            )
            cur.execute(
                "DELETE FROM trips WHERE trip_id >= %s AND trip_date = %s",
                (TRIP_ID_BASE, STORY_DATE),
            )
            for t in self.trips:
                cols = ", ".join(t)
                vals = ", ".join(f"%({k})s" for k in t)
                cur.execute(f"INSERT INTO trips ({cols}) VALUES ({vals})", t)
            for leg in self.legs:
                cols = ", ".join(leg)
                vals = ", ".join(f"%({k})s" for k in leg)
                cur.execute(f"INSERT INTO rider_legs ({cols}) VALUES ({vals})", leg)
            conn.commit()
        return len(self.trips), len(self.legs)


def remove() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM rider_legs WHERE trip_id >= %s", (TRIP_ID_BASE,))
        cur.execute("DELETE FROM trips WHERE trip_id >= %s", (TRIP_ID_BASE,))
        conn.commit()


ROSTER: dict[str, dict] = {}


def build() -> Morning:
    m = Morning()

    # ------------------------------------------------------------ billing
    #
    # The vendor problem. One cab, four riders, planned to leave at 07:50 on a
    # 60-minute route. It leaves at 08:28. The trip length is what makes the
    # projection see trouble at 08:05: a cab that is twenty minutes late on a
    # route that long cannot make 09:05, and the arithmetic knows it before
    # anyone has been collected. Stamped NODELAY, because that is what the
    # transport system does to 70% of arrivals that miss the shift start.
    m.cab(
        VENDOR_LATE, SHIFT_TYPE, "07:50", "08:28", "08:50", "09:30", "NODAL",
        [
            {"name": "Agent 17", "planned_pickup": "07:54", "actual_pickup": "08:32", "planned_drop": "08:50", "actual_drop": "09:28", "km": 22.4},
            {"name": "Agent 19", "planned_pickup": "07:57", "actual_pickup": "08:35", "planned_drop": "08:50", "actual_drop": "09:29", "km": 20.1},
            {"name": "Agent 21", "planned_pickup": "08:00", "actual_pickup": "08:38", "planned_drop": "08:50", "actual_drop": "09:29", "km": 18.6},
            {"name": "Agent 23", "planned_pickup": "08:03", "actual_pickup": "08:41", "planned_drop": "08:50", "actual_drop": "09:30", "km": 16.9},
        ],
    )
    # Two ordinary cabs. Everyone in before 08:56.
    m.cab(
        "Sanjay Mikhailov Travel", SHIFT_TYPE, "08:00", "08:02", "08:50", "08:50", "HOME",
        [
            {"name": "Agent 01", "planned_pickup": "08:05", "actual_pickup": "08:07", "planned_drop": "08:50", "actual_drop": "08:49", "km": 14.2},
            {"name": "Agent 03", "planned_pickup": "08:10", "actual_pickup": "08:12", "planned_drop": "08:50", "actual_drop": "08:49", "km": 11.8},
            {"name": "Agent 05", "planned_pickup": "08:15", "actual_pickup": "08:17", "planned_drop": "08:50", "actual_drop": "08:50", "km": 9.3},
            {"name": "Agent 07", "planned_pickup": "08:20", "actual_pickup": "08:22", "planned_drop": "08:50", "actual_drop": "08:50", "km": 7.1},
        ],
    )
    m.cab(
        "Rahul Orlov Travel", SHIFT_TYPE, "08:08", "08:07", "08:55", "08:55", "HOME",
        [
            {"name": "Agent 09", "planned_pickup": "08:10", "actual_pickup": "08:11", "planned_drop": "08:55", "actual_drop": "08:53", "km": 15.0},
            {"name": "Agent 11", "planned_pickup": "08:14", "actual_pickup": "08:15", "planned_drop": "08:55", "actual_drop": "08:54", "km": 12.6},
            {"name": "Agent 13", "planned_pickup": "08:18", "actual_pickup": "08:19", "planned_drop": "08:55", "actual_drop": "08:54", "km": 10.2},
            {"name": "Agent 15", "planned_pickup": "08:22", "actual_pickup": "08:23", "planned_drop": "08:55", "actual_drop": "08:55", "km": 8.4},
        ],
    )

    # ------------------------------------------------------- tech support
    #
    # The absence problem. Nine arrive normally. One is on leave, cancelled in
    # advance through the dashboard, which the agent must not raise as an
    # alarm. Two more are rostered, never cancel, and are not at their stops
    # when the cab arrives. Three seats gone on a queue staffed with one agent
    # of headroom: the day is lost at 08:30, and the value is in saying so at
    # 08:30 rather than at five o'clock.
    m.cab(
        "Rahul Morozov Travel", SHIFT_TYPE, "07:58", "08:00", "08:52", "08:53", "NODAL",
        [
            {"name": "Agent 02", "planned_pickup": "08:03", "actual_pickup": "08:05", "planned_drop": "08:52", "actual_drop": "08:50", "km": 19.7},
            {"name": "Agent 04", "planned_pickup": "08:07", "actual_pickup": "08:09", "planned_drop": "08:52", "actual_drop": "08:51", "km": 17.3},
            {"name": "Agent 06", "planned_pickup": "08:11", "actual_pickup": "08:13", "planned_drop": "08:52", "actual_drop": "08:51", "km": 15.0},
            {"name": "Agent 08", "planned_pickup": "08:15", "actual_pickup": "08:17", "planned_drop": "08:52", "actual_drop": "08:52", "km": 12.8},
            {"name": "Agent 10", "planned_pickup": "08:19", "actual_pickup": "08:21", "planned_drop": "08:52", "actual_drop": "08:53", "km": 10.4},
        ],
    )
    m.cab(
        "Divya Kozlov Travel", SHIFT_TYPE, "08:05", "08:04", "08:55", "08:58", "HOME",
        [
            {"name": "Agent 12", "planned_pickup": "08:09", "actual_pickup": "08:08", "planned_drop": "08:55", "actual_drop": "08:56", "km": 13.9},
            {"name": "Agent 14", "planned_pickup": "08:13", "actual_pickup": "08:12", "planned_drop": "08:55", "actual_drop": "08:57", "km": 11.5},
            {"name": "Agent 18", "planned_pickup": "08:17", "actual_pickup": "08:16", "planned_drop": "08:55", "actual_drop": "08:57", "km": 9.8},
            {"name": "Agent 24", "planned_pickup": "08:21", "actual_pickup": "08:20", "planned_drop": "08:55", "actual_drop": "08:58", "km": 7.6},
        ],
    )
    # The cab that collected nobody. Left on time, arrived early and empty.
    m.cab(
        VENDOR_NOSHOW, SHIFT_TYPE, "08:00", "08:02", "08:50", "08:40", "HOME",
        [
            {"name": "Agent 16", "planned_pickup": "08:10", "planned_drop": "08:50", "boarding": "Not Boarded", "reason": "NO_SHOW", "km": 13.1},
            {"name": "Agent 20", "planned_pickup": "08:14", "planned_drop": "08:50", "boarding": "Not Boarded", "reason": "NO_SHOW", "km": 10.7},
        ],
    )
    # Booked leave. The trip was cancelled from the dashboard the day before.
    m.cab(
        "Pooja Mikhailov Travel", SHIFT_TYPE, "08:05", None, "08:55", None, "HOME",
        [
            {"name": "Agent 22", "planned_pickup": "08:10", "planned_drop": "08:55", "boarding": "Not Boarded", "reason": "TRIP_CANCELLED_FROM_DASHBOARD", "km": 11.0},
        ],
    )

    # ------------------------------------------------------- early shift
    #
    # The cover pool. Twenty-four riders on the 08:30 shift, all in by 08:29.
    # The cabs carrying the people the story names land first, so the fairness
    # ranking picks them without being told to.
    cover = sorted(
        (r for r in ROSTER.values() if r["role"] == "cover"),
        key=lambda r: r["display_name"],
    )
    preferred = ["Agent 27", "Agent 29", "Agent 31", "Agent 35", "Agent 28", "Agent 36", "Agent 30", "Agent 32"]
    ordered = [ROSTER[n] for n in preferred] + [r for r in cover if r["display_name"] not in preferred]
    drops = ["08:14", "08:16", "08:19", "08:21", "08:23", "08:26"]
    vendors = ["Sneha Mikhailov Travel", "Priya Mikhailov Travel", "Arjun Mikhailov Travel",
               "Divya Sokolov Travel", "Aarav Mikhailov Travel", "Rohan Mikhailov Travel"]
    for c in range(6):
        group = ordered[c * 4:(c + 1) * 4]
        drop = drops[c]
        start = (at(drop) - timedelta(minutes=50)).strftime("%H:%M")
        m.cab(
            vendors[c], COVER_SHIFT_TYPE, start, (at(start) + timedelta(minutes=1)).strftime("%H:%M"),
            drop, drop, "HOME" if c % 2 else "NODAL",
            [
                {
                    "name": g["display_name"],
                    "planned_pickup": (at(start) + timedelta(minutes=5 + 4 * i)).strftime("%H:%M"),
                    "actual_pickup": (at(start) + timedelta(minutes=6 + 4 * i)).strftime("%H:%M"),
                    "planned_drop": drop,
                    "actual_drop": drop,
                    "km": 14.0 - 2.5 * i,
                }
                for i, g in enumerate(group)
            ],
        )
    return m


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="delete the story rows")
    args = parser.parse_args()
    if args.remove:
        remove()
        print("story rows removed")
        return 0

    ROSTER.update(roster_by_name())
    missing = [n for n in ["Agent 01", "Agent 24", "Agent 27", "Agent 48"] if n not in ROSTER]
    if missing:
        print(f"roster is missing {missing}; run db.seed_roster first")
        return 1

    trips, legs = build().write()
    print(f"seeded {STORY_DATE:%A %Y-%m-%d}: {trips} trips, {legs} rider legs")
    rows = query(
        """
        SELECT role, queue, count(*) AS n,
               count(*) FILTER (WHERE actual_drop IS NOT NULL) AS arrive
        FROM v_roster_day WHERE trip_date = %s GROUP BY 1, 2 ORDER BY 1 DESC, 2
        """,
        (STORY_DATE,),
    )
    for r in rows:
        print(f"  {r['role']:<8} {r['queue']:<12} {r['n']:>2} rostered, {r['arrive']:>2} arrive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
