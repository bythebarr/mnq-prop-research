"""Commission and slippage model for Baseline B0 (Rule Freeze Round 15).

COMPONENT OF A DRAFT SPECIFICATION. Costs are versioned EXTERNAL assumptions
taken from the rule file:

* commission: a fixed amount per contract SIDE (every entered and every
  exited contract), times a commission scenario multiplier (BASE, 125 %,
  150 %). A round trip is exactly two sides.
* slippage: a whole number of ticks per order purpose, always ADVERSE (a buy
  fills higher, a sell fills lower), times a slippage scenario multiplier
  (1x, 2x, 3x). The limit target receives none; its fill is governed by the
  one-tick trade-through rule instead.

Slippage is applied ONCE, to a MODELLED fill's reference price (the first
otherwise eligible execution price). An authoritative ACTUAL fill keeps its
actual price; its realized slippage is measured separately. Scenarios change
fill prices and P&L only, never signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from mnq_research.protection import ExitOutcome, OrderAction
from mnq_research.structural_levels import TICK


class OrderPurpose(str, Enum):
    ENTRY_MARKET = "entry_market"
    PROTECTIVE_STOP_MARKET = "protective_stop_market"
    NORMAL_TIME_EXIT_MARKET = "normal_time_exit_market"
    NEWS_FLATTEN_MARKET = "news_flatten_market"
    SESSION_BACKSTOP_MARKET = "session_backstop_market"
    PROTECTION_FAILURE_MARKET = "protection_failure_market"
    ENTRY_INVALIDATION_MARKET = "entry_invalidation_market"
    MANUAL_SAFETY_MARKET = "manual_safety_market"
    TARGET_LIMIT = "target_limit"


# Which market order a flatten reason uses.
FLATTEN_PURPOSE = {
    ExitOutcome.NORMAL_TIME_EXIT: OrderPurpose.NORMAL_TIME_EXIT_MARKET,
    ExitOutcome.SCHEDULED_NEWS_FLATTEN: OrderPurpose.NEWS_FLATTEN_MARKET,
    ExitOutcome.SESSION_EMERGENCY_FLATTEN: OrderPurpose.SESSION_BACKSTOP_MARKET,
    ExitOutcome.PROTECTION_FAILURE_FLATTEN: OrderPurpose.PROTECTION_FAILURE_MARKET,
    ExitOutcome.ENTRY_INVALIDATION_FLATTEN: OrderPurpose.ENTRY_INVALIDATION_MARKET,
    ExitOutcome.MANUAL_SAFETY_FLATTEN: OrderPurpose.MANUAL_SAFETY_MARKET,
}


class FillOrigin(Enum):
    MODELED = "MODELED"  # historical simulation: modelled slippage is applied
    ACTUAL_RECORDED = "ACTUAL_RECORDED"  # authoritative real fill: never slipped again


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError(f"{name} must be a quoted decimal or a whole number, got {value!r}")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
    return number


@dataclass(frozen=True)
class CostModel:
    commission_per_side_usd: Decimal
    commission_round_trip_usd: Decimal
    commission_scenarios: Mapping[str, Decimal]
    slippage_ticks: Mapping[OrderPurpose, int]
    slippage_scenarios: Mapping[str, int]
    point_value_usd: Decimal

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "CostModel":
        c, s = spec["commissions"], spec["slippage"]
        per_side = _decimal(c["per_side_per_contract_usd"], "per_side_per_contract_usd")
        round_trip = _decimal(c["round_turn_per_contract_usd"], "round_turn_per_contract_usd")
        if per_side + per_side != round_trip:
            raise ValueError("round-trip commission must equal exactly two per-side commissions")
        commission_scenarios = {k: _decimal(v, f"commission scenario {k}") for k, v in c["stress_multipliers"].items()}
        ticks = dict(s["baseline_ticks"])
        if set(ticks) != {p.value for p in OrderPurpose}:
            raise ValueError(f"slippage.baseline_ticks must cover exactly {[p.value for p in OrderPurpose]}")
        slippage_ticks = {}
        for purpose in OrderPurpose:
            v = ticks[purpose.value]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"slippage ticks for {purpose.value} must be a whole number >= 0, got {v!r}")
            slippage_ticks[purpose] = v
        if slippage_ticks[OrderPurpose.TARGET_LIMIT] != 0:
            raise ValueError("the limit target receives no modelled slippage (one-tick trade-through rule instead)")
        slippage_scenarios = dict(s["stress_multipliers"])
        for k, v in slippage_scenarios.items():
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"slippage scenario {k} must be a whole number >= 1, got {v!r}")
        return cls(per_side, round_trip, commission_scenarios, slippage_ticks, slippage_scenarios,
                   Decimal(str(spec["instrument"]["point_value_usd"])))

    def slippage_points(self, purpose: OrderPurpose, slippage_scenario: str) -> Decimal:
        return self.slippage_ticks[purpose] * self.slippage_scenarios[slippage_scenario] * TICK

    def fill_price(
        self, reference_price: Decimal, action: OrderAction, purpose: OrderPurpose, slippage_scenario: str, origin: FillOrigin
    ) -> Decimal:
        """Adverse modelled slippage on a modelled fill; an actual fill is returned unchanged (never slipped twice)."""
        if type(origin) is not FillOrigin or type(action) is not OrderAction or type(purpose) is not OrderPurpose:
            raise TypeError("origin, action and purpose must be their enums")
        if origin is FillOrigin.ACTUAL_RECORDED:
            return reference_price
        points = self.slippage_points(purpose, slippage_scenario)
        return reference_price + points if action is OrderAction.BUY else reference_price - points

    def commission_usd(self, contract_sides: int, commission_scenario: str) -> Decimal:
        if isinstance(contract_sides, bool) or not isinstance(contract_sides, int) or contract_sides < 0:
            raise ValueError("contract sides must be a whole number >= 0")
        return self.commission_per_side_usd * contract_sides * self.commission_scenarios[commission_scenario]

    def round_trip_commission_usd(self, contracts: int, commission_scenario: str) -> Decimal:
        return self.commission_round_trip_usd * contracts * self.commission_scenarios[commission_scenario]
