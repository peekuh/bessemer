"""Queue staffing projection and its consequence for the floor.

This is the translation layer. Upstream the world is riders and cabs; the line
manager does not think in either. They think in "is my billing queue staffed at
nine". Everything here converts commute facts into floor facts.

Two decisions worth stating plainly, because they shape the whole alert:

**Headcount projections use the median ETA, never the pessimistic one.** The
band from `eta.py` exists so a rider can be *shaded* as at risk in the UI, but
if the alert counted the unlucky end of every band it would fire every morning
before anyone had left home. The planned buffer between drop and shift start is
about 5 minutes against roughly 13 minutes of journey noise, so the pessimistic
read is "everybody is at risk, always", which is true and useless.

**Impact is expressed in the manager's own units.** "Three agents missing" is a
transport fact. "About twelve calls unanswered in the first half hour" is a
floor fact, and it is the one that decides whether they act. The arithmetic is
deliberately simple and its assumptions are on the record below, because a
number a manager cannot interrogate is a number they will not trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from app.core.eta import Projection, Risk, project_arrival
from app.core.sla import (
    Adherence,
    DayProjection,
    ServiceLevel,
    adherence,
    agents_needed,
    project_day,
    service_level,
)
from app.core.state import State, pickup_delay_min, rider_state


@dataclass(frozen=True)
class RiderStatus:
    """One rider, resolved at one instant. The unit the whole board is built from."""

    stwid: int
    display_name: str
    queue: str
    state: State
    projection: Projection
    vendor_id: str | None = None
    trip_nodal: str | None = None
    minutes_late: float | None = None
    """Projected minutes past the deadline. Negative means comfortably early."""

    pickup_delay_min: float = 0.0
    """How late the rider's pickup is or was, as observable at `now`. Separates
    "the cab reached them late" from "the journey ran long", which are
    different causes with different owners."""

    @property
    def on_floor(self) -> bool:
        return self.state.is_present

    @property
    def expected(self) -> bool:
        """Counted in the projected headcount: here, or credibly on the way.

        The test is the projected arrival against the deadline, not the risk
        label. Those come apart once the planned drop has passed: a rider
        aboard a cab two minutes from the door is `OVERDUE` by the strict
        definition, and would be wrong to score as missing. Reading the label
        instead of the clock made coverage collapse from 67% to 8% the moment
        the planned drop time went by, which is an artefact rather than
        anything that happened on the floor.

        A rider with no usable ETA is not counted. Unknown is not the same as
        fine, and the alert should rather ask than assume.
        """
        if self.state.is_present:
            return True
        if self.state.is_absent:
            return False
        return self.minutes_late is not None and self.minutes_late <= 0

    @property
    def is_affected(self) -> bool:
        """Belongs in the alert: not projected to make it, or unaccounted for.

        Judged on the median projection, the same basis as coverage and the
        queue-card buckets. An earlier version used the pessimistic band here,
        and because the plan leaves five minutes of slack against thirteen of
        noise, that swept every travelling rider into the affected set. The
        side effects were not cosmetic: the vendor named for escalation was
        whichever ordinary cab happened to sort first, and a queue with three
        confirmed absences was diagnosed as an en-route delay because nine
        on-time riders outvoted them.
        """
        if self.on_floor:
            return False
        return self.state.is_uncertain or not self.expected

    @property
    def needs_attention(self) -> bool:
        return (
            self.state.is_uncertain
            or self.state is State.NO_SHOW
            or (not self.state.is_present and self.projection.risk.needs_attention)
        )

    def as_dict(self) -> dict[str, Any]:
        """Flat form for the API, the UI and the agent's tool output."""
        return {
            "stwid": self.stwid,
            "name": self.display_name,
            "queue": self.queue,
            "state": self.state.value,
            "risk": self.projection.risk.value,
            "eta": self.projection.eta.isoformat() if self.projection.eta else None,
            "eta_spread_min": round(self.projection.spread_min, 1),
            "eta_basis": self.projection.basis,
            "minutes_late": round(self.minutes_late, 1) if self.minutes_late is not None else None,
            "vendor": self.vendor_id,
            "pickup_type": self.trip_nodal,
        }


