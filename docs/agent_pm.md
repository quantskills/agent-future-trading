# PM 内部机制

## 一、输入

### 1. 统一证据输入

内容：`signal_collection_contract`

生产者：`signal_collector`

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

`final_action_contract` 字段按真实层级分组如下。四个结构化容器位于顶层；新增风险路径才写入 rank 和预算明细，非新增风险路径不伪造这些字段。

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
  - `lifecycle_learning_trace`
    - `decision_learning_rows`
    - `trigger_profile_learning_rows`
  - `opportunity_score_components`
  - `side_priority`
  - `ticker_side_priority`
  - `side_priority_score`
- `learning_used`
  - `alpha_setup_action_values`
  - `memory_requirements`
  - `memory_retrieval`
  - `pm_lifecycle_learning_trace`
- `capital_deployment`
  - `opportunity_rank`
  - `rank_source`
  - `rank_lifecycle`
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
- `evidence_understanding`
- `signal_collection_contract_ref`
- `evidence_alignment_state`
- `multi_evidence_consensus_score`
- `cross_analyst_conflict_count`
- `missing_evidence_count`
- `resolution_effect`
- `lineage`
- `source_lineage_context`
- `generated_at`
- `pm_contract_generation_mode`

`pm_six_step_trace` 位于 `FuturesRecommendation.signal_snapshot`，不属于 `final_action_contract` 内部字段。

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

排名与预算分配摘要：只在新增风险路径中来自第 5 步排序与预算分配结果；非新增风险路径不生成该摘要。

仓位测算结果：新增风险路径来自第 5 步 position sizing；非新增风险路径由第 6 步按最终持仓生命周期确定目标手数，不进入全市场 rank 和预算分配。

来源链路：来自 SCC source refs、分析师引用完整性校验和 PM 生成上下文。

### 2. 物理材料输出

#### 2.1 返回对象

##### 2.1.1 落点

PM 第 6 步返回给 `workflow` 的内存对象：`FuturesRecommendation`。

`FuturesRecommendation` 包含 recommendation 基础字段和 `signal_snapshot`；唯一最终交易事实位于 `signal_snapshot.final_action_contract`。

PM 的直接输出到此结束。Auditor 审计结果、DB 记录、本地 artifact 和运行日志均属于后续处理结果。

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

### 3. 后续处理边界

Auditor 在 PM 返回 `FuturesRecommendation` 后审计最终合约。

workflow / 保存层负责将 `FuturesRecommendation` 写入 DB，并生成本地 artifact。

运行日志由 PM、Auditor、workflow 和保存层在各自运行阶段写入。

上述材料属于 PM 返回后的处理结果，不属于 PM 直接输出。

### 4. 定死口径

PM 只有第 6 步生成最终合约。

PM 第 6 步对外返回 `FuturesRecommendation`。

`FuturesRecommendation.signal_snapshot` 承载最终合约、原始 SCC 快照和 PM 摘要 trace。

DB 记录、本地 artifact 和运行日志都由 workflow / 保存层基于 `FuturesRecommendation` 物理化生成，不是 PM 第二次输出。

Step1 到 Step4，以及新增风险路径进入的 Step5，都不输出物理交易事实。

Step1 到 Step4，以及新增风险路径进入的 Step5，只更新同一个 PM 内部候选状态。

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

运行时 state 的统一读取入口是 `src/agents/decision_team/portfolio_manager.py` 中的 `_run_pm_six_step_decision`。该入口负责取出上述输入并传入同一个 PM 内部候选状态，不生成交易合约。

#### 1.2 输入校验

PM 校验 SCC 的 `producer="signal_collector"`。

PM 校验 SCC 的 `collector_decision_boundary="no_trade_authority"`。

PM 校验 SCC 与 `analyst_signals` 的来源引用完整。

PM 校验 `morning_price_context.base_price` 能作为 Phase1 盘前计划参考价。

PM 校验合约基础信息存在，且能支持手数、保证金和风险测算。

现有校验代码位于 `src/agents/decision_team/portfolio_manager.py`：

- `_run_pm_six_step_decision` 校验 SCC 是否存在，以及 `producer`、`collector_decision_boundary` 是否符合边界。
- `_validate_required_analyst_signals` 只校验分析师输出是否齐全，不使用分析师信号生成交易判断。

