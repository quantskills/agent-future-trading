# PM 内部工作流机制

本文固定 PM 内部工作流口径。PM 是唯一组合决策者和唯一 `final_action_contract` 签发者；工具和模块只提供结构化输入、语义判断、合约构造、自检和全市场资金 rank 支撑，不替代 PM 生成交易事实。

PM 内部 6 步流程固定为：

1. 读取输入
2. 判断生命周期动作口
3. 判断单品种方向与候选质量
4. 按生命周期消费学习
5. 新增风险进入全市场资金 rank 与资金部署
6. 生成唯一 `final_action_contract`

## PM 内部统筹调度边界

PM 内部 6 步由 `portfolio_manager.py` 作为 PM 主入口统筹，调用 PM 自己的确定性工具完成，不由 `workflow.py` 拆开调度。

PM 内部顺序必须固定为：

1. 读取输入：结构化信号、持仓、配置、学习、episode 反馈。
2. 判断生命周期动作口：调用 `pm_lifecycle_action_port.py`。
3. 判断单品种方向与候选质量：调用 `pm_ticker_side_selection.py`。
4. 按生命周期消费学习：调用 `pm_lifecycle_learning_router.py`。
5. 新增风险进入全市场 rank 与资金部署：调用 `pm_full_market_capital_deployment.py`。
6. 生成唯一 `final_action_contract`：调用 `pm_contract_builder.py`，写入 `pm_six_step_trace.step6_contract_generation_check`，并调用 `pm_contract_self_check.py`。

一句话：`workflow.py` 调 PM，PM 内部自己按六步调工具。`workflow.py` 管阶段，PM 管策略。

分流逻辑固定为：

- 如果是 `hold` / `reduce` / `exit` / `wait` 这类非新增风险动作，走 `1 -> 2 -> 3 -> 4 -> 6`，不经过全市场资金 rank。
- 如果是 `open` / `open_probe` / `add` / `scale` / `reverse` / `conditional open` 这类新增风险动作，走 `1 -> 2 -> 3 -> 4 -> 5 -> 6`，必须经过全市场资金 rank 和预算部署后，才能签最终合约。

所以第 6 步才是 PM 内部最后一步：无论走不走 rank，最终都必须落到唯一 `final_action_contract`。

## 1. 读取输入

PM 读取输入用到的工具/模块：

- `signal_collector` 结构化信号快照
- `pm_signal_fusion`
- `pm_ticker_side_selection`
- `pm_full_market_capital_deployment`
- `alpha_setup`
- `pm_contract_builder`
- `final_action_semantics`
- `pm_contract_self_check`

### 1.1 结构化信号快照

从 `signal_collector` 输出中按 `ticker` 读取当天结构化字段：

- 方向
- 证据强弱
- `setup`
- `trigger`
- `invalidation`
- 冲突
- `confirmation requirement`

PM 只读结构化字段，不把分析师自由文本当交易指令。

### 1.2 当前持仓

从 portfolio / position 状态中按 `ticker` 读取：

- `current_lots`
- `current_side`
- `holding_days`
- `entry_price`
- 未实现盈亏
- 当前保证金占用

这些字段用于判断有仓/无仓，以及后续生命周期动作口。

### 1.3 资金与风险配置

从 `dev.yaml` / `portfolio_policy_catalog.yaml` 读取：

- 总保证金上限
- 单品种上限
- 净敞口上限
- `probe=0.008`
- `real budget` / `alpha scale` 资金层级参数

PM 只能读配置参数，不能临时编仓位参数。

### 1.4 产品级学习

从 `alpha_setup_profile` / 产品级动态学习读取：

- 产品
- 方向
- `setup`
- `trigger`
- `evidence_combo`
- 历史表现
- 资金部署结果

读取时按 `ticker + side + setup + trigger + evidence_combo` 匹配，只做产品差异化和策略表现校准，不直接生成交易权限。

### 1.5 action-value 学习

从 `alpha_setup_action_value` 读取：

- `open/add` 学习
- `hold` 学习
- `reduce/exit` 学习
- `execution` 学习
- `conditional_monitor` 学习
- `reward`
- `outcome`
- `action_preference`

读取时按生命周期消费：

- `open/add` 给新增风险 rank
- `hold` 给持仓
- `reduce/exit` 给释放资金
- `execution` 给 trigger/profile
- `conditional_monitor` 给条件监控质量

不同生命周期不能混用。

### 1.6 历史 episode 反馈

从 Researcher 写回的已完成或可归因 episode 中读取：

