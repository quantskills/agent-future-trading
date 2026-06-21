# AgentQuant 数据与模型调用机制

更新时间：2026-06-21

本文档记录 AgentQuant 当前已经代码落地的数据调用、模型调用、缓存加速、数据质量摘要与回测验收要求。旧的 DataYes 接口已经退出系统；当前运行数据源为 PandaAI、Finoview 本地 feather 和本地新闻 txt。PandaAI/Finoview/新闻只提供当日可见证据，不能直接生成交易权限。

## 一、数据与模型调用原则

### 1. 数据调用原则

1. **只保留当前三类运行数据入口**：PandaAI 提供期货行情、分钟线、结算相关行情和期货衍生数据；Finoview 提供本地 feather 基本面数据；本地新闻 txt 提供新闻面证据。
2. **严禁未来信息污染当日决策**：Phase1 盘前策略只能读取 T-1 及以前可见信息；Phase2 只能读取当时已经发生的 T 日盘中数据；Phase3 才能读取当日官方结算数据；Phase4 复盘结果只能写给未来交易日使用。
3. **缓存不能改变数据可见性**：PandaAI、Finoview feather、新闻 txt 的共享缓存只减少重复读取或重复 API 调用，不能扩大时间窗口，不能把未来日期数据提前交给分析师或 PM。
4. **数据缺口必须显式记录**：PandaAI 衍生数据、Finoview 基本面、新闻数据是否可用、是否滞后、是否进入信号，都要进入结构化数据质量摘要。
5. **缺失不能伪造成方向证据**：少量可选缺口可以降级继续分析；关键缺口过多时只能降级为观察、等待触发或 Neutral，不能把“没数据”当成 Bullish/Bearish。
6. **学习必须记住数据依据**：交易记忆和未交易机会记忆不仅记录结论与盈亏，也要记录当时用了哪些数据源、哪些字段、数据是否可用、是否滞后，以及这些依据如何进入信号。

### 2. 模型调用原则

1. **LLM 只负责结构化理解与研究总结**：当前启用链路里，Technical、Fundamental、Commodity News、PM、Researcher 可以按配置调用 LLM；硬风控、成交、结算、账务、完整交易日志和 Phase4 验收不能由 LLM 最终裁决。Planner 当前 `planner_mode=false`，不进入本轮回测调用链。
2. **Reviewer 不直接调用 LLM**：Reviewer 只做确定性验收、账务一致性检查、交易流水检查和完整交易日志输出；Researcher 才负责 Phase4 后的 LLM 研究与学习写入。
3. **避免重复调用以加快回测**：Phase1 支持多品种分析并行、LLM 并发门、学习上下文缓存、PandaAI/Finoview/新闻预取和共享缓存；这些优化只减少工程耗时，不改变策略逻辑。
4. **PM 决策仍按组合顺序串行**：分析师读数和 LLM 分析可以并行，但 PM、Trader、Accountant、Reviewer/Researcher 仍按交易日和品种顺序执行，避免资金状态和学习状态串扰。
5. **模型路径必须可审计**：分析师与 PM 的输出保留 `llm_path`、模型配置审计信息、结构化信号、数据依据和 artifact 指针，便于复盘模型是否按配置调用。

### 3. 结构化输出与交易权限边界

结构化输出不是为了限制 LLM 推理，而是为了统一审计和交易出口。LLM 可以在分析报告、reasoning notes、raw rationale 中充分表达推理；但能进入交易链路的内容必须落到结构化字段。当前系统的最终交易真相只有一张 PM 输出并经审计的 `final_action_contract`，Trader 执行结果和会计结算结果都必须能回到这张合约；`active_opportunity_audit` 只解释候选、阻断和条件监控，不生成第二套交易事实。

PM 的全市场机会排序是资金部署输入，但不是第二套交易权限。`opportunity_score/opportunity_score_components/opportunity_rank/capital_allocation_reason/learning_adjustment_summary` 用来比较候选、部署资金和解释学习影响；它们可以进入 scorecard、`final_action_contract.evidence_used/learning_used`、Reviewer/Researcher 和评估模块，并且只能通过 PM 资金部署 pass 回写同一张 `final_action_contract.target_lots/lots_delta/final_action`，不能成为顶层交易命令。

