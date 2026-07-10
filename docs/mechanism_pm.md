# PM 内部机制

## 一、输入

### 1. 统一证据输入

内容：`signal_collection_contract`

生产者：`signal_collector_agent`

传递者：`workflow` 编排层。`workflow` 不是智能体，只负责把 signal collector 产物写入运行时 state。

接收位置：`state["signal_collection_contract"]`

### 2. 产品身份输入

内容：`ticker`、`trading_date`、`config_id`

生产者：`workflow` 编排层

接收位置：`state["ticker"]`、`state["trading_date"]`、`state["config_id"]`

### 3. 账户和持仓输入

内容：`portfolio`

生产者：DB 最新结算组合

传递者：`workflow` 编排层

接收位置：`state["portfolio"]`

### 4. 盘前价格输入

内容：`morning_price_context`

生产者：`workflow` 编排层调用 `Router.resolve_pre_open_reference_price`

接收位置：`state["morning_price_context"]`

价格口径：Phase1 盘前计划参考价，当前代码取上一交易日收盘价，字段为 `base_price`、`base_price_source`、`base_price_date`、`prev_close_price`。

### 5. 运行配置输入

内容：`config`、`full_config`

生产者：`workflow` 编排层

接收位置：`state["config"]`、`state["full_config"]`

### 6. 合约基础信息输入

内容：合约乘数、保证金率、主力合约信息

生产者：合约信息缓存

### 7. 学习成果输入

内容：action-value、setup/profile、历史 episode 摘要

生产者：Researcher 写入 research DB

### 8. 分析师信号引用

内容：`analyst_signals`

生产者：三类分析师

传递者：`workflow` 编排层

接收位置：`state["analyst_signals"]`

定死边界：

`state["signal_collection_contract"]` 是 PM 交易证据主入口。

`state["analyst_signals"]` 不是 PM 交易证据入口。

PM 不直接读取行情原始序列、基本面原始数据、新闻原文作为交易判断输入；这些信息先由分析师结构化，再由 signal collector 汇总进 `signal_collection_contract`。

## 二、输出

### 1. 最终合约

#### 1.1 传出方式

最终合约只由 PM 第 6 步一次性生成。

最终合约先进入内存中的 `FuturesRecommendation.signal_snapshot.final_action_contract`。

`workflow` 只接收 PM 返回的 `FuturesRecommendation`，再负责保存 DB 和本地 artifact。

最终合约生成时不读取 DB 中已经落盘的 PM 输出，不读取本地 artifact，不读取运行日志。

#### 1.2 包含内容

`final_action_contract` 必须包含：

- `contract_version`
- `producer`
- `trading_date`
- `config_id`
- `ticker`
- `underlying_code`
- `contract_code`
- `source_type`
- `final_action`
- `action_family`
- `contract_lifecycle_port`
- `current_lots`
- `target_lots`
- `lots_delta`
- `position_change_direction`
- `side`
- `direction`
- `direction_source`
- `authority`
- `execution_trigger_state`
- `execution_trigger_condition`
- `execution_timing_rule`
- `conditional_trigger_authority`
- `trigger_valid_until`
- `trigger_cancel_condition`
- `invalidation_boundary`
- `risk_boundary`
- `stop_reference`
- `risk_reason_codes`
- `do_not_trade_reason`
- `rejection_reason`
- `base_price`
- `base_price_source`
- `base_price_date`
- `prev_close_price`
- `price_basis_warning`
- `contract_multiplier`
- `margin_rate`
- `notional_value`
- `estimated_margin`
- `margin_delta`
- `post_trade_margin_estimate`
- `evidence_used`
- `evidence_understanding`
- `signal_collection_contract_ref`
- `evidence_alignment_state`
- `multi_evidence_consensus_score`
- `cross_analyst_conflict_count`
- `missing_evidence_count`
- `resolution_effect`
- `learning_used`
- `alpha_setup_action_values`
- `memory_retrieval`
- `lifecycle_learning_trace`
- `decision_learning_rows`
- `trigger_profile_learning_rows`
- `opportunity_rank`
- `rank_source`
- `rank_lifecycle`
- `side_priority`
- `ticker_side_priority`
- `side_priority_score`
- `scorecard_score`
- `scorecard_preferred_side`
- `capital_deployment`
- `capital_layer`
- `budget_approved`
- `budget_rejection_reason`
- `allocated_budget`
- `target_margin`
- `position_sizing_result`
- `sizing_method`
- `sizing_constraints`
- `max_lots_allowed`
- `target_lots_before_constraints`
- `target_lots_after_constraints`
- `lineage`
- `source_lineage_context`
- `generated_at`
- `pm_contract_generation_mode`

