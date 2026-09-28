"""Zone interaction states for Baseline B0 (Rule Freeze Round 9).

COMPONENT OF A DRAFT SPECIFICATION. Implements only the Round 9 rules in
``acceptance_rejection_breakout`` and ``level_states``: approach, touch,
breach, acceptance and rejection of structural zones, plus zone
initialisation at 09:30 / 09:45. It contains NO confirmation, entry, stop,
target or sizing logic and is not connected to any backtest.

Plain-English description: docs/LEVEL_STATES.md.

Rules of the implementation:

* Every distance comes from the rule file (``StateParams.from_spec``). This
  module deliberately contains no numeric trading constants (a test checks
  this).
* Only completed, eligible five-minute decision bars cause transitions.
  Incomplete bars, missing bars and news blackouts interrupt sequences.
* History is append-only. ``current_state`` is a summary chosen by the
  frozen precedence; no event is ever erased.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

import pandas as pd

from mnq_research.structural_levels import (
    OPENING_RANGE_END_NY,
    RTH_OPEN_NY,
    TICK,
    DailyLevelSet,
    LevelCluster,
    LevelType,
    cluster_levels,
    ny_time,
)

APPROACH_METHOD = "level_proximity_tolerance_points"
OPENING_RANGE_TYPES = frozenset({LevelType.OPENING_RANGE_HIGH, LevelType.OPENING_RANGE_LOW})


class ZoneState(str, Enum):
    UNTOUCHED = "UNTOUCHED"
    APPROACHED = "APPROACHED"
    TOUCHED = "TOUCHED"
    BREACHED_ABOVE = "BREACHED_ABOVE"
    BREACHED_BELOW = "BREACHED_BELOW"
    TWO_SIDED_BREACH = "TWO_SIDED_BREACH"
    REJECTED_UPWARD_ATTEMPT = "REJECTED_UPWARD_ATTEMPT"
    REJECTED_DOWNWARD_ATTEMPT = "REJECTED_DOWNWARD_ATTEMPT"
    ACCEPTED_ABOVE = "ACCEPTED_ABOVE"
    ACCEPTED_BELOW = "ACCEPTED_BELOW"


class ZoneEventType(str, Enum):
    ZONE_INITIALIZED = "ZONE_INITIALIZED"
    ZONE_SUPERSEDED = "ZONE_SUPERSEDED"
    APPROACHED_FROM_BELOW = "APPROACHED_FROM_BELOW"
    APPROACHED_FROM_ABOVE = "APPROACHED_FROM_ABOVE"
    TOUCH_FROM_BELOW = "TOUCH_FROM_BELOW"
    TOUCH_FROM_ABOVE = "TOUCH_FROM_ABOVE"
    TOUCH_ORIGIN_UNKNOWN = "TOUCH_ORIGIN_UNKNOWN"
    BREACHED_ABOVE = "BREACHED_ABOVE"
    BREACHED_BELOW = "BREACHED_BELOW"
    TWO_SIDED_BREACH = "TWO_SIDED_BREACH"
    GAPPED_ABOVE_ZONE = "GAPPED_ABOVE_ZONE"
    GAPPED_BELOW_ZONE = "GAPPED_BELOW_ZONE"
    ACCEPTED_ABOVE = "ACCEPTED_ABOVE"
    ACCEPTED_BELOW = "ACCEPTED_BELOW"
    REJECTION_WINDOW_OPENED = "REJECTION_WINDOW_OPENED"
    REJECTED_UPWARD_ATTEMPT = "REJECTED_UPWARD_ATTEMPT"
    REJECTED_DOWNWARD_ATTEMPT = "REJECTED_DOWNWARD_ATTEMPT"
    REJECTION_WINDOW_EXPIRED = "REJECTION_WINDOW_EXPIRED"
    REJECTION_WINDOW_INVALIDATED = "REJECTION_WINDOW_INVALIDATED"
    DATA_INTERRUPTION = "DATA_INTERRUPTION"
    BLACKOUT_RESET = "BLACKOUT_RESET"


class PriceRelation(str, Enum):
    BELOW_ZONE = "BELOW_ZONE"
    INSIDE_ZONE = "INSIDE_ZONE"
    ABOVE_ZONE = "ABOVE_ZONE"


class Origin(str, Enum):
    BELOW = "BELOW"
    ABOVE = "ABOVE"
    UNKNOWN = "UNKNOWN"


# current_state precedence when one bar satisfies several events (highest first).
_STATE_PRECEDENCE: tuple[tuple[ZoneEventType, ZoneState], ...] = (
    (ZoneEventType.ACCEPTED_ABOVE, ZoneState.ACCEPTED_ABOVE),
    (ZoneEventType.ACCEPTED_BELOW, ZoneState.ACCEPTED_BELOW),
    (ZoneEventType.REJECTED_UPWARD_ATTEMPT, ZoneState.REJECTED_UPWARD_ATTEMPT),
    (ZoneEventType.REJECTED_DOWNWARD_ATTEMPT, ZoneState.REJECTED_DOWNWARD_ATTEMPT),
    (ZoneEventType.TWO_SIDED_BREACH, ZoneState.TWO_SIDED_BREACH),
    (ZoneEventType.BREACHED_ABOVE, ZoneState.BREACHED_ABOVE),
    (ZoneEventType.BREACHED_BELOW, ZoneState.BREACHED_BELOW),
    (ZoneEventType.TOUCH_FROM_BELOW, ZoneState.TOUCHED),
    (ZoneEventType.TOUCH_FROM_ABOVE, ZoneState.TOUCHED),
    (ZoneEventType.TOUCH_ORIGIN_UNKNOWN, ZoneState.TOUCHED),
    (ZoneEventType.APPROACHED_FROM_BELOW, ZoneState.APPROACHED),
    (ZoneEventType.APPROACHED_FROM_ABOVE, ZoneState.APPROACHED),
)


class ZoneStateValidationError(ValueError):
    """A bar or zone is internally impossible (e.g. approaches from both sides)."""


@dataclass(frozen=True)
class StateParams:
    approach_distance_method: str
    breach_distance_ticks: int
    acceptance_distance_ticks: int
    acceptance_consecutive_closes: int
    rejection_close_distance_ticks: int
    rejection_window_complete_bars: int

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "StateParams":
        p = spec["level_states"]["parameters"]
        params = cls(
            p["approach_distance_method"],
            p["breach_distance_ticks"],
            p["acceptance_distance_ticks"],
            p["acceptance_consecutive_closes"],
            p["rejection_close_distance_ticks"],
            p["rejection_window_complete_bars"],
        )
        if params.approach_distance_method != APPROACH_METHOD:
            raise ValueError(f"unsupported approach_distance_method {params.approach_distance_method!r}")
        return params

    @property
    def breach_points(self) -> Decimal:
        return self.breach_distance_ticks * TICK

    @property
    def acceptance_points(self) -> Decimal:
        return self.acceptance_distance_ticks * TICK

    @property
    def rejection_points(self) -> Decimal:
        return self.rejection_close_distance_ticks * TICK


@dataclass(frozen=True)
class DecisionBar:
    """A five-minute decision bar as produced by the aggregation rule."""

    start_utc: pd.Timestamp
    end_utc: pd.Timestamp
    high: float | None
    low: float | None
    close: float | None
    complete: bool = True
    overlaps_blackout: bool = False


@dataclass(frozen=True)
class ZoneEvent:
    timestamp_utc: pd.Timestamp
    event: ZoneEventType
    initialization_replay: bool = False
    detail: str = ""


@dataclass
class _RejectionWindow:
    direction: str  # "UP" or "DOWN"
    opened_at_utc: pd.Timestamp
    bars_elapsed: int


def _d(value: float) -> Decimal:
    return Decimal(str(value))


class ZoneTracker:
    """Append-only interaction history and current state for one zone version."""

    def __init__(
        self,
        cluster: LevelCluster,
        params: StateParams,
        approach_distance: Decimal,
        initialized_at_utc: pd.Timestamp,
        predecessor_cluster_ids: tuple[str, ...] = (),
    ):
        if cluster.lower_boundary > cluster.upper_boundary:
            raise ZoneStateValidationError(f"{cluster.cluster_id}: lower boundary above upper boundary")
        self.cluster = cluster
        self.params = params
        self.approach_distance = approach_distance
        self.initialized_at_utc = initialized_at_utc
        self.predecessor_cluster_ids = predecessor_cluster_ids
        self.parent_constituent_types = cluster.constituent_level_types
        self.superseded_by: str | None = None
        self._history: list[ZoneEvent] = [ZoneEvent(initialized_at_utc, ZoneEventType.ZONE_INITIALIZED)]
        self._fresh_state()
        self._last_bar_end: pd.Timestamp = initialized_at_utc
        self._in_blackout = False
        self.most_recent_transition_timestamp: pd.Timestamp | None = None

    # -- public read-only views ------------------------------------------------
    @property
    def zone_id(self) -> str:
        return self.cluster.cluster_id

    @property
    def lower(self) -> Decimal:
        return _d(self.cluster.lower_boundary)

    @property
    def upper(self) -> Decimal:
        return _d(self.cluster.upper_boundary)

    @property
    def history(self) -> tuple[ZoneEvent, ...]:
        return tuple(self._history)

    @property
    def active_rejection_window(self) -> _RejectionWindow | None:
        return self._window

    def events(self, event: ZoneEventType) -> list[ZoneEvent]:
        return [e for e in self._history if e.event is event]

    # -- state helpers -----------------------------------------------------------
    def _fresh_state(self) -> None:
        self.current_state = ZoneState.UNTOUCHED
        self.current_price_relation: PriceRelation | None = None
        self.interaction_origin = Origin.UNKNOWN
        self.first_interaction_timestamp: pd.Timestamp | None = None
        self.consecutive_acceptance_closes_above = 0
        self.consecutive_acceptance_closes_below = 0
        self._window: _RejectionWindow | None = None
        self._last_clear_outside: Origin = Origin.UNKNOWN
        self._last_close: Decimal | None = None

    def _record(self, events: list[ZoneEvent]) -> tuple[ZoneEvent, ...]:
        self._history.extend(events)
        return tuple(events)

    def _end_window(self, when: pd.Timestamp, event: ZoneEventType, detail: str, replay: bool) -> list[ZoneEvent]:
        if self._window is None:
            return []
        self._window = None
        return [ZoneEvent(when, event, replay, detail)]

    def _interrupt(self, when: pd.Timestamp, detail: str, replay: bool) -> list[ZoneEvent]:
        self.consecutive_acceptance_closes_above = 0
        self.consecutive_acceptance_closes_below = 0
        return [ZoneEvent(when, ZoneEventType.DATA_INTERRUPTION, replay, detail)] + self._end_window(
            when, ZoneEventType.REJECTION_WINDOW_INVALIDATED, detail, replay
        )

    # -- external resets ---------------------------------------------------------
    def reset_for_blackout(self, blackout_start_utc: pd.Timestamp) -> tuple[ZoneEvent, ...]:
        """News-blackout start: reset counters and pending state; setups must form fresh after."""
        events = self._end_window(blackout_start_utc, ZoneEventType.REJECTION_WINDOW_INVALIDATED, "NEWS_BLACKOUT", False)
        events.append(ZoneEvent(blackout_start_utc, ZoneEventType.BLACKOUT_RESET))
        self._fresh_state()
        self._in_blackout = True
        return self._record(events)

    def end_trading_window(self, when_utc: pd.Timestamp) -> tuple[ZoneEvent, ...]:
        return self._record(self._end_window(when_utc, ZoneEventType.REJECTION_WINDOW_INVALIDATED, "TRADING_WINDOW_CLOSED", False))

    def supersede(self, when_utc: pd.Timestamp, successor_id: str) -> None:
        self.superseded_by = successor_id
        self._record([ZoneEvent(when_utc, ZoneEventType.ZONE_SUPERSEDED, False, successor_id)])

    # -- main transition ----------------------------------------------------------
    def process(self, bar: DecisionBar, initialization_replay: bool = False) -> tuple[ZoneEvent, ...]:
        if self.superseded_by is not None:
            raise ZoneStateValidationError(f"{self.zone_id} was superseded by {self.superseded_by}")
        if bar.end_utc <= self.initialized_at_utc:
            return ()  # bars before the zone existed never count
        replay = initialization_replay
        events: list[ZoneEvent] = []
        if bar.start_utc != self._last_bar_end:
            events += self._interrupt(bar.start_utc, "MISSING_DECISION_BAR_IN_SEQUENCE", replay)
        self._last_bar_end = bar.end_utc

        if bar.overlaps_blackout:
            if not self._in_blackout:
                self._record(events)
                return tuple(events) + self.reset_for_blackout(bar.start_utc)
            return self._record(events)
        self._in_blackout = False
        if not bar.complete:
            return self._record(events + self._interrupt(bar.end_utc, "INCOMPLETE_DECISION_BAR", replay))

        events += self._evaluate(bar, replay)
        return self._record(events)

    def _evaluate(self, bar: DecisionBar, replay: bool) -> list[ZoneEvent]:
        lo, hi = self.lower, self.upper
        high, low, close = _d(bar.high), _d(bar.low), _d(bar.close)
        if high < low or not low <= close <= high:
            raise ZoneStateValidationError(f"{self.zone_id}: impossible OHLC in bar ending {bar.end_utc}")
        p, t = self.params, bar.end_utc
        flags: list[ZoneEventType] = []
        details: dict[ZoneEventType, str] = {}

        self.current_price_relation = (
            PriceRelation.BELOW_ZONE if close < lo else PriceRelation.ABOVE_ZONE if close > hi else PriceRelation.INSIDE_ZONE
        )
        touched = high >= lo and low <= hi
        approach_below = not touched and lo - self.approach_distance <= high < lo
        approach_above = not touched and hi < low <= hi + self.approach_distance
        if approach_below and approach_above:
            raise ZoneStateValidationError(f"{self.zone_id}: bar ending {t} approaches from both sides")
        breach_above = high >= hi + p.breach_points
        breach_below = low <= lo - p.breach_points
        origin = self._last_clear_outside

        if approach_below:
            flags.append(ZoneEventType.APPROACHED_FROM_BELOW)
        if approach_above:
            flags.append(ZoneEventType.APPROACHED_FROM_ABOVE)
        if self._last_close is not None and not touched:
            if self._last_close < lo and low > hi:
                flags.append(ZoneEventType.GAPPED_ABOVE_ZONE)
            if self._last_close > hi and high < lo:
                flags.append(ZoneEventType.GAPPED_BELOW_ZONE)
        if touched:
            flags.append(
                {Origin.BELOW: ZoneEventType.TOUCH_FROM_BELOW, Origin.ABOVE: ZoneEventType.TOUCH_FROM_ABOVE}.get(
                    origin, ZoneEventType.TOUCH_ORIGIN_UNKNOWN
                )
            )
        if breach_above:
            flags.append(ZoneEventType.BREACHED_ABOVE)
        if breach_below:
            flags.append(ZoneEventType.BREACHED_BELOW)
        if breach_above and breach_below:
            flags.append(ZoneEventType.TWO_SIDED_BREACH)
            details[ZoneEventType.TWO_SIDED_BREACH] = "INTRABAR_ORDER_UNKNOWN;REQUIRES_FINER_DATA_REPLAY"

        interaction = touched or breach_above or breach_below
        if interaction and self._window is None:
            self.interaction_origin = origin
            if self.first_interaction_timestamp is None:
                self.first_interaction_timestamp = t
            if origin is Origin.BELOW and (touched or breach_above):
                self._window = _RejectionWindow("UP", t, 0)
            elif origin is Origin.ABOVE and (touched or breach_below):
                self._window = _RejectionWindow("DOWN", t, 0)
            if self._window is not None:
                flags.append(ZoneEventType.REJECTION_WINDOW_OPENED)
                details[ZoneEventType.REJECTION_WINDOW_OPENED] = self._window.direction

        # Closes: acceptance counters.
        self.consecutive_acceptance_closes_above = (
            self.consecutive_acceptance_closes_above + 1 if close >= hi + p.acceptance_points else 0
        )
        self.consecutive_acceptance_closes_below = (
            self.consecutive_acceptance_closes_below + 1 if close <= lo - p.acceptance_points else 0
        )
        accepted_above = self.consecutive_acceptance_closes_above == p.acceptance_consecutive_closes
        accepted_below = self.consecutive_acceptance_closes_below == p.acceptance_consecutive_closes

        # Closes: rejection within the window (interaction bar counts as bar 1).
        closing: list[ZoneEvent] = []
        if self._window is not None:
            self._window.bars_elapsed += 1
            if self._window.direction == "UP" and close <= lo - p.rejection_points:
                flags.append(ZoneEventType.REJECTED_UPWARD_ATTEMPT)
                self._window = None
            elif self._window.direction == "DOWN" and close >= hi + p.rejection_points:
                flags.append(ZoneEventType.REJECTED_DOWNWARD_ATTEMPT)
                self._window = None
        if accepted_above:
            flags.append(ZoneEventType.ACCEPTED_ABOVE)
            if self._window is not None and self._window.direction == "UP":
                closing += self._end_window(t, ZoneEventType.REJECTION_WINDOW_INVALIDATED, "ACCEPTED_ABOVE", replay)
        if accepted_below:
            flags.append(ZoneEventType.ACCEPTED_BELOW)
            if self._window is not None and self._window.direction == "DOWN":
                closing += self._end_window(t, ZoneEventType.REJECTION_WINDOW_INVALIDATED, "ACCEPTED_BELOW", replay)
        if self._window is not None and self._window.bars_elapsed >= p.rejection_window_complete_bars:
            closing += self._end_window(t, ZoneEventType.REJECTION_WINDOW_EXPIRED, "NO_QUALIFYING_CLOSE", replay)

        # Memory for the next bar's origin and gap tests (prior closes only).
        if close <= lo - p.rejection_points:
            self._last_clear_outside = Origin.BELOW
        elif close >= hi + p.rejection_points:
            self._last_clear_outside = Origin.ABOVE
        self._last_close = close

        for event, state in _STATE_PRECEDENCE:
            if event in flags:
                self.current_state = state
                self.most_recent_transition_timestamp = t
                break
        return [ZoneEvent(t, f, replay, details.get(f, "")) for f in flags] + closing


# ---------------------------------------------------------------------------
# Zone book: initialisation at 09:30 and versioning at 09:45
# ---------------------------------------------------------------------------


@dataclass
class ZoneBook:
    active: dict[str, ZoneTracker]
    archived: dict[str, ZoneTracker]
    unavailable_reason: str | None = None


def initialize_zones(daily: DailyLevelSet, pre_open_bars: Iterable[DecisionBar], params: StateParams) -> ZoneBook:
    """Build the day's zones.

    1. Zones of prior-RTH/overnight levels exist at 09:30 and replay the three
       09:30-09:45 decision bars (initialisation only; never an entry).
    2. At 09:45 the full level set (including the opening range) is clustered
       again. Zones containing an opening-range level start fresh (UNTOUCHED);
       any pre-open zone they absorb is archived, not transferred.
    """
    tolerance = daily.proximity_tolerance_points
    if tolerance is None:
        return ZoneBook({}, {}, "PROXIMITY_TOLERANCE_UNAVAILABLE")
    t_open = ny_time(daily.trade_date, RTH_OPEN_NY)
    t_or = ny_time(daily.trade_date, OPENING_RANGE_END_NY)

    pre_levels = [lv for lv in daily.available_levels(t_open) if lv.level_type not in OPENING_RANGE_TYPES]
    pre_clusters = cluster_levels(pre_levels, tolerance, t_open, label="PRE0930-")
    pre = {c.cluster_id: ZoneTracker(c, params, tolerance, t_open) for c in pre_clusters}
    for bar in sorted(pre_open_bars, key=lambda b: b.start_utc):
        if bar.end_utc <= t_or:
            for tracker in pre.values():
                tracker.process(bar, initialization_replay=True)

    active: dict[str, ZoneTracker] = {}
    archived: dict[str, ZoneTracker] = {}
    for cluster in cluster_levels(daily.available_levels(t_or), tolerance, t_or, label="0945-"):
        types = set(cluster.constituent_level_types)
        if types & OPENING_RANGE_TYPES:
            predecessors = tuple(c.cluster_id for c in pre_clusters if types & set(c.constituent_level_types))
            active[cluster.cluster_id] = ZoneTracker(cluster, params, tolerance, t_or, predecessors)
            for pid in predecessors:
                pre[pid].supersede(t_or, cluster.cluster_id)
                archived[pid] = pre[pid]
        else:
            match = next(c for c in pre_clusters if set(c.constituent_level_types) == types)
            active[match.cluster_id] = pre[match.cluster_id]
    return ZoneBook(active, archived)
