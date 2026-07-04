# AgentQuant 统一字段语义表

本文是 AgentQuant 的唯一字段语义表。系统从分析、决策、审计、执行、结算、复盘、研究、学习到评估，只允许使用本文定义的字段语义。

核心规则：

- 分析师只输出结构化证据，不输出仓位、保证金、交易授权。
- PM 只输出一张可执行策略合约：`final_action_contract`。
- Auditor 只输出审计结论：`audit_verdict`。
- Trader 只执行审计通过后的 `final_action_contract`，只写执行结果：`execution_result`。
- Accountant 只按成交和结算价写结算事实：`daily_settlement`。
- Reviewer 写复盘归因。
- Researcher 写分动作 action-value 学习。
- 换月、强平、回放、反事实观察不是策略交易，必须用 `source_type != strategy` 分账，不能污染策略 action-value。
- `payload`、`payload_json`、`artifact_json`、`signal_snapshot`、`evidence_json`、`result_json`、`features_json` 等只允许作为结构化容器；容器里的业务字段必须属于本文字段，不能形成第二套语义。

共享解释器：`src/tools/common/final_action_semantics.py` 是全系统唯一的确定性交易语义状态机。它不调用 LLM，不签合约，不下单，不入账，不写研究；只统一解释分析师证据禁用字段、信号收集边界、`final_action_contract` 全生命周期、`reason_codes` 分类、条件监控、直接执行、普通持有、硬阻断、软降级、未触发、已触发成交、扩大交易、减仓和退出。Protocol Governor 只能通过该工具解释学习 lane 匹配、`final_action + current_lots + target_lots + lots_delta` 一致性、no-change / rank / learning 无仓位变化解释、active opportunity rejection 和 open transaction blocker；PG 不得保留私有 reason code 词表或私有 final_action 推断口径。

分析师差异化分析协议：`src/config/product_price_behavior_profiles.yaml` 是三类分析师的商品价格行为冷启动配置；`src/tools/agent_tools/analysis/analyst_product_price_behavior_profile.py` 是三类分析师共享的确定性读取与格式化工具。它只服务 `technical`、`fundamental`、`commodity_news` 的证据分析，输出 `product_profile_evidence`，用于区分品种价格行为、趋势惯性、波动阈值、产业链确认、季节窗口、假突破风险和适合的 setup。它不调用 LLM，不读研究库，不签合约，不下单，不入账，不写研究；PM 只能从 `signal_collection_contract` 读取它作为证据上下文，Auditor、Trader、Accountant 不直接读取或解释该 profile。

多维证据融合预测协议：`src/config/evidence_fusion_policy_catalog.yaml` 是证据融合冷启动策略目录；`src/tools/common/evidence_fusion_semantics.py` 是跨分析师、信号收集、PM 评分、Auditor 审核、Reviewer 归因和 Researcher 学习上下文的确定性解释工具。它只解释技术、基本面、新闻、商品 profile、历史学习上下文和执行反馈形成的预测证据强弱、时效、一致性、冲突、确认需求和缺失证据；不调用 LLM，不签合约，不下单，不入账，不直接写 action-value。Trader 和 Accountant 不读取该工具，也不能用融合证据改执行或结算。

