# AgentQuant 数据与模型调用机制

更新时间：2026-06-27

本文档记录 AgentQuant 当前数据入口、模型调用边界、结构化输出要求和回测验收要求。它和 `docs/mechanism_multiagents.md`、`docs/matrix_field_semantics.md` 共同约束代码、提示词、工具和审计。

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
8. 回测前硬数据覆盖只针对交易必须依赖的 PandaAI 市场数据：交易日窗口、交易宇宙内每个品种的日线行情、开收盘价、官方结算价和主力合约映射必须可取；缺任一项属于非策略 hard error。
9. Finoview 基本面数据和本地新闻不要求每日齐全。它们按真实更新频率进入数据质量、证据强弱、缺失证据和降级理由；不能因为某品种某日没有基本面或新闻更新而阻断回测。

## 二、模型调用原则

LLM 只用于结构化理解和研究总结，不用于最终交易授权。

| 对象 | 是否调用 LLM | 允许用途 | 禁止用途 |
|---|---|---|---|
| `technical` 技术面分析师 | 是 | 用行情和技术指标生成结构化技术预测证据 | 手数、仓位、最终交易动作 |
| `fundamental` 基本面分析师 | 是 | 用基本面数据生成结构化基本面预测证据 | 手数、仓位、最终交易动作 |
| `commodity_news` 期货新闻面分析师 | 是 | 用新闻和事件生成结构化新闻预测证据 | 手数、仓位、最终交易动作 |
| `signal_collector` 信号收集员 | 否 | 确定性收集和对齐三类分析师证据 | 自由文本判断、研究结论、score/rank、手数 |
| `portfolio_manager` 投资组合经理 | 否 | 按 PM 六步确定性读取证据、判生命周期、消费学习、只对实际增加风险的候选做全市场 rank/资金部署，并签唯一合约 | LLM 判断、LLM 手数、第二套交易计划、重建 `signal_collection_contract` |
| `decision_memory_retrieval` | 否 | 确定性读取结构化研究信息 | 自由文本记忆解释、手数、交易动作 |
| `pm_ticker_side_selection` | 否 | PM 第 3 步单品种方向选择和候选质量判断 | 全市场 rank、最终手数、最终合约 |
| `pm_full_market_capital_deployment` | 否 | PM 第 5 步只对实际增加风险的候选做全市场资金 rank 和部署，包括新开仓与同方向扩大绝对手数的 `add/scale` | `wait/hold/reduce/exit`、当前反转退出腿和不增加风险条件监控的伪 rank、最终合约 |
| `pm_position_sizing` | 否 | PM 第 6 步签约前计算目标手数建议 | 改方向、签合约 |
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

不调用 LLM，不读取研究库。只读取三类分析师正式输出的 `action_evidence_contract`，生成 `signal_collection_contract`，并写明 `source_agent="signal_collector"` 与 `collector_decision_boundary="no_trade_authority"`。它不输出 score/rank、手数、仓位、交易动作或 `final_action_contract`。

### 投资组合经理

不调用 LLM。读取 `signal_collection_contract`、账户、持仓、资金、风控配置、市场确认和 PM 六步内部确定性工具输出：

- Step2 由 `pm_ticker_side_selection` 形成 `side_priority` 和 `ticker_side_priority`；
- Step3 结合持仓形成候选质量和内部生命周期分流；
- Step4 由 `decision_memory_retrieval` 输出 `effective_memory_summary` 和完整 canonical 候选学习池；
- Step5 只对实际增加风险的候选调用 `pm_full_market_capital_deployment` 和 `pm_position_sizing`，把 rank、预算和 `position_sizing_result` 写回同一个 PM 内存状态；
- Step6 原子装配最终合约和两个最终检查。

投资组合经理是唯一策略交易合约签发者。Step1–5 只更新同一个 PM 内存状态；Step6 原子返回唯一 `FuturesRecommendation`，唯一 `final_action_contract` 位于 `signal_snapshot.final_action_contract`。缺少 `signal_collection_contract` 或 source_agent/boundary 不合法时，PM 应 fail-fast，不能自行重建证据包。

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
- `capital_deployment`；
- `capital_allocation_reason`；
- `conditional_trigger_authority`；
- `requires_intraday_confirmation`；
- `can_execute_without_intraday_trigger`；
- `reason_codes`。

