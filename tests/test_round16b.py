"""Round 16B: approved acquisition, idempotency and raw-data validation.

No test touches the network or Databento: fake batch/metadata clients record
every call and fail on anything unexpected.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from test_entry_order import SPEC
from test_round16a import CONTRACTS, FAKE_KEY, FakeHistorical
from mnq_research import data_acquisition as da
from mnq_research import data_estimate as de

ROOT = Path(__file__).resolve().parents[1]


def metadata(calls, cost_a=0.009413488209, cost_b=14.254553765059):
    class Meta:
        def __init__(self):
            self.n = 0

        def get_cost(self, **kw):
            calls.append(("get_cost", kw))
            return cost_a if kw["schema"] == "definition" else cost_b

        def get_billable_size(self, **kw):
            calls.append(("get_billable_size", kw))
            return 100

        def get_record_count(self, **kw):
            calls.append(("get_record_count", kw))
            return 10

    h = FakeHistorical(calls)
    h.metadata = Meta()
    return de.MetadataOnlyClient(h)


class FakeBatch:
    def __init__(self, root: Path, jobs=None, fail_submit=False):
        self.root, self.jobs, self.submits, self.downloads, self.fail_submit = root, jobs or [], [], [], fail_submit

    def submit_job(self, **kw):
        if self.fail_submit:
            raise ConnectionError("connection reset during submit")
        job = {"id": f"GLBX-JOB-{len(self.submits) + 1}", "state": "done", "cost_usd": 14.25, "dataset": kw["dataset"],
               "schema": kw["schema"], "symbols": ",".join(kw["symbols"]), "start": kw["start"].replace("Z", ".000000000Z"),
               "end": kw["end"].replace("Z", ".000000000Z")}
        self.submits.append(kw)
        self.jobs.append(job)
        return job

    def list_jobs(self, states=None, since=None):
        return list(self.jobs)

    def download(self, job_id, output_dir):
        self.downloads.append(job_id)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        f = out / "glbx-mdp3.dbn.zst"
        f.write_bytes(f"raw-{job_id}".encode())
        return [f]


def workspace(tmp_path: Path) -> tuple[dict, Path]:
    for rel in (SPEC["data_acquisition"]["purchase_approval"]["exact_request_parameters"]["estimate_artifact"],
                SPEC["data_acquisition"]["purchase_approval"]["exact_request_parameters"]["symbol_manifest"]):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    return copy.deepcopy(SPEC), tmp_path


def run_preflight(spec, root, **kw):
    ledger = da.Ledger(root / da.LEDGER_PATH)
    return da.preflight(spec, root, metadata([], **kw), "0.87.0", FAKE_KEY, ledger)


# =========================================================================== approval and preflight


def test_the_approval_uses_the_existing_fields_and_cites_the_payload_hash():
    pa = SPEC["data_acquisition"]["purchase_approval"]
    assert set(pa) == {"approved", "approved_by", "approved_at_utc", "estimate_artifact_sha256", "exact_request_parameters",
                       "maximum_permitted_charge_usd"}
    assert pa["approved"] is True and pa["estimate_artifact_sha256"] == "0fcae769b903268d0e8f4e9c7e9219053b1b12b85746ddf8b8038da3c047d0a5"
    assert pa["maximum_permitted_charge_usd"] == "20.00"


def test_preflight_passes_for_the_exact_approved_requests(tmp_path):
    spec, root = workspace(tmp_path)
    report, approval, requests = run_preflight(spec, root)
    assert report.passed, report.problems
    assert [(r.schema, r.start_utc, r.end_utc) for r in requests] == list(approval.requests)
    assert report.fresh_combined_usd == "14.263967253268"


def test_a_request_differing_from_the_approval_is_refused(tmp_path):
    spec, root = workspace(tmp_path)
    changed = copy.deepcopy(spec)
    changed["data_acquisition"]["purchase_approval"]["exact_request_parameters"]["requests"][1]["end"] = "2026-09-27T00:00:00Z"
    report, _, _ = run_preflight(changed, root)
    assert not report.passed and "GENERATED_REQUESTS_DIFFER_FROM_APPROVAL" in report.problems[0]
    wrong_hash = copy.deepcopy(spec)
    wrong_hash["data_acquisition"]["purchase_approval"]["estimate_artifact_sha256"] = "0" * 64
    assert "ESTIMATE_PAYLOAD_HASH_NOT_APPROVED" in run_preflight(wrong_hash, root)[0].problems[0]
    (root / spec["data_acquisition"]["purchase_approval"]["exact_request_parameters"]["symbol_manifest"]).write_text("{}")
    assert not run_preflight(spec, root)[0].passed


def test_cost_above_the_maximum_is_refused_and_exactly_20_qualifies(tmp_path):
    spec, root = workspace(tmp_path)
    over, _, _ = run_preflight(spec, root, cost_a=0.01, cost_b=19.991)
    assert not over.passed and any(p.startswith("FRESH_COST_ABOVE_APPROVED_MAXIMUM") for p in over.problems)
    exact, _, _ = run_preflight(spec, root, cost_a=0.5, cost_b=19.5)
    assert exact.passed and Decimal(exact.fresh_combined_usd) == Decimal("20.0")
    unknown, _, _ = run_preflight(spec, root, cost_b=float("nan"))
    assert not unknown.passed and unknown.fresh_combined_usd is None  # never treated as zero


def test_unapproved_schemas_and_symbols_are_rejected(tmp_path):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    for bad in (de.EstimateRequest("C", "x", "ohlcv-1s", CONTRACTS, requests[1].start_utc, requests[1].end_utc),
                de.EstimateRequest("D", "x", "trades", CONTRACTS, requests[1].start_utc, requests[1].end_utc),
                de.EstimateRequest("B", "x", "ohlcv-1m", CONTRACTS[:-1], requests[1].start_utc, requests[1].end_utc)):
        with pytest.raises(da.PreflightFailed):
            da.assert_approved(bad, requests)
    with pytest.raises(ValueError):
        de.EstimateRequest("B", "x", "ohlcv-1m", ("MNQ.c.0",), requests[1].start_utc, requests[1].end_utc)


def test_missing_key_fails_the_preflight(tmp_path):
    spec, root = workspace(tmp_path)
    report, _, _ = da.preflight(spec, root, metadata([]), "0.87.0", None, da.Ledger(root / da.LEDGER_PATH))
    assert "DATABENTO_API_KEY_MISSING" in report.problems


# =========================================================================== idempotency


def acquire(root, batch, requests, ledger):
    return [da.acquire_one(r, requests, batch, ledger, root, "0.87.0", "1.0", poll_seconds=0, sleep=lambda s: None) for r in requests]


def test_a_repeated_command_cannot_repurchase_completed_data(tmp_path):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    ledger = da.Ledger(root / da.LEDGER_PATH)
    batch = FakeBatch(root)
    entries = acquire(root, batch, requests, ledger)
    for e in entries:
        e["status"] = "COMPLETED_VERIFIED"
        ledger.put(e["request_key"], e)
    acquire(root, batch, requests, da.Ledger(root / da.LEDGER_PATH))
    assert len(batch.submits) == 2 and len(batch.downloads) == 2  # the second run did nothing
    for e in entries:  # local files lost: re-download from the SAME job, never resubmit
        for f in e["raw_files"]:
            (root / f["path"]).unlink()
    acquire(root, batch, requests, da.Ledger(root / da.LEDGER_PATH))
    assert len(batch.submits) == 2 and len(batch.downloads) == 4


def test_an_uncertain_prior_submission_blocks_retry_pending_reconciliation(tmp_path):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    ledger = da.Ledger(root / da.LEDGER_PATH)
    with pytest.raises(ConnectionError):
        da.acquire_one(requests[0], requests, FakeBatch(root, fail_submit=True), ledger, root, "0.87.0", "1.0")
    assert ledger.entry(da.request_key(requests[0]))["status"] == "INTENT_RECORDED"  # durable intent, no job id
    retry = FakeBatch(root)  # Databento shows no matching job: must NOT resubmit
    with pytest.raises(da.ReconciliationRequired):
        da.acquire_one(requests[0], requests, retry, da.Ledger(root / da.LEDGER_PATH), root, "0.87.0", "1.0")
    assert retry.submits == []
    found = FakeBatch(root)  # Databento shows exactly one matching job: adopt it
    job = {"id": "GLBX-EXISTING", "state": "done", "cost_usd": 0.0094, "dataset": "GLBX.MDP3", "schema": requests[0].schema,
           "symbols": ",".join(requests[0].symbols), "start": requests[0].start_utc.replace("Z", ".000000000Z"),
           "end": requests[0].end_utc.replace("Z", ".000000000Z")}
    found.jobs.append(job)
    ledger = da.Ledger(root / da.LEDGER_PATH)
    ledger.entry(da.request_key(requests[0]))["status"] = "INTENT_RECORDED"
    entry = da.acquire_one(requests[0], requests, found, ledger, root, "0.87.0", "1.0", poll_seconds=0, sleep=lambda s: None)
    assert found.submits == [] and entry["job_id"] == "GLBX-EXISTING" and entry["reconciled"] is True


def test_the_api_key_is_never_logged_stored_or_hashed(tmp_path):
    spec, root = workspace(tmp_path)
    report, _, requests = run_preflight(spec, root)
    ledger = da.Ledger(root / da.LEDGER_PATH)
    acquire(root, FakeBatch(root), requests, ledger)
    text = (root / da.LEDGER_PATH).read_text() + json.dumps(report.__dict__, default=str)
    assert FAKE_KEY not in text
    import hashlib
    assert hashlib.sha256(FAKE_KEY.encode()).hexdigest() not in text


# =========================================================================== raw validation (synthetic fixed-point frames)

import datetime as dt  # noqa: E402

import pandas as pd  # noqa: E402

from mnq_research import raw_ingest as ri  # noqa: E402
from mnq_research import readiness  # noqa: E402

START, END = "2019-05-05T22:00:00Z", "2026-09-26T00:00:00Z"
FIRST, LAST = dt.date(2019, 5, 6), dt.date(2026, 9, 25)


def frame(rows):
    """rows: (ts, symbol, instrument_id, o, h, l, c, volume) with prices in points."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows], name="ts_event")
    fx = lambda x: int(round(x * ri.FIXED_SCALE))  # noqa: E731
    return pd.DataFrame({"rtype": ri.OHLCV_1M_RTYPE, "instrument_id": [r[2] for r in rows], "symbol": [r[1] for r in rows],
                         "open": [fx(r[3]) for r in rows], "high": [fx(r[4]) for r in rows], "low": [fx(r[5]) for r in rows],
                         "close": [fx(r[6]) for r in rows], "volume": [r[7] for r in rows]}, index=idx)


