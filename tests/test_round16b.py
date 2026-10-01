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

    FILES = ("glbx-mdp3.dbn.zst",)

    @staticmethod
    def content(job_id, name):
        return f"raw-{job_id}-{name}".encode()

    def list_files(self, job_id):
        import hashlib
        return [{"filename": n, "size": len(self.content(job_id, n)), "hash": "sha256:" + hashlib.sha256(self.content(job_id, n)).hexdigest()}
                for n in self.FILES]

    def download(self, job_id, output_dir, filename_to_download=None):
        assert filename_to_download in self.FILES  # one listed file at a time, never the whole-job zip
        self.downloads.append(job_id)
        out = Path(output_dir) / job_id
        out.mkdir(parents=True, exist_ok=True)
        f = out / filename_to_download
        f.write_bytes(self.content(job_id, filename_to_download))
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


# =========================================================================== staged resume (definitions first, one OHLCV submission)


def _fake_definitions(monkeypatch, requests, *, tick=250_000_000, extra_symbol=None, rtype=19):
    import pandas as pd

    from mnq_research import raw_ingest as ri

    d = requests[0]
    syms = list(d.symbols) + ([extra_symbol] if extra_symbol else [])

    class Meta:
        dataset, schema, stype_in = d.dataset, d.schema, d.stype_in
        symbols = list(d.symbols)
        start, end = pd.Timestamp(d.start_utc).value, pd.Timestamp(d.end_utc).value
        not_found: list[str] = []

    df = pd.DataFrame({"raw_symbol": syms, "instrument_id": range(1, len(syms) + 1), "min_price_increment": tick,
                       "currency": "USD", "unit_of_measure_qty": 2_000_000_000, "rtype": rtype})

    def load(path, sha):
        ri.verify_raw(path, sha)
        return Meta(), df

    monkeypatch.setattr(ri, "load_dbn", load)


def submitted_definitions(root, requests, batch):
    ledger = da.Ledger(root / da.LEDGER_PATH)
    entry = da.submit_once(requests[0], requests, batch, ledger, "0.87.0", "0.0094", "a" * 64)
    return ledger, entry


def staged(spec, root, batch, ledger, **kw):
    return da.run_staged(spec, root, batch, metadata([]), "0.87.0", FAKE_KEY, ledger, poll_seconds=0, sleep=lambda s: None, **kw)


def test_definitions_are_downloaded_validated_then_one_ohlcv_job_is_submitted_and_the_run_stops(tmp_path, monkeypatch):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    batch = FakeBatch(root)
    ledger, _ = submitted_definitions(root, requests, batch)
    _fake_definitions(monkeypatch, requests)
    result = staged(spec, root, batch, ledger)
    d = ledger.entry(da.request_key(requests[0]))
    assert d["status"] == da.DOWNLOADED_AND_VALIDATED and d["definitions_validation"]["passed"]
    assert d["available_files"] and d["raw_files"][0]["sha256"] == d["available_files"][0]["hash"].split(":")[1]
    assert result["outcome"] == "OHLCV_SUBMITTED_CHECKPOINT" and len(batch.submits) == 2
    assert [s["schema"] for s in batch.submits] == ["definition", "ohlcv-1m"]
    o = ledger.entry(da.request_key(requests[1]))
    assert o["status"] == "SUBMITTED" and o["approval_payload_sha256"] == spec["data_acquisition"]["purchase_approval"]["estimate_artifact_sha256"]
    assert batch.downloads == ["GLBX-JOB-1"]  # the checkpoint run never polled or downloaded the OHLCV job
    again = staged(spec, root, batch, da.Ledger(root / da.LEDGER_PATH))  # rerun polls/downloads, never resubmits
    assert len(batch.submits) == 2 and again["outcome"] == "OHLCV_DOWNLOADED_UNVALIDATED"


