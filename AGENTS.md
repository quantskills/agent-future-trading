# AgentQuant 项目工作手册

本文件是 AgentQuant 的最高开发工作手册。处理本项目时，无论是修改代码、调整系统框架、改配置参数、排查回测、评估业务路径、整理文档，还是回答“现在该怎么办”，都必须先按本手册校准边界和证据。

## 1. 项目目标

AgentQuant 的目标只有一个：让多智能体系统自动生成的期货交易策略，在回测和模拟盘中尽可能实现稳定正收益，并能在真实期货业务链路中一比一复刻。

系统设计必须保持主动 alpha 迭代导向：分析师、PM、Auditor、Trader、Researcher 不是用来堆叠被动限制的，而是要基于行情时序、基本面、新闻、执行反馈和历史学习，主动发现可交易优势，验证其正期望，把合格机会落实到仓位和交易出口，并把结果反哺下一轮策略。限制、封顶、观察、probe 只能服务于风险识别和学习验证，不能取代寻找收益机会本身。

所有工作都必须服务于这个目标。代码更复杂、机制更多、日志更详细、归因更漂亮，都不等于目标达成。判断一次工作是否有价值，要看它是否直接或间接改善：

- 净收益、收益稳定性、最大回撤；
- 胜率、盈亏比、交易成本后收益；
- 资金利用率和实战部署意义；
- 正期望机会识别、合理落仓、及时退出、盈利持仓保护；
- 回测策略能否在模拟盘和真实执行链路复刻。

## 2. 运行环境硬边界

