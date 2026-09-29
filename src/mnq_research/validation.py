"""Rule-freeze validation: decides whether a rule specification may execute.

A specification is EXECUTABLE only when ALL of these hold:

1. ``specification.status`` is ``FROZEN_APPROVED``;
2. every required field exists and has an explicit answer (not ``null``,
   not ``TBD``, not empty);
3. no other field anywhere in the file is left as ``TBD``;
4. fields fixed by the data contract still hold their fixed values;
5. ``approval_record`` is complete, and its ``approved_spec_hash`` equals the
   current hash of the specification (so any edit after approval blocks
   execution until it is re-approved).

The checker never stops at the first problem: it collects every problem and
reports them together, so the full list of open questions is always visible.

The list of required fields is defined HERE, in code, rather than in the YAML
file. Deleting a field from the YAML therefore produces a "missing" problem
instead of silently passing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mnq_research.config import load_mapping
from mnq_research.data_contracts import EXCHANGE_TIMEZONE, REQUIRED_COLUMNS
from mnq_research.hashing import hash_object
from mnq_research.confirmation import CONFIRMATION_TYPE, CONTINUATION_REQUIREMENT, RETEST_DISTANCE_METHOD
from mnq_research.level_states import APPROACH_METHOD
from mnq_research.structural_levels import B0_LEVEL_TYPES

STATUS_DRAFT = "DRAFT_NON_EXECUTABLE"
STATUS_FROZEN = "FROZEN_APPROVED"
ALLOWED_STATUSES = (STATUS_DRAFT, STATUS_FROZEN)

APPROVAL_SECTION = "approval_record"

# Every field that must hold an explicit, approved answer before execution.
# Keep in sync with configs/rule_freeze_v1.yaml and docs/RULE_FREEZE_GUIDE.md
# (tests enforce both).
REQUIRED_FIELDS: tuple[str, ...] = (
    # Specification identity
    "specification.spec_id",
    "specification.version",
    "specification.strategy_name",
    "specification.author",
    "specification.plain_english_summary",
    # Instrument and contract selection
    "instrument.root_symbol",
    "instrument.exchange",
    "instrument.tick_size_points",
    "instrument.tick_value_usd",
    "instrument.point_value_usd",
    "instrument.contract_selection_method",
    # Contract roll
    "contract_roll.roll_trigger",
    "contract_roll.roll_timing",
    "contract_roll.price_series_adjustment",
    "contract_roll.open_position_at_roll",
    # Source data
    "source_data.vendor",
    "source_data.dataset_name",
    "source_data.base_bar_interval",
    "source_data.vendor_timestamp_convention",
    "source_data.vendor_timezone",
    "source_data.history_start_date",
    "source_data.history_end_date",
    "source_data.higher_resolution_data_available",
    "source_data.required_fields",
    # Time zones
    "timezone_policy.storage_timezone",
    "timezone_policy.exchange_timezone",
    "timezone_policy.rule_timezone",
    "timezone_policy.dst_handling",
    # Sessions
    "sessions.trading_window_start",
    "sessions.trading_window_end",
    "sessions.reference_windows",
    "sessions.early_close_handling",
    # Decision clock
    "decision_clock.decision_bar_interval",
    "decision_clock.decision_point",
    "decision_clock.signal_to_order_delay",
    "decision_clock.bar_aggregation_rule",
    # Eligible dates
    "eligible_trading_dates.allowed_weekdays",
    "eligible_trading_dates.holiday_and_half_day_policy",
    "eligible_trading_dates.roll_week_policy",
    "eligible_trading_dates.calendar_source",
    # News
    "news_events.policy",
    "news_events.event_types",
    "news_events.blackout_minutes_before",
    "news_events.blackout_minutes_after",
    "news_events.entry_protection_buffer_before_blackout_minutes",
    "news_events.open_position_during_event",
    "news_events.calendar_source",
    # Setup and direction
    "setup.definition",
    "setup.preconditions",
    "setup.setup_expiry",
    "direction.long_conditions",
    "direction.short_conditions",
    "direction.conflict_resolution",
    # Structural levels
    "structural_levels.level_types",
    "structural_levels.calculation_method",
    "structural_levels.level_expiry",
    "structural_levels.level_proximity_tolerance",
    "structural_levels.missing_data_treatment",
    "structural_levels.price_validation",
    "structural_levels.clustering_method",
    "structural_levels.decision_use",
    # Level states (Round 9)
    "level_states.parameters",
    "level_states.state_storage",
    "level_states.incomplete_bar_handling",
    "level_states.initialization",
    "level_states.after_acceptance",
    "level_states.directional_episodes",
    "level_states.event_priority",
    # Market structure
    "market_structure.structure_bar_interval",
    "market_structure.swing_point_definition",
    "market_structure.bullish_progression_definition",
    "market_structure.bearish_progression_definition",
    "market_structure.mixed_structure_handling",
    # Acceptance / rejection / breakout
    "acceptance_rejection_breakout.measurement_basis",
    "acceptance_rejection_breakout.acceptance_definition",
    "acceptance_rejection_breakout.rejection_definition",
    "acceptance_rejection_breakout.breakout_definition",
    # Confirmation
    "confirmation.definition",
    "confirmation.max_bars_after_acceptance",
    "confirmation.failure_handling",
    "confirmation.parameters",
    "confirmation.clock",
    "confirmation.acceptance_lifetime",
    # Room to target
    "room_to_target.measurement_method",
    "room_to_target.minimum_room",
    "room_to_target.obstruction_definition",
    # EMA (dependent fields only required if ema.included is true)
    "ema.included",
    "ema.period",
    "ema.price_source",
    "ema.bar_interval",
    "ema.seed_method",
    "ema.warmup_bars",
    "ema.session_reset",
    "ema.role_in_rules",
    # VWAP (dependent fields only required if vwap.included is true)
    "vwap.included",
    "vwap.anchor",
    "vwap.price_source",
    "vwap.volume_source",
    "vwap.bands",
    "vwap.role_in_rules",
    # Entry and orders
    "entry_trigger.long_trigger",
    "entry_trigger.short_trigger",
    "order_type.entry_order_type",
    "order_type.entry_price_rule",
    "order_validity.time_in_force",
    "order_validity.cancellation_conditions",
    # Invalidation, stop, target
    "structural_invalidation.definition",
    "structural_invalidation.action",
    "stop_placement.method",
    "stop_placement.buffer_ticks",
    "stop_placement.minimum_stop_points",
    "stop_placement.maximum_stop_points",
    "stop_placement.trade_skipped_if_outside_limits",
    "target_placement.method",
    "target_placement.parameters",
    # Position management
    "position_management.contracts_per_trade",
    "position_management.sizing_method",
    "position_management.scaling_in_out",
    "position_management.breakeven_rule",
    "position_management.trailing_stop_rule",
    "position_management.time_based_exit",
    "position_management.risk_per_trade",
    "position_management.risk_reference_balance",
    "position_management.contract_rounding",
    "position_management.below_one_contract_action",
    "position_management.max_contracts_per_trade",
    # Daily limits and re-entry
    "daily_limits.max_trades_per_day",
    "daily_limits.max_losing_trades_per_day",
    "daily_limits.daily_loss_stop_usd",
    "no_trade_conditions.execution_safety_conditions",
    "no_trade_conditions.risk_constraint_conditions",
    "reentry.allowed",
    "reentry.conditions",
    "reentry.cooldown_minutes",
    # Flattening
    "session_flattening.no_new_entries_after",
    "session_flattening.flatten_all_by",
    "session_flattening.flatten_order_type",
    "session_flattening.emergency_flatten",
    # Intrabar ambiguity
    "intrabar_ambiguity.stop_and_target_same_bar",
    "intrabar_ambiguity.entry_and_exit_same_bar",
    "intrabar_ambiguity.resolution_with_finer_data",
    "intrabar_ambiguity.fill_approximation_without_finer_data",
    # Missing / bad data
    "missing_data.policy",
    "missing_data.max_tolerated_gap_minutes",
    "missing_data.open_position_during_gap",
    "bad_data.policy",
    "bad_data.handling_of_rejected_bars",
    # Costs
    "commissions.round_turn_per_contract_usd",
    "commissions.includes_exchange_clearing_nfa_fees",
    "commissions.source_and_date",
    "slippage.entry_ticks",
    "slippage.stop_exit_ticks",
    "slippage.target_exit_ticks",
    "slippage.market_exit_ticks",
    "slippage.stress_test_multipliers",
    # Prop-account rules
    "prop_account_rules.firm",
    "prop_account_rules.program_name",
    "prop_account_rules.rules_document_version",
    "prop_account_rules.rules_retrieved_date",
    "prop_account_rules.account_size_usd",
    "prop_account_rules.profit_target_usd",
    "prop_account_rules.max_loss_limit_usd",
    "prop_account_rules.drawdown_type",
    "prop_account_rules.daily_loss_limit_usd",
    "prop_account_rules.max_contracts",
    "prop_account_rules.consistency_rule",
    "prop_account_rules.minimum_trading_days",
    "prop_account_rules.payout_rules",
    "prop_account_rules.trading_restrictions",
)

# Approval-record fields, checked separately (they depend on the content hash).
APPROVAL_FIELDS: tuple[str, ...] = (
    "approval_record.approved",
    "approval_record.approved_by",
    "approval_record.approved_at_utc",
    "approval_record.approved_spec_hash",
)

# Sections whose other fields are only required when their switch is true.
CONDITIONAL_SECTIONS: dict[str, str] = {"ema": "ema.included", "vwap": "vwap.included"}

# Values fixed by the data contract. Changing them requires changing the data
# contract (and this code) deliberately, not just the YAML file.
FIXED_VALUES: dict[str, Any] = {
    "specification.spec_id": "MNQ-RULE-FREEZE",
    "instrument.root_symbol": "MNQ",
    "source_data.base_bar_interval": "1min",
    "source_data.required_fields": list(REQUIRED_COLUMNS),
    "timezone_policy.storage_timezone": "UTC",
    "timezone_policy.exchange_timezone": EXCHANGE_TIMEZONE,
}

# Fields whose answer must be a whole number >= 0.
NON_NEGATIVE_INT_FIELDS: frozenset[str] = frozenset(
    {
        "news_events.blackout_minutes_before",
        "news_events.blackout_minutes_after",
        "news_events.entry_protection_buffer_before_blackout_minutes",
    }
)

# Fields whose answer must be a true/false value.
BOOLEAN_FIELDS: frozenset[str] = frozenset({"ema.included", "vwap.included", "approval_record.approved"})

_MISSING = object()


class RuleFreezeNotExecutableError(Exception):
    """Raised when code tries to execute a rule specification that is not frozen."""

    def __init__(self, report: "RuleFreezeReport"):
        self.report = report
        super().__init__(
            f"Rule specification is NOT executable: {len(report.problems)} problem(s). "
            "Run `uv run mnq rules check` for the full list."
        )


@dataclass(frozen=True)
class RuleFreezeProblem:
    path: str
    kind: str  # MISSING | UNRESOLVED | INVALID | STATUS | APPROVAL
    detail: str


@dataclass
class RuleFreezeReport:
    spec_path: str | None
    status: Any
    spec_hash: str
    problems: list[RuleFreezeProblem] = field(default_factory=list)

    @property
    def is_executable(self) -> bool:
        return not self.problems

    def unresolved_paths(self) -> list[str]:
        return [p.path for p in self.problems if p.kind in ("MISSING", "UNRESOLVED")]

    def format(self) -> str:
        lines = [
            "RULE FREEZE CHECK",
            f"  File:        {self.spec_path or '<in memory>'}",
            f"  Status:      {self.status}",
            f"  Spec hash:   {self.spec_hash}",
            "               (canonical hash of everything except approval_record; this is the",
            "                value approval_record.approved_spec_hash must contain)",
        ]
        if self.is_executable:
            lines.append("  Result:      EXECUTABLE (frozen, complete and approval hash matches)")
            return "\n".join(lines)
        unresolved = self.unresolved_paths()
        lines += [
            "  Result:      NOT EXECUTABLE",
            f"  Problems:    {len(self.problems)} total, of which {len(unresolved)} are unanswered/missing fields",
            "",
        ]
        by_kind: dict[str, list[RuleFreezeProblem]] = {}
        for problem in self.problems:
            by_kind.setdefault(problem.kind, []).append(problem)
        titles = {
            "STATUS": "Status",
            "MISSING": "Required fields that are missing from the file",
            "UNRESOLVED": "Fields still unanswered (TBD / null / empty)",
            "INVALID": "Fields with invalid values",
            "APPROVAL": "Approval record",
        }
        for kind in ("STATUS", "MISSING", "UNRESOLVED", "INVALID", "APPROVAL"):
            items = by_kind.get(kind, [])
            if not items:
                continue
            lines.append(f"{titles[kind]} ({len(items)}):")
            for problem in items:
                lines.append(f"  - {problem.path}: {problem.detail}")
            lines.append("")
        lines.append(
            "Each unanswered field is explained, with an illustrative example, in docs/RULE_FREEZE_GUIDE.md."
        )
        return "\n".join(lines).rstrip()


def is_unresolved(value: Any) -> bool:
    """True if a value is an unanswered placeholder (None, TBD, empty)."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().upper() == "TBD"
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def get_path(spec: Any, dotted: str) -> Any:
    """Fetch ``a.b.c`` from nested dicts, returning a sentinel when absent."""
    node = spec
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def rule_spec_hash(spec: dict[str, Any]) -> str:
    """Canonical hash of the specification EXCLUDING the approval record.

    This is the value that must be copied into approval_record.approved_spec_hash.
    """
    return hash_object({k: v for k, v in spec.items() if k != APPROVAL_SECTION})


