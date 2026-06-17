# AgentQuant 多智能体运行机制

更新时间：2026-06-17

本文档说明 AgentQuant 当前已经代码落地的多智能体运行机制。它关注智能体之间如何分工、如何传递信息、各自输入输出是什么；不记录待执行优化清单。

## 一、运行机制原则

AgentQuant 的多智能体机制服务两个核心运行场景：期货策略回测，以及基于同一套四阶段流程的模拟盘/模拟交易。系统以交易日为最小运行单元，围绕 Phase1 至 Phase4 顺序推进。

当前主工作流是四阶段：

1. **Phase1 策略生成**：分析师读取当日可用数据并输出结构化信号，Portfolio Manager 汇总信号、记忆、风控和账户状态，生成每个品种的盘前策略推荐。
2. **Phase2 交易执行**：Trader 将 Phase1 推荐翻译为可执行订单，结合盘中确认、成交价格、滑点、合约和仓位限制写入真实交易流水。
3. **Phase3 日终结算**：Accountant 根据成交、持仓、结算价、手续费和保证金更新账户、持仓和日结算记录。
4. **Phase4 复盘与研究**：Reviewer 验证前三阶段完整性、账务一致性和完整交易日志；验证通过后 Researcher 写入未来可用记忆、研究假设和学习状态。

多智能体之间要保持职责边界。分析师只负责给出有数据依据的信号和研究契约，不直接下单；PM 负责组合层决策和仓位目标，不写成交；Auditor 负责确定性审核，不调用 LLM、不下单、不算最终手数；Trader 只执行已批准计划，不创造新策略；Accountant 只结算和记账，不被学习结果改写；Reviewer 只验收和输出日志；Researcher 只写未来记忆，不改当日交易。

智能体之间通过结构化对象和数据库 artifact 传递信息，而不是靠自由文本互相猜。核心消息包括 `AnalystSignal`、`FuturesDecision`、`FuturesRecommendation`、`signal_snapshot`、`final_action_contract`、`final_new_entry_trade_authority`、Auditor payload、交易流水、日结算记录、完整交易日志、学习记忆和下一轮策略更新契约；`pre_open_plan` 只保留为 PM 内部草稿和审计日志，不是跨智能体交易事实来源。

当前有效口径是“结构化证据驱动 + action-value 动作偏好 + 可审计交易出口”。分析师不是静态投票机器，`Bullish/Bearish/Neutral` 只作方向摘要；PM 自然语言只作审计材料；策略交易真相只认顶层 `final_action_contract`、顶层 `final_new_entry_trade_authority`、`active_opportunity_audit`、Trader 执行结果和会计结算结果。Researcher 写入的学习结果只有在未来交易日、同作用域、当日证据仍成立时，才能按 `open/hold/exit/execution` 动作分账被读取：分析师只读 `signal_calibration`；PM 读取 open/hold/exit 的仓位生命周期偏好，并把 execution 偏好写入最终合约的 `execution_plan/execution_profile`；Trader 只读审计后的 `final_action_contract` 和盘中数据，不直接读取研究 action-value；protocol_governor 只做审计。任何学习结果都不能绕过 Auditor 或 Trader。

运行机制必须避免未来信息污染。Phase1 只能使用当日盘前可见信息；Phase2 使用当日执行窗口信息；Phase3 才写入结算事实；Phase4 研究只能影响未来交易日。no-trade shadow、Neutral 后续窗口和研究结果都只能在未来日期已结算后回填。

并行只用于不改变业务语义的环节。当前代码支持 Phase1 多品种分析预取与分析师 fanout，并集中保存分析师输出，避免 SQLite 写入冲突。PM 虚拟组合更新、Trader、Accountant、Reviewer 和 Researcher 仍按顺序运行，确保资金状态、交易流水、账务和学习状态不串扰。

## 二、各智能体职责、输入与输出

### 1. Planner / Control Team

