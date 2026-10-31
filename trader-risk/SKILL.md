---
name: trader-risk
description: >-
  Risk Manager for Indian markets. Checks every sized plan against your
  limits and stops anything over them, before it can reach the broker, all
  on the IST clock via trader_core. The loss at the stop, measured net of
  charges (STT, exchange, SEBI, stamp duty, GST, your brokerage), must fit
  your max loss per trade in INR, reward:risk must meet your minimum, the
  day's realized loss must fit your daily cap, no more than your trades per
  day, and no new entries outside the entry window (09:30 to 15:00 IST by
  default; flat by 15:10, MIS square-off 15:25, or 15:10 on CAS accounts).
  It also keeps the basics: the two-leg plan validates as a set, quantity on
  whole exchange lots, prices on the tick grid (0.05, 1 or 10), the freeze
  quantity, MIS eligibility (product I, NSE_MIS), available margin and
  required margin through the Upstox broker seam, duplicate tags, long-only
  and your own ban/ASM/GSM lists as warnings. Weekends and NSE holidays are
  closed. A BLOCK is a verdict, not a crash: it prints PASS or BLOCK with
  the reasons and records the verdict so trader-broker only shows a plan
  that passed. Use when the user says "check my order", "is this safe",
  "risk check", "does this break my rules", "can I take this trade", "how
  much have I risked today", or before /trader-broker, always.
argument-hint: "[path to plan.json]"
---

# trader-risk