- 交易后收益
- 亏损 episode
- `trigger` 有效性
- `entry quality`
- `exit quality`
- `deployment outcome`

这些反馈用于修正下一轮证据质量、`rank_score`、持仓/退出/trigger 判断，不改当日已经发生的交易事实。

## 2. 判断生命周期动作口

PM 判断生命周期动作口时，按 6 大块里的第 2 块固定执行。

### 2.1 调用工具/模块

主工具：

- `pm_lifecycle_action_port.py`

共享语义依赖：

- `final_action_semantics`

输入辅助：

- `signal_collector` 结构化信号快照
- 当前持仓状态
- 条件字段
- `current_lots`
- `target_lots`
- `lots_delta`

明确禁止：

- `pm_contract_builder.py` 不参与动作口判断，只在第 6 步生成唯一 `final_action_contract`
- `pm_contract_self_check.py` 不参与动作口判断，只做最终合约自身机制边界检查
- `pm_ticker_side_selection.py` 不参与动作口判断，只在第 3 步判断单品种方向与候选质量
- `pm_full_market_capital_deployment.py` 不参与动作口判断，只在第 5 步处理新增风险候选的唯一全市场资金 rank 与资金部署
- `workflow.py` 不参与动作口判断

### 2.2 交易动作分口

新增风险口：走 rank。

- `open`
- `open_probe`
- `open_real`
- `add`
- `scale`
- `increase`
- `reverse`
- `conditional open`

非新增风险口：不走 rank。

- `wait`
- `hold`
- `reduce`
- `exit`
- `close`
- `risk_exit`

条件监控口：分两种。

- 保留新增风险意图：走 rank，并要求 Trader 写盘中触发结果。
- 已打回观察或 `no_rank_no_new_exposure`：不走 rank，不要求 Trader 盘中结果。

### 2.3 判断方法

按 `current_lots`、`target_lots`、`lots_delta` 和条件字段判断：

- `current_lots=0` 且 `target_lots=0`：`wait`
- `current_lots=0` 且 `target_lots!=0`：`open` / `open_probe` / `conditional open`，属于新增风险，必须走 rank
- `current_lots!=0` 且 `target_lots=current_lots`：`hold`
- `current_lots!=0` 且 `target_lots=0`：`exit` / `close` / `risk_exit`
- 同方向持仓，`abs(target_lots) < abs(current_lots)`：`reduce`
- 同方向持仓，`abs(target_lots) > abs(current_lots)`：`add` / `scale` / `increase`，属于新增风险，必须走 rank
- `current_lots` 和 `target_lots` 方向相反：`reverse`，属于新增风险，必须走 rank
- 有 `requires_intraday_confirmation=true` 且仍保留新增 `target_lots`：条件开仓，走 rank，并要求 Trader 写触发/未触发结果
- 已被还原成 `target_lots=current_lots` 或 `target_lots=0`，且原因是 `no_rank_no_new_exposure` / `no_rank_or_budget_no_new_exposure`：未部署条件候选，不走 rank

一句话：PM 先用持仓变化和条件字段判断动作口；只有会新增风险敞口的动作进入唯一全市场 rank，持仓、减仓、退出、等待不抢新资金 rank。

## 3. 判断单品种方向与候选质量

### 3.1 调用工具/模块

使用：

- `pm_signal_fusion`
- `alpha_setup`
- `final_action_semantics`

不调用 `pm_full_market_capital_deployment`。`pm_full_market_capital_deployment` 只用于第五步全市场资金 rank。

### 3.2 判断内容

方向判断：

对单个 `ticker` 判断当天应偏 `long`、`short` 还是 `flat`。

候选质量判断：

判断该方向是否具备候选资格，依据包括证据强度、三类分析师一致性、`setup` 清晰度、`trigger` 明确度、`invalidation` 明确度、冲突程度、产品级 profile 是否支持。

### 3.3 输出边界

这一步只能形成 PM 内部方向判断结果：

- `side_priority`
- `ticker_side_priority`
- `side_priority_score`

禁止输出：

- `opportunity_rank`
- `capital_layer`
- `real_budget_entry`
- 最终 `target_lots`
- 资金部署结论

一句话：第三步只判断“这个产品偏多、偏空还是不做，以及候选质量够不够”，不判断“全市场谁最值得投钱”。

## 4. 按生命周期消费学习

这一点只回答：这些学习应该影响哪个决策口。

### 4.1 调用工具/模块

使用：

