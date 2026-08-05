# Matrix Chain Contract

更新时间：2026-07-18

本文是 AgentQuant 全链路契约矩阵。它只回答一件事：每个关键系统事实由谁生产、落在哪里、谁消费、谁审计、什么条件必须 hard fail、什么条件只进入 diagnostics。

本文不替代：

- `docs/mechanism_multiagents.md`：智能体角色、阶段、权限边界。
- `docs/matrix_field_semantics.md`：字段名、字段含义、字段权限。
- `docs/matrix_action_canonical.md`：action-value 动作 canonical 矩阵。
- `docs/agent_pm.md`：PM 六步、最终合约、自检细节。
- `docs/workflow.md`：workflow 编排、传递、保存、阻断。
- `docs/mechanism_research.md`：复盘、研究、记忆、学习边界。

本文已经接入可执行闸门：
- `src/tools/agent_tools/control/pg_contract_coverage_audit.py` 按本文关键契约行执行六维 coverage：`producer`、`physical_landing`、`consumer`、`role_check`、`real_path_test`、`mechanism_doc`。pre-backtest readiness 与 daily PG 物理事实审计由各自正式入口独立执行，不是 coverage 的附加维度。
- 回测前检测通过 `src/tests/test_*.py` 的通用不变量和真实路径测试证明系统就绪；历史问题只能作为测试样本来源，不能成为回测前检测的设计中心。
- `src/tools/agent_tools/control/pg_system_invariants.py` 按本文第 5 节输出 daily PG hard fail 边界和 diagnostics 边界。
- `src/run/control/pre_backtest_acceptance.py` 与 `src/run/control/system_invariant_audit.py` 是本文对应的只读控制入口。

## 1. 使用规则

修改生产端、自检、回测前验收、日终 PG 审计、Research 写入、PM artifact、Trader/Reviewer/Researcher artifact 时，必须先定位本文对应行，再按以下八项开发同步清单处理。该清单是完整开发顺序，不等同于 `pg_contract_coverage_audit.py` 的六个可执行 coverage 维度：

1. 生产端。
2. artifact / DB 落点。
3. 消费端。
4. PM self-check / 角色内部校验；由角色自身和回测前测试负责，不交给 daily PG 复查。
5. pre-backtest fixture gate。
6. daily PG audit。
7. 真实路径测试。
8. 机制文档。

六维 coverage 与上述清单的关系固定为：生产端对应 `producer`，物理落点对应 `physical_landing`，消费端对应 `consumer`，角色自身校验对应 `role_check`，真实生产链行为测试对应 `real_path_test`，正式机制文档对应 `mechanism_doc`。pre-backtest fixture/readiness 和 daily PG 分别由回测前十项与每日七项门禁执行，不能用静态 coverage 字符串替代，也不能让 daily PG 复查智能体内部机制。

缺本文矩阵行的系统事实不得进入代码、artifact、DB、prompt 和审计。PG 的输入、判定和输出也不得例外；任何未在 `matrix_field_semantics.md` 登记的字段都不能通过 `metadata`、`payload`、JSON 容器或临时字典键进入旁路报告。

## 2. 判定边界

| 类型 | 定义 | 处理 |
|---|---|---|
| hard fail | 系统契约断裂、字段语义漂移、artifact 污染、越权、前视、阶段断链、交易不来自唯一合约 | 停止回测，先修系统 |
| diagnostics | 物理链路完整但策略效果弱、信号弱、学习为空、rank 低、合法 observe 无交易偏向、当天亏损 | 不停止回测，进入策略分析 |
| 禁止项 | 控制组生成交易动作、下游改 PM 合约、Research 改当天事实、Trader 读研究库下单 | 直接 hard fail |

## 3. 全链路契约矩阵

日期链固定为：`reference_portfolio.trading_date=Prev(T)` 只表示 Proposal 使用的最近已结算账户/持仓；三份持久化 AEC 的 `data_usage_summary.trading_date`、SCC、recommendation/effective date、execution、transaction、settlement、Phase4 和 Researcher 来源均为逻辑 `T`。Researcher 必须从 signal artifact 校验真实 AEC 日期，不能把 `signal.portfolio_id` 关联日期解释为信号日期；逻辑 `T` 学习只能由目标 `Next(T)` 及以后读取。

PM 持仓生命周期校准的唯一 Researcher 输入是 `final_action_contract.learning_used.pm_lifecycle_learning_impact_delta`。`hold/reduce/exit`、期限不匹配和亏损再验证结果都从该对象读取；该对象可记录当日独立规则的真实生命周期结果，但只有同一 FAC 最终 `decision_learning_rows` 与 `alpha_setup_action_values` 的精确 ID 交集才能证明历史学习实际参与。已退出的 `final_action_contract.action_candidates`、旧 `holding_rebalance_control` 对象及其他内部诊断不得恢复为兼容来源。

Researcher 单次运行中的学习 SQL、`researcher_learning_completed`、外置 payload artifact、template prior 和历史学习快照必须共同成功或共同回滚。失败不得留下完成事件或无数据库引用的新 artifact，也不得删除或覆盖运行前已经存在的合法 artifact。

