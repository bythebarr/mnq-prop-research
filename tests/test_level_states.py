"""Round 9 zone-state rules (owner-specified test list).

Zones and bars here are hand-built fixtures for logic tests, not market data.
Zone under test: L = 20000.00, U = 20004.00, approach distance 6.00 points.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import inspect
from decimal import Decimal

import pandas as pd
import pytest

from conftest import RULE_FREEZE_PATH
from mnq_research import level_states
from mnq_research.config import load_mapping
from mnq_research.level_states import (
    DecisionBar,
    Origin,
    StateParams,
    ZoneEventType as E,
    ZoneState,
    ZoneStateValidationError,
    ZoneTracker,
    initialize_zones,
)
from mnq_research.structural_levels import (
    DailyLevelSet,
    LevelType,
    NewEntryNotPermitted,
    StructuralLevel,
    cluster_levels,
    levels_for_new_entry,
    ny_time,
)
from mnq_research.validation import check_rule_freeze

SPEC = load_mapping(RULE_FREEZE_PATH)
PARAMS = StateParams.from_spec(SPEC)
TRADE = dt.date(2024, 3, 12)
L, U = 20000.0, 20004.0
START = ny_time(TRADE, dt.time(10, 0))
FIVE = pd.Timedelta(minutes=5)


def make_level(price: float, level_type: LevelType, available_at: pd.Timestamp, window: str = "PRIOR_RTH") -> StructuralLevel:
    return StructuralLevel(level_type, TRADE, "MNQM4", window, None, None, available_at, price=price)


def zone(lower: float = L, upper: float = U, params: StateParams = PARAMS) -> ZoneTracker:
    levels = [make_level(lower, LevelType.PRIOR_RTH_HIGH, START), make_level(upper, LevelType.OVERNIGHT_HIGH, START, "OVERNIGHT")]
    (cluster,) = cluster_levels(levels, Decimal(str(upper - lower)), START)
    return ZoneTracker(cluster, params, Decimal("6"), START)


def bar(i: int, high: float | None, low: float | None, close: float | None, **kwargs) -> DecisionBar:
    return DecisionBar(START + i * FIVE, START + (i + 1) * FIVE, high, low, close, **kwargs)


def run(tracker: ZoneTracker, *bars: DecisionBar) -> list:
    return [e.event for b in bars for e in tracker.process(b)]


def below(i: int) -> DecisionBar:
    """A bar closing clearly below the zone (sets origin BELOW)."""
    return bar(i, 19995.0, 19990.0, 19992.0)


def above(i: int) -> DecisionBar:
    return bar(i, 20012.0, 20008.0, 20010.0)


# --------------------------------------------------------------------------- wicks vs closes


def test_wicks_determine_approach_touch_and_breach():
    z = zone()
    # NOTE (pending owner decision D-024 Q1): under the literal breach rule a bar
    # lying wholly below the zone is also BREACHED_BELOW, so only the event is asserted.
    assert E.APPROACHED_FROM_BELOW in run(z, bar(0, 19995.0, 19990.0, 19991.0))  # high within 6 of L
    assert E.TOUCH_FROM_BELOW in run(z, bar(1, 20001.0, 19990.0, 19991.0))  # wick reaches zone, close far below
    assert E.BREACHED_ABOVE in run(z, bar(2, 20004.25, 19990.0, 19991.0))  # wick beyond U, close far below
    assert z.consecutive_acceptance_closes_above == 0  # wicks never count toward acceptance


def test_closes_determine_acceptance_and_rejection_not_wicks():
    z = zone()
    events = run(z, bar(0, 20020.0, 20003.0, 20004.25), bar(1, 20020.0, 20003.0, 20004.25))  # wicks far above
    assert E.ACCEPTED_ABOVE not in events
    assert E.ACCEPTED_ABOVE in run(z, bar(2, 20006.0, 20004.5, 20004.5), bar(3, 20006.0, 20004.5, 20004.5))


def test_equality_with_a_zone_boundary_counts_as_a_touch():
    assert E.TOUCH_ORIGIN_UNKNOWN in run(zone(), bar(0, 20000.0, 19990.0, 19995.0))  # high == L
    assert E.TOUCH_ORIGIN_UNKNOWN in run(zone(), bar(0, 20010.0, 20004.0, 20008.0))  # low == U


def test_a_one_tick_far_boundary_wick_is_a_breach():
    assert E.BREACHED_ABOVE in run(zone(), bar(0, 20004.25, 20001.0, 20002.0))
    assert E.BREACHED_ABOVE not in run(zone(), bar(0, 20004.0, 20001.0, 20002.0))  # at U: touch only
    assert E.BREACHED_BELOW in run(zone(), bar(0, 20002.0, 19999.75, 20001.0))


# --------------------------------------------------------------------------- acceptance


def test_one_close_beyond_a_zone_is_not_acceptance():
    z = zone()
    assert E.ACCEPTED_ABOVE not in run(z, bar(0, 20010.0, 20003.0, 20009.0))
    assert z.consecutive_acceptance_closes_above == 1


def test_two_consecutive_qualifying_closes_create_acceptance():
    z = zone()
    run(z, bar(0, 20010.0, 20003.0, 20009.0))
    (accepted,) = [e for e in z.process(bar(1, 20010.0, 20005.0, 20008.0)) if e.event is E.ACCEPTED_ABOVE]
    assert accepted.timestamp_utc == START + 2 * FIVE  # close of the second qualifying bar
    assert z.current_state is ZoneState.ACCEPTED_ABOVE

    z = zone()
    assert E.ACCEPTED_BELOW in run(z, bar(0, 20001.0, 19990.0, 19999.5), bar(1, 19999.5, 19990.0, 19999.5))


def test_a_close_exactly_two_ticks_beyond_the_boundary_qualifies():
    assert E.ACCEPTED_ABOVE in run(zone(), bar(0, 20005.0, 20003.0, 20004.5), bar(1, 20005.0, 20003.0, 20004.5))
    assert E.ACCEPTED_ABOVE not in run(zone(), bar(0, 20005.0, 20003.0, 20004.25), bar(1, 20005.0, 20003.0, 20004.25))


def test_an_incomplete_bar_resets_consecutive_close_counters():
    z = zone()
    events = run(z, bar(0, 20010.0, 20003.0, 20009.0), bar(1, None, None, None, complete=False), bar(2, 20010.0, 20005.0, 20009.0))
    assert E.ACCEPTED_ABOVE not in events and E.DATA_INTERRUPTION in events
    # A decision bar missing from the sequence is also an interruption, never skipped over.
    z = zone()
    events = run(z, bar(0, 20010.0, 20003.0, 20009.0), bar(2, 20010.0, 20005.0, 20009.0))
    assert E.ACCEPTED_ABOVE not in events and E.DATA_INTERRUPTION in events


def test_a_blackout_resets_counters_and_pending_state():
    z = zone()
    run(z, below(0), bar(1, 20002.0, 19998.0, 20001.0))  # upward attempt: rejection window open
    assert z.active_rejection_window is not None
    events = run(z, bar(2, 20010.0, 20005.0, 20009.0), bar(3, 20010.0, 20005.0, 20009.0, overlaps_blackout=True))
    assert E.BLACKOUT_RESET in events and E.REJECTION_WINDOW_INVALIDATED in events
    assert z.active_rejection_window is None and z.consecutive_acceptance_closes_above == 0
    assert z.current_state is ZoneState.UNTOUCHED and z.interaction_origin is Origin.UNKNOWN
    # The pre-blackout qualifying close cannot pair with a post-blackout one.
    assert E.ACCEPTED_ABOVE not in run(z, bar(4, 20010.0, 20005.0, 20009.0))


# --------------------------------------------------------------------------- rejection


def test_rejection_can_occur_on_the_interaction_bar():
    z = zone()
    events = run(z, below(0), bar(1, 20002.0, 19998.0, 19999.5))  # touch, close <= L - 0.50
    assert E.TOUCH_FROM_BELOW in events and E.REJECTED_UPWARD_ATTEMPT in events
    # NOTE (pending D-024 Q2): the two closes below L - 0.50 also satisfy the literal
    # ACCEPTED_BELOW rule, which outranks rejection, so current_state is not asserted here.


@pytest.mark.parametrize("bars_after", [1, 2])
def test_rejection_can_occur_on_either_of_the_next_two_bars(bars_after):
    z = zone()
    run(z, below(0), bar(1, 20002.0, 20000.0, 20001.0))  # interaction, close inside zone
    for i in range(2, 1 + bars_after):
        run(z, bar(i, 20002.0, 20000.0, 20001.0))
    assert E.REJECTED_UPWARD_ATTEMPT in run(z, bar(1 + bars_after, 20001.0, 19995.0, 19999.5))

    z = zone()  # mirror: downward attempt rejected
    run(z, above(0), bar(1, 20004.0, 20002.0, 20003.0))
    for i in range(2, 1 + bars_after):
        run(z, bar(i, 20004.0, 20002.0, 20003.0))
    assert E.REJECTED_DOWNWARD_ATTEMPT in run(z, bar(1 + bars_after, 20006.0, 20003.0, 20004.5))


def test_rejection_cannot_occur_after_its_three_bar_window():
    z = zone()
    events = run(z, below(0), *[bar(i, 20002.0, 20000.0, 20001.0) for i in (1, 2, 3)])
    assert E.REJECTION_WINDOW_EXPIRED in events
    # A qualifying close after the window, without a new interaction, is not a rejection.
    # (Whether a later touch may open a NEW attempt is pending D-024 Q3.)
    assert E.REJECTED_UPWARD_ATTEMPT not in run(z, bar(4, 19999.5, 19995.0, 19999.5))


def test_rejection_cannot_be_assigned_with_unknown_origin():
    z = zone()
    events = run(z, bar(0, 20002.0, 19998.0, 19999.5))  # no prior clearly-outside close
    assert E.TOUCH_ORIGIN_UNKNOWN in events
    assert E.REJECTED_UPWARD_ATTEMPT not in events and E.REJECTED_DOWNWARD_ATTEMPT not in events
    assert z.interaction_origin is Origin.UNKNOWN


# --------------------------------------------------------------------------- gaps, two-sided, breakout


def test_a_gap_can_produce_acceptance_without_a_fabricated_touch():
    z = zone()
    events = run(z, below(0), bar(1, 20015.0, 20010.0, 20012.0), bar(2, 20015.0, 20010.0, 20012.0))
    assert E.GAPPED_ABOVE_ZONE in events and E.ACCEPTED_ABOVE in events
    assert not {E.TOUCH_FROM_BELOW, E.TOUCH_FROM_ABOVE, E.TOUCH_ORIGIN_UNKNOWN} & set(events)


def test_a_two_sided_breach_is_flagged_without_inferred_ordering():
    z = zone()
    (two_sided,) = [e for e in z.process(bar(0, 20006.0, 19998.0, 20002.0)) if e.event is E.TWO_SIDED_BREACH]
    assert "INTRABAR_ORDER_UNKNOWN" in two_sided.detail and "REQUIRES_FINER_DATA_REPLAY" in two_sided.detail
    assert {E.BREACHED_ABOVE, E.BREACHED_BELOW} <= {e.event for e in z.history}
    assert z.current_state is ZoneState.TWO_SIDED_BREACH


def test_breakout_is_not_a_separate_event():
    assert SPEC["acceptance_rejection_breakout"]["breakout_definition"] == "NOT_APPLICABLE_AS_SEPARATE_EVENT"
    assert not any("BREAKOUT" in name for name in [*E.__members__, *ZoneState.__members__])
    z = zone()  # a single huge wick and a single far close are not acceptance
    assert E.ACCEPTED_ABOVE not in run(z, bar(0, 20100.0, 20003.0, 20090.0))


def test_impossible_bars_are_validation_failures():
    with pytest.raises(ZoneStateValidationError):
        zone().process(bar(0, 20001.0, 20003.0, 20002.0))  # high below low
    with pytest.raises(ZoneStateValidationError):
        zone().process(bar(0, 20004.25, 20003.0, 20009.0))  # close above high


# --------------------------------------------------------------------------- initialisation and versioning


def daily_set(prior_high: float, overnight_low: float, or_high: float, or_low: float) -> DailyLevelSet:
    t_prior, t_open, t_or = ny_time(TRADE, dt.time(9, 0)), ny_time(TRADE, dt.time(9, 30)), ny_time(TRADE, dt.time(9, 45))
    levels = (
        make_level(prior_high, LevelType.PRIOR_RTH_HIGH, t_prior),
        make_level(overnight_low, LevelType.OVERNIGHT_LOW, t_open, "OVERNIGHT"),
        make_level(or_high, LevelType.OPENING_RANGE_HIGH, t_or, "OPENING_RANGE"),
        make_level(or_low, LevelType.OPENING_RANGE_LOW, t_or, "OPENING_RANGE"),
    )
    return DailyLevelSet(TRADE, "MNQM4", levels, Decimal("6"), ())


def pre_open_bars(high: float, low: float, close: float) -> list[DecisionBar]:
    t = ny_time(TRADE, dt.time(9, 30))
    return [DecisionBar(t + i * FIVE, t + (i + 1) * FIVE, high, low, close) for i in range(3)]


def test_pre_0945_bars_initialize_prior_and_overnight_zones():
    daily = daily_set(prior_high=20100.0, overnight_low=19900.0, or_high=20050.0, or_low=20020.0)
    book = initialize_zones(daily, pre_open_bars(20100.5, 20030.0, 20060.0), PARAMS)  # wicks through prior high
    prior_zone = next(z for z in book.active.values() if LevelType.PRIOR_RTH_HIGH in z.parent_constituent_types)
    assert prior_zone.initialized_at_utc == ny_time(TRADE, dt.time(9, 30))
    replayed = [e for e in prior_zone.history if e.initialization_replay]
    assert {e.event for e in replayed} >= {E.TOUCH_ORIGIN_UNKNOWN, E.BREACHED_ABOVE}


def test_pre_0945_bars_cannot_generate_entries():
    daily = daily_set(20100.0, 19900.0, 20050.0, 20020.0)
    book = initialize_zones(daily, pre_open_bars(20100.5, 20030.0, 20060.0), PARAMS)
    for tracker in book.active.values():
        assert all(e.timestamp_utc <= ny_time(TRADE, dt.time(9, 45)) for e in tracker.history)
        assert all(e.initialization_replay for e in tracker.history if e.event is not E.ZONE_INITIALIZED)
    with pytest.raises(NewEntryNotPermitted):
        levels_for_new_entry(daily, ny_time(TRADE, dt.time(9, 40)))


def test_opening_range_zones_are_not_touched_by_their_own_construction_bars():
    daily = daily_set(20100.0, 19900.0, 20050.0, 20020.0)
    book = initialize_zones(daily, pre_open_bars(20050.0, 20020.0, 20035.0), PARAMS)  # bars built the OR
    or_zones = [z for z in book.active.values() if set(z.parent_constituent_types) & level_states.OPENING_RANGE_TYPES]
    assert len(or_zones) == 2
    for z in or_zones:
        assert z.initialized_at_utc == ny_time(TRADE, dt.time(9, 45))
        assert [e.event for e in z.history] == [E.ZONE_INITIALIZED]
        assert z.current_state is ZoneState.UNTOUCHED


def test_a_cluster_changed_by_an_opening_range_level_gets_a_new_version_and_fresh_state():
    daily = daily_set(prior_high=20100.0, overnight_low=19900.0, or_high=20103.0, or_low=20020.0)  # OR high joins prior high
    book = initialize_zones(daily, pre_open_bars(20101.0, 20095.0, 20100.5), PARAMS)
    (old,) = book.archived.values()
    assert old.events(E.TOUCH_ORIGIN_UNKNOWN)  # pre-open history preserved for audit
    new = book.active[old.superseded_by]
    assert "0945-" in new.zone_id and old.zone_id in new.predecessor_cluster_ids
    assert set(new.parent_constituent_types) == {LevelType.PRIOR_RTH_HIGH, LevelType.OPENING_RANGE_HIGH}
    assert new.current_state is ZoneState.UNTOUCHED and new.consecutive_acceptance_closes_above == 0
    assert [e.event for e in new.history] == [E.ZONE_INITIALIZED]
    assert old.history[-1].event is E.ZONE_SUPERSEDED


# --------------------------------------------------------------------------- history and parameters


def test_historical_events_survive_later_state_changes():
    z = zone()
    run(z, bar(0, 20002.0, 19998.0, 20001.0))
    run(z, bar(1, 20010.0, 20005.0, 20009.0), bar(2, 20010.0, 20005.0, 20009.0))
    run(z, bar(3, 20001.0, 19990.0, 19995.0), bar(4, 19996.0, 19990.0, 19995.0))  # later opposite acceptance
    kinds = [e.event for e in z.history]
    assert E.TOUCH_ORIGIN_UNKNOWN in kinds and E.ACCEPTED_ABOVE in kinds and E.ACCEPTED_BELOW in kinds
    assert kinds.index(E.ACCEPTED_ABOVE) < kinds.index(E.ACCEPTED_BELOW)
    assert z.current_state is ZoneState.ACCEPTED_BELOW


def test_all_candidate_distances_come_from_the_specification():
    # 1. Changing the spec changes behaviour.
    spec = copy.deepcopy(SPEC)
    spec["level_states"]["parameters"].update(acceptance_consecutive_closes=3, breach_distance_ticks=2)
    stricter = StateParams.from_spec(spec)
    assert E.BREACHED_ABOVE not in run(zone(params=stricter), bar(0, 20004.25, 20003.0, 20004.0))  # needs 2 ticks now
    z = zone(params=stricter)
    events = run(z, bar(0, 20010.0, 20005.0, 20009.0), bar(1, 20010.0, 20005.0, 20009.0))
    assert E.ACCEPTED_ABOVE not in events  # K = 3 now
    assert E.ACCEPTED_ABOVE in run(z, bar(2, 20010.0, 20005.0, 20009.0))
    # 2. The module holds no numeric trading constants of its own (only 0 and 1 for counting).
    literals = {
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(level_states)))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    }
    assert literals <= {0, 1}
    # 3. The validator guards the parameters.
    spec["level_states"]["parameters"]["rejection_window_complete_bars"] = 0
    assert any(p.path.endswith("rejection_window_complete_bars") and p.kind == "INVALID" for p in check_rule_freeze(spec).problems)
