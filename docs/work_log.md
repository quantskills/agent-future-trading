# AgentQuant 工作日志

本日志自 2026 年 07 月 27 日起重新记录。

只记录已经完成的 `.py`、`.yaml`、`.yml` 行为修改或运行配置修改。相关修改完成后再追加记录；纯讨论、方案、排查结论、仅运行测试、纯文档修改、数据或缓存处理、文件改名或删除均不记录。

日志按日期正序分组。每项只简要说明：

- 修改了什么。
- 为什么修改。

==========2026年07月27日==========

（1）[PM换约持仓血缘重放] `portfolio_manager.py`不再跳过换约成交，按日内换约平仓腿优先顺序重放策略持仓血缘；同一换约推荐的平旧开新继续继承原开仓FAC，新增同向手数沿用该FAC，换约只平旧则清空旧血缘，下一次策略开仓建立新FAC周期；血缘无法闭合时明确终止。`test_phase_flow_regression.py`覆盖同时间戳换约腿、同向增仓、只平旧及次日新开仓。原因：修复换约只平旧后PM仍把后续策略开仓接到旧FAC，导致持仓失效条件和持有天数错误的问题。

（2）[交易学习setup身份单源] `research_review_helpers.py`删除按分析师顺序、方向、周期及信号组合重建setup的旧函数，正式学习路径统一读取原始FAC的canonical `setup_type`，缺失及通配身份拒绝进入正式学习；`research_memory_writers.py`、`research_learning.py`、`research_snapshot_reports.py`同步覆盖模板绩效、持仓反馈、亏损政策、fast-loss、完整episode、alpha profile、规则校准及研究报告，`portfolio_manager.py`的当日政策检索改用已验证SCC中的canonical setup，`alpha_setup.py`删除失去生产用途的setup推断函数。`test_reviewer_learning.py`覆盖分析师标签与FAC不一致时fast-loss仍使用FAC身份。原因：修复亏损保护及同类学习写入旧复合setup后无法被PM精确检索的问题，并阻止字段统一后再次产生第二套setup键。

==========2026年07月28日==========

（1）[setup身份单源三处遗漏收口] `research_learning.py`把`alpha_setup_profile`的canonical `setup_type`原值写入`adaptive_policy_state`，身份缺失及通配时仅跳过该政策；日常hold/reduce/exit学习按成交血缘重放原开仓FAC，同向换约续仓继续继承原setup，当日FAC只保留在证据中；execution样本取消`execution_*_setup`第二套身份，改用对应FAC setup并直接继承Trader的`execution_retrieval_key`区分执行方式。`research_memory_writers.py`删除alpha政策落库时写死的通配setup，`portfolio_manager.py`从Trader执行检索键读取execution profile。回归覆盖政策值传递与跨setup隔离、换约后持仓setup继承、execution FAC setup及精确执行键。原因：完成既有setup单源修改遗漏的三条生产链，不改变换约决策、资金、rank、止损、fallback及升层阈值。
