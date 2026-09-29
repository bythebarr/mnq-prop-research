# Trade Accounting, Costs and Data Quality (Round 15)

Plain-English companion to `configs/rule_freeze_v1.yaml` (`commissions`,
`slippage`, `trade_accounting`, `missing_data`, `bad_data`,
`intrabar_ambiguity`, `position_management.normal_*`). The code is
`src/mnq_research/costs.py`, `accounting.py` and `data_quality.py`, and the
tests are `tests/test_round15.py`.

> The fees and slippage are **candidate assumptions**, not claims about
> future execution. No performance test has been run.

## Commissions

**$0.91 per contract side**: every contract entered and every contract
exited. So a 1-contract round trip is $1.82, and a 3-contract trade costs
$5.46, however it is split across fills. The source was supplied by the rule
owner: the Tradeify Help Center, retrieved 2026-09-29. It must still be
archived (URL, time, hash) before the one-contract backtest is approved.
Every result is reported three times: at the base fee, at 1.25× and at
1.50×.

## Slippage (always against you)

| Order | Ticks per contract |
|---|---|
| Entry (market) | 1 |
| Protective stop, normal 12:00 exit, news flatten | 2 |
| Session backstop, protection-failure, entry-invalidation, manual safety flatten | 3 |
| Target (limit) | 0, but it needs a one-tick trade-through to fill |

A buy fills higher and a sell fills lower. Every result is reported at 1×,
2× and 3× these ticks. Slippage is applied **once**: only to *modelled*
fills. Actual recorded fills keep their real price. Scenarios change prices
and P&L, never the signals.

## Exits

* **Normal exit at 12:00 New York.** Cancel the target, keep the stop, close
  at market, then confirm flat and that the orders are cancelled. If a stop,
  target, news or safety exit already happened, no second flatten order is
  sent. 15:55 remains only the emergency backstop.
* **Structural invalidation** means the stop-market acts immediately. There
  is no waiting for a 5-minute close.
* **Entry and exit in the same minute.** Only market events *after* the fill
  count. With only one-minute data, a reachable stop is assumed first, and a
  target is never awarded from unresolved ordering.

## P&L and R

* **Gross P&L**, per fill leg:
  * long: (exit − entry) × $2 × quantity;
  * short: (entry − exit) × $2 × quantity.
* **Net P&L** = gross − commissions. Slippage is already in the prices.
* **Planned risk:** planned points × $2 × intended quantity. It is kept, but
  never mixed with actual R.
* **Actual initial risk** is the sum of three parts:
  * |actual average entry − stop| × $2 × quantity;
  * the modelled stop slippage;
  * the round-trip commission.
* **Results in R:**
  * **net R** = net P&L ÷ actual initial risk;
  * **price-only R** = gross P&L ÷ price risk.

Worked long example:
* Entry reference 20010.50 fills at 20010.75, and the target fills at
  20026.00. Gross is +$30.50 and net is +$28.68.
* Actual initial risk is $24.82 (22 price risk + 1 stop slippage + 1.82
  commission), so net R = 28.68 ÷ 24.82 ≈ 1.16.
* The planned 1.50R was never the actual result.

## MFE / MAE (best and worst price during the trade)

These are measured from the first authoritative fill until flat, and never
capped at the stop or target:
* long: MFE = highest trade − average entry; MAE = average entry − lowest
  trade;
* short: the mirror.

The record also has both in price-only R, the time to each, and the time in
trade. Anything before the fill is ignored. If the data are bars rather than
single trades, the timing is marked approximate.

## Missing and bad data

| Situation | Treatment |
|---|---|
| Flat, and a known outage, an unexplained missing minute, or a rejected bad bar | The interval is ineligible, the setup resets, and a completely fresh setup is needed |
| Required levels or calendars can't be built | The whole date is ineligible |
| A trade is open when data go missing or bad | `OPEN_POSITION_DATA_GAP_UNRESOLVED`: the trade is kept and **unscorable**. There is no invented exit, and it is left out of primary statistics |
| Stress view | A separately **labelled** scenario charges every such gap a full −1R plus stop costs. This never goes into the primary baseline |
| Bad prices | Never repaired silently. Validation reports them, and the interval fails closed |
