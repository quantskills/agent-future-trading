# Matrix Chain Contract

更新时间：2026-07-15

本文是 AgentQuant 全链路契约矩阵。它只回答一件事：每个关键系统事实由谁生产、落在哪里、谁消费、谁审计、什么条件必须 hard fail、什么条件只进入 diagnostics。

本文不替代：

- `docs/mechanism_multiagents.md`：智能体角色、阶段、权限边界。
- `docs/matrix_field_semantics.md`：字段名、字段含义、字段权限。
- `docs/matrix_action_canonical.md`：action-value 动作 canonical 矩阵。
- `docs/agent_pm.md`：PM 六步、最终合约、自检细节。
- `docs/workflow.md`：workflow 编排、传递、保存、阻断。
- `docs/mechanism_research.md`：复盘、研究、记忆、学习边界。

本文已经接入可执行闸门：
- `src/tools/agent_tools/control/pg_contract_coverage_audit.py` 按本文关键契约行执行六维 coverage：`producer`、`physical_landing`、`consumer`、`role_check`、`real_path_test`、`mechanism_doc`。pre-backtest readiness 与 daily PG 物理事实审计由各自正式入口独立执行，不是 coverage 的附加维度。
- 回测前检测通过 `src/tests/test_*.py` 的通用不变量和真实路径测试证明系统就绪；历史问题只能作为测试样本来源，不能成为回测前检测的设计中心。
- `src/tools/agent_tools/control/pg_system_invariants.py` 按本文第 5 节输出 daily PG hard fail 边界和 diagnostics 边界。
- `src/run/control/pre_backtest_acceptance.py` 与 `src/run/control/system_invariant_audit.py` 是本文对应的只读控制入口。

## 1. 使用规则

修改生产端、自检、回测前验收、日终 PG 审计、Research 写入、PM artifact、Trader/Reviewer/Researcher artifact 时，必须先定位本文对应行，再按以下八项开发同步清单处理。该清单是完整开发顺序，不等同于 `pg_contract_coverage_audit.py` 的六个可执行 coverage 维度：

1. 生产端。
2. artifact / DB 落点。
3. 消费端。
4. PM self-check / 角色内部校验；由角色自身和回测前测试负责，不交给 daily PG 复查。
5. pre-backtest fixture gate。
6. daily PG audit。
7. 真实路径测试。
8. 机制文档。

六维 coverage 与上述清单的关系固定为：生产端对应 `producer`，物理落点对应 `physical_landing`，消费端对应 `consumer`，角色自身校验对应 `role_check`，真实生产链行为测试对应 `real_path_test`，正式机制文档对应 `mechanism_doc`。pre-backtest fixture/readiness 和 daily PG 分别由回测前十项与每日七项门禁执行，不能用静态 coverage 字符串替代，也不能让 daily PG 复查智能体内部机制。

缺本文矩阵行的系统事实不得进入代码、artifact、DB、prompt 和审计。PG 的输入、判定和输出也不得例外；任何未在 `matrix_field_semantics.md` 登记的字段都不能通过 `metadata`、`payload`、JSON 容器或临时字典键进入旁路报告。

## 2. 判定边界

| 类型 | 定义 | 处理 |
|---|---|---|
| hard fail | 系统契约断裂、字段语义漂移、artifact 污染、越权、前视、阶段断链、交易不来自唯一合约 | 停止回测，先修系统 |
| diagnostics | 物理链路完整但策略效果弱、信号弱、学习为空、rank 低、合法 observe 无交易偏向、当天亏损 | 不停止回测，进入策略分析 |
| 禁止项 | 控制组生成交易动作、下游改 PM 合约、Research 改当天事实、Trader 读研究库下单 | 直接 hard fail |

## 3. 全链路契约矩阵

