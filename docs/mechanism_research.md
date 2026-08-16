# AgentQuant 记忆与研究机制

研究链路的生产端、DB 落点、下游消费、PG 审计、pre-backtest fixture 与 diagnostics 边界统一锚定 `docs/matrix_chain_contract.md`；本文只说明研究与复盘机制细节。

更新时间：2026-08-07

本文档定义 AgentQuant 的复盘、研究、记忆持久化和未来学习消费机制。它必须与 `docs/mechanism_multiagents.md` 的固定工作流一致，并以 `docs/matrix_field_semantics.md` 作为唯一字段语义矩阵。若本文与多智能体运行机制冲突，以固定工作流、智能体边界和字段语义矩阵为准。

研究机制只服务未来交易日的结构化学习，不产生当天交易动作，不改写当天合约、成交、结算或收益。

## 当前代码契约

本节只记录生产代码中的对象定义、生产者、消费者和权限边界，不保存会随清库、实验配置或回测窗口变化的数据库行数。具体回测事实统一写入 `docs/backtest_outcome.md`，仍待自然真实回测验收的项目统一写入 `docs/check_list.md`。

### 1. 谁产生事实，谁产生未来记忆

| 环节 | 实际职责 | 是否生成未来研究记忆 |
|---|---|---|
| 三类分析师 | 生成当日预测证据；技术分析师在结构化输出中给出当日 `setup_type` | 否；只生成当日证据和 setup 身份 |
| 信号收集员 | 保真汇总 AEC 为 SCC | 否 |
| 投资组合经理 | 根据当日 SCC、持仓和历史记忆签发唯一 FAC，并记录实际消费过的学习 ID | 否；不写研究表 |
| 交易员 | 生成成交、未触发及 `execution_learning_trace` 等执行事实 | 否；这些是 Researcher 的未来输入 |
| 会计师 | 生成成交入账和结算事实 | 否 |
| 复盘员 | Phase4 确定性验证交易、执行、成交和结算事实 | 否；代码明确 `reviewer_writes_action_value=False` |
| 研究员 | 仅在 Phase4 completed 后读取已验证事实，统一生成、刷新和落库未来学习 | 是；绝大多数正式记录由确定性 writer/聚合函数生成，LLM 只参与因果候选和探索假设，不直接生成交易权限 |

因此，“研究员生成记忆”不等于“LLM 自由写策略”。正式 sample、profile、action-value、policy、digest、episode 和反馈均由 Researcher 调度的确定性代码校验并落库。

### 2. setup、sample、profile、action-value、policy 的关系

常见生产链是：

```text
当日技术证据给出 setup_type
-> PM 把最终交易身份冻结进 final_action_contract
-> Researcher 将已验证事实写成 alpha_setup_sample
-> 同作用域样本聚合为 alpha_setup_profile
-> 再按动作 lane 聚合为 alpha_setup_action_value
-> 达到相应条件后，部分 profile/绩效事实转成 adaptive/provisional policy
```

这是一条常见链，不表示每条 policy 都必须逐级经过以上所有对象；止损哨兵、参数校准等 policy 还可以从已验证的亏损、分析师绩效或上下文事实生成。

| 概念 | 当前代码中的准确含义 | 主要生产者 | 正式消费者 | 固定边界 |
|---|---|---|---|---|
| `setup_type` | 一笔机会采用的交易形态身份，不是收益、动作或记忆表。技术分析师正式枚举为 `trend_breakout_setup`、`trend_pullback_setup`、`range_reversal_setup`、`volatility_breakout_setup`、`failed_rebound_setup`、`unknown`；PM 将选中的 canonical setup 冻结进 FAC | 技术分析师产生当日值；PM 冻结最终值；Researcher 继承 | SCC、PM 识别当日机会；后续所有正式记忆用它隔离作用域 | 与 `opportunity_type`、`execution_profile` 独立；AEC finalization 只规范化既有 setup，不得用机会类型或执行画像覆盖 |
| `alpha_setup_sample` | 一条最小学习观察，记录某个 setup 在某日发生的交易、未交易、持仓动作或执行结果 | Researcher 的 `write_alpha_setup_profiles`/`upsert_alpha_setup_sample_and_profile` | Researcher 聚合；其他智能体不直接消费原始 sample | 原始 sample 不是交易授权 |
| `alpha_setup_profile` | 完整真实 episode 按同一 `ticker/side/setup/horizon/regime` 聚合成熟成绩单；未交易、日级审计和执行学习仍保留各自 `data_combo` 细节，包括样本数、胜率、盈亏因子、净盈亏、置信度和生命周期状态 | Researcher 确定性聚合 sample | 分析师读取安全摘要；PM 经 `decision_memory_retrieval` 读取同 setup profile | `data_combo` 保留证据与执行审计，但不得再次切碎完整 episode 的 Alpha 成熟样本；candidate/watchlist 不具备成熟放大权 |
| `alpha_setup_action_value` | 同一学习身份下，对某个动作 lane 的历史结果总结；回答“历史上 open/hold/reduce/exit/execution 等动作表现如何”，不是明日指令 | Researcher 在 profile 刷新时按动作分账聚合 | PM 经 `decision_memory_retrieval` 用于评分、排名、生命周期和执行偏好；分析师只读显式授权的安全校准投影 | 只有 canonical family/lane/preference 和 consumer_scope 完整的记录具备正式消费资格；置信度直接采用已包含样本量的对应动作生命周期置信度，不再二次乘样本比例 |
| `adaptive_policy_state` | 从合格历史事实导出的有时效、置信度和样本门槛的未来软规则，如参数校准、cap、probe；不是订单 | Researcher 的多个 policy writer | PM 经 `decision_memory_retrieval` 消费交易决策类 policy；技术分析师只消费 `contextual_rule_calibration:technical_parameters` | `fast_candidate_alpha` 仅来自合格 `missed_alpha_accountability`，只授予下一交易日同作用域 probe 权限 |
| `provisional_policy_state` | 低成熟度、可回滚的临时政策，仅允许进入 PM risk gate 的低权限校准 | Researcher | PM risk gate 经 `decision_memory_retrieval` 消费 | 不得直接授权 real/scale、方向或手数 |

