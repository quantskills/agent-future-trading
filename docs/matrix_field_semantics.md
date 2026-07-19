# Matrix Field Semantics

本文是 AgentQuant 的字段语义矩阵。系统从分析、决策、审计、执行、结算、复盘、研究、学习到评估，只允许使用本文定义的字段语义。

核心规则：

- 分析师只输出结构化证据，不输出仓位、保证金、交易授权。
- PM 只输出一张可执行策略合约：`final_action_contract`。
- Auditor 只输出审计结论：`audit_verdict`。
- Trader 只执行审计通过后的 `final_action_contract`，只写执行结果：`execution_result`。
- Accountant 只按成交和结算价写结算事实：`daily_settlement`。
- Reviewer 写复盘归因。
- Researcher 写分动作 action-value 学习。
- 智能体之间只传递通过共享校验的正式结构化契约；prompt、原始 response、内部推理、中间工作状态、隐藏上下文和未验证工具结果不得持久化、跨智能体传递或写入日志/异常。
- 换月、强平、回放、反事实观察不是策略交易，必须用 `source_type != strategy` 分账，不能污染策略 action-value。
- `payload`、`payload_json`、`artifact_json`、`signal_snapshot`、`evidence_json`、`result_json`、`features_json` 等只允许作为结构化容器；容器里的业务字段必须属于本文字段，不能形成第二套语义。
- Protocol Governor 的回测前与每日报告同样受本文约束：读取路径、判定字段和输出字段必须先在本文按精确路径登记；通用 `metadata`、`payload`、字典键和错误详情容器不能成为未登记控制字段的入口。确有新功能且现有字段无法表达时，必须先登记生产者、落点、消费者和语义，再进入 PG 代码。

共享解释器：`src/tools/common/final_action_semantics.py` 是全系统唯一的确定性交易语义状态机。它不调用 LLM，不签合约，不下单，不入账，不写研究；只统一解释分析师证据禁用字段、信号收集边界、`final_action_contract` 全生命周期、`reason_codes` 分类、条件监控、直接执行、普通持有、硬阻断、软降级、未触发、已触发成交、扩大交易、减仓、退出，以及 action-value 的 `action_name -> canonical_action_family -> action_value_lane/learning_lane -> action_preference`。动作 canonical 矩阵见 `docs/matrix_action_canonical.md`；可执行口径以 `final_action_semantics.py` 为准。Protocol Governor 只能对已落地物理字段使用该工具核对动作、手数变化和交易来源语义，不得借该工具复判 PM 的 no-change、rank、learning、active opportunity rejection 等内部形成过程，也不得保留私有 reason code 词表、私有 action-value 动作集合和私有 final_action 推断口径。

分析师差异化分析协议：`src/config/product_price_behavior_profiles.yaml` 是三类分析师的商品价格行为冷启动配置；`src/tools/agent_tools/analysis/analyst_product_price_behavior_profile.py` 是三类分析师共享的确定性读取与格式化工具。它只服务 `technical`、`fundamental`、`commodity_news` 的证据分析，输出 `product_profile_evidence`，用于区分品种价格行为、趋势惯性、波动阈值、产业链确认、季节窗口、假突破风险和适合的 setup。它不调用 LLM，不读研究库，不签合约，不下单，不入账，不写研究；PM 只能从 `signal_collection_contract` 读取它作为证据上下文，Auditor、Trader、Accountant 不直接读取或解释该 profile。

多维证据融合预测协议由 `src/tools/common/evidence_fusion_semantics.py` 的确定性函数固定实现，不设无运行时消费者的 YAML 参数。它只解释技术、基本面、新闻、商品 profile、历史学习上下文和执行反馈形成的预测证据强弱、时效、一致性、冲突、确认需求和缺失证据；不调用 LLM，不签合约，不下单，不入账，不直接写 action-value。Trader 和 Accountant 不读取该工具，也不能用融合证据改执行或结算。

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
| `trading_date` | 所有交易日记录 | 期货逻辑交易日；夜盘物理时间可位于前一日晚上，但不得改用物理日期登记业务事实。 |
| `effective_trade_date` | 推荐 / 执行 | 推荐实际适用和执行的逻辑交易日。 |
| `ticker` | 行情 / 信号 / 研究 | 品种代码。 |
| `sector` | 分析师 / 研究 / 绩效 | 品种所属板块或行业分组。 |
| `underlying_code` | 合约 / 换月 / 推荐 | 标的品种代码。 |
| `contract_code` | 合约 / 成交 / 结算 | 具体期货合约。 |
| `portfolio_id` | 组合 / 成交 / 结算 | 组合 ID。 |
| `reference_portfolio_id` | PM 推荐 | PM 决策使用的最近已结算参考组合 ID；其 `portfolio.trading_date` 必须等于正式 `Prev(T)`，不是 AEC 或 recommendation 的逻辑交易日。 |
| `recommendation_id` | 推荐 / 执行 / 研究 | 关联 PM 推荐记录。 |
| `evidence_pack_id` | 复盘 / Researcher `researcher_llm_notes` / artifact | 已验证研究证据包 ID。 |
| `created_at` | 所有持久化记录 | 真实物理创建时间；不替代逻辑 `trading_date`。 |
| `updated_at` | 可变学习 / 组合记录 | 更新时间。 |
| `last_updated` | 绩效 / 模板记录 | 最后更新时间。 |
| `snapshot_at` | memory history / 快照记录 | 快照生成时间。 |
| `valid_until` | 记忆 / 策略 / 学习 | 有效截止日期。 |
| `active` | 记忆 / 策略 / 学习 | 是否启用。 |
| `status` | 生命周期记录 / Protocol Governor 报告及 `checks[]` | 业务记录沿用既有生命周期状态；PG 报告只使用 `passed`、`failed`，单项检查可额外使用 `skipped`。 |
| `checks` | Protocol Governor 回测前报告 / 每日回测后报告 | PG 已执行检查的有序列表；每项只能包含已登记的 `check_name`、`status`、`violation_codes` 和 `diagnostic_codes`。 |
| `check_name` | Protocol Governor `checks[]` | 检查项稳定名称；只能对应 `agent_pg.md` 已确认的回测前十项或每日回测后七项，不承载交易语义。 |
| `violation_codes` | Protocol Governor `checks[]` | 导致本项检测失败的稳定违规代码列表；只能描述系统断点、字段或动作漂移、职责越权、前视、数据硬缺口、账务或物理事实链断裂。 |
| `diagnostic_codes` | Protocol Governor `checks[]` | 不导致检测失败的稳定诊断代码列表；用于记录合法缺数、合法无交易、可选路径未进入等事实，不评价策略、收益、学习质量或智能体内部机制。 |
| `phase` | `trading_day_phase` / workflow | 当前阶段。 |
| `started_at` | `trading_day_phase` | 阶段开始时间。 |
| `completed_at` | `trading_day_phase` | 阶段完成时间。 |
| `message` | `trading_day_phase` | 阶段说明。 |
| `incomplete_trading_day_phase` | 验收错误码 | 交易日存在推荐、成交、盘中决策或学习记录，但 phase1-4 未全部 completed；必须删除或重跑当天，不能进入策略结论或学习。 |
| `protocol_governor_report` | Protocol Governor 回测前入口 / 每日回测后入口 | PG 唯一报告结构；顶层只能包含 `contract_version`、`source_agent`、`status` 和 `checks`，不落交易库、不创建交易权限。 |
| `contract_coverage_audit` | Protocol Governor 只读版本级闸门 / 回测前验收 | 契约覆盖报告；对关键契约执行 `producer/physical_landing/consumer/role_check/real_path_test/mechanism_doc` 六维证据检查；不读收益、不写 DB、不创建交易权限。pre-backtest readiness 和 daily PG 是独立正式门禁，不是附加 coverage 维度。 |
| `matrix` | `contract_coverage_audit` | 契约覆盖矩阵列表；每行对应一个核心契约。 |
| `matrix_chain` | `contract_coverage_audit` | 当前可执行六维 coverage 行列表；每行由 `contract`、`dimensions` 和 `uncovered_risks` 组成。 |
| `dimensions` | `contract_coverage_audit.matrix_chain[]` | 六维证据映射，只允许 `producer`、`physical_landing`、`consumer`、`role_check`、`real_path_test`、`mechanism_doc`。 |
| `matrix_chain[].dimensions.producer` / `physical_landing` / `consumer` | `contract_coverage_audit` | 可导入真实生产者、正式 artifact/DB 落点和真实生产消费者证据。 |
| `matrix_chain[].dimensions.role_check` / `real_path_test` / `mechanism_doc` | `contract_coverage_audit` | 共享或角色自身校验、同库真实生产链行为测试和正式机制文档证据。 |
| `artifact_phase_boundary` | `contract_coverage_audit.matrix[].contract` / Protocol Governor 只读边界名 | artifact 阶段保存边界；规定 PM、审计员、交易员、会计师、复盘员、研究员 artifact 能保存和禁止保存的字段集合。只用于回测前契约覆盖和系统不变量审计，不是交易字段，不创建合约或交易权限。 |
| `producers` | `contract_coverage_audit.matrix[]` | 该契约的生产路径证据。 |
| `consumers` | `contract_coverage_audit.matrix[]` | 该契约的消费路径证据。 |
| `audits` | `contract_coverage_audit.matrix[]` | 该契约被系统审计或机制审计覆盖的证据。 |
| `tests` | `contract_coverage_audit.matrix[]` | 该契约被真实路径测试覆盖的证据；关键跨智能体边界必须包含字段保真测试，例如 Researcher action-value 进入 PM 后不能丢失 `id/action_preference/canonical_action_family/reward_source/evidence_scope/action_value_lane/learning_lane/reward`。 |
| `uncovered_risks` | `contract_coverage_audit.matrix[]` | 契约覆盖缺口；非空时表示版本级闸门失败，不能进入回测。 |
| `payload` | artifact 外层 | 结构化载荷容器；不能引入未登记语义。 |
| `payload_json` | 数据库存储 | `payload` 序列化结果；不能被当成另一套字段表。 |
| `artifact_json` | signal 表 | 分析师 artifact 序列化容器；正式内容只允许 `metadata.action_evidence_contract` 与 `signal_artifact_metadata` 协议头，不保存完整AnalystSignal或内部metadata。 |
| `artifact_path` | artifact 元数据 | 外部 artifact 路径。 |
| `sha256` | artifact 元数据 | artifact 内容哈希。 |
| `size` | artifact 元数据 | artifact 大小。 |
| `summary_json` | artifact 元数据 | artifact 摘要。 |
| `audit_payload_artifact_path` | audit payload artifact 元数据 | `audit_payload` 外部 artifact 路径。 |
| `audit_payload_sha256` | audit payload artifact 元数据 | `audit_payload` 内容哈希。 |
| `audit_payload_size` | audit payload artifact 元数据 | `audit_payload` 大小。 |
| `audit_payload_summary_json` | audit payload artifact 元数据 | `audit_payload` 摘要。 |
| `llm_prompt_artifact_path` | 历史物理列 | 禁止写入；正式写入口固定为 NULL。 |
| `llm_prompt_sha256` | 历史物理列 | 禁止写入；正式写入口固定为 NULL。 |
| `llm_prompt_size` | 历史物理列 | 禁止写入；正式写入口固定为 0。 |
| `llm_prompt_summary_json` | 历史物理列 | 禁止写入；正式写入口固定为 NULL。 |
| `llm_provider` | 主配置 `llm.provider` / config 运行元数据 / 智能体内部运行时 | 当前实际 LLM 提供方；三类分析师与 Researcher 共用同一选择。不得进入 AEC、SCC、signal artifact、分析师报告或 Researcher 学习 payload。 |
| `llm_model` | 主配置 `llm.model` / config 运行元数据 / 智能体内部运行时 | 当前实际 LLM 模型；必须随所启用的完整 `llm` 配置块同步更新。不得进入 AEC、SCC、signal artifact、分析师报告或 Researcher 学习 payload。 |
| `determinism_mode` | 智能体内部运行时 | 生成模式；不属于AEC、SCC或持久化分析师输出。 |
| `llm_prompt` | signal / transaction 历史物理列 | 禁止持久化或跨智能体传递；不属于 AnalystSignal、FuturesTransaction 或任何正式输出契约，历史非空值不得被消费。 |
| `raw_prompt` | `researcher_llm_notes` 历史物理列 | 禁止持久化；正式写入口只允许空值且无 artifact 元数据。 |
| `raw_response` | `researcher_llm_notes` 历史物理列 | 禁止持久化；正式写入口只允许空值且无 artifact 元数据。 |
| `data_cutoff` | 分析师 / PM / artifact | 数据截止点，用于防未来函数。 |
| `data_usage_summary` | 分析师证据 / 复盘 / 研究 | 本次分析使用的数据来源、日期范围、缺失情况、新鲜度。 |
| `data_usage_summary.ticker` / `trading_date` / `analyst` / `sources` | `action_evidence_contract.data_usage_summary` | AEC 的数据来源身份层；必须与当前 ticker、日期和分析师一致。 |
| `data_usage_summary.sources.*.source` / `dataset` / `available` / `used_in_signal` / `pre_open_only` / `info_cutoff` | 每个 AEC 数据来源记录 | 每个来源的必填物理事实；`available/used_in_signal/pre_open_only` 必须为布尔值。基本面或新闻无当日新增按真实时效与质量表达，不能伪造市场事实。 |
| `product_price_behavior_profiles` | config catalog | 三类分析师商品差异化分析冷启动配置；不随回测自动改写，不创建交易权限。 |
| `product_price_behavior_profile` | 分析师输入上下文 | 单品种价格行为分析框架，定义趋势惯性、波动、确认要求、季节窗口和假突破风险；只用于证据分析。 |
| `product_profile_evidence` | 分析师 `metadata.action_evidence_contract` / `signal_collection_contract.source_contracts[].action_evidence_contract` | 分析师实际使用商品差异化 profile 的结构化痕迹；SCC 来源记录同级不得复制；只能说明证据强调与确认纪律，不能包含手数、保证金、reason code 或最终交易动作。 |
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
| `rank_score_policy` | config catalog / `src/config/rank_score_policy.yaml` / runtime config | 唯一全市场资金 rank 的评分配置。`rank_score` 下的参数组固定与七个 `rank_score_components` 同名：`cold_start_evidence_quality`、`capital_layer_priority`、`open_add_action_value_delta`、`product_setup_trigger_history`、`trigger_execution_quality`、`capital_efficiency`、`conflict_risk_invalidation_penalty`；嵌套权重键与 Python 实际消费的输入字段同名。它不创建交易权限、不改变仓位参数、不覆盖 0.008 probe、20% 总保证金或 0.5 净敞口红线。40 个干净交易日后才允许依据 rank 分层平均收益微调。 |
| `evidence_fusion_semantics` | 公共工具 / 审计摘要 / 复盘摘要 / 研究输入摘要 | 由 `src/tools/common/evidence_fusion_semantics.py` 生成的只读融合语义解释；不签合约、不下单、不入账、不写当天交易事实。 |
| `fusion_evidence` | 分析师 `metadata.action_evidence_contract` / `signal_collection_contract.source_contracts[].action_evidence_contract` | 单个分析师的多维证据融合字段包；SCC 来源记录同级不得复制；说明证据强弱、时效、冲突、缺失和确认需求，不是交易合约。 |
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
| `analyst_execution_profile_missing` | 分析师结构化输出 / Phase1 安全错误码 | technical 或 commodity_news 已声明完整可执行候选且方向、具体触发、canonical 失效边界齐全，但 `entry_timing_signal` 为空或非法；现有 parse-error 重试连续耗尽后才允许安全透传。它不是业务字段，不适用于无方向、无具体触发、无失效边界或仅有研究价值的合法 `no_opportunity`。 |
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
| `morning_price_context` | workflow state / `MorningExecutionBasis` | Router 在交易日截止时间内形成的盘前价格与具体合约事实；不得由 PM 默认或猜测。 |
| `morning_price_context.contract_code` | `MorningExecutionBasis` | Router 从截止点内可见的具体合约行情取得的合约代码；新增风险缺失时不得新增风险。 |
| `morning_price_context.contract_facts` | `MorningExecutionBasis` | `contract_code`、`underlying_code`、`as_of_date`、`source` 及真实可得的交易所、乘数、保证金率、最小变动价位等事实；不得补默认事实。 |
| `pre_open_reference_price_unavailable` | workflow state | Router 已完成正式查询但必需盘前市场事实不可用；触发三个分析师各自生成共享校验通过的中性 AEC，不授权 Collector 造信号。 |
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
| `researcher_llm_notes` | 数据库表名 | 研究员保存已验证 evidence pack 与结构化研究结果的表；`raw_prompt/raw_response` 等历史列禁止写入。 |
| `provisional_policy_state` | 数据库表名 | 临时策略状态表名，不是字段语义。 |
| `learning_context_budget` | 数据库表名 | 学习上下文预算表名，不是字段语义。 |
| `trade_episode_memory` | 数据库表名 | 交易 episode 记忆表名，不是字段语义。 |
| `no_trade_opportunity_memory` | 数据库表名 | 无交易机会记忆表名，不是字段语义。 |
| `exploratory_hypothesis` | 数据库表名 | 探索假设表名，不是字段语义。 |

## 3. 分析师结构化证据字段

三类分析师共用 `validate_action_evidence_contract`。AEC 必填字段固定为：身份与方向 `contract_version/analyst/signal/side/confidence`；机会与触发 `opportunity_type/opportunity_state/setup_type/setup_quality_ok/trigger_valid/current_trigger_confirmed/invalidation_present/entry_trigger/exit_hint`；期限与证据 `horizon_class/expected_horizon_days/market_regime/evidence_quality/evidence_strength/evidence_freshness/confirmation_requirements/missing_evidence/current_evidence_conflict/factor_focus/no_lookahead_status`；结构化来源 `data_usage_summary/learning_scope/product_profile_evidence/fusion_evidence`。文本、布尔、列表、整数和对象类型必须与共享校验器一致；数据不可用中性 AEC 也不例外。

