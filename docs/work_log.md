# AgentQuant 工作日志

本文件是基于当前系统状态重整后的短版开发索引。保留按天划分的结构，只记录仍能解释现有代码、配置、字段、智能体边界和回测前验收的修改。已被后续重构覆盖的中间补丁、旧字段口径、旧工具名、旧运行入口和纯讨论内容不再保留。

字段语义以 `docs/unified_field_semantics.md` 为准；PM 内部链路以 `docs/mechanism_pm.md` 为准；workflow 编排边界以 `docs/mechanism_workflow.md` 为准；智能体权限、事实入口和 artifact 边界以 `docs/mechanism_multiagents.md` 为准；数据载体与 DB schema 口径以 `docs/mechanism_data_model.md` 为准。

每条只保留：修改了什么、为什么改。

==========2026年06月08日==========

（1）建立配置展开和资金目录基础。修改：`config_normalizer`、`dev.yaml`、portfolio/learning catalog 和配置测试。原因：主配置只承担运行入口职责，策略参数、资金边界和学习策略由 catalog 管理。

（2）收束 PM 最终推荐出口。修改：PM、Trader、reason-effects 和回归测试。原因：防止“不交易、无触发、只观察”等语义被最小手数、probe 或自然语言推成真实开仓。

==========2026年06月09日==========

（1）贯通分析师结构化证据进入 PM。修改：分析师证据质量工具、公共合约工具、PM 消费路径和测试。原因：分析师输出从方向文本升级为 PM 可读的 `action_evidence_contract`。

（2）补齐学习闭环进入 PM 候选和交易出口的测试基础。修改：PM 学习消费和 scorecard 测试。原因：证明历史学习能被 PM 看见并影响候选判断，但不能绕过最终合约。

==========2026年06月10日==========

（1）收束软门控和持仓生命周期保护。修改：PM 生命周期、reason-effect、资金/风控配置和测试。原因：软风险只降级、缩手数或要求确认，硬风险才阻断交易。

（2）打通 action-value 服务 PM 评分和执行 profile 的早期路径。修改：研究写入、DB 读取、PM 学习消费、执行 profile 和测试。原因：open/hold/exit/execution 分动作学习可进入 PM，但不能成为 Trader 权限。

==========2026年06月11日==========

（1）收紧研究学习时间边界。修改：DB schema/helper、研究写入、PM 历史读取和测试。原因：学习记录必须早于决策日，防止 Phase4、同日或未来学习污染当日 Phase1。

（2）收紧 alpha setup/action-value 的真实放大资格。修改：`alpha_setup`、DB 读取、PM 学习使用和测试。原因：区分真实同作用域 action-value、弱先验、相似样本和观察样本。

==========2026年06月12日==========

（1）把研究信息落到分析师证据校准。修改：`analyst_learning_calibration`、三类分析师、技术参数校准和测试。原因：分析师可消费本专业校准类研究，但不能生成交易动作。

（2）增强 PandaAI 数据调用稳定性。修改：PandaAI API adapter 和测试。原因：处理 auth、token、合约代码匹配和不可用字段重试，减少数据接口噪音。

（3）把 action-value 升级为可审计动作偏好。修改：`alpha_setup`、PM 学习使用、reason-effect 和研究测试。原因：固定 open/hold/exit/execution 分账，避免跨动作误用。

==========2026年06月13日==========

（1）建立协议管理员和控制组侧车。修改：`protocol_governor`、control tools、控制运行入口和测试。原因：控制组只治理边界、工具权限、artifact、preflight 和成本预算，不产生交易动作。

（2）对齐能力卡、工具权限和提示词边界。修改：`agent_cards`、`tool_access_policy`、`dev.yaml`、`prompt.py` 和测试。原因：配置、提示词和控制审计必须表达同一套智能体权限。

==========2026年06月14日==========

（1）把 action-value reward 收束到完整 episode 优先口径。修改：`alpha_setup`、研究学习、preflight 和测试。原因：避免只看单日 PnL 导致 open/hold/exit/execution 归因错位。