def _iter_leaves(node: Any, prefix: str):
    if isinstance(node, dict):
        if not node and prefix:
            yield prefix, node
        for key, value in node.items():
            yield from _iter_leaves(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        if not node:
            yield prefix, node
        for index, value in enumerate(node):
            yield from _iter_leaves(value, f"{prefix}[{index}]")
    else:
        yield prefix, node


def _disabled_sections(spec: dict[str, Any]) -> set[str]:
    return {section for section, switch in CONDITIONAL_SECTIONS.items() if get_path(spec, switch) is False}


def check_rule_freeze(spec: Any, spec_path: str | Path | None = None) -> RuleFreezeReport:
    """Check a parsed specification and return a report listing EVERY problem."""
    if not isinstance(spec, dict):
        report = RuleFreezeReport(str(spec_path) if spec_path else None, None, "n/a")
        report.problems.append(RuleFreezeProblem("<file>", "INVALID", "top level must be key/value pairs"))
        return report

    spec_hash = rule_spec_hash(spec)
    status = get_path(spec, "specification.status")
    report = RuleFreezeReport(str(spec_path) if spec_path else None, None if status is _MISSING else status, spec_hash)
    add = report.problems.append

    # 1. Status
    if status is _MISSING:
        add(RuleFreezeProblem("specification.status", "MISSING", "field is missing"))
    elif status == STATUS_DRAFT:
        add(RuleFreezeProblem("specification.status", "STATUS", f"is {STATUS_DRAFT}; drafts can never execute"))
    elif status != STATUS_FROZEN:
        add(RuleFreezeProblem("specification.status", "INVALID", f"{status!r} is not one of {ALLOWED_STATUSES}"))

    # 2. Required fields
    disabled = _disabled_sections(spec)
    reported: set[str] = set()
    for path in REQUIRED_FIELDS:
        section = path.split(".", 1)[0]
        if section in disabled and path != CONDITIONAL_SECTIONS[section]:
            continue
        value = get_path(spec, path)
        reported.add(path)
        if value is _MISSING:
            add(RuleFreezeProblem(path, "MISSING", "required field is missing from the file"))
        elif is_unresolved(value):
            add(RuleFreezeProblem(path, "UNRESOLVED", f"unanswered (currently {value!r})"))
        elif path in BOOLEAN_FIELDS and not isinstance(value, bool):
            add(RuleFreezeProblem(path, "INVALID", f"must be true or false, got {value!r}"))
        elif path in NON_NEGATIVE_INT_FIELDS and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            add(RuleFreezeProblem(path, "INVALID", f"must be a whole number of minutes >= 0, got {value!r}"))
        elif path in FIXED_VALUES and value != FIXED_VALUES[path]:
            add(
                RuleFreezeProblem(
                    path, "INVALID", f"must equal {FIXED_VALUES[path]!r} (fixed by the data contract), got {value!r}"
                )
            )

    # 2b. Instrument arithmetic must be internally consistent
    tick, tick_value, point_value = (
        get_path(spec, f"instrument.{k}") for k in ("tick_size_points", "tick_value_usd", "point_value_usd")
    )
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (tick, tick_value, point_value)):
        if abs(tick * point_value - tick_value) > 1e-9:
            add(
                RuleFreezeProblem(
                    "instrument.tick_value_usd",
                    "INVALID",
                    f"{tick_value} != tick_size_points x point_value_usd = {tick * point_value}",
                )
            )

    # 2c. Structural levels: exactly the implemented B0 set, sane proximity parameters
    for problem in _structural_level_problems(spec):
        add(problem)
    for problem in _level_state_problems(spec):
        add(problem)
    for problem in _confirmation_problems(spec):
        add(problem)

    # 3. Any other unanswered value anywhere (e.g. nested TBDs, extra fields)
    for path, value in _iter_leaves(spec, ""):
        top = path.split(".", 1)[0].split("[", 1)[0]
        if top == APPROVAL_SECTION or top in disabled or path in reported:
            continue
        if is_unresolved(value):
            add(RuleFreezeProblem(path, "UNRESOLVED", f"unanswered (currently {value!r})"))

    # 4. Approval record
    approval = spec.get(APPROVAL_SECTION)
    if not isinstance(approval, dict):
        add(RuleFreezeProblem(APPROVAL_SECTION, "MISSING", "approval_record section is missing"))
    else:
        if approval.get("approved") is not True:
            add(RuleFreezeProblem("approval_record.approved", "APPROVAL", "specification has not been approved"))
        if is_unresolved(approval.get("approved_by")):
            add(RuleFreezeProblem("approval_record.approved_by", "APPROVAL", "approver name not recorded"))
        approved_at = approval.get("approved_at_utc")
        if is_unresolved(approved_at):
            add(RuleFreezeProblem("approval_record.approved_at_utc", "APPROVAL", "approval time not recorded"))
        elif not _is_tz_aware_timestamp(approved_at):
            add(
                RuleFreezeProblem(
                    "approval_record.approved_at_utc",
                    "APPROVAL",
                    "must be an ISO timestamp with a time zone, e.g. \"2026-01-31T21:00:00Z\"",
                )
            )
        recorded_hash = approval.get("approved_spec_hash")
        if is_unresolved(recorded_hash):
            add(RuleFreezeProblem("approval_record.approved_spec_hash", "APPROVAL", "approved hash not recorded"))
        elif recorded_hash != spec_hash:
            add(
                RuleFreezeProblem(
                    "approval_record.approved_spec_hash",
                    "APPROVAL",
                    "does not match the current specification: the rules changed after approval "
                    f"(approved {str(recorded_hash)[:12]}..., current {spec_hash[:12]}...)",
                )
            )
    return report


