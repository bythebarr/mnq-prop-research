"""Round 16A: Databento cost estimation (mocked), source archiving, calendar plan, D-033 amendments.

No test touches the network. Fake clients raise if anything other than the
allowed metadata calls is used.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import hashlib
import inspect
import json
from fractions import Fraction
from pathlib import Path

import pytest

from test_entry_order import SPEC, D, invalid_paths
from test_protection import FILL, MS
from test_round15 import COSTS, gap_trade, long_trade, obs
from mnq_research import data_estimate as de
from mnq_research.accounting import account_all_scenarios, account_trade
from mnq_research.calendar_sources import CalendarSource, SourceStatus, load_plan
from mnq_research.confirmation import Side
from mnq_research.costs import OrderPurpose
from mnq_research.data_quality import IntervalState, MinuteQuality as Q, decision_interval_state, gap_minutes
from mnq_research.protection import PriceInterval, PriceSource
from mnq_research.source_archive import TRADEIFY_COMMISSION_URL, FetchedPage, archive_commission_source

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "db-FAKEKEYFORTESTS0123456789"
FIRST, LAST = dt.date(2019, 5, 6), dt.date(2026, 9, 25)
CONTRACTS = de.designated_contracts(FIRST, LAST)


# =========================================================================== fakes


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError(f"forbidden non-metadata access: {name}")


class FakeMetadata:
    def __init__(self, calls, cost=1.25, size=1000, count=10, error=None):
        self.calls, self.cost, self.size, self.count, self.error = calls, cost, size, count, error

    def _record(self, name, kw):
        self.calls.append((name, kw))
        if self.error:
            raise self.error

    def get_cost(self, **kw):
        self._record("get_cost", kw)
        return self.cost

    def get_billable_size(self, **kw):
        self._record("get_billable_size", kw)
        return self.size

    def get_record_count(self, **kw):
        self._record("get_record_count", kw)
        return self.count


class FakeSymbology:
    def __init__(self, calls, ambiguous=()):
        self.calls, self.ambiguous = calls, ambiguous

    def resolve(self, **kw):
        self.calls.append(("resolve", kw))
        result = {s: [{"d0": "2019-05-05", "d1": "2026-09-26", "s": str(1000 + i)}] for i, s in enumerate(kw["symbols"])}
        for s in self.ambiguous:
            result[s] = [{"s": "1"}, {"s": "2"}]
        return {"result": result, "partial": [], "not_found": []}


class FakeHistorical:
    def __init__(self, calls, **kw):
        ambiguous = kw.pop("ambiguous", ())
        self.metadata = FakeMetadata(calls, **kw)
        self.symbology = FakeSymbology(calls, ambiguous)
        self.timeseries = Forbidden()
        self.batch = Forbidden()
        self.live = Forbidden()


def run(request=None, **kw):
    calls = []
    client = de.MetadataOnlyClient(FakeHistorical(calls, **kw))
    result = de.estimate(request or de.alternatives(FIRST, LAST)[1], client, "0.87.0", FAKE_KEY)
    return result, calls


# =========================================================================== estimator safety


def test_no_estimate_operation_downloads_data():
    for request in de.alternatives(FIRST, LAST)[:4]:
        result, calls = run(request)
        assert result.status is de.EstimateStatus.KNOWN
        assert {name for name, _ in calls} == {"resolve", "get_cost", "get_billable_size", "get_record_count"}
    tree = ast.parse(inspect.getsource(de))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not used & {"timeseries", "batch", "live", "Live", "get_range", "submit_job", "download", "stream"}
    wrapper = de.MetadataOnlyClient(FakeHistorical([]))
    assert not any(hasattr(wrapper, n) for n in ("timeseries", "batch", "live"))


def test_no_api_key_is_logged_or_stored(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(de.API_KEY_ENV, raising=False)
    with pytest.raises(de.MissingCredentialError) as exc:
        de.load_api_key()
    assert "db-" not in str(exc.value)
    result, _ = run(error=RuntimeError(f"401 for key {FAKE_KEY} header Authorization: Basic {FAKE_KEY}"))
    assert FAKE_KEY not in json.dumps(de.asdict(result), default=str)
    assert any("[REDACTED]" in w for w in result.warnings)
    path = de.write_artifact([result], tmp_path, FAKE_KEY)
    assert FAKE_KEY not in path.read_text()
    leaked = copy.deepcopy(result)
    leaked.warnings.append(FAKE_KEY)
    with pytest.raises(RuntimeError):
        de.write_artifact([leaked], tmp_path, FAKE_KEY)
    import re
    import subprocess

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    tracked += [str(p.relative_to(ROOT)) for p in (ROOT / "outputs" / "estimates").glob("*.json")]
    leaks = [f for f in tracked if not f.startswith("tests/") and (ROOT / f).is_file()
             and re.search(r"db-[A-Za-z0-9]{20,}|DATABENTO_API_KEY\s*[=:]\s*['\"]?[A-Za-z0-9_-]{8,}", (ROOT / f).read_text(errors="ignore"))]
    assert leaks == []


def test_unknown_cost_can_never_be_zero():
    failed, _ = run(error=ConnectionError("proxy 403"))
    assert failed.status is de.EstimateStatus.UNKNOWN and failed.estimated_charge_usd is None
    for bad in (None, float("nan"), float("inf"), -1.0, "12.00", True):
        result, _ = run(cost=bad)
        assert result.status is de.EstimateStatus.UNKNOWN and result.estimated_charge_usd is None, bad
    zero, _ = run(cost=0.0)
    assert zero.status is de.EstimateStatus.KNOWN and zero.estimated_charge_usd == "0.0"  # a real zero stays distinct


def test_estimates_retain_their_complete_request_parameters(tmp_path):
    result, calls = run()
    for key in ("alternative", "dataset", "schema", "symbols", "stype_in", "start_utc", "end_utc", "mode"):
        assert key in result.request
    assert result.request["dataset"] == "GLBX.MDP3" and result.request["mode"] == "historical"
    assert result.client_version == "0.87.0" and result.retrieved_utc and result.currency == "USD"
    assert result.compressed_size_bytes == de.NOT_EXPOSED and result.applicable_credits_usd is None
    assert "get_cost" in json.dumps(result.raw_responses)
    cost_call = next(kw for name, kw in calls if name == "get_cost")
    assert cost_call["symbols"] == list(CONTRACTS) and cost_call["stype_in"] == "raw_symbol"
    path = de.write_artifact([result], tmp_path, FAKE_KEY)
    stored = json.loads(path.read_text())["payload"]["results"][0]["request"]
    assert tuple(stored["symbols"]) == CONTRACTS


def test_individual_contracts_only_and_no_expansion_to_other_instruments():
    assert CONTRACTS[0] == "MNQM9" and CONTRACTS[-1] == "MNQZ6" and len(CONTRACTS) == 31
    assert de.roll_date(2026, 9) == dt.date(2026, 9, 14)  # matches the frozen roll-rule example
    base = de.alternatives(FIRST, LAST)[1]
    for bad_symbols in (("MNQ.FUT",), ("MNQ.c.0",), ("NQZ6",), ("ES",), ("MNQ*",), ("MNQZ6", "MESZ6")):
        with pytest.raises(ValueError):
            de.EstimateRequest("X", "x", "ohlcv-1m", bad_symbols, base.start_utc, base.end_utc)
    for field, value in (("stype_in", "parent"), ("stype_in", "continuous"), ("dataset", "XNAS.ITCH")):
        with pytest.raises(ValueError):
            de.EstimateRequest("X", "x", "ohlcv-1m", CONTRACTS, base.start_utc, base.end_utc, **{field: value})
    with pytest.raises(ValueError):
        de.EstimateRequest("X", "x", "mbo", CONTRACTS, base.start_utc, base.end_utc)
    assert all(r.stype_in == "raw_symbol" and r.symbols == CONTRACTS for r in de.alternatives(FIRST, LAST))


def test_contract_overlap_is_not_double_counted():
    with pytest.raises(ValueError):
        de.EstimateRequest("X", "x", "ohlcv-1m", ("MNQZ6", "MNQZ6"), "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    with pytest.raises(ValueError):
        de.EstimateRequest("X", "x", "ohlcv-1s", CONTRACTS, "a", "z",
                           (("2026-01-02T14:30:00Z", "2026-01-02T17:05:00Z"), ("2026-01-02T17:00:00Z", "2026-01-02T18:00:00Z")))
    e1 = de.alternatives(FIRST, LAST)[4]
    assert len(e1.windows) == 1930 and all(a < b for a, b in e1.windows)
    assert all(e1.windows[i][1] <= e1.windows[i + 1][0] for i in range(len(e1.windows) - 1))
    short = de.EstimateRequest("E1", "x", "ohlcv-1s", CONTRACTS, e1.start_utc, e1.end_utc, e1.windows[:3])
    result, calls = run(short, cost=0.5, size=100, count=7)
    assert result.estimated_charge_usd == "1.5" and result.billable_size_uncompressed_bytes == 300  # disjoint windows summed once
    assert "NONE" in result.double_count_risk
    ambiguous, _ = run(ambiguous=("MNQZ9",))
    assert ambiguous.status is de.EstimateStatus.UNKNOWN and "AMBIGUOUS_INSTRUMENT_ID:MNQZ9" in ambiguous.warnings


def test_session_bounds_and_rth_windows_follow_dst():
    assert de.session_bounds(FIRST, LAST) == ("2019-05-05T22:00:00Z", "2026-09-26T00:00:00Z")
    windows = dict(de.rth_windows(dt.date(2024, 1, 2), dt.date(2024, 7, 2)))
    assert "2024-01-02T14:30:00Z" in windows and "2024-07-02T13:30:00Z" in windows


def test_all_estimate_artifacts_are_hashed(tmp_path):
    result, _ = run()
    path = de.write_artifact([result], tmp_path, FAKE_KEY)
    assert de.verify_artifact(path)
    data = json.loads(path.read_text())
    assert path.name.startswith("databento_estimate_" + data["sha256_of_payload"][:16])
    data["payload"]["results"][0]["estimated_charge_usd"] = "0.01"
    path.write_text(json.dumps(data))
    assert not de.verify_artifact(path)
    committed = sorted((ROOT / "outputs" / "estimates").glob("databento_estimate_*.json"))
    assert committed and all(de.verify_artifact(p) for p in committed)
    for p in committed:
        for r in json.loads(p.read_text())["payload"]["results"]:
            assert (r["status"] == "UNKNOWN") == (r["estimated_charge_usd"] is None)  # UNKNOWN never carries a number


# =========================================================================== D-033 amendments


def test_verified_no_trade_minutes_remain_distinct_from_gaps():
    assert gap_minutes([Q.VERIFIED_NO_TRADE_MINUTE] * 5) == 0
    assert gap_minutes([Q.KNOWN_DATA_OUTAGE, Q.UNEXPLAINED_MISSING_MINUTE, Q.REJECTED_BAD_DATA, "?"]) == 4
    assert decision_interval_state([Q.VERIFIED_NO_TRADE_MINUTE] * 5) is IntervalState.ELIGIBLE
    assert Q.VERIFIED_NO_TRADE_MINUTE not in (Q.KNOWN_DATA_OUTAGE, Q.UNEXPLAINED_MISSING_MINUTE)
    assert SPEC["missing_data"]["max_tolerated_gap_minutes"] == 0


def bar(ms: int, high: str, low: str) -> PriceInterval:
    return PriceInterval(FILL + ms * MS, D(high), D(low), PriceSource.AUTHORITATIVE_TRADES, f"bar@{ms}", FILL + (ms + 60000) * MS)


def test_entry_minute_mae_is_conservative_and_mfe_primary_excludes_it_long():
    entry_minute = bar(-30000, "20030.00", "20000.00")  # the minute containing the fill, both extremes unknown in order
    later = obs(40000, "20015.00")
    rec = account_trade(long_trade(observations=(entry_minute, later)), COSTS, "BASE_SLIPPAGE", "BASE")
    assert Fraction(rec["mae_points"]) == Fraction(D("20010.75")) - 20000  # full adverse extreme included
    assert Fraction(rec["mfe_points"]) == Fraction(D("20015.00")) - Fraction(D("20010.75"))  # entry-minute high excluded
    assert Fraction(rec["mfe_optimistic_bound_points"]) == Fraction(D("20030.00")) - Fraction(D("20010.75"))
    assert "ENTRY_MINUTE_EXCURSION_APPROXIMATION" in rec["data_quality_flags"]
    assert rec["mae_entry_minute_policy"] == "INCLUDE_FULL_MINUTE_CONSERVATIVE"
    assert rec["mfe_entry_minute_policy"] == "EXCLUDE_FULL_MINUTE_PRIMARY"


def test_entry_minute_policy_short_side():
    entry_minute = bar(-30000, "20000.00", "19980.00")  # adverse for a short is the HIGH
    later = obs(40000, "19990.00")
    short = long_trade(entries=((1, "19993.50"),), exits=((1, "19976.00", OrderPurpose.TARGET_LIMIT),), side=Side.SHORT,
                       stop="20004.25", target="19976.00", observations=(entry_minute, later))
    rec = account_trade(short, COSTS, "BASE_SLIPPAGE", "BASE")
    assert Fraction(rec["mae_points"]) == 20000 - Fraction(D("19993.25"))
    assert Fraction(rec["mfe_points"]) == Fraction(D("19993.25")) - 19990
    assert Fraction(rec["mfe_optimistic_bound_points"]) == Fraction(D("19993.25")) - 19980


def test_authoritative_finer_data_replace_the_entry_minute_approximation():
    entry_minute = bar(-30000, "20030.00", "20000.00")
    finer = (obs(1000, "20012.00"), obs(2000, "20009.00"), obs(20000, "20020.00"))  # trades after the fill
    rec = account_trade(long_trade(observations=(entry_minute,) + finer), COSTS, "BASE_SLIPPAGE", "BASE")
    assert "ENTRY_MINUTE_EXCURSION_APPROXIMATION" not in rec["data_quality_flags"]
    assert Fraction(rec["mae_points"]) == Fraction(D("20010.75")) - Fraction(D("20009.00"))
    assert Fraction(rec["mfe_points"]) == Fraction(D("20020.00")) - Fraction(D("20010.75"))
    assert rec["mfe_optimistic_bound_points"] is None


def test_base_and_stressed_risk_records_remain_separate():
    inputs = long_trade()
    base_alone = account_trade(inputs, COSTS, "BASE_SLIPPAGE", "BASE")
    records = account_all_scenarios(inputs, COSTS)
    base = records[("BASE_SLIPPAGE", "BASE")]
    assert base.values == base_alone.values and base["result_basis"] == "BASE_ASSUMPTIONS"
    stress = records[("BASE_SLIPPAGE", "COMMISSION_STRESS_1_50X")]
    assert stress["result_basis"] == "HYPOTHETICAL_STRESS"
    assert Fraction(stress["actual_initial_risk_usd"]) - Fraction(base["actual_initial_risk_usd"]) == Fraction(D("0.91"))  # 1.82 x 0.5
    assert sum(r["result_basis"] == "BASE_ASSUMPTIONS" for r in records.values()) == 1
    assert set(SPEC["commissions"]["stress_multipliers"]) == {"BASE", "COMMISSION_STRESS_1_25X", "COMMISSION_STRESS_1_50X"}


def test_gap_trade_primary_and_stress_costs():
    rec = account_trade(gap_trade(), COSTS, "BASE_SLIPPAGE", "BASE")
    assert rec["commissions_usd"] == "91/100"  # only the entry side is known to have occurred
    assert Fraction(rec["gap_stress_exit_price"]) == Fraction(D("19999.25"))  # stop 19999.75 less 2 adverse ticks
    assert Fraction(rec["gap_stress_commissions_usd"]) == Fraction(D("1.82"))


def test_retired_setup_fields_cannot_return():
    spec = copy.deepcopy(SPEC)
    spec["setup"]["preconditions"] = "anything"
    assert "setup.preconditions" in invalid_paths(spec)
    assert not {"definition", "preconditions", "setup_expiry"} & set(SPEC["setup"])


# =========================================================================== calendars and commission archive


def test_calendar_sources_cannot_be_ready_without_raw_and_parsed_hashes(tmp_path):
    plan = load_plan(ROOT / "configs" / "calendar_sources.yaml")
    assert plan and all(s.status is SourceStatus.PLANNED for s in plan)
    ids = {s.source_id for s in plan}
    assert {"CME_EQUITY_INDEX_HOLIDAY_AND_EARLY_CLOSE", "FOMC_MEETINGS_STATEMENTS_PRESS_CONFERENCES", "FED_CHAIR_TESTIMONY",
            "BLS_EMPLOYMENT_SITUATION", "BLS_CPI", "SESSION_AND_ROLL_CALENDAR"} <= ids
    source = plan[0]
    with pytest.raises(ValueError):
        source.mark_ready()
    raw, parsed = tmp_path / "raw.html", tmp_path / "parsed.json"
    raw.write_bytes(b"<html>calendar</html>")
    parsed.write_text("{}")
    h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    complete = CalendarSource(source.source_id, source.covers, source.responsible_body, source.official_url, True,
                              source.timezone_treatment, SourceStatus.PLANNED, "2026-09-29T00:00:00Z", "2019-05-06", "2026-09-25",
                              str(raw), h(raw), "parser-1.0", str(parsed), h(parsed))
    assert complete.mark_ready().status is SourceStatus.READY
    from dataclasses import replace
    for broken in (replace(complete, parsed_sha256="0" * 64), replace(complete, raw_sha256=None),
                   replace(complete, url_verified=False), replace(complete, recurring_rule_substitute=True)):
        with pytest.raises(ValueError):
            broken.mark_ready()


PAGE = b"""<html><head><title>Trading Commission Fees | Tradeify Help Center</title>
<script type="application/ld+json">{"dateModified": "2026-08-01T12:00:00Z"}</script></head><body>
<p>Micro E-Mini NASDAQ (MNQ) - Total Round Trip Cost: $1.82 per contract.</p></body></html>"""


def test_commission_archive_saves_raw_bytes_hash_and_calculation(tmp_path):
    m = archive_commission_source("1.82", "0.91", tmp_path, lambda url: FetchedPage(url, 200, PAGE, "text/html"),
                                  now=lambda: dt.datetime(2026, 9, 29, tzinfo=dt.timezone.utc))
    assert m.status == "MATCHES_CONFIGURATION" and m.url == TRADEIFY_COMMISSION_URL
    assert m.raw_sha256 == hashlib.sha256(PAGE).hexdigest() and Path(m.raw_path).read_bytes() == PAGE
    assert m.page_title.startswith("Trading Commission Fees") and m.effective_or_publication_date.startswith("2026-08-01")
    assert m.found_round_trip_usd == "1.82" and "1.82 / 2 = 0.91" in m.calculation
    assert (tmp_path / f"{m.raw_sha256}.manifest.json").is_file()


def test_a_changed_or_unreachable_source_never_updates_the_configuration(tmp_path):
    changed = PAGE.replace(b"$1.82", b"$2.10")
    m = archive_commission_source("1.82", "0.91", tmp_path, lambda url: FetchedPage(url, 200, changed, "text/html"))
    assert m.status == "MISMATCH_REQUIRES_NEW_DECISION" and m.found_round_trip_usd == "2.10"
    assert SPEC["commissions"]["round_turn_per_contract_usd"] == "1.82"

    def blocked(url):
        raise OSError("Tunnel connection failed: 403")

    failed = archive_commission_source("1.82", "0.91", tmp_path / "x", blocked)
    assert failed.status == "FETCH_FAILED" and failed.raw_sha256 == "" and not (tmp_path / "x").exists()
    assert "commissions.source_archive_status" in {p.path for p in __import__("mnq_research.validation", fromlist=["x"]).check_rule_freeze(SPEC).problems}


def test_redaction_removes_the_exact_key_even_in_an_unexpected_format():
    odd_key = "XYZ_unusual_format_secret_42"
    assert odd_key not in de.redact(f"error with {odd_key}", odd_key)
    calls = []
    client = de.MetadataOnlyClient(FakeHistorical(calls, error=RuntimeError(f"denied: {odd_key}")))
    result = de.estimate(de.alternatives(FIRST, LAST)[1], client, "0.87.0", odd_key)
    assert odd_key not in json.dumps(de.asdict(result), default=str)


def test_partial_resolution_is_accepted_only_when_the_designated_window_is_covered():
    class Partial(FakeSymbology):
        def __init__(self, calls, cover=True):
            super().__init__(calls)
            self.cover = cover

        def resolve(self, **kw):
            self.calls.append(("resolve", kw))
            result = {}
            for i, sym in enumerate(kw["symbols"]):
                start, end = de.designated_window(sym, FIRST, LAST)
                d0 = start - dt.timedelta(days=30)
                d1 = end + dt.timedelta(days=20) if (self.cover or sym != "MNQZ2") else end - dt.timedelta(days=5)
                result[sym] = [{"d0": d0.isoformat(), "d1": d1.isoformat(), "s": str(100 + i)}]
            return {"result": result, "partial": list(kw["symbols"]), "not_found": []}

    for cover, status in ((True, de.EstimateStatus.KNOWN), (False, de.EstimateStatus.UNKNOWN)):
        calls = []
        historical = FakeHistorical(calls)
        historical.symbology = Partial(calls, cover)
        result = de.estimate(de.alternatives(FIRST, LAST)[1], de.MetadataOnlyClient(historical), "0.87.0", FAKE_KEY)
        assert result.status is status
        if not cover:
            assert result.unresolved_symbols == ["MNQZ2"] and result.estimated_charge_usd is None
        else:
            assert result.metadata_call_count == 4  # resolve + cost + size + count
    assert de.designated_window("MNQZ6", FIRST, LAST) == (dt.date(2026, 9, 7), dt.date(2026, 9, 25))
    assert de.designated_window("MNQH0", FIRST, LAST) == (dt.date(2019, 12, 9), dt.date(2020, 3, 16))


def test_captured_federal_reserve_sources_are_hash_verified_and_remain_planned():
    plan = {s.source_id: s for s in load_plan(ROOT / "configs" / "calendar_sources.yaml")}
    for sid in ("FOMC_MEETINGS_STATEMENTS_PRESS_CONFERENCES", "FED_CHAIR_TESTIMONY"):
        source = plan[sid]
        assert source.access_status == "RAW_CAPTURED_NOT_PARSED" and source.status is SourceStatus.PLANNED
        assert source.raw_captures
        for url, digest, path, retrieved in source.raw_captures:
            assert url.startswith("https://www.federalreserve.gov/")
            assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
            manifest = json.loads((ROOT / path).with_suffix(".manifest.json").read_text())
            assert manifest["raw_sha256"] == digest and manifest["parsed"] is False and manifest["retrieved_utc"] == retrieved
        with pytest.raises(ValueError):
            source.mark_ready()  # raw capture alone is never READY


def test_blocked_sources_keep_their_truthful_statuses():
    plan = {s.source_id: s for s in load_plan(ROOT / "configs" / "calendar_sources.yaml")}
    assert plan["CME_EQUITY_INDEX_HOLIDAY_AND_EARLY_CLOSE"].access_status == "ACCESS_BLOCKED_TIMEOUT"
    assert plan["SESSION_AND_ROLL_CALENDAR"].access_status == "ACCESS_BLOCKED_TIMEOUT"
    for sid in ("BLS_EMPLOYMENT_SITUATION", "BLS_CPI", "BLS_PPI", "BLS_JOLTS", "BLS_RELEASE_SCHEDULES_BY_YEAR"):
        assert plan[sid].access_status == "ACCESS_BLOCKED_AKAMAI_403" and not plan[sid].raw_captures
    assert SPEC["commissions"]["source_access_status"] == "ACCESS_BLOCKED_CLOUDFLARE_403"
    assert SPEC["commissions"]["source_archive_status"] == "REQUIRED_BEFORE_EXECUTABLE"


def test_raw_capture_stores_exact_bytes_and_fails_closed(tmp_path):
    from mnq_research.source_archive import capture_raw_source

    body = b"<html><title>The Fed - Meeting calendars</title>2019</html>"
    m = capture_raw_source("https://example.gov/x", tmp_path, lambda u: FetchedPage(u, 200, body, "text/html"))
    assert m["status"] == "CAPTURED" and Path(m["raw_path"]).read_bytes() == body
    assert m["raw_sha256"] == hashlib.sha256(body).hexdigest() and m["parsed"] is False
    assert capture_raw_source("https://example.gov/y", tmp_path / "z", lambda u: FetchedPage(u, 403, b"", ""))["status"] == "FETCH_FAILED"
    assert not (tmp_path / "z").exists()


def test_the_captured_estimate_matches_the_specification_record():
    da = SPEC["data_acquisition"]
    path = ROOT / da["estimate_artifact"]
    assert de.verify_artifact(path)
    results = {r["request"]["alternative"]: r for r in json.loads(path.read_text())["payload"]["results"]}
    assert set(results) == {"A", "B"}
    assert results["A"]["estimated_charge_usd"] == da["estimated_charge_usd_a_definition"]
    assert results["B"]["estimated_charge_usd"] == da["estimated_charge_usd_b_ohlcv_1m"]
    for r in results.values():
        assert r["status"] == "KNOWN" and tuple(r["request"]["symbols"]) == CONTRACTS and r["unresolved_symbols"] == []
        assert r["request"]["stype_in"] == "raw_symbol" and r["client_version"] == "0.87.0" and r["metadata_call_count"] == 4
    assert all(v is None for v in da["purchase_approval"].values() if v is not False)  # never populated here
