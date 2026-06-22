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

==========2026年06月19日========

（1）收紧分析师条件触发句的结构化证据归一化。
修改了什么：`src/tools/agent_tools/analysis/quality.py`、`src/llm/prompt.py`、`src/tests/test_agent_contracts.py`。
为什么改：防止分析师把“如果/等待/需要确认后才交易”的未来条件写成 `trigger_valid=true` 和 `tradeable_candidate`；只有已有当前确认事实时，条件触发才可保持可交易，否则统一落到 `watch_for_trigger`，避免 PM 后续等待被误读成压死交易。

（2）对齐配置注释到条件触发归一化边界。
修改了什么：`src/config/portfolio_policy_catalog.yaml`。
为什么改：明确 `direction_only_new_entry.allow_probe=true` 只是观察/候选语义，不是交易授权；等待确认类 pending 条件必须先归一化为 `watch_for_trigger + trigger_valid=false`，不能被配置注释误读为可交易 setup。

（3）对齐主配置中文注释到当前分析师证据边界。
修改了什么：`src/config/dev.yaml`。
为什么改：在主配置入口明确“如果/等待/确认后才交易”属于 pending 条件，不能被当成当前触发或可交易候选；方向观点和等待触发不能开仓，只有当前触发成立且有失效边界的结构化证据才进入 PM 可交易候选链路。

（4）统一运行时字段语义并切断旧字段读写入口。
修改了什么：`src/tools/agent_tools/analysis/quality.py`、`src/tools/agent_tools/analysis/signal_fusion.py`、`src/agents/decision_team/portfolio_manager.py`、`src/agents/execution_team/trader.py`、`src/tools/agent_tools/research/alpha_setup.py`、`src/tools/agent_tools/research/reviewer_tools.py`、`src/tools/agent_tools/research/learning_contract.py`、`src/tools/agent_tools/research/neutral_accountability.py`、`src/database/sqlite_setup.py`、`src/database/sqlite_helper.py`、`src/evaluation/analyze_strategy_attribution.py`、`src/llm/prompt.py`、`src/config/dev.yaml`、`src/config/portfolio_policy_catalog.yaml`、`src/tests/test_unified_field_migration.py`、`src/tests/test_agent_contracts.py`、`src/tests/test_reviewer_learning.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：旧字段仍可能让分析师触发、PM 释放、Reviewer/Researcher 中性观察和学习输出出现第二套语义；本次把运行时统一到 `action_evidence_contract.trigger_valid`、`opportunity_state`、`action_preference`、`final_action_contract`，旧字段只允许在数据库迁移脚本和迁移测试中出现，避免旧字段继续驱动交易、复盘或学习。

（5）把字段统一检查接入回测前验收和每日系统审计。
修改了什么：`src/tools/agent_tools/control/unified_field_audit.py`、`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_unified_field_migration.py`、`src/tests/test_pre_backtest_acceptance.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：让回测前 `structured_io` 静态扫描生产路径是否重新读写旧字段，让每日 `system_invariant_audit` 扫新生成推荐产物是否泄露旧字段键，避免字段统一只停留在单测而没有进入真实验收链路。

==========2026年06月20日========