#### 1.3 PM 如何理解 SCC

PM 对 SCC 的理解写入 `evidence_understanding`：

- `direction_evidence`：SCC 中与多、空、退出、观望相关的结构化方向证据。
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

本步复用现有 `build_pm_fusion_diagnostics` 理解 SCC 中的证据融合信息。

工具路径：`src/tools/common/evidence_fusion_semantics.py`。

该工具只读取 `signal_collection_contract`，生成一致性、冲突、缺失证据和确认需求摘要，不生成方向、rank、手数或交易权限。

#### 1.4 PM 如何理解账户、价格、合约和配置

PM 对账户和持仓的理解写入 `position_context`：

- 当前手数。
- 当前方向。
- 当前保证金占用。
- 可用风险空间。

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

合约信息读取工具：`FuturesContractInfoCache.get_contract_info`。

工具路径：`src/apis/contract_info_cache.py`。

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

现有来源完整性校验入口是 `src/agents/decision_team/portfolio_manager.py` 中的 `_validate_required_analyst_signals`。该入口只核对启用分析师与实际输出，不解释分析师方向、触发、风险和失效边界。

#### 1.6 状态更新

PM 把上述理解写入同一个产品候选状态。

候选状态是 PM 内部主链对象，后续步骤继续改写同一个对象，不再另起一套交易事实。

本步状态不是最终合约。

候选状态继续传入第 2 步。

最终物理输出只来自第 6 步后的 `FuturesRecommendation`：原始 SCC 进入 `FuturesRecommendation.signal_snapshot.signal_collection_contract`，可执行交易事实进入 `FuturesRecommendation.signal_snapshot.final_action_contract`，候选状态演化摘要进入 `FuturesRecommendation.signal_snapshot.pm_six_step_trace`。

DB 记录和本地 artifact 由 workflow / 保存层基于 `FuturesRecommendation` 持久化生成，不是本步输出。

Step1 到 Step4，以及新增风险路径进入的 Step5，只更新同一个 PM 内部候选状态。

Step1 到 Step4，以及新增风险路径进入的 Step5，禁止生成 `candidate_contract`、`final_contract_builder_inputs`、`FuturesRecommendation` 或任何 recommendation。

#### 1.7 状态演化与自检边界

第 1 步读取的原始 `signal_collection_contract` 和来源引用事实必须保持不变。后续步骤只能消费这些事实并更新 PM 内部候选状态，不得反向改写第 1 步证据。

第 1 步 `evidence_understanding` 是后续决策输入，不是最终动作约束。第 2、3、4、5 步结合方向、持仓、生命周期、学习、风险和资金部署继续更新同一个候选状态，最终动作可以与 SCC `dominant_side` 和第 1 步证据理解不同。

第 6 步必须把原始 SCC 保真写入 `FuturesRecommendation.signal_snapshot.signal_collection_contract`，并只对最终 `final_action_contract` 自身执行 `pm_contract_self_check`。禁止因最终动作、最终持仓方向与 SCC `dominant_side`、第 1 步 `evidence_understanding` 不同而判定合约失败。

#### 1.8 禁止项

PM 不改写 SCC。

PM 不重建 SCC。

PM 不从 `state["analyst_signals"]` 判断方向、触发、风险、失效边界。

PM 不直接读取行情原始序列、基本面原始数据、新闻原文作为交易判断输入。

PM 不读取已落盘 DB 推荐记录作为本次最终合约输入。

PM 不读取本地 recommendation artifact 作为本次最终合约输入。

PM 不读取运行日志作为本次最终合约输入。

PM 不要求第 1 步证据方向与第 6 步最终动作、最终持仓方向保持一致，不执行 Step1 与 Step6 的比较式自检。

PM 不在第 1 步输出 `FuturesRecommendation`。

PM 不在第 1 步输出 `final_action_contract`。

PM 不在第 1 步输出 DB 记录、本地 artifact 或运行日志物理事实。

PM 不把第 1 步候选状态暴露给 workflow、Auditor、Trader、Reviewer、Researcher 或 PG 作为外部事实。

### 2. 判断产品方向

#### 2.1 本步目标

