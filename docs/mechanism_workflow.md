# Workflow 运行编排机制

本文固定 AgentQuant 中 `workflow` 的职责边界。`workflow` 不是智能体，也不是第十二个决策者；它只负责运行顺序、上下文传递、事实保存、审计/执行触发和流程阻断。各智能体的业务判断必须由各自智能体及其确定性工具完成。
全链路生产、落盘、消费、审计、hard fail 与 diagnostics 契约以 `docs/matrix_chain_contract.md` 为准；workflow 只按该矩阵触发只读 gate 与流程阻断，不补字段、不改语义。

## 0. 11 个智能体端到端业务流

AgentQuant 的固定业务链如下：

```text
technical / fundamental / commodity_news 分析师
-> signal_collector
-> portfolio_manager
-> auditor
-> trader
-> accountant
-> reviewer
-> researcher
-> 下一交易日 technical / fundamental / commodity_news 分析师与 portfolio_manager 消费结构化学习
```

`protocol_governor` 是旁路协议管理员，负责检查非策略问题、系统不变量、字段语义、机制断链和 hard fail，不参与交易策略生成，不改交易合约，不评价收益。

### 0.1 分析师

三类分析师分别对同一品种的价格序列及其相关数据做多维分析，判断下一交易日价格走势。`workflow` 编排层只传递主配置 `llm`、盘前可见数据、商品 profile 所需身份和研究检索所需上下文；不为分析师选择私有模型，也不解释学习结论。

分析师在历史学习提示词、LLM 专业分析、同批学习确定性校对、数据质量/时效和商品 profile 评估完成后，只输出最终校验通过的唯一 `action_evidence_contract`，其中保真承载方向、证据强弱、setup、trigger、invalidation、冲突、确认要求、`product_profile_evidence`、`fusion_evidence` 和可追溯依据。`workflow` 编排层不生成、补写或二次解释该合约。分析师不能输出手数、仓位、资金部署、rank、交易权限或 `final_action_contract`。

### 0.2 信号收集员

`signal_collector` 收集并整理所有分析师传出的结构化信号证据，保真打包为 PM 可消费的结构化信号包。

`signal_collector` 不做策略决策，不读取研究库，不生成 rank，不生成资金部署，不生成手数，不生成 `final_action_contract`。

### 0.3 PM

PM 接收 `signal_collector` 打包好的结构化信号证据，并按 `docs/agent_pm.md` 的六步机制处理结构化证据、持仓和配置；学习成果由 PM Step4 通过 `decision_memory_retrieval` 自行读取，不由 `workflow` 编排层传入。最终状态在 Step6 转成唯一合法的 `final_action_contract`。

PM 是唯一组合决策者、唯一资金 rank 与资金部署决策者、唯一 `final_action_contract` 签发者。

PM Step1–5 只更新同一个 PM 内存状态，不向 `workflow` 编排层返回中间对象。PM 第 6 步原子返回唯一 `FuturesRecommendation`，其中 `signal_snapshot.final_action_contract` 是唯一交易事实。

### 0.4 审计员

Auditor 只审计 PM 签出的唯一合法合约，检查账户、持仓、保证金、合约完整性、数据质量和硬风险边界。

Auditor 不能改策略、不能改方向、不能改手数、不能新建合约。

### 0.5 交易员

审计通过后，下一交易日由 Trader 执行这张唯一合约。

Trader 只能按合约里的 `current_lots/target_lots/lots_delta/final_action`、`execution_profile`、`entry_trigger`、`requires_intraday_confirmation` 和盘中行情事实执行；需要盘中确认的合约，Trader 通过盘中盯盘判断触发或未触发，并记录执行事实。

Trader 不读取学习，不改 rank，不改策略，不改方向，不改目标手数。

### 0.6 会计师

合约执行后，Accountant 根据成交、持仓、结算价、手续费、滑点、保证金率和合约乘数核算该交易日账户、保证金、持仓和浮盈亏。

Accountant 只结算，不读学习，不改交易。

### 0.7 复盘员

Reviewer 对该交易日 Phase1-3 的完整链路进行复盘，确认是否存在系统运行问题、数据问题、字段问题、越权问题、执行问题或结算问题。

Reviewer 只复盘和归因，不写最终 action-value，不改当日交易事实。

### 0.8 研究员

Researcher 基于复盘材料、交易过程和交易结果形成结构化研究成果，包括产品/setup/trigger/action-value、执行质量、条件监控质量、持仓/退出效果等学习记录。

这些结构化研究成果供下一交易日分析师和 PM 通过规定入口消费，用于分析与决策策略迭代；Researcher 不改当日交易事实，不生成当日交易合约，不改 Trader 或 Accountant。

### 0.9 协议管理员

`protocol_governor` 作为旁路控制智能体，检查系统是否存在非策略问题，包括字段缺失、边界越权、机制断链、hard fail 条件、artifact 越界、合约不完整和测试/文档/字段语义不一致。

`protocol_governor` 不参与交易策略判断，不修 PM 合约，不改手数，不评价收益。

## 1. workflow 职责总纲

`workflow` 负责“系统怎么跑、能不能继续跑”，不负责“交易什么、投多少”。

`workflow` 应该统筹：

