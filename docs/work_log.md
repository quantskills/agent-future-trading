# AgentQuant 工作日志

本日志自 2026 年 08 月 02 日起重新记录。

只记录已经完成的 `.py`、`.yaml`、`.yml` 行为修改或运行配置修改。相关修改完成后再追加记录；纯讨论、方案、排查结论、仅运行测试、纯文档修改、数据或缓存处理、文件改名或删除均不记录。

日志按日期正序分组。每项只简要说明：

- 修改了什么。
- 为什么修改。

==========2026年08月02日==========

（1）[学习时效、收益率同口径与策略修复闭环] `alpha_setup.py`为同完整作用域action-value写入最新完整周期手续费后名义收益率，并以最近最多5个完整周期平均收益复用既有`capped`及恢复门槛；`pm_signal_fusion.py`让最新亏损优先清零旧正向学习和Profile加分，Rank与`candidate_quality`共享同一时效学习结果；`portfolio_manager.py`让positive open seed、probe/real/scale仓位学习统一优先读取`return_on_notional`，保留0.8%～1.5%差异化试探和强当日证据试探资格。原因：阻止旧盈利记忆在最新同类亏损后继续抬高排名和仓位，同时不压死重新验证与恢复放大路径。

（2）[弱机会价格延续与量能确认] `trader_intraday_execution.py`只对既有`stronger/strict`触发确认追加下一根完整15分钟价格延续和相对量能校验，`standard`保持原触发；`dev.yaml`登记4根量能参考窗口和1.0相对量能阈值，回归覆盖弱量阻断、标准触发不受影响及原 stronger/strict 路径。原因：减少依赖历史亏损校准或弱冲突候选被短暂价格突破和低参与量错误成交，不新增Trader方向、手数、退出或研究消费权限。

（3）[分析师与触发确认学习同口径] `alpha_setup.py`把平均及最新完整周期手续费后名义收益率写入既有分析师安全投影，开仓`entry_quality_outcome`的正负、权重和确认等级改由`return_on_notional`生成，并以既有2%名义收益率满权重边界区分stronger与strict；`analyst_learning_calibration.py`删除人民币reward/net_pnl对开仓证据强度的影响，并让最新精确作用域亏损同步撤销旧正向Profile校准。回归覆盖安全投影、最新亏损优先、分析师学习强度、触发确认的人民币规模不变性及收益率严重程度。原因：关闭旧正向学习经分析师校准间接抬高当日证据的残余路径，并统一不同品种、乘数和手数的学习经济口径，不新增Rank、交易、风控或退出路径。

（4）[正式开仓收益率缺失时拒绝分析师采用] `alpha_setup.py`让正式开仓episode缺少`return_on_notional`时固定生成neutral且分析师不可用的校准契约，禁止按人民币reward或action preference回退为正负校准；`test_phase_flow_regression.py`覆盖正负人民币盈亏下的分析师安全投影拒绝，以及触发确认保持neutral。原因：关闭异常或遗留不完整episode绕过手续费后名义收益率口径进入分析师学习的防御缺口，不改变完整episode、PM Rank、资金、Trader或退出路径。

==========2026年08月04日==========

（1）[当前证据先行与学习后置] `pm_signal_fusion.py`拆分`current_evidence_quality`和`validated_learning_delta`，要求当日方向、可交易状态、setup、失效边界、数据质量和冲突处理先通过现有入场前提，正式学习才可参与综合分、唯一Rank及既有0.8%～1.5%差异化probe；未解决主导反对证据固定阻断开仓FAC。原因：防止历史正向记忆把当日证据不成立的机会抬成可交易候选，同时保留成熟学习在实时证据成立后的real/scale放大能力。

（2）[正向Profile作用域收窄与既有负向控制保留] `pm_signal_fusion.py`取消`candidate/watchlist` Profile正加分，并要求`protected/deployable` Profile同时存在未被最新亏损撤销的正式正向action-value才可加分；既有精确完整作用域`capped`、latest-loss和fast-loss sentinel保持原路径。原因：停止宽正向记忆持续抬分，不扩展负向作用域、不建立品种黑名单或第二个策略失效状态机。

