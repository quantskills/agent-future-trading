# AgentQuant 策略绩效优化记录

更新日期：2026-05-13

本文档用于持续提醒系统设计目标、当前架构边界、已经完成的优化，以及下一轮小规模回测前后的判断重点。AgentQuant 的优化不追求堆叠复杂模块，而是围绕“能产生更高质量期货交易策略，并且这些策略能在回测、模拟盘和实盘中一致复刻”这一核心目标逐步推进。

## 一、系统的两大核心功能

1. **期货交易策略生成与回测**

   系统在 Phase1 生成交易建议，在 Phase2 由 trader 执行交易，在 Phase3 由 accountant 完成日盯盘结算，在 Phase4 校验 recommendation、transaction、settlement、portfolio 与 phase status 的一致性。回测不是单独的研究脚本，而是尽量复用模拟盘/实盘同一套阶段化交易流程。

2. **模拟盘与实盘可复刻运行**

   系统保留日频策略生成逻辑，同时在 Phase2 支持盘中盯盘式执行。Phase1 只生成目标方向与目标仓位，Phase2 根据盘中价格、滑点、成交量和执行窗口确认是否成交，使回测中的交易逻辑可以被模拟盘和实盘尽量复刻。

## 二、六大核心优化目的

1. **提高策略 alpha**

   让 technical、fundamental、commodity_news 输出更高质量、更可解释、更可比对的交易信号，并减少低质量信号对最终交易的干扰。

2. **降低无效交易与错误反手**

   避免因为每日信号轻微波动而频繁开平仓；新开仓门槛低于反手门槛，反手必须有更强证据。

3. **增强账务、风控与阶段流一致性**

   确保 recommendation、transaction、settlement、position、PnL、margin 与 phase status 在四阶段运行中可追踪、可校验。

4. **支持 15 个期货品类差异化交易**

   覆盖 BU、C、CF、EB、HC、I、J、M、MA、P、PB、RB、SR、TA、ZN，并按能源、化工、黑色、有色、农产品设置不同分析侧重。

5. **提高系统自适应能力与可学习性**

   通过动态权重、交易记忆、auditor 审核结果和回测归因，为后续 contextual bandit 或更轻量的学习机制保留数据基础。

6. **提高实战部署性**

   所有回测策略都应尽量能在模拟盘和实盘中复刻；策略生成、盘中执行、日终结算和归因报告必须保持同一套口径。

## 三、当前已经完成的主要优化

1. **交易品类扩展至 15 个**

   系统配置、新闻数据、Finoview 基本面数据、PandaAI 行情与衍生数据调用逻辑已经围绕 15 个主力合约品类展开。

2. **三分析师信号质量优化**

   technical、fundamental、commodity_news 采用“结构化预处理 + 云端主模型信号生成”的默认流程；保留 local LLM / DeepAnalyze 调用开关。technical 偏短线时机，fundamental 偏中期方向锚，commodity_news 只在高相关、高新鲜度、高强度事件下显著影响决策。

3. **组合经理的自适应融合优化**

   portfolio_manager 默认只调用云端主模型，并继续承担市场自适应动态融合能力。系统按期货大类设置技术面、基本面、新闻面的基础权重，再结合信号质量、置信度、历史表现、PandaAI 确认层和 auditor 输出动态调整最终仓位。

4. **auditor 非 LLM 审核层**

   auditor 独立为确定性审核智能体，不调用 LLM。它负责质量门槛、弱组合限制、历史亏损组合限制、PandaAI 确认不足时的 reduce/block 决策，并把审核原因写入 recommendation snapshot。

5. **trader 与 accountant 智能体化**

   Phase2 的 order 逻辑已抽象为 trader，`run/order.py` 保留为运行脚本；Phase3 的 settlement 逻辑已抽象为 accountant，`run/settlement.py` 保留为运行脚本。执行工具放在 `src/tools/agent_tools/`，方便后续开发和审计。

6. **proposal 命名与阶段流语义优化**

   原主运行脚本 main 已改为 proposal，更清楚地表达 Phase1 的职责：生成交易建议，而不是直接交易。

