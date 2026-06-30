# AgentQuant 多智能体运行机制

更新时间：2026-06-25

本文档定义 AgentQuant 启用智能体的固定工作框架。它是版本级契约，不是建议流程。后续代码、提示词、工具、审计和测试都必须按这条链路收敛。

## 一、基础逻辑

AgentQuant 是期货交易策略生成系统。它采用多智能体结构，是因为期货价格走势虽然表现为单一时序数据，但影响该时序的因素不是单一维度。技术形态、基本面供需、库存仓单、基差、新闻事件、资金状态、历史相似交易和执行结果，都会改变同一条价格序列的未来走势判断。

系统使用 LLM 的位置，是让具备专业分工的分析师和研究员处理信息、解释冲突、形成结构化预测证据或结构化研究成果；系统不让 LLM 直接决定仓位、手数或最终交易合约。

结构化字段不是 LLM 推理上限；结构化字段是 LLM 结果的落地格式。LLM 可以产生解释、冲突分析、反事实和不确定性，但这些内容必须进入统一字段语义表登记过的结构化字段，不能作为自由文本交易权限。

分工原则是：LLM 负责提高预测质量；结构化字段负责保证信息不丢、不漂；投资组合经理、确定性工具和审计闸门负责保证交易动作可复现、可追责。

多智能体交易系统必须守住三条底线：

1. 权限隔离：智能体不能越权，不能偷窥其他智能体内部过程，只能读取上游正式输出。
2. 字段统一：字段名称、含义、权限和消费方式必须全系统一致，唯一来源是 `docs/unified_field_semantics.md`。
3. 门控统一：硬门控只阻断非策略风险、越权、前视、字段缺失、账务错误和非法合约；软门控用于降级、减分、缩手数、条件监控或补证据，不能层层重复把交易压死。

主业务链只认一条交易事实链：

```text
数据与行情
-> 分析师结构化预测证据
-> signal_collector 信号收集员
-> portfolio_manager 投资组合经理唯一交易合约
-> auditor 审计员
-> trader 交易员
-> accountant 会计师
-> reviewer 复盘员
-> researcher 研究员
-> 下一轮分析师和投资组合经理使用结构化学习成果
```

控制组只做旁路治理：

```text
protocol_governor 协议管理员
contract_coverage_audit
pre_backtest_acceptance
system_invariant_audit
mechanism_effectiveness_audit
```

## 二、固定工作流

本文中的“证据”和“信号”指盘前或决策时点前已经可见的信息，用于预测开盘后的日频价格走势；不是盘中已经发生结果，也不是收盘后复盘事实。所有证据必须满足 `no_lookahead_status`。

| 顺序 | 阶段 | 执行者 | 是否调用 LLM | 必须输出 | 下游 | 硬边界 |
|---|---|---|---|---|---|---|
| 1 | 盘前预测证据分析 | `technical` 技术面分析师、`fundamental` 基本面分析师、`commodity_news` 期货新闻面分析师 | 是 | `action_evidence_contract` | `signal_collector` 信号收集员 | 只能输出结构化预测证据，不能输出手数、仓位、最终交易动作 |
| 2 | 盘前预测信号收集 | `signal_collector` 信号收集员 | 否 | `signal_collection_contract` | `portfolio_manager` 投资组合经理 | 只整理三类分析师结构化预测证据，不读研究库，不输出 score/rank/手数/交易动作 |
| 3 | 历史学习读取 | `decision_memory_retrieval` | 否 | `effective_memory_summary`、有效 action-value 列表、剔除/降级原因 | `portfolio_manager` 投资组合经理 | 只能读取结构化研究信息；空历史不能挡真实历史；不能输出手数或交易动作 |
| 4 | 机会评分排序 | `opportunity_ranking` | 否 | `opportunity_scorecard`、`opportunity_score_components`、`opportunity_rank`、`capital_allocation_reason` | `portfolio_manager` 投资组合经理 | rank 只解释资金优先级，不是交易权限，不能输出最终手数或最终合约 |
| 5 | 手数计算 | `position_sizing` | 否 | `position_sizing_result` | `portfolio_manager` 投资组合经理 | 只按评分、资金、持仓、风控算目标手数建议；不能改方向，不能签合约 |
| 6 | 签发唯一交易合约 | `portfolio_manager` 投资组合经理 | 否 | `final_action_contract` | `auditor` 审计员 | 只能由投资组合经理签发唯一策略合约；不得调用 LLM；不得生成第二套交易计划 |
| 7 | 合约审计 | `auditor` 审计员 | 否 | `audit_verdict`、审计 payload | `trader` 交易员 / 投资组合经理记录 | 只审合约，不改方向、不改手数、不新建合约 |
| 8 | 合约执行 | `trader` 交易员 | 否 | `execution_result`、`execution_learning_trace` | `accountant` 会计师、`reviewer` 复盘员、`researcher` 研究员 | 只执行审计通过的 `final_action_contract` 及合约内 `execution_profile/entry_trigger/requires_intraday_confirmation/can_execute_without_intraday_trigger`，不能读研究库、action-value、`strategy_memory` 或 `adaptive_policy_state` 下单或放宽触发 |
| 9 | 结算入账 | `accountant` 会计师 | 否 | `daily_settlement`、PnL、费用、保证金、权益和持仓事实 | `reviewer` 复盘员 | 只按成交和结算事实入账，不能用学习或 LLM 改账 |
| 10 | 复盘归因 | `reviewer` 复盘员 | 否 | Phase4 验收、交易日志、事实归因、研究输入材料 | `researcher` 研究员 | 只复盘事实，不下单，不写最终 action-value，不触发研究员 LLM |
| 11 | 研究学习 | `researcher` 研究员 / `run/research/researcher_learning.py` | 可调但受限 | 结构化研究成果：分析师校准类研究、交易决策类 action-value、profile、state | 分析师 / `decision_memory_retrieval` | 只在复盘员 Phase4 验证完成后运行；不能修改当天合约、手数或交易员权限 |
| 12 | 协议治理 | `protocol_governor` 协议管理员及控制审计 | 否 | 契约覆盖、机制断链、系统不变量、回测前/每日非策略风险报告 | 开发与回测闸门 | 只治理链路和字段，不参与收益判断或交易动作 |

固定工作流禁止以下旁路：