- `alpha_setup`
- `pm_signal_fusion`
- `final_action_semantics`
- `pm_contract_builder`

不调用 `pm_full_market_capital_deployment` 做全生命周期学习消费。

`pm_full_market_capital_deployment` 只在第 5 步消费 `open/add` 学习，用于新增风险全市场资金 rank。

### 4.2 全周期动作划分

新增风险动作：

- `open`
- `open_probe`
- `open_real`
- `add`
- `scale`
- `increase`
- `reverse`

持仓动作：

- `hold`

释放资金动作：

- `reduce`
- `exit`
- `close`
- `risk_exit`

条件监控动作：

- `conditional open`
- `conditional_monitor`

执行触发动作：

- `execution`

### 4.3 消费哪些学习结论

新增风险动作消费：

消费 `open/add/scale/increase` 学习：历史收益、亏损 episode、`entry quality`、`deployment outcome`、产品/setup/trigger 同类表现。

影响：新增风险候选质量、后续 `rank_score`、是否具备 `real_budget_entry` / `alpha_scale` 候选资格。

持仓动作消费：

消费 `hold` 学习：继续持有是否有效、盈利持仓是否应保护、趋势持仓是否应延续、忽略 `exit` 学习是否需要解释。

影响：是否继续持有、是否写合法继续持有解释、是否保护性不动仓。

释放资金动作消费：

消费 `reduce/exit` 学习：退出是否有效、减仓是否保护收益、亏损 revalidation 是否失败、继续持有是否风险更大。

影响：是否减仓、是否退出、是否释放资金、是否保留仓位并写合法解释。

条件监控动作消费：

消费 `conditional_monitor` 学习：条件候选是否值得继续监控、触发要求是否需要加强、未部署候选是否只保留观察。

影响：是否保留条件开仓意图、是否要求盘中触发、是否打回观察。

执行触发动作消费：

消费 `execution` 学习：trigger/profile 历史质量，例如 breakout、pullback、intraday confirmation 的有效性。

影响：trigger/profile 校准、触发要求、执行画像；不能直接生成交易权限，不能直接进入新资金 rank。

### 4.4 怎么消费与联通

- 先由 `alpha_setup` 提供产品级和 action-value 学习。
- `pm_signal_fusion` 把学习转成证据质量、trigger/profile、产品差异化校准。
- `final_action_semantics` 判断学习属于哪个生命周期，防止混用。
- `pm_contract_builder` 把被消费的学习影响写入最终合约 trace。
- 如果是新增风险动作，`open/add` 学习再交给第 5 步进入全市场 rank。
- 如果是非新增风险动作，学习直接影响持仓、减仓、退出或条件监控决策，然后进入第 6 步生成最终合约。

一句话：第四步不是排名，也不是下单；它只把不同生命周期的学习接到对应决策口，确保 `open/add` 影响新资金，`hold` 影响持仓，`reduce/exit` 影响释放资金，`execution` 只影响 trigger/profile。

## 5. 新增风险进入全市场资金 rank 与资金部署

这一点只处理新增风险动作：

- `open`
- `open_probe`
- `open_real`
- `add`
- `scale`
- `increase`
- `reverse`
- `conditional open`

非新增风险动作不进这里。

### 5.1 调用工具/模块

使用：

- `pm_full_market_capital_deployment`
- `alpha_setup`
- `pm_signal_fusion`
- `final_action_semantics`
- `pm_contract_builder`
- `pm_contract_self_check`

评分配置读取：

- `src/config/rank_score_policy.yaml`

该配置只控制 `rank_score` 分项权重和资金效率小修正，不创建交易权限，不改变仓位参数，不覆盖 `0.008` probe、`20%` 总资金占用率或 `0.5` 净敞口红线。

### 5.2 唯一原则

`rank=1` 永远表示：当天全市场最值得占用资金、最可能盈利的产品机会。

排名越靠前，不是“字段更好看”，而是综合判断它更可能赚钱。

资金层级不由 rank 自动改变：试探仓仍是试探仓，正常仓仍是正常仓，放大仓仍是放大仓。

### 5.3 排名怎么决定

`rank_score` 应该由这些部分组成：

- 当前结构化证据强度
- 三类分析师方向一致性
- `setup` 清晰度
- `trigger` 明确度
- `invalidation` 明确度
- 冲突程度
- 产品级 profile 支持度
- 同产品/方向/setup/trigger/evidence_combo 历史表现
- `open/add` action-value 学习修正
- execution/trigger 质量修正
- 资金效率修正
- 风险和失效边界惩罚