7. **盘中执行与模拟盘复刻优化**

   Phase2 支持 15 分钟触发判断、1 分钟执行价格、开盘区间过滤、滑点模型、成交量检查和 loop 模式。回测、模拟盘和实盘尽量共用同一套 trader 逻辑。

8. **持仓周期与仓位再平衡控制层**

   portfolio_manager 新增 holding/rebalance 控制层：

   - technical 主要作为短线入场/出场滤波，不轻易推翻 fundamental 的中期方向。
   - fundamental 是中期仓位锚；当基本面仍支持当前持仓时，优先持有或小幅调仓。
   - commodity_news 仅在高质量事件冲击时改变仓位。
   - 已有持仓时，除非出现强反向信号、止损、风控或换月，否则优先保持或小幅增减仓。
   - 新开仓有最小仓位门槛，反手有更高证据门槛。
   - 目标仓位变化小于最小调仓阈值时不交易。
   - 农产品、黑色、化工、有色、能源设置不同最小持仓天数。
   - recommendation snapshot 新增 `rebalance_summary`，记录持仓天数、目标手数变化、估算换手额、调仓类型与调仓原因。

9. **PandaAI 数据质量处理**

   PandaAI `net_flow` 只有在多头与空头资金流两侧都有数据时才参与确认层打分；单侧缺失时不再制造虚假的确认或冲突。缺失数据仍记录为 data quality warning，但只作为“未知/降权”处理。

10. **日志与归因可读性增强**

   三分析师的技术指标、基本面因子、新闻证据、信号理由、tradeability、risk_flags、组合经理融合权重、auditor 决策、Phase2 执行计划和持仓再平衡原因都会进入日志或 recommendation snapshot，便于后续归因。

## 四、当前推荐的验证顺序

1. 重新建表或清理回测记录。
2. 先手动跑 1 个交易日完整四阶段，确认 proposal、order/trader、settlement/accountant、validate_phase_flow 全部通过。
3. 再跑 20 个交易日小规模自动回测，不建议立刻大规模回测。
4. 回测后重点检查：

   - 是否减少无意义开平仓与反手。
   - `rebalance_summary` 是否清楚记录持仓天数、调仓原因、换手额。
   - technical 是否主要影响执行时机，fundamental 是否真正承担中期方向锚。
   - PandaAI 缺失数据是否只降权，不再制造假冲突。
   - 亏损品种 P、RB、EB、J、ZN 是否因错误反手和过度交易减少而改善。
   - BU、I 等此前表现较好的品种是否没有被过度限制。

## 五、后续可能优化但暂不优先

1. attribution 报告进一步按“持仓天数、调仓原因、换手成本、平仓原因”聚合统计。
2. auditor 基于 20 个交易日小样本继续校准 reduce/block 阈值。
3. 若小规模回测证明交易记忆有稳定贡献，再考虑 contextual bandit，而不是立即引入复杂强化学习。
4. 继续修复历史日志中的终端乱码显示问题，但不影响数据库中的结构化字段与新日志口径。
# 2026-05-14 Execution Update: Post-Backtest Optimization Implemented

This section records the optimization changes executed after reviewing the latest backtest logs under `src/logs` and the SQLite database `src/assets/agentquant.db`.

## Backtest Diagnosis

- The four-phase workflow, execution, settlement, and validation path completed successfully for the latest window.
- The negative return was caused by strategy quality rather than accounting or execution errors.
- `new_entry` attribution was weak: 9 new-entry trade pairs had a 22.22% win rate.
- Main loss sources were `MA long`, `I long`, and `PB long`; `BU long` was the successful trend-holding case.
- The system was effectively long-only in realized trades, so high-confidence bearish setups were mostly defensive holds instead of small short probes.

## Executed Optimizations