正式 finalization 只使用一套机会状态规则：普通 `Neutral` 保持 `signal=Neutral`，缺具体 `entry_trigger` 或 canonical 失效边界时固定为 `no_opportunity`；只有明确 `counterfactual_side=long/short`、Trader 可用逻辑 T 日15分钟行情观察的具体触发、canonical 失效边界同时存在且当前触发未成立时，`Neutral` 才可为 `watch_for_trigger`，且不得成为 `probe_candidate/tradeable_candidate`。Bullish/Bearish 证据同样必须先具备该具体触发和 canonical 失效边界；数据质量、setup 完整性和机会质量的全部降级完成后，finalization 必须原子写入最终 `opportunity_state/trigger_valid/current_trigger_confirmed`：watch 固定为两项布尔值 false，probe/tradeable 固定为两者同时为 true，`no_opportunity` 固定为两者同时为 false。三个 LLM 入口使用角色化结构化输出模型：正常无方向、无具体触发、无 canonical 失效边界或仅有研究价值时允许 profile 为空并继续形成 `no_opportunity`；只有已声明且字段完整的 technical watch/probe/tradeable 或 news 即时候选漏填、错填 profile 时才触发既有有限 parse-error 重试，shared finalization 同时拒绝任何绕过角色模型的静默降级。`risk_reduction_candidate` 只服务已有持仓的 hold/reduce/exit 风险收缩，不进入上述新增风险映射，也不进入新增风险证据、rank、预算或交易权限，空仓时只保留为研究证据。

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `action_evidence_contract` | 分析师 `metadata` / PM 输入 | 分析师给 PM 的唯一证据契约。 |
| `signal` | `action_evidence_contract` / signal 表 | bullish、bearish、neutral；只表示方向，不是交易授权。 |
| `side` | `action_evidence_contract` / 研究状态 | long、short、flat。 |
| `confidence` | 分析师证据 / 学习输出 | 置信度。 |
| `confidence_score` | 数据库存储 | 数值置信度；运行时统一归一为 `confidence`。 |
| `signal.justification` | signal 历史物理列 / AnalystSignal类型占位 | 正式分析师出口和写入口固定为空；方向、原因和证据只能由AEC表达，不持久化LLM自由文本。 |
| `FuturesRecommendation.justification` / `FuturesTransaction.justification` | 推荐 / 成交 | 由对应正式合约或执行事实生成的可读理由；不能替代结构化字段。 |
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
| `entry_trigger` | 分析师证据 | 可执行 AEC 的机器可读盘中触发说明，由共享 canonical 定义根据 `entry_timing_signal+side` 确定性生成，不持久化 LLM 任意执行文字。technical 的说明必须与 Trader 15分钟算法一致；commodity_news 的 `event_immediate` 必须与即时事件边界一致；fundamental 固定为空。自由分析只能留在现有证据、冲突、确认需求和质量字段。 |
| `entry_timing_signal` | 分析师证据 | 唯一执行时机枚举。technical 可执行证据只允许 `breakout/pullback/vwap_confirmed`，完整 watch 虽未触发也必须填写；commodity_news 只在当前事件已满足即时执行边界时允许 `event_immediate`；fundamental 固定为空。正常 `no_opportunity` 允许为空；已声明且方向、具体触发、canonical 失效边界完整的候选不得为空或使用非法枚举。`range_reversal/trend_breakout/short_timing` 等分析形态只属于 `setup_type/opportunity_type`。 |
| `current_trigger_confirmed` | 分析师证据 / `action_evidence_contract` / 执行证据 | 当前触发已经被明确事实确认；它是 `trigger_valid=true` 的事实来源之一，不能由 `setup_quality_ok` 推出。 |
| `trigger_valid` | 分析师证据 | 当前触发是否已经成立。 |
| `trigger_quality_score` | 分析师证据 | 当前触发强度。 |
| `exit_hint` | 分析师证据 | 退出 / 减仓提示；不是失效边界别名。生产者形成明确失效条件时必须先写入 canonical `invalidation_condition`。 |
| `holding_period_hint` | 分析师证据 | 持仓周期提示。 |
| `invalidation_present` | 分析师证据 | 是否已有明确失效边界；只能由非空 canonical `invalidation_condition`、合法数值 `invalidation_level` 或正数 `atr_stop_distance` 证明，布尔值本身不能自证。 |
| `invalidation_condition` | 分析师 / 复盘 | canonical 失效条件；`would_change_view_if`、`neutral_trigger_condition`、`entry_trigger` 和通用 `exit_hint` 均不得作为其别名。 |
| `invalidation_level` | 分析师 / 执行风控 | 数值失效价位。 |
| `atr_stop_distance` | 分析师 / 执行风控 | ATR 止损距离。 |
| `add_allowed` | 分析师证据 | 证据是否允许加仓讨论；最终仍由 PM 决定。 |
| `evidence_role` | 分析师证据 | 分析师职责的固定结构化角色：technical=`entry_timing`、fundamental=`direction_context`、commodity_news=`event_catalyst`。基本面方向证据不能成为 Trader 执行来源。 |
| `evidence_quality` | 分析师证据 | 证据质量。 |
| `business_quality_score` | 分析师证据 | 业务质量评分。 |
| `tradeability_reason` | 分析师证据 | 为什么可交易或不可交易。 |
| `reward_risk_ratio` | 分析师证据 | 预期收益风险比。 |
| `target_return` | 未采用的旧读取字段 | 当前分析师、AEC、SCC和PM均无合法生产者，不得作为上游必传事实或由Trader伪造。 |
| `factor_focus` | 分析师证据 | 主要因子关注点。 |
| `current_evidence_conflict` | 分析师证据 | 当前冲突证据。 |
| `missing_evidence` | 分析师证据 / SCC 融合诊断 | 当前缺少的证据，只影响证据强度、融合分和机会状态；不得映射为 `data_missing`，不得按数量生成 `critical_data_gap`。 |
| `conflicting_factors` | 分析师证据 | 冲突因子。 |
| `counter_evidence` | 分析师证据 | 反向证据。 |
| `opportunity_type` | 分析师 / no-trade 记忆 | 机会类型。 |
| `opportunity_state` | 分析师证据 | `no_opportunity`、`watch_for_trigger`、`probe_candidate`、`tradeable_candidate`、`risk_reduction_candidate`；`no_opportunity` 可保留方向但不计入新增风险支持；正式 watch 必须同时有可观察具体触发、canonical 失效边界且当前未确认；probe/tradeable 必须是同一完整方案且当前触发已确认。`risk_reduction_candidate` 只支持已有持仓的 hold/reduce/exit，不构成新开仓证据或全局否决票；单个分析师 `no_opportunity` 也不是对其他分析师候选的否决票。 |
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
| `source_contracts` | `signal_collection_contract` | 固定三条被收集的上游 AEC 来源；每条只含 `analyst`、真实 `signal_record_id` 和 `action_evidence_contract`，不得同级复制 profile/fusion。 |
| `signal_record_id` | `signal_collection_contract.source_contracts` | 分析师 `signal` 表记录 ID，唯一合法生产者是workflow 编排层的分析师signal保存入口；正常与数据不可用路径均必须先物理化来源记录。仅用于 Reviewer 和 Researcher 追溯，不创建交易权限。 |
| `evidence_items` | `signal_collection_contract` | 逐条结构化证据明细，必须保留来源分析师、来源字段和证据含义，不能只写汇总文字。 |
| `product_profile_id` | `signal_collection_contract.evidence_items` | collector 保真传递的分析师商品 profile 来源 ID；不是交易权限。 |
| `product_profile_used` | `signal_collection_contract.evidence_items` | collector 保真传递的 profile 使用状态；collector 不解释、不评分。 |
| `product_profile_analysis_boundary` | `signal_collection_contract.evidence_items` | collector 保真传递的 profile 边界声明；固定为分析证据边界。 |
| `dominant_side` | `signal_collection_contract` | 盘前结构化预测证据汇总后的主方向，如 long、short、flat、mixed；不是交易授权。 |
| `side_consensus` | `signal_collection_contract` | 三类分析师在方向上的一致性或分歧状态。 |
| `trigger_status` | `signal_collection_contract` | 仅由 `dominant_side` 对应分析师的合法机会状态与 `trigger_valid/current_trigger_confirmed/entry_trigger` 汇总；主方向为 flat/mixed 或主方向证据全部为 `no_opportunity` 时固定为 `not_applicable`，反方向 watch/已触发证据不得确认或升级主方向；不是交易员执行权限。 |
| `supporting_analysts` | `signal_collection_contract` | 支持 `dominant_side` 的分析师列表。 |
| `opposing_analysts` | `signal_collection_contract` | 反对 `dominant_side` 或给出反向证据的分析师列表。 |
| `neutral_analysts` | `signal_collection_contract` | 无明确方向或只给背景证据的分析师列表。 |
| `evidence_strength` | `signal_collection_contract` | 盘前预测证据强弱汇总，来源于分析师置信度、证据质量和触发状态；不能替代 `opportunity_score`。 |
| `evidence_fusion` | `signal_collection_contract` | 信号收集员保真生成的多维证据融合汇总，包含强弱、时效、一致性、冲突、确认需求和缺失证据；不是 PM score/rank。 |
| `evidence_strength_by_analyst` | `signal_collection_contract.evidence_fusion` | 按 technical、fundamental、commodity_news 分开的证据强度标签。 |
| `evidence_freshness_by_analyst` | `signal_collection_contract.evidence_fusion` | 按分析师分开的证据时效标签。 |
| `evidence_alignment_state` | `signal_collection_contract.evidence_fusion` | 三类预测证据的一致性状态，如 aligned、conflicted、single_source、no_direction。 |
| `cross_analyst_conflicts` | `signal_collection_contract.evidence_fusion` | 三类分析师之间或同日证据内部的结构化冲突列表。 |
| `dominant_opposing_evidence` | `signal_collection_contract.evidence_fusion` | 针对主方向的反向证据摘要；PM 必须解释，Auditor 只审 PM 是否解释。 |
| `multi_evidence_consensus_score` | `signal_collection_contract.evidence_fusion` / PM scorecard | 多维证据一致性评分；只作为 PM `opportunity_score_components` 分项，不能替代最终合约。 |
| `evidence_conflict_level` | `signal_collection_contract` | 盘前预测证据冲突程度汇总，来源于 `current_evidence_conflict`、反向证据和分析师分歧。 |
| `data_quality_flags` | `signal_collection_contract` | Signal Collector从各AEC的 `data_usage_summary.sources.*` 真实可用性、时效、缺失、前视风险和可交易支持事实生成的唯一顶层摘要；不得从不存在的AEC顶层补偿字段读取。 |
| `status` / `flags` / `missing_evidence` / `source` | Workflow、PM、Auditor 共用的 SCC 数据质量摘要 | 由共享 `build_scc_data_quality_summary` 从已校验 SCC 投影；`status` 只允许 `clean/warning/hard_fail`，`source` 固定为 `signal_collection_contract`。只有 `status=hard_fail` 可形成候选硬数据阻断；warning、基本面/新闻无当日新增和 `missing_evidence` 不能冒充 hard fail，不得使用 `quality_status` 等别名。 |
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
| `neutral_trigger_condition` | 中性证据 | 中性观点的观察条件；不能替代正式 `entry_trigger`，也不能单独形成 watch。 |
| `neutral_opportunity_bucket` | 中性证据 | 中性机会分类。 |
| `neutral_watchlist_priority` | 中性证据 | 观察优先级；不创建 `watch_for_trigger`、action preference 或交易权限。 |
| `counterfactual_side` | 反事实记录 | 观察方向。 |
| `counterfactual_lots` | 反事实记录 | 假设手数。 |
| `counterfactual_entry_price` | 反事实记录 | 假设入场价。 |
| `counterfactual_results` | 反事实记录 | 反事实结果。 |
| `counterfactual_pnl` | 反事实记录 | 反事实盈亏。 |
| `opportunity_cost_risk` | 中性证据 | 错过机会风险。 |
| `recommended_observation_window` | 中性证据 | 推荐观察窗口。 |
| `accountability_tag` | 中性 / 复盘 | 责任标签。 |
| `similar_past_cases` | 分析师 / 复盘 | 相似历史案例。 |
| `would_change_view_if` | 分析师证据 | 什么条件会改变观点；不是 `invalidation_condition`，不得证明 `invalidation_present`。 |
| `do_not_trade_reason` | 分析师 / 复盘 | 不交易原因。 |

