# Governing Mandate

This document sets the rules for how this research is conducted. The code
serves it. When a technical convenience conflicts with the mandate, the
mandate wins.

## 1. Priority order

Every decision is judged in this order. A higher priority is never traded
away for a lower one.

1. **Account survival.** Not breaching prop-account rules comes first.
2. **Strategy robustness.** Results must hold across time periods,
   parameter neighbourhoods and cost assumptions.
3. **Consistency.** Results must be steady, not driven by a few outlier days.
4. **Drawdown control.** Peak-to-trough losses must stay small relative to
   account limits.
5. **Expected profitability.** This means profit after realistic costs.
6. **Speed toward financial targets.** This comes last, always.

## 2. The planning objective, and what it is NOT

The eventual planning objective is about **$17,000 of payout-producing
profit per funded account, across three identically traded fresh $50K prop
accounts.** That figure describes a *possible outcome to evaluate*, for
example through Monte Carlo survival analysis. **It is not an optimisation
target.**

We never optimise directly for:

* $17,000 of profit;
* a calendar deadline;
* a 90% win rate;
* maximum profit;
* the prettiest equity curve;
* a required number of trades per day.

We never make the strategy trade more often, take more risk, loosen its
filters, or change its behaviour to meet a dollar target or a deadline.
If the honest evidence says the strategy cannot reach the objective safely,
that is a valid and valuable result.

## 3. Testing progression

Each stage must be completed, with its evidence recorded, before the next
begins. Skipping a stage or going back to change rules starts a **new
experiment** with a new ID. It does not silently rewrite an old one.

```
Frozen baseline
   → Historical backtest
      → Diagnostics
         → Carefully justified changes (each one a new, registered experiment)
            → Out-of-sample / walk-forward testing
               → Monte Carlo (evaluation and funded-account survival)
                  → Paper trading
                     → Forward validation
                        → Only then consider prop deployment
```

| Stage | What it means here | Gate to pass |
|---|---|---|
| Frozen baseline | Rule Freeze v1.0 fully answered, approved and hash-locked | `mnq rules check` reports EXECUTABLE |
| Historical backtest | Frozen rules run once on in-sample data, with realistic costs, rolls and sessions | Registered experiment; data passes the contract |
| Diagnostics | Understand *why* results are what they are: by time, regime, setup and cost sensitivity. No parameter changes yet | Written diagnostic report |
| Justified changes | Only changes with a documented, a-priori rationale. Each is a new experiment. Broad, stable parameter *regions* are examined, not single best points | Every configuration counted in the registry |
| Out-of-sample / walk-forward | Chronological validation on data not used for any decision. The untouched holdout is used **once** | Pre-registered splits; no peeking |
| Monte Carlo | Resample trade sequences to estimate the chance of passing the evaluation and surviving funded rules (drawdown type, daily limit, consistency, payouts) | Survival estimates under the recorded prop-rule version |
| Paper trading | Same frozen logic on live data with no money at risk | Behaviour matches the backtest engine; **Databento-vs-Rithmic data parity** and **Python-vs-C# signal parity** tests passed first (see `DATA_ACQUISITION_PLAN.md`) |
| Forward validation | A sustained live-data period compared against expectations | Pre-agreed acceptance criteria |
| Prop deployment | Only then consider a separate C# adapter for Quantower execution | A separate decision, outside this phase |

## 4. Standing rules for all phases

* **No invented rules.** Missing specification items stay `TBD` until you
  answer them explicitly. Discretionary language is never silently turned
  into code.
* **No look-ahead.** Every feature must be calculable using only information
  available at its decision timestamp.
* **Conservative when uncertain.** Where the data cannot settle a question,
  such as the order of events inside a bar, assume the less favourable
  outcome.
* **Count every attempt.** The number of configurations tested is part of the
  evidence. More attempts demand stronger evidence.
* **Report, don't repair.** Data problems are reported and handled by a
  documented policy. They are never silently fixed.
* **No claims of edge** until evidence from every stage supports one.
* **No live trading, broker connectivity, credentials or paid services** in
  the research phase.
* **Scikit-learn is a helper, not the judge.** It may later support
  chronological splitting, preprocessing and parameter-experiment
  bookkeeping. It must never decide fills, stops, account state or
  prop-rule violations. Those belong to our own deterministic simulator.
