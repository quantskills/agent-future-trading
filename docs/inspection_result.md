# AgentQuant 上游信息传递审计结果

审计日期：2026-07-13

审计范围：基于中国期货交易业务、智能体职责边界、现有生产代码、`workflow.md`、`matrix_field_semantics.md` 与 `matrix_action_canonical.md`，核对：

```text
technical / fundamental / commodity_news
-> action_evidence_contract
-> signal_collection_contract
-> Portfolio Manager Step1-6
-> FuturesRecommendation
-> Auditor / Trader / Accountant / Reviewer / Researcher 合法消费需求
```

本次只形成审计结论，没有修改生产代码、配置、测试、数据库及其他机制文档。

## 一、唯一结论

正常路径下，三个分析师已经把正式预测证据完整写入 `action_evidence_contract`，Signal Collector 也通过 `source_contracts[].action_evidence_contract` 保真保存完整 AEC，PM 只从 SCC 重建证据读取对象。上游不存在“分析师证据整体丢失”。

真实缺口集中在四处：

1. PM 已读取并实际用于方向、学习作用域及执行判断的最终 `setup_type`、`horizon_class`、`expected_horizon_days`、`market_regime` 没有收口进唯一 `final_action_contract`。
2. 分析师真实生产的 `invalidation_level`、`atr_stop_distance` 没有由 PM 按最终方向选择后写入唯一 `final_action_contract`，Trader 因而转而读取已经不存在的旧 analyst snapshot 路径。
3. `contract_code` 已由 PM 读取并写入 `FuturesRecommendation` 顶层，但没有绑定进唯一 `final_action_contract`，导致单独读取最终合约不能确认被授权执行的具体期货合约。
4. 盘前参考价不可用路径由 Signal Collector 临时生成合法中性 AEC/SCC，但对应 AnalystSignal 没有形成 signal SQL 物理来源记录，`source_contracts[].signal_record_id` 无合法生产者。

`target_return` 不属于上游缺失：当前分析师、AEC 生产工具和 PM 都不生产该事实。`target_price` 是 Trader 使用当前价格和目标收益派生的执行期事实；当前系统没有合法 `target_return` 生产者，因此二者都不得反向补进 AEC、SCC 或 PM 合约。

## 二、第一张表：必要业务事实—合法生产者—合法消费者

