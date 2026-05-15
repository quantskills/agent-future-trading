# AgentQuant 期货交易机制说明

更新日期：2026-05-12

本文档说明优化后 AgentQuant 的交易机制、四阶段边界、日频策略与盘中执行确认的关系，以及 `auditor`、`planner`、`portfolio_manager`、`trader`、`accountant` 的职责划分。

## 一、系统定位

AgentQuant 不是高频交易系统。当前系统生成的是日频期货交易策略：

- Phase1 在交易日前或盘前生成当日策略 recommendation。
- Phase2 根据 recommendation 决定是否执行。
- 分钟数据只用于改善入场、出场和跳过不合适价格，不改变策略频率。

换句话说，系统仍然是“日频策略 + 盘中执行确认”，不是“15 分钟策略生成系统”。

## 二、四阶段边界

| 阶段 | 脚本 | 主要职责 |
|---|---|---|
| Phase1 | `run/proposal.py` | 生成三类分析师信号、聚合信号、生成 futures recommendation |
| Phase2 | `run/order.py` | 运行 `trader` 智能体，根据 recommendation 执行交易，可使用盘中确认 |
| Phase3 | `run/settlement.py` | 运行 `accountant` 智能体，完成结算、保证金、PnL、正式持仓和账户状态 |
| Phase4 | `run/validate_phase_flow.py` | 校验 recommendation、transaction、settlement、phase status 与账务 |

关键约束：

- Phase1 不写真实交易。
- Phase2 是正常交易写入入口。
- Phase3 是正式结算入口。
- Phase4 只审计和校验，不改写交易逻辑。

## 三、回测运行方式

从 `src/` 目录运行：

```powershell
python run/backtest.py --config config/dev.yaml --start-date 2025-01-01 --end-date 2025-02-28 --local-db
```

建议先小规模烟雾测试：

```powershell
python run/backtest.py --config config/dev.yaml --start-date 2025-01-06 --end-date 2025-01-10 --local-db
```

## 四、模拟盘运行方式

模拟盘仍然按四阶段运行。Phase2 使用 `--loop` 持续等待盘中触发：

```powershell
python run/proposal.py --config config/dev.yaml --trading-date YYYY-MM-DD --local-db
python run/order.py --config config/dev.yaml --trading-date YYYY-MM-DD --local-db --loop
python run/settlement.py --config config/dev.yaml --trading-date YYYY-MM-DD --local-db
python run/validate_phase_flow.py --config config/dev.yaml --trading-date YYYY-MM-DD --local-db
```

模拟盘时不需要每 15 分钟手动跑一次 `order.py`。正确方式是让 Phase2 以 `--loop` 方式保持运行，由 `src/agents/trader.py` 中的交易员智能体按配置检查盘中触发条件。

## 五、盘中执行确认

当前配置入口：

```yaml
execution:
  intraday_confirmation:
    enabled: true
    decision_frequency: "15m"
    execution_frequency: "1m"
    opening_range_minutes: 30
    require_complete_opening_range: true
    min_execution_volume: 1
    max_chase_ratio: 0.015
    finalize_after: "15:00:00"
    loop_check_interval_seconds: 300
```

执行规则：

- 新开仓或加仓，需要已完成的 15m K 线满足触发条件。
- `opening_range_minutes` 对应的开盘区间必须完整形成后，才允许使用开盘区间突破/跌破触发器；未完成时模拟盘继续等待，回测也跳过早于完整开盘区间的信号 K 线。
- 多头更偏好价格站上 VWAP 与开盘区间。
- 空头更偏好价格跌破 VWAP 与开盘区间。
- 真正执行时，使用下一根有效 1m K 线开盘价作为基准，再叠加 tick 滑点。
- 减仓、平仓、换月不应被过度延迟，可更积极使用第一根有效 1m 基准价。

未成交必须记录原因，例如：

- `intraday_trigger_not_met`
- `intraday_waiting_for_trigger`
- `intraday_opening_range_incomplete`
- `intraday_no_valid_bar`
- `after_last_entry_time`
- `duplicate_execution_prevented`

盘中审计表：

```text
futures_intraday_decision
```

## 六、夜盘与交易日归属

国内期货夜盘通常归属于下一个交易日。系统应以数据供应商返回的 `trading_date` 为准。

原则：

- Phase1 生成某交易日策略时，只使用该交易日之前已经可得的信息。
- Phase2 才读取当日盘中分钟线。
- 回测时分钟线需要按 cutoff 过滤，避免未来函数。
- 模拟盘时 `order.py --loop` 只能使用当前时间之前已经形成的数据。

## 七、auditor、planner、portfolio_manager、trader、accountant 边界

### auditor

`src/agents/auditor.py` 是当前真实启用的轻量级非 LLM 交易审计员。

职责：

- 读取三类分析师信号组合。
- 读取 PandaAI 市场确认。
- 读取历史 ticker + side 与 conditional combo 归因。
- 对 proposed exposure 做 `allow / reduce / block / hold`。
- 写入可审计的 state/action/reward_source，为后续交易记忆或 contextual bandit 做准备。

### planner

`src/agents/planner.py` 只保留 legacy LLM analyst selector。

它由 `planner_mode` 控制，当前 `src/config/dev.yaml` 中保持：

```yaml
planner_mode: false
```

因此当前系统默认不启用旧 planner。

### portfolio_manager

`portfolio_manager` 仍然是最终组合经理，职责包括：

- 聚合三类分析师信号。
- 应用动态权重。
- 调用 `TradeAuditor` 做交易审计。
- 计算目标仓位和风险等级。
- 生成最终 futures recommendation。

投资组合经理仍可以调用 LLM，因为它要解释和整合三类分析师信号；但 `trade_auditor` 本身不调用 LLM。

### trader

`src/agents/trader.py` 是 Phase2 的交易员智能体。它不重新判断交易方向，也不调用 LLM，而是读取 Phase1 已保存的 futures recommendation，调用 `src/tools/agent_tools/` 下的执行工具完成：

- recommendation 到 Phase2 订单的翻译。
- 15m / 1m 盘中执行确认。
- 滑点、手续费、保证金和成交记录。
- `phase2_execution`、`phase2_order_plan`、`futures_intraday_decision` 等审计记录。

`src/run/order.py` 只是稳定的命令行入口，负责启动 trader 智能体。

### accountant

`src/agents/accountant.py` 是 Phase3 的会计师智能体。它不重新判断交易方向，也不调用 LLM，而是读取 Phase2 已完成的交易流水，调用 `src/tools/agent_tools/futures_settlement.py` 完成：

- 结算价读取。
- 逐品种 PnL、手续费、保证金和账户余额计算。
- 官方 portfolio 持久化。
- `daily_settlement` 与 `ticker_daily_pnl` 写入。
- Phase2 交易入账标记和下一交易日换月 recommendation 检测。

`src/run/settlement.py` 只是稳定的命令行入口，负责启动 accountant 智能体。

## 八、账务和审计表

核心表：

- `futures_recommendation`
- `futures_transactions`
- `futures_intraday_decision`
- `daily_settlement`
- `ticker_daily_pnl`
- `trading_day_phase`

检查重点：

- recommendation 与 transaction 是否一一可追踪。
- transaction 是否只由 Phase2 正常写入。
- settlement 是否准确反映手续费、保证金、PnL 与账户余额。
- 零成交是否都有可解释原因。

## 九、当前最重要的运行建议

先做少量交易日烟雾测试，确认 Phase2 盘中确认、Phase3 结算和 Phase4 审计稳定后，再跑 2025 年 1-2 月回测。
