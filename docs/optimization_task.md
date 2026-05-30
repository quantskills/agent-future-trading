# AgentQuant 优化任务总纲

更新时间：2026-05-30

本文档只保留 AgentQuant 的两大核心功能与七大优化目的。已经代码落地的机制说明见 `mechanism_mutiagents.md`、`mechanism_data_model.md`、`mechanism_research.md`；待回测验收项目见 `optimization_check_list.md`。

## 一、两大核心功能

1. **期货策略回测**：用历史交易日复刻真实运行链路。Phase1 盘前生成策略，Phase2 盘中执行，Phase3 日终结算，Phase4 复盘、校验并写入未来可用学习状态。
2. **模拟盘/模拟交易**：与回测共用同一套盘前推荐、盘中执行、结算、复盘和学习逻辑。Trader 只按既定策略与当时可见盘中数据执行，Accountant 只按账务事实结算，Researcher 的学习只影响未来交易日。

## 二、七大优化目的

1. **扩大 alpha 收益**：让系统能识别真正可重复的交易机会，并在证据成熟后把它落实到仓位。
2. **提高资金利用率**：资金释放只给高质量信号和成熟 alpha，不为达标硬拉 weak/watchlist 仓位。
3. **改善收益质量**：不仅看总收益，也看回撤、盈亏因子、胜率、平均盈亏比、品种集中度、多空贡献和手续费侵蚀。
4. **让智能体真正学习**：学习结果必须可检索、可验证，并能影响后续分析、PM 决策、Auditor 审计和 Trader 执行边界。
5. **提高分析师信号质量**：分析师不能只给方向，必须说明机会类型、时间维度、入场触发、退出提示、失效边界、证据冲突和 Neutral 责任。
6. **保持无未来函数、账务正确、实盘可执行**：Phase1/2/3/4 信息边界清楚，账务以官方结算和确定性规则为准。
7. **防止过拟合**：不写具体品种黑白名单，不因短窗口单次盈亏调整硬规则；学习按品种、方向、周期、模板、市场状态和样本窗口分层验证。

## 三、已经代码落地的优化

1. **Phase1 signal 落库唯一性**：同一 portfolio、ticker、analyst 只保留最终一条信号，避免并行分析后重复 signal 污染统计、学习和评估。
2. **Phase4 signal 完整性验收**：Reviewer 校验推荐快照和 signal 表是否覆盖全部 `ticker × analyst`，并把结果写入 daily summary 的 `extra_audit.signal_persistence`。
3. **技术参数情境校准**：Researcher 根据已结算的同品种 technical 短周期表现，写入 `contextual_rule_calibration:technical_parameters`；Technical Analyst 下一轮读取后，只对 EMA、RSI、Bollinger 做小幅有界校准。
4. **技术校准审计可见性**：Technical Analyst 的 signal metadata 和分析师报告写入 `adaptive_params` 与 `technical_parameter_calibration`，便于验收校准是否真实进入分析链路。
5. **边界控制**：技术参数校准不直接放仓、不绕过 PM/Auditor/Trader/Accountant、不改变手续费、保证金、滑点、涨跌停、换约、结算和 20% 保证金硬门槛。
6. **signal artifact 机器可读元数据**：signal 落库 artifact 顶层稳定暴露 `llm_path`、`data_usage_summary`、`technical_parameter_calibration`、`adaptive_params`，评估脚本和 Researcher 不再只能读人类报告。
7. **亏损模板观察性研究记忆**：Researcher 会把已结算亏损模板写成 candidate `loss_template_observation`，包含数据组合、市场状态、使用边界和仓位影响条件；该记忆只做下一轮分析先验，不写品种黑名单，不直接放仓或压仓。