## 7. PM 唯一策略合约字段：`final_action_contract`

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `final_action_contract` | PM 输出 / 推荐 snapshot | 唯一策略交易合约。 |
| `optimal_position_ratio` | 风险评估 / PM 输入 | 风险评估建议仓位比例；不能绕过 PM，最终必须进入 `final_action_contract.target_position_ratio`。 |
| `final_action` | `final_action_contract` | wait、hold、open、open_probe、open_real、add、scale、reduce、exit。空仓且目标仍为空仓时必须为 `wait`；已有持仓且目标仓位不变时才是 `hold`。 |
| `current_lots` | `final_action_contract` | 动作前当前手数。 |
| `target_lots` | `final_action_contract` | 动作后目标手数。 |
| `lots_delta` | `final_action_contract` | `target_lots - current_lots`。 |
| `target_position_ratio` | `final_action_contract` | 目标仓位比例。 |
| `contract_code` | `final_action_contract.contract_code` | PM Step6只绑定正式输入事实：已有持仓优先绑定该持仓合约；新增风险绑定 Router 截止点内可见的具体合约。缺失时不得新增风险，禁止默认、猜测或品种代码代替具体合约；Auditor、Trader、Reviewer和Researcher只读消费。 |
| `setup_type` | `final_action_contract.setup_type` | 分析师在AEC生产原始setup；PM Step6只从SCC中选择与最终方向、动作及Step4学习作用域一致的最终setup。Trader、Reviewer和Researcher只读消费，不得重选。 |
| `horizon_class` | `final_action_contract.horizon_class` | 分析师在AEC生产原始期限类别；PM Step6只从SCC中选择与最终方向、动作及Step4学习作用域一致的最终值。Trader、Reviewer和Researcher只读消费。 |
| `expected_horizon_days` | `final_action_contract.expected_horizon_days` | 分析师在AEC生产原始天数；PM Step6只从与最终方向和 `horizon_class` 一致的真实AEC中选择，缺失时保持缺失。Trader、Reviewer和Researcher只读消费。 |
| `market_regime` | `final_action_contract.market_regime` | 分析师在AEC生产原始市场状态；PM Step6只从SCC中选择与最终方向和Step4学习检索作用域一致的最终值。Trader、Reviewer、Researcher和下一交易日PM学习只读消费。 |
| `invalidation_level` | `final_action_contract.invalidation_level` | 被 PM Step6 选为唯一执行证据的 AEC 所生产的数值失效价位；只在真实数值存在时与该 AEC 的触发、profile 和来源一并写入，禁止默认值、反方向填充或跨分析师拼接。Auditor、Trader、Reviewer和Researcher只读消费。 |
| `atr_stop_distance` | `final_action_contract.atr_stop_distance` | 被 PM Step6 选为唯一执行证据的 AEC 所生产的 ATR 止损距离；只在真实生产时与同一 AEC 的 setup、触发和失效条件一并写入，禁止默认值或从另一分析师借用。Trader、Reviewer和Researcher只读消费。 |
| `position_sizing_result` | `position_sizing` 输出 / PM 输入 / `final_action_contract.evidence_used` | 手数计算工具的确定性输出，记录建议 `current_lots`、`target_lots`、`lots_delta`、资金占用、风险约束和计算理由；不是最终交易合约，必须由 PM 写入唯一 `final_action_contract` 后才有交易效力。 |
| `effective_memory_summary` | `decision_memory_retrieval` 输出 / PM 输入 / `final_action_contract.learning_used` 摘要 | PM 交易决策类研究记忆的质量优先摘要；记录有效 action-value 数量、剔除或降级原因、空壳历史处理、consumer_scope 和匹配层级。它不是交易授权，不能输出手数或交易动作；Auditor 不消费或复审该摘要。 |
| `authority_type` | `final_action_contract` | watchlist_only、exploration_probe、real_budget_entry、scale、reduce、exit、risk_block、risk_exit、not_applicable。 |
| `execution_profile` | `final_action_contract` | `breakout/pullback/vwap_confirmed/event_immediate/exit_immediate/hold`。新增风险时 PM 只复制唯一被选执行 AEC 的 `entry_timing_signal`，不得从 `entry_trigger/setup_type/opportunity_type` 自由文本推断，也不得默认 `breakout`。执行 action-value 可保留为现有建议摘要，但不得改写顶层 profile、触发、来源或权限。 |
| `trigger_source` | `final_action_contract` / Trader 执行摘要 | 唯一顶层执行触发来源：technical 的 `breakout` 使用 `technical_breakout`，`pullback/vwap_confirmed` 使用 `technical_pullback`，commodity_news 的 `event_immediate` 使用 `commodity_news_event`；非新增风险使用 `none` 或 `position_lifecycle`。fundamental 不得成为执行来源；`execution_action_value_*` 只允许存在于既有 `execution_action_value_preference` 建议摘要，不能替代顶层来源。 |
| `entry_trigger` | `final_action_contract` / Trader 执行摘要 | 新增风险时只复制唯一被选 AEC 的 canonical `entry_trigger`；条件 FAC 必须为 Trader 在逻辑 T 日可观察的具体触发，禁止读取其他分析师或补造默认触发。 |
| `invalidation` | `final_action_contract` / Trader 执行摘要 | 新增风险时只复制唯一被选 AEC 的 canonical `invalidation_condition`；可与同一 AEC 的 `invalidation_level`、`atr_stop_distance` 共同证明失效边界，禁止使用 `exit_hint`、`would_change_view_if` 或其他别名。 |
| `execution_contract` | Trader Phase2 执行摘要 / 执行 payload | 从已审计 `final_action_contract` 白名单抽取的执行摘要，不是第二张交易合约。只能包含 `contract_code`、`setup_type`、`horizon_class`、`expected_horizon_days`、`market_regime`、`execution_profile`、`trigger_source`、`entry_trigger`、`invalidation`、`invalidation_level`、`atr_stop_distance`、`valid_until`、`requires_intraday_confirmation`、`can_execute_without_intraday_trigger`、`authority_type`、`max_allowed_margin_ratio`、执行相关 `reason_codes` 和 `execution_action_value_preference`；不得包含完整AEC、`target_lots`、`lots_delta`、`final_action`、`learning_used`、`opportunity_rank`、`opportunity_score*`、`capital_allocation_reason`、`position_sizing_result` 或 PM 学习解释。 |
| `final_contract_execution_fields` | Trader Phase2 执行学习上下文 / 执行摘要 | 从已审计 `final_action_contract` 抽取的执行必要字段摘要，可用于记录执行来源和复盘追溯；不是第二张交易合约，不能携带 PM 学习、排名、资金部署解释。 |
| `conditional_trigger_authority` | `final_action_contract` | PM 允许 Trader 盘中监控条件触发的受控 probe 权限；不等于当前触发成立，也不等于可无条件成交。合法 watch 有资格进入原 Step5 新增风险排名，但只有实际获选并形成非零条件目标的 FAC 才置为 true；已审计通过后必须先由 Trader 用15分钟线写触发/未触发事实，触发后再用1分钟线执行。 |
| `requires_intraday_confirmation` | `final_action_contract` / 执行字段 | 是否必须等待盘中触发确认；条件 probe 必须为 true。 |
| `can_execute_without_intraday_trigger` | `final_action_contract` / 执行字段 | 是否允许 Trader 不再用15分钟线复判触发而直接使用合法1分钟线执行。条件 watch/probe 必须为 false；退出路径，或具备 `trigger_valid=true`、`current_trigger_confirmed=true`、canonical 失效边界且已获 PM 资金授权和 Auditor 放行的 probe/tradeable FAC 可为 true，适用于 breakout、pullback、vwap_confirmed、event_immediate 等合法 profile。 |
| `reason_codes` | `final_action_contract` | PM 决策原因代码。 |
| `holding_period_control` | `final_action_contract.reason_codes` | 合法持仓生命周期解释；表示 PM 因最小持仓期、持仓周期控制或当前持仓保护规则，暂不执行减仓或退出。它只能解释持仓生命周期，不创建交易权限。 |
| `profitable_hold_continuation` | `final_action_contract.reason_codes` | 合法继续持仓解释；表示当前持仓仍处于有利或可继续验证状态，PM 暂不减仓或退出。它只能解释持仓生命周期，不创建交易权限。 |
| `position_lifecycle_trend_hold` | `final_action_contract.reason_codes` | 合法继续持仓解释；表示当前持仓方向仍被生命周期趋势判断支持，PM 暂不减仓或退出。它只能解释持仓生命周期，不创建交易权限。 |
| `hold_exit_action_value_protection` | `final_action_contract.reason_codes` | 合法学习保护解释；表示 PM 已消费 hold/exit 类学习，并据此选择保护当前持仓而非立即减仓或退出。它只能解释 hold/exit 学习未产生仓位变化，不创建交易权限。 |
| `position_matched` | `final_action_contract.reason_codes` / Trader 执行摘要 | 仓位匹配解释；表示当前仓位已经等于 PM 目标仓位，可解释无成交，不能单独解释负向 hold/exit 学习为什么没有导致减仓或退出。 |
| `final_action_semantics` | 公共工具 / 审计摘要 / 复盘摘要 / 研究输入摘要 / Protocol Governor 检查 | 由 `src/tools/common/final_action_semantics.py` 生成的只读语义解释结果；用于统一生命周期、执行权限、盘中结果要求、reason code 分类、学习 lane 匹配、手数变化与 `final_action` 一致性、no-change / rank / learning 解释和 open transaction blocker，不是第二张合约，不创建交易权限。 |
| `semantic_state` | Auditor / Reviewer / Researcher 只读摘要 | 对同一张 `final_action_contract` 的生命周期解释，如 `conditional_monitor`、`open`、`increase`、`decrease`、`exit`、`ordinary_hold`、`hard_block`；不得包含改手数、改方向或新合约字段。 |
| `scorecard_current_tradeable_probe_seed` | `final_action_contract.reason_codes` / PM 诊断 | PM scorecard 将当前可交易候选释放为受控 probe 的原因代码；只适用于 `probe_candidate` / `tradeable_candidate` 或当前触发已成立的候选，不能用于 `watch_for_trigger` 条件监控。 |
| `evidence_used` | `final_action_contract` | PM 使用的证据摘要。 |
| `pm_fusion_diagnostics` | PM scorecard / `final_action_contract.evidence_used` / Reviewer / Researcher | PM 从 `signal_collection_contract.evidence_fusion` 派生的融合诊断，记录共识分、冲突数量、反向证据数量、缺失证据、确认需求和 score 调整；不是第二合约。Auditor 不消费或复审该诊断。 |
| `pm_conflict_resolution` | PM scorecard / `final_action_contract.evidence_used` / Reviewer / Researcher | PM 对主要冲突、反向证据和确认需求的解释结果；只供后续复盘与研究归因，Auditor 不重新融合证据、不复审 PM 解释。 |
| `fusion_score_adjustment` | `pm_fusion_diagnostics` / `opportunity_score_components` | 由融合证据冲突、缺失和共识形成的 PM 排序分项调整；不能单独创建交易机会。 |
| `risk_controls` | `final_action_contract` | 风险控制项。 |
| `capital_controls` | `final_action_contract` | 资金控制项。 |
| `margin_ratio` | `final_action_contract` / 组合 / 结算 | 目标或当前保证金比例。 |
| `max_allowed_margin_ratio` | `final_action_contract` | 当前动作允许的最高保证金比例。 |
| `contract_hash` | 审计 / 执行 | 被审计的合约哈希。 |
| `single_source_of_trade_truth_remains` | PM 诊断 | 必须等于 `final_action_contract`；只用于审计说明。 |
| `opportunity_scorecard` | `pm_ticker_side_selection` 输出 / PM 输入 / `final_action_contract.evidence_used` | PM 对同一品种多方向候选的结构化评分卡，包含现实证据、历史学习、市场确认、数据质量、风险扣分和单品种方向优先级；它不生成最终资金 rank，也不是交易授权。 |
| `opportunity_score` | PM scorecard / `final_action_contract.evidence_used` / 资金部署 / 复盘评估 | PM 对候选机会的综合评分，用于资金部署排序解释；不是交易授权，不能替代 `target_lots`。 |
| `opportunity_score_components` | PM scorecard / `final_action_contract.evidence_used` / 复盘评估 | `opportunity_score` 的分项来源，如方向支持、setup 质量、市场确认、学习调整和风险扣分。 |
| `directional_support` / `tradeable_state` / `business_quality` / `setup_quality` / `market_confirmation` / `fusion_consensus` | `opportunity_score_components` | PM scorecard 的当日结构化证据分项；这些字段只参与 `opportunity_score` 和 `rank_score_input_components.cold_start_evidence_quality`，不直接生成资金 rank 或交易权限。 |
| `positive_learning` | `opportunity_score_components` | 正向 action-value 对对应生命周期决策口的加分分项。具体 family/lane/preference 匹配口径见 `docs/matrix_action_canonical.md`；该字段不能单独授权交易。 |
| `negative_learning` | `opportunity_score_components` | 负向 action-value 对对应生命周期决策口的扣分分项。具体保护偏向和生命周期匹配口径见 `docs/matrix_action_canonical.md`；该字段不是永久封杀。 |
| `observe_action_value` | action-value / PG 审计 | `canonical_action_family=observe` 且 `action_value_lane=learning_lane=hold` 的观察事实。该字段的 preference 合法集合由 `docs/matrix_action_canonical.md` 固定。 |
| `execution_profile_learning` | `opportunity_score_components` | 同类 `execution_profile` / `trigger_reason` 后续收益对 trigger/profile 选择的影响；可正可负，但不能直接生成或抬高新资金 rank。必须经 PM 写入 `final_action_contract.execution_profile/entry_trigger` 后才影响执行，Trader 不能直接读取学习记录、改手数或方向。 |
| `recent_tail_loss_penalty` | `opportunity_score_components` | 近期同作用域大亏或 tail-loss episode 对排序的惩罚分项，可抵消旧正向学习，防止失效 alpha 继续被抬分；不等于硬风险 block。 |
| `entry_quality_loss_penalty` | `opportunity_score_components` | 亏损开仓 episode 反写到原始入场质量后的 PM 排序扣分分项；来源必须是 Researcher 写入的 `entry_quality_outcome`，只降低资金优先级和真实部署资格，不是硬阻断。 |
| `trigger_quality_positive_bonus` | `opportunity_score_components` | 盈利开仓 episode 反写到原始触发质量后的 PM 排序加分分项；来源必须是 `entry_quality_outcome.positive_entry_episode`，只提高同类触发的资金优先级，不单独生成交易权限。 |
| `trigger_quality_loss_penalty` | `opportunity_score_components` | 亏损开仓 episode 反写到原始触发质量后的 PM 排序扣分分项；使用 `net_trigger_quality_loss_signal`，用于让同类触发在下一轮需要更强确认，不授权 Trader 修改触发或手数。 |
| `action_value_learning_summary` | PM Step4 `pm_signal_fusion` / PM Step5 rank 输入 | PM 对已通过 `canonical_action_family -> action_value_lane/learning_lane -> action_preference` 校验的 action-value 所做的同生命周期聚合摘要。它不是新动作语义，不得保存未通过 canonical 校验的记录。 |
| `positive_learning_signal` / `negative_learning_signal` | `action_value_learning_summary` / `rank_score_policy.rank_score.open_add_action_value_delta` | canonical open/add 正向候选偏好与保护偏好的归一化聚合强度。配置中的同名参数是各信号进入 `open_add_action_value_delta` 的乘数；禁止使用 `positive_signal`、`negative_signal` 或字符串前缀推断作为别名。 |
| `execution_profile_signal` | `action_value_learning_summary` | canonical execution family 的聚合强度，只进入 execution/profile 解释；不得进入 `rank_score`。 |
| `recent_tail_loss_signal` | `action_value_learning_summary` / `rank_score_policy.rank_score.open_add_action_value_delta` | canonical 保护偏好、tail-loss 计数和真实亏损阈值形成的近期尾损强度；配置中的同名参数是其进入 `open_add_action_value_delta` 的乘数。 |
| `positive_count` / `negative_count` / `exact_real_count` / `episode_count` / `strongest_positive` / `strongest_negative` / `used_lanes` / `ignored_lanes` | `action_value_learning_summary` / lifecycle learning trace | action-value 聚合的样本计数、最强记录摘要和 canonical lane 路由事实；只作可解释性与生命周期检查，不单独授权交易。 |
| `alpha_profile_adjustment` | `opportunity_score_components` / `rank_score_policy.rank_score.product_setup_trigger_history` | 产品/setup/trigger 历史 profile 对候选质量的净修正；配置中的同名参数是其进入 `product_setup_trigger_history` 的乘数。 |
| `market_conflict_penalty` / `critical_data_gap_penalty` / `fundamental_gap_penalty` | `opportunity_score_components` / `rank_score_policy.rank_score.conflict_risk_invalidation_penalty` | 市场确认冲突和共享 SCC `hard_fail` 的确定性扣分入口；基本面/新闻无当日新增及普通证据缺口通过 AEC 证据质量、时效和融合分反映，不能触发 `critical_data_gap`，也不得建立第二套基本面硬阻断。配置权重和 rank 公式保持不变。 |
| `gating_failures` | PM scorecard / `rank_score_policy.rank_score.conflict_risk_invalidation_penalty` | 当前候选未满足条件的固定原因列表；rank 只按 `gating_failure_penalty_per_item` 和 `gating_failure_penalty_cap` 计算有限扣分，不把它改写为 canonical action 或硬风险事实。 |
| `rank_score` | PM 第 5 步工具 `pm_full_market_capital_deployment` / `rank_score_policy` | 唯一全市场资金 rank 的直接排序分数。它由冷启动证据质量、资金层级资格、open/add action-value 学习修正、产品/setup/trigger 历史表现、trigger/execution 质量、资金效率和冲突/风险/失效边界惩罚共同组成；权重来自 `src/config/rank_score_policy.yaml`，只能由 `pm_full_market_capital_deployment` 在全市场候选池中生成。PM scorecard / `pm_signal_fusion` 只能提供输入组件，不能写最终 `rank_score`。 |
| `rank_score_input_components` | PM Step4 `pm_signal_fusion` / PM Step5 `pm_full_market_capital_deployment` | Step4 交给 Step5 的 rank 原始输入对象；当前只保存不含学习、profile 和风险重复项的 `cold_start_evidence_quality`。它不是最终 `rank_input_components`，不得包含 `final_rank_score_generated_by` 或预生成 rank。 |
| `rank_score_components` | PM Step5 `pm_full_market_capital_deployment` / `rank_input_components` | `rank_score` 的确定性拆解，固定包含 `cold_start_evidence_quality`、`capital_layer_priority`、`open_add_action_value_delta`、`product_setup_trigger_history`、`trigger_execution_quality`、`capital_efficiency`、`conflict_risk_invalidation_penalty`；字段名与 `rank_score_policy.rank_score` 的七个参数组一一对应，用于证明强化学习和当前证据真实进入排序。 |
| `cold_start_evidence_quality` | `rank_score_input_components` / `rank_score_components` / `rank_score_policy.rank_score` | Step4 输入值是仅由当日方向支持、可交易状态、业务/setup 质量、置信度、市场确认和融合共识形成的原始证据质量；Step5 乘以 catalog 同名参数后写入 `rank_score_components`。不得混入 action-value、profile 历史或风险扣分。 |
| `capital_layer_priority` / `open_add_action_value_delta` / `product_setup_trigger_history` / `trigger_execution_quality` / `capital_efficiency` / `conflict_risk_invalidation_penalty` | `rank_score_components` / `rank_score_policy.rank_score` | Step5 的其余六个唯一 rank 分项；catalog 同名参数组分别控制候选层级积分、canonical open/add 学习、产品历史、触发质量、资金效率和冲突风险扣分。`capital_layer_priority` 只加减 rank 分数，不生成或改变 `capital_layer`、`final_entry_authority`。配置名、Python 读取名和最终 trace 名必须一致。 |
| `max_abs_delta` / `gating_failure_penalty_per_item` / `gating_failure_penalty_cap` / `enabled` / `max_bonus` | `rank_score_policy.rank_score` 子参数 | 分别限定 open/add 学习净修正绝对值、单项 gating failure 扣分、gating failure 总扣分、资金效率修正是否启用和资金效率最高加分。它们必须由 `pm_full_market_capital_deployment` 以相同参数名读取；仅改变对应 rank 分项，不创建交易权限。 |
| `alpha_scale_eligible` | PM Step5 `pm_full_market_capital_deployment` 内部 scorecard 状态 | 放大资金层资格的唯一布尔字段。仅当 `final_entry_authority.authority_type=real_budget_entry`、候选为 `tradeable_candidate`，且既有 `capital_utilization_target` 已按配置确认 `high_quality_memory=true` 和 `target_mode=alpha_release_boost/alpha_release_max_boost` 时生成；rank 分数本身不得生成该资格。禁止使用 `alpha_scale_candidate`、`mature_alpha_candidate`、`repeated_positive_alpha` 或 `strong_opportunity_alpha_scale_candidate` 别名。 |
| `capital_priority_score` | PM 第 5 步工具 `pm_full_market_capital_deployment` | 全市场 `opportunity_rank` 的排序输入分数，综合当前证据、产品级学习、部署资格、触发质量和风险扣分；它不是第二个 rank，也不是交易权限。PM 第 3 步只能写 `candidate_quality`，不能写该字段。 |
| `capital_priority_tier` | PM 第 5 步工具 `pm_full_market_capital_deployment` | 候选的资金优先级层级：tradeable_candidate 高于 probe_candidate，高于 watch_for_trigger，高于 no_opportunity；只用于解释全市场资金 rank 的排序依据。PM 第 3 步只能写 `candidate_layer_hint`，不能写该字段。 |
| `analyst_direction_evidence` / `direction_evidence_strength` | `pm_signal_fusion` | 分析师结构化方向证据和候选质量摘要，用于保留 `signal_collector` 汇总后的方向、证据强弱、setup、trigger、invalidation、冲突和学习校准输入；它不是 PM 内部方向优先级，不得写 `side_priority`，不得进入 Trader/Accountant 权限链。 |
| `side_priority` / `ticker_side_priority` | `pm_ticker_side_selection` | PM 第 2 步单品种代表方向：无真实冲突时 SCC 唯一 `dominant_side=long/short` 对应方向固定为 1、反方向为 null，并同步成为 `preferred_side`；flat、mixed、真实 conflicted 或两侧无法区分时两侧均为 null、`preferred_side=flat`。唯一生成口是 `pm_ticker_side_selection`；不得读取学习或机会分重选方向，不得由 `pm_signal_fusion`/Collector 写入，也不是最终 `opportunity_rank`。 |
| `side_priority_semantics_version` | `pm_ticker_side_selection` | 单品种方向优先级语义版本，固定为 `agentquant.ticker_side_priority.v1`。 |
| `side_priority_is_not_capital_rank` | `pm_ticker_side_selection` | 布尔声明：单品种方向优先级不是全市场资金 rank。 |
| `opportunity_rank` | PM 全市场资金部署工具 `pm_full_market_capital_deployment` / `final_action_contract.evidence_used` / `capital_deployment` / 复盘评估 | 当日所有实际增加风险的候选进入同一个全市场资金候选池后的唯一资金优先级排序；包括 `current_lots=0` 且 `target_lots!=0` 的新开仓，以及同方向且 `abs(target_lots)>abs(current_lots)` 的 `add/scale`。`rank=1` 固定表示当天全市场最值得优先占用新增风险资金的产品机会。它可以对应小探、正常真实资金或学习验证后的放大资金，但不生成第二张合约。PM scorecard 的单品种方向排序不得写入该字段；`wait/hold/reduce/exit`、当前反转退出腿和不增加风险的条件监控不得生成该字段，反转只有在旧方向退出后形成新的反向开仓合约时才重新排名。 |
| `rank_capital_layer_contract` | PM self-check / pre-backtest contract coverage / `final_action_contract` 完整性检查 | 版本级契约名：凡最终 PM 合约出现 `opportunity_rank`，必须同时在同一合约的资金部署事实中写入 `rank_capital_role`、`capital_layer`、`capital_ratio_source`、`rank_reason`、`rank_input_components`、`lifecycle_learning_trace`、`learning_impact_delta`、`rank_source`、`rank_scope`、`capital_rank_generated_by`。缺任一项由 PM 自身检查和回测前契约覆盖处理，daily PG 不复查。 |
| `rank_capital_priority_real_budget_release` | `final_action_contract.reason_codes` / `final_action_contract.final_entry_authority` / PM 诊断 | PM 最终出口原因代码，表示唯一资金优先级 rank 支持真实资金部署资格。它只能在 `tradeable_candidate`、`rank=1`、`capital_priority_score/tier` 达标、当前新增风险证据成立、失效边界存在、无技术反对且硬风险通过时出现；rank 本身仍不是交易权限，不能绕过唯一合约和审计。 |
| `rank_semantics_version` | `final_action_contract.evidence_used` / `capital_deployment` | 唯一全市场资金 rank 语义版本，固定为 `agentquant.capital_priority_rank.v1`；用于证明 rank 含义已经收束为资金优先级。 |
| `opportunity_rank_meaning` | `final_action_contract.evidence_used` / `capital_deployment` | 固定值 `rank_1_is_current_highest_capital_priority_not_trade_authority`；说明 rank=1 是当前最高资金优先级，不是交易权限。 |
| `rank_is_capital_priority` | `final_action_contract.evidence_used` / `capital_deployment` | 布尔声明：该 rank 表达全市场资金优先级。 |
| `rank_is_not_trade_authority` | `final_action_contract.evidence_used` / `capital_deployment` | 布尔声明：该 rank 不是交易授权，不能绕过 PM 唯一合约、Auditor 审计和 Trader 执行边界。 |
| `rank_source` | `final_action_contract.evidence_used` / `capital_deployment` | 最终资金 rank 来源，固定为 `full_market_capital_deployment`；PM self-check 与回测前契约测试必须拒绝 PM scorecard 局部 rank 泄漏到最终合约，daily PG 不复查 rank 形成过程。 |
| `rank_scope` | `final_action_contract.evidence_used` / `capital_deployment` | 最终资金 rank 范围，固定为 `daily_full_market_capital_pool`；说明同一交易日所有产品代表候选同池排序。 |
| `capital_rank_generated_by` | `final_action_contract.evidence_used` / `capital_deployment` | 最终资金 rank 生成入口，固定为 `pm_full_market_capital_deployment`。 |
| `rank_capital_role` | `final_action_contract.evidence_used` / `capital_deployment` | 唯一 rank 对当前资金层级的角色解释。固定取值包括 `best_exploration_probe_candidate`、`best_real_budget_candidate`、`best_alpha_scale_candidate`；它说明 rank=1 是最值得小额探针、正常真实资金还是放大资金占用的候选，不新增第二套 rank。 |
| `capital_layer` | `final_action_contract.evidence_used` / `capital_deployment` | rank 对应的既定资金层级。它只能从 rank 前已形成的 `final_entry_authority.authority_type` 和既有 alpha-release 资格映射：`exploration_probe` 使用既有小探针资金参数，`real_budget_entry` 使用正常真实资金参数，`alpha_scale_entry` 使用已获准的强机会放大资金参数。资金层级决定占用多少，rank 只决定同层和全市场资金优先级；rank 不得把小仓试探升级为正常或放大资金。 |
| `capital_ratio_source` | `final_action_contract.evidence_used` / `capital_deployment` | 当前资金层级引用的资金参数来源，例如 `probe_margin_ratio_0.008`、`normal_trade_margin_ratio`、`strong_opportunity_target_margin_ratio`。该字段只解释参数来源，不改参数值。 |
| `rank_reason` | `final_action_contract.evidence_used` / `capital_deployment` | rank=1 或该候选排名位置的确定性原因摘要。watch/probe 层固定表达按证据、触发、学习、风险质量排序后的最佳小探针候选；真实资金层表达当前证据和产品级学习支持；放大层表达多次正向 alpha、触发质量和回撤约束均达标。 |
| `rank_input_components` | `final_action_contract.evidence_used` / `capital_deployment` | 全市场资金 rank 的确定性输入快照，至少包含 `capital_priority_tier`、`capital_priority_score`、`watch_priority_score`、`opportunity_score`、证据质量、setup/trigger 质量和主要学习分项；用于证明 rank 不是旧局部排序或空字段补齐。 |
| `lifecycle_learning_trace` | `final_action_contract.evidence_used` / `capital_deployment` | 学习路由轨迹。最终决策层 `decision_learning_rows` 必须来自 Step6 按最终 `final_action/current_lots/target_lots` 与合约权限重新路由出的 final lifecycle trace；Step1-Step5 的 router/diagnostic/provenance 不能直接复制为最终 `decision_learning_rows`。生命周期匹配规则见 `docs/matrix_action_canonical.md`。 |
| `learning_impact_delta` | `final_action_contract.evidence_used` / `capital_deployment` | 学习对本次资金 rank 或生命周期决策的净影响拆解。rank 场景记录正向 open/add 学习、负向 open/add 学习、入场质量亏损、触发质量正负反馈等分项；非 rank 场景记录是否改变持仓解释、减仓/退出倾向、条件监控或 execution profile；`execution_profile_learning_direct_to_rank` 必须为 false。 |
| `rank_cleanup_fields` | PM 全市场资金部署工具 / `final_action_semantics.canonicalize_final_action_contract_for_persistence()` | 非 full-market rank 清理只允许删除 rank 专属字段：`opportunity_rank`、`rank_source`、`rank_scope`、`capital_rank_generated_by`、`rank_capital_role`、`capital_layer`、`capital_ratio_source`、`rank_reason`、`rank_input_components`、`alpha_scale_eligible` 及其他 rank 语义布尔字段。`lifecycle_learning_trace`、`learning_impact_delta`、`pm_lifecycle_learning_trace`、`pm_lifecycle_learning_impact_delta` 是生命周期学习解释字段，不能因为合约不走 rank 而被清理。 |
| `pm_lifecycle_learning_trace` | `final_action_contract.learning_used` | PM 合约构造器写入的最终动作生命周期学习 trace，覆盖 `open_add_new_risk`、`hold`、`reduce_exit`、`conditional_monitor` 和 `wait`。它用于证明 PM 把 action-value 按生命周期路由到正确决策口，不创建第二张交易合约。 |
| `pm_lifecycle_learning_impact_delta` | `final_action_contract.learning_used`；Researcher `contextual_rule_calibration` evidence | PM 合约构造器写入的生命周期学习影响拆解，记录学习对 `target_lots/lots_delta`、持仓解释、释放资金动作、条件监控和 execution profile 的影响。Researcher 只可从该正式路径读取并原名保存已登记子集，用于未来情境校准；不得回读 `final_action_contract.action_candidates`、旧 `holding_rebalance_control` 对象或借用 `learning_to_position_summary.holding_lifecycle.lifecycle_classification`。它只解释最终合约结果，不授权 Trader 改手数或方向。 |
| `primary_lifecycle_action_port` | `portfolio_manager.py` 第 3 步 / PM 内部学习与诊断 trace | PM 主链第 3 步生命周期分流口，必须在第 2 步方向选择后、学习路由前由 `pm_lifecycle_action_port.py` 生成。它只服务 PM 内部分流、学习路由和 provenance；不得写入 `final_action_contract.evidence_used`，也不得作为 Step6 最终合约失败依据。Step6 是否需要 Step5 只能按第 4 步最终候选是否实际增加风险判定：从空仓建立非零仓位，或同方向且 `abs(target_lots)>abs(current_lots)`，均为 `requires_full_market_rank=true`；不增加风险为 false。 |
| `lifecycle_transition_diagnostic` | `pm_lifecycle_action_port.py` / PM 内部学习与诊断 trace | PM 内部用于解释候选生命周期曾经如何从 Step2 路径变化到后续候选形态的 provenance diagnostic。它不是最终合约自检，不是 workflow/PG 保存闸门，不得写入 `final_action_contract.evidence_used`，不得替代 `pm_six_step_trace.step6_contract_generation_check` 或 `pm_contract_self_check`。 |
| `pm_six_step_trace.step6_contract_generation_check` | `signal_snapshot.pm_six_step_trace` | Step6 签约时生成的最终合约生成合法性检查。它只检查最终 `final_action_contract` 是否由合法 PM 机制生成，包括手数动作自洽、实际增加风险是否具备 Step5 deployment、非新增风险合约不伪造 rank、Step5 未部署是否还原为 `target_lots=current_lots` 且无本次新增风险权限、`capital_deployment` 语义完整、PM 中间态不得进入保存 artifact。它不比较 Step2 与 Step6 是否一致。 |
| `pm_six_step_trace.pm_contract_self_check` | `signal_snapshot.pm_six_step_trace` | `pm_contract_self_check.py` 对最终 `final_action_contract` 自身做机制边界检查的结果。它检查基础字段、手数动作一致性、rank/非 rank/Step5 未部署边界、`capital_deployment` 和 `position_sizing_result` 语义完整、无空对象冒充事实、无盘中触发权限残留、无 PM 中间态污染。生命周期学习污染只审 Step6 final lifecycle trace 的 `decision_learning_rows`，不得读取 Step2 router、rank 外层 provenance 或 deployment 旧 trace 作为最终决策层学习行；`trigger_profile_learning_rows` 中的 execution/profile 学习只能作为触发画像证据，且 direct-to-rank 标志必须为 false。该结果由 PM 自身和回测前 PM 输出契约测试负责；daily PG 不读取、不复查，也不修合同。 |
| `initial_primary_lifecycle_action_port` / `contract_lifecycle_self_check` / `historical_lifecycle_transition_diagnostic` / `lifecycle_port_transition_reason` | 废弃旧字段名 / 旧迁移与负向测试 | 这些旧字段不得作为最终合约字段、不得写入 `final_action_contract.evidence_used`、不得作为 PM 最终闸门。若历史数据或旧输入里出现，Step6 必须清理，最终保存链只接受 `pm_six_step_trace.step6_contract_generation_check.ok == true` 与 `pm_six_step_trace.pm_contract_self_check.ok == true`。 |
| `pm_lifecycle_decision_port` | `final_action_contract.evidence_used` | 兼容性辅助快照，来自 `pm_lifecycle_learning_trace.contract_lifecycle_port`，只描述最终合约形态；不得反向替代 Step2 的 `primary_lifecycle_action_port`，也不得作为 Step6 最终合约失败依据。 |
| `pm_lifecycle_trace_landed_in_contract` | `final_action_contract.evidence_used` | 布尔声明：PM 在最终合约签出后已把安全的 `learning_to_position_summary` 与生命周期学习 trace 回填到 `final_action_contract`；内部 `learning_to_position_trace`、`adaptive_policy_state`、`strategy_memory` 和策略行对象不得进入 PM artifact。 |
| `capital_allocation_reason` | PM scorecard / `final_action_contract.evidence_used` / 资金部署 / 复盘评估 | PM 为什么给该候选资金、监控或暂不分配资金的机器可读理由。凡有资金排名、新开、加仓、扩大或条件监控的最终合约，都必须有该理由。 |
| `fusion_attribution_label` | Reviewer 归因 / Researcher 学习输入 | 复盘员对 PM 融合证据处理结果的只读标签，如 fusion_conflict_handled、fusion_conflict_unresolved、multi_evidence_consensus_supported；只供未来学习，不改当天事实。 |
| `evidence_fusion_attribution` | Researcher learning event | 研究员基于复盘事实写入的未来融合学习上下文；只服务下一交易日分析师校准和 PM 排序，不创建当天交易权限。 |
| `capital_deployment` | `final_action_contract` / PM 资金部署 / 复盘评估 | PM 资金部署结果对象，记录候选是否入选、原目标手数、部署后目标手数、部署手数变化、部署原因和新增风险排名；只能解释并回写同一张 `final_action_contract`，不能作为第二交易权限。所有最终合约都必须原子写入该对象；只有实际增加风险的对象允许包含 `opportunity_rank`，包括新开仓和同方向扩大绝对手数的 `add/scale`。 |
| `no_rank_no_new_exposure` | `final_action_contract.reason_codes` / `capital_deployment.capital_allocation_reason` | 新增风险候选未获得 PM 全市场 rank 时的确定性还原原因；最终合约必须把 `target_lots` 还原为 `current_lots`，空仓形成 `wait`、已有持仓形成 `hold`，不得由 workflow fallback 保留 `open/open_probe/open_real/add/scale/conditional open` 的新增风险目标。 |
| `no_rank_or_budget_no_new_exposure` | `final_action_contract.reason_codes` / `capital_deployment.capital_allocation_reason` | 新增风险候选已经进入全市场 rank 队列，但按 rank 顺序消耗总保证金、单品种或净敞口预算时未被选中；最终合约必须还原为 `target_lots=current_lots`，空仓形成 `wait`、已有持仓形成 `hold`，不能绕过预算继续增加风险。 |
| `contract_requires_conditional_intraday_result` | `final_action_semantics` / pre-backtest output-contract test / Trader 盘中结果要求 | 条件候选是否需要 Trader 写盘中触发/未触发结果的统一只读语义。只有最终合约仍然部署实际新增风险目标的条件开仓才需要盘中结果；已经被 `no_rank_no_new_exposure` 或明确未选中原因还原为 `target_lots=current_lots`、`selected_for_capital_deployment=false` 的未部署条件候选，不要求 `opportunity_rank`，也不要求 Trader 写盘中结果。同方向实际扩大绝对手数的 `add/scale` 必须经过全市场 rank；当前反转退出腿不经过 rank。daily PG 只核对实际进入执行路径后的外部执行与成交事实，不复查该内部语义推导。 |
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
| `watch_for_trigger_semantic_block` | PM 诊断 | 非法或未获条件权限的 watch 阻止立即、无条件开仓；不得阻止完整 canonical watch 进入资金排名。 |
| `watch_for_trigger_semantic_release_block` | PM 诊断 | 释放路径被 `watch_for_trigger` 语义阻止。 |