def _structural_level_problems(spec: dict[str, Any]) -> list[RuleFreezeProblem]:
    problems: list[RuleFreezeProblem] = []
    level_types = get_path(spec, "structural_levels.level_types")
    if isinstance(level_types, list) and level_types and not any(is_unresolved(v) for v in level_types):
        allowed = {t.value for t in B0_LEVEL_TYPES}
        unknown = [v for v in level_types if v not in allowed]
        if unknown:
            problems.append(
                RuleFreezeProblem(
                    "structural_levels.level_types",
                    "INVALID",
                    f"not implemented B0 level types: {unknown} (adding one needs a registered rule change)",
                )
            )
        if len(set(level_types)) != len(level_types):
            problems.append(RuleFreezeProblem("structural_levels.level_types", "INVALID", "duplicate level types"))
        missing = sorted(allowed - set(level_types))
        if missing:
            problems.append(
                RuleFreezeProblem("structural_levels.level_types", "INVALID", f"B0 requires all seven types; missing {missing}")
            )
    params = get_path(spec, "structural_levels.level_proximity_tolerance.parameters")
    if isinstance(params, dict):
        numbers = {k: params.get(k) for k in ("fraction_of_prior_rth_range", "minimum_points", "maximum_points")}
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in numbers.values()):
            if not 0 < numbers["fraction_of_prior_rth_range"] < 1:
                problems.append(
                    RuleFreezeProblem(
                        "structural_levels.level_proximity_tolerance.parameters.fraction_of_prior_rth_range",
                        "INVALID",
                        "must be between 0 and 1",
                    )
                )
            if not 0 < numbers["minimum_points"] <= numbers["maximum_points"]:
                problems.append(
                    RuleFreezeProblem(
                        "structural_levels.level_proximity_tolerance.parameters",
                        "INVALID",
                        "require 0 < minimum_points <= maximum_points",
                    )
                )
        elif not any(is_unresolved(v) for v in numbers.values()):
            problems.append(
                RuleFreezeProblem(
                    "structural_levels.level_proximity_tolerance.parameters", "INVALID", "parameters must be numbers"
                )
            )
    return problems


