"""Round 8 structural-level rules (owner-specified test list).

The bar data here is a hand-built, deterministic fixture for plumbing tests
only. It is not market data.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from conftest import RULE_FREEZE_PATH
from mnq_research.config import load_mapping
from mnq_research.data_contracts import MinuteStatus
from mnq_research.structural_levels import (
    LevelType,
    NewEntryNotPermitted,
    ProximityParams,
    SessionClass,
    SessionInfo,
    StructuralLevel,
    cluster_levels,
    compute_daily_levels,
    levels_for_new_entry,
    ny_time,
    prior_normal_rth_date,
    proximity_tolerance,
    rth_window,
    snapshot_at_entry,
)
from mnq_research.validation import check_rule_freeze

TRADE = dt.date(2024, 3, 12)  # Tuesday of the March 2024 roll week (roll Monday 2024-03-11)
PRIOR = dt.date(2024, 3, 11)
CURRENT_CONTRACT, CURRENT_ID = "MNQM4", 2002
OLD_CONTRACT, OLD_ID = "MNQH4", 1001
PARAMS = ProximityParams.from_spec(load_mapping(RULE_FREEZE_PATH))
ALL_TYPES = set(LevelType)
OR_TYPES = {LevelType.OPENING_RANGE_HIGH, LevelType.OPENING_RANGE_LOW}


# --------------------------------------------------------------------------- fixtures


def make_calendar(overrides: dict[dt.date, SessionClass] | None = None) -> dict[dt.date, SessionInfo]:
    calendar = {}
    day = dt.date(2024, 3, 1)
    while day <= dt.date(2024, 3, 15):
        if day.weekday() < 5:
            open_day = day - dt.timedelta(days=1)  # Globex opens 18:00 the previous evening (Monday: Sunday)
            calendar[day] = SessionInfo(day, SessionClass.NORMAL, ny_time(open_day, dt.time(18, 0)), True)
        day += dt.timedelta(days=1)
    for day, cls in (overrides or {}).items():
        calendar[day] = SessionInfo(day, cls, calendar[day].globex_open_utc, True)
    return calendar


def _minutes(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(start, end - pd.Timedelta(minutes=1), freq="min")


def make_bars(contract: str, instrument_id: int, base: float, prior: dt.date = PRIOR, trade: dt.date = TRADE) -> pd.DataFrame:
    """Prior day 09:00-16:30, overnight 18:00-09:30 and current 09:30-11:30 (New York)."""
    stamps = _minutes(ny_time(prior, dt.time(9, 0)), ny_time(prior, dt.time(16, 30))).append(
        _minutes(ny_time(trade - dt.timedelta(days=1), dt.time(18, 0)), ny_time(trade, dt.time(11, 30)))
    )
    close = [base + ((i * 3) % 17) * 0.25 for i in range(len(stamps))]
    return pd.DataFrame(
        {
            "timestamp_utc": stamps,
            "contract": contract,
            "instrument_id": instrument_id,
            "open": close,
            "high": [c + 0.5 for c in close],
            "low": [c - 0.5 for c in close],
            "close": close,
        }
    )


def set_bar(df: pd.DataFrame, date: dt.date, time: dt.time, **values) -> None:
    mask = df["timestamp_utc"] == ny_time(date, time)
    assert mask.sum() >= 1
    for column, value in values.items():
        df.loc[mask, column] = value


def drop_minutes(df: pd.DataFrame, date: dt.date, start: dt.time, end: dt.time, status: MinuteStatus | None):
    stamps = _minutes(ny_time(date, start), ny_time(date, end))
    kept = df[~df["timestamp_utc"].isin(stamps)].reset_index(drop=True)
    return kept, ({t: status for t in stamps} if status else {})


def compute(bars: pd.DataFrame, calendar=None, statuses=None, contract: str = CURRENT_CONTRACT):
    return compute_daily_levels(bars, TRADE, contract, calendar or make_calendar(), PARAMS, statuses)


def window_extreme(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, column: str, fn: str) -> float:
    rows = df[(df["timestamp_utc"] >= start) & (df["timestamp_utc"] < end)]
    return float(getattr(rows[column], fn)())


def level(price: float, level_type: LevelType, window: str = "PRIOR_RTH") -> StructuralLevel:
    return StructuralLevel(level_type, TRADE, CURRENT_CONTRACT, window, None, None, ny_time(TRADE, dt.time(9, 45)), price=price)


# --------------------------------------------------------------------------- level set


def test_exactly_seven_b0_level_types_are_active(completed_spec):
    spec_types = load_mapping(RULE_FREEZE_PATH)["structural_levels"]["level_types"]
    assert len(ALL_TYPES) == 7
    assert spec_types == [t.value for t in LevelType]
    assert {lv.level_type for lv in compute(make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0)).levels} == ALL_TYPES

    completed_spec["structural_levels"]["level_types"] = spec_types + ["VWAP"]
    assert any(p.path == "structural_levels.level_types" for p in check_rule_freeze(completed_spec).problems)
    completed_spec["structural_levels"]["level_types"] = spec_types[:-1]
    assert any(p.path == "structural_levels.level_types" for p in check_rule_freeze(completed_spec).problems)


def test_prior_rth_is_0930_inclusive_to_1600_exclusive():
    window = rth_window(PRIOR)  # 2024-03-11 is after the US DST change: New York = UTC-4
    assert window.start_utc == pd.Timestamp("2024-03-11 13:30", tz="UTC")
    assert window.end_utc == pd.Timestamp("2024-03-11 20:00", tz="UTC")
    assert len(window.expected_minutes()) == 390

    bars = make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0)
    set_bar(bars, PRIOR, dt.time(16, 0), high=18700.0)  # 16:00 bar: excluded
    set_bar(bars, PRIOR, dt.time(9, 29), low=17600.0)  # 09:29 bar: excluded
    set_bar(bars, PRIOR, dt.time(9, 30), low=17900.0)  # 09:30 bar: included
    levels = compute(bars)
    assert levels.level(LevelType.PRIOR_RTH_LOW).price == 17900.0
    assert levels.level(LevelType.PRIOR_RTH_HIGH).price < 18700.0
    assert levels.level(LevelType.PRIOR_RTH_HIGH).component_bar_count == 390


def test_closed_and_early_close_sessions_are_skipped_as_prior_rth():
    calendar = make_calendar({dt.date(2024, 3, 11): SessionClass.CLOSED, dt.date(2024, 3, 8): SessionClass.EARLY_CLOSE})
    assert prior_normal_rth_date(calendar, TRADE) == dt.date(2024, 3, 7)
    # A Monday references the preceding Friday unless it was closed or shortened.
    assert prior_normal_rth_date(make_calendar(), dt.date(2024, 3, 11)) == dt.date(2024, 3, 8)
    # An unknown weekday is never guessed.
    del calendar[dt.date(2024, 3, 7)]
    assert prior_normal_rth_date(calendar, TRADE) is None

    calendar = make_calendar({dt.date(2024, 3, 11): SessionClass.CLOSED, dt.date(2024, 3, 8): SessionClass.EARLY_CLOSE})
    bars = make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0, prior=dt.date(2024, 3, 7))
    prior_high = compute(bars, calendar).level(LevelType.PRIOR_RTH_HIGH)
    assert prior_high.window_start_utc == ny_time(dt.date(2024, 3, 7), dt.time(9, 30))
    assert prior_high.window_start_utc == pd.Timestamp("2024-03-07 14:30", tz="UTC")  # EST, UTC-5


def test_roll_week_reference_levels_use_the_current_contract():
    bars = pd.concat(
        [make_bars(OLD_CONTRACT, OLD_ID, 18000.0), make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0)], ignore_index=True
    ).sort_values("timestamp_utc", kind="stable")
    levels = compute(bars)
    for lv in levels.levels:
        assert lv.available, lv
        assert lv.source_contract == CURRENT_CONTRACT
        assert lv.source_instrument_ids == (CURRENT_ID,)
        assert lv.price >= 18199.0  # never an MNQH4 (old-contract) price
    mixed = [level(18000.0, LevelType.PRIOR_RTH_LOW), StructuralLevel(LevelType.PRIOR_RTH_HIGH, TRADE, OLD_CONTRACT, "PRIOR_RTH", None, None, None, price=18001.0)]
    with pytest.raises(ValueError):
        cluster_levels(mixed, Decimal("2"), ny_time(TRADE, dt.time(9, 45)))


# --------------------------------------------------------------------------- opening range


def test_opening_range_uses_exactly_the_15_required_bars():
    bars = make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0)
    set_bar(bars, TRADE, dt.time(9, 45), high=18900.0)  # first bar after the window: excluded
    set_bar(bars, TRADE, dt.time(9, 29), low=17500.0)  # last overnight bar: excluded from the OR
    set_bar(bars, TRADE, dt.time(9, 44), high=18300.0)  # last OR bar: included
    levels = compute(bars)
    or_high, or_low = levels.level(LevelType.OPENING_RANGE_HIGH), levels.level(LevelType.OPENING_RANGE_LOW)
    assert or_high.component_bar_count == or_low.component_bar_count == 15
    assert or_high.price == 18300.0
    expected_low = window_extreme(bars, ny_time(TRADE, dt.time(9, 30)), ny_time(TRADE, dt.time(9, 45)), "low", "min")
    assert or_low.price == expected_low > 17500.0


def test_opening_range_levels_are_unavailable_before_0945():
    levels = compute(make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0))
    before = {lv.level_type for lv in levels.available_levels(ny_time(TRADE, dt.time(9, 44, 59)))}
    at = {lv.level_type for lv in levels.available_levels(ny_time(TRADE, dt.time(9, 45)))}
    assert not (before & OR_TYPES)
    assert OR_TYPES <= at
    assert levels.level(LevelType.OPENING_RANGE_HIGH).available_at_utc == ny_time(TRADE, dt.time(9, 45))


def test_a_missing_opening_range_minute_invalidates_both_levels_and_the_day():
    bars, statuses = drop_minutes(
        make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0), TRADE, dt.time(9, 37), dt.time(9, 38), MinuteStatus.VERIFIED_NO_TRADE_MINUTE
    )
    levels = compute(bars, statuses=statuses)
    assert not levels.level(LevelType.OPENING_RANGE_HIGH).available
    assert not levels.level(LevelType.OPENING_RANGE_LOW).available
    assert "OPENING_RANGE_UNAVAILABLE" in levels.new_entry_blockers
    with pytest.raises(NewEntryNotPermitted):
        levels_for_new_entry(levels, ny_time(TRADE, dt.time(10, 0)))


# --------------------------------------------------------------------------- missing data


@pytest.mark.parametrize(
    "status, available",
    [
        (MinuteStatus.KNOWN_DATA_OUTAGE, False),
        (None, False),  # unexplained: no status recorded
        (MinuteStatus.UNEXPLAINED_MISSING_MINUTE, False),
        (MinuteStatus.VERIFIED_NO_TRADE_MINUTE, True),
    ],
)
def test_missing_overnight_minutes_by_status(status, available):
    bars, statuses = drop_minutes(make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0), TRADE, dt.time(2, 0), dt.time(2, 5), status)
    levels = compute(bars, statuses=statuses)
    assert levels.level(LevelType.OVERNIGHT_HIGH).available is available
    assert levels.level(LevelType.OVERNIGHT_LOW).available is available
    assert levels.level(LevelType.PRIOR_RTH_HIGH).available  # only the affected level is invalidated
    if status is MinuteStatus.KNOWN_DATA_OUTAGE:
        assert levels.level(LevelType.OVERNIGHT_HIGH).unavailable_reason == "KNOWN_DATA_OUTAGE_IN_WINDOW"


def test_no_trade_omissions_are_not_forward_filled():
    original = make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0)
    bars, statuses = drop_minutes(original, PRIOR, dt.time(12, 0), dt.time(12, 3), MinuteStatus.VERIFIED_NO_TRADE_MINUTE)
    rows_before = len(bars)
    levels = compute(bars, statuses=statuses)
    assert len(bars) == rows_before  # input untouched
    assert levels.level(LevelType.PRIOR_RTH_HIGH).available
    assert levels.level(LevelType.PRIOR_RTH_HIGH).component_bar_count == 390 - 3  # nothing fabricated

    bars, statuses = drop_minutes(original, PRIOR, dt.time(15, 59), dt.time(16, 0), MinuteStatus.VERIFIED_NO_TRADE_MINUTE)
    close = compute(bars, statuses=statuses).level(LevelType.PRIOR_RTH_CLOSE)
    assert close.source_bar_start_utc == ny_time(PRIOR, dt.time(15, 58))
    assert close.price == float(original.loc[original["timestamp_utc"] == ny_time(PRIOR, dt.time(15, 58)), "close"].iloc[0])
    assert close.staleness_seconds == 60.0


def test_prior_close_staleness_cannot_exceed_five_minutes():
    original = make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0)
    ok, statuses = drop_minutes(original, PRIOR, dt.time(15, 56), dt.time(16, 0), MinuteStatus.VERIFIED_NO_TRADE_MINUTE)
    close = compute(ok, statuses=statuses).level(LevelType.PRIOR_RTH_CLOSE)
    assert close.available and close.source_bar_start_utc == ny_time(PRIOR, dt.time(15, 55))
    assert close.staleness_seconds <= 300

    too_old, statuses = drop_minutes(original, PRIOR, dt.time(15, 55), dt.time(16, 0), MinuteStatus.VERIFIED_NO_TRADE_MINUTE)
    assert not compute(too_old, statuses=statuses).level(LevelType.PRIOR_RTH_CLOSE).available

    unexplained, _ = drop_minutes(original, PRIOR, dt.time(15, 59), dt.time(16, 0), None)
    close = compute(unexplained).level(LevelType.PRIOR_RTH_CLOSE)
    assert close.unavailable_reason == "FINAL_MINUTES_NOT_VERIFIED_NO_TRADE"


def test_off_tick_level_price_is_rejected():
    bars = make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0)
    set_bar(bars, TRADE, dt.time(9, 40), high=18400.1)
    assert compute(bars).level(LevelType.OPENING_RANGE_HIGH).unavailable_reason == "INVALID_PRICE_OFF_TICK"


# --------------------------------------------------------------------------- proximity and clustering


@pytest.mark.parametrize(
    "prior_range, expected",
    [("75", "2.00"), ("300", "6.00"), ("700", "10.00"), ("250", "5.00"), ("262.6", "5.50"), ("100.25", "2.25")],
)
def test_proximity_tolerance_is_clamped_and_rounded_up_to_the_tick(prior_range, expected):
    assert proximity_tolerance(20000.0 + float(prior_range), 20000.0, PARAMS) == Decimal(expected)


def test_proximity_tolerance_unavailable_without_a_valid_range():
    assert proximity_tolerance(None, 20000.0, PARAMS) is None
    assert proximity_tolerance(20000.0, 20000.0, PARAMS) is None
    assert proximity_tolerance(19990.0, 20000.0, PARAMS) is None


def test_clustering_is_transitive():
    a, b, c = level(20000.0, LevelType.PRIOR_RTH_LOW), level(20005.0, LevelType.OVERNIGHT_LOW, "OVERNIGHT"), level(20010.0, LevelType.OPENING_RANGE_LOW, "OPENING_RANGE")
    (cluster,) = cluster_levels([c, a, b], Decimal("6"), ny_time(TRADE, dt.time(9, 45)))
    assert cluster.lower_boundary == 20000.0 and cluster.upper_boundary == 20010.0
    assert len(cluster_levels([a, c], Decimal("6"), ny_time(TRADE, dt.time(9, 45)))) == 2


def test_constituent_identities_survive_clustering():
    a, b, c = level(20000.0, LevelType.PRIOR_RTH_LOW), level(20005.0, LevelType.OVERNIGHT_LOW, "OVERNIGHT"), level(20010.0, LevelType.OPENING_RANGE_LOW, "OPENING_RANGE")
    far = level(20100.0, LevelType.PRIOR_RTH_HIGH)
    created = ny_time(TRADE, dt.time(9, 45))
    zone, single = cluster_levels([a, b, c, far], Decimal("6"), created)
    assert zone.constituent_level_types == (LevelType.PRIOR_RTH_LOW, LevelType.OVERNIGHT_LOW, LevelType.OPENING_RANGE_LOW)
    assert zone.constituent_prices == (20000.0, 20005.0, 20010.0)
    assert zone.source_windows == ("PRIOR_RTH", "OVERNIGHT", "OPENING_RANGE")
    assert zone.representative_price == 20005.0
    assert zone.source_contract == CURRENT_CONTRACT and zone.created_at_utc == created
    assert single.lower_boundary == single.upper_boundary == single.representative_price == 20100.0
    assert single.constituents == (far,)


# --------------------------------------------------------------------------- persistence and expiry


def test_levels_do_not_disappear_when_price_touches_or_crosses_them():
    calm = make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0)
    violent = calm.copy()
    set_bar(violent, TRADE, dt.time(10, 15), high=19500.0, low=17000.0)  # crosses every level
    before, after = compute(calm), compute(violent)
    assert before.levels == after.levels  # nothing moved, redrawn or deleted
    assert {lv.level_type for lv in after.available_levels(ny_time(TRADE, dt.time(11, 0)))} == ALL_TYPES


def test_no_new_entry_use_at_or_after_1130():
    levels = compute(make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0))
    assert len(levels_for_new_entry(levels, ny_time(TRADE, dt.time(11, 25)))) == 7
    assert len(levels_for_new_entry(levels, ny_time(TRADE, dt.time(11, 29, 59)))) == 7
    with pytest.raises(NewEntryNotPermitted):
        levels_for_new_entry(levels, ny_time(TRADE, dt.time(11, 30)))


def test_entry_time_levels_are_preserved_for_an_open_position():
    levels = compute(make_bars(CURRENT_CONTRACT, CURRENT_ID, 18200.0))
    snapshot = snapshot_at_entry(levels, ny_time(TRADE, dt.time(11, 25)))
    assert snapshot.levels == levels.available_levels(ny_time(TRADE, dt.time(11, 25)))
    assert snapshot.usable_for_management(ny_time(TRADE, dt.time(13, 0)), flat_confirmed=False)
    assert not snapshot.usable_for_management(ny_time(TRADE, dt.time(13, 0)), flat_confirmed=True)
    assert not snapshot.usable_for_management(ny_time(TRADE, dt.time(15, 55)), flat_confirmed=False)
