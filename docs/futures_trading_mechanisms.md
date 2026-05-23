# AgentQuant 期货交易机制说明

更新日期：2026-05-24

本文档按当前代码实现描述 AgentQuant 的期货交易机制。当前主线市场类型为
`china_futures`，默认实验配置为 `src/config/dev.yaml`，本地状态库为
`src/assets/agentquant.db`。

## 1. 当前系统定位

AgentQuant 现在是一个“日频策略生成 + 日内确认执行 + 日终结算 + 事后复盘学习”的
期货研究与回测系统。它不是高频交易系统，也不是完全由 LLM 自主交易的系统。

当前默认交易品种为：

`BU`、`C`、`CF`、`EB`、`HC`、`I`、`J`、`M`、`MA`、`P`、`PB`、`RB`、`SR`、`TA`、`ZN`。

系统的关键现状：

- 只对 `china_futures` 路径做了完整主线开发。
- SQLite 本地库是当前回测、审计、结算、学习闭环的主要状态源。
- Phase 1 可以使用 LLM 做证据总结和组合经理综合判断，但交易前后会经过确定性控制。
- Phase 2、Phase 3 和主审计路径是确定性的，不应重新生成策略观点。
- Reviewer 可选调用 LLM 写因果复盘笔记，但这些笔记不会直接变成交易规则，必须经过确定性规则校验。
- 系统采用了类似 A2A 的本地 artifact contract 思路，但没有迁移到 A2A runtime。

## 2. 四阶段交易日流程

| 阶段 | 入口 | 主要职责 | 关键输出 |
| --- | --- | --- | --- |
| Phase 1 proposal | `run/proposal.py` | 读取交易日前可用证据，生成策略推荐 | `futures_recommendation`、`signal`、Phase 1 artifact |
| Phase 2 order | `run/order.py` | 处理换月和策略推荐，做日内确认并执行 | `futures_transactions`、`futures_intraday_decision`、`phase2_order_plan` |
| Phase 3 settlement | `run/settlement.py` | 日终逐日盯市、手续费、保证金和持仓状态更新 | `daily_settlement`、`ticker_daily_pnl`、official `portfolio` |
| Phase 4 review | `run/validate_phase_flow.py` | 校验四阶段一致性，写复盘和学习反馈 | reviewer 报告、daily summary、learning tables |

`run/backtest.py` 会按交易日循环执行四个阶段。它会检查
`trading_day_phase`，已经完成的阶段会跳过；使用 `--reset-config` 时会在窗口第一天重置当前实验状态。
回测窗口结束后默认执行 `evaluation/evaluate_config.py --update`，除非指定 `--skip-eval`。

## 3. Phase 1：策略生成

Phase 1 的核心是 `graph/workflow.py` 中的 LangGraph 工作流。默认配置下
`planner_mode: false`，因此会直接运行配置中的分析师：

- `commodity_news_analyst`：读取交易日前可用的商品新闻和文本证据。
- `fundamental_analyst`：读取 PandaAI 与 Finoview 基本面数据，生成供应、需求、库存、成本、进出口、基差等证据摘要。
- `technical_analyst`：读取价格、技术指标和趋势结构。

这些分析结果进入 `portfolio_manager`。组合经理会综合：

- 分析师信号与适用品种配置；
- PandaAI 价格、主力合约和额外日内/日频数据；
- Finoview 无前视快照和覆盖率；
- market confirmation；
- trade auditor 结果；
- 当前账户、保证金、持仓和风险状态；
- strategy memory、adaptive policy、provisional policy、template prior；
- 品种、板块和净敞口约束。

Phase 1 只生成推荐，不应写入真实成交。推荐落在
`futures_recommendation`，其中包含目标方向、目标手数、预期保证金、信号组合、审计结果、
`pre_open_plan`、`rebalance_summary`、`strategy_controls`、证据快照等字段。

## 4. 本地 Artifact Contract

当前系统使用本地 artifact contract 来约束代理输出。关键字段包括：

- `contract_version`
- `agent_name`
- `trading_date`
- `ticker`
- `config_id`
- `data_cutoff`
- `no_lookahead_status`
- `determinism_mode`
- `source_artifacts`
- `validation_errors`

