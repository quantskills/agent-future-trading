# AgentQuant 优化回测验收清单

更新时间：2026-05-30

本文档只保留“已经代码落地，但还没有经过下一轮干净回测验收”的项目。已经稳定验收过的机制不在这里展开；机制说明见 `mechanism_mutiagents.md`、`mechanism_data_model.md`、`mechanism_research.md`。

## 一、待回测验收项目

| 编号 | 待验收项目 | 验收重点 |
|---|---|---|
| V1 | 四阶段与多智能体边界 | Phase1-4 全部完成；Reviewer 只做流程、账务、日志验收；Researcher 写研究与记忆；Trader/Accountant 不被 LLM 或学习越权 |
| V2 | 完整交易日志 | 每个交易日生成 `src/logs/<交易日>_transaction.log`，格式和信息密度对齐模板，且 Phase4 终态正确 |
| V3 | 数据质量摘要与数据依据 | `src/logs/data_quality/<交易日>.json` 生成；推荐与记忆含数据可用性、滞后性、字段依据 |
| V4 | 无未来数据污染 | Phase1 只用盘前可见数据；Phase2 只用当时盘中数据；Phase4 shadow 与学习只影响未来交易日 |
| V5 | 模型调用审计 | Analyst、PM、Researcher 的 artifact 可追踪 provider/model/reasoning effort；Reviewer、Trader、Accountant 无 LLM 越权 |
| V6 | 回测加速机制 | 多品种分析并行、预取、缓存、LLM 并发门生效；无数据库锁、重复信号、缺失信号、日期错位 |
| V6a | Phase4 signal 完整性验收 | daily summary 写入 `extra_audit.signal_persistence`；每个品种应有三位分析师最终信号；重复/缺失会被 Reviewer 拦截 |
| V7 | 内部消息契约 | Analyst signal、PM snapshot、推荐、artifact 具备 agent/date/ticker/data_cutoff/no_lookahead/source/validation 等关键字段 |
| V8 | 交易研究契约 | 分析师与 PM snapshot 含机会类型、机会层级、入场触发、退出提示、持仓周期、因子关注面、证据冲突 |
| V9 | 下一轮策略更新契约 | 交易记忆、未交易机会、探索假设、策略状态含 `next_round_memory_contract`；后续 prompt 可读到下一轮策略更新 |
| V10 | 真实交易与未交易机会记忆 | `trade_episode_memory`、`no_trade_opportunity_memory`、`exploratory_hypothesis`、`learning_event_log` 按 Phase4 写入 |
| V11 | no-trade 与 Neutral shadow | no-trade、涨跌停错失成交 1 手影子和 Neutral 后续窗口只在未来已结算日期回填，不进入真实账务；no-trade payload 写入信号/风控/择时/执行/业务/学习六类 |
| V12 | 候选假设边界 | candidate hypothesis 只能作先验、观察或 probe，不支撑放仓、加仓、`position_matched` 或亏损仓硬扛 |
| V13 | 成熟经验落仓 | protected/deployable/alpha promotion 只有在当日证据、market confirmation、失效边界和 Auditor 通过时影响仓位 |
| V14 | learned vs unlearned | learned 交易不应长期显著跑输 unlearned；若跑输，同作用域 demote 应出现并反映到后续仓位 |
| V15 | tail-loss sentinel | 新仓首日/次日异常亏损后短期 cap/probe；不变成品种黑名单，不阻断必要减仓、平仓或换约 |
| V16 | 新仓亏损再验证与 horizon 一致性 | 新仓快速亏损需当日证据复核；中期基本面不能单独触发短线新仓、加仓或亏损持有 |
| V17 | alpha release 与资金利用 | 普通机会可接近 6%-8%；强 alpha 才可接近 16%-20%；不得硬拉 weak/watchlist 仓位 |
| V18 | 回撤保护与恢复 | 4%/5% 回撤场景下 warning、hard protection、cooling、recovery probe 按配置生效 |
| V19 | PandaAI/Finoview 缺口降级 | 可选缺口不中断；关键缺口降级为小仓、观察或 Neutral，不能包装成方向证据 |
| V20 | 评估与画图稳定 | `evaluate_config.py` 整体/区间评估正常；`plot_config.py` 只输出组合净值图和有交易品种图 |
| V21 | 收益与学习有效性 | 清库重跑后观察收益曲线、learned/unlearned、alpha promotion、tail-loss、资金利用率是否改善 |
| V22 | 学习弱参 | `memory_expires_after_days`、`overlay_expires_after_days`、`provisional_policy_state.valid_days`、`exploratory_research.valid_days`、`tail_loss_sentinel.valid_days`、`alpha_promotion.valid_days`、shadow windows、prompt budget 是否需要调整 |
| V23 | 实战化交易业务机制 | 涨跌停成交保护与 Phase3 复核、动态保证金回退、换约成本审计、临近交割新仓保护、未成交原因完整性和数据调用缓存需要通过回测验收 |
| V24 | 情境化弱参校准 | `contextual_rule_calibration` 是否按品种/方向/周期/市场状态写入并读取；PM、Auditor、Trader 只校准软阈值，不删除规则、不突破 20% 上限和硬业务约束 |
| V25 | 可能过硬规则校验 | 回测检查低 tradeability/stale fundamental Neutral、Auditor 历史硬拦截、新仓亏损再验证、horizon 一致性、盘中触发、板块最短持仓日是否过度压制交易 |
| V26 | 情境校准写入来源 | Phase4 是否从 no-trade shadow、PM lifecycle/horizon 诊断、分析师表现写入 `contextual_rule_calibration:*`；只影响未来交易日，payload 含证据、调整边界和失效日期 |
| V27 | 情境校准作用域与过期 | 校准是否严格按 ticker/side/horizon/market_regime 匹配；过期后自动失效；无匹配行时不出现全局泛化放松 |
| V28 | 三类执行边界 | PM 只校准持仓生命周期/horizon 软阈值；Auditor 只软化 allowlist 中历史表现类拦截；Trader 只微调 protected/deployable 的盘中 fallback，不绕过业务硬约束 |
| V29 | 技术参数情境校准 | Researcher 是否从已结算 technical 短周期表现写入 `contextual_rule_calibration:technical_parameters`；Technical Analyst 是否只小幅校准 EMA/RSI/Bollinger，并把 `adaptive_params` 与 `technical_parameter_calibration` 写入 metadata；不得直接放仓或压死交易 |