def test_ohlcv_is_never_submitted_unless_definitions_are_downloaded_and_validated(tmp_path, monkeypatch):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    for i, bad in enumerate(({"tick": 500_000_000}, {"extra_symbol": "MNQZ7"}, {"rtype": 20})):
        sub = workspace(tmp_path / f"case{i}")[1]
        batch = FakeBatch(sub)
        ledger, _ = submitted_definitions(sub, requests, batch)
        _fake_definitions(monkeypatch, requests, **bad)
        result = staged(spec, sub, batch, ledger)
        assert result["outcome"] == "DEFINITIONS_VALIDATION_FAILED", bad
        assert [s["schema"] for s in batch.submits] == ["definition"]


def test_done_without_files_and_hash_mismatch_never_submit_anything(tmp_path, monkeypatch):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    empty = FakeBatch(root)
    empty.list_files = lambda job_id: []
    ledger, _ = submitted_definitions(root, requests, empty)
    assert staged(spec, root, empty, ledger)["outcome"] == "DEFINITIONS_DONE_WITHOUT_AVAILABLE_FILES"
    assert len(empty.submits) == 1 and empty.downloads == []

    root2 = workspace(tmp_path / "b")[1]
    corrupt = FakeBatch(root2)
    real = corrupt.list_files
    corrupt.list_files = lambda job_id: [{**f, "hash": "sha256:" + "0" * 64} for f in real(job_id)]
    ledger2, _ = submitted_definitions(root2, requests, corrupt)
    assert staged(spec, root2, corrupt, ledger2)["outcome"] == "DEFINITIONS_DOWNLOAD_FAILED"
    assert len(corrupt.submits) == 1


def test_pending_jobs_are_polled_within_a_bound_and_never_cancelled_or_resubmitted(tmp_path, monkeypatch):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    batch = FakeBatch(root)
    ledger, entry = submitted_definitions(root, requests, batch)
    batch.jobs[0] = {"id": entry["job_id"], "state": "processing", "ts_received": "x"}  # Databento's sparse list_jobs fields
    sleeps = []
    result = da.run_staged(spec, root, batch, metadata([]), "0.87.0", FAKE_KEY, ledger, poll_timeout=90, poll_seconds=30, sleep=sleeps.append)
    assert result["outcome"] == "DEFINITIONS_SUBMITTED" and sleeps == [30, 30, 30]
    e = ledger.entry(da.request_key(requests[0]))
    assert e["job_id"] == entry["job_id"] and e["last_status_check"]["databento_state"] == "processing"
    assert len(batch.submits) == 1 and batch.downloads == [] and not hasattr(batch, "cancels")
    batch.jobs[0] = {"id": entry["job_id"], "state": "expired"}
    assert staged(spec, root, batch, ledger)["outcome"] == "DEFINITIONS_STATE_UNKNOWN" and len(batch.submits) == 1


def test_ohlcv_gate_blocks_unrecorded_jobs_existing_entries_and_over_cap_quotes(tmp_path, monkeypatch):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    batch = FakeBatch(root)
    ledger, _ = submitted_definitions(root, requests, batch)
    _fake_definitions(monkeypatch, requests)
    batch.jobs.append({"id": "GLBX-UNKNOWN", "state": "queued"})  # could be an OHLCV job submitted elsewhere
    result = staged(spec, root, batch, ledger)
    assert result["outcome"] == "OHLCV_SUBMISSION_BLOCKED" and "UNRECORDED_DATABENTO_JOBS_EXIST" in result["problems"][0]
    assert len(batch.submits) == 1
    batch.jobs.pop()
    over = da.run_staged(spec, root, batch, metadata([], cost_b=19.995), "0.87.0", FAKE_KEY, ledger, poll_seconds=0, sleep=lambda s: None)
    assert over["outcome"] == "PURCHASE_PREFLIGHT_FAILED" and len(batch.submits) == 1
    with pytest.raises(da.PreflightFailed):  # a second submission of any request is refused outright
        da.submit_once(requests[0], requests, batch, ledger, "0.87.0", "0", "a" * 64)
    assert len(batch.submits) == 1


