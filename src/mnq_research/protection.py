"""Protective stop/target (OCO bracket) lifecycle for Baseline B0 (Rule Freeze Round 14).

COMPONENT OF A DRAFT SPECIFICATION. SIMULATION ONLY: no broker, Quantower,
Rithmic, paper or live connection. Broker reports are *inputs* fed in by a
later simulator/adapter or by tests.

Every positive authoritative entry fill creates PROTECTION_REQUIRED. The
bracket then protects the CONFIRMED open quantity with

    one STOP_MARKET order at the frozen structural stop, and
    one LIMIT order at the frozen structural target,
    linked by a native/server-side OCO relationship.

Protection is PROTECTION_ACTIVE only when authoritative state confirms the
stop, the target, the OCO link, the quantities, the prices, and the side,
contract and account. Until then it is PROTECTION_PENDING; a submitted
bracket is never "active". Any failure of the stop, the target, the OCO
link, the quantity synchronisation or the dispatch deadline requires
EMERGENCY_FLATTEN_REQUIRED and halts new entries for the date. Prices are
never trailed, widened, tightened, moved to breakeven or recalculated.

Exits: a position is flat only when an authoritative position report says
quantity 0; remaining siblings are then cancelled and confirmed terminal.
The trade outcome is one of TARGET_FILLED, STOP_FILLED,
SCHEDULED_NEWS_FLATTEN, SESSION_EMERGENCY_FLATTEN, PROTECTION_FAILURE_FLATTEN,
ENTRY_INVALIDATION_FLATTEN, MANUAL_SAFETY_FLATTEN or UNKNOWN_EXIT_STATE.

The research replay helpers (stop trigger, conservative target fill,
same-bar ambiguity) decide WHEN an exit happens; stop-fill prices are left to
the later frozen stop-slippage model and are never assumed to equal the
trigger price.

Not implemented here: commissions, slippage distributions, dynamic sizing,
backtesting, broker connectivity.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Any, Iterable, Mapping

import pandas as pd

from mnq_research.confirmation import Side
from mnq_research.entry_order import (
    LIVE_OR_PAPER_SUBMISSION_STATUS,
    EntryOrder,
    EntryOrderParams,
    FillSource,
    OperationalAnomaly,
    stop_validity_problems,
)
from mnq_research.structural_levels import NEW_YORK, TICK, ny_time

STOP_ORDER_TYPE = "STOP_MARKET"
TARGET_ORDER_TYPE = "LIMIT"
PROTECTIVE_TIME_IN_FORCE = "DAY"
SAME_BAR_POLICY = "CONSERVATIVE_STOP_FIRST"
COST_STATUS = "UNRESOLVED_UNTIL_COST_MODEL_FROZEN"
STOP_FILL_PRICE_MODEL = "UNRESOLVED_UNTIL_STOP_SLIPPAGE_MODEL_FROZEN"
TARGET_FILL_FLAG = "CONSERVATIVE_ONE_TICK_TRADE_THROUGH_TARGET_FILL"


class BracketMode(str, Enum):
    NATIVE_SERVER_SIDE_OCO_BRACKET = "NATIVE_SERVER_SIDE_OCO_BRACKET"  # preferred: atomic
    SEPARATE_SERVER_SIDE_ORDERS = "SEPARATE_SERVER_SIDE_ORDERS"  # stop first, then target, then OCO link
    CLIENT_SIDE_ONLY = "CLIENT_SIDE_ONLY"  # insufficient for an unattended strategy


PREFERRED_BRACKET_MODE = BracketMode.NATIVE_SERVER_SIDE_OCO_BRACKET


def choose_bracket_mode(supports_atomic_server_side_oco: bool, supports_separate_server_side_orders: bool) -> BracketMode:
    """Atomic native bracket first; separate server-side orders second; otherwise insufficient."""
    if supports_atomic_server_side_oco is True:
        return BracketMode.NATIVE_SERVER_SIDE_OCO_BRACKET
    if supports_separate_server_side_orders is True:
        return BracketMode.SEPARATE_SERVER_SIDE_ORDERS
    return BracketMode.CLIENT_SIDE_ONLY


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


def protective_action(position_side: Side) -> OrderAction:
    return OrderAction.SELL if position_side is Side.LONG else OrderAction.BUY


class ProtectionState(str, Enum):
    PROTECTION_REQUIRED = "PROTECTION_REQUIRED"
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTION_ACTIVE = "PROTECTION_ACTIVE"
    EMERGENCY_FLATTEN_REQUIRED = "EMERGENCY_FLATTEN_REQUIRED"
    SCHEDULED_FLATTEN_IN_PROGRESS = "SCHEDULED_FLATTEN_IN_PROGRESS"
    AWAITING_FLAT_CONFIRMATION = "AWAITING_FLAT_CONFIRMATION"
    TRADE_CLOSED = "TRADE_CLOSED"


class Component(str, Enum):
    STOP = "STOP"
    TARGET = "TARGET"
    OCO_LINK = "OCO_LINK"


class ComponentStatus(str, Enum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    PENDING = "PENDING"  # submitted, modified or cancel-requested; awaiting authoritative confirmation
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    FILLED = "FILLED"
    FAILED = "FAILED"
    CANCELLATION_UNKNOWN = "CANCELLATION_UNKNOWN"  # D-031: may still be live; query and reconcile


class PendingReason(str, Enum):
    INITIAL = "INITIAL"
    ENTRY_QUANTITY_SYNC = "ENTRY_QUANTITY_SYNC"
    EXIT_REDUCTION = "EXIT_REDUCTION"
    CANCEL = "CANCEL"


class ExitSource(str, Enum):
    STOP = "STOP"
    TARGET = "TARGET"
    FLATTEN = "FLATTEN"  # emergency / scheduled / manual market close


class ExitOutcome(str, Enum):
    TARGET_FILLED = "TARGET_FILLED"
    STOP_FILLED = "STOP_FILLED"
    SCHEDULED_NEWS_FLATTEN = "SCHEDULED_NEWS_FLATTEN"
    SESSION_EMERGENCY_FLATTEN = "SESSION_EMERGENCY_FLATTEN"
    PROTECTION_FAILURE_FLATTEN = "PROTECTION_FAILURE_FLATTEN"
    ENTRY_INVALIDATION_FLATTEN = "ENTRY_INVALIDATION_FLATTEN"
    MANUAL_SAFETY_FLATTEN = "MANUAL_SAFETY_FLATTEN"
    NORMAL_TIME_EXIT = "NORMAL_TIME_EXIT"  # Round 15: the planned 12:00 New York market exit
    MIXED_STOP_TARGET_EXIT = "MIXED_STOP_TARGET_EXIT"  # D-031: both stop and target filled portions
    UNKNOWN_EXIT_STATE = "UNKNOWN_EXIT_STATE"


class FlatteningLeg(str, Enum):
    """Which leg made the position flat; always the most specific known value (Round 15)."""

    STOP = "STOP"
    TARGET = "TARGET"
    NORMAL_TIME_EXIT = "NORMAL_TIME_EXIT"
    NEWS_FLATTEN = "NEWS_FLATTEN"
    SESSION_BACKSTOP = "SESSION_BACKSTOP"
    PROTECTION_FAILURE = "PROTECTION_FAILURE"
    ENTRY_INVALIDATION = "ENTRY_INVALIDATION"
    MANUAL_SAFETY = "MANUAL_SAFETY"
    OTHER = "OTHER"  # only when no defined category fits
    UNKNOWN = "UNKNOWN"


FLATTEN_LEG = {
    ExitOutcome.NORMAL_TIME_EXIT: FlatteningLeg.NORMAL_TIME_EXIT,
    ExitOutcome.SCHEDULED_NEWS_FLATTEN: FlatteningLeg.NEWS_FLATTEN,
    ExitOutcome.SESSION_EMERGENCY_FLATTEN: FlatteningLeg.SESSION_BACKSTOP,
    ExitOutcome.PROTECTION_FAILURE_FLATTEN: FlatteningLeg.PROTECTION_FAILURE,
    ExitOutcome.ENTRY_INVALIDATION_FLATTEN: FlatteningLeg.ENTRY_INVALIDATION,
    ExitOutcome.MANUAL_SAFETY_FLATTEN: FlatteningLeg.MANUAL_SAFETY,
}


# Flatten reasons that a caller may start deliberately (failures start their own).
SCHEDULED_FLATTEN_OUTCOMES = frozenset(
    {ExitOutcome.NORMAL_TIME_EXIT, ExitOutcome.SESSION_EMERGENCY_FLATTEN, ExitOutcome.SCHEDULED_NEWS_FLATTEN, ExitOutcome.MANUAL_SAFETY_FLATTEN}
)

EMERGENCY_ACTIONS = (
    "HALT_NEW_ENTRIES_FOR_TRADING_DATE",
    "PRESERVE_ORIGINAL_STRUCTURAL_STOP_FOR_AUDIT",
    "BEGIN_EMERGENCY_FLATTEN_OF_CONFIRMED_QUANTITY",
    "RECONCILE_POSITION_AND_ORDERS",
    "CRITICAL_ALERT",
    "DO_NOT_WIDEN_OR_RECREATE_STRATEGY_RISK",
)
TARGET_FAILURE_ACTIONS = ("CANCEL_TARGET_AND_RECONCILE", "PRESERVE_STOP_UNTIL_FLAT_CONFIRMED")
UNKNOWN_EXIT_ACTIONS = (
    "RECONCILE_BROKER_POSITION",
    "DETECT_UNINTENDED_REVERSE_POSITION",
    "FLATTEN_ANY_UNINTENDED_EXPOSURE",
    "CANCEL_REMAINING_ORDERS",
    "CRITICAL_ALERT",
    "HALT_NEW_ENTRIES_FOR_TRADING_DATE",
)
CANCELLATION_UNKNOWN_ACTIONS = ("QUERY_AUTHORITATIVE_POSITION_AND_ORDERS", "CONTINUE_CANCELLING_AND_RECONCILING", "HALT_NEW_ENTRIES_FOR_TRADING_DATE")


def _whole(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be a whole number >= {minimum}, got {value!r}")
    return value


@dataclass(frozen=True)
class ProtectionParams:
    stop_order_type: str
    target_order_type: str
    time_in_force: str
    dispatch_deadline: pd.Timedelta
    acknowledgement_timeout: pd.Timedelta
    preferred_bracket_mode: BracketMode
    target_fill_trade_through_ticks: int
    same_bar_policy: str
    live_or_paper_order_submission: str
    point_value_usd: Decimal
    normal_flatten_time: dt.time
    specification_version: str
    configuration_hash: str

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "ProtectionParams":
        from mnq_research.validation import rule_spec_hash  # local import avoids a module cycle

        p = spec["protective_orders"]["parameters"]
        seconds = Decimal(str(p["protective_order_acknowledgement_timeout_seconds"]))
        ack = pd.Timedelta(seconds=float(seconds))
        if not seconds.is_finite() or seconds <= 0 or Decimal(str(ack.total_seconds())) != seconds:
            raise ValueError("protective_order_acknowledgement_timeout_seconds must be positive and exact")
        params = cls(
            p["protective_stop_order_type"],
            p["profit_target_order_type"],
            p["protective_time_in_force"],
            pd.Timedelta(milliseconds=_whole(p["protection_dispatch_deadline_milliseconds"], "protection_dispatch_deadline_milliseconds", 1)),
            ack,
            BracketMode(p["preferred_bracket_mode"]),
            _whole(p["target_fill_trade_through_ticks"], "target_fill_trade_through_ticks", 0),
            p["same_bar_stop_target_policy"],
            spec["entry_order_lifecycle"]["protective_order_dependency"]["live_or_paper_order_submission"],
            Decimal(str(spec["instrument"]["point_value_usd"])),
            dt.time.fromisoformat(str(spec["position_management"]["normal_flatten_time"])),
            str(spec["specification"]["version"]),
            rule_spec_hash(dict(spec)),
        )
        expected = (STOP_ORDER_TYPE, TARGET_ORDER_TYPE, PROTECTIVE_TIME_IN_FORCE, PREFERRED_BRACKET_MODE, SAME_BAR_POLICY)
        actual = (params.stop_order_type, params.target_order_type, params.time_in_force, params.preferred_bracket_mode, params.same_bar_policy)
        if actual != expected:
            raise ValueError(f"B0 implements only {expected}, got {actual}")
        if params.live_or_paper_order_submission != LIVE_OR_PAPER_SUBMISSION_STATUS:
            raise ValueError("live or paper order submission is prohibited")
        if spec["position_management"]["normal_flatten_timezone"] != str(NEW_YORK):
            raise ValueError("the normal flatten time is defined in America/New_York")
        return params


@dataclass(frozen=True)
class ProtectionRequirement:
    """PROTECTION_REQUIRED: created immediately by every positive authoritative entry fill."""

    entry_order_id: str
    position_id: str
    account_id: str
    contract: str
    side: Side
    confirmed_open_quantity: int
    actual_average_entry: Any  # exact Fraction
    frozen_stop_price: Decimal
    frozen_target_price: Decimal
    zone_version_id: str
    attempt_id: int
    acceptance_id: str
    confirmation_id: str
    candidate_id: str
    first_fill_utc: pd.Timestamp
    fill_received_utc: pd.Timestamp
    protection_deadline_utc: pd.Timestamp
    entry_invalidation_latched: bool
    stop_validation_problems: tuple[str, ...]
    specification_version: str
    configuration_hash: str
    status: str = ProtectionState.PROTECTION_REQUIRED.value

    @classmethod
    def from_entry(
        cls, order: EntryOrder, entry_params: EntryOrderParams, params: ProtectionParams, account_id: str, fill_received_utc: pd.Timestamp
    ) -> "ProtectionRequirement":
        if not order.fills:
            raise ValueError("protection is created only by a positive AUTHORITATIVE entry fill")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("an account identifier is required")
        c = order.candidate
        return cls(
            order.order_id,
            f"{order.order_id}:POSITION",
            account_id,
            c.contract,
            c.direction,
            order.confirmed_position_quantity,  # never the intended order quantity
            order.average_fill_price,
            c.planned_stop_price,
            c.planned_target_price,
            c.zone_version_id,
            c.attempt_id,
            c.acceptance_id,
            c.confirmation_id,
            c.candidate_id,
            order.first_fill_utc,
            fill_received_utc,
            fill_received_utc + params.dispatch_deadline,
            order.emergency_flatten_required,
            stop_validity_problems(c, entry_params),
            params.specification_version,
            params.configuration_hash,
        )


@dataclass(frozen=True)
class BrokerOrderReport:
    """Authoritative state of one protective order (a local object or request is not confirmation)."""

    order_type: str
    price: Decimal
    quantity: int
    action: OrderAction
    contract: str
    account_id: str


@dataclass
class ProtectiveComponent:
    kind: Component
    price: Decimal | None
    order_type: str | None
    quantity: int = 0  # required quantity (always the confirmed open quantity)
    status: ComponentStatus = ComponentStatus.NOT_SUBMITTED
    pending_reason: PendingReason | None = None
    pending_since_utc: pd.Timestamp | None = None
    submitted_utc: pd.Timestamp | None = None
    confirmed_utc: pd.Timestamp | None = None
    confirmed_quantity: int | None = None
    filled_quantity: int = 0
    failure: str | None = None

    @property
    def live(self) -> bool:
        """Could still execute: working or awaiting confirmation (including a pending cancel)."""
        return self.status in (ComponentStatus.PENDING, ComponentStatus.CONFIRMED, ComponentStatus.CANCELLATION_UNKNOWN)

    def request(self, at: pd.Timestamp, reason: PendingReason) -> None:
        self.status, self.pending_reason, self.pending_since_utc = ComponentStatus.PENDING, reason, at
        if self.submitted_utc is None:
            self.submitted_utc = at


@dataclass(frozen=True)
class ExitFill:
    """One partial or full exit, preserved individually (D-031)."""

    timestamp_utc: pd.Timestamp
    source: ExitSource
    quantity: int
    price: Decimal
    order_id: str
    realized_gross_pnl_points: Fraction  # (exit - actual average entry) x quantity, signed for the side
    realized_gross_pnl_usd: Fraction  # before commissions and slippage costs (cost model not frozen)
    remaining_position_after: int


@dataclass
class ProtectiveBracket:
    """The protective-order lifecycle for one strategy position (simulation state machine)."""

    requirement: ProtectionRequirement
    params: ProtectionParams
    mode: BracketMode
    state: ProtectionState = ProtectionState.PROTECTION_REQUIRED
    open_quantity: int = 0
    dispatched_utc: pd.Timestamp | None = None
    active_since_utc: pd.Timestamp | None = None
    emergency_flatten_required: bool = False
    flatten_reason: ExitOutcome | None = None
    flatten_requested_utc: pd.Timestamp | None = None
    exit_state_unknown: bool = False
    unintended_exposure_quantity: int = 0
    flat_confirmed_utc: pd.Timestamp | None = None
    closed_utc: pd.Timestamp | None = None
    exit_outcome: ExitOutcome | None = None
    closing_source: ExitSource | None = None
    reasons: list[str] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)
    exit_fills: list[ExitFill] = field(default_factory=list)
    events: list[tuple[str, str, str]] = field(default_factory=list)
    _synced_extra: int = 0
    cancellation_unknown: bool = False
    final_flattening_leg: FlatteningLeg | None = None
    broker_reported_exposure: tuple[int, str, str, str] | None = None  # (quantity, side, contract, account)
    _anomalies: list[OperationalAnomaly] = field(default_factory=list)
    stop: ProtectiveComponent = field(init=False)
    target: ProtectiveComponent = field(init=False)
    oco: ProtectiveComponent = field(init=False)

    def __post_init__(self) -> None:
        if type(self.mode) is not BracketMode:
            raise TypeError("mode must be a BracketMode")
        r = self.requirement
        self.open_quantity = r.confirmed_open_quantity
        self.stop = ProtectiveComponent(Component.STOP, r.frozen_stop_price, self.params.stop_order_type)
        self.target = ProtectiveComponent(Component.TARGET, r.frozen_target_price, self.params.target_order_type)
        self.oco = ProtectiveComponent(Component.OCO_LINK, None, None)
        self._log(r.fill_received_utc, "PROTECTION_REQUIRED", f"{r.side.value} {self.open_quantity} {r.contract}")

    # ------------------------------------------------------------------ helpers
    @property
    def halts_trading_date(self) -> bool:
        return self.emergency_flatten_required or self.exit_state_unknown or self.cancellation_unknown

    @property
    def anomalies(self) -> tuple[OperationalAnomaly, ...]:
        return tuple(self._anomalies)

    def _anomaly(self, at: pd.Timestamp, code: str, detail: str = "") -> None:
        self._anomalies.append(OperationalAnomaly(at, code, detail))
        self._log(at, "OPERATIONAL_ANOMALY", f"{code} {detail}".strip())

    @property
    def protection_active(self) -> bool:
        return self.state is ProtectionState.PROTECTION_ACTIVE

    def _log(self, at: pd.Timestamp, event: str, detail: str = "") -> None:
        self.events.append((at.isoformat(), event, detail))

    def _act(self, actions: Iterable[str]) -> None:
        for a in actions:
            if a not in self.required_actions:
                self.required_actions.append(a)

    def _reason(self, *reasons: str) -> None:
        self.reasons.extend(r for r in reasons if r not in self.reasons)

    def _component(self, kind: Component) -> ProtectiveComponent:
        return {Component.STOP: self.stop, Component.TARGET: self.target, Component.OCO_LINK: self.oco}[kind]

    def _emergency(self, at: pd.Timestamp, reason: str, outcome: ExitOutcome = ExitOutcome.PROTECTION_FAILURE_FLATTEN) -> None:
        self._reason(reason, "EMERGENCY_FLATTEN_REQUIRED")
        self._act(EMERGENCY_ACTIONS)
        self._log(at, "EMERGENCY_FLATTEN_REQUIRED", reason)
        if not self.emergency_flatten_required:
            self.emergency_flatten_required = True
            if self.flatten_reason is None or self.flatten_reason in SCHEDULED_FLATTEN_OUTCOMES:
                self.flatten_reason = outcome
            self.flatten_requested_utc = self.flatten_requested_utc or at
        if self.state is not ProtectionState.TRADE_CLOSED:
            self.state = ProtectionState.EMERGENCY_FLATTEN_REQUIRED

    def _fail(self, at: pd.Timestamp, kind: Component, code: str) -> None:
        component = self._component(kind)
        component.status, component.failure = ComponentStatus.FAILED, code
        if kind is Component.STOP:
            self._emergency(at, f"PROTECTIVE_STOP_FAILURE:{code}")
        elif kind is Component.TARGET:
            # B0 never turns into an unplanned "let the stop run" position.
            self._reason("TARGET_PROTECTION_FAILURE")
            self._act(TARGET_FAILURE_ACTIONS)
            self._emergency(at, f"TARGET_PROTECTION_FAILURE:{code}")
        else:
            self._emergency(at, f"OCO_LINK_FAILURE:{code}")

    def _unknown_exit(self, at: pd.Timestamp, reason: str, excess: int = 0) -> None:
        self.exit_state_unknown = True
        self.unintended_exposure_quantity += excess
        self._reason(reason)
        self._anomaly(at, reason, f"unintended exposure +{excess}")
        self._act(UNKNOWN_EXIT_ACTIONS)
        self._log(at, "UNKNOWN_EXIT_STATE", reason)
        if self.state is ProtectionState.TRADE_CLOSED:
            self.exit_outcome = ExitOutcome.UNKNOWN_EXIT_STATE  # the earlier close is kept in the event log

    def _evaluate(self, at: pd.Timestamp) -> None:
        if self.state in (
            ProtectionState.TRADE_CLOSED,
            ProtectionState.EMERGENCY_FLATTEN_REQUIRED,
            ProtectionState.SCHEDULED_FLATTEN_IN_PROGRESS,
            ProtectionState.AWAITING_FLAT_CONFIRMATION,
        ):
            return
        stop_ok = self.stop.status is ComponentStatus.CONFIRMED
        if self.target.status is ComponentStatus.CONFIRMED and self.stop.status not in (ComponentStatus.CONFIRMED, ComponentStatus.PENDING):
            self._emergency(at, "TARGET_WITHOUT_STOP")  # a target without a stop is not protection
            return
        all_confirmed = stop_ok and self.target.status is ComponentStatus.CONFIRMED and self.oco.status is ComponentStatus.CONFIRMED
        quantities = self.stop.confirmed_quantity == self.target.confirmed_quantity == self.open_quantity
        if all_confirmed and quantities and self.open_quantity > 0:
            if self.state is not ProtectionState.PROTECTION_ACTIVE:
                self.state, self.active_since_utc = ProtectionState.PROTECTION_ACTIVE, at
                self._log(at, "PROTECTION_ACTIVE", f"quantity {self.open_quantity}")
        elif self.dispatched_utc is not None:
            self.state = ProtectionState.PROTECTION_PENDING

    # ------------------------------------------------------------------ dispatch
    def dispatch(self, at: pd.Timestamp) -> None:
        """Begin protective submission (same event cycle as the fill; never later than the deadline)."""
        if self.dispatched_utc is not None or self.state is not ProtectionState.PROTECTION_REQUIRED:
            raise ValueError("protection has already been dispatched or superseded")
        r = self.requirement
        if at < r.fill_received_utc:
            raise ValueError("protection cannot be dispatched before the fill is received")
        self.dispatched_utc = at
        if at > r.protection_deadline_utc:
            self._emergency(at, "PROTECTION_DISPATCH_DEADLINE_MISSED")
            return
        if r.entry_invalidation_latched:
            # D-029/D-030: no normal target, no pretend bracket; flatten immediately.
            self._emergency(at, "ENTRY_FILLED_AT_OR_BEYOND_INVALIDATION", ExitOutcome.ENTRY_INVALIDATION_FLATTEN)
            return
        if r.stop_validation_problems:
            self._reason(*r.stop_validation_problems)
            self._emergency(at, "INVALID_STRUCTURAL_STOP")  # discovered after a fill: the position is unprotected
            return
        if self.mode is BracketMode.CLIENT_SIDE_ONLY:
            self._emergency(at, "PROTECTION_CAPABILITY_INSUFFICIENT:CLIENT_SIDE_ONLY")
            return
        self.state = ProtectionState.PROTECTION_PENDING
        for c in (self.stop, self.target, self.oco):
            c.quantity = self.open_quantity
        self.stop.request(at, PendingReason.INITIAL)
        if self.mode is BracketMode.NATIVE_SERVER_SIDE_OCO_BRACKET:  # one atomic server-side unit
            self.target.request(at, PendingReason.INITIAL)
            self.oco.request(at, PendingReason.INITIAL)
        self._log(at, "PROTECTION_DISPATCHED", self.mode.value)

    # ------------------------------------------------------------------ authoritative reports
    def on_order_confirmed(self, at: pd.Timestamp, kind: Component, report: BrokerOrderReport) -> None:
        if kind is Component.OCO_LINK:
            raise ValueError("use on_oco_confirmed for the OCO link")
        if type(report) is not BrokerOrderReport:
            raise TypeError("confirmation requires an authoritative BrokerOrderReport")
        c = self._component(kind)
        if c.status is not ComponentStatus.PENDING or c.pending_reason is PendingReason.CANCEL:
            return  # nothing awaited (a stale report never creates protection)
        r = self.requirement
        wrong = [
            name
            for name, ok in (
                ("TYPE", report.order_type == c.order_type),
                ("PRICE", report.price == c.price),
                ("SIDE", report.action is protective_action(r.side)),
                ("CONTRACT", report.contract == r.contract),
                ("ACCOUNT", report.account_id == r.account_id),
            )
            if not ok
        ]
        if report.quantity != c.quantity:
            if c.pending_reason in (PendingReason.ENTRY_QUANTITY_SYNC, PendingReason.EXIT_REDUCTION):
                label = "PROTECTION_QUANTITY_UNKNOWN" if c.pending_reason is PendingReason.ENTRY_QUANTITY_SYNC else "OCO_RECONCILIATION_FAILURE"
                self._emergency(at, label)
                c.status, c.failure = ComponentStatus.FAILED, "WRONG_QUANTITY"
                return
            wrong.append("QUANTITY")
        if wrong:
            self._fail(at, kind, "WRONG_" + "_".join(wrong))
            return
        c.status, c.confirmed_utc, c.confirmed_quantity, c.pending_reason, c.pending_since_utc = (
            ComponentStatus.CONFIRMED, at, report.quantity, None, None,
        )
        self._log(at, f"{kind.value}_CONFIRMED", f"{report.quantity} @ {report.price}")
        if self.mode is BracketMode.SEPARATE_SERVER_SIDE_ORDERS and not self.emergency_flatten_required:
            if kind is Component.STOP and self.target.status is ComponentStatus.NOT_SUBMITTED:
                self.target.request(at, PendingReason.INITIAL)  # the target only after the stop is confirmed
            elif kind is Component.TARGET and self.oco.status is ComponentStatus.NOT_SUBMITTED:
                self.oco.request(at, PendingReason.INITIAL)
        self._evaluate(at)

    def on_oco_confirmed(self, at: pd.Timestamp) -> None:
        if self.oco.status is not ComponentStatus.PENDING:
            return
        self.oco.status, self.oco.confirmed_utc, self.oco.pending_reason, self.oco.pending_since_utc = (
            ComponentStatus.CONFIRMED, at, None, None,
        )
        self._log(at, "OCO_LINK_CONFIRMED")
        self._evaluate(at)

    def on_rejected(self, at: pd.Timestamp, kind: Component, code: str) -> None:
        self._log(at, f"{kind.value}_REJECTED", code)
        self._fail(at, kind, f"REJECTED_{code}")

    def on_component_failure(self, at: pd.Timestamp, kind: Component, code: str) -> None:
        """E.g. STATE_UNKNOWN, CANCELLED_WITHOUT_REPLACEMENT, INACTIVE_WHILE_POSITION_OPEN, CLIENT_SERVER_DISAGREEMENT."""
        self._log(at, f"{kind.value}_FAILURE", code)
        self._fail(at, kind, code)

    def on_cancel_confirmed(self, at: pd.Timestamp, kind: Component) -> None:
        c = self._component(kind)
        if (c.status is ComponentStatus.PENDING and c.pending_reason is PendingReason.CANCEL) or c.status is ComponentStatus.CANCELLATION_UNKNOWN:
            c.status, c.pending_reason, c.pending_since_utc = ComponentStatus.CANCELLED, None, None
            self._log(at, f"{kind.value}_CANCELLED")
            self._try_close(at)

    def sync_entry_quantity(self, at: pd.Timestamp, order: EntryOrder) -> None:
        """Additional authoritative entry fills: protect the total CONFIRMED quantity (never the intended one)."""
        confirmed = order.confirmed_position_quantity
        if order.emergency_flatten_required and not self.emergency_flatten_required:
            self._emergency(at, "ENTRY_FILLED_AT_OR_BEYOND_INVALIDATION", ExitOutcome.ENTRY_INVALIDATION_FLATTEN)
        added = confirmed - (self.requirement.confirmed_open_quantity + self._synced_extra)
        if added <= 0:
            return
        self._synced_extra += added
        self.open_quantity += added
        self._log(at, "ENTRY_QUANTITY_INCREASED", f"open {self.open_quantity}")
        for c in (self.stop, self.target):
            if c.status in (ComponentStatus.PENDING, ComponentStatus.CONFIRMED) and c.pending_reason is not PendingReason.CANCEL:
                c.quantity = self.open_quantity
                c.request(at, PendingReason.ENTRY_QUANTITY_SYNC)
            elif c.status is ComponentStatus.NOT_SUBMITTED:
                c.quantity = self.open_quantity
        self._evaluate(at)

    def report_quantity_unsynchronisable(self, at: pd.Timestamp) -> None:
        self._emergency(at, "PROTECTION_QUANTITY_UNKNOWN")

    # ------------------------------------------------------------------ time
    def advance(self, at: pd.Timestamp) -> None:
        r = self.requirement
        if self.dispatched_utc is None and at > r.protection_deadline_utc:
            self.dispatched_utc = r.protection_deadline_utc
            self._emergency(r.protection_deadline_utc, "PROTECTION_DISPATCH_DEADLINE_MISSED")
        for c in (self.stop, self.target, self.oco):  # each has its own independent timeout
            if c.status is ComponentStatus.PENDING and at >= c.pending_since_utc + self.params.acknowledgement_timeout:
                expired = c.pending_since_utc + self.params.acknowledgement_timeout
                if c.pending_reason is PendingReason.ENTRY_QUANTITY_SYNC:
                    c.status, c.failure = ComponentStatus.FAILED, "QUANTITY_SYNC_TIMEOUT"
                    self._emergency(expired, "PROTECTION_QUANTITY_UNKNOWN")
                elif c.pending_reason is PendingReason.EXIT_REDUCTION or (c.pending_reason is PendingReason.CANCEL and self.open_quantity > 0 and self.exit_fills):
                    # A confirmed remainder is still open: emergency flatten it.
                    c.status, c.failure = ComponentStatus.FAILED, "OCO_RECONCILIATION_TIMEOUT"
                    self._emergency(expired, "OCO_RECONCILIATION_FAILURE")
                elif c.pending_reason is PendingReason.CANCEL:
                    # D-031: not an automatic flatten. Query first; act on the authoritative position.
                    c.status, c.failure, c.pending_since_utc = ComponentStatus.CANCELLATION_UNKNOWN, "CANCEL_TIMEOUT", None
                    self.cancellation_unknown = True
                    self._reason("PROTECTIVE_ORDER_CANCELLATION_UNKNOWN")
                    self._act(CANCELLATION_UNKNOWN_ACTIONS)
                    self._anomaly(expired, "PROTECTIVE_ORDER_CANCELLATION_UNKNOWN", c.kind.value)
                else:
                    self._fail(expired, c.kind, "ACKNOWLEDGEMENT_TIMEOUT")

    # ------------------------------------------------------------------ exits
    def begin_flatten(self, at: pd.Timestamp, outcome: ExitOutcome) -> None:
        """Scheduled (session/news) or manual safety flatten: cancel the target, keep the stop, close at market."""
        if outcome not in SCHEDULED_FLATTEN_OUTCOMES:
            raise ValueError("failure flattens are started by the failure itself")
        if (
            self.state in (ProtectionState.TRADE_CLOSED, ProtectionState.AWAITING_FLAT_CONFIRMATION)
            or self.flatten_requested_utc is not None
            or self.open_quantity == 0
        ):
            # An earlier stop, target, news, safety or normal exit already governs: never a duplicate flatten.
            self._log(at, "FLATTEN_NOT_DUPLICATED", outcome.value)
            return
        if self.flatten_reason is None:
            self.flatten_reason = outcome
        self.flatten_requested_utc = self.flatten_requested_utc or at
        if self.target.live:
            self.target.request(at, PendingReason.CANCEL)
        if not self.emergency_flatten_required:
            self.state = ProtectionState.SCHEDULED_FLATTEN_IN_PROGRESS
        self._act(("CANCEL_TARGET", "KEEP_PROTECTIVE_STOP_UNTIL_FLAT_RESOLVED", "SUBMIT_CONTROLLED_MARKET_FLATTEN", "CONFIRM_ZERO_POSITION"))
        self._log(at, "FLATTEN_REQUESTED", outcome.value)

    def apply_normal_time_exit(self, at: pd.Timestamp) -> None:
        """At or after the normal flatten time (12:00 New York), begin the planned market exit once."""
        if at >= ny_time(at.tz_convert(NEW_YORK).date(), self.params.normal_flatten_time):
            self.begin_flatten(at, ExitOutcome.NORMAL_TIME_EXIT)

    def on_exit_fill(self, at: pd.Timestamp, source: ExitSource, quantity: int, price: Decimal, fill_source: FillSource) -> None:
        """An authoritative exit fill (stop, target or flatten market order)."""
        if type(fill_source) is not FillSource:
            raise TypeError("fill source must be a FillSource")
        if fill_source is not FillSource.AUTHORITATIVE_FILL_RECORD:
            self._log(at, "LOCAL_EXIT_ESTIMATE_IGNORED", f"{source.value} {quantity} @ {price}")
            return
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("exit quantity must be a whole number >= 1")
        late = self.flat_confirmed_utc is not None
        excess = quantity if late else max(quantity - self.open_quantity, 0)
        remaining = max(self.open_quantity - quantity, 0)
        sign = Fraction(1) if self.requirement.side is Side.LONG else -Fraction(1)
        pnl_points = sign * (Fraction(price) - Fraction(self.requirement.actual_average_entry)) * quantity
        self.exit_fills.append(
            ExitFill(at, source, quantity, price, f"{self.requirement.position_id}:{source.value}", pnl_points,
                     pnl_points * Fraction(self.params.point_value_usd), remaining)
        )
        self._log(at, "EXIT_FILL", f"{source.value} {quantity} @ {price}; remaining {remaining}")
        sibling = {ExitSource.STOP: self.stop, ExitSource.TARGET: self.target}.get(source)
        if sibling is not None:
            sibling.filled_quantity += quantity
        if excess:
            self.open_quantity = remaining
            # Contradictory: this may reverse the account. UNKNOWN, reconcile, flatten the excess, cancel the rest.
            code = "LATE_SIBLING_FILL_AFTER_FLAT" if late else "EXCESS_SIBLING_FILL_REVERSE_EXPOSURE"
            self._unknown_exit(at, code, excess)
            for c in (self.stop, self.target):
                if c.status in (ComponentStatus.PENDING, ComponentStatus.CONFIRMED) and c.pending_reason is not PendingReason.CANCEL:
                    c.request(at, PendingReason.CANCEL)
            return
        self.open_quantity -= quantity
        if self.open_quantity == 0:
            self.closing_source = source
            if sibling is not None:
                sibling.status, sibling.pending_reason, sibling.pending_since_utc = ComponentStatus.FILLED, None, None
            for other in (self.stop, self.target):
                if other is not sibling and other.live and other.pending_reason is not PendingReason.CANCEL:
                    other.request(at, PendingReason.CANCEL)  # never let a sibling reopen exposure
            if self.state in (ProtectionState.PROTECTION_ACTIVE, ProtectionState.PROTECTION_PENDING):
                self.state = ProtectionState.AWAITING_FLAT_CONFIRMATION
            return
        for c in (self.stop, self.target):  # partial exit: shrink siblings to exactly the remainder
            if c.live and c.pending_reason is not PendingReason.CANCEL:
                c.quantity = self.open_quantity
                if c is sibling:
                    c.confirmed_quantity = self.open_quantity  # the filled order's own remainder
                else:
                    c.request(at, PendingReason.EXIT_REDUCTION)
        if self.state is ProtectionState.PROTECTION_ACTIVE:
            self.state = ProtectionState.PROTECTION_PENDING
        self._evaluate(at)

    def on_position_report(self, at: pd.Timestamp, quantity: int, side: Side | None, contract: str, account_id: str) -> None:
        """Authoritative position. Flat requires quantity 0 here; an exit fill alone is not enough."""
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ValueError("position quantity must be a whole number >= 0")
        r = self.requirement
        identity_ok = contract == r.contract and account_id == r.account_id and (quantity == 0 or side is r.side)
        if not identity_ok or quantity != self.open_quantity:
            # D-031: the broker position is ACTUAL exposure for safety; the disagreement is logged permanently.
            self.broker_reported_exposure = (quantity, None if side is None else side.value, contract, account_id)
            self._reason("POSITION_RECONCILIATION_REQUIRED")
            detail = f"broker {quantity} {side} {contract} {account_id}; expected {self.open_quantity} {r.side.value} {r.contract} {r.account_id}"
            self._unknown_exit(at, "POSITION_MISMATCH", quantity if not identity_ok else max(quantity - self.open_quantity, 0))
            self._log(at, "POSITION_MISMATCH_DETAIL", detail)
            if identity_ok:
                self.open_quantity = quantity
            if quantity > 0:
                self._emergency(at, "BROKER_REPORTED_EXPOSURE_AFTER_MISMATCH")
            return
        self._log(at, "POSITION_REPORT", str(quantity))
        if quantity > 0 and self.cancellation_unknown:
            # D-031: a position remains while a cancellation is unknown -> continue/invoke emergency flattening.
            self._emergency(at, "OPEN_POSITION_WITH_PROTECTIVE_ORDER_CANCELLATION_UNKNOWN")
        if quantity == 0 and self.flat_confirmed_utc is None:
            self.flat_confirmed_utc = at
            for c in (self.stop, self.target):
                if c.live and c.pending_reason is not PendingReason.CANCEL:
                    c.request(at, PendingReason.CANCEL)
            self._try_close(at)

    def _try_close(self, at: pd.Timestamp) -> None:
        if self.flat_confirmed_utc is None or self.state is ProtectionState.TRADE_CLOSED:
            return
        if any(c.live for c in (self.stop, self.target)):
            return  # every sibling must be confirmed terminal first
        sources = {f.source for f in self.exit_fills}
        if self.closing_source is ExitSource.STOP:
            self.final_flattening_leg = FlatteningLeg.STOP
        elif self.closing_source is ExitSource.TARGET:
            self.final_flattening_leg = FlatteningLeg.TARGET
        elif self.closing_source is ExitSource.FLATTEN and self.flatten_reason in FLATTEN_LEG:
            self.final_flattening_leg = FLATTEN_LEG[self.flatten_reason]
        else:
            self.final_flattening_leg = FlatteningLeg.UNKNOWN
        if self.exit_state_unknown:
            outcome = ExitOutcome.UNKNOWN_EXIT_STATE
        elif {ExitSource.STOP, ExitSource.TARGET} <= sources:
            outcome = ExitOutcome.MIXED_STOP_TARGET_EXIT
        elif self.closing_source is ExitSource.STOP:
            outcome = ExitOutcome.STOP_FILLED
        elif self.closing_source is ExitSource.TARGET:
            outcome = ExitOutcome.TARGET_FILLED
        else:
            outcome = self.flatten_reason or ExitOutcome.UNKNOWN_EXIT_STATE
        self.exit_outcome, self.closed_utc, self.state = outcome, at, ProtectionState.TRADE_CLOSED
        self._log(at, "TRADE_CLOSED", outcome.value)

    # ------------------------------------------------------------------ record
    def exit_record(self) -> dict[str, Any]:
        r = self.requirement
        return {
            "position_id": r.position_id,
            "entry_order_id": r.entry_order_id,
            "candidate_id": r.candidate_id,
            "confirmation_id": r.confirmation_id,
            "contract": r.contract,
            "account_id": r.account_id,
            "side": r.side.value,
            "entry_quantity": r.confirmed_open_quantity + self._synced_extra,
            "actual_average_entry": str(r.actual_average_entry),
            "frozen_stop_price": str(r.frozen_stop_price),
            "frozen_target_price": str(r.frozen_target_price),
            "state": self.state.value,
            "exit_outcome": None if self.exit_outcome is None else self.exit_outcome.value,
            "exit_fills": [
                {
                    "order_id": f.order_id,
                    "timestamp_utc": f.timestamp_utc.isoformat(),
                    "exit_type": f.source.value,
                    "quantity": f.quantity,
                    "price": str(f.price),
                    "realized_gross_pnl_points": str(f.realized_gross_pnl_points),
                    "realized_gross_pnl_usd": str(f.realized_gross_pnl_usd),
                    "remaining_position_after": f.remaining_position_after,
                }
                for f in self.exit_fills
            ],
            "final_flattening_leg": None if self.final_flattening_leg is None else self.final_flattening_leg.value,
            "broker_reported_exposure": None if self.broker_reported_exposure is None else list(self.broker_reported_exposure),
            "operational_anomalies": [[a.timestamp_utc.isoformat(), a.code, a.detail] for a in self._anomalies],
            "flat_confirmed_utc": None if self.flat_confirmed_utc is None else self.flat_confirmed_utc.isoformat(),
            "closed_utc": None if self.closed_utc is None else self.closed_utc.isoformat(),
            "reasons": list(self.reasons),
            "required_actions": list(self.required_actions),
            "unintended_exposure_quantity": self.unintended_exposure_quantity,
            "costs": COST_STATUS,
            "events": [list(e) for e in self.events],
            "specification_version": self.params.specification_version,
            "configuration_hash": self.params.configuration_hash,
        }


# =========================================================================== research replay helpers


class PriceSource(str, Enum):
    AUTHORITATIVE_TRADES = "AUTHORITATIVE_TRADES"  # trades, or bars built only from trades
    QUOTES = "QUOTES"  # never used to trigger or fill
    LOCAL_ESTIMATE = "LOCAL_ESTIMATE"


@dataclass(frozen=True)
class PriceInterval:
    """A trade (high == low) or a bar of any resolution, with its source reference."""

    start_utc: pd.Timestamp
    high: Decimal
    low: Decimal
    source: PriceSource
    source_reference: str


def _stop_hit(side: Side, stop: Decimal, interval: PriceInterval) -> bool:
    return interval.low <= stop if side is Side.LONG else interval.high >= stop


def _target_hit(side: Side, target: Decimal, interval: PriceInterval, params: ProtectionParams) -> bool:
    through = params.target_fill_trade_through_ticks * TICK
    return interval.high >= target + through if side is Side.LONG else interval.low <= target - through


@dataclass(frozen=True)
class ExitDecision:
    source: ExitSource
    trigger_utc: pd.Timestamp
    trigger_reference: str
    fill_price: Decimal | None  # target: its limit price; stop: None until the slippage model is frozen
    fill_price_model: str
    flags: tuple[str, ...]


def _authoritative(intervals: Iterable[PriceInterval]) -> list[PriceInterval]:
    return sorted((i for i in intervals if i.source is PriceSource.AUTHORITATIVE_TRADES), key=lambda i: i.start_utc)


def first_exit(
    side: Side,
    stop: Decimal,
    target: Decimal,
    intervals: Iterable[PriceInterval],
    params: ProtectionParams,
    ambiguity_flags: tuple[str, ...] = (),
) -> ExitDecision | None:
    """Scan chronologically. Stop: first authoritative trade at/through the stop. Target: a one-tick trade-through.

    If one interval reaches both and cannot be ordered, the stop is assumed first.
    """
    for interval in _authoritative(intervals):
        stop_hit, target_hit = _stop_hit(side, stop, interval), _target_hit(side, target, interval, params)
        if stop_hit:
            flags = ambiguity_flags + (("SAME_BAR_STOP_TARGET_AMBIGUITY", SAME_BAR_POLICY) if target_hit else ())
            return ExitDecision(ExitSource.STOP, interval.start_utc, interval.source_reference, None, STOP_FILL_PRICE_MODEL, flags)
        if target_hit:
            return ExitDecision(ExitSource.TARGET, interval.start_utc, interval.source_reference, target, TARGET_FILL_FLAG, ambiguity_flags + (TARGET_FILL_FLAG,))
    return None


def resolve_minute(
    side: Side,
    stop: Decimal,
    target: Decimal,
    minute_bar: PriceInterval,
    params: ProtectionParams,
    finer: Iterable[PriceInterval] = (),
) -> ExitDecision | None:
    """One-minute resolution; finer chronological data (one-second bars or trades) override when available."""
    if not (_stop_hit(side, stop, minute_bar) and _target_hit(side, target, minute_bar, params)):
        return first_exit(side, stop, target, (minute_bar,), params)
    finer = _authoritative(finer)
    if finer:
        decision = first_exit(side, stop, target, finer, params, ("CHRONOLOGY_FROM_HIGHER_RESOLUTION",))
        if decision is not None:
            return decision
    return first_exit(side, stop, target, (minute_bar,), params)


def resolve_entry_minute(
    side: Side,
    entry_fill_utc: pd.Timestamp,
    stop: Decimal,
    target: Decimal,
    entry_minute_bar: PriceInterval,
    params: ProtectionParams,
    finer: Iterable[PriceInterval] = (),
) -> ExitDecision | None:
    """Round 15: exits within the entry minute. Only events at/after the authoritative entry fill count.

    Finer chronology (trades/one-second bars after the fill) is used when available. With one-minute data only,
    a reachable stop is assumed to come first and a target is NEVER awarded from unresolved ordering.
    """
    after = [i for i in _authoritative(finer) if i.start_utc >= entry_fill_utc]
    if after:
        return first_exit(side, stop, target, after, params, ("CHRONOLOGY_FROM_HIGHER_RESOLUTION",))
    if _stop_hit(side, stop, entry_minute_bar):
        return ExitDecision(
            ExitSource.STOP, entry_minute_bar.start_utc, entry_minute_bar.source_reference, None, STOP_FILL_PRICE_MODEL,
            ("SAME_MINUTE_ENTRY_EXIT_AMBIGUITY", SAME_BAR_POLICY),
        )
    return None  # an unresolved target touch in the entry minute is not a fill
