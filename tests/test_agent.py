"""Phase 4 checks: the agent, its tools, and what it costs.

Split in two. The tool tests run offline and are the bulk, because the tools
are where the agent could do damage and they are ordinary Python. The
end-to-end tests call the real model, cost money, and are marked `live` so the
default suite stays fast and free:

    uv run pytest tests/ -m "not live"     # default, no spend
    uv run pytest tests/test_agent.py      # includes live calls
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from agent import tools
from app.core.alerts import Pathway
from app.db import query
from app.replay import Replay
from app.sessions import (
    SESSIONS,
    Session,
    SessionMissing,
    bind_session,
    unbind_session,
)
from app import store

DEMO = date(2026, 6, 11)
OFFICE = "Clearwater Campus"


@pytest.fixture()
def shift():
    """A replay positioned at the worst moment of the demo morning."""
    SESSIONS.clear()
    store.reset_shift(OFFICE, DEMO, "09:00")
    store.clear_cover_log(DEMO)
    store.forget_narratives()

    replay = Replay(shift_date=DEMO, tick_minutes=1)
    for tick in replay.ticks():
        replay.advance(tick)
        if tick >= datetime(2026, 6, 11, 8, 55):
            break
    session = Session(replay=replay)
    session.persist()

    token = bind_session(session)
    yield session
    unbind_session(token)
    store.reset_shift(OFFICE, DEMO, "09:00")
    store.clear_cover_log(DEMO)
    store.forget_narratives()
    SESSIONS.clear()


# ----------------------------------------------------------------- the fence


def test_tools_refuse_to_run_outside_a_bound_shift():
    """The agent must never be able to reach a shift it was not invoked for.

    Scope is a context variable set by the caller, not an argument the model
    can choose, so there is no prompt that reaches another tenant's floor.
    """
    assert tools.get_shift_board()["status"] == "error"
    assert tools.list_alerts()["status"] == "error"
    assert tools.get_alert(1)["status"] == "error"


def test_an_unknown_alert_id_is_refused_with_the_known_ones(shift):
    result = tools.get_alert(999_999)
    assert result["status"] == "error"
    assert "not part of this shift" in result["error"]


def test_an_unknown_queue_is_refused_with_the_real_ones(shift):
    result = tools.get_cover_candidates("payroll")
    assert result["status"] == "error"
    assert "billing" in result["error"]


def test_tool_errors_come_back_as_data_not_exceptions(shift):
    """A raised exception would abort the agent's turn and lose the
    conversation. Returning the problem lets it recover or say so."""
    for result in (
        tools.get_alert(-1),
        tools.get_cover_candidates("nope"),
        tools.compose_alert(-1, "text"),
        tools.record_action(-1, "WAIT"),
    ):
        assert result["status"] == "error"
        assert isinstance(result["error"], str)


# ------------------------------------------------------------------ the facts


def test_the_board_tool_reports_the_real_position(shift):
    board = tools.get_shift_board()["board"]
    assert board["time"] == "08:55"
    assert board["totals"]["rostered"] == 24
    assert len(board["queues"]) == 2


def test_the_alert_tool_answers_in_one_call(shift):
    """Context and candidates are folded in, so a write-up needs two tool
    calls rather than five. Five cost 22,000 prompt tokens per alert."""
    alert_id = shift.id_for("billing")
    payload = tools.get_alert(alert_id)["alert"]
    assert payload["alert_id"] == alert_id
    assert payload["context"], "benchmarks must arrive with the alert"
    assert payload["options"], "options must arrive with the alert"
    cover = next(o for o in payload["options"] if o["pathway"] == "EARLY_SHIFT_COVER")
    assert cover["people"], "the people to name must arrive with the alert"


def test_the_narrative_payload_is_a_quarter_of_the_full_one(shift):
    import json

    _, alert = shift.alert_for(shift.id_for("billing"))
    full = len(json.dumps(alert.payload(), default=str))
    compact = len(json.dumps(alert.for_narrative(), default=str))
    assert compact < full / 3


def test_the_narrative_payload_keeps_every_figure_worth_inventing(shift):
    """Anything omitted is something the model will estimate instead."""
    _, alert = shift.alert_for(shift.id_for("billing"))
    payload = alert.for_narrative()
    for field in (
        "coverage_pct",
        "agents_missing",
        "service_level_now_pct",
        "service_level_full_strength_pct",
        "day_target_holds",
        "if_nobody_acts",
        "recovered_by",
    ):
        assert payload[field] is not None, f"{field} missing; the model will guess it"


def test_cover_candidates_are_people_already_in_the_building(shift):
    result = tools.get_cover_candidates("billing", limit=4)
    assert result["status"] == "success"
    assert result["candidates"]
    for candidate in result["candidates"]:
        assert candidate["arrived_at"] <= shift.replay.now.isoformat()


# ------------------------------------------------------------------ the act


def test_composing_saves_the_narrative_and_drafts(shift):
    alert_id = shift.id_for("billing")
    result = tools.compose_alert(
        alert_id,
        narrative="Billing is four short until 09:19.",
        cover_draft="Early-shift lead: please move four agents onto billing.",
    )
    assert result["status"] == "success"
    assert result["drafts_saved"] == ["EARLY_SHIFT_COVER"]

    stored = query("SELECT narrative, drafts FROM shift_alerts WHERE id = %s", (alert_id,))[0]
    assert stored["narrative"].startswith("Billing is four short")
    assert "EARLY_SHIFT_COVER" in stored["drafts"]


def test_a_draft_for_an_option_that_is_not_offered_is_discarded(shift):
    """The bug this guards: one `escalation_draft` field was written to both
    the transport and operations keys, so a note about one vendor's cabs would
    have reached the operations director as if the day's target were at risk."""
    alert_id = shift.id_for("billing")
    _, alert = shift.alert_for(alert_id)
    assert Pathway.ESCALATE_OPS not in {o.pathway for o in alert.options}

    result = tools.compose_alert(
        alert_id,
        narrative="Billing is short.",
        transport_draft="Transport: several riders on one vendor.",
        operations_draft="Operations: the day is lost.",
    )
    assert result["drafts_saved"] == ["ESCALATE_TRANSPORT"]
    assert result["drafts_ignored"] == ["ESCALATE_OPS"]
    assert "ESCALATE_OPS" not in alert.drafts


