# 参数调节备忘录

本备忘录用于 smoke test 结束后的长期干净回测计划：从 `2025-05-01` 连续回测到 `2026-06-01`。目标不是为了调参本身，而是让系统更接近稳定正收益：合格机会能落成有意义仓位，弱方向观点不乱开仓，盈利 setup 能晋升，亏损 setup 能降级，资金利用率具备实战部署意义。

smoke test 用于确认两类事情：第一，代码、数据、LLM、账务、执行、学习链路是否按设计打通；第二，小样本策略行为是否明显异常，例如一直不交易、资金利用率没有部署意义、入场/退出明显断链、PM 推荐与 Trader 执行不一致、学习记录只写不读。长期回测才用于评估系统生成的交易策略是否有稳定收益质量。不能把 smoke test 盈亏直接当成最终策略结论，也不能因为单日或单品种盈亏立刻过拟合改参。

## 不要动的硬参数

- `dev.yaml:max_total_margin_ratio` 固定 0.20，组合保证金最多 20%。
- `execution_commission_catalog.yaml` 是手续费事实表，除非手续费规则有真实变化，否则不调。
- `execution_slippage_catalog.yaml` 是滑点 tick 假设，除非执行记录证明假设明显失真，否则不调。
- `data_factor_policy_catalog.yaml` 是 PandaAI/Finoview/新闻数据入口和数据质量策略，除非数据源、字段可用性或确认用因子集合真实变化，否则不调。
- `finoview_factor_catalog.yaml` 是本地 feather 字段目录，只在本地字段真实新增、删除或重命名时改。
- 交易事实、推荐、结算、PnL、完整交易日志、原始信号不自动清理。

## 可微调的弱参数

- `dev.yaml:position_budget_policy`：真实开仓最低保证金、probe/normal/deployable/exceptional 分层。
- `dev.yaml:capital_utilization_control`：合格机会的资金释放目标，不能突破 20%。
- `portfolio_policy_catalog.yaml:portfolio_manager / market_confirmation / alpha_setup_ev_fusion / holding_rebalance_control`：机会分层、市场确认、正负期望、持仓生命周期。运行时由 `config_normalizer.py` 展开到 PM 读取的配置形状。旧 `block/cap/probe/reduction` 字段是兼容输入和动作倾向，不是独立交易权限。
- `portfolio_policy_catalog.yaml:gatekeeping_policy`：统一门控语义说明。旧 `block/cap/probe` 字段必须先经 `reason_effects.py` 解释为硬风险、软风险标签、学习调整或释放信号，再由 PM 最终出口统一仲裁；不能把多层软限制叠乘成无交易，也不能让释放信号绕过硬风险。
- `analyst_prior_profiles.yaml`：中期方向背景与日频交易时机的冷启动先验；会展开到旧字段 `sector_weights / strategic_view_weights`，但只能辅助排序和上下文解释，不能被解释成静态加权开仓规则，也不能直接生成 open/add/scale 权限。
- `execution_exit_policy_catalog.yaml`：ATR 止损、probe time stop、趋势仓 time stop。
- `learning_policy_catalog.yaml`：学习、记忆、Neutral 追责、action-value 和保留周期。只能基于足够样本微调，不能把少数亏损写成死规则；`learning_gatekeeping_policy` 明确学习结果是 open/hold/exit/execution 的动作偏好，不是直接交易命令。学习侧的 `cap/probe/block` 只能影响同作用域偏好、确认要求或保护动作，不能绕过 PM/Auditor/Trader。

## 分阶段检查与调参节点

- smoke test 结束：先确认模型调用、数据读取、无未来数据污染、Phase1-Phase4、账务、Trader、Researcher、配置展开全部正常；再检查小样本策略行为是否有明显业务异常，包括是否交易、资金利用率是否有部署意义、开仓/加仓/减仓/平仓是否落实到交易出口、入场/退出是否明显断链、PM 推荐与 Trader 执行是否一致、学习记录是否被 PM 读取并改变 lots/margin。若有链路问题，先修链路，不进入长期回测；若只是正常业务亏损，不能立刻过拟合改参。
- 跑到 `2025-05-07`：检查 Phase1 是否完整、是否正常交易、真实新开仓是否满足最低保证金、是否有异常 no-trade、probe 是否被吞掉。这里主要修链路，不轻易调收益参数。
- 跑到 `2025-05-16`：检查资金利用率、真实 probe、normal/deployable 仓位是否落地。如果合格机会仍过小，才看 `position_budget_policy` 和 `capital_utilization_control`；如果弱机会亏损，先查资格链，不直接压低所有仓位。
- 跑到 `2025-06-01`：做第一个完整月收益审计。重点看每日 PnL、品种、动作、PM scorecard、alpha EV、分析师信号、Trader 执行和 Researcher 写入。只允许小幅修正明显不符合实战意义的弱参。
- 跑到 `2025-07-01`：检查学习机制是否开始影响下一轮交易。重点看 open/hold/exit/action-value 是否被 PM 读取并改变 lots，不看只写记录的假闭环。
- 跑到 `2025-09-01`：做季度级审计。此时样本开始足够，才允许评估 `analyst_prior_profiles.yaml`、`portfolio_policy_catalog.yaml:alpha_setup_ev_fusion`、`execution_exit_policy_catalog.yaml` 是否需要阶段性微调。若发现 `analyst_prior_profiles.yaml` 被当成静态权重开仓规则，优先修 PM/配置展开语义，不先调数值。
- 跑到 `2025-12-01`：做跨市场状态审计。重点看趋势、震荡、反转、事件驱动下，技术触发、基本面背景、新闻催化和 Trader 执行是否各司其职。
- 跑到 `2026-03-01`：检查长期学习是否过拟合、是否压死交易、是否形成隐性品种黑名单，必要时调整学习保留、样本门槛和 action-value 晋升/降级弱参。
- 跑到 `2026-06-01`：做完整长期收益审计，再决定是否进入模拟盘或继续优化。结论必须包括净收益、最大回撤、资金利用率、胜率、盈亏比、平均持仓、手续费后收益、错过机会和机制实际落仓效果。

## 每次请求调参时必须回答

- 为什么这个参数直接服务收益，而不是只完善机制。
- 依据来自哪段回测、哪类交易、哪条学习记录或哪条执行记录。
- 修改后预期改变什么交易行为，例如提高合格机会仓位、减少弱方向开仓、保护盈利持仓或加快亏损退出。
- 是否可能引入过拟合、压死交易、未来数据污染或隐性黑名单。
- 是否需要删除某段回测记录重跑；如果只是弱参微调，优先说明能否续跑，避免无意义重删。

## 清理规则

- 学习明细保留 90 天或约 60 个交易日。
- 聚合经验保留 180 天，只保留 active 或近期更新状态。
- 自动清理只在 Phase4/Researcher 之后执行，不能在 Phase1/PM 决策前执行。
- 交易事实永不自动清理；如果用户手动删除全部回测记录，必须先检查是否删干净，再决定能否重新跑。
