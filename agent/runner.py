"""Driving the agent, and deciding when it is worth driving at all.

Two entry points. `compose` is the proactive one: the replay notices a
situation worth explaining and asks for prose. `ask` is the reactive one: the
manager types a question. Both run through the same ADK runner and the same
tools; they differ in whether they keep a transcript, for reasons below.

The session store is `DatabaseSessionService` pointed at the same Postgres as
everything else. That is not an aesthetic preference: it means the agent's
conversation, the alerts it wrote and the actions taken on them can be read in
one query, which is what an audit of "why did the floor do that on Thursday"
actually requires.

## Two kinds of conversation

Writing up an alert and answering a question look similar and should not share
a transcript.

An alert write-up is a **one-shot task**. It needs the alert and nothing else.
A first version put it in the same per-shift conversation as chat, on the
reasoning that an agent answering questions should remember what it had already
said. The property was real; the cost was not survivable. Every narration
appended its tool results to a transcript that was replayed on every subsequent
call, so a single alert cost 22,000 prompt tokens and each re-run of the demo
made the next one worse. Compose now runs in a throwaway conversation.

The agent loses nothing by this. It can still see what it wrote, because
`list_alerts` and `get_alert` report the saved narrative. Reading a fact back
from the database costs a few dozen tokens; carrying the transcript that
produced it costs thousands.

**Chat** keeps the persistent per-shift conversation, because there a follow-up
question genuinely depends on the previous answer.

## Not calling the model

The cheapest model call is the one that does not happen, and this module is
mostly about not making them.

* The replay decides whether a situation has changed enough to deserve new
  prose. Ticks that only nudge a number reuse what is already written.
* Before invoking, the narrative cache is checked by payload hash. A morning
  with the same cause, severity band and recommendation as one already written
  up gets that text for free.
* If the model is unreachable, the alert keeps its structured payload and the
  board renders a table. Degraded, not broken.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import RunConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from app.config import SQLALCHEMY_URL
from app.core.alerts import Alert
from app.sessions import Session, bind_session, unbind_session
from app import store
from agent.agent import MODEL, narrator_agent, root_agent

log = logging.getLogger(__name__)

APP_NAME = "bessemer"
INVOCATION_TIMEOUT_S = 60.0


@dataclass
class Usage:
    """What the agent has cost so far, for the cost-at-scale claim.

    Counted rather than asserted. A system that says inference is cheap should
    be able to show the meter.
    """

    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    by_reason: dict[str, int] = field(default_factory=dict)

    def record(self, reason: str, seconds: float, prompt: int, completion: int) -> None:
        self.calls += 1
        self.seconds += seconds
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": MODEL,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "seconds": round(self.seconds, 1),
            "avg_seconds": round(self.seconds / self.calls, 1) if self.calls else 0.0,
            "by_reason": self.by_reason,
        }


USAGE = Usage()


async def clear_conversations(session: Session, user_id: str = "line_manager") -> int:
    """Delete this shift's agent transcripts.

    Called on replay reset so a demo run twice does not carry the first run's
    conversation into the second, which is how the token cost quietly triples
    between rehearsal and presentation.
    """
    service = session_service()
    prefix = conversation_id(session)
    removed = 0
    try:
        listing = await service.list_sessions(app_name=APP_NAME, user_id=user_id)
        for existing in getattr(listing, "sessions", []):
            if existing.id.startswith(prefix):
                await service.delete_session(
                    app_name=APP_NAME, user_id=user_id, session_id=existing.id
                )
                removed += 1
    except Exception as exc:  # noqa: BLE001 - reset must never fail on cleanup
        log.warning("could not clear agent conversations: %s", exc)
    return removed


_runners: dict[str, Runner] = {}
_session_service: DatabaseSessionService | None = None


def session_service() -> DatabaseSessionService:
    """One ADK session store, shared by both agents and by the app's Postgres."""
    global _session_service
    if _session_service is None:
        _session_service = DatabaseSessionService(db_url=SQLALCHEMY_URL)
    return _session_service


def runner(agent=None) -> Runner:
    """A runner per agent, built once and reused."""
    agent = agent or root_agent
    if agent.name not in _runners:
        _runners[agent.name] = Runner(
            agent=agent, app_name=APP_NAME, session_service=session_service()
        )
    return _runners[agent.name]


def conversation_id(session: Session) -> str:
    """The persistent per-shift conversation, used by chat."""
    replay = session.replay
    return f"{replay.office}:{replay.shift_date}:{replay.shift_type}".replace(" ", "_")


def task_conversation_id(session: Session, alert_id: int) -> str:
    """A throwaway conversation for one alert write-up.

    Unique per call. An earlier version keyed on alert and minute, and two
    write-ups of the same alert inside one minute collided on the same ADK
    session id, which surfaced as "Session ... not found" mid-run.
    """
    return f"{conversation_id(session)}:task:{alert_id}:{session.replay.now:%H%M}:{uuid.uuid4().hex[:6]}"


async def _ensure_conversation(session: Session, user_id: str, sid: str) -> str:
    """Create the named ADK conversation if it does not exist yet."""
    service = session_service()
    existing = await service.get_session(app_name=APP_NAME, user_id=user_id, session_id=sid)
    if existing is None:
        await service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=sid,
            state={
                "office": session.replay.office,
                "shift_date": session.replay.shift_date.isoformat(),
                "shift_type": session.replay.shift_type,
                "business_unit": session.replay.business_unit,
            },
        )
    return sid


