"""Deployment position-sizing MECHANICS for Baseline B0 (D-029 decision 6).

COMPONENT OF A DRAFT SPECIFICATION. Only the rounding and below-one-contract
mechanics are frozen:

    contracts = floor(allowed_planned_account_risk_usd / modeled_risk_per_contract_usd)
    contracts < 1  ->  POSITION_SIZE_BELOW_ONE_CONTRACT, SKIP_TRADE

Never round up, force one contract, compress the stop, increase the allowed
risk, or carry fractional contracts.

The allowed dollar risk and its balance basis are still UNRESOLVED
(``position_management.risk_per_trade`` / ``position_sizing_balance_basis``),
and the modelled risk per contract needs the frozen cost model. Nothing in the
research pipeline calls this yet: research simulations use the separate
one-contract RESEARCH_QUANTITY_ONLY value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

CONTRACT_ROUNDING = "FLOOR_TO_WHOLE_CONTRACT"
BELOW_ONE_CONTRACT_ACTION = "SKIP_TRADE"
BELOW_ONE_CONTRACT_REASON = "POSITION_SIZE_BELOW_ONE_CONTRACT"


class SizingAction(str, Enum):
    TRADE = "TRADE"
    SKIP_TRADE = "SKIP_TRADE"


@dataclass(frozen=True)
class SizingDecision:
    contracts: int
    action: SizingAction
    reason: str | None


def check_sizing_rules(spec: Mapping[str, Any]) -> None:
    """Refuse any specification whose sizing mechanics differ from the ones implemented here."""
    pm = spec["position_management"]
    if pm["contract_rounding"] != CONTRACT_ROUNDING or pm["below_one_contract_action"] != BELOW_ONE_CONTRACT_ACTION:
        raise ValueError(f"B0 implements only {CONTRACT_ROUNDING} and {BELOW_ONE_CONTRACT_ACTION}")


def size_position(allowed_planned_account_risk_usd: Decimal, modeled_risk_per_contract_usd: Decimal) -> SizingDecision:
    """Whole contracts, rounded DOWN; fewer than one means the trade is skipped."""
    for name, value in (("allowed", allowed_planned_account_risk_usd), ("per_contract", modeled_risk_per_contract_usd)):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise TypeError(f"{name} risk must be a finite Decimal, got {value!r}")
    if allowed_planned_account_risk_usd < 0 or modeled_risk_per_contract_usd <= 0:
        raise ValueError("allowed risk must be >= 0 and modelled risk per contract > 0")
    contracts = int(allowed_planned_account_risk_usd // modeled_risk_per_contract_usd)  # both positive: floor
    if contracts < 1:
        return SizingDecision(0, SizingAction.SKIP_TRADE, BELOW_ONE_CONTRACT_REASON)
    return SizingDecision(contracts, SizingAction.TRADE, None)
