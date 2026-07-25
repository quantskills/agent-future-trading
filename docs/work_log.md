# AgentQuant 工作日志

本日志自 2026 年 07 月 23 日起重新记录。

只记录已经完成的 `.py`、`.yaml`、`.yml` 行为修改或运行配置修改。相关修改完成后再追加记录；纯讨论、方案、排查结论、仅运行测试、纯文档修改、数据或缓存处理、文件改名或删除均不记录。

日志按日期正序分组。每项只简要说明：

- 修改了什么。
- 为什么修改。

==========2026年07月23日==========

（1）[分周期证据与canonical入场触发] `signal_evidence_collection.py`、`evidence_fusion_semantics.py`、`pm_signal_fusion.py` 按technical短期入场、fundamental中期方向/持仓、news事件修正融合证据；中性及跨周期反向证据不再通过共识、缺失或冲突字段稀释短期技术入场，后者仍作为持仓/放大风险保留；`execution_trigger_semantics.py`、`trader_intraday_execution.py` 让条件breakout只在严格越界时触发，并把条件pullback收口为已完成15分钟线的扩张、回踩、重新站回序列。原因：避免不同周期证据互相稀释或伪冲突，并修复碰线和单点VWAP位置被误当成有效条件入场。

（2）[Step4资金层、持仓生命周期与Step5唯一rank] `portfolio_manager.py` 在Step5前以冻结canonical学习池和当日证据确定probe/real/alpha-scale及层内连续比例，删除对未来rank的升层依赖，要求alpha-scale具有合格同向基本面支持，并在基本面反向时把新增风险限制为probe；现有持仓通过原transaction推荐追溯开仓FAC，按结算交易日和原期限管理，原FAC的持仓数值/ATR失效或当前技术反转可退出，中期基本面反向可减仓，无真实失效时不因缺少新trigger自动减仓；`pm_full_market_capital_deployment.py` 只按七项有符号总分和ticker排序，`rank_score_policy.yaml` 将层级积分键统一为`alpha_scale/real_budget/exploration_probe`。原因：让学习验证过且具备中期方向支持的机会真实进入更高资金层，同时保持rank只负责投资价值顺序、负分probe可交易、非新增风险无伪rank和既有硬资金边界。

（3）[完整持仓周期episode] `research_memory_writers.py`、`research_learning.py` 按strategy成交重放`0->持仓->完全归零`周期，归零后为既有物理pair补齐AEC/SCC/FAC、成交、结算、证据和失效变化轨迹，并用归零日刷新正式profile/action-value；各pair原收益、手续费、close date、trade count、去重和反馈ID保持不变。原因：防止分批减仓在仓位未结束时提前形成open/add学习，并保证完整周期结果只能从下一交易日起消费。

（4）[入场作废、持仓失效与rank层级收口] `schema.py`、`prompt.py`、`analyst_structured_output.py`、`analyst_quality.py`、`analyst_output_finalization.py`、`signal_evidence_collection.py`、`execution_trigger_semantics.py`、`contracts.py` 将canonical `invalidation_condition+invalidation_level+valid_until`固定为首次成交前作废链，并新增唯一 `position_invalidation_level`承载成交后持仓失效；`pm_signal_fusion.py`、`pm_invalidation_policy.py`、`portfolio_manager.py`、`pm_contract_builder.py`、`pm_contract_self_check.py` 分开验证两类边界，持仓只追溯原FAC的position/ATR/期限，Step4不依赖rank；`pm_full_market_capital_deployment.py`与`rank_score_policy.yaml`将层级分改为scale/real/probe=6/3/0、当日SCC trigger权重0.08，历史trigger只留在open/add学习，并只按有符号总分和ticker生成预算顺序；`auditor.py`校验同源canonical入场边界，`trader_intraday_execution.py`按分钟时序执行入场作废和到期，`trader.py`删除拟开仓持仓失效观察及策略退出判断。原因：防止入场边界与持仓止损串用、作废后仍成交、Trader同日产生第二策略动作，以及历史trigger重复计分或低层候选越过已学习验证层级。

（5）[7月23日优化最终消费者收尾] `futures_audit.py`、`trader_intraday_execution.py`、`trader.py` 让成交后执行事实只落持仓失效字段，并使直接入场按成交前全部分钟线判断入场作废/到期后立即终态跳过；`portfolio_manager.py`、`pm_invalidation_policy.py` 将同源入场AEC与同方向持仓依据独立选择；`schema.py`、`prompt.py`、`analyst_structured_output.py`、`analyst_quality.py` 独立生产当日确认trigger质量并经既有AEC/SCC重建链传给PM，fundamental、watch和未确认状态固定为零且不借setup或历史结果补值；`pm_full_market_capital_deployment.py` 保留rank顺序计划占用，在最终队列含alpha-scale时使用既有18%强机会预算、否则使用10%普通预算；`research_memory_writers.py` 在派生episode轨迹中分别记录入场作废与持仓失效而不重算经济结果。相关普通回归覆盖真实SCC到PM传递、同向边界、执行终态、预算上限及episode经济结果不变。原因：补齐已确定优化从生产端到最终执行、资金和研究消费者的六项断点，不改变分周期融合、Step4升层、唯一rank、学习算法、单日单动作和硬资金边界。