## 1. 通用消息与 artifact 字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `id` | 所有持久化记录 | 记录或 artifact 的稳定 ID。 |
| `contract_version` | 所有合约 / artifact | 合约版本。 |
| `message_type` | 智能体内部消息 | 消息类型。 |
| `artifact_type` | artifact 外层 | artifact 类型。 |
| `source_agent` | 所有智能体输出 | 生成该记录的智能体；运行时统一使用它表达来源。 |
| `agent_name` | artifact header / 展示字段 | 人类可读的智能体名称；不能作为交易决策字段。 |
| `analyst` | 分析师记录 / 学习预算 | 分析师名称，如 technical、fundamental、commodity_news。 |
| `config_id` | 所有运行记录 | 本次回测 / 配置实例 ID。 |
| `exp_name` | config | 实验名称。 |
| `trading_date` | 所有交易日记录 | 记录所属交易日。 |
| `effective_trade_date` | 推荐 / 执行 | 推荐实际可执行日期。 |
| `ticker` | 行情 / 信号 / 研究 | 品种代码。 |
| `sector` | 分析师 / 研究 / 绩效 | 品种所属板块或行业分组。 |
| `underlying_code` | 合约 / 换月 / 推荐 | 标的品种代码。 |
| `contract_code` | 合约 / 成交 / 结算 | 具体期货合约。 |
| `portfolio_id` | 组合 / 成交 / 结算 | 组合 ID。 |
| `reference_portfolio_id` | PM 推荐 | PM 决策使用的参考组合 ID。 |
| `recommendation_id` | 推荐 / 执行 / 研究 | 关联 PM 推荐记录。 |
| `evidence_pack_id` | 复盘 / artifact | 复盘证据包 ID。 |
| `created_at` | 所有持久化记录 | 创建时间。 |
| `updated_at` | 可变学习 / 组合记录 | 更新时间。 |
| `last_updated` | 绩效 / 模板记录 | 最后更新时间。 |
| `snapshot_at` | memory history / 快照记录 | 快照生成时间。 |
| `valid_until` | 记忆 / 策略 / 学习 | 有效截止日期。 |
| `active` | 记忆 / 策略 / 学习 | 是否启用。 |
| `status` | 生命周期记录 | pending、executed、skipped、failed、open、candidate、applied 等状态。 |
| `phase` | `trading_day_phase` / workflow | 当前阶段。 |
| `started_at` | `trading_day_phase` | 阶段开始时间。 |
| `completed_at` | `trading_day_phase` | 阶段完成时间。 |
| `message` | `trading_day_phase` | 阶段说明。 |
| `incomplete_trading_day_phase` | 验收错误码 | 交易日存在推荐、成交、盘中决策或学习记录，但 phase1-4 未全部 completed；必须删除或重跑当天，不能进入策略结论或学习。 |
| `mechanism_effectiveness_audit` | Protocol Governor 只读报告 / 回测后机制验收 | 机制链路有效性报告；按交易生命周期场景检查 action-value、PM score/rank、唯一合约、持仓/减仓/退出和条件 probe 是否接通，不评价收益，不创建交易权限。 |
| `hard_failures` | `mechanism_effectiveness_audit` | 机制断链列表；如学习存在但 PM 未读取、开仓/加仓/条件监控学习没有进入 score/rank、rank 写入但未影响合约且无解释、持仓/退出学习没有落到减仓/退出或解释、条件 probe 消失等。非空时回测应 fail-fast，不能进入策略收益评价；减仓/退出场景不强制要求 `opportunity_rank`。 |
| `diagnostics` | `mechanism_effectiveness_audit` / 评估报告 | 机制已接通但效果差的诊断列表；如高 rank 亏损、资金利用率低、正 alpha 放大不足。不会停止回测，只进入策略分析。 |
| `checked_chain` | `mechanism_effectiveness_audit.metadata` | 本次机制审计检查的链路节点，如 action_value_to_pm、score_to_rank、rank_to_final_action_contract、conditional_probe_to_trader_result。 |
| `checked_scenarios` | `mechanism_effectiveness_audit.metadata` | 本次机制审计的生命周期场景矩阵，如 open_increase、conditional_monitor、reduce_exit、position_hold、unselected_candidate、flat_wait；只定义审计口径，不是交易权限。 |
| `scenarios` | `mechanism_effectiveness_audit.counts` | 本次审计中各生命周期场景命中的推荐数量；只用于解释审计覆盖，不评价策略收益。 |
| `classification` | `mechanism_effectiveness_audit.metadata` | 区分 `hard_fail` 与 `diagnostic` 的报告分类说明；不能作为交易字段。 |
| `contract_coverage_audit` | Protocol Governor 只读版本级闸门 / 回测前验收 | 契约覆盖报告；检查关键契约是否有 producer、consumer、audit、test、字段表、配置/提示词/文档对齐，并要求关键智能体边界存在 producer-to-consumer 保真测试；不读收益、不写 DB、不创建交易权限。 |
| `matrix` | `contract_coverage_audit` | 契约覆盖矩阵列表；每行对应一个核心契约。 |
| `artifact_phase_boundary` | `contract_coverage_audit.matrix[].contract` / Protocol Governor 只读边界名 | artifact 阶段保存边界；规定 PM、审计员、交易员、会计师、复盘员、研究员 artifact 能保存和禁止保存的字段集合。只用于回测前契约覆盖和系统不变量审计，不是交易字段，不创建合约或交易权限。 |
| `producers` | `contract_coverage_audit.matrix[]` | 该契约的生产路径证据。 |
| `consumers` | `contract_coverage_audit.matrix[]` | 该契约的消费路径证据。 |
| `audits` | `contract_coverage_audit.matrix[]` | 该契约被系统审计或机制审计覆盖的证据。 |
| `tests` | `contract_coverage_audit.matrix[]` | 该契约被真实路径测试覆盖的证据；关键跨智能体边界必须包含字段保真测试，例如 Researcher action-value 进入 PM 后不能丢失 `id/action_preference/reward_source/evidence_scope/action_value_lane/reward`。 |
| `uncovered_risks` | `contract_coverage_audit.matrix[]` | 契约覆盖缺口；非空时表示版本级闸门失败，不能进入回测。 |
| `payload` | artifact 外层 | 结构化载荷容器；不能引入未登记语义。 |
| `payload_json` | 数据库存储 | `payload` 序列化结果；不能被当成另一套字段表。 |
| `artifact_json` | signal 表 | 分析师 artifact 序列化容器。 |
| `artifact_path` | artifact 元数据 | 外部 artifact 路径。 |
| `sha256` | artifact 元数据 | artifact 内容哈希。 |
| `size` | artifact 元数据 | artifact 大小。 |
| `summary_json` | artifact 元数据 | artifact 摘要。 |
| `audit_payload_artifact_path` | audit payload artifact 元数据 | `audit_payload` 外部 artifact 路径。 |
| `audit_payload_sha256` | audit payload artifact 元数据 | `audit_payload` 内容哈希。 |
| `audit_payload_size` | audit payload artifact 元数据 | `audit_payload` 大小。 |
| `audit_payload_summary_json` | audit payload artifact 元数据 | `audit_payload` 摘要。 |
| `llm_prompt_artifact_path` | LLM prompt artifact 元数据 | `llm_prompt` 外部 artifact 路径。 |
| `llm_prompt_sha256` | LLM prompt artifact 元数据 | `llm_prompt` 内容哈希。 |
| `llm_prompt_size` | LLM prompt artifact 元数据 | `llm_prompt` 大小。 |
| `llm_prompt_summary_json` | LLM prompt artifact 元数据 | `llm_prompt` 摘要。 |
| `llm_provider` | LLM 输出 / config | LLM 提供方。 |
| `llm_model` | LLM 输出 / config | LLM 模型。 |
| `determinism_mode` | LLM / 确定性输出 | 生成模式。 |
| `llm_prompt` | LLM 审计字段 | 发送给 LLM 的提示词记录；不能参与交易决策。 |
| `raw_prompt` | Researcher LLM notes | 研究员 LLM 原始 prompt。 |
| `raw_response` | Researcher LLM notes | 研究员 LLM 原始 response。 |
| `data_cutoff` | 分析师 / PM / artifact | 数据截止点，用于防未来函数。 |
| `data_usage_summary` | 分析师证据 / 复盘 / 研究 | 本次分析使用的数据来源、日期范围、缺失情况、新鲜度。 |
| `product_price_behavior_profiles` | config catalog | 三类分析师商品差异化分析冷启动配置；不随回测自动改写，不创建交易权限。 |
| `product_price_behavior_profile` | 分析师输入上下文 | 单品种价格行为分析框架，定义趋势惯性、波动、确认要求、季节窗口和假突破风险；只用于证据分析。 |
| `product_profile_evidence` | 分析师 `metadata` / `action_evidence_contract` / `signal_collection_contract.source_contracts` | 分析师实际使用商品差异化 profile 的结构化痕迹；只能说明证据强调与确认纪律，不能包含手数、保证金、reason code 或最终交易动作。 |
| `product_profile_id` | `product_profile_evidence` / `action_evidence_contract.learning_scope` / `signal_collection_contract.evidence_items` | 品种 profile 的稳定 ID，格式为 profile version 加 ticker；用于复盘和研究识别分析框架来源。 |
| `product_profile_version` | `product_profile_evidence` | 品种 profile 版本。 |
| `product_profile_used` | `product_profile_evidence` / `action_evidence_contract.learning_scope` | 本次分析是否使用了商品差异化 profile。 |
| `profile_fields_used` | `product_profile_evidence` | 分析师本次使用的 profile 字段集合。 |
| `profile_supported_evidence` | `product_profile_evidence` | 当前证据支持 profile 预期行为的部分。 |
| `profile_conflicting_evidence` | `product_profile_evidence` | 当前证据与 profile 预期行为冲突的部分。 |
| `profile_missing_evidence` | `product_profile_evidence` | 使用该 profile 时仍缺失的确认项。 |
| `profile_assumption_status` | `product_profile_evidence` | profile 假设在当前日证据下的状态。 |
| `profile_relevance_score` | `product_profile_evidence` | profile 对本次分析的相关性评分；不是机会评分或交易排序。 |
| `profile_learning_interaction` | `product_profile_evidence` | 静态 profile 与动态 `learning_context` / `analyst_learning_calibration` 的关系说明。 |
| `profile_invalid_use_flags` | `product_profile_evidence` | 本次分析中被识别的 profile 错用风险，如把成本变化当直接交易权限。 |
| `profile_analysis_boundary` | `product_profile_evidence` | 固定为分析证据边界，声明该 profile 不创建交易权限。 |
| `evidence_fusion_policy` | config catalog | 多维证据融合预测协议配置；只定义证据强弱、时效、冲突、确认需求、profile 融合和复盘学习口径，不创建交易权限。 |
| `evidence_fusion_semantics` | 公共工具 / 审计摘要 / 复盘摘要 / 研究输入摘要 | 由 `src/tools/common/evidence_fusion_semantics.py` 生成的只读融合语义解释；不签合约、不下单、不入账、不写当天交易事实。 |
| `fusion_evidence` | 分析师 `metadata.action_evidence_contract` / `signal_collection_contract.source_contracts` | 单个分析师的多维证据融合字段包，说明证据强弱、时效、冲突、缺失和确认需求；不是交易合约。 |
| `evidence_strength_score` | `fusion_evidence` / `evidence_fusion` | 预测证据强度的 0-1 确定性评分；可被 PM 用于排序分项，不能直接授权交易。 |
| `evidence_freshness` | `fusion_evidence` / `evidence_fusion` | 预测证据时效标签，如 fresh、usable、stale、unknown。 |
| `evidence_freshness_score` | `fusion_evidence` | 预测证据时效 0-1 评分。 |
| `evidence_decay_risk` | `fusion_evidence` | 证据失效风险，供分析师和 PM 判断是否需要更多确认。 |
| `technical_false_breakout_risk` | 技术面 `fusion_evidence` | 技术信号假突破风险；只能影响确认纪律和 PM 排序分项。 |
| `fundamental_opposition_strength` | 基本面 `fusion_evidence` | 基本面对当前方向的反向压制强度；不能直接阻断交易，必须由 PM 在合约里解释。 |
| `news_impact_window` | 新闻面 `fusion_evidence` | 新闻催化有效窗口；不是 Trader 触发权限。 |
| `one_off_event_risk` | 新闻面 `fusion_evidence` | 新闻是否属于一次性冲击或噪音风险。 |
| `fusion_boundary` | `fusion_evidence` / `evidence_fusion` | 融合字段权限边界；固定说明其不创建 score/rank/手数/交易动作。 |
| `no_lookahead_status` | 数据派生 artifact | 未来函数检查状态。 |
| `source_artifacts` | 所有 artifact | 上游 artifact ID 或来源说明。 |
| `validation_errors` | 所有合约 / artifact | 结构或语义校验错误。 |
| `source_artifacts_not_list` | 校验错误码 | `source_artifacts` 类型错误的校验说明，不是业务字段。 |
| `validation_errors_not_list` | 校验错误码 | `validation_errors` 类型错误的校验说明，不是业务字段。 |
| `artifact_header_missing` | 校验错误码 | artifact header 缺失说明，不是业务字段。 |
| `artifact_contract` | artifact 校验 | artifact 合约名。 |
| `artifact_validation_errors` | artifact 校验 | artifact 校验错误集合。 |
| `internal_message_contract` | 内部消息校验 | 内部消息合约名。 |
| `internal_message_contract_missing` | 校验错误码 | 内部消息合约缺失说明，不是业务字段。 |
| `internal_message_validation_errors` | 校验错误码 | 内部消息校验错误集合。 |
| `trade_research_contract_missing` | 校验错误码 | 研究合约缺失说明，不是业务字段。 |
| `invalid_no_lookahead_status` | 校验错误码 | `no_lookahead_status` 非法说明，不是业务字段。 |
| `invalid_message_no_lookahead_status` | 校验错误码 | 内部消息 `no_lookahead_status` 非法说明，不是业务字段。 |
| `message_source_artifacts_not_list` | 校验错误码 | 内部消息 `source_artifacts` 类型错误说明，不是业务字段。 |
| `message_validation_errors_not_list` | 校验错误码 | 内部消息 `validation_errors` 类型错误说明，不是业务字段。 |
| `research_factor_focus_not_list` | 校验错误码 | 研究合约 `factor_focus` 类型错误说明，不是业务字段。 |
| `research_conflicts_not_list` | 校验错误码 | 研究合约 `current_evidence_conflict` 类型错误说明，不是业务字段。 |

## 2. 数据边界与运行上下文字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `tickers` | config | 本次运行启用品种列表。 |
| `enabled_analysts` | workflow state | 启用的分析师列表。 |
| `market_type` | workflow state | 市场类型。 |
| `llm_config` | workflow state | LLM 配置对象。 |
| `config` | workflow state | 当前节点配置。 |
| `full_config` | workflow state | 完整配置。 |
| `router` | workflow state | 内部路由器对象；不是业务字段。 |
| `num_tickers` | workflow state | 启用品种数量。 |
| `pre_open_only` | workflow state | 是否仅运行盘前。 |
| `info_cutoff` | workflow state | 信息截止点。 |
| `morning_price_context` | workflow state | 早盘执行价格上下文。 |
| `portfolio` | workflow state | 当前组合对象。 |
| `analyst_signals` | workflow state | 分析师输出列表。 |
| `recommendation` | workflow state | PM 推荐记录。 |
| `futures_recommendation` | 数据库表名 | PM 推荐表名，不是字段语义。 |
| `ticker_daily_pnl` | 数据库表名 | 品种日盈亏表名，不是字段语义。 |
| `daily_settlement` | 数据库表名 | 日结算表名，不是字段语义。 |
| `signal_context_history` | 数据库表名 | 信号上下文历史表名，不是字段语义。 |
| `strategy_memory` | 数据库表名 | 策略记忆表名，不是字段语义。 |
| `setup_type_performance` | 数据库表名 | setup 类型绩效表名，不是字段语义。 |
| `analyst_performance` | 数据库表名 | 分析师绩效表名，不是字段语义。 |
| `adaptive_policy_state` | 数据库表名 | 自适应策略状态表名，不是字段语义。 |
| `capital_deployment_state` | 数据库表名 | 资金部署状态表名，不是字段语义。 |
| `config_learning_overlay` | 数据库表名 | 配置学习覆盖表名，不是字段语义。 |
| `research_position_feedback` | 数据库表名 | 研究持仓反馈表名，不是字段语义。 |
| `alpha_setup_profile` | 数据库表名 | alpha setup profile 表名，不是字段语义。 |
| `alpha_setup_sample` | 数据库表名 | alpha setup sample 表名，不是字段语义。 |
| `alpha_setup_action_value` | 数据库表名 | alpha setup action-value 表名，不是字段语义。 |
| `researcher_llm_notes` | 数据库表名 | 研究员 LLM notes 表名，不是字段语义。 |
| `provisional_policy_state` | 数据库表名 | 临时策略状态表名，不是字段语义。 |
| `learning_context_budget` | 数据库表名 | 学习上下文预算表名，不是字段语义。 |
| `trade_episode_memory` | 数据库表名 | 交易 episode 记忆表名，不是字段语义。 |
| `no_trade_opportunity_memory` | 数据库表名 | 无交易机会记忆表名，不是字段语义。 |
| `exploratory_hypothesis` | 数据库表名 | 探索假设表名，不是字段语义。 |

