"""Approved Databento acquisition (Round 16B): fail-closed preflight, idempotent batch jobs, immutable raw files.

Only the two requests recorded in ``data_acquisition.purchase_approval`` may
ever be submitted, byte-for-byte as approved:

    definition  2019-05-05T00:00:00Z .. 2026-09-26T00:00:00Z (exclusive)
    ohlcv-1m    2019-05-05T22:00:00Z .. 2026-09-26T00:00:00Z (exclusive)
    GLBX.MDP3, raw_symbol, the 31 contracts of the approved symbol manifest.

Preflight (all must pass, otherwise PURCHASE_PREFLIGHT_FAILED and nothing is submitted):
approved estimate and symbol-manifest hashes; generated requests equal the
approved ones; a fresh authoritative cost for both requests whose sum is <= the
approved maximum (USD 20.00 exactly qualifies); every symbol still resolves to
one instrument covering its roll window; the key exists; no verified download
already exists for that request.

Idempotency ledger (``outputs/acquisition/ACQUISITION_LEDGER.json``, committed):
an INTENT is written BEFORE submission. A later run that finds an INTENT
without a job id is UNCERTAIN and must reconcile with Databento (list_jobs)
before anything else; it never resubmits blindly. A job already SUBMITTED is
polled/downloaded, never resubmitted. A COMPLETED request is never
repurchased; missing local files are re-downloaded from the SAME job.

The API key is never logged, stored, hashed or placed in any record.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from mnq_research import data_estimate as de

LEDGER_PATH = Path("outputs/acquisition/ACQUISITION_LEDGER.json")
RAW_ROOT = Path("data/raw/databento")
SUBMIT_FORMAT = {"encoding": "dbn", "compression": "zstd", "split_duration": "none", "delivery": "download",
                 "stype_out": "instrument_id"}
APPROVED_SCHEMAS = ("definition", "ohlcv-1m")
JOB_STATES = "received,queued,processing,done,expired"
# Terminal outcomes of a downloaded stage. Only DOWNLOADED_AND_VALIDATED definitions permit the OHLCV-1m submission.
DOWNLOADED_AND_VALIDATED = "DOWNLOADED_AND_VALIDATED"
STAGE_OUTCOMES = (DOWNLOADED_AND_VALIDATED, "DOWNLOAD_FAILED", "VALIDATION_FAILED", "DONE_WITHOUT_AVAILABLE_FILES", "STATE_UNKNOWN")
VERIFIED_STATUSES = (DOWNLOADED_AND_VALIDATED, "COMPLETED_VERIFIED")


class PreflightFailed(RuntimeError):
    """PURCHASE_PREFLIGHT_FAILED: nothing was submitted."""


class ReconciliationRequired(RuntimeError):
    """A prior submission is uncertain; it must be reconciled with Databento before any retry."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def request_key(request: de.EstimateRequest) -> str:
    """Identity of an approved request: every parameter that defines what is purchased."""
    identity = {k: getattr(request, k) for k in ("dataset", "schema", "symbols", "stype_in", "start_utc", "end_utc")}
    return hashlib.sha256(de.canonical_json(identity).encode()).hexdigest()


# ------------------------------------------------------------------------------- approval


@dataclass(frozen=True)
class Approval:
    estimate_payload_sha256: str
    estimate_artifact: Path
    estimate_file_sha256: str
    symbol_manifest: Path
    symbols_sha256: str
    dataset: str
    stype_in: str
    requests: tuple[tuple[str, str, str], ...]  # (schema, start, end)
    maximum_usd: Decimal


def load_approval(spec: Mapping[str, Any], root: Path) -> Approval:
    pa = spec["data_acquisition"]["purchase_approval"]
    if pa.get("approved") is not True or not pa.get("approved_by") or not pa.get("approved_at_utc"):
        raise PreflightFailed("purchase_approval is not complete")
    p = pa["exact_request_parameters"]
    requests = tuple((r["schema"], r["start"], r["end"]) for r in p["requests"])
    if any(r.get("end_semantics") != "EXCLUSIVE" for r in p["requests"]):
        raise PreflightFailed("approved end semantics must be EXCLUSIVE")
    return Approval(pa["estimate_artifact_sha256"], root / p["estimate_artifact"], p["estimate_file_sha256"],
                    root / p["symbol_manifest"], p["symbols_sha256"], p["dataset"], p["stype_in"], requests,
                    Decimal(str(pa["maximum_permitted_charge_usd"])))


