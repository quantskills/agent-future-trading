# AgentQuant 项目工作手册

本文件是 AgentQuant 的最高开发工作手册。处理本项目时，无论是修改代码、调整系统框架、改配置参数、排查回测、评估业务路径、整理文档，还是回答“现在该怎么办”，都必须先按本手册校准边界和证据。

## 1. 项目目标

AgentQuant 的目标只有一个：让多智能体系统自动生成的期货交易策略，在回测和模拟盘中尽可能实现稳定正收益，并能在真实期货业务链路中一比一复刻。

系统设计必须保持主动 alpha 迭代导向：分析师、PM、Auditor、Trader、Researcher 不是用来堆叠被动限制的，而是要基于行情时序、基本面、新闻、执行反馈和历史学习，主动发现可交易优势，验证其正期望，把合格机会落实到仓位和交易出口，并把结果反哺下一轮策略。限制、封顶、观察、probe 只能服务于风险识别和学习验证，不能取代寻找收益机会本身。

所有工作都必须服务于这个目标。代码更复杂、机制更多、日志更详细、归因更漂亮，都不等于目标达成。判断一次工作是否有价值，要看它是否直接或间接改善：

- 净收益、收益稳定性、最大回撤；
- 胜率、盈亏比、交易成本后收益；
- 资金利用率和实战部署意义；
- 正期望机会识别、合理落仓、及时退出、盈利持仓保护；
- 回测策略能否在模拟盘和真实执行链路复刻。

## 2. 运行环境硬边界

