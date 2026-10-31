#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. Long side only.
It never places an order; nothing goes to the broker without your yes, out
loud.

    python3 exit.py plan                                     # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write trader/plan.json
    python3 exit.py plan --symbol RELIANCE --entry 1193.60 --or-high 1192.00 --or-low 1175.30
    python3 exit.py plan --stop atr:1.5 --offline RELIANCE-1m.json   # ATR needs the session's bars
    python3 exit.py plan --symbol TCS --entry 3120.00 --setup level --stop pct:1
    python3 exit.py show                                     # the current trader/plan.json

Prices snap to the instrument's tick_size (orders.round_tick). A long's stop
rounds DOWN, away from the entry; the target rounds UP, away from the entry;
rounding never widens the risk. A risk smaller than one tick is widened to
one tick so a plan always carries real risk. The instrument comes from
--master FILE (the Upstox instrument master, .json or .json.gz) or
--tick/--lot by hand; without it the plan keeps unrounded prices and skips
tick_size/lot_size.

Stops:
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's 5m bars
             up to the entry bar (--offline FILE, Upstox 1m resampled). With
             fewer than 14 bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (never below the entry after rounding):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under one tick (widened when the instrument is known only if the
entry itself allows it), an entry.json that has not triggered (unless you
give --entry), a short (long only).

flat_by (default 15:10 IST from config session.flat_by): flat by then, win or
lose; there is no overnight hold. "min_reward_risk" in the config account
(default 1.0) only warns here; trader-risk enforces it.

Sized by default from the config's max_loss_per_trade_inr (whole lots, never
over it); --budget INR caps that lower. Without an instrument for the lot size
quantity is left out rather than guessed.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import json
import os
import re
import sys
from decimal import ROUND_HALF_UP, Decimal

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from trader_core import OPEN  # noqa: E402
from trader_core import calendar as cal  # noqa: E402
from trader_core import config as cfgmod  # noqa: E402
from trader_core.format import money_text  # noqa: E402
from trader_core import instruments as instruments_mod  # noqa: E402
from trader_core import orders as orders_mod  # noqa: E402
from trader_core import upstox as upstox_mod  # noqa: E402
from trader_core.timeutil import (day_or_none, fmt_ist, hhmm_or_none, now_ist, parse_ist,  # noqa: E402
                                  session_dt)

ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # without an instrument, risk under 0.05% of the price is spread noise
DEFAULT_PLAN = cfgmod.PLAN_PATH
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def parse_day(text, flag="--date"):
    day = day_or_none(text)
    if day is None:
        raise PlanError("%s wants YYYY-MM-DD, got %r" % (flag, text))
    return day


def parse_hhmm(text, what):
    t = hhmm_or_none(text)
    if t is None:
        raise PlanError("%s wants an IST time like 15:10, not %r" % (what, text))
    return t


