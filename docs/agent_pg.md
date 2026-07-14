# Agent PG

## 一、协议与机制

1. 运行就绪机制（回测前检测项）
2. 配置装配机制（回测前检测项）
3. 字段与动作统一机制（回测前检测项、每日回测后检测项）
4. 分析师信号协议 `agentquant.signal.v2`（回测前检测项、每日回测后检测项）
5. 分析师证据协议 `agentquant.action_evidence.v1`（回测前检测项、每日回测后检测项）
6. 分析师内部消息协议 `agentquant.message.v1`（回测前检测项）
7. 分析师研究证据协议 `agentquant.research.v1`（回测前检测项）
8. 商品画像协议 `agentquant.analyst_product_price_behavior_profile.v1`（回测前检测项）
9. 证据融合协议 `agentquant.evidence_fusion.v1`（回测前检测项）
10. SCC 协议 `agentquant.signal_collection.v1`（回测前检测项、每日回测后检测项）
11. 数据与时间边界机制（回测前检测项、每日回测后检测项）
12. PM 方向优先级语义 `agentquant.ticker_side_priority.v1`（回测前检测项）
13. PM 最终交易合约协议 `agentquant.final_action.v1`（回测前检测项、每日回测后检测项）
14. PM 生命周期学习协议 `agentquant.pm_lifecycle_learning_trace.v1`（回测前检测项）
15. PM 资金排名语义 `agentquant.capital_priority_rank.v1`（回测前检测项）
16. Auditor 审计协议 `agentquant.audit_verdict.v1`（回测前检测项、每日回测后检测项）
17. Trader 执行字段协议 `agentquant.execution_contract_fields.v1`（回测前检测项、每日回测后检测项）
18. Trader 执行结果机制（回测前检测项、每日回测后检测项）
19. Accountant 结算机制（回测前检测项、每日回测后检测项）
20. Reviewer Phase4 复盘机制（回测前检测项、每日回测后检测项）
21. Researcher 学习协议 `agentquant.research_action_value.v1`（回测前检测项、每日回测后检测项）
22. 分析师校准协议 `agentquant.analysis_signal_calibration.v1`（回测前检测项）
23. workflow 阶段编排机制（回测前检测项、每日回测后检测项）
24. 数据库 Schema 机制（回测前检测项）
25. 数据库持久化机制（回测前检测项、每日回测后检测项）
26. Artifact 协议 `agentquant.artifact.v1` 与 `agentquant.signal_artifact.v1`（回测前检测项、每日回测后检测项）
27. PG 治理协议 `agentquant.protocol_governor.v1`（回测前检测项）

固定字段与动作边界：

- PG 的检测输入、字段路径、判定依据和报告输出只能使用 `matrix_field_semantics.md` 已登记字段及其固定语义。
- PG 对交易动作和 action-value 的解释只能使用 `matrix_action_canonical.md` 已登记动作、family、lane 和 preference 语义。
- `metadata`、`payload`、`payload_json`、`artifact_json`、`signal_snapshot` 及其他 JSON 容器不能承载未登记字段、别名、私有状态或第二套语义。
- PG 不得因为审计实现方便而自创字段、错误字段别名、动作集合、reason code 语义或兼容路径。
- 现有字段不能表达确属 PG 必需的非策略检测事实时，必须先证明真实功能缺口、唯一生产者和唯一消费者，再先登记到字段矩阵；未经登记不得进入代码、报告、artifact 或数据库。
- 当前代码中已经存在但字段矩阵未登记的 PG 报告字段，不构成正式协议；后续改造时无必要者删除，确有必要者按上述顺序登记后使用。

PG 回测前报告和每日回测后报告统一使用同一结构：

```text
protocol_governor_report
→ contract_version
→ source_agent
→ status
→ checks[]
   → check_name
   → status
   → violation_codes
   → diagnostic_codes
```

PG 报告不得输出 `ok`、`errors`、`warnings`、`metadata`、内部计数、能力卡或智能体内部检查结果。

## 二、回测前检测

