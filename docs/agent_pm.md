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

`workflow` 编排层接收 PM 返回的 `FuturesRecommendation` 后，先由保存层物理化，再组织独立 Auditor 审计并把审计结果更新回同一条 recommendation 记录。

最终合约生成时不读取 DB 中已经落盘的 PM 输出，不读取本地 artifact，不读取运行日志。

#### 1.2 包含内容

`final_action_contract` 只使用 `docs/matrix_field_semantics.md` 已登记字段，并按该文档规定的层级放置。动作与学习语义只使用 `docs/matrix_action_canonical.md` 的 canonical 口径。

- `contract_version`
- `source_agent`
- `trading_date`
- `config_id`
- `ticker`
- `underlying_code`
- `contract_code`
- `source_type`
- `final_action`
- `current_lots`
- `target_lots`
- `lots_delta`
- `target_position_ratio`
- `authority_type`
- `execution_profile`
- `trigger_source`
- `trigger_confirmation_adjustment`
- `entry_trigger`
- `invalidation`
- `invalidation_level`
- `position_invalidation_level`
- `atr_stop_distance`
- `valid_until`
- `requires_intraday_confirmation`
- `can_execute_without_intraday_trigger`
- `conditional_trigger_authority`
- `reason_codes`
- `risk_controls`
- `capital_controls`
- `margin_ratio`
- `max_allowed_margin_ratio`
- `base_price`
- `base_price_source`
- `base_price_date`
- `prev_close_price`
- `contract_multiplier`
- `margin_rate`
- `evidence_used`
  - `lifecycle_learning_trace`
  - `learning_impact_delta`
  - `opportunity_score_components`
  - `side_priority`
  - `ticker_side_priority`
  - `pm_fusion_diagnostics`
  - `pm_conflict_resolution`
  - `position_sizing_result`
- `learning_used`
  - `alpha_setup_action_values`
  - `memory_requirements`
  - `memory_retrieval`
    - `rejected_or_downgraded`
  - `pm_lifecycle_learning_trace`
  - `pm_lifecycle_learning_impact_delta`
  - `learning_adjustment_summary`
- `capital_deployment`
  - `selected_for_capital_deployment`
  - `capital_allocation_reason`
  - `opportunity_rank`
  - `rank_source`
  - `rank_scope`
  - `capital_rank_generated_by`
  - `rank_capital_role`
  - `capital_layer`
  - `capital_ratio_source`
  - `rank_reason`
  - `rank_input_components`
  - `rank_semantics_version`
  - `opportunity_rank_meaning`
  - `rank_is_capital_priority`
  - `rank_is_not_trade_authority`
  - `lifecycle_learning_trace`
  - `learning_impact_delta`
- `created_at`

`pm_six_step_trace` 位于 `FuturesRecommendation.signal_snapshot`，不属于 `final_action_contract` 内部字段。

`capital_deployment` 只在矩阵要求的资金 rank、新开、加仓、扩大或条件监控场景原子生成；非 rank 合约不得补造 rank 专属子字段。

#### 1.3 来源

最终合约内容只来自 PM 内部候选状态。

各内容来源固定：

产品、日期、配置、合约身份：来自 `state["ticker"]`、`state["trading_date"]`、`state["config_id"]` 和合约信息缓存。

`final_action`、`current_lots`、`target_lots`、`lots_delta`：来自第 6 步对最终候选状态的合约化结果。

最终动作和持仓变化：只由 `final_action`、`current_lots`、`target_lots`、`lots_delta` 表达；生命周期由共享 `final_action_semantics` 和 `pm_lifecycle_learning_trace` 解释，不新增顶层生命周期字段。

执行触发条件：只使用矩阵登记的 `execution_profile`、`trigger_source`、`trigger_confirmation_adjustment`、`entry_trigger`、`invalidation`、`invalidation_level`、`valid_until`、`requires_intraday_confirmation`、`can_execute_without_intraday_trigger` 和 `conditional_trigger_authority`。profile、source、trigger 和入场作废边界必须来自同一被选 technical/event AEC；`trigger_confirmation_adjustment`只可来自结构化 weak-conflict 权限或同品种、同方向、同 setup、同 canonical trigger 的正式 canonical open/add 学习，不得解析 reason 文本。`invalidation`和`invalidation_level`只在首次成交前作废当前FAC。合法 watch 获选后固定为条件执行；当前触发已确认且入场作废边界完整的候选可形成直执行权限，该字段不改变rank、预算或sizing。

新增风险的执行事实只允许来自 SCC 重建的三份已校验 AEC。PM Step6 必须先按执行职责过滤，再比较同类合法证据的置信度：普通15分钟条件或直执行只选最终 `target_side` 下的 technical `entry_timing`；`event_immediate` 只选当前事件已满足即时边界的 commodity_news `event_catalyst`；fundamental 固定为 `direction_context`，只能支持方向和评分，不能成为 Trader 执行来源。反方向、`no_opportunity` 和 `risk_reduction_candidate` 不得入选。

SCC 与 PM 按角色和周期使用这三类证据：technical `entry_timing` 是短期入场与技术失效锚点，fundamental `direction_context` 是中期方向、持仓和放大依据，commodity_news `event_catalyst` 只作事件修正。中性证据不进入有效技术信号的共识分母；跨周期反向保留为持仓/放大风险，同一入场周期的反向证据才形成入场冲突。

`entry_trigger`、`invalidation`、`invalidation_level`、`execution_profile` 和 `trigger_source` 必须由同一被选 AEC 原子形成。`position_invalidation_level/exit_hint/atr_stop_distance/expected_horizon_days` 是独立的成交后持仓事实，不得证明或替代入场作废。technical使用已完成OHLC确定性生成原始ATR14，AEC finalization负责把它写入`atr_stop_distance`，LLM不得生产或改写ATR。PM不直读原始AEC，只从已验证SCC重建内部证据：同方向technical优先提供结构失效，当前已确认的`event_immediate`可在technical结构位缺失时提供同源当日事件结构位；technical提供方向无关ATR；同方向fundamental只成对提供`horizon_class+expected_horizon_days`及中期方向，数值结构位固定为空；`exit_hint`仅解释。`execution_profile`直接复制AEC的`entry_timing_signal`；PM不得从自由文本猜测profile，也不得默认`breakout`。执行action-value只能形成建议摘要，不得改写顶层执行事实或交易权限。

风险约束：来自 SCC 的 `invalidation_summary`、PM `risk_controls` 和 `max_allowed_margin_ratio`。

计划参考价：来自 `morning_price_context`。

合约基础信息：来自 `FuturesContractInfoCache.get_contract_info`。

证据摘要：只使用 `evidence_used` 下已登记的 score、融合解释、生命周期学习解释和 `position_sizing_result`，不新增证据摘要字段。

学习使用摘要：由第 6 步从第 4 步保留的完整 canonical 学习池按最终生命周期重新路由后生成，不复制第 4 步初始路由结果。

排名与预算分配摘要：只在进入第 5 步的候选中来自排序与预算分配结果。候选即使最终因预算拒绝退回 `wait/hold`，仍保留本次 rank 和拒绝事实；直接从第 4 步进入第 6 步的候选不生成 rank 明细。

仓位测算结果：按矩阵固定落入 `final_action_contract.evidence_used.position_sizing_result`。新增风险路径来自第 5 步 position sizing；非新增风险路径由第 6 步按最终持仓变化确定目标手数，不进入全市场 rank。

来源链路由 `signal_snapshot.signal_collection_contract.source_contracts` 和 `evidence_items` 保真承载，不在最终合约中自创 lineage 字段。

### 2. PM 返回对象与后续物理化

#### 2.1 返回对象

##### 2.1.1 落点

PM 第 6 步返回给 `workflow` 编排层的内存对象：`FuturesRecommendation`。

`FuturesRecommendation` 包含 recommendation 基础字段和 `signal_snapshot`；唯一最终交易事实位于 `signal_snapshot.final_action_contract`。

PM 的直接输出到此结束。Auditor 审计结果、DB 记录、本地 artifact 和运行日志均属于后续处理结果。

##### 2.1.2 包含内容

返回对象结构沿用 `src/graph/schema.py` 中的 `FuturesRecommendation`，本文件不为 recommendation wrapper 另定字段语义。

其中业务字段必须来自 `matrix_field_semantics.md`；顶层动作、手数、价格、产品、日期和理由只能由唯一 `final_action_contract` 映射，不得在 wrapper 中产生第二套含义。

##### 2.1.3 来源

来自 PM 第 6 步对最终候选状态的合约化结果。

`FuturesRecommendation` 是 PM 对 `workflow` 编排层的直接返回值，不是缓存、DB 记录或本地 artifact。

PM 返回时不填充 Auditor 审计结果和 `audit_payload`。

`workflow` 编排层接收后，先由保存层把 PM 返回的 `FuturesRecommendation` 及其初始 `signal_snapshot` 写入 DB，并在需要时生成 recommendation artifact；随后把已经取得持久化 ID 的同一 `FuturesRecommendation` 交给独立 Auditor 审计。审计完成后，`workflow` 编排层 / 保存层只更新同一条 DB 记录中的 Auditor 摘要和 `audit_payload`，必要时同步更新对应 recommendation artifact，不生成第二条 recommendation 或第二张合约。

#### 2.2 DB 推荐记录

##### 2.2.1 落点

`futures_recommendation` 表。

##### 2.2.2 包含内容

DB 字段沿用既有 `futures_recommendation` 表结构；本文件不新增保存字段。

保存层只能保存 PM 返回对象、`signal_snapshot`、独立 `audit_payload` 以及 `matrix_field_semantics.md` 已登记的 artifact 元数据。未在矩阵登记的业务字段不得借 DB 列、JSON 容器或摘要字段进入系统。

##### 2.2.3 来源

初始记录来自 PM 返回的 `FuturesRecommendation`，由 `workflow` 编排层 / 保存层先写入 `futures_recommendation` 表。后续独立 Auditor 审计结果由保存层更新到同一条记录，不另建 recommendation。

