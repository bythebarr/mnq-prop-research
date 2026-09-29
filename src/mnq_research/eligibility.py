"""Typed, fail-closed execution-eligibility state (D-028 decision 3).

COMPONENT OF A DRAFT SPECIFICATION. News, safety, session, data and position
controls must hand the geometry and entry layers a *typed* state for every
control. There is no free-form string, missing key, ``None`` or default
``False`` that can mean "safe to trade":

* every control is one of ``ControlState.CLEAR``, ``BLOCKED`` or ``UNKNOWN``;
* only ``CLEAR`` permits an entry, and only when EVERY control is ``CLEAR``;
* the constructor has no defaults and refuses any value that is not a
  ``ControlState`` (the plain string ``"CLEAR"`` is rejected);
* ``from_mapping`` turns a missing key or a wrongly typed value into
  ``UNKNOWN``, which blocks.

The producers of these states (the news calendar, safety monitor, data
validator and position reconciler) do not exist yet:
``execution_eligibility_integration.status`` stays
``REQUIRED_BEFORE_EXECUTABLE`` until they are integrated and verified.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Mapping


class ControlState(Enum):
    """Deliberately NOT a ``str`` enum, so a plain string can never compare equal."""

    CLEAR = "CLEAR"  # positively verified: this control does not block
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"  # not established -> fails closed


@dataclass(frozen=True)
class ExecutionEligibility:
    """One snapshot of every control that can stop a new entry. CLEAR = verified not blocking."""

    news_blackout: ControlState
    news_entry_protection: ControlState
    safety_halt: ControlState
    directional_conflict_halt: ControlState
    daily_entry_halt: ControlState  # e.g. set by an entry-order outcome (Round 13)
    session_valid: ControlState  # eligible date, inside the entry window, session not invalidated
    data_valid: ControlState  # no missing/incomplete/unreliable market data
    candidate_current: ControlState  # candidate not expired, not already used
    no_open_position: ControlState
    no_working_entry_order: ControlState
    contract_current: ControlState
    zones_current: ControlState

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if type(value) is not ControlState:
                raise TypeError(f"eligibility control {f.name!r} must be a ControlState, got {value!r}")

    @classmethod
    def control_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_mapping(cls, states: Mapping[str, Any]) -> "ExecutionEligibility":
        """Missing or wrongly typed entries become UNKNOWN (fail closed); unknown keys are refused."""
        extra = set(states) - set(cls.control_names())
        if extra:
            raise KeyError(f"unknown eligibility controls: {sorted(extra)}")
        return cls(
            **{
                name: states[name] if type(states.get(name)) is ControlState else ControlState.UNKNOWN
                for name in cls.control_names()
            }
        )

    @classmethod
    def all_clear(cls) -> "ExecutionEligibility":
        return cls(**{name: ControlState.CLEAR for name in cls.control_names()})

    def blocking_reasons(self) -> tuple[str, ...]:
        """Deterministic reasons in declaration order, e.g. ``BLOCKED:SAFETY_HALT``, ``UNKNOWN:DATA_VALID``."""
        return tuple(
            f"{getattr(self, name).value}:{name.upper()}"
            for name in self.control_names()
            if getattr(self, name) is not ControlState.CLEAR
        )

    @property
    def permits_entry(self) -> bool:
        return not self.blocking_reasons()