profile 与 action-value 的区别：profile 是“这个 setup 整体成绩如何”；action-value 是“在这个 setup 下，某个具体动作历史上是否有效”。policy 与二者的区别：policy 是通过额外条件生成的未来软控制规则，只有当日证据再次验证后才能生效。

### 3. 其他正式研究、辅助记录和诊断记录

| 对象 | 当前用途 | 正式消费者 | 固定边界 |
|---|---|---|---|
| `trade_episode_memory` | 仓位从 0 开始并最终回到 0 后形成的一条完整学习周期 | Researcher 生成 sample/profile/action-value；分析师读取相对化摘要；PM 不直接读 episode 表 | 只收完整策略周期；rollover/forced-risk 不得污染 |
| `no_trade_opportunity_memory` | 记录未交易原因和后续固定窗口反事实结果 | Researcher 汇总，并间接转成 sample/profile/policy；分析师读取安全摘要 | 反事实不得冒充真实成交收益 |
| `analyst_learning_digest` | 面向 technical/fundamental/commodity_news/PM 作用域的压缩学习摘要 | 三类分析师通过 `build_learning_context` 消费各自作用域；`portfolio_manager` 作用域行不属于 PM 的 `decision_memory_retrieval` 正式输入 | 只在完整 episode 样本数或最新结束日变化时刷新，并按最新结束日计算有效期 |
| `analyst_forecast_evaluation` | AEC 中 1/3/5/10 日预测到期后的方向命中、Brier、标的实际收益和预测方向手续费后收益 | Researcher 聚合；分析师与 PM 只读到期后的摘要 | 预测期限未到不得写入；评价不能改写原预测 |
| `analyst_performance` | 分析师在品种、板块、全局及固定预测期限作用域下的表现统计 | 分析师动态权重校准；PM 校准预测 Rank；Researcher 生成上下文参数政策 | 完整 episode 归因与到期预测评价分别写入明确 payload，任何来源都不得使用未来样本 |
| `setup_type_performance` | 已完成 setup 的精确、去状态、跨品种和全局层级聚合表现 | 分析师动态权重校准；Researcher 生成成熟政策 | 层级回退只解决小样本碎片化；未交易反事实不得生成成熟 `alpha_promotion` |
| `strategy_memory` | 按品种、方向和 signal combo 汇总的较宽策略先验 | PM 经 `decision_memory_retrieval` 用于风险门和资金控制 | 不是精确 action-value，不得单独授权 real/scale |
| `research_position_feedback` | 记录 PM 是否实际声明消费学习，以及后续动作、成交和结算 | Researcher 闭环和开发验收，不直接控制交易 | 只有最终 FAC 实际消费的正式学习才可形成反馈 |
| `signal_context_history` | 每日 SCC/FAC 等上下文事实快照 | Researcher 后续归因和聚合 | 只记录已落地正式事实 |
| `capital_deployment_state` | 每日资金利用和部署诊断 | Researcher、Reviewer 报告和开发评估；不是 PM 次日直接记忆 | 不生成次日资金权限 |
| `exploratory_hypothesis` | Researcher LLM 基于完整持仓日轨迹和明确支持 episode ID 生成的结构化探索假设 | `candidate/monitoring` 只做影子验证；只有 `validated` 可进入分析师提示词作为可反驳先验，任何状态都不能直接授权交易 | 同一 `ticker或sector/side/setup/horizon/标准化market_regime` 只保留一个活动假设并合并支持 episode；正式验证只使用生成日之后的完整真实 episode |
| `causal_review_candidate` | Researcher LLM 形成的因果候选及确定性验证状态 | Researcher 和开发评估；未验证前无正式交易消费权 | 不直接进入交易链 |
| `researcher_llm_notes` | 保存经正式ID链验证的结构化 evidence pack、验证结果及外置 payload 元数据 | Research writer；仅结构化、验证后的候选进入后续研究链 | prompt、原始response、内部推理和未验证工具结果固定不保存 |
| `config_learning_overlay` | 研究参数覆盖层；PM 代码通过 `apply_config_learning_overlay` 读取 | PM 配置层 | 无合格参数生产者时不得复制原配置冒充学习覆盖 |
| `learning_context_budget` | 记录每次分析师提示词选了多少学习条目、字符及丢弃量 | 审计和开发验收，不是知识记忆 | 不产生交易权限 |
| `learning_event_log` | 研究事件总账，登记来源、日期、状态和 verifier | 审计、Researcher 和开发验收，不直接参与交易评分 | 有事件记录不等于已被分析师或PM消费 |

### 4. 消费边界的当前代码事实

- 三类分析师会消费 `analyst_learning_digest`、相对化 episode、未交易摘要、仅限 `validated` 的探索假设、profile 摘要、显式授权的 action-value 安全投影和已成熟的多期限 forecast calibration；`candidate/monitoring` 探索假设不进入提示词，技术分析师还会消费技术参数校准 policy。上述学习都只是可反驳先验，同时进入 LLM 前提示词和 LLM 后确定性信号校对，不创建交易权限。
- PM 的统一正式入口 `retrieve_pm_memory` 当前可返回 action-value、profile、strategy memory、adaptive policy 和 provisional policy。action-value 先按 exact，再按同品种同方向同期限同 setup 的跨市场状态记录读取；setup 不匹配的既有 fallback 仍按 partial scope 使用，不能冒充 exact 或单独支持 real/scale。
- Trader、Auditor、Accountant、Reviewer 和 Signal Collector 不直接读取研究表。Trader 只读取 PM 已写入 FAC 的执行字段；Auditor/Accountant 只审计 FAC、账户、风险和成交结算事实。
- `learning_event_log`、`learning_context_budget`、`researcher_llm_notes`、`capital_deployment_state` 和多数因果/排名事件属于总账、诊断或研究输入，不能仅因“有记录”就声称已经影响分析、排名、资金或交易。

### 5. PM 新机会与持仓 policy 的正式路由

底层 `retrieve_pm_memory` 和数据库按 `ticker/side/setup/horizon/regime/trading_date` 过滤。PM 调用方必须把两种政策用途分开：

1. 当天目标方向 policy 用于评估新开、反向和新增风险，这是合法用途；
2. 原持仓 policy 应按原开仓 FAC 身份服务 hold/reduce/exit 生命周期；
3. 反向日先按原开仓 FAC 管理并结束旧周期；仓位归零后的新机会才按当天 SCC/FAC 建立新政策作用域。

