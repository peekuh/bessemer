"""HTTP surface for the shift board.

Thin by design: every endpoint either reads the reasoning core or records a
decision. Two modes, one switch.

**Live.** The clock is real. Each tick is computed, and when an alert's
situation changes the model is called fresh, with the clock paused while it
writes so the audience sees generation happen. Every tick is captured into a
recording as it goes.

**Playback.** `/at?t=` reads the last recording. No compute, no model.

Every endpoint is scoped by business unit, office, date and shift through one
dependency, so no endpoint can quietly forget the tenant boundary.

Run:  uv run uvicorn app.api:app --port 8000
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app import recording as rec
from app import store
from app.config import BUSINESS_UNIT, DEMO_DATE, OFFICE, SHIFT_TYPE
from app.core.alerts import Alert, Option, Pathway, Status
from app.core.remediation import record_cover
from app.sessions import (
    SESSIONS,
    RosterMissing,
    Session,
    SessionMissing,
    drop_session,
    get_session,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    for session in SESSIONS.values():
        if session.task:
            session.task.cancel()


app = FastAPI(
    title="Shift Readiness Agent",
    description="Commute delays, translated into floor readiness for a line manager.",
    version="0.5.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------- tenant scope


class Scope:
    """Tenant, site and shift for every request. Defaults from config."""

    def __init__(
        self,
        business_unit: str = Query(BUSINESS_UNIT),
        office: str = Query(OFFICE),
        shift_date: date = Query(default_factory=lambda: date.fromisoformat(DEMO_DATE)),
        shift_type: str = Query(SHIFT_TYPE),
    ) -> None:
        self.business_unit = business_unit
        self.office = office
        self.shift_date = shift_date
        self.shift_type = shift_type

    def session(self, create: bool = True) -> Session:
        try:
            return get_session(
                self.business_unit, self.office, self.shift_date, self.shift_type, create
            )
        except (RosterMissing, SessionMissing) as exc:
            raise HTTPException(404, str(exc)) from exc


# ----------------------------------------------------------------- narration


async def _narrate_now(session: Session, force: bool = False) -> None:
    """Write up alerts, blocking the clock while the model works.

    Live mode always calls the model fresh; the memo is for nothing here.
    `force` rewrites every open alert regardless of whether its situation
    changed, which is what a presenter clicking a checkpoint wants to see.
    """
    from agent import runner as agent_runner

    if not session.narrate:
        return
    for queue, alert in list(session.replay.alerts.items()):
        if alert.status is Status.RESOLVED:
            continue
        if not force and not alert.needs_narrative(session.replay.now):
            continue
        session.narrating = queue
        try:
            await agent_runner.compose(session, alert, fresh=True)
        except Exception:  # noqa: BLE001 - the board outlives the model
            pass
        finally:
            session.narrating = None
        session.persist()


async def _run(session: Session) -> None:
    """Advance the clock at the session's speed, narrating and capturing."""
    try:
        for tick in session.replay.ticks():
            if tick <= session.replay.now:
                continue
            session.replay.advance(tick)
            session.persist()
            await _narrate_now(session)
            rec.capture(session)
            await asyncio.sleep(session.replay.tick_minutes / session.speed)
    except asyncio.CancelledError:
        pass
    finally:
        session.running = False
        rec.save(session)


def _advance_to(session: Session, target: datetime) -> None:
    for tick in session.replay.ticks():
        if tick <= session.replay.now:
            continue
        if tick > target:
            break
        session.replay.advance(tick)
        # Persist before capturing so every snapshot carries the alert's row
        # id. Without it, alerts opened mid-jump were recorded with id None
        # and could not be acted on from playback.
        session.persist()
        rec.capture(session)


# ------------------------------------------------------------------- control


