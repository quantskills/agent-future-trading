# AgentQuant 工作日志

本文件是基于当前系统状态重整后的短版开发索引。保留按天划分的结构，只记录仍能解释现有代码、配置、字段、智能体边界和回测前验收的修改。已被后续重构覆盖的中间补丁、旧字段口径、旧工具名、旧运行入口和纯讨论内容不再保留。

字段语义以 `docs/unified_field_semantics.md` 为准；智能体工作流、权限边界、事实入口和 artifact 边界以 `docs/mechanism_multiagents.md` 为准；数据载体与 DB schema 口径以 `docs/mechanism_data_model.md` 为准；智能体内部转换规则以 `docs/mechanism_agent_internal_rules.md` 为准。

每条只保留：修改了什么、为什么改。

==========2026年06月08日==========

（1）建立配置展开和资金目录基础。修改：`config_normalizer`、`dev.yaml`、portfolio/learning catalog 和配置测试。原因：主配置只承担运行入口职责，策略参数、资金边界和学习策略由 catalog 管理。

（2）收束 PM 最终推荐出口。修改：PM、Trader、reason-effects 和回归测试。原因：防止“不交易、无触发、只观察”等语义被最小手数、probe 或自然语言推成真实开仓。

==========2026年06月09日==========

（1）贯通分析师结构化证据进入 PM。修改：分析师证据质量工具、公共合约工具、PM 消费路径和测试。原因：分析师输出从方向文本升级为 PM 可读的 `action_evidence_contract`。

（2）补齐学习闭环进入 PM 候选和交易出口的测试基础。修改：PM 学习消费和 scorecard 测试。原因：证明历史学习能被 PM 看见并影响候选判断，但不能绕过最终合约。

==========2026年06月10日==========

（1）收束软门控和持仓生命周期保护。修改：PM 生命周期、reason-effect、资金/风控配置和测试。原因：软风险只降级、缩手数或要求确认，硬风险才阻断交易。

（2）打通 action-value 服务 PM 评分和执行 profile 的早期路径。修改：研究写入、DB 读取、PM 学习消费、执行 profile 和测试。原因：open/hold/exit/execution 分动作学习可进入 PM，但不能成为交易员直接权限。

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

（2）打通全市场机会评分、排序和资金部署解释。修改：PM、workflow、排序/资金字段、归因学习、审计和测试。原因：`opportunity_scorecard/rank/capital_allocation_reason` 只解释资金优先级，最终仓位仍由唯一合约承载。

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

（1）修正 PM 记忆读取中“空历史挡住真历史”的断点。修改：PM 记忆读取、`decision_memory_retrieval`、阶段流测试和契约覆盖。原因：PM 必须先收集所有可见历史，再按质量排序；空历史不能挡住真实有效历史。

（2）按固定工作流拆出 `signal_collector` 并让 PM 退出 LLM 调用。修改：信号收集员、决策工具、PM、workflow、schema、prompt、能力卡、工具权限、测试和机制文档。原因：固定“分析师结构化证据 -> 信号收集员统一证据包 -> PM 工具链 -> 唯一合约”链路。

（3）收干净复盘员、研究员、审计员和交易员边界。修改：Phase4、研究入口、审计员、交易员、执行工具、配置、系统不变量和协议测试。原因：复盘员不调 LLM、不写研究；审计员不消费研究记忆；交易员不读研究库下单。

（4）整理工具命名和公共 helper 边界。修改：decision/execution/research 工具、`src/tools/common/contracts.py`、`runtime_setup.py` 和测试。原因：工具按功能命名，公共基础能力放入 `src/tools/common`。

==========2026年06月26日==========

（1）切断 Phase4 completed 自动刷新研究记忆的旧副作用。修改：DB helper/interface、Phase4、研究入口、研究 writer、协议测试和机制文档。原因：Phase4 completed 只表示复盘验收通过，不能触发学习写入。

（2）把 template prior 从 Phase1 拆成显式研究初始化。修改：`proposal.py`、`run/research/load_template_prior.py`、`template_prior`、learning catalog 和测试。原因：冷启动研究种子不属于 Phase1 策略生成。

（3）收干净 phase completion 旧 API 与研究快照归属。修改：DB interface/helper、Phase4、`research_snapshot_reports`、研究入口和测试。原因：阶段完成 API 不再暗示学习副作用；研究快照归研究报告模块。

（4）完成字段、配置、提示词和分析师校准入口收尾。修改：字段语义表、learning catalog、prompt、分析师校准、研究 writer 和测试。原因：统一 `daily_settlement`、研究员 LLM notes、研究 causal review 命名和分析师安全校准入口。

