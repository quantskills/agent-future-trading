# PandaAI 数据接入说明

更新时间：2026-06-17

本文档说明 AgentQuant 当前如何使用 PandaAI 数据，以及 PandaAI 在日频策略生成、盘中执行确认、结算和数据质量审计中的角色。

## 一、PandaAI 在系统中的定位

PandaAI 是 AgentQuant 当前中国期货模式的主要行情与衍生数据源。系统通过 `src/apis/pandaai/api.py` 封装 PandaAI SDK，再通过 `src/apis/router.py` 对分析师、执行模块、结算模块提供统一接口。

PandaAI 当前主要服务于三类任务：

1. 日频行情  
   用于 technical 分析师、交易日识别、盘前参考价、Phase2 原始执行价、Phase3 结算。

2. 期货衍生数据  
   用于 market confirmation 和 portfolio manager 的交易确认，例如基差、仓单、资金流、持仓排名、多空比等。

3. 分钟线行情  
   用于优化后的 Phase2 盘中执行确认：15m 判断触发，1m 选择执行基准价。

PandaAI 只提供当时可见的数据证据，不直接生成交易权限。技术面、market confirmation、PM、Trader 和 Accountant 可以使用 PandaAI 数据，但真实新开仓仍必须经过结构化 PM 出口、Auditor 和 Trader 执行审计。

## 二、当前已接入的数据类型

### 1. 日频期货行情

接口：

```text
panda_data.get_market_data(..., type="future")
```

系统封装：

```text
PandaAIAPI.get_futures_daily_candles
PandaAIAPI.get_futures_daily_candles_df
PandaAIAPI.get_futures_daily_candles_optimized
PandaAIAPI.get_futures_quote_on_date
PandaAIAPI.get_main_contract_quote_on_date
```

主要字段：

- open
- day_session_open
- high
- low
- close
- settlement
- pre_settlement
- volume
- amount
- open_interest
- limit_up
- limit_down
- trading_code
- dominant_id
- underlying_symbol
- exchange

### 2. 分钟线期货行情

接口：

```text
panda_data.get_market_min_data(..., symbol_type="future")
```

系统封装：

```text
PandaAIAPI.get_futures_minute_bars
Router.get_china_futures_minute_bars
```

支持频率：

- 1m
- 5m
- 15m
- 60m

当前用途：

- Phase2 盘中执行确认。
- 15m K 线判断是否触发交易。
- 1m K 线开盘价作为成交基准价。
- 记录 `futures_intraday_decision` 审计。

### 3. 期货衍生数据

系统已支持通过 `get_futures_extra_snapshot` 读取多类确认数据，当前配置默认启用：

- basis
- warehouse_receipt
- net_flow
- variety_position_rank
- symbol_position_rank
- ls_ratio
- broker_net_margin_change
- broker_variety_profit
- broker_net_margin
- netposi_rank
- net_cap_change
- contract_daily_indicators
- contract_rank

这些数据主要用于 `market_confirmation` 和基本面上下文，不直接替代分析师信号，也不能单独授权开仓。字段缺失、接口无权限或记录数不足时，应写入 feature status / data quality，而不是兜底成方向信号。

## 三、主力合约与合约代码映射

系统内部常用合约代码格式为小写，例如：

```text
m2505
rb2601
```

PandaAI 查询通常需要带交易所后缀，例如：

```text
M2505.DCE
RB2601.SHF
ZN_DOMINANT.SHF
```

系统在 `PandaAIAPI` 内部处理：

- 主力连续合约：`M -> M_DOMINANT.DCE`
- 具体合约：`m2505 -> M2505.DCE`
- 郑商所短年份合约扩展：`cf601 -> CF2601.CZC`
- PandaAI 返回值再标准化回内部合约代码。

## 四、避免未来函数的原则

系统严格区分策略生成和执行：

1. Phase1 只使用盘前可得信息。  
   技术、基本面、新闻、市场确认都必须遵守 info cutoff。

2. Phase2 才读取 T 日盘中分钟线。  
   回测中可用完整历史分钟线，但逻辑上只允许使用当前 cutoff 以前的 bar。

3. Phase3 才读取 T 日结算价。  
   结算价只用于收盘后账务，不参与当日盘前策略生成。

Phase3 的实际执行者是 `src/agents/execution_team/accountant.py` 会计师智能体；它通过 `src/tools/agent_tools/execution/futures_settlement.py` 调用 PandaAI 结算价并完成账务入账。

## 五、盘中执行确认中的 PandaAI 用法

优化后 Phase2 的执行链路：

```text
pending recommendation
-> src/agents/execution_team/trader.py
-> Router.get_china_futures_minute_bars(frequency="15m")
-> Router.get_china_futures_minute_bars(frequency="1m")
-> intraday_execution.select_intraday_execution
-> FuturesExecutionEngine
```

当前规则：

- 15m 作为方向确认窗口。
- 1m 作为执行价格窗口。
- 新开仓或加仓需要触发条件。
- 平仓、减仓、换月使用第一个有效 1m 价格优先执行。
- 未触发写入明确 no-trade reason。
- `execution_action_value` 不能由 Trader 直接读取；它只能由 PM 消化进审计后的 `final_action_contract.execution_plan/execution_profile`，再由 Trader 按最终合约和盘中 PandaAI 数据执行或跳过。它不能创造策略方向、改变目标手数，也不能绕过 PM/Auditor。
- 回测和模拟盘共用同一套 Trader 执行语义。回测读取历史分钟线时仍必须按当前交易日和执行窗口裁剪，不得使用未来 bar 改写当时决策。

## 六、与 Finoview 和新闻数据的关系

PandaAI 不是唯一数据源：

- Finoview feather 用于基本面分析师。
- Future_news txt 用于新闻分析师。
- PandaAI 用于行情、市场确认、执行和结算。

三者共同服务于 15 个期货品类：

```text
BU, C, CF, EB, HC, I, J, M, MA, P, PB, RB, SR, TA, ZN
```

## 七、当前限制

1. PandaAI 分钟线已接入，但第一版执行规则仍是简洁规则，不是高频策略。
2. 夜盘处理以 PandaAI 返回的 `trading_date` 为准，后续可继续细化交易时段模板。
3. 新闻与基本面仍需要进一步结构化摘要，减少 LLM 输入噪声。
4. 部分 PandaAI 较慢或不稳定的衍生接口仍保持默认关闭，避免回测成本过高。
5. `src/assets/pandaai_market_cache.db` 是行情/衍生数据缓存，不是 SQLite 回测交易记录。清空回测交易记录时通常不需要删除它；只有怀疑市场数据缓存陈旧或需要强制刷新 PandaAI 数据时，才单独处理。

## 八、相关代码位置

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
```
