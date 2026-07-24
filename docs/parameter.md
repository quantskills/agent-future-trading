# 参数调节备忘录

本文记录 AgentQuant 的参数边界、唯一全市场 rank 分数制度，以及回测后允许微调的观察口径。它不是立即改参数清单；所有微调必须基于干净回测证据。

本轮重点回测样本区间固定为：

```text
2025-03-25 至 2025-05-31
```

样本不足前，不允许因为单日或少数品种盈亏直接改参数。至少 30 个交易日后才允许做第一轮复核；至少 40 个交易日后才允许微调 rank 分数权重和学习修正强度。

## 不动的账户硬边界与计划预算口径

- 账户硬边界：`max_total_margin_ratio = 0.20`、`position_budget_policy.hard_max_total_margin_ratio = 0.20`，组合总保证金不得突破 20%。
- PM 签约/部署阶段单品种资金硬约束：`position_budget_policy.max_single_ticker_margin_ratio`。它约束 PM 计划和签约，不作为 Reviewer/PG 日终收益审判线。
- PM 计划预算参数：`max_net_exposure`、`strong_opportunity_max_net_exposure`、`target_margin_ratio_*`、`probe_margin_ratio`、`probe_margin_max_ratio`、`normal/deployable/exceptional_*`、`warning_target_margin_ratio_max`、`recovery_*`。这些参数服务 PM Step5 rank、资金部署、资金层级和复盘归因；真实成交后因条件腿未触发、成交子集、价格变化或滑点产生偏离时，只进入 Reviewer `budget_drift_diagnostics`、warning、事实归因和 Researcher input，不作为 Phase4 hard fail 或 PG 交易语义复判。
- `probe_margin_ratio / min_real_trade_margin_ratio = 0.008`：小探针计划资金层级和真实交易最小保证金门槛不因 rank 高低自动提高。
- 现有 probe / normal / strong 计划资金层级参数不因短样本调整。
- 手续费、滑点、合约乘数、保证金率属于交易事实，不因策略表现调参。
- PM、Auditor、Trader、Accountant、Researcher 边界不因调参改变。
- hard fail 只用于非策略错误，不能为改善收益而降级。

## 唯一全市场 rank 分数制度

唯一 `opportunity_rank` 只用于实际增加风险的资金排序，包括从空仓建立新仓，以及同方向扩大绝对手数的 `add/scale`。`rank=1` 永远表示当天全市场最值得优先占用新增风险资金的产品机会。

基础公式：

```text
rank_score =
  冷启动证据质量分
+ 资金层级资格分
+ 生命周期 open/add action-value 学习修正分
+ 产品/setup/trigger 历史收益修正分
+ 当前 trigger 质量修正分
+ 资金效率小修正
- 冲突/风险/失效边界惩罚
```

上述七项只求和一次，不做 `[0,1]` 截断。`rank_score` 可以为负；负分不禁止交易或 probe，只保留候选之间的真实投资价值大小关系。

当前代码映射：

| 分项 | 字段 | 当前作用 |
|---|---|---|
| 冷启动证据质量 | `rank_score_components.cold_start_evidence_quality` | 无学习或学习样本少时的主排序依据 |
| 资金层级资格 | `capital_layer_priority` | 固定`alpha_scale=6.0`、`real_budget=3.0`、`exploration_probe=0.0`；只进入唯一总分一次，并以大于其余六项合法总跨度的分带严格保证scale > real > probe |
| open/add 学习 | `open_add_action_value_delta` | 正向学习提高 rank，负向/tail/entry loss 降低 rank |
| 产品/setup/trigger 历史表现 | `product_setup_trigger_history` | alpha profile 对同类机会的加减分 |
| 当前 trigger 质量 | `trigger_execution_quality` | 固定`current_trigger_quality_weight=0.08`，只使用PM由已验证SCC重建的当日technical/event `trigger_quality_score`；历史trigger结果和execution/profile学习不进入该分项 |
| 资金效率 | `capital_efficiency` | 预留小权重，40 日样本后再评估是否启用 |
| 冲突/风险/失效边界 | `conflict_risk_invalidation_penalty` | 冲突、数据缺口、风险和失效边界不足的扣分 |

权重配置入口固定为 `src/config/rank_score_policy.yaml`，由 `dev.yaml.config_catalogs.rank_score_policy` 引入并在运行时展开为 `rank_score_policy`。该 catalog 只允许微调 `rank_score` 权重和资金效率小修正，不允许改仓位参数、交易权限、`0.008` probe、`20%` 总保证金硬边界或 `0.5` 净敞口计划预算。样本不足 40 个干净交易日前，不应调整该 catalog。

失效字段不是可调参数：`invalidation_level+canonical invalidation_condition+valid_until`只定义首次成交前作废；`position_invalidation_level/atr_stop_distance/exit_hint/expected_horizon_days`只定义成交后由下一交易日PM消费的持仓依据。两组字段不得通过YAML互相替代。

