# AgentQuant 记忆与研究机制

更新时间：2026-06-21

本文档说明当前已经代码落地的研究、复盘和学习闭环。它不把临时想法写成事实；若回测库已被清空，旧样本数量不代表当前系统状态，所有学习结论必须由新一轮干净回测重新生成。

## 一、研究机制原则

研究机制服务主动 alpha 迭代，而不是堆叠被动限制。Researcher 要从真实交易、未交易机会、条件机会未触发、Neutral、执行失败、换月/强平运营事件和结算结果中总结：哪些 setup 值得开仓，哪些持仓应保护，哪些退出保护了利润，哪些执行方式更好，哪些证据只是噪音。

当前研究机制还要评价 PM 的全市场机会排序和资金部署是否有效。PM 写出的 `opportunity_score/opportunity_score_components/opportunity_rank/capital_allocation_reason/learning_adjustment_summary` 会驱动 PM 的资金部署 pass，并回写同一张 `final_action_contract`；Reviewer/Researcher 要用这些字段复盘“资金是否流向更强 alpha”，但不能把它们变成第二套交易权限。

学习必须守住时间边界。Phase1/2/3 的事实完成后，Reviewer 先做确定性验收；只有 Phase4 验证通过，Researcher 才能写入未来可用学习。任何学习都不能反向修改当天推荐、成交、保证金、手续费、结算价或 PnL。

当前有效学习链路是：

```text
state
  -> action lane / action_preference
  -> opportunity score / rank / capital allocation reason
  -> PM final_action_contract
  -> Trader execution / not_triggered / no_fill
  -> Accountant settlement outcome
  -> Reviewer validation
  -> Researcher future learning
```

state 由品种、板块、方向、setup 类型、horizon、market regime、evidence combo、数据质量、机会状态、触发/失效边界构成。动作价值只分 open、hold、exit、execution 四条主线。probe 是 PM 的受控探索权限或仓位形态，不是单独 action-value；add/reduce/scale 由 `current_lots -> target_lots -> lots_delta` 推出。

排序学习不新增交易动作。它只判断同一批候选里，PM 的评分、排名和资金分配理由是否提高了资金部署质量。被 PM 选中的 probe 仍按既有 0.8% 最小试探资金边界执行；未入选候选不能靠 probe floor 自动复活，必须留下 `capital_allocation_reason` 供复盘。

## 二、Reviewer 与 Researcher 分工

Reviewer 是确定性复盘者，不调用 LLM、不下单、不改账。它检查 Phase1-3 是否完成、推荐/成交/结算是否一致、完整交易日志是否输出、未完成交易日是否存在、数据质量和字段语义是否可审计。Reviewer 还要记录 PM 排序字段和资金分配理由，复盘高分/高排名候选是否真的贡献收益、低分/低排名或未入选候选是否错过收益。Reviewer 可以写学习候选和归因事实，但最终未来学习由 Researcher 写入。

Researcher 是研究员，可以按配置调用 LLM 做 causal review 和探索性研究。它只在 Reviewer 验证后的事实底座上运行，写入未来可用记忆、alpha setup 档案、action-value、adaptive policy state、机会排序偏好候选和研究摘要。Researcher 不能下交易指令，不能改账，不能绕过 PM/Auditor/Trader。排序偏好只能影响未来 PM 的评分和资金部署优先级，不能直接生成 `target_lots`、不能改变 Trader 执行方向或手数。

未完成交易日必须硬拦。若某天推荐、成交、盘中决策或学习记录已存在，但 phase1-4 没有全部 completed，系统会报 `incomplete_trading_day_phase`；该日不能进入收益判断，也不能被 Researcher 当成学习样本。

## 三、当前写入的研究对象