代码分别维护新机会 policy 与原持仓 policy：前者使用当天 SCC/FAC 身份并只服务新增风险；后者使用原开仓 FAC 的 side/setup/horizon/regime 并只服务原持仓周期。当天 SCC 继续提供最新确认和退出证据，但不得改写持仓学习身份。

## 一、研究机制原则

研究机制服务主动 alpha 迭代，而不是堆叠被动限制。研究员要从真实交易、未交易机会、条件机会未触发、neutral 观察、执行失败、换月/强平运营事件和结算结果中总结：

- 哪些 setup 值得开仓；
- 哪些持仓应继续保护；
- 哪些退出或减仓保护了利润；
- 哪些执行触发和成交方式更好；
- 哪些证据只是噪音；
- 投资组合经理的评分、排序、资金部署和手数计算是否把资金放到了更强机会。

结构化字段不是 LLM 推理上限；结构化字段是 LLM 结果的落地格式。研究员可以用 LLM 做解释、冲突分析、反事实推理和不确定性归因，但输出必须落到已登记的结构化研究字段，并按消费对象分作用域写出：分析师只消费本专业校准类研究；投资组合经理只通过 `decision_memory_retrieval` 消费交易决策类研究；信号收集员、审计员、交易员、会计师和复盘员不直接消费研究库。自由文本只能解释研究原因，不能成为任何智能体的交易权限、仓位依据或直接消费的研究结论。

学习必须守住时间边界。Phase1、Phase2、Phase3 的事实完成后，复盘员先做确定性验收；只有 Phase4 验证通过，研究员才能通过 `src/run/research/researcher_learning.py` 输出并持久化未来可用学习。任何研究学习都不能反向修改当天推荐、合约、成交、保证金、手续费、结算价或 PnL。

固定学习链路是：

```text
Phase1 投资组合经理 final_action_contract
-> Phase2 交易员 execution_result / execution_learning_trace
-> Phase3 会计师 daily_settlement
-> Phase4 复盘员 validation / transaction log / factual attribution
-> 研究员 structured learning
-> 下一交易日分析师校准或投资组合经理 decision_memory_retrieval
```

研究结果进入交易链路只有两条合法路径：

1. 分析师消费本专业校准类结构化研究，输出更干净的 `action_evidence_contract`。
2. 投资组合经理经 `decision_memory_retrieval` 消费交易决策类结构化研究，再按 PM 六步机制通过生命周期路由、仅新增风险 Step5 全市场资金部署和唯一 `final_action_contract` 落地。

多期限预测评价属于第二条路径中的只读校准输入：Researcher 以 AEC 的逻辑交易日为预测起点，在 1、3、5、10 个结算交易日分别成熟预测，按执行手续费事实表计算预测方向手续费后收益，并按品种、板块、市场状态和全局层级汇总方向准确率、Brier、预测偏差及往返手续费。PM 先按候选 `expected_horizon_days` 映射到同一预测网格，再读取三名分析师当日该期限的完整概率分布和当日预期收益；历史摘要校准对应分析师、期限和信号侧的概率与收益偏差，形成多空两侧当日手续费后净预期收益。两侧都已由SCC形成合法候选且校准成熟时，PM才在两侧中优先选择净预期收益较高的一侧；只有一个合法候选时原方向不变，冲突SCC保持flat，冷启动仍沿原SCC方向。中性分析师继续按上涨、下跌、震荡概率参与；结果进入既有 `calibrated_forecast_value`，不新增一致性门槛、不创建方向、不删除合法候选。

信号收集员、审计员、交易员、会计师、复盘员都不能直接读取研究库来生成或改变交易权限。

`template_prior` 是冷启动研究种子，只能通过 `src/run/research/load_template_prior.py` 显式加载。它不属于 Phase1 盘前策略生成，不由 `proposal.py` 自动写入研究记忆，也不能使用当天或未来交易结果。加载后的结构化学习记录不得保存源文件绝对路径，加载日志不得输出路径、原始 payload 或原始异常。

`product_price_behavior_profiles.yaml` 是三类分析师的商品差异化冷启动分析框架，不是研究库，不随回测自动改写。研究结论用于更新分析师差异化的方式只有一条：Researcher 写结构化分析师校准类研究；下一交易日同一批合格的 `learning_context` 和 `analyst_learning_calibration` 先进入 `technical`、`fundamental`、`commodity_news` 的提示词，再在 LLM 返回后确定性校对信号。静态 profile 继续提供品种基础框架，动态学习作为可反驳校准叠加其上。Auditor、Trader、Accountant 不读取 profile，也不读取分析师校准来改变交易权限、触发或入账。

多维证据融合协议由 `tools/common/evidence_fusion_semantics.py` 的确定性函数固定实现，不设置无人读取的 YAML 参数。Reviewer 可以只读标注 `fusion_attribution_label`，Researcher 可以写入 `evidence_fusion_attribution` 研究记录；但该记录目前没有正式下游消费端，不能写成已经进入分析师或 PM 的学习闭环。本次也不新增该消费链。研究记录不能回写当天 `final_action_contract`、`execution_result`、`daily_settlement` 或审计结果。

## 二、Phase4 与研究学习分工

复盘员是确定性复盘者，不调用 LLM、不下单、不改账、不写最终 action-value。它检查 Phase1-3 是否完成，推荐、审计、执行、成交、结算等已落地事实是否完整一致，完整交易日志是否输出，并对交易结果做事实归因。复盘员可以输出事实归因、交易日志和研究输入材料，但未来学习由研究员输出并持久化。`max_net_exposure`、`target_margin_ratio_*`、`probe_margin_ratio`、`strong_opportunity_*`、`recovery_*` 等 PM 计划预算参数在真实成交后出现偏离时，只能进入复盘事实归因、warning 和研究输入材料；复盘员不能把这类计划预算偏离当作日终 hard fail 或策略违规裁决。阶段断链、应落地事实缺失、成交与执行不一致、成交未入账、结算或账户公式不一致以及来源链断裂可以使 Phase4 事实复盘失败；账户硬风险合法性已经由 Auditor 和运营风控链负责，复盘员不得二次裁决。

Phase4 标记 completed 只表示复盘验收通过；它不能触发 `strategy_memory` 刷新、学习 retention 清理、研究表写入或任何未来学习状态更新。

