# AgentQuant 工作日志

本文件是基于 2026-06-26 当前系统状态重整后的短版开发索引，只记录仍能解释现有系统结构、字段、运行链路和验收边界的 `.py`、`.yaml`、`.yml` 行为修改。

已被后续改造完全覆盖的中间修法、旧字段口径、旧工具名、旧提示词入口和纯讨论内容不再保留。字段名称以 `docs/unified_field_semantics.md` 为准；智能体、工具和运行边界以 `docs/mechanism_multiagents.md` 为准。

每条只保留：

- 修改了什么：文件、模块或机制。
- 为什么改：对应的问题。

==========2026年06月08日========

（1）建立真实配置展开与资金目录回归基础。
修改了什么：`src/util/config_normalizer.py`、`src/config/dev.yaml`、portfolio/learning catalog、`src/tests/test_phase_flow_regression.py`。
为什么改：让主配置只承担运行入口职责，策略参数、资金边界和学习策略由 catalog 管理，并用测试确认展开后的运行字段稳定。

（2）收束投资组合经理最终推荐出口。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/agents/execution_team/trader.py`、`src/tools/agent_tools/decision/reason_effects.py`、相关回归测试。
为什么改：防止“不交易、无触发、只观察”等语义被最小手数、probe 或自然语言推荐推成真实开仓；所有策略动作必须落到结构化最终出口。

==========2026年06月09日========

（1）贯通分析师结构化证据进入投资组合经理。
修改了什么：分析师证据质量工具、公共合约工具、投资组合经理消费路径和回归测试。
为什么改：让分析师输出从方向文本升级为 PM 可读的 `action_evidence_contract`，避免下游把 raw signal 或自由文本当交易权限。

（2）补齐学习闭环进入 PM 候选与交易出口的测试基础。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、学习/scorecard 相关测试。
为什么改：证明历史学习能被 PM 看见并影响候选判断，同时弱方向、无当前证据和观察机会不能绕过最终合约。

==========2026年06月10日========

（1）收束软门控和持仓生命周期保护。
修改了什么：PM 持仓生命周期逻辑、reason-effect 分类、资金/风控策略配置和回归测试。
为什么改：避免多层软限制重复压死交易；软风险只降级、缩手数或要求确认，硬风险才阻断，同时让浮盈保护、浮亏复验失败和退出学习能落到减仓/退出。

（2）打通 action-value 学习服务 PM 评分和执行 profile 的早期路径。
修改了什么：研究 action-value 写入、数据库读取、PM 学习消费、执行 profile 和相关测试。
为什么改：让 open/hold/exit/execution 分动作学习能进入后续 PM 决策，但不能让执行学习变成交易员直接权限。

==========2026年06月11日========

（1）收紧研究学习的时间边界。
修改了什么：数据库 schema/helper、研究写入、PM 历史读取、学习路径测试。
为什么改：保证所有学习记录满足 `source_trading_date < decision_date`，防止 Phase4、同日或未来学习污染当日 Phase1 决策。

（2）收紧 alpha setup/action-value 的真实放大资格。
修改了什么：`alpha_setup`、数据库读取、投资组合经理学习使用和回归测试。
为什么改：区分真实同作用域 action-value、弱先验、相似样本和观察样本，防止弱先验被误当成真实 alpha 放大依据。

==========2026年06月12日========

（1）把研究信息落到分析师证据校准。
修改了什么：`analyst_learning_calibration`、三类分析师、技术参数校准和测试。
为什么改：让研究员输出的分析师校准类结构化研究能进入技术面、基本面、新闻面证据质量，而不是只停留在解释文本。

（2）增强 PandaAI 数据调用稳定性。
修改了什么：`src/apis/pandaai/api.py`、`src/tests/test_pandaai_api_adapter.py`。
为什么改：处理 SDK auth 路径、token 过期、合约代码匹配和不可用字段重复请求，减少环境/API 问题干扰策略链路。

（3）把 action-value 升级为可审计动作偏好。
修改了什么：`alpha_setup`、投资组合经理学习使用、reason-effect 和研究测试。
为什么改：固定 open/hold/exit/execution 分账，避免历史持有收益、退出收益和执行质量跨动作线误用。

==========2026年06月13日========

（1）建立协议管理员和控制组侧车。
修改了什么：`src/agents/control_team/protocol_governor.py`、`src/tools/agent_tools/control/*`、`src/run/control/protocol_preflight.py`、控制组测试。
为什么改：把智能体边界、工具权限、artifact、环境 preflight 和成本预算做成旁路治理，不让控制组产生交易动作。

（2）对齐控制组能力卡、工具权限和提示词边界。
修改了什么：`agent_cards`、`tool_access_policy`、`src/config/dev.yaml`、`src/llm/prompt.py`、协议治理测试。
为什么改：让配置、提示词和控制组审计表达同一套智能体权限，避免工具越权或提示词旧口径回流。

==========2026年06月14日========

（1）把 action-value reward 收束到完整交易 episode 优先口径。
修改了什么：`alpha_setup`、研究学习、preflight、研究测试和 CLI 测试。
为什么改：避免只看单日 PnL 导致 open/hold/exit/execution 归因错位，并让 preflight 真实失败能被命令行暴露。

（2）新增交易流水审计镜像和运行期系统不变量审计。
修改了什么：交易员、`src/util/futures_audit.py`、`system_invariants.py`、`src/run/control/system_invariant_audit.py`、`src/run/backtest.py`、系统审计测试。
为什么改：让每笔成交可回溯到 PM 最终合约，并让回测中出现的非策略 bug 当日 fail-fast，而不是混进收益分析。

（3）整理运行入口目录。
修改了什么：`src/run/control/*`、`src/run/research/*` 和相关测试。
为什么改：区分主回测、控制审计和研究入口，避免脚本职责混乱。

==========2026年06月15日========

（1）固定回测前验收入口。
修改了什么：`pre_backtest_acceptance.py`、`protocol_preflight.py`、`backtest.py`、preflight/acceptance 测试。
为什么改：把环境、配置、时间边界、智能体边界、结构化 IO、唯一合约、执行一致性、学习落地和资金边界从人工检查变成回测前控制组闸门。

（2）接入 LLM 鉴权与 provider 配置一致性检查。
修改了什么：LLM provider/inference、`dev.yaml`、preflight 和 acceptance 测试。
为什么改：保留扩展 provider 能力，但当前运行配置只允许已声明 provider/model；无效 token 或 provider 漂移不能到 Phase1 才暴露。

（3）把 action-value 放大资格和落仓审计接入系统不变量。
修改了什么：`system_invariants.py`、action-value 写入、系统审计测试。
为什么改：真实 action-value 才能支持放大；弱先验可存在但不得伪装成动作偏好，也不得被误判为必须落仓的真实 alpha。

==========2026年06月16日========

（1）收束 `final_action_contract` 为策略交易唯一执行契约。
修改了什么：投资组合经理、交易员、执行工具、公共审计工具和相关测试。
为什么改：策略成交只能来自审计通过的 `final_action_contract`；推荐顶层展示字段、草稿计划、score/rank 和自然语言说明都不能成为第二交易权限。

（2）统一 action-value canonical 字段。
修改了什么：数据库 schema/helper、`alpha_setup`、研究写入、系统不变量和测试。
为什么改：固定 `action_preference/reward_source/evidence_scope/action_value_lane/consumer_scope/learning_lane/retrieval_key` 等字段为机器消费语义，payload 只作兼容容器。

（3）明确研究成果的消费边界。
修改了什么：研究工具、分析师校准路径、PM 学习读取、交易员执行字段和提示词/配置说明。
为什么改：分析师只消费本专业校准类研究，PM 只消费交易决策类研究，交易员不能读取研究库、action-value、`strategy_memory` 或 `adaptive_policy_state` 下单。

==========2026年06月17日========

（1）新增分析师机会状态与证据契约。
修改了什么：`graph/schema.py`、分析师质量工具、集中提示词、投资组合经理消费路径、分析师/阶段流测试。
为什么改：把机会状态统一为 `opportunity_state`，把触发、失效、证据质量和学习影响落进 `action_evidence_contract`，避免分析师输出只剩方向。

（2）切断旧草稿计划和旧推荐字段的下游兜底。
修改了什么：PM、Trader、Reviewer/Researcher 事实提取、系统不变量和回归测试。
为什么改：下游必须读取 `final_action_contract` 的 `current_lots/target_lots/lots_delta/final_action`，不能从旧顶层字段或草稿计划恢复交易事实。

（3）完成运行时字段语义漂移收口。
修改了什么：`docs/unified_field_semantics.md`、字段迁移测试、运行时生产/消费路径和提示词/配置注释。
为什么改：旧字段只允许在迁移脚本和迁移测试中出现，生产运行必须使用统一字段语义。

==========2026年06月18日========

（1）新增 PM 释放阻塞诊断和学习使用诊断。
修改了什么：投资组合经理、研究反馈、策略归因、测试。
为什么改：把资金释放、学习使用和未落仓原因写成可审计诊断，但不把诊断变成第二交易权限。

（2）切断复盘/研究对旧推荐字段的仓位兜底。
修改了什么：复盘归因、研究 action-value 写入、SQL similar prior、归因报告和测试。
为什么改：复盘与研究必须从 `final_action_contract` 和真实执行/结算事实推导动作分账，不能从旧 `action/lots/target_position_ratio` 补算。

==========2026年06月19日========

（1）统一分析师触发字段和文本触发说明。
修改了什么：分析师证据归一化、主配置注释、字段审计和相关测试。
为什么改：`entry_trigger`、`trigger_valid`、`current_trigger_confirmed`、`setup_quality_ok` 必须表达同一语义，等待确认文字不能与当前触发字段自相矛盾。

（2）把统一字段语义检查接入回测前和每日审计。
修改了什么：统一字段审计、`pre_backtest_acceptance`、`system_invariant_audit`、配置/提示词/测试。
为什么改：防止旧字段或旧注释重新驱动生产路径。

==========2026年06月20日========

（1）拆开 `setup_quality_ok` 与当前触发语义并补齐条件监控闭环。
修改了什么：分析师证据、PM 条件机会分流、Trader 盘中触发、系统不变量、机制测试和配置目录。
为什么改：`setup_quality_ok=true` 只表示形态值得关注；`trigger_valid/current_trigger_confirmed` 才表示当前触发成立。等待触发的干净机会只能进入同一张合约的条件监控路径。

（2）接通非策略运营单的执行与审计隔离。
修改了什么：换月、强平/强减、盘中保证金风险触发、交易员执行、归因和系统审计。
为什么改：`source_type=rollover/forced_risk` 必须独立执行和核算，不能污染策略 `final_action_contract`、策略胜率或 alpha action-value。

（3）锁死 Trader raw action/lots 旁路并补齐执行学习上下文。
修改了什么：交易员、执行工具、执行学习 trace、系统不变量和测试。
为什么改：策略单不能用 recommendation 顶层 `action/lots` 绕过最终合约；执行学习只能作为未来研究输入。

（4）把自适应学习安全过滤接入分析和决策。
修改了什么：分析师权重、资金读取、上下文校准 safety、PM 条件机会判断和测试。
为什么改：候选/观察类学习只能作为先验或受控 probe 线索，不能直接放大仓位或绕过唯一合约。

==========2026年06月21日========

（1）把 PM 收成统一证据分流器。
修改了什么：PM 机会分流、回测前验收、每日系统审计和测试。
为什么改：同一机会必须被分流为当前可交易、条件监控或明确不可交易原因，不能在 PM 中间层静默变成无意义 wait。

（2）打通全市场机会评分、排序和资金部署解释。
修改了什么：PM、workflow、排序/资金部署字段、归因学习、系统审计和测试。
为什么改：让候选之间可比较，`opportunity_scorecard/opportunity_rank/capital_allocation_reason` 只解释资金优先级，不生成交易权限；最终仓位仍由唯一合约承载。

（3）锁定推荐顶层展示字段与唯一合约一致。
修改了什么：PM DB 更新、系统不变量和阶段流测试。
为什么改：防止“最终合约已更新，但推荐顶层 action/lots 未同步”的非策略风险。

==========2026年06月22日========

（1）把完整 episode/action-value 学习接入 PM 评分分项。
修改了什么：PM 学习评分、learning catalog、系统不变量、归因报告和测试。
为什么改：正向 alpha 要支持“probe 验证 -> rank 晋升 -> 合规放大 -> 盈利持有/加仓 -> 失效退出”的完整周期，近期 tail loss 可抵消旧正向学习。

（2）把 PM 学习/排名边界纳入回测前和每日检测。
修改了什么：`pre_backtest_acceptance`、`system_invariant_audit`、归因输出和图表。
为什么改：确认学习分项、排名分项、资金部署和唯一合约之间真实接通；资金利用率低或学习无效必须能被诊断。

==========2026年06月23日========

（1）修通研究员 action-value 到 PM 的传输断链。
修改了什么：PM 学习读取、action-value canonical 读取、研究写入、阶段流测试。
为什么改：真实 action-value 必须保留 `id/action_preference/reward_source/evidence_scope/action_value_lane/reward` 等字段，不能被空壳 trace 或弱先验覆盖。

（2）把“学习/排名存在但未影响合约且无解释”列为非策略断链。
修改了什么：`system_invariants.py`、`mechanism_effectiveness_audit.py`、阶段流测试和系统审计测试。
为什么改：学习或排名如果没有改变仓位，也必须写出资金队列、风险、未入选、已达目标、冷却/最短持有等明确原因；否则属于机制断链。

（3）新增只读机制有效性审计。
修改了什么：`mechanism_effectiveness_audit.py`、`system_invariants.py`、`backtest.py`、审计测试和字段语义表。
为什么改：在系统不变量 clean 后，继续检查 `action-value -> PM -> score -> rank -> final_action_contract -> Trader/Accountant -> Reviewer/Researcher` 是否真实闭环；hard fail 阻止收益评价，diagnostic 进入策略分析。

（4）收敛执行学习 trace 与版本级契约覆盖闸门。
修改了什么：`src/util/futures_audit.py`、交易员、执行工具、研究读取、`contract_coverage_audit.py`、`pre_backtest_acceptance.py`、`backtest.py` 和测试。
为什么改：所有执行学习 trace 固定带 `consumer_scope=trader_execution_learning`、`learning_lane=execution` 和 `execution_retrieval_key`；版本级闸门检查 producer、consumer、audit、test、字段表、配置、提示词和机制文档是否对齐。

==========2026年06月24日========

（1）把机制有效性审计改为交易生命周期场景审计。
修改了什么：`mechanism_effectiveness_audit.py`、`system_invariants.py`、机制审计测试、系统审计测试和机制文档。
为什么改：开仓/加仓、条件监控、持仓、减仓/退出、未入选候选的审计口径不同；不能用开仓 rank/score 规则误杀正确减仓或退出。

（2）补齐关键跨智能体边界保真测试。
修改了什么：`portfolio_manager.py`、`contract_coverage_audit.py`、`test_phase_flow_regression.py`。
为什么改：锁住分析师证据进入 PM、研究员 action-value 进入 PM、PM 合约进入 Trader、Trader 执行结果进入 Reviewer/Researcher 的字段保真，避免 producer-to-consumer 语义漂移。

==========2026年06月25日========

（1）修正 PM 记忆读取中“空历史挡住真历史”的断点。
修改了什么：PM 记忆读取、`decision_memory_retrieval`、阶段流测试和契约覆盖矩阵。
为什么改：PM 必须先收集所有可见历史，再按质量排序；空历史只能作为背景，不能占位挡住真实盈利或真实亏损 action-value。

（2）按固定工作流拆出 `signal_collector` 并让 PM 退出 LLM 调用。
修改了什么：`src/agents/decision_team/signal_collector.py`、`signal_evidence_collection.py`、`decision_memory_retrieval.py`、`opportunity_ranking.py`、`position_sizing.py`、PM、workflow、schema、prompt、能力卡、工具权限、契约覆盖、测试和机制文档。
为什么改：当前链路固定为“分析师结构化预测证据 -> 信号收集员统一证据包 -> PM 工具读取记忆/排序/算手数 -> PM 签唯一合约”。PM 不再调用 LLM，不直接读研究 DB，不自己解释原始研究记录。

（3）收干净复盘员、研究员、审计员和交易员边界。
修改了什么：`phase4_review.py`、`researcher_learning.py`、`research_learning.py`、`research_memory_writers.py`、`auditor.py`、`trader.py`、执行工具、配置、系统不变量和协议测试。
为什么改：复盘员只复盘、不调用 LLM、不触发研究学习；研究员单独输出结构化研究；审计员不直接消费研究记忆；交易员只执行 PM 合约和合约化触发规则，不读研究库、action-value、`strategy_memory` 或 `adaptive_policy_state`。

（4）整理工具命名和公共 helper 边界。
修改了什么：`src/tools/agent_tools/decision/*`、`src/tools/agent_tools/execution/*`、`src/tools/agent_tools/research/*`、`src/tools/common/contracts.py`、`src/tools/common/runtime_setup.py`、相关测试。
为什么改：工具按具体功能命名，不按智能体命名；跨智能体公共基础能力放入 `src/tools/common`，避免业务工具和公共 helper 混在一起。

==========2026年06月26日========

（1）切断 Phase4 completed 自动刷新研究记忆的旧副作用。
修改了什么：`src/database/sqlite_helper.py`、`src/database/interface.py`、`phase4_review.py`、`researcher_learning.py`、`research_memory_writers.py`、协议测试和机制文档。
为什么改：Phase4 标记 completed 只表示复盘验收通过，只写阶段状态；不能刷新 `strategy_memory`、执行学习 retention 清理或写任何研究状态。研究写入只能由研究员入口和研究工具承担。

（2）把 template prior 冷启动加载从 Phase1 拆成显式研究初始化。
修改了什么：`src/run/proposal.py`、`src/run/research/load_template_prior.py`、`template_prior.py`、`learning_policy_catalog.yaml`、测试和机制文档。
为什么改：`template_prior` 是冷启动研究种子，不属于 Phase1 策略生成；`proposal.py` 不再自动写研究记忆，必须通过 `load_template_prior.py` 显式初始化。

（3）收干净 phase completion 旧 API 与研究快照归属。
修改了什么：`src/database/interface.py`、`src/database/sqlite_helper.py`、`phase4_review.py`、`research_snapshot_reports.py`、`researcher_learning.py`、协议测试和工作流文档。
为什么改：删除 `complete_trading_day_phase` 的旧学习参数，避免 API 形状继续暗示阶段完成可以触发学习；只读历史学习快照报告迁到研究报告模块，复盘模块只保留 Phase4 验收和交易日志职责。
