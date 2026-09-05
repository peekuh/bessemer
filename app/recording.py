"""The last live run, captured tick by tick so it can be played back.

Live mode computes and calls the model for real. Every tick it takes is
captured here, narratives included, and saved to Postgres when the run pauses
or ends. Playback mode reads this and nothing else: no compute, no model, and
it survives a restart before the demo.

Landmarks are read off the recorded feed, not hand-placed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any

from app.db import connect, query_one


@dataclass
class Recording:
    key: str
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    saved_at: datetime | None = None

    @property
    def keys(self) -> list[str]:
        return sorted(self.snapshots)

    @property
    def ready(self) -> bool:
        return bool(self.snapshots)

    def at(self, hhmm: str) -> dict[str, Any] | None:
        """The snapshot at or just before a minute."""
        if not self.snapshots:
            return None
        if hhmm in self.snapshots:
            return self.snapshots[hhmm]
        earlier = [k for k in self.keys if k <= hhmm]
        return self.snapshots[earlier[-1]] if earlier else self.snapshots[self.keys[0]]

    def landmarks(self, shift_start: datetime, deadline: datetime) -> list[dict[str, Any]]:
        if not self.snapshots:
            return []
        last = self.snapshots[self.keys[-1]]
        return derive_landmarks(last["events"], shift_start, deadline, self._everyone_in())

    def _everyone_in(self) -> str | None:
        for key in self.keys:
            t = self.snapshots[key]["board"]["totals"]
            if t["on_floor"] == t["rostered"]:
                return self.snapshots[key]["clock"]
        return None

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "ticks": len(self.snapshots),
            "first": self.keys[0] if self.keys else None,
            "last": self.keys[-1] if self.keys else None,
            "saved_at": self.saved_at.isoformat() if self.saved_at else None,
        }


def capture(session) -> None:
    """Snapshot the session's current tick into its recording."""
    replay = session.replay
    if session.recording is None or session.recording_stale:
        session.recording = Recording(key=session.key)
        session.recording_stale = False
    session.recording.snapshots[replay.now.strftime("%H:%M")] = {
        "clock": replay.now.isoformat(),
        "time": replay.now.strftime("%H:%M"),
        "board": replay.board(),
        "alerts": [
            alert.as_dict() | {"id": session.id_for(queue)}
            for queue, alert in replay.alerts.items()
        ],
        "events": [e.as_dict() for e in replay.events],
    }


def save(session) -> None:
    rec = session.recording
    if rec is None or not rec.snapshots:
        return
    r = session.replay
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recordings (key, business_unit, office, shift_date, shift_type, saved_at, payload)
            VALUES (%s, %s, %s, %s, %s, now(), %s)
            ON CONFLICT (key) DO UPDATE SET saved_at = now(), payload = EXCLUDED.payload
            """,
            (rec.key, r.business_unit, r.office, r.shift_date, r.shift_type,
             json.dumps(rec.snapshots, default=str)),
        )
        conn.commit()
    rec.saved_at = datetime.now()


def load(session) -> Recording | None:
    row = query_one("SELECT saved_at, payload FROM recordings WHERE key = %s", (session.key,))
    if not row:
        return None
    rec = Recording(key=session.key, snapshots=row["payload"], saved_at=row["saved_at"])
    session.recording = rec
    return rec


def forget(key: str, session=None) -> None:
    """Drop a recording by key, whether or not a session is holding it."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM recordings WHERE key = %s", (key,))
        conn.commit()
    if session is not None:
        session.recording = None


def derive_landmarks(
    events: list[dict[str, Any]], shift_start: datetime, deadline: datetime, everyone_in: str | None
) -> list[dict[str, Any]]:
    marks: list[tuple[str, str, str]] = []  # (clock iso, label, kind)
    seen_cab_late = seen_arrival = False
    for e in events:
        k = e["kind"]
        if k == "cab_late" and not seen_cab_late:
            marks.append((e["at"], f"First cab fails to leave: {e['subject']}", "warn")); seen_cab_late = True
        elif k == "alert_opened":
            marks.append((e["at"], f"{e['subject']} alert opens", "bad"))
        elif k in ("no_pickup", "no_show"):
            marks.append((e["at"], f"{e['subject']} not collected", "bad"))
        elif k == "arrived" and not seen_arrival:
            marks.append((e["at"], f"First arrival: {e['subject']}", "ok")); seen_arrival = True
        elif k == "alert_resolved":
            marks.append((e["at"], f"{e['subject']} back to strength", "ok"))
    marks.append((shift_start.isoformat(), "Shift starts", "mark"))
    marks.append((deadline.isoformat(), "Grace period ends", "mark"))
    if everyone_in:
        marks.append((everyone_in, "Everyone is in", "ok"))

    priority = {"bad": 0, "mark": 1, "ok": 2, "warn": 3}
    by_minute: dict[str, tuple[str, str, str]] = {}
    for m in sorted(marks, key=lambda m: (m[0], priority[m[2]])):
        by_minute.setdefault(m[0][11:16], m)
    return [
        {"at": m[0], "time": m[0][11:16], "label": m[1], "kind": m[2]}
        for m in sorted(by_minute.values())
    ]
