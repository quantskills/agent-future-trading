# AgentQuant 工作日志

本文件只记录 `.py`、`.yaml`、`.yml` 的行为或配置修改；纯文档同步、纯验证命令、分支/提交固定、数据清理不记。

==========2026年07月10日==========

（1）[PM action-value artifact] `portfolio_manager.py` 将 `canonical_action_value=False` 的 similar/fallback prior 从 `final_action_contract.learning_used.alpha_setup_action_values` 剔除，并写入 `learning_used.memory_retrieval.rejected_or_downgraded`。原因：PM 正式 action-value 主列表只能保存完整 canonical 学习证据，weak prior 只能作诊断检索材料。
（2）[PM self-check] `pm_contract_self_check.py` 增加 `alpha_setup_action_values` 纯净性检查，相关 PM 单测同步。原因：最终合约主列表出现缺 family、缺 preference、缺 lane、`canonical_action_value=False` 或 `incomplete_trace_not_for_pm_scoring` 时必须 hard fail。
（3）[PG observe action-value] `pg_system_invariants.py` 落实 `canonical_action_family=observe`、lane 为 `hold` 时空 `action_preference` 的合法语义，相关 system invariant 单测同步。原因：observe/watchlist 是观察事实和 hold 诊断线，不应被误报为缺交易动作偏向；本次未修改 PM artifact、PM self-check、SCC、action-value 写入端或交易业务逻辑。
（4）[字段矩阵命名] `pg_system_invariants.py`、`pg_pre_backtest_acceptance.py`、`pg_contract_coverage_audit.py`、`src/config/dev.yaml` 和相关测试改用 `matrix_field_semantics` 与 `docs/matrix_field_semantics.md`。原因：字段语义文档改为矩阵命名后，控制组闸门、契约覆盖和测试不能继续引用旧文件名。

(5) [matrix executable gate] Updated `pg_contract_coverage_audit.py`, `pg_pre_backtest_acceptance.py`, new `pg_pre_backtest_failure_fixtures.py`, and control CLI wrappers. Reason: matrix_chain_contract now blocks missing producer, landing, consumer, self-check, fixture, daily PG, real-path test, and mechanism-doc coverage before backtest.
(6) [daily PG boundary] Updated `pg_system_invariants.py` and tests to expose contract-only hard fail boundaries and diagnostics boundaries. Reason: daily PG remains a system contract audit and does not score PM rank, lots, direction, weak learning, legal observe diagnostics, loss days, or no-trade days.
