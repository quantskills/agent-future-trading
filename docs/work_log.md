# AgentQuant 工作日志

本日志自 2026 年 07 月 27 日起重新记录。

只记录已经完成的 `.py`、`.yaml`、`.yml` 行为修改或运行配置修改。相关修改完成后再追加记录；纯讨论、方案、排查结论、仅运行测试、纯文档修改、数据或缓存处理、文件改名或删除均不记录。

日志按日期正序分组。每项只简要说明：

- 修改了什么。
- 为什么修改。

==========2026年07月27日==========

（1）[PM换约持仓血缘重放] `portfolio_manager.py`不再跳过换约成交，按日内换约平仓腿优先顺序重放策略持仓血缘；同一换约推荐的平旧开新继续继承原开仓FAC，新增同向手数沿用该FAC，换约只平旧则清空旧血缘，下一次策略开仓建立新FAC周期；血缘无法闭合时明确终止。`test_phase_flow_regression.py`覆盖同时间戳换约腿、同向增仓、只平旧及次日新开仓。原因：修复换约只平旧后PM仍把后续策略开仓接到旧FAC，导致持仓失效条件和持有天数错误的问题。

（2）[交易学习setup身份单源] `research_review_helpers.py`删除按分析师顺序、方向、周期及信号组合重建setup的旧函数，正式学习路径统一读取原始FAC的canonical `setup_type`，缺失及通配身份拒绝进入正式学习；`research_memory_writers.py`、`research_learning.py`、`research_snapshot_reports.py`同步覆盖模板绩效、持仓反馈、亏损政策、fast-loss、完整episode、alpha profile、规则校准及研究报告，`portfolio_manager.py`的当日政策检索改用已验证SCC中的canonical setup，`alpha_setup.py`删除失去生产用途的setup推断函数。`test_reviewer_learning.py`覆盖分析师标签与FAC不一致时fast-loss仍使用FAC身份。原因：修复亏损保护及同类学习写入旧复合setup后无法被PM精确检索的问题，并阻止字段统一后再次产生第二套setup键。

==========2026年07月28日==========

（1）[setup身份单源三处遗漏收口] `research_learning.py`把`alpha_setup_profile`的canonical `setup_type`原值写入`adaptive_policy_state`，身份缺失及通配时仅跳过该政策；日常hold/reduce/exit学习按成交血缘重放原开仓FAC，同向换约续仓继续继承原setup，当日FAC只保留在证据中；execution样本取消`execution_*_setup`第二套身份，改用对应FAC setup并直接继承Trader的`execution_retrieval_key`区分执行方式。`research_memory_writers.py`删除alpha政策落库时写死的通配setup，`portfolio_manager.py`从Trader执行检索键读取execution profile。回归覆盖政策值传递与跨setup隔离、换约后持仓setup继承、execution FAC setup及精确执行键。原因：完成既有setup单源修改遗漏的三条生产链，不改变换约决策、资金、rank、止损、fallback及升层阈值。

（2）[自适应政策来源交易日落库] `research_learning.py`在alpha setup政策写入时传递本次Phase4刷新交易日，`research_memory_writers.py`让全部`adaptive_policy_state`生产路径在同一学习事件落库后写入该事件的真实`trading_date`，并在alpha政策UPSERT中原子更新该字段；回归核对政策来源日等于关联学习事件日期。原因：补齐政策状态表已登记的标准日期字段，使每次Phase4自动刷新自身可追溯，不改变PM的T+1读取、政策有效期及刷新机制。

（3）[日频止盈与新仓快速止损] `portfolio_manager.py`按原开仓FAC追溯开仓日至T-1已完成结算价，盈利达到1个原始ATR后启用相距1个原始ATR且只能收紧的移动保护，并让新仓前两个交易日亏损达到0.5%且当日同向证据再验证失败时减仓50%、亏损达到2%时退出；`trader_execution_exit_policy.py`统一初始ATR止损与移动保护的确定性计算；`test_phase_flow_regression.py`与`test_pre_backtest_pm_workflow_contracts.py`覆盖多空移动保护、激活边界、T-1结算价、真实PM持仓链及新仓减仓/退出。原因：减少已形成浮盈的回吐，并让原FAC尚未失效但快速亏损且当日证据失效的新仓及时降险。

