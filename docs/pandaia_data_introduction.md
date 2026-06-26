# PandaAI 数据接入说明

更新时间：2026-06-25

本文档说明 AgentQuant 当前如何使用 PandaAI 数据，以及 PandaAI 在盘前分析、市场确认、盘中执行、运营风控、日终结算和回测验收中的角色。PandaAI 是行情与衍生数据源，不是交易授权来源；真实策略交易必须经过分析师结构化预测证据、信号收集员统一证据包、投资组合经理唯一 `final_action_contract`、审计员、交易员和会计师。

## 一、PandaAI 在系统中的定位

PandaAI 当前服务六类任务：

| 场景 | 使用方 | 用途 | 边界 |
| --- | --- | --- | --- |
| 日频行情 | 技术面分析师、投资组合经理、交易员、会计师、回测日历 | 技术分析、盘前参考价、主力合约、执行基准、结算审计 | Phase1 只用盘前可见数据，不能用当日结算反推策略 |
| 分钟线行情 | 交易员、forced_risk 扫描 | 15m 判断触发，1m 选择执行基准价，盘中保证金风险估价 | 只能使用当时 cutoff 前已发生 bar；回测也不能用未来 bar 改写当时判断 |
| 期货衍生数据 | 基本面分析师、投资组合经理 market confirmation | 基差、仓单、持仓排名、资金流、多空比等确认 | 缺失只能写 data quality，不能兜底成方向信号 |
| 合约详情 | 交易员、会计师 | 合约乘数、最小变动价位、保证金率、涨跌停/交割保护 | 无数据时回退本地静态缓存，并记录来源 |
| 结算价 | 会计师 | Phase3 逐日盯市、保证金重算、PnL | 只能收盘后使用，不参与当天 Phase1 策略 |
| 交易日识别 | 回测脚本、日历工具 | 解析回测交易日窗口 | 只决定运行日期，不提供交易方向 |

当前运行数据源分工是：PandaAI 负责行情、衍生确认、分钟线、合约和结算；Finoview 本地 feather 负责基本面；本地新闻 txt 负责新闻事件。三者都只是证据源，不能直接生成交易权限。

## 二、当前已接入的数据类型

### 1. 日频期货行情

接口与封装：

```text
panda_data.get_market_data(..., type="future")
PandaAIAPI.get_futures_daily_candles
PandaAIAPI.get_futures_daily_candles_df
PandaAIAPI.get_futures_daily_candles_optimized
PandaAIAPI.get_futures_quote_on_date
PandaAIAPI.get_main_contract_quote_on_date
```

主要字段包括 `open`、`day_session_open`、`high`、`low`、`close`、`settlement`、`pre_settlement`、`volume`、`amount`、`open_interest`、`limit_up`、`limit_down`、`trading_code`、`dominant_id`、`underlying_symbol`、`exchange`。

技术面分析师使用这些字段计算趋势、波动率、支撑阻力、RSI、MACD、ATR、均线等。投资组合经理使用它们作为 market confirmation 和参考价来源。交易员使用可见价格和限价字段做成交保护。会计师只在 Phase3 使用结算价做账。

### 2. 分钟线期货行情

接口与封装：

```text
panda_data.get_market_min_data(..., symbol_type="future")
PandaAIAPI.get_futures_minute_bars
Router.get_china_futures_minute_bars
```

支持 `1m`、`5m`、`15m`、`60m`。当前用途：

- 15m K 线用于盘中触发确认。
- 1m K 线用于执行基准价。
- 条件 probe 只在投资组合经理合约授权后，由交易员用分钟线检查触发。
- forced_risk 用盘中价格估算保证金风险，只能生成 close/reduce 运营风控单。
- 未触发或未成交会写入 `futures_intraday_decision` 和执行审计，不写真实交易流水。

### 3. 期货衍生数据

`get_futures_extra_snapshot` 当前支持 basis、warehouse_receipt、net_flow、variety_position_rank、symbol_position_rank、ls_ratio、broker_net_margin_change、broker_variety_profit、broker_net_margin、netposi_rank、net_cap_change、contract_daily_indicators、contract_rank 等。

这些数据主要进入基本面分析师和投资组合经理的 market confirmation。字段缺失、接口无权限、记录数不足或日期滞后，都必须进入 feature status / data quality；不能把缺数据包装成 Bullish/Bearish，也不能单独授权开仓。

### 4. 合约信息和保证金

交易员和会计师会读取合约代码、合约乘数、最小变动价位、保证金率和交易所信息。保证金率优先使用 PandaAI 合约详情；若不可用，回退本地静态合约缓存。所有回退都必须写入审计信息。

