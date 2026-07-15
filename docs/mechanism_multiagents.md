# AgentQuant 多智能体运行机制

更新时间：2026-07-10

本文是 AgentQuant 多智能体链路总纲，只定义启用智能体、阶段顺序、权限边界、事实入口总原则和文档分工。字段级生产/落盘/消费/审计矩阵不写在本文，避免和 `docs/matrix_chain_contract.md` 重复。
全链路契约的可执行依据固定为 `docs/matrix_chain_contract.md`；contract coverage、pre-backtest failure fixture 和 daily PG audit 均按该矩阵执行。

## 1. 文档职责

本文负责：

- 固定启用智能体。
- 固定端到端业务链。
- 固定 Phase1-Phase4 与研究学习顺序。
- 固定每个智能体的权限边界。
- 固定跨智能体禁止旁路。
- 指明字段语义、PM 细节、workflow 编排、action-value 动作矩阵和审计矩阵的唯一文档位置。

本文不负责：

- 字段级 `producer -> artifact/DB -> consumer -> audit -> hard fail` 细表。
- PM 六步内部细节。
- workflow 保存闸门细节。
- action-value 动作 canonical 矩阵。
- PG daily audit 错误分级表。
- 策略收益评价。

文档分工固定如下：

| 内容 | 唯一位置 |
|---|---|
| 多智能体角色、阶段、权限边界 | `docs/mechanism_multiagents.md` |
| workflow 编排、传递、保存、阻断 | `docs/workflow.md` |
| PM 六步、最终合约、自检、直接返回与返回后 artifact 边界 | `docs/agent_pm.md` |
| 全链路生产/落盘/消费/审计契约矩阵 | `docs/matrix_chain_contract.md` |
| 字段语义矩阵 | `docs/matrix_field_semantics.md` |
| action-value 动作 canonical 矩阵 | `docs/matrix_action_canonical.md` |
| 期货交易业务机制 | `docs/mechanism_future_trade.md` |
| 研究、复盘、记忆、学习边界 | `docs/mechanism_research.md` |

## 2. 系统目标与底线

AgentQuant 是多智能体期货交易系统。LLM 只用于分析师和研究员形成结构化预测证据与结构化研究成果；LLM 不直接决定方向、手数、资金部署、审计结论、成交、结算和最终交易合约。

系统底线：

1. 权限隔离：每个智能体只读取上游正式输出，只写本角色授权事实。
2. 字段统一：字段名称、含义、权限和消费方式全系统一致，唯一来源是 `docs/matrix_field_semantics.md`。
3. 合约唯一：策略交易唯一真相是 PM 签出的 `final_action_contract`。
4. 旁路只读：PG、contract coverage、pre-backtest acceptance、system invariant、mechanism audit 只审系统链路，不生成交易动作。
5. 研究只影响未来：Reviewer 与 Researcher 不改当天合约、成交、结算和交易权限。
6. 内部信息隔离：智能体间只传共享校验通过的正式契约；prompt、原始response、内部推理、中间工作状态、隐藏上下文和未验证工具结果不得持久化、跨角色传递或写日志/异常。

## 3. 固定业务链

```text
行情 / 基本面 / 新闻 / 历史学习
-> technical / fundamental / commodity_news
-> signal_collector
-> portfolio_manager
-> auditor
-> trader
-> accountant
-> reviewer
-> researcher
-> 下一交易日 technical / fundamental / commodity_news 与 portfolio_manager 消费结构化学习
```

控制组旁路链：

```text
protocol_governor
-> contract_coverage_audit
-> pre_backtest_acceptance
-> system_invariant_audit
```

控制组只审可静态证明的协议边界和已落地物理结果中的非策略问题：字段漂移、artifact 污染、越权、前视、schema 断裂、阶段断链、应落地合约缺失、交易不来自唯一合法来源。控制组不读取或复查智能体内部机制，不评价策略收益，不替 PM 判断方向、rank、手数和资金部署。

## 4. 阶段顺序

