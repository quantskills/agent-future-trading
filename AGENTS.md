# AgentQuant AI 开发协作手册

本文件是 AI 协助开发 AgentQuant 时必须遵守的最高工作手册。处理本项目时，无论是回答问题、改代码、改配置、改提示词、改文档、排查回测、评估策略表现，还是判断“下一步该怎么做”，都必须先按本手册校准边界、证据和验收路径。

核心原则：`docs/mechanism_multiagents.md` 定义当前启用智能体的固定工作流；`docs/unified_field_semantics.md` 是唯一字段语义来源；`docs/mechanism_research.md` 定义研究、记忆和学习消费边界。AI 协助开发时不能按旧口径、个人推测或局部函数名改系统。

`AGENTS.md` 是辅助开发手册，不是普通机制文档。除非用户本人明确要求修改、对齐或更新本文件，否则不要主动改动它；发现本文件与代码或机制文档不一致时，先向用户说明不一致和建议改法，不直接修改。

## 1. 项目目标

AgentQuant 的目标是让多智能体系统自动生成的期货交易策略，在回测和模拟盘中尽可能实现稳定正收益，并能在真实期货业务链路中一比一复刻。

系统采用多智能体结构，是为了利用 LLM 的信息处理和推理能力，对同一条期货价格时序进行技术面、基本面、新闻面和研究复盘的多维分析，提高对开盘后日频价格走势的预测质量。LLM 只能用于分析师和研究员形成结构化预测证据或结构化研究成果，不能直接决定仓位、手数或最终交易合约。

判断一次开发工作是否有价值，要看它是否直接或间接改善：

- 净收益、收益稳定性、最大回撤；
- 胜率、盈亏比、交易成本后收益；
- 资金利用率和实战部署意义；
- 正期望机会识别、合理落仓、及时退出、盈利持仓保护；
- 回测策略能否在模拟盘和真实执行链路复刻。

机制更多、日志更详细、门控更多，不等于目标达成。硬门控只阻断非策略风险、越权、前视、字段缺失、账务错误和非法合约；软门控用于降级、减分、缩手数、条件监控或补证据，不能层层重复把交易压死。

## 2. 必读机制文档

每次涉及系统实际运行方式的任务，动手前必须读对应文档。这里的“系统实际运行方式”指：运行脚本、阶段顺序、启用智能体、输入输出、LLM 调用、字段读写、数据库落库、工具调用、审计门控、交易执行、结算、复盘和研究学习链路。

“涉及系统实际运行方式的任务”包括：

- 修改代码，例如智能体、工具、运行脚本、审计、测试、数据库读写；
- 修改会影响代码行为的文档、配置或提示词，例如机制文档、字段语义表、prompt、`dev.yaml`；
- 判断或解释系统如何运行，例如谁调用 LLM、谁消费研究信息、交易员是否能读研究库、Phase4 后学习如何运行。

纯文字润色、错别字和不改变机制含义的格式调整，不属于系统实际运行方式任务。

- `docs/mechanism_multiagents.md`：当前启用智能体、固定工作流、LLM 边界、工具边界、研究信息消费边界。
- `docs/unified_field_semantics.md`：唯一字段语义表；新增字段必须先在这里登记。
- `docs/mechanism_research.md`：研究员、复盘员、action-value、记忆读取和未来学习消费机制。
- `docs/mechanism_data_model.md`：数据、模型调用、运行数据边界。
- `docs/mechanism_future_trade.md`：期货交易业务机制。
- `docs/work_log.md`：已完成行为修改记录；避免重复修、反向修和语义漂移。

文档之间发生冲突时，按以下顺序处理：

1. 当前代码事实和测试；
2. `mechanism_multiagents.md` 固定工作流；
3. `unified_field_semantics.md` 字段语义；
4. 其他机制文档；
5. 历史 work log。

如果文档落后于代码事实，先说明不一致，再按当前任务范围同步文档或代码。

## 3. 运行环境硬边界