PM 根据第 1 步写入候选状态的 `direction_evidence`，确定当前产品的 `preferred_direction`。

`preferred_direction` 只取 `long`、`short`、`flat`：

- `long`：当前 SCC 结构化证据的优先方向为多头。
- `short`：当前 SCC 结构化证据的优先方向为空头。
- `flat`：当前 SCC 没有形成可区分的多空优先方向。

`preferred_direction` 只表达产品方向选择，不表达开仓、加仓、持有、减仓、退出、手数和交易授权。

#### 2.2 使用的状态事实

本步只读取同一个产品候选状态中第 1 步已经整理的 `direction_evidence`，其中方向事实来自原始 `signal_collection_contract`：

- `dominant_side`
- `side_consensus`
- `supporting_analysts`
- `opposing_analysts`
- `neutral_analysts`
- `evidence_strength`
- `evidence_alignment_state`
- `multi_evidence_consensus_score`
- `cross_analyst_conflicts`
- `dominant_opposing_evidence`
- `confirmation_requirements`
- `missing_evidence`

本步沿用第 1 步已经生成的 `evidence_fusion_summary`，不重新读取分析师输出，不重新生成 SCC 融合事实。

#### 2.3 调用工具

产品方向选择沿用现有确定性工具入口：

- 工具：`select_ticker_side`
- 路径：`src/tools/agent_tools/decision/pm_ticker_side_selection.py`

`pm_ticker_side_selection` 中的 `side_priority` 只用于同一产品内部的多空方向排序，不是全市场 `opportunity_rank`，不是资金优先级，也不是交易授权。

代码梳理时保留该工具的确定性方向选择入口，把本步输入收窄为候选状态中的 `direction_evidence` 和原始 SCC 方向事实。现有 `build_opportunity_scorecard` 承担的候选质量计算不属于本步，学习成果也不在本步读取。

本步不新增方向判断工具，不调用 LLM。

#### 2.4 判断顺序

PM 按以下顺序判断方向：

1. 读取 SCC 的 `dominant_side`，确认其属于 `long`、`short`、`flat`、`mixed`。
2. 使用 `side_consensus`、`evidence_alignment_state` 和 `multi_evidence_consensus_score` 核对主方向的一致性。
3. 使用 `supporting_analysts`、`opposing_analysts`、`cross_analyst_conflicts` 和 `dominant_opposing_evidence` 保留主方向的支持与反对事实。
4. 对 `long`、`short` 形成产品内部 `side_priority`，取优先侧写入 `preferred_direction`。
5. `dominant_side` 为 `flat`、`mixed`、方向证据缺失、两侧无法区分时，写入 `preferred_direction="flat"`。

冲突、缺失和待确认项不会被删除。它们继续保留在候选状态中，供第 3 步判断交易状态和候选质量。

#### 2.5 状态更新

本步把以下内容写回同一个产品候选状态：

- `preferred_direction`
- `direction_source="signal_collection_contract"`
- `side_priority`
- `ticker_side_priority`
- 本次方向判断使用的 SCC 事实摘要
- 未解决的方向冲突、缺失证据和确认需求

本步不创建新的候选对象，不输出独立方向 artifact。更新后的同一候选状态继续传入第 3 步，由第 3 步比较当前持仓与 `preferred_direction`，再确定交易状态。

#### 2.6 状态演化与自检边界

`preferred_direction` 是第 2 步根据 SCC 事实确定的产品证据优先方向，不是最终交易动作，也不是最终合约必须保持不变的方向字段。

第 3、4 步结合当前持仓、生命周期和学习成果继续更新同一个候选状态。只有新增风险候选进入第 5 步执行风险排序和资金部署；非新增风险候选从第 4 步直接进入第 6 步。最终候选进入等待、持有、减仓、退出和新增风险路径都属于正常状态演化。

第 6 步只根据最终候选状态生成 `final_action_contract`，并只对最终合约自身执行 `pm_contract_self_check`。禁止直接比较第 2 步 `preferred_direction` 与第 6 步最终动作、最终持仓方向来判定合约失败。

#### 2.7 禁止项

PM 不从 `state["analyst_signals"]`、分析师自由文本和分析师 artifact 重新判断产品方向。

