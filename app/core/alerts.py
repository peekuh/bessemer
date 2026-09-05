"""When to speak, what to say, and what to offer.

This is the module that decides whether the manager hears anything at all. It
is also the one most able to ruin the product: an alert that fires every
morning is noise, and noise gets muted. Everything here is built around not
spending the manager's attention until it is worth spending.

## What fires

Three triggers, checked every tick against the projections:

  coverage      a queue is projected below 75% at the deadline
  hard_late     someone is projected more than 15 minutes past it
  unaccounted   a rider's cab has come and gone without them

`unaccounted` is the one worth firing on even when the numbers look fine,
because it is the only condition a phone call still fixes.

## Cause, not blame

The cause is read from what is observable *now*, never from the trip's
`delay_reason` column. That column is filled in after the fact, and on this
shift 70% of arrivals that miss the shift start are stamped `NODELAY`. By the
transport operator's own measure nothing went wrong. The floor is short
regardless. That gap between two true statements is the entire reason this
system exists, so the alert is built to sense the floor rather than to
re-read the operator's homework.

## One alert per queue per shift

Twelve riders arriving late is one situation, not twelve. The alert opens once,
updates in place as riders land or slip, and resolves itself when the queue
comes back to strength. A manager should be able to keep one card open and
watch it settle.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Sequence

from app.config import (
    HANDOVER_REQUIRED,
    LATE_ALERT_MIN,
    STAFFING_FLOOR,
)
from app.core.context import Fact, context_facts
from app.core.eta import Risk
from app.core.queue import QueueProjection, RiderStatus
from app.core.sla import ServiceLevel, service_level
from app.core.remediation import (
    Candidate,
    HoldOver,
    candidates,
    contactable,
    hold_over_cost,
    night_agents,
)
from app.core.state import State

MIN_RENARRATE_MIN = 15
"""Minimum minutes between narrative rewrites when only the severity band has
moved. Caps model spend on a queue oscillating around a band edge."""

RESOLVE_MARGIN = 0.05
"""Coverage must climb this far above the floor before an open alert closes.
Without it a queue hovering on the threshold opens and shuts on alternate
ticks, which reads as a system arguing with itself."""


class Cause(str, Enum):
    """Why the queue is short, judged from what is observable now."""

    CAB_NOT_STARTED = "CAB_NOT_STARTED"
    """Cabs are still sitting at their depots past their planned start. The
    earliest signal available, and squarely the operator's problem."""

    NO_PICKUP = "NO_PICKUP"
    """Riders were not collected. Either they were not there or the cab did
    not stop. Needs a phone call, not patience."""

    EN_ROUTE_DELAY = "EN_ROUTE_DELAY"
    """Riders are aboard and running behind. Nothing to do but plan around it."""

    LATE_PICKUP = "LATE_PICKUP"
    """Collected late enough that the arrival was lost before the journey began."""

    ABSENCE = "ABSENCE"
    """Confirmed absences, cancellations included. A rostering gap, not a
    transport one."""

    MIXED = "MIXED"
    """No single cause dominates. Said plainly rather than picking a
    scapegoat."""


class Status(str, Enum):
    OPEN = "OPEN"
    UPDATED = "UPDATED"
    RESOLVED = "RESOLVED"


class Pathway(str, Enum):
    """What the manager can do about it. A closed set, deliberately."""

    WAIT = "WAIT"
    HOLD_OVER = "HOLD_OVER"
    EARLY_SHIFT_COVER = "EARLY_SHIFT_COVER"
    CROSS_COVER = "CROSS_COVER"
    CONTACT_EMPLOYEE = "CONTACT_EMPLOYEE"
    ESCALATE_TRANSPORT = "ESCALATE_TRANSPORT"
    ESCALATE_OPS = "ESCALATE_OPS"