（2）新增交易流水审计镜像和运行期系统不变量审计。修改：Trader、`futures_audit`、`system_invariants`、`backtest.py` 和测试。原因：每笔成交可回溯 PM 合约，非策略 bug 当日 fail-fast。

（3）整理运行入口目录。修改：control/research 运行入口和测试。原因：区分主回测、控制审计和研究入口，避免脚本职责混乱。

==========2026年06月15日==========

（1）固定回测前验收入口。修改：`pre_backtest_acceptance`、protocol preflight、`backtest.py` 和测试。原因：把环境、配置、时间边界、智能体边界、结构化 IO、唯一合约和资金边界变成回测前闸门。

（2）接入 LLM 鉴权与 provider 配置一致性检查。修改：LLM provider/inference、`dev.yaml`、preflight 和 acceptance 测试。原因：无效 token 或 provider 漂移不能到 Phase1 才暴露。

（3）把 action-value 放大资格接入系统不变量。修改：`system_invariants`、action-value 写入和测试。原因：真实 action-value 才能支持放大，弱先验不得伪装成真实动作偏好。

==========2026年06月16日==========

（1）收束 `final_action_contract` 为唯一策略交易合约。修改：PM、Trader、执行工具、公共审计工具和测试。原因：策略成交只能来自审计通过的最终合约，score/rank 和自然语言说明不能成为第二交易权限。

（2）统一 action-value canonical 字段。修改：DB schema/helper、`alpha_setup`、研究写入、系统不变量和测试。原因：固定 `action_preference/reward_source/evidence_scope/action_value_lane/consumer_scope` 等机器消费语义。

（3）明确研究成果消费边界。修改：研究工具、分析师校准、PM 学习读取、Trader 执行字段和配置/提示词。原因：分析师消费校准类研究，PM 消费交易决策类研究，Trader 不读研究库下单。

==========2026年06月17日==========

（1）新增分析师机会状态与证据契约。修改：schema、分析质量工具、提示词、PM 消费路径和测试。原因：用 `opportunity_state` 和 `action_evidence_contract` 固定触发、失效、证据质量和学习影响。

（2）切断旧草稿计划和旧推荐字段的下游兜底。修改：PM、Trader、Reviewer/Researcher 事实提取、系统不变量和测试。原因：下游必须读 `final_action_contract.current_lots/target_lots/lots_delta/final_action`。

（3）完成运行时字段语义漂移收口。修改：字段语义表、字段迁移测试、运行时生产/消费路径和配置/提示词注释。原因：旧字段只允许在迁移代码、负向测试和历史归档中出现。

==========2026年06月18日==========

（1）新增 PM 释放阻塞诊断和学习使用诊断。修改：PM、研究反馈、策略归因和测试。原因：资金释放、学习使用和未落仓原因要可审计，但不能变成第二交易权限。

（2）切断复盘/研究对旧推荐字段的仓位兜底。修改：复盘归因、研究 action-value 写入、SQL similar prior、归因报告和测试。原因：复盘与研究必须从最终合约和真实执行/结算事实推导动作分账。

==========2026年06月19日==========

（1）统一分析师触发字段和文本触发说明。修改：分析师证据归一化、主配置注释、字段审计和测试。原因：`entry_trigger`、`trigger_valid`、`current_trigger_confirmed`、`setup_quality_ok` 语义必须一致。

（2）把统一字段语义检查接入回测前和每日审计。修改：统一字段审计、`pre_backtest_acceptance`、`system_invariants`、配置/提示词/测试。原因：防止旧字段或旧注释重新驱动生产路径。

==========2026年06月20日==========

（1）拆开 `setup_quality_ok` 与当前触发语义。修改：分析师证据、PM 条件机会分流、Trader 盘中触发、系统不变量和测试。原因：`setup_quality_ok=true` 只表示值得关注，当前触发必须由 `trigger_valid/current_trigger_confirmed` 表示。

