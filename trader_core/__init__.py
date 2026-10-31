"""Shared core for the India port: IST time, NSE calendar, config, Upstox data and orders."""

import datetime

__version__ = "3.0.0"

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
PRE_OPEN = datetime.time(9, 0)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)

from trader_core import calendar, config, timeutil
from trader_core.calendar import (
    is_holiday,
    is_trading_day,
    is_weekend,
    load_holidays,
    next_open,
    trading_days,
)
from trader_core.timeutil import fmt_ist, is_open, now_ist, parse_ist, session_phase, to_ist
