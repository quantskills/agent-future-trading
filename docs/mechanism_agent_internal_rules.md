# AgentQuant 智能体内部转换机制

更新时间：2026-06-27

本文定义 AgentQuant 各智能体内部如何把输入转换成正式输出。`mechanism_multiagents.md` 规定“谁负责什么、上下游怎么走”；本文规定“智能体内部怎样转，哪些状态必须落到什么输出”。本文不新增交易字段，不替代 `unified_field_semantics.md`，不改变固定工作流。

本文作用：给开发和审查代码时使用，专门约束每个智能体内部转换机制，防止输入已经正确、字段已经统一，但智能体内部规则把状态转错、写错或层层门控压死交易。本文不评价收益，不新增交易权限，不限制 LLM 的推理过程；它只规定 LLM 推理结果和确定性规则结果如何落到正式结构化输出。

## 文档导航

开发时按以下顺序使用本文：

| 开发任务 | 先看 | 再看 | 最后确认 |
|---|---|---|---|
| 修改任一智能体内部规则 | 一、二、三 | 对应智能体章节 | 十三 |
| 修改 PM 状态流转、资金或手数 | 二、三 | 六 | 十三 |
| 修改 LLM 提示词或解析器 | 三、四、十一 | `unified_field_semantics.md` | 十三 |
| 修改 reason code | 二的 `reason code` 语义表 | 对应智能体章节 | 十三 |
| 修改配置参数 | 六的配置参数对应关系 | `dev.yaml` / catalog YAML | 十三 |
| 修改测试或回测前检测 | 十三 | 十二 | `pre_backtest_test.py` / `backtest_daily_test.py` |

本文分三块使用：

1. 共性约束：一至三，规定术语、状态词、reason code、LLM 落地规则。
2. 智能体转换：四至十二，规定每个智能体如何把输入转成输出。
3. 开发落地：十三，规定字段、配置、提示词和测试如何同步。

## 一、本文目标

本文解决四类问题：

1. 非 LLM 智能体内部转换不能靠零散 if 和局部 reason code 互相覆盖，必须有明确状态流转。
2. LLM 智能体不能被结构化字段限制推理能力，但 LLM 输出必须落到登记字段。
3. 同一个字段、状态或 reason code 不能在前一段表示“候选”，后一段又表示“阻断”。
4. 软门控只能降级、缩手数、转条件监控或补证据；硬门控才阻断交易，不能层层软门控把交易压死。

核心原则：

```text
非 LLM 智能体：输入 -> 确定性状态流转 -> 结构化输出
LLM 智能体：自由推理 -> 结构化落地字段 -> 下游确定性消费
```

## 二、统一术语

| 术语 | 含义 | 边界 |
|---|---|---|
| 内部推理 | LLM 对信息、冲突、反事实和不确定性的分析过程 | 可以充分展开，但不能直接成为交易权限 |
| 内部状态 | 智能体内部对输入的确定性分类，如 `watch_for_trigger`、`probe_candidate`、`audit_pass`、`executed` | 必须有唯一后续流转 |
| 状态流转 | 输入状态在规则引擎中转成正式输出的路径 | 不能同一个状态既放行又阻断 |
| 软门控 | 降级、缩手数、降低 rank、转条件监控、要求补确认 | 不能直接永久压死交易 |
| 硬门控 | 越权、未来函数、字段缺失、无效合约、保证金硬风险、价格异常、结算错误等 | 可以阻断交易或回测 |
| 落地契约 | 智能体正式输出的结构化格式 | 下游只消费落地契约，不消费内部草稿 |
| profile | 对某类 setup 或行为模式的历史画像，记录它在什么市场环境、触发条件、持仓周期和风险边界下表现较好或较差 | 只能作为未来判断的结构化背景，不能单独生成交易动作或手数 |
| adaptive policy state | 研究员基于复盘事实形成的未来策略状态，表达某类 setup 后续应保护、降级、试探、观察或再验证 | 只能由 PM 经 `decision_memory_retrieval` 消费后再结合当日证据、失效边界、资金和审计落地；不能直接给审计员或交易员使用 |

例如：`BU 多头突破 setup profile` 可以记录“库存下降 + 技术突破 + 新闻无反向冲击”这类 setup 在过去 20 个样本中的胜率、平均收益、最大亏损、适合持有天数、常见失效条件。它能帮助分析师校准证据、帮助 PM 排序和控制仓位，但不能直接命令“今天买几手 BU”。

例如：某类 `BU short inventory_pressure` setup 连续两次触发后快速反弹，研究员可以生成 `adaptive policy state=watchlist_or_probe_only`，含义是“未来同类 setup 先观察或小仓再验证”。它不能命令交易员少下一手，也不能让审计员直接阻断；PM 必须在未来交易日看到当日证据、触发和失效边界后，才能决定是否观察、试探、正常交易或放弃。

### 2.1 统一状态词表

状态词分两类：分析师和信号收集员输出的是机会状态，PM 输出的是最终交易动作。机会状态只说明“机会质量和触发状态”，最终交易动作才说明“系统准备怎么交易”。任何代码不得把机会状态直接当成最终交易动作。

| 状态词 | 状态类型 | 产生者 | 消费者 | 含义 | 是否能直接交易 |
|---|---|---|---|---|---|
| `no_opportunity` | 机会状态 | 分析师 | 信号收集员、PM | 无有效方向、无完整 setup、数据不足或证据不足 | 否 |
| `watch_for_trigger` | 机会状态 | 分析师 | 信号收集员、PM | setup 可以观察，但当前触发未成立；需要 `entry_trigger` 和失效边界 | 否，不能直接交易；只能由 PM 写成条件触发合约 |
| `probe_candidate` | 机会状态 | 分析师 | 信号收集员、PM | 当前触发成立，但证据偏弱、单一或仍需小额验证 | 否，不能直接交易；PM 可转成 `open_probe` |
| `tradeable_candidate` | 机会状态 | 分析师 | 信号收集员、PM | 当前触发成立、setup 和失效边界完整、证据强 | 否，不能直接交易；PM 可转成 `open_real/add/scale` |
| `risk_reduction_candidate` | 机会状态 | 分析师/PM 诊断 | PM | 当前证据支持减仓、退出或风险收缩 | 否；PM 可转成 `reduce/exit` |
| `wait` | 最终交易动作 | PM | 审计员、交易员、复盘员、研究员 | 当天不建立新交易动作，目标手数为 0 | 否 |
| `hold` | 最终交易动作 | PM | 审计员、交易员、会计师、复盘员、研究员 | 继续持有当前仓位，`target_lots == current_lots` | 否，不产生新成交 |
| `open_probe` | 最终交易动作 | PM | 审计员、交易员、会计师、复盘员、研究员 | 小额试探开仓，必须有触发、失效边界和风险预算 | 是，审计通过后执行 |
| `open_real` | 最终交易动作 | PM | 审计员、交易员、会计师、复盘员、研究员 | 正常真实开仓，必须有强证据、资金预算和审计通过 | 是，审计通过后执行 |
| `add` | 最终交易动作 | PM | 审计员、交易员、会计师、复盘员、研究员 | 同方向增加已有仓位 | 是，审计通过后执行 |
| `scale` | 最终交易动作 | PM | 审计员、交易员、会计师、复盘员、研究员 | 在强机会、正向学习和资金预算支持下放大已有仓位或新开仓目标手数 | 是，审计通过后执行 |
| `reduce` | 最终交易动作 | PM | 审计员、交易员、会计师、复盘员、研究员 | 同方向降低已有仓位 | 是，审计通过后执行 |
| `exit` | 最终交易动作 | PM | 审计员、交易员、会计师、复盘员、研究员 | 退出已有仓位，目标手数为 0 | 是，审计通过后执行 |

硬规则：

