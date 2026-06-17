# AgentQuant Local Baseline - 2026-06-17

Baseline tag: `v2026.06.17-unique-contract-baseline`

This is a local development baseline for continuing AgentQuant work from a known-good system state. It is intended to stay local unless explicitly pushed later.

## Scope

Included in this baseline:

- Source code under `src/`
- Centralized prompts under `src/llm/`
- Configuration under `src/config/`
- Regression tests under `src/tests/`
- Project docs under `docs/`
- Root project docs and metadata such as `README.md`, `AGENTS.md`, `test_all.py`, `environment.yml`, `pyproject.toml`
- Local analyst data under `data/Fundamental_data/Finoview_data/`

Not included as a strategy conclusion:

- Backtest profitability
- Deleted or missing backtest database records
- Logs, runtime SQLite assets, cache files, and ignored local artifacts

## Runtime Boundary Fixed By This Baseline

The current system boundary is:

`Analyst evidence -> PM final_action_contract -> Auditor audit -> Trader execution -> Accountant settlement -> Reviewer/Researcher learning`

The runtime contract rules are:

- Analysts output structured evidence and learning calibration; they do not output lots or final trade authority.
- PM is the only strategy agent that turns evidence and learning into `final_action_contract`.
- Auditor reviews the final contract deterministically.
- Trader only executes the audited `final_action_contract` with intraday data.
- Trader does not read PM drafts, `pre_open_plan`, or research `execution_action_value` directly.
- Research action-value uses the fixed preference vocabulary:
  - `positive_candidate_open`
  - `positive_candidate_hold`
  - `positive_candidate_exit`
  - `positive_candidate_execution`
  - `negative_revalidate`
  - `negative_hold_revalidate`
  - `tail_loss_protect`
- `probe` is an exploration authority or position form, not a separate action-value lane.
- `pre_open_plan` remains PM internal draft/log context only, not a downstream trade truth.

## Verification Completed Before Baseline

Executed with local `deepfund` Python:

- `python -m compileall -q src\llm\prompt.py src\tests\test_protocol_governor.py`
- `python -m unittest -q src.tests.test_protocol_governor`
  - Result: 22 tests passed.
- PM/Trader/system-invariant/protocol target group
  - Result: 169 tests passed.
- `src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --start-date 2025-03-03 --end-date 2025-03-31 --check-llm-auth --json`
  - Result: `ok=true`, `decision=ready_for_strategy_backtest`.
  - Warning: `sqlite_missing`, expected because backtest DB records have been deleted.
- `src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json`
  - Result: `ok=true`.
  - Warning: `sqlite_missing`, expected because backtest DB records have been deleted.
- Full deterministic suite:
  - `python -m unittest -q`
  - Result: 445 tests passed.

## Static Scan Result

Current docs/config/prompt/protocol paths were scanned for stale runtime vocabulary. No current runtime-path match remains for:

- `positive_candidate_add`
- `positive_candidate_scale`
- `audited_final_action_contract`
- `controlled_open_or_add`
- `controlled_probe_or_hold`
- `cap_reduce_or_revalidate`
- `observe_or_probe`

Historical mentions may remain in `docs/work_log.md` because that file preserves the actual change timeline.

## How To Return To This Baseline Locally

After the local tag is created:

```powershell
git checkout v2026.06.17-unique-contract-baseline
```

To continue new work from this baseline:

```powershell
git checkout -b work/next-iteration v2026.06.17-unique-contract-baseline
```

Do not push this tag unless explicitly intended:

```powershell
git push origin v2026.06.17-unique-contract-baseline
```