### 5.4 冷启动怎么排

没有足够学习记录时，先靠冷启动证据质量排：

- 三类分析师越一致，越靠前
- 证据越强，越靠前
- `setup` 越清楚，越靠前
- `trigger` 越客观可判断，越靠前
- `invalidation` 越明确，越靠前
- 冲突越少，越靠前
- 产品 profile 越支持，越靠前

这时 rank 解决的是：第一批最值得小仓试探的是谁。

### 5.5 有学习记录后怎么排

有历史 episode / action-value 后，rank 必须被强化学习修正：

- 同类 `open/add` 赚钱：提高 rank
- 同类 `open/add` 亏钱：降低 rank
- 同类 `trigger` 有效：提高 trigger/profile 分
- 同类 `trigger` 失效：降低 trigger/profile 分
- 同类 `deployment outcome` 好：提高真实资金或放大资金候选资格
- 同类 `entry quality` 差：降低 rank 或保留观察

这时 rank 解决的是：哪些产品已经从“看起来好”变成“历史证明更可能赚钱”。

### 5.6 资金层级天然顺序

资金层级进入同一套 rank，但层级有天然优先级：

- `alpha_scale`：最高，因为反复验证有 alpha
- `real_budget_entry`：第二，因为已有学习和证据支持正常交易
- `exploration_probe`：第三，因为只是小仓试探

所以同等条件下：

- `alpha_scale` 排在 `real_budget_entry` 前面
- `real_budget_entry` 排在 `exploration_probe` 前面

但不能因为 probe 排名高，就把它升级成 normal 或 alpha。

也不能因为 alpha 排名低，就把它降成 probe。层级由证据和学习决定，rank 决定同一资金池里的资金优先级。

### 5.7 资金部署怎么挂钩

按 rank 顺序部署：

- `rank=1` 先占用预算
- `rank=2` 再占用剩余预算
- `rank=3` 继续占用剩余预算
- 直到触碰预算线，后续候选还原为 `wait` / `no_rank_or_budget_no_new_exposure`

必须守住两条预算线：

- 总资金占用率不超过 `20%`
- 多空净敞口平衡不超过 `0.5`

`probe=0.008` 也必须守住：

- probe 还是 `0.008`
- 不能因为 rank 高升到 `0.01`
- 不能因为预算紧降到更低
- 预算不够就不部署，不能缩水部署

### 5.8 最终结果

第 5 步输出给第 6 步的是资金部署结果：

- 谁进入 rank
- rank 是多少
- `rank_score` 为什么这样
- 资金层级是什么
- 使用哪个资金比例来源
- 预算是否足够
- 如果预算不够，为什么还原 wait
- 学习如何影响 rank

一句话：第五步就是把所有新增风险候选放进同一个全市场资金池，按“最可能盈利”排序，再按 rank 顺序占用资金；守住 `20%` 总资金占用、`0.5` 净敞口和仓位参数边界，probe 永远还是 `0.008`。

## 6. 生成唯一 final_action_contract

第 6 步是 PM 的最终签出点。它同时回答两件事：

- 这个 `ticker` 最终签出的唯一交易事实是什么。
- PM artifact 里可以输出什么，不能输出什么。

### 6.1 调用工具/模块

使用：

- `pm_contract_builder`
- `final_action_semantics`
- `pm_contract_self_check`
- `alpha_setup`

不调用：

- `pm_full_market_capital_deployment` 重新排名
- Trader / Accountant / Auditor 工具改合约

`pm_full_market_capital_deployment` 的结果如果已经在第 5 步生成，只能作为全市场资金 rank 结果写入合约；第 6 步不能重新排名。

第 6 步最终闸门固定为两道检查：

- `signal_snapshot.pm_six_step_trace.step6_contract_generation_check.ok == true`
- `signal_snapshot.pm_six_step_trace.pm_contract_self_check.ok == true`

其中 `step6_contract_generation_check` 只检查最终合约是否由合法 PM 机制生成；`pm_contract_self_check` 只检查最终合约自身字段、rank/非 rank/Step5 未部署边界和 artifact 污染。两者都不比较 Step2 的 `primary_lifecycle_action_port` 与 Step6 最终合约是否一致。Step2 到 Step6 的变化只能作为 PM 内部 provenance trace，不参与最终合约失败判断。

### 6.2 final_action_contract 回答什么

第 6 步生成的 `final_action_contract` 是 PM 签出的唯一交易事实，必须写清：

