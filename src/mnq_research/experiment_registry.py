"""Experiment registry: pre-register experiments BEFORE they are run.

Why this exists
---------------
The most common way strategy research fools itself is by quietly adjusting
rules, dates or data after seeing results, then reporting only the version
that looked best. The registry makes that visible:

* An experiment plan (a YAML file such as configs/experiment_001.yaml) states
  the rule file, data, date splits, parameters and costs IN ADVANCE.
* Registering it stamps the canonical hash of the rule file, the Git commit of
  the code and a UTC timestamp, and appends the whole record as one line to an
  append-only log (outputs/experiments/registry.jsonl), which is committed to
  Git.
* An experiment ID can never be re-registered with different content. A
  changed rule file, changed dates or changed data => a NEW experiment ID, and
  the earlier attempt stays on the record (the count of configurations tested
  is part of the evidence).
* Nothing can be registered while the rule freeze is not executable.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mnq_research.config import ConfigError, load_mapping
from mnq_research.hashing import hash_object
from mnq_research.validation import RuleFreezeReport, check_rule_freeze, rule_spec_hash


class ExperimentStatus(str, Enum):
    NOT_RUN_BLOCKED_BY_RULE_FREEZE = "NOT_RUN_BLOCKED_BY_RULE_FREEZE"
    REGISTERED_NOT_RUN = "REGISTERED_NOT_RUN"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class DateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: dt.date
    end: dt.date

    @model_validator(mode="after")
    def _ordered(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError(f"end {self.end} is before start {self.start}")
        return self


class ExperimentRecord(BaseModel):
    """An experiment plan. ``None`` means "not decided yet"."""

    model_config = ConfigDict(extra="forbid")  # typos in field names are errors

    experiment_id: str = Field(pattern=r"^EXP-\d{3,}$")
    title: str
    status: ExperimentStatus
    created_at_utc: dt.datetime | None = None
    rule_config_path: str
    rule_config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_identifier: str | None = None
    data_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    code_version: str | None = None
    training_period: DateRange | None = None
    validation_period: DateRange | None = None
    holdout_period: DateRange | None = None
    parameters_tested: list[dict[str, Any]] = Field(default_factory=list)
    num_configurations_tested: int = Field(default=0, ge=0)
    cost_assumptions: dict[str, Any] | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _chronological(self) -> "ExperimentRecord":
        """Training < validation < untouched holdout, with no overlap."""
        periods = [
            (name, p)
            for name, p in (
                ("training_period", self.training_period),
                ("validation_period", self.validation_period),
                ("holdout_period", self.holdout_period),
            )
            if p is not None
        ]
        for (name_a, a), (name_b, b) in zip(periods, periods[1:]):
            if not a.end < b.start:
                raise ValueError(
                    f"{name_b} must start after {name_a} ends (got {name_a} ending {a.end}, "
                    f"{name_b} starting {b.start}). Periods must be chronological and non-overlapping."
                )
        return self


class ExperimentBlockedError(Exception):
    def __init__(self, assessment: "ExperimentAssessment"):
        self.assessment = assessment
        super().__init__(f"Experiment {assessment.record.experiment_id} is BLOCKED: " + "; ".join(assessment.blockers))


class RegistryConflictError(Exception):
    """An experiment ID is already registered with different content."""


@dataclass
class ExperimentAssessment:
    record: ExperimentRecord
    rule_report: RuleFreezeReport | None
    current_rule_hash: str | None
    blockers: list[str]

    @property
    def can_register(self) -> bool:
        return not self.blockers

    def format(self) -> str:
        r = self.record
        lines = [
            f"EXPERIMENT {r.experiment_id}: {r.title}",
            f"  Status:                 {r.status.value}",
            f"  Rule config:            {r.rule_config_path}",
            f"  Rule config hash (now): {self.current_rule_hash or 'n/a'}",
            f"  Rule freeze executable: {'yes' if self.rule_report and self.rule_report.is_executable else 'NO'}",
            f"  Data identifier:        {r.data_identifier or 'TBD'}",
            f"  Training period:        {_fmt_period(r.training_period)}",
            f"  Validation period:      {_fmt_period(r.validation_period)}",
            f"  Untouched holdout:      {_fmt_period(r.holdout_period)}",
            f"  Configurations tested:  {r.num_configurations_tested}",
        ]
        if self.blockers:
            lines += ["", f"BLOCKED - cannot be registered or run ({len(self.blockers)} reason(s)):"]
            lines += [f"  - {b}" for b in self.blockers]
        else:
            lines += ["", "Ready to register (all pre-registration requirements met)."]
        return "\n".join(lines)


def _fmt_period(period: DateRange | None) -> str:
    return f"{period.start} to {period.end}" if period else "TBD"


def load_experiment(path: str | Path) -> ExperimentRecord:
    data = load_mapping(path)
    try:
        return ExperimentRecord.model_validate(data)
    except ValidationError as exc:
        problems = "\n".join(f"  - {'.'.join(map(str, e['loc'])) or '<file>'}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"{path}: experiment file has invalid fields:\n{problems}") from None


def assess_experiment(record: ExperimentRecord, project_root: str | Path) -> ExperimentAssessment:
    """Collect every reason the experiment cannot be registered/run yet."""
    blockers: list[str] = []
    rule_path = Path(project_root) / record.rule_config_path
    rule_report: RuleFreezeReport | None = None
    current_hash: str | None = None
    try:
        spec = load_mapping(rule_path)
    except ConfigError as exc:
        blockers.append(f"rule config unreadable: {exc}")
    else:
        rule_report = check_rule_freeze(spec, rule_path)
        current_hash = rule_spec_hash(spec)
        if not rule_report.is_executable:
            blockers.append(
                f"rule freeze is NOT executable ({len(rule_report.problems)} problem(s); "
                "run `uv run mnq rules check`)"
            )
        if record.rule_config_hash and record.rule_config_hash != current_hash:
            blockers.append("rule config has CHANGED since this plan recorded its hash; create a new experiment")

    if rule_report is not None and not rule_report.is_executable:
        if record.status is not ExperimentStatus.NOT_RUN_BLOCKED_BY_RULE_FREEZE:
            blockers.append(
                f"status is {record.status.value} but the rule freeze is not executable; "
                f"it must be {ExperimentStatus.NOT_RUN_BLOCKED_BY_RULE_FREEZE.value}"
            )
    for name in ("training_period", "validation_period", "holdout_period", "data_identifier", "data_hash", "cost_assumptions"):
        if getattr(record, name) is None:
            blockers.append(f"{name} is not decided (TBD)")
    return ExperimentAssessment(record, rule_report, current_hash, blockers)


def git_code_version(project_root: str | Path) -> str | None:
    """Current Git commit, suffixed with '+dirty' if there are uncommitted changes."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=project_root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return commit + ("+dirty" if dirty else "")