- 投资组合经理直接把分析师自由文本当交易依据；
- `signal_collector` 信号收集员直接读取研究库或混入历史学习结论；
- 投资组合经理绕过 `decision_memory_retrieval` 直接解析研究记录；
- `decision_memory_retrieval`、`opportunity_ranking`、`position_sizing` 生成 `final_action_contract`；
- 审计员、交易员、会计师、复盘员、研究员改写投资组合经理的交易方向或目标手数；
- 复盘员入口调用研究员学习或任何 LLM 研究函数；
- 交易员使用 `opportunity_rank`、`opportunity_score`、`learning_used` 或研究记录作为下单权限；
- 任何 LLM 自由文本成为交易权限、仓位依据、审计依据、结算依据或下游研究 action-value。

旧 `planner` 保留为封存开发组件，不属于当前启用智能体和固定工作流。`planner_mode=false` 是当前唯一合法运行配置；如果配置为 `planner_mode=true`，`AgentWorkflow` 必须 fail-fast，不能进入 Phase1。

`preflight` 的 LLM auth probe 只是环境认证探针，用于回测前检查 provider/key/model 是否可用；它不是协议管理员的交易链路 LLM 调用，也不是任何 Phase1-Phase4 智能体。该探针不能读取行情、生成证据、生成合约、改手数或参与收益判断。

## 三、四阶段运行脚本与时间边界

AgentQuant 的交易运行框架固定为四个交易阶段，研究学习在 Phase4 之后单独运行。策略回测、模拟盘和实盘复刻必须共享同一套阶段顺序；差异只在于回测可以由自动化脚本连续跑多个历史交易日，模拟盘和实盘按真实时间分段运行。

| 阶段 | 现实含义 | 运行脚本 | 运行的智能体/主流程 | 输出 |
|---|---|---|---|---|
| Phase1 | 盘前策略生成 | `src/run/proposal.py` | `AgentWorkflow`：`technical` 技术面分析师、`fundamental` 基本面分析师、`commodity_news` 期货新闻面分析师、`signal_collector` 信号收集员、`portfolio_manager` 投资组合经理、`auditor` 审计员 | `final_action_contract`、`audit_verdict`、策略推荐 |
| Phase2 | 开盘后/盘中执行 | `src/run/order.py` | `trader` 交易员 | 成交/未成交、`execution_result`、`futures_transactions` |
| Phase3 | 收盘后结算 | `src/run/settlement.py` | `accountant` 会计师 | `daily_settlement`、PnL、费用、保证金、权益和持仓事实 |
| Phase4 | 收盘后复盘验收 | `src/run/validate_phase_flow.py` | `reviewer` 复盘员 | Phase4 验收、完整交易日志、事实归因、研究输入材料 |

Phase4 完成后，研究学习单独运行：

| 入口 | 运行脚本 | 智能体 | 作用 | 边界 |
|---|---|---|---|---|
| 研究学习 | `src/run/research/researcher_learning.py` | `researcher` 研究员 | 消费复盘事实，输出并持久化结构化研究信息，供未来交易日使用 | 不是交易执行阶段，不产生当天交易动作，不修改当天合约、手数、成交或结算 |

冷启动研究初始化单独运行：

| 入口 | 运行脚本 | 智能体 | 作用 | 边界 |
|---|---|---|---|---|
| 模板先验初始化 | `src/run/research/load_template_prior.py` | 研究初始化入口 | 显式加载研究员导出的 template prior 冷启动种子 | 不属于 Phase1-4，不由 `proposal.py` 自动调用，不生成当天交易动作，不读取当天或未来交易结果 |

时间边界规则：

- Phase1 是盘前决策，只能使用盘前或决策时点前可见的信息，禁止读取当天盘中结果、收盘结果或未来交易日信息。
- Phase2 是开盘后/盘中执行，只能按审计通过的 `final_action_contract` 和盘中触发条件执行，不能重新生成策略方向或手数。
- Phase3 是收盘后结算，只能按 Phase2 成交、结算价、手续费、滑点、保证金率和合约乘数入账。
- Phase4 是收盘后复盘验收，只能检查推荐、合约、成交、结算和阶段状态，并输出完整交易日志与事实归因。
- Phase4 可以输出完整交易日志和复盘事实材料，但不能输出 `action-value`、`strategy_memory`、`adaptive_policy_state`、`capital_deployment_state` 等未来研究状态。
- Phase4 标记 completed 只更新阶段状态，不触发 `strategy_memory` 刷新、学习 retention 清理或任何研究表写入。
- 研究学习只能在 Phase4 完成后运行，输出的结构化研究信息只允许影响未来交易日；研究信息持久化统一由 `researcher_learning.py` 和 `research_memory_writers` 承担。
- `template_prior` 冷启动种子只能通过 `load_template_prior.py` 显式初始化，不得混入 Phase1 `proposal.py` 主流程。

回测运行规则：

- `src/run/backtest.py` 按交易日循环执行 `proposal.py -> order.py -> settlement.py -> validate_phase_flow.py -> researcher_learning.py`。
- 回测可以一次性跑多日，因为历史数据在现实世界已经存在；但每个具体回测日内部必须按四阶段顺序复刻真实交易时间。
- 回测、模拟盘和实盘复刻必须共享同一套阶段逻辑、字段契约、智能体边界和 no-lookahead 约束。
- 任何未来信息污染当下 Phase1 决策，或让研究学习回头改变当天交易结果，都属于非策略风险，应由回测前验收、系统不变量或机制审计阻断。

回测与模拟盘的脚本差异：

| 脚本 | 回测/模拟盘是否有不同模式 | 当前代码事实 |
|---|---|---|
| `proposal.py` | 否 | 只按 `--trading-date` 跑 Phase1，生成投资组合经理合约和策略推荐 |
| `order.py` | 是 | `trader_agent` 支持 `--loop` 和 `--check-interval-seconds` |
| `settlement.py` | 否 | Phase2 完成后结算 |
| `validate_phase_flow.py` | 否 | Phase3 完成后复盘验收 |
| `researcher_learning.py` | 否 | Phase4 完成后研究学习 |

`src/run/backtest.py` 调用 `order.py` 时不带 `--loop`，交易员运行模式为 `runtime_mode="backtest_replay"`：单次回放完整交易日，`finalize_untriggered=true`。模拟盘应调用 `order.py --loop`，交易员运行模式为 `runtime_mode="paper_loop"`：按 `check_interval_seconds` 循环检查盘中触发，直到触发完成、没有 pending 单，或到达收尾时间。除 Phase2 交易员脚本外，当前代码没有其它阶段的 `paper/live` 专用运行分支。

