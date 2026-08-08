# AgentQuant 回测后待验收清单

只记录代码已经实现、能够由自然真实回测产生验收证据、经当前真实回测仅部分验收或仍未验收的功能与效果项目。已经被当前回测完整验收的项目从本清单删除；必须依赖严格控制变量、成对反事实、内部中间态或未落盘字段才能证明的代码不变量，不属于本清单，由确定性回归、属性测试和生产链路测试验收。代码测试及只读重放不代替本清单所列真实回测验收，代码没有实现或语义已经偏离生产代码的功能不得写入本清单。`部分验收`表示已有命中样本但缺少完整分支或自然对照样本，`未验收`表示没有足以形成回测结论的真实样本。

当前证据基线：`config_id=2e43779d-9f73-4d5e-b09d-22a19f0346fa`，当前主配置 `exp_name=agentquant-futures-trading`，覆盖 2025-07-01 至 2025-09-30 共 66 个交易日。基线包含 92 笔成交、32 个完整 episode、277 条未交易记忆、74 条探索假设和 2970 份分析师 signal；该基线由本轮多期限预测与校准排序修改前的冻结代码产生。下一轮重新回测 2025 年 7—9 月，验证预测准确性、资金排序和研究闭环，同时核对 7 月盈利机会未被破坏。

## 系统与交易链

- [ ] 【未验收：无 forced-risk 真实样本】`forced-risk` 必须保持独立来源，不污染策略交易语义。
- [ ] 【部分验收：已有 setup 身份和政策记录，缺政策实际命中对照】新机会的 setup 专属政策必须按当天 SCC/FAC 身份检索。
- [ ] 【部分验收：已有跨日持仓和政策记录，缺持仓政策实际命中对照】原持仓的 setup 专属政策必须按原开仓 FAC 身份检索。
- [ ] 【部分验收：已有反向平仓，缺同品种完整反转日对照】反向日必须仅按原开仓 FAC 结束旧持仓周期。
- [ ] 【部分验收：当前反向平仓样本未出现同原子反向开仓，缺重复样本】反向日不得在同一原子决策中直接建立反向新仓。
- [ ] 【未验收：缺平仓后下一交易日同品种反向机会样本】下一交易日反向机会仍成立时必须使用当日新 FAC。
- [ ] 【未验收：缺平仓后下一交易日同品种反向成交样本】下一交易日按反向机会开仓时必须建立新学习周期。
- [ ] 【未验收：缺换约样本】换约日同向续仓必须延续原学习周期。
- [ ] 【未验收：缺换约样本】换约日同向续仓必须继承原开仓 FAC 身份。
- [ ] 【部分验收：仅有 1 个 PB 换约只平旧样本，缺后续学习闭环】换约日只平旧合约时必须结束原学习周期。
- [ ] 【未验收：缺换约后新开仓样本】换约只平旧后的下一次策略开仓必须建立新学习周期。
- [ ] 【未验收：缺换约后新开仓样本】换约只平旧后的下一次策略开仓必须使用新 FAC。
- [ ] 【部分验收：已有生命周期直接退出样本，缺原 FAC 失效独立命中对照】原开仓 FAC 失效时必须优先直接退出。
- [ ] 【部分验收：已有生命周期直接退出样本，缺结构止损独立命中对照】结构止损触发时必须优先直接退出。
- [ ] 【部分验收：已有生命周期直接退出样本，缺初始 ATR 止损独立命中对照】初始 ATR 止损触发时必须优先直接退出。
- [ ] 【未验收：缺真实样本】原 FAC 未失效、普通持仓 `position_pnl_ratio<=-0.02`、同向证据再验证失败且 `position_pnl_ratio>-0.04` 时，必须减仓 50%。
- [ ] 【未验收：缺真实样本】原 FAC 未失效、普通持仓 `position_pnl_ratio<=-0.04` 且同向证据再验证失败时，必须全部退出。
- [ ] 【未验收：缺真实样本】普通持仓 `position_pnl_ratio<=-0.02` 但同向证据再验证通过时，不得触发普通亏损减仓。
- [ ] 【未验收：缺真实样本】普通持仓 `position_pnl_ratio<=-0.04` 但同向证据再验证通过时，不得触发普通亏损退出。
- [ ] 【未验收：缺 `standard_confirmation_supported` 入场对照样本】`standard_confirmation_supported` 入场不得被增强量能确认门限制。

