# AgentQuant 记忆与研究机制

研究链路的生产端、DB 落点、下游消费、PG 审计、pre-backtest fixture 与 diagnostics 边界统一锚定 `docs/matrix_chain_contract.md`；本文只说明研究与复盘机制细节。

更新时间：2026-06-25

本文档定义 AgentQuant 的复盘、研究、记忆持久化和未来学习消费机制。它必须与 `docs/mechanism_multiagents.md` 的固定工作流一致，并以 `docs/matrix_field_semantics.md` 作为唯一字段语义矩阵。若本文与多智能体运行机制冲突，以固定工作流、智能体边界和字段语义矩阵为准。

研究机制只服务未来交易日的结构化学习，不产生当天交易动作，不改写当天合约、成交、结算或收益。

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

信号收集员、审计员、交易员、会计师、复盘员都不能直接读取研究库来生成或改变交易权限。

`template_prior` 是冷启动研究种子，只能通过 `src/run/research/load_template_prior.py` 显式加载。它不属于 Phase1 盘前策略生成，不由 `proposal.py` 自动写入研究记忆，也不能使用当天或未来交易结果。

`product_price_behavior_profiles.yaml` 是三类分析师的商品差异化冷启动分析框架，不是研究库，不随回测自动改写。研究结论用于更新分析师差异化的方式只有一条：Researcher 写结构化分析师校准类研究；下一交易日同一批合格的 `learning_context` 和 `analyst_learning_calibration` 先进入 `technical`、`fundamental`、`commodity_news` 的提示词，再在 LLM 返回后确定性校对信号。静态 profile 继续提供品种基础框架，动态学习作为可反驳校准叠加其上。Auditor、Trader、Accountant 不读取 profile，也不读取分析师校准来改变交易权限、触发或入账。

多维证据融合协议由 `tools/common/evidence_fusion_semantics.py` 的确定性函数固定实现，不设置无人读取的 YAML 参数。研究结论进入融合协议的方式只有一条：Reviewer 先只读标注 `fusion_attribution_label`，Researcher 再写入未来可用的 `evidence_fusion_attribution` 学习事件；下一交易日三类分析师通过 `learning_context` 校准证据，PM 通过 `decision_memory_retrieval`、生命周期学习路由和必要的新增风险 Step5 资金部署消费结构化学习摘要。融合学习不能回写当天 `final_action_contract`、`execution_result`、`daily_settlement` 或审计结果。

## 二、Phase4 与研究学习分工

复盘员是确定性复盘者，不调用 LLM、不下单、不改账、不写最终 action-value。它检查 Phase1-3 是否完成，推荐、审计、执行、成交、结算等已落地事实是否完整一致，完整交易日志是否输出，并对交易结果做事实归因。复盘员可以输出事实归因、交易日志和研究输入材料，但未来学习由研究员输出并持久化。`max_net_exposure`、`target_margin_ratio_*`、`probe_margin_ratio`、`strong_opportunity_*`、`recovery_*` 等 PM 计划预算参数在真实成交后出现偏离时，只能进入复盘事实归因、warning 和研究输入材料；复盘员不能把这类计划预算偏离当作日终 hard fail 或策略违规裁决。阶段断链、应落地事实缺失、成交与执行不一致、成交未入账、结算或账户公式不一致以及来源链断裂可以使 Phase4 事实复盘失败；账户硬风险合法性已经由 Auditor 和运营风控链负责，复盘员不得二次裁决。

Phase4 标记 completed 只表示复盘验收通过；它不能触发 `strategy_memory` 刷新、学习 retention 清理、研究表写入或任何未来学习状态更新。

研究员可以按配置调用 LLM，但只能在复盘员验证后的事实底座上运行。研究员输出结构化研究信息：分析师校准类研究、交易决策类 action-value、alpha setup profile、adaptive policy state、执行学习、排序偏好和研究反馈；这些信息供其他智能体按各自权限直接或间接使用，持久化到研究库只是保存方式。研究员不能下交易指令，不能改账，不能绕过投资组合经理、审计员或交易员。

未完成交易日必须硬拦。若某天推荐、成交、盘中决策或学习记录已存在，但 phase1-4 没有全部 completed，系统应报 `incomplete_trading_day_phase`；该日不能进入收益判断，也不能被研究员当成学习样本。

## 三、研究对象与消费边界