- 所有 AgentQuant 程序、测试、验收、回测、评估、数据库脚本都必须在本地 conda 环境 `deepfund` 中运行。
- 标准 Python 路径是 `C:\ProgramData\miniconda3\envs\deepfund\python.exe`。
- 不要使用 `base` 环境、系统默认 Python 或未确认环境运行本项目。
- 推荐从仓库根目录 `D:\research\AgentQuant` 运行命令；如果从 `src` 目录运行，必须相应调整路径。
- `.env` 保存 API key，不得在回复、日志或文档中泄露密钥内容。
- 临时排查脚本如确实需要，只能放在 `D:\research\Workshop\`，任务结束后删除；不要把一次性脚本长期留在 `src/run`、`src/tests` 或业务模块中。
- 不要执行 `git reset --hard`、`git checkout --` 等会丢弃用户工作的命令，除非用户明确要求。

## 3. 当前系统主链路

AgentQuant 的业务线是一个闭环：

`数据与行情 -> 分析师结构化证据 -> PM 唯一交易契约 -> Auditor 审计 -> Trader 执行 -> Accountant 结算 -> Reviewer/Researcher 复盘学习 -> 下一轮分析师和 PM 使用学习结果`

控制组在主链外做协议、验收、审计和观测：

`protocol_governor / preflight / pre_backtest_acceptance / system_invariant_audit`

控制组不能生成交易权限，不能改手数，不能改保证金，不能替代 PM、Auditor 或 Trader。

## 4. 唯一交易契约原则

策略交易的唯一交易真相是 PM 最终推荐记录中的 `final_action_contract`。

必须保持如下路径：

- 分析师只输出结构化证据，不输出手数、保证金比例或最终交易命令。
- PM 读取分析师证据、当前持仓、资金边界和研究学习结果，生成唯一 `final_action_contract`。
- Auditor 只审计这张契约的合规性、权限、风险边界和是否绕出口；审不过时，PM 必须把最终推荐改成 `hold/wait` 或相应受限动作。
- Trader 只读取最终推荐记录里的 `final_action_contract` 执行；盘中触发只决定成交或不成交，不能改方向、目标手数、变化手数或保证金授权。
- Accountant 只按实际成交和结算价核算，并把 PnL 绑定回对应契约。
- Reviewer/Researcher 只研究这张契约导致的完整 episode 结果，不从草稿或旁路学习。

PM 内部草稿只能是局部计算过程，不能以 `pre_open_plan` 字段落入运行时 artifact；它不是交易真相，不是 Trader 成交来源，不是 Researcher 学习来源，不是审计推导目标手数的来源。

策略单必须是 `contract_type=strategy` 的 `final_action_contract`。换月、强平、风控处置等非策略动作必须走运营或风险事件路径，例如 `source_type=rollover`，独立核算，不得污染 alpha 学习。

## 5. 已启用智能体职责边界

本章只描述当前系统实际启用并参与回测链路的智能体。当前 `src/config/dev.yaml` 的 `workflow_analysts` 只启用 `commodity_news`、`fundamental`、`technical` 三位分析师；策略链路还启用 PM、Auditor、Trader、Accountant、Reviewer、Researcher 和 Protocol Governor。未在本章列出的角色不参与当前主链路。

当前完整链路是：

`technical / fundamental / commodity_news -> portfolio_manager -> auditor -> trader -> accountant -> reviewer -> researcher`

控制与验收旁路是：

`protocol_governor / pre_backtest_acceptance / system_invariant_audit`

### 5.1 `technical` 技术分析师

输入：

- PandaAI 盘前价格、成交量、持仓量、结算价、开高低收、合约与交易规则；
- 趋势、波动率、支撑阻力、RSI、MACD、ADX、均值回归、随机指标、缺口等技术特征；
- 技术参数校准、技术形态历史学习、数据截止时间和 no-lookahead 状态。

输出：

- `AnalystSignal`；
- `metadata.action_evidence_contract`；
- `metadata.trade_research_contract`；
- `signal`、`confidence`、`market_regime`、`price_location`；
- `setup_quality_ok`、`setup_type`、`trigger_valid`、`current_trigger_confirmed`；
- `entry_trigger`、`exit_hint`、`invalidation_present`、`invalidation_condition`、`opportunity_state`；
- `metadata.data_usage_summary`、`metadata.reviewer_learning_context`、`metadata.learning_impact_summary`。

边界：

- 只判断技术证据质量、触发事实、失效边界和机会状态；
- `setup_quality_ok=true` 只表示形态值得关注，不能推出当前可交易；
- 只有 `trigger_valid=true` 且 `current_trigger_confirmed=true` 才表示当前触发成立；
- 不能输出手数、保证金比例、最终交易命令或 PM 排名分数。

### 5.2 `fundamental` 基本面分析师

输入：

- Finoview 本地 feather 基本面数据；
- PandaAI 衍生因子、期货库存、仓单、基差、供需、利润、开工率、产量、进口、产业链数据；
- 基本面数据质量、数据新鲜度、历史因子有效性与误判记录。

输出：

- `AnalystSignal`；
- `metadata.action_evidence_contract`；
- `metadata.trade_research_contract`；
- `primary_business_driver`、`supply_demand_state`、`basis_state`、`inventory_state`、`warehouse_receipt_state`、`data_freshness`；
- `factor_focus`、`business_quality_score`、`factor_alignment_score`；
- `entry_trigger`、`invalidation_present`、`invalidation_condition`、`opportunity_state`；
- `metadata.data_usage_summary`、`metadata.reviewer_learning_context`、`metadata.learning_impact_summary`。

边界：

- 只提供基本面方向、产业链逻辑、数据质量和短期确认要求；
- 中长期基本面观点不能直接变成开仓，必须有短期触发、失效边界和 PM 审查；
- 陈旧、缺失或冲突数据必须降级并写入 `data_usage_summary`；
- 不能给仓位、手数、保证金或最终交易权限。

### 5.3 `commodity_news` 商品新闻分析师

输入：

- 本地商品新闻文本；
- 新闻发布时间、事件类型、相关度、新鲜度、影响窗口和产业链背景；
- 历史类似新闻催化是否有效的研究记录。

输出：

- `AnalystSignal`；
- `metadata.action_evidence_contract`；
- `metadata.trade_research_contract`；
- `event_type`、`impact_window_days`、`catalyst_quality`、`news_relevance`；
- `entry_trigger`、`exit_hint`、`trigger_valid`、`current_trigger_confirmed`、`invalidation_present`、`opportunity_state`；
- `metadata.data_usage_summary`、`metadata.reviewer_learning_context`、`metadata.learning_impact_summary`。

边界：

- 只判断新闻是否构成真实催化、影响方向、影响窗口和是否需要价格/成交量确认；
- 背景新闻、低相关新闻、无时间戳新闻不能直接生成开仓证据；
- 新闻催化必须落到 `action_evidence_contract`，不能以自由文本绕过 PM；
- 不能输出手数、保证金、PM 排名或最终交易命令。

### 5.4 `portfolio_manager` 组合经理

输入：

- 三位分析师的 `action_evidence_contract` 和 `trade_research_contract`；
- 当前持仓、账户权益、可用资金、保证金、合约信息和市场确认；
- `alpha_setup_action_value`、`adaptive_policy_state`、memory quality、历史同类 state/action 结果；
- Auditor 反馈、配置中的资金边界和机会质量边界。

输出：

- `FuturesRecommendation`；
- 唯一 `final_action_contract`；
- `opportunity_scorecard`、`opportunity_score_components`、`opportunity_rank`、`capital_allocation_reason`、`learning_adjustment_summary`；
- `capital_deployment`、`active_opportunity_audit`、`reason_codes`、`learning_used`、`capital_controls`、`risk_controls`。

职责：

- PM 是唯一策略资金经理和唯一策略交易意图生成者；
- 对每个品种和方向做证据分流：当前可交易、条件监控、明确不可交易原因；
- 对全市场候选做机会评分、排序和资金部署；
- 把入选或未入选结果回写同一张 `final_action_contract.target_lots/lots_delta/final_action`；
- 对条件机会只能写入同一张合约的 `conditional_trigger_authority`，不能创建第二条交易路径。

边界：

- 不能用 `pre_open_plan`、旧字段、顶层 action/lots、direction_only、watchlist 或 minimum lot 绕过 `final_action_contract`；
- 不能把 `opportunity_score/opportunity_rank` 当成交易权限；
- 不能跳过 Auditor；
- 不能让 Researcher 候选偏好直接开仓或放大仓位；
- 未入选候选必须写清不交易原因，不能静默 wait。

### 5.5 `auditor` 审计员

输入：

- PM 生成的唯一 `final_action_contract`；
- 当前账户、持仓、保证金、合约状态、涨跌停、数据质量、硬风险配置；
- PM 的 `reason_codes`、`learning_used`、`capital_controls`、artifact lineage。

输出：

- `audit_verdict`；
- `approved`、`decision`、`hard_blocks`、`soft_controls`、`audit_reason_codes`；
- 审计 payload 和必要的降级/阻断原因。

边界：

- 只审 PM 的同一张合约，不创造新方向、新手数、新保证金目标或第二张合约；
- 可以 block、reduce_only、probe_only 或要求 PM 降级；
- 审计结果必须回到 PM 最终推荐记录，由 Trader 执行最终合约；
- 不评价策略是否赚钱，只审权限、风险、字段一致性和业务边界。

### 5.6 `trader` 交易员

输入：

- 通过 PM/Auditor 边界后的 `final_action_contract`；
- 合约内 `current_lots`、`target_lots`、`lots_delta`、`execution_profile`、`entry_trigger`、`conditional_trigger_authority`、`requires_intraday_confirmation`；
- 盘中 PandaAI 行情、触发状态、滑点模型、手续费、合约交易规则；
- 独立运营单 `source_type=rollover`、`source_type=forced_risk`。

输出：

- 成交或未成交记录；
- `execution_result`、`trigger_fired`、`trigger_reason`、`not_executed_reason`；
- 成交方向、手数、价格、滑点、手续费；
- execution learning trace；
- 必要时生成并执行 `forced_risk` close/reduce 运营单。

边界：

- 策略单只执行 `final_action_contract`，不能从分析师证据、PM 文本、排序字段、旧字段或顶层 raw action/lots 推交易；
- 条件 probe 只检查盘中触发，触发后按合约方向和手数执行，未触发只记录原因；
- 不能改变 PM 合约指定的方向、目标手数、变化手数或保证金权限；
- raw action/lots 只允许 `rollover/forced_risk` 运营单使用；
- `forced_risk` 只能 close/reduce，不能开新策略仓。

### 5.7 `accountant` 会计

输入：

- Trader 实际成交；
- 持仓、结算价、手续费、滑点、合约乘数、保证金率、账户余额；
- rollover/forced_risk 等运营成交事实。

输出：

- `daily_settlement`；
- 品种 PnL、手续费、保证金占用、释放保证金、账户权益、现金变化；
- 持仓状态、日终结算状态和评估所需事实表。

边界：

- 只按事实核算，不参与策略判断；
- 不读取 LLM 文本改变账务；
- 不生成 action-value，不改变 Researcher 学习结论；
- 策略单和运营单必须分账，运营动作不能污染策略 alpha 归因。

### 5.8 `reviewer` 复盘员

输入：

- Phase1-3 的推荐、合约、审计、成交、未成交、结算和日志事实；
- PM 机会评分、排名、资金部署理由；
- Trader 执行结果、Accountant 结算结果、数据质量记录。

输出：

- Phase4 验收结果；
- daily summary、交易日志、排序有效性复盘；
- 交易 episode 归因、未触发机会记录、未交易机会观察；
- 给 Researcher 使用的复盘事实。

边界：

- Reviewer 是确定性复盘与验收，不下单、不调仓、不写最终学习；
- 必须区分系统非策略问题和策略表现问题；
- 必须复盘高分/高排名候选是否贡献收益、低排名/未入选候选是否错过收益；
- 不能把未触发条件机会写成真实开仓亏损。

### 5.9 `researcher` 研究员

输入：

- Reviewer 产物；
- 已结算真实交易 episode；
- 未触发条件机会、未交易机会、shadow/counterfactual 观察；
- open/hold/exit/execution 结果、PM 排序有效性、执行质量、PnL 和回撤事实。

输出：

- `alpha_setup_profile`；
- `alpha_setup_action_value`；
- `adaptive_policy_state`；
- `opportunity_ranking_preference` 或等价机会排序偏好候选；
- 策略记忆、未来学习上下文、研究假设。

边界：

- 必须按 action lane 分账：open、hold、exit/reduce、execution；
- 只写未来可用学习，不能影响当天交易；
- 不能直接创建交易权限、修改 Trader 方向/手数、改账或绕过 PM/Auditor；
- exact real episode 可影响后续 PM 排序和资金部署，partial/similar/shadow 只能作为弱先验或候选；
- 学习结果必须可审计，不能用自由文本记忆替代结构化 action-value。

### 5.10 `protocol_governor` 协议管理员

输入：

- 能力卡、工具权限、字段语义、artifact lineage；
- `pre_backtest_acceptance`、`system_invariant_audit`、统一字段审计和阶段状态；
- PM、Auditor、Trader、Researcher 的关键运行 artifact。

输出：

- protocol audit；
- preflight health；
- 字段、权限、阶段、非策略问题和生命周期告警。

边界：

- Protocol Governor 是旁路治理和 fail-fast 检查，不是 PM、Auditor、Trader 或 Researcher；
- 不能创建交易权限，不能改方向、手数、保证金，不能执行订单，不能写结算；
- 发现 hard error 时应阻断回测继续解释策略收益；
- 只判断系统边界是否 clean，不评价策略是否赚钱。

## 6. 学习与 RAG 边界

研究学习是结构化输入，不是自由文本记忆。

记忆质量分层：

- `exact_real_state`：同 ticker/side/setup/regime/action 且来自真实交易 episode，可参与 real_budget_entry 或 scale；
- `partial_real_state`：真实交易但 state 不完整，只能支持 probe、复核、保护或降级；
- `similar_sql_prior`：相似历史，只能作弱先验；
- `shadow_prior`：影子或未交易观察，只能提示观察；
- `stale_or_conflicted_memory`：过期或冲突记忆，只能审计，不参与放大。

所有学习读取必须满足 `source_trading_date < decision_date`。同日 Phase4/Researcher 或未来记录不得影响当日分析师、PM、Auditor 或 Trader。

学习使用边界：

- 分析师使用学习来校准证据可靠性，不获得交易授权；
- PM 使用学习来调整 open/hold/exit/execution 倾向和仓位资格；
- Trader 使用 execution 学习来选择触发 profile，不改变契约方向和手数；
- Researcher 写入学习，但不能让学习绕过 PM 和 Auditor；
- protocol_governor 只审计学习是否按契约落仓，不参与收益判断。

## 7. 数据与事实边界

- PandaAI：行情、分钟线、结算、合约和期货衍生数据。
- Finoview 本地 feather：基本面数据，只能从 `data/Fundamental_data/Finoview_data/` 调用。
- 本地新闻：只能从 `data/News_data/Future_news/` 调用。
- `finoview_factor_catalog.yaml` 是本地 feather 字段目录。
- `data_factor_policy_catalog.yaml` 是 PandaAI、Finoview、新闻的数据入口和质量策略目录。
- 没有日期列、无法确认时点或超过决策日 cutoff 的数据，不能作为当日强证据。
- `metadata.data_usage_summary` 必须说明数据新鲜度、来源和降级原因。

## 8. 配置边界

主要配置文件：

- `src/config/dev.yaml`：运行入口、账户资金、LLM active block、控制组开关和核心 runtime 配置；
- `src/config/portfolio_policy_catalog.yaml`：PM、机会质量、市场确认、资金部署边界；
- `src/config/learning_policy_catalog.yaml`：学习、记忆、neutral 追踪和上下文预算；
- `src/config/analyst_prior_profiles.yaml`：分析师冷启动先验，不是开仓规则；
- `src/config/data_factor_policy_catalog.yaml`：数据质量与数据源策略；
- `src/config/finoview_factor_catalog.yaml`：本地基本面字段目录；
- `src/config/execution_commission_catalog.yaml`：手续费事实；
- `src/config/execution_slippage_catalog.yaml`：滑点假设；
- `src/config/execution_exit_policy_catalog.yaml`：退出策略冷启动边界。

当前 `dev.yaml` 只允许启用 CodexOpenAI / `gpt-5.5` / medium reasoning，网关为 `http://47.74.0.65`。TQXAI / `claude-opus-4-6-1` 必须保留为完整注释备用。代码层可以保留 DeepSeek 和其他 provider 接入能力，但当前 runtime 配置只保留 Codex 与 TQXAI 两类。

