# AgentQuant 工作日志

本文件只记录 `.py`、`.yaml`、`.yml` 的行为或配置修改；纯文档同步、纯验证命令、分支/提交固定、数据清理不记。

==========2026年07月10日==========

（1）[PM action-value artifact] `portfolio_manager.py` 将 `canonical_action_value=False` 的 similar/fallback prior 从 `final_action_contract.learning_used.alpha_setup_action_values` 剔除，并写入 `learning_used.memory_retrieval.rejected_or_downgraded`。原因：PM 正式 action-value 主列表只能保存完整 canonical 学习证据，weak prior 只能作诊断检索材料。
（2）[PM self-check] `pm_contract_self_check.py` 增加 `alpha_setup_action_values` 纯净性检查，相关 PM 单测同步。原因：最终合约主列表出现缺 family、缺 preference、缺 lane、`canonical_action_value=False` 或 `incomplete_trace_not_for_pm_scoring` 时必须 hard fail。
（3）[PG observe action-value] `pg_system_invariants.py` 落实 `canonical_action_family=observe`、lane 为 `hold` 时空 `action_preference` 的合法语义，相关 system invariant 单测同步。原因：observe/watchlist 是观察事实和 hold 诊断线，不应被误报为缺交易动作偏向；本次未修改 PM artifact、PM self-check、SCC、action-value 写入端或交易业务逻辑。
（4）[字段矩阵命名] `pg_system_invariants.py`、`pg_pre_backtest_acceptance.py`、`pg_contract_coverage_audit.py`、`src/config/dev.yaml` 和相关测试改用 `matrix_field_semantics` 与 `docs/matrix_field_semantics.md`。原因：字段语义文档改为矩阵命名后，控制组闸门、契约覆盖和测试不能继续引用旧文件名。

(5) [matrix executable gate] Updated `pg_contract_coverage_audit.py`, `pg_pre_backtest_acceptance.py`, new `pg_pre_backtest_failure_fixtures.py`, and control CLI wrappers. Reason: matrix_chain_contract now blocks missing producer, landing, consumer, self-check, fixture, daily PG, real-path test, and mechanism-doc coverage before backtest.
(6) [daily PG boundary] Updated `pg_system_invariants.py` and tests to expose contract-only hard fail boundaries and diagnostics boundaries. Reason: daily PG remains a system contract audit and does not score PM rank, lots, direction, weak learning, legal observe diagnostics, loss days, or no-trade days.

==========2026年07月11日==========

