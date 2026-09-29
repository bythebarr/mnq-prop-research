"""Round 15: staged readiness, exit completion, costs, slippage, data quality and trade accounting.

Hand-built fixtures, not market data. Base long trade: planned entry 20010.25,
stop 19999.75, target 20026.00 (see test_trade_geometry / test_protection).
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import inspect
import pkgutil
from decimal import Decimal
from fractions import Fraction

import pandas as pd
import pytest

import mnq_research
from test_confirmation import TRADE
from test_entry_order import AUTH, MS, SPEC, D, invalid_paths
from test_protection import ACCOUNT, FILL, PP, STOP, TARGET, active, close_flat, filled, bracket, minute, trade
from mnq_research import accounting, costs as costs_module, readiness
from mnq_research.accounting import FillLeg, LegType, TradeInputs, account_all_scenarios, account_trade, summarize
from mnq_research.confirmation import Side
from mnq_research.costs import CostModel, FillOrigin, OrderPurpose
from mnq_research.data_contracts import validate_bars
from mnq_research.data_quality import DateState, IntervalState, MinuteQuality as Q, decision_interval_state, open_position_flags, trading_date_state
from mnq_research.protection import ExitOutcome as X, ExitSource as E, FlatteningLeg, OrderAction, PriceInterval, PriceSource, first_exit, resolve_entry_minute
from mnq_research.readiness import STAGE_PREFIXES, Stage, broker_connectivity_permitted, check_stage, stage_hash, stage_of
from mnq_research.structural_levels import ny_time
from mnq_research.validation import REQUIRED_FIELDS, check_rule_freeze

COSTS = CostModel.from_spec(SPEC)
HASHES = {"specification_hash": "spec-h", "code_hash": "code-h", "data_hash": "data-h", "calendar_hash": "cal-h"}
IDS = {"zone_version_id": "Z1", "attempt_id": "1", "acceptance_id": "A1", "confirmation_id": "C1", "candidate_id": "K1"}


# =========================================================================== staged readiness


def signal_ready_spec() -> dict:
    """The draft with the SIGNAL_REPLAY implementation markers set (as a later round will) and SIGNAL_REPLAY approved."""
    spec = copy.deepcopy(SPEC)
    for path in ("execution_eligibility_integration.historical_signal_producers_status",
                 "research_pipeline.historical_data_ingestion_status",
                 "research_pipeline.historical_calendar_producers_status",
                 "research_pipeline.deterministic_replay_wiring_status"):
        section, name = path.split(".")
        spec[section][name] = "TEST_FIXTURE_IMPLEMENTED"
    approve(spec, Stage.SIGNAL_REPLAY)
    return spec


def approve(spec: dict, stage: Stage) -> None:
    spec["stage_approvals"][stage.value] = {
        "approved": True, "approved_by": "pytest", "approved_at_utc": "2026-09-29T12:00:00Z",
        "approved_stage_hash": stage_hash(spec, stage),
    }


def test_staged_gates_allow_research_while_later_fields_remain_unresolved():
    spec = signal_ready_spec()
    assert check_stage(spec, Stage.SIGNAL_REPLAY).is_ready
    assert not check_rule_freeze(spec).is_executable  # the old all-or-nothing gate still says no
    for later in (Stage.ONE_CONTRACT_BACKTEST, Stage.PROP_MONTE_CARLO, Stage.PAPER_FORWARD, Stage.LIVE_CONSIDERATION):
        assert not check_stage(spec, later).is_ready
    # Answering a later-stage field never invalidates the earlier approval.
    spec["prop_account_rules"]["profit_target_usd"] = 3000
    spec["protective_orders"]["deployment"]["capability_verification_status"] = "TEST_FIXTURE_VERIFIED"
    assert check_stage(spec, Stage.SIGNAL_REPLAY).is_ready
    assert not broker_connectivity_permitted(spec)


def test_missing_signal_stage_fields_block_signal_replay():
    spec = signal_ready_spec()
    spec["structural_levels"]["level_types"] = "TBD"
    paths = {p.path for p in check_stage(spec, Stage.SIGNAL_REPLAY).problems}
    assert "structural_levels.level_types" in paths
    edited = signal_ready_spec()
    edited["confirmation"]["max_bars_after_acceptance"] = 7  # an edit after approval
    assert "stage_approvals.SIGNAL_REPLAY.approved_stage_hash" in {p.path for p in check_stage(edited, Stage.SIGNAL_REPLAY).problems}
    assert not check_stage(copy.deepcopy(SPEC), Stage.SIGNAL_REPLAY).is_ready  # the pipeline itself is not built yet


def test_missing_cost_and_exit_fields_block_only_the_one_contract_backtest():
    spec = signal_ready_spec()
    spec["commissions"]["round_turn_per_contract_usd"] = "TBD"
    spec["position_management"]["normal_flatten_time"] = "TBD"
    assert check_stage(spec, Stage.SIGNAL_REPLAY).is_ready
    one = {p.path for p in check_stage(spec, Stage.ONE_CONTRACT_BACKTEST).problems}
    assert {"commissions.round_turn_per_contract_usd", "position_management.normal_flatten_time",
            "commissions.source_archive_status"} <= one
    assert "prop_account_rules.profit_target_usd" not in one  # prop items do not block the backtest
    assert "protective_orders.deployment.capability_verification_status" not in one


def test_every_required_field_declares_its_stage():
    undeclared = [p for p in REQUIRED_FIELDS if not any(p == k or p.startswith(k + ".") for k in STAGE_PREFIXES)]
    assert undeclared == []
    assert stage_of("an_unknown_section.field") is Stage.SIGNAL_REPLAY  # fails closed to the earliest stage
    assert stage_of("position_management.risk_per_trade.status") is Stage.PROP_MONTE_CARLO
    assert stage_of("protective_orders.deployment.capability_verification_status") is Stage.PAPER_FORWARD
    assert stage_of("protective_orders.parameters") is Stage.ONE_CONTRACT_BACKTEST
    assert stage_of("trade_geometry.candidate_selection") is Stage.SIGNAL_REPLAY


# =========================================================================== setup, indicators, normal exit


def test_setup_fields_point_to_the_frozen_sequence():
    setup = SPEC["setup"]
    assert setup["name"] == "MNQ_OBJECTIVE_STRUCTURE_CONTINUATION_B0"
    assert "ACCEPTED_ABOVE" in setup["long_definition"] and "ACCEPTED_BELOW" in setup["short_definition"]
    assert all(section in SPEC for section in setup["authoritative_sections"])
    assert "1.50" not in setup["long_definition"]  # parameters are not duplicated into the summary


def test_ema_and_vwap_are_false_and_unused():
    assert SPEC["ema"]["included"] is False and SPEC["vwap"]["included"] is False
    spec = copy.deepcopy(SPEC)
    spec["ema"]["included"] = True
    assert "ema.included" in invalid_paths(spec)
    for info in pkgutil.iter_modules(mnq_research.__path__):
        if info.name in ("validation", "readiness"):
            continue  # they only name the fields in order to forbid them
        tree = ast.parse(inspect.getsource(__import__(f"mnq_research.{info.name}", fromlist=["x"])))
        names = {(n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else n.name).lower()
                 for n in ast.walk(tree) if isinstance(n, (ast.Name, ast.Attribute, ast.FunctionDef))}
        tokens = {t for n in names for t in n.split("_")}
        assert not tokens & {"ema", "vwap"}, info.name


def test_normal_flatten_occurs_at_12_00_new_york():
    assert PP.normal_flatten_time == dt.time(12, 0)
    _, _, b = active()
    b.apply_normal_time_exit(ny_time(TRADE, dt.time(11, 59, 59)))
    assert b.flatten_reason is None
    noon = ny_time(TRADE, dt.time(12, 0))
    b.apply_normal_time_exit(noon)
    assert b.flatten_reason is X.NORMAL_TIME_EXIT and b.target.pending_reason.value == "CANCEL"
    assert b.stop.status.value == "CONFIRMED"  # the stop protects until the exit resolves
    b.on_cancel_confirmed(noon + 10 * MS, b.target.kind)
    b.on_exit_fill(noon + 100 * MS, E.FLATTEN, 1, D("20015.00"), AUTH)
    close_flat(b, noon + 200 * MS, b.stop.kind)
    assert b.exit_outcome is X.NORMAL_TIME_EXIT and b.final_flattening_leg is FlatteningLeg.NORMAL_TIME_EXIT
    assert SPEC["position_management"]["normal_time_exit_order_type"] == "MARKET"


def test_no_duplicate_flatten_after_an_earlier_exit():
    noon = ny_time(TRADE, dt.time(12, 0))
    _, _, b = active()
    b.on_exit_fill(FILL + 5000 * MS, E.STOP, 1, D("19999.50"), AUTH)
    b.apply_normal_time_exit(noon)
    assert b.flatten_reason is None and "FLATTEN_NOT_DUPLICATED" in [e[1] for e in b.events]
    _, _, n = active()
    n.begin_flatten(FILL + 60000 * MS, X.SCHEDULED_NEWS_FLATTEN)
    n.apply_normal_time_exit(noon)
    assert n.flatten_reason is X.SCHEDULED_NEWS_FLATTEN and [e[1] for e in n.events].count("FLATTEN_REQUESTED") == 1
    _, _, s = active()
    s.on_component_failure(FILL + 500 * MS, s.stop.kind, "STATE_UNKNOWN")  # safety exit already under way
    s.apply_normal_time_exit(noon)
    assert s.flatten_reason is X.PROTECTION_FAILURE_FLATTEN


def test_structural_invalidation_uses_the_stop_market_immediately():
    assert SPEC["structural_invalidation"]["action"] == "EXIT_VIA_PROTECTIVE_STOP_MARKET"
    trades = [trade(1500, "20003.00"), trade(1750, "19999.75")]  # mid-bar trade, long before any 5-minute close
    d = first_exit(Side.LONG, STOP, TARGET, trades, PP)
    assert d.source is E.STOP and d.trigger_utc == FILL + 1750 * MS
    _, _, b = active()
    b.on_exit_fill(d.trigger_utc, E.STOP, 1, D("19999.25"), AUTH)
    close_flat(b, d.trigger_utc + 100 * MS, b.target.kind)
    assert b.exit_outcome is X.STOP_FILLED
    assert SPEC["daily_limits"]["max_losing_trades_per_day"] == "NOT_APPLICABLE_BECAUSE_MAX_FILLED_ENTRIES_IS_ONE"


def test_final_flatten_labels_remain_specific():
    expected = {X.SESSION_EMERGENCY_FLATTEN: "SESSION_BACKSTOP", X.SCHEDULED_NEWS_FLATTEN: "NEWS_FLATTEN",
                X.MANUAL_SAFETY_FLATTEN: "MANUAL_SAFETY", X.NORMAL_TIME_EXIT: "NORMAL_TIME_EXIT"}
    for outcome, label in expected.items():
        _, _, b = active()
        b.begin_flatten(FILL + 60000 * MS, outcome)
        b.on_cancel_confirmed(FILL + 60010 * MS, b.target.kind)
        b.on_exit_fill(FILL + 60100 * MS, E.FLATTEN, 1, D("20015.00"), AUTH)
        close_flat(b, FILL + 60200 * MS, b.stop.kind)
        assert b.final_flattening_leg.value == label
    _, order = filled(price="19999.75")
    e = bracket(order)
    e.dispatch(FILL)
    e.on_exit_fill(FILL + 300 * MS, E.FLATTEN, 1, D("19999.25"), AUTH)
    e.on_position_report(FILL + 400 * MS, 0, None, "MNQM4", ACCOUNT)
    assert e.final_flattening_leg.value == "ENTRY_INVALIDATION"
    assert "EMERGENCY" not in {leg.value for leg in FlatteningLeg}
    assert SPEC["protective_orders"]["final_flattening_legs"] == [leg.value for leg in FlatteningLeg]


def test_scaling_remains_none():
    assert SPEC["position_management"]["scaling_in_out"] == "NONE"
    spec = copy.deepcopy(SPEC)
    spec["position_management"]["scaling_in_out"] = "SCALE_OUT_HALF_AT_1R"
    assert "position_management.scaling_in_out" in invalid_paths(spec)


# =========================================================================== costs and slippage


def test_commission_is_0_91_per_side_per_contract():
    assert COSTS.commission_per_side_usd == D("0.91") and COSTS.commission_round_trip_usd == D("1.82")
    assert COSTS.commission_usd(2, "BASE") == D("1.82")
    assert COSTS.commission_usd(2, "COMMISSION_STRESS_125") == D("2.275")
    assert COSTS.commission_usd(2, "COMMISSION_STRESS_150") == D("2.73")
    spec = copy.deepcopy(SPEC)
    spec["commissions"]["per_side_per_contract_usd"] = "0.90"  # no longer half of 1.82
    assert "commissions/slippage" in invalid_paths(spec)


def test_partial_quantities_receive_correct_costs():
    inputs = long_trade(entries=((1, "20010.25"), (2, "20010.50")), exits=((1, "20026.00", OrderPurpose.TARGET_LIMIT),
                                                                          (2, "20026.00", OrderPurpose.TARGET_LIMIT)))
    rec = account_trade(inputs, COSTS, "BASE_SLIPPAGE", "BASE")
    assert rec["commissions_usd"] == str(Fraction(D("0.91")) * 6)  # 3 entered + 3 exited sides
    assert rec["estimated_round_trip_commission_usd"] == str(Fraction(D("1.82")) * 3)


def test_slippage_is_adverse_for_every_side_and_type_and_limit_targets_get_none():
    for purpose in OrderPurpose:
        ticks = SPEC["slippage"]["baseline_ticks"][purpose.value]
        for scenario, mult in (("BASE_SLIPPAGE", 1), ("DOUBLE_SLIPPAGE", 2), ("TRIPLE_SLIPPAGE", 3)):
            buy = COSTS.fill_price(D("20000"), OrderAction.BUY, purpose, scenario, FillOrigin.MODELED)
            sell = COSTS.fill_price(D("20000"), OrderAction.SELL, purpose, scenario, FillOrigin.MODELED)
            assert buy - D("20000") == D("20000") - sell == ticks * mult * D("0.25")
            if purpose is OrderPurpose.TARGET_LIMIT:
                assert buy == sell == D("20000")
            else:
                assert buy > D("20000") > sell
    assert SPEC["slippage"]["baseline_ticks"]["protective_stop_market"] == 2
    assert SPEC["slippage"]["baseline_ticks"]["session_backstop_market"] == 3


def test_slippage_is_never_double_counted():
    for scenario in ("BASE_SLIPPAGE", "TRIPLE_SLIPPAGE"):
        assert COSTS.fill_price(D("20011.00"), OrderAction.BUY, OrderPurpose.ENTRY_MARKET, scenario, FillOrigin.ACTUAL_RECORDED) == D("20011.00")
    rec = account_trade(long_trade(), COSTS, "BASE_SLIPPAGE", "BASE")
    assert Fraction(rec["net_pnl_usd"]) == Fraction(rec["gross_pnl_usd"]) - Fraction(rec["commissions_usd"])  # no extra slippage line


def test_stress_multipliers_do_not_alter_signals():
    inputs = long_trade()
    before = copy.deepcopy(inputs)
    records = account_all_scenarios(inputs, COSTS)
    assert len(records) == 9 and inputs == before
    ids = {(tuple(r["identifiers"].items()), tuple(l["timestamp_utc"] for l in r["exit_legs"]), r["exit_reason"]) for r in records.values()}
    assert len(ids) == 1  # identical signals and timing in every scenario
    nets = [Fraction(records[(s, "BASE")]["net_pnl_usd"]) for s in ("BASE_SLIPPAGE", "DOUBLE_SLIPPAGE", "TRIPLE_SLIPPAGE")]
    assert nets[0] > nets[1] > nets[2]


# =========================================================================== same-minute ambiguity


def test_same_minute_ambiguity_assumes_stop_first_and_never_awards_a_target():
    bar = minute("20027.00", "19999.00")
    d = resolve_entry_minute(Side.LONG, FILL, STOP, TARGET, bar, PP)
    assert d.source is E.STOP and {"SAME_MINUTE_ENTRY_EXIT_AMBIGUITY", "CONSERVATIVE_STOP_FIRST"} <= set(d.flags)
    assert resolve_entry_minute(Side.LONG, FILL, STOP, TARGET, minute("20027.00", "20005.00"), PP) is None


def test_pre_entry_events_cannot_trigger_an_exit():
    finer = [trade(-500, "19999.00"), trade(200, "20026.25")]  # a stop-level trade BEFORE the fill, then a target trade-through
    d = resolve_entry_minute(Side.LONG, FILL, STOP, TARGET, minute("20027.00", "19999.00"), PP, finer)
    assert d.source is E.TARGET and d.trigger_reference == "trade@200"


# =========================================================================== data quality


def test_flat_state_gaps_reset_and_invalidate():
    assert decision_interval_state([Q.PRESENT_VALID, Q.VERIFIED_NO_TRADE_MINUTE] * 2) is IntervalState.ELIGIBLE
    for bad in (Q.KNOWN_DATA_OUTAGE, Q.UNEXPLAINED_MISSING_MINUTE, Q.REJECTED_BAD_DATA):
        assert decision_interval_state([Q.PRESENT_VALID, bad]) is IntervalState.INELIGIBLE_FRESH_SETUP_REQUIRED
    assert decision_interval_state(["PRESENT_VALID"]) is IntervalState.INELIGIBLE_FRESH_SETUP_REQUIRED  # untyped fails closed
    assert decision_interval_state([]) is IntervalState.INELIGIBLE_FRESH_SETUP_REQUIRED
    assert trading_date_state(True, True) is DateState.ELIGIBLE
    for args in ((False, True), (True, False), (None, True), ("yes", True)):
        assert trading_date_state(*args) is DateState.INELIGIBLE_ENTIRE_DATE
    assert SPEC["missing_data"]["max_tolerated_gap_minutes"] == 0


def gap_trade() -> TradeInputs:
    return long_trade(exits=(), flags=open_position_flags([Q.PRESENT_VALID, Q.KNOWN_DATA_OUTAGE]), flat=None)


def test_open_position_gaps_remain_visible_and_unscorable():
    rec = account_trade(gap_trade(), COSTS, "BASE_SLIPPAGE", "BASE")
    assert rec["scorable"] is False and rec["net_pnl_usd"] is None and rec["gross_pnl_usd"] is None
    assert "OPEN_POSITION_DATA_GAP_UNRESOLVED" in rec["data_quality_flags"]
    assert rec["entry_legs"] and rec["stop"] == "19999.75" and rec["target"] == "20026.00"  # kept, not dropped
    assert rec["commissions_usd"] == "91/100"  # only the entered side has actually been charged
    with pytest.raises(ValueError):  # an unflagged trade without exits is never silently accepted
        account_trade(long_trade(exits=()), COSTS, "BASE_SLIPPAGE", "BASE")


def test_gap_stress_reporting_assigns_the_labelled_conservative_loss():
    gap = account_trade(gap_trade(), COSTS, "BASE_SLIPPAGE", "BASE")
    assert Fraction(gap["gap_stress_net_pnl_usd"]) == -Fraction(gap["actual_initial_risk_usd"])
    assert gap["gap_stress_label"] == "GAP_STRESS_FULL_LOSS_ASSUMPTION_NOT_PRIMARY"
    win = account_trade(long_trade(), COSTS, "BASE_SLIPPAGE", "BASE")
    summary = summarize([win, gap])
    assert summary["primary_trades"] == 1 and summary["unscorable_open_position_gaps"] == 1
    assert Fraction(summary["primary_net_pnl_usd"]) == Fraction(win["net_pnl_usd"])  # the stress never enters the primary
    assert Fraction(summary["gap_stress_net_pnl_usd"]) == Fraction(win["net_pnl_usd"]) + Fraction(gap["gap_stress_net_pnl_usd"])


def test_bad_data_are_never_silently_repaired(clean_bars):
    bars = clean_bars.copy()
    bars.loc[bars.index[5], "high"] = bars.loc[bars.index[5], "low"] - 1  # impossible OHLC
    bars.loc[bars.index[6], "close"] = bars.loc[bars.index[6], "close"] + 0.1  # off-tick
    snapshot = bars.copy()
    report = validate_bars(bars)
    assert not report.is_valid
    pd.testing.assert_frame_equal(bars, snapshot)  # nothing was altered
    assert decision_interval_state([Q.REJECTED_BAD_DATA]) is IntervalState.INELIGIBLE_FRESH_SETUP_REQUIRED


# =========================================================================== P&L, R, excursions


def leg(ms: int, kind: LegType, qty: int, price: str, purpose: OrderPurpose, origin=FillOrigin.MODELED, exit_type=None) -> FillLeg:
    return FillLeg(FILL + ms * MS, kind, qty, D(price), purpose, origin, f"ORD-{kind.value}-{ms}", exit_type)


def long_trade(entries=((1, "20010.50"),), exits=((1, "20026.00", OrderPurpose.TARGET_LIMIT),), side=Side.LONG,
               stop="19999.75", target="20026.00", observations=(), flags=(), flat="default") -> TradeInputs:
    entry_legs = tuple(leg(i, LegType.ENTRY, q, p, OrderPurpose.ENTRY_MARKET) for i, (q, p) in enumerate(entries))
    exit_legs = tuple(leg(60000 + i, LegType.EXIT, q, p, purpose, exit_type=purpose.value) for i, (q, p, purpose) in enumerate(exits))
    flat_utc = FILL + 61000 * MS if flat == "default" else flat
    return TradeInputs(TRADE, "MNQM4", side, IDS, entry_legs, exit_legs, D(stop), D(target), D("10.50"), 1,
                       "TARGET_FILLED" if exits else None, "TARGET" if exits else None, tuple(observations), flat_utc, tuple(flags), HASHES)


def test_long_and_short_pnl_signs_are_correct():
    rec = account_trade(long_trade(), COSTS, "BASE_SLIPPAGE", "BASE")
    assert rec["actual_average_entry"] == "80043/4"  # 20010.50 + 1 adverse tick = 20010.75
    assert rec["gross_points"] == "61/4" and rec["gross_pnl_usd"] == "61/2"  # 15.25 points x $2 = 30.50
    short = long_trade(entries=((1, "19993.50"),), exits=((1, "19976.00", OrderPurpose.TARGET_LIMIT),), side=Side.SHORT,
                       stop="20004.25", target="19976.00")
    srec = account_trade(short, COSTS, "BASE_SLIPPAGE", "BASE")
    assert srec["actual_average_entry"] == "79973/4"  # 19993.25: a sell fills lower
    assert srec["gross_pnl_usd"] == "69/2"  # (19993.25 - 19976.00) x 2 = 34.50
    loss = account_trade(long_trade(exits=((1, "19999.75", OrderPurpose.PROTECTIVE_STOP_MARKET),)), COSTS, "BASE_SLIPPAGE", "BASE")
    assert Fraction(loss["gross_pnl_usd"]) == (Fraction(D("19999.25")) - Fraction(D("20010.75"))) * 2  # stop sells 2 ticks lower


def test_net_pnl_subtracts_costs_once_and_planned_and_actual_r_stay_distinct():
    rec = account_trade(long_trade(), COSTS, "BASE_SLIPPAGE", "BASE")
    assert Fraction(rec["net_pnl_usd"]) == Fraction(D("30.50")) - Fraction(D("1.82"))
    assert rec["planned_risk_points"] == "10.50" and Fraction(rec["planned_risk_usd_before_costs"]) == 21
    assert Fraction(rec["actual_price_risk_usd"]) == 22  # |20010.75 - 19999.75| x 2
    assert Fraction(rec["modeled_stop_slippage_risk_usd"]) == 1  # 2 ticks x $0.50
    assert Fraction(rec["estimated_round_trip_commission_usd"]) == Fraction(D("1.82"))
    assert Fraction(rec["actual_initial_risk_usd"]) == Fraction(D("24.82"))
    assert Fraction(rec["result_r_net"]) == Fraction(D("28.68")) / Fraction(D("24.82"))
    assert Fraction(rec["result_r_price_only"]) == Fraction(D("30.50")) / 22
    assert Fraction(rec["result_r_net"]) != Fraction(rec["result_r_price_only"])


def obs(ms: int, high: str, low: str | None = None) -> PriceInterval:
    return PriceInterval(FILL + ms * MS, D(high), D(low or high), PriceSource.AUTHORITATIVE_TRADES, f"obs@{ms}")


def test_mae_mfe_directions_timing_and_start_after_entry():
    observations = (obs(-1000, "20100.00", "19900.00"),  # before the fill: must be ignored
                    obs(1000, "20008.00"), obs(5000, "20020.00"), obs(9000, "20004.00"), obs(20000, "20026.25"))
    rec = account_trade(long_trade(observations=observations), COSTS, "BASE_SLIPPAGE", "BASE")
    assert Fraction(rec["mfe_points"]) == Fraction(D("20026.25")) - Fraction(D("20010.75"))  # not clamped to the target
    assert Fraction(rec["mae_points"]) == Fraction(D("20010.75")) - Fraction(D("20004.00"))
    assert rec["time_to_mfe"] == str(pd.Timedelta(seconds=20)) and rec["time_to_mae"] == str(pd.Timedelta(seconds=9))
    assert rec["time_in_trade"] == str(pd.Timedelta(seconds=61))
    assert Fraction(rec["mae_r"]) == Fraction(D("6.75")) / 11
    short_obs = (obs(1000, "19995.00"), obs(2000, "19980.00"))
    short = long_trade(entries=((1, "19993.50"),), exits=((1, "19976.00", OrderPurpose.TARGET_LIMIT),), side=Side.SHORT,
                       stop="20004.25", target="19976.00", observations=short_obs)
    srec = account_trade(short, COSTS, "BASE_SLIPPAGE", "BASE")
    assert Fraction(srec["mfe_points"]) == Fraction(D("19993.25")) - Fraction(D("19980.00"))
    assert Fraction(srec["mae_points"]) == Fraction(D("19995.00")) - Fraction(D("19993.25"))
    bars = account_trade(long_trade(observations=(obs(1000, "20020.00", "20000.00"),)), COSTS, "BASE_SLIPPAGE", "BASE")
    assert "EXCURSION_TIMING_APPROXIMATE" in bars["data_quality_flags"]


def test_every_trade_preserves_reproducibility_hashes():
    rec = account_trade(long_trade(), COSTS, "BASE_SLIPPAGE", "BASE")
    assert rec["hashes"] == HASHES and rec["identifiers"] == IDS
    for field in SPEC["trade_accounting"]["record_fields"]:
        assert field in rec.values or field in ("identifiers", "entry_legs", "exit_legs", "stop", "target", "hashes"), field
    missing = long_trade()
    object.__setattr__(missing, "hashes", {k: v for k, v in HASHES.items() if k != "data_hash"})
    with pytest.raises(ValueError):
        account_trade(missing, COSTS, "BASE_SLIPPAGE", "BASE")


def test_cost_and_accounting_parameters_come_from_the_specification():
    spec = copy.deepcopy(SPEC)
    spec["commissions"].update(round_turn_per_contract_usd="2.00", per_side_per_contract_usd="1.00")
    spec["slippage"]["baseline_ticks"]["entry_market"] = 2
    cm = CostModel.from_spec(spec)
    assert cm.commission_usd(2, "BASE") == D("2.00")
    assert cm.fill_price(D("20000"), OrderAction.BUY, OrderPurpose.ENTRY_MARKET, "BASE_SLIPPAGE", FillOrigin.MODELED) == D("20000.50")
    for module in (costs_module, accounting):
        literals = {n.value for n in ast.walk(ast.parse(inspect.getsource(module)))
                    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)}
        assert literals <= {0, 1}, module.__name__
    spec["slippage"]["baseline_ticks"]["target_limit"] = 1
    with pytest.raises(ValueError):
        CostModel.from_spec(spec)
