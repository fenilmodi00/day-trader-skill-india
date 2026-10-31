#!/usr/bin/env python3
"""News monitor for trader-news (Indian markets). Standard library only.

Watches headlines from Indian feeds for the stocks you watch or hold and
prints only what is new since the last check, newest first, with how far each
stock has moved today and a flag on anything that touches your tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers RELIANCE TCS --hours 12
    python3 news.py --json
    python3 news.py --feed broken.xml,moneycontrol.xml

Sources (configurable, defaults):
  - Yahoo Finance India RSS per symbol (SYMBOL.NS)
  - Moneycontrol markets RSS
  - Economic Times markets RSS

Optional NSE/BSE announcement links are read when a feed carries them and
skipped silently when blocked; one dead feed never fails the run.

Move-% is LTP vs previous close from Upstox quotes; MOVING at 3% or more.

Times print in IST. State: trader/state/news.json remembers what has been
shown (30 days). Offline: --offline DIR reads the feed files from DIR.

Headlines are leads, not facts: open the story before acting.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from trader_core import config, timeutil
from trader_core.http import UpstoxAuthError, UpstoxError
from trader_core.upstox import Client

KEEP_DAYS = 30
MOVE_PCT = 3.0

YAHOO_INDIA = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"

DEFAULT_FEEDS = [
    # (name, url, offline_name, per_symbol)
    ("Yahoo Finance", YAHOO_INDIA, "yahoo-%s.xml", True),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml", "moneycontrol.xml", False),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
     "et-markets.xml", False),
]
FEEDS = DEFAULT_FEEDS

KEYWORDS = [
    ("results", r"\bresults\b|\bearnings\b|\bprofit\b|\brevenue\b|\bquarterly\b|\bQ[1-4]\b"),
    ("earnings", r"\bresults\b|\bearnings\b|\bprofit\b|\brevenue\b|\bquarterly\b|\bQ[1-4]\b"),
    ("board meeting", r"\bboard meeting\b|\bboard meet\b"),
    ("dividend", r"\bdividend\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bstock split\b|\bsplit\b|\bbous\b"),
    ("bonus/split/buyback", r"\bbonus\b|\bsplit\b|\bbuyback\b|\brepurchase\b"),
    ("delisting", r"\bdelist"),
    ("ASM/GSM", r"\bASM\b|\bGSM\b|\bsurveillance\b"),
    ("circuit change", r"\bcircuit\b|\bprice band\b"),
    ("order win", r"\border\b|\bcontract\b|\bwin\b|\bbagged\b"),
    ("contract", r"\border\b|\bcontract\b|\bwin\b|\bbagged\b"),
    ("regulatory", r"\bSEBI\b|\bregulatory\b|\bapproval\b|\bcompliance\b"),
    ("SEBI", r"\bSEBI\b|\bregulatory\b|\bapproval\b|\bcompliance\b"),
    ("management change", r"\bCEO\b|\bCFO\b|\bMD\b|\bdirector\b|\bappoint|\bresign|\bmanagement\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bdirector\b|\bappoint|\bresign|\bmanagement\b"),
    ("rating", r"\brating\b|\bupgrade\b|\bdowngrade\b|\bCrisil\b|\bICRA\b|\bCARE\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bhack|\bbreach\b|\boutage\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)"),
    ("macro", r"\bFed\b|\bFOMC\b|\bCPI\b|\binflation\b|\btariffs?\b|\bfutures\b"),
]

# URL-free on purpose: NSE's edge stalls User-Agents that contain a URL.
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/3.0)"


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time


def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("symbols", []) or wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


# ---------------------------------------------------------------- fetching


class Fetcher(object):
    def __init__(self, offline=None, pause=0.15):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


# ---------------------------------------------------------------- parsing


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> aware IST datetime, or None. A value
    without a zone is taken as IST."""
    value = (value or "").strip()
    if not value:
        return None
    d = None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timeutil.IST)
    return timeutil.to_ist(d)