## 3. 分析师结构化证据字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `action_evidence_contract` | 分析师 `metadata` / PM 输入 | 分析师给 PM 的唯一证据契约。 |
| `signal` | `action_evidence_contract` / signal 表 | bullish、bearish、neutral；只表示方向，不是交易授权。 |
| `side` | `action_evidence_contract` / 研究状态 | long、short、flat。 |
| `confidence` | 分析师证据 / 学习输出 | 置信度。 |
| `confidence_score` | 数据库存储 | 数值置信度；运行时统一归一为 `confidence`。 |
| `justification` | 分析师 / 推荐 / 成交 | 可读理由；不能替代结构化字段。 |
| `horizon_class` | 分析师 / PM / 研究 | 期限类别。 |
| `analyst_horizon` | 分析师证据 | 分析师原始信号期限。 |
| `decision_horizon` | PM 证据融合 | PM 决策期限。 |
| `execution_horizon` | Trader 执行 | 执行期限。 |
| `validation_horizon` | Reviewer 复盘 | 验证期限。 |
| `expected_horizon_days` | 分析师 / 研究 | 预期期限天数。 |
| `market_regime` | 分析师 / state_key | 市场状态，如趋势、震荡、高波动。 |
| `trend_stage` | 技术证据 | 趋势阶段。 |
| `trend_direction` | 技术证据 | 技术趋势方向背景。 |
| `direction_context` | 分析师证据 | 方向背景说明；不能作为交易授权。 |
| `price_location` | 分析师证据 | 当前价格位置。 |
| `price_percentile` | 分析师证据 | 当前价格分位。 |
| `direction_anchor` | 分析师证据 | 中期方向锚。 |
| `setup_type` | 分析师 / 研究 state | 交易逻辑类型。 |
| `setup_quality_ok` | 分析师证据 | 形态值得关注；不代表当前已触发。 |
| `setup_quality_score` | 分析师 / 研究样本 | setup 质量评分。 |
| `setup_quality_notes` | 分析师证据 | setup 质量说明。 |
| `entry_quality` | 分析师证据 | 入场质量。 |
| `entry_trigger` | 分析师证据 | 当前触发事实或等待条件。 |
| `entry_timing_signal` | 技术证据 | 技术入场时机分类。 |
| `current_trigger_confirmed` | 分析师证据 / `action_evidence_contract` / 执行证据 | 当前触发已经被明确事实确认；它是 `trigger_valid=true` 的事实来源之一，不能由 `setup_quality_ok` 推出。 |
| `trigger_valid` | 分析师证据 | 当前触发是否已经成立。 |
| `trigger_quality_score` | 分析师证据 | 当前触发强度。 |
| `exit_hint` | 分析师证据 | 退出 / 减仓提示。 |
| `holding_period_hint` | 分析师证据 | 持仓周期提示。 |
| `invalidation_present` | 分析师证据 | 是否有明确失效边界。 |
| `invalidation_condition` | 分析师 / 复盘 | 失效条件。 |
| `invalidation_level` | 分析师 / 执行风控 | 数值失效价位。 |
| `atr_stop_distance` | 分析师 / 执行风控 | ATR 止损距离。 |
| `add_allowed` | 分析师证据 | 证据是否允许加仓讨论；最终仍由 PM 决定。 |
| `evidence_role` | 分析师证据 | 证据角色，如方向、入场、事件、风险、执行。 |
| `evidence_quality` | 分析师证据 | 证据质量。 |
| `business_quality_score` | 分析师证据 | 业务质量评分。 |
| `tradeability_reason` | 分析师证据 | 为什么可交易或不可交易。 |
| `reward_risk_ratio` | 分析师证据 | 预期收益风险比。 |
| `target_return` | 信号上下文 | 目标收益。 |
| `factor_focus` | 分析师证据 | 主要因子关注点。 |
| `current_evidence_conflict` | 分析师证据 | 当前冲突证据。 |
| `missing_evidence` | 分析师证据 | 缺失证据。 |
| `conflicting_factors` | 分析师证据 | 冲突因子。 |
| `counter_evidence` | 分析师证据 | 反向证据。 |
| `opportunity_type` | 分析师 / no-trade 记忆 | 机会类型。 |
| `opportunity_state` | 分析师证据 | `no_opportunity`、`watch_for_trigger`、`probe_candidate`、`tradeable_candidate`、`risk_reduction_candidate`。 |
| `learning_impact_summary` | 分析师证据 | 历史学习如何影响本次判断。 |
| `factor_calibration_summary` | 基本面证据 | 基本面因子校准摘要。 |
| `event_calibration_summary` | 新闻证据 | 新闻事件校准摘要。 |
| `research_contract_version` | 分析师证据 | 研究契约版本。 |
| `message_contract_version` | 分析师证据 | 内部消息契约版本。 |
| `metadata` | 分析师 artifact | 元数据容器；不能引入未登记交易语义。 |
| `sample_state` | trade research contract | 研究样本状态，只用于研究分层。 |
| `maturity` | trade research contract | 研究成熟度。 |
| `product_context` | trade research contract | 品种业务上下文。 |
| `price_behavior` | product price behavior profile | 品种价格行为摘要，如成本链敏感、库存驱动、季节/政策敏感；只用于分析框架。 |
| `trend_inertia` | product price behavior profile | 品种趋势惯性分层；技术分析用它调整趋势确认纪律，不是开仓权限。 |
| `volatility_profile` | product price behavior profile | 品种常态波动特征；分析师用它调整风险和触发质量要求。 |
| `false_breakout_risk` | product price behavior profile | 品种假突破风险分层；用于要求额外确认。 |
| `preferred_setups` | product price behavior profile | 该品种更适合关注的 setup 家族；不是 PM 排序结果。 |
| `caution_setups` | product price behavior profile | 该品种需要降级或额外确认的 setup 家族。 |
| `confirmation_requirements` | product price behavior profile / `product_profile_evidence` / `fusion_evidence` / `signal_collection_contract` | 该品种或当前融合证据必须优先寻找的确认项，如库存、成本链、下游需求、季节窗口、价格量能确认；不是交易授权。 |
| `fundamental_driver_priority` | product price behavior profile | 基本面分析师的驱动优先级；只影响证据排序，不影响 PM 权限。 |
| `news_catalyst_priority` | product price behavior profile | 新闻分析师的催化优先级；只影响事件筛选，不影响 Trader 触发。 |
| `seasonal_event_window` | product price behavior profile | 该品种需要关注的季节或事件窗口。 |
| `invalid_profile_use` | product price behavior profile | 明确禁止的 profile 使用方式。 |

### 3.1 信号收集员结构化证据包字段：`signal_collection_contract`

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `signal_collection_contract` | `signal_collector` 输出 / PM 输入 | 信号收集员给投资组合经理的盘前统一结构化预测证据包；不是交易合约，不能包含手数、仓位比例或最终交易动作。 |
| `source_contracts` | `signal_collection_contract` | 被收集的上游分析师 `action_evidence_contract` 引用列表。 |
| `evidence_items` | `signal_collection_contract` | 逐条结构化证据明细，必须保留来源分析师、来源字段和证据含义，不能只写汇总文字。 |
| `product_profile_id` | `signal_collection_contract.evidence_items` | collector 保真传递的分析师商品 profile 来源 ID；不是交易权限。 |
| `product_profile_used` | `signal_collection_contract.evidence_items` | collector 保真传递的 profile 使用状态；collector 不解释、不评分。 |
| `product_profile_analysis_boundary` | `signal_collection_contract.evidence_items` | collector 保真传递的 profile 边界声明；固定为分析证据边界。 |
| `dominant_side` | `signal_collection_contract` | 盘前结构化预测证据汇总后的主方向，如 long、short、flat、mixed；不是交易授权。 |
| `side_consensus` | `signal_collection_contract` | 三类分析师在方向上的一致性或分歧状态。 |
| `trigger_status` | `signal_collection_contract` | 由 `trigger_valid`、`current_trigger_confirmed`、`entry_trigger` 汇总出的当前触发状态；不是交易员执行权限。 |
| `supporting_analysts` | `signal_collection_contract` | 支持 `dominant_side` 的分析师列表。 |
| `opposing_analysts` | `signal_collection_contract` | 反对 `dominant_side` 或给出反向证据的分析师列表。 |
| `neutral_analysts` | `signal_collection_contract` | 无明确方向或只给背景证据的分析师列表。 |
| `evidence_strength` | `signal_collection_contract` | 盘前预测证据强弱汇总，来源于分析师置信度、证据质量和触发状态；不能替代 `opportunity_score`。 |
| `evidence_fusion` | `signal_collection_contract` | 信号收集员保真生成的多维证据融合汇总，包含强弱、时效、一致性、冲突、确认需求和缺失证据；不是 PM score/rank。 |
| `evidence_strength_by_analyst` | `signal_collection_contract.evidence_fusion` | 按 technical、fundamental、commodity_news 分开的证据强度标签。 |
| `evidence_freshness_by_analyst` | `signal_collection_contract.evidence_fusion` | 按分析师分开的证据时效标签。 |
| `evidence_alignment_state` | `signal_collection_contract.evidence_fusion` | 三类预测证据的一致性状态，如 aligned、conflicted、single_source、no_direction。 |
| `direction_alignment` | `signal_collection_contract.evidence_fusion` | `evidence_alignment_state` 的兼容字段；只能表达方向一致性，不表达交易授权。 |
| `cross_analyst_conflicts` | `signal_collection_contract.evidence_fusion` | 三类分析师之间或同日证据内部的结构化冲突列表。 |
| `dominant_opposing_evidence` | `signal_collection_contract.evidence_fusion` | 针对主方向的反向证据摘要；PM 必须解释，Auditor 只审 PM 是否解释。 |
| `multi_evidence_consensus_score` | `signal_collection_contract.evidence_fusion` / PM scorecard | 多维证据一致性评分；只作为 PM `opportunity_score_components` 分项，不能替代最终合约。 |
| `evidence_conflict_level` | `signal_collection_contract` | 盘前预测证据冲突程度汇总，来源于 `current_evidence_conflict`、反向证据和分析师分歧。 |
| `data_quality_flags` | `signal_collection_contract` | 数据新鲜度、缺失、前视风险和质量问题标记。 |
| `setup_types` | `signal_collection_contract` | 从上游分析师证据收集到的 `setup_type` 列表。 |
| `horizon_scope` | `signal_collection_contract` | 汇总后的证据期限范围，来源于 `horizon_class`、`analyst_horizon` 等字段。 |
| `invalidation_summary` | `signal_collection_contract` | 从上游证据汇总出的失效边界和失效条件。 |
| `collector_decision_boundary` | `signal_collection_contract` | 信号收集员权限边界标记，固定表达其无交易权限，例如 `no_trade_authority`。 |