@dataclass(frozen=True)
class QueueImpact:
    """What a staffing shortfall costs the queue, in the manager's units.

    The cost is reported as a **range**, not a point. Journey times carry about
    13 minutes of unmodelled spread, so a single number would be false
    precision in either direction: the median read under-calls a bad morning,
    and the pessimistic read cries wolf on an ordinary one. Measured on the
    demo day, the median projection expected 67% of billing to make it and 25%
    actually did. A manager can act sensibly on "5 to 15 calls"; they cannot
    act on a confident 5 that turns out to be 20.
    """

    agents_missing: int
    minutes_lost: float
    """Agent-minutes of absence between the deadline and the median arrival."""

    minutes_lost_high: float
    """The same at the pessimistic end of every rider's band."""

    calls_unanswered: float
    """minutes_lost / average handle time. The optimistic end of the range."""

    calls_unanswered_high: float
    """The pessimistic end. Quote both; never quote one alone."""

    coverage: float
    """Projected headcount as a fraction of the roster."""

    worst_eta: datetime | None
    recovered_by: datetime | None
    """When the last rider is projected to land, so the manager knows how long
    the gap lasts rather than only how deep it is."""

    service_level: ServiceLevel | None = None
    """The contracted metric, at the projected headcount. Non-linear in
    staffing, which is the part a headcount alone cannot convey."""

    service_level_full: ServiceLevel | None = None
    """The same queue at full strength, so the gap is legible as a gap."""

    day: DayProjection | None = None
    """The whole shift rolled up. This is what gets reported upward."""

    adherence: Adherence | None = None
    agents_needed: int = 0
    """Smallest headcount that meets the target. Turns "you are short" into
    "you need two more on this queue"."""

    @property
    def calls_range(self) -> str:
        """Human phrasing for the narrative and the board."""
        low, high = round(self.calls_unanswered), round(self.calls_unanswered_high)
        return str(low) if low == high else f"{low} to {high}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "agents_missing": self.agents_missing,
            "minutes_lost": round(self.minutes_lost),
            "minutes_lost_high": round(self.minutes_lost_high),
            "calls_unanswered": round(self.calls_unanswered),
            "calls_unanswered_high": round(self.calls_unanswered_high),
            "calls_range": self.calls_range,
            "coverage": round(self.coverage, 3),
            "coverage_pct": round(self.coverage * 100),
            "worst_eta": self.worst_eta.isoformat() if self.worst_eta else None,
            "recovered_by": self.recovered_by.isoformat() if self.recovered_by else None,
            "agents_needed": self.agents_needed,
            "service_level": self.service_level.as_dict() if self.service_level else None,
            "service_level_full": (
                self.service_level_full.as_dict() if self.service_level_full else None
            ),
            "day": self.day.as_dict() if self.day else None,
            "adherence": self.adherence.as_dict() if self.adherence else None,
        }


