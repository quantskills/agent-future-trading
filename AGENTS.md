# agent-future-trading AI 开发协作手册

本文件是 AI 协助开发 agent-future-trading 时必须遵守的最高工作手册。它只服务于项目开发、排错、回测验收和机制对齐，不保存普通讨论内容。

除非用户明确要求修改本文件，AI 不主动改动 `AGENTS.md`。发现本文件与代码、机制文档、测试结果不一致时，先说明冲突，再按用户指令处理。

## 1. 回答与方案规则

- 回答必须给出唯一结论、唯一原因、唯一行动方案。
- 禁用模糊表达：`如果`、`假如`、`例如`、`或`、`也许`、`可能`、`大概`、`倾向于`、`看起来`、`可以考虑`、`先观察`、`兜底`、`兼容旧路径`。
- 证据不足时先查代码、数据库、artifact、日志、测试和机制文档，查清后再回答。
- 不能用现有审计规则反推业务契约；必须先确定业务契约，再判断审计是否正确。
- 不能给多套方案让用户选择；必须给出明确修改点、原因、位置、验证方式。
- 不能把系统 bug 说成策略问题，也不能把策略亏损说成系统 bug。
- 用户问“现在该怎么做”时，直接给下一步动作。

## 2. 项目目标

agent-future-trading 的目标是让多智能体期货交易系统在回测、模拟盘和实盘链路中一比一复刻同一套交易逻辑，在系统链路干净后持续发现并扩大手续费后 alpha，形成稳定正净收益，同时提升资金利用率、回撤控制和学习闭环质量。限制交易、机械降仓和压低成交频率本身不构成收益优化；任何策略修改都必须说明它如何提高机会识别、正期望部署或失效 alpha 退出质量。

LLM 只能用于三个分析师形成结构化预测证据，以及研究员形成结构化研究结果。复盘员是确定性事实复盘者，不调用 LLM。LLM 不能直接决定仓位、手数、资金部署、审计结论和最终交易合约。

判断开发工作价值的标准：

- 修复真实系统链路 bug。
- 保持交易事实、审计事实、学习事实可追溯。
- 防止越权、前视、字段漂移、artifact 污染、PM 中间态外泄。
- 改善交易机会识别、入退场、资金部署、复盘学习和回测稳定性。

## 3. 运行环境

