# Live trading runbook (Robinhood)

The owner chose to let the scheduled routine trade a small Robinhood account
with real money and no per-trade approval. The money in that account is the
cap: they funded it with what they can afford to lose. This runbook is what
the routine's session follows. It is strict on purpose: when a step fails or
anything is unclear, the safe move is to not trade, or to get flat.

The account number is in the routine's own prompt, never in this repo. Nothing
about the live account is committed: Robinhood is the record.

## Rules that hold on every run

- Trade only the account named in the routine's prompt. Never any other account.
- Stocks only, long only. No options, crypto, shorting or margin: never spend
  more than the account's **cash** (if buying power is higher than cash, the
  difference is margin; ignore it).
- One position at a time, at most one entry a day, always flat by the close.
- Every position gets its stop at Robinhood (the sell OCO) right after the buy
  fills. A position must never sit without a stop. If the stop cannot be placed,
  sell the shares at market at once.
- If a review shows any alert (pattern day trader, buying power, halt, anything
  else), do not place that order. Report the alert.
- Never change account settings, trade approvals or transfers.
- Kill switch: if the account's total value is under `live_kill_switch_usd`
  (trader/live-config.json, $150), place no new entries. Flattening still runs.

## Morning run (10:22 AM New York): the entry

1. `get_portfolio` for the account: note the cash and the total value. Kill
   switch check (above).
2. `get_equity_positions`: if any stock position is open, do not enter. Go to
   step 9 and make sure it has a stop (an open sell OCO in `get_advanced_orders`);
   if it has none, sell it at market.
3. `get_equity_orders`: if a buy already filled today, do not enter (one a day).
4. Run, from the repo root, with the cash from step 1:

   ```bash
   python3 trader-paper/paper.py open --live --cash CASH \
     --config trader/live-config.json --portfolio trader/live/portfolio.json \
     --broker-state trader/live/broker.json --state trader/live/state.json
   ```

   It scans, picks, plans, runs the Time Machine, sizes to the cash and the
   $6 max loss, and runs the risk check, exactly as paper trading does. If it
   prints "LIVE ORDER READY", the order is in `trader/live/order.json`. If not,
   there is no trade today: report what it passed over and why, and stop.
5. `review_equity_order`: buy, `qty`, type limit, `entry.limit_price`,
   time_in_force gfd, market_hours regular_hours. Any alert: stop and report.
6. `place_equity_order` with the same fields and a fresh UUID `ref_id`. If the
   response says the customer must approve it in the app, report that and stop
   (do not place the OCO for shares that are not bought).
7. Check the order with `get_equity_orders` a few times over about two minutes.
   Not filled by then: `cancel_equity_order`, confirm it is canceled, report, stop.
   Partly filled: cancel the rest and protect the shares that did fill.
8. Filled (Q shares at F): `review_advanced_order`, then `place_advanced_order`:
   side sell, quantity Q, `stop_loss_stop_price` and `take_profit_limit_price`
   from `exit_oco`, time_in_force gfd, a fresh UUID `ref_id`. If Robinhood rejects
   it because the price has already moved within 0.25 % of a leg, sell the Q
   shares at market instead (the trade has hit its exit already).
9. Confirm the OCO is open with `get_advanced_orders`. If it is not, sell at market.
10. Report (the owner reads these to learn): the stock, shares, fill price, stop,
    target, max loss at the stop, the `why`, the Time Machine's odds, and the
    stocks passed over with their reasons, all in plain words.

## Afternoon run (3:47 PM New York): flat by the close

1. `get_advanced_orders`: cancel every open OCO on the account
   (`cancel_advanced_order`) and confirm each is canceled. The shares are held by
   those orders until then, so a sell placed first would be rejected.
2. `get_equity_positions`: for each stock position, `review_equity_order` then
   `place_equity_order`: sell all shares, type market, gfd. Confirm the fill.
3. Report the day: what was bought, how it got out (stop, target or flat by the
   close), the realized P&L in dollars and in R (P&L divided by the max loss at
   the stop), the account's total value now (`get_portfolio`), and one or two
   sentences on what the trade shows. No trade today: say so and why.

## Why these choices

- **Limit entry, 0.2 % over the last close**: a market order in a fast stock can
  fill far from the plan; a limit caps the price paid, at the cost of sometimes
  missing the trade.
- **OCO after the fill**: Robinhood cannot attach a stop and a target to the buy
  itself. The window between the fill and the OCO is the one moment the position
  has no stop, so step 8 comes right after the fill.
- **$6 max loss a trade, one trade a day**: about 2 % of the account. A losing
  streak of ten trades costs about $60, not the account.
- **Stocks under the cash, whole shares**: Robinhood's stop and limit orders take
  whole shares, so a stock priced over the cash cannot be traded at all.
- **Pattern day trader**: every trade here is a day trade. If Robinhood still
  applies the old rule to this account, the review shows a PDT alert before a
  fourth day trade in five business days, and the run stops there.
