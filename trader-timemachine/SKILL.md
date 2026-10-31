---
name: trader-timemachine
description: >-
  Time Machine for the NSE. Replays a planned trade before you risk a
  rupee: 10,000 replays drawn from the stock's own last month of 5-minute
  bars, plus the same rule on each of those sessions. Takes the plan from
  trader-exit (entry, stop, target, flat-by time), pulls Upstox 1-minute
  candles for the last month, resamples them to the 75-bar 5-minute session
  grid (09:15 to 15:25 IST), rebuilds the rest of the day 10,000 times from
  the stock's own bars, and re-runs the same rule on every past session in
  the window. Prints the odds of target first, stop first, or neither by
  the flat-by time, each with a 95 % interval, where the price sits at that
  time, the average R, the history, the window dates and session count, one
  honest verdict that names itself a simulation, not a prediction, and what
  the model leaves out, then hands off to /trader-budget. Use when the user
  says "run the time machine", "replay this trade", "what are the odds",
  "simulate this trade", "monte carlo", "backtest this setup", "how often
  does this hit the target", "is this a coin flip", or after /trader-exit
  writes a plan, before /trader-budget. All amounts are INR, all times are
  IST, the market is NSE and the broker is Upstox.
argument-hint: "[path to plan.json, or TICKER]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop, the target and the flat-by time. Before anything is
sized or sent, this skill asks one question: if the rest of the session is
stitched from the stock's own recent sessions, how often does this plan hit
the target first, the stop first, or neither? It answers with 10,000
replays and with the record of the same rule on each of those past
sessions. It is a bootstrap built from the stock's own recent bars, not a
prediction, and the section "What it cannot tell you" below is part of the
answer. Day trading loses most people money, and a good-looking replay does
not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --csv              # export replay outcomes and history to CSV
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:30      # end at the close instead of the plan's flat_by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --fill next-open   # fill at the next bar's open, not the signal close
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-26 \
    --entry 1193.60 --stop 1183.65 --target 1203.55 --entry-time "10:00 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a recorded Upstox 1-minute candle payload, `.json`
or `.json.gz`, no network), `--now "2026-10-26 10:15"` (IST) to pin the
clock. `--csv [PATH]` writes one row per simulated replay and historical
trade to `trader/timemachine.csv` by default; set `--csv PATH` to choose
another file, and keep it different from `--write`'s.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, instrument_key, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by |
| Upstox candles API | one request: one month of 1-minute candles for the instrument key (`interval=1m`) |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history, sessions_used, window_start, window_end, caveat, caveats}` plus the verdict, its notes and the caveats. `mc` holds the odds with their intervals, the both-touched count, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |
| `trader/timemachine.csv` | with `--csv`: one row per Monte Carlo replay and one per historical trade; `close` is the replay/session final candle close, while `exit_price` is the target, stop or closing price used for the outcome |

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
   first, then the target. A bar that touches both counts as the stop, and
   the run says how many replays were decided that way.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until`. Green, which means
   target first or above the entry at `--until`. The default `--until` is
   the plan's flat-by time (trader-exit writes 15:10), so "neither" means
   still open when the plan says to be flat. A plan without one ends at the
   close (15:30 IST). A flat_by off the 5-minute grid or after the close is
   moved to the last bar before it and the run says so. Target first and
   stop first carry a Wilson 95 % interval: the noise of 10,000 draws,
   about plus or minus one point, and nothing about whether the model is
   right. R is +1 at a 1R target, -1 at the stop, and otherwise the
   distance from the entry at `--until` divided by the risk. A gap through
   the stop still counts as -1R here. A real fill can be worse.
4. **The fill.** By default each replay is filled at the signal bar's close,
   the plan's entry. `--fill next-open` fills at the open of the next drawn
   bar instead, and measures R from that fill. Neither model adds a spread,
   slippage or a commission. The run prints which one it used.
5. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on each of those past sessions. That is what
   happened on those days, not a simulation. Its decided win rate (target
   over target plus stop) carries its own 95 % interval, which is wide: a
   handful of decided trades say little.
6. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from
   break-even: "Close to a coin flip" at 1R, "Close to break-even" at any
   other R. 5 to 15 points: "Leans toward the target" or "Leans toward the
   stop". 15 or more: "Strongly toward" one of them. So a 2R plan with
   target first 24.1 % and stop first 50.5 % is close to break-even (it
   needs 33 % of the 74.6 % that hit a side, and got 32 %), not a loser.
7. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither. Then, always, two
   caveats: the pool and the history share the same sessions, and the fill
   model with its missing costs.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**Refusals, in one plain line each.** Fewer than 15 complete sessions, the
entry bar not in the data (wrong date, not closed yet, a future day), a
plan that does not make sense (a stop above a buy's entry, an entry off the
5-minute grid), a plan whose interval is not 5m, or when the real bars
after the entry are already in the data and already hit the stop or the
target (same stop-first tie rule as the sim): odds on a settled trade would
be hindsight, not a forecast. A live in-progress bar is never used. If
later real bars are in the data but did not resolve it yet, it still runs,
with a warning that those bars were not used.

## What it cannot tell you

