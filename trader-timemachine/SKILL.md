---
name: trader-timemachine
description: >-
  Time Machine for the NSE. Replays every possible outcome of a trade before
  you risk a rupee: 10,000 simulated sessions plus every past time the setup
  fired in the window. Takes the plan from trader-exit (entry, stop, target),
  pulls Upstox 1-minute candles for the last month, resamples them to the
  75-bar 5-minute session grid (09:15 to 15:25 IST), rebuilds the rest of the
  day 10,000 times from the stock's own bars, and re-runs the same rule on
  every past session in the window. Prints the odds of target first, stop
  first, or neither by the close, where the price sits at the close, the
  average R, the history, the window dates and session count, and one honest
  verdict that names itself a simulation, not a prediction, then hands off to
  /trader-budget. Use when the user says "run the time machine", "replay this
  trade", "what are the odds", "simulate this trade", "monte carlo",
  "backtest this setup", "how often does this hit the target", "is this a
  coin flip", or after /trader-exit writes a plan, before /trader-budget.
  All amounts are INR, all times are IST, the market is NSE and the broker is
  Upstox.
argument-hint: "[path to plan.json, or TICKER]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if the next part of the session plays out like the
stock's own recent sessions, how often does this plan hit the target first,
the stop first, or neither? It answers with 10,000 replays and with the
real record of the same rule on every past session in the window. It is a
simulation of the recent window, not a prediction. Day trading loses most
people money, and a good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:10      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-26 \
    --entry 1193.60 --stop 1183.65 --target 1203.55 --entry-time "10:00 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a recorded Upstox 1-minute candle payload, `.json`
or `.json.gz`, no network), `--now "2026-10-26 10:15"` (IST) to pin the
clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, instrument_key, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range |
| Upstox candles API | one request: one month of 1-minute candles for the instrument key (`interval=1m`) |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history, sessions_used, window_start, window_end, caveat}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |

## The data: 1-minute in, 5-minute session grid out

Upstox keeps one month of 1-minute candle history and no 5-minute history at
all. The tool therefore always asks for `interval=1m` and resamples locally
with `trader_core.upstox.resample(bars, 5)`. It never requests a 5-minute
series (Upstox refuses that anyway) and never substitutes 30-minute candles:
a 30-minute payload is rejected in plain words, not silently resampled into
a thinner fake. Pre-open rows and after-close rows are dropped before
resampling; a bucket is kept only when all five of its 1-minute bars are
present.

One full session on the 5-minute grid is exactly 75 bars stamped 09:15
through 15:25 IST (the last bucket starts at 15:25 and runs to the 15:30
close).

## The window: 15 to 20 sessions, and why

One month of Upstox 1-minute history holds at most about 20 NSE trading
sessions, so that is the whole window the data can support, and the tool is
honest about it:

- Sessions strictly before the plan's date are kept; today's half-built
  session never joins the pool.
- A session joins the pool only when it is complete: all 75 bars. Sessions
  with missing bars, gaps or only a few rows are dropped and counted in
  `dropped_sessions`, never padded.
- Fewer than 15 complete sessions: the run refuses, exit code 2, with one
  plain line that names how many complete sessions were found.
- 15 to 19 complete sessions: the run proceeds and prints a caveat that
  names the count; a thinner pool swings the odds wider.
- 20 complete sessions: the full window, no caveat.

The result names the window in `window_start` and `window_end` (IST dates)
and `sessions_used`, so nobody mistakes one month of data for a long
history.

## How it works

1. **The pool.** It keeps only full sessions before the plan's date: all 75
   five-minute bars, 09:15 to 15:25 IST. Half-built sessions, gaps and today
   are left out. Every bar becomes four ratios to the bar before it: open,
   high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default 15:30, the
   close). Green, which means target first or above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions. That is
   what really happened in the window, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from
   break-even: "Close to a coin flip" at 1R, "Close to break-even" at any
   other R. 5 to 15 points: "Leans toward the target" or "Leans toward the
   stop". 15 or more: "Strongly toward" one of them. So a 2R plan with
   target first 24.1 % and stop first 50.5 % is close to break-even (it
   needs 33 % of the 74.6 % that hit a side, and got 32 %), not a loser.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**Refusals, in one plain line each.** Fewer than 15 complete sessions, the
entry bar not in the data (wrong date, not closed yet, a future day), a
plan that does not make sense (a stop above a buy's entry, an entry off the
5-minute grid), or a plan whose interval is not 5m. A live in-progress bar
is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If
   there is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. Add `--until 15:10` when the plan has a
   flat-by time, so "neither" means still open at the time you would get
   out.
3. Tell the user, in this order: the four odds, the window (dates and
   session count, with the caveat when there are fewer than 20 sessions),
   the median and the 5 to 95 range of the price at the close, the average
   R, the history line, then the verdict line and any line under it exactly
   as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is a simulation of the recent window and not a
   prediction: one month of sessions is all the data there is, and
   earnings, news and halts can do things those sessions never did. Check
   `/trader-news` before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size
   it.

## Example: RELIANCE, Monday 2026-10-26

The 15-minute opening range is 1175.30 to 1192.00, mid 1183.65. The 09:55
bar closes above the range: entry 1193.60 at 10:00 AM (bar 8), stop 1183.65
(the range midpoint, rounded down), target 1203.55 (1R, risk 9.95 a
share), all INR, all IST.

```
RELIANCE buy, Monday 2026-10-26: entry 1193.60 at 10:00 AM (bar 8), stop 1183.65, target 1203.55 (1R, risk 9.95 a share).
10,000 replays of the rest of the session, 10:00 AM to 3:30 PM (65 five-minute bars), seed 19.
Each bar is drawn whole from RELIANCE's own 5-minute bars (Upstox 1-minute candles resampled) at the same time of day (+-2 bars) over the window's 19 complete sessions (2026-09-28 to 2026-10-23).
Window: 2026-09-28 to 2026-10-23 IST, 19 complete sessions used (20 found, 1 dropped as incomplete).

  target first   31.2 %
  stop first     30.4 %
  neither        38.4 %   still open at 3:30 PM
  green          55.1 %   target first, or above the entry at 3:30 PM

  Price at 3:30 PM: median 1194.10, 90 % of replays between 1180.20 and 1207.90.
  Average R: +0.041

Caveat: 19 complete sessions instead of the full 20 (one month of Upstox 1-minute candles holds about 20 sessions): a thinner pool swings the odds wider.
History: the same rule (15-min opening range, stop at the opening-range midpoint, 1R target) on each of those 19 real sessions fired 11 times: 5 target, 5 stop, 1 neither. Average R +0.018.

Leans toward the target: target first 31.2 % vs stop first 30.4 %.
A simulation of the recent window: RELIANCE's own last 19 complete sessions (2026-09-28 to 2026-10-23, IST). It is not a prediction: earnings, news and halts can do things those sessions never did. Same seed, same numbers. Repeat this run with --seed 19.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 50.7 %. The replays lean barely off a coin flip, and the
window is one session short of the full month, so the caveat is on the
record too. That is the honest answer for this setup, and it is what the
user should hear before `/trader-budget` sizes the position.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. They are one month
  of recent sessions replayed. Say it once, plainly.
- Say the session count with every result. When it is below 20, say the
  caveat out loud.
- Upstox 1-minute candles only, resampled here. Never ask Upstox for a
  5-minute series and never use 30-minute candles as a stand-in.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, never mixes today's bars into
the pool, and never pads a session that is missing bars.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
