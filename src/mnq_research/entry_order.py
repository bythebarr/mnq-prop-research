"""Entry-order lifecycle for Baseline B0 (Rule Freeze Round 13, amended by D-029).

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
      -> exactly one lifecycle outcome: ENTRY_FULLY_FILLED,
         ENTRY_PARTIALLY_FILLED, ENTRY_NOT_FILLED, ENTRY_ORDER_REJECTED,
         ENTRY_ORDER_STATE_UNKNOWN or NOT_SUBMITTED_INELIGIBLE.

Each selected candidate produces at most one lifecycle. Broker reports
(acknowledgement, fills, rejection, cancel confirmation, state loss) are
*inputs* fed in by a later fill simulator or by tests. Only AUTHORITATIVE fill
records create exposure; a local fill estimate proves nothing.

D-029: an UNKNOWN classification and every operational anomaly are immutable
history. Later reconciliation adds a separate *current exposure* record
(RECONCILED_FLAT / _OPEN_POSITION / _PARTIAL_POSITION / RECONCILIATION_UNRESOLVED)
and never restores new-entry eligibility. Any positive confirmed fill consumes
the one-filled-entry daily allowance (FILLED_ENTRY_LIMIT_REACHED). A fill at or
beyond the structural stop requires emergency flattening; the stop is never
moved.

This module talks to no broker, network or platform:
``live_or_paper_order_submission`` is ``prohibited`` until a protective-order
layer exists. A fill only creates a REQUIRED protection (or emergency-flatten)
task; it places no order.

Not implemented here (later rounds): deployment position sizing, commissions,
protective stop/target orders, trade management, prop-account simulation,
backtesting. The quantity is a RESEARCH_QUANTITY_ONLY value.

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
EMERGENCY_FLATTEN_STATUS = "EMERGENCY_FLATTEN_REQUIRED"


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
# NOT_SUBMITTED reasons that halt the date (D-029 decision 4): unknown state, and a
# stop that fails its integrity checks. Known temporary blocks do not halt; permanent
# halts (safety, directional conflict) are already halted by their own rule.
HALTING_NOT_SUBMITTED_PREFIXES = ("UNKNOWN:", "INVALID_STOP:")


class FillSource(Enum):
    AUTHORITATIVE_FILL_RECORD = "AUTHORITATIVE_FILL_RECORD"  # broker record, or the research simulator's fill record
    LOCAL_ESTIMATE = "LOCAL_ESTIMATE"  # never proof of market receipt or exposure


class DeadlineReason(str, Enum):
    WORKING_TIME_ELAPSED = "WORKING_TIME_ELAPSED"
    NEW_ENTRY_CUTOFF = "NEW_ENTRY_CUTOFF"
    NEWS_BOUNDARY = "NEWS_BOUNDARY"  # entry-protection or blackout start
    SAFETY_HALT = "SAFETY_HALT"
    LOSS_OF_RELIABLE_STATE = "LOSS_OF_RELIABLE_STATE"  # only via on_state_lost
    CONTRACT_OR_SESSION_INVALIDATED = "CONTRACT_OR_SESSION_INVALIDATED"


class StateLossCause(str, Enum):
    CONNECTION_LOSS = "CONNECTION_LOSS"
    ORDER_QUERY_FAILURE = "ORDER_QUERY_FAILURE"
    POSITION_QUERY_FAILURE = "POSITION_QUERY_FAILURE"
    STALE_STATE = "STALE_STATE"
    PLATFORM_BROKER_DISAGREEMENT = "PLATFORM_BROKER_DISAGREEMENT"
    MISSING_RECONCILIATION_IDENTIFIERS = "MISSING_RECONCILIATION_IDENTIFIERS"


class Contradiction(str, Enum):
    FILLED_MORE_THAN_SUBMITTED = "FILLED_MORE_THAN_SUBMITTED"
    FILL_AFTER_CONFIRMED_CANCELLATION = "FILL_AFTER_CONFIRMED_CANCELLATION"
    REJECTION_AND_FILL_FOR_SAME_ORDER = "REJECTION_AND_FILL_FOR_SAME_ORDER"
    POSITION_DIFFERS_FROM_AUTHORITATIVE_FILLS = "POSITION_DIFFERS_FROM_AUTHORITATIVE_FILLS"
    SIDE_OR_CONTRACT_MISMATCH = "SIDE_OR_CONTRACT_MISMATCH"
    INCOMPATIBLE_TERMINAL_STATES = "INCOMPATIBLE_TERMINAL_STATES"


class ExposureState(str, Enum):
    RECONCILED_FLAT = "RECONCILED_FLAT"
    RECONCILED_OPEN_POSITION = "RECONCILED_OPEN_POSITION"
    RECONCILED_PARTIAL_POSITION = "RECONCILED_PARTIAL_POSITION"
    RECONCILIATION_UNRESOLVED = "RECONCILIATION_UNRESOLVED"


class DailyEntryState(str, Enum):
    OPEN_FOR_ENTRIES = "OPEN_FOR_ENTRIES"
    HALTED = "HALTED"
    FILLED_ENTRY_LIMIT_REACHED = "FILLED_ENTRY_LIMIT_REACHED"  # terminal for the date


UNKNOWN_STATE_ACTIONS = (
    "DO_NOT_RESUBMIT_OR_REPLACE",
    "QUERY_ORDER_STATUS",
    "QUERY_POSITION",
    "NO_EXPOSURE_INCREASE",
    "PRESERVE_CONFIRMED_PROTECTIVE_ORDERS",
    "PROTECT_CONFIRMED_POSITION",
    "CRITICAL_ALERT",
    "HALT_NEW_ENTRIES_FOR_TRADING_DATE",
)
RACE_FILL_ACTIONS = ("PROTECT_CONFIRMED_POSITION", "RECONCILE_ORDER_AND_POSITION", "HALT_NEW_ENTRIES_FOR_TRADING_DATE")
ACK_MISSING_ACTIONS = ("CONTINUE_RECONCILIATION", "PROTECT_CONFIRMED_POSITION", "DO_NOT_RESUBMIT_OR_REPLACE")
EMERGENCY_FLATTEN_ACTIONS = (
    "EMERGENCY_FLATTEN_CONFIRMED_QUANTITY",
    "USE_EMERGENCY_MARKET_CLOSE_RETRY_AND_RECONCILIATION_RULES",
    "HALT_NEW_ENTRIES_FOR_TRADING_DATE",
)
RECONCILED_POSITION_ACTIONS = ("PROTECT_OR_FLATTEN_RECONCILED_POSITION",)


def _seconds(value: Any, name: str) -> pd.Timedelta:
    seconds = Decimal(str(value))
    delta = pd.Timedelta(seconds=float(seconds))
    if not seconds.is_finite() or seconds <= 0 or Decimal(str(delta.total_seconds())) != seconds:
        raise ValueError(f"{name} must be a positive number of seconds exact to the microsecond, got {value!r}")
    return delta


def _whole(value: Any, name: str, required: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (required is not None and value != required):
        raise ValueError(f"{name} must be {'exactly ' + str(required) if required is not None else 'a whole number >= 1'}, got {value!r}")
    return value


@dataclass(frozen=True)
class EntryOrderParams:
    entry_order_type: str
    signal_to_order_delay: pd.Timedelta
    max_working: pd.Timedelta
    acknowledgement_timeout: pd.Timedelta
    research_quantity_contracts: int
    max_filled_entries_per_trading_date: int
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
            _seconds(p["order_acknowledgement_timeout_seconds"], "order_acknowledgement_timeout_seconds"),
            _whole(p["research_quantity_contracts"], "research_quantity_contracts", required=1),
            _whole(spec["daily_limits"]["max_filled_entries_per_trading_date"], "max_filled_entries_per_trading_date", required=1),
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
        return params


@dataclass(frozen=True)
class Fill:
    timestamp_utc: pd.Timestamp
    quantity: int
    price: Decimal
    source: FillSource


@dataclass(frozen=True)
class OperationalAnomaly:
    """Immutable history: never rewritten or deleted by later reconciliation."""

    timestamp_utc: pd.Timestamp
    code: str
    detail: str


@dataclass(frozen=True)
class ReconciliationRecord:
    """A NEW record of current exposure; earlier records and anomalies are untouched."""

    timestamp_utc: pd.Timestamp
    exposure_state: ExposureState
    confirmed_position_quantity: int | None
    detail: str


@dataclass(frozen=True)
class ProtectionTask:
    """A REQUIRED protective stop/target (or emergency flatten) for confirmed exposure. Nothing is placed."""

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


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


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
    fills: list[Fill] = field(default_factory=list)  # AUTHORITATIVE only
    local_fill_estimates: list[Fill] = field(default_factory=list)  # diagnostic only
    rejection_code: str | None = None
    rejection_message: str | None = None
    reconciliation_required: bool = False
    critical_alert: bool = False
    cancellation_race_fill: bool = False
    emergency_flatten_required: bool = False
    required_actions: list[str] = field(default_factory=list)
    protection_task: ProtectionTask | None = None
    _anomalies: list[OperationalAnomaly] = field(default_factory=list)
    _reconciliations: list[ReconciliationRecord] = field(default_factory=list)
    _acknowledgement_checked: bool = False
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
        """Authoritative filled quantity (local estimates never count)."""
        return sum(f.quantity for f in self.fills)

    @property
    def anomalies(self) -> tuple[OperationalAnomaly, ...]:
        return tuple(self._anomalies)

    @property
    def reconciliations(self) -> tuple[ReconciliationRecord, ...]:
        return tuple(self._reconciliations)

    @property
    def current_exposure(self) -> ExposureState | None:
        """Latest reconciliation result; None until a reconciliation has been recorded."""
        return self._reconciliations[-1].exposure_state if self._reconciliations else None

    @property
    def confirmed_position_quantity(self) -> int:
        """Latest reconciled quantity if known, otherwise the authoritative fills."""
        for record in reversed(self._reconciliations):
            if record.confirmed_position_quantity is not None:
                return record.confirmed_position_quantity
        return self.filled_quantity

    @property
    def consumed_filled_entry(self) -> bool:
        """Any positive confirmed fill (or reconciled strategy position) uses the daily allowance."""
        return bool(self.fills) or any((r.confirmed_position_quantity or 0) > 0 for r in self._reconciliations)

    @property
    def halts_trading_date(self) -> bool:
        if self.outcome in HALTING_OUTCOMES or self.cancellation_race_fill or self.reconciliation_required:
            return True
        if self.emergency_flatten_required:
            return True
        return self.outcome is EntryOutcome.NOT_SUBMITTED_INELIGIBLE and any(
            r.startswith(HALTING_NOT_SUBMITTED_PREFIXES) for r in self.reasons
        )

    def _log(self, at: pd.Timestamp, event: str, detail: str = "") -> None:
        self.events.append((at.isoformat(), event, detail))

    def _act(self, actions: Iterable[str]) -> None:
        for action in actions:
            if action not in self.required_actions:
                self.required_actions.append(action)

    def _reason(self, *reasons: str) -> None:
        self.reasons.extend(r for r in reasons if r not in self.reasons)

    def _anomaly(self, at: pd.Timestamp, code: str, detail: str = "") -> None:
        self._anomalies.append(OperationalAnomaly(at, code, detail))
        self._log(at, "OPERATIONAL_ANOMALY", f"{code} {detail}".strip())

    def _finalize(self, at: pd.Timestamp, outcome: EntryOutcome, *reasons: str) -> None:
        if self.outcome is not EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN:  # an unknown state is never rewritten
            self.outcome = outcome
        self._reason(*reasons)
        self.phase = OrderPhase.FINAL
        self.final_order_state_utc = at
        self._log(at, "FINAL_ORDER_STATE", self.outcome.value)

    def _mark_unknown(self, at: pd.Timestamp, reason: str) -> None:
        previous = None if self.outcome is None else self.outcome.value
        self.outcome = EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN
        self._reason(reason)
        self.reconciliation_required = True
        self.critical_alert = True
        self._act(UNKNOWN_STATE_ACTIONS)
        self._anomaly(at, EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN.value, f"{reason} (previous outcome {previous})")

    def _contradiction(self, at: pd.Timestamp, kind: Contradiction, detail: str = "") -> None:
        self._mark_unknown(at, f"CONTRADICTION:{kind.value}")
        if detail:
            self._log(at, "CONTRADICTION_DETAIL", detail)

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

    def on_fill(self, at: pd.Timestamp, quantity: int, price: Decimal, source: FillSource) -> None:
        """Every AUTHORITATIVE fill is real exposure, whenever it arrives. Local estimates prove nothing."""
        if self.order_submitted_utc is None or at < self.order_submitted_utc:
            raise FillBeforeSubmissionError(f"fill at {at} for order {self.order_id} submitted at {self.order_submitted_utc}")
        if type(source) is not FillSource:
            raise TypeError("fill source must be a FillSource (no default: an unlabelled fill is not authoritative)")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or not _valid_price(price):
            raise ValueError(f"invalid fill {quantity!r} @ {price!r}")
        fill = Fill(at, quantity, price, source)
        if source is FillSource.LOCAL_ESTIMATE:
            self.local_fill_estimates.append(fill)
            self._log(at, "LOCAL_FILL_ESTIMATE_IGNORED", f"{quantity} @ {price}")
            return
        phase_before, outcome_before = self.phase, self.outcome
        self.fills.append(fill)
        self.first_fill_utc = self.first_fill_utc or at
        self.final_fill_utc = at
        self._log(at, "FILL", f"{quantity} @ {price}")
        if self.filled_quantity > self.quantity:
            self._contradiction(at, Contradiction.FILLED_MORE_THAN_SUBMITTED)
        if self.cancellation_confirmed_utc is not None:
            self._contradiction(at, Contradiction.FILL_AFTER_CONFIRMED_CANCELLATION)
        elif outcome_before is EntryOutcome.ENTRY_ORDER_REJECTED:
            self._contradiction(at, Contradiction.REJECTION_AND_FILL_FOR_SAME_ORDER)
        elif phase_before is OrderPhase.CANCEL_REQUESTED:
            # Cancellation requested but not confirmed: a valid race fill, real exposure.
            self.cancellation_race_fill = True
            self.reconciliation_required = True
            self._act(RACE_FILL_ACTIONS)
        if phase_before is not OrderPhase.FINAL and self.filled_quantity >= self.quantity:
            self._finalize(at, EntryOutcome.ENTRY_FULLY_FILLED)
        self._update_protection(at)

    def on_rejected(self, at: pd.Timestamp, code: str, message: str) -> None:
        if self.order_submitted_utc is None:
            raise ValueError("rejection for an order that was never submitted")
        self._require(at, self.order_submitted_utc, "rejection")
        self.rejection_code, self.rejection_message = code, message
        self._log(at, "ORDER_REJECTED", f"{code}: {message}")
        if self.fills:
            self._contradiction(at, Contradiction.REJECTION_AND_FILL_FOR_SAME_ORDER)
        elif self.phase is OrderPhase.FINAL:
            self._contradiction(at, Contradiction.INCOMPATIBLE_TERMINAL_STATES, "rejected after another terminal state")
        else:
            self._finalize(at, EntryOutcome.ENTRY_ORDER_REJECTED, f"REJECTED:{code}")

    def on_cancel_confirmed(self, at: pd.Timestamp) -> None:
        """Authoritative confirmation that cancellation is complete."""
        if self.cancellation_requested_utc is None:
            raise ValueError("cancellation confirmed but never requested")
        self._require(at, self.cancellation_requested_utc, "cancellation confirmation")
        self.cancellation_confirmed_utc = self.cancellation_confirmed_utc or at
        self._log(at, "CANCELLATION_CONFIRMED")
        if self.phase is OrderPhase.CANCEL_REQUESTED:
            if self.filled_quantity == 0:
                self._finalize(at, EntryOutcome.ENTRY_NOT_FILLED, "NO_FILL_BEFORE_DEADLINE")
            else:
                self._finalize(at, EntryOutcome.ENTRY_PARTIALLY_FILLED, "REMAINDER_CANCELLED_AT_DEADLINE")
        elif self.phase is OrderPhase.FINAL and self.outcome is not EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN:
            self._contradiction(at, Contradiction.INCOMPATIBLE_TERMINAL_STATES, f"cancel confirmed after {self.outcome.value}")

    def report_contradiction(self, at: pd.Timestamp, kind: Contradiction, detail: str) -> None:
        """For contradictions detected outside this record (e.g. linked records disagree on side/contract)."""
        if type(kind) is not Contradiction:
            raise TypeError("kind must be a Contradiction")
        self._contradiction(at, kind, detail)
        self._request_cancel(at, DeadlineReason.LOSS_OF_RELIABLE_STATE)

    # ------------------------------------------------------------------ time, invalidation, state loss
    def advance(self, at: pd.Timestamp) -> None:
        """Apply the acknowledgement timeout and the working deadline up to ``at``."""
        if self.order_submitted_utc is None:
            return
        ack_deadline = self.order_submitted_utc + self.params.acknowledgement_timeout
        if at >= ack_deadline and not self._acknowledgement_checked:
            self._acknowledgement_checked = True
            ack_in_time = self.broker_acknowledged_utc is not None and self.broker_acknowledged_utc <= ack_deadline
            fills_in_time = any(f.timestamp_utc <= ack_deadline for f in self.fills)
            terminal_in_time = self.final_order_state_utc is not None and self.final_order_state_utc <= ack_deadline
            if ack_in_time:
                pass
            elif fills_in_time:
                # D-029: an authoritative fill proves market receipt; record the anomaly, never "unknown" for this alone.
                self._anomaly(ack_deadline, "ACKNOWLEDGEMENT_MISSING_BUT_FILL_CONFIRMED")
                self.reconciliation_required = True
                self._act(ACK_MISSING_ACTIONS)
            elif not terminal_in_time:  # a rejection is also an authoritative order state
                self._mark_unknown(ack_deadline, "NO_ACKNOWLEDGEMENT_OR_AUTHORITATIVE_STATE_WITHIN_TIMEOUT")
        if self.phase is not OrderPhase.FINAL and at >= self.deadline_utc:
            self._request_cancel(self.deadline_utc, self.deadline_reason)

    def invalidate(self, at: pd.Timestamp, reason: DeadlineReason) -> None:
        """An unscheduled event ends the working time now (safety halt, contract/session invalidation)."""
        if reason is DeadlineReason.LOSS_OF_RELIABLE_STATE:
            raise ValueError("use on_state_lost for a loss of reliable state")
        if self.order_submitted_utc is None or self.phase is OrderPhase.FINAL:
            return
        self._require(at, self.order_submitted_utc, "invalidation")
        if at < self.deadline_utc:
            self.deadline_utc, self.deadline_reason = at, reason
        self._request_cancel(at, reason)

    def on_state_lost(self, at: pd.Timestamp, cause: StateLossCause) -> None:
        """Loss of reliable order, position or account state -> ENTRY_ORDER_STATE_UNKNOWN (fail closed)."""
        if type(cause) is not StateLossCause:
            raise TypeError("cause must be a StateLossCause")
        if self.order_submitted_utc is None:
            raise ValueError("no order state to lose before submission")
        self._require(at, self.order_submitted_utc, "state loss")
        self._mark_unknown(at, f"LOST_RELIABLE_STATE:{cause.value}")
        if self.phase is not OrderPhase.FINAL and at < self.deadline_utc:
            self.deadline_utc, self.deadline_reason = at, DeadlineReason.LOSS_OF_RELIABLE_STATE
        self._request_cancel(at, DeadlineReason.LOSS_OF_RELIABLE_STATE)

    def reconcile(self, at: pd.Timestamp, confirmed_position_quantity: int | None, side: Side | None, contract: str | None) -> None:
        """Append a NEW current-exposure record. History (outcome, anomalies) is never rewritten.

        ``confirmed_position_quantity=None`` means the position query failed.
        Reconciliation never restores new-entry eligibility for the date.
        """
        if self.order_submitted_utc is None:
            raise ValueError("nothing to reconcile before submission")
        q = confirmed_position_quantity
        if q is not None and (isinstance(q, bool) or not isinstance(q, int) or q < 0):
            raise ValueError("confirmed position quantity must be a whole number >= 0, or None if unknown")
        if q is None:
            self._reconciliations.append(ReconciliationRecord(at, ExposureState.RECONCILIATION_UNRESOLVED, None, "position query failed"))
            self._log(at, "RECONCILED", ExposureState.RECONCILIATION_UNRESOLVED.value)
            self.on_state_lost(at, StateLossCause.POSITION_QUERY_FAILURE)
            return
        if q > 0 and (side is not self.side or contract != self.candidate.contract):
            self._contradiction(at, Contradiction.SIDE_OR_CONTRACT_MISMATCH, f"broker {side} {contract}")
            self._reconciliations.append(ReconciliationRecord(at, ExposureState.RECONCILIATION_UNRESOLVED, q, "side or contract mismatch"))
            self._log(at, "RECONCILED", ExposureState.RECONCILIATION_UNRESOLVED.value)
            return
        if q != self.filled_quantity:
            self._contradiction(at, Contradiction.POSITION_DIFFERS_FROM_AUTHORITATIVE_FILLS, f"position {q}, fills {self.filled_quantity}")
        state = (
            ExposureState.RECONCILED_FLAT if q == 0
            else ExposureState.RECONCILED_PARTIAL_POSITION if q < self.quantity
            else ExposureState.RECONCILED_OPEN_POSITION
        )
        self._reconciliations.append(ReconciliationRecord(at, state, q, f"authoritative fills {self.filled_quantity}"))
        self._log(at, "RECONCILED", f"{state.value} {q}")
        if q:
            self._act(RECONCILED_POSITION_ACTIONS)
            self._update_protection(at)

    # ------------------------------------------------------------------ fills, slippage, actual risk, protection
    def _fill_beyond_stop(self) -> bool:
        avg = self.average_fill_price
        stop = Fraction(self.candidate.planned_stop_price)
        return avg is not None and ((avg <= stop) if self.side is Side.LONG else (avg >= stop))

    def _update_protection(self, at: pd.Timestamp) -> None:
        if self._fill_beyond_stop() and not self.emergency_flatten_required:
            # D-029 decision 5: never an intentionally unprotected position, never a stop on the wrong side.
            self.emergency_flatten_required = True
            self._reason("ENTRY_FILLED_AT_OR_BEYOND_INVALIDATION", "EMERGENCY_FLATTEN_REQUIRED")
            self._act(EMERGENCY_FLATTEN_ACTIONS)
            self._anomaly(at, "ENTRY_FILLED_AT_OR_BEYOND_INVALIDATION", f"average fill {self.average_fill_price}")
        created = self.protection_task.created_utc if self.protection_task else at
        risk = self.actual_risk_points_per_contract  # None when only a reconciled position is known: fail closed
        self.protection_task = ProtectionTask(
            self.order_id,
            self.side,
            self.confirmed_position_quantity,
            self.candidate.planned_stop_price,  # frozen: never recalculated, never widened
            self.candidate.planned_target_price,
            created,
            risk is not None and risk > 0,
            EMERGENCY_FLATTEN_STATUS if self.emergency_flatten_required else PROTECTION_TASK_STATUS,
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

    @property
    def actual_risk_exceeds_planned(self) -> bool:
        risk = self.actual_risk_points_per_contract
        return risk is not None and risk > Fraction(self.candidate.planned_risk_points)

    @property
    def actual_gross_rr_to_frozen_target(self) -> Fraction | None:
        """Degraded (or improved) R:R on the actual fill; the target is never moved to restore the plan."""
        risk = self.actual_risk_points_per_contract
        if risk is None or risk <= 0:
            return None
        return self._signed(Fraction(self.candidate.planned_target_price) - self.average_fill_price) / risk

    def actual_r_multiple(self, exit_price: Decimal) -> Fraction:
        """R on the actual fill (never the planned entry); later rounds supply the exit."""
        risk = self.actual_risk_points_per_contract
        if risk is None or risk <= 0:
            raise ValueError("no valid actual risk: the stop is not on the protective side of the actual fill")
        return self._signed(Fraction(exit_price) - self.average_fill_price) / risk

    # ------------------------------------------------------------------ audit
    def audit_record(self) -> dict[str, Any]:
        c = self.candidate
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
            "fills": [[_iso(f.timestamp_utc), f.quantity, str(f.price), f.source.value] for f in self.fills],
            "local_fill_estimates": [[_iso(f.timestamp_utc), f.quantity, str(f.price)] for f in self.local_fill_estimates],
            "filled_quantity": self.filled_quantity,
            "average_fill_price": _str(self.average_fill_price),
            "slippage_vs_confirmation_close_points": _str(self.slippage_vs_confirmation_close_points),
            "slippage_vs_planned_entry_points": _str(self.slippage_vs_planned_entry_points),
            "submission_to_first_fill": _str(self.submission_to_first_fill),
            "submission_to_final_fill": _str(self.submission_to_final_fill),
            "actual_risk_points_per_contract": _str(self.actual_risk_points_per_contract),
            "actual_risk_exceeds_planned": self.actual_risk_exceeds_planned,
            "actual_gross_rr_to_frozen_target": _str(self.actual_gross_rr_to_frozen_target),
            "frozen_stop_price": str(c.planned_stop_price),
            "frozen_target_price": str(c.planned_target_price),
            "rejection_code": self.rejection_code,
            "rejection_message": self.rejection_message,
            "halts_trading_date": self.halts_trading_date,
            "consumed_filled_entry": self.consumed_filled_entry,
            "reconciliation_required": self.reconciliation_required,
            "critical_alert": self.critical_alert,
            "cancellation_race_fill": self.cancellation_race_fill,
            "emergency_flatten_required": self.emergency_flatten_required,
            "required_actions": list(self.required_actions),
            "operational_anomalies": [[_iso(a.timestamp_utc), a.code, a.detail] for a in self._anomalies],
            "reconciliations": [
                [_iso(r.timestamp_utc), r.exposure_state.value, r.confirmed_position_quantity, r.detail] for r in self._reconciliations
            ],
            "protection_task": None if self.protection_task is None else {
                "quantity": self.protection_task.quantity,
                "status": self.protection_task.status,
                "stop_protective_of_actual_entry": self.protection_task.stop_protective_of_actual_entry,
            },
            "events": [list(e) for e in self.events],
            "specification_version": self.params.specification_version,
            "configuration_hash": self.params.configuration_hash,
        }


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
    """Entry orders for one trading date and one contract.

    Enforces one lifecycle per selected candidate, one use per identity, and
    the filled-entry daily allowance (D-029 decision 4).
    """

    trading_date: Any
    contract: str
    params: EntryOrderParams
    orders: list[EntryOrder] = field(default_factory=list)
    position_flat_confirmed_utc: pd.Timestamp | None = None
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
        """Create the ONE entry-order lifecycle for a recorded selection."""
        if self._awaiting_order.pop(candidate.candidate_id, None) is not candidate:
            raise CandidateReuseError(f"{candidate.candidate_id} is not a recorded, unused selection")
        if created_at != candidate.decision_timestamp:
            raise ValueError("the entry order is created at the decision time")
        blockers = []
        if self.filled_entry_allowance_consumed:
            blockers.append(DailyEntryState.FILLED_ENTRY_LIMIT_REACHED.value)
        if self.halted:
            blockers.append("DAILY_ENTRY_HALT")
        order = EntryOrder(candidate, self.params, created_at, self.params.research_quantity_contracts, order_id=f"{candidate.candidate_id}:ENTRY")
        order._log(created_at, "ORDER_CREATED", f"MARKET {order.side.value} {order.quantity} (research quantity)")
        problems = blockers + list(stop_validity_problems(candidate, self.params))
        if problems:
            order._finalize(created_at, EntryOutcome.NOT_SUBMITTED_INELIGIBLE, *problems)
        self.orders.append(order)
        return order

    def record_position_flat(self, at: pd.Timestamp) -> None:
        """A later exit layer confirms the strategy position is flat. The allowance stays consumed."""
        self.position_flat_confirmed_utc = at

    @property
    def filled_entries(self) -> int:
        return sum(o.consumed_filled_entry for o in self.orders)

    @property
    def filled_entry_allowance_consumed(self) -> bool:
        return self.filled_entries >= self.params.max_filled_entries_per_trading_date

    @property
    def halted(self) -> bool:
        return any(o.halts_trading_date for o in self.orders)

    @property
    def daily_state(self) -> DailyEntryState:
        if self.filled_entry_allowance_consumed:
            return DailyEntryState.FILLED_ENTRY_LIMIT_REACHED
        return DailyEntryState.HALTED if self.halted else DailyEntryState.OPEN_FOR_ENTRIES

    def controls(self) -> dict[str, ControlState]:
        """This book's contribution to ExecutionEligibility (unknown order state -> UNKNOWN)."""
        unknown = any(o.outcome is EntryOutcome.ENTRY_ORDER_STATE_UNKNOWN for o in self.orders)
        exposure = any(o.consumed_filled_entry for o in self.orders) and self.position_flat_confirmed_utc is None
        working = any(o.phase in (OrderPhase.SUBMITTED, OrderPhase.ACKNOWLEDGED, OrderPhase.CANCEL_REQUESTED) for o in self.orders)
        blocked, clear = ControlState.BLOCKED, ControlState.CLEAR
        return {
            "daily_entry_halt": blocked if self.halted else clear,
            "filled_entry_allowance_available": blocked if self.filled_entry_allowance_consumed else clear,
            "no_open_position": ControlState.UNKNOWN if unknown else blocked if exposure else clear,
            "no_working_entry_order": ControlState.UNKNOWN if unknown else blocked if working else clear,
        }