- **Bars are drawn independently.** Each 5-minute bar is picked on its own
  from the same time of day. A quiet morning does not make a quiet
  afternoon, a wide bar does not make the next one wide, and nothing
  trends. There is no volatility clustering, no news, no halt, nothing the
  last month did not do.
- **Fills are at the close of the signal bar.** The plan's entry is a bar's
  close, and the replay starts there. A real order fills at the next bar's
  open or worse. `--fill next-open` shows that case, still with no spread,
  slippage or commission.
- **The history is the same sessions.** The pool is built from the sessions
  the history runs on, so when the two agree they are one piece of
  evidence, not two.
- **No costs.** No spread, no slippage, no commission, no borrow. Every R
  here is before all of those.
- **The intervals are sampling noise only.** The 95 % interval on target
  first and stop first says how much another 10,000 draws would move the
  number. It says nothing about whether drawing bars this way is a fair
  picture of today.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If
   there is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. The replays end at the plan's flat-by
   time on their own; pass `--until 15:30` only if the user wants the close.
3. Tell the user, in this order: the four odds with their intervals, the
   both-touched count, the window (dates and session count, with the
   caveat when there are fewer than 20 sessions), the median and the 5 to
   95 range of the price at the end time, the average R, the history line,
   then the verdict line and every line under it exactly as printed. Say
   the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once what the model leaves out: bars drawn one at a time from the
   last month, fills at the signal bar's close, no costs, and a history
   that is not independent of the pool. Earnings, news and halts can do
   things those sessions never did. Check `/trader-news` before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size
   it.

## Example: RELIANCE, Monday 2026-10-26

The 15-minute opening range is 1267.60 to 1268.25. The 09:55 bar closes
above it: entry 1268.30 at 10:00 AM (bar 8). The plan risked 1.40 a share
to 1266.90 and targeted 1269.20 (0.64R). `timemachine.py run` at 10:30 AM
IST, six closed bars later, on the recorded Upstox candles:

```
RELIANCE buy, Monday 2026-10-26: entry 1268.30 at 10:00 AM (bar 8), stop 1266.90, target 1269.20 (0.64R, risk 1.40 a share).
10,000 replays of the rest of the session, 10:00 AM to 3:30 PM, the close (66 five-minute bars), seed 19.
Each bar is drawn whole from RELIANCE's own 5-minute bars (Upstox 1-minute candles resampled) at the same time of day (+-2 bars) over the window's 15 complete sessions (2026-10-05 to 2026-10-23).
Window: 2026-10-05 to 2026-10-23 IST, 15 complete sessions used (15 found, 0 dropped as incomplete).
Fills at the signal bar's close. No spread, slippage or commission is included.

  target first   100.0 %  (95 % interval 100.0 to 100.0)
  stop first     0.0 %  (95 % interval 0.0 to 0.0)
  neither        0.0 %    still open at 3:30 PM
  green          100.0 %   target first, or above the entry at 3:30 PM
  both touched in one bar: 0 (0.0 %), counted as the stop

  Price at 3:30 PM: median 1271.72, 90 % of replays between 1271.71 and 1271.73.
  Average R: +0.643 (R = the 1.40 risk: +0.6429R at the target, -1R at the stop).

Caveat: 15 complete sessions instead of the full 20 (one month of Upstox 1-minute candles holds about 20 sessions): a thinner pool swings the odds wider.
History: the same rule (15-min opening range, stop at the 0.11 % from the entry, 0.64R target) on each of those 15 real sessions fired 15 times: 15 target, 0 stop, 0 neither. 15 of 15 decided, 100 % (95 % interval 80 to 100). Average R +0.640.

Strongly toward the target: at 0.64R the target has to come first in 61 % of the replays that hit one side; it did in 100 % (target first 100.0 % vs stop first 0.0 %).
Built from the same 15 sessions as the history: the two are not independent evidence.
Fills at the signal bar's close; a real fill is the next bar's open or worse. No costs included.

Note: 6 bar(s) after the entry are already in the data and were not used: the odds are from the entry bar only.
A simulation of the recent window: RELIANCE's own last 15 complete sessions (2026-10-05 to 2026-10-23, IST). It is not a prediction: earnings, news and halts can do things those sessions never did. Same seed, same numbers. Repeat this run with --seed 19.
```

Read the example with suspicion, the way the tool means it. The 95 %
interval on the history's 15-of-15 win rate is 80 to 100, so even a perfect
record on 15 decided trades leaves room for a coin. The replays look
unanimous because the fixture's sessions trend all afternoon; six bars were
already in the data and were not used, and the run says so. Had the day
gone the other way and the real bars hit the stop or the target before
10:30, the tool would have refused outright: odds on a settled trade are
hindsight, not a forecast. That is the honest answer for this setup, and it
is what the user should hear before `/trader-budget` sizes the position.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly,
  with what the model leaves out.
- Say the session count with every result. When it is below 20, say the
  caveat out loud.
- Upstox 1-minute candles only, resampled here. Never ask Upstox for a
  5-minute series and never use 30-minute candles as a stand-in.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target, date or fill model is a
  new run.

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