（6）[Step2方向单源与事件即时终态收口] `portfolio_manager.py` 让probe及positive-open学习种子只消费Step2 `preferred_side`，flat不生成种子，并在Step2后按该方向重建供最终Step4消费的唯一`market_confirmation`；`trader.py` 保留`event_immediate`新增风险的`fac_invalidated_before_entry/fac_expired_before_entry`终态，不再被`force_immediate`盘前基准回退覆盖。原因：防止学习种子绕过Step2方向、Step4消费旧确认对象，以及已经作废或到期的即时事件FAC被执行回退复活。

==========2026年07月24日==========

（1）[持仓退场条件生产与消费闭合] `technical.py` 复用已完成OHLC的既有True Range/EWM口径确定性生成原始ATR14，`prompt.py`、`analyst_structured_output.py`、`analyst_output_finalization.py` 取消LLM的ATR生产权并将系统值写入technical AEC；`portfolio_manager.py`、`pm_invalidation_policy.py`、`pm_contract_self_check.py` 只从已验证SCC重建证据，分别组装同方向technical结构位、方向无关technical ATR和同方向fundamental期限，按盘前参考价与真实开仓成交价两次校验结构位，并让结构位与ATR并行触发次日唯一exit、技术反转exit、基本面中期反向reduce、期限到达仅复评；`trader.py` 删除策略reduce/exit缺合法分钟基准时回退盘前价的伪成交路径。原因：补齐数值退场条件从生产到FAC、次日PM和真实执行的断点，防止自由文本或期限冒充止损、结构位遮蔽ATR以及无真实分钟成交价仍落库，同时保持日频单动作、forced-risk、rank、仓位和资金参数不变。

（2）[退场学习生产消费闭环] `analyst_learning_context.py` 将完整持仓episode的结构位/ATR转为相对距离，删除旧episode自由文本回退，并只把同品种、T+1、canonical完整且内嵌校准版本、`analyst_calibration`作用域均合法的action-value投影为无原始ID和资金字段的提示词摘要；`technical.py`、`commodity_news.py`、`prompt.py` 让既有技术参数学习先有界生效、重算当日指标后再形成退场分析，并让已确认event_immediate复用Router既有盘前参考价生成和校验当日事件结构位；`analyst_structured_output.py`、`analyst_output_finalization.py`禁止fundamental生产数值结构位，继续以当日正式参考价校验technical/当前事件结构位并用确定性ATR覆盖；`pm_contract_builder.py`、`pm_lifecycle_learning_router.py`、`final_action_semantics.py`只在软hold/reduce/exit学习精确命中ID且真实改变最终动作或比例时写入正式FAC学习行，负向hold真实造成减仓时保留原family/lane和ID，结构/ATR止损、技术反转、基本面反向等独立规则只记录当日生命周期结果而不冒领正式学习。原因：让完整周期经验通过分析校准和PM软生命周期决策影响未来退场，同时保持当日硬事实优先、T+1、AEC→SCC→PM边界、单日单动作、rank、资金参数和交易链不变。

（3）[开仓条件生产消费与学习闭环] `alpha_setup.py`、`analyst_learning_context.py` 将完整episode形成的正式入场/触发质量结论投影为technical专用、T+1、同品种、无学习ID/历史绝对价/资金字段的有界校准摘要；`prompt.py`、`analyst_quality.py`、`analyst_output_finalization.py` 让盘前technical只生产canonical待触发setup，固定当前触发与触发质量为未确认/零，并按当日正式参考价校验独立入场作废位；`portfolio_manager.py` 让该待触发setup在软确认控制把方向比例归零后仍复用Step2方向完成Step4学习、候选和层级评估，再形成需要盘中确认的新增风险FAC；`pm_contract_builder.py` 只在execution/profile学习真实改变最终执行profile时列入正式消费，否则保留为拒绝诊断。原因：补齐开仓条件从当日生产、AEC/SCC、Step4、rank/FAC到Trader的消费链，并让历史结果真实校准后续入场而不创建方向、机会、无条件仓位或重复rank计分。

（4）[测试契约去重] `contract_test_fixtures.py` 统一分析师测试的数据使用摘要构造，相关分析师测试删除重复helper；`test_trade_path_incremental_repairs.py` 将无需盘中确认的测试样例限定为合法`event_immediate`，技术profile继续只走盘中确认。原因：减少重复测试实现，并防止测试fixture继续承载当前生产链不可达的技术直入语义。

