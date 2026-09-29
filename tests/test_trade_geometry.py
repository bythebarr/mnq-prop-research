"""Round 12 trade geometry, room to target and candidate selection (owner-specified test list).

Hand-built fixtures, not market data. Base long example (origin zone 20000-20004):
confirmation close 20010.00 -> planned entry 20010.25, stop 19999.75 (risk 10.50);
target zone lower 20026.25 -> target 20026.00 (reward 15.75) -> exactly 1.50R.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import inspect
from dataclasses import fields
from decimal import Decimal
from fractions import Fraction

import pytest

from conftest import RULE_FREEZE_PATH
from test_confirmation import HOLD, STATE_PARAMS, TRADE, Session
from mnq_research import trade_geometry
from mnq_research.config import load_mapping
from mnq_research.confirmation import Side
from mnq_research.direction import CandidateStatus as S, DailyDirectionBook, DirectionCandidate
from mnq_research.eligibility import ControlState, ExecutionEligibility
from mnq_research.level_states import ZoneTracker
from mnq_research.structural_levels import LevelType, StructuralLevel, cluster_levels, ny_time
from mnq_research.trade_geometry import GeometryParams, SelectionResult as R, evaluate_geometry, select_candidate
from mnq_research.validation import check_rule_freeze, rule_spec_hash

SPEC = load_mapping(RULE_FREEZE_PATH)
GP = GeometryParams.from_spec(SPEC)
T = ny_time(TRADE, dt.time(10, 30))
T0945 = ny_time(TRADE, dt.time(9, 45))
D = Decimal
CLEAR = ExecutionEligibility.all_clear()


def zone(lower: str, upper: str, label: str, contract: str = "MNQM4", date: dt.date = TRADE) -> ZoneTracker:
    levels = [
        StructuralLevel(LevelType.PRIOR_RTH_HIGH, date, contract, "PRIOR_RTH", None, None, T0945, price=float(lower)),
        StructuralLevel(LevelType.OVERNIGHT_HIGH, date, contract, "OVERNIGHT", None, None, T0945, price=float(upper)),
    ]
    (cluster,) = cluster_levels(levels, D(upper) - D(lower), T0945, label=label)
    return ZoneTracker(cluster, STATE_PARAMS, D("6"), T0945)


ORIGIN = zone("20000", "20004", "ORIGIN-")


def cand(side: str = "LONG", close: str = "20010", lo: str = "20000", hi: str = "20004", key: str = "A") -> DirectionCandidate:
    return DirectionCandidate(
        Side.LONG if side == "LONG" else Side.SHORT,
        TRADE,
        "MNQM4",
        f"{ORIGIN.zone_id}{key}",
        1,
        f"{ORIGIN.zone_id}{key}:ACC1",
        T,
        S.ENTRY_CANDIDATE,
        confirmation_id=f"{ORIGIN.zone_id}{key}:ACC1:CONF",
        confirmation_close=D(close),
        origin_lower_boundary=D(lo),
        origin_upper_boundary=D(hi),
    )


def geo(c, *zones, eligibility=CLEAR):
    return evaluate_geometry(c, T, [ORIGIN, *zones], GP, eligibility)


# =========================================================================== entry, stop, risk


def test_planned_entry_uses_a_one_tick_adverse_buffer():
    assert geo(cand("LONG", "20010"), zone("20030", "20032", "T-")).planned_entry_price == D("20010.25")
    assert geo(cand("SHORT", "19994"), zone("19970", "19972", "T-")).planned_entry_price == D("19993.75")


def test_structural_invalidation_is_one_tick_beyond_the_far_side():
    long_r = geo(cand("LONG"), zone("20030", "20032", "T-"))
    assert long_r.structural_invalidation_price == long_r.planned_stop_price == D("19999.75")  # L - 0.25
    short_r = geo(cand("SHORT", "19994"), zone("19970", "19972", "T-"))
    assert short_r.structural_invalidation_price == short_r.planned_stop_price == D("20004.25")  # U + 0.25


def test_the_stop_distance_cannot_be_compressed():
    far = geo(cand("LONG", "20020"), zone("20060", "20062", "T-"))
    near = geo(cand("LONG", "20010"), zone("20060", "20062", "T-"))
    assert far.planned_stop_price == near.planned_stop_price == D("19999.75")  # always the structural stop
    assert far.planned_risk_points == D("20.50")
    # No argument exists to override the stop or target a dollar risk / contract count.
    assert list(inspect.signature(evaluate_geometry).parameters) == [
        "candidate", "decision_time_utc", "active_zones", "params", "eligibility"
    ]


# =========================================================================== target zone and target price


def test_the_nearest_opposing_zone_is_chosen_and_a_farther_one_cannot_be_substituted():
    near, far = zone("20026.25", "20030", "NEAR-"), zone("20100", "20104", "FAR-")
    r = geo(cand("LONG"), far, near)
    assert r.target_zone_version_id == near.zone_id
    assert r.planned_target_price == D("20026.00")  # far zone would give far more reward: never used
    short_near, short_far = zone("19970", "19975.75", "SN-"), zone("19800", "19804", "SF-")
    r = geo(cand("SHORT", "19994"), short_far, short_near)
    assert r.target_zone_version_id == short_near.zone_id


def test_a_zone_starting_exactly_at_the_planned_entry_is_not_a_target():
    at_entry, beyond = zone("20010.25", "20012", "AT-"), zone("20040", "20044", "BEYOND-")
    r = geo(cand("LONG"), at_entry, beyond)  # planned entry 20010.25: only zones STRICTLY above count
    assert r.target_zone_version_id == beyond.zone_id and r.qualified


def test_target_is_one_tick_before_the_opposing_boundary():
    assert geo(cand("LONG"), zone("20026.25", "20030", "T-")).planned_target_price == D("20026.00")  # lower - 0.25
    assert geo(cand("SHORT", "19994"), zone("19970", "19975.75", "T-")).planned_target_price == D("19976.00")  # upper + 0.25


def test_excluded_zones_are_never_targets():
    other_contract = zone("20026.25", "20030", "X-", contract="MNQZ4")
    other_date = zone("20026.25", "20030", "Y-", date=TRADE + dt.timedelta(days=1))
    superseded, expired = zone("20026.25", "20030", "S-"), zone("20026.25", "20030", "E-")
    superseded.supersede(T0945, "elsewhere")
    expired.expire(T0945)
    r = geo(cand("LONG"), other_contract, other_date, superseded, expired)
    assert "ROOM_TO_TARGET_UNAVAILABLE" in r.rejection_reasons


def test_a_missing_target_zone_makes_the_candidate_ineligible():
    r = geo(cand("LONG"))
    assert not r.qualified and r.rejection_reasons == ("ROOM_TO_TARGET_UNAVAILABLE",)
    assert r.planned_target_price is None


def test_nonpositive_risk_or_reward_is_rejected():
    r = geo(cand("LONG", close="19990", lo="20000"), zone("20026.25", "20030", "T-"))  # entry below the stop
    assert "NONPOSITIVE_RISK" in r.rejection_reasons
    r = geo(cand("LONG"), zone("20010.50", "20012", "T-"))  # target == planned entry
    assert {"NONPOSITIVE_REWARD", "TARGET_NOT_BEYOND_ENTRY"} <= set(r.rejection_reasons)


# =========================================================================== reward-to-risk


def test_exactly_1_50_r_qualifies_and_less_fails():
    exact = geo(cand("LONG"), zone("20026.25", "20030", "T-"))
    assert exact.planned_risk_points == D("10.50") and exact.planned_reward_points == D("15.75")
    assert exact.planned_gross_rr == Fraction(3, 2) and exact.qualified
    below = geo(cand("LONG"), zone("20026", "20030", "T-"))  # one tick less reward
    assert below.planned_gross_rr < Fraction(3, 2) and below.rejection_reasons == ("RR_BELOW_MINIMUM",)


def test_eligibility_failures_are_recorded_with_exact_reasons():
    blocked = ExecutionEligibility.from_mapping({**CLEAR.__dict__, "news_entry_protection": ControlState.BLOCKED})
    r = geo(cand("LONG"), zone("20026.25", "20030", "T-"), eligibility=blocked)
    assert r.rejection_reasons == ("BLOCKED:NEWS_ENTRY_PROTECTION",)
    late = evaluate_geometry(cand("LONG"), ny_time(TRADE, dt.time(11, 30)), [ORIGIN, zone("20026.25", "20030", "T-")], GP, CLEAR)
    assert "DECISION_TIME_OUTSIDE_ENTRY_WINDOW" in late.rejection_reasons
    c = cand("LONG")
    c.status = S.INVALIDATED_BY_DIRECTIONAL_CONFLICT
    assert "NOT_AN_ENTRY_CANDIDATE:INVALIDATED_BY_DIRECTIONAL_CONFLICT" in geo(c, zone("20026.25", "20030", "T-")).rejection_reasons


# =========================================================================== selection


def three_longs():
    target = zone("20040", "20044", "T-")
    a = geo(cand("LONG", "20010", key="A"), target)  # risk 10.50, reward 29.50
    b = geo(cand("LONG", "20012", key="B"), target)  # risk 12.50, reward 27.50 (lower R:R)
    c = geo(cand("LONG", "20014", key="C"), target)  # risk 14.50, reward 25.50 (lowest R:R)
    return a, b, c


def test_same_direction_candidates_rank_by_the_stated_criteria():
    a, b, c = three_longs()
    sel = select_candidate(T, [b, c, a], GP)
    assert sel.result is R.SELECTED and sel.selected.acceptance_id == a.candidate.acceptance_id
    assert a.candidate.status is S.USED_BY_ENTRY_CANDIDATE
    assert b.candidate.status is c.candidate.status is S.NOT_SELECTED_BY_GEOMETRY_RANKING  # D-028: terminal
    for loser in (b, c):
        with pytest.raises(ValueError):  # never reusable
            DailyDirectionBook.mark_used(loser.candidate)


def test_secondary_criterion_smaller_risk_wins_at_equal_rr():
    # Both exactly 2.00R: A risks 10.50 for 21.00, B risks 21.00 for 42.00.
    a = geo(cand("LONG", "20010", lo="20000", key="A"), zone("20031.50", "20034", "TA-"))
    b = geo(cand("LONG", "20010", lo="19989.50", key="B"), zone("20052.50", "20055", "TB-"))
    assert a.planned_gross_rr == b.planned_gross_rr == Fraction(2)
    assert (a.planned_risk_points, b.planned_risk_points) == (D("10.50"), D("21.00"))
    for order in ([a, b], [b, a]):
        for r in order:
            r.candidate.status = S.ENTRY_CANDIDATE
        assert select_candidate(T, order, GP).selected.acceptance_id == a.candidate.acceptance_id
    # D-028: there is no third criterion. Equal R:R and equal risk imply equal reward.


def test_ranking_has_exactly_two_criteria_and_rejects_any_other_list():
    assert trade_geometry.RANKING_CRITERIA == ("highest_planned_gross_rr", "smallest_planned_risk_points")
    assert SPEC["trade_geometry"]["parameters"]["candidate_ranking"] == list(trade_geometry.RANKING_CRITERIA)
    assert SPEC["trade_geometry"]["parameters"]["exact_tie_action"] == "NO_TRADE_SAME_DIRECTION_GEOMETRY_TIE"
    for bad in (
        ["highest_planned_gross_rr", "smallest_planned_risk_points", "greatest_planned_reward_points"],
        ["highest_planned_gross_rr", "greatest_planned_reward_points"],  # reordered to prefer reward
        ["smallest_planned_risk_points", "highest_planned_gross_rr"],
    ):
        spec = copy.deepcopy(SPEC)
        spec["trade_geometry"]["parameters"]["candidate_ranking"] = bad
        assert ("trade_geometry.parameters.candidate_ranking", "INVALID") in {(p.path, p.kind) for p in check_rule_freeze(spec).problems}
        with pytest.raises(ValueError):
            GeometryParams.from_spec(spec)
    spec = copy.deepcopy(SPEC)
    spec["trade_geometry"]["parameters"]["exact_tie_action"] = "NO_TRADE"
    with pytest.raises(ValueError):
        GeometryParams.from_spec(spec)


def test_ranking_is_independent_of_input_order():
    picks = set()
    for order in ((0, 1, 2), (2, 1, 0), (1, 2, 0)):
        results = three_longs()
        picks.add(select_candidate(T, [results[i] for i in order], GP).selected.acceptance_id)
    assert len(picks) == 1


def test_an_exact_two_criterion_tie_creates_no_trade_but_does_not_halt_the_date():
    target = zone("20040", "20044", "T-")
    one, two = geo(cand("LONG", key="A"), target), geo(cand("LONG", key="B"), target)  # identical geometry
    sel = select_candidate(T, [one, two], GP)
    assert sel.result is R.NO_TRADE_SAME_DIRECTION_GEOMETRY_TIE and sel.selected is None
    assert {one.candidate.status, two.candidate.status} == {S.NON_EXECUTABLE_GEOMETRY_TIE}
    book = DailyDirectionBook(TRADE, "MNQM4")
    assert not book.halted and book.new_entries_permitted  # a tie blocks only this timestamp
    later = geo(cand("LONG", key="C"), target)
    assert select_candidate(T, [later], GP).result is R.SELECTED
    with pytest.raises(ValueError):  # tied confirmations cannot be reused
        DailyDirectionBook.mark_used(one.candidate)


def test_a_selected_candidate_preserves_every_identifier_and_price():
    r = geo(cand("LONG"), zone("20026.25", "20030", "T-"))
    s = select_candidate(T, [r], GP).selected
    expected = {
        "candidate_id", "direction", "decision_timestamp", "confirmation_timestamp", "contract", "zone_version_id",
        "attempt_id", "acceptance_id", "confirmation_id", "target_zone_version_id", "planned_entry_price",
        "structural_invalidation_price", "planned_stop_price", "planned_target_price", "planned_risk_points",
        "planned_reward_points", "planned_gross_rr", "selection_rank_values", "specification_version", "configuration_hash",
    }
    assert expected <= {f.name for f in fields(s)}
    assert all(getattr(s, name) not in (None, "") for name in expected)
    assert s.status == "SELECTED_ENTRY_CANDIDATE" and s.configuration_hash == rule_spec_hash(SPEC)
    assert (s.planned_entry_price, s.planned_stop_price, s.planned_target_price) == (D("20010.25"), D("19999.75"), D("20026.00"))


def test_no_orders_or_position_sizes_are_created():
    names = {f.name for f in fields(trade_geometry.SelectedEntryCandidate)}
    assert not {n for n in names if any(w in n for w in ("quantity", "contracts", "size", "order", "fill"))}
    identifiers = {
        (n.id if isinstance(n, ast.Name) else n.attr).lower()
        for n in ast.walk(ast.parse(inspect.getsource(trade_geometry)))
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    assert not {i for i in identifiers if any(w in i for w in ("submit", "quantity", "contracts", "broker"))}


def test_end_to_end_from_a_real_confirmation():
    s = Session()
    s.feed(*HOLD)
    s.feed(20012.0, 20006.0, 20009.25)  # CONFIRMED_LONG_CONTINUATION at 10:30, close 20009.25
    t = s.sequence.events[-1].timestamp_utc
    (candidate,) = DailyDirectionBook(TRADE, "MNQM4").resolve(t, [s.sequence]).candidates
    target = zone("20100", "20104", "E2E-")
    r = evaluate_geometry(candidate, t, [s.engine.zone, target], GP, CLEAR)
    assert r.planned_entry_price == D("20009.50") and r.planned_stop_price == D("19999.75") and r.qualified
    assert select_candidate(t, [r], GP).selected.confirmation_id == s.sequence.confirmation_id


# =========================================================================== parameters


def test_all_numeric_trading_parameters_come_from_the_specification():
    spec = copy.deepcopy(SPEC)
    spec["trade_geometry"]["parameters"].update(minimum_planned_gross_rr="2.00", target_buffer_ticks=2, planned_entry_adverse_buffer_ticks=2)
    gp = GeometryParams.from_spec(spec)
    r = evaluate_geometry(cand("LONG"), T, [ORIGIN, zone("20026.25", "20030", "T-")], gp, CLEAR)
    assert r.planned_entry_price == D("20010.50") and r.planned_target_price == D("20025.75")
    assert "RR_BELOW_MINIMUM" in r.rejection_reasons
    literals = {
        n.value
        for n in ast.walk(ast.parse(inspect.getsource(trade_geometry)))
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
    }
    assert literals <= {0, 1}
    spec["trade_geometry"]["parameters"].update(candidate_ranking=["closest_zone"], exact_tie_action="PICK_FIRST", minimum_planned_gross_rr="-1")
    invalid = {p.path for p in check_rule_freeze(spec).problems if p.kind == "INVALID"}
    assert {
        "trade_geometry.parameters.candidate_ranking",
        "trade_geometry.parameters.exact_tie_action",
        "trade_geometry.parameters.minimum_planned_gross_rr",
    } <= invalid


# =========================================================================== typed eligibility (D-028)


def test_eligibility_is_typed_and_fails_closed():
    names = ExecutionEligibility.control_names()
    # Free-form strings, booleans and None are refused outright ("CLEAR" is not ControlState.CLEAR).
    for bad in ("CLEAR", False, True, None, 0):
        with pytest.raises(TypeError):
            ExecutionEligibility(**{**CLEAR.__dict__, "safety_halt": bad})
    with pytest.raises(TypeError):  # a missing key cannot default to safe
        ExecutionEligibility(**{n: ControlState.CLEAR for n in names[1:]})
    # Mapping input: missing or wrongly typed -> UNKNOWN, which blocks.
    partial = ExecutionEligibility.from_mapping({n: ControlState.CLEAR for n in names if n != "data_valid"} | {"news_blackout": "CLEAR"})
    assert not partial.permits_entry
    assert partial.blocking_reasons() == ("UNKNOWN:NEWS_BLACKOUT", "UNKNOWN:DATA_VALID")
    with pytest.raises(KeyError):
        ExecutionEligibility.from_mapping({"is_safe": ControlState.CLEAR})
    assert ExecutionEligibility.from_mapping({}).blocking_reasons() == tuple(f"UNKNOWN:{n.upper()}" for n in names)
    r = geo(cand("LONG"), zone("20026.25", "20030", "T-"), eligibility=partial)
    assert not r.qualified and "UNKNOWN:DATA_VALID" in r.rejection_reasons
    with pytest.raises(TypeError):  # the old free-form list can no longer be passed
        evaluate_geometry(cand("LONG"), T, [ORIGIN], GP, ("NEWS",))


def test_execution_eligibility_integration_blocks_execution_until_done():
    assert SPEC["execution_eligibility_integration"]["historical_signal_producers_status"] == "REQUIRED_BEFORE_EXECUTABLE"
    assert SPEC["execution_eligibility_integration"]["controls"] == list(ExecutionEligibility.control_names())
    assert "execution_eligibility_integration.historical_signal_producers_status" in check_rule_freeze(SPEC).unresolved_paths()