1. 分析师和信号收集员只能产生机会状态，不能产生最终交易动作。
2. PM 是唯一能把机会状态转换成最终交易动作的智能体。
3. `watch_for_trigger` 不能直接变成成交；必须由 PM 写入条件触发合约，并由 Trader 盘中确认。
4. `probe_candidate` 和 `tradeable_candidate` 不能自己决定手数；手数只能来自 PM 的资金、风险和合约计算。
5. `tradeable_candidate` 不是小额试探；它是强候选，PM 可按资金和风险规则转成 `open_real/add/scale`。
6. 审计员、交易员、会计师、复盘员、研究员都不能把机会状态改成最终交易动作。

### 2.2 reason code 语义表

`reason_codes` 只解释状态流转原因，不能代替状态、动作或手数。每个 reason code 必须只属于一类；同一个 code 不能前面表示“候选”，后面又表示“阻断”。新增或修改 reason code 时，必须同步到共享分类逻辑和测试。

| 类别 | 含义 | 允许效果 | 禁止效果 | 典型例子 |
|---|---|---|---|---|
| 候选 | 说明机会可进入观察、试探或条件触发评估 | 进入 PM 后续判断；保留条件触发、probe 或排序资格 | 直接阻断、直接成交、直接生成手数 | `pm_watch_for_trigger_probe_cap`、`scorecard_current_tradeable_probe_seed`、`conditional_monitor_probe_seed` |
| 降级 | 说明机会质量、确认度、学习表现或资金条件不足，需要降低交易强度 | 降为观察、条件触发、probe、缩小手数或降低排序 | 当成硬阻断清零；绕过 PM 直接改手数 | `horizon_consistency_probe_cap`、`market_confirmation_conflict`、`weak_signal_combo_probe_cap`、`business_quality_probe_only`、`adaptive_policy_cap` |
| 阻断 | 说明交易在合法性、数据、合约、保证金、执行条件或硬风险上不可通过 | 阻断新开仓、阻断执行、要求 `wait/0` 或风险处置 | 被释放 code 覆盖；被软门控降级成可交易 | `margin_insufficient`、`critical_data_gap`、`data_price_anomaly`、`future_data_contamination`、`contract_expiry_hard_block`、`minimum_real_trade_no_feasible_lot`、`intraday_trigger_not_met` |
| 释放 | 说明某个候选在当前证据、学习、资金或触发条件下可从观察/试探进入更高交易强度 | 允许 PM 在硬门控通过后转成条件触发、`open_probe`、`open_real`、`add` 或 `scale` | 绕过硬门控、绕过失效边界、绕过审计、直接给 Trader 下单 | `conditional_trigger_authority`、`qualified_positive_expectancy`、`positive_expectancy_scale`、`real_probe_positive_or_strong_confirmation_release`、`mature_alpha_release`、`minimum_one_lot_probe` |
| 诊断 | 说明为什么保持、未选、未成交、无机会或已匹配 | 进入日志、复盘、研究归因 | 改变动作、改变手数、改变审计权限 | `position_matched`、`neutral_signal_no_trade`、`capital_queue_not_selected`、`learning_signal_seen` |

分类边界：

| 情况 | 归类规则 | 禁止 |
|---|---|---|
| 审计裁决类 code | 按实际效果归类：硬风险阻断归入阻断，风险降级归入降级，日志说明归入诊断 | 单独开一套审计语义绕过 PM 合约 |
| 执行结果类 code | 成交、未成交、等待触发、部分执行等只能归入诊断；`intraday_trigger_not_met` 这类阻止当日执行的 code 归入阻断 | 反向修改 PM 合约或生成新动作 |
| 学习类 code | 只要降低交易强度，归入降级；只有明确硬规则禁止交易时才归入阻断；正向学习只能归入释放 | 把负向学习默认当硬阻断，或把正向学习当免审计授权 |
| 资金/手数类 code | 不可成交或无可行手数归入阻断；只能缩小手数或降低优先级归入降级；满足最小可交易单位归入释放 | 在不同函数里既当降级又当阻断 |
| 未登记但带交易效果的 code | 回测前测试必须失败，并要求登记到本表对应类别 | 默认按字符串猜测语义继续运行 |

组合规则：

1. 阻断 code 优先级最高；只要存在未解除的阻断 code，释放 code 不能把它变成可交易。
2. 降级 code 只能降低交易强度；不能独立把候选清成 `wait/0`，除非同时存在明确阻断 code 或必要字段缺失。
3. 候选 code 只保留机会资格；它不能证明可成交，也不能证明必须阻断。
4. 释放 code 只在硬门控、失效边界、资金预算和审计都通过后生效。
5. 诊断 code 不参与交易权限计算；它只能用于日志、复盘和研究归因。
6. `pm_watch_for_trigger_probe_cap` 固定属于候选/受控观察语义，不能再被用作阻断理由。
7. 一个 code 不得同时出现在两个类别；如果业务含义变化，必须新增 code，不能复用旧 code。
8. code 的真实效力以共享分类逻辑为准，PM、Auditor、Trader、Reviewer 和审计器不能各自维护一套相反解释。
9. 所有带交易效果的新 code 必须同时具备：类别、允许效果、禁止效果、至少一个结构测试。

## 三、全局内部转换规则

所有智能体必须遵守：

1. 只能读取上游正式输出，不能读取上游内部草稿。
2. 内部草稿不能直接写入下游 payload、DB 或 artifact。
3. 一个 reason code 只能有一个业务含义；如需表达候选、降级、阻断，必须分清语义。
4. 状态流转必须有正向出口，不能只有阻断规则。
5. 硬门控和软门控必须分层：硬门控先判定合法性，软门控再决定降级、条件触发或手数。
6. 非 LLM 智能体不能重新解释自由文本；只能消费结构化字段。
7. LLM 智能体可以充分推理，但最终只能输出结构化证据或结构化研究成果。

### 3.1 LLM 输出落地检查

LLM 可以自由推理，但提示词、解析器和测试必须保证输出落到结构化字段。自由文本只能解释原因，不能成为下游交易权限。

| LLM 智能体 | 输出契约 | 必须覆盖的结构化字段 | 自由文本允许范围 | 禁止 |
|---|---|---|---|---|
| 技术面分析师 | `action_evidence_contract` | `signal`、`opportunity_state`、`setup_type`、`entry_trigger`、`trigger_valid/current_trigger_confirmed`、`invalidation_present/invalidation_condition`、`confidence`、`data_usage_summary`、`conflict_analysis` | 解释价格形态、触发依据、失效位和不确定性 | 输出手数、仓位、PM rank、资金理由、`final_action_contract` |
| 基本面分析师 | `action_evidence_contract` | `signal`、`opportunity_state`、`setup_type`、`fundamental_driver`、`driver_direction`、`driver_freshness`、`setup_quality_ok`、`invalidation_present/invalidation_condition`、`confidence`、`data_usage_summary` | 解释供需、库存、利润、基差、季节性和驱动持续性 | 输出手数、仓位、交易动作、资金部署 |
| 期货新闻面分析师 | `action_evidence_contract` | `signal`、`opportunity_state`、`event_type`、`event_direction`、`impact_window`、`catalyst_quality`、`event_priced_in`、`entry_trigger`、`invalidation_present/invalidation_condition`、`confidence`、`data_usage_summary` | 解释新闻事件、政策冲击、影响窗口、是否已兑现 | 把新闻方向直接写成交易动作或手数 |
| 研究员 | 结构化研究成果 | `research_domain`、`sample_scope`、`source_trading_date/trading_date`、`setup_type/profile`、`action_value` 或 `policy_state`、`confidence`、`validity_window`、`evidence_scope`、`excluded_reason` | 解释因果、冲突、反事实、不确定性和未来适用条件 | 修改当天合约、成交、结算、PnL；直接给 Trader 执行规则 |

落地硬规则：

