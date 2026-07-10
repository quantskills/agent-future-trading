# AgentQuant 工作日志

本文件只记录 `.py`、`.yaml`、`.yml` 的行为或配置修改；纯文档同步、纯验证命令、分支/提交固定、数据清理不记。

==========2026年07月06日==========

（1）[rank 配置] 新增 `src/config/rank_score_policy.yaml`，同步 `src/config/dev.yaml` 与 PM rank/score 相关 Python 工具。原因：rank 权重可在干净交易日样本后配置化微调，不再靠代码硬编码。

（2）[PM 六步主链] 收口 `src/agents/decision_team/portfolio_manager.py` 与 `src/tools/agent_tools/decision/` 下 PM 工具的 `1 -> 2 -> 3 -> 4 -> 5/6` 顺序。原因：生命周期动作、方向选择、学习路由、全市场部署和 Step6 签约必须各司其职。

（3）[PM 签约时点] 在 `portfolio_manager.py` 中将单品种阶段限制为内部候选，最终合约统一由 Step6 `finalize_pm_full_market_contracts()` 签出。原因：避免 Step5、workflow 或中间态提前生成/改写交易合约。

（4）[PM 双路径] 在 PM Step5/Step6 相关代码中明确新增风险路径必须有 Step5 部署事实，非新增风险路径不得伪造 rank/deployment。原因：区分 open/add 新风险和 hold/reduce/exit 等非新增风险合约边界。

（5）[workflow 只读保存] 调整 `src/graph/workflow.py` 保存前检查，只检查 PM 最终合约、自检和中间态清洁，不修补字段。原因：workflow 是编排层，不生产 PM 交易语义。

（6）[缺数据信号] 调整 `signal_collector.py` 与 workflow 传递逻辑，缺盘前基准价时由 signal collector 输出结构化不可用信号。原因：保持智能体输出零代工边界。

==========2026年07月07日==========

（1）[SCC 生产者] 收口 `signal_collection_contract` 只能由 `signal_collector.py` 产出，`portfolio_manager.py` 对缺包或 producer/boundary 非法 fail-fast。原因：PM 不能重建、补造或改写结构化信号证据包。

（2）[Step5 未部署] 修正 `portfolio_manager.py` 中新增风险候选 rank 后未部署时的 Step6 合约与自检。原因：预算或 rank 未部署必须还原为无新增风险敞口，不能留下盘中触发执行权限。

（3）[旧生命周期 trace] 调整 PM Step6 代码，不再按旧 `primary_lifecycle_action_port` 反向要求资本部署。原因：最终候选已被 RiskGate 压回 wait/hold 时，旧 trace 只能作为历史诊断。

（4）[Step6 自检] 调整 `portfolio_manager.py`、`pm_contract_self_check.py` 与相关测试，废弃 Step2 vs Step6 比较式自检。原因：最终合约只按自身与 rank/非 rank 边界审，不读取旧 lifecycle compare 字段。

（5）[pre-backtest PM gate] `src/run/pre_backtest_test.py` 接入 PM/workflow 静态契约测试。原因：回测前固定验证 PM 三类合约矩阵、workflow 只读闸门、Signal Collector/PM 边界和中间态污染。

（6）[daily PG gate] `pg_system_invariants.py` 增加 PM/workflow 运行期只读审计。原因：真实 DB/artifact 中缺 final contract、PM 自检失败、中间态残留都必须 hard fail。

==========2026年07月08日==========

（1）[智能体出口 fixture] 更新 `src/tests/` 中回测前 fixture/gate 相关测试，覆盖 PM、Signal Collector、Trader、Accountant、Reviewer、Researcher 对外 artifact 边界。原因：内部推理可保留，但对外输出必须白名单化、可审计。

（2）[统一理解入口] 调整 `final_action_semantics.py`、`contracts.py`、`order_semantics.py`。原因：下游对 `final_action_contract` 的手数、方向、生命周期、执行权限和研究归因统一走共享解释入口，避免各智能体私写 PM 字段解释器。

（3）[PG 旁路边界] 调整 `pg_mechanism_effectiveness_audit.py`、`pg_contract_coverage_audit.py`、`pg_system_invariants.py`。原因：PG 只审协议连通、artifact 污染、PM 自检和条件合约执行链路，不复判 PM rank/reason/deployment。

（4）[分析师学习召回] `src/config/learning_policy_catalog.yaml` 将若干学习上下文召回上限从 3 调到 5。原因：给分析师更多近期 episode/no-trade/setup profile 摘要，不改变 action-value 权限。

（5）[Reviewer 预算漂移] `reviewer_phase4_review.py` 将 PM 计划预算偏离归入 `budget_drift_diagnostics`/warning/事实归因。原因：计划预算漂移不等于 PM 合约失效；账户硬上限、阶段断链、越权成交仍 hard fail。

（6）[Researcher 提示词] 调整 `src/llm/prompt.py` 中研究员对预算漂移的消费边界。原因：研究员只能把预算漂移作为未来研究输入，不能据此改写交易权限或绕过 final contract。

（7）[资金参数口径] 整理 `src/config/dev.yaml` 中账户保证金硬边界与 PM Step5 计划预算参数。原因：避免 PG/Reviewer 误审资金参数含义。

==========2026年07月09日==========

（1）[PM SCC 落盘] `portfolio_manager.py` 在最终 `signal_snapshot` 保存原始 `signal_collection_contract`。原因：Reviewer/Researcher/PG 的主证据必须是 `signal_snapshot.signal_collection_contract`，`signal_collection_contract_ref` 只能是摘要。

（2）[PG SCC 审计] `pg_mechanism_effectiveness_audit.py` 只认完整 SCC，检查存在性、producer/boundary 和 SCC 内 PM 越权字段。原因：不接受 ref 作为主证据，也不反推 PM 方向、手数、rank 或资金部署。

（3）[action-value 语义归一] `final_action_semantics.py` 新增/收口 canonical family、action_value_lane、learning_lane 和一致性校验。原因：`add_or_open` 等动作必须先映射到统一业务家族，再与学习偏向匹配。

（4）[action-value 写入与读取] 调整 `alpha_setup.py`、`sqlite_setup.py`、`sqlite_helper.py`、`research_memory_writers.py`、PM decision tools、Reviewer/Research helper，持久化并消费 `canonical_action_family`。原因：PM、Researcher、Reviewer、PG 不能靠裸 `action_name` 猜语义。

（5）[PG action-value 审计] `pg_system_invariants.py` 改为审 family/lane/preference 一致性。原因：`positive_candidate_open` 不再要求 `action_name == open`，而要求 `open_add_new_risk + open/add/scale/increase`；缺 family 或不一致 hard fail。

（6）[PM 自检口径] `final_action_semantics.py` 收住 PM final contract 自检，`portfolio_manager.py` 保留 Step6 已生成的生命周期学习 trace，相关 PM/PG 单测同步。原因：生命周期决策污染只应审 `decision_learning_rows`；`trigger_profile_learning_rows` 中的 execution/profile 学习不能被误判为 reduce_exit 决策污染。

（7）[PM Step6 final trace] `pm_contract_builder.py` 按 Step6 最终生命周期重新生成 `decision_learning_rows`，`portfolio_manager.py` 不再用 Step2 消费结果裁掉最终候选池，`final_action_semantics.py` 自检只审 Step6 final lifecycle trace，相关测试同步。原因：修复 2025-03-26 `open_rank_mixed_forbidden_learning_lanes:hold`，避免 Step2 hold trace 被误装成 open/rank 最终决策层证据。
