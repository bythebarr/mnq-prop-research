"""Data contract for one-minute MNQ OHLCV bars.

The contract (explained in plain English in docs/DATA_CONTRACT.md):

* One row = one one-minute bar for one contract.
* ``timestamp_utc`` is the bar's **start** (open) time, timezone-aware, in UTC.
  The bar covers [timestamp_utc, timestamp_utc + 1 minute). Its contents are
  only knowable at ``timestamp_utc + 1 minute`` (see ``bar_available_at``).
* ``timestamp_exchange`` is the same instant expressed in exchange-local time
  (America/Chicago, the CME's home time zone).
* ``trading_date`` follows the CME Globex convention: the session that opens
  at 17:00 Chicago time belongs to the NEXT calendar day's trading date.
* Nothing is ever silently repaired or filled. Problems are REPORTED.

Validation distinguishes:

* **errors** - the file breaks the contract and must not be used;
* **warnings** - facts that need a documented decision (e.g. missing bars).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

EXCHANGE_TIMEZONE = "America/Chicago"
BAR_INTERVAL = pd.Timedelta(minutes=1)
# 17:00 Chicago + 7 hours = midnight, so adding 7 hours to Chicago time and
# taking the calendar date yields the CME Globex trading date.
TRADING_DATE_OFFSET = pd.Timedelta(hours=7)

REQUIRED_COLUMNS: tuple[str, ...] = (
    "timestamp_utc",
    "timestamp_exchange",
    "trading_date",
    "contract",
    "instrument_id",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "source_timezone",
    "ingestion_timestamp_utc",
)
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
TIMESTAMP_COLUMN_ZONES: dict[str, str] = {
    "timestamp_utc": "UTC",
    "timestamp_exchange": EXCHANGE_TIMEZONE,
    "ingestion_timestamp_utc": "UTC",
}
SYNTHETIC_MARKER = "SYNTHETIC"

_OFFSET_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")


class MinuteStatus(str, Enum):
    """Why an expected one-minute bar is absent (supplied by the ingestion layer).

    Databento prints no bar for a minute without trades, so an absent minute is
    not automatically a data failure. A minute with no recorded status is
    treated as UNEXPLAINED_MISSING_MINUTE (fail closed).
    """

    VERIFIED_NO_TRADE_MINUTE = "VERIFIED_NO_TRADE_MINUTE"
    KNOWN_DATA_OUTAGE = "KNOWN_DATA_OUTAGE"
    UNEXPLAINED_MISSING_MINUTE = "UNEXPLAINED_MISSING_MINUTE"


class DataLoadError(Exception):
    """A data file could not be read at all."""


@dataclass
class Issue:
    code: str
    message: str
    count: int = 0
    examples: list[str] = field(default_factory=list)


@dataclass
class MissingBarGap:
    contract: str
    last_bar_before_gap_utc: pd.Timestamp
    first_bar_after_gap_utc: pd.Timestamp
    missing_bars: int


@dataclass
class DataValidationReport:
    row_count: int
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    missing_bar_gaps: list[MissingBarGap] = field(default_factory=list)
    is_synthetic: bool = False

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def total_missing_bars(self) -> int:
        return sum(g.missing_bars for g in self.missing_bar_gaps)

    def error_codes(self) -> set[str]:
        return {i.code for i in self.errors}

    def warning_codes(self) -> set[str]:
        return {i.code for i in self.warnings}

    def format(self, max_gaps: int = 10) -> str:
        lines = ["DATA VALIDATION REPORT", f"  Rows checked: {self.row_count}"]
        if self.is_synthetic:
            lines += [
                "",
                "  *** SYNTHETIC FAKE DATA - for software testing only. ***",
                "  *** It is NOT market data and says NOTHING about any strategy. ***",
            ]
        lines += ["", f"  Result: {'PASSES the data contract' if self.is_valid else 'FAILS the data contract'}"]
        for title, issues in (("ERRORS (file must not be used)", self.errors), ("WARNINGS (need a decision)", self.warnings)):
            if not issues:
                continue
            lines += ["", f"{title}: {len(issues)}"]
            for issue in issues:
                count = f" [{issue.count} row(s)]" if issue.count else ""
                lines.append(f"  - {issue.code}{count}: {issue.message}")
                for example in issue.examples:
                    lines.append(f"      e.g. {example}")
        if self.missing_bar_gaps:
            lines += [
                "",
                f"MISSING BARS: {self.total_missing_bars} expected bar(s) absent in "
                f"{len(self.missing_bar_gaps)} gap(s). They were NOT filled.",
            ]
            for gap in self.missing_bar_gaps[:max_gaps]:
                lines.append(
                    f"  - {gap.contract}: {gap.missing_bars} bar(s) missing between "
                    f"{gap.last_bar_before_gap_utc.isoformat()} and {gap.first_bar_after_gap_utc.isoformat()}"
                )
            if len(self.missing_bar_gaps) > max_gaps:
                lines.append(f"  ... and {len(self.missing_bar_gaps) - max_gaps} more gap(s)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def bar_available_at(timestamp_utc: pd.Series | pd.Timestamp):
    """Earliest instant at which a bar's OHLCV values are known (its close)."""
    return timestamp_utc + BAR_INTERVAL


