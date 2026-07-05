# 参数调节备忘录

本文档记录 AgentQuant 的参数调节边界、rank 分数机制和回测后再微调的观察口径。它不是当前策略指令，也不是立即改参清单；所有微调必须基于干净回测证据。

本轮重点回测样本区间固定为：

```text
2025-03-25 至 2025-05-31
```

在样本不足前，不允许因为单日或少数品种盈亏直接改参数。至少 30 个交易日后才允许做第一轮系数复核；至少 40 个交易日后才允许微调 rank 分数权重和学习修正强度。

## 不动的硬边界

- `max_total_margin_ratio = 0.20`：组合总保证金硬上限 20%。
- 现有 probe/normal/strong 资金层级参数不因短样本调动。
- 手续费、滑点、合约乘数、保证金率属于交易事实，不因策略表现调参。
- PM/Auditor/Trader/Accountant/Researcher 边界不因调参改变。
- hard fail 只用于非策略错误，不能为改善收益而降级。

## 唯一全市场 rank 分数机制

唯一 `opportunity_rank` 只用于新增风险敞口资金排序。rank=1 永远表示当天全市场最值得占用资金的产品机会。

基础公式：

```text
rank_score =
  冷启动证据质量分
+ 生命周期 action-value 学习修正分
+ 产品/setup/trigger 历史收益修正分
+ trigger/执行质量修正分
+ 资金效率小修正
- 冲突/风险/失效边界惩罚
```

建议 100 分口径：

| 模块 | 分值 | 含义 |
|---|---:|---|
| 当前证据质量 | 45 | 冷启动或学习不足时的主分数 |
| 强化学习修正 | -35 到 +35 | action-value 和产品级历史表现对 rank 的核心修正 |
| trigger / execution 质量 | -10 到 +10 | 只修正触发质量，不直接生成开仓权限 |
| 资金效率 | 0 到 10 | 小权重修正，不能替代盈利概率 |
| 硬风险 | 不打分 | 审计 block、无效合约、超硬风控直接不能部署 |

当前证据质量 45 分拆分：

| 子项 | 分值 |
|---|---:|
| 三类分析师方向一致或主方向清楚 | 10 |
| 证据强度和新鲜度 | 8 |
| setup 清晰且适合该产品 | 8 |
| entry trigger 明确且 Trader 可客观判断 | 7 |
| invalidation / stop 边界明确 | 6 |
| 市场确认支持 | 6 |

强化学习修正 -35 到 +35 分拆分：

| 子项 | 分值 |
|---|---:|
| 同产品 + 同方向 + 同 setup + 同 trigger 的 open/add action-value | -18 到 +18 |
| 产品/setup/trigger 历史收益表现 | -10 到 +10 |
| entry quality outcome | -4 到 +4 |
| 近期亏损或 tail loss 惩罚 | 最高 -8 |

资金部署规则：

- 按 `rank_score` 从高到低排出 1-N。
- `rank=1` 只表示最值得占用资金，不自动升仓。
- `watch_for_trigger / exploration_probe` 仍使用原 probe 资金层，不因 rank 高而加仓。
- `tradeable_candidate` 才能进入 normal 真实资金层。
- 反复验证有 alpha 的候选才允许进入 strong / scale 资金层。
- 资金按 rank 顺序逐个占用预算；触及总保证金、净敞口或多空平衡限制后，后续候选还原为 wait。

## 需要 30 个交易日后复核的参数

以下项目必须至少有 30 个干净交易日记录后才允许复核：

- 冷启动证据质量 45 分的子项权重。
- `watch_priority_score` 和 `capital_priority_score` 中当前证据项的权重。
- `probe_candidate` 与 `watch_for_trigger` 的排序差异是否合理。
- `entry_trigger`、`invalidation`、`market_confirmation` 对 rank 的贡献比例。
- `execution_profile_learning` 对 trigger 质量的修正强度。
- 探针层 rank 表现诊断口径：只比较 exploration_probe 新仓收益，不混入 hold/reduce/exit。

30 日复核只允许判断“方向是否明显错位”，原则上不做大幅调参。

## 需要 40 个交易日后微调的参数

以下项目必须至少有 40 个干净交易日记录后才允许微调：

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
- 不能把 watch_for_trigger 直接排除出 rank；全是 watch 时仍要排出最值得小仓试探的 1-N。
- 不能让 execution 学习直接冒充 open/add 学习进入资金 rank。
- 不能让 hold/reduce/exit 抢新资金 rank。
- 不能在样本不足时把单日亏损写成硬规则。

## 每次调参前必须回答

1. 依据来自哪一段回测、多少交易日、多少笔样本？
2. 调的是冷启动证据分、学习修正分、trigger 质量分，还是资金层级阈值？
3. 修改后预期改变什么交易行为？
4. 是否可能压死交易、过拟合、引入品种黑名单或未来数据污染？
5. 是否需要删除某日回测记录重跑，还是可以继续回测？

## 当前提醒

当前优先任务不是立即调 rank 分数，而是确认 Researcher 写出的 action-value / product learning 能在下一交易日进入 PM rank 输入并形成非零修正。只有确认学习闭环真实生效后，才可以用 30/40 个交易日样本微调上述系数。