（2）接通非策略运营单的执行与审计隔离。修改：换月、强平/强减、保证金风险触发、交易员执行、归因和审计。原因：`rollover/forced_risk` 独立执行和核算，不污染策略合约和 alpha 评价。

（3）锁死 Trader 顶层 action/lots 旁路。修改：Trader、执行工具、执行学习 trace、系统不变量和测试。原因：策略单不能用推荐顶层旧动作/手数字段绕过最终合约。

（4）把自适应学习安全过滤接入分析和决策。修改：分析师权重、资金读取、上下文校准 safety、PM 条件机会判断和测试。原因：候选/观察类学习只能作先验或 probe 线索，不能直接放大仓位。

==========2026年06月21日==========

（1）把 PM 收成统一证据分流器。修改：PM 机会分流、回测前验收、每日审计和测试。原因：机会必须分流为当前可交易、条件监控或明确不可交易原因。

（2）打通全市场机会评分、排序和资金部署解释。修改：PM、排序/资金字段、归因学习、审计和测试。原因：`opportunity_scorecard` 只解释资金优先级输入，最终仓位仍由唯一合约承载。

（3）锁定推荐顶层展示字段与唯一合约一致。修改：PM DB 更新、系统不变量和阶段流测试。原因：避免最终合约与推荐记录顶层展示字段不一致。

==========2026年06月22日==========

（1）把完整 episode/action-value 学习接入 PM 评分分项。修改：PM 学习评分、learning catalog、系统不变量、归因报告和测试。原因：正向 alpha 要支持从 probe、rank 晋升、放大到持有/退出的完整周期。

（2）把 PM 学习/排名边界纳入回测前和每日检测。修改：`pre_backtest_acceptance`、`system_invariants`、归因输出和图表。原因：学习、排名、资金部署和唯一合约之间必须真实接通并可诊断。

==========2026年06月23日==========

（1）修通研究员 action-value 到 PM 的传输断链。修改：PM 学习读取、action-value canonical 读取、研究写入和阶段流测试。原因：真实 action-value 必须保留关键字段，不能被空壳 trace 或弱先验覆盖。

（2）把“学习/排名存在但未影响合约且无解释”列为非策略断链。修改：`system_invariants`、`mechanism_effectiveness_audit`、阶段流和审计测试。原因：学习或排名未改变仓位时，也必须写出明确原因。

（3）新增只读机制有效性审计。修改：`mechanism_effectiveness_audit`、`system_invariants`、`backtest.py` 和测试。原因：检查 action-value 到 PM、Trader、Accountant、Reviewer/Researcher 的闭环是否真实有效。

（4）收敛执行学习 trace 与契约覆盖闸门。修改：`futures_audit`、Trader、执行工具、研究读取、`contract_coverage_audit`、`pre_backtest_acceptance` 和测试。原因：锁住执行学习 trace 的字段和 producer/consumer/audit/test 覆盖。

==========2026年06月24日==========

（1）把机制有效性审计改为交易生命周期场景审计。修改：机制审计、系统不变量、审计测试和机制文档。原因：开仓、条件监控、持仓、减仓/退出、未入选候选需要不同审计口径。

（2）补齐关键跨智能体边界保真测试。修改：PM、契约覆盖和阶段流测试。原因：锁住分析师证据、研究 action-value、PM 合约、Trader 执行结果在上下游之间的字段保真。

==========2026年06月25日==========

（1）修正 PM 记忆读取中“空历史挡住真历史”的断点。修改：PM 记忆读取、`pm_decision_memory_retrieval.py`、阶段流测试和契约覆盖。原因：PM 必须先收集所有可见历史，再按质量排序；空历史不能挡住真实有效历史。

（2）按固定工作流拆出 `signal_collector` 并让 PM 退出 LLM 调用。修改：信号收集员、决策工具、PM、workflow、schema、prompt、能力卡、工具权限、测试和机制文档。原因：固定“分析师结构化证据 -> 信号收集员统一证据包 -> PM 工具链 -> 唯一合约”链路。

