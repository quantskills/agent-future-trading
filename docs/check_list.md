# AgentQuant 2025-01-16 起续跑回测验收清单

生成日期：2026-05-23

适用范围：从 `2025-01-16` 开始继续运行自动化回测后的结果检查。

建议命令：

```powershell
cd D:\research\AgentQuant\src
conda activate deepfund
python run\backtest.py --config config\dev.yaml --start-date 2025-01-16 --end-date <目标结束日期> --local-db
python evaluation\evaluate_config.py --config config\dev.yaml --start-date 2025-01-16 --end-date <目标结束日期> --local-db
```

## 一、续跑口径

1. 本清单只验收 `2025-01-16` 及之后新产生的回测结果。`2025-01-02` 至 `2025-01-15` 已经完成短窗口冒烟，但样本太短，不能验收长期收益、学习曲线、资金利用率稳定性、Neutral 改善趋势和数据质量稳定性。
2. 继续使用同一个数据库是允许的，因为 `2025-01-02` 至 `2025-01-15` 的学习结果可以作为真实顺序回测中的历史记忆输入。但正式评估新增优化项时，应使用 `--start-date 2025-01-16`，避免把旧代码生成的日志混入新验收口径。
3. 第 1 至第 5 条新增优化的报告字段从本次代码修改后才会完整出现，旧窗口缺少这些新字段时，不应视为未通过。
4. 每个交易日必须完成 Phase1、Phase2、Phase3、Phase4 后再计入验收。Phase4 失败的日期不能纳入学习效果统计。

## 二、五条新增优化验收项

| 编号 | 验收主题 | 检查位置 | 通过标准 | 失败/预警信号 |
|---|---|---|---|---|
| N1 | PandaAI 扩展数据适配层 | `src/logs/analyst_decisions/<run_id>/*_fundamental.md`、`market_confirmation` payload、`agentquant.db` 中 recommendation snapshot | `contract_rank`、席位持仓、净资金、基差等字段有明确状态：`ok`、`no_data`、`unsupported`、`parameter_error`、`fallback_covered` 等；`get_future_contract_rank` 不再出现缺少 `rank_type` 的参数错误 | 大量 `parameter_error`、`HTTP 403`、同一字段长期 0 行却没有状态解释 |
| N2 | market confirmation missing 告警降噪 | `src/logs/*.log`、daily summary JSON、Phase4 warnings | 不再把所有 0 行混成单一 warning；日志能区分参数错误、无数据、不支持、provider 错误、fallback 覆盖 | 每天大量重复 `market confirmation data missing`，且无法定位字段、品种、原因 |
| N3 | 资金利用率专项诊断 | `capital_deployment_state.deployment_plan_json`、daily summary、`evaluate_config.py` 输出 | `under_deployed_reason_counts`、`under_deployed_category_counts`、`capital_alpha_release_candidate_count`、`capital_parameter_review_counts` 都有统计；能区分 `llm_neutral`、`position_matched`、`intraday_trigger_not_met`、`trade_auditor_block`、`minimum_new_entry_threshold` | 仍只看到一个笼统 under-deployed 数字；不知道是信号少、已有仓位匹配、盘中触发过严还是 auditor 压制 |
| N4 | 资金利用率提高是否合理 | `evaluate_config.py` 保证金风险区、Phase4 capital diagnostics | 平均保证金比例逐步接近 8%-12%；强机会日允许接近 16%-20%；扩仓候选必须来自 protected/deployable/recovering 或高确认机会 | 保证金比例仍长期 2%-4%，且原因不是 alpha 不足；或为了提高利用率而放大 weak/watchlist/低质量信号 |
| N5 | 因果候选变可验证交易权限 | `causal_review_candidate`、`adaptive_policy_state`、reviewer report `Causal Rule Validation` | `notes_only_pending_rule_validation` 应逐步被验证为 `validated_rule_applied`、`validated_rule_rejected` 或 `insufficient_evidence_pending_rule_validation`；只有样本成熟后才生成 `policy_type='causal_review_rule'` | 候选长期停留 notes only；或 LLM 笔记未经样本验证直接变成交易权限 |
| N6 | learned vs unlearned 表现 | reviewer report `Learned vs Unlearned Trade Performance`、`evaluate_config.py` 学习与审计区 | `learned_trade_count` 随时间增加；学习生效交易的净 PnL、胜率、平均盈亏优于或至少不差于 unlearned 交易 | learned 交易长期为 0；或 learned 交易明显劣于 unlearned，且无 rollback/cap |
| N7 | Neutral 责任化验收 | reviewer report `Neutral Accountability`、`evaluate_config.py` 的 Neutral 输出 | 输出 `neutral_signal_ratio`、`neutral_accountability_complete_rate`、`neutral_category_counts`、`neutral_by_analyst`；能区分 `reasonable_avoidance` 与 `evidence_gap_conservative` | Neutral 比例高但没有分类；`unaccountable_neutral` 较多；`evidence_gap_conservative` 长期高企说明数据/证据不足导致保守 |
| N8 | Neutral 是否改善方向信号质量 | `neutral_category_counts`、交易表现、分析师报告 | Neutral 不必机械下降，但无理由 Neutral 应下降；`reasonable_avoidance` 占比应高于 `unaccountable_neutral`；方向信号的胜率和净 PnL应提高 | Neutral 被强行减少但亏损增加；或者 Neutral 仍主要来自证据不足和缺字段 |

