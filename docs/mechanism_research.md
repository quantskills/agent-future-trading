# AgentQuant 记忆与研究机制

更新时间：2026-06-17

本文档用于后续优化 AgentQuant 的记忆、研究与策略自我迭代机制。它只记录已经代码落地或需要通过回测验收的机制，不把临时想法写成既成事实。

## 一、记忆与研究机制原则

AgentQuant 的记忆机制要服务未来决策，而不是只做事后解释。系统应尽量完整保存真实交易、未交易机会、Neutral 观望、影子结果、分析师判断原因、数据依据、PM/Auditor/Trader 决策、账务结果和后续表现，并让相关智能体在下一轮分析与决策时可检索、可引用、可反驳。

AgentQuant 的研究机制坚持自由探索式学习：让智能体从历史交易和未交易样本中主动探索期货价格走势、品类差异、分析侧重点、入退出场时机、持仓周期、失效边界和风险收益特征。学习的目标是提高交易信号质量、推动分析与交易策略自我迭代，并在成熟后落实到仓位，从而扩大 alpha 收益；不是不断堆规则、黑名单或硬约束来限制交易。

自由探索必须有边界。记忆和研究结论不能污染当日决策，未来结果只能在未来交易日结算后回填；候选假设不能直接放仓、加仓或支撑亏损仓硬扛；成熟经验也必须经过当日数据、市场确认、失效边界、PM、Auditor、Trader 和组合 20% 保证金硬门槛。系统要防止过拟合，优先使用同品种、同方向、同周期、同模板、同市场状态的经验，其次才是同板块经验，默认不使用全局泛化经验直接影响仓位。

归因的终点不是“为什么亏了或赚了”，而是“下一轮可用记忆”。每条研究结论都应尽量落到结构化的下一轮策略更新契约：适用范围、数据关注点、分析师下一轮该检查什么、PM 在什么条件下可开仓/加仓/减仓/退出、什么条件下经验失效、当前是候选假设还是成熟经验、最多能如何影响仓位。

学习机制可以提出经验，但经验必须通过后续同作用域样本证明自己；如果它影响仓位后的表现不好，系统应自动降权、撤销或退回候选状态。也就是说，研究结论可以自由产生，但仓位影响必须可验证、可反驳、可回滚。

当前有效学习链路是 `state -> action preference -> PM final_action_contract -> Trader execution -> Accountant outcome -> update preference`。state 由品种、板块、方向、setup 类型、horizon、market regime、evidence combo 和当日数据质量构成；action-value 只分 open/hold/exit/execution 四条主线。probe 是 PM 的探索权限或仓位形态，不是单独 action-value 词表；reduce/scale/add 由 `final_action_contract.current_lots -> target_lots -> lots_delta` 推出，归入 hold/exit 或 open 生命周期。Researcher 可以自由分析原因，但只有被写成固定集合内的结构化 `action_preference`，并被 PM 在未来交易日消化进最终合约，才算完成研究闭环。Trader 不直接读取研究 action-value，只执行审计后的最终合约。

## 二、当前代码落地的具体机制

Phase4 已经拆成 Reviewer 和 Researcher 两个角色。Reviewer 是确定性复盘者，负责检查 Phase1-3 是否完整、账务是否一致、交易流水是否入账、完整交易日志是否输出，并决定 Phase4 是否通过。Reviewer 不调用 LLM，不下单，不改账，也不写交易指令。Researcher 是研究员，只在 Reviewer 验证后的事实底座上写入未来可用记忆和研究结果；只有 Researcher 可以调用 LLM 做因果研究和探索式假设。

