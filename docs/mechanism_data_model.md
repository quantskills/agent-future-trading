# AgentQuant 数据与模型调用机制

更新时间：2026-05-29

本文档记录 AgentQuant 当前已经代码落地的数据调用、模型调用、缓存加速、数据质量摘要与回测验收要求。旧的 DataYes 接口已经退出系统；当前系统只使用 PandaAI 与 Finoview 两类数据源。

## 一、数据与模型调用原则

### 1. 数据调用原则

1. **只保留两类数据源**：PandaAI 提供期货行情、分钟线、结算相关行情和期货衍生数据；Finoview 提供本地 feather 基本面数据和本地 txt 新闻数据。
2. **严禁未来信息污染当日决策**：Phase1 盘前策略只能读取 T-1 及以前可见信息；Phase2 只能读取当时已经发生的 T 日盘中数据；Phase3 才能读取当日官方结算数据；Phase4 复盘结果只能写给未来交易日使用。
3. **缓存不能改变数据可见性**：PandaAI、Finoview feather、新闻 txt 的共享缓存只减少重复读取或重复 API 调用，不能扩大时间窗口，不能把未来日期数据提前交给分析师或 PM。
4. **数据缺口必须显式记录**：PandaAI 衍生数据、Finoview 基本面、新闻数据是否可用、是否滞后、是否进入信号，都要进入结构化数据质量摘要。
5. **缺失不能伪造成方向证据**：少量可选缺口可以降级继续分析；关键缺口过多时只能降级为小仓、观察或 Neutral，不能把“没数据”当成 Bullish/Bearish。
6. **学习必须记住数据依据**：交易记忆和未交易机会记忆不仅记录结论与盈亏，也要记录当时用了哪些数据源、哪些字段、数据是否可用、是否滞后，以及这些依据如何进入信号。

### 2. 模型调用原则

1. **LLM 只负责结构化理解与研究总结**：分析师、PM、Planner、Researcher 可以调用 LLM；硬风控、成交、结算、账务、完整交易日志和 Phase4 验收不能由 LLM 最终裁决。
2. **Reviewer 不直接调用 LLM**：Reviewer 只做确定性验收、账务一致性检查、交易流水检查和完整交易日志输出；Researcher 才负责 Phase4 后的 LLM 研究与学习写入。
3. **避免重复调用以加快回测**：Phase1 支持多品种分析并行、LLM 并发门、学习上下文缓存、PandaAI/Finoview/新闻预取和共享缓存；这些优化只减少工程耗时，不改变策略逻辑。
4. **PM 决策仍按组合顺序串行**：分析师读数和 LLM 分析可以并行，但 PM、Trader、Accountant、Reviewer/Researcher 仍按交易日和品种顺序执行，避免资金状态和学习状态串扰。
5. **模型路径必须可审计**：分析师与 PM 的输出保留 `llm_path`、模型配置审计信息、结构化信号、数据依据和 artifact 指针，便于复盘模型是否按配置调用。

## 二、各智能体的数据与模型调用方式

### 1. 工作流与缓存层

`src/graph/workflow.py` 是 Phase1 策略生成主链路。每个交易日开始时会先做数据预取与缓存：

- PandaAI 日线行情和 PandaAI 衍生数据通过 Router 调用，进入 PandaAI 客户端已有的共享缓存、节流、重试和持久行情缓存。
- Finoview feather 和新闻 txt 通过 `data_usage.py` 的进程内共享缓存读取，避免 15 个品种和多个智能体重复读本地文件。
- 预取失败只写 warning，不改变交易决策，不中断回测。
- 并行分析下，分析师输出由工作流集中保存；signal 表按 `portfolio_id + ticker + analyst` 保留最终一条，避免重复信号污染后续统计、学习和评估。

配置入口在 `src/config/dev.yaml` 的 `runtime.data_cache` 和 `runtime.data_quality_summary`。

### 2. Technical Analyst

技术分析师调用 PandaAI 的连续日频行情，计算趋势、波动率、成交量、支撑阻力、ATR 等技术证据，再调用 LLM 输出结构化 `AnalystSignal`。

当前已经写入信号 metadata 和分析师报告的数据依据包括：

- PandaAI 行情是否可用。
- 最新行情日期、行数、字段。
- 实际使用的技术指标。
- 技术参数自适应结果：`adaptive_params` 与 `technical_parameter_calibration`，用于审计 Researcher 写入的技术参数情境校准是否被读取、是否只做有界微调。
- `data_usage_summary`。

### 3. Fundamental Analyst

基本面分析师读取 Finoview 本地 feather 基本面数据，并可读取 PandaAI 期货衍生数据作为补充证据。Finoview 数据会经过无未来函数快照、覆盖率、新鲜度、缺口和低置信度诊断后进入 prompt。

当前已经写入的数据依据包括：

- Finoview 指标配置数量、加载数量、缺失数量、陈旧数量、覆盖率、陈旧率。
- PandaAI 衍生数据 reference date、lookback、feature status、record counts、缺口、错误。
- 是否进入 fundamental 信号。
- `data_usage_summary`。

### 4. Commodity News Analyst

新闻分析师读取本地 `data/News_data/Future_news/<ticker>.txt`。新闻只按当日可见窗口过滤，盘前默认不读取当日及未来新闻。

