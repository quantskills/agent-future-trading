# AgentQuant 工作日志

本文件只记录 `.py`、`.yaml`、`.yml` 的行为或运行配置修改，按日期正序整理。每条仅说明最终有效的修改及原因；纯讨论、方案、验证命令、测试数量、分支提交、数据或缓存清理、文件改名删除和纯文档同步不记录。

==========2026年07月10日==========

（1）[PM action-value 纯净性] `portfolio_manager.py` 将非 canonical 的相似或弱先验从 `final_action_contract.learning_used.alpha_setup_action_values` 移入 `memory_retrieval.rejected_or_downgraded`，`pm_contract_self_check.py` 同步拒绝缺 family、preference、lane 或标记为不参与 PM 评分的记录。原因：正式 action-value 主列表只能保存完整、可消费的学习证据。

（2）[PG observe 与每日边界] `pg_system_invariants.py` 将 observe/watchlist 的 hold lane 和空 `action_preference` 识别为合法观察事实，并将每日 PG 限定为契约与物理事实检查。原因：观察、合法无交易和弱学习诊断不能被误判为交易动作缺失或策略错误。

（3）[矩阵与 coverage 门禁] `pg_pre_backtest_acceptance.py`、`pg_contract_coverage_audit.py`、控制入口和 `dev.yaml` 统一使用字段语义矩阵，并检查生产者、落点、消费者、角色校验和真实路径覆盖。原因：回测前门禁必须依赖当前可执行契约，不能依赖旧文件名或字符串命中。

==========2026年07月11日==========

（1）[PM 单状态六步链] `portfolio_manager.py`、`schema.py`、`workflow.py` 将 Step1-5 收口为单一内存 `pm_state`，Step6 才原子生成唯一 `FuturesRecommendation` 与 `final_action_contract`；Step1-5 不再生成 snapshot、草稿、DB 记录或物理日志。原因：防止 PM 中间状态成为外部交易事实或泄露内部过程。

（2）[Step6 合约与自检] `pm_contract_builder.py`、`pm_contract_self_check.py` 将最终 sizing 固定写入 `evidence_used.position_sizing_result`，自检只接收最终 FAC 并核对动作、手数、rank、学习和最终合约自身一致性。原因：PM 自检只验证最终输出，不回溯或比较已退出的中间对象。

（3）[Step4 学习与 Step5 资金部署] `portfolio_manager.py` 将学习检索放到生命周期分流后的 Step4；`pm_full_market_capital_deployment.py` 在 Step5 完成全市场排序、预算消费和 sizing，拒绝部署时恢复原持仓目标。原因：方向选择、学习校对、资金排名和手数测算必须按六步职责顺序运行。

（4）[rank 语义与配置同源] `pm_signal_fusion.py`、`pm_ticker_side_selection.py`、`pm_full_market_capital_deployment.py` 与 `rank_score_policy.yaml` 统一 rank 输入、组件和参数名；execution/profile 学习不直接进入 rank，rank 不改变 `authority_type`、资金层或保证金权限。原因：rank 只决定既有候选的资金优先级和预算顺序。

（5）[配置真实消费] `config_normalizer.py`、配置 catalog 和 `test_config_parameter_mapping.py` 清理无生产消费者的参数，并要求保留参数绑定真实 Python 消费函数。原因：避免装饰性配置、失效参数和同义参数造成调参假象。

（6）[分析师身份唯一] 分析师、PM、学习和复盘链删除旧新闻分析师别名，只接受 `commodity_news`。原因：同一智能体必须使用唯一身份完成持久化、SCC 汇总和学习追溯。

==========2026年07月12日==========

（1）[AEC 到 SCC 唯一链] `analyst_output_finalization.py`、三个分析师、`signal_evidence_collection.py`、`signal_collector.py`、`workflow.py` 与 `schema.py` 统一生成、校验和保存三份正式 AEC，再由 Collector 只输出一份 SCC；SCC 只保存分析师身份、真实 `signal_record_id` 和唯一 AEC。原因：消除重复证据、伪造来源、第二套 SCC 和 Collector 交易越权。