其中 `action`、`lots`、`base_price`、`signal_snapshot` 必须由 `final_action_contract` 对齐生成。

#### 2.3 signal_snapshot

##### 2.3.1 落点

`futures_recommendation.signal_snapshot`。

##### 2.3.2 包含内容

- `signal_collection_contract`
- `final_action_contract`
- `pm_six_step_trace`
- `matrix_field_semantics.md` 已登记的必要 header、来源字段和派生摘要字段

##### 2.3.3 来源

`signal_collection_contract` 来自 `state["signal_collection_contract"]` 原始 SCC。

`final_action_contract` 来自第 6 步最终合约。

`pm_six_step_trace` 只由第 6 步生成，只包含矩阵登记的最终合约生成检查和最终合约自身一致性检查；不保存 Step1–5 原始中间状态、合约草稿、额外 provenance 字段和比较式自检结果。

recommendation-level 摘要字段来自最终合约，不另造第二套事实。

Auditor 审计结果不属于 PM 返回时的初始 `signal_snapshot`；由 `workflow` 编排层在初始 recommendation 已保存后，将 Auditor 摘要更新至同一条记录的 `signal_snapshot.auditor`，并将完整结果更新至同一条记录的 `audit_payload`。

#### 2.4 本地 recommendation artifact

##### 2.4.1 落点

`src/logs/artifacts/{config_id}/{trading_date}/recommendation/`

##### 2.4.2 包含内容

- recommendation payload JSON
- signal_snapshot JSON
- audit_payload JSON
- `matrix_field_semantics.md` 已登记的 artifact 元数据

##### 2.4.3 来源

来自保存层对 `signal_snapshot`、`audit_payload` 等大 JSON 的 externalize 外置镜像。

本地 recommendation artifact 不是 PM 再生成的第二份输出。

本地 artifact 是 DB 输出的可读镜像，不参与交易决策。

#### 2.5 运行日志

##### 2.5.1 落点

`src/logs/` 下运行日志。

##### 2.5.2 包含内容

日志只记录既有 logger 元数据、最终返回状态、最终检查结果和异常上下文。本文件不定义日志业务字段，也不允许日志引入 `matrix_field_semantics.md` 之外的交易语义。

##### 2.5.3 来源

来自 `workflow` 编排层、Auditor 和持久化过程的 logger。PM Step1–5 不直接写物理日志。

PM 正常返回后，`workflow` 编排层只记录最终返回状态和安全摘要。PM 调用异常时，`workflow` 编排层在调用结束后记录异常上下文；不把 PM 内部候选状态、学习池、rank 草稿和步骤对象写入日志。

运行日志只用于排查，不是交易事实来源。

### 3. 后续处理边界

`workflow` 编排层 / 保存层在 PM 返回后先将 `FuturesRecommendation` 写入 DB，并按既有外置规则生成本地 artifact。

Auditor 随后读取已保存且带持久化 ID 的同一 `FuturesRecommendation`，只读审计其中的唯一最终合约；`workflow` 编排层 / 保存层再把审计摘要和 `audit_payload` 更新回同一条 recommendation 记录。

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

`pm_six_step_trace` 只保存矩阵登记的第 6 步最终生成检查和最终合约自身检查，不保存 Step1–5 原始中间状态，不新增 provenance 字段，不生成交易动作。

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

PM 校验 SCC 的 `source_agent="signal_collector"`。

PM 校验 SCC 的 `collector_decision_boundary="no_trade_authority"`。

PM 校验 SCC 与 `analyst_signals` 的来源引用完整。

PM 校验 `morning_price_context.base_price` 能作为 Phase1 盘前计划参考价。

PM 校验合约基础信息存在，且能支持手数、保证金和风险测算。

现有校验代码位于 `src/agents/decision_team/portfolio_manager.py`：

- `_run_pm_six_step_decision` 校验 SCC 是否存在，以及 `source_agent`、`collector_decision_boundary` 是否符合边界。
- `_validate_required_analyst_signals` 只校验分析师输出是否齐全，不使用分析师信号生成交易判断。

#### 1.3 PM 如何理解 SCC

PM 只读取 SCC 已登记字段，并在同一个内部状态中保真使用：

- `dominant_side`：SCC 汇总方向，不是交易授权。
- `trigger_status`：SCC 当前触发状态。
- `entry_trigger`：当前触发事实或等待条件。
- `evidence_strength`：证据强弱。
- `evidence_fusion.evidence_alignment_state`：证据一致性。
- `evidence_fusion.cross_analyst_conflicts`：结构化冲突。
- `confirmation_requirements`：仍需确认的条件。
- `data_quality_flags`：数据质量和缺失标记。
- `invalidation_summary`：失效边界。
- `evidence_items.product_profile_used`：profile 使用痕迹。
- `evidence_fusion`：融合证据原始结构。

PM 只解释 SCC 已经给出的结构化证据，不回读分析师原始文本，不补造 SCC 没有给出的交易证据。

本步复用现有 `build_pm_fusion_diagnostics` 理解 SCC 中的证据融合信息。

工具路径：`src/tools/common/evidence_fusion_semantics.py`。

该工具只读取 `signal_collection_contract`，生成一致性、冲突、缺失证据和确认需求摘要，不生成方向、rank、手数或交易权限。

#### 1.4 PM 如何理解账户、价格、合约和配置

PM 从 `portfolio` 读取账户和持仓事实，不新增 position context 字段：

- 当前手数。
- 当前方向。
- 当前保证金占用。
- 可用风险空间。

PM 使用矩阵登记的价格字段：

- `base_price`。
- `base_price_source`。
- `base_price_date`。
- `prev_close_price`。
- Phase1 价格只用于计划测算，不代表真实成交价。

PM 使用矩阵登记的 `contract_code`、`contract_multiplier` 和 `margin_rate`。已有持仓的 `contract_code` 只来自持仓事实；新增风险的 `contract_code` 只来自Router在盘前截止点内可见的具体合约事实。PM只在Step6绑定该事实，不默认、不猜测、不以主品种代码代替；缺具体合约时不得新增风险。静态合约缓存只补充乘数、保证金率等已知属性，不生产具体合约代码，也不新增 contract context 字段。

合约信息读取工具：`FuturesContractInfoCache.get_contract_info`。

工具路径：`src/apis/contract_info_cache.py`。

PM 直接读取配置中的风险和资金参数，不新增 config context 字段：

- 单品种风险上限。
- 总保证金上限。
- 预算分配参数。
- PM 策略参数。

#### 1.5 PM 如何理解分析师信号引用

PM 只通过 SCC 已登记的 `source_contracts` 和 `evidence_items` 校验来源：

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

最终物理输出只来自第 6 步后的 `FuturesRecommendation`：原始 SCC 进入 `FuturesRecommendation.signal_snapshot.signal_collection_contract`，可执行交易事实进入 `FuturesRecommendation.signal_snapshot.final_action_contract`，矩阵登记的第 6 步最终生成检查和最终合约自身检查进入 `FuturesRecommendation.signal_snapshot.pm_six_step_trace`。

DB 记录和本地 artifact 由 `workflow` 编排层 / 保存层基于 `FuturesRecommendation` 持久化生成，不是本步输出。

Step1 到 Step4，以及新增风险路径进入的 Step5，只更新同一个 PM 内部候选状态。

Step1 到 Step4，以及新增风险路径进入的 Step5，禁止生成 `candidate_contract`、builder 输入、`FuturesRecommendation` 或任何 recommendation。

#### 1.7 状态演化与自检边界

第 1 步读取的原始 `signal_collection_contract` 和来源引用事实必须保持不变。后续步骤只能消费这些事实并更新 PM 内部候选状态，不得反向改写第 1 步证据。

第 1 步读取的 SCC 字段是后续决策输入，不是最终动作约束。第 2、3、4 步继续更新同一个候选状态，只有实际增加风险的路径再由第 5 步更新该状态；最终动作可以与 SCC `dominant_side` 不同。

第 6 步必须把原始 SCC 保真写入 `FuturesRecommendation.signal_snapshot.signal_collection_contract`，并只对最终 `final_action_contract` 自身执行 `pm_contract_self_check`。禁止因最终动作、最终持仓方向与 SCC `dominant_side` 不同而判定合约失败。

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

PM 根据原始 SCC 方向事实，调用唯一方向选择工具生成 `side_priority` 和 `ticker_side_priority`。

产品代表方向只从上述既有方向优先级结果读取，取值为 `long`、`short`、`flat`：

- `long`：当前 SCC 结构化证据的优先方向为多头。
- `short`：当前 SCC 结构化证据的优先方向为空头。
- `flat`：当前 SCC 没有形成可区分的多空优先方向。

产品代表方向只表达单品种方向选择，不表达开仓、加仓、持有、减仓、退出、手数和交易授权。

#### 2.2 使用的状态事实

本步只读取原始 `signal_collection_contract` 已登记的方向事实：

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

本步沿用 SCC 原始 `evidence_fusion`，不重新读取分析师输出，不重新生成融合字段。

#### 2.3 调用工具

产品方向选择沿用现有确定性工具入口：

- 工具：`select_ticker_side`
- 路径：`src/tools/agent_tools/decision/pm_ticker_side_selection.py`

`pm_ticker_side_selection` 中的 `side_priority` 只用于同一产品内部的多空方向排序，不是全市场 `opportunity_rank`，不是资金优先级，也不是交易授权。

代码梳理时保留该工具的确定性方向选择入口，把本步输入收窄为原始 SCC 方向事实。现有 `build_opportunity_scorecard` 承担的候选质量计算不属于本步，学习成果也不在本步读取。

本步不新增方向判断工具，不调用 LLM。

#### 2.4 判断顺序

PM 按以下顺序判断方向：

