# PM 内部机制

## 一、输入

### 1. 统一证据输入

内容：`signal_collection_contract`

生产者：`signal_collector`

传递者：`workflow` 编排层。`workflow` 编排层不是智能体，只负责把 signal collector 产物写入运行时 state。

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

`workflow` 编排层只接收 PM 返回的 `FuturesRecommendation`，再负责组织后续审计和保存。

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
- `reason_codes`
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

方向、交易状态、生命周期口：来自第 6 步读取的最终候选状态；第 2、3 步写入的内容只作为该状态的内部演化来源。

执行触发条件：来自最终候选状态中保留的 SCC trigger 信息和最终交易状态，不直接复制第 3 步中间判断。

失效边界、风险边界：来自 SCC 的 invalidation、risk 信息。

计划参考价：来自 `morning_price_context`。

合约基础信息：来自 `FuturesContractInfoCache.get_contract_info`。

证据摘要：由第 6 步从最终候选状态读取第 1 步保真证据事实后生成，不把第 1 步中间状态直接当作最终合约字段。

学习使用摘要：由第 6 步从第 4 步保留的完整 canonical 学习池按最终生命周期重新路由后生成，不复制第 4 步初始路由结果。

排名与预算分配摘要：只在新增风险路径中来自第 5 步排序与预算分配结果；非新增风险路径不生成该摘要。

仓位测算结果：新增风险路径来自第 5 步 position sizing；非新增风险路径由第 6 步按最终持仓生命周期确定目标手数，不进入全市场 rank 和预算分配。

来源链路：来自 SCC source refs、分析师引用完整性校验和 PM 生成上下文。

### 2. PM 返回对象与后续物理化

#### 2.1 返回对象

##### 2.1.1 落点

PM 第 6 步返回给 `workflow` 编排层的内存对象：`FuturesRecommendation`。

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
- `warning_message`
- `status`
- `created_at`

##### 2.1.3 来源

来自 PM 第 6 步对最终候选状态的合约化结果。

`FuturesRecommendation` 是 PM 对 `workflow` 编排层的直接返回值，不是缓存、DB 记录或本地 artifact。

PM 返回时不填充 Auditor 审计结果和 `audit_payload`。

`workflow` 编排层接收后先交给独立 Auditor 审计；审计完成后，`workflow` 编排层 / 保存层才把 recommendation 和审计结果保存为 DB 记录和本地 artifact。

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

来自 PM 返回的 `FuturesRecommendation` 和后续独立 Auditor 审计结果，由 `workflow` 编排层 / 保存层在审计完成后写入 `futures_recommendation` 表。

其中 `action`、`lots`、`base_price`、`signal_snapshot` 必须由 `final_action_contract` 对齐生成。

#### 2.3 signal_snapshot

##### 2.3.1 落点

`futures_recommendation.signal_snapshot`。

##### 2.3.2 包含内容

- `signal_collection_contract`
- `final_action_contract`
- `pm_six_step_trace`
- 必要的 snapshot header 和 lineage 字段
- 由 `final_action_contract` 派生的 recommendation-level 摘要字段

##### 2.3.3 来源

`signal_collection_contract` 来自 `state["signal_collection_contract"]` 原始 SCC。

`final_action_contract` 来自第 6 步最终合约。

`pm_six_step_trace` 只由第 6 步生成，包含最终合约生成检查、最终合约自身一致性检查和安全 provenance 摘要；不保存 Step1–5 原始中间状态、合约草稿和比较式自检结果。

recommendation-level 摘要字段来自最终合约，不另造第二套事实。

Auditor 审计结果不属于 PM 返回时的 `signal_snapshot`；由 `workflow` 编排层在 PM 返回后单独交给保存层物理化。

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
- `pm_invocation_result`
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

来自 `workflow` 编排层、Auditor 和持久化过程的 logger。PM Step1–5 不直接写物理日志。

PM 正常返回后，`workflow` 编排层只记录最终返回状态和安全摘要。PM 调用异常时，`workflow` 编排层在调用结束后记录异常上下文；不把 PM 内部候选状态、学习池、rank 草稿和步骤对象写入日志。

运行日志只用于排查，不是交易事实来源。

### 3. 后续处理边界

Auditor 在 PM 返回 `FuturesRecommendation` 后审计最终合约。

`workflow` 编排层 / 保存层负责将 `FuturesRecommendation` 写入 DB，并生成本地 artifact。

运行日志由 `workflow` 编排层、Auditor 和保存层在 PM 返回或 PM 调用结束后写入。PM Step1–5 不产生独立日志输出。

上述材料属于 PM 返回后的处理结果，不属于 PM 直接输出。

### 4. 定死口径

PM 只有第 6 步生成最终合约。

PM 第 6 步对外返回 `FuturesRecommendation`。

`FuturesRecommendation.signal_snapshot` 承载最终合约、原始 SCC 快照和 PM 摘要 trace。

DB 记录、本地 artifact 和运行日志都由 `workflow` 编排层 / 保存层基于 `FuturesRecommendation` 物理化生成，不是 PM 第二次输出。

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

`pm_six_step_trace` 只保存第 6 步最终生成检查、最终合约自身检查和安全 provenance 摘要，不保存 Step1–5 原始中间状态，不生成交易动作。

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

