"""DataSource seam: the protocol every data backend implements (T2 spec).

The Broker protocol lives in trader_core.broker; this is its market-data twin.
Skills consume this seam only, so tests inject fakes and recorded fixtures and
never touch the network. trader_core.upstox.Client implements it.
"""

import abc


class DataSource(abc.ABC):
    """The seam every market-data backend implements. Upstox instrument keys in,
    plain dicts and bar lists out.

    quote(keys, fields)     batch quotes -> {instrument_key: bar}; keys missing
                            from the response stay absent, never invented.
    candles(key, interval,  historical bars for 1m/30m/day/week/month ->
           start, end)      [{dt, o, h, l, c, v, oi}] on aware IST stamps.
    intraday(key, interval) current-day bars for 1m/5m/30m (5m resampled from 1m).
    """

    @abc.abstractmethod
    def quote(self, keys, fields="ohlc"):
        ...

    @abc.abstractmethod
    def candles(self, instrument_key, interval="1m", start=None, end=None):
        ...

    @abc.abstractmethod
    def intraday(self, instrument_key, interval="5m"):
        ...