- `final_action`
- `current_lots`
- `target_lots`
- `lots_delta`
- `reason_codes`
- `execution_profile`
- `entry_trigger`
- `invalidation`
- `learning_used`
- `evidence_used`
- `capital_deployment`
- `position_sizing_result`

### 6.3 走 rank 的新增风险动作输出什么

如果是 `open` / `open_probe` / `add` / `scale` / `reverse` / `conditional open`，最终合约必须同时带两类 trace。

资金 rank trace：

- `opportunity_rank`
- `rank_source=full_market_capital_deployment`
- `rank_score`
- `rank_input_components`
- `rank_capital_role`
- `capital_layer`
- `capital_ratio_source`
- `rank_reason`

生命周期学习 trace：

- `lifecycle_learning_trace`
- `learning_impact_delta`
- `pm_lifecycle_learning_trace`
- `pm_lifecycle_learning_impact_delta`

走 rank 的合约必须能说明：

- 为什么这个候选这么排
- 分数来自哪些证据和学习
- 学习让分数加了还是扣了
- 资金层级是什么
- 使用哪个资金比例来源
- 最终为什么保留或还原目标手数

### 6.4 不走 rank 的非新增风险动作输出什么

如果是 `hold` / `reduce` / `exit` / `wait` / `close` / `risk_exit`，最终合约不能有新资金 rank。

但只要消费了生命周期学习，必须保留：

- `lifecycle_learning_trace`
- `learning_impact_delta`
- `pm_lifecycle_learning_trace`
- `pm_lifecycle_learning_impact_delta`

非 rank 合约必须能说明：

- 哪些学习被消费
- 哪些学习被拒绝
- 学习影响的是持仓、减仓、退出、等待还是条件监控
- 为什么不进入新资金 rank
- 如果继续持有，是否合法解释不动仓
- 如果减仓或退出，是否是释放资金动作

### 6.5 PM artifact 边界

最终合约和 PM artifact 只能写安全摘要，不能写 Researcher 原始事实对象。

允许写：

- 学习条数
- 生命周期 `lane`
- `scope`
- `status`
- `delta`
- `reason`
- 使用/拒绝摘要
- trace 是否已落合约

禁止写：

- `adaptive_policy_state`
- `strategy_memory`
- `adaptive_policy_scope.policies`
- Researcher 原始策略行
- 原始学习 rows 全量对象

### 6.6 trace 是什么

trace 不是原始研究事实对象。

trace 是 PM 对“自己如何使用学习”的安全摘要，必须能回答：

- 这条合约用了哪个生命周期的学习
- 学习影响了哪个决策口
- 学习影响方向是加分、扣分、保护性持有、释放资金还是触发校准
- 学习是否改变 rank、资金层级、目标手数、持仓解释或触发要求

### 6.7 自检要求

`pm_contract_self_check` 必须检查：

- 最终合约字段完整
- `capital_deployment` 和 `position_sizing_result` 语义完整，不能用空对象冒充事实
- 走 rank 的合约有资金 rank trace 和生命周期学习 trace
- 非 rank 但消费学习的合约有生命周期学习 trace
- Step5 未部署新增风险必须还原为 `wait/hold`、`target_lots=current_lots`、`lots_delta=0`，且不得残留盘中触发执行权限
- PM artifact 没有越界研究事实对象
- `final_action` 与 `current_lots` / `target_lots` / `lots_delta` 一致

`pm_contract_self_check` 不读取 `final_action_contract.evidence_used.contract_lifecycle_self_check`，也不把 Step2 与 Step6 的生命周期差异作为最终失败依据。

### 6.8 2025-03-27 错误提醒

2025-03-27 的 C / HC / M 报错说明：

非 rank 动作如果消费了学习，最终合约里的 `lifecycle_learning_trace` 不能漏掉，也不能在落盘或清理 rank 字段时被误删。

PG 对 `lifecycle_learning_trace_missing` hard fail 是合理的，因为它证明 PM 消费了学习，但最终合约没有留下可审计 trace。

这个错误提醒两条边界：

- 走 rank 的合约必须同时带完整资金 rank 语义和生命周期学习 trace。
- 不走 rank 但消费学习的合约必须保留生命周期学习 trace。

一句话：第 6 步是 PM 的最终签出点；走 rank 的合约必须同时带完整资金 rank 语义和生命周期学习 trace，非 rank 但消费学习的合约必须保留生命周期学习 trace；所有 PM artifact 只能写安全摘要，不能搬 Researcher 原始事实对象。
