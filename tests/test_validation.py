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
    disabled = {section for section in ("ema", "vwap") if draft_spec[section]["included"] is False}
    expected_unanswered = {
        path for path in REQUIRED_FIELDS
        if get_path(draft_spec, path) in (None, "TBD") and path.split(".")[0] not in disabled
    }
    # Every unanswered required field is reported - not just the first one.
    assert expected_unanswered <= unresolved
    kinds = {p.kind for p in report.problems}
    assert {"STATUS", "UNRESOLVED", "APPROVAL"} <= kinds


def test_every_required_field_is_reported_when_all_are_blank(draft_spec):
    # Independent of how far the draft has progressed: blank out every
    # required field and demand that every single one is reported at once.
    for path in REQUIRED_FIELDS:
        *parents, leaf = path.split(".")
        node = draft_spec
        for part in parents:
            node = node[part]
        node[leaf] = "TBD"
    unresolved = set(check_rule_freeze(draft_spec).unresolved_paths())
    # ema/vwap dependents are only required once included; with included=TBD they are reported too.
    assert set(REQUIRED_FIELDS) <= unresolved
    assert len(REQUIRED_FIELDS) > 100


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


def test_news_minute_fields_must_be_non_negative_whole_numbers(completed_spec):
    completed_spec["news_events"]["blackout_minutes_before"] = -5
    completed_spec["news_events"]["entry_protection_buffer_before_blackout_minutes"] = "fifteen"
    invalid = {p.path for p in check_rule_freeze(completed_spec).problems if p.kind == "INVALID"}
    assert {
        "news_events.blackout_minutes_before",
        "news_events.entry_protection_buffer_before_blackout_minutes",
    } <= invalid


def test_recorded_news_examples_agree_with_recorded_minutes(draft_spec):
    """Guards against examples drifting from the numbers (the Round 7 10:30 error)."""
    import datetime as dt

    news = draft_spec["news_events"]
    before = dt.timedelta(minutes=news["blackout_minutes_before"])
    after = dt.timedelta(minutes=news["blackout_minutes_after"])
    buffer = dt.timedelta(minutes=news["entry_protection_buffer_before_blackout_minutes"])
    hhmm = lambda t: t.strftime("%H:%M")  # noqa: E731

    event = dt.datetime(2026, 1, 1, 11, 0)
    example = news["entry_protection_buffer"]["example"]
    assert f"new-entry block from {hhmm(event - before - buffer)}" in example
    assert f"formal blackout from {hhmm(event - before)}" in example
    assert f"news-flatten begins {hhmm(event - before - dt.timedelta(minutes=1))}" in example

    event = dt.datetime(2026, 1, 1, 10, 0)
    blackout_end = event + after
    # First five-minute bar lying entirely at or after the blackout end closes 5 minutes after
    # the first five-minute boundary at or after that end.
    first_bar_start = blackout_end + dt.timedelta(minutes=(-blackout_end.minute) % 5)
    earliest_decision = first_bar_start + dt.timedelta(minutes=5)
    example = " ".join(news["blackout_interval"]["example"].split())
    assert f"earliest possible fresh decision {hhmm(earliest_decision)}" in example
    assert hhmm(earliest_decision) == "10:35"
