# Protective Orders: the stop/target bracket (Round 14)

Plain-English companion to `configs/rule_freeze_v1.yaml` (`protective_orders`,
`intrabar_ambiguity`). The code is `src/mnq_research/protection.py`, and the
tests are `tests/test_protection.py`.

> DRAFT component and **simulation only**. There is no broker, Quantower,
> Rithmic, paper or live connection. Deployment stays blocked until
> Quantower/Rithmic are proven to support server-side stops and server-side
> OCO (`capability_verification_status: REQUIRED_BEFORE_EXECUTABLE`). A
> client-side-only stop is not enough for a strategy that runs unattended.
> Commissions, slippage distributions, sizing and backtesting are not part
> of this round.

## What protects a position

| Piece | Order | Price (long; short mirrors) |
|---|---|---|
| Protective stop | STOP_MARKET, DAY | frozen structural stop, L − 0.25 |
| Profit target | LIMIT, DAY | frozen target, one tick before the next zone |
| OCO link | native, server-side | one fills → the other is cancelled or shrunk |

The quantity is always the **confirmed open quantity**, never the intended
order size. Prices are never trailed, widened, tightened, moved to breakeven
or recalculated.

## From fill to "protected"

1. **The fill.** Every positive authoritative entry fill creates
   `PROTECTION_REQUIRED`. The task carries every ID, the prices, the actual
   average entry, the first-fill time, the deadline and the spec hashes.
2. **Dispatch.** Sending the protective orders must start in the same step
   as the fill, and **no later than 250 ms** after it. If it starts later,
   the result is `PROTECTION_DISPATCH_DEADLINE_MISSED`, an emergency flatten,
   and a day halt.
3. **Submission.**
   - **Preferred:** one atomic, server-side OCO bracket.
   - **Fallback:** separate server-side orders. The stop goes first; only
     after it is confirmed does the target go; only after that is the OCO
     link requested.
   - A **client-side-only** setup is refused: emergency flatten.
4. **Confirmation.** The stop, the target and the OCO link each have their
   **own 2-second acknowledgement clock**. Each confirmation must be an
   authoritative broker report with the right order type, price, quantity,
   side, contract and account. A local object or a request is not
   confirmation.
5. **Active.** The state becomes `PROTECTION_ACTIVE` only when all three
   parts are confirmed with matching quantities. Before that it is
   `PROTECTION_PENDING`. A submitted bracket is never "active".

**Stop first:** a confirmed stop without a target is temporarily protected. A
target without a stop is never acceptable and forces an emergency flatten.

## When the entry fills in pieces

Each confirmed partial fill is protected immediately. When more entry fills
arrive, the stop and target are raised to the new total confirmed quantity at
the same prices, and the state goes back to pending until the broker
confirms. If the quantity cannot be kept in step, the result is
`PROTECTION_QUANTITY_UNKNOWN` and an emergency flatten.

## OCO behaviour on exit

* **One sibling fills completely:** the other is cancelled, and that
  cancellation must be confirmed. It can never create a reverse position.
* **One sibling fills partly:** the other is shrunk to exactly the remaining
  quantity, and the OCO stays in force for the rest.
* **A shrink or cancel isn't confirmed in time:**
  `OCO_RECONCILIATION_FAILURE`, and any confirmed remainder is flattened.
* **A sibling fill after the position was confirmed flat,** or a fill larger
  than the open quantity: this is a contradiction that may have reversed the
  account. The result is `UNKNOWN_EXIT_STATE`, the unintended quantity is
  recorded, and it is reconciled and flattened immediately.

**Flat** means an authoritative position report of **zero**. An exit fill
alone is not enough. The remaining siblings must then be confirmed
cancelled, and only then is the trade closed. The day's entry allowance
stays used.

## Failures: always flatten, never improvise

| Failure | Result |
|---|---|
| **Stop problem:** rejection, timeout, unknown state, wrong price, quantity, contract, account or side, cancelled without replacement, inactive, or platform disagreement | `PROTECTIVE_STOP_FAILURE` → emergency flatten |
| **Target problem** while the stop is active | `TARGET_PROTECTION_FAILURE` → cancel the target, keep the stop until flat, emergency flatten. There is no "let the stop run" trade |
| **OCO link** absent, rejected or unknown | `OCO_LINK_FAILURE` → emergency flatten |
| **Entry filled at or beyond the stop** (the latched flag) | no target and no pretend bracket; flatten immediately (`ENTRY_INVALIDATION_FLATTEN`) |
| **Invalid structural stop** found after a fill | position treated as unprotected → emergency flatten |

Every emergency also means:
* a day halt;
* the original stop kept for the audit;
* reconciliation;
* a critical alert;
* no widening or re-creating strategy risk.

## Scheduled flatten

At the session emergency-flatten time, or a news flatten, or a manual safety
flatten:
1. Cancel the target.
2. Keep the stop.
3. Close at market.
4. Confirm a zero position.
5. Cancel the stop.
6. Confirm no sibling can reopen exposure.

The exact live order of these steps must be proven by adapter fault-injection
tests.

## Research replay: when do the exits happen?

| Exit | Rule |
|---|---|
| **Stop** | Triggers on the **first authoritative trade** at or through the stop. The fill price comes from the (not yet frozen) stop-slippage model; it is never assumed to be the trigger price |
| **Target** | Needs a **one-tick trade-through** (long: a trade ≥ target + 0.25). A touch is not a fill. Recorded as `CONSERVATIVE_ONE_TICK_TRADE_THROUGH_TARGET_FILL` |
| **Both in one minute** | With no finer data, the stop is assumed first (`SAME_BAR_STOP_TARGET_AMBIGUITY`, `CONSERVATIVE_STOP_FIRST`). Trades or one-second bars that show the order are used instead (`CHRONOLOGY_FROM_HIGHER_RESOLUTION`, with the source reference). If the finer interval is still ambiguous, the stop is assumed first |

Quotes and local estimates never trigger or fill anything.

## How a trade can end

* TARGET_FILLED
* STOP_FILLED
* SCHEDULED_NEWS_FLATTEN
* SESSION_EMERGENCY_FLATTEN
* PROTECTION_FAILURE_FLATTEN
* ENTRY_INVALIDATION_FLATTEN
* MANUAL_SAFETY_FLATTEN
* UNKNOWN_EXIT_STATE

Every record keeps:
* the actual fills, quantities and times;
* the reasons and the event log;
* the costs, marked `UNRESOLVED_UNTIL_COST_MODEL_FROZEN` for now.
