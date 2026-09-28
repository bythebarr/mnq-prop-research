"""Strict YAML loading protects rule files from silent misreads."""

from __future__ import annotations

import pytest

from mnq_research.config import ConfigError, parse_yaml
from mnq_research.validation import get_path


def test_duplicate_keys_are_rejected():
    with pytest.raises(ConfigError, match="Duplicate key 'stop_ticks'"):
        parse_yaml("stop_ticks: 8\nother: 1\nstop_ticks: 12\n")


def test_clock_times_are_not_converted_to_numbers():
    # Standard YAML 1.1 would turn 17:00 into the integer 1020.
    data = parse_yaml("flatten_at: 17:00\nstart: 9:30\n")
    assert data == {"flatten_at": "17:00", "start": "9:30"}


def test_yes_no_on_off_are_text_not_booleans():
    assert parse_yaml("a: yes\nb: off\nc: true\n") == {"a": "yes", "b": "off", "c": True}


def test_draft_rule_freeze_loads_as_draft(draft_spec):
    assert get_path(draft_spec, "specification.status") == "DRAFT_NON_EXECUTABLE"
    assert get_path(draft_spec, "approval_record.approved") is False