（1）修复分析师证据里等待确认文字与触发字段自相矛盾。
修改了什么：`src/tools/agent_tools/analysis/quality.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_agent_contracts.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：防止 `entry_trigger` 明确写着“requires confirmed break after open / without confirmation remain on watch”时，运行时仍把 `trigger_valid`、`action_evidence_contract` 或 `trade_research_contract` 标成当前触发；新增审计 hard fail，后续真实记录再出现“等待确认文字 + trigger_valid=true”会直接停在非策略问题。

（2）彻底拆开 `setup_quality_ok` 与当前触发语义。
修改了什么：`src/tools/agent_tools/analysis/quality.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_agent_contracts.py`、`src/tests/test_system_invariant_audit.py`、`docs/unified_field_semantics.md`。
为什么改：`setup_quality_ok` 只能表示“形态值得关注”，不能推出 `trigger_valid=true`；新增 `current_trigger_confirmed` 作为当前触发事实来源，并让没有当前确认的 probe/tradeable 候选统一回到 `watch_for_trigger`，避免分析师证据脏字段继续让 PM 误判为可交易。

（3）补齐条件触发机会闭环。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/agents/execution_team/trader.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：让 `watch_for_trigger + trigger_valid=false + setup_quality_ok + 明确方向/触发条件/失效边界` 的机会不再被 PM 当普通 wait 丢掉，而是由 PM 写成唯一 `final_action_contract` 的受控条件 probe；Auditor 审同一张合约，Trader 只按合约盘中检查触发，未触发只记录原因，触发后才按合约方向和手数成交。

（4）补齐条件机会字段传递与未完成交易日审计。
修改了什么：`src/tools/agent_tools/analysis/signal_fusion.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_agent_contracts.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：让 `opportunity_scorecard` 稳定携带 `setup_quality_ok/trigger_valid/current_trigger_confirmed/invalidation_present/entry_trigger/opportunity_state/source_analysts`，让 PM 可见但不落仓地记录干净的条件监控候选；同时把 `phase2=running` 等未完成交易日作为 `incomplete_trading_day_phase` hard fail，避免未完成记录混入策略结论或学习。

（5）把未完成交易日 hard fail 接入回测前验收分类。
修改了什么：`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/tests/test_pre_backtest_acceptance.py`。
为什么改：让 `pre_backtest_acceptance` 在回测前把 `incomplete_trading_day_phase` 明确归入 `data_time_boundary` 失败，而不是模糊落到审计解释项，避免带着未完成 phase 记录继续开新回测。

（6）对齐配置目录到条件监控闭环与统一字段语义。
修改了什么：`src/config/dev.yaml`、`src/config/portfolio_policy_catalog.yaml`、`src/config/learning_policy_catalog.yaml`、`src/config/analyst_prior_profiles.yaml`。
为什么改：避免配置注释继续表达“旧字段兼容”“watch_for_trigger 不能进入真实路径”等过期口径；明确 `watch_for_trigger + trigger_valid=false` 不是即时开仓，但干净条件机会可由 PM 写入同一张 `final_action_contract` 的条件监控 probe，Trader 只按合约盘中检查触发。

（7）接通强平/风控运营单的非策略执行与审计隔离。
修改了什么：`src/graph/schema.py`、`src/tools/agent_tools/execution/futures_execution.py`、`src/agents/execution_team/trader.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/util/futures_trade_pairs.py`、`src/evaluation/analyze_strategy_attribution.py`、`src/tests/test_system_invariant_audit.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：字段表已定义 `source_type=forced_risk`，但代码未完整接入；本次让风控运营单独立执行和核算，禁止它使用策略 `final_action_contract` 或开仓，并让策略归因/学习视图排除 `rollover/forced_risk` 等非策略成交，避免强平或运营动作污染 alpha 学习。

（8）锁住换月时点、强平即时执行与换月敞口协调边界。
修改了什么：`src/agents/execution_team/trader.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：防止当天结算后才发现的 `rollover` 同日生效影响当天盘前策略；让 phase2 纸面盘中循环每轮先执行当天 pending `forced_risk`，避免强平/强减只等收盘；并用测试锁住换月恢复敞口必须参考同日 PM 策略目标，同方向才平旧开新，空仓/反向只平旧约。

（9）补齐盘中强平/强减运营单生成入口。
修改了什么：`src/tools/agent_tools/execution/futures_execution.py`、`src/agents/execution_team/trader.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：此前 `forced_risk` 只有 pending 单执行与审计隔离，缺少盘中保证金风险触发器；本次让 Trader phase2 每轮先按盘中价格、账户权益和保证金率扫描风险，超过强平线时生成 `source_type=forced_risk` 的平仓/强减运营单并立即走现有执行链路，同时保持 PM、策略 `final_action_contract`、分析师证据和 alpha 学习边界不变。

（10）对齐协议管理员能力卡到运营风控链路。
修改了什么：`src/tools/agent_tools/control/agent_cards.py`、`src/tests/test_protocol_governor.py`。
为什么改：盘中 `forced_risk` 生成入口接入 Trader 后，协议能力卡需要明确 Trader 可读取 `portfolio_margin_state` 并输出 `forced_risk_operational_recommendation`，但仍不能创建策略交易权限、不能修改策略手数/保证金；避免后续把运营风控单误解成策略 `final_action_contract` 旁路。

（11）收住学习适应层的候选偏好晋升边界。
修改了什么：`src/tools/agent_tools/research/adaptive_policy_safety.py`、`src/agents/decision_team/portfolio_manager.py`、`src/agents/decision_team/auditor.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：把 Researcher 生成的候选偏好与 PM/Auditor 可实际使用的自适应策略分开；候选/观察类学习只能作为先验或受控 probe 线索，不能直接释放 `protect/allow`、放大仓位或绕过 `final_action_contract`，并让系统审计对未验证 release 型自适应偏好 hard fail。