def test_an_existing_raw_file_is_never_overwritten(tmp_path):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    batch = FakeBatch(root)
    ledger, entry = submitted_definitions(root, requests, batch)
    target = root / da.RAW_ROOT / "definition" / entry["job_id"] / FakeBatch.FILES[0]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"something else")
    entry = da.download_job_files(ledger.entry(entry["request_key"]), batch, ledger, root)
    assert entry["status"] == "DOWNLOAD_FAILED" and target.read_bytes() == b"something else" and batch.downloads == []
    assert entry["download_problems"] == [f"REFUSING_TO_OVERWRITE_EXISTING_RAW_FILE:{FakeBatch.FILES[0]}"]


def test_the_ohlcv_gate_itself_requires_validated_definitions_with_intact_files(tmp_path, monkeypatch):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    batch = FakeBatch(root)
    ledger, entry = submitted_definitions(root, requests, batch)
    assert any(p.startswith("DEFINITIONS_NOT_DOWNLOADED_AND_VALIDATED") for p in da.ohlcv_submission_problems(ledger, requests, batch, root))
    _fake_definitions(monkeypatch, requests)
    staged(spec, root, batch, ledger)
    ledger.data["entries"].pop(da.request_key(requests[1]))  # pretend the OHLCV job had not been submitted
    batch.jobs = batch.jobs[:1]
    assert da.ohlcv_submission_problems(ledger, requests, batch, root) == []
    (root / ledger.entry(entry["request_key"])["raw_files"][0]["path"]).write_bytes(b"altered")
    assert any(p.startswith("DEFINITIONS_NOT_DOWNLOADED_AND_VALIDATED") for p in da.ohlcv_submission_problems(ledger, requests, batch, root))


def test_an_existing_raw_file_identical_to_the_listing_is_kept_without_redownloading(tmp_path):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    batch = FakeBatch(root)
    ledger, entry = submitted_definitions(root, requests, batch)
    entry = da.download_job_files(ledger.entry(entry["request_key"]), batch, ledger, root)
    entry = da.download_job_files(entry, batch, ledger, root)  # e.g. a rerun after an interrupted ledger write
    assert entry["status"] == "DOWNLOADED_UNVALIDATED" and batch.downloads == [entry["job_id"]]


def test_a_retry_downloads_nothing_if_the_listing_differs_from_the_ledger_record(tmp_path):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    batch = FakeBatch(root)
    ledger, entry = submitted_definitions(root, requests, batch)
    recorded = [{**f, "hash": "sha256:" + "1" * 64} for f in batch.list_files(entry["job_id"])]
    entry = ledger.entry(entry["request_key"])
    entry.update(status="DOWNLOAD_FAILED", available_files=recorded)  # e.g. the earlier proxy-refused attempt
    entry = da.download_job_files(entry, batch, ledger, root)
    assert entry["status"] == "DOWNLOAD_FAILED" and entry["download_problems"] == ["LISTING_DIFFERS_FROM_LEDGER_RECORD"]
    assert batch.downloads == [] and entry["available_files"] == recorded  # the ledger record is never replaced
    entry["available_files"] = batch.list_files(entry["job_id"])  # identical record: the retry downloads and verifies
    assert da.download_job_files(entry, batch, ledger, root)["status"] == "DOWNLOADED_UNVALIDATED"


def test_the_ohlcv_gate_blocks_an_existing_local_ohlcv_artifact(tmp_path, monkeypatch):
    spec, root = workspace(tmp_path)
    _, _, requests = run_preflight(spec, root)
    batch = FakeBatch(root)
    ledger, _ = submitted_definitions(root, requests, batch)
    _fake_definitions(monkeypatch, requests)
    stray = root / da.RAW_ROOT / "ohlcv_1m" / "GLBX-OTHER" / "x.dbn.zst"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    result = staged(spec, root, batch, ledger)
    assert result["outcome"] == "OHLCV_SUBMISSION_BLOCKED" and any("OHLCV_LOCAL_ARTIFACT_ALREADY_EXISTS" in p for p in result["problems"])
    assert [s["schema"] for s in batch.submits] == ["definition"]
