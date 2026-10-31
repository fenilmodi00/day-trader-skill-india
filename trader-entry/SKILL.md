---
name: trader-entry
description: >-
  Entry Timer for the NSE: watches a setup bar by bar on Upstox 5-minute bars
  (resampled from 1-minute candles) and says the moment to get in. The
  opening-range breakout is the default: 15-minute range 09:15 to 09:30 IST,
  trigger on the first completed 5m bar that closes above the range high, one
  trigger per symbol per IST day, long side only, entry window 09:30 to 15:00
  IST. Also runs a level setup (first close above a price) and a volume
  filter. Writes trader/entry.json for trader-exit. All times IST, all money
  INR, data from Upstox. Use when the user says "watch RELIANCE for a
  breakout", "has it triggered", "opening range breakout", "did the level
  break", "is it time to buy", or any time a scanner hit should be turned
  into a timed entry.
argument-hint: "[check | watch | watchlist] SYMBOL [--at HH:MM]"
---

# trader-entry

The Entry Timer is the third step of a day trade on the NSE: the scanner
found a chart, this skill watches it and says the moment. That is all it
does. A trigger is a chart event, not a prediction, and most people who day
trade lose money; the timer does not change that.

This is not financial advice. The timer never places an order and never
will: nothing goes to the broker without you saying yes, out loud, to
trader-broker. `entry.py` only prints the status and writes
`trader/entry.json`.

Everything runs on the sandbox by default (`mode: "sandbox"` in
`trader/config.json`). Reading market data needs Upstox credentials; going
live is trader-broker's job and needs your explicit confirmation there.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

## Quick start

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --key "NSE_EQ|INE002A01018"
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --master tests/fixtures/upstox/instruments-NSE.json.gz --json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --now "2026-10-26 10:00" \
  --offline tests/fixtures/upstox/RELIANCE-1m-2026-10-26.json --write
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --setup level --level 1192.00
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --min-volume-x 1.5 --write
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --every 30 --until 15:00
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist --master tests/fixtures/upstox/instruments-NSE.json.gz
```

Flags: `--setup orb|level` (default orb), `--or-minutes 15`, `--level PRICE`
(level setup), `--min-volume-x X`, `--key`, `--master FILE`, `--universe
FILE`, `--now "2026-10-26 10:00"` (IST) to pin the clock, `--date
YYYY-MM-DD`, `--offline PATH` to read a recorded Upstox fixture instead of
the network, `--every SECONDS` (watch, default 30), `--until HH:MM` (watch),
`--write [PATH]` (default `trader/entry.json`), `--json`, `--config`.
Exit codes: `check`/`watch` 0 = triggered, 1 = anything else; `watchlist` 0
when at least one symbol triggered; 2 when it could not run (the reason is
one plain line on stderr).

## The rule

The NSE session opens 09:15 IST. `orb` builds the opening range from the
first `--or-minutes` (default 15) of the session on the 5-minute grid: the
09:15, 09:20 and 09:25 bars. The bars are Upstox 1-minute candles resampled
to the session-aware 5-minute grid with `trader_core.upstox.resample`
(Upstox keeps no 5-minute history, so nothing ever requests it), and
pre-open rows are ignored.

- The trigger is the first COMPLETED 5-minute bar after the range whose
  close is above the range high. A wick above the high is not a trigger.
- The signal time is when that bar closes: the classic RELIANCE case (range
  high 1192.00) triggers when the 09:55 bar closes at 10:00 IST, entry
  1193.60 INR.
- One trigger per symbol per IST day: the first close above is the signal,
  later bars do not re-fire.
- The entry window is 09:30 to 15:00 IST (`session.entry_start` and
  `session.entry_cutoff` in `trader/config.json`). A bar closing outside the
  window never triggers; after 15:00 with no trigger the answer is
  "expired", and that is the day's final word.
- Statuses: `waiting` (before the open, the range still forming, or no close
  above the level yet), `triggered`, `expired`, `no-data` (NSE closed today:
  weekend or NSE holiday, or Upstox sent no bars). Long side only.

`level` is the same clock without a range: the first completed bar that
closes above `--level` (for example a 20-day high from trader-scanner). A
level setup has no opening-range default stop downstream: give trader-exit a
stop.

`--min-volume-x X` asks the trigger bar for at least X times the average
volume of the opening-range bars; a close above on thin volume is reported
as waiting with the reason, not as a trigger.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | read by `watchlist`: the scanner's `symbols[]`, each with `symbol`, `instrument_key`, `level` |
| `trader/universe.txt` | fallback instrument keys, one `SYMBOL\|ISIN\|instrument_key` a line (from `scan.py universe`) |
| `trader/entry.json` | written by `--write`/`watch` on any status: `symbol`, `instrument_key`, `date`, `time` and `checked_at` in IST, `price`, `opening_range` (high, low, mid), `status`, `reason`, `tick_size` and `lot_size` when the instrument row is known |
| `trader/config.json` | optional; the session block sets the entry window, sandbox is the default when it is absent |

## Offline mode and tests

`--offline PATH` points at a recorded Upstox candles envelope (one
`{"status": "success", "data": {"candles": [...]}}` doc) or a JSON doc keyed
by `instrument_key`, or a folder of such files (see
`tests/fixtures/upstox/`). Offline data flows through the same
`intraday()` seam and the same resample as the live path. The clock is
pinned with `--now` (IST); a fixture replay without `--now` uses the latest
date inside the fixture at 10:00 IST.

## Where it sits in the chain

1. `scan.py orb` from 09:35 IST writes the watchlist.
2. `entry.py watchlist` checks every hit, then `entry.py watch SYMBOL`
   follows one chart bar by bar and writes `trader/entry.json` on the
   trigger.
3. `/trader-exit` turns the trigger into a stop and target (tick-rounded,
   flat by 15:10 IST) BEFORE you buy, and only `/trader-broker` after your
   explicit yes places anything.

Data source: Upstox intraday candles (1m). Times are IST everywhere; prices
are INR. Nothing here places an order.
