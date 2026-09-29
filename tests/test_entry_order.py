"""Round 13 entry-order lifecycle (owner-specified test list, items 1-24).

Hand-built fixtures, not market data. Base long candidate (see test_trade_geometry):
decision 10:30:00 NY, confirmation close 20010.00, planned entry 20010.25,
frozen stop 19999.75, frozen target 20026.00. Broker reports are simulated inputs.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import datetime as dt
import inspect
import json
from decimal import Decimal
from fractions import Fraction

import pandas as pd
import pytest

from test_confirmation import TRADE
from test_trade_geometry import CLEAR, GP, SPEC, T, cand, geo, zone
from mnq_research import eligibility as eligibility_module, entry_order, sizing
from mnq_research.confirmation import Side
from mnq_research.eligibility import ControlState, ExecutionEligibility
from mnq_research.entry_order import (
    CandidateReuseError,
    Contradiction,
    DailyEntryState,
    DeadlineReason,
    EntryOrder,
    EntryOrderBook,
    EntryOrderParams,
    EntryOutcome as O,
    ExposureState,
    FillBeforeSubmissionError,
    FillSource,
    OrderPhase,
    StateLossCause,
)
from mnq_research.structural_levels import ny_time
from mnq_research.trade_geometry import select_candidate
from mnq_research.validation import check_rule_freeze

EP = EntryOrderParams.from_spec(SPEC)
D = Decimal
MS = pd.Timedelta(milliseconds=1)
SUB = T + pd.Timedelta(seconds=1)  # the only permitted submission instant
AUTH = FillSource.AUTHORITATIVE_FILL_RECORD


def booked(close: str = "20010", lo: str = "20000", key: str = "A", params: EntryOrderParams = EP):
    book = EntryOrderBook(TRADE, "MNQM4", params)
    sel = select_candidate(T, [geo(cand("LONG", close, lo=lo, key=key), zone("20026.25", "20030", "T-"))], GP)
    book.record_selection(sel)
    return book, sel.selected


def new_order(params: EntryOrderParams = EP, **kwargs):
    book, selected = booked(params=params, **kwargs)
    return book, book.create_order(selected, T)


def submitted(params: EntryOrderParams = EP, boundaries=()):
    book, order = new_order(params)
    order.submit(SUB, CLEAR, boundaries)
    return book, order


def with_state(**changes) -> ExecutionEligibility:
    return ExecutionEligibility.from_mapping({**CLEAR.__dict__, **changes})


def invalid_paths(spec) -> set[str]:
    return {p.path for p in check_rule_freeze(spec).problems if p.kind == "INVALID"}


def scenario(kind: str) -> tuple[EntryOrderBook, EntryOrder]:
    """Every failure (and the full fill) from a fresh book, fully deterministic."""
    params = dataclasses.replace(EP, research_quantity_contracts=3) if kind in ("partial", "race_after_cancel") else EP
    if kind == "not_submitted":
        book, order = new_order()
        order.submit(SUB, with_state(safety_halt=ControlState.BLOCKED), ())
        return book, order
    book, order = submitted(params)
    if kind == "unknown":
        order.advance(SUB + 2000 * MS)
        return book, order
    if kind == "rejected":
        order.on_rejected(SUB + 50 * MS, "RISK_LIMIT", "Order exceeds limit")
        return book, order
    order.on_acknowledged(SUB + 20 * MS)
    if kind == "full":
        order.on_fill(SUB + 200 * MS, 1, D("20010.50"), AUTH)
        return book, order
    if kind == "partial":
        order.on_fill(SUB + 300 * MS, 1, D("20010.50"), AUTH)
    if kind == "race_after_cancel":
        order.on_fill(SUB + 300 * MS, 1, D("20010.50"), AUTH)
    order.advance(SUB + 2000 * MS)
    order.on_cancel_confirmed(SUB + 2100 * MS)
    if kind == "race_after_cancel":
        order.on_fill(SUB + 2150 * MS, 1, D("20010.75"), AUTH)
    return book, order


# =========================================================================== 1-3 order type and timing


def test_01_entry_order_type_is_market_only():
    assert EP.entry_order_type == "MARKET" and new_order()[1].order_type == "MARKET"
    assert SPEC["order_type"]["entry_order_type"] == "MARKET"
    assert SPEC["order_type"]["prohibited_entry_order_types"] == ["MARKET_IF_TOUCHED", "LIMIT", "STOP_LIMIT", "STOP_MARKET"]
    for other in ("LIMIT", "MARKET_IF_TOUCHED", "STOP_LIMIT", "STOP_MARKET"):
        spec = copy.deepcopy(SPEC)
        spec["order_type"]["entry_order_type"] = other
        spec["entry_order_lifecycle"]["parameters"]["entry_order_type"] = other
        assert {"order_type.entry_order_type", "entry_order_lifecycle.parameters.entry_order_type"} <= invalid_paths(spec)
        with pytest.raises(ValueError):
            EntryOrderParams.from_spec(spec)
    # No price field exists that could turn the order into a limit/stop order.
    assert not {f.name for f in dataclasses.fields(EntryOrder)} & {"limit_price", "stop_price", "trigger_price"}


def test_02_submission_is_exactly_one_second_after_the_decision():
    assert SPEC["entry_order_lifecycle"]["parameters"]["signal_to_order_delay_seconds"] == "1.000"
    assert SPEC["decision_clock"]["signal_to_order_delay"]["value"].startswith("1.000 second")
    book, selected = booked()
    with pytest.raises(ValueError):  # created at the decision time, never later
        book.create_order(selected, T + MS)
    _, order = new_order()
    assert order.order_created_utc == T and order.scheduled_submission_utc == T + pd.Timedelta(seconds=1)
    for wrong in (SUB - MS, SUB + MS, T):
        with pytest.raises(ValueError):
            order.submit(wrong, CLEAR, ())
    assert order.phase is OrderPhase.CREATED
    order.submit(SUB, CLEAR, ())
    assert order.order_submitted_utc == SUB and order.phase is OrderPhase.SUBMITTED


def test_03_no_fill_can_occur_before_submission():
    _, order = new_order()
    with pytest.raises(FillBeforeSubmissionError):
        order.on_fill(T + 500 * MS, 1, D("20010.25"), AUTH)  # e.g. the decision-bar close trade
    order.submit(SUB, CLEAR, ())
    with pytest.raises(FillBeforeSubmissionError):
        order.on_fill(SUB - MS, 1, D("20010.25"), AUTH)
    assert order.fills == []
    order.on_fill(SUB, 1, D("20010.25"), AUTH)  # at or after submission is possible
    assert order.outcome is O.ENTRY_FULLY_FILLED


# =========================================================================== 4-5 eligibility at submission


def test_04_eligibility_is_rechecked_at_submission():
    for name in ExecutionEligibility.control_names():
        book, order = new_order()
        order.submit(SUB, with_state(**{name: ControlState.BLOCKED}), ())
        assert order.outcome is O.NOT_SUBMITTED_INELIGIBLE and order.phase is OrderPhase.FINAL
        assert order.reasons == [f"BLOCKED:{name.upper()}"] and order.order_submitted_utc is None
        assert order.final_order_state_utc == SUB and not order.halts_trading_date
        with pytest.raises(CandidateReuseError):
            book.create_order(order.candidate, T)
    # A news boundary already reached at the submission instant also blocks.
    _, order = new_order()
    order.submit(SUB, CLEAR, (SUB,))
    assert order.reasons == ["SUBMISSION_AT_OR_AFTER_NEWS_BOUNDARY"]
    # The 11:30 cutoff is checked from the clock itself, not only from the snapshot.
    late = EntryOrder(booked()[1], EP, ny_time(TRADE, dt.time(11, 29, 59)), 1, order_id="LATE")
    late.submit(ny_time(TRADE, dt.time(11, 30)), CLEAR, ())
    assert late.reasons == ["SUBMISSION_AT_OR_AFTER_NEW_ENTRY_CUTOFF"]


def test_05_unknown_eligibility_fails_closed():
    _, order = new_order()
    states = {n: ControlState.CLEAR for n in ExecutionEligibility.control_names() if n != "no_open_position"}
    order.submit(SUB, ExecutionEligibility.from_mapping(states), ())
    assert order.outcome is O.NOT_SUBMITTED_INELIGIBLE and order.reasons == ["UNKNOWN:NO_OPEN_POSITION"]
    _, order = new_order()
    with pytest.raises(TypeError):  # a plain dict / strings / booleans are never accepted
        order.submit(SUB, {n: "CLEAR" for n in ExecutionEligibility.control_names()}, ())
    with pytest.raises(TypeError):
        order.submit(SUB, CLEAR, ("10:45",))  # boundaries must be real, zone-aware timestamps
    assert order.phase is OrderPhase.CREATED and order.order_submitted_utc is None


# =========================================================================== 6-10 deadline, cancellation, unknown state


def test_06_order_expires_after_two_seconds_or_earlier_at_a_boundary():
    _, order = submitted()
    assert (order.deadline_utc, order.deadline_reason) == (SUB + 2000 * MS, DeadlineReason.WORKING_TIME_ELAPSED)
    _, order = submitted(boundaries=(SUB + 1500 * MS, SUB + 1800 * MS))
    assert (order.deadline_utc, order.deadline_reason) == (SUB + 1500 * MS, DeadlineReason.NEWS_BOUNDARY)
    near_cutoff = EntryOrder(booked()[1], EP, ny_time(TRADE, dt.time(11, 29, 58, 500000)), 1, order_id="NEAR")
    near_cutoff.submit(ny_time(TRADE, dt.time(11, 29, 59, 500000)), CLEAR, ())
    assert (near_cutoff.deadline_utc, near_cutoff.deadline_reason) == (ny_time(TRADE, dt.time(11, 30)), DeadlineReason.NEW_ENTRY_CUTOFF)
    for reason in (DeadlineReason.SAFETY_HALT, DeadlineReason.CONTRACT_OR_SESSION_INVALIDATED):
        _, order = submitted()
        order.on_acknowledged(SUB + 10 * MS)
        order.invalidate(SUB + 500 * MS, reason)
        assert (order.deadline_utc, order.deadline_reason) == (SUB + 500 * MS, reason)
        assert order.phase is OrderPhase.CANCEL_REQUESTED and order.cancellation_requested_utc == SUB + 500 * MS
    _, order = submitted()
    order.on_acknowledged(SUB + 10 * MS)
    order.on_state_lost(SUB + 500 * MS, StateLossCause.CONNECTION_LOSS)
    assert (order.deadline_utc, order.deadline_reason) == (SUB + 500 * MS, DeadlineReason.LOSS_OF_RELIABLE_STATE)
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN  # losing reliable state is itself an unknown state
    with pytest.raises(ValueError):
        submitted()[1].invalidate(SUB, DeadlineReason.LOSS_OF_RELIABLE_STATE)


def test_07_remainder_is_cancelled_at_the_deadline_never_extended_or_replaced():
    _, order = submitted()
    order.on_acknowledged(SUB + 20 * MS)
    order.advance(SUB + 1999 * MS)
    assert order.phase is OrderPhase.ACKNOWLEDGED and order.cancellation_requested_utc is None
    order.advance(SUB + 2500 * MS)  # even if processed late, the request is stamped at the deadline
    assert order.phase is OrderPhase.CANCEL_REQUESTED and order.cancellation_requested_utc == SUB + 2000 * MS
    order.on_cancel_confirmed(SUB + 2100 * MS)
    assert order.outcome is O.ENTRY_NOT_FILLED and order.cancellation_confirmed_utc == SUB + 2100 * MS
    with pytest.raises(ValueError):
        order.submit(SUB, CLEAR, ())
    names = {n.lower() for n in dir(EntryOrder)}
    assert not {n for n in names if any(w in n for w in ("extend", "replace", "modify", "chase", "convert", "resubmit", "retry"))}


def test_08_an_unacknowledged_order_is_reconciled_not_duplicated():
    book, order = scenario("unknown")
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and "NO_ACKNOWLEDGEMENT_OR_AUTHORITATIVE_STATE_WITHIN_TIMEOUT" in order.reasons
    assert order.outcome is not O.ENTRY_ORDER_REJECTED  # never assumed rejected
    assert order.reconciliation_required and order.critical_alert
    assert {"DO_NOT_RESUBMIT_OR_REPLACE", "QUERY_ORDER_STATUS", "QUERY_POSITION", "NO_EXPOSURE_INCREASE"} <= set(order.required_actions)
    with pytest.raises(ValueError):
        order.submit(SUB, CLEAR, ())
    with pytest.raises(CandidateReuseError):
        book.create_order(order.candidate, T)
    assert len(book.orders) == 1
    # A fill proves the order reached the market: missing acknowledgement alone is then not "unknown".
    _, filled = submitted(dataclasses.replace(EP, research_quantity_contracts=3))
    filled.on_fill(SUB + 300 * MS, 1, D("20010.50"), AUTH)
    filled.advance(SUB + 2000 * MS)
    assert filled.outcome is None and filled.phase is OrderPhase.CANCEL_REQUESTED


def test_09_unknown_state_halts_and_is_never_silently_resolved():
    book, order = scenario("unknown")
    assert book.halted and order.halts_trading_date
    controls = book.controls()
    assert controls["no_open_position"] is controls["no_working_entry_order"] is ControlState.UNKNOWN
    assert not ExecutionEligibility.from_mapping({**CLEAR.__dict__, **controls}).permits_entry
    order.on_acknowledged(SUB + 2500 * MS)  # a late acknowledgement does not clear the unknown state
    order.on_cancel_confirmed(SUB + 2600 * MS)
    order.reconcile(SUB + 3000 * MS, 0, Side.LONG, "MNQM4")
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and book.halted
    _, other = scenario("unknown")
    other.reconcile(SUB + 3000 * MS, 1, Side.LONG, "MNQM4")  # the query found a real position: protect it
    assert other.protection_task.quantity == 1 and "CONTRADICTION:POSITION_DIFFERS_FROM_AUTHORITATIVE_FILLS" in other.reasons


def test_10_zero_fill_creates_no_position_and_halts():
    book, order = scenario("not_filled")
    assert order.outcome is O.ENTRY_NOT_FILLED and order.filled_quantity == 0
    assert order.protection_task is None and order.average_fill_price is None
    assert book.halted and book.controls() == {
        "daily_entry_halt": ControlState.BLOCKED,
        "filled_entry_allowance_available": ControlState.CLEAR,  # no fill: allowance not used
        "no_open_position": ControlState.CLEAR,
        "no_working_entry_order": ControlState.CLEAR,
    }


# =========================================================================== 11-14 fills


def test_11_partial_fill_creates_exposure_for_the_confirmed_quantity_only():
    book, order = scenario("partial")
    assert order.quantity == 3 and order.outcome is O.ENTRY_PARTIALLY_FILLED and order.filled_quantity == 1
    task = order.protection_task
    assert task.quantity == 1 and task.frozen_stop_price == D("19999.75") and task.frozen_target_price == D("20026.00")
    assert order.actual_risk_points_per_contract == Fraction(D("20010.50") - D("19999.75"))
    assert order.actual_risk_points_total == order.actual_risk_points_per_contract * 1
    assert book.halted and book.controls()["no_open_position"] is ControlState.BLOCKED


def test_12_partial_fill_is_never_topped_up():
    book, order = scenario("partial")
    with pytest.raises(CandidateReuseError):
        book.create_order(order.candidate, T)
    with pytest.raises(ValueError):
        order.submit(SUB, CLEAR, ())
    assert order.phase is OrderPhase.FINAL and order.filled_quantity == 1
    assert book.controls()["daily_entry_halt"] is ControlState.BLOCKED


def test_13_full_fill_records_average_price_individual_fills_and_latency():
    _, order = submitted(dataclasses.replace(EP, research_quantity_contracts=3))
    order.on_acknowledged(SUB + 20 * MS)
    order.on_fill(SUB + 200 * MS, 1, D("20010.50"), AUTH)
    order.on_fill(SUB + 400 * MS, 2, D("20010.75"), AUTH)
    assert order.outcome is O.ENTRY_FULLY_FILLED and order.final_order_state_utc == SUB + 400 * MS
    assert [(f.quantity, f.price) for f in order.fills] == [(1, D("20010.50")), (2, D("20010.75"))]
    assert order.average_fill_price == Fraction(D("20010.50") + 2 * D("20010.75")) / 3
    assert order.slippage_vs_confirmation_close_points == Fraction(2, 3)  # vs close 20010.00
    assert order.slippage_vs_planned_entry_points == Fraction(5, 12)  # vs planned 20010.25
    assert order.submission_to_first_fill == 200 * MS and order.submission_to_final_fill == 400 * MS
    record = order.audit_record()
    for name in ("order_created_utc", "order_submitted_utc", "broker_acknowledged_utc", "first_fill_utc", "final_fill_utc", "final_order_state_utc"):
        assert record[name] is not None
    assert not order.halts_trading_date  # a full fill does not halt; the open position blocks new entries


def test_14_actual_r_uses_the_actual_fill_not_the_planned_entry():
    _, order = submitted()
    order.on_fill(SUB + 100 * MS, 1, D("20011.25"), AUTH)  # one point worse than planned
    assert order.actual_risk_points_per_contract == Fraction(D("11.50"))  # 20011.25 - frozen stop 19999.75
    assert order.actual_r_multiple(D("20026.00")) == Fraction(D("14.75")) / Fraction(D("11.50"))
    assert order.actual_r_multiple(D("20026.00")) < Fraction(3, 2)  # the planned 1.50R is not the actual R
    assert order.actual_r_multiple(D("19999.75")) == -1
    # Short mirror: adverse means a LOWER fill; risk runs up to the frozen stop U + 0.25.
    book = EntryOrderBook(TRADE, "MNQM4", EP)
    sel = select_candidate(T, [geo(cand("SHORT", "19994", key="S"), zone("19970", "19975.75", "TS-"))], GP)
    book.record_selection(sel)
    short = book.create_order(sel.selected, T)
    short.submit(SUB, CLEAR, ())
    short.on_fill(SUB + 100 * MS, 1, D("19993.00"), AUTH)  # planned 19993.75, close 19994.00
    assert short.slippage_vs_planned_entry_points == Fraction(3, 4) and short.slippage_vs_confirmation_close_points == 1
    assert short.actual_risk_points_per_contract == Fraction(D("11.25"))  # 20004.25 - 19993.00
    assert short.actual_r_multiple(D("19976.00")) == Fraction(17) / Fraction(D("11.25"))


# =========================================================================== 15-17 rejection, race, reuse


def test_15_rejection_is_not_retried():
    book, order = scenario("rejected")
    assert order.outcome is O.ENTRY_ORDER_REJECTED and order.reasons == ["REJECTED:RISK_LIMIT"]
    assert (order.rejection_code, order.rejection_message) == ("RISK_LIMIT", "Order exceeds limit")
    assert book.halted and order.order_type == "MARKET"
    with pytest.raises(ValueError):
        order.submit(SUB, CLEAR, ())
    with pytest.raises(CandidateReuseError):
        book.create_order(order.candidate, T)
    # A rejection after a recorded fill contradicts it: unknown state, never a clean rejection.
    _, filled = submitted(dataclasses.replace(EP, research_quantity_contracts=3))
    filled.on_fill(SUB + 100 * MS, 1, D("20010.50"), AUTH)
    filled.on_rejected(SUB + 200 * MS, "LATE", "rejected after fill")
    assert filled.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and filled.protection_task.quantity == 1


def test_16_a_cancellation_race_fill_is_real_exposure():
    _, order = submitted()
    order.on_acknowledged(SUB + 20 * MS)
    order.advance(SUB + 2000 * MS)
    order.on_fill(SUB + 2050 * MS, 1, D("20010.50"), AUTH)  # arrives after the cancel request
    assert order.cancellation_race_fill and order.reconciliation_required and order.halts_trading_date
    assert order.filled_quantity == 1 and order.protection_task.quantity == 1
    assert {"PROTECT_CONFIRMED_POSITION", "RECONCILE_ORDER_AND_POSITION"} <= set(order.required_actions)
    book, late = scenario("race_after_cancel")  # a fill even after the cancel was confirmed
    assert late.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and late.filled_quantity == 2
    assert late.protection_task.quantity == 2 and book.halted


def test_17_a_confirmation_is_not_reusable_after_any_outcome():
    for kind in ("not_submitted", "not_filled", "partial", "full", "rejected", "unknown", "race_after_cancel"):
        book, order = scenario(kind)
        with pytest.raises(CandidateReuseError):
            book.create_order(order.candidate, T)
        fresh = select_candidate(T, [geo(cand("LONG", key="A"), zone("20026.25", "20030", "T-"))], GP)
        with pytest.raises(CandidateReuseError):  # same confirmation / acceptance / attempt identity
            book.record_selection(fresh)
    # D-028: non-selected candidates are consumed too.
    book = EntryOrderBook(TRADE, "MNQM4", EP)
    target = zone("20040", "20044", "T-")
    sel = select_candidate(T, [geo(cand("LONG", "20010", key="A"), target), geo(cand("LONG", "20012", key="B"), target)], GP)
    book.record_selection(sel)
    retry_b = select_candidate(T, [geo(cand("LONG", "20012", key="B"), target)], GP)
    with pytest.raises(CandidateReuseError):
        book.record_selection(retry_b)


# =========================================================================== 18-23 quantity, stops, risk, protection


def test_18_research_quantity_is_exactly_one_and_not_deployment_sizing():
    assert EP.research_quantity_contracts == 1 and new_order()[1].quantity == 1
    assert SPEC["entry_order_lifecycle"]["research_quantity_labels"] == ["RESEARCH_QUANTITY_ONLY", "NOT_DEPLOYMENT_SIZING"]
    assert scenario("full")[1].audit_record()["quantity_labels"] == ["RESEARCH_QUANTITY_ONLY", "NOT_DEPLOYMENT_SIZING"]
    for bad in (2, 0, True, "1"):
        spec = copy.deepcopy(SPEC)
        spec["entry_order_lifecycle"]["parameters"]["research_quantity_contracts"] = bad
        assert "entry_order_lifecycle.parameters.research_quantity_contracts" in invalid_paths(spec)
        with pytest.raises(ValueError):
            EntryOrderParams.from_spec(spec)
    assert SPEC["position_management"]["contracts_per_trade"] == "TBD"  # deployment sizing is still open


def test_19_there_is_no_fixed_stop_size_filter():
    sp = SPEC["stop_placement"]
    assert (sp["minimum_stop_points"], sp["maximum_stop_points"], sp["trade_skipped_if_outside_stop_limits"]) == (
        "NOT_APPLICABLE", "NOT_APPLICABLE", False,
    )
    book = EntryOrderBook(TRADE, "MNQM4", EP)
    wide = geo(cand("LONG", "20010", lo="19950", key="W"), zone("20200", "20204", "FAR-"))  # 60.50-point stop
    sel = select_candidate(T, [wide], GP)
    book.record_selection(sel)
    order = book.create_order(sel.selected, T)
    order.submit(SUB, CLEAR, ())
    assert order.phase is OrderPhase.SUBMITTED and sel.selected.planned_risk_points == D("60.50")
    spec = copy.deepcopy(SPEC)
    spec["stop_placement"].update(minimum_stop_points=5, maximum_stop_points=40, trade_skipped_if_outside_stop_limits=True)
    assert {
        "stop_placement.minimum_stop_points", "stop_placement.maximum_stop_points", "stop_placement.trade_skipped_if_outside_stop_limits",
    } <= invalid_paths(spec)


def test_20_structural_stops_are_never_compressed_or_recalculated():
    _, order = submitted()
    order.on_fill(SUB + 100 * MS, 1, D("20012.00"), AUTH)  # heavy slippage
    assert order.protection_task.frozen_stop_price == order.candidate.planned_stop_price == D("19999.75")
    assert "stop" not in {p for p in inspect.signature(EntryOrder.submit).parameters}
    # Wrong-zone or non-protective stops are refused (never resized to "fix" them).
    _, selected = booked()
    for bad_stop in (D("20000.00"), D("20010.50")):
        forged = dataclasses.replace(selected, planned_stop_price=bad_stop, structural_invalidation_price=bad_stop, candidate_id="FORGED")
        book = EntryOrderBook(TRADE, "MNQM4", EP)
        book._awaiting_order[forged.candidate_id] = forged
        order = book.create_order(forged, T)
        assert order.outcome is O.NOT_SUBMITTED_INELIGIBLE and "INVALID_STOP:NOT_FROM_THE_ORIGINATING_ZONE" in order.reasons
    assert "INVALID_STOP:NOT_PROTECTIVE_OF_PLANNED_ENTRY" in order.reasons
    # A fill beyond the stop leaves the stop unprotective of the ACTUAL entry: flagged, never moved.
    _, order = submitted()
    order.on_fill(SUB + 100 * MS, 1, D("19999.50"), AUTH)
    assert not order.protection_task.stop_protective_of_actual_entry
    assert order.protection_task.frozen_stop_price == D("19999.75")
    assert order.protection_task.status == "EMERGENCY_FLATTEN_REQUIRED"
    with pytest.raises(ValueError):
        order.actual_r_multiple(D("20026.00"))


def test_21_risk_per_trade_is_unresolved():
    rpt = SPEC["position_management"]["risk_per_trade"]
    assert rpt["status"] == "UNRESOLVED_EVIDENCE_DERIVED" and "floor" in rpt["later_formula"]
    assert "position_management.risk_per_trade.status" in check_rule_freeze(SPEC).unresolved_paths()
    source = inspect.getsource(entry_order).lower()
    assert "risk_per_trade" not in source and "allowed_risk" not in source


def test_22_nominal_account_size_is_not_risk_capital():
    pm = SPEC["position_management"]
    assert pm["position_sizing_balance_basis"] == "UNRESOLVED_PENDING_PROP_RULE_MODEL"
    assert "position_management.position_sizing_balance_basis" in check_rule_freeze(SPEC).unresolved_paths()
    for module in (entry_order, eligibility_module):
        source = inspect.getsource(module).lower()
        assert "account_size" not in source and "50000" not in source and "balance" not in source


def test_23_no_live_or_paper_entry_without_the_protection_layer():
    dep = SPEC["entry_order_lifecycle"]["protective_order_dependency"]
    assert dep["live_or_paper_order_submission"] == "prohibited" and EP.live_or_paper_order_submission == "prohibited"
    assert "entry_order_lifecycle.protective_order_dependency.protective_order_layer_status" in check_rule_freeze(SPEC).unresolved_paths()
    spec = copy.deepcopy(SPEC)
    spec["entry_order_lifecycle"]["protective_order_dependency"]["live_or_paper_order_submission"] = "permitted"
    assert "entry_order_lifecycle.protective_order_dependency.live_or_paper_order_submission" in invalid_paths(spec)
    with pytest.raises(ValueError):
        EntryOrderParams.from_spec(spec)
    _, order = scenario("full")
    assert order.protection_task.status == "REQUIRED_PROTECTION_LAYER_NOT_IMPLEMENTED"
    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(entry_order))):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "decimal", "enum", "fractions", "typing", "pandas", "mnq_research"}


# =========================================================================== 24 audit and parameters


def test_24_every_failure_has_a_deterministic_audit_record():
    for kind in ("not_submitted", "not_filled", "partial", "rejected", "unknown", "race_after_cancel", "full"):
        first, second = scenario(kind)[1].audit_record(), scenario(kind)[1].audit_record()
        assert first == second
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)  # serialisable, stable
        assert first["outcome"] is not None and first["events"]
        assert first["configuration_hash"] and first["order_created_utc"] == T.isoformat()
        if kind != "full":
            assert first["reasons"], kind


def test_all_numeric_trading_parameters_come_from_the_specification():
    spec = copy.deepcopy(SPEC)
    spec["entry_order_lifecycle"]["parameters"].update(signal_to_order_delay_seconds="2.500", entry_order_max_working_seconds="0.750")
    params = EntryOrderParams.from_spec(spec)
    book, selected = booked(params=params)
    order = book.create_order(selected, T)
    assert order.scheduled_submission_utc == T + 2500 * MS
    order.submit(T + 2500 * MS, CLEAR, ())
    assert order.deadline_utc == T + 3250 * MS
    for module in (entry_order, eligibility_module, sizing):
        literals = {
            n.value
            for n in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
        }
        assert literals <= {0, 1}, module.__name__
    spec["entry_order_lifecycle"]["parameters"].update(entry_order_max_working_seconds=2, order_acknowledgement_timeout_seconds="-1")
    assert {
        "entry_order_lifecycle.parameters.entry_order_max_working_seconds",
        "entry_order_lifecycle.parameters.order_acknowledgement_timeout_seconds",
    } <= invalid_paths(spec)


# =========================================================================== D-029 confirmations


def fresh_order(book: EntryOrderBook, key: str = "B"):
    sel = select_candidate(T, [geo(cand("LONG", key=key), zone("20026.25", "20030", "T-"))], GP)
    book.record_selection(sel)
    return book.create_order(sel.selected, T)


def test_d029_01_acknowledgement_timeout_is_two_seconds_from_submission():
    assert SPEC["entry_order_lifecycle"]["parameters"]["order_acknowledgement_timeout_seconds"] == "2.000"
    assert EP.acknowledgement_timeout == 2000 * MS
    _, order = submitted()
    order.advance(SUB + 1999 * MS)
    assert order.outcome is None
    order.advance(SUB + 2000 * MS)
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN
    assert order.anomalies[0].timestamp_utc == SUB + 2000 * MS  # stamped at submission + 2 s, not at creation + 2 s
    _, on_time = submitted()
    on_time.on_acknowledged(SUB + 2000 * MS)  # exactly at the limit counts
    on_time.advance(SUB + 2000 * MS)
    assert on_time.outcome is None
    _, late = submitted()
    late.on_acknowledged(SUB + 2100 * MS)  # after the limit: too late
    late.advance(SUB + 2500 * MS)
    assert late.outcome is O.ENTRY_ORDER_STATE_UNKNOWN
    _, rejected = submitted()
    rejected.on_rejected(SUB + 100 * MS, "X", "rejected")  # a rejection is an authoritative order state
    rejected.advance(SUB + 2000 * MS)
    assert rejected.outcome is O.ENTRY_ORDER_REJECTED


def test_d029_02_authoritative_fills_prove_market_receipt_without_acknowledgement():
    book, order = submitted()
    order.on_fill(SUB + 300 * MS, 1, D("20010.50"), AUTH)
    order.advance(SUB + 2000 * MS)
    assert order.outcome is O.ENTRY_FULLY_FILLED  # not UNKNOWN merely because the acknowledgement is missing
    assert [a.code for a in order.anomalies] == ["ACKNOWLEDGEMENT_MISSING_BUT_FILL_CONFIRMED"]
    assert {"CONTINUE_RECONCILIATION", "PROTECT_CONFIRMED_POSITION", "DO_NOT_RESUBMIT_OR_REPLACE"} <= set(order.required_actions)
    assert order.reconciliation_required and order.protection_task.quantity == 1
    with pytest.raises(ValueError):
        order.submit(SUB, CLEAR, ())


def test_d029_03_local_fill_estimates_are_not_proof():
    _, order = submitted()
    order.on_fill(SUB + 300 * MS, 1, D("20010.50"), FillSource.LOCAL_ESTIMATE)
    order.advance(SUB + 2000 * MS)
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN
    assert order.filled_quantity == 0 and order.protection_task is None and not order.consumed_filled_entry
    assert len(order.local_fill_estimates) == 1 and order.audit_record()["local_fill_estimates"]
    for unlabelled in ("AUTHORITATIVE_FILL_RECORD", None, True):
        with pytest.raises(TypeError):
            submitted()[1].on_fill(SUB + 300 * MS, 1, D("20010.50"), unlabelled)


def test_d029_04_lost_reliable_state_becomes_unknown_and_fails_closed():
    for cause in StateLossCause:
        book, order = submitted()
        order.on_acknowledged(SUB + 10 * MS)
        order.on_state_lost(SUB + 400 * MS, cause)
        assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and f"LOST_RELIABLE_STATE:{cause.value}" in order.reasons
        assert order.phase is OrderPhase.CANCEL_REQUESTED and book.halted
        assert {"DO_NOT_RESUBMIT_OR_REPLACE", "NO_EXPOSURE_INCREASE", "PRESERVE_CONFIRMED_PROTECTIVE_ORDERS",
                "PROTECT_CONFIRMED_POSITION", "CRITICAL_ALERT"} <= set(order.required_actions)
    _, filled = scenario("full")
    filled.on_state_lost(SUB + 1000 * MS, StateLossCause.POSITION_QUERY_FAILURE)  # even after a fill: fail closed
    assert filled.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and filled.protection_task.quantity == 1
    _, queried = scenario("full")
    queried.reconcile(SUB + 1000 * MS, None, None, None)  # the position query itself failed
    assert queried.current_exposure is ExposureState.RECONCILIATION_UNRESOLVED
    assert "LOST_RELIABLE_STATE:POSITION_QUERY_FAILURE" in queried.reasons
    with pytest.raises(TypeError):
        submitted()[1].on_state_lost(SUB, "CONNECTION_LOSS")


def contradiction_cases():
    q3 = dataclasses.replace(EP, research_quantity_contracts=3)
    _, overfill = scenario("full")
    overfill.on_fill(SUB + 500 * MS, 1, D("20010.50"), AUTH)
    yield Contradiction.FILLED_MORE_THAN_SUBMITTED, overfill
    yield Contradiction.FILL_AFTER_CONFIRMED_CANCELLATION, scenario("race_after_cancel")[1]
    _, fill_then_reject = submitted(q3)
    fill_then_reject.on_fill(SUB + 100 * MS, 1, D("20010.50"), AUTH)
    fill_then_reject.on_rejected(SUB + 200 * MS, "R", "rejected")
    yield Contradiction.REJECTION_AND_FILL_FOR_SAME_ORDER, fill_then_reject
    _, reject_then_fill = scenario("rejected")
    reject_then_fill.on_fill(SUB + 300 * MS, 1, D("20010.50"), AUTH)
    yield Contradiction.REJECTION_AND_FILL_FOR_SAME_ORDER, reject_then_fill
    _, differs = scenario("full")
    differs.reconcile(SUB + 900 * MS, 2, Side.LONG, "MNQM4")
    yield Contradiction.POSITION_DIFFERS_FROM_AUTHORITATIVE_FILLS, differs
    for side, contract in ((Side.SHORT, "MNQM4"), (Side.LONG, "MNQU4")):
        _, mismatch = scenario("full")
        mismatch.reconcile(SUB + 900 * MS, 1, side, contract)
        yield Contradiction.SIDE_OR_CONTRACT_MISMATCH, mismatch
    _, reported = submitted()
    reported.report_contradiction(SUB + 100 * MS, Contradiction.SIDE_OR_CONTRACT_MISMATCH, "fill record contract MNQU4")
    yield Contradiction.SIDE_OR_CONTRACT_MISMATCH, reported
    _, terminal = submitted()
    terminal.on_acknowledged(SUB + 10 * MS)
    terminal.advance(SUB + 2000 * MS)
    terminal.on_fill(SUB + 2050 * MS, 1, D("20010.50"), AUTH)  # race fill completes the order
    terminal.on_cancel_confirmed(SUB + 2100 * MS)  # ...then "cancelled" is reported too
    yield Contradiction.INCOMPATIBLE_TERMINAL_STATES, terminal


def test_d029_05_contradictions_create_an_immutable_unknown_event():
    kinds = set()
    for kind, order in contradiction_cases():
        kinds.add(kind)
        assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and f"CONTRADICTION:{kind.value}" in order.reasons, kind
        assert order.halts_trading_date and order.critical_alert
        event = next(a for a in order.anomalies if kind.value in a.detail)
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.code = "EDITED"
        assert isinstance(order.anomalies, tuple)
    assert kinds == set(Contradiction)


def test_d029_06_reconciliation_updates_exposure_without_rewriting_the_anomaly():
    book, order = scenario("unknown")
    history = order.anomalies
    order.reconcile(SUB + 3000 * MS, 0, Side.LONG, "MNQM4")
    assert order.current_exposure is ExposureState.RECONCILED_FLAT
    assert order.anomalies == history and order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN
    order.reconcile(SUB + 4000 * MS, 1, Side.LONG, "MNQM4")  # later the broker shows a position after all
    assert [r.exposure_state for r in order.reconciliations] == [ExposureState.RECONCILED_FLAT, ExposureState.RECONCILED_OPEN_POSITION]
    assert order.anomalies[: len(history)] == history  # earlier events untouched; new ones only appended
    assert "PROTECT_OR_FLATTEN_RECONCILED_POSITION" in order.required_actions and order.protection_task.quantity == 1
    assert book.halted and not ExecutionEligibility.from_mapping({**CLEAR.__dict__, **book.controls()}).permits_entry
    _, partial = scenario("partial")
    partial.reconcile(SUB + 3000 * MS, 1, Side.LONG, "MNQM4")
    assert partial.current_exposure is ExposureState.RECONCILED_PARTIAL_POSITION and partial.outcome is O.ENTRY_PARTIALLY_FILLED


def test_d029_07_a_fill_while_cancellation_is_pending_is_a_valid_race_fill():
    _, order = submitted()
    order.on_acknowledged(SUB + 20 * MS)
    order.advance(SUB + 2000 * MS)
    assert order.phase is OrderPhase.CANCEL_REQUESTED
    order.on_fill(SUB + 2050 * MS, 1, D("20010.50"), AUTH)
    assert order.cancellation_race_fill and order.outcome is O.ENTRY_FULLY_FILLED
    assert not [r for r in order.reasons if r.startswith("CONTRADICTION")]
    assert order.protection_task.quantity == 1


def test_d029_08_a_fill_after_confirmed_cancellation_is_contradictory():
    _, order = scenario("not_filled")
    assert order.cancellation_confirmed_utc is not None
    final_at = order.final_order_state_utc
    order.on_fill(SUB + 2200 * MS, 1, D("20010.50"), AUTH)
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN
    assert "CONTRADICTION:FILL_AFTER_CONFIRMED_CANCELLATION" in order.reasons
    assert order.filled_quantity == 1 and order.protection_task.quantity == 1  # still real exposure
    assert order.final_order_state_utc == final_at  # the historical final state is not rewritten


def test_d029_09_any_positive_fill_consumes_the_one_entry_daily_allowance():
    assert SPEC["daily_limits"]["max_filled_entries_per_trading_date"] == 1 and EP.max_filled_entries_per_trading_date == 1
    books = [scenario(kind)[0] for kind in ("full", "partial", "race_after_cancel")]
    race_book, race = submitted()
    race.on_acknowledged(SUB + 20 * MS)
    race.advance(SUB + 2000 * MS)
    race.on_fill(SUB + 2050 * MS, 1, D("20010.50"), AUTH)
    no_ack_book, no_ack = submitted()
    no_ack.on_fill(SUB + 300 * MS, 1, D("20010.50"), AUTH)
    no_ack.advance(SUB + 2000 * MS)
    reconciled_book, reconciled = scenario("unknown")
    reconciled.reconcile(SUB + 3000 * MS, 1, Side.LONG, "MNQM4")
    for book in books + [race_book, no_ack_book, reconciled_book]:
        assert book.filled_entry_allowance_consumed and book.daily_state is DailyEntryState.FILLED_ENTRY_LIMIT_REACHED
        assert book.controls()["filled_entry_allowance_available"] is ControlState.BLOCKED
        second = fresh_order(book)
        assert second.outcome is O.NOT_SUBMITTED_INELIGIBLE and "FILLED_ENTRY_LIMIT_REACHED" in second.reasons
    spec = copy.deepcopy(SPEC)
    spec["daily_limits"]["max_filled_entries_per_trading_date"] = 2
    assert "daily_limits.max_filled_entries_per_trading_date" in invalid_paths(spec)
    with pytest.raises(ValueError):
        EntryOrderParams.from_spec(spec)


def test_d029_10_closing_the_position_does_not_restore_entry_eligibility():
    book, _ = scenario("full")
    assert book.controls()["no_open_position"] is ControlState.BLOCKED
    book.record_position_flat(SUB + 60000 * MS)  # e.g. a later stop or target exit
    controls = book.controls()
    assert controls["no_open_position"] is ControlState.CLEAR
    assert controls["filled_entry_allowance_available"] is ControlState.BLOCKED
    assert not ExecutionEligibility.from_mapping({**CLEAR.__dict__, **controls}).permits_entry
    assert book.daily_state is DailyEntryState.FILLED_ENTRY_LIMIT_REACHED
    assert fresh_order(book).outcome is O.NOT_SUBMITTED_INELIGIBLE
    assert SPEC["reentry"]["allowed"] is False and SPEC["daily_limits"]["max_trades_per_day"] == 1
    spec = copy.deepcopy(SPEC)
    spec["reentry"]["allowed"] = True
    spec["daily_limits"]["max_trades_per_day"] = 2
    assert {"reentry.allowed", "daily_limits.max_trades_per_day"} <= invalid_paths(spec)


def test_d029_11_not_submitted_does_not_consume_the_filled_entry_allowance():
    book, order = new_order()
    order.submit(SUB, with_state(news_entry_protection=ControlState.BLOCKED), ())  # temporary known block
    assert order.outcome is O.NOT_SUBMITTED_INELIGIBLE and not order.consumed_filled_entry
    assert not book.halted and book.daily_state is DailyEntryState.OPEN_FOR_ENTRIES
    later = fresh_order(book)  # a completely fresh setup after the block ends
    later.submit(SUB, CLEAR, ())
    assert later.phase is OrderPhase.SUBMITTED
    _, safety = new_order()
    safety.submit(SUB, with_state(safety_halt=ControlState.BLOCKED), ())
    assert not safety.halts_trading_date  # the safety halt itself keeps the date halted, via its own control


def test_d029_12_unknown_submission_eligibility_halts_the_date():
    book, order = new_order()
    order.submit(SUB, ExecutionEligibility.from_mapping({n: ControlState.CLEAR for n in ExecutionEligibility.control_names() if n != "data_valid"}), ())
    assert order.reasons == ["UNKNOWN:DATA_VALID"] and order.halts_trading_date
    assert book.daily_state is DailyEntryState.HALTED and not order.consumed_filled_entry
    later = fresh_order(book)
    assert later.outcome is O.NOT_SUBMITTED_INELIGIBLE and "DAILY_ENTRY_HALT" in later.reasons


def test_d029_13_a_fill_at_or_beyond_the_stop_invokes_emergency_flattening():
    for price in ("19999.75", "19999.00"):  # at, and beyond, the long stop
        book, order = submitted()
        order.on_fill(SUB + 100 * MS, 1, D(price), AUTH)
        assert order.emergency_flatten_required and order.halts_trading_date and book.halted
        assert {"ENTRY_FILLED_AT_OR_BEYOND_INVALIDATION", "EMERGENCY_FLATTEN_REQUIRED"} <= set(order.reasons)
        assert "EMERGENCY_FLATTEN_CONFIRMED_QUANTITY" in order.required_actions
        assert order.protection_task.status == "EMERGENCY_FLATTEN_REQUIRED" and order.protection_task.quantity == 1
        assert order.outcome is O.ENTRY_FULLY_FILLED and order.consumed_filled_entry  # never "not opened"
        assert order.audit_record()["fills"] == [[(SUB + 100 * MS).isoformat(), 1, price, "AUTHORITATIVE_FILL_RECORD"]]
    book = EntryOrderBook(TRADE, "MNQM4", EP)
    sel = select_candidate(T, [geo(cand("SHORT", "19994", key="S"), zone("19970", "19975.75", "TS-"))], GP)
    book.record_selection(sel)
    short = book.create_order(sel.selected, T)
    short.submit(SUB, CLEAR, ())
    short.on_fill(SUB + 100 * MS, 1, D("20004.25"), AUTH)  # at the short stop U + 0.25
    assert short.emergency_flatten_required
    _, valid = submitted()
    valid.on_fill(SUB + 100 * MS, 1, D("20000.00"), AUTH)  # one tick inside: valid side
    assert not valid.emergency_flatten_required and valid.protection_task.status == "REQUIRED_PROTECTION_LAYER_NOT_IMPLEMENTED"


def test_d029_14_the_structural_stop_is_never_widened():
    _, order = submitted()
    order.on_fill(SUB + 100 * MS, 1, D("20012.00"), AUTH)  # valid side, 1.75 points worse than planned
    assert order.actual_risk_exceeds_planned
    assert order.actual_gross_rr_to_frozen_target == Fraction(D("14.00")) / Fraction(D("12.25"))  # degraded, recorded
    assert order.protection_task.frozen_stop_price == D("19999.75") and order.protection_task.frozen_target_price == D("20026.00")
    _, beyond = submitted()
    beyond.on_fill(SUB + 100 * MS, 1, D("19998.00"), AUTH)
    assert beyond.protection_task.frozen_stop_price == D("19999.75")  # not moved farther away
    assert beyond.actual_gross_rr_to_frozen_target is None
    assert not {n for n in dir(EntryOrder) if "widen" in n or "move_stop" in n}


def test_d029_15_contract_sizing_rounds_down():
    assert SPEC["position_management"]["contract_rounding"] == "FLOOR_TO_WHOLE_CONTRACT"
    sizing.check_sizing_rules(SPEC)
    assert sizing.size_position(D("299.99"), D("100")).contracts == 2
    assert sizing.size_position(D("300"), D("100")).contracts == 3
    assert sizing.size_position(D("250"), D("100")) == sizing.SizingDecision(2, sizing.SizingAction.TRADE, None)
    spec = copy.deepcopy(SPEC)
    spec["position_management"]["contract_rounding"] = "ROUND_HALF_UP"
    assert "position_management.contract_rounding" in invalid_paths(spec)
    with pytest.raises(ValueError):
        sizing.check_sizing_rules(spec)


def test_d029_16_a_result_below_one_contract_skips_the_trade():
    assert SPEC["position_management"]["below_one_contract_action"] == "SKIP_TRADE"
    for allowed in (D("99.99"), D("0")):
        decision = sizing.size_position(allowed, D("100"))
        assert decision == sizing.SizingDecision(0, sizing.SizingAction.SKIP_TRADE, "POSITION_SIZE_BELOW_ONE_CONTRACT")
    with pytest.raises(ValueError):
        sizing.size_position(D("100"), D("0"))
    for bad in (100.0, 250, D("Infinity"), D("NaN")):  # floats, ints and non-finite values are refused
        with pytest.raises(TypeError):
            sizing.size_position(bad, D("100"))
    spec = copy.deepcopy(SPEC)
    spec["position_management"]["below_one_contract_action"] = "FORCE_ONE_CONTRACT"
    assert "position_management.below_one_contract_action" in invalid_paths(spec)


def test_d029_17_risk_per_trade_remains_unresolved():
    unresolved = check_rule_freeze(SPEC).unresolved_paths()
    assert "position_management.risk_per_trade.status" in unresolved
    assert "position_management.position_sizing_balance_basis" in unresolved
    source = inspect.getsource(sizing)
    literals = {n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)}
    assert literals <= {0, 1}  # no dollar amount hidden in code


def test_d029_18_long_and_short_trigger_fields_are_fully_populated():
    trig = SPEC["entry_trigger"]
    for field_name, side in (("long_trigger", "LONG"), ("short_trigger", "SHORT")):
        text = " ".join(trig[field_name].split())
        for phrase in ("SELECTED_ENTRY_CANDIDATE", f"direction {side}", "one MARKET entry order", "exactly 1.000 second later", "typed CLEAR"):
            assert phrase in text, (field_name, phrase)
    assert len(trig["not_a_trigger"]) == 7 and "a discretionary command" in trig["not_a_trigger"]
    assert not [p for p in check_rule_freeze(SPEC).unresolved_paths() if p.startswith("entry_trigger")]


def test_d029_19_one_selected_candidate_creates_at_most_one_order_lifecycle():
    for kind in ("not_submitted", "not_filled", "partial", "full", "rejected", "unknown"):
        book, order = scenario(kind)
        with pytest.raises(CandidateReuseError):
            book.create_order(order.candidate, T)
        assert len(book.orders) == 1
    book, selected = booked()
    sel_again = select_candidate(T, [geo(cand("LONG", key="A"), zone("20026.25", "20030", "T-"))], GP)
    with pytest.raises(CandidateReuseError):
        book.record_selection(sel_again)  # the same confirmation cannot be selected into a second lifecycle
    book.create_order(selected, T)
    with pytest.raises(CandidateReuseError):
        book.create_order(selected, T)