PM 不改写、重建、补造 `signal_collection_contract` 和 `direction_evidence`。

PM 不在本步读取 research DB、学习成果和未来日期数据。

PM 不在本步比较当前持仓与 `preferred_direction`，不判断生命周期和交易状态。

PM 不在本步生成 `opportunity_scorecard`、候选质量、全市场 `opportunity_rank`、资金部署和手数。

PM 不把 `side_priority` 当作全市场资金排名、开仓资格和执行权限。

PM 不要求第 2 步 `preferred_direction` 与第 6 步最终动作、最终持仓方向保持不变，不执行 Step2 与 Step6 的比较式自检。

PM 不在本步生成 `candidate_contract`、`final_contract_builder_inputs`、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

PM 不在本步执行最终合约自检，不生成 DB 记录、本地 artifact 和运行日志物理事实。

PM 不把本步候选状态暴露给 workflow、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

### 3. 结合持仓确定交易状态

#### 3.1 本步目标

PM 比较第 1 步整理的当前持仓方向与第 2 步确定的 `preferred_direction`，写入 `position_direction_relation`，再确定当前产品的初始生命周期分流口和候选交易状态。

本步只回答三件事：

- 当前是否持仓以及持仓方向。
- 当前持仓与产品优先方向是什么关系。
- 该产品进入新增风险、持仓管理、释放资金、等待中的哪条内部处理路径。

本步不确定最终动作和目标手数。初始生命周期分流和候选交易状态都属于同一个 PM 内部候选状态，先进入第 4 步；第 4 步完成后，非新增风险直接进入第 6 步，新增风险进入第 5 步后再进入第 6 步。

#### 3.2 使用的状态事实

本步只读取同一个产品候选状态中的以下内容：

- `position_context.current_lots`
- `position_context.current_direction`
- `position_context.current_margin`
- `position_context.available_risk_space`
- `preferred_direction`
- `direction_source`
- `trigger_state`
- `trigger_condition`
- `evidence_strength`
- `evidence_quality`
- `confirmation_requirements`
- `missing_evidence`
- `risk_factors`
- `invalidation_boundary`

PM 以有符号当前手数确认持仓方向：当前手数大于零为 `long`，小于零为 `short`，等于零为 `flat`。`position_context.current_direction` 必须与有符号当前手数一致。

本步沿用第 1、2 步已经写入候选状态的事实，不重新读取 portfolio，不重新判断产品方向。

#### 3.3 持仓与方向关系

`position_direction_relation` 固定按下表确定：

| 当前持仓方向 | `preferred_direction` | `position_direction_relation` | 初始处理含义 |
|---|---|---|---|
| `flat` | `flat` | `flat_no_direction` | 当前没有持仓，也没有方向候选 |
| `flat` | `long`、`short` | `flat_with_direction` | 当前存在新增风险候选 |
| `long` | `long` | `same_direction` | 当前多头持仓进入同向持仓管理 |
| `short` | `short` | `same_direction` | 当前空头持仓进入同向持仓管理 |
| `long` | `short` | `opposite_direction` | 当前多头持仓先进入释放资金判断 |
| `short` | `long` | `opposite_direction` | 当前空头持仓先进入释放资金判断 |
| `long`、`short` | `flat` | `position_without_direction` | 当前持仓进入释放资金判断 |

`position_direction_relation` 只描述当前事实关系，不直接等于开仓、加仓、持有、减仓、退出和反转动作。

#### 3.4 判断初始生命周期分流口

本步沿用现有 PM 生命周期分类工具：

- 工具：`classify_lifecycle_action_port`
- 路径：`src/tools/agent_tools/decision/pm_lifecycle_action_port.py`

初始 `primary_lifecycle_action_port` 按持仓关系确定：

- `flat_no_direction` 进入 `wait`。
- `flat_with_direction` 进入 `new_risk` 候选路径。
- `same_direction` 进入 `position_hold`，后续新增同向风险仍须由第 5 步重新识别为新增风险路径。
- `opposite_direction` 进入 `capital_release`，反向新开风险不得与原持仓释放合并成一步。
- `position_without_direction` 进入 `capital_release` 候选路径；最终减仓、退出和继续持有仍由后续状态演化决定。

