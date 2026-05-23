# AgentQuant 策略绩效优化方案与回测前验收结论

更新日期：2026-05-22

本文档是本轮优化工作的回测前验收版。它不再只是优化设想，而是记录：

1. 该系统的两大基础功能与六大优化目的。
2. 本轮九次代码验收得到的结论。
3. 下一轮至少 6 个月正式回测期间，必须验收哪些优化成果。

本轮优化的底线是：代码层面该改的都必须在正式回测前完成，正式回测只用于验证策略效果、资金利用率、收益质量、学习效果和实盘可复刻性，不能再用短窗口结果反复小修小改。

## 一、系统的两大基础功能

### 1. 期货策略回测

系统以 15 个期货主力合约为交易对象：

`BU, C, CF, EB, HC, I, J, M, MA, P, PB, RB, SR, TA, ZN`

回测必须复用系统真实四阶段流程：

1. Phase1：盘前生成交易建议，只能使用 T-1 及以前的可见信息。
2. Phase2：交易员按盘前建议和盘中择时规则执行交易。
3. Phase3：会计师在收盘后按交易所结算价完成日终结算。
4. Phase4：复盘者校验交易、账务和归因，并写入学习结果。

回测不是单独的研究脚本，而是模拟盘和未来实盘流程的历史回放。

### 2. 模拟盘与实盘可复刻运行

系统的另一项基础功能是模拟盘运行，并为未来实盘保留一致路径。

盘前建议、盘中择时、成交基准、滑点、手续费、保证金、日终结算、复盘学习，都必须使用确定性代码和统一 artifact 口径。LLM 可以用于分析师信号和复盘解释，但不能直接决定成交价、成交手数、账务结算、硬风控或模板权限。

## 二、六大优化目的

### 1. 扩大策略 alpha 收益

通过结构化基本面因子、新闻事件质量判断、技术形态模板、horizon 分层、模板治理和历史学习，提高交易信号的真实正期望，而不是只靠加杠杆放大噪音。

### 2. 提高资金利用率

将平均保证金占用从约 2% 提升到 capacity-aware 的真实可用区间：基础目标 8%-12%，强机会目标 16%-20%。资金释放只能给 protected、deployable 或确认充分的 recovering 模板，不能用 watchlist、weak_block 或低质量信号硬凑仓位。

### 3. 实现正收益并改善收益质量

目标不是单日或短窗口偶然转正，而是在至少 6 个月正式回测中实现账户权益收益转正，并力争达到 +1% 至 +3% 的稳定正收益，同时提高 profit factor、胜率、平均盈亏比，降低尾部亏损和手续费侵蚀。

### 4. 解决智能体“学得太浅”的问题

学习结果必须从日志和 prompt 附件升级为交易权限、模板状态、PM 仓位释放、auditor 风险分层、trader 执行注意事项和分析师下一日输入。系统要能积累知识，而不是只记住当天复盘摘要。

### 5. 从业务层提高分析师信号质量

三个分析师不能只输出 Bullish、Bearish、Neutral 和泛泛理由。它们必须输出可验证的业务字段，包括趋势阶段、方向锚、供需状态、库存/仓单/资金流、事件类型、影响窗口、无效位、ATR 止损距离、业务质量分、反证和 Neutral 责任说明。

### 6. 保持无未来函数、账务正确和实盘可执行

盘前策略只能基于 T-1 及以前信息；盘中交易员只能使用当时已经发生的分钟线；会计师只能在收盘后使用 T 日结算价；复盘者只能在 Phase3 完成后使用当日及以前结果。回测、模拟盘、未来实盘必须共享同一套交易员、会计师和复盘者逻辑。

## 三、九次代码验收结论

### 验收 1：三个分析师

结论：已通过。

三个启用分析师为 `commodity_news`、`fundamental`、`technical`，宏观经济与政策分析师只保留接口，本轮回测不启用。

已验收内容：

