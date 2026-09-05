"""Finding someone to cover the gap, and pricing what it costs.

An alert that only reports a problem hands the manager a search task at the
worst possible moment. This module does the search first, so the alert arrives
with names and a price attached.

Clearwater runs 24/7 under **positional handover**: a night agent cannot leave
their desk until their relief is seated. That single rule changes the shape of
the problem. On a nine-to-five floor a late arrival thins the queue and that is
the end of it. Here the queue never goes unmanned, so the cost lands on a
colleague instead. Somebody who started at one in the morning is still at their
desk at ten past nine, being paid overtime, watching their cab home leave
without them.

So there is no free option. Doing nothing is itself a decision to spend
somebody's morning, and the alert says so.

Three pathways, in order of preference:

  HOLD_OVER          keep the night agent who is already covering the position.
                     Zero risk, immediate, and the most expensive: overtime at
                     1.5x, and past 09:15 they miss their booked ride home.
  EARLY_SHIFT_COVER  move someone from the 08:30 shift onto the position. They
                     do this work already and are demonstrably in the building,
                     so the night agent goes home on time.
  CROSS_COVER        borrow from the other queue. Flagged as a trade, because
                     queue skills are not interchangeable and the manager
                     should know they are accepting slower handling.

Ranking is by cover minutes already absorbed this ISO week, ascending, so the
same willing person is not asked every morning. That counter starts at zero and
increments when the manager acts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any

from app.config import (
    NIGHT_OUTBOUND_CAB,
    OVERTIME_MULTIPLIER,
    OVERTIME_RATE_PER_HOUR,
)
from app.core.state import State, rider_state
from app.db import query

SHIFT_HOURS = 8.0
"""How long a night agent is held if their relief never comes at all."""


@dataclass(frozen=True)
class Candidate:
    """One person who could cover, with the evidence that they can."""

    stwid: int
    display_name: str
    queue: str
    same_queue: bool
    arrived_at: datetime
    cover_minutes_this_week: int
    pathway: str

    @property
    def note(self) -> str:
        """One line a manager can read without opening anything else."""
        where = "same queue" if self.same_queue else f"from {self.queue}"
        seen = self.arrived_at.strftime("%H:%M")
        if self.cover_minutes_this_week:
            return f"{where}, on floor since {seen}, {self.cover_minutes_this_week} min cover this week"
        return f"{where}, on floor since {seen}, no cover yet this week"

    def as_dict(self) -> dict[str, Any]:
        return {
            "stwid": self.stwid,
            "name": self.display_name,
            "queue": self.queue,
            "same_queue": self.same_queue,
            "arrived_at": self.arrived_at.isoformat(),
            "cover_minutes_this_week": self.cover_minutes_this_week,
            "pathway": self.pathway,
            "note": self.note,
        }


def iso_week(on: date) -> str:
    """'2026-W24'. The window the fairness counter resets on."""
    year, week, _ = on.isocalendar()
    return f"{year}-W{week:02d}"


COVER_POOL_SQL = """
SELECT d.stwid,
       d.display_name,
       d.queue,
       d.actual_drop
FROM v_roster_day d
WHERE d.trip_date = %(on)s
  AND d.office = %(office)s
  AND d.role = 'cover'
  AND d.actual_drop IS NOT NULL