- 所有 AgentQuant 程序、测试、验收、回测、评估、数据库脚本都必须在本地 conda 环境 `deepfund` 中运行。
- 标准 Python 路径是 `C:\ProgramData\miniconda3\envs\deepfund\python.exe`。
- 不要使用 `base` 环境、系统默认 Python 或未确认环境运行本项目。
- 推荐从仓库根目录 `D:\research\AgentQuant` 运行命令；如果从 `src` 目录运行，必须相应调整路径。
- `.env` 保存 API key，不得在回复、日志或文档中泄露密钥内容。
- 临时排查脚本如确实需要，只能放在 `D:\research\Workshop\`，任务结束后删除；不要把一次性脚本长期留在 `src/run`、`src/tests` 或业务模块中。
- 不要执行 `git reset --hard`、`git checkout --` 等会丢弃用户工作的命令，除非用户明确要求。

## 3. 当前系统主链路

AgentQuant 的业务线是一个闭环：

`数据与行情 -> 分析师结构化证据 -> PM 唯一交易契约 -> Auditor 审计 -> Trader 执行 -> Accountant 结算 -> Reviewer/Researcher 复盘学习 -> 下一轮分析师和 PM 使用学习结果`

控制组在主链外做协议、验收、审计和观测：

`protocol_governor / preflight / pre_backtest_acceptance / system_invariant_audit`

控制组不能生成交易权限，不能改手数，不能改保证金，不能替代 PM、Auditor 或 Trader。

## 4. 唯一交易契约原则

策略交易的唯一交易真相是 PM 最终推荐记录中的 `final_action_contract`。

必须保持如下路径：

- 分析师只输出结构化证据，不输出手数、保证金比例或最终交易命令。
- PM 读取分析师证据、当前持仓、资金边界和研究学习结果，生成唯一 `final_action_contract`。
- Auditor 只审计这张契约的合规性、权限、风险边界和是否绕出口；审不过时，PM 必须把最终推荐改成 `hold/wait` 或相应受限动作。
- Trader 只读取最终推荐记录里的 `final_action_contract` 执行；盘中触发只决定成交或不成交，不能改方向、目标手数、变化手数或保证金授权。
- Accountant 只按实际成交和结算价核算，并把 PnL 绑定回对应契约。
- Reviewer/Researcher 只研究这张契约导致的完整 episode 结果，不从草稿或旁路学习。

PM 内部草稿只能是局部计算过程，不能以 `pre_open_plan` 字段落入运行时 artifact；它不是交易真相，不是 Trader 成交来源，不是 Researcher 学习来源，不是审计推导目标手数的来源。

策略单必须是 `contract_type=strategy` 的 `final_action_contract`。换月、强平、风控处置等非策略动作必须走运营或风险事件路径，例如 `source_type=rollover`，独立核算，不得污染 alpha 学习。

## 5. 智能体职责边界

### 5.1 分析师

当前主要启用的分析师是 `technical`、`fundamental`、`commodity_news`。`macroeconomic`、`policy` 如被启用，也必须遵守同一证据边界。

分析师输入：

- 当日可用的价格、成交量、持仓量、结算价、技术指标、基本面、新闻、事件、数据新鲜度；
- 过去交易日的结构化研究记录和相似 state/action 经验；
- 当前配置允许的数据源和数据截止时间。

分析师输出：

- `signal`：方向判断；
- `confidence`：证据置信度；
- `market_regime`：趋势、震荡、高波动等市场状态；
- `entry_trigger` / `exit_hint`：触发和退出提示；
- `trigger_valid`：当前触发是否成立；
- `opportunity_state`：无机会、等待触发、可小仓试、可交易、风险减仓候选；
- `metadata.data_usage_summary`：用了哪些数据，是否新鲜；
- `metadata.reviewer_learning_context`：读取了哪些过去学习；
- `metadata.learning_impact_summary`：学习如何影响今天判断；
- `metadata.action_evidence_contract`：给 PM 使用的结构化证据契约。

分析师不能直接输出最终仓位、手数、保证金比例、交易权限或订单。

### 5.2 PM

PM 是策略交易意图的唯一生成者。PM 输入分析师证据、当前持仓、资金边界、市场确认、研究学习结果和历史同类 state/action 表现，输出最终推荐记录。

PM 必须输出：

- `final_action_contract`：唯一交易契约；
- `authority_type`、`authority_decision`、`open_action_evidence`、`strong_current_evidence`：只能作为 `final_action_contract` 内部权限字段存在，不得另建第二张权限合约；
- `reason_codes`：机器可审计原因码；
- `learning_used`：本次决策使用的学习证据；
- `capital_boundary` / `margin_boundary`：资金边界说明。

PM 不得让静态分析师权重、旧字段、草稿计划、minimum lot、watchlist、direction_only 或相似历史记忆绕过最终契约。

### 5.3 Auditor

Auditor 审计 PM 的唯一契约。它可以否决、降级或要求等待，但不能创造新的交易方向、目标手数或执行方式。

Auditor 输入：

- PM 的 `final_action_contract`；
- 当前账户、保证金、持仓、涨跌停、合约状态、风控边界；
- 必要的 artifact lineage 和 reason codes。

Auditor 输出：

- `audit_verdict`；
- 审计原因码；
- 审计通过后的最终契约状态。

Trader 不直接执行 Auditor 自己生成的新命令；Trader 只执行 PM 最终推荐记录中已经通过审计或被审计修正后的 `final_action_contract`。

### 5.4 Trader

Trader 是执行器，不是策略生成器。

Trader 输入：

- PM 最终推荐记录里的 `final_action_contract`；
- 契约内的 `execution_profile`、`trigger_source`、`entry_trigger`、`invalidation` 等执行字段；
- 当日盘中行情、触发条件、滑点和合约交易规则。

Trader 输出：

- 是否触发；
- 是否成交；
- 成交方向、手数、价格、滑点、手续费；
- 未成交原因；
- execution learning 所需事实。

Trader 可以根据 `execution_profile` 区分突破、回踩、VWAP、开盘区间、事件立即执行等触发方式，但不能改变契约指定的方向、目标手数和变化手数。

### 5.5 Accountant

Accountant 不参与策略判断。它只负责事实核算。

Accountant 输入实际成交、手续费、滑点、保证金规则、结算价和账户余额，输出日度结算、品种 PnL、手续费、保证金占用、权益曲线和评估所需事实。

这些事实会供 Reviewer/Researcher 学习使用，但 Accountant 自身不生成 action-value，也不改变研究结论。

### 5.6 Reviewer / Researcher

Reviewer 复盘交易事实，Researcher 把复盘结果沉淀成下一轮可用学习。

Researcher 必须按动作分账：

- `open`：用完整交易 episode reward 评价开仓是否值得；
- `hold`：用持仓期间回吐、保护利润、风险暴露评价继续持有是否值得；
- `exit/reduce`：用退出后是否避免回吐或是否过早离场评价退出是否值得；
- `execution`：用触发方式、滑点、追价、错过机会评价执行是否有效。

Researcher 输出必须使用固定 `action_preference` 集合：

- `positive_candidate_open`
- `positive_candidate_hold`
- `positive_candidate_exit`
- `positive_candidate_execution`
- `negative_revalidate`
- `negative_hold_revalidate`
- `tail_loss_protect`

非真实成交、shadow、similar SQL、partial state 只能作为弱先验或候选，不得伪装成 exact real action-value。

## 6. 学习与 RAG 边界

研究学习是结构化输入，不是自由文本记忆。

记忆质量分层：

- `exact_real_state`：同 ticker/side/setup/regime/action 且来自真实交易 episode，可参与 real_budget_entry 或 scale；
- `partial_real_state`：真实交易但 state 不完整，只能支持 probe、复核、保护或降级；
- `similar_sql_prior`：相似历史，只能作弱先验；
- `shadow_prior`：影子或未交易观察，只能提示观察；
- `stale_or_conflicted_memory`：过期或冲突记忆，只能审计，不参与放大。

所有学习读取必须满足 `source_trading_date < decision_date`。同日 Phase4/Researcher 或未来记录不得影响当日分析师、PM、Auditor 或 Trader。

学习使用边界：

- 分析师使用学习来校准证据可靠性，不获得交易授权；
- PM 使用学习来调整 open/hold/exit/execution 倾向和仓位资格；
- Trader 使用 execution 学习来选择触发 profile，不改变契约方向和手数；
- Researcher 写入学习，但不能让学习绕过 PM 和 Auditor；
- protocol_governor 只审计学习是否按契约落仓，不参与收益判断。

## 7. 数据与事实边界

- PandaAI：行情、分钟线、结算、合约和期货衍生数据。
- Finoview 本地 feather：基本面数据，只能从 `data/Fundamental_data/Finoview_data/` 调用。
- 本地新闻：只能从 `data/News_data/Future_news/` 调用。
- `finoview_factor_catalog.yaml` 是本地 feather 字段目录。
- `data_factor_policy_catalog.yaml` 是 PandaAI、Finoview、新闻的数据入口和质量策略目录。
- 没有日期列、无法确认时点或超过决策日 cutoff 的数据，不能作为当日强证据。
- `metadata.data_usage_summary` 必须说明数据新鲜度、来源和降级原因。

## 8. 配置边界

主要配置文件：

- `src/config/dev.yaml`：运行入口、账户资金、LLM active block、控制组开关和核心 runtime 配置；
- `src/config/portfolio_policy_catalog.yaml`：PM、机会质量、市场确认、资金部署边界；
- `src/config/learning_policy_catalog.yaml`：学习、记忆、neutral 追踪和上下文预算；
- `src/config/analyst_prior_profiles.yaml`：分析师冷启动先验，不是开仓规则；
- `src/config/data_factor_policy_catalog.yaml`：数据质量与数据源策略；
- `src/config/finoview_factor_catalog.yaml`：本地基本面字段目录；
- `src/config/execution_commission_catalog.yaml`：手续费事实；
- `src/config/execution_slippage_catalog.yaml`：滑点假设；
- `src/config/execution_exit_policy_catalog.yaml`：退出策略冷启动边界。

当前 `dev.yaml` 只允许启用 CodexOpenAI / `gpt-5.5` / medium reasoning，网关为 `http://47.74.0.65`。TQXAI / `claude-opus-4-6-1` 必须保留为完整注释备用。代码层可以保留 DeepSeek 和其他 provider 接入能力，但当前 runtime 配置只保留 Codex 与 TQXAI 两类。

