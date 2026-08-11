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
