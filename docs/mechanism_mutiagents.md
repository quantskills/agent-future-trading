# AgentQuant 多智能体运行机制

更新时间：2026-05-30

本文档说明 AgentQuant 当前已经代码落地的多智能体运行机制。它关注智能体之间如何分工、如何传递信息、各自输入输出是什么；不记录待执行优化清单。

## 一、运行机制原则

AgentQuant 的多智能体机制服务两个核心运行场景：期货策略回测，以及基于同一套四阶段流程的模拟盘/模拟交易。系统以交易日为最小运行单元，围绕 Phase1 至 Phase4 顺序推进。

当前主工作流是四阶段：

1. **Phase1 策略生成**：分析师读取当日可用数据并输出结构化信号，Portfolio Manager 汇总信号、记忆、风控和账户状态，生成每个品种的盘前策略推荐。
2. **Phase2 交易执行**：Trader 将 Phase1 推荐翻译为可执行订单，结合盘中确认、成交价格、滑点、合约和仓位限制写入真实交易流水。
3. **Phase3 日终结算**：Accountant 根据成交、持仓、结算价、手续费和保证金更新账户、持仓和日结算记录。
4. **Phase4 复盘与研究**：Reviewer 验证前三阶段完整性、账务一致性和完整交易日志；验证通过后 Researcher 写入未来可用记忆、研究假设和学习状态。

多智能体之间要保持职责边界。分析师只负责给出有数据依据的信号和研究契约，不直接下单；PM 负责组合层决策和仓位目标，不写成交；Auditor 负责确定性审核，不调用 LLM、不下单、不算最终手数；Trader 只执行已批准计划，不创造新策略；Accountant 只结算和记账，不被学习结果改写；Reviewer 只验收和输出日志；Researcher 只写未来记忆，不改当日交易。

智能体之间通过结构化对象和数据库 artifact 传递信息，而不是靠自由文本互相猜。核心消息包括 `AnalystSignal`、`FuturesDecision`、`FuturesRecommendation`、`signal_snapshot`、`pre_open_plan`、`trade_auditor` payload、交易流水、日结算记录、完整交易日志、学习记忆和下一轮策略更新契约。

运行机制必须避免未来信息污染。Phase1 只能使用当日盘前可见信息；Phase2 使用当日执行窗口信息；Phase3 才写入结算事实；Phase4 研究只能影响未来交易日。no-trade shadow、Neutral 后续窗口和研究结果都只能在未来日期已结算后回填。

并行只用于不改变业务语义的环节。当前代码支持 Phase1 多品种分析预取与分析师 fanout，并集中保存分析师输出，避免 SQLite 写入冲突。PM 虚拟组合更新、Trader、Accountant、Reviewer 和 Researcher 仍按顺序运行，确保资金状态、交易流水、账务和学习状态不串扰。

## 二、各智能体职责、输入与输出

### 1. Planner / Control Team

Planner 是旧版 LLM 分析师选择器，只在 `planner_mode=true` 时启用。当前默认 `planner_mode=false`，系统直接运行 `workflow_analysts` 中配置的分析师。

输入：品种代码、可选分析师列表、LLM 配置。

输出：本轮要运行的分析师列表。它不参与交易审核，不替代 Auditor，也不决定仓位。

### 2. Technical Analyst

Technical 负责短线和价格行为分析。它读取 PandaAI 期货日频行情，计算趋势、MACD、ADX、波动率、成交额、支撑阻力、均值回归、RSI、随机指标等技术特征，并读取受限学习上下文。

输入：品种、交易日、盘前价格上下文、行情数据、学习上下文、LLM 配置。

输出：`AnalystSignal`、技术分析报告、数据使用摘要、研究契约字段。信号会说明方向、置信度、周期、模板、入场触发、退出提示、失效条件、证据冲突和 Neutral 责任信息。

### 3. Fundamental Analyst

Fundamental 负责中期基本面分析。它通过 Router 读取 Finoview 本地基本面数据和 PandaAI 衍生数据上下文，分析供需、库存、基差、仓单、产业链状态、数据新鲜度和业务质量，并读取受限学习上下文。

输入：品种、交易日、基本面数据、PandaAI 衍生数据、学习上下文、LLM 配置。

输出：`AnalystSignal`、基本面分析报告、数据使用摘要、研究契约字段。中期观点必须说明短线交易需要什么确认，不能单独作为短线建仓或亏损硬扛依据。

### 4. Commodity News Analyst

Commodity News 负责事件和新闻面分析。它读取本地新闻/Router 新闻结果，按品种产业链语境分析事件方向、强度、新鲜度、相关性和可交易性，并读取受限学习上下文。

输入：品种、交易日、盘前新闻、学习上下文、LLM 配置。

