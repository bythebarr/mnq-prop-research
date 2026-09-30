"""Calendar-source registry (Round 16A): acquisition PLAN and fail-closed readiness.

A calendar source (CME holidays/early closes, official release schedules,
FOMC, Chair testimony, session/roll calendars) may be marked READY only when
it has: an official URL, a UTC retrieval timestamp, a covered date range, an
immutable raw artifact whose SHA-256 matches, a parser version, a
parsed-output SHA-256 that matches, and an explicit time-zone/DST treatment.
Anything missing keeps it PLANNED; a mismatch is refused. A current recurring
rule ("first Friday", "every Thursday 08:30") is never accepted in place of
archived historical release timestamps.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from mnq_research.config import load_mapping


class SourceStatus(str, Enum):
    PLANNED = "PLANNED"
    READY = "READY"


REQUIRED_FOR_READY = ("official_url", "retrieved_utc", "covered_start", "covered_end", "raw_artifact_path",
                      "raw_sha256", "parser_version", "parsed_output_path", "parsed_sha256", "timezone_treatment")


@dataclass(frozen=True)
class CalendarSource:
    source_id: str
    covers: str
    responsible_body: str
    official_url: str
    url_verified: bool
    timezone_treatment: str
    status: SourceStatus = SourceStatus.PLANNED
    retrieved_utc: str | None = None
    covered_start: str | None = None
    covered_end: str | None = None
    raw_artifact_path: str | None = None
    raw_sha256: str | None = None
    parser_version: str | None = None
    parsed_output_path: str | None = None
    parsed_sha256: str | None = None
    recurring_rule_substitute: bool = False
    access_status: str = "NOT_ATTEMPTED"  # e.g. RAW_CAPTURED, ACCESS_BLOCKED_AKAMAI_403, ACCESS_BLOCKED_TIMEOUT
    raw_captures: tuple = ()  # additional raw index pages captured (url, sha256, path, retrieved_utc)
    notes: str = ""

    def readiness_problems(self) -> list[str]:
        problems = [f"MISSING:{name}" for name in REQUIRED_FOR_READY if not getattr(self, name)]
        if not self.url_verified:
            problems.append("URL_NOT_VERIFIED")
        if self.recurring_rule_substitute:
            problems.append("RECURRING_RULE_IS_NOT_AN_ARCHIVED_HISTORY")
        for path_name, hash_name in (("raw_artifact_path", "raw_sha256"), ("parsed_output_path", "parsed_sha256")):
            path, expected = getattr(self, path_name), getattr(self, hash_name)
            if path and expected:
                p = Path(path)
                if not p.is_file():
                    problems.append(f"FILE_MISSING:{path_name}")
                elif hashlib.sha256(p.read_bytes()).hexdigest() != expected:
                    problems.append(f"HASH_MISMATCH:{path_name}")
        return problems

    def mark_ready(self) -> "CalendarSource":
        problems = self.readiness_problems()
        if problems:
            raise ValueError(f"{self.source_id} cannot be READY: {problems}")
        return replace(self, status=SourceStatus.READY)


def load_plan(path: str | Path) -> list[CalendarSource]:
    data: Mapping[str, Any] = load_mapping(path)
    sources = []
    for item in data["sources"]:
        source = CalendarSource(**{**item, "status": SourceStatus(item.get("status", "PLANNED")),
                                   "raw_captures": tuple(tuple(c) for c in item.get("raw_captures", ()))})
        if source.status is SourceStatus.READY and source.readiness_problems():
            raise ValueError(f"{source.source_id} is declared READY but fails: {source.readiness_problems()}")
        sources.append(source)
    return sources
