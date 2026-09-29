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
STAGE_APPROVAL_SECTION = "stage_approvals"  # Round 15: staged readiness approvals (readiness.py)
APPROVAL_SECTIONS = frozenset({APPROVAL_SECTION, STAGE_APPROVAL_SECTION})

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
    "setup.name",
    "setup.long_definition",
    "setup.short_definition",
    "setup.authoritative_sections",
    "direction.long_conditions",
    "direction.short_conditions",
    "direction.conflict_resolution",
    "direction.event_derived",
    "direction.same_direction_candidates",
    "direction.directional_state_reset",
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
    "market_structure.definition",
    "market_structure.additional_swing_structure_filter",
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
    # Entry order lifecycle (Round 13)
    "entry_order_lifecycle.parameters",
    "entry_order_lifecycle.research_quantity_labels",
    "entry_order_lifecycle.creation",
    "entry_order_lifecycle.submission_recheck",
    "entry_order_lifecycle.tracked_timestamps",
    "entry_order_lifecycle.outcomes",
    "entry_order_lifecycle.cancellation_race",
    "entry_order_lifecycle.protective_order_dependency",
    "entry_order_lifecycle.acknowledgement_rules",
    "entry_order_lifecycle.lost_reliable_state",
    "entry_order_lifecycle.contradictory_reports",
    "entry_order_lifecycle.reconciliation",
    "entry_order_lifecycle.fill_at_or_beyond_stop",
    "entry_order_lifecycle.not_submitted_effects",
    "entry_trigger.not_a_trigger",
    "entry_trigger.terminality",
    # Protective stop/target orders (Round 14)
    "protective_orders.parameters",
    "protective_orders.deployment",
    "protective_orders.prices",
    "protective_orders.protection_task_fields",
    "protective_orders.dispatch",
    "protective_orders.bracket_workflow",
    "protective_orders.stop_first_rule",
    "protective_orders.protection_active_definition",
    "protective_orders.partial_entry_fills",
    "protective_orders.oco_behavior",
    "protective_orders.stop_activation",
    "protective_orders.target_activation",
    "protective_orders.same_bar_ambiguity",
    "protective_orders.stop_failure",
    "protective_orders.target_failure",
    "protective_orders.oco_link_failure",
    "protective_orders.fill_at_or_beyond_stop",
    "protective_orders.flat_confirmation",
    "protective_orders.mandatory_session_flatten",
    "protective_orders.exit_outcomes",
    "protective_orders.exit_record",
    "protective_orders.mixed_exits",
    "protective_orders.late_fill_after_close",
    "protective_orders.oco_link_clock",
    "protective_orders.cancellation_unknown",
    "protective_orders.position_mismatch",
    # Execution eligibility integration (D-028)
    "execution_eligibility_integration.historical_signal_producers_status",
    "execution_eligibility_integration.simulated_execution_producers_status",
    "execution_eligibility_integration.live_producers_status",
    "execution_eligibility_integration.simulated_producers_required",
    "execution_eligibility_integration.requirement",
    "execution_eligibility_integration.controls",
    # Invalidation, stop, target
    "structural_invalidation.definition",
    "structural_invalidation.action",
    "structural_invalidation.action_rules",
    "stop_placement.method",
    "stop_placement.buffer_ticks",
    "stop_placement.minimum_stop_points",
    "stop_placement.maximum_stop_points",
    "stop_placement.trade_skipped_if_outside_stop_limits",
    "stop_placement.stop_validity_rules",
    "target_placement.method",
    "target_placement.parameters",
    # Position management
    # Trade geometry (Round 12)
    "trade_geometry.parameters",
    "trade_geometry.planned_entry_reference",
    "trade_geometry.geometry",
    "trade_geometry.target_zone_selection",
    "trade_geometry.cost_treatment",
    "trade_geometry.candidate_eligibility",
    "trade_geometry.candidate_selection",
    "trade_geometry.selected_candidate_record",
    "position_management.contracts_per_trade",
    "position_management.sizing_method",
    "position_management.scaling_in_out",
    "position_management.breakeven_rule",
    "position_management.trailing_stop_rule",
    "position_management.time_based_exit",
    "position_management.normal_flatten_time",
    "position_management.normal_flatten_timezone",
    "position_management.normal_time_exit_order_type",
    "position_management.normal_time_exit_procedure",
    "position_management.risk_per_trade",
    "position_management.position_sizing_balance_basis",
    "position_management.contract_rounding",
    "position_management.below_one_contract_action",
    "position_management.max_contracts_per_trade",
    # Daily limits and re-entry
    "daily_limits.max_filled_entries_per_trading_date",
    "daily_limits.filled_entry_allowance_rule",
    "daily_limits.max_trades_per_day",
    "daily_limits.trade_definition",
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
    "commissions.per_side_per_contract_usd",
    "commissions.source_archive_status",
    "commissions.stress_multipliers",
    "slippage.baseline_ticks",
    "slippage.adverse_direction_rule",
    "slippage.stress_multipliers",
    "slippage.actual_fill_rule",
    # Trade accounting and research pipeline (Round 15)
    "trade_accounting.gross_pnl",
    "trade_accounting.net_pnl",
    "trade_accounting.commission_accounting",
    "trade_accounting.planned_risk",
    "trade_accounting.actual_initial_risk",
    "trade_accounting.result_r",
    "trade_accounting.excursions",
    "trade_accounting.record_fields",
    "trade_accounting.reproducibility_hashes",
    "research_pipeline.historical_data_ingestion_status",
    "research_pipeline.historical_calendar_producers_status",
    "research_pipeline.deterministic_replay_wiring_status",
    "research_pipeline.execution_simulation_wiring_status",
    "protective_orders.final_flattening_legs",
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


