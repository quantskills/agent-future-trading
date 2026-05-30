# AgentQuant 记忆与研究机制

更新时间：2026-05-30

本文档用于后续优化 AgentQuant 的记忆、研究与策略自我迭代机制。它只记录已经代码落地或需要通过回测验收的机制，不把临时想法写成既成事实。

## 一、记忆与研究机制原则

AgentQuant 的记忆机制要服务未来决策，而不是只做事后解释。系统应尽量完整保存真实交易、未交易机会、Neutral 观望、影子结果、分析师判断原因、数据依据、PM/Auditor/Trader 决策、账务结果和后续表现，并让相关智能体在下一轮分析与决策时可检索、可引用、可反驳。

AgentQuant 的研究机制坚持自由探索式学习：让智能体从历史交易和未交易样本中主动探索期货价格走势、品类差异、分析侧重点、入退出场时机、持仓周期、失效边界和风险收益特征。学习的目标是提高交易信号质量、推动分析与交易策略自我迭代，并在成熟后落实到仓位，从而扩大 alpha 收益；不是不断堆规则、黑名单或硬约束来限制交易。

自由探索必须有边界。记忆和研究结论不能污染当日决策，未来结果只能在未来交易日结算后回填；候选假设不能直接放仓、加仓或支撑亏损仓硬扛；成熟经验也必须经过当日数据、市场确认、失效边界、PM、Auditor、Trader 和组合 20% 保证金硬门槛。系统要防止过拟合，优先使用同品种、同方向、同周期、同模板、同市场状态的经验，其次才是同板块经验，默认不使用全局泛化经验直接影响仓位。

归因的终点不是“为什么亏了或赚了”，而是“下一轮可用记忆”。每条研究结论都应尽量落到结构化的下一轮策略更新契约：适用范围、数据关注点、分析师下一轮该检查什么、PM 在什么条件下可开仓/加仓/减仓/退出、什么条件下经验失效、当前是候选假设还是成熟经验、最多能如何影响仓位。

## 二、当前代码落地的具体机制

Phase4 已经拆成 Reviewer 和 Researcher 两个角色。Reviewer 是确定性复盘者，负责检查 Phase1-3 是否完整、账务是否一致、交易流水是否入账、完整交易日志是否输出，并决定 Phase4 是否通过。Reviewer 不调用 LLM，不下单，不改账，也不写交易指令。Researcher 是研究员，只在 Reviewer 验证后的事实底座上写入未来可用记忆和研究结果；只有 Researcher 可以调用 LLM 做因果研究和探索式假设。

Researcher 当前会写入多类记忆：真实交易片段记忆、未交易机会记忆、no-trade 影子结果、Neutral 责任与后续窗口、探索式假设、分析师学习摘要、策略记忆、临时策略状态、成熟自适应策略状态、资本部署状态、学习事件账本和学习上下文预算。真实交易记忆和未交易机会记忆会带上数据依据，记录用了哪些 PandaAI、Finoview 或新闻字段，形成了什么判断，最后如何影响仓位与盈亏。所有 no-trade reason 会统一收束为“信号、风控、择时、执行、业务、学习”六类，并写入记忆 payload、证据摘要和学习事件，使后续研究能区分系统到底是不会看、不敢做、没等到、做不了、业务上不该做，还是学习边界在起作用。若 Trader 因触及涨跌停价而跳过原本可交易的推荐，Researcher 也会把该样本写成“择时/执行价错失机会”记忆，供下一轮分析入场时机、追价边界和回落/反抽条件，而不是直接授权放仓。

所有核心记忆都会尽量挂载 `next_round_memory_contract`，当前版本为 `next_round_strategy_update_v2`。这个契约把记忆统一成“下一轮策略更新”：写明作用域、可用经验、数据关注点、分析师动作项、PM 动作条件、失效条件、仓位权限、最大仓位影响和防过拟合边界。统一的是记忆格式，不是统一交易策略；不同品种、方向、周期和市场状态仍可形成差异化经验。

分析师团队 technical、fundamental、commodity_news 会在 prompt 中读取受限学习上下文。它们可以看到成熟摘要、相似完成交易、未交易机会及影子结果、探索式假设和下一轮策略更新契约。分析师引用记忆时必须把历史经验与当日数据进行比较，说明当前证据是确认、削弱还是反驳该记忆。Neutral 仍是合法信号，但需要说明证据缺口、冲突因素和转向条件。

