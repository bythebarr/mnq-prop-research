"""Missing- and bad-data classification for research replay (Rule Freeze Round 15).

COMPONENT OF A DRAFT SPECIFICATION. Pure classification, fail closed:

* A decision interval is ELIGIBLE only if every minute in it is either a
  present, validated bar or a VERIFIED_NO_TRADE_MINUTE. A known outage, an
  unexplained missing minute, or a bar rejected by validation (bad data, never
  repaired) makes the interval INELIGIBLE: no signal, the affected setup state
  resets (level_states treats it as an incomplete decision bar) and a
  completely fresh setup is required afterwards.
* A trading date is ELIGIBLE only if its required daily levels and calendars
  could be built reliably; otherwise the whole date is ineligible.
* During an open simulated position, any such interval makes the trade
  OPEN_POSITION_DATA_GAP_UNRESOLVED (see accounting.py): kept, unscorable,
  never given an invented exit.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from mnq_research.accounting import OPEN_POSITION_DATA_GAP
from mnq_research.data_contracts import MinuteStatus


class MinuteQuality(str, Enum):
    PRESENT_VALID = "PRESENT_VALID"
    VERIFIED_NO_TRADE_MINUTE = MinuteStatus.VERIFIED_NO_TRADE_MINUTE.value
    KNOWN_DATA_OUTAGE = MinuteStatus.KNOWN_DATA_OUTAGE.value
    UNEXPLAINED_MISSING_MINUTE = MinuteStatus.UNEXPLAINED_MISSING_MINUTE.value
    REJECTED_BAD_DATA = "REJECTED_BAD_DATA"  # impossible OHLC, off-tick, wrong contract, conflicting duplicate, zone/session error


USABLE = frozenset({MinuteQuality.PRESENT_VALID, MinuteQuality.VERIFIED_NO_TRADE_MINUTE})


class IntervalState(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE_FRESH_SETUP_REQUIRED = "INELIGIBLE_FRESH_SETUP_REQUIRED"


class DateState(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE_ENTIRE_DATE = "INELIGIBLE_ENTIRE_DATE"


def decision_interval_state(minutes: Iterable[MinuteQuality]) -> IntervalState:
    minutes = list(minutes)
    if not minutes or any(type(m) is not MinuteQuality for m in minutes):
        return IntervalState.INELIGIBLE_FRESH_SETUP_REQUIRED  # unknown or untyped state fails closed
    return IntervalState.ELIGIBLE if all(m in USABLE for m in minutes) else IntervalState.INELIGIBLE_FRESH_SETUP_REQUIRED


def trading_date_state(required_levels_reliable: bool, calendars_available: bool) -> DateState:
    ok = required_levels_reliable is True and calendars_available is True  # anything but an explicit True fails closed
    return DateState.ELIGIBLE if ok else DateState.INELIGIBLE_ENTIRE_DATE


def open_position_flags(minutes: Iterable[MinuteQuality]) -> tuple[str, ...]:
    """Data-quality flags for a trade whose open period contains these minutes."""
    return () if decision_interval_state(minutes) is IntervalState.ELIGIBLE else (OPEN_POSITION_DATA_GAP,)