## 四、启用智能体契约表

| 角色 | 输入 | 输出 | 是否调用 LLM | 禁止输出 | 下游如何消费 |
|---|---|---|---|---|---|
| `technical` 技术面分析师 | 行情、技术指标、商品价格行为 profile、技术学习校准、数据截止时间 | `AnalystSignal`、`action_evidence_contract`、`product_profile_evidence`、`fusion_evidence`、技术方向、触发、失效边界、证据强弱、假突破风险 | 是 | 手数、仓位比例、最终交易动作、`final_action_contract` | `signal_collector` 读取结构化证据；投资组合经理不直接把技术文本当交易权限 |
| `fundamental` 基本面分析师 | 库存、仓单、基差、供需、产业数据、商品价格行为 profile、基本面学习校准 | `AnalystSignal`、`action_evidence_contract`、`product_profile_evidence`、`fusion_evidence`、基本面方向、驱动、数据质量、反向压制、失效边界 | 是 | 手数、仓位比例、最终交易动作、`final_action_contract` | `signal_collector` 读取结构化证据 |
| `commodity_news` 期货新闻面分析师 | 新闻、事件、政策、舆情、商品价格行为 profile、新闻学习校准 | `AnalystSignal`、`action_evidence_contract`、`product_profile_evidence`、`fusion_evidence`、事件方向、催化质量、时效、确认条件、一次性冲击风险 | 是 | 手数、仓位比例、最终交易动作、`final_action_contract` | `signal_collector` 读取结构化证据 |
| `signal_collector` 信号收集员 | 三类分析师的结构化预测证据、`product_profile_evidence` 和 `fusion_evidence` | `signal_collection_contract`：统一结构化预测证据包，至少包含来源引用、逐条证据、方向、触发状态、证据强弱、时效、一致性、冲突、确认需求、缺失、风险、失效边界、profile 使用痕迹和 `evidence_fusion` | 否 | 历史学习结论、score/rank、仓位比例、手数、交易动作、`final_action_contract` | 投资组合经理读取盘前统一结构化预测证据包 |
| `portfolio_manager` 投资组合经理 | `signal_collection_contract`、`evidence_fusion`、账户、持仓、合约信息、市场确认、投资组合经理工具输出、资金与风控配置 | `FuturesRecommendation`、唯一 `final_action_contract`、`learning_used`、`opportunity_scorecard`、`opportunity_rank`、`position_sizing_result`、资金部署理由、`pm_fusion_diagnostics`、`pm_conflict_resolution` | 否 | LLM 自由判断、第二套交易计划、绕过审计员的交易权限、对外持久化 PM 内部草稿 | 审计员只审这张合约；交易员只执行这张合约 |
| `auditor` 审计员 | 投资组合经理的 `final_action_contract`、`pm_fusion_diagnostics`、账户、持仓、保证金、数据质量、硬风险边界 | `audit_verdict`、hard/soft risk reasons、审计 payload、`pm_fusion_explanation_audit` | 否 | 重新融合证据、改手数、改方向、新建合约、生成交易动作 | 投资组合经理记录审计结果；交易员只执行审过的合约 |
| `trader` 交易员 | 审计通过的 `final_action_contract`、合约化执行触发规则、盘中行情、执行配置 | 成交/未成交、`execution_result`、`execution_learning_trace` | 否 | 改投资组合经理方向、改投资组合经理手数、直接读取研究库/action-value/`strategy_memory`/`adaptive_policy_state` 下单或放宽触发 | 复盘员/研究员读取执行事实 |
| `accountant` 会计师 | 成交、持仓、结算价、手续费、滑点、保证金率、合约乘数 | `daily_settlement`、PnL、费用、保证金、账户权益、持仓状态 | 否 | LLM 调账、学习改账、交易动作 | 复盘员使用结算事实 |
| `reviewer` 复盘员 | 推荐、合约、成交、结算、执行结果、阶段状态、投资组合经理学习使用痕迹 | Phase4 验收、交易日志、事实归因、学习输入材料 | 否 | 下单、调仓、写最终 action-value | 研究员消费复盘事实 |
| `researcher` 研究员 | 复盘员事实、完整 episode、未交易机会、未触发条件机会、执行结果 | 结构化研究信息：`alpha_setup_action_value`、`alpha_setup_profile`、`adaptive_policy_state`、分析师校准类研究、交易决策类 action-value | 可调，但受限 | 当天策略交易指令、投资组合经理手数、交易员权限、直接修改合约、只供下游消费的自由文本研究结论 | 分析师消费校准类研究；投资组合经理经工具消费交易决策类研究 |
| `protocol_governor` 协议管理员 | 代码、配置、字段语义、契约覆盖、系统审计、机制审计 | 回测前/每日非策略风险报告、契约覆盖矩阵、机制断链报告 | 否 | 交易动作、手数、保证金、策略收益结论 | 发现非策略 hard error 时阻断回测或阻断收益评价 |

字段语义以 `docs/unified_field_semantics.md` 为唯一来源。允许为全系统改造新增字段，但必须同一轮同步完成：统一字段语义表、生产端、消费端、提示词、测试和契约覆盖闸门。缺任一项都视为语义漂移。

`src/tools/common/final_action_semantics.py` 是 `final_action_contract` 全生命周期和交易生命周期记忆语义的唯一共享解释器。PM、Auditor、Trader、Accountant、Reviewer、Researcher、Protocol Governor 及控制审计只能通过该工具解释条件监控、直接执行、普通持有、硬阻断、软降级、新开仓、扩大交易、减仓、退出、未触发、已触发成交，以及该合约必须读取的 `action_value_lane` 和 `memory_side_role`。分析师和 `signal_collector` 不生成交易权限；它们通过提示词、落地校验和信号收集边界禁止输出 `final_action_contract`、`conditional_trigger_authority`、`requires_intraday_confirmation`、`can_execute_without_intraday_trigger`、`reason_codes`、手数、保证金和最终交易动作。

`src/tools/agent_tools/analysis/analyst_product_price_behavior_profile.py` 是三类分析师共享的商品差异化分析协议工具。它读取 `src/config/product_price_behavior_profiles.yaml`，把每个品种的趋势惯性、波动特征、产业链确认、季节窗口、假突破风险和适用 setup 格式化给 `technical`、`fundamental`、`commodity_news`，并落成 `product_profile_evidence`。它只改变分析证据层的证据强调与确认纪律；PM 只通过 `signal_collection_contract` 把它当证据上下文读取，Auditor、Trader、Accountant 不直接读取、不解释、不执行该 profile。

