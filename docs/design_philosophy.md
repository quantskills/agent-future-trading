# AgentQuant Design Philosophy

更新时间：2026-05-30

本文档是 AgentQuant 后续优化和修改计划的方向护栏。每次修改代码、配置或验收清单前，都应先对齐这里的设计初衷，避免扩大修改范围、违背已有机制，或把短窗口回测结果写成僵硬规则。

当前三份机制文档是本设计哲学的代码侧展开：

1. 多智能体运行机制：`docs/mechanism_mutiagents.md`。
2. 数据与模型调用机制：`docs/mechanism_data_model.md`。
3. 记忆与研究机制：`docs/mechanism_research.md`。

## 1. 系统定位

AgentQuant 本质上是一个多智能体期货交易策略生成系统，自带策略回测与模拟盘功能。它不是单一脚本、单一模型或单一信号源，而是多个智能体按固定工作流协作，生成日频期货交易策略，并直接进入回测或模拟交易。

各智能体既要独立工作，又要按边界配合：

1. 分析师负责在各自信息域内生成结构化证据、方向判断、时间维度、风险边界和 Neutral 责任说明。
2. Portfolio Manager 负责综合信号、学习记忆、市场确认、组合约束和资金预算，生成可执行的日频策略目标。
3. Trade Auditor 负责确定性审计和风险分层，放过真正该放过的机会，拦住硬风险、浅样本乐观和成熟弱模板。
4. Trader 负责按盘前策略和当时可见盘中数据执行，不临时创造新策略。
5. Accountant 负责日终结算、手续费、保证金、持仓、账户权益和 PnL，不被分析文本覆盖。
6. Reviewer 负责 Phase4 确定性复盘验收、账务一致性检查、交易流水入账检查和完整交易日志输出，不能用事后结果污染当日决策。
7. Researcher 负责在 Reviewer 验证后的事实底座上写入交易记忆、未交易机会记忆、Neutral shadow、探索式假设、分析师学习摘要和学习策略状态；它可以调用 LLM 做研究总结，但不能给交易指令、改账或绕过 Auditor。

任何优化都应增强这种分工协作，而不是让某个智能体越权，或让回测路径和模拟盘/实盘路径分叉。

当前各智能体的职责、输入输出和四阶段脚本关系，详见 `docs/mechanism_mutiagents.md`。

## 2. 四阶段运行原则

AgentQuant 必须保持四阶段运行模式：

1. Phase1 盘前生成当日策略，只能使用 T-1 及以前可见信息。
2. Phase2 开盘后执行，只能使用当时已经发生的 T 日盘中数据。
3. Phase3 收盘后结算，必须使用当日官方结算价、真实手续费和保证金规则。
4. Phase4 结算完成后复盘，把结构化学习结果写给未来交易日使用。

回测中能用的信息，必须是在模拟盘或真实交易同一时点也能拿到的信息。否则该策略不可复刻。

策略回测系统应由 `src/run/backtest.py` 统一驱动，不应手工跳阶段。该入口会按交易日顺序解析真实交易日，并依次运行 `proposal.py`、`order.py`、`settlement.py`、`validate_phase_flow.py`，分别对应 Phase1、Phase2、Phase3、Phase4；若某阶段在 `trading_day_phase` 中已经 completed，则允许跳过该阶段继续后续日期。回测 Phase2 使用 `order.py` 的默认模式，也就是 `backtest_replay`，只用于历史回放，不代表模拟盘的盘中轮询。

```powershell
cd D:\research\AgentQuant\src
python run\backtest.py --config config/dev.yaml --local-db --start-date <开始日期> --end-date <结束日期>
```

模拟盘或模拟交易必须按同一四阶段顺序逐日运行，但 Phase2 不能使用回测 replay 语义。正确流程是先用 `proposal.py` 完成盘前策略，再用 `order.py --loop` 进入盘中执行循环；Trader 会反复读取仍为 pending 的当日策略建议，按当前时点可见的盘中数据检查触发条件，达到 `execution.intraday_confirmation.finalize_after` 后才收尾未触发建议。Phase2 完成后，再运行 `settlement.py` 做日终结算，最后运行 `validate_phase_flow.py` 做 Phase4 复盘、验收诊断、完整交易日志生成，并在 Reviewer 验证通过后调用 Researcher 写入未来可用学习。