（12）收束 PM 条件机会释放的重复路径。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：`watch_for_trigger` 条件机会不能再混用 `scorecard_current_tradeable_probe_seed`；等待触发的干净机会只进入 `conditional_monitor_probe_seed` 和 `pm_watch_for_trigger_probe_cap`，再由同一张 `final_action_contract` 的 `conditional_trigger_authority` 决定是否交给 Trader 盘中监控。

（13）锁死 Trader raw action/lots 旁路并修复执行学习上下文。
修改了什么：`src/agents/execution_team/trader.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：策略或未知 `source_type` 不能用 recommendation 顶层 `action/lots` 绕过 `final_action_contract`；raw action/lots 只允许 `rollover/forced_risk` 运营单，同时修复执行学习上下文里 `execution_contract` 被精简字段覆盖的问题。

（14）把自适应学习安全过滤接入分析师权重和资金读取器。
修改了什么：`src/tools/agent_tools/analysis/dynamic_weights.py`、`src/tools/agent_tools/decision/capital_allocator.py`、`src/tools/agent_tools/research/adaptive_policy_safety.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：Researcher 候选偏好即使不直接开仓，也不能绕到分析师权重或资金配置侧间接改变 PM 输入；所有自适应策略读取都必须先通过同一套候选/验证/风险降低边界。

（15）让分析师融合和统一字段审计只认结构化语义。
修改了什么：`src/tools/agent_tools/analysis/signal_fusion.py`、`src/tools/agent_tools/control/unified_field_audit.py`、`src/tests/test_unified_field_migration.py`。
为什么改：融合层读取 `trigger_valid/setup_quality_ok` 等字段时必须优先使用 `action_evidence_contract`，不能让原始信号字段压过 canonical 合约；统一字段审计改为扫描 Python/YAML 的结构化 key，避免注释、reason 文本误杀，同时继续阻断旧字段重新驱动生产路径。

（16）收到底 PM 条件监控、上下文校准 safety 与文本 canonical 优先。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/decision/contextual_rule_calibration.py`、`src/tools/agent_tools/research/adaptive_policy_safety.py`、`src/tools/agent_tools/research/reviewer_tools.py`、`src/tools/agent_tools/analysis/signal_fusion.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_reviewer_learning.py`、`src/tests/test_agent_contracts.py`、`docs/unified_field_semantics.md`。
为什么改：补上真实 PM 链路里 `watch_for_trigger + setup_quality_ok` 被误判为 `watch_for_trigger_without_setup` 的漏口；让技术参数和盘中执行的 contextual calibration 也必须通过同一套自适应策略安全过滤；让 `entry_trigger/invalidation` 文本同样优先读 `action_evidence_contract`，避免布尔字段已统一但文本解释继续漂移。

（17）修复学习安全过滤与当前触发确认的过度收紧回归。
修改了什么：`src/tools/agent_tools/analysis/quality.py`、`src/tools/agent_tools/research/adaptive_policy_safety.py`。
为什么改：显式 `current_trigger_confirmed/short_term_trigger_confirmed` 已成立时，不能再被 `only after/confirmation` 等 pending 文本压回 `watch_for_trigger`；自适应学习安全过滤也不能把动作明确的合规降风险/候选 release 旧记录全部误杀，否则 PM/Auditor 的 cap、protect、calibrate、learned underperformance 降级会失效。

==========2026年06月21日========

（1）把 PM 收成统一证据分流器的运行时入口。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/analysis/signal_fusion.py`、`src/tools/agent_tools/decision/pm_invalidation_policy.py`。
为什么改：PM 是唯一交易决策人，读取分析师证据时必须优先使用 `action_evidence_contract` 的 `entry_trigger/invalidation_present/invalidation_condition`，不能再让 `would_change_view_if` 或原始信号字段直接生成交易边界；同一机会必须被分流为当前可交易、条件监控或明确不可交易原因，避免干净机会在 PM 中间层静默变成无意义 wait。

