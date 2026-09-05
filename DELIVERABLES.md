# Phase-wise Deliverables — Line Manager Agent

Tick a box only when its check passes. Each phase ends with one command or query that proves it.

## Phase 1 — Postgres foundation (1.25 h)

- [x] `db/schema.sql` — tables `trips`, `rider_legs`, `trip_alerts`, `queues`, `roster`, `shift_alerts`, `alert_actions`, `cover_log`; views `v_login_legs`, `v_travel_time`, `v_shift_baseline`, `v_roster_day`; indexes on `(office, shift_type, trip_date)` and `stwid`
- [x] `db/load.py` — CSV to Postgres via COPY; prints rows loaded and rows rejected per table with reason
- [x] `db/seed_roster.py` — 24 primary stwids (09:00) and 24 cover stwids (08:30), two queues, queue params, manager placeholders
- [x] `app/config.py`, `app/db.py` — DB URL, defaults, connection helper
- [x] `.gitignore` — dataset, `.venv`, `agent/.env`, `scratch/`
- [x] `tests/test_phase1.py` — 12 checks on the loaded data and the views

**Check**
```
psql -h 127.0.0.1 -U postgres -d bessemer -Atc "select count(*) from v_login_legs where office='Clearwater Campus' and shift_type='09:00'"
```
Returns 30,884 (39k before the ad-hoc and non-staff filters). `select count(*) from roster` returns 48. `uv run pytest tests/test_phase1.py` passes 12 tests.

## Phase 2 — Reasoning core (2 h)

- [x] `app/core/state.py`, `app/core/eta.py` — 9-state rider machine; ETA = own planned travel + median overshoot, with a spread band and a deterministic `OVERDUE` signal
- [x] `app/core/queue.py`, `app/core/context.py` — per-queue headcount and impact as a range; five benchmarks including peer sites and structural slack
- [x] `app/core/sla.py` — Erlang C service level, speed of answer, abandonment, occupancy, schedule adherence, daily rollup, `agents_needed`; every option scored on the contract
- [x] `tests/test_sla.py` — 22 checks on the queueing maths and the escalation trigger
- [x] `app/core/alerts.py` — `Cause` enum, three triggers, `OPEN -> UPDATED -> RESOLVED` with hysteresis, priced options
- [x] `app/core/remediation.py` — hold-over cost, cover candidates verified on the floor, ranked by cover minutes
- [x] `app/replay.py` — clock, event feed, board, CLI
- [x] `tests/test_core.py` — 28 checks, each naming the mistake it guards against
- [x] `samples/alert_payload.json`, `samples/board_0855.json`, `samples/replay_2026-06-11.txt`

**Check**
```
uv run pytest tests/test_core.py
uv run python -m app.replay --date 2026-06-11 --print > samples/replay_2026-06-11.txt
```
All 62 tests pass. Billing opens at 08:30 and resolves at 09:45; tech support opens 08:15, resolves 10:00. No model call anywhere. A 150-tick morning needs 11 narratives across both queues.

## Phase 3 — Replay engine + API (1 h)

- [x] `app/store.py` — alert upsert, narrative cache lookup, action audit trail, scoped reset
- [x] `app/api.py` — `/replay/start` (with `to=HH:MM`), `/replay/reset`, `/replay/step`, `/board`, `/alerts`, `/events`, `/alerts/{id}/act`, `/chat` (stub), `/health`
- [x] `Scope` dependency — every endpoint scoped by business unit, office, date and shift
- [x] Fallback drafts — a sendable message per pathway even with no model running
- [x] Connection pooling and per-shift caching — replay went from 18.3s to 2.29s, 15ms per tick
- [x] `tests/test_api.py` — 19 checks on persistence, scoping and the act path
- [x] `samples/api_board_0855.json`, `api_alerts_0855.json`, `api_action.json`, `api_events.json`

**Check**
```
uv run uvicorn app.api:app --port 8000 &
curl -s -X POST "localhost:8000/replay/start?to=08:55"
curl -s localhost:8000/board | head -c 400
psql -h 127.0.0.1 -U postgres -d bessemer -Atc "select queue, status, opened_at from shift_alerts"
```
Board returns mid-replay. Two alert rows, billing opening 08:25 and tech support 08:05. All 81 tests pass in under 15 seconds.

## Phase 4 — ADK agent (1.25 h)

- [x] `app/sessions.py` — shared replay registry and the context var that binds a shift to the agent's tools
- [x] `agent/tools.py` — 7 tools; errors returned as data; scope not settable by the model
- [x] `agent/agent.py` — `narrator_agent` (2 tools) and `root_agent` (7 tools) on `openai/gpt-5.6-luna` via ADK's LiteLLM adapter
- [x] `agent/runner.py` — ADK runner, `DatabaseSessionService` on the same Postgres, metered usage
- [x] Narrative cache by payload hash; throwaway conversation per write-up
- [x] `/alerts/{id}/narrate`, `/chat`, `/usage` endpoints
- [x] `tests/test_agent.py` — 17 offline checks plus 3 `live` ones behind a marker
- [x] `samples/api_alerts_narrated.json`, `api_chat.json`, `usage.json`

**Check**
```
uv run pytest tests/ -m "not live"     # 98 pass, no spend
uv run pytest tests/test_agent.py -m live   # 3 real model calls
curl -s -X POST localhost:8000/alerts/1/narrate | python3 -m json.tool
curl -s localhost:8000/usage
```
Narrative present and naming only real people. Chat answers from the board. About 5,000 tokens and 9 seconds per alert, down from 22,600 in the first working version.

## Phase 5 — UI, demo, deliverables (2.25 h)

- [x] `web/index.html` — the board in MoveInSync's palette and type: clock, queue cards with service level, rider table, every open alert with options as buttons, sent log, chat. One file, served at `/`.
- [x] `README.md` — setup, run, six beats, architecture diagram, what is real, findings, limitations, quirks handled
- [x] `docs/deck.md` — twelve slides plus the demo
- [x] `samples/screens/` — desktop, alert panel, mobile
- [x] Narration detached from the clock so the replay never freezes for a model call
- [x] Live run at 180x: full morning, alerts narrated, zero failures
- [x] Pause that actually pauses, and resume from the same tick
- [x] `db/seed_story.py` — the designed morning: five beats, each a different cause and pathway, in the dataset's schema
- [x] Live switch: live runs compute and call the model fresh at every checkpoint, clock paused while it writes; playback reads the recording of the last live run
- [x] `app/recording.py` — every live tick captured and saved to Postgres; landmarks derived from the feed
- [x] Streaming: narrator writes plain text so it streams; one server-sent event stream carries clock, phase and tokens; alerts capped at 45 words
- [x] Classification fixes the story exposed: affected set on the median, `LATE_PICKUP` for a cab running behind, vendors only blamed for transport-caused lateness, permanent absence priced across the shift
- [x] "On the way" bucket fixed: counts now add up to the roster at every minute
- [x] Narrative memo moved to its own table so Start over does not throw it away
- [x] `schema.sql` brought back in line with the live database (night role, synthetic flag, action cost)
- [x] `tests/test_recording.py` — 7 checks including the five story beats

**Check**: a teammate who has not seen the code follows the README and reaches beat 4 in under 5 minutes. `uv run pytest tests/ -m "not live"` passes 109.

## Cut order if behind

1. `/chat` and the chat box (the alert write-up survives without it)
2. `CROSS_COVER` pathway
3. "Same weekday last week" context fact
4. Event feed and speed control
5. `trip_alerts` table

Never cut: the alert, the cover draft, the payload cache, the README.