GOOD = [("2024-03-12T14:30:00Z", "MNQM4", 10, 18000.0, 18001.0, 17999.5, 18000.25, 5),
        ("2024-03-12T14:31:00Z", "MNQM4", 10, 18000.25, 18000.5, 17999.0, 17999.75, 7)]


def codes(v):
    return {i.code for i in v.issues}


def test_valid_bars_pass():
    v, clean = ri.validate_ohlcv(frame(GOOD), CONTRACTS, START, END, record_count=2)
    assert v.passed and len(clean) == 2


def test_off_tick_invalid_ohlc_and_negative_volume_fail():
    bad = [("2024-03-12T14:32:00Z", "MNQM4", 10, 18000.1, 18001.0, 17999.5, 18000.0, 1),  # off tick
           ("2024-03-12T14:33:00Z", "MNQM4", 10, 18000.0, 17999.0, 17999.5, 18000.0, 1),  # high < low
           ("2024-03-12T14:34:00Z", "MNQM4", 10, 18000.0, 18001.0, 17999.5, 18000.0, -3)]  # negative volume
    v, _ = ri.validate_ohlcv(frame(GOOD + bad), CONTRACTS, START, END)
    assert {"OFF_TICK_PRICE", "INVALID_OHLC", "NEGATIVE_VOLUME"} <= codes(v) and not v.passed


