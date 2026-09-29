# Readiness Gates: what may run, and when (Round 15)

The code is `src/mnq_research/readiness.py`, and the tests are
`tests/test_round15.py`. Check a gate with:

```bash
uv run mnq rules stage SIGNAL_REPLAY          # or ONE_CONTRACT_BACKTEST, PROP_MONTE_CARLO, PAPER_FORWARD, LIVE_CONSIDERATION
```

## The five stages, in order

| Stage | What it lets you run | It additionally needs |
|---|---|---|
| SIGNAL_REPLAY | Count historical signals (no P&L) | Frozen signal logic, instrument, sessions, levels, states, confirmation, direction, room, selection, the data contract, historical calendar and eligibility producers, missing/bad-data rules, replay wiring |
| ONE_CONTRACT_BACKTEST | One-contract P&L and R | Entry, stop and target simulation, costs, slippage, the normal exit, accounting, MAE/MFE, complete trade outcomes, the archived commission source |
| PROP_MONTE_CARLO | Account-level simulation | Versioned Tradeify rules, sizing, risk-per-trade candidates, loss constraints |
| PAPER_FORWARD | Paper trading | Quantower/Rithmic capability verification, the adapter, fault injection, live producers |
| LIVE_CONSIDERATION | Considering live money | Everything: the original full gate (`mnq rules check`) |

## How it works

* **Every field has a stage.** Each field in the spec is assigned the
  earliest stage that needs it (`STAGE_PREFIXES`, in code). A field without
  one is treated as SIGNAL_REPLAY, so it blocks everything (fail closed).
* **Only earlier stages block.** A stage is ready only when its own fields
  and every earlier stage's fields are answered and valid. Unanswered
  later-stage fields never block an earlier research stage.
* **Each stage has its own approval.** It goes in `stage_approvals.<STAGE>`
  and records that stage's **stage hash**, which covers only the fields at
  or before that stage. So:
  * answering a prop-firm field doesn't undo the signal-replay approval;
  * changing a confirmation rule does undo it.
* **Implementation readiness is part of the gate.** `research_pipeline.*`
  and the `execution_eligibility_integration.*_status` fields stay
  `REQUIRED_BEFORE_EXECUTABLE` until the code that produces those states
  exists and is verified. A rule can't be run before its machinery exists.
* **No research approval opens a broker connection.** That requires
  PAPER_FORWARD in full *and* lifting `live_or_paper_order_submission:
  prohibited`.

Right now SIGNAL_REPLAY is blocked only by the unbuilt pipeline (ingestion,
calendars, historical producers, replay wiring) and by its own approval.
