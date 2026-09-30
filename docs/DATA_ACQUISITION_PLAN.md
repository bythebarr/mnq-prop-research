# Data Acquisition Plan (requirements for Phase 2)

Recorded from the rule owner's Round 2B answers, 2026-09-28. **Nothing here
has been implemented or downloaded yet.** These are binding requirements for
the future ingestion code.

## 1. Source

| Item | Requirement |
|---|---|
| Provider | **Databento**. Historical research data only. |
| Dataset / schema | `GLBX.MDP3`, schema `ohlcv-1m` |
| Instruments | Individual quarterly MNQ futures contracts, **not** a back-adjusted continuous series |
| Companion data | Instrument-definition and symbology data, downloaded alongside the bars |
| Per-row identity | Actual contract symbol (`contract`) **and** Databento `instrument_id` on every row (enforced by the data contract) |
| Range | 2019-05-06 to 2026-09-25 (the last fully completed trading day before the spec was written) |

## 2. Cost gate: nothing is bought automatically

1. Before any download, use Databento's metadata and cost-estimation
   functionality to report the **expected charge** for the exact request.
2. Show the estimate to the rule owner and **wait for explicit approval**.
3. Never purchase a subscription, or start a material paid download,
   automatically.
4. The Databento API key must come from an environment variable or a local
   untracked file. It is never committed or printed.

## 2a. Higher-resolution data (Round 4)

* Databento `trades` and `ohlcv-1s` are available, but `ohlcv-1m` stays the
  canonical strategy-decision dataset.
* Higher-resolution data is for execution validation, ambiguous
  same-minute stop/target ordering, slippage analysis and selected replay
  investigations. Only the **required segments** are fetched, each after a
  cost estimate and owner approval (the section 2 gate applies). The complete
  higher-resolution history is never downloaded automatically.
* Until a required segment is available, the frozen conservative same-bar
  rule applies, and **every affected trade is flagged**.

## 3. Raw-data immutability and appending

* Raw files are stored exactly as received under `data/raw/`, with a
  manifest of hashes (`uv run mnq hash`).
* A frozen raw file is **never modified or downloaded again**. Later
  completed sessions are appended as **new** files with their own manifest
  entries.
* Conversion (Databento fixed-point prices to decimals, `ts_event` to
  `timestamp_utc`, joining the symbology) happens in `data/interim/`, and is
  reproducible from raw data plus code.

## 4. Timestamps and absent minutes

* `ts_event` marks the **start** of the minute in UTC (matches D-008). A bar
  stamped 13:45:00 is usable only from 13:46:00.
* UTC is canonical. America/New_York time is derived for session rules,
  including daylight-saving transitions.
* **Databento prints no bar for a minute with no trades.** An absent minute
  is therefore **identified and classified**. It is never assumed to be
  corrupt, never forward-filled, and never turned into a fabricated
  zero-volume bar.
* The current validator already reports absent minutes against the regular
  Globex schedule, and never fills them. Phase 2 must add finer
  classification. A one-minute OHLCV file alone cannot tell a "no trades"
  minute from a vendor gap, so the method is an **open design question**
  (options include an exchange holiday calendar, Databento status data, or
  spot-checks against trade-level data). It will be decided before real
  data is used.
* **Unresolved dependency (confirmed in Round 9):** the structural-level
  code (D-023) treats every unlabelled absent minute as
  `UNEXPLAINED_MISSING_MINUTE`, which fails closed. Until ingestion can
  assign `VERIFIED_NO_TRADE_MINUTE` or `KNOWN_DATA_OUTAGE` reliably,
  real-data levels will often be unavailable. That is safe, but it will
  reduce the number of tradable days.

## 5. Contracts and rolls

* Contracts are joined **only** by the frozen roll rule
  (`contract_roll.*`, still TBD).
* Every roll transition is **flagged** in the joined series, so a contract
  change cannot create an artificial signal. No indicator, level or
  structure may span a roll boundary unless the rule freeze explicitly says
  how.

## 5a. Decision bars and execution timing (Round 5)

