"""Event-derived direction for Baseline B0 (Rule Freeze Round 11).

COMPONENT OF A DRAFT SPECIFICATION. B0 has no directional bias and no swing
or trend filter: a LONG or SHORT direction candidate exists only as the
final event of a fully linked sequence

    arming -> attempt -> acceptance -> retest-hold -> continuation close
    (CONFIRMED_LONG_CONTINUATION / CONFIRMED_SHORT_CONTINUATION)

and only when it completes inside the new-entry window. Nothing else (price
location, candle colour, EMA, VWAP, overnight/gap/prior-close bias) can
create or choose a direction. The only inputs accepted here are
confirmation sequences.

This module creates *direction candidates* only. It places no order and
selects nothing among same-direction candidates (a later round).

Conflict rule: if long and short confirmations complete at the same decision
timestamp, the result is NO_TRADE_DIRECTIONAL_CONFLICT; every conflicting
confirmation becomes non-executable and new entries halt for the rest of the
trading date. The outcome depends only on the *set* of confirmations, never
on processing order.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Iterable

import pandas as pd

from mnq_research.confirmation import ConfirmationEventType, ConfirmationOutcome, ConfirmationSequence, Side
from mnq_research.structural_levels import NEW_ENTRY_CUTOFF_NY, NEW_ENTRY_START_NY, NEW_YORK, ny_time


class CandidateStatus(str, Enum):
    ENTRY_CANDIDATE = "ENTRY_CANDIDATE"  # executable, awaiting the (later) entry round
    NON_EXECUTABLE_BEFORE_WINDOW = "NON_EXECUTABLE_BEFORE_WINDOW"
    NON_EXECUTABLE_AFTER_CUTOFF = "NON_EXECUTABLE_AFTER_CUTOFF"
    INVALIDATED_BY_DIRECTIONAL_CONFLICT = "INVALIDATED_BY_DIRECTIONAL_CONFLICT"
    NON_EXECUTABLE_DAILY_HALT = "NON_EXECUTABLE_DAILY_HALT"
    USED_BY_ENTRY_CANDIDATE = "USED_BY_ENTRY_CANDIDATE"
    NON_EXECUTABLE_GEOMETRY = "NON_EXECUTABLE_GEOMETRY"  # failed room-to-target / eligibility (Round 12)
    # D-028: lower-ranked same-direction candidate; terminal, never reused.
    NOT_SELECTED_BY_GEOMETRY_RANKING = "NOT_SELECTED_BY_GEOMETRY_RANKING"
    NON_EXECUTABLE_GEOMETRY_TIE = "NON_EXECUTABLE_GEOMETRY_TIE"  # exact two-criterion tie


class ResolutionResult(str, Enum):
    NO_CONFIRMATIONS = "NO_CONFIRMATIONS"
    CANDIDATES = "CANDIDATES"
    NO_TRADE_DIRECTIONAL_CONFLICT = "NO_TRADE_DIRECTIONAL_CONFLICT"
    NO_TRADE_DAILY_HALT = "NO_TRADE_DAILY_HALT"
    NO_TRADE_OUTSIDE_ENTRY_WINDOW = "NO_TRADE_OUTSIDE_ENTRY_WINDOW"


class LinkageError(ValueError):
    """A confirmation's identifiers do not all refer to the same date/contract/zone/attempt/acceptance."""


@dataclass
class DirectionCandidate:
    side: Side
    trading_date: dt.date
    contract: str
    zone_version_id: str
    attempt_id: int
    acceptance_id: str
    confirmation_timestamp_utc: pd.Timestamp
    status: CandidateStatus
    requires_selection: bool = False  # several same-direction candidates at one time (Round 12 selects)
    confirmation_id: str = ""
    confirmation_close: Decimal | None = None
    origin_lower_boundary: Decimal | None = None
    origin_upper_boundary: Decimal | None = None


@dataclass
class Resolution:
    decision_time_utc: pd.Timestamp
    result: ResolutionResult
    candidates: tuple[DirectionCandidate, ...]


def _confirmed_event(sequence: ConfirmationSequence):
    kinds = (ConfirmationEventType.CONFIRMED_LONG_CONTINUATION, ConfirmationEventType.CONFIRMED_SHORT_CONTINUATION)
    return next((e for e in sequence.events if e.event in kinds), None)