| 契约 / 字段群 | 生产者 / 阶段 | 输入 | 输出 | artifact / DB 落点 | 下游消费者 | 审计点 | hard fail 条件 | diagnostics 条件 |
|---|---|---|---|---|---|---|---|---|
| `action_evidence_contract` | `technical` / `fundamental` / `commodity_news`，Phase1 | 截止点内行情、基本面、新闻、商品 profile、分析师校准摘要 | 三份经同一共享校验的结构化预测证据；必需市场事实不可用时仍由三个分析师各自产生合法中性 AEC；基本面或新闻无当日新增只影响本分析师 | Workflow 保存三份 signal 表记录；`artifact_json`和分析师报告都只保存同一AEC；内存metadata保存前仅AEC、保存后仅AEC与真实ID | `signal_collector`、Reviewer、Researcher | 共享 AEC 校验；pre-backtest structured IO；daily PG 只核对实际进入策略路径时物理来源记录存在 | 含手数、仓位比例、保证金、`final_action_contract`、`opportunity_rank`、最终交易动作；缺必填字段/类型/数据来源层级；伪造方向、profile、trigger、权限或市场事实；携带自由文本、LLM路由、内部参数、校准过程、学习检索上下文或其他metadata | 证据弱、证据冲突、基本面或新闻无新增、合法中性数据不可用状态 |
| `product_profile_evidence` | 三类分析师，Phase1 | `product_price_behavior_profiles.yaml`、行情与品种上下文 | profile 使用痕迹、支持证据、冲突证据、缺失确认项 | 仅 `action_evidence_contract.product_profile_evidence`；SCC source/evidence items只做已登记索引 | `signal_collector`、PM 证据上下文、Reviewer、Researcher | contract coverage；pre-backtest artifact boundary | profile 字段含交易授权、手数、rank、PM reason code、最终动作，或在AEC外复制第二份 | profile 不相关、profile 证据不足 |
| `fusion_evidence` | 分析师质量工具，Phase1 | 分析师结构化证据、profile、数据质量 | 证据强弱、时效、一致性、冲突、确认需求 | 分析师 `metadata.action_evidence_contract.fusion_evidence`；SCC 只在 `source_contracts[].action_evidence_contract.fusion_evidence` 保留同一份，并另行形成顶层跨分析师 `evidence_fusion` 汇总 | `signal_collector`、PM scorecard、Reviewer、Researcher | contract coverage；Reviewer/Researcher 事实归因 | 在 SCC source 同级或 evidence item 复制第二份 fusion；fusion 字段直接授权交易、替代 PM score/rank、进入 Trader 执行权限 | evidence_fusion 冲突高、确认需求多 |
| `signal_collection_contract` | `signal_collector`，Phase1 | Workflow 已保存并取得真实 `signal_record_id` 的三份 AEC | 唯一统一结构化预测证据包，保留 `source_agent=signal_collector` 与 `collector_decision_boundary=no_trade_authority` | workflow state `signal_collection_contract`；PM final `signal_snapshot.signal_collection_contract` | PM、Reviewer、Researcher | 共享 SCC 校验；pre-backtest SCC contract；daily PG 只核对策略路径物理落地完整性；contract coverage | Collector 生成 AnalystSignal/虚假 ID；缺或重复分析师；缺 SCC；source_agent/boundary 非法；SCC 含 PM 字段、手数、rank、资金部署或交易动作 | 分析师冲突、证据弱、缺确认项 |
| `signal_snapshot.signal_collection_contract` | PM Step6 返回、保存层物理化，Phase1 | workflow state 原始 SCC | 原始 SCC 快照 | `futures_recommendation.signal_snapshot`；recommendation artifact | Reviewer、Researcher | daily PG 只核对策略路径物理落地完整性 | PM 重建、补造、改写 SCC；只保存 SCC ref；完整 SCC 缺失；source_agent/boundary 错 | SCC 证据弱、冲突多 |
| `final_action_contract` | PM Step6，Phase1 | SCC、持仓、账户、Router 截止点内具体合约事实、配置、有效学习、PM 工具输出 | 唯一可执行策略合约：`final_action`、`current_lots`、`target_lots`、`lots_delta`、具体合约、执行触发、风险边界、证据摘要 | `futures_recommendation.signal_snapshot.final_action_contract`；recommendation artifact | Auditor、Trader、Reviewer、Researcher | PM self-check；Auditor；daily PG 只核对唯一交易来源及执行成交事实 | 新增风险缺合法具体合约；默认/猜测合约；已有持仓未绑定持仓合约；缺 final contract；PM 自检失败；顶层 action/lots 或 `lots_delta` 不一致；PM 中间态污染；第二套交易计划 | no trade、rank 低、资金预算不足、信号弱 |
| `pm_six_step_trace` | PM Step6，Phase1 | 唯一最终 `final_action_contract` 与最终 `FuturesRecommendation` | `step6_contract_generation_check`、`pm_contract_self_check` | `signal_snapshot.pm_six_step_trace`；recommendation artifact | Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试 | 缺任一最终检查；check failed；Step1-5 中间状态、早期生命周期和跨步骤比较结果进入 trace | 最终检查通过后的 no trade、学习为空、候选降级 |
| `artifact_phase_boundary` | 各智能体对外 artifact 写入端 | 本角色授权事实、上游正式输出摘要 | 阶段白名单 artifact | signal / recommendation / audit / transaction / settlement / reviewer / research artifact | 下游智能体、contract coverage | pre-backtest artifact boundary；daily PG 只检查对应物理结果是否可读取 | 下游 artifact 复制完整上游合约；Trader 保存 PM 学习/rank；Reviewer 写最终 action-value；Researcher 改当天交易事实；Accountant 保存学习字段 | artifact 摘要字段不足、诊断信息较少 |
| `lifecycle_learning_trace.decision_learning_rows` | PM Step6 合约装配 | 最终 `final_action/current_lots/target_lots`、contract lifecycle、有效 action-value | Step6 final 决策层学习 rows | `final_action_contract.evidence_used.lifecycle_learning_trace.pm_final_contract_lifecycle_trace.decision_learning_rows` | PM self-check、Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试 | 复制 Step4 临时 router rows；open/rank 混入 hold/reduce/exit/execution/conditional_monitor；reduce_exit 混入 open/add/execution；hold 混入非 hold；conditional_monitor 混入非 conditional_monitor；trace lifecycle 与最终合约不一致 | 对应生命周期没有有效学习 |
| `lifecycle_learning_trace.trigger_profile_learning_rows` | PM Step6 合约装配 | execution / trigger / profile 类 action-value | 触发画像与执行质量学习 rows | `final_action_contract.evidence_used.lifecycle_learning_trace.trigger_profile_learning_rows` | PM self-check、Reviewer、Researcher | PM self-check | execution/profile 学习 direct-to-rank；改变 final_action、target_lots、lots_delta、rank、资金部署 | execution 学习为空、触发质量弱 |
| `learning_used` | PM Step6 合约装配 | 有效 action-value、检索摘要、剔除/降级诊断、memory requirements | PM 最终合约学习证据容器 | `final_action_contract.learning_used` | PM self-check、Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试；contract coverage | `learning_used` 含第二套交易计划；formal 与 diagnostics 混层；缺 memory requirements；execution/profile 学习直接改 rank/手数/final_action | 没有命中有效学习、命中层级弱 |
| `learning_used.alpha_setup_action_values` | PM Step6 合约装配 | `decision_memory_retrieval` 返回的正式 canonical action-value | PM 实际声明消费的正式 action-value 主证据列表 | `final_action_contract.learning_used.alpha_setup_action_values` | PM self-check、Reviewer、Researcher | PM self-check purity；pre-backtest PM 输出契约测试 | 缺 `canonical_action_family`；缺必需的 `action_preference` 或 preference 违反 canonical family；缺 `action_value_lane`；缺 `learning_lane`；`canonical_action_value != true`；`consumer_scope != pm_learning`；future dated；incomplete prior 混入 | 列表为空、同类样本少、弱命中、canonical 允许的空 preference |
| `learning_used.memory_retrieval.rejected_or_downgraded` | PM learning retrieval / Step6 装配 | 被 PM 候选学习集合剔除的 weak prior、incomplete prior、降级行 | 诊断检索材料，记录剔除原因 | `final_action_contract.learning_used.memory_retrieval.rejected_or_downgraded` | Reviewer、Researcher | PM self-check 边界；pre-backtest artifact boundary | 参与 score/rank/手数/final_action；被当作正式 action-value 主证据 | weak prior 多、同类历史不足 |
| `effective_memory_summary` | `pm_decision_memory_retrieval` | 研究库 action-value、profile、state、有效日期 | PM 记忆检索质量摘要、有效数量、剔除原因、匹配层级 | PM 输入对象；`final_action_contract.learning_used` 摘要 | PM、Reviewer、Researcher | contract coverage；pre-backtest memory fixture | 空壳历史覆盖真实历史；future learning 进入 PM；非 `pm_learning` 进入 PM 正式学习 | 历史为空、弱匹配、样本少 |
| `opportunity_scorecard` | PM 内部方向与状态判断，Step2–3 | SCC、证据融合、当前持仓、风险上下文 | 单品种方向选择、候选质量和内部状态分项 | 同一个 PM 内存状态；Step6 仅把矩阵登记摘要写入 `final_action_contract.evidence_used` | PM Step4/Step5、Reviewer、Researcher | contract coverage；PM 最终合约间接校验 | scorecard 独立输出 artifact、直接写最终 rank/手数/资金部署、替代 full-market rank、形成第二套交易计划 | 方向冲突、候选质量低 |
| `opportunity_score_components` | PM scorecard / signal fusion | SCC、学习摘要、证据融合、风险边界 | PM 机会评分分项 | PM 内部结果；`final_action_contract.evidence_used` 摘要 | PM Step5 rank、Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试 | score component 被 Trader 当交易意图；学习分项直接生成手数；执行学习直接推 rank | 正向学习弱、负向学习强、冲突高 |
| `rank_capital_layer_contract` | PM full-market capital deployment，Step5 | 当日所有实际增加风险的候选池、资金状态、rank score、资金政策 | rank、capital layer、ratio source、资金部署解释 | `final_action_contract.capital_deployment` | PM self-check、Reviewer、Researcher | PM self-check；pre-backtest PM 输出契约测试 | `open/open_probe/open_real` 或同方向扩大绝对手数的 `add/scale` 未经过 rank；rank 字段缺失；`wait/hold/reduce/exit`、当前反转退出腿或不增加风险的条件监控错误携带 rank；资金层字段缺失；rank 写在非 PM artifact 里变成交易权限 | 未入选、资金层级低、资金利用不足 |
| `position_sizing_result` | PM position sizing tool | 持仓、合约乘数、保证金率、风险参数、目标资金层 | 确定性手数计算结果 | PM 输入；`final_action_contract.evidence_used.position_sizing_result` | PM Step6、PM self-check、Auditor（仅取 `target_margin_ratio_estimate` 作为硬保证金上限输入）、Reviewer | PM self-check；contract coverage | sizing 工具直接签最终交易；空对象冒充 sizing；结果与 `target_lots/lots_delta` 不一致；Auditor 重算 sizing | sizing 被风险上限压低 |
| `audit_verdict` | Auditor，Phase1 | 完整 FAC；权益、保证金、保证金比例、`risk_status`；当前持仓；SCC 数据质量摘要；具体合约及失效边界事实；主配置硬保证金上限 | `approve` / `approve_with_warning` / `block`、hard/soft risk reasons、完整审计 payload | recommendation audit fields；audit artifact | Trader、Reviewer、PG | Auditor contract audit；daily PG 只核对审计与执行的外部事实 | 输入事实缺失；清算账户或硬保证金上限阻止新增风险；具体合约/失效边界非法；硬数据错误；Auditor 改方向、手数、FAC，或复审 PM 学习/融合/rank/预算/sizing；阻断后 Trader 仍执行 | 数据质量 warning、风险接近硬上限 |
| `execution_contract` | Trader 执行入口，Phase2 | 审计通过的 final contract、盘中行情、执行配置 | 执行触发摘要、执行 profile、触发条件、失效条件 | Trader runtime payload；transaction audit payload 摘要 | Trader、Reviewer、Researcher | pre-backtest artifact boundary；Trader trigger parity；daily PG 只核对已落地执行与成交事实 | 复制完整 PM 合约；包含 `learning_used`、rank、score、资金解释、PM 学习；放宽 PM 触发；改方向、改手数 | 未触发、价格错过、流动性不足 |
| `futures_transactions` / transaction payload | Trader，Phase2 | audit passed contract、盘中触发、成交价格、合约信息 | 仅真实成交事实及执行审计 | `futures_transactions`；transaction artifact；audit payload | Accountant、Reviewer、Researcher、PG | daily PG trade source audit | 未成交写入 transaction；成交不来自最终合约；无 open authority 却开仓；缺触发记录；source_type 错；交易手数超合约授权；运营单污染策略单 | 滑点大、部分成交 |
| `execution_result` | Trader，Phase2 | 审计通过的 final contract、盘中触发、成交/未成交事实 | 执行结果、状态、真实成交列表、未触发/未成交/失效/市场规则阻断原因 | recommendation `signal_snapshot.execution_result`；execution result artifact；Reviewer / Researcher input | Accountant、Reviewer、Researcher、PG | daily PG execution result lineage | execution result 改 PM 合约；缺 recommendation lineage；source_type 错；结果与 transaction 不一致；把未成交伪造成交 | 审计通过但未触发或未成交、部分成交、滑点偏大 |
| `execution_learning_trace` | Trader / futures audit helper，Phase2 | 执行结果、触发状态、成交质量 | 执行学习 trace，`consumer_scope=trader_execution_learning` | execution result；Reviewer input；Researcher input | Reviewer、Researcher | pre-backtest execution trace contract；daily PG 不复查其内部学习语义 | bare execution trace 缺 consumer_scope；Trader 用 trace 下单；trace 改 PM 合约 | 触发质量弱、成交质量差 |
| `portfolio.positions` / `daily_settlement.positions_snapshot` | Accountant，Phase3 | 成交、持仓、结算价、手续费、保证金率、合约乘数 | 结算后的当前持仓、日结算持仓快照、PnL、权益和保证金事实 | `portfolio.positions`；`daily_settlement.positions_snapshot`；`ticker_daily_pnl`；settlement artifact，不存在独立 position SQL 路径 | Reviewer、Researcher、PG、评估 | Phase4 review；daily PG accounting boundary | 两份持仓事实不一致；成交重复入账；改交易动作；用学习或 LLM 改账；结算与成交不一致；写 PM rank/learning 字段 | 当天亏损、保证金利用偏低、实际敞口偏离 PM 规划预算 |
| `reviewer_phase4_review` / review facts | Reviewer，Phase4 | recommendation、audit、transaction、settlement、execution result、phase 状态 | Phase4 验收、交易日志、事实归因、研究输入材料 | reviewer artifact；review payload | Researcher | Phase4 gate；daily PG 只核对 Phase4 状态和物理结果可读性，不复查结论 | Reviewer 下单、调仓、写最终 action-value、改交易事实、触发 Researcher LLM 直接改当天 | 预算漂移 warning、执行质量差、归因不利 |
| `researcher_llm_notes` | Researcher，Phase4 与结算完成后 | 通过正式 ID 链验证的 AEC → SCC → FAC → Auditor → execution_result → transaction → settlement 事实包 | 经结构校验的 evidence pack 与 `validated_output`；禁止保存 prompt、原始 response、内部推理和未验证工具结果 | `researcher_llm_notes.evidence_pack_id`、`payload_json` 及 payload artifact 元数据；`raw_prompt/raw_response` 和对应 artifact 元数据固定为空 | Research writer；分析师正式校准检索；PM `decision_memory_retrieval` | data time boundary；structured IO；正式 ID lineage | 来源记录/日期/ID 断链；保存原始模型内容；使用未结算或未完成 Phase4 日期；改当天事实；输出当日交易指令 | 合法零成交、无合格学习成果、研究观点弱、样本少 |
| `alpha_setup_action_value` | Researcher 写入工具，Phase4 后 | 已结算 episode、复盘事实、未交易机会、执行事实 | canonical action-value：`action_name -> canonical_action_family -> action_value_lane/learning_lane -> action_preference` | `alpha_setup_action_value` DB；payload_json 同值保留 | PM next-day retrieval、Reviewer、Researcher | pre-backtest action matrix contract；daily PG 只检查实际生成记录的来源日期与前视边界 | 缺 canonical family/lane/preference；family/lane/preference 不一致；observe 冒充 positive candidate；future dated；非策略事件污染 strategy action-value | observe 空 preference 合法；样本少；reward 弱 |
| `alpha_setup_profile` / product learning | Researcher 写入工具，Phase4 后 | episode 聚合、setup、trigger、证据组合、deployment outcome | 产品/setup/trigger 历史表现 | research DB | 分析师校准、PM product learning、Researcher | contract coverage；data time boundary | 直接写手数、交易授权、当日合约修改 | 产品表现差、setup 样本少 |
| `adaptive_policy_state` / `provisional_policy_state` / `config_learning_overlay` | Researcher / 配置学习写入工具，Phase4 后 | 长窗口研究结果、验证通过的参数证据、回滚值 | 策略参数学习状态、候选参数、回滚信息 | research / config learning DB | 开发验收、PM 配置读取、PG | pre-backtest config consistency；data time boundary | 未验证参数直接生效；缺 rollback；改当天交易事实；绕过 PM 合约权限；未来数据参与参数 | 候选策略待验证、样本不足 |
| `trade_episode_memory` / `no_trade_opportunity_memory` | Reviewer / Researcher，Phase4 后 | 已结算完整交易对、未交易机会、未触发条件机会 | 可为空的未来学习事实底座 | research DB；artifact | 分析师正式校准检索、PM `decision_memory_retrieval`、Researcher | data time boundary；mechanism audit | 前视；未完成 Phase4/结算进入学习；无代表样本却写结论；要求每笔交易都形成学习 | 无合格样本、学习为空、机会少、错过交易 |
| `trading_day_phase` | workflow，Phase1-Phase4 | 各阶段运行结果 | 阶段状态与时间戳 | `trading_day_phase` DB | PG、backtest gate、Reviewer | daily PG phase audit | 存在业务记录但 phase 未 completed；阶段顺序断裂；失败残留跨日污染 | 当天无交易但 phase 完整 |
| `contract_coverage_audit` | PG，回测前 | 可导入生产代码、正式 schema、机制文档和真实路径测试 | 六维版本级契约覆盖矩阵：producer、physical_landing、consumer、role_check、real_path_test、mechanism_doc | pre-backtest report | 开发者、回测闸门 | pre-backtest acceptance | 任一六维 runtime/document evidence 缺失；依赖字符串命中、废弃函数或禁用代码冒充 coverage；核心契约缺字段表登记 | coverage 完整但真实样本少 |
| `pre_backtest_acceptance` | PG，回测前 | DB schema、代码、配置、fixture、契约覆盖、系统不变量；指定窗口与配置品种的只读市场、合约、Finoview 和新闻数据入口 | 回测前 readiness 结论 | pre-backtest report | 开发者、回测脚本 | 回测前闸门 | 通用系统不变量 fixture 失败；schema 断裂；越权字段；contract coverage 缺口；字段矩阵断裂；交易日、PandaAI 日线开收盘价、官方结算价、主力合约映射、合约乘数、保证金率、具体合约信息或 Trader 分钟行情接口能力等交易必需数据断裂 | LLM 配置或密钥环境变量缺失、某品种某日无新增基本面或新闻；PG 不发起 LLM 鉴权请求 |
| `system_invariant_audit` | PG，每日回测后 | 当日 DB、artifact、phase、字段矩阵 | 系统不变量报告 | daily gate output | 开发者、回测脚本 | daily gate | 应落地的 final contract 或 SCC 缺失、artifact 污染、越权、交易不来自唯一合法来源、执行/成交/结算事实不一致、阶段断裂 | 无交易、收益差、学习为空或未产生、PM 内部自检/rank/学习作用过程 |