| 阶段 | 现实含义 | 入口 | 主体 | 输出 | 边界 |
|---|---|---|---|---|---|
| Phase1 | 盘前策略生成 | `src/run/proposal.py` | Analyst、Signal Collector、PM、Auditor | recommendation、`final_action_contract`、`audit_verdict` | 只能用盘前可见信息 |
| Phase2 | 开盘后执行 | `src/run/order.py` | Trader | transaction、execution result、intraday decision | 只能执行审计通过的 PM 合约 |
| Phase3 | 收盘后结算 | `src/run/settlement.py` | Accountant | settlement、PnL、保证金、持仓事实 | 只按成交和结算事实入账 |
| Phase4 | 收盘后复盘 | `src/run/validate_phase_flow.py` | Reviewer | Phase4 事实复盘、交易日志、事实归因 | 只核对已落地事实，不重新裁决合约合法性和账户硬风险，不写最终 action-value |
| Phase4 后 | 研究学习 | `src/run/research/researcher_learning.py` | Researcher | 结构化研究与学习记录 | 只影响未来交易日 |

回测、模拟盘、实盘共享同一阶段顺序和字段契约。回测可以连续跑历史交易日，每个交易日内部仍按 Phase1 -> Phase2 -> Phase3 -> Phase4 -> Research 顺序执行。

## 5. 启用智能体边界

| 角色 | 输入 | 输出 | LLM | 禁止行为 | 下游 |
|---|---|---|---|---|---|
| `technical` | 行情、技术指标、仅限历史交易日的技术校准研究、商品差异化 profile、数据截止时间、主 `llm` 配置 | 唯一正式 `action_evidence_contract`，保真承载技术方向、trigger、invalidation、`product_profile_evidence` 和 `fusion_evidence` | 是 | 私有模型路由；输出手数、仓位、资金部署、`final_action_contract` | Signal Collector |
| `fundamental` | 截止当前交易日可见的库存、仓单、基差、供需、产业数据，基本面校准研究、商品差异化 profile、主 `llm` 配置 | 唯一正式 `action_evidence_contract`；无当日新增数据时使用最近有效数据并标注时效，确无数据时输出合法 `no_opportunity` 证据 | 是 | 私有模型路由；伪造缺失数据；输出手数、仓位、资金部署、`final_action_contract` | Signal Collector |
| `commodity_news` | 截止当前交易日可见的新闻、事件、政策、舆情，新闻校准研究、商品差异化 profile、主 `llm` 配置 | 唯一正式 `action_evidence_contract`；无当日新事件时如实表达无当前催化，确无可用数据时输出合法 `no_opportunity` 证据 | 是 | 私有模型路由；伪造新闻催化；输出手数、仓位、资金部署、`final_action_contract` | Signal Collector |
| `signal_collector` | Workflow已保存、带真实 `signal_record_id` 且共享校验通过的三份AEC | 唯一 `signal_collection_contract` | 否 | 生成AnalystSignal或ID、读取研究库、输出 score/rank、手数、交易动作、`final_action_contract` | PM |
| `portfolio_manager` | SCC、账户、持仓、合约信息、配置、PM 工具输出、有效学习 | 第 6 步原子返回唯一 `FuturesRecommendation`；最终合约与两个最终检查位于 `signal_snapshot` | 否 | 调 LLM、重建 SCC、Step1–5 输出中间对象、输出第二套交易计划 | Auditor；审计通过后由 workflow 编排层交给 Trader |
| `auditor` | 完整FAC；权益、保证金、保证金比例、`risk_status`；持仓；SCC数据质量摘要；具体合约及失效边界；主配置硬上限 | `approve` / `approve_with_warning` / `block`、完整审计 payload、hard/soft risk reasons | 否 | 改方向、改手数、改FAC、新建合约；消费研究记忆；复审 PM 学习、融合、rank、预算和 sizing | Trader |
| `trader` | 审计通过的 PM 合约、盘中行情、执行配置 | 成交/未成交、触发事实、执行结果 | 否 | 读研究库或 action-value 下单、改 PM 方向、改目标手数、放宽触发 | Accountant、Reviewer |
| `accountant` | 成交、持仓、结算价、费用、保证金率、合约乘数 | settlement、PnL、保证金、权益、持仓状态 | 否 | 用 LLM、学习、复盘改账；写交易动作 | Reviewer |
| `reviewer` | recommendation、审计、执行结果、成交、结算、账户/持仓和阶段状态 | Phase4 事实复盘、交易日志、事实归因、研究输入材料 | 否 | 下单、调仓、写最终 action-value、重新裁决合约合法性或账户硬风险、触发 Researcher LLM | Researcher |
| `researcher` | Phase4与结算完成后，通过正式ID链验证的AEC、SCC、FAC、审计、执行、成交和结算事实 | 可为空的验证后结构化研究、action-value、profile、state、分析师校准信息 | 受限可调 | 保存原始模型内容；改当天合约、成交、结算、PnL、交易员权限；强制每笔交易学习 | 分析师正式校准检索、PM `decision_memory_retrieval` |
| `protocol_governor` | 代码、配置、DB、artifact、字段语义、契约覆盖；只读取字段矩阵已登记路径 | 只由字段矩阵已登记字段组成的回测前就绪报告和每日物理结果非策略风险报告 | 否 | 读取或复查智能体内部机制；使用未登记字段或通用容器补字段；自建动作语义；生成交易动作、改手数、写业务表、评价收益为 pass/fail | 开发与回测闸门 |

