# Rule Freeze — Open Terms Tracker

Every judgement word in `specification.plain_english_summary` is listed here
until it has an exact, codeable definition in `configs/rule_freeze_v1.yaml`.
**Nothing in this file is a rule.** It is a to-do list of definitions still
owed by the rule owner.

Status values: `OPEN` (no definition yet) · `DEFINED` (answered in the named
field) · `DROPPED` (the owner removed the concept).

Source: Round 1 summary, recorded 2026-09-28. The fields for T7, T8, T9,
T12, T16 and T17 were added to the template in Round 2 at the owner's request.

| # | Phrase in the summary | What must be defined | Where it will live | Status |
|---|---|---|---|---|
| T1 | "opening volatility has had time to develop" | Which open (e.g. the 09:30 New York cash open or the 18:00 Globex open) and the exact delay before entries are allowed | `sessions.trading_window_start` | DEFINED (Round 3): 09:30 NY cash open; entries eligible from 09:45:00 inclusive |
| T2 | "eligible session" | Exact entry window start and end | `sessions.trading_window_*` | DEFINED (Round 3): 09:45:00 ≤ decision < 11:30:00 NY |
| T3 | "clear" / "conditions are unclear" | Confirm that "unclear" means only "at least one defined condition is not met", with no separate discretionary clarity judgement | `setup.preconditions` | OPEN |
| T4 | "objectively measurable structure-continuation opportunity" | The complete, measurable setup condition | `setup.definition` | OPEN |
| T5 | "meaningful price levels" / "structural level" / "relevant level" | Which level types; exact formulas; which level applies when several are nearby | `structural_levels.*` | OPEN |
| T6 | "defined acceptance beyond a structural level" | Bars, closes versus wicks, tick distance and count | `acceptance_rejection_breakout.acceptance_definition` | OPEN |
| T7 | "sufficient confirmation that continuation is more likely than immediate rejection" | A concrete event. "More likely" cannot be computed live, so this must be an observable rule | `confirmation.*` | OPEN |
| T8 | "adequate unobstructed room toward the next meaningful level" | Minimum room (points or multiple of risk), and what counts as an obstruction | `room_to_target.*` | OPEN |
| T9 | "developing market structure" / "bullish / bearish structural progression" | Swing-point definition and exact higher-high/higher-low (or equivalent) test | `market_structure.*`, `direction.*` | OPEN |
| T10 | "predefined order method" | Order type and price | `order_type.*`, `order_validity.*` | OPEN |
| T11 | "structural invalidation point" | Exact price that invalidates the idea; whether the stop sits exactly there or with a buffer | `structural_invalidation.definition`, `stop_placement.*` | OPEN |
| T12 | "permitted account risk" | Risk per trade (US dollars or % of what balance); contract rounding; behaviour when even 1 contract exceeds it | `position_management.risk_per_trade` and related | OPEN |
| T13 | "conservatively before the next meaningful opposing level" | Which level counts as "opposing" and how far before it the target sits | `target_placement.*` | OPEN |
| T14 | "mandatory session-closing time" | Exact flatten time and order type | `session_flattening.*` | OPEN |
| T15 | "data are incomplete" | Link to the data policy | `missing_data.*`, `bad_data.*` | OPEN |
| T16 | "execution assumptions are unsafe" | A finite list of measurable conditions, or drop the phrase | `no_trade_conditions.*` | OPEN |
| T17 | "required risk constraints cannot be satisfied" | A finite list, e.g. risk per contract, remaining daily loss, prop limits | `no_trade_conditions.*` | OPEN |