def read_registry(registry_path: str | Path) -> list[dict[str, Any]]:
    path = Path(registry_path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def register_experiment(
    record_path: str | Path,
    project_root: str | Path,
    registry_path: str | Path,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append a registration entry. Returns (entry, newly_registered).

    Raises ExperimentBlockedError if anything is undecided or the rule freeze
    is not executable, and RegistryConflictError if the ID already exists
    with different content. Nothing is written in either case.
    """
    record = load_experiment(record_path)
    assessment = assess_experiment(record, project_root)
    if not assessment.can_register:
        raise ExperimentBlockedError(assessment)

    plan = record.model_dump(mode="json", exclude={"created_at_utc", "code_version", "status"})
    plan["rule_config_hash"] = assessment.current_rule_hash
    plan_hash = hash_object(plan)

    for entry in read_registry(registry_path):
        if entry["experiment_id"] == record.experiment_id:
            if entry["plan_hash"] == plan_hash:
                return entry, False
            raise RegistryConflictError(
                f"{record.experiment_id} is already registered with DIFFERENT content "
                f"(registered at {entry['registered_at_utc']}). Registered experiments are immutable: "
                "give the changed plan a new experiment_id."
            )

    entry = {
        "experiment_id": record.experiment_id,
        "registered_at_utc": (now or dt.datetime.now(dt.timezone.utc)).isoformat(),
        "code_version": git_code_version(project_root),
        "status": ExperimentStatus.REGISTERED_NOT_RUN.value,
        "plan_hash": plan_hash,
        "plan": plan,
    }
    registry = Path(registry_path)
    registry.parent.mkdir(parents=True, exist_ok=True)
    with registry.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry, True