1. 分析师 LLM 输出必须能生成 `action_evidence_contract`；缺少方向、机会状态、触发、失效边界或数据说明时，必须降级，不能靠自由文本补权。
2. 研究员 LLM 输出必须能生成结构化研究成果；自由文本结论不能被分析师、PM、审计员或交易员直接消费。
3. 提示词可以鼓励充分推理，但必须要求模型把结论写入结构化字段。
4. 解析器不能从自由文本中猜手数、动作、rank、资金理由或交易权限。
5. 新增 LLM 输出字段前，必须先登记字段语义，再补提示词检查和结构测试。

---

**智能体内部转换区：以下章节按工作流顺序排列。每个智能体章节只规定该智能体内部怎样转换输入，不重新规定上下游职责。**

## 四、分析师内部机制

适用智能体：技术面分析师、基本面分析师、期货新闻面分析师。

本文中的 `setup` 指一次可被交易系统识别的机会形态或交易条件组合，不等于已经可以成交。它通常包含方向、驱动原因、入场触发、失效边界和适用窗口。例如：BU 沥青盘前出现基本面库存下降、技术面价格接近上方突破位、新闻无反向冲击，这可以形成“多头突破 setup”。如果盘前证据完整但入场触发尚未成立，它应成为 `watch_for_trigger` 条件触发候选，并由 PM 写入需要盘中确认的 `final_action_contract`；开盘后只有价格真正突破并满足合约触发条件，Trader 才能执行成交。

### 4.1 共同转换规则

```text
盘前可见数据
-> LLM 专业推理
-> action_evidence_contract
```

分析师可以调用 LLM 做多维信息理解、冲突分析、反事实推理、不确定性判断和价格走势预测解释；但正式输出只能是结构化预测证据，不能是手数、仓位、保证金、排名或最终交易动作。

| LLM 推理内容 | 必须落地字段 | 不能落地为 |
|---|---|---|
| 方向判断 | `signal`、`trend_direction`、`direction_reason` | 手数、仓位 |
| 机会形态 | `setup_type`、`setup_quality_ok`、`setup_quality_reason` | 最终交易动作 |
| 当前触发是否成立 | `trigger_valid`、`current_trigger_confirmed` | 自由文本触发权限 |
| 等待触发 | `opportunity_state=watch_for_trigger`、`entry_trigger` | 直接成交 |
| 失效边界 | `invalidation_present`、`invalidation_condition` | 无边界开仓 |
| 证据冲突 | `conflict_analysis`、`conflicting_evidence` | 强行给方向 |
| 数据缺口 | `data_usage_summary`、`missing_data`、`data_quality` | 伪造证据 |
| 不确定性 | `uncertainty`、`confidence` | 交易授权 |

### 4.2 三类分析师差异

| 分析师 | 内部推理重点 | 输出侧重点 |
|---|---|---|
| 技术面分析师 | 价格形态、趋势、位置、波动、支撑阻力、入场触发、失效位 | `entry_trigger`、`trigger_valid`、`invalidation_condition`、`technical_timing` |
| 基本面分析师 | 供需、库存、利润、基差、产量、进口、季节性、驱动持续性 | `fundamental_driver`、`driver_direction`、`driver_freshness`、`setup_quality_ok` |
| 期货新闻面分析师 | 新闻事件、政策冲击、突发催化、影响方向、影响窗口、是否已兑现 | `event_type`、`event_direction`、`impact_window`、`catalyst_quality` |

三类分析师都不能输出 `opportunity_score`、`opportunity_rank`、`capital_allocation_reason`、手数、仓位或 `final_action_contract`。

### 4.3 状态流转规则

| 分析结果 | 必须落地为 | 不能落地为 |
|---|---|---|
| 无方向或数据不足 | `opportunity_state=no_opportunity`、`data_usage_summary` | 手数、仓位、交易动作 |
| 有信息但无明确方向 | `signal=Neutral`、`opportunity_state=no_opportunity`、`uncertainty` | 伪造 Bullish/Bearish |
| 有长期方向但无开盘后触发条件 | `opportunity_state=watch_for_trigger` 或 `no_opportunity`，并写明缺少短期触发 | `probe_candidate`、`tradeable_candidate` |
| 有方向但 setup 不完整 | `opportunity_state=no_opportunity` 或弱观察说明，写明缺失项 | `watch_for_trigger` 交易候选 |
| 有方向和 setup，但无明确 `entry_trigger` | `opportunity_state=no_opportunity` 或弱观察说明，写明缺少入场触发 | `watch_for_trigger`、`probe_candidate` |
| setup 完整但无失效边界 | 降级为 `no_opportunity` 或弱观察，写明缺少 `invalidation_condition` | `watch_for_trigger`、`probe_candidate`、`tradeable_candidate` |
| setup 完整、失效边界完整、当前触发未成立 | `opportunity_state=watch_for_trigger`、`trigger_valid=false`、`entry_trigger`、`invalidation_condition` | 直接成交、直接给手数 |
| setup 完整、失效边界完整、当前触发成立但证据偏弱、单一或仍需试探 | `probe_candidate`，并写明 `trigger_valid=true/current_trigger_confirmed=true`、证据弱点 | 直接给 PM 手数 |
| setup 完整、失效边界完整、当前触发成立且多维证据强 | `tradeable_candidate`，并写明 `trigger_valid=true/current_trigger_confirmed=true`、`confidence`、`evidence_quality`、主要支持证据 | 直接给 PM 手数、直接限定为小额试探 |
| 当前触发成立但无失效边界 | 降级为 `watch_for_trigger` 或 `no_opportunity`，写明失效边界缺失 | `probe_candidate`、`tradeable_candidate` |
| 方向冲突 | `conflict_analysis`、`opportunity_state=watch_for_trigger/no_opportunity` | 强行输出单边交易动作 |
| 多维证据冲突但仍有可监控触发 | `opportunity_state=watch_for_trigger`、`conflict_analysis`、`entry_trigger`、`invalidation_condition` | `tradeable_candidate` |
| 数据过旧或缺口明显 | `data_usage_summary`、`uncertainty`、降级后的 `opportunity_state` | 伪造强证据 |
| 本专业研究校准反驳当前 setup | `calibration_conflict`、`uncertainty`、降级后的 `opportunity_state` | 忽略校准直接给强候选 |
| 新闻事件已兑现或影响窗口已过 | `event_priced_in=true` 或影响窗口失效说明，降级后的 `opportunity_state` | 继续作为强催化 |
| 新闻事件方向明确但缺少价格/基本面确认 | `watch_for_trigger`、`entry_trigger`、`impact_window`、`uncertainty` | `tradeable_candidate` |
| 技术触发成立但基本面/新闻强反向 | `conflict_analysis`、`watch_for_trigger` 或 `probe_candidate`，按冲突强度降级 | 无冲突强开 |
| 基本面驱动成立但技术入场位置不好 | `watch_for_trigger`、`entry_trigger`、`invalidation_condition` | 当前直接开仓 |
| 仅有单一弱证据 | `no_opportunity` 或弱观察，写明证据弱点 | 强候选 |
| 数据可信、setup 完整、触发成立、失效边界完整、无重大冲突 | `tradeable_candidate`；若证据强度不足则为 `probe_candidate` | 手数、仓位、最终交易动作 |

### 4.4 强机会与 alpha 放大边界

分析师不是只能输出小额试探机会。分析师必须把强机会落成 `tradeable_candidate`，并把强在哪里结构化写清楚；但正常开仓、加仓和放大仓位只能由 PM 决定。