现有 `classify_lifecycle_action_port` 以 `current_lots`、`target_lots` 和动作字段组成的 contract-shaped payload 分类。代码梳理时保留该工具，把本步输入收窄为 `current_lots`、`position_direction_relation` 和候选交易状态，不创建 `candidate_contract`，不提前生成目标手数。最终合约生命周期仍由第 6 步根据最终 `current_lots`、`target_lots` 和合约权限重新分类。

该工具是确定性、无 LLM、无 DB 写入、无 artifact 写入、无合约签发的 PM 内部工具。

#### 3.5 判断候选交易状态

候选交易状态沿用现有 `pm_state_transition` 状态语义：

- 工具：`classify_pm_decision_state`
- 路径：`src/tools/agent_tools/decision/pm_state_transition.py`

本步使用以下候选状态：

- `no_opportunity`：无持仓且 `preferred_direction="flat"`。
- `watch_for_trigger`：存在方向候选，但触发、确认、失效边界、必要证据尚未完整。
- `probe_candidate`：方向和必要结构化证据已经成立，但当前只具备小规模候选条件。
- `tradeable_candidate`：方向、触发、证据质量、失效边界和当前风险空间支持进入后续资金决策。
- `risk_reduction_candidate`：当前持仓与优先方向相反，当前风险事实要求进入释放资金判断。

代码梳理时把 `classify_pm_decision_state` 的基础状态判断前移到本步，把输入收窄为 `current_lots`、`position_direction_relation`、触发状态、证据质量、失效边界和当前风险空间。目标手数、学习成果、全市场 rank 和最终资金部署不得反向成为本步初始状态的必需输入。

候选交易状态不是交易动作。第 4 步完成学习修正后，只有形成 open、add、scale、reverse 和 conditional open 意图的新增风险候选进入第 5 步；wait、hold、reduce、exit 和 `capital_release` 等非新增风险候选直接进入第 6 步。

#### 3.6 状态更新

本步把以下内容写回同一个产品候选状态：

- `current_position_direction`
- `position_direction_relation`
- `primary_lifecycle_action_port`
- `pm_decision_state`
- 持仓关系使用的事实摘要
- 待确认条件、缺失证据、风险因素和失效边界

本步不创建新的候选对象，不输出独立持仓状态 artifact。更新后的同一候选状态继续传入第 4 步，由第 4 步读取与当前产品、方向和生命周期匹配的学习成果，修正候选质量。

#### 3.7 状态演化与自检边界

`primary_lifecycle_action_port` 只是第 3 步的内部初始分流口，不是最终合约的 `contract_lifecycle_port`。

第 4 步可以改变候选质量和风险路径；新增风险候选还可以由第 5 步资金部署继续改变。最终生命周期可以与本步初始分流不同，该变化属于正常状态演化，不构成最终合约错误。

现有 `build_lifecycle_transition_diagnostic` 只用于解释内部状态变化：

- 工具：`build_lifecycle_transition_diagnostic`
- 路径：`src/tools/agent_tools/decision/pm_lifecycle_action_port.py`

该诊断不是最终合约闸门，不路由学习，不生成动作，不参与 rank，不修改手数，不签发合约。它不得写入 `final_action_contract.evidence_used`，不得作为 workflow、Auditor 和 PG 的失败依据。

第 6 步只根据最终候选状态生成最终生命周期，并只对最终 `final_action_contract` 自身执行 `pm_contract_self_check`。禁止比较第 3 步初始分流与第 6 步最终生命周期来判定合约失败。

#### 3.8 禁止项

PM 不在本步重新读取 `state["analyst_signals"]`、分析师自由文本和分析师 artifact 判断持仓状态。

PM 不在本步重新判断和改写 `preferred_direction`。

PM 不在本步读取 research DB、学习成果和未来日期数据。

PM 不在本步生成目标手数、`lots_delta`、最终动作和执行权限。

PM 不在本步执行全市场 `opportunity_rank`、资金部署、预算批准和 position sizing。

PM 不把 `primary_lifecycle_action_port` 当作最终合约生命周期、最终交易动作和 Trader 权限。

PM 不要求第 3 步初始状态与第 6 步最终合约保持不变，不执行 Step3 与 Step6 的比较式自检。