研究员可以按配置调用 LLM，但只能在Phase4 completed且结算事实形成后运行。运行前必须以真实 `signal_record_id`、recommendation ID、transaction recommendation ID、交易日期和config ID验证 AEC → SCC → FAC → Auditor → `execution_result` → transaction → settlement；零成交是合法链路。研究员只保存验证后的结构化 evidence pack 和研究结果，不保存prompt、原始response、内部推理、隐藏上下文或未验证工具结果。研究信息包括分析师校准类研究、交易决策类 action-value、alpha setup profile、adaptive policy state、执行学习、排序偏好和研究反馈；这些信息供其他智能体按各自权限通过正式检索接口使用。研究员不能下交易指令，不能改账，不能绕过投资组合经理、审计员或交易员。

PM 持仓生命周期进入 Researcher 校准的唯一正式接口是 `final_action_contract.learning_used.pm_lifecycle_learning_impact_delta`。Researcher 可读取其中已登记的 `trace_version`、`hold_decision`、`reduce_exit_decision`、`current_lots`、`target_lots`、`lots_delta` 等字段，但这些字段也可如实记录当日独立生命周期规则的结果；是否存在历史学习因果影响，只能由同一 FAC 最终 `decision_learning_rows` 与 `alpha_setup_action_values` 的精确正式 ID 共同证明。不得恢复 `final_action_contract.action_candidates`、旧 `holding_rebalance_control` 对象或用 `lifecycle_classification` 代替正式决策字段。

Researcher 的数据库写入、`researcher_learning_completed`、外置 payload artifact、template prior 和历史学习快照按一次运行原子提交。任何写入或提交失败都必须回滚数据库、删除本次新 artifact、恢复运行前已有 artifact，且不得留下完成事件；该原子性不改变学习算法和学习结果允许为空的边界。

未完成交易日必须硬拦。若某天推荐、成交、盘中决策或学习记录已存在，但 phase1-4 没有全部 completed，系统应报 `incomplete_trading_day_phase`；该日不能进入收益判断，也不能被研究员当成学习样本。

学习成果允许为空。不是每笔交易都具备形成学习成果的代表性，也不是每次分析或PM决策都必须命中学习记录；空检索是合法冷启动，不能触发默认学习、伪样本或替代策略。交易成交不等于形成 episode，episode 落地不等于形成 canonical action-value，action-value 存在不等于本次 PM 实际消费，PM 消费学习也不等于当天必须成交；反过来，没有历史学习也不阻止当日证据合格的候选进入既有 PM、审计和 Trader 链路。

## 三、研究对象与消费边界

| 研究对象 | 记录什么 | 合法消费者 | 边界 |
|---|---|---|---|
| `trade_episode_memory` | 仓位完全归零后的策略 episode、完整持仓周期 AEC/SCC/FAC、成交、结算、证据/失效变化及各物理 pair PnL | 研究员生成样本、profile 和候选 action-value；分析师只经 `analyst_learning_context` 消费 T+1 可见的相对生命周期摘要；PM 不读取 episode 表 | `setup_type/horizon_class/expected_horizon_days/market_regime`只从原开仓 FAC 复制，任一缺失则不写正式 episode；分批未归零、裸 transaction、rollover、forced_risk 不得冒充完整策略 episode 学习；历史绝对价格不得进入分析师提示词 |
| `no_trade_opportunity_memory` | 未交易机会、no-trade 原因、多周期影子结果、错过机会；方向、setup、入场触发、周期和市场状态直接继承对应 `final_action_contract` | 研究员汇总；分析师读取校准摘要；投资组合经理经 `decision_memory_retrieval` 间接消费 | 不能直接授权开仓，只能作为先验、反证或排序诊断；不得从分析师顺序、信号组合或推荐动作重建学习身份，FAC 身份不完整时不写该记录；多周期结果只作诊断；只有 `missed_alpha_accountability` 可按固定5日窗口、同作用域完整正负样本、未发生入场前失效且FAC具备canonical触发/profile/来源/失效边界时生成`fast_candidate_alpha`，该政策只允许下一交易日同作用域probe/小仓复核；Profile candidate/watchlist不得生成同名政策，未交易反事实不得生成成熟`alpha_promotion`，也不得把影子记录改造成 Trader 成交 |
| `alpha_setup_sample` | 单个 setup 的交易、未交易、执行样本 | 研究员汇总 | 必须有交易日、方向、setup、horizon、regime、数据质量 |
| `alpha_setup_profile` | setup 生命周期、胜率、盈亏因子、净 PnL、最大亏损 | 分析师读取校准类摘要；投资组合经理经 `decision_memory_retrieval` 消费交易决策类摘要 | 只作为同作用域证据，不是品种黑名单 |
| `alpha_setup_action_value` | 按 `canonical_action_family` 与 open/add/hold/reduce/exit/execution/conditional_monitor lane 分账的动作学习结果，并带 `memory_side_role` | 投资组合经理只经 `decision_memory_retrieval` 消费顶层 `pm_learning` 正式行；分析师只消费其中显式授权 `analyst_calibration` 的安全投影 | 分析师不得读取原始 PM 行；交易员、审计员不直接读取；不能跨 action family/lane 使用 |
| `adaptive_policy_state` | protect/cap/probe/watchlist 及 technical_parameters 等未来策略状态 | 投资组合经理只经 `decision_memory_retrieval` 消费交易决策 policy；技术分析师只消费 exact-ticker、short-horizon 的 `contextual_rule_calibration:technical_parameters` | 必须被当日证据、失效边界、资金和审计再验证；审计员和交易员不直接消费 |
| `opportunity_ranking_preference` | 投资组合经理新增风险排序、资金分配理由、排名与后续收益的关系 | 目前仅供 Researcher 和开发评估诊断，没有正式 PM 消费端 | 不能声称已经影响未来 rank 或资金部署；本次不新增消费链 |
| `research_position_feedback` | 正式 action-value 与 adaptive policy 是否被实际消费，以及该合约后续是否成交和结算 | Researcher 和开发评估诊断 | action-value 只匹配 `learning_used.alpha_setup_action_values` 与最终 `decision_learning_rows`；policy 只读取 `learning_used.adaptive_policy_applied`，两类引用独立归因，不作为新 rank、手数或交易输入 |
| `setup_execution_learning` | 盘中触发、未成交、涨跌停、追价、执行质量 | 投资组合经理经 `decision_memory_retrieval` 消费后写入未来合约执行字段 | 只能影响未来 `final_action_contract.execution_profile/entry_trigger`，不改方向、不改手数；交易员不直接读取 |
| `evidence_fusion_attribution` | PM 是否正确处理多维证据一致性、冲突、反向证据、新闻时效、profile 下假突破和确认需求 | 目前仅供 Researcher 和开发评估诊断，没有正式分析师或 PM 消费端 | 不能声称已影响未来证据、rank 或仓位；不创建交易权限，不改当天事实，本次不新增消费链 |

