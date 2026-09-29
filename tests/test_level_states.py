"""Round 9 zone-state rules, as amended with directional arming and episodes.

Zones and bars are hand-built fixtures for logic tests, not market data.
Zone under test: L = 20000.00, U = 20004.00, approach distance 6.00 points,
clear-side distance 0.50 (arms below at close <= 19999.50, above at >= 20004.50).
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
    AttemptStatus,
    DecisionBar,
    Direction,
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


def zone(params: StateParams = PARAMS) -> ZoneTracker:
    levels = [make_level(L, LevelType.PRIOR_RTH_HIGH, START), make_level(U, LevelType.OVERNIGHT_HIGH, START, "OVERNIGHT")]
    (cluster,) = cluster_levels(levels, Decimal("4"), START)
    return ZoneTracker(cluster, params, Decimal("6"), START)


def bar(i: int, high: float | None, low: float | None, close: float | None, **kwargs) -> DecisionBar:
    return DecisionBar(START + i * FIVE, START + (i + 1) * FIVE, high, low, close, **kwargs)


def run(tracker: ZoneTracker, *bars: DecisionBar) -> list:
    return [e.event for b in bars for e in tracker.process(b)]


def far_below(i: int) -> DecisionBar:
    """Wholly below the zone and beyond approach distance; close arms from below."""
    return bar(i, 19990.0, 19985.0, 19988.0)


def far_above(i: int) -> DecisionBar:
    return bar(i, 20020.0, 20015.0, 20018.0)


def inside(i: int) -> DecisionBar:
    return bar(i, 20002.0, 20001.0, 20001.5)


def armed_up_attempt() -> ZoneTracker:
    """Armed below on bar 0, upward episode started by a touch on bar 1 (close inside)."""
    z = zone()
    run(z, far_below(0), inside(1))
    assert z.active_attempt_direction is Direction.UPWARD
    return z


def armed_down_attempt() -> ZoneTracker:
    z = zone()
    run(z, far_above(0), bar(1, 20003.0, 20002.0, 20002.5))
    assert z.active_attempt_direction is Direction.DOWNWARD
    return z


# =========================================================================== location vs attempt


def test_a_bar_wholly_below_a_zone_is_not_breached_below_without_a_downward_episode():
    z = zone()
    events = run(z, bar(0, 19995.0, 19990.0, 19992.0))
    assert E.BREACHED_BELOW not in events and E.ARMED_FROM_BELOW in events
    assert z.current_price_relation.value == "BELOW_ZONE"
    assert z.current_state is ZoneState.APPROACHED  # approach outranks arming; never a breach


def test_a_bar_wholly_above_a_zone_is_not_breached_above_without_an_upward_episode():
    events = run(zone(), bar(0, 20012.0, 20008.0, 20010.0))
    assert E.BREACHED_ABOVE not in events and E.ARMED_FROM_ABOVE in events


def test_remaining_below_for_two_closes_is_not_acceptance_below():
    z = zone()
    events = run(z, far_below(0), far_below(1), far_below(2))
    assert E.ACCEPTED_BELOW not in events
    assert z.armed_side is Origin.BELOW and not z.attempts


def test_remaining_above_for_two_closes_is_not_acceptance_above():
    z = zone()
    events = run(z, far_above(0), far_above(1), far_above(2))
    assert E.ACCEPTED_ABOVE not in events
    assert z.armed_side is Origin.ABOVE and not z.attempts


def test_upward_acceptance_requires_an_armed_below_episode():
    z = zone()  # never armed below: two closes above only arm from above
    assert E.ACCEPTED_ABOVE not in run(z, bar(0, 20010.0, 20001.0, 20008.0), bar(1, 20010.0, 20005.0, 20008.0))
    z = armed_up_attempt()
    events = run(z, bar(2, 20010.0, 20003.0, 20008.0), bar(3, 20010.0, 20005.0, 20008.0))
    assert E.ACCEPTED_ABOVE in events and z.attempt_status is AttemptStatus.ACCEPTED


def test_downward_acceptance_requires_an_armed_above_episode():
    z = zone()
    assert E.ACCEPTED_BELOW not in run(z, bar(0, 20003.0, 19990.0, 19995.0), bar(1, 19999.0, 19990.0, 19995.0))
    z = armed_down_attempt()
    events = run(z, bar(2, 20001.0, 19994.0, 19995.0), bar(3, 19999.0, 19990.0, 19995.0))
    assert E.ACCEPTED_BELOW in events


def test_arming_and_starting_a_touch_attempt_cannot_happen_on_the_same_bar():
    z = zone()
    events = run(z, bar(0, 20002.0, 19990.0, 19999.5))  # touches AND closes clear below
    assert E.ARMED_FROM_BELOW in events and E.ATTEMPT_STARTED not in events
    assert E.TOUCH_ORIGIN_UNKNOWN in events
    assert E.ATTEMPT_STARTED in run(z, inside(1))


# =========================================================================== wicks, closes, boundaries


def test_wicks_determine_approach_touch_and_breach():
    z = zone()
    run(z, far_below(0))
    assert E.APPROACHED_FROM_BELOW in run(z, bar(1, 19995.0, 19991.0, 19993.0))  # high within 6 of L
    assert E.TOUCH_FROM_BELOW in run(z, bar(2, 20001.0, 19999.75, 20000.0))  # wick reaches zone
    assert E.BREACHED_ABOVE in run(z, bar(3, 20004.25, 20001.0, 20001.0))  # wick beyond U, close inside
    assert z.consecutive_acceptance_closes_above == 0  # wicks never count toward acceptance


def test_closes_determine_acceptance_not_wicks():
    z = armed_up_attempt()
    assert E.ACCEPTED_ABOVE not in run(z, bar(2, 20020.0, 20003.0, 20004.25), bar(3, 20020.0, 20003.0, 20004.25))
    assert E.ACCEPTED_ABOVE in run(z, bar(4, 20006.0, 20004.5, 20004.5), bar(5, 20006.0, 20004.5, 20004.5))


def test_equality_with_a_zone_boundary_counts_as_a_touch():
    z = zone()
    run(z, far_below(0))
    assert E.TOUCH_FROM_BELOW in run(z, bar(1, 20000.0, 19995.0, 19999.75))  # high == L
    assert E.TOUCH_ORIGIN_UNKNOWN in run(zone(), bar(0, 20010.0, 20004.0, 20008.0))  # low == U, unarmed


def test_a_one_tick_far_boundary_wick_is_a_breach_within_an_episode():
    z = zone()
    run(z, far_below(0))
    assert E.BREACHED_ABOVE in run(z, bar(1, 20004.25, 20001.0, 20002.0))
    z = zone()
    run(z, far_below(0))
    assert E.BREACHED_ABOVE not in run(z, bar(1, 20004.0, 20001.0, 20002.0))  # at U: touch only
    z = zone()
    run(z, far_above(0))
    assert E.BREACHED_BELOW in run(z, bar(1, 20002.0, 19999.75, 20001.0))


def test_one_close_beyond_is_not_acceptance_but_two_are():
    z = armed_up_attempt()
    assert E.ACCEPTED_ABOVE not in run(z, bar(2, 20010.0, 20003.0, 20009.0))
    assert z.consecutive_acceptance_closes_above == 1
    (accepted,) = [e for e in z.process(bar(3, 20010.0, 20005.0, 20008.0)) if e.event is E.ACCEPTED_ABOVE]
    assert accepted.timestamp_utc == START + 4 * FIVE  # close of the second qualifying bar
    assert accepted.attempt_id == 1


def test_a_close_exactly_two_ticks_beyond_the_boundary_qualifies():
    z = armed_up_attempt()
    assert E.ACCEPTED_ABOVE in run(z, bar(2, 20005.0, 20003.0, 20004.5), bar(3, 20005.0, 20003.0, 20004.5))
    z = armed_up_attempt()
    assert E.ACCEPTED_ABOVE not in run(z, bar(2, 20005.0, 20003.0, 20004.25), bar(3, 20005.0, 20003.0, 20004.25))


# =========================================================================== rejection


def test_rejection_can_occur_on_the_interaction_bar_and_is_not_overridden_by_opposite_acceptance():
    z = zone()
    events = run(z, far_below(0), bar(1, 20002.0, 19998.0, 19999.5))  # touch; close <= L - 0.50
    assert E.TOUCH_FROM_BELOW in events and E.REJECTED_UPWARD_ATTEMPT in events
    # The previous close below the zone does not create an ACCEPTED_BELOW that could outrank it.
    assert E.ACCEPTED_BELOW not in events
    assert z.current_state is ZoneState.REJECTED_UPWARD_ATTEMPT


@pytest.mark.parametrize("bars_after", [1, 2])
def test_rejection_can_occur_on_either_of_the_next_two_bars(bars_after):
    z = armed_up_attempt()
    for i in range(2, 1 + bars_after):
        run(z, inside(i))
    assert E.REJECTED_UPWARD_ATTEMPT in run(z, bar(1 + bars_after, 20001.0, 19995.0, 19999.5))

    z = armed_down_attempt()
    for i in range(2, 1 + bars_after):
        run(z, bar(i, 20003.0, 20002.0, 20002.5))
    assert E.REJECTED_DOWNWARD_ATTEMPT in run(z, bar(1 + bars_after, 20006.0, 20003.0, 20004.5))


def test_rejection_cannot_occur_after_its_three_bar_window():
    z = armed_up_attempt()
    assert E.REJECTION_WINDOW_EXPIRED in run(z, inside(2), inside(3))
    events = run(z, bar(4, 20001.0, 19995.0, 19999.5))  # clear-side close after expiry
    assert E.REJECTED_UPWARD_ATTEMPT not in events
    assert z.attempts[0].status is AttemptStatus.ENDED_REARMED_ON_ORIGIN_SIDE
    assert E.ARMED_FROM_BELOW in events


def test_rejection_cannot_be_assigned_with_unknown_origin():
    z = zone()
    events = run(z, bar(0, 20002.0, 19998.0, 19999.5))  # never armed: no episode
    assert E.TOUCH_ORIGIN_UNKNOWN in events
    assert not {E.REJECTED_UPWARD_ATTEMPT, E.REJECTED_DOWNWARD_ATTEMPT} & set(events)


def test_a_rejection_closes_its_attempt_and_may_rearm_the_zone():
    z = zone()
    events = run(z, far_below(0), bar(1, 20002.0, 19998.0, 19999.5))
    assert E.ATTEMPT_ENDED in events and E.ARMED_FROM_BELOW in events
    assert z.attempts[0].status is AttemptStatus.REJECTED and z.active_attempt is None
    assert E.ATTEMPT_STARTED in run(z, inside(2))  # only a LATER bar starts attempt 2
    assert z.attempt_id == 2


def test_delayed_acceptance_after_the_rejection_window_expires():
    z = armed_up_attempt()
    run(z, inside(2), inside(3))  # window expired; episode still active
    assert z.active_attempt_direction is Direction.UPWARD
    events = run(z, bar(4, 20010.0, 20003.0, 20008.0), bar(5, 20010.0, 20005.0, 20008.0))
    assert E.ACCEPTED_ABOVE in events


# =========================================================================== rearming and attempt ids


def test_an_expired_attempt_cannot_restart_from_another_touch_without_rearming():
    z = armed_up_attempt()
    run(z, inside(2), inside(3), inside(4), inside(5))  # expired; more touches
    assert len(z.attempts) == 1 and E.ATTEMPT_STARTED not in run(z, inside(6))


def test_the_rearming_bar_cannot_also_begin_the_next_attempt_and_ids_increment():
    z = armed_up_attempt()
    run(z, inside(2), inside(3))
    events = run(z, bar(4, 20002.0, 19995.0, 19999.5))  # touches AND closes clear below: rearm only
    assert E.ARMED_FROM_BELOW in events and E.ATTEMPT_STARTED not in events
    started = [e for e in z.process(inside(5)) if e.event is E.ATTEMPT_STARTED]
    assert [e.attempt_id for e in started] == [2]
    assert [a.attempt_id for a in z.attempts] == [1, 2]


# =========================================================================== gaps and two-sided bars


def test_a_qualifying_gap_begins_an_attempt_without_a_fabricated_touch():
    z = zone()
    events = run(z, far_below(0), bar(1, 20015.0, 20010.0, 20012.0), bar(2, 20015.0, 20010.0, 20012.0))
    assert E.GAPPED_ABOVE_ZONE in events and E.ATTEMPT_STARTED in events and E.ACCEPTED_ABOVE in events
    assert not {E.TOUCH_FROM_BELOW, E.TOUCH_FROM_ABOVE, E.TOUCH_ORIGIN_UNKNOWN} & set(events)


def test_gap_origin_must_satisfy_the_clear_side_threshold():
    z = zone()
    events = run(z, bar(0, 19999.75, 19996.0, 19999.75), bar(1, 20015.0, 20010.0, 20012.0), bar(2, 20015.0, 20010.0, 20012.0))
    assert E.GAPPED_ABOVE_ZONE not in events and E.ACCEPTED_ABOVE not in events  # 19999.75 is not <= L - 0.50


def test_a_two_sided_bar_records_only_the_direction_of_the_active_episode():
    z = zone()
    run(z, far_below(0))
    (two_sided,) = [e for e in z.process(bar(1, 20006.0, 19998.0, 20002.0)) if e.event is E.TWO_SIDED_BREACH]
    kinds = {e.event for e in z.history}
    assert E.BREACHED_ABOVE in kinds and E.BREACHED_BELOW not in kinds
    assert "INTRABAR_ORDER_UNKNOWN" in two_sided.detail and "REQUIRES_FINER_DATA_REPLAY" in two_sided.detail
    assert z.current_state is ZoneState.TWO_SIDED_BREACH


def test_a_two_sided_bar_with_no_episode_records_no_directional_breach():
    events = run(zone(), bar(0, 20006.0, 19998.0, 20002.0))
    assert E.TWO_SIDED_BREACH in events
    assert E.BREACHED_ABOVE not in events and E.BREACHED_BELOW not in events


def test_breakout_is_not_a_separate_event():
    assert SPEC["acceptance_rejection_breakout"]["breakout_definition"] == "NOT_APPLICABLE_AS_SEPARATE_EVENT"
    assert not any("BREAKOUT" in name for name in [*E.__members__, *ZoneState.__members__])
    z = armed_up_attempt()
    assert E.ACCEPTED_ABOVE not in run(z, bar(2, 20100.0, 20003.0, 20090.0))  # one huge bar is not acceptance


def test_impossible_bars_are_validation_failures():
    with pytest.raises(ZoneStateValidationError):
        zone().process(bar(0, 20001.0, 20003.0, 20002.0))  # high below low
    with pytest.raises(ZoneStateValidationError):
        zone().process(bar(0, 20004.25, 20003.0, 20009.0))  # close above high


# =========================================================================== interruptions


def test_an_incomplete_or_missing_bar_resets_counters_and_executable_state():
    z = armed_up_attempt()
    events = run(z, bar(2, 20010.0, 20003.0, 20009.0), bar(3, None, None, None, complete=False), bar(4, 20010.0, 20005.0, 20009.0))
    assert E.ACCEPTED_ABOVE not in events and E.DATA_INTERRUPTION in events
    assert z.attempts[0].status is AttemptStatus.ENDED_INTERRUPTION
    z = armed_up_attempt()
    events = run(z, bar(2, 20010.0, 20003.0, 20009.0), bar(4, 20010.0, 20005.0, 20009.0))  # bar 3 missing
    assert E.ACCEPTED_ABOVE not in events and E.DATA_INTERRUPTION in events


def test_blackout_and_missing_bar_resets_preserve_history_but_remove_executable_state():
    for interrupt in (
        lambda z: z.process(bar(2, 20010.0, 20005.0, 20009.0, overlaps_blackout=True)),
        lambda z: z.process(bar(3, 20002.0, 20001.0, 20001.5)),  # bar 2 missing
    ):
        z = armed_up_attempt()
        before = z.history
        interrupt(z)
        assert z.history[: len(before)] == before  # nothing erased
        assert z.active_attempt is None and z.armed_side is None
        assert z.consecutive_acceptance_closes_above == 0 and not z.active_rejection_window
        assert z.interaction_origin is Origin.UNKNOWN
    z = armed_up_attempt()
    events = run(z, bar(2, 20010.0, 20005.0, 20009.0, overlaps_blackout=True))
    assert E.BLACKOUT_RESET in events and E.REJECTION_WINDOW_INVALIDATED in events
    assert z.current_state is ZoneState.UNTOUCHED
    # After the blackout, a fresh rearm is required before any new attempt.
    assert E.ATTEMPT_STARTED not in run(z, inside(3))


# =========================================================================== initialisation and versioning


def daily_set(prior_high: float, overnight_low: float, or_high: float, or_low: float) -> DailyLevelSet:
    t_prior, t_open, t_or = ny_time(TRADE, dt.time(9, 0)), ny_time(TRADE, dt.time(9, 30)), ny_time(TRADE, dt.time(9, 45))
    levels = (
        make_level(prior_high, LevelType.PRIOR_RTH_HIGH, t_prior),
        make_level(overnight_low, LevelType.OVERNIGHT_LOW, t_open, "OVERNIGHT"),
        make_level(or_high, LevelType.OPENING_RANGE_HIGH, t_or, "OPENING_RANGE"),
        make_level(or_low, LevelType.OPENING_RANGE_LOW, t_or, "OPENING_RANGE"),
    )
    return DailyLevelSet(TRADE, "MNQM4", levels, Decimal("6"), ())


def pre_open_bars(*hlc: tuple[float, float, float]) -> list[DecisionBar]:
    t = ny_time(TRADE, dt.time(9, 30))
    return [DecisionBar(t + i * FIVE, t + (i + 1) * FIVE, *v) for i, v in enumerate(hlc)]


def test_pre_0945_bars_initialize_prior_and_overnight_zones():
    daily = daily_set(prior_high=20100.0, overnight_low=19900.0, or_high=20070.0, or_low=20020.0)
    bars = pre_open_bars((20060.0, 20030.0, 20050.0), (20100.5, 20090.0, 20095.0), (20098.0, 20090.0, 20094.0))
    book = initialize_zones(daily, bars, PARAMS)
    prior_zone = next(z for z in book.active.values() if LevelType.PRIOR_RTH_HIGH in z.parent_constituent_types)
    assert prior_zone.initialized_at_utc == ny_time(TRADE, dt.time(9, 30))
    replayed = {e.event for e in prior_zone.history if e.initialization_replay}
    assert {E.ARMED_FROM_BELOW, E.ATTEMPT_STARTED, E.BREACHED_ABOVE} <= replayed


def test_pre_0945_bars_cannot_generate_entries():
    daily = daily_set(20100.0, 19900.0, 20070.0, 20020.0)
    book = initialize_zones(daily, pre_open_bars((20060.0, 20030.0, 20050.0), (20100.5, 20090.0, 20095.0)), PARAMS)
    for tracker in book.active.values():
        assert all(e.timestamp_utc <= ny_time(TRADE, dt.time(9, 45)) for e in tracker.history)
        assert all(e.initialization_replay for e in tracker.history if e.event is not E.ZONE_INITIALIZED)
    with pytest.raises(NewEntryNotPermitted):
        levels_for_new_entry(daily, ny_time(TRADE, dt.time(9, 40)))


def test_opening_range_zones_start_unarmed_and_are_not_touched_by_their_construction_bars():
    daily = daily_set(20100.0, 19900.0, 20050.0, 20020.0)
    book = initialize_zones(daily, pre_open_bars((20050.0, 20020.0, 20035.0), (20045.0, 20025.0, 20030.0)), PARAMS)
    or_zones = [z for z in book.active.values() if set(z.parent_constituent_types) & level_states.OPENING_RANGE_TYPES]
    assert len(or_zones) == 2
    for z in or_zones:
        assert z.initialized_at_utc == ny_time(TRADE, dt.time(9, 45))
        assert [e.event for e in z.history] == [E.ZONE_INITIALIZED]
        assert z.current_state is ZoneState.UNTOUCHED and z.armed_side is None and z.active_attempt is None
    # A post-09:45 bar must arm first; only a later bar may begin an episode.
    or_high = next(z for z in or_zones if LevelType.OPENING_RANGE_HIGH in z.parent_constituent_types)
    t = ny_time(TRADE, dt.time(9, 45))
    first = [e.event for e in or_high.process(DecisionBar(t, t + FIVE, 20051.0, 20040.0, 20049.5))]
    assert E.ARMED_FROM_BELOW in first and E.ATTEMPT_STARTED not in first
    second = [e.event for e in or_high.process(DecisionBar(t + FIVE, t + 2 * FIVE, 20051.0, 20045.0, 20050.0))]
    assert E.ATTEMPT_STARTED in second


def test_a_cluster_changed_by_an_opening_range_level_gets_a_new_version_and_fresh_state():
    daily = daily_set(prior_high=20100.0, overnight_low=19900.0, or_high=20103.0, or_low=20020.0)
    book = initialize_zones(daily, pre_open_bars((20060.0, 20030.0, 20050.0), (20101.0, 20095.0, 20100.5)), PARAMS)
    (old,) = book.archived.values()
    assert old.events(E.ATTEMPT_STARTED)  # pre-open history preserved for audit
    new = book.active[old.superseded_by]
    assert "0945-" in new.zone_id and old.zone_id in new.predecessor_cluster_ids
    assert set(new.parent_constituent_types) == {LevelType.PRIOR_RTH_HIGH, LevelType.OPENING_RANGE_HIGH}
    assert new.current_state is ZoneState.UNTOUCHED and new.armed_side is None and not new.attempts
    assert [e.event for e in new.history] == [E.ZONE_INITIALIZED]
    assert old.history[-1].event is E.ZONE_SUPERSEDED


# =========================================================================== history and parameters


def test_historical_events_survive_later_state_changes():
    z = armed_up_attempt()
    run(z, bar(2, 20010.0, 20003.0, 20008.0), bar(3, 20010.0, 20005.0, 20008.0))  # accepted above; armed above
    run(z, bar(4, 20006.0, 20002.0, 20003.0))  # retest from above: downward attempt 2
    run(z, bar(5, 20001.0, 19994.0, 19995.0), bar(6, 19999.0, 19990.0, 19995.0))  # accepted below
    kinds = [e.event for e in z.history]
    assert E.TOUCH_FROM_BELOW in kinds and E.ACCEPTED_ABOVE in kinds and E.ACCEPTED_BELOW in kinds
    assert kinds.index(E.ACCEPTED_ABOVE) < kinds.index(E.ACCEPTED_BELOW)
    assert [a.status for a in z.attempts] == [AttemptStatus.ACCEPTED, AttemptStatus.ACCEPTED]
    assert z.current_state is ZoneState.ACCEPTED_BELOW


def test_all_candidate_distances_come_from_the_specification():
    spec = copy.deepcopy(SPEC)
    spec["level_states"]["parameters"].update(acceptance_consecutive_closes=3, breach_distance_ticks=2)
    stricter = StateParams.from_spec(spec)
    z = zone(stricter)
    run(z, far_below(0))
    assert E.BREACHED_ABOVE not in run(z, bar(1, 20004.25, 20003.0, 20004.0))  # needs 2 ticks now
    events = run(z, bar(2, 20010.0, 20005.0, 20009.0), bar(3, 20010.0, 20005.0, 20009.0))
    assert E.ACCEPTED_ABOVE not in events  # K = 3 now
    assert E.ACCEPTED_ABOVE in run(z, bar(4, 20010.0, 20005.0, 20009.0))
    # The module holds no numeric trading constants of its own (only 0 and 1 for counting).
    literals = {
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(level_states)))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    }
    assert literals <= {0, 1}
    spec["level_states"]["parameters"]["rejection_window_complete_bars"] = 0
    assert any(p.path.endswith("rejection_window_complete_bars") and p.kind == "INVALID" for p in check_rule_freeze(spec).problems)


# =========================================================================== D-025 clarifications


def test_an_approach_only_episode_is_withdrawn_not_rejected():
    z = zone()
    run(z, far_below(0))
    run(z, bar(1, 19995.0, 19991.0, 19994.0))  # approach starts the episode (close not clear-side)
    assert z.active_attempt.phase == "APPROACH_ONLY" and not z.active_rejection_window
    events = run(z, bar(2, 19996.0, 19990.0, 19992.0))  # back at the origin-side threshold, never touched
    assert E.REJECTED_UPWARD_ATTEMPT not in events
    assert z.attempts[0].status is AttemptStatus.APPROACH_WITHDRAWN
    assert E.ARMED_FROM_BELOW in events and E.ATTEMPT_STARTED not in events  # rearmed; a LATER bar must start
    assert E.APPROACHED_FROM_BELOW in [e.event for e in z.history]  # approach history kept
    assert E.ATTEMPT_STARTED in run(z, inside(3))
    assert z.attempt_id == 2


def test_every_acceptance_gets_a_unique_acceptance_id():
    z = armed_up_attempt()
    (accepted,) = [e for e in (*z.process(bar(2, 20010.0, 20003.0, 20008.0)), *z.process(bar(3, 20010.0, 20005.0, 20008.0))) if e.event is E.ACCEPTED_ABOVE]
    assert accepted.acceptance_id == f"{z.zone_id}:ACC1" == z.attempts[0].acceptance_id


def test_gap_threshold_is_enforced_in_a_non_b0_parameter_fixture():
    """Component-level parameter-interaction test (D-025 clarification 3).

    Under B0 the approach distance (>= 2 points) exceeds the 0.50 clear-side
    distance, which hides the gap-origin threshold behind the arming rule. With
    a wide clear-side distance (2.00) and a narrow approach distance (0.50) a
    "near-gap" close 1.00 below L neither arms nor approaches, so only the gap
    threshold itself decides whether the next bar is a qualifying gap.
    These values are NOT B0 values.
    """
    spec = copy.deepcopy(SPEC)
    spec["level_states"]["parameters"]["rejection_close_distance_ticks"] = 8  # clear side = 2.00 points
    params = StateParams.from_spec(spec)
    levels = [make_level(L, LevelType.PRIOR_RTH_HIGH, START), make_level(U, LevelType.OVERNIGHT_HIGH, START, "OVERNIGHT")]
    (cluster,) = cluster_levels(levels, Decimal("4"), START)
    z = ZoneTracker(cluster, params, Decimal("0.5"), START)
    run(z, bar(0, 19990.0, 19985.0, 19988.0))  # clear-side close: armed from below
    assert z.armed_side is Origin.BELOW
    run(z, bar(1, 19999.0, 19997.0, 19999.0))  # near-gap close L - 1.00: not <= L - 2.00, no approach
    assert z.active_attempt is None
    events = run(z, bar(2, 20015.0, 20010.0, 20012.0))  # low above U
    assert E.GAPPED_ABOVE_ZONE not in events and E.ATTEMPT_STARTED not in events