==========2026年07月25日==========

（1）[hold手数与Step4资金所有权] `portfolio_manager.py` 在最终生命周期为hold且目标比例未变时直接保留`current_lots`，并删除Step4最终计划后的名义仓位二次裁剪及品种日盈亏软控制消费者；`portfolio_policy_catalog.yaml`、`config_normalizer.py`删除`ticker_performance_control`运行配置展开。原因：防止价格变化把同一持仓意图机械换算成减仓，并保证0.8%-1.5%等层内保证金计划只由Step4形成，后续仅受既有硬资金边界和手数取整收缩。

（2）[分析师跨regime安全校准] `analyst_learning_context.py`、`analyst_learning_calibration.py` 在精确regime没有安全正式投影时，才允许同品种、同方向、同周期、T+1且canonical/作用域合法的跨regime摘要以低权重进入分析校准；技术参数overlay继续要求精确regime。原因：修复完整episode因regime字符串变化完全无法迭代分析的问题，同时避免跨状态经验覆盖当前证据或放宽PM学习边界。

（3）[候选质量去重与更强入场确认] `pm_signal_fusion.py`只以已经包含setup、学习/profile及冲突的`opportunity_score`加一次trigger/失效完整性形成`candidate_quality`，`pm_ticker_side_selection.py`停止二次重算；`portfolio_manager.py`从结构化weak-conflict权限或同品种、同方向、同setup、同canonical trigger的精确正式open/add学习生成既有`trigger_confirmation_adjustment`，`pm_contract_builder.py`、`contracts.py`、`execution_trigger_semantics.py`、`trader_intraday_execution.py`将其保真传到Trader，使stronger/strict `breakout/pullback/vwap_confirmed`在原触发后等待下一根完整15分钟线确认再执行。原因：恢复Step4层内比例区分度，并让亏损学习与弱冲突要求真实改变后续入场方式，而不调整rank、ATR、probe参数或单日单动作规则。

（4）[PM RiskGate临时策略合约边界] `portfolio_manager.py`保留非空`provisional_policy_state`在PM RiskGate内部的检索、判定和仓位倍率作用，但写入FAC的`pm_risk_gate_alignment`只投影决策、方向、倍率、原因码和策略版本，不再原样携带内部`diagnostics`、`audit_payload`、说明文本及临时策略记录。原因：修复首条真实RB临时策略生效后，原始研究对象经落地一致性审计进入FAC并被正确的PM事实边界拒绝，导致Phase1保存回滚；不改变临时策略的`probe_only=0.35`实际风控效果、学习、rank或交易规则。

==========2026年07月26日==========

（1）[完整策略周期学习身份与计数] `research_memory_writers.py`、`research_learning.py`、`alpha_setup.py`让换约成交仅参与原策略持仓血缘、手数和经济结果，换约后继续持仓延续原episode、最终归零结束原episode；完整episode的setup、entry trigger和trigger source只继承原开仓FAC，分批平仓pair保留为经济明细，但open/add仅按完整持仓周期形成一次sample、trade count和最终收益。原因：修复换约切断episode、新闻证据改写原技术setup以及分批平仓虚增样本、胜负和尾损的问题，同时保持换约不生成独立学习、forced-risk边界、T+1和反馈ID不变。

（2）[分析师学习生命周期路由] `analyst_learning_context.py`、`analyst_learning_calibration.py`只将canonical open/add正式学习投影到入场校准，并要求当前setup与canonical trigger精确一致后才改变证据质量；hold、reduce/exit和execution/profile不得进入新增风险入场证据校准，无匹配记录保持冷启动。原因：防止仅因品种和方向相同便让持仓、退场或执行经验污染新的入场证据和置信度。

（3）[PM精确setup检索键] `portfolio_manager.py`从已验证SCC重建的当前technical/event执行证据取得canonical setup作为精确学习检索键，禁止历史best profile、Step4 final_state及通配setup冒充精确命中；当前setup缺失时只走既有降级检索。原因：修复正式学习消费全部落入fallback、无法按当前真实setup影响候选质量、升层和rank的问题。

（4）[分析师双路径学习与PM检索失败边界] `analyst_learning_context.py`恢复把经T+1、canonical、作用域和family/lane校验的正式open/add安全摘要按预算写入分析师Prompt，并保留同一批记录供LLM输出后按最终setup/canonical trigger确定性校准；hold、reduce/exit、execution/profile及similar/weak/incomplete仍不得进入新增风险入场校准。`portfolio_manager.py`不再吞掉当前canonical setup正式学习检索异常，只有检索成功且结果为空才按合法冷启动处理。对应测试恢复Prompt安全投影、跨regime低权重投影，并覆盖精确检索异常终止。原因：修复生命周期隔离时误将合格open/add学习从Prompt全部清空，以及真实检索故障被伪装成无学习而阻断分析迭代、候选升层和交易放大。