Planner 是可选控制智能体，只在 `planner_mode=true` 时启用。当前默认关闭，系统按 `workflow_analysts` 直接运行固定分析团队。

输入：品种代码、可选分析师列表、LLM 配置。

输出：本轮分析师列表。Planner 不审核交易、不生成仓位、不替代 PM 或 Auditor。

### 2. Technical Analyst / Analysis Team

Technical 负责短线价格行为与技术结构。它读取 PandaAI 日频行情、盘前价格上下文、受限学习上下文和技术参数情境校准，计算趋势、波动率、成交量、支撑阻力、ATR、RSI、布林、均线等证据。

输入：品种、交易日、盘前可见行情、学习上下文、技术参数校准、LLM 配置。

输出：`AnalystSignal`、技术分析报告、数据使用摘要、研究契约字段、`adaptive_params` 与 `technical_parameter_calibration` metadata。当前技术面已经接入机会分层和 setup 质量评分：趋势延续必须匹配 market regime；在 choppy/range/weak trend/high volatility 中，普通趋势信号会降级为 `direction_only`，除非出现突破、量能或盘中确认；非 Neutral 信号还要写入入场质量、失效边界、退出提示和持仓周期。技术面主要提供 open/exit 的短线触发、价格位置和失效边界，不单独决定真实仓位。

### 3. Fundamental Analyst / Analysis Team

Fundamental 负责供需、库存、基差、仓单、产业链和中期基本面判断。它读取 Finoview 本地 feather 基本面数据、PandaAI 衍生数据上下文、数据质量摘要和受限学习上下文。

输入：品种、交易日、Finoview 基本面快照、PandaAI 衍生字段、数据可用性摘要、学习上下文、LLM 配置。

输出：`AnalystSignal`、基本面分析报告、数据使用摘要、研究契约字段和 setup 质量 metadata。中期或长期基本面观点必须补充短线触发、失效边界和持仓周期；缺少这些要素时只能作为方向观点或观察线索，不能单独触发短线正常仓位或亏损仓硬扛。基本面主要输出 support/conflict/background/invalidation、factor_driver、horizon 和 action_relevance，服务 PM 的 hold/exit/scale 背景判断。

### 4. Commodity News Analyst / Analysis Team

Commodity News 负责新闻、事件和催化剂分析。它读取本地新闻 txt、品种产业链语境、新闻可见窗口、数据使用摘要和受限学习上下文。

输入：品种、交易日、盘前可见新闻、学习上下文、LLM 配置。

输出：`AnalystSignal`、新闻分析报告、数据使用摘要、事件强度、研究契约字段和 setup 质量 metadata。当前新闻面已经区分真实催化、普通方向性消息和噪声；缺少强催化或价格/盘中反应时，只能进入 `direction_only` 或 watchlist 线索，不能直接变成正常可交易机会。只有具备事件窗口、可执行催化、触发条件、风险边界和当日确认的新闻，才可能参与 open 的证据组合。

### 5. Portfolio Manager / Decision Team

Portfolio Manager 是 Phase1 组合决策智能体。它读取三位分析师信号、账户和持仓、盘前价格、数据质量摘要、market confirmation、交易记忆、学习上下文、策略记忆、自适应策略状态、临时策略状态和 Auditor 结果。PM 可以调用 LLM 形成初步判断，但最终仓位会被确定性风控、质量门槛、机会层级、持仓生命周期、失效边界、资金利用率和 Auditor 结果修正。

输入：`AnalystSignal` 列表、Portfolio、配置、盘前价格、PandaAI market confirmation、学习上下文、历史交易和策略状态。