* Five-minute decision bars are aggregated per
  `decision_clock.bar_aggregation_rule`. Every interval gets an audit record:
  expected minutes, present count, missing timestamps, validation status and
  eligibility. Bars with fewer than five components are
  `INCOMPLETE_NOT_DECISION_ELIGIBLE`. Empty intervals produce a
  `NO_TRADES_OR_DATA_FOR_INTERVAL` gap record, not synthetic OHLCV.
* Entry submission is modelled at the decision-bar close plus 1.000 second.
  Execution-grade fills need trades or one-second data around each entry.
  Without it, the trade is flagged `EXECUTION_AMBIGUOUS`.
* The data manifest stores each applied roll timestamp and both contract
  symbols.

## 5b. Trading calendar artifacts (Round 6)

* Source: the official CME Group Holiday and Trading Hours calendar, per
  historical year and product category.
* One versioned local artifact per year, recording: source URL, retrieval
  timestamp, source publication/update date where available, file hash,
  applicable products, and for each session date the published open,
  published close, classification (normal, closed or early close) and notes.
* Never replaced silently after experiments are registered. A revision
  becomes a new calendar version with a new hash.
* The calendar drives date eligibility and emergency-flatten timing. The
  date status codes are `INELIGIBLE_SCHEDULED_EARLY_CLOSE`,
  `INELIGIBLE_ROLL_DATE`, `PRE_HOLIDAY_NORMAL_SESSION`,
  `POST_HOLIDAY_NORMAL_SESSION` and `ROLL_WEEK`.
* **Open practical question:** CME's site mainly publishes current and
  upcoming schedules. Obtaining authoritative calendars for 2019–2025 may
  need archived CME notices or another documented source. It must be
  resolved before real-data backtests.

## 5c. Scheduled-news calendar artifacts (Round 7)

* Built from the **official publishers** (BLS, BEA, Federal Reserve, Census,
  Department of Labor, ISM, The Conference Board, University of Michigan).
  Third-party calendars may help with discovery or cross-checks only.
* One normalised, dated, hashed file. Each event records: `event_id`,
  `event_name`, `event_category`, `scheduled_timestamp_utc`,
  `scheduled_timestamp_new_york`, `scheduled_end_timestamp_utc` and
  `official_scheduled_end_available` (for duration events such as Fed Chair
  testimony; an end time is never inferred from recordings or reports),
  `publishing_organization`, `source_url`,
  `source_publication_or_calendar_date`,
  `calendar_retrieval_timestamp_utc`, `calendar_file_hash`,
  `schedule_revision_status` and `notes`.
* Times are the release times **scheduled in advance**, never scrape times
  or market-reaction times.
* Raw snapshots are kept where licensing permits. Files are never silently
  rewritten after experiments are registered.
* A date whose schedule can't be verified from an archived source is
  `NEWS_CALENDAR_UNVERIFIED` and ineligible until resolved.

## 6. Research data versus the live execution path

| Layer | Component |
|---|---|
| Historical research (this repo) | Python + Databento |
| Eventual execution (separate, later) | Quantower (C#), connected to the **Tradeify** account through **Rithmic** |

* **No Databento component or API may exist in the live order-routing
  path.**
* Gates before any paper or prop deployment:
  1. **Data parity:** Databento versus Rithmic bars for the same period
     agree within documented tolerances.
  2. **Signal parity:** the Python research implementation and the C#
     Quantower implementation produce materially identical signals on the
     same data.

## 7. Using the history honestly

* Downloading the whole history **does not** authorise using all of it for
  strategy development.
* Training, validation, walk-forward and final untouched-holdout boundaries
  must be **registered** (`mnq experiment register`) **before** any
  strategy result is examined.
* The **final holdout must stay inaccessible** during rule development and
  parameter selection. Phase 2 should enforce this in code, for example
  with a data loader that refuses holdout dates unless an explicit,
  logged final-evaluation flag is set.

## Round 16A status: estimation and planning only (2026-09-29)

**Nothing has been purchased or downloaded, and no estimate has been
captured.** In this session `DATABENTO_API_KEY` was not set, and the
environment's network policy denied `hist.databento.com` (and the Tradeify,
CME, BLS and Federal Reserve hosts). Every estimate is therefore **UNKNOWN**,
which is never the same as $0.