最终物理输出只来自第 6 步后的 `FuturesRecommendation`：原始 SCC 进入 `FuturesRecommendation.signal_snapshot.signal_collection_contract`，可执行交易事实进入 `FuturesRecommendation.signal_snapshot.final_action_contract`，第 6 步最终生成检查、最终合约自身检查和安全 provenance 摘要进入 `FuturesRecommendation.signal_snapshot.pm_six_step_trace`。

DB 记录和本地 artifact 由 `workflow` 编排层 / 保存层基于 `FuturesRecommendation` 持久化生成，不是本步输出。

Step1 到 Step4，以及新增风险路径进入的 Step5，只更新同一个 PM 内部候选状态。

Step1 到 Step4，以及新增风险路径进入的 Step5，禁止生成 `candidate_contract`、`final_contract_builder_inputs`、`FuturesRecommendation` 或任何 recommendation。

#### 1.7 状态演化与自检边界

第 1 步读取的原始 `signal_collection_contract` 和来源引用事实必须保持不变。后续步骤只能消费这些事实并更新 PM 内部候选状态，不得反向改写第 1 步证据。

第 1 步 `evidence_understanding` 是后续决策输入，不是最终动作约束。第 2、3、4 步继续更新同一个候选状态，只有新增风险路径再由第 5 步更新该状态；最终动作可以与 SCC `dominant_side` 和第 1 步证据理解不同。

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

PM 不把第 1 步候选状态暴露给 `workflow` 编排层、Auditor、Trader、Reviewer、Researcher 或 PG 作为外部事实。

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

PM 不把本步候选状态暴露给 `workflow` 编排层、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

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

Step1–5 不生成生命周期转换对比对象，不保留“初始生命周期应当等于最终生命周期”的回溯诊断。生命周期变化只体现为同一个候选状态被继续更新。

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

PM 不把本步候选状态暴露给 `workflow` 编排层、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

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

PM 直接读取 research DB。学习成果不由 `workflow` 编排层读取后传入 PM，也不从历史 recommendation artifact、运行日志和分析师输出中反向提取。

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
- 产品和方向符合本次检索范围；当前生命周期只用于第 4 步临时候选质量路由，不作为完整 canonical 候选学习池的准入条件。
- 不是 empty shell、incomplete prior、weak prior 和纯诊断记录。

Step4 在此只形成完整 canonical action-value 候选学习池，不直接形成最终 `alpha_setup_action_values`。

`final_action_contract.learning_used.alpha_setup_action_values` 是 PM 最终正式 canonical action-value 主证据列表。第 6 步必须按最终 `final_action`、最终持仓变化和最终生命周期重新路由 Step4 候选学习池；只有最终路由实际接收的完整 canonical 记录，才允许进入该正式列表。禁止直接复制第 4 步初始生命周期路由结果。

以下材料只能进入内部 `memory_retrieval.rejected_or_downgraded`：

- `canonical_action_value=false`，包括 similar SQL prior、fallback prior
- 缺少 canonical family、preference、action-value lane、learning lane，包括 similar SQL prior、fallback prior
- `consumer_scope` 不是 `pm_learning`
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
- 当前生命周期路由的内部摘要
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

在学习边界内，第 6 步只校验最终 `learning_used.alpha_setup_action_values` 的纯净性、最终生命周期与最终 `decision_learning_rows` 的一致性，以及 execution/profile 学习只进入 `trigger_profile_learning_rows`。禁止读取第 4 步初始路由结果作为最终自检输入，禁止比较第 4 步初始路由与第 6 步最终生命周期来判定合约失败。

检索为空、有效学习数量少、匹配层级较弱、完整 canonical 记录与第 4 步当前生命周期不匹配，只进入 diagnostics，不触发最终合约 hard fail。

非完整 canonical、非 `pm_learning` 和 action-value 语义不一致的记录在第 4 步被识别并隔离到拒绝诊断后，不得参与候选质量和后续路由，也不因“已正确拒绝”触发最终合约 hard fail。只有这些非法记录进入候选质量、`learning_used.alpha_setup_action_values`、最终 `decision_learning_rows` 和 `trigger_profile_learning_rows` 时，才属于学习契约污染并触发 hard fail。

research DB 中晚于当前交易日的记录只要未被检索返回，就不属于本次 PM 输入。future dated 记录一旦被 `retrieve_pm_memory` 返回，代表时间边界已经断裂，必须在第 4 步输入校验处 hard fail，禁止把它降级成普通 diagnostics 后继续签约。

#### 4.10 禁止项

PM 不通过 `workflow` 编排层接收学习成果，不让 `workflow` 编排层读取 research DB 和生成学习摘要。

PM 不读取当前交易日和未来日期的学习记录。

PM 不把 weak prior、incomplete prior、empty shell、非 `pm_learning` 记录，以及 `canonical_action_value=false` 或 canonical 字段不完整的 similar SQL prior、fallback prior 写入正式 `alpha_setup_action_values`。

PM 不把 `rejected_or_downgraded` 中的材料用于候选质量、rank、手数、资金部署和最终动作。

PM 不让 execution、trigger、profile 学习直接改变方向、候选质量、rank、资金部署和手数。

PM 不用历史学习重建、补造和改写 SCC，不用历史学习覆盖当日结构化证据。

PM 不在本步生成全市场 `opportunity_rank`、资金部署、目标手数、`lots_delta`、最终动作和执行权限。

PM 不要求第 4 步学习路由与第 6 步最终生命周期保持不变，不执行 Step4 与 Step6 的比较式自检。

