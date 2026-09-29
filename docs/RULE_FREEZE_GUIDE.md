# Rule Freeze Guide — the questions that must be answered before any code trades

> **Every example in this document is ILLUSTRATIVE ONLY. None is a
> recommendation, a default, or an accepted rule.** Examples exist only to
> show the *kind* and *precision* of answer required. Copying an example
> without deciding it deliberately defeats the purpose of the rule freeze.

## How to use this guide

`configs/rule_freeze_v1.yaml` is a blank template. Each `TBD` in it matches
one question below (same `section.field` name). Answer the questions **in the
order given**: they are ordered by dependency. For example, you cannot define
a session window until you know which time zone the times are written in.

A good answer is one that **two different programmers would code identically
without asking you anything**. If an answer contains words like "strong",
"clean", "near", "usually", "looks like" or "if it feels right", it is not
finished yet: each such word needs a measurable definition.

Formatting rules for answers in the YAML file:

* Put clock times in quotes: `"09:30"` (unquoted YAML times can be misread).
* Write `NONE` or `NOT_APPLICABLE` when a field deliberately does not apply.
  Never leave it empty. Empty, `null` and `TBD` all count as unanswered.
* Long answers can use YAML's folded style (`>`) over several lines.

Checking progress at any time:

```bash
uv run mnq rules check
```

The command lists **every** unanswered field at once.

When every field is answered, approve the spec like this. Set
`specification.status: FROZEN_APPROVED` and run `uv run mnq rules check` again.
It prints a **Spec hash**. Then fill in `approval_record`: `approved: true`,
your name, the UTC time, and that hash. Any later edit changes the hash and
blocks execution again until you re-approve. That is deliberate.

---

## 1. Specification identity

### `specification.spec_id`
**Fixed:** `MNQ-RULE-FREEZE`. Identifies this family of specifications. It
does not need an answer.

### `specification.version`
**Question:** Which version is this? Start at `1.0` when the first complete
version is approved. Any later change becomes `1.1`, `2.0` and so on, and is
a **new experiment**.
**Illustrative only:** `"1.0"`.

### `specification.strategy_name`
**Question:** What is the strategy's short name?
**Illustrative only:** `"Example Level Reaction Strategy"`.

### `specification.author`
**Question:** Who defined these rules, and who is accountable for them?
**Illustrative only:** `"B. Barr"`.

### `specification.plain_english_summary`
**Question:** In one paragraph, what does the strategy do? Describe what it
waits for, which way it trades, how it enters, how it exits, and when it
stops for the day.
**Why it matters:** Every other answer must be consistent with this
paragraph. It is also the reference for spotting "rule drift" later.
**Illustrative only:** "During a defined morning window, wait for price to
test a pre-defined structural level, require a measurable rejection of that
level, enter in the rejection direction with a stop beyond the level, target
a fixed multiple of risk, and stop after a fixed number of trades."

## 2. Instrument and contract selection

### `instrument.root_symbol`
**Fixed:** `MNQ`, as stated in the project brief.

### `instrument.exchange`
**Question:** On which exchange and platform is the contract traded?
**Illustrative only:** `"CME Globex"`.

### `instrument.tick_size_points`
**Question:** What is the minimum price increment, in index points? Please
confirm it against the exchange's published contract specification. Don't
rely on memory, including mine.
**Illustrative only:** `0.25`.

### `instrument.tick_value_usd`
**Question:** How many US dollars is one tick worth for one contract? Please
confirm it from the exchange specification.
**Illustrative only:** `0.50`.

### `instrument.point_value_usd`
**Question:** How many US dollars is one full index point worth for one
contract?
**Illustrative only:** `2.00`.

### `instrument.contract_selection_method`
**Question:** On any given date, exactly which contract month is traded?
**Why it matters:** Several contract months trade at the same time. Their
prices differ, and so do their volume and liquidity.
**Illustrative only:** "The front quarterly contract (H, M, U, Z) until the
roll date defined in `contract_roll`, then the next quarterly contract."

## 3. Source data

### `source_data.vendor`
**Question:** Which data provider supplies the historical one-minute bars?
**Illustrative only:** `"<vendor name>"`. This project does not choose or
pay for a vendor. That decision is yours.

### `source_data.dataset_name`
**Question:** Which exact product or dataset from that vendor? Is it
per-contract or a continuous series?
**Illustrative only:** `"MNQ 1-minute OHLCV, individual contracts"`.

### `source_data.base_bar_interval`
**Fixed:** `1min`, by the data contract.

### `source_data.vendor_timestamp_convention`
**Question:** In the vendor's raw files, does a bar's timestamp mark the
**start** of the minute or its **end**?
**Why it matters:** If this is wrong, every bar is shifted by one minute. The
backtest would then "see" each bar one minute before it had closed. That is
look-ahead bias, and it silently inflates results. Our data contract stores
bar **start** times, so vendor data must be converted if necessary.
**Illustrative only:** `"bar_start"`.

