#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches a setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an
order; nothing goes to the broker without your yes, out loud.

    python3 entry.py check RELIANCE --key "NSE_EQ|INE002A01018"
    python3 entry.py check RELIANCE --now "2026-10-26 10:00" --offline RELIANCE-1m.json
    python3 entry.py check RELIANCE --setup level --level 1192.00
    python3 entry.py check RELIANCE --min-volume-x 1.5 --master instruments.json.gz --write
    python3 entry.py watch RELIANCE --every 30 --until 15:00
    python3 entry.py watchlist                            # every symbol in trader/watchlist.json

Setups (longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 09:15, 09:20 and 09:25 five-minute bars,
         resampled from Upstox 1-minute candles). It triggers on the first
         COMPLETED 5m bar after the range that CLOSES above the range high.
         A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars.

A trigger's time is the trigger bar's CLOSE time (the 09:55 bar closes at
10:00 IST) and its price is that close, to the paisa. One trigger per symbol
per IST day: the first close above wins. The entry window is 09:30 to 15:00
IST (config session entry_start/entry_cutoff); a bar closing outside the
window never triggers. Bars come from Upstox 1-minute candles resampled to
the 5m grid (Upstox keeps no 5m history); a bar counts only once it has
closed. Pre-open rows are ignored.

Statuses: triggered; waiting (not open yet, the range is still forming, or no
close above the level yet); expired (no trigger by the 15:00 entry cutoff);
no-data (NSE is closed today, or Upstox sent no bars).

trader/config.json (optional) sets the session; the defaults are entry_start
09:30 and entry_cutoff 15:00 IST. The plan's flat-by lives in trader-exit.

watch wakes just after each 5m bar closes, polls every --every seconds
(default 30) while the new bar has not arrived, prints one line per closed
bar and stops on a trigger (exit 0, writes trader/entry.json), on expiry, at
the close or at --until (exit 1). With --now or --offline it runs on a
simulated clock and replays the day without sleeping.

Data: Upstox intraday candles (1m) via the configured credentials.
  --offline PATH reads a recorded fixture instead: one Upstox candles
  envelope ({"status": "success", "data": {"candles": [...]}}) or a JSON doc
  keyed by instrument_key, or a folder of such files. Without --now, an
  offline file is replayed as of 10:00 IST on its latest recorded day.

Instrument identity: the symbol maps to an Upstox instrument_key from --key,
then --master FILE (the instrument master, .json or .json.gz, which also
supplies tick_size and lot_size), then the scanner's trader/universe.txt
(SYMBOL|ISIN|instrument_key lines). watchlist.json entries carry their own
instrument_key.

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time

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
GRACE = 2                      # seconds after a bar closes before watch asks for it
DEFAULT_OUT = "trader/entry.json"
DEFAULT_WATCHLIST = "trader/watchlist.json"
DEFAULT_UNIVERSE = "trader/universe.txt"
REPLAY_AT = datetime.time(10, 0)   # offline replay clock when --now is not given
NOTE = ("Nothing goes to the broker without your yes: entry.py only says the "
        "moment and writes trader/entry.json.")

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _ist(day, t):
    """Aware IST datetime for a date and a time."""
    return datetime.datetime.combine(day, t, tzinfo=IST)


def parse_day(text, flag="--date"):
    try:
        return datetime.date.fromisoformat((text or "").strip())
    except ValueError:
        raise EntryError("%s wants YYYY-MM-DD, got %r" % (flag, text))


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants an IST time like 15:00, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def read_any(path):
    """File text, .json.gz included."""
    if str(path).endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            return fh.read()
    with open(path, "rb") as fh:
        return fh.read()


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


# ---------------------------------------------------------------- offline source

def bars_of(rows):
    """Raw [ts,o,h,l,c,v,oi] rows -> pinned bar dicts (aware IST)."""
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append(row)
        else:
            out.append({"dt": upstox_mod._parse_ts(row[0]), "o": row[1], "h": row[2],
                        "l": row[3], "c": row[4], "v": row[5] if len(row) > 5 else None,
                        "oi": row[6] if len(row) > 6 else None})
    return out