| 必要业务事实 | 业务用途 | 原始数据来源 | 合法生产者 | 实际生产代码 | 已登记字段及路径 | 传递载体 | 合法消费者 | 当前状态 |
|---|---|---|---|---|---|---|---|---|
| 分析师身份、方向与置信度 | 表达三个专业维度各自的预测方向和可信度 | 行情、基本面、新闻及 LLM 结构化推理 | 三个分析师；共享最终收口工具 | `graph/schema.py::AnalystSignal`；`analyst_quality.py::_sync_signal_fields_to_action_evidence_contract` | `AEC.analyst/signal/side/confidence` | AnalystSignal -> AEC -> SCC source/evidence item | Signal Collector、PM、Reviewer、Researcher | 完整传递 |
| 数据截止与防未来函数 | 证明信号只使用决策时点前信息 | 分析师运行上下文和数据源日期 | 三个分析师 | `analyst_output_finalization.py::finalize_analyst_signal`；`analyst_quality.py` | `AEC.data_cutoff/no_lookahead_status` | AEC -> SCC source | PM、Reviewer、Researcher、PG | 完整传递 |
| 数据来源、可用性、覆盖和时效明细 | 解释本次信号真实使用了什么数据，避免无数据伪造方向 | PandaAI、Finoview、新闻文件读取结果 | 三个分析师的数据使用工具 | `analyst_data_usage.py`；AEC 同步函数 | `AEC.data_usage_summary.sources.*` | AEC -> SCC source | PM 数据质量摘要、Reviewer、Researcher | 完整传递 |
| SCC 顶层数据质量摘要 | 让 PM 直接看到缺失、陈旧和不可用来源摘要 | AEC 的嵌套 `data_usage_summary.sources.*` | Signal Collector | `signal_evidence_collection.py::build_signal_collection_contract` | `SCC.data_quality_flags` | SCC | PM、Reviewer、PG | AEC存在但SCC遗漏 |
| 自然分析期限 | 保留各分析师自己的预测周期 | 分析策略及结构化输出 | 三个分析师 | `technical.py`、`fundamental.py`、`commodity_news.py` | `AEC.analyst_horizon` | AEC -> SCC source | PM、Reviewer、Researcher | 完整传递 |
| 最终决策期限类别 | 确定 PM 本次交易论点、学习检索作用域和后续归因周期 | 分析师 `horizon_class` 与最终方向 | 分析师提供原始值；PM 选择最终值 | 分析师代码；`portfolio_manager.py::_resolve_decision_horizon` | `AEC.horizon_class`；最终合约精确路径尚未登记 | AEC -> SCC -> PM state | PM、Trader执行摘要、Reviewer、Researcher | PM已读取但未形成最终事实 |
| 最终预期交易日数 | 对期限一致性、时间退出和研究收益窗口进行归因 | 分析师 `expected_horizon_days` | 三个分析师提供原始值；PM 应按最终方向选择 | 三个分析师代码；当前 PM 未收口 | `AEC.expected_horizon_days`；最终合约精确路径尚未登记 | AEC -> SCC | Trader/Reviewer/Researcher仅应读取PM选定值 | PM已读取但未形成最终事实 |
| 最终市场状态 | 绑定本次 setup 与 action-value 的市场环境，供未来同作用域学习 | 分析师 `market_regime` 与最终方向 | 分析师提供原始值；PM 选择最终值 | `portfolio_manager.py::_market_regime_from_signals` | `AEC.market_regime`；最终合约精确路径尚未登记 | AEC -> SCC -> PM Step4 | Reviewer、Researcher、PM未来学习 | PM已读取但未形成最终事实 |
| 最终 setup 类型 | 绑定本次交易论点、退出模板和研究归因 | 分析师 `setup_type/opportunity_type` 与最终方向 | 分析师提供原始值；PM 选择最终值 | `portfolio_manager.py::_setup_type_from_signals` | `AEC.setup_type`；最终合约精确路径尚未登记 | AEC -> SCC -> PM Step4 | Trader退出策略、Reviewer、Researcher | PM已读取但未形成最终事实 |
| setup、业务质量和入场质量原始证据 | 支持 PM 候选质量、方向选择和 rank 输入 | 当日专业证据、学习校对、profile | 三个分析师 | `analyst_business_quality.py`；`analyst_quality.py` | `setup_quality_ok/setup_quality_score/business_quality_score/entry_quality/setup_quality_notes` | AEC -> SCC source/evidence item -> PM evidence summary | PM、Reviewer、Researcher | 完整传递 |
| 价格位置 | 技术面证据解释及未来同类 setup 研究 | 技术行情窗口 | technical | `technical.py` | `AEC.price_percentile/price_location` | AEC -> SCC source | PM证据评分、Reviewer、Researcher | 完整传递 |
| 当前方向支持与反向证据 | PM 选择产品方向并解释冲突 | 三个分析师结构化证据 | 三个分析师；Signal Collector汇总；PM选择 | `signal_evidence_collection.py`；`pm_ticker_side_selection.py` | `SCC.dominant_side/evidence_items/evidence_fusion`；`final_action_contract.evidence_used` | AEC -> SCC -> PM final evidence | PM、Auditor只读解释、Reviewer、Researcher | 完整传递 |
| 当前触发事实 | 区分已触发、等待触发和不可交易 | 当日技术/事件证据 | 三个分析师；Signal Collector按主方向汇总；PM选择 | `analyst_quality.py`；`signal_evidence_collection.py`；`portfolio_manager.py::_build_execution_contract_fields` | `trigger_valid/current_trigger_confirmed/entry_trigger`；`final_action_contract.entry_trigger` | AEC -> SCC -> final_action_contract | Trader | 完整传递 |
| 失效文字条件 | 说明何时当前交易论点失效 | 分析师结构化失效说明 | 三个分析师；PM按最终执行来源选择 | `analyst_quality.py`；`portfolio_manager.py::_build_execution_contract_fields` | `AEC.invalidation_condition/exit_hint`；`final_action_contract.invalidation` | AEC -> SCC -> final_action_contract | Auditor、Trader、Reviewer、Researcher | 完整传递 |
| 数值失效价位 | 盘中确定性判断当前方向是否已经失效 | 分析师结构化数值边界 | 分析师提供；PM应按最终方向选择 | `AnalystSignal.invalidation_level`；当前 PM 未收口 | `AEC.invalidation_level`；最终合约精确路径尚未登记 | AEC -> SCC | Trader只能读取PM选定值 | PM已读取但未形成最终事实 |
| ATR止损距离 | 按已签约 setup 执行 ATR 风险退出 | 技术分析 ATR 与结构化输出 | technical提供；PM应按最终方向选择 | `AnalystSignal.atr_stop_distance`；当前 PM 未收口 | `AEC.atr_stop_distance`；最终合约精确路径尚未登记 | AEC -> SCC | Trader退出策略、Reviewer、Researcher | PM已读取但未形成最终事实 |
| 目标收益率 | 计算目标价格 | 当前系统没有分析师或PM生产逻辑 | 无 | 全仓只有 Trader/Researcher读取尝试，没有生产端 | `target_return`仅有通用语义登记，没有合法当前生产路径 | 无 | 无合法消费者输入 | 无生产者 |
| 目标价格 | 记录执行期派生目标 | 当前价格与合法目标收益率 | Trader运行时派生 | `trader.py::_target_return_price` | `execution_translation.signal_lifecycle.target_price` | Trader execution translation | Reviewer、Researcher | 下游运行时派生 |
| 基本面专业状态 | 支持中期方向、反向压制和数据缺口判断 | 供需、基差、库存、仓单、持仓流 | fundamental | `fundamental.py`、`analyst_quality.py` | `direction_anchor/supply_demand_state/basis_state/inventory_state/warehouse_receipt_state/position_flow_state` | AEC -> SCC source | PM、Reviewer、Researcher | 完整传递 |
| 新闻事件与有效窗口 | 解释事件催化、时效和一次性冲击 | 新闻文本和事件分类 | commodity_news | `commodity_news.py`、`evidence_fusion_semantics.py` | `event_type/impact_window_days/news_impact_window/one_off_event_risk` | AEC -> SCC source/fusion | PM、Reviewer、Researcher | 完整传递 |
| Neutral、观察和反事实事实 | 保留未交易机会及未来观察条件，不伪造开仓 | 分析师当前证据 | 三个分析师 | `AnalystSignal`与质量门 | `neutral_* / counterfactual_side / would_change_view_if / opportunity_cost_risk` | AEC -> SCC source | PM条件监控、Reviewer、Researcher | 完整传递 |
| 学习校对结果 | 说明历史经验如何确认、削弱或反驳当日证据 | 当前日前研究SQL | 三个分析师 | `analyst_learning_calibration.py`、`analyst_quality.py` | `learning_impact_summary/factor_calibration_summary/event_calibration_summary/learning_scope` | AEC -> SCC source | PM证据理解、Reviewer、Researcher | 完整传递 |
| 商品差异化profile使用痕迹 | 证明品种差异化规则实际用于分析且未创造交易权限 | product profile配置和当日证据 | 三个分析师 | `analyst_product_price_behavior_profile.py` | `AEC.product_profile_evidence` | AEC -> SCC source/evidence item | PM、Reviewer、Researcher | 完整传递 |
| 多维证据融合 | 汇总强度、时效、冲突、缺失和确认需求 | AEC专业证据 | 分析师生成单体融合；Signal Collector生成跨分析师融合；PM形成使用解释 | `evidence_fusion_semantics.py` | `AEC.fusion_evidence`、`SCC.evidence_fusion`、`final_action_contract.evidence_used.pm_fusion_diagnostics/pm_conflict_resolution` | AEC -> SCC -> PM final evidence | PM、Auditor只审解释完整性、Reviewer、Researcher | 完整传递 |
| 分析师物理来源记录ID | 追溯 SCC 中每份证据对应的 signal SQL 行 | signal保存返回ID | workflow保存层/分析师持久化入口 | `persist_analyst_signal`、`workflow._save_prefetched_analyst_outputs` | `SCC.source_contracts[].signal_record_id` | AnalystSignal metadata -> SCC | Reviewer、Researcher、PG | 完整传递 |
| 数据不可用路径来源ID | 追溯自动生成的中性证据 | 数据不可用 Signal Collector包 | 当前无合法物理生产者 | `signal_collection_data_unavailable.py`生成信号；workflow在PM返回后丢失该列表 | 已登记 `SCC.source_contracts[].signal_record_id` | SCC中为空 | Reviewer、Researcher、PG | 无生产者 |
| 最终动作与仓位变化 | 唯一决定下个交易日买卖、持有、减仓或退出 | SCC、当前持仓、学习、预算和风险 | PM Step6 | `pm_contract_builder.py::build_final_action_contract` | `final_action/current_lots/target_lots/lots_delta/target_position_ratio` | final_action_contract -> FuturesRecommendation | Auditor、Trader、Reviewer、Researcher | 完整传递 |
| 最终交易权限 | 区分观察、小仓试探、真实资金、缩放、减仓、退出和阻断 | PM当日证据与资金风险机制 | PM Step6 | `portfolio_manager.py::_final_contract_authority`及builder | `authority_type/authority_decision/requires_authority/open_action_evidence/strong_current_evidence/...` | final_action_contract | Auditor、Trader、Reviewer、Researcher | 完整传递 |
| 执行profile和盘中确认权限 | 规定Trader采用突破、回撤、事件立即、退出立即或持有方式 | PM最终方向、触发证据和execution action-value | PM Step6 | `portfolio_manager.py::_build_execution_contract_fields` | `execution_profile/trigger_source/entry_trigger/invalidation/valid_until/requires_intraday_confirmation/can_execute_without_intraday_trigger` | final_action_contract | Trader | 完整传递 |
| 具体期货合约 | 确定Trader执行哪一张期货合约，防止只按品种代码下单 | 合约缓存与当前持仓 | PM | `portfolio_manager.py::_build_pm_memory_state` | `FuturesRecommendation.contract_code`；最终合约精确路径尚未登记 | FuturesRecommendation顶层 | Trader、Reviewer、Researcher | PM已读取但未形成最终事实 |
| 计划参考价格及来源 | 为Phase2提供盘前计划基准，不是实际成交价 | Router盘前价格上下文 | PM写入Recommendation header | `_build_pm_memory_state.recommendation_context` | `FuturesRecommendation.base_price/base_price_source/base_price_date/prev_close_price` | FuturesRecommendation顶层 | Trader、Reviewer、Researcher | 完整传递 |
| 合约乘数和动态保证金率 | Trader实际成交保证金及Accountant结算 | 合约缓存、PandaAI实际合约数据、成交 | Trader和Accountant按阶段独立读取 | `trader_futures_execution.py`、`accountant_futures_settlement.py` | transaction/position/settlement字段 | 运行时行情、交易、持仓和结算记录 | Trader、Accountant | 下游独立读取 |
| PM rank、预算和sizing事实 | 解释资金优先级、资金层级、是否部署及目标手数 | PM Step5全市场候选池、账户和配置 | PM | `pm_full_market_capital_deployment.py`、`pm_contract_builder.py` | `capital_deployment`、`evidence_used.position_sizing_result` | final_action_contract | PM自检、Auditor、Reviewer、Researcher | 完整传递 |
| PM学习消费事实 | 证明历史学习只影响合法生命周期并未直接下单 | Research DB经Step4检索 | PM | `pm_decision_memory_retrieval.py`、`pm_contract_builder.py` | `learning_used`、`lifecycle_learning_trace` | final_action_contract | PM自检、Reviewer、Researcher、PG | 完整传递 |
| 完整AEC在final_action_contract内再次复制 | 当前代码试图供Trader读取分析师执行角色 | SCC已经完整保存的AEC | PM重复复制 | `portfolio_manager.py::_execution_signal_payloads` | `final_action_contract.analyst_execution_roles.*.action_evidence_contract` | final_action_contract | 不应成为任何下游的第二证据入口 | 重复传递 |
| product profile与fusion在SCC source中重复 | 当前validator要求副本与AEC完全相等 | AEC内部同名对象 | Signal Collector重复复制 | `signal_evidence_collection.py::build_signal_collection_contract` | `source_contracts[].action_evidence_contract.*`及同级`product_profile_evidence/fusion_evidence` | SCC | 下游只需唯一AEC和跨分析师摘要 | 重复传递 |
| rank字段在`evidence_used`和`capital_deployment`双写 | 当前用于解释和部署的两套路径 | Step5同一rank结果 | PM重复写入 | `_sign_pm_memory_state` | 两个容器中的`opportunity_rank/rank_source/...` | final_action_contract | Reviewer、Researcher、PG | 重复传递 |
| `recommendation_intent`与`action_candidates` | 保存最终动作映射及未成为最终事实的候选 | PM内部候选状态 | PM builder | `pm_contract_builder.py` | final_action_contract同名字段 | final_action_contract | 不应成为Trader交易依据 | 重复传递 |
| Auditor所需账户与硬限制 | 对最终合约做独立硬风险审计 | 当前portfolio和配置 | workflow在审计时传入 | `workflow._audit_phase1_strategy_recommendations` | 不属于AEC/SCC/PM合约字段 | AuditorInput运行时对象 | Auditor | 下游独立读取 |
| Trader盘中行情、涨跌停、到期、动态保证金和账户状态 | 执行已审计合约并检查实际市场可执行性 | PandaAI、当前portfolio、主配置、合约缓存 | Trader运行时工具 | `trader_futures_execution.py` | execution/transaction字段 | Trader运行时输入 | Trader | 下游独立读取 |
| 成交、结算价、手续费和官方持仓账务 | 形成真实盈亏、保证金和账户权益 | transaction、结算行情、手续费规则 | Trader、Accountant | execution/settlement工具 | transaction/portfolio/position/daily_settlement | SQL事实链 | Accountant、Reviewer、Researcher | 下游独立读取 |
| 审计结果、执行结果和结算结果 | 供复盘、研究及下一交易日学习 | Auditor、Trader、Accountant实际运行 | 各合法下游 | 对应agent/tool | `audit_payload/execution_result/daily_settlement` | FuturesRecommendation追加及SQL事实 | Reviewer、Researcher | 下游运行时派生 |

