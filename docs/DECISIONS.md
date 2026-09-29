# Architecture Decision Record

Each entry records what was decided, why, what else was considered, and
whether it is easy to reverse. Decisions that affect **trading logic, test
validity or the prop-risk model** are *not* made here. They are questions in
`RULE_FREEZE_GUIDE.md`.

---

## D-001 — Python 3.11 managed with `uv`, with a pip fallback
**Decision:** Target Python 3.11. `uv` manages the virtual environment and a
lockfile (`uv.lock`). `.python-version` pins 3.11.
**Why:** 3.11 was the environment's default interpreter and is supported by
every required library. `uv` was already installed, and a single `uv sync`
rebuilds an identical environment from the lockfile.
**Alternatives:** plain `venv` + `pip` (documented in the README as a
fallback); conda (heavier, and unnecessary).
**Reversible:** yes.

## D-002 — `src/` layout; CLI in `cli.py`
**Decision:** The code lives in `src/mnq_research/`. The requested
`__init__.py` and `__main__.py` exist. The command-line code lives in
`cli.py`, and `__main__.py` only calls it, so both `uv run mnq ...` and
`python -m mnq_research ...` work.
**Why:** The `src/` layout means tests run against the *installed* package,
which catches packaging mistakes. Keeping the CLI out of `__main__.py` lets
tests import it cleanly.
**Reversible:** yes.

## D-003 — All libraries installed now, including ones not yet used
**Decision:** scikit-learn, matplotlib and seaborn are dependencies although
Phase 1 does not use them.
**Why:** It avoids changing the environment in the middle of later
experiments. The lockfile records exact versions.
**Guardrail:** scikit-learn will never decide fills, stops, account state or
prop-rule violations (see `GOVERNING_MANDATE.md`).
**Reversible:** yes.

## D-004 — Strict YAML loading
**Decision:** A custom loader rejects **duplicate keys** and uses YAML 1.2
style scalars. Unquoted `17:00` stays text instead of becoming the integer
1020, and `yes`/`no`/`on`/`off` stay text instead of becoming booleans.
**Why:** Both default PyYAML behaviours can silently change the meaning of a
rule file without any error.
**Alternatives:** `ruamel.yaml` (another dependency); documenting "always
quote" only (depends on memory).
**Reversible:** yes.

## D-005 — Required rule fields listed in code, not in the YAML file
**Decision:** `validation.REQUIRED_FIELDS` lists every field that must be
answered. The YAML file is checked against that list.
**Why:** If the YAML defined its own requirements, deleting a question would
make it "pass". Now a deleted field is reported as `MISSING`. A test also
ensures that every required field is explained in `RULE_FREEZE_GUIDE.md`.
**Alternative considered:** a full pydantic model of the rule spec. It was
rejected for now because the *shape* of many answers (a text, a list, a
table) is not yet known, and a model would force premature choices. It can
be added once the spec is frozen.
**Reversible:** yes.

## D-006 — Approval is tied to a content hash
**Decision:** A spec is executable only if its status is `FROZEN_APPROVED`,
every field is answered, and `approval_record.approved_spec_hash` equals the
canonical hash of the spec. The hash covers everything except the approval
record itself.
**Why:** It makes "tweak a rule after seeing results" impossible to do
quietly. Any edit breaks the hash and blocks execution until you re-approve,
which in turn requires a new version and a new experiment.
**Reversible:** yes, but it should not be weakened.

## D-007 — Canonical configuration hashing
**Decision:** A config hash is the SHA-256 of the parsed YAML, rendered as
JSON with sorted keys and no whitespace. Comments, key order, indentation
and quoting style do not affect it. Values keep their type, so `1`, `1.0`
and `"1"` hash differently.
**Why:** It is formatting-insensitive, as required, but sensitive to every
change in meaning. The type distinction is intentional: an integer and a
decimal can behave differently in code.
**Data** is hashed by exact bytes, and folders by a sorted manifest of file
hashes. The order in which files are listed does not matter; any added,
removed, renamed or modified file changes the hash.
**Reversible:** yes. Changing the method would change all recorded hashes,
so it should be versioned if ever changed.

