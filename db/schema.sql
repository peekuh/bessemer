-- Shift Readiness Agent — schema
-- Postgres 18. Idempotent: safe to re-run.
--
-- Two groups of tables:
--   dataset/*  loaded from the MoveInSync CSVs, never edited by the app
--   app/*      written by the agent and the manager
--
-- Timestamps are stored as `timestamp` (no zone). The dataset's epochs align
-- with its own `shift_type` clock with no offset, so we treat that clock as
-- local wall time throughout. Converting to UTC would only add a constant.

BEGIN;

-- ---------------------------------------------------------------- dataset

DROP VIEW IF EXISTS v_shift_baseline CASCADE;
DROP VIEW IF EXISTS v_travel_time CASCADE;
DROP VIEW IF EXISTS v_login_legs CASCADE;

CREATE TABLE IF NOT EXISTS trips (
    trip_id         bigint PRIMARY KEY,
    business_unit   text        NOT NULL,
    office          text        NOT NULL,
    product_type    text,
    trip_date       date        NOT NULL,
    shift_type      text        NOT NULL,
    trip_direction  text        NOT NULL,          -- LOGIN | LOGOUT
    vendor_id       text,
    planned_start   timestamp,
    planned_end     timestamp,
    actual_start    timestamp,
    actual_end      timestamp,
    delay_reason    text,                          -- NODELAY | TRAFFIC | DRIVER | EMPLOYEE
    delay_minutes   integer,
    trip_nodal      text,                          -- NODAL | HOME | SHUTTLE | null
    planned_cnt     integer,
    actual_cnt      integer,
    noshow_cnt      integer
);

CREATE TABLE IF NOT EXISTS rider_legs (
    id                  bigserial PRIMARY KEY,
    trip_id             bigint      NOT NULL,
    stwid               bigint      NOT NULL,
    business_unit       text        NOT NULL,
    office              text        NOT NULL,
    trip_date           date        NOT NULL,
    shift_type          text        NOT NULL,
    planned_pickup      timestamp,
    planned_drop        timestamp,
    actual_pickup       timestamp,
    actual_drop         timestamp,
    planned_km          double precision,          -- negatives nulled at load
    traveled_km         double precision,
    signintype          text,                      -- Planned | Adhoc | Guest
    gender              text,
    emp_role            text,
    boarding_status     text,                      -- Boarded | Not Boarded
    not_boarding_reason text,                      -- NO_SHOW | TRIP_CANCELLED_FROM_DASHBOARD | ...
    is_no_show          boolean
);

CREATE TABLE IF NOT EXISTS trip_alerts (
    event_id      uuid PRIMARY KEY,
    business_unit text,
    trip_id       bigint,
    stwid         bigint,                          -- 0 = trip-level, not a rider
    event_type    text,
    start_time    timestamp,
    ack_time      timestamp,
    state_text    text,
    severity      text,                            -- stray literal "False" nulled at load
    source        text
);

CREATE INDEX IF NOT EXISTS ix_trips_office_shift_date
    ON trips (office, shift_type, trip_date);
CREATE INDEX IF NOT EXISTS ix_trips_bu_date
    ON trips (business_unit, trip_date);
CREATE INDEX IF NOT EXISTS ix_legs_office_shift_date
    ON rider_legs (office, shift_type, trip_date);
CREATE INDEX IF NOT EXISTS ix_legs_stwid
    ON rider_legs (stwid);
CREATE INDEX IF NOT EXISTS ix_legs_trip
    ON rider_legs (trip_id);
CREATE INDEX IF NOT EXISTS ix_trip_alerts_trip
    ON trip_alerts (trip_id);

-- ---------------------------------------------------------------- app

CREATE TABLE IF NOT EXISTS queues (
    queue             text PRIMARY KEY,
    display_name      text    NOT NULL,
    business_unit     text    NOT NULL,
    office            text    NOT NULL,
    shift_type        text    NOT NULL,
    aht_min           numeric NOT NULL,            -- average handle time, minutes
    calls_per_30min   integer NOT NULL,            -- forecast volume
    sl_target         text    NOT NULL,            -- e.g. '80/20'
    line_manager      text    NOT NULL,
    early_shift_lead  text    NOT NULL,
    transport_manager text    NOT NULL
);

-- Who sits on which queue.
-- role='primary' -> rostered on the 09:00 shift, the team we watch. Real riders.
-- role='cover'   -> rostered on the 08:30 shift, the substitute pool. Real riders.
-- role='night'   -> the 01:00-09:00 shift the day team relieves. Synthetic,
--                   and flagged as such, because the trip log has no night shift
--                   at this site to read.
CREATE TABLE IF NOT EXISTS roster (
    stwid         bigint PRIMARY KEY,
    display_name  text    NOT NULL,
    business_unit text    NOT NULL,
    office        text    NOT NULL,
    shift_type    text    NOT NULL,
    queue         text    NOT NULL REFERENCES queues (queue),
    role          text    NOT NULL CHECK (role IN ('primary', 'cover', 'night')),
    synthetic     boolean NOT NULL DEFAULT false,
    shift_ends    time
);

CREATE INDEX IF NOT EXISTS ix_roster_queue_role ON roster (queue, role);

CREATE TABLE IF NOT EXISTS shift_alerts (
    id            bigserial PRIMARY KEY,
    business_unit text      NOT NULL,
    office        text      NOT NULL,
    shift_date    date      NOT NULL,
    shift_type    text      NOT NULL,
    queue         text      NOT NULL,
    status        text      NOT NULL CHECK (status IN ('OPEN', 'UPDATED', 'RESOLVED')),
    cause         text      NOT NULL,
    payload       jsonb     NOT NULL,
    payload_hash  text      NOT NULL,
    narrative     text,
    drafts        jsonb,
    opened_at     timestamp NOT NULL,              -- replay clock, not wall clock
    updated_at    timestamp,
    resolved_at   timestamp,
    UNIQUE (office, shift_date, shift_type, queue)
);

CREATE INDEX IF NOT EXISTS ix_shift_alerts_hash ON shift_alerts (payload_hash);

CREATE TABLE IF NOT EXISTS alert_actions (
    id         bigserial PRIMARY KEY,
    alert_id   bigint    NOT NULL REFERENCES shift_alerts (id) ON DELETE CASCADE,
    pathway    text      NOT NULL,
    draft      text,
    candidates jsonb,
    cost       jsonb,
    sent_at    timestamp NOT NULL
);

-- Prose written for a situation, keyed by the situation rather than the shift.
-- A morning with the same cause, severity band and recommendation as one
-- already written up gets that text for free. Lives apart from shift_alerts so
-- that resetting a shift for a demo re-run does not throw the memo away.
CREATE TABLE IF NOT EXISTS narrative_cache (
    payload_hash text PRIMARY KEY,
    narrative    text      NOT NULL,
    drafts       jsonb,
    written_at   timestamp NOT NULL DEFAULT now()
);

-- The last live run, tick by tick, for playback. One row per shift.
CREATE TABLE IF NOT EXISTS recordings (
    key           text PRIMARY KEY,
    business_unit text NOT NULL,
    office        text NOT NULL,
    shift_date    date NOT NULL,
    shift_type    text NOT NULL,
    saved_at      timestamp NOT NULL DEFAULT now(),
    payload       jsonb NOT NULL
);

-- Synthetic fairness counter: how many cover minutes each rider has absorbed
-- this ISO week. Starts empty; incremented when the manager acts on an alert.
CREATE TABLE IF NOT EXISTS cover_log (
    stwid    bigint  NOT NULL,
    iso_week text    NOT NULL,                     -- e.g. '2026-W24'
    minutes  integer NOT NULL DEFAULT 0,
    PRIMARY KEY (stwid, iso_week)
);

-- ---------------------------------------------------------------- views

-- Inbound legs only, cleaned to the population the line manager cares about.
-- Excludes escorts/vendor staff, ad-hoc and guest rides (no planned baseline
-- to be late against), and the 'Non Shift' / 'Adhoc' shift labels.
CREATE VIEW v_login_legs AS
SELECT
    l.id,
    l.trip_id,
    l.stwid,
    l.business_unit,
    l.office,
    l.trip_date,
    l.shift_type,
    (l.trip_date + l.shift_type::time)             AS shift_start,
    l.planned_pickup,
    l.planned_drop,
    l.actual_pickup,
    l.actual_drop,
    l.boarding_status,
    l.not_boarding_reason,
    l.is_no_show,
    l.emp_role,
    t.vendor_id,
    t.trip_nodal,
    t.planned_start,
    t.actual_start,
    t.delay_reason,
    t.delay_minutes,
    EXTRACT(EPOCH FROM (l.actual_drop - (l.trip_date + l.shift_type::time))) / 60.0
                                                   AS late_for_shift_min,
    EXTRACT(EPOCH FROM (l.actual_drop - l.actual_pickup)) / 60.0
                                                   AS travel_min,
    EXTRACT(EPOCH FROM (t.actual_start - t.planned_start)) / 60.0
                                                   AS cab_start_delay_min
FROM rider_legs l
JOIN trips t ON t.trip_id = l.trip_id
WHERE t.trip_direction = 'LOGIN'
  AND l.stwid <> 0
  AND l.emp_role IN ('employee', 'projectmgr')
  AND l.signintype = 'Planned'
  AND l.shift_type ~ '^[0-9]{2}:[0-9]{2}$';

-- Historical travel time by site, pickup type and half-hour of day.
--
-- Two measures, because the ETA projection prefers the first and falls back
-- to the second:
--
--   excess_*  actual travel minus the rider's OWN planned travel. The plan is
--             per-rider and known in advance, and it correlates 0.82 with the
--             actual, so "plan + typical overshoot" is far more discriminating
--             than one site-wide number. Median overshoot is ~0, p75 ~9 min.
--   p*_min    raw pickup->drop minutes, used when planned_drop is missing.
--
-- The projection reads p75 rather than the median on purpose: without GPS an
-- en-route delay is invisible until the drop lands, so we lean pessimistic.
CREATE VIEW v_travel_time AS
SELECT
    office,
    COALESCE(trip_nodal, 'UNKNOWN')                                   AS trip_nodal,
    (EXTRACT(HOUR FROM actual_pickup) * 2
        + FLOOR(EXTRACT(MINUTE FROM actual_pickup) / 30))::int         AS pickup_halfhour,
    COUNT(*)                                                           AS n,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY travel_min)           AS p50_min,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY travel_min)           AS p75_min,
    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY travel_min)           AS p90_min,
    COUNT(*) FILTER (WHERE planned_travel_min IS NOT NULL)             AS n_excess,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY excess_min)           AS excess_p50,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY excess_min)           AS excess_p75,
    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY excess_min)           AS excess_p90
FROM (
    SELECT
        office,
        trip_nodal,
        actual_pickup,
        travel_min,
        EXTRACT(EPOCH FROM (planned_drop - planned_pickup)) / 60.0     AS planned_travel_min,
        travel_min - EXTRACT(EPOCH FROM (planned_drop - planned_pickup)) / 60.0
                                                                       AS excess_min
    FROM v_login_legs
    WHERE actual_pickup IS NOT NULL
      AND actual_drop IS NOT NULL
      AND travel_min BETWEEN 0 AND 240
) s
GROUP BY 1, 2, 3;

-- How this site/shift normally behaves, by weekday. This is the
-- contextualisation reference point: "21 late is bad against a typical 14".
-- Grace window is 5 minutes past shift start.
CREATE VIEW v_shift_baseline AS
SELECT
    business_unit,
    office,
    shift_type,
    TO_CHAR(trip_date, 'Dy')                                          AS dow,
    COUNT(DISTINCT trip_date)                                         AS days,
    COUNT(*)                                                          AS legs,
    AVG(CASE WHEN late_for_shift_min > 5 THEN 1.0 ELSE 0.0 END)       AS late_share,
    AVG(CASE WHEN boarding_status = 'Not Boarded' THEN 1.0 ELSE 0.0 END) AS absent_share,
    PERCENTILE_CONT(0.50) WITHIN GROUP (
        ORDER BY late_for_shift_min) FILTER (WHERE actual_drop IS NOT NULL)
                                                                      AS median_late_min
FROM v_login_legs
GROUP BY 1, 2, 3, 4;

-- One row per rostered rider per date: the leg that actually brings them in
-- for their rostered shift.
--
-- A handful of riders have two inbound legs on the same day (a second trip
-- later in the morning). Ranking by how close the planned drop lands to the
-- rider's rostered shift start picks the commute that matters and ignores the
-- rest, so downstream code can assume one row per rider per day.
--
-- Riders with no leg at all on a date do not appear here. The replay treats a
-- missing row as "not scheduled today", which is different from a no-show.
CREATE VIEW v_roster_day AS
SELECT DISTINCT ON (r.stwid, l.trip_date)
    r.stwid,
    r.display_name,
    r.queue,
    r.role,
    r.business_unit,
    r.office,
    r.shift_type                                                      AS rostered_shift,
    l.trip_date,
    (l.trip_date + r.shift_type::time)                                AS shift_start,
    l.trip_id,
    l.shift_type                                                      AS leg_shift,
    l.planned_pickup,
    l.planned_drop,
    l.actual_pickup,
    l.actual_drop,
    l.planned_start,
    l.actual_start,
    l.boarding_status,
    l.not_boarding_reason,
    l.is_no_show,
    l.vendor_id,
    l.trip_nodal,
    l.delay_reason,
    l.delay_minutes,
    EXTRACT(EPOCH FROM (l.planned_drop - l.planned_pickup)) / 60.0     AS planned_travel_min,
    EXTRACT(EPOCH FROM (l.actual_drop - (l.trip_date + r.shift_type::time))) / 60.0
                                                                      AS late_for_shift_min
FROM roster r
JOIN v_login_legs l
  ON l.stwid = r.stwid
 AND l.office = r.office
ORDER BY
    r.stwid,
    l.trip_date,
    ABS(EXTRACT(EPOCH FROM (
        COALESCE(l.planned_drop, l.actual_drop) - (l.trip_date + r.shift_type::time)
    )));

COMMIT;