1. 读取 SCC 的 `dominant_side`，确认其属于 `long`、`short`、`flat`、`mixed`。
2. 使用 `side_consensus`、`evidence_alignment_state` 和 `multi_evidence_consensus_score` 核对主方向的一致性。
3. 使用 `supporting_analysts`、`opposing_analysts`、`cross_analyst_conflicts` 和 `dominant_opposing_evidence` 保留主方向的支持与反对事实。
4. 当 `dominant_side` 为唯一、无真实冲突的 `long` 或 `short` 时，只把该方向写成 `side_priority=1/ticker_side_priority=1` 并同步为 `preferred_side`；反方向优先级为 null。
5. `dominant_side` 为 `flat`、`mixed`、`side_consensus/evidence_alignment_state=conflicted`、方向证据缺失或两侧无法区分时，方向选择结果保持 `flat`，两侧优先级均为 null。

冲突、缺失和待确认项不会被删除。它们继续保留在候选状态中，供第 3 步判断交易状态和候选质量。

#### 2.5 状态更新

本步把以下内容写回同一个产品候选状态：

- `side_priority`
- `ticker_side_priority`
- 原始 SCC 中未解决的 `cross_analyst_conflicts`、`missing_evidence` 和 `confirmation_requirements`

本步不创建新的候选对象，不输出独立方向 artifact。更新后的同一候选状态继续传入第 3 步，由第 3 步比较当前持仓与 `ticker_side_priority` 的代表方向，再确定交易状态。

#### 2.6 状态演化与自检边界

`side_priority` 和 `ticker_side_priority` 是第 2 步根据 SCC 事实确定的单品种方向优先级，不是最终交易动作，也不是最终合约必须保持不变的方向字段。

第 3、4 步结合当前持仓、生命周期和学习成果继续更新同一个候选状态。凡最终意图会增加风险敞口的候选均进入第 5 步执行统一排名和资金部署，包括从空仓建立新仓，以及同方向 `add/scale`；不增加风险的候选从第 4 步直接进入第 6 步。最终候选进入等待、持有、加仓、减仓、退出和开仓路径都属于正常状态演化。

第 6 步只根据最终候选状态生成 `final_action_contract`，并只对最终合约自身执行 `pm_contract_self_check`。禁止直接比较第 2 步方向优先级与第 6 步最终动作、最终持仓方向来判定合约失败。

#### 2.7 禁止项

PM 不从 `state["analyst_signals"]`、分析师自由文本和分析师 artifact 重新判断产品方向。

PM 不改写、重建、补造 `signal_collection_contract` 和其中的方向事实。

PM 不在本步读取 research DB、学习成果和未来日期数据。

PM 不在本步比较当前持仓与方向优先级，不判断生命周期和交易状态。

PM 不在本步生成 `opportunity_scorecard`、候选质量、全市场 `opportunity_rank`、资金部署和手数。

PM 不把 `side_priority` 当作全市场资金排名、开仓资格和执行权限。

PM 不要求第 2 步 `side_priority` / `ticker_side_priority` 与第 6 步最终动作、最终持仓方向保持不变，不执行 Step2 与 Step6 的比较式自检。

PM 不在本步生成 `candidate_contract`、builder 输入、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

PM 不在本步执行最终合约自检，不生成 DB 记录、本地 artifact 和运行日志物理事实。

PM 不把本步候选状态暴露给 `workflow` 编排层、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

### 3. 结合持仓确定交易状态

#### 3.1 本步目标

PM 根据 `current_lots` 推导当前持仓方向，与第 2 步 `ticker_side_priority` 的代表方向进行内存比较，再确定 `primary_lifecycle_action_port`、`candidate_quality` 和 `candidate_layer_hint`。该比较不新增关系字段，也不改写分析师证据字段 `opportunity_state`。

本步只回答三件事：

- 当前是否持仓以及持仓方向。
- 当前持仓与产品优先方向是什么关系。
- 该产品进入新增风险、持仓管理、释放资金、等待中的哪条内部处理路径。

本步不确定最终动作和目标手数。初始生命周期分流和候选交易状态都属于同一个 PM 内部候选状态，先进入第 4 步；第 4 步完成后，不增加风险的行为直接进入第 6 步，实际增加风险的候选进入第 5 步后再进入第 6 步。

#### 3.2 使用的状态事实

本步只读取矩阵已有事实：

- `current_lots`
- `ticker_side_priority`
- `opportunity_state`
- `trigger_status`
- `entry_trigger`
- `evidence_strength`
- `evidence_quality`
- `confirmation_requirements`
- `missing_evidence`
- `data_quality_flags`
- `invalidation_summary`

PM 以有符号 `current_lots` 确认持仓方向：当前手数大于零为 `long`，小于零为 `short`，等于零为 `flat`。不另存当前方向字段。

本步沿用第 1、2 步已经写入候选状态的事实，不重新读取 portfolio，不重新判断产品方向。

#### 3.3 持仓与方向关系

持仓与产品代表方向在内存中按下表比较，不生成新字段：

| 当前持仓方向 | 产品代表方向 | 初始处理含义 |
|---|---|---|
| `flat` | `flat` | 当前没有持仓，也没有方向候选 |
| `flat` | `long`、`short` | 当前存在开仓候选 |
| `long` | `long` | 当前多头持仓进入同向持仓管理 |
| `short` | `short` | 当前空头持仓进入同向持仓管理 |
| `long` | `short` | 当前多头持仓先进入 reduce/exit 判断 |
| `short` | `long` | 当前空头持仓先进入 reduce/exit 判断 |
| `long`、`short` | `flat` | 当前持仓进入 reduce/exit 判断 |

该内存比较不直接等于开仓、加仓、持有、减仓、退出和反转动作。

#### 3.4 判断初始生命周期分流口

本步沿用现有 PM 生命周期分类工具：

- 工具：`classify_lifecycle_action_port`
- 路径：`src/tools/agent_tools/decision/pm_lifecycle_action_port.py`

初始 `primary_lifecycle_action_port` 按持仓与方向比较结果确定：

- 空仓且没有代表方向时进入 `wait`。
- 空仓且存在代表方向时进入 `new_risk` 候选路径。
- 持仓与代表方向相同时进入 `position_hold`；后续若形成扩大同向敞口的 `add/scale` 意图，必须进入第 5 步参与新增风险资金排名。
- 持仓与代表方向相反时进入 `capital_release` 内部分流口；退出旧方向与授权反向新风险必须分开。
- 有持仓但无代表方向时进入 `capital_release` 内部分流口；最终 `reduce`、`exit` 或 `hold` 由后续状态演化决定。

现有 `classify_lifecycle_action_port` 以 `current_lots`、`target_lots` 和动作字段组成的 contract-shaped payload 分类。代码梳理时保留该工具，把本步输入收窄为 `current_lots`、方向比较结果和 `opportunity_state`，不创建 `candidate_contract`，不提前生成目标手数。最终生命周期仍由第 6 步通过共享 `final_action_semantics` 和最终学习 trace 解释。

该工具是确定性、无 LLM、无 DB 写入、无 artifact 写入、无合约签发的 PM 内部工具。

#### 3.5 判断候选交易状态

候选质量与层级判断沿用现有 `pm_state_transition` 状态语义：

- 工具：`classify_pm_decision_state`
- 路径：`src/tools/agent_tools/decision/pm_state_transition.py`

PM 只读取 `opportunity_state` 的以下分析师证据语义，再形成自己的 `candidate_quality` 和 `candidate_layer_hint`：

- `no_opportunity`：没有完整的“逻辑 T 日出现什么可观察条件就入场、在哪里失效”的方案；可以保留 long/short 方向和研究证据，但不计入新增风险支持或排名候选。
- `watch_for_trigger`：方向、setup、具体入场触发和 canonical 失效边界完整，当前触发尚未成立，且 Trader 能用当日15分钟行情观察该触发。
- `probe_candidate`：方向和必要结构化证据已经成立，但当前只具备小规模候选条件。
- `tradeable_candidate`：方向、触发、证据质量、失效边界和当前风险空间支持进入后续资金决策。
- `risk_reduction_candidate`：只在已有持仓时进入 hold/reduce/exit 风险收缩判断；空仓时只保留研究证据，不进入新增风险证据、rank、预算或交易权限，也不否决其他分析师的合法新增风险候选。

代码梳理时把 `classify_pm_decision_state` 的基础判断前移到本步，把输入收窄为只读 `opportunity_state`、`current_lots`、方向比较结果、`trigger_status`、`evidence_quality`、`invalidation_summary` 和账户风险事实，并把 PM 结果收口到 `candidate_quality` 与 `candidate_layer_hint`。目标手数、学习成果、全市场 rank 和最终资金部署不得反向成为本步初始状态的必需输入。

`opportunity_state` 不是交易动作，`candidate_quality` 和 `candidate_layer_hint` 也不是交易权限。第 4 步完成学习修正后，形成 open/add/scale 新增风险、反转后新风险，或获得非零条件目标的合法 `watch_for_trigger` 候选进入第 5 步；wait、hold、reduce、exit、仅有 `risk_reduction_candidate` 的空仓证据和 `target_lots=current_lots` 的零增量监控直接进入第 6 步。

`missing_evidence` 和 `confirmation_requirements` 只降低证据强度、融合分或机会状态，不得转换为 `data_missing`，也不得按数量形成 `critical_data_gap`。PM 对数据质量只消费共享 `build_scc_data_quality_summary`；只有 `status=hard_fail` 可形成硬数据阻断。基本面或新闻没有当日新增、使用截止点内有效 T-n 数据以及普通 freshness warning 都不是全局 hard fail。

#### 3.6 状态更新

本步把以下内容写回同一个产品候选状态：

- `primary_lifecycle_action_port`
- `candidate_quality`
- `candidate_layer_hint`
- 原始 `confirmation_requirements`、`missing_evidence`、`data_quality_flags` 和 `invalidation_summary`

本步不创建新的候选对象，不输出独立持仓状态 artifact。更新后的同一候选状态继续传入第 4 步，由第 4 步读取与当前产品、方向和生命周期匹配的学习成果，修正候选质量。

#### 3.7 状态演化与自检边界

`primary_lifecycle_action_port` 只是第 3 步的内部初始分流口，不是最终合约字段，也不得作为 Step6 最终合约失败依据。

