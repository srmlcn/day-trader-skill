---
name: trader-paper
description: >-
  Paper Autopilot. Runs the whole day-trading pipeline on a schedule with
  simulated fills, so a strategy can be paper traded unattended: scanner,
  entry timer, exit planner, time machine, budget and risk check run exactly
  as they would for you, and where you would send the order it books a
  simulated fill from the real 5-minute bars instead. Never talks to a broker,
  never needs an API key. Settles each position at its stop, its target or
  flat by the close, and keeps a journal, a ledger and a running scorecard
  (P&L, win rate, average R). Use when the user says "paper trade on a
  schedule", "run the autopilot", "how is the paper account doing", "settle
  today's paper trades", "paper trading report", or sets up a scheduled
  routine for this repo.
argument-hint: "[tick | open | settle | report]"
---

# trader-paper

The Paper Autopilot is the one way to run this pack with nobody watching.
It keeps the pack's hardest rule: **trader-broker never places an order from
a scheduled task, because nobody is there to say yes.** So the autopilot does
not place orders at all. It books simulated fills in a ledger, and the ledger
lives in the same files trader-risk reads, so your limits still hold.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/paper.py" tick       # what the clock calls for
python3 "${CLAUDE_SKILL_DIR}/paper.py" open       # scan, pick, plan, size, check, book the buy
python3 "${CLAUDE_SKILL_DIR}/paper.py" settle     # stop, target or flat by the close
python3 "${CLAUDE_SKILL_DIR}/paper.py" report     # the scorecard -> trader/paper/summary.md
```

`--now "2026-10-19 10:22"` pins the clock (New York time), `--offline FILE`
reads a saved Yahoo chart, `--skip-scan` reuses `trader/watchlist.json`.

## What `tick` does

| New York time | what happens |
| --- | --- |
| weekend or NYSE holiday | nothing |
| before 9:50 | settle only (nothing to open yet) |
| 9:50 to `no_entries_after` (15:30) | settle, then open: up to `max_trades_per_day` new trades |
| `flat_by` (15:55) and later | settle (everything still open is sold at the flat-by bar), then the report |

The scheduled routine calls `tick` twice a weekday: once in the morning for
the entries and once after the close to settle and report.

## How it picks a trade

1. `scan.py orb`: every opening-range breakout so far (trader-scanner).
2. Hits under `min_price` are out. The most recent triggers go first, then the
   smallest move over the range high.
3. For each, the live 5-minute chart: the last completed close is the entry.
   Out if it is back under the range high or the midpoint, or more than
   `max_chase_pct` over the range high (a late entry is a different trade).
4. `entry.py check` must say triggered; `exit.py plan` sets the stop (`stop`,
   default the range midpoint) and target (`target`, default 1R) from that entry.
5. With `timemachine_gate` on, `timemachine.py run` must give an average R
   over `min_tm_avg_r` (0).
6. `size.py` sizes it to your max loss, cash and caps; `check.py` must PASS.
7. Booked: a placed entry in `trader/state/broker.json` (with its max loss, so
   the daily loss limit counts it), the buy in `trader/portfolio.json`, the
   position in `trader/state/paper.json`, a line in `trader/paper/journal.md`.

The settings live in the `"autopilot"` block of `trader/config.json`
(defaults in `trader-budget/config.example.json`). The autopilot refuses to
run unless the config says `"paper": true`.

## How a fill is simulated

| | |
| --- | --- |
| entry | the close of the last completed 5-minute bar, plus `slippage_bps` (5) |
| stop | the first later bar whose low reaches the stop: filled at the stop, or at the bar's open if it gapped through |
| target | the first later bar whose high reaches the target: filled at the target, or the open if it gapped above |
| both in one bar | counted as the stop (the conservative read) |
| flat by | still open at `flat_by`: sold at that bar's open |
| missing bars | if the flat-by bar never printed, sold at the last close, and the journal says so |

Honest limits: 5-minute bars are not ticks, so the order of the high and the
low inside one bar is unknown, and a real market order can fill worse than a
bar close. The risk check sizes from the bar close; the slippage makes the
recorded loss at the stop a little larger than the max loss it checked.

## Files

| file | what |
| --- | --- |
| `trader/config.json` | your limits, `"paper": true`, the `"autopilot"` block |
| `trader/state/paper.json` | open positions, closed trades, what was passed over and why |
| `trader/state/broker.json` | the same log trader-broker keeps, so trader-risk sees the trades |
| `trader/portfolio.json` | simulated cash and shares |
| `trader/paper/journal.md` | one line per buy and sell |
| `trader/paper/summary.md` | the scorecard, rewritten after each close |

## The scheduled routine

The routine runs in a cloud session on a schedule. Each run: check out the
branch that holds the ledger, run `paper.py tick` from the repo root, then
commit the ledger files above and push, so the next run starts from them.
The session's network must reach query1.finance.yahoo.com,
query2.finance.yahoo.com and www.nasdaqtrader.com.

## Live mode (opt in)

`paper.py open --live --cash CASH --config trader/live-config.json` runs the
same pipeline against a real account's cash and writes ONE order to
`trader/live/order.json` instead of a simulated fill. `paper.py` itself still
places nothing: the scheduled session places that order through the Robinhood
connector, following `trader-paper/LIVE.md` to the letter (limit entry, the
stop and target as an OCO right after the fill, flat by the close). It exists
because an owner chose to let a routine trade a small account they funded with
money they can afford to lose, with no per-trade yes. The account balance is
the cap. `--live` refuses a config that does not say `"paper": false`, and the
simulated commands refuse one that does.

## What it never does

- Simulated mode never calls a broker or an order tool; neither mode asks for API keys.
- Simulated fills never run unless the config says `"paper": true`.
- Never moves a stop, adds to a position or holds overnight.
- Never reports simulated P&L as if it were real.

## Safety

- Education and tooling, not financial advice. A paper result is a simulation, not a promise; most day traders lose money.
- Real orders still go only through trader-broker, after trader-risk passes that exact order and the user types yes for it in the chat, then clicks Allow once.
- Paper results from 5-minute bars are optimistic about fills. Treat a good scorecard as a reason to keep testing, not to go live.