检测目的：在不调用 LLM、不运行真实回测的前提下，尽可能提前发现系统运行断点和违反既定业务逻辑的问题。该检测属于事前运行就绪检查，不评价策略收益和信号质量，也不以历史错误复现作为主要检测方式。

运行机制：既可通过独立入口 `src/run/pre_backtest_test.py` 单独执行，也由自动化回测入口 `src/run/backtest.py` 在整段回测启动前统一调用。每次完整回测只执行一次，不在每个交易日开始前重复执行。

### 1. 环境与入口

- deepfund Python 存在且可运行。
- 项目依赖可导入，生产代码可编译。
- `backtest.py`、Phase1–4、Researcher 及 PG 入口可加载。
- 配置、catalog、数据目录和环境变量存在。
- 只检查 LLM 配置和密钥环境变量，不调用 LLM。

### 2. 配置与参数映射

- `dev.yaml` 及全部 catalog 可以正常展开。
- 每个配置参数都有现有 Python 消费者。
- 参数类型、上下限关系和必填项合法。
- 不锁死模型名称和可调策略参数具体数值。
- PG 配置不能获得交易权限。

### 3. 字段、动作与职责统一

- 字段及路径服从 `matrix_field_semantics.md`。
- 动作及 action-value 服从 `matrix_action_canonical.md`。
- 禁止旧字段、别名和第二套语义。
- PG 自身回测前报告也服从相同字段规则，不得以通用容器或临时字典键绕过登记。
- 各智能体只调用现有职责范围内的工具。
- 启用运行链中的 LLM 调用位置只能出现在现有分析师和 Researcher 链路。

### 4. 数据就绪

硬检查：

- 指定区间交易日能够解析。
- PandaAI 日线具备开盘价、收盘价、结算价和主力合约信息。
- 合约乘数、保证金率和具体合约信息可读取。
- Trader 使用的分钟行情接口及字段结构存在。
- 数据日期不得产生前视。

基本面与新闻不做每日齐全检查：

- Finoview 和新闻读取路径、解析函数与日期过滤机制可运行。
- 某品种某日没有基本面或新闻属于正常情况，不阻断回测。
- 数据缺失时，现有分析师必须生成合法中性 `action_evidence_contract`，不得报错、伪造方向或获得交易权限。

### 5. 时间边界

- 盘前分析不能读取当日收盘后数据。
- 夜盘时间正确映射交易日。
- Trader 只使用执行时点已经出现的分钟行情。
- Accountant 只能在 Phase3 读取结算价。
- 分析师和 PM 只能读取早于当前交易日的学习成果。
- Researcher 只能处理 Phase4 完成后的已结算事实。

### 6. 正式临时数据库

- 使用现有 `sqlite_setup` 创建隔离临时数据库。
- 验证真实 schema、索引和 DB helper 读写。
- 验证 signal、recommendation、审计、执行、成交、结算、Phase4 和研究学习的真实落点。
- 验证 artifact 外置、哈希和回读。
- 不再使用手写假 schema，不写正式交易库。

### 7. 无 LLM 全链路预演

通过确定性结构化替身替代 LLM 和外部行情响应，调用现有生产函数贯通：

```text
AnalystSignal/action_evidence_contract
→ signal_collection_contract
→ PM Step1–6
→ FuturesRecommendation/final_action_contract
→ Auditor
→ Trader
→ Accountant
→ Reviewer
→ Researcher 确定性校验和学习写入
→ 下一交易日分析师与 PM 学习读取
```

预演只在临时库运行，不属于真实回测。

### 8. 现有业务路径

覆盖现有代码已经支持的：

- long、short、mixed 及数据不可用。
- wait、hold、open、open_probe、open_real。
- add、scale、预算拒绝。
- reduce、exit、条件监控与触发。
- 反转先退出旧仓。
- strategy、rollover、forced_risk 分账。
- 成交、未触发、无成交。
- 多空开平、手续费、滑点、保证金、逐日盯市和 PnL。

### 9. 编排、状态与物理边界