默认快照合约版本为 `agentquant.snapshot.v2`。这套机制用于提高可审计性和可回放性，
不是外部 A2A 协议迁移。

## 5. Trade Auditor 与风险控制

`agents/auditor.py` 是确定性交易闸门。它不会决定最终下单价格，也不会自行生成策略观点；
它只对 Phase 1 的候选操作给出交易许可和缩放建议。

常见审计结果：

- `allow`：允许按控制后的目标执行。
- `scale_down`：允许但降低规模。
- `probe_only`：只允许试探性仓位。
- `reduce_only`：只允许减仓，不允许新增风险。
- `block`：阻止交易。

审计考虑的因素包括信号组合、基本面质量、market confirmation、账户回撤、近期品种表现、
strategy memory、adaptive policy、provisional policy、冷启动规则、业务质量门槛和净敞口约束。

默认配置中还有资金使用控制：

- 目标保证金使用率区间约为 16% 到 20%。
- 常规缩放后保证金使用率上限约为 20%。
- 配置层仍保留组合总保证金硬上限。
- 低资金使用率不会强制系统盲目加仓，而会被 reviewer 记录并进入
  `capital_deployment_state` 等学习状态。

## 6. Phase 2：换月、日内确认与成交

Phase 2 入口是 `agents/trader.py`。它要求同一交易日 Phase 1 已完成，并且当前 Phase 2
尚未完成。

执行顺序：

1. 读取最新 settled portfolio。
2. 先处理 pending rollover recommendations。
3. 在 `rollover.mode: reconcile_with_strategy` 下，把换月需求与策略目标合并。
4. 再处理普通策略推荐。
5. 根据目标手数、当前持仓和风险控制生成订单计划。
6. 使用日内数据确认是否触发。
7. 写入成交、日内决策和 Phase 2 日志。

默认日内确认参数包括：

- 15 分钟决策频率；
- 1 分钟执行频率；
- 开盘区间约 30 分钟；
- 最低成交量要求；
- 最大追价比例；
- `finalize_after` 到时后结束未触发推荐。

在回测 replay 模式下，系统会使用当日可回放数据完成最终判定；在 `--loop` 模式下，
Phase 2 会按配置间隔轮询，适合纸面盘或模拟运行。

## 7. Phase 3：结算与逐日盯市

Phase 3 入口是 `agents/accountant.py`。它要求同一交易日 Phase 2 已完成，并且 Phase 3
尚未完成。

结算模块负责：

- 读取当日成交；
- 用结算价进行逐日盯市；
- 计算每个品种的日 PnL；
- 计算手续费；
- 更新当前保证金、权益、可用资金和风险比例；
- 标记成交是否已经入账；
- 写入 official portfolio state。

核心表：

- `daily_settlement`
- `ticker_daily_pnl`
- `portfolio`
- `futures_transactions.booked_in_settlement`

因此，账户级每日收益应以 Phase 3 结算结果为准，而不是只看成交配对归因。

## 8. Phase 4：审计、日志与学习闭环

Phase 4 入口是 `agents/reviewer.py` 和 `tools/agent_tools/reviewer_tools.py`。它会检查：

- Phase 1 是否没有写入成交；
- Phase 2/Phase 3 是否按顺序完成；
- 推荐、订单计划、实际成交是否一致；
- settlement row 是否存在；
- portfolio date 是否正确；
- 手续费是否匹配；
- 是否存在未入账成交；
- 零成交日是否有合理原因；
- artifact contract 是否通过；
- capital utilization、neutral accountability、learning overlay 是否可解释。

输出位置：

- 每日成交日志：`src/logs/<YYYY-MM-DD>_transaction.log`
- reviewer JSON/Markdown：`src/logs/reviewer/<run_id>/`
- daily summary：`src/logs/summaries/<run_id>/`
- attribution/template prior：`src/logs/attribution/`

学习相关表包括：

- `strategy_memory_history`
- `signal_context_history`
- `signal_template_performance`
- `analyst_performance`
- `adaptive_policy_state`
- `capital_deployment_state`
- `config_learning_overlay`
- `analyst_learning_digest`
- `learning_event_log`
- `provisional_policy_state`
- `reviewer_llm_notes`
- `causal_review_candidate`