```powershell
cd D:\research\AgentQuant\src
python run\proposal.py --config config/dev.yaml --local-db --trading-date <交易日>
python run\order.py --config config/dev.yaml --local-db --trading-date <交易日> --loop
python run\settlement.py --config config/dev.yaml --local-db --trading-date <交易日>
python run\validate_phase_flow.py --config config/dev.yaml --local-db --trading-date <交易日>
```

四阶段之间存在硬前置关系：Phase2 必须等待 Phase1 completed，Phase3 必须等待 Phase2 completed，Phase4 必须等待 Phase3 完成并成功结算。任何绕过这些前置关系的脚本调用，都只能用于排查问题，不能视为有效回测或有效模拟交易记录。

## 3. LLM 调用边界

LLM 在系统中的角色是理解、归纳、解释和生成结构化候选判断。硬风控、成交、结算、保证金、手续费、数据库事实和最终审计必须由确定性代码约束。

当前主链路允许直接调用 LLM 的角色和目的：

1. Planner：只在 `planner_mode=true` 时作为旧版分析师选择器启用，不直接生成交易或资金指令。
2. Technical Analyst：把价格、成交量、波动率、趋势、支撑阻力、ATR 等整理为结构化技术面信号。
3. Fundamental Analyst：把供需、库存、仓单、基差、利润、资金流、产业链位置、数据新鲜度整理为结构化基本面信号。
4. Commodity News Analyst：把商品新闻、事件类型、影响窗口、方向锚、可信度和是否已被价格消化整理为结构化事件信号。
5. Portfolio Manager：可用 LLM 形成结构化组合建议和解释，但输出必须再经过资金硬门、Auditor、Trader 和 Accountant 校验。
6. Researcher：可用 LLM 做 post-trade causal research、探索式假设和下一轮可用记忆总结，但正式学习状态和交易权限必须由规则引擎、样本验证和结构化状态确认。Reviewer 不直接调用 LLM。

Macroeconomic/Policy 等旧入口只可作为保留或未来扩展，不属于当前默认主链路；启用时也只能提供背景约束，不能替代品种级证据。

不应依赖 LLM 做最终裁决的角色：

1. Trade Auditor：不调用 LLM；它可以读取 LLM 产生的结构化信号，但 allow、scale_down、probe_only、reduce_only、block 必须可由规则和证据解释。
2. Trader：不调用 LLM 创造新策略，不用 LLM 改写成交价、成交手数或入场时点。
3. Accountant：不用 LLM 解释覆盖账务结果，结算必须来自成交记录和官方行情结算数据。

当前数据源、模型路由、缓存、数据质量摘要和模型调用审计，详见 `docs/mechanism_data_model.md`。

## 4. 资金设计初衷

资金利用率的目标不是“为了满仓而满仓”，而是把经过交易记录和市场证据反复验证的 alpha 机会适当放大。

核心原则：

1. 组合可交易资金/保证金占用最高为账户权益的 20%，这是硬门槛。
2. 不硬性规定单品种固定资金占比。单品种能用多少资金，由机会质量、学习证据、市场确认、止损保护、剩余组合容量和当日风险状态共同决定。
3. 未经过验证的机会只能小额试探；多日交易或模拟盘证明有效的机会，才可逐步获得更高预算。
4. 即使特别看好一个品种，也不能 all-in；系统必须保留资金给其他机会、风险缓冲和组合弹性。
5. 单品种止损、失效价、ATR stop、回撤控制、换约和日终结算必须真实生效。提高资金利用率必须和退出机制一起设计。

`max_single_position_ratio` 只能理解为冷启动或弱信号下的 sizing anchor，不是僵硬的单品种硬上限。真正硬上限是组合层面的 20% 总保证金门槛。

## 5. 坚持自由探索式记忆与研究原则

AgentQuant 真正应引入的记忆与研究机制，是自由探索式机制：让智能体记住完整历史交易经历和当时判断原因，围绕品种、方向、周期、分析师、市场状态、数据依据、入退出场时机和失效条件主动探索期货交易规律，而不是把历史盈亏压缩成越来越多的僵硬规则。

记忆负责保存事实，研究负责提出可验证假设，并把归因收束成下一轮可用记忆。完整交易 episode、未交易机会、Neutral 责任、影子结果、账务结果、数据依据、分析师表现、策略模板表现和 Researcher 探索假设，都可以进入后续分析和决策；但它们只能作为可反驳先验，不能替代当日证据、不能绕过 Auditor、不能直接决定成交手数，也不能突破组合 20% 总保证金硬门。

