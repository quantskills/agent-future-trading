# AgentQuant 工作日志

本文件是短版开发索引，只记录已完成的 `.py`、`.yaml` 或 `.yml` 修改任务。

不记录纯讨论、纯方案、纯回测分析、纯文档整理、纯数据文件变更、文件归档或仅测试运行。

每条只保留：

- 修改了什么：文件、模块或机制。
- 为什么改：对应的问题。

原始长日志另存为归档文件；归档本身不作为新工作日志条目。

==========2026年06月08日========

（1）新增真实 `dev.yaml` 配置目录展开回归测试。
修改了什么：`src/tests/test_phase_flow_regression.py` 增加配置目录展开测试。
为什么改：确认 analyst、portfolio、learning、data、execution catalog 能展开到运行时字段，且资金参数不被改写。

（2）优化 `dev.yaml` 配置布局，将策略冷启动和审计控制外移到 portfolio catalog。
修改了什么：`src/util/config_normalizer.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：让 `dev.yaml` 保持运行入口职责，复杂策略控制由 catalog 管理，同时保持旧代码读取路径不变。

（3）修复 PM 最终推荐的语义一致性出口。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：防止 PM 写明不交易、无触发、只观察时，后续 probe、最小手数或资金地板仍生成真实开仓。

（4）统一 PM/Trader 最终交易出口语义。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/agents/execution_team/trader.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：让 PM 可见推荐文本由结构化出口派生，避免自然语言推理和 Trader 镜像字段成为独立交易真相源。

（5）统一现有 reason codes 的交易效果解释。
修改了什么：`src/tools/agent_tools/decision/reason_effects.py`、`src/tools/agent_tools/decision/hard_risk_rules.py`、`src/agents/decision_team/portfolio_manager.py`、`src/agents/decision_team/auditor.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：避免 block、cap、probe、watchlist 等原因码分散解释，导致软限制和硬风险互相打架。

（6）补齐已知最终出口原因码的 reason-effect 覆盖测试。
修改了什么：`src/tools/agent_tools/decision/reason_effects.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：避免已知出口原因码运行后才落入 `unknown_trade_effects`。

==========2026年06月09日========

（1）收紧 PM 最终交易出口的结构化证据镜像。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/agents/execution_team/trader.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：让 `final_new_entry_trade_authority` 成为新开仓唯一可执行出口，防止 direction-only、watchlist、no-trade 被资金逻辑推成真实开仓。

（2）贯通分析师 action-evidence/product-context 契约。
修改了什么：`src/tools/agent_tools/analysis/quality.py`、`src/tools/agent_tools/contracts.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：让分析师不只输出方向，而是输出 PM 和 Researcher 可读的结构化交易证据与品种上下文。

（3）补齐 PM 学习闭环、scorecard 层级审计和强弱机会通路验收测试。
修改了什么：`src/tests/test_phase_flow_regression.py`。
为什么改：验证 Researcher action-value 能被 PM 读取并影响候选，且弱方向或无当前证据不能绕过最终出口。

（4）同步 probe 资金口径到回归测试。
修改了什么：`src/tests/test_phase_flow_regression.py`。
为什么改：让测试与当前 probe 资金目标 `0.8%-1.5%` 对齐，避免旧断言误判。

（5）强化学习/action-value 落地到 PM 交易出口与持仓保护。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/decision/capital_allocator.py`、`src/tools/agent_tools/decision/reason_effects.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：解决学习记录写入后没有稳定改变下一轮 authority、lots、margin 或保护退出的问题。

==========2026年06月10日========

（1）修补强冲突 probe 与浮盈 no-continuation 保护落地。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/decision/reason_effects.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：防止强冲突弱确认仍被放成 probe，并让浮盈后确认转弱的仓位能触发保护减仓。

（2）补齐 `business_quality_deployable` 的 reason-effect 审计分类。
修改了什么：`src/tools/agent_tools/decision/reason_effects.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：避免既有 reason code 被误判为 unknown，从而把审计解释遗漏当成交易出口 bug。

（3）接入轻量 SQL 相似 setup action-value 检索。
修改了什么：`src/database/interface.py`、`src/database/sqlite_helper.py`、`src/tools/agent_tools/analysis/learning_context.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：用历史同类 setup 经验服务 alpha 放大和亏损降级，同时要求历史样本早于当前决策日。

