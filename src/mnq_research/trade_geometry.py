"""Trade geometry, room to target and same-direction selection (Rule Freeze Round 12).

COMPONENT OF A DRAFT SPECIFICATION. For a direction candidate (Round 11) it
computes a deterministic *planning* geometry:

    planned entry   = confirmation close +/- 1 tick (adverse)
    planned stop    = one tick beyond the FAR side of the originating zone
    planned target  = one tick before the nearest boundary of the NEXT
                      distinct zone in the direction of travel
    planned gross R:R = reward points / risk points  (>= 1.50 to qualify)

and selects at most one same-direction candidate per decision time
(highest R:R, then smallest risk, then greatest reward; an exact tie means
no trade at that timestamp).

It submits NO order, computes NO position size, simulates NO fill and applies
NO costs. The planned entry is a planning reference only; actual performance
and R values must later use actual fills.

All prices are exact Decimals on the 0.25 grid and R:R values are exact
Fractions, so comparisons and ties never depend on binary floating point.
Every numeric trading parameter comes from the rule file.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum
from fractions import Fraction
from typing import Any, Iterable, Mapping

import pandas as pd

from mnq_research.confirmation import Side
from mnq_research.direction import CandidateStatus, DailyDirectionBook, DirectionCandidate
from mnq_research.level_states import ZoneTracker
from mnq_research.structural_levels import NEW_ENTRY_CUTOFF_NY, NEW_ENTRY_START_NY, NEW_YORK, TICK, ny_time

RANKING_CRITERIA = ("highest_planned_gross_rr", "smallest_planned_risk_points", "greatest_planned_reward_points")
EXACT_TIE_ACTION = "NO_TRADE"
COST_STATUS = "UNRESOLVED_UNTIL_COST_MODEL_FROZEN"


class SelectionResult(str, Enum):
    NO_CANDIDATES = "NO_CANDIDATES"
    NO_ROOM_QUALIFIED_CANDIDATE = "NO_ROOM_QUALIFIED_CANDIDATE"
    SELECTED = "SELECTED"
    NO_TRADE_SAME_DIRECTION_GEOMETRY_TIE = "NO_TRADE_SAME_DIRECTION_GEOMETRY_TIE"


@dataclass(frozen=True)
class GeometryParams:
    planned_entry_adverse_buffer_ticks: int
    structural_invalidation_buffer_ticks: int
    target_buffer_ticks: int
    minimum_planned_gross_rr: Fraction
    candidate_ranking: tuple[str, ...]
    exact_tie_action: str
    specification_version: str
    configuration_hash: str

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "GeometryParams":
        from mnq_research.validation import rule_spec_hash  # local import avoids a module cycle

        p = spec["trade_geometry"]["parameters"]
        params = cls(
            p["planned_entry_adverse_buffer_ticks"],
            p["structural_invalidation_buffer_ticks"],
            p["target_buffer_ticks"],
            Fraction(Decimal(str(p["minimum_planned_gross_rr"]))),
            tuple(p["candidate_ranking"]),
            p["exact_tie_action"],
            str(spec["specification"]["version"]),
            rule_spec_hash(dict(spec)),
        )
        if params.candidate_ranking != RANKING_CRITERIA:
            raise ValueError(f"B0 implements only the ranking {RANKING_CRITERIA}")
        if params.exact_tie_action != EXACT_TIE_ACTION:
            raise ValueError(f"B0 implements only exact_tie_action {EXACT_TIE_ACTION!r}")
        return params


def _to_tick(value: Decimal, up: bool) -> Decimal:
    return (value / TICK).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * TICK


def _valid_price(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0 and value % TICK == 0


@dataclass
class GeometryResult:
    candidate: DirectionCandidate
    decision_time_utc: pd.Timestamp
    rejection_reasons: tuple[str, ...]
    planned_entry_price: Decimal | None = None
    structural_invalidation_price: Decimal | None = None
    planned_stop_price: Decimal | None = None
    planned_target_price: Decimal | None = None
    planned_risk_points: Decimal | None = None
    planned_reward_points: Decimal | None = None
    planned_gross_rr: Fraction | None = None
    target_zone_version_id: str | None = None
    # Reserved until the commission and slippage model is frozen (Round 12 cost treatment).
    estimated_round_trip_cost_usd: Decimal | None = None
    estimated_cost_points: Decimal | None = None
    planned_net_reward_points: Decimal | None = None
    planned_net_rr: Fraction | None = None
    cost_status: str = COST_STATUS

    @property
    def qualified(self) -> bool:
        return not self.rejection_reasons

    @property
    def rank_key(self) -> tuple:
        """Smaller is better: highest R:R, then smallest risk, then greatest reward."""
        return (-self.planned_gross_rr, self.planned_risk_points, -self.planned_reward_points)


def _eligible_target_zones(candidate: DirectionCandidate, decision_time: pd.Timestamp, zones: Iterable[ZoneTracker]) -> list[ZoneTracker]:
    return [
        z
        for z in zones
        if z.zone_id != candidate.zone_version_id
        and z.superseded_by is None
        and not z.expired
        and z.cluster.source_contract == candidate.contract
        and z.cluster.constituents[0].trade_date == candidate.trading_date
        and z.initialized_at_utc <= decision_time
    ]


def evaluate_geometry(
    candidate: DirectionCandidate,
    decision_time_utc: pd.Timestamp,
    active_zones: Iterable[ZoneTracker],
    params: GeometryParams,
    blocking_conditions: Iterable[str] = (),
) -> GeometryResult:
    """Planned entry, stop, target and R:R for one candidate, with every rejection reason."""
    reasons: list[str] = []
    if candidate.status is not CandidateStatus.ENTRY_CANDIDATE:
        reasons.append(f"NOT_AN_ENTRY_CANDIDATE:{candidate.status.value}")
    local = decision_time_utc.tz_convert(NEW_YORK)
    if not ny_time(local.date(), NEW_ENTRY_START_NY) <= decision_time_utc < ny_time(local.date(), NEW_ENTRY_CUTOFF_NY):
        reasons.append("DECISION_TIME_OUTSIDE_ENTRY_WINDOW")
    reasons += [f"BLOCKED:{condition}" for condition in blocking_conditions]

    result = GeometryResult(candidate, decision_time_utc, ())
    close, lo, hi = candidate.confirmation_close, candidate.origin_lower_boundary, candidate.origin_upper_boundary
    if not all(_valid_price(v) for v in (close, lo, hi)):
        result.rejection_reasons = tuple(reasons + ["INVALID_CONFIRMATION_OR_ZONE_PRICE"])
        return result

    long = candidate.side is Side.LONG
    entry = _to_tick(close + params.planned_entry_adverse_buffer_ticks * TICK, up=True) if long else _to_tick(
        close - params.planned_entry_adverse_buffer_ticks * TICK, up=False
    )
    stop = lo - params.structural_invalidation_buffer_ticks * TICK if long else hi + params.structural_invalidation_buffer_ticks * TICK
    risk = entry - stop if long else stop - entry
    result.planned_entry_price, result.structural_invalidation_price, result.planned_stop_price = entry, stop, stop
    result.planned_risk_points = risk
    if not (_valid_price(entry) and _valid_price(stop)):
        reasons.append("INVALID_PLANNED_PRICE")
    if risk <= 0:
        reasons.append("NONPOSITIVE_RISK")

    zones = _eligible_target_zones(candidate, decision_time_utc, active_zones)
    if long:
        ahead = sorted((z for z in zones if z.lower > entry), key=lambda z: (z.lower, z.zone_id))
    else:
        ahead = sorted((z for z in zones if z.upper < entry), key=lambda z: (-z.upper, z.zone_id))
    if not ahead:
        reasons.append("ROOM_TO_TARGET_UNAVAILABLE")
        result.rejection_reasons = tuple(reasons)
        return result
    target_zone = ahead[0]  # the NEAREST zone; never skipped for a more distant one
    target = (
        target_zone.lower - params.target_buffer_ticks * TICK if long else target_zone.upper + params.target_buffer_ticks * TICK
    )
    reward = target - entry if long else entry - target
    result.target_zone_version_id, result.planned_target_price, result.planned_reward_points = target_zone.zone_id, target, reward
    if not _valid_price(target):
        reasons.append("INVALID_PLANNED_PRICE")
    if (target <= entry) if long else (target >= entry):
        reasons.append("TARGET_NOT_BEYOND_ENTRY")
    if reward <= 0:
        reasons.append("NONPOSITIVE_REWARD")
    if risk > 0 and reward > 0:
        result.planned_gross_rr = Fraction(reward) / Fraction(risk)
        if result.planned_gross_rr < params.minimum_planned_gross_rr:
            reasons.append("RR_BELOW_MINIMUM")
    result.rejection_reasons = tuple(dict.fromkeys(reasons))
    return result


@dataclass(frozen=True)
class SelectedEntryCandidate:
    """The single selected candidate. NOT an order: no quantity, no fill."""

    candidate_id: str
    direction: Side
    decision_timestamp: pd.Timestamp
    confirmation_timestamp: pd.Timestamp
    contract: str
    zone_version_id: str
    attempt_id: int
    acceptance_id: str
    confirmation_id: str
    target_zone_version_id: str
    planned_entry_price: Decimal
    structural_invalidation_price: Decimal
    planned_stop_price: Decimal
    planned_target_price: Decimal
    planned_risk_points: Decimal
    planned_reward_points: Decimal
    planned_gross_rr: Fraction
    selection_rank_values: tuple[tuple[str, str], ...]
    specification_version: str
    configuration_hash: str
    status: str = "SELECTED_ENTRY_CANDIDATE"


@dataclass
class Selection:
    decision_time_utc: pd.Timestamp
    result: SelectionResult
    evaluated: tuple[GeometryResult, ...]
    selected: SelectedEntryCandidate | None = None
    tied: tuple[GeometryResult, ...] = field(default_factory=tuple)


def select_candidate(decision_time_utc: pd.Timestamp, evaluated: Iterable[GeometryResult], params: GeometryParams) -> Selection:
    """Pick at most one same-direction candidate; an exact tie selects none (this timestamp only)."""
    evaluated = tuple(evaluated)
    if not evaluated:
        return Selection(decision_time_utc, SelectionResult.NO_CANDIDATES, evaluated)
    if len({r.candidate.side for r in evaluated}) > 1:
        raise ValueError("opposite directions must be resolved by the direction layer (conflict rule) first")
    for r in evaluated:
        if not r.qualified and r.candidate.status is CandidateStatus.ENTRY_CANDIDATE:
            r.candidate.status = CandidateStatus.NON_EXECUTABLE_GEOMETRY
    qualified = [r for r in evaluated if r.qualified]
    if not qualified:
        return Selection(decision_time_utc, SelectionResult.NO_ROOM_QUALIFIED_CANDIDATE, evaluated)
    best_key = min(r.rank_key for r in qualified)
    best = [r for r in qualified if r.rank_key == best_key]
    for r in qualified:
        if r not in best:
            r.candidate.status = CandidateStatus.NON_EXECUTABLE_NOT_SELECTED
    if len(best) > 1:
        for r in best:
            r.candidate.status = CandidateStatus.NON_EXECUTABLE_GEOMETRY_TIE
        return Selection(decision_time_utc, SelectionResult.NO_TRADE_SAME_DIRECTION_GEOMETRY_TIE, evaluated, None, tuple(best))
    (chosen,) = best
    DailyDirectionBook.mark_used(chosen.candidate)
    c = chosen.candidate
    selected = SelectedEntryCandidate(
        candidate_id=f"{c.confirmation_id}:CANDIDATE",
        direction=c.side,
        decision_timestamp=decision_time_utc,
        confirmation_timestamp=c.confirmation_timestamp_utc,
        contract=c.contract,
        zone_version_id=c.zone_version_id,
        attempt_id=c.attempt_id,
        acceptance_id=c.acceptance_id,
        confirmation_id=c.confirmation_id,
        target_zone_version_id=chosen.target_zone_version_id,
        planned_entry_price=chosen.planned_entry_price,
        structural_invalidation_price=chosen.structural_invalidation_price,
        planned_stop_price=chosen.planned_stop_price,
        planned_target_price=chosen.planned_target_price,
        planned_risk_points=chosen.planned_risk_points,
        planned_reward_points=chosen.planned_reward_points,
        planned_gross_rr=chosen.planned_gross_rr,
        selection_rank_values=(
            ("planned_gross_rr", str(chosen.planned_gross_rr)),
            ("planned_risk_points", str(chosen.planned_risk_points)),
            ("planned_reward_points", str(chosen.planned_reward_points)),
        ),
        specification_version=params.specification_version,
        configuration_hash=params.configuration_hash,
    )
    return Selection(decision_time_utc, SelectionResult.SELECTED, evaluated, selected)