## D-008 — Bars are labelled by their START time
**Decision:** `timestamp_utc` marks the start of the minute. A bar is usable
only from `timestamp_utc + 1 minute`.
**Why:** Bar-start labelling is the most common convention and makes the
"available at" rule explicit. Vendor data labelled by end time must be
converted at ingestion. The `INGESTED_BEFORE_BAR_CLOSED` check helps catch
mistakes.
**Reversible:** yes, before any real data is ingested. It becomes costly
afterwards.
**Status:** CONFIRMED by the project owner on 2026-09-28.

## D-009 — Exchange-local time is America/Chicago; trading date follows CME
**Decision:** `timestamp_exchange` is in America/Chicago, the CME's local
time. `trading_date` follows the CME Globex convention, where the session
opening at 17:00 Chicago time belongs to the next day. The time zone in
which **your rules** are written (for example New York) is a separate
rule-freeze question.
**Why:** It keeps the data layer factual about the exchange and leaves
strategy timing to the rule freeze.
**Reversible:** yes.
**Status:** CONFIRMED by the project owner on 2026-09-28.

## D-010 — Missing-bar detection uses the regular Globex schedule only
**Decision:** A gap is reported when minutes inside the regular schedule
(Sunday 17:00 to Friday 16:00 Chicago, daily halt 16:00–17:00) have no bar.
Holidays and early closes are **not** modelled yet, so their gaps are
reported for a human to judge. Nothing is filled.
**Why:** Over-reporting is safer than guessing. An exchange-calendar
dependency can be added later with its own dated source.
**Reversible:** yes.

## D-011 — Duplicates are keyed on (contract, timestamp_utc)
**Decision:** Two rows for the *same contract* and minute are a duplicate.
Two different contracts in the same minute (for example around a roll) are
not.
**Why:** Raw per-contract data legitimately overlaps during rolls. How to
choose among contracts is a rule-freeze question (`contract_roll.*`).
**Reversible:** yes.

## D-012 — Validation separates errors from warnings and reports everything
**Decision:** Contract violations are *errors*, and the file must not be
used. Facts needing a decision, such as missing bars, are *warnings*. All
problems are collected; checks never stop at the first one.
**Why:** It gives you a complete picture in one run, and it keeps "needs a
policy decision" separate from "broken".
**Reversible:** yes.

## D-013 — Experiment registry: append-only JSON Lines, tracked in Git
**Decision:** Registering an experiment appends one line to
`outputs/experiments/registry.jsonl`. The line records the plan, the
canonical rule-spec hash, the Git commit (with `+dirty` if there are
uncommitted changes) and a UTC timestamp. An ID can never be re-registered
with different content. Registration is refused while the rule freeze is
not executable, or while any plan field (dates, data, costs) is undecided.
Periods must be chronological and non-overlapping.
**Why:** Pre-registration and immutability make quiet rule, date or data
changes after seeing results visible. Keeping every attempt counts the
real number of configurations tested. A plain text log is readable and
reviewable in Git.
**Alternatives:** SQLite, or MLflow (both overkill now and harder to review
in Git).
**Reversible:** yes.

## D-014 — Synthetic data: a structureless random walk, loudly labelled
**Decision:** The generator produces a symmetric random walk on a 0.25-point
grid. It is labelled FAKE in the `source` and `contract` columns, in a
sidecar manifest, and in every validation report. Defects are planted only
through a separate, explicit function.
**Why:** The data must be able to test the plumbing, but it must be
impossible to mistake for evidence. There is deliberately no pattern in it
to "discover".
**Reversible:** yes.

## D-015 — Git ignores data and generated figures and reports
**Decision:** Everything under `data/` except `.gitkeep`, all common market
data formats anywhere in the repo, and generated figures and reports are
ignored. `outputs/experiments/` stays tracked because the registry is the
audit trail.
**Reversible:** yes.

## D-016 — An `intrabar_ambiguity` section added to the rule freeze
**Decision:** The rule freeze has an extra section, beyond the requested
list, for same-bar stop/target and entry/exit ordering.
**Why:** The project brief requires a conservative policy for this, or
higher-resolution data. Making it a required field means it cannot be
forgotten.
**Reversible:** yes.