分析师输出的 `Bullish/Bearish/Neutral` 只是方向摘要，不是开仓投票。Technical/Fundamental/Commodity News 必须分别输出动作证据、品种/品类上下文、触发、失效边界、support/conflict/catalyst/risk 等结构化字段。`setup_quality_ok` 只表示形态值得关注，`trigger_valid/current_trigger_confirmed` 才表示当前触发已经成立；`watch_for_trigger + trigger_valid=false` 不是开仓授权，但如果同时有明确方向、触发条件、失效边界和可关注 setup，PM 可以把它纳入条件监控候选。分析师读取历史 action-value 时只能使用 `signal_calibration` 校准证据质量，不能生成交易授权，也不能输出 `opportunity_score/opportunity_rank/capital_allocation_reason`。PM 按 open/hold/exit 读取当前证据和匹配 action lane 的 action-value，并负责全市场候选评分与资金部署；execution action-value 只能由 PM 消化进 `final_action_contract.execution_plan/execution_profile`。最终交易出口仍由 market confirmation、资金参数、Auditor 硬风险、Trader 执行约束和顶层 `final_action_contract` 共同决定。

## 二、当前启用智能体的数据与模型调用方式

本节只列 `src/config/dev.yaml` 当前启用的运行角色。三位启用分析师来自 `workflow_analysts: commodity_news, fundamental, technical`；Planner、macroeconomic、policy 等停用或退役角色不参与当前回测调用链，不在这里展开。

| 启用角色 | 主要调用/读取 | 是否调用 LLM | 主要写出 | 边界 |
| --- | --- | --- | --- | --- |
| `technical` | PandaAI 盘前可见行情、技术指标、技术参数校准、历史技术学习上下文 | 是 | `AnalystSignalArtifact`、`action_evidence_contract`、`trade_research_contract` | 只给技术证据，不给仓位/保证金/交易命令 |
| `fundamental` | Finoview 本地 feather、PandaAI 衍生因子、基本面数据质量、历史基本面学习上下文 | 是 | `AnalystSignalArtifact`、`action_evidence_contract`、`trade_research_contract` | 只给基本面证据和触发/失效边界，不给交易授权 |
| `commodity_news` | 本地新闻 txt、新闻事件上下文、新闻新鲜度与影响窗口、历史新闻学习上下文 | 是 | `AnalystSignalArtifact`、`action_evidence_contract`、`trade_research_contract` | 只给催化证据，不能把新闻直接变成开仓 |
| `portfolio_manager` | 三位分析师结构化证据、账户/持仓/资金、action-value、memory quality、market confirmation、Auditor 结果 | 是 | `PMDecisionArtifact`、全市场机会 scorecard、唯一 `final_action_contract` | 只有 PM 可以生成策略交易意图和目标手数；负责机会排序与资金部署，但必须经 Auditor 与资金/风险修正 |
| `auditor` | `final_action_contract`、风险状态、合约状态、数据质量与硬风险配置 | 否 | `AuditVerdictArtifact`、`audit_verdict`、`hard_risk_reasons` | 只审合约，不新造方向/手数，不替 PM 生成第二张合约 |
| `trader` | 审过的 `final_action_contract`、`audit_verdict`、盘中行情、账户保证金状态、运营单 | 否 | 成交/未成交记录、执行原因、`forced_risk_operational_recommendation` | 只执行合约和运营风控单，不能自己创造策略交易 |
| `accountant` | 成交、持仓、结算价、手续费/滑点/保证金规则 | 否 | 结算、PnL、费用、保证金、持仓状态 | 只核算事实，不改交易意图，不写学习结论 |
| `reviewer` | 推荐、成交、结算、阶段状态、交易日志所需事实、PM 排序与资金分配理由 | 否 | Phase4 验收、daily summary、完整交易日志、排序有效性复盘、学习候选 | 只做确定性复盘验收，不下单、不调仓、不直接写最终策略学习 |
| `researcher` | Reviewer 产物、已结算 episode、action outcome、未交易/未触发机会、排序有效性复盘 | 是 | `alpha_setup_profile`、`alpha_setup_action_value`、`adaptive_policy_state`、排序偏好候选 | 只写未来可用学习和排序偏好候选，不影响当天交易和账务 |
| `protocol_governor` | 能力卡、工具权限、任务生命周期、artifact lineage、preflight/audit 状态 | 否 | `protocol_audit`、`preflight_health`、工具权限/字段语义告警 | 只做旁路治理，不创建/否决交易权限，不改 lots/margin |

