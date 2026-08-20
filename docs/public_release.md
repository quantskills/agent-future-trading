# Public release boundary

This file defines what belongs in the public repository and what remains local.

## Public

- `src/` application code and non-secret configuration catalogs.
- `README.md`, `AGENTS.md`, `docs/optimization.md`, `docs/workflow.md`, and the
  mechanism/contract documents needed to understand the architecture.
- `.env.example`, `pyproject.toml`, `environment.yml`, `.gitignore`, and `LICENSE`.
- Small synthetic fixtures and deterministic tests that do not contain private
  market data, credentials, or account history.

## Keep local

- `.env`, API credentials, token caches, `user.json`, and any provider login
  artifacts.
- `data/` (Finoview Feather files, news files, and any downloaded market data).
- `src/assets/` databases, database backups, PandaAI caches, and generated
  artifacts.
- `src/logs/`, `image/`, `Workshop/`, temporary scripts, and private reports.
- Full historical backtest ledgers, account balances, transaction-level records,
  and proprietary research outputs unless they have been anonymized and
  explicitly approved for publication.

## Redaction before publishing

Remove local absolute paths, usernames, email addresses, API endpoint secrets,
account identifiers, and provider credentials from all Markdown and fixtures.
Keep only aggregate, reproducible results with a clear data source, date range,
assumptions, costs, and limitations. This project is research software; it does
not guarantee returns and is not investment advice.