def approved_requests(spec: Mapping[str, Any], root: Path) -> tuple[Approval, list[de.EstimateRequest]]:
    """The downloader's requests, generated independently and checked against the approval and the estimate."""
    approval = load_approval(spec, root)
    problems: list[str] = []
    if not approval.estimate_artifact.is_file() or sha256_file(approval.estimate_artifact) != approval.estimate_file_sha256:
        problems.append("ESTIMATE_FILE_HASH_MISMATCH")
    elif not de.verify_artifact(approval.estimate_artifact):
        problems.append("ESTIMATE_PAYLOAD_HASH_INVALID")
    else:
        stored = json.loads(approval.estimate_artifact.read_text())
        if stored["sha256_of_payload"] != approval.estimate_payload_sha256:
            problems.append("ESTIMATE_PAYLOAD_HASH_NOT_APPROVED")
    manifest = json.loads(approval.symbol_manifest.read_text()) if approval.symbol_manifest.is_file() else {}
    canon = manifest.get("canonical_json", "")
    if hashlib.sha256(canon.encode()).hexdigest() != approval.symbols_sha256 or json.loads(canon or "[]") != manifest.get("symbols"):
        problems.append("SYMBOL_MANIFEST_HASH_MISMATCH")
    symbols = tuple(manifest.get("symbols", ()))
    first = dt.date.fromisoformat(str(spec["source_data"]["history_start_date"]))
    last = dt.date.fromisoformat(str(spec["source_data"]["history_end_date"]))
    generated = [r for r in de.alternatives(first, last) if r.schema in APPROVED_SCHEMAS and not r.windows]
    if tuple(r.symbols for r in generated) != (symbols, symbols):
        problems.append("GENERATED_SYMBOLS_DIFFER_FROM_MANIFEST")
    if [(r.schema, r.start_utc, r.end_utc) for r in generated] != list(approval.requests):
        problems.append("GENERATED_REQUESTS_DIFFER_FROM_APPROVAL")
    if any(r.dataset != approval.dataset or r.stype_in != approval.stype_in for r in generated):
        problems.append("DATASET_OR_STYPE_DIFFERS_FROM_APPROVAL")
    if not problems:
        estimate = json.loads(approval.estimate_artifact.read_text())["payload"]["results"]
        est = [(e["request"]["schema"], e["request"]["start_utc"], e["request"]["end_utc"], tuple(e["request"]["symbols"])) for e in estimate]
        if est != [(r.schema, r.start_utc, r.end_utc, r.symbols) for r in generated]:
            problems.append("GENERATED_REQUESTS_DIFFER_FROM_ESTIMATE")
    if problems:
        raise PreflightFailed(f"PURCHASE_PREFLIGHT_FAILED: {problems}")
    return approval, generated


def assert_approved(request: de.EstimateRequest, approved: list[de.EstimateRequest]) -> None:
    """Final guard immediately before submission: any difference is refused."""
    if request_key(request) not in {request_key(a) for a in approved} or request.schema not in APPROVED_SCHEMAS:
        raise PreflightFailed(f"PURCHASE_PREFLIGHT_FAILED: request not approved ({request.schema})")


# ------------------------------------------------------------------------------- ledger


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text()) if path.is_file() else {"entries": {}}

    def entry(self, key: str) -> dict[str, Any] | None:
        return self.data["entries"].get(key)

    def put(self, key: str, entry: dict[str, Any]) -> None:
        self.data["entries"][key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True, default=str) + "\n")
        os.replace(tmp, self.path)  # atomic: an intent is durable before the submission call


def raw_files_verified(entry: Mapping[str, Any], root: Path) -> bool:
    files = entry.get("raw_files") or []
    return bool(files) and all((root / f["path"]).is_file() and sha256_file(root / f["path"]) == f["sha256"] for f in files)


# ------------------------------------------------------------------------------- preflight


@dataclass
class PreflightReport:
    passed: bool
    problems: list[str] = field(default_factory=list)
    fresh_costs_usd: dict[str, str] = field(default_factory=dict)
    fresh_combined_usd: str | None = None
    checked_utc: str = ""
    estimate_results: list[dict[str, Any]] = field(default_factory=list)