`atr_stop_distance`固定为technical使用开仓前已完成OHLC按既有True Range/EWM口径确定性生成的原始ATR14，不是LLM输出，也不预乘止损倍数。下一交易日PM以真实开仓价为锚，乘`execution_exit_policy`当前真实命中的default/sector `atr_multiplier`后判断止损；本轮不修改任何倍数。现有template/setup覆盖只有setup键与catalog键精确相等时才生效，不能把未命中的覆盖值解释为已经参与交易。

资金部署规则：

- 从空仓建立非零仓位，以及同方向且 `abs(target_lots)>abs(current_lots)` 的 `add/scale` 进入 1-N 排名；`wait/hold/reduce/exit`、当前反转退出腿和不增加风险的条件监控没有 rank。
- 固定按唯一 `rank_score` 降序、再按标准化 `ticker` 排出 1-N；资金层、证据、学习、trigger、资金效率和风险已经各自进入总分一次，不再形成第二套排序。
- `rank=1` 只表示最值得占用资金，不自动升仓。
- `candidate_quality` 由唯一scorecard按 `opportunity_score + 0.04*trigger_valid + 0.04*invalidation_present` 计算并限制在 `[0,1]`；setup、学习/profile和冲突已经进入 `opportunity_score`，不得再次叠加。该限制只服务Step4比例，不适用于可为负的rank。
- Step4 按最终 `candidate_quality` 在现有区间内连续形成计划比例：probe `0.008-0.015`、real `0.030-0.060`、scale `0.060-0.120`、exceptional `0.075-0.130`。
- 正式 canonical open/add 正向学习与完整当日证据共同成立时才允许由 probe 升为 real；成熟重复正收益、强确认、失效边界完整且没有中期基本面反向时才允许进入 scale。
- Step5 不升层、不重算 `candidate_quality`，只用唯一 rank 顺序消费预算。
- Step4层内保证金比例是唯一软仓位计划；旧 `ticker_performance_control` 不再作为运行参数。`risk_control.max_single_position_ratio`只保留为Step4前名义风险锚，Step4后只能由可用保证金、单品种/总保证金硬线、净敞口、最小手数和手数取整收缩。
- 资金按 rank 顺序逐个占用 PM 计划预算；触及总保证金硬边界、单品种签约/部署约束或净敞口计划预算后，后续候选还原为 wait/hold，并写入 `no_rank_or_budget_no_new_exposure`。

## 30 个交易日后复核

至少 30 个干净交易日记录后，允许复核：

- 冷启动证据质量在 rank 中是否过强或过弱。
- `watch_for_trigger` 与 `probe_candidate` 的排序差异是否合理。
- entry trigger、invalidation、market confirmation 对 rank 的贡献是否方向正确。
- exploration_probe 层 rank=1 平均新仓收益是否高于低 rank。
- rank 诊断是否只比较同资金层、同生命周期，不混入 hold/reduce/exit。

30 日复核只判断机制是否错位，原则上不做大幅调参。

## 40 个交易日后微调

至少 40 个干净交易日记录后，允许微调：

- open/add action-value 对新资金 rank 的修正强度。
- 产品/setup/trigger 历史收益修正分。
- `entry_quality_loss_penalty`。
- `trigger_quality_positive_bonus`。
- `trigger_quality_loss_penalty`。
- `recent_tail_loss_penalty`。
- `learning_reward_unit`。
- `learning_full_weight_sample_count`。
- alpha setup 从 probe 到 normal/strong 资金层的晋升阈值。
- rank=1、rank=2、rank=3 分层平均收益是否显著优于低 rank。

40 日微调依据必须来自：

- rank 分层平均收益；
- 同资金层内 rank 表现；
- 同生命周期 action-value 命中后的收益；
- 产品 + 方向 + setup + trigger + evidence combo 的历史表现；
- 资金利用率、净敞口、手续费后收益和最大回撤。

## 禁止调参方式

- 不能用“限制交易”冒充优化。
- 不能因为某个品种短期亏损写死品种黑名单。
- 不能把 `watch_for_trigger` 直接排除出 rank；全是 watch 时仍要排出最值得小仓试探的 1-N。
- 不能让 execution 学习直接冒充 open/add 学习进入资金 rank。
- 不能让 `wait/hold/reduce/exit`、当前反转退出腿和不增加风险的条件监控抢新增风险资金 rank，也不能让实际增加风险的 `add/scale` 绕过 rank。
- 不能在样本不足时把单日亏损写成硬规则。

## 每次调参前必须回答

1. 依据来自哪一段回测、多少交易日、多少笔样本？
2. 调的是冷启动证据分、学习修正分、trigger 质量分，还是资金层级阈值？
3. 修改后预期改变什么交易行为？
4. 是否可能压死交易、过拟合、引入品种黑名单或未来数据污染？
5. 是否需要删除某日回测记录重跑，还是可以继续回测？