机会状态链只使用共享校验语义：`no_opportunity` 可以保留方向和研究证据，但不构成新增风险支持；`watch_for_trigger` 必须有 Trader 可用逻辑 T 日15分钟行情观察的具体条件，以及由同一 technical/event AEC 的 profile、side、canonical `invalidation_condition` 和正数有限 `invalidation_level` 共同证明的首次成交前作废边界，并且 `trigger_valid/current_trigger_confirmed=false`；`probe_candidate/tradeable_candidate` 必须同时满足两项当前触发布尔值为 true。`position_invalidation_level/exit_hint/atr_stop_distance/expected_horizon_days` 只服务成交后持仓生命周期，不能证明入场作废。分析师 finalization 必须先完成全部质量降级，再原子写入最终状态；SCC 只汇总最终主方向合法状态，反方向 watch 不得借给主方向。

`missing_evidence` 与 `confirmation_requirements` 是证据强度和待确认诊断，不进入 `data_missing`，也不按数量形成 `critical_data_gap`。候选硬数据阻断只来自共享 `build_scc_data_quality_summary.status=hard_fail`。单来源完整已触发候选保留真实低共识分进入 Step5 队列，不补分、不提高一致性，也不自动获得预算、手数或交易权限。

Phase1 数据解释固定为一套事实：PandaAI `basis_ratio` 按百分数、`ls_ratio` 按 50 中性、合约日指标 `ratio` 按 0 中性归一化；技术分析师只登记并接收实际进入提示词的指标，布林带使用当次已校准 `bollinger_std`；Finoview 的频率、freshness、正式交易日发布滞后和可见行只来自同一 factor catalog，`tradeDate` 不是发布时间；本地基差只在同一事实日匹配现货与期货并按 `(spot-futures)/spot` 计算；新闻必须先通过品种产业链相关性筛选。Phase2 分钟接口异常是数据链 hard fail，不能写成合法 `intraday_no_valid_bar`；只有真实非异常空结果才允许形成该未成交事实。

新增风险的顺序固定为：当日 SCC/AEC 当前证据先独立通过既有入场前提，正式学习才可继续影响唯一 Rank 和既有 0.8%～1.5% 差异化 probe；未解决的主导反对证据不得签开仓 FAC。Trader 仍只执行该 FAC：`stronger` 追加一根完整15分钟确认，`strict` 追加连续两根完整15分钟确认。持仓端必须同时保留单日硬风险收益和原开仓 FAC 完整周期手续费后累计收益，原开仓 `opening_authority_type` 沿 hold/reduce/exit 生命周期传递。Researcher 的 candidate/monitoring 假设只做未来同作用域影子验证，只有 validated 才可作为下一交易日分析师先验，不得形成第二条 PM、Trader 或退出路径。

