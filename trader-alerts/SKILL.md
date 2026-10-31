---
name: trader-alerts
description: >-
  The Alerts skill for Indian markets. Watches NSE prices in INR on the IST
  clock: session bells at 09:00 (pre-open), 09:15 (open) and 15:30 (close),
  a flat-by reminder at 15:10, and price alerts for stops and targets using
  live Upstox quotes. Rings each alert once per session, remembers what it
  has already rung, stays quiet on NSE holidays and weekends, and always
  says why when a quote is missing. Use when the user says "alert me",
  "watch this price", "remind me to flat by close", "ring me at the open",
  or asks what alerts are armed. All amounts are INR, all times are IST,
  the market is NSE and the broker is Upstox.
---

# Trader Alerts

Price alerts and the session clock for the NSE, in IST and INR. It rings a
bell when the session opens, reminds you to go flat before the intraday
square-off window, and rings when a stop or target is touched, using live
Upstox quotes. Every alert rings once per session and then stays quiet; the
next session re-arms everything by itself.

The tool is `alerts.py` in this folder, standard library Python only.

## Safety first

No order goes out from here, ever. Alerts are only noise: a ring is a
nudge for you to look at the chart, not a signal to fire an order. Nothing
is bought, sold or scheduled by this skill; an alert fires and it is still
always you who decides, and an order only happens through the broker skill
after you say an explicit yes. This skill is not financial advice: it
watches prices and rings bells, the trading decisions are yours.

## The IST clock

The session is 09:15 to 15:30 IST, with the pre-open window from 09:00.
The bells:

- 09:00 pre-open bell, the session is 15 minutes away
- 09:15 open bell, the market is open
- 15:10 flat-by reminder, from the config `session.flat_by`
- 15:30 close bell

A bell rings at its time, or at the first check after its time. It never
rings early: checking at 09:14 does not ring the open bell. On weekends
and NSE holidays no bell and no flat-by fires, and the check says the
market is closed and why. The holiday list comes from
`trader_core/data/nse_holidays.json`.

## Quotes

Price alerts are evaluated on Upstox batch quotes (last traded price,
OHLC) through the shared `trader_core` client, sandbox or live depending
on your config. The rules file carries the Upstox instrument key, for
example `NSE_EQ|INE002A01018` for RELIANCE.

- A check with no price rules never calls the network.
- A missing quote is reported plainly, "no quote for RELIANCE", and the
  alert stays armed. It is never guessed from yesterday's close.
- An expired or rejected Upstox access token is reported with
  re-authenticate guidance and the check exits 2. Re-run the Upstox login
  flow, then check again.

Quotes are for alerting only. The last traded price can be a few seconds
old, so never time an order from an alert ring alone.

## Commands

```bash
python3 alerts.py add --symbol RELIANCE --instrument-key "NSE_EQ|INE002A01018" --above 1200
python3 alerts.py add --symbol RELIANCE --below 1170
python3 alerts.py add --symbol RELIANCE --stop 1183.65
python3 alerts.py add --symbol RELIANCE --target 1203.55
python3 alerts.py add --preopen-bell
python3 alerts.py add --opening-bell
python3 alerts.py add --close-bell
python3 alerts.py add --flat-by --at 15:10 --label "flat by close"
python3 alerts.py add --from-plan
python3 alerts.py check
python3 alerts.py check --now "2026-10-26 09:15" --json
python3 alerts.py plan
```

- `add` writes a rule into `trader/alerts.json`. A price rule needs
  `--symbol`; `--above`/`--below` watch a level, `--stop`/`--target` are
  the plan levels, `--note` and `--date` are optional. `--from-plan`
  copies the stop, target and flat-by from the trade plan.
- `check` evaluates every rule right now and prints what fired. It writes
  the seen state to `trader/state/alerts.json` so each alert rings once
  per session; the key includes the IST date, so tomorrow everything is
  armed again. Add `--json` for machine output: `now`, `fired`, `pending`,
  `missing`, `errors`. Exit code is 0 for a normal quiet check, 2 only for
  real failures (bad args, unreadable config, quote source failure).
- `plan` prints the plan the alerts follow: `trader/exit.json` when the
  exit skill wrote one, else `alerts.example.json` in this folder.

## The plan

`add --from-plan` reads `trader/exit.json`, the plan the exit skill
writes: `symbol`, `instrument_key`, `stop`, `target`, `flat_by` (HH:MM
IST), plus entry and quantity. From it the skill arms the plan stop, the
plan target and the flat-by reminder, all dated to the plan's date. With
no plan on disk it points you at `alerts.example.json` as the shape to
copy.

## State and re-arming

Seen state lives in `trader/state/alerts.json` and is written atomically.
Each fired alert is recorded under its symbol, alert kind, rule id and
IST date, so:

- a second check in the same session stays quiet,
- the next trading day re-arms every alert,
- a rule with a `date` only watches that one day.

Delete the state file, or edit `trader/alerts.json`, to re-arm by hand.
