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

（1）[Action-value置信度单次样本计权] `alpha_setup.py`删除动作生命周期置信度形成后的第二次`sample_count/min_samples_deployable`乘法，继续由既有生命周期公式唯一计算样本成熟度；对应学习回归证明两样本置信度与生命周期置信度一致、单样本仍不能获得real权限。原因：修复样本量被重复折损，导致配置规定的两次精确完整周期长期无法通过`min_action_value_confidence=0.35`的问题。

（2）[Real与Scale样本门槛分离] `portfolio_policy_catalog.yaml`明确`real_trade_min_action_value_samples=2`与`alpha_scale_min_action_value_samples=5`，`portfolio_manager.py`的Step4放大判断改读独立5样本门槛；对应回归证明4样本只能进入既有`real_budget_entry`，5样本才可在其他现有条件全部成立时进入`alpha_scale_entry`。原因：修复real与scale共用两样本门槛造成低成熟度正向学习直接跨层放大的问题，不改变资金比例、Rank、Trader触发、退出链和硬风险上限。

（3）[Alpha学习重建入口恢复] `research_learning.py`让历史重建继续复用`reviewer_phase4_review.py`现有交易按recommendation分组函数，替换已不存在的helper属性引用；新增内存库真实路径回归验证分笔成交保持同一正式分组。原因：恢复既有`bootstrap_alpha_setup.py`重建入口，使更新后的action-value置信度能从已结算历史重建，同时仍只写`alpha_setup_sample/profile/action_value`三张学习表。

==========2026年08月13日==========

（1）[成熟预测反馈进入既有学习上下文] `analyst_learning_context.py`从现有已成熟的forecast calibration结果读取同一分析师、品种和预测期限的样本数、命中率、Brier、手续费后收益及市场状态，并以有界摘要注入下一次分析师LLM学习上下文；不新增交易权限、交易分支或数据库表。

（2）[预测校准改为带符号的既有Rank分项] `pm_signal_fusion.py`保留概率分布合法性和原有候选生成链，在现有校准强度上使用历史方向技能的符号影响Rank：负技能降低或反转该分析师的Rank贡献，中性分析师仍参与概率分布；未增加一致性门槛、未减少自然交易机会。

（3）[Action-value收益强度进入既有评分] 对已有手续费后`mean_return_on_notional`按配置单位归一化后参与现有action-value学习摘要与评分，增加同品种/方向/期限/`setup_type`跨市场状态优先检索；保留原有宽范围回退，不改变Step5、Trader触发、退出链、保证金上下限或真实交易路径。

（4）[前向回测记录重新分界] 保留数据库中2025-07-01至2025-09-30的训练/基准事实与学习记录；删除本配置2025-10-01以后不完整的事实、派生学习记录及对应10—11月回测/审核运行目录，并在`D:\research\Workshop\agentquant_db_before_oct_reset_20260813.db`保存完整回滚副本。新版本前向验证从2025-10-01开始，7—9月不回填、不改写。

此前未纳入数据库删除范围的10—11月文件型回测记录已补充清理：从`src/logs`删除全部文件名或路径包含2025-10-01至2025-11-30日期的4,875个回测/分析日志文件，并在`D:\research\Workshop\agentquant_oct_nov_logs_before_reset_20260813`保留原样归档；核验剩余匹配文件为0。数据库与文件型记录现在均从2025-10-01重新开始。

（6）[7—9月训练基线核验] 未重跑、未改写7—9月原始推荐、成交、结算和收益事实；确认当前版本可直接读取该段已有`analyst_performance`（76条）、`alpha_setup_sample`（990条）、`alpha_setup_profile`（456条）、`alpha_setup_action_value`（505条）、`adaptive_policy_state`（47条）及`learning_event_log`（2344条）作为10月前向学习输入。旧版记录继续作为训练/基准，不被宣称为新版策略结果；历史重建入口因当前运行时异常退出未执行，避免在未验证时改写学习状态。

（5）[验证] 目标回归、全量单元测试（1116项）、`compileall`、pre-backtest acceptance及system invariant audit全部通过；未改变现有数据库结构和零启动入口。