`src/tools/common/evidence_fusion_semantics.py` 是多维证据融合预测协议的确定性解释工具。它把三类分析师的技术、基本面、新闻、商品 profile、历史校准和执行反馈相关字段整理为 `fusion_evidence`、`evidence_fusion`、`pm_fusion_diagnostics` 和复盘 `fusion_attribution_label`。它不调用 LLM、不签 `final_action_contract`、不输出手数、不下单、不入账、不直接写 action-value。PM 只能把它作为 `opportunity_scorecard` 的证据分项和合约解释来源；Auditor 只审 PM 是否解释主要冲突和必要确认；Trader、Accountant 不读取该工具。

## 五、系统事实统一入口

系统事实指已经被系统正式写入 DB、artifact 或 payload，且作为下游可信事实并消费的结构化结果。系统事实不是智能体的内部推理过程，也不是展示用自由文本；一旦进入 DB、artifact 或 payload，下游就必须能判断它属于哪一类事实、由谁产生、谁能消费、谁不能改写。

事实入口指某类系统事实被正式写入 DB、artifact 或 payload，并被下游允许消费的唯一授权写入点。入口是从系统治理角度命名的：虽然具体动作是智能体把结果写出去，但对整个系统来说，是该类事实进入后续流程的唯一入口。

统一规则：

- 每类系统事实只有一个授权事实入口。
- DB、artifact、payload 只是同一事实入口下的不同持久化载体，不是三套事实来源。
- 如果同一事实需要同时写入 DB、artifact 和 payload，必须由同一个授权入口统一写入。
- 下游可以记录上游事实的 ID、路径或必要摘要，用于追溯来源；但不能把上游完整对象复制成自己的事实输出。
- 控制审计、机制审计和回测前验收只能读取已由授权事实入口产生的标准事实，并按 DB schema 契约和字段语义表检查；不能生成业务事实，不能写业务表，不能猜测 DB 字段，不能创建交易权限。
- 任何模块绕过授权事实入口写入、复制或改写其他智能体事实，都属于非策略 hard error。

| 系统事实类型 | 唯一授权事实入口 | 允许写入的事实 | 只能读不能写的模块 | 禁止行为 |
|---|---|---|---|---|
| 预测证据事实 | 技术面分析师、基本面分析师、期货新闻面分析师的分析师输出入口 | `AnalystSignal`、`action_evidence_contract`、方向、触发、证据强弱、失效边界、数据质量 | 信号收集员、投资组合经理、审计员、复盘员、研究员、控制审计 | 写手数、仓位、最终交易动作、`final_action_contract` |
| 信号收集事实 | `signal_collector` 信号收集员输出入口 | `signal_collection_contract`、来源引用、逐条证据、共识/冲突、缺失、风险、失效边界 | 投资组合经理、审计员、复盘员、研究员、控制审计 | 写历史学习结论、score/rank、手数、交易动作、`final_action_contract` |
| 策略交易事实 | `portfolio_manager` 投资组合经理唯一合约写入入口 | `FuturesRecommendation`、唯一 `final_action_contract`、`learning_used`、`opportunity_scorecard`、`opportunity_rank`、`position_sizing_result`、`capital_allocation_reason`、`capital_deployment` | 审计员、交易员、会计师、复盘员、研究员、控制审计 | 由非 PM 模块新建、复制成第二套、改写或补写交易合约；PM 内部草稿进入 DB/artifact/payload |
| 审计事实 | `auditor` 审计员审计结果写入入口 | `audit_verdict`、hard/soft risk reasons、只读审计 payload | 投资组合经理、交易员、复盘员、控制审计 | 改方向、改手数、新建合约、直接消费研究记忆改交易权限 |
| 执行事实 | `trader` 交易员执行结果写入入口 | 成交/未成交、触发事实、`execution_result`、`execution_learning_trace`、执行必要字段摘要 | 会计师、复盘员、研究员、控制审计 | 保存完整 PM 合约镜像、保存 PM 学习/排名/资金解释字段、读取研究库下单 |
| 结算事实 | `accountant` 会计师结算写入入口 | `daily_settlement`、PnL、手续费、滑点、保证金、权益、持仓状态 | 复盘员、研究员、控制审计 | 用 LLM、学习或复盘改账；写交易动作或研究学习字段 |
| 复盘事实 | `reviewer` 复盘员 Phase4 写入入口 | Phase4 验收、完整交易日志、事实归因、研究输入材料、上游引用 | 研究员、控制审计 | 下单、调仓、写最终 action-value、写研究状态、触发研究员 LLM |
| 研究学习事实 | `researcher` 研究员学习写入入口 | 结构化研究信息、分析师校准类研究、交易决策类 action-value、profile、state、执行学习 | 分析师、`decision_memory_retrieval`、投资组合经理、控制审计 | 修改当天合约、成交、结算、PnL、交易员权限；把自由文本当下游研究结论 |
| 控制治理事实 | 协议管理员和控制审计报告入口 | 契约覆盖、系统不变量、机制断链、schema 契约、回测前/每日非策略风险报告 | 开发者、回测闸门 | 生成交易动作、改手数、写业务表、评价策略收益为 pass/fail |

事实入口到代码落点的收口目标如下。该表是后续代码扫描和改造的施工表；如果当前代码与该表不一致，以本表为目标收敛，但不得为满足表格新增重复事实或第二套字段语义。