def preflight(spec: Mapping[str, Any], root: Path, metadata_client: de.MetadataOnlyClient, client_version: str,
              secret: str | None, ledger: Ledger) -> tuple[PreflightReport, Approval | None, list[de.EstimateRequest]]:
    report = PreflightReport(False, checked_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    try:
        approval, requests = approved_requests(spec, root)
    except (PreflightFailed, KeyError, ValueError, FileNotFoundError) as exc:
        report.problems.append(de.redact(str(exc), secret))
        return report, None, []
    if not secret:
        report.problems.append("DATABENTO_API_KEY_MISSING")
    total = Decimal(0)
    for request in requests:
        result = de.estimate(request, metadata_client, client_version, secret)
        report.estimate_results.append(asdict(result))
        if result.status is not de.EstimateStatus.KNOWN:
            report.problems.append(f"FRESH_ESTIMATE_UNKNOWN:{request.schema}:{result.warnings}")
            continue
        if result.unresolved_symbols:
            report.problems.append(f"SYMBOL_RESOLUTION_FAILED:{result.unresolved_symbols}")
        report.fresh_costs_usd[request.schema] = result.estimated_charge_usd
        total += Decimal(result.estimated_charge_usd)
    if len(report.fresh_costs_usd) == len(requests):
        report.fresh_combined_usd = str(total)
        if total > approval.maximum_usd:
            report.problems.append(f"FRESH_COST_ABOVE_APPROVED_MAXIMUM:{total}>{approval.maximum_usd}")
    report.passed = not report.problems
    return report, approval, requests


# ------------------------------------------------------------------------------- acquisition


def _find_matching_job(jobs: list[dict[str, Any]], request: de.EstimateRequest) -> list[dict[str, Any]]:
    def same(job: Mapping[str, Any]) -> bool:
        symbols = job.get("symbols")
        symbols = symbols.split(",") if isinstance(symbols, str) else list(symbols or [])
        return (job.get("dataset") == request.dataset and job.get("schema") == request.schema
                and sorted(symbols) == sorted(request.symbols) and str(job.get("start", "")).startswith(request.start_utc[:19].replace("Z", ""))
                and str(job.get("end", "")).startswith(request.end_utc[:19].replace("Z", "")))
    return [j for j in jobs if same(j)]


def acquire_one(request: de.EstimateRequest, approved: list[de.EstimateRequest], batch: Any, ledger: Ledger, root: Path,
                client_version: str, preflight_cost_usd: str, poll_seconds: float = 10.0, timeout_seconds: float = 3600.0,
                sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Submit (once), wait, download and hash one approved request. Idempotent and reconciling."""
    assert_approved(request, approved)
    key = request_key(request)
    entry = ledger.entry(key)
    if entry and entry.get("status") in VERIFIED_STATUSES and raw_files_verified(entry, root):
        return entry  # never repurchase or redownload a verified complete artifact
    if entry and not entry.get("job_id"):
        # INTENT without a job id: the submission outcome is uncertain. Reconcile, never resubmit blindly.
        matches = _find_matching_job(batch.list_jobs(states="received,queued,processing,done,expired",
                                                     since=entry["intent_utc"]), request)
        if len(matches) != 1:
            entry.update(status="RECONCILIATION_REQUIRED", reconciliation_candidates=len(matches))
            ledger.put(key, entry)
            raise ReconciliationRequired(f"{request.schema}: {len(matches)} matching Databento jobs; resolve manually")
        entry.update(job_id=matches[0]["id"], status="SUBMITTED", reconciled=True)
        ledger.put(key, entry)
    if not entry:
        entry = {"request_key": key, "request": asdict(request), "submit_format": SUBMIT_FORMAT, "client_version": client_version,
                 "cost_quoted_before_submission_usd": preflight_cost_usd, "status": "INTENT_RECORDED",
                 "intent_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        ledger.put(key, entry)  # durable BEFORE the paid call
        job = batch.submit_job(dataset=request.dataset, symbols=list(request.symbols), schema=request.schema,
                               start=request.start_utc, end=request.end_utc, stype_in=request.stype_in, **SUBMIT_FORMAT)
        entry.update(job_id=job["id"], status="SUBMITTED", submitted_job=job)
        ledger.put(key, entry)
    job_id = entry["job_id"]
    waited = 0.0
    while True:
        jobs = [j for j in batch.list_jobs(states="received,queued,processing,done,expired", since=entry["intent_utc"]) if j.get("id") == job_id]
        state = jobs[0].get("state") if jobs else None
        if state == "done":
            entry["final_job"] = jobs[0]
            break
        if state == "expired" or (state is None and jobs == [] and waited >= timeout_seconds):
            entry.update(status="RECONCILIATION_REQUIRED", last_state=state)
            ledger.put(key, entry)
            raise ReconciliationRequired(f"job {job_id} state {state}")
        if waited >= timeout_seconds:
            raise TimeoutError(f"job {job_id} still {state}; rerun the same command to resume (never resubmits)")
        sleep(poll_seconds)
        waited += poll_seconds
    ledger.put(key, entry)
    return download_job_files(entry, batch, ledger, root)



# ------------------------------------------------------------------------------- staged acquisition (Round 16B resume)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def job_state(batch: Any, entry: Mapping[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """One read-only list_jobs query for the ledger's job id (Databento may omit most fields)."""
    jobs = [j for j in batch.list_jobs(states=JOB_STATES, since=entry["intent_utc"]) if j.get("id") == entry["job_id"]]
    return (jobs[0].get("state"), jobs[0]) if len(jobs) == 1 else (None, None)


def download_job_files(entry: dict[str, Any], batch: Any, ledger: Ledger, root: Path) -> dict[str, Any]:
    """Download ONLY the files Databento lists for this existing job, one by one, each checked against the listed size and SHA-256.

    Never submits anything. Raw files are written once and never modified; outcome is DOWNLOADED_UNVALIDATED,
    DOWNLOAD_FAILED or DONE_WITHOUT_AVAILABLE_FILES.
    """
    key, job_id = entry["request_key"], entry["job_id"]
    listed = batch.list_files(job_id)
    entry["available_files"] = [{"filename": f["filename"], "size": f["size"], "hash": f["hash"]} for f in listed]
    entry["files_listed_utc"] = _now()
    if not listed:
        entry["status"] = "DONE_WITHOUT_AVAILABLE_FILES"
        ledger.put(key, entry)
        return entry
    out_dir = root / RAW_ROOT / entry["request"]["schema"].replace("-", "_")
    files, problems = [], []
    for f in sorted(listed, key=lambda f: f["filename"]):
        target = out_dir / job_id / f["filename"]
        algo, _, expected = str(f["hash"]).partition(":")
        if target.exists():  # an already-present raw file is kept only if it is exactly the listed file; never overwritten
            if not (target.is_file() and sha256_file(target) == expected and target.stat().st_size == int(f["size"])):
                problems.append(f"REFUSING_TO_OVERWRITE_EXISTING_RAW_FILE:{f['filename']}")
                continue
        else:
            try:
                paths = batch.download(job_id=job_id, output_dir=out_dir, filename_to_download=f["filename"])
            except Exception as exc:  # network / server error: the job is NOT resubmitted
                problems.append(f"DOWNLOAD_ERROR:{f['filename']}:{type(exc).__name__}")
                continue
            if [Path(p).resolve() for p in paths] != [target.resolve()] or not target.is_file():
                problems.append(f"UNEXPECTED_DOWNLOAD_PATHS:{f['filename']}")
                continue
        digest, size = sha256_file(target), target.stat().st_size
        if algo != "sha256" or digest != expected:
            problems.append(f"HASH_MISMATCH_WITH_DATABENTO_LISTING:{f['filename']}")
        if size != int(f["size"]):
            problems.append(f"SIZE_MISMATCH_WITH_DATABENTO_LISTING:{f['filename']}")
        files.append({"path": str(target.relative_to(root)), "sha256": digest, "bytes": size, "databento_hash": f["hash"]})
    entry.update(raw_files=files, downloaded_utc=_now(), final_charge_usd=(entry.get("final_job") or {}).get("cost_usd"),
                 git_tracked=False, download_problems=problems,
                 storage=f"local container path {RAW_ROOT} (git-ignored, licensed data); re-downloadable from job {job_id} while Databento retains it")
    entry["status"] = "DOWNLOAD_FAILED" if problems else "DOWNLOADED_UNVALIDATED"
    ledger.put(key, entry)
    return entry


def validate_definitions_entry(entry: dict[str, Any], request: de.EstimateRequest, ledger: Ledger, root: Path) -> dict[str, Any]:
    """Hash-verify, decode and validate the definitions DBN against the approved request -> DOWNLOADED_AND_VALIDATED / VALIDATION_FAILED."""
    from mnq_research import raw_ingest as ri

    if request.schema != "definition" or entry.get("status") != "DOWNLOADED_UNVALIDATED":
        raise ValueError("only downloaded, unvalidated definitions can be validated here")
    dbn = [f for f in entry["raw_files"] if f["path"].endswith((".dbn.zst", ".dbn"))]
    issues: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    if len(dbn) != 1:
        issues.append({"code": "EXPECTED_ONE_DBN_FILE", "count": len(dbn), "blocking": True})
    else:
        try:
            meta, df = ri.load_dbn(root / dbn[0]["path"], dbn[0]["sha256"])
        except Exception as exc:  # corrupt / truncated / altered
            issues.append({"code": "DECODE_OR_HASH_FAILURE", "count": 1, "blocking": True, "examples": [f"{type(exc).__name__}: {exc}"]})
        else:
            v = ri.check_metadata(meta, request.dataset, request.schema, request.symbols, request.start_utc, request.end_utc)
            dv, ids = ri.validate_definitions(df.reset_index(), request.symbols)
            rtypes = sorted({int(r) for r in df["rtype"].unique()}) if "rtype" in df else []
            v.add("UNEXPECTED_RECORD_TYPE", int(rtypes != [19]), True, rtypes)  # 19 = InstrumentDefMsg
            issues = [i.__dict__ for i in v.issues + dv.issues]
            summary = {"records_decoded": int(len(df)), "metadata": {"dataset": str(meta.dataset), "schema": str(getattr(meta.schema, "value", meta.schema)),
                       "stype_in": str(getattr(meta.stype_in, "value", meta.stype_in)), "start_ns": int(meta.start), "end_ns": int(meta.end),
                       "symbol_count": len(meta.symbols), "not_found": list(getattr(meta, "not_found", []) or [])},
                       "record_types": rtypes, "instrument_ids": ids,
                       "records_per_symbol": {s: int(n) for s, n in df.reset_index()["raw_symbol"].astype(str).value_counts().sort_index().items()}}
    blocking = [i for i in issues if i.get("blocking") and i.get("count")]
    entry.update(definitions_validation={"validated_utc": _now(), "passed": not blocking, "issues": issues, **summary},
                 status="VALIDATION_FAILED" if blocking else DOWNLOADED_AND_VALIDATED)
    ledger.put(entry["request_key"], entry)
    return entry


def ohlcv_submission_problems(ledger: Ledger, approved: list[de.EstimateRequest], batch: Any, root: Path) -> list[str]:
    """Fail-closed gate before the single OHLCV-1m submission."""
    definition, ohlcv = approved
    problems = []
    d = ledger.entry(request_key(definition)) or {}
    if d.get("status") not in VERIFIED_STATUSES or not raw_files_verified(d, root):
        problems.append(f"DEFINITIONS_NOT_DOWNLOADED_AND_VALIDATED:{d.get('status')}")
    if ledger.entry(request_key(ohlcv)) is not None:
        problems.append("OHLCV_LEDGER_ENTRY_ALREADY_EXISTS")
    known = {e.get("job_id") for e in ledger.data["entries"].values()}
    since = min((e["intent_utc"] for e in ledger.data["entries"].values()), default="2026-09-30T00:00:00Z")
    unknown = [j.get("id") for j in batch.list_jobs(states=JOB_STATES, since=since) if j.get("id") not in known]
    if unknown:  # list_jobs may omit schema fields, so ANY unrecorded job blocks (it could be an OHLCV job)
        problems.append(f"UNRECORDED_DATABENTO_JOBS_EXIST:{unknown}")
    return problems


def submit_once(request: de.EstimateRequest, approved: list[de.EstimateRequest], batch: Any, ledger: Ledger, client_version: str,
                preflight_cost_usd: str, approval_payload_sha256: str) -> dict[str, Any]:
    """Write the INTENT, submit exactly once, persist the job id. Refuses if ANY ledger entry exists for the request."""
    assert_approved(request, approved)
    key = request_key(request)
    if ledger.entry(key) is not None:
        raise PreflightFailed(f"PURCHASE_PREFLIGHT_FAILED: ledger already has an entry for {request.schema}; never resubmitted")
    entry = {"request_key": key, "request": asdict(request), "submit_format": SUBMIT_FORMAT, "client_version": client_version,
             "cost_quoted_before_submission_usd": preflight_cost_usd, "approval_payload_sha256": approval_payload_sha256,
             "status": "INTENT_RECORDED", "intent_utc": _now()}
    ledger.put(key, entry)  # durable BEFORE the paid call
    job = batch.submit_job(dataset=request.dataset, symbols=list(request.symbols), schema=request.schema,
                           start=request.start_utc, end=request.end_utc, stype_in=request.stype_in, **SUBMIT_FORMAT)
    entry.update(job_id=job["id"], status="SUBMITTED", submitted_job=job, databento_ts_received=job.get("ts_received"))
    ledger.put(key, entry)
    return entry


def poll_until_done(entry: dict[str, Any], batch: Any, ledger: Ledger, timeout_seconds: float, poll_seconds: float = 30.0,
                    sleep: Callable[[float], None] = time.sleep) -> str | None:
    """Bounded read-only polling. Returns the last state; never cancels or resubmits. Records only the status and check time."""
    waited = 0.0
    while True:
        state, job = job_state(batch, entry)
        entry["last_status_check"] = {"checked_at_utc": _now(), "databento_state": state, "fields_returned_by_list_jobs": job}
        if state == "done":
            entry["final_job"] = job
        ledger.put(entry["request_key"], entry)
        if state in ("done", "expired") or waited >= timeout_seconds:
            return state
        sleep(poll_seconds)
        waited += poll_seconds


def _advance_existing(entry: dict[str, Any], request: de.EstimateRequest, batch: Any, ledger: Ledger, root: Path,
                      poll_timeout: float, poll_seconds: float, sleep: Callable[[float], None]) -> None:
    """Move an already-submitted job forward without ever submitting: poll (bounded), download its own files, validate definitions."""
    if not entry.get("job_id"):
        raise ReconciliationRequired(f"{request.schema}: intent without a job id; reconcile with Databento before anything else")
    if entry["status"] == "SUBMITTED":
        state = poll_until_done(entry, batch, ledger, poll_timeout, poll_seconds, sleep)
        if state is None or state == "expired":
            entry.update(status="STATE_UNKNOWN", last_state=state)
            ledger.put(entry["request_key"], entry)
            return
        if state != "done":
            return  # still pending: job id and ledger preserved, nothing cancelled or resubmitted
    if entry["status"] in ("SUBMITTED", "DOWNLOAD_FAILED") or (entry["status"] in VERIFIED_STATUSES and not raw_files_verified(entry, root)):
        download_job_files(entry, batch, ledger, root)
    if request.schema == "definition" and entry["status"] == "DOWNLOADED_UNVALIDATED":
        validate_definitions_entry(entry, request, ledger, root)


def run_staged(spec: Mapping[str, Any], root: Path, batch: Any, metadata_client: de.MetadataOnlyClient, client_version: str,
               secret: str | None, ledger: Ledger, poll_timeout: float = 600.0, poll_seconds: float = 30.0,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Definitions first; the single OHLCV-1m submission only after DOWNLOADED_AND_VALIDATED definitions and a passing preflight.

    Returns immediately after ANY submission (outcome *_SUBMITTED_CHECKPOINT) so the job id is committed before polling.
    """
    approval, requests = approved_requests(spec, root)
    definition, ohlcv = requests
    result: dict[str, Any] = {"preflight": None}

    def checked_preflight() -> PreflightReport:
        report, _, _ = preflight(spec, root, metadata_client, client_version, secret, ledger)
        result["preflight"] = report
        return report

    d = ledger.entry(request_key(definition))
    if d is None:
        report = checked_preflight()
        if not report.passed:
            return {**result, "outcome": "PURCHASE_PREFLIGHT_FAILED"}
        submit_once(definition, requests, batch, ledger, client_version, report.fresh_costs_usd["definition"], approval.estimate_payload_sha256)
        return {**result, "outcome": "DEFINITIONS_SUBMITTED_CHECKPOINT"}
    if not (d["status"] in VERIFIED_STATUSES and raw_files_verified(d, root)):
        _advance_existing(d, definition, batch, ledger, root, poll_timeout, poll_seconds, sleep)
    if d["status"] not in VERIFIED_STATUSES:
        return {**result, "outcome": f"DEFINITIONS_{d['status']}"}
    o = ledger.entry(request_key(ohlcv))
    if o is None:
        report = checked_preflight()
        if not report.passed:
            return {**result, "outcome": "PURCHASE_PREFLIGHT_FAILED"}
        problems = ohlcv_submission_problems(ledger, requests, batch, root)
        if problems:
            return {**result, "outcome": "OHLCV_SUBMISSION_BLOCKED", "problems": problems}
        submit_once(ohlcv, requests, batch, ledger, client_version, report.fresh_costs_usd["ohlcv-1m"], approval.estimate_payload_sha256)
        return {**result, "outcome": "OHLCV_SUBMITTED_CHECKPOINT"}
    if not (o["status"] in VERIFIED_STATUSES and raw_files_verified(o, root)):
        _advance_existing(o, ohlcv, batch, ledger, root, poll_timeout, poll_seconds, sleep)
    return {**result, "outcome": f"OHLCV_{o['status']}"}