Researcher 当前会写入多类记忆：真实交易片段记忆、未交易机会记忆、no-trade 影子结果、Neutral 责任与后续窗口、探索式假设、分析师学习摘要、策略记忆、临时策略状态、成熟自适应策略状态、资本部署状态、学习事件账本、学习上下文预算和研究到仓位反馈账本。真实交易记忆和未交易机会记忆会带上数据依据，记录用了哪些 PandaAI、Finoview 或新闻字段，形成了什么判断，最后如何影响仓位与盈亏。所有 no-trade reason 会统一收束为“信号、风控、择时、执行、业务、学习”六类，并写入记忆 payload、证据摘要和学习事件，使后续研究能区分系统到底是不会看、不敢做、没等到、做不了、业务上不该做，还是学习边界在起作用。若 Trader 因触及涨跌停价而跳过原本可交易的推荐，Researcher 也会把该样本写成“择时/执行价错失机会”记忆，供下一轮分析入场时机、追价边界和回落/反抽条件，而不是直接授权放仓。

基础研究表的写入能力应在每轮干净回测后重新验收，包括 `learning_event_log`、`trade_episode_memory`、`no_trade_opportunity_memory`、`exploratory_hypothesis`、`learning_context_budget` 和 `research_position_feedback`。如果用户已经手动删除全部回测与研究记录，旧主库行数不再代表当前事实；下一轮应重点检查这些表是否重新积累样本、是否被分析师和 PM 读取、是否最终改变 `lots/margin_ratio/action`。

所有核心记忆都会尽量挂载 `next_round_memory_contract`，当前版本为 `next_round_strategy_update_v2`。这个契约把记忆统一成“下一轮策略更新”：写明作用域、可用经验、数据关注点、分析师动作项、PM 动作条件、失效条件、仓位权限、最大仓位影响和防过拟合边界。统一的是记忆格式，不是统一交易策略；不同品种、方向、周期和市场状态仍可形成差异化经验。

分析师团队 technical、fundamental、commodity_news 会在 prompt 中读取受限学习上下文。它们可以看到成熟摘要、相似完成交易、未交易机会及影子结果、探索式假设和下一轮策略更新契约。分析师引用记忆时必须把历史经验与当日数据进行比较，说明当前证据是确认、削弱还是反驳该记忆。Neutral 仍是合法信号，但需要说明证据缺口、冲突因素和转向条件。

分析师信号已经进一步接入“可交易机会”分层，而不是只输出方向观点。commodity_news 会区分真实催化、普通方向性消息和噪声；缺少强催化或价格/盘中反应时，只能作为 `direction_only` 或 watchlist 观察线索。technical 会把趋势延续信号绑定到 market regime，震荡、区间、弱趋势或高波动状态下的普通趋势信号会降级为 `direction_only`，除非有突破、量能或盘中确认。fundamental 的中期/长期观点不能直接短线落仓，必须补短线触发、失效边界和持仓周期说明。以上分层写入 signal metadata、证据冲突和 opportunity layer，不按品种黑名单处理。

当 fundamental 已经补足短线触发、失效边界、数据支撑和 setup 契约时，质量函数允许它进入 `tradeable_setup/deployable_alpha` 审查；若缺少这些条件，仍只能作为方向观点、watchlist 或 Neutral。这样基本面不再只是中期解释，也不会在证据不足时冒充短线交易机会。

非 Neutral 信号会形成机器可读 setup 质量评分。`setup_quality_score`、`entry_quality` 和 `setup_quality_notes` 会综合可交易原因、入场触发、失效边界、退出提示、持仓周期、数据覆盖/新鲜度、市场状态、价格位置、新闻催化和基本面短线触发，判断当前信号是可交易 setup、方向观点还是弱 setup。该评分只用于提高信号质量和 PM 复核，不是硬性品种规则；当 setup 不足时，信号可以保留为方向观点或观察，但不能直接放大为正常仓位。当前代码进一步区分关键执行字段与描述性字段：缺少入场触发或失效边界才会把机会降级为方向观点；缺少 factor focus、holding hint、exit hint 等描述性字段只进入审计提示，不应机械吞掉已有触发与失效边界的可交易 setup。高波动等软风险交给 PM/Auditor 做限仓、probe 或 cap，不直接等同于 no-trade。

