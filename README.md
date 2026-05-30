# AgentQuant

AgentQuant 是一个面向中国期货主力合约的多智能体交易策略系统。当前代码重点服务两个核心功能：

1. **期货策略回测**：按历史交易日复刻“盘前策略、盘中执行、日终结算、复盘研究”的完整运行链路。
2. **模拟盘/模拟交易**：与回测共用同一套推荐、执行、结算、日志和学习机制，使回测生成的策略尽量可以一比一迁移到模拟盘。

系统当前只支持 `china_futures`，默认交易 15 个期货品种：

`BU`、`C`、`CF`、`EB`、`HC`、`I`、`J`、`M`、`MA`、`P`、`PB`、`RB`、`SR`、`TA`、`ZN`。

## 一、系统运行总览

AgentQuant 以交易日为最小运行单元，每个交易日分为四个阶段：

1. **Phase1 策略生成**  
   技术面、基本面、新闻面分析师读取当日盘前可见数据，生成结构化信号；Portfolio Manager 汇总分析师信号、账户状态、市场确认、研究记忆和风控状态，生成每个品种的盘前期货推荐。

2. **Phase2 交易执行**  
   Trader 读取 Phase1 推荐，结合盘中确认、合约、持仓、手数、滑点、涨跌停、保证金和订单语义，写入真实交易流水。Trader 只执行或跳过已批准计划，不创造新的交易策略。

3. **Phase3 日终结算**  
   Accountant 使用成交流水、合约乘数、保证金率、手续费和官方结算价逐日盯市，更新组合账户、持仓、日结算和品种日 PnL。账务事实不由 LLM 或研究解释改写。

4. **Phase4 复盘与研究**  
   Reviewer 做确定性验收，检查 Phase1-3 是否完整、账务是否一致、交易流水是否入账、signal 是否完整唯一、完整交易日志是否输出。Reviewer 验证通过后，Researcher 写入未来可用记忆、探索式假设和学习状态。

## 二、多智能体结构

当前智能体已经按职责分组：

```text
src/agents/
  analysis_team/      # technical、fundamental、commodity_news
  decision_team/      # portfolio_manager、auditor
  execution_team/     # trader、accountant
  research_team/      # reviewer、researcher
  control_team/       # planner
```

主要职责如下：

| 智能体 | 当前职责 |
|---|---|
| Technical Analyst | 读取 PandaAI 行情与学习上下文，分析价格行为、趋势、波动率、成交量、技术指标和短线交易条件 |
| Fundamental Analyst | 读取 Finoview 本地基本面数据与 PandaAI 衍生数据，分析供需、库存、基差、仓单、产业链和数据质量 |
| Commodity News Analyst | 读取本地期货新闻，分析事件方向、强度、新鲜度、相关性和可交易性 |
| Portfolio Manager | 汇总分析师信号、学习记忆、账户、持仓、市场确认和风控状态，生成目标仓位和期货推荐 |
| Auditor | 做确定性交易审核，输出 allow、scale_down、probe_only、reduce_only 或 block，不调用 LLM |
| Trader | 执行 Phase1 推荐，处理盘中触发、开平仓、反手、换约、滑点、涨跌停和未成交原因 |
| Accountant | 做 Phase3 日终结算、手续费、保证金、持仓、账户权益和 PnL |
| Reviewer | 做 Phase4 确定性验收并输出完整交易日志，不调用 LLM |
| Researcher | 在 Reviewer 验证通过后写入记忆、研究假设、策略状态和学习事件，可调用 LLM 做研究 |
| Planner | 旧版分析师选择器，默认 `planner_mode=false`，当前主流程不启用 |

更多细节见：

- `docs/mechanism_mutiagents.md`
- `docs/mechanism_research.md`

## 三、数据与模型

当前系统只使用两类数据源：

1. **PandaAI**  
   用于期货日频行情、分钟线、主力合约、结算相关行情、涨跌停、合约详情和期货衍生数据。

2. **Finoview 本地数据**  
   基本面数据保存在 `data/Fundamental_data/Finoview_data/` 的 feather 文件中；期货新闻由人工放入 `data/News_data/Future_news/` 的 txt 文件中。

数据调用原则：

- Phase1 只能读取盘前可见数据。
- Phase2 只能读取当时已经发生的盘中数据。
- Phase3 才能读取日终结算事实。
- Phase4 的 shadow、研究和学习只影响未来交易日。
- 缓存和预取只用于减少重复读取或重复 API 调用，不能扩大数据可见窗口。

LLM 调用原则：