## 三、第二张表：核心载体边界

| 载体 | 必须包含的信息 | 禁止包含的信息 | 当前缺失 | 当前重复 |
|---|---|---|---|---|
| `action_evidence_contract` | 分析师身份；signal/side/confidence；数据截止与防前视；自然期限；market regime；setup/opportunity；触发与失效；技术/基本面/新闻专业事实；质量、冲突、缺失、确认；neutral/反事实；学习校对；profile；fusion；data usage | 最终动作、当前/目标手数、仓位比例、保证金授权、rank、预算部署、`final_action_contract`、Auditor/Trader/Accountant事实 | 无全局字段缺失；数值失效位与ATR允许按真实数据为空，不得伪造；`target_return`无生产者，不得增加 | AnalystSignal metadata仍保存部分AEC同义上下文，但唯一正式证据已由AEC校验收口 |
| `signal_collection_contract` | version/source/ticker/date/boundary；完整`source_contracts[].action_evidence_contract`及`signal_record_id`；逐分析师evidence item；主方向、共识、触发；支持/反对/中性分析师；强度、冲突、确认、缺失、数据质量；setup、horizon、失效摘要和跨分析师fusion | PM方向优先级、机会分、rank、手数、仓位、资金部署、最终动作、最终合约、PM trace | `data_quality_flags`没有从真实嵌套source质量事实完整生成；数据不可用路径缺`signal_record_id`物理来源 | `source_contracts`同级重复`product_profile_evidence/fusion_evidence`；`evidence_items[].fusion_evidence`再次复制单体fusion |
| `final_action_contract` | 合约版本；ticker和具体contract_code；final action及current/target/delta；目标仓位和保证金摘要；最终权限；PM最终选择的setup/horizon/expected days/market regime；执行profile、trigger、文字失效、数值失效、ATR止损、有效期和盘中确认权限；reason codes；PM证据使用摘要；学习消费摘要；capital deployment；position sizing；一致性和SCC引用摘要 | 完整AEC副本；原始SCC副本；分析师自由文本；PM Step1-5状态；candidate/builder/rank草稿；第二套动作或手数计划；审计、执行、结算结果；无生产者`target_return`；Trader派生`target_price` | `contract_code`；最终选定`setup_type/horizon_class/expected_horizon_days/market_regime`；最终方向对应`invalidation_level/atr_stop_distance` | `analyst_execution_roles.*.action_evidence_contract`；`action_candidates`；`recommendation_intent`；rank/deployment字段在`evidence_used`和`capital_deployment`双写；`risk_flags`与`reason_codes`同源 |
| `FuturesRecommendation` | schema顶层header、产品/具体合约、创建与生效日期、source type、顶层action/lots投影、计划价格、状态；PM返回时snapshot只含原始SCC、唯一final contract、Step6 trace；后续只由Auditor/Trader在各自阶段追加审计和执行事实 | PM中间状态；独立候选合约；未签约recommendation；分析师顶层旧snapshot；workflow生成的交易语义；Accountant结算事实副本 | PM嵌套final contract存在上述执行/作用域缺口；数据不可用SCC来源ID缺口 | 顶层action/lots是最终合约必要投影，不算第二交易计划；禁止再增加第三套动作/手数对象 |