Portfolio Manager 会读取学习上下文、分析师契约、策略记忆、自适应策略状态、临时策略状态、资本部署状态、市场确认和数据质量摘要。PM 可以让成熟经验影响开仓、加仓、减仓、退出或资金释放，但候选假设、影子记忆和单笔交易片段只能作为分析先验或观察候选，不能单独支撑 `position_matched`、加仓或亏损仓继续持有。亏损仓继续持有必须重新接受当日证据验证。

PM 融合层已经读取 `opportunity_layer` 和 `opportunity_type`，把可交易机会、方向观点、风险减仓信号和 no-trade 分开处理。`deployable_alpha` 和 `tradeable_setup` 可以在当日证据、失效边界和审计通过时正常进入仓位决策；`direction_only/watchlist/no_trade` 只能观察或等待触发，不能独立变成真实新开仓；`no_trade` 不给新仓权限。该机制是可校准弱参，不是固定品种规则，目的是把“观点”与“交易机会”分开，减少弱多头、单一驱动信号反复落仓，同时保留真正被当日结构化证据重新证明后的探索空间。

PM 还会生成 `opportunity_scorecard`。它把分析师是否给出可交易 setup、业务质量、信号置信度、失效边界、market confirmation、数据缺口、正负学习状态和当前证据冲突合成 long/short 两侧的机器可读机会评分，并给出 `deployable_alpha/tradeable_setup/direction_only/no_trade` 层级。这个评分卡不是新智能体，也不是交易硬规则；它只把分散证据收束成同一口径，帮助 PM、Reviewer 和 Researcher 判断“是真机会、只是方向观点，还是不该交易”。PM 新仓 gate 会读取该评分卡：`no_trade` 不开新仓，`direction_only/watchlist/no_trade` 只观察或等待触发，`tradeable_setup/deployable_alpha` 才进入后续仓位、Auditor 和 Trader 检查。PM 还会用 `opportunity_quality_position_sizing` 把仓位大小和机会/setup 质量绑定：强机会可以受控落仓，弱 setup 只能观察或在被当日证据重新证明为可交易后进入探索候选。所有调整仍受当日证据、Auditor、Trader 和 20% 保证金硬上限约束。

PM 的早期机会评分入口已经读取同作用域 `adaptive_policy_state`。Researcher 写入的 `fast_candidate_alpha`、`alpha_promotion`、`loss_template_policy`、`tail_loss_sentinel` 和 `learning_mechanism:*` 不只停留在归因报告里，而会在 probe seed、cap/review 和机会层级判断时被看见；但它们仍只是可反驳先验，必须经过当日数据、失效边界、Auditor、Trader 和硬业务边界。

持仓生命周期现在同时保留两类方向：亏损仓必须再验证，盈利仓不能被机械减掉。若已有盈利仓位在同作用域下仍有同向证据、market confirmation 和可交易支持，`winning_template_continuation` 可以少减仓或继续持有；该机制不保护亏损仓，不支撑候选假设硬扛。若同品种同方向近期频繁交易且已结算表现差，`trade_churn_cost_control` 会做交易磨损 cap，减少手续费和反复错误对 alpha 的侵蚀。

PM 当前还接入了两条收益导向的受控落仓通道。第一条是 `mature_alpha_release`：只有同作用域成熟策略状态、当日市场确认、可交易 setup、失效边界和样本/置信门槛同时满足时，才允许在 20% 保证金硬上限、Auditor 和 Trader 执行边界内小幅释放仓位。第二条是 `fast_candidate_alpha_probe`：高质量机会若当日没有落仓或没有成交，Researcher 会在未来结算后看 shadow；若同作用域 shadow 证明该机会有正向价值，会写入 `fast_candidate_alpha`，PM 下一轮只允许在当前仍有可交易 setup、当日确认和失效边界成立时给受控有效 probe。前者避免成熟 alpha 被压住，后者避免系统学得过慢、错过真正 alpha；两者都不是品种白名单，也不能支撑亏损仓硬扛。

