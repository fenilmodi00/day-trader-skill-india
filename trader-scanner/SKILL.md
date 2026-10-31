---
name: trader-scanner
description: >-
  Chart Scanner for the NSE. Builds the tradeable universe from NSE's EQUITY_L
  list and the Nifty 500 constituents, caches it as SYMBOL|ISIN|instrument_key,
  runs a daily setup scan off the official NSE bhavcopy (20-day high breakout,
  20-DMA reclaim, bull flag, oversold, oversold bounce, ranked by turnover) and
  an opening-range breakout scan on Upstox 1-minute candles resampled to 5
  minutes (15-minute range, close-above trigger, one signal per symbol per day),
  then writes trader/watchlist.json for trader-news and trader-entry. All times
  are IST, all money INR, data from NSE and Upstox. Use when the user says
  "scan the market", "find me setups", "what is breaking out", "opening range
  breakouts", "scan for bull flags", "oversold stocks", "pre-market scan",
  "build my watchlist", "what should I look at today", or at the start of an
  NSE trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade on the NSE. It reads the
exchange's own files and Upstox candles, then lists the charts that match a
rule. That is all it does. A scan is a list of charts that match a rule, not a
list of stocks to buy, and a setup on a chart is not a prediction. Most people
who day trade lose money; the scanner does not change that. It only saves you
from flipping through 2,000 charts by hand.

This is not financial advice. The scanner never places an order and never
will: nothing goes to the broker without you saying yes, out loud, to
trader-entry and trader-broker.

Everything here runs on the sandbox by default (`mode: "sandbox"` in
`trader/config.json`). The scanner reads market data only; it has no order
code at all. Going live is trader-broker's job and needs your explicit
confirmation there.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

## Quick start

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe            # NSE lists -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily               # today's bhavcopy -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --bhavcopy FILE.csv   # or a folder of past days
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                 # after 09:35 IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15      # cut the clock at 10:15 IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --symbols RELIANCE --json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --symbols RELIANCE --offline tests/fixtures/trader-scanner/india/RELIANCE-1m-2026-10-21-27.json
```

Flags: `--symbols A,B` (keys resolved from the universe file), `--watchlist
FILE`, `--universe FILE`, `--min-price` (daily floor), `--date
YYYY-MM-DD` and `--bhavcopy FILE-or-DIR` (daily), `--or-minutes 15` and `--at
HH:MM` (orb), `--master FILE` (instrument master .json.gz for offline runs),
`--now "2026-10-26 10:15"` (IST) to pin the clock, `--offline PATH` to read
recorded files instead of the network, `--out`, `--no-write`, `--show N`,
`--json`. Exit code 0 when the scan found something, 1 when it ran clean and
found nothing, 2 when it could not run (the reason is one plain line on
stderr, for example a holiday, a 404, or an empty bhavcopy).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|ISIN\|instrument_key` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of` (IST), `count`, `symbols[]` with `symbol`, `instrument_key`, `isin`, `last`, `change_pct`, `turnover_inr`, `setup`, `tick_size`, `lot_size`, `freeze_quantity`, `orb`, `as_of`. trader-news and trader-entry read `symbols[]` |
| `trader/config.json` | optional; sandbox is the default when it is absent |

## The universe

`universe` merges two lists:

1. NSE's `EQUITY_L.csv` (the full equities list), keeping the EQ series rows.
2. The Nifty 500 constituents. A checked-in seed slice ships with the skill
   at `nifty500.csv`; refresh it from NSE's indices downloads when the index
   committee rebalances. A symbol present on either list makes the cut, EQ
   series wins for the ISIN.

Every kept symbol is then resolved against the Upstox instrument master
(downloaded live, or passed with `--master` for offline runs) so the cache
line carries the exact `instrument_key` Upstox needs, plus the ISIN, tick
size, lot size and freeze quantity live in the master too and reach the
watchlist through it. Symbols with no NSE_EQ master row are counted and
skipped, not guessed.

The NSE session runs 09:15 to 15:30 IST, Monday to Friday, minus the NSE
holiday list (`trader_core/data/nse_holidays.json`). There is no pre-market
scan here: the bhavcopy for a day is published after that day's close, so
`daily` is an end-of-day and next-morning tool, and `orb` is the in-session
tool.

## The daily scan: one bhavcopy, every EQ symbol

`daily` reads the official bhavcopy `sec_bhavdata_full_DDMMYYYY.csv` (NSE
archives). One file covers every EQ symbol for one date: SYMBOL, SERIES,
DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE,
AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, DELIV_QTY, DELIV_PER. Rows outside the
EQ series and rows without a usable close are dropped. The scan closes on:

- `breakout_20d`: the close is above the highest close of the previous 20 sessions.
- `reclaim_ma20`: yesterday closed below its 20-session average, today closed above its.
- `bull_flag`: roughly +10% over 10 sessions, then a tight 5-session flag.
- `oversold`: RSI(14) under 30.
- `oversold_bounce`: yesterday was oversold, today closed up.

Ranking is by turnover (`TURNOVER_LACS` to INR), most traded first, so the
top of the list is where the crowd actually is.

The 20-day rules need 20 sessions of history behind the final day. Pass
`--bhavcopy` with several files, or a folder of prior days' bhavcopies, and
the scan builds the window from them; with fewer sessions supplied it still
runs on what it has but says so in the output instead of pretending the
statistics are complete. A bhavcopy dated on an NSE holiday, a 404, or an
empty file is an error with a plain message (exit 2), never a silent zero.

## The opening-range breakout: Upstox 1m resampled to 5m

`orb` pulls the day's 1-minute candles from Upstox's v3 intraday endpoint for
each symbol's `instrument_key`, resamples them to the session-aware 5-minute
grid with `trader_core.upstox.resample`, and takes the 15-minute opening
range 09:15 to 09:30 IST:

- The trigger is the first completed 5-minute bar after the range whose close
  is above the range high. The signal time is when that bar closes (09:15 +
  5m per bar index): the classic RELIANCE case triggers at 10:00 IST.
- Before the range completes and its first successor closes, the answer is
  "waiting", not "no signal".
- One signal per symbol per day: the first close above is the signal, later
  bars do not re-fire.
- With a cutoff (`--at`, or the current clock) only bars complete at the
  cutoff count; nothing after the cutoff is ever reported. At the session end
  with no trigger the answer is "expired", and that is the day's final word.

## Offline mode and tests

`--offline PATH` points at recorded files, so the whole skill runs with no
network: bhavcopy CSVs for `universe` and `daily`, an instrument-keyed
candles fixture for `orb` (see `tests/fixtures/trader-scanner/india/`).
Offline ORB data still flows through the real Upstox client code, so parsing
and session filtering are the live paths. The clock is pinned with `--now`
(IST) or `--at` (HH:MM IST); a fixture replay without either uses the latest
date inside the fixture and replays the full session.

## Where it sits in the chain

1. `scan.py universe` once (refresh when NSE rebalances or the master drifts).
2. `scan.py daily` after a close, or `scan.py orb` from 09:35 IST onward.
3. `/trader-news SYMBOL`, then `/trader-entry SYMBOL` turns a watchlist hit
   into a plan, and only `/trader-broker` after your explicit yes places
   anything.

Data sources: NSE archives (EQUITY_L, bhavcopy, holidays) and Upstox market
data (instrument master, intraday candles). Times are IST everywhere; prices
and turnover are INR. If a download fails, the message says which host and
file, and the exit code is 2.