PM 不在本步生成 `candidate_contract`、`final_contract_builder_inputs`、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

PM 不在本步写入 research DB，不生成 DB 记录、本地 artifact 和运行日志物理事实。

PM 不把本步候选状态暴露给 `workflow` 编排层、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

### 5. 新增风险排序与预算分配

#### 5.1 本步目标

第 5 步只处理第 4 步确认需要增加风险敞口的产品候选，在完整的当日全市场候选集合中完成统一排名、预算安排和 position sizing。

排名的业务含义是资金投入优先级。`opportunity_rank=1` 表示：在当前 SCC 证据、正式 action-value、产品历史经验和风险约束共同作用下，该候选是本轮相对最值得优先投入资金、预期风险收益最优的候选。它不表示已经校准的盈利概率，也不保证盈利。

排名不是独立研究结论，也不是展示性指标。排名必须直接服务预算：PM 按排名顺序消耗同一个账户预算，先处理更值得投入资金的候选，再处理后续候选。没有进入预算安排的排名不完整；脱离排名单独分配资金同样不允许。

本步仍然只更新 Step1–4 延续下来的同一 PM 内存候选状态。第 5 步不生成 recommendation、合约草稿和任何物理输出。

#### 5.2 进入本步的候选集合

PM 在开始排名前，汇集同一 `config_id`、同一 `trading_date` 下已经完成第 4 步的全部产品候选状态，并只把新增风险候选放入统一队列。

进入队列的候选包括：

- `open`
- `add`
- `scale`
- `reverse` 中释放原持仓后形成的反向新增风险部分
- 具有新开仓权限的 `conditional_open`

以下状态不进入排名队列：

- `wait`
- `hold`
- `reduce`
- `exit`
- `capital_release`
- 不增加风险敞口的 `conditional_monitor`
- `no_opportunity`、`blocked`、`rejected`

队列为空是合法状态，表示当日没有需要竞争新增风险预算的候选。候选集合不完整、混入其他交易日或混入非新增风险状态属于 Step5 输入契约错误，不得通过补造 rank 继续运行。

`workflow` 编排层只负责组织 PM 获得完整的当日输入集合，不计算 rank、不筛选资金候选、不分配预算，也不生成 Step5 结果。

#### 5.3 使用的内部状态

每个新增风险候选沿用同一个 PM 内存状态中的以下事实：

- `ticker`、`trading_date`、`config_id`
- `preferred_direction`
- `pm_decision_state`
- 当前新增风险意图
- `evidence_strength`、`evidence_quality`、setup 和 trigger 质量
- 冲突、缺失证据、风险因素和失效边界
- 第 4 步学习修正后的候选质量
- 第 4 步保留的完整 canonical action-value 候选学习池及当前新增风险 lane 路由摘要
- `position_context.current_lots`
- 当前品种敞口和账户组合敞口
- Phase1 参考价
- 第 1 步读取的合约乘数和方向保证金率

PM 从同一个账户状态读取：

- `account_equity`
- `margin_used`
- `margin_available`
- 当前组合保证金比例
- 当前组合净敞口
- 已有各品种敞口
- 当前风险等级、回撤和冷却状态

本步沿用第 1 步已经读取的价格与合约基础信息，不从 recommendation、artifact 和日志反向补取测算输入。

#### 5.4 调用工具

全市场排名和资金部署沿用现有确定性工具：

- 工具：`apply_full_market_capital_deployment`
- 路径：`src/tools/agent_tools/decision/pm_full_market_capital_deployment.py`

该工具继续作为全市场 `opportunity_rank` 的唯一生产者，并按排名顺序消耗账户预算。代码梳理时必须把它的输入从现有 `FuturesRecommendation`、`signal_snapshot` 和 `candidate_contract` 收窄为第 4 步延续下来的 PM 内存候选状态集合；工具只就地更新这些状态，不签发、不修复 recommendation 和 `final_action_contract`。

position sizing 结果沿用现有确定性工具：

- 工具：`build_position_sizing_result`
- 路径：`src/tools/agent_tools/decision/pm_position_sizing.py`

该工具只记录预算换算、手数约束和最终测算结果，不生成方向，不改变 rank，不创建交易权限，也不调用 LLM。

两个工具都不写 DB、不写 artifact、不写运行日志，不向 `workflow` 编排层返回可被当作交易事实的中间对象。

#### 5.5 排名积分制度

排名使用唯一 `rank_score`，取值限制在 `[0, 1]`。现行积分结构为：

```text
rank_score = clamp(
    当日证据质量积分
  + 候选资金层级积分
  + open/add/scale action-value 积分
  + 产品/setup/trigger 已验证历史积分
  + 当前 trigger 质量积分
  + 资金效率积分
  - 冲突、风险、失效和缺失证据扣分,
  0,
  1
)
```

具体积分由 `src/config/rank_score_policy.yaml` 配置，并由 `src/config/dev.yaml` 的 `config_catalogs.rank_score_policy` 载入。当前基线参数如下：