Auditor 不调用 LLM，只读取结构化状态。它区分 candidate、watchlist、weak_block、protected、deployable、tail_loss_sentinel、alpha_promotion 等层级：候选探索只能提示分析方向；成熟 protected/deployable 经验可以在当日证据、市场确认和失效边界满足时帮助释放仓位；tail-loss sentinel 只做短期 cap/probe，不是品种黑名单。历史弱表现、弱质量和新闻单驱动默认不再作为硬拦截，而是进入 probe_only、scale_down 或 cap；这样研究结论能限制仓位、保留受控验证和退出空间，而不是把系统推向长期不交易。

Trader 只执行 PM/Auditor 已批准且盘中触发条件满足的计划，不创造新策略。候选假设、小样本记忆和泛化记忆不能绕过盘中确认；只有成熟且验证过的 protected/deployable 记忆，才可能在严格条件下支持有限的执行 fallback。盘中触发失败、涨跌停/执行价问题、市场规则跳过等会写入 `execution_learning_trace`，进入 no-trade 六类原因与研究记忆，供未来研究择时和执行策略。Accountant 只负责结算、保证金、手续费、持仓、权益和 PnL，学习不能改写账务事实。

学习进入下一轮的路径是：Phase4 结算事实和交易日志形成记忆；Researcher 将事实转成可检索记忆和下一轮策略更新契约；下一交易日分析师和 PM 读取有预算限制的学习上下文；PM/Auditor 再根据成熟度、作用域、当日证据和风险边界决定是否落实到仓位。

为避免“研究只停留在归因解释”，当前新增了 `research_position_feedback` 闭环账本。每个交易日 Phase4 会把被推荐读取到的记忆、策略状态、PM 目标仓位、Auditor 审核、Trader 是否成交、no-trade 原因、当日交易结果和结算结果写成机器可读反馈，并同步生成 `portfolio_manager` 可检索 digest。该反馈只作为下一轮同作用域分析和 PM 复核材料，不能单独授权放仓；只有后续反复同作用域验证并晋升为成熟策略状态后，才可能通过原有 PM/Auditor 边界影响仓位。

当前已经不再只用粗糙的 learned / unlearned 口径评价学习效果。Reviewer 学习报告和评估模块会把已完成交易按具体学习机制拆开统计，包括 `alpha_promotion`、`tail_loss_sentinel`、`technical_parameter_calibration`、`loss_template_policy`、`learned_vs_unlearned`、`strategy_memory_protected`、`strategy_memory_weak_block`、`strategy_memory_watchlist` 等，并分别输出交易笔数、胜率、净 PnL 和平均 PnL。在此基础上，Researcher 会把达到样本与绩效门槛的同作用域学习机制写入 `adaptive_policy_state`，`policy_type` 形如 `learning_mechanism:alpha_promotion`、`learning_mechanism:strategy_memory_weak_block`。正向机制写成 `protect`，只在同品种、方向、模板、周期、市场状态、当日证据、失效边界、PM/Auditor/Trader 和 20% 保证金硬上限都通过时，才可能帮助仓位释放或持仓权限；负向机制写成 `cap`，只能做同作用域降权、probe/cap/reduce，不形成品种黑名单，也不能污染当日决策。

为了不让止损学习过慢，Researcher 还会写入 `fast_loss_sentinel`。当短窗口内同作用域快速出现亏损，系统先生成短期 cap/probe 保护，PM 只对同作用域新/增仓做缩放；该状态有效期短、可被后续 shadow 与同作用域样本反证，不会变成永久压仓规则。PM 会把 `loss_template_policy`、`fast_loss_sentinel`、`tail_loss_sentinel` 和 `learning_mechanism:*` 的实际影响写进 `learning_to_position_trace`，因此后续可以检查某条研究结论到底有没有进入仓位，而不是只停留在解释文本。