- 项目根目录：`D:\research\agent-future-trading`
- Python 环境：`C:\ProgramData\miniconda3\envs\deepfund\python.exe`
- 不使用 `base` 环境、系统默认 Python、未确认解释器。
- `.env` 保存密钥，禁止在回复、日志、文档中泄露。
- 临时排查脚本放在 `D:\research\Workshop\`，任务结束后清理。
- 禁止执行会丢弃用户工作的命令，尤其是 `git reset --hard`、`git checkout --`。

## 4. 必读文档

任何任务启动时，必须先读本文件，再读 `docs/matrix_chain_contract.md`，随后再读任务相关机制文档、代码、测试、artifact、DB、日志。

涉及系统运行、代码修改、回测排错、字段语义、artifact、审计、学习链路时，先读相关文档和代码。

- `docs/work_log.md`：只记录 `.py/.yaml/.yml` 行为修改。
- `docs/check_list.md`：只记录代码已实现且仍能由自然真实回测产生证据的部分验收和未验收项目；内部公式、严格控制变量反事实和未落盘中间态由确定性测试验收，不混入该清单。
- `docs/backtest_outcome.md`：保存指定历史回测的逐日逐笔结果与修改前后证据，只作对应实验评价，不覆盖当前代码契约。
- `docs/matrix_chain_contract.md`：全链路生产、落盘、消费、审计、hard fail、diagnostics 契约矩阵，是理解系统问题和修改 bug 的第一锚点。
- `docs/mechanism_multiagents.md`：多智能体固定工作流和边界。
- `docs/workflow.md`：workflow 编排边界。
- `docs/agent_pm.md`：PM 六步链路、最终合约、自检。
- `docs/mechanism_agent_internal_rules.md`：智能体内部状态流转。
- `docs/mechanism_research.md`：研究、复盘、记忆和学习边界。
- `docs/matrix_field_semantics.md`：字段语义矩阵唯一来源。
- `docs/matrix_action_canonical.md`：action-value 动作 canonical 矩阵。
- `docs/mechanism_data_model.md`：数据模型和落库边界。
- `docs/mechanism_future_trade.md`：期货交易业务机制。

冲突裁决顺序：

1. 当前代码事实、数据库事实、artifact 事实、测试结果。
2. `matrix_chain_contract.md`
3. `mechanism_multiagents.md`
4. `workflow.md`
5. `agent_pm.md`
6. `matrix_field_semantics.md`
7. `matrix_action_canonical.md`
8. `mechanism_agent_internal_rules.md`
9. 其他机制文档。
10. `docs/work_log.md`

## 5. 固定工作流

当前业务主链固定为：

```text
行情/数据
-> technical / fundamental / commodity_news 结构化预测证据
-> signal_collector 生成 signal_collection_contract
-> portfolio_manager 生成唯一 final_action_contract
-> auditor 审计合约
-> trader 执行
-> accountant 结算
-> reviewer 复盘
-> researcher 学习
-> 下一交易日分析师校准与 PM decision_memory_retrieval
```

运行阶段固定为：

| 阶段 | 含义 | 入口 | 输出 |
|---|---|---|---|
| Phase1 | 盘前策略生成 | `src/run/proposal.py` | recommendation、`final_action_contract`、`audit_verdict` |
| Phase2 | 开盘后执行 | `src/run/order.py` | transactions、execution result |
| Phase3 | 收盘后结算 | `src/run/settlement.py` | settlement、PnL、持仓事实 |
| Phase4 | 收盘后复盘验收 | `src/run/validate_phase_flow.py` | 复盘事实和研究输入 |
| Phase4 后 | 研究学习 | `src/run/research/researcher_learning.py` | 结构化学习记录 |

workflow 只编排、传递、保存和阻断；禁止生成 PM 交易语义、rank、资金部署、合约字段、审计结论。

## 6. 智能体边界

### 6.1 分析师

`technical`、`fundamental`、`commodity_news` 可调用 LLM，只输出结构化预测证据。

禁止输出：

- 手数、仓位比例、保证金授权。
- 最终交易动作。
- `final_action_contract`
- `opportunity_rank`、资金部署结论。

### 6.2 signal_collector

只读取分析师结构化证据，输出 `signal_collection_contract`。

必须保留：

- `source_agent="signal_collector"`
- `collector_decision_boundary="no_trade_authority"`
- 来源引用、逐条证据、方向、触发状态、强弱、时效、一致性、冲突、确认需求、缺失、风险、失效边界、profile 使用痕迹、`evidence_fusion`

禁止读取研究库、输出 score/rank、仓位、手数、交易动作、`final_action_contract`。

### 6.3 portfolio_manager

PM 不调用 LLM。PM 是唯一策略资金经理和唯一最终交易合约签发者。

PM 六步固定为：

1. 读取 SCC、账户、持仓、合约、行情、配置。
2. 判断单品种方向。
3. 结合持仓确定交易状态、候选质量和内部生命周期分流。
4. 按生命周期消费有效学习。
5. 新增风险路径执行全市场 rank 与资金部署。
6. 原子生成唯一 `FuturesRecommendation` 与 `final_action_contract`，并检查最终输出自身一致性。

PM 的直接输出只有第 6 步返回的 `FuturesRecommendation`。其 `signal_snapshot` 必须包含：

- `signal_snapshot.final_action_contract`
- `signal_snapshot.pm_six_step_trace`
- `signal_snapshot.signal_collection_contract`

PM 禁止重建、补造、改写 SCC。SCC 必须来自 workflow state 中 signal_collector 输出的原始 `signal_collection_contract`。

`final_action_contract.learning_used.alpha_setup_action_values` 只保存可作为 PM 正式学习证据的 canonical action-value。弱先验、相似 SQL 检索、诊断材料不得进入正式 action-value 主列表，只能进入 `learning_used.memory_retrieval.rejected_or_downgraded` 诊断位置。

PM 当前持仓生命周期口径固定如下：

- `position_pnl_ratio` 优先读取原开仓 FAC 完整周期手续费后 `cycle_return_on_notional`。普通持仓达到 -2% 且同向证据再验证失败时减仓 50%，达到 -4% 时退出；再验证通过时不得触发对应普通亏损减仓或退出。
- 新仓前两个交易日复核保持独立口径：达到 -0.5% 且复核失败时减仓 50%，达到 -2% 时退出。
- 原开仓 FAC 完整周期收益峰值为正、当前 `cycle_return_on_notional<=0` 且同向证据再验证失败时，所有仓位类型均保持既有减仓复核路径。
- 反向目标在当前原子决策中必须强制 `target_lots=0` 并先退出旧方向。仓位归零后的后续反向机会使用新的当日 FAC、重新进入 Rank 与资金部署并建立新学习周期；不得在同一 recommendation 中同时平旧和开反向新仓。

### 6.4 auditor

只读审计 PM 唯一最终合约的必需字段、基本动作逻辑、账户硬风险、保证金硬上限、合约/失效边界和数据质量；不改方向、不改手数、不新建合约、不直接消费研究记录，也不复审 PM 的学习、融合、rank、预算和 sizing 过程。

### 6.5 trader

只执行审计通过的 `final_action_contract` 和合约化触发规则。

禁止读取研究库、action-value、`learning_used`、`opportunity_rank` 并据此下单。

Trader 保留通用订单翻译中的两步反转防御能力，但当前生产策略 PM 不向 Trader 下发同一原子反向目标；该能力不得被文档描述为当前策略反手路径，也不得绕过 PM 的 exit-first、新 FAC 和重新 Rank 约束。

### 6.6 accountant

只按成交和结算事实入账，禁止生成策略结论。

### 6.7 reviewer

只复盘决策、审计、执行、成交和结算等已落地事实，核对物理事实一致性并做结果归因；不重新裁决合约合法性和账户硬风险。复盘员可提供研究材料，不能直接写最终 action-value。

### 6.8 researcher

只在 Phase4 验收后运行，输出结构化研究和学习记录。

禁止生成当日交易指令、修改 PM 合约、修改交易员权限、使用未来数据。

### 6.9 protocol_governor

只做旁路治理和系统不变量审计，不参与交易消费，不评价策略收益。

PG 审 artifact 边界、字段语义、可追溯性、越权、中间态污染、契约断链。PG 不复刻 PM rank、手数、方向、资金部署判断。

PG 的输入路径、判定字段和输出报告只能使用 `docs/matrix_field_semantics.md` 已登记字段；动作解释只能使用 `docs/matrix_action_canonical.md`。`metadata`、`payload` 和 JSON 容器不能绕过登记。现有字段不足时，必须先证明真实功能缺口并登记，再修改 PG 代码；禁止先在控制工具中自创字段、别名或私有动作语义。

回测前 PG 必须通过现有只读数据入口检查指定回测区间和配置品种的真实数据就绪性。交易日、PandaAI 日线开收盘价、官方结算价、主力合约映射、合约乘数、保证金率、具体合约信息及 Trader 分钟行情接口能力属于交易必需检查；Finoview 和新闻只检查路径、可读性、解析及日期边界，不要求每品种每日齐全。该检查不调用 LLM、不运行策略、不写正式业务库。

## 7. 唯一交易真相

策略交易的唯一真相是 PM recommendation 中的 `final_action_contract`。

禁止旁路：

- 分析师自由文本成为交易依据。
- signal_collector 读取研究库。
- workflow 生成 rank、资金部署、PM 字段、PM 合约。
- 审计员、交易员、会计师、复盘员、研究员改写 PM 交易方向和目标手数。
- 交易员用学习记录下单。
- LLM 自由文本成为仓位、审计、结算、研究 action-value 依据。

非策略动作使用独立来源类型，不能污染 alpha 学习。

## 8. 字段语义

- `docs/matrix_field_semantics.md` 是字段语义矩阵唯一来源。
- `docs/matrix_action_canonical.md` 是 action-value 动作 canonical 矩阵唯一来源。
- 已有字段能表达同一语义时必须复用已有字段。
- 新字段必须同轮同步生产端、消费端、审计、测试、机制文档。
- 禁止新增第二套字段名、别名、旧字段兼容路径。
- 禁止从裸 `action_name` 私自推断学习语义；必须使用统一动作语义工具。

action-value 标准路径：

```text
action_name -> canonical_action_family -> action_value_lane / learning_lane -> action_preference
```

学习偏向不是明日执行指令。具体交易动作只来自 PM 当日 `final_action_contract`。

## 9. Artifact 与审计边界

- PM Step1-5 只更新同一个 PM 内存状态，不生成独立 artifact、合约草稿、recommendation、DB 记录和物理日志。
- `workflow` 编排层、Auditor 和保存层只能在 PM 返回后审计并物理化最终 `FuturesRecommendation`；不得保存 PM Step1-5 中间状态。
- `pm_six_step_trace` 只保留 Step6 最终合约生成检查和最终合约自身检查，不保存早期状态和跨步骤比较结果。
- `decision_learning_rows` 是 Step6 final contract 按最终生命周期重新生成的最终决策层学习 trace。
- `trigger_profile_learning_rows` 只保存 execution/trigger/profile 学习，不进入决策层。
- 自检审最终合约证据链是否干净，不审交易判断是否正确。
- PG daily audit 只认 `signal_snapshot.signal_collection_contract` 作为 SCC 主证据。
- `signal_collection_contract_ref` 可保留为摘要，不能替代完整 SCC。

## 10. 开发流程

动手前必须完成：

1. 先读 `AGENTS.md`，确认执行规范。
2. 再读 `docs/matrix_chain_contract.md`，分别定位本次任务对应的生产端、落点、消费端、角色自身校验、pre-backtest gate、daily PG 物理结果审计、测试和机制文档；不得让 daily PG 复查角色内部机制。
3. 明确任务属于系统 bug、策略表现、配置、数据、学习、文档对齐中的哪一类。
4. 阅读 `docs/work_log.md`、相关机制文档、相关代码、测试、最近回测记录和审计结果。
5. 沿完整链路排查，不只盯单个函数。
6. 先确定业务契约，再确定实现，再确定审计。

修改 bug 时必须先在 `docs/matrix_chain_contract.md` 定位对应契约行，再按生产端、落点、消费端、角色自身校验、pre-backtest gate、daily PG 物理结果审计、测试和机制文档的顺序核对。

修改时必须守住：

- 不新增兜底逻辑。
- 不静默降级。
- 不补默认值伪造事实。
- 不用旧字段绕过唯一合约。
- 不让控制组写交易策略。
- 不让 workflow 生成 PM 语义。
- 不把偶然失败写成全局硬规则。
- 不用门控压死交易来伪装修复。

真实失败路径要先补可复现测试，再修代码，再跑目标测试和相关链路验收。

## 11. 验证命令

常用命令必须使用 deepfund：

```powershell
C:\ProgramData\miniconda3\envs\deepfund\python.exe -m compileall src
C:\ProgramData\miniconda3\envs\deepfund\python.exe -m unittest
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\pre_backtest_test.py --config src\config\dev.yaml --local-db --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\backtest_daily_test.py --config src\config\dev.yaml --local-db --json
```

按影响面选择：

- 目标单测。
- 相关链路单测。
- `compileall`
- `pre_backtest_acceptance`
- `system_invariant_audit`
- `contract_coverage_audit`
- `git diff --check`

## 12. 回测判断

- `system_invariant_audit` hard fail 时，停止收益讨论，按系统 bug 处理。
- `system_invariant_audit` clean 后，收益差才进入策略层分析。
- `docs/check_list.md` 只保留自然真实回测能够产生证据的项目。严格控制变量比较、内部固定公式、未持久化中间态和代码不变量必须由回归、属性或生产链路测试验收；已经完整验收的项目从清单删除，语义与生产代码不一致的项目先改清单再回测。
- daily gate 只检查真实 DB、artifact 和 payload 中的物理运行结果，不读取或复查 PM 自检、rank、学习作用过程及任何智能体内部机制，也不承载静态代码扫描职责。
- pre-backtest gate 是回测前 readiness 检查，不代表策略一定盈利。

## 13. 工作日志

`docs/work_log.md` 只记录完成后的 `.py/.yaml/.yml` 行为修改和运行配置修改。

必须记录：

- 业务逻辑修改。
- 智能体输入输出修改。
- 交易合约、审计、执行、结算、学习修改。
- 测试逻辑修改。
- 控制组工具修改。
- runtime 配置修改。

不记录：

- 纯讨论。
- 纯方案。
- 纯回测分析。
- 纯文档、README、AGENTS 修改。
- 数据文件变动。
- 文件改名、删除。
- 只改注释且不改变行为。
- 只运行测试和命令。

日志按日期正序追加，每条只写：

- 改了什么。
- 为什么改。

## 14. 交付回复

完成任务后必须说明：

- 修改文件。
- 核心逻辑变化。
- 验证命令和结果。
- 真实交易链路是否改变。
- 是否存在新旁路风险。
- 下一轮回测重点观察项。

未完成任务时必须说明唯一阻塞点和下一步动作。
