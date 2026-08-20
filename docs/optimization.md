# agent-future-trading 策略优化方案

> 本文记录基于现有代码与 2025 年 7—12 月回测证据确定的优化边界。
> 本方案只包含两个策略优化方向和一个独立代码断点修复，不新增第二条交易链。
> 本文只记录方案，不表示代码和数据库已经执行修改。

## 一、优化方向一：提高多维预测准确性

目标：在不改变技术指标、基本面因子、新闻关键词和差异化分析本身的前提下，提高三名分析师对价格波动的预测准确性。

### 修改内容

- 保留现有技术、基本面和新闻分析流程，以及 `ForwardForecast` 的 1/3/5/10 日预测结构。
- 使用现有结构化输出中的指标、因子组、事件类型和 `key_drivers`，不新增数据源、预测模型或分析师角色。
- 在现有预测评价链中，按分析师、品种、方向、期限和市场状态统计已有证据的命中率、Brier 和手续费后收益。
- 将“哪些已有证据有效、哪些证据失效”通过现有 `analyst_learning_context` 反馈给对应分析师，作为下一交易日的可反驳校准先验。
- 预测反馈只调整分析师的概率、预期收益和证据质量，不直接产生交易方向、手数、资金权限或 PM 绕过。

### 相关现有代码

- `src/tools/agent_tools/analysis/analyst_learning_calibration.py`
- `src/tools/agent_tools/analysis/analyst_learning_context.py`
- `src/tools/agent_tools/research/research_memory_writers.py`

## 二、优化方向二：提高 Alpha 的重复性和稳定性

目标：让经过校准的手续费后净预期收益，沿现有 SCC → PM → Rank → 资金部署链传递，使重复出现的正 Alpha 能自然放大。

### 已经存在、不得重复重写的机制

- `pm_signal_fusion.py` 已将 1/3/5/10 日校准结果形成多空手续费后净预期收益，并接入现有 Rank。
- `alpha_setup.py` 和 `research_learning.py` 已按 `ticker/side/setup_type/horizon_class/market_regime` 聚合成熟样本。
- Rank、`capital_layer_priority=0/3/6`、0.8%—1.5%探索资金、real/scale 门槛、Trader 触发和退出链保持不变。

### 本轮边界

- 不重新设计 Rank 积分，不调整资金层，不降低 real/scale 门槛。
- 不封禁品种、不删除空头、不增加一致性门槛、不减少合法交易机会。
- 冷启动后只观察同一规范作用域的正 Alpha 是否重复出现，并由现有 real/scale 机制自然放大。

## 三、独立代码断点修复：policy 实际归因

### 目标链路

```text
实际生效的技术/PM policy
    → 现有控制诊断
    → final_action_contract.learning_used.adaptive_policy_applied
    → research_position_feedback.policy_refs_json
```

### 修改边界

- 当前自然回测中 FAC 的 `adaptive_policy_applied` 仍为空，需要检查并修复现有传递链的最后一跳。
- 只有真正改变评分、参数、仓位比例或资金层的 policy 才写入 FAC。
- 仅被检索但没有产生实际变化的 policy 不记录。
- 研究反馈只读取 FAC 的统一字段，并与 action-value 独立归因。
- 不新增数据库表、不回填历史 FAC、不建立旁路 policy 消费路径。

## 四、冷启动回测边界

1. 备份当前数据库和回测日志。
2. 新建空数据库，不导入旧的 profile、action-value、policy、hypothesis 或研究反馈。
3. 旧回测只作为问题分析依据，不作为新运行的学习输入。
4. 行情、基本面和新闻缓存可以保留。
5. 固定完成后的代码和现有 Rank 配置，从 2025-07-01 重新回测至 2026-07-01。
6. 回测过程中不临时调 Rank、不修改技术指标或基本面因子、不新增交易限制。

## 五、验收重点

- 三类分析师的 Brier、方向命中率和手续费后预测收益是否改善。
- 已有证据在不同品种、期限和市场状态下是否形成可重复效果。
- 高 Rank 组手续费后收益和 Profit Factor 是否高于低 Rank 组。
- 正 Alpha 是否能在同一规范作用域重复出现并自然达到 real/scale 门槛。
- 实际生效的 policy 是否完整进入 FAC 和研究反馈。
- 交易机会数量是否没有被人为压缩。

稳定正收益必须由冷启动后的前向回测证明，不能用修改后的静态指标或减少交易来替代验收。