| 积分项 | 当前参数 | 含义 |
|---|---:|---|
| 当日证据质量 | `0.52 * opportunity_score` | 保证当前结构化证据是排名主体 |
| `tradeable_candidate` | `+0.18` | 完整可交易候选层级积分 |
| `probe_candidate` | `+0.10` | 探索候选层级积分 |
| `watch_for_trigger` | `+0.02` | 等待触发候选层级积分 |
| 正向 action-value | `+0.18 * positive_signal` | 已验证正向 open/add/scale 经验 |
| trigger 正向质量 | `+0.08 * trigger_quality_positive_signal` | 与新增风险相关的正向触发经验 |
| 负向 action-value | `-0.18 * negative_signal` | 已验证负向新增风险经验 |
| 近期尾部损失 | `-0.14 * recent_tail_loss_signal` | 抑制重复尾部风险 |
| 入场质量损失 | `-0.16 * entry_quality_loss_signal` | 抑制低质量入场 |
| trigger 净损失 | `-0.10 * net_trigger_quality_loss_signal` | 抑制失效触发模式 |
| action-value 合计边界 | `[-0.35, +0.35]` | 防止历史学习压倒当日事实 |
| 每项 gating failure | `-0.025` | 对未满足条件逐项扣分 |
| gating failure 总上限 | `-0.16` | 限定该类扣分边界 |
| 资金效率 | 最高 `+0.02` | 同等质量下优先资金效率更高者 |

`product_setup_trigger_history`、当前 trigger 质量、市场冲突、关键数据缺口、基本面缺口和失效风险继续按 catalog 中对应权重计入。所有积分必须保留组成项，不能只保存一个无法解释的总分。

现有 catalog 中的 `execution_profile_learning_weight` 属于遗留入口。按第 4 步已经确定的学习边界，execution/profile 学习只能进入执行画像，不得直接增加或扣减 `rank_score`；代码优化时该直连项必须停止参与排名，不能借 trigger 质量名义重新进入决策层。

#### 5.6 action-value 与已验证经验如何影响排名

只有第 4 步接收的完整 canonical action-value，并且在本步匹配 `open`、`add`、`scale`、`increase` 新增风险 lane，才允许影响排名。

已验证的产品、方向、setup 和 trigger 经验通过两条路径自然提高资金优先级：

1. 正向 canonical action-value 提高 `open_add_action_value_delta`。
2. 重复出现且样本、收益、回撤和触发质量满足配置要求的经验提高 `product_setup_trigger_history`，使候选进入更高的资金层级。

因此，在当日证据仍然成立、风险边界完整的前提下，经过真实交易验证且持续为正的产品候选应天然排在未验证候选之前，并获得更高的预算竞争优先级。负向 action-value、近期尾部损失和低质量入场经验则必须降低排名或阻止扩大风险。

历史经验不能单独创造候选。即使 action-value 很强，只要当日 SCC 没有方向、setup、触发、失效边界或必要证据，候选仍不得进入新增风险资金队列。

`hold`、`reduce`、`exit`、execution 和 profile lane 不得进入新增风险积分。`rejected_or_downgraded`、weak prior、incomplete prior、similar SQL prior 和 fallback prior 不得影响 rank。

#### 5.7 排名顺序

PM 先按资金层级，再按 `rank_score` 对新增风险候选排序：

1. `alpha_scale_entry`：当前证据成立，且有重复正向真实经验支持的已验证候选。
2. `real_budget_entry`：当前证据完整的 `tradeable_candidate`。
3. `exploration_probe`：尚需以小资金验证的 `probe_candidate` 或合法条件候选。

同层候选依次比较 `rank_score`、当日证据分、学习后候选质量和资金效率。所有比较项完全相同时，使用标准化 `ticker` 作为固定最终排序键，保证长期回测在相同输入下得到相同 rank。

每个进入队列的候选只能获得一个连续、唯一的全市场 `opportunity_rank`。产品内部 `side_priority`、`ticker_side_priority` 不能替代全市场 rank。

#### 5.8 排名与预算原子绑定

PM 按 `opportunity_rank` 从 1 开始顺序消费同一个账户可部署预算。处理每个候选时，必须同时计算并写回：

- 排名前的账户已用保证金比例
- 候选所需保证金比例
- 选中后的账户保证金比例
- 当前品种与选中后的单品种保证金比例
- 当前组合净敞口与选中后的预计净敞口
- `budget_approved`
- `budget_rejection_reason`
- `allocated_budget`
- `target_margin`

候选只有同时满足可用保证金、单品种上限、组合保证金预算、净敞口上限、回撤和冷却限制时，才允许占用预算。批准后立即更新同一内部账户预算游标，后续候选只能使用剩余预算。

排名靠前不绕过硬约束。即使 `opportunity_rank=1`，资金不足、单品种超限、组合超限、净敞口超限或风险状态禁止新增风险时，也必须 `budget_approved=false`，并把新增目标恢复为不增加风险敞口的状态。被拒绝的候选保留 rank 和唯一拒绝原因，用于说明“机会存在但本轮未获资金”，不得伪造为无机会。

排名靠后的候选不得越过排名靠前且满足约束的候选抢占预算。只有靠前候选因明确硬约束被拒绝后，剩余预算才继续评估后续候选。

#### 5.9 资金配置与仓位层级

资金预算使用 `src/config/dev.yaml` 的以下配置：

- `position_budget_policy`
- `capital_utilization_control`
- `net_exposure_control`
- `drawdown_control`
- 顶层 `max_total_margin_ratio`

当前基线资金参数为：