1. `AnalystSignal` 已升级为结构化 schema，支持 horizon、template、entry_type、invalidation_level、atr_stop_distance、direction_anchor、supply_demand_state、business_quality_score、neutral_reason 等字段。
2. 三个分析师均调用 LLM 输出结构化 `AnalystSignal`，但下游 PM、auditor、trader 不再从自由文本中解析交易权限。
3. Neutral 信号允许存在，但必须给出责任化解释，包括缺失证据、冲突因素、什么条件会改变观点。
4. 分析师输入已接入学习摘要、业务质量门槛和数据质量检查。

回测关注点：分析师是否减少无理由 Neutral，是否提高方向信号质量，是否在强模板场景中给出更明确的结构化交易字段。

### 验收 2：审计员 Auditor

结论：已通过。

审计员仍是确定性风险分层器，不调用 LLM。它的职责不是“放大赚钱交易”，而是放过真正应该放过的交易、拦住硬风险和成熟弱模板，让 PM 负责资金释放。

已验收内容：

1. auditor 输出已扩展为五档：`allow`、`scale_down`、`probe_only`、`reduce_only`、`block`。
2. 硬风险仍可 block，包括保证金、账务、强平、严重数据缺失、合约不可交易、成熟 weak_block 等。
3. 软风险主要降仓或试探，不再一刀切拦截。
4. protected/deployable 模板可绕过普通保守规则，不会被冷启动、小样本或轻微信号冲突误杀。
5. `audit_decision_types`、`hard_risk_rules`、`soft_risk_rules`、`memory_policy_rules`、`audit_explainer` 已放在 `src/tools/agent_tools/` 下，与 `src/agents/auditor.py` 解耦。

回测关注点：auditor 的 block 是否明显减少且主要对应硬风险；protected/deployable 的交易是否不再被普通保守规则压死。

### 验收 3：投资组合经理 PM

结论：已通过。

PM 已从简单融合信号升级为质量感知的组合资金分配器。

已验收内容：

1. PM 读取结构化分析师信号、horizon、业务质量、市场确认、strategy memory、adaptive policy、provisional policy。
2. PM 能区分 technical 短线执行信号、fundamental 中期方向锚、commodity_news 事件窗口。
3. 资金利用率目标改为 capacity-aware：基础目标 8%-12%，强机会目标 16%-20%。
4. protected/deployable/recovering/watchlist/weak_block 模板状态会影响仓位权限。
5. `signal_fusion`、`risk_controls`、`capital_allocator`、`position_lifecycle` 等模块已形成可测试拆分。

回测关注点：资金占用是否从约 2% 提升到 8%-12%；强机会日是否接近 16%；未达目标时是否能区分 `system_under_deployed` 与 `alpha_capacity_limited`。

### 验收 4：交易员 Trader

结论：已通过。

交易员已改为确定性盘中择时执行器，回测和模拟盘路径可复刻。

已验收内容：

1. Phase2 才允许使用 T 日开盘和盘中分钟线。
2. 入场使用 15 分钟确认和下一根 1 分钟开盘价成交基准。
3. 模拟盘循环使用 `cutoff_datetime`，只能看到当前时刻以前的分钟线。
4. 未触发条件时，模拟盘保持 wait，回测日终才 finalize 为 skip。
5. 已接入 invalidation level、ATR、time stop、trend break 等退出策略。
6. `intraday_execution`、`entry_timing`、`trader_exit_policy`、`order_sizing`、`execution_simulator` 等确定性模块已拆分。

回测关注点：入场是否减少高位追多和低位追空；MFE/MAE 是否改善；止损是否截断尾部亏损且不误伤 protected 趋势模板。

### 验收 5：复盘者 Reviewer

结论：已通过。

Reviewer 已从日终日志生成器升级为学习闭环核心。

已验收内容：