## 二、每段回测后快速检查

1. 四阶段状态、Traceback、数据库锁、LLM/PandaAI 错误。
2. 交易流水、结算、手续费、保证金、持仓、账户权益是否对账。
3. 完整交易日志、data quality JSON、daily summary 是否按日生成。
4. 推荐快照与 signal 表是否每个品种都有三位分析师最终信号，且无重复。
5. PM/Auditor diagnostics 是否能解释交易、no-trade、降权、晋升、回撤和资金利用。
6. 学习表是否写入，prompt 是否读取，候选记忆与成熟记忆是否越权。
7. learned vs unlearned、Neutral、no-trade shadow、资金利用率和收益曲线是否改善。
8. 限价、合约详情和保证金数据是否复用缓存；未成交推荐是否有 `limit_locked_no_fill` 或 `near_expiry_new_entry_block` 等可解释原因。
9. `limit_locked_no_fill` 是否写入未交易机会记忆，并在下一轮策略更新契约中作为择时/执行价研究样本，而不是直接放仓依据。
10. no-trade 原因是否能按“信号/风控/择时/执行/业务/学习”六类在 payload、learning event 和 Phase4 日志中追踪。
11. 情境校准是否只在同作用域成熟/影子/分析师表现证据下生效；若无校准行，原规则是否保持不变。
12. 若出现低交易频率或连续 no-trade，分别检查是信号质量、Auditor、PM lifecycle/horizon、Trader 盘中触发还是业务硬规则导致，避免把所有停交易都误判成风控写死。
13. `adaptive_policy_state` 中 `contextual_rule_calibration:*` 行是否来自已结算或已关闭的样本，不污染当日 Phase1/Phase2 决策。
14. 校准生效时，PM diagnostics、Auditor 决策、Trader intraday features 是否能看到对应的 `contextual_rule_calibration` 证据，方便复盘判断是否真的落到仓位。
15. 技术参数校准生效时，Technical Analyst 的信号质量、Neutral 比例和后续仓位是否改善；若出现低交易频率、参数滞留或过拟合，应检查 `technical_min_confidence`、`technical_positive_hit_rate`、`technical_weak_hit_rate`、`technical_valid_days`。

## 三、建议验收顺序

1. 清库后先跑小窗口 `2025-01-02` 至 `2025-01-10`。
2. 小窗口通过后跑完整月度窗口。
3. 每个窗口结束后运行：

```powershell
cd D:\research\AgentQuant\src
python run\evaluate_config.py --config config\dev.yaml --local-db --update
python run\plot_config.py --config config\dev.yaml
```
