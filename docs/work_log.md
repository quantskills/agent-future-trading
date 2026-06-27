# AgentQuant 工作日志

本文件是基于当前系统状态重整后的短版开发索引。保留按天划分的结构，只记录仍能解释现有代码、配置、字段、智能体边界和回测前验收的修改。已被后续重构覆盖的中间补丁、旧字段口径、旧工具名、旧运行入口和纯讨论内容不再保留。

字段语义以 `docs/unified_field_semantics.md` 为准；智能体工作流、权限边界、事实入口和 artifact 边界以 `docs/mechanism_multiagents.md` 为准；数据载体与 DB schema 口径以 `docs/mechanism_data_model.md` 为准。

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

==========当前验证口径==========

（1）回测前总门：`src/run/pre_backtest_test.py`。

（2）每日回测后总门：`src/run/backtest_daily_test.py`。

（3）结构测试重点：事实入口、合约解析、artifact 边界、结算公式、研究写入、控制组只读。

（4）回测前验收只检查系统可运行性、字段/schema/权限/硬数据/边界，不评价策略收益。