1. Reviewer 在 Phase4 校验 Phase1/2/3、recommendation、transaction、settlement、portfolio 后才写学习。
2. 已写入或支持 `signal_context_history`、`signal_template_performance`、`analyst_performance`、`analyst_learning_digest`、`strategy_memory_history`、`adaptive_policy_state`、`provisional_policy_state`、`config_learning_overlay`、`capital_deployment_state`、`template_prior.json`、`reviewer_llm_notes`、`causal_review_candidate`、`learning_event_log`。
3. Reviewer 可以调用 LLM 做 post-trade causal review，但 LLM 只生成候选因果解释，正式学习和交易权限仍由规则引擎确认。
4. 学习结果能在下一交易日进入分析师、PM、auditor 和 trader 输入。

回测关注点：学习生效后的交易是否优于未生效交易；LLM 因果候选有多少被采纳；被拒绝原因是否合理；template_prior 是否减少冷启动亏损。

### 验收 6：数据调用层

结论：已通过。

系统可调用并理解当前可用数据源。

已验收内容：

1. 行情数据来自 PandaAI 文档支持的日线、分钟线、主力合约、成交量、持仓量等接口。
2. PandaAI 扩展期货数据已接入，包括基差、仓单、净资金流、多空比、席位持仓、合约排名、净资金变化等。
3. Finoview 本地基本面数据共 422 个 feather 文件，已通过 `finoview_factor_catalog.yaml` 映射到 15 个目标合约。
4. 新闻数据来自 `data/News_data/Future_news/*.txt`，15 个目标合约均有对应新闻文件。
5. Fundamental 分析师已读取 Finoview snapshot 和 PandaAI extra factor context。
6. 新闻分析师按 `pre_open_only` 过滤新闻，盘前不读取 T 日新闻。

回测关注点：Finoview/PandaAI 因子是否真正改善 fundamental 方向锚；新闻是否只在高质量事件和同向确认时推动交易。

### 验收 7：分析决策执行层耦合

结论：已通过。

智能体之间已通过结构化 artifact 协作，而不是靠自由文本互相猜。

已验收内容：

1. 三个分析师输出 `AnalystSignal`。
2. PM 读取结构化字段并生成 recommendation。
3. Auditor 读取 PM 推荐、模板状态、市场确认和学习状态，输出五档审计结论。
4. Trader 只按结构化推荐和确定性盘中规则执行。
5. Accountant 只在 Phase3 读取成交与结算价记账。
6. Reviewer 只在 Phase4 校验并学习。
7. 宏观经济与政策分析师接口保留，但本轮回测不启用。

回测关注点：artifact 链条是否完整；是否存在某个 agent 输出字段但下游未消费；是否存在自由文本直接控制交易动作。

### 验收 8：记忆与学习层

结论：已通过。

系统已经具备可积累的数据库记忆与学习功能，不是只记住当天总结。

已验收内容：

1. 学习结果写入数据库表和 `template_prior.json`，可跨交易日读取。
2. `strategy_memory`、`adaptive_policy_state`、`provisional_policy_state`、`analyst_learning_digest`、`config_learning_overlay` 已进入下游决策。
3. 学习有过期、样本数、置信度、rollback、防过拟合约束。
4. 学习不是训练 LLM 权重，也不是自动切换模型，而是用确定性数据库记忆改变交易权限和分析上下文。

回测关注点：学习曲线是否随时间改善；弱模板是否更早被 cap/probe/block；强模板是否获得更高仓位权限。

### 验收 9：无未来函数与四阶段实盘逻辑

结论：已通过，并补了一处盘前可见性边界。

已验收内容：

1. Phase1 技术日线排除 T 日收盘/结算，只使用 T-1 及以前历史窗口。
2. Phase1 盘前参考价只取前一交易日收盘。
3. Finoview factor snapshot 现在按“数据日期 + 滞后天数 <= 交易日”判断可见，且盘前至少滞后 1 天，避免误读 T 日数据。
4. PandaAI extra factor 使用 T-1 或更早参考日期。
5. 新闻在 `pre_open_only=True` 时排除 T 日及未来新闻。
6. Phase2 交易员可以使用 T 日开盘和已发生分钟线，但模拟盘用 `cutoff_datetime` 防止看未来分钟线。
7. Phase3 会计师收盘后才使用 T 日结算价。
8. Phase4 复盘者在结算完成后才使用当日结果学习。