1. Entry gating was tightened.
   - Added `MA long`, `I long`, and `PB long` to the strict ticker-side auditor watchlist.
   - Raised the base PandaAI confirmation threshold for new entries from `0.45` to `0.55`.
   - Added weak analyst combinations `[Bullish, Bearish, Bullish]` and `[Neutral, Bullish, Bullish]`.
   - Cold-start weak-combo entries are now blocked when confirmation is below `0.65`.

2. Loss feedback was accelerated.
   - `ticker_loss_control.loss_threshold` was tightened from `-8000` to `-3000`.
   - Consecutive-loss blocking was tightened from 3 days to 2 days.
   - `ticker_performance_control.min_trade_days` was reduced from 5 to 3.
   - Auditor attribution soft/hard sample thresholds were reduced from 5/10 to 3/6.

3. Position lifecycle control was added.
   - Positions are classified as `trend_position`, `probe_position`, `failed_position`, or `normal`.
   - Profitable, confirmed trend positions are protected from premature exits.
   - Failed positions can exit before the sector minimum holding period.
   - Unvalidated probe positions can be exited after 2 days when PnL and confirmation do not validate the entry.

4. High-quality bearish handling was added.
   - Added `directional_override_control`.
   - A strong blended bearish signal can override a non-short LLM target into a small `open_short` probe.
   - The short probe remains capped by `short_probe_max_ratio: 0.03` and still must pass auditor/PandaAI controls.

5. Technical quality filtering was strengthened.
   - `MA`, `I`, and `PB` bullish technical setups now require stronger trend, cleaner indicator alignment, and better volume confirmation.
   - Weak bullish watchlist setups are downgraded before they reach portfolio fusion.

6. Attribution reporting was made more readable.
   - New attribution Markdown output uses readable English section headers and table labels.
   - Weak-side suggestions now include low-sample but material weak directions and weak signal combinations.

7. LLM switching was implemented.
   - `OpenRouter` now uses `json_mode` structured output for compatibility with flexible metadata fields in analyst schemas.
   - `llm.inference` supports `openrouter.reasoning` config and loads env files from `AgentQuant/.env`, `AgentQuant.env`, or `AGENTQUANT_ENV_FILE`.
   - `dev.yaml` and the backup `planner.yaml` are switched to `OpenRouter` / `openai/gpt-5.5`.
   - DeepSeek `deepseek-v4-flash` access remains in the config as commented switch-back lines and the existing `deepseek` block remains supported.
   - Provider authentication and invalid-request errors are configured to raise immediately, so a broken LLM call will stop the backtest instead of silently producing all-neutral recommendations.

8. Follow-up gating correction was applied after the first GPT-5.5 rerun on 2025-01-06 produced zero trades.
   - The latest rerun confirmed that LLM calls were succeeding, so the no-trade result was no longer a model-availability problem.
   - The strict ticker-side watchlist was narrowed back to the intended scope: `MA long`, `I long`, and `PB long`.
   - Legacy strict entries on other tickers were removed because they were over-blocking beyond the original optimization target.
   - The single-supporter conflict block threshold was relaxed from `0.65` to `0.50`, so medium-confirmation probe ideas can be reduced instead of always being hard-blocked.
   - This keeps the weak-side protection on the main loss sources while allowing non-watchlist names to express small cold-start positions when confirmation is not outright poor.

9. BU-style conflicted-but-confirmed probe handling was corrected.
   - A new-entry signal with weak raw strength but strong PandaAI confirmation can now be reduced instead of being hard-blocked when the conflict is only partial.
   - This specifically protects cases like `BU` on 2025-01-07, where two analysts supported the long side and PandaAI confirmation score was high enough, but two conflicting sub-features previously forced the target to zero.
   - The relaxed path still requires strong confirmation and only produces a capped probe through the existing conflict multiplier and cold-start multiplier.
   - Weak watchlist names such as `MA/I/PB long` remain under the stricter quality gate and are not loosened by this change.

10. Opening-range timing was aligned between backtest and paper trading.
   - `require_complete_opening_range: true` was added to intraday confirmation.
   - Opening-range breakout/down triggers are now disabled until the configured opening range has fully formed.
   - Backtests skip signal bars earlier than the completed opening range instead of using full-day data to evaluate them.
   - Paper trading reports `intraday_opening_range_incomplete` before the opening range is complete, then uses the same trigger logic as backtest after completion.