def load_offline(path):
    """Offline fixture: a candles envelope, an instrument-keyed JSON doc, or a
    folder of them. -> ({instrument_key: raw rows}, {YYYY-MM-DD, ...})."""
    if os.path.isdir(path):
        names = sorted(n for n in os.listdir(path) if n.endswith(".json"))
        if not names:
            raise EntryError("no .json files in %s" % path)
        paths = [os.path.join(path, n) for n in names]
    elif os.path.isfile(path):
        paths = [path]
    else:
        raise EntryError("no offline file or folder at %s" % path)
    doc, days, envelope = {}, set(), None
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError) as e:
            raise EntryError("cannot read %s: %s" % (p, e))
        if isinstance(d, dict) and isinstance(d.get("data"), dict) and "candles" in d["data"]:
            envelope = d["data"]["candles"]
            continue
        if not isinstance(d, dict):
            raise EntryError("%s is not an instrument-keyed fixture or a candles envelope"
                             % os.path.basename(p))
        for key, value in d.items():
            if isinstance(value, dict) and isinstance(value.get("data"), dict):
                value = value["data"].get("candles")
            if isinstance(value, list):
                doc[str(key)] = value
    for rows in list(doc.values()) + ([envelope] if envelope else []):
        for row in rows:
            try:
                days.add(str(row[0])[:10])
            except (IndexError, TypeError):
                continue
    if not days:
        raise EntryError("%s holds no candle rows" % os.path.basename(path.rstrip("/\\")))
    return doc, days, envelope


class OfflineSource:
    """Recorded Upstox data played back through the intraday() seam."""

    def __init__(self, doc, envelope=None):
        self.doc = doc
        self.envelope = envelope

    def intraday(self, instrument_key, interval="1m"):
        if interval not in ("1m", "5m"):
            raise EntryError("intraday interval is 1m or 5m, got %r" % (interval,))
        rows = self.doc.get(instrument_key) if self.doc else None
        if rows is None and self.envelope is not None:
            rows = self.envelope
        if rows is None:
            raise EntryError("no recorded intraday bars for %s in the offline fixture"
                             % instrument_key)
        return rows


# ---------------------------------------------------------------- instrument

def _instrument(a, sym):
    """(instrument_key, normalized row or None) for sym: --key wins, then
    --master, then the scanner's universe file. The master row carries
    tick_size and lot_size."""
    if getattr(a, "key", None):
        row = None
        if getattr(a, "master", None):
            try:
                row = instruments_mod.find(instruments_mod.load(read_any(a.master)), sym)
            except instruments_mod.InstrumentError as e:
                raise EntryError("%s is not in the instrument master %s: %s" % (sym, a.master, e))
        return a.key, row
    if getattr(a, "master", None):
        try:
            rows = instruments_mod.load(read_any(a.master))
            row = instruments_mod.find(rows, sym)
        except instruments_mod.InstrumentError as e:
            raise EntryError("%s is not in the instrument master %s: %s" % (sym, a.master, e))
        return row["instrument_key"], row
    uni = getattr(a, "universe", None) or DEFAULT_UNIVERSE
    if os.path.exists(uni):
        with open(uni, "r", encoding="utf-8") as fh:
            for ln in fh:
                parts = ln.strip().split("|")
                if parts and parts[0].upper() == sym and len(parts) >= 3:
                    return parts[-1], None
    raise EntryError("no instrument_key for %s: pass --key, --master FILE, or run "
                     "/trader-scanner first (it writes %s)" % (sym, DEFAULT_UNIVERSE))


# ---------------------------------------------------------------- the rule

