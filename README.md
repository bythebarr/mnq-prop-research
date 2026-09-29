# mnq-prop-research

An evidence-driven research and testing system for a Micro E-mini Nasdaq-100
(**MNQ**) futures strategy intended, eventually, for prop-firm accounts.

> **Current phase: Phase 1, Foundation.** There is **no trading strategy**
> in this repository yet, and **no results**. The rule specification is a
> DRAFT and the software deliberately refuses to execute it.

## What this project is

A disciplined pipeline that will, step by step:

1. convert an **explicitly defined** MNQ strategy into deterministic code;
2. freeze that baseline, with its content hashed and approved;
3. backtest it with realistic commissions, slippage, sessions and contract
   rolls;
4. diagnose the results **without** immediately changing weak parameters;
5. study broad, stable regions of parameter space, not one historical
   "winner";
6. validate chronologically, with out-of-sample and walk-forward tests;
7. estimate prop-evaluation and funded-account survival with Monte Carlo;
8. paper trade and forward validate;
9. only then consider a separate C# adapter for Quantower execution.

The priorities, in order, are **account survival, robustness, consistency,
drawdown control, expected profit, then speed**. See
[`docs/GOVERNING_MANDATE.md`](docs/GOVERNING_MANDATE.md).

## What this project is NOT

* Not a trading bot. There is no broker connection, no live trading, no
  Quantower code, no credentials and no paid services.
* Not an optimiser hunting for the best historical parameters.
* Not a claim that any strategy has an edge.
* The synthetic data it can generate is **fake** and proves nothing about
  any strategy.

## Why no strategy has been coded yet

The strategy's rules are not yet specified precisely enough to code. Writing
code now would mean guessing at definitions such as what counts as
"rejection" or which time zone "09:30" is in. Every guess would quietly
become part of the "strategy", and the backtest would then test the guesses,
not your strategy. So `configs/rule_freeze_v1.yaml` lists every open
question as `TBD`, and the software **blocks** any use of the rules until
every question is answered and you formally approve the result.

## Setup (one time)

You need a terminal opened in this folder.

**Option A: with `uv` (recommended; already installed here)**

```bash
uv sync
```

This creates a private environment in `.venv/` with Python 3.11 and the
exact library versions recorded in `uv.lock`.