| 研究对象 | 记录什么 | 未来谁用 | 边界 |
| --- | --- | --- | --- |
| `trade_episode_memory` | 已成交策略 episode、证据、合约、执行、结算、PnL | 分析师、PM、Researcher | 只来自 Phase4 后已验证事实 |
| `no_trade_opportunity_memory` | 未交易机会、no-trade 原因、影子结果、错过机会 | 分析师、PM、Researcher | 不能直接授权开仓，只能作为先验或反证 |
| `alpha_setup_sample` | 单个 setup 的交易/未交易/执行样本 | Researcher 汇总 | 必须有交易日、方向、setup、horizon、regime、数据质量 |
| `alpha_setup_profile` | setup 生命周期、胜率、盈亏因子、净 PnL、最大亏损 | 分析师、PM | 只作为同作用域证据，不是品种黑名单 |
| `alpha_setup_action_value` | open/hold/exit/execution 分动作结果 | PM、分析师间接使用 | Trader 不直接读取；PM 只按 action lane 使用 |
| `adaptive_policy_state` | protect/cap/probe/watchlist 等未来策略状态 | PM、Auditor | 必须被当日证据、失效边界和审计再验证 |
| `opportunity_ranking_preference` | PM 排序、资金分配理由、排名与后续收益的关系 | PM、Researcher | 只影响未来机会评分和资金部署优先级，不生成交易权限 |
| `research_position_feedback` | 研究是否进入 PM、是否改变合约、是否成交和结算 | PM、Researcher | 用于检查学习是否真的进入仓位链路 |
| `setup_execution_learning` | 盘中触发、未成交、涨跌停、追价、执行质量 | PM、Researcher | 只影响未来 execution profile，不改方向/手数 |

运营风控事件也要记录，但不进入策略 alpha 学习。`source_type=rollover` 用于换月成本、合约切换和敞口恢复检查；`source_type=forced_risk` 用于保证金风险和强减结果检查。它们可以进入运营/风险复盘，不能写成策略 open/hold/exit 正负样本。

## 四、action-value 语义

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

open 评价“当时开仓是否有正期望”；hold 评价“继续持有是否保护收益或扩大收益”；exit 评价“退出/减仓是否避免回吐或尾部亏损”；execution 评价“触发方式和成交质量是否改善结果”。

不同动作不能混用。历史 hold 赚钱不能证明新开仓赚钱；历史 exit 有效不能反向支持加仓；历史 execution 好只能被 PM 写入 `final_action_contract.execution_plan/execution_profile`，不能改变方向或目标手数。

## 五、分析师如何使用研究成果

Technical、Fundamental、Commodity News 只读取受限学习上下文和 `signal_calibration`。它们必须把历史经验与当日数据比较，说明当前证据确认、削弱还是反驳历史经验。分析师不能用 action-value 输出手数、保证金、最终开仓或平仓命令，也不能输出 `opportunity_score/opportunity_rank/capital_allocation_reason`。分析师只提供足够可排序的证据，排序和资金部署由 PM 完成。

分析师当前必须输出结构化证据：`setup_quality_ok`、`trigger_valid`、`current_trigger_confirmed`、`invalidation_present`、`entry_trigger`、`opportunity_state`、`data_usage_summary`。`setup_quality_ok` 只表示形态值得关注；`trigger_valid/current_trigger_confirmed` 才表示当前触发成立。等待确认文字必须落到 `watch_for_trigger + trigger_valid=false`。

`watch_for_trigger` 不是“没用的等待”。若它同时带有明确方向、触发条件、失效边界和可关注 setup，PM 可以把它纳入条件监控候选，由同一张 `final_action_contract` 交给 Trader 盘中检查。

## 六、PM 如何使用研究成果

PM 读取 open/hold/exit 的动作偏好、alpha setup profile 和机会排序偏好。成熟正向 open setup 在当日触发、失效边界、market confirmation、资金和 Auditor 通过时，可以支持受控落仓或放大；未知 setup 可以保留受控探索；负向 setup 只能同作用域 cap、revalidate、probe/reduce，不能形成全局品种黑名单。

PM 现在还要做全市场候选比较。`opportunity_score` 和 `opportunity_rank` 用于决定同一交易日内哪些候选优先获得资金，`capital_allocation_reason` 用于解释为什么给资金、只监控或暂不分配，`learning_adjustment_summary` 用于说明历史学习如何影响排序。它们只能通过 PM 资金部署 pass 回写同一张 `final_action_contract.target_lots/lots_delta/final_action`，不能形成第二套交易命令。

PM 评分必须把完整 episode 学习放在单日噪声之前。`positive_candidate_open/hold/exit/execution` 会进入 `positive_learning` 或 `execution_profile_learning`，支持同作用域机会提升排名、继续持有、择机放大或优化执行 profile；`tail_loss_protect/negative_revalidate/negative_hold_revalidate` 会进入 `negative_learning` 或 `recent_tail_loss_penalty`，用于降低同作用域排名、降级持仓或更快退出。负向学习不是品种黑名单，正向学习也不是无限放大；二者都必须按 `exact_real_state > partial_real_state > similar_sql_prior > observation_only` 和时间衰减生效，并且只能通过 PM 的同一张最终合约改变目标仓位。