## PM、排名与资金

- [ ] 【未验收：新代码尚未回测】到期预测校准为正的同层候选必须获得更高 `calibrated_forecast_value` 与唯一 Rank，且不得改变其交易权限和资金层。
- [ ] 【未验收：新代码尚未回测】到期预测校准为负的候选只能降低相对 Rank，不得被永久禁止、清零或移出探索链。
- [ ] 【未验收：新代码尚未回测】预测历史为空时 `calibrated_forecast_value` 必须为 0，冷启动候选仍按原交易链参加排序。
- [ ] 【未验收：新代码尚未回测】高 Rank 组的方向命中率、手续费后平均收益和 Brier 必须优于低 Rank 组。

- [ ] 【未验收：缺学习抬分对照样本】当日证据未满足现有入场前提时，历史学习不得把候选升级为 `probe/real/scale`。
- [ ] 【未验收：缺主导反对证据样本】存在未解决主导反对证据时不得签发开仓 FAC。
- [ ] 【未验收：缺成熟学习放大样本】当前证据满足入场前提后，仍有效的成熟正向学习必须能够参与唯一 Rank 和差异化仓位放大。
- [ ] 【未验收：缺真实样本】严格匹配的成熟正向学习与合格当日证据必须能够把候选从 `probe` 升级至 `real/scale`。
- [ ] 【未验收：缺真实样本】`real/scale` 候选必须按现有资金分层获得高于 `probe` 的资金比例。
- [ ] 【未验收：缺过期学习对照样本】超过 `valid_until` 的 action-value 不得进入 PM 当日 `candidate_quality`、Rank 或仓位学习输入。
- [ ] 【部分验收：已有最新亏损后的 probe 决策，缺强实时证据升级对照】最新完整周期亏损后，当日强实时证据仍必须能够进入非零差异化 `probe`。
- [ ] 【未验收：缺后续消费样本】完整 episode 的 `worst_return_on_notional` 必须形成排名尾部学习信号。
- [ ] 【未验收：缺人民币盈亏与收益率冲突样本】正的人民币 `reward_sum/reward_mean` 不得在 `mean_return_on_notional<=0` 时赋予正向仓位学习资格。
- [ ] 【未验收：缺人民币盈亏与收益率冲突样本】负的人民币 `reward_sum/reward_mean` 不得在 `mean_return_on_notional>0` 时形成负向仓位学习方向。
- [ ] 【未验收：缺成熟放大样本】`alpha_scale` 必须要求成熟 action-value 的 `mean_return_on_notional>0`。
- [ ] 【未验收：缺策略失效真实样本】负期望策略失效统计必须只使用同 ticker、side、setup 和标准化 market_regime 的最近最多 5 个完整 episode。
- [ ] 【未验收：缺策略失效样本下限对照】同 ticker、side、setup 和标准化 market_regime 的最近完整 episode 数少于现有 `cap_min_samples` 时，不得因聚合负期望规则进入 `capped`。
- [ ] 【未验收：缺策略失效真实样本】同 ticker、side、setup 和标准化 market_regime 的最近完整 episode 数达到现有 `cap_min_samples`、平均 `return_on_notional<0` 且当前精确 Profile 尚未处于 `capped/rejected` 时，当前精确 Profile 必须进入 `capped`。
- [ ] 【未验收：缺策略失效真实样本】当前精确 Profile 自身未触发负期望规则、但聚合策略失效作用域触发 `capped` 时，必须记录 `reason=strategy_failure_scope_recent_return_on_notional_negative`。
- [ ] 【未验收：缺跨 horizon/data_combo 策略失效样本】同 ticker、side、setup 和标准化 market_regime 的不同 horizon 或 data_combo 完整 episode 必须共同参与聚合负期望判定。
- [ ] 【未验收：缺跨作用域失效对照样本】一个聚合策略失效作用域进入 `capped` 不得使其他 ticker、side、setup 或标准化 market_regime 同步降级。
- [ ] 【未验收：缺 capped 后强证据样本】聚合策略失效作用域进入 `capped` 后，当日强实时证据仍必须能够进入差异化 `probe` 重新验证。
- [ ] 【未验收：缺策略恢复真实样本】聚合策略失效作用域最近完整 episode 平均 `return_on_notional` 不再为负且不存在其他 capped 原因时，不得继续使用 `strategy_failure_scope_recent_return_on_notional_negative` 维持 `capped`。
- [ ] 【未验收：缺策略恢复真实样本】策略恢复后的 `real/scale` 资格必须继续使用现有样本数、置信度、Profile 生命周期和正收益门槛。

