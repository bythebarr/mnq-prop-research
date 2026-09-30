"""Raw Databento validation and reproducible canonical ingestion (Round 16B).

Raw DBN files are never modified: every read re-verifies the SHA-256 recorded
in the acquisition ledger. Validation is fail-closed and never repairs,
interpolates or silently drops a price:

* decode completely (corrupt or truncated files fail);
* metadata: dataset GLBX.MDP3, the requested schema, stype_in raw_symbol, the
  exact approved symbols, start and end;
* every record belongs to an approved MNQ contract (no unrelated instrument);
* UTC, minute-start timestamps inside [start, end);
* 0.25 tick alignment, valid OHLC relationships, non-negative volume;
* identical duplicates are counted and removed with an explicit reason;
  conflicting duplicates fail;
* definitions agree with the frozen instrument facts (tick 0.25, $2 per point,
  USD) or produce a blocking mismatch;
* every contract has bars on the first and last trading dates the frozen roll
  rule designates it for.

Absent expected minutes are never labelled VERIFIED_NO_TRADE here: without
exchange calendars they are UNCLASSIFIED_MISSING_PENDING_CALENDAR. No
zero-volume bars are fabricated and no continuous-contract adjustment exists.
The canonical dataset is derived only from the verified raw files and carries a
content hash that reproduces from the same inputs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from mnq_research.data_contracts import EXCHANGE_TIMEZONE, cme_trading_date, is_scheduled_globex_minute
from mnq_research.data_estimate import designated_window, roll_date

FIXED_SCALE = 1_000_000_000  # DBN fixed-point prices: 1e-9 units
TICK_FIXED = 250_000_000  # 0.25 points
UNDEF_PRICE = 9_223_372_036_854_775_807
OHLCV_1M_RTYPE = 33
SOURCE_LABEL = "DATABENTO_GLBX.MDP3_OHLCV_1M"
UNCLASSIFIED = "UNCLASSIFIED_MISSING_PENDING_CALENDAR"
EXPECTED_TICK = Decimal("0.25")
EXPECTED_POINT_VALUE = Decimal("2")


@dataclass
class Issue:
    code: str
    count: int
    blocking: bool
    examples: list[str] = field(default_factory=list)


@dataclass
class Validation:
    issues: list[Issue] = field(default_factory=list)

    def add(self, code: str, mask_or_count: Any, blocking: bool, examples: Iterable[str] = ()) -> None:
        count = int(np.count_nonzero(mask_or_count)) if not isinstance(mask_or_count, (int, np.integer)) else int(mask_or_count)
        if count:
            self.issues.append(Issue(code, count, blocking, [str(e) for e in list(examples)[:5]]))

    @property
    def passed(self) -> bool:
        return not any(i.blocking for i in self.issues)


def verify_raw(path: Path, expected_sha256: str) -> None:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected_sha256:
        raise ValueError(f"RAW_FILE_MODIFIED_OR_INCOMPLETE: {path}")


def load_dbn(path: Path, expected_sha256: str) -> tuple[Any, pd.DataFrame]:
    """Hash-verify, then decode the whole file (any decode error fails)."""
    import databento

    verify_raw(path, expected_sha256)
    store = databento.DBNStore.from_file(path)
    df = store.to_df(price_type="fixed", pretty_ts=True, map_symbols=True)
    return store.metadata, df


def check_metadata(meta: Any, dataset: str, schema: str, symbols: tuple[str, ...], start_utc: str, end_utc: str) -> Validation:
    v = Validation()
    start_ns = pd.Timestamp(start_utc).value
    end_ns = pd.Timestamp(end_utc).value
    v.add("METADATA_DATASET_MISMATCH", int(str(meta.dataset) != dataset), True, [meta.dataset])
    v.add("METADATA_SCHEMA_MISMATCH", int(str(getattr(meta.schema, "value", meta.schema)) != schema), True, [meta.schema])
    v.add("METADATA_STYPE_MISMATCH", int(str(getattr(meta.stype_in, "value", meta.stype_in)) != "raw_symbol"), True, [meta.stype_in])
    v.add("METADATA_SYMBOLS_MISMATCH", int(sorted(meta.symbols) != sorted(symbols)), True, [meta.symbols])
    v.add("METADATA_RANGE_MISMATCH", int(int(meta.start) != start_ns or int(meta.end) != end_ns), True, [(meta.start, meta.end)])
    v.add("METADATA_UNRESOLVED_SYMBOLS", len(list(getattr(meta, "not_found", []) or [])), True, list(getattr(meta, "not_found", []) or []))
    return v


def validate_ohlcv(df: pd.DataFrame, symbols: tuple[str, ...], start_utc: str, end_utc: str, record_count: int | None = None) -> tuple[Validation, pd.DataFrame]:
    """Validate a fixed-price OHLCV-1m frame (index ts_event). Returns the validation and the de-duplicated frame."""
    v = Validation()
    if record_count is not None:
        v.add("RECORD_COUNT_MISMATCH_PARTIAL_OR_EXTRA", int(len(df) != record_count), True, [f"{len(df)} vs {record_count}"])
    ts = pd.DatetimeIndex(df.index)
    if ts.tz is None or str(ts.tz) != "UTC":
        v.add("TIMESTAMPS_NOT_UTC", len(df), True)
        return v, df
    ns = ts.as_unit("ns").asi8  # resolution-independent (pandas may use microseconds)
    v.add("NOT_MINUTE_START", (ns % 60_000_000_000) != 0, True, ts[(ns % 60_000_000_000) != 0][:5])
    outside = (ts < pd.Timestamp(start_utc)) | (ts >= pd.Timestamp(end_utc))
    v.add("OUTSIDE_APPROVED_INTERVAL", outside, True, ts[outside][:5])
    if "rtype" in df:
        v.add("WRONG_RECORD_TYPE", df["rtype"].to_numpy() != OHLCV_1M_RTYPE, True)
    unrelated = ~df["symbol"].isin(symbols)
    v.add("UNRELATED_INSTRUMENT", unrelated, True, df.loc[unrelated, "symbol"].unique()[:5])
    prices = df[["open", "high", "low", "close"]].to_numpy(dtype="int64")
    v.add("UNDEFINED_PRICE", (prices == UNDEF_PRICE).any(axis=1), True)
    v.add("OFF_TICK_PRICE", (prices % TICK_FIXED != 0).any(axis=1), True)
    o, h, l, c = prices.T
    v.add("INVALID_OHLC", (h < l) | (h < o) | (h < c) | (l > o) | (l > c) | (l <= 0), True)
    v.add("NEGATIVE_VOLUME", df["volume"].to_numpy().astype("int64") < 0, True)
    keyed = df.assign(_ts=ns).reset_index(drop=True)
    dup_key = keyed.duplicated(["instrument_id", "_ts"], keep=False)
    if dup_key.any():
        cols = ["instrument_id", "_ts", "open", "high", "low", "close", "volume"]
        groups = keyed.loc[dup_key, cols].groupby(["instrument_id", "_ts"]).nunique()
        conflicting = int((groups.max(axis=1) > 1).sum())
        v.add("CONFLICTING_DUPLICATE", conflicting, True)
        identical = int(keyed.duplicated(cols, keep="first").sum())
        v.add("IDENTICAL_DUPLICATE_REMOVED", identical, False)
        keep = ~keyed.duplicated(cols, keep="first").to_numpy()
        df = df[keep]
    return v, df


def validate_definitions(df: pd.DataFrame, symbols: tuple[str, ...]) -> tuple[Validation, dict[str, list[int]]]:
    """Definitions agree with the frozen instrument facts; returns raw_symbol -> instrument ids."""
    v = Validation()
    raw = df["raw_symbol"].astype(str)
    v.add("UNRELATED_INSTRUMENT", ~raw.isin(symbols), True, raw[~raw.isin(symbols)].unique()[:5])
    v.add("MISSING_DEFINITION", len(set(symbols) - set(raw)), True, sorted(set(symbols) - set(raw)))
    tick = df["min_price_increment"].astype("int64")
    v.add("TICK_SIZE_MISMATCH", tick != TICK_FIXED, True, tick[tick != TICK_FIXED].unique()[:5])
    if "currency" in df:
        v.add("CURRENCY_MISMATCH", df["currency"].astype(str) != "USD", True, df["currency"].unique()[:5])
    if "unit_of_measure_qty" in df:
        uom = df["unit_of_measure_qty"].astype("int64")
        v.add("POINT_VALUE_MISMATCH", uom != int(EXPECTED_POINT_VALUE * FIXED_SCALE), True, (uom / FIXED_SCALE).unique()[:5])
    else:
        v.add("POINT_VALUE_NOT_IN_DEFINITIONS", 1, True)
    ids = {s: sorted(set(df.loc[raw == s, "instrument_id"].astype(int))) for s in symbols}
    v.add("AMBIGUOUS_INSTRUMENT_ID", sum(len(i) != 1 for i in ids.values()), True, [s for s, i in ids.items() if len(i) != 1])
    return v, ids


def _year_of(symbol: str, first: dt.date, last: dt.date) -> int:
    month = {"H": 3, "M": 6, "U": 9, "Z": 12}[symbol[3]]
    years = [y for y in range(first.year - 1, last.year + 2) if y % 10 == int(symbol[4]) and roll_date(y, month) > first]
    return min(years)


def core_window(symbol: str, first: dt.date, last: dt.date) -> tuple[dt.date, dt.date]:
    """Trading dates on which the frozen roll rule designates this contract: [previous roll Monday, own roll Monday)."""
    month = {"H": 3, "M": 6, "U": 9, "Z": 12}[symbol[3]]
    year = _year_of(symbol, first, last)
    prev = roll_date(year, month - 3) if month > 3 else roll_date(year - 1, 12)
    return max(prev, first), min(roll_date(year, month) - dt.timedelta(days=1), last)


def canonicalize(df: pd.DataFrame, first: dt.date, last: dt.date, ingestion_utc: str) -> pd.DataFrame:
    """Research frame in the data-contract schema. Prices are the unadjusted contract prices; nothing is joined."""
    ts = pd.DatetimeIndex(df.index)
    out = pd.DataFrame({
        "timestamp_utc": ts,
        "timestamp_exchange": ts.tz_convert(EXCHANGE_TIMEZONE),
        "contract": df["symbol"].astype(str).to_numpy(),
        "instrument_id": df["instrument_id"].astype("int64").to_numpy(),
        "open": df["open"].to_numpy(dtype="int64") / FIXED_SCALE,
        "high": df["high"].to_numpy(dtype="int64") / FIXED_SCALE,
        "low": df["low"].to_numpy(dtype="int64") / FIXED_SCALE,
        "close": df["close"].to_numpy(dtype="int64") / FIXED_SCALE,
        "volume": df["volume"].to_numpy().astype("int64"),
    })
    out["trading_date"] = cme_trading_date(out["timestamp_utc"])
    out["source"] = SOURCE_LABEL
    out["source_timezone"] = "UTC"
    out["ingestion_timestamp_utc"] = pd.Timestamp(ingestion_utc)
    windows = {s: core_window(s, first, last) for s in out["contract"].unique()}
    td = out["trading_date"].dt.date
    out["in_designated_window"] = [windows[c][0] <= d <= windows[c][1] for c, d in zip(out["contract"], td)]
    return out.sort_values(["contract", "timestamp_utc"], kind="mergesort").reset_index(drop=True)


def content_hash(frame: pd.DataFrame, chunk: int = 250_000) -> str:
    """Deterministic hash of the canonical content (CSV of sorted rows), independent of parquet encoding."""
    h = hashlib.sha256()
    h.update(",".join(frame.columns).encode())
    for i in range(0, len(frame), chunk):
        h.update(frame.iloc[i:i + chunk].to_csv(index=False, header=False, date_format="%Y-%m-%dT%H:%M:%S%z").encode())
    return h.hexdigest()


def coverage(frame: pd.DataFrame, symbols: tuple[str, ...], first: dt.date, last: dt.date) -> tuple[Validation, dict[str, Any]]:
    """Per-contract coverage of the designated window, and missing minutes (all UNCLASSIFIED pending calendars)."""
    v = Validation()
    summary: dict[str, Any] = {}
    for s in symbols:
        rows = frame[frame["contract"] == s]
        start, end = core_window(s, first, last)
        inside = rows[rows["in_designated_window"]]
        dates = set(inside["trading_date"].dt.date)
        weekdays = [start + dt.timedelta(days=i) for i in range((end - start).days + 1) if (start + dt.timedelta(days=i)).weekday() < 5]
        missing_dates = [d for d in weekdays if d not in dates]
        edges_ok = bool(weekdays) and weekdays[0] in dates and weekdays[-1] in dates
        v.add("DESIGNATED_WINDOW_EDGE_NOT_COVERED", int(not edges_ok), True, [s])
        # expected minutes: scheduled Globex minutes of every weekday trading date in the window
        session_start = pd.Timestamp(dt.datetime.combine(start - dt.timedelta(days=1), dt.time(17)), tz=EXCHANGE_TIMEZONE)
        session_end = pd.Timestamp(dt.datetime.combine(end, dt.time(16)), tz=EXCHANGE_TIMEZONE)
        grid = pd.date_range(session_start, session_end, freq="1min", inclusive="left").tz_convert("UTC")
        grid = grid[is_scheduled_globex_minute(grid)]
        present = pd.DatetimeIndex(inside["timestamp_utc"])
        missing_minutes = int(len(grid.difference(present)))
        summary[s] = {
            "rows_total": int(len(rows)), "rows_in_designated_window": int(len(inside)),
            "rows_outside_designated_window_retained_for_audit": int(len(rows) - len(inside)),
            "first_timestamp_utc": None if rows.empty else str(rows["timestamp_utc"].min()),
            "last_timestamp_utc": None if rows.empty else str(rows["timestamp_utc"].max()),
            "designated_window": [str(start), str(end)], "edge_dates_covered": edges_ok,
            "weekday_dates_without_bars": [str(d) for d in missing_dates],
            "expected_scheduled_minutes": int(len(grid)),
            "missing_minutes": {UNCLASSIFIED: missing_minutes},
        }
    return v, summary
