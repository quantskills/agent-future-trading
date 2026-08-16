# AgentQuant 工作日志

本日志自 2026 年 08 月 10 日起重新记录。

只记录已经完成的 `.py`、`.yaml`、`.yml` 行为修改或运行配置修改。相关修改完成后再追加记录；纯讨论、方案、排查结论、仅运行测试、纯文档修改、数据或缓存处理、文件改名或删除均不记录。

日志按日期正序分组。每项只简要说明：

- 修改了什么。
- 为什么修改。

==========2026年08月10日==========

（1）[PandaAI扩展数据持久化与日额度硬停止] `api.py`在现有`pandaai_market_cache.db`中新增按接口名和完整参数精确命中的扩展数据缓存，只保存成功响应与确定性空响应；将`500009`转换为稳定`pandaai_daily_quota_exhausted`并阻断同一进程的后续远程请求。`analyst_data_usage.py`让Phase1日线、交易日参考日期和扩展数据预取收到该错误后立即上抛，不再遍历剩余品种。原因：消除跨回测子进程对同一历史扩展参数的重复请求，并阻止额度耗尽后继续消耗请求与等待时间；策略生成、PM、审计、Trader分钟线调用和交易行为均未修改。

==========2026年08月11日==========

（1）[Adaptive policy实际应用归因闭环] 技术参数policy的实际参数变化沿既有signal/AEC→SCC链传入PM，PM评分、仓位和资金控制policy沿既有诊断链进入Step6，统一写入`final_action_contract.learning_used.adaptive_policy_applied`；Researcher的`research_position_feedback.policy_refs_json`只读取该字段并与action-value独立归因。原因：修复policy虽被生产和消费、但最终FAC与研究反馈没有实际应用ID及作用域的断点，单纯检索或未改变结果的policy不再冒充已应用。

（2）[技术参数policy独立精确生产] `research_memory_writers.py`将`technical_parameters`从PM contextual policy共享配额中拆出，独立读取exact-ticker、short-horizon技术绩效并继续写入现有`adaptive_policy_state`；技术分析师仍在原参数边界内消费，`learning_impact_summary`只记录真实改变的参数及policy身份。原因：避免合格技术参数policy被PM共享Top-N配额挤出，同时继续禁止跨品种通配和第二条生产、消费路径。

（3）[候选期限三分析师预测校准] `pm_signal_fusion.py`按候选`expected_horizon_days`映射1/3/5/10日网格，读取technical、fundamental、commodity_news当日同期限完整概率分布，并只用同分析师、期限、信号侧的到期命中率、Brier、市场状态和手续费后收益校准唯一Rank预测分项；`portfolio_manager.py`沿现有SCC身份链传入候选期限。原因：修复旧校准只按分析师主方向筛选、未消费当日多期限预测且排除中性分析师概率的问题，不新增一致性门槛和交易限制。

（4）[探索资金按唯一Rank区间分配] `portfolio_manager.py`让Step4只确定探索资金层、0.8%下限和1.5%上限；`pm_full_market_capital_deployment.py`在八项最终`rank_score`形成后，仅对探索层在该区间计算一次计划保证金并沿现有合约最小手数、总保证金、单品种和净敞口边界确定目标手数。原因：让高Rank探索候选获得更多真实资金、低Rank与负Rank候选保留0.8%探索计划，同时保持real/scale资金层、Trader触发和持仓退出链不变。

==========2026年08月12日==========

（1）[Action-value置信度单次样本计权] `alpha_setup.py`删除动作生命周期置信度形成后的第二次`sample_count/min_samples_deployable`乘法，继续由既有生命周期公式唯一计算样本成熟度。原因：修复样本量被重复折损，导致配置规定的两次精确完整周期长期无法通过`min_action_value_confidence=0.35`的问题。

（2）[Real与Scale样本门槛分离] `portfolio_policy_catalog.yaml`明确`real_trade_min_action_value_samples=2`与`alpha_scale_min_action_value_samples=5`，`portfolio_manager.py`的Step4放大判断改读独立5样本门槛。原因：修复real与scale共用两样本门槛造成低成熟度正向学习直接跨层放大的问题，不改变资金比例、Rank、Trader触发、退出链和硬风险上限。

（3）[Alpha学习重建入口恢复] `research_learning.py`让历史重建继续复用`reviewer_phase4_review.py`现有交易按recommendation分组函数，替换已不存在的helper属性引用。原因：恢复既有`bootstrap_alpha_setup.py`重建入口，使更新后的action-value置信度能从已结算历史重建，同时仍只写`alpha_setup_sample/profile/action_value`三张学习表。

==========2026年08月13日==========

（1）[成熟预测反馈进入既有学习上下文] `analyst_learning_context.py`从现有已成熟的forecast calibration结果读取同一分析师、品种和预测期限的样本数、命中率、Brier、手续费后收益及市场状态，并以有界摘要注入下一次分析师LLM学习上下文；不新增交易权限、交易分支或数据库表。

（2）[预测校准改为带符号的既有Rank分项] `pm_signal_fusion.py`保留概率分布合法性和原有候选生成链，在现有校准强度上使用历史方向技能的符号影响Rank：负技能降低或反转该分析师的Rank贡献，中性分析师仍参与概率分布；未增加一致性门槛、未减少自然交易机会。

（3）[Action-value收益强度进入既有评分] 对已有手续费后`mean_return_on_notional`按配置单位归一化后参与现有action-value学习摘要与评分，增加同品种/方向/期限/`setup_type`跨市场状态优先检索；保留原有宽范围回退，不改变Step5、Trader触发、退出链、保证金上下限或真实交易路径。