| 系统事实类型 | 授权入口 | DB 表 | artifact / payload 载体 | 后续代码收口目标 |
|---|---|---|---|---|
| 预测证据事实 | 分析师结构化输出入口 | 分析师信号/上下文相关表 | 分析师报告 artifact、`action_evidence_contract`、`artifact_json` | 分析师输出统一经过结构化证据构造与校验；禁止分析师 artifact 或 payload 携带手数、仓位或最终交易动作。 |
| 信号收集事实 | `signal_collector` 信号收集员输出入口 | 信号收集或推荐前上下文记录 | `signal_collection_contract`、来源引用、证据摘要 payload | 信号收集员只聚合分析师正式输出；禁止读取研究库、写 score/rank、写手数或生成交易合约。 |
| 策略交易事实 | `portfolio_manager` 投资组合经理推荐/合约写入入口 | `futures_recommendation` | PM recommendation artifact、`signal_snapshot`、`final_action_contract` payload | 完整 `final_action_contract` 只能由 PM 推荐写入路径产生；其他阶段只能引用 `recommendation_id`、路径或执行必要摘要。 |
| 审计事实 | `auditor` 审计结果写入入口 | 推荐记录中的审计结果或审计 payload 载体 | `audit_verdict`、`audit_payload`、risk reasons | 审计员只写审计结论和风险原因；不得写新合约、改目标手数或直接消费研究记忆改交易权限。 |
| 执行事实 | `trader` 交易员执行写入入口 | `futures_transactions`、`futures_intraday_decision` | `execution_result`、`execution_learning_trace`、Phase2 execution artifact、transaction audit payload | Phase2 和 transaction payload 只能保存执行事实、触发事实和执行必要摘要；禁止镜像完整 PM 合约、PM 学习、排名或资金解释字段。 |
| 结算事实 | `accountant` 会计师结算写入入口 | `daily_settlement`、持仓/组合结算相关表 | settlement payload、positions snapshot、结算日志 | 结算只能由成交、结算价、费用、保证金和持仓事实计算；禁止学习、LLM 或复盘改账。 |
| 复盘事实 | `reviewer` 复盘员 Phase4 写入入口 | `trading_day_phase`、复盘日志索引或相关只读记录 | Phase4 review artifact、完整交易日志、事实归因、研究输入材料 | 复盘只验证 Phase1-3 和写事实归因；禁止写 action-value、研究状态或触发研究员 LLM。 |
| 研究学习事实 | `researcher_learning.py` 和 `research_memory_writers` | `alpha_setup_action_value`、`alpha_setup_profile`、`adaptive_policy_state`、`researcher_llm_notes` | research artifact、action-value payload、profile/state payload、研究员 LLM notes | 研究写入统一由研究员学习入口和研究写入工具承担；只能影响未来交易日，不得修改当天交易、执行、结算或复盘事实。 |
| 控制治理事实 | 控制组只读审计入口 | 不写业务表；只读核心运行表 | 契约覆盖报告、系统不变量报告、机制审计报告、schema 契约报告 | 控制审计只能按 DB schema 契约和字段语义表读取标准事实；禁止手写猜字段、写业务表、生成交易动作或评价收益为 pass/fail。 |

事实入口与载体关系固定为：

```text
授权智能体 / 授权工具
        |
        v
唯一事实入口
        |
        |-- DB 表记录
        |-- artifact 文件
        |-- payload / payload_json / signal_snapshot / audit_payload 等结构化容器
        |
        v
下游只读消费
```

同一事实可以有多种载体，但不能有多个入口。例如完整 `final_action_contract` 可以在 PM recommendation DB 记录、PM recommendation artifact 或 PM payload 中保存；但它不能被交易员 Phase2 artifact、transaction audit payload、复盘 artifact 或研究 artifact 复制成自己的事实输出。下游如需追溯来源，应使用 `recommendation_id`、`source_artifacts`、artifact 路径、摘要字段或执行必要字段摘要。

边界检查必须区分“事实对象”和“摘要/状态/错误/引用”。字段名本身不等于事实对象；非空 dict/list 结构才可能承载可被下游消费的事实对象。例如 `selected_counts.alpha_setup_action_value=0`、`source_status.alpha_setup_action_value="empty"`、`source_errors.adaptive_policy_state="db_unavailable"` 或上游 ID/path 只是计数、状态、错误或引用摘要，不是研究事实进入 PM artifact。真正的 `alpha_setup_action_value`、`adaptive_policy_state` 或 `researcher_llm_notes` 对象仍只能由研究员学习入口写入。

事实入口边界不改变多智能体底层逻辑：LLM 仍用于分析师和研究员形成结构化预测证据或结构化研究成果；学习仍通过研究员输出、分析师校准和投资组合经理记忆读取影响未来分析与决策；投资组合经理仍是唯一策略交易合约签发者；交易员仍按合约执行。事实入口边界只禁止旁路写入、重复复制和跨阶段改写，不是交易门控，也不能因为没有副本而压死交易。

### 工具目录边界

工具目录按功能边界分类，不按智能体名字分类：

- `src/tools/agent_tools/analysis`：分析侧业务工具，例如分析师证据质量、学习校准、商品差异化 profile 和信号融合。
- `src/tools/agent_tools/decision`：决策侧业务工具，例如信号证据收集、记忆读取、机会排序、手数计算、资金部署、失效边界。
- `src/tools/agent_tools/execution`：执行侧业务工具，例如盘中触发、成交模拟、执行退出规则。
- `src/tools/agent_tools/research`：研究侧业务工具，例如复盘学习、action-value、profile、state 和结构化研究信息持久化。
- `src/tools/agent_tools/control`：控制侧治理工具，例如契约覆盖、系统不变量、机制审计、能力卡、工具权限。
- `src/tools/common`：跨智能体公共基础能力，例如 `contracts.py` 和 `runtime_setup.py`。它们不属于任一智能体，不调用 LLM，不生成策略判断、score/rank、手数、交易动作或 `final_action_contract`。
- `src/tools/common/final_action_semantics.py`：跨智能体确定性交易语义状态机，只解释已存在交易字段，不签发合约、不下单、不入账、不写研究。
- `src/tools/common/evidence_fusion_semantics.py`：跨分析师和决策链的多维预测证据解释工具，只解释证据强弱、时效、一致性、冲突、确认需求和复盘归因，不签发合约、不生成手数、不下单、不入账。
- `src/util`：更底层的通用基础设施，例如日志、数据库 helper、文本清洗、配置归一化、通用期货审计函数。

命名规则：

- `agent_tools` 下的工具必须按具体功能命名，不能按智能体名命名。
- 禁止新增 `*_tools`、`pm_*`、`trader_*`、`reviewer_tools`、`researcher_tools` 这类泛称或角色名工具。
- 跨多类智能体共享、且不表达业务动作权限的基础 helper 才能放入 `src/tools/common`。

## 六、投资组合经理工具契约

### `decision_memory_retrieval`

输入：

- `ticker`、`side`、`trading_date`；
- `horizon_class`、`market_regime`、`setup_type`；
- `consumer_scope=pm_learning`；
- 研究库中的 `alpha_setup_action_value`、`adaptive_policy_state`、相关 profile。

输出：