### `source_data.vendor_timezone`
**Question:** What time zone are the vendor's raw timestamps written in? Use
an official IANA zone name.
**Illustrative only:** `"UTC"` or `"America/Chicago"`.

### `source_data.history_start_date`
**Question:** What is the first date of history that will be acquired?
**Illustrative only:** `2019-05-06`.

### `source_data.history_end_date`
**Question:** What is the last date of history that will be acquired?
**Illustrative only:** `2025-12-31`.

### `source_data.higher_resolution_data_available`
**Question:** Will tick-level or one-second data be available to resolve
same-bar ambiguity? See the `intrabar_ambiguity` section. Answer yes or no,
and say for which dates.
**Illustrative only:** `"no"`, or `"yes, ticks from 2022-01-01"`.

### `source_data.required_fields`
**Fixed** by the data contract (`docs/DATA_CONTRACT.md`). It does not need
an answer.

## 4. Time-zone policy

### `timezone_policy.storage_timezone`
**Fixed:** `UTC`. All stored timestamps are in UTC.

### `timezone_policy.exchange_timezone`
**Fixed:** `America/Chicago`. This is the CME's local time, used for the
trading date.

### `timezone_policy.rule_timezone`
**Question:** Which time zone are the clock times in **your rules** written
in? This covers the session windows, flatten time, news windows and so on.
**Why it matters:** "09:30" means different instants in New York and
Chicago. Stating the zone once removes the ambiguity everywhere.
**Illustrative only:** `"America/New_York"`.

### `timezone_policy.dst_handling`
**Question:** Your rule times are written in the rule time zone, which
changes its clock for daylight saving. Should the rules follow that local
clock through daylight-saving changes? For example, should a "09:30" rule
stay at 09:30 local time all year?
**Why it matters:** The US and Europe change their clocks on different
dates. For a few weeks each year, the same local time lines up with
different market events.
**Illustrative only:** "Rules follow local wall-clock time in the rule time
zone all year."

## 5. Session definitions

### `sessions.trading_window_start`
**Question:** At what clock time, in the rule time zone, may the strategy
first **enter** a trade each day?
**Illustrative only:** `"09:35"`.

### `sessions.trading_window_end`
**Question:** At what clock time does the entry window close? This is
distinct from the forced flatten time in `session_flattening`.
**Illustrative only:** `"11:30"`.

### `sessions.reference_windows`
**Question:** Which named time windows are used to **measure** things? For
example, an overnight high and low, or an opening range. Give the exact
start and end for each, and say whether each end is inclusive or exclusive.
**Why it matters:** Levels computed over slightly different windows give
different trades.
**Illustrative only:** "overnight = 18:00 previous day to 09:29 (inclusive
bar starts); opening_range = 09:30 to 09:44."

### `sessions.early_close_handling`
**Question:** What happens on days when the exchange closes early?
**Illustrative only:** "No trading on scheduled early-close days."

## 6. Decision-clock timing

### `decision_clock.decision_bar_interval`
**Question:** Which bar length do decisions use? For example, one-minute
bars, or five-minute bars built from one-minute bars. If bars are built
from smaller ones, how are they aligned?
**Illustrative only:** "5-minute bars built from 1-minute bars, aligned to
:00, :05, :10 and so on in the rule time zone."

### `decision_clock.decision_point`
**Question:** At what exact moment is a decision made? For example, at the
close of a decision bar, or as soon as price touches a level?
**Why it matters:** Deciding at bar close uses only completed information.
Deciding intrabar needs finer data to simulate honestly.
**Illustrative only:** "Only at the close of each decision bar."

### `decision_clock.signal_to_order_delay`
**Question:** How long after the decision is the order assumed to reach the
market?
**Illustrative only:** "Order submitted at the open of the next one-minute
bar."

### `decision_clock.bar_aggregation_rule`
*(Added in Round 5.)*
**Question:** When a longer decision bar is built from one-minute bars, what
happens if some of its one-minute bars are absent? Databento prints no bar
for a minute with no trades.
**Why it matters:** A bar built from fewer minutes than expected can look
like ordinary data, but it can create or hide a signal.
**Illustrative only:** "Decision-eligible only when all component minutes
are present. Otherwise, diagnostics only."

## 7. Eligible trading dates and contract roll

### `eligible_trading_dates.allowed_weekdays`
**Question:** Which days of the week may the strategy trade?
**Illustrative only:** `[MON, TUE, WED, THU, FRI]`.

### `eligible_trading_dates.holiday_and_half_day_policy`
**Question:** Does the strategy trade on exchange holidays, half days, and
the days just before or after them?
**Illustrative only:** "No trading on half days or on the day after
Thanksgiving."

### `eligible_trading_dates.roll_week_policy`
**Question:** Does the strategy trade during the contract-roll period?
**Illustrative only:** "Trades normally, using the contract chosen by
`contract_roll`."

### `eligible_trading_dates.calendar_source`
**Question:** Which calendar says which days are holidays or half days?
**Illustrative only:** "The CME holiday calendar, saved as a dated file in
the repo."

