# AgentQuant 智能体内部转换机制

更新时间：2026-06-27

本文定义 AgentQuant 各智能体内部如何把输入转换成正式输出。`mechanism_multiagents.md` 规定“谁负责什么、上下游怎么走”；本文规定“智能体内部怎样转，哪些状态必须落到什么输出”。本文不新增交易字段，不替代 `matrix_field_semantics.md`，不改变固定工作流。

本文作用：给开发和审查代码时使用，专门约束每个智能体内部转换机制，防止输入已经正确、字段已经统一，但智能体内部规则把状态转错、写错或层层门控压死交易。本文不评价收益，不新增交易权限，不限制 LLM 的推理过程；它只规定 LLM 推理结果和确定性规则结果如何落到正式结构化输出。

## 文档导航

开发时按以下顺序使用本文：

| 开发任务 | 先看 | 再看 | 最后确认 |
|---|---|---|---|
| 修改任一智能体内部规则 | 一、二、三 | 对应智能体章节 | 十三 |
| 修改 PM 状态流转、资金或手数 | 二、三 | 六 | 十三 |
| 修改 LLM 提示词或解析器 | 三、四、十一 | `matrix_field_semantics.md` | 十三 |
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

交易生命周期记忆读取固定规则：

| 最终合约生命周期 | PM 必须读取的记忆 | `side` 的含义 |
|---|---|---|
| 新开仓 `open/open_probe/open_real` | open lane | `target_side` |
| 加仓/扩大 `add/scale/increase` | add/scale/increase/open 与 hold lane | `target_side` 与 `current_position_side` |
| 持仓 `hold` | hold lane，必要时 exit/reduce 作为审计背景 | `current_position_side` |
| 减仓 `reduce/trim` | reduce/exit/hold lane | `current_position_side` |
| 退出 `exit/close/risk_exit` | exit/reduce/hold lane | `current_position_side` |
| 条件监控 `conditional_probe/watch_trigger` | conditional_monitor lane | `trigger_side` |

### 2.2 reason code 语义表

`reason_codes` 只解释状态流转原因，不能代替状态、动作或手数。每个 reason code 必须只属于一类；同一个 code 不能前面表示“候选”，后面又表示“阻断”。新增或修改 reason code 时，必须同步到共享分类逻辑和测试。

共享分类逻辑固定为 `src/tools/common/final_action_semantics.py`。各智能体不得在本地维护与它相反的 hard/soft/candidate/release/diagnostic 分类；PM、Auditor、Trader、Reviewer、Researcher 和 Protocol Governor 对同一张 `final_action_contract` 的生命周期解释、记忆读取 lane 和 `memory_side_role` 必须来自这个状态机。

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
10. `real_probe_qualification_not_met` 固定属于软降级，不属于硬阻断；带该 code 的条件监控合约仍必须进入 Trader 盘中检查，未触发也必须写 `futures_intraday_decision`。

## 三、全局内部转换规则

所有智能体必须遵守：

1. 只能读取上游正式输出，不能读取上游内部草稿。
2. 内部草稿不能直接写入下游 payload、DB 或 artifact。
3. 一个 reason code 只能有一个业务含义；如需表达候选、降级、阻断，必须分清语义。
4. 状态流转必须有正向出口，不能只有阻断规则。
5. 硬门控和软门控必须分层：硬门控先判定合法性，软门控再决定降级、条件触发或手数。
6. 非 LLM 智能体不能重新解释自由文本；只能消费结构化字段。
7. LLM 智能体可以充分推理，但最终只能输出结构化证据或结构化研究成果。
8. 跨智能体只允许传递共享校验通过的正式结构化契约；prompt、原始 response、内部推理、中间工作状态、隐藏上下文和未验证工具结果不得持久化、传递或进入日志/异常。

### 3.1 LLM 输出落地检查

LLM 可以自由推理，但提示词、解析器和测试必须保证输出落到结构化字段。自由文本只能解释原因，不能成为下游交易权限。

| LLM 智能体 | 输出契约 | 必须覆盖的结构化字段 | 自由文本允许范围 | 禁止 |
|---|---|---|---|---|
| 技术面分析师 | 唯一 `action_evidence_contract`，其中保真承载 `product_profile_evidence`、`fusion_evidence` | `signal`、`opportunity_state`、`setup_type`、`entry_trigger`、`trigger_valid/current_trigger_confirmed`、`invalidation_present/invalidation_condition`、`confidence`、`data_usage_summary`、`current_evidence_conflict`、`product_profile_id`、`profile_fields_used`、`evidence_strength`、`evidence_freshness`、`technical_false_breakout_risk` | 解释价格形态、触发依据、失效位、品种趋势惯性、波动纪律、假突破风险和不确定性 | 输出手数、仓位、PM rank、资金理由、`final_action_contract` |
| 基本面分析师 | 唯一 `action_evidence_contract`，其中保真承载 `product_profile_evidence`、`fusion_evidence` | `signal`、`opportunity_state`、`setup_type`、`primary_business_driver`、`direction_anchor`、`data_freshness`、`setup_quality_ok`、`invalidation_present/invalidation_condition`、`confidence`、`data_usage_summary`、`product_profile_id`、`confirmation_requirements`、`evidence_strength`、`evidence_freshness`、`fundamental_opposition_strength` | 解释供需、库存、利润、基差、季节性、商品驱动优先级、驱动持续性和反向压制强度 | 输出手数、仓位、交易动作、资金部署 |
| 期货新闻面分析师 | 唯一 `action_evidence_contract`，其中保真承载 `product_profile_evidence`、`fusion_evidence` | `signal`、`opportunity_state`、`event_type`、`direction_anchor`、`impact_window_days`、`evidence_quality`、`entry_trigger`、`invalidation_present/invalidation_condition`、`confidence`、`data_usage_summary`、`product_profile_id`、`news_impact_window`、`one_off_event_risk`、`evidence_decay_risk` | 解释新闻事件、政策冲击、影响窗口、是否已兑现、该品种事件催化价值和一次性冲击风险 | 把新闻方向直接写成交易动作或手数 |
| 研究员 | 结构化研究成果 | `research_domain`、`sample_scope`、`source_trading_date/trading_date`、`setup_type/profile`、`action_value` 或 `policy_state`、`confidence`、`validity_window`、`evidence_scope`、`excluded_reason` | 解释因果、冲突、反事实、不确定性和未来适用条件 | 修改当天合约、成交、结算、PnL；直接给 Trader 执行规则 |