运营风控事件也要记录，但不进入策略 alpha 学习。`source_type=rollover` 用于换月成本、合约切换和敞口恢复检查；`source_type=forced_risk` 用于保证金风险和强减结果检查。它们可以进入运营/风险复盘，不能写成策略 open/hold/exit 正负样本。

## 四、action-value 语义

action-value 的动作含义必须按 `action_name -> canonical_action_family -> action_value_lane/learning_lane -> action_preference` 解释，统一工具是 `src/tools/common/final_action_semantics.py`，完整动作矩阵见 `docs/matrix_action_canonical.md`。Researcher 写入时必须保存 canonical family 和 lane；PM、Reviewer、Researcher 后续链路和 PG 审计不得各自维护私有字符串集合来猜动作含义。

学习偏向不是明日执行指令。`positive_candidate_open` 只能说明同 family/lane 的历史样本支持 open/add 新增风险候选，不能让交易员直接下单；具体执行动作仍只能来自 PM 当日 `final_action_contract`。

固定 `action_preference` 词表只允许：

```text
positive_candidate_open
positive_candidate_hold
positive_candidate_exit
positive_candidate_execution
negative_revalidate
negative_hold_revalidate
tail_loss_protect
```

open 评价“当时开仓是否有正期望”；add 评价“同方向扩大风险是否有效”；hold 评价“继续持有是否保护收益或扩大收益”；reduce 评价“减仓是否保护收益或降低尾部风险”；exit 评价“退出是否避免回吐或尾部亏损”；conditional_monitor 评价“等待触发是否应被保留为盘中监控”；execution 评价“触发方式和成交质量是否改善结果”。

不同动作不能混用。历史 hold 赚钱不能证明新开仓赚钱；历史 exit 有效不能反向支持加仓；历史 execution 好只能被投资组合经理写入 `final_action_contract.execution_profile/entry_trigger/requires_intraday_confirmation/can_execute_without_intraday_trigger`，不能改变方向或目标手数，也不能由交易员直接读取后放宽触发。

open/add 的收益只来自仓位完全归零后的完整策略 episode。系统按 ticker/side 重放策略成交识别 `0 -> 持仓 -> 0` 的完整持仓周期；分批平仓未归零时不提前落完整 episode，最终归零后整个周期只形成一条 open/add episode/sample、一次 trade count 和一个最终净收益。各物理开平 pair 仅保留在 episode payload 中作为 gross PnL、手续费、close date 和去重明细，不得分别增加 sample、trade count、胜负或 tail loss。周期 T 的结果只能在 T+1 以后消费。日内或日终 PnL 碎片不能重复充当 open/add 收益；日记录仍只服务真实发生的 hold、reduce、exit 和 execution 生命周期。完整 episode 只是 action-value 候选来源：状态字段不完整、没有合法 preference 或只形成弱先验时，可以保留样本和 profile，但不得提升成 PM 正式 canonical 学习。

`memory_side_role` 随最终学习 lane 固定：open/add/scale/increase 使用 `target_side`，hold/reduce/exit 使用 `current_position_side`，conditional_monitor 使用 `trigger_side`，execution 使用 `historical_sample_side`。

action-value 必须保留以下核心字段，用于 `decision_memory_retrieval` 质量排序和审计保真：

- `id`；
- `action_preference`；
- `canonical_action_family`；
- `reward_source`；
- `evidence_scope`；
- `action_value_lane`；
- `consumer_scope`；
- `learning_lane`；
- `memory_side_role`；
- `reward_sum`、`reward_mean`，保留既有 action-value 生命周期分类、升层门槛和人民币财务审计用途，不进入分析师开仓学习强度、PM Rank、仓位学习方向或触发确认等级；
- `mean_return_on_notional`、`worst_return_on_notional`、`episode_return_on_notional_count`，供分析师开仓学习强度、PM Rank、仓位学习、尾部学习和触发确认统一读取手续费后名义收益率；
- `latest_complete_episode_return_on_notional`、`latest_complete_episode_date`、`latest_complete_episode_outcome`，供同完整作用域最新亏损优先撤销分析师与 PM 旧正向加分；
- `sample_count`；
- `last_sample_date`；
- `valid_until`；
- `retrieval_key`、`fallback_retrieval_key` 或 `execution_retrieval_key`。

空壳记录、无收益记录、未来日期记录、非目标 `consumer_scope` 记录、过期记录和弱先验记录不能覆盖真实有效记录。

## 五、分析师如何使用研究成果

技术面分析师、基本面分析师、期货新闻面分析师只消费本专业校准类结构化研究。它们必须把历史经验与盘前或决策时点前可见数据比较，说明当前证据确认、削弱还是反驳历史经验。

三类分析师共同使用学习成果完成两件事：

1. LLM 调用前，把仅限历史交易日且作用域匹配的校准摘要加入提示词，帮助模型识别该产品、setup 和本专业常见的有效证据、反例与数据缺口。
2. LLM 返回后，用同一批合格记录执行确定性信号校对，将确认、削弱或反驳结果落入已登记的证据、冲突、缺失和质量字段。

同一批通过 T+1、canonical、作用域、family/lane 和安全投影校验的正式 open/add 摘要必须同时用于 LLM 前 Prompt 和 LLM 后确定性校准，不得清空 Prompt 侧。Prompt 只允许模型条件性参考，最终 setup 或 canonical trigger 不精确匹配时，后置确定性校准贡献必须为零。