（3）收干净复盘员、研究员、审计员和交易员边界。修改：Phase4、研究入口、审计员、交易员、执行工具、配置、系统不变量和协议测试。原因：复盘员不调 LLM、不写研究；审计员不消费研究记忆；交易员不读研究库下单。

（4）整理工具命名和公共 helper 边界。修改：decision/execution/research 工具、`src/tools/common/contracts.py`、`runtime_setup.py` 和测试。原因：工具按功能命名，公共基础能力放入 `src/tools/common`。

==========2026年06月26日==========

（1）切断 Phase4 completed 自动刷新研究记忆的旧副作用。修改：DB helper/interface、Phase4、研究入口、研究 writer、协议测试和机制文档。原因：Phase4 completed 只表示复盘验收通过，不能触发学习写入。

（2）把 template prior 从 Phase1 拆成显式研究初始化。修改：`proposal.py`、`run/research/load_template_prior.py`、`template_prior`、learning catalog 和测试。原因：冷启动研究种子不属于 Phase1 策略生成。

（3）收干净 phase completion 旧 API 与研究快照归属。修改：DB interface/helper、Phase4、`research_snapshot_reports`、研究入口和测试。原因：阶段完成 API 不再暗示学习副作用；研究快照归研究报告模块。

（4）完成字段、配置、提示词和分析师校准入口收尾。修改：字段语义表、learning catalog、prompt、分析师校准、研究 writer 和测试。原因：统一 `daily_settlement`、研究员 LLM notes、研究 causal review 命名和分析师安全校准入口。

（5）补齐分阶段 artifact 保存边界。修改：Trader、`futures_audit`、系统不变量、契约覆盖、阶段流测试和机制文档。原因：Phase2/transaction artifact 只能保存执行事实和必要摘要，不能镜像完整 PM 合约或 PM 解释字段。

（6）把真实 SQLite schema 契约接入回测前验收。修改：`pg_db_schema_contract`、`pre_backtest_acceptance`、`system_invariants` 和测试。原因：控制审计必须按真实表和日期字段读数据，schema 错误必须回测前 fail-fast。

==========2026年06月27日==========

（1）把系统事实载体契约落到数据模型，并收住执行/研究事实写入口。修改：`mechanism_data_model.md`、`futures_audit`、`system_invariants`、`research_learning`、`research_memory_writers` 和测试。原因：DB、artifact、payload 都必须服从授权事实入口，不能复制完整上游对象成为自己的事实。

（2）把事实入口边界同步到提示词、配置和契约覆盖。修改：`prompt.py`、learning/portfolio catalog、`contract_coverage_audit` 和测试。原因：提示词和配置必须明确“事实入口”口径，不能暗示第二交易出口或旧权限。

（3）把字段统一从口头承诺改成回测前检查。修改：`unified_field_audit`、`pre_backtest_acceptance` 和字段迁移测试。原因：旧字段只允许在迁移、负向测试和历史归档中出现，生产路径出现即 hard fail。

（4）代码化 contract parser、artifact boundary 和研究写入边界。修改：`tools/common/contracts.py`、Trader、`futures_audit`、执行工具、研究工具、Phase4、`test_fact_entry_boundaries`。原因：下游读取合约必须统一解析；各阶段写 artifact/payload 前必须校验边界。

（5）统一回测前和每日回测后测试编排入口。修改：`pre_backtest_test.py`、`backtest_daily_test.py`、`backtest.py`、相关 CLI 测试。原因：测试逻辑放 `src/tests/test_*.py`，run 脚本只负责编排。

（6）修正 artifact 边界检查器的事实对象语义。修改：`tools/common/contracts.py`、事实入口结构测试和机制/数据模型文档。原因：字段名本身不等于系统事实；数字计数、字符串状态、错误摘要、空列表和上游引用不是事实对象。

==========2026年06月28日==========