不要无证据改手续费、滑点、结算事实、20% 总保证金硬边界、probe 资金边界和用户已调好的资金参数。

## 9. 开发任务流程

每次代码或配置任务必须按以下顺序执行：

先读 `docs/work_log.md`，确认过去是否已经做过同类修改；再读相关 `.py`、`.yaml/.yml`、测试和必要文档；然后判断问题是系统 bug、策略表现问题、配置问题、数据问题还是文档不一致。

修改前必须说清楚：

- 要解决哪个真实问题；
- 涉及哪些智能体和业务链路；
- 是否会改变交易权限、手数、资金、学习或执行；
- 是否可能压死交易、过拟合或引入旁路；
- 需要哪些失败测试或回归测试证明。

涉及新增或调整字段时，必须先查 `docs/unified_field_semantics.md`。如果已有字段能表达同一语义或功能，必须复用已有字段，不得重复起名；如果确认确实需要新字段，必须在代码、配置、测试或 schema 变更的同一轮同步写入该字段表，明确放置位置和含义，然后才能让新字段进入运行时链路。

修改时必须遵守：

- 不新增兜底逻辑掩盖错误；
- 不用旧字段绕过唯一契约；
- 不让控制组写交易策略；
- 不让分析师给仓位；
- 不让 Trader 创造策略；
- 不让 Researcher 用未来数据或弱先验放大真实仓位；
- 不把一个品种的偶然失败写成全局硬规则。