不要无证据改手续费、滑点、结算事实、20% 总保证金硬边界、probe 资金边界和用户已调好的资金参数。

## 9. 开发任务流程

本节规定每次任务的执行顺序。无论是回答问题、排查回测、改代码、改配置、改提示词、改文档，还是评估“下一步该做什么”，都必须按本流程执行。

### 9.1 先定义任务目标

动手前必须先判断本次任务属于哪一类，并用对应边界处理：

- 修非策略 bug：目标是消除系统实现错误、字段误读、未来函数、旁路、未完成交易日、账务不一致等问题。
- 解决不交易：目标是找出机会在哪一层消失，不能简单放松风险或让 Trader 创造交易。
- 提升收益：目标是改善净收益、盈亏比、回撤、胜率、品种贡献、退出保护和资金部署。
- 提高资金利用率：目标是让资金流向更高质量机会，而不是用弱机会凑仓位。
- 优化学习闭环：目标是让 Reviewer/Researcher 基于真实 episode 和 action-value 生成可审计学习结果，并在不绕过 PM、Auditor、Trader 边界的前提下，影响后续分析师证据校准、PM 机会排序与资金部署、Trader 执行 profile；学习本身不能直接生成交易权限或放大仓位。
- 对齐文档、提示词或配置：目标是让说明口径和运行口径一致，不能改变交易事实。

