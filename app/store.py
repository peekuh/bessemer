"""Persisting alerts and the actions taken on them.

The reasoning core is deliberately stateless: give it a roster and a clock and
it tells you the situation. That makes it testable but it also means nothing
survives the process, and three things have to.

**The narrative cache.** Prose is the only expensive part of the system. Keying
it on the situation rather than the moment means a re-run of the demo, or the
same shape of morning next Tuesday, costs nothing.

**The audit trail.** A manager who acted on an alert at ten to nine needs to be
able to show what they were told and what they chose. `alert_actions` is that
record, and it is also what makes the cover-fairness counter honest.

**The handover.** An alert opened by the replay and answered by the agent are
two different processes touching the same row. Postgres is the meeting point.

Everything here is scoped by business unit and office. That is not decoration:
it is the multi-tenancy claim, enforced at the only layer where it can actually
be enforced.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from app.core.alerts import Alert, Cause, Status
from app.db import connect, query, query_one

UPSERT_ALERT = """
INSERT INTO shift_alerts (
    business_unit, office, shift_date, shift_type, queue,
    status, cause, payload, payload_hash, narrative, drafts,
    opened_at, updated_at, resolved_at
) VALUES (
    %(business_unit)s, %(office)s, %(shift_date)s, %(shift_type)s, %(queue)s,
    %(status)s, %(cause)s, %(payload)s, %(payload_hash)s, %(narrative)s, %(drafts)s,
    %(opened_at)s, %(updated_at)s, %(resolved_at)s
)
ON CONFLICT (office, shift_date, shift_type, queue) DO UPDATE SET
    status       = EXCLUDED.status,
    cause        = EXCLUDED.cause,
    payload      = EXCLUDED.payload,
    payload_hash = EXCLUDED.payload_hash,
    updated_at   = EXCLUDED.updated_at,
    resolved_at  = EXCLUDED.resolved_at,
    -- Never overwrite prose with nothing. A tick that did not warrant a
    -- rewrite must leave the existing narrative in place, or the board would
    -- flicker back to a bare table between rewrites.
    narrative    = COALESCE(EXCLUDED.narrative, shift_alerts.narrative),
    drafts       = COALESCE(EXCLUDED.drafts, shift_alerts.drafts)
RETURNING id
"""


def save_alert(alert: Alert) -> int:
    """Write an alert, returning its row id. Idempotent per queue per shift."""
    row = query_one(
        UPSERT_ALERT,
        {
            "business_unit": alert.business_unit,
            "office": alert.office,
            "shift_date": alert.shift_date,
            "shift_type": alert.shift_type,
            "queue": alert.queue,
            "status": alert.status.value,
            "cause": alert.cause.value,
            "payload": json.dumps(alert.payload(), default=str),
            "payload_hash": alert.payload_hash(),
            "narrative": alert.narrative,
            "drafts": json.dumps(alert.drafts) if alert.drafts else None,
            "opened_at": alert.opened_at,
            "updated_at": alert.updated_at,
            "resolved_at": alert.resolved_at,
        },
    )
    return row["id"]


def load_alerts(office: str, shift_date: date, shift_type: str) -> list[dict[str, Any]]:
    """Every alert for one shift, newest activity first."""
    return query(
        """
        SELECT id, queue, status, cause, payload, payload_hash, narrative, drafts,
               opened_at, updated_at, resolved_at
        FROM shift_alerts
        WHERE office = %s AND shift_date = %s AND shift_type = %s
        ORDER BY (status = 'RESOLVED'), opened_at
        """,
        (office, shift_date, shift_type),
    )


def find_cached_narrative(payload_hash: str) -> dict[str, Any] | None:
    """Reuse prose written for an identical situation.

    Keyed by the situation rather than the shift. Two mornings that produce
    the same cause, the same severity band and the same recommendation deserve
    the same sentence, and the second one should be free. Kept in its own
    table so that resetting a shift for a demo re-run does not throw it away;
    an earlier version read it back out of `shift_alerts`, which meant every
    Start over paid for every write-up again.
    """
    return query_one(
        "SELECT narrative, drafts FROM narrative_cache WHERE payload_hash = %s",
        (payload_hash,),
    )


def remember_narrative(payload_hash: str, narrative: str, drafts: dict[str, str]) -> None:
    """Memoise prose against the situation that produced it."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO narrative_cache (payload_hash, narrative, drafts)
            VALUES (%s, %s, %s)
            ON CONFLICT (payload_hash) DO UPDATE SET
                narrative = EXCLUDED.narrative,
                drafts = EXCLUDED.drafts,
                written_at = now()
            """,
            (payload_hash, narrative, json.dumps(drafts)),
        )
        conn.commit()


def forget_narratives() -> int:
    """Empty the memo. Only for tests and for forcing fresh prose."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM narrative_cache")
        n = cur.rowcount
        conn.commit()
        return n


