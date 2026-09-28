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