def evaluate(source, symbol, instrument_key, day, at, setup="orb", or_minutes=15, level=None,
             min_volume_x=None, entry_start=None, entry_cutoff=None, inst=None):
    """Pure function: the setup's status at `at` (aware IST) from one day's bars.
    Bars are Upstox 1m candles resampled to the 5m grid; only bars complete at
    `at` count. One trigger per symbol per day by construction."""
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    if level is not None and level <= 0:
        raise EntryError("--level must be a price above 0")
    if or_minutes <= 0 or or_minutes % BAR_MIN:
        raise EntryError("--or-minutes must be a whole number of %dm bars" % BAR_MIN)
    if min_volume_x is not None and min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    n_or = or_minutes // BAR_MIN
    inst = inst or {}
    open_dt, close_dt = _ist(day, OPEN), _ist(day, CLOSE)
    entry_start = entry_start or datetime.time(9, 30)
    entry_cutoff = entry_cutoff or datetime.time(15, 0)
    start_dt = _ist(day, entry_start)
    cutoff_dt = _ist(day, entry_cutoff)
    res = {"symbol": symbol, "instrument_key": instrument_key, "setup": setup, "side": "buy",
           "date": day.isoformat(), "interval": "%dm" % BAR_MIN, "or_minutes": or_minutes,
           "now": fmt_ist(at), "status": "waiting", "phase": "", "reason": "",
           "opening_range": None, "level": None, "trigger": None, "last": None, "forming": None,
           "min_volume_x": min_volume_x, "entry_start": entry_start.strftime("%H:%M"),
           "entry_cutoff": entry_cutoff.strftime("%H:%M"),
           "tick_size": inst.get("tick_size"), "lot_size": inst.get("lot_size"), "_bars": []}

    if not cal.is_trading_day(day):
        why = "weekend" if cal.is_weekend(day) else ("NSE holiday: %s" % cal.load_holidays().get(day.isoformat())
                                                     if cal.is_holiday(day) else "not a trading day")
        res.update(status="no-data", phase="closed", reason="NSE is closed on %s (%s)" % (day.isoformat(), why),
                   next_open=fmt_ist(cal.next_open(open_dt)))
        return res
    try:
        rows = source.intraday(instrument_key, "1m")
    except (upstox_mod.UpstoxError, EntryError) as e:
        res.update(status="no-data", phase="open", reason=str(e))
        return res
    bars = [b for b in upstox_mod.resample(bars_of(rows), BAR_MIN) if b["dt"].date() == day]
    for b in bars:
        slot = int((b["dt"] - open_dt).total_seconds()) // (BAR_MIN * 60)
        b["slot"] = slot
        b["end"] = b["dt"] + datetime.timedelta(minutes=BAR_MIN)
    closed = [b for b in bars if b["end"] <= at]
    res["_bars"] = closed
    res["bars_closed"] = len(closed)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)

    or_bars = [b for b in closed if b["slot"] < n_or]
    avg_vol = (sum((b.get("v") or 0) for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "mid": r2((max(b["h"] for b in or_bars) + min(b["l"] for b in or_bars)) / 2),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": len(or_bars) >= n_or and at >= or_end,
                                "avg_volume": int(round(avg_vol)) if avg_vol else 0}
    last = closed[-1] if closed else None
    if last:
        res["last"] = {"bar_start": last["dt"].strftime("%H:%M"), "close": r2(last["c"])}

    if at < open_dt:
        res.update(phase="pre-open", reason="NSE opens at 09:15 IST%s" % (
            ", the %d-minute range is set at %s" % (or_minutes, or_end.strftime("%H:%M")) if setup == "orb" else ""))
        return res

    if setup == "orb":
        if at < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s IST" % (
                len(or_bars), n_or, or_end.strftime("%H:%M")))
            return res
        if len(or_bars) < n_or:
            res.update(status="no-data", phase="open",
                       reason="Upstox sent %d of %d opening-range bars for %s" % (len(or_bars), n_or, day.isoformat()))
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in closed:
        if b["slot"] < first_slot or b["end"] < start_dt or b["end"] > cutoff_dt:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if at < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (trig["dt"].strftime("%H:%M"),
                                                          "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"at": fmt_ist(trig["end"]), "bar_start": trig["dt"].strftime("%H:%M"),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig.get("v"), "volume_x": r2(vx),
                          "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((at - trig["end"]).total_seconds() // 60)
        return res
    if not closed:
        res.update(status="no-data", phase="open",
                   reason="Upstox sent no closed bars for %s yet" % day.isoformat())
        return res
    what = "close above the range high %.2f" % lvl if setup == "orb" else "close above %.2f" % lvl
    if at >= cutoff_dt:
        res.update(status="expired", phase="after-close" if at >= close_dt else "after-cutoff",
                   reason="no %s by %s IST, the last time for a new entry" % (what, res["entry_cutoff"]))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %.2f but on %.2fx the range's volume, under your %.2fx" % (
            last["dt"].strftime("%H:%M"), last["c"], last["v"] / float(avg_vol) if avg_vol else 0, min_volume_x)
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %.2f (%s bar), %.2f %s %s (%+.2f%%)" % (
            last["c"], last["dt"].strftime("%H:%M"), abs(gap), "under" if gap >= 0 else "over",
            "the range high %.2f" % lvl if setup == "orb" else "the level %.2f" % lvl,
            100.0 * (last["c"] / lvl - 1))
    return res


