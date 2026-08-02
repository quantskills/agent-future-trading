# AgentQuant 回测后待验收清单

只记录代码已经实现、仍等待真实回测事实验收的功能与效果项目。代码测试及只读重放不代替真实回测验收；代码没有实现的功能不得写入本清单，策略效果项目必须明确作为回测验收目标，不视为代码预先保证。

当前证据基线：回测记录已清空。下列全部项目均须使用从 2025-07-01 开始的空库新回测事实重新验收。

## 系统与交易链

- [ ] 【未验证：缺真实样本】`forced-risk` 必须保持独立来源，不污染策略交易语义。
- [ ] 【未验证：待空库新回测】新机会的 setup 专属政策必须按当天 SCC/FAC 身份检索。
- [ ] 【未验证：待空库新回测】原持仓的 setup 专属政策必须按原开仓 FAC 身份检索。
- [ ] 【未验证：待空库新回测】反向日必须仅按原开仓 FAC 结束旧持仓周期。
- [ ] 【未验证：待空库新回测】反向日不得在同一原子决策中直接建立反向新仓。
- [ ] 【未验证：待空库新回测】下一交易日反向机会仍成立时必须使用当日新 FAC。
- [ ] 【未验证：待空库新回测】下一交易日按反向机会开仓时必须建立新学习周期。
- [ ] 【未验证：缺换约样本】换约日同向续仓必须延续原学习周期。
- [ ] 【未验证：缺换约样本】换约日同向续仓必须继承原开仓 FAC 身份。
- [ ] 【未验证：缺换约样本】换约日只平旧合约时必须结束原学习周期。
- [ ] 【未验证：缺换约后新开仓样本】换约只平旧后的下一次策略开仓必须建立新学习周期。
- [ ] 【未验证：缺换约后新开仓样本】换约只平旧后的下一次策略开仓必须使用新 FAC。
- [ ] 【未验证：待空库新回测】原开仓 FAC 失效时必须优先直接退出。
- [ ] 【未验证：待空库新回测】结构止损触发时必须优先直接退出。
- [ ] 【未验证：待空库新回测】初始 ATR 止损触发时必须优先直接退出。
- [ ] 【未验证：缺真实样本】原 FAC 未失效、普通持仓浮亏达到 2% 且同向证据再验证失败时，必须减仓 50%。
- [ ] 【未验证：待空库新回测】原 FAC 未失效、普通持仓浮亏达到 4% 且同向证据再验证失败时，必须全部退出。
- [ ] 【未验证：缺真实样本】普通持仓的同向证据再验证通过时，不得触发普通亏损减仓。
- [ ] 【未验证：缺真实样本】普通持仓的同向证据再验证通过时，不得触发普通亏损退出。
- [ ] 【未验证：缺增强确认真实样本】只有 `trigger_confirmation_adjustment=stronger_confirmation_required/strict_confirmation_required` 的入场 FAC 才能进入增强确认路径。
- [ ] 【未验证：缺增强确认真实样本】增强确认路径必须使用初次触发后的下一根完整 15 分钟线验证价格延续。
- [ ] 【未验证：缺增强确认真实样本】增强确认路径的初次触发线和延续确认线成交量必须都大于零。
- [ ] 【未验证：缺增强确认真实样本】存在可比前序成交量时，增强确认路径两根确认线的平均成交量必须不低于最近最多 4 根前序完整 15 分钟线平均成交量。
- [ ] 【未验证：缺增强确认低量样本】增强确认路径量能不足时不得执行该次入场。
- [ ] 【未验证：缺标准确认对照样本】`standard_confirmation_supported` 入场不得被增强量能确认门限制。
- [ ] 【未验证：缺增强确认真实样本】增强确认结果只能决定原入场 FAC 的执行、等待或跳过，不得生成新的交易动作。
- [ ] 【未验证：缺增强确认真实样本】增强确认路径不得生成 Trader 盘中策略退出。

## PM、排名与资金