不同目标不能混用同一套修法。尤其不能把所有问题都处理成“加门控、加限制、少交易”。

### 9.2 先读上下文

修改或判断前必须先读：

- `docs/work_log.md`：确认过去是否已经做过同类修改，是否存在重复、冲突或未收敛问题；
- `docs/unified_field_semantics.md`：凡涉及字段、语义、合约、学习、执行、复盘，都必须先核对字段表；
- 相关 `.py`、`.yaml/.yml`、提示词、测试和机制文档；
- 最近回测记录和系统审计结果，如果任务与回测表现有关。

读完后必须能说清楚：问题是系统 bug、策略表现问题、配置问题、数据问题、学习问题，还是文档口径问题。

### 9.3 沿完整链路排查

不得只盯单个函数、单个字段或单个智能体。至少沿这条链路核对：

`分析师证据 -> PM 分流/排序/合约 -> Auditor 审计 -> Trader 执行 -> Accountant 结算 -> Reviewer/Researcher 学习 -> 回测前/每日验收`

检查重点：

- 分析师证据是否结构化、可排序、无未来函数；
- PM 是否把机会分成当前可交易、条件监控、明确不可交易原因，并写入唯一 `final_action_contract`；
- PM 的排序、学习影响和资金部署是否真的改变 `target_lots`、`lots_delta`、`final_action`，或明确写出不交易原因；
- Auditor 是否只审同一张契约，没有创造新交易权限；
- Trader 是否只按契约执行，并写清触发、成交或未成交原因；
- Accountant 是否能按成交和结算事实对账；
- Reviewer/Researcher 是否只按真实 episode 和 action-value 学习；
- 回测前验收和每日审计是否覆盖这条真实路径。