（5）补齐分阶段 artifact 保存边界。修改：Trader、`futures_audit`、系统不变量、契约覆盖、阶段流测试和机制文档。原因：Phase2/transaction artifact 只能保存执行事实和必要摘要，不能镜像完整 PM 合约或 PM 解释字段。

（6）把真实 SQLite schema 契约接入回测前验收。修改：`db_schema_contract`、`pre_backtest_acceptance`、`system_invariants` 和测试。原因：控制审计必须按真实表和日期字段读数据，schema 错误必须回测前 fail-fast。

==========2026年06月27日==========

（1）把系统事实载体契约落到数据模型，并收住执行/研究事实写入口。修改：`mechanism_data_model.md`、`futures_audit`、`system_invariants`、`research_learning`、`research_memory_writers` 和测试。原因：DB、artifact、payload 都必须服从授权事实入口，不能复制完整上游对象成为自己的事实。

（2）把事实入口边界同步到提示词、配置和契约覆盖。修改：`prompt.py`、learning/portfolio catalog、`contract_coverage_audit` 和测试。原因：提示词和配置必须明确“事实入口”口径，不能暗示第二交易出口或旧权限。

（3）把字段统一从口头承诺改成回测前检查。修改：`unified_field_audit`、`pre_backtest_acceptance` 和字段迁移测试。原因：旧字段只允许在迁移、负向测试和历史归档中出现，生产路径出现即 hard fail。

（4）代码化 contract parser、artifact boundary 和研究写入边界。修改：`tools/common/contracts.py`、Trader、`futures_audit`、执行工具、研究工具、Phase4、`test_fact_entry_boundaries`。原因：下游读取合约必须统一解析；各阶段写 artifact/payload 前必须校验边界。

（5）统一回测前和每日回测后测试编排入口。修改：`pre_backtest_test.py`、`backtest_daily_test.py`、`backtest.py`、相关 CLI 测试；删除旧 `src/run/control` CLI 壳。原因：测试逻辑放 `src/tests/test_*.py`，run 脚本只负责编排。

（6）扩展回测前硬数据检查到 15 个交易品种。修改：`pre_backtest_acceptance`、数据时间边界测试和数据模型文档。原因：基本面/新闻不要求每日齐全，但行情、开收盘、结算价和主力合约映射是硬依赖。

（7）补齐会计师结算公式固定样例测试。修改：`test_accountant_settlement_formulas.py`、`pre_backtest_test.py`。原因：回测前用固定样例锁住手续费、保证金、PnL、权益和可用资金公式。

（8）收口分析师学习消费、执行摘要净化和研究 helper 归属。修改：analysis learning context、`contracts.py`、Trader、执行工具、Phase4、research writer 和测试。原因：分析师不直接消费交易 action-value；执行摘要只保留执行规则字段；研究 policy helper 归研究 writer。

（9）清理旧智能体、旧脚本、缓存和配置冗余。修改：退休分析师占位、旧 prompt/graph 常量、`__pycache__`、learning/portfolio catalog 和参数文档。原因：删除不启用入口和无消费方伪开关，降低后续误接风险。

（10）重整主配置中文注释。修改：`src/config/dev.yaml`。原因：主配置只解释配置职责、资金保护区和不可绕过边界，不再堆叠完整机制说明。

（11）修正 artifact 边界检查器的事实对象语义。修改：`tools/common/contracts.py`、事实入口结构测试和机制/数据模型文档。原因：字段名本身不等于系统事实；数字计数、字符串状态、错误摘要、空列表和上游引用不是事实对象。PM、Trader、Accountant、Reviewer、Researcher 的 artifact 边界统一按“本阶段禁止字段 + 非空 dict/list 事实对象”判断，避免把研究来源摘要误判为研究事实越界。

==========2026年06月28日==========

（1）收口智能体内部转换中的 reason code 语义和 Trader 输入读取。修改：`reason_effects`、PM、Trader、`test_pm_watch_for_trigger_release`、`test_phase_flow_regression`。原因：候选类 reason code 只能保留观察、条件触发或排序资格，不能再被当成软阻断或弱理由清零；释放类 reason code 仍必须通过 PM 最终权限、硬门控和审计。Trader 的 Phase2 转换函数同步从 PM 推荐的标准 `signal_snapshot` 补齐工作快照，避免正确 PM 合约因调用方传入空 snapshot 被错误翻译成 hold。