- 分析师、PM、Planner、Researcher 可以调用 LLM。
- Reviewer、Trader、Accountant 不应让 LLM 直接裁决流程、成交或账务。
- 当前主 LLM 路由在 `src/config/dev.yaml` 的 `llm` 段配置，API Key 放在 `.env`，不要写入配置文件。
- signal artifact 会稳定记录 `llm_path`、`data_usage_summary`、`technical_parameter_calibration`、`adaptive_params`，方便评估和 Researcher 读取。

更多细节见：

- `docs/mechanism_data_model.md`

## 四、期货交易业务机制

当前业务机制不是简单按收盘价买卖，而是尽量接近真实期货交易：

- 使用具体合约代码，不只记录品种代码。
- 支持多头、空头、开仓、平仓、减仓、清仓和反手。
- 反手交易按“先平原方向，再开新方向”处理。
- 支持主力换约，换约按平旧合约、开新合约两条真实腿记录。
- 支持手续费、滑点、合约乘数、保证金率和保证金释放。
- 支持 PandaAI 动态保证金回退到本地静态合约缓存。
- 支持涨跌停成交保护，触及涨停的买入类订单或触及跌停的卖出类订单会跳过并写入 `limit_locked_no_fill`。
- Phase3 使用官方结算价逐日盯市，更新现金、保证金、账户权益、持仓和品种日 PnL。
- 每个交易日 Phase4 都应输出完整交易日志：`src/logs/<YYYY-MM-DD>_transaction.log`。

组合层面存在一个不可突破的硬约束：

```text
max_total_margin_ratio = 0.20
```

也就是说，系统可以根据学习和信号质量释放资金，但总保证金占用不能超过 20% 硬门槛。

更多细节见：

- `docs/mechanism_future_trade.md`

## 五、记忆与研究机制

AgentQuant 的学习目标不是写死更多交易规则，而是让智能体从历史交易和未交易样本中探索期货交易规律，并把研究结论变成下一轮可用记忆。

当前已经代码落地的学习链路包括：

- 真实交易片段记忆：`trade_episode_memory`
- 未交易机会记忆：`no_trade_opportunity_memory`
- no-trade shadow 与 Neutral 后续窗口
- 探索式假设：`exploratory_hypothesis`
- 分析师学习摘要：`analyst_learning_digest`
- 策略记忆与模板表现：`strategy_memory_history`、`signal_template_performance`
- 自适应策略状态：`adaptive_policy_state`
- 资本部署状态：`capital_deployment_state`
- 学习事件账本：`learning_event_log`
- 下一轮策略更新契约：`next_round_memory_contract`

研究结论必须带使用边界：

- 候选假设只能作为分析先验，不能直接放仓、加仓、`position_matched` 或支撑亏损仓继续持有。
- 成熟经验也必须经过当日证据、市场确认、失效边界、PM、Auditor、Trader 和 20% 保证金硬门槛。
- Researcher 写入的 `loss_template_observation` 只是亏损模板观察记忆，不是品种黑名单，也不能直接压仓或放仓。
- 技术参数情境校准只允许 Technical Analyst 小幅调整 EMA、RSI、Bollinger 等技术参数，不直接生成交易授权。

更多细节见：

- `docs/mechanism_research.md`

## 六、环境准备

建议使用项目环境：

```powershell
conda env create -f environment.yml
conda activate deepfund
```

配置 `.env`。常用项包括：

```text
DB_PATH=src/assets/agentquant.db
CHECK_DB_PATH=src/assets/agentquantcheck.db

PANDAAI_USERNAME=...
PANDAAI_PASSWORD=...

TQX_LLM_API_KEY=...
```

当前默认配置使用 `src/config/dev.yaml`。不要随意修改 `exp_name`，否则会生成新的实验口径，导致历史回测记录无法自然接续。

初始化主数据库和评估表：

```powershell
cd D:\research\AgentQuant\src
python database\sqlite_setup.py
python database\sqlite_setup_eval.py
```

如需重建只读检查库：

```powershell
python database\build_check_db.py
```

## 七、运行方式

以下命令默认从 `D:\research\AgentQuant\src` 目录执行。

### 1. 跑完整回测窗口

```powershell
python run\backtest.py --config config\dev.yaml --local-db --start-date 2025-01-02 --end-date 2025-01-31 --reset-config
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--reset-config` | 只在窗口第一个交易日重建当前实验组合，适合干净重跑 |
| `--skip-eval` | 回测结束后不自动运行评估 |
| `--plot` | 回测结束后自动运行绘图 |
| `--plot-no-price` | 绘图时不加载价格曲线，只画净值相关图 |

如果只是接着已有记录继续回测，通常不要使用 `--reset-config`：

```powershell
python run\backtest.py --config config\dev.yaml --local-db --start-date 2025-03-01 --end-date 2025-03-31
```

### 2. 单日分阶段运行

