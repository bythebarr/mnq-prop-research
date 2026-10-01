"""Beginner-friendly command-line interface: ``uv run mnq <command>``.

Expected validation failures print a readable explanation and exit with a
non-zero code; they never show a Python stack trace. Exit codes:

  0 = success / check passed
  1 = check ran and found problems (e.g. rule freeze not executable)
  2 = could not run (missing file, malformed YAML, bad arguments)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mnq_research.config import (
    DEFAULT_EXPERIMENT_PATH,
    DEFAULT_REGISTRY_PATH,
    DEFAULT_RULE_FREEZE_PATH,
    DEFAULT_SYNTHETIC_DIR,
    ConfigError,
    find_project_root,
)
from mnq_research.data_contracts import DataLoadError, load_bars, validate_bars
from mnq_research.experiment_registry import (
    ExperimentBlockedError,
    RegistryConflictError,
    assess_experiment,
    load_experiment,
    read_registry,
    register_experiment,
)
from mnq_research.hashing import build_directory_manifest, hash_config_file, hash_file_bytes
from mnq_research.synthetic_data import (
    SYNTHETIC_WARNING,
    SyntheticSpec,
    generate_synthetic_bars,
    inject_defects,
    write_synthetic_dataset,
)
from mnq_research.validation import check_rule_freeze_file

OK, FOUND_PROBLEMS, CANNOT_RUN = 0, 1, 2


def _resolve(path: str | Path, root: Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else root / path


def cmd_rules_check(args: argparse.Namespace, root: Path) -> int:
    report = check_rule_freeze_file(_resolve(args.spec, root))
    print(report.format())
    if not report.is_executable:
        print(
            "\nThis is EXPECTED while the rule freeze is a draft. No backtest or "
            "experiment can run until every item above is answered and approved."
        )
        return FOUND_PROBLEMS
    return OK


def cmd_rules_stage(args: argparse.Namespace, root: Path) -> int:
    from mnq_research.config import load_mapping
    from mnq_research.readiness import Stage, check_stage

    report = check_stage(load_mapping(_resolve(args.spec, root)), Stage(args.stage))
    print(report.format())
    return OK if report.is_ready else FOUND_PROBLEMS


def cmd_data_estimate(args: argparse.Namespace, root: Path) -> int:
    """Databento COST ESTIMATE only (metadata calls). Never downloads, never prints the key."""
    import datetime as dt

    from mnq_research import data_estimate as de
    from mnq_research.config import load_mapping

    spec = load_mapping(_resolve(args.spec, root))
    first = dt.date.fromisoformat(str(spec["source_data"]["history_start_date"]))
    last = dt.date.fromisoformat(str(spec["source_data"]["history_end_date"]))
    requests = de.alternatives(first, last)
    if args.alternatives:
        wanted = [a.strip() for a in args.alternatives.split(",") if a.strip()]
        unknown = sorted(set(wanted) - {r.alternative for r in requests})
        if unknown:
            print(f"unknown alternatives: {unknown}")
            return CANNOT_RUN
        requests = [r for r in requests if r.alternative in wanted]
    out_dir = _resolve(args.out, root)
    try:
        key = de.load_api_key()
    except de.MissingCredentialError as exc:
        results = [
            de.EstimateResult(request=de.asdict(r), status=de.EstimateStatus.UNKNOWN, estimated_cost_before_credits_usd=None,
                              applicable_credits_usd=None, estimated_charge_usd=None, currency="USD",
                              billable_size_uncompressed_bytes=None, compressed_size_bytes=de.NOT_EXPOSED, record_count=None,
                              symbol_resolution={}, includes_definitions=r.schema == "definition",
                              double_count_risk="not assessed: no request made", warnings=[str(exc)])
            for r in requests
        ]
        path = de.write_artifact(results, out_dir, None)
        print(f"{exc}.\nNo request was sent. Planned requests written with status UNKNOWN: {path}")
        print(f"After setting {de.API_KEY_ENV} in your shell (never in a tracked file), run:\n"
              "  uv run --extra databento mnq data estimate")
        return FOUND_PROBLEMS
    import databento

    client = de.MetadataOnlyClient(databento.Historical(key))
    results = [de.estimate(r, client, databento.__version__, key) for r in requests]
    path = de.write_artifact(results, out_dir, key)
    for r in results:
        print(f"{r.request['alternative']:3} {r.request['schema']:10} {r.status.value:8} charge={r.estimated_charge_usd} "
              f"size_bytes={r.billable_size_uncompressed_bytes} warnings={len(r.warnings)}")
    print(f"Artifact: {path}")
    return OK if all(r.status is de.EstimateStatus.KNOWN for r in results) else FOUND_PROBLEMS


def cmd_data_acquire(args: argparse.Namespace, root: Path) -> int:
    """Approved purchase only: fail-closed preflight, then idempotent batch jobs. Key from DATABENTO_API_KEY."""
    from mnq_research import data_acquisition as da
    from mnq_research import data_estimate as de
    from mnq_research.config import load_mapping

    spec = load_mapping(_resolve(args.spec, root))
    ledger = da.Ledger(root / da.LEDGER_PATH)
    try:
        key = de.load_api_key()
    except de.MissingCredentialError as exc:
        print(f"PURCHASE_PREFLIGHT_FAILED: {exc}")
        return FOUND_PROBLEMS
    import databento

    client = databento.Historical(key)
    if args.preflight_only:
        report, _, _ = da.preflight(spec, root, de.MetadataOnlyClient(client), databento.__version__, key, ledger)
        result = {"preflight": report, "outcome": "PREFLIGHT_ONLY"}
    else:
        try:
            result = da.run_staged(spec, root, client.batch, de.MetadataOnlyClient(client), databento.__version__, key, ledger,
                                   poll_timeout=args.timeout, poll_seconds=args.poll_seconds)
        except (da.PreflightFailed, da.ReconciliationRequired) as exc:
            print(de.redact(str(exc), key))
            return FOUND_PROBLEMS
    report = result.get("preflight")
    if report is not None:
        out = root / "outputs" / "acquisition"
        out.mkdir(parents=True, exist_ok=True)
        stamp = report.checked_utc.replace(":", "").replace("-", "")[:15]
        (out / f"preflight_{stamp}.json").write_text(json.dumps(de.asdict(report), indent=2, sort_keys=True, default=str) + "\n")
        print(json.dumps({"preflight_passed": report.passed, "problems": report.problems, "fresh_costs_usd": report.fresh_costs_usd,
                          "fresh_combined_usd": report.fresh_combined_usd, "checked_utc": report.checked_utc}, indent=2))
    summary = {s: {k: e.get(k) for k in ("status", "job_id", "databento_ts_received", "final_charge_usd", "raw_files", "last_status_check")}
               for s, e in ((e["request"]["schema"], e) for e in ledger.data["entries"].values())}
    print(json.dumps({"outcome": result["outcome"], "problems": result.get("problems", []), "ledger": summary}, indent=2, default=str))
    if result["outcome"].endswith("_SUBMITTED_CHECKPOINT"):
        print("CHECKPOINT: commit and push the ledger now, then rerun the same command to poll (it never resubmits).")
    return OK if result["outcome"] in ("PREFLIGHT_ONLY", "OHLCV_SUBMITTED_CHECKPOINT", "DEFINITIONS_SUBMITTED_CHECKPOINT",
                                       "OHLCV_SUBMITTED", "OHLCV_DOWNLOADED_UNVALIDATED", "OHLCV_COMPLETED_VERIFIED") and \
        (report is None or report.passed) else FOUND_PROBLEMS


def cmd_data_ingest(args: argparse.Namespace, root: Path) -> int:
    """Validate the approved raw DBN files and derive the canonical dataset (never modifies raw files)."""
    import datetime as dt
    import hashlib
    import subprocess

    from mnq_research import data_acquisition as da
    from mnq_research import raw_ingest as ri
    from mnq_research.config import load_mapping
    from mnq_research.hashing import hash_object

    spec = load_mapping(_resolve(args.spec, root))
    approval, requests = da.approved_requests(spec, root)
    ledger = da.Ledger(root / da.LEDGER_PATH)
    first = dt.date.fromisoformat(str(spec["source_data"]["history_start_date"]))
    last = dt.date.fromisoformat(str(spec["source_data"]["history_end_date"]))
    report: dict = {"raw_inputs": [], "validation": {}}
    frames = {}
    for request in requests:
        entry = ledger.entry(da.request_key(request))
        if not entry or not entry.get("raw_files"):
            print(f"no downloaded raw files for {request.schema}")
            return FOUND_PROBLEMS
        dbn = [f for f in entry["raw_files"] if f["path"].endswith((".dbn.zst", ".dbn"))]
        if len(dbn) != 1:
            print(f"expected exactly one DBN file for {request.schema}, found {len(dbn)}")
            return FOUND_PROBLEMS
        f = dbn[0]
        record_count = (entry.get("final_job") or {}).get("record_count")
        try:
            meta, df = ri.load_dbn(root / f["path"], f["sha256"])
        except Exception as exc:  # corrupt / truncated / modified
            report["validation"][request.schema] = {"passed": False, "issues": [f"DECODE_OR_HASH_FAILURE: {type(exc).__name__}: {exc}"]}
            frames[request.schema] = None
            continue
        v = ri.check_metadata(meta, request.dataset, request.schema, request.symbols, request.start_utc, request.end_utc)
        if request.schema == "definition":
            dv, ids = ri.validate_definitions(df.reset_index(), request.symbols)
            v.issues += dv.issues
            report["definition_instrument_ids"] = ids
            frames["definition"] = df
        else:
            ov, clean = ri.validate_ohlcv(df, request.symbols, request.start_utc, request.end_utc, record_count)
            v.issues += ov.issues
            frames["ohlcv-1m"] = clean
        report["validation"][request.schema] = {"passed": v.passed, "issues": [i.__dict__ for i in v.issues],
                                                "records_decoded": int(len(df)), "job_record_count": record_count}
        report["raw_inputs"].append({"schema": request.schema, "job_id": entry["job_id"], **f})
    passed = all(x["passed"] for x in report["validation"].values()) and frames.get("ohlcv-1m") is not None
    output = {}
    if frames.get("ohlcv-1m") is not None and "definition_instrument_ids" in report:
        ohlcv = frames["ohlcv-1m"]
        ids = report["definition_instrument_ids"]
        mismatch = [s for s, g in ohlcv.groupby("symbol")["instrument_id"] if not set(g.astype(int)) <= set(ids.get(s, []))]
        report["validation"]["cross_check"] = {"passed": not mismatch, "ohlcv_ids_not_in_definitions": mismatch}
        passed = passed and not mismatch
        created = dt.datetime.now(dt.timezone.utc).isoformat()
        ingestion_utc = min(e.get("downloaded_utc", created) for e in ledger.data["entries"].values())
        canon = ri.canonicalize(ohlcv, first, last, ingestion_utc)
        cv, summary = ri.coverage(canon, requests[1].symbols, first, last)
        report["validation"]["coverage"] = {"passed": cv.passed, "issues": [i.__dict__ for i in cv.issues]}
        passed = passed and cv.passed
        digest = ri.content_hash(canon)
        out_path = root / "data" / "processed" / f"mnq_ohlcv_1m_canonical_{digest[:16]}.parquet"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canon.to_parquet(out_path, index=False)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
        code_sha = hashlib.sha256(Path(ri.__file__).read_bytes()).hexdigest()
        output = {
            "output_path": str(out_path.relative_to(root)), "output_file_sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
            "output_content_sha256": digest, "row_count": int(len(canon)),
            "contract_row_counts": {k: int(v) for k, v in canon.groupby("contract").size().items()},
            "rows_in_designated_windows": int(canon["in_designated_window"].sum()),
            "coverage": summary, "ingestion_code_git_commit": commit, "raw_ingest_module_sha256": code_sha,
            "configuration_sha256": hash_object({"purchase_approval": spec["data_acquisition"]["purchase_approval"],
                                                  "history": [str(first), str(last)], "roll_rule": spec["contract_roll"]["roll_trigger"]}),
            "exclusions": [i for x in report["validation"].values() for i in x.get("issues", []) if isinstance(i, dict) and not i.get("blocking")],
            "missing_minute_classification": ri.UNCLASSIFIED + " for every absent scheduled minute (no calendar evidence yet); none labelled VERIFIED_NO_TRADE",
            "price_adjustment": "NONE: original unadjusted individual-contract prices; no continuous series",
            "created_utc": created,
        }
    manifest = {"kind": "MNQ_INGESTION_MANIFEST", "validation_status": "PASSED" if passed else "FAILED",
                "approval_payload_sha256": approval.estimate_payload_sha256, **report, **output}
    mpath = root / "outputs" / "acquisition" / "INGESTION_MANIFEST.json"
    mpath.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
    if passed:
        for request in requests:
            entry = ledger.entry(da.request_key(request))
            entry["status"] = "COMPLETED_VERIFIED"
            ledger.put(da.request_key(request), entry)
    print(json.dumps({"validation_status": manifest["validation_status"], "validation": report["validation"],
                      **{k: output.get(k) for k in ("output_path", "output_file_sha256", "output_content_sha256", "row_count")}},
                     indent=2, default=str))
    return OK if passed else FOUND_PROBLEMS


def cmd_sources_capture_raw(args: argparse.Namespace, root: Path) -> int:
    from mnq_research.source_archive import capture_raw_source

    m = capture_raw_source(args.url, _resolve(args.out, root))
    print(json.dumps(m, indent=2, sort_keys=True))
    return OK if m["status"] == "CAPTURED" else FOUND_PROBLEMS


def cmd_sources_archive_commission(args: argparse.Namespace, root: Path) -> int:
    from mnq_research.config import load_mapping
    from mnq_research.source_archive import archive_commission_source

    c = load_mapping(_resolve(args.spec, root))["commissions"]
    m = archive_commission_source(str(c["round_turn_per_contract_usd"]), str(c["per_side_per_contract_usd"]), _resolve(args.out, root))
    print(json.dumps(m.__dict__, indent=2))
    return OK if m.status == "MATCHES_CONFIGURATION" else FOUND_PROBLEMS


def cmd_data_synth(args: argparse.Namespace, root: Path) -> int:
    spec = SyntheticSpec(first_trading_date=args.start, n_trading_days=args.days, seed=args.seed)
    df = generate_synthetic_bars(spec)
    defects = None
    if args.with_defects:
        df, defects = inject_defects(df)
    default_name = f"synthetic_FAKE_mnq_seed{args.seed}{'_with_defects' if args.with_defects else ''}.parquet"
    out = _resolve(args.out, root) if args.out else root / DEFAULT_SYNTHETIC_DIR / default_name
    manifest = write_synthetic_dataset(df, out, spec, defects)
    print(f"!!! {SYNTHETIC_WARNING} !!!\n")
    print(f"Wrote {len(df)} FAKE bars to: {out}")
    print(f"Manifest (with hashes):     {manifest}")
    if defects:
        print(f"Known defects planted: {json.dumps({k: v for k, v in defects.items() if k != 'missing_bar_timestamps_utc'})}")
    print(f"\nNext: uv run mnq data validate {out.relative_to(root) if out.is_relative_to(root) else out}")
    return OK


def cmd_data_validate(args: argparse.Namespace, root: Path) -> int:
    df = load_bars(_resolve(args.path, root))
    report = validate_bars(df)
    print(report.format(max_gaps=args.max_gaps))
    return OK if report.is_valid else FOUND_PROBLEMS


def cmd_hash(args: argparse.Namespace, root: Path) -> int:
    for raw in args.paths:
        path = _resolve(raw, root)
        if path.is_dir():
            manifest = build_directory_manifest(path)
            print(f"{raw}  (directory, {len(manifest['files'])} files)")
            print(f"  manifest sha256:  {manifest['manifest_sha256']}")
        elif path.suffix.lower() in (".yaml", ".yml"):
            print(f"{raw}")
            print(f"  canonical config sha256: {hash_config_file(path)}  (ignores formatting/key order/comments)")
            print(f"  exact file sha256:       {hash_file_bytes(path)}")
            print("  (For rule-freeze approval use the 'Spec hash' printed by `mnq rules check`.)")
        elif path.is_file():
            print(f"{raw}\n  file sha256: {hash_file_bytes(path)}")
        else:
            raise ConfigError(f"Not found: {raw}")
    return OK


def cmd_experiment_inspect(args: argparse.Namespace, root: Path) -> int:
    record = load_experiment(_resolve(args.path, root))
    assessment = assess_experiment(record, root)
    print(assessment.format())
    entries = [e for e in read_registry(root / DEFAULT_REGISTRY_PATH) if e["experiment_id"] == record.experiment_id]
    print(f"\nRegistry entries for {record.experiment_id}: {len(entries) or 'none'}")
    for entry in entries:
        print(f"  - registered {entry['registered_at_utc']} code={entry['code_version']} plan_hash={entry['plan_hash'][:16]}...")
    return OK if assessment.can_register or entries else FOUND_PROBLEMS


def cmd_experiment_register(args: argparse.Namespace, root: Path) -> int:
    try:
        entry, new = register_experiment(_resolve(args.path, root), root, root / DEFAULT_REGISTRY_PATH)
    except ExperimentBlockedError as exc:
        print(exc.assessment.format())
        print("\nNothing was registered. This is EXPECTED until Rule Freeze v1.0 is approved.")
        return FOUND_PROBLEMS
    except RegistryConflictError as exc:
        print(f"REFUSED: {exc}")
        return FOUND_PROBLEMS
    verb = "Registered" if new else "Already registered (identical content)"
    print(f"{verb}: {entry['experiment_id']} plan_hash={entry['plan_hash']}")
    print(f"Registry: {DEFAULT_REGISTRY_PATH} (commit this file to Git)")
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mnq",
        description="MNQ research foundation (Phase 1). No strategy, no optimisation, no broker connections.",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    rules = sub.add_parser("rules", help="rule-freeze specification").add_subparsers(dest="action", required=True)
    p = rules.add_parser("check", help="check whether the rule freeze is complete and approved")
    p.add_argument("--spec", default=str(DEFAULT_RULE_FREEZE_PATH))
    p.set_defaults(func=cmd_rules_check)
    p = rules.add_parser("stage", help="check one staged readiness gate (SIGNAL_REPLAY, ONE_CONTRACT_BACKTEST, ...)")
    p.add_argument("stage", choices=["SIGNAL_REPLAY", "ONE_CONTRACT_BACKTEST", "PROP_MONTE_CARLO", "PAPER_FORWARD", "LIVE_CONSIDERATION"])
    p.add_argument("--spec", default=str(DEFAULT_RULE_FREEZE_PATH))
    p.set_defaults(func=cmd_rules_stage)

    data = sub.add_parser("data", help="bar data").add_subparsers(dest="action", required=True)
    p = data.add_parser("synth", help="generate SYNTHETIC FAKE bars for software tests")
    p.add_argument("--start", default="2024-01-08", help="first trading date (a weekday), default 2024-01-08")
    p.add_argument("--days", type=int, default=3, help="number of trading days (default 3)")
    p.add_argument("--seed", type=int, default=20240108, help="random seed (same seed = same data)")
    p.add_argument("--with-defects", action="store_true", help="plant known defects to demonstrate validation")
    p.add_argument("--out", help=f"output .parquet path (default under {DEFAULT_SYNTHETIC_DIR})")
    p.set_defaults(func=cmd_data_synth)
    p = data.add_parser("estimate", help="Databento COST ESTIMATE only (no download); key from DATABENTO_API_KEY")
    p.add_argument("--spec", default=str(DEFAULT_RULE_FREEZE_PATH))
    p.add_argument("--out", default="outputs/estimates")
    p.add_argument("--alternatives", default="", help="comma-separated subset, e.g. A,B (default: all)")
    p.set_defaults(func=cmd_data_estimate)
    p = data.add_parser("acquire", help="APPROVED purchase only: preflight + idempotent Databento batch jobs")
    p.add_argument("--spec", default=str(DEFAULT_RULE_FREEZE_PATH))
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--timeout", type=float, default=480.0, help="bounded read-only polling window in seconds (rerun resumes)")
    p.add_argument("--poll-seconds", type=float, default=30.0, help="seconds between read-only status queries")
    p.set_defaults(func=cmd_data_acquire)
    p = data.add_parser("ingest", help="validate approved raw DBN files and derive the canonical dataset")
    p.add_argument("--spec", default=str(DEFAULT_RULE_FREEZE_PATH))
    p.set_defaults(func=cmd_data_ingest)
    p = data.add_parser("validate", help="validate a .parquet or .csv bar file against the data contract")
    p.add_argument("path")
    p.add_argument("--max-gaps", type=int, default=10, help="how many missing-bar gaps to list")
    p.set_defaults(func=cmd_data_validate)

    p = sub.add_parser("hash", help="print reproducible hashes of config files, data files or folders")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_hash)

    src = sub.add_parser("sources", help="archive external sources").add_subparsers(dest="action", required=True)
    p = src.add_parser("archive-commission", help="archive the Tradeify commission page (raw bytes + SHA-256 manifest)")
    p.add_argument("--spec", default=str(DEFAULT_RULE_FREEZE_PATH))
    p.add_argument("--out", default="archives/sources/tradeify_commissions")
    p.set_defaults(func=cmd_sources_archive_commission)
    p = src.add_parser("capture-raw", help="archive one official page as raw bytes + SHA-256 manifest (no parsing)")
    p.add_argument("url")
    p.add_argument("--out", default="archives/calendars")
    p.set_defaults(func=cmd_sources_capture_raw)

    exp = sub.add_parser("experiment", help="experiment registry").add_subparsers(dest="action", required=True)
    p = exp.add_parser("inspect", help="show an experiment plan and everything blocking it")
    p.add_argument("path", nargs="?", default=str(DEFAULT_EXPERIMENT_PATH))
    p.set_defaults(func=cmd_experiment_inspect)
    p = exp.add_parser("register", help="pre-register an experiment (refused while blocked)")
    p.add_argument("path", nargs="?", default=str(DEFAULT_EXPERIMENT_PATH))
    p.set_defaults(func=cmd_experiment_register)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = find_project_root()
        return args.func(args, root)
    except (ConfigError, DataLoadError, FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return CANNOT_RUN


if __name__ == "__main__":
    sys.exit(main())
