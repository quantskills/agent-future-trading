# AgentQuant 工作流

本文用于固定现有系统中各智能体的编排顺序、内存传递、物理化落点和三类核心载体的完整结构，使后续逐个梳理智能体时能够先确认上游传入事实、下游必需事实和字段责任，避免中途补字段、重复解释信息或建立旁路载体。本文不把 workflow 编排层视为智能体，不替代 `matrix_field_semantics.md` 的字段语义，也不替代各智能体机制文档。

## 一、工作流程

本文中的 `T` 始终表示期货逻辑交易日，`Prev(T)` / `Next(T)` 由正式交易日与夜盘映射机制确定，不按自然日加减。对有夜盘的品种，物理 `Prev(T)` 晚间产生的分钟线、订单和成交可以属于逻辑 `T`；`created_at`、分钟 `datetime` 表示物理时间，业务 `trading_date` 仍统一登记逻辑交易日。

Proposal 为逻辑 `T` 生成策略时，`reference_portfolio_id` 必须指向 `portfolio.trading_date=Prev(T)` 的最近已结算账户/持仓快照；PandaAI 日线与新闻不得晚于 `Prev(T)`。历史日线查询默认不包含结束日，需要精确消费该日时必须显式使用包含结束日语义。PandaAI 日线保留现有持久化缓存，扩展数据按接口名和完整历史参数持久化成功响应及确定性空响应；分钟线仍由 Trader 在实际获批合约上按 cutoff 调用。PandaAI `500009` 必须转为 `pandaai_daily_quota_exhausted` 并立即终止当日预取，禁止继续遍历剩余品种。Finoview 按同一 factor catalog 中的实际频率、freshness、`release_lag_days` 和正式交易日机制选择最新可见 `tradeDate`，Router 格式化输入与 factor snapshot 共用同一选择器；`recordTime` 不作为发布时间或可见边界。三份 AEC、SCC、recommendation 及其 `effective_trade_date` 均属于逻辑 `T`。

```text
【物理输入】
PandaAI行情 / Finoview基本面文件 / 新闻txt
        ↓
【内存：三个分析师】
workflow 编排层以交易日期和主配置启动 technical、fundamental、commodity_news
三个分析师分别从研究学习SQL读取仅限当前交易日前的本专业学习成果，
用于完善LLM提示词和校对当日信号，
再各生成一份内含 metadata.action_evidence_contract 正式证据的 AnalystSignal，
并将三份 AnalystSignal 返回 workflow 编排层
        ↓
【物理输出①】
workflow 编排层将三个分析师返回的 AnalystSignal
写入 state["analyst_signals"]，继续在内存中向下传递；
同时物理化为：
→ 三条 signal SQL记录，artifact_json只保存经过共享校验的action_evidence_contract
→ 三份只呈现同一action_evidence_contract的分析师报告
分析师最终出口清除自由文本justification、LLM路由、内部参数、校准过程、学习检索上下文、report_sections和其他中间状态；
Workflow取得真实ID后，每份内存信号metadata只允许action_evidence_contract和signal_record_id
        ↓
【内存：Signal Collector】
workflow 编排层将 state["analyst_signals"] 中的三份 AnalystSignal 传入
Signal Collector提取各自 metadata.action_evidence_contract
生成唯一 SCC并写回 state["signal_collection_contract"]
        ↓
【内存：PM】
workflow 编排层将包含 state["analyst_signals"] 和 state["signal_collection_contract"] 的运行状态传入 PM
PM接收时，只使用 state["analyst_signals"] 核对启用分析师的身份、数量，
以及SCC来源引用是否完整、匹配
随后调用 build_pm_evidence_signals_from_scc，
只从SCC形成PM证据读取对象
PM Step1–3基于SCC、账户、持仓和合约信息更新同一个 pm_state
PM Step4调用 pm_decision_memory_retrieval.retrieve_pm_memory，
从研究学习SQL读取当前交易日前、与产品、方向和生命周期匹配的PM学习成果，
并继续更新同一个 pm_state
PM Step5仅对新增风险候选完成排名、预算分配和手数计算，
其中合法 `watch_for_trigger` 只有在获得非零条件目标时才作为新增风险候选进入同一排名；未获选或零手条件观察不携带资金 rank，
并继续更新同一个 pm_state；非新增风险候选跳过Step5
PM Step6基于最终 pm_state生成 FuturesRecommendation，
其 signal_snapshot 内含：
→ signal_collection_contract
→ final_action_contract
→ pm_six_step_trace
并将 FuturesRecommendation 返回 workflow 编排层
        ↓
【物理输出②】
workflow 编排层接收 PM 返回的 FuturesRecommendation，
并将其物理化为：
→ 一条 futures_recommendation SQL记录
→ signal_snapshot写入该记录；超出内联限制时生成对应 recommendation artifact
        ↓
【内存：Auditor】
workflow 编排层将已保存的 FuturesRecommendation、账户状态和配置传入 Auditor
Auditor读取 FuturesRecommendation.signal_snapshot["final_action_contract"]，
生成 audit_verdict和audit_payload，
不修改 final_action_contract，并将审计结果返回 workflow 编排层
        ↓
【物理输出③】
workflow 编排层接收 Auditor 返回的审计结果，
并更新同一条 futures_recommendation SQL记录：
→ signal_snapshot["auditor"]写入审计摘要
→ audit_payload写入完整审计结果；超出内联限制时生成对应 recommendation artifact
        ↓
【内存：Trader】
workflow 编排层读取审计后的 FuturesRecommendation，
并将其与当前账户、持仓和主配置传入 Trader
Trader读取 FuturesRecommendation.signal_snapshot["final_action_contract"]，
通过行情路由读取盘中行情，
判断触发、未触发或成交，并形成执行结果
        ↓
【物理输出④】
workflow 编排层调度 Trader 执行链，
Trader执行链将执行事实物理化为：
→ intraday decision写入 futures_intraday_decision SQL记录
→ execution_result写入同一条 futures_recommendation记录的signal_snapshot
→ 以原始完整Auditor audit_payload为基底追加trade_contract_audit、execution_translation、execution_result和phase2_execution
→ 成交结果写入 futures_transaction SQL记录
→ 更新同一条 futures_recommendation记录的状态和执行价格
        ↓
【内存：Accountant】
workflow 编排层在 Phase2 完成后，以交易日期和主配置启动 Accountant
Accountant从 SQL 读取最近已结算 portfolio及当日未入账的 futures_transaction，
通过行情路由读取结算行情，并读取合约信息，
在内存中计算账户、保证金、持仓和PnL，形成结算结果
        ↓
【物理输出⑤】
workflow 编排层调度 Accountant 结算链，
Accountant结算链将结算事实物理化为：
→ 结算后账户和当前持仓写入 portfolio SQL记录，其中持仓唯一物理路径为portfolio.positions
→ 日结算结果写入 daily_settlement SQL记录，其中结算持仓快照为daily_settlement.positions_snapshot
→ 分产品盈亏写入 ticker_daily_pnl SQL记录
→ 回填当日 futures_transaction的结算价并标记为已入账
        ↓
【内存：Reviewer】
workflow 编排层在 Phase3 完成后，以交易日期和主配置启动 Reviewer
Reviewer从 SQL 读取 Phase1–3阶段状态、分析师signal、审计及执行后的 futures_recommendation、
futures_transaction、daily_settlement、最新portfolio.positions和对应daily_settlement.positions_snapshot，
在内存中复盘决策、审计、执行和结算事实，分析交易结果并进行事实归因，
形成Phase4复盘结果和研究输入材料
        ↓
【物理输出⑥】
workflow 编排层调度 Reviewer 复盘链，
Reviewer复盘链将复盘结果物理化为：
→ Phase4状态写入 trading_day_phase SQL记录
→ 复盘摘要写入 daily summary JSON和CSV
→ 每日交易报告写入 transaction report
Researcher后续读取已完成的Phase4状态及完整SQL事实链，不单独生成“研究输入材料”
        ↓
【内存：Researcher】
workflow 编排层在 Phase4 完成后，以交易日期和主配置启动 Researcher
Researcher从 SQL 读取已完成的Phase4状态、审计及执行后的 futures_recommendation、
当日 futures_transaction和daily_settlement，
分别验证参考组合日期为正式 `Prev(T)`，三份持久化 AEC、SCC、recommendation、execution、transaction和settlement属于逻辑 `T`，
在内存中形成已结算交易episode、未交易机会、结构化研究和未来学习结果
        ↓
【物理输出⑦】
workflow 编排层调度 Researcher 学习链，
Researcher学习链将研究与未来学习结果物理化为：
→ 交易episode、未交易机会、action-value、profile及policy/state写入相应研究学习SQL表
→ 历史学习快照写入Researcher MD和JSON artifact
→ researcher_learning_completed事件写入 learning_event_log SQL记录
上述 SQL、完成事件、外置 payload artifact、template prior 和历史学习快照按单次 Researcher 运行原子提交；失败时全部回滚，本次新 artifact 不得残留，已有合法 artifact 不得被覆盖或删除
        ↓
【下一交易日学习回流】
workflow 编排层以新的交易日期和主配置启动分析师及PM链路
逻辑 `T` 的Phase4与Researcher完成后，成果可由物理 `T` 晚间运行、目标为逻辑 `Next(T)` 的Proposal读取；严格日期过滤禁止其反向影响已经完成的逻辑 `T`，
三个分析师分别从研究学习SQL读取当前交易日前的本专业学习成果，
用于完善LLM提示词和校对当日信号
PM在Step4通过 pm_decision_memory_retrieval.retrieve_pm_memory
读取当前交易日前、与产品、方向和生命周期匹配的PM学习成果
学习成果只影响新的交易日，不回写历史交易事实
```

## 二、信息传递表

本表只登记各智能体实际接收和传出的信息，不重新规定智能体输入输出契约，也不改变 workflow 编排层的传递和物理化职责。

