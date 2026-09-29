# Confirmation: from acceptance to a confirmed continuation (Round 10)

Plain-English companion to `configs/rule_freeze_v1.yaml` (`confirmation`).
The code is `src/mnq_research/confirmation.py`, and the tests are
`tests/test_confirmation.py`.

> Status: part of a **DRAFT** specification. A confirmation is **only a
> recorded event**. It does not place an order, and it has no entry price,
> size, stop or target. Those are later rounds. Open items: D-026.

## The single B0 pattern: PULLBACK_HOLD_CONTINUATION

Price has to *accept* beyond a zone, *pull back and hold*, and then
*continue*. Acceptance followed only by more extension is **not** enough:
B0 does not chase a move that never offers a pullback.

Zone boundaries are **L / U**. The tick is 0.25. The *retest distance* is
the day's proximity tolerance (at least 2 points).

### Long (after ACCEPTED_ABOVE)

| Step | Bar | Condition (all inclusive) |
|---|---|---|
| 0 | Acceptance bar | Round 9 acceptance. Can **never** double as the hold bar |
| 1 | Retest-hold (the first such bar) | low ≤ U + retest distance, low ≥ L − 0.25, and close ≥ U + 0.50 |
| 2 | The **very next** bar | close ≥ retest-hold high + 0.25 → `CONFIRMED_LONG_CONTINUATION` |

The short side is the mirror image. The retest-hold has high ≥ L − retest
distance, high ≤ U + 0.25 and close ≤ L − 0.50. The continuation needs a
close ≤ hold low − 0.25.

A **wick** beyond the hold bar's extreme is never enough; it must be a
**close**. The retest may dip inside the zone, as long as it doesn't go a
full tick through the far side.

## The clock: six bars

The acceptance bar is bar 0. Confirmation must finish by the close of
**bar 6**, which is 30 minutes under normal bars. The hold therefore has to
come by **bar 5**, since a hold on bar 6 would have no room for its
continuation bar.

## One acceptance, one outcome

Each acceptance ends in exactly one outcome, reached once and never revised:

| Outcome | When |
|---|---|
| CONFIRMED | The hold, then an immediate qualifying continuation close |
| FAILED | Before the hold: a close back at or through the near boundary, a one-tick wick through the far boundary, or an opposite acceptance. After the hold: the next bar doesn't close beyond the hold extreme, or wicks through the far boundary |
| TIMED_OUT | Bar 6 passes without a confirmation |
| INVALIDATED_BY_INTERRUPTION | A missing or incomplete decision bar |
| INVALIDATED_BY_BLACKOUT | A bar overlaps a news blackout |
| INVALIDATED_BY_CUTOFF | The 11:30 new-entry cutoff is reached |
| INVALIDATED_BY_ZONE_CHANGE | The zone expires or gets a new version |

Only the **first** qualifying hold counts. If the bar after it fails, that
acceptance is finished. There is no hunting for a later pair that happens
to work.

## After a failure

The zone isn't retired for the day, but nothing is reused. A new setup
needs the **whole process again**: a fresh arm, a new attempt (new
`attempt_id`), a new acceptance (new `acceptance_id`) and its own
six-bar clock. Every confirmation event carries exactly one
`acceptance_id`, `attempt_id` and zone version, and they can never be mixed.

## Open items (D-026)

1. **Equality conflict:** a low of exactly L − 0.25 (long) meets both "hold:
   low ≥ L − 0.25" and "failure: trades at or below L − 0.25". It is
   implemented as a **failure**.
2. **Acceptances during the 09:30–09:45 replay** aren't followed by
   confirmation. Confirmation tracking only starts for acceptances observed
   from 09:45 on.