def parse_rss(data, ticker, source="Yahoo Finance"):
    """RSS feed -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS feed is not valid XML: %s" % exc)
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": source, "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def yahoo_symbol(ticker):
    sym = (ticker or "").strip().upper()
    if "." in sym:
        return sym
    return sym + ".NS"


# ---------------------------------------------------------------- tagging


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\b%s\b" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it.get("ticker") in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = []
        seen_tags = set()
        for label, rx in KEYWORDS:
            if re.search(rx, hay, re.I) and label not in seen_tags:
                it["tags"].append(label)
                seen_tags.add(label)
    return items


def dedupe(items):
    seen, out = {}, []
    for it in items:
        norm_title = re.sub(r"\W+", " ", (it.get("title") or "").lower()).strip()
        keys = [it.get("id"), it.get("link"),
                "%s|%s" % (norm_title, it["time"].date() if it.get("time") else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1((it.get("id") or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- collect


def _normalize_feed(feed):
    if isinstance(feed, (list, tuple)):
        name = feed[0] if len(feed) > 0 else "feed"
        url = feed[1] if len(feed) > 1 else ""
        offline = feed[2] if len(feed) > 2 else None
        per_symbol = feed[3] if len(feed) > 3 else ("%s" in str(url))
        src_name = feed[4] if len(feed) > 4 else name
        return (name, url, offline, per_symbol, src_name)
    if isinstance(feed, dict):
        name = feed.get("name") or feed.get("source") or "feed"
        url = feed.get("url") or feed.get("feed") or ""
        offline = feed.get("offline") or feed.get("file") or feed.get("offline_name")
        per_symbol = feed.get("per_symbol")
        if per_symbol is None:
            per_symbol = "%s" in str(url)
        src_name = feed.get("source") or name
        if offline is None:
            offline = None
        return (name, url, offline, per_symbol, src_name)
    s = str(feed).strip()
    aliases = {f[0].lower(): f for f in DEFAULT_FEEDS}
    # also alias by common tokens
    alias_tokens = {
        "yahoo": DEFAULT_FEEDS[0],
        "moneycontrol": DEFAULT_FEEDS[1],
        "mc": DEFAULT_FEEDS[1],
        "et": DEFAULT_FEEDS[2],
        "economic": DEFAULT_FEEDS[2],
        "economictimes": DEFAULT_FEEDS[2],
    }
    low = s.lower()
    if low in aliases:
        f = aliases[low]
        return (f[0], f[1], f[2], f[3], f[0])
    if low in alias_tokens:
        f = alias_tokens[low]
        return (f[0], f[1], f[2], f[3], f[0])
    if s.endswith(".xml") or s.endswith(".rss"):
        return (s, s, s, False, s)
    if "://" in s:
        base = s.rsplit("/", 1)[-1] or s
        return (base, s, base, "%s" in s, base)
    # unknown: treat as alias-ish filename
    return (s, s, s, "%s" in s, s)


def _offline_name(feed, sym):
    name, url, offline, per_symbol, src = feed
    if offline:
        if per_symbol and "%s" in offline:
            return offline % (sym + ".NS" if "." not in sym else sym)
        return offline
    base = (url or "").rsplit("/", 1)[-1] or "feed.xml"
    if per_symbol and "%s" in base:
        return base % (sym + ".NS" if "." not in sym else sym)
    return base


def _feed_url(feed, sym):
    name, url, offline, per_symbol, src = feed
    if per_symbol and "%s" in str(url):
        return url % (sym + ".NS" if "." not in sym else sym)
    return url


def collect(fetcher, watch, state, feeds=None, errors=None, count=None):
    errors = [] if errors is None else errors
    items = []
    feed_list = [ _normalize_feed(f) for f in (feeds if feeds is not None else FEEDS) ]
    for feed in feed_list:
        name, url, offline, per_symbol, src_name = feed
        symbols = sorted(watch) if per_symbol else [None]
        for sym in symbols:
            fname = _offline_name(feed, sym if sym else "")
            furl = _feed_url(feed, sym if sym else "")
            ticker = sym if sym else ""
            try:
                data = fetcher.get(furl, fname)
                items += parse_rss(data, ticker, source=src_name)
            except NewsError as exc:
                msg = str(exc)
                # blocked NSE/BSE-style feeds are skipped silently
                if "HTTP 403" in msg or "HTTP 401" in msg or "HTTP 404" in msg:
                    continue
                errors.append("%s: %s" % (src_name, exc))
            except Exception as exc:  # pragma: no cover - defensive
                errors.append("%s: %s" % (src_name, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)
    keep_cut = now - _dt.timedelta(days=KEEP_DAYS)
    out = []
    for it in items:
        if it.get("time") is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        stamp = seen.get(fp)
        fresh = None
        if stamp:
            try:
                fresh = parse_time(stamp)
            except (TypeError, ValueError):
                fresh = None
        # a seen stamp older than KEEP_DAYS (or unreadable) no longer suppresses
        it["new"] = fresh is None or fresh < keep_cut
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i.get("time") or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


# ---------------------------------------------------------------- moves


def collect_moves(source, watch, errors=None, keys=None):
    errors = [] if errors is None else errors
    out = []
    if source is None:
        return out
    keys = keys or {}
    key_list = []
    missing = []
    for sym in sorted(watch):
        k = keys.get(sym)
        if not k:
            missing.append(sym)
            continue
        if k not in key_list:
            key_list.append(k)
    if missing:
        errors.append("no instrument_key for %s: run /trader-scanner so the watchlist carries the "
                      "keys; the quote was not requested" % ", ".join(missing))
    if not key_list:
        return out
    raw_quotes = {}
    try:
        raw_quotes = source.quote(key_list) or {}
    except UpstoxAuthError as exc:
        errors.append("Upstox rejected the access token (%s): re-authenticate with Upstox "
                      "to refresh the token, then run the check again" % exc)
        return out
    except (UpstoxError, OSError) as exc:
        errors.append("quote request failed: %s" % exc)
        return out
    for sym in sorted(watch):
        k = keys.get(sym)
        if not k:
            continue
        bar = raw_quotes.get(k)
        if not bar or bar.get("ltp") is None or bar.get("prev_close") is None:
            errors.append("no quote for %s" % sym)
            continue
        price = float(bar["ltp"])
        prev = float(bar["prev_close"])
        pct = round((price / prev - 1) * 100, 2)
        out.append({"ticker": sym, "price": round(price, 2), "prev_close": round(prev, 2),
                    "pct": pct, "time": bar.get("ts")})
    return out


# ---------------------------------------------------------------- render


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = ""
        if m.get("time"):
            when = " at %s" % timeutil.fmt_ist(m["time"])
        out.append("%-6s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"],
            "{:,.2f}".format(m["prev_close"]), when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = timeutil.fmt_ist(it["time"]) if it.get("time") else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it.get("touches", [])) or it.get("ticker", "")
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it.get("source", ""), it.get("title", "")))
        extra = []
        if it.get("tags"):
            extra.append("tags: " + ", ".join(it["tags"]))
        if it.get("link"):
            extra.append(it["link"])
        if extra:
            out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % timeutil.fmt_ist(now))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it.get("time") else None
        out.append(d)
    return out


# ---------------------------------------------------------------- main


def main(argv=None, source=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--seen", action="store_true", help="show seen items too")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="trader/news.json",
                    help="artifact file written on a normal run (not --dry-run)")
    ap.add_argument("--feed", "--feeds", dest="feed", help="comma separated feeds")
    args = ap.parse_args(argv)

    if args.tickers:
        watch = {t.upper(): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2

    # instrument keys for moves
    keys_map = {}
    wl = load_json(args.watchlist) or {}
    for h in wl.get("symbols", []) or wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym and h.get("instrument_key"):
            keys_map[sym.upper()] = h.get("instrument_key")
    pl = load_json(args.plan) or {}
    if pl.get("symbol") and pl.get("instrument_key"):
        keys_map.setdefault(pl["symbol"].upper(), pl.get("instrument_key"))

    cfg = config.load(args.config)
    # point state_dir at --state if provided
    if args.state:
        cfg = dict(cfg)
        cfg["state_dir"] = os.path.dirname(os.path.abspath(args.state)) or cfg.get("state_dir")
    state_name = os.path.basename(args.state) if args.state else "news.json"

    now = parse_time(args.now) if args.now else timeutil.now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timeutil.IST)
    now = timeutil.to_ist(now)

    state = config.state_read(cfg, state_name, {})
    if not isinstance(state, dict):
        state = {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    feeds = None
    if args.feed:
        feeds = [x.strip() for x in args.feed.split(",") if x.strip()]
    items = collect(fetcher, watch, state, feeds=feeds, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=(args.all or args.seen))

    moves = []
    if not args.no_moves:
        src = source if source is not None else None
        if src is None:
            try:
                src = Client(cfg)
            except Exception:
                src = None
        moves = collect_moves(src, watch, errors=errors, keys=keys_map)

    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))

    if not args.dry_run:
        remember(items, state, now)
        config.state_write(cfg, state_name, state)
        artifact = {"created_by": "trader-news", "checked": now.isoformat(),
                    "moves": [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                                   moving=abs(m["pct"]) >= args.move_pct) for m in moves],
                    "items": _json_ready(shown), "notes": errors}
        folder = os.path.dirname(os.path.abspath(args.out))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, indent=2)
            fh.write("\n")

    auth_failed = any("re-authenticate" in (e or "") for e in errors)
    if auth_failed:
        # also print re-auth guidance to stderr
        for e in errors:
            if "re-authenticate" in (e or ""):
                print("news: %s" % e, file=sys.stderr)
        return 2
    return 0 if any(i.get("new") for i in shown) else 1




if __name__ == "__main__":
    sys.exit(main())
