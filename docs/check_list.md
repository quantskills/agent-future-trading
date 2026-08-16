# AgentQuant 回测后待验收清单

## 使用边界

本清单只记录代码已经具备正式生产端、持久化落点和正式消费端，但尚未由自然真实回测证明实际触发的业务功能，以及必须用真实成交结果判断的策略效果。每项只验收一个可观察结论；生产、消费和效果属于不同结论时分项记录，不在一项内捆绑。真实回测满足对应结论后，整项删除。

内部公式、配置阈值、严格控制变量反事实、禁止行为、字段一致性、角色权限、作用域匹配和未落盘中间态不进入本清单。这些代码不变量由确定性回归、生产链路测试、pre-backtest gate 与 daily PG audit 验收；不会自然出现的偶发事件不作为长期回测任务挂账。`1d/3d/5d/10d`预测评价不得生成PM contextual policy、同一policy唯一作用域不得混合多条绩效记录，均属于该类代码不变量，不等待自然回测碰巧证明。

历史证据基线：`config_id=add584b8-b015-45e8-a4f7-4e50c53c8862`，`exp_name=agentquant-futures-trading`，覆盖 2025-07-01 至 2025-09-30 共 66 个交易日。原始事实完整保留：264 个完整阶段、66 次日结、81 笔成交、27 个完整 episode、3015 份分析师 signal。2026-08-16 按当前代码只重建截至9月30日的派生学习，现有11025条到期预测评价、607条分层分析师绩效、755条alpha setup样本、375条profile、420条action-value、7条聚合`setup_type_performance`、4条`contextual_rule_calibration:portfolio_manager` policy；exact-ticker setup绩效、`technical_parameters` policy、探索假设、未交易记忆和研究持仓反馈当前均为0。原始推荐、成交、结算、盈亏和episode指纹核验未改变。当前数据库没有2025-10月及以后记录；2025-10-01起用于验收当前版本，7—9月只作为训练与历史基线，不宣称为当前版本的前向结果。

## 持仓生命周期功能

- [ ] **H1｜到期经济转负进入退出链。** 【当前版本尚无前向触发样本】持仓达到开仓FAC的`expected_horizon_days`且同侧预测校准成熟并转为手续费后负预期时，PM必须在当日FAC中落下对应减仓或退出结果。

## 学习与资金功能

- [ ] **L1｜精确setup绩效生产。** 【当前仅有7条聚合绩效，exact-ticker为0】后续完整真实episode达到现有样本条件后，Researcher必须形成至少一条ticker非通配的`setup_type_performance`。
- [ ] **L2｜真实预算层成交。** 【7—9月策略开仓均为`exploration_probe`】满足现有成熟正收益和当日证据条件的候选必须以`real_budget_entry`实际成交。
- [ ] **L3｜Alpha放大层成交。** 【当前无`alpha_scale_entry`成交】满足独立五样本及现有放大条件的候选必须以`alpha_scale_entry`实际开仓或加仓成交。
- [ ] **L4｜生效Policy写入FAC。** 【当前4条policy尚无前向实际应用】至少一条实际改变评分、技术参数、仓位比例或资金层的policy必须写入`final_action_contract.learning_used.adaptive_policy_applied`。
- [ ] **L5｜研究反馈继承Policy。** 【当前研究持仓反馈为0】已写入FAC的policy必须由同日Researcher以相同policy ID写入`research_position_feedback.policy_refs_json`。
- [ ] **L6｜技术参数Policy生产。** 【当前仅I多头有3个technical short样本，低于现有4样本门槛】满足现有同品种technical、short-horizon聚合样本、置信度和绩效条件后，Researcher必须生产一条`side=*`的`contextual_rule_calibration:technical_parameters`。
- [ ] **L7｜技术参数Policy应用。** 【当前无实际应用样本】技术分析师实际采用合格`technical_parameters` policy后，`learning_impact_summary`必须记录该policy ID及参数前后值。
- [ ] **L8｜研究假设生产。** 【当前探索假设为0】Researcher必须从新增完整真实episode生成至少一条`candidate`探索假设。
- [ ] **L9｜研究假设验证。** 【当前无待验证假设】至少一条探索假设必须仅由生成日之后、同规范作用域的完整真实episode推进至`validated`。
- [ ] **L10｜已验证假设消费。** 【当前无`validated`假设】至少一条`validated`假设必须在下一交易日进入匹配分析师的正式学习上下文。
- [ ] **L11｜未交易记忆生产。** 【派生重建后当前未交易记忆为0】后续合法未交易事实必须形成至少一条`no_trade_opportunity_memory`。
- [ ] **L12｜Fast candidate生产。** 【当前未交易记忆与候选均为0】同作用域未交易记忆满足固定五日反事实、样本、正收益及完整FAC执行依据后，Researcher必须形成`fast_candidate_alpha`。
- [ ] **L13｜Fast candidate形成Probe。** 【当前无可消费候选】至少一条仍有效并通过当日确认条件的`fast_candidate_alpha`必须使PM实际签发同作用域probe FAC。

## 策略效果

- [ ] **S1｜Rank经济排序。** 【待2025-10-01起前向验收】高低Rank组均形成完整成交episode后，Rank 1—2组的手续费后平均`return_on_notional`必须高于Rank 3及以后组。
- [ ] **S2｜放大层收益贡献。** 【待产生真实预算层或Alpha放大层成交后验收】`real_budget_entry`与`alpha_scale_entry`对应完整episode的手续费后累计收益必须为正。
- [ ] **S3｜连续自然月正收益。** 【待2025-10-01至2025-12-31前向验收】10月、11月、12月各自然月的策略成交手续费后净收益必须分别为正。
