"""Rule-freeze protections: drafts and incomplete specs can never execute."""

from __future__ import annotations

import re

import pytest

from conftest import PROJECT_ROOT
from mnq_research.validation import (
    REQUIRED_FIELDS,
    RuleFreezeNotExecutableError,
    check_rule_freeze,
    get_path,
    require_executable,
)


def test_draft_rule_freeze_cannot_execute(draft_spec):
    assert draft_spec["specification"]["status"] == "DRAFT_NON_EXECUTABLE"
    with pytest.raises(RuleFreezeNotExecutableError) as excinfo:
        require_executable(draft_spec)
    assert not excinfo.value.report.is_executable


def test_all_unresolved_fields_are_reported_together(draft_spec):
    report = check_rule_freeze(draft_spec)
    unresolved = set(report.unresolved_paths())
    expected_unanswered = {
        path for path in REQUIRED_FIELDS if get_path(draft_spec, path) in (None, "TBD")
    }
    # Every unanswered required field is reported - not just the first one.
    assert expected_unanswered <= unresolved
    assert len(expected_unanswered) > 100
    kinds = {p.kind for p in report.problems}
    assert {"STATUS", "UNRESOLVED", "APPROVAL"} <= kinds


def test_draft_template_contains_every_required_field(draft_spec):
    missing = [p for p in REQUIRED_FIELDS if get_path(draft_spec, p) is get_path({}, "x")]
    assert missing == []


def test_deleting_a_field_does_not_make_it_pass(draft_spec):
    del draft_spec["stop_placement"]["method"]
    report = check_rule_freeze(draft_spec)
    assert ("stop_placement.method", "MISSING") in {(p.path, p.kind) for p in report.problems}


def test_completed_and_approved_spec_is_executable(completed_spec):
    # Proves the validator is not simply "always fail".
    report = require_executable(completed_spec)
    assert report.is_executable


def test_single_remaining_tbd_blocks_execution(completed_spec):
    completed_spec["slippage"]["entry_ticks"] = "TBD"
    report = check_rule_freeze(completed_spec)
    assert not report.is_executable
    assert "slippage.entry_ticks" in report.unresolved_paths()


def test_edit_after_approval_blocks_execution(completed_spec):
    completed_spec["position_management"]["contracts_per_trade"] = "SOMETHING_ELSE"
    report = check_rule_freeze(completed_spec)
    assert [p.path for p in report.problems] == ["approval_record.approved_spec_hash"]


def test_disabled_optional_indicator_needs_no_details(completed_spec):
    completed_spec["ema"] = {"included": False, "period": None}
    completed_spec["approval_record"]["approved_spec_hash"] = check_rule_freeze(completed_spec).spec_hash
    assert check_rule_freeze(completed_spec).is_executable


def test_fixed_data_contract_values_cannot_drift(completed_spec):
    completed_spec["timezone_policy"]["storage_timezone"] = "America/New_York"
    report = check_rule_freeze(completed_spec)
    assert ("timezone_policy.storage_timezone", "INVALID") in {(p.path, p.kind) for p in report.problems}


def test_guide_explains_every_required_field():
    guide = (PROJECT_ROOT / "docs" / "RULE_FREEZE_GUIDE.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`([a-z_]+\.[a-z_]+)`", guide))
    undocumented = [p for p in REQUIRED_FIELDS if p not in documented]
    assert undocumented == []


def test_inconsistent_tick_arithmetic_is_rejected(completed_spec):
    completed_spec["instrument"].update(tick_size_points=0.25, point_value_usd=2.0, tick_value_usd=5.0)
    report = check_rule_freeze(completed_spec)
    assert ("instrument.tick_value_usd", "INVALID") in {(p.path, p.kind) for p in report.problems}


def test_recorded_instrument_facts_are_consistent(draft_spec):
    report = check_rule_freeze(draft_spec)
    assert not [p for p in report.problems if p.path.startswith("instrument.")]