#### 1.3 来源

最终合约内容只来自 PM 内部候选状态。

各内容来源固定：

产品、日期、配置、合约身份：来自 `state["ticker"]`、`state["trading_date"]`、`state["config_id"]` 和合约信息缓存。

`final_action`、`current_lots`、`target_lots`、`lots_delta`：来自第 6 步对最终候选状态的合约化结果。

方向、交易状态、生命周期口：来自第 2、3 步写回的候选状态。

执行触发条件：来自 SCC 中的 trigger 信息和第 3 步交易状态判断。

失效边界、风险边界：来自 SCC 的 invalidation、risk 信息。

计划参考价：来自 `morning_price_context`。

合约基础信息：来自 `FuturesContractInfoCache.get_contract_info`。

证据摘要：来自第 1 步 `evidence_understanding`。

学习使用摘要：来自第 4 步学习检索结果。

排名与预算分配摘要：来自第 5 步新增风险排序与预算分配结果。

仓位测算结果：来自第 5 步 position sizing 结果。

来源链路：来自 SCC source refs、分析师引用完整性校验和 PM 生成上下文。

### 2. 物理材料输出

#### 2.1 返回对象

##### 2.1.1 落点

PM 第 6 步返回给 `workflow` 的内存对象：`FuturesRecommendation`。

##### 2.1.2 包含内容

- `id`
- `config_id`
- `reference_portfolio_id`
- `trading_date`
- `effective_trade_date`
- `source_type`
- `underlying_code`
- `from_contract`
- `to_contract`
- `contract_code`
- `action`
- `lots`
- `base_price`
- `base_price_source`
- `base_price_date`
- `open_price`
- `prev_close_price`
- `slippage_model`
- `slippage_ticks`
- `slippage_amount`
- `execution_price`
- `justification`
- `signal_snapshot`
- `audit_payload`
- `warning_message`
- `status`
- `created_at`

##### 2.1.3 来源

来自 PM 第 6 步对最终候选状态的合约化结果。

`FuturesRecommendation` 是 PM 对 `workflow` 的直接返回值，不是缓存、DB 记录或本地 artifact。

`workflow` 接收后，才把它保存为 `futures_recommendation` DB 记录和本地 recommendation artifact。

#### 2.2 DB 推荐记录

##### 2.2.1 落点

`futures_recommendation` 表。

##### 2.2.2 包含内容

- `id`
- `config_id`
- `reference_portfolio_id`
- `trading_date`
- `effective_trade_date`
- `source_type`
- `underlying_code`
- `from_contract`
- `to_contract`
- `contract_code`
- `action`
- `lots`
- `base_price`
- `base_price_source`
- `base_price_date`
- `open_price`
- `prev_close_price`
- `slippage_model`
- `slippage_ticks`
- `slippage_amount`
- `execution_price`
- `justification`
- `status`
- `warning_message`
- `signal_snapshot`
- `signal_snapshot_artifact_path`
- `signal_snapshot_sha256`
- `signal_snapshot_size`
- `signal_snapshot_summary_json`
- `audit_payload`
- `audit_payload_artifact_path`
- `audit_payload_sha256`
- `audit_payload_size`
- `audit_payload_summary_json`
- `created_at`

##### 2.2.3 来源

来自 `workflow` 接收的 `FuturesRecommendation`，由保存层写入 `futures_recommendation` 表。

其中 `action`、`lots`、`base_price`、`signal_snapshot` 必须由 `final_action_contract` 对齐生成。

#### 2.3 signal_snapshot

##### 2.3.1 落点

`futures_recommendation.signal_snapshot`。

##### 2.3.2 包含内容

- 三类分析师结构化信号快照。
- `pm_raw_rationale`
- `signal_collection_contract`
- `position_budget_policy`
- `release_block_diagnostics`
- `market_confirmation`
- `data_quality_summary`
- `data_quality_summary_path`
- `horizon_scope`
- `opportunity_scorecard`
- `pm_raw_rationale_semantic_audit`
- `pm_semantic_consistency_gate`
- `active_opportunity_audit`
- `business_quality_summary`
- `trade_research_contracts`
- `pm_research_contract_summary`
- `pm_internal_message_contract`
- `pm_internal_message_validation_errors`
- `audit`
- `pm_justification_contract`
- snapshot header / lineage contract
- `final_action_contract`
- `pm_six_step_trace`
- `auditor`
- 必要的 recommendation-level 摘要字段