- [ ] 【未验证：缺真实样本】严格匹配的成熟正向学习与合格当日证据必须能够把候选从 `probe` 升级至 `real/scale`。
- [ ] 【未验证：缺真实样本】`real/scale` 候选必须按现有资金分层获得高于 `probe` 的资金比例。
- [ ] 【未验证：待空库新回测】`probe` 的 `target_margin_ratio` 必须按 `0.8% + candidate_quality × (1.5% - 0.8%)` 计算。
- [ ] 【未验证：缺同日差异候选样本】`candidate_quality` 不同的 `probe` 必须形成对应不同的 `target_margin_ratio`。
- [ ] 【未验证：缺冷启动差异候选样本】完整作用域真实周期为零时，`probe` 不得仅因零样本被机械固定为 0.8%。
- [ ] 【未验证：缺冷启动差异候选样本】完整作用域真实周期为零时，`probe` 不得仅因零样本被机械固定为 1.5%。
- [ ] 【未验证：待空库新回测】`candidate_quality_components` 必须只由当日 `opportunity_score`、`trigger_quality` 和 `invalidation_quality` 构成。
- [ ] 【未验证：缺过期学习对照样本】超过 `valid_until` 的 action-value 不得进入 PM 当日 `candidate_quality`、Rank 或仓位学习输入。
- [ ] 【未验证：缺相同学习不同实时证据样本】有效学习相同而当日证据强度不同的候选必须形成不同的 `candidate_quality`。
- [ ] 【未验证：缺相同实时证据不同学习样本】当日证据相同而有效学习结果不同的候选必须形成对应不同的 `candidate_quality`。
- [ ] 【未验证：缺最新亏损后续决策样本】同完整作用域最新完整真实周期 `return_on_notional<0` 时必须标记 `latest_complete_episode_loss=true`。
- [ ] 【未验证：缺最新亏损后续决策样本】`latest_complete_episode_loss=true` 时当日同方向 `positive_learning` 排名分项必须为零。
- [ ] 【未验证：缺最新亏损后续决策样本】`latest_complete_episode_loss=true` 时同作用域正向 Profile 加分必须为零。
- [ ] 【未验证：缺最新亏损后续决策样本】`latest_complete_episode_loss=true` 时旧正向 action-value 不得继续生成 positive open seed。
- [ ] 【未验证：缺最新亏损后续决策样本】`latest_complete_episode_loss=true` 时旧正向 action-value 不得继续赋予 `real/scale` 正向放大资格。
- [ ] 【未验证：缺最新亏损且强当日证据样本】最新完整周期亏损后，当日强实时证据仍必须能够进入非零差异化 `probe`。
- [ ] 【未验证：待空库新回测】正的 `mean_return_on_notional` 必须形成正向排名学习信号。
- [ ] 【未验证：待空库新回测】负的 `mean_return_on_notional` 必须形成负向排名学习信号。
- [ ] 【未验证：缺后续消费样本】完整 episode 的 `worst_return_on_notional` 必须形成排名尾部学习信号。
- [ ] 【未验证：待空库新回测】人民币 `reward_sum` 不得进入排名学习分。
- [ ] 【未验证：待空库新回测】人民币 `reward_mean` 不得进入排名学习分。
- [ ] 【未验证：待空库新回测】PM 仓位学习的正负方向必须由完整 episode 的 `mean_return_on_notional` 决定。
- [ ] 【未验证：缺人民币盈亏与收益率冲突样本】正的人民币 `reward_sum/reward_mean` 不得在 `mean_return_on_notional<=0` 时赋予正向仓位学习资格。
- [ ] 【未验证：缺人民币盈亏与收益率冲突样本】负的人民币 `reward_sum/reward_mean` 不得在 `mean_return_on_notional>0` 时形成负向仓位学习方向。
- [ ] 【未验证：缺成熟放大样本】`alpha_scale` 必须要求成熟 action-value 的 `mean_return_on_notional>0`。
- [ ] 【未验证：缺策略失效真实样本】策略失效统计必须只使用同 ticker、side、setup、horizon 和 market_regime 的最近最多 5 个完整 episode。
- [ ] 【未验证：缺策略失效样本下限对照】最近完整 episode 数少于现有 `cap_min_samples` 时，不得因近期收益率规则进入 `capped`。
- [ ] 【未验证：缺策略失效真实样本】最近完整 episode 数达到现有 `cap_min_samples` 且平均 `return_on_notional<0` 时必须进入 `capped`。
- [ ] 【未验证：缺策略失效真实样本】近期收益率规则触发 `capped` 时必须记录 `reason=same_scope_recent_return_on_notional_negative`。
- [ ] 【未验证：缺跨作用域失效对照样本】一个完整作用域进入 `capped` 不得使其他 ticker、side、setup、horizon 或 market_regime 同步降级。
- [ ] 【未验证：缺 capped 后强证据样本】同作用域进入 `capped` 后，当日强实时证据仍必须能够进入差异化 `probe` 重新验证。
- [ ] 【未验证：缺策略恢复真实样本】最近完整 episode 平均 `return_on_notional` 不再为负时，不得继续使用 `same_scope_recent_return_on_notional_negative` 维持 `capped`。
- [ ] 【未验证：缺策略恢复真实样本】策略恢复后的 `real/scale` 资格必须继续使用现有样本数、置信度、Profile 生命周期和正收益门槛。
- [ ] 【未验证：缺跨品种样本】在作用域匹配级别、样本数、置信度和时效相同的条件下，相同完整周期收益率不得因品种不同而产生不同学习分。
- [ ] 【未验证：缺不同合约乘数样本】在作用域匹配级别、样本数、置信度和时效相同的条件下，相同完整周期收益率不得因合约乘数不同而产生不同学习分。
- [ ] 【未验证：缺不同人民币盈亏样本】在作用域匹配级别、样本数、置信度和时效相同的条件下，相同完整周期收益率不得因人民币盈亏绝对额不同而产生不同学习分。
- [ ] 【未验证：缺不同手数样本】在作用域匹配级别、样本数、置信度和时效相同的条件下，相同完整周期收益率不得因交易手数不同而产生不同学习分。