修改后必须验证：

- 目标测试；
- 相关链路测试；
- 必要时运行 `pre_backtest_acceptance` 和 `system_invariant_audit`；
- 影响面大时运行全量 `python -m unittest`；
- 用 `git diff --check` 检查补丁格式。

## 10. 回测前验收

回测前先跑控制组验收，而不是让回测暴露已知系统 bug。

推荐命令：

```powershell
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --check-llm-auth --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json
```

`pre_backtest_acceptance` 固定覆盖：

- environment_api；
- config_consistency；
- data_time_boundary；
- agent_boundaries；
- structured_io；
- single_trade_exit；
- pm_opportunity_routing；
- trader_trigger_parity；
- learning_landing；
- capital_boundary；
- audit_explainability。

`backtest.py` 已接入回测前验收和逐日累计 `system_invariant_audit` fail-fast。验收通过只表示系统 readiness，不表示策略一定盈利。

## 11. 回测中与回测后判断

如果回测中 `system_invariant_audit` hard fail，必须停止，把结果按系统 bug 处理，不得讨论策略收益。

如果 audit clean 但收益差，才进入策略层分析，重点看：

- 正 alpha 是否被识别；
- 正 alpha 是否从 probe 走向 real_budget_entry 或 scale；
- 亏损 setup 是否快速降级；
- 入场触发是否过慢、过严或错过；
- 退出是否过慢、过早或回吐；
- 资金利用率是否过低；
- 品种/setup 分布是否集中或负期望；
- 分析师是否长期只输出 no_opportunity/watch_for_trigger；
- 学习是否真正改变 PM/Trader 的动作偏好。

旧回测记录不能证明新代码已经 clean；只有新代码生成的新记录通过 audit，才算该路径可信。

## 12. 测试矩阵

常用命令必须使用 deepfund：

```powershell
C:\ProgramData\miniconda3\envs\deepfund\python.exe -m compileall src
C:\ProgramData\miniconda3\envs\deepfund\python.exe -m unittest
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --check-llm-auth --json
C:\ProgramData\miniconda3\envs\deepfund\python.exe src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json
```