| 智能体 | 传入信息 | 传出信息 |
|---|---|---|
| 分析师（`technical`、`fundamental`、`commodity_news`） | 交易日期、主配置；PandaAI行情、Finoview基本面文件、新闻txt中的本专业数据；当前交易日前的本专业研究学习SQL成果 | 三份 `AnalystSignal`；每份正式证据位于 `metadata.action_evidence_contract`。基本面/新闻无当日新增只影响本分析师；必需市场事实不可用时三个入口各自产生合法中性 AEC且不调用LLM |
| workflow 编排层 | 三个分析师正式输出 | 共享校验后保存三份 `AnalystSignal`，取得三个真实 `signal_record_id`，再交给 Signal Collector；signal artifact和报告只保存同一份AEC，不传递或保存 prompt、原始 response、自由文本justification、LLM路由、内部参数、校准过程、学习检索上下文和中间工作状态；普通LLM调用失败不得生成默认信号，重试耗尽直接以稳定错误码终止该入口 |
| 信号收集员 | `state["analyst_signals"]` 中已带真实ID的三份 `AnalystSignal`，实际提取并共享校验各自 `metadata.action_evidence_contract` | 唯一 `signal_collection_contract`，写回 `state["signal_collection_contract"]`；不生成信号或ID |
| PM | state["analyst_signals"]，仅用于核对启用分析师的身份、数量及SCC来源引用；state["signal_collection_contract"]，作为PM唯一正式证据；账户、持仓、Router具体合约事实和主配置；Step4从研究学习SQL读取当前交易日前、与产品、方向和生命周期匹配的PM学习成果 | FuturesRecommendation；其 signal_snapshot 内含 signal_collection_contract、final_action_contract、pm_six_step_trace |
| 审计员 | 完整FAC；账户权益、保证金、保证金比例、`risk_status`；当前持仓；SCC数据质量摘要；具体合约及失效边界事实；主配置硬风控参数 | `audit_verdict`、完整 `audit_payload`；不修改 `final_action_contract` |
| 交易员 | 审计后的 `FuturesRecommendation`、当前账户、持仓和主配置；从 `signal_snapshot["final_action_contract"]` 读取执行合约；通过行情路由读取盘中行情 | `intraday decision`、`execution_result`、`futures_transaction`；更新后的 recommendation状态和执行价格 |
| 会计师 | 交易日期、主配置；最近已结算 `portfolio`；当日未入账 `futures_transaction`；结算行情和合约信息 | 结算后的 `portfolio.positions`、`daily_settlement.positions_snapshot`、`ticker_daily_pnl`；已回填结算价并标记入账的 `futures_transaction`；不存在独立 position SQL 事实路径 |
| 复盘员 | Phase1–3阶段状态、分析师signal、审计及执行后的 `futures_recommendation`、`futures_transaction`、`daily_settlement`、最新 `portfolio.positions` 和对应 `daily_settlement.positions_snapshot` | Phase4复盘结果、复盘摘要、每日交易报告和事实归因；不产生第二次合约审计结论 |
| 研究员 | 已完成的Phase4和结算事实；通过正式ID链追溯的AEC、SCC、FAC、Auditor、`execution_result`、transaction、settlement；PM 生命周期校准只读 `final_action_contract.learning_used.pm_lifecycle_learning_impact_delta` | 经验证的结构化交易episode、未交易机会、action-value、profile、policy/state、template prior、历史学习快照和 `researcher_learning_completed` 事件；同次运行的 SQL 与 artifact 原子提交；成果可为空，供下一交易日分析师及PM通过正式检索接口读取 |

## 三、信息传递核心载体

本章固定三个核心载体的生产、承载、传递、修改、完整字段目录和权限边界，并统一服从 `matrix_field_semantics.md`。

### 1. `action_evidence_contract`

#### 1.1 固定边界

- `action_evidence_contract` 由 `technical`、`fundamental`、`commodity_news` 分别生成，是分析师本专业的正式结构化证据。
- `action_evidence_contract` 位于 `AnalystSignal.metadata["action_evidence_contract"]`，不是与 `AnalystSignal` 并列传递的独立对象。
- workflow 编排层以 `AnalystSignal` 作为类型载体，但分析师最终出口的 metadata 只能含 `action_evidence_contract`，Workflow保存后只追加真实 `signal_record_id`；Signal Collector发现其他metadata立即拒绝。signal SQL的自由文本理由列固定为空，artifact和分析师报告都只保存同一份经过共享校验的AEC。
- 必需市场事实不可用时，三个分析师仍分别经自己的正式入口生成同一共享校验可接受的中性 AEC；不得调用LLM补数据，也不得伪造方向、profile、trigger、权限或市场事实。
- 基本面或新闻没有当日新增记录不是全局市场数据不可用；对应分析师使用截止点前最近有效事实并写明时效/质量，或输出本专业合法 `no_opportunity` AEC。
- 技术面必须把已计算并声明使用的波动率、成交强度、价格位置和指标投票实际传入分析师；Finoview只有实际可见且传入分析师的因子才能登记为已使用；新闻先按产品产业链相关性过滤再截取最新记录，非空新闻不自动构成相关证据。
- 数据可用但模型输出普通 `Neutral` 时保持 `signal=Neutral`。三个 LLM 入口使用角色化结构化输出模型：technical 只有在反事实方向、固定 `entry_timing_signal`、canonical 失效边界完整且当前未触发时才可形成 watch；fundamental 固定为 `direction_context` 且新增风险状态为 `no_opportunity`；commodity_news 只有当前事件已满足即时边界时才可形成 `event_immediate` probe/tradeable，不能形成普通15分钟 watch。Neutral 不得升级为 probe/tradeable。
- 可执行 `entry_trigger` 由共享 canonical 定义按 `entry_timing_signal+side` 生成：technical 只允许 `breakout/pullback/vwap_confirmed`，commodity_news 只允许 `event_immediate`，fundamental 固定为空。LLM 自由分析继续进入现有证据、冲突、确认需求和质量字段，不能成为正式执行触发。
- 正式 watch 的 `entry_trigger` 不得为空、`unknown` 或 `wait_for_trigger`；`invalidation_present` 只能由 canonical `invalidation_condition`、合法 `invalidation_level` 或正数 `atr_stop_distance` 证明。`would_change_view_if`、`neutral_trigger_condition`、`entry_trigger` 和通用 `exit_hint` 都不是失效边界别名。
- Signal Collector必须保真消费该契约，不得改写分析师原始证据。Reviewer和Researcher通过已保存的 `FuturesRecommendation.signal_snapshot["signal_collection_contract"]` 追溯分析师证据及其来源。
- Reviewer和Researcher只读取正式 AEC 的 `opportunity_state/trigger_valid/invalidation_present/entry_trigger` 与 canonical 失效字段；不得从旧 metadata 路径补出默认 watch 或中性 `action_preference`。合法 `no_opportunity` 仍可在 Phase4 与结算完成后形成反事实观察或学习记录。
- 无方向、有方向但无具体触发、缺 canonical 失效边界或仅有研究价值均正常形成 `no_opportunity` 并继续；只有已声明且字段完整的 technical/news 候选漏填或错填 profile 才触发现有有限 parse-error 重试。连续三次同一错误后，LLM 层只允许透传预登记安全码 `analyst_execution_profile_missing`，其他解析错误仍使用原有通用错误码。
- 任一分析师或 PM 最终契约失败时，Workflow 只传播稳定契约错误码并终止整个 Phase1 写入；`analyst_execution_profile_missing` 已登记为安全 Phase1 错误码。三份 AnalystSignal、唯一 SCC、全部 FAC/recommendation 及本轮新 artifact 使用同一写事务提交或共同回滚，不得留下部分品种事实。日志和异常不得携带 prompt、原始 response、内部推理或原始异常内容。
- `action_evidence_contract` 只承载分析师预测证据，不具有交易决策权限，禁止包含最终交易动作、手数、rank、资金部署和 `final_action_contract`。

#### 1.2 内容

##### 1.2.1 分析师生成：action_evidence_contract顶层字段

三个分析师都必须生成同一结构的正式证据契约；专业差异只能落入本专业字段内容和学习范围，禁止改变契约字段名、层级和权限边界。

```text
AnalystSignal.metadata.action_evidence_contract
→ contract_version
→ analyst
→ sector
→ side
→ signal
→ confidence
→ data_cutoff
→ no_lookahead_status
→ horizon_class
→ analyst_horizon
→ expected_horizon_days
→ market_regime
→ trend_stage
→ setup_type
→ setup_quality_ok
→ setup_quality_score
→ price_percentile
→ invalidation_level
→ invalidation_condition
→ invalidation_present
→ atr_stop_distance
→ add_allowed
→ direction_anchor
→ supply_demand_state
→ basis_state
→ inventory_state
→ warehouse_receipt_state
→ position_flow_state
→ data_freshness
→ event_type
→ impact_window_days
→ requires_fundamental_confirmation
→ evidence_quality
→ evidence_strength
→ evidence_freshness
→ evidence_decay_risk
→ confirmation_requirements
→ technical_false_breakout_risk
→ fundamental_opposition_strength
→ news_impact_window
→ one_off_event_risk
→ business_quality_score
→ primary_business_driver
→ secondary_confirmation
→ counter_evidence
→ reward_risk_ratio
→ factor_alignment_score
→ data_coverage_score
→ tradeability_reason
→ opportunity_type
→ opportunity_state
→ learning_impact_summary
→ factor_calibration_summary
→ event_calibration_summary
→ entry_quality
→ setup_quality_notes
→ entry_trigger
→ exit_hint
→ holding_period_hint
→ evidence_role
→ direction_context
→ trend_direction
→ entry_timing_signal
→ price_location
→ trigger_valid
→ current_trigger_confirmed
→ factor_focus
→ current_evidence_conflict
→ neutral_reason
→ missing_evidence
→ conflicting_factors
→ would_change_view_if
→ opportunity_cost_risk
→ recommended_observation_window
→ neutral_opportunity_bucket
→ neutral_trigger_condition
→ counterfactual_side
→ neutral_watchlist_priority
→ accountability_tag
→ do_not_trade_reason
→ learning_scope
→ product_profile_evidence
→ fusion_evidence
→ data_usage_summary
```

`learning_scope` 按生产分析师使用以下真实结构：