| 情况 | 分析师必须输出 | PM 才能决定 |
|---|---|---|
| 证据偏弱、单一、冲突未完全解除 | `probe_candidate` 或 `watch_for_trigger` | 是否小额试探或继续观察 |
| setup 完整、触发成立、失效边界完整、多维证据一致 | `tradeable_candidate`、强证据说明 | 是否正常开仓 `open_real` |
| 已有持仓且同向证据继续增强 | 同向 `tradeable_candidate` 或持仓支持证据 | 是否加仓 `add` 或放大 `scale` |
| 研究校准支持该类 setup，但当前触发未成立 | `watch_for_trigger`、触发和失效边界 | 是否写入条件触发合约 |
| 研究校准支持该类 setup，且当前证据、触发、风险均成立 | `tradeable_candidate`、校准引用和当前证据 | 是否提高 rank、正常开仓或放大资金 |

硬规则：

1. `setup_quality_ok=true` 只表示机会形态完整，不表示当前可成交。
2. `trigger_valid=true/current_trigger_confirmed=true` 才表示当前触发成立。
3. `invalidation_present=true` 或明确 `invalidation_condition` 是进入 `watch_for_trigger/probe_candidate/tradeable_candidate` 的必要条件。
4. `watch_for_trigger` 是条件触发候选，不是交易动作，不是手数授权。
5. `tradeable_candidate` 是强可交易候选，不等于 probe；它可以被 PM 转成 `open_real/add/scale`，但分析师不能直接给这些动作。
6. 分析师不能把“长期方向”“事件方向”“历史校准”直接落成当前可成交候选，必须同时说明触发和失效边界。
7. 任何无法结构化落地的自由文本判断，只能进入解释字段，不能进入交易权限字段。

结构化字段不是 LLM 推理上限；结构化字段是 LLM 结果的落地格式。

## 五、信号收集员内部机制

信号收集员不调用 LLM。

信号收集员不是第四个分析师，也不是轻量 PM。它只把已启用分析师的 `action_evidence_contract` 收成一份 PM 可读的 `signal_collection_contract`，不能重新解释自由文本，不能改写分析师机会状态，不能把强候选压成弱候选，也不能把弱证据升级成强候选。

固定转换：

```text
三类分析师 action_evidence_contract
-> 去重、保留来源、对齐方向/触发/失效/冲突/缺失
-> signal_collection_contract
```

如果配置只启用一个或两个分析师，信号收集员只按已启用分析师收集证据；未启用分析师不记为缺失。已启用但没有输出的分析师，必须写入 `missing_evidence=missing_analyst:*`，不能伪造补齐。

### 5.1 必须保留的内容

| 输入内容 | 输出落点 | 规则 |
|---|---|---|
| 分析师原始结构化证据 | `source_contracts` | 保留来源分析师、原 `action_evidence_contract`、来源记录 ID |
| 每条证据的方向和状态 | `evidence_items` | 保留 `side`、`signal`、`opportunity_state`，不能改写 |
| 触发信息 | `trigger_status`、`evidence_items.trigger_*` | 汇总触发状态，但不生成执行权限 |
| setup 信息 | `setup_types`、`evidence_items.setup_*` | 只收集，不判断能否开仓 |
| 失效边界 | `invalidation_summary` | 有则保留，无则记录缺失，不补造 |
| 冲突证据 | `opposing_analysts`、`evidence_conflict_level`、`current_evidence_conflict` | 必须显式保留，不能吞掉 |
| 缺失和数据质量 | `missing_evidence`、`data_quality_flags` | 必须显式保留，不能当作方向证据 |
| 证据强弱摘要 | `evidence_strength` | 只能来自分析师置信度和证据质量，不是 PM score/rank |

### 5.2 聚合状态规则

| 输入组合 | 信号收集员必须输出 | 不能输出 |
|---|---|---|
| 所有已启用分析师无方向 | `dominant_side=flat`、`side_consensus=no_direction` | long/short 主方向 |
| 单个分析师有方向 | `side_consensus=single_analyst_support`，保留该分析师证据 | 多分析师共识 |
| 两个及以上分析师同向 | `side_consensus=multi_analyst_support`、对应 `supporting_analysts` | PM rank、手数 |
| 有反向分析师 | `side_consensus=conflicted`、`opposing_analysts`、`evidence_conflict_level` | 抹掉反向证据 |
| 任一分析师当前触发成立 | `trigger_status=confirmed` | 交易员执行权限 |
| 有触发条件但未确认 | `trigger_status=valid_unconfirmed` 或 `watch_for_trigger` | 当前可成交判断 |
| 有 `tradeable_candidate` 证据 | 原样保留为 `tradeable_candidate` | 降级成 probe 或直接开仓 |
| 有 `probe_candidate` 证据 | 原样保留为 `probe_candidate` | 升级成 `tradeable_candidate` |
| 有 `watch_for_trigger` 证据 | 原样保留触发条件和失效边界 | 转成当前成交候选 |
| 有数据缺口或前视风险标记 | 写入 `data_quality_flags` | 忽略缺口或改成方向证据 |

### 5.3 权限硬规则

允许：

- 保留每条分析师证据；
- 标记方向一致、方向冲突、触发缺失、数据缺口；
- 输出统一证据包；
- 汇总 `dominant_side`、`side_consensus`、`trigger_status`、`evidence_strength`、`evidence_conflict_level`；
- 保留强候选 `tradeable_candidate`，供 PM 后续判断正常交易、加仓或放大。

禁止：

- 评分排序；
- 输出手数；
- 输出交易动作；
- 输出 `final_action_contract`；
- 读取研究库；
- 把历史学习结论混入信号包；
- 把 `watch_for_trigger` 升级为 `probe_candidate/tradeable_candidate`；
- 把 `tradeable_candidate` 降级为 `probe_candidate`；
- 因为某个分析师缺失就伪造证据；
- 因为存在主方向就吞掉反向证据。

信号收集员的输出只能回答“分析师们分别说了什么、方向是否一致、触发是否存在、证据是否冲突、数据是否缺失”。它不能回答“该买几手、是否放大、是否下单”。这些只能由 PM 在 `final_action_contract` 中决定。

## 六、投资组合经理内部机制

PM 不调用 LLM。PM 是唯一 `final_action_contract` 签发者。PM 的职责不是“再次理解文本”，而是把信号收集员的结构化证据、账户/持仓、市场确认、研究记忆和资金风控，确定性转换成唯一交易合约。

PM 内部必须拆成五层：

```text
证据读取层 -> 机会状态层 -> 学习/排序层 -> 资金/风险层 -> 最终合约层
```

### 6.1 输入读取边界

| 输入 | PM 可以做 | PM 不能做 |
|---|---|---|
| `signal_collection_contract` | 读取方向、触发、setup、失效边界、冲突、缺失、证据强弱 | 重新解释分析师自由文本 |
| 账户、持仓、合约、保证金 | 计算当前手数、风险、可用预算 | 伪造成交或结算 |
| `decision_memory_retrieval` 输出 | 读取有效 action-value、profile、剔除原因、学习摘要 | 直接查研究 DB 原始记录 |
| `opportunity_ranking` 输出 | 排序、解释资金优先级 | 让 rank 替代 `target_lots` |
| `position_sizing` 输出 | 计算目标手数建议 | 让 sizing 工具签最终合约 |

PM 只能消费结构化字段。任何未结构化落地的文本，只能作为解释背景，不能成为交易权限。

### 6.2 PM 固定执行顺序

PM 每次生成 `final_action_contract` 必须按以下顺序执行。代码可以拆函数，但不能改变业务顺序。

```text
1. 读取标准输入
2. 硬门控预检
3. 持仓处理通道
4. 新开仓候选处理通道
5. 学习和排序
6. 资金和手数
7. 最终合约签发
8. 合约自检
```

