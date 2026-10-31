# The Day Trader skill, India

10 Claude skills for day trading Indian markets: scan the NSE universe, track
Indian news, time every entry and exit in IST, and replay every outcome in INR
before you risk a rupee, on Upstox with sandbox trading by default. Free, MIT,
no signup. Everything is plain Python, standard library only.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Sandbox is the default.

> **Looking for the original?** This is the India port of Jake Schincariol's
> day-trading pack,
> [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill).
> The original ships the US-market version: Alpaca paper accounts, SEC EDGAR
> filings, and the House Clerk's trade disclosures are out of scope here;
> this repo trades NSE equities in INR on Upstox.

## Install

Paste this into Claude:

```
https://github.com/fenilmodi00/day-trader-skill-india

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add fenilmodi00/day-trader-skill-india
/plugin install day-trader-skill-india@day-trader-skill-india
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill-india:trader-scanner` and so on. Copy the folders instead
if you want plain `/trader-scanner`:

```bash
git clone https://github.com/fenilmodi00/day-trader-skill-india.git
cp -r day-trader-skill-india/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. Keep `trader_core/` alongside them: every skill imports
it. No Claude Code at all? Paste any single `SKILL.md` at the top of a chat
and it runs as a mode. You lose the Python tools, but the method works.

The tools need Python 3.8 or newer. Nothing to pip install. All network
access goes through `trader_core`, so tests and every `--offline` flag run
without a token.

## The ten

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans the NSE EQ universe and Nifty 500 for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks Indian news on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, snapped to the tick. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions from 1-minute bars, plus every past session the setup fired. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your INR budget and your max loss per trade. |
| `/trader-broker` | Order Executor | Sends approved orders to Upstox, sandbox by default, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings at the 09:00 pre-open bell, the 09:15 open, a price, a stop, a target and 15:10. |
| `/trader-analyst` | Results Analyst | Reads an Indian results filing you supply (XBRL) and sums up what changed. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: 09:00 pre-open, 09:15 open, stop, target, flat by 15:10               (while it is open)
news / analyst                                                                (on a schedule)
```

An example, one trade on RELIANCE, Monday October 26 2026, the numbers the
fixtures reproduce:

1. **`/trader-scanner`** after 09:35 IST: the opening range 09:15-09:30 on
   RELIANCE is 1175.30 to 1192.00, and the watchlist flags the symbol.
2. **`/trader-news`**: the morning's RELIANCE headlines in IST, tagged
   results, board meeting, or ASM/GSM, so a day trade is out before a
   scheduled announcement.
3. **`/trader-entry`** watched RELIANCE bar by bar. At 10:00 IST it said
   triggered: the first 5-minute close above the range, the 09:55 candle
   closing at 1193.60.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 1183.65, so 9.95 a share at risk; target one times that risk,
   1203.55; flat by 15:10 either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle resampled from the stock's own 1-minute bars (seed 19, so
   anyone can repeat it), and re-ran the same rule on every past session in
   its window. It states how many sessions it used, because Upstox gives it
   only about 15 to 20. That is the point of looking before you risk a rupee.
6. **`/trader-budget`** with a 50,000 rupee account and a 250 rupee max loss
   per trade: 250 / 9.95 = 25 shares (29,840 rupees at entry). If the stop
   fills at 1183.65, the loss is 248.75 before charges. It writes one
   two-leg MIS order: buy 25 RELIANCE intraday, stop-loss 1183.65,
   take-profit 1203.55.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, the flat-by
   time, lot size, tick size and freeze quantity. PASS or BLOCK, with the
   reason.
8. **`/trader-broker`** shows you the exact Upstox v3 payload in plain words.
   You type yes for that exact order. Sandbox is the default; live mode needs
   the config change and the typed phrase below, and Claude's own
   "Allow once / Deny" card asks you one more time.
9. **`/trader-alerts`** rings on the stop, the target, and at 15:10 to
   flatten whatever is still open, before the broker's 15:25 MIS square-off.

## The tools

All standard-library Python. Market data and orders come from Upstox.

```bash
python3 trader-scanner/scan.py orb --at 10:15            # opening-range breakouts so far
python3 trader-scanner/scan.py daily                     # today's bhavcopy -> the day's watchlist
python3 trader-news/news.py --tickers RELIANCE --hours 12    # Indian headlines, today's move
python3 trader-entry/entry.py check RELIANCE --key "NSE_EQ|INE002A01018"   # waiting / triggered / expired
python3 trader-entry/entry.py watch RELIANCE --every 30  # bar by bar until it triggers
python3 trader-exit/exit.py plan --from trader/entry.json --write    # -> trader/exit.json
python3 trader-timemachine/timemachine.py run --write    # 10,000 replays + the history
python3 trader-budget/size.py --config trader-budget/config.example.json --write   # -> the order plan
python3 trader-risk/check.py --config trader-budget/config.example.json            # PASS / BLOCK
python3 trader-broker/ticket.py show                     # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan          # stop, target, flat by 15:10
python3 trader-alerts/alerts.py check                    # rings what hit, once
python3 trader-analyst/facts.py RELIANCE --xbrl RELIANCE-results.xml   # quarters, growth, EPS
```

## Sandbox first, live behind a phrase

Sandbox is the default everywhere. Orders go to Upstox's sandbox host and
market data comes from the analytics token; nothing touches real money. Live
mode requires both of these, and anything missing or misspelled fails closed:

1. `"mode": "live"` in your ignored `trader/config.json`.
2. The typed phrase `GO LIVE WITH UPSTOX`, stored once via
   `trader_core.config.confirm_live` after you enter it.

Even then, only `/trader-broker` can lead to an order, and only after
`/trader-risk` passes that exact order and you type yes for it in the chat.

## Token setup

Upstox asks for an app's API key and secret, then issues tokens. Set them as
environment variables, or in an ignored token file under `trader/state/`,
never in `trader/config.json`, the chat or git:

| variable | what it is |
| --- | --- |
| `UPSTOX_API_KEY` | your app's key from the Upstox developer console |
| `UPSTOX_API_SECRET` | your app's secret |
| `UPSTOX_ACCESS_TOKEN` | the daily access token; expires at 03:30 IST, required for orders |
| `UPSTOX_SANDBOX_TOKEN` | the sandbox token; lasts 30 days |
| `UPSTOX_ANALYTICS_TOKEN` | the read-only analytics token; lasts one year, serves market data |

The access token is issued per day, so it expires at 03:30 IST: every
morning before the open, log in again and refresh it. Nothing in this pack
refreshes tokens automatically.

## Upstox history limits

Historical candle limits on Upstox, which every skill respects:

| interval | history | note |
| --- | --- | --- |
| 1 minute | 1 month | the deepest intraday series |
| 30 minutes | 1 year | |
| day | 1 year | |
| week, month | 10 years | |
| 5 minutes | none | no historical 5-minute series: the tools resample 1-minute bars to 5-minute |

That is why the Time Machine's window shrinks: it replays from 1-minute bars
resampled to 5-minute, so it sees at most about one month of sessions, 15 to
20, never fewer than 15, and it says how many it used. The current day has a
live 5-minute intraday endpoint, which the entry timer uses.

## The IST session

All times IST, NSE trading days only (weekends and NSE holidays are closed):

| time | what happens |
| --- | --- |
| 09:00 | pre-open window begins; the alerts pre-open bell rings |
| 09:15 | the market opens; the opening range starts |
| 09:30 | the entry window opens |
| 15:00 | the entry cutoff: no new entries after this |
| 15:10 | flat-by: everything closed; also the MIS square-off time on CAS days |
| 15:25 | the broker's MIS square-off: the broker closes what is still open |

The session closes at 15:30. Cut-offs are configurable in `config.json`, but
the defaults are the ones above, and the point of 15:10 is that you flatten
yourself before the broker does it for you at 15:25.

## Charges

Every rate is configurable in `config.json`; the defaults below are worked
example numbers, not a quote. On an intraday round trip, buy 25 RELIANCE at
1193.60, sell at 1203.55, turnover 59,928.75 rupees:

| head | who takes it | this trade |
| --- | --- | --- |
| brokerage | Upstox, per your slab (default: 0.05% capped at 20 rupees an order) | 29.96 |
| STT | the government, sell side only for intraday | 7.52 |
| exchange transaction | NSE | 1.78 |
| SEBI turnover fee | SEBI | 0.06 |
| stamp duty | the state, buy side only | 0.90 |
| GST | 18% on brokerage + exchange + SEBI | 5.72 |

46.00 rupees of charges on a 248.75 gross: 202.81 net. At the stop instead of
the target the same trade is 294.28 out of pocket, charges included. The
risk manager shows you charges-adjusted numbers before you approve anything.

## India fine print

**Not financial advice.** This is software that reads market data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a licensed professional if you need advice.

**SEBI algo rules are documented, not automated.** If you automate order
placement, SEBI's algorithmic-trading framework and your broker's terms can
require a static IP registered with the broker and, for some setups, algo
registration or exchange approval. This pack does not set up a static IP,
does not register anything, and makes no claim of compliance: that is
between you, your broker and SEBI.

**No live surveillance feed.** The scanner and risk manager read the
config's static lists for blocked symbols, ASM and GSM stages and the F&O
ban list. They warn, they do not claim live enforcement: there is no
exchange feed of circuit bands, ban lists or surveillance stages here.

**Sandbox data is not live data.** Upstox's documented sandbox covers order
APIs; market data comes from the live analytics token. If sandbox data is
missing for what you asked, the tools fail closed instead of quietly mixing
in live orders.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last month of 1-minute bars. The future can do things that month
never did: a results day, a halt, a headline, a circuit band. Treat its odds
as a reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop order can fill well past its price, and the MIS square-off is a market
order by nature. The max loss shown is the loss at the stop price.

**Taxes.** Intraday gains are speculative business income in India, and
frequent trading means frequent charges. Nothing here is tax advice.

## Tests

Everything runs offline on recorded fixtures, no token, no network:

```bash
python -m unittest discover -s tests
```

The fixtures are cut from real Upstox and NSE data, including the RELIANCE
session of October 26 2026, and they reproduce every number in the example
above.

## Files

```
trader-scanner/     SKILL.md, scan.py, nifty500.csv
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json
trader-broker/      SKILL.md, ticket.py, config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader_core/        the shared core: IST time, NSE calendar, config, Upstox client, orders, charges
tests/              unittest suite + offline fixtures
```

MIT. Original pack by [Jake Schincariol](https://opusjake.ai), India port by
the contributors of this repo.
