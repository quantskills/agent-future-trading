# AgentQuant 2025-02-26 起继续回测验收清单

生成日期：2026-05-25

适用范围：继续使用当前 `exp_name=agentquant-futures-trading-2025`，从 `2025-02-26` 开始续跑。此清单只保留已经在代码层面落地、但尚未通过 2025-02-26 之后新样本充分验收的优化项。

## 1. 已回测窗口结论

### 1.1 2025-01-02 至 2025-02-09

该窗口四阶段流程已跑通，21 个结算交易日的 Phase1-4 全部 completed。

关键结果：

- 累计结算 PnL：约 `+17,130`
- 手续费：约 `1,753.91`
- 平均保证金比例：约 `3.84%`
- 峰值保证金比例：约 `5.62%`
- 盈利日/亏损日：`12/9`

结论：工程链路、账务链路、学习写回链路具备继续续跑条件，但资金利用率仍低于 6%-8% 普通确认目标。

### 1.2 2025-02-10 至 2025-02-25

该窗口 12 个结算交易日的 Phase1-4 也全部 completed，但绩效明显恶化。

关键结果：

- 累计结算 PnL：约 `-136,900`
- 手续费：约 `1,533.43`
- 平均保证金比例：约 `2.12%`
- 峰值保证金比例：约 `9.66%`
- 盈利日/亏损日：`4/7`
- 主要亏损来源：TA 约 `-120,420`

核心诊断：

1. 2025-02-13 TA long 被过度放大，主要来自泛化 protected 记忆覆盖当前信号组合。
2. 2025-02-11 至 2025-02-25 的 learned 交易表现为负，尤其 `alpha_release` 负贡献明显。
3. 风控后续开始收缩，但已经晚于主要亏损发生日。
4. 2025-02-18 曾出现 PandaAI `WinError 10048` socket/端口耗尽，最终重跑完成；该问题需要 2025-02-26 起验证缓存和共享 token 是否解决。

## 2. 已经回测验收通过的内容

以下内容已经在 2025-01-02 至 2025-02-25 的回测窗口中完成基本工程验收，后续只做常规巡检，不再作为本清单重点：

1. Phase1 / Phase2 / Phase3 / Phase4 可以完整闭环。
2. `daily_settlement`、`futures_transactions`、`futures_recommendation`、`signal_context_history`、Reviewer 学习表均能落库。
3. Artifact 外置机制已能被 `validate_artifacts.py --json` 校验；最近一次校验 `checked=4908`，缺失、hash mismatch、size mismatch 均为 0。
4. `ticker_daily_pnl` 已能分解 holding/new_position/close PnL，并用于定位 TA 主亏损。
5. Neutral accountability、causal candidate、learning event、capital deployment state 均有持续产出。
6. Phase3 会计路径严格使用官方结算价；不得使用成交价或上一结算价替代。

## 3. 2025-02-26 起仍需重点验收的优化项