落地硬规则：

1. 分析师 LLM 只生成结构化专业分析结果；学习校准、质量门、时效性和商品差异化 profile 评估完成后，由共享确定性收口工具生成唯一 `action_evidence_contract`。缺少方向、机会状态、触发、失效边界或数据说明时必须降级，不能靠自由文本补权。
2. 研究员 LLM 输出必须能生成结构化研究成果；自由文本结论不能被分析师、PM、审计员或交易员直接消费。
3. 提示词可以鼓励充分推理，但必须要求模型把结论写入结构化字段。
4. 解析器不能从自由文本中猜手数、动作、rank、资金理由或交易权限。
5. 新增 LLM 输出字段前，必须先登记字段语义，再补提示词检查和结构测试。
6. `product_profile_evidence` 是分析层字段。三类分析师必须读取 `analyst_product_price_behavior_profile.py` 生成的商品差异化框架，但只能把它用于证据强调、setup 分类、确认要求、季节窗口和假突破风险识别；不能从 profile 推导手数、保证金、reason code、PM rank 或 `final_action_contract`。
7. `fusion_evidence` 是分析层预测证据字段。三类分析师必须把证据强弱、时效、冲突、确认需求、缺失证据和本专业风险落入该字段；不能从它推导手数、保证金、reason code、PM rank 或 `final_action_contract`。

---

**智能体内部转换区：以下章节按工作流顺序排列。每个智能体章节只规定该智能体内部怎样转换输入，不重新规定上下游职责。**

## 四、分析师内部机制

适用智能体：技术面分析师、基本面分析师、期货新闻面分析师。

本文中的 `setup` 指一次可被交易系统识别的机会形态或交易条件组合，不等于已经可以成交。它通常包含方向、驱动原因、入场触发、失效边界和适用窗口。例如：BU 沥青盘前出现基本面库存下降、技术面价格接近上方突破位、新闻无反向冲击，这可以形成“多头突破 setup”。如果盘前证据完整但入场触发尚未成立，它应成为 `watch_for_trigger` 条件触发候选，并由 PM 写入需要盘中确认的 `final_action_contract`；开盘后只有价格真正突破并满足合约触发条件，Trader 才能执行成交。

### 4.1 共同转换规则

```text
盘前可见数据
-> 技术面分析师以当前可见价格计算市场特征、初始自适应参数和初始 market_regime
-> 技术面分析师读取过去有效的同产品/周期/market_regime contextual rule calibration，有界校准参数并重算最终指标与 technical_context
-> 仅限历史交易日的本专业学习上下文进入提示词
-> product_price_behavior_profile 冷启动分析框架进入提示词
-> 主配置指定的 LLM 生成结构化专业分析
-> 同一批合格学习记录执行确定性信号校准
-> 数据质量、时效性和商品差异化 profile 评估
-> 确定性生成唯一 action_evidence_contract（内含 product_profile_evidence、fusion_evidence）
-> 最终落地校验
```

商品差异化分析协议固定为：三类分析师通过 `src/tools/agent_tools/analysis/analyst_product_price_behavior_profile.py` 读取 `src/config/product_price_behavior_profiles.yaml`。静态 profile 提供冷启动品种分析框架并进入提示词，LLM 返回后再由确定性工具核对 profile 的支持、冲突、缺失和确认要求。三类分析师共同使用动态学习完善 LLM 提示词并在 LLM 返回后校对信号；技术面分析师额外使用经过验证的 contextual rule calibration，对当前产品、短周期和初始 market_regime 对应的技术指标参数执行有界校准。该校准只改变技术分析内部参数，不直接生成方向、机会状态或交易权限。学习记录必须早于当前交易日，不在回测中改写 YAML，也不能单独创造交易机会。

三类分析师的 LLM provider、model、base URL、reasoning effort 和 API key 环境变量只服从主配置 `llm`。分析师不得维护私有模型路由、硬编码模型名或第二套 API 配置；切换主配置后，三类分析师必须共同切换。

基本面和新闻数据不要求每个产品每日都有新增记录。存在可用历史记录时，使用交易日可见的最近有效记录并显式标注时效；确无可用记录时，生成合法、无交易权限、可追溯的 `no_opportunity` 证据，禁止伪造方向、催化或缺失数据。