@dataclass
class DailyDirectionBook:
    """Direction candidates for one trading date and one contract."""

    trading_date: dt.date
    contract: str
    halted: bool = False
    halt_reason: str | None = None
    history: list[Resolution] = field(default_factory=list)

    def _candidate(self, sequence: ConfirmationSequence) -> DirectionCandidate:
        event = _confirmed_event(sequence)
        if sequence.outcome is not ConfirmationOutcome.CONFIRMED or event is None:
            raise LinkageError("only a CONFIRMED continuation can create a direction candidate")
        zone = sequence.zone
        expected_side = Side.LONG if event.event is ConfirmationEventType.CONFIRMED_LONG_CONTINUATION else Side.SHORT
        checks = {
            "trading date": event.timestamp_utc.tz_convert(NEW_YORK).date() == self.trading_date
            and sequence.acceptance_timestamp.tz_convert(NEW_YORK).date() == self.trading_date
            and zone.cluster.constituents[0].trade_date == self.trading_date,
            "contract": zone.cluster.source_contract == self.contract,
            "zone version": event.zone_version_id == sequence.zone_version_id == zone.zone_id,
            "attempt": event.attempt_id == sequence.attempt_id
            and any(a.attempt_id == sequence.attempt_id and a.acceptance_id == sequence.acceptance_id for a in zone.attempts),
            "acceptance": event.acceptance_id == sequence.acceptance_id,
            "side": expected_side is sequence.side,
        }
        broken = [name for name, ok in checks.items() if not ok]
        if broken:
            raise LinkageError(f"confirmation {sequence.acceptance_id}: mismatched {', '.join(broken)}")
        return DirectionCandidate(
            sequence.side,
            self.trading_date,
            self.contract,
            sequence.zone_version_id,
            sequence.attempt_id,
            sequence.acceptance_id,
            event.timestamp_utc,
            CandidateStatus.ENTRY_CANDIDATE,
            confirmation_id=sequence.confirmation_id,
            confirmation_close=sequence.confirmation_close,
            origin_lower_boundary=zone.lower,
            origin_upper_boundary=zone.upper,
        )

    def resolve(self, decision_time_utc: pd.Timestamp, sequences: Iterable[ConfirmationSequence]) -> Resolution:
        """Direction candidates from confirmations completing exactly at ``decision_time_utc``."""
        completing = []
        for sequence in sequences:
            event = _confirmed_event(sequence)
            if sequence.outcome is ConfirmationOutcome.CONFIRMED and event and event.timestamp_utc == decision_time_utc:
                completing.append(self._candidate(sequence))
        # Deterministic order for reporting only; never used to select.
        completing.sort(key=lambda c: (c.zone_version_id, c.acceptance_id, c.side.value, c.attempt_id))
        local = decision_time_utc.tz_convert(NEW_YORK)
        start = ny_time(local.date(), NEW_ENTRY_START_NY)
        cutoff = ny_time(local.date(), NEW_ENTRY_CUTOFF_NY)

        if not completing:
            result = ResolutionResult.NO_CONFIRMATIONS
        elif {c.side for c in completing} == {Side.LONG, Side.SHORT}:
            # Evidence of disorderly structure: nothing trades and the day halts.
            for c in completing:
                c.status = CandidateStatus.INVALIDATED_BY_DIRECTIONAL_CONFLICT
            self.halted = True
            self.halt_reason = self.halt_reason or f"DIRECTIONAL_CONFLICT_AT_{decision_time_utc.isoformat()}"
            result = ResolutionResult.NO_TRADE_DIRECTIONAL_CONFLICT
        elif self.halted:
            for c in completing:
                c.status = CandidateStatus.NON_EXECUTABLE_DAILY_HALT
            result = ResolutionResult.NO_TRADE_DAILY_HALT
        elif decision_time_utc < start or decision_time_utc >= cutoff:
            status = CandidateStatus.NON_EXECUTABLE_BEFORE_WINDOW if decision_time_utc < start else CandidateStatus.NON_EXECUTABLE_AFTER_CUTOFF
            for c in completing:
                c.status = status
            result = ResolutionResult.NO_TRADE_OUTSIDE_ENTRY_WINDOW
        else:
            for c in completing:
                c.requires_selection = len(completing) > 1
            result = ResolutionResult.CANDIDATES
        resolution = Resolution(decision_time_utc, result, tuple(completing))
        self.history.append(resolution)
        return resolution

    @property
    def new_entries_permitted(self) -> bool:
        return not self.halted

    @staticmethod
    def mark_used(candidate: DirectionCandidate) -> None:
        if candidate.status is not CandidateStatus.ENTRY_CANDIDATE:
            raise ValueError(f"candidate {candidate.acceptance_id} is not executable ({candidate.status.value})")
        candidate.status = CandidateStatus.USED_BY_ENTRY_CANDIDATE
