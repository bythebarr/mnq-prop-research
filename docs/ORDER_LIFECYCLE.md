# Order Lifecycle: from a selected candidate to a filled (or not) entry (Round 13, D-029)

Plain-English companion to `configs/rule_freeze_v1.yaml`
(`entry_order_lifecycle`, `order_type`, `order_validity`, `stop_placement`,
`position_management`, `execution_eligibility_integration`). The code is
`src/mnq_research/entry_order.py` and `src/mnq_research/eligibility.py`,
and the tests are `tests/test_entry_order.py`.

> DRAFT component and **simulation only**. Nothing here talks to a broker,
> platform or network. Live and paper submission are **prohibited** until a
> protective-order layer exists. This round does **not** do position sizing,
> commissions, protective stop/target orders, trade management, prop-account
> simulation or backtesting.

## What triggers an order (D-029)

The trigger is **only** a `SELECTED_ENTRY_CANDIDATE`: the end of the whole
frozen sequence.

    structure → acceptance → pullback → hold → continuation confirmation
      → room qualification → deterministic selection → submission-time safety checks

A touch, a breach, an acceptance close, a retest, a wick through the hold bar
or a discretionary command never places an order on its own.

Each selected candidate gets **at most one** order lifecycle. There is no
retry after a rejection, no replacement after expiry, no second order after a
zero fill, and no top-up after a partial fill. A later entry needs a
completely fresh attempt, acceptance, confirmation and selection. It is also
subject to the one-filled-entry daily limit.

## The timeline

```
10:30:00.000  decision bar closes, SELECTED_ENTRY_CANDIDATE -> order CREATED
10:30:01.000  eligibility re-checked -> MARKET order SUBMITTED (or NOT_SUBMITTED_INELIGIBLE)
   ...        acknowledgement, fills (each recorded with time, quantity, price)
10:30:03.000  deadline (or earlier): cancel whatever is still unfilled
   ...        cancel confirmed -> exactly one final outcome
```

* **Market orders only.** No limit, stop, stop-limit or market-if-touched.
* **Exactly 1.000 s** after the decision. Nothing can fill before the
  submission moment: a fill reported earlier is treated as a simulator bug.
* **The deadline** is the earliest of:
  * submission + 2.000 s;
  * 11:30:00 New York;
  * the next news entry-protection or blackout boundary;
  * a safety halt, loss of reliable state, or contract/session invalidation.

  At the deadline, the remainder is cancelled. It is never extended,
  replaced, converted or chased.

## The re-check at submission

Everything that could have changed in that one second is checked again. Each
control must be positively `CLEAR`:

| Control | Blocks when |
|---|---|
| news_blackout / news_entry_protection | inside a blackout or the protection buffer |
| safety_halt / directional_conflict_halt / daily_entry_halt | any halt is active |
| session_valid | wrong date, outside the window, session invalidated |
| data_valid | missing, incomplete or unreliable data |
| candidate_current | the candidate expired or was already used |
| no_open_position / no_working_entry_order | a position or another entry order exists |
| contract_current / zones_current | the contract rolled or the zones changed |

The code also checks the clock itself (11:30 cutoff) and the news boundaries.
If any check is not CLEAR (including `UNKNOWN`), the order is **not
submitted**. It becomes `NOT_SUBMITTED_INELIGIBLE` with the exact reasons,
and the candidate is used up.

## The outcomes

| Outcome | What happens | Halts the day? |
|---|---|---|
| ENTRY_FULLY_FILLED | average fill, each fill, slippage vs the confirmation close and vs the planned entry, and latency to first and to final fill are recorded | uses the day's one filled entry (see below) |
| ENTRY_PARTIALLY_FILLED | the filled part is kept and protected; the rest is cancelled; never topped up; stop and target unchanged | **yes** |
| ENTRY_NOT_FILLED | no position; candidate used up | **yes** |
| ENTRY_ORDER_REJECTED | code and message recorded; no retry, no other order type | **yes** |
| ENTRY_ORDER_STATE_UNKNOWN | never resubmit or replace; query order and position; no added exposure; protect what is confirmed; critical alert | **yes** |
| NOT_SUBMITTED_INELIGIBLE | exact reasons recorded; candidate used up; no position, so the filled-entry allowance is **not** used | depends on the reason (below) |

**When a NOT_SUBMITTED order stops the day:**

| Reason | Effect on the rest of the day |
|---|---|
| A temporary, known block (e.g. news entry protection) | none; a completely fresh setup may trade after the block ends |
| A permanent safety halt or directional conflict | the day is already halted by that rule |
| Any `UNKNOWN` eligibility or system state | **halt** for the rest of the date |
| A stop that fails its integrity checks | **halt** (integrity failure; awaiting your confirmation) |

### Acknowledgement (D-029 decision 1)

The broker must acknowledge the order, or report some other authoritative
state such as a rejection, within **2.000 s of the submission timestamp**.
Otherwise the lifecycle is `ENTRY_ORDER_STATE_UNKNOWN`.

The exception is an **authoritative fill record**, which proves that the
order reached the market. The lifecycle is then not "unknown" for that
reason alone. Instead:
* the anomaly `ACKNOWLEDGEMENT_MISSING_BUT_FILL_CONFIRMED` is recorded;
* reconciliation continues;
* the confirmed position is protected;
* nothing is resubmitted.

A **local fill estimate** is not proof. It creates no exposure and is kept
only as a diagnostic. Every fill must carry its source (`FillSource`); an
unlabelled fill is refused.

### The unknown state (D-029 decisions 2 and 3)

