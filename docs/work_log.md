# AgentQuant 工作日志

本文件是基于当前系统状态重整后的短版开发索引。保留按天划分的结构，只记录仍能解释现有代码、配置、字段、智能体边界和回测前验收的修改。已被后续重构覆盖的中间补丁、旧字段口径、旧工具名、旧运行入口和纯讨论内容不再保留。

字段语义以 `docs/unified_field_semantics.md` 为准；PM 内部链路以 `docs/mechanism_pm.md` 为准；workflow 编排边界以 `docs/mechanism_workflow.md` 为准；智能体权限、事实入口和 artifact 边界以 `docs/mechanism_multiagents.md` 为准；数据载体与 DB schema 口径以 `docs/mechanism_data_model.md` 为准。

每条只保留：修改了什么、为什么改。

==========2026年07月06日==========

（1）新增唯一全市场 rank 评分策略配置入口。修改：新增 `src/config/rank_score_policy.yaml`，同步 `dev.yaml`、`config_normalizer.py`、`pm_signal_fusion.py`、`pm_full_market_capital_deployment.py`、`parameter.md`、`mechanism_pm.md`、统一字段语义表和回归测试。原因：rank 评分机制需要能在 40 个干净交易日后依据 rank 分层平均收益微调，但不能再通过改代码硬编码权重完成；该配置只影响 `rank_score/rank_score_components` 和资金效率小修正，不创建交易权限。

（2）收口 PM 六步主链入口、顺序和工具职责。修改：`portfolio_manager.py`、`pm_signal_fusion.py`、`pm_ticker_side_selection.py`、`pm_lifecycle_action_port.py`、`pm_lifecycle_learning_router.py`、`pm_full_market_capital_deployment.py`、`pm_contract_builder.py`、`pm_contract_self_check.py` 和回归测试。原因：PM 按 `1 -> 2 -> 3 -> 4 -> 5/6` 线性运行；第 2 步生命周期动作口先于 scorecard/side selection；第 4 步 lifecycle router 是唯一学习路由；第 6 步才签唯一 `final_action_contract`。

（3）结构性重写 PM 签约时点和候选隔离。修改：`portfolio_manager.py`、`pm_full_market_capital_deployment.py`、`workflow.py` 和决策工作流回归测试。原因：单品种阶段只生成 `pm_internal_candidate`；第 5 步只写 `pm_capital_deployment_decision`，不创建、修复或改写最终合约；第 6 步由 `finalize_pm_full_market_contracts()` 统一调用 builder 和 self-check 签约。

（4）收口 PM 双路径签约边界。修改：`portfolio_manager.py`、`pm_full_market_capital_deployment.py`、`pm_contract_builder.py`、`pm_contract_self_check.py`、`workflow.py` 和回归测试。原因：新增风险路径必须有第 5 步部署事实，缺失直接 fail；非新增风险路径不要求第 5 步，但最终合约必须写明 `non_new_risk_no_capital_rank`，且不得伪造 rank/deployment。

（5）收口 workflow 保存链只读 hard gate。修改：`workflow.py` 和阶段流回归测试。原因：workflow 保存前只检查 PM 已签合同是否存在、PM 中间态是否清空、`pm_six_step_trace.pm_contract_self_check.ok == true`；workflow 不修、不补、不保存自检失败合同。

（6）收口 workflow 智能体输出零生产边界。修改：`workflow.py`、`signal_collector.py`，新增 `signal_collection_data_unavailable.py`，同步阶段流回归测试。原因：workflow 不能生产任何智能体输出；缺盘前基准价时只设置上下文缺失标志并调度 `signal_collector`，由 signal_collector 签出结构化不可用信号，再交 PM 签唯一合约。

==========2026年07月07日==========

（1）收口 `signal_collection_contract` 生产者边界。修改：`portfolio_manager.py`、`signal_evidence_collection.py`、`pg_contract_coverage_audit.py` 和回归测试。原因：`signal_collection_contract` 只能由 `signal_collector` 产出，PM 缺包、producer 非 `signal_collector` 或 boundary 非 `no_trade_authority` 必须 fail-fast，不能在 PM 内重建证据包。

（2）修正 Step5 未部署新增风险候选进入 Step6 的自检语义。修改：`portfolio_manager.py`、`pm_full_market_capital_deployment.py`、`pm_contract_self_check.py` 和 PM 状态转移回归测试。原因：新增风险候选进入全市场 rank 后若因预算或 rank 未部署，最终合约必须还原为无新增风险敞口并清除盘中触发执行权限；self-check 只接受 `no_rank_no_new_exposure` / `no_rank_or_budget_no_new_exposure` 系列原因，不再误按普通非新增风险路径要求 `non_new_risk_no_capital_rank`。