## Researcher 正式学习身份

- [ ] 【未验证：待空库新回测】成交型 hold 学习必须继承原开仓 FAC 的完整身份。
- [ ] 【未验证：待空库新回测】成交型 reduce 学习必须继承原开仓 FAC 的完整身份。
- [ ] 【未验证：待空库新回测】成交型 exit 学习必须继承原开仓 FAC 的完整身份。
- [ ] 【未验证：待空库新回测】成交型 execution 学习必须继承原开仓 FAC 的完整身份。
- [ ] 【未验证：待空库新回测】execution 学习必须由 `execution_retrieval_key` 区分具体执行方式。
- [ ] 【未验证：待空库新回测】完整 `trade_episode_memory` 必须继承原开仓 FAC 的完整身份。
- [ ] 【未验证：待空库新回测】Profile 必须继承其正式上游样本的完整 FAC 身份。
- [ ] 【未验证：待空库新回测】action-value 必须继承其正式上游样本的完整 FAC 身份。
- [ ] 【未验证：待空库新回测】position feedback 必须继承其正式上游样本的完整 FAC 身份。
- [ ] 【未验证：待空库新回测】setup 绩效必须继承其正式上游样本的完整 FAC 身份。
- [ ] 【未验证：待空库新回测】成交结果产生的亏损政策必须继承其正式上游样本的完整 FAC 身份。
- [ ] 【未验证：待空库新回测】未交易学习必须继承对应当日 FAC 的完整身份。
- [ ] 【未验证：待空库新回测】FAC 身份不完整时不得写入对应的正式学习记录。
- [ ] 【未验证：待空库新回测】完整 episode 生成的正式 action-value 必须按同一完整 FAC 身份被 PM 精确命中。

## 市场状态规范化

- [ ] 【未验证：待空库新回测】FAC 必须保存小写下划线格式的 `market_regime`。
- [ ] 【未验证：待空库新回测】正式学习落库必须保存小写下划线格式的 `market_regime`。
- [ ] 【未验证：待空库新回测】正式学习查询必须使用小写下划线格式的 `market_regime`。
- [ ] 【未验证：待空库新回测】同一市场状态不得因空格与下划线差异降级为 fallback。

## 未交易 fast-candidate