```text
AnalystSignal.metadata.action_evidence_contract.learning_scope（technical）
→ setup_family
→ sector_setup_alignment
→ sector_preferred_setups
→ sector_caution_setups
→ primary_confirmation
→ execution_focus
→ market_regime
→ product_profile_id
→ product_profile_version
→ product_profile_used
→ product_profile_fields_used
→ product_profile_learning_interaction
→ product_profile_analysis_boundary

AnalystSignal.metadata.action_evidence_contract.learning_scope（fundamental）
→ factor_tree
→ primary_driver_groups
→ short_trigger_groups
→ conflict_groups
→ product_profile_id
→ product_profile_version
→ product_profile_used
→ product_profile_fields_used
→ product_profile_learning_interaction
→ product_profile_analysis_boundary

AnalystSignal.metadata.action_evidence_contract.learning_scope（commodity_news）
→ event_regime
→ event_type_counts
→ catalyst_classification
→ product_profile_id
→ product_profile_version
→ product_profile_used
→ product_profile_fields_used
→ product_profile_learning_interaction
→ product_profile_analysis_boundary
```

##### 1.2.2 分析师生成：学习校准字段

```text
AnalystSignal.metadata.action_evidence_contract.learning_impact_summary
→ contract_version
→ analyst
→ historical_support
→ historical_contradiction
→ product_learning_scopes
→ current_evidence_confirmed
→ current_evidence_missing
→ opportunity_state
→ opportunity_state_reason
→ positive_strength
→ negative_strength
→ broad_positive_strength
→ broad_negative_strength
→ net_evidence_adjustment
→ authority_boundary

AnalystSignal.metadata.action_evidence_contract.factor_calibration_summary
→ contract_version
→ effective_factors
→ stale_or_conflicting_factors
→ factors_requiring_price_confirmation
→ supporting_learning_scopes
→ contradicting_learning_scopes
→ factor_calibration_reason
→ authority_boundary

AnalystSignal.metadata.action_evidence_contract.event_calibration_summary
→ contract_version
→ effective_catalysts
→ background_noise
→ impact_window_assessment
→ price_volume_confirmation_required
→ supporting_learning_scopes
→ contradicting_learning_scopes
→ event_calibration_reason
→ authority_boundary
```

##### 1.2.3 分析师生成：产品差异化与证据融合字段

```text
AnalystSignal.metadata.action_evidence_contract.product_profile_evidence
→ contract_version
→ product_profile_id
→ product_profile_version
→ ticker
→ sector
→ profile_analysis_boundary
→ analyst
→ product_profile_used
→ profile_fields_used
→ profile_supported_evidence
→ profile_conflicting_evidence
→ profile_missing_evidence
→ profile_assumption_status
→ profile_relevance_score
→ profile_learning_interaction
→ profile_invalid_use_flags
→ confirmation_requirements

AnalystSignal.metadata.action_evidence_contract.fusion_evidence
→ contract_version
→ ticker
→ analyst
→ evidence_strength
→ evidence_strength_score
→ evidence_freshness
→ evidence_freshness_score
→ evidence_decay_risk
→ confirmation_requirements
→ missing_evidence
→ current_evidence_conflict
→ technical_false_breakout_risk
→ fundamental_opposition_strength
→ news_impact_window
→ one_off_event_risk
→ fusion_boundary
```

##### 1.2.4 分析师生成：数据使用追溯字段

```text
AnalystSignal.metadata.action_evidence_contract.data_usage_summary
→ ticker
→ trading_date
→ analyst
→ data_available（仅必需市场事实不可用时出现，类型为bool）
→ sources

AnalystSignal.metadata.action_evidence_contract.data_usage_summary.sources.pandaai_market（technical）
→ source
→ dataset
→ available
→ used_in_signal
→ pre_open_only
→ info_cutoff
→ latest_data_date
→ row_count
→ fields_used
→ indicators_used

AnalystSignal.metadata.action_evidence_contract.data_usage_summary.sources.finoview_fundamental（fundamental）
→ source
→ dataset
→ available
→ used_in_signal
→ pre_open_only
→ info_cutoff
→ configured_indicator_count
→ loaded_indicator_count
→ missing_like_count
→ stale_indicator_count
→ near_stale_indicator_count
→ coverage_ratio
→ stale_ratio
→ factor_groups
→ freshness_score
→ no_lookahead_status
→ local_availability_audit
→ coverage_status
→ supports_trade_setup
→ runtime_data_boundary

AnalystSignal.metadata.action_evidence_contract.data_usage_summary.sources.pandaai_extra（fundamental）
→ source
→ dataset
→ available
→ used_in_signal
→ pre_open_only
→ info_cutoff
→ reference_date
→ lookback_days
→ feature_count
→ record_counts
→ feature_status
→ data_missing
→ error_count

AnalystSignal.metadata.action_evidence_contract.data_usage_summary.sources.finoview_news_txt（commodity_news）
→ source
→ dataset
→ available
→ used_in_signal
→ pre_open_only
→ info_cutoff
→ news_cutoff
→ raw_block_count
→ parsed_news_count
→ selected_news_count
→ latest_news_date
→ freshness_score
→ relevance_score

AnalystSignal.metadata.action_evidence_contract.data_usage_summary.sources.pandaai_pre_open_reference（三个分析师的正式全局数据不可用状态）
→ source
→ dataset
→ available=false
→ used_in_signal=false
→ pre_open_only=true
→ info_cutoff
→ missing_data
→ data_quality_flags
→ reason=pre_open_reference_price_unavailable
```

每个来源记录必须保留来源名、数据集、可用状态、是否用于信号、盘前边界、信息截止时间及本来源实际生成的数据覆盖、时效、缺失和质量字段。共享校验器按上述四种正常来源及 `pandaai_pre_open_reference` 的固定身份、字段集合和基础类型拒绝未登记字段。`local_availability_audit` 只允许覆盖数量、分组、比例、状态和稳定错误计数，不得包含本机目录、源文件路径、编码、原始解析错误、内部说明、请求参数或工具原始结果。分析师不得遗漏 `learning_scope`、`product_profile_evidence`、`fusion_evidence` 或 `data_usage_summary`，也不得把LLM自由文本当作正式字段补偿路径。

### 2. `signal_collection_contract`

#### 2.1 固定边界

- `signal_collection_contract` 由 Signal Collector根据三份正式 `action_evidence_contract` 唯一生成，是分析师证据进入PM的唯一统一证据载体。
- `signal_collection_contract` 生成后写入 `state["signal_collection_contract"]`，在进入PM前不独立落库、不独立生成artifact。
- PM只能通过 `signal_collection_contract` 理解方向、证据强弱、冲突、触发和失效边界；原始 `analyst_signals` 只用于来源核对和研究追溯，不得形成第二套交易语义。
- PM不得重建、补造或改写 `signal_collection_contract`；Step6必须将原始SCC保真写入 `FuturesRecommendation.signal_snapshot["signal_collection_contract"]`。
- `signal_collection_contract` 只承载统一证据，不具有交易决策权限，禁止包含最终交易动作、手数、rank、资金部署和 `final_action_contract`。

#### 2.2 内容

##### 2.2.1 Signal Collector生成：signal_collection_contract顶层字段

```text
state["signal_collection_contract"]
→ contract_version
→ source_agent
→ ticker
→ trading_date
→ source_contracts
→ evidence_items
→ dominant_side
→ side_consensus
→ trigger_status
→ supporting_analysts
→ opposing_analysts
→ neutral_analysts
→ evidence_strength
→ evidence_conflict_level
→ confirmation_requirements
→ missing_evidence
→ data_quality_flags
→ setup_types
→ horizon_scope
→ invalidation_summary
→ evidence_fusion
→ collector_decision_boundary
```

##### 2.2.2 Signal Collector保真收录：source_contracts完整结构

```text
state["signal_collection_contract"].source_contracts[]
→ analyst
→ signal_record_id
→ action_evidence_contract
```

每个启用分析师对应一条来源记录。`action_evidence_contract` 必须是该分析师第1.2节正式契约的完整保真副本。`product_profile_evidence` 和分析师单体 `fusion_evidence` 只保存在该AEC内，禁止在 `source_contracts[]` 同级复制第二份。

`signal_record_id` 的唯一合法生产者是workflow 编排层的分析师signal保存入口。正常路径和数据不可用路径都必须先形成signal SQL来源记录，Signal Collector才允许将其ID写入SCC；Signal Collector不得自创ID或以空ID冒充已完成物理追溯。

##### 2.2.3 Signal Collector生成：evidence_items完整结构

```text
state["signal_collection_contract"].evidence_items[]
→ analyst
→ side
→ confidence
→ signal
→ opportunity_state
→ trigger_valid
→ current_trigger_confirmed
→ trigger_status
→ entry_trigger
→ setup_type
→ setup_quality_ok
→ horizon_class
→ market_regime
→ evidence_quality
→ current_evidence_conflict
→ missing_evidence
→ evidence_strength
→ evidence_freshness
→ confirmation_requirements
→ product_profile_id
→ product_profile_used
→ product_profile_analysis_boundary
```

##### 2.2.4 Signal Collector生成：失效与融合结构

```text
state["signal_collection_contract"].invalidation_summary[]
→ analyst
→ condition
→ level

state["signal_collection_contract"].evidence_fusion
→ contract_version
→ evidence_strength_by_analyst
→ evidence_freshness_by_analyst
→ evidence_alignment_state
→ cross_analyst_conflicts
→ dominant_opposing_evidence
→ confirmation_requirements
→ missing_evidence
→ multi_evidence_consensus_score
→ fusion_boundary

state["signal_collection_contract"].evidence_fusion.evidence_strength_by_analyst
→ technical
→ fundamental
→ commodity_news

state["signal_collection_contract"].evidence_fusion.evidence_freshness_by_analyst
→ technical
→ fundamental
→ commodity_news

state["signal_collection_contract"].evidence_fusion.cross_analyst_conflicts[]
→ analyst
→ side
→ conflicts

state["signal_collection_contract"].evidence_fusion.dominant_opposing_evidence[]
→ analyst
→ side
→ strength
→ freshness
→ conflicts
```

Signal Collector必须输出唯一SCC并完整保留来源、逐条证据、方向汇总、主方向触发、冲突、缺失、确认要求、失效边界和融合事实。`trigger_status` 只使用主方向对应分析师的合法机会状态与触发证据；主方向为flat/mixed或主方向证据全部为 `no_opportunity` 时固定为 `not_applicable`，反方向 watch 不得借给主方向。`missing_evidence/confirmation_requirements` 只作为证据诊断，不得映射成 `data_missing`；`data_quality_flags` 必须由Signal Collector从AEC内真实 `data_usage_summary.sources.*` 汇总，并由共享摘要仅在 `status=hard_fail` 时形成候选硬阻断。禁止遗漏启用分析师、重复来源、用反方向触发确认主方向，或加入交易动作、rank、预算、手数和PM内部状态。