| 研究对象 | 记录什么 | 合法消费者 | 边界 |
|---|---|---|---|
| `trade_episode_memory` | 已成交策略 episode、证据、合约、执行、结算、PnL | 研究员汇总；分析师读取校准摘要；投资组合经理经 `decision_memory_retrieval` 间接消费 | 只来自 Phase4 后已验证事实 |
| `no_trade_opportunity_memory` | 未交易机会、no-trade 原因、影子结果、错过机会 | 研究员汇总；分析师读取校准摘要；投资组合经理经 `decision_memory_retrieval` 间接消费 | 不能直接授权开仓，只能作为先验、反证或排序诊断 |
| `alpha_setup_sample` | 单个 setup 的交易、未交易、执行样本 | 研究员汇总 | 必须有交易日、方向、setup、horizon、regime、数据质量 |
| `alpha_setup_profile` | setup 生命周期、胜率、盈亏因子、净 PnL、最大亏损 | 分析师读取校准类摘要；投资组合经理经 `decision_memory_retrieval` 消费交易决策类摘要 | 只作为同作用域证据，不是品种黑名单 |
| `alpha_setup_action_value` | 按 `canonical_action_family` 与 open/add/hold/reduce/exit/execution/conditional_monitor lane 分账的动作学习结果，并带 `memory_side_role` | 投资组合经理只经 `decision_memory_retrieval` 消费；分析师只消费校准类摘要 | 交易员不直接读取；审计员不直接读取；不能跨 action family/lane 使用 |
| `adaptive_policy_state` | protect/cap/probe/watchlist 等未来策略状态 | 投资组合经理只经 `decision_memory_retrieval` 消费 | 必须被当日证据、失效边界、资金和审计再验证；审计员和交易员不直接消费 |
| `opportunity_ranking_preference` | 投资组合经理新增风险排序、资金分配理由、排名与后续收益的关系 | 投资组合经理经 `decision_memory_retrieval` 和 PM Step5 新增风险资金部署机制消费；研究员复核 | 只影响未来新增风险机会评分和资金部署优先级，不生成交易权限 |
| `research_position_feedback` | 研究是否进入投资组合经理、是否改变合约、是否成交和结算 | 投资组合经理 / 研究员 / 协议治理审计 | 用于检查学习是否真的进入仓位链路 |
| `setup_execution_learning` | 盘中触发、未成交、涨跌停、追价、执行质量 | 投资组合经理经 `decision_memory_retrieval` 消费后写入未来合约执行字段 | 只能影响未来 `final_action_contract.execution_profile/entry_trigger`，不改方向、不改手数；交易员不直接读取 |
| `evidence_fusion_attribution` | PM 是否正确处理多维证据一致性、冲突、反向证据、新闻时效、profile 下假突破和确认需求 | 分析师读取校准摘要；投资组合经理经 `decision_memory_retrieval`、PM 生命周期路由和必要的 Step5 资金部署间接消费 | 只影响未来证据解释、排序分项和冲突处理偏好；不创建交易权限，不改当天事实 |

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
- `reward_sum`、`reward_mean`；
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

技术面分析师额外保留一项专业机制：在 LLM 调用前，根据当前可见价格形成初始自适应参数和初始 `market_regime`，再读取过去有效、作用域匹配且经过验证的 `contextual_rule_calibration:technical_parameters`，有界调整 EMA、RSI 和 Bollinger 参数，并用校准后的参数重新计算最终技术指标和 `technical_context`。该机制不直接修改 `signal`、`opportunity_state`、触发、手数、rank、预算和交易权限。

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

多维证据融合学习进入 PM 的固定链路是：

```text
Reviewer fusion_attribution_label
-> Researcher evidence_fusion_attribution
-> 下一交易日 decision_memory_retrieval / analyst_learning_calibration
-> signal_collection_contract.evidence_fusion
-> PM scorecard.pm_fusion_diagnostics
-> portfolio_manager.final_action_contract.evidence_used.pm_fusion_diagnostics
```

这条链只改变未来预测证据解释、PM 排序分项和冲突处理说明，不改变当天成交、结算或交易员执行权限。

研究记忆只影响评分分项、排序分项、仓位生命周期解释和执行 profile 偏好，不能单独创造交易机会。当前触发不成立时，正向历史只能支持观察或条件监控；当前证据强但没有真实历史时，历史分项按冷启动中性处理；当前证据强但历史亏损明确时，排名必须降级并写入 `capital_allocation_reason`。

投资组合经理可以消费 execution action-value，但只能把它转成未来最终合约里的合约化执行字段。交易员只读 `final_action_contract` 中的 `execution_profile/entry_trigger/requires_intraday_confirmation/can_execute_without_intraday_trigger` 和盘中数据，不读取 action-value、`strategy_memory` 或 `adaptive_policy_state`。

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

当前系统使用结构化检索和轻量 SQL 相似 setup 检索，不使用长文本向量 RAG 作为交易授权。检索按 ticker、sector、side、setup_type、horizon、regime、action lane 等结构化键聚合 compact evidence，并强制历史样本 `trading_date < decision_date`。

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
