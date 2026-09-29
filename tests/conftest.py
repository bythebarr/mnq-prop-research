"""Shared test fixtures.

`completed_spec` fills every unanswered field with the literal string
"TEST_FIXTURE_NOT_A_RULE". It exists only to prove the validator CAN pass
when everything is answered (so a validator that always fails would be
caught). It is not, and must never be mistaken for, a trading rule.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from mnq_research.config import load_mapping
from mnq_research.synthetic_data import SyntheticSpec, generate_synthetic_bars
from mnq_research.validation import STATUS_FROZEN, is_unresolved, rule_spec_hash

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULE_FREEZE_PATH = PROJECT_ROOT / "configs" / "rule_freeze_v1.yaml"
FIXTURE_VALUE = "TEST_FIXTURE_NOT_A_RULE"


@pytest.fixture
def draft_spec() -> dict:
    return load_mapping(RULE_FREEZE_PATH)


def _fill(node):
    if isinstance(node, dict):
        return {k: _fill(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill(v) for v in node] if node else [FIXTURE_VALUE]
    return FIXTURE_VALUE if is_unresolved(node) else node


def complete_and_approve(spec: dict) -> dict:
    spec = copy.deepcopy(spec)
    spec = {k: (_fill(v) if k != "approval_record" else v) for k, v in spec.items()}
    spec["specification"]["status"] = STATUS_FROZEN
    spec["approval_record"] = {
        "approved": True,
        "approved_by": "pytest",
        "approved_at_utc": "2026-01-01T00:00:00Z",
        "approved_spec_hash": rule_spec_hash(spec),
        "approval_notes": FIXTURE_VALUE,
    }
    return spec


@pytest.fixture
def completed_spec(draft_spec) -> dict:
    return complete_and_approve(draft_spec)


@pytest.fixture
def write_yaml(tmp_path):
    def _write(name: str, data) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write


@pytest.fixture(scope="session")
def clean_bars_session():
    return generate_synthetic_bars(SyntheticSpec(n_trading_days=2))


@pytest.fixture
def clean_bars(clean_bars_session):
    return clean_bars_session.copy()
