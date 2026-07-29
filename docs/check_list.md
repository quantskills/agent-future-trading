# AgentQuant 回测后待验收清单

仅保留最新回测中部分通过及未通过的项目；完整通过项已删除。

当前验收范围：2025-05-12 至 2025-06-06，共 19 个已完成交易日。285条策略建议满足每日单动作约束，46笔成交、31个FIFO平仓片段、17个完整episode已形成，系统不变量七项全部通过。

2026-07-29完成的五项代码修改尚未经过新回测；下列对应项目以修改后的真实交易、FAC、学习记录及Rank结果为验收依据，旧窗口只保留为问题基线。

## 系统与交易链

- [ ] 【未验证：缺真实样本】forced-risk必须保持独立来源，不污染策略交易语义。本轮没有forced-risk事实。
- [ ] 【未验证：待新回测】同一持仓周期内，PM正式学习检索及hold/reduce/exit FAC的`setup_type`、`horizon_class`、`expected_horizon_days`、`market_regime`必须逐项等于原开仓FAC；当天SCC只提供最新行情、确认及退出证据。换约同向续仓继续原身份，只平旧后下一次策略开仓建立新身份。
- [ ] 【未验证：待新回测】普通亏损再验证必须服从既有退出优先级：原开仓FAC失效、结构止损或ATR止损先触发时直接退出；原FAC未失效且普通持仓浮亏达到2%、同向证据再验证失败时减仓50%，浮亏达到4%时退出；同向证据通过时不得因该规则减仓或退出。

## PM、rank与资金

- [ ] 【未验证：缺真实样本】合格学习必须把候选从probe升级至real/scale，并按对应层级获得更高资金比例。99个带rank的FAC与最终资金部署字段一致，但全部仍为`exploration_probe`。
- [ ] 【未验证：缺真实样本】严格匹配的成熟正向学习必须保留real/scale晋升能力。本轮正式消费3条`exact_real_state`，尚未形成晋升样本。
- [ ] 【未验证：待新回测】PM正式读取`alpha_setup_profile`时，数据库查询、检索层、Rank及仓位/放大端必须全部精确匹配当前正式`setup_type`；持仓使用原开仓FAC setup，新开仓使用当天SCC选定的canonical setup，跨setup Profile对Rank、仓位及放大的影响必须为零。
- [ ] 【未验证：待新回测】Rank学习分必须使用完整episode聚合后的`mean_return_on_notional`形成正负学习信号，并使用`worst_return_on_notional`形成尾部信号；人民币`reward_sum/reward_mean`只保留学习生命周期与财务审计，不得影响Rank。相同收益率跨品种、手数必须得到相同学习分，较高正收益率必须获得更高正向学习分。

## 学习闭环

- [ ] 【未验证：缺真实样本】正向学习必须把后续合格交易从probe升级至real/scale并放大仓位；负向学习必须降低质量、层级并改变入场与退出处理。本轮正负学习均已进入rank，但尚未形成升层、放大及降层样本。
- [ ] 【未验证：缺真实样本】未交易fast-candidate晋升只使用固定5日窗口内同作用域完整正负样本；入场前已失效、已到期及缺FAC可执行依据的记录不得晋升；不再满足条件的既有active政策必须停用，多周期影子结果仅保留诊断用途。本轮没有形成新`fast_candidate_alpha`，也没有T+1真实消费样本。
- [ ] 【未验证：缺真实样本】`alpha_setup_profile → adaptive_policy_state`必须原值传递同一canonical `setup_type`。生产链与跨setup隔离测试已通过；本轮没有新`fast_candidate_alpha`落库及T+1实际消费FAC，仍缺真实回测样本。
- [ ] 【未验证：待新回测】每个完整`trade_episode_memory`的`setup_type`、`horizon_class`、`expected_horizon_days`、`market_regime`必须逐项继承原开仓FAC，不得由平仓日分析师证据或SCC重新推导；任一身份字段缺失时不得写入正式完整episode，后续action-value必须按同一身份被PM精确命中。

## 回测结果评价

- [ ] 【未通过】按rank、资金层级、setup、trigger、持有期和退出原因比较完整episode表现，确认高投资价值排序具有实际区分度。本轮rank 1共6个episode，平均净收益-5325.68元；rank 2共1个，平均净收益3029.85元；rank 3共4个，平均净收益-2344.80元；rank 4共3个，平均净收益-5942.30元；rank 5共3个，平均净收益-3295.62元。高rank尚未稳定获得更高收益。
