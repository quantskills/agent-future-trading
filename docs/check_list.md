# AgentQuant 回测后待验收清单

仅保留最新回测中部分通过及未通过的项目；完整通过项已删除。

当前验收范围：2025-04-01 至 2025-04-11，共 8 个已完成交易日。

## 系统与交易链

- [ ] 【部分通过】入场触发、入场作废、持仓失效、ATR止损和持有期限均被正确生产、传递和消费。35个新增风险FAC的入场边界、ATR和期限完整，24个持仓生命周期摘要字段完整；4个完整episode仍全部采用`technical_breakout`触发，其他触发类型尚未形成完整周期样本。
- [ ] 【部分通过】每品种每日最多一个策略动作；换约和forced-risk保持独立来源，不污染策略交易语义。120个策略动作均满足单动作约束，4个换约建议及6笔换约成交腿保持独立来源；本轮没有forced-risk事实。

## PM、rank与资金

- [ ] 【部分通过】Step4正确确定生命周期、candidate quality、probe/real/scale层级和计划比例，后续没有二次覆盖。52个带rank的FAC与最终资金部署字段一致，18条策略建议实际执行；全部仍为`exploration_probe`，本轮没有real/scale样本。
- [ ] 【部分通过】学习和当日证据均真实影响rank；real/scale能够由合格学习升级，并按对应层级获得更高资金比例。本轮5个FAC获得正向学习增量，尚无负向学习增量；没有real/scale升级样本。
- [ ] 【部分通过】PM fallback学习必须标记为`partial_real_state`并以低权重影响rank，不能单独支持real/scale；严格匹配学习仍须保留晋升能力。本轮3条fallback action-value均为`partial_real_state`并停留在`exploration_probe`；另有1条`exact_state`被生命周期减仓链消费，尚无严格匹配驱动real/scale晋升的实盘样本。

## 学习闭环

- [ ] 【部分通过】正向学习能够把后续合格交易从probe升级至real/scale并放大仓位；负向学习能够降低质量、层级并改变入场与退出处理。本轮正向学习已影响5个FAC，负向学习尚无消费样本；生命周期动作出现11次hold、9次reduce、4次exit，其中2次按原开仓FAC持仓失效退出；正向升级至real/scale尚无样本。
- [ ] 【部分通过】未交易fast-candidate晋升只使用固定5日窗口内同作用域完整正负样本；入场前已失效、已到期及缺FAC可执行依据的记录不得晋升；不再满足条件的既有active政策必须停用，多周期影子结果仅保留诊断用途。本轮26条未交易记录中，16条已判定错失机会、2条已判定正确回避、8条仍待观察；尚无合格的新晋升及旧政策停用样本，实际晋升路径仍未验收。
- [ ] 【未验证：等待新回测】`alpha_setup_profile → adaptive_policy_state`必须原值传递同一canonical `setup_type`。`fast_candidate_alpha`及alpha setup政策的列值必须与来源profile和payload一致；空值、`*`、`unknown`不得生成该条政策；同setup在T+1可命中，跨setup检索结果必须为空。修复前2条`setup_type='*'`记录不得计入验收样本。
- [ ] 【未验证：等待新回测】日常hold/reduce/exit学习必须继承当前持仓原开仓FAC的canonical `setup_type`，不得使用当日FAC重新定义持仓身份。同向换约续仓后仍继承原开仓FAC；换约只平旧后原周期结束；后续策略新开仓建立新FAC身份。逐条核对`alpha_setup_sample/profile/action_value`与成交血缘及原开仓recommendation一致，当日FAC setup仅作为当日证据保留。
- [ ] 【未验证：等待新回测】execution学习必须使用对应FAC的canonical `setup_type`，正式样本和action-value不得再生成`execution_*_setup`；执行方式只由Trader已落地的`execution_result.execution_learning_trace.execution_retrieval_key`区分。逐条核对execution样本、action-value、Trader trace三者的检索键完全一致，PM从该键读取execution profile，身份不完整的执行记录不得形成正式execution action-value。

## 回测结果评价

- [ ] 【未通过：样本不足】按rank、资金层级、setup、trigger、持有期和退出原因比较完整episode表现，确认高投资价值排序具有实际区分度。当前仅4个完整episode，全部属于`exploration_probe`；rank 2净收益32080.11，rank 3净收益-13743.02，rank 4净收益7052.46，rank 5净收益-3548.00，尚未形成稳定且单调的区分度。
- [ ] 【未通过：等待重新回测】最新结果仅覆盖2025-04-01至2025-04-11共8个交易日，且该区间的日常持仓学习、execution学习及4月11日收盘后的alpha setup政策均由修复前代码生成，不能验收本次修改。清理本轮回测记录后从2025-04-01重新回测至2025-07-31；不得根据当前4个完整episode调整rank权重、止损倍数和仓位参数。