（3）修正 PM Step6 对旧生命周期 trace 的误用。修改：`portfolio_manager.py` 和 Phase1 回归测试。原因：Step6 是否需要第 5 步资金部署只能按 Step4/门控后的最终 `candidate_contract` 判定；当 RiskGate 已把候选压回 `target_lots=current_lots`、`lots_delta=0`、`final_action=wait/hold` 时，旧 `primary_lifecycle_action_port.requires_full_market_rank=true` 只能作为历史诊断，不能反向要求 `pm_capital_deployment_decision`。

（4）修正 PM Step6 最终合约自检制度。修改：`portfolio_manager.py`、`pm_contract_builder.py`、`pm_contract_self_check.py`、`workflow.py` 和回归测试。原因：废掉“Step2 生命周期结果 vs Step6 最终合约”的比较式自检；Step6 改为在 `pm_six_step_trace.step6_contract_generation_check` 中检查最终合约生成合法性，`check_final_action_contract()` 只检查最终合约自身和 rank/非 rank/Step5 未部署边界，不再读取 `evidence_used.contract_lifecycle_self_check` 作为最终失败依据。

（5）把 PM/workflow 确定性协议检测接入常态回测前检测。修改：`pre_backtest_test.py`、新增 `test_pre_backtest_pm_workflow_contracts.py` 和 CLI 回归测试。原因：回测前总门必须静态验证 PM 三类合约矩阵、Step6 生命周期自检、workflow 保存前只读闸门、Signal Collector/PM 边界、rank 与中间态字段边界；该 gate 不跑真实交易日、不调 LLM、不读真实行情、不写真实 DB。

（6）补齐 PM/workflow 重构后的运行期只读审计。修改：`pg_system_invariants.py` 和系统不变量回归测试。原因：每日回测后检测必须在真实 DB/artifact 中 fail-fast 发现缺 `final_action_contract`、PM 中间态残留、`pm_six_step_trace.pm_contract_self_check` 失败、`pm_six_step_trace.step6_contract_generation_check` 缺失或失败，以及非 rank/Step5 未部署新增风险合约边界漂移；该审计只读，不修合同、不补字段。

（7）统一 PM Step6 自检语义、代码命名和机制文档。修改：`pm_lifecycle_action_port.py`、`portfolio_manager.py`、PM/workflow/字段语义相关测试、`unified_field_semantics.md`、`mechanism_pm.md`、`mechanism_workflow.md`、`mechanism_agent_internal_rules.md`。原因：旧 `build_contract_lifecycle_self_check()` 改名为内部 `build_lifecycle_transition_diagnostic()`，只作为 PM 内部 provenance；最终合约不再保存或读取 `contract_lifecycle_self_check`、`historical_lifecycle_transition_diagnostic`、`initial_primary_lifecycle_action_port`、`lifecycle_port_transition_reason`，最终闸门统一为 `pm_six_step_trace.step6_contract_generation_check.ok == true` 与 `pm_six_step_trace.pm_contract_self_check.ok == true`。

（8）统一 PM/workflow 重塑后的回测前检测、每日后置审计和 PG 旁路审计。修改：`pg_system_invariants.py`、`pg_unified_field_audit.py`、`pg_contract_coverage_audit.py`、`pg_mechanism_effectiveness_audit.py`、`pre_backtest_test.py` 和相关控制测试。原因：检测层只认 `pm_six_step_trace.step6_contract_generation_check` 与 `pm_contract_self_check` 两个最终闸门，保存后旧 lifecycle compare 字段必须 hard fail，contract coverage 不再重复运行，机制有效性审计只检查机制连通和未部署候选/条件监控真实边界。

==========2026年07月08日==========

（1）接入智能体出口契约洁净与下游统一语义理解的回测前 fixture 检测。修改：`portfolio_manager.py`、`pre_backtest_test.py`，新增 `test_agent_output_contract_boundary.py`。原因：内部推理、路由、学习和自检机制继续保留，但各智能体对外 artifact 必须按白名单投影；PM 最终合约清除 `learning_used.memory_retrieval` 等保存路径中的旧生命周期内部诊断字段，Signal Collector 固定 `producer=signal_collector` 与 `collector_decision_boundary=no_trade_authority`，Trader/Accountant/Reviewer/Researcher 的输出边界由 fixture 在回测前验证，并统一通过 `final_action_semantics.py` 与 `evidence_fusion_semantics.py` 理解上游输出。