## 6. 唯一事实入口总原则

每类系统事实只有一个授权入口：

| 事实类型 | 授权入口 | 总边界 |
|---|---|---|
| 预测证据事实 | 三类分析师结构化输出 | 不含手数、仓位、最终交易动作 |
| 信号收集事实 | `signal_collector` | 只保真聚合预测证据，保留 `source_agent=signal_collector` 与 `collector_decision_boundary=no_trade_authority` |
| 策略交易事实 | `portfolio_manager` | 只由 PM 第 6 步签出唯一 `final_action_contract` |
| 审计事实 | `auditor` | 只审 PM 合约，不改合约 |
| 执行事实 | `trader` | 只记录执行和触发事实，不复制 PM 学习、rank、资金解释 |
| 结算事实 | `accountant` | 只按成交与结算事实计算 |
| 复盘事实 | `reviewer` | 只写验收、日志、事实归因、研究输入材料 |
| 研究学习事实 | `researcher_learning.py` 与研究写入工具 | 只影响未来交易日 |
| 控制治理事实 | 控制组只读审计入口 | 只读检查，不写业务表，不生成交易权限 |

三类分析师共同用本专业历史校准结论完成两项学习任务：LLM 调用前完善提示词，LLM 返回后用同一批合格记录确定性校对信号。技术面分析师额外保留产品、短周期和当前 market_regime 相关的有界指标参数校准，并在校准后重算最终指标与 technical_context。参数校准只属于技术分析算法，不能单独创造方向、机会状态或交易权限。三类分析师只服从主配置 `llm`，并在学习校准、质量/时效和商品 profile 评估完成后，由共享收口工具生成及校验唯一 `action_evidence_contract`。

字段级 `producer -> artifact/DB -> consumer -> audit -> hard fail` 矩阵不放在本文。本文只固定入口归属，细表由 `docs/matrix_chain_contract.md` 承载。

## 7. 禁止旁路

- 分析师自由文本成为交易依据。
- Signal Collector 读取研究库。
- PM 缺 SCC 时重建证据包。
- PM 工具直接签 `final_action_contract`。
- Workflow 生成 rank、资金部署、手数、PM 合约字段。
- Auditor 改 PM 方向、手数、新建合约。
- Trader 用学习记录、rank、score、研究库下单。
- Accountant 用学习、LLM、复盘材料改账。
- Reviewer 写最终 action-value。
- Researcher 改当天交易事实。
- PG 复判 PM 策略判断或收益优劣。
- 任意下游 artifact 复制完整 PM 合约为自己的事实输出。