## Researcher 正式学习身份

- [ ] 【未验收：新代码尚未回测】三类分析师每个交易日 AEC 必须同时落地 1、3、5、10 日预测，概率和为 1 且收益区间顺序合法。
- [ ] 【未验收：新代码尚未回测】预测只能在对应结算交易日到达后写入 `analyst_forecast_evaluation`，未到期记录数必须为 0。
- [ ] 【未验收：新代码尚未回测】到期评价必须形成品种、板块和全局层级 `analyst_performance`，精确层样本不足时仍要有合法上层校准摘要。
- [ ] 【未验收：新代码尚未回测】`setup_type_performance` 必须同时产生精确、去状态、跨品种和全局层级行，不能因细粒度样本碎片化继续保持空表。

- [ ] 【未验收：待新实验回测】原开仓 FAC 完整周期收益峰值为正、当前 `cycle_return_on_notional<=0` 且当日同向证据再验证失败时，所有仓位类型必须通过现有 PM 生命周期路径减仓并记录 `reason=profit_giveback_revalidation_failed`。
- [ ] 【未验收：待新实验回测】原开仓 FAC 完整周期收益峰值为正、当前 `cycle_return_on_notional<=0` 但当日同向证据再验证通过时，不得触发 `profit_giveback_revalidation_failed` 退出或减仓。
- [ ] 【未验收：缺 real/scale 持仓样本】real/scale 开仓 FAC 不得被 Trader 按 probe 期限规则识别。
- [ ] 【部分验收：已有完整 episode 与后续学习检索，缺同作用域精确命中和 fallback 对照】完整 episode 生成的正式 action-value 必须按同一完整 FAC 身份被 PM 精确命中。

## Researcher 探索假设

- [ ] 【未验收：缺品种级探索假设未来样本】品种级探索假设验证必须只使用生成日之后同 ticker、side、setup 和标准化 market_regime 的完整真实 episode。
- [ ] 【未验收：缺板块级探索假设未来样本】板块级探索假设验证必须只使用生成日之后同 sector、side、setup 和标准化 market_regime 的完整真实 episode。
- [ ] 【未验收：缺跨 horizon 探索假设未来样本】仅 horizon 不同不得使其他验证作用域相同的未来完整真实 episode 被排除。
- [ ] 【未验收：缺探索假设未来样本】探索假设验证收益必须使用手续费后 `return_on_notional`。
- [ ] 【未验收：缺探索假设晋升样本】未来同作用域样本达到现有下限、平均 `return_on_notional>0` 且最新完整周期 `return_on_notional>=0` 时，探索假设必须进入 `validated`。
- [ ] 【未验收：缺探索假设最新亏损样本】未来同作用域样本平均 `return_on_notional>0` 但最新完整周期 `return_on_notional<0` 时，旧 `validated` 结论必须立即进入 `monitoring`。
- [ ] 【未验收：缺探索假设负期望样本】未来同作用域样本达到现有下限且平均 `return_on_notional<=0` 时，探索假设必须进入 `rejected`。
- [ ] 【未验收：缺探索假设恢复样本】`monitoring/rejected` 探索假设的后续未来样本达到现有下限、平均 `return_on_notional>0` 且最新完整周期 `return_on_notional>=0` 时，必须能够恢复为 `validated`。
- [ ] 【未验收：缺已验证假设后续分析样本】只有 `validated` 探索假设可以进入下一交易日分析师先验。
- [ ] 【未验收：缺探索假设验证样本】探索假设表层 `sample_count` 必须等于已匹配的未来完整验证 episode 数。
- [ ] 【未验收：缺方向匹配探索假设样本】分析师检索探索假设时必须使用当日确定的 side 进行匹配。
- [ ] 【未验收：缺已验证假设后续分析样本】进入分析师提示词的探索假设必须明确记录 side、setup、horizon 和 market_regime 作用域。