```powershell
python run\proposal.py --config config\dev.yaml --local-db --trading-date 2025-01-02 --reset-config
python run\order.py --config config\dev.yaml --local-db --trading-date 2025-01-02
python run\settlement.py --config config\dev.yaml --local-db --trading-date 2025-01-02
python run\validate_phase_flow.py --config config\dev.yaml --local-db --trading-date 2025-01-02
```

### 3. 模拟盘 / 模拟交易

模拟盘使用同一个 Phase2 Trader：

```powershell
python run\order.py --config config\dev.yaml --local-db --trading-date 2025-01-02 --loop
```

`--loop` 会让 Trader 在交易日内按配置等待盘中触发。模拟盘仍应先有 Phase1 推荐，之后再执行 Phase2、Phase3、Phase4。

## 八、评估与绘图

评估当前配置：

```powershell
python run\evaluate_config.py --config config\dev.yaml --local-db --update
```

评估指定区间：

```powershell
python run\evaluate_config.py --config config\dev.yaml --local-db --start-date 2025-01-02 --end-date 2025-02-28
```

绘图：

```powershell
python run\plot_config.py --config config\dev.yaml
```

绘图原则：

- 画一张组合净值曲线。
- 只针对回测期或模拟盘期有交易的品种画图。
- 每个有交易品种画一张图：上方是该品种净值贡献，下方是价格曲线和开平仓点位。
- 默认输出到 `image/`，也可用 `--output-dir` 指定目录。

## 九、主要输出位置

| 内容 | 位置 |
|---|---|
| 主 SQLite 数据库 | `src/assets/agentquant.db` |
| 检查库 | `src/assets/agentquantcheck.db` |
| 主日志 | `src/logs/agentquant.log`、`src/logs/trade.log` |
| 每日完整交易日志 | `src/logs/<YYYY-MM-DD>_transaction.log` |
| 分析师决策报告 | `src/logs/analyst_decisions/<run_id>/` |
| 每日 summary | `src/logs/summaries/<run_id>/` |
| 数据质量摘要 | `src/logs/data_quality/<YYYY-MM-DD>.json` |
| artifact 外置文件 | `src/logs/artifacts/` |
| 评估与归因输出 | `src/logs/attribution/` |
| 绘图输出 | `image/` 或指定输出目录 |

## 十、重要数据库表

常用表包括：

| 类型 | 表 |
|---|---|
| 配置与组合 | `config`、`portfolio`、`trading_day_phase` |
| 分析与推荐 | `signal`、`futures_recommendation`、`signal_context_history` |
| 执行与结算 | `futures_transactions`、`futures_intraday_decision`、`daily_settlement`、`ticker_daily_pnl` |
| 学习与研究 | `trade_episode_memory`、`no_trade_opportunity_memory`、`exploratory_hypothesis`、`adaptive_policy_state`、`learning_event_log` |
| 评估 | `config_outcome` |

## 十一、测试与检查

常用回归测试：

```powershell
python -m unittest src.tests.test_phase_flow_regression
python -m unittest src.tests.test_reviewer_learning
python -m unittest src.tests.test_phase1_acceleration
python -m unittest src.tests.test_futures_market_rules
python -m unittest src.tests.test_market_confirmation
python -m unittest src.tests.test_plot_future_price_data
```

语法检查：

```powershell
python -m compileall src
```

## 十二、当前验收重点

最新已经代码落地、但仍需要通过干净回测观察的项目，见：

- `docs/check_list.md`
- `docs/optimization_check_list.md`

当前尤其要关注：

- 四阶段是否完整，是否有 Traceback、数据库锁、LLM/PandaAI 错误。
- 交易流水、手续费、保证金、持仓和账户权益是否对账。
- 每个交易日是否输出完整交易日志。
- signal 表和推荐快照是否覆盖全部 `ticker × analyst`，且没有重复。
- signal artifact 是否能机器读取 `llm_path`、`data_usage_summary`、`technical_parameter_calibration`、`adaptive_params`。
- Researcher 写入的候选记忆是否进入 prompt，但没有越权直接影响仓位。
- learned vs unlearned、资金利用率、收益曲线和回撤是否改善。

## 十三、设计边界

AgentQuant 当前不是盘口级撮合系统，也不是自动训练新模型的机器学习平台。它更接近一个可审计的期货策略生成、执行、结算和研究闭环系统。

系统设计底线：

- 不用未来信息污染当日决策。
- 不让 LLM 改写账务事实。
- 不让候选研究结论直接变成交易指令。
- 不写品种黑名单或无条件放仓规则。
- 不突破 20% 保证金硬门槛。
- 回测、模拟盘和未来实盘迁移尽量共用同一套业务链路。