## 8. 关键共享工具位置

| 工具 | 职责 |
|---|---|
| `src/tools/common/final_action_semantics.py` | 解释最终合约生命周期、手数变化、action-value family/lane/preference、自检共享语义 |
| `src/tools/common/evidence_fusion_semantics.py` | 解释证据融合、冲突、确认需求和复盘归因，不签合约 |
| `src/tools/agent_tools/decision/pm_contract_self_check.py` | PM 最终合约自检，不审策略对错 |
| `src/tools/agent_tools/control/pg_system_invariants.py` | 每日系统不变量，只读检查契约断裂 |
| `src/tools/agent_tools/control/pg_pre_backtest_acceptance.py` | 回测前非策略风险验收 |

## 9. Artifact 总边界

| 阶段 | 可以保存 | 禁止保存 |
|---|---|---|
| PM recommendation | 第 6 步返回的唯一 `FuturesRecommendation`；其中只保存 `final_action_contract`、两个最终检查、完整原始 `signal_snapshot.signal_collection_contract` 和矩阵登记的最终摘要 | Step1–5 内存状态、合约草稿、第二套交易计划、Trader 执行结果、收盘后事实、PM 重建 SCC |
| Auditor | 审计裁决、风险原因、只读审计摘要 | 新合约、改写后的方向、改写后的手数、研究库原始记录 |
| Trader / Phase2 | 执行事实、触发事实、执行必要字段摘要 | 完整 PM 合约镜像、PM 学习、rank、资金解释 |
| Transaction audit payload | 成交事实、保证金审计、执行触发摘要 | 完整 PM 合约镜像、PM 学习解释、PM 排名解释 |
| Accountant | 成交、费用、结算价、保证金、权益、持仓事实 | 研究学习字段、PM rank 字段、LLM 解释、交易动作改写 |
| Reviewer | Phase4 验收、交易日志、事实归因、研究输入材料 | 最终 action-value、研究状态写入、当天交易事实改写 |
| Researcher | 结构化研究、action-value、profile、state、执行学习、分析师校准 | 当天交易指令、当天合约改写、交易员权限、会计事实改写 |

## 10. 回测前与每日审计边界

回测前验收负责提前发现非策略系统问题：

- 字段语义漂移。
- artifact 越权。
- PM 对外最终合约生成与返回边界断裂。
- SCC 生产和落盘断裂。
- action-value family/lane/preference 断裂。
- Trader、Reviewer、Researcher 越权字段。
- 代表性 fixture 证明的通用字段、动作、职责、阶段或唯一交易事实不变量断裂；历史问题只可作为样本来源，不作为检测目的。
- 指定窗口与配置品种的交易日、PandaAI 日线开收盘价、官方结算价、主力合约映射、合约乘数、保证金率、具体合约信息或 Trader 分钟行情接口能力不可用。

回测前 PG 通过现有数据入口只读调用真实数据；不运行分析、决策、执行和结算，不调用 LLM，不写正式业务库。Finoview 和新闻只检查路径、可读性、解析与日期过滤，某品种某日没有新增基本面或新闻不阻断回测。

每日 PG 审计只读取当日已落地 DB、artifact 和 payload，并只 hard fail 外部物理事实中的系统契约断裂：

- 缺 `final_action_contract`。
- SCC 缺失、source_agent 错、boundary 错。
- Trader 成交不来自最终合约。
- artifact 污染。
- 字段语义不一致。
- phase 未完成。
- 越权字段进入下游 artifact。

每日 PG 不读取 `pm_six_step_trace` 判断 PM 自检，不检查学习如何影响 score/rank/手数/动作，也不复查 Auditor、Trader、Reviewer、Researcher 的内部判断过程。

每日 PG 不 hard fail 策略质量问题：

- rank 低。
- 机会少。
- 信号弱。
- 学习为空。
- 合法 observe 诊断线无交易动作偏向。
- 没开仓。
- 当天收益差。

策略优劣只由长期 PnL、回撤、胜率、资金利用率、成交质量和学习闭环分析评价。