- 所有 AgentQuant 程序、测试、验收、回测、评估、数据库脚本都必须在本地 conda 环境 `deepfund` 中运行。
- 标准 Python 路径是 `C:\ProgramData\miniconda3\envs\deepfund\python.exe`。
- 推荐从仓库根目录 `D:\research\AgentQuant` 运行命令。
- 不要使用 `base` 环境、系统默认 Python 或未确认环境运行本项目。
- `.env` 保存 API key，不得在回复、日志或文档中泄露密钥内容。
- 临时排查脚本如确实需要，只能放在 `D:\research\Workshop\`，任务结束后删除；不要把一次性脚本长期留在 `src/run`、`src/tests` 或业务模块中。
- 不要执行 `git reset --hard`、`git checkout --` 等会丢弃用户工作的命令，除非用户明确要求。

## 4. 当前固定工作流

当前业务主链只有一条：

```text
数据与行情
-> technical / fundamental / commodity_news 结构化预测证据
-> signal_collector 信号收集员
-> portfolio_manager 投资组合经理唯一 final_action_contract
-> auditor 审计员
-> trader 交易员
-> accountant 会计师
-> reviewer 复盘员
-> researcher 研究员
-> 下一交易日分析师校准或投资组合经理 decision_memory_retrieval
```

当前四阶段运行框架固定为：

| 阶段 | 现实含义 | 运行脚本 | 智能体/主流程 | 输出 |
|---|---|---|---|---|
| Phase1 | 盘前策略生成 | `src/run/proposal.py` | `AgentWorkflow`：技术面分析师、基本面分析师、期货新闻面分析师、信号收集员、投资组合经理 | `final_action_contract`、策略推荐 |
| Phase2 | 开盘后/盘中执行 | `src/run/order.py` | 交易员 | 成交/未成交、`execution_result`、`futures_transactions` |
| Phase3 | 收盘后结算 | `src/run/settlement.py` | 会计师 | `daily_settlement`、PnL、费用、保证金、权益和持仓事实 |
| Phase4 | 收盘后复盘验收 | `src/run/validate_phase_flow.py` | 复盘员 | Phase4 验收、完整交易日志、事实归因、研究输入材料 |
| Phase4 后 | 研究学习 | `src/run/research/researcher_learning.py` | 研究员 | 结构化研究信息，供未来交易日使用 |

回测、模拟盘和实盘复刻共享同一套阶段顺序、字段契约、智能体边界和 no-lookahead 约束。区别只在 Phase2：回测中 `order.py` 单次回放完整交易日；模拟盘用 `order.py --loop` 按真实盘中时间循环检查触发。

旧 `planner` 是封存开发组件，不属于当前启用智能体和固定工作流。`planner_mode=false` 是当前唯一合法运行配置；`planner_mode=true` 必须 fail-fast。

`preflight` 的 LLM auth probe 是环境认证探针，不是协议管理员的交易链路 LLM 调用，也不是 Phase1-Phase4 智能体。

## 5. 启用智能体边界

### 5.1 分析师

启用分析师只有：

- `technical` 技术面分析师；
- `fundamental` 基本面分析师；
- `commodity_news` 期货新闻面分析师。

分析师可以调用 LLM。它们只输出结构化预测证据，核心输出是 `AnalystSignal` 和 `action_evidence_contract`。分析师可以消费本专业校准类结构化研究，用于修正证据解释、触发质量和失效边界。

分析师禁止输出：

- 手数；
- 仓位比例；
- 保证金授权；
- 最终交易动作；
- `final_action_contract`；
- `opportunity_score`、`opportunity_rank`、`capital_allocation_reason`。

`setup_quality_ok=true` 只表示形态值得关注，不代表当前触发成立。`trigger_valid=true/current_trigger_confirmed=true` 才表示当前触发成立。

### 5.2 `signal_collector` 信号收集员

信号收集员属于决策组，文件位置是 `src/agents/decision_team/signal_collector.py`。

职责：

- 读取三类分析师的结构化预测证据；
- 输出 `signal_collection_contract`；
- 保留来源引用、逐条证据、方向、触发状态、证据强弱、冲突、缺失、风险、失效边界；
- 不调用 LLM。

禁止：

- 直接读取研究库；
- 混入历史学习结论；
- 输出 score/rank；
- 输出仓位、手数、交易动作；
- 输出 `final_action_contract`。

### 5.3 `portfolio_manager` 投资组合经理

投资组合经理不调用 LLM。它是唯一策略资金经理和唯一策略交易意图签发者。

固定输入：

- `signal_collection_contract`；
- 账户、持仓、合约信息、市场确认；
- `decision_memory_retrieval` 输出；
- `opportunity_ranking` 输出；
- `position_sizing` 输出；
- 资金与风控配置。

固定输出：

- `FuturesRecommendation`；
- 唯一 `final_action_contract`；
- `learning_used`；
- `opportunity_scorecard`；
- `opportunity_rank`；
- `position_sizing_result`；
- `capital_allocation_reason`。

投资组合经理研究消费入口只有 `decision_memory_retrieval`。投资组合经理不直接查研究表，不直接解析原始研究记录，不直接用空历史覆盖真实历史。

投资组合经理的确定性工具链固定为：

```text
signal_collection_contract
-> decision_memory_retrieval
-> opportunity_ranking
-> position_sizing
-> portfolio_manager 签发 final_action_contract
```

`decision_memory_retrieval`、`opportunity_ranking`、`position_sizing` 不能签发 `final_action_contract`。最终交易什么、交易多少，只能由投资组合经理根据工具输出和规则写入唯一合约。

### 5.4 `auditor` 审计员

审计员不调用 LLM，不直接消费研究记录。

输入：

- 投资组合经理的 `final_action_contract`；
- 账户；
- 持仓；
- 保证金；
- 数据质量；
- 硬风险边界。

输出：

- `audit_verdict`；
- hard/soft risk reasons；
- 审计 payload。

审计员只审合约，不改方向、不改手数、不新建合约。研究记忆只能通过投资组合经理的评分、排序、手数计算和唯一合约间接影响审计对象。

### 5.5 `trader` 交易员

交易员不调用 LLM，不直接读取研究库、action-value、`strategy_memory` 或 `adaptive_policy_state`。

输入：

- 审计通过的 `final_action_contract`；
- 合约化执行触发规则；
- 盘中行情；
- 执行配置。

输出：

- 成交/未成交；
- `execution_result`；
- `execution_learning_trace`。

交易员只能按合约中的 `current_lots/target_lots/lots_delta/final_action` 和 `execution_profile/entry_trigger/requires_intraday_confirmation/can_execute_without_intraday_trigger` 执行。交易员不能按 `opportunity_score`、`opportunity_rank`、`learning_used` 或研究记录下单、放宽触发、改方向或改手数。

执行触发机制的迭代路径固定为：

```text
交易员 execution_result / execution_learning_trace
-> 复盘员 Phase4 factual validation
-> 研究员 structured execution learning
-> 下一交易日投资组合经理 decision_memory_retrieval
-> 投资组合经理将 execution_profile / entry_trigger 写入 final_action_contract
-> 交易员执行合约化触发规则
```

### 5.6 `accountant` 会计师

会计师不调用 LLM。它只按成交、持仓、结算价、手续费、滑点、保证金率和合约乘数入账。

会计师禁止：

- 用 LLM 调账；
- 用学习改账；
- 生成交易动作；
- 写最终 action-value。

### 5.7 `reviewer` 复盘员

复盘员不调用 LLM，不触发研究员学习，不写最终 action-value。

职责：

- 验证 Phase1-3 是否完整；
- 输出 Phase4 验收、完整交易日志、事实归因、研究输入材料；
- 复盘投资组合经理合约、交易员执行、会计师结算和学习使用痕迹；
- 区分系统非策略问题和策略表现问题。

复盘员可以给研究员提供事实材料，但未来学习由 `researcher_learning.py` 和研究工具写入。

### 5.8 `researcher` 研究员

研究员可受限调用 LLM。研究员只在 Phase4 验证完成后运行，入口是 `src/run/research/researcher_learning.py`。

输出必须是结构化研究成果：

- `alpha_setup_action_value`；
- `alpha_setup_profile`；
- `adaptive_policy_state`；
- 分析师校准类研究；
- 交易决策类 action-value；
- 执行学习；
- 排序偏好和研究反馈。

研究员禁止：

- 生成当天交易指令；
- 修改投资组合经理手数；
- 修改交易员权限；
- 直接修改合约；
- 输出只供下游消费的自由文本研究结论。

### 5.9 `protocol_governor` 协议管理员

协议管理员不调用 LLM，不参与交易消费，不评价收益。

职责：

- 契约覆盖；
- 机制断链；
- 系统不变量；
- 回测前/每日非策略风险报告；
- 字段、提示词、配置、测试和机制文档一致性检查。

发现 hard error 时，应阻断回测或阻断收益评价。

## 6. 唯一交易契约原则

策略交易的唯一交易真相是投资组合经理最终推荐记录中的 `final_action_contract`。

必须保持如下路径：

- 分析师只输出结构化预测证据；
- 信号收集员只输出统一结构化预测证据包；
- 投资组合经理使用 `decision_memory_retrieval`、`opportunity_ranking`、`position_sizing` 后签发唯一合约；
- 审计员只审这张合约；
- 交易员只执行审计通过后的这张合约和合约化触发规则；
- 会计师只按成交和结算事实入账；
- 复盘员只复盘事实；
- 研究员只输出未来可用结构化学习。

禁止以下旁路：

- 投资组合经理直接把分析师自由文本当交易依据；
- 信号收集员直接读研究库；
- 投资组合经理绕过 `decision_memory_retrieval` 直接解析研究记录；
- `decision_memory_retrieval`、`opportunity_ranking`、`position_sizing` 生成 `final_action_contract`；
- 审计员、交易员、会计师、复盘员、研究员改写投资组合经理的交易方向或目标手数；
- 复盘员入口调用研究员学习或任何 LLM 研究函数；
- 交易员使用 `opportunity_rank`、`opportunity_score`、`learning_used` 或研究记录作为下单权限；
- 任何 LLM 自由文本成为交易权限、仓位依据、审计依据、结算依据或下游研究 action-value。

策略单必须使用 `source_type=strategy`。换月、强平、风控处置等非策略动作必须走运营或风险事件路径，例如 `source_type=rollover`、`source_type=forced_risk`，独立核算，不得污染 alpha 学习。

## 7. 字段、提示词和工具命名规则

字段语义以 `docs/unified_field_semantics.md` 为唯一来源。

- 已有字段能表达同一语义时，必须复用已有字段。
- 确认需要新字段时，必须同一轮同步字段表、生产端、消费端、提示词、测试和契约覆盖闸门。
- `payload`、`payload_json`、`artifact_json` 等只允许作为结构化容器；容器里的业务字段仍必须属于统一字段语义表。
- 字段新增缺任一同步项，都视为语义漂移。

提示词集中在 `src/llm/prompt.py` 管理。涉及智能体输入、输出、禁止项、字段语义、LLM 权限边界的改造，必须同步检查提示词。

不调用 LLM 的智能体和工具不得新增提示词入口。旧提示词入口如仍存在，改造时必须删除。

工具目录按功能边界分类，不按智能体名字分类：

- `src/tools/agent_tools/analysis`：分析侧业务工具；
- `src/tools/agent_tools/decision`：决策侧业务工具；
- `src/tools/agent_tools/execution`：执行侧业务工具；
- `src/tools/agent_tools/research`：研究侧业务工具；
- `src/tools/agent_tools/control`：控制侧治理工具；
- `src/tools/common`：跨智能体公共基础能力，例如 `contracts.py` 和 `runtime_setup.py`；
- `src/util`：更底层的通用基础设施。

`agent_tools` 下的工具必须按具体功能命名，不能按智能体名命名。禁止新增 `*_tools`、`pm_*`、`trader_*`、`reviewer_tools`、`researcher_tools` 这类泛称或角色名工具。

## 8. 数据与事实边界

- PandaAI：行情、分钟线、结算、合约和期货衍生数据。
- Finoview 本地 feather：基本面数据，只能从 `data/Fundamental_data/Finoview_data/` 调用。
- 本地新闻：只能从 `data/News_data/Future_news/` 调用。
- `finoview_factor_catalog.yaml` 是本地 feather 字段目录。
- `data_factor_policy_catalog.yaml` 是 PandaAI、Finoview、新闻的数据入口和质量策略目录。
- 没有日期列、无法确认时点或超过决策日 cutoff 的数据，不能作为当日强证据。
- `data_usage_summary` 必须说明数据新鲜度、来源和降级原因。

所有学习读取必须满足 `source_trading_date < decision_date`。同日 Phase4、研究员或未来记录不得影响当日分析师、投资组合经理、审计员或交易员。

## 9. 开发任务流程

### 9.1 先定义任务目标

动手前必须先判断任务类型：

- 修非策略 bug；
- 解决不交易；
- 提升收益；
- 提高资金利用率；
- 优化学习闭环；
- 对齐文档、提示词或配置；
- 整理工具目录或智能体边界。

不同目标不能混用同一套修法。尤其不能把所有问题都处理成“加门控、加限制、少交易”。

### 9.2 先读上下文

修改或判断前必须读：

- `docs/work_log.md`；
- `docs/mechanism_multiagents.md`；
- `docs/unified_field_semantics.md`；
- 相关 `.py`、`.yaml/.yml`、提示词、测试和机制文档；
- 最近回测记录和系统审计结果，如果任务与回测表现有关。

读完后必须能说清楚：问题是系统 bug、策略表现问题、配置问题、数据问题、学习问题，还是文档口径问题。

### 9.3 沿完整链路排查

不得只盯单个函数、单个字段或单个智能体。至少沿这条链路核对：

```text
分析师证据
-> signal_collector
-> 投资组合经理工具链与唯一合约
-> 审计员审计
-> 交易员执行
-> 会计师结算
-> 复盘员复盘
-> 研究员学习
-> 回测前/每日验收
```

凡是只写日志、分数、原因、诊断或报告，但没有影响真实合约或明确不交易原因的修改，只能算解释增强，不能算交易链路修复。

### 9.4 修改边界

修改时必须守住：

- 不新增兜底逻辑掩盖错误；
- 不用旧字段绕过唯一合约；
- 不让控制组写交易策略；
- 不让分析师给仓位；
- 不让信号收集员读研究库；
- 不让投资组合经理绕过 `decision_memory_retrieval` 读研究；
- 不让审计员消费研究记录改交易权限；
- 不让交易员创造策略、方向、目标手数、保证金权限或研究触发权限；
- 不让复盘员调用 LLM、触发研究员学习或写最终 action-value；
- 不让研究员用未来数据、弱先验或候选偏好直接放大真实仓位；
- 不把一个品种、一个窗口或一次偶然失败写成全局硬规则；
- 不默认通过新增门控解决收益问题。

新增限制前必须说明它是在提升机会排序、资金迁移、退出保护、风险识别，还是单纯减少交易。单纯减少交易不能被当成策略优化。

### 9.5 测试与验收

如果是修真实失败路径，优先写能复刻该路径的失败测试，再修代码。测试必须覆盖真实入口和真实链路，不能只用绕过主流程的手工构造样例证明局部函数正确。

修改后按影响面选择验证：

- 目标测试；
- 相关链路测试；
- `compileall`；
- `contract_coverage_audit`；
- `pre_backtest_acceptance`；
- `system_invariant_audit`；
- `mechanism_effectiveness_audit`；
- `git diff --check`。

影响面大时运行全量 `python -m unittest`。

### 9.6 交付结论

完成后必须给出三个结论：

- 实际交易链路是否改变，具体改变到哪一层；
- 是否存在压死交易、资金利用率下降或新旁路风险；
- 下一轮回测应重点观察哪些指标和非策略问题。

## 10. 回测前验收

回测前先跑控制组验收，而不是让回测暴露已知系统 bug。

推荐命令：

```powershell
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --check-llm-auth --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\contract_coverage_audit.py --repo-root . --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\mechanism_effectiveness_audit.py --config src\config\dev.yaml --local-db --json
```

`contract_coverage_audit` 是版本级只读闸门，固定检查核心契约是否有 producer、consumer、audit、test、字段表、配置、提示词和机制文档覆盖，并要求关键智能体边界有 producer-to-consumer 保真测试。它不读收益、不写 DB、不改交易。

`pre_backtest_acceptance` 固定覆盖：

- environment_api；
- config_consistency；
- data_time_boundary；
- agent_boundaries；
- structured_io；
- contract_coverage；
- single_trade_exit；
- pm_opportunity_routing；
- trader_trigger_parity；
- learning_landing；
- capital_boundary；
- audit_explainability。

验收通过只表示系统 readiness，不表示策略一定盈利。

## 11. 回测中与回测后判断

如果 `system_invariant_audit` 或 `mechanism_effectiveness_audit` 出现 hard fail，必须停止，把结果按系统 bug 或机制断链处理，不得讨论策略收益。

`mechanism_effectiveness_audit` 必须按交易生命周期场景判断：

- 开仓/加仓看学习是否进入 score/rank 和唯一合约；
- 条件监控看盘中触发或未触发结果；
- 持仓/减仓/退出看学习是否落到目标手数下降、退出/减仓动作或明确继续持有解释；
- 不能用开仓评分规则误杀已经正确退出的合约。

`mechanism_effectiveness_audit` 的 diagnostic 不停止回测；它只说明机制已连接但效果差，需要进入策略层分析。

如果两类 audit 都 clean 但收益差，才进入策略层分析。

## 12. 测试矩阵

常用命令必须使用 deepfund：

```powershell
C:\ProgramData\miniconda3\envs\deepfund\python.exe -m compileall src
C:\ProgramData\miniconda3\envs\deepfund\python.exe -m unittest
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\contract_coverage_audit.py --repo-root . --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --check-llm-auth --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\mechanism_effectiveness_audit.py --config src\config\dev.yaml --local-db --json
```

关键测试入口：

- `src/tests/test_agent_contracts.py`；
- `src/tests/test_phase_flow_regression.py`；
- `src/tests/test_decision_workflow_tools.py`;
- `src/tests/test_pre_backtest_acceptance.py`；
- `src/tests/test_protocol_governor.py`；
- `src/tests/test_protocol_preflight_cli.py`；
- `src/tests/test_system_invariant_audit.py`；
- `src/tests/test_mechanism_effectiveness_audit.py`；
- `src/tests/test_contract_coverage_audit.py`；
- `src/tests/test_reviewer_learning.py`；
- `src/tests/test_pandaai_api_adapter.py`；
- `src/tests/test_futures_market_rules.py`；
- `src/tests/test_market_confirmation.py`；
- `src/tests/test_phase1_acceleration.py`。

新发现真实失败路径时，先写能复刻该路径的失败测试，再修代码，再跑目标测试、相关链路测试和必要验收。

## 13. 项目结构索引

- `src/agents/analysis_team/`：技术面、基本面、期货新闻面分析师；
- `src/agents/decision_team/signal_collector.py`：信号收集员；
- `src/agents/decision_team/portfolio_manager.py`：投资组合经理，唯一交易合约签发；
- `src/agents/decision_team/auditor.py`：审计员；
- `src/agents/execution_team/trader.py`：交易员；
- `src/agents/execution_team/accountant.py`：会计师；
- `src/agents/research_team/reviewer.py`：复盘员；
- `src/agents/research_team/researcher.py`：研究员；
- `src/agents/control_team/protocol_governor.py`：协议管理员；
- `src/agents/control_team/planner.py`：封存开发组件，当前 workflow 不启用；
- `src/tools/agent_tools/analysis/`：分析侧工具；
- `src/tools/agent_tools/decision/`：决策侧工具；
- `src/tools/agent_tools/execution/`：执行侧工具；
- `src/tools/agent_tools/research/`：研究侧工具；
- `src/tools/agent_tools/control/`：控制侧治理工具；
- `src/tools/common/`：跨智能体公共基础能力；
- `src/llm/prompt.py`：集中提示词和 prompt builder；
- `src/run/backtest.py`：回测主入口；
- `src/run/control/`：控制组命令入口；
- `src/run/research/researcher_learning.py`：研究学习入口；
- `src/tests/`：确定性测试和回归测试。

## 14. 文档边界

- `docs/work_log.md`：行为代码/配置工作日志；
- `docs/mechanism_multiagents.md`：多智能体固定工作流、边界和协作；
- `docs/mechanism_research.md`：研究、记忆、action-value 和学习闭环；
- `docs/mechanism_data_model.md`：数据与模型调用机制；
- `docs/mechanism_future_trade.md`：期货交易业务机制；
- `docs/unified_field_semantics.md`：唯一字段语义表；
- `docs/parameter.md`：长期参数调节备忘；
- `docs/pandaia_data_introduction.md`：PandaAI 数据接入说明；
- `docs/ppt.md`：演示稿生成提示，不代表运行规则。

纯文档说明不能替代代码、测试和真实 audit 证据。文档变更必须和现有代码语义一致。

## 15. 工作日志规则

`docs/work_log.md` 只记录完成后的 `.py`、`.yaml`、`.yml` 行为或运行配置修改。

必须记录的情况：

- 修改业务逻辑；
- 修改智能体输入输出；
- 修改交易合约、审计、执行、结算、学习；
- 修改测试逻辑；
- 修改控制组工具；
- 修改 runtime 配置。

不记录的情况：

- 纯讨论；
- 纯方案；
- 纯回测分析；
- 纯文档或 README；
- 数据文件变动；
- 文件改名或删除；
- 只改注释或 docstring 且不改变行为；
- 只运行测试或命令。

每条只写两项：

- 修改了什么：文件/模块/机制；
- 为什么改：对应哪个问题。

## 16. 回答用户时的规则

回答必须直接、基于证据、服务项目目标。

不要用“可能”“观察一下”“再小修一下”代替判断。若证据不足，先查代码、配置、数据库、日志或测试。若是系统 bug，明确说是系统 bug；若系统不变量 clean 但收益差，明确进入策略层分析。

不要把机制建设说成收益保证，也不要用“不保证盈利”逃避系统目标。正确说法是：系统链路必须先能一比一复刻交易逻辑；链路 clean 后，亏损才按策略信号、入退场、资金利用、学习效果和品种/setup 分布分析。

用户问“现在该干什么”时，必须给出下一步唯一动作或非常短的决策，不要绕回多套方案。
