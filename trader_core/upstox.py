"""Upstox data client: auth, batch quotes, historical and intraday candles, resample."""

import datetime
import json
import os
import time
from urllib.parse import quote

from trader_core import CLOSE, IST, OPEN
from trader_core import config as cfgmod
from trader_core.datasource import DataSource
from trader_core.http import (LIVE, SANDBOX, SandboxUnsupported, UpstoxAPIError, UpstoxAuthError,
                              UpstoxError, default_transport, raise_for_response)
from trader_core import instruments as instruments_mod

QUOTE_CHUNK = 500
SESSION_MINUTES = 375  # 09:15 to 15:30
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/%s.json.gz"
MARKET_DATA_PATHS = ("/market-quote", "/historical-candle", "/market-data", "/market-instrument",
                     "/v2/market-quote", "/v3/historical-candle", "/v2/market-data")

# v3 historical endpoint: (unit, step) for each supported interval
_INTERVALS = {
    "1m": ("minutes", "1"),
    "30m": ("minutes", "30"),
    "day": ("days", "1"),
    "week": ("weeks", "1"),
    "month": ("months", "1"),
}
# documented default lookback windows in days
_WINDOWS = {"1m": 30, "30m": 365, "day": 365, "week": 3650, "month": 3650}


def _f(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value):
    """ISO-8601 (or datetime) to an aware IST datetime."""
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, datetime.date):
        dt = datetime.datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _as_date(value, name):
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise UpstoxError("%s must be a date or a YYYY-MM-DD string, got %r" % (name, value))


def _in_session(dt):
    return OPEN <= dt.time() < CLOSE


def _normalize_rows(rows, session_filter=True):
    """[ts,o,h,l,c,v,oi] rows to pinned bar dicts: aware IST, floats, sorted."""
    bars = []
    for row in rows or []:
        if not isinstance(row, list) or len(row) < 5:
            continue
        o, h, low, c = row[1:5]
        if o is None or h is None or low is None or c is None:
            continue
        try:
            dt = _parse_ts(row[0])
        except (ValueError, TypeError):
            continue
        if session_filter and not _in_session(dt):
            continue
        bars.append({
            "dt": dt,
            "o": float(o),
            "h": float(h),
            "l": float(low),
            "c": float(c),
            "v": _f(row[5]) if len(row) > 5 else None,
            "oi": _f(row[6]) if len(row) > 6 else None,
        })
    bars.sort(key=lambda bar: bar["dt"])
    return bars


def _merge_bucket(group, day, start_t):
    volumes = [bar.get("v") for bar in group if bar.get("v") is not None]
    open_interests = [bar.get("oi") for bar in group if bar.get("oi") is not None]
    return {
        "dt": datetime.datetime.combine(day, start_t, IST),
        "o": group[0]["o"],
        "h": max(bar["h"] for bar in group),
        "l": min(bar["l"] for bar in group),
        "c": group[-1]["c"],
        "v": float(sum(volumes)) if volumes else None,
        "oi": float(sum(open_interests)) if open_interests else None,
    }