## 四、第三张表：下游合法需求与读取路径

| 下游智能体 | 完成职责真正需要的上游事实 | 不应由上游传递的事实 | 当前读取路径是否正确 |
|---|---|---|---|
| Auditor | `FuturesRecommendation`身份；唯一`final_action_contract`的动作、手数、权限、目标保证金摘要、失效边界、reason codes；审计时的当前账户和硬风险配置 | 原始AEC全文、研究DB、PM内部候选、重新计算方向/rank/预算/sizing、Trader执行期行情 | final contract读取正确；`account_state/position_state/contract_state`虽已声明，但当前Auditor几乎未消费，属于下游实现缺口，不是要求PM复制账户/持仓/合约缓存 |
| Trader | 已审计Recommendation；具体contract_code；final action/current/target/delta；最终权限；执行profile/trigger/失效/有效期/盘中确认；PM选定setup/horizon/regime及数值失效/ATR；当前账户持仓、盘中行情、执行配置和合约缓存 | SCC原始冲突证据、完整AEC、学习记录、rank、资金解释、PM scorecard、研究DB；无生产者target_return | 动作、手数、权限和执行profile读取正确；`extract_signal_lifecycle`与方向过滤仍读取旧`snapshot.technical/fundamental/commodity_news`，生命周期路径错误 |
| Accountant | 当日未入账transaction、最近已结算portfolio/position、官方结算价、手续费、实际contract multiplier和margin rate | AEC、SCC、PM学习、rank、预算、审计解释、Trader触发文本 | 正确；Accountant不需要FuturesRecommendation作为账务输入，上游不应复制结算参数给它 |
| Reviewer | 已保存SCC及AEC来源链、唯一final contract、Auditor结果、Trader执行结果、transaction、settlement、portfolio/position和phase状态 | PM中间状态、第二次审计结论、研究员未来action-value | final contract与SQL事实读取基本正确；分析师组合、期限、regime、setup及AEC提取仍大量读取旧顶层analyst snapshot，路径错误 |
| Researcher | Phase4完成事实；SCC/AEC来源；final contract中的最终setup/horizon/regime、动作、手数、rank/预算/学习使用；审计、执行、成交、结算和PnL | 当日交易权限、PM内部状态、未完成Phase4事实、未来数据；不应要求PM携带Trader/Accountant运行时事实 | final contract、execution和SQL读取存在；AEC、signal combo、horizon、regime、setup仍依赖旧顶层analyst snapshot，路径错误 |