## 三、原优化方案中仍需靠 2025-01-16 后验收的项目

### 1. 收益质量

- 检查 `evaluate_config.py` 输出：总收益率、年化收益率、账户权益夏普、最大回撤、波动率、日胜率、完整交易对胜率、profit factor 或等价盈亏质量指标。
- 通过标准：至少 6 个月窗口账户权益收益转正，并力争达到 `+1%` 至 `+3%`；最大回撤不能靠低仓位假装优秀。
- 旧窗口状态：`2025-01-02` 至 `2025-01-15` 样本太短，且完整交易对不足，不能验收。

### 2. 信号质量

- 检查 analyst decision report、`signal_context_history`、`analyst_performance`、`signal_template_performance`。
- 通过标准：方向信号必须带结构化字段，包括 trend stage、方向锚、业务质量分、无效位、horizon、反证、Neutral 责任说明。
- 重点关注：高质量方向信号是否比 Neutral/低质量方向信号有更好的后验 PnL。

### 3. 资金利用率

- 检查 `daily_settlement.margin_ratio`、`capital_deployment_state`、`evaluate_config.py` 的保证金风险区。
- 通过标准：平均保证金比例逐步接近基础目标 `8%-12%`；强机会日出现 `16%-20%` 的合理部署；未达目标时能解释是 alpha 不足还是系统保守。
- 旧窗口状态：1 月上半月仍偏低，且新专项诊断是后续新增，必须从 1 月 16 日后重新观察。

### 4. PM 资金释放逻辑

- 检查 PM recommendation snapshot 中 `strategy_controls`、`capital_utilization_learning`、`capital_utilization_same_side_add_on`。
- 通过标准：PM 只对 protected/deployable/recovering、高确认、非硬风险机会释放资金；不能对 watchlist/weak_block/低质量信号硬扩仓。
- 失败信号：`system_under_deployed` 长期高，但 `alpha_release_candidates` 明明存在却没有被使用。

### 5. Auditor 风险分层

- 检查 `trade_auditor_decision_counts`、recommendation snapshot 的 `trade_auditor.reasons`。
- 通过标准：block 主要对应硬风险、成熟弱模板或严重数据问题；protected/deployable 不应被普通保守规则误杀。
- 失败信号：`trade_auditor_block` 成为最大资金空转原因，且不是硬风险。

### 6. Trader 择时和执行

- 检查 Phase2 intraday decision、transaction report、成交价基准、滑点、MFE/MAE 或可替代入场质量指标。
- 通过标准：减少高位追多、低位追空；未成交要能解释是盘中触发未满足、价格不可执行、还是风控阻断。
- 旧窗口状态：短窗口无法验收 MFE/MAE 和出场质量。

### 7. Reviewer 学习闭环

- 检查 reviewer report、`learning_event_log`、`strategy_memory_history`、`adaptive_policy_state`、`provisional_policy_state`、`config_learning_overlay`、`template_prior.json`。
- 通过标准：Phase4 每天稳定写学习；模板表现成熟后进入策略权限；学习有样本数、过期、rollback、防过拟合约束；被采纳学习后的交易表现优于未采纳交易。
- 失败信号：学习只停留在日志或 notes，不影响 PM/auditor/trader。