第 4 步可以改变候选质量和风险路径；实际增加风险的新开仓或同方向 `add/scale` 候选还可以由第 5 步资金部署继续改变。最终生命周期可以与本步初始分流不同，该变化属于正常状态演化，不构成最终合约错误。

Step1–5 不生成生命周期转换对比对象，不保留“初始生命周期应当等于最终生命周期”的回溯诊断。生命周期变化只体现为同一个候选状态被继续更新。

第 6 步只根据最终候选状态生成最终生命周期，并只对最终 `final_action_contract` 自身执行 `pm_contract_self_check`。禁止比较第 3 步初始分流与第 6 步最终生命周期来判定合约失败。

#### 3.8 禁止项

PM 不在本步重新读取 `state["analyst_signals"]`、分析师自由文本和分析师 artifact 判断持仓状态。

PM 不在本步重新生成和改写 `side_priority`、`ticker_side_priority`。

PM 不在本步读取 research DB、学习成果和未来日期数据。

PM 不在本步生成目标手数、`lots_delta`、最终动作和执行权限。

PM 不在本步执行全市场 `opportunity_rank`、资金部署、预算批准和 position sizing。

PM 不把 `primary_lifecycle_action_port` 当作最终合约生命周期、最终交易动作和 Trader 权限。

PM 不要求第 3 步初始状态与第 6 步最终合约保持不变，不执行 Step3 与 Step6 的比较式自检。

PM 不在本步生成 `candidate_contract`、builder 输入、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

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
- `ticker_side_priority` 的代表方向
- `current_lots`
- `primary_lifecycle_action_port`
- `opportunity_state`
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
4. 相似 setup 和同板块检索结果保留 `retrieval_match_level` 与来源范围，但只作为诊断材料；similar、weak 和 incomplete prior 均不得进入 Step4 正式候选学习池。

真实完整历史优先于空壳历史。缺少 canonical 语义、奖励事实和动作偏好的空壳记录不得占用有效历史名额，也不得压住真实完整样本。

检索结果按矩阵已有学习对象保留：

- `effective_memory_summary`
- `alpha_setup_profile` 表中的学习记录
- `strategy_memory` 表中的学习记录
- `adaptive_policy_state` 表中的学习记录
- `provisional_policy_state` 表中的学习记录
- 正式 canonical action-value 候选集合
- `learning_used.memory_retrieval.rejected_or_downgraded` 所需的拒绝诊断材料

其中 `effective_memory_summary` 只描述检索质量、有效数量、匹配层级、剔除原因和来源状态，不是交易授权。

#### 4.5 正式学习与诊断材料分层

进入 PM 正式 action-value 候选集合的记录必须同时满足：

- `trading_date` 早于当前交易日。
- `consumer_scope="pm_learning"`。
- `canonical_action_value=true`。
- `canonical_action_family` 存在。
- `action_preference` 符合该 canonical action family 的语义。矩阵明确无正向偏好的 `no_trade`、`observe`、`conditional_monitor` 可以在对应语义下保留空值；其他记录不得由 PM 自行放宽，必须已经通过矩阵口径和写入一致性校验。
- `action_value_lane` 和 `learning_lane` 存在且一致。
- 产品和方向符合本次检索范围；当前生命周期只用于第 4 步临时候选质量路由，不作为完整 canonical 候选学习池的准入条件。
- 不是 empty shell、incomplete prior、weak prior 和纯诊断记录。

缺失 `consumer_scope` 不得由 PM 默认补成 `pm_learning`。缺失 `canonical_action_value` 时，只有上述字段完整且共享语义校验通过的现有正式记录才可推导为 `true`；显式 `canonical_action_value=false` 永不提升。

Step4 必须在首次消费正式 action-value 前完成本次决策所需的完整 canonical 候选学习池，并覆盖新增风险、持仓、减仓/退出、条件监控和 execution/profile lane。随后唯一 scorecard、候选控制和 Step5 都读取该池；Step4 完成后不得再检索或追加正式 action-value。Step4 不直接形成最终 `learning_used.alpha_setup_action_values`，也不新增冻结 ID 字段或第二套候选对象。

分析师提示词中的 action-value 安全投影只用于生成当日 AEC，不是 PM 正式学习来源。PM 仍只从已验证 SCC 消费当日证据，并只经 `decision_memory_retrieval` 取得顶层 `pm_learning` 正式行；不得从分析师 learning trace、AEC 或 SCC 反向恢复、复制或重复登记 action-value。

`final_action_contract.learning_used.alpha_setup_action_values` 是 PM 最终正式 canonical action-value 主证据列表。第 6 步必须按最终 `final_action`、最终持仓变化和最终生命周期重新路由 Step4 候选学习池；只有最终路由实际接收的完整 canonical 记录，才允许进入该正式列表。禁止直接复制第 4 步初始生命周期路由结果。

以下材料只能进入最终 `learning_used.memory_retrieval.rejected_or_downgraded` 所需的内部诊断集合：

- `canonical_action_value=false`，包括 similar SQL prior、fallback prior
- 缺少 canonical family、action-value lane 或 learning lane；需要动作偏好的 family 缺少 preference；包括不完整的 similar SQL prior、fallback prior
- `consumer_scope` 不是 `pm_learning`
- empty shell
- incomplete prior
- weak prior
- action family、lane、preference 语义不一致

incomplete prior 的固定诊断原因为 `incomplete_prior_not_pm_scoring_evidence`。`learning_used.memory_retrieval.rejected_or_downgraded` 只保留必要 provenance 摘要和剔除原因，不参与候选质量、rank、手数、资金部署和最终动作。

完整 canonical 记录在生命周期路由阶段发生 lane 不匹配时，只进入路由器内部拒绝诊断，不混入 `learning_used.memory_retrieval.rejected_or_downgraded`。前者表示完整学习与当前生命周期不匹配，后者表示检索记录本身被剔除或降级；两者都不新增最终合约字段。

`alpha_setup_profile`、`strategy_memory`、`adaptive_policy_state` 和 `provisional_policy_state` 表中的学习记录只作为经过现有安全过滤后的学习上下文。这些表名不是最终合约字段，其记录不得冒充正式 action-value，也不得单独生成交易权限。

#### 4.6 按生命周期分流学习

`route_lifecycle_learning` 按第 3 步当前 `primary_lifecycle_action_port` 对正式 action-value 候选集合分流：

| 当前生命周期口 | 决策层允许的学习 lane |
|---|---|
| `new_risk` | `open`、`add`、`scale`、`increase` |
| `position_hold` | `hold` |
| `capital_release` | `reduce`、`exit` |
| `conditional_monitor` | `conditional_monitor` |
| `wait` | 不接收决策层 action-value；`no_trade`、`observe` 只保留为诊断语义 |

execution、trigger、profile 类学习只进入 `trigger_profile_learning_rows`，用于后续执行画像和触发质量解释。它们不得进入 `decision_learning_rows`，矩阵规定的 `execution_profile_learning_direct_to_rank` 必须为 `false`。

生命周期不匹配的记录进入路由器内部拒绝诊断，不得通过修改 `learning_lane`、`canonical_action_family` 和 `action_preference` 强行进入当前候选。

#### 4.7 修正候选质量

PM 先保留学习修正前的候选质量，再只用当前生命周期允许的正式学习修正候选状态：

- 正向且同生命周期的 action-value 可以提高候选质量和后续资金评估优先程度。
- 负向且同生命周期的 action-value 可以降低候选质量、转入重新确认、限制新增风险和增强释放资金倾向。
- hold 学习只影响现有持仓管理，不直接支持新开仓 rank。
- reduce、exit 学习只影响释放资金路径，不直接压低其他产品的新增风险排名。
- execution、trigger、profile 学习只影响执行画像，不改变方向、候选质量、rank 和手数。

学习修正不得覆盖当前 SCC 事实。没有当日方向、setup、触发和失效边界时，历史正向学习不能把 `no_opportunity` 单独提升为可交易候选。

本步可以更新 `candidate_quality`、`candidate_layer_hint` 和内部生命周期意图，但不得改写原始 `opportunity_state`，也不生成最终动作。候选状态发生变化后，后续步骤继续读取同一个对象。

`candidate_quality` 只由唯一最终 scorecard 计算一次：`opportunity_score + trigger_valid完整性加分 + invalidation_present完整性加分`，再按候选比例语义限制在 `[0,1]`。`opportunity_score` 已经包含 setup、正式学习、profile 和冲突事实，Step2及后续控制不得再次加入这些原始分量或重算 `candidate_quality`。这里的 `[0,1]` 只服务 Step4 层内比例，不改变 Step5 保持有符号且可为负的 `rank_score`。

Step4 还必须在 Step5 之前确定新增风险候选的最终资金层和层内计划比例。冷启动或未验证机会保持 `exploration_probe`；正式 canonical open/add 正向学习只有与当日完整证据、technical 触发和失效边界同时成立时才可升为 `real_budget_entry`，中期基本面明确反向时仍只能保留 probe；成熟重复正收益、强确认、失效边界和合格同向基本面支持同时成立时才可升为 `alpha_scale_entry`。计划保证金比例由最终 `candidate_quality` 在现有区间内连续映射：probe `0.008-0.015`、real `0.030-0.060`、scale `0.060-0.120`、exceptional `0.075-0.130`。这是新增风险仓位的唯一软计划；Step4输出后只允许可用保证金、单品种保证金硬线、总保证金硬线、净敞口、市场最小手数和手数取整收缩，不得再用日盈亏或名义仓位软比例二次改写。`risk_control.max_single_position_ratio`保留为Step4前的名义风险锚，不是Step4后的第二资金所有者。Step4 不读取或等待尚未生成的 `opportunity_rank`。