（2）对齐 PM 内部风险门、Reviewer 能力卡和分析师提示词口径。修改：PM 内部风险门命名、能力卡、契约 fixture、`prompt.py`、配置注释和字段语义表。原因：PM 签约前风险门不再冒充独立审计员，不再写旧 planner 与 PM-LLM 生产镜像；Reviewer 只产出复盘事实和研究输入材料，不写未来学习；分析师提示词不再要求输出 `action_name` 交易动作名。

（3）恢复独立审计员链路并清零旧审计命名。修改：`auditor.py`、PM 内部风险门工具、workflow、Trader、审计 payload、系统不变量、配置、文档和测试。原因：严格执行“PM 签唯一合约 -> 独立 Auditor 审合约 -> Trader 只执行审过合约”；PM 签约前风险门只是 PM 内部工具，不是智能体；自动回测在显式 `--reset-config` 时先清旧配置数据再跑回测前总门，避免旧产物被误当成新链路失败。

（4）落地 PM 内部转换工具链和回测前矩阵测试。修改：`pm_state_transition.py`、`pm_position_transition.py`、`pm_contract_builder.py`、`pm_contract_self_check.py`、PM 调用点、分析师输出落地校验、`pre_backtest_test.py` 和机制文档。原因：PM 仍是唯一合约签发者，但状态转换、持仓转换、合约构造和自检改为决策工具链；分析师 LLM 输出只能落到结构化证据，不能落地手数、仓位或最终动作。

（5）重整回测前与每日回测后检测分层。修改：`pre_backtest_test.py`、`backtest_daily_test.py`、`test_system_invariant_audit.py`、`mechanism_agent_internal_rules.md`、`mechanism_multiagents.md`、`mechanism_future_trade.md`。原因：规则、转换、schema、权限、公式和样例审计全部前置到回测前；每日回测后只读真实 DB/artifact/payload 做动态产物审计，避免重复跑静态测试。

（6）统一分析团队工具命名。修改：`src/tools/agent_tools/analysis/` 下工具统一改为 `analyst_*.py`，并同步分析师、PM、workflow、API、研究复盘、契约覆盖和测试引用。原因：分析团队工具目录只放分析团队工具，文件名必须体现所属智能体和工具作用，避免后续跨团队误接。

（7）统一协议管理员工具命名。修改：`src/tools/agent_tools/control/` 下工具统一改为 `pg_*.py`，并同步协议管理员、回测前/每日检测入口、控制审计内部依赖、测试和机制文档引用。原因：控制目录是协议管理员和治理检测工具目录，文件名必须体现 `protocol governor` 归属，避免被误认成业务智能体工具。

（8）统一决策团队中 PM 专用工具命名。修改：`src/tools/agent_tools/decision/` 下 PM 专用工具改为 `pm_*.py`，包括资金分配、资金部署、决策记忆、失效边界、机会排序、手数计算、reason code、硬风险、风险控制和上下文规则校准工具；同步 PM、分析师技术校准入口、决策工具内部依赖、测试和文档引用。原因：决策目录属于信号收集员、PM、Auditor 共同团队；本轮只改 PM 口径工具，共用或非 PM 归属工具暂不改名。

（9）迁移跨阶段共用持仓生命周期工具。修改：`position_lifecycle.py` 从 `src/tools/agent_tools/decision/` 移到 `src/tools/common/`，并同步 PM、Trader 和 PM 决策工具引用。原因：该工具被 PM 与 Trader 直接共用，不属于单一决策智能体专用工具，应按 common 工具“功能名、不加智能体前缀”管理。

（10）迁移决策团队共用信号证据集合工具。修改：`signal_evidence_collection.py` 从 `src/tools/agent_tools/decision/` 移到 `src/tools/common/`，并同步信号收集员、PM、契约覆盖和测试引用。原因：该工具由信号收集员和 PM 直接共用，不属于单一智能体专用工具，应按 common 工具“功能名、不加智能体前缀”管理。

（11）统一执行团队工具命名和共用工具归属。修改：`src/tools/agent_tools/execution/` 下 Trader 直接专用工具改为 `trader_*.py`，Accountant 直接专用结算工具改为 `accountant_futures_settlement.py`；跨 PM/Trader/Research/Control 共用的 `order_semantics.py` 移到 `src/tools/common/`；Trader 执行引擎佣金工具改为 `src/tools/agent_tools/execution/trade_futures_commission.py`；执行和结算共用的 `futures_market_rules.py` 移到 `src/tools/common/`；同步 Trader、Accountant、PM、workflow、研究、控制审计、契约覆盖和测试引用。原因：execution 目录保留执行团队直接或交易执行链工具；跨阶段语义和市场规则归 common。