（3）[弱机会分级成交确认] `portfolio_manager.py`把弱setup、迟入场和结构化弱冲突写入既有`trigger_confirmation_adjustment`，未解决主导反对证据不授予开仓权限；`trader_intraday_execution.py`让stronger验证1根完整15分钟延续线、strict验证连续2根，并对完整确认序列执行价格和相对量能校验。原因：堵住弱价格触发成交泄漏，使stronger与strict具有真实不同的执行强度，同时不新增Trader方向、手数或盘中退出路径。

（4）[完整开仓FAC周期收益与probe身份] `portfolio_manager.py`从`ticker_daily_pnl`累计原开仓日至决策时点的手续费后周期净收益、峰值和回吐，并与单日收益率分开供生命周期和硬风险消费；`pm_contract_builder.py`、`contracts.py`、`futures_audit.py`和`trader_execution_exit_policy.py`沿后续FAC传递原开仓`opening_authority_type`。原因：避免用单日波动冒充完整持仓周期盈亏，并让既有probe期限规则只作用于真实probe，不误伤real/scale或新建退出路径。

（5）[探索研究未来验证闭环与归因] `research_learning.py`把完整episode逐日轨迹及明确`support_episode_ids`交给Researcher LLM，新假设先做shadow-only并仅按生成日之后同完整作用域真实episode的手续费后`return_on_notional`迁移`candidate/monitoring/validated/rejected`；`sqlite_helper.py`与`analyst_learning_context.py`只向分析师提供validated假设，`learning_attribution.py`及评估工具补记正式学习对Rank和仓位的实际分量。原因：让LLM抽象经验具备可证伪的未来验证和稳定归因，同时只经原分析师AEC→SCC→PM FAC链生效，不形成第二条研究或交易路径。

（6）[RAG记忆刷新、去重与收益率口径] `research_memory_writers.py`让分析师绩效与摘要只在出现新完整真实episode时按真实结束日刷新和续期，并按完整作用域及摘要内容复用单行；`sqlite_helper.py`与`analyst_learning_context.py`在数据库检索和提示词装配两层按内容去重；`research_learning.py`和分析师episode检索改按手续费后`return_on_notional`选择、排序和表达完整周期，人民币盈亏只留审计。回归覆盖无新周期不续期、不同ID同内容只占一个提示词位置及跨人民币规模按收益率排序。原因：阻止旧摘要每日复制续期和大额人民币盈亏挤占RAG上下文，不改变PM、Rank、FAC、仓位、风控或Trader权限。

==========2026年08月05日==========

（1）[研究假设亏损降级与可恢复验证] `research_learning.py`让已验证假设遇到最新完整亏损但未来样本平均收益仍为正时先降为`monitoring`并立即退出分析师RAG；样本达到门槛且平均`return_on_notional`不为正时才`rejected`，后续未来样本恢复正期望且最新非负时可恢复`validated`。原因：防止一次小亏永久淘汰总体仍有正期望的研究经验，同时保证最新亏损不会继续影响当日分析。

（2）[研究验证作用域收敛] `research_learning.py`将品种级假设的硬验证身份收敛为“品种＋方向＋setup＋标准化市场状态”，板块级假设改为“板块＋方向＋setup＋标准化市场状态”，周期只保留为适用性和检索字段。原因：保留方向与策略隔离、防止跨策略污染，同时避免周期细分令研究假设长期无法达到未来验证样本门槛。

（3）[完整周期利润回吐复核闭环] `portfolio_manager.py`使用原开仓FAC周期名义金额计算手续费后累计`return_on_notional`、峰值与回吐，不再让会随减仓变化的当前保证金决定生命周期收益；正收益已全部回吐至非正且当日证据不足时，通过既有PM生命周期路径减仓复核。原因：直接处理持仓曾盈利后转亏而PM仍按错误周期口径继续持有的问题，不增加Trader盘中退出权或第二条退出路径。

（4）[负期望策略失效聚合] `alpha_setup.py`保留正向Profile、Rank和action-value的完整精确作用域，仅将最近完整周期负期望判定按“品种＋方向＋setup＋标准化市场状态”聚合，并复用既有`capped`及恢复路径。原因：使被周期或data combo拆散的重复失效策略能够撤销历史放大并重新probe验证，不建立品种黑名单，也不阻断强当日证据。