PATHWAY_LABELS = {
    Pathway.WAIT: "Hold and watch",
    Pathway.HOLD_OVER: "Keep the night shift on",
    Pathway.EARLY_SHIFT_COVER: "Move the early shift onto the queue",
    Pathway.CROSS_COVER: "Borrow from the other queue",
    Pathway.CONTACT_EMPLOYEE: "Call the unaccounted riders",
    Pathway.ESCALATE_TRANSPORT: "Escalate to transport",
    Pathway.ESCALATE_OPS: "Flag the day's service level to operations",
}


CROSS_QUEUE_AHT_PENALTY = 1.3
"""Handle time multiplier for an agent working a queue they are not primary
on. Borrowed skill is slower skill, and pretending otherwise would make
cross-cover look free when it is not."""


@dataclass
class Option:
    """One course of action, priced, named, and scored on the contract.

    Every option carries the service level it would produce, so the choice in
    front of the manager reads as a comparison rather than a list. That is the
    difference between "here are three things you could do" and "doing nothing
    holds the queue at 15%, moving three people takes it to 88%".
    """

    pathway: Pathway
    label: str
    rationale: str
    people: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, Any] | None = None
    recommended: bool = False
    urgent: bool = False
    """Must happen regardless of which staffing option is chosen. Escalating a
    lost day is not an alternative to covering the queue; it is in addition."""
    agents_after: float | None = None
    service_level: ServiceLevel | None = None

    @property
    def outcome(self) -> str:
        """One phrase naming what this choice buys."""
        if self.service_level is None:
            return ""
        return self.service_level.headline

    def as_dict(self) -> dict[str, Any]:
        return {
            "pathway": self.pathway.value,
            "label": self.label,
            "rationale": self.rationale,
            "people": self.people,
            "cost": self.cost,
            "recommended": self.recommended,
            "urgent": self.urgent,
            "agents_after": self.agents_after,
            "service_level": self.service_level.as_dict() if self.service_level else None,
            "outcome": self.outcome,
        }