## 4. 基本面分析师字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `primary_business_driver` | 基本面证据 | 主驱动。 |
| `secondary_confirmation` | 基本面证据 | 次级确认链。 |
| `supply_demand_state` | 基本面证据 | 供需状态。 |
| `basis_state` | 基本面证据 | 基差状态。 |
| `inventory_state` | 基本面证据 | 库存状态。 |
| `warehouse_receipt_state` | 基本面证据 | 仓单状态。 |
| `position_flow_state` | 基本面证据 | 持仓 / 资金流状态。 |
| `data_freshness` | 基本面证据 | 数据新鲜度。 |
| `factor_alignment_score` | 基本面证据 | 因子一致性评分。 |
| `data_coverage_score` | 基本面证据 | 数据覆盖度评分。 |
| `requires_fundamental_confirmation` | 跨分析师证据 | 是否需要基本面确认。 |

## 5. 新闻分析师字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `event_type` | 新闻证据 | 事件类型。 |
| `impact_window_days` | 新闻证据 | 影响窗口。 |
| `event_freshness` | 新闻证据 | 事件新鲜度。 |
| `event_relevance` | 新闻证据 | 与品种相关性。 |
| `price_reaction_required` | 新闻证据 | 是否需要价格 / 成交量确认。 |

## 6. 中性、观察、反事实字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `neutral_reason` | 中性证据 | 中性原因。 |
| `neutral_trigger_condition` | 中性证据 | 中性转为可交易所需条件。 |
| `neutral_opportunity_bucket` | 中性证据 | 中性机会分类。 |
| `neutral_watchlist_priority` | 中性证据 | 观察优先级。 |
| `counterfactual_side` | 反事实记录 | 观察方向。 |
| `counterfactual_lots` | 反事实记录 | 假设手数。 |
| `counterfactual_entry_price` | 反事实记录 | 假设入场价。 |
| `counterfactual_results` | 反事实记录 | 反事实结果。 |
| `counterfactual_pnl` | 反事实记录 | 反事实盈亏。 |
| `opportunity_cost_risk` | 中性证据 | 错过机会风险。 |
| `recommended_observation_window` | 中性证据 | 推荐观察窗口。 |
| `accountability_tag` | 中性 / 复盘 | 责任标签。 |
| `similar_past_cases` | 分析师 / 复盘 | 相似历史案例。 |
| `would_change_view_if` | 分析师证据 | 什么条件会改变观点。 |
| `do_not_trade_reason` | 分析师 / 复盘 | 不交易原因。 |