（12）迁移跨团队自适应策略安全过滤工具。修改：`adaptive_policy_safety.py` 从 `src/tools/agent_tools/research/` 移到 `src/tools/common/`，并同步分析师学习校准、PM 决策记忆/资金/规则校准、控制审计和测试引用。原因：该工具被分析师、PM 决策工具和控制审计共同读取，不属于复盘员或研究员专用工具，应按 common 工具管理。

（13）迁移跨团队 alpha setup 机制工具。修改：`alpha_setup.py` 从 `src/tools/agent_tools/research/` 移到 `src/tools/common/`，并同步研究员学习、分析师学习上下文、PM trace、协议权限和测试引用。原因：该工具由 Researcher 写学习事实，同时被分析师和 PM 读取安全摘要，不属于复盘员或研究员单一专用工具，应按 common 工具管理。

（14）迁移跨团队学习合约工具。修改：`learning_contract.py` 从 `src/tools/agent_tools/research/` 移到 `src/tools/common/`，并同步 DB helper、alpha setup、分析师学习上下文、Phase4、研究学习、研究写入器和测试引用。原因：该工具定义 next-round memory contract，被分析、研究和数据库写入共同使用，不属于研究团队单一专用工具。

（15）迁移跨团队 neutral 责任诊断工具。修改：`neutral_accountability.py` 从 `src/tools/agent_tools/research/` 移到 `src/tools/common/`，并同步 Phase4、研究快照、评估模块和测试引用。原因：该工具被复盘、研究报告、研究写入和评估共同使用，不属于复盘员或研究员单一专用工具。

（16）统一复盘员 Phase4 主工具命名。修改：`phase4_review.py` 改为 `reviewer_phase4_review.py`，并同步 Reviewer、研究学习入口、研究报告、研究写入、运行校验、契约覆盖和测试引用。原因：该文件主入口由复盘员直接使用，按研究团队工具命名体现 Reviewer 归属；artifact 阶段字段 `phase4_review` 不变。

（17）迁移 template prior 冷启动加载工具。修改：`template_prior.py` 从 `src/tools/agent_tools/research/` 移到 `src/tools/common/`，并同步研究初始化运行脚本和测试引用。原因：该工具只负责显式加载冷启动研究种子，被运行入口和测试使用，不属于复盘员或研究员单一专用工具。

（18）拆出研究团队内部复用 helper，切断 Researcher 对 Reviewer 主工具的反向依赖。修改：新增 `research_review_helpers.py`，让 `reviewer_phase4_review.py`、`research_memory_writers.py`、`research_snapshot_reports.py` 依赖该 helper；研究 writer 改为显式导入自身需要的基础依赖；补 `test_protocol_governor` 结构测试。原因：Reviewer 主工具只保留 Phase4 主流程和复盘报告，Researcher 不再从 Reviewer 主工具偷用格式化、快照解析、统计和报告 rows helper。

==========2026年06月29日==========

（1）新增 `src/tools/common/final_action_semantics.py` 统一交易语义状态机。修改：共享工具、PM、Trader、Auditor、Reviewer、Researcher、Protocol Governor、分析师落地校验、契约覆盖和测试。原因：消除 11 个智能体对同一字段、状态和 reason code 的解释漂移，固定条件监控、直接执行、普通持有、硬阻断、软降级、新开仓、扩大交易、减仓、退出、未触发和已触发成交的唯一解释。

（2）修复条件监控合约被 Trader 误判硬阻断。修改：`real_probe_qualification_not_met` 固定为软降级，Trader/PM/PG 改用共享语义解释器，条件监控合约必须进入盘中检查，未触发也必须写 `futures_intraday_decision`。原因：此前 PM 已签条件监控，但 Trader 本地 hard list 把软降级当阻断，导致每日机制检查报 `mechanism_conditional_probe_missing_intraday_result`。

（3）修复 Phase1 加速测试假 DB 与独立 Auditor 状态写回接口不一致。修改：`src/tests/test_phase1_acceleration.py`。原因：全量单测中 workflow 已按独立 Auditor 链路调用 `update_futures_recommendation_status`，测试假 DB 缺少该接口会造成夹具断裂，无法用全量测试干净判断回测前系统状态。

==========2026年06月30日==========