PM 还会读取 execution action-value，但只能把它消化成最终合约的执行计划。Trader 仍只读 `final_action_contract` 和盘中数据。

条件机会闭环现在是研究机制的一部分。若分析师输出干净的 `watch_for_trigger + trigger_valid=false + setup_quality_ok + 明确方向/entry_trigger/invalidation`，PM 不能把它当普通 wait 丢掉；PM 可以生成条件 probe 合约。Trader 未触发时只记录未触发原因，Researcher 不能把未触发当成开仓亏损样本，只能研究条件是否太苛刻、监控是否有价值。

## 七、Trader、Accountant、Reviewer 与学习边界

Trader 写执行事实和执行学习事件，但不直接读取研究 action-value，不创造策略。未触发、涨跌停、追价失败、成交量不足、合约临近交割、保证金不足等，都要写明原因供 Researcher 未来研究。Trader 可以把成交或未成交事实与 PM 排名诊断关联记录，但不能按 `opportunity_score/opportunity_rank` 改方向、改手数或创造交易。

Accountant 只按成交和结算价入账。手续费、保证金、释放保证金、持仓盈亏、平仓盈亏和账户权益都不能被研究文本改写。

Reviewer 负责确认事实完整。只有 Reviewer 验证通过，Researcher 才能更新未来学习。若 phase 不完整、账务不一致、交易日志缺失或字段语义冲突，该日不得进入学习。

## 八、相似 setup 检索和防未来函数

当前系统使用轻量 SQL 相似 setup 检索，不使用长文本向量 RAG 作为交易授权。检索按 ticker、sector、side、setup_type、horizon、regime、action 等结构化键聚合 compact evidence，并强制历史样本 `trading_date < decision_date`。

同品种同作用域真实样本优先；同板块样本、similar SQL/RAG、shadow 样本只能作弱先验。它们不能 seed 新开仓，不能覆盖同作用域负期望，不能绕过 `final_action_contract`、Auditor、Trader 和 20% 保证金硬上限。

## 九、研究结果如何判断有效

干净回测后，不只看有没有写入研究表，还要看学习是否真正进入下一轮链路：

1. 分析师 metadata 是否读取并解释了学习上下文。
2. PM 的 `learning_to_position_trace` 是否显示学习进入机会评分、仓位生命周期或执行 profile。
3. `final_action_contract` 是否仍由当日证据和审计决定，而不是被学习单独覆盖。
4. Trader 是否只按合约执行或跳过。
5. Accountant 是否按事实结算。
6. Researcher 是否按 open/hold/exit/execution 分账更新 action-value。

如果学习只增加解释文本，却没有在未来同作用域、合规边界内改善开仓、持仓、退出、执行质量或资金部署质量，就不能认为研究机制已经贡献收益。

排序学习的有效性要单独检查：

1. 高分/高排名候选是否比低分/低排名候选贡献更好净收益、盈亏比和回撤表现。
2. 未入选候选是否频繁错过大收益，若是，Researcher 要生成排序偏好修正候选。
3. 资金是否从弱 alpha 状态迁移到强 alpha 状态，而不是单纯减少交易。
4. 0.8% probe floor 是否只对 PM 入选候选生效，没有把排序落后的弱机会重新拉回交易。
5. `learning_adjustment_summary` 是否能解释本次排序受哪些真实 action-value、setup profile 或复盘结论影响。
6. 正向 alpha 是否经历“probe 验证 -> rank 提升 -> 合规放大 -> 持仓保护/加仓 -> 失效退出”的完整周期，而不是长期停留在小仓试探。
7. 近期 tail loss 是否能抵消旧正向学习，避免失效 alpha 继续被高 rank 和 probe floor 机械放出来。

## 十、回测前验收口径

回测前应确认：

- `pre_backtest_acceptance.py` 通过。
- `system_invariant_audit.py` 对现有库没有 hard error。
- 字段语义表仍是唯一字段来源。
- 未完成交易日不会进入学习。
- 策略单、rollover、forced_risk 按 `source_type` 分账。
- 分析师证据不再出现“等待确认文字 + trigger_valid=true”。
- 条件 probe 未触发不会被写成真实开仓结果。
- Trader、Accountant、Reviewer、Researcher 的边界没有互相泄露。
- PM 排序字段只出现在 scorecard、`final_action_contract.evidence_used/learning_used`、复盘和评估诊断中，没有成为顶层交易权限。