## 五、唯一上游缺失清单

### 5.1 PM最终合约缺失

以下字段已经有真实上游生产者，且PM已有合法选择上下文；它们属于唯一合约应收口的事实，不是新业务字段：

```text
final_action_contract.contract_code
final_action_contract.setup_type
final_action_contract.horizon_class
final_action_contract.expected_horizon_days
final_action_contract.market_regime
final_action_contract.invalidation_level
final_action_contract.atr_stop_distance
```

固定边界：

- `setup_type/horizon_class/expected_horizon_days/market_regime` 必须是PM针对最终方向、最终动作和最终学习作用域选择后的值，不得简单取“第一个分析师”。
- `invalidation_level/atr_stop_distance` 只有在上游真实生产且方向匹配时才写入；缺失时保持缺失，不补默认值。
- `contract_code` 来自PM已经使用的合约信息/持仓事实，用于把唯一动作与具体期货合约绑定。
- `target_return` 不进入该清单，因为系统没有合法生产者。
- `target_price` 不进入该清单，因为它属于Trader运行时派生事实。

### 5.2 SCC摘要缺失

`signal_collection_contract.data_quality_flags` 当前只读取 `data_usage_summary` 顶层不存在的 `data_quality_flags/risk_flags/missing_data/stale_data`，没有从真实 `sources.*.available/stale_ratio/stale_indicator_count/supports_trade_setup` 形成完整摘要。完整原始数据仍在AEC中，因此这是SCC摘要缺失，不是原始证据丢失。