### 3. `FuturesRecommendation`

#### 3.1 固定边界

- `FuturesRecommendation` 只由PM Step6基于最终 `pm_state` 原子生成，是PM向下游返回的唯一正式对象。
- PM生成时，其 `signal_snapshot` 内含原始 `signal_collection_contract`、唯一 `final_action_contract` 和 `pm_six_step_trace`；PM Step1–5不得提前生成该对象或其草稿。
- workflow 编排层负责保存同一份 `FuturesRecommendation`。Auditor只增加审计事实，Trader只增加执行事实和更新执行状态，二者均不得修改 `final_action_contract` 和原始SCC。
- Accountant不接收 `FuturesRecommendation`；Reviewer和Researcher只读取已保存的 `FuturesRecommendation` 及其他物理事实，不向其中追加复盘和研究结果。
- `FuturesRecommendation.signal_snapshot["final_action_contract"]` 是唯一合法交易合约。禁止任何智能体或workflow 编排层生成第二张合约、改写PM交易语义或用旁路字段替代该合约。

#### 3.2 内容

##### 3.2.1 顶层字段

```text
FuturesRecommendation
→ id
→ config_id
→ reference_portfolio_id
→ trading_date
→ effective_trade_date
→ source_type
→ underlying_code
→ from_contract
→ to_contract
→ contract_code
→ action
→ lots
→ base_price
→ base_price_source
→ base_price_date
→ open_price
→ prev_close_price
→ slippage_model
→ slippage_ticks
→ slippage_amount
→ execution_price
→ justification
→ signal_snapshot
   → signal_collection_contract（策略路径）
   → final_action_contract（策略路径）
   → pm_six_step_trace（策略路径）
   → auditor（策略审计后）
   → phase2_execution（策略执行后）
   → execution_translation（执行后）
   → execution_result（执行后）
   → rollover_policy（换约路径）
   → source_type（强制风控路径）
   → operation_reason（强制风控路径）
   → risk_status（强制风控路径）
   → margin_ratio（强制风控路径）
   → current_margin_ratio（强制风控路径）
   → trigger_margin_ratio（强制风控路径）
   → post_reduce_target_margin_ratio（强制风控路径）
   → account_equity（强制风控路径）
   → total_margin（强制风控路径）
   → total_unrealized_pnl（强制风控路径）
   → underlying_code（强制风控路径）
   → contract_code（强制风控路径）
   → risk_price（强制风控路径）
   → risk_price_source（强制风控路径）
   → risk_price_datetime（强制风控路径）
   → forced_risk_boundary（强制风控路径）
→ audit_payload
→ warning_message
→ status
→ created_at
```

本目录必须保留完整 `FuturesRecommendation` 顶层字段、`signal_snapshot` 全部业务路径和 `audit_payload`。后续梳理任何智能体时，必须先在本目录定位其读取、生成或追加的字段，禁止遗漏既有路径或另建旁路载体。

##### 3.2.2 PM Step6组装：signal_snapshot初始结构

PM Step6必须一次性组装以下初始结构：

```text
FuturesRecommendation.signal_snapshot
→ FuturesRecommendation.signal_snapshot.signal_collection_contract
→ FuturesRecommendation.signal_snapshot.final_action_contract
→ FuturesRecommendation.signal_snapshot.pm_six_step_trace
```

`signal_collection_contract` 必须是 Signal Collector原始SCC的保真写入；`final_action_contract` 和 `pm_six_step_trace` 由PM Step6生成。三者共同构成PM返回时不可遗漏的初始 `signal_snapshot`。PM不得在Step1–5生成其中任何物理草稿，也不得提前写入Auditor、Trader、换约或强制风控字段。

##### 3.2.3 PM Step6生成：final_action_contract完整结构

PM Step6必须生成唯一 `FuturesRecommendation.signal_snapshot.final_action_contract`。该合约必须完整承载最终动作、当前手数、目标手数、手数变化、交易权限、证据、学习、原因与风险边界、执行要求、资金部署、position sizing、SCC引用和最终一致性事实。

本节对应下方字段目录中的 `final_action_contract（策略路径）`。完整AEC的唯一追溯落点是 `signal_snapshot.signal_collection_contract.source_contracts[].action_evidence_contract`；禁止在 `final_action_contract` 复制第二份AEC。候选动作、推荐意图和PM Step1–5中间状态不是必传事实，不得进入唯一合约。rank及资金部署详细的唯一落点是 `capital_deployment`；`evidence_used` 只保留非重复的PM证据使用摘要与 `position_sizing_result`。

以下七项是已审计确认的上游必传最终事实，字段名复用现有统一语义，不新建别名：

| 唯一落点 | 合法生产者 | 固定选择规则 | 合法消费者 |
|---|---|---|---|
| `final_action_contract.contract_code` | Router/当前持仓提供事实；PM Step6只绑定 | 已有持仓优先绑定持仓合约；新增风险只绑定Router在截止点内可见的具体合约，缺失时不得新增风险，禁止默认或猜测 | Auditor、Trader、Reviewer、Researcher |
| `final_action_contract.setup_type` | 分析师生产原始事实；PM Step6生产最终事实 | PM只从SCC中选择与最终方向、动作及学习作用域一致的setup，不得取第一个分析师或反方向值 | Trader、Reviewer、Researcher |
| `final_action_contract.horizon_class` | 分析师生产原始事实；PM Step6生产最终事实 | PM只从SCC中选择与最终方向、动作及Step4学习作用域一致的期限类别 | Trader、Reviewer、Researcher |
| `final_action_contract.expected_horizon_days` | 分析师生产原始事实；PM Step6生产最终事实 | 只从与最终方向和 `horizon_class` 一致的真实AEC期限中选择，缺失时保持缺失 | Trader、Reviewer、Researcher |
| `final_action_contract.market_regime` | 分析师生产原始事实；PM Step6生产最终事实 | PM只从SCC中选择与最终方向和Step4学习检索作用域一致的市场状态 | Trader、Reviewer、Researcher、下一交易日PM学习 |
| `final_action_contract.invalidation_level` | 分析师生产原始事实；PM Step6生产最终事实 | 只在AEC存在真实数值且来源方向与最终方向一致时写入；禁止默认值和反方向填充 | Auditor、Trader、Reviewer、Researcher |
| `final_action_contract.atr_stop_distance` | technical生产原始事实；PM Step6生产最终事实 | 只在technical AEC真实生产且与最终方向及setup一致时写入；禁止默认值 | Trader、Reviewer、Researcher |

`target_return` 当前没有合法上游生产者，不是AEC、SCC或PM合约的必传字段。`target_price` 只能在存在合法输入时由Trader作为运行时派生事实，不得倒灌进上游载体。

##### 3.2.4 PM Step6生成：pm_six_step_trace完整结构

PM Step6必须生成：

```text
FuturesRecommendation.signal_snapshot.pm_six_step_trace
→ FuturesRecommendation.signal_snapshot.pm_six_step_trace.step6_contract_generation_check
→ FuturesRecommendation.signal_snapshot.pm_six_step_trace.pm_contract_self_check
```

两项检查必须保留各自的工具名、检查结果、错误、最终动作与手数一致性以及禁止物理写入事实。该trace只记录Step6生成检查和最终合约自身检查，不得加入Step1–5中间状态或跨步骤回溯比较。

##### 3.2.5 Auditor追加：审计摘要与audit_payload

Workflow只向Auditor传入完整FAC、账户权益/保证金/保证金比例/`risk_status`、当前持仓、共享SCC数据质量摘要、具体合约及失效边界事实和主配置 `max_total_margin_ratio`。完整策略配置、PM学习、融合、rank、预算和sizing过程不属于Auditor输入。

Auditor只允许追加：

```text
FuturesRecommendation.signal_snapshot.auditor
FuturesRecommendation.audit_payload
```

`signal_snapshot.auditor` 是安全摘要；`audit_payload` 必须保留原始结论、来源、边界、hard/soft reasons、contract summary和semantic state。Auditor只核对FAC结构、动作/方向/手数变化基本合法性、硬保证金上限、清算状态、具体合约、失效边界和硬数据质量错误；新增风险的硬保证金检查固定使用“账户当前组合保证金比例 + max(0, FAC目标品种保证金比例 - 当前品种保证金比例)”形成投影组合比例，不得把单品种目标比例误当成组合比例。Auditor不得修改PM已生成的SCC、`final_action_contract`、`pm_six_step_trace`、方向、rank、预算、sizing和策略参数，也不得生成第二张合约。

##### 3.2.6 Trader追加：phase2_execution完整结构

Trader执行链必须把Phase2运行状态写入：

```text
FuturesRecommendation.signal_snapshot.phase2_execution
```

该结构必须保留执行模式、状态、产品、recommendation引用、参考动作与手数、检查时间、截止时间、循环状态、执行合约、翻译后决策、盘中选择、执行学习、PM计划校验、合约执行观察、入场权限门、退出策略、入场时机、执行模拟和防御性的两步反转翻译事实。当前生产策略 PM 已将异号目标原子收敛为 `exit/target_lots=0`；该翻译事实不得被当成同一 recommendation 的反向开仓权限，Trader也不得用它改写 `final_action_contract`。

##### 3.2.7 Trader追加：execution_translation完整结构

Trader执行链必须把合约到订单的翻译事实写入：

```text
FuturesRecommendation.signal_snapshot.execution_translation
```

该结构必须保留翻译订单、改写原因、参考动作与手数、执行价格基础、PM最终生命周期摘要、执行合约、盘中执行、Phase2订单计划、最终合约来源、Auditor结论、执行阻断、最终执行依据和市场规则阻断。该结构只解释执行翻译，不得从原始AEC再做方向过滤，不得成为第二套交易权限或第二张合约。

Trader必须从已审计 `final_action_contract` 读取最终setup、期限、市场状态、触发、数值失效和ATR止损事实；SCC只供追溯，不得成为Trader重新选择方向或执行边界的第二证据入口。禁止继续依赖旧的 `signal_snapshot.technical/fundamental/commodity_news` 路径补造 `signal_lifecycle`；无法从正式合约取得的生命周期字段不得以空对象冒充完整执行事实。`target_price` 仅在Trader存在合法运行时输入时才允许派生；当前无合法 `target_return` 生产者时不得伪造。