| 契约 / 字段群 | 生产者 / 阶段 | 输入 | 输出 | artifact / DB 落点 | 下游消费者 | 审计点 | hard fail 条件 | diagnostics 条件 |
|---|---|---|---|---|---|---|---|---|
| `action_evidence_contract` | `technical` / `fundamental` / `commodity_news`，Phase1 | 截止点内行情、基本面、新闻、商品 profile、分析师校准摘要 | 三份经同一共享校验的结构化预测证据。technical 固定 `entry_timing`，可执行 profile 只允许 `breakout/pullback/vwap_confirmed`；commodity_news 固定 `event_catalyst`，即时事件只允许 `event_immediate`；fundamental 固定 `direction_context` 且不得生产 Trader profile、entry invalidation。可执行 `entry_trigger` 与 pre-fill `invalidation_condition` 均由共享 canonical 定义确定性形成；`invalidation_level` 只服务首次成交前作废。technical 使用开仓前已完成OHLC确定性生成原始ATR14并由finalization写入`atr_stop_distance`，LLM不得生产或改写ATR；成交后结构位只允许同方向technical或当前已确认的同源`event_immediate`提出并按当日参考价校验，fundamental的`position_invalidation_level`固定为null，只提供中期方向、期限和退出解释。现有 `learning_impact_summary` 同时保存实际进入提示词的学习记录编号、实际参与确定性证据校准的记录编号，以及技术参数校准的政策编号和参数前后值；不保存完整提示词 | Workflow 在一个 Phase1 写事务中保存三份 signal 及全部 recommendation；内存metadata只保留同一AEC与真实ID | `signal_collector`、Reviewer、Researcher | 角色结构校验；shared finalization；共享 AEC 校验；pre-backtest structured IO | 含手数、rank或最终动作；entry trigger/profile/side/condition/level不一致；`exit_hint`或ATR冒充entry invalidation；LLM生产ATR；fundamental声明执行机会或数值结构位；自由执行文字进入正式触发/失效；部分落地 | 无方向、缺具体触发或入场作废边界、仅有研究价值、证据弱/冲突、合法普通中性或数据不可用状态 |
| `product_profile_evidence` | 三类分析师，Phase1 | `product_price_behavior_profiles.yaml`、行情与品种上下文 | profile 使用痕迹、支持证据、冲突证据、缺失确认项 | 仅 `action_evidence_contract.product_profile_evidence`；SCC source/evidence items只做已登记索引 | `signal_collector`、PM 证据上下文、Reviewer、Researcher | contract coverage；pre-backtest artifact boundary | profile 字段含交易授权、手数、rank、PM reason code、最终动作，或在AEC外复制第二份 | profile 不相关、profile 证据不足 |
| `fusion_evidence` | 分析师质量工具，Phase1 | 分析师结构化证据、profile、数据质量 | 证据强弱、时效、一致性、冲突、确认需求 | 分析师 `metadata.action_evidence_contract.fusion_evidence`；SCC 只在 `source_contracts[].action_evidence_contract.fusion_evidence` 保留同一份，并另行形成顶层跨分析师 `evidence_fusion` 汇总 | `signal_collector`、PM scorecard、Reviewer、Researcher | contract coverage；Reviewer/Researcher 事实归因 | 在 SCC source 同级或 evidence item 复制第二份 fusion；fusion 字段直接授权交易、替代 PM score/rank、进入 Trader 执行权限 | evidence_fusion 冲突高、确认需求多 |
| `signal_collection_contract` | `signal_collector`，Phase1 | Workflow 已保存并取得真实 `signal_record_id` 的三份 AEC | 唯一统一结构化预测证据包，保留 `source_agent=signal_collector` 与 `collector_decision_boundary=no_trade_authority` | workflow state `signal_collection_contract`；PM final `signal_snapshot.signal_collection_contract` | PM、Reviewer、Researcher | 共享 SCC 校验；pre-backtest SCC contract；daily PG 只核对策略路径物理落地完整性；contract coverage | Collector 生成 AnalystSignal/虚假 ID；缺或重复分析师；缺 SCC；source_agent/boundary 非法；SCC 含 PM 字段、手数、rank、资金部署或交易动作 | 分析师冲突、证据弱、缺确认项 |
| `signal_snapshot.signal_collection_contract` | PM Step6 返回、保存层物理化，Phase1 | workflow state 原始 SCC | 原始 SCC 快照 | `futures_recommendation.signal_snapshot`；recommendation artifact | Reviewer、Researcher | daily PG 只核对策略路径物理落地完整性 | PM 重建、补造、改写 SCC；只保存 SCC ref；完整 SCC 缺失；source_agent/boundary 错 | SCC 证据弱、冲突多 |
| `final_action_contract` | PM Step6，Phase1 | 已验证 SCC、持仓、账户、Router 截止点内具体合约、冻结 canonical 学习和 PM 工具输出 | 唯一可执行策略合约。新增风险的 `execution_profile/trigger_source/entry_trigger/invalidation/invalidation_level` 必须来自同一被选 technical/event AEC；结构化`trigger_confirmation_adjustment`只可来自weak-conflict权限或同品种/方向/setup/canonical trigger的正式canonical open/add学习，并由Trader按既有profile执行，不解析reason文本；`valid_until`只约束首次成交前。PM只从已验证SCC重建内部证据，分别组装同方向technical结构失效价、technical确定性原始ATR、同方向fundamental期限/中期方向和event修正；不直读原始AEC。次日PM以结构位与ATR并行止损；初始ATR止损沿用真实开仓价和既有倍数，按开仓日至T-1已完成结算价计算最有利价，浮盈达到1个原始ATR后启用相距1个原始ATR且只能收紧的移动保护。结构位、初始ATR、移动保护任一触发即签唯一exit。原FAC尚未失效的新仓在前两个交易日亏损达到0.5%且当日同向证据再验证失败时减仓50%，亏损达到2%时退出；技术明确反转签exit、基本面中期反向签reduce、期限到达只触发复评。wait/hold/reduce/exit由PM每天只签一个动作且无rank | `futures_recommendation.signal_snapshot.final_action_contract` | Auditor、Trader、Reviewer、Researcher | PM self-check；Auditor；daily PG只核对唯一交易来源和成交事实 | 执行字段跨源拼接；PM绕过SCC直读AEC；入场/持仓失效串用；fundamental成为Trader来源；自由文本或期限冒充数值止损；缺FAC或自检失败；动作/手数不一致；未确认却直执行 | 合法no trade、预算不足、弱证据、入场先失效或到期而不成交 |
| `pm_six_step_trace` | PM Step6，Phase1 | 唯一最终 `final_action_contract` 与最终 `FuturesRecommendation` | `step6_contract_generation_check`、`pm_contract_self_check` | `signal_snapshot.pm_six_step_trace`；recommendation artifact | Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试 | 缺任一最终检查；check failed；Step1-5 中间状态、早期生命周期和跨步骤比较结果进入 trace | 最终检查通过后的 no trade、学习为空、候选降级 |
| `artifact_phase_boundary` | 各智能体对外 artifact 写入端 | 本角色授权事实、上游正式输出摘要 | 阶段白名单 artifact | signal / recommendation / audit / transaction / settlement / reviewer / research artifact | 下游智能体、contract coverage | pre-backtest artifact boundary；daily PG 只检查对应物理结果是否可读取 | 下游 artifact 复制完整上游合约；Trader 保存 PM 学习/rank；Reviewer 写最终 action-value；Researcher 改当天交易事实；Accountant 保存学习字段 | artifact 摘要字段不足、诊断信息较少 |
| `lifecycle_learning_trace.decision_learning_rows` | PM Step6 合约装配 | 最终 `final_action/current_lots/target_lots`、contract lifecycle、有效 action-value | Step6 final 决策层学习 rows | `final_action_contract.learning_used.pm_lifecycle_learning_trace.decision_learning_rows` | PM self-check、Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试 | 复制 Step4 临时 router rows；open/rank 混入 hold/reduce/exit/execution/conditional_monitor；reduce_exit 混入 open/add/execution；hold 混入非 hold；conditional_monitor 混入非 conditional_monitor；trace lifecycle 与最终合约不一致。仅允许精确命中并真实造成同方向减仓的canonical负向hold记录保持原family/lane进入reduce FAC，不得全局放宽 | 对应生命周期没有有效学习，允许 rows 为空且不阻断当日候选 |
| `lifecycle_learning_trace.trigger_profile_learning_rows` | PM Step6 合约装配 | execution / trigger / profile 类 action-value | 触发画像与执行质量学习 rows | `final_action_contract.learning_used.pm_lifecycle_learning_trace.trigger_profile_learning_rows` | PM self-check、Reviewer、Researcher | PM self-check | execution/profile 学习 direct-to-rank；改变 final_action、target_lots、lots_delta、rank、资金部署 | execution 学习为空、触发质量弱 |
| `learning_used` | PM Step6 合约装配 | 有效 action-value、检索摘要、剔除/降级诊断、memory requirements | PM 最终合约学习证据容器 | `final_action_contract.learning_used` | PM self-check、Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试；contract coverage | `learning_used` 含第二套交易计划；formal 与 diagnostics 混层；缺 memory requirements；execution/profile 学习直接改 rank/手数/final_action | 没有命中有效学习、命中层级弱 |
| `learning_used.alpha_setup_action_values` | PM Step6 合约装配 | `decision_memory_retrieval` 返回且与最终 `decision_learning_rows` 同生命周期匹配的正式 canonical action-value | PM 实际声明消费的正式 action-value 主证据列表 | `final_action_contract.learning_used.alpha_setup_action_values` | PM self-check、Reviewer、Researcher | PM self-check purity；pre-backtest PM 输出契约测试 | 缺 `canonical_action_family`；缺必需的 `action_preference` 或 preference 违反 canonical family；缺 `action_value_lane`；缺 `learning_lane`；`canonical_action_value != true`；`consumer_scope != pm_learning`；future dated；incomplete prior 或未匹配最终生命周期的记录混入 | 列表为空、同类样本少、弱命中；空列表是合法冷启动，不阻断 probe 或正式候选 |
| `learning_used.memory_retrieval.rejected_or_downgraded` | PM learning retrieval / Step6 装配 | 被 PM 候选学习集合剔除的 weak prior、incomplete prior、降级行 | 诊断检索材料，记录剔除原因 | `final_action_contract.learning_used.memory_retrieval.rejected_or_downgraded` | Reviewer、Researcher | PM self-check 边界；pre-backtest artifact boundary | 参与 score/rank/手数/final_action；被当作正式 action-value 主证据 | weak prior 多、同类历史不足 |
| `effective_memory_summary` | `pm_decision_memory_retrieval` | 研究库 action-value、profile、state、有效日期 | PM 记忆检索质量摘要、有效数量、剔除原因、匹配层级 | PM 输入对象；`final_action_contract.learning_used` 摘要 | PM、Reviewer、Researcher | contract coverage；pre-backtest memory fixture | 空壳历史覆盖真实历史；future learning 进入 PM；非 `pm_learning` 进入 PM 正式学习；当前 canonical setup 的正式检索异常被吞成空历史或冷启动 | 检索成功但结果为空；当前 setup 缺失而合法走既有降级层；弱匹配、样本少 |
| `analyst_entry_exit_learning_projection` | `alpha_setup` / `analyst_learning_context` / technical参数校准 | T+1可见的完整episode、同品种合法formal action-value的内嵌`analyst_calibration`摘要、当前技术数据 | technical可见无ID、无历史绝对价的canonical触发及入场/触发质量结论，用于校准当前profile与确认强度；相对结构/ATR距离、期限、退出原因和结果继续服务角色内退场校准；技术参数先有界应用并重算当日指标 | 同一批安全投影同时进入分析师内部 prompt 与 quality/finalization 校准上下文；入场质量摘要只给technical，当前AEC仍由分析师finalization生成 | 三个分析师；technical finalization | 普通分析师学习/最终化回归 | 历史绝对价格或旧episode自由文本进入prompt；入场质量泄露给非technical；raw PM记录、ID、reward、rank、手数或保证金泄露；未复核内嵌calibration版本/作用域；合格正式open/add投影仅留在后置校准而从Prompt强制清空；Prompt items与确定性校准items不是同一批安全投影；学习重选方向、创建机会或改写原始ATR14；same-day/future学习进入 | 无历史、无匹配正式学习、当前结构不支持历史profile、结构位被当日参考价校验清空 |
| `opportunity_scorecard` | PM 内部状态判断，Step2–4 | SCC、证据融合、当前持仓、风险上下文、冻结 canonical 学习池 | Step2 固定 `preferred_side/side_priority`；Step4 在学习消费完成后形成唯一最终scorecard，并决定生命周期、candidate quality、probe/real/alpha_scale及层内连续计划比例；不得读取未来Step5 rank | 同一个 PM 内存状态；Step6 仅写矩阵登记摘要 | PM Step4/Step5、Reviewer、Researcher | contract coverage；PM最终合约间接校验 | 学习重选方向；Step4依赖rank；scorecard独立artifact或第二套交易计划；similar/weak/incomplete参与正式升层 | 冲突、候选质量低、无学习的合法冷启动probe或未获预算 |
| `opportunity_score_components` | PM scorecard / signal fusion | SCC、学习摘要、证据融合、风险边界 | PM 机会评分分项 | PM 内部结果；`final_action_contract.evidence_used` 摘要 | PM Step5 rank、Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试 | score component 被 Trader 当交易意图；学习分项直接生成手数；执行学习直接推 rank | 正向学习弱、负向学习强、冲突高 |
| `rank_capital_layer_contract` | PM full-market capital deployment，Step5 | Step4已定层且实际增加风险的候选、资金状态和rank政策 | 七项有符号 `rank_score` 只求和一次：冷启动证据 + 层级6/3/0 + canonical open/add历史学习 + setup历史 + 当日SCC触发质量×0.08 + 一次资金效率 - 风险；只按score降序、ticker排序生成 `rank_budget_sequence` | `final_action_contract.capital_deployment` | PM self-check、Trader只读预算顺序、Reviewer、Researcher | 普通PM回归；pre-backtest PM路径 | 历史trigger重复进入当日trigger分项；第二排序；截断/负分禁入；非新增风险带rank；层级不能保证scale>real>probe；Phase2不按序 | 合法负rank probe、未获预算、同层证据或学习较弱 |
| `position_sizing_result` | PM position sizing tool | 持仓、合约乘数、保证金率、风险参数、目标资金层 | 确定性手数计算结果 | PM 输入；`final_action_contract.evidence_used.position_sizing_result` | PM Step6、PM self-check、Auditor（仅取 `target_margin_ratio_estimate` 作为硬保证金上限输入）、Reviewer | PM self-check；contract coverage | sizing 工具直接签最终交易；空对象冒充 sizing；结果与 `target_lots/lots_delta` 不一致；Auditor 重算 sizing | sizing 被风险上限压低 |
| `audit_verdict` | Auditor，Phase1 | 完整 FAC；权益、保证金、保证金比例、`risk_status`；当前持仓；SCC 数据质量摘要；具体合约及失效边界事实；主配置硬保证金上限 | `approve` / `approve_with_warning` / `block`、hard/soft risk reasons、完整审计 payload；新增风险保证金投影统一为 `current_account_margin-current_ticker_margin+target_ticker_margin` | recommendation audit fields；audit artifact | Trader、Reviewer、PG | Auditor contract audit；daily PG 只核对审计与执行的外部事实 | 输入事实缺失；清算账户或硬保证金上限阻止新增风险；具体合约/失效边界非法；硬数据错误；Auditor 改方向、手数、FAC，或复审 PM 学习/融合/rank/预算/sizing；阻断后 Trader 仍执行 | 数据质量 warning、风险接近硬上限 |
| `execution_contract` | Trader 执行入口，Phase2 | 审计通过的FAC、分钟行情、执行配置 | 只含入场执行白名单。Trader按真实分钟时序持续比较canonical触发、`invalidation_level`和`valid_until`：作废先到则`fac_invalidated_before_entry`并当日永久不成交；触发先到且成交前未失效，下一合法1分钟只成交一次。策略reduce/exit同样必须取得合法1分钟成交基准；真实非异常空结果形成零transaction，禁止回退盘前参考价伪造成交，分钟接口异常继续hard fail。成交后不反手、不保护平仓、不运行策略退出判断；下一交易日PM决定唯一hold/reduce/exit | Trader runtime payload；transaction audit摘要 | Trader、Reviewer、Researcher | 普通Trader时序回归；daily PG只核对外部成交事实 | 复制PM学习/rank；解析自由文本；入场/持仓失效混用；到期后成交；作废后恢复；策略平仓无合法分钟价却回退盘前价成交；同品种同日第二个策略动作；改target_lots | 合法未触发、入场先失效、FAC到期、价格错过、真实分钟空结果、硬保证金未成交 |
| `futures_transactions` / transaction payload | Trader，Phase2 | audit passed contract、盘中触发、成交价格、合约信息 | 仅真实成交事实及执行审计 | `futures_transactions`；transaction artifact；audit payload | Accountant、Reviewer、Researcher、PG | daily PG trade source audit | 未成交写入 transaction；成交不来自最终合约；无 open authority 却开仓；缺触发记录；source_type 错；交易手数超合约授权；运营单污染策略单 | 滑点大、部分成交 |
| `execution_result` | Trader，Phase2 | 审计通过的 final contract、盘中触发、成交/未成交事实 | 执行结果、状态、真实成交列表、未触发/未成交/失效/市场规则阻断原因 | recommendation `signal_snapshot.execution_result`；execution result artifact；Reviewer / Researcher input | Accountant、Reviewer、Researcher、PG | daily PG execution result lineage | execution result 改 PM 合约；缺 recommendation lineage；source_type 错；结果与 transaction 不一致；把未成交伪造成交 | 审计通过但未触发或未成交、部分成交、滑点偏大 |
| `execution_learning_trace` | Trader / futures audit helper，Phase2 | 执行结果、触发状态、成交质量 | 执行学习 trace，`consumer_scope=trader_execution_learning` | execution result；Reviewer input；Researcher input | Reviewer、Researcher | pre-backtest execution trace contract；daily PG 不复查其内部学习语义 | bare execution trace 缺 consumer_scope；Trader 用 trace 下单；trace 改 PM 合约 | 触发质量弱、成交质量差 |
| `portfolio.positions` / `daily_settlement.positions_snapshot` | Accountant，Phase3 | 成交、持仓、结算价、手续费、保证金率、合约乘数 | 结算后的当前持仓、日结算持仓快照、PnL、权益和保证金事实 | `portfolio.positions`；`daily_settlement.positions_snapshot`；`ticker_daily_pnl`；settlement artifact，不存在独立 position SQL 路径 | Reviewer、Researcher、PG、评估 | Phase4 review；daily PG accounting boundary | 两份持仓事实不一致；成交重复入账；改交易动作；用学习或 LLM 改账；结算与成交不一致；写 PM rank/learning 字段 | 当天亏损、保证金利用偏低、实际敞口偏离 PM 规划预算 |
| `reviewer_phase4_review` / review facts | Reviewer，Phase4 | recommendation、audit、transaction、settlement、execution result、phase 状态 | Phase4 验收、交易日志、事实归因、研究输入材料 | reviewer artifact；review payload | Researcher | Phase4 gate；daily PG 只核对 Phase4 状态和物理结果可读性，不复查结论 | Reviewer 下单、调仓、写最终 action-value、改交易事实、触发 Researcher LLM 直接改当天 | 预算漂移 warning、执行质量差、归因不利 |
| `researcher_llm_notes` | Researcher，Phase4 与结算完成后 | 通过正式 ID 链验证的 AEC → SCC → FAC → Auditor → execution_result → transaction → settlement 事实包；参考组合为正式 `Prev(T)`，链上业务事实为逻辑 `T` | 经结构校验的 evidence pack 与 `validated_output`；禁止保存 prompt、原始 response、内部推理和未验证工具结果 | `researcher_llm_notes.evidence_pack_id`、`payload_json` 及 payload artifact 元数据；`raw_prompt/raw_response` 和对应 artifact 元数据固定为空 | Research writer；分析师正式校准检索；PM `decision_memory_retrieval` | data time boundary；structured IO；正式 ID lineage | 参考组合不等于正式 `Prev(T)`；持久化AEC/SCC/recommendation/结算日期不等于逻辑T；来源记录/日期/ID断链；保存原始模型内容；使用未结算或未完成Phase4日期；改当天事实；输出当日交易指令 | 合法零成交、无合格学习成果、研究观点弱、样本少 |
| `alpha_setup_action_value` | Researcher 写入工具，Phase4 后 | open/add 每个已结算完整策略 episode 只提供一个聚合 reward 与 `return_on_notional`，物理pair仅为经济明细；hold/reduce/exit/execution 可使用对应日记录；未交易机会只作既有弱先验 | 候选 action-value：`action_name -> canonical_action_family -> action_value_lane/learning_lane -> action_preference`；payload 保存 `mean_return_on_notional/worst_return_on_notional` 和最新完整周期收益率，PM Rank、仓位学习、分析师安全校准及开仓触发确认均使用手续费后名义收益率；同完整作用域最新亏损同步撤销 PM 与分析师旧正向加分；人民币 reward 保留既有学习生命周期与审计用途；只有字段完整、语义一致且具正式 preference 的记录可成为 PM canonical 学习 | `alpha_setup_action_value` DB；payload_json 同值保留 | 分析师安全投影、PM next-day retrieval、Reviewer、Researcher | 普通跨日 episode→action-value→分析师/PM retrieval 回归；相同收益率跨品种/乘数/人民币盈亏/手数的分析师强度、Rank学习分及触发确认相同；pre-backtest action matrix contract | 裸 transaction 或日 PnL 碎片生成 open/add reward；人民币盈亏大小进入分析师强度、Rank或触发确认；最新亏损未撤销分析师旧正向Profile；一个episode的分批pair分别增加sample/trade/tail loss；缺 canonical family/lane/preference；future dated；非策略/rollover/forced_risk 污染 strategy action-value | episode 没有形成正式 preference、observe 空 preference、样本少、reward 弱均合法 |
| `alpha_setup_profile` / product learning | Researcher 写入工具，Phase4 后 | 完整 episode 聚合交易次数/胜负/PnL；日记录补充非 open/add 生命周期与执行事实；成交型样本的四项正式身份只继承原开仓 FAC，execution 另用 `execution_retrieval_key` 区分执行方式 | 产品/setup/trigger 历史表现；PM 正式读取必须精确匹配当前正式 setup，持仓期间完整身份来自原开仓 FAC；分析师探索读取可保持宽范围 | research DB | 分析师校准、PM product learning、Researcher | contract coverage；data time boundary；PM 跨 setup Profile 隔离；成交型 FAC 身份单源回归 | 将一笔 episode 的每日 PnL 碎片重复计成多笔交易；PM Profile 查询缺 setup 条件；跨 setup Profile 进入 Rank、仓位或放大；成交型学习从当日 SCC 重新推导身份；直接写手数、交易授权、当日合约修改 | 产品表现差、setup 样本少 |
| `adaptive_policy_state` / `provisional_policy_state` / `config_learning_overlay` | Researcher / 配置学习写入工具，Phase4 后 | 长窗口研究结果、验证通过的参数证据、回滚值；成交结果政策继承其上游 FAC 身份；PM 新机会按当天 SCC/FAC 检索，已持仓、减仓、退出按原开仓 FAC 检索；PM 生命周期校准只读 FAC 中的 `pm_lifecycle_learning_impact_delta`；未交易机会只有固定5日、同作用域、完整正负样本且FAC可执行依据完整的 `missed_alpha_accountability` 路径可生成 `fast_candidate_alpha` | 策略参数学习状态、候选参数、回滚信息；市场状态统一使用小写下划线格式；`fast_candidate_alpha`只允许下一交易日同作用域probe/小仓复核，Profile candidate/watchlist不生成该政策，未交易反事实不得生成成熟`alpha_promotion`；当前无合格参数优化生产者时不得复制原配置并标记为 overlay 刷新，既有原值复制 overlay 按原来源与原因精确停用 | research / config learning DB | 开发验收、PM 配置读取、PG | pre-backtest config consistency；PM 生命周期正式契约行为测试；data time boundary；新机会/持仓政策路由回归；fast-candidate生产来源、停用归属与消费权限回归 | 未验证参数直接生效；持仓政策用当天反向机会身份检索；Profile与未交易路径共用fast-candidate生产权；未交易影子结果按最佳周期晋升成熟alpha；fast-candidate跨来源停用或错误来源进入PM；同义市场状态因空格/下划线格式不同降级；复制当前配置冒充已学习参数；从旧 PM 内部对象读取生命周期；缺 rollback；改当天交易事实；绕过 PM 合约权限；未来数据参与参数 | 候选策略待验证、样本不足、无合格参数优化结果时 overlay 写入为零 |
| `trade_episode_memory` / `no_trade_opportunity_memory` | Reviewer / Researcher，Phase4 后 | 已结算完整策略持仓周期及其物理pair经济明细、未交易机会、未触发条件机会；完整 episode 的 `setup_type/horizon_class/expected_horizon_days/market_regime` 直接继承原开仓 FAC；未交易身份只继承对应 FAC | 可为空的未来学习事实底座；episode 不是 formal action-value；四项开仓身份任一缺失时不写正式完整 episode；未交易 FAC 身份不完整时不写学习记录；多周期影子结果只作诊断 | research DB；artifact；`entry_trigger` 保存在既有 payload 与 `next_round_memory_contract.scope` | Researcher；分析师与 PM 只消费后续按各自作用域形成的正式摘要/action-value，不直接读取 episode 表 | data time boundary；完整 episode 开仓 FAC 四字段单源；未交易身份 FAC 单源回归 | 前视；完整 episode 的 horizon/regime 从当日分析师或 SCC 重新推导；未完成 Phase4/结算、裸 transaction、非策略、rollover 或 forced_risk 进入策略 episode 学习；未交易身份重建；要求每笔交易都形成学习 | 无合格 episode、学习为空、机会少、错过交易、FAC 身份不完整而跳过正式学习、固定周期样本不足而不晋升 |
| `research_position_feedback` | Researcher，Phase4 后 | `learning_used.alpha_setup_action_values` 与最终 `pm_lifecycle_learning_trace.decision_learning_rows` 的正式 ID/canonical/scope/family/lane 匹配，以及后续执行和结算事实 | 仅记录 PM 实际消费过的正式学习如何进入最终仓位链 | research DB | Researcher 与开发评估诊断 | 普通反馈回归；daily PG 不复查内部学习作用过程 | 从 legacy selected refs、未匹配记录或未消费学习伪造反馈；把反馈反向作为 rank、手数或交易输入 | 合法无学习、学习未匹配、学习被消费但未成交、学习为空 |
| `trading_day_phase` | workflow，Phase1-Phase4 | 各阶段运行结果 | 阶段状态与时间戳 | `trading_day_phase` DB | PG、backtest gate、Reviewer | daily PG phase audit | 存在业务记录但 phase 未 completed；阶段顺序断裂；失败残留跨日污染 | 当天无交易但 phase 完整 |
| `contract_coverage_audit` | PG，回测前 | 可导入生产代码、正式 schema、机制文档和真实路径测试 | 六维版本级契约覆盖矩阵：producer、physical_landing、consumer、role_check、real_path_test、mechanism_doc | pre-backtest report | 开发者、回测闸门 | pre-backtest acceptance | 任一六维 runtime/document evidence 缺失；依赖字符串命中、废弃函数或禁用代码冒充 coverage；核心契约缺字段表登记 | coverage 完整但真实样本少 |
| `pre_backtest_acceptance` | PG，回测前 | DB schema、代码、配置、fixture、契约覆盖、系统不变量；指定窗口与配置品种的只读市场、合约、Finoview 和新闻数据入口 | 回测前 readiness 结论 | pre-backtest report | 开发者、回测脚本 | 回测前闸门 | 通用系统不变量 fixture 失败；schema 断裂；越权字段；contract coverage 缺口；字段矩阵断裂；交易日、PandaAI 日线开收盘价、官方结算价、主力合约映射、合约乘数、保证金率、具体合约信息或 Trader 分钟行情接口能力等交易必需数据断裂 | LLM 配置或密钥环境变量缺失、某品种某日无新增基本面或新闻；PG 不发起 LLM 鉴权请求 |
| `system_invariant_audit` | PG，每日回测后 | 当日 DB、artifact、phase、字段矩阵 | 系统不变量报告 | daily gate output | 开发者、回测脚本 | daily gate | 应落地的 final contract 或 SCC 缺失、artifact 污染、越权、交易不来自唯一合法来源、执行/成交/结算事实不一致、阶段断裂 | 无交易、收益差、学习为空或未产生、PM 内部自检/rank/学习作用过程 |