async def _invoke(
    session: Session,
    user_id: str,
    text: str,
    reason: str,
    sid: str | None = None,
    agent=None,
    on_token=None,
) -> str:
    """Run one agent turn and return its final text.

    With `on_token`, the model's reply is streamed and each delta is handed to
    the callback as it arrives. The shift is bound for the duration, so the
    tools can only reach this tenant's data.
    """
    sid = await _ensure_conversation(session, user_id, sid or conversation_id(session))
    message = types.Content(role="user", parts=[types.Part(text=text)])
    config = RunConfig(streaming_mode=StreamingMode.SSE) if on_token else RunConfig()

    token = bind_session(session)
    started = time.perf_counter()
    prompt_tokens = completion_tokens = 0
    streamed: list[str] = []
    final: list[str] = []
    try:
        async for event in runner(agent).run_async(
            user_id=user_id, session_id=sid, new_message=message, run_config=config
        ):
            usage = getattr(event, "usage_metadata", None)
            if usage:
                prompt_tokens += getattr(usage, "prompt_token_count", 0) or 0
                completion_tokens += getattr(usage, "candidates_token_count", 0) or 0
            if not (event.content and event.content.parts):
                continue
            chunk = "".join(p.text or "" for p in event.content.parts if p.text)
            if getattr(event, "partial", False):
                if chunk and on_token:
                    streamed.append(chunk)
                    on_token(chunk)
            elif event.is_final_response() and chunk:
                final.append(chunk)
    finally:
        unbind_session(token)
        USAGE.record(reason, time.perf_counter() - started, prompt_tokens, completion_tokens)

    return ("\n".join(final) or "".join(streamed)).strip()


DRAFT_KEYS = {
    "cover": "EARLY_SHIFT_COVER",
    "transport": "ESCALATE_TRANSPORT",
    "operations": "ESCALATE_OPS",
}


def split_reply(text: str) -> tuple[str, dict[str, str]]:
    """Separate the summary from the trailing draft lines.

    Drafts are lines that start with Cover:, Transport: or Operations:. Anything
    before the first of those is the summary the board shows.
    """
    summary: list[str] = []
    drafts: dict[str, str] = {}
    in_drafts = False
    for line in text.splitlines():
        stripped = line.strip()
        head, sep, body = stripped.partition(":")
        key = head.strip().lower().rstrip("*").lstrip("*").strip()
        if sep and key in DRAFT_KEYS:
            in_drafts = True
            if body.strip():
                drafts[DRAFT_KEYS[key]] = body.strip()
            continue
        if in_drafts:
            # A continuation line of the last draft.
            if drafts and stripped:
                last = list(drafts)[-1]
                drafts[last] = f"{drafts[last]} {stripped}"
            continue
        summary.append(line)
    return "\n".join(summary).strip(), drafts


async def compose(
    session: Session, alert: Alert, user_id: str = "line_manager", fresh: bool = False
) -> bool:
    """Write up one alert, streaming the words to the board as they arrive.

    Returns True if the alert now carries prose. False means the model was
    unreachable twice and the board should fall back to the structured payload.
    """
    alert_id = session.alert_ids.get(alert.queue)
    if alert_id is None:
        alert_id = store.save_alert(alert)
        session.alert_ids[alert.queue] = alert_id

    cached = None if fresh else store.find_cached_narrative(alert.payload_hash())
    if cached and cached.get("narrative"):
        alert.narrative = cached["narrative"]
        alert.drafts = cached.get("drafts") or {}
        alert.mark_narrated(session.replay.now)
        store.save_alert(alert)
        USAGE.cache_hits += 1
        return True

    prompt = (
        f"Alert {alert_id} on the {alert.queue} queue, "
        f"{'open' if alert.status.value == 'OPEN' else alert.status.value.lower()} "
        f"at {session.replay.now:%H:%M}. Write it up."
    )
    queue = alert.queue
    session.partial[queue] = ""

    def on_token(delta: str) -> None:
        session.partial[queue] += delta
        session.publish("token", queue=queue, text=delta)

    reply = ""
    for attempt in (1, 2):
        try:
            reply = await asyncio.wait_for(
                _invoke(
                    session, user_id, prompt,
                    reason=f"compose:{alert.status.value}",
                    sid=task_conversation_id(session, alert_id),
                    agent=narrator_agent,
                    on_token=on_token,
                ),
                timeout=INVOCATION_TIMEOUT_S,
            )
            break
        except Exception as exc:  # noqa: BLE001 - the board must survive any failure here
            USAGE.failures += 1
            log.warning("narrative generation failed for alert %s (attempt %d): %s", alert_id, attempt, exc)
            session.partial[queue] = ""
            if attempt == 2:
                return False

    summary, drafts = split_reply(reply)
    if not summary:
        return False
    offered = {o.pathway.value for o in alert.options}
    alert.narrative = summary
    alert.drafts = {k: v for k, v in drafts.items() if k in offered}
    alert.mark_narrated(session.replay.now)
    store.save_alert(alert)
    store.remember_narrative(alert.payload_hash(), alert.narrative, alert.drafts)
    session.partial.pop(queue, None)
    return True


async def ask(session: Session, text: str, user_id: str = "line_manager") -> dict[str, Any]:
    """Answer a manager's question about the shift."""
    try:
        reply = await asyncio.wait_for(
            _invoke(session, user_id, text, reason="chat"),
            timeout=INVOCATION_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        USAGE.failures += 1
        log.warning("chat failed: %s", exc)
        return {
            "reply": (
                "I could not reach the model just then. The board and alerts are "
                "still live and accurate."
            ),
            "error": str(exc),
        }
    return {"reply": reply or "No answer came back.", "usage": USAGE.as_dict()}
