"""Where the replay clock lives between requests.

Pulled out of the API module so the agent's tools can reach the same running
replay without importing the web layer. Without this split, `api` imports
`agent` to answer chat and `agent` imports `api` to read the board, which is a
cycle Python will refuse at the least convenient moment.

The registry is process-local and deliberately so. Everything durable is in
Postgres: alerts, the actions taken on them, the narrative cache. What lives
here is only the cursor into a morning, which is cheap to rebuild and
meaningless to persist.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.alerts import Alert
from app.replay import Replay
from app import store


@dataclass
class Session:
    """One replay, its clock, and the speed it is running at."""

    replay: Replay
    speed: float = 60.0
    """Replayed minutes per real second. 60 puts a 2.5-hour morning in about
    two and a half minutes, which is roughly a demo's attention span."""

    running: bool = False
    started_wall: datetime | None = None
    started_clock: datetime | None = None
    alert_ids: dict[str, int] = field(default_factory=dict)
    task: asyncio.Task | None = None
    recording: Any = None
    """The last live run, for playback. See app.recording."""
    narrating: str | None = None
    """Queue whose alert the agent is writing right now, for the UI."""
    narrate: bool = True
    """Whether live runs call the model. Tests turn this off."""
    narrating_since: datetime | None = None
    recording_stale: bool = False
    """Set on reset. The old recording stays for playback until the next live
    capture, which then starts a fresh one rather than mixing two runs."""

    @property
    def key(self) -> str:
        return session_key(
            self.replay.business_unit,
            self.replay.office,
            self.replay.shift_date,
            self.replay.shift_type,
        )

    def persist(self) -> None:
        """Write every alert the replay is holding, remembering its row id."""
        for queue, alert in self.replay.alerts.items():
            self.alert_ids[queue] = store.save_alert(alert)

    def alert_for(self, alert_id: int) -> tuple[str, Alert] | None:
        for queue, known_id in self.alert_ids.items():
            if known_id == alert_id:
                return queue, self.replay.alerts[queue]
        return None

    def id_for(self, queue: str) -> int | None:
        return self.alert_ids.get(queue)


def session_key(bu: str, office: str, shift_date: date, shift_type: str) -> str:
    return f"{bu}|{office}|{shift_date.isoformat()}|{shift_type}"


SESSIONS: dict[str, Session] = {}


class SessionMissing(LookupError):
    """No replay is running for the requested shift."""


class RosterMissing(LookupError):
    """Nobody is rostered for the requested shift."""


def get_session(
    business_unit: str, office: str, shift_date: date, shift_type: str, create: bool = True
) -> Session:
    """Fetch the session for one tenant/office/shift, creating it if asked."""
    key = session_key(business_unit, office, shift_date, shift_type)
    session = SESSIONS.get(key)
    if session is None:
        if not create:
            raise SessionMissing(f"no replay running for {key}")
        replay = Replay(
            shift_date=shift_date,
            office=office,
            business_unit=business_unit,
            shift_type=shift_type,
        )
        if not replay.legs:
            raise RosterMissing(
                f"no roster rows for {office} {shift_type} on {shift_date}"
            )
        session = Session(replay=replay)
        SESSIONS[key] = session
        from app import recording as rec
        rec.load(session)
    return session


def drop_session(bu: str, office: str, shift_date: date, shift_type: str) -> Session | None:
    """Remove a session, cancelling its clock if it is running."""
    session = SESSIONS.pop(session_key(bu, office, shift_date, shift_type), None)
    if session and session.task:
        session.task.cancel()
    return session


# --------------------------------------------------------------- tool context
#
# The agent's tools are plain functions with no argument for "which shift are
# we talking about". Threading four scope parameters through every tool
# signature would put them in the model's schema, where they are noise at best
# and something for it to hallucinate at worst. A context variable set by the
# caller keeps the scope out of the model's hands entirely: the agent can only
# ever act on the shift it was invoked for.

_CURRENT: ContextVar[Session | None] = ContextVar("current_session", default=None)


def bind_session(session: Session):
    """Make `session` the one the agent's tools operate on."""
    return _CURRENT.set(session)


def unbind_session(token) -> None:
    _CURRENT.reset(token)


def current_session() -> Session:
    """The session the tools should read. Raises rather than guessing."""
    session = _CURRENT.get()
    if session is None:
        raise SessionMissing(
            "no shift is bound; tools must be called inside an agent invocation"
        )
    return session