干净回测后需检查 `adaptive_policy_state` 是否重新出现并持续更新，包括 `contextual_rule_calibration:portfolio_manager`、`contextual_rule_calibration:intraday_confirmation`、`alpha_promotion`、`contextual_rule_calibration:technical_parameters`、`template_quality`、`learned_vs_unlearned`、`tail_loss_sentinel`、`learning_mechanism:*` 等类型。验收重点不是写了多少行，而是这些状态是否被 PM/Auditor 读取，并在合规边界内改变开仓、加仓、减仓、退出或资金释放。

学习机制分项识别已经扩展到完整 PM/Auditor trace：不仅读取 Auditor diagnostics，也读取 `strategy_controls`、`learning_to_position_trace`、`adaptive_policy_state`、技术参数校准 metadata 和记忆引用。这样可以追踪某条记忆是否进入 prompt、是否被 PM 看到、是否对应到成熟/候选策略状态，以及它最终对仓位链路的影响；不会再只因为字段藏在 PM trace 里就被误判为“未学习”。

为避免小样本经验过早成熟，Researcher 在把 `alpha_promotion`、`template_quality`、`loss_template_policy`、`learning_mechanism:*` 写入 `adaptive_policy_state` 前，会经过 `policy_promotion_guard`。该闸门要求同作用域样本至少覆盖一定交易日数和日历观察跨度，并检查单笔极端盈亏是否占比过高；不达标的正向经验只保留为 watchlist/观察，不直接获得 protect 权限，不达标的负向经验也不会升级成强 cap。为避免亏损模板把系统压得太死，负向 cap 还会读取同作用域 no-trade shadow 结果；若被压制的机会在未来结算后反复显示正向 shadow PnL，系统会写入 guard 事件并撤销/降级对应 active cap。这个过程不写品种黑名单，不使用未来数据改变当日决策，只让未来交易日知道哪些历史压制需要被反证。

Phase4 还会校验当日分析师信号的事实底座：推荐快照和 signal 表必须覆盖全部 `ticker × analyst` 组合，且 signal 表同一品种同一分析师只能有最终一条。这个校验不创造交易规则，但会保护研究机制，避免 Neutral 比例、分析师表现、动态权重、学习摘要和下一轮记忆被重复或缺失的 signal 污染。

Researcher 还会把 technical 分析师在同品种短周期上的历史表现，沉淀为 `contextual_rule_calibration:technical_parameters`。这类记忆不直接给 PM 放仓，也不改变 Auditor 或 Trader 的硬边界，只允许 Technical Analyst 在下一轮分析前，对 EMA、RSI、Bollinger 这类技术参数做很小幅度的有界校准，并把实际采用的参数和校准原因写入 signal metadata 与分析师报告。它的目的不是让研究机制替代交易决策，而是让学习能逐步影响“怎么看行情”，再由后续信号、PM、Auditor、Trader 决定是否落到仓位。

亏损模板研究分为两层：第一层是 candidate `loss_template_observation`，只做观察性研究和下一轮分析先验；第二层是成熟 `loss_template_policy`，只有同一品种、方向、模板、周期、市场状态下样本数、累计亏损和置信度达到配置门槛后才写入 `adaptive_policy_state`。成熟亏损模板也不是品种黑名单，只能在同作用域且当日证据仍弱、失效边界不足时，通过 PM/Auditor 触发 probe/cap/reduce；若当日证据明显改善或市场状态不同，PM 应记录反证并允许正常审查。

亏损模板现在会进一步写入 `failure_family` 和 `data_combo`。`failure_family` 用来区分新闻事件试探失败、震荡市场里的趋势延续失败、中期基本面缺短线择时失败、数据缺口驱动失败等场景；`data_combo` 记录当时 PandaAI、Finoview、新闻和 market confirmation 等数据是否可用、是否使用、是否滞后。它们只用于下一轮分析和同作用域仓位复核，不是品种规则，也不能越过当日证据、失效边界和 Auditor。

