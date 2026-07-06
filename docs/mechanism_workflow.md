# Workflow 运行编排机制

本文固定 AgentQuant 中 `workflow` 的职责边界。`workflow` 不是智能体，也不是第十二个决策者；它只负责运行顺序、上下文传递、事实保存、审计/执行触发和流程阻断。各智能体的业务判断必须由各自智能体及其确定性工具完成。

## 0. workflow 职责总纲

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

## 1. workflow 应该负责什么

### 1.1 调度顺序

`workflow` 负责让系统按固定阶段运行：

分析师 -> 信号收集员 -> PM -> PG/Auditor -> Trader -> Accountant -> Reviewer/Researcher。

这里的“调度”只表示安排谁在什么阶段运行，不表示 `workflow` 代替任何智能体完成其业务工作。

### 1.2 传递输入输出

`workflow` 负责把上游已经签出的输出传给下游：

- 把分析师输出交给 `signal_collector`。
- 把 `signal_collector` 签出的结构化结果交给 PM。
- 把 PM 签出的 `final_action_contract` 交给 PG/Auditor/Trader。

`workflow` 只传递事实，不解释策略，不改字段语义。

### 1.3 等待必要上下文齐全

例如 PM 做全市场 rank 前，`workflow` 只能确保 PM 所需的全市场候选上下文已经齐备。

但“怎么 rank、怎么部署资金”是 PM 的决策逻辑，不是 `workflow` 的逻辑。PM 必须通过自己的确定性工具完成全市场 rank 和资金部署。

### 1.4 保存事实

`workflow` 负责保存各智能体已经签出的 artifact / contract / execution / settlement。

`workflow` 只能保存，不能改语义，不能把候选、草稿或中间诊断伪装成最终交易事实。

### 1.5 触发审计和执行

PM 签出合约后，`workflow` 触发 PG/Auditor 检查；审计通过后，触发 Trader 执行；执行后，触发 Accountant 结算；最后触发 Reviewer/Researcher 复盘学习。

这些触发动作不等于 `workflow` 参与策略判断。

### 1.6 阻断流程

如果 PG/Auditor hard fail，`workflow` 可以停止后续执行。

但 `workflow` 不能自己修 PM 合约、改手数、补 rank、改资金层级或补学习 trace。

### 1.7 回测/模拟盘运行节奏

自动回测或模拟盘中，`workflow` 负责按交易日推进、触发各阶段、记录阶段结果，并在 hard fail 或必要上下文缺失时停机。

这只是运行节奏控制，不是策略判断；`workflow` 不能因为回测结果、资金占用或策略表现而自行改变 PM 合约、rank 或仓位。

## 2. workflow 不应该负责什么

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