def test_out_of_range_unrelated_and_non_minute_records_fail():
    rows = GOOD + [("2026-09-26T00:00:00Z", "MNQZ6", 42005282, 20000.0, 20000.0, 20000.0, 20000.0, 1),  # end is exclusive
                   ("2019-05-05T21:59:00Z", "MNQM9", 8078, 7600.0, 7600.0, 7600.0, 7600.0, 1),
                   ("2024-03-12T14:35:00Z", "NQM4", 99, 18000.0, 18000.0, 18000.0, 18000.0, 1),
                   ("2024-03-12T14:36:30Z", "MNQM4", 10, 18000.0, 18000.0, 18000.0, 18000.0, 1)]
    v, _ = ri.validate_ohlcv(frame(rows), CONTRACTS, START, END)
    assert {"OUTSIDE_APPROVED_INTERVAL", "UNRELATED_INSTRUMENT", "NOT_MINUTE_START"} <= codes(v) and not v.passed


def test_duplicates_identical_are_removed_explicitly_and_conflicting_fail():
    v, clean = ri.validate_ohlcv(frame(GOOD + [GOOD[0]]), CONTRACTS, START, END)
    assert v.passed and len(clean) == 2 and "IDENTICAL_DUPLICATE_REMOVED" in codes(v)
    first = [i for i in v.issues if i.code == "IDENTICAL_DUPLICATE_REMOVED"][0]
    assert first.count == 1 and first.blocking is False  # counted, never silent
    v2, _ = ri.validate_ohlcv(frame(GOOD + [GOOD[1]] + [GOOD[1]]), CONTRACTS, START, END)
    assert [i.count for i in v2.issues if i.code == "IDENTICAL_DUPLICATE_REMOVED"] == [2]  # deterministic
    conflict = list(GOOD[0])
    conflict[6] = 18000.5
    v3, _ = ri.validate_ohlcv(frame(GOOD + [tuple(conflict)]), CONTRACTS, START, END)
    assert "CONFLICTING_DUPLICATE" in codes(v3) and not v3.passed