## 8. Auditor 字段：`audit_verdict`

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `audit_verdict` | Auditor 输出 / recommendation `audit_payload` / signal snapshot auditor 摘要 | 独立审计员对 PM 已签 `final_action_contract` 的审计裁决；当前 Auditor 只生成 `approve`、`approve_with_warning`、`block`。 |
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
| `entry_authority_gate` | Trader Phase2 audit | 条件新增风险合约的下单安全闸记录。对需要盘中确认的合约，初次翻译可记录 `deferred_until_intraday_trigger`，表示先写盘中触发/未触发事实；触发后再校验最终下单安全，不生成策略权限。 |
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
| `positions` | `portfolio.positions` JSON 物理列 / Portfolio 对象 | 结算后当前持仓的唯一 portfolio 事实；不存在独立 position SQL 表。 |
| `positions_snapshot` | `daily_settlement.positions_snapshot` | 当日日结算持仓快照；用于与 `portfolio.positions`、成交和逐品种 PnL 对账。 |
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

Researcher 只在 Phase4 completed 且结算事实形成后运行；写入前必须沿真实物理路径验证：AEC 由 `signal_collection_contract.source_contracts[].signal_record_id` 指向 signal SQL；SCC、FAC、Auditor 和 `execution_result` 位于同一 `futures_recommendation`；transaction 通过 `futures_transactions.recommendation_id` 关联 recommendation；settlement 通过 transaction/portfolio 的 `portfolio_id`、配置和 `trading_date` 对应 `daily_settlement`。系统没有学习专用 `settlement_id`，不得为追溯自造该字段。零成交和无合格学习成果均合法；不要求每笔交易形成学习，也不要求每次决策使用学习。

Researcher 单次运行的研究 SQL 写入、`researcher_learning_completed`、外置 payload artifact、template prior 和历史学习快照属于同一提交边界。任一环节失败时 SQL 必须回滚、完成事件不得存在、本次新文件必须删除、被本次尝试覆盖的既有合法文件必须恢复；不得以孤立 artifact 代替数据库引用。

| 字段 | 放置位置 | 含义 |
|---|---|---|
| `researcher_llm_notes.evidence_pack_id` | Researcher 验证后研究记录 | 已通过来源、日期、Phase4 和结算边界检查的 evidence pack 身份。 |
| `researcher_llm_notes.payload_json` / `payload_artifact_path` / `payload_sha256` / `payload_size` / `payload_summary_json` | Researcher 验证后研究记录 | 保存可解析的结构化 `evidence` 与 `validated_output`；超出内联限制时使用正式 artifact。`raw_prompt/raw_response` 及其 artifact 元数据固定为空。 |
| `alpha_setup_sample.trading_date` / `recommendation_id` / `source_type` / `evidence_json` / `result_json` / `payload_json` | Researcher 结构化样本 | 学习来源交易日、推荐引用、真实交易/episode/反事实类型、验证证据和结算后结果；未交易机会不得伪造 transaction。 |
| `trade_episode_memory.payload_json.pair.open_recommendation_id` / `open_transaction_id` / `close_transaction_id` | 成交型 episode 学习 | 已完成交易 episode 对推荐及开平 transaction 的真实引用；只在对应事实存在时写入。 |
| `learning_event_log.config_id` / `trading_date` / `event_type` / `status` / `created_at` | Researcher completion 事件 | `researcher_learning_completed` 的配置、来源交易日、完成状态和写入时间；必须晚于对应 Phase4，并与本次研究 SQL 及 artifact 成功提交，不得在失败回滚后残留；不代表每个交易日必须产生具体学习记录。 |
| `state_key` | action-value / 学习 | 统一状态 key。 |
| `scope_type` | 学习记录 | 学习作用范围类型。 |
| `evidence_signature` | action-value / 学习 | 统一证据组合签名。 |
| `policy_type` | adaptive policy / provisional policy | 策略学习类型；不能作为交易动作。 |
| `policy_multiplier` | adaptive policy / provisional policy | 策略学习倍率；只能影响策略参数，不能覆盖 PM 合约。 |
| `action_name` | action-value | 历史动作名称，如 open、add、add_or_open、hold、reduce、exit、execution、conditional_monitor 等；不能直接作为学习家族判断依据，必须先经过 `canonical_action_family`。 |
| `action_preference` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 | 唯一动作偏好；PM 评分优先读取 DB 顶层 canonical 字段，payload 只作历史兼容来源。合法取值和 family/lane 匹配规则见 `docs/matrix_action_canonical.md`。 |
| `reward_source` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 | 奖励来源；用于区分真实 episode、真实交易、反事实或观察先验。 |
| `evidence_scope` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 | exact、partial、similar、counterfactual；PM 评分优先使用该字段判断学习作用域。 |
| `canonical_action_family` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 / PM `learning_used` | action-value 的统一动作家族；固定枚举为 `open_add_new_risk`、`reduce_exit`、`execution`、`hold`、`no_trade`、`observe`、`conditional_monitor`。完整 family/lane/preference 矩阵见 `docs/matrix_action_canonical.md`。 |
| `action_value_lane` | `alpha_setup_action_value` 顶层 canonical 列 / payload 兼容 | action-value 适用动作线；取值集合由 `src/tools/common/final_action_semantics.py` 与 `docs/matrix_action_canonical.md` 固定。 |
| `consumer_scope` | `alpha_setup_action_value` 顶层 canonical 列 / 学习 payload / 执行学习 trace | 学习记录的唯一消费边界；固定为 `pm_learning`、`analyst_calibration`、`trader_execution_learning`、`research_diagnostics`。PM 只读 `pm_learning`，分析师只读 `analyst_calibration` 安全摘要，Trader 只读 `trader_execution_learning` 执行诊断；`execution` family 的 `pm_learning` 只能进入 PM execution profile / trigger 输入，不能进入新开仓 rank、方向、手数或交易授权。 |
| `learning_lane` | `alpha_setup_action_value` 顶层 canonical 列 / 学习 payload | 学习消费动作线；与 `action_value_lane` 对齐，用于声明该学习服务的生命周期入口，完整矩阵见 `docs/matrix_action_canonical.md`。 |
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
| `learning_used.alpha_setup_action_values` | `final_action_contract.learning_used` | PM 最终合约实际声明消费的正式 canonical action-value 主证据列表。行级纯净性和生命周期匹配口径由 `docs/agent_pm.md` 与 `docs/matrix_action_canonical.md` 固定。 |
| `learning_used.memory_retrieval.rejected_or_downgraded` | `final_action_contract.learning_used` | PM 学习检索诊断列表。被剔除的 incomplete prior 只能以 `reason=incomplete_prior_not_pm_scoring_evidence` 保留在这里；该列表不参与 score、rank、手数、资金部署和 `final_action`。 |
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

## 16. 工作流核心载体嵌套字段路径

登记规则：

- 本节登记 `docs/workflow.md` 中由现有代码真实生成并跨智能体传递的嵌套字段路径。
- 同名子字段必须结合本节父路径理解；不得脱离父对象建立第二套语义。
- `[]` 表示列表中单条记录结构，不表示父对象顶层字段。

### 16.1 分析师 `action_evidence_contract`

