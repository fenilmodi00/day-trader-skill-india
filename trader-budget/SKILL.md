---
name: trader-budget
description: >-
  Budget Manager for Indian markets. Sizes every trade to your INR budget and
  your max loss per trade. Reads the trade plan from trader-exit (entry, stop,
  target), loads the instrument row from a recorded Upstox master slice, snaps
  every price onto the tick grid (0.05, 1 or 10) without ever widening the
  risk, rounds the quantity down to whole exchange lots (equity lot 1, NIFTY
  and BANKNIFTY lots from the Upstox master), and prints ONE two-leg MIS plan
  (entry, stop and target legs in the Upstox v3 shape) for trader-risk to
  check and trader-broker to place. A stop too far away for your max loss is
  a plain refusal, not a smaller order. Nothing is placed. All amounts are
  INR, all times are IST, the market is NSE and the broker is Upstox. Use
  when the user says "how many shares", "size this trade", "how much should
  I buy", "I have Rs 50,000", "I only want to risk Rs 250 a trade", "set my
  rules", or after /trader-exit makes a plan.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE at 1193.60, stop 1183.65")
into an exact order ("buy 25"), inside rules the user sets once. The stop
decides the size: the further away the stop, the fewer shares or lots.
It prints a proposal only. trader-risk checks it and trader-broker asks the
user yes or no. Day trading loses most people money. Sizing to a max loss
keeps each loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --config trader-budget/config.example.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --config trader-budget/config.example.json --json
python3 "${CLAUDE_SKILL_DIR}/size.py" --config trader-budget/config.example.json --budget 200000 --max-loss 800
python3 "${CLAUDE_SKILL_DIR}/size.py" --config trader-budget/config.example.json --instruments instruments.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --config trader-budget/config.example.json --now "2026-10-26 10:00"
python3 "${CLAUDE_SKILL_DIR}/size.py" --config trader-budget/config.example.json --write
```

`--now "2026-10-26 10:00"` pins the clock, IST. A plan dated for another day
is refused: make a new one with trader-exit. Exit codes: 0 sized, 1 refused
(no order), 2 bad input.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade: entry, stop, target, setup, date. trader-exit prints it and saves it only with `--write`, to this same default path |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `--instruments` file | a recorded Upstox instrument master slice: lot, tick, freeze |
| stdout | the sized plan: the report, or the full JSON with `--json` |

It writes no file unless you pass `--write`: then it saves the sized plan,
the exact JSON trader-risk checks and trader-broker turns into orders, to
`trader/plan.json` (or the PATH you give). Without `--write` the sized plan
goes to stdout only. Nothing is placed either way.

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` from this
folder and ask the user for each value in plain words:

- **account.budget_inr**: how much money this is for (example: 50000). Only
  money they can afford to lose.
- **account.max_loss_per_trade_inr**: the most one trade may lose at the
  stop, before charges (example: 250).
- The day limits trader-risk enforces: **account.max_daily_loss_inr** (500),
  **account.max_trades_per_day** (3), **account.max_orders_per_day** (12),
  **account.min_reward_risk** (1.0) and **account.long_only** (true).
- **session**, all IST: open 09:15, entries 09:30 to 15:00, flat by 15:10,
  close 15:30, MIS square-off 15:25 (15:10 on CAS accounts).
- **charges**: brokerage (5 bps or Rs 20 an order), STT, exchange, SEBI,
  stamp duty and 18% GST; trader-risk measures the loss net of these.
- **risk.ban_symbols / asm_symbols / gsm_symbols**: the user's own lists to
  be warned about; nothing here watches an exchange feed for them.
- Keep **mode** "sandbox" until they have watched it work. Going live needs
  the typed phrase GO LIVE WITH UPSTOX and a stored confirmation.

## Step 2: size the trade

Give the instrument row: `--instruments` with a recorded Upstox master slice
(the same master the scanner and entry skills use). The row decides the lot
size (equity lot is 1, NIFTY 75, every F&O symbol from the master), the tick
grid and the freeze quantity.

Whole exchange lots only. The size is the smallest of three limits:

| limit | lots |
| --- | --- |
| max loss | `max_loss_per_trade_inr` / (risk a unit x lot size), floored |
| budget | `budget_inr` / (entry x lot size), floored |
| freeze | the instrument's freeze quantity / lot size, floored |

The plan is long only ("direction": "long"): this pack does not size shorts.

Before the math, every price is snapped onto the tick grid in the direction
that never widens the risk: the stop moves down (never closer to the entry),
the target moves up (never closer), the entry itself never up. Minimum risk
is one tick. A 1193.62 entry with a 1183.67 stop on the 0.05 grid becomes
1193.60 and 1183.65: the risk stays 9.95 a unit and never grows.

It refuses, and prints no order, when: one lot costs more than the budget,
one lot risks more than the max loss at the stop, the stop is not below the
entry after rounding, the target is not above it, the plan is for another
day, or the plan is a short. Say the reason to the user in one line.

## Step 3: show the user

Show the three limits, which one decided the size (`bound_by` in the JSON),
the quantity in units and lots, the cost in INR and the max loss at the
stop. Say plainly that a stop is an order, not a promise: in a fast market
it can fill below the stop, and charges make the real loss a little bigger.
Save it with `--write` so trader-risk checks exactly the plan trader-broker
will place, then hand over to `/trader-risk`.

## Example: the reel

NIFTY on Monday 2026-10-26, sized at 10:00 IST: entry 25150.0, stop 25100.0
(the opening-range midpoint), target 25250.0 (1R). Risk 50.0 a unit, so
3750 a lot. Budget Rs 20,00,000, max loss Rs 8,000. Max loss would allow two
lots; the budget allows one, so the budget decides:

```
NIFTY long, entry 25150, stop 25100, target 25250 (tick 0.05)
Size: 75 units of NIFTY = 1 lots (lot size 75). Cost Rs 1,886,250.
Risk at the stop: Rs 3,750 of your Rs 8,000 max loss. Reward:risk 2.0. Bound by the budget.
Flat by 15:10 IST, before the MIS square-off. Product I (intraday).

Next: trader-risk checks this plan, then trader-broker asks you yes or no.
Education and tooling, not financial advice.
```

The same plan with a Rs 10,00,000 budget refuses, because one lot costs more
than the budget:

```
No order: one lot of NIFTY (75 units) costs Rs 1,886,250 at the entry, more than your Rs 1,000,000 budget. No order. Raise the budget, or pick a cheaper instrument.
```

For an equity plan (RELIANCE, lot 1): entry 1193.60, stop 1183.65, target
1203.55, 25 shares. Risk 9.95 a share, Rs 248.75 gross at the stop; with the
example charges the round trip adds Rs 45.53 of STT, exchange, SEBI, stamp
duty, GST and brokerage, so trader-risk judges the Rs 294.28 net loss
against the cap. Gross loss never rounds up: the sizing floors the lots.

## Rules

- Proposals only. This skill never calls a broker tool and never places,
  approves or schedules an order.
- Never size money the user has not said is for this. Ask for the budget
  and the max loss in INR.
- Every number comes from the config, the plan or the instrument row.
  Nothing is guessed; nothing is rounded in the user's favor.
- No projections: never say what a trade will make. A target is a plan,
  not a forecast.
- Upstox quotes can lag; the broker's price is the one that fills.

## What it never does

It never places an order, never raises a limit on its own, never shrinks a
stop to fit more lots, never puts a price off the tick grid, and never sizes
options, margin or short positions.

## Safety

- Education and tooling, not financial advice.
- Sandbox by default (`"mode": "sandbox"`). Going live is the user's own
  decision: mode "live", the typed phrase GO LIVE WITH UPSTOX and a stored
  confirmation, or the tools refuse.
- Nothing in this pack places an order except trader-broker, and only when
  all three hold: trader-risk passed that exact plan, the user typed an
  explicit yes for it in the chat, and the order tool is set to "Needs
  approval" (Customize > Connectors > tool permissions). Never set an order
  tool to "Always allow".
- No options, margin or leverage unless the config allows it and the user
  confirms.
- Upstox tokens live in environment variables (UPSTOX_ACCESS_TOKEN and
  friends) or the local login state, never in the chat or the config file.