==========2026年07月29日==========

（1）[持仓学习身份与Profile精确作用域] `portfolio_manager.py`让持仓正式学习检索及hold/reduce/exit FAC继承原开仓FAC的setup、horizon、expected days和regime，同时保留当天SCC的行情、确认及退出证据；`sqlite_helper.py`、`pm_decision_memory_retrieval.py`与`pm_signal_fusion.py`让PM Profile按正式setup精确查询并在Rank和仓位端复核。原因：修复持仓周期身份漂移及跨setup Profile进入Rank、仓位和放大的问题，不改变分析师宽范围探索读取、换约和资金参数。

（2）[完整episode身份、普通亏损复评与收益率Rank] `research_memory_writers.py`让完整episode的四项身份直接继承原开仓FAC，身份缺失时不写正式episode；`portfolio_manager.py`让原FAC未失效的普通持仓在浮亏2%且证据失败时减仓50%、浮亏4%时退出；`alpha_setup.py`与`pm_signal_fusion.py`把完整episode的平均及最差名义收益率写入action-value并用于Rank，人民币奖励保留审计。`test_phase_flow_regression.py`与`test_reviewer_learning.py`覆盖开仓身份、跨setup隔离、2%/4%复评、相同收益率同分及完整episode收益率聚合。原因：恢复持仓学习周期的一致身份、激活既有普通亏损保护，并消除不同合约和手数按人民币盈亏比较的不公平。

（3）[决策工具旧测试契约同步] `test_decision_workflow_tools.py`把PM学习身份的源码字符串断言改为新机会读取当天SCC、已持仓读取原开仓FAC的行为测试，并为排名正负学习与权重测试补齐完整周期平均及最差名义收益率，人民币盈亏仅保留为审计样本。原因：同步本日持仓身份和收益率Rank生产契约，防止旧测试反向要求已删除实现或缺失正式排名输入。

==========2026年07月31日==========

（1）[正式学习FAC身份单源收口] `learning_identity.py`新增完整FAC学习身份与市场状态规范化公共入口；`research_review_helpers.py`、`research_learning.py`、`research_memory_writers.py`、`research_snapshot_reports.py`和`alpha_setup.py`让成交型日常持仓、execution、完整episode及其Profile、action-value、持仓反馈、setup绩效和亏损政策统一继承原开仓FAC四项身份，未交易学习继承对应当日FAC，身份不完整时不写正式学习；execution继续使用独立`execution_retrieval_key`区分执行方式。原因：完成既有FAC身份单源修改遗漏路径，禁止同一学习周期因平仓日信号或分析师重新推导而形成第二套身份。

（2）[PM政策生命周期路由与市场状态规范化] `portfolio_manager.py`把新机会政策与原持仓政策分开检索和消费：空仓按当天SCC/FAC，持仓、减仓和退出按原开仓FAC，反向日先按旧周期管理；`pm_contract_builder.py`、`pm_decision_memory_retrieval.py`、`sqlite_helper.py`和`learning_contract.py`统一正式市场状态的小写下划线格式。原因：修复当天目标方向覆盖原持仓政策及同义市场状态格式不同导致精确检索降级的问题，不改变当天行情判断、排名、资金或交易规则。

（3）[分析师学习实际采用摘要] `analyst_learning_context.py`只记录真正进入提示词预算的学习记录编号，`analyst_learning_calibration.py`把实际参与确定性证据校准的记录编号及技术参数政策编号和参数前后值写入现有`learning_impact_summary`，`technical.py`传递已执行的技术参数校准结果；现有AEC与signal artifact继续保存该摘要。原因：使回测后能够证明分析师实际采用了哪些学习及参数校准，不新增表、不保存完整提示词、不改变分析策略和交易权限。

（4）[四项修改永久行为回归] `test_decision_workflow_tools.py`、`test_reviewer_learning.py`、`test_researcher_lifecycle_contract.py`和`test_phase_flow_regression.py`增加新机会/持仓政策路由、完整FAC身份继承、execution身份、跨setup隔离、市场状态规范化及分析师学习摘要落入AEC的行为测试。原因：用现有测试套件封闭本轮生产、落库和消费路径，防止旧夹具缺字段或同类遗漏再次通过。