@dataclass
class QueueProjection:
    """A queue's position at one instant: who is where, and what it costs."""

    queue: str
    display_name: str
    deadline: datetime
    riders: list[RiderStatus] = field(default_factory=list)
    aht_min: float = 5.0
    calls_per_30min: int = 30
    sl_target: str = "80/20"
    shift_hours: float = 8.0

    # --- headcount --------------------------------------------------------

    @property
    def rostered(self) -> int:
        """Everyone on the roster, absences included. The denominator."""
        return len(self.riders)

    @property
    def scheduled(self) -> int:
        """Rostered and actually expected to travel today."""
        return sum(1 for r in self.riders if r.state is not State.NOT_SCHEDULED)

    @property
    def on_floor(self) -> list[RiderStatus]:
        return [r for r in self.riders if r.on_floor]

    @property
    def in_transit(self) -> list[RiderStatus]:
        """Travelling and projected to make the deadline.

        Bucketed on the median projection, the same basis as `projected` and
        `coverage`, so the four counts on a queue card add up to the roster and
        agree with the percentage beside them. A first version bucketed on the
        pessimistic band instead, and because the plan leaves five minutes of
        slack against thirteen of noise, that put every travelling rider in
        "at risk" and left "on the way" reading zero for the entire morning
        while the card said eight were expected.
        """
        return [
            r for r in self.riders
            if not r.on_floor and r.state.is_travelling and r.expected
        ]

    @property
    def at_risk(self) -> list[RiderStatus]:
        """Not projected to make it, or unaccounted for."""
        return [
            r for r in self.riders
            if not r.on_floor
            and not r.state.is_absent
            and (r.state.is_uncertain or not r.expected)
        ]

    @property
    def absent(self) -> list[RiderStatus]:
        return [r for r in self.riders if r.state.is_absent and r.state is not State.NOT_SCHEDULED]

    @property
    def projected(self) -> int:
        """Headcount expected on the floor by the deadline."""
        return sum(1 for r in self.riders if r.expected)

    @property
    def coverage(self) -> float:
        """Projected headcount over the roster. What the threshold reads."""
        base = self.scheduled or self.rostered
        return self.projected / base if base else 1.0

    # --- consequence ------------------------------------------------------

    def impact(self) -> QueueImpact:
        """Cost of the shortfall, from the deadline to the last arrival.

        Each missing agent contributes the minutes between the deadline and
        their own projected arrival. Dividing by average handle time turns
        agent-minutes into calls the queue will not answer.

        Assumptions, all synthetic and all in `queues`: handle time is
        constant, agents are interchangeable within a queue, and demand is
        even across the window. Real workforce planning models none of these
        that simply. It is enough to rank one morning against another, which
        is the decision actually in front of the manager.
        """
        base = self.scheduled or self.rostered
        missing = [r for r in self.riders if not r.expected and r.state is not State.NOT_SCHEDULED]

        # Absence for the whole forecast half-hour, charged when a rider has no
        # usable ETA. Scoring the unknown as free would let the worst cases
        # vanish from the total.
        window_min = 30.0

        minutes_lost = 0.0
        minutes_lost_high = 0.0
        etas: list[datetime] = []
        for rider in missing:
            eta = rider.projection.eta
            worst = rider.projection.pessimistic_eta
            if eta is None:
                minutes_lost += window_min
                minutes_lost_high += window_min
                continue
            minutes_lost += max(0.0, (eta - self.deadline).total_seconds() / 60.0)
            minutes_lost_high += max(0.0, (worst - self.deadline).total_seconds() / 60.0)
            if eta > self.deadline:
                etas.append(eta)

        aht = self.aht_min or 1.0
        recovered_by = max(etas) if etas else None

        # --- the contracted metrics -------------------------------------
        #
        # Headcount is the input; service level is the number the manager is
        # measured on, and it falls away far faster than the headcount does.
        # Reporting both together is the point: "four short" and "service
        # level from 92% to 15%" are the same fact, and only the second one
        # tells a manager whether to act.
        now_sl = service_level(
            self.projected, self.calls_per_30min, aht, 30.0, self.sl_target
        )
        full_sl = service_level(
            self.rostered, self.calls_per_30min, aht, 30.0, self.sl_target
        )
        needed = agents_needed(self.calls_per_30min, aht, 30.0, self.sl_target)

        day = project_day(
            impaired=self._impaired_intervals(recovered_by),
            full_strength_agents=self.rostered,
            calls_per_interval=self.calls_per_30min,
            aht_min=aht,
            shift_hours=self.shift_hours,
            interval_min=30.0,
            sl_target=self.sl_target,
        )

        return QueueImpact(
            agents_missing=len(missing),
            minutes_lost=minutes_lost,
            minutes_lost_high=minutes_lost_high,
            calls_unanswered=minutes_lost / aht,
            calls_unanswered_high=minutes_lost_high / aht,
            coverage=self.projected / base if base else 1.0,
            worst_eta=recovered_by,
            recovered_by=recovered_by,
            service_level=now_sl,
            service_level_full=full_sl,
            day=day,
            adherence=adherence(
                self.rostered, minutes_lost, minutes_lost_high, self.shift_hours
            ),
            agents_needed=needed,
        )

    def _impaired_intervals(
        self, recovered_by: datetime | None
    ) -> list[tuple[float, float]]:
        """Headcount per 30-minute interval while the gap lasts.

        Returns (agents, share_of_interval) pairs from the deadline forward.
        The share matters: a gap that closes ten minutes into an interval
        should not be charged for the whole thirty, or a brief disruption would
        look identical to a sustained one in the day's number.

        A confirmed absence has no arrival time, and an early version treated
        that as nothing to project and therefore nothing to count. The effect
        was the opposite of intended: a queue permanently seven agents down
        reported a healthy day, because the one condition that never recovers
        looked like the one that had already recovered. Absences now hold the
        interval list open to the end of the shift, which is exactly how long
        they last.
        """
        permanently_short = any(
            r.state.is_absent and r.state is not State.NOT_SCHEDULED for r in self.riders
        )
        shift_end = self.deadline + timedelta(hours=self.shift_hours)

        if permanently_short:
            horizon = shift_end
        elif recovered_by is None or recovered_by <= self.deadline:
            return []
        else:
            horizon = min(recovered_by, shift_end)

        intervals: list[tuple[float, float]] = []
        cursor = self.deadline
        while cursor < horizon and len(intervals) < 16:
            interval_end = cursor + timedelta(minutes=30)
            # Who is on the floor for the bulk of this interval. Absent riders
            # have no ETA and are never counted, which is the correct answer
            # for every interval rather than a reason to skip the interval.
            midpoint = cursor + timedelta(minutes=15)
            present = sum(
                1 for r in self.riders
                if r.on_floor
                or (r.projection.eta is not None and r.projection.eta <= midpoint)
            )
            overlap = (min(interval_end, horizon) - cursor).total_seconds() / 1800.0
            intervals.append((float(present), max(0.0, min(1.0, overlap))))
            cursor = interval_end
        return intervals

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue": self.queue,
            "display_name": self.display_name,
            "deadline": self.deadline.isoformat(),
            "rostered": self.rostered,
            "scheduled": self.scheduled,
            "on_floor": len(self.on_floor),
            "in_transit": len(self.in_transit),
            "at_risk": len(self.at_risk),
            "absent": len(self.absent),
            "projected": self.projected,
            "coverage_pct": round(self.coverage * 100),
            "impact": self.impact().as_dict(),
            "riders": [r.as_dict() for r in sorted(
                self.riders,
                key=lambda r: (r.projection.eta or datetime.max, r.display_name),
            )],
        }