## 7. PM 唯一策略合约字段：`final_action_contract`

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `final_action_contract` | PM 输出 / 推荐 snapshot | 唯一策略交易合约。 |
| `optimal_position_ratio` | 风险评估 / PM 输入 | 风险评估建议仓位比例；不能绕过 PM，最终必须进入 `final_action_contract.target_position_ratio`。 |
| `final_action` | `final_action_contract` | wait、hold、open、open_probe、open_real、add、scale、reduce、exit。 |
| `current_lots` | `final_action_contract` | 动作前当前手数。 |
| `target_lots` | `final_action_contract` | 动作后目标手数。 |
| `lots_delta` | `final_action_contract` | `target_lots - current_lots`。 |
| `target_position_ratio` | `final_action_contract` | 目标仓位比例。 |
| `position_sizing_result` | `position_sizing` 输出 / PM 输入 / `final_action_contract.evidence_used` | 手数计算工具的确定性输出，记录建议 `current_lots`、`target_lots`、`lots_delta`、资金占用、风险约束和计算理由；不是最终交易合约，必须由 PM 写入唯一 `final_action_contract` 后才有交易效力。 |
| `effective_memory_summary` | `decision_memory_retrieval` 输出 / PM 输入 / PM 学习审计 | PM 交易决策类研究记忆的质量优先摘要；记录有效 action-value 数量、剔除或降级原因、空壳历史处理、consumer_scope 和匹配层级。它不是交易授权，不能输出手数或交易动作。 |
| `authority_type` | `final_action_contract` | watchlist_only、exploration_probe、real_budget_entry、scale、reduce、exit、risk_block、risk_exit、not_applicable。 |
| `execution_profile` | `final_action_contract` | breakout、pullback、vwap_confirmed、event_immediate、exit_immediate、hold。它是 PM 写入合约的执行触发 profile，Trader 只能按该字段和盘中数据执行。 |
| `execution_contract` | Trader Phase2 执行摘要 / 执行 payload | 从已审计 `final_action_contract` 抽取的触发/执行配置摘要，不是第二张交易合约。只能包含 `execution_profile`、`trigger_source`、`entry_trigger`、`invalidation`、`valid_until`、`requires_intraday_confirmation`、`can_execute_without_intraday_trigger`、`authority_type`、`max_allowed_margin_ratio`、执行相关 `reason_codes`、`execution_action_value_preference`、`analyst_execution_roles` 等执行规则字段；不得包含 `target_lots`、`lots_delta`、`final_action`、`learning_used`、`opportunity_rank`、`opportunity_score*`、`capital_allocation_reason`、`position_sizing_result` 或 PM 学习解释。 |
| `final_contract_execution_fields` | Trader Phase2 执行学习上下文 / 执行摘要 | 从已审计 `final_action_contract` 抽取的执行必要字段摘要，可用于记录执行来源和复盘追溯；不是第二张交易合约，不能携带 PM 学习、排名、资金部署解释。 |
| `conditional_trigger_authority` | `final_action_contract` | PM 允许 Trader 盘中监控条件触发的受控 probe 权限；不等于当前触发成立，也不等于可无条件成交。 |
| `requires_intraday_confirmation` | `final_action_contract` / 执行字段 | 是否必须等待盘中触发确认；条件 probe 必须为 true。 |
| `can_execute_without_intraday_trigger` | `final_action_contract` / 执行字段 | 是否允许不等盘中触发直接执行；条件 probe 必须为 false，只有合约明确授权的退出或事件立即执行可为 true。 |
| `reason_codes` | `final_action_contract` | PM 决策原因代码。 |
| `holding_period_control` | `final_action_contract.reason_codes` | 合法持仓生命周期解释；表示 PM 因最小持仓期、持仓周期控制或当前持仓保护规则，暂不执行减仓或退出。它只能解释持仓生命周期，不创建交易权限。 |
| `profitable_hold_continuation` | `final_action_contract.reason_codes` | 合法继续持仓解释；表示当前持仓仍处于有利或可继续验证状态，PM 暂不减仓或退出。它只能解释持仓生命周期，不创建交易权限。 |
| `position_lifecycle_trend_hold` | `final_action_contract.reason_codes` | 合法继续持仓解释；表示当前持仓方向仍被生命周期趋势判断支持，PM 暂不减仓或退出。它只能解释持仓生命周期，不创建交易权限。 |
| `hold_exit_action_value_protection` | `final_action_contract.reason_codes` | 合法学习保护解释；表示 PM 已消费 hold/exit 类学习，并据此选择保护当前持仓而非立即减仓或退出。它只能解释 hold/exit 学习未产生仓位变化，不创建交易权限。 |
| `position_matched` | `final_action_contract.reason_codes` / Trader 执行摘要 | 仓位匹配解释；表示当前仓位已经等于 PM 目标仓位，可解释无成交，不能单独解释负向 hold/exit 学习为什么没有导致减仓或退出。 |
| `final_action_semantics` | 公共工具 / 审计摘要 / 复盘摘要 / 研究输入摘要 / Protocol Governor 检查 | 由 `src/tools/common/final_action_semantics.py` 生成的只读语义解释结果；用于统一生命周期、执行权限、盘中结果要求、reason code 分类、学习 lane 匹配、手数变化与 `final_action` 一致性、no-change / rank / learning 解释、active opportunity rejection 和 open transaction blocker，不是第二张合约，不创建交易权限。 |
| `semantic_state` | Auditor / Reviewer / Researcher 只读摘要 | 对同一张 `final_action_contract` 的生命周期解释，如 `conditional_monitor`、`open`、`increase`、`decrease`、`exit`、`ordinary_hold`、`hard_block`；不得包含改手数、改方向或新合约字段。 |
| `scorecard_current_tradeable_probe_seed` | `final_action_contract.reason_codes` / PM 诊断 | PM scorecard 将当前可交易候选释放为受控 probe 的原因代码；只适用于 `probe_candidate` / `tradeable_candidate` 或当前触发已成立的候选，不能用于 `watch_for_trigger` 条件监控。 |
| `evidence_used` | `final_action_contract` | PM 使用的证据摘要。 |
| `pm_fusion_diagnostics` | PM scorecard / `final_action_contract.evidence_used` / Auditor / Reviewer | PM 从 `signal_collection_contract.evidence_fusion` 派生的融合诊断，记录共识分、冲突数量、反向证据数量、缺失证据、确认需求和 score 调整；不是第二合约。 |
| `pm_conflict_resolution` | PM scorecard / `final_action_contract.evidence_used` / Auditor / Reviewer | PM 对主要冲突、反向证据和确认需求的解释结果；Auditor 只审是否存在且自洽，不重新融合证据、不改方向手数。 |
| `fusion_score_adjustment` | `pm_fusion_diagnostics` / `opportunity_score_components` | 由融合证据冲突、缺失和共识形成的 PM 排序分项调整；不能单独创建交易机会。 |
| `risk_controls` | `final_action_contract` | 风险控制项。 |
| `capital_controls` | `final_action_contract` | 资金控制项。 |
| `margin_ratio` | `final_action_contract` / 组合 / 结算 | 目标或当前保证金比例。 |
| `max_allowed_margin_ratio` | `final_action_contract` | 当前动作允许的最高保证金比例。 |
| `contract_hash` | 审计 / 执行 | 被审计的合约哈希。 |
| `single_source_of_trade_truth_remains` | PM 诊断 | 必须等于 `final_action_contract`；只用于审计说明。 |
| `active_opportunity_audit` | PM 推荐 snapshot | PM 对当前机会释放路径的诊断对象；只用于解释候选、阻断和条件监控，不生成第二张交易合约。 |
| `opportunity_scorecard` | `opportunity_ranking` 输出 / PM 输入 / `final_action_contract.evidence_used` | PM 对同一品种多方向候选的结构化评分卡，包含现实证据、历史学习、市场确认、数据质量、风险扣分和 rank；它解释资金优先级，不是交易授权。 |
| `opportunity_score` | PM scorecard / `final_action_contract.evidence_used` / 资金部署 / 复盘评估 | PM 对候选机会的综合评分，用于资金部署排序解释；不是交易授权，不能替代 `target_lots`。 |
| `opportunity_score_components` | PM scorecard / `final_action_contract.evidence_used` / 复盘评估 | `opportunity_score` 的分项来源，如方向支持、setup 质量、市场确认、学习调整和风险扣分。 |
| `positive_learning` | `opportunity_score_components` | 正向 open/add/hold/reduce/exit/conditional_monitor/execution action-value 对机会排序的加分分项；按 episode、作用域、样本、收益、`memory_side_role` 和时间衰减计算，不能单独授权交易。 |
| `negative_learning` | `opportunity_score_components` | `tail_loss_protect` / `negative_revalidate` / `negative_hold_revalidate` 等负向 action-value 对机会排序的扣分分项；只降低 rank，不是永久封杀。 |
| `execution_profile_learning` | `opportunity_score_components` | 同类 `execution_profile` / `trigger_reason` 后续收益对排序的影响；可正可负，只供 PM 排名和执行 profile 选择参考；必须经 PM 写入 `final_action_contract.execution_profile/entry_trigger` 后才影响执行，Trader 不能直接读取学习记录、改手数或方向。 |
| `recent_tail_loss_penalty` | `opportunity_score_components` | 近期同作用域大亏或 tail-loss episode 对排序的惩罚分项，可抵消旧正向学习，防止失效 alpha 继续被抬分；不等于硬风险 block。 |
| `entry_quality_loss_penalty` | `opportunity_score_components` | 亏损开仓 episode 反写到原始入场质量后的 PM 排序扣分分项；来源必须是 Researcher 写入的 `entry_quality_outcome`，只降低资金优先级和真实部署资格，不是硬阻断。 |
| `trigger_quality_positive_bonus` | `opportunity_score_components` | 盈利开仓 episode 反写到原始触发质量后的 PM 排序加分分项；来源必须是 `entry_quality_outcome.positive_entry_episode`，只提高同类触发的资金优先级，不单独生成交易权限。 |
| `trigger_quality_loss_penalty` | `opportunity_score_components` | 亏损开仓 episode 反写到原始触发质量后的 PM 排序扣分分项；使用 `net_trigger_quality_loss_signal`，用于让同类触发在下一轮需要更强确认，不授权 Trader 修改触发或手数。 |
| `capital_priority_score` | PM scorecard / `opportunity_ranking` / `final_action_contract.evidence_used` / 资金部署 | 唯一 `opportunity_rank` 的排序输入分数，综合当前证据、产品级学习、部署资格、触发质量和风险扣分；它不是第二个 rank，也不是交易权限。 |
| `capital_priority_tier` | PM scorecard / `opportunity_ranking` / 资金部署 | 候选的资金优先级层级：tradeable_candidate 高于 probe_candidate，高于 watch_for_trigger，高于 no_opportunity；只用于解释唯一 rank 的排序依据。 |
| `opportunity_rank` | PM scorecard / 主机会审计 / 资金部署 / 复盘评估 | 当日候选机会在 PM 可比较候选中的唯一资金优先级排序；`rank=1` 固定表示当前最值得投入资金的机会。它可以对应小探、正常真实资金或学习验证后的放大资金，但不生成第二张合约。只要进入最终 `final_action_contract` 或其 `evidence_used`，同一合约必须同时写入完整 `capital_deployment`，不得裸 rank 落盘。 |
| `rank_capital_layer_contract` | PG / contract coverage / `final_action_contract` 完整性检查 | 版本级契约名：凡最终 PM 合约出现 `opportunity_rank`，必须同时在同一合约的资金部署事实中写入 `rank_capital_role`、`capital_layer`、`capital_ratio_source`、`rank_reason`。缺任一项是非策略契约错误，不是策略收益诊断。 |
| `rank_capital_priority_real_budget_release` | `final_action_contract.reason_codes` / `final_action_contract.final_entry_authority` / PM 诊断 | PM 最终出口原因代码，表示唯一资金优先级 rank 支持真实资金部署资格。它只能在 `tradeable_candidate`、`rank=1`、`capital_priority_score/tier` 达标、当前开仓证据成立、失效边界存在、无技术反对且硬风险通过时出现；rank 本身仍不是交易权限，不能绕过唯一合约和审计。 |
| `rank_semantics_version` | PM scorecard / `opportunity_ranking` / `final_action_contract.evidence_used` / `capital_deployment` | 唯一 rank 语义版本，固定为 `agentquant.capital_priority_rank.v1`；用于证明 rank 含义已经收束为资金优先级。 |
| `opportunity_rank_meaning` | PM scorecard / `opportunity_ranking` / `final_action_contract.evidence_used` / `capital_deployment` | 固定值 `rank_1_is_current_highest_capital_priority_not_trade_authority`；说明 rank=1 是当前最高资金优先级，不是交易权限。 |
| `rank_is_capital_priority` | PM scorecard / `opportunity_ranking` / `final_action_contract.evidence_used` / `capital_deployment` | 布尔声明：该 rank 表达资金优先级。 |
| `rank_is_not_trade_authority` | PM scorecard / `opportunity_ranking` / `final_action_contract.evidence_used` / `capital_deployment` | 布尔声明：该 rank 不是交易授权，不能绕过 PM 唯一合约、Auditor 审计和 Trader 执行边界。 |
| `rank_capital_role` | PM scorecard / `final_action_contract.evidence_used` / `capital_deployment` | 唯一 rank 对当前资金层级的角色解释。固定取值包括 `best_exploration_probe_candidate`、`best_real_budget_candidate`、`best_alpha_scale_candidate`；它说明 rank=1 是最值得小额探针、正常真实资金还是放大资金占用的候选，不新增第二套 rank。 |
| `capital_layer` | PM scorecard / `final_action_contract.evidence_used` / `capital_deployment` | rank 对应的资金层级。`exploration_probe` 使用既有小探针资金参数，`real_budget_entry` 使用正常真实资金参数，`alpha_scale_entry` 使用强机会放大资金参数；资金层级决定占用多少，rank 只决定同层和全市场资金优先级。 |
| `capital_ratio_source` | PM scorecard / `final_action_contract.evidence_used` / `capital_deployment` | 当前资金层级引用的资金参数来源，例如 `probe_margin_ratio_0.008`、`normal_trade_margin_ratio`、`strong_opportunity_target_margin_ratio`。该字段只解释参数来源，不改参数值。 |
| `rank_reason` | PM scorecard / `final_action_contract.evidence_used` / `capital_deployment` | rank=1 或该候选排名位置的确定性原因摘要。watch/probe 层固定表达按证据、触发、学习、风险质量排序后的最佳小探针候选；真实资金层表达当前证据和产品级学习支持；放大层表达多次正向 alpha、触发质量和回撤约束均达标。 |
| `capital_allocation_reason` | PM scorecard / `final_action_contract.evidence_used` / 资金部署 / 复盘评估 | PM 为什么给该候选资金、监控或暂不分配资金的机器可读理由。凡有资金排名、新开、加仓、扩大或条件监控的最终合约，都必须有该理由。 |
| `fusion_attribution_label` | Reviewer 归因 / Researcher 学习输入 | 复盘员对 PM 融合证据处理结果的只读标签，如 fusion_conflict_handled、fusion_conflict_unresolved、multi_evidence_consensus_supported；只供未来学习，不改当天事实。 |
| `evidence_fusion_attribution` | Researcher learning event | 研究员基于复盘事实写入的未来融合学习上下文；只服务下一交易日分析师校准和 PM 排序，不创建当天交易权限。 |
| `capital_deployment` | `final_action_contract` / PM 资金部署 / 复盘评估 | PM 全市场资金部署结果对象，记录候选是否入选、原目标手数、部署后目标手数、部署手数变化、部署原因和排名；只能解释并回写同一张 `final_action_contract`，不能作为第二交易权限。最终合约出现 rank、新开、加仓、扩大或条件监控时必须原子写入该对象。 |
| `pm_internal_draft` / `pm_scoring_draft` / `pm_ranking_draft` / `pm_capital_deployment_draft` / `pm_contract_submission_draft` / `internal_pm_draft` | PM 内部内存草稿名，非系统事实字段 | PM 可在内部内存分步形成评分、排序、资金部署和提交草稿；这些名字不得进入 DB、artifact、payload、`signal_snapshot` 或跨智能体消息。出现即为系统事实入口越界。 |
| `learning_adjustment_summary` | 分析师证据 / PM scorecard / `final_action_contract.learning_used` / Researcher / 复盘评估 | 历史学习如何影响本次证据、评分或资金排序；不能直接改变 Trader 方向或手数。 |
| `opportunity_state_counts` | PM scorecard / PM 诊断 | 按 `opportunity_state` 统计的分析师证据数量。 |
| `tradeable_opportunity_state_count` | PM scorecard | `tradeable_candidate` 与 `probe_candidate` 的证据数量。 |
| `preferred_state` | PM 主机会审计 | PM 选中的首要机会状态。 |
| `source_analysts` | PM scorecard / PM 主机会审计 | 支持该方向或条件机会的分析师列表。 |
| `conditional_monitor_candidate` | PM 主机会审计 | 单个方向是否满足条件监控候选：`watch_for_trigger + setup_quality_ok + trigger_valid=false + entry_trigger + invalidation_present + 明确方向`。 |
| `conditional_monitor_candidates` | PM 主机会审计 | 满足条件监控候选的方向列表；只供 PM 判断是否生成同一张 `final_action_contract` 的条件 probe 权限。 |
| `conditional_monitor_candidate_count` | PM 主机会审计 | 条件监控候选数量。 |
| `has_monitorable_setup` | PM 当前证据诊断 / `alpha_setup_ev_fusion` | 当前方向是否存在干净的条件监控 setup；它允许 PM 生成条件监控合约，但不等于 `has_tradeable_support`，也不代表当前触发成立。 |
| `watch_for_trigger_semantic_block` | PM 诊断 | `watch_for_trigger` 语义阻止真实开仓。 |
| `watch_for_trigger_semantic_release_block` | PM 诊断 | 释放路径被 `watch_for_trigger` 语义阻止。 |