只有必需盘前市场事实不可用才进入全局中性状态。此时technical、fundamental、commodity_news仍分别通过自己的正式入口生成共享校验通过的中性AEC，不调用LLM；Workflow保存三份信号并取得真实ID后，Signal Collector才可生成唯一SCC。Collector不得代替分析师、生成信号或自创ID。

分析师可以调用 LLM 做多维信息理解、冲突分析、反事实推理、不确定性判断和价格走势预测解释；但正式输出只能是结构化预测证据，不能是手数、仓位、保证金、排名或最终交易动作。最终收口必须重建只含AEC字段的 `AnalystSignal`：metadata保存前仅AEC、Workflow追加ID后仅AEC和真实ID；自由文本justification、LLM路由、内部参数、校准过程、学习检索上下文及report_sections不得跨智能体、持久化或写日志。数据工具请求参数、原始结果、原始异常、本机路径、文件编码、动态权重调整和学习覆盖过程也不得进入AEC或日志；运行失败只记录稳定边界码。LLM结构化调用可以按配置重试，但不得以默认Pydantic对象、默认信号或默认事实结束；重试耗尽必须抛出稳定 `llm_inference_failed:*` 并终止该正式分析入口。分析师报告只能呈现同一份已校验AEC。

| LLM 推理内容 | 必须落地字段 | 不能落地为 |
|---|---|---|
| 方向判断 | `signal`、`side`、`trend_direction`及结构化证据字段 | 自由文本理由、手数、仓位 |
| 机会形态 | `setup_type`、`setup_quality_ok`、`setup_quality_notes` | 最终交易动作 |
| 当前触发是否成立 | `trigger_valid`、`current_trigger_confirmed` | 自由文本触发权限 |
| 等待触发 | `opportunity_state=watch_for_trigger`、`entry_trigger` | 直接成交 |
| 失效边界 | `invalidation_present`、`invalidation_condition` | 无边界开仓 |
| 证据冲突 | `current_evidence_conflict`、`conflicting_factors` | 强行给方向 |
| 证据强弱和时效 | `fusion_evidence.evidence_strength`、`fusion_evidence.evidence_freshness`、`fusion_evidence.evidence_decay_risk` | PM score、rank、手数 |
| 跨专业确认需求 | `fusion_evidence.confirmation_requirements` | Trader 触发权限 |
| 本专业特殊风险 | `technical_false_breakout_risk` / `fundamental_opposition_strength` / `news_impact_window` / `one_off_event_risk` | 审计阻断或交易动作 |
| 数据缺口 | `data_usage_summary`、`missing_evidence` | 伪造证据 |
| 不确定性 | `confidence`、`current_evidence_conflict` | 交易授权 |

### 4.2 三类分析师差异

| 分析师 | 内部推理重点 | 输出侧重点 |
|---|---|---|
| 技术面分析师 | 价格形态、趋势、位置、波动、支撑阻力、入场触发、失效位 | `entry_trigger`、`trigger_valid`、`invalidation_condition`、`timing` |
| 基本面分析师 | 供需、库存、利润、基差、产量、进口、季节性、驱动持续性 | `primary_business_driver`、`direction_anchor`、`data_freshness`、`setup_quality_ok` |
| 期货新闻面分析师 | 新闻事件、政策冲击、突发催化、影响方向、影响窗口、是否已兑现 | `event_type`、`direction_anchor`、`impact_window_days`、`evidence_quality` |

三类分析师都不能输出 `opportunity_score`、`opportunity_rank`、`capital_allocation_reason`、手数、仓位或 `final_action_contract`。

### 4.3 状态流转规则