（2）新增条件机会与高质量机会静默 wait 的系统审计。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：有 `watch_for_trigger + setup_quality_ok + entry_trigger + invalidation_present` 的条件监控候选时，必须进入同一张 `final_action_contract` 的 `conditional_trigger_authority` 或写出明确拒绝原因；`trigger_valid=true` 也必须有 `current_trigger_confirmed`，否则直接 hard fail，防止分析师证据、PM 分流和 Trader 执行再次语义分叉。

（3）把 PM 机会三分流接入回测前验收。
修改了什么：`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/tests/test_pre_backtest_acceptance.py`。
为什么改：回测前验收不能只停留在原来的通用 readiness 项；新增 `pm_opportunity_routing`，把条件监控候选静默 wait、高质量机会静默 wait、`trigger_valid` 缺当前确认、`setup_quality_ok` 被误当触发等 PM 分流问题明确归类为回测前 hard fail，避免新一轮回测才暴露 PM 机会分流没接住。

（4）把 PM 机会三分流归类接入每日系统审计输出。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/tests/test_system_invariant_audit.py`、`src/tests/test_pre_backtest_acceptance.py`。
为什么改：每日回测后 `system_invariant_audit` 不能只给普通错误列表；新增共享的错误分类表和 `metadata.error_categories/failed_categories`，让条件机会静默 wait、高质量机会静默 wait、触发字段缺当前确认等问题在每日 fail-fast JSON 中明确归到 `pm_opportunity_routing`，并让回测前验收复用同一分类口径，避免两套检测口径再次漂移。

（5）把统一字段语义接成独立验收项和每日审计摘要。
修改了什么：`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_pre_backtest_acceptance.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：字段语义已经统一到 `docs/unified_field_semantics.md`，检测也必须显式体现；本次让回测前报告新增 `unified_field_semantics` 检查项，并让每日 `system_invariant_audit` 输出 `metadata.unified_field_semantics_audit`，把旧字段复活、`trigger_valid/current_trigger_confirmed` 矛盾、`setup_quality_ok` 被误当触发等问题单独归类，而不是埋在普通错误或 PM 分类里。

（6）修正自适应策略审计对白名单动作 `calibrate` 的误杀。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`。
为什么改：`contextual_rule_calibration:*` 的 `policy_action=calibrate` 已由 `adaptive_policy_safety.py` 定义为有界校准动作，不能被每日审计判成未知动作；本次让审计白名单与学习安全过滤保持一致，并补测试锁住 validated contextual calibration 不再触发 `adaptive_policy_unknown_action`。

（7）对齐评估模块到统一字段语义与策略/运营分账口径。
修改了什么：`src/evaluation/evaluation.py`、`src/evaluation/plot_portfolio.py`、`src/evaluation/analyze_strategy_attribution.py`、`src/tests/test_evaluation_unified_semantics.py`。
为什么改：字段语义表要求 `source_type=strategy` 与换月、强平等运营单分账；本次让策略胜率、策略质量、分析师/PM 归因和弱边建议只看策略交易对，账户曲线继续包含所有真实结算，运营单只进入独立摘要，避免运营成交污染 alpha 归因。

（8）打通 PM 全市场机会评分与资金部署解释字段。
修改了什么：`src/tools/agent_tools/analysis/signal_fusion.py`、`src/agents/decision_team/portfolio_manager.py`、`src/graph/workflow.py`、`src/config/portfolio_policy_catalog.yaml`、`src/config/dev.yaml`、`docs/unified_field_semantics.md`。
为什么改：把候选机会从逐品种平铺释放收束为可比较的 `opportunity_score/opportunity_score_components/opportunity_rank/capital_allocation_reason/learning_adjustment_summary` 诊断，并在 Phase1 完成后写回当日全市场排名；这些字段只进入 scorecard、`final_action_contract.evidence_used/learning_used` 和审计诊断，不生成第二交易权限，不改变 `target_lots/lots_delta/final_action`。

（9）补齐 Reviewer/Researcher 对 PM 排序效果的学习闭环。
修改了什么：`src/tools/agent_tools/research/reviewer_tools.py`、`src/tools/agent_tools/research/researcher_tools.py`、`src/config/learning_policy_catalog.yaml`、`src/util/config_normalizer.py`。
为什么改：让复盘不只看单笔盈亏，还记录 PM 评分、排名、资金分配理由是否把资金推向更强 alpha；Researcher 只写候选排序偏好学习事件，不能直接创建交易权限、改 Trader 方向或手数。

（10）对齐审计、归因、提示词和测试到新增排序字段边界。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/evaluation/analyze_strategy_attribution.py`、`src/llm/prompt.py`、`src/tests/test_agent_contracts.py`、`src/tests/test_phase_flow_regression.py`、`src/tests/test_system_invariant_audit.py`、`src/tests/test_reviewer_learning.py`。
为什么改：防止 `opportunity_score/opportunity_rank` 被误用成交易授权，同时让策略归因能按高分/低分、排名和资金分配理由评估效果；测试覆盖字段不越权、全市场 rank 写回不改合约、审计拦截顶层交易权限误用。