技术面分析师额外保留一项专业机制：Researcher 从 exact-ticker、short-horizon 技术绩效用独立查询和独立配额生产 `contextual_rule_calibration:technical_parameters`，不允许跨品种通配 policy，也不与 PM contextual policy 共享配额。在下一交易日 LLM 调用前，技术分析师根据当前可见价格形成初始自适应参数和初始 `market_regime`，再读取过去有效、作用域匹配且经过验证的 technical policy，有界调整 EMA、RSI 和 Bollinger 参数，并用校准后的参数重新计算最终技术指标和 `technical_context`。`learning_impact_summary` 记录实际改变参数的 policy ID、作用域及参数前后值，并沿 AEC→SCC 进入 PM Step6；该机制不直接修改 `signal`、`opportunity_state`、触发、手数、rank、预算和交易权限。

完整持仓 episode 进入分析师提示词时，只投影结构化的相对结构失效距离、原始 ATR 距离、预期/实际持有期、最终退出原因和手续费后 `return_on_notional`；其检索继续服从既有 ticker/sector/horizon 范围和 T+1 边界，并按 `ABS(return_on_notional)` 选择代表周期，人民币 `net_pnl` 只保留数据库审计，不进入提示词、排序或周期好坏判断。历史开仓价、历史结构价等绝对价格不得复制到当日信号，旧 episode 自由文本不得作为回退来源。正式 action-value 只有在同品种、严格早于当日、有效期合法、canonical 语义完整，且其顶层 `consumer_scope=pm_learning`、内嵌 `signal_calibration.contract_version` 合法、内嵌 `consumer_scope=analyst_calibration` 并明确允许 `analysis_team` 使用时，才投影为不含原始学习 ID、人民币reward、rank、手数和保证金的提示词摘要；该摘要保留`mean_return_on_notional`与最新完整周期收益率。分析师校准强度只按手续费后名义收益率、样本数、置信度和胜率计算；同完整作用域最新完整周期亏损时，正式action-value的负向校准优先撤销旧正向Profile校准。similar、weak、incomplete、counterfactual prior 不进入该投影。

分析校准先检索当前精确 `market_regime`。只有精确结果没有形成任何安全投影、且当前方向明确时，才允许读取同品种、同方向、同周期的跨 regime 正式摘要；该摘要以内部 `retrieval_match_level=cross_regime_same_ticker_side_horizon` 标识并按低权重进入分析校准，当前 regime 证据始终优先。技术参数 overlay 仍严格要求精确 regime，不使用该回退，也不放宽 PM 的正式学习检索。

技术参数学习先有界调整参数，再重新计算当日指标，提示词只接收不含学习 ID 的已应用摘要。技术分析师可以结合该摘要、相对化 episode 经验和当日价格结构提出新的 `position_invalidation_level/exit_hint`；结构位必须由当日正式参考价做方向校验，原始 ATR14 始终由已完成 OHLC 确定性计算并在 finalization 覆盖落地。基本面学习只校准当日独立形成中期方向时的证据评估、预期持有期和退出解释，不得覆盖方向，也不得生产数值结构位；新闻学习只校准事件影响窗口。

学习记录不得替代当日数据，不得单独创造方向、setup、触发、失效边界或交易权限；检索为空属于合法冷启动。最终唯一 `action_evidence_contract` 必须在学习校对、数据质量/时效和商品 profile 评估完成后由共享确定性工具生成。

分析师不能用 action-value 输出手数、保证金、最终开仓、加仓、减仓或平仓命令，也不能输出 `opportunity_score`、`opportunity_rank`、`capital_allocation_reason`。分析师只提供结构化预测证据，排序、资金部署和目标手数由投资组合经理及其确定性工具完成。

分析师必须输出 `action_evidence_contract`，核心字段包括：

- `setup_quality_ok`；
- `trigger_valid`；
- `current_trigger_confirmed`；
- `invalidation_present`；
- `invalidation_condition`；
- `entry_trigger`；
- `opportunity_state`；
- `data_usage_summary`；
- `no_lookahead_status`；
- `fusion_evidence`；
- `evidence_strength`；
- `evidence_freshness`；
- `evidence_decay_risk`；
- `confirmation_requirements`；
- 本专业特殊融合字段：技术面 `technical_false_breakout_risk`，基本面 `fundamental_opposition_strength`，新闻面 `news_impact_window` 和 `one_off_event_risk`。

`setup_quality_ok=true` 只表示形态值得关注，不代表当前触发成立。`trigger_valid=true/current_trigger_confirmed=true` 才表示当前触发成立。等待确认必须落到 `watch_for_trigger + trigger_valid=false`，不能写成自由文本后再被下游误读。

分析师的 `fusion_evidence` 只服务预测证据质量。它让 signal_collector 保真收集证据强弱、时效、一致性、冲突、确认需求和缺失证据；不能写入手数、保证金、reason code、authority type、`opportunity_score`、`opportunity_rank` 或 `final_action_contract`。

## 六、投资组合经理如何使用研究成果

投资组合经理不直接查研究表，不直接解析原始研究记录，不直接调用 LLM。投资组合经理只通过 `decision_memory_retrieval` 读取结构化研究成果。

`decision_memory_retrieval` 的固定职责是：

- 按 `ticker`、`side`、`trading_date`、`horizon_class`、`market_regime`、`setup_type`、`consumer_scope=pm_learning` 读取研究成果；
- 先收集可见历史，再按质量排序；
- 保留真实有效 action-value；
- 输出 `effective_memory_summary`、有效 action-value 列表、剔除/降级原因；
- 拒绝未来数据、过期数据、空壳记录、非 `pm_learning` scope、弱先验越权；
- 保证空历史不能占位置挡住真实盈利或真实亏损历史。

投资组合经理使用研究成果的固定链路是：

```text
signal_collection_contract
-> PM Step2 单品种方向
-> PM Step3 candidate_quality / 内部生命周期分流
-> PM Step4 decision_memory_retrieval.effective_memory_summary / 生命周期学习消费
-> PM Step5 pm_full_market_capital_deployment（仅实际增加风险，包括新开仓和同方向 add/scale）
-> PM Step5 position_sizing_result
-> PM Step6 原子生成 FuturesRecommendation / final_action_contract / 最终合约自身检查
```

当前 AEC → SCC → PM 的证据融合来自当日正式预测证据及确定性融合函数，不来自 `evidence_fusion_attribution` 研究记录。后者目前没有正式消费端，不能用当日融合通路反推其学习闭环已经成立。