凡是只写日志、分数、原因、诊断或报告，但没有影响真实合约或明确不交易原因的修改，只能算解释增强，不能算交易链路修复。

### 9.4 修改边界

修改时必须守住以下边界：

- 不新增兜底逻辑掩盖错误；
- 不用旧字段绕过唯一契约；
- 不让控制组写交易策略；
- 不让分析师给仓位；
- 不让 Trader 创造策略、方向、目标手数或保证金权限；
- 不让 Researcher 用未来数据、弱先验或候选偏好直接放大真实仓位；
- 不把一个品种、一个窗口或一次偶然失败写成全局硬规则；
- 不默认通过新增门控解决收益问题。

新增限制前必须说明它是在提升机会排序、资金迁移、退出保护、风险识别，还是单纯减少交易。单纯减少交易不能被当成策略优化。

### 9.5 字段与口径同步

涉及新增或调整字段时，必须先查 `docs/unified_field_semantics.md`。

- 已有字段能表达同一语义或功能时，必须复用已有字段，不得重复起名；
- 确认确实需要新字段时，必须在同一轮同步写入字段表，明确放置位置和含义；
- 新字段进入运行时链路前，必须同步代码、测试和必要审计；
- 机制变更必须同步检查五类口径：代码、提示词、配置、字段语义表、回测前/每日验收。