- Phase1 → Phase2 → Phase3 → Phase4 → Research 顺序正确。
- PM Step1–5 只更新 `pm_state`，不落库、不输出 artifact。
- Step6 只生成唯一 `FuturesRecommendation` 和 `final_action_contract`。
- Auditor 不修改合约；Trader 不重做策略；Reviewer 不二次审计；Researcher 不改历史事实。
- 阶段失败立即停止，不留下 completed 状态或半成品。
- 重复运行、已完成阶段跳过、并行与串行 Phase1 结果边界一致。
- 回测前检测必须发生在 reset 和任何正式业务写入之前。

### 10. 判定边界

以下情况阻止回测：

- 环境、入口、schema 或数据硬事实断裂。
- 字段或动作语义漂移。
- 前视。
- 智能体越权。
- 唯一合约断裂。
- 执行不来自合约。
- 账务公式或权益守恒错误。
- 阶段、物理落点或学习回流断链。

以下内容不属于回测前检测：

- 收益、胜率、回撤和策略优劣。
- LLM 回答质量及真实 API 调用。
- 基本面和新闻每日是否齐全。
- 某个历史错误是否再次发生。
- 每日真实回测产物是否正确；这属于每日回测后检测。
- 现有代码尚未实现的业务功能。

## 三、每日回测后检测

检测目的：通过只读检查每日回测形成的物理结果，判断当日系统是否出现非策略问题。该检测不检查智能体内部机制，不重复 PM 自检和 Auditor 审计，不评价收益、信号、复盘或学习质量。真实成交和行情造成账户保证金比例、净敞口或多空比例超过 PM 规划预算门槛，不直接判定为系统错误。

每日检测只读取字段矩阵已经登记的物理字段，并只按动作矩阵解释动作语义；每日 PG 报告不得新增未登记字段、别名、私有诊断对象或第二套动作解释。

运行机制：既可通过独立入口 `src/run/backtest_daily_test.py` 单独执行，也由自动化回测入口 `src/run/backtest.py` 在每个交易日的 Phase1–4 与 Researcher 全部结束后调用。它只在单日业务链结束后运行，不进入 Phase1–4、Researcher 或任何智能体的内部运行阶段。

### 1. 当日阶段完成性

Phase1–4 及 Researcher 运行顺序完整，无失败、跳阶段或跨日残留。

### 2. 物理结果落地完整性

仅对当日实际进入对应业务路径的结果检查落地：进入分析与决策路径时检查 signal 和 recommendation；进入盘中监控时检查盘中决策；实际成交时检查成交记录；完成 Phase3、Phase4 和 Researcher 时检查各自物理结果。wait、hold、审计阻断及其他未进入盘中监控的合法路径，不强制生成盘中决策或成交记录。

### 3. 唯一交易事实来源

strategy 成交只能关联已审计的唯一 `FuturesRecommendation` 及其 `final_action_contract`，不得来自第二张合约、旧字段或旁路交易意图。rollover 和 forced_risk 成交分别检查现有合法 `source_type` 链路，不要求具有 PM 策略合约。

### 4. 审计放行与执行结果一致性

strategy 路径中，Auditor 拒绝不得成交；审计通过不等于必须成交。未触发、失效、市场规则阻断时允许无交易，但实际进入执行路径后必须形成合法执行结果和原因。rollover 和 forced_risk 按各自现有合法来源与执行边界检查，不套用 PM 策略合约的审计条件。

### 5. 执行与成交事实一致性

已触发并成交时，动作、合约、方向、手数和成交记录一致；未触发或决定不交易时应无成交记录。允许部分成交，但结果必须如实记录。

### 6. 结算与账户事实一致性

成交只入账一次；成交、持仓、手续费、保证金、每日盈亏和账户权益之间可核对。真实行情导致保证金比例、多空净敞口超过预算规划值不判错，只检查账务事实是否正确。

### 7. 学习记录落地边界

不复查 Reviewer 的复盘结论；不要求每笔交易生成学习记录，也不要求每笔交易使用过学习。只对实际生成的学习记录检查来源日期合法、来自已完成事实、无前视且未改写当日交易事实。
