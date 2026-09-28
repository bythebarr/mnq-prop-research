"""Data-contract protections for one-minute bars."""

from __future__ import annotations

import pandas as pd

from mnq_research.data_contracts import load_bars, validate_bars
from mnq_research.synthetic_data import inject_defects


def test_clean_synthetic_bars_pass_and_are_flagged_as_fake(clean_bars):
    report = validate_bars(clean_bars)
    assert report.is_valid, report.format()
    assert report.warnings == []
    assert report.is_synthetic
    assert "SYNTHETIC FAKE DATA" in report.format()


def test_timezone_naive_timestamps_are_rejected(clean_bars):
    clean_bars["timestamp_utc"] = clean_bars["timestamp_utc"].dt.tz_localize(None)
    report = validate_bars(clean_bars)
    assert not report.is_valid
    assert "TZ_NAIVE_TIMESTAMP" in report.error_codes()


def test_wrong_timezone_is_rejected(clean_bars):
    clean_bars["timestamp_utc"] = clean_bars["timestamp_utc"].dt.tz_convert("America/New_York")
    assert "WRONG_TIMEZONE" in validate_bars(clean_bars).error_codes()


def test_impossible_ohlc_rows_are_rejected(clean_bars):
    clean_bars.loc[5, "high"] = clean_bars.loc[5, "low"] - 0.25
    clean_bars.loc[6, "low"] = clean_bars.loc[6, "close"] + 0.25
    report = validate_bars(clean_bars)
    issue = next(i for i in report.errors if i.code == "IMPOSSIBLE_OHLC")
    assert issue.count == 2


def test_nonpositive_prices_are_rejected(clean_bars):
    clean_bars.loc[3, ["open", "high", "low", "close"]] = 0.0
    assert "NONPOSITIVE_PRICE" in validate_bars(clean_bars).error_codes()


def test_duplicate_timestamps_are_detected(clean_bars):
    duplicated = pd.concat([clean_bars.iloc[:8], clean_bars.iloc[[7]], clean_bars.iloc[8:]], ignore_index=True)
    report = validate_bars(duplicated)
    issue = next(i for i in report.errors if i.code == "DUPLICATE_TIMESTAMPS")
    assert issue.count == 2  # both copies are listed


def test_unsorted_bars_are_detected(clean_bars):
    order = list(range(len(clean_bars)))
    order[50], order[51] = 51, 50
    assert "UNSORTED" in validate_bars(clean_bars.iloc[order]).error_codes()


def test_missing_bars_are_reported_not_filled(clean_bars):
    gap = clean_bars.drop(index=range(300, 307))
    report = validate_bars(gap)
    assert report.is_valid  # missing bars are a warning needing a decision, not silently fixed
    assert "MISSING_BARS" in report.warning_codes()
    assert report.total_missing_bars == 7
    assert len(gap) == len(clean_bars) - 7  # nothing was inserted
    (only_gap,) = report.missing_bar_gaps
    assert only_gap.last_bar_before_gap_utc == clean_bars.loc[299, "timestamp_utc"]


def test_daily_halt_and_weekend_are_not_missing_bars():
    from mnq_research.synthetic_data import SyntheticSpec, generate_synthetic_bars

    # Friday + Monday: spans the daily halt and a weekend.
    bars = generate_synthetic_bars(SyntheticSpec(first_trading_date="2024-01-12", n_trading_days=2))
    report = validate_bars(bars)
    assert report.missing_bar_gaps == []


def test_ingestion_before_bar_close_is_future_information(clean_bars):
    clean_bars.loc[10, "ingestion_timestamp_utc"] = clean_bars.loc[10, "timestamp_utc"]
    assert "INGESTED_BEFORE_BAR_CLOSED" in validate_bars(clean_bars).error_codes()


def test_trading_date_must_follow_cme_convention(clean_bars):
    # First bar opens 17:00 Chicago on Sunday; its trading date is Monday.
    first = clean_bars.loc[0]
    assert first["timestamp_exchange"].hour == 17
    assert first["trading_date"] == (first["timestamp_exchange"] + pd.Timedelta(days=1)).date()
    clean_bars.loc[0, "trading_date"] = first["timestamp_exchange"].date()
    assert "TRADING_DATE_MISMATCH" in validate_bars(clean_bars).error_codes()


def test_all_planted_defects_are_caught_together(clean_bars):
    bad, planted = inject_defects(clean_bars)
    report = validate_bars(bad)
    assert {"DUPLICATE_TIMESTAMPS", "UNSORTED", "IMPOSSIBLE_OHLC", "NONPOSITIVE_PRICE"} <= report.error_codes()
    assert report.total_missing_bars == planted["missing_bars"]


def test_missing_columns_are_reported(clean_bars):
    report = validate_bars(clean_bars.drop(columns=["volume", "source"]))
    assert report.error_codes() == {"MISSING_COLUMNS"}
    assert "volume" in report.errors[0].message and "source" in report.errors[0].message


def test_parquet_round_trip_preserves_time_zones(clean_bars, tmp_path):
    path = tmp_path / "bars.parquet"
    clean_bars.to_parquet(path, index=False)
    assert validate_bars(load_bars(path)).is_valid


def test_csv_without_utc_offsets_is_rejected(clean_bars, tmp_path):
    csv = clean_bars.head(20).copy()
    for column in ("timestamp_utc", "timestamp_exchange", "ingestion_timestamp_utc"):
        csv[column] = csv[column].dt.strftime("%Y-%m-%d %H:%M:%S")  # offsets stripped
    path = tmp_path / "naive.csv"
    csv.to_csv(path, index=False)
    assert "TZ_NAIVE_TIMESTAMP" in validate_bars(load_bars(path)).error_codes()


def test_csv_with_utc_offsets_is_accepted(clean_bars, tmp_path):
    path = tmp_path / "aware.csv"
    clean_bars.head(20).to_csv(path, index=False)
    assert validate_bars(load_bars(path)).is_valid