- [ ] 【未验证：待空库新回测】`alpha_setup_profile` 的 `candidate/watchlist` 状态不得生成 `fast_candidate_alpha` 政策。
- [ ] 【未验证：待空库新回测】每条新生成的 `fast_candidate_alpha` 必须来自 `missed_alpha_accountability` 学习事件。
- [ ] 【未验证：缺真实样本】`fast-candidate` 晋升必须使用固定 5 日观察窗口。
- [ ] 【未验证：缺真实样本】`fast-candidate` 晋升只能使用同作用域样本。
- [ ] 【未验证：缺真实样本】同作用域存在固定 5 日完整正样本时，该正样本必须计入 `fast-candidate` 晋升统计。
- [ ] 【未验证：缺真实样本】同作用域存在固定 5 日完整负样本时，该负样本必须计入 `fast-candidate` 晋升统计。
- [ ] 【未验证：待空库新回测】入场前已经失效的未交易记录不得支持政策晋升。
- [ ] 【未验证：缺真实样本】已经到期的未交易记录不得支持政策晋升。
- [ ] 【未验证：待空库新回测】缺少 FAC 可执行依据的未交易记录不得支持政策晋升。
- [ ] 【未验证：缺真实样本】不再满足条件的 active 政策必须停用。
- [ ] 【未验证：缺真实停用样本】`fast_candidate_alpha` 停用必须只更新其自身 `missed_alpha_accountability` 来源政策。
- [ ] 【未验证：缺真实样本】多观察周期影子结果只允许用于诊断和 prior-only 研究学习。
- [ ] 【未验证：缺真实样本】多观察周期影子结果不得参与 `fast_candidate_alpha` 晋升统计。
- [ ] 【未验证：缺真实样本】多观察周期影子结果不得生成成熟 `alpha_promotion` 政策。
- [ ] 【未验证：待空库新回测】未交易反事实结果不得生成成熟 `alpha_promotion` 政策。
- [ ] 【未验证：待空库新回测】新生成的成熟 `alpha_promotion` 必须来自真实成交的 `setup_type_performance`。
- [ ] 【未验证：缺后续消费样本】晋升后的政策必须从下一交易日起被 PM 消费。
- [ ] 【未验证：缺后续消费样本】晋升后的政策必须按生成时的同一作用域被 PM 消费。
- [ ] 【未验证：缺后续消费样本】PM 消费的 `fast_candidate_alpha` 必须具有 `missed_opportunity_counterfactual` 来源。
- [ ] 【未验证：缺后续消费样本】PM 消费的 `fast_candidate_alpha` 必须仅具有 probe/小仓复核权限。

## 分析师学习应用

