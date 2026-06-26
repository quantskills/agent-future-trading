# AgentQuant 数据与模型调用机制

更新时间：2026-06-25

本文档记录 AgentQuant 当前数据入口、模型调用边界、结构化输出要求和回测验收要求。它和 `docs/mechanism_multiagents.md`、`docs/unified_field_semantics.md` 共同约束代码、提示词、工具和审计。

## 一、数据调用原则

当前运行数据源只包括：

- PandaAI：期货行情、分钟线、结算相关行情和期货衍生数据；
- Finoview 本地 feather：基本面数据；
- 本地新闻 txt：新闻面证据；
- 研究库：Phase4 后持久化的结构化研究信息。

数据规则：

1. Phase1 盘前策略只能读取 T-1 及以前可见信息。
2. Phase2 盘中执行只能读取当时已经发生的 T 日盘中数据。
3. Phase3 结算后才能读取当日官方结算数据。
4. Phase4 复盘只能输出交易日志、事实归因和研究输入材料；Phase4 标记 completed 不触发 `strategy_memory` 刷新、学习 retention 清理或研究表写入。
5. 缓存只减少重复读取，不改变数据可见性。
6. 数据缺口必须显式记录，不能把“没数据”伪造成 Bullish/Bearish。
7. 学习记录必须保留当时使用的数据依据、字段、质量状态和 no-lookahead 状态。

## 二、模型调用原则

LLM 只用于结构化理解和研究总结，不用于最终交易授权。

| 对象 | 是否调用 LLM | 允许用途 | 禁止用途 |
|---|---|---|---|
| `technical` 技术面分析师 | 是 | 用行情和技术指标生成结构化技术预测证据 | 手数、仓位、最终交易动作 |
| `fundamental` 基本面分析师 | 是 | 用基本面数据生成结构化基本面预测证据 | 手数、仓位、最终交易动作 |
| `commodity_news` 期货新闻面分析师 | 是 | 用新闻和事件生成结构化新闻预测证据 | 手数、仓位、最终交易动作 |
| `signal_collector` 信号收集员 | 否 | 确定性收集和对齐三类分析师证据 | 自由文本判断、研究结论、score/rank、手数 |
| `portfolio_manager` 投资组合经理 | 否 | 确定性融合证据、研究、评分、手数和风控，签唯一合约 | LLM 判断、LLM 手数、第二套交易计划 |
| `decision_memory_retrieval` | 否 | 确定性读取结构化研究信息 | 自由文本记忆解释、手数、交易动作 |
| `opportunity_ranking` | 否 | 确定性评分和排序 | 最终手数、最终合约 |
| `position_sizing` | 否 | 确定性计算目标手数建议 | 改方向、签合约 |
| `auditor` 审计员 | 否 | 审计最终合约和硬风险 | 改方向、改手数、新建合约 |
| `trader` 交易员 | 否 | 执行审计通过的最终合约和合约化触发规则 | 读取研究库/action-value/`strategy_memory`/`adaptive_policy_state` 下单或放宽触发、改方向、改手数 |
| `accountant` 会计师 | 否 | 按成交和结算事实入账 | LLM 调账、学习改账 |
| `reviewer` 复盘员 | 否 | 复盘合约、执行、结算事实 | 下单、调仓、写最终 action-value |
| `researcher` 研究员 | 可调但受限 | 基于复盘事实生成结构化研究成果 | 当天交易指令、手数、交易员权限 |
| `protocol_governor` 协议管理员 | 否 | 契约、字段、提示词和审计覆盖检查 | 交易动作、收益结论 |

结构化字段不是 LLM 推理上限；结构化字段是 LLM 结果的落地格式。LLM 可以解释冲突、反事实和不确定性，但进入交易链路的内容必须落到登记字段。

### 工具目录边界

`src/tools/agent_tools` 只放按业务功能分类的智能体工作流工具，子目录包括 `analysis`、`decision`、`execution`、`research`、`control`。工具名必须表达具体功能，不能用智能体名或 `*_tools` 泛称命名。

`src/tools/common` 只放跨智能体公共基础能力，例如 `contracts.py` 和 `runtime_setup.py`。这些 helper 不属于任一业务智能体，不调用 LLM，不生成 score/rank、手数、交易动作或 `final_action_contract`。

`src/util` 放更底层的通用基础设施，例如日志、数据库 helper、文本清洗、配置归一化和通用期货审计函数。

## 三、启用智能体的数据边界

### 技术面分析师

读取 PandaAI 盘前可见行情、技术指标、技术学习校准。输出 `AnalystSignal` 和 `action_evidence_contract`。可以调用 LLM，但只能输出结构化技术预测证据。

### 基本面分析师

读取 Finoview 基本面数据、PandaAI 衍生因子、基本面学习校准。输出 `AnalystSignal` 和 `action_evidence_contract`。可以调用 LLM，但只能输出结构化基本面预测证据。

### 期货新闻面分析师

读取本地新闻 txt、事件上下文、新闻学习校准。输出 `AnalystSignal` 和 `action_evidence_contract`。可以调用 LLM，但只能输出结构化新闻预测证据。