| 字段路径 | 生产与消费位置 | 固定含义 |
|---|---|---|
| `action_evidence_contract.learning_scope` | 三个分析师生成 / Signal Collector 保真传递 | 本专业学习、商品 profile 与市场状态的分析范围；只校准证据和提示词，不创建交易权限。 |
| `learning_scope.setup_family` / `learning_scope.sector_setup_alignment` / `learning_scope.sector_preferred_setups` / `learning_scope.sector_caution_setups` | technical 学习范围 | 技术 setup 家族、板块匹配及优先/谨慎 setup。 |
| `learning_scope.primary_confirmation` / `learning_scope.execution_focus` / `learning_scope.market_regime` | technical 学习范围 | 技术主确认、执行关注点和当日市场状态。 |
| `learning_scope.factor_tree` / `learning_scope.primary_driver_groups` / `learning_scope.short_trigger_groups` / `learning_scope.conflict_groups` | fundamental 学习范围 | 基本面因子树、主驱动组、短期触发组和冲突组。 |
| `learning_scope.event_regime` / `learning_scope.event_type_counts` / `learning_scope.catalyst_classification` | commodity_news 学习范围 | 新闻事件状态、事件类型计数和催化分类。 |
| `learning_scope.product_profile_id` / `learning_scope.product_profile_version` / `learning_scope.product_profile_used` / `learning_scope.product_profile_fields_used` | 三个分析师学习范围 | 本次使用的商品 profile 身份、版本、使用状态和字段清单。 |
| `learning_scope.product_profile_learning_interaction` / `learning_scope.product_profile_analysis_boundary` | 三个分析师学习范围 | 商品 profile 与历史学习的交互说明及仅限分析证据的权限边界。 |
| `learning_impact_summary.historical_support` / `learning_impact_summary.historical_contradiction` | 分析师学习校准 | 历史学习对当日证据的支持数与反对数。 |
| `learning_impact_summary.product_learning_scopes` | 分析师学习校准 | 实际命中的商品学习范围列表。 |
| `learning_impact_summary.current_evidence_confirmed` / `learning_impact_summary.current_evidence_missing` | 分析师学习校准 | 历史结论被当日证据确认或仍缺少确认的项目。 |
| `learning_impact_summary.opportunity_state_reason` | 分析师学习校准 | 学习校准后机会状态的结构化原因。 |
| `learning_impact_summary.positive_strength` / `learning_impact_summary.negative_strength` / `learning_impact_summary.broad_positive_strength` / `learning_impact_summary.broad_negative_strength` | 分析师学习校准 | 同范围与宽范围历史学习的正负强度。 |
| `learning_impact_summary.net_evidence_adjustment` | 分析师学习校准 | 历史学习对当日证据的净校准量。 |
| `learning_impact_summary.authority_boundary` / `factor_calibration_summary.authority_boundary` / `event_calibration_summary.authority_boundary` | 分析师学习校准 | 固定声明学习只校准分析证据，不产生动作、手数、rank 或预算权限。 |
| `factor_calibration_summary.effective_factors` / `factor_calibration_summary.stale_or_conflicting_factors` / `factor_calibration_summary.factors_requiring_price_confirmation` | fundamental 因子校准 | 有效、陈旧/冲突及仍需价格确认的基本面因子。 |
| `factor_calibration_summary.supporting_learning_scopes` / `factor_calibration_summary.contradicting_learning_scopes` / `factor_calibration_summary.factor_calibration_reason` | fundamental 因子校准 | 支持/反对学习范围及因子校准原因。 |
| `event_calibration_summary.effective_catalysts` / `event_calibration_summary.background_noise` / `event_calibration_summary.impact_window_assessment` / `event_calibration_summary.price_volume_confirmation_required` | commodity_news 事件校准 | 有效催化、背景噪声、影响窗口及是否需要量价确认。 |
| `event_calibration_summary.supporting_learning_scopes` / `event_calibration_summary.contradicting_learning_scopes` / `event_calibration_summary.event_calibration_reason` | commodity_news 事件校准 | 支持/反对学习范围及事件校准原因。 |
| `product_profile_evidence.profile_analysis_boundary` / `product_profile_evidence.profile_fields_used` / `product_profile_evidence.profile_supported_evidence` / `product_profile_evidence.profile_conflicting_evidence` / `product_profile_evidence.profile_missing_evidence` | 分析师商品 profile 证据 | profile 使用边界、字段及支持/冲突/缺失证据。 |
| `product_profile_evidence.profile_assumption_status` / `product_profile_evidence.profile_relevance_score` / `product_profile_evidence.profile_learning_interaction` / `product_profile_evidence.profile_invalid_use_flags` | 分析师商品 profile 证据 | profile 假设状态、相关度、学习交互和违规使用标记。 |
| `fusion_evidence.evidence_strength_score` / `fusion_evidence.evidence_freshness_score` | 分析师融合证据 | 单分析师证据强度和时效的标准化数值。 |
| `fusion_evidence.fusion_boundary` | 分析师融合证据 | 固定声明该融合对象仍是分析证据，不是 PM rank 或交易授权。 |
| `data_usage_summary.sources` | 分析师数据追溯 | 本次分析实际访问的数据源记录集合。 |
| `data_usage_summary.sources.*.source` / `data_usage_summary.sources.*.dataset` / `data_usage_summary.sources.*.available` / `data_usage_summary.sources.*.used_in_signal` / `data_usage_summary.sources.*.pre_open_only` / `data_usage_summary.sources.*.info_cutoff` | 分析师数据追溯 | 数据来源、数据集、可用性、是否进入信号及盘前信息截止边界。 |
| `data_usage_summary.sources.pandaai_market.latest_data_date` / `row_count` / `fields_used` / `indicators_used` | technical 数据追溯 | PandaAI 行情最新日期、记录数、使用字段和实际进入提示词的技术指标；波动率、成交强度和价格位置必须真实传入后才能登记，布林带使用当次学习校准后的 `bollinger_std`。 |
| `data_usage_summary.sources.finoview_fundamental.configured_indicator_count` / `loaded_indicator_count` / `missing_like_count` / `stale_indicator_count` / `near_stale_indicator_count` | fundamental 数据追溯 | Finoview 配置、加载、缺失、陈旧和临近陈旧指标数量；Router文本输入与factor snapshot共用同一 catalog 频率、freshness、正式交易日 release-lag 和可见行选择器。频率优先使用显式配置及文件实际日期节奏；原始 `tradeDate` 是事实日期，`recordTime` 不进入正式契约或可见边界。只有实际传给分析师的非陈旧因子可登记为 `used_factors`。 |
| `data_usage_summary.sources.finoview_fundamental.coverage_ratio` / `stale_ratio` / `factor_groups` / `freshness_score` / `local_availability_audit` / `coverage_status` / `supports_trade_setup` / `runtime_data_boundary` | fundamental 数据追溯 | 基本面覆盖、时效、因子组、本地可用性、交易 setup 支持状态和运行时边界。`local_availability_audit` 只允许已登记数量、分组、比例、状态、布尔边界和 `index_map_parse_error_count`，禁止路径、样本、原始解析错误及内部说明。 |
| `data_usage_summary.sources.pandaai_extra.reference_date` / `lookback_days` / `feature_count` / `record_counts` / `feature_status` / `data_missing` / `error_count` | fundamental 扩展数据追溯 | PandaAI 扩展基本面参考日、窗口、特征覆盖、稳定缺失状态和错误数量；`basis_ratio` 是百分数、`ls_ratio` 以 50 为中性、合约日指标 `ratio` 以 0 为中性，不得使用同一通用比例猜测；不传请求参数、原始错误、内部方向提示或内部可交易性判断。 |
| `data_usage_summary.sources.finoview_news_txt.news_cutoff` / `raw_block_count` / `parsed_news_count` / `selected_news_count` / `latest_news_date` / `freshness_score` / `relevance_score` | commodity_news 数据追溯 | 新闻截止、解析/筛选数量、最新日期、时效及按品种产业链确定性计算的相关度；新闻必须在截取最新条目之前过滤产品无关内容，非空不能自动获得固定相关度；不传本机文件路径、编码或内部事件/方向统计。 |
| `data_usage_summary.sources.pandaai_pre_open_reference.missing_data` / `data_quality_flags` / `reason` | 三个分析师全局数据不可用状态 | 必需盘前市场事实缺失时的唯一正式来源；固定指向前交易日具体主力合约报价，`reason` 固定为 `pre_open_reference_price_unavailable`，不得复制 Router 或 provider 自由文本。 |
| `data_usage_summary` / `data_usage_summary.sources.*` | AEC 共享校验 | 顶层、来源身份、字段集合及基础类型必须由共享校验器登记；禁止 prompt/response、内部状态、隐藏上下文、未验证工具结果、本机路径、编码和内部说明。 |

### 16.2 Signal Collector `signal_collection_contract`

| 字段路径 | 生产与消费位置 | 固定含义 |
|---|---|---|
| `source_contracts[].analyst` / `source_contracts[].signal_record_id` / `source_contracts[].action_evidence_contract` | Signal Collector 来源记录 | 来源分析师、signal SQL 记录 ID 和完整 AEC 保真副本。 |
| `evidence_items[].analyst` / `evidence_items[].side` / `evidence_items[].confidence` / `evidence_items[].signal` / `evidence_items[].opportunity_state` | SCC 逐条证据 | 单条证据来源、方向、置信度、分析信号和机会状态。 |
| `evidence_items[].trigger_status` / `evidence_items[].entry_trigger` / `evidence_items[].setup_quality_ok` | SCC 逐条证据 | 单条证据的触发汇总、入场条件和 setup 质量状态。 |
| `evidence_items[].product_profile_id` / `evidence_items[].product_profile_used` / `evidence_items[].product_profile_analysis_boundary` | SCC 逐条证据 | 来源商品 profile 身份、使用状态和分析边界。 |
| `invalidation_summary[].analyst` / `invalidation_summary[].condition` / `invalidation_summary[].level` | SCC 失效边界 | 每个分析师对应的失效条件和数值边界。 |
| `evidence_fusion.evidence_strength_by_analyst` / `evidence_fusion.evidence_freshness_by_analyst` | SCC 融合 | 按 technical、fundamental、commodity_news 分开的证据强度和时效映射。 |
| `cross_analyst_conflicts[].analyst` / `cross_analyst_conflicts[].side` / `cross_analyst_conflicts[].conflicts` | SCC 跨分析师冲突 | 冲突来源、方向和冲突明细。 |
| `dominant_opposing_evidence[].analyst` / `dominant_opposing_evidence[].side` / `dominant_opposing_evidence[].strength` / `dominant_opposing_evidence[].freshness` / `dominant_opposing_evidence[].conflicts` | SCC 主方向反证 | 反对主方向的来源、方向、强度、时效和冲突明细。 |

### 16.3 PM `FuturesRecommendation` 与 `final_action_contract`

