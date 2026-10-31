---
name: trader-broker
description: >
  Order tickets, the three gates, and the Upstox lifecycle for the Indian
  pack: render the exact Upstox v3 payload, record your typed yes, and
  execute, follow and flatten through the broker seam. Sandbox by default,
  live only after a stored confirmation. Education and tooling, not financial
  advice.
---

# trader-broker: tickets, the three gates, and the Upstox lifecycle

Every order that leaves this pack passes through three gates, all enforced
by code in `ticket.py`, not by a checklist:

1. Risk. `trader-risk/check.py` must have a fresh PASS for that exact order
   today, matched by order hash (the order fields plus the plan's own entry,
   stop and target leg levels, so an edited plan voids the PASS too). Edit the
   quantity, the entry, the stop, the target or the tag and the old PASS is
   void. The PASS also expires: check.py stamps a `valid_until` (15 minutes,
   never past the entry cutoff), and an expired PASS is refused and asks for
   `check.py` again.
2. You. `approve --yes "<your reply>"` records your explicit yes, word for
   word, bound to the same order hash. No approval, no execute. A yes that
   names another order in the batch by its symbol is refused.
3. Host. Claude shows an Allow once / Deny card before the execute call. That
   card is the host tool approval and is never automated here.

Sandbox is the default. Live mode requires `mode: "live"` in the config and a
stored confirmation of the typed phrase `GO LIVE WITH UPSTOX` (via
`trader_core.config.confirm_live`). Without it the tools refuse.

## Commands

- `show` renders the ticket and the exact payload. `show --json` prints the
  same as JSON.
- `approve --yes "..."` records your yes. `--flatten` approves the flatten
  order instead of the entry.
- `execute` sends the approved order through `trader_core.broker.UpstoxBroker`
  and records the order id, status, fills and positions on the ticket.
- `result` reads the order book, the trades and the positions and updates the
  ticket; a completed exit records its realized P&L.
- `fill` records a fill by hand, with an optional `--pnl`.
- `flatten` prepares the opposite MARKET order with market protection set.
  It obeys the same three gates and is never bypassed on a live account.
- `log` prints today's decisions and fills.

## Files

| file | what |
| --- | --- |
| `trader/plan.json` | the proposed order's plan (trader-budget writes it; `flatten` reuses it) |
| `trader/state/risk.json` | trader-risk's verdicts; only an unexpired PASS from today is READY |
| `trader/state/orders.json` | every yes, placed, failed and filled, with each entry's order hash |
| `trader/ticket.json` | the ticket: the order, its payload, your yes, the result |

Reconcile first: before every check or approve, compare the ledger with the
read-only Upstox `get_order_book` and `get_positions`, and run `result` for
any approval with no result yet (the order may already be at the broker).

## Payload fidelity

The ticket renders the exact Upstox v3 place body in plain words and writes
it into the ticket JSON. The body is built once by `trader_core.orders.leg`,
rendered, stored, and sent unchanged. Shown equals sent, field for field.

## Upstox lifecycle

- Sandbox host by default: `https://api-sandbox.upstox.com`. Live host:
  `https://api.upstox.com`. The host is chosen from the config mode.
- Live needs the stored confirmation or `LiveGateError` is raised and no
  broker call is made.
- Expired or missing token: `UpstoxAuthError` prints the re-auth guidance
  and exits 2. Nothing is retried silently.
- The open-window guard covers 05:30 to 24:00 IST and is enforced before any
  order request is sent.
- `result` and `fill` update the ledger in `trader/state/orders.json`.

## Token setup and re-auth

- `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_ACCESS_TOKEN`,
  `UPSTOX_SANDBOX_TOKEN`, `UPSTOX_ANALYTICS_TOKEN`.
- Tokens live in the environment or an ignored file under `trader/state/`,
  never in the config, the chat or git.
- Daily access token expires at 03:30 IST; sandbox token lasts 30 days; the
  analytics token is read-only and lasts one year.

## IST market hours

- Open 09:15, entry window 09:30 to 15:00, flat by 15:10, MIS square-off
  15:25 (15:10 on CAS). Close 15:30. Weekends and NSE holidays are closed.
- `ticket.py` reports IST times and uses the IST date.

## SEBI static IP and algo note (documentation only)

- Static IP registration, SEBI algo registration and exchange approval are
  the user's responsibility. Nothing here automates them.

## Safety rule

No order goes out without your yes. The three gates (risk PASS, typed yes,
host tool card) must all hold before execute. Flatten is a separate order
and obeys the same gates; it is never bypassed on a live account. Education
and tooling, not financial advice.
