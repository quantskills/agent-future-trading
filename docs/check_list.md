# AgentQuant 回测后待验收清单

仅保留最新回测中部分通过及未通过的项目；完整通过项已删除。

当前验收范围：2025-04-01 至 2025-05-09，共 25 个已完成交易日。375条策略建议满足每日单动作约束，59笔成交、11个完整episode已形成，系统不变量七项全部通过。

## 系统与交易链

- [ ] 【部分通过】入场触发、入场作废、持仓失效、ATR止损和持有期限均被正确生产、传递和消费。81/81个新增风险FAC的入场触发、作废边界、ATR及期限完整，115/115条持仓生命周期学习记录准确继承原开仓FAC；11个完整episode只覆盖9个`trend_breakout_setup`与2个`trend_pullback_setup`，其他setup尚未形成完整周期样本。
- [ ] 【部分通过】每品种每日最多一个策略动作；换约和forced-risk保持独立来源，不污染策略交易语义。375/375条策略建议满足单动作约束，6个换约建议及8笔换约成交腿保持独立来源；本轮没有forced-risk事实。

## PM、rank与资金

- [ ] 【部分通过】Step4正确确定生命周期、candidate quality、probe/real/scale层级和计划比例，后续没有二次覆盖。150个带rank的FAC与最终资金部署字段一致；全部仍为`exploration_probe`，本轮没有real/scale样本。
- [ ] 【部分通过】学习和当日证据均真实影响rank；real/scale能够由合格学习升级，并按对应层级获得更高资金比例。42个FAC产生非零学习增量，其中32个含正向增量、25个含负向增量；没有real/scale升级及更高资金比例样本。
- [ ] 【部分通过】PM fallback学习必须标记为`partial_real_state`并以低权重影响rank，不能单独支持real/scale；严格匹配学习仍须保留晋升能力。22条PM建议正式消费23条`partial_real_state`与6条`exact_real_state` action-value；partial记录均停留在`exploration_probe`，严格匹配尚未形成real/scale晋升样本。

## 学习闭环

- [ ] 【部分通过】正向学习能够把后续合格交易从probe升级至real/scale并放大仓位；负向学习能够降低质量、层级并改变入场与退出处理。正向与负向学习均已进入rank，持仓链形成82次hold、25次reduce、8次exit；全部新增风险交易仍处于`exploration_probe`，升层、放大及降层样本尚未形成。
- [ ] 【部分通过】未交易fast-candidate晋升只使用固定5日窗口内同作用域完整正负样本；入场前已失效、已到期及缺FAC可执行依据的记录不得晋升；不再满足条件的既有active政策必须停用，多周期影子结果仅保留诊断用途。本轮62条未交易记录中，40条判定为错失机会、21条判定为正确回避、1条待观察；尚无固定5日链生成的合格晋升样本。现有4条由alpha profile生成的`fast_candidate_alpha`均已停用，不作为固定5日链验收样本。
- [ ] 【部分通过】`alpha_setup_profile → adaptive_policy_state`必须原值传递同一canonical `setup_type`。本轮4/4条`fast_candidate_alpha`的表字段、来源profile及payload setup完全一致，未产生空值、`*`、`unknown`身份；尚无政策在T+1被实际消费的FAC，跨setup实际检索隔离仍待回测记录验收。
- [ ] 【未验证：等待修复后新记录】`adaptive_policy_state.source_trading_date`必须等于该政策当前`source_event_id`所关联的`learning_event_log.trading_date`，记录本次Phase4政策生成及刷新日，不得写成episode结束日、profile的`last_sample_date`及字段名字符串。当前12/12条政策由修复前代码生成，`source_trading_date`均为空，不能验收本次修复；同日政策不得被PM读取，T+1起按既有`valid_until`读取。本项不改变Phase4自动刷新、有效期、PM检索、rank、资金及交易事实。

## 回测结果评价

- [ ] 【未验收：样本不足】按rank、资金层级、setup、trigger、持有期和退出原因比较完整episode表现，确认高投资价值排序具有实际区分度。当前11个完整episode全部属于`exploration_probe`；rank 1平均净收益2179.81，rank 3平均净收益581.62，rank 4平均净收益-1139.92，rank 5平均净收益5596.06，rank 6净收益-2802.00，尚未形成稳定且单调的区分度。