## D-017 — `daily_limits` section name
**Decision:** "Maximum trades per day" is a field inside a section called
`daily_limits`. That section also holds the losing-trade and daily loss
stops.
**Why:** These three limits interact, and they sit next to the prop firm's
daily limits.
**Reversible:** yes.

## D-018 — Five sections added to the rule-freeze template (Round 2)
**Decision:** At the rule owner's request (2026-09-28), five sets of required
questions were added: `market_structure`, `confirmation`, `room_to_target`,
extra risk-sizing fields in `position_management`, and `no_trade_conditions`.
All start as `TBD`. No values were added.
**Why:** The owner's plain-English summary depends on these concepts
(structural progression, confirmation, room to the next level, risk-based
sizing, and no-trade conditions), and the original template had nowhere to
define them. As required fields, they cannot be forgotten.
**Reversible:** yes, by the rule owner.
**Status:** CONFIRMED by the project owner on 2026-09-28.

## D-019 — `instrument_id` added to the data contract
**Decision:** Every bar carries the vendor's `instrument_id` next to
`contract`. Validation rejects non-integer or negative IDs, and any
contract symbol that appears with more than one ID in a file. An ID used by
several symbols is a warning only, because exchanges can recycle IDs after
expiry.
**Why:** The rule owner requires the contract symbol and instrument ID on
every row (Round 2B). It makes each bar traceable to its exact Databento
instrument.
**Reversible:** yes.
**Status:** CONFIRMED by the project owner on 2026-09-28 (requested in Round 2B).