def status_for(leg: Mapping[str, Any], now: datetime, office: str, deadline: datetime) -> RiderStatus:
    """Resolve one rider's row into a status at `now`."""
    projection = project_arrival(leg, now, office, deadline)
    return RiderStatus(
        pickup_delay_min=pickup_delay_min(leg, now),
        stwid=leg["stwid"],
        display_name=leg["display_name"],
        queue=leg["queue"],
        state=rider_state(leg, now),
        projection=projection,
        vendor_id=leg.get("vendor_id"),
        trip_nodal=leg.get("trip_nodal"),
        minutes_late=projection.minutes_past(deadline),
    )


def project_queues(
    legs: Iterable[Mapping[str, Any]],
    queues: Sequence[Mapping[str, Any]],
    now: datetime,
    office: str,
    deadline: datetime,
) -> dict[str, QueueProjection]:
    """Build one projection per queue from today's roster rows.

    Args:
        legs: rows from `v_roster_day` for one date, primary role only.
        queues: rows from `queues`, carrying handle time and forecast.
        now: the replay clock.
        office: site, selects the travel-time history.
        deadline: shift start plus grace.
    """
    projections = {
        q["queue"]: QueueProjection(
            queue=q["queue"],
            display_name=q["display_name"],
            deadline=deadline,
            aht_min=float(q["aht_min"]),
            calls_per_30min=int(q["calls_per_30min"]),
            sl_target=q.get("sl_target") or "80/20",
        )
        for q in queues
    }

    for leg in legs:
        projection = projections.get(leg["queue"])
        if projection is None:
            continue
        projection.riders.append(status_for(leg, now, office, deadline))

    return projections


def floor_totals(projections: Mapping[str, QueueProjection]) -> dict[str, Any]:
    """Roll the queues up to one floor-level line for the top of the board."""
    queues = list(projections.values())
    rostered = sum(q.rostered for q in queues)
    projected = sum(q.projected for q in queues)
    return {
        "rostered": rostered,
        "on_floor": sum(len(q.on_floor) for q in queues),
        "in_transit": sum(len(q.in_transit) for q in queues),
        "at_risk": sum(len(q.at_risk) for q in queues),
        "absent": sum(len(q.absent) for q in queues),
        "projected": projected,
        "coverage_pct": round(100 * projected / rostered) if rostered else 100,
        "calls_unanswered": round(sum(q.impact().calls_unanswered for q in queues)),
        "calls_unanswered_high": round(sum(q.impact().calls_unanswered_high for q in queues)),
        "queues_breaching_sla": sum(
            1 for q in queues
            if q.impact().service_level and not q.impact().service_level.meets_target
        ),
        "day_sla_at_risk": sum(
            1 for q in queues if q.impact().day and not q.impact().day.meets_target
        ),
    }
