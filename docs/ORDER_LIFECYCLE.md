# Order Lifecycle: from a selected candidate to a filled (or not) entry (Round 13)

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
| ENTRY_FULLY_FILLED | average fill, each fill, slippage vs the confirmation close and vs the planned entry, and latency to first and to final fill are recorded | no, but the open position blocks new entries |
| ENTRY_PARTIALLY_FILLED | the filled part is kept and protected; the rest is cancelled; never topped up; stop and target unchanged | **yes** |
| ENTRY_NOT_FILLED | no position; candidate used up | **yes** |
| ENTRY_ORDER_REJECTED | code and message recorded; no retry, no other order type | **yes** |
| ENTRY_ORDER_STATE_UNKNOWN | never resubmit or replace; query order and position; no added exposure; protect what is confirmed; critical alert | **yes** |
| NOT_SUBMITTED_INELIGIBLE | exact reasons recorded; candidate used up | no |

**The unknown state** arises from any of these:
* no acknowledgement (and no fill) within 2.000 s;
* loss of reliable state;
* contradictory reports: an overfill, a fill after the final state, a
  rejection after a fill, or a reconciled position that differs from the
  recorded fills.

It is never assumed to be a rejection, and it is never silently replaced by
a cleaner outcome.

**Cancellation race:** a fill that arrives after the cancel request is real
exposure. It is protected and reconciled, and there is no new entry that day.

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
* The stop is **never** resized, compressed or recalculated. If a fill lands
  at or beyond the stop, the protection task is flagged
  `stop_protective_of_actual_entry: false` and the stop stays where it was.

## Quantity and risk

* The quantity is **1 contract**, labelled `RESEARCH_QUANTITY_ONLY` and
  `NOT_DEPLOYMENT_SIZING`. It exists so the lifecycle can be tested. It is
  not a sizing decision. (With 1 contract a partial fill cannot happen; the
  partial-fill rules are tested with a larger test-only quantity.)
* `risk_per_trade` is deliberately **unresolved** until evidence exists. The
  later formula:
  * risk per contract = stop points × $2 + modelled slippage and costs;
  * contracts = floor(allowed risk ÷ risk per contract);
  * zero contracts → skip;
  * never compress the stop.
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
