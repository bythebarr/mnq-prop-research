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
    p = data.add_parser("validate", help="validate a .parquet or .csv bar file against the data contract")
    p.add_argument("path")
    p.add_argument("--max-gaps", type=int, default=10, help="how many missing-bar gaps to list")
    p.set_defaults(func=cmd_data_validate)

    p = sub.add_parser("hash", help="print reproducible hashes of config files, data files or folders")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_hash)

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