## RAG 记忆质量

- [ ] 【部分验收：已有有/无新增 episode 的跨日记录，缺逐日摘要版本重放】完整 episode 样本数和最新结束日均未变化时，不得刷新 `analyst_performance`。
- [ ] 【部分验收：已有有/无新增 episode 的跨日记录，缺逐日摘要版本重放】完整 episode 样本数和最新结束日均未变化时，不得新增或刷新 `analyst_learning_digest`。
- [ ] 【未验收：缺摘要有效期样本】`analyst_learning_digest.valid_until` 必须从其最新完整 episode 的真实结束日计算。

## 未交易 fast-candidate

- [ ] 【未验收：当前未生成 `fast_candidate_alpha` 政策】每条新生成的 `fast_candidate_alpha` 必须来自 `missed_alpha_accountability` 学习事件。
- [ ] 【未验收：缺真实样本】`fast-candidate` 晋升必须使用固定 5 日观察窗口。
- [ ] 【未验收：缺真实样本】`fast-candidate` 晋升只能使用同作用域样本。
- [ ] 【未验收：缺真实样本】同作用域存在固定 5 日完整正样本时，该正样本必须计入 `fast-candidate` 晋升统计。
- [ ] 【未验收：缺真实样本】同作用域存在固定 5 日完整负样本时，该负样本必须计入 `fast-candidate` 晋升统计。
- [ ] 【未验收：缺真实样本】`execution_reason=fac_invalidated_before_entry` 或 `fac_expired_before_entry` 的未交易记录不得支持 `fast_candidate_alpha` 晋升。
- [ ] 【未验收：缺真实样本】不再满足条件的 active 政策必须停用。
- [ ] 【未验收：缺真实停用样本】`fast_candidate_alpha` 停用必须只更新其自身 `missed_alpha_accountability` 来源政策。
- [ ] 【未验收：缺真实样本】多观察周期影子结果只允许用于诊断和 prior-only 研究学习。
- [ ] 【未验收：缺真实样本】多观察周期影子结果不得参与 `fast_candidate_alpha` 晋升统计。
- [ ] 【未验收：缺真实样本】多观察周期影子结果不得生成成熟 `alpha_promotion` 政策。
- [ ] 【未验收：当前未生成成熟 `alpha_promotion` 政策】新生成的成熟 `alpha_promotion` 必须来自真实成交的 `setup_type_performance`。
- [ ] 【未验收：缺后续消费样本】晋升后的政策必须从下一交易日起被 PM 消费。
- [ ] 【未验收：缺后续消费样本】晋升后的政策必须按生成时的同一作用域被 PM 消费。
- [ ] 【未验收：缺后续消费样本】PM 消费的 `fast_candidate_alpha` 必须具有 `missed_opportunity_counterfactual` 来源。
- [ ] 【未验收：缺后续消费样本】PM 消费的 `fast_candidate_alpha` 必须仅具有 probe/小仓复核权限。

## 分析师学习应用

- [ ] 【未验收：缺技术参数校准样本】技术参数校准实际采用的政策编号必须写入现有 `learning_impact_summary`。
- [ ] 【未验收：缺技术参数校准样本】技术参数校准的参数前后值必须写入现有 `learning_impact_summary`。
- [ ] 【未验收：缺严重开仓亏损 episode 样本】开仓 episode 的手续费后 `return_on_notional<=-0.02` 时必须生成 `strict_confirmation_required`。

## 回测结果评价

- [ ] 【部分验收：18 个完整 episode 均已关联 Rank 1—5，但 Rank 4 仅 2 个、Rank 5 仅 1 个】各排名组的完整 episode 样本数必须达到预设下限；样本不足时不得执行排名效果验收。
- [ ] 【部分验收：已按开仓 FAC Rank 关联 18 个 episode，组内样本仍不足】样本达到预设下限后，必须按开仓 FAC 的 `opportunity_rank` 比较手续费后平均 `return_on_notional`。
- [ ] 【未验收：Rank 4—5 样本不足】高排名组的手续费后平均 `return_on_notional` 必须高于低排名组。
- [ ] 【未验收：当前不得执行排名效果验收】高排名组未优于低排名组时，必须判定排名策略未通过效果验收并进入模型诊断。
