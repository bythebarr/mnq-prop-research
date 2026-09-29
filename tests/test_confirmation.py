"""Round 10 continuation confirmation (owner-specified test list).

Hand-built fixtures for logic tests, not market data.
Zone: L = 20000.00, U = 20004.00, retest distance (proximity tolerance) 6.00.
Long acceptance is produced by: arm below -> touch -> two closes >= 20004.50.
Short acceptance mirrors it from above.
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
from mnq_research import confirmation
from mnq_research.config import load_mapping
from mnq_research.confirmation import (
    ConfirmationEventType as C,
    ConfirmationOutcome as O,
    ConfirmationParams,
    ConfirmationSequence,
    IdentityMismatchError,
    SetupEngine,
)
from mnq_research.level_states import DecisionBar, StateParams, ZoneEvent, ZoneEventType, ZoneTracker
from mnq_research.structural_levels import LevelType, StructuralLevel, cluster_levels, ny_time
from mnq_research.validation import check_rule_freeze

SPEC = load_mapping(RULE_FREEZE_PATH)
STATE_PARAMS = StateParams.from_spec(SPEC)
PARAMS = ConfirmationParams.from_spec(SPEC)
TRADE = dt.date(2024, 3, 12)
FIVE = pd.Timedelta(minutes=5)
L, U = 20000.0, 20004.0


def make_zone(start: pd.Timestamp) -> ZoneTracker:
    levels = [
        StructuralLevel(LevelType.PRIOR_RTH_HIGH, TRADE, "MNQM4", "PRIOR_RTH", None, None, start, price=L),
        StructuralLevel(LevelType.OVERNIGHT_HIGH, TRADE, "MNQM4", "OVERNIGHT", None, None, start, price=U),
    ]
    (cluster,) = cluster_levels(levels, Decimal("4"), start)
    return ZoneTracker(cluster, STATE_PARAMS, Decimal("6"), start)


class Session:
    """Feeds consecutive five-minute bars; bar k (k >= 1) is the k-th bar after acceptance."""

    def __init__(self, side: str = "LONG", start: dt.time = dt.time(10, 0), params: ConfirmationParams = PARAMS):
        self.start = ny_time(TRADE, start)
        self.engine = SetupEngine(make_zone(self.start), params)
        self.i = 0
        setup = (
            [(19990.0, 19985.0, 19988.0), (20002.0, 20001.0, 20001.5), (20010.0, 20003.0, 20008.0), (20010.0, 20005.0, 20008.0)]
            if side == "LONG"
            else [(20020.0, 20015.0, 20018.0), (20003.0, 20002.0, 20002.5), (20001.0, 19994.0, 19995.0), (19999.0, 19990.0, 19995.0)]
        )
        for hlc in setup:
            self.feed(*hlc)
        assert self.sequence is not None and self.sequence.is_pending  # acceptance on the 4th bar
        self.acceptance_time = self.sequence.acceptance_timestamp

    @property
    def sequence(self) -> ConfirmationSequence | None:
        return self.engine.sequences[-1] if self.engine.sequences else None

    def feed(self, high=None, low=None, close=None, complete=True, blackout=False, skip=0):
        self.i += skip
        t = self.start + self.i * FIVE
        self.i += 1
        return self.engine.process(DecisionBar(t, t + FIVE, high, low, close, complete, blackout))

    def kinds(self) -> list:
        return [e.event for e in self.sequence.events]


# Typical long bars
HOLD = (20009.0, 20005.0, 20007.0)  # low within U + 6, close >= U + 0.50; high 20009
NEUTRAL = (20006.0, 20004.25, 20004.25)  # close between U and U + 0.50: neither hold nor failure
EXTENSION = (20020.0, 20012.0, 20018.0)  # low above U + 6: no pullback


# =========================================================================== basics


def test_acceptance_alone_is_not_confirmation():
    s = Session()
    assert s.kinds() == [C.CONFIRMATION_SEQUENCE_STARTED] and s.sequence.outcome is None


def test_continued_extension_without_a_pullback_does_not_confirm():
    s = Session()
    for _ in range(6):
        s.feed(*EXTENSION)
    assert s.sequence.outcome is O.TIMED_OUT
    assert C.LONG_RETEST_HOLD not in s.kinds() and C.CONFIRMED_LONG_CONTINUATION not in s.kinds()


def test_long_retest_uses_the_upper_boundary_and_proximity_tolerance():
    s = Session()
    s.feed(20012.0, 20010.25, 20011.0)  # low one tick beyond U + 6: not a retest
    assert C.LONG_RETEST_HOLD not in s.kinds()
    s.feed(20012.0, 20010.0, 20011.0)  # low exactly U + 6: retest (equality qualifies)
    assert s.sequence.hold is not None and s.sequence.hold.bar_index == 2


def test_short_retest_uses_the_lower_boundary_and_proximity_tolerance():
    s = Session("SHORT")
    s.feed(19993.75, 19990.0, 19992.0)  # high one tick short of L - 6
    assert C.SHORT_RETEST_HOLD not in s.kinds()
    s.feed(19994.0, 19990.0, 19992.0)  # high exactly L - 6
    assert s.sequence.hold is not None


def test_a_retest_may_wick_inside_the_zone_without_failing():
    s = Session()
    s.feed(20008.0, 20001.0, 20005.0)  # low inside the zone, close >= U + 0.50
    assert s.sequence.hold is not None and s.sequence.outcome is None


def test_equality_at_every_threshold():
    s = Session()
    s.feed(20008.0, 20003.0, 20004.5)  # close exactly U + 0.50 qualifies as a hold
    assert s.sequence.hold.close == Decimal("20004.5")
    s.feed(20009.0, 20005.0, 20008.25)  # close exactly hold high + 0.25 confirms
    assert s.sequence.outcome is O.CONFIRMED

    s = Session()
    s.feed(20006.0, 20002.0, 20004.0)  # close exactly U fails before a hold
    assert s.sequence.outcome is O.FAILED

    # D-026 (pending): a low of exactly L - 0.25 meets both the hold limit and the
    # failure rule; the failure rule is applied.
    s = Session()
    s.feed(20008.0, 19999.75, 20005.0)
    assert s.sequence.outcome is O.FAILED and s.sequence.hold is None


def test_the_retest_hold_cannot_be_the_acceptance_bar():
    s = Session()  # the acceptance bar (low 20005, close 20008) would itself look like a hold
    s.feed(20013.0, 20012.0, 20012.5)  # would confirm if the acceptance bar were the hold
    assert C.CONFIRMED_LONG_CONTINUATION not in s.kinds() and s.sequence.hold is None


# =========================================================================== continuation


def test_long_confirmation_requires_a_close_one_tick_above_the_hold_high():
    s = Session()
    s.feed(*HOLD)
    s.feed(20012.0, 20006.0, 20009.0)  # close == hold high
    assert s.sequence.outcome is O.FAILED
    s = Session()
    s.feed(*HOLD)
    events = s.feed(20012.0, 20006.0, 20009.25)
    assert [e.event for e in events] == [C.CONFIRMED_LONG_CONTINUATION]
    assert events[0].timestamp_utc == s.acceptance_time + 2 * FIVE


def test_short_confirmation_requires_a_close_one_tick_below_the_hold_low():
    s = Session("SHORT")
    s.feed(19998.0, 19993.0, 19996.0)
    s.feed(19995.0, 19990.0, 19993.0)  # close == hold low
    assert s.sequence.outcome is O.FAILED
    s = Session("SHORT")
    s.feed(19998.0, 19993.0, 19996.0)
    s.feed(19995.0, 19990.0, 19992.75)
    assert C.CONFIRMED_SHORT_CONTINUATION in s.kinds()


def test_a_wick_through_the_continuation_threshold_is_insufficient():
    s = Session()
    s.feed(*HOLD)
    s.feed(20020.0, 20006.0, 20008.0)  # high far above the hold high, close below it
    assert s.sequence.outcome is O.FAILED and C.CONFIRMATION_FAILED_AFTER_HOLD in s.kinds()


@pytest.mark.parametrize("interruption", ["missing", "incomplete", "blackout"])
def test_hold_and_continuation_must_be_consecutive_eligible_bars(interruption):
    s = Session()
    s.feed(*HOLD)
    if interruption == "missing":
        s.feed(20012.0, 20006.0, 20010.0, skip=1)
        assert s.sequence.outcome is O.INVALIDATED_BY_INTERRUPTION
    elif interruption == "incomplete":
        s.feed(complete=False)
        assert s.sequence.outcome is O.INVALIDATED_BY_INTERRUPTION
    else:
        s.feed(20012.0, 20006.0, 20010.0, blackout=True)
        assert s.sequence.outcome is O.INVALIDATED_BY_BLACKOUT
    assert C.CONFIRMED_LONG_CONTINUATION not in s.kinds()


def test_only_the_first_hold_controls_and_its_failure_invalidates_the_acceptance():
    s = Session()
    s.feed(*HOLD)
    s.feed(20008.0, 20005.0, 20006.0)  # continuation fails (and would itself look like a hold)
    assert s.sequence.outcome is O.FAILED and C.CONFIRMATION_FAILED_AFTER_HOLD in s.kinds()
    s.feed(20012.0, 20007.0, 20010.0)  # a later "pair" is never considered
    assert C.CONFIRMED_LONG_CONTINUATION not in s.kinds()
    assert [e.event for e in s.sequence.events].count(C.LONG_RETEST_HOLD) == 1


# =========================================================================== clock


def test_confirmation_may_occur_no_later_than_bar_6():
    s = Session()
    for _ in range(4):
        s.feed(*NEUTRAL)  # bars 1-4
    s.feed(*HOLD)  # bar 5
    events = s.feed(20012.0, 20006.0, 20009.25)  # bar 6
    assert events[0].event is C.CONFIRMED_LONG_CONTINUATION and events[0].bar_index == 6
    assert events[0].timestamp_utc == s.acceptance_time + 6 * FIVE


def test_a_hold_candidate_on_bar_6_cannot_qualify():
    s = Session()
    for _ in range(5):
        s.feed(*NEUTRAL)
    s.feed(*HOLD)  # bar 6
    assert s.sequence.outcome is O.TIMED_OUT and s.sequence.hold is None
    assert s.sequence.events[-1].detail == "HOLD_ON_FINAL_BAR_CANNOT_QUALIFY"


# =========================================================================== failures


def test_a_long_close_at_or_below_u_invalidates_before_hold():
    s = Session()
    s.feed(20006.0, 20002.0, 20004.0)
    assert s.sequence.outcome is O.FAILED and C.CONFIRMATION_FAILED_BEFORE_HOLD in s.kinds()


def test_a_short_close_at_or_above_l_invalidates_before_hold():
    s = Session("SHORT")
    s.feed(20002.0, 19997.0, 20000.0)
    assert s.sequence.outcome is O.FAILED


def test_a_one_tick_wick_through_the_opposite_boundary_invalidates():
    s = Session()
    s.feed(20008.0, 19999.75, 20005.0)  # before hold
    assert s.sequence.outcome is O.FAILED
    s = Session()
    s.feed(*HOLD)
    s.feed(20012.0, 19999.75, 20010.0)  # after hold, even with a qualifying close
    assert s.sequence.outcome is O.FAILED
    s = Session("SHORT")
    s.feed(20004.25, 19993.0, 19996.0)
    assert s.sequence.outcome is O.FAILED


@pytest.mark.parametrize("interruption", ["missing", "incomplete", "blackout"])
def test_interruptions_invalidate_pending_confirmation_before_hold(interruption):
    s = Session()
    s.feed(*NEUTRAL)
    if interruption == "missing":
        s.feed(*HOLD, skip=1)
    elif interruption == "incomplete":
        s.feed(complete=False)
    else:
        s.feed(*HOLD, blackout=True)
    assert s.sequence.outcome in (O.INVALIDATED_BY_INTERRUPTION, O.INVALIDATED_BY_BLACKOUT)
    s.feed(*HOLD)
    assert s.sequence.hold is None


def test_confirmation_cannot_cross_the_1130_cutoff():
    s = Session(start=dt.time(11, 0))  # acceptance at 11:20
    assert s.acceptance_time == ny_time(TRADE, dt.time(11, 20))
    s.feed(*HOLD)  # bar 1 closes 11:25
    s.feed(20012.0, 20006.0, 20010.0)  # bar 2 closes 11:30: would confirm, but cutoff
    assert s.sequence.outcome is O.INVALIDATED_BY_CUTOFF


# =========================================================================== identity and reuse


def test_confirmation_cannot_combine_different_acceptance_attempt_or_zone_ids():
    s1, s2 = Session(), Session()
    acceptance_1 = next(e for e in s1.engine.zone.history if e.event is ZoneEventType.ACCEPTED_ABOVE)
    with pytest.raises(IdentityMismatchError):  # acceptance from another zone version
        ConfirmationSequence(s2.engine.zone, acceptance_1, PARAMS)
    forged = ZoneEvent(acceptance_1.timestamp_utc, acceptance_1.event, False, "", 99, acceptance_1.acceptance_id)
    with pytest.raises(IdentityMismatchError):  # attempt id does not match
        ConfirmationSequence(s1.engine.zone, forged, PARAMS)
    seq = s1.sequence
    assert {e.acceptance_id for e in seq.events} == {seq.acceptance_id}
    assert {e.attempt_id for e in seq.events} == {seq.attempt_id}
    assert {e.zone_version_id for e in seq.events} == {s1.engine.zone.zone_id}


def test_a_failed_acceptance_cannot_be_reused():
    s = Session()
    s.feed(20006.0, 20002.0, 20004.0)  # fails
    assert s.sequence.outcome is O.FAILED
    assert s.feed(*HOLD) == () or C.LONG_RETEST_HOLD not in s.kinds()
    acceptance = next(e for e in s.engine.zone.history if e.event is ZoneEventType.ACCEPTED_ABOVE)
    with pytest.raises(IdentityMismatchError):
        ConfirmationSequence(s.engine.zone, acceptance, PARAMS)


def test_a_zone_produces_a_later_setup_only_through_a_fresh_attempt_and_acceptance():
    s = Session()
    first = s.sequence
    s.feed(20006.0, 20001.0, 20003.0)  # fails the long; touch from above starts downward attempt 2
    assert first.outcome is O.FAILED and s.engine.active is None
    s.feed(19990.0, 19985.0, 19988.0)  # downward acceptance close 1
    s.feed(19990.0, 19985.0, 19988.0)  # close 2 -> new acceptance, new clock
    second = s.sequence
    assert second is not first
    assert second.attempt_id == 2 and second.acceptance_id.endswith(":ACC2")
    assert second.acceptance_timestamp > first.acceptance_timestamp and second.hold is None


def test_zone_change_invalidates_pending_confirmation():
    s = Session()
    s.engine.zone_changed(s.acceptance_time + FIVE)
    assert s.sequence.outcome is O.INVALIDATED_BY_ZONE_CHANGE


# =========================================================================== parameters


def test_every_parameter_comes_from_the_specification():
    spec = copy.deepcopy(SPEC)
    spec["confirmation"]["max_bars_after_acceptance"] = 3
    spec["confirmation"]["parameters"].update(continuation_break_distance_ticks=2, retest_hold_close_distance_ticks=4)
    params = ConfirmationParams.from_spec(spec)
    s = Session(params=params)
    s.feed(20008.0, 20003.0, 20004.5)  # close U + 0.50 no longer a hold (needs U + 1.00)
    assert s.sequence.hold is None
    s.feed(*HOLD)  # bar 2 (close 20007 >= U + 1.00)
    s.feed(20012.0, 20006.0, 20009.25)  # hold high + 0.25 no longer enough (needs + 0.50)
    assert s.sequence.outcome is O.FAILED
    s = Session(params=params)
    s.feed(*NEUTRAL)
    s.feed(*NEUTRAL)
    s.feed(*HOLD)  # bar 3 is now the final bar
    assert s.sequence.outcome is O.TIMED_OUT
    # No numeric trading constants in the module itself.
    literals = {
        n.value
        for n in ast.walk(ast.parse(inspect.getsource(confirmation)))
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
    }
    assert literals <= {0, 1}
    # The validator guards the parameters.
    spec["confirmation"]["max_bars_after_acceptance"] = 1
    spec["confirmation"]["parameters"]["confirmation_type"] = "CHASE_EXTENSION"
    invalid = {p.path for p in check_rule_freeze(spec).problems if p.kind == "INVALID"}
    assert {"confirmation.max_bars_after_acceptance", "confirmation.parameters.confirmation_type"} <= invalid


# =========================================================================== D-026 exclusivity


def test_hold_and_failure_inequalities_no_longer_overlap():
    s = Session()
    s.feed(20008.0, 20000.0, 20005.0)  # low exactly L: the lowest permissible long retest low
    assert s.sequence.hold is not None and s.sequence.outcome is None
    s = Session()
    s.feed(20008.0, 19999.75, 20005.0)  # one tick below L: failure, never a hold
    assert s.sequence.outcome is O.FAILED and s.sequence.hold is None
    s = Session("SHORT")
    s.feed(20004.0, 19993.0, 19996.0)  # high exactly U: permissible
    assert s.sequence.hold is not None
    s = Session("SHORT")
    s.feed(20004.25, 19993.0, 19996.0)  # one tick above U: failure
    assert s.sequence.outcome is O.FAILED and s.sequence.hold is None