- 阶段顺序：分析师 -> `signal_collector` -> PM -> PG/Auditor -> Trader -> Accountant -> Reviewer/Researcher。
- 上下文齐全：结构化信号、持仓、配置、学习输入、全市场候选上下文是否齐备。
- 输入输出传递：把上游签出的结果交给下游。
- 事实保存：保存各智能体已经签出的 artifact / contract / execution / settlement / learning。
- 流程阻断：PG/Auditor hard fail、缺合约、非法 artifact 时停止。
- 回测/模拟盘节奏：按交易日推进、触发阶段、失败停机。

`workflow` 不应该做：

- 判断多空方向。
- 判断哪个产品最值得交易。
- 生成 `opportunity_rank`。
- 部署资金。
- 改 PM 合约。
- 改 `target_lots` / `final_action` / `capital_deployment`。
- 补字段。
- 解释策略。
- fallback 修复。

## 2. workflow 应该负责什么

### 2.1 调度顺序

`workflow` 负责让系统按固定阶段运行：

分析师 -> 信号收集员 -> PM -> PG/Auditor -> Trader -> Accountant -> Reviewer/Researcher。

这里的“调度”只表示安排谁在什么阶段运行，不表示 `workflow` 代替任何智能体完成其业务工作。

### 2.2 传递输入输出

`workflow` 负责把上游已经签出的输出传给下游：

- 把分析师输出交给 `signal_collector`。
- 把 `signal_collector` 签出的结构化结果交给 PM。
- 接收 PM 第 6 步返回的唯一 `FuturesRecommendation`，先把其中的最终合约交给 PG/Auditor，审计通过后再交给 Trader。

`workflow` 只传递事实，不解释策略，不改字段语义。

### 2.3 等待必要上下文齐全

例如 PM 做全市场 rank 前，`workflow` 只能确保 PM 所需的全市场候选上下文已经齐备。

但“怎么 rank、怎么部署资金”是 PM 的决策逻辑，不是 `workflow` 的逻辑。PM 必须通过自己的确定性工具完成全市场 rank 和资金部署。

### 2.4 保存事实

`workflow` 编排层负责在 PM 返回后组织审计，并由保存层物理化最终 recommendation、审计结果、artifact、execution 和 settlement。

`workflow` 只能保存，不能改语义，不能把候选、草稿或中间诊断伪装成最终交易事实。

PM strategy recommendation 保存前必须通过只读 hard gate：

- `signal_snapshot.final_action_contract` 必须存在且为 PM Step6 已签出的最终合约。
- `signal_snapshot.pm_six_step_trace.pm_contract_self_check.ok == true`。
- `signal_snapshot.pm_six_step_trace.step6_contract_generation_check.ok == true`。
- `signal_snapshot.signal_collection_contract` 必须存在，且是 PM 从 workflow state 读取到的 signal_collector 原始 `signal_collection_contract` 快照，保留 `source_agent="signal_collector"` 与 `collector_decision_boundary="no_trade_authority"`。
- `signal_snapshot` 不得残留 `pm_internal_candidate`、`pm_internal_candidate_contract`、`pm_capital_deployment_decision` 或 PM draft 字段。

当前第一阶段没有独立 signal_collector artifact，PM final `signal_snapshot.signal_collection_contract` 保存完整原始 SCC 供 PG、Reviewer 和 Researcher 审计追溯；这不是 PM 重建证据包，也不是第二套字段语义。后续如果信号收集员独立落 artifact，可再收敛为 artifact path / id / sha256 强引用，但强引用不是本阶段目标。

`workflow` 编排层只检查这些条件，不修合同、不补字段、不读取和保存 Step1–5 内存状态，也不执行跨步骤比较式自检。

### 2.5 触发审计和执行

PM 签出合约后，`workflow` 触发 PG/Auditor 检查；审计通过后，触发 Trader 执行；执行后，触发 Accountant 结算；最后触发 Reviewer/Researcher 复盘学习。

这些触发动作不等于 `workflow` 参与策略判断。

### 2.6 阻断流程

如果 PG/Auditor hard fail，`workflow` 可以停止后续执行。

但 `workflow` 不能自己修 PM 合约、改手数、补 rank、改资金层级或补学习 trace。

### 2.7 回测/模拟盘运行节奏

自动回测或模拟盘中，`workflow` 负责按交易日推进、触发各阶段、记录阶段结果，并在 hard fail 或必要上下文缺失时停机。

这只是运行节奏控制，不是策略判断；`workflow` 不能因为回测结果、资金占用或策略表现而自行改变 PM 合约、rank 或仓位。

## 3. workflow 不应该负责什么

`workflow` 明确不负责：

- 不收集信号，这是 `signal_collector` 的职责。
- 不判断交易方向，这是 PM 的职责。
- 不判断哪个产品最值得投钱，这是 PM 的 rank 机制。
- 不生成 `opportunity_rank`。
- 不部署资金，这是 PM 的资金部署机制。
- 不改 `final_action_contract`。
- 不改 `target_lots` / `final_action` / `capital_deployment`。
- 不补 rank 字段、资金字段或学习 trace。
- 不补学习 trace。
- 不解释策略。
- 不让旧字段“看起来合法”。
- 不保留 atomic fallback 或任何 fallback 修复非法合约的路径。

一句话：`workflow` 是运行编排层，只保证阶段顺序、上下文传递、事实保存、审计执行触发和 hard fail 阻断；所有策略判断、rank、资金部署和最终合约签发都必须留在 PM 及其确定性工具边界内。