| 顺序 | 阶段 | 对应工具/入口 | 必须做 | 禁止 |
|---|---|---|---|---|
| 1 | 读取标准输入 | `signal_evidence_collection.build_signal_collection_contract`、账户/持仓/行情读取入口 | 只读 `signal_collection_contract`、账户、持仓、合约、市场数据 | 直接查研究 DB；读取上游内部草稿 |
| 2 | 硬门控预检 | `reason_effects.reason_effect_summary`、`hard_risk_rules`、`invalidation_policy` | 先检查未来函数、合约非法、价格异常、保证金硬风险、必需字段缺失、失效边界缺失 | 在硬门控未通过前放大、加仓或释放真实开仓 |
| 3 | 持仓处理通道 | PM 持仓处理函数、`position_lifecycle`、持仓风险规则 | 对已有仓位先判断 `hold/add/scale/reduce/exit`，保护退出和风险处置优先 | 用新开仓规则误杀持仓，或用新开仓信号覆盖退出 |
| 4 | 新开仓候选处理通道 | PM 机会状态函数、`invalidation_policy`、`capital_deployment_policy` | 对无仓或反手后的新机会判断 `no_opportunity/watch_for_trigger/probe_candidate/tradeable_candidate` | 把 `watch_for_trigger` 直接清成普通 `wait/0`，或直接成交 |
| 5 | 学习和排序 | `decision_memory_retrieval.retrieve_pm_memory`、`opportunity_ranking.rank_opportunities` | 只用有效摘要、action-value、剔除原因和 ranking 结果调整优先级 | 让 rank 或学习记录绕过当前证据、失效边界和硬门控 |
| 6 | 资金和手数 | `position_sizing.build_position_sizing_result`、`capital_deployment_policy`、PM 资金规则 | 用资金预算、风险上限、最小可交易单位计算 `target_lots`、`target_position_ratio`、`lots_delta` | 让分析师、信号收集员、审计员或研究员决定手数 |
| 7 | 最终合约签发 | PM `final_action_contract` 构造入口 | PM 统一写 `final_action_contract`，包括动作、手数、触发、失效、资金理由、reason code | 分散写多个交易合约，或让 Trader/Reviewer 补签合约 |
| 8 | 合约自检 | `tools.common.contracts` 合约解析/执行摘要和 PM 自检 | 校验 `lots_delta = target_lots - current_lots`、动作与手数一致、条件触发字段一致 | 带不一致合约进入审计员或交易员 |

顺序硬规则：

1. 持仓处理必须早于新开仓候选；已有仓位的保护、减仓、退出不能被新开仓观察规则覆盖。
2. 硬门控必须早于学习释放；正向 action-value 或 rank 不能释放硬风险。
3. `watch_for_trigger` 的条件触发出口必须早于最终清零；合格条件触发候选不能被普通 `wait/0` 吞掉。
4. 手数计算必须晚于机会状态和学习排序；分析师证据不能直接决定手数。
5. 最终合约自检失败时必须停止保存 PM 推荐，不能把不一致合约交给审计员兜底。

### 6.3 配置参数对应关系

PM 的小额试探、正常交易、放大交易和硬上限必须只读取下表列出的配置段。新增资金、手数或门控参数时，必须先补本表，不能在代码里临时读取散落 YAML。

| 交易强度/控制项 | 对应配置位置 | 主要参数 | 生效边界 |
|---|---|---|---|
| 小额试探 `open_probe` | `src/config/dev.yaml: position_budget_policy` | `probe_margin_ratio`、`probe_margin_max_ratio`、`min_real_trade_margin_ratio` | 只决定 probe 保证金层级；不能突破总保证金硬上限 |
| 小额试探质量门槛 | `src/config/portfolio_policy_catalog.yaml: portfolio_manager.watch_for_trigger_new_entry` | `probe_max_ratio`、`probe_floor_ratio`、`scorecard_probe_min_score`、`single_high_quality_probe_*` | 只允许完整条件机会或当前触发候选进入受控 probe；不能把方向观点变成交易 |
| 正常真实开仓 `open_real` | `src/config/dev.yaml: position_budget_policy` | `normal_trade_margin_ratio`、`normal_trade_margin_max_ratio`、`deployable_margin_ratio`、`deployable_margin_max_ratio` | 只能由 PM 在 `tradeable_candidate`、资金和风险达标后写入同一张 `final_action_contract` |
| 正常开仓质量门槛 | `src/config/portfolio_policy_catalog.yaml: portfolio_manager.quality_aware_fusion.opportunity_scorecard` | `tradeable_threshold`、`deployable_threshold`、`min_tradeable_candidate_setup_quality`、`min_deployable_setup_quality` | 只影响 PM 排序和仓位层级；不能绕过失效边界或审计 |
| 放大交易 `scale/add` | `src/config/dev.yaml: capital_utilization_control` | `strong_opportunity_target_margin_ratio_*`、`max_margin_ratio_after_scaling`、`exceptional_validated_*` | 只向强机会、正向学习、强确认和止损/失效边界同时达标的候选释放 |
| 正向学习释放 | `src/config/portfolio_policy_catalog.yaml: portfolio_manager.alpha_setup_ev_fusion` | `positive_expectancy_multiplier`、`min_action_value_samples`、`min_action_value_confidence`、`require_tradeable_support_for_release`、`require_invalidation_for_release` | 只能提高 PM 优先级或释放仓位层级；不能单独生成动作、手数或交易权限 |
| 条件触发候选 | `src/config/portfolio_policy_catalog.yaml: portfolio_manager.watch_for_trigger_new_entry` | `semantic_role`、`requires_final_contract_authority`、`allow_probe`、`probe_max_ratio`、`probe_floor_ratio` | 只允许 PM 写入需要盘中确认的条件触发合约；Trader 未触发不得成交 |
| 失效边界控制 | `src/config/portfolio_policy_catalog.yaml: portfolio_manager.holding_rebalance_control.position_lifecycle` | `require_pretrade_invalidation_for_new_entry`、`missing_invalidation_cap_multiplier`、`missing_invalidation_probe_max_ratio` | 新开/加仓必须有失效边界；缺失时只能降级或阻断，不能正常开仓 |
| 硬资金上限 | `src/config/dev.yaml` | `max_total_margin_ratio`、`position_budget_policy.hard_max_total_margin_ratio`、`position_budget_policy.max_single_ticker_margin_ratio` | 任何学习、rank、释放、probe、scale 都不能突破 |
| 回撤和账户风险 | `src/config/dev.yaml: drawdown_control / risk_control / net_exposure_control` | `hard_drawdown`、`warning_drawdown`、`position_scaling`、`max_net_exposure`、`strong_opportunity_max_net_exposure` | 只作为账户级风险边界；不能创建交易机会 |
| 市场确认和冲突降级 | `src/config/portfolio_policy_catalog.yaml: market_confirmation` | `min_confirmation_score_for_new_entry`、`quality_gate_cap_multiplier`、`conflict_cap_multiplier`、`data_gap_cap_multiplier` | 只确认、降级或阻断当前机会；不能替代分析师 setup 或 PM 合约 |
| 审计质量门槛 | `src/config/portfolio_policy_catalog.yaml: trade_auditor` | `quality_gate.*`、`cold_start.*`、`attribution_feedback.*` | 只影响审计裁决；审计员不能直接改 PM 手数 |

配置硬规则：

1. `dev.yaml` 中的资金保护区参数是硬边界，学习机制、配置整理和门控优化不得自动改值。
2. `portfolio_policy_catalog.yaml` 只定义 PM 如何解释证据、学习、市场确认和质量门槛；最终交易事实仍只能来自 `final_action_contract`。
3. `learning_policy_catalog.yaml` 只定义研究学习如何生成和保留；不能直接改变当日 PM 手数、Trader 执行或 Accountant 结算。
4. `execution_*_catalog.yaml` 只定义手续费、滑点和退出执行事实；不能产生 PM 交易动作。
5. 任何 YAML 参数如果会改变交易强度，必须落到 probe、normal、scale、hard cap、diagnostic 中的一类，不能成为第六套隐性门控。

### 6.4 新开仓机会状态流转