（2）收住各智能体对上游信息的统一理解入口。修改：`final_action_semantics.py`、`auditor.py`、`trader.py`、`research_review_helpers.py`、`research_learning.py`、`research_memory_writers.py` 和 `test_agent_output_contract_boundary.py`。原因：分析师继续通过分析师学习上下文/校准工具消费研究，Signal Collector 继续通过证据融合语义产出无交易权限证据包，PM 继续只消费 Signal Collector 证据包与 PM 学习路由；Auditor/Trader/Reviewer/Researcher 对 `final_action_contract` 的手数、方向、生命周期、执行权限和研究归因解释统一回收到 `final_action_semantics.py`、`contracts.py`、`order_semantics.py`，避免下游重新私写 PM 字段解释器，同时不改变 PM 六步机制和交易策略逻辑。

（3）对齐 PG 旁路审计与当前智能体出口/理解边界。修改：`pg_mechanism_effectiveness_audit.py`、`pg_contract_coverage_audit.py`、`pg_system_invariants.py` 和相关控制测试。原因：PG 每日机制审计只检查 `final_action_contract`、`signal_collection_contract` 安全摘要、`pm_six_step_trace.pm_contract_self_check`、`pm_six_step_trace.step6_contract_generation_check` 和条件合约执行链路，不再复判 PM 内部 rank、资金部署或 reason 语义；rank 合约完整性仍由 PM self-check 和回测前 fixture 覆盖。

（4）小幅放宽分析师学习上下文召回数量。修改：`learning_policy_catalog.yaml`。原因：将 `learning_context.exploratory_memory.max_episode_items`、`max_no_trade_items` 和 `alpha_setup_profile.max_items` 从 3 调整为 5，让分析师校准能看到更多近期 episode、no-trade 和 setup profile 摘要，同时不改变 action-value 权限边界。

（5）修正 Reviewer Phase4 对 PM 计划预算参数的复盘口径。修改：`reviewer_phase4_review.py`、`test_phase_flow_regression.py`、`mechanism_agent_internal_rules.md`、`mechanism_research.md` 和 `mechanism_multiagents.md`。原因：`max_net_exposure`、`target_margin_ratio_*`、`probe_margin_ratio`、`strong_opportunity_*` 等属于 PM Step5 计划预算/资金层级参数，真实成交后因条件腿未触发、成交子集、价格变化或滑点产生偏离时只进入 `budget_drift_diagnostics`、warning 和事实归因，不再触发 Phase4 hard fail；账户级保证金硬上限、阶段断链、成交/结算不一致、越权成交和 artifact 污染仍保持 hard fail。

（6）同步 Researcher 提示词到 Reviewer 预算漂移事实归因口径。修改：`prompt.py` 和 `test_agent_output_contract_boundary.py`。原因：研究员 LLM 读取 `budget_drift_diagnostics`、warning 和事实归因时，只能把 PM 计划预算漂移当作未来研究输入，不能当作 PM 合约失效、日终交易违规、交易权限或绕过 `final_action_contract` 的依据。

（7）对齐资金配置注释、主配置布局和参数文档口径。修改：`dev.yaml` 布局/注释和 `parameter.md`。原因：明确 `max_total_margin_ratio`、`hard_max_total_margin_ratio` 是账户保证金硬边界，`max_net_exposure`、`target_margin_ratio_*`、`probe/normal/deployable/exceptional/recovery` 等是 PM Step5 计划预算、资金层级和复盘归因参数，避免后续再把计划预算漂移误当 Reviewer/PG 日终 hard fail，同时保留 TQXAI 作为停用备用 LLM 通道。

（8）收住回测前检测与 PG 旁路审计对齐当前系统。修改：`pre_backtest_test.py`、`test_reviewer_transaction_log_readability.py`、`pg_contract_coverage_audit.py` 和 `test_contract_coverage_audit.py`。原因：回测前总门接入 Reviewer 交易日志可读性 gate；契约覆盖固定 Reviewer 预算漂移事实归因、Researcher 提示词边界和 UTF-8 日志输出；PG 仍只验协议边界、PM 自检结果和 artifact 污染，不复判 PM 内部 rank/reason/deployment，也不把 PM 计划预算漂移当 hard fail。

==========2026年07月09日==========