（2）[非每日数据与分析师收口] 基本面和新闻无当日新增时只影响对应分析师，允许使用截止时间前最新有效数据并记录 freshness；必需事实不可用时三个分析师仍通过正式入口生成合法中性 AEC。原因：非每日基本面或新闻不能导致全局失败，也不能调用 LLM 补造事实。

（3）[分析师学习边界] 三个分析师共用顶层 LLM 配置和正式结构化输出；技术面保留有界指标参数校准，提示词校对和学习 overlay 不能生成方向、手数、资金或交易权限。原因：学习只改善分析证据和参数，不绕过 SCC 与 PM。

（4）[PM 只消费 SCC] `portfolio_manager.py`、`pm_signal_fusion.py`、`pm_ticker_side_selection.py` 与 `pm_invalidation_policy.py` 只从已校验 SCC 重建证据视图，不再读取原始信号旁路、旧研究合约或重复市场确认。原因：PM 与 Collector 必须对方向、触发、质量和失效边界使用同一套证据语义。

（5）[所有新增风险统一排名] `final_action_semantics.py`、`pm_lifecycle_action_port.py`、`pm_full_market_capital_deployment.py`、`portfolio_manager.py` 与 `pm_contract_self_check.py` 将 open 和实际增加风险的 add/scale 纳入同一 Step5 rank、预算和 sizing；不增加风险的 hold/reduce/exit 与反转退出腿跳过 Step5。原因：新增资金请求必须共同竞争预算，持仓收缩动作不能被排名压死。

（6）[最终合约事实分层] `portfolio_manager.py`、`pm_contract_builder.py`、`pm_contract_self_check.py`、`contracts.py` 与相关语义工具将 rank、sizing、学习、执行字段和 SCC 分别落入唯一登记位置。原因：Step6 只能从最终方向一致证据生成一份 FAC，禁止重复事实和分析师执行角色外泄。

（7）[Auditor 与下游职责] `auditor.py`、`futures_audit.py`、`trader.py`、Reviewer、Researcher 和 evaluation 读取端删除方向、rank、学习和旧分析师 snapshot 的重复解释。原因：Auditor 只做独立硬门控，Trader 只执行 FAC，Reviewer 只复盘事实，Researcher 只研究已结算链路。

（8）[Researcher 确定性校验] `researcher_learning.py`、`research_learning.py`、`research_memory_writers.py` 与研究辅助工具在 LLM 前校验来源链，在 LLM 后过滤无证据、越权、前视和非法动作结果，只保存验证后的结构化学习。原因：研究成果只能用于未来决策，不能改写当日合约、执行和结算。

==========2026年07月13日==========

（1）[PG 单一报告与职责] `protocol_governor.py`、`pg_schemas.py`、回测前和每日入口统一使用 `contract_version/source_agent/status/checks[]` 及检查项固定字段，删除能力卡、任务生命周期和私有诊断容器。原因：PG 不得自创第二套字段或泄露智能体内部状态。

（2）[回测前十项门禁] `pg_pre_backtest_acceptance.py`、`pg_preflight.py`、`pg_db_schema_contract.py` 与 `pre_backtest_test.py` 建立环境入口、配置、字段职责、数据、时间、临时库、无 LLM 预演、业务路径、编排边界和判定边界十项检查。原因：回测前检测只判断系统是否具备启动条件，不运行策略、不调用 LLM、不评价收益。

（3）[每日七项只读事实检查] `pg_system_invariants.py` 与 `backtest_daily_test.py` 只读检查阶段、物理落点、交易来源、Auditor 放行、执行成交、结算守恒和学习日期边界，并允许合法无交易、未触发和无学习。原因：每日 PG 只识别非策略断点，不复查 PM、Auditor、Reviewer 或学习质量。