def cme_trading_date(timestamp_utc: pd.Series) -> pd.Series:
    """CME Globex trading date (as midnight-normalised naive Timestamps)."""
    local = timestamp_utc.dt.tz_convert(EXCHANGE_TIMEZONE)
    return (local + TRADING_DATE_OFFSET).dt.tz_localize(None).dt.normalize()


def is_scheduled_globex_minute(timestamp_utc: pd.DatetimeIndex) -> np.ndarray:
    """True where a bar-start minute falls in the *regular* Globex schedule.

    Regular CME equity-index futures schedule (Chicago time): Sunday 17:00 to
    Friday 16:00, with a daily halt 16:00-17:00. This is used ONLY to decide
    which gaps count as "missing bars" for data-quality reporting. It knows
    nothing about exchange holidays or early closes: gaps caused by those are
    still reported, for a human to confirm. It is NOT a trading-session rule.
    """
    local = timestamp_utc.tz_convert(EXCHANGE_TIMEZONE)
    weekday = np.asarray(local.weekday)  # Monday=0 ... Sunday=6
    hour = np.asarray(local.hour)
    closed = (hour == 16) | (weekday == 5) | ((weekday == 4) & (hour >= 16)) | ((weekday == 6) & (hour < 17))
    return ~closed


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_bars(path: str | Path) -> pd.DataFrame:
    """Load bars from Parquet (canonical) or CSV (convenience).

    Nothing is repaired here. In particular, CSV timestamps WITHOUT an explicit
    UTC offset are left timezone-naive so that validation rejects them, rather
    than guessing a time zone.
    """
    path = Path(path)
    if not path.is_file():
        raise DataLoadError(f"File not found: {path}")
    suffix = path.suffix.lower()
    try:
        if suffix == ".parquet":
            return pd.read_parquet(path)
        if suffix == ".csv":
            df = pd.read_csv(path, dtype={c: str for c in (*TIMESTAMP_COLUMN_ZONES, "trading_date", "contract")})
            for column, zone in TIMESTAMP_COLUMN_ZONES.items():
                if column in df.columns:
                    df[column] = _parse_csv_timestamps(df[column], zone)
            return df
    except Exception as exc:  # pragma: no cover - depends on file corruption details
        raise DataLoadError(f"Could not read {path}: {exc}") from None
    raise DataLoadError(f"Unsupported file type {suffix!r}; use .parquet or .csv")