（5）[回测前全路径测试门] `pg_pre_backtest_acceptance.py`把决策检索、Researcher学习和Phase全链路三组行为回归纳入`pre_backtest`的`supported_business_paths`检查，`test_pre_backtest_acceptance.py`固定校验测试组完整性及任一失败令回测前报告失败。原因：使学习生产、落库、消费、排名、资金、止损和换约在用户下令回测前检测时统一验收，不接入`backtest.py`，不改变每日七项审计。

==========2026年08月01日==========

（1）[排名尾部学习信号独立生效] `pm_signal_fusion.py`让完整周期的`worst_return_on_notional`为负时独立形成尾部风险信号，不再要求该周期的平均收益同时为负；`test_phase_flow_regression.py`覆盖平均收益为正、最差收益为负时同时保留正向学习并产生尾部扣分。原因：修复盈利周期中的真实最差结果未进入排名尾部学习分的问题。

（2）[未交易fast-candidate政策单源] `research_learning.py`停止由candidate/watchlist Profile生成`fast_candidate_alpha`，`research_memory_writers.py`删除未交易反事实按多周期最佳结果生成成熟`alpha_promotion`的旁路，并让固定5日未交易路径只按自身`missed_alpha_accountability`事件精确停用政策；`adaptive_policy_safety.py`拒绝错误来源fast-candidate及历史未交易反事实成熟alpha，配置删除失去生产用途的阈值；回归覆盖真实Alpha政策保持、Profile不生成fast-candidate、固定5日政策生成与来源隔离、停用归属和消费拦截。原因：消除两条政策生产路径共用同名政策、跨来源停用及未交易影子结果越权晋升成熟Alpha的问题，不改变真实成交Alpha、其他政策、排名、资金与试仓参数。

==========2026年08月02日==========

（1）[学习时效、收益率同口径与策略修复闭环] `alpha_setup.py`为同完整作用域action-value写入最新完整周期手续费后名义收益率，并以最近最多5个完整周期平均收益复用既有`capped`及恢复门槛；`pm_signal_fusion.py`让最新亏损优先清零旧正向学习和Profile加分，Rank与`candidate_quality`共享同一时效学习结果；`portfolio_manager.py`让positive open seed、probe/real/scale仓位学习统一优先读取`return_on_notional`，保留0.8%～1.5%差异化试探和强当日证据试探资格。原因：阻止旧盈利记忆在最新同类亏损后继续抬高排名和仓位，同时不压死重新验证与恢复放大路径。

（2）[弱机会价格延续与量能确认] `trader_intraday_execution.py`只对既有`stronger/strict`触发确认追加下一根完整15分钟价格延续和相对量能校验，`standard`保持原触发；`dev.yaml`登记4根量能参考窗口和1.0相对量能阈值，回归覆盖弱量阻断、标准触发不受影响及原 stronger/strict 路径。原因：减少依赖历史亏损校准或弱冲突候选被短暂价格突破和低参与量错误成交，不新增Trader方向、手数、退出或研究消费权限。

（3）[分析师与触发确认学习同口径] `alpha_setup.py`把平均及最新完整周期手续费后名义收益率写入既有分析师安全投影，开仓`entry_quality_outcome`的正负、权重和确认等级改由`return_on_notional`生成，并以既有2%名义收益率满权重边界区分stronger与strict；`analyst_learning_calibration.py`删除人民币reward/net_pnl对开仓证据强度的影响，并让最新精确作用域亏损同步撤销旧正向Profile校准。回归覆盖安全投影、最新亏损优先、分析师学习强度、触发确认的人民币规模不变性及收益率严重程度。原因：关闭旧正向学习经分析师校准间接抬高当日证据的残余路径，并统一不同品种、乘数和手数的学习经济口径，不新增Rank、交易、风控或退出路径。

（4）[正式开仓收益率缺失时拒绝分析师采用] `alpha_setup.py`让正式开仓episode缺少`return_on_notional`时固定生成neutral且分析师不可用的校准契约，禁止按人民币reward或action preference回退为正负校准；`test_phase_flow_regression.py`覆盖正负人民币盈亏下的分析师安全投影拒绝，以及触发确认保持neutral。原因：关闭异常或遗留不完整episode绕过手续费后名义收益率口径进入分析师学习的防御缺口，不改变完整episode、PM Rank、资金、Trader或退出路径。
