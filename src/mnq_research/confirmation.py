"""Continuation confirmation for Baseline B0 (Rule Freeze Round 10).

COMPONENT OF A DRAFT SPECIFICATION. Implements only the owner's single B0
confirmation pattern, PULLBACK_HOLD_CONTINUATION:

    acceptance (Round 9)  ->  a later retest-hold bar  ->  the immediately
    following bar closes one tick beyond the hold bar's extreme  ->  CONFIRMED

A confirmation is only a recorded event. It places NO order and defines no
entry price, size, stop, target or management (later rounds). It is not
connected to any backtest.

Rules of the implementation:

* Every distance, count and the bar interval come from the rule file
  (``ConfirmationParams.from_spec``); no numeric trading constants live here.
* One acceptance -> at most one outcome, reached once, never revisited.
* A sequence references exactly one acceptance_id, attempt_id and zone
  version; events from different ones can never be combined.
* Fail closed: interruptions, blackouts, the 11:30 cutoff and zone changes
  end the sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

import pandas as pd

from mnq_research.level_states import AttemptStatus, DecisionBar, ZoneEvent, ZoneEventType, ZoneTracker
from mnq_research.structural_levels import NEW_ENTRY_CUTOFF_NY, NEW_YORK, TICK, ny_time

CONFIRMATION_TYPE = "PULLBACK_HOLD_CONTINUATION"
RETEST_DISTANCE_METHOD = "level_proximity_tolerance_points"
CONTINUATION_REQUIREMENT = "immediately_next_eligible_complete_bar"


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ConfirmationOutcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    INVALIDATED_BY_INTERRUPTION = "INVALIDATED_BY_INTERRUPTION"
    INVALIDATED_BY_BLACKOUT = "INVALIDATED_BY_BLACKOUT"
    INVALIDATED_BY_CUTOFF = "INVALIDATED_BY_CUTOFF"
    INVALIDATED_BY_ZONE_CHANGE = "INVALIDATED_BY_ZONE_CHANGE"


class ConfirmationEventType(str, Enum):
    CONFIRMATION_SEQUENCE_STARTED = "CONFIRMATION_SEQUENCE_STARTED"
    LONG_RETEST_HOLD = "LONG_RETEST_HOLD"
    SHORT_RETEST_HOLD = "SHORT_RETEST_HOLD"
    CONFIRMED_LONG_CONTINUATION = "CONFIRMED_LONG_CONTINUATION"
    CONFIRMED_SHORT_CONTINUATION = "CONFIRMED_SHORT_CONTINUATION"
    CONFIRMATION_FAILED_BEFORE_HOLD = "CONFIRMATION_FAILED_BEFORE_HOLD"
    CONFIRMATION_FAILED_AFTER_HOLD = "CONFIRMATION_FAILED_AFTER_HOLD"
    CONFIRMATION_TIMED_OUT = "CONFIRMATION_TIMED_OUT"
    CONFIRMATION_INVALIDATED = "CONFIRMATION_INVALIDATED"


class IdentityMismatchError(ValueError):
    """An acceptance does not belong to the zone version / attempt it is paired with."""


@dataclass(frozen=True)
class ConfirmationParams:
    confirmation_type: str
    retest_distance_method: str
    opposite_boundary_failure_distance_ticks: int
    retest_hold_close_distance_ticks: int
    continuation_break_distance_ticks: int
    max_bars_after_acceptance: int
    continuation_bar_requirement: str
    decision_bar_interval: pd.Timedelta

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "ConfirmationParams":
        conf = spec["confirmation"]
        p = conf["parameters"]
        params = cls(
            p["confirmation_type"],
            p["retest_distance_method"],
            p["opposite_boundary_failure_distance_ticks"],
            p["retest_hold_close_distance_ticks"],
            p["continuation_break_distance_ticks"],
            conf["max_bars_after_acceptance"],
            p["continuation_bar_requirement"],
            pd.Timedelta(spec["decision_clock"]["decision_bar_interval"]["interval"]),
        )
        for value, expected in (
            (params.confirmation_type, CONFIRMATION_TYPE),
            (params.retest_distance_method, RETEST_DISTANCE_METHOD),
            (params.continuation_bar_requirement, CONTINUATION_REQUIREMENT),
        ):
            if value != expected:
                raise ValueError(f"unsupported confirmation setting {value!r} (B0 implements only {expected!r})")
        return params

    @property
    def opposite_failure_points(self) -> Decimal:
        return self.opposite_boundary_failure_distance_ticks * TICK

    @property
    def hold_close_points(self) -> Decimal:
        return self.retest_hold_close_distance_ticks * TICK

    @property
    def continuation_points(self) -> Decimal:
        return self.continuation_break_distance_ticks * TICK


@dataclass(frozen=True)
class ConfirmationEvent:
    timestamp_utc: pd.Timestamp
    event: ConfirmationEventType
    acceptance_id: str
    attempt_id: int
    zone_version_id: str
    bar_index: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class RetestHold:
    timestamp_utc: pd.Timestamp
    bar_index: int
    high: Decimal
    low: Decimal
    close: Decimal


def _d(value: float) -> Decimal:
    return Decimal(str(value))


class ConfirmationSequence:
    """The confirmation lifecycle of exactly one acceptance."""

    def __init__(self, zone: ZoneTracker, acceptance: ZoneEvent, params: ConfirmationParams):
        if acceptance.event not in (ZoneEventType.ACCEPTED_ABOVE, ZoneEventType.ACCEPTED_BELOW):
            raise IdentityMismatchError(f"{acceptance.event} is not an acceptance event")
        if not any(e is acceptance for e in zone.history):
            raise IdentityMismatchError(f"acceptance {acceptance.acceptance_id} is not in zone {zone.zone_id}'s history")
        attempt = next((a for a in zone.attempts if a.attempt_id == acceptance.attempt_id), None)
        if attempt is None or attempt.status is not AttemptStatus.ACCEPTED or attempt.acceptance_id != acceptance.acceptance_id:
            raise IdentityMismatchError(
                f"acceptance {acceptance.acceptance_id} does not match attempt {acceptance.attempt_id} of zone {zone.zone_id}"
            )
        if zone.superseded_by is not None:
            raise IdentityMismatchError(f"zone {zone.zone_id} was superseded by {zone.superseded_by}")
        if acceptance.acceptance_id in zone.claimed_acceptance_ids:
            raise IdentityMismatchError(f"acceptance {acceptance.acceptance_id} was already used; acceptances are never reused")
        zone.claimed_acceptance_ids.add(acceptance.acceptance_id)
        self.zone = zone
        self.params = params
        self.side = Side.LONG if acceptance.event is ZoneEventType.ACCEPTED_ABOVE else Side.SHORT
        self.acceptance_id: str = acceptance.acceptance_id  # type: ignore[assignment]
        self.attempt_id: int = acceptance.attempt_id  # type: ignore[assignment]
        self.zone_version_id = zone.zone_id
        self.acceptance_timestamp = acceptance.timestamp_utc
        self.retest_distance = zone.approach_distance  # the day's proximity tolerance
        self.cutoff_utc = ny_time(acceptance.timestamp_utc.tz_convert(NEW_YORK).date(), NEW_ENTRY_CUTOFF_NY)
        self.hold: RetestHold | None = None
        self.outcome: ConfirmationOutcome | None = None
        self._last_bar_end = acceptance.timestamp_utc
        self._events: list[ConfirmationEvent] = [self._event(acceptance.timestamp_utc, ConfirmationEventType.CONFIRMATION_SEQUENCE_STARTED, 0)]

    # -- helpers ----------------------------------------------------------------
    @property
    def events(self) -> tuple[ConfirmationEvent, ...]:
        return tuple(self._events)

    @property
    def is_pending(self) -> bool:
        return self.outcome is None

    def _event(self, when, kind, index=None, detail="") -> ConfirmationEvent:
        return ConfirmationEvent(when, kind, self.acceptance_id, self.attempt_id, self.zone_version_id, index, detail)

    def _finish(self, when, outcome: ConfirmationOutcome, kind: ConfirmationEventType, index=None, detail="") -> tuple[ConfirmationEvent, ...]:
        self.outcome = outcome
        event = self._event(when, kind, index, detail or outcome.value)
        self._events.append(event)
        return (event,)

    def invalidate_zone_change(self, when_utc: pd.Timestamp) -> tuple[ConfirmationEvent, ...]:
        if self.outcome is not None:
            return ()
        return self._finish(when_utc, ConfirmationOutcome.INVALIDATED_BY_ZONE_CHANGE, ConfirmationEventType.CONFIRMATION_INVALIDATED)

    def end_trading_window(self, when_utc: pd.Timestamp) -> tuple[ConfirmationEvent, ...]:
        if self.outcome is not None:
            return ()
        return self._finish(when_utc, ConfirmationOutcome.INVALIDATED_BY_CUTOFF, ConfirmationEventType.CONFIRMATION_INVALIDATED)

    # -- main -------------------------------------------------------------------
    def process(self, bar: DecisionBar, zone_events: Iterable[ZoneEvent] = ()) -> tuple[ConfirmationEvent, ...]:
        """Evaluate one decision bar after the acceptance bar (bar zero)."""
        if self.outcome is not None or bar.end_utc <= self.acceptance_timestamp:
            return ()
        invalid = ConfirmationEventType.CONFIRMATION_INVALIDATED
        if bar.start_utc != self._last_bar_end:
            return self._finish(bar.start_utc, ConfirmationOutcome.INVALIDATED_BY_INTERRUPTION, invalid, None, "MISSING_DECISION_BAR")
        self._last_bar_end = bar.end_utc
        index = (bar.end_utc - self.acceptance_timestamp) // self.params.decision_bar_interval
        if bar.overlaps_blackout:
            return self._finish(bar.end_utc, ConfirmationOutcome.INVALIDATED_BY_BLACKOUT, invalid, index)
        if not bar.complete:
            return self._finish(bar.end_utc, ConfirmationOutcome.INVALIDATED_BY_INTERRUPTION, invalid, index, "INCOMPLETE_DECISION_BAR")
        if index > self.params.max_bars_after_acceptance:
            return self._finish(bar.end_utc, ConfirmationOutcome.TIMED_OUT, ConfirmationEventType.CONFIRMATION_TIMED_OUT, index)
        if bar.end_utc >= self.cutoff_utc:
            return self._finish(bar.end_utc, ConfirmationOutcome.INVALIDATED_BY_CUTOFF, invalid, index)

        lo, hi = self.zone.lower, self.zone.upper
        high, low, close = _d(bar.high), _d(bar.low), _d(bar.close)
        p, long = self.params, self.side is Side.LONG
        through_opposite = low <= lo - p.opposite_failure_points if long else high >= hi + p.opposite_failure_points

        if self.hold is None:
            opposite_acceptance = ZoneEventType.ACCEPTED_BELOW if long else ZoneEventType.ACCEPTED_ABOVE
            reasons = []
            if (close <= hi) if long else (close >= lo):
                reasons.append("CLOSE_BACK_AT_OR_THROUGH_NEAR_BOUNDARY")
            if through_opposite:
                reasons.append("WICK_THROUGH_OPPOSITE_BOUNDARY")
            if any(e.event is opposite_acceptance for e in zone_events):
                reasons.append("OPPOSITE_ACCEPTANCE")
            if reasons:
                return self._finish(bar.end_utc, ConfirmationOutcome.FAILED, ConfirmationEventType.CONFIRMATION_FAILED_BEFORE_HOLD, index, ";".join(reasons))
            if long:
                is_hold = low <= hi + self.retest_distance and close >= hi + p.hold_close_points
            else:
                is_hold = high >= lo - self.retest_distance and close <= lo - p.hold_close_points
            if is_hold and index < p.max_bars_after_acceptance:
                self.hold = RetestHold(bar.end_utc, index, high, low, close)
                event = self._event(bar.end_utc, ConfirmationEventType.LONG_RETEST_HOLD if long else ConfirmationEventType.SHORT_RETEST_HOLD, index)
                self._events.append(event)
                return (event,)
            if index >= p.max_bars_after_acceptance:
                detail = "HOLD_ON_FINAL_BAR_CANNOT_QUALIFY" if is_hold else "NO_RETEST_HOLD"
                return self._finish(bar.end_utc, ConfirmationOutcome.TIMED_OUT, ConfirmationEventType.CONFIRMATION_TIMED_OUT, index, detail)
            return ()

        # Post-hold: only the immediately following eligible bar may confirm.
        failed_after = ConfirmationEventType.CONFIRMATION_FAILED_AFTER_HOLD
        if through_opposite:
            return self._finish(bar.end_utc, ConfirmationOutcome.FAILED, failed_after, index, "WICK_THROUGH_OPPOSITE_BOUNDARY")
        if long and close >= self.hold.high + p.continuation_points:
            return self._finish(bar.end_utc, ConfirmationOutcome.CONFIRMED, ConfirmationEventType.CONFIRMED_LONG_CONTINUATION, index)
        if not long and close <= self.hold.low - p.continuation_points:
            return self._finish(bar.end_utc, ConfirmationOutcome.CONFIRMED, ConfirmationEventType.CONFIRMED_SHORT_CONTINUATION, index)
        return self._finish(bar.end_utc, ConfirmationOutcome.FAILED, failed_after, index, "CONTINUATION_CLOSE_NOT_BEYOND_HOLD_EXTREME")


class SetupEngine:
    """Runs one zone's state tracker and the confirmation of each acceptance it produces."""

    def __init__(self, zone: ZoneTracker, params: ConfirmationParams):
        self.zone = zone
        self.params = params
        self.sequences: list[ConfirmationSequence] = []

    @property
    def active(self) -> ConfirmationSequence | None:
        return next((s for s in self.sequences if s.is_pending), None)

    def process(self, bar: DecisionBar) -> tuple[ConfirmationEvent, ...]:
        zone_events = self.zone.process(bar)
        out: list[ConfirmationEvent] = []
        if self.active is not None:
            out += self.active.process(bar, zone_events)
        for event in zone_events:
            if event.event in (ZoneEventType.ACCEPTED_ABOVE, ZoneEventType.ACCEPTED_BELOW):
                sequence = ConfirmationSequence(self.zone, event, self.params)
                self.sequences.append(sequence)
                out += sequence.events
        return tuple(out)

    def zone_changed(self, when_utc: pd.Timestamp) -> tuple[ConfirmationEvent, ...]:
        return self.active.invalidate_zone_change(when_utc) if self.active else ()

    def end_trading_window(self, when_utc: pd.Timestamp) -> tuple[ConfirmationEvent, ...]:
        self.zone.end_trading_window(when_utc)
        return self.active.end_trading_window(when_utc) if self.active else ()