| 配置 | 当前值 | 用途 |
|---|---:|---|
| 组合保证金硬上限 | `0.20` | 任何新增风险不得突破 |
| 基础目标保证金比例 | `0.10` | 普通全市场预算目标 |
| 强机会目标保证金比例 | `0.18` | 已确认强机会的组合预算目标 |
| probe 计划比例 | `0.008` | 探索资金起点 |
| probe 上限 | `0.015` | 探索资金边界 |
| normal 计划比例 | `0.030` | 普通真实交易资金起点 |
| normal 上限 | `0.060` | 普通真实交易资金边界 |
| deployable 计划比例 | `0.060` | 已验证候选扩大资金起点 |
| deployable 上限 | `0.120` | 已验证候选扩大资金边界 |
| exceptional 计划比例 | `0.075` | 极强已验证候选资金起点 |
| exceptional 上限 | `0.130` | 极强已验证候选资金边界 |
| 单品种保证金硬上限 | `0.130` | 单一产品不得突破 |
| 普通组合净敞口上限 | `0.50` | 控制方向集中度 |

强机会预算只在正向真实经验、样本数、胜率、净收益、确认分数和止损保护满足 `capital_utilization_control` 时启用；它仍受组合保证金 `0.20` 和单品种 `0.130` 硬上限约束。已验证经验影响的是排序和可使用的资金层级，不得直接写死手数。

#### 5.10 预算换算与 position sizing

预算批准后，PM 使用参考价、合约乘数、方向保证金率和账户权益，把分配的保证金预算确定性换算为可承受手数：

```text
one_lot_margin = base_price * contract_multiplier * margin_rate
max_lots_by_budget = floor(allocated_margin / one_lot_margin)
```

PM 再依次施加可用保证金、单品种上限、组合保证金上限、净敞口上限、最大仓位比例、风险等级、回撤、冷却和最小真实交易预算约束，形成：

- `sizing_method`
- `sizing_constraints`
- `max_lots_allowed`
- `target_lots_before_constraints`
- `target_lots_after_constraints`

不足一手时不得为了“必须交易”而向上取整。`reverse` 必须先计算原持仓释放，再只对反向新增风险部分占用预算，不能把平旧仓和开新仓的保证金重复计算。

`build_position_sizing_result` 记录最终测算事实，但不决定最终动作。第 6 步根据本步更新后的 `current_lots`、`target_lots_after_constraints`、预算结果和最终权限原子生成 `final_action`、`lots_delta` 与唯一合约。

#### 5.11 配置微调边界

上述积分权重和预算比例是当前回测基线，后续允许依据长期回测结果微调，但必须先满足 `rank_score_policy.usage_boundary.tune_after_min_clean_backtest_days=40`：至少完成 40 个无系统错误、无契约污染、无旁路审计异常的干净回测交易日。

微调只修改 catalog 和 `dev.yaml` 对应参数，不在代码中散落新的隐式常量。每次只调整一组可归因参数，并比较排序稳定性、资金利用率、收益、回撤、尾部损失和预算拒绝分布。系统错误、artifact 错误、自检错误和数据污染期间的结果不得用于调参。

调参不得改变以下边界：rank 只服务资金优先级，action-value 不覆盖当日 SCC，execution/profile 学习不直连 rank，硬风险上限不由积分覆盖，最终交易权仍只由第 6 步签发。

#### 5.12 状态更新与自检边界

本步把以下内容写回同一个 PM 内存候选状态：

- `rank_score_components`
- `rank_score`
- `opportunity_rank`
- `rank_source`
- `rank_lifecycle="open_add_new_risk"`
- `capital_layer`
- `budget_approved`
- `budget_rejection_reason`
- `allocated_budget`
- `target_margin`
- `position_sizing_result`
- `target_lots_after_constraints`
- 更新后的内部账户预算游标和组合预计敞口

本步不创建第二个候选对象，不输出独立 rank、预算或 sizing artifact。更新后的同一候选状态进入第 6 步。

第 5 步只执行新增风险候选集合和资金测算输入的契约校验，不执行最终合约自检。预算拒绝、手数被约束为零、候选由新增风险转为不增加风险，都属于正常状态演化。

第 6 步只根据最终候选状态形成 `capital_deployment`、`position_sizing_result` 和最终交易字段，并只检查最终 `final_action_contract` 自身一致性。禁止比较第 5 步约束前目标与第 6 步最终动作，禁止要求 Step4 排名预期、Step5 初始手数和 Step6 最终合约保持不变。

#### 5.13 禁止项

PM 不让非新增风险候选进入全市场 rank 和预算队列。

PM 不生成脱离预算安排的展示性 rank，不绕过 rank 顺序分配新增风险资金。

PM 不把 rank 当作交易授权、盈利保证和硬风险豁免。

PM 不让 action-value 覆盖当日 SCC，不让 hold、reduce、exit、execution、profile 和被拒绝学习进入新增风险积分。

PM 不让 `rejected_or_downgraded`、weak prior、incomplete prior、similar SQL prior 和 fallback prior 影响 rank、预算和手数。

PM 不把 execution/profile 学习通过 `trigger_execution_quality` 或其他别名重新直连 rank。

PM 不在工具和代码中写死替代 catalog 的积分权重、预算比例和手数边界，不根据单次回测临时改分。

PM 不在本步生成或修改 `candidate_contract`、`final_contract_builder_inputs`、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

PM 不读取和修改 `signal_snapshot`，不让 `apply_full_market_capital_deployment` 继续通过 recommendation/snapshot 落地中间交易事实。