@app.post("/replay/start", tags=["live"])
async def start_replay(
    speed: float = Query(60.0, gt=0, le=3600),
    to: str | None = Query(None, description="Jump to HH:MM, then write up what is open"),
    narrate: bool = Query(True, description="Call the model. Tests pass false."),
    scope: Scope = Depends(),
) -> dict[str, Any]:
    session = scope.session()
    session.speed = speed
    session.narrate = narrate

    if to:
        target = datetime.combine(scope.shift_date, datetime.strptime(to, "%H:%M").time())
        if target < session.replay.now:
            session = _restart(session)
        if session.task and not session.task.done():
            session.task.cancel()
            session.running = False
        _advance_to(session, target)
        await _narrate_now(session, force=True)
        rec.capture(session)
        rec.save(session)
        return {"status": "positioned", "clock": session.replay.now.isoformat()}

    if session.running:
        return {"status": "already running", "clock": session.replay.now.isoformat()}
    session.running = True
    session.task = asyncio.create_task(_run(session))
    return {"status": "running", "speed": speed, "clock": session.replay.now.isoformat()}


@app.post("/replay/pause", tags=["live"])
async def pause_replay(scope: Scope = Depends()) -> dict[str, Any]:
    session = scope.session(create=False)
    if session.task and not session.task.done():
        session.task.cancel()
        try:
            await session.task
        except asyncio.CancelledError:
            pass
    session.running = False
    session.task = None
    rec.save(session)
    return {"status": "paused", "clock": session.replay.now.isoformat()}


@app.post("/replay/reset", tags=["live"])
async def reset_replay(
    clear_cover: bool = Query(False),
    forget_recording: bool = Query(False, description="Also discard the playback recording"),
    scope: Scope = Depends(),
) -> dict[str, Any]:
    from agent import runner as agent_runner

    from app.sessions import session_key

    dropped = drop_session(scope.business_unit, scope.office, scope.shift_date, scope.shift_type)
    if dropped:
        await agent_runner.clear_conversations(dropped)
    if forget_recording:
        rec.forget(session_key(scope.business_unit, scope.office, scope.shift_date, scope.shift_type), dropped)
    removed = store.reset_shift(scope.office, scope.shift_date, scope.shift_type)
    if clear_cover:
        store.clear_cover_log(scope.shift_date)
    fresh = scope.session()
    return {"status": "reset", "alerts_cleared": removed, "clock": fresh.replay.now.isoformat()}


@app.post("/replay/step", tags=["live"])
async def step_replay(minutes: int = Query(5, ge=1, le=180), scope: Scope = Depends()) -> dict[str, Any]:
    """Advance the clock by hand without narrating. Handy from curl."""
    session = scope.session()
    _advance_to(session, session.replay.now + timedelta(minutes=minutes))
    return {"status": "stepped", "clock": session.replay.now.isoformat()}


def _restart(session: Session) -> Session:
    """The clock only moves forward; rewinding means a fresh replay."""
    r = session.replay
    recording = session.recording
    drop_session(r.business_unit, r.office, r.shift_date, r.shift_type)
    store.reset_shift(r.office, r.shift_date, r.shift_type)
    fresh = get_session(r.business_unit, r.office, r.shift_date, r.shift_type)
    fresh.speed = session.speed
    fresh.narrate = session.narrate
    fresh.recording = recording  # keep capturing into the same recording
    return fresh


# ---------------------------------------------------------------------- views


@app.get("/board", tags=["board"])
async def board(scope: Scope = Depends()) -> dict[str, Any]:
    session = scope.session()
    payload = session.replay.board()
    payload["running"] = session.running
    payload["speed"] = session.speed
    payload["narrating"] = session.narrating
    payload["recording"] = session.recording.status() if session.recording else {"ready": False}
    return payload


def _alerts_with_actions(session: Session) -> list[dict[str, Any]]:
    session.persist()
    taken = store.actions_for(list(session.alert_ids.values()))
    out = []
    for queue, alert in session.replay.alerts.items():
        alert_id = session.alert_ids.get(queue)
        out.append(alert.as_dict() | {"id": alert_id, "actions": _fmt_actions(taken.get(alert_id or -1, []))})
    out.sort(key=lambda a: (a["status"] == "RESOLVED", a["opened_at"]))
    return out