干净回测后需确认 `loss_template_observation` 能否积累为 `loss_template_policy`，并写入 `failure_family`、`data_combo` 和对应动作边界。这些字段只用于下一轮同作用域分析和 PM 复核，不是品种黑名单，也不能越过当日证据、失效边界和 Auditor。

当前进一步落地了 Alpha Setup 档案层。Researcher 会在 Phase4 后把每个 setup 的交易、未交易、执行结果和后续表现，按品种、方向、周期、市场状态、setup 类型和数据组合写入 `alpha_setup_sample`，并汇总成 `alpha_setup_profile`。Profile 会按样本数、胜率、盈亏因子、净 PnL、最大亏损和观察有效期形成 `candidate/watchlist/protected/deployable/capped/rejected` 生命周期。该层不是固定交易规则，也不是品种黑名单；它只是把干净回测暴露出的“哪些 setup 真可交易、哪些 setup 容易亏、哪些 setup 只是方向观点”沉淀成下一轮可检索、可验证、可反驳的档案。若回测记录已被清空，则这些档案必须从新回测事实重新生成，不能依赖旧库。

同时，系统会为同作用域 setup 写入 `alpha_setup_action_value`。它记录 open、hold、exit、execution 四类动作的后续样本表现，用于形成受控的动作先验。这个机制接近轻量 bandit/弱强化学习，但不做端到端自动交易，不直接给智能体交易权，也不使用未来结果污染当日决策。Researcher 只能在未来已结算后更新动作价值，并且必须写清 `action_value_lane`、`usage_boundary`、`usable_by`、`allowed_effects`、`forbidden_effects` 和 `signal_calibration`。固定 `action_preference` 词表只允许：`positive_candidate_open`、`positive_candidate_hold`、`positive_candidate_exit`、`positive_candidate_execution`、`negative_revalidate`、`negative_hold_revalidate`、`tail_loss_protect`。PM 只能读取 open/hold/exit 对仓位生命周期的偏好，并把 execution 偏好写入最终合约；Trader 只读审计后的最终合约和盘中数据；分析师只能读取 `signal_calibration` 来校准当日证据质量，不能把 action-value 转成交易授权、手数、保证金或方向覆盖。

当前系统已经接入轻量 SQL 相似 setup 检索，而不是向量库或长文本 RAG。检索只按 ticker/sector/side/setup_type/horizon/regime/action 等结构化键返回 compact evidence，并强制历史样本 `trading_date < decision_date`。同品种同作用域真实样本可以进入对应 action lane；同板块样本、similar SQL/RAG 和 shadow 只能作为弱先验帮助分析师和 PM 对照当日证据，不能直接生成开仓权限，不能覆盖同作用域负期望，也不能绕过 `final_new_entry_trade_authority`、Auditor 和 Trader。

Alpha setup 档案会进入下一轮分析师和 PM 的学习上下文。分析师看到它时必须比较当日数据是否确认或反驳该 setup；PM 的 `opportunity_scorecard` 和 `alpha_setup_ev_fusion` 会读取同作用域 profile。成熟正向 profile 可以在当日触发、失效边界、market confirmation、Auditor、Trader 和 20% 保证金硬上限都通过时，支持受控落仓或少减仓；负向 profile 只能触发同作用域复核、probe/cap/reduce，不能变成全局压仓或品种黑名单。

当前 PM 已进一步把 `alpha_setup_action_value` 接入收益主决策。也就是说，Researcher 写入的 open/hold/exit 等仓位动作价值，不再只停留在 prompt、trace 或诊断中；PM 会在 `alpha_setup_ev_fusion` 中读取同作用域、同 action lane 的手续费后收益、样本数、胜率、置信度和固定 `action_preference`。若同作用域 open/profile 呈现正期望，且当日触发、失效边界、market confirmation、Auditor、Trader 和 20% 保证金硬上限通过，PM 可以受控放大；若未知，则保留受控探索；若 hold/exit 呈现回吐或保护收益，则同作用域进入持仓保护、减仓或退出倾向。execution action-value 不进入 PM 改方向/改手数，只能被 PM 消化为最终合约的执行 profile；Trader 不直接读取研究 action-value。该机制不是产品黑名单，也不是固定规则；它的目的只是把研究结果真正转成“该不该交易、交易多大、何时退出、怎样执行”的分动作主路径。