@dataclass
class Alert:
    """One queue's situation for one shift, from first warning to resolution."""

    queue: str
    display_name: str
    office: str
    business_unit: str
    shift_date: date
    shift_type: str
    status: Status
    cause: Cause
    opened_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    coverage_pct: int = 100
    riders_affected: list[dict[str, Any]] = field(default_factory=list)
    impact: dict[str, Any] = field(default_factory=dict)
    hold_over: dict[str, Any] | None = None
    facts: list[Fact] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)

    narrative: str | None = None
    drafts: dict[str, str] = field(default_factory=dict)
    narrated_at: datetime | None = None
    narrated_signature: tuple[str, str | None, int] | None = None
    narrated_resolved: bool = False

    @property
    def key(self) -> tuple[str, date, str, str]:
        return (self.office, self.shift_date, self.shift_type, self.queue)

    @property
    def affected_ids(self) -> tuple[int, ...]:
        return tuple(sorted(r["stwid"] for r in self.riders_affected))

    def for_narrative(self) -> dict[str, Any]:
        """The compact form the language model sees.

        The full payload is around 2,400 tokens, most of it detail the board
        needs and a five-sentence summary does not: eleven riders with eleven
        fields each, every option with every candidate's full record. Sent
        through a four-turn tool loop that came to 18,700 prompt tokens for one
        alert, which is not a cost anybody would run at enterprise volume.

        This carries the same facts at roughly a quarter of the size. Two
        principles decide what stays. Anything the model might otherwise invent
        is included, because a missing number is a number it will estimate.
        Anything it cannot use in a sentence is dropped.

        Context facts and cover candidates are folded in here rather than left
        to separate tools. They were always going to be fetched, and fetching
        them separately meant three extra round trips carrying the whole
        conversation each time.
        """
        impact = self.impact or {}
        sl = impact.get("service_level") or {}
        sl_full = impact.get("service_level_full") or {}
        day = impact.get("day") or {}

        return {
            "alert_id": None,  # filled in by the tool, which knows the row id
            "queue": self.queue,
            "queue_name": self.display_name,
            "office": self.office,
            "shift": f"{self.shift_type} on {self.shift_date}",
            "status": self.status.value,
            "cause": self.cause.value,
            "coverage_pct": self.coverage_pct,
            "agents_missing": impact.get("agents_missing"),
            "agents_needed_for_target": impact.get("agents_needed"),
            "recovered_by": (impact.get("recovered_by") or "")[11:16] or None,
            "service_level_now_pct": sl.get("service_level_pct"),
            "service_level_full_strength_pct": sl_full.get("service_level_pct"),
            "service_level_target_pct": sl.get("target_pct"),
            "answer_delay_seconds": sl.get("asa_seconds"),
            "day_service_level_pct": day.get("day_service_level_pct"),
            "day_target_holds": day.get("meets_target"),
            "day_recoverable": day.get("recoverable"),
            "if_nobody_acts": (self.hold_over or {}).get("summary"),
            "overtime_cost": (self.hold_over or {}).get("cost"),
            # The riders a sentence could plausibly name, not all of them.
            "worst_affected": [
                {
                    "name": r["name"],
                    "state": r["state"],
                    "eta": (r["eta"] or "")[11:16] or None,
                    "vendor": r["vendor"],
                }
                for r in self.riders_affected[:4]
            ],
            "affected_total": len(self.riders_affected),
            "context": [f.text for f in self.facts[:3]],
            "options": [
                {
                    "pathway": o.pathway.value,
                    "recommended": o.recommended,
                    "outcome": o.outcome or None,
                    "why": o.rationale,
                    "people": [
                        p.get("name") or p.get("vendor") or p.get("role")
                        for p in o.people[:4]
                    ],
                }
                for o in self.options
            ],
        }

    def payload(self) -> dict[str, Any]:
        """The structured form. Everything downstream reads this and only this.

        The narrative layer receives exactly these fields, so the language
        model never sees the database and cannot invent a name or a time that
        is not already here. If the model is unavailable the UI renders this
        directly as a table, which is why the alert stays useful without it.
        """
        return {
            "queue": self.queue,
            "queue_name": self.display_name,
            "office": self.office,
            "business_unit": self.business_unit,
            "shift_date": self.shift_date.isoformat(),
            "shift_type": self.shift_type,
            "status": self.status.value,
            "cause": self.cause.value,
            "coverage_pct": self.coverage_pct,
            "triggers": self.triggers,
            "riders_affected": self.riders_affected,
            "impact": self.impact,
            "hold_over": self.hold_over,
            "context": [f.as_dict() for f in self.facts],
            "options": [o.as_dict() for o in self.options],
        }

    @property
    def coverage_band(self) -> int:
        """Coverage rounded into 20-point bands: 100, 80, 60, 40, 20, 0."""
        return (self.coverage_pct // 20) * 20

    @property
    def recommended_pathway(self) -> str | None:
        return next((o.pathway.value for o in self.options if o.recommended), None)

    def payload_hash(self) -> str:
        """Fingerprint of what a written summary would actually say.

        Used to look up a cached narrative, so the same situation on a later
        day or a re-run of the demo costs nothing.
        """
        material = {
            "queue": self.queue,
            "cause": self.cause.value,
            "coverage_band": self.coverage_band,
            "options": sorted(o.pathway.value for o in self.options),
            "recommended": self.recommended_pathway,
        }
        blob = json.dumps(material, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def needs_narrative(self, now: datetime) -> bool:
        """Whether this tick is worth paying a language model for.

        Content hashing turned out to be the wrong instrument here. Hashing the
        payload, however coarsely, still tracked things that drift on every
        tick: the affected count falls by one each time somebody walks in, and
        the three names a summary would mention reshuffle as ETAs move. Across
        one morning that produced 44 distinct payloads across two queues, which
        would have meant 44 model calls to keep saying much the same thing.

        So the decision is made explicitly instead. Prose is rewritten when the
        *situation* changes, not when a number does:

          * the alert has never been narrated
          * it has just resolved, which deserves a closing line
          * the cause changed, so the explanation is now wrong
          * the recommended action changed, so the advice is now wrong
          * coverage crossed a 20-point band, and it has been at least
            `MIN_RENARRATE_MIN` since the last rewrite

        The interval is what makes the cost predictable. Without it a queue
        oscillating around a band edge could still trigger a rewrite every
        minute. With it, a bad morning costs a handful of calls per queue and
        a normal one costs none at all.
        """
        if self.narrative is None:
            return True
        if self.status is Status.RESOLVED and not self.narrated_resolved:
            return True

        signature = (self.cause.value, self.recommended_pathway, self.coverage_band)
        if signature == self.narrated_signature:
            return False

        # Cause or advice going stale is worth immediate prose. A band change
        # on its own can wait for the interval.
        cause_or_advice_changed = signature[:2] != (self.narrated_signature or (None, None, None))[:2]
        if cause_or_advice_changed:
            return True

        if self.narrated_at is None:
            return True
        return now - self.narrated_at >= timedelta(minutes=MIN_RENARRATE_MIN)

    def mark_narrated(self, now: datetime) -> None:
        """Record that prose was produced for this situation."""
        self.narrated_at = now
        self.narrated_signature = (
            self.cause.value,
            self.recommended_pathway,
            self.coverage_band,
        )
        if self.status is Status.RESOLVED:
            self.narrated_resolved = True

    def as_dict(self) -> dict[str, Any]:
        return self.payload() | {
            "opened_at": self.opened_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "narrative": self.narrative,
            "drafts": self.drafts,
            "payload_hash": self.payload_hash(),
        }


# ------------------------------------------------------------------ triggers


def evaluate_triggers(projection: QueueProjection, resolving: bool = False) -> list[str]:
    """Which conditions, if any, justify interrupting the manager.

    Every trigger asks about people who are **still out**. Someone who walked
    in twenty minutes late is a fact for the record, not a reason to keep an
    alert lit: their seat is filled and there is nothing left to decide. An
    early version counted them and left every alert permanently open, which is
    exactly how a manager learns to ignore the panel.

    Args:
        projection: the queue at this instant.
        resolving: apply the hysteresis margin. Coverage has to climb a little
            above the floor before an open alert closes, so a queue sitting
            exactly on the threshold does not open and close on alternate
            ticks.
    """
    fired: list[str] = []
    floor = STAFFING_FLOOR + (RESOLVE_MARGIN if resolving else 0.0)

    if projection.coverage < floor:
        pct = round(projection.coverage * 100)
        if pct < round(STAFFING_FLOOR * 100):
            fired.append(f"coverage {pct}% below the {round(STAFFING_FLOOR * 100)}% floor")
        else:
            fired.append(
                f"coverage {pct}% has not recovered past {round(floor * 100)}%"
            )

    still_out = [r for r in projection.riders if not r.on_floor]

    badly_late = [
        r for r in still_out
        if r.minutes_late is not None and r.minutes_late > LATE_ALERT_MIN
    ]
    if badly_late:
        worst = max(r.minutes_late for r in badly_late)
        fired.append(
            f"{len(badly_late)} still out and projected more than "
            f"{LATE_ALERT_MIN} min late, worst {round(worst)} min"
        )

    unaccounted = [r for r in still_out if r.state in {State.NO_PICKUP, State.NO_SHOW}]
    if unaccounted:
        fired.append(f"{len(unaccounted)} unaccounted for after their cab passed")

    return fired


LATE_PICKUP_MIN = 10.0
"""A pickup this far behind plan is the cause of a late arrival, not the road."""


def classify(projection: QueueProjection) -> Cause:
    """Name the dominant cause from observable state, never from `delay_reason`.

    The progression for one late cab reads: CAB_NOT_STARTED while it sits at
    the depot, LATE_PICKUP once it is moving or has collected people well
    behind plan, EN_ROUTE_DELAY only when the pickup was on time and the
    journey itself ran long. Each names a different owner.
    """
    affected = [r for r in projection.riders if r.is_affected]
    if not affected:
        return Cause.MIXED

    tally: dict[Cause, int] = {}
    for rider in affected:
        if rider.state in {State.NO_SHOW, State.CANCELLED}:
            cause = Cause.ABSENCE
        elif rider.state is State.NO_PICKUP:
            cause = Cause.NO_PICKUP
        elif rider.state is State.CAB_LATE:
            cause = Cause.CAB_NOT_STARTED
        elif rider.state is State.CAB_MOVING:
            # The cab is running behind before it has reached anyone.
            cause = Cause.LATE_PICKUP
        elif rider.state is State.PICKED_UP:
            late_kerb = rider.pickup_delay_min > LATE_PICKUP_MIN
            cause = Cause.LATE_PICKUP if late_kerb else Cause.EN_ROUTE_DELAY
        else:
            cause = Cause.EN_ROUTE_DELAY
        tally[cause] = tally.get(cause, 0) + 1

    top, count = max(tally.items(), key=lambda kv: kv[1])
    # A cause that explains less than half of the affected riders is not really
    # the cause. Saying "mixed" is more useful than naming a plurality.
    return top if count >= len(affected) / 2 else Cause.MIXED


# ------------------------------------------------------------------ pathways


def build_options(
    projection: QueueProjection,
    cause: Cause,
    on: date,
    now: datetime,
    office: str,
    shift_start: datetime,
    hold: HoldOver,
) -> list[Option]:
    """Assemble the courses of action, priced, with the best one marked.

    The cause selects which options are relevant; the size of the gap decides
    whether any of them are worth offering. Options are always returned in the
    same order so the manager's eye learns where to look.
    """
    impact = projection.impact()
    gap = impact.agents_missing
    options: list[Option] = []

    def sl_at(agents: float, aht: float | None = None) -> ServiceLevel:
        """Service level this queue would run at with `agents` on it."""
        return service_level(
            agents,
            projection.calls_per_30min,
            aht or projection.aht_min,
            30.0,
            projection.sl_target,
        )

    if gap == 0:
        return [
            Option(
                pathway=Pathway.WAIT,
                label=PATHWAY_LABELS[Pathway.WAIT],
                rationale="Queue is projected to reach strength without intervention.",
                agents_after=float(projection.projected),
                service_level=sl_at(projection.projected),
            )
        ]

    # Doing nothing is not free under positional handover. Listing it first,
    # with its price, is what makes the other options legible as choices.
    if HANDOVER_REQUIRED and hold.agents_held:
        options.append(
            Option(
                pathway=Pathway.HOLD_OVER,
                label=PATHWAY_LABELS[Pathway.HOLD_OVER],
                rationale=(
                    f"Positions stay manned, but {hold.summary}. "
                    f"This is what happens by default if nothing else is decided."
                ),
                people=night_agents(projection.queue, limit=gap),
                cost=hold.as_dict(),
                agents_after=float(projection.rostered),
                service_level=sl_at(projection.rostered),
            )
        )

    if not (HANDOVER_REQUIRED and hold.agents_held):
        # No handover obligation, so the seats simply stay empty. Still worth
        # stating as a choice, with its price on the contract.
        options.append(
            Option(
                pathway=Pathway.WAIT,
                label=PATHWAY_LABELS[Pathway.WAIT],
                rationale="Leave the queue short and absorb the wait times.",
                agents_after=float(projection.projected),
                service_level=sl_at(projection.projected),
            )
        )

    pool = candidates(projection.queue, on, now, office, limit=gap, allow_cross_queue=True)
    same_queue = [c for c in pool if c.same_queue]
    cross_queue = [c for c in pool if not c.same_queue]

    if same_queue:
        options.append(
            Option(
                pathway=Pathway.EARLY_SHIFT_COVER,
                label=PATHWAY_LABELS[Pathway.EARLY_SHIFT_COVER],
                rationale=(
                    f"{len(same_queue)} early-shift agent"
                    f"{'s' if len(same_queue) > 1 else ''} already on the floor and "
                    f"trained on this queue. The night shift goes home on time."
                ),
                people=[c.as_dict() for c in same_queue],
                recommended=True,
                agents_after=float(projection.projected + len(same_queue)),
                service_level=sl_at(projection.projected + len(same_queue)),
            )
        )

    if cross_queue and len(same_queue) < gap:
        options.append(
            Option(
                pathway=Pathway.CROSS_COVER,
                label=PATHWAY_LABELS[Pathway.CROSS_COVER],
                rationale=(
                    f"{projection.queue} cannot field enough of its own. "
                    f"Borrowing costs handling speed on both queues."
                ),
                people=[c.as_dict() for c in cross_queue],
                agents_after=float(projection.projected + len(cross_queue)),
                service_level=sl_at(
                    projection.projected + len(cross_queue),
                    projection.aht_min * CROSS_QUEUE_AHT_PENALTY,
                ),
            )
        )

    unaccounted = contactable(projection.riders)
    if unaccounted:
        options.append(
            Option(
                pathway=Pathway.CONTACT_EMPLOYEE,
                label=PATHWAY_LABELS[Pathway.CONTACT_EMPLOYEE],
                rationale=(
                    "Their cab has passed without them. A call now is the only "
                    "thing that still changes the outcome."
                ),
                people=unaccounted,
                recommended=cause is Cause.NO_PICKUP,
            )
        )

    vendors = _repeat_vendors(projection.riders)
    if vendors:
        vendor, affected = vendors[0]
        options.append(
            Option(
                pathway=Pathway.ESCALATE_TRANSPORT,
                label=PATHWAY_LABELS[Pathway.ESCALATE_TRANSPORT],
                rationale=(
                    f"{affected} of the affected riders are on {vendor}. "
                    f"That is a pattern for the transport manager, not this shift."
                ),
                people=[{"vendor": vendor, "riders_affected": affected}],
            )
        )

    # The reporting-upward trigger. A half-hour dip is a floor problem the
    # manager handles themselves; a day that will miss its contracted service
    # level is their director's problem too, and they should not find out
    # about it at five o'clock. This is the only option that leaves the shift.
    day = impact.day
    if day and not day.meets_target:
        options.append(
            Option(
                pathway=Pathway.ESCALATE_OPS,
                label=PATHWAY_LABELS[Pathway.ESCALATE_OPS],
                rationale=(
                    f"{day.headline}. "
                    + (
                        "A clean remainder still recovers it."
                        if day.recoverable
                        else "The day cannot be recovered even with a perfect afternoon."
                    )
                    + " Operations should hear this now, not in the evening report."
                ),
                people=[{"role": "operations head", "reason": day.headline}],
                # Not an alternative to fixing the floor, so never the single
                # recommended action. Urgent instead: it happens as well.
                urgent=True,
            )
        )

    if not any(o.recommended for o in options) and options:
        # No staffing fix is available yet, typically because nobody from the
        # early shift is in the building. The actionable move is then the one
        # that goes after the cause, not the one that absorbs it.
        preferred = next((o for o in options if o.pathway is Pathway.ESCALATE_TRANSPORT), None)
        (preferred or options[0]).recommended = True
    return options


def _repeat_vendors(riders: Sequence[RiderStatus], minimum: int = 2) -> list[tuple[str, int]]:
    """Vendors carrying more than one affected rider, worst first.

    One late cab is weather. Three from the same operator on one morning is a
    conversation the transport manager should be having, and the line manager
    should not have to spot it themselves.
    """
    tally: dict[str, int] = {}
    for rider in riders:
        # Only transport-caused trouble counts against a vendor. A rider who
        # was not at their stop, or who cancelled, is not the cab's fault.
        if rider.vendor_id and rider.is_affected and not rider.state.is_absent:
            tally[rider.vendor_id] = tally.get(rider.vendor_id, 0) + 1
    return sorted(
        ((v, n) for v, n in tally.items() if n >= minimum),
        key=lambda kv: kv[1],
        reverse=True,
    )


# ------------------------------------------------------------------ lifecycle


def build_alert(
    projection: QueueProjection,
    now: datetime,
    on: date,
    office: str,
    business_unit: str,
    shift_type: str,
    shift_start: datetime,
    existing: Alert | None = None,
) -> Alert | None:
    """Produce the alert for one queue at one instant, or None if nothing to say.

    Returns an existing alert marked RESOLVED once the queue recovers, so a
    card the manager is watching closes itself rather than lingering.
    """
    # A resolved alert stays resolved for the rest of the shift. The morning
    # settles once; reopening the same card after the queue has recovered
    # would tell the manager something has gone wrong again when nothing has.
    if existing and existing.status is Status.RESOLVED:
        return existing

    triggers = evaluate_triggers(projection, resolving=existing is not None)

    if not triggers:
        if existing:
            existing.status = Status.RESOLVED
            existing.resolved_at = now
            existing.updated_at = now
            existing.coverage_pct = round(projection.coverage * 100)
            existing.riders_affected = []
            existing.triggers = []
            existing.options = [
                Option(
                    pathway=Pathway.WAIT,
                    label=PATHWAY_LABELS[Pathway.WAIT],
                    rationale="Queue back to strength. Nothing outstanding.",
                )
            ]
            return existing
        return None

    impact = projection.impact()
    hold = hold_over_cost(
        projection.queue, on, shift_start, impact.agents_missing, impact.recovered_by
    )
    cause = classify(projection)
    options = build_options(projection, cause, on, now, office, shift_start, hold)

    affected = [
        r.as_dict()
        for r in sorted(
            (r for r in projection.riders if r.is_affected),
            key=lambda r: (r.projection.eta or datetime.max, r.display_name),
        )
    ]
    # Count everyone heading for a late arrival, not only those who have
    # already walked in late. Counting arrivals alone made an alert about 67%
    # coverage carry the line "4 late today, better than usual", because most
    # of the riders it was worried about had not landed yet.
    late_count = sum(
        1 for r in projection.riders
        if r.state is not State.NOT_SCHEDULED
        and r.minutes_late is not None
        and r.minutes_late > 0
    ) + sum(
        1 for r in projection.riders
        if r.state in {State.NO_SHOW, State.NO_PICKUP}
    )
    facts = context_facts(office, shift_type, on, projection.queue, late_today=late_count)

    if existing is None:
        return Alert(
            queue=projection.queue,
            display_name=projection.display_name,
            office=office,
            business_unit=business_unit,
            shift_date=on,
            shift_type=shift_type,
            status=Status.OPEN,
            cause=cause,
            opened_at=now,
            updated_at=now,
            coverage_pct=round(projection.coverage * 100),
            riders_affected=affected,
            impact=impact.as_dict(),
            hold_over=hold.as_dict(),
            facts=facts,
            options=options,
            triggers=triggers,
        )

    existing.status = Status.UPDATED if existing.status is not Status.OPEN else Status.OPEN
    existing.cause = cause
    existing.updated_at = now
    existing.resolved_at = None
    existing.coverage_pct = round(projection.coverage * 100)
    existing.riders_affected = affected
    existing.impact = impact.as_dict()
    existing.hold_over = hold.as_dict()
    existing.facts = facts
    existing.options = options
    existing.triggers = triggers
    return existing