| 输入状态 | 必要条件 | 输出 |
|---|---|---|
| `no_opportunity` | 无有效方向或无 setup | `wait/0` |
| `watch_for_trigger` | 无 setup、无 `entry_trigger` 或无失效边界 | `wait/0`，并写明缺失项 |
| `watch_for_trigger` | setup 完整、失效边界完整、方向明确、风险预算可承受、当前触发未确认 | 条件触发合约 |
| `probe_candidate` | 当前触发确认、失效边界完整、风险预算可承受 | `open_probe` |
| `tradeable_candidate` | 当前证据强、失效边界完整、资金和风险可承受 | `open_real`；若资金或风险只允许试探，则 `open_probe` |
| `tradeable_candidate` | 当前证据强、历史同类 action-value 为正、rank 靠前、资金和风险可承受 | `open_real`，并提高资金优先级 |
| `tradeable_candidate` | 当前证据极强、历史同类 action-value 为正且样本质量达标、rank 靠前、组合仍有可用风险预算 | 放大新开仓目标手数；仍写入同一张 `final_action_contract` |
| `tradeable_candidate` | 当前证据强但组合资金已接近硬上限、单品种风险过高或冲突未完全解除 | `open_probe` 或较小 `open_real`，并写明缩手数原因 |
| hard block | 越权、未来信息、价格异常、保证金硬风险、合约非法 | `wait/0` 或风险处置 |
| negative learning block | 负向 action-value、重复亏损且无新证据 | `wait/0` 或降级观察 |

新开仓放大不是第二套交易动作。它只表示 PM 在同一张 `final_action_contract` 中，把 `target_lots/target_position_ratio` 提高到高质量机会应有的资金层级；最终仍必须满足保证金硬上限、单品种风险、失效边界、审计通过和 `lots_delta` 一致性。

`watch_for_trigger` 的语义固定为：

```text
不能直接成交；
可以在条件满足时进入 final_action_contract；
合约必须写 requires_intraday_confirmation=true；
合约必须写 can_execute_without_intraday_trigger=false；
Trader 只能在盘中触发后成交。
```

`pm_watch_for_trigger_probe_cap` 只能表示：

```text
watch_for_trigger 候选被压成受控观察/条件触发候选。
```

它不能同时表示“候选”和“阻断”。若要阻断，必须由明确硬原因或缺失条件负责，例如无 setup、无失效边界、负向学习、保证金硬风险。

### 6.5 持仓状态流转

| 输入状态 | 输出 |
|---|---|
| 持仓方向仍被证据支持，未触发退出 | `hold` |
| 持仓方向继续被强证据支持，盈利/风险状态允许，PM 评分和资金预算支持 | `add` 或 `scale` |
| 持仓盈利但回吐风险升高 | `reduce` 或保护性 `exit` |
| 持仓亏损且失效条件成立 | `exit` |
| 持仓亏损但同向证据仍强、失效边界未触发 | `hold` 或部分 `reduce`，必须写明再验证理由 |
| 反向证据成立且风险允许 | 先 `exit`，再按新开仓规则判断是否反手 |
| 换月、强平、临近交割 | 运营或风险事件，不写成 alpha 开仓学习 |

持仓处理不能被新开仓规则误杀。开仓、持仓、加仓、减仓、退出必须分不同处理通道判断。

### 6.6 最终动作一致性

| 手数变化 | `final_action` |
|---|---|
| `current_lots == target_lots == 0` | `wait` |
| `current_lots == target_lots != 0` | `hold` |
| `current_lots == 0` 且 `target_lots != 0`，`authority_type=real_budget_entry` | `open_real` |
| `current_lots == 0` 且 `target_lots != 0`，非真实预算授权 | `open_probe` |
| 同方向且 `abs(target_lots) > abs(current_lots)` | `add` 或 `scale` |
| 同方向且 `abs(target_lots) < abs(current_lots)` | `reduce` |
| `target_lots == 0` 且当前有仓 | `exit` |
| 目标方向反转 | 先 `exit`，再按新开仓规则决定是否反手 |

`final_action_contract` 必须同时写清 `current_lots`、`target_lots`、`lots_delta`、`target_position_ratio`、`final_action`、`reason_codes`、执行触发字段和审计所需资金理由。`lots_delta` 必须等于 `target_lots - current_lots`。

### 6.7 PM 门控原则

硬门控可以阻断：

- 未来函数；
- 合约非法；
- 价格异常；
- 保证金硬风险；
- 缺失失效边界；
- 审计前必需字段缺失；
- 明确负向学习且无新证据；
- 交易所或运营规则禁止。

软门控只能：

- 降低 rank；
- 缩手数；
- 转 `watch_for_trigger`；
- 转条件触发合约；
- 要求盘中确认；
- 降低资金优先级；
- 从 `open_real` 降为 `open_probe`。

软门控不能层层叠加后把所有机会变成 `wait/0`，除非最终存在明确硬原因。每个软门控必须有正向出口：试探、条件触发、缩手数、等待确认、保留持仓或退出。

### 6.8 PM 禁止事项

PM 不能：

- 调 LLM；
- 绕过 `decision_memory_retrieval` 直接读研究 DB；
- 把 `signal_collection_contract` 当交易合约；
- 让 `opportunity_rank` 替代 `target_lots`；
- 签第二套交易计划；
- 跳过审计员；
- 让学习记忆单独创造交易权限；
- 把无触发、无失效边界的机会写成可成交合约。

## 七、审计员内部机制

审计员不调用 LLM。审计员只审 PM 已签出的 `final_action_contract` 是否能被系统合法执行，不评价策略是否赚钱，也不消费研究库改变交易权限。

固定转换：

```text
final_action_contract + 账户/持仓/保证金/数据质量/硬风险
-> audit_verdict
```

### 7.1 审计状态流转

| 输入情况 | 审计输出 | 边界 |
|---|---|---|
| 合约字段完整、手数一致、保证金安全、价格/数据有效 | approve / allow | 允许进入 Trader |
| 缺少必需字段、`lots_delta` 不一致、无效合约 | block | 不改合约，只给原因 |
| 保证金超过硬上限、账户资金不足、价格异常 | block | 不创建替代交易 |
| 风险较高但未触发硬风险 | approve_with_limit / probe_only_allowed / reduce_only_allowed | 只限制 PM 合约可执行范围，不自行改方向或给手数 |
| PM 合约为退出、减仓或风险处置 | approve / reduce_only_allowed / block | 只审合法性和风险，不生成新减仓手数 |
| 数据质量不足以执行 | block 或 require_review | 不能补造行情 |

### 7.2 审计员可以输出

- 审计通过/拒绝/降级裁决；
- 硬风险原因；
- 数据质量原因；
- 保证金、持仓、合约一致性检查结果；
- 审计 payload。

### 7.3 审计员禁止事项

审计员不能：

- 改方向；
- 新建合约；
- 直接改 `target_lots`；
- 直接消费研究库；
- 用研究记忆改变交易权限；
- 用收益好坏判断是否允许交易；
- 把软风险当硬风险无限叠加；
- 代替 PM 做资金部署。

审计员只能让不合法或风险越界的合约停下，不能把一个没有 PM 授权的机会变成交易。
审计员不能直接改 `target_lots`。如果审计裁决要求降级、只允许试探或只允许减仓，必须由 PM 已签合约的已有字段承接，或由 PM 重新签发合约；交易员不能执行审计员临时给出的新手数。

## 八、交易员内部机制

交易员不调用 LLM。交易员只执行审计通过的 `final_action_contract`，不能读取研究库，不能用 PM 的排名、资金解释或学习说明下单。

固定转换：

```text
审计通过的 final_action_contract
-> 盘中触发检查
-> execution_result / execution_learning_trace
```