def test_composing_counts_as_having_narrated(shift):
    """Otherwise the replay would ask for the same write-up on the next tick."""
    alert_id = shift.id_for("billing")
    _, alert = shift.alert_for(alert_id)
    assert alert.needs_narrative(shift.replay.now)
    tools.compose_alert(alert_id, narrative="Written.")
    assert not alert.needs_narrative(shift.replay.now)


def test_recording_an_action_writes_the_audit_trail(shift):
    alert_id = shift.id_for("billing")
    result = tools.record_action(alert_id, "EARLY_SHIFT_COVER")
    assert result["status"] == "success"
    assert result["people"]
    stored = query("SELECT * FROM alert_actions WHERE alert_id = %s", (alert_id,))
    assert len(stored) == 1


def test_recording_an_option_that_is_not_offered_is_refused(shift):
    result = tools.record_action(shift.id_for("billing"), "CROSS_COVER")
    assert result["status"] == "error"
    assert "EARLY_SHIFT_COVER" in result["error"]


# ------------------------------------------------------------------- the cost


def test_the_narrator_carries_only_the_tools_it_needs():
    """Tool schemas are resent on every request. Handing a single-purpose
    agent all seven costs about 1,300 tokens per round trip for capability it
    never uses."""
    from agent.agent import narrator_agent, root_agent

    assert len(narrator_agent.tools) == 1
    assert {t.__name__ for t in narrator_agent.tools} == {"get_alert"}
    assert len(root_agent.tools) > len(narrator_agent.tools)


def test_composing_uses_a_throwaway_conversation():
    """A shared transcript grew without bound: every past narration was
    replayed on the next call, so one alert reached 22,000 prompt tokens and
    each demo re-run made the following one worse."""
    from agent.runner import conversation_id, task_conversation_id

    replay = Replay(shift_date=DEMO, tick_minutes=1)
    session = Session(replay=replay)
    shared = conversation_id(session)
    task = task_conversation_id(session, 1)
    assert task != shared
    assert task.startswith(shared)
    assert task_conversation_id(session, 2) != task


