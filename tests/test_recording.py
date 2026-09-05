"""Live runs are captured; playback reads the capture and nothing else."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import store
from app.api import app
from app.config import DEMO_DATE, OFFICE
from app.sessions import SESSIONS


@pytest.fixture()
def client():
    SESSIONS.clear()
    store.forget_narratives()
    with TestClient(app) as c:
        c.post("/replay/reset", params={"clear_cover": True, "forget_recording": True})
        yield c
        c.post("/replay/reset", params={"clear_cover": True, "forget_recording": True})
    SESSIONS.clear()


def jump(client, hhmm):
    return client.post("/replay/start", params={"to": hhmm, "narrate": False}).json()


def test_playback_refuses_until_something_has_been_recorded(client):
    assert client.get("/at", params={"t": "08:55"}).status_code == 409


def test_a_live_jump_records_every_minute_it_passed_through(client):
    jump(client, "08:55")
    early = client.get("/at", params={"t": "08:05"}).json()
    late = client.get("/at", params={"t": "08:55"}).json()
    assert early["time"] == "08:05" and late["time"] == "08:55"
    assert early["board"]["totals"]["on_floor"] <= late["board"]["totals"]["on_floor"]


def test_playback_is_what_the_live_clock_showed(client):
    jump(client, "08:55")
    live = client.get("/board").json()
    played = client.get("/at", params={"t": "08:55"}).json()["board"]
    assert played["totals"] == live["totals"]


def test_the_recording_survives_a_restart(client):
    jump(client, "08:30")
    SESSIONS.clear()  # a new process would start here
    snap = client.get("/at", params={"t": "08:30"}).json()
    assert snap["time"] == "08:30"
    assert snap["board"]["totals"]["rostered"] == 24


def test_landmarks_come_from_the_feed(client):
    jump(client, "10:00")
    marks = client.get("/landmarks").json()
    labels = [m["label"] for m in marks["landmarks"]]
    assert "Shift starts" in labels
    assert any("alert opens" in l for l in labels)
    assert any("back to strength" in l for l in labels)


def test_acting_from_playback_is_stamped_with_that_minute(client):
    jump(client, "09:30")
    snap = client.get("/at", params={"t": "08:55"}).json()
    billing = next(a for a in snap["alerts"] if a["queue"] == "billing")
    done = client.post(f"/alerts/{billing['id']}/act",
                       params={"pathway": "EARLY_SHIFT_COVER", "at": "08:55"}).json()
    assert done["status"] == "recorded"
    assert done["sent_at"].endswith("08:55:00")
    before = client.get("/at", params={"t": "08:50"}).json()
    after = client.get("/at", params={"t": "09:00"}).json()
    assert sum(len(a["actions"]) for a in before["alerts"]) == 0
    assert sum(len(a["actions"]) for a in after["alerts"]) == 1


def test_the_story_beats_hold_on_the_designed_morning(client):
    """The five checkpoints, asserted against the deterministic core."""
    jump(client, "09:30")

    def alerts_at(t):
        return {a["queue"]: a for a in client.get("/at", params={"t": t}).json()["alerts"]}

    quiet = client.get("/at", params={"t": "07:30"}).json()
    assert not quiet["alerts"]

    a0805 = alerts_at("08:05")
    assert a0805["billing"]["cause"] == "CAB_NOT_STARTED"
    assert a0805["billing"]["coverage_pct"] == 67
    assert "techsupport" not in a0805
    vendors = [p.get("vendor") for o in a0805["billing"]["options"] for p in o["people"] if p.get("vendor")]
    assert vendors == ["Karan Mikhailov Travel"]

    a0830 = alerts_at("08:30")
    assert a0830["techsupport"]["cause"] == "ABSENCE"
    assert any(o["pathway"] == "ESCALATE_OPS" and o["urgent"] for o in a0830["techsupport"]["options"])
    assert not a0830["techsupport"]["impact"]["day"]["meets_target"]

    a0855 = alerts_at("08:55")
    assert a0855["billing"]["impact"]["service_level"]["service_level_pct"] < 30
    assert a0855["billing"]["impact"]["day"]["meets_target"]

    a0930 = alerts_at("09:30")
    assert a0930["billing"]["status"] == "RESOLVED"
    assert a0930["techsupport"]["status"] != "RESOLVED"