研究记忆只影响评分分项、排序分项、仓位生命周期解释和执行 profile 偏好，不能单独创造交易机会。`candidate_quality`仍由当日机会分、触发和失效边界形成，学习只能先进入唯一机会分；同完整作用域最新完整周期亏损时，旧 `positive_learning`、正向 Profile 加分和 positive open seed 立即失效，但当日强证据仍可沿既有 exploration probe 验证。当前触发不成立时，正向历史只能支持观察或条件监控；当前证据强但没有真实历史时，历史分项按冷启动中性处理；当前证据强但历史亏损明确时，排名必须降级并写入 `capital_allocation_reason`。

策略失效不新增状态机：同 ticker、side、setup 和标准化 market_regime 的最近最多5个手续费后完整周期达到既有 `cap_min_samples` 且平均 `return_on_notional<0` 时，复用 `capped` 撤销 real/scale 放大；horizon和data_combo不拆分该负期望统计。新 probe 改善该滚动均值后，再由既有 watchlist/protected/deployable 样本、胜率、盈利因子和收益门槛恢复。人民币盈亏继续用于财务事实和既有生命周期审计，不得重新进入 Rank 或仓位学习方向判断。

PM 的决策学习生命周期与 Trader 的条件执行生命周期必须分开。`current_lots=0 -> target_lots!=0` 的条件 probe 在 PM 中仍使用 open/add 决策学习并参加新增风险 rank，但 `requires_intraday_confirmation` 继续要求 Trader 等待触发；只有手数不变且仅保留监控的最终合约才使用 conditional_monitor 决策学习。正式学习进入 `learning_used` 只证明 PM 消费事实，不证明合约一定获预算、通过审计、触发或成交。

Adaptive policy 的实际应用只由 `final_action_contract.learning_used.adaptive_policy_applied` 证明。技术参数 policy 沿 signal/AEC→SCC，PM policy 沿 control diagnostics 进入 Step6；只有真实改变评分、参数、仓位比例或资金层且未被后续规则覆盖的 policy 才写入。Researcher 的 `research_position_feedback.policy_refs_json` 只读取该字段，不读取 policy 检索列表，也不因 `memory_refs_json` 为空而清空；policy 与 action-value 分别归因。

对最终 `hold/reduce/exit` 合约，生命周期匹配只是必要条件。只有某条正式 action-value 的精确 ID 被对应软生命周期控制选中、实际改变了最终动作或仓位比例，并且该影响没有被后续规则覆盖，才允许同时进入最终 `decision_learning_rows` 与 `learning_used.alpha_setup_action_values`。唯一窄桥接是：canonical、`pm_learning` 的负向 hold 记录确实使同方向持仓比例下降时，可保持原 hold family/lane 和精确 ID 进入最终 reduce FAC；它不得被重标为 reduce，也不得把其他 hold 记录全局放行。结构/ATR 止损、明确技术反转、基本面中期反向和其他独立确定性生命周期规则即使得到同 lane 历史记录，也不得把该记录写成实际消费学习。

投资组合经理可以消费 execution action-value，但只能把它转成未来最终合约里的合约化执行字段。对入场学习，只有同品种、同方向、同 setup、同 canonical trigger 的正式 canonical open/add `entry_quality_outcome.trigger_confirmation_adjustment`，或当日结构化 weak-conflict 权限，才允许形成最终合约的 `trigger_confirmation_adjustment`；开仓episode的正负、`support_weight/penalty_weight`和确认等级只按手续费后`return_on_notional`生成，人民币盈亏只保留审计与生命周期统计。reason 文本、similar/weak/incomplete prior 不得产生该字段。交易员对`stronger_confirmation_required`在现有 FAC 路径内追加一根完整15分钟线，对`strict_confirmation_required`追加连续两根完整15分钟线，并逐根验证价格延续及整段相对量能；`standard_confirmation_supported`保持原触发。交易员仍只读 `final_action_contract` 中的 `execution_profile/entry_trigger/trigger_confirmation_adjustment/requires_intraday_confirmation/can_execute_without_intraday_trigger` 和盘中数据，不读取 action-value、`strategy_memory` 或 `adaptive_policy_state`，也不新增方向、手数或退出权限。

探索研究固定为单一路径：Researcher 将完整 episode 的每日 SCC/FAC、成交、结算、证据变化、累计峰值和利润回吐压缩进一次 LLM 输入，LLM 必须引用实际提供且与生成假设作用域匹配的 `support_episode_ids`。同一 `ticker或sector/side/setup/horizon/标准化market_regime` 只保留一个活动假设；后续同作用域研究合并支持 episode，不重复新建假设。新假设先写为 `candidate`；未来验证只使用生成日之后、同一完整作用域的真实 episode。无未来样本保持`candidate`，样本不足但已有未来样本为`monitoring`；样本达到下限后，均值不为正则`rejected`，均值为正但最新完整周期亏损则`monitoring`，均值为正且最新非负才`validated`；到期仍不足为`rejected`，后续合格样本允许恢复。未交易反事实只进入既有错失机会和 `fast_candidate_alpha` 候选路径，不能改变假设正式状态。验证过程只更新 `exploratory_hypothesis`，不得写 Profile、action-value、policy、Rank、仓位或 Trader 权限；`validated` 只进入下一交易日分析师先验，再经原有 AEC→SCC→PM FAC→Auditor→Trader 链路产生作用。

## 七、交易员、会计师、复盘员与研究边界

交易员写执行事实和 `execution_learning_trace`，不直接读取研究库、action-value、`strategy_memory` 或 `adaptive_policy_state`，不按历史好坏放宽触发，不改方向，不改手数。未触发、涨跌停、追价失败、成交量不足、合约临近交割、保证金不足等，都要写明原因供研究员未来研究。

执行触发机制的迭代路径固定为：

```text
交易员 execution_result / execution_learning_trace
-> 复盘员 Phase4 factual validation
-> 研究员 structured execution learning
-> 下一交易日投资组合经理 decision_memory_retrieval
-> 投资组合经理将 execution_profile / entry_trigger 写入 final_action_contract
-> 交易员执行合约化触发规则
```