def _parse_csv_timestamps(values: pd.Series, zone: str) -> pd.Series:
    text = values.astype("string").str.strip()
    has_offset = text.str.contains(_OFFSET_RE, na=False)
    if bool(has_offset.all()):
        return pd.to_datetime(text, utc=True).dt.tz_convert(zone)
    # At least one timestamp lacks an offset: keep them naive (or raw text) so
    # validation reports the problem instead of us inventing a time zone.
    try:
        parsed = pd.to_datetime(text, errors="raise", format="ISO8601")
    except (ValueError, TypeError):
        return values
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        return parsed.dt.tz_localize(None)
    return parsed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _examples(df: pd.DataFrame, mask: pd.Series | np.ndarray, columns: list[str], limit: int) -> list[str]:
    subset = df.loc[np.asarray(mask), [c for c in columns if c in df.columns]].head(limit)
    return [f"row {idx}: " + ", ".join(f"{c}={row[c]}" for c in subset.columns) for idx, row in subset.iterrows()]


def validate_bars(df: pd.DataFrame, max_examples: int = 3) -> DataValidationReport:
    """Check a bar table against the data contract, collecting ALL problems."""
    report = DataValidationReport(row_count=len(df))
    err, warn = report.errors.append, report.warnings.append

    missing_columns = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_columns:
        err(Issue("MISSING_COLUMNS", f"required column(s) absent: {', '.join(missing_columns)}"))
        return report  # the remaining checks are meaningless without the schema
    if df.empty:
        err(Issue("EMPTY", "the file contains no bars"))
        return report

    df = df.reset_index(drop=True)
    report.is_synthetic = bool(df["source"].astype("string").str.upper().str.contains(SYNTHETIC_MARKER, na=False).any())

    # --- Nulls -------------------------------------------------------------
    for column in REQUIRED_COLUMNS:
        nulls = df[column].isna()
        if nulls.any():
            err(Issue("NULL_VALUES", f"column '{column}' has empty values", int(nulls.sum())))

    # --- Timestamp types and time zones ------------------------------------
    tz_ok: dict[str, bool] = {}
    for column, zone in TIMESTAMP_COLUMN_ZONES.items():
        dtype = df[column].dtype
        if isinstance(dtype, pd.DatetimeTZDtype):
            actual = str(dtype.tz)
            tz_ok[column] = actual == zone
            if not tz_ok[column]:
                err(Issue("WRONG_TIMEZONE", f"'{column}' is in time zone {actual}; the contract requires {zone}"))
        elif pd.api.types.is_datetime64_dtype(dtype):
            tz_ok[column] = False
            err(
                Issue(
                    "TZ_NAIVE_TIMESTAMP",
                    f"'{column}' has no time zone. Timestamps must be timezone-aware "
                    f"(expected {zone}); a naive time could mean any zone.",
                    len(df),
                )
            )
        else:
            tz_ok[column] = False
            err(Issue("NOT_A_TIMESTAMP", f"'{column}' is not a timestamp column (dtype {dtype})"))

    ts = df["timestamp_utc"]
    ts_usable = pd.api.types.is_datetime64_any_dtype(ts.dtype)

    # --- Ordering and duplicates (work even on naive timestamps) -----------
    if ts_usable:
        dupes = df.duplicated(subset=["contract", "timestamp_utc"], keep=False)
        if dupes.any():
            err(
                Issue(
                    "DUPLICATE_TIMESTAMPS",
                    "more than one bar for the same contract and minute",
                    int(dupes.sum()),
                    _examples(df, dupes, ["contract", "timestamp_utc", "close"], max_examples),
                )
            )
        backwards = ts.diff() < pd.Timedelta(0)
        if backwards.any():
            err(
                Issue(
                    "UNSORTED",
                    "bars are not in ascending timestamp_utc order",
                    int(backwards.sum()),
                    _examples(df, backwards, ["timestamp_utc"], max_examples),
                )
            )
        not_on_minute = ts.notna() & (ts != ts.dt.floor("min"))
        if not_on_minute.any():
            err(Issue("NOT_MINUTE_ALIGNED", "timestamp_utc must fall exactly on a minute", int(not_on_minute.sum())))

    # --- Prices and volume -------------------------------------------------
    prices_numeric = all(pd.api.types.is_numeric_dtype(df[c]) for c in PRICE_COLUMNS)
    if not prices_numeric:
        err(Issue("NON_NUMERIC_PRICE", "open/high/low/close must be numeric"))
    else:
        prices = df[list(PRICE_COLUMNS)].astype(float)
        non_finite = ~np.isfinite(prices).all(axis=1) & prices.notna().all(axis=1)
        if non_finite.any():
            err(Issue("NON_FINITE_PRICE", "price is infinite", int(non_finite.sum())))
        nonpositive = (prices <= 0).any(axis=1)
        if nonpositive.any():
            err(
                Issue(
                    "NONPOSITIVE_PRICE",
                    "a price is zero or negative",
                    int(nonpositive.sum()),
                    _examples(df, nonpositive, ["timestamp_utc", *PRICE_COLUMNS], max_examples),
                )
            )
        o, h, low, c = (prices[col] for col in PRICE_COLUMNS)
        impossible = (h < low) | (h < o) | (h < c) | (low > o) | (low > c)
        if impossible.any():
            err(
                Issue(
                    "IMPOSSIBLE_OHLC",
                    "high must be >= open, close and low; low must be <= open, close and high",
                    int(impossible.sum()),
                    _examples(df, impossible, ["timestamp_utc", *PRICE_COLUMNS], max_examples),
                )
            )
    if not pd.api.types.is_numeric_dtype(df["volume"]):
        err(Issue("NON_NUMERIC_VOLUME", "volume must be numeric"))
    else:
        negative_volume = df["volume"] < 0
        if negative_volume.any():
            err(Issue("NEGATIVE_VOLUME", "volume is negative", int(negative_volume.sum())))

    # --- Text fields ---------------------------------------------------------
    for column in ("contract", "source"):
        blank = df[column].astype("string").str.strip().eq("").fillna(False)
        if blank.any():
            err(Issue("BLANK_TEXT", f"column '{column}' has blank values", int(blank.sum())))
    _check_instrument_ids(df, err, warn, max_examples)
    bad_zones = sorted({z for z in df["source_timezone"].dropna().unique() if not _is_valid_zone(z)})
    if bad_zones:
        err(Issue("INVALID_SOURCE_TIMEZONE", f"not IANA time-zone names: {bad_zones[:5]}"))

    # --- Checks that need correct time zones ---------------------------------
    if tz_ok.get("timestamp_utc") and tz_ok.get("timestamp_exchange"):
        mismatch = ts.notna() & (df["timestamp_exchange"].dt.tz_convert("UTC") != ts)
        if mismatch.any():
            err(
                Issue(
                    "EXCHANGE_TIME_MISMATCH",
                    "timestamp_exchange is not the same instant as timestamp_utc",
                    int(mismatch.sum()),
                    _examples(df, mismatch, ["timestamp_utc", "timestamp_exchange"], max_examples),
                )
            )

    if tz_ok.get("timestamp_utc"):
        actual_dates = pd.to_datetime(df["trading_date"].astype("string"), errors="coerce")
        unparseable = actual_dates.isna() & df["trading_date"].notna()
        if unparseable.any():
            err(Issue("BAD_TRADING_DATE", "trading_date is not a valid date", int(unparseable.sum())))
        expected = cme_trading_date(ts)
        wrong_date = actual_dates.notna() & ts.notna() & (actual_dates.dt.normalize() != expected)
        if wrong_date.any():
            err(
                Issue(
                    "TRADING_DATE_MISMATCH",
                    "trading_date does not follow the CME convention (session opening 17:00 Chicago "
                    "belongs to the next day's trading date)",
                    int(wrong_date.sum()),
                    _examples(df, wrong_date, ["timestamp_utc", "trading_date"], max_examples),
                )
            )

        if tz_ok.get("ingestion_timestamp_utc"):
            too_early = df["ingestion_timestamp_utc"] < bar_available_at(ts)
            if too_early.any():
                err(
                    Issue(
                        "INGESTED_BEFORE_BAR_CLOSED",
                        "ingestion time is earlier than the bar's close, which is impossible "
                        "without future information (or the timestamp labels bar CLOSE, not bar START)",
                        int(too_early.sum()),
                        _examples(df, too_early, ["timestamp_utc", "ingestion_timestamp_utc"], max_examples),
                    )
                )

        valid_ts = ts.dropna()
        outside = ~is_scheduled_globex_minute(pd.DatetimeIndex(valid_ts))
        if outside.any():
            warn(
                Issue(
                    "BAR_OUTSIDE_REGULAR_SCHEDULE",
                    "bars exist during the regular Globex halt/weekend; check the source",
                    int(outside.sum()),
                )
            )

        report.missing_bar_gaps = find_missing_bars(df)
        if report.missing_bar_gaps:
            warn(
                Issue(
                    "MISSING_BARS",
                    f"{report.total_missing_bars} bar(s) expected under the regular Globex schedule are "
                    f"absent, in {len(report.missing_bar_gaps)} gap(s). Not filled. Causes can include "
                    "holidays/early closes, no trades in that minute, or vendor gaps.",
                    report.total_missing_bars,
                )
            )
    return report