### 1. 工作流、预取与共享缓存（运行底座，不是智能体）

`src/graph/workflow.py` 是 Phase1 策略生成主链路。每个交易日会先按当日可见性边界预取和缓存数据：PandaAI 日线、分钟线、结算相关行情和衍生字段进入共享缓存；Finoview feather 与本地新闻 txt 通过进程内缓存读取；不可用字段会写入数据质量摘要，而不是反复调用或伪造成方向证据。预取失败只产生 warning，不改变交易语义。

并行分析只发生在不会改变业务状态的环节。分析师输出由工作流集中保存，signal 表按组合、交易日、品种、分析师保留最终一条，避免重复信号污染后续统计、学习和评估。

当前 Phase1 还会在 PM 决策前做分析师输出完整性验收：technical、fundamental、commodity_news 必须各自生成一条结构化 `AnalystSignal`。技术面核心行情、合约元数据或 PM 风险评估异常属于真实系统错误，应直接暴露并停止当日流程；本地 Finoview 基本面或本地新闻真实缺口可以降级，但必须生成机器可读 no-trade 数据缺口信号，不能空返回，也不能伪造成 Bullish/Bearish。

### 2. Technical Analyst

Technical 调用 PandaAI 盘前可见行情和技术分析工具，计算趋势、波动率、成交量、支撑阻力、ATR、RSI、布林、均线等技术证据，并读取技术参数情境校准与历史技术学习上下文。它可调用 LLM 生成结构化技术信号，写出 `data_usage_summary`、`adaptive_params`、`technical_parameter_calibration`、`action_evidence_contract` 和 `trade_research_contract`。它不能输出手数、保证金或最终开平仓命令；`setup_quality_ok` 只表示形态值得关注，`trigger_valid/current_trigger_confirmed` 才表示当前触发成立。技术面核心行情为空、缺少 close 或读取异常时，不允许返回空信号或 HOLD 兜底，应直接暴露错误。

### 3. Fundamental Analyst

Fundamental 调用 Finoview 本地基本面 feather、PandaAI 衍生字段和基本面学习上下文。Finoview 数据会按交易日快照、覆盖率、新鲜度、缺口和低置信度诊断进入 prompt；PandaAI 衍生字段会记录 reference date、feature status、record counts、权限不足和缺口状态。它可调用 LLM 生成结构化基本面信号，写出主驱动、供需、基差、库存、数据新鲜度、触发条件、失效边界和 `action_evidence_contract`。中期/长期基本面观点必须说明短线触发、失效边界和持仓周期，否则只能形成方向观点或观察线索。若本地基本面真实无可用数据，Fundamental 必须输出 `deterministic_data_gap_no_trade` 信号和 metadata；若读取链路异常，则直接报错，不用空返回掩盖。

### 4. Commodity News Analyst

Commodity News 调用本地 `data/News_data/Future_news/<ticker>.txt` 和新闻事件学习上下文，按当日可见窗口过滤，不读取未来新闻。它记录新闻文件、解析数量、最终使用新闻数、新闻截止规则、事件方向、强度、新鲜度、相关性、影响窗口和可交易性。新闻面可调用 LLM，但输出要区分真实催化、方向性消息和噪声；缺少强催化或价格反应时只进入 `direction_context`、`opportunity_state=no_opportunity/watch_for_trigger` 或等待触发。若本地新闻真实无可用内容，Commodity News 必须输出 `deterministic_no_news_no_trade` 信号和 metadata；若新闻读取链路异常，则直接报错，不用空返回掩盖。

