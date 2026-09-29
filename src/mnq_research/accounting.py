"""Trade accounting for Baseline B0 (Rule Freeze Round 15).

COMPONENT OF A DRAFT SPECIFICATION. Turns one completed (or data-gapped)
simulated trade into a reproducible record:

* fill prices: modelled legs get adverse modelled slippage for the chosen
  slippage scenario; actual recorded legs keep their prices (never slipped
  twice);
* gross P&L per fill leg (long: exit - entry, short: entry - exit) x $/point x
  quantity, exact fractions;
* commissions per contract SIDE for every entered and exited contract, times
  the commission scenario;
* net P&L = gross - commissions (slippage is inside the prices already);
* planned risk kept separately from actual initial risk (price risk + modelled
  stop slippage + estimated round-trip commission); net R and price-only R;
* MFE/MAE from the first authoritative entry fill to flat, never clamped,
  with times to each and time in trade;
* an OPEN_POSITION_DATA_GAP_UNRESOLVED trade is kept, marked unscorable,
  excluded from primary statistics, and priced separately in a LABELLED
  full-loss gap-stress scenario.

Scenarios change fill prices and P&L only: signals, timestamps and
identifiers are inputs and are never altered here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Any, Iterable, Mapping

import pandas as pd

from mnq_research.confirmation import Side
from mnq_research.costs import CostModel, FillOrigin, OrderPurpose
from mnq_research.protection import OrderAction, PriceInterval, PriceSource

OPEN_POSITION_DATA_GAP = "OPEN_POSITION_DATA_GAP_UNRESOLVED"
GAP_STRESS_LABEL = "GAP_STRESS_FULL_LOSS_ASSUMPTION_NOT_PRIMARY"
ENTRY_MINUTE_FLAG = "ENTRY_MINUTE_EXCURSION_APPROXIMATION"
MAE_ENTRY_MINUTE_POLICY = "INCLUDE_FULL_MINUTE_CONSERVATIVE"
MFE_ENTRY_MINUTE_POLICY = "EXCLUDE_FULL_MINUTE_PRIMARY"
BASE_SCENARIO = ("BASE_SLIPPAGE", "BASE")
REQUIRED_HASHES = ("specification_hash", "code_hash", "data_hash", "calendar_hash")


class LegType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


@dataclass(frozen=True)
class FillLeg:
    timestamp_utc: pd.Timestamp
    leg_type: LegType
    quantity: int
    reference_price: Decimal  # first otherwise eligible execution price (modelled) or the actual fill price
    purpose: OrderPurpose
    origin: FillOrigin
    order_id: str
    exit_type: str | None = None


@dataclass(frozen=True)
class TradeInputs:
    trading_date: Any
    contract: str
    direction: Side
    identifiers: Mapping[str, str]  # setup/zone/attempt/acceptance/confirmation/candidate/order IDs
    entry_legs: tuple[FillLeg, ...]
    exit_legs: tuple[FillLeg, ...]
    structural_stop: Decimal
    target: Decimal
    planned_risk_points: Decimal
    intended_quantity: int
    exit_reason: str | None
    final_flattening_leg: str | None
    observations: tuple[PriceInterval, ...]
    flat_utc: pd.Timestamp | None
    data_quality_flags: tuple[str, ...]
    hashes: Mapping[str, str]


@dataclass(frozen=True)
class TradeRecord:
    values: Mapping[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


def _avg(legs: list[tuple[int, Fraction]]) -> Fraction:
    quantity = sum(q for q, _ in legs)
    return sum((p * q for q, p in legs), Fraction(0)) / quantity


def account_trade(inputs: TradeInputs, costs: CostModel, slippage_scenario: str, commission_scenario: str) -> TradeRecord:
    missing = [h for h in REQUIRED_HASHES if not inputs.hashes.get(h)]
    if missing:
        raise ValueError(f"every trade must carry reproducibility hashes; missing {missing}")
    if not inputs.entry_legs:
        raise ValueError("a trade needs at least one authoritative entry fill")
    long = inputs.direction is Side.LONG
    sign = Fraction(1) if long else -Fraction(1)
    entry_action, exit_action = (OrderAction.BUY, OrderAction.SELL) if long else (OrderAction.SELL, OrderAction.BUY)
    pv = Fraction(costs.point_value_usd)

    def price(leg: FillLeg, action: OrderAction) -> Fraction:
        return Fraction(costs.fill_price(leg.reference_price, action, leg.purpose, slippage_scenario, leg.origin))

    entries = [(leg.quantity, price(leg, entry_action)) for leg in inputs.entry_legs]
    exits = [(leg.quantity, price(leg, exit_action), leg) for leg in inputs.exit_legs]
    entry_qty = sum(q for q, _ in entries)
    exit_qty = sum(q for q, _, _ in exits)
    avg_entry = _avg(entries)
    first_fill = min(leg.timestamp_utc for leg in inputs.entry_legs)
    gap = OPEN_POSITION_DATA_GAP in inputs.data_quality_flags
    if not gap and exit_qty != entry_qty:
        raise ValueError(f"exits ({exit_qty}) must equal entries ({entry_qty}) unless the trade is flagged {OPEN_POSITION_DATA_GAP}")
    if exit_qty > entry_qty:
        raise ValueError("exit quantity exceeds entry quantity")

    stop = Fraction(inputs.structural_stop)
    price_risk_points = abs(avg_entry - stop)
    actual_price_risk_usd = price_risk_points * pv * entry_qty
    stop_slip_usd = Fraction(costs.slippage_points(OrderPurpose.PROTECTIVE_STOP_MARKET, slippage_scenario)) * pv * entry_qty
    est_rt_commission = Fraction(costs.round_trip_commission_usd(entry_qty, commission_scenario))
    actual_initial_risk_usd = actual_price_risk_usd + stop_slip_usd + est_rt_commission
    if actual_initial_risk_usd <= 0:
        raise ValueError("actual initial risk must be positive")

    legs_out = []
    gross_points_total = Fraction(0)
    for q, p, leg in exits:
        pts = sign * (p - avg_entry) * q
        gross_points_total += pts
        legs_out.append({"order_id": leg.order_id, "timestamp_utc": leg.timestamp_utc.isoformat(), "exit_type": leg.exit_type,
                         "quantity": q, "price": str(p), "gross_points": str(pts), "gross_pnl_usd": str(pts * pv)})
    commissions = Fraction(costs.commission_usd(entry_qty + exit_qty, commission_scenario))
    scorable = not gap
    gross_usd = gross_points_total * pv if scorable else None
    net_usd = gross_usd - commissions if scorable else None

    # Excursions: authoritative observations from the first fill to flat (never clamped to stop/target).
    end = inputs.flat_utc if inputs.flat_utc is not None else max((leg.timestamp_utc for leg in inputs.exit_legs), default=None)
    usable = [o for o in inputs.observations if o.source is PriceSource.AUTHORITATIVE_TRADES and (end is None or o.start_utc <= end)]
    entry_bar = next((o for o in usable if o.end_utc is not None and o.start_utc < first_fill < o.end_utc), None)
    after = sorted((o for o in usable if o.start_utc >= first_fill), key=lambda o: o.start_utc)
    finer_around_fill = entry_bar is not None and any(o.start_utc < entry_bar.end_utc and o.end_utc is None for o in after)
    flags = list(inputs.data_quality_flags)
    mfe = mae = t_mfe = t_mae = mfe_optimistic = None
    if entry_bar is not None and finer_around_fill:
        entry_bar = None  # authoritative finer data establish the order: measure from the fill itself
    fav = (lambda o: Fraction(o.high)) if long else (lambda o: -Fraction(o.low))  # larger = more favourable
    adv = (lambda o: -Fraction(o.low)) if long else (lambda o: Fraction(o.high))  # larger = more adverse
    if after or entry_bar is not None:
        if after:
            best = max(after, key=fav)
            mfe = sign * (Fraction(best.high if long else best.low) - avg_entry)
            t_mfe = best.start_utc - first_fill
        conservative = after + ([entry_bar] if entry_bar is not None else [])
        worst = max(conservative, key=adv)
        mae = sign * (avg_entry - Fraction(worst.low if long else worst.high))
        t_mae = max(worst.start_utc, first_fill) - first_fill
        if entry_bar is not None:
            flags.append(ENTRY_MINUTE_FLAG)
            optimistic = max(after + [entry_bar], key=fav)
            mfe_optimistic = sign * (Fraction(optimistic.high if long else optimistic.low) - avg_entry)
        if any(o.high != o.low for o in after):
            flags.append("EXCURSION_TIMING_APPROXIMATE")
    else:
        flags.append("NO_EXCURSION_OBSERVATIONS")

    def r(points: Fraction | None) -> str | None:
        return None if points is None or price_risk_points == 0 else str(points / price_risk_points)

    avg_exit = _avg([(q, p) for q, p, _ in exits]) if exits else None
    values = {
        "trading_date": str(inputs.trading_date),
        "contract": inputs.contract,
        "direction": inputs.direction.value,
        "identifiers": dict(inputs.identifiers),
        "entry_legs": [{"order_id": l.order_id, "timestamp_utc": l.timestamp_utc.isoformat(), "quantity": q, "price": str(p)}
                       for l, (q, p) in zip(inputs.entry_legs, entries)],
        "exit_legs": legs_out,
        "entry_quantity": entry_qty,
        "actual_average_entry": str(avg_entry),
        "actual_average_exit": None if avg_exit is None else str(avg_exit),
        "stop": str(inputs.structural_stop),
        "target": str(inputs.target),
        "gross_points": str(gross_points_total) if scorable else None,
        "gross_pnl_usd": None if gross_usd is None else str(gross_usd),
        "commissions_usd": str(commissions),
        "net_pnl_usd": None if net_usd is None else str(net_usd),
        "planned_risk_points": str(inputs.planned_risk_points),
        "planned_risk_usd_before_costs": str(Fraction(inputs.planned_risk_points) * pv * inputs.intended_quantity),
        "actual_price_risk_usd": str(actual_price_risk_usd),
        "modeled_stop_slippage_risk_usd": str(stop_slip_usd),
        "estimated_round_trip_commission_usd": str(est_rt_commission),
        "actual_initial_risk_usd": str(actual_initial_risk_usd),
        "result_r_price_only": None if gross_usd is None or actual_price_risk_usd == 0 else str(gross_usd / actual_price_risk_usd),
        "result_r_net": None if net_usd is None else str(net_usd / actual_initial_risk_usd),
        "mfe_points": None if mfe is None else str(mfe),
        "mae_points": None if mae is None else str(mae),
        "mfe_optimistic_bound_points": None if mfe_optimistic is None else str(mfe_optimistic),
        "mae_entry_minute_policy": MAE_ENTRY_MINUTE_POLICY,
        "mfe_entry_minute_policy": MFE_ENTRY_MINUTE_POLICY,
        "mfe_r": r(mfe),
        "mae_r": r(mae),
        "time_to_mfe": None if t_mfe is None else str(t_mfe),
        "time_to_mae": None if t_mae is None else str(t_mae),
        "time_in_trade": None if inputs.flat_utc is None else str(inputs.flat_utc - first_fill),
        "exit_reason": inputs.exit_reason,
        "final_flattening_leg": inputs.final_flattening_leg,
        "slippage_scenario": slippage_scenario,
        "commission_scenario": commission_scenario,
        "result_basis": "BASE_ASSUMPTIONS" if (slippage_scenario, commission_scenario) == BASE_SCENARIO else "HYPOTHETICAL_STRESS",
        "data_quality_flags": flags,
        "scorable": scorable,
        "gap_stress_exit_price": None if scorable else str(stop - sign * Fraction(costs.slippage_points(OrderPurpose.PROTECTIVE_STOP_MARKET, slippage_scenario))),
        "gap_stress_commissions_usd": None if scorable else str(est_rt_commission),
        "gap_stress_net_pnl_usd": None if scorable else str(-actual_initial_risk_usd),
        "gap_stress_label": None if scorable else GAP_STRESS_LABEL,
        "hashes": dict(inputs.hashes),
    }
    return TradeRecord(values)


def account_all_scenarios(inputs: TradeInputs, costs: CostModel) -> dict[tuple[str, str], TradeRecord]:
    """Every slippage x commission scenario for the SAME trade (signals are inputs, never changed)."""
    return {
        (s, c): account_trade(inputs, costs, s, c)
        for s in costs.slippage_scenarios
        for c in costs.commission_scenarios
    }


def summarize(records: Iterable[TradeRecord]) -> dict[str, Any]:
    """Primary statistics use scorable trades only; unscorable gaps are reported separately and in a labelled stress."""
    records = list(records)
    primary = [r for r in records if r["scorable"]]
    gaps = [r for r in records if not r["scorable"]]
    primary_net = sum((Fraction(r["net_pnl_usd"]) for r in primary), Fraction(0))
    stress = sum((Fraction(r["gap_stress_net_pnl_usd"]) for r in gaps), Fraction(0))
    return {
        "primary_trades": len(primary),
        "primary_wins": sum(Fraction(r["net_pnl_usd"]) > 0 for r in primary),
        "primary_losses": sum(Fraction(r["net_pnl_usd"]) <= 0 for r in primary),
        "primary_net_pnl_usd": str(primary_net),
        "unscorable_open_position_gaps": len(gaps),
        "gap_stress_label": GAP_STRESS_LABEL,
        "gap_stress_net_pnl_usd": str(primary_net + stress),
    }