**Option B: without `uv`**

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e . pytest
```

With option B, drop the `uv run` prefix from the commands below. For
example, run `mnq rules check` instead of `uv run mnq rules check`.

## Everyday commands

| Goal | Command |
|---|---|
| 1. Check the rule freeze (lists every unanswered question) | `uv run mnq rules check` |
| 2. Generate SYNTHETIC FAKE test data | `uv run mnq data synth` |
| 2b. …with known defects planted, to see validation catch them | `uv run mnq data synth --with-defects` |
| 3. Validate a data file | `uv run mnq data validate data/interim/synthetic/synthetic_FAKE_mnq_seed20240108.parquet` |
| 4a. Inspect an experiment and what blocks it | `uv run mnq experiment inspect` |
| 4b. Try to register an experiment (refused while blocked) | `uv run mnq experiment register` |
| 5. Run the automated tests | `uv run pytest` |
| Print reproducible hashes | `uv run mnq hash configs/rule_freeze_v1.yaml` |
| Help | `uv run mnq --help` or, e.g., `uv run mnq data synth --help` |

**Exit codes:** `0` means OK. `1` means the check found problems. For
example, `rules check` returns 1 for a draft, which is **expected right
now**. `2` means the command couldn't run, for example because a file was
missing.

## Directory structure

```
mnq-prop-research/
├── README.md                  ← you are here
├── pyproject.toml             ← project metadata and dependencies
├── uv.lock                    ← exact library versions (reproducibility)
├── .python-version            ← Python 3.11
├── configs/
│   ├── rule_freeze_v1.yaml    ← DRAFT rule specification (all TBD, NON-EXECUTABLE)
│   └── experiment_001.yaml    ← baseline experiment plan (NOT RUN, blocked)
├── docs/
│   ├── GOVERNING_MANDATE.md   ← priorities and testing progression
│   ├── RULE_FREEZE_GUIDE.md   ← every open question, in plain English
│   ├── DATA_CONTRACT.md       ← bar-data schema, time policy, intrabar ambiguity
│   ├── LEVEL_STATES.md        ← plain-English zone state rules
│   ├── CONFIRMATION.md        ← plain-English confirmation rules
│   ├── DIRECTION.md           ← plain-English direction rules
│   ├── TRADE_GEOMETRY.md      ← plain-English geometry and selection rules
│   ├── ORDER_LIFECYCLE.md     ← plain-English entry-order lifecycle (simulation only)
│   ├── PROTECTIVE_ORDERS.md   ← plain-English stop/target bracket (simulation only)
│   ├── CRITICAL_PATH.md       ← what remains, by milestone, to reach Baseline Test 001
│   └── DECISIONS.md           ← architecture decisions and rationale
├── data/                      ← NOT committed to Git (except placeholders)
│   ├── raw/                   ← vendor files exactly as received
│   ├── interim/               ← converted or synthetic files
│   └── processed/             ← validated, research-ready files
├── outputs/
│   ├── experiments/           ← registry.jsonl audit trail (committed)
│   ├── figures/               ← generated charts (not committed)
│   └── reports/               ← generated reports (not committed)
├── src/mnq_research/
│   ├── __init__.py, __main__.py
│   ├── cli.py                 ← the `mnq` command
│   ├── config.py              ← strict YAML loading
│   ├── validation.py          ← rule-freeze completeness and approval gate
│   ├── data_contracts.py      ← bar schema and data validation
│   ├── hashing.py             ← reproducible fingerprints
│   ├── experiment_registry.py ← pre-registration of experiments
│   ├── confirmation.py        ← Round 10 continuation confirmation (component only)
│   ├── direction.py           ← Round 11 event-derived direction and conflicts (component only)
│   ├── trade_geometry.py      ← Round 12 planned entry/stop/target and selection (component only)
│   ├── eligibility.py         ← D-028 typed, fail-closed eligibility controls
│   ├── entry_order.py         ← Round 13 simulated market entry-order lifecycle (no broker)
│   ├── sizing.py              ← D-029 floor/skip sizing mechanics (dollar risk unresolved)
│   ├── protection.py          ← Round 14 simulated protective stop/target OCO bracket (no broker)
│   ├── level_states.py        ← Round 9 zone interaction states (component only)
│   ├── structural_levels.py   ← Round 8 level rules (component only; not a backtest)
│   └── synthetic_data.py      ← FAKE data for software tests only
└── tests/                     ← automated checks of the protections
```

## How hashes stop rules changing quietly

* The rule spec has a **canonical hash**, a fingerprint that ignores
  formatting but changes if any rule changes.
* **Approval** records that hash. Editing any rule afterwards breaks the
  match, and the spec becomes non-executable until it is re-approved as a
  new version.
* **Registering** an experiment writes its plan, the rule hash, the data
  hash, the Git commit and the time into an append-only log committed to
  Git. The same experiment ID can never be re-registered with different
  content. Changes need a new ID, so every attempt stays on the record.

## Next required milestone: Rule Freeze v1.0 approval

1. Work through [`docs/RULE_FREEZE_GUIDE.md`](docs/RULE_FREEZE_GUIDE.md) in
   order, writing answers into `configs/rule_freeze_v1.yaml`.
2. Run `uv run mnq rules check` until only the status and approval items
   remain.
3. Set `status: FROZEN_APPROVED` and complete `approval_record` with the
   printed Spec hash.
4. `uv run mnq rules check` reports **EXECUTABLE**. Only then does Phase 2
   (data ingestion and a deterministic baseline simulator) begin.
