# Critical Path to Frozen Baseline Test 001

Recorded after D-031 (2026-09-29). This lists every item still unresolved in
`configs/rule_freeze_v1.yaml` (`uv run mnq rules check`), plus the
non-spec work, grouped by the **first milestone that needs it**. Nothing
here adds strategy logic, indicators, filters, entries, management or
optimisation.

> **One structural blocker affects every group.** The validator currently has
> a single gate. Nothing may execute until *every* field, including
> prop-firm and deployment items, is answered and approved. A research
> replay therefore cannot legally run until live-deployment questions are
> settled. The smallest fix is a **staged readiness gate**: each field is
> tagged with the first milestone that needs it, and a replay may run only
> when its own stage and all earlier stages are answered and approved.
> Every later-stage item must still be listed as blocking the next stage.

## 1. Required before a preliminary signal-frequency replay

This replay counts signals only (confirmations, candidates, selections), with
no P&L.

| Item | Why | Kind |
|---|---|---|
| Staged readiness gate (see above) | Otherwise nothing may run | Code + owner approval |
| `setup.definition`, `setup.preconditions`, `setup.setup_expiry` | Likely just pointers to the frozen chain (T3/T4), but you must say so | Owner confirmation |
| `ema.included`, `vwap.included` | B0 has no indicators; setting `false` disables the other 14 fields | Owner confirmation |
| `missing_data.policy`, `missing_data.max_tolerated_gap_minutes` | How outages and missing minutes interrupt the state machine | Owner decision |
| `bad_data.policy`, `bad_data.handling_of_rejected_bars` | What happens to bars that fail validation | Owner decision |
| `execution_eligibility_integration.status` (historical producers) | Session, news and data states must be *typed inputs* to the replay | Code |
| Databento acquisition | Cost estimate → your approval → download (no automatic purchase) | Owner approval |
| Ingestion and validation | Vendor → data contract; classify missing minutes; roll mapping | Code |
| Historical CME calendar artifact | Holidays and early closes | Data |
| Historical news calendar artifact | Blackouts and protection buffers | Data |
| End-to-end deterministic replay | Wiring: bars → 5-min decisions → levels → states → confirmation → direction → geometry → entry recheck | Code |

## 2. Required before a complete one-contract baseline backtest

This means P&L and R, at the research quantity of 1.

| Item | Why | Kind |
|---|---|---|
| `commissions.round_turn_per_contract_usd`, `commissions.includes_exchange_clearing_nfa_fees`, `commissions.source_and_date` | Net P&L | Owner decision + a dated source |
| `slippage.entry_ticks`, `slippage.stop_exit_ticks`, `slippage.target_exit_ticks`, `slippage.market_exit_ticks` | Fill prices; the stop fill is currently `UNRESOLVED_UNTIL_STOP_SLIPPAGE_MODEL_FROZEN` | Owner decision |
| `slippage.stress_test_multipliers` | The robustness report | Owner decision |
| **`session_flattening.flatten_all_by`, `session_flattening.flatten_order_type`, `position_management.time_based_exit`** | **Critical gap:** today a trade that hits neither stop nor target stays open until the 15:55 emergency backstop, which you described as a safety net, not a planned exit | Owner decision |
| `structural_invalidation.action` | Is there any exit other than the stop, e.g. `NONE`? | Owner decision |
| `intrabar_ambiguity.entry_and_exit_same_bar` | Entry fill and stop/target in the same minute | Owner decision |
| `missing_data.open_position_during_gap` | A data gap while a trade is open | Owner decision |
| `daily_limits.max_losing_trades_per_day` | With one trade a day this is probably `NOT_APPLICABLE` | Owner confirmation |
| `no_trade_conditions.execution_safety_conditions` | Possibly answered by the typed eligibility list | Owner confirmation |
| Trade accounting | Gross and net P&L, actual R, costs per leg, MAE/MFE (excursion) measurement, trade ledger | Code |
| Replay integration of the entry fill model, the protective bracket and exits | The first performance report | Code |

## 3. Required before Monte Carlo and prop-account simulation

| Item | Kind |
|---|---|
| `prop_account_rules.*`: rules document version and date, profit target, max loss, drawdown type, daily loss limit, max contracts, consistency rule, minimum days, payout rules, other restrictions | Capture the Tradeify rules document |
| `position_management.risk_per_trade` (`UNRESOLVED_EVIDENCE_DERIVED`) | Evidence from the baseline |
| `position_management.position_sizing_balance_basis` | Prop-rule model |
| `position_management.contracts_per_trade`, `position_management.sizing_method`, `position_management.max_contracts_per_trade` | Owner decision after the evidence |
| `daily_limits.daily_loss_stop_usd` | Owner decision after the evidence |
| `no_trade_conditions.risk_constraint_conditions` | Owner decision |

## 4. Required only before paper or live deployment

| Item | Kind |
|---|---|
| `protective_orders.deployment.capability_verification_status` | Quantower/Rithmic adapter and paper tests |
| Server-side stop and OCO verification, and fault injection for the session-flatten sequence | Adapter tests |
| Live producers for every eligibility control (news, safety, data, positions) | Code |
| `live_or_paper_order_submission` (stays `prohibited` until then) | Owner decision |
| The C# Quantower adapter | A separate project |

## Recommended next round (Round 15): costs, exit completion and trade accounting

This is the smallest round that unblocks a performance report once data exists.
1. **Owner decisions** (no new strategy logic):
   * commissions;
   * slippage ticks per order type, and the stress multipliers;
   * the normal flatten time and order type, and the time-based exit;
   * `structural_invalidation.action`;
   * entry and exit in the same minute;
   * an open position during a data gap;
   * the confirmations for setup, EMA/VWAP `included: false`,
     `max_losing_trades_per_day` and the execution-safety list.
2. **Code:**
   * the cost and slippage model;
   * trade accounting (P&L, R, per-leg costs, MAE/MFE);
   * the staged readiness gate.

Then Round 16: the Databento cost estimate for your approval, ingestion,
validation, and the calendar artifacts. Then Round 17: end-to-end replay and
**Frozen Baseline Test 001**, pre-registered before it runs.