def test_partial_and_corrupt_downloads_fail(tmp_path):
    v, _ = ri.validate_ohlcv(frame(GOOD), CONTRACTS, START, END, record_count=5)
    assert "RECORD_COUNT_MISMATCH_PARTIAL_OR_EXTRA" in codes(v) and not v.passed
    garbage = tmp_path / "x.dbn.zst"
    garbage.write_bytes(b"\x28\xb5\x2f\xfd not really zstd")
    digest = da.sha256_file(garbage)
    with pytest.raises(Exception):
        ri.load_dbn(garbage, digest)


def test_raw_files_are_immutable_by_hash(tmp_path):
    raw = tmp_path / "raw.dbn.zst"
    raw.write_bytes(b"original bytes")
    digest = da.sha256_file(raw)
    ri.verify_raw(raw, digest)
    raw.write_bytes(b"original bytes, edited")
    with pytest.raises(ValueError, match="RAW_FILE_MODIFIED"):
        ri.verify_raw(raw, digest)


def test_missing_raw_files_prevent_readiness(tmp_path):
    spec = copy.deepcopy(SPEC)
    spec["research_pipeline"]["historical_data_ingestion_status"] = "VERIFIED_BY_MANIFEST: m.json"
    assert any("manifest missing" in p.detail for p in readiness.data_presence_problems(spec, tmp_path))
    (tmp_path / "m.json").write_text(json.dumps({"validation_status": "PASSED", "raw_inputs": [{"path": "raw.dbn.zst", "sha256": "0" * 64}],
                                                 "output_path": "out.parquet", "output_file_sha256": "0" * 64}))
    problems = readiness.data_presence_problems(spec, tmp_path)
    assert sum("DATA_FILE_ABSENT" in p.detail for p in problems) == 2
    (tmp_path / "raw.dbn.zst").write_bytes(b"x")
    assert any("DATA_FILE_HASH_MISMATCH" in p.detail for p in readiness.data_presence_problems(spec, tmp_path))
    assert not readiness.check_stage(spec, readiness.Stage.SIGNAL_REPLAY).is_ready


