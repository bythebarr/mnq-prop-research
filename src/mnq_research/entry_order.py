"""Entry-order lifecycle for Baseline B0 (Rule Freeze Round 13).

COMPONENT OF A DRAFT SPECIFICATION. A deterministic *simulation* state machine
for the single market entry order that may follow a SELECTED_ENTRY_CANDIDATE
(Round 12):

    created at the decision time
      -> submitted exactly signal_to_order_delay later, only if every
         eligibility control is re-verified CLEAR at that moment
      -> works for at most entry_order_max_working_seconds, or less if the
         11:30 cutoff, a news boundary, a safety halt, loss of reliable state
         or a contract/session invalidation comes first
      -> at the deadline the remainder is cancelled: never extended, replaced,
         converted or chased
      -> exactly one outcome: ENTRY_FULLY_FILLED, ENTRY_PARTIALLY_FILLED,
         ENTRY_NOT_FILLED, ENTRY_ORDER_REJECTED, ENTRY_ORDER_STATE_UNKNOWN or
         NOT_SUBMITTED_INELIGIBLE.

Broker reports (acknowledgement, fills, rejection, cancel confirmation) are
*inputs* fed in by a later fill simulator or by tests. This module talks to
no broker, network or platform: ``live_or_paper_order_submission`` is
``prohibited`` until a protective-order layer exists. A fill only creates a
REQUIRED protection task; it places no stop or target order.

Not implemented here (later rounds): position sizing, commissions, protective
stop/target orders, trade management, prop-account simulation, backtesting.
The quantity is a RESEARCH_QUANTITY_ONLY value, NOT deployment sizing.

All prices are exact Decimals on the tick grid; averages, slippage and R are
exact Fractions. Every numeric trading parameter comes from the rule file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Any, Iterable, Mapping

import pandas as pd

from mnq_research.confirmation import Side
from mnq_research.eligibility import ControlState, ExecutionEligibility
from mnq_research.structural_levels import NEW_ENTRY_CUTOFF_NY, NEW_YORK, TICK, ny_time
from mnq_research.trade_geometry import Selection, SelectedEntryCandidate

ENTRY_ORDER_TYPE = "MARKET"
PROHIBITED_ENTRY_ORDER_TYPES = ("MARKET_IF_TOUCHED", "LIMIT", "STOP_LIMIT", "STOP_MARKET")
LIVE_OR_PAPER_SUBMISSION_STATUS = "prohibited"
RESEARCH_QUANTITY_LABELS = ("RESEARCH_QUANTITY_ONLY", "NOT_DEPLOYMENT_SIZING")
PROTECTION_TASK_STATUS = "REQUIRED_PROTECTION_LAYER_NOT_IMPLEMENTED"


class FillBeforeSubmissionError(ValueError):
    """A fill was reported for an order that had not been submitted (a simulator defect)."""


class CandidateReuseError(ValueError):
    """A confirmation, acceptance or attempt identity was offered for a second entry."""


class OrderPhase(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"  # awaiting acknowledgement
    ACKNOWLEDGED = "ACKNOWLEDGED"  # working
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    FINAL = "FINAL"


class EntryOutcome(str, Enum):
    NOT_SUBMITTED_INELIGIBLE = "NOT_SUBMITTED_INELIGIBLE"
    ENTRY_NOT_FILLED = "ENTRY_NOT_FILLED"
    ENTRY_PARTIALLY_FILLED = "ENTRY_PARTIALLY_FILLED"
    ENTRY_FULLY_FILLED = "ENTRY_FULLY_FILLED"
    ENTRY_ORDER_REJECTED = "ENTRY_ORDER_REJECTED"
    ENTRY_ORDER_STATE_UNKNOWN = "ENTRY_ORDER_STATE_UNKNOWN"


# Outcomes after which no new entry may be made for the rest of the trading date.
HALTING_OUTCOMES = frozenset(
    {
        EntryOutcome.ENTRY_NOT_FILLED,
        EntryOutcome.ENTRY_PARTIALLY_FILLED,
        EntryOutcome.ENTRY_ORDER_REJECTED,
        EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN,
    }
)


class DeadlineReason(str, Enum):
    WORKING_TIME_ELAPSED = "WORKING_TIME_ELAPSED"
    NEW_ENTRY_CUTOFF = "NEW_ENTRY_CUTOFF"
    NEWS_BOUNDARY = "NEWS_BOUNDARY"  # entry-protection or blackout start
    SAFETY_HALT = "SAFETY_HALT"
    LOSS_OF_RELIABLE_STATE = "LOSS_OF_RELIABLE_STATE"
    CONTRACT_OR_SESSION_INVALIDATED = "CONTRACT_OR_SESSION_INVALIDATED"


UNKNOWN_STATE_ACTIONS = (
    "DO_NOT_RESUBMIT_OR_REPLACE",
    "QUERY_ORDER_STATUS",
    "QUERY_POSITION",
    "NO_EXPOSURE_INCREASE",
    "PROTECT_CONFIRMED_POSITION",
    "CRITICAL_ALERT",
    "HALT_NEW_ENTRIES_FOR_TRADING_DATE",
)
RACE_FILL_ACTIONS = ("PROTECT_CONFIRMED_POSITION", "RECONCILE_ORDER_AND_POSITION", "HALT_NEW_ENTRIES_FOR_TRADING_DATE")


def _seconds(value: Any, name: str) -> pd.Timedelta:
    seconds = Decimal(str(value))
    delta = pd.Timedelta(seconds=float(seconds))
    if not seconds.is_finite() or seconds <= 0 or Decimal(str(delta.total_seconds())) != seconds:
        raise ValueError(f"{name} must be a positive number of seconds exact to the microsecond, got {value!r}")
    return delta


@dataclass(frozen=True)
class EntryOrderParams:
    entry_order_type: str
    signal_to_order_delay: pd.Timedelta
    max_working: pd.Timedelta
    acknowledgement_timeout: pd.Timedelta
    research_quantity_contracts: int
    structural_invalidation_buffer_ticks: int
    live_or_paper_order_submission: str
    specification_version: str
    configuration_hash: str

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "EntryOrderParams":
        from mnq_research.validation import rule_spec_hash  # local import avoids a module cycle

        life = spec["entry_order_lifecycle"]
        p = life["parameters"]
        params = cls(
            p["entry_order_type"],
            _seconds(p["signal_to_order_delay_seconds"], "signal_to_order_delay_seconds"),
            _seconds(p["entry_order_max_working_seconds"], "entry_order_max_working_seconds"),
            _seconds(p["acknowledgement_timeout_seconds"], "acknowledgement_timeout_seconds"),
            p["research_quantity_contracts"],
            spec["trade_geometry"]["parameters"]["structural_invalidation_buffer_ticks"],
            life["protective_order_dependency"]["live_or_paper_order_submission"],
            str(spec["specification"]["version"]),
            rule_spec_hash(dict(spec)),
        )
        if params.entry_order_type != ENTRY_ORDER_TYPE:
            raise ValueError(f"B0 implements only entry_order_type {ENTRY_ORDER_TYPE!r}")
        if params.live_or_paper_order_submission != LIVE_OR_PAPER_SUBMISSION_STATUS:
            raise ValueError("live or paper order submission is prohibited until the protective-order layer exists")
        if tuple(life["research_quantity_labels"]) != RESEARCH_QUANTITY_LABELS:
            raise ValueError(f"the research quantity must be labelled {RESEARCH_QUANTITY_LABELS}")
        q = params.research_quantity_contracts
        if isinstance(q, bool) or not isinstance(q, int) or q != 1:
            raise ValueError("B0 research quantity is exactly 1 contract (not deployment sizing)")
        return params


@dataclass(frozen=True)
class Fill:
    timestamp_utc: pd.Timestamp
    quantity: int
    price: Decimal


@dataclass(frozen=True)
class ProtectionTask:
    """A REQUIRED protective stop/target for confirmed exposure. Nothing is placed in Round 13."""

    order_id: str
    side: Side
    quantity: int
    frozen_stop_price: Decimal
    frozen_target_price: Decimal
    created_utc: pd.Timestamp
    stop_protective_of_actual_entry: bool
    status: str = PROTECTION_TASK_STATUS


def _valid_price(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0 and value % TICK == 0


def _iso(ts: pd.Timestamp | None) -> str | None:
    return None if ts is None else ts.isoformat()


@dataclass
class EntryOrder:
    """One simulated market entry order. Never resubmitted, replaced, converted or chased."""

    candidate: SelectedEntryCandidate
    params: EntryOrderParams
    order_created_utc: pd.Timestamp
    quantity: int
    order_id: str = ""
    order_type: str = ENTRY_ORDER_TYPE
    phase: OrderPhase = OrderPhase.CREATED
    outcome: EntryOutcome | None = None
    reasons: list[str] = field(default_factory=list)
    order_submitted_utc: pd.Timestamp | None = None
    broker_acknowledged_utc: pd.Timestamp | None = None
    first_fill_utc: pd.Timestamp | None = None
    final_fill_utc: pd.Timestamp | None = None
    cancellation_requested_utc: pd.Timestamp | None = None
    cancellation_confirmed_utc: pd.Timestamp | None = None
    final_order_state_utc: pd.Timestamp | None = None
    deadline_utc: pd.Timestamp | None = None
    deadline_reason: DeadlineReason | None = None
    fills: list[Fill] = field(default_factory=list)
    rejection_code: str | None = None
    rejection_message: str | None = None
    reconciliation_required: bool = False
    critical_alert: bool = False
    cancellation_race_fill: bool = False
    required_actions: list[str] = field(default_factory=list)
    protection_task: ProtectionTask | None = None
    reconciled_utc: pd.Timestamp | None = None
    reconciled_position_quantity: int | None = None
    events: list[tuple[str, str, str]] = field(default_factory=list)  # append-only audit trail

    # ------------------------------------------------------------------ helpers
    @property
    def side(self) -> Side:
        return self.candidate.direction

    @property
    def scheduled_submission_utc(self) -> pd.Timestamp:
        return self.order_created_utc + self.params.signal_to_order_delay

    @property
    def filled_quantity(self) -> int:
        return sum(f.quantity for f in self.fills)

    @property
    def halts_trading_date(self) -> bool:
        return self.outcome in HALTING_OUTCOMES or self.cancellation_race_fill or self.reconciliation_required

    def _log(self, at: pd.Timestamp, event: str, detail: str = "") -> None:
        self.events.append((at.isoformat(), event, detail))

    def _act(self, actions: Iterable[str]) -> None:
        for action in actions:
            if action not in self.required_actions:
                self.required_actions.append(action)

    def _finalize(self, at: pd.Timestamp, outcome: EntryOutcome, *reasons: str) -> None:
        if self.outcome is not EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN:  # an unknown state is never silently resolved
            self.outcome = outcome
        self.reasons.extend(r for r in reasons if r not in self.reasons)
        self.phase = OrderPhase.FINAL
        self.final_order_state_utc = at
        self._log(at, "FINAL_ORDER_STATE", self.outcome.value)

    def _mark_unknown(self, at: pd.Timestamp, reason: str) -> None:
        self.outcome = EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.reconciliation_required = True
        self.critical_alert = True
        self._act(UNKNOWN_STATE_ACTIONS)
        self._log(at, "ENTRY_ORDER_STATE_UNKNOWN", reason)

    def _request_cancel(self, at: pd.Timestamp, reason: DeadlineReason) -> None:
        if self.phase in (OrderPhase.SUBMITTED, OrderPhase.ACKNOWLEDGED):
            self.phase = OrderPhase.CANCEL_REQUESTED
            self.cancellation_requested_utc = at
            self._log(at, "CANCELLATION_REQUESTED", f"remainder {self.quantity - self.filled_quantity}; {reason.value}")

    def _require(self, at: pd.Timestamp, earliest: pd.Timestamp | None, what: str) -> None:
        if earliest is not None and at < earliest:
            raise ValueError(f"{what} at {at} precedes {earliest}")

    # ------------------------------------------------------------------ submission
    def submit(self, at: pd.Timestamp, eligibility: ExecutionEligibility, boundaries_utc: Iterable[pd.Timestamp]) -> None:
        """Simulated submission exactly ``signal_to_order_delay`` after creation, after a full eligibility recheck.

        ``boundaries_utc`` are the known upcoming news entry-protection/blackout
        starts; an order can never work past one.
        """
        if self.phase is not OrderPhase.CREATED:
            raise ValueError(f"order {self.order_id} cannot be submitted from {self.phase.value} (no resubmission)")
        if at != self.scheduled_submission_utc:
            raise ValueError(f"submission must be exactly at {self.scheduled_submission_utc}, got {at}")
        if not isinstance(eligibility, ExecutionEligibility):
            raise TypeError("eligibility must be an ExecutionEligibility snapshot (fail closed)")
        boundaries = tuple(boundaries_utc)
        if any(not isinstance(b, pd.Timestamp) or b.tzinfo is None for b in boundaries):
            raise TypeError("boundaries must be time-zone-aware timestamps")
        cutoff = ny_time(at.tz_convert(NEW_YORK).date(), NEW_ENTRY_CUTOFF_NY)
        reasons = list(eligibility.blocking_reasons())
        if at >= cutoff:
            reasons.append("SUBMISSION_AT_OR_AFTER_NEW_ENTRY_CUTOFF")
        if any(b <= at for b in boundaries):
            reasons.append("SUBMISSION_AT_OR_AFTER_NEWS_BOUNDARY")
        if reasons:
            self._finalize(at, EntryOutcome.NOT_SUBMITTED_INELIGIBLE, *reasons)
            return
        self.order_submitted_utc = at
        self.phase = OrderPhase.SUBMITTED
        candidates = [(at + self.params.max_working, DeadlineReason.WORKING_TIME_ELAPSED), (cutoff, DeadlineReason.NEW_ENTRY_CUTOFF)]
        candidates += [(b, DeadlineReason.NEWS_BOUNDARY) for b in boundaries]
        order = list(DeadlineReason)
        self.deadline_utc, self.deadline_reason = min(candidates, key=lambda c: (c[0], order.index(c[1])))
        self._log(at, "ORDER_SUBMITTED", f"MARKET {self.side.value} {self.quantity}; deadline {self.deadline_utc.isoformat()}")

    # ------------------------------------------------------------------ broker reports (simulated inputs)
    def on_acknowledged(self, at: pd.Timestamp) -> None:
        if self.order_submitted_utc is None:
            raise ValueError("acknowledgement for an order that was never submitted")
        self._require(at, self.order_submitted_utc, "acknowledgement")
        if self.broker_acknowledged_utc is None:
            self.broker_acknowledged_utc = at
        if self.phase is OrderPhase.SUBMITTED:
            self.phase = OrderPhase.ACKNOWLEDGED
        self._log(at, "BROKER_ACKNOWLEDGED")

    def on_fill(self, at: pd.Timestamp, quantity: int, price: Decimal) -> None:
        """Every reported fill is real exposure, whenever it arrives."""
        if self.order_submitted_utc is None or at < self.order_submitted_utc:
            raise FillBeforeSubmissionError(f"fill at {at} for order {self.order_id} submitted at {self.order_submitted_utc}")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or not _valid_price(price):
            raise ValueError(f"invalid fill {quantity!r} @ {price!r}")
        phase_before = self.phase
        self.fills.append(Fill(at, quantity, price))
        self.first_fill_utc = self.first_fill_utc or at
        self.final_fill_utc = at
        self._log(at, "FILL", f"{quantity} @ {price}")
        self._update_protection(at)
        if self.filled_quantity > self.quantity:
            self._mark_unknown(at, "FILLED_MORE_THAN_ORDERED")
        if phase_before is OrderPhase.FINAL:
            self._mark_unknown(at, "FILL_AFTER_FINAL_ORDER_STATE")
            return
        if phase_before is OrderPhase.CANCEL_REQUESTED:
            self.cancellation_race_fill = True
            self.reconciliation_required = True
            self._act(RACE_FILL_ACTIONS)
        if self.filled_quantity >= self.quantity:
            self._finalize(at, EntryOutcome.ENTRY_FULLY_FILLED)

    def on_rejected(self, at: pd.Timestamp, code: str, message: str) -> None:
        if self.order_submitted_utc is None:
            raise ValueError("rejection for an order that was never submitted")
        self._require(at, self.order_submitted_utc, "rejection")
        self.rejection_code, self.rejection_message = code, message
        self._log(at, "ORDER_REJECTED", f"{code}: {message}")
        if self.fills or self.phase is OrderPhase.FINAL:
            self._mark_unknown(at, "REJECTION_CONTRADICTS_RECORDED_STATE")
            return
        self._finalize(at, EntryOutcome.ENTRY_ORDER_REJECTED, f"REJECTED:{code}")

    def on_cancel_confirmed(self, at: pd.Timestamp) -> None:
        self._require(at, self.cancellation_requested_utc, "cancellation confirmation")
        if self.cancellation_requested_utc is None:
            raise ValueError("cancellation confirmed but never requested")
        self.cancellation_confirmed_utc = self.cancellation_confirmed_utc or at
        self._log(at, "CANCELLATION_CONFIRMED")
        if self.phase is not OrderPhase.CANCEL_REQUESTED:
            return  # e.g. the order had already filled completely: nothing more to cancel
        if self.filled_quantity == 0:
            self._finalize(at, EntryOutcome.ENTRY_NOT_FILLED, "NO_FILL_BEFORE_DEADLINE")
        else:
            self._finalize(at, EntryOutcome.ENTRY_PARTIALLY_FILLED, "REMAINDER_CANCELLED_AT_DEADLINE")

    # ------------------------------------------------------------------ time and invalidation
    def advance(self, at: pd.Timestamp) -> None:
        """Apply the acknowledgement timeout and the working deadline up to ``at``."""
        if self.order_submitted_utc is None or self.phase is OrderPhase.FINAL:
            return
        ack_deadline = self.order_submitted_utc + self.params.acknowledgement_timeout
        if (
            at >= ack_deadline
            and self.broker_acknowledged_utc is None
            and not self.fills
            and self.outcome is not EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN
        ):
            self._mark_unknown(ack_deadline, "NO_ACKNOWLEDGEMENT_WITHIN_TIMEOUT")
        if at >= self.deadline_utc:
            self._request_cancel(self.deadline_utc, self.deadline_reason)

    def invalidate(self, at: pd.Timestamp, reason: DeadlineReason) -> None:
        """An unscheduled event ends the working time now (safety halt, lost state, contract/session)."""
        if self.order_submitted_utc is None or self.phase is OrderPhase.FINAL:
            return
        self._require(at, self.order_submitted_utc, "invalidation")
        if at < self.deadline_utc:
            self.deadline_utc, self.deadline_reason = at, reason
        if reason is DeadlineReason.LOSS_OF_RELIABLE_STATE:
            self._mark_unknown(at, "LOSS_OF_RELIABLE_STATE")
        self._request_cancel(at, reason)

    def reconcile(self, at: pd.Timestamp, confirmed_position_quantity: int) -> None:
        """Record the queried, confirmed position. The recorded outcome is kept; the day stays halted."""
        if isinstance(confirmed_position_quantity, bool) or not isinstance(confirmed_position_quantity, int) or confirmed_position_quantity < 0:
            raise ValueError("confirmed position quantity must be a whole number >= 0")
        self.reconciled_utc, self.reconciled_position_quantity = at, confirmed_position_quantity
        detail = f"confirmed {confirmed_position_quantity}; recorded fills {self.filled_quantity}"
        if confirmed_position_quantity != self.filled_quantity:
            self._mark_unknown(at, "RECONCILED_POSITION_DIFFERS_FROM_RECORDED_FILLS")
        self._log(at, "RECONCILED", detail)
        if confirmed_position_quantity:
            self._update_protection(at, confirmed_position_quantity)

    # ------------------------------------------------------------------ fills, slippage and actual risk
    def _update_protection(self, at: pd.Timestamp, quantity: int | None = None) -> None:
        created = self.protection_task.created_utc if self.protection_task else at
        risk = self.actual_risk_points_per_contract  # None when only a reconciled position is known: fail closed
        self.protection_task = ProtectionTask(
            self.order_id,
            self.side,
            self.filled_quantity if quantity is None else quantity,
            self.candidate.planned_stop_price,  # frozen: never recalculated after a fill
            self.candidate.planned_target_price,
            created,
            risk is not None and risk > 0,
        )

    def _signed(self, value: Fraction) -> Fraction:
        return value if self.side is Side.LONG else -value

    @property
    def average_fill_price(self) -> Fraction | None:
        if not self.fills:
            return None
        return sum((Fraction(f.price) * f.quantity for f in self.fills), Fraction(0)) / self.filled_quantity

    @property
    def slippage_vs_confirmation_close_points(self) -> Fraction | None:
        """Adverse-positive: how much worse than the confirmation close the average fill was."""
        avg = self.average_fill_price
        return None if avg is None else self._signed(avg - Fraction(self.candidate.confirmation_close))

    @property
    def slippage_vs_planned_entry_points(self) -> Fraction | None:
        avg = self.average_fill_price
        return None if avg is None else self._signed(avg - Fraction(self.candidate.planned_entry_price))

    @property
    def submission_to_first_fill(self) -> pd.Timedelta | None:
        return None if self.first_fill_utc is None else self.first_fill_utc - self.order_submitted_utc

    @property
    def submission_to_final_fill(self) -> pd.Timedelta | None:
        return None if self.final_fill_utc is None else self.final_fill_utc - self.order_submitted_utc

    @property
    def actual_risk_points_per_contract(self) -> Fraction | None:
        """From the ACTUAL average fill to the FROZEN structural stop."""
        avg = self.average_fill_price
        return None if avg is None else self._signed(avg - Fraction(self.candidate.planned_stop_price))

    @property
    def actual_risk_points_total(self) -> Fraction | None:
        risk = self.actual_risk_points_per_contract
        return None if risk is None else risk * self.filled_quantity

    def actual_r_multiple(self, exit_price: Decimal) -> Fraction:
        """R on the actual fill (never the planned entry); later rounds supply the exit."""
        risk = self.actual_risk_points_per_contract
        if risk is None or risk <= 0:
            raise ValueError("no valid actual risk: the stop is not on the protective side of the actual fill")
        return self._signed(Fraction(exit_price) - self.average_fill_price) / risk

    # ------------------------------------------------------------------ audit
    def audit_record(self) -> dict[str, Any]:
        c = self.candidate
        avg = self.average_fill_price
        return {
            "order_id": self.order_id,
            "candidate_id": c.candidate_id,
            "confirmation_id": c.confirmation_id,
            "acceptance_id": c.acceptance_id,
            "attempt_id": c.attempt_id,
            "zone_version_id": c.zone_version_id,
            "contract": c.contract,
            "direction": self.side.value,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "quantity_labels": list(RESEARCH_QUANTITY_LABELS),
            "phase": self.phase.value,
            "outcome": None if self.outcome is None else self.outcome.value,
            "reasons": list(self.reasons),
            "order_created_utc": _iso(self.order_created_utc),
            "order_submitted_utc": _iso(self.order_submitted_utc),
            "broker_acknowledged_utc": _iso(self.broker_acknowledged_utc),
            "first_fill_utc": _iso(self.first_fill_utc),
            "final_fill_utc": _iso(self.final_fill_utc),
            "cancellation_requested_utc": _iso(self.cancellation_requested_utc),
            "cancellation_confirmed_utc": _iso(self.cancellation_confirmed_utc),
            "final_order_state_utc": _iso(self.final_order_state_utc),
            "deadline_utc": _iso(self.deadline_utc),
            "deadline_reason": None if self.deadline_reason is None else self.deadline_reason.value,
            "fills": [[_iso(f.timestamp_utc), f.quantity, str(f.price)] for f in self.fills],
            "filled_quantity": self.filled_quantity,
            "average_fill_price": None if avg is None else str(avg),
            "slippage_vs_confirmation_close_points": _str(self.slippage_vs_confirmation_close_points),
            "slippage_vs_planned_entry_points": _str(self.slippage_vs_planned_entry_points),
            "submission_to_first_fill": _str(self.submission_to_first_fill),
            "submission_to_final_fill": _str(self.submission_to_final_fill),
            "actual_risk_points_per_contract": _str(self.actual_risk_points_per_contract),
            "frozen_stop_price": str(c.planned_stop_price),
            "frozen_target_price": str(c.planned_target_price),
            "rejection_code": self.rejection_code,
            "rejection_message": self.rejection_message,
            "halts_trading_date": self.halts_trading_date,
            "reconciliation_required": self.reconciliation_required,
            "reconciled_position_quantity": self.reconciled_position_quantity,
            "critical_alert": self.critical_alert,
            "cancellation_race_fill": self.cancellation_race_fill,
            "required_actions": list(self.required_actions),
            "protection_task": None if self.protection_task is None else {
                "quantity": self.protection_task.quantity,
                "status": self.protection_task.status,
                "stop_protective_of_actual_entry": self.protection_task.stop_protective_of_actual_entry,
            },
            "events": [list(e) for e in self.events],
            "specification_version": self.params.specification_version,
            "configuration_hash": self.params.configuration_hash,
        }


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def stop_validity_problems(candidate: SelectedEntryCandidate, params: EntryOrderParams) -> tuple[str, ...]:
    """Structural stop checks. There is NO minimum/maximum stop filter, and the stop is never resized."""
    stop, entry, long = candidate.planned_stop_price, candidate.planned_entry_price, candidate.direction is Side.LONG
    if not _valid_price(stop):
        return ("INVALID_STOP:NOT_A_POSITIVE_FINITE_TICK_PRICE",)
    problems = []
    if stop != candidate.structural_invalidation_price:
        problems.append("INVALID_STOP:NOT_THE_STRUCTURAL_INVALIDATION_PRICE")
    buffer = params.structural_invalidation_buffer_ticks * TICK
    expected = candidate.origin_lower_boundary - buffer if long else candidate.origin_upper_boundary + buffer
    if stop != expected:
        problems.append("INVALID_STOP:NOT_FROM_THE_ORIGINATING_ZONE")
    if not _valid_price(entry) or ((stop >= entry) if long else (stop <= entry)):
        problems.append("INVALID_STOP:NOT_PROTECTIVE_OF_PLANNED_ENTRY")
    return tuple(problems)


@dataclass
class EntryOrderBook:
    """Entry orders for one trading date and one contract. Enforces one use per identity."""

    trading_date: Any
    contract: str
    params: EntryOrderParams
    orders: list[EntryOrder] = field(default_factory=list)
    _consumed: set[tuple[str, str]] = field(default_factory=set)
    _awaiting_order: dict[str, SelectedEntryCandidate] = field(default_factory=dict)

    @staticmethod
    def _identities(zone_version_id: str, attempt_id: int, acceptance_id: str, confirmation_id: str) -> set[tuple[str, str]]:
        return {
            ("confirmation", confirmation_id),
            ("acceptance", acceptance_id),
            ("attempt", f"{zone_version_id}#{attempt_id}"),
        }

    def record_selection(self, selection: Selection) -> None:
        """Consume the identity of EVERY evaluated candidate: selected, not selected, tied or rejected."""
        selected = selection.selected
        if selected is not None:
            ids = self._identities(selected.zone_version_id, selected.attempt_id, selected.acceptance_id, selected.confirmation_id)
            if ids & self._consumed:
                raise CandidateReuseError(f"{selected.candidate_id} reuses an already consumed identity")
        for r in selection.evaluated:
            c = r.candidate
            self._consumed |= self._identities(c.zone_version_id, c.attempt_id, c.acceptance_id, c.confirmation_id)
        if selected is not None:
            self._awaiting_order[selected.candidate_id] = selected

    def create_order(self, candidate: SelectedEntryCandidate, created_at: pd.Timestamp) -> EntryOrder:
        """Create the one entry order for a recorded selection. Validates the frozen structural stop."""
        if self._awaiting_order.pop(candidate.candidate_id, None) is not candidate:
            raise CandidateReuseError(f"{candidate.candidate_id} is not a recorded, unused selection")
        if created_at != candidate.decision_timestamp:
            raise ValueError("the entry order is created at the decision time")
        order = EntryOrder(candidate, self.params, created_at, self.params.research_quantity_contracts, order_id=f"{candidate.candidate_id}:ENTRY")
        order._log(created_at, "ORDER_CREATED", f"MARKET {order.side.value} {order.quantity} (research quantity)")
        problems = stop_validity_problems(candidate, self.params)
        if problems:
            order._finalize(created_at, EntryOutcome.NOT_SUBMITTED_INELIGIBLE, *problems)
        self.orders.append(order)
        return order

    @property
    def halted(self) -> bool:
        return any(o.halts_trading_date for o in self.orders)

    def controls(self) -> dict[str, ControlState]:
        """This book's contribution to ExecutionEligibility (unknown order state -> UNKNOWN)."""
        unknown = any(o.outcome is EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN for o in self.orders)
        exposure = any(o.fills for o in self.orders)
        working = any(o.phase in (OrderPhase.SUBMITTED, OrderPhase.ACKNOWLEDGED, OrderPhase.CANCEL_REQUESTED) for o in self.orders)
        blocked = ControlState.BLOCKED
        return {
            "daily_entry_halt": blocked if self.halted else ControlState.CLEAR,
            "no_open_position": ControlState.UNKNOWN if unknown else blocked if exposure else ControlState.CLEAR,
            "no_working_entry_order": ControlState.UNKNOWN if unknown else blocked if working else ControlState.CLEAR,
        }