## 4. 回测前通用不变量 fixture 矩阵

这些 fixture 必须在真实回测前运行，用代表性结构化样本证明字段、动作、职责、阶段和唯一交易事实等通用不变量仍然成立。历史问题可以贡献样本，但不得把“某个旧错误是否再次发生”作为检测目的，也不得围绕单次故障维护专用门禁。

| 不变量场景 | fixture 输入 | 必须命中的 gate | hard fail 断言 |
|---|---|---|---|
| SCC 缺失 | PM recommendation 只有 `signal_collection_contract_ref`，缺 `signal_snapshot.signal_collection_contract` | pre-backtest acceptance / contract coverage SCC fixture | `signal_snapshot.signal_collection_contract` 缺失 hard fail |
| SCC source_agent/boundary 错 | SCC `source_agent` 非 `signal_collector`，boundary 非 `no_trade_authority` | pre-backtest SCC fixture | source_agent/boundary 非法 hard fail |
| PM artifact 混入 incomplete prior | `canonical_action_value=false` 的 similar/fallback prior 塞进 `learning_used.alpha_setup_action_values` | PM self-check fixture | formal action-value 主列表污染 hard fail |
| observe 空 `action_preference` | `canonical_action_family=observe`，lane 为 `hold`，preference 为空 | PG action matrix fixture | 合法通过；禁止误报 missing preference |
| observe 冒充交易偏向 | observe 行带 `positive_candidate_open/exit/execution/hold` | PG action matrix fixture | family/lane/preference 不一致 hard fail |
| Step4 临时路由 / Step6 final trace 混用 | Step4 临时路由是 hold，Step6 final 是 open/rank，最终 decision rows 带 hold | PM self-check fixture | final lifecycle trace 污染 hard fail；禁止比较早期路由与最终生命周期本身是否一致 |
| execution/profile 污染决策层 | reduce_exit final contract 的 decision rows 带 execution | PM self-check fixture | decision rows 污染 hard fail |
| execution/profile 合法分层 | reduce_exit final contract 的 trigger profile rows 带 execution，direct-to-rank false | PM self-check fixture | 通过；不误判为 reduce_exit 污染 |
| action family/lane/preference 不一致 | `positive_candidate_open` 配 `reduce_exit`；缺 family；缺 lane | PG action matrix fixture | hard fail |
| Trader 越权字段 | transaction payload 保存完整 PM 合约、学习、rank、资金解释 | artifact boundary fixture | hard fail |
| Reviewer 越权字段 | Reviewer artifact 写最终 action-value、研究状态、当天交易事实改写 | artifact boundary fixture | hard fail |
| Researcher 越权字段 | Research artifact 改当天合约、成交、结算、PnL | artifact boundary fixture | hard fail |
| Trader 成交不来自合约 | open transaction 缺 final contract open authority | single trade truth fixture | hard fail |
| 未完成交易日进入学习 | phase 未 completed，存在 recommendation/transaction/learning | data time boundary fixture | hard fail |