### `contract_roll.roll_trigger`
**Question:** What event decides when to switch to the next contract?
**Illustrative only:** "A fixed number of business days before expiry", or
"when next-contract volume first exceeds front-contract volume".

### `contract_roll.roll_timing`
**Question:** At exactly which point in time does the switch happen?
**Illustrative only:** "At the start of the trading date after the trigger
fires."

### `contract_roll.price_series_adjustment`
**Question:** Are historical prices used as traded (unadjusted,
per-contract), or joined into a back-adjusted continuous series? If
adjusted, which method?
**Why it matters:** Back-adjustment changes historical price levels. That
distorts any rule that uses absolute prices or round numbers.
**Illustrative only:** "Unadjusted, per-contract prices. No continuous series
is used for signals."

### `contract_roll.open_position_at_roll`
**Question:** What happens to an open position when the roll happens?
**Illustrative only:** "Not applicable: all positions are flat every day
before the roll time."

## 8. Structural levels and price-behaviour definitions

### `structural_levels.level_types`
**Question:** Which kinds of price level does the strategy use? List every
one.
**Illustrative only:** "prior-day high, prior-day low, overnight high,
overnight low".

### `structural_levels.calculation_method`
**Question:** For each level type, what is the exact formula? Say which bars
are used, whether highs and lows or closes are used, and which session
window applies.
**Illustrative only:** "overnight high = maximum `high` of one-minute bars
starting in the `overnight` reference window."

### `structural_levels.level_expiry`
**Question:** When does a level stop being valid? For example, at the end of
the day, after it is broken, or after a number of touches.
**Illustrative only:** "Valid until the trading window ends, or until the
first breakout, whichever comes first."

### `structural_levels.level_proximity_tolerance`
**Question:** How close must price be to count as "at" or "testing" a
level? Give the answer in ticks or points.
**Illustrative only:** "Within 2 ticks."

### `structural_levels.missing_data_treatment`
*(Added in Round 8.)*
**Question:** How do absent one-minute bars affect each level? Distinguish
verified no-trade minutes, known data outages and unexplained gaps.
**Illustrative only:** "A verified no-trade minute doesn't invalidate a
high or low. Any outage makes the level unavailable."

### `structural_levels.price_validation`
*(Added in Round 8.)*
**Question:** What must every level satisfy before it can be used? For
example, a finite positive price, on the tick grid, knowable by the decision
time, with its source recorded.
**Illustrative only:** "A finite positive price on the 0.25 grid, with its
source window and contract recorded."

### `structural_levels.clustering_method`
*(Added in Round 8.)*
**Question:** How are nearby levels grouped into one zone, and what is kept
for each zone?
**Illustrative only:** "Sort by price. Join neighbours within tolerance,
transitively. Keep every constituent level."

### `structural_levels.decision_use`
*(Added in Round 8.)*
**Question:** How do later rules measure distances to a zone, so that room
and risk are never made to look better than they are?
**Illustrative only:** "A long measures to a resistance zone's lower edge."

### `market_structure.definition`
*(Added in Round 11.)*
**Question:** What sequence defines the strategy's market structure? Is it
an existing rule sequence, or a separate swing/trend filter?
**Illustrative only:** `ACCEPTANCE_PULLBACK_HOLD_CONTINUATION`.

### `market_structure.additional_swing_structure_filter`
*(Added in Round 11.)*
**Question:** Is any additional swing, pivot or trend filter active? If
not, write `NOT_APPLICABLE`.
**Illustrative only:** `NOT_APPLICABLE`.

### `market_structure.structure_bar_interval`
*(Added in Round 2 for "developing market structure" in the summary.)*
**Question:** Which bar length is market structure judged on?
**Why it matters:** Swings on one-minute bars and on 15-minute bars are
different structures. The same market can be "bullish" on one and "mixed"
on the other.
**Illustrative only:** `"5min"`.

### `market_structure.swing_point_definition`
**Question:** What exactly is a swing high and a swing low? For example, a
bar whose high is higher than the highs of N bars on each side. Give N. Say
whether ties count. Say when a swing becomes *known*.
**Why it matters:** A swing that needs N bars *after* it to be confirmed is
only known N bars later. Using it earlier is look-ahead bias.
**Illustrative only:** "A swing high is a bar whose high exceeds the highs
of the 2 bars before and 2 bars after it. It is known only when the second
later bar closes."

### `market_structure.bullish_progression_definition`
**Question:** Exactly which sequence of swings counts as bullish structural
progression?
**Illustrative only:** "The last two confirmed swing highs are ascending
*and* the last two confirmed swing lows are ascending."

### `market_structure.bearish_progression_definition`
**Question:** Exactly which sequence of swings counts as bearish structural
progression?
**Illustrative only:** "The last two confirmed swing highs are descending
*and* the last two confirmed swing lows are descending."

### `market_structure.mixed_structure_handling`
**Question:** What happens when structure is neither bullish nor bearish
under the definitions above? And what if there aren't yet enough swings
today?
**Illustrative only:** "No trade."

