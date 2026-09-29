# Direction: where long and short come from (Round 11)

Plain-English companion to `configs/rule_freeze_v1.yaml` (`market_structure`,
`direction`). The code is `src/mnq_research/direction.py`, and the tests are
`tests/test_direction.py`.

> DRAFT component. It creates *direction candidates* only: no orders, no
> entry selection, no stops or targets.

## No bias, no extra filter

B0 starts every morning **with no opinion**. There is no swing or trend
filter (`additional_swing_structure_filter: NOT_APPLICABLE`), and nothing
from an EMA, VWAP, overnight direction, opening gap or prior close. Market
structure *is* the rule sequence already defined:

arm → interaction → acceptance → pullback-hold → continuation close
(`ACCEPTANCE_PULLBACK_HOLD_CONTINUATION`).

## Where a candidate comes from

A **LONG** candidate exists only when a `CONFIRMED_LONG_CONTINUATION`
completes, and the trading date, contract, zone version, attempt and
acceptance IDs all belong together. **SHORT** is the mirror. If any link
doesn't match, the code refuses (`LinkageError`).

| Confirmation completes… | Result |
|---|---|
| before 09:45 | `NON_EXECUTABLE_BEFORE_WINDOW`: history only, never carried forward |
| from 09:45 up to, but not including, 11:30 | `ENTRY_CANDIDATE` |
| at or after 11:30 | Impossible: the confirmation is already `INVALIDATED_BY_CUTOFF` |

## Opposite confirmations at the same moment

If a long and a short confirmation complete at the **same decision time**,
the result is `NO_TRADE_DIRECTIONAL_CONFLICT`:
- Every conflicting confirmation becomes non-executable. All are kept for
  diagnostics.
- **New entries halt for the rest of the trading date.** No later setup can
  restore eligibility that day.
- The outcome depends only on *which* confirmations exist, never on the order
  they were processed. Distance, zone size and hypothetical profit play no
  part.

## Several same-direction confirmations

They are all recorded as candidates and marked `requires_selection`. Nothing
is chosen yet; the entry and room-to-target round decides.

## Open item (D-027)

A conflict made up of confirmations completing *before* 09:45 also halts the
day. This was implemented conservatively.