PM 不在本步生成 `candidate_contract`、`final_contract_builder_inputs`、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

PM 不在本步生成 DB 记录、本地 artifact 和运行日志物理事实。

PM 不把本步候选状态暴露给 workflow、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

### 4. 读取学习成果修正候选质量

#### 4.1 本步目标

PM 在本步读取当前交易日之前已经形成的研究学习成果，并按当前产品、方向、生命周期和候选交易状态筛选可用学习。

学习成果只用于修正同一个产品候选状态的质量、优先程度、持仓管理倾向和后续风险路径，不重新生成 SCC，不替代当日证据，不直接生成交易动作和手数。

本步允许学习结果为空。没有命中有效学习时，PM 保留第 3 步候选状态，并记录 `effective_memory_summary.status="empty"`，不得补造学习结论和阻断真实当日证据。

#### 4.2 检索输入

PM 从同一个产品候选状态读取以下检索事实：

- `config_id`
- `ticker`
- `trading_date`
- `preferred_direction`
- `current_position_direction`
- `position_direction_relation`
- `primary_lifecycle_action_port`
- `pm_decision_state`
- `horizon_class`
- `market_regime`
- `setup_type`
- `sector`
- 当前结构化证据组合摘要

PM 直接读取 research DB。学习成果不由 workflow 读取后传入 PM，也不从历史 recommendation artifact、运行日志和分析师输出中反向提取。

所有学习记录的有效日期必须早于当前 `trading_date`。当前交易日和未来日期的研究记录不得进入本次候选状态。

#### 4.3 调用工具

学习检索使用现有确定性工具：

- 工具：`retrieve_pm_memory`
- 路径：`src/tools/agent_tools/decision/pm_decision_memory_retrieval.py`

生命周期学习分流使用现有确定性工具：

- 工具：`route_lifecycle_learning`
- 路径：`src/tools/agent_tools/decision/pm_lifecycle_learning_router.py`

action-value 语义完整性复用现有共享校验：

- 工具：`validate_action_preference_family_consistency`
- 路径：`src/tools/common/final_action_semantics.py`

上述工具均不调用 LLM，不写 DB，不写 artifact，不部署资金，不计算手数，不签发 `final_action_contract`。

#### 4.4 检索顺序

`retrieve_pm_memory` 按质量优先、匹配范围逐步放宽的顺序读取：

1. 当前产品、方向、期限、市场状态和 setup 完全匹配的 `exact_state`。
2. 当前产品、方向和期限匹配的 `same_ticker_side_horizon`。
3. 当前产品和方向匹配的 `same_ticker_side`。
4. 相似 setup 和同板块检索结果保留 `retrieval_match_level` 与来源范围，继续接受 canonical 完整性和语义校验；其中完整 canonical 记录可以进入 Step4 候选学习池，弱先验和不完整 prior 只进入诊断材料。

真实完整历史优先于空壳历史。缺少 canonical 语义、奖励事实和动作偏好的空壳记录不得占用有效历史名额，也不得压住真实完整样本。

检索结果至少保留：

- `effective_memory_summary`
- `action_values`
- `alpha_setup_profiles`
- `strategy_memory`
- `adaptive_policy_state`
- `provisional_policy_state`
- `rejected_or_downgraded`
- `retrieval_attempts`

其中 `effective_memory_summary` 只描述检索质量、有效数量、匹配层级、剔除原因和来源状态，不是交易授权。

#### 4.5 正式学习与诊断材料分层

进入 PM 正式 action-value 候选集合的记录必须同时满足：

- `trading_date` 早于当前交易日。
- `consumer_scope="pm_learning"`。
- `canonical_action_value=true`。
- `canonical_action_family` 存在。
- `action_preference` 符合该 canonical action family 的语义。
- `action_value_lane` 和 `learning_lane` 存在且一致。
- 产品、方向和当前生命周期匹配。
- 不是 empty shell、incomplete prior、weak prior 和纯诊断记录。

Step4 在此只形成完整 canonical action-value 候选学习池，不直接形成最终 `alpha_setup_action_values`。

