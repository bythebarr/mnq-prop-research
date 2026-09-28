# Data Contract: one-minute MNQ bars

This is the agreement every bar file must meet before any research code may
use it. It is enforced by `src/mnq_research/data_contracts.py`:

```bash
uv run mnq data validate <file.parquet|file.csv>
```

## 1. Schema

Each row is **one one-minute bar for one contract**.

| Column | Type | Meaning |
|---|---|---|
| `timestamp_utc` | timezone-aware timestamp, **UTC** | The bar's **start** time. The bar covers `[t, t + 1 minute)`. |
| `timestamp_exchange` | timezone-aware timestamp, **America/Chicago** | The same instant in CME local time. |
| `trading_date` | date | The CME Globex trading date. The session opening at 17:00 Chicago time belongs to the **next** calendar day. |
| `contract` | text | The specific contract, e.g. `MNQH5`. Synthetic data uses `FAKE-MNQ`. |
| `open`, `high`, `low`, `close` | number > 0 | Prices in index points. |
| `volume` | number ≥ 0 | Contracts traded in the minute. |
| `source` | text | Where the data came from. Synthetic data says `SYNTHETIC_FAKE_DATA_NOT_MARKET_DATA`. |
| `source_timezone` | IANA zone name | The zone the vendor's raw timestamps used, before conversion. |
| `ingestion_timestamp_utc` | timezone-aware timestamp, UTC | When the data entered this project. |

**Storage format.** Parquet is the canonical format, because it preserves
time zones and data types exactly. CSV is accepted for convenience, but every
timestamp in it must carry an explicit UTC offset (e.g.
`2024-01-08T14:30:00+00:00`). Otherwise the file is rejected, because a time
without a zone could mean anything.

## 2. Time policy

* **Internal timestamps are always timezone-aware.** Timezone-naive values
  are rejected. The code never guesses the zone.
* **UTC is the canonical storage time.** UTC has no daylight-saving jumps, so
  every minute exists exactly once.
* **Exchange-local time (Chicago) is stored alongside, and any other local
  time (e.g. New York) can be derived from UTC.** Rule times will be
  interpreted in the rule time zone chosen in the rule freeze
  (`timezone_policy.rule_timezone`).
* **Bar-start labelling.** `timestamp_utc` marks the *start* of the minute.
  A bar's values are only knowable at its close, `timestamp_utc + 1 minute`
  (`bar_available_at()` in code). A decision at time T may use only bars
  whose close is ≤ T.
* **Vendor conversion.** Some vendors label bars by their *end* time. Such
  data must be shifted to bar-start labels at ingestion. The vendor's
  convention is a rule-freeze question
  (`source_data.vendor_timestamp_convention`).

## 3. Checks

**Errors** mean the file breaks the contract and must not be used:

| Code | Detects |
|---|---|
| `MISSING_COLUMNS` | required columns absent |
| `EMPTY` | no rows |
| `NULL_VALUES` | empty cells in required columns |
| `TZ_NAIVE_TIMESTAMP` / `WRONG_TIMEZONE` / `NOT_A_TIMESTAMP` | time-zone violations |
| `DUPLICATE_TIMESTAMPS` | two bars for the same contract and minute |
| `UNSORTED` | rows not in ascending time order |
| `NOT_MINUTE_ALIGNED` | a timestamp not exactly on a minute |
| `NONPOSITIVE_PRICE` / `NON_FINITE_PRICE` / `NON_NUMERIC_PRICE` | price is zero, negative, infinite or not a number |
| `IMPOSSIBLE_OHLC` | high below open, close or low, or low above open, close or high |
| `NEGATIVE_VOLUME` / `NON_NUMERIC_VOLUME` | volume problems |
| `BLANK_TEXT` / `INVALID_SOURCE_TIMEZONE` | blank contract or source; unknown zone name |
| `EXCHANGE_TIME_MISMATCH` | `timestamp_exchange` is a different instant from `timestamp_utc` |
| `BAD_TRADING_DATE` / `TRADING_DATE_MISMATCH` | trading date unreadable or not following the CME convention |
| `INGESTED_BEFORE_BAR_CLOSED` | the data claims to have been stored before the bar finished, which is impossible without future information. It usually means end-labelled bars were not converted |

**Warnings** are facts that need a documented decision:

| Code | Detects |
|---|---|
| `MISSING_BARS` | minutes inside the regular Globex schedule with no bar |
| `BAR_OUTSIDE_REGULAR_SCHEDULE` | bars during the daily halt or the weekend |

All problems are collected and reported together. Validation never stops at
the first problem.

## 4. Missing bars are reported, never filled

A gap is counted against the **regular CME Globex schedule for equity-index
futures** (Chicago time): Sunday 17:00 to Friday 16:00, with a daily halt
from 16:00 to 17:00. The halt and the weekend are not reported as missing.

This schedule model is used **only for data-quality reporting**. It is not a
trading-session rule, and it has limits:

* It does **not** know about exchange holidays or early closes. Gaps on those
  days *are* reported as missing, for a human to confirm.
* Some vendors omit a minute in which **no trade occurred**. Such a minute is
  genuinely "missing" from the file, but carries no price information. The
  report cannot tell this apart from a vendor fault.

Nothing is ever forward-filled or interpolated. Filling a gap invents prices
that never traded. How the strategy behaves around gaps is a rule-freeze
decision (`missing_data.*`).

## 5. Intrabar ambiguity (important)

A one-minute bar records only four prices: open, high, low and close. It
does **not** record the order in which the high and the low occurred.

Suppose a long trade has its stop at 99.00 and its target at 101.00, and one
bar has a low of 98.75 and a high of 101.25. Both the stop and the target
were touched, but the bar cannot tell us which was touched **first**. The
trade might have won or lost.

Assuming the favourable order ("target first") systematically inflates
backtest results. This project therefore requires, before any simulation
runs:

* an explicit, **conservative** same-bar policy in the rule freeze
  (`intrabar_ambiguity.*`). For example, assume the stop is hit first when
  both are touched; **or**
* higher-resolution data (ticks or seconds) to settle ambiguous bars, with
  results reported both ways.

The same applies to an entry and an exit inside the same bar, and to limit
orders "touching" a price without trading through it.

## 6. No future information

* A bar may be used only after it has closed.
* Any feature must be calculable using only data available at its decision
  timestamp. That includes levels, averages, VWAP and session statistics.
* Anything calculated over a "whole day" is unavailable until the day ends.
* News calendars must use times that were **scheduled in advance**, not
  revised actual release times.
* Future simulator code will be tested for look-ahead explicitly, for
  example by truncating data at time T and checking decisions are unchanged.

## 7. Synthetic data

`uv run mnq data synth` produces **FAKE** data that follows this contract.
It exists only to exercise the software. Its `source` is
`SYNTHETIC_FAKE_DATA_NOT_MARKET_DATA`, its contract is `FAKE-MNQ`, it is a
structureless random walk, and every validation report on it prints a
warning. It must never be used as evidence about any strategy.