PM 不在本步执行最终 `pm_contract_self_check`，不执行 Step4/Step5/Step6 跨步骤回溯比较式自检。

PM 不在本步生成 DB 记录、本地 artifact 和运行日志物理事实。

PM 不把本步候选状态、排名队列和预算游标暴露给 `workflow` 编排层、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

### 6. 生成唯一最终交易合约

#### 6.1 本步目标

第 6 步把 Step1–4 延续下来的最终 PM 内存候选状态，以及新增风险路径经 Step5 更新后的排名、预算和手数结果，一次性转换为唯一 `final_action_contract` 和唯一 `FuturesRecommendation`。

本步是 PM 唯一签约点。Step1–5 的状态只有在第 6 步完成最终装配并通过最终合约自身一致性检查后，才成为对外交易事实。

第 6 步采用原子生成：

1. 在 PM 本地内存中读取最终候选状态。
2. 确定最终手数、动作、生命周期和学习路由。
3. 构建唯一 `final_action_contract`。
4. 从该合约派生唯一 `FuturesRecommendation` 和白名单 `signal_snapshot`。
5. 检查最终输出自身一致性。
6. 全部通过后一次性返回；任一检查失败都不返回半成品。

本步不重新执行 Step1–5，不回溯比较早期状态，也不修复上游中间对象。

#### 6.2 两条进入路径

非新增风险候选从第 4 步直接进入本步：

```text
Step4 -> Step6
```

包括 `wait`、`hold`、`reduce`、`exit`、`capital_release` 和不增加风险敞口的 `conditional_monitor`。

新增风险候选经第 5 步进入本步：

```text
Step4 -> Step5 -> Step6
```

包括 `open`、`add`、`scale`、`reverse` 和具有新开仓权限的 `conditional_open`。

两条路径进入第 6 步的都是同一个 PM 内存候选状态，不是候选合约、recommendation 草稿、snapshot 草稿或 artifact。

#### 6.3 最终输入校验

第 6 步开始前只校验最终签约所需输入是否完整、类型是否正确：

- `ticker`、`trading_date`、`config_id` 和 `source_type` 存在。
- 原始 `signal_collection_contract` 存在，且 `producer="signal_collector"`、`collector_decision_boundary="no_trade_authority"`。
- 当前持仓、账户权益、可用保证金和风险空间存在。
- 计划参考价、合约代码、合约乘数和方向保证金率有效。
- 最终候选方向、触发、失效边界、风险原因和权限状态可解释。
- 第 4 步完整 canonical 学习候选池仍保留，且未混入 future dated 记录。
- 最终状态若增加风险，必须存在第 5 步唯一全市场 rank、预算结论和 position sizing 结果。
- 最终状态若不增加风险，不要求也不补造第 5 步 rank。

缺少必要输入属于输入契约错误，立即停止本产品签约。证据弱、学习为空、rank 低、预算不足和最终不交易是合法业务结果，不属于输入契约错误。

#### 6.4 调用工具

最终持仓变化和动作语义使用现有确定性工具：

- 工具：`classify_position_transition`、`final_action_from_lots`
- 路径：`src/tools/agent_tools/decision/pm_position_transition.py`

最终生命周期分类使用现有确定性工具：

- 工具：`classify_lifecycle_action_port`
- 路径：`src/tools/agent_tools/decision/pm_lifecycle_action_port.py`

最终生命周期学习重路由使用现有确定性工具：

- 工具：`route_lifecycle_learning`
- 路径：`src/tools/agent_tools/decision/pm_lifecycle_learning_router.py`

最终合约构建使用现有 PM 工具：

- 工具：`build_final_action_contract`
- 路径：`src/tools/agent_tools/decision/pm_contract_builder.py`

recommendation 顶层动作和手数映射使用现有共享工具：

- 工具：`recommendation_intent_from_lots`
- 路径：`src/tools/common/order_semantics.py`

最终合约自身一致性检查使用现有 PM 工具：

- 工具：`check_final_action_contract`
- 路径：`src/tools/agent_tools/decision/pm_contract_self_check.py`

`FuturesRecommendation` 数据结构沿用：

- 类型：`FuturesRecommendation`
- 路径：`src/graph/schema.py`

上述工具都不调用 LLM、不写 DB、不生成 artifact、不写运行日志、不调用 Auditor，也不自行修复最终合约。

代码梳理时，`build_final_action_contract` 的输入必须从旧的 `builder_inputs`、`candidate_contract`、`opportunity_scorecard` 草稿和 recommendation snapshot 收窄为一个最终 PM 内存候选状态。工具不得接收或返回第二套交易计划。

#### 6.5 确定最终目标手数与动作

新增风险路径直接读取第 5 步约束后的 `target_lots_after_constraints`。非新增风险路径在本步根据最终持仓管理状态确定目标手数：

| 最终状态 | `target_lots` |
|---|---|
| `wait` | `0` |
| `hold` | 等于 `current_lots` |
| `reduce` | 与当前持仓同号，绝对值小于 `current_lots` |
| `exit`、`capital_release` | `0` |
| 未获预算的新增风险候选 | 恢复为 `current_lots` |
| 不增加风险的 `conditional_monitor` | 等于 `current_lots` |

PM 只从最终 `current_lots`、最终 `target_lots` 和最终权限状态调用 `classify_position_transition`，一次性形成：