# Explicit "not yet" markers the rule owner may record instead of TBD. They are
# answers about status, not rules, so they block execution exactly like TBD.
UNRESOLVED_MARKERS: frozenset[str] = frozenset({"TBD", "REQUIRED_BEFORE_EXECUTABLE"})
_UNRESOLVED_PREFIX = "UNRESOLVED_"


def is_unresolved(value: Any) -> bool:
    """True if a value is an unanswered placeholder (None, TBD, empty, or an explicit unresolved marker)."""
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        if text.upper() in UNRESOLVED_MARKERS or text == "":
            return True
        return text.startswith(_UNRESOLVED_PREFIX) and text.replace("_", "").isalnum() and text.isupper()
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
    return hash_object({k: v for k, v in spec.items() if k not in APPROVAL_SECTIONS})


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
    for problem in _direction_problems(spec):
        add(problem)
    for problem in _trade_geometry_problems(spec):
        add(problem)
    for problem in _entry_order_problems(spec):
        add(problem)
    for problem in _protective_order_problems(spec):
        add(problem)
    for problem in _cost_problems(spec):
        add(problem)

    # 3. Any other unanswered value anywhere (e.g. nested TBDs, extra fields)
    for path, value in _iter_leaves(spec, ""):
        top = path.split(".", 1)[0].split("[", 1)[0]
        if top in APPROVAL_SECTIONS or top in disabled or path in reported:
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
    lookback = params.get("pre_open_arming_lookback_bars", _MISSING)
    if lookback is _MISSING:
        problems.append(RuleFreezeProblem(f"{base}.pre_open_arming_lookback_bars", "MISSING", "required parameter is missing"))
    elif not is_unresolved(lookback) and (isinstance(lookback, bool) or lookback not in (0, 1)):
        problems.append(RuleFreezeProblem(f"{base}.pre_open_arming_lookback_bars", "INVALID", "must be 0 or 1 (only these are implemented)"))
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


B0_MARKET_STRUCTURE = "ACCEPTANCE_PULLBACK_HOLD_CONTINUATION"
B0_CONFLICT_RESULT = "NO_TRADE_DIRECTIONAL_CONFLICT"


def _direction_problems(spec: dict[str, Any]) -> list[RuleFreezeProblem]:
    problems: list[RuleFreezeProblem] = []
    expectations = (
        ("market_structure.definition", B0_MARKET_STRUCTURE),
        ("market_structure.additional_swing_structure_filter", "NOT_APPLICABLE"),
        ("direction.conflict_resolution.result", B0_CONFLICT_RESULT),
        ("direction.conflict_resolution.halt_new_entries_for_remainder_of_trading_date", True),
    )
    for path, expected in expectations:
        value = get_path(spec, path)
        if value is _MISSING or is_unresolved(value):
            continue  # reported as missing/unresolved elsewhere
        if value != expected:
            problems.append(RuleFreezeProblem(path, "INVALID", f"B0 implements only {expected!r}, got {value!r}"))
    return problems