（1）扩展 `final_action_semantics.py` 为交易生命周期记忆语义状态机。修改：共享语义工具、PM、PM 合约构造、PG 机制有效性审计和测试。原因：PM、Auditor、Trader、Accountant、Reviewer、Researcher、Protocol Governor 必须按同一套 `open/add/hold/reduce/exit/conditional_monitor/execution` 与 `memory_side_role` 解释 action-value；减仓/退出必须读取当前持仓方向记忆，条件监控必须读取触发方向记忆。

（2）补齐 action-value `memory_side_role` canonical 字段。修改：SQLite schema/helper、研究写入器、`alpha_setup`、学习配置、提示词和测试。原因：固定 `side` 在学习记录中的角色，区分 `target_side/current_position_side/trigger_side/historical_sample_side`，避免 PM 退出 long 时只按目标方向读记忆。

（3）让回测前总门先执行本地 SQLite schema 迁移。修改：`pre_backtest_test.py` 和 CLI 回归测试。原因：回测前验收要检查迁移后的真实运行库 schema，不能在 `init_database()` 补列前被旧库缺列误拦。

（4）补齐 Auditor/Reviewer/Researcher 在交易生命周期记忆语义里的显式边界。修改：`final_action_semantics.py`、Auditor、Reviewer Phase4、Researcher 写入器和测试。原因：Auditor 只按 PM 合约审 `learning_used.memory_requirements` 与 `alpha_setup_action_values` 覆盖，不查研究库、不改方向手数；Reviewer 标注生命周期和历史学习是否影响 PM 合约；Researcher 缺 `action_value_lane/learning_lane/consumer_scope/memory_side_role/last_sample_date/valid_until/reward_source/evidence_scope` 的记录不能进入 PM 可消费记忆。

（5）锁住 Trader、Accountant、三类分析师和 signal_collector 的非记忆解释边界。修改：分析师输出落地测试、信号收集测试、事实入口边界测试和决策工具测试。原因：Trader 只继承 PM 合约学习解释、不读 action-value；Accountant 只读成交、持仓、结算价、费用、保证金和权益；分析师不能输出合约、手数、保证金、reason code 或 authority type；signal_collector 只保真收集证据，不读 action-value、不生成交易动作。

（6）建立三类分析师商品差异化分析协议。修改：新增 `src/config/product_price_behavior_profiles.yaml` 和 `src/tools/agent_tools/analysis/analyst_product_price_behavior_profile.py`，同步三类分析师入口、提示词、`signal_collection_contract` 保真传递、能力卡、契约覆盖、统一字段语义、机制文档和回测前测试。原因：technical、fundamental、commodity_news 必须按不同期货品种的价格行为、趋势惯性、波动特征、产业链确认、季节窗口、假突破风险和适用 setup 做差异化分析；该 profile 只作为冷启动分析框架，动态学习通过 `learning_context` 与 `analyst_learning_calibration` 叠加，不创建交易权限、不改 PM/Auditor/Trader/Accountant 边界。

（7）建立多维证据融合预测协议。修改：新增 `src/config/evidence_fusion_policy_catalog.yaml` 和 `src/tools/common/evidence_fusion_semantics.py`，同步三类分析师落地、`signal_collection_contract`、PM 评分和唯一合约、Auditor 审核、Reviewer 归因、Researcher 未来学习、协议管理员能力卡/契约覆盖、统一字段语义、机制文档、提示词和回测前测试。原因：technical、fundamental、commodity_news 必须把技术、基本面、新闻、商品 profile、历史学习和执行反馈形成的预测证据强弱、时效、一致性、冲突、确认需求和缺失证据结构化传给 PM；signal_collector 只保真汇总，PM 只把融合诊断作为 scorecard 分项和合约解释，Auditor 只审 PM 是否解释主要冲突，Trader/Accountant 不读取融合证据改执行或结算。

（8）建立 PM 内部草稿隔离与最终合约原子提交协议。修改：workflow 资金部署提交口、PM artifact 边界、PG 系统不变量、阶段流/事实入口/系统不变量测试和机制文档。原因：PM 可以在内部内存分步形成评分、排序和资金部署草稿，但对外只能一次性提交完整 `final_action_contract`；凡最终合约出现 rank、新开、加仓、扩大或条件监控，必须同时落入 `capital_deployment`、资金理由和部署前后手数，杜绝裸 rank、半成品资金部署和 PM 草稿被下游偷看。

（9）收口交易生命周期记忆 lane 覆盖与 PG 旧检查路径。修改：`final_action_semantics.py`、PM 生命周期记忆读取、PG 系统不变量、PG 机制有效性审计、机制文档和回归测试。原因：open 学习只能支持开仓，不能冒充持仓、减仓、退出或条件监控学习；add/scale/increase 仍可读取 add/scale/increase/open 与当前持仓 hold 学习；PG 学习检查改为按当前合约动作要求匹配记忆，Auditor block/require_review 的条件监控不再要求 Trader 写盘中结果，从而修记录和检查口径，不新增交易阻断。