（4）接入 execution action-value 与正向 exit action-value。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/execution/intraday_execution.py`、`src/tools/agent_tools/decision/reason_effects.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：让 PM 能把执行经验转成执行 profile，并让正向退出经验参与盈利保护，而不让 Trader 创造交易策略。

（5）修补浮亏 probe revalidation 失败被 cooling-period 改回持有的问题。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：防止保护性减仓/退出被 min-hold 或 cooling-period 误改回继续持有。

（6）收口多层软门控为 PM 最终出口统一仲裁。
修改了什么：`src/tools/agent_tools/decision/reason_effects.py`、`src/tools/agent_tools/decision/pm_capital_policy.py`、`src/util/config_normalizer.py`、`src/config/portfolio_policy_catalog.yaml`、`src/config/learning_policy_catalog.yaml`、`src/tests/test_phase_flow_regression.py`。
为什么改：避免软限制叠乘压死 alpha，让软风险只降级或要求确认，硬风险才直接阻断。

==========2026年06月11日========

（1）把 no-trade shadow 结果桥接进 SQL RAG / alpha setup action-value。
修改了什么：`src/tools/agent_tools/research/researcher_tools.py`、`src/tools/agent_tools/research/alpha_setup.py`、`src/database/sqlite_helper.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_reviewer_learning.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：复用影子机会结果作为弱先验，同时防止 shadow-only 结果直接放大真实开仓。

（2）收紧学习结果的时间边界。
修改了什么：`src/database/sqlite_setup.py`、`src/database/sqlite_helper.py`、`src/tools/agent_tools/research/alpha_setup.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：防止 Phase4/Researcher 同日或未来学习污染当日交易决策。

（3）补齐其余学习/研究读取路径的时间边界。
修改了什么：`src/database/sqlite_setup.py`、`src/database/sqlite_helper.py`、`src/tools/agent_tools/research/reviewer_tools.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：统一为“过去经验影响未来交易”，防止 overlay、digest、hypothesis 等学习路径读取同日或未来信息。

（4）补齐 PM 交易记忆与 strategy_memory 的交易日时间边界。
修改了什么：`src/database/sqlite_helper.py`、`src/database/sqlite_setup.py`、`src/database/interface.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/research/template_prior.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_reviewer_learning.py`。
为什么改：防止 PM prompt 读取同日/未来交易流水或无来源日期的 strategy memory。

（5）收紧 alpha action-value 的 state 归因和真实放大资格。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/database/sqlite_helper.py`、`src/tools/agent_tools/research/alpha_setup.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：避免粗 scope、shadow 或相似经验被误当成真实精确 alpha 来放大。

==========2026年06月12日========

（1）补齐 alpha action-value state 精确归因的遗漏入口。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：防止 direct action-value 或旧 payload 在 state 字段不完整时仍被当成 `exact_real_state` 放大。

（2）把研究记忆落到分析师实时证据质量。
修改了什么：`src/tools/agent_tools/analysis/analyst_learning_calibration.py`、`src/agents/analysis_team/technical.py`、`src/agents/analysis_team/fundamental.py`、`src/agents/analysis_team/commodity_news.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：避免研究记忆只停留在 prompt 文本，让历史学习能确定性校准技术、基本面、新闻证据质量。

（3）集中修复 PandaAI 数据调用稳定性。
修改了什么：`src/apis/pandaai/api.py`、`src/tests/test_pandaai_api_adapter.py`。
为什么改：解决 SDK auth 写入无权限、token 过期、合约代码匹配和硬不可用字段重复请求等环境/API 问题。

（4）同步 PM fallback 提示词到结构化证据与最终出口语义。
修改了什么：`src/llm/prompt.py`。
为什么改：清理旧提示词中“方向直接映射仓位”“静态权重直接给仓位”的旧语义。

（5）修复 PandaAI adapter 自检的类级缓存隔离。
修改了什么：`src/tests/test_pandaai_api_adapter.py`。
为什么改：避免前序测试残留 cache 让 PandaAI 自检假失败。

