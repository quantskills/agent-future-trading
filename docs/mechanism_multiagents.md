# AgentQuant 多智能体运行机制

更新时间：2026-06-20

本文档只说明当前代码已经启用的多智能体工作流。不在本文启用清单里的角色不参与当前回测链路；本轮回测只按 `src/config/dev.yaml` 中的 `workflow_analysts: commodity_news, fundamental, technical` 和四阶段执行链运行。

## 一、总原则

AgentQuant 的多智能体机制服务一条主业务链：分析师给证据，PM 生成唯一策略合约，Auditor 审这张合约，Trader 只执行审过的合约或独立运营风控单，Accountant 按事实结算，Reviewer 做确定性复盘验收，Researcher 只在验收后写未来学习，Protocol Governor 做旁路协议检查。

系统最终策略交易事实只认 `final_action_contract`。PM 内部草稿只能保留为本地推演和日志上下文，不是 Trader、Researcher、evaluation 或 audit 的交易事实入口。`source_type=strategy` 走策略合约链；`source_type=rollover` 和 `source_type=forced_risk` 是非策略运营风控单，独立执行、独立核算，不进入策略 alpha 学习。

所有学习结果只能影响未来交易日。分析师只用学习校准证据质量；PM 读取 open/hold/exit 的动作偏好，并把 execution 偏好写入最终合约；Trader 不直接读取研究 action-value；Accountant 不被学习改账；Reviewer 不下单；Researcher 不影响当天交易。

## 二、当前启用智能体清单

| 智能体 | 输入/读取 | 输出/写入 | 是否调用 LLM | 工作边界 |
| --- | --- | --- | --- | --- |
| `technical` | PandaAI 盘前行情、技术指标、技术参数校准、历史技术学习上下文 | `AnalystSignal`、`action_evidence_contract`、`trade_research_contract`、技术数据摘要 | 是 | 只给技术证据、触发、失效边界和机会状态；不输出手数、保证金或交易命令 |
| `fundamental` | Finoview 本地 feather、PandaAI 衍生因子、基本面数据质量、历史基本面学习上下文 | `AnalystSignal`、基本面驱动、数据新鲜度、触发/失效边界、研究契约 | 是 | 只给基本面证据；中长期观点必须有短线触发与失效边界才可进入交易审查 |
| `commodity_news` | 本地新闻 txt、事件上下文、新闻影响窗口、历史新闻学习上下文 | `AnalystSignal`、事件类型、催化质量、影响窗口、研究契约 | 是 | 只给新闻催化证据；普通背景新闻不能直接生成开仓 |
| `portfolio_manager` | 三位分析师证据、账户/持仓/资金、market confirmation、action-value、memory quality、Auditor 结果 | `FuturesRecommendation`、唯一 `final_action_contract`、PM trace | 是 | 唯一生成策略交易意图和目标手数；不能跳过 Auditor、Trader、资金和字段审计 |
| `auditor` | `final_action_contract`、风险状态、合约状态、数据质量、硬风险配置 | `audit_verdict`、hard/soft risk reasons、审计 payload | 否 | 只审 PM 合约，不新造方向、手数或第二张合约 |
| `trader` | 审过的 `final_action_contract`、`audit_verdict`、盘中 PandaAI 行情、账户保证金状态、rollover/forced_risk 运营单 | 成交/未成交记录、执行审计、`execution_learning_trace`、forced-risk close order | 否 | 只执行策略合约和运营风控单；不能自己创造策略方向、手数或保证金目标 |
| `accountant` | 成交、持仓、结算价、手续费、滑点、合约乘数、保证金率 | `daily_settlement`、PnL、费用、保证金、账户权益、持仓状态 | 否 | 只按事实结算，不接受 LLM 或学习文本改账 |
| `reviewer` | Phase1-3 状态、推荐、成交、结算、data quality、交易日志事实 | Phase4 验收、daily summary、完整交易日志、学习候选 | 否 | 只做确定性验收和日志，不下单、不调仓、不写最终学习 |
| `researcher` | Reviewer 产物、已结算 episode、未交易机会、未触发条件机会、action outcome | `alpha_setup_profile`、`alpha_setup_action_value`、`adaptive_policy_state`、未来学习记忆 | 是 | 只写未来可用学习，不影响当天交易、成交和账务 |
| `protocol_governor` | 能力卡、工具权限、字段语义、artifact lineage、preflight/audit 状态 | `protocol_audit`、`preflight_health`、字段/权限/生命周期告警 | 否 | 旁路治理；不创建/否决交易权限，不改 lots/margin |