已审计 FAC 若为条件 watch，必须保持 `requires_intraday_confirmation=true`，Trader 用15分钟线判断触发并用下一根合法1分钟线执行。FAC 若以 canonical 当前触发事实写入 `can_execute_without_intraday_trigger=true`，Trader 对 breakout、pullback、vwap_confirmed、event_immediate 等合法 profile 不再复判15分钟触发，只使用当时可见的合法1分钟线；此时执行审计的 `trigger_checked=false`。Trader 对 open/add/scale 的硬保证金安全检查与 PM、Auditor 共用 `current_account_margin-current_ticker_margin+target_ticker_margin` 投影，不能重复计算已有持仓，reduce/exit 不受新增风险保证金检查阻断。

##### 3.2.8 Trader追加：execution_result完整结构

Trader执行链必须把最终执行结果写入：

```text
FuturesRecommendation.signal_snapshot.execution_result
```

该结构必须保留结果、状态、成交数量、实际成交、实际动作、实际手数、不交易原因及分类、执行学习轨迹、警告和一致性诊断。成交与未成交都必须形成明确结果，禁止只更新顶层状态而遗漏执行事实。

Trader写入执行审计时必须以Auditor已生成的完整 `audit_payload` 为基底，仅追加 `trade_contract_audit`、`execution_translation`、`execution_result` 和 `phase2_execution`。禁止用仅含 `independent_auditor` 摘要的执行payload覆盖、替换或截断原始Auditor payload。

##### 3.2.9 换约链生成与Trader执行：rollover_policy完整结构

换约路径必须保留：

```text
FuturesRecommendation.signal_snapshot.rollover_policy
→ mode
→ reason
→ execution_type
→ strategy_target_lots
→ close_lots
→ open_lots
→ from_contract
→ to_contract
```

换约策略目标、旧约平仓和新约开仓必须在同一换约事实中可追溯。Trader只执行既定换约路径，不得借换约重写PM策略方向和目标仓位。

##### 3.2.10 强制风控链生成：强制风控Recommendation完整结构

强制风控路径必须在 `FuturesRecommendation.signal_snapshot` 保留：

```text
source_type
operation_reason
risk_status
margin_ratio
current_margin_ratio
trigger_margin_ratio
post_reduce_target_margin_ratio
account_equity
total_margin
total_unrealized_pnl
underlying_code
contract_code
risk_price
risk_price_source
risk_price_datetime
forced_risk_boundary
```

同时必须在 `FuturesRecommendation.audit_payload` 保留强制风控来源、触发原因、风险状态、保证金边界、账户事实、强制风控范围和策略学习隔离边界。强制风控Recommendation不得伪装成PM策略合约，也不得污染alpha学习。

##### 3.2.11 Trader执行后更新：FuturesRecommendation顶层执行字段

Trader执行后只按真实执行结果更新同一份Recommendation的以下顶层字段：

```text
FuturesRecommendation.execution_price
FuturesRecommendation.warning_message
FuturesRecommendation.status
```

未触发、被阻断、跳过和已成交必须使用对应真实状态；只有真实成交才能写入成交执行价。顶层更新必须与 `signal_snapshot.execution_result` 和实际transaction一致，不得改写PM原始动作、目标手数和唯一合约。

##### 3.2.12 Reviewer与Researcher只读：完整FuturesRecommendation读取边界

Reviewer和Researcher读取的是保存后的完整 `FuturesRecommendation`，包括PM初始SCC与唯一合约、Auditor审计事实、Trader执行事实、换约或强制风控事实以及顶层最终状态。二者追溯分析师AEC的唯一路径是 `FuturesRecommendation.signal_snapshot.signal_collection_contract.source_contracts[].action_evidence_contract`；禁止读取旧 `signal_snapshot.technical/fundamental/commodity_news` 或 `final_action_contract` 内的AEC副本。

Reviewer和Researcher不得向 `FuturesRecommendation` 追加复盘、归因、研究或学习字段，不得修改历史SCC、`final_action_contract`、审计结论和执行结果。Reviewer负责复盘和事实归因；Researcher基于完整物理事实链生成未来学习记录。

##### 3.2.13 按生产者划分：对象字段完整目录

本节是3.2.2至3.2.10各生产者职责的字段目录，不是独立载体，也不允许形成第二套同名结构。

```text
final_action_contract（策略路径）
→ contract_version
→ ticker
→ contract_code
→ final_action
→ current_lots
→ target_lots
→ lots_delta
→ lots_delta_abs
→ target_position_ratio
→ target_margin_ratio_estimate
→ setup_type
→ horizon_class
→ expected_horizon_days
→ market_regime
→ authority_type
→ authority_decision
→ requires_authority
→ open_action_evidence
→ strong_current_evidence
→ watch_for_trigger_block
→ conditional_trigger_authority
→ negative_profile
→ tradeable_state
→ weak_conflict_probe
→ max_allowed_margin_ratio
→ reason_codes
→ evidence_used
→ learning_used
→ execution_profile
→ trigger_source
→ entry_trigger
→ invalidation
→ invalidation_level
→ atr_stop_distance
→ valid_until
→ requires_intraday_confirmation
→ can_execute_without_intraday_trigger
→ execution_action_value_preference
→ capital_deployment
→ execution_requirement
→ consistency
→ single_source_of_trade_truth
→ candidate_sources_do_not_bypass_contract
→ signal_collection_contract_ref

pm_six_step_trace（策略路径）
→ step6_contract_generation_check
→ pm_contract_self_check

auditor（策略审计后）
→ producer
→ audit_status
→ audit_verdict
→ audit_reason_codes
→ audited_at
→ independent_auditor_agent
→ pm_risk_gate_is_not_auditor

phase2_execution（策略执行后）
→ mode
→ status
→ ticker
→ recommendation_id
→ reference_action
→ reference_lots
→ last_checked_at
→ cutoff_datetime
→ finalize_untriggered
→ loop_iteration
→ reason
→ execution_contract
→ current_lots_before
→ translated_decision
→ intraday_selection
→ setup_execution_learning
→ pm_plan_validation
→ contract_execution_observation
→ entry_authority_gate
→ exit_policy
→ entry_timing
→ execution_simulation
→ two_step_reversal

execution_translation（执行后）
→ translated_orders
→ rewrite_reasons
→ reference_action
→ reference_lots
→ base_price
→ base_price_source
→ base_price_date
→ open_price
→ prev_close_price
→ warning_message
→ signal_lifecycle
→ execution_contract
→ intraday_execution
→ phase2_order_plan
→ final_action_contract_source
→ auditor_verdict
→ execution_block
→ final_execution_basis
→ market_rule_block

execution_result（执行后）
→ outcome
→ status
→ transaction_count
→ actual_transactions
→ actual_action
→ actual_lots
→ no_trade_reason
→ no_trade_reason_category
→ execution_learning_trace
→ warning_message
→ consistency_diagnostics

rollover_policy（换约路径）
→ mode
→ reason
→ execution_type
→ strategy_target_lots
→ close_lots
→ open_lots
→ from_contract
→ to_contract

audit_payload（策略审计后）
→ contract_version
→ producer
→ agent_name
→ recommendation_id
→ ticker
→ trading_date
→ config_id
→ audit_status
→ audit_verdict
→ audit_reason_codes
→ hard_risk_reasons
→ soft_risk_reasons
→ audited_by
→ audited_at
→ source
→ boundary
→ contract_summary
→ semantic_state

audit_payload（执行后）
→ 保留上述全部Auditor字段
→ trade_contract_audit
→ independent_auditor
→ execution_translation
→ execution_result
→ phase2_execution

audit_payload（强制风控生成时）
→ source_type
→ operation_reason
→ risk_status
→ margin_ratio
→ trigger_margin_ratio
→ post_reduce_target_margin_ratio
→ account_equity
→ total_margin
→ total_unrealized_pnl
→ forced_risk_scope
→ strategy_learning_boundary
```

##### 3.2.14 按生产者划分：嵌套字段完整目录

以下嵌套字段继续归属于3.2.2至3.2.10中标明的生产者。对象列表统一使用 `[]` 表示单条记录结构；不得把列表记录字段误写为父对象顶层字段。