（4）[PG 真实 coverage 与编排] PG 工具和测试删除废弃能力、历史故障 fixture 与内部机制复查，coverage 只验证当前 producer -> landing -> consumer -> role check -> real path；每日检测固定在 Phase1-4 和 Researcher 后运行一次。原因：旁路审计必须服务当前主链，不维护废弃机制或重复执行。

==========2026年07月14日==========

（1）[数据不可用正式主链] 三个分析师、`analyst_output_finalization.py`、`workflow.py`、`signal_collector.py` 与共享校验器使正常和数据不可用状态共用同一 AEC 保存、真实 ID 和 SCC 聚合链。原因：Collector 不再伪造信号，基本面或新闻缺失不再扩大为全局失败。

（2）[具体合约与 Auditor 输入] `router.py`、`portfolio_manager.py`、`workflow.py`、`auditor.py` 与 `portfolio_policy_catalog.yaml` 统一具体合约事实、账户、持仓、SCC 数据质量和硬风控输入；新增风险缺少合法具体合约时不得增加手数。原因：Auditor 必须审计真实 FAC 和硬风险事实，不能重做 PM 决策。

（3）[Trader 审计保真] `futures_audit.py`、`trader.py` 与执行工具以原始完整 `audit_payload` 为基底追加合约审计、执行翻译、执行结果和 Phase2 事实。原因：Trader 只能追加成交或未成交事实，不能用摘要覆盖 Auditor 原结论。

（4）[智能体信息隔离] 分析师、Workflow、LLM/PandaAI 适配、数据库、日志和研究写入口只传递或持久化已校验契约，拒绝 prompt、原始 response、内部参数、隐藏上下文、本机路径和未验证工具结果；失败只暴露稳定错误码。原因：智能体内部工作不得跨角色、落库或进入日志。

（5）[Researcher 正式 ID 追溯] `research_learning.py` 与 `research_memory_writers.py` 在 Phase4 和结算后按 AEC、SCC、FAC、Auditor、execution_result、transaction 和 settlement 的正式 ID/日期关系生成学习，允许零交易和零学习。原因：学习必须来自已完成事实链，但不要求每笔交易形成或消费学习。

（6）[共享字段与配置收口] `signal_evidence_collection.py`、`evidence_fusion_semantics.py`、`analyst_data_usage.py`、`schema.py`、数据库接口和 `dev.yaml` 删除字段别名、默认事实、原始错误和零值补造路径。原因：AEC、SCC、Auditor、execution_result 和 action-value 只能使用字段矩阵与共享校验的一套含义。

==========2026年07月15日==========

（1）[回测前数据与时间实证] `pg_pre_backtest_acceptance.py` 通过正式 Router 和交易日机制检查指定窗口的日线、结算价、主力/具体合约、乘数、保证金率、分钟接口、Finoview/新闻读取和时间消费者接线。原因：门禁必须验证真实运行条件，同时允许基本面和新闻非每日更新。

（2）[契约、schema 与配置实证] `pg_pre_backtest_acceptance.py`、`pg_db_schema_contract.py` 与 `pg_contract_coverage_audit.py` 调用共享校验器和正式 `sqlite_setup`，并将配置项绑定真实 Python 消费者。原因：PG 不维护私有字段表、假 schema、字符串 coverage 或废弃函数解释。

（3）[同库无 LLM 全链路预演] `pg_full_chain_dry_run.py` 在同一隔离临时库中使用 canonical 确定性输入调用真实生产函数和保存接口，贯通 AEC -> SCC -> FAC -> Auditor -> Trader -> Accountant -> Reviewer -> Researcher -> 次日学习读取。原因：回测前门禁需要证明正式链路可装配运行，但不得调用 LLM 或写正式业务库。

（4）[每日来源与交易授权] `pg_system_invariants.py` 共享校验 SCC 并核对三个真实信号 ID；每笔 transaction 必须按 strategy、rollover 或 forced_risk 绑定对应授权，strategy 交易必须具有唯一 FAC 和 Auditor 放行。原因：每日 PG 只核对已落地来源和执行权限，不复做策略或审计判断。

