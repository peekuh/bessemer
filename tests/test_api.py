"""Phase 3 checks: the HTTP surface and what it writes to Postgres.

Uses FastAPI's TestClient against the real database, because the interesting
failures here are about persistence and tenant scoping, and an in-memory stub
would test neither.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import store
from app.api import SESSIONS, app
from app.config import DEMO_DATE, OFFICE, SHIFT_TYPE
from app.db import query


@pytest.fixture()
def client():
    SESSIONS.clear()
    # The narrative memo is global by design and survives resets, which is
    # exactly right in production and exactly wrong between tests: a fake
    # draft written by one test would come back as the "sendable" draft in
    # another. Clear it on both sides.
    store.forget_narratives()
    with TestClient(app) as test_client:
        test_client.post("/replay/reset", params={"clear_cover": True})
        yield test_client
        test_client.post("/replay/reset", params={"clear_cover": True})
    store.forget_narratives()
    SESSIONS.clear()


def at(client: TestClient, hhmm: str):
    return client.post("/replay/start", params={"to": hhmm, "narrate": False})


# ------------------------------------------------------------------- control


def test_health_reports_live_sessions(client):
    at(client, "08:30")
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["clock"].endswith("08:30:00")


def test_the_clock_can_be_positioned_for_a_demo(client):
    assert at(client, "08:55").json()["clock"].endswith("08:55:00")
    assert client.get("/board").json()["time"] == "08:55"


def test_stepping_advances_by_hand(client):
    at(client, "08:30")
    client.post("/replay/step", params={"minutes": 25})
    assert client.get("/board").json()["time"] == "08:55"


def test_rewinding_starts_the_morning_over(client):
    """The clock only moves forward, so asking for an earlier time has to
    rebuild the session rather than silently do nothing."""
    at(client, "09:30")
    assert client.get("/alerts").json()["alerts"]
    at(client, "07:45")
    board = client.get("/board").json()
    assert board["time"] == "07:45"
    assert board["totals"]["on_floor"] == 0


def test_reset_clears_this_shift_only(client):
    at(client, "09:00")
    assert query(
        "SELECT count(*) AS n FROM shift_alerts WHERE office = %s", (OFFICE,)
    )[0]["n"] > 0
    client.post("/replay/reset")
    assert query(
        "SELECT count(*) AS n FROM shift_alerts WHERE office = %s", (OFFICE,)
    )[0]["n"] == 0


# ---------------------------------------------------------------------- views


def test_board_carries_the_floor_and_every_queue(client):
    at(client, "08:55")
    board = client.get("/board").json()
    assert board["office"] == OFFICE
    assert board["shift_type"] == SHIFT_TYPE
    assert board["totals"]["rostered"] == 24
    assert len(board["queues"]) == 2
    for queue in board["queues"]:
        assert queue["impact"]["service_level"] is not None
        assert queue["impact"]["day"] is not None
        assert len(queue["riders"]) == queue["rostered"]


def test_board_never_leaks_an_arrival_before_it_happens(client):
    """The replay's central promise, checked at the edge of the system."""
    at(client, "08:00")
    board = client.get("/board").json()
    for queue in board["queues"]:
        for rider in queue["riders"]:
            if rider["eta"]:
                assert rider["eta"] >= "2026-06-11T08:00" or rider["state"] == "DROPPED"
    assert board["totals"]["on_floor"] == 0


def test_alerts_are_persisted_with_an_id(client):
    at(client, "08:55")
    alerts = client.get("/alerts").json()["alerts"]
    assert alerts
    for alert in alerts:
        assert alert["id"]
        assert alert["options"]
        assert alert["context"]
        stored = query("SELECT * FROM shift_alerts WHERE id = %s", (alert["id"],))[0]
        assert stored["queue"] == alert["queue"]
        assert stored["payload_hash"] == alert["payload_hash"]


def test_alerts_survive_a_restart_of_the_session(client):
    """Alerts are the durable record; the session is only a cursor."""
    at(client, "08:55")
    before = {a["queue"]: a["id"] for a in client.get("/alerts").json()["alerts"]}
    SESSIONS.clear()
    at(client, "08:55")
    after = {a["queue"]: a["id"] for a in client.get("/alerts").json()["alerts"]}
    assert before == after, "re-running the morning must not duplicate alert rows"


def test_events_feed_is_ordered_and_filterable(client):
    at(client, "09:30")
    events = client.get("/events").json()["events"]
    assert events
    assert [e["at"] for e in events] == sorted(e["at"] for e in events)
    cutoff = f"{DEMO_DATE}T09:00:00"
    later = client.get("/events", params={"since": cutoff}).json()["events"]
    assert 0 < len(later) < len(events)
    assert all(e["at"] > cutoff for e in later)


# --------------------------------------------------------------------- acting


