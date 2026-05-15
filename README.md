# AgentQuant

AgentQuant is a multi-agent China futures strategy generation, backtesting, paper-trading, settlement, and audit system.

The system currently targets 15 futures underlyings:

```text
BU, C, CF, EB, HC, I, J, M, MA, P, PB, RB, SR, TA, ZN
```

## Core Capabilities

AgentQuant has two primary runtime capabilities:

1. Historical backtesting
   The system replays trading days through a strict four-phase workflow: strategy generation, execution, settlement, and validation.

2. Paper trading / live simulation
   The same daily strategy workflow can be run against current data. Phase2 can keep running with `--loop` so intraday execution confirmation happens inside one Phase2 run instead of repeatedly restarting `order.py`.

## Runtime Model

AgentQuant uses a four-phase daily workflow.

| Phase | Script | Responsibility |
|-------|--------|----------------|
| Phase 1 | `run/proposal.py` | Generate analyst signals and save pre-open futures recommendations |
| Phase 2 | `run/order.py` | CLI runner for the `trader` agent, which translates recommendations into executable trades |
| Phase 3 | `run/settlement.py` | CLI runner for the `accountant` agent, which runs settlement, PnL, margin, and official portfolio persistence |
| Phase 4 | `run/validate_phase_flow.py` | Validate phase status, recommendation audit, transactions, settlement, and accounting |

Phase boundaries are intentional:

- Phase1 must not write real transactions.
- Phase2 is the only normal phase that writes futures transactions.
- Phase3 is the only phase that finalizes settlement and official portfolio state.
- Phase4 validates the full accounting and audit trail.

## Data Sources

AgentQuant currently uses:

- PandaAI for futures daily quotes, minute bars, main-contract quotes, settlement-related market data, and futures derivative confirmation data.
- Local Finoview feather files for futures fundamental data.
- Local futures news text files for news/event analysis.

Key local data locations:

```text
data/Fundamental_data/Finoview_data/
data/News_data/Future_news/
```

## Analysts and Trade Auditor

The Phase1 strategy is generated from three analyst agents:

- `technical`
- `fundamental`
- `commodity_news`

Their signals are aggregated by the portfolio manager. The deterministic trade gate is now referred to as `trade_auditor`; the old `decision_planner` name remains backward compatible in code and historical audit payloads.

`trade_auditor` does not call an LLM. It uses market confirmation, signal combinations, recent ticker-side performance, and conditional trade-pair feedback to allow, reduce, block, or hold proposed exposure.

The concrete implementation lives in `src/agents/auditor.py`. `src/agents/planner.py` is kept only for the legacy LLM analyst selector controlled by `planner_mode`.

## Intraday Execution Confirmation

The system still generates daily-frequency strategies. Intraday data is used only to improve execution timing.

The concrete Phase2 implementation now lives in `src/agents/trader.py`. The script `src/run/order.py` is kept as the stable command-line entrypoint used by manual runs and `run/backtest.py`.

When `execution.intraday_confirmation.enabled` is true:

- New or increasing exposure is gated by completed 15-minute bars.
- Long entries require confirmation above VWAP and the opening range.
- Short entries require confirmation below VWAP and the opening range.
- The execution base price is the next valid 1-minute bar open.
- Close/reduce/rollover actions use the first valid 1-minute execution basis more aggressively.
- Untriggered recommendations receive explicit no-trade reasons such as `intraday_trigger_not_met`.

The intraday execution audit is stored in:

```text
futures_intraday_decision
```

## Daily Settlement

The concrete Phase3 implementation now lives in `src/agents/accountant.py`. The script `src/run/settlement.py` is kept as the stable command-line entrypoint used by manual runs and `run/backtest.py`.

The accountant agent does not call an LLM. It uses `src/tools/agent_tools/futures_settlement.py` to replay Phase2 transactions, fetch settlement prices, persist the official portfolio, write `daily_settlement`, and mark Phase2 transactions as booked.

## Quick Start

Run commands from `src/`.

Initialize the SQLite database:

```bash
python database/sqlite_setup.py
```

Run one full trading day:

```bash
python run/proposal.py --config config/dev.yaml --trading-date 2025-01-06 --local-db
python run/order.py --config config/dev.yaml --trading-date 2025-01-06 --local-db
python run/settlement.py --config config/dev.yaml --trading-date 2025-01-06 --local-db
python run/validate_phase_flow.py --config config/dev.yaml --trading-date 2025-01-06 --local-db
```

Run one paper-trading day with intraday execution confirmation:

```bash
python run/proposal.py --config config/dev.yaml --trading-date YYYY-MM-DD --local-db
python run/order.py --config config/dev.yaml --trading-date YYYY-MM-DD --local-db --loop
python run/settlement.py --config config/dev.yaml --trading-date YYYY-MM-DD --local-db
python run/validate_phase_flow.py --config config/dev.yaml --trading-date YYYY-MM-DD --local-db
```

Run a multi-day backtest:

```bash
python run/backtest.py --config config/dev.yaml --start-date 2025-01-01 --end-date 2025-02-28 --local-db
```

Run a small smoke backtest before a larger window:

```bash
python run/backtest.py --config config/dev.yaml --start-date 2025-01-06 --end-date 2025-01-10 --local-db
```

## Database

The default SQLite database is:

```text
src/assets/agentquant.db
```

Important tables:

- `config`
- `portfolio`
- `futures_recommendation`
- `futures_transactions`
- `futures_intraday_decision`
- `daily_settlement`
- `ticker_daily_pnl`
- `trading_day_phase`

## Logging and Outputs

Main outputs are written under:

```text
src/logs/
```

Validation summaries are written under:

```text
src/logs/summaries/<run_id>/
```

## Tests

The project environment may not include `pytest`, so the maintained regression path uses standard `unittest`:

```bash
python -m unittest src.tests.test_pandaai_api_adapter src.tests.test_phase_flow_regression
```

## Project Structure

```text
src/
  agents/
  apis/
  config/
  database/
  evaluation/
  graph/
  run/
  tests/
  tools/
  util/
docs/
data/
```

## Design Notes

- AgentQuant is not a high-frequency trading system.
- Strategies are generated at daily frequency.
- Minute data is used for execution confirmation and better price selection.
- All normal trading activity must remain auditable through the four-phase workflow.
