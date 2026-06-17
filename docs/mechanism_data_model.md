# AgentQuant 数据与模型调用机制

更新时间：2026-06-17

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

1. **LLM 只负责结构化理解与研究总结**：分析师、PM、Planner、Researcher 可以调用 LLM；硬风控、成交、结算、账务、完整交易日志和 Phase4 验收不能由 LLM 最终裁决。
2. **Reviewer 不直接调用 LLM**：Reviewer 只做确定性验收、账务一致性检查、交易流水检查和完整交易日志输出；Researcher 才负责 Phase4 后的 LLM 研究与学习写入。
3. **避免重复调用以加快回测**：Phase1 支持多品种分析并行、LLM 并发门、学习上下文缓存、PandaAI/Finoview/新闻预取和共享缓存；这些优化只减少工程耗时，不改变策略逻辑。
4. **PM 决策仍按组合顺序串行**：分析师读数和 LLM 分析可以并行，但 PM、Trader、Accountant、Reviewer/Researcher 仍按交易日和品种顺序执行，避免资金状态和学习状态串扰。
5. **模型路径必须可审计**：分析师与 PM 的输出保留 `llm_path`、模型配置审计信息、结构化信号、数据依据和 artifact 指针，便于复盘模型是否按配置调用。

### 3. 结构化输出与交易权限边界

结构化输出不是为了限制 LLM 推理，而是为了统一审计和交易出口。LLM 可以在分析报告、reasoning notes、raw rationale 中充分表达推理；但能进入交易链路的内容必须落到结构化字段。当前系统的最终交易真相不是 PM 自然语言，也不是 `pre_open_plan` 草稿，而是顶层 `final_action_contract`、顶层 `final_new_entry_trade_authority`、`active_opportunity_audit`、Trader 执行结果和会计结算结果；`pre_open_plan` 只作为 PM 内部草稿/日志保留，不得作为 Trader、Researcher、evaluation 或 audit 的兜底事实来源。

分析师输出的 `Bullish/Bearish/Neutral` 只是方向摘要，不是开仓投票。Technical/Fundamental/Commodity News 必须分别输出动作证据、品种/品类上下文、触发、失效边界、support/conflict/catalyst/risk 等结构化字段。分析师读取历史 action-value 时只能使用 `signal_calibration` 校准证据质量，不能生成交易授权。PM 按 open/hold/exit 读取当前证据和匹配 action lane 的 action-value；execution action-value 只能由 PM 消化进 `final_action_contract.execution_plan/execution_profile`。最终交易出口仍由 market confirmation、资金参数、Auditor 硬风险、Trader 执行约束和顶层 `final_action_contract` 共同决定。

## 二、各智能体的数据与模型调用方式

### 1. 工作流、预取与共享缓存

`src/graph/workflow.py` 是 Phase1 策略生成主链路。每个交易日会先按当日可见性边界预取和缓存数据：PandaAI 日线、分钟线、结算相关行情和衍生字段进入共享缓存；Finoview feather 与本地新闻 txt 通过进程内缓存读取；不可用字段会写入数据质量摘要，而不是反复调用或伪造成方向证据。预取失败只产生 warning，不改变交易语义。

并行分析只发生在不会改变业务状态的环节。分析师输出由工作流集中保存，signal 表按组合、交易日、品种、分析师保留最终一条，避免重复信号污染后续统计、学习和评估。

当前 Phase1 还会在 PM 决策前做分析师输出完整性验收：technical、fundamental、commodity_news 必须各自生成一条结构化 `AnalystSignal`。技术面核心行情、合约元数据或 PM 风险评估异常属于真实系统错误，应直接暴露并停止当日流程；本地 Finoview 基本面或本地新闻真实缺口可以降级，但必须生成机器可读 no-trade 数据缺口信号，不能空返回，也不能伪造成 Bullish/Bearish。

### 2. Technical Analyst

Technical 调用 PandaAI 盘前可见行情，计算趋势、波动率、成交量、支撑阻力、ATR、RSI、布林和均线等技术证据，并读取技术参数情境校准。它会把 `data_usage_summary`、`adaptive_params`、`technical_parameter_calibration`、机会层级和证据冲突写入 signal metadata。技术面可调用 LLM 生成结构化信号，但参数校准只做小幅、有界、同作用域调整，不直接授权仓位。技术面核心行情为空、缺少 close 或读取异常时，不允许返回空信号或 HOLD 兜底，应直接暴露错误。

### 3. Fundamental Analyst

Fundamental 读取 Finoview 本地基本面 feather 和 PandaAI 衍生字段。Finoview 数据会按交易日快照、覆盖率、新鲜度、缺口和低置信度诊断进入 prompt；PandaAI 衍生字段会记录 reference date、feature status、record counts、权限不足和缺口状态。中期/长期基本面观点必须说明短线触发、失效边界和持仓周期，否则只能形成方向观点或观察线索。若本地基本面真实无可用数据，Fundamental 必须输出 `deterministic_data_gap_no_trade` 信号和 metadata；若读取链路异常，则直接报错，不用空返回掩盖。

### 4. Commodity News Analyst

Commodity News 读取本地 `data/News_data/Future_news/<ticker>.txt`，按当日可见窗口过滤，不读取未来新闻。它记录新闻文件、解析数量、最终使用新闻数、新闻截止规则、事件方向、强度、新鲜度、相关性和可交易性。新闻面可调用 LLM，但输出要区分真实催化、方向性消息和噪声；缺少强催化或价格反应时只进入 `direction_only`、watchlist 或等待触发。若本地新闻真实无可用内容，Commodity News 必须输出 `deterministic_no_news_no_trade` 信号和 metadata；若新闻读取链路异常，则直接报错，不用空返回掩盖。

