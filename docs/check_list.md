# AgentQuant 回测后待验收清单

仅保留最新回测中部分通过及未通过的项目；完整通过项已删除。

## 系统与交易链

- [ ] 【待重新回测】数据库重复初始化不得删除或重置`signal.setup_type`；逐日核对signal记录的setup与首次落库值一致，不再批量变成`unknown`。
- [ ] 【部分通过】入场触发、入场作废、持仓失效、ATR止损和持有期限均被正确生产、传递和消费。54个新增风险FAC的入场边界、ATR和期限完整；持仓管理样本仍未覆盖全部字段及全部触发类型。
- [ ] 【部分通过】每品种每日最多一个策略动作；换约和forced-risk保持独立来源，不污染策略交易语义。单动作约束及4笔换约来源隔离已通过；本轮没有forced-risk事实。
- [ ] 【待重新回测】未交易学习的`side/setup_type/entry_trigger/horizon_class/market_regime`必须逐项继承对应FAC；FAC身份不完整的记录不得写入，不再出现分析师复合setup键。

## PM、rank与资金

- [ ] 【部分通过】Step4正确确定生命周期、candidate quality、probe/real/scale层级和计划比例，后续没有二次覆盖。86个排名候选均为exploration_probe且计划比例正确；本轮没有real/scale样本。
- [ ] 【部分通过】学习和当日证据均真实影响rank；real/scale能够由合格学习升级，并按对应层级获得更高资金比例。正向学习已产生+0.005 rank增量，负向学习已产生负增量；本轮仍没有real/scale升级样本。
- [ ] 【待重新回测】PM fallback学习必须标记为`partial_real_state`并以低权重影响rank，不能单独支持real/scale；严格匹配学习仍须保留晋升能力。

## 学习闭环

- [ ] 【部分通过】正向学习能够把后续合格交易从probe升级至real/scale并放大仓位；负向学习能够降低质量、层级或改变入场/退出处理。正负学习均已影响rank及生命周期动作；正向升级至real/scale仍没有样本。
- [ ] 【待重新回测】未交易fast-candidate晋升只使用固定5日窗口内同作用域完整正负样本，入场前已失效、已到期及缺FAC可执行依据的记录不得晋升；不再满足条件的既有active政策必须停用，多周期影子结果仅保留诊断用途。
- [ ] 【待重新回测】无合格长期参数证据时不得写`config_overlay_refresh`及active原值复制overlay；本轮确认PM实际配置未被伪刷新改变。
- [ ] 【待重新回测】PM审计摘要必须正确落地`holding_days`、`target_side`、`market_confirmation_score`，且不得暴露`held_days`、`raw_target_side`、`confirmation_score`及完整PM内部对象。

## 回测结果评价

- [ ] 【未通过：样本不足】按rank、资金层级、setup、trigger、持有期和退出原因比较完整episode表现，确认高投资价值排序具有实际区分度。当前仅5个完整episode，资金层均为exploration_probe且trigger均为technical_breakout，无法完成全维度分组比较。
- [ ] 【未通过：待继续】样本不足时继续延长回测，不根据少量交易直接调整rank权重、止损倍数或仓位参数。回测已由8日延长至15日，完整episode仍不足。
