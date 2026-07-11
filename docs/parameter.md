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

唯一 `opportunity_rank` 只用于从空仓建立新仓的资金排序。`rank=1` 永远表示当天全市场最值得占用开仓资金的产品机会。

基础公式：

```text
rank_score =
  冷启动证据质量分
+ 生命周期 open/add action-value 学习修正分
+ 产品/setup/trigger 历史收益修正分
+ trigger/execution 质量修正分
+ 资金效率小修正
- 冲突/风险/失效边界惩罚
```

当前代码映射：

| 分项 | 字段 | 当前作用 |
|---|---|---|
| 冷启动证据质量 | `rank_score_components.cold_start_evidence_quality` | 无学习或学习样本少时的主排序依据 |
| 资金层级资格 | `capital_layer_priority` | tradeable_candidate 高于 probe，高于 watch |
| open/add 学习 | `open_add_action_value_delta` | 正向学习提高 rank，负向/tail/entry loss 降低 rank |
| 产品/setup/trigger 历史表现 | `product_setup_trigger_history` | alpha profile 对同类机会的加减分 |
| trigger/execution 质量 | `trigger_execution_quality` | execution 学习只修正触发质量，不直接生成开仓权限 |
| 资金效率 | `capital_efficiency` | 预留小权重，40 日样本后再评估是否启用 |
| 冲突/风险/失效边界 | `conflict_risk_invalidation_penalty` | 冲突、数据缺口、风险和失效边界不足的扣分 |

权重配置入口固定为 `src/config/rank_score_policy.yaml`，由 `dev.yaml.config_catalogs.rank_score_policy` 引入并在运行时展开为 `rank_score_policy`。该 catalog 只允许微调 `rank_score` 权重和资金效率小修正，不允许改仓位参数、交易权限、`0.008` probe、`20%` 总保证金硬边界或 `0.5` 净敞口计划预算。样本不足 40 个干净交易日前，不应调整该 catalog。

资金部署规则：

- 只有 `current_lots=0` 且 `target_lots!=0` 的开仓候选进入 1-N 排名；`add/scale/hold/reduce/exit/reverse` 当前合约没有 rank。
- 按 `rank_score` 从高到低排出 1-N。
- `rank=1` 只表示最值得占用资金，不自动升仓。
- `watch_for_trigger / exploration_probe` 仍使用原 0.008 小探针资金层。
- `tradeable_candidate` 才能进入 normal 真实资金层。
- 反复验证有 alpha 的候选才允许进入 strong / scale 资金层。
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
- 不能让 add/scale/hold/reduce/exit/reverse 当前合约抢开仓资金 rank。
- 不能在样本不足时把单日亏损写成硬规则。

## 每次调参前必须回答

1. 依据来自哪一段回测、多少交易日、多少笔样本？
2. 调的是冷启动证据分、学习修正分、trigger 质量分，还是资金层级阈值？
3. 修改后预期改变什么交易行为？
4. 是否可能压死交易、过拟合、引入品种黑名单或未来数据污染？
5. 是否需要删除某日回测记录重跑，还是可以继续回测？