### 5. Portfolio Manager

PM 调用三位分析师结构化信号、交易研究契约、学习上下文、PandaAI market confirmation、账户持仓、资金、风险状态、数据质量摘要和 Auditor 结果。PM 的 recommendation snapshot 会写入每位分析师的 `data_usage_summary`、market confirmation feature status、每日 `data_quality_summary` 路径、`llm_path`、机会层级、setup 质量、`learning_to_position_trace`、仓位质量控制和 PM 诊断。`opportunity_scorecard` 必须稳定携带 `setup_quality_ok`、`trigger_valid`、`current_trigger_confirmed`、`invalidation_present`、`entry_trigger`、`opportunity_state` 和 `source_analysts`，并写入 PM 计算的 `opportunity_score/opportunity_score_components/opportunity_rank/capital_allocation_reason/learning_adjustment_summary`。干净的 `watch_for_trigger` 条件机会只进入 `conditional_monitor_candidates` 供 PM 判断是否生成同一张 `final_action_contract` 的条件 probe 权限，不能直接变成开仓，也不能被当成普通 wait 丢失。PM 可以调用 LLM 形成初步判断，但仓位目标必须经过数据质量、setup 质量、机会层级、全市场排序、风控和 Auditor 修正。

每日回测或模拟盘会输出：

```text
src/logs/data_quality/<交易日>.json
```

干净回测后，每个完成的交易日都应对应一份 data quality 文件与一份完整交易日志。若用户已经手动删除全部回测记录，旧主库交易日数量不再代表当前事实；下一轮应重新检查 data quality、推荐快照、signal、交易日志和学习记忆是否逐日生成并互相对齐。

### 6. Auditor

Auditor 不调用 LLM，不直接拉取原始数据。它调用决策审计与执行约束工具，读取 PM 输出的唯一 `final_action_contract`、分析师证据摘要、market confirmation、数据质量、策略记忆、自适应策略状态、合约状态和风控配置，形成确定性审核。审计通过只表示这张合约可以交给 Trader；审计不生成第二张合约，不新造方向/手数。缺失数据、候选记忆、方向观点或 shadow 结果都不能单独成为交易授权。

### 7. Trader 与 Accountant

Trader 调用执行侧工具、盘中 PandaAI 行情、持仓和账户保证金状态，不调用 LLM 创造策略。Trader 只读取审计后的 `final_action_contract`：普通开仓必须有当前触发；条件 probe 必须带 `conditional_trigger_authority=true`、`requires_intraday_confirmation=true`、`can_execute_without_intraday_trigger=false`，Trader 只能盘中检查合约里的触发条件，触发才按合约方向和手数成交，未触发只记录原因。Trader 可以把成交/未成交事实与 PM 排序诊断一并写回供复盘，但不能按 `opportunity_score/opportunity_rank` 改手数或方向。换月和强平/强减属于 `source_type=rollover/forced_risk` 的运营风控单，Trader 可以执行，但它们独立核算，不污染策略 alpha 学习。

Accountant 调用成交记录、官方结算价、合约乘数、手续费、滑点和保证金规则做 Phase3 结算，不调用 LLM，也不被学习文本改账。它写出每日 PnL、费用、持仓、释放/占用保证金和账户权益；若成交、结算价或保证金对不上，应停止而不是兜底改账。

### 8. Reviewer

Reviewer 调用研究侧确定性复盘工具和数据库事实，读取推荐、成交、结算、阶段状态、PM 评分/排名、资金分配理由和执行日志，负责 Phase4 验收、daily summary、排序有效性复盘和完整交易日志，不直接调用 LLM。Reviewer 可以写学习候选和归因事实，但不下单、不调仓、不改账，也不把未完成交易日当作策略结论。

### 9. Researcher

