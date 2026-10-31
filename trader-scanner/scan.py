#!/usr/bin/env python3
"""NSE Chart Scanner for trader-scanner: build the NSE universe from EQUITY_L and
the Nifty 500 list, run the daily setup scan off NSE bhavcopies, and run the
opening-range breakout scan on Upstox 1m candles resampled to 5m.

Standard library only. Times are IST, money is INR, data is NSE and Upstox.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from trader_core import CLOSE, IST, OPEN  # noqa: E402
from trader_core import calendar as cal  # noqa: E402
from trader_core import config as cfgmod  # noqa: E402
from trader_core import instruments as instruments_mod  # noqa: E402
from trader_core import upstox as upstox_mod  # noqa: E402
from trader_core.timeutil import fmt_ist, now_ist, parse_ist  # noqa: E402

BAR_MIN = 5
SESSIONS_NEEDED = 20        # sessions behind the 20-day statistics
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_WATCHLIST = "trader/watchlist.json"
NIFTY500_DEFAULT = os.path.join(_ROOT, "trader-scanner", "nifty500.csv")
EQUITY_L_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BHAV_HOSTS = ("https://archives.nseindia.com", "https://nsearchives.nseindia.com")
BHAV_PATH = "/products/content/sec_bhavdata_full_%s.csv"
UA = ("Mozilla/5.0 (compatible; day-trader-skill/3.0; "
      "+https://github.com/fenilmodi00/day-trader-skill-india)")
BHAV_STEM = re.compile(r"sec_bhavdata_full_(\d{2})(\d{2})(\d{4})")
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

SETUPS = [
    ("breakout_20d", "20-day high"),
    ("reclaim_ma20", "20-DMA reclaim"),
    ("bull_flag", "bull flag"),
    ("oversold", "oversold"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _ist(day, t):
    """Aware IST datetime for a date and a time."""
    return datetime.datetime.combine(day, t, tzinfo=IST)


def _number(text):
    """Bhavcopy cell -> float, or None for '', '-', '-' style blanks."""
    if text is None:
        return None
    text = str(text).strip().replace(",", "")
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bhav_date(text):
    """'26-OCT-2026' (DATE1) -> date, or None."""
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", (text or "").strip())
    if not m:
        return None
    month = _MONTHS.get(m.group(2).upper())
    if not month:
        return None
    return datetime.date(int(m.group(3)), month, int(m.group(1)))


def parse_hhmm(text, flag="--at"):
    """'10:15' -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m:
        raise ScanError("%s wants HH:MM, got %r" % (flag, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_day(text, flag="--date"):
    """'YYYY-MM-DD' -> date."""
    try:
        return datetime.date.fromisoformat((text or "").strip())
    except ValueError:
        raise ScanError("%s wants YYYY-MM-DD, got %r" % (flag, text))


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def write_lines(path, lines):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")


# ---------------------------------------------------------------- bhavcopy

def read_bhav(text, name=""):
    """Bhavcopy CSV text -> {symbol: row}. EQ series only, and only rows with a
    usable close: the rest (BE/BZ/SM series, blank closes) are dropped."""
    reader = csv.DictReader(io.StringIO(text))
    fields = {}
    for f in (reader.fieldnames or []):
        if f:
            fields[f.strip().upper()] = f

    def col(*names):
        for n in names:
            if n in fields:
                return fields[n]
        return None

    sym_f = col("SYMBOL")
    close_f = col("CLOSE_PRICE", "CLOSE")
    if not sym_f or not close_f:
        raise ScanError("%s is not an NSE bhavcopy (SYMBOL/CLOSE_PRICE columns missing)"
                        % (name or "the file"))
    ser_f = col("SERIES")
    date_f = col("DATE1", "DATE")
    out = {}
    for raw in reader:
        sym = (raw.get(sym_f) or "").strip().upper()
        if not sym or (ser_f and (raw.get(ser_f) or "").strip().upper() != "EQ"):
            continue
        close = _number(raw.get(close_f))
        if close is None or close <= 0:
            continue
        out[sym] = {
            "date1": (raw.get(date_f) or "").strip() if date_f else "",
            "prev_close": _number(raw.get(col("PREV_CLOSE"))),
            "open": _number(raw.get(col("OPEN_PRICE"))),
            "high": _number(raw.get(col("HIGH_PRICE"))),
            "low": _number(raw.get(col("LOW_PRICE"))),
            "last": _number(raw.get(col("LAST_PRICE"))),
            "close": close,
            "avg_price": _number(raw.get(col("AVG_PRICE"))),
            "volume": _number(raw.get(col("TTL_TRD_QNTY"))),
            "turnover_lacs": _number(raw.get(col("TURNOVER_LACS"))),
            "deliv_qty": _number(raw.get(col("DELIV_QTY"))),
            "deliv_per": _number(raw.get(col("DELIV_PER"))),
        }
    return out


def read_bhav_file(path):
    """One bhavcopy CSV -> (date, {symbol: row}). The date comes from the file
    name (sec_bhavdata_full_DDMMYYYY.csv) or the DATE1 column. A bhavcopy dated
    on an NSE holiday, or an empty one, is a hard error."""
    name = os.path.basename(path)
    day = None
    m = BHAV_STEM.search(name)
    if m:
        day = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if day is not None:
        holiday = cal.load_holidays().get(day.isoformat())
        if holiday:
            raise ScanError("NSE was closed on %s (%s): there is no bhavcopy on a holiday."
                            % (day.isoformat(), holiday))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        raise ScanError("cannot read %s: %s" % (path, e))
    if not text.strip():
        raise ScanError("bhavcopy %s is empty: NSE served no data (it is usually published "
                        "by 06:00 PM IST on a trading day)." % name)
    rows = read_bhav(text, name)
    if not rows:
        raise ScanError("bhavcopy %s has no EQ rows with a usable close." % name)
    if day is None:
        for row in rows.values():
            day = _bhav_date(row.get("date1"))
            if day:
                break
        if day is None:
            raise ScanError("cannot date %s: rename it sec_bhavdata_full_DDMMYYYY.csv or fix DATE1"
                            % name)
    return day, rows


def load_bhav(paths):
    """Bhavcopy files (or folders of them) -> [(date, {symbol: row})] oldest first.
    One file per session; a date supplied twice keeps the last file."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            names = sorted(n for n in os.listdir(p) if n.lower().endswith(".csv"))
            if not names:
                raise ScanError("no .csv bhavcopy files in %s" % p)
            files.extend(os.path.join(p, n) for n in names)
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise ScanError("no bhavcopy file or folder at %s" % p)
    if not files:
        raise ScanError("no bhavcopy given: pass --bhavcopy FILE-or-DIR")
    by_day = {}
    for p in files:
        day, rows = read_bhav_file(p)
        by_day[day] = rows
    return sorted(by_day.items())


def fetch_bhavcopy(day):
    """Download one day's bhavcopy -> (text, file name). Weekends and holidays are
    named before the request; a 404 on a trading day means 'not published yet'."""
    holiday = cal.load_holidays().get(day.isoformat())
    if holiday:
        raise ScanError("NSE was closed on %s (%s): there is no bhavcopy on a holiday."
                        % (day.isoformat(), holiday))
    if cal.is_weekend(day):
        raise ScanError("NSE is closed on %s (weekend): there is no bhavcopy." % day.isoformat())
    stamp = day.strftime("%d%m%Y")
    fname = "sec_bhavdata_full_%s.csv" % stamp
    path = BHAV_PATH % stamp
    last = None
    for host in BHAV_HOSTS:
        try:
            return http_get(host + path), fname
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 404:
                break
        except (urllib.error.URLError, OSError) as e:
            last = e
    if isinstance(last, urllib.error.HTTPError) and last.code == 404:
        raise ScanError("NSE returned 404 for %s: the bhavcopy is not published yet (usually "
                        "by 06:00 PM IST on a trading day), or the date is wrong." % fname)
    raise ScanError("could not download %s: %s" % (fname, getattr(last, "reason", last) or last))


# ---------------------------------------------------------------- universe

def parse_equity_l(text):
    """NSE EQUITY_L CSV -> {symbol: isin} for EQ-series rows."""
    reader = csv.DictReader(io.StringIO(text))
    fields = {}
    for f in (reader.fieldnames or []):
        if f:
            fields[f.strip().upper()] = f
    sym_f = fields.get("SYMBOL")
    if not sym_f:
        raise ScanError("not an NSE EQUITY_L file (no SYMBOL column)")
    ser_f = fields.get("SERIES")
    isin_f = fields.get("ISIN NUMBER") or fields.get("ISIN")
    out = {}
    for row in reader:
        sym = (row.get(sym_f) or "").strip().upper()
        if not sym or sym.startswith("#"):
            continue
        series = (row.get(ser_f) or "").strip().upper() if ser_f else "EQ"
        if sym and series == "EQ":
            out[sym] = (row.get(isin_f) or "").strip() if isin_f else ""
    return out


def parse_nifty500(text):
    """Nifty 500 constituents CSV -> {symbol: isin}."""
    reader = csv.DictReader(io.StringIO(text))
    fields = {}
    for f in (reader.fieldnames or []):
        if f:
            fields[f.strip().upper()] = f
    sym_f = fields.get("SYMBOL")
    if not sym_f:
        raise ScanError("not a Nifty 500 constituents file (no SYMBOL column)")
    isin_f = fields.get("ISIN CODE") or fields.get("ISIN")
    out = {}
    for row in reader:
        sym = (row.get(sym_f) or "").strip().upper()
        if sym:
            out[sym] = (row.get(isin_f) or "").strip() if isin_f else ""
    return out


def read_universe(path):
    """universe.txt -> [(symbol, isin, instrument_key)]. The key itself carries a
    pipe (NSE_EQ|INE002A01018), so everything from the third field on is the key."""
    if not os.path.exists(path):
        raise ScanError("no %s: run `scan.py universe --write` first, or pass --universe" % path)
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            if not parts[0]:
                continue
            sym = parts[0].upper()
            isin = parts[1] if len(parts) > 1 else ""
            key = "|".join(parts[2:]) if len(parts) > 2 else ""
            out.append((sym, isin, key))
    if not out:
        raise ScanError("%s holds no symbols" % path)
    return out


def load_master(a, cfg=None, live=False):
    """Instrument master rows: the --master file (offline) or the Upstox download."""
    if getattr(a, "master", None):
        try:
            with open(a.master, "rb") as fh:
                blob = fh.read()
        except OSError as e:
            raise ScanError("cannot read %s: %s" % (a.master, e))
        try:
            return instruments_mod.load(blob)
        except instruments_mod.InstrumentError as e:
            raise ScanError("%s is not an instrument master: %s" % (a.master, e))
    if live:
        try:
            return upstox_mod.Client(cfg or {}).instruments("NSE")
        except upstox_mod.UpstoxError as e:
            raise ScanError("cannot download the Upstox instrument master: %s" % e)
    return None


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI, or None when there are fewer than n + 1 closes."""
    if closes is None or len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def find_setups(closes):
    """closes oldest first (one per supplied session, 2+) -> (setup keys, numbers).

    The rules are the 20-day ones; with fewer than 20 sessions supplied the same
    math runs on the available window and the output says the window is short."""
    if len(closes) < 2:
        return [], {"rsi": None, "high20": None, "ma20": None}
    last = closes[-1]
    prior = closes[:-1]
    hi20 = max(prior[-SESSIONS_NEEDED:])
    ma20 = sum(closes[-SESSIONS_NEEDED:]) / float(min(SESSIONS_NEEDED, len(closes)))
    ma20_prev = sum(closes[-(SESSIONS_NEEDED + 1):-1]) / float(min(SESSIONS_NEEDED, len(closes) - 1))
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if len(closes) >= 16:
        run = closes[-6] / closes[-16] - 1
        rng5 = (max(closes[-5:]) - min(closes[-5:])) / last
        if run > 0.10 and rng5 < 0.05:
            tags.append("bull_flag")
    if len(closes) >= 3 and closes[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    r = rsi(closes)
    r_prev = rsi(closes[:-1])
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > closes[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "high20": hi20, "ma20": ma20}


def scan_daily(sessions, universe, master_index=None, min_price=0.0):
    """sessions [(date, rows)] oldest first, universe [(sym, isin, key)].
    Returns (entries, counts, final_day). Entries are ranked by turnover."""
    counts = {"sessions_supplied": len(sessions), "sessions_needed": SESSIONS_NEEDED,
              "universe": len(universe), "no_data": 0, "illiquid": 0, "short": 0,
              "scanned": 0, "flat": 0, "hits": 0}
    final_day, final_rows = sessions[-1]
    members = {}
    for sym, isin, key in universe:
        members[sym] = {"isin": isin, "key": key}
    entries = []
    for sym in sorted(members):
        row = final_rows.get(sym)
        if not row:
            counts["no_data"] += 1
            continue
        closes = []
        for _, rows in sessions:
            r = rows.get(sym)
            if r:
                closes.append(r["close"])
        if len(closes) < 3:
            counts["short"] += 1
            continue
        price = row["close"]
        turnover = (row["turnover_lacs"] or 0.0) * 100000.0
        if price < min_price or turnover <= 0:
            counts["illiquid"] += 1
            continue
        if len(closes) < 3:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        tags, nums = find_setups(closes)
        if not tags:
            counts["flat"] += 1
            continue
        inst = (master_index or {}).get(sym) or {}
        prev = row["prev_close"]
        entries.append({
            "symbol": sym,
            "isin": members[sym]["isin"] or inst.get("isin") or None,
            "instrument_key": members[sym]["key"] or inst.get("instrument_key") or None,
            "last": round(price, 2),
            "change_pct": round((price / prev - 1) * 100, 2) if prev else None,
            "turnover_inr": round(turnover, 2),
            "setups": tags,
            "setup": tags[0],
            "tick_size": inst.get("tick_size"),
            "lot_size": inst.get("lot_size"),
            "freeze_quantity": inst.get("freeze_quantity"),
            "rsi": round(nums["rsi"], 1) if nums["rsi"] is not None else None,
            "high20": round(nums["high20"], 2),
            "ma20": round(nums["ma20"], 2),
            "close_date": final_day.isoformat(),
            "orb": None,
        })
    entries.sort(key=lambda e: (-e["turnover_inr"], e["symbol"]))
    counts["hits"] = len(entries)
    return entries, counts, final_day


# ---------------------------------------------------------------- ORB

class UpstoxSource:
    """Live ORB data: Upstox v3 intraday 1m candles (needs an analytics token)."""

    def __init__(self, cfg=None):
        self.client = upstox_mod.Client(cfg if isinstance(cfg, dict) else {})

    def intraday(self, instrument_key, interval="1m"):
        return self.client.intraday(instrument_key, interval)


class OfflineSource:
    """Recorded ORB data: an instrument-keyed fixture doc played back through the
    real Upstox client (so parsing and session filtering stay the shared code).

    doc maps instrument_key -> raw candle rows, or carries one envelope under the
    key "__envelope__" for a fixture that is a plain v3 candles answer."""

    def __init__(self, doc):
        self.rows = {}
        for key, value in (doc or {}).items():
            if isinstance(value, dict):
                value = (value.get("data") or {}).get("candles") or []
            if isinstance(value, list):
                self.rows[str(key)] = value
        self.client = upstox_mod.Client({}, transport=self._transport, token="offline")

    def _transport(self, method, url, payload=None, headers=None, timeout=30):
        for key, rows in self.rows.items():
            if key != "__envelope__" and urllib.parse.quote(key, safe="") in url:
                return 200, json.dumps({"status": "success", "data": {"candles": rows}})
        envelope = self.rows.get("__envelope__")
        if envelope is not None:
            return 200, json.dumps({"status": "success", "data": {"candles": envelope}})
        return 200, json.dumps({"status": "error", "errorCode": "UDAPI100098",
                                "message": "no recorded intraday bars for this instrument "
                                           "in the offline fixture"})

    def intraday(self, instrument_key, interval="1m"):
        return self.client.intraday(instrument_key, "1m")


def load_offline(path):
    """ORB offline fixture: a file or folder of JSON docs, each either keyed by
    instrument_key or one Upstox candles envelope.
    -> (doc, day set, envelope flag)."""
    if os.path.isdir(path):
        names = sorted(n for n in os.listdir(path) if n.endswith(".json"))
        if not names:
            raise ScanError("no .json files in %s" % path)
        paths = [os.path.join(path, n) for n in names]
    elif os.path.isfile(path):
        paths = [path]
    else:
        raise ScanError("no offline file or folder at %s" % path)
    doc, days, envelope = {}, set(), False
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError) as e:
            raise ScanError("cannot read %s: %s" % (p, e))
        if isinstance(d, dict) and isinstance(d.get("data"), dict) and "candles" in d["data"]:
            doc["__envelope__"] = d["data"]["candles"]
            envelope = True
            d = {}
        elif not isinstance(d, dict):
            raise ScanError("%s is not an instrument-keyed fixture or a candles envelope"
                            % os.path.basename(p))
        for key, value in d.items():
            if isinstance(value, dict) and isinstance(value.get("data"), dict):
                value = value["data"].get("candles")
            if isinstance(value, list):
                doc[str(key)] = value
    for rows in doc.values():
        for row in rows:
            try:
                days.add(str(row[0])[:10])
            except (IndexError, TypeError):
                continue
    if not days:
        raise ScanError("%s holds no candle rows" % os.path.basename(path.rstrip("/\\")))
    return doc, days, envelope


def orb_first(day_bars, or_minutes):
    """The first completed 5m bar after the opening range that CLOSED above the
    range high, or None. One signal per symbol per day by construction."""
    n_range = or_minutes // BAR_MIN
    if len(day_bars) < n_range:
        return None
    rng = day_bars[:n_range]
    or_high = max(b["h"] for b in rng)
    or_low = min(b["l"] for b in rng)
    for b in day_bars[n_range:]:
        if b["c"] > or_high:
            closes_at = b["dt"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": fmt_ist(closes_at),
                    "bar_start": b["dt"].strftime("%H:%M"),
                    "or_high": round(or_high, 2), "or_low": round(or_low, 2),
                    "pct_over": round((b["c"] / or_high - 1) * 100, 2)}
    return None


def _as_bars(rows):
    """Raw [ts,o,h,l,c,v,oi] rows (or already bar dicts) -> pinned bar dicts."""
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append(row)
        else:
            out.append({"dt": upstox_mod._parse_ts(row[0]), "o": row[1], "h": row[2],
                        "l": row[3], "c": row[4], "v": row[5] if len(row) > 5 else None,
                        "oi": row[6] if len(row) > 6 else None})
    return out


def scan_orb(source, entries, day, cutoff, or_minutes):
    """entries [{symbol, instrument_key}]. Bars are Upstox 1m candles resampled to
    the 5m grid; with a cutoff only bars complete at it count.
    Returns (hit entries, skipped {symbol: why})."""
    first_close = _ist(day, OPEN) + datetime.timedelta(minutes=or_minutes)
    hits, skipped = [], {}
    for entry in entries:
        sym = entry.get("symbol") or ""
        key = entry.get("instrument_key")
        if not key:
            skipped[sym] = "no instrument_key"
            continue
        try:
            bars = upstox_mod.resample(_as_bars(source.intraday(key, "1m")), BAR_MIN)
        except upstox_mod.UpstoxError as e:
            skipped[sym] = str(e)
            continue
        day_bars = [b for b in bars if b["dt"].date() == day]
        if not day_bars:
            skipped[sym] = "no intraday bars on %s" % day.isoformat()
            continue
        if cutoff is not None:
            if cutoff < first_close:
                skipped[sym] = ("waiting: the %d-minute opening range is not complete at %s"
                                % (or_minutes, fmt_ist(cutoff)))
                continue
            day_bars = [b for b in day_bars
                        if b["dt"] + datetime.timedelta(minutes=BAR_MIN) <= cutoff]
        hit = orb_first(day_bars, or_minutes)
        if hit:
            hits.append({"symbol": sym, "instrument_key": key, "orb": hit})
    hits.sort(key=lambda h: (h["orb"]["at"], h["symbol"]))
    return hits, skipped


def orb_window(now, at, or_minutes, day=None):
    """Live ORB clock check -> cutoff (aware IST)."""
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    day = day or now.date()
    holiday = cal.load_holidays().get(day.isoformat())
    if holiday:
        raise ScanError("NSE is closed on %s (%s): there is no opening range to break."
                        % (day.isoformat(), holiday))
    if cal.is_weekend(day):
        raise ScanError("NSE is closed on %s (weekend): there is no opening range to break."
                        % day.isoformat())
    range_end = _ist(day, OPEN) + datetime.timedelta(minutes=or_minutes)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = _ist(day, CLOSE)
    cutoff = _ist(day, at) if at else now.replace(second=0, microsecond=0)
    if at and cutoff > now + datetime.timedelta(minutes=1):
        raise ScanError("--at %s is later than now (%s)." % (at.strftime("%H:%M"), fmt_ist(now)))
    if cutoff >= close_dt:
        raise ScanError("NSE closed at %s. Replay the session with --at HH:MM." % fmt_ist(close_dt))
    if cutoff < first_close:
        raise ScanError("It is %s: the %d-minute opening range runs 09:15 to %s and the first "
                        "5m bar after it closes at %s. Run orb again after %s."
                        % (fmt_ist(cutoff), or_minutes, fmt_ist(range_end), fmt_ist(first_close),
                           fmt_ist(first_close)))
    return cutoff


# ---------------------------------------------------------------- watchlist

def read_watchlist(path):
    """A watchlist.json (symbols[] or hits[]) or a text file -> [entry dicts]."""
    if not os.path.exists(path):
        raise ScanError("no watchlist at %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = []
        if isinstance(doc, dict):
            items = doc.get("symbols") or doc.get("hits") or []
        elif isinstance(doc, list):
            items = doc
        out = []
        for item in items:
            if isinstance(item, dict) and item.get("symbol"):
                out.append(dict(item))
            elif item:
                out.append({"symbol": str(item).upper()})
        return out
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            parts = ln.split("|")
            entry = {"symbol": parts[0].strip().upper()}
            if len(parts) > 2:
                entry["isin"] = parts[1].strip()
                entry["instrument_key"] = "|".join(parts[2:]).strip()
            out.append(entry)
    return out


def read_entries(a):
    """ORB symbols: --symbols (keys resolved from the universe file), --watchlist,
    else the universe file -> (entries, base entries or None)."""
    universe_path = a.universe or DEFAULT_UNIVERSE
    keys = {}
    if os.path.exists(universe_path):
        for sym, isin, key in read_universe(universe_path):
            keys[sym] = {"isin": isin, "key": key}
    base = None
    if a.symbols:
        names = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist and os.path.exists(a.watchlist):
        base = read_watchlist(a.watchlist)
        names = [e["symbol"] for e in base]
    elif os.path.exists(universe_path):
        names = sorted(keys)
    else:
        raise ScanError("no %s: run `scan.py universe --write` first, or pass --symbols"
                        % universe_path)
    if not names:
        raise ScanError("no symbols to scan")
    entries = []
    for name in names:
        entry = None
        for e in (base or []):
            if e.get("symbol") == name:
                entry = dict(e)
                break
        if entry is None:
            entry = {"symbol": name, "isin": None, "instrument_key": None}
        if not entry.get("instrument_key") and name in keys:
            entry["instrument_key"] = keys[name]["key"]
            if not entry.get("isin"):
                entry["isin"] = keys[name]["isin"]
        entries.append(entry)
    return entries, base


def merge_watchlist(base, hits, master_index, as_of_text, scanned=None):
    """Daily/orb watchlist entries: base entries updated with the orb result,
    plus any new orb hits. When no watchlist file backs the scan, the scanned
    entries carry it so waiting symbols still appear (with orb: null).
    Instrument fields come from the master when missing."""
    merged, seen = [], set()
    for entry in (base if base is not None else (scanned or [])):
        merged.append(dict(entry))
        seen.add(entry.get("symbol"))
    for hit in hits:
        if hit["symbol"] in seen:
            continue
        merged.append({"symbol": hit["symbol"], "isin": None,
                       "instrument_key": hit.get("instrument_key"), "last": hit["orb"]["price"],
                       "change_pct": None, "turnover_inr": None, "setups": ["orb"],
                       "setup": "orb"})
        seen.add(hit["symbol"])
    for entry in merged:
        inst = (master_index or {}).get(entry.get("symbol")) or {}
        for field in ("instrument_key", "isin", "tick_size", "lot_size", "freeze_quantity"):
            if entry.get(field) is None and inst.get(field) is not None:
                entry[field] = inst[field]
        entry.setdefault("setups", [])
        entry.setdefault("setup", None)
        entry.setdefault("last", None)
        entry.setdefault("change_pct", None)
        entry.setdefault("turnover_inr", None)
        hit = next((h for h in hits if h["symbol"] == entry.get("symbol")), None)
        if hit:
            entry["orb"] = hit["orb"]
            entry["setup"] = "orb"
            if "orb" not in entry["setups"]:
                entry["setups"] = entry["setups"] + ["orb"]
        else:
            entry["orb"] = None
        entry["as_of"] = as_of_text
    merged.sort(key=lambda e: e.get("symbol") or "")
    return merged


# ---------------------------------------------------------------- commands

def cmd_universe(a, source=None):
    now = now_ist()
    t0 = time.time()
    nifty_path = a.nifty500 or NIFTY500_DEFAULT
    equity, nifty = {}, {}
    if a.offline:
        for name in (os.path.basename(a.offline.rstrip("/\\")), "EQUITY_L.csv"):
            p = os.path.join(a.offline, name)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    equity = parse_equity_l(fh.read())
                break
        for name in (os.path.basename(a.offline.rstrip("/\\")), "nifty500.csv"):
            p = os.path.join(a.offline, name)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    nifty = parse_nifty500(fh.read())
                break
    else:
        try:
            equity = parse_equity_l(http_get(EQUITY_L_URL))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            raise ScanError("could not download EQUITY_L.csv: %s" % e)
    if not nifty and nifty_path and os.path.exists(nifty_path):
        with open(nifty_path, "r", encoding="utf-8", errors="replace") as fh:
            nifty = parse_nifty500(fh.read())
    if not equity and not nifty:
        raise ScanError("no EQUITY_L.csv or nifty500.csv found%s"
                        % (": pass --offline DIR with them" if a.offline else ""))
    union = {}
    for table in (equity, nifty):
        for sym, isin in table.items():
            union.setdefault(sym, isin)
    cfg = cfgmod.load(a.config)
    rows = load_master(a, cfg=cfg, live=not a.offline)
    if rows is None:
        raise ScanError("no instrument master: pass --master FILE when offline")
    lines, missing = [], []
    for sym in sorted(union):
        try:
            row = instruments_mod.find(rows, sym, "NSE_EQ")
        except instruments_mod.InstrumentError:
            missing.append(sym)
            continue
        lines.append("%s|%s|%s" % (sym, row.get("isin") or "", row.get("instrument_key") or ""))
    out = None
    if not a.no_write:
        out = a.out
        write_lines(out, lines)
    if a.json:
        print(json.dumps({"created_by": "trader-scanner", "as_of": fmt_ist(now),
                          "source": "NSE EQUITY_L + Nifty 500", "equity_l": len(equity),
                          "nifty500": len(nifty), "union": len(union), "count": len(lines),
                          "missing_master": len(missing), "written": out,
                          "seconds": round(time.time() - t0, 1)},
                         indent=2))
    else:
        print("NSE universe: %s EQUITY_L EQ rows + %s Nifty 500 rows = %s symbols."
              % ("{:,}".format(len(equity)), "{:,}".format(len(nifty)), "{:,}".format(len(union))))
        print("Resolved %s instrument keys from the Upstox master%s; %s symbols have no NSE_EQ row."
              % ("{:,}".format(len(lines)), (" (%s)" % a.master) if a.master else "",
                 "{:,}".format(len(missing))))
        if out:
            print("Wrote %s (one SYMBOL|ISIN|instrument_key a line)." % out)
    return 0 if lines else 1


def cmd_daily(a, source=None):
    now = parse_ist(a.now) if a.now else now_ist()
    day = parse_day(a.date) if a.date else now.date()
    if a.bhavcopy:
        sessions = load_bhav(a.bhavcopy)
        doc_source = "nse-bhavcopy-offline"
    elif a.offline:
        sessions = load_bhav([a.offline])
        doc_source = "nse-bhavcopy-offline"
    else:
        text, fname = fetch_bhavcopy(day)
        sessions = [(day, read_bhav(text, fname))]
        doc_source = "nse-bhavcopy"
    universe = read_universe(a.universe or DEFAULT_UNIVERSE)
    master = load_master(a, cfg=cfgmod.load(a.config), live=not a.offline)
    mindex = instruments_mod.index(master) if master else {}
    entries, counts, final_day = scan_daily(sessions, universe, mindex, a.min_price)
    as_of = fmt_ist(now)
    for entry in entries:
        entry["as_of"] = as_of
    doc = {"created_by": "trader-scanner", "mode": "daily", "as_of": as_of,
           "source": doc_source, "date": final_day.isoformat(),
           "sessions_supplied": counts["sessions_supplied"],
           "sessions_needed": counts["sessions_needed"],
           "universe": counts["universe"], "scanned": counts["scanned"],
           "failed": counts["no_data"], "note": NOTE, "count": len(entries),
           "symbols": entries}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/\\"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        short = "" if counts["sessions_supplied"] >= SESSIONS_NEEDED else (
            " Only %d session(s) supplied: the 20-day statistics use the available window "
            "(full rules need %d sessions)." % (counts["sessions_supplied"], SESSIONS_NEEDED))
        print("NSE daily scan, closes of %s (bhavcopy).%s" % (final_day.isoformat(), short))
        print("Universe %s, with data %s, scanned %s, no setup %s, no data %s."
              % ("{:,}".format(counts["universe"]), "{:,}".format(counts["scanned"] + counts["illiquid"]),
                 "{:,}".format(counts["scanned"]), "{:,}".format(counts["flat"]),
                 "{:,}".format(counts["no_data"])))
        if entries:
            per = [(k, sum(1 for e in entries if k in e["setups"])) for k, _ in SETUPS]
            print("%d chart(s) matched a setup: %s. Most traded first:"
                  % (len(entries), ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
            for e in entries[:a.show]:
                print("  %-12s %10.2f  %+6.2f%%  %s" % (
                    e["symbol"], e["last"], e["change_pct"] or 0.0,
                    ", ".join(LABEL[t] for t in e["setups"])))
            if len(entries) > a.show:
                print("  ... and %d more in %s (--show N for more)." % (len(entries) - a.show, a.out))
        else:
            print("No setups.")
        print(NOTE + " Next: pick one chart, then /trader-news SYMBOL.")
    return 0 if entries else 1


def cmd_orb(a, source=None):
    now = parse_ist(a.now) if a.now else now_ist()
    at = parse_hhmm(a.at) if a.at else None
    if a.or_minutes < BAR_MIN or a.or_minutes % BAR_MIN or a.or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    offline = bool(a.offline)
    if offline:
        doc, days, envelope = load_offline(a.offline)
        if envelope and (a.symbols or "").count(",") and len([s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]) > 1:
            raise ScanError("the offline file is one instrument's candles: pass one --symbols symbol")
        src = OfflineSource(doc)
        day = now.date() if a.now else datetime.date.fromisoformat(max(days))
        cutoff = _ist(day, at) if at else (now.replace(second=0, microsecond=0) if a.now else None)
        doc_source = "offline-fixture"
    else:
        cfg = cfgmod.load(a.config)
        src = source or UpstoxSource(cfg)
        day = now.date()
        cutoff = orb_window(now, at, a.or_minutes, day)
        doc_source = "upstox-intraday"
    entries, base = read_entries(a)
    if offline and envelope and len(entries) > 1:
        raise ScanError("the offline file is one instrument's candles: pass --symbols with one symbol")
    if entries and not any(e.get("instrument_key") for e in entries):
        raise ScanError("no instrument_key for %s: run `scan.py universe --write` first, or "
                        "pass --universe" % ", ".join(e["symbol"] for e in entries))
    hits, skipped = scan_orb(src, entries, day, cutoff, a.or_minutes)
    if skipped and not hits and len(skipped) == len(entries):
        raise ScanError("; ".join("%s: %s" % (s, w) for s, w in sorted(skipped.items())))
    if source is not None:
        master = None  # an injected source owns the data; no instrument master call
    else:
        master = load_master(a, cfg=cfgmod.load(a.config), live=not offline)
    mindex = instruments_mod.index(master) if master else {}
    as_of = fmt_ist(now)
    symbols = merge_watchlist(base, hits, mindex, as_of, scanned=entries)
    if hits:
        status = "triggered"
    elif cutoff is None:
        # a full-session replay with no close above the range: the day is done
        status = "expired"
    elif cutoff < _ist(day, CLOSE):
        # in-session with no trigger yet: the session can still produce one
        status = "waiting"
    else:
        status = "expired"
    doc = {"created_by": "trader-scanner", "mode": "orb", "as_of": as_of,
           "source": doc_source, "date": day.isoformat(), "or_minutes": a.or_minutes,
           "universe": len(entries), "scanned": len(entries) - len(skipped),
           "failed": len(skipped), "status": status, "note": NOTE,
           "count": len(symbols), "symbols": symbols, "skipped": skipped}
    if offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/\\"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("Opening-range breakout scan for %s (NSE, Upstox 1m resampled to 5m): "
              "%d-minute range 09:15 to %s, cutoff %s."
              % (day.isoformat(), a.or_minutes,
                 (_ist(day, OPEN) + datetime.timedelta(minutes=a.or_minutes)).strftime("%H:%M"),
                 fmt_ist(cutoff) if cutoff else "full session"))
        print("Universe %s, scanned %s, skipped %s."
              % ("{:,}".format(len(entries)), "{:,}".format(len(entries) - len(skipped)),
                 "{:,}".format(len(skipped))))
        if hits:
            print("%d close(s) above the opening-range high:"
                  % len(hits))
            for h in hits[:a.show]:
                orb = h["orb"]
                print("  %s  %-12s %10.2f  range %s to %s  %+.2f%% over"
                      % (orb["at"], h["symbol"], orb["price"], orb["or_low"], orb["or_high"],
                         orb["pct_over"]))
            if len(hits) > a.show:
                print("  ... and %d more in %s (--show N for more)." % (len(hits) - a.show, a.out))
        elif status == "waiting":
            print("Waiting: the opening range is not complete yet.")
        elif status == "expired":
            print("Expired: no bar closed above the opening-range high in this session.")
        else:
            print("No close above the opening-range high yet.")
        for sym, why in sorted(skipped.items()):
            print("  skipped %s: %s" % (sym, why))
        print(NOTE + " Next: pick one chart, then /trader-news SYMBOL and /trader-entry SYMBOL.")
    return 0 if hits else 1


# ---------------------------------------------------------------- main

def main(argv=None, source=None):
    ap = argparse.ArgumentParser(
        description="Chart Scanner (trader-scanner): NSE universe, bhavcopy daily scan, "
                    "Upstox opening-range breakout.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="NSE EQUITY_L + Nifty 500 -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--write", action="store_true", help="write the cache (writing is the default)")
    p.add_argument("--offline", metavar="DIR", help="a folder with EQUITY_L.csv and/or nifty500.csv")
    p.add_argument("--nifty500", metavar="FILE", help="Nifty 500 constituents CSV (default: the "
                                                     "checked-in seed slice)")
    p.add_argument("--master", metavar="FILE", help="Upstox instrument master .json/.json.gz "
                                                    "(offline; live downloads it)")
    p.add_argument("--config", default=None, help="trader config.json (defaults apply when absent)")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    def common(p):
        p.add_argument("--symbols", help="comma-separated symbols instead of the universe")
        p.add_argument("--watchlist", metavar="FILE", help="a watchlist.json or a text list")
        p.add_argument("--universe", help="universe file, one SYMBOL|ISIN|instrument_key a line "
                                          "(default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--master", metavar="FILE", help="Upstox instrument master .json/.json.gz "
                                                        "(offline; live downloads it)")
        p.add_argument("--offline", metavar="PATH", help="fixture file or folder instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-26 10:15", IST)')
        p.add_argument("--out", default=DEFAULT_WATCHLIST)
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")
        p.add_argument("--config", default=None, help="trader config.json (defaults apply when absent)")

    p = sub.add_parser("daily", help="setup scan from NSE bhavcopy closes")
    common(p)
    p.add_argument("--bhavcopy", nargs="+", metavar="PATH", help="one or more sec_bhavdata_full "
                                                                 "CSV files, or a folder of prior days")
    p.add_argument("--date", help="trading day, YYYY-MM-DD (default: today)")
    p.add_argument("--min-price", type=float, default=0.0, help="price floor")

    p = sub.add_parser("orb", help="opening-range breakout on Upstox 1m -> 5m bars")
    common(p)
    p.add_argument("--or-minutes", type=int, default=15, help="opening range minutes (default 15)")
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    a._t0 = time.time()
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a, source=source)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 2
    except cfgmod.ConfigError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 2
    except ValueError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
