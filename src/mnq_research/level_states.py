"""Zone interaction states for Baseline B0 (Rule Freeze Round 9, as amended).

COMPONENT OF A DRAFT SPECIFICATION. Implements only the Round 9 rules in
``acceptance_rejection_breakout`` and ``level_states`` (including the
directional-episode amendment): location, directional arming, interaction
episodes, touch, directional breach, acceptance and rejection of structural
zones, plus zone initialisation at 09:30 / 09:45. It contains NO
confirmation, entry, stop, target or sizing logic and is not connected to
any backtest.

Plain-English description: docs/LEVEL_STATES.md.

Core idea (owner, Round 9 amendment): being above or below a zone is
*location*, not breach or acceptance. A zone must first be *armed* by a
clear-side close, and only a LATER bar can begin a directional *interaction
episode*. Breaches and acceptance count only inside an episode, and only in
its direction.

Rules of the implementation:

* Every distance comes from the rule file (``StateParams.from_spec``). This
  module deliberately contains no numeric trading constants (a test checks
  this).
* Only completed, eligible five-minute decision bars cause transitions.
  Incomplete bars, missing bars and news blackouts remove all executable
  state (arming, episode, counters) but never history.
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
    ARMED_FROM_BELOW = "ARMED_FROM_BELOW"
    ARMED_FROM_ABOVE = "ARMED_FROM_ABOVE"
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
    ARMED_FROM_BELOW = "ARMED_FROM_BELOW"
    ARMED_FROM_ABOVE = "ARMED_FROM_ABOVE"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    ATTEMPT_ENDED = "ATTEMPT_ENDED"
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


class Direction(str, Enum):
    UPWARD = "UPWARD"
    DOWNWARD = "DOWNWARD"


class AttemptStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    ENDED_REARMED_ON_ORIGIN_SIDE = "ENDED_REARMED_ON_ORIGIN_SIDE"
    ENDED_INTERRUPTION = "ENDED_INTERRUPTION"
    ENDED_BLACKOUT = "ENDED_BLACKOUT"
    ENDED_TRADING_WINDOW = "ENDED_TRADING_WINDOW"
    ENDED_ZONE_EXPIRED = "ENDED_ZONE_EXPIRED"


class WindowStatus(str, Enum):
    NOT_OPENED = "NOT_OPENED"  # episode began by approach; no touch/breach/gap yet
    OPEN = "OPEN"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


# current_state precedence among events valid in the same bar (highest first).
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
    (ZoneEventType.ARMED_FROM_BELOW, ZoneState.ARMED_FROM_BELOW),
    (ZoneEventType.ARMED_FROM_ABOVE, ZoneState.ARMED_FROM_ABOVE),
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
    def clear_side_points(self) -> Decimal:
        """Also the rejection close distance (owner: clear_side_distance)."""
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
    attempt_id: int | None = None


@dataclass
class Attempt:
    """One directional interaction episode."""

    attempt_id: int
    direction: Direction
    origin: Origin
    start_timestamp_utc: pd.Timestamp
    armed_timestamp_utc: pd.Timestamp
    armed_close: Decimal
    status: AttemptStatus = AttemptStatus.ACTIVE
    window_status: WindowStatus = WindowStatus.NOT_OPENED
    window_bars_elapsed: int = 0
    acceptance_count: int = 0
    end_timestamp_utc: pd.Timestamp | None = None


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
        self.attempts: list[Attempt] = []
        self._history: list[ZoneEvent] = [ZoneEvent(initialized_at_utc, ZoneEventType.ZONE_INITIALIZED)]
        self._last_bar_end: pd.Timestamp = initialized_at_utc
        self._in_blackout = False
        self.first_interaction_timestamp: pd.Timestamp | None = None
        self.most_recent_transition_timestamp: pd.Timestamp | None = None
        self._clear_executable_state()

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
    def active_attempt(self) -> Attempt | None:
        return self._attempt

    @property
    def active_attempt_direction(self) -> Direction | None:
        return self._attempt.direction if self._attempt else None

    @property
    def attempt_id(self) -> int | None:
        return self._attempt.attempt_id if self._attempt else None

    @property
    def attempt_start_timestamp(self) -> pd.Timestamp | None:
        return self._attempt.start_timestamp_utc if self._attempt else None

    @property
    def attempt_status(self) -> AttemptStatus | None:
        return self.attempts[-1].status if self.attempts else None

    @property
    def active_rejection_window(self) -> bool:
        return self._attempt is not None and self._attempt.window_status is WindowStatus.OPEN

    @property
    def interaction_origin(self) -> Origin:
        return self._attempt.origin if self._attempt else Origin.UNKNOWN

    @property
    def consecutive_acceptance_closes_above(self) -> int:
        return self._attempt.acceptance_count if self.active_attempt_direction is Direction.UPWARD else 0

    @property
    def consecutive_acceptance_closes_below(self) -> int:
        return self._attempt.acceptance_count if self.active_attempt_direction is Direction.DOWNWARD else 0

    def events(self, event: ZoneEventType) -> list[ZoneEvent]:
        return [e for e in self._history if e.event is event]

    # -- executable state ---------------------------------------------------------
    def _clear_executable_state(self) -> None:
        self.current_state = ZoneState.UNTOUCHED
        self.current_price_relation: PriceRelation | None = None
        self.armed_side: Origin | None = None
        self.armed_timestamp: pd.Timestamp | None = None
        self.armed_close: Decimal | None = None
        self._attempt: Attempt | None = None
        self._last_close: Decimal | None = None

    def _end_attempt(self, when: pd.Timestamp, status: AttemptStatus, replay: bool) -> list[ZoneEvent]:
        attempt = self._attempt
        if attempt is None:
            return []
        events = []
        if attempt.window_status is WindowStatus.OPEN and status is not AttemptStatus.REJECTED:
            events.append(ZoneEvent(when, ZoneEventType.REJECTION_WINDOW_INVALIDATED, replay, status.value, attempt.attempt_id))
        attempt.window_status = WindowStatus.CLOSED if attempt.window_status is WindowStatus.OPEN else attempt.window_status
        attempt.status, attempt.end_timestamp_utc = status, when
        events.append(ZoneEvent(when, ZoneEventType.ATTEMPT_ENDED, replay, status.value, attempt.attempt_id))
        self._attempt = None
        return events

    def _reset_all(self, when: pd.Timestamp, status: AttemptStatus, replay: bool) -> list[ZoneEvent]:
        """Remove all executable state (arming, episode, counters); keep history."""
        events = self._end_attempt(when, status, replay)
        self._clear_executable_state()
        return events

    def _record(self, events: list[ZoneEvent]) -> tuple[ZoneEvent, ...]:
        self._history.extend(events)
        return tuple(events)

    # -- external resets ---------------------------------------------------------
    def reset_for_blackout(self, blackout_start_utc: pd.Timestamp) -> tuple[ZoneEvent, ...]:
        events = self._reset_all(blackout_start_utc, AttemptStatus.ENDED_BLACKOUT, False)
        events.append(ZoneEvent(blackout_start_utc, ZoneEventType.BLACKOUT_RESET))
        self._in_blackout = True
        return self._record(events)

    def end_trading_window(self, when_utc: pd.Timestamp) -> tuple[ZoneEvent, ...]:
        return self._record(self._end_attempt(when_utc, AttemptStatus.ENDED_TRADING_WINDOW, False))

    def expire(self, when_utc: pd.Timestamp) -> tuple[ZoneEvent, ...]:
        return self._record(self._end_attempt(when_utc, AttemptStatus.ENDED_ZONE_EXPIRED, False))

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
            events.append(ZoneEvent(bar.start_utc, ZoneEventType.DATA_INTERRUPTION, replay, "MISSING_DECISION_BAR_IN_SEQUENCE"))
            events += self._reset_all(bar.start_utc, AttemptStatus.ENDED_INTERRUPTION, replay)
        self._last_bar_end = bar.end_utc

        if bar.overlaps_blackout:
            if not self._in_blackout:
                self._record(events)
                return tuple(events) + self.reset_for_blackout(bar.start_utc)
            return self._record(events)
        self._in_blackout = False
        if not bar.complete:
            events.append(ZoneEvent(bar.end_utc, ZoneEventType.DATA_INTERRUPTION, replay, "INCOMPLETE_DECISION_BAR"))
            return self._record(events + self._reset_all(bar.end_utc, AttemptStatus.ENDED_INTERRUPTION, replay))

        events += self._evaluate(bar, replay)
        return self._record(events)

    def _evaluate(self, bar: DecisionBar, replay: bool) -> list[ZoneEvent]:
        lo, hi = self.lower, self.upper
        high, low, close = _d(bar.high), _d(bar.low), _d(bar.close)
        if high < low or not low <= close <= high:
            raise ZoneStateValidationError(f"{self.zone_id}: impossible OHLC in bar ending {bar.end_utc}")
        p, t = self.params, bar.end_utc
        clear = p.clear_side_points
        flags: list[tuple[ZoneEventType, str, int | None]] = []

        def flag(event: ZoneEventType, detail: str = "", attempt_id: int | None = None) -> None:
            flags.append((event, detail, attempt_id))

        # --- Location and wick geometry (descriptive) -------------------------
        self.current_price_relation = (
            PriceRelation.BELOW_ZONE if close < lo else PriceRelation.ABOVE_ZONE if close > hi else PriceRelation.INSIDE_ZONE
        )
        touched = high >= lo and low <= hi
        approach_below = not touched and lo - self.approach_distance <= high < lo
        approach_above = not touched and hi < low <= hi + self.approach_distance
        if approach_below and approach_above:
            raise ZoneStateValidationError(f"{self.zone_id}: bar ending {t} approaches from both sides")
        raw_breach_above = high >= hi + p.breach_points
        raw_breach_below = low <= lo - p.breach_points
        prev = self._last_close
        gap_above = prev is not None and prev <= lo - clear and low > hi
        gap_below = prev is not None and prev >= hi + clear and high < lo
        if approach_below:
            flag(ZoneEventType.APPROACHED_FROM_BELOW)
        if approach_above:
            flag(ZoneEventType.APPROACHED_FROM_ABOVE)

        # --- Episode start: needs arming from an EARLIER bar ------------------
        # Arming only exists while no episode is active (see the arming step
        # below), so a new episode can only start when none is running.
        closing: list[ZoneEvent] = []
        start: Direction | None = None
        if self._attempt is None and self.armed_side is Origin.BELOW and (approach_below or touched or gap_above):
            start = Direction.UPWARD
        elif self._attempt is None and self.armed_side is Origin.ABOVE and (approach_above or touched or gap_below):
            start = Direction.DOWNWARD
        if start is not None:
            new_id = len(self.attempts) + 1
            self._attempt = Attempt(
                new_id,
                start,
                self.armed_side,
                t,
                self.armed_timestamp,
                self.armed_close,
            )
            self.attempts.append(self._attempt)
            self.armed_side = self.armed_timestamp = self.armed_close = None  # consumed
            if self.first_interaction_timestamp is None:
                self.first_interaction_timestamp = t
            flag(ZoneEventType.ATTEMPT_STARTED, start.value, new_id)

        attempt = self._attempt
        aid = attempt.attempt_id if attempt else None

        # --- Touch and breach ---------------------------------------------------
        if touched:
            if attempt is None:
                flag(ZoneEventType.TOUCH_ORIGIN_UNKNOWN)
            else:
                flag(ZoneEventType.TOUCH_FROM_BELOW if attempt.direction is Direction.UPWARD else ZoneEventType.TOUCH_FROM_ABOVE, "", aid)
        if raw_breach_above and raw_breach_below:
            flag(ZoneEventType.TWO_SIDED_BREACH, "INTRABAR_ORDER_UNKNOWN;REQUIRES_FINER_DATA_REPLAY", aid)
        if attempt is not None and attempt.direction is Direction.UPWARD:
            if raw_breach_above:
                flag(ZoneEventType.BREACHED_ABOVE, "", aid)
            if gap_above:
                flag(ZoneEventType.GAPPED_ABOVE_ZONE, "ORIGIN_BELOW", aid)
            interacted = touched or raw_breach_above or gap_above
        elif attempt is not None:
            if raw_breach_below:
                flag(ZoneEventType.BREACHED_BELOW, "", aid)
            if gap_below:
                flag(ZoneEventType.GAPPED_BELOW_ZONE, "ORIGIN_ABOVE", aid)
            interacted = touched or raw_breach_below or gap_below
        else:
            interacted = False

        # --- Episode evaluation: window, rejection, acceptance ----------------
        if attempt is not None:
            upward = attempt.direction is Direction.UPWARD
            if attempt.window_status is WindowStatus.NOT_OPENED and interacted:
                attempt.window_status = WindowStatus.OPEN
                flag(ZoneEventType.REJECTION_WINDOW_OPENED, attempt.direction.value, aid)
            if attempt.window_status is WindowStatus.OPEN:
                attempt.window_bars_elapsed += 1
            beyond = close >= hi + p.acceptance_points if upward else close <= lo - p.acceptance_points
            back_on_origin_side = close <= lo - clear if upward else close >= hi + clear
            attempt.acceptance_count = attempt.acceptance_count + 1 if beyond else 0

            if attempt.window_status is WindowStatus.OPEN and back_on_origin_side:
                flag(ZoneEventType.REJECTED_UPWARD_ATTEMPT if upward else ZoneEventType.REJECTED_DOWNWARD_ATTEMPT, "", aid)
                closing += self._end_attempt(t, AttemptStatus.REJECTED, replay)
            elif attempt.acceptance_count >= p.acceptance_consecutive_closes:
                flag(ZoneEventType.ACCEPTED_ABOVE if upward else ZoneEventType.ACCEPTED_BELOW, "", aid)
                closing += self._end_attempt(t, AttemptStatus.ACCEPTED, replay)
            elif attempt.window_status is WindowStatus.EXPIRED and back_on_origin_side:
                closing += self._end_attempt(t, AttemptStatus.ENDED_REARMED_ON_ORIGIN_SIDE, replay)
            elif attempt.window_status is WindowStatus.OPEN and attempt.window_bars_elapsed >= p.rejection_window_complete_bars:
                attempt.window_status = WindowStatus.EXPIRED
                closing.append(ZoneEvent(t, ZoneEventType.REJECTION_WINDOW_EXPIRED, replay, "NO_QUALIFYING_CLOSE", aid))

        # --- Arming from this bar's close (usable only by LATER bars) ---------
        # Pending owner decision (D-025): arming happens only while NO episode is
        # active, so an active attempt's own acceptance closes never arm the
        # opposite side. After an episode ends (even on this bar), closes arm.
        if self._attempt is not None:
            pass
        elif close <= lo - clear:
            if self.armed_side is not Origin.BELOW:
                flag(ZoneEventType.ARMED_FROM_BELOW, str(close))
                self.armed_side, self.armed_timestamp, self.armed_close = Origin.BELOW, t, close
        elif close >= hi + clear:
            if self.armed_side is not Origin.ABOVE:
                flag(ZoneEventType.ARMED_FROM_ABOVE, str(close))
                self.armed_side, self.armed_timestamp, self.armed_close = Origin.ABOVE, t, close
        self._last_close = close

        kinds = [f[0] for f in flags]
        for event, state in _STATE_PRECEDENCE:
            if event in kinds:
                self.current_state = state
                self.most_recent_transition_timestamp = t
                break
        return [ZoneEvent(t, e, replay, d, a) for e, d, a in flags] + closing


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
       09:30-09:45 decision bars (initialisation only; never an entry). The
       replay may arm, start episodes, accept or reject under the full rules.
    2. At 09:45 the full level set (including the opening range) is clustered
       again. Zones containing an opening-range level start fresh: no arming,
       no episode, UNTOUCHED. Any pre-open zone they absorb is archived, not
       transferred.
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
