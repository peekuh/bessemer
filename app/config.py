"""Central configuration. Every value here can be overridden by an env var."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = ROOT / "MoveInSync - Anonymised Trip-Log Dataset"
SAMPLES_DIR = ROOT / "samples"

# --- database ---------------------------------------------------------------

PGHOST = os.getenv("PGHOST", "127.0.0.1")
PGPORT = os.getenv("PGPORT", "5432")
PGUSER = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")
PGDATABASE = os.getenv("PGDATABASE", "bessemer")

DSN = os.getenv(
    "BESSEMER_DSN",
    f"host={PGHOST} port={PGPORT} user={PGUSER} dbname={PGDATABASE}"
    + (f" password={PGPASSWORD}" if PGPASSWORD else ""),
)

# SQLAlchemy-style URL, for ADK's DatabaseSessionService.
SQLALCHEMY_URL = os.getenv(
    "BESSEMER_SQLALCHEMY_URL",
    f"postgresql+psycopg://{PGUSER}"
    + (f":{PGPASSWORD}" if PGPASSWORD else "")
    + f"@{PGHOST}:{PGPORT}/{PGDATABASE}",
)

# --- the slice we build for -------------------------------------------------
#
# One tenant, one site, one shift. Every query is scoped by business_unit and
# office, so pointing the app at another tenant is a config change, not a code
# change. That is the multi-tenancy claim, made true.

BUSINESS_UNIT = os.getenv("BESSEMER_BU", "pinnacle-Slc")
OFFICE = os.getenv("BESSEMER_OFFICE", "Clearwater Campus")
SHIFT_TYPE = os.getenv("BESSEMER_SHIFT", "09:00")
COVER_SHIFT_TYPE = os.getenv("BESSEMER_COVER_SHIFT", "08:30")
DEMO_DATE = os.getenv("BESSEMER_DEMO_DATE", "2026-08-06")
"""The designed morning from db/seed_story.py. Set to 2026-06-11 for the real one."""

# --- the operating model ----------------------------------------------------
#
# Clearwater Campus runs a 24/7 enterprise support centre on three shifts:
#
#     01:00 - 09:00   night
#     09:00 - 17:00   day     <- the shift we watch
#     17:00 - 01:00   evening
#
# This part is synthetic. The dataset carries commutes, not org charts, and
# Clearwater's real trip log shows no outbound legs anywhere near 09:00, so
# there is no night shift in the data to read. We assert one because it is what
# makes the problem the brief describes actually bite.
#
# The rule that matters is **positional handover**: a night agent cannot leave
# their desk until their relief is seated. On a 24/7 desk the queue is never
# allowed to go unmanned, so a late arrival does not simply thin the floor. It
# pins a colleague who has already worked eight hours to their chair, and every
# minute of that is paid at overtime and comes off the back of a night shift.
#
# That is the ripple the line manager actually feels, and it is why the same
# twenty minutes of traffic costs this site more than it would cost a nine-to-
# five office.

NIGHT_SHIFT_TYPE = os.getenv("BESSEMER_NIGHT_SHIFT", "01:00")
NIGHT_SHIFT_ENDS = os.getenv("BESSEMER_NIGHT_SHIFT_ENDS", "09:00")

HANDOVER_REQUIRED = os.getenv("BESSEMER_HANDOVER", "1") != "0"
"""Whether a position must stay manned through the handover. True on a 24/7
desk, false for an office that simply opens at nine."""

OVERTIME_MULTIPLIER = float(os.getenv("BESSEMER_OT_MULTIPLIER", "1.5"))
OVERTIME_RATE_PER_HOUR = float(os.getenv("BESSEMER_OT_RATE", "420"))
"""Base hourly cost of an agent, in the site's currency. Synthetic."""

NIGHT_OUTBOUND_CAB = os.getenv("BESSEMER_NIGHT_OUTBOUND", "09:15")
"""When the night shift's cabs home are booked. Hold an agent past this and
they miss their ride, which turns twenty minutes of overtime into a two-hour
problem for one person."""

NIGHT_POOL_SIZE = int(os.getenv("BESSEMER_NIGHT_POOL_SIZE", "16"))

# --- operational thresholds -------------------------------------------------

GRACE_MIN = int(os.getenv("BESSEMER_GRACE_MIN", "5"))
"""Minutes past shift start still counted as on time."""

NO_PICKUP_GRACE_MIN = int(os.getenv("BESSEMER_NO_PICKUP_GRACE_MIN", "5"))
"""Minutes past planned pickup with no pickup before we suspect a no-show."""

STAFFING_FLOOR = float(os.getenv("BESSEMER_STAFFING_FLOOR", "0.75"))
"""Projected queue staffing at shift start below which an alert fires."""

LATE_ALERT_MIN = int(os.getenv("BESSEMER_LATE_ALERT_MIN", "15"))
"""A single rider projected this many minutes past shift start fires an alert."""

TEAM_SIZE = int(os.getenv("BESSEMER_TEAM_SIZE", "24"))
COVER_POOL_SIZE = int(os.getenv("BESSEMER_COVER_POOL_SIZE", "24"))
MIN_RIDE_DAYS = int(os.getenv("BESSEMER_MIN_RIDE_DAYS", "40"))
"""A rider must appear on this many days to count as rostered to the shift."""