==========2026年08月14日==========

（1）[预测评价与PM policy作用域分离] `research_memory_writers.py`取消将`analyst_performance`中的`1d/3d/5d/10d`预测评价期限改写为`short/medium/event_short` policy作用域；现有PM contextual policy只读取原本就以`short/medium/long/event_short`记录的控制绩效，并按最终唯一作用域确定性地只写一条完整policy。原因：数字期限预测评价属于分析师校准和Step5 Rank证据，不能在没有同一语义证据时转换成PM控制policy；同时避免同一唯一键被多名分析师依次覆盖后混合不同记录的样本数、置信度与规则。FAC归因、研究反馈、Rank、Step5、Trader、目标手数和交易路径均未修改。

（2）[技术参数policy精确品种聚合] `research_memory_writers.py`继续由现有`_write_contextual_rule_calibration_state()`独立读取technical、exact-ticker、`short`绩效，不占用PM contextual policy配额；同品种long/short绩效按样本数聚合后只生成一条`side=*`的`technical_parameters` policy，并保留来源绩效ID、方向、样本和收益证据。原因：技术指标参数由品种和期限决定，不能任取单一交易方向的绩效，也不能让同一side-neutral唯一键在循环中被后续行覆盖。

==========2026年08月16日==========

（1）[Setup身份与Alpha成熟作用域修复] `analyst_quality.py`和`learning_identity.py`将`setup_type`、`opportunity_type`、`execution_profile`彻底分离，AEC finalization只按既有技术结构与正式枚举确定唯一canonical setup，不再用机会类型覆盖；`alpha_setup.py`与`research_learning.py`让完整真实episode固定按`ticker/side/setup/horizon/regime`聚合成熟样本，`data_combo`继续保留在未交易、日级审计和执行学习通道。原因：修复旧setup身份塌缩以及完整episode被证据组合切碎，避免已有真实收益样本无法形成可重复、可消费的Alpha，同时不改变候选、交易权限和Trader路径。

（2）[当日预测净收益贯通合法方向与既有Rank] `research_memory_writers.py`在原预测绩效摘要中增加预测侧预期收益、实际收益和往返手续费；`pm_signal_fusion.py`用三名分析师同期限当日预期收益、历史偏差、Brier、方向命中率、市场状态和手续费形成`current_expected_return_after_fee`，并作为原`calibrated_forecast_value`输入；`pm_ticker_side_selection.py`只在SCC已有的双侧合法候选且校准成熟时选择净预期收益较高侧，单侧、冲突和冷启动保持原SCC语义。`portfolio_manager.py`在Step2前读取两侧精确setup profile，使既有Rank的产品历史分项实际取得证据。原因：修复当日预期收益计算后未贯穿方向和Rank、以及精确setup历史读取发生在方向已锁定之后的断点；`rank_score_policy.yaml`的层级基分、八项积分和资金机制均未修改。

（3）[探索假设单作用域合并与未来验证] `research_learning.py`和`research_memory_writers.py`把探索假设作用域统一为`ticker或sector/side/setup/horizon/regime`，同一作用域只保留一个活动假设并合并后续支持episode；状态验证同时匹配期限与canonical setup，且仍只允许生成日之后的完整真实episode推进`monitoring/validated/rejected`。原因：消除重复假设和作用域错配造成的长期无法验证；未交易反事实仍只进入既有错失机会研究，不获得正式假设验证和资金放大权限。

（4）[预测期限经济复评接入现有持仓链] `portfolio_manager.py`在原开仓预测期限到达时读取当前侧、同一校准口径的手续费后净预期收益：成熟净值非负不形成期限失效，转负才作为现有持仓减仓/退出链的复评证据。原因：让开仓Rank与到期复评使用同一经济输入，不新增机械时间止损、不改变技术失效、基本面反向、风险上限和既有退出路径。

（5）[预测净收益方向偏差修正] `pm_signal_fusion.py`将历史预测误差固定为标的收益口径，并在计算多头、空头当日手续费后净预期收益时按目标侧分别转换符号；缺少同口径标的预期与实现收益时保持零偏差。原因：修复预测方向收益偏差被多空共用、反方向经济值被错误校准，同时保持既有Rank积分项、权重和资金机制不变。

（6）[双侧经济比较成熟性修正] `pm_ticker_side_selection.py`仅在SCC已有的两侧合法候选都具备成熟校准时比较净预期收益；任一侧冷启动时保持SCC原方向。原因：防止冷启动侧以零收益覆盖已成熟负值侧，不新增候选、门槛、交易禁令或资金分配路径。

（7）[板块探索假设作用域锚点修正] `research_learning.py`生成探索假设作用域键时使用具体ticker，否则使用sector，再拼接side/setup/horizon/regime。原因：修复`ticker=*`时不同板块假设被错误合并，保持同作用域合并与未来完整真实episode验证机制不变。

（8）[Direction watchlist语义边界与新风险契约硬门] `analyst_quality.py`和`learning_identity.py`规定`direction_watchlist`只表示机会观察状态，不能写成技术`setup_type`；技术AEC在`signal_evidence_collection.py`进入watch/probe/tradeable前必须具备正式技术setup，`pm_contract_self_check.py`要求新增风险FAC使用正式可执行setup。原因：阻止观察标签污染setup学习身份或穿过新风险契约，同时不修改Rank积分、资金分配、Trader触发和退出链。