| 分析结果 | 必须落地为 | 不能落地为 |
|---|---|---|
| 无方向或数据不足 | `opportunity_state=no_opportunity`、`data_usage_summary` | 手数、仓位、交易动作 |
| 有信息但无明确方向 | `signal=Neutral`、`opportunity_state=no_opportunity`、`confidence` 和冲突说明 | 伪造 Bullish/Bearish |
| 有长期方向但无开盘后触发条件 | `opportunity_state=watch_for_trigger` 或 `no_opportunity`，并写明缺少短期触发 | `probe_candidate`、`tradeable_candidate` |
| 有方向但 setup 不完整 | `opportunity_state=no_opportunity` 或弱观察说明，写明缺失项 | `watch_for_trigger` 交易候选 |
| 有方向和 setup，但无明确 `entry_trigger` | `opportunity_state=no_opportunity` 或弱观察说明，写明缺少入场触发 | `watch_for_trigger`、`probe_candidate` |
| setup 完整但无失效边界 | 降级为 `no_opportunity` 或弱观察，写明缺少 `invalidation_condition` | `watch_for_trigger`、`probe_candidate`、`tradeable_candidate` |
| setup 完整、失效边界完整、当前触发未成立 | `opportunity_state=watch_for_trigger`、`trigger_valid=false`、`entry_trigger`、`invalidation_condition` | 直接成交、直接给手数 |
| setup 完整、失效边界完整、当前触发成立但证据偏弱、单一或仍需试探 | `probe_candidate`，并写明 `trigger_valid=true/current_trigger_confirmed=true`、证据弱点 | 直接给 PM 手数 |
| setup 完整、失效边界完整、当前触发成立且多维证据强 | `tradeable_candidate`，并写明 `trigger_valid=true/current_trigger_confirmed=true`、`confidence`、`evidence_quality`、主要支持证据 | 直接给 PM 手数、直接限定为小额试探 |
| 当前触发成立但无失效边界 | 降级为 `watch_for_trigger` 或 `no_opportunity`，写明失效边界缺失 | `probe_candidate`、`tradeable_candidate` |
| 方向冲突 | `current_evidence_conflict`、`opportunity_state=watch_for_trigger/no_opportunity` | 强行输出单边交易动作 |
| 多维证据冲突但仍有可监控触发 | `opportunity_state=watch_for_trigger`、`current_evidence_conflict`、`entry_trigger`、`invalidation_condition` | `tradeable_candidate` |
| 数据过旧或缺口明显 | `data_usage_summary`、`missing_evidence`、降级后的 `opportunity_state` | 伪造强证据 |
| 本专业研究校准反驳当前 setup | `current_evidence_conflict`、`conflicting_factors`、降级后的 `opportunity_state` | 忽略校准直接给强候选 |
| 新闻事件已兑现或影响窗口已过 | `evidence_decay_risk`、`news_impact_window` 和降级后的 `opportunity_state` | 继续作为强催化 |
| 新闻事件方向明确但缺少价格/基本面确认 | `watch_for_trigger`、`entry_trigger`、`impact_window_days`、`confirmation_requirements` | `tradeable_candidate` |
| 技术触发成立但基本面/新闻强反向 | `current_evidence_conflict`、`watch_for_trigger` 或 `probe_candidate`，按冲突强度降级 | 无冲突强开 |
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
-> signal_collection_contract（source_agent="signal_collector"，collector_decision_boundary="no_trade_authority"）
```

如果配置只启用一个或两个分析师，信号收集员只按已启用分析师收集证据；未启用分析师不记为缺失。已启用但没有输出的分析师，必须写入 `missing_evidence=missing_analyst:*`，不能伪造补齐。

### 5.1 必须保留的内容

| 输入内容 | 输出落点 | 规则 |
|---|---|---|
| 分析师原始结构化证据 | `source_contracts` | 保留来源分析师、原 `action_evidence_contract`、`product_profile_evidence`、来源记录 ID |
| 每条证据的方向和状态 | `evidence_items` | 保留 `side`、`signal`、`opportunity_state`，不能改写 |
| 触发信息 | `trigger_status`、`evidence_items.trigger_*` | 汇总触发状态，但不生成执行权限 |
| setup 信息 | `setup_types`、`evidence_items.setup_*` | 只收集，不判断能否开仓 |
| 失效边界 | `invalidation_summary` | 有则保留，无则记录缺失，不补造 |
| 冲突证据 | `opposing_analysts`、`evidence_conflict_level`、`current_evidence_conflict` | 必须显式保留，不能吞掉 |
| 缺失和数据质量 | `missing_evidence`、`data_quality_flags` | 必须显式保留，不能当作方向证据 |
| 证据强弱摘要 | `evidence_strength` | 只能来自分析师置信度和证据质量，不是 PM score/rank |
| 商品差异化 profile 使用痕迹 | `source_contracts.product_profile_evidence`、`evidence_items.product_profile_id` | 只保真传递，不重新解释、不评分、不生成交易动作 |
| 多维融合证据 | `source_contracts.fusion_evidence`、`evidence_items.fusion_evidence`、`evidence_fusion` | 只保真汇总证据强弱、时效、一致性、冲突、确认需求和缺失证据，不生成 PM score/rank、不生成交易动作 |
| 生产者和权限边界 | `source_agent`、`collector_decision_boundary` | 固定为 `signal_collector` 和 `no_trade_authority`，供 PM 入口校验 |

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
- 汇总 `evidence_fusion`、`evidence_strength_by_analyst`、`evidence_freshness_by_analyst`、`cross_analyst_conflicts`、`dominant_opposing_evidence`、`confirmation_requirements`；
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

PM 内部固定为六步：

```text
1. 读取标准输入
2. 判断单品种方向
3. 结合持仓确定交易状态、候选质量和内部生命周期分流
4. 按生命周期消费学习
5. 只有实际增加风险时进入全市场资金 rank 与资金部署，包括从空仓建立新仓和同方向扩大绝对手数的 `add/scale`
6. 原子生成唯一 FuturesRecommendation 与 final_action_contract，并检查最终输出自身
```

### 6.1 输入读取边界

| 输入 | PM 可以做 | PM 不能做 |
|---|---|---|
| `signal_collection_contract` | 只读取 `signal_collector` 已签出的结构化预测证据包，要求 `source_agent="signal_collector"` 且 `collector_decision_boundary="no_trade_authority"` | 在 PM 内重建证据包，或重新解释分析师自由文本 |
| 账户、持仓、合约、保证金 | 计算当前手数、风险、可用预算 | 伪造成交或结算 |
| `decision_memory_retrieval` 输出 | 读取有效 action-value、profile、剔除原因、学习摘要 | 直接查研究 DB 原始记录 |
| `pm_ticker_side_selection` 输出 | 读取单品种方向优先级、候选质量和候选层级提示 | 把单品种方向优先级写成最终全市场 rank |
| `pm_full_market_capital_deployment` 输出 | 只在实际增加风险的路径读取全市场资金 rank、部署结论和 rank trace，包括新开仓和同方向扩大绝对手数的 `add/scale` | 让 rank 替代 `target_lots`，给 `wait/hold/reduce/exit`、当前反转退出腿或不增加风险的条件监控伪造 rank，或让实际增加风险的 `add/scale` 绕过 rank |
| `pm_position_sizing` 输出 | 计算目标手数建议并交给 PM 第 6 步签约 | 让 sizing 工具签最终合约 |

PM 必须通过 `src/tools/common/evidence_fusion_semantics.py` 把 `signal_collection_contract.evidence_fusion` 转成 `pm_fusion_diagnostics`。PM 只能把该诊断写入 `opportunity_scorecard` 分项和 `final_action_contract.evidence_used.pm_fusion_diagnostics`，并在 `pm_conflict_resolution` 解释主要冲突、反向证据和必要确认。PM 不能因为融合工具存在而调用 LLM、绕过 `decision_memory_retrieval`、跳过资金/风险计算或让融合分项直接生成 `target_lots`。

PM 只能消费结构化字段。任何未结构化落地的文本，只能作为解释背景，不能成为交易权限。

### 6.2 PM 固定执行顺序

PM 每次生成 `final_action_contract` 必须按以下顺序执行。代码可以拆函数，但不能改变业务顺序。

```text
1. 读取标准输入
2. 生成 side_priority / ticker_side_priority
3. 结合 current_lots 形成 candidate_quality / candidate_layer_hint / primary_lifecycle_action_port
4. 按生命周期消费学习
5. 开仓全市场资金 rank 与部署
6. 原子生成唯一 FuturesRecommendation / final_action_contract 并检查最终输出自身
```

| 顺序 | 阶段 | 对应工具/入口 | 必须做 | 禁止 |
|---|---|---|---|---|
| 1 | 读取标准输入 | workflow 已提供的 `signal_collection_contract`、账户/持仓/行情读取入口 | 只读信号收集员正式证据包、账户、持仓、合约、市场数据，并写入同一个 PM 内存状态 | 在 PM 内调用证据包 builder；读取上游内部草稿；生成任何独立输出 |
| 2 | 单品种方向 | `pm_ticker_side_selection`、SCC 方向事实 | 只形成 `side_priority`、`ticker_side_priority`，继续更新同一个 PM 内存状态 | 读取学习、比较持仓、生成生命周期、rank、手数和交易权限 |
| 3 | 持仓与交易状态 | `pm_lifecycle_action_port`、`pm_state_transition`、`current_lots`、Step2 方向结果 | 在内存中比较持仓与代表方向，形成 `candidate_quality`、`candidate_layer_hint`、`primary_lifecycle_action_port` | 改写上游 `opportunity_state`；生成最终动作、目标手数和合约；把 Step3 与 Step6 比较作为失败依据 |
| 4 | 生命周期学习消费 | `decision_memory_retrieval.retrieve_pm_memory`、生命周期学习路由 | 按 canonical family/lane 消费学习，把完整候选学习池、临时路由和拒绝原因留在同一个 PM 内存状态 | 把 Step4 临时路由当最终 `decision_learning_rows`；拿 execution 学习给开仓权限；把原始研究对象写入 artifact |
| 5 | 新增风险全市场 rank 与部署 | `pm_full_market_capital_deployment` | 只处理实际增加风险的 `open/open_probe/open_real/add/scale` 和条件开仓，把唯一全市场 rank、预算和 sizing 事实写回同一个 PM 内存状态 | 让非新增风险合约进入 rank，或让实际增加风险的 `add/scale` 绕过 rank；生成独立 rank/budget/sizing artifact；把 Step3/4 候选字段当最终 rank trace |
| 6 | 最终合约签发与自检 | `pm_contract_builder`、`step6_contract_generation_check`、`pm_contract_self_check`、`FuturesRecommendation` 返回入口 | 从最终 PM 内存状态原子生成唯一 `final_action_contract` 与唯一 `FuturesRecommendation`，按最终动作和手数重新形成学习事实，并检查最终输出自身 | 分散写多个交易合约；读取 Step1–5 早期状态做回溯比较；返回半成品；让 Trader/Reviewer 补签合约 |

顺序硬规则：

1. 第 2 步只判断单品种方向；第 3 步结合持仓形成交易状态和内部生命周期分流；两步都不是最终交易事实。
2. 非新增风险动作走 `1 -> 2 -> 3 -> 4 -> 6`，包括 `wait/hold/reduce/exit`、当前反转退出腿和不增加风险的 `conditional_monitor`；它们不得伪造全市场 rank。
3. 实际增加风险的动作走 `1 -> 2 -> 3 -> 4 -> 5 -> 6`，包括从空仓建立非零仓位的 `open/open_probe/open_real`、同方向且 `abs(target_lots)>abs(current_lots)` 的 `add/scale` 和增加目标仓位的条件开仓；缺 Step5 资金部署事实时不能签出新增风险最终合约。
4. `watch_for_trigger` 的条件触发出口必须由 PM 在唯一合约中写明 `conditional_trigger_authority`、触发条件和失效边界；Trader 未触发不得成交。
5. 手数计算必须晚于生命周期口、候选质量、学习路由和必要的全市场资金部署；分析师证据不能直接决定手数。
6. `pm_six_step_trace.step6_contract_generation_check` 和 `pm_six_step_trace.pm_contract_self_check` 只检查最终输出自身；任一失败时 PM 不返回半成品，不能把非法合约交给审计员修复。

Step1–5 只更新同一个 PM 内存状态，不生成独立评分草稿、排序草稿、资金部署 artifact、签约候选、recommendation 和合约草稿。对外事实入口只有第 6 步返回的唯一 `FuturesRecommendation`；`workflow` 编排层、Auditor 和保存层在 PM 返回后完成审计与物理化。最终合约提交必须是原子动作：凡最终 `final_action_contract` 实际增加风险并出现 `opportunity_rank`，必须同时写入完整 `capital_deployment`、`capital_allocation_reason`、部署前后目标手数、部署手数变化、rank trace 和资金部署 reason code；直接跳过 Step5 的非新增风险合约不得补造 rank，Step5 预算拒绝路径保留真实 rank 和拒绝事实，并恢复 `target_lots=current_lots`。

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
| 账户硬资金上限 | `src/config/dev.yaml` | `max_total_margin_ratio`、`position_budget_policy.hard_max_total_margin_ratio`、`position_budget_policy.max_single_ticker_margin_ratio` | 任何学习、rank、释放、probe、scale 都不能突破；Auditor 与运营风控链负责硬边界，复盘员只记录真实账户风险事实和归因，不做二次合法性裁决 |
| PM 计划预算和复盘诊断 | `src/config/dev.yaml: position_budget_policy / capital_utilization_control / net_exposure_control` | `max_net_exposure`、`strong_opportunity_max_net_exposure`、`target_margin_ratio_*`、`probe_margin_ratio`、`probe_margin_max_ratio`、`normal/deployable/exceptional_margin_ratio*`、`warning_target_margin_ratio_max`、`recovery_*` | 只服务 PM Step5 计划预算、rank/部署和资金层级；真实成交后因条件腿未触发、成交子集、价格变化或滑点产生偏离时，复盘员只能写事实归因/预警，不能作为日终 hard fail |
| 回撤和账户风险 | `src/config/dev.yaml: drawdown_control / risk_control` | `hard_drawdown`、`warning_drawdown`、`position_scaling` | 只作为账户级风险边界或降级依据；不能创建交易机会 |
| 市场确认和冲突降级 | `src/config/portfolio_policy_catalog.yaml: market_confirmation` | `min_confirmation_score_for_new_entry`、`quality_gate_cap_multiplier`、`conflict_cap_multiplier`、`data_gap_cap_multiplier` | 只确认、降级或阻断当前机会；不能替代分析师 setup 或 PM 合约 |
| PM 内部风险门槛 | `src/config/portfolio_policy_catalog.yaml: pm_risk_gate` | `quality_gate.*`、`cold_start.*`、`attribution_feedback.*` | 只影响 PM 签约前的风险降级或阻断；不是独立审计员写入口，不能让审计员直接改 PM 手数 |

配置硬规则：

1. `dev.yaml` 中的账户硬资金上限是硬边界，学习机制、配置整理和门控优化不得自动改值；PM 计划预算参数只能用于计划、部署、预警和复盘归因，不能被复盘员或 PG 当作日终交易违规裁决线。
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
- 在缺 `signal_collection_contract` 时自行重建证据包；
- 绕过 `decision_memory_retrieval` 直接读研究 DB；
- 把 `signal_collection_contract` 当交易合约；
- 让 `opportunity_rank` 替代 `target_lots`；
- 签第二套交易计划；
- 跳过审计员；
- 让学习记忆单独创造交易权限；
- 把无触发、无失效边界的机会写成可成交合约；
- 在 Step1–5 生成 `pm_internal_candidate`、`pm_capital_deployment_decision`、recommendation、合约草稿和独立 artifact。

## 七、审计员内部机制

审计员不调用 LLM。审计员只审 PM 已签出的 `final_action_contract` 是否能被系统合法执行，不评价策略是否赚钱，也不消费研究库改变交易权限。

固定转换：

```text
完整 final_action_contract
+ 账户权益/保证金/保证金比例/risk_status
+ 当前持仓
+ SCC数据质量摘要
+ 具体合约及失效边界事实
+ 主配置硬风控参数
-> audit_verdict
```

### 7.1 审计状态流转

| 输入情况 | 审计输出 | 边界 |
|---|---|---|
| 合约字段完整、动作与手数一致、保证金未超硬上限、合约/失效边界和数据有效 | approve | 允许进入 Trader |
| 缺少必需字段、`lots_delta` 不一致、无效合约 | block | 不改合约，只给原因 |
| 新增风险缺具体合约或失效边界 | block | 不创建替代交易 |
| 目标保证金超过硬上限，或账户处于 `LIQUIDATION` 且合约新增风险 | block | 不创建替代交易 |
| 数据质量为 warning / degraded | approve_with_warning | 只记录软风险，不修改合约 |
| 数据质量为 invalid / hard_fail / future_leak | block | 不能补造行情或放行前视数据 |

### 7.2 审计员可以输出

- `approve`、`approve_with_warning` 或 `block` 裁决；
- 硬风险原因；
- 软风险和数据质量原因；
- 被审合约动作、手数和语义状态摘要；
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
- 代替 PM 做资金部署；
- 复审 PM 的学习消费、证据融合解释、方向、rank、预算部署或 sizing 过程。

审计员只能让不合法或风险越界的合约停下，不能把一个没有 PM 授权的机会变成交易。
审计员不能直接改 `target_lots`，也不输出临时降级手数或第二张交易合约。

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

研究员可以按配置调用 LLM，但只能在 Phase4 completed 且结算事实形成后的事实底座上运行。运行前必须通过正式ID链验证 AEC → SCC → FAC → Auditor → `execution_result` → transaction → settlement。研究员只输出验证后的结构化研究信息，供未来交易日由分析师或 PM 通过正式检索接口直接/间接使用。

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
| 排序偏好研究 | PM 经 `decision_memory_retrieval` 与 Step5 全市场资金部署机制 | 改善高低 rank、资金优先级、候选入选顺序 | rank 不是交易权限，不能替代 `target_lots` |
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
- 保存或传递prompt、原始response、内部推理、隐藏上下文或未验证工具结果；
- 要求每笔交易都形成学习，或要求每次决策都消费学习。

研究员的正向出口是“未来可用的结构化研究信息”，成果允许为空。研究员没有当天交易出口。

## 十二、协议管理员内部机制

协议管理员不调用 LLM。协议管理员不是交易智能体，不生成业务事实，只做只读治理。

协议管理员不存在独立字段体系。所有读取路径、判定字段和报告字段只允许来自 `matrix_field_semantics.md`，所有动作解释只允许来自 `matrix_action_canonical.md`。已有字段不足时，必须先证明必要性并完成矩阵登记，不能先写控制代码再以 `metadata`、`payload` 或私有字典键补语义。

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
- 回测前是否已通过现有只读数据入口确认交易必需的真实行情、合约和时间边界数据足够；基本面与新闻不按每日齐全硬拦。

### 12.3 协议管理员禁止事项

协议管理员不能：

- 生成业务事实；
- 写业务表；
- 创建交易权限；
- 修改合约；
- 修改成交或结算；
- 用收益好坏改审计规则；
- 猜测 DB 字段；
- 使用未在字段矩阵登记的输入或输出字段；
- 在 `metadata`、`payload`、JSON 容器中创建未登记控制字段；
- 维护私有动作集合、字段别名、兼容路径或 reason code 语义；
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
8. 修改内部转换机制时，必须先对照 `docs/matrix_field_semantics.md`，确认字段含义、产生者、消费者和可跨阶段范围一致。
9. 不能为了局部规则复用含义不匹配的旧字段或 reason code；确需新增字段时，必须先更新 `docs/matrix_field_semantics.md`，再更新提示词、配置、测试和本文。
10. 功能语义必须前后一致：同一字段不能在分析师侧表示机会状态，在 PM 侧又表示最终动作；同一 reason code 不能在一个函数中表示候选，在另一个函数中表示阻断。

### 13.1 测试映射表

所有测试逻辑必须放在 `src/tests/test_*.py`；运行编排脚本只放在 `src/run/pre_backtest_test.py` 和 `src/run/backtest_daily_test.py`。内部转换、字段边界、权限边界、固定公式和系统不变量样例都在回测前一次性检测；每日回测后只读取真实 DB、artifact、payload，检查真实物理结果和系统不变量，不读取或复查任何智能体内部机制。新增内部状态流转或边界规则时，必须补下表对应测试，不能只改代码。

| 关键规则 | 覆盖测试文件 | 回测前总入口 | 每日回测后总入口 |
|---|---|---|---|
| 事实入口、artifact/payload 边界、业务模块不能绕写核心事实 | `src/tests/test_fact_entry_boundaries.py` | `pre_backtest_test.py` | 不进入 |
| 合格 `watch_for_trigger` 必须进入条件触发合约，不能被清成普通 `wait/0` | `src/tests/test_pm_watch_for_trigger_release.py` | `pre_backtest_test.py` | 不进入 |
| PM 状态转换矩阵：`watch_for_trigger/probe_candidate/tradeable_candidate/open_real/add/scale/reduce/exit` | `src/tests/test_pm_state_transition_matrix.py` | `pre_backtest_test.py` | 不进入 |
| 分析师 LLM 输出落地：结构化字段可表达 setup/触发/失效，但不能落地手数、仓位或最终动作 | `src/tests/test_analyst_output_landing.py` | `pre_backtest_test.py` | 不进入 |
| PM、Trader、Reviewer、Audit 的合约读取和执行摘要边界 | `src/tests/test_fact_entry_boundaries.py`、`src/tests/test_system_invariant_audit.py` | `pre_backtest_test.py` | 每日只跑真实产物 audit，不跑 unittest |
| Accountant 手续费、保证金、权益、PnL 固定公式 | `src/tests/test_accountant_settlement_formulas.py` | `pre_backtest_test.py` | 不进入 |
| 契约覆盖：producer、consumer、audit、test、文档、字段、配置、提示词对齐 | `src/tests/test_contract_coverage_audit.py` | `pre_backtest_test.py` | 不进入 |
| 回测前 DB schema、硬数据、配置和环境验收 | `src/tests/test_pre_backtest_acceptance.py` | `pre_backtest_test.py` | 不进入 |
| PG 单一报告字段、无 LLM、固定入口和无交易权限边界 | `src/tests/test_protocol_governor.py` | `pre_backtest_test.py` | 不进入 |
| 每日物理结果不变量样例：阶段、交易来源、审计放行、执行成交、结算账户和学习日期 | `src/tests/test_system_invariant_audit.py` | `pre_backtest_test.py` | 每日只读检查该日真实产物 |
| Reviewer 不写学习、Researcher 只写未来学习 | `src/tests/test_reviewer_learning.py`、`src/tests/test_fact_entry_boundaries.py` | 相关单测按需运行 | 由日后新增时纳入 |
| 统一字段迁移和旧字段残留 | `src/tests/test_unified_field_migration.py`、`src/tests/test_evaluation_unified_semantics.py` | 相关单测按需运行 | 不进入 |
| 市场确认和硬交易规则 | `src/tests/test_market_confirmation.py`、`src/tests/test_futures_market_rules.py` | 相关单测按需运行 | 不进入 |

当前固定编排：

```text
src/run/pre_backtest_test.py
-> test_fact_entry_boundaries
-> test_accountant_settlement_formulas
-> test_pm_watch_for_trigger_release
-> test_pm_state_transition_matrix
-> test_analyst_output_landing
-> test_system_invariant_audit
-> test_contract_coverage_audit
-> test_pre_backtest_acceptance
-> test_protocol_governor
-> protocol preflight
-> contract_coverage_audit
-> pre_backtest_acceptance