def test_acting_records_a_decision_and_returns_a_sendable_draft(client):
    at(client, "08:55")
    alert = next(a for a in client.get("/alerts").json()["alerts"] if a["queue"] == "billing")
    response = client.post(
        f"/alerts/{alert['id']}/act", params={"pathway": "EARLY_SHIFT_COVER"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "recorded"
    assert body["people"]
    # The draft has to stand on its own even before any model has run.
    assert alert["queue_name"] in body["draft"]
    assert any(p["name"] in body["draft"] for p in body["people"])

    stored = query("SELECT * FROM alert_actions WHERE alert_id = %s", (alert["id"],))
    assert len(stored) == 1
    assert stored[0]["pathway"] == "EARLY_SHIFT_COVER"


def test_acting_charges_cover_minutes_to_the_people_moved(client):
    at(client, "08:55")
    alert = next(a for a in client.get("/alerts").json()["alerts"] if a["queue"] == "billing")
    body = client.post(
        f"/alerts/{alert['id']}/act", params={"pathway": "EARLY_SHIFT_COVER"}
    ).json()

    moved = {p["stwid"] for p in body["people"]}
    charged = {
        r["stwid"] for r in query("SELECT stwid FROM cover_log WHERE minutes > 0")
    }
    assert moved and moved <= charged


def test_escalating_costs_nobody_their_shift(client):
    """Only people actually moved onto a queue accrue cover minutes. An
    escalation costs attention, not somebody's morning."""
    at(client, "08:55")
    alert = next(a for a in client.get("/alerts").json()["alerts"] if a["queue"] == "billing")
    client.post(f"/alerts/{alert['id']}/act", params={"pathway": "ESCALATE_TRANSPORT"})
    assert query("SELECT count(*) AS n FROM cover_log")[0]["n"] == 0


def test_the_actions_taken_come_back_on_the_alert(client):
    at(client, "08:55")
    alert = next(a for a in client.get("/alerts").json()["alerts"] if a["queue"] == "billing")
    client.post(f"/alerts/{alert['id']}/act", params={"pathway": "HOLD_OVER"})
    refreshed = next(
        a for a in client.get("/alerts").json()["alerts"] if a["id"] == alert["id"]
    )
    assert len(refreshed["actions"]) == 1
    assert refreshed["actions"][0]["pathway"] == "HOLD_OVER"
    assert refreshed["actions"][0]["cost"]["agents_held"] > 0


def test_an_option_that_is_not_offered_is_refused_with_the_ones_that_are(client):
    at(client, "08:55")
    alert = next(a for a in client.get("/alerts").json()["alerts"] if a["queue"] == "billing")
    response = client.post(f"/alerts/{alert['id']}/act", params={"pathway": "CROSS_COVER"})
    assert response.status_code == 409
    assert "EARLY_SHIFT_COVER" in response.json()["detail"]


def test_an_unknown_pathway_is_rejected(client):
    at(client, "08:55")
    alert = client.get("/alerts").json()["alerts"][0]
    assert client.post(
        f"/alerts/{alert['id']}/act", params={"pathway": "TELEPORT"}
    ).status_code == 400


def test_acting_on_another_shifts_alert_is_refused(client):
    """The scoping rule, checked where it matters. An alert id from one shift
    must not be actionable from another."""
    at(client, "08:55")
    alert = client.get("/alerts").json()["alerts"][0]
    response = client.post(
        f"/alerts/{alert['id']}/act",
        params={"pathway": "HOLD_OVER", "shift_date": "2026-06-10"},
    )
    assert response.status_code in {404, 409}


# ---------------------------------------------------------------- multi-tenant


def test_every_view_is_scoped_to_a_tenant_and_site(client):
    at(client, "08:55")
    response = client.get("/board", params={"office": "Denver Office", "shift_type": "11:00"})
    if response.status_code == 200:
        assert response.json()["office"] == "Denver Office"
    else:
        # No roster seeded for that site, which is the correct refusal.
        assert response.status_code == 404


def test_an_unseeded_site_is_a_clean_404(client):
    response = client.get("/board", params={"office": "Nowhere Campus"})
    assert response.status_code == 404
    assert "roster" in response.json()["detail"].lower()


def test_pause_holds_the_clock_and_resume_continues_from_it(client):
    """The pause button was a stub in the first version: it showed a toast and
    the clock kept running. A replay a presenter cannot stop is not a demo."""
    import time

    client.post("/replay/start", params={"speed": 600, "narrate": False})
    time.sleep(0.5)
    paused = client.post("/replay/pause").json()
    assert paused["status"] == "paused"
    held_at = paused["clock"]
    assert held_at > "2026-06-11T07:30:00"

    time.sleep(0.5)
    board = client.get("/board").json()
    assert board["clock"] == held_at, "the clock must not move while paused"
    assert board["running"] is False

    resumed = client.post("/replay/start", params={"speed": 600, "narrate": False}).json()
    assert resumed["status"] == "running"
    assert resumed["clock"] == held_at, "resume must continue from the paused tick"
    client.post("/replay/pause")