def _fmt_actions(rows: list[dict[str, Any]], cutoff: str | None = None) -> list[dict[str, Any]]:
    return [
        {
            "pathway": a["pathway"], "draft": a["draft"], "people": a["candidates"],
            "cost": a["cost"], "sent_at": a["sent_at"].isoformat(),
            "time": a["sent_at"].strftime("%H:%M"),
        }
        for a in rows
        if cutoff is None or a["sent_at"].isoformat() <= cutoff
    ]


@app.get("/alerts", tags=["board"])
async def alerts(scope: Scope = Depends()) -> dict[str, Any]:
    session = scope.session()
    return {"clock": session.replay.now.isoformat(), "alerts": _alerts_with_actions(session)}


@app.get("/events", tags=["board"])
async def events(since: str | None = Query(None), scope: Scope = Depends()) -> dict[str, Any]:
    session = scope.session()
    cutoff = datetime.fromisoformat(since) if since else None
    return {
        "clock": session.replay.now.isoformat(),
        "events": [e.as_dict() for e in session.replay.events if cutoff is None or e.at > cutoff],
    }


@app.get("/landmarks", tags=["board"])
async def landmarks(scope: Scope = Depends()) -> dict[str, Any]:
    """Story beats. From the recording if there is one, else from the live feed so far."""
    session = scope.session()
    r = session.replay
    if session.recording and session.recording.ready:
        marks = session.recording.landmarks(r.shift_start, r.deadline)
        source = "recording"
    else:
        everyone = r.now.isoformat() if r.board()["totals"]["on_floor"] == len(r.legs) else None
        marks = rec.derive_landmarks([e.as_dict() for e in r.events], r.shift_start, r.deadline, everyone)
        source = "live"
    return {"source": source, "landmarks": marks}


@app.get("/at", tags=["playback"])
async def at(t: str = Query(..., description="HH:MM"), scope: Scope = Depends()) -> dict[str, Any]:
    """The board, alerts and feed as recorded at one minute. Playback only."""
    session = scope.session()
    recording = session.recording
    if recording is None or not recording.ready:
        raise HTTPException(409, "no recording yet; switch to Live and run the morning")
    snap = recording.at(t)
    taken = store.actions_for([a["id"] for a in snap["alerts"] if a.get("id")])
    return {
        "clock": snap["clock"],
        "time": snap["time"],
        "board": snap["board"],
        "alerts": [a | {"actions": _fmt_actions(taken.get(a.get("id") or -1, []), snap["clock"])} for a in snap["alerts"]],
        "events": snap["events"],
    }


# --------------------------------------------------------------------- acting


@app.post("/alerts/{alert_id}/act", tags=["act"])
async def act(
    alert_id: int,
    pathway: str = Query(...),
    at: str | None = Query(None, description="HH:MM when acting from playback"),
    scope: Scope = Depends(),
) -> dict[str, Any]:
    session = scope.session(create=False)
    session.persist()
    found = session.alert_for(alert_id)
    if found is None:
        raise HTTPException(404, f"alert {alert_id} is not part of this shift")
    _, alert = found

    when = session.replay.now
    if at and session.recording and session.recording.ready:
        snap = session.recording.at(at)
        frozen = next((a for a in snap["alerts"] if a.get("id") == alert_id), None) if snap else None
        if frozen:
            when = datetime.fromisoformat(snap["clock"])
            alert.options = [
                Option(pathway=Pathway(o["pathway"]), label=o["label"], rationale=o["rationale"],
                       people=o["people"], cost=o["cost"], recommended=o["recommended"],
                       urgent=o.get("urgent", False))
                for o in frozen["options"]
            ]
            alert.drafts = frozen.get("drafts") or {}
            alert.impact = frozen.get("impact") or alert.impact
            alert.coverage_pct = frozen.get("coverage_pct", alert.coverage_pct)

    try:
        chosen = Pathway(pathway)
    except ValueError:
        raise HTTPException(400, f"unknown pathway {pathway!r}")
    option = next((o for o in alert.options if o.pathway is chosen), None)
    if option is None:
        raise HTTPException(409, f"{pathway} is not offered; options are {[o.pathway.value for o in alert.options]}")

    draft = alert.drafts.get(chosen.value) or _fallback_draft(alert, option)
    if chosen in {Pathway.EARLY_SHIFT_COVER, Pathway.CROSS_COVER}:
        movers = [p["stwid"] for p in option.people if p.get("stwid")]
        minutes = round((alert.impact.get("minutes_lost") or 0) / max(1, len(movers)))
        if movers:
            record_cover(movers, scope.shift_date, minutes)

    action = store.record_action(alert_id, chosen.value, draft, option.people, when, option.cost)
    return {"status": "recorded", "pathway": chosen.value, "draft": draft,
            "people": option.people, "sent_at": action["sent_at"].isoformat()}