当前进一步收紧了 action-value 到真实落仓的资格边界。负期望同作用域 setup 若缺少新的技术确认、高质量新闻催化，或多分析师同向并带失效边界的当日新证据，只能进入 watchlist/shadow，不再被普通最小开仓阈值、方向观点 probe、horizon mismatch probe 或最小一手机制转成真实开仓。正期望 setup 若样本、置信、收益和当日证据通过，仍可进入受控 probe/open；未知 setup 若有新的当日可交易证据，也保留受控探索空间。这个边界服务收益主线：减少重复亏损真实落仓，同时避免把系统压成长期不交易。

Trader 会把 setup 层面的执行反馈写入 `setup_execution_learning`，包括机会层级、alpha setup 融合结果、盘中触发、未成交原因和执行状态。Researcher 后续可把“信号质量、PM 是否落仓、Auditor 是否放行、Trader 是否成交、后续盈亏”统一接回 alpha setup 档案，形成更完整的“研究到仓位、仓位到结果、结果再研究”的闭环。

当前进一步要求 `execution_action_value` 只能通过 PM 进入最终合约。它只在 PM/Auditor 已授权计划内生效，用来让 PM 选择更适合某个同作用域 setup 的执行方式，例如突破、回踩、VWAP、开盘区间、立即执行或跳过追价，并写入 `final_action_contract.execution_plan/execution_profile`；它不能创造方向、不能替代盘中触发，也不能改变回测和模拟盘的一致执行语义。

当前进一步补齐了 Alpha Setup 到仓位策略状态的闭环。Researcher 会在 Phase4 后把 `alpha_setup_profile` 的生命周期和 `alpha_setup_action_value` 的动作收益，翻译成未来 PM 可读取的 `adaptive_policy_state`。成熟正向 setup 会写成 `learning_mechanism:alpha_setup_ev/protect`，早期正向候选会写成 `fast_candidate_alpha/probe`，负向 setup 会写成 `learning_mechanism:alpha_setup_ev/cap`。这是一种轻量 bandit 式的在线学习：状态是品种、方向、周期、市场状态、setup 类型和数据组合；动作价值只按 open/hold/exit/execution 四条主线记录；probe 是探索权限，reduce/scale/add 是最终合约手数变化结果；奖励是结算后的净 PnL、胜率、盈亏因子、最大亏损和执行质量。它只影响未来交易日，只按同作用域进入 PM 复核，不写品种黑名单，不绕过当日证据、失效边界、Auditor、Trader 和 20% 保证金硬上限。

当前进一步要求 action-value 必须匹配真实交易动作。PM 在使用 `alpha_setup_action_value` 前，会先识别当前意图属于 open、hold、exit 或 execution，再只读取同动作类型的历史收益；历史 hold 正收益不能单独证明新开仓正期望，历史 execution 正收益不能改变方向或手数，历史 exit 正收益不能反向支持加仓。Researcher 侧也会把同作用域多笔交易后净亏且盈亏因子差的 profile 降为 capped/revalidate 类保护偏好，避免负期望 setup 继续作为普通 watchlist/probe。该机制保护“持仓 alpha”不被误用为“新仓 alpha”，同时保留真正有当日新证据或正向 open 经验的受控探索空间。

当前进一步补充了受控探索释放边界。`real_probe_positive_or_strong_confirmation_release` 只能释放非方向观察类软拦截，例如样本不足但当日证据已经重新证明为 `tradeable_setup` 或 `deployable_alpha` 的机会；它不是最终开仓权限。若机会仍带有 `pm_direction_only_probe_cap`、`direction_only_cannot_open_position`、`daily_tradeability_watchlist_only`、`scorecard_layer=direction_only/watchlist/no_trade` 等方向或观察语义，即使存在正向历史线索，也不能被 release 直接放成真实 probe。若存在当前技术触发或事件催化、失效边界和 market confirmation，必须先由结构化证据把机会重新证明为可交易 setup，再进入受控 `exploration_probe` 复核。若存在负期望、重复亏损 watchlist、缺失失效边界或业务硬风险，则不能释放。