（1）统一工具命名和归属边界。修改：analysis 工具统一为 `analyst_*.py`，control 工具统一为 `pg_*.py`，PM 专用工具统一为 `pm_*.py`，Trader/Accountant 工具统一为执行团队命名；跨团队语义工具迁入 `src/tools/common`。原因：工具目录和文件名必须体现真实拥有者，避免后续跨团队误接。

（2）迁移跨团队 common 工具。修改：`position_lifecycle.py`、`signal_evidence_collection.py`、`adaptive_policy_safety.py`、`alpha_setup.py`、`learning_contract.py`、`neutral_accountability.py`、`template_prior.py`、`order_semantics.py`、`futures_market_rules.py`。原因：这些工具被多个团队共同读取，不属于单一智能体专用工具。

（3）收口 PM 内部转换工具链。修改：`pm_state_transition.py`、`pm_position_transition.py`、`pm_contract_builder.py`、`pm_contract_self_check.py`、PM 调用点和回测前矩阵测试。原因：PM 仍是唯一合约签发者，但状态转换、持仓转换、合约构造和自检由确定性决策工具链完成。

（4）恢复独立 Auditor 链路并清零旧审计命名。修改：`auditor.py`、PM 内部风险门工具、workflow、Trader、审计 payload、系统不变量、配置、文档和测试。原因：执行“PM 签唯一合约 -> 独立 Auditor 审合约 -> Trader 只执行审过合约”；PM 签约前风险门只是 PM 内部工具，不是独立审计员。

（5）拆出研究团队内部复用 helper。修改：新增 `research_review_helpers.py`，让 `reviewer_phase4_review.py`、`research_memory_writers.py`、`research_snapshot_reports.py` 依赖该 helper。原因：Reviewer 主工具只保留 Phase4 主流程和复盘报告，Researcher 不再反向依赖 Reviewer 主工具。

==========2026年06月29日==========

（1）新增 `src/tools/common/final_action_semantics.py` 统一交易语义状态机。修改：共享工具、PM、Trader、Auditor、Reviewer、Researcher、Protocol Governor、分析师落地校验、契约覆盖和测试。原因：消除 11 个智能体对同一字段、状态和 reason code 的解释漂移。

（2）修复条件监控合约被 Trader 误判硬阻断。修改：PM/Trader/PG 语义解释、盘中触发记录和回归测试。原因：条件监控合约必须进入盘中检查，未触发也必须写 `futures_intraday_decision`。

（3）修复 Phase1 加速测试假 DB 与独立 Auditor 状态写回接口不一致。修改：`src/tests/test_phase1_acceleration.py`。原因：workflow 已按独立 Auditor 链路调用 `update_futures_recommendation_status`，测试假 DB 必须实现同一接口。

==========2026年06月30日==========

（1）扩展 `final_action_semantics.py` 为交易生命周期记忆语义状态机。修改：共享语义工具、PM、PM 合约构造、PG 机制有效性审计和测试。原因：所有团队必须按同一套 `open/add/hold/reduce/exit/conditional_monitor/execution` 与 `memory_side_role` 解释 action-value。

（2）补齐 action-value `memory_side_role` canonical 字段。修改：SQLite schema/helper、研究写入器、`alpha_setup`、学习配置、提示词和测试。原因：固定 `side` 在学习记录中的角色，避免 PM 退出 long 时只按目标方向读记忆。

（3）让回测前总门先执行本地 SQLite schema 迁移。修改：`pre_backtest_test.py` 和 CLI 回归测试。原因：回测前验收要检查迁移后的真实运行库 schema，不能在 `init_database()` 补列前被旧库缺列误拦。

（4）建立三类分析师商品差异化分析和多维证据融合协议。修改：`product_price_behavior_profiles.yaml`、`analyst_product_price_behavior_profile.py`、`evidence_fusion_policy_catalog.yaml`、`evidence_fusion_semantics.py`、三类分析师入口、`signal_collection_contract`、PM 评分和唯一合约、Auditor/Reviewer/Researcher/PG 覆盖与测试。原因：分析师必须输出结构化预测证据和商品差异化校准，signal_collector 保真汇总，PM 再转成评分、rank 输入和合约解释。

