# Matrix Action Canonical

本文是 action-value 动作语义矩阵。所有进入 Researcher 写入、PM 消费、Reviewer 复盘理解、Research 后续研究链路和 PG 审计的交易动作，必须走同一条解释链：

```text
action_name -> canonical_action_family -> action_value_lane / learning_lane -> action_preference
```

可执行来源是 `src/tools/common/final_action_semantics.py`。本文只记录动作矩阵口径，不新增第二套字段名、别名和字段语义。

## 核心边界

- `action_preference` 是历史学习后的行为偏向，不是明日交易指令。
- 具体执行动作只能来自 PM 当日签出的 `final_action_contract`，并经 Auditor 审计后由 Trader 执行。
- Researcher、PM、Reviewer、PG 和后续研究链路共享 `final_action_semantics.py` 解释动作语义。
- Trader 不读取 action-value 改方向、改手数或放宽触发；Trader 只执行 `final_action_contract` 里的合约化执行字段。
- Accountant 不消费 action-value 语义，只按成交、持仓、结算价、手续费、滑点、保证金和合约乘数入账。
- 换月、强平、运营风控等非策略动作必须用非策略 `source_type` 分账，不能写成策略 action-value。

## PM Artifact Boundary

- `final_action_contract.learning_used.alpha_setup_action_values` 只保存完整 canonical action-value 正式学习证据。
- `similar_sql_prior` / fallback prior 缺 `canonical_action_family`、缺 `action_preference` 或 `canonical_action_value == false` 时，只能保留在 `learning_used.memory_retrieval.rejected_or_downgraded`。
- `rejected_or_downgraded` 中的 prior 只作 provenance / diagnostics，不参与 score、rank、手数、资金部署或 `final_action`。

## Canonical Action Family Table

| `action_name` | `canonical_action_family` | `action_value_lane / learning_lane` | 正向 `action_preference` | 负向 / 保护偏向 | 业务边界 |
|---|---|---|---|---|---|
| `wait` / `no_trade` / `flat` | `no_trade` | `hold` 或诊断线 | 无 | `negative_hold_revalidate` 只可作保护/再验证 | 不产生 open 授权，不支持 `positive_candidate_open`。 |
| `hold` / `hold_position` / `continue_hold` | `hold` | `hold` | `positive_candidate_hold` | `negative_hold_revalidate` | 评价已有持仓是否应继续持有，不证明新开仓有效。 |
| `observe` / `watchlist` | `observe` | `hold` | 空 `action_preference` | `negative_hold_revalidate` / `negative_revalidate` / `tail_loss_protect` | 空偏向是合法观察语义；禁止 `positive_candidate_open` / `positive_candidate_exit` / `positive_candidate_execution` / `positive_candidate_hold`；观察事实不能冒充 open/add 学习。 |
| `conditional_probe` / `conditional_monitor` / `watch_trigger` | `conditional_monitor` | `conditional_monitor` | 无 | `negative_revalidate` / `tail_loss_protect` | 条件监控不是已执行开仓；只有 PM 当日合约触发后，才能形成执行事实。 |
| `open` / `open_long` / `open_short` | `open_add_new_risk` | `open` | `positive_candidate_open` | `negative_revalidate` / `tail_loss_protect` | 标准新增风险开仓学习。 |
| `open_probe` / `open_real` | `open_add_new_risk` | `open` | `positive_candidate_open` | `negative_revalidate` / `tail_loss_protect` | 探针或真实预算开仓都属于新增风险 family。 |
| `add_or_open` / `new_or_adjust` | `open_add_new_risk` | 默认 `open`；有 `current_lots -> target_lots` 时可归 `add` | `positive_candidate_open` | `negative_revalidate` / `tail_loss_protect` | 当前报错场景属于该类：业务含义是 open/add family，不能被 PG 当成非 open family。 |
| `add` / `scale` / `increase` / `increase_position` | `open_add_new_risk` | `add` | `positive_candidate_open` | `negative_revalidate` / `tail_loss_protect` | 同方向扩大风险暴露，学习上仍归 open/add 新增风险 family。 |
| `reverse` | `open_add_new_risk` | `open` | `positive_candidate_open` | `negative_revalidate` / `tail_loss_protect` | 反手必须由 PM 合约明确先退出旧方向再授权新风险；action-value 只表达新风险 family 学习，不自动生成反手执行。 |
| `reduce` / `trim` / `decrease` / `reduce_position` / `scale_down` / `reduce_only` | `reduce_exit` | `reduce` | `positive_candidate_exit` | `negative_revalidate` / `tail_loss_protect` | 评价降低已有风险暴露是否有效，不支持 open/add。 |
| `reduce_or_exit` / `close_or_reduce` / `decrease_position` | `reduce_exit` | `reduce` 或 `exit` | `positive_candidate_exit` | `negative_revalidate` / `tail_loss_protect` | 具体是减仓还是退出由当日 `current_lots -> target_lots` 与 PM 合约决定。 |
| `exit` / `close` / `close_long` / `close_short` / `close_position` | `reduce_exit` | `exit` | `positive_candidate_exit` | `negative_revalidate` / `tail_loss_protect` | 标准退出或平仓学习。 |
| `risk_exit` / `flatten` | `reduce_exit` | `exit` | `positive_candidate_exit` | `negative_revalidate` / `tail_loss_protect` | 风险退出、清仓学习，不能反向支持开仓。 |
| `execution` / `intraday` / `trigger` / `fill` 及包含这些词的执行动作 | `execution` | `execution` | `positive_candidate_execution` | `negative_revalidate` / `tail_loss_protect` | 只评价执行 profile、触发和成交质量；可以被 PM 用于合约化执行字段，不能进入新开仓 rank 或直接授权交易。 |