PM 最终推荐前还会执行 `final_new_entry_trade_authority`。它只作用于当前无仓的新开仓，不影响平仓、减仓、换约、强反转退出或盈利仓持有保护。若最终 `target_lots` 仍来自 `direction_only`、`pm_direction_only_probe_cap`、`direction_only_cannot_open_position`、`daily_tradeability_watchlist_only`、弱 market confirmation 等弱证据，系统必须把它留在 watchlist/shadow，不能让最小一手、软 probe 标签或 release 标签把它变成真实开仓。若机会已经被当前结构化证据证明为 `tradeable_setup` 或 `deployable_alpha`，且具备正期望、强确认或合格释放，则仍允许受控 `exploration_probe` 或 `real_budget_entry`。这个出口把“错误尝试仓”挡在最终动作前，同时保留真正可验证的探索仓。

## 三、当前研究与交易出口语义边界（2026-06-17）

为避免旧 probe 术语误导后续审计，当前研究闭环按以下口径执行：

1. `direction_only_new_entry.allow_probe` 是旧配置兼容字段，只代表方向观点观察候选。它不能创建真实开仓权限；真实新开仓必须经过 PM 的 `final_new_entry_trade_authority`，并落到 `authority_type`、`can_open_real_position`、`can_apply_min_real_floor`、`target_lots` 和 Trader 执行审计。
2. 同板块学习 fallback 只作为 broad prior。Researcher 和 learning context 可以把同板块案例放入 prompt 帮助分析，但不能把它当作同品种 action-value，不能用来放大仓位、跳过当日触发或覆盖同作用域负期望。
3. `open/hold/exit/execution` 必须分开学习。持有赚钱不能证明新开仓赚钱；执行失败也不能被归因成信号失败。后续审计要检查 action-value 是否按真实动作进入 PM，而不是只写研究记录。
4. `real_probe`、`release` 等历史文档词汇统一按当前最终出口解释：它们只表示受控探索释放线索，不是开仓权限。只有 PM 最终出口、Auditor、Trader 和交易日志共同证明真实落仓，才算交易闭环完成。
5. 旧 `block/cap/probe` 字段不再各自形成平行门控。它们先由 reason-effect 统一解释为硬风险、软风险、学习调整或释放信号，再交给 PM 最终出口仲裁。硬风险仍必须阻断；软风险只应缩放、复核、保护或要求更强确认，不应多层叠乘把可验证 alpha 压成 0。
6. 正向 action-value 不能裸放大，负向 action-value 也不能永久封杀。两者都必须与当前证据、失效边界、market confirmation、Auditor、Trader 和资金上限共同作用。未知 setup 仍保留受控探索空间；只要决定做 probe，就必须遵守当前资金参数定义的有效 probe 边界。

## 四、旧 probe 表述废止说明（2026-06-17）

本文前文若出现“direction-only 小仓 probe”“real probe 释放”“release 穿透”等历史表述，只保留为旧阶段审计线索，不再作为当前交易权限定义。当前统一翻译为：受控有效探索候选、受控探索释放、最终交易权限穿透。

当前有效口径只有一个：方向观点、同板块 fallback、历史相似案例、弱 market confirmation、软风险标签都不能单独生成真实开仓权限。真实新开仓必须同时通过 PM 的 `final_new_entry_trade_authority`、当日可交易证据、失效边界、资金约束、Auditor、Trader 执行审计和交易日志。若其中任何一环没有落到 `authority_type / can_open_real_position / target_lots / execution_audit`，就只能算观察候选或历史诊断，不能算交易闭环。