LEVEL_STATE_INT_PARAMETERS = (
    "breach_distance_ticks",
    "acceptance_distance_ticks",
    "acceptance_consecutive_closes",
    "rejection_close_distance_ticks",
    "rejection_window_complete_bars",
)


def _level_state_problems(spec: dict[str, Any]) -> list[RuleFreezeProblem]:
    params = get_path(spec, "level_states.parameters")
    if not isinstance(params, dict):
        return []
    base = "level_states.parameters"
    problems = []
    method = params.get("approach_distance_method")
    if not is_unresolved(method) and method != APPROACH_METHOD:
        problems.append(RuleFreezeProblem(f"{base}.approach_distance_method", "INVALID", f"must be {APPROACH_METHOD!r}"))
    for name in LEVEL_STATE_INT_PARAMETERS:
        value = params.get(name, _MISSING)
        if value is _MISSING:
            problems.append(RuleFreezeProblem(f"{base}.{name}", "MISSING", "required parameter is missing"))
        elif not is_unresolved(value) and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            problems.append(RuleFreezeProblem(f"{base}.{name}", "INVALID", f"must be a whole number >= 1, got {value!r}"))
    return problems


def _confirmation_problems(spec: dict[str, Any]) -> list[RuleFreezeProblem]:
    problems: list[RuleFreezeProblem] = []
    max_bars = get_path(spec, "confirmation.max_bars_after_acceptance")
    if max_bars is not _MISSING and not is_unresolved(max_bars):
        # A retest-hold bar plus its continuation bar need at least two slots.
        if isinstance(max_bars, bool) or not isinstance(max_bars, int) or max_bars < 2:
            problems.append(
                RuleFreezeProblem("confirmation.max_bars_after_acceptance", "INVALID", f"must be a whole number >= 2, got {max_bars!r}")
            )
    params = get_path(spec, "confirmation.parameters")
    if not isinstance(params, dict):
        return problems
    base = "confirmation.parameters"
    for name, expected in (
        ("confirmation_type", CONFIRMATION_TYPE),
        ("retest_distance_method", RETEST_DISTANCE_METHOD),
        ("continuation_bar_requirement", CONTINUATION_REQUIREMENT),
    ):
        value = params.get(name, _MISSING)
        if value is _MISSING:
            problems.append(RuleFreezeProblem(f"{base}.{name}", "MISSING", "required parameter is missing"))
        elif not is_unresolved(value) and value != expected:
            problems.append(RuleFreezeProblem(f"{base}.{name}", "INVALID", f"B0 implements only {expected!r}"))
    for name in ("opposite_boundary_failure_distance_ticks", "retest_hold_close_distance_ticks", "continuation_break_distance_ticks"):
        value = params.get(name, _MISSING)
        if value is _MISSING:
            problems.append(RuleFreezeProblem(f"{base}.{name}", "MISSING", "required parameter is missing"))
        elif not is_unresolved(value) and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            problems.append(RuleFreezeProblem(f"{base}.{name}", "INVALID", f"must be a whole number >= 1, got {value!r}"))
    return problems


def _is_tz_aware_timestamp(value: Any) -> bool:
    if isinstance(value, dt.datetime):
        return value.tzinfo is not None
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None
    return False


def check_rule_freeze_file(path: str | Path) -> RuleFreezeReport:
    return check_rule_freeze(load_mapping(path), spec_path=path)


def require_executable(spec: dict[str, Any], spec_path: str | Path | None = None) -> RuleFreezeReport:
    """Gatekeeper for any future backtest/simulation code.

    Every component that would *use* trading rules must call this first. It
    raises RuleFreezeNotExecutableError (carrying the full report) unless the
    specification is frozen, complete and approved.
    """
    report = check_rule_freeze(spec, spec_path)
    if not report.is_executable:
        raise RuleFreezeNotExecutableError(report)
    return report