（6）收束 PM 最终动作契约。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：避免 scorecard、自然语言结论和持仓生命周期各自抢交易出口。

（7）把 action-value 从记录/弱提示升级为可审计动作偏好。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/decision/reason_effects.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_reviewer_learning.py`。
为什么改：解决有 alpha 但放大慢、浮盈回吐和负期望 probe 降级慢的问题。

==========2026年06月13日========

（1）新增控制组 `protocol_governor` 协议治理骨架。
修改了什么：`src/agents/control_team/protocol_governor.py`、`src/agents/control_team/__init__.py`、`src/tools/agent_tools/control/*`、`src/tests/test_protocol_governor.py`。
为什么改：把智能体边界、artifact、记忆质量、action-preference、preflight、执行一致性和探索审计收束为控制组侧车，而不是新增交易门控。

（2）新增 `protocol_preflight.py` 入口。
修改了什么：`src/run/control/protocol_preflight.py`、`src/tests/test_protocol_preflight_cli.py`。
为什么改：让回测前协议/环境自检可显式执行，避免依赖人工记忆。

（3）新增 `cost_budget_audit` 运营成本审计。
修改了什么：`src/tools/agent_tools/control/cost_budget_audit.py`、`src/tools/agent_tools/control/__init__.py`、`src/tools/agent_tools/control/agent_cards.py`、`src/agents/control_team/protocol_governor.py`、`src/tests/test_protocol_governor.py`。
为什么改：限制无效 LLM、PandaAI、SQL/RAG、重试和反思调用，降低运营浪费而不影响交易授权。

（4）新增 `tool_access_policy`。
修改了什么：`src/tools/agent_tools/control/tool_access_policy.py`、`src/tools/agent_tools/control/agent_cards.py`、`src/agents/control_team/protocol_governor.py`、`src/tests/test_protocol_governor.py`。
为什么改：审计各智能体是否只调用所属业务线工具，避免工具越权导致职责漂移。

（5）对齐控制组治理配置与集中化提示词边界。
修改了什么：`src/config/dev.yaml`、`src/llm/prompt.py`、`src/tools/agent_tools/control/agent_cards.py`、`src/tests/test_protocol_governor.py`。
为什么改：让协议治理、提示词和配置都表达同一套智能体边界。

==========2026年06月14日========

（1）把 alpha setup action-value reward 收束到完整交易 episode 优先口径，并修复 preflight CLI 静默失败。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/tools/agent_tools/research/researcher_tools.py`、`src/tools/agent_tools/control/preflight.py`、`src/run/control/protocol_preflight.py`、`src/tests/test_reviewer_learning.py`、`src/tests/test_protocol_preflight_cli.py`。
为什么改：防止只看单日样本收益导致 open/hold/exit/execution 学习归因错位，并让 preflight 真实失败能被 CLI 暴露。

（2）补齐交易流水审计镜像。
修改了什么：`src/agents/execution_team/trader.py`、`src/util/futures_audit.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：让每笔成交的 `audit_payload` 携带最终交易契约和最终新开仓权限，便于逐笔对账。

（3）新增运行期系统不变量审计并接入回测入口。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/run/control/system_invariant_audit.py`、`src/run/backtest.py`、`src/tests/test_system_invariant_audit.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：让回测中生成的非策略 bug 能被 fail-fast 拦住，而不是混进策略收益分析。

（4）整理 `src/run` 入口目录。
修改了什么：`src/run/control/*`、`src/run/research/bootstrap_alpha_setup.py`、相关测试。
为什么改：区分主回测流程、控制审计入口和研究回填入口，避免脚本职责混乱。

==========2026年06月15日========

（1）把 LLM 鉴权探针接入回测前 preflight。
修改了什么：`src/tools/agent_tools/control/preflight.py`、`src/run/control/protocol_preflight.py`、`src/run/control/pre_backtest_acceptance.py`、`src/tests/test_protocol_preflight_cli.py`。
为什么改：防止无效 token 到 Phase1 分析师调用时才暴露。

（2）修复 PM direction-only/watchlist release 覆盖最终新开仓出口的真实路径，并补齐 PandaAI SDK auth cache 初始化防线。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/apis/pandaai/api.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_pandaai_api_adapter.py`。
为什么改：防止弱方向/观察语义被 release 和最小 probe 放成真实开仓，同时保证 PandaAI auth 目录可写。

（3）把 LLM 路由配置一致性纳入 preflight 回归。
修改了什么：`src/tools/agent_tools/control/preflight.py`、`src/run/control/pre_backtest_acceptance.py`、`src/tests/test_protocol_preflight_cli.py`、`src/tests/test_pre_backtest_acceptance.py`。
为什么改：防止 Codex/TQXAI 之外的遗留配置混入当前回测。

（4）修复真实亏损 exit action-value 被写成 `weak_prior` 的学习偏好漏洞。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/tests/test_reviewer_learning.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：让真实亏损退出经验写成保护性动作偏好，而不是弱先验。

（5）新增固定的 `pre_backtest_acceptance` 回测前验收入口。
修改了什么：`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/run/control/pre_backtest_acceptance.py`、`src/tests/test_pre_backtest_acceptance.py`。
为什么改：把回测前必须查什么从口头清单变成控制组可执行契约。

（6）澄清 LLM provider 扩展能力与当前 `dev.yaml` 运行配置边界。
修改了什么：`src/llm/provider.py`、`src/llm/inference.py`、`src/config/dev.yaml`、相关测试。
为什么改：保留 DeepSeek 等 provider 接入能力，但当前运行配置只启用 Codex 与 TQXAI 两类。

（7）补齐运行期系统不变量测试覆盖。
修改了什么：`src/tests/test_system_invariant_audit.py`。
为什么改：把真实失败路径写进审计测试，避免只在回测后首次暴露。

（8）将正向 open action-value 的放大资格从 warning 收紧为系统硬不变量。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：防止 partial/similar/shadow 或非真实 reward 的正向 open 被当作真实放大依据。

（9）把完整 `pre_backtest_acceptance` 接入 `backtest.py`。
修改了什么：`src/run/backtest.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：避免手工验收和真实回测命令脱节。

（10）补齐进攻型 alpha 释放链路端到端回归证明。
修改了什么：`src/tests/test_phase_flow_regression.py`。
为什么改：证明 tradeable evidence、exact state、PM 晋升和 Trader execution profile 能串起来，而不是只分段通过。

（11）把回测日期窗口交易日解析纳入 `pre_backtest_acceptance`。
修改了什么：`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/tests/test_pre_backtest_acceptance.py`。
为什么改：避免周末或无交易日窗口验收通过后才由 `backtest.py` 抛错。

（12）对齐 `pre_backtest_acceptance` 与 `system_invariant_audit` 的验收分类。
修改了什么：`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_pre_backtest_acceptance.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：让 action preference 未落仓在有后续合约时成为硬错误，在尚无后续交易日时只是 warning。

（13）修正 action preference 落仓审计的误杀口径。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：避免弱先验或尚无后续交易日的学习记录被误判为链路失败。

（14）把 `system_invariant_audit` 接入 `backtest.py` 逐日累计审计。
修改了什么：`src/run/backtest.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：实现首个非策略 hard error 当日即停。

==========2026年06月16日========

（1）补齐 alpha setup action-value 的 reward_source 写入。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_reviewer_learning.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：让真实 reward 来源可审计，并兼容已落库旧 payload 的真实 reward 计数。

（2）修正系统不变量对 open 维度弱先验 action-value 的误杀口径。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：允许 `weak_prior_not_action_preference` 作为先验存在，但不得伪装成动作偏好。

（3）收束 `final_action_contract` 为策略成交的唯一执行契约。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/agents/execution_team/trader.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：覆盖 open/reduce/exit/hold 的逐笔成交对账，防止 PM 合约与 Trader 成交翻译不一致。

（4）修复 `system_invariant_audit` 对 `target_lots=0` 的误读。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：避免正常平仓到 0 被误报为合约错账。

（5）修复真实正收益 exit/execution action-value 被写成 `weak_prior` 的学习偏好漏洞。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/tests/test_reviewer_learning.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：让真实 exit/execution 结果进入对应动作偏好，而不是弱先验。

（6）把研究成果收束为按动作分账且带使用边界的可审计契约。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/tools/agent_tools/analysis/learning_context.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/control/system_invariants.py`、相关测试。
为什么改：让 open、hold、exit、execution 各自对应不同 value 和不同使用者，避免研究结果混用。

（7）对齐集中提示词和配置说明到研究成果新口径。
修改了什么：`src/llm/prompt.py`、`src/config/learning_policy_catalog.yaml`、相关测试。
为什么改：避免提示词和配置继续表达“单一泛化记忆”旧口径。

（8）把分析师使用研究成果的运行路径收紧为只读 `signal_calibration`。
修改了什么：`src/tools/agent_tools/analysis/learning_context.py`、`src/tools/agent_tools/analysis/analyst_learning_calibration.py`、相关测试。
为什么改：防止 PM/Trader 动作偏好泄漏进分析师信号，导致分析师越权决定交易。

（9）把 Trader 执行方式收进 `final_action_contract`。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/agents/execution_team/trader.py`、`src/util/futures_audit.py`、`src/tools/agent_tools/control/agent_cards.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_protocol_governor.py`。
为什么改：防止 Trader 从 `pre_open_plan.execution_plan` 读取执行方式，让执行 profile 也属于唯一合约。

（10）切断其他智能体对 PM 草稿执行计划的泄漏口。
修改了什么：`src/agents/execution_team/trader.py`、`src/tools/agent_tools/research/researcher_tools.py`、`src/util/futures_audit.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_reviewer_learning.py`。
为什么改：防止缺少 `final_action_contract` 的策略 recommendation 仍通过旧草稿字段生成成交或学习执行结果。

==========2026年06月17日========

（1）新增分析师 `opportunity_state` 机会状态契约。
修改了什么：`src/graph/schema.py`、`src/llm/prompt.py`、`src/tools/agent_tools/contracts.py`、`src/tools/agent_tools/analysis/quality.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/research/reviewer_tools.py`、`src/tools/agent_tools/research/researcher_tools.py`、相关测试。
为什么改：把分析师输出从方向投票收束为可审计机会状态，避免有触发和失效边界的机会被压成无机会。

（2）补齐分析师学习影响的结构化可解释输出。
修改了什么：`src/graph/schema.py`、`src/llm/prompt.py`、`src/tools/agent_tools/analysis/analyst_learning_calibration.py`、`src/tools/agent_tools/analysis/quality.py`、相关测试。
为什么改：让日志说明历史学习支持什么、反驳什么、今天确认什么、缺什么，以及为什么是当前机会状态。

（3）对齐启用分析师的集中提示词到学习解释字段。
修改了什么：`src/llm/prompt.py`、`src/tests/test_protocol_governor.py`。
为什么改：确保 technical、fundamental、commodity_news 三类 LLM 输出要求与结构化字段一致。

（4）补齐分析师机会识别反例测试。
修改了什么：`src/tests/test_agent_contracts.py`。
为什么改：防止“当前触发 + 失效边界 + 足够证据”被隐藏成 `no_opportunity` 或 `watch_for_trigger`。

（5）补齐三个启用分析师的数据时间边界核查。
修改了什么：`src/tests/test_agent_contracts.py`。
为什么改：防止盘前技术、基本面、新闻分析读取 T 日或未来信息。

（6）彻底切断下游对 PM 草稿 `pre_open_plan` 的交易事实兜底读取。
修改了什么：`src/agents/execution_team/trader.py`、`src/tools/agent_tools/research/researcher_tools.py`、`src/util/futures_audit.py`、`src/tools/agent_tools/control/system_invariants.py`、相关测试。
为什么改：让策略交易事实只能来自顶层 `final_action_contract`、顶层权限、Trader 实际执行和会计结算。

（7）补齐唯一合约里的学习与资本释放诊断。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/research/reviewer_tools.py`、`src/tools/agent_tools/decision/reason_effects.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：把仍需跨智能体读取的学习和资本释放诊断放回 `final_action_contract.learning_used`。

（8）补死策略单非 strategy 合约类型回落到 raw action/lots 的边缘口。
修改了什么：`src/agents/execution_team/trader.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：防止普通策略 recommendation 通过非 strategy contract_type 绕过唯一合约翻译。

（9）对齐集中提示词到“审后唯一合约给 Trader 执行”的边界。
修改了什么：`src/llm/prompt.py`、`src/tests/test_protocol_governor.py`。
为什么改：避免提示词误导 Trader 直接读取分析师证据或 execution action-value。

（10）清理 action-value 旧兼容别名。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：移除 `positive_candidate_add/scale`，统一用 `positive_candidate_open` 表示增加敞口候选。

（11）清理 action-value 与最终合约命名漂移。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tools/agent_tools/control/action_preference_audit.py`、`src/tools/agent_tools/control/agent_cards.py`、`src/tools/agent_tools/analysis/analyst_learning_calibration.py`、`src/llm/prompt.py`、相关测试。
为什么改：让 `weak_prior` 只作为先验，不再写成动作偏好；运行时只承认 `final_action_contract` 加审计结果。

（12）完成运行时语义漂移收口。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tools/agent_tools/decision/reason_effects.py`、`src/tools/agent_tools/analysis/analyst_learning_calibration.py`、相关测试。
为什么改：移除旧 `policy_hint/action_bias` 作为动作语义的读写路径，只允许 `payload.action_preference` 驱动动作偏好。

（13）对齐集中提示词和配置注释到当前唯一合约边界。
修改了什么：`src/llm/prompt.py`、`src/config/portfolio_policy_catalog.yaml`、`src/config/learning_policy_catalog.yaml`、`src/tests/test_protocol_governor.py`。
为什么改：避免提示词或配置继续表达 Trader 直读研究结果、direction-only 可开仓、旧 action-value 词汇等过期口径。

==========2026年06月18日========

（1）新增 PM 释放阻塞诊断并锁定其观测边界。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：解释机会卡在硬阻断、弱确认、失效边界、资本容量或学习证据的哪一层，同时用审计禁止诊断字段携带交易动作字段，避免诊断变成新门控或旁路。

（2）收敛旧字段为兼容镜像并锁定 canonical 字段来源。
修改了什么：`src/tools/agent_tools/research/alpha_setup.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/analysis/signal_fusion.py`、`src/util/futures_audit.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：让 `profile_state_hint` 表达 setup/profile 生命周期，让 `payload.action_preference` 成为唯一动作偏好来源；`action_bias/policy_hint` 仅保留为数据库兼容镜像，不能单独驱动 PM、Trader、Researcher 或审计结论。

（3）切断复盘/研究反馈对旧推荐字段的仓位兜底。
修改了什么：`src/tools/agent_tools/research/reviewer_tools.py`、`src/tests/test_reviewer_learning.py`。
为什么改：防止 `final_action_contract` 已经是 hold/flat 时，复盘和研究反馈仍从 recommendation 顶层旧 `action/lots/target_position_ratio` 恢复方向或目标仓位，污染学习归因。

（4）切断 Researcher alpha setup 写入与 SQL similar prior 的旧语义兜底。
修改了什么：`src/tools/agent_tools/research/researcher_tools.py`、`src/database/sqlite_helper.py`、`src/tests/test_reviewer_learning.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：防止 Researcher 从 recommendation 顶层旧 `action/lots` 推导真实 alpha setup 动作；动作分账改为只按 `final_action_contract.current_lots/target_lots` 推导，同时让 SQL similar prior 顶层 `policy_hint` 固定为 `no_action_preference`，避免弱先验被误读为动作偏好。

（5）收口 PM 合格可交易候选的软门控归零路径。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：防止 `tradeable_setup/tradeable_candidate + 当前触发 + 失效边界` 在无硬风险、无负期望且资金可行时，被多层软门控压成 `wait/llm_neutral`；同时保持 `direction_only`、负期望、唯一合约、Auditor 和 Trader 边界不变。

（6）对齐策略归因报告到唯一合约与动作分账边界。
修改了什么：`src/evaluation/analyze_strategy_attribution.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_strategy_attribution_report.py`。
为什么改：让归因报告只读 `final_action_contract`、释放阻塞诊断和 `learning_used.alpha_setup_action_values`，不再从缺失字段补算交易事实，也不输出可被误解为 PM 规则或风控指令的弱侧建议。