会计师只按成交和结算价入账。手续费、保证金、释放保证金、持仓盈亏、平仓盈亏和账户权益都不能被研究文本改写。

复盘员负责确认事实完整。只有复盘员验证通过，研究员才能更新未来学习。若 phase 不完整、账务不一致、交易日志缺失或字段语义冲突，该日不得进入学习。

审计员不直接消费研究记录。研究记忆只能通过投资组合经理的评分、排序、手数计算和唯一合约间接影响审计对象；审计员只审 `final_action_contract`、账户、持仓、保证金、数据质量和硬风险边界。

## 八、相似 setup 检索和防未来函数

当前系统唯一正式术语是 RAG（Retrieval-Augmented Generation/检索增强）；`REG` 不是项目术语，也不得作为兼容别名或第三套机制。系统使用结构化检索和轻量 SQL 相似 setup 检索，不使用长文本向量 RAG 作为交易授权。检索按 ticker、sector、side、setup_type、horizon、regime、action lane 等结构化键聚合 compact evidence，并强制历史样本 `trading_date < decision_date`。

分析师摘要只在同作用域出现新的完整真实 episode 时刷新；`last_sample_date` 和 `valid_until` 从该 episode 的真实结束日计算，没有新完整周期时不重写、不续期。`analyst_learning_digest` 的生产端按完整作用域和 `digest_text` 复用单行，检索端再按同一内容键去重，历史不同 ID 的同内容副本不得重复占用提示词预算。Researcher 与分析师选择完整 episode 时统一按手续费后 `return_on_notional` 排序和判断；人民币 `net_pnl` 只服务持久化审计与财务复核。

同品种同作用域真实样本优先；同板块样本、similar SQL/RAG、shadow 样本只能作弱先验。它们不能 seed 新开仓，不能覆盖同作用域负期望，不能绕过 `decision_memory_retrieval`、PM 生命周期路由、必要的 Step5 全市场资金部署、Step6 `final_action_contract`、审计员、交易员和保证金硬上限。

研究结果不得使用未来行情污染当下决策。回测可以一次性跑多日，但每个具体回测日内部必须按 `proposal.py -> order.py -> settlement.py -> validate_phase_flow.py -> researcher_learning.py` 的时间顺序复刻真实交易流程。

## 九、研究结果如何判断有效

干净回测后，不只看研究表有没有持久化记录，还要看学习是否真正进入下一轮链路：

1. 分析师 metadata 是否读取并解释了本专业校准类研究。
2. `decision_memory_retrieval` 是否保留真实有效 action-value，且空历史没有挡住真实历史。
3. 投资组合经理的 `learning_used`、`opportunity_scorecard`、`opportunity_rank`、`position_sizing_result` 是否显示学习进入评分、排序、仓位生命周期或执行 profile。
4. `final_action_contract` 是否仍由盘前预测证据、研究分项、资金风控和审计共同决定，而不是被学习单独覆盖。
5. 交易员是否只按审计通过的合约和合约化触发规则执行或跳过。
6. 会计师是否按事实结算。
7. 研究员是否按 `canonical_action_family` 和 open/add/hold/reduce/exit/conditional_monitor/execution lane 分账并带 `memory_side_role` 更新 action-value。

如果学习只增加解释文本，却没有在未来同作用域、合规边界内改善开仓、持仓、退出、执行质量或资金部署质量，就不能认为研究机制已经贡献收益。

排序学习的有效性要单独检查：

1. 高分/高排名候选是否比低分/低排名候选贡献更好净收益、盈亏比和回撤表现。
2. 未入选候选是否频繁错过大收益；若是，研究员要生成排序偏好修正候选。
3. 资金是否从弱 alpha 状态迁移到强 alpha 状态，而不是单纯减少交易。
4. 0.8% probe floor 是否只对投资组合经理入选候选生效，没有把排序落后的弱机会重新拉回交易。
5. `learning_adjustment_summary` 是否能解释本次排序受哪些真实 action-value、setup profile 或复盘结论影响。
6. 正向 alpha 是否经历“probe 验证 -> rank 提升 -> 合规放大 -> 持仓保护/加仓 -> 失效退出”的完整周期，而不是长期停留在小仓试探。
7. 近期 tail loss 是否能抵消旧正向学习，避免失效 alpha 继续被高 rank 和 probe floor 机械放出来。

## 十、回测前验收口径

回测前应确认：

- `pg_contract_coverage_audit.py` 通过，确认 action-value、learning trace、score components、唯一合约和执行结果等核心契约都有生产、消费、审计和测试覆盖。
- 研究员 -> 投资组合经理的 action-value 边界必须通过 `decision_memory_retrieval` 保真测试，证明真实 canonical 记录不会丢失 `id/action_preference/canonical_action_family/reward_source/evidence_scope/action_value_lane/learning_lane/reward`，也不会被空壳 trace 或空历史覆盖。
- 投资组合经理不直接调用研究库读取函数；研究消费入口只保留 `decision_memory_retrieval`。
- 审计员输入不含 `strategy_memory` 或 `adaptive_policy_state`。
- 交易员执行入口不含研究库、action-value、`strategy_memory` 或 `adaptive_policy_state` 消费权限。
- 复盘员不调用 LLM、不触发研究员学习、不写最终 action-value。
- `pg_pre_backtest_acceptance.py` 通过。
- `system_invariant_audit.py` 对现有库没有 hard error。
- 字段语义表仍是唯一字段来源。
- 未完成交易日不会进入学习。
- 策略单、rollover、forced_risk 按 `source_type` 分账。
- 分析师证据不再出现“等待确认文字 + trigger_valid=true”。
- 条件 probe 未触发不会被写成真实开仓结果。
- 投资组合经理排序字段只出现在 scorecard、`final_action_contract.evidence_used/learning_used`、复盘和评估诊断中，没有成为顶层交易权限。

## 研究机制审计边界补充（2026-07-07）

PG 只对实际落库的研究记忆检查来源日期、未来学习边界、当日事实未被改写，以及 Trader/Accountant 未直接消费；不读取或复查 Researcher 的内部研究过程，也不判断 PM 如何把研究记忆转化为 open/hold/reduce/exit、为什么 rank 或不 rank。PM 对研究成果的消费是否合法由 PM Step6 和 PM 自身最终合约检查负责，不属于 daily PG 审计对象。