（5）[Research/RAG上下文质量补齐] `research_learning.py`只在出现新完整episode ID时生成新研究、同步假设验证样本数并允许被否定假设由未来样本恢复；`analyst_learning_context.py`按方向检索并明确展示方向、setup、周期和市场状态；`research_memory_writers.py`让同一研究命题的新摘要版本停用旧版本，同时保留其他方向或命题。原因：避免相同历史反复生成、样本数失真及跨方向先验混入提示词，同时继续保持Researcher只提供可反驳分析先验。

（7）[七八月独立复测实验] 七八月复测运行使用独立`exp_name=agentquant-futures-trading-2025-rag-retest-202507-08`，其余运行、策略、账户、品种和学习配置保持不变；复测完成后当前`dev.yaml`已恢复默认`exp_name=agentquant-futures-trading`，复测事实继续按独立`config_id`保留。原因：隔离RAG修复后2025年7—8月复测，完整保留旧实验结果用于同窗口对比，同时避免临时实验名长期成为默认运行口径。

（8）[主导反对证据条件权限一致性修复] `portfolio_manager.py`让未解决主导反对证据同时阻断real、probe与`conditional_trigger_authority`，`test_phase_flow_regression.py`覆盖冲突候选不得保留条件开仓权且正常`watch_for_trigger`条件试探不受影响。原因：修复最终目标手数恢复为当前持仓后仍残留条件触发权限并导致PM Step6自检失败的问题，不增加兜底、自检修复或第二条交易路径。

==========2026年08月07日==========

（1）[探索仓完整周期盈利回吐退出] `portfolio_manager.py`保留既有“原开仓FAC完整周期收益峰值为正、当前手续费后`cycle_return_on_notional<=0`且当日同向证据再验证失败”的触发条件，仅按已沿生命周期传递的`opening_authority_type`区分处理：`exploration_probe`首次触发即由PM唯一FAC全部退出，real/scale继续走既有减仓复核路径；`test_phase_flow_regression.py`覆盖探索仓退出、real仓保持减仓及当日证据复核通过不退出。原因：七八月独立复测中SR探索空单连续三次触发并由11手逐级减至5手、2手、0手，证明探索仓减半路径延长了已失效Alpha的亏损暴露；本次不改变开仓、Rank、正向学习作用域、成熟Alpha放大、Trader权限或新仓亏损复核路径。

（2）[主LLM切换至DeepSeek V4 Pro思考模式] `dev.yaml`将唯一启用的主`llm`配置切换为`DeepSeek / deepseek-v4-pro`，开启thinking并请求`reasoning_effort=medium`，完整保留停用的`CodexOpenAI / gpt-5.6-sol`配置；`test_protocol_preflight_cli.py`同步验证DeepSeek provider kwargs与非敏感路由元数据。原因：让技术面、基本面、期货新闻面分析师和Researcher通过现有统一LLM入口共同切换模型，不改变其他智能体权限、AEC→SCC→PM FAC交易链或失败即抛错边界。

==========2026年08月08日==========

（1）[完整撤销探索仓完整周期盈利回吐直接退出] 撤销8月7日第一项：`portfolio_manager.py`删除探索仓首次`profit_giveback_revalidation_failed`时直接全部退出的分支，并删除该分支新增的`opening_authority_type`读取及诊断记录，使持仓生命周期代码恢复至七八月复测版本；对应回归测试、机制契约、检查表和项目规则同步恢复减仓口径。原因：七八月复测中的SR样本在首次触发后的分批减仓比同价全部退出少亏约650元，该样本不支持将直接退出固化为全局收益优化规则。

（2）[主LLM切回GPT-5.6 Sol中等推理] `dev.yaml`将唯一启用的主`llm`配置切回`CodexOpenAI / gpt-5.6-sol`并明确使用`reasoning_effort=medium`，完整保留停用的`DeepSeek / deepseek-v4-pro`思考模式配置；协议预检测试、README和智能体内部机制文档同步更新。原因：三类分析师和Researcher必须继续通过统一主配置共同切换模型，不改变其他智能体权限、AEC→SCC→PM FAC交易链或失败即抛错边界。