`execution_learning_trace` 指交易员在执行阶段留下的结构化执行学习线索，用来告诉研究员“这张合约为什么成交、为什么没成交、触发条件是否合理、成交方式是否有改进空间”。它不是交易权限，不能改变当天合约、方向或手数。例子：条件触发单当天未突破入场价，trace 写 `trigger_checked=true`、`trigger_fired=false`、`no_trade_reason=intraday_trigger_not_met`；如果突破后成交，trace 写 `trigger_fired=true`、触发原因、成交价格和滑点信息，供研究员未来评估该触发规则是否有效。

### 8.1 执行状态流转

| 合约状态 | Trader 行为 | 输出 |
|---|---|---|
| `final_action=wait/hold` 且 `lots_delta=0` | 不下单 | no trade fact |
| `requires_intraday_confirmation=true` | 只监控触发；触发后按合约执行，未触发不成交 | trigger checked / executed or not triggered |
| `can_execute_without_intraday_trigger=true` | 按合约允许直接执行 | execution_result |
| `open/open_probe/open_real/add/scale/reduce/exit` 且审计通过 | 按合约方向和 `lots_delta` 执行 | 成交、未成交或部分执行事实 |
| 行情缺失、价格异常、合约不可交易 | 不成交 | execution_block_reason |
| 到达日内收尾仍未触发 | 记为未触发，不成交 | intraday_trigger_not_met |

### 8.2 交易员必须写清

- 执行状态；
- 触发是否检查；
- 触发是否成立；
- 成交/未成交/部分成交；
- 成交价格、数量、手续费输入；
- 未成交原因；
- `execution_learning_trace`，供研究员未来学习。

### 8.3 交易员禁止事项

交易员不能：

- 读取研究库；
- 读取 action-value 下单；
- 用 rank 或资金理由下单；
- 改 PM 方向；
- 改 PM 目标手数；
- 把未触发条件单强行成交；
- 把完整 PM 合约复制成自己的执行事实；
- 放宽 `requires_intraday_confirmation`；
- 用执行学习当场修改合约。

交易员的正向出口是“按合约触发后成交”。交易员的负向出口是“未触发、被阻断、无行情或执行失败，并写明原因”。未触发不是策略错误，也不能被交易员改成成交。

## 九、会计师内部机制

会计师不调用 LLM。会计师只根据交易员成交事实、收盘结算价、手续费、保证金参数和持仓状态，生成日结算事实。

固定转换：

```text
成交事实 + 结算价 + 费用 + 保证金参数
-> daily_settlement
```

### 9.1 结算状态流转

| 输入情况 | 会计师输出 |
|---|---|
| 当日有成交 | 更新成交后持仓、手续费、保证金、当日 PnL、权益 |
| 当日无成交但有持仓 | 按结算价更新持仓盯市、保证金和权益 |
| 当日无成交且无持仓 | 写完整零交易结算事实 |
| 缺少结算价或合约参数 | hard error，不补造结算 |
| 强平、换月、运营处置 | 写运营/风险结算事实，不写成 alpha 信号 |

### 9.2 会计师可以写

- `daily_settlement`；
- 当日 PnL；
- 手续费；
- 保证金；
- 权益；
- 持仓事实；
- 强平或换月运营事实。

### 9.3 会计师禁止事项

会计师不能：

- 用学习改账；
- 用 LLM 调账；
- 修改交易员成交事实；
- 生成交易动作；
- 根据策略好坏改 PnL；
- 把未成交机会写成成交；
- 把研究结论写入结算表。

会计师只回答“今天账怎么算”，不能回答“明天该怎么交易”。

## 十、复盘员内部机制

复盘员不调用 LLM。复盘员只在 Phase1、Phase2、Phase3 事实完成后做确定性验收、交易日志和事实归因。复盘员不写未来学习，研究学习由研究员入口单独运行。

固定转换：

```text
推荐 + 合约 + 审计 + 成交 + 结算 + 阶段状态
-> Phase4 验收 + 完整交易日志 + 事实归因 + 研究输入材料
```

### 10.1 复盘状态流转

| 输入情况 | 复盘员输出 |
|---|---|
| 四阶段事实完整且一致 | Phase4 completed，写完整交易日志 |
| Phase1 合约缺失或字段不一致 | Phase4 failed，写缺失原因 |
| Phase2 执行缺失或与合约不一致 | Phase4 failed 或 warning，写执行归因 |
| Phase3 结算缺失或账务不一致 | Phase4 failed，写结算归因 |
| 条件单未触发 | 写未触发事实，不改成失败交易 |
| 交易被审计阻断 | 写阻断事实和审计原因，不评价收益 |
| 有研究痕迹 | 只读展示历史学习快照，不写研究状态 |

### 10.2 复盘员可以输出

- 阶段完成状态；
- 完整交易日志；
- 合约、审计、执行、结算的一致性归因；
- 未成交原因；
- 研究员可消费的事实材料；
- 只读历史学习快照。

### 10.3 复盘员禁止事项

复盘员不能：

- 写 action-value；
- 写 strategy memory；
- 写 adaptive policy state；
- 调 researcher；
- 调 LLM；
- 修改交易动作；
- 修改成交事实；
- 修改结算；
- 把复盘判断写成未来交易权限。

复盘员的职责是“把当天事实说清楚”，不是“学习怎么改策略”。

## 十一、研究员内部机制

研究员可以按配置调用 LLM，但只能在 Phase4 验证后的事实底座上运行。研究员输出结构化研究信息，供未来交易日由分析师或 PM 直接/间接使用。

固定转换：

```text
Phase4 后事实
-> LLM/确定性研究归因
-> 结构化研究成果
-> 未来交易日使用
```

### 11.1 研究员可以推理

- 真实交易成败；
- 未成交、未触发和错过机会；
- 条件触发是否合理；
- setup 生命周期；
- 执行方式和成交质量；
- 分析师证据是否噪音；
- PM 排序、资金部署和手数计算是否合理；
- 反事实机会和未交易机会。

LLM 推理可以充分展开，但必须落成结构化研究成果。自由文本只能解释原因，不能成为下游直接消费的研究结论。

### 11.2 研究输出分域

| 输出域 | 未来消费者 | 用途 | 边界 |
|---|---|---|---|
| 分析师校准类研究 | 分析师 | 改善未来证据解释、触发质量、失效边界 | 不能生成手数或动作 |
| 交易决策类 action-value | PM 经 `decision_memory_retrieval` | 改善未来评分、排序、仓位 | 不能绕过 PM 合约 |
| setup 样本与 profile | 分析师读取校准摘要；PM 经 `decision_memory_retrieval` 消费交易决策摘要 | 判断同类 setup 生命周期、胜率、亏损边界、适用窗口 | 不是品种黑名单，不能跨作用域硬套 |
| 未交易/错过机会研究 | 分析师校准；PM 经工具消费 | 判断未入选、未触发、错过机会是否应提高未来 rank 或触发敏感度 | 只能影响未来，不把影子收益写成真实收益 |
| neutral 观察研究 | 分析师 | 区分合理中性、证据缺口、错过机会风险、观察触发条件 | 不能把 neutral 直接变成交易动作 |
| 执行学习 | PM 写入未来执行字段后由 Trader 执行 | 改善触发、成交方式、追价、未成交处理和执行 profile | Trader 不能直接读研究库 |
| 持仓/退出学习 | PM 经 `decision_memory_retrieval` | 改善 hold、reduce、exit、保护盈利、止损和反手判断 | 历史 hold/exit 不能直接证明新开仓可行 |
| 排序偏好研究 | PM 经 `opportunity_ranking` | 改善高低 rank、资金优先级、候选入选顺序 | rank 不是交易权限，不能替代 `target_lots` |
| 资金部署反馈 | PM 和机制审计 | 判断资金是否放到更强机会、是否长期停留 probe、是否该放大 alpha | 不能越过保证金硬上限或审计员 |
| adaptive policy state | PM 经 `decision_memory_retrieval`；分析师只读安全校准摘要 | 记录 protect/cap/probe/watchlist 等未来策略状态 | 必须被当日证据、失效边界、资金和审计再验证 |
| 运营/风控事件研究 | PM、会计师、复盘员、机制审计按职责读取 | 记录换月、强平、保证金风险、合约切换成本 | 不能写成策略 alpha 正负样本 |
| 研究反馈 / 机制反馈 | 开发者和机制审计 | 判断学习机制是否接通、是否进入 PM、是否改善排序和资金部署 | 不能创建交易权限，不能评价当天合约合法性 |