`opportunity_rank` 和 `opportunity_score` 只用于投资组合经理新增风险资金部署解释，不是交易员执行权限。`wait/hold/reduce/exit`、当前反转退出腿和不增加风险的条件监控不得伪造 rank 或空 `capital_deployment`；新开仓与同方向扩大绝对手数的 `add/scale` 都必须把 Step5 资金部署事实原子写入同一张 `final_action_contract`。

## 七、系统事实载体契约

系统事实指已经被授权事实入口正式写入 DB、artifact 或 payload，且作为下游可信事实被消费的结构化结果。DB、artifact 和 payload 只是事实载体，不是新的事实来源；同一类事实必须服从同一个授权写入口和同一套字段语义。

控制审计、机制审计和回测前验收只能读取已由授权事实入口产生的标准事实，并按 `src/tools/agent_tools/control/pg_db_schema_contract.py` 和 `docs/matrix_field_semantics.md` 检查；不能生成业务事实，不能写业务表，不能猜测 DB 字段，不能创建交易权限。

| 事实载体 | 保存的系统事实 | 标准日期字段 | 授权写入口 | 允许保存 | 禁止保存或改写 |
|---|---|---|---|---|---|
| 分析师报告 artifact / `action_evidence_contract` / `artifact_json` | 盘前预测证据事实 | `trading_date` | 技术面分析师、基本面分析师、期货新闻面分析师 | 方向、触发、证据强弱、缺失、冲突、失效边界、数据可见性 | 手数、仓位、最终交易动作、`final_action_contract` |
| 信号收集 artifact / `signal_collection_contract` | 统一结构化证据事实 | `trading_date` | 信号收集员 | `source_agent="signal_collector"`、`collector_decision_boundary="no_trade_authority"`、来源引用、逐条证据、方向汇总、触发状态、证据强弱、冲突、缺失、风险、失效边界 | 历史学习结论、score/rank、仓位、手数、交易动作、`final_action_contract`；PM 或其他模块重建证据包 |
| `futures_recommendation` | PM 策略交易事实；换月/强平等运营推荐事实；独立 Auditor 审计事实载体 | `trading_date` | PM Step6 返回唯一 `FuturesRecommendation`；独立 Auditor 写 `audit_payload` / auditor 摘要；保存层负责物理化；换月/强平运营入口写运营事实 | PM 策略路径只保存完整 `signal_snapshot.final_action_contract`、原始 SCC 和两个最终检查；Auditor 只能保存 `audit_verdict`、hard/soft risk reasons 和只读审计 payload；运营路径只保存运营动作事实 | Step1–5 内存状态进入持久化载体；Phase2、Phase3、Phase4 或研究入口改写 PM 策略合约；Auditor 改方向/改手数/新建合约；运营推荐伪装成 PM 策略评分 |
| `futures_intraday_decision` / Phase2 `features_json` | 盘中触发和执行判断事实 | `trading_date` | 交易员执行入口 | 触发是否成立、执行摘要、盘中行情、执行原因、成交/未成交依据 | 完整 `final_action_contract` 镜像、`learning_used`、`opportunity_rank`、`opportunity_score*`、`capital_allocation_reason`、`position_sizing_result` |
| `futures_transactions` / transaction `audit_payload` | 交易员执行事实 | `trading_date` | 交易员成交写入入口 | 成交/未成交、动作、手数、品种、执行审计摘要、执行结果 | 完整 PM 合约镜像、PM 学习解释、PM 排名、PM 资金部署理由、研究记录 |
| `daily_settlement` / `ticker_daily_pnl` | 会计师结算事实 | `trading_date` | 会计师结算入口 | PnL、手续费、保证金、权益、持仓快照、分品种盈亏 | 学习字段、LLM 字段、交易授权、研究结论 |
| `trading_day_phase` | 阶段状态事实 | `trading_date` | 四阶段运行脚本 | Phase1/2/3/4 的 started/completed/failed 状态和消息 | 研究表写入、学习刷新、策略记忆 retention 清理 |
| 复盘日志 artifact / Phase4 payload | 复盘事实和事实归因 | `trading_date` | 复盘员 Phase4 入口 | 链路验收、交易日志、事实归因、上游事实 ID/path 或必要摘要 | 写 action-value、写策略记忆、改推荐、改成交、改结算 |
| `alpha_setup_action_value` | 交易决策类和校准类结构化研究成果 | `last_sample_date` | 研究员学习入口 / `research_memory_writers` | action-value、`canonical_action_family`、`action_value_lane/learning_lane`、消费边界、奖励来源、证据作用域、PM 合约作为学习证据 | 修改当天 PM 合约、成交、结算或复盘事实；从裸 `action_name` 私自猜动作家族 |
| `adaptive_policy_state` | 未来可用的结构化研究状态 | `source_trading_date` | 研究员学习入口 / `research_memory_writers` | 研究状态、策略校准状态、来源交易日、研究 payload | 交易员直接读取下单或放宽触发；Phase4 自动刷新 |
| `researcher_llm_notes` | 研究员 LLM 研究 notes | `trading_date` | 研究员学习入口 | 研究输入、研究输出、结构化研究 payload、raw prompt/response | 当天交易指令、手数、成交、结算改写 |

