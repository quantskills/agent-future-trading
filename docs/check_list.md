# agent-future-trading 回测后待验收清单

## 使用边界

本清单只记录代码已经具备正式生产端、持久化落点和正式消费端，但尚未由自然真实回测证明实际触发的业务功能，以及必须用真实成交结果判断的策略效果。每项只验收一个可观察结论；生产、消费和效果属于不同结论时分项记录，不在一项内捆绑。真实回测满足对应结论后，整项删除。

内部公式、配置阈值、严格控制变量反事实、禁止行为、字段一致性、角色权限、作用域匹配和未落盘中间态不进入本清单。这些代码不变量由确定性回归、生产链路测试、pre-backtest gate 与 daily PG audit 验收；不会自然出现的偶发事件不作为长期回测任务挂账。`1d/3d/5d/10d`预测评价不得生成PM contextual policy、同一policy唯一作用域不得混合多条绩效记录，均属于该类代码不变量，不等待自然回测碰巧证明。

历史事实与学习基线：`config_id=add584b8-b015-45e8-a4f7-4e50c53c8862`，`exp_name=agentquant-futures-trading`，当前数据库覆盖 2025-07-01 至 2025-12-31，共 126 条日结记录。当前库只读计数为：5,670 条 signal、1,895 条 recommendation、153 条 futures transaction、504 条 trading-day phase、21,825 条到期预测评价、663 条分层分析师绩效、1,636 条 alpha setup sample、827 条 profile、912 条 action-value、14 条 `setup_type_performance`、20 条 `adaptive_policy_state`、5 条探索假设、255 条未交易机会记忆和 317 条 `research_position_feedback`。7—9 月仍是历史基线；10—12 月是已落库的后续前向记录，不能仅凭同一 `config_id` 宣称为同一固定代码版本的半年实验。账户与策略绩效按 `docs/matrix_field_semantics.md` 的双口径记录，12 月账户净收益为负，稳定正收益仍未验收。

## 持仓生命周期功能

- [ ] **H1｜到期经济转负进入退出链。** 【当前版本尚无前向触发样本】持仓达到开仓FAC的`expected_horizon_days`且同侧预测校准成熟并转为手续费后负预期时，PM必须在当日FAC中落下对应减仓或退出结果。

## 学习与资金功能

- [ ] **L1｜多层预测校准被PM实际消费。** 【当前 FAC 中 `reliability_blend` 及完整 `source_scopes` 计数为 0】同一分析师、品种与期限同时存在至少两层合格成熟绩效时，后续新增风险 FAC 的 `rank_input_components.forecast_calibration` 必须实际记录 `scope_level=reliability_blend` 及至少两层对应 `source_scopes`。
- [ ] **L2｜真实预算层成交。** 【当前 FAC 与成交记录中 `real_budget_entry` 计数为 0】满足现有成熟正收益和当日证据条件的候选必须以 `real_budget_entry` 实际成交。
- [ ] **L3｜Alpha放大层成交。** 【当前无`alpha_scale_entry`成交】满足独立五样本及现有放大条件的候选必须以`alpha_scale_entry`实际开仓或加仓成交。
- [ ] **L4｜研究假设验证。** 【当前 5 条假设为 4 `candidate`、1 `rejected`，无 `monitoring/validated`】至少一条探索假设必须仅由生成日之后、同规范作用域的完整真实 episode 推进至 `validated`。
- [ ] **L5｜已验证假设消费。** 【当前无 `validated` 假设】至少一条 `validated` 假设必须在下一交易日进入匹配分析师的正式学习上下文。
- [ ] **L6｜Fast candidate 生产。** 【当前 255 条未交易机会记忆中未出现 `fast_candidate_alpha`】同作用域未交易记忆满足固定五日反事实、样本、正收益及完整 FAC 执行依据后，Researcher 必须形成 `fast_candidate_alpha`。
- [ ] **L7｜Fast candidate形成Probe。** 【当前无可消费候选】至少一条仍有效并通过当日确认条件的`fast_candidate_alpha`必须使PM实际签发同作用域probe FAC。

## 策略效果

- [ ] **S1｜Rank经济排序。** 【7—12月尚未完成当前可靠性融合来源的自然验收】高低 Rank 组均形成完整成交 episode 后，Rank 1—2 组的手续费后平均 `return_on_notional` 必须高于 Rank 3 及以后组。
- [ ] **S2｜真实预算层收益贡献。** 【待产生`real_budget_entry`完整episode】`real_budget_entry`对应完整episode的手续费后累计收益必须为正。
- [ ] **S3｜Alpha放大层收益贡献。** 【待产生`alpha_scale_entry`完整episode】`alpha_scale_entry`对应完整episode的手续费后累计收益必须为正。
- [ ] **S4｜连续自然月正收益。** 【当前 10—11 月为正、12 月为负，未通过】后续连续三个完整自然月的策略成交手续费后净收益必须分别为正。