## D-020 — Databento for research only; separate live execution path
**Decision:** Databento `GLBX.MDP3` `ohlcv-1m`, as individual contracts, is
the canonical historical research source. Live execution will be Quantower
(C#) to Tradeify via Rithmic. No Databento component may sit in the order
path. Data-parity and signal-parity gates come before paper or prop
deployment. Paid downloads need a cost estimate and explicit owner
approval. Details are in `docs/DATA_ACQUISITION_PLAN.md`.
**Why:** It keeps research reproducible, stops a research dependency from
leaking into execution, and proves that the backtested signals are the ones
that will actually trade.
**Reversible:** by the rule owner.
**Status:** CONFIRMED by the project owner on 2026-09-28.

## D-021 — Round 5 template additions and instrument arithmetic check
**Decision:** Three required fields were added:
- `decision_clock.bar_aggregation_rule`, answered by the owner in the same
  round.
- `session_flattening.emergency_flatten`: the 15:55 New York backstop
  requested by the owner. Its order type, failure handling and early-close
  treatment are still TBD.
- `intrabar_ambiguity.fill_approximation_without_finer_data`: the
  "separately frozen conservative approximation" the owner referenced. Still
  TBD.

The validator also rejects a spec whose `tick_value_usd` does not equal
`tick_size_points × point_value_usd`.
**Why:** Each concept was named by the owner as a rule, so it needs a
required home. The arithmetic check catches typos in money-critical
constants.
**Reversible:** yes.
**Status:** CONFIRMED by the project owner on 2026-09-28 (requested in Round 5).

## D-022 — News policy corrections (Round 7b)
**Decision:** At the owner's direction:
- A 15-minute **entry-protection buffer** (new required field
  `news_events.entry_protection_buffer_before_blackout_minutes`) blocks new
  entries from 30 minutes before a point-in-time Tier 1 event.
- **Fresh setups after a blackout:** all setup state is reset at the
  blackout start, and five-minute bars overlapping a blackout can't
  participate. The earliest decision after a 10:00 event is therefore
  **10:35, not 10:30**, which corrects the Round 7 example.
- **Fed Chair testimony** is treated as a duration event: blackout from
  15 minutes before the scheduled start to 30 minutes after the scheduled
  end. With no official end, the rest of the trading window is blocked.

The validator now requires the three news minute fields to be whole numbers
≥ 0. A test checks that the recorded examples agree with the recorded minute
values.
**Why:** Avoid trades that are force-closed almost immediately, stop setups
leaking across news releases, and handle long testimony honestly. The
example test stops a wrong worked example from contradicting the numbers,
as happened in Round 7.
**Reversible:** only through the registered rule-change process.
**Status:** CONFIRMED by the project owner on 2026-09-28.

## D-023 — Structural levels implemented as a stand-alone component (Round 8)
**Decision:** At the owner's request (tests were required), the Round 8
rules are implemented in `src/mnq_research/structural_levels.py`. The module
is **pure and fail-closed**, reads its parameters from the rule file, and is
not connected to any backtest. The spec stays `DRAFT_NON_EXECUTABLE`.

Engineering choices within the owner's rules (please confirm or change):
1. **Unknown calendar weekday:** a weekday missing from the session calendar
   makes the prior-RTH reference unknown, so those levels are unavailable.
   The code never guesses.
2. **Missing-minute default:** an absent minute with no recorded status is
   `UNEXPLAINED_MISSING_MINUTE`.
3. **Prior close:** the "within five minutes" rule and the "15:55 through
   just before 16:00" window are one constant. Staleness is measured from
   the source bar's close to 16:00 and recorded. Every minute after the
   source bar must be a verified no-trade minute.
4. **Zero or negative prior-RTH range:** treated as invalid, so the tolerance
   is unavailable and the day is not eligible for new entries.
5. **Representative price:** the statistical median. With an even number of
   constituents this can fall between ticks. It is informational only and is
   never used for conservative distances.
6. **Ties in clustering:** levels at the same price are ordered by level
   type name.

The validator now requires `structural_levels.level_types` to equal the
seven implemented types exactly, and checks the proximity parameters
(0 < fraction < 1, 0 < minimum ≤ maximum). Four new required fields were
added: `missing_data_treatment`, `price_validation`, `clustering_method`
and `decision_use`.

The Phase 1 test assertion "more than 100 fields unanswered" was replaced
by a stronger test that blanks every required field and requires all of
them to be reported.
**Reversible:** yes; the level rules themselves only through the registered
rule-change process.
**Status:** CONFIRMED by the project owner on 2026-09-28. Rules confirmed
in Round 8; engineering choices 1–6 confirmed in Round 9, with these
clarifications:
1. An absent weekday is judged against the *applicable calendar version*.
3. Every intervening minute must be explicitly classified
   `VERIFIED_NO_TRADE_MINUTE`.
5. The median representative can never be used for orders, stops, targets,
   sizing or room-to-target.

Still unresolved: how real-data ingestion will assign missing-minute
statuses is an open data-acquisition dependency (see
`DATA_ACQUISITION_PLAN.md` §4).

## D-024 — Zone state tracking implemented; three definitions need a decision (Round 9)
**Decision:** The owner's Round 9 rules are implemented as written in
`src/mnq_research/level_states.py`: append-only history, parameters read
only from the spec, and completed eligible five-minute bars only. The
plain-English guide is `docs/LEVEL_STATES.md`. Six required
`level_states.*` fields were added, and the validator checks the parameters
(whole numbers ≥ 1, and the approach method). `levels_for_new_entry` now
also refuses decisions before 09:45.

Implementation choices within the rules (please confirm):
- **Blackout reset:** at a blackout start, the zone's current state, origin
  and first-interaction time reset to a fresh state. The history is kept.
  This follows the Round 7b fresh-setup rule.
- **Gap:** "gapped above" is recorded when the previous eligible close was
  below L and the current bar's low is above U (mirrored for below).
- **Two-sided breach:** records `BREACHED_ABOVE`, `BREACHED_BELOW` and
  `TWO_SIDED_BREACH`.
- **Missing bar in the sequence:** treated exactly like an incomplete bar.

Open questions found while testing (the literal rules are implemented):
1. **Breach without crossing:** `low ≤ L − 1 tick` is also true for every
   bar lying wholly below a zone, so APPROACHED can never become the current
   state.
2. **Acceptance without crossing:** two closes beyond a boundary accept even
   if price never came from the other side.
3. **New attempt:** after a rejection window expires with price still in
   the zone, a later touch opens a new attempt.
**Reversible:** yes.
**Status:** CONFIRMED by the project owner on 2026-09-28 and amended by
the Round 9 gap resolution (D-025). Blackout reset and missing-bar treatment
are confirmed, and both remove all executable state. The gap definition was
confirmed with stronger clear-side thresholds. The two-sided breach choice
was changed to record only the active episode's direction. Questions 1–3
are resolved by directional arming and episodes (D-025).

## D-025 — Directional arming and interaction episodes (Round 9 gap resolution)
**Decision:** The zone tracker was rewritten around the owner's
directional-episode model:
- Clear-side closes **arm** a zone.
- Only a **later** bar's approach, touch or qualifying gap starts an
  attempt, and each attempt gets a new id.
- Breaches and acceptance count only in the attempt's direction.
- A new attempt needs a **fresh arm**.
- The rejection window limits rejection only, so delayed acceptance is
  possible.
- Interruptions and blackouts remove all executable state but keep the
  history.

A new required field `level_states.directional_episodes` holds the rules,
and `docs/LEVEL_STATES.md` was rewritten.

The tests now cover the 18 amendment items and the original Round 9 list
(36 level-state tests). Mutation checks confirmed that the tests catch
arming not being consumed, non-directional breaches, a missing rearm after
expiry, and same-bar arm-and-start. Loosening the gap-origin threshold is
**not observable**: under B0, a close within 0.50 below L comes from a bar
whose high is within the approach distance (at least 2 points), so that bar
has already started an episode. The threshold is therefore implied by the
arming rule.

**Interpretations pending owner confirmation:**
1. **Arming only while no episode is active.** Taken literally, the first
   acceptance close of an upward attempt (≥ U + 0.50) also arms from above.
   Any following bar that approaches from above would then start a downward
   episode and end the upward one, making two-close acceptance nearly
   impossible. As implemented, active attempts never arm the opposite side.
   Consequence: the end condition "an opposite episode is validly armed and
   begins" cannot occur under B0.
2. **Approach-started episodes** open their rejection window at the first
   touch, breach or gap. Until then, a clear-side close on the origin side
   neither rejects nor ends the episode.
**Reversible:** yes.
**Status:** CONFIRMED by the project owner on 2026-09-29, with these
clarifications:
1. Opposite-side arming is evaluated only after the current episode ends,
   and the end condition "an opposite episode arms and begins" is removed.
2. Approach-only episodes are withdrawn (`APPROACH_WITHDRAWN`, rearmed,
   never rejected) when price returns to the origin-side threshold before a
   touch, breach or gap.
3. The hidden gap threshold is now covered by a non-B0 parameter-interaction
   test (`test_gap_threshold_is_enforced_in_a_non_b0_parameter_fixture`),
   which catches the mutation that went undetected in Round 9.

## D-026 — Continuation confirmation implemented (Round 10)
**Decision:** The owner's single B0 pattern, `PULLBACK_HOLD_CONTINUATION`,
is implemented in `src/mnq_research/confirmation.py`:
- An acceptance, then the first retest-hold bar, then the immediately
  following bar closing one tick beyond the hold extreme.
- A six-bar clock after the acceptance (the hold must come by bar 5).
- Pre- and post-hold failure rules, and exactly one outcome per acceptance.
- Strict identity: one `acceptance_id`, `attempt_id` and zone version per
  sequence. Each acceptance can be used only once, which the zone records.

`SetupEngine` runs a zone's state tracker and the confirmation of each new
acceptance. The Round 9 tracker now also gives each acceptance an
`acceptance_id`, and ends approach-only episodes as `APPROACH_WITHDRAWN`
(D-025).

New required fields: `confirmation.parameters`, `confirmation.clock` and
`confirmation.acceptance_lifetime`. The validator checks the enumerated
settings, that the tick distances are ≥ 1, and that `max_bars` ≥ 2. The
plain-English guide is `docs/CONFIRMATION.md`.

28 confirmation tests cover the owner's Round 10 list. Seven deliberate
code mutations were all caught: a hold on the final bar, a hold without a
pullback, a wick confirming, strict failure comparisons, an inclusive
cutoff, missing bars ignored, and acceptance reuse.

**Questions and choices pending owner confirmation:**
1. **Equality conflict:** a low of exactly L − 0.25 (long), or a high of
   exactly U + 0.25 (short), satisfies both the retest-hold limit
   ("≥ L − 0.25", equality qualifies) and the failure rule ("at or below
   L − 0.25"). Implemented: **failure wins**, because the hold is meant not
   to "wick through the opposite side by one tick".
2. **Approach withdrawal timing:** it is evaluated from the bar *after* the
   episode's start bar. The start bar's own close is part of the approach,
   so price hasn't "returned" yet. Because price hovering below a zone
   repeatedly satisfies both the approach and the withdrawal conditions,
   `attempt_id`s can increase quickly in that situation. This is harmless
   to confirmation, but it inflates attempt counts in diagnostics.
3. **Replay acceptances:** an acceptance reached during the 09:30–09:45
   initialisation replay does not get a confirmation sequence. Confirmation
   is only started for acceptances observed from 09:45 on.
4. **Cutoff test:** the cutoff is applied to the decision bar's close. A
   continuation bar closing at or after 11:30 is `INVALIDATED_BY_CUTOFF`.
**Reversible:** yes.
**Status:** CONFIRMED by the project owner on 2026-09-29, with amendments
(see D-027):
1. Failure wins, and the inequalities are now exclusive.
2. Approaches became separate observations.
3. Replay acceptances may be confirmed; pre-09:45 confirmations are
   non-executable.
4. The cutoff at the bar's close is confirmed.

## D-027 — Direction, approach observations and replay confirmation (Round 11)
**Decision:** D-026 was confirmed with the owner's amendments and
implemented:
- Hold and failure inequalities are now mutually exclusive.
- Approaches are separate observations, with their own IDs, statuses and
  counts. Attempts start only at interaction, and the rejection clock starts
  then.
- Acceptances during the replay start confirmation from their real
  timestamp, via `initialize_engines`. Zone supersession at 09:45
  invalidates their confirmation.
- The cutoff is applied at the bar's close.

Round 11 adds `direction.py`:
- Direction candidates come only from fully linked confirmed continuations.
- There is no bias and no swing filter (`market_structure.definition =
  ACCEPTANCE_PULLBACK_HOLD_CONTINUATION`).
- Same-time opposite confirmations give `NO_TRADE_DIRECTIONAL_CONFLICT`
  plus a daily halt, independent of processing order.
- Multiple same-direction candidates are flagged `requires_selection`.
- Pre-09:45 confirmations are non-executable.

The validator enforces the B0 values for the structure definition, the swing
filter, the conflict result and the halt. Five new required fields were
added.

**Testing:** 16 direction tests, plus new level-state and confirmation
tests. Mutation checks caught a missing conflict check, a missing halt,
pre-window executability, 09:45 exclusion, an unchecked contract link, and
approaches starting attempts. One mutation is **equivalent**: reintroducing
the hold/failure overlap changes nothing, because the failure check runs
before the hold check. The exclusive hold inequality is a second guard for
that case. A report-ordering bug with identical zone IDs was found by the
tests and fixed.

**Pending owner confirmation:**
1. **Jumps:** a bar that jumps a whole armed zone without a qualifying gap
   starts no attempt. "Directionally valid breach" is treated as always
   accompanied by a touch; otherwise any jump would bypass the gap-origin
   threshold.
2. **Replay timing:** zones start unarmed at 09:30, so the earliest possible
   replay acceptance closes at 09:45. The owner's examples with acceptance at
   09:35 or 09:40 cannot occur under the frozen initialisation rules. The
   executability rules were tested at component level with synthetic clock
   times.
3. **Pre-window conflicts:** a same-time long/short conflict completing
   before 09:45 also halts the day (conservative).
4. **Withdrawal timing:** an approach can't be withdrawn on the same bar it
   started (carried over from D-026 item 2).
**Reversible:** yes.
**Status:** CONFIRMED by the project owner on 2026-09-29, with these
clarifications:
1. Prior-day and overnight zones take one pre-open arming observation from
   the 09:25–09:30 bar, and nothing else from it. The earliest replay
   acceptance is 09:40.
2. Attempts start only on an armed touch or a qualifying gap; breaches are
   recorded only inside attempts.
3. A pre-09:45 conflict halts the day.
4. Same-bar withdrawal stays prohibited.

## D-028 — Trade geometry and candidate selection (Round 12)
**Decision:**
- **D-027 implementation:** `apply_pre_open_arming` uses the completed bar
  ending at 09:30 only for arming (new parameter
  `level_states.parameters.pre_open_arming_lookback_bars: 1`; the validator
  allows 0 or 1). A missing or incomplete bar leaves the zone unarmed and is
  recorded as `PRE_OPEN_ARMING_UNAVAILABLE`. Opening-range zones are never
  pre-armed.
- **Round 12:** `trade_geometry.py` computes the planned entry (confirmation
  close ± 1 adverse tick), the structural stop (one tick beyond the far side
  of the origin zone, never compressed), the nearest distinct opposing zone,
  the target (one tick before it) and the exact gross R:R (a `Fraction`;
  ≥ 1.50 qualifies).
- **Selection:** highest R:R, then smallest risk, then greatest reward. An
  exact tie means no trade at that timestamp only.
- The selected candidate carries every identifier and price, plus the spec
  version and hash. There are no orders, sizes, fills or costs; the cost
  fields are reserved.
- Confirmations now expose `confirmation_close` and `confirmation_id`.
  Direction candidates carry the origin zone's boundaries. Zones have an
  `expired` flag.
- `minimum_planned_gross_rr` is written in quotes in the YAML (`"1.50"`) so
  it parses as an exact decimal.
- New required fields: eight `trade_geometry.*` fields. The validator
  enforces the ranking, the tie action, non-negative buffers and a positive
  minimum R:R. Plain English is in `docs/TRADE_GEOMETRY.md`.

**Testing:** 19 geometry tests and 8 D-027 tests. Of the mutation checks,
the first run left two weak tests undetected: a zone exactly at the entry,
and a secondary-ranking test whose candidates actually differed in R:R. Both
tests were fixed, and all mutations are now caught.

**Pending owner confirmation:**
1. **The tertiary ranking criterion is mathematically unreachable.** With
   equal R:R and equal risk, reward is equal too (reward = R:R × risk), so
   "greatest reward" can never break a tie. Is that acceptable, or should
   the order of the criteria change?
2. **Losers of a selection:** lower-ranked, non-tied candidates are marked
   non-executable (`NON_EXECUTABLE_NOT_SELECTED`), so they aren't
   reconsidered at a later timestamp.
3. **Blocking conditions:** blackout, news entry-protection and safety
   halts are passed in as named conditions. The logic that derives them
   from the news calendar is not built yet.
4. **Target zones must already exist at the decision time.** For example,
   an opening-range zone can be a target only from 09:45.
**Reversible:** yes.
**Status:** CONFIRMED by the owner (Round 12, D-027 and the D-028
confirmations, 2026-09-29):
1. The ranking is exactly `[highest_planned_gross_rr,
   smallest_planned_risk_points]`, then no trade. It is not reordered to
   prefer greater reward. `exact_tie_action` is
   `NO_TRADE_SAME_DIRECTION_GEOMETRY_TIE`.
2. Non-selected candidates are terminal and non-executable, with the reason
   `NOT_SELECTED_BY_GEOMETRY_RANKING`. They cannot be reused through the same
   confirmation, acceptance, attempt or zone identity.
3. `execution_eligibility_integration.status: REQUIRED_BEFORE_EXECUTABLE`.
   News, safety, session and missing-data controls must produce typed states
   that geometry and entry code consume directly. Unknown fails closed; no
   free-form string, missing key or default false may mean "safe".
4. Target-zone existence at the decision time is confirmed.

Implementation: the ranking and tie action were changed in the spec, the
code and the validator. The status `NON_EXECUTABLE_NOT_SELECTED` was renamed
`NOT_SELECTED_BY_GEOMETRY_RANKING`. The new module `eligibility.py` provides
`ExecutionEligibility` with `ControlState`, and `evaluate_geometry` now
requires it (there is no default). The entry-order book consumes the
identity of every evaluated candidate (D-029). `is_unresolved` now also
treats `REQUIRED_BEFORE_EXECUTABLE` and all-capitals `UNRESOLVED_…` markers
as unanswered.

## D-029 — Entry order lifecycle (Round 13)
**Decision:** The entry is a **market** order, simulated only
(`src/mnq_research/entry_order.py`, plain English in
`docs/ORDER_LIFECYCLE.md`).
- **Creation and submission:** the order is created at the decision time and
  submitted exactly 1.000 s later. It is submitted only if every eligibility
  control is CLEAR, the 11:30 cutoff hasn't been reached, and no news
  boundary has been reached. Otherwise it is terminal
  `NOT_SUBMITTED_INELIGIBLE` with the exact reasons.
- **Deadline:** the earliest of submission + 2.000 s, 11:30, a news boundary,
  or an event-driven invalidation (safety halt, loss of reliable state,
  contract/session invalidation). The cancellation request is stamped at the
  deadline itself. There is no extend, replace, convert or chase.
- **Outcomes:**
  - Full fill.
  - Partial fill: the fill is kept, the remainder cancelled, and the day
    halted.
  - No fill: the day is halted.
  - Rejection: no retry, and the day is halted.
  - Unknown state: reconcile, never resubmit, raise a critical alert, halt.
- **Exposure:** every fill is real exposure, including one that races a
  cancellation or arrives after the order is final. A fill creates a
  REQUIRED protection task with the confirmed quantity and the frozen stop
  and target. No protective order is placed.
- **Performance:** actual R, slippage and latency use the actual fills. All
  values are exact fractions.
- **Research quantity:** 1 contract, labelled `RESEARCH_QUANTITY_ONLY` /
  `NOT_DEPLOYMENT_SIZING`.
- **Stops:** there is no minimum or maximum stop filter. Stop validity checks
  refuse stops from the wrong zone and stops on the wrong side, and never
  resize them.
- **Unresolved on purpose:** `risk_per_trade` (UNRESOLVED_EVIDENCE_DERIVED),
  `position_sizing_balance_basis` (UNRESOLVED_PENDING_PROP_RULE_MODEL) and
  `protective_order_layer_status` (REQUIRED_BEFORE_EXECUTABLE).
  `live_or_paper_order_submission` is `prohibited`, and the validator
  rejects any other value.
- **Renamed fields:** `stop_placement.trade_skipped_if_outside_limits` became
  `…_outside_stop_limits`, and `position_management.risk_reference_balance`
  became `position_sizing_balance_basis`.

**Testing:** 25 lifecycle tests (the owner's 24, plus parameter provenance)
and 3 D-028 tests. In the mutation checks, 3 of 30 mutations first survived:
- a fill with no acknowledgement;
- a rejection after a fill;
- order creation at the wrong time.

Tests were added for each, and all 30 are now caught.

**Pending owner confirmation:**
1. **Acknowledgement timeout.** It is recorded as its own parameter
   (`acknowledgement_timeout_seconds: "2.000"`), measured from submission, so
   it coincides with the working deadline. A fill counts as proof that the
   order reached the market, so a filled but unacknowledged order is not
   "unknown". Is that right?
2. **Loss of reliable state.** Besides ending the working time, it is also
   treated as `ENTRY_ORDER_STATE_UNKNOWN` (reconcile, alert, halt).
3. **Contradictory reports** (overfill, fill after the final state, a
   rejection after a fill, a reconciled position that differs from the
   recorded fills) become `ENTRY_ORDER_STATE_UNKNOWN`. That outcome is never
   later replaced by a cleaner one.
4. **Halting.** A full fill does not halt the day. The open position blocks
   new entries, through `no_open_position`, until trade management exists.
   A `NOT_SUBMITTED_INELIGIBLE` order does not halt the day either (your
   rule said only "terminal").
5. **Stop beyond the actual fill.** If the actual fill is at or beyond the
   frozen stop, the stop is kept unchanged and the protection task is
   flagged `stop_protective_of_actual_entry: false`. What should happen next
   (an immediate flatten?) belongs to the protection and trade-management
   round.
6. **Floor and skip.** `contract_rounding` and `below_one_contract_action`
   stay TBD, even though your later formula says floor and skip. They should
   be confirmed together with `risk_per_trade`.
7. **`entry_trigger.long_trigger` / `short_trigger`** are still TBD. Rounds
   11–13 seem to answer them ("a SELECTED_ENTRY_CANDIDATE → market order at
   decision + 1.000 s"), but I have not filled them in without your
   confirmation.
**Reversible:** yes.
**Status:** Rules CONFIRMED by the owner (Round 13). Items 1–7 PENDING.