开仓保证金按 `执行价 * 手数 * 合约乘数 * 保证金率` 冻结；日终后按结算价重算持仓保证金；平仓释放当前账上保证金占用，而不一定是最初开仓冻结额。

## 三、主力合约与合约代码映射

系统内部常用合约代码格式为小写，例如：

```text
m2505
rb2601
```

PandaAI 查询通常需要交易所后缀，例如：

```text
M2505.DCE
RB2601.SHF
ZN_DOMINANT.SHF
```

系统在 `PandaAIAPI` 内部处理主力连续合约、具体合约、郑商所短年份合约扩展，并把 PandaAI 返回值标准化回内部合约代码。换月由 Phase3 日终结算后识别，生成下一交易日 `source_type=rollover` 推荐；当天收盘后才知道的换月信息不能影响当天 Phase1 策略。

## 四、避免未来函数的边界

1. Phase1 只使用盘前可得信息，不能读取 T 日结算价、未来新闻、未来分钟线或未来 shadow 结果。
2. Phase2 才读取 T 日盘中分钟线。回测可读取历史文件，但执行逻辑必须按当前 cutoff 裁剪。
3. Phase3 才读取官方结算价，只用于账务、保证金重算、换月检测和 Phase4 研究。
4. Phase4 后的研究输出只能供未来交易日使用，不能回改当日推荐、成交或账务。
5. PandaAI 缓存只能减少重复调用，不能扩大数据可见窗口。

## 五、盘中执行确认中的 PandaAI 用法

当前 Phase2 链路：

```text
pending recommendation
-> trader.py
-> Router.get_china_futures_minute_bars(frequency="15m")
-> Router.get_china_futures_minute_bars(frequency="1m")
-> intraday_execution.select_intraday_execution
-> FuturesExecutionEngine
```

普通策略单必须来自审计后的 `final_action_contract`。条件 probe 必须在合约里写明 `conditional_trigger_authority=true`、触发条件和失效边界；交易员只用 PandaAI 分钟线判断触发是否成立，不能自己创造方向、手数或保证金目标。

平仓、减仓、换月和 forced_risk 属于执行优先事项。换月走 `source_type=rollover`，强平/强减走 `source_type=forced_risk`；两者都不是策略 alpha 学习样本。

## 六、与 Finoview 和新闻数据的关系

PandaAI 不是唯一数据源。当前三类运行数据是：

- PandaAI：行情、衍生确认、分钟线、合约、结算。
- Finoview feather：基本面分析师的本地供需/库存/产业链数据。
- Future_news txt：新闻分析师的本地新闻事件。

三者共同服务于当前 15 个期货品类：

```text
BU, C, CF, EB, HC, I, J, M, MA, P, PB, RB, SR, TA, ZN
```

## 七、回测前后的检查口径

回测前 `pre_backtest_acceptance.py` 会检查数据、配置、字段语义、协议边界和本地库状态。回测过程中 `system_invariant_audit.py` 会逐日累计检查唯一合约、字段语义、未完成交易日、策略单与运营单分账、交易员执行是否只来自最终合约；随后 `mechanism_effectiveness_audit.py` 按生命周期场景只读检查 action-value、投资组合经理 score/rank、唯一合约、条件 probe、持仓保护、减仓和退出链路是否真正接通。PandaAI 数据缺口可以作为 warning 降级，但若导致未来函数、账务错误、交易合约冲突或机制断链 hard_fail，必须停止；若只是 diagnostic，则进入策略效果分析。

## 八、当前限制

1. 分钟线执行是实战近似，不是盘口逐笔撮合。
2. 夜盘小节和交易日归属仍可继续细化。
3. 部分衍生接口较慢或不稳定，需通过缓存和 feature status 控制成本。
4. `src/assets/pandaai_market_cache.db` 是行情/衍生数据缓存，不是回测交易记录；清空交易记录通常不需要删除它。

## 九、相关代码位置

```text
src/apis/pandaai/api.py
src/apis/router.py
src/tools/agent_tools/analysis/market_confirmation.py
src/tools/agent_tools/execution/intraday_execution.py
src/tools/agent_tools/execution/futures_execution.py
src/tools/agent_tools/execution/futures_settlement.py
src/agents/execution_team/trader.py
src/agents/execution_team/accountant.py
src/run/order.py
src/run/settlement.py
src/run/control/pre_backtest_acceptance.py
src/run/control/system_invariant_audit.py
src/run/control/mechanism_effectiveness_audit.py
```
