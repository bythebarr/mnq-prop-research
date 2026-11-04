"""Experiment registry: the freeze gate blocks registration until the rules
are executable; a plan also cannot register while its own fields are
undecided; immutable once registered."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import PROJECT_ROOT
from mnq_research.config import load_mapping
from mnq_research.experiment_registry import (
    ExperimentBlockedError,
    ExperimentRecord,
    RegistryConflictError,
    assess_experiment,
    load_experiment,
    read_registry,
    register_experiment,
)

EXPERIMENT_PATH = PROJECT_ROOT / "configs" / "experiment_001.yaml"


def _plan(**overrides) -> dict:
    plan = load_mapping(EXPERIMENT_PATH)
    plan.update(
        rule_config_path="rules.yaml",
        data_identifier="TEST_FIXTURE_DATASET",
        data_hash="0" * 64,
        training_period={"start": "2020-01-01", "end": "2020-12-31"},
        validation_period={"start": "2021-01-01", "end": "2021-06-30"},
        holdout_period={"start": "2021-07-01", "end": "2021-12-31"},
        cost_assumptions={"source": "TEST_FIXTURE"},
    )
    plan.update(overrides)
    return plan


def test_experiment_001_is_blocked_by_its_undecided_plan_not_the_freeze(tmp_path):
    record = load_experiment(EXPERIMENT_PATH)
    # The plan file still carries its pre-freeze status label...
    assert record.status.value == "NOT_RUN_BLOCKED_BY_RULE_FREEZE"
    assessment = assess_experiment(record, PROJECT_ROOT)
    assert not assessment.can_register
    # ...but the freeze itself no longer blocks anything: it was approved on
    # 2026-11-03 and is executable. What still blocks EXP-001 is the plan's
    # own undecided fields (data, splits, costs are null in the plan file).
    assert not any("rule freeze" in b for b in assessment.blockers)
    undecided = {
        "training_period",
        "validation_period",
        "holdout_period",
        "data_identifier",
        "data_hash",
        "cost_assumptions",
    }
    assert {b.split(" ", 1)[0] for b in assessment.blockers} == undecided

    registry = tmp_path / "registry.jsonl"
    with pytest.raises(ExperimentBlockedError):
        register_experiment(EXPERIMENT_PATH, PROJECT_ROOT, registry)
    assert not registry.exists()  # nothing written


def test_complete_plan_is_still_blocked_by_draft_rules(tmp_path, draft_spec, write_yaml):
    write_yaml("rules.yaml", draft_spec)
    plan = write_yaml("exp.yaml", _plan())
    with pytest.raises(ExperimentBlockedError) as excinfo:
        register_experiment(plan, tmp_path, tmp_path / "registry.jsonl")
    assert len(excinfo.value.assessment.blockers) == 1


def test_claiming_a_runnable_status_with_draft_rules_is_flagged(tmp_path, draft_spec, write_yaml):
    write_yaml("rules.yaml", draft_spec)
    record = ExperimentRecord.model_validate(_plan(status="REGISTERED_NOT_RUN"))
    blockers = assess_experiment(record, tmp_path).blockers
    assert any("must be NOT_RUN_BLOCKED_BY_RULE_FREEZE" in b for b in blockers)


def test_registration_is_immutable(tmp_path, completed_spec, write_yaml):
    write_yaml("rules.yaml", completed_spec)
    plan_path = write_yaml("exp.yaml", _plan())
    registry = tmp_path / "registry.jsonl"

    entry, new = register_experiment(plan_path, tmp_path, registry)
    assert new and entry["plan"]["rule_config_hash"]
    _, new_again = register_experiment(plan_path, tmp_path, registry)
    assert not new_again  # identical content: idempotent

    write_yaml("exp.yaml", _plan(holdout_period={"start": "2021-08-01", "end": "2021-12-31"}))
    with pytest.raises(RegistryConflictError):
        register_experiment(plan_path, tmp_path, registry)
    assert len(read_registry(registry)) == 1


def test_overlapping_periods_are_rejected():
    with pytest.raises(ValidationError, match="chronological"):
        ExperimentRecord.model_validate(_plan(holdout_period={"start": "2021-06-01", "end": "2021-12-31"}))
