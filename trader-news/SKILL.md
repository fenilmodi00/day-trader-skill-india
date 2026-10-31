---
name: trader-news
description: >-
  News Monitor for Indian markets. Tracks breaking news on every stock you
  watch from Yahoo Finance India (SYMBOL.NS), Moneycontrol and Economic Times
  RSS, plus optional NSE/BSE announcement links, tags each item for Indian
  catalysts (results, board meeting, dividend, bonus/split/buyback, delisting,
  ASM/GSM, circuit change, order win, regulatory/SEBI, management change,
  rating), and shows how far each stock is from yesterday's close using
  Upstox quotes. Shows only what is new since the last check, newest first,
  in IST, and never repeats one. Use when the user says "any news on
  RELIANCE", "why is this moving", "what's the news on my watchlist",
  "morning briefing", "pre-market news", "monitor the news", "did anything
  break", or after /trader-scanner finds setups.
argument-hint: "[tickers]"
---

# trader-news

A chart can look perfect and still be about to gap on an earnings date, a
halt or a lawsuit. Before a setup becomes a trade, this skill reads the
feeds that carry those first, for every stock on the watchlist, and tells
you only what is new. It also prints how far each stock has moved today,
so a headline and a move sit side by side.

Tool in this folder (standard library Python, Upstox for prices):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                         # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold |
| Yahoo Finance India RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS` |
| Moneycontrol markets RSS | `https://www.moneycontrol.com/rss/marketreports.xml` |
| Economic Times markets RSS | markets feed |
| Upstox quotes | LTP vs previous close for the move line |
| `trader/state/news.json` | what has been shown (kept 30 days) |
| `trader/news.json` | the run artifact: checked time, moves, tagged items, notes (`--out`) |

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story and read it. Quote the source and link it.
- **Tags, not codes.** Each item carries one or more Indian catalyst
  tags: results, board meeting, dividend, bonus/split/buyback, delisting,
  ASM/GSM, circuit change, order win, regulatory/SEBI, management change,
  rating.
- **MOVING** marks a stock 3 % or more from yesterday's close
  (`--move-pct`). A move with no headline is worth saying out loud too.
- **Times are IST.** Feed timestamps are parsed to aware IST and printed
  with `fmt_ist`; a timestamp without a zone is taken as IST.
- **Dedupe.** One item per story: same id, same link, or same title on
  the same day.
- **Seen state.** `trader/state/news.json` remembers shown items for 30
  days, so an item shows once.
- **Partial failure.** One dead feed never fails the run; the broken feed
  is reported and the rest still produce items.
- **No predictions.** Report what was published and how far the price is
  from yesterday's close. Never say what the stock will do next.
- **Configurable feeds.** Pass `--feed a.xml,b.xml` or a URL to override
  the defaults.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1
   means nothing new. `--all` or `--seen` shows items already seen,
   `--json` gives everything machine readable, `--dry-run` leaves the
   state alone, `--offline DIR` reads fixtures.
2. Read the items that matter: anything tagged results, halt, regulatory
   or risk, and every MOVING ticker first.
3. Write a short brief, per ticker: what happened, the source and link,
   the move, and one line on why it matters to the setup.
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: an earnings date inside the trade, a halt, an offering.
   Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the open.

## Pricing notes

- The move line uses Upstox LTP against the previous close from a batch
  quote (`quote`). A missing quote is reported per symbol, not fatal.
- An expired or rejected Upstox access token is reported with
  re-authenticate guidance and the check exits 2.
- Sandbox is the default; live needs an explicit config change.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because
  of a headline.
- Never places an order. No order goes out except through
  `/trader-broker`, after `/trader-risk` passes and the user types an
  explicit yes for that order.
- Never hammers a server: one request per feed per ticker, a pause
  between calls.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.