### 8. 数据与无未来函数

- 检查 analyst reports 中 `info_cutoff`、`fundamental_cutoff`、PandaAI extra factor `reference_date`、Finoview snapshot 日期。
- 通过标准：Phase1 只使用 T-1 及以前可见数据；PandaAI extra factor 使用 T-1 或更早；Phase4 只在结算后学习。
- 失败信号：T 日收盘/结算/未来新闻进入盘前信号；旧数据库未来学习污染历史窗口。

### 9. Artifact 与自由文本控制

- 检查 `artifact_contract_validation_pass_rate`、`free_text_control_violation_count`、recommendation snapshot。
- 通过标准：artifact 契约通过率接近 100%；交易权限、仓位、风控、执行不由自由文本直接控制。
- 失败信号：某个 agent 输出字段下游没有消费，或下游靠自然语言解析交易动作。

### 10. 归因解释能力

- 检查 `2025-xx-xx_transaction.log`、reviewer report、evaluation summary。
- 通过标准：每笔交易能归因到分析师、模板、horizon、ticker-side、PM 动作、auditor 决策、trader 入场/出场；每个未交易建议能说明硬风险、软风险、资金容量不足、信号质量不足或盘中未触发。
- 失败信号：亏损、空仓、资金未部署无法归因。

## 四、每日检查清单

每跑完一个交易日，至少检查：

- Phase1、Phase2、Phase3、Phase4 是否全部 completed。
- 是否有 `ERROR`、`Traceback`、PandaAI `HTTP 403`、登录失败、结算价缺失。
- 当日 transaction log 是否生成。
- 当日 reviewer report `.md` 与 `.json` 是否生成。
- `capital_deployment_state` 是否写入。
- `neutral_accountability_review` 是否写入 `learning_event_log`。
- `causal_review_candidate` 是否产生，并在样本成熟后被验证。
- `market_confirmation` 缺失是否有明确状态，不是噪声 warning。

## 五、阶段性评估节点

| 节点 | 建议窗口 | 目的 |
|---|---|---|
| 冒烟续跑 | 2025-01-16 至 2025-01-20 | 确认新增字段、报告、日志、数据库写入均正常 |
| 小样本诊断 | 2025-01-16 至 2025-01-31 | 检查资金利用率诊断、Neutral 分类、PandaAI extra factor 状态是否稳定 |
| 月度复盘 | 2025-01-16 至 2025-02-28 | 初步观察 learned vs unlearned、模板成熟和资金释放 |
| 正式验收 | 至少 6 个月 | 验收收益质量、资金利用率、学习曲线和风控稳定性 |
| 稳健性复核 | 12 个月 | 检查策略是否跨市场阶段稳定 |

## 六、继续回测后的推荐评估命令

```powershell
cd D:\research\AgentQuant\src

# 新优化项只按 2025-01-16 后的新结果验收
python evaluation\evaluate_config.py --config config\dev.yaml --start-date 2025-01-16 --end-date <目标结束日期> --local-db

# 如需看完整账户路径，可另跑全窗口，但不要把全窗口用于新增诊断项验收
python evaluation\evaluate_config.py --config config\dev.yaml --start-date 2025-01-02 --end-date <目标结束日期> --local-db
```

## 七、结论判定

从 `2025-01-16` 之后的新回测结果看，若同时满足以下条件，才可认为本轮优化进入正式有效状态：

1. 回测流程稳定，无 Phase3/Phase4 阻塞错误。
2. PandaAI 与 market confirmation 的数据质量可解释，日志不再被无意义 warning 淹没。
3. 资金利用率提升来自高质量 alpha，而不是无原则加仓。
4. 学习规则能从候选被验证为权限，并且 learned 交易表现优于 unlearned。
5. Neutral 能被责任化拆解，`unaccountable_neutral` 低，`evidence_gap_conservative` 有明确数据改进方向。
6. 至少 6 个月窗口收益、回撤、胜率、手续费、资金利用和归因均达到 `strategy_performance_optimization.md` 的正式验收标准。
