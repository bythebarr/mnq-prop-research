"""Round 14 protective stop/target layer and D-030 confirmations (owner-specified test lists).

Hand-built fixtures, not market data. Base long position (see test_entry_order):
frozen stop 19999.75, frozen target 20026.00; authoritative entry fill at SUB + 100 ms.
Broker reports are simulated inputs; nothing here talks to a broker.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect

import pytest

from test_entry_order import AUTH, EP, MS, SPEC, SUB, D, invalid_paths, new_order, scenario, submitted
from mnq_research import protection
from mnq_research.confirmation import Side
from mnq_research.entry_order import (
    DailyEntryState,
    EntryOutcome as O,
    FillSource,
    StateLossCause,
)
from mnq_research.protection import (
    BracketMode as M,
    BrokerOrderReport,
    Component as K,
    ComponentStatus as CS,
    ExitOutcome as X,
    ExitSource as E,
    OrderAction,
    PendingReason,
    PriceInterval,
    PriceSource,
    ProtectionParams,
    ProtectionRequirement,
    ProtectionState as P,
    ProtectiveBracket,
    choose_bracket_mode,
    first_exit,
    resolve_minute,
)
from mnq_research.validation import check_rule_freeze

PP = ProtectionParams.from_spec(SPEC)
ACCOUNT = "SIM-ACCOUNT-1"
FILL = SUB + 100 * MS
STOP, TARGET = D("19999.75"), D("20026.00")
TYPES = {K.STOP: "STOP_MARKET", K.TARGET: "LIMIT"}
PRICES = {K.STOP: STOP, K.TARGET: TARGET}


def filled(quantity: int = 1, fill_quantity: int = 1, price: str = "20010.50"):
    book, order = submitted(dataclasses.replace(EP, research_quantity_contracts=quantity))
    order.on_fill(FILL, fill_quantity, D(price), AUTH)
    return book, order


def bracket(order, mode: M = M.NATIVE_SERVER_SIDE_OCO_BRACKET, received=None) -> ProtectiveBracket:
    return ProtectiveBracket(ProtectionRequirement.from_entry(order, EP, PP, ACCOUNT, received or FILL), PP, mode)


def report(kind: K, quantity: int = 1, **changes) -> BrokerOrderReport:
    base = dict(order_type=TYPES[kind], price=PRICES[kind], quantity=quantity, action=OrderAction.SELL, contract="MNQM4", account_id=ACCOUNT)
    return BrokerOrderReport(**{**base, **changes})


def active(quantity: int = 1, fill_quantity: int | None = None, mode: M = M.NATIVE_SERVER_SIDE_OCO_BRACKET):
    book, order = filled(quantity, fill_quantity or quantity)
    b = bracket(order, mode)
    b.dispatch(FILL)
    q = order.confirmed_position_quantity
    b.on_order_confirmed(FILL + 50 * MS, K.STOP, report(K.STOP, q))
    b.on_order_confirmed(FILL + 60 * MS, K.TARGET, report(K.TARGET, q))
    b.on_oco_confirmed(FILL + 70 * MS)
    assert b.state is P.PROTECTION_ACTIVE
    return book, order, b


def close_flat(b: ProtectiveBracket, at, cancel: K) -> None:
    b.on_position_report(at, 0, None, "MNQM4", ACCOUNT)
    b.on_cancel_confirmed(at + 10 * MS, cancel)


# =========================================================================== creation, dispatch, prices


def test_p01_any_positive_fill_creates_protection_required():
    _, order = filled(3, 1)
    r = ProtectionRequirement.from_entry(order, EP, PP, ACCOUNT, FILL)
    assert r.status == "PROTECTION_REQUIRED" and r.confirmed_open_quantity == 1
    for name in ("entry_order_id", "position_id", "account_id", "contract", "side", "actual_average_entry", "frozen_stop_price",
                 "frozen_target_price", "zone_version_id", "attempt_id", "acceptance_id", "confirmation_id", "candidate_id",
                 "first_fill_utc", "protection_deadline_utc", "specification_version", "configuration_hash"):
        assert getattr(r, name) not in (None, ""), name
    assert set(SPEC["protective_orders"]["protection_task_fields"]) <= {f.name for f in dataclasses.fields(r)}
    assert order.protection_task.status == "PROTECTION_REQUIRED"
    _, unfilled = submitted()
    unfilled.on_fill(FILL, 1, D("20010.50"), FillSource.LOCAL_ESTIMATE)  # not authoritative
    with pytest.raises(ValueError):
        ProtectionRequirement.from_entry(unfilled, EP, PP, ACCOUNT, FILL)


def test_p02_protection_dispatch_begins_within_250_ms():
    assert SPEC["protective_orders"]["parameters"]["protection_dispatch_deadline_milliseconds"] == 250
    _, order = filled()
    on_time = bracket(order)
    on_time.dispatch(FILL + 250 * MS)
    assert on_time.state is P.PROTECTION_PENDING and not on_time.emergency_flatten_required
    late = bracket(order)
    late.dispatch(FILL + 251 * MS)
    assert late.emergency_flatten_required and "PROTECTION_DISPATCH_DEADLINE_MISSED" in late.reasons and late.halts_trading_date
    never = bracket(order)
    never.advance(FILL + 300 * MS)
    assert never.emergency_flatten_required and "PROTECTION_DISPATCH_DEADLINE_MISSED" in never.reasons
    with pytest.raises(ValueError):
        on_time.dispatch(FILL + 260 * MS)  # never dispatched twice


def test_p03_stop_and_target_prices_stay_frozen():
    _, order = filled(price="20012.00")  # slipped fill: prices still unchanged
    b = bracket(order)
    b.dispatch(FILL)
    assert (b.stop.price, b.target.price) == (STOP, TARGET)
    assert (b.stop.order_type, b.target.order_type) == ("STOP_MARKET", "LIMIT")
    b.on_order_confirmed(FILL + 50 * MS, K.STOP, report(K.STOP, price=STOP - D("1")))  # broker holds a different price
    assert b.emergency_flatten_required and "PROTECTIVE_STOP_FAILURE:WRONG_PRICE" in b.reasons
    assert b.stop.price == STOP  # never "adopted"
    names = {n.lower() for n in dir(ProtectiveBracket)}
    assert not {n for n in names if any(w in n for w in ("trail", "breakeven", "widen", "tighten", "move", "recalc"))}
    assert SPEC["position_management"]["breakeven_rule"] == SPEC["position_management"]["trailing_stop_rule"] == "NONE"


# =========================================================================== bracket workflow


def test_p04_atomic_server_side_oco_is_preferred():
    assert PP.preferred_bracket_mode is M.NATIVE_SERVER_SIDE_OCO_BRACKET
    assert choose_bracket_mode(True, True) is M.NATIVE_SERVER_SIDE_OCO_BRACKET
    assert choose_bracket_mode(False, True) is M.SEPARATE_SERVER_SIDE_ORDERS
    assert choose_bracket_mode(False, False) is M.CLIENT_SIDE_ONLY
    _, order = filled()
    b = bracket(order)
    b.dispatch(FILL)
    assert b.stop.submitted_utc == b.target.submitted_utc == b.oco.submitted_utc == FILL  # one atomic unit
    client = bracket(order, M.CLIENT_SIDE_ONLY)
    client.dispatch(FILL)
    assert client.emergency_flatten_required and "PROTECTION_CAPABILITY_INSUFFICIENT:CLIENT_SIDE_ONLY" in client.reasons


def test_p05_non_atomic_workflow_confirms_stop_before_target():
    _, order = filled()
    b = bracket(order, M.SEPARATE_SERVER_SIDE_ORDERS)
    b.dispatch(FILL)
    assert (b.stop.status, b.target.status, b.oco.status) == (CS.PENDING, CS.NOT_SUBMITTED, CS.NOT_SUBMITTED)
    b.on_order_confirmed(FILL + 40 * MS, K.TARGET, report(K.TARGET))  # a stray target report cannot jump the queue
    assert b.target.status is CS.NOT_SUBMITTED
    b.on_order_confirmed(FILL + 50 * MS, K.STOP, report(K.STOP))
    assert b.target.status is CS.PENDING and b.target.submitted_utc == FILL + 50 * MS and b.oco.status is CS.NOT_SUBMITTED
    b.on_order_confirmed(FILL + 90 * MS, K.TARGET, report(K.TARGET))
    assert b.oco.status is CS.PENDING and b.state is P.PROTECTION_PENDING
    b.on_oco_confirmed(FILL + 120 * MS)
    assert b.state is P.PROTECTION_ACTIVE


def test_p06_a_target_without_a_stop_is_unsafe():
    _, order = filled()
    b = bracket(order)
    b.dispatch(FILL)
    b.on_order_confirmed(FILL + 30 * MS, K.TARGET, report(K.TARGET))
    assert b.state is P.PROTECTION_PENDING  # target confirmed, stop still awaited: not protection
    b.on_rejected(FILL + 40 * MS, K.STOP, "MARGIN")
    assert b.emergency_flatten_required and b.state is P.EMERGENCY_FLATTEN_REQUIRED
    _, order, b = active()
    b.on_component_failure(FILL + 500 * MS, K.STOP, "CANCELLED_WITHOUT_REPLACEMENT")  # target left alone
    assert b.emergency_flatten_required and "PROTECTIVE_STOP_FAILURE:CANCELLED_WITHOUT_REPLACEMENT" in b.reasons
    # Directly: a confirmed target meeting a failed stop is caught by the evaluation itself.
    _, order = filled()
    b = bracket(order)
    b.dispatch(FILL)
    b.stop.status = CS.FAILED
    b.on_order_confirmed(FILL + 30 * MS, K.TARGET, report(K.TARGET))
    assert "TARGET_WITHOUT_STOP" in b.reasons


def test_p07_each_protective_acknowledgement_has_its_own_two_second_timeout():
    assert PP.acknowledgement_timeout == 2000 * MS
    _, order = filled()
    b = bracket(order, M.SEPARATE_SERVER_SIDE_ORDERS)
    b.dispatch(FILL)
    b.on_order_confirmed(FILL + 1500 * MS, K.STOP, report(K.STOP))  # target submitted at +1.5 s
    b.advance(FILL + 2000 * MS)
    assert not b.emergency_flatten_required  # the target's clock started at +1.5 s, not at dispatch
    b.advance(FILL + 3500 * MS)
    assert b.emergency_flatten_required and "TARGET_PROTECTION_FAILURE:ACKNOWLEDGEMENT_TIMEOUT" in b.reasons
    _, order = filled()
    s = bracket(order)
    s.dispatch(FILL)
    s.advance(FILL + 1999 * MS)
    assert not s.emergency_flatten_required
    s.advance(FILL + 2000 * MS)
    assert "PROTECTIVE_STOP_FAILURE:ACKNOWLEDGEMENT_TIMEOUT" in s.reasons
    _, order = filled()
    o = bracket(order)
    o.dispatch(FILL)
    o.on_order_confirmed(FILL + 50 * MS, K.STOP, report(K.STOP))
    o.on_order_confirmed(FILL + 60 * MS, K.TARGET, report(K.TARGET))
    o.advance(FILL + 2000 * MS)
    assert "OCO_LINK_FAILURE:ACKNOWLEDGEMENT_TIMEOUT" in o.reasons


def test_p08_protection_is_not_active_until_authoritative_confirmation():
    _, order = filled()
    b = bracket(order)
    assert b.state is P.PROTECTION_REQUIRED
    b.dispatch(FILL)
    assert b.state is P.PROTECTION_PENDING and not b.protection_active  # submitted is not active
    with pytest.raises(TypeError):
        b.on_order_confirmed(FILL + 10 * MS, K.STOP, {"price": STOP, "quantity": 1})  # a local object is not confirmation
    b.on_order_confirmed(FILL + 50 * MS, K.STOP, report(K.STOP))
    b.on_order_confirmed(FILL + 60 * MS, K.TARGET, report(K.TARGET))
    assert b.state is P.PROTECTION_PENDING  # OCO link not yet confirmed
    b.on_oco_confirmed(FILL + 70 * MS)
    assert b.state is P.PROTECTION_ACTIVE and b.active_since_utc == FILL + 70 * MS


# =========================================================================== quantities


def test_p09_partial_entry_fills_are_protected_immediately():
    _, order = filled(3, 1)
    assert order.phase.value in ("SUBMITTED", "ACKNOWLEDGED")  # the entry order is still working
    b = bracket(order)
    b.dispatch(FILL)
    assert b.open_quantity == b.stop.quantity == b.target.quantity == 1


def test_p10_added_fills_increase_protection_only_to_the_confirmed_quantity():
    _, order, b = active(3, 1)
    order.on_fill(FILL + 400 * MS, 1, D("20010.75"), AUTH)
    b.sync_entry_quantity(FILL + 400 * MS, order)
    assert b.open_quantity == 2 and b.state is P.PROTECTION_PENDING
    assert (b.stop.quantity, b.target.quantity) == (2, 2) and b.stop.pending_reason is PendingReason.ENTRY_QUANTITY_SYNC
    assert (b.stop.price, b.target.price) == (STOP, TARGET)
    b.on_order_confirmed(FILL + 450 * MS, K.STOP, report(K.STOP, 2))
    b.on_order_confirmed(FILL + 460 * MS, K.TARGET, report(K.TARGET, 2))
    assert b.state is P.PROTECTION_ACTIVE
    _, order, b = active(3, 1)
    order.on_fill(FILL + 400 * MS, 1, D("20010.75"), AUTH)
    b.sync_entry_quantity(FILL + 400 * MS, order)
    b.on_order_confirmed(FILL + 450 * MS, K.STOP, report(K.STOP, 1))  # broker did not take the increase
    assert b.emergency_flatten_required and "PROTECTION_QUANTITY_UNKNOWN" in b.reasons
    _, order, b = active(3, 1)
    order.on_fill(FILL + 400 * MS, 1, D("20010.75"), AUTH)
    b.sync_entry_quantity(FILL + 400 * MS, order)
    b.advance(FILL + 2400 * MS)
    assert "PROTECTION_QUANTITY_UNKNOWN" in b.reasons
    _, _, b = active(3, 1)
    b.report_quantity_unsynchronisable(FILL + 500 * MS)
    assert b.emergency_flatten_required


def test_p11_protective_quantity_never_uses_the_intended_unfilled_quantity():
    _, order = filled(3, 1)
    assert order.quantity == 3
    b = bracket(order)
    b.dispatch(FILL)
    b.on_order_confirmed(FILL + 50 * MS, K.STOP, report(K.STOP, 3))  # sized to the intended order: wrong
    assert "PROTECTIVE_STOP_FAILURE:WRONG_QUANTITY" in b.reasons


# =========================================================================== OCO behaviour


def test_p12_a_stop_fill_cancels_the_target():
    _, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.STOP, 1, D("19999.50"), AUTH)
    assert b.target.status is CS.PENDING and b.target.pending_reason is PendingReason.CANCEL
    assert b.state is P.AWAITING_FLAT_CONFIRMATION and b.exit_outcome is None
    b.on_position_report(FILL + 5100 * MS, 0, None, "MNQM4", ACCOUNT)
    assert b.state is not P.TRADE_CLOSED  # the target is not yet confirmed cancelled
    b.on_cancel_confirmed(FILL + 5200 * MS, K.TARGET)
    assert b.state is P.TRADE_CLOSED and b.exit_outcome is X.STOP_FILLED


def test_p13_a_target_fill_cancels_the_stop():
    _, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.TARGET, 1, TARGET, AUTH)
    assert b.stop.pending_reason is PendingReason.CANCEL
    close_flat(b, FILL + 5100 * MS, K.STOP)
    assert b.exit_outcome is X.TARGET_FILLED
    record = b.exit_record()
    (exit_fill,) = record["exit_fills"]
    assert exit_fill["timestamp_utc"] == (FILL + 5000 * MS).isoformat() and exit_fill["exit_type"] == "TARGET"
    assert (exit_fill["quantity"], exit_fill["price"], exit_fill["remaining_position_after"]) == (1, "20026.00", 0)
    assert exit_fill["realized_gross_pnl_points"] == "31/2" and exit_fill["realized_gross_pnl_usd"] == "31"  # 15.50 pts x $2
    assert record["final_flattening_leg"] == "TARGET"
    assert record["costs"] == "UNRESOLVED_UNTIL_COST_MODEL_FROZEN" and record["closed_utc"]


def test_p14_partial_exits_reduce_the_sibling_quantity():
    _, _, b = active(3)
    b.on_exit_fill(FILL + 5000 * MS, E.TARGET, 1, TARGET, AUTH)
    assert b.open_quantity == 2 and b.stop.quantity == 2 and b.stop.pending_reason is PendingReason.EXIT_REDUCTION
    assert b.oco.status is CS.CONFIRMED  # the OCO stays in force for the remainder
    b.on_order_confirmed(FILL + 5050 * MS, K.STOP, report(K.STOP, 2))
    assert b.state is P.PROTECTION_ACTIVE and b.target.confirmed_quantity == 2
    _, _, b = active(3)
    b.on_exit_fill(FILL + 5000 * MS, E.STOP, 1, D("19999.50"), AUTH)
    b.advance(FILL + 7000 * MS)  # the target reduction is never confirmed
    assert b.emergency_flatten_required and "OCO_RECONCILIATION_FAILURE" in b.reasons


def test_p15_a_sibling_cannot_intentionally_reverse_the_position():
    _, _, b = active(3)
    b.on_exit_fill(FILL + 5000 * MS, E.TARGET, 2, TARGET, AUTH)
    assert b.stop.quantity == b.open_quantity == 1  # never more than the open quantity
    _, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.TARGET, 2, TARGET, AUTH)  # more than the open quantity
    assert b.exit_state_unknown and b.unintended_exposure_quantity == 1 and b.open_quantity == 0
    assert b.stop.pending_reason is PendingReason.CANCEL  # remaining orders are cancelled
    assert {"RECONCILE_BROKER_POSITION", "FLATTEN_ANY_UNINTENDED_EXPOSURE"} <= set(b.required_actions)


def test_p16_late_sibling_fills_create_contradiction_handling():
    book, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.STOP, 1, D("19999.50"), AUTH)
    close_flat(b, FILL + 5100 * MS, K.TARGET)
    assert b.exit_outcome is X.STOP_FILLED
    b.on_exit_fill(FILL + 6000 * MS, E.TARGET, 1, TARGET, AUTH)  # after confirmed flat
    assert b.exit_state_unknown and b.unintended_exposure_quantity == 1 and b.halts_trading_date
    assert b.exit_outcome is X.UNKNOWN_EXIT_STATE
    assert "TRADE_CLOSED" in [e[1] for e in b.events]  # the earlier close stays in the history


# =========================================================================== research replay


def trade(ms: int, price: str, source: PriceSource = PriceSource.AUTHORITATIVE_TRADES) -> PriceInterval:
    return PriceInterval(FILL + ms * MS, D(price), D(price), source, f"trade@{ms}")


def test_p17_stops_trigger_on_the_first_qualifying_authoritative_trade():
    trades = [trade(10, "19999.00", PriceSource.QUOTES), trade(20, "20000.00"), trade(30, "19999.75"), trade(40, "19999.00")]
    d = first_exit(Side.LONG, STOP, TARGET, trades, PP)
    assert d.source is E.STOP and d.trigger_utc == FILL + 30 * MS and d.trigger_reference == "trade@30"
    assert d.fill_price is None and d.fill_price_model == "UNRESOLVED_UNTIL_STOP_SLIPPAGE_MODEL_FROZEN"  # not the trigger price
    short = first_exit(Side.SHORT, D("20004.25"), D("19976.00"), [trade(10, "20004.00"), trade(20, "20004.25")], PP)
    assert short.source is E.STOP and short.trigger_utc == FILL + 20 * MS


def test_p18_a_target_touch_alone_does_not_fill():
    assert first_exit(Side.LONG, STOP, TARGET, [trade(10, "20026.00")], PP) is None
    assert first_exit(Side.SHORT, D("20004.25"), D("19976.00"), [trade(10, "19976.00")], PP) is None


def test_p19_a_one_tick_trade_through_fills_the_target():
    d = first_exit(Side.LONG, STOP, TARGET, [trade(10, "20026.00"), trade(20, "20026.25")], PP)
    assert d.source is E.TARGET and d.fill_price == TARGET and d.trigger_utc == FILL + 20 * MS
    assert "CONSERVATIVE_ONE_TICK_TRADE_THROUGH_TARGET_FILL" in d.flags
    short = first_exit(Side.SHORT, D("20004.25"), D("19976.00"), [trade(10, "19975.75")], PP)
    assert short.source is E.TARGET and short.fill_price == D("19976.00")


def minute(high: str, low: str) -> PriceInterval:
    return PriceInterval(FILL, D(high), D(low), PriceSource.AUTHORITATIVE_TRADES, "1min@bar")


def test_p20_unresolved_same_bar_ambiguity_assumes_the_stop_first():
    d = resolve_minute(Side.LONG, STOP, TARGET, minute("20027.00", "19999.00"), PP)
    assert d.source is E.STOP and {"SAME_BAR_STOP_TARGET_AMBIGUITY", "CONSERVATIVE_STOP_FIRST"} <= set(d.flags)
    only_target = resolve_minute(Side.LONG, STOP, TARGET, minute("20027.00", "20005.00"), PP)
    assert only_target.source is E.TARGET and "SAME_BAR_STOP_TARGET_AMBIGUITY" not in only_target.flags


def test_p21_higher_resolution_chronology_overrides_one_minute_ambiguity():
    finer = [trade(10, "20026.25"), trade(20, "19999.00")]  # target traded through first
    d = resolve_minute(Side.LONG, STOP, TARGET, minute("20027.00", "19999.00"), PP, finer)
    assert d.source is E.TARGET and "CHRONOLOGY_FROM_HIGHER_RESOLUTION" in d.flags and d.trigger_reference == "trade@10"
    one_second = [PriceInterval(FILL, D("20027.00"), D("19999.00"), PriceSource.AUTHORITATIVE_TRADES, "1s@bar")]
    d = resolve_minute(Side.LONG, STOP, TARGET, minute("20027.00", "19999.00"), PP, one_second)
    assert d.source is E.STOP and {"CHRONOLOGY_FROM_HIGHER_RESOLUTION", "CONSERVATIVE_STOP_FIRST"} <= set(d.flags)
    assert d.trigger_reference == "1s@bar"


# =========================================================================== failures


def test_p22_stop_failure_invokes_emergency_flattening():
    failures = [
        lambda b: b.on_rejected(FILL + 30 * MS, K.STOP, "REJECTED"),
        lambda b: b.advance(FILL + 2000 * MS),
        lambda b: b.on_component_failure(FILL + 30 * MS, K.STOP, "STATE_UNKNOWN"),
        lambda b: b.on_order_confirmed(FILL + 30 * MS, K.STOP, report(K.STOP, price=D("19990.00"))),
        lambda b: b.on_order_confirmed(FILL + 30 * MS, K.STOP, report(K.STOP, 2)),
        lambda b: b.on_order_confirmed(FILL + 30 * MS, K.STOP, report(K.STOP, contract="MNQU4")),
        lambda b: b.on_order_confirmed(FILL + 30 * MS, K.STOP, report(K.STOP, account_id="OTHER")),
        lambda b: b.on_order_confirmed(FILL + 30 * MS, K.STOP, report(K.STOP, action=OrderAction.BUY)),
        lambda b: b.on_component_failure(FILL + 30 * MS, K.STOP, "CANCELLED_WITHOUT_REPLACEMENT"),
        lambda b: b.on_component_failure(FILL + 30 * MS, K.STOP, "INACTIVE_WHILE_POSITION_OPEN"),
        lambda b: b.on_component_failure(FILL + 30 * MS, K.STOP, "CLIENT_SERVER_DISAGREEMENT"),
    ]
    for fail in failures:
        _, order = filled()
        b = bracket(order)
        b.dispatch(FILL)
        fail(b)
        assert b.emergency_flatten_required and b.halts_trading_date and b.state is P.EMERGENCY_FLATTEN_REQUIRED
        assert any(r.startswith("PROTECTIVE_STOP_FAILURE:") for r in b.reasons)
        assert {"HALT_NEW_ENTRIES_FOR_TRADING_DATE", "PRESERVE_ORIGINAL_STRUCTURAL_STOP_FOR_AUDIT", "BEGIN_EMERGENCY_FLATTEN_OF_CONFIRMED_QUANTITY",
                "RECONCILE_POSITION_AND_ORDERS", "CRITICAL_ALERT", "DO_NOT_WIDEN_OR_RECREATE_STRATEGY_RISK"} <= set(b.required_actions)
        assert b.stop.price == STOP and b.flatten_reason is X.PROTECTION_FAILURE_FLATTEN


def test_p23_target_failure_with_an_active_stop_still_flattens():
    for fail in (
        lambda b: b.on_rejected(FILL + 30 * MS, K.TARGET, "PRICE"),
        lambda b: b.on_component_failure(FILL + 30 * MS, K.TARGET, "STATE_UNKNOWN"),
        lambda b: b.on_order_confirmed(FILL + 30 * MS, K.TARGET, report(K.TARGET, price=D("20030.00"))),
        lambda b: b.on_order_confirmed(FILL + 30 * MS, K.TARGET, report(K.TARGET, 2)),
    ):
        _, order = filled()
        b = bracket(order)
        b.dispatch(FILL)
        b.on_order_confirmed(FILL + 20 * MS, K.STOP, report(K.STOP))
        fail(b)
        assert b.emergency_flatten_required and "TARGET_PROTECTION_FAILURE" in b.reasons
        assert {"CANCEL_TARGET_AND_RECONCILE", "PRESERVE_STOP_UNTIL_FLAT_CONFIRMED"} <= set(b.required_actions)
        assert b.stop.status is CS.CONFIRMED  # the stop is preserved until flat


def test_p24_oco_link_failure_invokes_emergency_flattening():
    for fail in (lambda b: b.on_rejected(FILL + 80 * MS, K.OCO_LINK, "UNSUPPORTED"),
                 lambda b: b.on_component_failure(FILL + 80 * MS, K.OCO_LINK, "STATE_UNKNOWN")):
        _, order = filled()
        b = bracket(order)
        b.dispatch(FILL)
        b.on_order_confirmed(FILL + 50 * MS, K.STOP, report(K.STOP))
        b.on_order_confirmed(FILL + 60 * MS, K.TARGET, report(K.TARGET))
        fail(b)
        assert b.emergency_flatten_required and any(r.startswith("OCO_LINK_FAILURE") for r in b.reasons)
        assert not b.protection_active


def test_p25_fill_at_invalidation_bypasses_the_normal_target():
    _, order = filled(price="19999.75")
    b = bracket(order)
    assert b.requirement.entry_invalidation_latched
    b.dispatch(FILL)
    assert b.emergency_flatten_required and b.flatten_reason is X.ENTRY_INVALIDATION_FLATTEN
    assert b.target.status is CS.NOT_SUBMITTED and b.stop.status is CS.NOT_SUBMITTED  # no pretend bracket, no wrong-side stop
    b.on_exit_fill(FILL + 300 * MS, E.FLATTEN, 1, D("19999.25"), AUTH)
    b.on_position_report(FILL + 400 * MS, 0, None, "MNQM4", ACCOUNT)
    assert b.state is P.TRADE_CLOSED and b.exit_outcome is X.ENTRY_INVALIDATION_FLATTEN
    _, order, b = active(3, 1)  # the latch arriving through a later entry fill
    order.on_fill(FILL + 400 * MS, 1, D("19990.00"), AUTH)
    b.sync_entry_quantity(FILL + 400 * MS, order)
    assert b.emergency_flatten_required and b.flatten_reason is X.ENTRY_INVALIDATION_FLATTEN


# =========================================================================== flat, allowance, deployment


def test_p26_flat_status_requires_an_authoritative_zero_position():
    _, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.STOP, 1, D("19999.50"), AUTH)
    assert b.flat_confirmed_utc is None and b.state is not P.TRADE_CLOSED
    b.on_cancel_confirmed(FILL + 5050 * MS, K.TARGET)
    assert b.state is not P.TRADE_CLOSED  # still no authoritative position report
    b.on_position_report(FILL + 5100 * MS, 0, None, "MNQM4", ACCOUNT)
    assert b.state is P.TRADE_CLOSED
    _, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.STOP, 1, D("19999.50"), AUTH)
    b.on_position_report(FILL + 5100 * MS, 1, Side.LONG, "MNQM4", ACCOUNT)  # broker still shows a position
    assert b.exit_state_unknown and b.flat_confirmed_utc is None
    _, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.STOP, 1, D("19999.50"), FillSource.LOCAL_ESTIMATE)
    assert b.open_quantity == 1 and not b.exit_fills  # a local estimate never exits a position


def test_p27_the_daily_entry_allowance_stays_consumed_after_the_exit():
    book, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.TARGET, 1, TARGET, AUTH)
    close_flat(b, FILL + 5100 * MS, K.STOP)
    book.record_position_flat(b.flat_confirmed_utc)
    assert book.daily_state is DailyEntryState.FILLED_ENTRY_LIMIT_REACHED
    assert book.controls()["filled_entry_allowance_available"].value == "BLOCKED"


def test_p28_no_live_or_paper_execution_is_enabled():
    assert PP.live_or_paper_order_submission == "prohibited"
    spec = copy.deepcopy(SPEC)
    spec["entry_order_lifecycle"]["protective_order_dependency"]["live_or_paper_order_submission"] = "paper"
    with pytest.raises(ValueError):
        ProtectionParams.from_spec(spec)
    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(protection))):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "datetime", "dataclasses", "decimal", "enum", "fractions", "typing", "pandas", "mnq_research"}


def test_p29_deployment_stays_blocked_pending_capability_verification():
    dep = SPEC["protective_orders"]["deployment"]
    assert dep["capability_verification_status"] == "REQUIRED_BEFORE_EXECUTABLE"
    assert dep["server_side_protective_orders_required"] is True and dep["server_side_oco_required"] is True
    assert "protective_orders.deployment.capability_verification_status" in check_rule_freeze(SPEC).unresolved_paths()
    spec = copy.deepcopy(SPEC)
    spec["protective_orders"]["deployment"].update(server_side_protective_orders_required=False, server_side_oco_required=False)
    assert {"protective_orders.deployment.server_side_protective_orders_required", "protective_orders.deployment.server_side_oco_required"} <= invalid_paths(spec)


def test_session_flatten_cancels_the_target_and_keeps_the_stop_until_flat():
    _, _, b = active()
    b.begin_flatten(FILL + 60000 * MS, X.SESSION_EMERGENCY_FLATTEN)
    assert b.target.pending_reason is PendingReason.CANCEL and b.stop.status is CS.CONFIRMED
    b.on_cancel_confirmed(FILL + 60010 * MS, K.TARGET)
    b.on_exit_fill(FILL + 60100 * MS, E.FLATTEN, 1, D("20015.00"), AUTH)
    assert b.stop.pending_reason is PendingReason.CANCEL  # stop kept until the flatten fill, then cancelled
    close_flat(b, FILL + 60200 * MS, K.STOP)
    assert b.exit_outcome is X.SESSION_EMERGENCY_FLATTEN
    with pytest.raises(ValueError):
        b.begin_flatten(FILL, X.STOP_FILLED)


def test_protection_parameters_come_from_the_specification():
    spec = copy.deepcopy(SPEC)
    spec["protective_orders"]["parameters"].update(protection_dispatch_deadline_milliseconds=100, target_fill_trade_through_ticks=2)
    pp = ProtectionParams.from_spec(spec)
    assert pp.dispatch_deadline == 100 * MS
    assert first_exit(Side.LONG, STOP, TARGET, [trade(10, "20026.25")], pp) is None  # now needs two ticks
    literals = {
        n.value for n in ast.walk(ast.parse(inspect.getsource(protection)))
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
    }
    assert literals <= {0, 1}
    spec["protective_orders"]["parameters"].update(protective_stop_order_type="STOP_LIMIT", protection_dispatch_deadline_milliseconds=0)
    assert {"protective_orders.parameters.protective_stop_order_type", "protective_orders.parameters.protection_dispatch_deadline_milliseconds"} <= invalid_paths(spec)
    with pytest.raises(ValueError):
        ProtectionParams.from_spec(spec)


# =========================================================================== D-030 confirmations


def test_d030_01_only_a_positive_fill_consumes_the_one_trade_allowance():
    assert SPEC["daily_limits"]["max_trades_per_day"] == 1 and SPEC["reentry"]["allowed"] is False
    for kind in ("not_submitted", "rejected", "not_filled"):  # includes zero-fill rejection and zero-fill cancel
        book, order = scenario(kind)
        assert not order.consumed_filled_entry and not book.filled_entry_allowance_consumed, kind
    book, _ = scenario("full")
    assert book.filled_entry_allowance_consumed


def test_d030_02_an_invalid_structural_stop_is_a_known_pre_submission_halt():
    from test_entry_order import booked
    from mnq_research.entry_order import EntryOrderBook
    from test_confirmation import TRADE

    _, selected = booked()
    forged = dataclasses.replace(selected, planned_stop_price=D("20000.00"), structural_invalidation_price=D("20000.00"), candidate_id="FORGED")
    book = EntryOrderBook(TRADE, "MNQM4", EP)
    book._awaiting_order[forged.candidate_id] = forged
    order = book.create_order(forged, forged.decision_timestamp)
    assert order.outcome is O.NOT_SUBMITTED_INELIGIBLE and order.outcome is not O.ENTRY_ORDER_STATE_UNKNOWN
    assert order.reasons[:2] == ["INVALID_STRUCTURAL_STOP", "PRE_SUBMISSION_SAFETY_HALT"]
    assert order.halts_trading_date and book.daily_state is DailyEntryState.HALTED and not order.anomalies
    # Discovered after a fill: the position is unprotected -> emergency flatten, records preserved.
    _, filled_order = filled()
    wrong = dataclasses.replace(filled_order.candidate, origin_lower_boundary=D("20001.00"))
    filled_order.candidate = wrong
    b = bracket(filled_order)
    b.dispatch(FILL)
    assert b.emergency_flatten_required and "INVALID_STRUCTURAL_STOP" in b.reasons
    assert "INVALID_STOP:NOT_FROM_THE_ORIGINATING_ZONE" in b.reasons and filled_order.fills


def test_d030_03_state_loss_after_a_full_fill_preserves_everything():
    book, order = scenario("full")
    events_before, fills_before = list(order.events), list(order.fills)
    order.on_state_lost(SUB + 1000 * MS, StateLossCause.CONNECTION_LOSS)
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN
    assert order.fills == fills_before and order.filled_quantity == 1 and order.events[: len(events_before)] == events_before
    assert ("FINAL_ORDER_STATE" in [e[1] for e in order.events]) and "previous outcome ENTRY_FULLY_FILLED" in order.anomalies[-1].detail
    assert book.filled_entry_allowance_consumed and order.protection_task.quantity == 1
    assert order.confirmed_position_quantity == 1


def test_d030_04_the_emergency_flatten_latch_never_resets():
    _, order = submitted(dataclasses.replace(EP, research_quantity_contracts=3))
    order.on_fill(FILL, 1, D("19999.50"), AUTH)  # beyond the stop
    assert order.emergency_flatten_required
    order.on_fill(FILL + 100 * MS, 2, D("20012.00"), AUTH)  # later fills improve the average back above the stop
    assert order.average_fill_price > STOP
    assert order.emergency_flatten_required  # latched
    assert order.protection_task.quantity == 3 and order.protection_task.status == "EMERGENCY_FLATTEN_REQUIRED"
    b = bracket(order, received=FILL + 100 * MS)
    b.dispatch(FILL + 100 * MS)
    assert b.emergency_flatten_required and b.open_quantity == 3  # every fill is flattened


def test_resyncing_without_new_fills_changes_nothing_and_mismatched_quantities_are_never_active():
    _, order, b = active(3, 1)
    b.sync_entry_quantity(FILL + 400 * MS, order)  # no additional authoritative fill
    assert b.state is P.PROTECTION_ACTIVE and b.stop.pending_reason is None and b.open_quantity == 1
    _, order = filled()
    b = bracket(order)
    b.dispatch(FILL)
    b.on_order_confirmed(FILL + 50 * MS, K.STOP, report(K.STOP))
    b.on_order_confirmed(FILL + 60 * MS, K.TARGET, report(K.TARGET))
    b.target.confirmed_quantity = 2  # defensive: any disagreement blocks PROTECTION_ACTIVE
    b.on_oco_confirmed(FILL + 70 * MS)
    assert b.state is P.PROTECTION_PENDING


# =========================================================================== D-031 confirmations


def test_d031_01_mixed_exits_are_named_mixed_and_every_leg_is_preserved():
    _, _, b = active(3)
    b.on_exit_fill(FILL + 5000 * MS, E.TARGET, 1, TARGET, AUTH)
    b.on_order_confirmed(FILL + 5050 * MS, K.STOP, report(K.STOP, 2))
    b.on_exit_fill(FILL + 9000 * MS, E.STOP, 2, D("19999.50"), AUTH)  # the stop makes it flat
    close_flat(b, FILL + 9100 * MS, K.TARGET)
    assert b.exit_outcome is X.MIXED_STOP_TARGET_EXIT  # not "STOP_FILLED" just because the stop came last
    assert b.final_flattening_leg.value == "STOP"
    legs = b.exit_record()["exit_fills"]
    assert [(l["exit_type"], l["quantity"], l["price"], l["remaining_position_after"]) for l in legs] == [
        ("TARGET", 1, "20026.00", 2), ("STOP", 2, "19999.50", 0)
    ]
    assert legs[0]["order_id"].endswith(":TARGET") and legs[1]["order_id"].endswith(":STOP")
    assert (legs[0]["realized_gross_pnl_points"], legs[1]["realized_gross_pnl_points"]) == ("31/2", "-22")  # entry 20010.50
    assert legs[1]["realized_gross_pnl_usd"] == "-44"
    _, _, e = active()
    e.on_component_failure(FILL + 500 * MS, K.TARGET, "STATE_UNKNOWN")
    e.on_exit_fill(FILL + 700 * MS, E.FLATTEN, 1, D("20008.00"), AUTH)
    e.on_position_report(FILL + 800 * MS, 0, None, "MNQM4", ACCOUNT)
    e.on_cancel_confirmed(FILL + 900 * MS, K.STOP)
    assert e.exit_outcome is X.PROTECTION_FAILURE_FLATTEN and e.final_flattening_leg.value == "PROTECTION_FAILURE"


def test_d031_02_a_late_fill_after_close_is_unknown_and_logged_separately():
    _, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.STOP, 1, D("19999.50"), AUTH)
    close_flat(b, FILL + 5100 * MS, K.TARGET)
    close_event = next(e for e in b.events if e[1] == "TRADE_CLOSED")
    b.on_exit_fill(FILL + 6000 * MS, E.TARGET, 1, TARGET, AUTH)
    assert b.exit_outcome is X.UNKNOWN_EXIT_STATE and close_event in b.events  # history unchanged
    assert [a.code for a in b.anomalies] == ["LATE_SIBLING_FILL_AFTER_FLAT"]
    assert {"RECONCILE_BROKER_POSITION", "DETECT_UNINTENDED_REVERSE_POSITION", "FLATTEN_ANY_UNINTENDED_EXPOSURE",
            "CANCEL_REMAINING_ORDERS", "HALT_NEW_ENTRIES_FOR_TRADING_DATE"} <= set(b.required_actions)
    assert b.unintended_exposure_quantity == 1 and b.halts_trading_date


def test_d031_03_the_oco_link_clock_starts_at_the_right_moment():
    _, order = filled()
    atomic = bracket(order)
    atomic.dispatch(FILL)
    assert atomic.oco.pending_since_utc == FILL  # atomic: at dispatch
    _, order = filled()
    sep = bracket(order, M.SEPARATE_SERVER_SIDE_ORDERS)
    sep.dispatch(FILL)
    sep.on_order_confirmed(FILL + 500 * MS, K.STOP, report(K.STOP))
    sep.on_order_confirmed(FILL + 1500 * MS, K.TARGET, report(K.TARGET))
    assert sep.oco.pending_since_utc == FILL + 1500 * MS  # stop-first: once the target is confirmed
    sep.advance(FILL + 3499 * MS)
    assert not sep.emergency_flatten_required
    sep.advance(FILL + 3500 * MS)
    assert "OCO_LINK_FAILURE:ACKNOWLEDGEMENT_TIMEOUT" in sep.reasons


def test_d031_04_unknown_cancellation_queries_first_and_never_duplicates_a_flatten_when_flat():
    # Flat already: no extra flatten order; keep reconciling; a later target fill is flattened as unintended.
    _, _, b = active()
    b.begin_flatten(FILL + 60000 * MS, X.SESSION_EMERGENCY_FLATTEN)
    b.on_exit_fill(FILL + 60100 * MS, E.FLATTEN, 1, D("20015.00"), AUTH)
    b.on_position_report(FILL + 60200 * MS, 0, None, "MNQM4", ACCOUNT)
    b.on_cancel_confirmed(FILL + 60300 * MS, K.STOP)
    b.advance(FILL + 62000 * MS)  # the target cancel (requested at 60000) is still unconfirmed
    assert b.cancellation_unknown and "PROTECTIVE_ORDER_CANCELLATION_UNKNOWN" in b.reasons
    assert not b.emergency_flatten_required  # no duplicate emergency market order while confirmed flat
    assert b.halts_trading_date and "QUERY_AUTHORITATIVE_POSITION_AND_ORDERS" in b.required_actions
    assert b.target.live and b.state is not P.TRADE_CLOSED  # still possibly live: not closed yet
    b.on_exit_fill(FILL + 63000 * MS, E.TARGET, 1, TARGET, AUTH)  # the target was live after all
    assert b.exit_state_unknown and b.unintended_exposure_quantity == 1
    # Position still open while the cancellation is unknown: continue/invoke emergency flattening, keep the stop.
    _, _, o = active()
    o.begin_flatten(FILL + 60000 * MS, X.SCHEDULED_NEWS_FLATTEN)
    o.advance(FILL + 62000 * MS)
    assert o.cancellation_unknown and not o.emergency_flatten_required
    o.on_position_report(FILL + 62100 * MS, 1, Side.LONG, "MNQM4", ACCOUNT)
    assert o.emergency_flatten_required and o.stop.status is CS.CONFIRMED


def test_d031_05_a_position_mismatch_treats_the_broker_position_as_actual_exposure():
    for quantity, side, contract, account in ((2, Side.LONG, "MNQM4", ACCOUNT), (1, Side.SHORT, "MNQM4", ACCOUNT),
                                              (1, Side.LONG, "MNQU4", ACCOUNT), (1, Side.LONG, "MNQM4", "OTHER")):
        book, _, b = active()
        b.on_position_report(FILL + 1000 * MS, quantity, side, contract, account)
        assert b.exit_state_unknown and {"POSITION_MISMATCH", "POSITION_RECONCILIATION_REQUIRED"} <= set(b.reasons)
        assert b.broker_reported_exposure == (quantity, side.value, contract, account)
        assert b.emergency_flatten_required and b.halts_trading_date
        assert [a.code for a in b.anomalies] == ["POSITION_MISMATCH"]
    _, _, b = active()
    b.on_position_report(FILL + 1000 * MS, 2, Side.LONG, "MNQM4", ACCOUNT)
    assert b.open_quantity == 2  # the broker's quantity is what must be flattened


def test_d031_06_stop_and_target_stay_fixed_with_no_management_rules():
    pm = SPEC["position_management"]
    assert pm["breakeven_rule"] == pm["trailing_stop_rule"] == "NONE"
    spec = copy.deepcopy(SPEC)
    spec["position_management"]["trailing_stop_rule"] = "TRAIL_2_POINTS"
    assert "position_management.trailing_stop_rule" in invalid_paths(spec)


def test_d031_07_unresolved_intrabar_order_assumes_the_stop_first():
    one_second = [PriceInterval(FILL, D("20027.00"), D("19999.00"), PriceSource.AUTHORITATIVE_TRADES, "1s@bar")]
    d = resolve_minute(Side.LONG, STOP, TARGET, minute("20027.00", "19999.00"), PP, one_second)
    assert d.source is E.STOP and "CONSERVATIVE_STOP_FIRST" in d.flags


def test_d031_short_exit_pnl_is_signed_for_the_side():
    from test_entry_order import booked  # noqa: F401  (fixtures only)
    from test_confirmation import TRADE
    from test_trade_geometry import GP, T, cand, geo, zone
    from mnq_research.entry_order import EntryOrderBook
    from mnq_research.trade_geometry import select_candidate
    from test_trade_geometry import CLEAR

    book = EntryOrderBook(TRADE, "MNQM4", EP)
    sel = select_candidate(T, [geo(cand("SHORT", "19994", key="S"), zone("19970", "19975.75", "TS-"))], GP)
    book.record_selection(sel)
    order = book.create_order(sel.selected, T)
    order.submit(SUB, CLEAR, ())
    order.on_fill(FILL, 1, D("19993.50"), AUTH)
    b = bracket(order)
    b.dispatch(FILL)
    b.on_exit_fill(FILL + 5000 * MS, E.TARGET, 1, D("19976.00"), AUTH)
    (leg,) = b.exit_record()["exit_fills"]
    assert leg["realized_gross_pnl_points"] == "35/2" and leg["realized_gross_pnl_usd"] == "35"  # short: entry - exit