##### 2.3.3 来源

`signal_collection_contract` 来自 `state["signal_collection_contract"]` 原始 SCC。

`final_action_contract` 来自第 6 步最终合约。

`pm_six_step_trace` 来自 PM 内部候选状态演化摘要。

`auditor` 来自独立 Auditor 审计结果。

recommendation-level 摘要字段来自最终合约，不另造第二套事实。

#### 2.4 本地 recommendation artifact

##### 2.4.1 落点

`src/logs/artifacts/{config_id}/{trading_date}/recommendation/`

##### 2.4.2 包含内容

- recommendation payload JSON
- signal_snapshot JSON
- audit_payload JSON
- final_action_contract
- pm_six_step_trace
- signal_collection_contract
- auditor
- signal_snapshot_artifact_path
- signal_snapshot_sha256
- signal_snapshot_size
- signal_snapshot_summary_json
- audit_payload_artifact_path
- audit_payload_sha256
- audit_payload_size
- audit_payload_summary_json

##### 2.4.3 来源

来自保存层对 `signal_snapshot`、`audit_payload` 等大 JSON 的 externalize 外置镜像。

本地 recommendation artifact 不是 PM 再生成的第二份输出。

本地 artifact 是 DB 输出的可读镜像，不参与交易决策。

#### 2.5 运行日志

##### 2.5.1 落点

`src/logs/` 下运行日志。

##### 2.5.2 包含内容

- `trading_date`
- `config_id`
- `ticker`
- `run_id`
- `log_namespace`
- `pm_progress`
- `pm_step_progress`
- `scc_read_validation_result`
- `input_missing_block_reason`
- `pre_open_reference_price_missing_reason`
- `contract_info_missing_reason`
- `data_quality_summary_path`
- `memory_retrieval_summary`
- `full_market_rank_start_finish`
- `capital_deployment_result`
- `final_contract_generation_result`
- `pm_contract_self_check_result`
- `auditor_result_summary`
- `db_persistence_result`
- `artifact_externalization_result`
- `contract_generation_block_reason`
- `persistence_block_reason`
- `error_stack`
- `exception_context`
- `runtime_elapsed`
- `runtime_notice`

##### 2.5.3 来源

来自 PM、workflow、Auditor、持久化过程的 logger。

运行日志只用于排查，不是交易事实来源。

### 3. 定死口径

PM 只有第 6 步生成最终合约。

PM 第 6 步对外返回 `FuturesRecommendation`。

`FuturesRecommendation.signal_snapshot` 承载最终合约、原始 SCC 快照和 PM 摘要 trace。

DB 记录、本地 artifact 和运行日志都由 workflow / 保存层基于 `FuturesRecommendation` 物理化生成，不是 PM 第二次输出。

Step1 到 Step5 不输出物理交易事实。

Step1 到 Step5 只更新同一个 PM 内部候选状态。

排名和预算分配是内部候选状态的一部分，不是独立输出。

最终合约生成时只读取内部候选状态。

最终合约生成时不读取已落盘 DB 输出。

最终合约生成时不读取本地 artifact。

最终合约生成时不读取运行日志。

DB、artifact、日志都是最终合约生成之后的物理材料。

`final_action_contract` 是唯一交易真相。

`signal_snapshot.signal_collection_contract` 是 PM 使用的原始 SCC 快照。

`pm_six_step_trace` 只解释候选状态演化，不生成交易动作。

recommendation-level 摘要字段必须来自 `final_action_contract`，不能形成第二套交易事实。

## 三、内部结构

### 1. 读取统一证据

#### 1.1 读取入口

PM 从 `state["signal_collection_contract"]` 读取统一证据。

PM 从 `state["ticker"]`、`state["trading_date"]`、`state["config_id"]` 读取产品身份。

PM 从 `state["portfolio"]` 读取账户、持仓、权益、保证金和可用风险空间。

PM 从 `state["morning_price_context"]` 读取 Phase1 盘前计划参考价。

PM 从 `state["config"]`、`state["full_config"]` 读取风险参数、预算参数和 PM 策略参数。

