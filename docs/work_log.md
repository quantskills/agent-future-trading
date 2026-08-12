# AgentQuant 工作日志

本日志自 2026 年 08 月 08 日起重新记录。

只记录已经完成的 `.py`、`.yaml`、`.yml` 行为修改或运行配置修改。相关修改完成后再追加记录；纯讨论、方案、排查结论、仅运行测试、纯文档修改、数据或缓存处理、文件改名或删除均不记录。

日志按日期正序分组。每项只简要说明：

- 修改了什么。
- 为什么修改。

==========2026年08月08日==========

（1）[完整撤销探索仓完整周期盈利回吐直接退出] 撤销8月7日第一项：`portfolio_manager.py`删除探索仓首次`profit_giveback_revalidation_failed`时直接全部退出的分支，并删除该分支新增的`opening_authority_type`读取及诊断记录，使持仓生命周期代码恢复至七八月复测版本；对应回归测试、机制契约、检查表和项目规则同步恢复减仓口径。原因：七八月复测中的SR样本在首次触发后的分批减仓比同价全部退出少亏约650元，该样本不支持将直接退出固化为全局收益优化规则。

（2）[主LLM切回GPT-5.6 Sol中等推理] `dev.yaml`将唯一启用的主`llm`配置切回`CodexOpenAI / gpt-5.6-sol`并明确使用`reasoning_effort=medium`，完整保留停用的`DeepSeek / deepseek-v4-pro`思考模式配置；协议预检测试、README和智能体内部机制文档同步更新。原因：三类分析师和Researcher必须继续通过统一主配置共同切换模型，不改变其他智能体权限、AEC→SCC→PM FAC交易链或失败即抛错边界。

（3）[多期限可检验预测契约] `schema.py`、`analyst_structured_output.py`、`analyst_quality.py`、`signal_evidence_collection.py`与`prompt.py`让三类分析师固定生成并校验1、3、5、10日方向概率、预期收益区间、驱动和预测失效条件，确定性数据缺失路径写入中性预测网格；预测字段沿AEC与SCC落地且不含手数、Rank和交易权限。原因：把未校准的单一置信度改成能被未来真实价格逐期限检验的预测事实。

（4）[预测到期评价与分层研究闭环] `sqlite_setup.py`新增`analyst_forecast_evaluation`，`research_memory_writers.py`以AEC逻辑交易日为预测起点，仅在期限到达后按执行手续费事实表计算方向命中、Brier、标的收益和预测方向手续费后收益，并按品种、板块、市场状态和全局层级写入分析师绩效；完整episode绩效同步写入精确、去状态、跨品种和全局setup层级。原因：让真实成交与全市场预测结果形成足够密度的未来校准数据，解决细粒度分组令正式学习表长期为空的问题。

（5）[手续费后预测校准进入唯一Rank] `sqlite_helper.py`新增到期预测校准检索，`pm_signal_fusion.py`按品种、板块、全局依次回退，并确定性融合匹配市场状态下的方向准确率、Brier和预测方向手续费后收益；`pm_full_market_capital_deployment.py`与`rank_score_policy.yaml`新增唯一`calibrated_forecast_value`分项，冷启动为0，负值只降低相对顺序。原因：阻止原始LLM置信度继续被解释为盈利概率，同时保留探索仓、负Rank候选和既有交易机会链。

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