（1）修正 PM recommendation artifact 的信号收集契约落盘。修改：`portfolio_manager.py`、PM/workflow 相关测试和机制文档。原因：PM Step6 最终 `signal_snapshot` 必须保存 workflow state 中 signal_collector 原始 `signal_collection_contract`，保留 `producer=signal_collector` 与 `collector_decision_boundary=no_trade_authority`；`final_action_contract.signal_collection_contract_ref` 只保留为摘要，不能替代可审计主证据。

（2）收住 PG daily audit 对 SCC 的旁路审计边界。修改：`pg_mechanism_effectiveness_audit.py`、机制有效性审计测试和机制文档。原因：每日机制审计只认 `signal_snapshot.signal_collection_contract`，检查存在性、producer/boundary 和 SCC 内 PM 越权字段；不接受弱摘要 ref，不反推 PM 方向、手数、rank 或资金部署语义。该 SCC 任务当时未处理第一个问题“学习语义归一不一致”；本日后续见（3）-（6）。
（3）新增并落地 action-value 动作语义归一机制。修改：`final_action_semantics.py`、`alpha_setup.py`、`sqlite_setup.py`、`sqlite_helper.py`、`research_memory_writers.py`、PM decision tools、`research_review_helpers.py`、`pg_db_schema_contract.py`、`pg_system_invariants.py`、`analyze_strategy_attribution.py`、`docs/action_value_canonical_action_family.md` 和相关测试。原因：`action_name=add_or_open` 的真实业务含义是 open/add 新增风险家族；Researcher、PM、Reviewer、PG 必须共用 `action_name -> canonical_action_family -> action_value_lane/learning_lane -> action_preference`，不能各自用私有字符串集合猜动作含义。

（4）收住 PG action-value 系统不变量审计。修改：`pg_system_invariants.py` 和 `test_system_invariant_audit.py`。原因：PG 不再要求 `positive_candidate_open` 的 `action_name == open`，而是 hard fail 缺 `canonical_action_family` 或 family/lane/preference 不一致；`positive_candidate_open` 必须落在 `open_add_new_risk + open/add/scale/increase`，reduce/exit/execution/hold 同样按统一 family 审一致性，不根据学习偏向反推明日交易动作。

（5）同步字段语义、机制文档和研究边界。修改：`action_value_canonical_action_family.md`、`unified_field_semantics.md`、`mechanism_agent_internal_rules.md`、`mechanism_multiagents.md`、`mechanism_research.md`、`mechanism_data_model.md`。原因：明确 Researcher 写入 canonical family/lane，PM 只经 `decision_memory_retrieval` 消费，Reviewer 只作事实归因理解，PG 只审一致性；Trader 只执行 `final_action_contract`，Accountant 不消费 action-value。

（6）验证结果：`compileall src` 通过；`test_final_action_semantics` 32/32 通过；`test_system_invariant_audit` 75/75 通过；`test_reviewer_learning` 100/100 通过；`test_decision_workflow_tools + test_evidence_fusion_semantics` 27/27 通过；`test_contract_coverage_audit + test_pre_backtest_acceptance + test_pre_backtest_pm_workflow_contracts + test_mechanism_effectiveness_audit` 39/39 通过；`git diff --check` 通过。2025-03-25 daily gate 没有再出现 `action_value_open_preference_on_non_open_lane`；但旧 DB schema 缺 `alpha_setup_action_value.canonical_action_family`，且 SCC 第二问题旧 artifact 仍报 `mechanism_signal_collection_contract_missing`。本轮不补造旧 action-value family，不处理 SCC 第二问题；旧回测 DB 需要重新跑研究写入或执行明确的一次性迁移后才能过新 schema gate。

==========当前验证口径==========

（1）回测前总门：`src/run/pre_backtest_test.py`。

（2）每日回测后总门：`src/run/backtest_daily_test.py`。

（3）结构测试重点：事实入口、合约解析、artifact 边界、结算公式、研究写入、控制组只读、PM 状态转换、分析师输出落地。

（4）回测前验收只检查系统可运行性、字段/schema/权限/硬数据/边界和确定性转换规则，不评价策略收益。

（5）重塑 PG 旁路审计、回测前检测、每日回测后检测与相关测试边界。修改：`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py`、`pg_contract_coverage_audit.py`、`pg_pre_backtest_acceptance.py`、`pre_backtest_test.py` 和相关控制测试。原因：PG 定死只验协议边界、artifact 污染、唯一交易事实和 PM 自检结果，不再复刻 PM 内部交易语义；回测前入口只编排静态/fixture/fake DB 检查，每日后置入口只读真实 DB/artifact 调 PG 工具 fail-fast，测试文件只验证工具判定，不成为第二套审计。
