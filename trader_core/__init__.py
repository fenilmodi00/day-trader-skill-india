"""Shared core for the India port: IST time, NSE calendar, config, Upstox data and orders."""

import datetime

__version__ = "3.0.0"

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
PRE_OPEN = datetime.time(9, 0)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