关键测试入口：

- `src/tests/test_agent_contracts.py`：智能体结构化契约；
- `src/tests/test_phase_flow_regression.py`：PM、Trader、Researcher 主链路；
- `src/tests/test_pre_backtest_acceptance.py`：回测前 10 项验收；
- `src/tests/test_protocol_governor.py`：控制组边界；
- `src/tests/test_protocol_preflight_cli.py`：preflight 和 backtest 接入；
- `src/tests/test_system_invariant_audit.py`：真实流水系统不变量；
- `src/tests/test_reviewer_learning.py`：复盘和研究学习；
- `src/tests/test_pandaai_api_adapter.py`：PandaAI adapter，真实 API 必须隔离为 integration；
- `src/tests/test_futures_market_rules.py`：期货交易规则；
- `src/tests/test_market_confirmation.py`：市场确认；
- `src/tests/test_phase1_acceleration.py`：Phase1 加速和入口行为。

新发现真实失败路径时，先写能复刻该路径的失败测试，再修代码，再跑目标测试、相关链路测试和必要验收。

## 13. 项目结构索引

- `src/agents/analysis_team/`：分析师 agent；
- `src/agents/decision_team/portfolio_manager.py`：PM 唯一交易契约生成；
- `src/agents/decision_team/auditor.py`：策略契约审计；
- `src/agents/execution_team/trader.py`：Trader 执行最终契约；
- `src/agents/execution_team/accountant.py`：账务和结算；
- `src/agents/research_team/`：Reviewer 和 Researcher；
- `src/agents/control_team/protocol_governor.py`：控制组协议治理，不是交易 agent；
- `src/tools/agent_tools/analysis/`：分析师工具、学习校准和证据合约；
- `src/tools/agent_tools/decision/`：PM 辅助工具；
- `src/tools/agent_tools/execution/`：触发、成交、合约、滑点和执行学习；
- `src/tools/agent_tools/research/`：研究学习、action-value、RAG、episode 归因；
- `src/tools/agent_tools/control/`：能力卡、工具权限、artifact lineage、task lifecycle、memory quality、action-preference 审计、cost budget、preflight、acceptance、system invariants；
- `src/llm/prompt.py`：集中提示词和 prompt builder；
- `src/llm/provider.py`、`src/llm/inference.py`：LLM provider 和推理入口；
- `src/database/`：SQLite schema、迁移、artifact 校验和数据库工具；
- `src/run/backtest.py`：回测主入口；
- `src/run/control/`：控制组命令入口；
- `src/run/research/`：研究初始化和学习相关命令；
- `src/evaluation/`：评估、报告和图表；
- `src/tests/`：确定性测试和回归测试。

## 14. 文档边界

- `docs/work_log.md`：行为代码/配置工作日志；
- `docs/mechanism_multiagents.md`：多智能体职责、边界和协作；
- `docs/mechanism_future_trade.md`：期货交易业务机制；
- `docs/mechanism_data_model.md`：数据与模型调用机制；
- `docs/mechanism_research.md`：研究、记忆、action-value 和学习闭环；
- `docs/parameter.md`：长期参数调节备忘；
- `docs/pandaia_data_introduction.md`：PandaAI 数据接入说明；
- `docs/ppt.md`：演示稿生成提示，不代表运行规则；
- `docs/release_baseline_2026-06-17.md`：本地基线说明。

纯文档说明不能替代代码、测试和真实 audit 证据。文档变更必须和现有代码语义一致。

## 15. 工作日志规则

`docs/work_log.md` 只记录完成后的 `.py`、`.yaml`、`.yml` 行为或运行配置修改。

必须记录的情况：

- 修改业务逻辑；
- 修改智能体输入输出；
- 修改交易契约、审计、执行、结算、学习；
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