Portfolio Manager 会读取学习上下文、分析师契约、策略记忆、自适应策略状态、临时策略状态、资本部署状态、市场确认和数据质量摘要。PM 可以让成熟经验影响开仓、加仓、减仓、退出或资金释放，但候选假设、影子记忆和单笔交易片段只能作为分析先验或观察候选，不能单独支撑 `position_matched`、加仓或亏损仓继续持有。亏损仓继续持有必须重新接受当日证据验证。

Auditor 不调用 LLM，只读取结构化状态。它区分 candidate、watchlist、weak_block、protected、deployable、tail_loss_sentinel、alpha_promotion 等层级：候选探索只能提示分析方向；成熟 protected/deployable 经验可以在当日证据、市场确认和失效边界满足时帮助释放仓位；tail-loss sentinel 只做短期 cap/probe，不是品种黑名单。

Trader 只执行 PM/Auditor 已批准且盘中触发条件满足的计划，不创造新策略。候选假设、小样本记忆和泛化记忆不能绕过盘中确认；只有成熟且验证过的 protected/deployable 记忆，才可能在严格条件下支持有限的执行 fallback。Accountant 只负责结算、保证金、手续费、持仓、权益和 PnL，学习不能改写账务事实。

学习进入下一轮的路径是：Phase4 结算事实和交易日志形成记忆；Researcher 将事实转成可检索记忆和下一轮策略更新契约；下一交易日分析师和 PM 读取有预算限制的学习上下文；PM/Auditor 再根据成熟度、作用域、当日证据和风险边界决定是否落实到仓位。

Phase4 还会校验当日分析师信号的事实底座：推荐快照和 signal 表必须覆盖全部 `ticker × analyst` 组合，且 signal 表同一品种同一分析师只能有最终一条。这个校验不创造交易规则，但会保护研究机制，避免 Neutral 比例、分析师表现、动态权重、学习摘要和下一轮记忆被重复或缺失的 signal 污染。

Researcher 还会把 technical 分析师在同品种短周期上的历史表现，沉淀为 `contextual_rule_calibration:technical_parameters`。这类记忆不直接给 PM 放仓，也不改变 Auditor 或 Trader 的硬边界，只允许 Technical Analyst 在下一轮分析前，对 EMA、RSI、Bollinger 这类技术参数做很小幅度的有界校准，并把实际采用的参数和校准原因写入 signal metadata 与分析师报告。它的目的不是让研究机制替代交易决策，而是让学习能逐步影响“怎么看行情”，再由后续信号、PM、Auditor、Trader 决定是否落到仓位。

## 三、回测验收项与待校验弱参

以下项目需要在下一轮干净回测中验收：

1. `trade_episode_memory`、`no_trade_opportunity_memory`、`exploratory_hypothesis`、`strategy_memory`、`adaptive_policy_state` 和 `learning_event_log` 是否按 Phase4 写入，且 payload 中含 `next_round_memory_contract`。
2. 分析师和 PM prompt 是否实际出现 `Next-round strategy update`，并能引用真实交易、未交易机会、影子结果和探索假设。
3. 候选假设是否只作为分析先验，不直接触发放仓、加仓、`position_matched` 或亏损仓继续持有。
4. 成熟经验是否能在同作用域、当日证据确认、market confirmation、失效边界和 Auditor 允许时落实到具体仓位。
5. no-trade shadow、涨跌停错失成交 shadow 和 Neutral 后续窗口是否只在未来日期结算后回填，不能污染当日交易。
6. learned 交易是否不再长期显著跑输 unlearned；若继续跑输，需要拆分 alpha_release、risk_suppression、evidence_rejection 分别看。
7. `tail_loss_sentinel` 是否能短期抑制重复尾部亏损，但不会变成永久品种黑名单，也不会阻断必要减仓、平仓和换约。
8. `alpha_promotion` 是否只在真实交易或影子结果样本达标后生效，并且只在当前证据确认时帮助释放资金。
9. 记忆使用是否符合防过拟合边界：同品种同方向优先，同板块其次，不允许全局泛化经验直接放仓。
10. 每个交易日是否仍自动输出与模板一致的完整交易日志，且日志内容不因研究机制拆分而缺失。
11. `limit_locked_no_fill` 推荐是否进入 `no_trade_opportunity_memory`，payload 是否含 `market_rule_block`、`limit_lock_audit` 和 `next_round_memory_contract`，且该记忆只提示择时研究和 shadow 验证，不直接影响仓位。
12. `no_trade_opportunity_memory`、Phase4 零成交日诊断和 `learning_event_log` 是否写入六类 no-trade 分类；回测后能否按“信号/风控/择时/执行/业务/学习”解释未成交原因。
13. 推荐快照与 signal 表是否保持一致唯一；若 signal 缺失或重复，Reviewer 应阻断 Phase4，避免错误信号样本进入 Researcher 记忆。
14. `contextual_rule_calibration:technical_parameters` 是否只来自已结算的同品种 technical 短周期表现；Technical Analyst metadata 是否出现 `adaptive_params` 与 `technical_parameter_calibration`；校准是否只小幅调整技术指标参数，不直接放仓、不突破 20% 硬上限，也不导致系统长期不敢交易。
15. `loss_template_observation` 是否只基于已结算亏损样本写入 candidate `exploratory_hypothesis` 与 `learning_event_log`；payload 是否包含数据组合、市场状态、使用边界和 `next_round_memory_contract`；该记忆不得写品种黑名单，不得直接放仓、压仓或支撑亏损仓继续持有。
16. signal artifact 的机器可读元数据是否进入 Researcher 可检索材料；研究结论应能引用 `data_usage_summary`、`llm_path` 与技术参数校准信息，而不是只读取人类报告文本。

