# Trade Geometry: entry, stop, target and selection (Round 12)

Plain-English companion to `configs/rule_freeze_v1.yaml` (`trade_geometry`,
`room_to_target`, `stop_placement`, `target_placement`). The code is
`src/mnq_research/trade_geometry.py`, and the tests are
`tests/test_trade_geometry.py`.

> DRAFT component. It computes a **planning geometry only**. It submits no
> order, sizes no position, simulates no fill and applies no costs.

## The geometry (long shown; short mirrors)

For an origin zone with boundaries L to U, a confirmation close `C`, and the
next zone ahead with boundaries `L2` to `U2`:

| Item | Long rule | Worked example (zone 20000–20004, C = 20010) |
|---|---|---|
| Planned entry | C + 0.25 (one adverse tick) | 20010.25 |
| Structural stop | L − 0.25 (one tick beyond the far side) | 19999.75 |
| Planned risk | entry − stop | 10.50 |
| Target zone | the **nearest** distinct active zone with lower boundary strictly above the entry | lower 20026.25 |
| Planned target | L2 − 0.25 (one tick before it) | 20026.00 |
| Planned reward | target − entry | 15.75 |
| Gross R:R | reward ÷ risk; must be **≥ 1.50** | exactly 1.50: qualifies |

Rules that protect honesty:
* **The planned entry isn't a fill.** Real results and R must use the actual
  fill, recorded later.
* **The stop is never squeezed** to fit a dollar amount or a contract count.
  There is simply no way to pass one in.
* **The nearest zone is always the target.** A farther, more profitable zone
  is never substituted. With no zone ahead, there is no trade: no fixed
  target is invented.
* **The ratio is gross**, before commissions and slippage. The cost fields
  are reserved until the cost model is frozen.
* **All arithmetic is exact:** decimals on the 0.25 grid, and exact
  fractions for R:R. Floating-point rounding never decides a threshold or a
  tie.

## Choosing among same-direction candidates (D-028)

At one decision time, keep only the room-qualified candidates. Then:
1. Pick the highest gross R:R.
2. Then the smallest planned risk.

That is the whole list. A third criterion, "greatest reward", was removed:
with equal R:R and equal risk, reward is equal too, so it could never decide
anything. The list is never reordered to prefer bigger rewards.

An **exact tie on both** means **no trade at that timestamp**
(`NO_TRADE_SAME_DIRECTION_GEOMETRY_TIE`). The tied confirmations are used up,
but the day is **not** halted. Input order, names and IDs never decide.

Lower-ranked candidates become **terminal** with the reason
`NOT_SELECTED_BY_GEOMETRY_RANKING`. They can never come back through the same
confirmation, acceptance or attempt.

The winner becomes a `SELECTED_ENTRY_CANDIDATE`. It carries every identifier
and planned price, the confirmation close and origin-zone boundaries, the spec
version and the spec hash. It is still **not an order**; the entry order is
described in `ORDER_LIFECYCLE.md`.

## Eligibility is typed and fails closed (D-028)

Geometry no longer accepts a free-form list of "blocking conditions". It takes
an `ExecutionEligibility` snapshot (`src/mnq_research/eligibility.py`), which
has one field per control: news blackout, news protection, safety halt,
directional-conflict halt, daily entry halt, session, data, candidate,
open position, working order, contract and zones.

* Each field must be `ControlState.CLEAR`, `BLOCKED` or `UNKNOWN`.
* Only CLEAR on **every** field lets a candidate through.
* The plain string `"CLEAR"`, `True`/`False`, `None` or a missing field is
  refused. When the snapshot is built from a mapping, a missing or wrongly
  typed entry becomes `UNKNOWN`, which blocks.

The real producers of these states (the news calendar, safety monitor, data
checks) are not built yet. So `execution_eligibility_integration.status` is
`REQUIRED_BEFORE_EXECUTABLE`, and the rule check keeps the spec
non-executable until they are integrated.

Target zones must already exist at the decision time. For example, an
opening-range zone can be a target only from 09:45 (confirmed in D-028).