- [ ] 【未验证：待空库新回测】每名分析师实际采用的提示词学习记录编号必须写入现有 `learning_impact_summary`。
- [ ] 【未验证：待空库新回测】每名分析师的 `prompt_calibration_applied` 必须真实反映是否采用了提示词学习记录。
- [ ] 【未验证：缺真实采用样本】每名分析师实际参与确定性证据校准的学习记录编号必须写入现有 `learning_impact_summary`。
- [ ] 【未验证：缺真实采用样本】每名分析师的 `evidence_calibration_applied` 必须真实反映是否执行了确定性证据校准。
- [ ] 【未验证：缺技术参数校准样本】技术参数校准实际采用的政策编号必须写入现有 `learning_impact_summary`。
- [ ] 【未验证：缺技术参数校准样本】技术参数校准的参数前后值必须写入现有 `learning_impact_summary`。
- [ ] 【未验证：待空库新回测】上述 `learning_impact_summary` 必须随现有 `action_evidence_contract` 落入持久化 signal artifact。
- [ ] 【未验证：缺完整 episode 后续分析样本】分析师安全投影必须包含同完整作用域 `mean_return_on_notional`。
- [ ] 【未验证：缺完整 episode 后续分析样本】分析师安全投影必须包含 `latest_complete_episode_return_on_notional`。
- [ ] 【未验证：缺完整 episode 后续分析样本】分析师安全投影必须包含 `latest_complete_episode_date`。
- [ ] 【未验证：缺完整 episode 后续分析样本】分析师安全投影必须包含 `latest_complete_episode_outcome`。
- [ ] 【未验证：缺完整 episode 后续分析样本】正式开仓 episode 的 `learning_economics_basis` 必须为 `after_fee_return_on_notional`。
- [ ] 【未验证：缺最新亏损后续分析样本】同完整作用域最新完整周期亏损后，分析师 `positive_strength` 必须为0。
- [ ] 【未验证：缺最新亏损后续分析样本】同完整作用域最新完整周期亏损后，分析师 `negative_strength` 必须大于0。
- [ ] 【未验证：缺最新亏损后续分析样本】同完整作用域最新完整周期亏损后，`signal_calibration.calibration_bias` 必须为 `negative_evidence_calibration`。
- [ ] 【未验证：缺最新亏损后续分析样本】同完整作用域最新完整周期亏损后，`signal_calibration.positive_amplification_suspended` 必须为真。
- [ ] 【未验证：缺最新亏损与旧正向Profile并存样本】同完整作用域最新完整周期亏损后，旧正向 Profile 不得进入分析师正向校准。
- [ ] 【未验证：缺跨品种同收益率分析样本】学习质量参数相同时，相同 `mean_return_on_notional` 不得因品种不同产生不同分析师学习强度。
- [ ] 【未验证：缺不同乘数同收益率分析样本】学习质量参数相同时，相同 `mean_return_on_notional` 不得因合约乘数不同产生不同分析师学习强度。
- [ ] 【未验证：缺不同人民币盈亏同收益率分析样本】学习质量参数相同时，相同 `mean_return_on_notional` 不得因人民币 `reward_mean/net_pnl` 不同产生不同分析师学习强度。
- [ ] 【未验证：缺不同手数同收益率分析样本】学习质量参数相同时，相同 `mean_return_on_notional` 不得因交易手数不同产生不同分析师学习强度。
- [ ] 【未验证：缺跨品种同收益率触发样本】相同开仓 episode `return_on_notional` 不得因品种不同产生不同 `trigger_confirmation_adjustment`。
- [ ] 【未验证：缺不同乘数同收益率触发样本】相同开仓 episode `return_on_notional` 不得因合约乘数不同产生不同 `trigger_confirmation_adjustment`。
- [ ] 【未验证：缺不同人民币盈亏同收益率触发样本】相同开仓 episode `return_on_notional` 不得因人民币盈亏不同产生不同 `trigger_confirmation_adjustment`。
- [ ] 【未验证：缺不同手数同收益率触发样本】相同开仓 episode `return_on_notional` 不得因交易手数不同产生不同 `trigger_confirmation_adjustment`。
- [ ] 【未验证：缺开仓盈利 episode 样本】开仓 episode 的手续费后 `return_on_notional>0` 时必须生成 `standard_confirmation_supported`。
- [ ] 【未验证：缺普通开仓亏损 episode 样本】开仓 episode 的手续费后 `-0.02<return_on_notional<0` 时必须生成 `stronger_confirmation_required`。
- [ ] 【未验证：缺严重开仓亏损 episode 样本】开仓 episode 的手续费后 `return_on_notional<=-0.02` 时必须生成 `strict_confirmation_required`。
- [ ] 【未验证：缺分析师学习作用样本】人民币 `reward_mean/net_pnl` 不得进入开仓 episode 的分析师学习强度。
- [ ] 【未验证：缺开仓 episode 学习落库样本】人民币 `net_pnl` 必须继续保留为开仓 episode 的审计事实。
- [ ] 【未验证：缺分析师学习作用样本】分析师学习校准不得生成交易方向。
- [ ] 【未验证：缺分析师学习作用样本】分析师学习校准不得生成交易手数。
- [ ] 【未验证：缺分析师学习作用样本】分析师学习校准不得生成保证金比例。
- [ ] 【未验证：缺分析师学习作用样本】分析师学习校准不得生成交易权限。
- [ ] 【未验证：缺分析师学习作用样本】分析师学习校准不得新增 PM Rank。
- [ ] 【未验证：缺分析师学习作用样本】分析师学习校准不得新增 Trader 执行路径。
- [ ] 【未验证：缺分析师学习作用样本】分析师学习校准不得新增盘中退出路径。

## 回测结果评价

- [ ] 【未验证：完整 episode 样本不足】各排名组的完整 episode 样本数必须达到预设下限；样本不足时不得执行排名效果验收。
- [ ] 【未验证：完整 episode 样本不足】样本达到预设下限后，必须按开仓 FAC 的 `opportunity_rank` 比较手续费后平均 `return_on_notional`。
- [ ] 【未验证：完整 episode 样本不足】高排名组的手续费后平均 `return_on_notional` 必须高于低排名组。
- [ ] 【未验证：完整 episode 样本不足】高排名组未优于低排名组时，必须判定排名策略未通过效果验收。
- [ ] 【未验证：完整 episode 样本不足】排名策略未通过效果验收时，必须进入模型诊断。