## 5. Daily PG 审计边界矩阵

每日 PG 固定输出以下七项。它只读核对当日已经形成的物理事实，不进入智能体内部，不重复 PM 自检、Auditor 审计或 Reviewer 复盘。

| 正式检查名 | PG 必审 | PG 禁审 |
|---|---|---|
| `daily_phase_completion` | Phase1→Phase4 completed 状态、真实时间顺序，以及实际生成的 Researcher completion 事件晚于 Phase4 | 阶段内推理质量；无交易日是否策略无效 |
| `physical_result_landing` | 只对实际进入的路径要求对应落点；SCC 通过共享完整校验；三个真实 `signal_record_id` 精确对应三名分析师 SQL AEC、ticker 和日期；artifact/持久化无 prompt、原始 response、隐藏上下文和未登记字段 | 信号强弱是否足够交易；分析师内部工作过程 |
| `single_trade_fact_source` | 每笔 transaction 显式登记 `strategy`、`rollover` 或 `forced_risk`；三类来源分别绑定唯一 FAC、rollover policy 或 forced-risk boundary | 把运营交易强套 PM 策略合约；评价交易方向和收益 |
| `audit_release_and_execution_result` | strategy 成交具有完整 Auditor payload、允许执行的 verdict 和 FAC 授权；block/require_review 不得成交；approve 后合法未触发、未成交或部分成交允许 | 重做 Auditor 硬风险判断；把 approve 解释为必须成交 |
| `execution_and_transaction_fact` | recommendation ID、动作、方向、具体合约和累计成交手数与 FAC 授权一致；execution_result 与 transaction 一致；仅条件 FAC 核对盘中决策 | 复判 Trader 内部推理、追价和择时质量 |
| `settlement_and_account_fact` | transaction 只入账一次；逐品种手数、`portfolio.positions`、`daily_settlement.positions_snapshot`、ticker PnL、手续费、保证金、现金和权益守恒 | 要求实际成交等于 PM 预算；因实际净敞口偏离规划预算判错；评价当天盈亏 |
| `learning_record_landing_boundary` | 只检查实际生成的学习记录；核对 Phase4、结算、来源日期、正式 ID 和 canonical action-value；成交型学习追溯真实 transaction/settlement，反事实机会不伪造 transaction | 要求每笔交易产生学习、要求每次决策使用学习、评价学习质量或改写历史事实 |