- `effective_memory_summary`；
- 有效 action-value 列表；
- `action_preference`、`reward_source`、`evidence_scope`、`action_value_lane`、`memory_side_role`、`reward_sum/reward_mean`、`last_sample_date`；
- 过期、空壳、非 `pm_learning` scope、未来数据、弱先验的剔除或降级原因。

边界：

- 不调用 LLM；
- 先收集可见历史，再按质量排序；
- 空历史不能占位置挡住真实历史；
- 不输出 `target_lots`、`lots_delta`、`final_action`；
- 不把 `execution` 学习当成交易员权限；execution 学习必须先由投资组合经理写入 `final_action_contract.execution_profile/entry_trigger` 后才影响交易员执行。

### `opportunity_ranking`

输入：

- `signal_collection_contract`；
- `effective_memory_summary`；
- 市场确认、数据质量、当前持仓、资金边界；
- 投资组合经理配置中的评分和资金部署规则。

输出：

- `opportunity_scorecard`；
- `opportunity_score_components`；
- `opportunity_rank`；
- `capital_allocation_reason`；
- 未入选或降级原因。

边界：

- 不调用 LLM；
- 评分和排序必须可复现；
- rank 不是交易权限，只能供投资组合经理资金部署使用；
- 不能输出最终手数或最终合约。

排名至少由以下分项组成：

- 现实预测证据：方向一致性、触发状态、证据强弱、证据冲突、缺失证据、数据质量、失效边界；
- 历史研究记忆：真实交易收益、action preference、样本数、最近样本时间、是否过期、是否同品种同方向同 setup；
- 账户与持仓状态：已有仓位、浮盈浮亏、保证金占用、净敞口、是否处于减仓/退出生命周期；
- 风控与市场确认：硬风险、软风险、流动性、盘中确认要求。

研究记忆只影响评分分项，不单独创造交易机会。当前触发不成立时，正向历史只能支持观察或条件监控；当前证据强但没有真实历史时，历史分项按冷启动中性处理；当前证据强但历史亏损明确时，排名必须降级并写入 `capital_allocation_reason`。

### `position_sizing`

输入：

- `opportunity_scorecard`、`opportunity_rank`、`capital_allocation_reason`；
- 当前持仓、账户权益、保证金、合约乘数、最大净敞口；
- 投资组合经理资金和风控配置。

输出：

- `position_sizing_result`；
- `current_lots`、建议 `target_lots`、`lots_delta`；
- `target_position_ratio`、`target_value`、`margin_required`；
- 缩手数、降级、减仓、退出或不交易原因。

边界：

- 不调用 LLM；
- 不读取研究库；
- 不改方向；
- 不签发 `final_action_contract`；
- 不绕过审计员。

投资组合经理使用三个工具输出后，仍由投资组合经理自己决定 `final_action`，并把 `current_lots/target_lots/lots_delta/final_action` 写入唯一 `final_action_contract`。

## 七、完整链路图

```text
行情 / 基本面 / 新闻 / 历史价格
        |
        v
+------------------------+   +------------------------+   +----------------------------+
| technical 技术面分析师 |   | fundamental 基本面分析师 |   | commodity_news 新闻面分析师 |
| 可调 LLM               |   | 可调 LLM                 |   | 可调 LLM                    |
| 输出 action_evidence   |   | 输出 action_evidence     |   | 输出 action_evidence        |
+------------------------+   +------------------------+   +----------------------------+
             \                         |                         /
              v                        v                        v
                    +--------------------------------+
                    | signal_collector 信号收集员   |
                    | 不调 LLM                       |
                    | 输出 signal_collection_contract |
                    +--------------------------------+
                                      |
                                      v
                    +--------------------------------+
                    | portfolio_manager 投资组合经理 |
                    | 不调 LLM                       |
                    | 读取统一结构化预测证据包       |
                    +--------------------------------+
                         |                      ^
                         v                      |
      +--------------------------------+         |
      | decision_memory_retrieval       |         |
      | 不调 LLM                       |         |
      | 输出 effective_memory_summary  |         |
      | empty_history_cannot_block_real_history |
      +--------------------------------+         |
                         |                      |
                         v                      |
      +--------------------------------+         |
      | opportunity_ranking             |         |
      | 不调 LLM                       |         |
      | 输出 scorecard/rank/reason      |         |
      | rank_is_not_trade_authority     |         |
      +--------------------------------+         |
                         |                      |
                         v                      |
      +--------------------------------+         |
      | position_sizing                 |         |
      | 不调 LLM                       |         |
      | 输出 position_sizing_result     |         |
      | no_final_action_authority       |         |
      +--------------------------------+         |
                         |                      |
                         v                      |
                    +--------------------------------+
                    | portfolio_manager 投资组合经理 |
                    | 不调 LLM                       |
                    | 签唯一 final_action_contract   |
                    +--------------------------------+
                                      |
                                      v
                           +----------------------+
                           | auditor 审计员       |
                           | 不调 LLM             |
                           +----------------------+
                                      |
                                      v
                           +----------------------+
                           | trader 交易员        |
                           | 不调 LLM             |
                           +----------------------+
                                      |
                                      v
                           +----------------------+
                           | accountant 会计师    |
                           | 不调 LLM             |
                           +----------------------+
                                      |
                                      v
                           +----------------------+
                           | reviewer 复盘员      |
                           | 不调 LLM             |
                           +----------------------+
                                      |
                                      v
                           +----------------------+
                           | researcher 研究员    |
                           | 受限可调 LLM         |
                           | 输出结构化历史学习   |
                           +----------------------+
                                      |
                                      v
                           memory / action-value DB
                           不调 LLM
                                      |
                                      v
                           下一交易日 decision_memory_retrieval
                           不调 LLM
```

## 八、研究信息消费边界

### 直接消费研究信息

| 消费者 | 消费内容 | 用途 | 边界 |
|---|---|---|---|
| `technical` 技术面分析师 | 技术分析校准类结构化研究 | 修正技术信号解释、触发质量、失效边界 | 不能生成交易动作、仓位、手数 |
| `fundamental` 基本面分析师 | 基本面因子校准类结构化研究 | 修正基本面驱动、数据新鲜度、因子可靠性 | 不能生成交易动作、仓位、手数 |
| `commodity_news` 期货新闻面分析师 | 新闻事件校准类结构化研究 | 修正新闻催化质量、影响窗口、事件有效性 | 不能生成交易动作、仓位、手数 |
| `portfolio_manager` 投资组合经理 | 交易决策类 action-value，经 `decision_memory_retrieval` 过滤 | 评分、排序、仓位决策 | 只能通过投资组合经理工具和唯一合约落地 |