## 4. 回测前通用不变量 fixture 矩阵

这些 fixture 必须在真实回测前运行，用代表性结构化样本证明字段、动作、职责、阶段和唯一交易事实等通用不变量仍然成立。历史问题可以贡献样本，但不得把“某个旧错误是否再次发生”作为检测目的，也不得围绕单次故障维护专用门禁。

| 不变量场景 | fixture 输入 | 必须命中的 gate | hard fail 断言 |
|---|---|---|---|
| SCC 缺失 | PM recommendation 只有 `signal_collection_contract_ref`，缺 `signal_snapshot.signal_collection_contract` | pre-backtest acceptance / contract coverage SCC fixture | `signal_snapshot.signal_collection_contract` 缺失 hard fail |
| SCC source_agent/boundary 错 | SCC `source_agent` 非 `signal_collector`，boundary 非 `no_trade_authority` | pre-backtest SCC fixture | source_agent/boundary 非法 hard fail |
| PM artifact 混入 incomplete prior | `canonical_action_value=false` 的 similar/fallback prior 塞进 `learning_used.alpha_setup_action_values` | PM self-check fixture | formal action-value 主列表污染 hard fail |
| observe 空 `action_preference` | `canonical_action_family=observe`，lane 为 `hold`，preference 为空 | PG action matrix fixture | 合法通过；禁止误报 missing preference |
| observe 冒充交易偏向 | observe 行带 `positive_candidate_open/exit/execution/hold` | PG action matrix fixture | family/lane/preference 不一致 hard fail |
| Step4 临时路由 / Step6 final trace 混用 | Step4 临时路由是 hold，Step6 final 是 open/rank，最终 decision rows 带 hold | PM self-check fixture | final lifecycle trace 污染 hard fail；禁止比较早期路由与最终生命周期本身是否一致 |
| execution/profile 污染决策层 | reduce_exit final contract 的 decision rows 带 execution | PM self-check fixture | decision rows 污染 hard fail |
| execution/profile 合法分层 | reduce_exit final contract 的 trigger profile rows 带 execution，direct-to-rank false | PM self-check fixture | 通过；不误判为 reduce_exit 污染 |
| action family/lane/preference 不一致 | `positive_candidate_open` 配 `reduce_exit`；缺 family；缺 lane | PG action matrix fixture | hard fail |
| Trader 越权字段 | transaction payload 保存完整 PM 合约、学习、rank、资金解释 | artifact boundary fixture | hard fail |
| Reviewer 越权字段 | Reviewer artifact 写最终 action-value、研究状态、当天交易事实改写 | artifact boundary fixture | hard fail |
| Researcher 越权字段 | Research artifact 改当天合约、成交、结算、PnL | artifact boundary fixture | hard fail |
| Trader 成交不来自合约 | open transaction 缺 final contract open authority | single trade truth fixture | hard fail |
| 未完成交易日进入学习 | phase 未 completed，存在 recommendation/transaction/learning | data time boundary fixture | hard fail |