输出：`FuturesDecision` 和 `FuturesRecommendation`。推荐包含 `signal_snapshot`、顶层 `final_action_contract`、顶层 `final_new_entry_trade_authority`、`data_quality_summary`、`business_quality_summary`、最终契约学习追踪、Auditor payload、PM 诊断和 no-trade 原因；`pre_open_plan` 只作为 PM 内部草稿随快照留痕，不能被 Trader、Researcher、evaluation 或 audit 当作交易事实兜底读取。PM 现在按动作读取证据：open 需要技术触发或明确事件催化、失效边界和当前确认；hold 读取趋势/基本面背景是否仍有效；exit 读取触发失败、失效边界、风险事件、止损/时间止损和回吐学习；execution 偏好只能被 PM 写入最终合约的执行计划，不能改变方向或手数。加仓/缩仓不是单独 action-value 词表，而是由 `final_action_contract.current_lots -> target_lots -> lots_delta` 唯一推出。PM 按 `deployable_alpha/tradeable_setup/direction_only/no_trade` 融合机会层级，并用 setup 质量调整目标仓位：成熟可交易机会可进入仓位决策；`direction_only/watchlist/no_trade` 只能观察或等待触发，不能被最小手数、旧 probe seed 或释放标签直接绕成真实开仓；no-trade 不给新仓权限；盈利同作用域仓位若证据仍成立可少减仓，频繁亏损/磨损样本会被 cap。PM 还会读取 `alpha_setup_profile`、`alpha_setup_action_value` 和轻量 SQL 相似 setup 先验，把成熟正向 open setup 作为受控落仓证据，把负向 hold/exit setup 作为同作用域复核/cap/保护边界。所有影响仍必须经过当日证据、最终交易出口、Auditor、Trader 和 20% 保证金硬上限。Phase1 只写推荐，不写真实交易流水。

### 6. Auditor / Decision Team

Auditor 是 PM 内部调用的确定性审核器。它读取 PM 目标方向和仓位、分析师信号组合、market confirmation、基本面质量、近期表现、策略记忆、自适应策略状态、临时策略状态和风控配置。

输入：`TradeAuditorInput`，包括信号、目标仓位、当前仓位、market confirmation、strategy memory、adaptive/provisional policy 和风控配置。

输出：`TradeAuditorOutput`，包括 allow、scale_down、probe_only、reduce_only 或 block，及仓位乘数、置信度乘数、cap 乘数、原因、备注和诊断。Auditor 不调用 LLM，不下单，不计算最终成交手数；候选假设只能提示分析，成熟经验才可能在同作用域、当日证据和失效边界通过时影响仓位权限。当前 Auditor 将历史弱表现、弱质量、新闻单驱动等归为软风险，优先转成 probe_only、scale_down 或 cap；只有显式策略 block 和业务硬边界才应把新风险暴露归零。

### 7. Trader / Execution Team

Trader 是 Phase2 执行智能体。它读取 Phase1 推荐，把目标手数翻译为 open/close/hold/rollover 订单，检查当前持仓、合约、开平仓语义、盘中确认、VWAP/开盘区间、滑点、涨跌停、临近交割、保证金和执行窗口。

输入：Phase1 `FuturesRecommendation`、当前组合、合约信息、盘中 PandaAI 行情、执行配置、订单语义和业务规则工具。

输出：真实期货交易流水、执行审计 payload、推荐状态更新和未成交/no-trade 原因。Trader 不创造新策略，只执行或拒绝 PM/Auditor 已批准计划；未成交、涨跌停、盘中触发失败和执行价问题会写入 `execution_learning_trace`。Trader 还会写入 `setup_execution_learning`，记录 setup 类型、机会层级、alpha setup 融合结果和执行状态，进入后续研究记忆，但不改变当日成交或账务。

Trader 不直接读取 `execution_action_value`。execution 研究偏好必须先由 PM 消化进审计后的 `final_action_contract.execution_plan/execution_profile`；Trader 只读取这张最终合约和盘中数据，决定触发或不触发，不能改方向、目标手数、变化手数或保证金口径。

### 8. Accountant / Execution Team