未知动作不得靠各模块私有字符串集合解释。若要进入策略 action-value，写入端必须先在 `final_action_semantics.py` 中有明确 family/lane 语义；否则只能作为诊断或被拒绝。

## 从学习偏向到执行动作

学习偏向不能反推明日具体执行动作。正确链路是：

1. Researcher 在已结算事实或复盘事实上写 action-value，并保存 `canonical_action_family`、`action_value_lane`、`learning_lane` 和 `action_preference`。
2. PM 通过 `decision_memory_retrieval` 读取同 family/lane 的历史学习，只把它用于评分、排序、降级、保护、再验证或执行 profile 输入。
3. PM 结合当日 `signal_collection_contract`、仓位、方向、资金预算、风险边界、失效条件和审计要求，签出唯一 `final_action_contract`。
4. Trader 只按审计通过的 `final_action_contract.current_lots/target_lots/lots_delta/final_action` 与合约化触发字段执行。

PM 最终合约里的 `decision_learning_rows` 只能是 Step6 按最终生命周期重新路由出的决策层学习证据；早期 Step2 router 结果只能作为 provenance / diagnostics，不得直接作为最终执行合约的决策层 trace。

典型执行归类由 PM 当日合约决定：

| `current_lots -> target_lots` | 执行含义 |
|---|---|
| `0 -> 非 0` | open-family |
| 同方向且 `abs(target_lots) > abs(current_lots)` | add/scale-family |
| 同方向且 `abs(target_lots) < abs(current_lots)` | reduce-family |
| `非 0 -> 0` | exit-family |
| 方向反转 | 先退出旧方向；只有 PM 签出新风险授权时才进入 open-family |
| `target_lots == current_lots` | hold / wait |

## PG Audit Rule

PG 审计检查统一语义一致性，不审死字符串，也不根据学习偏向反推具体交易动作：

- `positive_candidate_open` 必须满足 `canonical_action_family=open_add_new_risk`，且 lane 属于 `open/add/scale/increase`。
- `positive_candidate_exit` 必须满足 `canonical_action_family=reduce_exit`，且 lane 属于 `reduce/exit`。
- `positive_candidate_execution` 必须满足 `canonical_action_family=execution`，且 lane 为 `execution`。
- `positive_candidate_hold` 必须满足 `canonical_action_family=hold`，且 lane 为 `hold`。
- `canonical_action_family=observe` 必须满足 lane 为 `hold`；空 `action_preference` 是合法语义，不是缺字段；负向保护偏向只允许 `negative_hold_revalidate` / `negative_revalidate` / `tail_loss_protect`。
- `observe` 禁止 `positive_candidate_open` / `positive_candidate_exit` / `positive_candidate_execution` / `positive_candidate_hold`。
- 缺 `canonical_action_family`、缺 lane，或 family/lane/preference 不一致，必须 hard fail。
- `signal_collection_contract` / SCC 审计属于另一条机制问题，不由本文规则处理。