## 三、完整业务链路

```text
Phase1 盘前策略
  technical / fundamental / commodity_news
      -> 结构化证据、trigger_valid、setup_quality_ok、invalidation、opportunity_state
  portfolio_manager
      -> 唯一 final_action_contract
  auditor
      -> audit_verdict，审不过则 hold/wait/reduce

Phase2 盘中执行
  trader
      -> 先处理 forced_risk，再协调 pending rollover，再执行 strategy final_action_contract
      -> 普通策略单按 target_lots/lots_delta 翻译订单
      -> 条件 probe 只盘中检查触发，未触发只记录原因

Phase3 日终结算
  accountant
      -> 用成交、官方结算价、手续费、保证金率逐日盯市
      -> 写 daily_settlement / ticker_daily_pnl / position_state
      -> 收盘后若发现换月，只生成下一交易日 rollover

Phase4 复盘研究
  reviewer
      -> 验证 phase 完整性、账务一致性、交易日志
  researcher
      -> 写未来学习：open/hold/exit/execution action-value、setup profile、策略状态

旁路治理
  protocol_governor
      -> pre_backtest_acceptance / system_invariant_audit / unified field audit
      -> 发现非策略 hard error 时让回测 fail-fast
```

## 四、关键协作口径

### 分析师到 PM

分析师输出的是证据，不是仓位。`Bullish/Bearish/Neutral` 只是方向摘要；真正进入 PM 的是 `action_evidence_contract` 和 `trade_research_contract`。`setup_quality_ok=true` 只表示形态值得关注；`trigger_valid=true/current_trigger_confirmed=true` 才表示当前触发已经成立。`watch_for_trigger + trigger_valid=false` 不是立即开仓授权，但如果同时具备明确方向、触发条件、失效边界和可关注 setup，PM 可以把它纳入条件监控候选。

### PM 到 Auditor 到 Trader

PM 内部可以推演草稿，但跨智能体只输出唯一策略合约。Auditor 只审这张合约；审计结果写回推荐快照。Trader 读取审过的最终推荐记录，只能按 `final_action_contract.current_lots / target_lots / lots_delta` 执行，不能从 PM 文本、旧字段或最小一手机制重新推交易。

### 条件 probe

条件 probe 仍然是同一张 `final_action_contract`，不是第二套交易路径。PM 必须写明 `conditional_trigger_authority=true`、`requires_intraday_confirmation=true`、`can_execute_without_intraday_trigger=false`、方向、目标手数、触发条件和失效边界。Trader 只负责盘中判断触发是否成立；成立则按合约成交，未触发则记录 `not_triggered` 或对应原因。

### 运营风控单

换月和强平/强减不是策略决策。`rollover` 由日终结算后生成，下一交易日执行，并按当日策略目标决定是否恢复敞口；`forced_risk` 由盘中保证金风险触发，只能 close/reduce。两者都不写入策略 `final_action_contract`，不污染策略 action-value。

### 学习闭环

Researcher 写入的学习必须按 action lane 分账：open 评价开仓，hold 评价持仓，exit 评价退出/保护，execution 评价触发方式和成交质量。PM 只能在未来交易日、同作用域、当日证据仍成立时使用这些 action-value。Trader 和 Accountant 不直接读取 action-value。

## 五、回测前后验收

回测前必须通过 `pre_backtest_acceptance.py`，回测中每个交易日完成后通过累计 `system_invariant_audit.py`。验收重点包括：唯一合约、字段语义一致、分析师证据不自相矛盾、Trader 只按最终合约执行、运营风控单与策略单分账、未完成交易日硬拦、账务和阶段状态一致。出现 hard error 时，不能把该窗口盈亏当策略结论。