The Risk Manager is the gate between a sized plan and the broker. Every plan
written by trader-budget passes through it before trader-broker may show it.
A BLOCK is final for that plan: it does not go to the user for a yes, and it
does not go to the broker. Most people who day trade lose money. These
limits keep each loss and each day's losses small. They do not make a trade
win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py" --config trader-budget/config.example.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --config trader-budget/config.example.json --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --config trader-budget/config.example.json --no-write
python3 "${CLAUDE_SKILL_DIR}/check.py" --config trader-budget/config.example.json --now "2026-10-26 10:00"
```

Exit codes: 0 PASS, 1 BLOCK (a verdict, not an error), 2 bad input. With
`--json` the verdict prints to stdout and is recorded to
`trader/state/risk.json` unless `--no-write`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the sized two-leg plan (trader-budget prints it; save it here) |
| `trader/config.json` | the limits (trader-budget ships `config.example.json`) |
| `--instruments` file | a recorded Upstox master slice, for lot, tick and freeze |
| `trader/state/orders.json` | what was approved or placed today: trades per day, daily loss, duplicate tags |
| `trader/state/risk.json` | the verdict it writes, with the plan attached |

## The IST clock

All times are IST, fixed at UTC+05:30 all year: the clock never shifts. It
lives in trader_core (`timeutil.now_ist`, `parse_ist`), not in the local
machine's zone, and `--now "2026-10-26 10:00"` pins it for tests and replays.
The session opens 09:15 and closes 15:30. New entries run from
`session.entry_start` (09:30) to `session.entry_cutoff` (15:00), the plan
must be flat by `session.flat_by` (15:10), and the broker auto-squares-off
MIS positions at `session.mis_square_off` (15:25), or 15:10 when the user's
account is on the CAS timetable (`session.mis_square_off_cas`). Weekends and
the NSE holidays in `trader_core/data/nse_holidays.json` are closed days;
the calendar module decides, not a hardcoded list.

## The checks

Every check reads the plan, the config and the recorded state. The plan is
judged as a set first: entry, stop and target legs with the right sides,
product and quantities.

| check | blocks when |
| --- | --- |
| plan | the two legs do not validate as a set: a missing leg, a wrong side, a stop at or above the entry, a target at or below it, a quantity off-lot, a price off-grid, or a product the config does not allow |
| max loss | quantity x (entry minus stop) is over `account.max_loss_per_trade_inr` |
| daily loss | the day's realized P&L in `trader/state/orders.json` is at or past `account.max_daily_loss_inr` |
| trades | `account.max_trades_per_day` entries are already approved or placed today |
| orders | `account.max_orders_per_day` orders are already approved or placed today (an entry and its flatten both count) |
| reward:risk | (target minus entry) / (entry minus stop) is under `account.min_reward_risk` |
| window | now is before `entry_start`, at or after `entry_cutoff`, or the NSE is closed (weekend or holiday) |
| freeze | the quantity is above the instrument's freeze quantity (NIFTY 1800, from the master) |
| lot size | the quantity is not a whole multiple of the lot size (equity lot 1, F&O lots from the master) |
| tick | any leg price or trigger is off the tick grid: 0.05 equity, 1 F&O price bands, 10 where the master says 10 |
| mis | the plan's product is not the intraday MIS product I (`risk.mis_products`, default NSE_MIS); D and MTF are blocked here |
| funds | the broker seam's available margin is missing or under the entry cost |
| margins | the broker seam errors, or required margin is far above the entry cost (a warning) |
| duplicate | a leg tag was already approved or placed today |
| charges | the loss at the stop net of STT, exchange, SEBI, stamp duty, GST and brokerage is over the cap, even when the gross loss fits |
| long only | the plan direction is short while `account.long_only` is true (the default) |
| blocked | the symbol is on your `risk.blocked_symbols` list; the ASM, GSM and ban lists only warn |

The charges check runs through `trader_core.charges.round_trip`: on the
example RELIANCE plan the gross loss at the stop is Rs 248.75, charges add
Rs 45.53, so a Rs 250 cap blocks it and a Rs 300 cap passes. Set the
brokerage in `charges.brokerage_bps` or `charges.brokerage_per_order_inr`.

Funds and margins go through the injected broker seam (an UpstoxBroker in
the CLI path). A missing or failed answer is a reason, never a silent pass;
without a seam the check says "not checked here" and the broker skill
confirms funds at placement time.

Warnings never block: a symbol on the user's own `risk.ban_symbols`,
`risk.asm_symbols` or `risk.gsm_symbols` list in the config (no live
surveillance feed is checked, the user maintains the lists), or margin
required well above the entry cost.

## Steps

1. Size the plan first if it is not sized yet (trader-budget), then run
   `check.py`. Exit code 0 means PASS, 1 means BLOCK.
2. Tell the user the result in plain words: the max loss at the stop net of
   charges, reward:risk, and how much of today's limits it uses. For a
   BLOCK, give the reason and the fix (fewer lots, a closer stop from
   trader-exit, wait for the window, or skip the trade). Never change a
   limit yourself.
3. If the plan changes in any way, run the check again. trader-broker
   matches the verdict to the plan, so an edited plan is not ready until it
   passes again, and a verdict from yesterday does not count today.

## Example: the reel

The RELIANCE plan at 10:00 IST on Monday 2026-10-26: entry 1193.60, stop
1183.65, target 1203.55, 25 shares, with the max loss set to Rs 300:

```
PASS RELIANCE at 2026-10-26 10:00 IST
  plan         two-leg plan validates as a set
  max_loss     Rs 248.75 of Rs 300 max loss per trade
  daily_loss   Rs 0 realized today of Rs 500 cap
  trades_today 0 of 3
  reward_risk  1.0 (minimum 1.0)
  entry_window 09:30 to 15:00 IST, now 10:00
  freeze       no freeze data
  lot_size     quantity 25 on lot 1
  tick         prices on the 0.05 grid
  mis          product I (MIS intraday)
  funds        no broker seam: not checked here (the broker skill confirms funds)
  margins      no broker seam: not checked here
  duplicate    tag dt-entry-orb not recorded
  charges      net loss at the stop Rs 294.28 after Rs 45.53 charges (gross Rs 248.75)
  long_only    long only

Every check passed. trader-broker still asks you yes or no before anything is placed.
Education and tooling, not financial advice.
```

The same plan with the cap at Rs 250: the gross Rs 248.75 fits, but the net
Rs 294.28 after Rs 45.53 of charges does not, so it is blocked:

```
  - charges-adjusted loss Rs 294.28 (charges Rs 45.53) is over your Rs 250 max loss per trade; the gross loss Rs 248.75 fits, the net does not
```

At 09:29 the plan is blocked by the window ("entries run 09:30 to 15:00
IST, it is now 09:29"), at 15:01 by the cutoff, and a 1875-unit NIFTY plan
is blocked by the 1800 freeze quantity.

## Rules

- A PASS only means the plan is allowed to be shown to the user. It is not
  a yes.
- Every number comes from the config, the plan, the recorded state or the
  broker seam. Nothing is guessed.
- Getting out is never the risk check's business here: this skill judges
  the entry plan as a whole. The exit legs are part of the plan it checks.
- Upstox prices can lag; the broker's price is the one that fills, and a
  stop can fill worse than its trigger in a fast market.

## What it never does

It never places an order, never edits a plan to make it pass, never loosens
a limit, never treats a PASS as the user's yes, and never claims to watch a
live exchange surveillance feed.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check,
  not a recommendation.
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