def _fallback_draft(alert: Alert, option: Option) -> str:
    """A sendable message when the model has not written one."""
    names = ", ".join(str(p.get("name") or p.get("vendor") or p.get("role")) for p in option.people) or "the team"
    when = alert.updated_at.strftime("%H:%M")
    q, shift = alert.display_name, alert.shift_type
    if option.pathway is Pathway.EARLY_SHIFT_COVER:
        return f"{q} is {alert.coverage_pct}% staffed for the {shift} start. Could {names} move onto the queue until the {shift} team is in? Asking at {when}."
    if option.pathway is Pathway.CROSS_COVER:
        return f"{q} is short and its own cover pool is exhausted. Requesting {names} from the adjacent queue, accepting slower handling."
    if option.pathway is Pathway.HOLD_OVER:
        return f"{names}: please hold your positions past shift end. {(option.cost or {}).get('summary', '')}. Relief is en route."
    if option.pathway is Pathway.CONTACT_EMPLOYEE:
        return f"No pickup recorded for {names} on the {shift} run. Please confirm whether they are travelling."
    if option.pathway is Pathway.ESCALATE_TRANSPORT:
        return f"{q} at {alert.office}: several riders affected on {names} this morning. Raising for the {alert.shift_date} record."
    if option.pathway is Pathway.ESCALATE_OPS:
        day = (alert.impact.get("day") or {}).get("headline", "")
        return f"{q}, {alert.shift_date}: {day}. Cause: {alert.cause.value.replace('_', ' ').lower()}. Flagging now rather than in the evening report."
    return f"{q}: holding at {alert.coverage_pct}% and watching."


# ---------------------------------------------------------------------- agent


@app.post("/chat", tags=["agent"])
async def chat(body: dict = Body(...), scope: Scope = Depends()) -> dict[str, Any]:
    from agent import runner as agent_runner

    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "send a question as {'text': ...}")
    session = scope.session()
    session.persist()
    answer = await agent_runner.ask(session, text)
    return {"clock": session.replay.now.isoformat(), **answer}


@app.post("/alerts/{alert_id}/narrate", tags=["agent"])
async def narrate(alert_id: int, scope: Scope = Depends()) -> dict[str, Any]:
    from agent import runner as agent_runner

    session = scope.session()
    session.persist()
    found = session.alert_for(alert_id)
    if found is None:
        raise HTTPException(404, f"alert {alert_id} is not part of this shift")
    _, alert = found
    written = await agent_runner.compose(session, alert, fresh=True)
    session.persist()
    return {"status": "written" if written else "unavailable", "narrative": alert.narrative,
            "drafts": alert.drafts, "usage": agent_runner.USAGE.as_dict()}


@app.get("/usage", tags=["agent"])
async def usage() -> dict[str, Any]:
    from agent import runner as agent_runner

    return agent_runner.USAGE.as_dict()


# ------------------------------------------------------------------------ ops

WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


@app.get("/", include_in_schema=False)
async def board_page() -> FileResponse:
    return FileResponse(WEB, media_type="text/html")


@app.get("/health", tags=["ops"])
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "sessions": [
            {"key": k, "clock": s.replay.now.isoformat(), "running": s.running,
             "narrating": s.narrating, "recording": bool(s.recording and s.recording.ready)}
            for k, s in SESSIONS.items()
        ],
    }