自由探索必须保留差异。系统不能把所有品种、板块、周期、分析师和 market regime 混成平均经验，也不能因为短窗口盈亏永久封杀或放大某类期货。研究结论应通过未来样本、shadow tracking、template performance、learned vs unlearned 对比和真实账务结果逐步提高或降低信任。

学习进入仓位时必须有边界。亏损持仓继续持有需要当日可见证据复核；盘中执行只能在成熟且样本充分的记忆支持下有限放宽触发方式。候选假设、小样本记忆和泛化经验不能直接放仓。

当前已代码落地的具体记忆类型、Researcher 写入机制、`next_round_memory_contract`、各智能体如何使用记忆、以及后续校准项，详见 `docs/mechanism_research.md`。

## 6. 日频分析不等于日频交易

每天盘前分析的意义是重新校验证据，不是机械制造交易。策略结果可以是继续持有、观察、加仓、减仓、止损或平仓。

趋势已经被验证且同向证据仍成立时，系统应允许跨日持有和受控加仓。只有当失效位、止损、反向证据、持仓生命周期或组合风险真正触发时，才应退出或降低仓位。

系统不应硬性压制或硬性释放某个品种。品种、方向和模板只能通过真实信号、市场确认、历史学习、当前持仓和风险预算动态形成策略，不能为了修复某个短窗口表现写死黑名单、白名单或方向特例。

## 7. 重仓必须谨慎论证

系统允许对“看得准”的机会重仓，但重仓必须满足更高门槛：

1. 有足够历史样本，而不是一两笔偶然收益。
2. 胜率、净收益、回撤和盈亏比支持该模板或方向。
3. 当前 market confirmation 支持，而不是只依赖旧记忆。
4. 不存在同 ticker-side 或 signal combo 的 weak/watchlist 冲突记忆。
5. 有明确止损、失效价或退出机制。
6. 组合 20% 硬门、净敞口、可用保证金和账户回撤允许。
7. protected/deployable/adaptive protect 结论来自足够样本，不是浅样本乐观。

强机会扩仓必须有建仓前已经形成的风险失效边界。`invalidation_level` 应来自 Phase1 盘前可见的历史价格结构，例如前低/前高、支撑阻力、突破位回踩失守、关键均线或趋势结构失效位；`atr_stop_distance` 应来自 T-1 及以前价格波动率计算出的 ATR 止损距离。

Researcher 可以在复盘学习中总结“未来类似 ticker-side-signal_combo 必须带止损/失效边界才允许加仓”的规则，但不能给已经发生的交易事后补一个止损价来证明当时可重仓。复盘学到的是未来规则；下一次交易日前，分析师必须基于当时可见数据重新生成当日可执行的失效边界。生成不出来，即使历史学习结果是 protected/deployable，也不能进入强机会资金带。

## 8. 回撤保护与恢复交易

回撤控制必须保护本金，但不能把系统变成永久停摆。

账户权益回撤采用三层状态：

1. 正常状态：回撤 <4%。
2. 警戒状态：回撤 4%-5%，降低新开仓和加仓预算，但允许平仓、减仓、换约、止损和被充分确认的小仓机会。
3. 硬保护状态：回撤 >=5%，进入冷静期。

第一次进入硬保护后，系统先完整冷静 1 个交易日，不开新的增量风险，但各智能体仍正常分析、复盘、更新记忆并记录 shadow recommendation。冷静期后，只有满足更高门槛的机会才能做 1%-2% 总保证金预算内的恢复试探。

如果恢复试探继续亏损，系统进入更长冷静期：第一次试探亏损后暂停新开仓 2 个交易日；第二次试探亏损后暂停 3 个交易日并进入只观察模式；之后每再亏一次增加 1 个交易日冷静期。冷静期不是摆烂，所有智能体仍按工作流运行，并记录“如果交易会怎样”的 shadow recommendation 用于学习。

恢复正常必须有条件：恢复试探开始连续盈利，真实交易和 shadow recommendation 不再恶化，没有新的止损或连续亏损，Researcher/Auditor 未提示过度乐观，并且账户回撤重新低于 4%。恢复顺序应从 1%-2% 小仓试探，逐步走向 2%-4%、4%-6%、6%-8%；16%-20% 强机会总保证金上限只能在正常状态下、经过充分学习和止损边界验证后使用。