现有持仓通过真实 transaction 的 `recommendation_id` 追溯原开仓 FAC，并按已结算交易日计算持有天数。没有新的入场 trigger 只表示不增加风险，不等于持仓失效；PM只读取原 FAC 的 `position_invalidation_level`、原始ATR14、期限和持仓依据，绝不复用入场`invalidation_level`。结构位在开仓FAC组装时按盘前参考价校验，次日消费时再按真实开仓成交价校验；合法结构位与`开仓价±ATR×当前真实命中的default/sector倍数`分别计算后取OR，任一触发均形成唯一exit。明确技术反转形成exit，基本面中期反向按既有规则形成reduce，期限到达只用当日技术与基本面强制复评，不自动退出；其余保持既有hold/生命周期判断。现有template/setup ATR覆盖只有setup键精确匹配时才生效，不宣称普遍命中。

当最终生命周期结论为 hold、目标比例与当前比例相同且没有硬风险或真实 reduce/exit 覆盖时，PM直接保留 `current_lots`。不得把旧价格下的持仓比例按新价格重新换算并向零取整，从而制造没有策略依据的减仓。

#### 4.8 状态更新

本步把以下内容写回同一个产品候选状态：

- `effective_memory_summary`
- 正式 action-value 候选集合
- `alpha_setup_profile` 表中学习记录的摘要
- 安全过滤后的策略和 policy state 摘要
- 当前生命周期路由的内部摘要
- `learning_used.memory_retrieval.rejected_or_downgraded` 所需的拒绝诊断材料
- `learning_adjustment_summary`
- 学习修正后的 `candidate_quality`、`candidate_layer_hint` 和内部生命周期意图

本步不创建新的候选对象，不输出独立学习 artifact。第 4 步完成后按最终意图是否实际增加风险分流：

- 非新增风险路径：`Step4 -> Step6`。`wait`、`hold`、`reduce`、`exit`、当前反转的退出腿和 `target_lots=current_lots` 的 `conditional_monitor` 跳过第 5 步，不生成 rank。
- 新增风险路径：`Step4 -> Step5 -> Step6`。从空仓建立新仓的 `open`、`open_probe`、`open_real`，同方向且 `abs(target_lots)>abs(current_lots)` 的 `add/scale`，以及实际增加目标仓位的条件开仓，进入第 5 步执行全市场 rank、预算分配和 position sizing。

这里的 `conditional_monitor` 是 canonical family / 生命周期语义，不是 `final_action` 的新增枚举；最终动作仍由第 6 步按 `current_lots` 与 `target_lots` 形成 `wait/hold`。

两条路径继续传递同一个 PM 内部候选状态，不生成第二套候选对象和中间交易事实。

#### 4.9 状态演化与自检边界

第 4 步的完整 canonical 候选学习池、当前生命周期路由和候选质量修正都是 PM 内部中间状态，不是最终合约学习事实。检索拒绝诊断和路由拒绝诊断只解释材料为什么未被当前候选消费，不得冒充最终决策层学习证据。

不增加风险的候选从第 4 步直接进入第 6 步。凡最终意图实际增加风险的候选，都由第 5 步排名、资金部署和 position sizing 继续更新后再进入第 6 步。

无论是否经过第 5 步，第 6 步都必须从第 4 步保留的完整 canonical 候选学习池重新开始，根据最终 `final_action`、`current_lots`、`target_lots` 和 `pm_lifecycle_learning_trace.contract_lifecycle_port` 重新形成正式 `decision_learning_rows` 和独立的 `trigger_profile_learning_rows`，再写入唯一 `final_action_contract`。第 6 步不得复制第 4 步的 `decision_learning_rows`，也不得让第 4 步未消费的生命周期记录因早期路由被永久丢弃。

在学习边界内，第 6 步只校验最终 `learning_used.alpha_setup_action_values` 的纯净性、最终生命周期与最终 `decision_learning_rows` 的一致性，以及 execution/profile 学习只进入 `trigger_profile_learning_rows`。禁止读取第 4 步初始路由结果作为最终自检输入，禁止比较第 4 步初始路由与第 6 步最终生命周期来判定合约失败。

检索为空、有效学习数量少、匹配层级较弱、完整 canonical 记录与第 4 步当前生命周期不匹配，只进入 diagnostics，不触发最终合约 hard fail。

非完整 canonical、非 `pm_learning` 和 action-value 语义不一致的记录在第 4 步被识别并隔离到拒绝诊断后，不得参与候选质量和后续路由，也不因“已正确拒绝”触发最终合约 hard fail。只有这些非法记录进入候选质量、`learning_used.alpha_setup_action_values`、最终 `decision_learning_rows` 和 `trigger_profile_learning_rows` 时，才属于学习契约污染并触发 hard fail。

research DB 中晚于当前交易日的记录只要未被检索返回，就不属于本次 PM 输入。future dated 记录一旦被 `retrieve_pm_memory` 返回，代表时间边界已经断裂，必须在第 4 步输入校验处 hard fail，禁止把它降级成普通 diagnostics 后继续签约。

#### 4.10 禁止项

PM 不通过 `workflow` 编排层接收学习成果，不让 `workflow` 编排层读取 research DB 和生成学习摘要。

PM 不读取当前交易日和未来日期的学习记录。

PM 不把 weak prior、incomplete prior、empty shell、非 `pm_learning` 记录，以及 `canonical_action_value=false` 或 canonical 字段不完整的 similar SQL prior、fallback prior 写入正式 `learning_used.alpha_setup_action_values`。

PM 不把 `learning_used.memory_retrieval.rejected_or_downgraded` 中的材料用于候选质量、rank、手数、资金部署和最终动作。

PM 不让 execution、trigger、profile 学习直接改变方向、候选质量、rank、资金部署和手数。

PM 不用历史学习重建、补造和改写 SCC，不用历史学习覆盖当日结构化证据。

PM 不在本步生成全市场 `opportunity_rank`、资金部署、目标手数、`lots_delta`、最终动作和执行权限。

PM 不要求第 4 步学习路由与第 6 步最终生命周期保持不变，不执行 Step4 与 Step6 的比较式自检。

PM 不在本步生成 `candidate_contract`、builder 输入、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

PM 不在本步写入 research DB，不生成 DB 记录、本地 artifact 和运行日志物理事实。

PM 不把本步候选状态暴露给 `workflow` 编排层、Auditor、Trader、Reviewer、Researcher 和 PG 作为外部交易事实。

### 5. 新增风险排序与预算分配

#### 5.1 本步目标

第 5 步只处理第 4 步确认会实际增加风险的产品候选，在完整的当日全市场新增风险候选集合中完成统一排名、预算安排和 position sizing。候选包括从空仓建立新仓，以及同方向扩大绝对手数的 `add/scale`。

排名的业务含义只使用矩阵固定的资金投入优先级。`opportunity_rank=1` 表示：在当前 SCC 证据、正式 action-value、产品历史经验和风险约束共同作用下，该候选是本轮最高资金优先级；它不是交易权限，不表示已经校准的盈利概率，也不保证盈利。

排名不是独立研究结论，也不是展示性指标。排名必须直接服务预算：PM 按排名顺序消耗同一个账户预算，先处理更值得投入资金的候选，再处理后续候选。没有进入预算安排的排名不完整；脱离排名单独分配资金同样不允许。

本步仍然只更新 Step1–4 延续下来的同一 PM 内存候选状态。第 5 步不生成 recommendation、合约草稿和任何物理输出。

#### 5.2 进入本步的候选集合

PM 在开始排名前，汇集同一 `config_id`、同一 `trading_date` 下已经完成第 4 步的全部产品候选状态，并只把最终意图会实际增加风险的候选放入统一队列。

进入队列的候选包括：

- `open`
- `open_probe`
- `open_real`
- 同方向且 `abs(target_lots)>abs(current_lots)` 的 `add/scale`
- 旧方向已经由前一张 `exit` 合约退出后，后续从空仓形成的反向 `open/open_probe/open_real`
- 最终实际增加目标仓位并保留 `conditional_trigger_authority` 的条件 `open_probe`

以下状态不进入排名队列：

- `wait`
- `hold`
- `reduce`
- `exit`
- 当前反转的 `exit` 腿
- `target_lots=current_lots`、不增加风险敞口的 `conditional_monitor`
- 已确认不具备新增风险资格或已被输入门拒绝的候选

队列为空是合法状态，表示当日没有需要竞争新增风险预算的候选。候选集合不完整、混入其他交易日或混入非新增风险状态属于 Step5 输入契约错误，不得通过补造 rank 继续运行。

单个分析师形成共享校验通过的完整 setup、具体触发、canonical 失效边界且 `trigger_valid/current_trigger_confirmed=true` 时，即使另外两名分析师为 `no_opportunity`，也不得在 Step5 前清零。该候选必须携带真实 `supporting_signal_count=1`、分析师身份、证据强度、共识分和冲突进入队列；单来源自然获得更低 `cold_start_evidence_quality/rank_score`，不得补分、提高共识或自动授予预算、手数和交易权限。

`workflow` 编排层只负责组织 PM 获得完整的当日输入集合，不计算 rank、不筛选资金候选、不分配预算，也不生成 Step5 结果。

#### 5.3 使用的内部状态

每个新增风险候选沿用同一个 PM 内存状态中的以下事实：

- `ticker`、`trading_date`、`config_id`
- `ticker_side_priority`
- `opportunity_state`
- `candidate_quality`
- `candidate_layer_hint`
- 当前新增风险意图
- `evidence_strength`、`evidence_quality`、setup 和 trigger 质量
- 冲突、缺失证据、风险因素和失效边界
- 第 4 步学习修正后的候选质量
- 第 4 步保留的完整 canonical action-value 候选学习池及当前开仓 lane 路由摘要
- `current_lots`
- 当前品种敞口和账户组合敞口
- Phase1 参考价
- 第 1 步读取的合约乘数和方向保证金率

PM 从同一个账户状态读取：

- `account_equity`
- `margin_used`
- 可用保证金
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

排名使用唯一 `rank_score`。它是七项分量的有符号总和，不做 `[0,1]` 截断：

```text
rank_score =
    当日证据质量积分
  + 候选资金层级积分
  + open/add/scale action-value 积分
  + 产品/setup/trigger 已验证历史积分
  + 当前 trigger 质量积分
  + 资金效率积分
  - 冲突、风险、失效和缺失证据扣分
```