Any of these makes the lifecycle `ENTRY_ORDER_STATE_UNKNOWN`:
* no acknowledgement or authoritative state within 2.000 s;
* **lost reliable state**: connection loss, an order-query failure, a
  position-query failure, stale state, platform and broker disagreeing, or
  missing identifiers needed to reconcile;
* **contradictory reports**:
  * filled more than submitted;
  * a fill after the cancellation was *confirmed*;
  * a rejection and a fill for the same order;
  * a broker position that differs from the authoritative fills;
  * a side or contract mismatch between linked records;
  * incompatible terminal states, such as "filled" and then "cancelled".

In the unknown state, fail closed: no duplicate or replacement entry, no
added exposure, halt for the date, alert and reconcile, keep any confirmed
protective order, and protect any confirmed position.

**History never changes.** The UNKNOWN event is an immutable
`OperationalAnomaly`, and it is never rewritten or deleted.

**Reconciliation** adds a *new* current-exposure record, which is one of:
* `RECONCILED_FLAT`
* `RECONCILED_OPEN_POSITION`
* `RECONCILED_PARTIAL_POSITION`
* `RECONCILIATION_UNRESOLVED`

A reconciled position must be protected or flattened. Reconciliation never
gives back the day's ability to enter.

**Cancellation race:** a fill that arrives while the cancellation is only
*requested* is not a contradiction. It is a race fill: real exposure,
protected and reconciled, and no new entry that day. A fill after the
cancellation was *confirmed* is a contradiction (see above).

## One filled entry per day (D-029 decision 4)

`max_filled_entries_per_trading_date: 1` is a candidate B0 account-safety
value, not a claim that one trade a day is best.

Any positive confirmed fill uses up the day's allowance. That includes:
* a full fill;
* a partial fill;
* a race fill;
* a fill confirmed without acknowledgement;
* a reconciled open position.

The daily state then becomes `FILLED_ENTRY_LIMIT_REACHED`. The open position
blocks entries while it is open. **Closing it does not give the allowance
back**: there is no second entry and no re-entry that date. Later signals
may still be logged as diagnostics, but they can never become B0 trades.

## Actual numbers, not planned ones

The planned entry (confirmation close ± 1 tick) was only for choosing a
candidate. After a fill:

* actual risk per contract = |average fill − **frozen** stop|;
* actual R for an exit = (exit − average fill) ÷ actual risk (long; the
  short side mirrors it);
* slippage is measured adverse-positive (for a long, a higher fill is worse).

Example: planned entry 20010.25, stop 19999.75, fill 20011.25. The actual
risk is 11.50 points, not 10.50. A target at 20026.00 is then worth about
1.28R, not the planned 1.50R.

## Stops

* There is **no minimum or maximum stop size** in B0. A wide structural stop
  is traded, not filtered.
* The stop must be positive, finite, on the tick grid, from the originating
  zone, and on the protective side of the planned entry. Otherwise the order
  is not submitted.
* The stop is **never** resized, compressed, widened or recalculated.
* **Fill at or beyond the stop (D-029 decision 5).** This means a long
  average fill ≤ the stop, or a short average fill ≥ the stop. The position
  is never left unprotected on purpose, and no stop is placed on the wrong
  side of the fill. Instead:
  * `ENTRY_FILLED_AT_OR_BEYOND_INVALIDATION` and `EMERGENCY_FLATTEN_REQUIRED`
    are recorded;
  * the confirmed quantity must be flattened immediately, using the
    emergency market-close, retry and reconciliation rules;
  * the day halts;
  * the original stop is kept unchanged for the audit;
  * every fill, slippage, cost and loss still counts, and the trade is never
    treated as if it hadn't happened.
* **Fill on the valid side but worse than planned.** The stop and target stay
  exactly where they were. The larger actual risk and the lower actual R:R
  are recorded, and nothing is widened to "restore" the planned 1.50R.

## Quantity and risk

* The quantity is **1 contract**, labelled `RESEARCH_QUANTITY_ONLY` and
  `NOT_DEPLOYMENT_SIZING`. It exists so the lifecycle can be tested. It is
  not a sizing decision. (With 1 contract a partial fill cannot happen; the
  partial-fill rules are tested with a larger test-only quantity.)
* `risk_per_trade` is deliberately **unresolved** until evidence exists.
* **The sizing mechanics are frozen (D-029 decision 6)**, in
  `src/mnq_research/sizing.py`:
  * contracts = floor(allowed planned account risk ÷ modelled risk per
    contract);
  * fewer than 1 → `POSITION_SIZE_BELOW_ONE_CONTRACT`, `SKIP_TRADE`;
  * never round up, force one contract, compress the stop, raise the allowed
    risk, or use fractional contracts.

  Only exact decimals are accepted. Nothing calls this yet, because the
  dollar risk and its balance basis are still unresolved.
* The **nominal $50,000 account size is not risk capital**. The balance
  basis waits for the prop-rule model.

## Protection

Any fill creates a **required protection task**: the confirmed quantity, the
frozen stop and the frozen target. Its status is
`REQUIRED_PROTECTION_LAYER_NOT_IMPLEMENTED`. Until that layer exists,
`live_or_paper_order_submission: prohibited` stands. The validator rejects
any other value.

## Audit

Every order produces a deterministic `audit_record()`. It includes:
* all eight lifecycle timestamps;
* the deadline and its reason;
* each fill;
* the outcome and its reasons;
* the required actions;
* the protection task;
* an append-only event list;
* the spec version and hash.

Two identical runs give identical records.