### 5. Portfolio Manager

PM 读取三位分析师结构化信号、交易研究契约、学习上下文、PandaAI market confirmation、账户持仓、资金、风险状态、数据质量摘要和 Auditor 结果。PM 的 recommendation snapshot 会写入每位分析师的 `data_usage_summary`、market confirmation feature status、每日 `data_quality_summary` 路径、`llm_path`、机会层级、setup 质量、`learning_to_position_trace`、仓位质量控制和 PM 诊断。PM 可以调用 LLM 形成初步判断，但仓位目标必须经过数据质量、setup 质量、机会层级、风控和 Auditor 修正。

每日回测或模拟盘会输出：

```text
src/logs/data_quality/<交易日>.json
```

干净回测后，每个完成的交易日都应对应一份 data quality 文件与一份完整交易日志。若用户已经手动删除全部回测记录，旧主库交易日数量不再代表当前事实；下一轮应重新检查 data quality、推荐快照、signal、交易日志和学习记忆是否逐日生成并互相对齐。

### 6. Auditor

Auditor 不调用 LLM，不直接拉取原始数据。它读取 PM、分析师、market confirmation、数据质量、策略记忆、自适应策略状态和风控配置形成确定性审核。缺失数据、候选记忆、方向观点或 shadow 结果都不能单独成为交易授权。

### 7. Trader 与 Accountant

Trader 使用 Phase1 推荐和当时已发生的盘中 PandaAI 行情执行，不调用 LLM 创造策略。Trader 会记录盘中确认、执行基准、滑点、限价/交割保护和未成交原因。Accountant 使用官方结算价、成交记录、手续费、合约乘数和保证金做 Phase3 结算，不调用 LLM，也不被学习文本改账。

### 8. Reviewer 与 Researcher

Reviewer 负责 Phase4 确定性验收、daily summary 和完整交易日志，不直接调用 LLM。Researcher 在 Reviewer 验证通过后写入未来可用学习，包括交易记忆、未交易机会、Neutral shadow、探索式假设、分析师学习摘要和策略状态。学习记忆会保留 `data_usage_summary`、数据质量、当时信号、setup 质量、PM/Auditor/Trader 结果、执行失败 trace、仓位、盈亏、手续费、持仓周期和后续影子结果。

`research_position_feedback` 与 `learning_mechanism:*` 账本/策略状态只认 Phase4 后的已验证事实。如果用户已经清空回测库，旧样本数量不能证明当前状态；新回测生成后应按同一套 audit 检查它们是否被重新写入和读取。

Researcher 还会写入 `alpha_setup_sample`、`alpha_setup_profile` 和 `alpha_setup_action_value`。这些表保存 setup 的数据组合、数据质量、机会层级、PM/Auditor/Trader 结果、执行反馈和后续盈亏，只在 Phase4 后生成，只给未来交易日读取。分析师和 PM 的学习上下文会读取这些档案，但缓存只减少重复读取，不改变交易日可见性。

当前系统已接入轻量 SQL 相似 setup 检索。它不是向量库，也不存长文本；只按 ticker/sector/side/setup_type/horizon/regime/action 聚合 compact action-value 先验，并且必须满足历史样本 `trading_date < decision_date`。同品种同作用域真实样本可以按 open/hold/exit/execution lane 进入对应读取路径；同板块 fallback、similar SQL/RAG 和 shadow 只能作为弱先验，不能 seed 新开仓、不能覆盖当日证据、不能绕过 PM/Auditor/Trader。

### 9. Planner 与模型路由

Planner 只在启用 planner mode 时调用 LLM 选择分析师组合，不生成交易指令。模型配置统一来自 `src/config/dev.yaml` 的 `llm` 与各智能体 override；系统通过 `llm_path`、模型 provider、model、reasoning effort、artifact metadata 保持模型调用可追踪。

## 三、数据、先验与授权边界（2026-06-10）

当前数据和学习上下文必须区分“可引用证据”和“交易授权”：

1. Finoview、PandaAI、新闻和缓存只提供当日可见数据；缺字段、旧数据或无新闻不能伪造成方向证据。
2. 同板块 learning fallback 是相似案例先验，只能进入 prompt 帮助分析师比较当前证据，不能变成同品种 action-value 或开仓/加仓权限。
3. `risk_context` 是证据分类，不是独立智能体；真实风险审计来自 Auditor/TradeAuditor 业务链。
4. Trader 的执行 fallback 只能在 PM 显式授权的 execution plan 下使用，并必须记录触发检查、追价检查、失败原因和是否错过机会，供 Researcher 分开学习 execution action-value。
5. `execution_action_value` 不能被 Trader 直接读取；它只能由 PM 消化进审计后的 `final_action_contract.execution_plan/execution_profile`，Trader 再按最终合约和盘中数据执行或跳过。它不能创造策略方向、改变目标手数或绕过最终合约。
6. 软风险标签、历史 block/cap/probe 字段和学习状态必须先经 `reason_effects.py` 解释为硬风险、软风险、学习调整或释放信号，再由 PM 最终出口统一仲裁。硬风险仍必须阻断；软风险不能多层叠乘压死 alpha。