def entry_doc(res, at):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    doc = {"created_by": "trader-entry", "symbol": res["symbol"], "instrument_key": res["instrument_key"],
           "setup": res["setup"], "side": "buy", "date": res["date"], "status": res["status"],
           "time": t.get("at"), "bar_start": t.get("bar_start"), "bar_index": t.get("bar_index"),
           "price": t.get("price"), "level": res.get("level"), "interval": res["interval"],
           "opening_range": {"high": orr["high"], "low": orr["low"], "mid": orr["mid"],
                             "minutes": orr["minutes"]} if orr else None,
           "source": "upstox", "checked_at": fmt_ist(at), "reason": res.get("reason")}
    if res.get("tick_size") is not None:
        doc["tick_size"] = res["tick_size"]
    if res.get("lot_size") is not None:
        doc["lot_size"] = res["lot_size"]
    return doc


# ---------------------------------------------------------------- words

def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %.2f" % res["level"] if res.get("level") is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    out = ["%s  %s  %s on %d-minute Upstox bars resampled from 1m (IST, INR)"
           % (res["symbol"], res["now"], setup_name(res), BAR_MIN)]
    orr = res.get("opening_range")
    if orr:
        end = (datetime.datetime.combine(datetime.date(2000, 1, 1), OPEN)
               + datetime.timedelta(minutes=orr["minutes"])).time()
        out.append("Opening range 09:15-%s: high %.2f, low %.2f, mid %.2f INR (%d of %d bars%s)." % (
            end.strftime("%H:%M"), orr["high"], orr["low"], orr["mid"], orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %.2f INR: the %s bar (bar %d) closed above %s %.2f (+%.2f%%%s)." % (
            t["at"], t["price"], t["bar_start"], t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", res["level"], t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        out.append("Entry window %s to %s IST. Next: set the stop and target BEFORE you buy: "
                   "/trader-exit (exit.py plan --from trader/entry.json)." % (res["entry_start"], res["entry_cutoff"]))
        if res.get("minutes_since", 0) > BAR_MIN and res.get("last"):
            out.append("That was %d minutes ago; the last close is %.2f (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], res["last"]["close"], res["last"]["bar_start"]))
        out.append("Nothing was placed. No order goes out without your yes.")
    else:
        out.append("%s  %s." % (LABEL.get(res["status"], res["status"].upper()), res["reason"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s." % res["next_open"])
    if res.get("tick_size") is not None:
        out.append("Instrument: tick %.2f, lot %s." % (res["tick_size"], res.get("lot_size") or 1))
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = b["dt"].strftime("%H:%M")
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %.2f to %.2f" % (min(x["l"] for x in seen), max(x["h"] for x in seen))
        if b["slot"] == n_or - 1 and orr.get("complete"):
            note = "opening range set: high %.2f, low %.2f" % (orr["high"], orr["low"])
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %.2f (%+.2f%%). Get in now if you take it: %.2f INR" % (
            lvl, t["pct_over"], t["price"])
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %.2f" % lvl
        avg = orr.get("avg_volume")
        if res.get("min_volume_x") and avg:
            note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
    else:
        note = "%.2f under %.2f" % (lvl - b["c"], lvl)
    return "%5s bar  closed %.2f  %s" % (start, b["c"], note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def _sleep(seconds):
    time.sleep(seconds)


def watch_loop(source, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    source exposes .intraday(instrument_key, interval); clock() returns an
    aware IST datetime. It sleeps in steps of at most `every` seconds and
    re-reads the clock each time, so a computer that dozed off catches up."""
    printed, errors, said_preopen, first = -1, 0, False, True
    while True:
        now = clock()
        day = now.date()
        if not cal.is_trading_day(day):
            emit("%s: NSE is closed today (%s). Nothing to watch." % (day.isoformat(),
                                                                      "weekend" if cal.is_weekend(day) else "holiday"))
            return 1, None
        open_dt, close_dt = _ist(day, OPEN), _ist(day, CLOSE)
        if now < open_dt + datetime.timedelta(minutes=BAR_MIN):
            if not said_preopen:
                emit("%s  %s: watching from the first bar's close at %s." % (
                    fmt_ist(now), "not open yet" if now < open_dt else "open, no bar closed yet",
                    fmt_ist(open_dt + datetime.timedelta(minutes=BAR_MIN))))
                said_preopen = True
            sleep(max(1, min(every, (open_dt + datetime.timedelta(minutes=BAR_MIN + GRACE // 60) - now).total_seconds())))
            continue
        try:
            res = evaluate(source, opts["symbol"], opts["instrument_key"], day, now, **opts["rule"])
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s  could not read bars: %s" % (fmt_ist(now), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            sleep(every)
            continue
        bars = res["_bars"]
        lines = describe(res)
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or line.startswith("TRIGGERED 20"):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or now >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        cutoff_dt = _ist(day, parse_hhmm(res["entry_cutoff"], "entry_cutoff"))
        if until and now >= _ist(day, until):
            emit("Stopped at --until %s without a trigger. %s." % (until.strftime("%H:%M"), res["reason"]))
            return 1, res
        cur_slot = int((now - open_dt).total_seconds()) // (BAR_MIN * 60)
        nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * BAR_MIN * 60 + GRACE)
        wait = (nxt - now).total_seconds()
        for stop_at in (cutoff_dt, close_dt, _ist(day, until) if until else None):
            if stop_at and stop_at > now:
                wait = min(wait, (stop_at - now).total_seconds())
        sleep(max(1, min(every, wait)))


# ---------------------------------------------------------------- commands

def _session_opts(cfg):
    times = cfgmod.session_times(cfg)
    return {"entry_start": times["entry_start"], "entry_cutoff": times["entry_cutoff"]}


def _rule_opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("long only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    opts = {"setup": a.setup, "or_minutes": a.or_minutes, "level": a.level, "min_volume_x": a.min_volume_x}
    opts.update(_session_opts(cfg))
    return opts


def _clock(a, source, cfg):
    """(at, source). --now pins the clock; --offline plays a fixture instead of
    the network (default replay time 10:00 IST on the fixture's latest day)."""
    if a.offline:
        doc, days, envelope = load_offline(a.offline)
        if a.now:
            at = parse_ist(a.now)
        else:
            at = _ist(datetime.date.fromisoformat(max(days)), REPLAY_AT)
        return at, OfflineSource(doc, envelope)
    at = parse_ist(a.now) if a.now else now_ist()
    return at, source or upstox_mod.Client(cfg)


def cmd_check(a, source=None):
    cfg = cfgmod.load(a.config)
    sym = a.symbol.upper()
    rule = _rule_opts(a, cfg)
    key, inst = _instrument(a, sym)
    at, src = _clock(a, source, cfg)
    day = parse_day(a.date, "--date") if a.date else at.date()
    res = evaluate(src, sym, key, day, at, inst=inst, **rule)
    doc = entry_doc(res, at) if a.write else None
    if a.write:
        write_json(a.write, doc)
    if a.json:
        # --json prints the artifact that --write saves when there is one,
        # the full evaluate() result otherwise.
        print(json.dumps(doc if doc is not None else public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a, source=None):
    cfg = cfgmod.load(a.config)
    sym = a.symbol.upper()
    rule = _rule_opts(a, cfg)
    key, inst = _instrument(a, sym)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    opts = {"symbol": sym, "instrument_key": key, "rule": rule}
    if a.offline or a.now:
        at, src = _clock(a, source, cfg)
        clock = SimClock(at)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        src = source or upstox_mod.Client(cfg)
        now_fn, sleep_fn = now_ist, _sleep
    until = parse_hhmm(a.until, "--until") if a.until else None

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(src, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a, source=None):
    cfg = cfgmod.load(a.config)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)"
                         % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    entries = [h for h in (doc.get("symbols") or doc.get("hits") or [])
               if isinstance(h, dict) and h.get("symbol")]
    if not entries:
        raise EntryError("no symbols in %s: the scanner found nothing to watch" % a.watchlist)
    master_index = {}
    if a.master:
        try:
            master_index = instruments_mod.index(instruments_mod.load(read_any(a.master)))
        except instruments_mod.InstrumentError as e:
            raise EntryError("%s is not an instrument master: %s" % (a.master, e))
    if a.offline:
        idoc, days, envelope = load_offline(a.offline)
        src = OfflineSource(idoc, envelope)
        day = parse_ist(a.now).date() if a.now else datetime.date.fromisoformat(max(days))
        at = parse_ist(a.now) if a.now else _ist(day, REPLAY_AT)
    else:
        src = source or upstox_mod.Client(cfg)
        at = parse_ist(a.now) if a.now else now_ist()
        day = at.date()
    rule = _rule_opts(a, cfg)
    rows = []
    for h in entries:
        sym = str(h["symbol"]).upper()
        inst = master_index.get(sym)
        if inst is None and (h.get("tick_size") is not None or h.get("lot_size") is not None):
            inst = {"tick_size": h.get("tick_size"), "lot_size": h.get("lot_size")}
        key = h.get("instrument_key")
        level = a.level if (a.setup == "level" and a.level is not None) else h.get("level")
        if a.setup == "level" and level is None:
            rows.append((sym, None, "needs a level: --level PRICE or a \"level\" in the watchlist"))
            continue
        if not key:
            rows.append((sym, None, "no instrument_key: run /trader-scanner first"))
            continue
        try:
            use = dict(rule)
            if level is not None:
                use["level"] = float(level)
            res = evaluate(src, sym, key, day, at, inst=inst or {}, **use)
            rows.append((sym, res, None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e}
                          for s, r, e in rows], indent=2))
    else:
        print("%-10s  %-9s  %-21s  %-9s  %-9s  %s" % ("SYMBOL", "STATUS", "TRIGGER AT", "PRICE", "LAST", "NOTE"))
        for s, r, e in rows:
            if e:
                print("%-10s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            print("%-10s  %-9s  %-21s  %-9s  %-9s  %s" % (
                s, r["status"], t.get("at") or "-", ("%.2f" % t["price"]) if t else "-",
                ("%.2f" % last["close"]) if last else "-", r.get("reason") or ""))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /trader-exit." % (n, len(rows)))
        print("Upstox 1m bars resampled to 5m. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None, source=None):
    ap = argparse.ArgumentParser(
        description="Entry Timer (trader-entry): watches a setup bar by bar on Upstox 5m bars, IST.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--key", help="Upstox instrument_key, e.g. NSE_EQ|INE002A01018")
        p.add_argument("--master", metavar="FILE", help="Upstox instrument master .json/.json.gz "
                                                        "(supplies tick_size and lot_size)")
        p.add_argument("--universe", help="universe file, one SYMBOL|ISIN|instrument_key a line "
                                          "(default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-26 10:00"')
        p.add_argument("--date", help="session date YYYY-MM-DD (default: the --now date, or today)")
        p.add_argument("--offline", metavar="PATH", help="recorded Upstox fixture (candles envelope, "
                                                         "instrument-keyed doc, or a folder) instead of the network")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const=DEFAULT_OUT, metavar="PATH",
                   help="save the result (default %s)" % DEFAULT_OUT)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls (default 30)")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", default=DEFAULT_OUT, help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every symbol in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default=DEFAULT_WATCHLIST)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a, source=source)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 2
    except (cfgmod.ConfigError, ValueError) as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