### 间接消费研究信息

| 消费者 | 间接路径 | 边界 |
|---|---|---|
| `signal_collector` 信号收集员 | 读取已被分析师校准后的结构化信号 | 不直接读研究员/DB，不混入历史交易结论 |
| `auditor` 审计员 | 审投资组合经理合约里的 `learning_used` 和资金理由 | 不读研究信息改方向或手数 |
| `trader` 交易员 | 只执行审计通过的 `final_action_contract` 及其中已合约化的执行触发规则 | 不直接读取研究库、`strategy_memory`、`adaptive_policy_state` 或 action-value；不按历史好坏放宽触发、改方向或改手数 |
| `accountant` 会计师 | 只结算研究影响后的真实成交 | 不读研究信息改账 |
| `reviewer` 复盘员 | 复盘投资组合经理合约、交易员执行和结算结果 | 不用研究信息决定交易 |
| `protocol_governor` 协议管理员 | 审计结构化研究信息是否正确传到应传边界 | 不参与交易消费，不评价收益 |

研究员输出给下游的内容必须是结构化研究信息，并按直接或间接消费边界使用；持久化到研究库只是保存方式。自由文本可以解释研究原因，但不能成为下游直接消费的研究结论。

## 九、artifact 保存边界

智能体输入输出定义的是业务事实；artifact 是这些事实的持久化载体。artifact 可以引用上游记录，但不能把上游完整对象无差别复制成下游自己的输出。每个阶段落盘前必须遵守以下边界：

| artifact 所属阶段 | 可以保存 | 禁止保存 |
|---|---|---|
| PM recommendation artifact | 完整 `final_action_contract`、`learning_used`、`opportunity_scorecard`、`opportunity_rank`、`position_sizing_result`、`capital_allocation_reason`、`capital_deployment` | 第二套交易计划、交易员执行结果、收盘后事实、PM 内部评分/排序/资金部署草稿 |
| Auditor artifact | `audit_verdict`、hard/soft risk reasons、审计 payload、对 PM 合约的只读审计摘要 | 改写后的方向、改写后的手数、新建合约、研究库原始记录 |
| Trader / Phase2 artifact | 执行事实、触发事实、成交/未成交、`execution_result`、`execution_learning_trace`、来自 PM 合约的执行必要字段摘要 | 完整 `final_action_contract` 镜像、`position_sizing_result`、`capital_allocation_reason`、`opportunity_rank`、`opportunity_score`、`opportunity_score_components`、`learning_used`、`learning_adjustment_summary` |
| Transaction audit payload | 交易执行审计摘要、保证金审计、成交事实、执行触发事实 | 完整 `final_action_contract` 镜像、PM 学习解释、PM 排名解释、PM 资金部署解释 |
| Accountant artifact | 成交、费用、保证金、结算价、权益、持仓事实 | 研究学习字段、PM 排名字段、LLM 解释、交易动作改写 |
| Reviewer artifact | Phase4 验收、交易日志、事实归因、上游 artifact 引用、研究输入材料 | 最终 action-value、研究状态写入、当天合约/成交/结算改写 |
| Researcher artifact | 结构化研究成果、action-value、profile、state、执行学习、分析师校准信息 | 当天交易指令、当天合约改写、交易员权限、会计事实改写 |

核心规则：

- 完整 `final_action_contract` 只能作为 PM recommendation artifact 的策略交易事实保存。
- 交易员仍然读取审计通过后的 `final_action_contract` 执行，但 Phase2 artifact 不能把完整 PM 合约复制成自己的输出。
- 下游 artifact 如需说明来源，应使用 `recommendation_id`、`source_artifacts`、artifact 路径或执行字段摘要，而不是复制 PM 的学习、排名和资金解释字段。
- `system_invariant_audit.py` 必须检查执行 artifact 中是否出现 PM 排名、学习或资金解释字段与交易意图字段同节点混用；出现时属于非策略 hard error。
- `pg_contract_coverage_audit.py` 必须覆盖 artifact 阶段边界的 producer、consumer、audit 和 test。

## 十、关键字段契约

### 分析师到信号收集员

分析师必须输出 `action_evidence_contract`。关键字段包括：

- `signal`、`side`、`confidence`；
- `opportunity_state`；
- `trigger_valid`、`current_trigger_confirmed`；
- `entry_trigger`、`invalidation_present`、`invalidation_condition`；
- `setup_type`、`setup_quality_ok`、`horizon_class`、`market_regime`；
- `evidence_quality`、`missing_evidence`、`current_evidence_conflict`；
- `fusion_evidence`、`evidence_strength`、`evidence_freshness`、`evidence_decay_risk`、`confirmation_requirements`；
- 技术面必须补 `technical_false_breakout_risk`；基本面必须补 `fundamental_opposition_strength`；新闻面必须补 `news_impact_window` 和 `one_off_event_risk`；
- `data_usage_summary`、`no_lookahead_status`。

`setup_quality_ok=true` 只表示形态值得关注，不代表当前触发成立。`trigger_valid=true/current_trigger_confirmed=true` 才表示当前触发成立。

### 信号收集员到投资组合经理

`signal_collector` 信号收集员输出 `signal_collection_contract`。字段必须来自 `docs/unified_field_semantics.md` 已登记语义，包括：

- `ticker`、`trading_date`；
- `source_contracts`；
- `evidence_items`；
- `dominant_side`、`side_consensus`；
- `trigger_status`；
- `supporting_analysts`、`opposing_analysts`、`neutral_analysts`；
- `evidence_strength`、`evidence_conflict_level`；
- `evidence_fusion`、`evidence_strength_by_analyst`、`evidence_freshness_by_analyst`；
- `evidence_alignment_state`、`direction_alignment`、`cross_analyst_conflicts`、`dominant_opposing_evidence`、`confirmation_requirements`、`multi_evidence_consensus_score`；
- `missing_evidence`、`data_quality_flags`；
- `setup_types`、`horizon_scope`；
- `invalidation_summary`；
- `collector_decision_boundary="no_trade_authority"`。

`signal_collection_contract` 不是交易合约，不能含 `target_lots`、`lots_delta`、`final_action`、`target_position_ratio`。

### 投资组合经理到审计员到交易员

跨智能体的唯一策略交易事实是 `final_action_contract`。关键字段包括：