（10）修复 Researcher 聚合学习写回代表样本归因路径。修改：`research_memory_writers.py`、`test_reviewer_learning.py` 和统一字段语义表。原因：机会排序学习按多条 episode 聚合，但证据融合归因必须绑定组内确定性代表样本；避免单条推荐上下文和聚合样本上下文混用导致 Phase4 后 `researcher_learning.py` 写入失败。

==========2026年07月01日==========

（1）收口 PG 持仓生命周期解释语义。修改：`final_action_semantics.py`、`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py`、统一字段语义表和回归测试。原因：当 hold/exit 学习未导致减仓或退出时，两条 PG 检查必须用同一套确定性解释判断；`holding_period_control` 是合法持仓生命周期解释，`position_matched` 只能解释仓位已匹配，不能单独解释负向 hold/exit 学习。

（2）收口 PG 统一语义入口。修改：`final_action_semantics.py`、`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py`、统一字段语义表和回归测试。原因：Protocol Governor 不再保留学习 lane 匹配、`final_action` 与手数变化一致性、泛化 no-change/rank/learning 解释、active opportunity routing 和 open transaction blocking 的私有判断；两条 PG 检查统一调用共享语义工具，只检查正式事实，不改策略、参数、交易生成、结算、学习写回或已有回测记录。

（3）同步 `learning_used` 契约覆盖到 PG 统一语义入口。修改：`pg_contract_coverage_audit.py` 和 `test_contract_coverage_audit.py`。原因：`learning_used` 的消费路径已迁入 `final_action_semantics.py` 共享解释器，并由两条 PG 检查调用共享语义函数；回测前契约覆盖闸门必须识别当前真实 consumer 路径，不能继续按旧文件和旧字段组合误判缺失。

（4）收口学习记忆写入与消费生命周期硬锁。修改：`alpha_setup.py`、`final_action_semantics.py`、Researcher 写入器、PM 生命周期记忆读取、统一字段语义表和回归测试。原因：Researcher 写入真实正收益 open action-value 时必须写成 `positive_candidate_open`，不能写成保护/止损偏好；PM 最终合约只能落入与当前动作生命周期、方向和 `memory_side_role` 匹配的 `pm_learning`，持仓/减仓/退出不能把不匹配方向的 open 或 execution 学习写进 `learning_used.alpha_setup_action_values`。分析师主逻辑不改，只补边界测试，确保三类分析师只读学习摘要与校准，不直接读取 PM action-value 或输出交易权限。

（5）修复每日交易日志中文模板乱码。修改：`reviewer_phase4_review.py` 和交易日志可读性回归测试。原因：6 月 28 日 Reviewer Phase4 文件迁移时交易日志中文模板被损坏；本次只恢复 `*_transaction.log` 的人类可读中文标题和段落说明，不改策略、数据库、回测记录、Phase4 复盘逻辑、研究学习写回或 30 个交易日回测计划。

==========2026年07月02日==========

（1）收口 PM 持仓学习消费闭环与 PG 重复报错。修改：`portfolio_manager.py`、`final_action_semantics.py`、`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py`、统一字段语义表和回归测试。原因：PM 在最终合约为继续持仓且消费 hold/exit 类 PM 学习时，必须做到减仓、退出或写入合法继续持有解释；`position_matched` 只能解释仓位已匹配，不能单独解释 hold/exit 学习未落地。PG 对同一推荐 ID 的同一 hold/exit 未落地问题只报一次，不降低 hard fail，不改策略参数、不改 Trader/Accountant/Researcher 主逻辑、不改数据库或回测记录。

（2）收口 AgentQuant Codex GPT-5.5 调用路由一致性。修改：`.env`、`.env.example` 注释和 `test_protocol_preflight_cli.py`。原因：当前 LLM 主路由固定为 `CodexOpenAI -> gpt-5.5 -> http://47.74.0.65/v1 -> CODEX_OPENAI_API_KEY -> reasoning_effort=medium`；`dev.yaml` 实际配置值不改，`base_url: http://47.74.0.65` 继续由运行时规范化为 `/v1`，TQX 只作为备用第三方 LLM 接口保留且停用。新增回归测试锁住 runtime `ChatOpenAI.extra_body={"reasoning_effort": "medium"}` 和 `.env.example` 说明口径，不改提示词、智能体边界、策略参数、数据库或回测记录。

