# AgentQuant 工作说明书

本文件是 AgentQuant 项目的 AI 工作流程说明书。每次处理本项目任务时，无论是改代码、查 bug、分析回测、更新文档，还是回答“现在该怎么办”，都要先按这里的流程校准方向。

## 0. 运行环境硬约束

- 所有 AgentQuant 程序、测试、检查脚本、回测、评估、模拟盘、数据库脚本，都必须在 conda 环境 `deepfund` 下运行。
- 推荐 Python 路径：`C:\ProgramData\miniconda3\envs\deepfund\python.exe`
- 不要用 `base` 环境、系统默认 Python 或未确认环境运行本项目。
- 临时脚本如确实需要，只能放在 `D:\research\Workshop\`，任务结束后删除；不要把临时审计脚本长期留在 AgentQuant 仓库内。

## 1. 最高目标

AgentQuant 的目标只有一个：让多智能体系统自动生成的期货交易策略，在回测和模拟盘中尽可能实现稳定正收益，并能在真实期货业务链路中一比一复刻。

系统设计必须保持主动 alpha 迭代导向：分析师、PM、Auditor、Trader、Researcher 不是用来堆叠被动限制的，而是要基于行情时序、基本面、新闻、执行反馈和历史学习，主动发现可交易优势，验证其正期望，把合格机会落实到仓位和交易出口，并把结果反哺下一轮策略。限制、封顶、观察、probe 只能服务于风险识别和学习验证，不能取代寻找收益机会本身。

所有工作都必须服务于这个目标。代码更复杂、机制更多、日志更详细、归因更漂亮，都不等于目标达成。判断一次工作是否有价值，要看它是否直接或间接改善：

- 净收益、收益稳定性、最大回撤；
- 胜率、盈亏比、交易成本后收益；
- 资金利用率和实战部署意义；
- 正期望机会识别、合理落仓、及时退出、盈利持仓保护；
- 回测策略能否在模拟盘和真实执行链路复刻。

## 2. 工作底线

- 不要把“不交易”当作赚钱。软风险应优先限仓、probe、复核或学习，不应机械归零。
- 不要用兜底逻辑掩盖错误。数据、接口、账务、执行、结算、日志异常必须查根因。
- 不要用未来数据污染当日决策。Phase4/Researcher 的学习结果只能影响未来交易日。
- 不要把历史亏损写成死规则、品种黑名单或僵硬限制。历史经验要可验证、可反驳、可撤销。
- 不要随意改账务、手续费、滑点、评估、画图、交易事实日志，除非发现确定性错误或用户明确要求。
- 不要扩大修改范围。每次修改都要能说清楚它如何服务收益、执行闭环或系统正确性。
- 不要把回测当成查系统 bug 的工具。回测是检验策略在真实市场环境下是否盈利；链路、数据、出口、执行、结算问题应尽量在回测前通过代码和测试查清。

## 3. 每次任务的固定流程

### 3.1 先判断任务类型

- **代码修改任务**：先读 `docs/work_log.md`、相关代码和配置，确认不是重复修改、不是和既有修改冲突，再改；改后必须测试。
- **回测结果分析**：先查 PnL、品种、动作、PM 推荐、scorecard/action-value、分析师信号、执行、结算、研究记录，再下结论。
- **能否回测判断**：先确认 `pre_backtest_acceptance`、`system_invariant_audit`、核心链路回归测试、LLM/API、交易日窗口、PM 出口、Trader 触发、资金边界、研究闭环、账务结算无已知阻塞。
- **方案讨论**：先给完整逻辑和风险，再决定是否改；不要一上来动代码。
- **文档/配置整理**：必须和现有代码语义对齐，不能只写漂亮说明。

### 3.2 必查问题

每次做判断前，至少问自己：

- 这件事是否服务“策略稳定盈利和可部署”？
- 依据来自代码、数据库、日志、配置、图表、文档，还是只是推测？
- 是否可能压死交易、过拟合、引入未来数据污染或新增兜底？
- 是否会破坏 Phase1-Phase4 主链、Trader、Accountant、评估、画图或日志？
- 修改后是否会真的影响最终交易出口，还是只写了字段/日志？
- 是否已经有工作日志记录过同类修改？如果有，本次是补真实路径覆盖、修明确断点，还是无证据重复改？

## 4. 当前业务链路

完整链路是：

Protocol Governor / preflight / acceptance / system invariant  
→ 确认环境、配置、协议、交易日窗口、学习边界和系统不变量可用  
→ 不生成交易权限、不改仓位、不执行订单  
→ 
数据/行情/基本面/新闻  
→ 分析师结构化输出证据  
→ PM 生成 scorecard 和 action evidence  
→ PM 最终交易权限出口  
→ Auditor 硬风险审计  
→ Trader 按授权计划执行  
→ Accountant/settlement 结算  
→ Researcher 分动作学习  
→ 下一轮分析师和 PM 读取学习结果

`protocol_governor` 是控制组旁路治理，不是新的交易智能体。它只能做能力边界、结构化契约、preflight、pre_backtest_acceptance、system_invariant_audit、artifact lineage、memory quality 和 action-preference 落仓审计；不能替代 PM 决策，不能替代 Auditor 风控，不能替代 Trader 执行。

### 4.1 分析师怎么用

分析师不再投票开仓，而是分工生产证据。

- **技术面分析师**：负责日频触发、入场时机、价格位置、趋势/震荡状态、失效边界。
- **基本面分析师**：负责供需、库存、基差、产业链、中期背景，输出 support/conflict/background。
- **新闻面分析师**：负责事件催化和风险事件。只有当前明确、可执行的催化才可能参与开仓。

静态权重只是冷启动先验，不是开仓逻辑。`analyst_prior_profiles.yaml` 里的权重不能直接生成真实开仓权限。

### 4.2 PM 怎么用证据

PM 不再简单加权 Bullish/Bearish/Neutral。PM 按动作读取证据：

- `open`：技术触发或明确事件催化 + 失效边界 + 市场确认；
- `hold`：趋势/基本面背景仍有效；
- `exit`：触发失败、失效边界、风险事件、止损或时间止损；
- `scale`：正期望 action-value + 当前确认。

最终交易权限必须落到：

- `authority_type`
- `can_open_real_position`
- `can_apply_min_real_floor`
- `max_allowed_margin_ratio`
- `reason_codes`

语义边界：

- `direction_only`：观察/候选，不能直接开仓。
- `watchlist_only`：不能真实开仓。
- `exploration_probe`：允许小试，但不能用真实仓位地板放大。
- `real_budget_entry`：才允许真实仓位、最低真实资金地板和后续放大。

PM 的放大边界：

- 正向 open action-value 只有在 `exact_real_state` 且 reward 来自真实 episode / real trade 时，才能支持 `real_budget_entry` 或 scale。
- `partial_real_state`、`similar_sql_prior`、`shadow_prior` 只能支持候选、probe、复核或观察，不能直接放大真实仓。
- 历史正收益不能绕过当前 tradeable evidence；当前仍需要技术触发或明确事件催化、失效边界和市场确认。
- 近期同 state tail loss、revalidation 失败或 hold/exit 保护信号，必须进入降级、保护、减仓或退出倾向。

### 4.3 Auditor / Trader / Researcher

- **Auditor**：只拦硬风险和部署边界，如保证金、换月、涨跌停、价格异常、硬风控。软风险不能机械压死交易。
- **Trader**：不创造策略，只执行 PM/Auditor 已授权计划。普通开仓需要 intraday 触发；事件立即执行和 VWAP fallback 必须有 PM 授权并可审计。Trader 必须读取结构化 `execution_profile / execution_action_value`，区分 breakout、pullback、VWAP、opening range、event_immediate；未触发必须写清楚原因并形成 execution learning。
- **Researcher**：不只写归因，必须把结果转成下一轮可用经验，且分开学习 `open / hold / exit / execution`。open reward 使用完整交易 episode 评价入场决策；hold reward 评价持仓保护和回吐；exit reward 评价退出是否避免回吐或过早离场；execution reward 评价触发方式、滑点、追价和错过机会。

### 4.4 记忆/RAG 质量边界

轻量 SQL RAG 只返回 compact evidence，不返回长文本，不直接授权开仓。记忆质量分层如下：

- `exact_real_state`：同 ticker/sector/side/setup/regime/action 且来自真实成交 episode，可参与 real_budget_entry / scale。
- `partial_real_state`：真实成交但 state 不完整，只能支持 probe、复核、保护或降级，不能直接放大。
- `similar_sql_prior`：相似历史样本，只能作为弱先验或候选，不得直接生成真实开仓权限。
- `shadow_prior`：影子/未交易观察结果，只能提示观察或研究，不得直接放大。
- `stale_or_conflicted_memory`：过期或冲突记忆只能审计，不参与放大。

所有学习读取必须满足 `source_trading_date / last_sample_date < decision_date`。Phase4/Researcher 同日或未来结果不能影响当日 PM、Auditor、Trader 或分析师 prompt。

## 5. 回测前检查

回测前要先跑固定控制组验收，而不是只靠人工口头检查：

- `python src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --check-llm-auth --json`
- `python src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json`

`backtest.py` 已经接入 `pre_backtest_acceptance`；验收失败时不能进入逐日回测。验收通过只说明系统 readiness，不说明策略一定盈利。

回测前还要尽量通过代码和测试确认：

- 分析师数据调用正确，没有旧数据替代新数据，没有未来数据污染；
- LLM provider、prompt、结构化输出位置正确；
- 回测日期窗口至少包含一个真实交易日；
- PM 最终出口不会被方向观点、静态权重、资金利用逻辑或最小手数绕过；
- Trader 不会在无触发、无授权、无成交条件下开仓；
- 20% 总保证金硬上限生效，probe/真实仓位地板语义一致；
- Auditor、Trader、Accountant、Researcher 主链无阻塞；
- 学习明细和聚合经验保留策略生效，交易事实不自动删除。
- 正向 open action-value 的放大资格必须来自 exact real episode/reward；partial/similar/shadow 不得直接放大。

如果这些存在已知阻塞，不要让用户继续回测。

## 6. 回测后检查

回测后按以下顺序查，不要只看总盈亏：

1. 每日 PnL、品种贡献、手续费、滑点、保证金占用；
2. 是否有 no_trade/watchlist/direction_only 变成真实开仓；
3. 是否有 Trader 未触发成交、追价失败、未成交或错过机会；
4. 是否资金利用率过低、超过 20%，或强机会未放大；
5. 最大亏损交易逐笔追到：分析师信号、PM scorecard/action-value、Auditor、Trader、退出、Researcher；
6. 区分系统 bug、正常业务亏损、信号质量问题、入场/退出问题、资金分配问题、学习闭环问题；
7. 如果链路无 bug 但亏损，重点审计策略本身，而不是反复修系统出口。

## 7. 数据与事实边界

- PandaAI：行情、分钟线、结算、合约与期货衍生数据。
- Finoview 本地 feather：基本面数据，只能从 `data/Fundamental_data/Finoview_data/` 调用。
- 本地新闻：只能从 `data/News_data/Future_news/` 调用。
- `finoview_factor_catalog.yaml` 是本地 feather 字段目录。
- `data_factor_policy_catalog.yaml` 是 PandaAI/Finoview/新闻入口和数据质量策略目录。
- 没有日期列或无法确认时点的数据，不能作为当日决策的强证据；必须降级、标注或阻断。

## 8. 配置文件边界

- `src/config/dev.yaml`：运行入口、账户资金硬约束和少量用户调好的弱参。未经用户允许，不要改用户已调好的仓位/资金利用率参数。
- 当前 `dev.yaml` 只允许启用 CodexOpenAI / `gpt-5.5` / medium reasoning；TQXAI / `claude-opus-4-6-1` 只能作为完整注释备用。DeepSeek 和其他 provider 接入能力可以保留在代码层，但不能作为当前 `dev.yaml` 的 active runtime block 混入回测。
- `control_governance.protocol_governor` 只能表达控制组权限边界，不能允许创建交易权限、修改 lots/margin 或执行订单。
- `execution_commission_catalog.yaml`：手续费事实表，不能学习，不能省略。
- `execution_slippage_catalog.yaml`：每品种滑点假设，不允许系统自行乱改。
- `execution_exit_policy_catalog.yaml`：退场冷启动策略，可通过长期表现校准，但不能破坏实盘可复刻。
- `analyst_prior_profiles.yaml`：分析师冷启动先验和适用性参考，不是静态权重开仓规则。
- `portfolio_policy_catalog.yaml`：PM/市场确认/机会质量/组合策略冷启动边界。
- `learning_policy_catalog.yaml`：学习、记忆、neutral 追踪、保留周期和上下文预算。

事实参数和数据源不能当作学习参数随意改。弱参调整必须有回测/研究证据，并说明对收益行为的预期影响。

## 9. 学习与数据库保留

- 交易事实永不自动清理：成交、推荐、结算、PnL、原始信号、完整交易日志。
- 学习明细保留 90 天或 60 个交易日：低价值 learning event、notes、临时上下文、过期 overlay 等。
- 聚合经验保留 180 天：`alpha_setup_profile`、`alpha_setup_action_value`、`adaptive_policy_state` 等 active 或近期更新状态。
- action-value 必须分清 `open / hold / exit / execution`，不能把单日盈亏当作完整入场 reward，也不能把真实亏损 exit 写成 `weak_prior`。
- 正向 alpha 放大必须能追溯到完整 episode reward、精确 state、当前 tradeable evidence 和 PM 最终出口；亏损保护必须能追溯到 tail loss、negative revalidation、hold/exit action preference。
- 自动清理只能在 Phase4/Researcher 之后执行，不能在 Phase1/PM 决策前清理。
- 数据库写满必须停机处理、checkpoint、清理、VACUUM 或归档，不能静默兜底。

## 10. 项目结构索引

- `src/llm/prompt.py`：集中管理静态提示词、prompt builder 和通用输出契约。
- `src/llm/provider.py`、`src/llm/inference.py`：模型 provider、调用和结构化输出入口。
- `src/agents/analysis_team/`：分析师 agent 主流程。
- `src/tools/agent_tools/analysis/`：分析侧工具、数据质量摘要、学习上下文、信号融合辅助。
- `src/agents/control_team/`：控制组 agent。`protocol_governor.py` 只做协议治理、能力边界、回测前验收和系统不变量协调；它不是 PM、Auditor 或 Trader，不能生成交易权限、不能改手数/保证金、不能执行订单。
- `src/tools/agent_tools/control/`：控制组工具，包括 agent capability card、tool access policy、artifact lineage、task lifecycle、memory quality、action-preference 审计、cost budget、preflight、pre_backtest_acceptance、system invariant audit、回测/模拟盘一致性检查。该目录只做协议、验收、审计和观测，不写交易策略。
- `src/agents/decision_team/portfolio_manager.py`：PM 主决策链路和最终交易出口。
- `src/tools/agent_tools/decision/`：资金分配、资格判定、风险/机会评分等 PM 辅助工具。
- `src/agents/execution_team/`、`src/tools/agent_tools/execution/`：Trader、触发、成交、合约、滑点、执行学习。
- `src/tools/agent_tools/research/`：Researcher/Reviewer 工具和学习沉淀。
- `src/database/`：SQLite schema、迁移、artifact 校验和数据库访问。
- `src/apis/`、`src/tools/data_fetch/`：PandaAI、Finoview、新闻等数据源适配和抓取。
- `src/run/`：主运行入口，只保留业务主流程脚本：`backtest.py`、`proposal.py`、`order.py`、`settlement.py`、`validate_phase_flow.py`、`evaluate_config.py`、`plot_config.py`。不要把临时审计脚本长期放在这里。
- `src/run/control/`：控制组命令入口，包括 `pre_backtest_acceptance.py`、`protocol_preflight.py`、`system_invariant_audit.py`。回测前系统验收和真实流水不变量审计放这里，不放在主流程根目录。
- `src/run/research/`：研究/学习回填入口，例如 `bootstrap_alpha_setup.py`。这类脚本服务 Researcher/学习状态初始化，不属于回测主流程入口。
- `src/evaluation/`：收益评估、图表和报告。
- `src/tests/`：确定性单元和回归测试目录。重点入口包括 `test_phase_flow_regression.py`（Phase1-Phase4、PM/Trader/Researcher 主链路）、`test_pre_backtest_acceptance.py`（回测前 10 项系统验收）、`test_system_invariant_audit.py`（真实流水系统不变量）、`test_protocol_governor.py` / `test_protocol_preflight_cli.py`（控制组协议和 CLI）、`test_reviewer_learning.py`（研究学习持久化）、`test_pandaai_api_adapter.py`（PandaAI adapter，真实 API 必须标为 integration 或隔离）。
- `docs/`：项目文档目录。当前各 md 用途如下：
  - `docs/mechanism_multiagents.md`：多智能体职责、输入输出、四阶段脚本与智能体关系。
  - `docs/mechanism_future_trade.md`：期货交易业务机制，包括开平仓、换约、手续费、滑点、保证金、结算、回测与模拟盘边界。
  - `docs/mechanism_data_model.md`：数据与模型调用机制，包括 PandaAI、Finoview、新闻、LLM 调用边界和防未来函数原则。
  - `docs/mechanism_research.md`：记忆、研究、action-value、学习保留和研究闭环机制。
  - `docs/parameter.md`：长期回测期间的参数调节备忘，只记录哪些参数可人工微调、何时调、怎么验收。
  - `docs/pandaia_data_introduction.md`：PandaAI 数据接入说明，解释行情、分钟线、衍生数据、主力映射和相关代码位置。
  - `docs/work_log.md`：Python 行为代码工作日志，只记录实际修改 `.py` 且影响系统行为/测试逻辑的任务。
  - `docs/ppt.md`：演示文稿生成提示词，与系统运行、交易出口和回测验收无直接关系。

动态提示词边界：`prompt.py` 只放提示词文本和 builder。学习上下文怎么查、数据怎么读、PM 怎么算仓位、Trader 怎么成交、Accountant 怎么结算，都必须留在各自业务模块。

## 11. 测试与验收

所有测试和验收都必须用 `deepfund` 环境运行。回测不是系统 bug 探测器；回测前要尽量用确定性测试、控制组验收和系统不变量审计把已知非策略问题挡住。

### 11.1 基础测试

- 语法/导入：`python -m compileall src`
- 单元/回归：`python -m unittest ...`
- 全量确定性测试：`python -m unittest`
- 格式补丁检查：`git diff --check`

普通自检必须是 deterministic tests，可以 fake PandaAI、Finoview、新闻和 LLM。真实 API 测试必须标为 integration，不要混入默认单元测试。

### 11.2 回测前固定验收

回测前的固定验收入口是：

- `python src\run\control\pre_backtest_acceptance.py --config src\config\dev.yaml --check-llm-auth --json`
- `python src\run\control\system_invariant_audit.py --config src\config\dev.yaml --local-db --json`

`src/run/backtest.py` 已经接入 `pre_backtest_acceptance`，真实回测命令必须先通过该验收才进入逐日回测。该验收只判断系统 readiness，不判断策略收益。

`pre_backtest_acceptance` 固定覆盖 10 项：

- `environment_api`：deepfund、LLM auth、SQLite、assets、PandaAI runtime cache 等环境/API；
- `config_consistency`：当前 dev.yaml 只启用 CodexOpenAI/gpt-5.5，TQXAI 只能作为注释备用；资金边界不漂移；
- `data_time_boundary`：学习日期边界、runtime data cutoff、回测窗口至少包含一个真实交易日；
- `agent_boundaries`：各智能体能力边界不越权；
- `structured_io`：结构化 artifact / capability card 存在且可校验；
- `single_trade_exit`：最终交易真相只能来自 final contract / final authority；
- `trader_trigger_parity`：Trader 不能无 PM 授权或无 intraday 触发成交；
- `learning_landing`：action-value 必须落成正确动作偏好，正向 open 放大必须来自 exact real episode/reward；
- `capital_boundary`：probe 0.008-0.015、总保证金 20% 等资金边界不漂移；
- `audit_explainability`：真实流水、artifact、reason/action-value 审计可解释。

### 11.3 主链路回归

涉及交易链、学习链或控制链时，至少按影响面选择以下测试：

- PM/Trader/Researcher 主链：`python -m unittest src.tests.test_phase_flow_regression.PMExpectancyTradeQualificationRegressionTest src.tests.test_phase_flow_regression.IntradayExecutionRegressionTest`
- 进攻型 alpha 释放整链：`python -m unittest src.tests.test_phase_flow_regression.PMExpectancyTradeQualificationRegressionTest.test_exact_alpha_release_chain_reaches_pm_authority_and_trader_profile`
- 控制组验收链：`python -m unittest src.tests.test_pre_backtest_acceptance src.tests.test_protocol_preflight_cli src.tests.test_system_invariant_audit`
- 研究学习持久化：`python -m unittest src.tests.test_reviewer_learning`
- 智能体契约：`python -m unittest src.tests.test_agent_contracts src.tests.test_protocol_governor`

如果出现新系统不变量失败，必须先补能复刻真实路径的失败测试，再修代码，再跑相关链路测试和全量确定性测试。不要只补一个局部函数测试就说系统链路已打通。

### 11.4 回测后验收口径

新回测生成记录后，先跑 `system_invariant_audit`。如果 audit 失败，结果不是策略收益，必须按系统 bug 处理；如果 audit clean 但收益差，才进入策略层分析，重点看 alpha 放大、亏损 setup 降级、入退场择时、资金利用率和品种/setup 分布。

旧回测记录不能证明新代码已通过；修改后必须用新代码生成的记录再审计。

最终回复必须说明：

- 改了什么；
- 为什么改；
- 如何服务收益、部署或链路正确性；
- 做了哪些测试；
- 哪些风险仍需回测后用策略表现验证。

从 2026 年 06 月 08 日开始，每当完成一个“动代码”任务，也就是针对 Python 代码的修改任务，必须同步更新 `docs/work_log.md`。只有实际修改 `.py` 文件，并且影响系统行为、业务链路、测试验证或工具逻辑的任务才记入日志。纯文档、README、配置 YAML、数据文件、文件改名/删除，以及只改注释或 docstring 且不改变代码行为的任务，不记入该日志。

## 12. 回答用户时的口径

用户最关心的是：现在该怎么办、为什么这么办、这是否服务赚钱、能不能继续回测。

回答必须直接、具体、基于证据。避免：

- “机制已经落地所以没问题”；
- “继续观察”但不给判断；
- “可能保守/可能激进”但不给证据；
- “再做一批优化”但不说明为何服务收益；
- 把用户目标偷换成完善机制。

如果某项修改只能修链路或改善审计，而不能直接证明收益改善，必须如实说明。  
如果无法证明当前信息足够，先查代码、日志、数据库或配置，不要凭印象回答。