def _trade_geometry_problems(spec: dict[str, Any]) -> list[RuleFreezeProblem]:
    from decimal import Decimal, InvalidOperation

    from mnq_research.trade_geometry import EXACT_TIE_ACTION, RANKING_CRITERIA

    params = get_path(spec, "trade_geometry.parameters")
    if not isinstance(params, dict):
        return []
    base = "trade_geometry.parameters"
    problems = []
    for name in ("planned_entry_adverse_buffer_ticks", "structural_invalidation_buffer_ticks", "target_buffer_ticks"):
        value = params.get(name, _MISSING)
        if value is _MISSING:
            problems.append(RuleFreezeProblem(f"{base}.{name}", "MISSING", "required parameter is missing"))
        elif not is_unresolved(value) and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            problems.append(RuleFreezeProblem(f"{base}.{name}", "INVALID", f"must be a whole number >= 0, got {value!r}"))
    rr = params.get("minimum_planned_gross_rr", _MISSING)
    if rr is _MISSING:
        problems.append(RuleFreezeProblem(f"{base}.minimum_planned_gross_rr", "MISSING", "required parameter is missing"))
    elif not is_unresolved(rr):
        try:
            ok = not isinstance(rr, bool) and Decimal(str(rr)) > 0
        except InvalidOperation:
            ok = False
        if not ok:
            problems.append(RuleFreezeProblem(f"{base}.minimum_planned_gross_rr", "INVALID", f"must be a positive number, got {rr!r}"))
    ranking = params.get("candidate_ranking")
    if not is_unresolved(ranking) and tuple(ranking or ()) != RANKING_CRITERIA:
        problems.append(RuleFreezeProblem(f"{base}.candidate_ranking", "INVALID", f"B0 implements only {list(RANKING_CRITERIA)}"))
    tie = params.get("exact_tie_action")
    if not is_unresolved(tie) and tie != EXACT_TIE_ACTION:
        problems.append(RuleFreezeProblem(f"{base}.exact_tie_action", "INVALID", f"B0 implements only {EXACT_TIE_ACTION!r}"))
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


def _entry_order_problems(spec: dict[str, Any]) -> list[RuleFreezeProblem]:
    """Round 13 / D-028 values that B0 code implements exactly; anything else is INVALID."""
    from decimal import Decimal, InvalidOperation

    from mnq_research.eligibility import ExecutionEligibility
    from mnq_research.entry_order import (
        ENTRY_ORDER_TYPE,
        LIVE_OR_PAPER_SUBMISSION_STATUS,
        PROHIBITED_ENTRY_ORDER_TYPES,
        RESEARCH_QUANTITY_LABELS,
    )
    from mnq_research.sizing import BELOW_ONE_CONTRACT_ACTION, CONTRACT_ROUNDING

    problems: list[RuleFreezeProblem] = []
    expectations = (
        ("order_type.entry_order_type", ENTRY_ORDER_TYPE),
        ("order_type.prohibited_entry_order_types", list(PROHIBITED_ENTRY_ORDER_TYPES)),
        ("entry_order_lifecycle.parameters.entry_order_type", ENTRY_ORDER_TYPE),
        ("entry_order_lifecycle.parameters.research_quantity_contracts", 1),
        ("entry_order_lifecycle.research_quantity_labels", list(RESEARCH_QUANTITY_LABELS)),
        ("entry_order_lifecycle.protective_order_dependency.live_or_paper_order_submission", LIVE_OR_PAPER_SUBMISSION_STATUS),
        ("execution_eligibility_integration.controls", list(ExecutionEligibility.control_names())),
        ("stop_placement.minimum_stop_points", "NOT_APPLICABLE"),
        ("stop_placement.maximum_stop_points", "NOT_APPLICABLE"),
        ("stop_placement.trade_skipped_if_outside_stop_limits", False),
        ("daily_limits.max_filled_entries_per_trading_date", 1),
        ("daily_limits.max_trades_per_day", 1),
        ("reentry.allowed", False),
        ("position_management.contract_rounding", CONTRACT_ROUNDING),
        ("position_management.below_one_contract_action", BELOW_ONE_CONTRACT_ACTION),
    )
    for path, expected in expectations:
        value = get_path(spec, path)
        if value is _MISSING or is_unresolved(value):
            continue  # reported as missing/unresolved elsewhere
        if value != expected or type(value) is not type(expected):
            problems.append(RuleFreezeProblem(path, "INVALID", f"B0 implements only {expected!r}, got {value!r}"))
    params = get_path(spec, "entry_order_lifecycle.parameters")
    if isinstance(params, dict):
        for name in ("signal_to_order_delay_seconds", "entry_order_max_working_seconds", "order_acknowledgement_timeout_seconds"):
            path = f"entry_order_lifecycle.parameters.{name}"
            value = params.get(name, _MISSING)
            if value is _MISSING:
                problems.append(RuleFreezeProblem(path, "MISSING", "required parameter is missing"))
                continue
            if is_unresolved(value):
                continue
            try:
                ok = isinstance(value, str) and Decimal(value).is_finite() and Decimal(value) > 0
            except InvalidOperation:
                ok = False
            if not ok:
                problems.append(RuleFreezeProblem(path, "INVALID", f"must be a quoted positive number of seconds, got {value!r}"))
    return problems


