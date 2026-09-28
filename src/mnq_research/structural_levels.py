"""Structural reference levels for Baseline B0 (Rule Freeze Round 8).

COMPONENT OF A DRAFT SPECIFICATION. This module implements only the
structural-level rules the rule owner answered in Round 8
(configs/rule_freeze_v1.yaml: ``structural_levels`` and
``sessions.reference_windows``). It contains NO acceptance, confirmation,
entry, stop or target logic, and it is not connected to any backtest.

Design rules:

* **Pure functions.** Nothing here reads files, clocks or the network.
* **Fail closed.** Anything uncertain (unknown calendar day, unverified
  session open, data outage, unexplained missing minute, off-tick price)
  makes the affected level UNAVAILABLE, with a reason. Nothing is guessed,
  filled or interpolated.
* **One-minute source data.** Levels come from canonical start-stamped
  one-minute bars (data contract, D-008) for ONE contract: the contract
  designated for the current strategy date. Callers are expected to pass
  bars that already passed ``validate_bars``.
* **Parameters come from the spec**, never from constants buried in code
  (``ProximityParams.from_spec``).
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import Enum
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from mnq_research.data_contracts import MinuteStatus

NEW_YORK = ZoneInfo("America/New_York")
TICK = Decimal("0.25")
ONE_MINUTE = pd.Timedelta(minutes=1)
RTH_OPEN_NY = dt.time(9, 30)
RTH_CLOSE_NY = dt.time(16, 0)
OPENING_RANGE_END_NY = dt.time(9, 45)
NEW_ENTRY_START_NY = dt.time(9, 45)
NEW_ENTRY_CUTOFF_NY = dt.time(11, 30)
EMERGENCY_FLATTEN_NY = dt.time(15, 55)
# PRIOR_RTH_CLOSE: a closing observation may be at most five minutes old, so only
# bars stamped 15:55-15:59 can supply it (single source of truth for both rules).
MAX_PRIOR_CLOSE_STALENESS = pd.Timedelta(minutes=5)
MAX_PRIOR_SESSION_LOOKBACK_DAYS = 14


class LevelType(str, Enum):
    """The seven, and only seven, structural level types active in B0."""

    PRIOR_RTH_HIGH = "PRIOR_RTH_HIGH"
    PRIOR_RTH_LOW = "PRIOR_RTH_LOW"
    PRIOR_RTH_CLOSE = "PRIOR_RTH_CLOSE"
    OVERNIGHT_HIGH = "OVERNIGHT_HIGH"
    OVERNIGHT_LOW = "OVERNIGHT_LOW"
    OPENING_RANGE_HIGH = "OPENING_RANGE_HIGH"
    OPENING_RANGE_LOW = "OPENING_RANGE_LOW"


B0_LEVEL_TYPES: tuple[LevelType, ...] = tuple(LevelType)


class SessionClass(str, Enum):
    NORMAL = "NORMAL"
    EARLY_CLOSE = "EARLY_CLOSE"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class SessionInfo:
    """One entry of the versioned CME session calendar (a Phase 2 artifact)."""

    trade_date: dt.date
    classification: SessionClass
    globex_open_utc: pd.Timestamp | None = None
    open_verified: bool = False


SessionCalendar = Mapping[dt.date, SessionInfo]


class NewEntryNotPermitted(Exception):
    """Raised when levels are requested for a new entry that the rules forbid."""


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def ny_time(date: dt.date, time: dt.time) -> pd.Timestamp:
    """A New York wall-clock time on ``date`` as a UTC timestamp (tz database rules)."""
    return pd.Timestamp(dt.datetime.combine(date, time), tz=NEW_YORK).tz_convert("UTC")


@dataclass(frozen=True)
class Window:
    name: str
    start_utc: pd.Timestamp  # inclusive
    end_utc: pd.Timestamp  # exclusive

    def expected_minutes(self) -> pd.DatetimeIndex:
        return pd.date_range(self.start_utc, self.end_utc - ONE_MINUTE, freq="min")


def rth_window(date: dt.date) -> Window:
    return Window("PRIOR_RTH", ny_time(date, RTH_OPEN_NY), ny_time(date, RTH_CLOSE_NY))


def opening_range_window(date: dt.date) -> Window:
    return Window("OPENING_RANGE", ny_time(date, RTH_OPEN_NY), ny_time(date, OPENING_RANGE_END_NY))


def prior_normal_rth_date(calendar: SessionCalendar, trade_date: dt.date) -> dt.date | None:
    """Most recent previous NORMAL full RTH session, skipping closed and early-close days.

    Weekends are skipped. A weekday missing from the calendar is unknown, so
    the answer is None (fail closed) rather than a guess.
    """
    day = trade_date
    for _ in range(MAX_PRIOR_SESSION_LOOKBACK_DAYS):
        day -= dt.timedelta(days=1)
        if day.weekday() >= 5:
            continue
        info = calendar.get(day)
        if info is None:
            return None
        if info.classification is SessionClass.NORMAL:
            return day
    return None


def overnight_window(calendar: SessionCalendar, trade_date: dt.date) -> tuple[Window | None, str | None]:
    info = calendar.get(trade_date)
    if info is None or info.classification is not SessionClass.NORMAL:
        return None, "CURRENT_DATE_NOT_A_NORMAL_SESSION"
    if not info.open_verified or info.globex_open_utc is None:
        return None, "SESSION_OPEN_UNVERIFIED"
    return Window("OVERNIGHT", info.globex_open_utc, ny_time(trade_date, RTH_OPEN_NY)), None


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuralLevel:
    level_type: LevelType
    trade_date: dt.date
    source_contract: str
    source_window: str
    window_start_utc: pd.Timestamp | None
    window_end_utc: pd.Timestamp | None
    available_at_utc: pd.Timestamp | None  # calculation time: the window end
    price: float | None = None
    unavailable_reason: str | None = None
    component_bar_count: int = 0
    source_instrument_ids: tuple[int, ...] = ()
    data_manifest_ref: str | None = None
    source_bar_start_utc: pd.Timestamp | None = None  # PRIOR_RTH_CLOSE only
    staleness_seconds: float | None = None  # PRIOR_RTH_CLOSE only
    # Chronological state history (untouched, touched, ...). Transition rules
    # are defined in a later round; Round 8 only guarantees the level persists.
    state_history: tuple[tuple[pd.Timestamp, str], ...] = ()

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None


def _price_problem(value: float) -> str | None:
    if not math.isfinite(value) or value <= 0:
        return "INVALID_PRICE_NOT_FINITE_POSITIVE"
    if Decimal(str(value)) % TICK != 0:
        return "INVALID_PRICE_OFF_TICK"
    return None


def _missing_problem(
    window: Window,
    present: pd.DatetimeIndex,
    statuses: Mapping[pd.Timestamp, MinuteStatus],
    allow_verified_no_trade: bool,
) -> str | None:
    missing = window.expected_minutes().difference(present)
    if missing.empty:
        return None
    if not allow_verified_no_trade:
        return f"REQUIRED_MINUTE_MISSING ({len(missing)} of {len(window.expected_minutes())})"
    kinds = {statuses.get(m, MinuteStatus.UNEXPLAINED_MISSING_MINUTE) for m in missing}
    if MinuteStatus.KNOWN_DATA_OUTAGE in kinds:
        return "KNOWN_DATA_OUTAGE_IN_WINDOW"
    if MinuteStatus.UNEXPLAINED_MISSING_MINUTE in kinds:
        return "UNEXPLAINED_MISSING_MINUTE_IN_WINDOW"
    return None  # only verified no-trade minutes are absent


def _window_rows(bars: pd.DataFrame, contract: str, window: Window) -> pd.DataFrame:
    ts = bars["timestamp_utc"]
    rows = bars[(bars["contract"] == contract) & (ts >= window.start_utc) & (ts < window.end_utc)]
    return rows.sort_values("timestamp_utc")


def _base(level_type: LevelType, trade_date: dt.date, contract: str, window: Window | None, manifest: str | None) -> dict:
    return {
        "level_type": level_type,
        "trade_date": trade_date,
        "source_contract": contract,
        "source_window": window.name if window else level_type.value.rsplit("_", 1)[0],
        "window_start_utc": window.start_utc if window else None,
        "window_end_utc": window.end_utc if window else None,
        "available_at_utc": window.end_utc if window else None,
        "data_manifest_ref": manifest,
    }


def _extreme_levels(
    high_type: LevelType,
    low_type: LevelType,
    trade_date: dt.date,
    contract: str,
    window: Window | None,
    window_problem: str | None,
    bars: pd.DataFrame,
    statuses: Mapping[pd.Timestamp, MinuteStatus],
    allow_verified_no_trade: bool,
    manifest: str | None,
) -> list[StructuralLevel]:
    if window is None:
        return [StructuralLevel(**_base(t, trade_date, contract, None, manifest), unavailable_reason=window_problem) for t in (high_type, low_type)]
    rows = _window_rows(bars, contract, window)
    problem = window_problem or ("NO_BARS_IN_WINDOW" if rows.empty else None)
    problem = problem or _missing_problem(window, pd.DatetimeIndex(rows["timestamp_utc"]), statuses, allow_verified_no_trade)
    results = []
    for level_type, value in ((high_type, None if problem else float(rows["high"].max())), (low_type, None if problem else float(rows["low"].min()))):
        reason = problem or _price_problem(value)
        results.append(
            StructuralLevel(
                **_base(level_type, trade_date, contract, window, manifest),
                price=None if reason else value,
                unavailable_reason=reason,
                component_bar_count=len(rows),
                source_instrument_ids=tuple(sorted(set(int(i) for i in rows["instrument_id"]))),
            )
        )
    return results


def _prior_close_level(
    trade_date: dt.date, contract: str, window: Window | None, window_problem: str | None,
    bars: pd.DataFrame, statuses: Mapping[pd.Timestamp, MinuteStatus], manifest: str | None,
) -> StructuralLevel:
    base = _base(LevelType.PRIOR_RTH_CLOSE, trade_date, contract, window, manifest)
    if window is None:
        return StructuralLevel(**base, unavailable_reason=window_problem)
    lookback = Window("PRIOR_RTH_CLOSE_LOOKBACK", window.end_utc - MAX_PRIOR_CLOSE_STALENESS, window.end_utc)
    rows = _window_rows(bars, contract, lookback)
    if rows.empty:
        return StructuralLevel(**base, unavailable_reason="NO_CLOSING_OBSERVATION_WITHIN_5_MINUTES_OF_16_00")
    last = rows.iloc[-1]
    last_start = last["timestamp_utc"]
    # Every minute after the source bar up to 15:59 must be a verified no-trade minute.
    later = pd.date_range(last_start + ONE_MINUTE, window.end_utc - ONE_MINUTE, freq="min")
    if any(statuses.get(m, MinuteStatus.UNEXPLAINED_MISSING_MINUTE) is not MinuteStatus.VERIFIED_NO_TRADE_MINUTE for m in later):
        return StructuralLevel(**base, unavailable_reason="FINAL_MINUTES_NOT_VERIFIED_NO_TRADE")
    staleness = window.end_utc - (last_start + ONE_MINUTE)  # source bar close -> 16:00
    if staleness > MAX_PRIOR_CLOSE_STALENESS:
        return StructuralLevel(**base, unavailable_reason="PRIOR_CLOSE_TOO_STALE")
    value = float(last["close"])
    reason = _price_problem(value)
    return StructuralLevel(
        **base,
        price=None if reason else value,
        unavailable_reason=reason,
        component_bar_count=1,
        source_instrument_ids=(int(last["instrument_id"]),),
        source_bar_start_utc=last_start,
        staleness_seconds=staleness.total_seconds(),
    )


# ---------------------------------------------------------------------------
# Proximity tolerance and clustering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProximityParams:
    fraction_of_prior_rth_range: Decimal
    minimum_points: Decimal
    maximum_points: Decimal

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "ProximityParams":
        params = spec["structural_levels"]["level_proximity_tolerance"]["parameters"]
        return cls(
            Decimal(str(params["fraction_of_prior_rth_range"])),
            Decimal(str(params["minimum_points"])),
            Decimal(str(params["maximum_points"])),
        )


def proximity_tolerance(prior_high: float | None, prior_low: float | None, params: ProximityParams) -> Decimal | None:
    """clamp(fraction x prior-RTH range, min, max), rounded UP to the 0.25 tick.

    Returns None (unavailable) when the range is missing or not positive.
    """
    if prior_high is None or prior_low is None:
        return None
    prior_range = Decimal(str(prior_high)) - Decimal(str(prior_low))
    if prior_range <= 0:
        return None
    raw = params.fraction_of_prior_rth_range * prior_range
    clamped = min(max(raw, params.minimum_points), params.maximum_points)
    return (clamped / TICK).to_integral_value(rounding=ROUND_CEILING) * TICK


@dataclass(frozen=True)
class LevelCluster:
    cluster_id: str
    lower_boundary: float
    upper_boundary: float
    representative_price: float  # median of constituent prices (informational)
    constituents: tuple[StructuralLevel, ...]
    source_contract: str
    created_at_utc: pd.Timestamp

    @property
    def constituent_level_types(self) -> tuple[LevelType, ...]:
        return tuple(c.level_type for c in self.constituents)

    @property
    def constituent_prices(self) -> tuple[float, ...]:
        return tuple(c.price for c in self.constituents)  # type: ignore[misc]

    @property
    def source_windows(self) -> tuple[str, ...]:
        return tuple(c.source_window for c in self.constituents)


def cluster_levels(
    levels: Iterable[StructuralLevel], tolerance: Decimal, created_at_utc: pd.Timestamp, label: str = ""
) -> tuple[LevelCluster, ...]:
    """Transitive single-linkage clustering: adjacent gap <= tolerance joins a cluster.

    ``label`` distinguishes cluster versions built at different times (e.g. PRE0930-, 0945-).
    """
    available = sorted((lv for lv in levels if lv.available), key=lambda lv: (lv.price, lv.level_type.value))
    contracts = {lv.source_contract for lv in available}
    if len(contracts) > 1:
        raise ValueError(f"levels from more than one contract cannot be clustered: {sorted(contracts)}")
    groups: list[list[StructuralLevel]] = []
    for level in available:
        if groups and Decimal(str(level.price)) - Decimal(str(groups[-1][-1].price)) <= tolerance:
            groups[-1].append(level)
        else:
            groups.append([level])
    clusters = []
    for number, group in enumerate(groups, start=1):
        prices = [lv.price for lv in group]
        first = group[0]
        clusters.append(
            LevelCluster(
                cluster_id=f"{first.trade_date.isoformat()}:{first.source_contract}:{label}C{number}",
                lower_boundary=min(prices),
                upper_boundary=max(prices),
                representative_price=float(statistics.median(prices)),
                constituents=tuple(group),
                source_contract=first.source_contract,
                created_at_utc=created_at_utc,
            )
        )
    return tuple(clusters)


# ---------------------------------------------------------------------------
# Daily level set and use rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyLevelSet:
    trade_date: dt.date
    contract: str
    levels: tuple[StructuralLevel, ...]
    proximity_tolerance_points: Decimal | None
    new_entry_blockers: tuple[str, ...]

    def level(self, level_type: LevelType) -> StructuralLevel:
        return next(lv for lv in self.levels if lv.level_type is level_type)

    def available_levels(self, as_of_utc: pd.Timestamp) -> tuple[StructuralLevel, ...]:
        """Levels that exist and were knowable at ``as_of_utc`` (no look-ahead)."""
        return tuple(
            lv for lv in self.levels if lv.available and lv.available_at_utc is not None and lv.available_at_utc <= as_of_utc
        )


def compute_daily_levels(
    bars: pd.DataFrame,
    trade_date: dt.date,
    contract: str,
    calendar: SessionCalendar,
    params: ProximityParams,
    missing_minute_status: Mapping[pd.Timestamp, MinuteStatus] | None = None,
    data_manifest_ref: str | None = None,
) -> DailyLevelSet:
    """Compute all seven B0 levels for one strategy date and its designated contract.

    ``missing_minute_status`` maps absent minute timestamps (UTC, for this
    contract) to their ingestion classification; absent entries default to
    UNEXPLAINED_MISSING_MINUTE.
    """
    statuses = missing_minute_status or {}
    blockers: list[str] = []
    today = calendar.get(trade_date)
    if today is None or today.classification is not SessionClass.NORMAL:
        blockers.append("CURRENT_DATE_NOT_A_NORMAL_FULL_SESSION")

    prior_date = prior_normal_rth_date(calendar, trade_date)
    prior_window = rth_window(prior_date) if prior_date else None
    prior_problem = None if prior_date else "PRIOR_NORMAL_SESSION_UNKNOWN"
    overnight, overnight_problem = overnight_window(calendar, trade_date)
    opening = opening_range_window(trade_date) if not blockers else None
    opening_problem = None if opening else "CURRENT_DATE_NOT_A_NORMAL_FULL_SESSION"

    levels = [
        *_extreme_levels(LevelType.PRIOR_RTH_HIGH, LevelType.PRIOR_RTH_LOW, trade_date, contract, prior_window,
                         prior_problem, bars, statuses, True, data_manifest_ref),
        _prior_close_level(trade_date, contract, prior_window, prior_problem, bars, statuses, data_manifest_ref),
        *_extreme_levels(LevelType.OVERNIGHT_HIGH, LevelType.OVERNIGHT_LOW, trade_date, contract, overnight,
                         overnight_problem, bars, statuses, True, data_manifest_ref),
        *_extreme_levels(LevelType.OPENING_RANGE_HIGH, LevelType.OPENING_RANGE_LOW, trade_date, contract, opening,
                         opening_problem, bars, statuses, False, data_manifest_ref),
    ]
    by_type = {lv.level_type: lv for lv in levels}
    if not (by_type[LevelType.OPENING_RANGE_HIGH].available and by_type[LevelType.OPENING_RANGE_LOW].available):
        blockers.append("OPENING_RANGE_UNAVAILABLE")
    tolerance = proximity_tolerance(by_type[LevelType.PRIOR_RTH_HIGH].price, by_type[LevelType.PRIOR_RTH_LOW].price, params)
    if tolerance is None:
        blockers.append("PROXIMITY_TOLERANCE_UNAVAILABLE")
    return DailyLevelSet(trade_date, contract, tuple(levels), tolerance, tuple(dict.fromkeys(blockers)))


def levels_for_new_entry(daily: DailyLevelSet, decision_time_utc: pd.Timestamp) -> tuple[StructuralLevel, ...]:
    """Levels usable by a NEW-entry decision; raises when new entries are not permitted."""
    if daily.new_entry_blockers:
        raise NewEntryNotPermitted(f"{daily.trade_date}: " + ", ".join(daily.new_entry_blockers))
    local = decision_time_utc.tz_convert(NEW_YORK)
    if local.date() != daily.trade_date:
        raise NewEntryNotPermitted(f"decision {local} is not on trade date {daily.trade_date}")
    if local.time() < NEW_ENTRY_START_NY:
        raise NewEntryNotPermitted(f"decision {local.time()} is before the 09:45 new-entry window start")
    if local.time() >= NEW_ENTRY_CUTOFF_NY:
        raise NewEntryNotPermitted(f"decision {local.time()} is at/after the 11:30 new-entry cutoff")
    return daily.available_levels(decision_time_utc)


@dataclass(frozen=True)
class EntryLevelSnapshot:
    """Levels frozen at entry, kept for management and audit until the position closes."""

    trade_date: dt.date
    entry_time_utc: pd.Timestamp
    levels: tuple[StructuralLevel, ...]

    def usable_for_management(self, now_utc: pd.Timestamp, flat_confirmed: bool) -> bool:
        if flat_confirmed:
            return False
        return now_utc < ny_time(self.trade_date, EMERGENCY_FLATTEN_NY)


def snapshot_at_entry(daily: DailyLevelSet, entry_time_utc: pd.Timestamp) -> EntryLevelSnapshot:
    return EntryLevelSnapshot(daily.trade_date, entry_time_utc, levels_for_new_entry(daily, entry_time_utc))