`execution_contract` 只能作为交易员触发/执行配置摘要使用，不是第二张交易合约。它只能从已审计的 `final_action_contract` 中抽取执行规则字段，例如 `execution_profile`、`trigger_source`、`entry_trigger`、`invalidation`、`valid_until`、`requires_intraday_confirmation`、`can_execute_without_intraday_trigger`、`authority_type`、`max_allowed_margin_ratio`、执行相关 `reason_codes`、`execution_action_value_preference` 和 `analyst_execution_roles`；不得携带 `target_lots`、`lots_delta`、`final_action`、`learning_used`、`opportunity_rank`、`opportunity_score*`、`capital_allocation_reason`、`position_sizing_result` 或 PM 学习解释。交易员执行动作和手数摘要只能来自已审计 `final_action_contract` 的必要执行字段摘要，不能由 `execution_contract` 单独授权。

Transaction audit payload 可以保存交易员执行事实和执行审计摘要，不能保存完整 PM 合约副本。需要追溯上游来源时，只能记录 `recommendation_id`、上游 artifact path 或必要执行摘要。

研究员可以在研究学习事实中保存完整 PM 合约作为上游证据，用于未来分析与决策策略迭代；该保存只能发生在研究员学习入口，不能发生在 Phase4 复盘入口，也不能反向修改当天推荐、成交、结算、复盘或阶段状态。

artifact 边界校验必须按值类型和载体语义识别事实，不能只按字段名黑名单判断。字段名出现在数字计数、字符串状态、错误摘要、空列表或上游 ID/path 中，不等于事实对象进入该载体；非空 dict/list 才可能承载可被下游消费的事实对象。真正的 `alpha_setup_action_value`、`adaptive_policy_state`、`researcher_llm_notes` 对象仍只能由研究员学习入口写入。

## 八、验收要求

回测前必须检查：

- 字段语义表、生产端、消费端、提示词、审计和测试是否同步；
- `signal_collector_no_trade_authority`；
- `empty_history_cannot_block_real_history`；
- `rank_is_not_trade_authority`；
- `no_final_action_authority`；
- 投资组合经理不调用 LLM；
- 交易员不读研究库或研究记录下单；
- 研究员输出必须结构化；
- 回测区间内交易宇宙每个品种的 PandaAI 日线行情、开收盘价、官方结算价和主力合约映射必须通过硬覆盖检查；
- 基本面和新闻只检查可见性、时间边界和质量降级，不做每日齐全硬拦。

回测中每日必须检查：

- 无前视数据；
- 合约、执行、结算、复盘链路一致；
- 学习是否按生命周期正确落地；
- 减仓/退出不被开仓 rank 规则误杀；
- 条件监控必须写出盘中触发或未触发事实。

## PG 审计数据模型补充（2026-07-07）

PG 审计读取的是已落地 artifact 和数据库事实，不生成新的交易事实。对 futures recommendation 的 PM 部分，运行期审计只承认四类保存链字段：`signal_snapshot.final_action_contract`、`signal_snapshot.pm_six_step_trace.pm_contract_self_check`、`signal_snapshot.pm_six_step_trace.step6_contract_generation_check`、`signal_snapshot.signal_collection_contract`。

PM 中间态字段不得成为保存 artifact：`pm_internal_candidate`、`pm_internal_candidate_contract`、`pm_capital_deployment_decision`、`pm_internal_draft`、`pm_scoring_draft`、`pm_ranking_draft`、`pm_capital_deployment_draft`。PG 可以检查这些字段是否污染保存结果，但不能根据 PM reason code 重新推断交易动作是否正确。

回测前检测不读取真实行情、不调 LLM、不写真实 DB；真实行情覆盖、执行、结算和复盘事实只在实际运行或每日后置只读审计中检查。