## 8. Auditor 字段：`audit_verdict`

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `audit_verdict` | Auditor 输出 / recommendation `audit_payload` / signal snapshot auditor 摘要 | 独立审计员对 PM 已签 `final_action_contract` 的审计裁决；只允许 `approve`、`approve_with_warning`、`block`、`require_review`。 |
| `audit_status` | Auditor 输出 / recommendation `audit_payload` / signal snapshot auditor 摘要 | 审计状态，如 `approved`、`blocked`。 |
| `hard_risk_reasons` | Auditor 输出 | 合约字段、保证金、价格、数据质量等硬阻断原因；不能改合约。 |
| `soft_risk_reasons` | Auditor 输出 | 警告或降级说明；不能直接改方向或手数。 |
| `audit_reason_codes` | `audit_verdict` | 审计原因代码。 |
| `warning_message` | 审计 / 执行 | 警告信息。 |
| `audit_payload` | 数据库存储 | 审计 payload 容器；Phase1 保存独立 Auditor 事实，Phase2 execution audit 可保留 `independent_auditor` 摘要，但不能改写 PM 合约。 |
| `intraday_audit` | 早盘执行上下文 | 盘中执行审计 payload。 |

## 9. Trader 执行字段：`execution_result`

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `execution_result` | Trader 输出 / snapshot | 执行结果对象。 |
| `execution_phase` | 执行 / 成交 | 执行阶段。 |
| `slot_datetime` | 盘中决策 | 盘中判断时间。 |
| `cutoff_datetime` | 盘中决策 | 盘中数据截止时间。 |
| `mode` | 盘中决策 | 执行模式。 |
| `trigger_fired` | `execution_result` | 盘中触发是否发生。 |
| `trigger_reason` | 盘中决策 / 执行结果 | 触发或未触发原因。 |
| `executed_action` | `execution_result` | 实际执行动作。 |
| `executed_lots` | `execution_result` / 研究反馈 | 实际成交手数。 |
| `execution_price` | `execution_result` / 成交 | 实际执行价。 |
| `execution_price_candidate` | 盘中决策 | 候选执行价。 |
| `execution_price_basis` | 成交 / 执行 | 执行价格依据。 |
| `base_price` | 推荐 / 成交 | 基准价。 |
| `base_price_source` | 推荐 / 成交 | 基准价来源。 |
| `base_price_date` | 推荐 / 成交 | 基准价日期。 |
| `open_price` | 推荐 / 成交 | 当日开盘价。 |
| `prev_close_price` | 推荐 / 成交 | 前收盘价。 |
| `settle_price` | 成交 / 结算 / 持仓 | 结算价。 |
| `current_settle_price` | 持仓 | 当前结算价。 |
| `contract_multiplier` | 成交 / 持仓 / 结算 | 合约乘数。 |
| `slippage_model` | 推荐 / 成交 | 滑点模型。 |
| `slippage_ticks` | 推荐 / 成交 | 滑点跳数。 |
| `slippage_amount` | 推荐 / 成交 | 滑点金额。 |
| `features` | 盘中决策 | 盘中特征。 |
| `not_executed_reason` | `execution_result` | 合约未执行原因。 |
| `execution_learning_trace` | `execution_result` | 执行学习轨迹，供 Researcher 使用；凡写入学习/记忆的 trace 必须带 `consumer_scope=trader_execution_learning`、`learning_lane=execution` 和 `execution_retrieval_key`，不能作为交易授权。 |

## 10. 成交、持仓、结算、账户字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `value` | 持仓 | 持仓名义价值。 |
| `shares` | 持仓 | 股票股数或期货有符号手数。 |
| `entry_price` | 持仓 / ticker daily pnl | 入场均价。 |
| `entry_date` | 持仓 | 入场日期。 |
| `position_type` | ticker daily pnl | 持仓类型。 |
| `cashflow` | 组合 | 现金流。 |
| `total_assets` | 组合 | 总资产。 |
| `account_equity` | 组合 / 结算 | 账户权益。 |
| `previous_account_equity` | 日结算 | 前一日账户权益。 |
| `current_account_equity` | 日结算 | 当前账户权益。 |
| `cash_available` | 组合 / 日结算 | 可用现金。 |
| `positions` | 组合 | 持仓快照对象。 |
| `positions_snapshot` | 日结算 | 日结算持仓快照。 |
| `margin_used` | 成交 / 组合 | 已用保证金。 |
| `previous_margin` | 日结算 | 前一日保证金。 |
| `current_margin` | 日结算 / 资金部署 | 当前保证金。 |
| `reserved_margin` | 日结算 | 预留保证金。 |
| `margin_as_asset_prev` | 日结算 | 前一日保证金资产口径。 |
| `margin_as_asset_curr` | 日结算 | 当前保证金资产口径。 |
| `margin_rate` | 成交 / final contract / 持仓 | 保证金率。 |
| `margin_delta` | 成交 / 结算 | 保证金变化。 |
| `released_margin` | 成交 / 结算 | 平仓 / 减仓释放保证金。 |
| `post_trade_margin_used` | 成交 / 结算 | 交易后保证金。 |
| `leverage` | 组合 | 杠杆。 |
| `daily_settlement_pnl` | 组合 | 当日日结盈亏。 |
| `daily_pnl` | 日结算 / ticker daily pnl / 成交缓存 | 当日盈亏。 |
| `holding_pnl` | ticker daily pnl | 持仓盈亏。 |
| `new_position_pnl` | ticker daily pnl | 新仓盈亏。 |
| `close_pnl` | ticker daily pnl | 平仓 / 减仓盈亏。 |
| `realized_pnl` | 持仓 / 复盘 / 强平 | 已实现盈亏。 |
| `unrealized_pnl` | 持仓 / 复盘 | 未实现盈亏。 |
| `commission` | 成交 / 日结 / ticker pnl | 手续费。 |
| `deposit` | 日结算 | 入金。 |
| `withdraw` | 日结算 | 出金。 |
| `is_warning` | 日结算 | 风险警告标记。 |
| `is_liquidation` | 日结算 | 是否触发强平。 |
| `booked_in_settlement` | 成交 | 是否已入日结算。 |
| `risk_status` | 组合 | NORMAL、WARNING、LIQUIDATION。 |
| `last_settle_date` | 组合 | 最近结算日期。 |
| `is_settled` | 组合 | 当日是否已结算。 |
| `previous_balance` | 日结算 | 前一日余额。 |
| `current_balance` | 日结算 | 当前余额。 |

## 11. 非策略订单、换月、强平字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `source_type` | 推荐 / 成交 / 研究 | 订单来源唯一字段；取值 `strategy`、`rollover`、`forced_risk`、`counterfactual_*`。策略单只能用 `strategy`；换月与强平必须独立分账，不能污染策略 action-value。 |
| `from_contract` | 换月订单 | 换出合约。 |
| `to_contract` | 换月订单 | 换入合约。 |
| `operation_reason` | 非策略订单 | 换月 / 风控动作原因。 |
| `original_portfolio_id` | 强平记录 | 原组合 ID。 |
| `new_portfolio_id` | 强平记录 | 强平后新组合 ID。 |
| `previous_portfolio_id` | 组合 | 前序组合 ID。 |
| `is_recovery_portfolio` | 组合 | 是否恢复组合。 |
| `settlement_event_id` | 组合 | 关联强平 / 结算事件 ID。 |
| `settlement_date` | 强平记录 | 强平结算日期。 |
| `settlement_reason` | 强平记录 | 强平 / 特殊结算原因。 |
| `pre_settlement_cashflow` | 强平记录 | 强平前现金流。 |
| `pre_settlement_positions` | 强平记录 | 强平前持仓。 |
| `forced_liquidation_details` | 强平记录 | 强平明细。 |
| `post_settlement_cashflow` | 强平记录 | 强平后现金流。 |
| `total_realized_pnl` | 强平记录 | 强平总已实现盈亏。 |
| `total_commission` | 强平 / setup profile | 总手续费。 |
| `remaining_capital` | 强平记录 | 剩余资金。 |
| `is_forced_settlement` | 强平记录 | 是否强制结算。 |