| 字段路径 | 生产与消费位置 | 固定含义 |
|---|---|---|
| `FuturesRecommendation.id` / `config_id` / `reference_portfolio_id` / `trading_date` / `effective_trade_date` / `created_at` | recommendation 顶层 | 推荐身份、配置、`Prev(T)`参考组合、逻辑交易日T、逻辑生效日T和物理创建时间。 |
| `FuturesRecommendation.source_type` / `underlying_code` / `from_contract` / `to_contract` / `contract_code` | recommendation 顶层 | 推荐来源类型、品种及策略/换约涉及的具体合约。 |
| `FuturesRecommendation.action` / `lots` / `justification` | recommendation 顶层 | PM Step6 根据唯一最终合约重建的动作、交易手数和可读理由；不能替代 `final_action_contract`。 |
| `FuturesRecommendation.signal_snapshot` / `audit_payload` / `warning_message` / `status` | recommendation 顶层 | 唯一信号快照、审计载体、警告和运行状态。 |
| `signal_snapshot.signal_collection_contract` / `signal_snapshot.final_action_contract` / `signal_snapshot.pm_six_step_trace` | PM Step6 初始 snapshot | PM 原子写入的原始 SCC、唯一交易合约和最终签约检查。 |
| `final_action_contract.lots_delta_abs` | PM 唯一合约 | 最终目标手数变化量的绝对值。 |
| `final_action_contract.target_margin_ratio_estimate` | PM 唯一合约 | PM 按最终目标手数估算的目标保证金占权益比例。 |
| `final_action_contract.authority_decision` / `requires_authority` / `open_action_evidence` / `strong_current_evidence` / `watch_for_trigger_block` / `negative_profile` / `tradeable_state` / `weak_conflict_probe` | PM 唯一合约 | 最终开仓权限判定、当日证据、触发阻断、负面画像和候选质量事实。 |
| `final_action_contract.max_allowed_margin_ratio` | PM 唯一合约 | PM 权限链允许该合约使用的最大保证金比例；不得高于硬风控。 |
| `final_action_contract.contract_code` | Router/持仓生产输入事实；PM Step6绑定；Auditor / Trader / Reviewer / Researcher只读 | 已有持仓优先绑定持仓合约；新增风险只绑定 Router 截止点内可见的合法具体合约，缺失时不得新增风险。 |
| `final_action_contract.setup_type` | 分析师AEC生产原始值；PM Step6选择；Trader / Reviewer / Researcher只读 | 与最终方向、动作及Step4学习作用域一致的最终setup；不得取第一个分析师或反方向值。 |
| `final_action_contract.horizon_class` | 分析师AEC生产原始值；PM Step6选择；Trader / Reviewer / Researcher只读 | 与最终方向、动作及Step4学习作用域一致的最终期限类别。 |
| `final_action_contract.expected_horizon_days` | 分析师AEC生产原始值；PM Step6选择；Trader / Reviewer / Researcher只读 | 仅从与最终方向和 `horizon_class` 一致的真实AEC中选择；缺失时保持缺失。 |
| `final_action_contract.market_regime` | 分析师AEC生产原始值；PM Step6选择；Trader / Reviewer / Researcher / 下一交易日PM只读 | 与最终方向和Step4学习检索作用域一致的最终市场状态。 |
| `final_action_contract.invalidation_level` | 分析师AEC生产原始值；PM Step6选择；Auditor / Trader / Reviewer / Researcher只读 | 仅在真实数值存在且来源方向与最终方向一致时写入；禁止默认值和反方向填充。 |
| `final_action_contract.atr_stop_distance` | technical AEC生产原始值；PM Step6选择；Trader / Reviewer / Researcher只读 | 仅在真实生产且与最终方向及setup一致时写入；禁止默认值。 |
| `final_action_contract.evidence_used` | PM 唯一合约 | PM Step6 写入的最终证据、方向、rank、资金部署和 sizing 解释容器。 |
| `evidence_used.scorecard_preferred_side` / `scorecard_state` / `scorecard_score` | PM 最终证据 | PM scorecard 的首选方向、最终机会状态和分数。 |
| `evidence_used.direction_evidence_strength` / `direction_evidence_boundary` | PM 最终证据 | 方向证据质量和“只读 SCC、不重建方向证据”的边界。 |
| `evidence_used.market_confirmation_score` / `market_confirmation_conflicts` | PM 最终证据 | 盘前市场确认分与冲突事实。 |
| `evidence_used.side_priority_score` / `side_priority_meaning` / `side_priority_is_not_trade_authority` | PM Step2 方向选择 | 单品种内方向优先级分、固定含义及非交易权限声明。 |
| `evidence_used.candidate_quality` / `candidate_layer_hint` | PM 最终证据 | 方向候选质量和候选层级提示，不是资金层级或最终授权。 |
| `evidence_used.analyst_direction_evidence.side` / `source` / `boundary` / `supporting_signal_count` / `supporting_analysts` / `candidate_quality` / `candidate_layer_hint` / `opportunity_score` | PM 方向证据摘要 | 从 SCC 形成的方向支持来源、数量、候选质量和机会分。 |
| `evidence_used.direction_evidence_components.opportunity_score` / `candidate_quality` / `supporting_signal_count` / `supporting_analysts` / `setup_quality` / `trigger_valid` / `invalidation_present` / `conflict_count` | PM 方向证据分项 | 方向选择实际使用的机会、支持、setup、触发、失效和冲突分项。 |
| `evidence_used.opportunity_score_components.directional_support` / `tradeable_state` / `business_quality` / `setup_quality` / `confidence` / `market_confirmation` | PM 机会评分 | 当日方向、可交易状态、业务质量、setup、置信度和市场确认分项。 |
| `evidence_used.opportunity_score_components.positive_learning` / `negative_learning` / `recent_tail_loss_penalty` / `entry_quality_loss_penalty` / `trigger_quality_positive_bonus` / `trigger_quality_loss_penalty` | PM 机会评分 | 正负 action-value、尾部亏损、入场质量和触发质量的学习修正。 |
| `evidence_used.opportunity_score_components.execution_profile_learning` | PM 机会评分诊断 | execution/profile 学习观察值；固定不直接进入 rank 总分。 |
| `evidence_used.opportunity_score_components.fusion_consensus` / `fusion_score_adjustment` | PM 机会评分 | SCC 多证据一致性和冲突/缺失造成的融合调整。 |
| `evidence_used.pm_fusion_diagnostics.contract_version` / `pm_fusion_diagnostics` / `evidence_alignment_state` / `multi_evidence_consensus_score` | PM 融合解释 | SCC 融合版本、诊断标记、一致性状态和一致性分。 |
| `evidence_used.pm_fusion_diagnostics.cross_analyst_conflict_count` / `dominant_opposing_evidence_count` / `missing_evidence_count` / `confirmation_requirement_count` | PM 融合解释 | 跨分析师冲突、主方向反证、缺失和确认要求计数。 |
| `evidence_used.pm_fusion_diagnostics.fusion_score_adjustment` / `requires_pm_conflict_resolution` / `requires_pm_confirmation_explanation` / `no_trade_authority` | PM 融合解释 | 融合调整、PM 必须解释的冲突/确认事项及非交易权限声明。 |
| `evidence_used.pm_conflict_resolution.handled` / `resolution_effect` / `confirmation_requirements_addressed` / `no_trade_authority` | PM 冲突处理解释 | PM 是否处理冲突、处理效果、确认要求是否覆盖及非授权边界。 |
| `evidence_used.position_sizing_result.tool` / `ticker` / `current_lots` / `target_lots` / `lots_delta` / `lots_delta_abs` | PM sizing 事实 | sizing 工具、品种和最终手数变化。 |
| `evidence_used.position_sizing_result.target_position_ratio` / `target_value` / `margin_required` / `account_equity` / `target_margin_ratio_estimate` / `margin_rate` | PM sizing 事实 | 目标仓位、名义价值、保证金、权益和保证金率计算事实。 |
| `evidence_used.position_sizing_result.current_net_exposure` / `projected_net_exposure` / `current_ticker_exposure` / `max_position_ratio` / `max_net_exposure` / `risk_level` | PM sizing 事实 | sizing 前后净敞口、品种敞口、上限和风险等级。 |
| `evidence_used.position_sizing_result.lots_to_trade_reason` / `control_reasons` / `capital_allocation_reason` | PM sizing 事实 | 最终手数、控制和资金分配原因。 |
| `evidence_used.position_sizing_result.no_final_action_authority` / `no_direction_override_authority` / `no_llm` | PM sizing 事实 | sizing 不签动作、不改方向且不调用 LLM 的固定边界。 |
| `rank_input_components.rank_score_components.cold_start_evidence_quality` / `capital_layer_priority` / `open_add_action_value_delta` / `product_setup_trigger_history` / `trigger_execution_quality` / `capital_efficiency` / `conflict_risk_invalidation_penalty` | PM rank 分项 | 唯一 rank 的七项确定性评分；配置名与 Python 消费字段一一对应。 |
| `evidence_used.lifecycle_learning_trace` | PM 最终证据 | Step6 按最终生命周期形成的学习路由与 rank 学习解释。 |
| `lifecycle_learning_trace.trace_version` / `contract_lifecycle_port` / `pm_lifecycle_action_port` / `router_source` / `rank_lifecycle` | PM 生命周期学习 trace | trace 版本、最终生命周期口、路由入口、来源和 rank 生命周期。 |
| `lifecycle_learning_trace.allowed_learning_lanes` / `accepted_learning_lanes` / `blocked_learning_lanes` / `trigger_profile_learning_lanes` / `used_lanes` / `ignored_lanes` | PM 生命周期学习 trace | 允许、采用、阻断、execution/profile、已用和忽略的学习 lane。 |
| `lifecycle_learning_trace.positive_count` / `negative_count` / `exact_real_count` / `episode_count` | PM 生命周期学习 trace | 被路由学习记录的正负、精确实盘和 episode 数量。 |
| `lifecycle_learning_trace.decision_learning_rows` / `trigger_profile_learning_rows` / `rejected_learning` | PM 生命周期学习 trace | 决策层、execution/profile 层和拒绝层学习记录列表。 |
| `decision_learning_rows[].source_index` / `id` / `ticker` / `side` / `canonical_action_family` / `lane` / `action_preference` / `reward_mean` / `sample_count` | PM 最终决策学习行 | 被最终生命周期接受的 action-value 索引、身份、语义和样本表现。 |
| `trigger_profile_learning_rows[].source_index` / `id` / `ticker` / `side` / `canonical_action_family` / `lane` / `action_preference` / `reward_mean` / `sample_count` / `route` / `not_rank_learning` | PM execution/profile 学习行 | 只供触发画像的学习记录，固定不进入 rank。 |
| `rejected_learning[].source_index` / `id` / `ticker` / `side` / `canonical_action_family` / `lane` / `action_preference` / `reward_mean` / `sample_count` / `reason` / `errors` | PM 拒绝学习行 | 因语义或生命周期不匹配被拒绝的学习记录及原因。 |
| `lifecycle_learning_trace.execution_profile_learning_direct_to_rank` / `trigger_profile_learning_direct_to_rank` / `execution_profile_signal_direct_to_rank` | PM 生命周期学习 trace | 固定为 false，禁止 execution/profile 学习直接改变 rank。 |
| `lifecycle_learning_trace.memory_requirement_status` / `memory_requirements` | PM 生命周期学习 trace | 最终生命周期所需 PM 记忆的检索状态和要求。 |
| `lifecycle_learning_trace.hold_learning_decision` / `reduce_exit_learning_decision` / `open_add_learning_decision` / `conditional_monitor_learning_decision` / `execution_profile_learning_decision` | PM 生命周期学习 trace | 各生命周期口实际使用的学习决策摘要。 |
| `lifecycle_learning_trace.final_contract_effect_fields` / `pm_final_contract_lifecycle_trace` | PM 生命周期学习 trace | 学习允许影响的最终合约字段清单和 Step6 最终生命周期 trace。 |
| `evidence_used.learning_impact_delta.trace_version` / `current_lots` / `target_lots` / `lots_delta` | PM 学习影响 | 学习影响版本和最终手数变化。 |
| `learning_impact_delta.pre_learning_position_ratio` / `final_target_position_ratio` / `position_ratio_delta` | PM 学习影响 | 学习前仓位、最终目标仓位及净变化。 |
| `learning_impact_delta.open_add_rank_score_delta` / `net_rank_learning_delta` / `rank_score` / `rank_score_open_add_learning_delta` | PM 学习影响 | open/add 学习对唯一 rank 的净影响。 |
| `learning_impact_delta.alpha_setup_multiplier` / `alpha_setup_expectancy_lane` | PM 学习影响 | alpha setup 仓位乘数和收益预期 lane。 |
| `learning_impact_delta.hold_decision` / `hold_changes_position` / `reduce_exit_decision` / `reduce_exit_changes_position` / `conditional_monitor_decision` | PM 学习影响 | hold、reduce/exit 和条件监控学习对最终仓位的影响。 |
| `learning_impact_delta.execution_profile_changed` / `execution_profile_learning_direct_to_rank` / `execution_profile_learning_observed` | PM 学习影响 | execution profile 是否改变、禁止直达 rank 和观察量。 |
| `final_action_contract.learning_used` | PM 唯一合约 | PM Step4 消费并由 Step6 安全落入最终合约的学习事实。 |
| `learning_used.alpha_setup_action_values[]` | PM 正式学习 | 通过 canonical、consumer_scope、日期和生命周期过滤的正式 action-value 列表。 |
| `alpha_setup_action_values[].action_value_id` / `scope_key` / `canonical_action_family` / `learning_lane` / `action_value_lane` / `action_preference` / `memory_side_role` | PM 正式学习行 | action-value 身份、范围、canonical 家族、lane、偏好和方向角色。 |
| `alpha_setup_action_values[].reward_mean` / `reward_sum` / `win_rate` / `sample_count` / `last_sample_date` / `retrieval_match_level` | PM 正式学习行 | 历史收益、胜率、样本、最后日期和检索匹配层级。 |
| `learning_used.memory_requirements.contract` / `action_lifecycle` / `action` / `current_position_side` / `target_side` / `contract_side_role` | PM 记忆要求 | 最终动作生命周期、动作和当前/目标方向角色。 |
| `memory_requirements.required_memory_lanes` / `required_memory_side_roles` / `required_pm_memory` / `must_land_in_pm_contract` / `audit_only_memory` | PM 记忆要求 | 必需 lane、方向角色、必须落入合约和仅审计记忆集合。 |
| `required_pm_memory[].lane` / `learning_lane` / `action_value_lane` / `side` / `memory_side_role` / `must_land_in_pm_contract` / `reason` | PM 单条记忆要求 | 单条正式记忆要求的 lane、方向、落地要求和原因。 |
| `learning_used.memory_retrieval.tool` / `boundary` / `status` / `reason` | PM Step4 检索 | 检索工具、边界、状态和不可用/降级原因。 |
| `memory_retrieval.requirement_details` / `alpha_setup_action_value_count_after_lifecycle` / `rejected_action_values` / `rejected_or_downgraded` | PM Step4 检索 | 各要求检索明细、最终数量及拒绝/降级记录。 |
| `requirement_details[].side` / `lane` / `memory_side_role` / `row_count` / `error` / `effective_memory_summary` / `retrieval_attempts` / `rejected_or_downgraded` | PM Step4 检索明细 | 单个方向/lane 的检索数量、错误、有效摘要、尝试和降级记录。 |
| `rejected_action_values[].id` / `scope_key` / `ticker` / `side` / `setup_type` / `action_name` / `learning_lane` / `memory_side_role` / `reason` | PM 拒绝 action-value | 被日期、方向、lane、范围或契约纯净性过滤的记录及原因。 |
| `learning_used.positive_open_seed.enabled` / `decision` / `target_side` / `seed_position_ratio` | PM 学习候选种子 | 正向 open action-value 在当日证据确认后形成的候选种子；不是最终授权。 |
| `positive_open_seed.selected_action_value` / `current_evidence` / `not_product_rule` / `does_not_bypass_final_contract_authority` | PM 学习候选种子 | 选中 action-value、当日证据和不绕过最终权限的边界。 |
| `positive_open_seed.current_evidence.strong_realtime_evidence` / `strong_market_confirmation` / `technical_entry_timing_supports_side` / `technical_opposes_side` / `has_tradeable_support` / `has_invalidation_or_stop` / `current_confirmation_score` / `independent_support_count` | PM 学习候选种子 | 正向历史学习必须由当日实时、市场、技术、setup、失效和独立支持证据确认。 |
| `learning_used.learning_adjustment_summary.positive_policy_count` / `negative_policy_count` / `positive_action_value_count` / `negative_action_value_count` / `exact_real_action_value_count` / `episode_action_value_count` | PM 学习摘要 | 正负 policy/action-value 及精确实盘、episode 数量。 |
| `learning_adjustment_summary.positive_learning_signal` / `negative_learning_signal` / `execution_profile_learning_signal` / `recent_tail_loss_signal` / `entry_quality_loss_signal` / `trigger_quality_positive_signal` / `trigger_quality_loss_signal` / `net_trigger_quality_loss_signal` | PM 学习摘要 | PM 评分和执行画像使用的学习信号。 |
| `learning_adjustment_summary.strongest_positive_action_value` / `strongest_negative_action_value` / `alpha_setup_score_adjustment` / `best_profile_state` / `best_profile_scope_key` / `capped_or_rejected_profile_count` / `effect` / `not_trade_authority` | PM 学习摘要 | 最强正负样本、profile 调整、总体效果及非授权边界。 |
| `learning_used.alpha_setup_ev_fusion` | PM alpha 学习融合 | action-value/profile 与当日证据融合后的确定性 PM 学习对象。 |
| `alpha_setup_ev_fusion.target_side` / `intended_action` / `profile_count` / `action_value_count` / `matched_action_value_count` / `ignored_action_value_count` | PM alpha 学习融合 | 目标方向、动作意图及 profile/action-value 匹配数量。 |
| `alpha_setup_ev_fusion.scorecard_state` / `side_priority_score` / `candidate_quality` / `candidate_layer_hint` / `scorecard_gating_failures` / `current_confirmation_score` | PM alpha 学习融合 | 当前机会、方向质量、候选层和门控事实。 |
| `alpha_setup_ev_fusion.has_tradeable_support` / `has_monitorable_setup` / `setup_quality_ok` / `has_invalidation_or_stop` | PM alpha 学习融合 | 当日可交易、可监控、setup 和失效保护事实。 |
| `alpha_setup_ev_fusion.expectancy_lane` / `positive_action_value` / `positive_action_value_candidate` / `candidate_positive_action_preference` / `negative_action_value` / `positive_profile` / `positive_profile_raw` / `negative_profile` | PM alpha 学习融合 | 正负 action-value/profile 及收益预期 lane。 |
| `alpha_setup_ev_fusion.open_action_value_missing` / `qualified_positive_expectancy` / `repeat_loss_without_new_evidence` / `tail_loss_blocks_real_amplification` | PM alpha 学习融合 | 开仓学习缺失、合格正收益、重复亏损和尾损放大阻断。 |
| `alpha_setup_ev_fusion.strong_realtime_evidence` / `strong_market_confirmation` / `technical_supports_side` / `technical_direction_supports_side` / `technical_entry_timing_supports_side` / `technical_opposes_side` / `fundamental_supports_side` / `news_supports_side` / `independent_support_count` | PM alpha 学习融合 | 当日三维证据对历史学习的确认或反对。 |
| `alpha_setup_ev_fusion.multiplier` / `max_profile_impact` / `gate_failures` / `pre_control_ratio` / `final_ratio` | PM alpha 学习融合 | 学习乘数、最大影响、门控及调整前后仓位。 |
| `alpha_setup_ev_fusion.not_product_blacklist` / `same_scope_required` / `candidate_prior_only` / `money_objective` | PM alpha 学习融合 | 不得形成产品黑名单、必须同范围、候选先验边界和资金目标。 |
| `alpha_setup_ev_fusion.profile_stats.sample_count` / `win_rate` / `profit_factor` / `net_pnl` | PM alpha profile 统计 | 选中 profile 的样本、胜率、盈亏因子和净收益。 |
| `alpha_setup_ev_fusion.action_value_stats.action_name` / `sample_count` / `reward_mean` / `reward_sum` / `win_rate` / `confidence_score` / `action_preference` / `scope_quality` / `real_amplification_support` / `loss_reward_count` / `tail_loss_count` / `worst_reward` | PM alpha action-value 统计 | 选中 action-value 的动作、表现、范围质量、实盘放大资格和亏损尾部事实。 |
| `learning_used.capital_utilization_learning.protected_memory` / `recovering_memory` / `learned_demote_record` / `adaptive_protect` / `adaptive_protect_record` | PM 资金利用学习 | 受保护/恢复记忆、降级 policy 和自适应保护记录。 |
| `capital_utilization_learning.learned_underperformance_block` / `protected_evidence_rejected` / `conflicting_weak_memory` | PM 资金利用学习 | 学习表现不佳阻断、保护证据拒绝和冲突弱记忆。 |
| `learning_used.capital_utilization_target.target_mode` / `high_quality_memory` / `current_margin_ratio` / `target_margin_ratio_min` / `target_margin_ratio_max` / `target_margin_ratio_confirmed` | PM 资金利用目标 | 资金目标模式、高质量记忆和当前/目标保证金区间。 |
| `capital_utilization_target.base_max_position_ratio` / `effective_max_position_ratio` / `effective_single_margin_ratio_cap` | PM 资金利用目标 | 基础/有效仓位上限和单品种保证金上限。 |
| `capital_utilization_target.dynamic_opportunity_margin_ratio_budget` / `dynamic_opportunity_margin_ratio_cap` / `dynamic_allocation_tier` / `dynamic_budget_diagnostics` | PM 资金利用目标 | 动态机会预算、上限、层级和计算诊断。 |
| `capital_utilization_target.alpha_release_tier` / `alpha_release` / `stop_protected` / `structured_invalidation` | PM 资金利用目标 | alpha 释放层级、释放事实、止损保护和结构化失效。 |
| `capital_utilization_target.base_position_anchor_lifted` / `single_position_cap_lifted` / `opportunity_margin_cap_limited` / `underutilization_breach` / `capital_allocation_tier` / `margin_ratio_gap_to_min` | PM 资金利用目标 | 仓位上限调整、机会保证金限制、资金不足和目标缺口。 |
| `learning_used.learning_to_position_summary` | PM 学习落地摘要 | 只保留学习如何影响最终仓位的安全摘要，不保留原始研究对象。 |
| `learning_to_position_summary.learning_context.enabled` / `selected_digest_count` / `candidate_hypothesis_count` / `validated_hypothesis_count` / `candidate_hypothesis_authority` | PM 学习落地摘要 | 学习启用、摘要与假设数量及候选假设权限。 |
| `learning_to_position_summary.learning_source_summary` / `position_effect` / `opportunity_to_position` / `current_day_validation` / `holding_lifecycle` / `artifact_boundary` | PM 学习落地摘要 | 学习来源、仓位影响、机会落地、当日验证、持仓生命周期和 artifact 边界。 |
| `learning_to_position_summary.position_effect.current_lots` / `target_lots` / `lots_delta` / `pre_control_position_ratio` / `final_target_position_ratio` / `action` / `action_lots` / `reason` / `control_reasons` | PM 学习仓位影响 | 学习调整前后手数、仓位、动作和原因。 |
| `learning_to_position_summary.opportunity_to_position.target_side` / `scorecard_preferred_side` / `mature_alpha_policy_count` / `fast_candidate_alpha_count` / `high_quality_opportunity_present` / `high_quality_opportunity_executed_or_targeted` / `if_not_targeted_requires_accountability` | PM 机会落地 | 高质量机会是否进入目标仓位及未进入时是否需要归因。 |
| `learning_to_position_summary.current_day_validation.market_confirmation_score` / `has_structured_invalidation` / `has_explicit_stop_protection` / `requires_today_signal_market_state_and_invalidation` | PM 当日验证 | 历史学习必须由当日市场、失效和止损事实确认。 |
| `learning_to_position_summary.artifact_boundary.summary_only` / `research_fact_objects_omitted` | PM artifact 边界 | 只保存摘要并明确剔除原始研究事实对象。 |
| `learning_used.pm_landing_consistency_audit` | PM 学习落地检查 | PM 内部只读的证据、学习、仓位和执行可行性摘要，不是 Auditor 结论。 |
| `pm_landing_consistency_audit.consistency_flags` / `consistent_enough_for_phase1` / `not_product_rule` / `no_future_data` | PM 学习落地检查 | 一致性问题、Phase1 可接受性及非产品规则、无未来数据声明。 |
| `final_action_contract.capital_deployment.selected_for_capital_deployment` / `original_target_lots` / `deployed_target_lots` / `deployed_lots_delta` | PM Step5 部署事实 | 是否入选资金队列及部署前后目标手数。 |
| `capital_deployment.rank_budget_sequence` / `rank_score` / `candidate_margin_ratio` / `queue_margin_ratio_before` / `queue_margin_ratio_after_if_selected` / `target_margin_ratio_budget` | PM Step5 预算队列 | rank 消费顺序、分数、候选保证金和队列前后保证金。 |
| `capital_deployment.max_single_ticker_margin_ratio` / `current_net_exposure_before` / `current_ticker_exposure` / `projected_net_exposure_if_selected` / `max_net_exposure` | PM Step5 预算队列 | 单品种上限及部署前后净敞口。 |
| `capital_deployment.single_ticker_budget_ok` / `total_margin_budget_ok` / `net_exposure_budget_ok` | PM Step5 预算队列 | 单品种、总保证金和净敞口预算是否通过。 |
| `final_action_contract.consistency.status` / `mode` / `issues` / `actual` / `expected` | PM 唯一合约一致性 | 最终动作与手数意图的一致性状态、问题、实际值和期望值。 |
| `final_action_contract.single_source_of_trade_truth` / `candidate_sources_do_not_bypass_contract` | PM 唯一合约 | 固定声明唯一交易真相及候选来源不得绕过合约。 |
| `final_action_contract.signal_collection_contract_ref.ticker` / `trading_date` / `source_contract_count` / `collector_decision_boundary` | PM SCC 引用摘要 | 原始 SCC 的产品、日期、来源数量和无交易权限边界；不能替代完整 SCC。 |
| `pm_six_step_trace.step6_contract_generation_check.tool` / `ok` / `errors` / `expected_final_action` / `actual_final_action` / `current_lots` / `target_lots` / `lots_delta` / `writes_db` / `writes_contract` / `no_llm` | PM Step6 生成检查 | 最终合约生成合法性及不写 DB、不生成第二张合约、不调用 LLM 的事实。 |
| `pm_six_step_trace.pm_contract_self_check.tool` / `ok` / `errors` / `expected_final_action` / `actual_final_action` / `current_lots` / `target_lots` / `lots_delta` / `writes_db` / `writes_artifact` / `writes_payload` | PM 最终合约自检 | 唯一最终合约自身一致性及 PM 不写 DB、artifact、payload 的事实。 |
| `final_action_contract.consistency` / `signal_collection_contract_ref` | PM 唯一合约 | 动作手数一致性对象和 SCC 摘要引用对象。 |
| `learning_used.memory_retrieval` / `positive_open_seed` / `capital_utilization_learning` / `pm_lifecycle_learning_router` | PM 最终学习 | Step4 检索、正向开仓候选、资金利用学习和生命周期路由对象。 |
| `pm_lifecycle_learning_router.accepted_lanes` / `accepted_learning` / `accepted_indices` / `decision_learning_indices` | PM 生命周期路由 | 被当前生命周期允许的 lane、记录和原列表索引。 |
| `pm_lifecycle_learning_router.rejected_learning_rows` / `rejected_indices` / `trigger_profile_learning` / `trigger_profile_indices` / `execution_profile_indices` | PM 生命周期路由 | 被拒绝和 execution/profile 分流的记录与索引。 |
| `pm_lifecycle_learning_trace.trigger_profile_learning` / `trigger_profile_indices` / `rejected_learning_lanes` | PM 最终生命周期 trace | execution/profile 学习行、索引和拒绝 lane。 |
| `alpha_setup_ev_fusion.selected_profile` / `profile_stats` / `action_value_stats` | PM alpha 学习融合 | 选中 profile、profile 统计和 action-value 统计对象。 |
| `selected_profile.profile_state_hint` / `profile_state_hint_boundary` / `data_combo` | PM alpha profile | profile 生命周期提示、非交易权限边界和数据组合。 |
| `selected_profile.product_learning_calibration_view.source_contract_version` / `deployment_tier` / `historical_pm_rank` / `historical_pm_score` / `historical_selected_for_capital_deployment` / `historical_net_pnl` | PM alpha profile | 商品学习来源版本、历史部署层级、历史 PM 资金事实和历史净收益。 |
| `product_learning_calibration_view.trigger_key` / `evidence_combo` / `not_trade_authority` / `future_only` / `analyst_usage_boundary` | PM alpha profile | 历史触发/证据组合及仅限未来证据校准的权限边界。 |
| `selected_action_value.canonical_action_preference_source` / `canonical_action_value` / `canonical_action_value_source` / `amplification_scope_quality` / `strict_no_lookahead` | PM action-value 摘要 | canonical 偏好来源、正式 action-value 标记、放大范围质量和无前视声明。 |
| `selected_action_value.exact_state_real_trade_sample_count` / `partial_state_real_trade_sample_count` / `similar_real_trade_sample_count` / `exact_ticker_sample_count` / `exact_ticker_real_trade_sample_count` / `real_trade_reward_count` / `counterfactual_reward_count` / `counterfactual_prior_only` | PM action-value 摘要 | 精确/部分/相似状态、精确品种、真实交易和反事实样本数量。 |
| `learning_to_position_summary.learning_source_summary.adaptive_policy_summary` / `alpha_setup_profile_summary` / `action_value_summary` / `strategy_memory_summary` | PM 学习来源摘要 | policy、profile、action-value 和策略记忆的安全汇总对象。 |
| `adaptive_policy_summary.policy_count` / `policy_type_counts` / `scope` / `status` | PM 学习来源摘要 | policy 数量、类型计数、摘要范围和状态。 |
| `alpha_setup_profile_summary.profile_count` / `lifecycle_counts` / `status` | PM 学习来源摘要 | profile 数量、生命周期计数和摘要状态。 |
| `action_value_summary.action_value_count` / `canonical_action_value_count` / `incomplete_trace_action_value_count` / `action_preference_counts` / `status` | PM 学习来源摘要 | action-value 总数、正式数、不完整数、偏好计数和摘要状态。 |
| `strategy_memory_summary.status` / `raw_object_omitted` | PM 学习来源摘要 | 策略记忆仅保存摘要且原始对象已剔除。 |
| `pm_landing_consistency_audit.decision` / `opportunity_scorecard_alignment` / `analyst_setup_alignment` / `learning_alignment` / `pm_risk_gate_alignment` / `trader_pre_execution_feasibility` | PM 学习落地检查 | 最终决策、机会、分析师、学习、PM 风险门和 Trader 可行性检查对象。 |
| `pm_landing_consistency_audit.decision.current_position_ratio` / `final_position_ratio` / `recommendation_action` / `lots_to_trade` / `margin_available` | PM 学习落地检查 | 调整前后仓位、推荐动作、交易手数和可用保证金。 |
| `opportunity_scorecard_alignment.side_final_state` / `side_score` / `entry_setup_count` / `invalidation_count` | PM 学习落地检查 | 目标方向的最终机会状态、分数、setup 数和失效边界数。 |
| `learning_alignment.learning_enabled` / `policy_count` / `policy_types` / `alpha_setup_profile_count` / `alpha_setup_lifecycle_counts` / `alpha_setup_action_value_count` / `alpha_setup_action_preference_counts` / `money_decision_trace_required` | PM 学习落地检查 | 学习启用状态、policy/profile/action-value 数量和资金决策 trace 要求。 |
| `trader_pre_execution_feasibility.margin_available` / `margin_feasible` / `actual_trader_result_pending_phase2` | PM 学习落地检查 | Phase1 可用保证金、执行可行性及 Trader 结果仍待 Phase2。 |
| `direction_evidence_components` | `final_action_contract.evidence_used` | PM 从 SCC 形成的方向证据分项对象。 |
| `rank_capital_priority_release_detail` | `final_action_contract.evidence_used` | rank 支持真实预算释放的条件与边界对象。 |
| `memory_state` | `final_action_contract.learning_used` | 资金利用学习中受保护记忆的状态摘要。 |
| `memory_requirement_reason` | PM action-value 摘要 | action-value 被当前生命周期和方向角色要求的原因。 |
| `execution_action_value_preference.base_execution_profile` / `does_not_create_trade_authority` / `keeps_pm_authority_boundary` | PM execution profile 偏好 | 调整前 profile 及不得创建权限、不得突破 PM 权限的固定边界。 |
| `selected_action_value.exact_ticker_support` | PM action-value 摘要 | 历史 action-value 是否具备当前品种的精确支持。 |
| `learning_to_position_summary.holding_lifecycle.lifecycle_classification` / `current_side` / `loss_revalidation_due` / `loss_revalidation_failed` | PM 学习落地摘要 | 持仓生命周期分类、当前方向及亏损持仓是否到期/未通过重新验证。 |
| `pm_landing_consistency_audit.version` | PM 学习落地检查 | PM 学习落地检查的结构版本。 |