（3）收口 `final_action_semantics.py` 迁移依赖完整性。修改：`final_action_semantics.py`、`pg_system_invariants.py`、`pg_mechanism_effectiveness_audit.py` 和回归测试。原因：PG 统一语义入口迁移后，`contract_consumes_hold_exit_pm_learning()` 已进入共享语义工具，但 `ACTION_PREFERENCE_VALUES` 仍停留在 PG 私有常量中，导致部分日期 daily gate 命中 hold/exit 学习分支时崩溃；本次把 action preference 常量迁入共享语义工具，PG 两条检查只导入同一常量，并用 2025-03-12、2025-03-20、2025-03-24 触发分支做回归，不改策略、参数、交易生成、数据库或回测记录。

==========2026年07月03日==========

（1）强化产品级动态学习写入身份键。修改：`alpha_setup.py`、统一字段语义表和 `test_reviewer_learning.py`。原因：Researcher 写入 alpha setup/profile/action-value 时，必须把产品、方向、setup、触发、证据组合、资金部署结果、rank/score 和后续收益绑定成同一个 `product_learning_performance_key`；该键只服务下一轮分析师校准、PM 排名和资金部署学习，不创建交易权限、不改 PM/Trader/Accountant 边界、不硬编码具体品种好坏。

（2）强化分析师读取产品级学习的安全视图。修改：`alpha_setup.py`、`analyst_learning_context.py`、`analyst_learning_calibration.py`、统一字段语义表和分析师/学习回归测试。原因：三类分析师下一交易日必须能通过 `learning_context` 读取产品、方向、setup、触发、证据组合、历史部署层级、历史 PM rank/score 和后续收益形成的产品级表现摘要，但只能作为证据质量校准和待验证问题；安全视图改用 `historical_pm_rank/historical_pm_score`，不暴露 `authority_type/final_action/target_lots/lots_delta/opportunity_rank` 等 PM 权限字段，不改 PM/Trader/Accountant 边界。

（3）固定唯一 rank 为资金优先级语义。修改：`analyst_signal_fusion.py`、`pm_opportunity_ranking.py`、`pm_contract_builder.py`、`workflow.py`、统一字段语义表和 PM 排序/资金部署回归测试。原因：系统只保留一个 `opportunity_rank`，`rank=1` 固定表示当前最值得投入资金的机会；新增 `capital_priority_score/tier` 作为唯一 rank 的排序输入和解释字段，全市场资金部署队列按同一资金优先级口径重排，并把 `rank_semantics_version/opportunity_rank_meaning/rank_is_capital_priority/rank_is_not_trade_authority` 原子写入 scorecard、最终合约证据和 `capital_deployment`。本次不新增第二套 rank，不让 rank 直接成为交易权限，不改 Trader/Accountant 边界。

（4）打通 rank 到真实资金部署出口。修改：`portfolio_manager.py`、统一字段语义表、机制文档和 PM 阶段流回归测试。原因：`rank=1` 不再只停留在观察/探针队列解释中；当唯一资金优先级 rank 对应 `tradeable_candidate`，且当前开仓证据、失效边界、确认质量、无技术反对和硬风险均通过时，PM 最终出口可写入 `rank_capital_priority_real_budget_release` 并释放 `real_budget_entry`。本次不新增第二套 rank，不让 rank 单独授权交易，不改 Trader/Accountant 边界，不通过压低交易频率改善结果。

（5）把亏损开仓 episode 反写到入场质量。修改：`alpha_setup.py`、`analyst_signal_fusion.py`、统一字段语义表、机制文档和回归测试。原因：Researcher 写入产品级学习时必须把开仓亏损绑定回原始 setup、触发、证据组合和资金部署层级，PM 下一轮用 `entry_quality_loss_signal`、`trigger_quality_loss_signal`、`entry_quality_loss_penalty`、`trigger_quality_loss_penalty` 调整唯一 rank 和真实资金部署资格；本次只降低同类低质量入场/触发的资金优先级，不新增硬阻断，不改 Trader/Accountant 边界，不压低整体交易频率。

（6）校准触发质量并保留放大通道。修改：`alpha_setup.py`、`analyst_signal_fusion.py`、`portfolio_policy_catalog.yaml`、统一字段语义表、机制文档和回归测试。原因：开仓 episode 必须把盈利/亏损结果同时反写到 `trigger_quality_verdict`、`trigger_confirmation_adjustment`、`trigger_quality_positive_signal` 和 `net_trigger_quality_loss_signal`；PM 下一轮用 `trigger_quality_positive_bonus` 放大被验证有效的同类 trigger，用净触发亏损信号降低失效 trigger 的真实资金部署优先级。本次不新增第二套 rank，不让 Trader 读学习或放宽触发，不用单向限制压低交易频率。