（1）[PM 单状态主链] `portfolio_manager.py`、`graph/schema.py`、`graph/workflow.py` 将 Phase1 PM 接口改为 Step1–5 只传递同一个 `pm_state`；workflow 编排层不再接收未签约 `FuturesRecommendation`，Step6 才原子生成唯一 `final_action_contract` 和 `FuturesRecommendation`。原因：删除 candidate contract、builder inputs 和部署 snapshot 的中间物理输出，避免回测保存链把 PM 中间态当成外部事实。
（2）[PM Step5/Step6 分流] `pm_full_market_capital_deployment.py` 直接更新新增风险 PM 内存状态；原生 wait/hold/reduce/exit 跳过 Step5，新增风险执行全市场 rank、预算和 sizing，未获 rank 或预算时还原 `target_lots=current_lots`。反转状态先签 `exit`，反向新风险留待后续独立 rank 与授权。
（3）[PM 最终自检] `pm_contract_self_check.py` 与 `pm_contract_builder.py` 将 `position_sizing_result` 固定落入 `final_action_contract.evidence_used`，并校验其 current/target/delta 与最终合约一致；删除 PM 生命周期回溯诊断调用，只保留 Step6 最终生成检查和最终合约自身一致性检查。
（4）[SCC 来源字段] `signal_evidence_collection.py`、`signal_collection_data_unavailable.py`、PM 输入校验、PG 控制闸门和相关测试统一使用 `source_agent="signal_collector"`，不再使用 SCC `producer` 别名。
（5）[PM 回归测试] 新增 `test_pm_atomic_contract_flow.py`，重写 PM/workflow 回测前契约测试，并同步资金部署、字段边界、SCC、contract coverage、mechanism audit 与 Phase1 acceleration 测试。覆盖非新增风险直达 Step6、新增风险获准、无 rank 拒绝、预算拒绝、反转先退出、唯一推荐对象和最终自检边界。
（6）[PM Step4 学习收口] `portfolio_manager.py` 将配置学习 overlay、`retrieve_pm_memory`、profile、policy 和 action-value 检索统一移到第 3 步生命周期分流之后；`pm_ticker_side_selection.py` 删除学习参数，第 2 步只消费 SCC 方向事实，学习只能在第 4 步修正候选质量。
（7）[PM Step5 手数测算] `portfolio_manager.py` 删除第 1–4 步 `build_position_sizing_result` 调用；`pm_full_market_capital_deployment.py` 在全市场 rank 和预算结论形成后生成最终 sizing，预算或资格拒绝时同步还原目标手数、仓位比例、保证金与 sizing 事实；非新增风险由 Step6 形成无 rank 手数摘要。
（8）[PM 物理输出边界] `portfolio_manager.py` 与 PM 专属 decision 工具删除全部物理 logger 调用，Step1–5 不再构建 `signal_snapshot`；Step6 只从单一 PM 内存状态原子生成 `FuturesRecommendation`、`final_action_contract` 和两项最终检查，`workflow.py` 仅在 PM 返回或异常后记录安全日志并执行保存。
（9）[PM 最终自检接口] `pm_contract_self_check.py` 将 `check_final_action_contract` 收窄为只接收最终合约，删除 PM artifact 和 snapshot 比较入口；相关 PM、workflow、Phase-flow、contract coverage 测试改为验证 Step4 学习、Step5 sizing、Step6 唯一 snapshot 和最终合约自身一致性。
（10）[PM 未签约 recommendation 诊断清理] `portfolio_manager.py` 删除 Step6 前写入 `plan_snapshot["recommendation_position_consistency"]` 的无效一致性计算；最终 `final_action_contract.consistency` 和 Step6 最终合约自身检查保持不变，并增加源码边界回归测试。
（11）[PM 排名预算顺序] `pm_full_market_capital_deployment.py` 将 `alpha_scale_entry`、`real_budget_entry`、`exploration_probe` 资金层级真正置于 Step5 排序首键，并以标准化 `ticker` 作为完全同分时的固定末级决胜键；预算游标继续严格按生成的 `opportunity_rank` 顺序消费。增加回归测试证明已验证候选优先占用预算且输入顺序不再改变同分 rank。
（12）[PM 学习到 rank 边界] `pm_signal_fusion.py` 与 `pm_full_market_capital_deployment.py` 切断 execution/profile 学习对候选分数、资金层级解锁和 `rank_score` 的直接或间接影响；execution/profile 学习仍保留为执行画像与诊断事实。canonical open/add action-value、产品/setup/trigger 历史及已结算入场质量继续按配置影响 Step5 排名。
（13）[PM rank 评分与配置对齐] `pm_signal_fusion.py` 为既有 `rank_candidate_input_components.cold_start_evidence_quality` 提供不含学习项的当前证据分，`pm_full_market_capital_deployment.py` 只从该分项计算冷启动积分，避免 action-value、历史和冲突项重复计分；rank trace 删除 execution-profile 混入。`rank_score_policy.yaml` 删除已停用的 `execution_profile_learning_weight` 和过期未消费的固定 `tuning_window`，保留全部有效权重不变。
（14）[PM rank 字段与调参链统一] `rank_score_policy.yaml`、`pm_signal_fusion.py`、`pm_ticker_side_selection.py`、`pm_full_market_capital_deployment.py` 及相关测试将七个 rank 参数组与 `rank_score_components` 固定同名，组内参数与 Python 消费字段同名；`rank_candidate_input_components` 收束为 `rank_score_input_components`，删除预生成 rank 别名、学习分项后缀别名、四个 alpha-scale 同义字段和私有负向字符串推断，并由既有资金利用诊断唯一生成 `alpha_scale_eligible`。原因：保证每个 YAML 调参项真实进入唯一 Step5 rank 计算，最终 trace 使用统一字段且不改变现有积分值、预算比例和 canonical 动作语义。
（15）[PM rank 不改变交易属性] `pm_full_market_capital_deployment.py` 将 `capital_layer` 的唯一来源改为 rank 前已形成的 `final_entry_authority.authority_type` 和既有 alpha-release 资格，删除按 scorecard 状态或分数推导 probe/real 层级的路径；新增回归测试证明高分 `exploration_probe` 仍保持小仓试探属性，正常 `real_budget_entry` 天然先于 probe，rank 不修改 `authority_type` 和 `max_allowed_margin_ratio`。原因：rank 只决定既定交易属性候选的预算竞争顺序，不能把小仓试探升级为正常或放大资金。
（16）[PM 仅开仓排名] `final_action_semantics.py`、`pm_lifecycle_action_port.py`、`pm_full_market_capital_deployment.py` 和相关测试将全市场 `opportunity_rank` 的唯一触发收口为 `current_lots=0` 且 `target_lots!=0`；`add/scale/hold/reduce/exit/reverse` 当前合约不再进入 Step5 排名，反转先退出、后续从空仓新开时才重新排名；最终合约自检拒绝非开仓合约残留 rank，但保留真实开仓候选预算拒绝后的 rank 事实。原因：rank 只比较当日开仓机会，不能给持仓管理动作生成排名或改变交易属性。
（17）[配置参数一一映射] 全量审计 `src/config/*.yaml` 后，删除无生产消费函数的 `evidence_fusion_policy_catalog.yaml`，并清理 analyst prior、数据因子、主配置、手续费、Finoview、学习、portfolio、商品 profile 与 rank catalog 中失效的说明型或历史残留参数；`config_normalizer.py` 同步删除无行为的 catalog 展开和 role 注入，PM 默认表删除同名失效键。新增 `test_config_parameter_mapping.py` 并接入回测前静态测试，要求每个保留 YAML 参数都登记到 `matrix_field_semantics.md`、对应真实 Python 消费函数，固定叶字段必须在生产代码逐名读取，动态 ticker/sector/factor/template 参数必须归入已登记参数族。原因：保证配置调参真实改变对应代码行为，防止经过多轮修改后出现无映射参数、装饰性配置和字段语义漂移。
（18）[PM 冗余工具清理] `decision` 与 `common` 工具删除生产零引用的旧 PM snapshot artifact、生命周期回溯诊断、旧开仓动作提示、旧 Auditor 学习校准、未使用的自检异常包装、学习 prompt/trace 和相关私有辅助函数；同步删除只验证旧机制的测试，并新增零引用旧函数禁止回归测试。原因：新 PM 只保留单一内存状态到 Step6 唯一合约的真实调用链，防止废弃接口重新引入中间物理输出、跨步骤比较式自检和第二套动作语义。
（19）[新闻分析师身份统一] 全链路只接受 `commodity_news`，删除旧新闻分析师别名常量、prompt 别名、配置静默转换、旧 snapshot/DB 读取回退及学习、PM、复盘兼容分支；新增全仓身份唯一性测试。原因：防止同一智能体出现第二名称，保证分析师输出、SCC、PM、学习和归因只使用唯一字段语义。
