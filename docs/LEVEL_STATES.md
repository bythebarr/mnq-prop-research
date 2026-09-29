# Level States: how a zone's interaction history works (Round 9, amended)

This explains, in plain English, how price interacts with a structural
**zone** (a cluster of nearby levels; see Round 8). The rules live in
`configs/rule_freeze_v1.yaml` (`acceptance_rejection_breakout`,
`level_states`). The code is `src/mnq_research/level_states.py`, and the
tests are `tests/test_level_states.py`.

> Status: part of a **DRAFT** specification. D-025 is confirmed. This logic
> records history only. Continuation confirmation is in `CONFIRMATION.md`.
> There is no entry, stop or target logic yet.

## The core idea: location is not an attempt

Price sitting above or below a zone is just **location**. It is not a
breach, and it is not acceptance. Something only happens *to* a zone
during a **directional attempt** (an "interaction episode"), and an attempt
must be set up first:

```
arm (clear-side close)  →  a LATER bar touches / gaps (approach alone = observation only)  →  attempt starts
                                                                        ↓
                         accepted (2 qualifying closes)  or  rejected (within 3 bars)
                         or ended (interruption, blackout, window end, rearm after expiry)
```

Terms: **L / U** are the zone's lower and upper boundaries. The **tick** is
0.25 points. The **clear-side distance** is 0.50 points in B0.

## 1. Arming

| Arm | Condition (complete, eligible five-minute bar) |
|---|---|
| `ARMED_FROM_BELOW` | close ≤ L − 0.50 |
| `ARMED_FROM_ABOVE` | close ≥ U + 0.50 |

Arming only means "price is clearly established on this side." It is not an
entry signal. **The bar that arms can never also start an attempt**,
because a five-minute bar can't show which happened first.

## 2. Approaches and attempts (D-026)

An **approach** (coming within the proximity tolerance of an armed zone
without touching it) is only an *observation*. It gets an
`approach_sequence_id` and a status: ACTIVE_APPROACH, APPROACH_WITHDRAWN,
APPROACH_CONVERTED_TO_ATTEMPT or APPROACH_INVALIDATED. It **never** gets an
`attempt_id`, never starts the rejection clock, and never counts as an
attempt.

| Attempt | Needs |
|---|---|
| UPWARD | armed from below, then a *later* bar that **touches**, or makes a **qualifying gap** above |
| DOWNWARD | armed from above, then a *later* bar that **touches**, or makes a **qualifying gap** below |

A directional breach always includes a touch unless the bar jumped the whole
zone, and a jump counts only if it is a qualifying gap (D-027). Starting an
attempt uses up the arm, creates a new `attempt_id`, starts the rejection
clock, and converts any active approach. If price closes back at the
origin-side threshold before any interaction, the approach is **withdrawn**.
The arm is kept and no attempt is created. Diagnostics report approach and
attempt counts separately.

**Qualifying gap above:** the previous close was ≤ L − 0.50 and this bar's
low is above U. `GAPPED_ABOVE_ZONE` is recorded, and no touch is ever
invented. The gap below mirrors this.

## 3. During an attempt

| Event | Decided by | Rule (B0) |
|---|---|---|
| Touch | wick | Range meets [L, U] (equality counts) |
| Breach | wick | **Only in the attempt's direction**: UPWARD needs high ≥ U + 0.25, DOWNWARD needs low ≤ L − 0.25 |
| Two-sided breach | wick | Both extremes beyond. Always `TWO_SIDED_BREACH` (order unknown, needs finer data). Only the attempt's own direction is also recorded as a breach |
| Acceptance | **close** | 2 consecutive closes ≥ U + 0.50 (UPWARD) or ≤ L − 0.50 (DOWNWARD). Only the attempt's direction counts |
| Rejection | **close** | Within 3 complete bars of the first touch, breach or gap: a close back ≥ 0.50 on the origin side |

A breakout is not a separate event; it's the plain-language name for
acceptance.

**Delayed acceptance:** the 3-bar window only limits *rejection*. The
attempt stays alive after the window expires, so acceptance can still come
later.

## 4. How an attempt ends

* **Accepted** in its direction.
* **Rejected** within the window. The rejection close may rearm the zone.
* **Rearmed after expiry:** once the window has expired, a clear-side close
  back on the origin side ends the attempt and rearms.
* **Interruption:** an incomplete or missing decision bar, or a news
  blackout. **All executable state is removed** (arm, attempt, counters,
  origin), the history is kept, and a fresh arm is needed afterwards.
* The entry window closes (11:30), or the zone expires.

A new attempt in the same direction **always needs a fresh arm**, followed
by a later bar. Touching the zone again isn't enough.

## 5. Start of day

* **Pre-open arming (D-027):** prior-day and overnight zones may be *armed*
  by the completed 09:25–09:30 bar: a close ≤ L − 0.50 arms from below, and a
  close ≥ U + 0.50 arms from above. That bar is used for nothing else. If it
  is missing or incomplete, the zone starts unarmed. The earliest replay
  acceptance is therefore 09:40.

* **Prior-day and overnight zones** exist at 09:30. The three bars
  09:30–09:45 are replayed under the full rules (arming, attempts,
  acceptance and rejection are all possible). The replay events are marked
  `initialization_replay` and **can never produce an entry**, because new
  entries start at 09:45.
* **Opening-range zones** start at 09:45 with no arm and no attempt. A
  post-09:45 bar must arm first, and only a later bar can start an attempt.
  The bars that built the opening range are never used against it.
* If an opening-range level merges with an earlier zone at 09:45, a **new
  zone version** is created. The old version is archived with its history
  and links, and none of its state carries over.

## 6. Current state versus history

The history is **append-only**. `current_state` summarises the latest bar
using this priority, among events valid within the active attempt:

acceptance → rejection → two-sided breach → breach → touch → approach → arm → untouched.

## Confirmed interpretations (D-025)

1. **No arming during an active attempt.** Opposite-side arming is evaluated
   only after the current attempt ends. An attempt can't be "stolen" by an
   opposite approach created by its own successful move. The former ending
   "an opposite episode arms and begins" has been removed.
2. **Approaches are not attempts** (replaced by D-026): see section 2.
3. **Every acceptance** gets a unique `acceptance_id`, which confirmation
   refers to.
