# AgentQuant

AgentQuant is a research and backtesting system for China futures trading.  The
current codebase focuses on one market type, `china_futures`, and runs a
four-phase daily workflow:

1. **Phase 1 - proposal**: analyst agents and the portfolio manager create
   auditable futures recommendations before execution.
2. **Phase 2 - order**: the trader converts pending recommendations and rollover
   needs into orders, using deterministic intraday confirmation.
3. **Phase 3 - settlement**: the accountant marks positions to market, books
   commission, margin, and daily PnL.
4. **Phase 4 - review**: the reviewer validates phase consistency and writes
   bounded learning feedback for future runs.

The default development configuration trades 15 China futures underlyings:

`BU`, `C`, `CF`, `EB`, `HC`, `I`, `J`, `M`, `MA`, `P`, `PB`, `RB`, `SR`, `TA`,
and `ZN`.

## Current Development State

AgentQuant is no longer only a loose multi-agent prototype.  The important
runtime boundaries are now explicit in code:

- `src/run/backtest.py` is the main replay entry point. It walks trading days,
  skips phases already completed in SQLite, and runs evaluation by default after
  the backtest window.
- `src/run/proposal.py`, `src/run/order.py`, `src/run/settlement.py`, and
  `src/run/validate_phase_flow.py` can still be run phase by phase.
- Phase 1 uses a LangGraph workflow. Analysts and the portfolio manager may use
  LLM synthesis, but their outputs are wrapped with deterministic controls and
  local artifact contracts.
- Phase 2, Phase 3, and the main Phase 4 validation path are deterministic.
  They should not reinterpret the market thesis created in Phase 1.
- The reviewer may optionally ask an LLM for causal-review notes, but those
  notes are stored separately and do not become trading policy unless the
  deterministic rule engine later validates them.
- The project adopts useful A2A-style artifact principles, such as stable
  headers and source references, but it is **not** migrating to an A2A runtime.
- SQLite is the active local state store for `china_futures` development and
  backtests.

## Data And Evidence

The configured workflow combines local and vendor data:

- **PandaAI futures data**: daily bars, minute bars, main-contract mapping,
  settlement references, and optional intraday/extra factor inputs.
- **Finoview factor files**: local feather files under
  `data/Fundamental_data/Finoview_data`, interpreted through
  `src/config/finoview_factor_catalog.yaml`.
- **Local news and text evidence**: commodity news files are filtered by the
  configured pre-open cutoff.
- **SQLite audit state**: recommendations, transactions, settlements, reviewer
  reports, and learning overlays are persisted in `src/assets/agentquant.db`.

Finoview snapshots are built with no-lookahead rules.  The snapshot code applies
trading-date cutoffs, release-lag rules, required group coverage, freshness
checks, and explicit penalties for unknown or stale data.

## Agent Roles

| Component | Current role |
| --- | --- |
| `technical_analyst` | Reads price/technical context and produces directional technical evidence. |
| `fundamental_analyst` | Uses PandaAI and Finoview snapshots to summarize fundamental pressure, coverage, and freshness. |
| `commodity_news_analyst` | Reads pre-open commodity news evidence for the trading date. |
| `portfolio_manager` | Combines analyst evidence, risk state, market confirmation, learned context, and deterministic controls into `futures_recommendation` rows. |
| `trade_auditor` | Deterministic gate that can allow, scale down, probe only, reduce only, or block a proposed action. |
| `trader` | Executes pending recommendations and rollover instructions during Phase 2. |
| `accountant` | Performs daily settlement and official portfolio state updates in Phase 3. |
| `reviewer` | Validates phase flow, writes transaction/reviewer logs, and stores bounded learning feedback in Phase 4. |
| `planner` | Available in code, but `planner_mode` is disabled in the default config. |

## Risk And Execution Controls

The default configuration is designed to keep trading behavior auditable rather
than aggressively maximize turnover:

- Total margin usage has a hard portfolio cap from config, with additional
  capital-utilization controls for normal scaling.
- Position sizing considers target margin, base per-opportunity sizing anchors, signal
  strength, market confirmation, quality gates, recent ticker behavior, and
  strategy memory.
- The trade auditor applies deterministic business-quality, neutralization,
  drawdown, cooldown, and memory-aware checks.
- Phase 2 uses intraday confirmation before execution.  The default setup uses
  15-minute decision bars, 1-minute execution bars, an opening-range check, and
  a configured `finalize_after` time.
- Rollover recommendations are processed before normal strategy
  recommendations and are reconciled with the strategy target when configured.

## Learning Loop

The learning layer is present, but intentionally bounded:

- Reviewer outputs update tables such as `strategy_memory_history`,
  `signal_context_history`, `signal_template_performance`,
  `analyst_performance`, `adaptive_policy_state`,
  `capital_deployment_state`, `config_learning_overlay`,
  `analyst_learning_digest`, `learning_event_log`, and
  `provisional_policy_state`.
- Template priors can be exported and reused through
  `src/logs/attribution/template_prior.json`.
- Learning context is budgeted and capped before it is injected back into Phase
  1.