（5）建立 PM 内部草稿隔离与最终合约原子提交协议。修改：PM artifact 边界、PG 系统不变量、阶段流/事实入口/系统不变量测试和机制文档。原因：PM 可以内部分步形成评分、排序和资金部署草稿，但对外只能一次性提交完整 `final_action_contract`。

==========2026年07月01日==========

（1）收口 PG 持仓生命周期解释语义。修改：`final_action_semantics.py`、`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py`、统一字段语义表和回归测试。原因：hold/exit 学习未改变仓位时，PG 必须用同一套确定性解释判断。

（2）收口 PG 统一语义入口。修改：`final_action_semantics.py`、`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py`、统一字段语义表和回归测试。原因：Protocol Governor 不再保留学习 lane 匹配、手数一致性、no-change/rank/learning 解释等私有判断，只检查正式事实。

（3）同步 `learning_used` 契约覆盖到 PG 统一语义入口。修改：`pg_contract_coverage_audit.py` 和 `test_contract_coverage_audit.py`。原因：回测前契约覆盖闸门必须识别当前真实 consumer 路径。

（4）收口学习记忆写入与消费生命周期硬锁。修改：`alpha_setup.py`、`final_action_semantics.py`、Researcher 写入器、PM 生命周期记忆读取、统一字段语义表和回归测试。原因：Researcher 写入和 PM 消费都必须匹配当前动作生命周期、方向和 `memory_side_role`。

==========2026年07月02日==========

（1）收口 PM 持仓学习消费闭环与 PG 重复报错。修改：`portfolio_manager.py`、`final_action_semantics.py`、`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py`、统一字段语义表和回归测试。原因：PM 消费 hold/exit 类 PM 学习时，必须减仓、退出或写出合法继续持有解释；PG 对同一推荐 ID 的同一问题只报一次。

（2）收口 AgentQuant Codex GPT-5.5 调用路由一致性。修改：`.env`、`.env.example` 注释和 `test_protocol_preflight_cli.py`。原因：固定当前 LLM 主路由和 `reasoning_effort=medium`，TQX 只作为停用备用接口保留。

（3）收口 `final_action_semantics.py` 迁移依赖完整性。修改：`final_action_semantics.py`、`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py` 和回归测试。原因：PG 统一语义入口迁移后，共享 action preference 常量也必须迁入同一语义工具。

==========2026年07月03日==========

（1）强化产品级动态学习写入身份键。修改：`alpha_setup.py`、统一字段语义表和 `test_reviewer_learning.py`。原因：Researcher 写入 alpha setup/profile/action-value 时，必须把产品、方向、setup、触发、证据组合、资金部署结果、rank/score 和后续收益绑定成同一个 `product_learning_performance_key`。

（2）强化分析师读取产品级学习的安全视图。修改：`alpha_setup.py`、`analyst_learning_context.py`、`analyst_learning_calibration.py`、统一字段语义表和分析师/学习回归测试。原因：分析师只能把产品级历史表现用于证据质量、确认需求、setup 分类和待验证问题校准，不能获得 PM 权限字段。

（3）收口唯一资金优先级 rank 语义。修改：`pm_signal_fusion.py`、`pm_full_market_capital_deployment.py`、`pm_contract_builder.py`、`final_action_semantics.py`、统一字段语义表和回归测试。原因：系统只保留一个 `opportunity_rank`，`rank=1` 固定表示当前最值得占用资金的机会；rank 解释必须同步落入最终合约证据和 `capital_deployment`，且不能直接成为交易权限。

（4）打通 rank 到真实资金部署出口。修改：`portfolio_manager.py`、`pm_full_market_capital_deployment.py`、统一字段语义表、机制文档和 PM 阶段流回归测试。原因：`tradeable_candidate`、反复验证 alpha 候选和 watch/probe 候选通过同一资金优先级 rank 映射到不同资金层级，不新增第二套 rank。