PM 按 `ticker` 调 `FuturesContractInfoCache.get_contract_info` 读取合约乘数、保证金率和主力合约信息。

PM 从 `state["analyst_signals"]` 读取 SCC 来源引用材料。

#### 1.2 输入校验

PM 校验 SCC 的 `producer="signal_collector"`。

PM 校验 SCC 的 `collector_decision_boundary="no_trade_authority"`。

PM 校验 SCC 与 `analyst_signals` 的来源引用完整。

PM 校验 `morning_price_context.base_price` 能作为 Phase1 盘前计划参考价。

PM 校验合约基础信息存在，且能支持手数、保证金和风险测算。

#### 1.3 PM 如何理解 SCC

PM 对 SCC 的理解写入 `evidence_understanding`：

- `preferred_direction`：多、空、退出、观望。
- `trigger_state`：已触发、等待触发、未触发。
- `trigger_condition`：进入交易需要满足的触发条件。
- `evidence_strength`：证据强弱。
- `evidence_quality`：证据质量。
- `alignment_state`：证据一致性。
- `conflict_summary`：主要冲突。
- `confirmation_requirements`：仍需确认的条件。
- `missing_evidence`：缺失证据。
- `risk_factors`：主要风险。
- `invalidation_boundary`：失效边界。
- `profile_usage`：profile 使用痕迹。
- `evidence_fusion_summary`：融合摘要。

PM 只解释 SCC 已经给出的结构化证据，不回读分析师原始文本，不补造 SCC 没有给出的交易证据。

#### 1.4 PM 如何理解账户、价格、合约和配置

PM 对账户和持仓的理解写入 `position_context`：

- 当前手数。
- 当前方向。
- 当前保证金占用。
- 可用风险空间。
- 当前仓位与 SCC 方向的关系。

PM 对盘前价格的理解写入 `price_basis_context`：

- `base_price`。
- `base_price_source`。
- `base_price_date`。
- `prev_close_price`。
- `warning_message`。
- Phase1 价格只用于计划测算，不代表真实成交价。

PM 对合约信息的理解写入 `contract_context`：

- 合约乘数。
- 保证金率。
- 主力合约信息。
- 合约基础校验结果。

PM 对运行参数的理解写入 `config_context`：

- 单品种风险上限。
- 总保证金上限。
- 预算分配参数。
- PM 策略参数。

#### 1.5 PM 如何理解分析师信号引用

PM 对分析师信号引用的理解写入 `source_lineage_context`：

- 分析师输出是否齐全。
- SCC 来源引用是否能对应分析师输出。
- artifact 引用是否完整。
- 该部分只证明 SCC 来源完整，不生成交易判断。

#### 1.6 本步输出

PM 把上述理解写入同一个产品候选状态。

候选状态是 PM 内部主链对象，后续步骤继续改写同一个对象，不再另起一套交易事实。

本步输出不是最终合约。

候选状态继续传入第 2 步。

最终物理输出只来自第 6 步后的 `FuturesRecommendation`：原始 SCC 进入 `FuturesRecommendation.signal_snapshot.signal_collection_contract`，可执行交易事实进入 `FuturesRecommendation.signal_snapshot.final_action_contract`，候选状态演化摘要进入 `FuturesRecommendation.signal_snapshot.pm_six_step_trace`。

DB 记录和本地 artifact 由 workflow / 保存层基于 `FuturesRecommendation` 持久化生成，不是本步输出。

#### 1.7 禁止项

PM 不改写 SCC。

PM 不重建 SCC。

PM 不从 `state["analyst_signals"]` 判断方向、触发、风险、失效边界。

PM 不直接读取行情原始序列、基本面原始数据、新闻原文作为交易判断输入。

PM 不读取已落盘 DB 推荐记录作为本次最终合约输入。

PM 不读取本地 recommendation artifact 作为本次最终合约输入。

PM 不读取运行日志作为本次最终合约输入。

PM 不在第 1 步输出 `FuturesRecommendation`。

PM 不在第 1 步输出 `final_action_contract`。

PM 不在第 1 步输出 DB 记录、本地 artifact 或运行日志物理事实。

PM 不把第 1 步候选状态暴露给 workflow、Auditor、Trader、Reviewer、Researcher 或 PG 作为外部事实。

### 2. 判断产品方向

### 3. 结合持仓确定交易状态

### 4. 读取学习成果修正候选质量

### 5. 新增风险排序与预算分配

### 6. 生成唯一最终交易合约
