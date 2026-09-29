"""Databento cost ESTIMATION only (Round 16A). Never downloads data.

The estimator talks to Databento exclusively through ``MetadataOnlyClient``,
which exposes four metadata calls and nothing else:

    metadata.get_cost, metadata.get_billable_size, metadata.get_record_count,
    symbology.resolve

There is no timeseries, batch or live access anywhere in this module, so no
paid job, stream or download can be started from here.

Fail-closed rules:
* a charge is KNOWN only when Databento returned a finite, non-negative
  number; anything else (error, missing key, malformed reply) is UNKNOWN and
  carries ``estimated_charge_usd = None``, never 0;
* every raw symbol must resolve to exactly one instrument over the requested
  range, otherwise the estimate is UNKNOWN;
* requests are limited to explicit individual quarterly MNQ contracts
  (``stype_in = raw_symbol``); parent, continuous and wildcard symbols are
  refused, so a request can never expand to unrelated instruments;
* the API key is read only from DATABENTO_API_KEY, never stored, logged,
  written to an artifact or included in an error message.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field  # noqa: F401  (asdict re-exported for the CLI)
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable

API_KEY_ENV = "DATABENTO_API_KEY"
DATASET = "GLBX.MDP3"
ROOT = "MNQ"
STYPE_IN = "raw_symbol"
FEED_MODE = "historical"
ALLOWED_SCHEMAS = ("definition", "ohlcv-1m", "ohlcv-1s", "trades")
QUARTER_MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
CONTRACT_PATTERN = re.compile(r"^MNQ[HMUZ][0-9]$")
NOT_EXPOSED = "NOT_EXPOSED_BY_DATABENTO_METADATA_API"


class MissingCredentialError(RuntimeError):
    """DATABENTO_API_KEY is not set. The message never contains any key material."""


class EstimateStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"  # never interpreted as zero


def load_api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "")
    if not key.strip():
        raise MissingCredentialError(f"{API_KEY_ENV} is not set; no estimate can be requested")
    return key


def redact(text: str, secret: str | None) -> str:
    """Remove the key (if any) from text that may be stored or shown."""
    text = str(text)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return re.sub(r"db-[A-Za-z0-9]{8,}", "[REDACTED]", text)


# ------------------------------------------------------------------------------- contracts


def third_friday(year: int, month: int) -> dt.date:
    fridays = [d for d in calendar.Calendar().itermonthdates(year, month) if d.month == month and d.weekday() == calendar.FRIDAY]
    return fridays[2]


def roll_date(year: int, month: int) -> dt.date:
    """Frozen roll rule: the Monday preceding the third Friday of the expiration month."""
    return third_friday(year, month) - dt.timedelta(days=4)


def designated_contracts(first_trade_date: dt.date, last_trade_date: dt.date) -> tuple[str, ...]:
    """Every quarterly contract designated at some point in the range (deterministic, calendar-based).

    The contract expiring in quarter Q is designated from the previous quarter's roll date up to (not
    including) its own roll date. Holiday adjustments of the roll moment never add or remove a contract.
    """
    out: list[str] = []
    year, month = first_trade_date.year, first_trade_date.month
    # first quarterly month whose roll date is after the first trade date
    while True:
        if month in QUARTER_MONTH_CODES and roll_date(year, month) > first_trade_date:
            break
        month += 1
        if month > 12:
            year, month = year + 1, 1
    while True:
        out.append(f"{ROOT}{QUARTER_MONTH_CODES[month]}{year % 10}")
        if roll_date(year, month) > last_trade_date:
            return tuple(out)
        month += 3
        if month > 12:
            year, month = year + 1, month - 12


# ------------------------------------------------------------------------------- requests


@dataclass(frozen=True)
class EstimateRequest:
    alternative: str  # A..E
    description: str
    schema: str
    symbols: tuple[str, ...]
    start_utc: str  # ISO 8601, inclusive
    end_utc: str  # ISO 8601, exclusive (Databento convention)
    windows: tuple[tuple[str, str], ...] = ()  # optional disjoint sub-windows (alternative E); summed
    dataset: str = DATASET
    stype_in: str = STYPE_IN
    mode: str = FEED_MODE

    def __post_init__(self) -> None:
        if self.dataset != DATASET:
            raise ValueError(f"only {DATASET} is permitted")
        if self.stype_in != STYPE_IN:
            raise ValueError("only individual contracts (stype_in=raw_symbol) are permitted; no parent or continuous symbols")
        if self.schema not in ALLOWED_SCHEMAS:
            raise ValueError(f"schema must be one of {ALLOWED_SCHEMAS}")
        if not self.symbols or any(not CONTRACT_PATTERN.match(s) for s in self.symbols):
            raise ValueError("symbols must be explicit individual quarterly MNQ contracts, e.g. MNQZ6")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("duplicate symbols would double-count data")
        spans = sorted(self.windows) or [(self.start_utc, self.end_utc)]
        for (a0, a1), (b0, _) in zip(spans, spans[1:]):
            if b0 < a1:
                raise ValueError("windows overlap: the estimate would double-count data")


@dataclass
class EstimateResult:
    request: dict[str, Any]
    status: EstimateStatus
    estimated_cost_before_credits_usd: str | None
    applicable_credits_usd: str | None
    estimated_charge_usd: str | None
    currency: str
    billable_size_uncompressed_bytes: int | None
    compressed_size_bytes: str
    record_count: int | None
    symbol_resolution: dict[str, Any]
    includes_definitions: bool
    double_count_risk: str
    warnings: list[str] = field(default_factory=list)
    raw_responses: dict[str, Any] = field(default_factory=dict)
    retrieved_utc: str = ""
    client_version: str = ""


class MetadataOnlyClient:
    """Allow-list wrapper: only cost/size/count metadata and symbology resolution are reachable."""

    def __init__(self, client: Any):
        self._metadata = client.metadata
        self._symbology = client.symbology

    def get_cost(self, **kw: Any) -> Any:
        return self._metadata.get_cost(**kw)

    def get_billable_size(self, **kw: Any) -> Any:
        return self._metadata.get_billable_size(**kw)

    def get_record_count(self, **kw: Any) -> Any:
        return self._metadata.get_record_count(**kw)

    def resolve(self, **kw: Any) -> Any:
        return self._symbology.resolve(**kw)


def _finite_non_negative(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _resolution_problems(response: Any, symbols: tuple[str, ...]) -> list[str]:
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        return ["SYMBOL_RESOLUTION_RESPONSE_MALFORMED"]
    problems = [f"UNRESOLVED:{s}" for s in response.get("not_found", []) or []]
    problems += [f"PARTIALLY_RESOLVED:{s}" for s in response.get("partial", []) or []]
    for s in symbols:
        mappings = response["result"].get(s)
        if not mappings:
            problems.append(f"UNRESOLVED:{s}")
        elif len({m.get("s") for m in mappings}) != 1:
            problems.append(f"AMBIGUOUS_INSTRUMENT_ID:{s}")
    return sorted(set(problems))


def estimate(request: EstimateRequest, client: MetadataOnlyClient, client_version: str, secret: str | None,
             now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> EstimateResult:
    """One authoritative estimate, or UNKNOWN. Makes metadata calls only."""
    result = EstimateResult(
        request=asdict(request), status=EstimateStatus.UNKNOWN, estimated_cost_before_credits_usd=None,
        applicable_credits_usd=None, estimated_charge_usd=None, currency="USD",
        billable_size_uncompressed_bytes=None, compressed_size_bytes=NOT_EXPOSED, record_count=None,
        symbol_resolution={}, includes_definitions=request.schema == "definition",
        double_count_risk=(
            "NONE: one request per alternative; each record belongs to exactly one instrument; symbols are unique; "
            "sub-windows (if any) are disjoint and validated. Roll-week overlap is two DIFFERENT contracts, both required."
        ),
        retrieved_utc=now().isoformat(), client_version=client_version,
    )
    result.warnings.append(f"CREDITS_{NOT_EXPOSED}: the final charge equals the pre-credit estimate unless the portal shows credits")
    try:
        resolution = client.resolve(dataset=request.dataset, symbols=list(request.symbols), stype_in=request.stype_in,
                                    stype_out="instrument_id", start_date=request.start_utc[:10], end_date=request.end_utc[:10])
        result.raw_responses["symbology.resolve"] = resolution
        problems = _resolution_problems(resolution, request.symbols)
        result.symbol_resolution = {"problems": problems}
        if problems:
            result.warnings += problems
            return result
        windows = request.windows or ((request.start_utc, request.end_utc),)
        cost, size, count = Decimal(0), 0, 0
        for start, end in windows:
            kw = dict(dataset=request.dataset, symbols=list(request.symbols), schema=request.schema,
                      stype_in=request.stype_in, start=start, end=end)
            c = client.get_cost(mode=request.mode, **kw)
            b = client.get_billable_size(**kw)
            n = client.get_record_count(**kw)
            result.raw_responses.setdefault("windows", []).append({"start": start, "end": end, "get_cost": c, "get_billable_size": b, "get_record_count": n})
            if not (_finite_non_negative(c) and _finite_non_negative(b) and _finite_non_negative(n)):
                result.warnings.append(f"NON_AUTHORITATIVE_RESPONSE for window {start}..{end}")
                return result
            cost += Decimal(str(c))
            size += int(b)
            count += int(n)
    except Exception as exc:  # noqa: BLE001 - any failure is UNKNOWN, never zero
        result.warnings.append(f"REQUEST_FAILED: {redact(f'{type(exc).__name__}: {exc}', secret)}")
        return result
    result.status = EstimateStatus.KNOWN
    result.estimated_cost_before_credits_usd = str(cost)
    result.estimated_charge_usd = str(cost)  # credits are not exposed; see warning
    result.billable_size_uncompressed_bytes = size
    result.record_count = count
    return result


# ------------------------------------------------------------------------------- the five alternatives


def session_bounds(first_trade_date: dt.date, last_trade_date: dt.date) -> tuple[str, str]:
    """UTC bounds covering the CME Globex sessions of the frozen trade-date range.

    Trade date D's session opens 18:00 New York on the previous calendar day; the last session closes
    17:00 New York on the last trade date. The end is exclusive and placed at the next UTC midnight
    after that close (no MNQ trading occurs between a Friday close and the Sunday open).
    """
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    start = dt.datetime.combine(first_trade_date - dt.timedelta(days=1), dt.time(18), ny).astimezone(dt.timezone.utc)
    close = dt.datetime.combine(last_trade_date, dt.time(17), ny).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(close.date() + dt.timedelta(days=1), dt.time(0), dt.timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def rth_windows(first_trade_date: dt.date, last_trade_date: dt.date) -> tuple[tuple[str, str], ...]:
    """Disjoint 09:30-12:05 New York windows on every weekday (DST-correct), for alternative E."""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    out = []
    day = first_trade_date
    while day <= last_trade_date:
        if day.weekday() < 5:
            a = dt.datetime.combine(day, dt.time(9, 30), ny).astimezone(dt.timezone.utc)
            b = dt.datetime.combine(day, dt.time(12, 5), ny).astimezone(dt.timezone.utc)
            out.append((a.strftime("%Y-%m-%dT%H:%M:%SZ"), b.strftime("%Y-%m-%dT%H:%M:%SZ")))
        day += dt.timedelta(days=1)
    return tuple(out)


def alternatives(first_trade_date: dt.date, last_trade_date: dt.date) -> list[EstimateRequest]:
    symbols = designated_contracts(first_trade_date, last_trade_date)
    start, end = session_bounds(first_trade_date, last_trade_date)
    windows = rth_windows(first_trade_date, last_trade_date)
    return [
        EstimateRequest("A", "Instrument definitions for every designated contract (symbols, expirations, tick metadata)", "definition", symbols, start, end),
        EstimateRequest("B", "Complete OHLCV-1m history, individual contracts, unadjusted", "ohlcv-1m", symbols, start, end),
        EstimateRequest("C", "Complete OHLCV-1s history, individual contracts, unadjusted", "ohlcv-1s", symbols, start, end),
        EstimateRequest("D", "Complete trade-level history, individual contracts", "trades", symbols, start, end),
        EstimateRequest("E1", "OHLCV-1s restricted to 09:30-12:05 New York each weekday (covers every entry, stop, target and the 12:00 exit)", "ohlcv-1s", symbols, windows[0][0], windows[-1][1], windows),
        EstimateRequest("E2", "Trades restricted to 09:30-12:05 New York each weekday", "trades", symbols, windows[0][0], windows[-1][1], windows),
    ]


# ------------------------------------------------------------------------------- artifact


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def write_artifact(results: list[EstimateResult], out_dir: Path, secret: str | None) -> Path:
    """Write a hash-addressed JSON artifact. Refuses to write anything containing the key."""
    payload = {"kind": "DATABENTO_COST_ESTIMATE", "results": [asdict(r) for r in results]}
    text = canonical_json(payload)
    if secret and secret in text:
        raise RuntimeError("refusing to write an artifact containing credential material")
    digest = hashlib.sha256(text.encode()).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"databento_estimate_{digest[:16]}.json"
    path.write_text(json.dumps({"sha256_of_payload": digest, "payload": payload}, indent=2, sort_keys=True, default=str))
    return path


def verify_artifact(path: Path) -> bool:
    data = json.loads(Path(path).read_text())
    return hashlib.sha256(canonical_json(data["payload"]).encode()).hexdigest() == data["sha256_of_payload"]