def test_the_cached_narrative_is_reused_for_the_same_situation(shift):
    """Two mornings with the same cause, severity and recommendation deserve
    the same sentence, and the second should be free."""
    alert_id = shift.id_for("billing")
    _, alert = shift.alert_for(alert_id)
    tools.compose_alert(alert_id, narrative="Cached text.", cover_draft="Cover please.")

    found = store.find_cached_narrative(alert.payload_hash())
    assert found is not None
    assert found["narrative"] == "Cached text."


# -------------------------------------------------------------------- live


@pytest.mark.live
def test_the_agent_writes_a_usable_alert(shift):
    """One real call. Checks the model obeys the rules that matter."""
    from agent import runner as agent_runner

    alert_id = shift.id_for("billing")
    _, alert = shift.alert_for(alert_id)

    assert asyncio.run(agent_runner.compose(shift, alert)) is True
    assert alert.narrative

    # It must not invent people. Every capitalised "Agent NN" it names has to
    # be somebody the tools actually returned.
    import re

    named = set(re.findall(r"Agent \d+", alert.narrative))
    real = {
        r["display_name"]
        for r in query("SELECT display_name FROM roster WHERE office = %s", (OFFICE,))
    }
    assert named <= real, f"invented names: {named - real}"

    # It must not contradict the computed position.
    assert "67" in alert.narrative or "four" in alert.narrative.lower()
    assert len(alert.narrative.split(". ")) <= 6


@pytest.mark.live
def test_the_agent_answers_a_question_from_the_board(shift):
    from agent import runner as agent_runner

    answer = asyncio.run(agent_runner.ask(shift, "How many are on the floor right now?"))
    assert answer["reply"]
    assert "3" in answer["reply"] or "three" in answer["reply"].lower()


@pytest.mark.live
def test_one_alert_costs_a_few_thousand_tokens_not_twenty(shift):
    """The cost claim, metered rather than asserted."""
    from agent import runner as agent_runner

    before = agent_runner.USAGE.prompt_tokens + agent_runner.USAGE.completion_tokens
    _, alert = shift.alert_for(shift.id_for("techsupport"))
    asyncio.run(agent_runner.compose(shift, alert))
    spent = agent_runner.USAGE.prompt_tokens + agent_runner.USAGE.completion_tokens - before
    assert 0 < spent < 9_000, f"{spent} tokens for one alert is too many"


# ------------------------------------------------------------ the reply format


def test_the_reply_splits_into_a_short_summary_and_drafts():
    """The narrator writes plain text so it can stream. The server takes the
    drafts off the end and keeps the summary short."""
    from agent.runner import split_reply

    reply = (
        "Billing is four short until 09:30. Service level 15% now; the day holds at 88%. "
        "Move Agent 35, 27, 31 and 29 from the early shift.\n\n"
        "Cover: Daniel, please put Agent 35, 27, 31 and 29 on Billing Support until 09:30.\n"
        "Transport: Meera, four billing riders on Karan Mikhailov Travel were 38 minutes late."
    )
    summary, drafts = split_reply(reply)
    assert len(summary.split()) < 45
    assert "Cover:" not in summary
    assert set(drafts) == {"EARLY_SHIFT_COVER", "ESCALATE_TRANSPORT"}
    assert drafts["EARLY_SHIFT_COVER"].startswith("Daniel")


def test_a_reply_with_no_drafts_is_all_summary():
    from agent.runner import split_reply

    summary, drafts = split_reply("Tech support is three short. The day is lost.")
    assert summary.startswith("Tech support")
    assert drafts == {}


def test_bold_draft_labels_still_parse():
    from agent.runner import split_reply

    _, drafts = split_reply("Short.\n\n**Cover:** Daniel, two people please.\n**Operations:** Day at 53%.")
    assert set(drafts) == {"EARLY_SHIFT_COVER", "ESCALATE_OPS"}