## Next Validation Criteria

- `MA long`, `I long`, and `PB long` should no longer repeatedly pass as low-quality new entries.
- `BU long` should remain holdable when it becomes a confirmed trend position.
- `new_entry` win rate should improve materially from 22.22%.
- Total trades should not expand sharply.
- Phase4 validation and settlement reconciliation must remain clean.
- The next A/B backtest should compare the previous DeepSeek run against the new OpenRouter GPT-5.5 configuration.

# 2026-05-15 Execution Update: Attribution-Driven Template Calibration

This section records the targeted optimization executed after reviewing the latest
backtest, attribution report, and ticker contribution/price-entry charts. The
core objective remains unchanged: generate higher-quality futures strategies
that can be reproduced consistently across backtest, paper trading, and live
trading. Therefore this update does not change accounting, settlement, Phase4
validation, intraday execution, or LLM routing.

## Latest Diagnosis

- Strong captured templates: `BU long`, `J short`, `RB long`, and `ZN short`.
- Main weak templates: `P long`, `TA long`, `C long`, `I short`, `HC short`,
  and `SR short`.
- The system already supports short exposure; the issue is not "no shorting",
  but uneven ticker-side short quality.
- Several losing setups were driven or reinforced by commodity news without a
  reliable fundamental anchor.
- Attribution learning must not punish a strong ticker-side template because of
  one small losing sub-combo.

## Executed Optimizations

1. Strong ticker-side template protection was added.
   - Added `protected_ticker_sides` for `BU long`, `J short`, `RB long`, and
     `ZN short`.
   - These templates are not unconditional approvals. They can still be blocked
     by market confirmation, severe performance decay, or risk controls.
   - The protection only prevents generic weak-combo/cold-start rules from
     hard-blocking a historically strong side when confirmation is acceptable.

2. Weak ticker-side template rules were added.
   - Added `weak_ticker_side_rules` for `P long`, `TA long`, `C long`,
     `I short`, `HC short`, and `SR short`.
   - `P long / Bearish|Neutral|Bullish`, `TA long / Bullish|Neutral|Bullish`,
     `C long / Bullish|Neutral|Bearish`, `HC short / Neutral|Bearish|Neutral`,
     and `SR short / Bearish|Neutral|Neutral` now require stronger confirmation
     and qualified analyst support.
   - `I short` is tightened instead of globally disabled, so successful bearish
     templates such as `J short` are not harmed.

3. Commodity-news-only directional trades were tightened.
   - Added `news_driver_control`.
   - If commodity news is the only directional supporter, the auditor now
     requires high tradeability, sufficient news confidence, freshness,
     relevance, and PandaAI confirmation.
   - If news supports the target but fundamental does not anchor it, the target
     is capped rather than freely expanded.

4. Static attribution caps were aligned with the new attribution readout.
   - Added static caps for `P long`, `TA long`, and `SR short`.
   - Kept `C long` capped.
   - Removed the old `RB long` static cap so the current profitable `RB long`
     template is not unnecessarily muted.

5. Phase4 no-trade classification was updated.
   - Added the new auditor reasons to expected no-trade classifications:
     `weak_ticker_side_quality_gate`, `weak_ticker_side_cap`,
     `news_only_directional_trade`, `news_without_fundamental_anchor`,
     `protected_ticker_side_weak_combo`, and
     `protected_ticker_side_cold_start`.

## Validation

- `dev.yaml` parses successfully with the new config.
- `tests.test_phase_flow_regression` passes: 25 tests OK.

## Next Backtest Checks

- Confirm `BU long`, `J short`, `RB long`, and `ZN short` are not over-blocked.
- Confirm `P long`, `TA long`, `C long`, `I short`, `HC short`, and `SR short`
  either trade less or require visibly stronger confirmation.