`final_action_contract.learning_used.alpha_setup_action_values` 是 PM 最终正式 canonical action-value 主证据列表。第 6 步必须按最终 `final_action`、最终持仓变化和最终生命周期重新路由 Step4 候选学习池；只有最终路由实际接收的完整 canonical 记录，才允许进入该正式列表。禁止直接复制第 4 步初始生命周期路由结果。

以下材料只能进入内部 `memory_retrieval.rejected_or_downgraded`：

- `canonical_action_value=false`，包括 similar SQL prior、fallback prior
- 缺少 canonical family、preference、action-value lane、learning lane，包括 similar SQL prior、fallback prior
- `consumer_scope` 不是 `pm_learning`
- future dated
- empty shell
- incomplete prior
- weak prior
- action family、lane、preference 语义不一致

incomplete prior 的固定诊断原因为 `incomplete_prior_not_pm_scoring_evidence`。`rejected_or_downgraded` 只保留必要 provenance 摘要和剔除原因，不参与候选质量、rank、手数、资金部署和最终动作。

完整 canonical 记录在生命周期路由阶段发生 lane 不匹配时，进入 `pm_lifecycle_learning_router.rejected_learning_rows`，不混入 `memory_retrieval.rejected_or_downgraded`。前者表示完整学习与当前生命周期不匹配，后者表示检索记录本身被剔除或降级。

`alpha_setup_profiles`、`strategy_memory`、`adaptive_policy_state` 和 `provisional_policy_state` 只作为经过现有安全过滤后的学习上下文。它们不得冒充正式 action-value，不得单独生成交易权限。

#### 4.6 按生命周期分流学习

`route_lifecycle_learning` 按第 3 步当前 `primary_lifecycle_action_port` 对正式 action-value 候选集合分流：

| 当前生命周期口 | 决策层允许的学习 lane |
|---|---|
| `new_risk` | `open`、`add`、`scale`、`increase` |
| `position_hold` | `hold` |
| `capital_release` | `reduce`、`exit`、`close`、`risk_exit` |
| `conditional_monitor` | `conditional_monitor` |
| `wait` | 不接收决策层 action-value |

execution、trigger、profile 类学习只进入 `trigger_profile_learning_rows`，用于后续执行画像和触发质量解释。它们不得进入 `decision_learning_rows`，`trigger_profile_learning_direct_to_rank` 和 `execution_profile_learning_direct_to_rank` 必须为 `false`。

生命周期不匹配的记录进入 `rejected_learning_rows`，不得通过修改 lane、action family 和 action preference 强行进入当前候选。

#### 4.7 修正候选质量

PM 先保留学习修正前的候选质量，再只用当前生命周期允许的正式学习修正候选状态：

- 正向且同生命周期的 action-value 可以提高候选质量和后续资金评估优先程度。
- 负向且同生命周期的 action-value 可以降低候选质量、转入重新确认、限制新增风险和增强释放资金倾向。
- hold 学习只影响现有持仓管理，不直接支持新开仓 rank。
- reduce、exit 学习只影响释放资金路径，不直接压低其他产品的新增风险排名。
- execution、trigger、profile 学习只影响执行画像，不改变方向、候选质量、rank 和手数。

学习修正不得覆盖当前 SCC 事实。没有当日方向、setup、触发和失效边界时，历史正向学习不能把 `no_opportunity` 单独提升为可交易候选。

本步可以更新 `pm_decision_state` 和内部生命周期意图，但不生成最终动作。候选状态发生变化后，后续步骤继续读取同一个对象。

#### 4.8 状态更新

本步把以下内容写回同一个产品候选状态：

- `effective_memory_summary`
- `memory_retrieval` 检索摘要
- 正式 action-value 候选集合
- `alpha_setup_profiles` 摘要
- 安全过滤后的策略和 policy state 摘要
- `pm_lifecycle_learning_router`
- `decision_learning_rows`
- `trigger_profile_learning_rows`
- `rejected_learning_rows`
- `rejected_or_downgraded`
- `candidate_quality_before_learning`
- `candidate_quality_after_learning`
- `learning_adjustment_summary`
- 学习修正后的 `pm_decision_state` 和内部生命周期意图

本步不创建新的候选对象，不输出独立学习 artifact。第 4 步完成后按最终候选风险性质分流：