```text
【PM Step6：final_action_contract嵌套结构】
evidence_used（final_action_contract）
→ scorecard_preferred_side
→ scorecard_state
→ scorecard_score
→ opportunity_score
→ lifecycle_learning_trace
→ learning_impact_delta
→ opportunity_score_components
→ analyst_direction_evidence
→ direction_evidence_strength
→ direction_evidence_components
→ direction_evidence_boundary
→ pm_fusion_diagnostics
→ pm_conflict_resolution
→ market_confirmation_score
→ market_confirmation_conflicts
→ position_sizing_result
→ side_priority
→ ticker_side_priority
→ side_priority_score
→ candidate_quality
→ candidate_layer_hint
→ side_priority_semantics_version
→ side_priority_meaning
→ side_priority_is_not_capital_rank
→ side_priority_is_not_trade_authority
→ pm_lifecycle_learning_router

opportunity_score_components（evidence_used）
→ directional_support
→ tradeable_state
→ business_quality
→ setup_quality
→ confidence
→ market_confirmation
→ positive_learning
→ negative_learning
→ execution_profile_learning
→ recent_tail_loss_penalty
→ entry_quality_loss_penalty
→ trigger_quality_positive_bonus
→ trigger_quality_loss_penalty
→ fusion_consensus
→ fusion_score_adjustment

analyst_direction_evidence（evidence_used）
→ side
→ source
→ boundary
→ supporting_signal_count
→ supporting_analysts
→ candidate_quality
→ candidate_layer_hint
→ opportunity_score

direction_evidence_components（evidence_used）
→ opportunity_score
→ candidate_quality
→ supporting_signal_count
→ supporting_analysts
→ setup_quality
→ trigger_valid
→ invalidation_present
→ conflict_count

pm_fusion_diagnostics（evidence_used）
→ contract_version
→ pm_fusion_diagnostics
→ evidence_alignment_state
→ multi_evidence_consensus_score
→ cross_analyst_conflict_count
→ dominant_opposing_evidence_count
→ missing_evidence_count
→ confirmation_requirement_count
→ fusion_score_adjustment
→ requires_pm_conflict_resolution
→ requires_pm_confirmation_explanation
→ no_trade_authority

pm_conflict_resolution（evidence_used）
→ handled
→ resolution_effect
→ confirmation_requirements_addressed
→ no_trade_authority

position_sizing_result（evidence_used）
→ tool
→ ticker
→ current_lots
→ target_lots
→ lots_delta
→ lots_delta_abs
→ target_position_ratio
→ target_value
→ margin_required
→ account_equity
→ target_margin_ratio_estimate
→ margin_rate
→ current_net_exposure
→ projected_net_exposure
→ current_ticker_exposure
→ max_position_ratio
→ max_net_exposure
→ risk_level
→ lots_to_trade_reason
→ control_reasons
→ capital_allocation_reason
→ no_final_action_authority
→ no_direction_override_authority
→ no_llm

lifecycle_learning_trace（evidence_used）
→ trace_version
→ contract_lifecycle_port
→ pm_lifecycle_action_port
→ router_source
→ rank_lifecycle
→ allowed_learning_lanes
→ accepted_learning_lanes
→ blocked_learning_lanes
→ trigger_profile_learning_lanes
→ used_lanes
→ ignored_lanes
→ positive_count
→ negative_count
→ exact_real_count
→ episode_count
→ decision_learning_rows
→ trigger_profile_learning
→ trigger_profile_learning_rows
→ trigger_profile_indices
→ rejected_learning
→ rejected_learning_lanes
→ pm_lifecycle_learning_router
→ execution_profile_learning_direct_to_rank
→ trigger_profile_learning_direct_to_rank
→ execution_profile_signal_direct_to_rank
→ memory_requirement_status
→ memory_requirements
→ hold_learning_decision
→ reduce_exit_learning_decision
→ open_add_learning_decision
→ conditional_monitor_learning_decision
→ execution_profile_learning_decision
→ final_contract_effect_fields
→ strongest_positive
→ strongest_negative
→ pm_final_contract_lifecycle_trace

decision_learning_rows[]（lifecycle_learning_trace）
→ source_index
→ id
→ ticker
→ side
→ canonical_action_family
→ lane
→ action_preference
→ reward_mean
→ sample_count

trigger_profile_learning_rows[]（lifecycle_learning_trace）
→ source_index
→ id
→ ticker
→ side
→ canonical_action_family
→ lane
→ action_preference
→ reward_mean
→ sample_count
→ route
→ not_rank_learning

rejected_learning[]（lifecycle_learning_trace）
→ source_index
→ id
→ ticker
→ side
→ canonical_action_family
→ lane
→ action_preference
→ reward_mean
→ sample_count
→ reason
→ errors

learning_impact_delta（evidence_used）
→ trace_version
→ current_lots
→ target_lots
→ lots_delta
→ pre_learning_position_ratio
→ final_target_position_ratio
→ position_ratio_delta
→ open_add_rank_score_delta
→ alpha_setup_multiplier
→ alpha_setup_expectancy_lane
→ hold_decision
→ hold_changes_position
→ reduce_exit_decision
→ reduce_exit_changes_position
→ conditional_monitor_decision
→ execution_profile_changed
→ positive_learning
→ negative_learning
→ entry_quality_loss_penalty
→ trigger_quality_positive_bonus
→ trigger_quality_loss_penalty
→ net_rank_learning_delta
→ rank_score
→ rank_score_open_add_learning_delta
→ execution_profile_learning_direct_to_rank
→ execution_profile_learning_observed

rank_input_components（evidence_used）
→ final_state
→ capital_priority_tier
→ rank_score
→ rank_score_components
→ capital_priority_score
→ watch_priority_score
→ opportunity_score
→ cold_start_evidence_quality
→ setup_quality_score
→ trigger_quality_score
→ positive_learning
→ negative_learning
→ entry_quality_loss_penalty
→ trigger_quality_positive_bonus
→ trigger_quality_loss_penalty

rank_score_components（rank_input_components）
→ cold_start_evidence_quality
→ capital_layer_priority
→ open_add_action_value_delta
→ product_setup_trigger_history
→ trigger_execution_quality
→ capital_efficiency
→ conflict_risk_invalidation_penalty

learning_used（final_action_contract）
→ alpha_setup_action_values
→ memory_requirements
→ memory_retrieval
→ positive_open_seed
→ alpha_setup_ev_fusion
→ capital_utilization_learning
→ capital_utilization_target
→ memory_state
→ learning_adjustment_summary
→ pm_lifecycle_learning_router
→ trigger_profile_learning
→ pm_lifecycle_learning_trace
→ pm_lifecycle_learning_impact_delta
→ learning_to_position_summary
→ pm_landing_consistency_audit

alpha_setup_action_values[]（learning_used）
→ id
→ action_value_id
→ scope_key
→ ticker
→ side
→ setup_type
→ action_name
→ canonical_action_family
→ learning_lane
→ action_value_lane
→ action_preference
→ memory_side_role
→ reward_mean
→ reward_sum
→ win_rate
→ sample_count
→ last_sample_date
→ retrieval_match_level

memory_requirements（learning_used）
→ contract
→ action_lifecycle
→ action
→ current_position_side
→ target_side
→ contract_side_role
→ required_memory_lanes
→ required_memory_side_roles
→ required_pm_memory
→ must_land_in_pm_contract
→ audit_only_memory

required_pm_memory[]（memory_requirements）
→ lane
→ learning_lane
→ action_value_lane
→ side
→ memory_side_role
→ must_land_in_pm_contract
→ reason

memory_retrieval（learning_used）
→ tool
→ boundary
→ memory_requirements
→ status
→ reason
→ requirement_details
→ alpha_setup_action_value_count_after_lifecycle
→ rejected_action_values
→ rejected_or_downgraded
→ primary_lifecycle_action_port
→ pm_lifecycle_learning_router

alpha_setup_ev_fusion（learning_used）
→ enabled
→ decision
→ target_side
→ intended_action
→ selected_profile
→ selected_action_value
→ profile_count
→ action_value_count
→ matched_action_value_count
→ ignored_action_value_count
→ scorecard_state
→ side_priority
→ ticker_side_priority
→ side_priority_score
→ candidate_quality
→ candidate_layer_hint
→ side_priority_semantics_version
→ side_priority_is_not_capital_rank
→ scorecard_gating_failures
→ current_confirmation_score
→ has_tradeable_support
→ has_monitorable_setup
→ setup_quality_ok
→ has_invalidation_or_stop
→ expectancy_lane
→ positive_action_value
→ positive_action_value_candidate
→ candidate_positive_action_preference
→ negative_action_value
→ positive_profile
→ positive_profile_raw
→ negative_profile
→ open_action_value_missing
→ qualified_positive_expectancy
→ repeat_loss_without_new_evidence
→ tail_loss_blocks_real_amplification
→ strong_realtime_evidence
→ strong_market_confirmation
→ technical_supports_side
→ technical_direction_supports_side
→ technical_entry_timing_supports_side
→ technical_opposes_side
→ fundamental_supports_side
→ news_supports_side
→ independent_support_count
→ profile_stats
→ action_value_stats
→ multiplier
→ max_profile_impact
→ gate_failures
→ pre_control_ratio
→ final_ratio
→ not_product_blacklist
→ same_scope_required
→ candidate_prior_only
→ money_objective

profile_stats（alpha_setup_ev_fusion）
→ sample_count
→ win_rate
→ profit_factor
→ net_pnl

selected_profile（alpha_setup_ev_fusion）
→ scope_key
→ ticker
→ side
→ horizon_class
→ market_regime
→ setup_type
→ data_combo
→ lifecycle_state
→ profile_state_hint
→ profile_state_hint_boundary
→ sample_count
→ trade_count
→ win_rate
→ profit_factor
→ net_pnl
→ confidence_score
→ max_position_impact
→ valid_until
→ product_learning_calibration_view

product_learning_calibration_view（selected_profile）
→ contract_version
→ source_contract_version
→ performance_scope_key
→ ticker
→ side
→ horizon_class
→ market_regime
→ setup_type
→ action_name
→ trigger_key
→ evidence_combo
→ opportunity_state
→ deployment_tier
→ historical_pm_rank
→ historical_pm_score
→ historical_selected_for_capital_deployment
→ historical_net_pnl
→ outcome_label
→ reward_source
→ not_trade_authority
→ future_only
→ analyst_usage_boundary

selected_action_value（alpha_setup_ev_fusion / positive_open_seed / execution_action_value_preference）
→ id
→ scope_key
→ ticker
→ side
→ horizon_class
→ market_regime
→ setup_type
→ action_name
→ sample_count
→ reward_sum
→ reward_mean
→ win_rate
→ confidence_score
→ action_preference
→ canonical_action_preference_source
→ max_position_impact
→ last_sample_date
→ valid_until
→ source
→ reward_source
→ consumer_scope
→ canonical_action_family
→ learning_lane
→ memory_side_role
→ memory_requirement_reason
→ retrieval_key
→ fallback_retrieval_key
→ execution_retrieval_key
→ retrieval_match_level
→ retrieval_match_reason
→ strict_no_lookahead
→ evidence_scope
→ amplification_scope_quality
→ action_value_lane
→ exact_state_real_trade_sample_count
→ partial_state_real_trade_sample_count
→ similar_real_trade_sample_count
→ exact_ticker_sample_count
→ exact_ticker_real_trade_sample_count
→ real_trade_reward_count
→ counterfactual_prior_only
→ counterfactual_reward_count
→ loss_reward_count
→ tail_loss_count
→ worst_reward
→ canonical_action_value
→ canonical_action_value_source

action_value_stats（alpha_setup_ev_fusion）
→ action_name
→ sample_count
→ reward_mean
→ reward_sum
→ win_rate
→ confidence_score
→ action_preference
→ canonical_action_preference_source
→ exact_ticker_support
→ scope_quality
→ real_amplification_support
→ loss_reward_count
→ tail_loss_count
→ worst_reward

capital_utilization_learning（learning_used）
→ protected_memory
→ recovering_memory
→ learned_demote_record
→ adaptive_protect
→ adaptive_protect_record
→ learned_underperformance_block
→ protected_evidence_rejected
→ conflicting_weak_memory

capital_utilization_target（learning_used）
→ target_mode
→ high_quality_memory
→ current_margin_ratio
→ target_margin_ratio_min
→ target_margin_ratio_max
→ target_margin_ratio_confirmed
→ base_max_position_ratio
→ effective_max_position_ratio
→ effective_single_margin_ratio_cap
→ dynamic_opportunity_margin_ratio_budget
→ dynamic_opportunity_margin_ratio_cap
→ dynamic_allocation_tier
→ dynamic_budget_diagnostics
→ alpha_release_tier
→ alpha_release
→ stop_protected
→ structured_invalidation
→ base_position_anchor_lifted
→ single_position_cap_lifted
→ opportunity_margin_cap_limited
→ underutilization_breach
→ capital_allocation_tier
→ margin_ratio_gap_to_min

requirement_details[]（memory_retrieval）
→ side
→ lane
→ memory_side_role
→ row_count
→ error
→ effective_memory_summary
→ retrieval_attempts
→ rejected_or_downgraded

rejected_action_values[]（memory_retrieval）
→ id
→ scope_key
→ ticker
→ side
→ setup_type
→ action_name
→ learning_lane
→ memory_side_role
→ reason

positive_open_seed（learning_used）
→ enabled
→ decision
→ target_side
→ seed_position_ratio
→ selected_action_value
→ current_evidence
→ not_product_rule
→ does_not_bypass_final_contract_authority

current_evidence（positive_open_seed）
→ scorecard_state
→ strong_realtime_evidence
→ strong_market_confirmation
→ technical_entry_timing_supports_side
→ technical_opposes_side
→ has_tradeable_support
→ has_invalidation_or_stop
→ current_confirmation_score
→ independent_support_count

learning_adjustment_summary（learning_used）
→ positive_policy_count
→ negative_policy_count
→ positive_action_value_count
→ negative_action_value_count
→ exact_real_action_value_count
→ episode_action_value_count
→ positive_learning_signal
→ negative_learning_signal
→ execution_profile_learning_signal
→ recent_tail_loss_signal
→ entry_quality_loss_signal
→ trigger_quality_positive_signal
→ trigger_quality_loss_signal
→ net_trigger_quality_loss_signal
→ strongest_positive_action_value
→ strongest_negative_action_value
→ alpha_setup_score_adjustment
→ best_profile_state
→ best_profile_scope_key
→ capped_or_rejected_profile_count
→ effect
→ not_trade_authority

pm_lifecycle_learning_router（learning_used）
→ tool
→ pm_lifecycle_action_port
→ accepted_lanes
→ decision_learning_rows
→ accepted_learning
→ accepted_indices
→ decision_learning_indices
→ rejected_learning_rows
→ rejected_learning
→ rejected_indices
→ trigger_profile_learning_rows
→ trigger_profile_learning
→ trigger_profile_indices
→ execution_profile_learning
→ execution_profile_indices
→ not_rank_learning
→ trigger_profile_learning_direct_to_rank
→ execution_profile_learning_direct_to_rank
→ writes_db
→ writes_contract
→ no_llm

pm_lifecycle_learning_trace（learning_used）
→ trace_version
→ contract_lifecycle_port
→ pm_lifecycle_action_port
→ router_source
→ rank_lifecycle
→ used_lanes
→ accepted_learning_lanes
→ decision_learning_rows
→ trigger_profile_learning
→ trigger_profile_learning_rows
→ trigger_profile_indices
→ rejected_learning
→ rejected_learning_lanes
→ pm_lifecycle_learning_router
→ blocked_learning_lanes
→ execution_profile_learning_direct_to_rank
→ trigger_profile_learning_direct_to_rank
→ memory_requirement_status
→ memory_requirements
→ hold_learning_decision
→ reduce_exit_learning_decision
→ open_add_learning_decision
→ conditional_monitor_learning_decision
→ execution_profile_learning_decision
→ execution_profile_signal_direct_to_rank
→ final_contract_effect_fields

pm_lifecycle_learning_impact_delta（learning_used）
→ trace_version
→ current_lots
→ target_lots
→ lots_delta
→ pre_learning_position_ratio
→ final_target_position_ratio
→ position_ratio_delta
→ open_add_rank_score_delta
→ alpha_setup_multiplier
→ alpha_setup_expectancy_lane
→ hold_decision
→ hold_changes_position
→ reduce_exit_decision
→ reduce_exit_changes_position
→ conditional_monitor_decision
→ execution_profile_changed
→ execution_profile_learning_direct_to_rank

learning_to_position_summary（learning_used）
→ trace_version
→ learning_context
→ learning_source_summary
→ position_effect
→ opportunity_to_position
→ current_day_validation
→ holding_lifecycle
→ artifact_boundary

learning_context（learning_to_position_summary）
→ enabled
→ selected_digest_count
→ candidate_hypothesis_count
→ validated_hypothesis_count
→ candidate_hypothesis_authority

learning_source_summary（learning_to_position_summary）
→ adaptive_policy_summary
→ alpha_setup_profile_summary
→ action_value_summary
→ strategy_memory_summary

adaptive_policy_summary（learning_source_summary）
→ policy_count
→ policy_type_counts
→ scope
→ status

alpha_setup_profile_summary（learning_source_summary）
→ profile_count
→ lifecycle_counts
→ status

action_value_summary（learning_source_summary）
→ action_value_count
→ canonical_action_value_count
→ incomplete_trace_action_value_count
→ action_preference_counts
→ status

strategy_memory_summary（learning_source_summary）
→ status
→ raw_object_omitted

position_effect（learning_to_position_summary）
→ current_lots
→ target_lots
→ lots_delta
→ pre_control_position_ratio
→ final_target_position_ratio
→ action
→ action_lots
→ reason
→ control_reasons

opportunity_to_position（learning_to_position_summary）
→ target_side
→ scorecard_preferred_side
→ mature_alpha_policy_count
→ fast_candidate_alpha_count
→ high_quality_opportunity_present
→ high_quality_opportunity_executed_or_targeted
→ if_not_targeted_requires_accountability

current_day_validation（learning_to_position_summary）
→ market_confirmation_score
→ has_structured_invalidation
→ has_explicit_stop_protection
→ requires_today_signal_market_state_and_invalidation

holding_lifecycle（learning_to_position_summary）
→ decision
→ lifecycle_classification
→ holding_days
→ current_side
→ target_side
→ loss_revalidation_due
→ loss_revalidation_failed
→ market_confirmation_score

artifact_boundary（learning_to_position_summary）
→ summary_only
→ research_fact_objects_omitted

pm_landing_consistency_audit（learning_used）
→ version
→ ticker
→ decision
→ opportunity_scorecard_alignment
→ analyst_setup_alignment
→ learning_alignment
→ pm_risk_gate_alignment
→ trader_pre_execution_feasibility
→ consistency_flags
→ consistent_enough_for_phase1
→ not_product_rule
→ no_future_data

decision（pm_landing_consistency_audit）
→ current_lots
→ target_lots
→ lots_delta
→ current_position_ratio
→ final_position_ratio
→ recommendation_action
→ action_type
→ lots_to_trade
→ lots_to_trade_reason
→ control_reasons

opportunity_scorecard_alignment（pm_landing_consistency_audit）
→ preferred_side
→ target_side
→ side_final_state
→ side_score
→ opportunity_score
→ opportunity_score_components
→ side_priority
→ ticker_side_priority
→ capital_allocation_reason
→ learning_adjustment_summary
→ gating_failures
→ entry_setup_count
→ invalidation_count

learning_alignment（pm_landing_consistency_audit）
→ learning_enabled
→ selected_digest_ids
→ candidate_hypothesis_count
→ validated_hypothesis_count
→ policy_count
→ policy_types
→ alpha_setup_profile_count
→ alpha_setup_lifecycle_counts
→ alpha_setup_action_value_count
→ alpha_setup_action_preference_counts
→ money_decision_trace_required

trader_pre_execution_feasibility（pm_landing_consistency_audit）
→ margin_required
→ margin_available
→ margin_feasible
→ market_confirmation_score
→ actual_trader_result_pending_phase2

execution_action_value_preference（final_action_contract）
→ enabled
→ source
→ execution_profile
→ trigger_source
→ base_execution_profile
→ reason_codes
→ selected_action_value
→ same_scope_required
→ does_not_create_trade_authority
→ keeps_pm_authority_boundary

capital_deployment（final_action_contract）
→ selected_for_capital_deployment
→ capital_allocation_reason
→ original_target_lots
→ deployed_target_lots
→ deployed_lots_delta
→ reason_codes
→ opportunity_rank
→ rank_capital_role
→ capital_layer
→ capital_ratio_source
→ rank_reason
→ rank_source
→ rank_scope
→ capital_rank_generated_by
→ rank_input_components
→ rank_semantics_version
→ opportunity_rank_meaning
→ rank_is_capital_priority
→ rank_is_not_trade_authority
→ rank_budget_sequence
→ rank_score
→ candidate_margin_ratio
→ queue_margin_ratio_before
→ queue_margin_ratio_after_if_selected
→ target_margin_ratio_budget
→ max_single_ticker_margin_ratio
→ current_net_exposure_before
→ current_ticker_exposure
→ target_position_ratio
→ projected_net_exposure_if_selected
→ max_net_exposure
→ single_ticker_budget_ok
→ total_margin_budget_ok
→ net_exposure_budget_ok

consistency（final_action_contract）
→ status
→ mode
→ issues
→ actual
→ expected

signal_collection_contract_ref（final_action_contract）
→ ticker
→ trading_date
→ source_contract_count
→ collector_decision_boundary

step6_contract_generation_check（pm_six_step_trace）
→ tool
→ ok
→ errors
→ expected_final_action
→ actual_final_action
→ current_lots
→ target_lots
→ lots_delta
→ writes_db
→ writes_contract
→ no_llm

pm_contract_self_check（pm_six_step_trace）
→ tool
→ ok
→ errors
→ expected_final_action
→ actual_final_action
→ current_lots
→ target_lots
→ lots_delta
→ writes_db
→ writes_artifact
→ writes_payload

【Trader：phase2_execution嵌套结构】
execution_contract（phase2_execution）
→ contract_code
→ setup_type
→ horizon_class
→ expected_horizon_days
→ market_regime
→ execution_profile
→ trigger_source
→ entry_trigger
→ invalidation
→ invalidation_level
→ atr_stop_distance
→ valid_until
→ requires_intraday_confirmation
→ can_execute_without_intraday_trigger
→ authority_type
→ max_allowed_margin_ratio
→ reason_codes
→ execution_action_value_preference

translated_decision（phase2_execution）
→ action
→ lots
→ contract_code
→ price

intraday_selection（phase2_execution）
→ decision
→ reason
→ base_price
→ base_datetime
→ base_price_source
→ signal_datetime
→ features
→ trigger_checked
→ trigger_passed
→ price_chase_check
→ execution_failure_reason
→ missed_opportunity_flag
→ learning_writeback_contract

price_chase_check（intraday_selection）
→ checked
→ passed
→ reason
→ gap_ratio
→ threshold

features（intraday_selection）
→ error
→ underlying_code
→ contract_code
→ action
→ execution_mode
→ execution_profile
→ execution_contract
→ signal_close
→ vwap
→ opening_range
→ signal_bars
→ eligible_signal_bars
→ execution_bars
→ min_execution_volume
→ latest_execution_bar
→ finalize_untriggered
→ trigger_rule
→ chase_check

opening_range（features）
→ high
→ low
→ minutes
→ start
→ complete_at
→ complete
→ bars

chase_check（features）
→ passed
→ reason
→ gap_ratio
→ threshold

setup_execution_learning（phase2_execution）
→ consumer_scope
→ learning_lane
→ setup_type
→ opportunity_state
→ preferred_side
→ execution_contract
→ final_contract_execution_fields
→ analyst_action_evidence_contracts
→ analyst_learning_scopes
→ execution_contract_summary
→ learning_boundary
→ phase2_status
→ no_trade_reason
→ intraday_selection
→ reason_family

final_contract_execution_fields（setup_execution_learning）
→ contract_version
→ contract_type
→ ticker
→ underlying_code
→ contract_code
→ final_action
→ current_lots
→ target_lots
→ lots_delta
→ entry_trigger
→ invalidation
→ invalidation_condition
→ requires_intraday_confirmation
→ can_execute_without_intraday_trigger
→ execution_profile
→ execution_requirement
→ trigger_source
→ authority_type
→ authority_decision
→ reason_codes
→ single_source_of_trade_truth
→ candidate_sources_do_not_bypass_contract

analyst_action_evidence_contracts（setup_execution_learning）
→ technical
→ fundamental
→ commodity_news

analyst_learning_scopes（setup_execution_learning）
→ technical
→ fundamental
→ commodity_news

execution_contract_summary（setup_execution_learning）
→ profile
→ trigger_source
→ entry_trigger
→ invalidation
→ requires_intraday_confirmation
→ can_execute_without_intraday_trigger
→ authority_type

learning_boundary（setup_execution_learning）
→ consumer_scope
→ trader_executes_only
→ execution_feedback_future_only
→ not_strategy_creation
→ learning_source
→ no_full_final_action_contract_mirror

pm_plan_validation（phase2_execution）
→ passed
→ reason
→ validation_errors
→ required_for
→ source_type
→ contract_type
→ current_lots
→ target_lots
→ target_lots_after_validation
→ original_target_lots
→ contract_current_lots
→ actual_current_lots
→ contract_lots_delta
→ expected_lots_delta
→ final_contract_execution_fields
→ contract_authority_audit
→ authority_consistency
→ business_boundary

contract_authority_audit（pm_plan_validation）
→ authority_type
→ authority_decision
→ max_allowed_margin_ratio
→ reason_codes
→ open_action_evidence
→ strong_current_evidence
→ watch_for_trigger_block
→ conditional_trigger_authority
→ requires_intraday_confirmation

authority_consistency（pm_plan_validation）
→ passed
→ reason
→ selected_authority
→ sources
→ business_boundary

selected_authority（authority_consistency）
→ authority_type
→ authority_decision
→ requires_authority
→ open_action_evidence
→ strong_current_evidence
→ watch_for_trigger_block
→ conditional_trigger_authority
→ negative_profile
→ tradeable_state
→ weak_conflict_probe
→ max_allowed_margin_ratio
→ reason_codes

sources[]（authority_consistency）
→ source
→ authority_type
→ authority_decision
→ open_action_evidence
→ strong_current_evidence
→ max_allowed_margin_ratio

contract_execution_observation（phase2_execution）
→ signal_invalidation_observed
→ exit_policy_required
→ exit_policy_reason
→ business_boundary

entry_authority_gate（phase2_execution）
→ status
→ reason
→ current_lots
→ target_lots
→ business_boundary

exit_policy（phase2_execution）
→ enabled
→ exit_required
→ target_lots
→ reason
→ policy
→ same_direction_supported
→ days_held
→ is_probe

entry_timing（phase2_execution）
→ entry_action_family
→ opening_range
→ target_lots_source

execution_simulation（phase2_execution）
→ base_price
→ base_price_source
→ base_price_date
→ open_price
→ prev_close_price
→ warning_message

【Trader：execution_translation嵌套结构】
translated_orders[]（execution_translation）
→ stage
→ action
→ lots
→ contract_code
→ price

signal_lifecycle（execution_translation）
→ horizon_class
→ expected_horizon_days
→ entry_trigger
→ invalidation_level
→ atr_stop_distance
→ setup_type
→ market_regime
→ target_price

execution_contract（execution_translation）
→ contract_code
→ setup_type
→ horizon_class
→ expected_horizon_days
→ market_regime
→ execution_profile
→ trigger_source
→ entry_trigger
→ invalidation
→ invalidation_level
→ atr_stop_distance
→ valid_until
→ requires_intraday_confirmation
→ can_execute_without_intraday_trigger
→ authority_type
→ max_allowed_margin_ratio
→ reason_codes
→ execution_action_value_preference

intraday_execution（execution_translation）
→ decision
→ reason
→ base_price
→ base_datetime
→ base_price_source
→ signal_datetime
→ features
→ trigger_checked
→ trigger_passed
→ price_chase_check
→ execution_failure_reason
→ missed_opportunity_flag
→ learning_writeback_contract

price_chase_check（intraday_execution）
→ checked
→ passed
→ reason
→ gap_ratio
→ threshold

phase2_order_plan（execution_translation）
→ current_lots
→ target_lots
→ action
→ lots
→ contract_code
→ price
→ account_equity
→ current_price
→ risk_level
→ cashflow_ratio
→ current_margin_ratio
→ max_total_margin_ratio
→ max_single_margin_ratio
→ remaining_margin
→ signal_lifecycle
→ execution_contract
→ consistency_diagnostics

final_action_contract_source（execution_translation）
→ source
→ contract_type
→ final_action
→ current_lots
→ target_lots
→ lots_delta

auditor_verdict（execution_translation）
→ producer
→ audit_status
→ audit_verdict
→ audit_reason_codes
→ audited_by
→ audited_at

final_execution_basis（execution_translation）
→ base_price
→ base_price_source
→ base_price_date
→ open_price
→ prev_close_price
→ execution_price
→ execution_price_basis
→ slippage_model
→ slippage_ticks
→ slippage_amount
→ intraday_execution
→ execution_learning_fields
→ signal_lifecycle

execution_learning_fields（final_execution_basis）
→ trigger_checked
→ trigger_passed
→ price_chase_check
→ execution_failure_reason
→ missed_opportunity_flag

market_rule_block（execution_translation）
→ limit_lock
→ contract_expiry_guard

limit_lock（market_rule_block）
→ status
→ limit_up
→ limit_down
→ trade_date
→ ticker
→ enabled
→ action
→ execution_price
→ tolerance_ticks
→ minimum_tick
→ blocked
→ reason
→ side
→ limit_price

contract_expiry_guard（market_rule_block）
→ enabled
→ action
→ contract_code
→ trading_date
→ source_type
→ blocked
→ reason
→ status
→ last_trade_date
→ days_to_last_trade
→ delivery_month
→ days_to_delivery_month

【Trader：execution_result嵌套结构】
actual_transactions[]（execution_result）
→ action
→ lots
→ contract_code
→ execution_price
→ execution_phase

no_trade_reason_category（execution_result）
→ reason
→ category
→ category_label
→ category_description
→ source

execution_learning_trace（execution_result）
→ consumer_scope
→ learning_lane
→ execution_retrieval_key
→ outcome
→ status
→ no_trade_reason
→ no_trade_reason_category
→ actual_transaction_count
→ turn_into_memory
→ not_direction_evidence
→ execution_learning_type
→ timing_strategy_question

consistency_diagnostics（execution_result）
→ status
→ issues
→ phase2_plan_action
→ phase2_plan_lots
→ actual_action
→ actual_lots
→ no_trade_reason

【Auditor：策略审计audit_payload嵌套结构】
source（audit_payload策略审计后）
→ pm_recommendation_id
→ final_action_contract_hash_source
→ contract_state_source
→ data_quality_source

boundary（audit_payload策略审计后）
→ auditor_does_not_modify_final_action_contract
→ auditor_does_not_create_trade_authority
→ trader_requires_approved_audit_verdict
→ research_memory_not_consumed
→ auditor_reads_research_db

contract_summary（audit_payload策略审计后）
→ final_action
→ current_lots
→ target_lots
→ lots_delta
→ contract_code
→ invalidation_present
→ requires_intraday_confirmation
→ can_execute_without_intraday_trigger

semantic_state（audit_payload策略审计后）
→ lifecycle_state
→ requires_intraday_result
→ hard_block_reasons
→ soft_limit_reasons
→ semantic_errors

【Trader：执行后audit_payload嵌套结构】
trade_contract_audit（audit_payload执行后）
→ audit_boundary
→ single_source_of_trade_truth
→ candidate_sources_do_not_bypass_contract
→ contract_version
→ final_action
→ authority_type
→ authority_decision
→ open_action_evidence
→ strong_current_evidence
→ current_lots
→ target_lots
→ lots_delta
→ target_margin_ratio_estimate
→ max_allowed_margin_ratio
→ reason_codes
→ execution_profile
→ execution_requirement
→ pm_plan_validation_passed
→ pm_plan_validation_reason
→ authority_consistency_reason
→ business_boundary

independent_auditor（audit_payload执行后）
→ producer
→ audit_status
→ audit_verdict
→ audit_reason_codes
→ audited_at
```