- `final_action`
- `action_family`
- `lots_delta = target_lots - current_lots`
- `position_change_direction`
- 最终 direction / side

预算未批准时，新增风险目标必须恢复为不增加风险，清除直接执行权限和新增风险条件权限，并保留唯一 `budget_rejection_reason`。该正常降级不得被第 6 步重新恢复为开仓。

反转候选必须沿用现有 `exit_then_reenter` 两段语义。最终合约要明确当前可执行腿和后续反向开仓腿；recommendation 顶层 `action/lots` 只映射当前可执行腿，不得用一条交易指令伪装已同时完成平仓和反向开仓。反向新增风险腿必须已经通过第 5 步 rank 和预算约束。

#### 6.6 形成最终生命周期

PM 根据最终动作、最终 `current_lots`、最终 `target_lots` 和最终条件权限调用 `classify_lifecycle_action_port`，形成唯一 `contract_lifecycle_port`：

| 最终合约事实 | `contract_lifecycle_port` |
|---|---|
| 新开、加仓、扩大或反向新增风险 | `open_add_new_risk` |
| 持仓手数不变 | `hold` |
| 减仓或退出 | `reduce_exit` |
| 只保留触发监控、当前不增加敞口 | `conditional_monitor` |
| 空仓等待 | `wait` |

最终生命周期只由最终合约事实决定。第 3 步的 `primary_lifecycle_action_port`、第 4 步临时学习路由和第 5 步约束前风险意图都不进入最终生命周期判定。

本步不调用 `build_lifecycle_transition_diagnostic`，不生成初始/最终生命周期对照表，不检查生命周期是否与早期状态保持不变。

#### 6.7 按最终生命周期重新形成学习事实

PM 从第 4 步保留的完整 canonical action-value 候选学习池重新开始，按最终 `contract_lifecycle_port` 调用 `route_lifecycle_learning`：

- `open_add_new_risk` 只接收 `open`、`add`、`scale`、`increase` 决策学习。
- `hold` 只接收 `hold` 决策学习。
- `reduce_exit` 只接收 `reduce`、`exit` 决策学习。
- `conditional_monitor` 只接收 `conditional_monitor` 决策学习。
- `wait` 不接收决策层 action-value。

最终形成两个严格分层的列表：

- `decision_learning_rows`：与最终生命周期匹配的决策层学习。
- `trigger_profile_learning_rows`：execution、trigger、profile 类执行画像学习。

只有最终 `decision_learning_rows` 中实际被最终动作消费的完整 canonical 记录，才进入 `learning_used.alpha_setup_action_values`。不得先截取 Step4 列表再路由，不得复制 Step4 临时 `decision_learning_rows`，不得让未匹配最终生命周期的记录进入正式主证据列表。

`trigger_profile_learning_rows` 只进入 `evidence_used.lifecycle_learning_trace`，其 `execution_profile_learning_direct_to_rank` 和 `trigger_profile_learning_direct_to_rank` 必须为 `false`。它不能改变最终动作、rank、预算和手数。

`memory_retrieval.rejected_or_downgraded` 和最终生命周期未接受的完整学习只保留必要 provenance 与拒绝原因，不进入正式决策列表。

#### 6.8 原子构建 final_action_contract

`build_final_action_contract` 只读取最终 PM 内存候选状态，并一次性创建 `agentquant.final_action.v1` 合约。字段范围以“二、输出 / 1.2 包含内容”为唯一白名单。

装配顺序固定为：

1. 产品、日期、配置、合约和 source identity。
2. 最终动作、持仓变化、方向和生命周期。
3. 最终权限、触发、时效、取消条件和执行规则。
4. 失效边界、风险边界、止损参考和 `reason_codes`。
5. 参考价、合约乘数、保证金率、名义价值和保证金测算。
6. 最终证据摘要和 SCC 引用。
7. 最终生命周期学习事实。
8. 新增风险路径的最终 rank、预算和 position sizing；非新增风险路径的无 rank 说明及最终手数摘要。
9. lineage、生成时间和生成模式。

新增风险合约的 `capital_deployment` 必须与第 5 步最终状态一致。未获预算时必须同时满足：

- `budget_approved=false`
- `target_lots=current_lots`
- `lots_delta=0`
- `final_action` 为 `wait` 或 `hold`
- 无直接执行和新增风险条件权限

非新增风险合约可以保留 `deployment_required=false` 和 `new_risk_rank_required=false` 的说明，但不得伪造 `opportunity_rank`、资金层级和已批准预算。

合约不得包含 `action_candidates`、`recommendation_intent`、`candidate_contract`、`builder_inputs`、`pm_internal_candidate`、scorecard 草稿、rank 草稿、预算草稿和任何可被解释为第二套交易计划的字段。

#### 6.9 生成 FuturesRecommendation

PM 只从已经形成的 `final_action_contract` 派生 `FuturesRecommendation`：

- `config_id`、日期、产品和合约身份来自最终合约。
- `action` 和 `lots` 由 `recommendation_intent_from_lots(current_lots, target_lots)` 映射。
- `base_price`、价格来源和日期来自最终合约。
- `justification` 只摘要最终动作、手数、核心证据和原因代码，不产生新动作。
- `status` 反映本次 recommendation 状态，不改写最终交易事实。
- `audit_payload` 在 PM 返回时保持空值。

`signal_snapshot` 只允许包含：

