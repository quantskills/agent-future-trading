# AgentQuant 待回测验收清单

更新时间：2026-05-31

本文档只保留已经 100% 代码落地、但还没有经过下一轮干净回测验收的项目。正式机制说明见 `mechanism_data_model.md`、`mechanism_research.md`、`mechanism_future_trade.md`、`mechanism_mutiagents.md`。

## 一、待验收项目

| 编号 | 待验收项目 | 验收重点 |
|---|---|---|
| V1 | 四阶段与智能体边界 | Phase1-4 完整；Reviewer 只验收流程、账务、日志；Researcher 写记忆与研究；Trader/Accountant 不被 LLM 或学习越权 |
| V2 | 完整交易日志 | 每个交易日生成 `src/logs/<交易日>_transaction.log`，结构和信息密度对齐模板 |
| V3 | 数据质量与数据依据 | `src/logs/data_quality/<交易日>.json` 生成；推荐、signal artifact、交易记忆和未交易记忆包含数据可用性、滞后性、字段依据 |
| V4 | 无未来数据污染 | Phase1 只用盘前可见数据；Phase2 只用当时盘中数据；Phase4 shadow 与学习只影响未来交易日 |
| V5 | 模型调用审计 | Analyst、PM、Researcher 可追踪 provider/model/reasoning effort；Reviewer、Trader、Accountant 无 LLM 决策越权 |
| V6 | 回测加速与落库稳定 | 多品种分析并行、预取、缓存、LLM 并发门生效；无数据库锁、重复 signal、缺失 signal、日期错位 |
| V7 | signal artifact 机器可读元数据 | signal artifact 顶层可读 `llm_path`、`data_usage_summary`、`technical_parameter_calibration`、`adaptive_params` |
| V8 | Phase4 signal 完整性 | daily summary 写入 `extra_audit.signal_persistence`；每个交易日覆盖全部 `ticker × analyst`，重复/缺失会被 Reviewer 拦截 |
| V9 | 研究与交易契约 | Analyst/PM snapshot、记忆、探索假设、策略状态含下一轮可用记忆、使用边界和仓位影响条件 |
| V10 | 真实交易与未交易机会记忆 | `trade_episode_memory`、`no_trade_opportunity_memory`、`exploratory_hypothesis`、`learning_event_log` 按 Phase4 写入 |
| V11 | 亏损模板观察性研究 | 已结算亏损模板写入 candidate `loss_template_observation`；只做分析先验，不写品种黑名单，不直接放仓或压仓 |
| V12 | no-trade 与 Neutral shadow | no-trade、涨跌停错失成交 shadow、Neutral 后续窗口只在未来结算后回填，不进入真实账务 |
| V13 | 候选假设边界 | candidate hypothesis 不能支撑放仓、加仓、`position_matched` 或亏损仓继续持有 |
| V14 | 成熟经验落仓 | protected/deployable/alpha promotion 只有在当日证据、market confirmation、失效边界和 Auditor 通过时影响仓位 |
| V15 | learned vs unlearned | learned 交易不应长期显著跑输 unlearned；若跑输，同作用域 demote 应出现并反映到后续仓位 |
| V16 | tail-loss 与 horizon | 新仓快速亏损需当日证据复核；中期基本面不能单独触发短线新仓、加仓或亏损持有 |
| V17 | alpha release 与资金利用 | 普通机会可接近 6%-8%；强 alpha 才可接近 16%-20%；不硬拉 weak/watchlist 仓位 |
| V18 | 回撤保护与恢复 | 4%/5% 回撤场景中 warning、hard protection、cooling、recovery probe 按配置生效 |
| V19 | PandaAI/Finoview 缺口降级 | 可选缺口不打断；关键缺口降级为小仓、观察或 Neutral，不伪造成方向证据 |
| V20 | 评估与画图稳定 | `evaluate_config.py` 整体/区间评估正常；`plot_config.py` 只输出组合净值图和有交易品种图 |
| V21 | 收益与学习有效性 | 干净回测后观察收益曲线、learned/unlearned、alpha promotion、tail-loss、资金利用率是否改善 |
| V22 | 学习弱参 | 观察记忆有效期、overlay、provisional policy、exploratory research、loss template observation、shadow windows、prompt budget 是否需要调整 |
| V23 | 实战化交易业务机制 | 涨跌停成交保护、动态保证金回退、换约成本审计、临近交割新仓保护、未成交原因完整性和数据缓存通过回测验收 |
| V24 | 情境化弱参校准 | `contextual_rule_calibration` 按品种/方向/周期/市场状态写入并读取；只校准软阈值，不突破 20% 上限 |
| V25 | 技术参数情境校准 | Researcher 写入 `contextual_rule_calibration:technical_parameters`；Technical Analyst 只小幅校准 EMA/RSI/Bollinger，并在 metadata 中记录 `adaptive_params` 与 `technical_parameter_calibration` |

## 二、回测后快速检查

1. 四阶段状态、Traceback、数据库锁、LLM/PandaAI 错误。
2. 交易流水、结算、手续费、保证金、持仓、账户权益是否对账。
3. 完整交易日志、data quality JSON、daily summary 是否按日生成。
4. 推荐快照、signal 表、signal artifact 是否完整、唯一、可机器读取。
5. 学习表是否写入，prompt 是否读取，候选记忆与成熟记忆是否越权。
6. learned vs unlearned、Neutral、no-trade shadow、loss template observation、资金利用率和收益曲线是否改善。
7. 技术参数校准是否来自已结算样本，是否改善 technical 信号质量，且没有造成过拟合或低交易频率。