具体积分由 `src/config/rank_score_policy.yaml` 配置，并由 `src/config/dev.yaml` 的 `config_catalogs.rank_score_policy` 载入。当前基线参数如下：

| 积分项 | 当前参数 | 含义 |
|---|---:|---|
| 当日证据质量 | `cold_start_evidence_quality * 0.52` | `cold_start_evidence_quality` 只汇总当日方向、状态、业务/setup、置信度、市场确认和融合共识，保证当前结构化证据是排名主体 |
| `alpha_scale` | `+6.00` | Step4 已确认的放大资金层级积分 |
| `real_budget` | `+3.00` | Step4 已确认的正常资金层级积分 |
| `exploration_probe` | `+0.00` | Step4 已确认的探索资金层级积分 |
| 正向 action-value | `+0.18 * positive_learning_signal` | 已验证正向 open/add/scale 经验 |
| trigger 正向质量 | `+0.08 * trigger_quality_positive_signal` | 与新增风险相关的正向触发经验 |
| 负向 action-value | `-0.18 * negative_learning_signal` | 已验证负向新增风险经验 |
| 近期尾部损失 | `-0.14 * recent_tail_loss_signal` | 抑制重复尾部风险 |
| 入场质量损失 | `-0.16 * entry_quality_loss_signal` | 抑制低质量入场 |
| trigger 净损失 | `-0.10 * net_trigger_quality_loss_signal` | 抑制失效触发模式 |
| action-value 合计边界 | `[-0.35, +0.35]` | 防止历史学习压倒当日事实 |
| 每项 gating failure | `-0.025` | 对未满足条件逐项扣分 |
| gating failure 总上限 | `-0.16` | 限定该类扣分边界 |
| 资金效率 | 最高 `+0.02` | 同等质量下优先资金效率更高者 |
| 当日 trigger 质量 | `+0.08 * trigger_quality_score` | 只读取PM由已验证SCC重建的当日technical/event执行证据；历史trigger结果不得进入本分项 |

`product_setup_trigger_history`、当前 trigger 质量、市场冲突、关键数据缺口、基本面缺口和失效风险继续按 catalog 中对应权重计入。所有积分必须保留组成项，不能只保存一个无法解释的总分。

`rank_score_policy.rank_score` 下七个参数组与 `rank_score_components` 固定同名；每个组内的权重键与 Python 消费的输入字段同名。调参时禁止新增 `_weight`、`_bonus` 别名或只改 YAML 不改消费端。

`execution_profile_learning_weight` 不属于排名配置，catalog 不得保留该入口。按第 4 步已经确定的学习边界，execution/profile 学习只能进入执行画像，不得直接或通过 `opportunity_score` 间接增加或扣减 `rank_score`，不能借 trigger 质量名义重新进入决策层。

#### 5.6 action-value 与已验证经验如何影响排名

只有第 4 步接收的完整 canonical action-value，并且在本步匹配 `open`、`add`、`scale`、`increase` 新增风险 lane，才允许影响排名。

已验证的产品、方向、setup 和 trigger 经验通过两条路径自然提高资金优先级：

1. 正向 canonical action-value 提高 `open_add_action_value_delta`。
2. 重复出现且样本、收益、回撤和触发质量满足配置要求的经验提高 `product_setup_trigger_history`，使候选进入更高的资金层级。

因此，在当日证据仍然成立、风险边界完整的前提下，经过真实交易验证且持续为正的产品候选应天然排在未验证候选之前，并获得更高的预算竞争优先级。负向 action-value、近期尾部损失和低质量入场经验则必须降低排名或阻止扩大风险。

历史经验不能单独创造候选。即使 action-value 很强，只要当日 SCC 没有方向、setup、触发、失效边界或必要证据，候选仍不得进入新增风险资金队列。

只有 `matrix_action_canonical.md` 允许的 `open`、`add`、`scale`、`increase` 新增风险 lane 可以进入新增风险积分；其他 family/lane 均不得进入。`learning_used.memory_retrieval.rejected_or_downgraded` 所对应的 weak prior、incomplete prior、similar SQL prior 和 fallback prior 不得影响 rank。

这里的 `open/add/scale/increase` action-value 只能影响与其新增风险语义匹配的候选。当天是否必须排名统一由最终持仓变化判断：从空仓建立非零仓位，或同方向且 `abs(target_lots)>abs(current_lots)`，均属于新增风险资金请求。

#### 5.7 排名顺序

交易属性必须在进入 rank 前由 Step4 的最终 scorecard 和 `final_entry_authority` 确定。`exploration_probe`、`real_budget_entry`、`alpha_scale_entry` 均由 Step4 的当日证据、正式学习和失效边界形成；rank 不生成、修改或升级资金层。

PM 只对实际增加风险的候选排序。资金层、当日证据、正式 open/add 学习、setup 历史、当前 trigger 质量、资金效率及冲突/失效风险各自只进入一次 `rank_score`；最终排序固定为 `rank_score` 降序，再以标准化 `ticker` 作为唯一稳定兜底键，不再用资金层、tier、证据、`candidate_quality` 或资金效率形成第二套排序：

1. `alpha_scale_entry`：当前证据成立，且有重复正向真实经验支持的已验证候选。
2. `real_budget_entry`：当前证据完整的 `tradeable_candidate`。
3. `exploration_probe`：尚需以小资金验证的 `probe_candidate` 或合法条件候选。

`rank_score` 可以为负；负分仍保留真实大小关系并正常参加排序、probe 资格和预算流程，不构成交易禁入规则。它只表达相对投资价值和资金优先级，不是未来盈利概率或盈利金额。所有比较项完全相同时，使用标准化 `ticker` 作为固定最终排序键。

每个进入队列的候选只能获得一个连续、唯一的全市场 `opportunity_rank`。产品内部 `side_priority`、`ticker_side_priority` 不能替代全市场 rank。

无论 `rank_score` 或 `opportunity_rank` 多高，`exploration_probe` 始终是小仓试探，不得由 Step5 升为 `real_budget_entry` 或 `alpha_scale_entry`；其计划比例已由 Step4 按 `candidate_quality` 在 `0.008-0.015` 内确定，rank 工具不重复生成第二套比例。层级分采用6/3/0，严格大于其余六项的最大合法总跨度，因此任意alpha_scale都高于任意real，任意real都高于任意probe；同层内部仍由同一个总分的证据、历史学习、setup、当日trigger、资金效率和风险拉开顺序。

#### 5.8 排名与预算原子绑定

PM 按 `opportunity_rank` 从 1 开始顺序消费同一个账户可部署预算。处理每个候选时，必须形成矩阵已有资金部署事实：

- 排名前的账户已用保证金比例
- 候选所需保证金比例
- 选中后的账户保证金比例
- 当前品种与选中后的单品种保证金比例
- 当前组合净敞口与选中后的预计净敞口
- `selected_for_capital_deployment`
- `capital_allocation_reason`
- `capital_layer`
- `capital_ratio_source`

候选只有同时满足可用保证金、单品种上限、组合保证金预算、净敞口上限、回撤和冷却限制时，才允许占用预算。批准后立即更新同一内部账户预算游标，后续候选只能使用剩余预算。

排名靠前不绕过硬约束。即使 `opportunity_rank=1`，资金不足、单品种超限、组合超限、净敞口超限或风险状态禁止开仓时，也必须 `selected_for_capital_deployment=false`，并写入 `capital_allocation_reason=no_rank_or_budget_no_new_exposure`，把 `target_lots` 还原为 `current_lots`。被拒绝的候选保留 rank 和完整 rank 解释字段，不得伪造为无机会。

rank 只能决定既定交易属性候选的资金竞争顺序，并可因预算不足拒绝部署；不得改变 `authority_type`、`max_allowed_margin_ratio`、probe/real 属性、方向和原始风险权限。

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

PM 再依次施加可用保证金、单品种保证金硬上限、组合保证金硬上限、净敞口上限、风险等级、回撤、冷却和最小真实交易预算约束，形成矩阵登记的 `position_sizing_result`。Step4前使用的名义仓位锚和已删除的品种日盈亏软控制不得在这里成为第二套仓位上限。该对象记录 `current_lots`、`target_lots`、`lots_delta`、资金占用、风险约束和计算理由，不新增 sizing 子字段。

不足一手时不得为了“必须交易”而向上取整。`reverse` 必须先计算原持仓释放，再只对反向新增风险部分占用预算，不能把平旧仓和开新仓的保证金重复计算。

`build_position_sizing_result` 记录最终测算事实，但不决定最终动作。第 6 步根据该对象中的 `current_lots`、`target_lots`、`lots_delta` 和最终权限原子生成唯一合约。

#### 5.11 配置微调边界

上述积分权重和预算比例是当前回测基线。后续允许依据长期回测结果微调，但至少应先完成 40 个无系统错误、无契约污染、无旁路审计异常的干净回测交易日；该要求是 PM 调参治理规则，不伪装成无人读取的 YAML 参数。

微调只修改 catalog 和 `dev.yaml` 对应参数，不在代码中散落新的隐式常量。每次只调整一组可归因参数，并比较排序稳定性、资金利用率、收益、回撤、尾部损失和预算拒绝分布。系统错误、artifact 错误、自检错误和数据污染期间的结果不得用于调参。

调参不得改变以下边界：rank 只服务资金优先级，action-value 不覆盖当日 SCC，execution/profile 学习不直连 rank，硬风险上限不由积分覆盖，最终交易权仍只由第 6 步签发。

#### 5.12 状态更新与自检边界

本步把以下内容写回同一个 PM 内存候选状态：

- `rank_score_components`
- `rank_score`
- `opportunity_rank`
- `rank_source`
- `rank_scope`
- `capital_rank_generated_by`
- `rank_capital_role`
- `capital_layer`
- `capital_ratio_source`
- `rank_reason`
- `rank_input_components`
- `rank_semantics_version`
- `opportunity_rank_meaning`
- `rank_is_capital_priority`
- `rank_is_not_trade_authority`
- `selected_for_capital_deployment`
- `capital_allocation_reason`
- `position_sizing_result`
- 更新后的内部账户预算游标和组合预计敞口