### 16.4 Auditor 审计字段

| 字段路径 | 生产与消费位置 | 固定含义 |
|---|---|---|
| `AuditorInput.final_action_contract` | Workflow → Auditor 临时正式输入 | 完整、未修改的 `FuturesRecommendation.signal_snapshot.final_action_contract`。 |
| `AuditorInput.account_state.account_equity` / `margin_used` / `margin_ratio` / `risk_status` | Workflow → Auditor 临时正式输入 | 当前组合权益、已用保证金、保证金比例和清算风险状态；新增风险时均为必需事实。 |
| `AuditorInput.position_state.current_lots` / `contract_code` / `margin_used` / `margin_rate` / `contract_multiplier` | Workflow → Auditor 临时正式输入 | 当前品种持仓及持仓合约事实；用于核对 FAC 当前手数和已有持仓合约。 |
| `AuditorInput.contract_state.contract_code` / `underlying_code` / `as_of_date` / `source` | Workflow → Auditor 临时正式输入 | 新增风险使用 Router 具体合约事实；已有持仓使用持仓合约事实。不得使用默认合约。 |
| `AuditorInput.data_quality.status` / `flags` / `missing_evidence` / `source` | Workflow → Auditor 临时正式输入 | 共享 SCC 校验器生成的数据质量摘要，不接受别名或第二套质量状态。 |
| `AuditorInput.hard_risk_config.max_total_margin_ratio` | Workflow → Auditor 临时正式输入 | 主配置硬保证金上限的最小投影；Auditor 不接收或解释完整策略配置。 |
| `signal_snapshot.auditor.producer` / `audit_status` / `audit_verdict` / `audit_reason_codes` / `audited_at` | Auditor snapshot 摘要 | 独立审计生产者、状态、裁决、原因和时间。 |
| `signal_snapshot.auditor.independent_auditor_agent` / `pm_risk_gate_is_not_auditor` | Auditor snapshot 摘要 | 固定声明独立审计员身份及 PM 风险门不是 Auditor。 |
| `audit_payload.contract_version` / `producer` / `agent_name` / `recommendation_id` / `ticker` / `trading_date` / `config_id` | Auditor 完整 payload | 审计契约、生产者、推荐、产品、日期和配置身份。 |
| `audit_payload.audited_by` / `audited_at` | Auditor 完整 payload | 独立审计主体和审计时间。 |
| `audit_payload.source.pm_recommendation_id` / `final_action_contract_hash_source` / `contract_state_source` / `data_quality_source` | Auditor 来源 | 被审推荐 ID、唯一合约路径、具体合约事实来源和 SCC 数据质量来源。 |
| `audit_payload.boundary.auditor_does_not_modify_final_action_contract` / `auditor_does_not_create_trade_authority` / `trader_requires_approved_audit_verdict` | Auditor 边界 | Auditor 不改合约、不建权限且 Trader 只执行审计通过合约。 |
| `audit_payload.boundary.research_memory_not_consumed` / `auditor_reads_research_db` | Auditor 边界 | Auditor 不读取研究库，也不审计 PM 的学习消费过程。 |
| `audit_payload.contract_summary.final_action` / `current_lots` / `target_lots` / `lots_delta` / `contract_code` / `invalidation_present` / `requires_intraday_confirmation` / `can_execute_without_intraday_trigger` | Auditor 合约摘要 | 被审唯一合约的动作、手数、具体合约、失效边界和盘中确认权限摘要。 |
| `audit_payload.contract_summary.account_margin_ratio_before` / `current_ticker_margin_ratio` / `target_ticker_margin_ratio` / `incremental_margin_ratio` / `projected_total_margin_ratio` / `hard_max_total_margin_ratio` | Auditor 硬保证金投影 | 新增风险时，以账户已用保证金/权益形成当前组合比例，扣除当前品种已占比例后使用 FAC 目标品种比例计算增量及投影组合比例；投影超过主配置硬上限必须阻断。 |
| `audit_payload.semantic_state.lifecycle_state` / `requires_intraday_result` / `hard_block_reasons` / `soft_limit_reasons` / `semantic_errors` | Auditor 统一语义状态 | 公共 final-action 语义工具生成的生命周期、盘中结果要求及硬/软问题。 |
| `signal_snapshot.auditor` / `audit_payload.source` / `boundary` / `contract_summary` / `semantic_state` | Auditor 审计对象 | 对唯一 `final_action_contract` 的来源、只读边界、动作手数摘要和硬风险语义状态；不包含 PM 学习消费审计或融合解释审计。 |
| `signal_snapshot.auditor.audit_verdict` | Auditor snapshot 摘要 | Auditor 对唯一合法合约的最终裁决。 |
| `auditor_verdict` | Auditor 输出 / snapshot / audit payload | 对唯一合法合约的独立审计裁决。 |

### 16.5 Trader Phase2 执行字段

| 字段路径 | 生产与消费位置 | 固定含义 |
|---|---|---|
| `signal_snapshot.phase2_execution` / `execution_translation` / `execution_result` | Trader snapshot | Phase2 运行、合约翻译和最终执行事实；不得改写 PM 合约。 |
| `phase2_execution.mode` / `status` / `ticker` / `recommendation_id` / `reference_action` / `reference_lots` | Trader Phase2 | 执行模式、状态、产品、推荐引用及 PM 顶层参考动作/手数。 |
| `phase2_execution.last_checked_at` / `cutoff_datetime` / `finalize_untriggered` / `loop_iteration` / `reason` | Trader Phase2 | 最后检查、数据截止、未触发收口、循环次数和运行原因。 |
| `phase2_execution.current_lots_before` / `two_step_reversal` | Trader Phase2 | 执行前持仓及是否需要先退出再反向开仓。 |
| `phase2_execution.execution_contract` | Trader 执行摘要 | 从已审 `final_action_contract` 白名单提取的执行规则，不是第二张合约。 |
| `execution_contract.execution_profile` / `trigger_source` / `entry_trigger` / `invalidation` / `valid_until` | Trader 执行摘要 | PM 既定执行 profile、触发来源、入场、失效和有效期。 |
| `execution_contract.requires_intraday_confirmation` / `can_execute_without_intraday_trigger` / `authority_type` / `max_allowed_margin_ratio` / `reason_codes` | Trader 执行摘要 | 盘中确认、直接执行、权限、保证金和原因边界。 |
| `execution_contract.execution_action_value_preference` | Trader 执行摘要 | PM 已落地的 execution profile 偏好；Trader 不读取研究库或完整AEC。 |
| `phase2_execution.translated_decision.action` / `lots` / `contract_code` / `price` | Trader 翻译决策 | 合约翻译后的订单动作、手数、具体合约和价格。 |
| `phase2_execution.intraday_selection.decision` / `reason` / `base_price` / `base_datetime` / `base_price_source` / `signal_datetime` | Trader 盘中选择 | 盘中执行/等待/跳过结论、原因和价格时间基准。 |
| `intraday_selection.trigger_checked` / `trigger_passed` / `execution_failure_reason` / `missed_opportunity_flag` / `learning_writeback_contract` | Trader 盘中选择 | 条件 FAC 的15分钟触发检查、执行失败、错过机会及未来学习写回契约；`can_execute_without_intraday_trigger=true` 的直执行路径不得伪记为 Trader 再次检查了触发。 |
| `intraday_selection.price_chase_check.checked` / `passed` / `reason` / `gap_ratio` / `threshold` | Trader 追价检查 | 是否检查、是否通过、原因、跳空比例和配置阈值。 |
| `intraday_selection.features.error` / `underlying_code` / `contract_code` / `action` / `execution_mode` / `execution_profile` / `execution_contract` | Trader 盘中特征 | 数据错误、产品合约、动作及执行模式/profile/规则。 |
| `intraday_selection.features.signal_close` / `vwap` / `opening_range` / `signal_bars` / `eligible_signal_bars` / `execution_bars` | Trader 盘中特征 | 信号收盘、VWAP、开盘区间及信号/可用/执行 bar 数量。 |
| `intraday_selection.features.min_execution_volume` / `latest_execution_bar` / `finalize_untriggered` / `trigger_rule` / `chase_check` | Trader 盘中特征 | 最低成交量、最新执行 bar、未触发收口、触发规则和追价检查。 |
| `features.opening_range.high` / `low` / `minutes` / `start` / `complete_at` / `complete` / `bars` | Trader 开盘区间 | 开盘区间高低、窗口、起止、完整性和 bar 数。 |
| `features.chase_check.passed` / `reason` / `gap_ratio` / `threshold` | Trader 追价特征 | 追价过滤结果、原因、跳空比例和阈值。 |
| `phase2_execution.setup_execution_learning.consumer_scope` / `learning_lane` / `setup_type` / `opportunity_state` / `preferred_side` | Trader 执行学习上下文 | execution 学习消费者、lane、setup、机会状态和方向。 |
| `setup_execution_learning.final_contract_execution_fields` / `analyst_action_evidence_contracts` / `analyst_learning_scopes` | Trader 执行学习上下文 | 最终合约执行白名单、AEC 和学习范围的只读摘要。 |
| `setup_execution_learning.execution_contract_summary.profile` / `trigger_source` / `entry_trigger` / `invalidation` / `requires_intraday_confirmation` / `can_execute_without_intraday_trigger` / `authority_type` | Trader 执行学习摘要 | 实际执行 profile、触发、失效和权限摘要。 |
| `setup_execution_learning.learning_boundary.consumer_scope` / `trader_executes_only` / `execution_feedback_future_only` / `not_strategy_creation` / `learning_source` / `no_full_final_action_contract_mirror` | Trader 学习边界 | Trader 只执行、反馈仅影响未来、不生成策略且不镜像完整 PM 合约。 |
| `setup_execution_learning.phase2_status` / `no_trade_reason` / `intraday_selection` / `reason_family` | Trader 执行学习上下文 | Phase2 状态、不交易原因、盘中选择及原因家族。 |
| `phase2_execution.pm_plan_validation.passed` / `reason` / `validation_errors` / `required_for` / `source_type` / `contract_type` | Trader PM 计划校验 | PM 合约结构、来源和类型是否允许翻译。 |
| `pm_plan_validation.current_lots` / `target_lots` / `target_lots_after_validation` / `original_target_lots` / `contract_current_lots` / `actual_current_lots` / `contract_lots_delta` / `expected_lots_delta` | Trader PM 计划校验 | 合约、账户与校验后的当前/目标手数一致性。 |
| `pm_plan_validation.final_contract_execution_fields` / `contract_authority_audit` / `authority_consistency` / `business_boundary` | Trader PM 计划校验 | 执行字段白名单、权限和业务边界检查。 |
| `contract_authority_audit.authority_type` / `authority_decision` / `max_allowed_margin_ratio` / `reason_codes` / `open_action_evidence` / `strong_current_evidence` / `watch_for_trigger_block` / `conditional_trigger_authority` / `requires_intraday_confirmation` | Trader 权限审计 | 从唯一合约提取的最终入场权限事实。 |
| `authority_consistency.passed` / `reason` / `selected_authority` / `sources` / `business_boundary` | Trader 权限一致性 | 唯一合约权限是否自洽及其来源；不得在冲突镜像中自行选择。 |
| `authority_consistency.sources[].source` / `authority_type` / `authority_decision` / `open_action_evidence` / `strong_current_evidence` / `max_allowed_margin_ratio` | Trader 权限来源 | 权限来源和关键权限字段。 |
| `phase2_execution.contract_execution_observation.signal_invalidation_observed` / `exit_policy_required` / `exit_policy_reason` / `business_boundary` | Trader 合约观察 | 盘中是否触及失效、是否要求退出策略及其边界。 |
| `phase2_execution.entry_authority_gate.status` / `reason` / `current_lots` / `target_lots` / `business_boundary` | Trader 入场安全闸 | 条件新增风险在触发后是否允许进入下单安全检查。 |
| `phase2_execution.exit_policy.enabled` / `exit_required` / `target_lots` / `reason` / `policy` / `same_direction_supported` / `days_held` / `is_probe` | Trader 退出策略 | 已审合约下的止损、止盈、时间退出和 probe 持仓事实。 |
| `phase2_execution.entry_timing.entry_action_family` / `opening_range` / `target_lots_source` | Trader 入场时机 | 入场动作家族、开盘区间和目标手数来源。 |
| `phase2_execution.execution_simulation.base_price` / `base_price_source` / `base_price_date` / `open_price` / `prev_close_price` / `warning_message` | Trader 执行模拟 | 回测、模拟盘和实盘共用的价格基准与警告。 |
| `execution_translation.translated_orders` / `rewrite_reasons` / `reference_action` / `reference_lots` | Trader 翻译事实 | 翻译订单、确定性改写原因和参考动作/手数。 |
| `translated_orders[].stage` / `action` / `lots` / `contract_code` / `price` | Trader 翻译订单 | 单条订单阶段、动作、手数、合约和价格。 |
| `execution_translation.signal_lifecycle.horizon_class` / `expected_horizon_days` / `entry_trigger` / `invalidation_level` / `atr_stop_distance` / `setup_type` / `market_regime` | Trader 信号生命周期 | 仅从已审计 `final_action_contract` 白名单抽取的PM最终期限、触发、失效、止损、setup和市场状态；不得从SCC重新选择或读旧顶层analyst snapshot。 |
| `execution_translation.signal_lifecycle.target_price` | Trader运行时派生 | 仅在Trader存在合法输入时才允许派生；不是AEC、SCC或PM必传事实。当前无合法 `target_return` 生产者时不得伪造。 |
| `execution_translation.phase2_order_plan.current_lots` / `target_lots` / `action` / `lots` / `contract_code` / `price` | Trader Phase2 订单计划 | 最终合约翻译出的当前/目标手数和订单。 |
| `phase2_order_plan.account_equity` / `current_price` / `risk_level` / `cashflow_ratio` / `current_margin_ratio` / `max_total_margin_ratio` / `max_single_margin_ratio` / `remaining_margin` | Trader 下单安全事实 | 下单时账户权益、价格、风险等级和保证金边界。新增风险统一按 `projected_total_margin=current_account_margin-current_ticker_margin+target_ticker_margin`、`incremental_margin=max(0,target_ticker_margin-current_ticker_margin)` 检查；不得用目标品种总保证金重复占用已有持仓，reduce/exit 的新增风险保证金为0。 |
| `phase2_order_plan.signal_lifecycle` / `execution_contract` / `consistency_diagnostics` | Trader Phase2 订单计划 | 生命周期、执行规则和动作/手数一致性诊断。 |
| `execution_translation.final_action_contract_source.source` / `contract_type` / `final_action` / `current_lots` / `target_lots` / `lots_delta` | Trader 合约来源 | 明确订单目标只来自唯一最终合约。 |
| `execution_translation.auditor_verdict.producer` / `audit_status` / `audit_verdict` / `audit_reason_codes` / `audited_by` / `audited_at` | Trader 审计摘要 | Trader 执行前读取的独立审计结果。 |
| `execution_translation.execution_block` | Trader 翻译事实 | 硬风控、市场规则或执行条件阻断原因。 |
| `execution_translation.final_execution_basis.base_price` / `base_price_source` / `base_price_date` / `open_price` / `prev_close_price` / `execution_price` / `execution_price_basis` | Trader 最终执行基准 | 实际执行价格及其组成依据。 |
| `final_execution_basis.slippage_model` / `slippage_ticks` / `slippage_amount` / `intraday_execution` / `signal_lifecycle` | Trader 最终执行基准 | 滑点、盘中选择和生命周期事实。 |
| `final_execution_basis.execution_learning_fields.trigger_checked` / `trigger_passed` / `price_chase_check` / `execution_failure_reason` / `missed_opportunity_flag` | Trader 执行学习字段 | 供 Researcher 使用的触发、追价、失败和错过机会事实。 |
| `execution_translation.market_rule_block.limit_lock` / `contract_expiry_guard` | Trader 市场规则阻断 | 涨跌停和到期/交割规则检查结果。 |
| `limit_lock.status` / `limit_up` / `limit_down` / `trade_date` / `ticker` / `enabled` / `action` / `execution_price` / `tolerance_ticks` / `minimum_tick` / `blocked` / `reason` / `side` / `limit_price` | Trader 涨跌停检查 | 涨跌停价格、容差、方向及是否阻断。 |
| `contract_expiry_guard.enabled` / `action` / `contract_code` / `trading_date` / `source_type` / `blocked` / `reason` / `status` / `last_trade_date` / `days_to_last_trade` / `delivery_month` / `days_to_delivery_month` | Trader 到期检查 | 具体合约最后交易日、交割月距离及是否允许当前动作。 |
| `execution_result.outcome` / `status` / `transaction_count` / `actual_action` / `actual_lots` / `warning_message` | Trader 最终执行结果 | 执行结果、状态、真实成交数量、实际动作/手数和警告；未触发、未成交、失效和市场规则阻断只能落在本结构，不得写入 `futures_transactions`。 |
| `execution_result.actual_transactions[]` | Trader 最终执行结果 | 当次 recommendation 生成的真实成交摘要列表。 |
| `actual_transactions[].action` / `lots` / `contract_code` / `execution_price` / `execution_phase` | Trader 实际成交摘要 | 单笔成交动作、手数、合约、价格和执行阶段。 |
| `execution_result.no_trade_reason` / `no_trade_reason_category` | Trader 未成交结果 | 未成交原因及标准化分类。 |
| `no_trade_reason_category.reason` / `category` / `category_label` / `category_description` / `source` | Trader 未成交分类 | 原因、分类代码、标签、说明和来源。 |
| `execution_result.execution_learning_trace.consumer_scope` / `learning_lane` / `execution_retrieval_key` | Trader 执行学习 trace | execution 学习消费者、lane 和未来检索键。 |
| `execution_learning_trace.outcome` / `status` / `no_trade_reason` / `no_trade_reason_category` / `actual_transaction_count` | Trader 执行学习 trace | 执行结果、未成交分类和真实成交数量。 |
| `execution_learning_trace.turn_into_memory` / `not_direction_evidence` / `execution_learning_type` / `timing_strategy_question` | Trader 执行学习 trace | 是否形成未来记忆、非方向证据、学习类型和时机研究问题。 |
| `execution_result.consistency_diagnostics.status` / `issues` / `phase2_plan_action` / `phase2_plan_lots` / `actual_action` / `actual_lots` / `no_trade_reason` | Trader 执行一致性 | Phase2 订单计划与实际成交/未成交的一致性。 |
| `audit_payload.trade_contract_audit.audit_boundary` / `single_source_of_trade_truth` / `candidate_sources_do_not_bypass_contract` / `contract_version` | Trader 执行审计 | transaction 审计只读唯一合约且候选不得绕过。 |
| `trade_contract_audit.final_action` / `authority_type` / `authority_decision` / `open_action_evidence` / `strong_current_evidence` / `current_lots` / `target_lots` / `lots_delta` | Trader 执行审计 | 被执行合约的动作、权限、证据和手数。 |
| `trade_contract_audit.target_margin_ratio_estimate` / `max_allowed_margin_ratio` / `reason_codes` / `execution_profile` / `execution_requirement` | Trader 执行审计 | PM 目标保证金、允许上限、原因和执行要求。 |
| `trade_contract_audit.pm_plan_validation_passed` / `pm_plan_validation_reason` / `authority_consistency_reason` / `business_boundary` | Trader 执行审计 | PM 计划及权限一致性检查结果。 |
| `audit_payload.independent_auditor.producer` / `audit_status` / `audit_verdict` / `audit_reason_codes` / `audited_at` | Trader 执行审计 | 保留的独立 Auditor 摘要；不得替代或改写原审计事实。 |
| `audit_payload.trade_contract_audit` / `execution_translation` / `execution_result` / `phase2_execution` | Trader 对原 Auditor payload 的追加字段 | Trader 必须以原始完整 Auditor payload 为基底追加执行事实；`producer/source/boundary/hard_risk_reasons/soft_risk_reasons/contract_summary/semantic_state` 保持原值。 |
| `futures_transactions.llm_prompt` / `llm_prompt_artifact_path` / `llm_prompt_sha256` / `llm_prompt_size` / `llm_prompt_summary_json` | transaction 历史物理列 | 正式交易写入口固定写空值/NULL/0，不属于 `FuturesTransaction` 契约；任何非空 prompt 输入必须拒绝。 |
| `phase2_execution.translated_decision` / `intraday_selection` / `setup_execution_learning` / `pm_plan_validation` / `contract_execution_observation` / `entry_authority_gate` / `exit_policy` / `entry_timing` / `execution_simulation` | Trader Phase2 对象 | 翻译、盘中选择、执行学习、计划校验、合约观察、权限、退出、时机和模拟对象。 |
| `execution_contract_summary` / `learning_boundary` | `phase2_execution.setup_execution_learning` | 执行规则摘要和 Trader 学习权限边界对象。 |
| `execution_translation.final_action_contract_source` / `phase2_order_plan` / `final_execution_basis` / `market_rule_block` | Trader 翻译对象 | 唯一合约来源、订单计划、最终价格依据和市场规则对象；不包含从原始AEC重新选择方向的对象。 |
| `final_execution_basis.execution_learning_fields` | Trader 最终执行基准 | 从盘中选择提取的执行学习字段对象。 |
| `audit_payload.trade_contract_audit` | Trader 执行审计 | 交易执行对唯一 PM 合约的只读摘要。 |

