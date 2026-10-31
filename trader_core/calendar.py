"""NSE trading calendar: weekend, holiday, trading-day and next-open helpers."""

import datetime
import json
import os

from trader_core import OPEN
from trader_core.timeutil import to_ist

HOLIDAY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "nse_holidays.json")


def load_holidays(path=None):
    """Load the NSE holiday table as {"YYYY-MM-DD": "name"}; absent or unreadable file yields {}."""
    if path is None:
        path = HOLIDAY_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _as_date(d):
    return d.date() if isinstance(d, datetime.datetime) else d


def is_weekend(d):
    """True for Saturday or Sunday."""
    return _as_date(d).weekday() >= 5


def is_holiday(d):
    """True when the date (YYYY-MM-DD) is in the NSE holiday table."""
    return _as_date(d).isoformat() in load_holidays()


def is_trading_day(d):
    """True for a weekday that is not an NSE holiday."""
    day = _as_date(d)
    return not is_weekend(day) and not is_holiday(day)


def next_open(dt):
    """The next 09:15 IST strictly after dt, skipping weekends and holidays."""
    cur = to_ist(dt)
    day = cur.date()
    candidate = datetime.datetime.combine(day, OPEN, tzinfo=cur.tzinfo)
    if candidate > cur and is_trading_day(day):
        return candidate
    day += datetime.timedelta(days=1)
    while not is_trading_day(day):
        day += datetime.timedelta(days=1)
    return datetime.datetime.combine(day, OPEN, tzinfo=cur.tzinfo)


def trading_days(start, end):
    """Trading dates from start to end, inclusive; [] when end is before start."""
    day = _as_date(start)
    last = _as_date(end)
    days = []
    while day <= last:
        if is_trading_day(day):
            days.append(day)
        day += datetime.timedelta(days=1)
    return days