当前需要重点观察、暂不急着调整的弱参：

- `learning.memory_expires_after_days: 30`：普通记忆有效期。若好经验过早消失，可延长；若错误经验滞留，可缩短。
- `learning.overlay_expires_after_days: 10`：配置 overlay 有效期。若软调整过快反复，可延长；若压仓残留，可缩短。
- `learning.provisional_policy_state.valid_days: 10`：临时策略状态有效期。用于短期异常亏损或连续亏损保护。
- `learning.exploratory_research.valid_days: 30`：探索式假设有效期。若假设进入 prompt 后长期无验证，应缩短或提高样本门槛。
- `learning.tail_loss_sentinel.valid_days: 5`：尾部亏损哨兵有效期。若过度压仓可缩短，若重复尾亏仍多可延长。
- `learning.alpha_promotion.valid_days: 10`：正向 alpha 晋升有效期。若有效机会保持太短可延长，若放大错误经验可缩短。
- `learning.no_trade_opportunity_memory.shadow_forward_days: [3, 5, 10]`：未交易机会影子观察窗口。用于区分合理观望和错失机会。
- `learning.exploratory_research.min_episode_samples: 2`、`max_episode_samples: 24`、`max_hypotheses_per_day: 5`：控制 Researcher 研究素材和每日假设数量。
- `learning_context.max_items_per_prompt: 5`、`max_chars_per_prompt: 1200`、`exploratory_memory` 下各类 max items/chars：控制记忆进入 prompt 的预算，避免 prompt 膨胀和过拟合。
- `learning.anti_overfit.min_samples_for_template: 3`、`min_samples_for_policy: 4`：控制模板和策略状态晋升的最小样本。
- `learning.contextual_rule_calibration.technical_min_confidence: 0.35`、`technical_positive_hit_rate: 0.60`、`technical_weak_hit_rate: 0.40`、`technical_valid_days: 10`：控制技术参数校准的置信度、正负表现门槛和有效期。若回测显示校准后技术信号质量改善但反应太慢，可小幅放宽；若出现过拟合、低交易频率或错误参数滞留，应提高门槛或缩短有效期。
- `learning.loss_template_observation.lookback_days: 30`、`min_loss_samples: 1`、`min_cumulative_loss_abs: 1`、`max_rows_per_day: 4`、`valid_days: 30`：控制亏损模板观察性研究的窗口、触发门槛、每日数量和有效期。若样本太少导致噪声过多，应提高样本或亏损门槛；若亏损模板没有进入 prompt，可检查预算和有效期。

弱参调整原则：先用干净回测观察，不凭单日盈亏调参。只有当回测显示记忆没有进入 prompt、候选假设越权、成熟经验无法落仓、错误经验滞留、好经验过早失效或学习显著压仓时，才小范围调整对应弱参。