def _protective_order_problems(spec: dict[str, Any]) -> list[RuleFreezeProblem]:
    """Round 14 values that the B0 protection code implements exactly; anything else is INVALID."""
    from decimal import Decimal, InvalidOperation

    from mnq_research import protection as pr

    base = "protective_orders.parameters"
    expectations = (
        (f"{base}.protective_stop_order_type", pr.STOP_ORDER_TYPE),
        (f"{base}.profit_target_order_type", pr.TARGET_ORDER_TYPE),
        (f"{base}.protective_time_in_force", pr.PROTECTIVE_TIME_IN_FORCE),
        (f"{base}.preferred_bracket_mode", pr.PREFERRED_BRACKET_MODE.value),
        (f"{base}.same_bar_stop_target_policy", pr.SAME_BAR_POLICY),
        ("protective_orders.deployment.server_side_protective_orders_required", True),
        ("protective_orders.deployment.server_side_oco_required", True),
        ("protective_orders.exit_outcomes", [o.value for o in pr.ExitOutcome]),
        ("position_management.breakeven_rule", "NONE"),
        ("position_management.trailing_stop_rule", "NONE"),
        ("position_management.scaling_in_out", "NONE"),
        ("protective_orders.final_flattening_legs", [leg.value for leg in pr.FlatteningLeg]),
        ("structural_invalidation.action", "EXIT_VIA_PROTECTIVE_STOP_MARKET"),
        ("position_management.normal_time_exit_order_type", "MARKET"),
        ("position_management.normal_flatten_timezone", "America/New_York"),
        ("session_flattening.flatten_order_type", "MARKET"),
        ("daily_limits.max_losing_trades_per_day", "NOT_APPLICABLE_BECAUSE_MAX_FILLED_ENTRIES_IS_ONE"),
        ("ema.included", False),
        ("vwap.included", False),
    )
    problems: list[RuleFreezeProblem] = []
    for path, expected in expectations:
        value = get_path(spec, path)
        if value is _MISSING or is_unresolved(value):
            continue
        if value != expected or type(value) is not type(expected):
            problems.append(RuleFreezeProblem(path, "INVALID", f"B0 implements only {expected!r}, got {value!r}"))
    for name, minimum in (("protection_dispatch_deadline_milliseconds", 1), ("target_fill_trade_through_ticks", 0)):
        value = get_path(spec, f"{base}.{name}")
        if value is _MISSING or is_unresolved(value):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            problems.append(RuleFreezeProblem(f"{base}.{name}", "INVALID", f"must be a whole number >= {minimum}, got {value!r}"))
    value = get_path(spec, f"{base}.protective_order_acknowledgement_timeout_seconds")
    if value is not _MISSING and not is_unresolved(value):
        try:
            ok = isinstance(value, str) and Decimal(value).is_finite() and Decimal(value) > 0
        except InvalidOperation:
            ok = False
        if not ok:
            problems.append(
                RuleFreezeProblem(f"{base}.protective_order_acknowledgement_timeout_seconds", "INVALID", f"must be a quoted positive number of seconds, got {value!r}")
            )
    return problems


def _cost_problems(spec: dict[str, Any]) -> list[RuleFreezeProblem]:
    """Round 15: the cost model must be internally consistent and complete (costs.CostModel enforces the same)."""
    from mnq_research.costs import CostModel

    problems: list[RuleFreezeProblem] = []
    for section in ("commissions", "slippage"):
        body = spec.get(section)
        if not isinstance(body, dict) or any(is_unresolved(v) for k, v in body.items() if k != "source_archive_status"):
            return problems  # reported as missing/unresolved elsewhere
    try:
        CostModel.from_spec(spec)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        problems.append(RuleFreezeProblem("commissions/slippage", "INVALID", str(exc)))
    return problems
