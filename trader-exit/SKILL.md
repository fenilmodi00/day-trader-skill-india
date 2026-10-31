---
name: trader-exit
description: >-
  Exit Planner for the NSE: sets the stop and the target BEFORE the entry, so
  you know when to get out. Stops: opening-range midpoint, opening-range low,
  price, percent, or ATR on the session's 5-minute Upstox bars; targets: 1R,
  2R or a price. Every price snaps to the instrument's tick_size, the stop
  rounding away from the entry (never widening risk), with a one-tick minimum
  risk. The plan carries the Upstox instrument_key, tick and lot sizes, an
  optional whole-lot quantity sized from --budget, and flat-by 15:10 IST: no
  overnight hold. Reads trader/entry.json, writes trader/exit.json. All times
  IST, all money INR. Use when the user says "set my stop", "what is my
  target", "plan the exit", "where do I get out", or any time an entry is
  about to be taken and the exit must exist first.
argument-hint: "[plan | show] [--stop or-mid|or-low|pct:X|atr:K] [--target 2R]"
---

# trader-exit

The Exit Planner is the gate before an entry: it writes down the stop and
the target before you buy, so the way out exists before the way in. A stop
written after a fill is a hope, not a plan.

This is not financial advice. The planner never places an order and never
will: nothing goes to the broker without you saying yes, out loud, to
trader-broker. `exit.py` only prints the plan and writes `trader/exit.json`.

Everything runs on the sandbox by default (`mode: "sandbox"` in
`trader/config.json`). Only the optional ATR stop reads market data (Upstox
intraday bars); the math itself is offline.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

## Quick start

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --from trader/entry.json --write trader/exit.json
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --json
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --key "NSE_EQ|INE002A01018" \
  --entry 1193.60 --or-high 1192.00 --or-low 1175.30 --master tests/fixtures/upstox/instruments-NSE.json.gz
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5 --offline tests/fixtures/upstox/RELIANCE-1m-2026-10-26.json
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol TCS --entry 3120.00 --setup level --stop pct:1
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --budget 50000 --master tests/fixtures/upstox/instruments-NSE.json.gz
python3 "${CLAUDE_SKILL_DIR}/exit.py" show
```

Flags: `--from PATH` (default `trader/entry.json`), `--symbol`, `--key`,
`--entry PRICE`, `--or-high`, `--or-low`, `--or-minutes 15`, `--setup
orb|level`, `--date`, `--time`, `--entry-bar`, `--stop`, `--target 2R`,
`--flat-by HH:MM`, `--master FILE` (Upstox instrument master, .json or
.json.gz: supplies `tick_size` and `lot_size`), `--tick`/`--lot` by hand,
`--budget INR` (whole-lot sizing), `--offline PATH` (a recorded Upstox 1m
fixture for the ATR stop), `--now "2026-10-26 10:05"` (IST), `--write
[PATH]` (default `trader/exit.json`), `--json`, `--config`. Exit codes:
`plan` 0 = a plan, 1 = refused or an error; `show` 0 = a plan exists.

## Stops and targets

Stops (a long's stop sits under the entry and rounds DOWN to the tick,
away from the entry, never widening the risk):

- `or-mid`: the opening-range midpoint, the default for an orb entry. The
  RELIANCE case: (1192.00 + 1175.30) / 2 = 1183.65, already on the 0.05
  tick grid, risk 9.95 a share.
- `or-low`: the opening-range low.
- `price:X`: your price.
- `pct:X`: X percent under the entry.
- `atr:K`: K times the 14-bar ATR under the entry, on the session's 5m bars
  up to the entry bar (`--offline FILE`, Upstox 1m resampled). With fewer
  than 14 bars it averages all of them and says so. A level setup has no
  opening-range default: give a stop.

Targets (rounded UP to the tick, away from the entry, so rounding never
pays less than planned): `2R` (default), `1R`, `1.5R ...` = entry + R x
risk, or `price:X`. The rounding mode is away from the entry on both legs:
the stop is never closer to the entry and the target never closer than the
plan. A risk smaller than one tick is widened back to one tick (the
smallest risk the exchange can price); a tick-1 or tick-10 instrument
rounds on its own grid the same way.

## The plan

`plan` writes `trader/exit.json`:

```json
{"symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018", "date": "2026-10-26",
 "direction": "long", "entry": 1193.6, "stop": 1183.65, "target": 1203.55,
 "tick_size": 0.05, "lot_size": 1, "quantity": 25, "risk_per_share": 9.95,
 "risk_inr": 248.75, "reward_risk": 1.0, "product": "I", "flat_by": "15:10",
 "legs": [], "source": "trader-exit", "generated_at": "2026-10-26 10:00:05 IST"}
```

- `instrument_key`, `tick_size` and `lot_size` come from `--master` (or the
  entry.json written by trader-entry); without an instrument row the prices
  stay unrounded and `tick_size`/`lot_size` are left out, never guessed.
- `--budget INR` sizes `quantity` to whole lots the same way trader-budget
  sizes: floor(cap / (risk x lot)) x lot, where the cap is the budget or
  the account's `max_loss_per_trade_inr`, whichever is smaller. The
  RELIANCE case: 250.00 max loss / 9.95 risk = 25 shares. Sizing needs the
  lot size and the risk; when the inputs are absent `quantity` is left out.
- `legs` are the three Upstox v3 order payloads (entry LIMIT, stop SL-M,
  target LIMIT) built with `trader_core.orders.leg`/`plan` once a quantity
  exists; trader-broker reads them after your explicit yes.
- `flat_by` is 15:10 IST from `session.flat_by` in `trader/config.json`:
  flat by then, win or lose, no overnight hold. An entry at or after
  flat-by is refused.
- Refused, with the reason: a stop at or above the entry, a target at or
  below it, a risk under the one-tick floor with no room to widen, an
  entry.json that has not triggered (unless you give `--entry`), a short
  (long only), a non-trading day.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry (symbol, instrument_key, price, opening_range, time) |
| `trader/exit.json` | the plan above; trader-budget sizes it further, trader-risk vets it, trader-timemachine replays it, trader-broker places it after your yes |
| `trader/config.json` | optional; `session.flat_by` and `account.max_loss_per_trade_inr` matter here, sandbox is the default when it is absent |

## Where it sits in the chain

1. `/trader-entry` triggers and writes `trader/entry.json`.
2. `exit.py plan --from trader/entry.json --write trader/exit.json` sets the
   exit BEFORE the buy.
3. `/trader-timemachine` replays it, `/trader-budget` sizes it, /trader-risk
   vets it, and only `/trader-broker` after your explicit yes places it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Times are IST everywhere; prices are INR. Nothing here places an order.