（11）把 PM 全市场机会排序真正落到资金部署和最终合约。
修改了什么：`src/graph/workflow.py`、`src/database/sqlite_helper.py`、`src/database/interface.py`、`src/config/dev.yaml`、`src/config/portfolio_policy_catalog.yaml`、`src/tests/test_phase_flow_regression.py`、`docs/unified_field_semantics.md`、`docs/mechanism_multiagents.md`、`docs/mechanism_research.md`、`docs/mechanism_future_trade.md`、`docs/mechanism_data_model.md`。
为什么改：此前 `opportunity_score/opportunity_rank/capital_allocation_reason` 已进入诊断、复盘和学习，但还没有真正改变 `final_action_contract.target_lots/lots_delta`；本次让 Phase1 先收集所有品种候选，再由 PM 全市场资金部署 pass 按排名和资金目标回写同一张最终合约与推荐顶层 action/lots，入选候选保留实际 probe/开仓，未入选候选退回不增加敞口并写清可复盘原因，同时保持 Trader 只执行唯一合约、不新增第二交易权限。

（12）锁死推荐顶层 action/lots 与唯一合约部署结果一致。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：每日审计此前只检查 `final_action_contract` 内部一致性和成交是否来自合约，没有直接拦截推荐表顶层 `action/lots` 与部署后的 `final_action_contract.current_lots/target_lots` 不一致；本次新增 hard fail，并让 workflow 部署测试确认 DB 更新接口同步写回 action/lots，防止“合约改了、顶层没同步”的非策略风险。

（13）重命名当前可交易 scorecard probe reason code 并同步字段语义。
修改了什么：`src/agents/decision_team/portfolio_manager.py`、`src/tools/agent_tools/decision/pm_capital_policy.py`、`src/tools/agent_tools/decision/reason_effects.py`、`src/tests/test_phase_flow_regression.py`、`docs/unified_field_semantics.md`。
为什么改：旧名容易被误读成 `watch_for_trigger` 条件监控释放；本次统一改为 `scorecard_current_tradeable_probe_seed`，明确它只适用于当前可交易或当前触发已成立的候选，条件监控仍走 `conditional_monitor_probe_seed/pm_watch_for_trigger_probe_cap`，避免 PM 当前可交易通道与条件监控通道再次语义混用。

==========2026年06月22日========

（1）把完整 episode/action-value 学习接入 PM 机会评分。
修改了什么：`src/tools/agent_tools/analysis/signal_fusion.py`、`src/agents/decision_team/portfolio_manager.py`、`src/tests/test_phase_flow_regression.py`。
为什么改：让 `positive_candidate_open/positive_candidate_execution/positive_candidate_hold/positive_candidate_exit`、`tail_loss_protect/negative_revalidate/negative_hold_revalidate` 按 `evidence_scope`、真实 episode 来源、样本数、收益质量和时间衰减进入 `opportunity_score_components`；PM 在取到相似 action-value 后重建 scorecard，使完整交易生命周期学习真正影响排名与后续资金部署，而不是只看上一天或表面 setup。