### 11.3 研究员禁止事项

研究员不能：

- 改当天合约；
- 改当天成交；
- 改当天结算；
- 给 Trader 直接交易权限；
- 绕过 PM 给手数或动作；
- 用自由文本作为下游研究结论；
- 在 Phase4 未完成前写未来学习；
- 把运营事件误写成 alpha 开仓学习。

研究员的正向出口是“未来可用的结构化研究信息”。研究员没有当天交易出口。

## 十二、协议管理员内部机制

协议管理员不调用 LLM。协议管理员不是交易智能体，不生成业务事实，只做只读治理。

固定转换：

```text
代码、字段、配置、提示词、DB schema、artifact、测试
-> 边界审计 / 契约覆盖 / 系统不变量 / 回测前验收
```

### 12.1 控制治理状态流转

| 输入情况 | 输出 |
|---|---|
| 字段、schema、artifact、权限均符合契约 | pass |
| DB schema 缺字段或日期字段不一致 | hard fail |
| 智能体越权调用 LLM、读研究库或写业务事实 | hard fail |
| artifact 跨阶段复制完整上游对象 | hard fail |
| 配置、提示词、测试和机制文档不一致 | hard fail 或 warning |
| 策略收益差但机制未断链 | diagnostics，不停止基础链路 |

### 12.2 协议管理员只能检查

- 字段是否统一；
- 智能体是否越权；
- artifact 是否越界；
- schema 是否匹配；
- 测试是否覆盖；
- 非策略 hard error 是否存在；
- 回测前硬数据是否足够。

### 12.3 协议管理员禁止事项

协议管理员不能：

- 生成业务事实；
- 写业务表；
- 创建交易权限；
- 修改合约；
- 修改成交或结算；
- 用收益好坏改审计规则；
- 猜测 DB 字段；
- 把 warning 当作策略门控。

协议管理员的职责是“证明系统边界没坏”，不是“替策略做交易判断”。

---

**开发落地区：以下规则用于后续改代码、改字段、改配置、改提示词和补测试。**

## 十三、状态流转表的开发要求

以后修改任一智能体内部逻辑，必须满足：

1. 先确认该智能体是 LLM 智能体还是确定性规则智能体。
2. 非 LLM 智能体必须更新状态流转表或对应测试，不能只加局部 if。
3. LLM 智能体必须更新结构化输出契约或提示词，不得让自由文本成为权限。
4. 同一个 reason code 不得承担候选和阻断两种相反含义。
5. 每个软门控必须说明正向出口：降级、条件触发、缩手数、补证据或等待。
6. 每个硬门控必须说明阻断理由和恢复条件。
7. 新增测试必须覆盖正向路径和负向路径。
8. 修改内部转换机制时，必须先对照 `docs/unified_field_semantics.md`，确认字段含义、产生者、消费者和可跨阶段范围一致。
9. 不能为了局部规则复用含义不匹配的旧字段或 reason code；如果确实需要新增字段，必须先更新 `docs/unified_field_semantics.md`，再更新提示词、配置、测试和本文。
10. 功能语义必须前后一致：同一字段不能在分析师侧表示机会状态，在 PM 侧又表示最终动作；同一 reason code 不能在一个函数中表示候选，在另一个函数中表示阻断。

### 13.1 测试映射表

所有测试逻辑必须放在 `src/tests/test_*.py`；运行编排脚本只放在 `src/run/pre_backtest_test.py` 和 `src/run/backtest_daily_test.py`。内部转换、字段边界、权限边界和固定公式只在回测前检测；每日回测后只检测真实运行产物、系统不变量和机制接通情况。新增内部状态流转或边界规则时，必须补下表对应测试，不能只改代码。

| 关键规则 | 覆盖测试文件 | 回测前总入口 | 每日回测后总入口 |
|---|---|---|---|
| 事实入口、artifact/payload 边界、业务模块不能绕写核心事实 | `src/tests/test_fact_entry_boundaries.py` | `pre_backtest_test.py` | 不进入 |
| 合格 `watch_for_trigger` 必须进入条件触发合约，不能被清成普通 `wait/0` | `src/tests/test_pm_watch_for_trigger_release.py` | `pre_backtest_test.py` | 不进入 |
| PM、Trader、Reviewer、Audit 的合约读取和执行摘要边界 | `src/tests/test_fact_entry_boundaries.py`、`src/tests/test_system_invariant_audit.py` | `pre_backtest_test.py` | `backtest_daily_test.py` 只检测真实产物 |
| Accountant 手续费、保证金、权益、PnL 固定公式 | `src/tests/test_accountant_settlement_formulas.py` | `pre_backtest_test.py` | 不进入 |
| 契约覆盖：producer、consumer、audit、test、文档、字段、配置、提示词对齐 | `src/tests/test_contract_coverage_audit.py` | `pre_backtest_test.py` | 不进入 |
| 回测前 DB schema、硬数据、配置和环境验收 | `src/tests/test_pre_backtest_acceptance.py` | `pre_backtest_test.py` | 不进入 |
| 协议管理员能力卡、LLM 边界、planner 封存、工具权限 | `src/tests/test_protocol_governor.py` | `pre_backtest_test.py` | 不进入 |
| 每日系统不变量：字段越界、交易事实错位、artifact 污染、条件触发执行一致性 | `src/tests/test_system_invariant_audit.py` | 不进入 | `backtest_daily_test.py` |
| 机制有效性：学习链路、PM 学习消费、研究反馈是否接通 | `src/tests/test_mechanism_effectiveness_audit.py` | 不进入 | `backtest_daily_test.py` |
| Reviewer 不写学习、Researcher 只写未来学习 | `src/tests/test_reviewer_learning.py`、`src/tests/test_fact_entry_boundaries.py` | 相关单测按需运行 | 由日后新增时纳入 |
| 统一字段迁移和旧字段残留 | `src/tests/test_unified_field_migration.py`、`src/tests/test_evaluation_unified_semantics.py` | 相关单测按需运行 | 不进入 |
| 市场确认和硬交易规则 | `src/tests/test_market_confirmation.py`、`src/tests/test_futures_market_rules.py` | 相关单测按需运行 | 不进入 |

当前固定编排：

```text
src/run/pre_backtest_test.py
-> test_fact_entry_boundaries
-> test_accountant_settlement_formulas
-> test_pm_watch_for_trigger_release
-> test_contract_coverage_audit
-> test_pre_backtest_acceptance
-> test_protocol_governor
-> protocol preflight
-> contract_coverage_audit
-> pre_backtest_acceptance

src/run/backtest_daily_test.py
-> test_system_invariant_audit
-> test_mechanism_effectiveness_audit
-> system_invariant_audit
-> mechanism_effectiveness_audit
```

测试硬规则：

1. 新增测试文件必须命名为 `src/tests/test_*.py`。
2. 回测前必须跑的测试，加入 `src/run/pre_backtest_test.py`。
3. 每个交易日后必须跑的测试，加入 `src/run/backtest_daily_test.py`。
4. 测试文件负责断言规则；运行脚本只负责编排，不能写业务测试逻辑。
5. 任何会影响交易状态流转、reason code 语义、配置门控或 LLM 输出落地的修改，必须同时更新本映射表。