- 非新增风险路径：`Step4 -> Step6`。`wait`、`hold`、`reduce`、`exit`、`capital_release` 和不增加风险敞口的 `conditional_monitor` 跳过第 5 步。
- 新增风险路径：`Step4 -> Step5 -> Step6`。`open`、`add`、`scale`、`reverse` 和具有新开仓权限的 `conditional_open` 必须进入第 5 步执行全市场 rank、预算分配和 position sizing。

两条路径继续传递同一个 PM 内部候选状态，不生成第二套候选对象和中间交易事实。

#### 4.9 状态演化与自检边界

第 4 步的完整 canonical 候选学习池、当前生命周期路由和候选质量修正都是 PM 内部中间状态，不是最终合约学习事实。`rejected_or_downgraded` 和 `rejected_learning_rows` 只解释材料为什么未被当前候选消费，不得冒充最终决策层学习证据。

非新增风险候选从第 4 步直接进入第 6 步。新增风险候选由第 5 步风险排序、资金部署和 position sizing 继续更新后再进入第 6 步。

无论是否经过第 5 步，第 6 步都必须从第 4 步保留的完整 canonical 候选学习池重新开始，根据最终 `final_action`、`current_lots`、`target_lots` 和最终 `contract_lifecycle_port` 重新形成正式 `decision_learning_rows` 和独立的 `trigger_profile_learning_rows`，再写入唯一 `final_action_contract`。第 6 步不得复制第 4 步的 `decision_learning_rows`，也不得让第 4 步未消费的生命周期记录因早期路由被永久丢弃。

第 6 步只校验最终 `learning_used.alpha_setup_action_values` 的纯净性、最终生命周期与最终 `decision_learning_rows` 的一致性，以及 execution/profile 学习只进入 `trigger_profile_learning_rows`。禁止读取第 4 步初始路由结果作为最终自检输入，禁止比较第 4 步初始路由与第 6 步最终生命周期来判定合约失败。

检索为空、有效学习数量少、匹配层级较弱、完整 canonical 记录与第 4 步当前生命周期不匹配，只进入 diagnostics，不触发最终合约 hard fail。

非完整 canonical、非 `pm_learning` 和 action-value 语义不一致的记录在第 4 步被识别并隔离到拒绝诊断后，不得参与候选质量和后续路由，也不因“已正确拒绝”触发最终合约 hard fail。只有这些非法记录进入候选质量、`learning_used.alpha_setup_action_values`、最终 `decision_learning_rows` 和 `trigger_profile_learning_rows` 时，才属于学习契约污染并触发 hard fail。

research DB 中晚于当前交易日的记录只要未被检索返回，就不属于本次 PM 输入。future dated 记录一旦被 `retrieve_pm_memory` 返回，代表时间边界已经断裂，必须在第 4 步输入校验处 hard fail，禁止把它降级成普通 diagnostics 后继续签约。

#### 4.10 禁止项

PM 不通过 workflow 接收学习成果，不让 workflow 读取 research DB 和生成学习摘要。

PM 不读取当前交易日和未来日期的学习记录。

PM 不把 weak prior、incomplete prior、empty shell、非 `pm_learning` 记录，以及 `canonical_action_value=false` 或 canonical 字段不完整的 similar SQL prior、fallback prior 写入正式 `alpha_setup_action_values`。

PM 不把 `rejected_or_downgraded` 中的材料用于候选质量、rank、手数、资金部署和最终动作。

PM 不让 execution、trigger、profile 学习直接改变方向、候选质量、rank、资金部署和手数。

PM 不用历史学习重建、补造和改写 SCC，不用历史学习覆盖当日结构化证据。

PM 不在本步生成全市场 `opportunity_rank`、资金部署、目标手数、`lots_delta`、最终动作和执行权限。

PM 不要求第 4 步学习路由与第 6 步最终生命周期保持不变，不执行 Step4 与 Step6 的比较式自检。

PM 不在本步生成 `candidate_contract`、`final_contract_builder_inputs`、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

PM 不在本步写入 research DB，不生成 DB 记录、本地 artifact 和运行日志物理事实。

PM 不把本步候选状态暴露给 workflow、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

### 5. 新增风险排序与预算分配

### 6. 生成唯一最终交易合约