## 9. PandaAI 与 Finoview 数据使用原则

AgentQuant 当前只保留两类数据源：PandaAI 提供期货行情、分钟线、结算相关行情和期货衍生数据；Finoview 提供本地 feather 基本面数据和本地 txt 新闻数据。旧 DataYes/AlphaVantage 路径不应再进入主链路。

PandaAI 与 Finoview 数据都是证据，不是每条证据每天都必须到场。只要关键证据链足够，系统可以继续分析和交易；如果缺口太多，就降级为小仓、观察或 Neutral。

原则：

1. fallback covered 的缺口可以继续使用降级后的证据。
2. 少量可选因子缺失不应中断回测，也不应机械阻断全部交易。
3. 关键缺口过多、接口错误或无可用特征时，PM 必须降低新风险，不能把缺失当成方向证据。
4. PandaAI、Finoview feather 和新闻 txt 缓存只能缓存当前时点允许看到的数据，不能扩大信息窗口。
5. Phase3 当天结算必须使用当日官方结算价；取不到应修复数据链路并重跑该日，不能用成交价、上一结算价或估算价替代。
6. 每日数据质量摘要应记录哪些数据可用、滞后、缺失、进入了信号，并进入后续记忆，避免学习只记交易结论不记数据依据。

当前数据调用、模型调用、缓存和数据质量摘要机制，详见 `docs/mechanism_data_model.md`。

## 10. Neutral 追责原则

Neutral 是合法信号，但不能成为分析师逃避责任的默认出口。

Neutral 必须说明：

1. 为什么中性。
2. 缺失了哪些证据。
3. 有哪些冲突因素。
4. 什么条件会改变观点。

Researcher 要把 Neutral 分类为合理观望、证据缺口保守、潜在错过机会或无责任 Neutral。追责结果不能只写报告，必须进入结构化学习状态，让后续分析师和 PM 能学到：哪些场景应该继续观望，哪些场景应该补证据，哪些场景未来可以小仓 probe。

## 11. 数据库与 Artifact 边界

`src/assets/agentquant.db` 是唯一运行主库。所有回测、模拟盘、智能体学习、复盘写回和评估逻辑，都只能把这个主库视为运行事实来源。

主库保存结构化、可索引、会被系统继续使用的内容：

1. config、portfolio、phase 状态。
2. futures_transactions、daily_settlement、ticker_daily_pnl 等账本和 PnL。
3. futures_recommendation 的关键字段、状态、动作、价格、warning、摘要和 artifact 指针。
4. strategy_memory、adaptive_policy_state、capital_deployment_state、config_learning_overlay、signal_template_performance、analyst_performance 等学习结论。
5. neutral accountability、causal validation、learning_event_log 等结构化复盘结果。

不适合长期全量塞进主库的内容，应外置到 `src/logs/artifacts/` 或对应日志目录：完整日志、LLM 原始长 prompt/response、大段 analyst report、完整 evidence pack、PandaAI 原始 payload、分钟线或大行情缓存、每日 summary 的完整 JSON、图片、报表和临时 debug artifact。主库只保存路径、checksum、大小、摘要、交易日、config_id、artifact 类型和关键诊断字段。

`src/assets/agentquantcheck.db` 只能是可选只读查看副本，用于人工查看资金流、日盈亏、品种 PnL、推荐摘要和学习状态。它不参与任何智能体运行和学习，也不能成为回测或模拟盘事实来源。

## 12. 修改系统时的边界

后续每次优化都应先问：

1. 这个修改是否服务于 alpha、资金利用率、信号质量、学习深度、无未来函数或实盘可复刻？
2. 它是否只是针对短窗口结果过拟合？
3. 它是否会让回测和模拟盘/实盘逻辑分叉？
4. 它是否绕过 20% 组合硬门、官方结算价、确定性风控或审计日志？
5. 它是否让学习结果真正影响下一轮决策，而不是只增加解释文本？
6. 它是否保留品种、板块、分析师、horizon 和 market regime 的差异？
7. 它是否符合 `mechanism_mutiagents.md`、`mechanism_data_model.md` 和 `mechanism_research.md` 记录的现有机制边界？

如果答案不清楚，应先补诊断和验收项，而不是扩大修改范围。

