"""Staged readiness gates (Rule Freeze Round 15).

Replaces the single all-or-nothing gate for RESEARCH milestones:

    SIGNAL_REPLAY < ONE_CONTRACT_BACKTEST < PROP_MONTE_CARLO < PAPER_FORWARD < LIVE_CONSIDERATION

Every field declares the earliest stage that needs it (``STAGE_PREFIXES``,
longest dotted-prefix match; defined HERE in code so that deleting a YAML field
cannot hide it). A stage may run only when its own fields AND every earlier
stage's fields are answered and valid, and when that stage and every earlier
stage carry a matching staged approval. Unresolved later-stage fields never
block an earlier research stage.

Each stage approval records a STAGE hash covering only the fields at or before
that stage, so answering a later-stage field never invalidates an earlier
approval, while editing an earlier-stage field always does.

A field with no declared stage is treated as SIGNAL_REPLAY (fail closed: it
blocks everything). LIVE_CONSIDERATION is the original full gate
(``check_rule_freeze``: every field, FROZEN_APPROVED, full approval record)
plus every staged approval. No staged approval permits broker connectivity.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mnq_research.hashing import hash_object
from mnq_research.validation import (
    APPROVAL_SECTIONS,
    STAGE_APPROVAL_SECTION,
    RuleFreezeProblem,
    _iter_leaves,
    check_rule_freeze,
    get_path,
    is_unresolved,
)


class Stage(str, Enum):
    SIGNAL_REPLAY = "SIGNAL_REPLAY"
    ONE_CONTRACT_BACKTEST = "ONE_CONTRACT_BACKTEST"
    PROP_MONTE_CARLO = "PROP_MONTE_CARLO"
    PAPER_FORWARD = "PAPER_FORWARD"
    LIVE_CONSIDERATION = "LIVE_CONSIDERATION"

    @property
    def rank(self) -> int:
        return list(Stage).index(self)


STAGED = (Stage.SIGNAL_REPLAY, Stage.ONE_CONTRACT_BACKTEST, Stage.PROP_MONTE_CARLO, Stage.PAPER_FORWARD)

S, O, P, F = Stage.SIGNAL_REPLAY, Stage.ONE_CONTRACT_BACKTEST, Stage.PROP_MONTE_CARLO, Stage.PAPER_FORWARD

# Earliest stage that needs each field (longest dotted prefix wins).
STAGE_PREFIXES: dict[str, Stage] = {
    # --- SIGNAL_REPLAY: frozen signal logic, instrument, sessions, levels, data, calendars
    "specification": S,
    "instrument": S,
    "contract_roll": S,
    "source_data": S,
    "timezone_policy": S,
    "sessions": S,
    "decision_clock": S,
    "eligible_trading_dates": S,
    "news_events": S,
    "setup": S,
    "direction": S,
    "market_structure": S,
    "structural_levels": S,
    "level_states": S,
    "acceptance_rejection_breakout": S,
    "confirmation": S,
    "room_to_target": S,
    "trade_geometry": S,
    "entry_trigger": S,
    "ema": S,
    "vwap": S,
    "missing_data.policy": S,
    "missing_data.max_tolerated_gap_minutes": S,
    "bad_data": S,
    "execution_eligibility_integration": S,
    "research_pipeline": S,
    # --- ONE_CONTRACT_BACKTEST: execution simulation, exits, costs, accounting
    "news_events.open_position_during_event": O,
    "execution_eligibility_integration.simulated_execution_producers_status": O,
    "research_pipeline.execution_simulation_wiring_status": O,
    "order_type": O,
    "order_validity": O,
    "entry_order_lifecycle": O,
    "structural_invalidation": O,
    "stop_placement": O,
    "target_placement": O,
    "protective_orders": O,
    "intrabar_ambiguity": O,
    "missing_data.open_position_during_gap": O,
    "commissions": O,
    "slippage": O,
    "session_flattening": O,
    "position_management": O,
    "daily_limits": O,
    "reentry": O,
    "no_trade_conditions.execution_safety_conditions": O,
    "trade_accounting": O,
    # --- PROP_MONTE_CARLO: Tradeify rules, sizing, account-level loss constraints
    "prop_account_rules": P,
    "position_management.contracts_per_trade": P,
    "position_management.sizing_method": P,
    "position_management.risk_per_trade": P,
    "position_management.position_sizing_balance_basis": P,
    "position_management.contract_rounding": P,
    "position_management.below_one_contract_action": P,
    "position_management.sizing_never": P,
    "position_management.max_contracts_per_trade": P,
    "daily_limits.daily_loss_stop_usd": P,
    "no_trade_conditions.risk_constraint_conditions": P,
    # --- PAPER_FORWARD: adapter capability, live producers
    "protective_orders.deployment": F,
    "execution_eligibility_integration.live_producers_status": F,
}


def stage_of(path: str) -> Stage:
    """Earliest stage needing ``path``; undeclared fields fail closed to SIGNAL_REPLAY."""
    parts = path.replace("[", ".[").split(".")
    for n in range(len(parts), 0, -1):
        stage = STAGE_PREFIXES.get(".".join(parts[:n]))
        if stage is not None:
            return stage
    return Stage.SIGNAL_REPLAY


def _covered(stage: Stage) -> list[Stage]:
    return [s for s in STAGED if s.rank <= stage.rank]


def stage_hash(spec: dict[str, Any], stage: Stage) -> str:
    """Hash of every field needed at or before ``stage`` (approval sections excluded)."""
    content = {
        path: value
        for path, value in _iter_leaves({k: v for k, v in spec.items() if k not in APPROVAL_SECTIONS}, "")
        if stage_of(path).rank <= stage.rank
    }
    return hash_object({"stage": stage.value, "fields": content})


def _is_tz_aware(value: Any) -> bool:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


@dataclass
class StageReport:
    stage: Stage
    problems: list[RuleFreezeProblem] = field(default_factory=list)
    stage_hashes: dict[str, str] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return not self.problems

    def format(self) -> str:
        lines = [f"READINESS: {self.stage.value}", f"  Result: {'READY' if self.is_ready else 'NOT READY'} ({len(self.problems)} problem(s))"]
        for name, digest in self.stage_hashes.items():
            lines.append(f"  {name} stage hash: {digest}")
        lines += [f"  - {p.path}: {p.detail}" for p in self.problems]
        return "\n".join(lines)


def check_stage(spec: dict[str, Any], stage: Stage) -> StageReport:
    """Every problem blocking ``stage``: its own and earlier fields, and every required staged approval."""
    if type(stage) is not Stage:
        raise TypeError("stage must be a Stage")
    report = StageReport(stage)
    full = check_rule_freeze(spec)
    if stage is Stage.LIVE_CONSIDERATION:
        report.problems.extend(full.problems)  # every field, FROZEN_APPROVED and the full approval record
    else:
        report.problems.extend(
            p for p in full.problems if p.kind not in ("STATUS", "APPROVAL") and stage_of(p.path).rank <= stage.rank
        )
    approvals = spec.get(STAGE_APPROVAL_SECTION)
    for s in _covered(stage):
        digest = stage_hash(spec, s)
        report.stage_hashes[s.value] = digest
        record = approvals.get(s.value) if isinstance(approvals, dict) else None
        base = f"{STAGE_APPROVAL_SECTION}.{s.value}"
        if not isinstance(record, dict):
            report.problems.append(RuleFreezeProblem(base, "APPROVAL", "staged approval record is missing"))
            continue
        if record.get("approved") is not True:
            report.problems.append(RuleFreezeProblem(f"{base}.approved", "APPROVAL", f"{s.value} has not been approved"))
        if is_unresolved(record.get("approved_by")):
            report.problems.append(RuleFreezeProblem(f"{base}.approved_by", "APPROVAL", "approver not recorded"))
        if not _is_tz_aware(record.get("approved_at_utc")):
            report.problems.append(RuleFreezeProblem(f"{base}.approved_at_utc", "APPROVAL", "a time-zone-aware approval time is required"))
        if record.get("approved_stage_hash") != digest:
            report.problems.append(
                RuleFreezeProblem(f"{base}.approved_stage_hash", "APPROVAL", f"does not match the current {s.value} stage hash {digest[:12]}...")
            )
    return report


def broker_connectivity_permitted(spec: dict[str, Any]) -> bool:
    """Never through a research approval: PAPER_FORWARD in full AND live/paper submission explicitly permitted."""
    submission = get_path(spec, "entry_order_lifecycle.protective_order_dependency.live_or_paper_order_submission")
    return check_stage(spec, Stage.PAPER_FORWARD).is_ready and submission != "prohibited"