当前已经写入的数据依据包括：

- 新闻文件是否存在、编码、原始 block 数、解析新闻数、最终使用新闻数。
- 最新新闻日期、新闻截止规则。
- 新闻事件类型、方向计数、新鲜度、相关性。
- `data_usage_summary`。

### 5. Portfolio Manager

PM 读取三位分析师的结构化信号、交易研究契约、学习上下文、PandaAI market confirmation、持仓、资金、风险状态和 Auditor 结果。

本轮优化后，PM 的 recommendation snapshot 会写入：

- 每位分析师的 `data_usage_summary`。
- PandaAI market confirmation 的可用性、缺口、feature status、confirmation score。
- 汇总后的 `data_quality_summary`。
- 每日数据质量 JSON 路径。

真实回测或模拟盘每天会输出：

```text
src/logs/data_quality/<交易日>.json
```

该文件用于让分析师、PM、Researcher 和人工复盘清楚知道：当日信号依赖了哪些真实数据，哪些数据缺失或滞后，哪些数据真正进入了信号。

### 6. Trade Auditor

Auditor 不调用 LLM，也不直接拉取原始数据。它读取 PM、分析师和学习系统已经结构化好的证据，做确定性审计：allow、scale_down、probe_only、reduce_only、block。

Auditor 可以使用数据质量、市场确认、学习状态和风险状态，但不能把缺失数据或候选记忆当成交易授权。

### 7. Trader 与 Accountant

Trader 使用 Phase1 推荐和当时已发生的盘中 PandaAI 行情执行，不调用 LLM 创造新策略。Accountant 使用官方结算价、手续费、保证金和成交记录做 Phase3 结算，不调用 LLM，也不被学习文本改账。

### 8. Reviewer 与 Researcher

Reviewer 负责 Phase4 确定性验收和完整交易日志输出，不直接调用 LLM。

Researcher 在 Reviewer 验证通过后写入未来可用学习，包括真实交易记忆、未交易机会记忆、Neutral shadow、探索式假设、分析师学习摘要和学习策略状态。当前学习记忆已经写入：

- `data_usage_summary`
- `data_usage_notes`
- 当时信号、PM/Auditor/Trader 结果
- 最后仓位、盈亏、手续费、持仓周期和后续影子结果

这保证学习不是只记“赚了/亏了”，而是记住当时为什么这样判断、用了什么数据、数据质量如何，以及这些依据未来该如何被反驳或验证。

### 9. Planner 与模型路由

Planner 只在启用 planner mode 时调用 LLM 选择分析师组合，不生成交易指令。模型配置统一来自 `src/config/dev.yaml` 的 `llm` 与各智能体 override；系统通过 `llm_path`、审计 metadata 和配置注释保持模型调用可追踪。

## 三、回测验收项

本轮数据与模型调用优化已经代码落地，但仍需要通过后续回测验收以下项目：

1. **每日数据质量摘要输出**：每个完成 Phase1/Phase4 的交易日，`src/logs/data_quality/<交易日>.json` 应包含所有已分析品种的数据质量摘要。
2. **推荐快照包含数据依据**：`futures_recommendation.signal_snapshot` 中应包含 `data_quality_summary`，并能追溯到 technical、fundamental、commodity_news 的 `data_usage_summary`。
3. **学习记忆包含数据依据**：`trade_episode_memory` 与 `no_trade_opportunity_memory` 的 payload 中应包含 `data_usage_summary` / `data_usage_notes`，并能说明数据依据、判断、仓位和盈亏之间的关系。
4. **无未来数据污染**：Phase1 的 Finoview、新闻、PandaAI 衍生数据 reference date 必须早于或等于盘前可见边界；Phase4 shadow 和学习结果只能影响未来交易日。
5. **缓存只加速不改策略**：开启 `runtime.data_cache` 后，recommendation 数量、交易日顺序、PM 串行决策、完整交易日志和账务结果不应因缓存而错位或重复。
6. **模型调用可审计**：分析师、PM、Researcher 输出中应能看到模型路径或模型审计 metadata；Reviewer、Trader、Accountant 不应出现 LLM 决策越权。
7. **回测速度改善**：Phase1 timing summary 中应能看到本地数据预取、PandaAI 预取、分析并行和缓存复用生效，且没有 database locked、重复 signal、重复 recommendation 或 artifact 日期错位。
8. **signal 落库完整唯一**：每个交易日 signal 表应与推荐快照一致，覆盖全部 `ticker × analyst` 组合；daily summary 的 `extra_audit.signal_persistence` 不应出现 missing、extra 或 duplicate。
9. **技术参数校准可审计**：若 `adaptive_policy_state` 中存在 `contextual_rule_calibration:technical_parameters`，Technical Analyst 的 signal metadata 和分析师报告应显示实际 `adaptive_params`、校准来源与变动幅度；该校准不能绕过数据可见性、不能触发 LLM 重复调用，也不能直接产生交易授权。
10. **signal artifact 机器可读元数据**：signal 表的 artifact payload 顶层应稳定包含 `llm_path`、`data_usage_summary`、`technical_parameter_calibration`、`adaptive_params` 和 `signal_artifact_metadata`，评估脚本与 Researcher 应能直接读取，不依赖人工报告解析。