def _check_instrument_ids(df: pd.DataFrame, err, warn, max_examples: int) -> None:
    """instrument_id must be a non-negative integer, consistent with ``contract``.

    One contract symbol with several IDs in one file is an error (rows from
    different instruments are being mixed). One ID used by several symbols is
    only a warning: exchanges can recycle IDs after a contract expires.
    """
    ids = df["instrument_id"]
    if not pd.api.types.is_integer_dtype(ids):
        numeric = pd.to_numeric(ids, errors="coerce")
        if numeric.isna().any() or not (numeric.dropna() % 1 == 0).all():
            err(Issue("NON_INTEGER_INSTRUMENT_ID", "instrument_id must be a whole number"))
            return
        ids = numeric
    negative = ids < 0
    if negative.any():
        err(Issue("NEGATIVE_INSTRUMENT_ID", "instrument_id is negative", int(negative.sum())))
    pairs = pd.DataFrame({"contract": df["contract"], "instrument_id": ids}).dropna().drop_duplicates()
    ids_per_contract = pairs.groupby("contract")["instrument_id"].nunique()
    conflicted = ids_per_contract[ids_per_contract > 1]
    if not conflicted.empty:
        err(
            Issue(
                "CONTRACT_HAS_MULTIPLE_INSTRUMENT_IDS",
                "one contract symbol appears with more than one instrument_id",
                len(conflicted),
                [f"{c}: {sorted(pairs.loc[pairs.contract == c, 'instrument_id'].tolist())}" for c in conflicted.index[:max_examples]],
            )
        )
    contracts_per_id = pairs.groupby("instrument_id")["contract"].nunique()
    shared = contracts_per_id[contracts_per_id > 1]
    if not shared.empty:
        warn(
            Issue(
                "INSTRUMENT_ID_SHARED_BY_CONTRACTS",
                "one instrument_id appears with more than one contract symbol (possible ID reuse; check symbology)",
                len(shared),
            )
        )


def _is_valid_zone(name: object) -> bool:
    try:
        ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def find_missing_bars(df: pd.DataFrame) -> list[MissingBarGap]:
    """List gaps where regular-schedule one-minute bars are absent (per contract)."""
    gaps: list[MissingBarGap] = []
    for contract, group in df.groupby("contract", sort=True):
        stamps = pd.DatetimeIndex(group["timestamp_utc"].dropna().drop_duplicates().sort_values())
        if len(stamps) < 2:
            continue
        deltas = stamps[1:] - stamps[:-1]
        for position in np.flatnonzero(deltas > BAR_INTERVAL):
            before, after = stamps[position], stamps[position + 1]
            between = pd.date_range(before + BAR_INTERVAL, after - BAR_INTERVAL, freq="min")
            expected = int(is_scheduled_globex_minute(between).sum())
            if expected:
                gaps.append(MissingBarGap(str(contract), before, after, expected))
    return gaps