回测关注点：正式回测必须使用新的 `config_id` 或 `--reset-config`，避免旧数据库中未来日期学习结果污染历史窗口。

## 四、回测前总验收状态

截至 2026-05-22，代码层面已完成以下回测前验收：

1. `dev.yaml` 启用 15 个目标合约。
2. 启用分析师为 `commodity_news`、`fundamental`、`technical`。
3. `planner.yaml` 已同步为 15 个合约和三分析师，避免误跑旧的三品种配置。
4. 本地 Finoview feather 文件共 422 个。
5. 15 个目标合约均有本地新闻 txt。
6. Finoview catalog 对 15 个目标合约覆盖 `all_ready=True`。
7. `compileall` 通过。
8. 完整单元测试通过：`Ran 60 tests ... OK`。

因此，除正式回测才能验证的绩效结果外，当前没有发现必须在回测前继续修改的阻塞项。

## 五、下一轮至少 6 个月回测的验收清单

下一轮正式回测至少覆盖 6 个月。3 个月窗口只能做冒烟回放，不能作为最终绩效验收。若 6 个月结果达标，再用 12 个月窗口做稳健性检验。

### 1. 总体绩效

必须验收：

1. 账户权益收益率是否转正。
2. 是否力争达到 +1% 至 +3% 的稳定正收益。
3. 最大回撤是否下降。
4. Profit factor 是否改善。
5. 胜率与平均盈亏比是否改善。
6. 手续费侵蚀率是否下降。
7. 收益是否来自高质量模板，而不是少数偶然大单。

### 2. Alpha 来源

必须验收：

1. protected/deployable 模板贡献的 PnL。
2. weak_block/watchlist 模板的新开仓是否明显减少。
3. I long、BU 强趋势 long、EB short、J short、SR short 等历史强方向是否延续有效。
4. P long、ZN short、RB short、PB long、M long、C long 等历史弱方向是否被更早限制。
5. Finoview 与 PandaAI 基本面因子是否改善 fundamental direction_anchor。
6. 新闻信号是否只在高质量事件和同向确认时贡献正收益。

### 3. 资金利用率

必须验收：

1. 平均保证金占用是否从约 2% 提升到 8%-12%。
2. 强机会交易日是否接近或进入 16%。
3. 16%-20% 未达成时，原因是否被明确记录。
4. 是否区分 `system_under_deployed` 与 `alpha_capacity_limited`。
5. PM 主动资金补足是否只发生在 protected/deployable/recovering 机会中。
6. 是否仍存在高质量机会被 PM 或 auditor 压死。

### 4. 分析师信号质量

必须验收：

1. 三个分析师结构化字段合法率。
2. Neutral 责任化完整率。
3. 无理由 Neutral 是否下降。
4. 方向信号准确率是否提升。
5. `business_quality_score` 高的交易是否明显优于低分交易。
6. technical 的 trend_stage/template 是否能解释入场质量。
7. fundamental 的 direction_anchor 是否能改善持仓方向。
8. commodity_news 的 event window 是否能解释事件驱动收益。

### 5. Horizon 分层

必须验收：

1. `signal_context_history` 不再把绝大多数画像压成 short。
2. technical short、fundamental medium、commodity_news event_short 是否分别保留。
3. PM 是否把 fundamental 当作方向锚，而不是短线追涨杀跌信号。
4. Reviewer 归因是否按 horizon_scope 评价模板，不再把中期基本面锚放到短线框架里误杀。

### 6. Auditor 风险分层

必须验收：

1. block 主要来自硬风险和成熟弱模板。
2. 普通分歧、小样本、轻微确认不足是否更多进入 scale_down/probe_only。
3. protected 模板是否能绕过普通保守规则。
4. auditor 正确拦截带来的避免亏损金额。
5. auditor 误杀高质量交易的次数和机会成本。

### 7. Trader 择时与出场

必须验收：