## 6. 修改检查清单

每次修改链路时按下列顺序执行：

1. 定位本文矩阵行。
2. 确认字段在 `docs/matrix_field_semantics.md` 已登记。
3. action-value 改动同步对照 `docs/matrix_action_canonical.md`。
4. 生产端与落盘端同轮修改。
5. 消费端与自检同轮修改。
6. 行为回归测试覆盖具体失败形态；pre-backtest 只运行通用不变量与 readiness 验收，不增加按历史错误命名的生产检查分支。
7. daily PG audit 只审系统契约，不复判策略。
8. 真实路径测试证明 producer-to-consumer 字段保真。
9. 修改 `.py/.yaml/.yml` 后更新 `docs/work_log.md`。

## 7. 固定结论

- PM final contract 是策略交易唯一真相。
- SCC 主证据是 `signal_snapshot.signal_collection_contract`。
- action-value 主语义是 `action_name -> canonical_action_family -> action_value_lane/learning_lane -> action_preference`。
- PM formal action-value 主列表只保存完整 canonical 证据。
- weak prior 只进入 diagnostics。
- observe 空 preference 是合法观察语义。
- `final_action_contract` 中由 Step6 重新形成的 final lifecycle trace 是 PM 自检唯一决策层学习 trace；`pm_six_step_trace` 只保存两个最终检查。
- pre-backtest gate 用代表性样本证明通用系统不变量，不以复现历史故障为目标。
- daily PG audit 只 hard fail 已落地物理结果中的系统契约断裂，不读取或复查任何智能体内部机制。
- 策略优劣只由长期策略评估判断。