### `acceptance_rejection_breakout.measurement_basis`
**Question:** Are acceptance, rejection and breakout judged on closes,
highs/lows (wicks), or both? Measured on which bar interval?
**Illustrative only:** "Decision-bar closes only."

### `acceptance_rejection_breakout.acceptance_definition`
**Question:** What exactly counts as price **accepting** beyond a level?
**Illustrative only:** "Two consecutive decision-bar closes beyond the level
by at least 1 tick."

### `acceptance_rejection_breakout.rejection_definition`
**Question:** What exactly counts as price **rejecting** a level?
**Illustrative only:** "A bar trades beyond the level but closes back on the
original side by at least 2 ticks."

### `acceptance_rejection_breakout.breakout_definition`
**Question:** What exactly counts as a **breakout**? How is it different
from acceptance?
**Illustrative only:** "One decision-bar close at least 4 ticks beyond the
level."

### `confirmation.definition`
*(Added in Round 2 for "sufficient confirmation that continuation is more
likely than immediate rejection".)*
**Question:** After acceptance, what exact, observable event counts as
confirmation? It must be something visible in the bars at that moment. The
backtest cannot compute "more likely".
**Illustrative only:** "The next decision bar closes further beyond the
level than the acceptance bar did, and its low stays beyond the level."

### `confirmation.max_bars_after_acceptance`
**Question:** Within how many bars after acceptance must confirmation
happen? After that, the setup is void.
**Illustrative only:** `3`.

### `confirmation.failure_handling`
**Question:** What happens if confirmation doesn't come in time, or price
rejects back through the level first? Does the level become unusable for
the day, or can a fresh acceptance start again?
**Illustrative only:** "The setup is void. That level may not be used again
today."

### `confirmation.parameters`
*(Added in Round 10.)*
**Question:** Which confirmation pattern is used, and which distances in
ticks define the retest-hold and continuation bars?
**Illustrative only:** `retest_hold_close_distance_ticks: 2`.

### `confirmation.clock`
*(Added in Round 10.)*
**Question:** How are the bars after acceptance counted, and what is the
last bar on which a hold, or a continuation, can still qualify?
**Illustrative only:** "The acceptance bar is bar 0. The hold can be no
later than the second-to-last bar."

### `confirmation.acceptance_lifetime`
*(Added in Round 10.)*
**Question:** What outcomes can an acceptance reach? Can it ever be reused?
What must happen before a zone can produce another setup?
**Illustrative only:** "One outcome per acceptance. A new setup needs a
fresh arm, attempt and acceptance."

### `level_states.parameters`
*(Added in Round 9.)*
**Question:** Which distances and counts define approach, breach, acceptance
and rejection? Give them in ticks and numbers of bars.
**Illustrative only:** `breach_distance_ticks: 1`,
`acceptance_consecutive_closes: 2`.

### `level_states.state_storage`
*(Added in Round 9.)*
**Question:** What is stored for every zone, and is its history ever
overwritten?
**Illustrative only:** "The current state plus an append-only event
history."

### `level_states.incomplete_bar_handling`
*(Added in Round 9.)*
**Question:** What does an incomplete decision bar do to counters and
pending sequences?
**Illustrative only:** "Resets counters and cancels pending sequences."

### `level_states.initialization`
*(Added in Round 9.)*
**Question:** In what state does each zone start, and do bars before the
entry window count? How are opening-range zones handled?
**Illustrative only:** "Start UNTOUCHED. Opening-range zones start at
09:45."

### `level_states.directional_episodes`
*(Added in the Round 9 amendment.)*
**Question:** How does a zone distinguish location (price merely above or
below it) from a directional attempt through it? What arms a zone, what
starts and ends an attempt, and what is needed before a new attempt?
**Illustrative only:** "A clear-side close arms the zone. A later touch
starts the attempt. A fresh arm is needed after each attempt."

### `level_states.after_acceptance`
*(Added in Round 9.)*
**Question:** What does acceptance permit, and for how long? (The details
belong to later rounds.)
**Illustrative only:** "A historical event only. Entry eligibility is
defined in the confirmation round."

### `level_states.event_priority`
*(Added in Round 9.)*
**Question:** When one bar satisfies several events, which one sets the
zone's current state?
**Illustrative only:** "Acceptance, then rejection, then breach, then
touch."

### `ema.included`
**Question:** Is an exponential moving average used in any rule? Answer
`true` or `false`. If `false`, the other EMA fields are not required.
**Illustrative only:** `false`.

### `ema.period`
**Question:** How many bars does the EMA cover?
**Illustrative only:** `20`.

### `ema.price_source`
**Question:** Which price feeds the EMA?
**Illustrative only:** `"close"`.

### `ema.bar_interval`
**Question:** Which bar length is the EMA computed on?
**Illustrative only:** `"5min"`.

### `ema.seed_method`
**Question:** How is the first EMA value initialised?
**Why it matters:** Different starting values give different EMA values for
many bars afterwards.
**Illustrative only:** "A simple average of the first `period` closes."

### `ema.warmup_bars`
**Question:** How many bars must pass before the EMA may be used?
**Illustrative only:** `100`.

### `ema.session_reset`
**Question:** Does the EMA carry over between days, or restart each
session?
**Illustrative only:** "Continuous across days. No reset."

### `ema.role_in_rules`
**Question:** Exactly how is the EMA used? For example, as a filter or a
trigger. What comparison is made?
**Illustrative only:** "Longs allowed only when the decision-bar close is
above the EMA."

### `vwap.included`
**Question:** Is VWAP used in any rule? Answer `true` or `false`. If
`false`, the other VWAP fields are not required.
**Illustrative only:** `false`.

### `vwap.anchor`
**Question:** When does the VWAP calculation start (its anchor)?
**Illustrative only:** "18:00 New York, at the Globex session open."

### `vwap.price_source`
**Question:** Which price per bar is used?
**Illustrative only:** "The typical price, (high + low + close) / 3."

### `vwap.volume_source`
**Question:** Which volume is used? Single contract or combined across
contract months?
**Illustrative only:** "The traded contract's own one-minute volume."

### `vwap.bands`
**Question:** Are deviation bands used? If so, how exactly are they
calculated?
**Illustrative only:** `"NONE"`.

### `vwap.role_in_rules`
**Question:** Exactly how is VWAP used?
**Illustrative only:** "Shorts allowed only below VWAP."

## 9. Setup and direction

### `setup.definition`
**Question:** What measurable market condition makes a trade **possible**?
This is before any trigger.
**Illustrative only:** "Price comes within the proximity tolerance of an
active structural level during the trading window."

### `setup.preconditions`
**Question:** What else must be true? For example, a minimum range, time
since the open, or no open position.
**Illustrative only:** "Flat, and fewer than the maximum trades taken
today."

### `setup.setup_expiry`
**Question:** How long does a setup stay valid if no trigger occurs?
**Illustrative only:** "Six decision bars."

### `direction.long_conditions`
**Question:** Exactly when is a setup traded **long**?
**Illustrative only:** "The setup is at a support-type level and a
rejection occurs upward."

### `direction.short_conditions`
**Question:** Exactly when is a setup traded **short**?
**Illustrative only:** "The setup is at a resistance-type level and a
rejection occurs downward."

### `direction.event_derived`
*(Added in Round 11.)*
**Question:** Is direction chosen in advance (a bias), or only derived from
completed market events? Which inputs are forbidden from choosing it?
**Illustrative only:** "Only from a confirmed continuation. No indicator or
bias."

### `direction.same_direction_candidates`
*(Added in Round 11.)*
**Question:** What happens when several same-direction confirmations
complete at the same decision time?
**Illustrative only:** "Keep them all, and resolve them in the entry round."

### `direction.directional_state_reset`
*(Added in Round 11.)*
**Question:** When does a confirmation stop being usable for an entry?
**Illustrative only:** "When it is used, invalidated, or its order validity
expires."

### `direction.conflict_resolution`
**Question:** What happens if long and short conditions are both true?
**Illustrative only:** "No trade."

## 10. Entry and orders

### `room_to_target.measurement_method`
*(Added in Round 2 for "adequate unobstructed room toward the next
meaningful level".)*
**Question:** How is "room" measured? From which price (the planned entry,
or the confirmation close) to which level (the next opposing structural
level)? In points, or as a multiple of the planned risk?
**Illustrative only:** "From the planned entry price to the nearest
opposing level from `structural_levels`, as a multiple of planned risk."

### `room_to_target.minimum_room`
**Question:** What is the smallest room that allows a trade?
**Illustrative only:** "1.5 times the planned risk."

### `room_to_target.obstruction_definition`
**Question:** What counts as an obstruction between the entry and the
target? For example, any other level type, a prior swing, or a round
number. Say whether an obstruction cancels the trade or moves the target.
**Illustrative only:** "Any active structural level or confirmed swing
point in between. If there is one, the trade is skipped."

### `entry_trigger.long_trigger`
**Question:** What exact event places a long entry order?
**Illustrative only:** "The close of the rejection bar."

### `entry_trigger.short_trigger`
**Question:** What exact event places a short entry order?
**Illustrative only:** "The close of the rejection bar."

### `order_type.entry_order_type`
**Question:** Which order type is used to enter: market, limit or
stop-market?
**Why it matters:** Market orders always fill but pay slippage. Limit orders
may not fill, and backtests often assume unrealistically good fills for
them.
**Illustrative only:** `"market"`.

### `order_type.entry_price_rule`
**Question:** For a limit or stop order, where exactly is the price? For a
market order, which price is assumed before slippage?
**Illustrative only:** "The open of the next one-minute bar, plus entry
slippage."

### `order_validity.time_in_force`
**Question:** How long does an unfilled entry order stay working?
**Illustrative only:** "Three one-minute bars."

### `order_validity.cancellation_conditions`
**Question:** What cancels a working entry order early?
**Illustrative only:** "Structural invalidation, or the end of the trading
window."

## 11. Invalidation, stop, target and position management

### `structural_invalidation.definition`
**Question:** What price behaviour proves the trade idea wrong,
independently of the stop?
**Illustrative only:** "A decision-bar close back through the level by
2 ticks."

### `structural_invalidation.action`
**Question:** What happens when invalidation occurs?
**Illustrative only:** "Cancel unfilled orders. Exit an open position at
market at the next bar open."

### `stop_placement.method`
**Question:** Where exactly is the protective stop placed?
**Illustrative only:** "Beyond the rejection bar's extreme, plus the
buffer."

### `stop_placement.buffer_ticks`
**Question:** How many ticks of buffer are added beyond the stop reference?
**Illustrative only:** `2`.

### `stop_placement.minimum_stop_points`
**Question:** What is the smallest stop distance allowed, in points?
**Illustrative only:** `5`.

### `stop_placement.maximum_stop_points`
**Question:** What is the largest stop distance allowed, in points?
**Why it matters:** This directly limits the loss of a single trade against
the prop-account drawdown.
**Illustrative only:** `25`.

### `stop_placement.trade_skipped_if_outside_limits`
**Question:** If the computed stop falls outside the minimum or maximum, is
the trade skipped, or is the stop clipped?
**Illustrative only:** "Skipped."

### `target_placement.method`
**Question:** How is the profit target determined?
**Illustrative only:** "A fixed multiple of the initial risk."

### `target_placement.parameters`
**Question:** What exact numbers does the target method use?
**Illustrative only:** `{risk_multiple: 1.5}`.

### `position_management.contracts_per_trade`
**Question:** How many MNQ contracts are used per trade?
**Illustrative only:** `1`.

### `position_management.sizing_method`
**Question:** Is size fixed, or does it vary? If it varies, what is the
exact formula?
**Illustrative only:** "Fixed."

### `position_management.scaling_in_out`
**Question:** Can the position be added to or partly closed?
**Illustrative only:** `"NONE"`.

### `position_management.breakeven_rule`
**Question:** Is the stop ever moved to the entry price? If so, exactly
when?
**Illustrative only:** `"NONE"`.

### `position_management.trailing_stop_rule`
**Question:** Does the stop trail price? If so, exactly how?
**Illustrative only:** `"NONE"`.

### `position_management.time_based_exit`
**Question:** Is a trade closed after a maximum holding time?
**Illustrative only:** "Exit at market after 60 minutes."

### `position_management.risk_per_trade`
*(Added in Round 2 for "sizes the MNQ position from that stop distance and
the permitted account risk".)*
**Question:** How much may one trade lose, including costs? Give it in US
dollars, or as a percentage of the balance named in the next question.
**Why it matters:** This is the single most important survival setting
against the prop firm's drawdown limit.
**Illustrative only:** `"USD 100"`.

### `position_management.risk_reference_balance`
**Question:** If risk is a percentage, a percentage of what? Starting
balance, current balance, or the remaining distance to the drawdown limit?
If risk is in fixed dollars, answer `NOT_APPLICABLE`.
**Illustrative only:** "Remaining distance to the max-loss limit."

### `position_management.contract_rounding`
**Question:** How is the computed number of contracts turned into a whole
number?
**Why it matters:** Rounding up can exceed the permitted risk.
**Illustrative only:** "Always round down: floor(risk ÷ (stop distance ×
$ per point + costs per contract))."

### `position_management.below_one_contract_action`
**Question:** What happens if even one contract would risk more than the
permitted amount?
**Illustrative only:** "Skip the trade."

### `position_management.max_contracts_per_trade`
**Question:** What is the hard cap on contracts in one trade, whatever the
sizing formula says? It must not exceed the prop firm's limit.
**Illustrative only:** `5`.

## 12. Daily limits, re-entry and flattening

### `daily_limits.max_trades_per_day`
**Question:** What is the maximum number of trades per day? This is a hard
cap. It is a limit, not a target.
**Illustrative only:** `2`.

### `daily_limits.max_losing_trades_per_day`
**Question:** After how many losing trades does the strategy stop for the
day?
**Illustrative only:** `1`.

### `daily_limits.daily_loss_stop_usd`
**Question:** At what realised daily loss, in US dollars and including
costs, does trading stop for the day? This must sit comfortably inside the
prop firm's own daily limit.
**Illustrative only:** `300`.

### `no_trade_conditions.execution_safety_conditions`
*(Added in Round 2 for "execution assumptions are unsafe".)*
**Question:** Give the complete, finite list of measurable conditions under
which a trade must not be taken because fills can't be simulated honestly.
Anything not on the list is not a reason to skip.
**Illustrative only:** "(a) The stop and target would both be inside the
entry bar's range. (b) The entry bar's range exceeds 40 points. (c) Less
than 10 minutes before the flatten time."

### `no_trade_conditions.risk_constraint_conditions`
**Question:** Give the complete, finite list of risk conditions that block
a trade.
**Illustrative only:** "(a) The planned loss would breach
`daily_limits.daily_loss_stop_usd`. (b) The planned loss would bring the
account within $200 of the prop max-loss limit. (c) The contract count
would exceed any cap."

### `reentry.allowed`
**Question:** After an exit, may the strategy trade the **same** level or
setup again?
**Illustrative only:** `"no"`.

### `reentry.conditions`
**Question:** If re-entry is allowed, exactly what must happen first?
**Illustrative only:** `"NOT_APPLICABLE"`.

### `reentry.cooldown_minutes`
**Question:** What is the minimum wait between one exit and the next entry?
**Illustrative only:** `15`.

### `session_flattening.no_new_entries_after`
**Question:** After what time, in the rule time zone, are new entries
forbidden?
**Illustrative only:** `"11:30"`.

### `session_flattening.flatten_all_by`
**Question:** By what time must every position be closed?
**Illustrative only:** `"15:50"`.

### `session_flattening.flatten_order_type`
**Question:** Which order type is used to flatten?
**Illustrative only:** "Market, with market-exit slippage."

### `session_flattening.emergency_flatten`
*(Added in Round 5.)*
**Question:** At what time, every eligible day, are all positions forcibly
closed as an account-safety backstop? What exact steps happen (cancel
entries, handle exits, submit the closing order, confirm flat)? Which order
type is used? What happens if that order is rejected? What about early-close
days?
**Illustrative only:** "15:55 New York, market order, alert if not
confirmed flat within 30 seconds."

## 13. Same-bar (intrabar) ambiguity

A one-minute bar tells us its open, high, low and close. It does **not** tell
us whether the high or the low happened first. If a trade's stop and target
both sit inside one bar's range, the bar alone cannot say which was hit
first. See `docs/DATA_CONTRACT.md`.

### `intrabar_ambiguity.stop_and_target_same_bar`
**Question:** When one bar touches both the stop and the target, which is
assumed to have been hit?
**Why it matters:** Assuming "target first" flatters results. The
conservative choice is to assume the stop was hit.
**Illustrative only:** "Assume the stop was hit first."

### `intrabar_ambiguity.entry_and_exit_same_bar`
**Question:** If the entry fills during a bar, can that same bar also hit
the stop or target?
**Illustrative only:** "Yes. The stop is assumed first if both are inside
the bar."

### `intrabar_ambiguity.resolution_with_finer_data`
**Question:** When finer data is available, is it used to settle ambiguous
bars? How are the results compared with the conservative version?
**Illustrative only:** "Use tick data where available, and report both
versions."

### `intrabar_ambiguity.fill_approximation_without_finer_data`
*(Added in Round 5.)*
**Question:** When the entry must be simulated at "bar close + delay" but
only one-minute data is available, what conservative fill price and time is
assumed? The trade is flagged `EXECUTION_AMBIGUOUS` either way.
**Why it matters:** Assuming the next minute's open, or any favourable
price, flatters results.
**Illustrative only:** "Fill at the worse of the next minute's open and the
decision-bar close, plus entry slippage."

## 14. Missing and bad data

### `missing_data.policy`
**Question:** What does the strategy do when expected bars are missing? The
validator reports them and never fills them. For example, skip the day, or
forbid entries near the gap?
**Illustrative only:** "Skip any trading date with a missing bar inside the
trading window."

### `missing_data.max_tolerated_gap_minutes`
**Question:** What is the largest gap, outside the trading window, that is
still acceptable?
**Illustrative only:** `5`.

### `missing_data.open_position_during_gap`
**Question:** How is an open position treated if data goes missing while it
is open?
**Illustrative only:** "Exit at the first bar after the gap, at its open
price, with market-exit slippage."

### `bad_data.policy`
**Question:** What happens to a day containing bars that fail the data
contract? For example, impossible OHLC or duplicates.
**Illustrative only:** "Exclude the whole trading date and log it."

### `bad_data.handling_of_rejected_bars`
**Question:** Are rejected bars ever corrected? If so, by what documented
process?
**Illustrative only:** "Never corrected automatically. Re-downloaded from
the vendor or excluded."

## 15. News events

### `news_events.policy`
**Question:** Does the strategy avoid scheduled economic news? If so, how?
**Illustrative only:** "No new entries inside the blackout window."

### `news_events.event_types`
**Question:** Which events count?
**Illustrative only:** `[CPI, NFP, FOMC_DECISION]`.

### `news_events.blackout_minutes_before`
**Question:** How many minutes before an event does the blackout start?
**Illustrative only:** `5`.

### `news_events.blackout_minutes_after`
**Question:** How many minutes after an event does the blackout end?
**Illustrative only:** `10`.

### `news_events.entry_protection_buffer_before_blackout_minutes`
*(Added in Round 7b.)*
**Question:** How many extra minutes *before* the formal blackout are new
entries already blocked? This stops the strategy opening a trade that would
be force-closed almost at once for a known event.
**Illustrative only:** `15`. For an 11:00 event with a 15-minute blackout,
new entries would then stop at 10:30.

### `news_events.open_position_during_event`
**Question:** What happens to a position that is open when the blackout
starts?
**Illustrative only:** "Flatten at market before the blackout."

### `news_events.calendar_source`
**Question:** Where do historical event dates and times come from? They
must be the originally *scheduled* times, known in advance.
**Illustrative only:** "A dated CSV of scheduled release times, committed to
the repo."

## 16. Costs

### `commissions.round_turn_per_contract_usd`
**Question:** What is the total cost per contract for one entry plus one
exit, in US dollars?
**Illustrative only:** `1.50`. This is a made-up figure, not a real fee
schedule.

### `commissions.includes_exchange_clearing_nfa_fees`
**Question:** Does that figure include exchange, clearing and regulatory
fees?
**Illustrative only:** `"yes"`.

### `commissions.source_and_date`
**Question:** Where does the number come from, and as of what date?
**Illustrative only:** "The prop firm's fee page, retrieved 2026-01-15."

### `slippage.entry_ticks`
**Question:** How many ticks worse than the reference price is each entry
assumed to fill?
**Illustrative only:** `1`.

### `slippage.stop_exit_ticks`
**Question:** How many ticks of slippage apply to stop exits? Stops are
often worse, because they fill in fast markets.
**Illustrative only:** `2`.

### `slippage.target_exit_ticks`
**Question:** How many ticks of slippage apply to target (limit) exits? And
does a target need price to trade *through* it to count as filled?
**Illustrative only:** "0 ticks, but price must trade 1 tick through the
target."

### `slippage.market_exit_ticks`
**Question:** How many ticks of slippage apply to market exits (time exits,
flattening, invalidation)?
**Illustrative only:** `1`.

### `slippage.stress_test_multipliers`
**Question:** By what multiples will slippage and commissions be scaled
up, to test that results survive worse costs?
**Illustrative only:** `[1.0, 1.5, 2.0]`.

## 17. Prop-account rules

Prop firms change their rules often. Record the exact version so that
Monte Carlo survival estimates use the rules that actually apply.

### `prop_account_rules.firm`
**Question:** Which prop firm?
**Illustrative only:** `"<firm name>"`.

### `prop_account_rules.program_name`
**Question:** Which exact account program or plan?
**Illustrative only:** `"<program> 50K"`.

### `prop_account_rules.rules_document_version`
**Question:** Which version or date of the firm's rules document applies?
**Illustrative only:** `"Rules page as of 2026-01-15"`.

### `prop_account_rules.rules_retrieved_date`
**Question:** On what date did you read and save those rules?
**Illustrative only:** `2026-01-15`.

### `prop_account_rules.account_size_usd`
**Question:** What is the account size? The project brief says $50K.
Confirm this for the chosen program.
**Illustrative only:** `50000`.

### `prop_account_rules.profit_target_usd`
**Question:** What profit target applies during the evaluation?
**Illustrative only:** `3000`.

### `prop_account_rules.max_loss_limit_usd`
**Question:** What is the maximum total loss or drawdown allowed?
**Illustrative only:** `2000`.

### `prop_account_rules.drawdown_type`
**Question:** How is that drawdown measured? Static, trailing at end of day,
or trailing intraday, including unrealised profit? Does it stop trailing at
some point?
**Why it matters:** This single rule often decides whether an account
survives.
**Illustrative only:** "Trailing at end of day; stops trailing at the
starting balance."

### `prop_account_rules.daily_loss_limit_usd`
**Question:** Is there a daily loss limit? How much is it, and how is it
measured?
**Illustrative only:** `"NONE"`, or `1000`.

### `prop_account_rules.max_contracts`
**Question:** What is the maximum position size allowed, in MNQ contracts?
**Illustrative only:** `50`.

### `prop_account_rules.consistency_rule`
**Question:** Is there a consistency rule? For example, no single day may
make up more than a set percentage of total profit. State the exact rule.
**Illustrative only:** "The best day must be under 50% of total profit."

### `prop_account_rules.minimum_trading_days`
**Question:** What is the minimum number of trading days, for the
evaluation and for payouts?
**Illustrative only:** `5`.

### `prop_account_rules.payout_rules`
**Question:** What are the payout rules? Cover when you can withdraw, the
minimum and maximum amounts, buffers, the profit split, and what happens to
the drawdown after a payout.
**Illustrative only:** "After 5 winning days of $200 or more, up to 50% of
profit, 90/10 split."

### `prop_account_rules.trading_restrictions`
**Question:** What other restrictions apply? For example, news trading,
holding over the weekend, required flat times, or copy-trading across
accounts.
**Illustrative only:** "Flat by 15:59 Central. Copy trading allowed."

## 18. Approval record

These fields are filled in **last**, after every question above is answered.

### `approval_record.approved`
`true` only when you approve the complete specification.

### `approval_record.approved_by`
Your name.

### `approval_record.approved_at_utc`
The time of approval, as an ISO timestamp with a zone, e.g.
`"2026-02-01T21:00:00Z"`.

### `approval_record.approved_spec_hash`
The **Spec hash** printed by `uv run mnq rules check`. It ties the approval
to this exact content.