（5）同步分析师提示词和主配置说明。修改：`prompt.py`、`dev.yaml` 注释和提示词回归测试。原因：机制解释放机制文档和统一字段语义表，主配置只保留运行入口、catalog 索引、资金红线、交易宇宙和 LLM 路由重点说明。

==========2026年07月04日==========

（1）收口全市场资金优先级 rank 生成权到 PM。修改：`pm_signal_fusion.py`、`pm_ticker_side_selection.py`、`pm_full_market_capital_deployment.py`、`pm_contract_builder.py`、`portfolio_manager.py`、`final_action_semantics.py`、PG 检查、契约覆盖、统一字段语义表和回归测试。原因：单品种 long/short 只能生成 `side_priority/ticker_side_priority`，最终资金 rank 只能由 PM 第 5 步全市场资金部署工具生成。

（2）收口新增风险敞口全市场 rank 闸门。修改：`pm_full_market_capital_deployment.py`、`portfolio_manager.py`、`final_action_semantics.py`、`pg_system_invariants.py`、统一字段语义表和回归测试。原因：所有新增风险动作必须先获得 PM 第 5 步部署事实；未入选或预算不足必须还原到无新增风险并写清原因。

==========2026年07月05日==========

（1）收口未部署条件候选与新增风险 rank 闸门的 PG 机制审计语义。修改：`final_action_semantics.py`、`pg_mechanism_effectiveness_audit.py`、统一字段语义表和机制审计回归测试。原因：只有真正部署新增风险敞口的条件开仓才需要 `opportunity_rank` 和 Trader 盘中结果；未部署且无新增风险候选只要求明确未选中原因。

（2）收口全市场资金 rank 目标函数与生命周期学习入口。修改：`pm_signal_fusion.py`、`pm_full_market_capital_deployment.py`、`portfolio_manager.py`、`pm_contract_builder.py`、`final_action_semantics.py`、PG 检查、契约覆盖、统一字段语义表和回归测试。原因：`rank_score/rank_score_components` 是唯一全市场资金 rank 的排序输入；open/add/scale/increase 学习服务新资金 rank，hold/reduce/exit 学习服务持仓和释放资金，execution 学习只服务 trigger/profile。

（3）收口 Trader 条件触发记录与下单安全闸顺序。修改：`trader.py`、`final_action_semantics.py`、统一字段语义表和回归测试。原因：Trader 对需要盘中确认的合约先写触发/未触发事实，未触发不下单，触发后再运行最终下单安全闸。

（4）补齐 PM 最终合约生命周期学习落盘和 artifact 边界。修改：`portfolio_manager.py`、`pm_contract_builder.py`、`pm_contract_self_check.py`、`tools/common/contracts.py`、PG 检查、统一字段语义表和回归测试。原因：rank 与非 rank 合约都必须按机制落入 lifecycle trace；内部 `learning_to_position_trace`、`adaptive_policy_state`、`strategy_memory` 和策略行对象不得进入 PM artifact。

（5）收口 PM 六步机制与 workflow 编排边界。修改：`portfolio_manager.py`、`workflow.py`、`pm_ticker_side_selection.py`、`pm_lifecycle_action_port.py`、`pm_lifecycle_learning_router.py`、`pm_full_market_capital_deployment.py`、`final_action_semantics.py`、`pg_contract_coverage_audit.py`、统一字段语义表、PM/workflow 机制文档和回归测试。原因：PM 才是组合决策者，workflow 只是编排层；workflow 不生成 rank、不做预算部署、不改 `final_action_contract`、不补字段。

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

==========当前验证口径==========

（1）回测前总门：`src/run/pre_backtest_test.py`。

（2）每日回测后总门：`src/run/backtest_daily_test.py`。

（3）结构测试重点：事实入口、合约解析、artifact 边界、结算公式、研究写入、控制组只读、PM 状态转换、分析师输出落地。

（4）回测前验收只检查系统可运行性、字段/schema/权限/硬数据/边界和确定性转换规则，不评价策略收益。