本步不创建第二个候选对象，不输出独立 rank、预算或 sizing artifact。更新后的同一候选状态进入第 6 步。

第 5 步只执行新增风险候选集合和资金测算输入的契约校验，不执行最终合约自检。预算拒绝、手数被约束为不增加风险、新增风险候选还原为当前持仓，都属于正常状态演化。

第 6 步只根据最终候选状态形成 `capital_deployment`、`evidence_used.position_sizing_result`、最终 `lifecycle_learning_trace`、最终 `learning_impact_delta` 和最终交易字段，并只检查最终 `final_action_contract` 自身一致性。禁止比较第 5 步约束前目标与第 6 步最终动作，禁止要求 Step4 排名预期、Step5 初始手数和 Step6 最终合约保持不变。

#### 5.13 禁止项

PM 不让 `wait/hold/reduce/exit`、当前反转的退出腿、`target_lots=current_lots` 的条件监控和其他不增加风险的候选进入全市场 rank 队列；同方向实际扩大绝对手数的 `add/scale` 必须进入。

PM 不生成脱离预算安排的展示性 rank，不绕过 rank 顺序分配新增风险资金。

PM 不把 rank 当作交易授权、盈利保证和硬风险豁免。

PM 不让 action-value 覆盖当日 SCC，不让 hold、reduce、exit、execution、profile 和被拒绝学习进入新增风险积分。

PM 不让 `learning_used.memory_retrieval.rejected_or_downgraded`、weak prior、incomplete prior、similar SQL prior 和 fallback prior 影响 rank、预算和手数。

PM 不把 execution/profile 学习通过 `trigger_execution_quality` 或其他别名重新直连 rank。

PM 不在工具和代码中写死替代 catalog 的积分权重、预算比例和手数边界，不根据单次回测临时改分。

PM 不在本步生成或修改 `candidate_contract`、builder 输入、`final_action_contract`、`FuturesRecommendation` 和任何 recommendation。

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

不增加风险的候选从第 4 步直接进入本步：

```text
Step4 -> Step6
```

包括 `wait`、`hold`、`reduce`、`exit`、当前反转的退出腿和不增加风险敞口的 `conditional_monitor`。这些动作不生成 `opportunity_rank`。

新增风险候选经第 5 步进入本步：

```text
Step4 -> Step5 -> Step6
```

包括 `current_lots=0` 且 `target_lots!=0` 的 `open`、`open_probe`、`open_real`，同方向且 `abs(target_lots)>abs(current_lots)` 的 `add/scale`，以及实际增加目标仓位的条件 `open_probe`。反转必须先由当前 `exit` 合约退出；后续从空仓建立反向新仓时才作为新的新增风险候选进入第 5 步。

两条路径进入第 6 步的都是同一个 PM 内存候选状态，不是候选合约、recommendation 草稿、snapshot 草稿或 artifact。

#### 6.3 最终输入校验

第 6 步开始前只校验最终签约所需输入是否完整、类型是否正确：

- `ticker`、`trading_date`、`config_id` 和 `source_type` 存在。
- 原始 `signal_collection_contract` 存在，且 `source_agent="signal_collector"`、`collector_decision_boundary="no_trade_authority"`。
- 当前持仓、账户权益、可用保证金和风险空间存在。
- 计划参考价、合约代码、合约乘数和方向保证金率有效。
- 最终候选方向、触发、失效边界、风险原因和权限状态可解释。
- 第 4 步完整 canonical 学习候选池仍保留，且未混入 future dated 记录。
- 最终状态若从空仓建立非零仓位，或同方向且 `abs(target_lots)>abs(current_lots)`，必须存在第 5 步唯一全市场 rank、预算结论和 position sizing 结果。
- 最终状态不增加风险时不要求也不补造 rank。新增风险候选已经在第 5 步获得 rank 后因预算拒绝还原为 `target_lots=current_lots` 时，保留该次真实 rank 和拒绝事实。

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

代码梳理时，`build_final_action_contract` 的输入必须从旧的 builder 输入、`candidate_contract`、`opportunity_scorecard` 草稿和 recommendation snapshot 收窄为一个最终 PM 内存候选状态。工具不得接收或返回第二套交易计划。

#### 6.5 确定最终目标手数与动作

新增风险路径读取第 5 步 `position_sizing_result` 中的 `target_lots`。第 6 步签约时才把该确定性测算对象写入 `evidence_used.position_sizing_result`。非新增风险路径在本步根据最终持仓管理状态确定目标手数：

| 最终状态 | `target_lots` |
|---|---|
| `wait` | `current_lots=0` 且 `target_lots=0` |
| `hold` | `current_lots` 非零且 `target_lots=current_lots` |
| `reduce` | 与当前持仓同号，且 `abs(target_lots) < abs(current_lots)` |
| `exit` | `0` |
| `no_rank_no_new_exposure`、`no_rank_or_budget_no_new_exposure` | 等于 `current_lots`，最终为 `wait/hold` |
| 不增加风险的 `conditional_monitor` 生命周期语义 | 等于 `current_lots`，`final_action` 仍按空仓或持仓事实取 `wait/hold` |

PM 只从最终 `current_lots`、最终 `target_lots` 和最终权限状态调用 `classify_position_transition`，一次性形成：

- `final_action`
- `lots_delta = target_lots - current_lots`
- `target_position_ratio`
- `authority_type`

新增风险候选未获得 rank 时，必须写入 `capital_allocation_reason=no_rank_no_new_exposure`；已有 rank 但未获预算时，必须写入 `capital_allocation_reason=no_rank_or_budget_no_new_exposure`。两者都必须恢复 `target_lots=current_lots`：空仓最终为 `wait`，已有持仓最终为 `hold`，并清除本次新增风险执行权限。

反转遵守 `matrix_action_canonical.md`：当前合约先以 `exit` 退出旧方向；后续反向新风险必须重新获得 `opportunity_rank`，再由 `open/open_probe/open_real` 合约授权。不得自创 reverse final action，也不得用一条 recommendation 同时表达两腿。

#### 6.6 形成最终生命周期

PM 根据最终动作、最终 `current_lots`、最终 `target_lots` 和最终条件权限使用共享最终学习生命周期语义，形成 `pm_lifecycle_learning_trace.contract_lifecycle_port`：

| 最终合约事实 | `pm_lifecycle_learning_trace.contract_lifecycle_port` |
|---|---|
| `0 -> 非0` 新开，或同方向扩大绝对手数 | `open_add_new_risk` |
| 减仓或退出 | `reduce_exit` |
| 只保留触发监控、当前不增加敞口 | `conditional_monitor` |
| 非零持仓手数不变且不是条件监控 | `hold` |
| 空仓等待且不是条件监控 | `wait` |

最终生命周期只由最终合约事实决定。第 3 步的 `primary_lifecycle_action_port`、第 4 步临时学习路由和第 5 步约束前风险意图都不进入最终生命周期判定。

条件 `open_probe` 只要最终为 `0 -> 非0`，决策学习生命周期仍是 `open_add_new_risk`；`requires_intraday_confirmation=true` 继续约束 Trader 等待盘中触发，不把该开仓改写成 `conditional_monitor`。`conditional_monitor` 仅用于 `target_lots=current_lots` 且只保留监控的合约。

本步不调用 `build_lifecycle_transition_diagnostic`，不生成初始/最终生命周期对照表，不检查生命周期是否与早期状态保持不变。

#### 6.7 按最终生命周期重新形成学习事实

PM 从第 4 步保留的完整 canonical action-value 候选学习池重新开始，按最终 `pm_lifecycle_learning_trace.contract_lifecycle_port` 调用 `route_lifecycle_learning`：

- `open_add_new_risk` 只接收 `open`、`add`、`scale`、`increase` 决策学习。
- `hold` 只接收 `hold` 决策学习。
- `reduce_exit` 只接收 `reduce`、`exit` 决策学习。
- `conditional_monitor` 只接收 `conditional_monitor` 决策学习。
- `wait` 不接收决策层 action-value。

最终形成两个严格分层的列表：

- `decision_learning_rows`：与最终生命周期匹配的决策层学习。
- `trigger_profile_learning_rows`：execution、trigger、profile 类执行画像学习。

只有最终 `decision_learning_rows` 中实际被最终动作消费的完整 canonical 记录，才进入 `learning_used.alpha_setup_action_values`。对 `hold/reduce/exit`，最终生命周期匹配仍不等于实际消费：必须由软生命周期控制精确选中同一 action-value ID、真实改变最终动作或比例，且影响未被后续规则覆盖。负向hold学习若精确造成同方向减仓，可保持原hold family/lane及ID进入reduce FAC，但不得重标为reduce或放行其他hold记录。结构/ATR 止损、明确技术反转、基本面中期反向及其他独立确定性生命周期规则不得冒领同 lane 学习；这些当日结果可以留在`pm_lifecycle_learning_impact_delta`，但没有匹配正式ID时不构成历史学习消费。不得先截取 Step4 列表再路由，不得复制 Step4 临时 `decision_learning_rows`，不得让未匹配或未产生实际影响的记录进入正式主证据列表。

`trigger_profile_learning_rows` 只进入 `learning_used.pm_lifecycle_learning_trace` 及执行画像摘要，矩阵规定的 `execution_profile_learning_direct_to_rank` 必须为 `false`。它不能改变最终动作、candidate quality、rank、预算和手数。

`learning_used.memory_retrieval.rejected_or_downgraded` 和最终生命周期未接受的完整学习只保留必要 provenance 与拒绝原因，不进入正式决策列表。

#### 6.8 原子构建 final_action_contract

`build_final_action_contract` 只读取最终 PM 内存候选状态，并一次性创建 `agentquant.final_action.v1` 合约。字段范围以“二、输出 / 1.2 包含内容”为唯一白名单。

装配顺序固定为：