Accountant 是 Phase3 结算智能体。它要求 Phase2 已完成，然后按成交、持仓、结算价、手续费、合约乘数和保证金更新官方账户事实。

输入：配置、交易日、Phase2 交易流水、上一日组合、行情结算数据。

输出：`daily_settlement`、Portfolio、持仓、手续费、保证金、账户权益、品种日 PnL 和 Phase3 状态。Accountant 不学习、不调用 LLM、不接受研究结果改写账务。

### 9. Reviewer / Research Team

Reviewer 是 Phase4 确定性复盘者。它检查 Phase1 推荐、原始 signal 表、Phase2 交易流水、Phase3 结算、手续费一致性、余额/权益公式、持仓和保证金一致性、交易是否入账、artifact、data quality 文件和完整交易日志。

输入：Phase1-3 状态、推荐、交易流水、结算记录、最新组合、配置和数据库。

输出：Phase4 验证结果、daily summary、完整交易日志 `src/logs/<交易日>_transaction.log`、错误或警告。Reviewer 不直接调用 LLM；验证通过后才触发 Researcher 写未来研究记忆。

### 10. Researcher / Research Team

Researcher 是 Phase4 后置研究智能体，只在 Reviewer 验证后的事实底座上运行。它把真实交易、未交易机会、Neutral、影子结果、no-trade 原因、分析师表现和归因研究沉淀为未来可用记忆。

输入：已验证推荐、交易流水、结算结果、no-trade 原因、历史交易片段、数据依据、学习上下文预算、配置和数据库。

输出：真实交易片段记忆、未交易机会记忆、no-trade shadow、Neutral 责任与后续窗口、探索式假设、分析师学习摘要、策略记忆、自适应策略状态、临时策略状态、资本部署状态、学习事件和 `next_round_memory_contract`。Researcher 还会维护 `alpha_setup_sample/profile/action_value`，把 setup 的样本表现、生命周期和动作价值写成未来可用档案，并生成可被 PM 检索的 action preference。Researcher 可以调用 LLM 做研究，但不能下交易指令、改账、绕过 Auditor 或用未来结果污染当日决策。

### 11. 四阶段脚本与智能体关系

`run/backtest.py` 按交易日依次运行 `proposal.py`、`order.py`、`settlement.py`、`validate_phase_flow.py`。

`proposal.py` 启动 Phase1 `AgentWorkflow`，运行分析师与 PM，并写入策略推荐。

`order.py` 启动 Trader，执行 Phase2。

`settlement.py` 启动 Accountant，执行 Phase3。

`validate_phase_flow.py` 启动 Reviewer，执行 Phase4；Reviewer 验证通过后调用 Researcher 写研究记忆。

## 三、当前智能体与证据角色边界（2026-06-10）

- 分析师可以输出 `risk_context` 证据角色，用于说明风险、冲突、缺口或失效边界；`risk_context` 不是新增智能体。
- 真正的审计智能体/业务链是 Auditor / TradeAuditor。它读取结构化证据、PM 推荐、账户状态和风险边界，不能被分析师 evidence_role 替代。
- PM 不再把方向观点、旧静态权重或相似案例直接变成真实开仓。新开仓必须经过 action evidence、`final_new_entry_trade_authority`、资金约束、Auditor 和 Trader。
- Trader 不创造策略方向；它只执行 PM/Auditor 批准的计划，并把触发失败、追价失败、未成交和错过机会写回研究链路。
- 轻量 SQL 相似 setup 检索只返回 compact evidence，且历史样本必须早于当前交易日；同品种同作用域真实样本按 `open/hold/exit/execution` lane 使用，同板块、similar SQL/RAG 和 shadow 只能作弱先验，不能直接放大真实仓。
- 旧 `block/cap/probe` 字段统一先由 reason-effect 解释为硬风险、软风险、学习调整或释放信号，再交给 PM 最终出口仲裁；硬风险仍阻断，软风险不应多层叠乘压死真实 alpha。