### 5.3 数据不可用路径来源缺失

`build_data_unavailable_signal_package` 生成中性AnalystSignal和SCC后，`portfolio_agent_futures`只返回`pm_state`。workflow随后对PM返回对象调用`_save_prefetched_analyst_outputs`，已经取不到这些AnalystSignal，导致没有signal SQL记录和`signal_record_id`。

## 六、唯一重复传递清单

1. `final_action_contract.analyst_execution_roles.*.action_evidence_contract`复制完整AEC；原始AEC已由`signal_snapshot.signal_collection_contract.source_contracts[]`唯一保真保存。
2. `SCC.source_contracts[].product_profile_evidence`与同一记录AEC内部对象重复。
3. `SCC.source_contracts[].fusion_evidence`与同一记录AEC内部对象重复；`evidence_items[].fusion_evidence`又复制一次。
4. rank、rank来源、资金层级、生命周期学习trace等同时写入`evidence_used`和`capital_deployment`。
5. `recommendation_intent`与最终`final_action/current_lots/target_lots/lots_delta`重复表达动作和手数。
6. `action_candidates`保存未成为最终交易事实的候选，容易形成第二套交易计划解释。
7. `risk_flags`直接复制`reason_codes`，没有独立生产语义。

## 七、唯一下游旧读取路径清单