def num_text(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    return format(Decimal(str(x)).normalize(), "f")


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


def parse_when(text, what):
    """10:00, 10:00 AM, or "2026-10-26 10:00 IST" -> time."""
    text = str(text or "").strip()
    if text.upper().endswith(" IST"):
        text = text[:-4].strip()
    if " " in text:
        text = text.split()[-1]
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", text)
    if not m:
        raise PlanError("%s wants an IST time like 10:00, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants an IST time like 10:00, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants an IST time like 10:00, not %r" % (what, text))
    return datetime.time(h, mi)


def as_decimal(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().lstrip("INR ").replace(",", ""))
    except (ArithmeticError, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def read_any(path):
    if str(path).endswith(".gz"):
        import gzip
        with gzip.open(path, "rb") as fh:
            return fh.read()
    with open(path, "rb") as fh:
        return fh.read()


def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- stop and target words

def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*(?:inr\s+)?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), as_decimal(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = as_decimal(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*(?:inr\s+)?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", as_decimal(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num_text(r), WORDS.get(n, num_text(r)))


# ---------------------------------------------------------------- the math

def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, tick=None):
    """The stop and target, tick-rounded away from the entry (never wider
    risk), a one-tick minimum risk, and the exact risk numbers. Raises
    PlanError with the reason."""
    e = as_decimal(entry)
    if e <= 0:
        raise PlanError("the entry price must be above 0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)"
                            % stop_rule.replace("_", "-"))
        oh, ol = as_decimal(or_high), as_decimal(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s"
                            % (money_text(oh, prefix="INR", trim=False),
                               money_text(ol, prefix="INR", trim=False)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (money_text(oh, prefix="INR", trim=False),
                                             money_text(ol, prefix="INR", trim=False), num_text(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num_text(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (money_text(e, prefix="INR", trim=False),
                                           num_text(stop_param), num_text(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = as_decimal(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num_text(stop_param), atr_bars)
        detail = "ATR %s over the %d bars to the entry%s" % (
            num_text(a.quantize(Decimal("0.0001"))), atr_bars,
            " (fewer than %d so far)" % ATR_BARS if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    # A long's stop rounds down, away from the entry; never up into the risk.
    stop = orders_mod.round_tick(float(raw), tick, "down") if tick else float(raw)
    if tick and orders_mod.round_tick(float(raw), tick, "nearest") != stop and detail:
        detail += ", rounded down to the tick"
    if stop >= float(e):
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (money_text(stop, prefix="INR", trim=False), label,
                           money_text(e, prefix="INR", trim=False)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above 0"
                        % money_text(stop, prefix="INR", trim=False))
    risk = e - as_decimal(stop)
    if tick:
        # One tick is the smallest risk the exchange can even price; a round that
        # ate the whole risk is widened back to one tick, never left at zero.
        if risk < as_decimal(tick):
            if as_decimal(tick) >= e:
                raise PlanError("one tick (%s) is not smaller than the entry %s: no plan fits"
                                % (money_text(tick, prefix="INR", trim=False),
                                   money_text(e, prefix="INR", trim=False)))
            stop = float(as_decimal(stop) - as_decimal(tick))
            risk = e - as_decimal(stop)
    else:
        floor_risk = e * MIN_RISK_PCT / 100
        if risk < floor_risk:
            raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s): "
                            "too tight to survive the spread, normal wiggle would stop you out. Widen it."
                            % (money_text(risk, prefix="INR", trim=False),
                               money_text(floor_risk.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                                          prefix="INR", trim=False),
                               money_text(e, prefix="INR", trim=False)))
    if target_rule == "r":
        target_label = r_label(target_param)
        target = e + target_param * risk
    else:
        target_label = None
        target = target_param
    # A long's target rounds up, away from the entry: rounding never pays less than planned.
    target_f = orders_mod.round_tick(float(target), tick, "up") if tick else float(target)
    if target_f <= float(e):
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money_text(target_f, prefix="INR", trim=False),
                           money_text(e, prefix="INR", trim=False)))
    rr = ((as_decimal(target_f) - e) / risk).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if target_label is None:
        target_label = "your price (%sR)" % ratio(rr)
    return {"entry": float(e), "stop": stop, "stop_label": label, "stop_detail": detail,
            "target": target_f, "target_r": float(target_param) if target_rule == "r" else float(rr),
            "target_label": target_label, "risk": float(risk), "reward_risk": float(rr)}


# ---------------------------------------------------------------- bars for ATR

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


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


def session_bars(rows, day, bar_min=5, upto_slot=None, at=None):
    """Closed N-minute bars of one IST session, oldest first: the 1m rows go
    through the one resample in trader_core.upstox. `at` cuts the session at a
    wall-clock time; upto_slot cuts after the entry bar."""
    open_dt = session_dt(day, OPEN)
    out = []
    for b in upstox_mod.resample(bars_of(rows), bar_min):
        if b["dt"].date() != day:
            continue
        slot = int((b["dt"] - open_dt).total_seconds()) // (bar_min * 60)
        if upto_slot is not None and slot > upto_slot:
            continue
        if at is not None and b["dt"] + datetime.timedelta(minutes=bar_min) > at:
            continue
        out.append({"h": b["h"], "l": b["l"], "c": b["c"]})
    return out


def load_bars_offline(path):
    """ATR bars from --offline: a candles envelope or an instrument-keyed doc."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        raise PlanError("cannot read %s: %s" % (path, e))
    if isinstance(d, dict) and isinstance(d.get("data"), dict) and "candles" in d["data"]:
        return d["data"]["candles"]
    if isinstance(d, dict):
        for value in d.values():
            if isinstance(value, dict) and isinstance(value.get("data"), dict):
                value = value["data"].get("candles")
            if isinstance(value, list) and value:
                return value
    raise PlanError("%s holds no candle rows" % os.path.basename(path))


# ---------------------------------------------------------------- instrument and sizing

def instrument_for(a, symbol):
    """The normalized instrument row (--master), or the --tick/--lot stand-in,
    or None. tick_size/lot_size stay out of the plan without one."""
    if getattr(a, "master", None):
        try:
            return instruments_mod.find(instruments_mod.load(read_any(a.master)), symbol)
        except instruments_mod.InstrumentError as e:
            raise PlanError("%s is not in the instrument master %s: %s" % (symbol, a.master, e))
    if getattr(a, "tick", None) or getattr(a, "lot", None):
        return {"tick_size": a.tick, "lot_size": a.lot, "instrument_key": None}
    return None


def size_for(a, inst, risk_per_share, entry):
    """Whole-lot quantity: the smallest of the budget (explicit --budget or the
    config's max_loss_per_trade_inr), floored to whole lots. None when there is
    nothing to size from. --budget 0 opts out of sizing entirely."""
    budget, max_loss = getattr(a, "budget", None), None
    try:
        cfg = cfgmod.load(a.config)
        max_loss = cfgmod.limits(cfg).get("max_loss_per_trade_inr")
    except cfgmod.ConfigError:
        max_loss = None
    if budget is None:                # no --budget: size from the account cap
        budget = max_loss
    else:
        budget = float(budget)        # explicit: 0 opts out, no config fallback
        if max_loss:
            budget = min(budget, float(max_loss))
    if not budget:
        return None
    if not inst or not inst.get("lot_size") or not risk_per_share or risk_per_share <= 0:
        raise PlanError("--budget sizing needs the instrument's lot_size (pass --master or --lot) "
                        "and a risk above 0")
    lot = int(inst["lot_size"])
    qty = int(budget / (risk_per_share * lot)) * lot
    return max(qty, 0)


# ---------------------------------------------------------------- gather and plan

def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("long only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "instrument_key": a.key, "entry": a.entry,
                "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: long plans only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give "
                            "--entry PRICE to plan ahead" % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "instrument_key": doc.get("instrument_key") or a.key,
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or doc.get("time") or doc.get("entry_time"),
                "entry_bar": a.entry_bar if a.entry_bar is not None else doc.get("bar_index"),
                "interval": doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes,
                "master_tick": doc.get("tick_size"), "master_lot": doc.get("lot_size"), "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    return info


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). flat_by comes from config session.flat_by (15:10 IST);
    the entry must land before it."""
    times = cfgmod.session_times(cfg)
    t = parse_hhmm(explicit, "--flat-by") if explicit else times["flat_by"]
    note = ""
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (entry_time.strftime("%H:%M"), t.strftime("%H:%M")))
    return t.strftime("%H:%M"), note


def build_plan(a, now):
    info = gather(a)
    cfg = cfgmod.load(a.config)
    inst = instrument_for(a, info["symbol"])
    if inst is None and info.get("master_tick"):
        inst = {"tick_size": info["master_tick"], "lot_size": info.get("master_lot"),
                "instrument_key": info.get("instrument_key")}
    tick = inst.get("tick_size") if inst else None
    if tick is not None and float(tick) <= 0:
        raise PlanError("tick_size must be above 0, got %r" % (tick,))

    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, "
                            "pct:X or atr:K" % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    day = parse_day(info["date"], "--date") if info["date"] else now.date()
    if not cal.is_trading_day(day):
        why = "weekend" if cal.is_weekend(day) else "NSE holiday: %s" % cal.load_holidays().get(day.isoformat(), "")
        raise PlanError("%s is not a trading day (%s)" % (day.isoformat(), why))
    entry_time = parse_when(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        if a.offline:
            rows = load_bars_offline(a.offline)
        else:
            key = info.get("instrument_key")
            if not key:
                raise PlanError("an atr stop needs the session's bars: pass --offline FILE or an "
                                "instrument_key (from --key or entry.json)")
            try:
                rows = upstox_mod.Client(cfg).intraday(key, "1m")
            except upstox_mod.UpstoxError as e:
                raise PlanError("cannot read Upstox intraday bars: %s" % e)
        if not cal.is_trading_day(day) or not rows:
            raise PlanError("no bars for %s" % day.isoformat())
        bars = session_bars(rows, day, 5, info.get("entry_bar"),
                            parse_ist(a.now) if a.now else None)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed 5m bars by the entry on %s; found %d"
                            % (day.isoformat(), len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param,
                info["or_high"], info["or_low"], atr, atr_n, tick)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    key = info.get("instrument_key") or (inst or {}).get("instrument_key")
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(as_decimal(info["or_high"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
               "low": float(as_decimal(info["or_low"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": c["entry"], "entry_time": entry_time.strftime("%H:%M") if entry_time else None,
            "entry_bar": info.get("entry_bar"), "interval": "5m", "opening_range": orr,
            "stop": c["stop"], "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": c["target"], "target_r": c["target_r"], "target_label": c["target_label"],
            "risk_per_share": c["risk"], "reward_risk": c["reward_risk"], "flat_by": flat_by,
            "stop_detail": c["stop_detail"], "generated_at": fmt_ist(now, seconds=True), "note": NOTE}
    if key:
        plan["instrument_key"] = key
    if inst and inst.get("tick_size") is not None:
        plan["tick_size"] = inst.get("tick_size")
        plan["lot_size"] = inst.get("lot_size")
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    if inst and inst.get("instrument_key") and inst.get("lot_size"):
        qty = size_for(a, inst, c["risk"], c["entry"])
        if qty:
            plan["quantity"] = qty
    legs = legs_for(plan)
    plan["legs"] = legs["legs"]
    entry_leg = next((one for one in legs["legs"]
                      if isinstance(one, dict) and one.get("role") == "entry"), None)
    plan["risk_inr"] = round(c["risk"] * entry_leg["quantity"], 2) if entry_leg else None
    plan["product"] = "I"
    plan["direction"] = "long"
    warnings = []
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    try:
        mrr = (cfgmod.limits(cfg) or {}).get("min_reward_risk")
        if mrr is not None and c["reward_risk"] < float(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num_text(as_decimal(mrr))))
    except cfgmod.ConfigError:
        pass
    if day != now.date():
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), now.date().isoformat()))
    return plan, warnings


def legs_for(plan):
    """The three v3 legs via orders.leg/plan. Without a quantity the legs stay
    unbuilt (validate_plan wants one): the sizing skill owns that step."""
    key = plan.get("instrument_key")
    qty = plan.get("quantity")
    if not key or not qty:
        return {"legs": []}
    entry_leg = orders_mod.leg(key, "BUY", qty, "LIMIT", product="I", price=plan["entry"], tag="entry")
    stop_leg = orders_mod.leg(key, "SELL", qty, "SL-M", product="I", trigger_price=plan["stop"],
                              market_protection=-1, tag="exit-stop")
    target_leg = orders_mod.leg(key, "SELL", qty, "LIMIT", product="I", price=plan["target"], tag="exit-target")
    return orders_mod.plan(entry_leg, stop_leg, target_leg, symbol=plan.get("symbol"))


# ---------------------------------------------------------------- words and commands

def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = as_decimal(plan["entry"])
    risk = as_decimal(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s IST" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    stop_label = plan.get("stop_label") or plan.get("stop_rule") or "stop"
    target_label = plan.get("target_label") or plan.get("target_rule") or "target"
    out = ["Exit plan: %s long, %s, %s on Upstox 5m bars (IST, INR)"
           % (plan["symbol"], day.strftime("%a %Y-%m-%d"), setup)]
    out.append("  Entry         %s%s" % (money_text(plan["entry"], prefix="INR", trim=False), when))
    out.append("  Stop          %s  %s%s" % (money_text(plan["stop"], prefix="INR", trim=False),
                                             stop_label,
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money_text(plan["target"], prefix="INR", trim=False),
                                           target_label))
    out.append("  Risk a share  %s (%s%% of the entry)" % (
        money_text(plan["risk_per_share"], prefix="INR", trim=False), pct))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    out.append("  Flat by       %s IST, win or lose: no overnight hold" % plan["flat_by"])
    if plan.get("quantity"):
        out.append("  Size          %s shares (whole lots of %s), risk %s at the stop" % (
            plan["quantity"], plan.get("lot_size") or 1,
            money_text(plan.get("risk_inr") or 0, prefix="INR", trim=False)))
    if plan.get("tick_size") is not None:
        out.append("  Tick          %.2f: the stop rounds down, the target up, away from the entry." % plan["tick_size"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a, source=None):
    now = parse_ist(a.now) if a.now else now_ist()
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a, source=None):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    base = {"setup": "orb", "entry_time": None, "entry_bar": None, "quantity": None,
            "lot_size": None, "tick_size": None, "risk_inr": None, "stop_detail": ""}
    for line in describe(dict(base, **plan)):
        print(line)
    today = now_ist().date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None, source=None):
    ap = argparse.ArgumentParser(
        description="Exit Planner (trader-exit): the stop and target before the entry, tick-rounded, IST.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH",
                   help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--key", help="Upstox instrument_key, e.g. NSE_EQ|INE002A01018")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:00 (IST)")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config session.flat_by, 15:10)")
    p.add_argument("--master", metavar="FILE", help="Upstox instrument master .json/.json.gz "
                                                    "(supplies tick_size and lot_size)")
    p.add_argument("--tick", type=float, help="tick_size by hand when there is no master")
    p.add_argument("--lot", type=int, help="lot_size by hand when there is no master")
    p.add_argument("--budget", type=float, metavar="INR", help="size the plan to whole lots of this budget")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="recorded Upstox 1m fixture for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-26 10:05"')
    p.add_argument("--write", nargs="?", const=DEFAULT_PLAN, metavar="PATH",
                   help="save the plan (default %s)" % DEFAULT_PLAN)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default=DEFAULT_PLAN)
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a, source=source)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1
    except (cfgmod.ConfigError, orders_mod.PlanError, ValueError) as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