- Optional LLM causal-review notes are stored in `reviewer_llm_notes` and
  `causal_review_candidate`; they require deterministic validation before they
  can affect adaptive policy.

The system does not automatically train a new model, change LLM providers, or
turn reviewer notes directly into trading authority.

## Quick Start

Create and activate the configured environment:

```powershell
conda env create -f environment.yml
conda activate deepfund
```

Configure credentials and local paths in `.env`, then run commands from
`src/`:

```powershell
cd D:\research\AgentQuant\src
python init_database.py
```

Run one full trading day phase by phase:

```powershell
python run\proposal.py --config config/dev.yaml --local-db --date 2025-01-02 --reset-config
python run\order.py --config config/dev.yaml --local-db --date 2025-01-02
python run\settlement.py --config config/dev.yaml --local-db --date 2025-01-02
python run\validate_phase_flow.py --config config/dev.yaml --local-db --date 2025-01-02
```

Run a replay window:

```powershell
python run\backtest.py --config config/dev.yaml --local-db --start-date 2025-01-01 --end-date 2025-01-17 --reset-config
```

Useful backtest flags:

- `--reset-config`: clears the current experiment/config state before the first
  replay trading day.
- `--skip-eval`: skips the automatic post-backtest evaluation.
- `--plot`: generates portfolio and ticker plots after the replay.
- `--plot-no-price`: skips price panels in generated plots.

Run Phase 2 in paper-loop mode for a trading date:

```powershell
python run\order.py --config config/dev.yaml --local-db --date 2025-01-02 --loop
```

## Evaluation, Attribution, And Plots

Run the current config evaluation:

```powershell
python evaluation\evaluate_config.py --config config/dev.yaml --local-db --update
```

Run completed-trade-pair attribution:

```powershell
python evaluation\analyze_strategy_attribution.py --config config/dev.yaml --local-db --start-date 2025-01-01 --end-date 2025-01-17
```

The attribution module is read-only. It matches completed open/close trade
pairs from `futures_transactions`, joins the Phase 1 recommendation snapshot,
and reports attribution by ticker, side, signal combination, trade-auditor
decision, rebalance action, and rollover category. It is useful for explaining
realized closed trades, but it is not a full account-PnL reconciler: open
positions, daily mark-to-market effects, and every raw log line remain the
responsibility of the settlement and reviewer reports.

Generate plots manually:

```powershell
python run\plot_config.py --config config/dev.yaml --output-dir logs\plots
```

## Main Outputs

| Output | Location |
| --- | --- |
| SQLite database | `src/assets/agentquant.db` |
| Main runtime logs | `src/logs/agentquant.log`, `src/logs/trade.log` |
| Daily transaction logs | `src/logs/<YYYY-MM-DD>_transaction.log` |
| Reviewer reports | `src/logs/reviewer/<run_id>/` |
| Daily summaries | `src/logs/summaries/<run_id>/` |
| Attribution reports and priors | `src/logs/attribution/` |
| Generated plots | `src/logs/plots/` or a configured output directory |

## Database Areas

Important SQLite tables include:

- Configuration and account state: `config`, `portfolio`,
  `portfolio_forced_settlement`.
- Strategy and execution: `signal`, `futures_recommendation`,
  `trading_day_phase`, `futures_transactions`,
  `futures_intraday_decision`.
- Settlement: `daily_settlement`, `ticker_daily_pnl`.
- Reviewer and learning: `reviewer_daily_report`,
  `strategy_memory_history`, `signal_context_history`,
  `signal_template_performance`, `analyst_performance`,
  `adaptive_policy_state`, `capital_deployment_state`,
  `config_learning_overlay`, `analyst_learning_digest`,
  `learning_event_log`, `provisional_policy_state`,
  `reviewer_llm_notes`, and `causal_review_candidate`.

## Tests And Checks

Focused regression checks live under `src/tests/`.  Common local checks include:

```powershell
python -m unittest src.tests.test_phase_flow_regression
python -m unittest src.tests.test_reviewer_learning
python -m unittest src.tests.test_agent_contracts
python -m unittest src.tests.test_market_confirmation
python -m unittest src.tests.test_plot_future_price_data
```

For syntax-only validation:

```powershell
python -m compileall src
```

## Project Layout

```text
AgentQuant/
  README.md
  docs/
    design_philosophy.md
  environment.yml
  pyproject.toml
  src/
    agents/
    assets/
    config/
    data/
    database/
    evaluation/
    graph/
    logs/
    run/
    tests/
    tools/
```

## Design Notes

- The project currently targets China futures only.  Other market types should
  be treated as future extensions unless their runtime path is verified in code.
- Phase 1 may produce recommendations, but it must not write actual execution
  transactions.
- Phase 2 executes recommendations; it should not create a new strategy thesis.
- Phase 3 is the source of truth for daily mark-to-market and margin state.
- Phase 4 is the source of truth for workflow validation and bounded learning
  feedback.
- Attribution explains completed trade pairs. Settlement explains account-level
  daily PnL.