def resample(bars, minutes=5):
    """Session-aware 1m to Nm resample. Buckets are stamped at their start time
    (09:15 first, 15:25 last, 75 per complete 5m session). A bucket is kept only
    when all N minutes are present: empty buckets and partial buckets (late feed
    at the session start, gaps, after 15:30) are dropped, never fabricated."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise UpstoxError("resample minutes must be an int, got %r" % (minutes,))
    if minutes <= 0:
        raise UpstoxError("resample minutes must be positive, got %d" % minutes)
    if SESSION_MINUTES % minutes != 0:
        raise UpstoxError("resample minutes must divide the %d minute session, %d does not"
                          % (SESSION_MINUTES, minutes))
    by_day = {}
    for bar in bars:
        dt = bar["dt"]
        if not _in_session(dt):
            continue
        by_day.setdefault(dt.date(), []).append(bar)
    out = []
    for day in sorted(by_day):
        day_bars = sorted(by_day[day], key=lambda bar: bar["dt"])
        open_offset = OPEN.hour * 60 + OPEN.minute
        buckets = {}
        for bar in day_bars:
            offset = bar["dt"].hour * 60 + bar["dt"].minute - open_offset
            start = offset - (offset % minutes)
            hh, mm = divmod(open_offset + start, 60)
            buckets.setdefault(datetime.time(hh, mm), []).append(bar)
        for start_t in sorted(buckets):
            group = buckets[start_t]
            if len(group) != minutes:
                continue
            out.append(_merge_bucket(group, day, start_t))
    return out


class Client(DataSource):
    """Upstox REST client. Market data always uses the live host (the sandbox
    serves orders only); trading uses the sandbox host in sandbox mode."""

    def __init__(self, cfg=None, transport=None, token=None, mode=None):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self._transport = transport or default_transport
        self._explicit_token = token
        self.mode = mode or self.cfg.get("mode") or "sandbox"
        if self.mode not in ("sandbox", "live"):
            raise UpstoxError("client mode must be 'sandbox' or 'live', got %r" % (self.mode,))
        self.trading_base = SANDBOX if self.mode == "sandbox" else LIVE
        data_cfg = self.cfg.get("data") or {}
        try:
            self._timeout = int(data_cfg.get("timeout_s", 30))
        except (TypeError, ValueError):
            self._timeout = 30
        rate = _f(data_cfg.get("rate_limit_per_sec"))
        self._min_interval = (1.0 / rate) if rate and rate > 0 else 0.0
        self._last_request = 0.0

    # ---- auth ----

    def _token_from_file(self, name):
        """tokens.json holds sandbox/access/analytics tokens only, never a PIN,
        TOTP secret, or API secret; unknown keys are ignored by construction.
        An empty state_dir means the same default config.state_dir resolves:
        <repo>/trader/state, so the file is read by default."""
        state = self.cfg.get("state_dir") or os.path.join(cfgmod.REPO_ROOT, "trader", "state")
        try:
            with open(os.path.join(str(state), "tokens.json"), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        value = doc.get(name) if isinstance(doc, dict) else None
        return value if isinstance(value, str) and value else None

    def _token(self, purpose):
        if self._explicit_token:
            return self._explicit_token
        env = os.environ
        if purpose == "market":
            token = (env.get("UPSTOX_ANALYTICS_TOKEN") or env.get("UPSTOX_ACCESS_TOKEN")
                     or self._token_from_file("analytics") or self._token_from_file("access"))
            if not token:
                raise UpstoxAuthError(
                    "no Upstox market data token: set UPSTOX_ANALYTICS_TOKEN (or"
                    " UPSTOX_ACCESS_TOKEN); to refresh credentials, re-authenticate, run the"
                    " login skill", code="AUTH_ERROR")
            return token
        if self.mode == "sandbox":
            token = env.get("UPSTOX_SANDBOX_TOKEN") or self._token_from_file("sandbox")
            if not token:
                raise UpstoxAuthError(
                    "no Upstox sandbox token: set UPSTOX_SANDBOX_TOKEN; to refresh credentials,"
                    " re-authenticate, run the login skill", code="AUTH_ERROR")
            return token
        token = env.get("UPSTOX_ACCESS_TOKEN") or self._token_from_file("access")
        if not token:
            raise UpstoxAuthError(
                "no Upstox access token: set UPSTOX_ACCESS_TOKEN; to refresh credentials,"
                " re-authenticate, run the login skill", code="AUTH_ERROR")
        return token

    # ---- transport ----

    def _throttle(self):
        if self._min_interval > 0:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()

    def _send(self, method, url, payload=None, purpose=None):
        headers = {"Accept": "application/json"}
        if purpose:
            headers["Authorization"] = "Bearer " + self._token(purpose)
        self._throttle()
        status, body = self._transport(method, url, payload=payload, headers=headers,
                                       timeout=self._timeout)
        raise_for_response(status, body)
        if not body:
            return {}
        try:
            doc = json.loads(body)
        except ValueError:
            return {}
        if isinstance(doc, dict) and doc.get("status") == "error":
            raise_for_response(status or 500, body)
        return doc

    def _data(self, method, path, payload=None):
        doc = self._send(method, LIVE + path, payload=payload, purpose="market")
        data = doc.get("data") if isinstance(doc, dict) else None
        return data if isinstance(data, dict) else {}

    def request(self, method, path, payload=None, base=None):
        """Trading request seam for broker.py. Returns the parsed JSON envelope."""
        base = base or self.trading_base
        if base == SANDBOX and path.startswith(MARKET_DATA_PATHS):
            raise SandboxUnsupported(
                "the Upstox sandbox host serves orders only, not market data; request quotes and"
                " candles from the live host")
        return self._send(method, base + path, payload=payload, purpose="trading")

    # ---- market data ----

    def quote(self, keys, fields="ohlc"):
        """Batch quotes, 500 keys per request. Returns {instrument_key: {...}} keyed by
        instrument_key; keys missing from the response or carrying a null last_price
        stay absent (no invented numbers)."""
        wanted = [str(key) for key in (keys or [])]
        out = {}
        for i in range(0, len(wanted), QUOTE_CHUNK):
            chunk = wanted[i:i + QUOTE_CHUNK]
            path = "/v2/market-quote/quotes?instrument_key=" + quote(",".join(chunk), safe=",")
            data = self._data("GET", path)
            for raw_key, entry in data.items():
                bar = _quote_entry(entry, fields)
                if bar is not None:
                    out[raw_key.replace(":", "|")] = bar
        return out

    def candles(self, instrument_key, interval="1m", start=None, end=None):
        """v3 historical candles for 1m/30m/day/week/month. Upstox keeps no 5m
        history: resample the 1m feed or use intraday() for the current day."""
        if interval == "5m":
            raise UpstoxError("Upstox has no 5m history: resample the 1m candles, or use"
                              " intraday() for the current day")
        if interval not in _INTERVALS:
            raise UpstoxError("unsupported candle interval %r, use one of %s"
                              % (interval, ", ".join(sorted(_INTERVALS))))
        unit, step = _INTERVALS[interval]
        end_d = _as_date(end, "end") or datetime.datetime.now(IST).date()
        start_d = _as_date(start, "start") or (end_d - datetime.timedelta(days=_WINDOWS[interval]))
        if start_d > end_d:
            raise UpstoxError("start %s is after end %s" % (start_d, end_d))
        path = "/v3/historical-candle/%s/%s/%s/%s/%s" % (
            quote(str(instrument_key), safe=""), unit, step, end_d.isoformat(), start_d.isoformat())
        doc = self._send("GET", LIVE + path, purpose="market")
        data = doc.get("data") if isinstance(doc, dict) else {}
        rows = data.get("candles") if isinstance(data, dict) else None
        # session filter applies to minute bars; day/week/month bars carry 00:00 stamps
        return _normalize_rows(rows, session_filter=interval in ("1m", "30m"))

    def intraday(self, instrument_key, interval="5m"):
        """Current-day candles from the v3 intraday endpoint (1m or 30m); a 5m
        request fetches 1m and resamples to the session-aware 5m grid."""
        if interval == "5m":
            unit, step, target = "minutes", "1", 5
        elif interval == "1m":
            unit, step, target = "minutes", "1", None
        elif interval == "30m":
            unit, step, target = "minutes", "30", None
        else:
            raise UpstoxError("intraday() supports 1m, 5m and 30m, got %r" % (interval,))
        path = "/v3/historical-candle/intraday/%s/%s/%s" % (
            quote(str(instrument_key), safe=""), unit, step)
        doc = self._send("GET", LIVE + path, purpose="market")
        data = doc.get("data") if isinstance(doc, dict) else {}
        rows = data.get("candles") if isinstance(data, dict) else None
        bars = _normalize_rows(rows, session_filter=True)
        if target:
            bars = resample(bars, target)
        return bars

    def instruments(self, exchange="NSE", refresh=False):
        """Instrument master rows, cached under the state dir when one is set."""
        state = self.cfg.get("state_dir")
        cache = os.path.join(str(state), "instruments-%s.json" % exchange) if state else None
        if cache and not refresh and os.path.exists(cache):
            try:
                with open(cache, "r", encoding="utf-8") as fh:
                    rows = json.load(fh)
                if isinstance(rows, list):
                    return rows
            except (OSError, ValueError):
                pass
        status, body = self._transport("GET", INSTRUMENT_MASTER_URL % exchange, payload=None,
                                       headers={"Accept": "application/gzip"},
                                       timeout=self._timeout)
        raise_for_response(status, body)
        rows = instruments_mod.load(body)
        if cache:
            try:
                os.makedirs(str(state), exist_ok=True)
                with open(cache, "w", encoding="utf-8") as fh:
                    json.dump(rows, fh)
            except OSError:
                pass  # a full cache dir must not break the data call
        return rows


def _quote_entry(entry, fields):
    if not isinstance(entry, dict):
        return None
    ltp = _f(entry.get("last_price"))
    if ltp is None:
        return None  # null last_price: nothing real to report
    ohlc = entry.get("ohlc") if isinstance(entry.get("ohlc"), dict) else {}
    # Upstox ohlc.close is the previous close; the pinned dict keeps close as the
    # last traded price and carries the previous close separately.
    full = {
        "ltp": ltp,
        "open": _f(ohlc.get("open")),
        "high": _f(ohlc.get("high")),
        "low": _f(ohlc.get("low")),
        "close": ltp,
        "prev_close": _f(ohlc.get("close")),
        "volume": _f(ohlc.get("volume")),
        "ts": None,
    }
    if entry.get("timestamp"):
        try:
            full["ts"] = _parse_ts(entry["timestamp"])
        except (ValueError, TypeError):
            full["ts"] = None
    if fields == "ltp":
        return {"ltp": full["ltp"], "ts": full["ts"]}
    return full