| 编号 | 待验收优化 | 验收位置 | 2025-02-26 起通过标准 |
|---|---|---|---|
| V1 | PandaAI 持久化日行情缓存与共享 token | 日志、`src/assets/pandaai_market_cache.db`、Phase3 settlement | 不再因 `WinError 10048`、重复登录或端口耗尽打断 Phase；缓存只能复用官方行情，不能污染学习状态 |
| V2 | Phase3 官方结算价强约束 | Phase3 日志、`daily_settlement`、`futures_transactions.settle_price` | 当日交易必须有当日官方结算价；若取不到，应停在当日修数据链路并重跑，不能用 fallback 伪造账本 |
| V3 | 强机会泛化记忆闸门 | PM `capital_utilization_learning`、recommendation artifact | `signal_combo="*"` 或仅 ticker-side 泛化 protected 不得直接进入 `target_mode=strong_opportunity`；应记录 `specific_signal_combo=false` 或降级普通确认/试探 |
| V4 | 强机会止损/失效边界闸门 | Analyst signal、PM diagnostics、Trader exit policy | 若没有 Phase1 盘前生成的 `invalidation_level` 或 `atr_stop_distance`，不得强机会扩仓；应记录 `missing_stop_protection_for_strong_scaling` |
| V5 | learned 干预类型拆分后的真实效果 | `evaluate_config`、Reviewer learning report | 必须单独看 `alpha_release`、`risk_suppression`、`evidence_rejection`；`alpha_release` 不能继续成为主要负贡献 |
| V6 | TA 类失败路径是否被修复 | TA recommendation、TA `ticker_daily_pnl`、PM/Auditor diagnostics | 不得再出现 2025-02-13 这种泛化 protected + 无风险边界的大额 TA 扩仓；若 TA 再亏，要能区分信号、执行、止损还是市场确认问题 |
| V7 | 资金利用率改善但不盲目放大 | `daily_settlement.margin_ratio`、capital deployment state | 普通确认机会逐步接近 6%-8%；强机会接近 16%-20% 只能发生在特异证据、market confirmation、止损边界同时满足时 |
| V8 | 弱模板与浅样本防乐观 | strategy memory、adaptive policy、Auditor diagnostics | 样本数、胜率、净收益不足时必须看到 `protected_evidence_rejected`；watchlist/weak_block 不得被其它泛化 protected 覆盖 |
| V9 | 持仓生命周期与低换手 | `rebalance_summary`、Phase2 exit policy、ticker PnL 分解 | 日频分析不等于日频交易；同向趋势仓位应能继续持有或受控加仓，不能机械日内反复开平 |
| V10 | Codex GPT-5.5 reasoning effort 与 OpenRouter 移除 | `dev.yaml`、`planner.yaml`、LLM call artifact、环境变量模板 | 当前主模型保持 `provider=CodexOpenAI, model=gpt-5.5, reasoning_effort=medium`；不得因短期收益自动切换 provider/model/effort；不得再出现 OpenRouter provider、API key 或运行脚本入口 |

## 4. 每日最小检查清单

从 2025-02-26 起，每跑完一段，至少检查：

1. 每个实际交易日 Phase1-4 是否全部 completed。
2. 日志是否有 `Traceback`、未处理 `ERROR`、`database is locked`、Phase3 结算价缺失。
3. 是否还出现 PandaAI `WinError 10048`、rate limit、method not found、参数缺失 warning。
4. 当日 `daily_settlement.margin_ratio` 是否低于 6%、位于 6%-12%、还是进入 16%-20%。
5. 若出现强机会扩仓，是否同时满足当前 signal_combo 特异验证、market confirmation、止损/失效边界和组合 20% 硬闸。
6. 若没有扩仓，是否能解释为 alpha 信号不足、auditor 抑制、执行门槛、风险状态、持仓已匹配或容量约束。
7. learned 交易必须拆分干预类型，不得用混合 learned PnL 判断学习成功。
8. 对亏损品种先看 `ticker_daily_pnl.holding_pnl/new_position_pnl/close_pnl`，再判断是否需要修信号、执行或止损。
9. Artifact 外置文件可通过 `python database/validate_artifacts.py --json` 校验。
10. `agentquantcheck.db` 只能通过 `python database/build_check_db.py` 重建用于人工查看，不参与系统运行。

## 5. 续跑命令

当前目标是节省资源，先继续当前 exp 做 2025-02-26 至 2025-02-28 的 smoke 验收：

```powershell
cd D:\research\AgentQuant\src
conda activate deepfund

python run\backtest.py --config config\dev.yaml --start-date 2025-02-26 --end-date 2025-02-28 --local-db
python evaluation\evaluate_config.py --config config\dev.yaml --start-date 2025-02-26 --end-date 2025-02-28 --local-db
python evaluation\evaluate_config.py --config config\dev.yaml --start-date 2025-02-10 --end-date 2025-02-28 --local-db
python database\validate_artifacts.py --json
python database\build_check_db.py
```

注意：

- 不要使用 `--reset-config`。
- 不需要删除 `pandaai_market_cache.db`；它只缓存官方行情，不保存学习状态。
- 若 2025-02-26 至 2025-02-28 仍出现 Phase 中断、PandaAI socket 错误、官方结算价缺失或强机会无边界扩仓，应暂停扩大回测并先修代码。

## 6. 后续扩大回测判定

2025-02-26 至 2025-02-28 通过后，可以继续跑至 2025-03-31 做月度检验。若仍稳定，再考虑三个月窗口。

正式半年绩效口径应在代码冻结后使用新的 `exp_name/config_id` 从起点完整重跑；当前同一 exp 的续跑用于节省资源、验证最近修复是否奏效，不能单独作为最终半年收益能力结论。