def test_missing_minutes_remain_unclassified_and_no_bars_are_fabricated():
    rows = [("2024-03-12T14:30:00Z", "MNQM4", 10, 18000.0, 18000.0, 18000.0, 18000.0, 1),
            ("2024-03-12T14:33:00Z", "MNQM4", 10, 18000.0, 18000.0, 18000.0, 18000.0, 1)]
    canon = ri.canonicalize(frame(rows), FIRST, LAST, "2026-09-30T00:00:00Z")
    assert len(canon) == 2  # the two absent minutes are not filled with zero-volume bars
    _, summary = ri.coverage(canon, ("MNQM4",), FIRST, LAST)
    missing = summary["MNQM4"]["missing_minutes"]
    assert set(missing) == {"UNCLASSIFIED_MISSING_PENDING_CALENDAR"} and missing["UNCLASSIFIED_MISSING_PENDING_CALENDAR"] > 0
    from mnq_research.data_quality import MinuteQuality, gap_minutes
    assert gap_minutes([MinuteQuality.UNCLASSIFIED_MISSING_PENDING_CALENDAR]) == 1  # counts as a gap, never as no-trade


def test_derived_outputs_reproduce_and_prices_stay_unadjusted():
    a = ri.canonicalize(frame(GOOD), FIRST, LAST, "2026-09-30T00:00:00Z")
    b = ri.canonicalize(frame(list(reversed(GOOD))), FIRST, LAST, "2026-09-30T00:00:00Z")
    assert ri.content_hash(a) == ri.content_hash(b)  # same raw content -> same canonical hash
    assert list(a["open"]) == [18000.0, 18000.25] and set(a["contract"]) == {"MNQM4"}  # raw contract prices, no adjustment
    assert not {c for c in a.columns if "adjust" in c or "continuous" in c or "back" in c}
    c = ri.canonicalize(frame([GOOD[0]]), FIRST, LAST, "2026-09-30T00:00:00Z")
    assert ri.content_hash(c) != ri.content_hash(a)
    assert bool(a["in_designated_window"].all())  # 2024-03-12 lies in MNQM4's designated window
    assert ri.core_window("MNQM4", FIRST, LAST) == (dt.date(2024, 3, 11), dt.date(2024, 6, 16))


def test_definitions_must_match_the_frozen_instrument_facts():
    def defs(tick=0.25, uom=2.0, currency="USD", symbols=CONTRACTS):
        return pd.DataFrame({"raw_symbol": list(symbols), "instrument_id": range(len(symbols)),
                             "min_price_increment": int(tick * ri.FIXED_SCALE), "unit_of_measure_qty": int(uom * ri.FIXED_SCALE),
                             "currency": currency})
    v, ids = ri.validate_definitions(defs(), CONTRACTS)
    assert v.passed and len(ids) == 31
    for bad, code in ((defs(tick=0.5), "TICK_SIZE_MISMATCH"), (defs(uom=20.0), "POINT_VALUE_MISMATCH"),
                      (defs(currency="EUR"), "CURRENCY_MISMATCH"), (defs(symbols=CONTRACTS[:-1]), "MISSING_DEFINITION"),
                      (defs(symbols=CONTRACTS[:-1] + ("NQZ6",)), "UNRELATED_INSTRUMENT")):
        v, _ = ri.validate_definitions(bad, CONTRACTS)
        assert code in codes(v) and not v.passed