1. 产品、日期、配置、合约和 `source_agent`。
2. `final_action`、`current_lots`、`target_lots`、`lots_delta` 和 `target_position_ratio`。
3. `authority_type`、`execution_profile`、`trigger_source`、`entry_trigger`、`invalidation`、`valid_until` 和盘中确认字段。
4. `risk_controls`、`capital_controls`、`max_allowed_margin_ratio` 和 `reason_codes`。
5. 参考价、合约乘数、保证金率和 `margin_ratio`。
6. 最终 `evidence_used` 和原始 SCC 快照引用关系。
7. 最终生命周期学习事实。
8. 新增风险路径的最终 rank、预算和 position sizing；第 5 步预算拒绝路径保留真实 rank 与拒绝事实；直接非新增风险路径只保留无 rank 说明和最终手数摘要。
9. `created_at`。

新增风险候选的 `capital_deployment` 必须与第 5 步最终状态一致，并只使用矩阵已有字段。资金部署结果固定为三种合法形态：

| 资金部署形态 | 矩阵字段 | 最终交易事实 |
|---|---|---|
| 新增风险获准部署 | `selected_for_capital_deployment=true`；存在 `opportunity_rank`、满足 `rank_capital_layer_contract` 且具有 `capital_allocation_reason` | 才允许从空仓形成 `open/open_probe/open_real`、同方向形成 `add/scale`，或保留经 rank 授权且增加目标仓位的条件开仓合约 |
| 新增风险未获得 rank | `selected_for_capital_deployment=false`；`capital_allocation_reason=no_rank_no_new_exposure`；不存在 `opportunity_rank` | 固定还原为 `target_lots=current_lots`，空仓为 `wait`、已有持仓为 `hold`，不得保留本次新增风险权限 |
| 已有 rank 但未获预算 | `selected_for_capital_deployment=false`；`capital_allocation_reason=no_rank_or_budget_no_new_exposure`；保留 `opportunity_rank` 和完整 rank 解释字段 | 固定还原为 `target_lots=current_lots`，最终为 `wait/hold`，不得绕过预算继续增加风险 |

`wait/hold/reduce/exit` 从 Step4 直接进入 Step6，不要求 `opportunity_rank`。同方向实际扩大绝对手数的 `add/scale` 必须经过 Step5；手数不变的 `hold` 不得伪装为 `add/scale`。`conditional_monitor` 只表达监控，不是开仓动作；只有实际增加目标仓位的条件 `open_probe` 合约才必须经过 rank。

Step5 拒绝或其他正式门控使最终 `target_lots=current_lots` 时，Step6 必须按最终手数清除先前条件执行标记并写入 `execution_profile=hold/trigger_source=none`；最终 reduce/exit 写入 `execution_profile=exit_immediate/trigger_source=position_lifecycle`。该收口只反映最终生命周期，不恢复已拒绝的新增风险权限。

`reverse` 只表示 `open_add_new_risk` 学习家族。执行必须先由 `exit` 合约退出旧方向，再由后续已获得 rank 的新风险合约授权反向开仓；不得把反转写成一个自创 final action 或一条同时完成两腿的 recommendation。

合约不得包含 `candidate_contract`、矩阵列明的 PM 内部 draft、builder 输入、scorecard 草稿、rank 草稿、预算草稿和任何可被解释为第二套交易计划的字段。

#### 6.9 生成 FuturesRecommendation

PM 只从已经形成的 `final_action_contract` 派生 `FuturesRecommendation`：

- `config_id`、日期、产品和合约身份来自最终合约。
- recommendation 顶层动作和手数由 `recommendation_intent_from_lots(current_lots, target_lots)` 映射。
- `base_price`、价格来源和日期来自最终合约。
- `justification` 只摘要最终动作、手数、核心证据和原因代码，不产生新动作。
- `status` 反映本次 recommendation 状态，不改写最终交易事实。
- `audit_payload` 在 PM 返回时保持空值。

`signal_snapshot` 只允许包含：

- 原样深拷贝的 `signal_collection_contract`
- 唯一 `final_action_contract`
- `pm_six_step_trace`
- `matrix_field_semantics.md` 已登记的必要 header、来源字段和最终合约派生摘要

recommendation 顶层动作、手数、价格、产品和日期必须与 `final_action_contract` 对齐。反转时本次 recommendation 只表达当前 `exit` 腿；后续反向新增风险必须由另一张经过 rank 和审计的最终合约授权。

#### 6.10 Step6 最终检查

第 6 步只保留两类针对最终输出的检查。

`step6_contract_generation_check` 检查原子生成结果：

- 只生成一个 `final_action_contract` 和一个 `FuturesRecommendation`。
- 最终合约位于 `signal_snapshot.final_action_contract`。
- 原始 SCC 位于 `signal_snapshot.signal_collection_contract` 且未被改写。
- recommendation 顶层字段来自最终合约。
- snapshot 不含 PM 内部候选状态、builder inputs、合约草稿和 rank/预算草稿。

`check_final_action_contract` 只使用共享 `final_action_semantics` 检查最终合约自身：

- 矩阵规定的必填字段和结构化对象存在、位置正确且类型正确。
- `lots_delta = target_lots - current_lots`。
- `final_action`、`current_lots`、`target_lots` 和 `lots_delta` 一致。
- `authority_type`、canonical `execution_profile`、非空且与 profile 自洽的 `trigger_source`、盘中确认字段和 `reason_codes` 一致。
- 保留新增风险敞口的条件 `open_probe` 具有具体 `entry_trigger`；所有新增风险 FAC 具有来自同一执行证据的 canonical 失效边界；`conditional_monitor` 不被解释为开仓授权。
- 最终实际增加风险的合约具有第 5 步唯一 rank、预算和 sizing；Step5 拒绝结果只保留对应拒绝事实；不增加风险的合约没有 rank。
- `evidence_used.position_sizing_result` 的 `target_lots`、`lots_delta` 与最终合约一致。
- `learning_used.alpha_setup_action_values` 纯净且与最终生命周期匹配。
- decision learning 与 trigger/profile learning 分层正确。
- 最终合约不含 PM 内部中间状态和第二套交易计划。

rank 自检只使用矩阵已有字段和固定原因代码：

| 最终合约形态 | 必须检查 | 禁止误判 |
|---|---|---|
| 最终新增风险获准 | 从空仓建立非零仓位，或同方向且 `abs(target_lots)>abs(current_lots)`；`selected_for_capital_deployment=true`，存在 `opportunity_rank`，满足 `rank_capital_layer_contract`，并具有 `capital_allocation_reason` 和 `evidence_used.position_sizing_result` | 不得允许无 rank 的 `open/open_probe/open_real/add/scale` 增加风险 |
| `no_rank_no_new_exposure` | 不存在 `opportunity_rank`，`selected_for_capital_deployment=false`，`target_lots=current_lots`，空仓最终为 `wait`、已有持仓最终为 `hold`，无本次新增风险权限 | 不得因缺少 rank 判错，也不得保留原新增风险目标 |
| `no_rank_or_budget_no_new_exposure` | 保留 `opportunity_rank` 和完整 rank 解释字段，`selected_for_capital_deployment=false`，`target_lots=current_lots`，空仓最终为 `wait`、已有持仓最终为 `hold`，无本次新增风险权限 | 不得把 rank 当作交易权限，不得因最终不增加风险判定 rank 合约失败 |
| 共享语义解释为 `conditional_monitor` 且不增加风险 | `final_action` 为 `wait/hold`、`target_lots=current_lots`，存在矩阵要求的 `capital_deployment` 和 `capital_allocation_reason`，且不存在 rank 专属字段 | 不得要求 `opportunity_rank`，不得解释为已授权开仓 |
| 原生非新增风险行为 | `final_action` 为 `wait/hold/reduce/exit`，且最终持仓变化与动作一致 | 不得因合法非新增风险行为缺少 rank 判错，也不得允许其携带伪 rank |

PM 自检不得自建资金部署状态字段、私有动作集合和私有生命周期字段。动作解释统一调用 `final_action_semantics`；学习 family/lane 统一遵守 `matrix_action_canonical.md`。

`check_final_action_contract` 必须收窄为 `check_final_action_contract(final_action_contract)`。PM 不再把 artifact 对象、`signal_snapshot`、Step1–5 状态和早期生命周期传给合约自检；artifact 边界由 PM 返回后的 Auditor、保存层和 PG 检查。

两个检查都只读、无副作用。检查器只能报告错误，不能补字段、改动作、改手数、改变生命周期和修复合约。

#### 6.11 pm_six_step_trace

最终检查通过后，`signal_snapshot.pm_six_step_trace` 只保留矩阵已登记的：

- `step6_contract_generation_check`
- `pm_contract_self_check`

`pm_six_step_trace` 只证明唯一最终对象如何生成并通过最终检查。它不保存 Step1–5 原始状态，不保存 candidate contract、builder inputs、旧生命周期、旧手数、旧 rank 和任何跨步骤比较结论。

是否需要 rank、是否已部署和未部署原因全部由最终 `final_action_contract` 的既有字段解释，不在 `pm_six_step_trace` 自创字段。

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

PM 不要求所有产品都有 rank；只对最终实际增加风险的 `open/open_probe/open_real/add/scale` 要求排名，不把 `wait/hold/reduce/exit` 等 Step4 直接进入 Step6 的合法合约误判为缺少排名。

PM 不要求已排名但未获预算的候选继续保持新增风险生命周期，不把正常预算拒绝误判为 Step5/Step6 不一致。

PM 不在 `final_action_contract` 中保留矩阵列明的 PM 内部 draft、scorecard 草稿、rank 草稿、预算草稿、第二套手数计划和内部审计对象。

PM 不让 recommendation 顶层字段形成第二套交易事实；所有顶层摘要必须从最终合约派生。

PM 不在本步调用 Auditor，不填充 `audit_payload`，不写 DB，不生成本地 artifact，不写运行日志物理事实。

PM 不向 `workflow` 编排层、Trader、Reviewer、Researcher 和 PG 暴露内部候选状态；唯一返回对象只能是通过最终检查的 `FuturesRecommendation`。