（5）[每日执行、结算与学习] `pg_system_invariants.py` 按 FAC 条件核对盘中结果、成交唯一入账、持仓快照、手续费、PnL、保证金、现金和权益，并只检查实际生成学习的 Phase4、结算、日期、ID 和 canonical 动作。原因：实际成交不必等于 PM 预算，学习允许为空且不评价质量。

==========2026年07月16日==========

（1）[普通 Neutral 与机会状态] `analyst_output_finalization.py`、三个分析师、质量工具和提示词将普通 Neutral 收口为 `no_opportunity`；只有方向、具体触发、canonical 失效边界和未触发状态完整时才形成 `watch_for_trigger`，Neutral 不得升级为 probe/tradeable。原因：删除 `wait_for_trigger` 和虚构中性触发，避免普通中性结果在正式校验中崩溃。

（2）[AEC 触发与失效同源] `signal_evidence_collection.py` 要求 watch/candidate 具有具体非占位 `entry_trigger`，只接受 `invalidation_condition`、合法 `invalidation_level` 或正数 `atr_stop_distance`；PM 与学习读取端删除 `would_change_view_if`、`exit_hint` 和旧 metadata 别名。原因：所有生产者和消费者必须用同一套机会、触发和失效语义。

（3）[Workflow 安全失败与普通中性预演] `workflow.py` 在任一分析师失败时于持久化前终止，只暴露稳定契约错误；`pg_full_chain_dry_run.py` 与回测前门禁加入数据可用普通 Neutral 的真实 finalization 和同库链路。原因：防止部分 AEC/SCC/FAC 落盘，并让通用就绪检测覆盖正常中性生产路径。

（4）[逻辑交易日与数据可见边界] Researcher、`analyst_finoview_factors.py`、`router.py` 与 `pandaai/api.py` 区分 Prev(T) 参考组合和逻辑 T 事实，统一 Finoview release lag、新闻截止、日线 `<T` 与夜盘分钟线 `trading_date=T`。原因：T 日研究成果只供 Next(T) 决策消费，所有策略与执行数据不得前视或使用自然日猜测。

（5）[合法 watch 条件交易链] `portfolio_manager.py` 删除排名前自由文本否决，合法 watch 可按既有 rank、预算和 sizing 竞争；获选后形成非零条件 FAC，Trader 使用 15 分钟确认和下一根合法 1 分钟执行。原因：等待触发是受控交易候选，不得在 Step5 前被清零，也不等于自动成交。

（6）[直接执行、保证金与风险收缩] `portfolio_manager.py`、`trader_intraday_execution.py`、`final_action_semantics.py`、`auditor.py`、`trader.py` 与 PM 工具区分已触发直执行和条件 watch，共用增量保证金投影，并将 `risk_reduction_candidate` 隔离在已有持仓的 hold/reduce/exit 生命周期。原因：已触发候选不应被二次触发压死，add/scale 不重复计算已有保证金，风险收缩证据不能创建新风险。

（7）[Step2 方向与 Step5 候选准入] `pm_ticker_side_selection.py` 只按 SCC 方向事实确定 `preferred_side`；`pm_signal_fusion.py`、`portfolio_manager.py` 与共享触发谓词分离 `missing_evidence`、`confirmation_requirements` 和 hard data failure，允许单来源已触发候选携带真实低分进入 Step5。原因：方向选择不等于资金排名，证据不足或非每日数据不能制造硬阻断，一个 no-opportunity 也不能否决其他合法候选。

（8）[交易路径回归] `test_trade_path_incremental_repairs.py` 覆盖三类机会状态、直执行/条件执行、add/scale、reduce/exit、风险收缩、方向、数据质量和单来源候选。原因：这些主线行为必须由确定性生产函数测试保护，不能交给 PG 复判。

==========2026年07月17日==========

