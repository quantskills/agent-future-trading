# AgentQuant 回测后待验收清单

## 使用边界

本清单只记录代码已经具备正式生产端、持久化落点和正式消费端，但尚未由自然真实回测证明实际触发的业务功能，以及必须用真实成交结果判断的策略效果。真实回测已经生成对应业务记录并被正式下游消费后，整项删除。

内部公式、配置阈值、严格控制变量反事实、禁止行为、字段一致性、角色权限、作用域匹配和未落盘中间态不进入本清单。这些代码不变量由确定性回归、生产链路测试、pre-backtest gate 与 daily PG audit 验收；不会自然出现的偶发事件不作为长期回测任务挂账。`1d/3d/5d/10d`预测评价不得生成PM contextual policy、同一policy唯一作用域不得混合多条绩效记录，均属于该类代码不变量，不等待自然回测碰巧证明。

历史证据基线：`config_id=add584b8-b015-45e8-a4f7-4e50c53c8862`，`exp_name=agentquant-futures-trading`，覆盖 2025-07-01 至 2025-09-30 共 66 个交易日。基线包含 264 个完整阶段、66 次日结、81 笔成交、27 个完整 episode、286 条未交易记忆、64 条探索假设、2970 份分析师 signal、11025 条到期预测评价、607 条分层分析师绩效、990 条 alpha setup 样本、516 条 profile、561 条 action-value、21 条 adaptive policy 和 2306 条学习事件。当前数据库已清除全部2025-10月记录；2025-10-01 起的新记录用于验收2026-08-11至2026-08-14完成的学习、预测校准、资金部署及policy生产修复，7—9月只作为训练/历史基线，不宣称为当前版本的前向结果。

## 交易业务功能

- [ ] 【部分验收：已有反向平仓，缺后续反向开仓样本】旧方向由原 FAC 平仓并归零后，后续反向开仓必须由新的 FAC 重新取得 Rank、建立新 episode，并完成成交与结算。
- [ ] 【部分验收：C 已完成同向换约续仓，RB 已完成只平旧合约；缺只平旧后的再次策略开仓】换约只平旧合约后，下一次策略开仓必须由新 FAC 建立新 episode。
- [ ] 【未验收：没有 `standard_confirmation_supported` 实际成交样本】携带 `standard_confirmation_supported` 的 FAC 必须经 Auditor 放行、Trader 按既有标准确认触发并形成真实成交。

## 学习与资金功能

- [ ] 【待新版自然回测验收：7—9月保留21条policy，其中PM contextual policy为short 5条、medium 4条，历史FAC与反馈仍无实际应用引用】至少一条合法、语义期限匹配的 adaptive policy 必须真实改变评分、技术参数、仓位比例或资金层，并同时落入 `final_action_contract.learning_used.adaptive_policy_applied` 与 `research_position_feedback.policy_refs_json`。
- [ ] 【未验收：7—9月的 29 次策略开仓全部属于 `exploration_probe`】达到现有成熟正收益条件的 action-value 必须实际产生 `real_budget_entry`；达到独立五样本及现有放大条件后必须实际产生 `alpha_scale_entry`，并完成成交、持仓和结算。
- [ ] 【部分验收：`setup_type_performance` 现有7条全部为上层聚合记录，exact-ticker仍为0】完整 episode 必须同时形成可供正式下游读取的 exact-ticker setup 绩效与上层聚合绩效。
- [ ] 【部分验收：64 条探索假设中已有 37 条 `rejected`，没有 `validated`】至少一条由未来同作用域完整 episode 支持的探索假设必须进入 `validated`，并在下一交易日进入对应分析师的学习上下文。
- [ ] 【未验收：286 条未交易记忆中已有 111 条错失机会，没有 `fast_candidate_alpha`】满足现有固定五日、同作用域和有效 FAC 条件的错失机会必须形成 `fast_candidate_alpha`，并在下一交易日被 PM 作为同作用域 probe policy 实际消费。
- [ ] 【待新版自然回测验收：7—9月没有`technical_parameters` policy；现有代码已具备technical、exact-ticker、short绩效的多空样本聚合生产端及正式消费端】Researcher 必须生产一条`side=*`的 `contextual_rule_calibration:technical_parameters`，其证据必须保留来源绩效ID、方向、样本与收益；技术分析师实际采用后，`learning_impact_summary` 必须记录 policy ID 与参数前后值，FAC 必须记录该 policy 的实际应用。

## 策略效果

- [ ] 【待 2025-10-01 至 2025-12-31 当前版本前向回测验收】候选期限预测校准、带符号历史技能、action-value收益强度与探索资金分配必须形成完整成交 episode；按开仓 FAC 分组后，Rank 1—2 组的成交胜率和手续费后平均 `return_on_notional` 必须同时高于 Rank 3 及以后组。
- [ ] 【待 2025-10-01 至 2025-12-31 前向回测验收】10、11、12 月各自然月的策略成交手续费后净收益必须为正，三个月累计净收益必须为正；`real_budget_entry/alpha_scale_entry` 的手续费后收益贡献必须单独统计，证明学习放大的是正收益机会。