1. `src/util/futures_audit.py::_first_signal_value`读取`snapshot.technical/fundamental/commodity_news`。
2. `src/agents/execution_team/trader.py::_align_signal_lifecycle_to_target`读取同一旧路径选择失效价。
3. `src/tools/agent_tools/research/research_review_helpers.py::_signal_combo_from_snapshot`、`_first_analyst_field`、`_analyst_payloads`读取同一旧路径。
4. Researcher据`_analyst_payloads`再查`payload.metadata.action_evidence_contract`，新PM snapshot下得到空AEC。
5. Reviewer报告和归因中的analyst signal matrix仍依赖旧顶层analyst snapshot；真实完整证据已经迁至`signal_snapshot.signal_collection_contract.source_contracts[]`。
6. `extract_signal_lifecycle`尝试读取`target_return/expected_return/target_return_ratio/expected_return_ratio`，但当前生产链没有任何合法生产者。

## 八、唯一越权、重复审计和无关需求清单

1. Auditor重新审PM是否正确消费memory和融合证据，超出“独立检查最终合约和硬风险”的最小边界，并与PM最终合约自身检查重叠；这不能成为要求PM复制更多学习对象的理由。
2. Trader不能通过原始AEC冲突证据自行选择方向、setup、失效边界或目标手数；这些必须由PM收口。
3. Trader当前exit policy能够基于旧analyst lifecycle把目标手数改为零。合法业务应是执行PM签约的数值失效/ATR规则，而不是Trader从原始分析证据创造新的退出策略。
4. Accountant不需要AEC、SCC、final contract学习、rank、预算和审计解释；它只读取成交、持仓、结算行情、手续费、乘数和实际保证金率。
5. Reviewer不能把计划预算参数与真实成交后账户比例的偏离当成第二次审计或hard fail。
6. Researcher不能要求PM预先生产execution result、transaction、settlement或PnL；这些只在对应阶段真实发生后产生。
7. `base_price/prev_close_price/contract_multiplier/margin_rate/risk_controls/capital_controls`不应仅因Auditor或Accountant方便而全部复制进final contract。计划价格已在Recommendation header；乘数、实际保证金和结算数据由Trader/Accountant合法独立读取；PM只需保留目标保证金、资金部署、sizing和reason摘要。

## 九、定死后的核心载体必传信息目录

### 9.1 `action_evidence_contract`

AEC继续服从当前共享validator的真实字段集合，按业务分组如下：

```text
身份与方向：
contract_version, analyst, sector, signal, side, confidence

时间与数据边界：
data_cutoff, no_lookahead_status, data_freshness, data_usage_summary

期限与市场状态：
horizon_class, analyst_horizon, expected_horizon_days, market_regime, trend_stage

setup与机会：
setup_type, setup_quality_ok, setup_quality_score, entry_quality,
setup_quality_notes, opportunity_type, opportunity_state, tradeability_reason,
reward_risk_ratio, business_quality_score, factor_alignment_score,
data_coverage_score, add_allowed

触发与失效：
entry_trigger, entry_timing_signal, trigger_valid,
current_trigger_confirmed, invalidation_present, invalidation_condition,
invalidation_level, atr_stop_distance, exit_hint, holding_period_hint,
price_percentile, price_location

专业方向证据：
evidence_role, direction_context, trend_direction, direction_anchor,
supply_demand_state, basis_state, inventory_state, warehouse_receipt_state,
position_flow_state, requires_fundamental_confirmation,
event_type, impact_window_days, factor_focus,
primary_business_driver, secondary_confirmation, counter_evidence

证据质量与融合：
evidence_quality, evidence_strength, evidence_freshness,
evidence_decay_risk, confirmation_requirements,
technical_false_breakout_risk, fundamental_opposition_strength,
news_impact_window, one_off_event_risk,
current_evidence_conflict, missing_evidence, conflicting_factors,
fusion_evidence

中性与反事实：
neutral_reason, would_change_view_if, opportunity_cost_risk,
recommended_observation_window, neutral_opportunity_bucket,
neutral_trigger_condition, counterfactual_side,
neutral_watchlist_priority, accountability_tag, do_not_trade_reason

学习与产品差异化：
learning_impact_summary, factor_calibration_summary,
event_calibration_summary, learning_scope, product_profile_evidence
```

AEC不增加`target_return`，不保存任何PM、Auditor、Trader、Accountant字段。

### 9.2 `signal_collection_contract`

```text
contract_version
source_agent
ticker
trading_date
source_contracts[]
  analyst
  signal_record_id
  action_evidence_contract
evidence_items[]
  analyst
  side
  confidence
  signal
  opportunity_state
  trigger_valid
  current_trigger_confirmed
  trigger_status
  entry_trigger
  setup_type
  setup_quality_ok
  horizon_class
  market_regime
  evidence_quality
  current_evidence_conflict
  missing_evidence
  evidence_strength
  evidence_freshness
  confirmation_requirements
  product_profile_id
  product_profile_used
  product_profile_analysis_boundary
dominant_side
side_consensus
trigger_status
supporting_analysts
opposing_analysts
neutral_analysts
evidence_strength
evidence_conflict_level
confirmation_requirements
missing_evidence
data_quality_flags
setup_types
horizon_scope
invalidation_summary
evidence_fusion
collector_decision_boundary
```