只改其中一类会造成后续开发按旧口径理解系统，最终导致字段漂移、边界漂移或交易链路断裂。

### 9.6 测试与验收

如果是修真实失败路径，优先写能复刻该路径的失败测试，再修代码。测试必须尽量覆盖真实入口和真实链路，不能只用绕过主流程的手工构造样例证明局部函数正确。

修改后按影响面选择验证：

- 目标测试；
- 相关链路测试；
- 必要时运行 `pre_backtest_acceptance` 和 `system_invariant_audit`；
- 影响面大时运行全量 `python -m unittest`；
- 用 `git diff --check` 检查补丁格式。

如果问题发生在回测、Phase1、PM 分流、Trader 执行或 Researcher 学习中，测试应尽可能从 workflow、agent 入口或系统审计入口复刻，防止“单测通过但回测仍不交易”。

### 9.7 交付结论

完成后必须给出三个结论：

- 实际交易链路是否改变，具体改变到哪一层；
- 是否存在压死交易、资金利用率下降或新旁路风险；
- 下一轮回测应重点观察哪些指标和非策略问题。

## 10. 回测前验收

回测前先跑控制组验收，而不是让回测暴露已知系统 bug。

推荐命令：

```powershell
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --check-llm-auth --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json
```

`pre_backtest_acceptance` 固定覆盖：

- environment_api；
- config_consistency；
- data_time_boundary；
- agent_boundaries；
- structured_io；
- single_trade_exit；
- pm_opportunity_routing；
- trader_trigger_parity；
- learning_landing；
- capital_boundary；
- audit_explainability。

`backtest.py` 已接入回测前验收和逐日累计 `system_invariant_audit` fail-fast。验收通过只表示系统 readiness，不表示策略一定盈利。

## 11. 回测中与回测后判断

如果回测中 `system_invariant_audit` hard fail，必须停止，把结果按系统 bug 处理，不得讨论策略收益。

如果 audit clean 但收益差，才进入策略层分析，重点看：

- 正 alpha 是否被识别；
- 正 alpha 是否从 probe 走向 real_budget_entry 或 scale；
- 亏损 setup 是否快速降级；
- 入场触发是否过慢、过严或错过；
- 退出是否过慢、过早或回吐；
- 资金利用率是否过低；
- 品种/setup 分布是否集中或负期望；
- 分析师是否长期只输出 no_opportunity/watch_for_trigger；
- 学习是否真正改变 PM/Trader 的动作偏好。

旧回测记录不能证明新代码已经 clean；只有新代码生成的新记录通过 audit，才算该路径可信。

## 12. 测试矩阵

常用命令必须使用 deepfund：

```powershell
C:\ProgramData\miniconda3\envs\deepfund\python.exe -m compileall src
C:\ProgramData\miniconda3\envs\deepfund\python.exe -m unittest
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --check-llm-auth --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json
```

关键测试入口：

- `src/tests/test_agent_contracts.py`：智能体结构化契约；
- `src/tests/test_phase_flow_regression.py`：PM、Trader、Researcher 主链路；
- `src/tests/test_pre_backtest_acceptance.py`：回测前 10 项验收；
- `src/tests/test_protocol_governor.py`：控制组边界；
- `src/tests/test_protocol_preflight_cli.py`：preflight 和 backtest 接入；
- `src/tests/test_system_invariant_audit.py`：真实流水系统不变量；
- `src/tests/test_reviewer_learning.py`：复盘和研究学习；
- `src/tests/test_pandaai_api_adapter.py`：PandaAI adapter，真实 API 必须隔离为 integration；
- `src/tests/test_futures_market_rules.py`：期货交易规则；
- `src/tests/test_market_confirmation.py`：市场确认；
- `src/tests/test_phase1_acceleration.py`：Phase1 加速和入口行为。

