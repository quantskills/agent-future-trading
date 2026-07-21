# AgentQuant 工作日志

本日志自 2026 年 07 月 19 日起重新记录。

只记录已经完成的 `.py`、`.yaml`、`.yml` 行为修改或运行配置修改。相关修改完成后再追加记录；纯讨论、方案、排查结论、仅运行测试、纯文档修改、数据或缓存处理、文件改名或删除均不记录。

日志按日期正序分组。每项只简要说明：

- 修改了什么。
- 为什么修改。

==========2026年07月19日==========

（1）[PandaAI与技术指标语义] `analyst_market_confirmation.py` 按官方单位分别解释基差率、多空比和合约日指标；`technical.py`、`prompt.py` 将波动率、成交强度和价格位置真实传入分析师，并让布林带使用学习校准后的标准差参数。原因：消除比例误读、指标声明与实际消费不一致及学习参数不生效。

（2）[Finoview可见性与基差] `analyst_finoview_factors.py`、`finoview_factor_catalog.yaml`、`router.py` 统一使用单一catalog生成频率、freshness、正式交易日发布滞后和可见行，删除Router第二套频率判断及J重复因子；PandaAI历史日线增加显式结束日包含语义，本地基差改为同日现货期货匹配和统一现货分母。原因：防止周频/月频误判、前视、日期错位和同一基差两套解释。

（3）[基本面与新闻真实消费] fundamental提示上下文只登记并展示实际可用于方向的Finoview因子值；本地新闻在截取最新记录前按15个产品产业链过滤，并由真实匹配计算相关度。原因：防止未传递因子被登记为已使用，以及非空但无关新闻污染品种证据。

（4）[分钟行情错误边界] `trader_intraday_execution.py` 将分钟接口异常改为稳定数据故障，真实无异常空响应继续保留为`intraday_no_valid_bar`；相关确定性测试同步覆盖九项数据链语义。原因：接口故障不能伪装成合法未触发或无行情。

（5）[technical canonical触发顺序] `analyst_quality.py` 在setup完整性判断前，先按合法`entry_timing_signal + side`生成唯一canonical `entry_trigger`；相关测试覆盖三种technical profile的多空路径、缺失效边界、自由文字不创建profile及条件FAC保护。原因：防止合法technical watch仅因LLM执行文字为空就在进入SCC和PM排名前被错误清成`no_opportunity`。

（6）[PandaAI合约代码生产] `api.py` 在对外业务出口统一生产大写品种代码加四位年月的具体合约，主连读取`dominant_id`，郑商所三位年月按记录交易日展开；相关测试覆盖上期所、大商所、郑商所、主连、历史年份和换月比较。原因：消除同一物理合约因大小写、交易所后缀和郑商所年月格式差异触发的虚假换月。

（7）[PG推荐执行日期] `pg_system_invariants.py` 按`effective_trade_date`加载成交日推荐；相关测试覆盖T日生成、`Next(T)`生效成交和真实缺失推荐。原因：防止跨日rollover推荐被单日PG错误报告为`transaction_recommendation_missing`。

（8）[PandaAI郑商所分钟合约代码] `api.py` 在分钟行情返回边界按记录逻辑交易日生产唯一业务合约代码，具体合约严格核对回包月份，主连逐行读取`dominant_id`，只覆盖返回副本的`trading_code`；相关测试覆盖SR、CF、TA、BU、C的15m/1m、历史年份、夜盘、错月和回测前检测。原因：防止郑商所三位分钟代码被过滤成虚假空行情，并让真实合约冲突进入既有数据异常链路。

==========2026年07月20日==========

（1）[Phase2资金顺序与动态保证金硬线] `trader.py` 在策略批次中先稳定处理不增加风险的减仓/退出，再按已审 FAC 的`rank_budget_sequence`调度新增风险，并让汇总服从最终执行事实；`trader_futures_execution.py` 在实际执行价和最终动态保证金率确定后、交易形成前复核账户总保证金硬线，超线使用既有`margin_insufficient`整单不成交；相关测试覆盖wait/hold、open/scale、reduce/exit、条件等待、forced-risk平仓边界、低于/等于/超过硬线、真实保证金释放、靠前阻断后继续和执行汇总。原因：保持PM已签资金优先级，并补齐计划保证金与实际动态保证金偏离后的最终账户硬风控。

（2）[评估持仓血缘与区间边界] `futures_trade_pairs.py` 新增只供评估链使用的策略起源持仓配对，保留 rollover/forced-risk 的真实执行来源并在完整换月后传递原策略开仓血缘；`evaluation.py`、`analyze_strategy_attribution.py` 统一使用该配对口径，并让指定区间先重放截至结束日的成交历史、再按平仓日统计；相关评估测试覆盖运营平仓、完整换月传递、期初继承持仓和区间结束边界。原因：修复策略持仓被运营动作平仓后从胜率、质量指标和策略归因中漏算，以及子窗口无法识别期初持仓的问题。

（3）[PM学习排名资金闭环] `portfolio_manager.py` 在精确检索及弱先验诊断完成后重建同一个Step4机会评分对象并保留Step2方向，显式非canonical及similar/weak prior记录不再进入正式学习；`pm_signal_fusion.py` 只消费完整canonical PM学习并将execution/profile严格隔离在执行画像；`pm_full_market_capital_deployment.py` 将资金效率纳入七项rank分量后统一截断，并按资金层、候选层、rank、当日证据、学习后候选质量、资金效率和ticker顺序消费预算。原因：修复后置精确action-value未进入scorecard、弱先验混入正式score/rank、执行学习借trigger分量污染rank、负原始分被二次加分抬正及同分候选退化为ticker排序的问题。

==========2026年07月21日==========

（1）[PM学习生命周期与有符号rank闭环] `portfolio_manager.py` 在首次正式学习消费前组装并冻结显式`pm_learning`的完整canonical Step4池，隔离similar/weak/incomplete prior且禁止后置追加；`pm_signal_fusion.py` 仅让open/add/scale/increase学习进入新增风险候选质量和rank；`pm_full_market_capital_deployment.py` 将七项分量一次求和并保留有符号rank；`pm_contract_builder.py`、`final_action_semantics.py`、`pm_contract_self_check.py` 按最终手数生命周期先路由完整池，再严格分离正式决策行与execution/profile行。原因：让学习真实影响新增风险投资价值与预算顺序，同时保证条件开仓继续等待盘中触发、非新增风险动作无伪rank，并保持既有资金层、probe预算和20%硬门控不变。

（2）[证据新鲜度到资金落地闭环] `evidence_fusion_semantics.py` 只从正式时效字段和数据源事实解析新鲜度，不再让普通风险标签污染时效；`signal_evidence_collection.py` 由SCC来源AEC的唯一`fusion_evidence`生成并校验跨分析师融合；`pm_risk_gate.py`、`portfolio_manager.py` 的既有新闻规则只消费SCC重建证据中的正式新鲜度和相关性。原因：修复真实AEC时效在SCC中退化为unknown、共识分失真以及PM新闻规则固定读到零的问题，使新鲜度经候选质量、rank和仓位手数真实落地，同时不改变rank公式、参数和交易权限。
