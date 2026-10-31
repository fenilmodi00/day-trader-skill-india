#!/usr/bin/env python3
"""India prototype for the Day Trader skill: free NSE data, no API key.

The free Kite Connect Personal app has no market data, so this prototype uses
two free public sources instead:

  universe  NSE's EQUITY_L.csv from nsearchives.nseindia.com (all listed
            symbols, series and ISINs; filtered to the EQ series).
  bars      Yahoo Finance's chart endpoint with the NSE suffix (RELIANCE.NS),
            the same free endpoint the US tools already use. It reports IST
            timestamps and 1m/5m/15m/30m/60m/1d bars.

It then applies the same opening-range breakout rule as trader-entry, on the
Indian session: range = the first --minutes of 09:15 IST, trigger = the first
completed bar after the range that closes above the range high. Stop is the
range midpoint, target is 1R above the entry.

    python3 india.py universe --write
    python3 india.py bars RELIANCE --interval 5m --range 5d
    python3 india.py orb RELIANCE --offline tests/fixtures/trader-india
    python3 india.py orb RELIANCE --date 2026-10-26 --json

This is a data prototype. It places no orders and talks to no broker. Yahoo's
endpoint is unofficial and can lag; Kite Connect is the paid, exchange-grade
path for the real port.

Python 3.8+, standard library only.
"""

import argparse
import csv
import datetime
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
NSE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
BACKOFF = 5.0
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1d": 86400}
INTRADAY = ("1m", "5m", "15m", "30m", "60m")


class IndiaError(Exception):
    pass


# ---------------------------------------------------------------- fetching

