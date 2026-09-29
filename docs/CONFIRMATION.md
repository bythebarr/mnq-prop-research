# Confirmation: from acceptance to a confirmed continuation (Round 10)

Plain-English companion to `configs/rule_freeze_v1.yaml` (`confirmation`).
The code is `src/mnq_research/confirmation.py`, and the tests are
`tests/test_confirmation.py`.

> Status: part of a **DRAFT** specification. A confirmation is **only a
> recorded event**. It does not place an order, and it has no entry price,
> size, stop or target. Those are later rounds. D-026 is confirmed.
> Direction rules are in `DIRECTION.md`.

## The single B0 pattern: PULLBACK_HOLD_CONTINUATION

Price has to *accept* beyond a zone, *pull back and hold*, and then
*continue*. Acceptance followed only by more extension is **not** enough:
B0 does not chase a move that never offers a pullback.

Zone boundaries are **L / U**. The tick is 0.25. The *retest distance* is
the day's proximity tolerance (at least 2 points).

### Long (after ACCEPTED_ABOVE)

| Step | Bar | Condition |
|---|---|---|
| 0 | Acceptance bar | Round 9 acceptance. Can **never** double as the hold bar |
| 1 | Retest-hold (the first such bar) | low ≤ U + retest distance, low **> L − 0.25** (so the lowest allowed low is L itself), and close ≥ U + 0.50 |
| 2 | The **very next** bar | close ≥ retest-hold high + 0.25 → `CONFIRMED_LONG_CONTINUATION` |

The short side is the mirror image. The retest-hold has high ≥ L − retest
distance, high **< U + 0.25** (so the highest allowed high is U) and close ≤
L − 0.50. The continuation needs a
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

## Confirmed details (D-026)

1. **No overlap at the far boundary.** One tick through the far side (a
   print at L − 0.25 for longs, or U + 0.25 for shorts) always **fails**.
   Equality belongs to the failure rule, never the hold rule.
2. **Replay acceptances** (prior-day and overnight zones, 09:30–09:45) do
   start confirmation, from their real timestamp. The six-bar clock is never
   reset at 09:45. A confirmation completed before 09:45 is historical and
   non-executable, and it is not carried forward. A confirmation completing
   at 09:45 or later may become a candidate.
3. **The cutoff** is checked at the decision bar's close. A continuation
   closing at 11:30 is invalidated; one closing at 11:25 may proceed to the
   entry round.

**Note on timing.** A zone is initialised at 09:30 with no arm. It needs an
arming bar, then an interaction bar, then a second acceptance close, so the
earliest possible replay acceptance closes at **09:45** (D-027).