（7）同步分析师提示词中的产品级学习安全边界。修改：`prompt.py` 和提示词回归测试。原因：`product_learning_calibration_view` 已由代码生成并注入 `learning_context`，三类分析师 prompt 需要显式说明只能把产品级历史表现用于证据质量、确认需求、setup 分类和待验证问题校准，不能生成 rank、资金部署、手数或交易权限；PM 仍是唯一把校准后证据转成排名和资金部署的智能体。

（8）瘦身主配置中文注释。修改：`dev.yaml` 注释，不改任何参数值。原因：主配置只保留运行入口、catalog 索引、资金红线、交易宇宙和 LLM 路由的重点说明；机制解释继续放在机制文档和统一字段语义表，产品级学习写入、分析师安全视图、唯一 rank、真实资金部署、入场/触发质量校准继续由 catalog、代码机制和数据库学习承载。

（9）修复空回测库下的已知日期回归测试口径。修改：`test_system_invariant_audit.py`、`test_mechanism_effectiveness_audit.py`。原因：当前已清理全部回测记录准备干净重跑，旧的“已知日期不崩溃”测试仍强制要求真实库存在历史 `config_id`，导致回测前总门误阻断；本次只让测试识别 `config_not_found_empty_db` 空库边界，有真实配置记录时仍要求 `config_id`，不放宽真实回测日 hard fail。

（10）收口唯一资金优先级 rank 与资金层级解耦。修改：`pm_opportunity_ranking.py`、workflow 资金部署、PG 机制诊断、统一字段语义表、机制文档和回归测试。原因：系统仍只保留一个 `opportunity_rank`，`rank=1` 固定表示当前最值得占用资金；当所有候选都是 `watch_for_trigger` 时，rank 按证据质量、触发完整度、失效边界、冲突程度、产品级学习、trigger 历史表现和资金效率排出最值得用既有 `0.008` 小探针资金试的候选，不自动升仓；`tradeable_candidate` 与反复验证 alpha 候选通过同一 rank 映射到真实资金或放大资金层级，并把 `rank_capital_role/capital_layer/capital_ratio_source/rank_reason` 写入最终合约资金部署。本次不新增第二套 rank，不改仓位参数，不硬编码品种好坏，不改 Trader/Accountant/Researcher 边界。

（11）收口产品级学习到唯一资金 rank 合约闭环。修改：`final_action_semantics.py`、`pg_system_invariants.py`、`pg_contract_coverage_audit.py`、`analyst_signal_fusion.py`、`pm_contract_builder.py`、统一字段语义表和回归测试。原因：凡最终合约写入 `opportunity_rank`，必须同步写入 `rank_capital_role/capital_layer/capital_ratio_source/rank_reason` 并由 PG 与契约覆盖硬检查；PM 必须能消费顶层 `entry_quality_outcome` 形成入场/触发质量分项，并把真实资金释放或未释放诊断写入最终合约证据，防止产品级学习、唯一 rank 和资金部署只停留在诊断字段。

==========2026年07月04日==========

（1）收口 PM/workflow 最终合约统一落盘出口。修改：`final_action_semantics.py`、`workflow.py` 和回归测试。原因：2025-03-21 暴露出两个出口漂移：空仓无目标的最终合约仍可能保留 `final_action=hold`，以及条件候选/full-market capital queue 分支可能只把 rank 资金语义写入 `evidence_used`，没有同步写入 `capital_deployment`。本次新增最终合约持久化前规范化入口，统一把 flat/no position 合约写成 `wait`，并把 ranked 合约的 `rank_capital_role/capital_layer/capital_ratio_source/rank_reason` 同步到 `evidence_used` 与 `capital_deployment`；不改策略参数、不新增第二套 rank、不降低 PG hard fail、不改 Trader/Accountant/Researcher 边界。

==========当前验证口径==========

（1）回测前总门：`src/run/pre_backtest_test.py`。

（2）每日回测后总门：`src/run/backtest_daily_test.py`。

（3）结构测试重点：事实入口、合约解析、artifact 边界、结算公式、研究写入、控制组只读、PM 状态转换、分析师输出落地。

（4）回测前验收只检查系统可运行性、字段/schema/权限/硬数据/边界和确定性转换规则，不评价策略收益。