### The exact proposed requests

These requests come from `src/mnq_research/data_estimate.py`. They use
metadata calls only: `get_cost`, `get_billable_size`, `get_record_count` and
`symbology.resolve`, in `historical` mode.

| | Value |
|---|---|
| Dataset | `GLBX.MDP3` |
| Symbol type | `raw_symbol` (individual contracts; parent and continuous symbols are refused) |
| Symbols | 31 quarterly contracts designated by the frozen roll rule: `MNQM9 MNQU9 MNQZ9 MNQH0 … MNQU6 MNQZ6`. Each must resolve to exactly one instrument, or the estimate stays UNKNOWN |
| Start (inclusive) | `2019-05-05T22:00:00Z`: the 18:00 New York Globex open of trade date 2019-05-06 |
| End (exclusive) | `2026-09-26T00:00:00Z`: after the 17:00 New York close of trade date 2026-09-25 |
| Prices | Original, unadjusted contract prices |

| Alternative | Schema | Scope |
|---|---|---|
| A | `definition` | Symbols, expirations, tick metadata |
| B | `ohlcv-1m` | Full range: **the baseline** |
| C | `ohlcv-1s` | Full range |
| D | `trades` | Full range |
| E1 | `ohlcv-1s` | Only 09:30–12:05 New York on each weekday. This is 1,930 disjoint windows, DST-correct and summed once. It covers every entry, stop, target and the 12:00 exit, without changing the strategy |
| E2 | `trades` | The same windows as E1 |

**No double counting.** There is one request per alternative, the symbols
are unique, and the windows are disjoint (all enforced in code). During a roll
week the old and new contracts are *different* instruments, and both are
needed.

**Uncertainties recorded with every estimate:**
- Databento's metadata API doesn't expose compressed size or account credits.
  The estimated charge therefore equals the pre-credit estimate unless the
  portal shows credits.
- E1 and E2 need about 5,800 metadata calls (1,930 windows × 3), so they may
  hit rate limits. A failure makes the estimate UNKNOWN, never partial.

### How to obtain the authoritative estimate (on a machine that can reach Databento)

1. Set the key in your shell only. Never put it in a tracked file:
   `export DATABENTO_API_KEY=...`
2. Run `uv run --extra databento mnq data estimate`. This installs the pinned
   client `databento==0.87.0`.

The command writes `outputs/estimates/databento_estimate_<sha>.json`. The
artifact includes:
- every request parameter;
- the raw responses;
- the client version;
- the retrieval time;
- its SHA-256.

The command never downloads data, and the key never appears in the artifact
or in any error text.

The artifact committed in Round 16A contains only the planned requests,
with status UNKNOWN.

### Purchase approval (Round 16B gate)

`data_acquisition.purchase_approval` in the spec must record all of:
- the approver and a UTC time;
- the **estimate artifact SHA-256**;
- the **exact request parameters**;
- a **maximum permitted charge in USD**.

Any change to the request, or any charge above that maximum, needs a new
approval.

### Calendars

The plan is in `configs/calendar_sources.yaml` (15 sources, all `PLANNED`,
URLs not yet verified live). `src/mnq_research/calendar_sources.py` refuses
READY for any source unless it has all of:
- a verified official URL;
- the retrieval time and the covered date range;
- the raw artifact and its matching SHA-256;
- the parser version;
- the parsed output and its matching SHA-256;
- an explicit time-zone treatment.

A recurring rule is never accepted as a history.

### Round 16A capture result (2026-09-30)

Only alternatives A and B were estimated, as authorised. Metadata calls only;
nothing was downloaded or purchased.

| | Definitions (A) | OHLCV-1m (B) |
|---|---|---|
| Estimated cost before credits | USD 0.009413488209 | USD 14.254553765059 |
| Uncompressed size | 5,945,680 bytes | 218,653,008 bytes |
| Records | 11,434 | 3,904,518 |
| Unresolved symbols | none | none |

Combined: **USD 14.263967253268** before credits. Compressed size and credits
are not exposed by the API.

The artifact is `outputs/estimates/databento_estimate_5fc1e4cc64682a29.json`.
C, D and E are deferred until signal replay identifies candidate timestamps.
