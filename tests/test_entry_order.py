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
from mnq_research import eligibility as eligibility_module, entry_order
from mnq_research.eligibility import ControlState, ExecutionEligibility
from mnq_research.entry_order import (
    CandidateReuseError,
    DeadlineReason,
    EntryOrder,
    EntryOrderBook,
    EntryOrderParams,
    EntryOutcome as O,
    FillBeforeSubmissionError,
    OrderPhase,
)
from mnq_research.structural_levels import ny_time
from mnq_research.trade_geometry import select_candidate
from mnq_research.validation import check_rule_freeze

EP = EntryOrderParams.from_spec(SPEC)
D = Decimal
MS = pd.Timedelta(milliseconds=1)
SUB = T + pd.Timedelta(seconds=1)  # the only permitted submission instant


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
        order.on_fill(SUB + 200 * MS, 1, D("20010.50"))
        return book, order
    if kind == "partial":
        order.on_fill(SUB + 300 * MS, 1, D("20010.50"))
    if kind == "race_after_cancel":
        order.on_fill(SUB + 300 * MS, 1, D("20010.50"))
    order.advance(SUB + 2000 * MS)
    order.on_cancel_confirmed(SUB + 2100 * MS)
    if kind == "race_after_cancel":
        order.on_fill(SUB + 2150 * MS, 1, D("20010.75"))
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
        order.on_fill(T + 500 * MS, 1, D("20010.25"))  # e.g. the decision-bar close trade
    order.submit(SUB, CLEAR, ())
    with pytest.raises(FillBeforeSubmissionError):
        order.on_fill(SUB - MS, 1, D("20010.25"))
    assert order.fills == []
    order.on_fill(SUB, 1, D("20010.25"))  # at or after submission is possible
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
    for reason in (DeadlineReason.SAFETY_HALT, DeadlineReason.CONTRACT_OR_SESSION_INVALIDATED, DeadlineReason.LOSS_OF_RELIABLE_STATE):
        _, order = submitted()
        order.on_acknowledged(SUB + 10 * MS)
        order.invalidate(SUB + 500 * MS, reason)
        assert (order.deadline_utc, order.deadline_reason) == (SUB + 500 * MS, reason)
        assert order.phase is OrderPhase.CANCEL_REQUESTED and order.cancellation_requested_utc == SUB + 500 * MS
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN  # losing reliable state is itself an unknown state


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
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and "NO_ACKNOWLEDGEMENT_WITHIN_TIMEOUT" in order.reasons
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
    filled.on_fill(SUB + 300 * MS, 1, D("20010.50"))
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
    order.reconcile(SUB + 3000 * MS, 0)
    assert order.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and book.halted
    _, other = scenario("unknown")
    other.reconcile(SUB + 3000 * MS, 1)  # the query found a real position: protect it
    assert other.protection_task.quantity == 1 and "RECONCILED_POSITION_DIFFERS_FROM_RECORDED_FILLS" in other.reasons


def test_10_zero_fill_creates_no_position_and_halts():
    book, order = scenario("not_filled")
    assert order.outcome is O.ENTRY_NOT_FILLED and order.filled_quantity == 0
    assert order.protection_task is None and order.average_fill_price is None
    assert book.halted and book.controls() == {
        "daily_entry_halt": ControlState.BLOCKED,
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
    order.on_fill(SUB + 200 * MS, 1, D("20010.50"))
    order.on_fill(SUB + 400 * MS, 2, D("20010.75"))
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
    order.on_fill(SUB + 100 * MS, 1, D("20011.25"))  # one point worse than planned
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
    short.on_fill(SUB + 100 * MS, 1, D("19993.00"))  # planned 19993.75, close 19994.00
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
    filled.on_fill(SUB + 100 * MS, 1, D("20010.50"))
    filled.on_rejected(SUB + 200 * MS, "LATE", "rejected after fill")
    assert filled.outcome is O.ENTRY_ORDER_STATE_UNKNOWN and filled.protection_task.quantity == 1


def test_16_a_cancellation_race_fill_is_real_exposure():
    _, order = submitted()
    order.on_acknowledged(SUB + 20 * MS)
    order.advance(SUB + 2000 * MS)
    order.on_fill(SUB + 2050 * MS, 1, D("20010.50"))  # arrives after the cancel request
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
    order.on_fill(SUB + 100 * MS, 1, D("20012.00"))  # heavy slippage
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
    order.on_fill(SUB + 100 * MS, 1, D("19999.50"))
    assert not order.protection_task.stop_protective_of_actual_entry
    assert order.protection_task.frozen_stop_price == D("19999.75")
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
    for module in (entry_order, eligibility_module):
        literals = {
            n.value
            for n in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
        }
        assert literals <= {0, 1}, module.__name__
    spec["entry_order_lifecycle"]["parameters"].update(entry_order_max_working_seconds=2, acknowledgement_timeout_seconds="-1")
    assert {
        "entry_order_lifecycle.parameters.entry_order_max_working_seconds",
        "entry_order_lifecycle.parameters.acknowledgement_timeout_seconds",
    } <= invalid_paths(spec)