（1）[LLM 路由配置] `dev.yaml` 与 `pg_preflight.py` 使用 Provider 的唯一 URL/密钥环境变量定义，并将 GPT、Claude、DeepSeek v4 Pro 配置为可整块切换的顶层 LLM；三个分析师和 Researcher 继续共同消费该配置。原因：避免重复路由事实和智能体私有模型路径，同时保持密钥和内部响应不进入业务契约。

（2）[PM 到 Researcher 生命周期契约] `research_memory_writers.py` 删除已退出 `holding` 对象读取，只从 `final_action_contract.learning_used.pm_lifecycle_learning_impact_delta` 消费正式 hold/reduce/exit、期限不匹配和亏损再验证结果。原因：完成 PM 正式接口迁移，避免合法减仓结果因旧变量引用中断 Researcher。

（3）[Researcher 原子提交] `artifact_store.py` 与 `researcher_learning.py` 将研究 SQL、完成事件、外置 payload、template prior 和学习快照纳入同一事务，失败时同时回滚数据库和本次文件。原因：防止数据库回滚后留下无引用 artifact，并保护运行前已有合法文件。

（4）[生命周期通用回归] `test_researcher_lifecycle_contract.py` 与回测前 `supported_business_paths` 使用 canonical FAC 和真实 Researcher 消费函数覆盖 hold/reduce/exit、期限不匹配、亏损再验证和原子失败。原因：通用机制验收不能写成具体日期、品种或历史故障分支。

（5）[包入口去重] 多个包的 `__init__.py` 收口为最小包声明，正式实现只保留在具名模块。原因：避免包导入重复实现、隐式副作用和第二代码来源。

（6）[PandaAI 稳定调用与精确缓存] `pandaai/api.py` 将 429、502、503、504、正式 SDK 限流码及明确 timeout/connection 异常纳入同一节流和持续退避循环；401/403、鉴权、权限、参数、接口和数据错误立即失败。日线缓存按 symbol、日期范围和记录事实精确校验，无效、空或错配缓存不得命中。原因：瞬时服务波动只暂停当前真实请求，不能终止整段回测，也不能用默认或近似数据冒充成功。

（7）[回测前代表性数据检查] `pg_pre_backtest_acceptance.py` 使用正式 `get_previous_trading_day` 检查 Prev(start_date) 与窗口日线、主力/具体合约事实，并只对一个正式代表合约各调用一次 15m 和 1m 验证 Trader 接口能力。原因：回测前检测不遍历下载全区间分钟数据，也不使用自然日猜测或建立第二套重试器。

（8）[模拟盘分钟刷新] `pandaai/api.py` 在带 `cutoff_datetime` 的持续盯盘读取中绕过旧分钟缓存，只有非空合法结果才替换对应缓存；回测的无 cutoff 一次性读取继续复用现有缓存。原因：模拟盘每轮必须看到新增行情，但空响应或异常不能回放旧行情冒充最新事实。

（9）[回测前检测与回测编排分离] `backtest.py` 不再自动调用回测前门禁、窗口评估和画图，只逐日执行 Proposal -> Order -> Settlement -> Validate -> Researcher，并在每天 Researcher 后自动运行一次 `backtest_daily_test.py`；`pg_pre_backtest_acceptance.py` 从正式 Prev(start_date) 检查首日依赖。原因：事前 readiness 由操作者独立运行，自动化回测只保留主链、断点续跑和每日只读事实门禁。

==========2026年07月18日==========

（1）[PM Step6 执行证据同源] `portfolio_manager.py` 从最终方向对应的合法 AEC 中确定性选择唯一执行证据，并由同一来源生成 `entry_trigger`、`invalidation`、`execution_profile`、`trigger_source`、失效价和 ATR；fundamental 来源统一登记为 `fundamental_entry_trigger`，无法识别 profile 时不再默认 `breakout`。原因：防止 Step6 遗漏基本面触发、借用反方向证据或跨分析师拼接最终合约事实。