src/run/backtest_daily_test.py
-> system_invariant_audit
```

测试硬规则：

1. 新增测试文件必须命名为 `src/tests/test_*.py`。
2. 回测前必须跑的静态/样例测试，加入 `src/run/pre_backtest_test.py`。
3. 每个交易日后需要读取真实产物的 audit，加入 `src/run/backtest_daily_test.py`；不要把可静态证明的 unittest 放进每日入口。
4. 测试文件负责断言规则；运行脚本只负责编排，不能写业务测试逻辑。
5. 任何会影响交易状态流转、reason code 语义、配置门控或 LLM 输出落地的修改，必须同时更新本映射表。

## PG 审计边界补充（2026-07-07）

Protocol Governor 只检查协议边界和已落地物理结果，不替 PM 解释交易语义。对 PM 返回后由保存层物理化的 recommendation artifact，daily PG 只读取 `final_action_contract` 和 `signal_snapshot.signal_collection_contract` 核对唯一交易事实来源、来源边界和外部字段污染；不读取 `pm_six_step_trace` 复查 PM 自检或 Step6 生成过程。PG 不判断 PM 为什么 wait/hold/open/exit，不判断 PM 为什么 rank、不 rank、部署或不部署资金，也不复刻 PM 三类合约矩阵。

PG 对 `signal_snapshot.signal_collection_contract` 只审存在性、`source_agent="signal_collector"`、`collector_decision_boundary="no_trade_authority"`，以及 SCC 内不得出现 PM 越权字段，例如 `final_action`、`target_lots`、`lots_delta`、`opportunity_rank`、`opportunity_score`、`rank_score`、`position_sizing_result`、`capital_deployment`、`final_action_contract` 或 `pm_six_step_trace`。`final_action_contract.signal_collection_contract_ref` 只是摘要，不是主证据，不能替代完整 SCC。

PG 对 PM 外部物理结果的 hard fail 只来自协议断链或 artifact 污染：实际进入策略路径却缺最终合约、缺完整 SCC、SCC source_agent/boundary/越权字段非法、残留 Step1–5 中间状态或出现第二套交易事实。PM 自检、内部 reason code、rank、预算、sizing 和学习作用过程只由 PM 自身机制及回测前测试负责，daily PG 不读取、不复查。

测试体系按职责分层：`src/tests` 只构造样本并断言对应工具是否判对；`src/run/pre_backtest_test.py` 和 `src/run/backtest_daily_test.py` 只负责编排，不写审计规则。PG 专用审计规则只放在 `src/tools/agent_tools/control/pg_*.py`。

## Action-Value 动作语义补充（2026-07-09）

action-value 的动作含义必须按 `action_name -> canonical_action_family -> action_value_lane/learning_lane -> action_preference` 解释。统一解释工具是 `src/tools/common/final_action_semantics.py`，完整动作矩阵见 `docs/matrix_action_canonical.md`。

Researcher 写 `alpha_setup_action_value` 时必须保存 `canonical_action_family`、`action_value_lane` 和 `learning_lane`；PM 通过 `decision_memory_retrieval`、`pm_lifecycle_learning_router` 和合约构造链路消费这些 canonical 字段；Reviewer 复盘归因时只能用这些字段理解历史动作属于开仓/加仓、持仓、减仓/退出、条件监控还是执行质量；PG 只审 family/lane/preference 一致性和缺字段 hard fail。各模块不得维护私有字符串集合来猜 `add_or_open`、`reduce_or_exit`、`execution` 等动作含义。

`positive_candidate_open` 只允许落在 `canonical_action_family=open_add_new_risk` 且 lane 属于 `open/add/scale/increase` 的记录；`positive_candidate_exit` 只允许落在 `canonical_action_family=reduce_exit` 且 lane 属于 `reduce/exit` 的记录；`positive_candidate_execution` 只允许落在 `canonical_action_family=execution` 且 lane 为 `execution` 的记录。缺 `canonical_action_family`，或 family/lane/preference 不一致，属于系统字段语义 hard error。

学习偏向不是明日执行指令。PM 可以把 open/add 学习用于同生命周期评分或降级，并把安全的历史 open/add 经验作为对应新增风险 rank 输入；当天实际增加风险的 `add/scale` 与新开仓一起参与 rank。reduce/exit 学习用于释放风险判断，execution 学习用于未来 `final_action_contract.execution_profile/entry_trigger/requires_intraday_confirmation/can_execute_without_intraday_trigger`，但任何 action-value 都不能直接生成 `final_action`、方向、手数或资金部署。Trader 仍只执行审计通过后的 `final_action_contract`；Accountant 不消费 action-value。
