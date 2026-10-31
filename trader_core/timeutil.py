"""IST clock helpers: one fixed offset (+05:30), it never shifts, no tz database."""

import datetime
import re

from trader_core import CLOSE, IST, OPEN, PRE_OPEN


def now_ist():
    """Current time as an aware IST datetime."""
    return datetime.datetime.now(IST)


def to_ist(dt):
    """Convert to IST; naive input is taken as IST already."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def parse_ist(text):
    """Parse ISO-8601 or "YYYY-MM-DD HH:MM" into an aware IST datetime.

    A bare "HH:MM" (an optional trailing " IST", any case) is today's IST
    date at that time; an offset or Z in the input makes it absolute.
    """
    raw = str(text).strip()
    if raw.upper().endswith(" IST"):
        raw = raw[:-4].strip()
    t = hhmm_or_none(raw)
    if t is not None:
        raw = "%s %s" % (now_ist().date().isoformat(), t.strftime("%H:%M"))
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError("cannot parse time: %r" % (text,))
    return to_ist(dt)


def session_dt(day, t):
    """Aware IST datetime for a date and a time of day."""
    return datetime.datetime.combine(day, t, tzinfo=IST)


def day_or_none(text):
    """'YYYY-MM-DD' -> date, or None when it is not a valid date."""
    try:
        return datetime.date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def hhmm_or_none(text):
    """'HH:MM' -> time, or None; hours 0-23 and minutes 0-59 only."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return datetime.time(h, mi)


def fmt_ist(dt, seconds=False):
    """Format as "2026-10-26 10:00 IST" (":%S" appended when seconds is true)."""
    pattern = "%Y-%m-%d %H:%M" + (":%S" if seconds else "")
    return to_ist(dt).strftime(pattern) + " IST"


def session_phase(dt):
    """Time-of-day phase in IST: "closed" | "preopen" | "regular" | "closing".

    preopen 09:00-09:15, regular 09:15-15:20, closing 15:20-15:30, closed otherwise.
    Calendar awareness (weekends, holidays) lives in is_open / the calendar module.
    """
    t = to_ist(dt).time()
    if PRE_OPEN <= t < OPEN:
        return "preopen"
    if OPEN <= t < datetime.time(15, 20):
        return "regular"
    if datetime.time(15, 20) <= t < CLOSE:
        return "closing"
    return "closed"


def is_open(dt):
    """True only during 09:15-15:30 IST on a trading day (no weekend, no NSE holiday)."""
    t = to_ist(dt).time()
    if not (OPEN <= t < CLOSE):
        return False
    from trader_core import calendar as cal

    return cal.is_trading_day(to_ist(dt).date())