（2）[PM 最终执行契约自检] `pm_contract_self_check.py` 增加 canonical execution profile、trigger source 自洽性及新增风险触发和失效边界完整性检查。原因：Step6 必须在签署唯一 FAC 前拒绝执行字段缺失或语义不一致的合约，不能把不完整事实交给 Auditor 和 Trader。

（3）[Step6 来源对齐回归] `test_trade_path_incremental_repairs.py` 和 `test_pm_atomic_contract_flow.py` 增加 fundamental 独立 watch/已触发、technical/news 原路径、反方向隔离、多来源不混拼、未知 profile 拒绝、学习 overlay 权限边界及 SCC→Step5→条件 FAC 行为覆盖。原因：执行证据来源一致性属于 PM 开发回归，不进入 PG 生产检查。

（4）[Step6 最终生命周期执行摘要] `portfolio_manager.py` 在最终手数不再增加风险时按 wait/hold/reduce/exit 重建 `hold/exit_immediate` 执行字段并清除已失效的条件确认标记；`test_phase_flow_regression.py` 将残缺 AEC 和空执行字段夹具迁移为正式 SCC 与真实执行字段生成函数。原因：Step5 拒绝或其他门控收回目标手数后，Step6 必须按最终生命周期签约，开发回归也不能绕过最终合约不变量。

（5）[PM 执行字段状态矩阵] `test_pm_state_transition_matrix.py` 将 hold、wait、open 和 scale 合约夹具补齐 canonical profile、trigger source、具体触发和失效边界。原因：PM 状态迁移测试必须使用当前可签署 FAC，不能依赖自检曾经放行空执行字段的旧前提。

（6）[分析师机会状态原子收口] `analyst_quality.py` 在数据质量、setup 完整性和机会质量降级全部完成后，统一写入最终 `opportunity_state`、`trigger_valid` 和 `current_trigger_confirmed`；新增风险的 no-opportunity/watch 固定为 false/false，probe/tradeable 固定为 true/true，风险收缩状态保持独立语义。原因：防止 fundamental 已触发证据被质量规则降为 watch 后仍携带已触发布尔值，形成共享 AEC 契约矛盾。

（7）[LLM 瞬时故障持续重试] `inference.py` 将 429、全部 5xx、SDK timeout 和连接中断识别为瞬时错误，并在当前调用中按既有上限指数退避持续重试；`dev.yaml` 将当前 GPT 的 `server_error` 切换为 `retry_with_backoff`，结构化输出、未知错误和非瞬时 4xx 继续有限或立即失败。原因：外部网关短暂波动不能在三次外层尝试后终止 Phase1，同时不得改变模型、接口、并发、SDK 重试和业务工作流。

（8）[执行职责与盘中触发唯一语义] `prompt.py`、`analyst_quality.py`、`execution_trigger_semantics.py`、`signal_evidence_collection.py`、`portfolio_manager.py`、PM 自检和 Trader 契约入口统一执行 profile、方向、canonical `entry_trigger` 与 `trigger_source`：普通盘中入场只由 technical 提供，事件即时入场只由 commodity_news 提供，fundamental 固定为方向上下文；PM/Trader 删除自由文本 profile 推断和默认 `breakout`，无合法执行证据的品种只能形成零新增风险合约。原因：执行职责必须先于置信度选择，正式 FAC 的 profile、触发和来源必须来自同一已校验证据，并与 Trader 既有 15 分钟确认及 1 分钟执行算法完全一致；本条取代此前 `fundamental_entry_trigger` 独立执行来源。

（9）[Phase1 数据与 artifact 原子落点] `interface.py`、`sqlite_helper.py`、`schema.py`、`workflow.py` 和分析师报告写入将三份 AnalystSignal、SCC 驱动的 PM/FAC、Auditor 结果及本次新 artifact 纳入同一 SQLite 写入范围；任一真实契约异常同时回滚数据库和本次文件，合法无执行证据则只取消对应品种新增风险。原因：多品种 Phase1 不能因后续品种失败留下部分信号、推荐或无数据库引用 artifact，也不能把单品种合法无候选扩大为全市场失败。
