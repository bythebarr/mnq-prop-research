"""Round 11 direction rules and D-026 replay/executability (owner-specified test list).

Hand-built fixtures for logic tests, not market data.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT, RULE_FREEZE_PATH
from test_confirmation import HOLD, PARAMS, STATE_PARAMS, TRADE, FIVE, Session
from mnq_research import direction as direction_module
from mnq_research.config import load_mapping
from mnq_research.confirmation import ConfirmationOutcome as O, Side, initialize_engines
from mnq_research.direction import CandidateStatus as S, DailyDirectionBook, LinkageError, ResolutionResult as R
from mnq_research.level_states import DecisionBar, ZoneEventType
from mnq_research.structural_levels import DailyLevelSet, LevelType, StructuralLevel, ny_time
from mnq_research.validation import check_rule_freeze

SPEC = load_mapping(RULE_FREEZE_PATH)
CONTINUE_LONG = (20012.0, 20006.0, 20009.25)
SHORT_HOLD = (19998.0, 19993.0, 19996.0)
CONTINUE_SHORT = (19995.0, 19990.0, 19992.75)


def book() -> DailyDirectionBook:
    return DailyDirectionBook(TRADE, "MNQM4")


def confirmed(side: str = "LONG", start: dt.time = dt.time(10, 0)) -> Session:
    s = Session(side, start)
    if side == "LONG":
        s.feed(*HOLD)
        s.feed(*CONTINUE_LONG)
    else:
        s.feed(*SHORT_HOLD)
        s.feed(*CONTINUE_SHORT)
    assert s.sequence.outcome is O.CONFIRMED
    return s


def confirmation_time(s: Session):
    return s.sequence.events[-1].timestamp_utc


# =========================================================================== direction is event-derived


def test_long_direction_comes_only_from_a_confirmed_long_continuation():
    s = confirmed("LONG")
    res = book().resolve(confirmation_time(s), [s.sequence])
    assert res.result is R.CANDIDATES
    (c,) = res.candidates
    assert c.side is Side.LONG and c.status is S.ENTRY_CANDIDATE
    assert (c.trading_date, c.contract, c.zone_version_id, c.attempt_id, c.acceptance_id) == (
        TRADE, "MNQM4", s.engine.zone.zone_id, s.sequence.attempt_id, s.sequence.acceptance_id
    )


def test_short_direction_comes_only_from_a_confirmed_short_continuation():
    s = confirmed("SHORT")
    (c,) = book().resolve(confirmation_time(s), [s.sequence]).candidates
    assert c.side is Side.SHORT


def test_no_direction_without_a_confirmation_and_no_external_bias():
    s = Session()  # accepted, not confirmed
    s.feed(*HOLD)  # a hold alone is not a confirmation
    res = book().resolve(s.sequence.events[-1].timestamp_utc, [s.sequence])
    assert res.result is R.NO_CONFIRMATIONS and not res.candidates
    # The direction layer takes only confirmation sequences: no indicator, bias or location inputs.
    params = list(inspect.signature(DailyDirectionBook.resolve).parameters)
    assert params == ["self", "decision_time_utc", "sequences"]
    names = {
        n.id.lower() if isinstance(n, ast.Name) else n.attr.lower()
        for n in ast.walk(ast.parse(inspect.getsource(direction_module)))
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    assert not {w for w in names for bad in ("ema", "vwap", "bias", "swing", "trend", "gap_") if bad in w}


def test_no_additional_swing_filter_is_active():
    assert SPEC["market_structure"]["definition"] == "ACCEPTANCE_PULLBACK_HOLD_CONTINUATION"
    assert SPEC["market_structure"]["additional_swing_structure_filter"] == "NOT_APPLICABLE"
    spec = load_mapping(RULE_FREEZE_PATH)
    spec["market_structure"]["additional_swing_structure_filter"] = "HIGHER_HIGH_HIGHER_LOW"
    assert any(p.path == "market_structure.additional_swing_structure_filter" and p.kind == "INVALID" for p in check_rule_freeze(spec).problems)
    for module in (PROJECT_ROOT / "src" / "mnq_research").glob("*.py"):
        tree = ast.parse(module.read_text())
        defs = [n.name.lower() for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        assert not [d for d in defs if any(w in d for w in ("swing", "pivot", "zigzag", "fractal"))], module.name


# =========================================================================== conflicts


def test_same_time_opposite_confirmations_generate_no_trade_and_halt_the_day():
    long_s, short_s = confirmed("LONG"), confirmed("SHORT")
    t = confirmation_time(long_s)
    assert t == confirmation_time(short_s)
    b = book()
    res = b.resolve(t, [long_s.sequence, short_s.sequence])
    assert res.result is R.NO_TRADE_DIRECTIONAL_CONFLICT
    assert {c.status for c in res.candidates} == {S.INVALIDATED_BY_DIRECTIONAL_CONFLICT}
    assert {c.side for c in res.candidates} == {Side.LONG, Side.SHORT}  # preserved for diagnostics
    assert b.halted and not b.new_entries_permitted and "DIRECTIONAL_CONFLICT" in b.halt_reason
    # No fresh setup restores eligibility that day.
    later = confirmed("LONG", dt.time(10, 30))
    res_later = b.resolve(confirmation_time(later), [later.sequence])
    assert res_later.result is R.NO_TRADE_DAILY_HALT
    assert res_later.candidates[0].status is S.NON_EXECUTABLE_DAILY_HALT
    assert len(b.history) == 2  # every resolution is kept


def test_conflict_resolution_is_independent_of_processing_order():
    long_s, short_s = confirmed("LONG"), confirmed("SHORT")
    t = confirmation_time(long_s)
    a = book().resolve(t, [long_s.sequence, short_s.sequence])
    long_s, short_s = confirmed("LONG"), confirmed("SHORT")
    b = book().resolve(t, [short_s.sequence, long_s.sequence])
    assert a.result is b.result is R.NO_TRADE_DIRECTIONAL_CONFLICT
    assert [(c.side, c.status) for c in a.candidates] == [(c.side, c.status) for c in b.candidates]


def test_multiple_same_direction_candidates_remain_unresolved():
    one, two = confirmed("LONG"), confirmed("LONG")
    res = book().resolve(confirmation_time(one), [one.sequence, two.sequence])
    assert res.result is R.CANDIDATES and len(res.candidates) == 2
    assert all(c.requires_selection and c.status is S.ENTRY_CANDIDATE for c in res.candidates)
    swapped = book().resolve(confirmation_time(one), [two.sequence, one.sequence])
    assert [c.acceptance_id for c in swapped.candidates] == [c.acceptance_id for c in res.candidates]


def test_a_used_candidate_becomes_non_executable():
    s = confirmed("LONG")
    (c,) = book().resolve(confirmation_time(s), [s.sequence]).candidates
    DailyDirectionBook.mark_used(c)
    assert c.status is S.USED_BY_ENTRY_CANDIDATE
    with pytest.raises(ValueError):
        DailyDirectionBook.mark_used(c)


# =========================================================================== linkage


def test_every_event_linkage_uses_matching_date_contract_and_identifiers():
    s = confirmed("LONG")
    t = confirmation_time(s)
    with pytest.raises(LinkageError):
        DailyDirectionBook(TRADE, "MNQZ4").resolve(t, [s.sequence])  # wrong contract
    with pytest.raises(LinkageError):
        DailyDirectionBook(TRADE + dt.timedelta(days=1), "MNQM4").resolve(t, [s.sequence])  # wrong date
    s.sequence.acceptance_id = "forged:ACC9"
    with pytest.raises(LinkageError):
        book().resolve(t, [s.sequence])  # identifiers no longer match the zone's attempt/acceptance


# =========================================================================== D-026 replay and executability


def test_pre_0945_bars_consume_the_normal_six_bar_lifetime():
    s = Session(start=dt.time(9, 10))  # acceptance closes 09:30
    assert s.acceptance_time == ny_time(TRADE, dt.time(9, 30))
    for _ in range(3):
        s.feed(20006.0, 20004.25, 20004.25)  # neutral bars close 09:35, 09:40, 09:45
    assert s.sequence.hold is None
    s.feed(*HOLD)  # bar 4 (09:50)
    s.feed(*HOLD)  # bar 5 fails continuation -> the clock was NOT reset at 09:45
    assert s.sequence.outcome is O.FAILED


def test_a_pre_0945_confirmation_is_non_executable_and_not_carried_forward():
    s = confirmed("LONG", dt.time(9, 10))  # acceptance 09:30, hold 09:35, confirmation 09:40
    t = confirmation_time(s)
    assert t == ny_time(TRADE, dt.time(9, 40))
    b = book()
    res = b.resolve(t, [s.sequence])
    assert res.result is R.NO_TRADE_OUTSIDE_ENTRY_WINDOW
    assert res.candidates[0].status is S.NON_EXECUTABLE_BEFORE_WINDOW
    assert b.resolve(ny_time(TRADE, dt.time(9, 45)), [s.sequence]).result is R.NO_CONFIRMATIONS  # not waiting


def test_a_confirmation_exactly_at_0945_may_create_a_candidate():
    s = confirmed("LONG", dt.time(9, 15))  # acceptance 09:35, hold 09:40, confirmation 09:45
    t = confirmation_time(s)
    assert t == ny_time(TRADE, dt.time(9, 45))
    (c,) = book().resolve(t, [s.sequence]).candidates
    assert c.status is S.ENTRY_CANDIDATE


def test_a_confirmation_closing_at_1130_is_invalidated():
    s = Session(start=dt.time(11, 0))
    s.feed(*HOLD)
    s.feed(*CONTINUE_LONG)  # closes 11:30
    assert s.sequence.outcome is O.INVALIDATED_BY_CUTOFF
    assert book().resolve(ny_time(TRADE, dt.time(11, 30)), [s.sequence]).result is R.NO_CONFIRMATIONS


def _level(price, kind, at, window):
    return StructuralLevel(kind, TRADE, "MNQM4", window, None, None, at, price=price)


def _daily(or_high: float) -> DailyLevelSet:
    t9, t930, t945 = (ny_time(TRADE, dt.time(9, m)) for m in (0, 30, 45))
    levels = (
        _level(20096.0, LevelType.PRIOR_RTH_HIGH, t9, "PRIOR_RTH"),
        _level(20100.0, LevelType.OVERNIGHT_HIGH, t930, "OVERNIGHT"),
        _level(or_high, LevelType.OPENING_RANGE_HIGH, t945, "OPENING_RANGE"),
        _level(19900.0, LevelType.OPENING_RANGE_LOW, t945, "OPENING_RANGE"),
    )
    return DailyLevelSet(TRADE, "MNQM4", levels, Decimal("6"), ())


def _replay_bars():
    t = ny_time(TRADE, dt.time(9, 30))
    hlc = [(20080.0, 20070.0, 20075.0), (20102.0, 20094.0, 20101.0), (20104.0, 20101.0, 20102.0)]
    return [DecisionBar(t + i * FIVE, t + (i + 1) * FIVE, *v) for i, v in enumerate(hlc)]


def test_a_replay_acceptance_uses_its_real_timestamp_and_starts_confirmation():
    engines = initialize_engines(_daily(or_high=20040.0), _replay_bars(), STATE_PARAMS, PARAMS)
    engine = next(e for e in engines.active.values() if LevelType.OVERNIGHT_HIGH in e.zone.parent_constituent_types)
    (seq,) = engine.sequences
    accepted = next(e for e in engine.zone.history if e.event is ZoneEventType.ACCEPTED_ABOVE)
    assert accepted.initialization_replay and seq.acceptance_timestamp == accepted.timestamp_utc == ny_time(TRADE, dt.time(9, 45))
    t = ny_time(TRADE, dt.time(9, 45))
    engine.process(DecisionBar(t, t + FIVE, 20106.0, 20101.0, 20104.0))  # hold (bar 1 of the real clock)
    engine.process(DecisionBar(t + FIVE, t + 2 * FIVE, 20110.0, 20103.0, 20106.25))  # continuation
    assert seq.outcome is O.CONFIRMED
    (c,) = book().resolve(t + 2 * FIVE, [seq]).candidates
    assert c.status is S.ENTRY_CANDIDATE


def test_opening_range_zones_still_cannot_use_construction_bars_and_superseded_confirmations_die():
    engines = initialize_engines(_daily(or_high=20103.0), _replay_bars(), STATE_PARAMS, PARAMS)  # OR high merges
    (old,) = engines.archived.values()
    assert old.sequences and old.sequences[0].outcome is O.INVALIDATED_BY_ZONE_CHANGE
    new = next(e for e in engines.active.values() if LevelType.OPENING_RANGE_HIGH in e.zone.parent_constituent_types)
    assert [e.event for e in new.zone.history] == [ZoneEventType.ZONE_INITIALIZED] and not new.sequences