def save_narrative(alert_id: int, narrative: str, drafts: dict[str, str]) -> None:
    """Attach prose to an alert row."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE shift_alerts SET narrative = %s, drafts = %s WHERE id = %s",
            (narrative, json.dumps(drafts), alert_id),
        )
        conn.commit()


def record_action(
    alert_id: int,
    pathway: str,
    draft: str | None,
    people: list[dict[str, Any]],
    at: datetime,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Log what the manager chose, and charge any cover minutes to whoever
    absorbed them.

    The cover counter is what stops the same obliging person being asked every
    morning. It only stays honest if it is written at the moment of the
    decision, which is here.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alert_actions (alert_id, pathway, draft, candidates, sent_at, cost)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, alert_id, pathway, draft, candidates, sent_at, cost
            """,
            (
                alert_id,
                pathway,
                draft,
                json.dumps(people, default=str),
                at,
                json.dumps(cost, default=str) if cost else None,
            ),
        )
        action = cur.fetchone()
        conn.commit()
        return action


def actions_for(alert_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Everything already done about these alerts, so the board can show it."""
    if not alert_ids:
        return {}
    rows = query(
        """
        SELECT id, alert_id, pathway, draft, candidates, sent_at, cost
        FROM alert_actions
        WHERE alert_id = ANY(%s)
        ORDER BY sent_at
        """,
        (alert_ids,),
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["alert_id"], []).append(row)
    return grouped


def actions_for_shift(office: str, shift_date: date, shift_type: str) -> dict[str, list[dict[str, Any]]]:
    """Every action taken on this shift, grouped by queue.

    Keyed by queue rather than alert id because one alert exists per queue per
    shift and the queue survives a reset or a restart, while the row id does
    not. Playback merges actions by this key.
    """
    rows = query(
        """
        SELECT a.id, a.alert_id, s.queue, a.pathway, a.draft, a.candidates, a.sent_at, a.cost
        FROM alert_actions a JOIN shift_alerts s ON s.id = a.alert_id
        WHERE s.office = %s AND s.shift_date = %s AND s.shift_type = %s
        ORDER BY a.sent_at
        """,
        (office, shift_date, shift_type),
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["queue"], []).append(row)
    return grouped


def reset_shift(office: str, shift_date: date, shift_type: str) -> int:
    """Clear one shift's alerts and actions so the demo can be run again.

    Scoped tightly on purpose. A reset that took the whole table with it would
    be one mis-typed argument away from deleting another tenant's audit trail.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM alert_actions
            WHERE alert_id IN (
                SELECT id FROM shift_alerts
                WHERE office = %s AND shift_date = %s AND shift_type = %s
            )
            """,
            (office, shift_date, shift_type),
        )
        cur.execute(
            """
            DELETE FROM shift_alerts
            WHERE office = %s AND shift_date = %s AND shift_type = %s
            """,
            (office, shift_date, shift_type),
        )
        removed = cur.rowcount
        conn.commit()
        return removed


def clear_cover_log(shift_date: date) -> None:
    """Reset the fairness counter for the demo week."""
    from app.core.remediation import iso_week

    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM cover_log WHERE iso_week = %s", (iso_week(shift_date),))
        conn.commit()