## 12. 复盘归因字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `primary_cause` | 复盘归因 | 主要原因。 |
| `direction_error` | 复盘归因 | 方向错误。 |
| `horizon_error` | 复盘归因 | 期限错误。 |
| `entry_error` | 复盘归因 | 入场错误。 |
| `exit_error` | 复盘归因 | 出场错误。 |
| `position_sizing_error` | 复盘归因 | 仓位大小错误。 |
| `pm_error` | 复盘归因 | PM 决策错误。 |
| `auditor_error` | 复盘归因 | 审计错误。 |
| `trader_error` | 复盘归因 | 执行错误。 |
| `accounting_error` | 复盘归因 | 会计错误。 |
| `missed_factors` | 复盘归因 | 遗漏因子。 |
| `analyst_lessons` | 复盘归因 | 给分析师的教训。 |
| `next_analyst_checks` | 复盘输出 | 下次分析师应检查项。 |
| `promotion_or_demotion_rule` | 复盘 / 研究输出 | setup/action 升降级规则。 |
| `expected_trade_behavior_change` | 复盘 / 研究输出 | 预期交易行为改变。 |
| `feedback_label` | 研究反馈 | 反馈标签。 |
| `outcome_status` | 信号上下文 | 结果状态。 |
| `outcome_label` | 研究样本 / episode | 盈利、亏损、持平、观察等标签。 |
| `evidence_summary` | no-trade / hypothesis / review | 证据摘要。 |
| `pm_reason` | no-trade 记忆 | PM 未释放原因。 |
| `auditor_reason` | no-trade 记忆 | Auditor 阻断原因。 |
| `execution_reason` | no-trade 记忆 | Trader 未执行原因。 |
| `classification` | no-trade 记忆 | 复盘分类。 |
| `candidate_type` | causal review | 候选归因类型。 |
| `rule_validation_status` | causal review | 规则验证状态。 |

## 13. 研究与学习字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `state_key` | action-value / 学习 | 统一状态 key。 |
| `scope_type` | 学习记录 | 学习作用范围类型。 |
| `evidence_signature` | action-value / 学习 | 统一证据组合签名。 |
| `policy_type` | adaptive policy / provisional policy | 策略学习类型；不能作为交易动作。 |
| `policy_multiplier` | adaptive policy / provisional policy | 策略学习倍率；只能影响策略参数，不能覆盖 PM 合约。 |
| `action_name` | action-value | open、add、hold、reduce、exit、execution、conditional_monitor 等历史动作名称。 |
| `action_preference` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 | 唯一动作偏好；PM 评分优先读取 DB 顶层 canonical 字段，payload 只作历史兼容来源。真实正收益 open / add / increase 固定写 `positive_candidate_open`，不能写成 `tail_loss_protect`；保护类偏好只能表达负收益、持仓再验证、退出保护或风险诊断。 |
| `reward_source` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 | 奖励来源；用于区分真实 episode、真实交易、反事实或观察先验。 |
| `evidence_scope` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 | exact、partial、similar、counterfactual；PM 评分优先使用该字段判断学习作用域。 |
| `action_value_lane` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 | action-value 适用动作线，固定为 open、add、hold、reduce、exit、execution、conditional_monitor；不能跨动作线使用。`execution` 是执行反馈线，不能作为 PM 可消费的开仓、持仓、减仓或退出学习。 |
| `consumer_scope` | `alpha_setup_action_value` 顶层 canonical 列 / 学习 payload / 执行学习 trace | 学习记录的唯一消费边界；固定为 `pm_learning`、`analyst_calibration`、`trader_execution_learning`、`research_diagnostics`。PM 只读 `pm_learning`，分析师只读 `analyst_calibration` 安全摘要，Trader 只读 `trader_execution_learning` 执行诊断；`execution` lane 记录不得写成 `pm_learning`。 |
| `learning_lane` | `alpha_setup_action_value` 顶层 canonical 列 / 学习 payload | 学习消费动作线；与 `action_value_lane` 对齐，用于声明该学习服务 open、add、hold、reduce、exit、execution、conditional_monitor、calibration 或 diagnostic。 |
| `memory_side_role` | `alpha_setup_action_value` 顶层 canonical 列 / 学习 payload / PM `learning_used` | 声明该学习记录中 `side` 的角色；固定为 `target_side`、`current_position_side`、`trigger_side`、`historical_sample_side`。新开仓/加仓读取目标方向，减仓/退出/持仓读取当前持仓方向，条件监控读取触发方向；会计师不读取该字段入账。 |
| `product_learning_performance_key` | `alpha_setup_profile.payload_json` / `alpha_setup_action_value.payload_json` / 学习样本 payload | 产品级动态学习身份键，固定记录 ticker、side、setup_type、entry_trigger、evidence_combo、deployment_outcome、entry_quality_outcome、opportunity_rank、opportunity_score 与后续收益；只供下一轮分析师校准、PM 排名和资金部署学习使用，不创建交易权限、不替代 `final_action_contract`。 |
| `performance_scope_key` | `product_learning_performance_key` | 产品级表现聚合键，格式为 ticker、side、setup_type、trigger_key、evidence_combo、deployment_tier；用于把历史表现绑定到同类机会，不得硬编码具体品种好坏。 |
| `deployment_outcome` | `product_learning_performance_key` | PM 资金部署结果摘要，包含 selected_for_capital_deployment、deployment_tier、authority_type、final_action、手数变化、rank、score、capital_allocation_reason；只描述已发生事实，不授权新交易。 |
| `entry_quality_outcome` | `product_learning_performance_key` / `alpha_setup_action_value.payload_json` | Researcher 把已结算开仓 episode 的盈亏结果绑定回原始 setup、entry_trigger、evidence_combo 和 deployment_tier 的未来学习归因；同时写入 `trigger_quality_verdict` 与 `trigger_confirmation_adjustment`，用于下一轮 PM 的入场质量、触发质量和资金优先级校准，不创建交易权限，不修改当天事实。 |
| `entry_quality_loss_signal` | PM `action_value_learning_summary` / `learning_adjustment_summary` | PM 从 `entry_quality_outcome.loss_episode` 聚合出的同作用域入场质量亏损信号；只能进入 scorecard 扣分和 rank 解释，不能单独禁止交易。 |
| `trigger_quality_positive_signal` | PM `action_value_learning_summary` / `learning_adjustment_summary` | PM 从 `entry_quality_outcome.positive_entry_episode` 聚合出的同作用域触发质量正向信号；只能进入 scorecard 加分和 rank 解释，不能单独授权交易。 |
| `trigger_quality_loss_signal` | PM `action_value_learning_summary` / `learning_adjustment_summary` | PM 从 `entry_quality_outcome.tail_loss_episode` 聚合出的同作用域触发质量亏损信号；用于要求更强确认和降低真实资金部署优先级，不改变 Trader 执行权限。 |
| `net_trigger_quality_loss_signal` | PM `action_value_learning_summary` / `learning_adjustment_summary` | 正向触发学习抵消部分负向触发学习后的净触发质量亏损信号；用于防止单次亏损把同类触发压死，同时保留真实亏损对资金部署的降级作用。 |
| `product_learning_calibration_view` | 分析师 `learning_context.alpha_setup_items` / `memory_trace` / `learning_impact_summary` | `product_learning_performance_key` 的分析师安全视图；只保留产品、方向、setup、trigger、证据组合、历史部署层级、历史 PM rank/score 别名和历史收益，用于校准证据质量与待验证问题。该视图不得包含 `authority_type`、`final_action`、`target_lots`、`lots_delta`、`opportunity_rank`、`capital_allocation_reason` 等 PM 权限字段原名。 |
| `historical_pm_rank` | `product_learning_calibration_view` | 历史 PM 排名事实别名，只描述过去样本在当日 PM 队列中的位置；分析师只能用它判断该类证据组合过去是否值得复核，不能输出 `opportunity_rank` 或交易权限。 |
| `historical_pm_score` | `product_learning_calibration_view` | 历史 PM 分数事实别名，只描述过去样本的 PM 评分；分析师只能用它校准证据可靠性，不能生成 PM score、rank、手数或资金部署。 |
| `memory_requirements` | `final_action_contract.learning_used` / `final_action_semantics` / 机制审计 | 由 `src/tools/common/final_action_semantics.py` 根据最终合约生命周期生成的 PM 必读记忆需求；包含 lanes、side roles、是否必须落入 PM 合约，不创建交易权限。 |
| `learning_used.alpha_setup_action_values` | `final_action_contract.learning_used` | PM 最终合约实际声明消费的 action-value。只能包含与当前 `final_action_contract` 的动作生命周期、方向和 `memory_side_role` 匹配的 `pm_learning` 记录；持仓、减仓、退出不能落入不匹配方向的 open 学习或 execution 学习。缺少匹配学习只记录检索为空，不直接禁止交易。 |
| `retrieval_key` | `alpha_setup_action_value` 顶层 canonical 列 / 学习 payload | PM exact state 检索键，格式为 ticker、side、horizon_class、market_regime、setup_type、learning_lane；用于机器检索，不是交易授权。 |
| `fallback_retrieval_key` | `alpha_setup_action_value` 顶层 canonical 列 / 学习 payload | PM fallback 检索键，格式为 ticker、side、horizon_class、learning_lane；exact state 漂移时用于同品种同方向同期限学习消费。 |
| `execution_retrieval_key` | `alpha_setup_action_value` 顶层 canonical 列 / 学习 payload | 执行学习检索键，格式为 ticker、execution_profile、trigger_reason、learning_lane；只能支持执行质量诊断或 PM execution profile 偏好，不产生交易权限，Trader 不直接读取。 |
| `retrieval_match_level` | `final_action_contract.learning_used.alpha_setup_action_values` / 机制审计 | PM 实际消费 action-value 时的命中层级，如 exact_state、same_ticker_side_horizon、same_ticker_side、weak_prior。 |
| `retrieval_match_reason` | `final_action_contract.learning_used.alpha_setup_action_values` / 机制审计 | PM 使用该 action-value 的机器可读原因；用于审计学习是否按固定层级消费。 |
| `counterfactual_reward_weight` | action-value payload | 反事实样本在学习奖励中的权重。 |
| `counterfactual_source_types` | action-value payload | 参与该 action-value 的反事实来源类型。 |
| `sample_count` | 学习记录 | 样本数。 |
| `trade_count` | setup profile | 真实交易样本数。 |
| `no_trade_count` | setup profile | 无交易 / 反事实样本数。 |
| `win_count` | setup profile | 盈利样本数。 |
| `loss_count` | setup profile | 亏损样本数。 |
| `win_rate` | 学习记录 / 评估输出 | 胜率。评估中默认指 `source_type=strategy` 的完成交易对胜率。 |
| `hit_rate` | analyst performance | 命中率。 |
| `reward_sum` | action-value | 奖励总和。 |
| `reward_mean` | action-value | 平均奖励。 |
| `gross_profit` | setup profile | 总盈利。 |
| `gross_loss` | setup profile | 总亏损。 |
| `net_pnl` | 学习 / 复盘 / 评估 | 净盈亏。 |
| `avg_pnl` | 记忆 / 绩效 | 平均盈亏。 |
| `profit_factor` | 绩效 / profile | 盈亏比。 |
| `max_loss` | setup profile | 最大亏损。 |
| `avg_holding_days` | setup profile | 平均持仓天数。 |
| `holding_days` | 样本 / episode | 实际持仓天数。 |
| `max_position_impact` | action-value / profile | 学习结果允许影响仓位的上限。 |
| `last_sample_date` | 学习记录 | 最近样本日期。 |
| `source_event_id` | 学习记录 | 来源事件 ID。 |
| `source_trading_date` | 学习记录 | 来源交易日。 |
| `digest_text` | 分析师学习摘要 | 学习摘要文本。 |
| `accepted` | 学习摘要 | 摘要是否被接受。 |
| `hypothesis_text` | 探索假设 | 假设文本。 |
| `suggested_use` | 探索假设 | 建议用途。 |
| `validation_plan` | hypothesis / research | 验证计划。 |
| `param_key` | 配置学习 | 参数 key。 |
| `learned_value` | 配置学习 | 学到的新值。 |
| `previous_value` | 配置学习 | 修改前值。 |
| `rollback_value` | 配置学习 | 回滚值。 |
| `memory_refs` | research feedback | 使用到的记忆引用。 |
| `policy_refs` | research feedback | 使用到的策略引用。 |
| `pm_effect` | research feedback | PM 影响。 |
| `auditor_effect` | research feedback | Auditor 影响。 |
| `trader_effect` | research feedback | Trader 影响。 |
| `aggregation_scope` | Researcher learning event | 聚合学习写回的样本分组口径；用于说明该学习事件来自单条样本还是机会排序分组，不创建交易权限。 |
| `attribution_scope` | Researcher learning event | 归因对象口径；`representative_episode` 表示融合归因绑定组内确定性代表样本，而不是整组样本本身。 |
| `source_episode_count` | Researcher learning event | 本次聚合学习事件包含的 episode 样本数量。 |
| `representative_recommendation_id` | Researcher learning event | 聚合组中被选作归因代表样本的 PM 推荐 ID；只用于未来学习追溯。 |
| `representative_selection_reason` | Researcher learning event | 代表样本选择原因，如最大亏损样本用于降级、最大盈利样本用于升级、最高评分样本用于观察。 |
| `representative_net_pnl` | Researcher learning event | 代表样本的净盈亏。 |
| `representative_opportunity_score` | Researcher learning event | 代表样本的 PM 机会评分。 |
| `outcome` | research feedback / execution | 结果对象。 |
| `trader_status` | alpha setup sample | Trader 执行状态。 |
| `transaction_count` | research feedback | 成交笔数。 |
| `position_delta_lots` | research feedback | 持仓变化手数。 |
| `result` | alpha setup sample | 结果对象。 |
| `episode_date` | trade episode memory | episode 日期。 |
| `first_seen_at` | trade episode memory | 首次观察时间。 |
| `last_reviewed_at` | 研究 / no-trade 记忆 | 最近复盘时间。 |
| `open_date` | trade episode memory | 开仓日期。 |
| `close_date` | trade episode memory | 平仓日期。 |
| `return_on_notional` | trade episode memory | 名义本金收益率。 |
| `lesson_text` | trade episode memory | 教训文本。 |
| `selected_digest_ids` | learning context budget | 选中的学习摘要 ID。 |
| `selected_chars` | learning context budget | 已选摘要字符数。 |
| `digest_count` | learning context budget | 摘要数量。 |
| `trade_episode_count` | learning context budget | 交易 episode 数。 |
| `hypothesis_count` | learning context budget | 假设数量。 |
| `total_context_chars` | learning context budget | 总上下文字符数。 |
| `dropped_count` | learning context budget | 被丢弃条数。 |
| `max_items` | learning context budget | 最大条数。 |
| `max_chars` | learning context budget | 最大字符数。 |
| `verifier` | learning event log | 验证者。 |
| `event_type` | learning event log | 学习事件类型。 |