def http_get(url, timeout=30, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def yahoo_get(path):
    """GET a Yahoo path: query2 first, then query1 on a 429 or 5xx, with one
    retry after BACKOFF if a 429 was seen."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path).decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                last = "HTTP %d" % exc.code
                try:
                    body = exc.read().decode("utf-8", "replace") if 400 <= exc.code < 500 and exc.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    exc.close()
                if exc.code == 429:
                    saw_429 = True
                    continue
                if exc.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body
                raise IndiaError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as exc:
                last = str(getattr(exc, "reason", exc))
                continue
        if attempt == 0 and saw_429:
            time.sleep(BACKOFF)
            continue
        break
    raise IndiaError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(symbol):
    """RELIANCE -> RELIANCE.NS. An explicit .NS/.BO or ^NSEI is kept as is."""
    sym = symbol.strip().upper()
    if sym.startswith("^") or "." in sym:
        return sym
    return sym + ".NS"


def chart_path(symbol, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false" % (
        urllib.parse.quote(to_yahoo(symbol)), urllib.parse.quote(rng), urllib.parse.quote(interval))


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def read_json(path):
    return json.loads(read_text(path))


def offline_file(path, symbol, kind):
    """--offline may be a file or a folder holding <SYMBOL>.<ext>(.gz)."""
    if os.path.isfile(path):
        return path
    name = to_yahoo(symbol) if kind == "chart" else symbol
    ext = ".csv" if kind == "csv" else ".json"
    for candidate in (name + ext + ".gz", name + ext):
        full = os.path.join(path, candidate)
        if os.path.isfile(full):
            return full
    raise IndiaError("offline: no %s file for %s in %s" % (kind, symbol, path))


# ---------------------------------------------------------------- parsing

def parse_chart(text, symbol):
    """Yahoo v8 chart JSON -> (bars, meta). Bars are IST datetimes in session."""
    try:
        doc = json.loads(text)
        result = doc["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise IndiaError("Yahoo returned something unexpected for %s" % symbol)
    if not result:
        err = (doc.get("chart") or {}).get("error") or {}
        raise IndiaError("no data for %s: %s" % (symbol, err.get("description", "unknown ticker")))
    res = result[0]
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    stamps = res.get("timestamp") or []

    def column(key):
        values = quote.get(key)
        return values if isinstance(values, list) else []

    cols = {key: column(key) for key in ("open", "high", "low", "close", "volume")}
    bars = []
    for i, stamp in enumerate(stamps):
        if any(i >= len(cols[key]) for key in ("open", "high", "low", "close")):
            continue
        o, h, l, c = cols["open"][i], cols["high"][i], cols["low"][i], cols["close"][i]
        if None in (o, h, l, c):
            continue
        when = datetime.datetime.fromtimestamp(stamp, IST)
        if when.time() < OPEN or when.time() >= CLOSE:
            continue  # pre-open and any post-close prints are not session bars
        vol = cols["volume"][i] if i < len(cols["volume"]) else None
        bars.append({"dt": when, "o": float(o), "h": float(h), "l": float(l),
                     "c": float(c), "v": float(vol) if vol is not None else None})
    bars.sort(key=lambda bar: bar["dt"])
    return bars, res.get("meta") or {}


def parse_universe(text, series="EQ"):
    """NSE EQUITY_L.csv -> [{symbol, name, series, isin}], filtered by series."""
    rows = []
    for row in csv.DictReader(text.splitlines()):
        clean = {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        if not clean.get("SYMBOL"):
            continue
        if series and clean.get("SERIES", "").upper() != series.upper():
            continue
        rows.append({"symbol": clean["SYMBOL"], "name": clean.get("NAME OF COMPANY", ""),
                     "series": clean.get("SERIES", ""), "isin": clean.get("ISIN NUMBER", "")})
    return rows


# ---------------------------------------------------------------- opening range

def orb(bars, symbol, interval="5m", minutes=15, day=None, now=None):
    """Opening-range breakout on one IST session. Returns a plain dict."""
    if interval not in INTRADAY:
        raise IndiaError("orb needs an intraday interval, not %s" % interval)
    bar_minutes = INTERVALS[interval] // 60
    if minutes % bar_minutes:
        raise IndiaError("--minutes must be a multiple of the %d-minute bar" % bar_minutes)
    now = now or datetime.datetime.now(IST)
    sessions = sorted({bar["dt"].date() for bar in bars})
    if not sessions:
        raise IndiaError("no session bars for %s" % symbol)
    day = day or sessions[-1]
    if day not in sessions:
        raise IndiaError("no %s bars on %s" % (interval, day.isoformat()))

    session = [bar for bar in bars if bar["dt"].date() == day]
    range_end = datetime.datetime.combine(day, OPEN).replace(tzinfo=IST) + datetime.timedelta(minutes=minutes)
    close_at = datetime.datetime.combine(day, CLOSE).replace(tzinfo=IST)
    cutoff = min(now, close_at) if day == now.date() else (close_at if day < now.date() else now)
    if day > now.date():
        raise IndiaError("%s is in the future" % day.isoformat())

    in_range = [bar for bar in session if bar["dt"].time() < range_end.time()]
    want = minutes // bar_minutes
    out = {"symbol": to_yahoo(symbol), "date": day.isoformat(), "interval": interval,
           "range_minutes": minutes, "range_start": OPEN.strftime("%H:%M"),
           "range_end": range_end.strftime("%H:%M")}
    if len(in_range) < want:
        out.update({"status": "waiting" if cutoff < range_end else "no-data",
                    "reason": "opening range still forming" if cutoff < range_end
                    else "fewer than %d range bars in the source" % want})
        return out

    high = max(bar["h"] for bar in in_range)
    low = min(bar["l"] for bar in in_range)
    mid = round((high + low) / 2.0, 2)
    out.update({"range_high": round(high, 2), "range_low": round(low, 2), "range_mid": mid})

    for bar in session:
        if bar["dt"].time() < range_end.time():
            continue
        if bar["dt"] + datetime.timedelta(minutes=bar_minutes) > cutoff:
            break  # the bar has not closed yet
        if bar["c"] > high:
            entry = round(bar["c"], 2)
            stop = mid
            risk = round(entry - stop, 2)
            out.update({"status": "triggered", "trigger_bar": bar["dt"].strftime("%H:%M"),
                        "trigger_time": (bar["dt"] + datetime.timedelta(minutes=bar_minutes)).strftime("%H:%M"),
                        "entry": entry, "stop": stop, "risk_per_share": risk,
                        "target": round(entry + risk, 2), "reward_risk": 1.0})
            return out

    complete = cutoff >= close_at
    out.update({"status": "expired" if complete else "waiting",
                "reason": "no completed close above the range high by 15:30" if complete
                else "no trigger yet"})
    return out


# ---------------------------------------------------------------- commands

def write_json(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def cmd_universe(args):
    if args.offline:
        text = read_text(offline_file(args.offline, "EQUITY_L", "csv"))
    else:
        try:
            text = http_get(NSE_EQUITY_LIST, timeout=60, ua=NSE_UA).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise IndiaError("NSE answered HTTP %d for EQUITY_L.csv" % exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise IndiaError("NSE: %s" % getattr(exc, "reason", exc))
    rows = parse_universe(text, args.series)
    if args.limit:
        rows = rows[:args.limit]
    print("NSE %s symbols: %d" % (args.series or "all", len(rows)))
    for row in rows[:5]:
        print("  %-14s %s" % (row["symbol"], row["name"][:60]))
    if args.write:
        path = os.path.join("trader", "india-universe.json")
        write_json(path, {"source": "nsearchives.nseindia.com EQUITY_L.csv",
                          "series": args.series, "count": len(rows), "symbols": rows})
        print("wrote %s" % path)
    return 0


def get_chart_bars(args):
    if args.offline:
        text = read_text(offline_file(args.offline, args.symbol, "chart"))
    else:
        text = yahoo_get(chart_path(args.symbol, args.range, args.interval))
    return parse_chart(text, args.symbol)


def cmd_bars(args):
    bars, meta = get_chart_bars(args)
    if not bars:
        raise IndiaError("no session bars for %s" % args.symbol)
    summary = {"symbol": to_yahoo(args.symbol), "interval": args.interval, "range": args.range,
               "bars": len(bars), "first": bars[0]["dt"].isoformat(),
               "last": bars[-1]["dt"].isoformat(),
               "last_price": meta.get("regularMarketPrice"),
               "timezone": meta.get("exchangeTimezoneName")}
    print("%(symbol)s %(interval)s %(range)s: %(bars)d bars" % summary)
    print("  %(first)s -> %(last)s IST" % summary)
    if summary["last_price"] is not None:
        print("  last %(last_price).2f" % summary)
    if args.json:
        print(json.dumps({"summary": summary, "bars": [
            {"t": bar["dt"].isoformat(), "o": bar["o"], "h": bar["h"],
             "l": bar["l"], "c": bar["c"], "v": bar["v"]} for bar in bars]}, indent=2))
    return 0


def cmd_orb(args):
    bars, meta = get_chart_bars(args)
    day = datetime.date.fromisoformat(args.date) if args.date else None
    result = orb(bars, args.symbol, args.interval, args.minutes, day)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("%(symbol)s %(date)s %(interval)s" % result)
        if "range_high" in result:
            print("  range %(range_start)s-%(range_end)s: %(range_low).2f to %(range_high).2f (mid %(range_mid).2f)" % result)
        status = result["status"].upper()
        if result["status"] == "triggered":
            print("  %(status)s close %(entry).2f (bar %(trigger_bar)s, closed %(trigger_time)s)"
                  % dict(result, status=status))
            print("  stop %(stop).2f range mid, target %(target).2f 1R, risk %(risk_per_share).2f/share" % result)
        else:
            print("  %s: %s" % (status, result.get("reason", "")))
    if args.write:
        path = os.path.join("trader", "india-orb.json")
        write_json(path, result)
        print("wrote %s" % path)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Free India data prototype: NSE universe, Yahoo .NS bars, ORB.")
    sub = parser.add_subparsers(dest="command")

    uni = sub.add_parser("universe", help="NSE EQUITY_L.csv symbols")
    uni.add_argument("--series", default="EQ", help="series filter (default EQ; empty string keeps all)")
    uni.add_argument("--limit", type=int, default=0, help="keep only the first N rows")
    uni.add_argument("--write", action="store_true", help="write trader/india-universe.json")
    uni.add_argument("--offline", help="saved EQUITY_L.csv (or a folder holding it)")
    uni.set_defaults(func=cmd_universe)

    bars = sub.add_parser("bars", help="Yahoo .NS chart bars")
    bars.add_argument("symbol")
    bars.add_argument("--interval", default="5m", choices=sorted(INTERVALS))
    bars.add_argument("--range", default="5d")
    bars.add_argument("--json", action="store_true")
    bars.add_argument("--offline", help="saved chart JSON (.json or .json.gz) or a folder")
    bars.set_defaults(func=cmd_bars)

    orb_cmd = sub.add_parser("orb", help="opening-range breakout on the IST session")
    orb_cmd.add_argument("symbol")
    orb_cmd.add_argument("--interval", default="5m", choices=INTRADAY)
    orb_cmd.add_argument("--range", default="5d")
    orb_cmd.add_argument("--date", help="YYYY-MM-DD session (default: the last one in the bars)")
    orb_cmd.add_argument("--minutes", type=int, default=15, help="opening range length (default 15)")
    orb_cmd.add_argument("--json", action="store_true")
    orb_cmd.add_argument("--write", action="store_true", help="write trader/india-orb.json")
    orb_cmd.add_argument("--offline", help="saved chart JSON (.json or .json.gz) or a folder")
    orb_cmd.set_defaults(func=cmd_orb)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except IndiaError as exc:
        print("india: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