定死原则：完整AEC只在`source_contracts[].action_evidence_contract`保存一次；单体profile/fusion不再需要在同一source记录复制第二份。`evidence_items`和顶层fusion只保存Signal Collector实际生成的必要索引与汇总。

### 9.3 `final_action_contract`

```text
身份与具体合约：
contract_version, ticker, contract_code

唯一动作与仓位：
final_action, current_lots, target_lots, lots_delta, lots_delta_abs,
target_position_ratio, target_margin_ratio_estimate

最终交易论点作用域：
setup_type, horizon_class, expected_horizon_days, market_regime

最终权限：
authority_type, authority_decision, requires_authority,
open_action_evidence, strong_current_evidence, watch_for_trigger_block,
conditional_trigger_authority, negative_profile, tradeable_state,
weak_conflict_probe, max_allowed_margin_ratio

最终执行规则：
execution_profile, trigger_source, entry_trigger, invalidation,
invalidation_level, atr_stop_distance, valid_until,
requires_intraday_confirmation, can_execute_without_intraday_trigger,
execution_action_value_preference, execution_requirement

最终解释与控制：
reason_codes, evidence_used, learning_used, capital_deployment,
consistency, signal_collection_contract_ref,
single_source_of_trade_truth, candidate_sources_do_not_bypass_contract
```

`evidence_used.position_sizing_result`继续作为唯一sizing落点。rank与资金部署的唯一详细落点应收口到`capital_deployment`，`evidence_used`只保留非重复的PM证据使用摘要。

final contract禁止保存：

```text
analyst_execution_roles.*.action_evidence_contract
action_candidates
recommendation_intent
PM Step1-5状态和草稿
target_return
target_price
Auditor/Trader/Accountant结果
```

### 9.4 `FuturesRecommendation`

顶层继续严格使用`graph/schema.py::FuturesRecommendation`：

```text
id
config_id
reference_portfolio_id
trading_date
effective_trade_date
source_type
underlying_code
from_contract
to_contract
contract_code
action
lots
base_price
base_price_source
base_price_date
open_price
prev_close_price
slippage_model
slippage_ticks
slippage_amount
execution_price
justification
signal_snapshot
audit_payload
warning_message
status
created_at
```

PM Step6初始snapshot固定为：

```text
signal_snapshot.signal_collection_contract
signal_snapshot.final_action_contract
signal_snapshot.pm_six_step_trace.step6_contract_generation_check
signal_snapshot.pm_six_step_trace.pm_contract_self_check
```

Auditor和Trader只能在PM返回后按阶段追加自己的审计与执行事实。PM不得预填`auditor/phase2_execution/execution_translation/execution_result`。Accountant的结算事实留在transaction、portfolio、position和settlement SQL，不复制进Recommendation。

## 十、需要登记但尚未登记的字段路径缺口

以下字段名已经在`matrix_field_semantics.md`其他位置登记，不新建字段名；需要在修改代码前补充其`final_action_contract`精确路径和PM最终选择语义：

```text
final_action_contract.contract_code
final_action_contract.setup_type
final_action_contract.horizon_class
final_action_contract.expected_horizon_days
final_action_contract.market_regime
final_action_contract.invalidation_level
final_action_contract.atr_stop_distance
```

`target_return`和`target_price`不登记为PM/AEC/SCC路径。

## 十一、下一步唯一文档修改建议

先只修改`workflow.md`与`matrix_field_semantics.md`：

1. 在PM Step6载体目录中登记上述七个最终合约路径、真实生产者、选择规则和合法消费者。
2. 删除`final_action_contract.analyst_execution_roles.*.action_evidence_contract`作为必传结构，明确完整AEC只从SCC追溯。
3. 把rank详细事实收口为`capital_deployment`唯一详细路径，删除双落点口径。
4. 从上游必传目录删除无生产者`target_return`；保留`target_price`为Trader运行时派生字段。
5. 明确数据不可用路径也必须先形成signal SQL来源记录，再允许SCC引用`signal_record_id`。
6. 明确Reviewer和Researcher从`signal_snapshot.signal_collection_contract.source_contracts[]`读取AEC，不再读取旧顶层analyst snapshot。

该文档口径确认后，下一轮才允许同步生产代码和测试。