### 信号收集员

不调用 LLM，不读取研究库。只读取三类分析师正式输出的 `action_evidence_contract`，生成 `signal_collection_contract`。它不输出 score/rank、手数、仓位、交易动作或 `final_action_contract`。

### 投资组合经理

不调用 LLM。读取 `signal_collection_contract`、账户、持仓、资金、风控配置、市场确认和三个确定性工具输出：

- `decision_memory_retrieval` 输出 `effective_memory_summary`；
- `opportunity_ranking` 输出 `opportunity_scorecard`、`opportunity_rank` 和 `capital_allocation_reason`；
- `position_sizing` 输出 `position_sizing_result`。

投资组合经理是唯一策略交易合约签发者，只能输出一张 `final_action_contract`。

### 审计员

不调用 LLM。只审计投资组合经理签发的 `final_action_contract`，输出 `audit_verdict` 和审计 payload。不能改方向、改手数或新建合约。

### 交易员

不调用 LLM。只执行审计通过的 `final_action_contract` 及其中已合约化的执行触发规则，输出 `execution_result` 和 `execution_learning_trace`。不能读取研究库、action-value、`strategy_memory` 或 `adaptive_policy_state` 下单或放宽触发。

### 会计师

不调用 LLM。只按成交、结算价、费用、保证金率、合约乘数入账，输出 `daily_settlement`、PnL、费用、保证金、权益和持仓事实。

### 复盘员

不调用 LLM。只复盘合约、执行、结算和阶段事实，输出 Phase4 验收、交易日志、事实归因和研究输入材料；不能在复盘员入口触发研究员学习或研究员 LLM。

### 研究员

可受限调用 LLM。通过 `run/research/researcher_learning.py` 在复盘员 Phase4 验证完成后单独运行，基于复盘事实、完整 episode、未交易机会、未触发条件机会和执行结果，输出并持久化结构化研究信息，包括：

- 分析师校准类研究；
- 交易决策类 `alpha_setup_action_value`；
- `alpha_setup_profile`；
- `adaptive_policy_state`。

研究员不能修改当天合约、手数或交易员权限。

### 协议管理员

不调用 LLM。只运行契约覆盖、机制断链、字段语义、系统不变量和回测前非策略风险检查。不能参与交易动作或收益判断。

## 四、研究信息边界

研究员必须输出结构化研究信息；持久化到研究库只是保存方式。自由文本只能解释，不是下游直接消费的研究结论。

直接消费研究信息：

- 技术面分析师：只消费技术分析校准类研究；
- 基本面分析师：只消费基本面因子校准类研究；
- 期货新闻面分析师：只消费新闻事件校准类研究；
- 投资组合经理：只经 `decision_memory_retrieval` 消费交易决策类 action-value。

间接消费研究信息：

- 信号收集员只读取已被分析师校准后的结构化信号；
- 审计员只审合约里的 `learning_used` 和资金理由；
- 交易员只执行已吸收研究影响后的合约和合约化触发规则；
- 会计师只结算真实成交；
- 复盘员只复盘合约、执行和结算结果；
- 协议管理员只审计结构化研究信息是否正确传递。

## 五、历史学习读取原则

`decision_memory_retrieval` 是投资组合经理读取历史学习的唯一工具入口。

必须满足：

- 只读取 `consumer_scope=pm_learning` 的结构化 action-value；
- 只读取早于当前交易日的记录；
- 先收集可见历史，再按质量排序；
- 空壳历史、无收益历史、无偏好历史不能挡住真实有效历史；
- 真实交易、有收益、有明确正负偏好、未过期、品种方向匹配的记录优先；
- `execution` 学习不能变成交易员权限；
- 输出 `effective_memory_summary` 和剔除/降级原因。

这条原则对应版本闸门标记：`empty_history_cannot_block_real_history`。

## 六、唯一交易事实

策略交易事实只认投资组合经理签发、审计员审过的 `final_action_contract`。关键字段包括：

- `ticker`；
- `current_lots`；
- `target_lots`；
- `lots_delta`；
- `final_action`；
- `learning_used`；
- `opportunity_scorecard` 或其摘要；
- `position_sizing_result`；
- `capital_allocation_reason`；
- `conditional_trigger_authority`；
- `requires_intraday_confirmation`；
- `can_execute_without_intraday_trigger`；
- `reason_codes`。

`opportunity_rank` 和 `opportunity_score` 只用于投资组合经理资金部署解释，不是交易员执行权限。

## 七、验收要求

回测前必须检查：

- 字段语义表、生产端、消费端、提示词、审计和测试是否同步；
- `signal_collector_no_trade_authority`；
- `empty_history_cannot_block_real_history`；
- `rank_is_not_trade_authority`；
- `no_final_action_authority`；
- 投资组合经理不调用 LLM；
- 交易员不读研究库或研究记录下单；
- 研究员输出必须结构化。

回测中每日必须检查：

- 无前视数据；
- 合约、执行、结算、复盘链路一致；
- 学习是否按生命周期正确落地；
- 减仓/退出不被开仓 rank 规则误杀；
- 条件监控必须写出盘中触发或未触发事实。