（2）调整 PM 排名先验与学习配置，避免 probe floor 复活弱机会。
修改了什么：`src/config/portfolio_policy_catalog.yaml`、`src/config/learning_policy_catalog.yaml`、`docs/unified_field_semantics.md`。
为什么改：降低 `tradeable_state/setup_quality/confidence` 等表面机会分，提高 `positive_learning/negative_learning/execution_profile_learning/recent_tail_loss_penalty` 权重；新增组件只作为 `opportunity_score_components` 的正式评分分项，负向学习只降低排名、不做品种黑名单，正向学习可支持 alpha 从 probe 晋升到更高目标仓位。

（3）对齐提示词和机制文档到全周期 alpha 学习与放大。
修改了什么：`src/llm/prompt.py`、`docs/mechanism_research.md`、`docs/mechanism_multiagents.md`、`docs/mechanism_data_model.md`、`docs/mechanism_future_trade.md`。
为什么改：明确 PM/Researcher 使用完整 episode 优先于单日噪声；正向 alpha 要支持“probe 验证 → rank 晋升 → 合规放大 → 盈利持有/加仓 → 失效退出”，近期 tail loss 可抵消旧正向学习；所有仓位变化仍只能通过唯一 `final_action_contract`，不让 Researcher 或 Trader 越权。

（4）把新增学习评分分项接入每日审计和回测前验收。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tests/test_system_invariant_audit.py`、`src/tests/test_pre_backtest_acceptance.py`。
为什么改：`positive_learning/negative_learning/execution_profile_learning/recent_tail_loss_penalty` 只能作为 `opportunity_score_components` 影响 PM 排名；如果这些学习分项和 `action/lots/lots_delta/final_action` 同层出现，说明它们被误当成交易意图，必须在每日审计与回测前验收中归到 `learning_landing` hard fail，避免学习字段绕过唯一合约。

（5）补齐策略归因报告对 PM 学习评分分项的收益统计。
修改了什么：`src/evaluation/analyze_strategy_attribution.py`、`src/tests/test_strategy_attribution_report.py`、`docs/unified_field_semantics.md`。
为什么改：下一轮回测后需要直接判断 `positive_learning/negative_learning/execution_profile_learning/recent_tail_loss_penalty` 是否真的改善 rank 和收益；本次新增只读的 `by_opportunity_learning_component` 归因输出，按学习分项和正/负/零/缺失 bucket 统计策略交易表现，不新增交易字段、不改变 PM/Trader/Auditor 权限。

（6）对齐组合净值图到资金利用率验收目标。
修改了什么：`src/evaluation/plot_portfolio.py`、`src/tests/test_evaluation_unified_semantics.py`。
为什么改：PM 全市场排序和学习落地的目标不是少交易，而是让资金更聪明地流向正期望机会；组合净值图需要同时展示 `margin_ratio`、0.8% probe 下限、4% 部署参考和 20% 硬上限，便于回测后直接判断优化是否导致资金利用率塌陷或是否具备实战部署意义。

（7）最后对齐回测前检测与每日回测后检测的 PM 学习/排名边界。
修改了什么：`src/tools/agent_tools/control/system_invariants.py`、`src/tools/agent_tools/control/pre_backtest_acceptance.py`、`src/tests/test_system_invariant_audit.py`、`src/tests/test_pre_backtest_acceptance.py`。
为什么改：每日审计已经能拦截学习分项误作交易意图、排名字段越权、推荐顶层 `action/lots` 与唯一合约不一致、未完成交易日混入评估等非策略问题；本次把这些错误统一归类到回测前验收的 `learning_landing`、`pm_opportunity_routing`、`single_trade_exit`、`data_time_boundary`，并在两类报告中显式输出同一组 `pm_learning_ranking_audit_boundaries`，避免回测前检测和每日检测再次出现口径漂移。