Reviewer 的学习反馈是“受控输入”，不是自由改写交易逻辑。候选规则需要经过确定性校验后，
才可能进入 adaptive policy。

## 9. 无前视规则

当前代码在多个位置约束前视问题：

- Phase 1 使用交易日前可用数据，Finoview 快照会应用 `release_lag_days` 和交易日 cutoff。
- PandaAI 日频与主力合约数据通过交易日和参考价接口读取。
- 新闻分析只应读取配置 cutoff 前可见的文本。
- Phase 2 可以使用当日日内数据，但只用于执行确认，不用于重写 Phase 1 策略观点。
- Phase 3 与 Phase 4 是事后阶段，它们的结果只进入结算和下一期学习。

夜盘数据的交易日归属由数据源和合约日历决定。代码层面应以 provider 返回的
`trading_date` 和项目 trading calendar 为准。

## 10. 常用命令

以下命令默认从 `D:\research\AgentQuant\src` 目录运行。

初始化数据库：

```powershell
python init_database.py
```

单日四阶段：

```powershell
python run\proposal.py --config config/dev.yaml --local-db --date 2025-01-02 --reset-config
python run\order.py --config config/dev.yaml --local-db --date 2025-01-02
python run\settlement.py --config config/dev.yaml --local-db --date 2025-01-02
python run\validate_phase_flow.py --config config/dev.yaml --local-db --date 2025-01-02
```

窗口回测：

```powershell
python run\backtest.py --config config/dev.yaml --local-db --start-date 2025-01-01 --end-date 2025-01-17 --reset-config
```

回测常用参数：

- `--reset-config`：窗口第一天重置当前实验状态。
- `--skip-eval`：跳过回测后的自动评估。
- `--plot`：回测后生成图表。
- `--plot-no-price`：图表不绘制价格面板。

Phase 2 纸面盘循环：

```powershell
python run\order.py --config config/dev.yaml --local-db --date 2025-01-02 --loop
```

评估当前配置：

```powershell
python evaluation\evaluate_config.py --config config/dev.yaml --local-db --update
```

成交配对归因：

```powershell
python evaluation\analyze_strategy_attribution.py --config config/dev.yaml --local-db --start-date 2025-01-01 --end-date 2025-01-17
```

生成图表：

```powershell
python run\plot_config.py --config config/dev.yaml --output-dir logs\plots
```

## 11. 归因模块的能力边界

`evaluation/analyze_strategy_attribution.py` 当前是只读分析脚本。它会：

- 从 `futures_transactions` 中按 FIFO 形成已完成开平仓配对；
- 关联 Phase 1 的 recommendation snapshot；
- 按品种、方向、信号组合、trade auditor、rebalance action、换月类别等维度汇总；
- 输出弱方向建议和已实现交易归因报告；
- 可读取 `agentquant.db` 中的推荐、成交和结算相关信息。

它不等价于完整账户收益归因：

- 未平仓持仓不会形成 completed pair；
- 日终逐日盯市收益以 Phase 3 settlement 为准；
- 保证金、权益曲线和每日账户级 PnL 应看 `daily_settlement` 与 `ticker_daily_pnl`；
- 原始 `logs/` 文件提供审计上下文，但归因脚本的主数据源仍是 SQLite 表。

因此，完整复盘建议同时查看：

1. `evaluation/evaluate_config.py` 的账户级评估；
2. `evaluation/analyze_strategy_attribution.py` 的已完成交易配对归因；
3. `src/logs/reviewer/<run_id>/` 的 reviewer 报告；
4. `src/logs/<YYYY-MM-DD>_transaction.log` 的日成交审计；
5. `daily_settlement` 与 `ticker_daily_pnl` 的结算结果。

## 12. 当前边界和开发注意事项

- 当前主线只应按 `china_futures` 理解。
- Phase 1 是策略生成阶段，不能写真实成交。
- Phase 2 是执行阶段，不能临时创造新的策略观点。
- Phase 3 是账户级 PnL、保证金和持仓的结算真相源。
- Phase 4 是审计和受控学习入口，不是自由策略改写器。
- A2A 相关配置只代表本地 artifact contract 设计取向。
- LLM 因果复盘笔记必须经过确定性验证才能影响 adaptive policy。
- Attribution 解释已完成交易对，settlement 解释账户级每日收益。