输出：`AnalystSignal`、新闻分析报告、数据使用摘要、研究契约字段。新闻可以提出事件机会，但需要说明进入交易前还需要哪些当前确认。

### 5. Portfolio Manager / Decision Team

Portfolio Manager 是 Phase1 的组合决策智能体。它读取三个分析师信号、账户与持仓、盘前价格、历史交易记忆、学习上下文、市场确认、数据质量摘要、策略记忆、自适应策略状态和临时策略状态。它调用 LLM 形成初步仓位判断，再用确定性风控、质量门槛、市场确认、持仓生命周期、资金利用率、净敞口、失效边界和 Auditor 结果修正。

输入：`AnalystSignal` 列表、Portfolio、配置、盘前价格、学习上下文、PandaAI market confirmation、DB 中的交易和学习状态。

输出：`FuturesDecision` 和 `FuturesRecommendation`。推荐中包含 `signal_snapshot`、`pre_open_plan`、数据质量摘要、业务质量摘要、研究契约摘要、Auditor payload、PM 诊断和 no-trade 原因。Phase1 只写推荐，不写真实交易流水。

### 6. Auditor / Decision Team

Auditor 是 PM 内部调用的确定性交易审核器，不是独立 Phase 脚本。它读取 PM 的目标方向和仓位、分析师信号组合、市场确认、基本面质量、近期表现、策略记忆、自适应策略状态和临时策略状态，给出 allow、scale_down、probe_only、reduce_only 或 block。

输入：`TradeAuditorInput`，包括信号、目标仓位、当前仓位、market confirmation、strategy memory、adaptive/provisional policy 和风控配置。

输出：`TradeAuditorOutput`，包括审核决策、仓位乘数、置信度乘数、cap 乘数、原因、备注、诊断和审计 payload。Auditor 不调用 LLM，不下单，不计算最终成交手数。

### 7. Trader / Execution Team

Trader 是 Phase2 执行智能体。它读取 Phase1 推荐，翻译成订单意图，检查当前持仓、合约、开平仓语义、盘中确认、VWAP/开盘区间、滑点、动态手数上限、净敞口和止损/退出政策，然后写入真实交易流水。

输入：Phase1 `FuturesRecommendation`、当前组合、合约信息、盘中数据、执行配置、风险和订单语义工具。

输出：期货交易流水、执行审计 payload、推荐状态更新、未成交/no-trade 原因。Trader 不创造新策略，只执行或拒绝 PM/Auditor 已批准的计划。

### 8. Accountant / Execution Team

Accountant 是 Phase3 结算智能体。它要求 Phase2 已完成，然后运行日终结算，按成交、持仓、结算价、手续费和保证金更新官方账户事实。

输入：配置、交易日、Phase2 交易流水、上一日组合、行情结算数据。

输出：`daily_settlement`、最新 Portfolio、持仓状态、手续费、保证金、账户权益和 Phase3 状态。Accountant 不学习、不调用 LLM、不接受研究结果改写账务。

### 9. Reviewer / Research Team

Reviewer 是 Phase4 确定性复盘者。它检查 Phase1 推荐数量、Phase2 交易流水、Phase3 结算、手续费一致性、余额/权益公式、持仓和保证金一致性、交易是否入账、artifact 与完整交易日志是否输出。

输入：Phase1-3 状态、推荐、交易流水、结算记录、最新组合、配置和数据库。

输出：Phase4 验证结果、daily summary、完整交易日志 `src/logs/<交易日>_transaction.log`、错误或警告。Reviewer 不调用 LLM；只有在验证通过后才调用 Researcher 写研究记忆。

### 10. Researcher / Research Team

Researcher 是 Phase4 后置研究智能体。它只在 Reviewer 验证后的事实底座上运行，负责把交易、未交易机会、Neutral、影子结果和归因研究沉淀为未来可用记忆。

输入：已验证的推荐、交易流水、结算结果、no-trade 原因、历史交易片段、配置和数据库。

输出：真实交易片段记忆、未交易机会记忆、no-trade shadow、Neutral 责任与后续窗口、探索式假设、分析师学习摘要、策略记忆、自适应策略状态、临时策略状态、资本部署状态、学习事件和下一轮策略更新契约。Researcher 可以调用 LLM 做研究，但不能下交易指令、改账或绕过 Auditor。

### 11. 四阶段脚本与智能体关系

`run/backtest.py` 按交易日依次运行 `proposal.py`、`order.py`、`settlement.py`、`validate_phase_flow.py`。

`proposal.py` 启动 Phase1 `AgentWorkflow`，运行分析师与 PM，并写入策略推荐。

`order.py` 启动 Trader，执行 Phase2。

`settlement.py` 启动 Accountant，执行 Phase3。

`validate_phase_flow.py` 启动 Reviewer，执行 Phase4；Reviewer 验证通过后调用 Researcher 写研究记忆。
