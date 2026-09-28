# Level States: how a zone's interaction history works (Round 9)

This explains, in plain English, how price interacts with a structural
**zone** (a cluster of nearby levels; see Round 8). The rules live in
`configs/rule_freeze_v1.yaml` (`acceptance_rejection_breakout`,
`level_states`). The code is `src/mnq_research/level_states.py`, and the
tests are `tests/test_level_states.py`.

> Status: part of a **DRAFT** specification. Three definitions await an
> owner decision (D-024). This logic records history only. It does not
> confirm setups, enter, stop or target.

## Words used

* **L / U:** the zone's lower and upper boundaries. **Tick:** 0.25 points.
* **Decision bar:** a *complete, eligible* five-minute bar. Only these can
  change a zone's state.
* **Wick:** a bar's high or low. **Close:** a bar's last price.

## What wicks decide, and what closes decide

| Event | Decided by | B0 rule (candidate parameters) |
|---|---|---|
| Approached | wick | Within today's proximity tolerance of the zone, but not touching |
| Touched | wick | The bar's range meets [L, U] (equality counts) |
| Breached | wick | A wick at least 1 tick beyond a boundary (0.25 points) |
| Two-sided breach | wick | Both breaches on one bar. Flagged: intrabar order unknown, needs finer-data replay |
| Accepted above / below | **close** | 2 consecutive closes at least 2 ticks (0.50) beyond the boundary |
| Rejected attempt | **close** | Within 3 complete bars of an attempt, a close at least 0.50 back on the origin side |

A touch, a breach or a single close is **never** acceptance. A
**breakout** is not a separate event: it's the plain-language name for
acceptance.

## Origin (which side price came from)

The origin is the side of the most recent close that was *clearly* outside
the zone: at least 0.50 below L, or at least 0.50 above U. With no such
close, the origin is **UNKNOWN**. In that case touches, breaches and
acceptance are still recorded, but **no rejection** can be assigned. The
origin is never guessed from candle colour or wick order.

## Things that interrupt a sequence

| Interruption | Effect |
|---|---|
| Incomplete decision bar | No transition. Acceptance counters reset. A pending rejection window is invalidated. Recorded as `DATA_INTERRUPTION` |
| A decision bar missing from the sequence | Same as an incomplete bar. It can never be "skipped over" |
| News blackout start | Counters reset, pending windows invalidated, and the zone starts fresh (Round 7b) |
| Trading window closes (11:30) | A pending rejection window is invalidated |

## Gaps

If price jumps completely over a zone, from a close below L to a bar whose
low is above U, `GAPPED_ABOVE_ZONE` is recorded, and acceptance can follow
without a touch. No touch is ever invented at a price that didn't trade.

## Start of day

* Zones made only of prior-day and overnight levels exist at **09:30**. The
  three bars 09:30–09:45 are replayed to set their starting state. These
  replay events are marked `initialization_replay` and can never produce an
  entry (new entries start at 09:45).
* Zones containing an **opening-range** level start at **09:45** as
  UNTOUCHED. The bars that *built* the opening range are never used to say
  it was touched.
* If an opening-range level merges with an earlier zone at 09:45, a **new
  zone version** is created. The old version and its history are archived
  and linked, and none of its state carries over.

## Current state versus history

Each zone keeps an **append-only** event history. `current_state` is only a
summary. When one bar causes several events, the state follows this
priority:

acceptance → rejection → two-sided breach → breach → touch → approach → untouched.

Nothing in the history is ever erased, including an earlier acceptance
followed by a later opposite one.

## Open questions (D-024)

1. **Breach without crossing.** Taken literally, any bar lying wholly below a
   zone is a "downward breach", because its low is more than a tick below L.
   Should a breach require price to come from, or touch, the zone first?
2. **Acceptance without crossing.** Taken literally, two closes below L − 0.50
   "accept below" a zone even if price never came from above it. Should
   acceptance require a prior position on the other side, a touch, or a gap
   across?
3. **New attempt after an expired window.** If price stays inside the zone
   and then touches again, does that start a new attempt?