- Confirm commodity-news-only signals no longer push weak new entries.
- Confirm Phase4 validation and cash-plus-margin reconciliation remain clean.

# 2026-05-15 Execution Update: DB-Backed Real-Time Strategy Memory

This section records the follow-up memory optimization. The purpose is to make
the system adjust future trade audits from its own validated trading history,
without hard-coding every profitable or weak template into `dev.yaml`.

## Why This Was Added

- Static attribution rules help immediately, but they do not learn as the
  backtest progresses.
- Strong templates such as `BU long`, `J short`, `RB long`, and `ZN short`
  should gain protection from repeated validated wins.
- Weak templates should be tightened only after completed round-trip evidence,
  so the system avoids overreacting to a single open position or incomplete day.
- The memory layer must not change settlement/accounting logic or introduce
  look-ahead bias.

## Executed Optimizations

1. Added a local strategy memory table.
   - Added `strategy_memory` to SQLite initialization.
   - Memory rows are keyed by `config_id`, `ticker`, `side`, `signal_combo`,
     and `source`.
   - Stored fields include `memory_state`, `sample_count`, `win_rate`,
     `net_pnl`, `avg_pnl`, `confidence_score`, `valid_until`, and a JSON
     payload with the attribution summary.

2. Added automatic Phase4 memory refresh.
   - After `phase4` is marked completed, the database refreshes
     `strategy_memory` from completed futures round-trip trade pairs.
   - The Phase4 validator passes the active `strategy_memory` config into the
     refresh step, so sample thresholds, PnL thresholds, and expiry days remain
     weak-parameter tunable from `dev.yaml`.
   - Rollover transactions are excluded from strategy memory, because this
     memory should learn signal quality, not mechanical contract replacement.
   - Memory only uses trade pairs whose close date is on or before the
     completed trading day.

3. Added memory states for real-time audit use.
   - `protected`: repeated positive attribution; generic weak/cold-start rules
     should avoid hard-blocking unless confirmation or risk controls fail.
   - `watchlist`: early weak attribution; new exposure is capped unless current
     confirmation is strong enough.
   - `weak_block`: repeated weak attribution; new exposure requires stronger
     confirmation and qualified analyst support.
   - `recovering`: positive but not yet strong enough for protected treatment.

4. Connected memory to the trade auditor.
   - Portfolio manager reads `get_strategy_memory()` before audit when
     `strategy_memory.enabled` is true.
   - Trade auditor now records the memory payload in diagnostics and audit
     metadata.
   - Protected memory can strengthen a ticker-side template.
   - Watchlist/weak-block memory can cap or block new exposure before the
     older static weak-template rules are applied.

5. Added config-level switches.
   - Added `strategy_memory.enabled`.
   - Added sample, win-rate, PnL, expiry, and audit threshold settings.
   - These settings keep memory behavior configurable while preserving the
     existing DeepSeek/OpenRouter model switch.

6. Updated validation classification.
   - Added `strategy_memory_weak_block` and
     `strategy_memory_watchlist_cap` as expected Phase4 no-trade reasons.
   - This prevents a correctly memory-blocked day from being treated as an
     unknown workflow failure.

## Guardrails

- Memory is refreshed from completed trade pairs only.
- Memory does not write new static ticker rules into `dev.yaml`.
- Memory is not an unconditional permission to trade; market confirmation,
  severe performance controls, margin controls, and settlement validation still
  dominate.
- Memory is local-DB backed and designed for `--local-db` backtests first.

## Validation

- Added regression coverage for:
  - memory-based auditor blocking via `strategy_memory_weak_block`;
  - SQLite refresh from completed trade pairs;
  - next-day memory lookup for protected and watchlist templates.

## Next Backtest Checks

- Confirm strong templates gain protection only after validated completed wins.
- Confirm weak templates become capped/blocked only after enough completed
  evidence.
- Confirm no look-ahead behavior: a same-day open position should not affect
  memory until Phase4 validates completed round trips.
- Confirm Phase4 validation remains clean on zero-transaction days generated by
  strategy memory.