### 16.6 换约与强制风控 Recommendation

| 字段路径 | 生产与消费位置 | 固定含义 |
|---|---|---|
| `signal_snapshot.rollover_policy.mode` / `reason` / `execution_type` / `strategy_target_lots` / `close_lots` / `open_lots` / `from_contract` / `to_contract` | 换约链 / Trader | 换约模式、原因、执行类型、策略目标仓位及旧约平仓/新约开仓事实。 |
| `signal_snapshot.source_type` / `operation_reason` / `risk_status` | 强制风控 Recommendation | 非策略来源、强制操作原因和账户风险状态。 |
| `signal_snapshot.margin_ratio` / `current_margin_ratio` / `trigger_margin_ratio` / `post_reduce_target_margin_ratio` | 强制风控 Recommendation | 当前、触发和强制减仓后目标保证金比例。 |
| `signal_snapshot.account_equity` / `total_margin` / `total_unrealized_pnl` | 强制风控 Recommendation | 触发强制风控时的权益、总保证金和浮动盈亏。 |
| `signal_snapshot.underlying_code` / `contract_code` / `risk_price` / `risk_price_source` / `risk_price_datetime` | 强制风控 Recommendation | 被处理品种、具体合约和风险价格来源时间。 |
| `signal_snapshot.forced_risk_boundary` | 强制风控 Recommendation | 固定声明强制风控是非策略操作，必须隔离 alpha 学习。 |
| `audit_payload.forced_risk_scope` / `strategy_learning_boundary` | 强制风控审计 | 强制风控范围及不得进入策略学习的边界。 |
| `signal_snapshot.rollover_policy` | 换约 Recommendation | 换约链生成并由 Trader 执行的换约策略对象。 |

## 17. 配置参数与 Python 消费函数

登记规则：

- YAML 只保留真实改变运行行为的参数；纯说明、边界宣言、版本说明放在本文或机制文档，不伪装成配置。
- 固定参数名必须与消费函数的 `dict.get` / 字段读取名一致。动态 ticker、sector、factor、template 映射用 `*` 或 `**` 表示参数族，族内键由同一函数按运行上下文读取。
- 每个配置参数路径必须对应至少一个真实存在的 Python 消费函数。配置路径与函数存在性由 `test_config_parameter_mapping.py` 静态验证。

| 配置参数路径 | Python 消费函数 | 固定含义 |
|---|---|---|
| `src/config/analyst_prior_profiles.yaml::dynamic_bounds.*` | `src/util/config_normalizer.py::_apply_analyst_weight_catalog` | 分析师动态权重的最小值与最大值。 |
| `src/config/analyst_prior_profiles.yaml::profiles.**` | `src/util/config_normalizer.py::_profile_weights` | 按战略视图/日频时机与 sector 读取 technical、fundamental、commodity_news 冷启动先验。 |
| `src/config/analyst_prior_profiles.yaml::applicability_profile.**` | `src/agents/decision_team/portfolio_manager.py::_quality_aware_fusion_context` | 按分析师、周期、sector 和市场状态动态读取适用性乘数；只调整证据质量。 |
| `src/config/data_factor_policy_catalog.yaml::fundamental_quality_control.**` | `src/tools/agent_tools/analysis/analyst_dynamic_weights.py::_apply_fundamental_quality_adjustment` | Finoview 覆盖、陈旧、缺失阈值及对应基本面权重乘数。 |
| `src/config/data_factor_policy_catalog.yaml::pandaai_extra_data.**` | `src/agents/analysis_team/fundamental.py::fundamental_agent` | PandaAI 扩展因子启用、可见日期、回看窗口和分析因子集合。 |
| `src/config/data_factor_policy_catalog.yaml::factor_data.**` | `src/tools/agent_tools/analysis/analyst_data_usage.py::prefetch_local_daily_data` | Finoview 与本地新闻的数据目录、开关和盘前可见性入口。 |
| `src/config/dev.yaml::config_catalogs.*` | `src/util/config_normalizer.py::normalize_config` | 主配置到各业务 catalog 的唯一加载索引。 |
| `src/config/dev.yaml::runtime.phase1.**` | `src/graph/workflow.py::_phase1_acceleration_enabled` | Phase1 并行、预取、计时及分析师写库方式。 |
| `src/config/dev.yaml::runtime.data_cache.**` | `src/tools/agent_tools/analysis/analyst_data_usage.py::prefetch_pandaai_daily_data` | 本地数据与 PandaAI 日级缓存预取开关。 |
| `src/config/dev.yaml::control_governance.protocol_governor.*` | `src/tools/agent_tools/control/pg_pre_backtest_acceptance.py::_config_mapping_check` | PG 不得创建交易权限、修改手数/保证金或执行订单的验收开关。 |
| `src/config/dev.yaml::exp_name`、`src/config/dev.yaml::market_type`、`src/config/dev.yaml::tickers`、`src/config/dev.yaml::planner_mode`、`src/config/dev.yaml::workflow_analysts` | `src/graph/workflow.py::__init__` | 实验身份、市场、交易宇宙和固定 workflow 编排入口。 |
| `src/config/dev.yaml::cashflow` | `src/run/proposal.py::main` | 新建或显式重建实验账户时的初始现金。 |
| `src/config/dev.yaml::max_total_margin_ratio` | `src/tools/agent_tools/control/pg_pre_backtest_acceptance.py::_config_mapping_check` | 账户总保证金硬上限。 |
| `src/config/dev.yaml::position_budget_policy.**` | `src/agents/decision_team/portfolio_manager.py::_position_budget_policy_config` | Step5/Step6 的 probe、正常、deployable、exceptional 资金层级及单品种约束。 |
| `src/config/dev.yaml::analyst_weight_policy.**` | `src/agents/decision_team/portfolio_manager.py::_final_contract_authority` | 静态分析师先验的证据路由边界；不得创建交易权限。 |
| `src/config/dev.yaml::risk_control.**` | `src/agents/decision_team/portfolio_manager.py::check_risk_level` | 账户风险等级阈值及不同风险等级的仓位缩放/单仓上限。 |
| `src/config/dev.yaml::net_exposure_control.**` | `src/agents/decision_team/portfolio_manager.py::_resolve_net_exposure_control` | Step5 计划净敞口上限及对称缩放方式。 |
| `src/config/dev.yaml::drawdown_control.**` | `src/agents/decision_team/portfolio_manager.py::_apply_drawdown_and_ticker_loss_control` | 回撤冷却、恢复预算阶梯和恢复确认条件。 |
| `src/config/dev.yaml::capital_utilization_control.**` | `src/tools/agent_tools/decision/pm_capital_deployment_policy.py::_apply_capital_utilization_control` | Step5 资金利用目标、强机会部署条件、储备比例和成熟学习释放约束。 |
| `src/config/dev.yaml::execution.limit_lock.**` | `src/tools/agent_tools/execution/trader_futures_execution.py::_build_market_rules_audit` | 涨跌停锁定检查参数。 |
| `src/config/dev.yaml::execution.dynamic_margin.**` | `src/tools/agent_tools/execution/trader_futures_execution.py::_resolve_dynamic_margin_rate` | 动态保证金来源及静态缓存回退。 |
| `src/config/dev.yaml::execution.contract_expiry_guard.**` | `src/tools/agent_tools/execution/trader_futures_execution.py::_build_market_rules_audit` | 交割月、临近到期和换月的新仓限制。 |
| `src/config/dev.yaml::execution.intraday_confirmation.**` | `src/tools/agent_tools/execution/trader_intraday_execution.py::intraday_confirmation_enabled` | 盘中确认频率、开盘区间、追价、成交量与循环检查参数。 |
| `src/config/dev.yaml::rollover.*` | `src/agents/execution_team/trader.py::_reconcile_rollover_with_strategy_target` | 换月与策略目标仓位的协调方式。 |
| `src/config/dev.yaml::audit.*` | `src/agents/analysis_team/technical.py::technical_agent` | 技术分析运行审计细节开关。 |
| `src/config/dev.yaml::analyst_llm.**` | `src/tools/agent_tools/analysis/analyst_quality.py::apply_signal_quality_gate` | 分析师模型路由、报告和低可交易性/陈旧基本面置信度处理。 |
| `src/config/dev.yaml::llm.**` | `src/llm/inference.py::_normalize_llm_config` / `agent_call` | LLM provider、model、并发、结构化输出、错误策略和密钥入口；失败策略只允许重试后抛错、限流退避或立即抛错，禁止 `retry_then_default`、默认Pydantic输出及原始provider错误外泄。 |
| `src/config/execution_commission_catalog.yaml::commission.**` | `src/tools/agent_tools/execution/trade_futures_commission.py::resolve_commission_rule` | 按 underlying 和开平/平今方式读取真实手续费规则及舍入精度。 |
| `src/config/execution_slippage_catalog.yaml::slippage.**` | `src/tools/agent_tools/execution/trader_futures_execution.py::_get_slippage_ticks` | 滑点模型、默认 ticks 和 underlying 动态映射。 |
| `src/config/execution_exit_policy_catalog.yaml::exit_policy.**` | `src/tools/agent_tools/execution/trader_execution_exit_policy.py::resolve_exit_policy_config` | 默认、sector 和 setup/template 的止损、止盈与时间退出参数。 |
| `src/config/finoview_factor_catalog.yaml::required_groups`、`src/config/finoview_factor_catalog.yaml::ticker_overrides.**`、`src/config/finoview_factor_catalog.yaml::context_ticker_overrides.*` | `src/tools/agent_tools/analysis/analyst_finoview_factors.py::build_local_finoview_availability_audit` | 可交易快照要求的因子组及辅助字段到交易品种的动态映射。 |
| `src/config/finoview_factor_catalog.yaml::factor_group_overrides.**`、`src/config/finoview_factor_catalog.yaml::frequency_overrides.*`、`src/config/finoview_factor_catalog.yaml::release_lag_days.*`、`src/config/finoview_factor_catalog.yaml::freshness_threshold_days.*` | `src/tools/agent_tools/analysis/analyst_finoview_factors.py::build_factor_catalog`、`resolve_finoview_visibility_cutoffs`、Router fundamentals | 因子名到业务组、频率、正式交易日发布滞后和新鲜度阈值的动态映射；Router格式化输入与factor snapshot共用同一选择器。 |
| `src/config/learning_policy_catalog.yaml::opportunity_ranking_learning_policy.**` | `src/tools/agent_tools/research/research_memory_writers.py::_write_opportunity_ranking_learning_events` | rank 表现学习事件的样本门槛、有效期、输入字段和允许/禁止影响。 |
| `src/config/learning_policy_catalog.yaml::strategy_memory.**` | `src/database/sqlite_helper.py::_strategy_memory_thresholds` | 策略记忆回看、过期、样本、胜率、净收益和 PM 风险门阈值。 |
| `src/config/learning_policy_catalog.yaml::learning.**` | `src/tools/agent_tools/research/research_learning.py::apply_researcher_learning`、`src/tools/agent_tools/research/research_memory_writers.py::_write_adaptive_policy_state`、`src/tools/agent_tools/research/research_memory_writers.py::_write_contextual_rule_calibration_state`、`src/tools/agent_tools/research/research_memory_writers.py::_write_provisional_policy_state` | Researcher 的 profile、action-value、overlay、策略晋升、情境校准、哨兵、episode、反事实和因果研究参数；只影响未来学习。 |
| `src/config/learning_policy_catalog.yaml::analyst_business_quality.**` | `src/agents/decision_team/portfolio_manager.py::_quality_multiplier` | 分析师业务质量的 probe/deployable 阈值与软仓位乘数。 |
| `src/config/learning_policy_catalog.yaml::signal_quality.**` | `src/tools/agent_tools/research/research_memory_writers.py::_write_neutral_accountability_state` | Neutral 责任字段、结构化学习和反事实跟踪参数。 |
| `src/config/learning_policy_catalog.yaml::learning_context.**` | `src/tools/agent_tools/analysis/analyst_learning_context.py::build_learning_context` | 分析师学习上下文的条数、字符数、缓存和跨品种回退预算。 |
| `src/config/learning_policy_catalog.yaml::learning_retention.**` | `src/database/sqlite_helper.py::_cleanup_learning_retention_with_cursor` | 学习明细/聚合表保留天数、最大行数和清理表集合。 |
| `src/config/portfolio_policy_catalog.yaml::market_confirmation.**` | `src/agents/decision_team/portfolio_manager.py::_apply_market_confirmation_control` | 新仓确认、冲突、数据缺口与受控 probe 的证据阈值。 |
| `src/config/portfolio_policy_catalog.yaml::directional_override_control.**` | `src/agents/decision_team/portfolio_manager.py::_apply_directional_override` | 高质量空头方向覆盖的分数、置信度、强度、边际和 probe 上限。 |
| `src/config/portfolio_policy_catalog.yaml::pm_risk_gate.**` | `src/tools/agent_tools/decision/pm_risk_gate.py::plan` | PM 内部确定性风险门、质量门、历史归因和冷启动参数。 |
| `src/config/portfolio_policy_catalog.yaml::auditor.**` | `src/agents/decision_team/auditor.py::audit` | Auditor 是否启用；其余审计规则由最终合约语义固定实现。 |
| `src/config/portfolio_policy_catalog.yaml::trade_frequency_control.**` | `src/tools/agent_tools/decision/pm_risk_gate.py::_evaluate_performance` | 交易频率、弱/严重表现和 churn 的软风险参数。 |
| `src/config/portfolio_policy_catalog.yaml::ticker_performance_control.**`、`src/config/portfolio_policy_catalog.yaml::ticker_loss_control.**` | `src/agents/decision_team/portfolio_manager.py::_apply_drawdown_and_ticker_loss_control` | 品种近期表现与连续亏损的软缩放和恢复参数。 |
| `src/config/portfolio_policy_catalog.yaml::dynamic_weights.**` | `src/tools/agent_tools/analysis/analyst_dynamic_weights.py::_apply_weight_constraints` | 分析师动态权重启用状态和上下界。 |
| `src/config/portfolio_policy_catalog.yaml::portfolio_manager.holding_rebalance_control.**` | `src/agents/decision_team/portfolio_manager.py::_apply_holding_rebalance_control` | 持仓生命周期、观察候选、成熟 alpha、日频可交易性和周期一致性参数。 |
| `src/config/portfolio_policy_catalog.yaml::portfolio_manager.adaptive_fusion.**`、`src/config/portfolio_policy_catalog.yaml::portfolio_manager.quality_aware_fusion.**` | `src/agents/decision_team/portfolio_manager.py::_quality_aware_fusion_context` | 证据质量融合、scorecard、学习权重、状态乘数和质量 sizing 参数。 |
| `src/config/product_price_behavior_profiles.yaml::profile_contract_version`、`src/config/product_price_behavior_profiles.yaml::required_tickers`、`src/config/product_price_behavior_profiles.yaml::profiles.**` | `src/tools/agent_tools/analysis/analyst_product_price_behavior_profile.py::load_product_price_behavior_profiles` | 商品价格行为 profile 的契约版本、覆盖品种和按 ticker 动态分析参数。 |
| `src/config/rank_score_policy.yaml::rank_score_policy.rank_score.**` | `src/tools/agent_tools/decision/pm_full_market_capital_deployment.py::_rank_score_components_for_row` | Step5 唯一 rank 的七个分项权重、学习修正边界、资金效率和风险扣分参数；不改变交易属性。 |

## 18. 静态验证要求

必须保留静态测试：

- 扫描生产代码、schema、配置、评估脚本。
- 运行时业务字段必须属于本文字段表。
- PM、Auditor、Trader、Accountant、Reviewer、Researcher、评估脚本不得读取未登记字段来推导交易、结算、复盘或学习结果。

任何新增字段必须先写入本文，再进入代码；否则视为语义漂移。