## 5. Daily PG 审计边界矩阵

每日 PG 固定输出以下七项。它只读核对当日已经形成的物理事实，不进入智能体内部，不重复 PM 自检、Auditor 审计或 Reviewer 复盘。

| 正式检查名 | PG 必审 | PG 禁审 |
|---|---|---|
| `daily_phase_completion` | Phase1→Phase4 completed 状态、真实时间顺序，以及实际生成的 Researcher completion 事件晚于 Phase4 | 阶段内推理质量；无交易日是否策略无效 |
| `physical_result_landing` | 只对实际进入的路径要求对应落点；SCC 通过共享完整校验；三个真实 `signal_record_id` 精确对应三名分析师 SQL AEC、ticker 和日期；artifact/持久化无 prompt、原始 response、隐藏上下文和未登记字段 | 信号强弱是否足够交易；分析师内部工作过程 |
| `single_trade_fact_source` | 每笔 transaction 显式登记 `strategy`、`rollover` 或 `forced_risk`；三类来源分别绑定唯一 FAC、rollover policy 或 forced-risk boundary | 把运营交易强套 PM 策略合约；评价交易方向和收益 |
| `audit_release_and_execution_result` | strategy 成交具有完整 Auditor payload、允许执行的 verdict 和 FAC 授权；block/require_review 不得成交；approve 后合法未触发、未成交或部分成交允许 | 重做 Auditor 硬风险判断；把 approve 解释为必须成交 |
| `execution_and_transaction_fact` | recommendation ID、动作、方向、具体合约和累计成交手数与 FAC 授权一致；execution_result 与 transaction 一致；仅条件 FAC 核对盘中决策 | 复判 Trader 内部推理、追价和择时质量 |
| `settlement_and_account_fact` | transaction 只入账一次；逐品种手数、`portfolio.positions`、`daily_settlement.positions_snapshot`、ticker PnL、手续费、保证金、现金和权益守恒 | 要求实际成交等于 PM 预算；因实际净敞口偏离规划预算判错；评价当天盈亏 |
| `learning_record_landing_boundary` | 只检查实际生成的学习记录；核对 Phase4、结算、来源日期、正式 ID 和 canonical action-value；成交型学习追溯真实 transaction/settlement，反事实机会不伪造 transaction | 要求每笔交易产生学习、要求每次决策使用学习、评价学习质量或改写历史事实 |