## 14. 评估与归因输出字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `overall` | 归因报告 | 全账户完成交易对汇总；可包含 `rollover/forced_risk` 等运营成交，只用于账户路径观察。 |
| `strategy_only_overall` | 归因报告 | 仅 `source_type=strategy` 的完成交易对汇总；策略胜率、策略净 PnL 和策略归因必须使用它。 |
| `trade_pairs` | 归因报告 | 全账户交易对列表，可带 `contains_non_strategy` 标记。 |
| `strategy_only_trade_pairs` | 归因报告 | 仅策略交易对列表；分析师、PM、Auditor 归因和弱边建议必须使用它。 |
| `by_ticker_side` | 归因报告 | 按品种和方向统计的策略交易对表现。 |
| `by_signal_combo` | 归因报告 | 按分析师信号组合统计的策略交易对表现。 |
| `by_pm_risk_gate_decision` | 归因报告 | 按 PM 内部风险门和最终合约决策统计的策略交易对表现；不是独立 Auditor 裁决，也不表示审计员改写 PM 合约。 |
| `by_ticker_side_signal_combo` | 归因报告 | 按品种、方向、分析师信号组合统计的策略交易对表现。 |
| `by_opportunity_learning_component` | 归因报告 | 按 `positive_learning/negative_learning/execution_profile_learning/recent_tail_loss_penalty` 及其正/负/零/缺失 bucket 统计策略交易对表现；只用于评估 PM 学习评分是否有效，不生成交易权限。 |
| `learning_component` | `by_opportunity_learning_component` | 被统计的 PM 学习评分分项名称。 |
| `learning_component_bucket` | `by_opportunity_learning_component` | 该学习分项在开仓推荐中的符号 bucket：positive、negative、zero、missing。 |
| `learning_component_value` | 归因中间字段 / 交易对诊断 | 该学习分项在开仓推荐中的数值；只用于评估，不进入 PM/Trader 交易权限。 |
| `winning_trades` | 评估输出 | 盈利完成交易对数量；默认只统计策略交易对。 |
| `losing_trades` | 评估输出 | 亏损完成交易对数量；默认只统计策略交易对。 |
| `flat_trades` | 评估输出 | 盈亏为零的完成交易对数量；默认只统计策略交易对。 |
| `total_trades` | 评估输出 | 完成交易对数量；默认只统计策略交易对。 |
| `avg_return_per_trade` | 评估输出 | 单笔完成交易对平均收益率；默认只统计策略交易对。 |
| `realized_trade_pnl` | 评估输出 | 已实现完成交易对净盈亏；默认只统计策略交易对。 |
| `unmatched_close_lots` | 评估输出 | 找不到对应开仓的平仓手数。 |
| `inherited_close_lots` | 评估输出 | 子窗口内继承自窗口前持仓的平仓手数。 |
| `rollover_transaction_count` | 评估输出 | 换月运营流水笔数；不能计入策略胜率或 alpha 归因。 |
| `forced_risk_transaction_count` | 评估输出 | 强平/强减风控运营流水笔数；不能计入策略胜率或 alpha 归因。 |
| `operational_transaction_count` | 评估输出 | `source_type != strategy` 的运营流水笔数。 |
| `rollover_summary` | 归因报告 | 换月运营流水摘要。 |
| `forced_risk_summary` | 归因报告 | 强平/强减风控运营流水摘要。 |

## 15. 资金部署字段

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `capital_base` | 资金部署 | 资金基数。 |
| `current_margin_ratio` | 资金部署 | 当前保证金比例。 |
| `target_margin_ratio_min` | 资金部署 | 目标最低保证金比例。 |
| `target_margin_ratio_max` | 资金部署 | 目标最高保证金比例。 |
| `target_margin_abs_min` | 资金部署 | 目标最低保证金金额。 |
| `target_margin_abs_max` | 资金部署 | 目标最高保证金金额。 |
| `underutilization_breach` | 资金部署 | 资金利用不足标记。 |
| `overutilization_breach` | 资金部署 | 资金使用过高标记。 |
| `margin_gap_to_min` | 资金部署 | 距最低目标的保证金缺口。 |
| `capital_allocation_tier` | 资金部署 | 资金分配层级。 |
| `reason_bucket` | 资金部署 | 资金状态原因分桶。 |
| `deployment_plan` | 资金部署 | 资金部署计划。 |

## 16. 静态验证要求

必须保留静态测试：

- 扫描生产代码、schema、配置、评估脚本。
- 运行时业务字段必须属于本文字段表。
- PM、Auditor、Trader、Accountant、Reviewer、Researcher、评估脚本不得读取未登记字段来推导交易、结算、复盘或学习结果。

任何新增字段必须先写入本文，再进入代码；否则视为语义漂移。