- `ticker`；
- `current_lots`；
- `target_lots`；
- `lots_delta`；
- `final_action`；
- `action_candidates`；
- `learning_used`；
- `opportunity_scorecard` 或其摘要；
- `position_sizing_result`；
- `capital_allocation_reason`；
- `conditional_trigger_authority`；
- `requires_intraday_confirmation`；
- `can_execute_without_intraday_trigger`；
- `reason_codes`。

交易员只能从审计通过后的 `final_action_contract.current_lots/target_lots/lots_delta/final_action` 和合约内 `execution_profile/entry_trigger/requires_intraday_confirmation/can_execute_without_intraday_trigger` 执行策略单。`opportunity_rank/opportunity_score/learning_used` 不是交易员权限，研究库、action-value、`strategy_memory`、`adaptive_policy_state` 也不是交易员触发放宽权限。

## 十一、提示词契约

提示词是智能体契约的一部分。AgentQuant 的提示词集中在 `src/llm/prompt.py` 管理；涉及智能体输入、输出、禁止项、字段语义、LLM 权限边界的改造，必须同步检查和更新提示词。

| 对象 | 是否有提示词 | 提示词用途 | 禁止项 |
|---|---|---|---|
| `technical` 技术面分析师 | 有 | 生成结构化技术预测证据 | 手数、仓位、最终交易动作、`final_action_contract` |
| `fundamental` 基本面分析师 | 有 | 生成结构化基本面预测证据 | 手数、仓位、最终交易动作、`final_action_contract` |
| `commodity_news` 期货新闻面分析师 | 有 | 生成结构化新闻预测证据 | 手数、仓位、最终交易动作、`final_action_contract` |
| `researcher` 研究员 | 有，受限 | 生成结构化研究成果和学习记录 | 当天交易指令、手数、交易员权限、自由文本研究结论供下游直接消费 |
| `signal_collector` 信号收集员 | 无 | 不调用 LLM，只做确定性证据收集 | LLM 自由判断、研究结论、rank、手数、交易动作 |
| `portfolio_manager` 投资组合经理 | 无 | 不调用 LLM，只用工具和规则签唯一合约 | LLM 自由判断或提示词驱动手数 |
| `decision_memory_retrieval` | 无 | 确定性读取结构化研究信息 | 自由文本记忆解释、交易动作、手数 |
| `opportunity_ranking` | 无 | 确定性评分和排序 | 最终手数、最终合约、交易动作 |
| `position_sizing` | 无 | 确定性计算目标手数建议 | 改方向、签合约、绕过审计员 |
| `auditor` 审计员 | 无 | 确定性审计最终合约 | 改方向、改手数、新建合约 |
| `trader` 交易员 | 无 | 执行审过的最终合约和合约化触发规则 | 改方向、改手数、读取研究库/action-value/`strategy_memory`/`adaptive_policy_state` 下单或放宽触发 |
| `accountant` 会计师 | 无 | 按成交和结算事实入账 | LLM 调账、学习改账、交易动作 |
| `reviewer` 复盘员 | 无 | 确定性复盘合约、执行和结算事实 | 下单、调仓、写最终 action-value |
| `protocol_governor` 协议管理员 | 无 | 检查契约、字段、提示词和审计覆盖 | 交易动作、手数、收益结论 |

不调用 LLM 的智能体和工具不得新增提示词入口；若代码仍保留旧提示词，改造时必须删除。

## 十二、生命周期场景

投资组合经理生成合约时必须按交易生命周期解释：

| 场景 | 合约要求 | 学习落地要求 |
|---|---|---|
| 开仓/加仓 | `target_lots` 绝对值大于 `current_lots`，`final_action=open/add` 或对应 signed lots | 正向 open 学习可进 score/rank，但必须有盘前预测证据、资金和审计员通过 |
| 条件监控 | `requires_intraday_confirmation=true`，`can_execute_without_intraday_trigger=false` | 交易员必须写盘中触发或未触发结果 |
| 持仓 | `target_lots == current_lots` 或有限调整 | 若有 hold/exit 学习，必须写明继续持有或不减仓原因 |
| 减仓/退出 | `target_lots` 向 0 收敛，`final_action=reduce/exit` | exit/保护学习应落到目标手数下降或明确解释 |
| 未入选候选 | `target_lots` 不因该候选变化 | 必须写 `capital_allocation_reason` 或未入选原因 |

减仓/退出不是新增风险资金部署，不强制要求 `opportunity_rank`。开仓/加仓、扩大交易、条件监控和资金部署场景必须保留 score/rank/资金理由，并且 `opportunity_rank`、资金部署结论、部署前后手数、资金理由必须在同一张最终 `final_action_contract` 中原子落地。PM 内部评分草稿、排序草稿和资金部署草稿不得被任何下游智能体读取，也不得进入跨智能体消息。

## 十三、回测前后验收

回测前统一运行 `src/run/pre_backtest_test.py`。该总入口只负责编排测试和控制检查，具体测试文件仍位于 `src/tests/test_*.py`，控制逻辑仍位于 `src/tools/agent_tools/control/`。回测前必须覆盖：

- 结构测试：事实入口、artifact 边界、字段迁移和智能体权限；
- 协议预检：配置、环境、能力卡和 LLM 权限；
- 契约覆盖：核心契约是否有 producer、consumer、audit、test、字段表、配置、提示词和机制文档覆盖；
- 回测前验收：唯一合约、字段语义、DB schema、账务、阶段和执行不变量；
- 静态规则测试：PM 状态转换、分析师 LLM 输出落地、会计结算公式、系统不变量样例和机制有效性样例。

回测中每日统一运行 `src/run/backtest_daily_test.py`。该总入口不再重复运行静态结构测试；它只读取已生成的 DB、artifact 和 payload，按累计交易日窗口执行真实产物审计：

- 系统不变量检查真实运行记录是否违反字段、artifact、事实入口、账务、阶段和执行边界；
- 机制有效性检查真实学习、评分、排名、合约、执行、复盘是否按生命周期场景接通；
- 不依赖真实回测产物的规则、转换、schema、权限和公式测试全部前置到 `pre_backtest_test.py`。

版本闸门稳定标记：

- `signal_collector_no_trade_authority`
- `empty_history_cannot_block_real_history`
- `rank_is_not_trade_authority`
- `no_final_action_authority`

这些标记必须由文档、代码和测试同时覆盖。缺任一项，视为版本级契约覆盖失败。