## 6. 修改检查清单

每次修改链路时按下列顺序执行：

1. 定位本文矩阵行。
2. 确认字段在 `docs/matrix_field_semantics.md` 已登记。
3. action-value 改动同步对照 `docs/matrix_action_canonical.md`。
4. 生产端与落盘端同轮修改。
5. 消费端与自检同轮修改。
6. 行为回归测试覆盖具体失败形态；pre-backtest 只运行通用不变量与 readiness 验收，不增加按历史错误命名的生产检查分支。
7. daily PG audit 只审系统契约，不复判策略。
8. 真实路径测试证明 producer-to-consumer 字段保真。
9. 修改 `.py/.yaml/.yml` 后更新 `docs/work_log.md`。

## 7. 固定结论

- PM final contract 是策略交易唯一真相。
- SCC 主证据是 `signal_snapshot.signal_collection_contract`。
- action-value 主语义是 `action_name -> canonical_action_family -> action_value_lane/learning_lane -> action_preference`。
- PM formal action-value 主列表只保存完整 canonical 证据。
- weak prior 只进入 diagnostics。
- observe 空 preference 是合法观察语义。
- `final_action_contract` 中由 Step6 重新形成的 final lifecycle trace 是 PM 自检唯一决策层学习 trace；`pm_six_step_trace` 只保存两个最终检查。
- pre-backtest gate 用代表性样本证明通用系统不变量，不以复现历史故障为目标。
- daily PG audit 只 hard fail 已落地物理结果中的系统契约断裂，不读取或复查任何智能体内部机制。
- 策略优劣只由长期策略评估判断。