- 原样深拷贝的 `signal_collection_contract`
- 唯一 `final_action_contract`
- `pm_six_step_trace`
- 必要 header 和 lineage
- 从最终合约派生的 recommendation-level 摘要

recommendation 顶层 `action`、`lots`、价格、产品和日期必须与 `final_action_contract` 对齐。若需要兼容两段反转，顶层动作表示当前可执行腿，完整目标和两段语义仍以最终合约为唯一事实。

#### 6.10 Step6 最终检查

第 6 步只保留两类针对最终输出的检查。

`step6_contract_generation_check` 检查原子生成结果：

- 只生成一个 `final_action_contract` 和一个 `FuturesRecommendation`。
- 最终合约位于 `signal_snapshot.final_action_contract`。
- 原始 SCC 位于 `signal_snapshot.signal_collection_contract` 且未被改写。
- recommendation 顶层字段来自最终合约。
- snapshot 不含 PM 内部候选状态、builder inputs、合约草稿和 rank/预算草稿。

`check_final_action_contract` 只检查最终合约自身：

- 必填字段和四个结构化容器存在且类型正确。
- `lots_delta = target_lots - current_lots`。
- `final_action`、`action_family`、方向、持仓变化和最终生命周期一致。
- 条件执行权限、trigger、有效期和取消条件内部一致。
- 可执行 `conditional_open` 有明确 trigger；只监控的 `conditional_monitor` 可以保持 `target_lots=current_lots`，不得因手数不变被误报为合约失败。
- 新增风险合约具有第 5 步唯一 rank、预算和 sizing；非新增风险合约没有伪造 rank。
- `position_sizing_result.target_lots_after_constraints` 与最终 `target_lots` 一致。
- `learning_used.alpha_setup_action_values` 纯净且与最终生命周期匹配。
- decision learning 与 trigger/profile learning 分层正确。
- 最终合约不含 PM 内部中间状态和第二套交易计划。

`check_final_action_contract` 必须收窄为 `check_final_action_contract(final_action_contract)`。PM 不再把 `pm_artifact`、`signal_snapshot`、Step1–5 状态和早期生命周期传给合约自检；artifact 边界由 PM 返回后的 Auditor、保存层和 PG 检查。

两个检查都只读、无副作用。检查器只能报告错误，不能补字段、改动作、改手数、改变生命周期和修复合约。

#### 6.11 pm_six_step_trace

最终检查通过后，PM 在 `signal_snapshot.pm_six_step_trace` 写入安全摘要：

- `stage="step_6_final_action_contract_signed"`
- `step_6_contract_builder`
- `step6_contract_generation_check`
- `pm_contract_self_check`
- 最终 `contract_lifecycle_port`
- 是否经过 Step5
- 是否需要全市场 rank
- 安全 provenance 摘要
- `candidate_was_internal_only=true`

`pm_six_step_trace` 只证明唯一最终对象如何生成并通过最终检查。它不保存 Step1–5 原始状态，不保存 candidate contract、builder inputs、旧生命周期、旧手数、旧 rank 和任何跨步骤比较结论。

#### 6.12 成功、失败与返回边界

全部检查通过后，PM 一次性向 `workflow` 编排层返回唯一 `FuturesRecommendation`，随后立即释放本次产品的内部候选状态引用。

检查失败时：

- PM 不返回 `FuturesRecommendation` 半成品。
- PM 不生成降级 artifact、失败 recommendation 和替代合约。
- PM 不把失败对象写 DB。
- PM 不调用 Auditor 修复。
- PM 抛出唯一明确的输入契约或最终合约错误。
- `workflow` 编排层只在 PM 调用结束后记录异常上下文并停止该链路。

证据弱、学习为空、无新增风险、预算不足、手数为零、合法等待和持有都应形成有效的 `wait/hold` 最终合约，不得用异常代替合法不交易结果。

#### 6.13 禁止项

PM 不在第 6 步重新读取分析师自由文本、分析师 artifact、历史 recommendation artifact 和运行日志判断交易。

PM 不重新生成、改写和补造 `signal_collection_contract`。

PM 不重新执行 Step1–5，不生成第二个内部候选，不读取 candidate contract 和 builder inputs 签约。

PM 不复制 Step4 临时学习路由，不让早期 `decision_learning_rows` 直接进入最终合约。

PM 不要求 Step1 证据整理、Step2 方向、Step3 初始生命周期、Step4 学习路由和 Step5 约束前目标与最终合约保持不变。

PM 不调用 `build_lifecycle_transition_diagnostic`，不执行任何 Step1/2/3/4/5 与 Step6 的回溯比较式自检。

PM 不让 `check_final_action_contract` 读取 artifact、snapshot 和 PM 中间状态，不让自检器修复最终合约。

PM 不在 `final_action_contract` 中保留 `action_candidates`、`recommendation_intent`、scorecard 草稿、rank 草稿、预算草稿、第二套手数计划和内部审计对象。

PM 不让 recommendation 顶层字段形成第二套交易事实；所有顶层摘要必须从最终合约派生。

PM 不在本步调用 Auditor，不填充 `audit_payload`，不写 DB，不生成本地 artifact，不写运行日志物理事实。

PM 不向 `workflow` 编排层、Trader、Reviewer、Researcher 和 PG 暴露内部候选状态；唯一返回对象只能是通过最终检查的 `FuturesRecommendation`。