新发现真实失败路径时，先写能复刻该路径的失败测试，再修代码，再跑目标测试、相关链路测试和必要验收。

## 13. 项目结构索引

- `src/agents/analysis_team/`：分析师 agent；
- `src/agents/decision_team/portfolio_manager.py`：PM 唯一交易契约生成；
- `src/agents/decision_team/auditor.py`：策略契约审计；
- `src/agents/execution_team/trader.py`：Trader 执行最终契约；
- `src/agents/execution_team/accountant.py`：账务和结算；
- `src/agents/research_team/`：Reviewer 和 Researcher；
- `src/agents/control_team/protocol_governor.py`：控制组协议治理，不是交易 agent；
- `src/tools/agent_tools/analysis/`：分析师工具、学习校准和证据合约；
- `src/tools/agent_tools/decision/`：PM 辅助工具；
- `src/tools/agent_tools/execution/`：触发、成交、合约、滑点和执行学习；
- `src/tools/agent_tools/research/`：研究学习、action-value、RAG、episode 归因；
- `src/tools/agent_tools/control/`：能力卡、工具权限、artifact lineage、task lifecycle、memory quality、action-preference 审计、cost budget、preflight、acceptance、system invariants；
- `src/llm/prompt.py`：集中提示词和 prompt builder；
- `src/llm/provider.py`、`src/llm/inference.py`：LLM provider 和推理入口；
- `src/database/`：SQLite schema、迁移、artifact 校验和数据库工具；
- `src/run/backtest.py`：回测主入口；
- `src/run/control/`：控制组命令入口；
- `src/run/research/`：研究初始化和学习相关命令；
- `src/evaluation/`：评估、报告和图表；
- `src/tests/`：确定性测试和回归测试。

## 14. 文档边界

- `docs/work_log.md`：行为代码/配置工作日志；
- `docs/mechanism_multiagents.md`：多智能体职责、边界和协作；
- `docs/mechanism_future_trade.md`：期货交易业务机制；
- `docs/mechanism_data_model.md`：数据与模型调用机制；
- `docs/mechanism_research.md`：研究、记忆、action-value 和学习闭环；
- `docs/parameter.md`：长期参数调节备忘；
- `docs/pandaia_data_introduction.md`：PandaAI 数据接入说明；
- `docs/ppt.md`：演示稿生成提示，不代表运行规则；
- `docs/release_baseline_2026-06-17.md`：本地基线说明。

纯文档说明不能替代代码、测试和真实 audit 证据。文档变更必须和现有代码语义一致。

## 15. 工作日志规则

`docs/work_log.md` 只记录完成后的 `.py`、`.yaml`、`.yml` 行为或运行配置修改。

必须记录的情况：

- 修改业务逻辑；
- 修改智能体输入输出；
- 修改交易契约、审计、执行、结算、学习；
- 修改测试逻辑；
- 修改控制组工具；
- 修改 runtime 配置。

不记录的情况：

- 纯讨论；
- 纯方案；
- 纯回测分析；
- 纯文档或 README；
- 数据文件变动；
- 文件改名或删除；
- 只改注释或 docstring 且不改变行为；
- 只运行测试或命令。

每条只写两项：

- 修改了什么：文件/模块/机制；
- 为什么改：对应哪个问题。

## 16. 回答用户时的规则

回答必须直接、基于证据、服务项目目标。

不要用“可能”“观察一下”“再小修一下”代替判断。若证据不足，先查代码、配置、数据库、日志或测试。若是系统 bug，明确说是系统 bug；若系统不变量 clean 但收益差，明确进入策略层分析。

不要把机制建设说成收益保证，也不要用“不保证盈利”逃避系统目标。正确说法是：系统链路必须先能一比一复刻交易逻辑；链路 clean 后，亏损才按策略信号、入退场、资金利用、学习效果和品种/setup 分布分析。

用户问“现在该干什么”时，必须给出下一步唯一动作或非常短的决策，不要绕回多套方案。