1. 15m 确认 + 下一根 1m 开盘成交是否减少追价。
2. 未触发交易是否正确 wait/skip。
3. 入场后 MFE/MAE 是否改善。
4. ATR 止损、无效位、时间止损、趋势破坏是否降低尾部亏损。
5. protected 趋势模板是否没有被过紧止损洗掉。
6. probe/recovering 模板的探索亏损是否被控制。
7. 回测成交逻辑是否能被模拟盘一比一复刻。

### 8. Reviewer 学习闭环

必须验收：

1. 每日 Phase4 是否稳定写入学习结果。
2. `template_prior.json` 是否在新一轮 Day 1 生效。
3. `provisional_policy_state` 是否能在 1-2 笔异常亏损后临时 cap/probe。
4. `adaptive_policy_state` 是否在样本成熟后形成稳定策略。
5. LLM causal review 生成的候选知识有多少被规则引擎采纳。
6. 被采纳学习后的交易表现是否优于未采纳交易。
7. 学习是否遵守样本数、过期、rollback 和防过拟合约束。

### 9. 数据与无未来函数

必须验收：

1. Phase1 所有数据快照是否只含 T-1 及以前可见数据。
2. Finoview factor snapshot 是否记录 data_date、lag_days、freshness_status。
3. 新闻是否不读取 T 日及未来新闻。
4. PandaAI extra factor 是否使用 T-1 或更早参考日。
5. Phase2 是否只使用已发生分钟线。
6. Phase3 是否只在收盘后使用结算价。
7. Phase4 是否只在结算完成后复盘学习。
8. 正式回测是否使用新 `config_id` 或 `--reset-config`，避免数据库历史学习泄漏。

### 10. 账务与阶段一致性

必须验收：

1. Phase1 不写真实交易。
2. Phase2 交易记录与 recommendation 对齐。
3. Phase3 settlement、portfolio、ticker PnL、commission、margin 全部一致。
4. Phase4 校验通过后才写学习。
5. 账户权益收益口径、保证金收益口径、手续费、滑点、保证金占用口径一致。

### 11. 归因与报告

必须验收：

1. 每笔交易能归因到分析师、模板、horizon、ticker-side、PM 动作、auditor 决策、trader 入场/出场。
2. 每个未交易建议能说明是硬风险、软风险、资金容量不足、信号质量不足、还是盘中未触发。
3. `alpha_capacity_limited` 与 `system_under_deployed` 能在报告中分开统计。
4. 评估报告能回答：亏损来自信号错、PM 错、auditor 误拦、trader 入场差、出场慢、还是账务/成本侵蚀。
5. 图像输出保留组合净值曲线和品种价格曲线 + 开平仓点。

## 六、正式回测通过标准

6 个月正式回测通过，至少应同时满足：

1. 账户权益收益转正，且收益来源可归因。
2. 平均保证金占用进入 8%-12% 区间，强机会日能接近 16%。
3. protected/deployable 模板收益为正，且仓位明显高于普通模板。
4. weak_block/watchlist 新开仓显著减少。
5. auditor block 不再是最大资金空转原因。
6. 学习生效交易表现优于未学习交易。
7. trader 入场和出场质量指标改善。
8. 无未来函数、账务、阶段校验全部通过。
9. 归因报告能解释收益、亏损、空仓和资金未部署原因。

若 6 个月未达标，不允许直接调参重跑。必须先用 attribution 报告判断失败原因属于：

1. alpha 容量不足。
2. 分析师信号质量仍不足。
3. PM 仍过度保守。
4. auditor 误杀高质量交易。
5. trader 入场或出场仍有问题。
6. 学习闭环未真正生效。
7. 数据覆盖或 no-look-ahead 存在问题。
8. 市场窗口本身不支持当前 15 品种策略容量。

只有定位到结构性原因后，才允许进入下一轮有边界的优化。

## 七、当前结论

基于本轮九次代码验收，AgentQuant 已经具备再次正式回测的代码条件。现在可以进行至少 6 个月的新一轮回测。

执行正式回测时，必须使用 `src/config/dev.yaml`，并使用新的实验状态或 `--reset-config`，避免旧数据库中未来日期学习结果污染历史窗口。