Researcher 调用研究工具、Reviewer 产物、已结算 episode、未交易机会、未触发条件机会、PM 排序有效性复盘和 action outcome；可按配置调用 LLM 做 causal review 与探索性研究。Researcher 在 Reviewer 验证通过后写入未来可用学习，包括交易记忆、未交易机会、Neutral shadow、探索式假设、分析师学习摘要、`alpha_setup_profile`、`alpha_setup_action_value`、机会排序偏好候选和策略状态。学习记忆会保留 `data_usage_summary`、数据质量、当时信号、setup 质量、PM 评分/排名/资金理由、PM/Auditor/Trader 结果、执行失败 trace、仓位、盈亏、手续费、持仓周期和后续影子结果。

`research_position_feedback` 与 `learning_mechanism:*` 账本/策略状态只认 Phase4 后的已验证事实。如果用户已经清空回测库，旧样本数量不能证明当前状态；新回测生成后应按同一套 audit 检查它们是否被重新写入和读取。

`alpha_setup_sample`、`alpha_setup_profile` 和 `alpha_setup_action_value` 保存 setup 的数据组合、数据质量、机会层级、PM/Auditor/Trader 结果、执行反馈和后续盈亏，只在 Phase4 后生成，只给未来交易日读取。分析师和 PM 的学习上下文会读取这些档案，但缓存只减少重复读取，不改变交易日可见性。

未完成交易日不能进入策略结论或学习。如果某天已经有推荐、成交、盘中决策或学习记录，但 `trading_day_phase` 中 phase1-4 没有全部 completed，系统验收必须报 `incomplete_trading_day_phase`，要求删除或重跑当天；这种记录不能用于判断收益、无成交或策略释放是否健康。

当前系统已接入轻量 SQL 相似 setup 检索。它不是向量库，也不存长文本；只按 ticker/sector/side/setup_type/horizon/regime/action 聚合 compact action-value 先验，并且必须满足历史样本 `trading_date < decision_date`。同品种同作用域真实样本可以按 open/hold/exit/execution lane 进入对应读取路径；同板块 fallback、similar SQL/RAG 和 shadow 只能作为弱先验，不能 seed 新开仓、不能覆盖当日证据、不能绕过 PM/Auditor/Trader。

### 10. Protocol Governor

Protocol Governor 调用控制侧工具、能力卡、工具权限策略、字段语义审计、preflight acceptance、system invariant audit 和 artifact lineage，不调用 LLM。它输出 `protocol_audit`、`preflight_health`、字段/权限/生命周期告警和成本观察；这些结果只能发现协议问题或阻止脏回测继续，不能创建交易授权，不能否决 PM 已审合约，不能改 lots 或 margin。

模型配置统一来自 `src/config/dev.yaml` 的 `llm` 与各智能体 override；系统通过 `llm_path`、模型 provider、model、reasoning effort、artifact metadata 保持模型调用可追踪。当前 `planner_mode=false`，Planner 不参与当前回测；macroeconomic、policy 等旧分析师已退役，不是当前启用智能体。

## 三、数据、先验与授权边界（2026-06-10）

当前数据和学习上下文必须区分“可引用证据”和“交易授权”：

1. Finoview、PandaAI、新闻和缓存只提供当日可见数据；缺字段、旧数据或无新闻不能伪造成方向证据。
2. 同板块 learning fallback 是相似案例先验，只能进入 prompt 帮助分析师比较当前证据，不能变成同品种 action-value 或开仓/加仓权限。
3. `risk_context` 是证据分类，不是独立智能体；真实风险审计来自 Auditor/TradeAuditor 业务链。
4. Trader 的执行 fallback 只能在 PM 显式授权的 execution plan 下使用，并必须记录触发检查、追价检查、失败原因和是否错过机会，供 Researcher 分开学习 execution action-value。
5. `execution_action_value` 不能被 Trader 直接读取；它只能由 PM 消化进审计后的 `final_action_contract.execution_plan/execution_profile`，Trader 再按最终合约和盘中数据执行或跳过。它不能创造策略方向、改变目标手数或绕过最终合约。
6. 软风险标签、历史 block/cap/probe 字段和学习状态必须先经 `reason_effects.py` 解释为硬风险、软风险、学习调整或释放信号，再由 PM 最终出口统一仲裁。硬风险仍必须阻断；软风险不能多层叠乘压死 alpha。
