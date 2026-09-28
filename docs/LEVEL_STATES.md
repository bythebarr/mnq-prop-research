# Level States: how a zone's interaction history works (Round 9, amended)

This explains, in plain English, how price interacts with a structural
**zone** (a cluster of nearby levels; see Round 8). The rules live in
`configs/rule_freeze_v1.yaml` (`acceptance_rejection_breakout`,
`level_states`). The code is `src/mnq_research/level_states.py`, and the
tests are `tests/test_level_states.py`.

> Status: part of a **DRAFT** specification. Two interpretations await the
> owner's confirmation (D-025). This logic records history only. It does
> not confirm setups, enter, stop or target.

## The core idea: location is not an attempt

Price sitting above or below a zone is just **location**. It is not a
breach, and it is not acceptance. Something only happens *to* a zone
during a **directional attempt** (an "interaction episode"), and an attempt
must be set up first:

```
arm (clear-side close)  →  a LATER bar approaches / touches / gaps  →  attempt starts
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

## 2. Starting an attempt

| Attempt | Needs |
|---|---|
| UPWARD | armed from below, then a *later* bar that approaches from below, touches, or gaps above |
| DOWNWARD | armed from above, then a *later* bar that approaches from above, touches, or gaps below |

Starting an attempt uses up the arm. Every attempt gets a new
`attempt_id`.

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

## Interpretations awaiting confirmation (D-025)

1. **No arming during an active attempt.** During an upward attempt, the
   first close at or above U + 0.50 also meets the "armed from above"
   condition. If that armed the zone, the next bar hovering just above the
   zone would count as an *approach from above*, start a downward attempt,
   and cancel the upward one. Two-close acceptance would then almost never
   happen. So arming is recorded only while no attempt is active. The side
   effect is that the "opposite episode begins" ending can't occur in B0.
2. **Attempts started by an approach.** Their rejection window opens at the
   first touch, breach or gap. Before that, a clear-side close on the origin
   side neither rejects nor ends the attempt.