ORDER BY d.actual_drop
"""

COVER_MINUTES_SQL = "SELECT stwid, minutes FROM cover_log WHERE iso_week = %s"


@lru_cache(maxsize=16)
def _cover_pool(on: date, office: str) -> tuple[dict[str, Any], ...]:
    """Everyone who could cover today, loaded once per shift.

    The eligibility filter that varies with the clock is `actual_drop <= now`,
    and applying it in SQL meant a fresh query on every tick: 168 round trips
    for one replay, nine of its eighteen seconds. The rows themselves never
    change during a shift, so they are fetched once and filtered in memory.
    """
    return tuple(query(COVER_POOL_SQL, {"on": on, "office": office}))


def _cover_minutes(week: str) -> dict[int, int]:
    """This week's fairness counter. Small, and it changes when people act,
    so it is read fresh rather than cached."""
    return {r["stwid"]: r["minutes"] for r in query(COVER_MINUTES_SQL, (week,))}


def candidates(
    queue: str,
    on: date,
    now: datetime,
    office: str,
    limit: int = 3,
    allow_cross_queue: bool = True,
) -> list[Candidate]:
    """Who could cover this queue right now.

    Only riders whose drop has already been recorded at `now` are returned, so
    every suggestion is a person verifiably in the building. Same-queue
    candidates come first; cross-queue ones follow only if the queue cannot
    field enough of its own.

    Args:
        queue: the short-staffed queue.
        on: the shift date.
        now: the replay clock. Nothing after this instant is visible.
        office: site.
        limit: how many names to return.
        allow_cross_queue: whether to fall back to the other queue.
    """
    charged = _cover_minutes(iso_week(on))
    rows = [
        dict(r, cover_minutes=charged.get(r["stwid"], 0))
        for r in _cover_pool(on, office)
        if r["actual_drop"] <= now
    ]
    rows.sort(key=lambda r: (r["cover_minutes"], r["actual_drop"]))

    same = [r for r in rows if r["queue"] == queue]
    other = [r for r in rows if r["queue"] != queue] if allow_cross_queue else []

    picked: list[Candidate] = []
    for row, is_same in [(r, True) for r in same] + [(r, False) for r in other]:
        if len(picked) >= limit:
            break
        picked.append(
            Candidate(
                stwid=row["stwid"],
                display_name=row["display_name"],
                queue=row["queue"],
                same_queue=is_same,
                arrived_at=row["actual_drop"],
                cover_minutes_this_week=int(row["cover_minutes"]),
                pathway="EARLY_SHIFT_COVER" if is_same else "CROSS_COVER",
            )
        )
    return picked


@dataclass(frozen=True)
class HoldOver:
    """What it costs to keep the night shift at their desks.

    This is the consequence of doing nothing, priced. Under positional handover
    the position stays manned whatever the manager decides, so these minutes
    are spent unless somebody else takes the seat.
    """

    agents_held: int
    minutes: float
    """Total overtime minutes across everyone held past their shift end."""

    cost: float
    """Overtime pay at the configured multiplier."""

    until: datetime | None
    """When the last held agent can finally leave."""

    missed_cabs: int
    """How many will miss their booked ride home. The cost that is not money:
    a night worker stranded at the campus after an eight-hour shift."""

    @property
    def summary(self) -> str:
        if not self.agents_held:
            return "no hold-over needed"
        each = round(self.minutes / max(1, self.agents_held))
        span = f"{each} min" if each < 120 else f"{each / 60:.0f} hours"
        parts = [
            f"{self.agents_held} night agent{'s' if self.agents_held > 1 else ''} "
            f"held {span} past shift end"
        ]
        if self.missed_cabs:
            parts.append(
                f"{self.missed_cabs} would miss the {NIGHT_OUTBOUND_CAB} cab home"
            )
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agents_held": self.agents_held,
            "minutes": round(self.minutes),
            "cost": round(self.cost),
            "until": self.until.isoformat() if self.until else None,
            "missed_cabs": self.missed_cabs,
            "summary": self.summary,
        }


def hold_over_cost(
    queue: str,
    on: date,
    shift_start: datetime,
    gap_size: int,
    recovered_by: datetime | None,
) -> HoldOver:
    """Price keeping the night shift on for a queue that is short.

    One night agent is pinned per missing relief, and only until their own
    relief actually arrives, so the cost scales with both how many seats are
    empty and how long they stay empty.

    Args:
        queue: the short-staffed queue.
        on: shift date.
        shift_start: when the night shift was due to end.
        gap_size: how many positions are unrelieved.
        recovered_by: when the last late rider is projected to land.
    """
    if gap_size <= 0:
        return HoldOver(0, 0.0, 0.0, None, 0)

    # No projected arrival means nobody is coming: a confirmed no-show or a
    # cancellation. Under positional handover the seat still has to be held,
    # and it has to be held for the whole shift. An earlier version returned a
    # zero cost here, which made a permanent absence look cheaper than a
    # twenty-minute delay.
    if recovered_by is None:
        recovered_by = shift_start + timedelta(hours=SHIFT_HOURS)
    if recovered_by <= shift_start:
        return HoldOver(0, 0.0, 0.0, None, 0)

    available = query(
        """
        SELECT count(*) AS n FROM roster
        WHERE role = 'night' AND queue = %s
        """,
        (queue,),
    )[0]["n"]
    held = min(gap_size, int(available))

    minutes_each = (recovered_by - shift_start).total_seconds() / 60.0
    total_minutes = held * minutes_each
    cost = (total_minutes / 60.0) * OVERTIME_RATE_PER_HOUR * OVERTIME_MULTIPLIER

    cab_home = datetime.combine(on, time.fromisoformat(NIGHT_OUTBOUND_CAB))
    missed = held if recovered_by > cab_home else 0

    return HoldOver(
        agents_held=held,
        minutes=total_minutes,
        cost=cost,
        until=recovered_by,
        missed_cabs=missed,
    )


def night_agents(queue: str, limit: int = 3) -> list[dict[str, Any]]:
    """The night agents currently holding this queue's positions.

    Named so the manager can see who they are asking to stay, and so the
    drafted message to the night lead is addressed to real people rather than
    a headcount.
    """
    rows = query(
        """
        SELECT stwid, display_name, queue, shift_ends
        FROM roster
        WHERE role = 'night' AND queue = %s
        ORDER BY display_name
        LIMIT %s
        """,
        (queue, limit),
    )
    return [
        {
            "stwid": r["stwid"],
            "name": r["display_name"],
            "queue": r["queue"],
            "shift_ends": r["shift_ends"].strftime("%H:%M") if r["shift_ends"] else None,
        }
        for r in rows
    ]


def clear_cache() -> None:
    """Drop the cached cover pool. Needed after a reload, mainly in tests."""
    _cover_pool.cache_clear()


def record_cover(stwids: list[int], on: date, minutes: int) -> None:
    """Charge cover minutes to the people who absorbed them.

    Keeps the fairness ranking honest across a week: whoever covered on Monday
    drops down the list on Tuesday.
    """
    week = iso_week(on)
    from app.db import connect

    with connect() as conn, conn.cursor() as cur:
        for stwid in stwids:
            cur.execute(
                """
                INSERT INTO cover_log (stwid, iso_week, minutes)
                VALUES (%s, %s, %s)
                ON CONFLICT (stwid, iso_week)
                DO UPDATE SET minutes = cover_log.minutes + EXCLUDED.minutes
                """,
                (stwid, week, minutes),
            )
        conn.commit()


def contactable(riders: list[Any]) -> list[dict[str, Any]]:
    """Riders the manager should phone: unaccounted for, not merely late.

    A rider stuck in traffic needs no call. A rider whose cab has been and gone
    without them is a different problem, and it is the one where a two-minute
    phone call still changes the outcome.
    """
    return [
        {
            "stwid": r.stwid,
            "name": r.display_name,
            "queue": r.queue,
            "state": r.state.value,
            "reason": (
                "no pickup recorded well past the expected time"
                if r.state is State.NO_PICKUP
                else "marked not boarded with no advance cancellation"
            ),
        }
        for r in riders
        if r.state in {State.NO_PICKUP, State.NO_SHOW}
    ]
