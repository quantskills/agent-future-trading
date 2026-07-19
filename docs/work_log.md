# AgentQuant 工作日志

本日志自 2026 年 07 月 19 日起重新记录。

只记录已经完成的 `.py`、`.yaml`、`.yml` 行为修改或运行配置修改。相关修改完成后再追加记录；纯讨论、方案、排查结论、仅运行测试、纯文档修改、数据或缓存处理、文件改名或删除均不记录。

日志按日期正序分组。每项只简要说明：

- 修改了什么。
- 为什么修改。

==========2026年07月19日==========

（1）[PandaAI与技术指标语义] `analyst_market_confirmation.py` 按官方单位分别解释基差率、多空比和合约日指标；`technical.py`、`prompt.py` 将波动率、成交强度和价格位置真实传入分析师，并让布林带使用学习校准后的标准差参数。原因：消除比例误读、指标声明与实际消费不一致及学习参数不生效。

（2）[Finoview可见性与基差] `analyst_finoview_factors.py`、`finoview_factor_catalog.yaml`、`router.py` 统一使用单一catalog生成频率、freshness、正式交易日发布滞后和可见行，删除Router第二套频率判断及J重复因子；PandaAI历史日线增加显式结束日包含语义，本地基差改为同日现货期货匹配和统一现货分母。原因：防止周频/月频误判、前视、日期错位和同一基差两套解释。

（3）[基本面与新闻真实消费] fundamental提示上下文只登记并展示实际可用于方向的Finoview因子值；本地新闻在截取最新记录前按15个产品产业链过滤，并由真实匹配计算相关度。原因：防止未传递因子被登记为已使用，以及非空但无关新闻污染品种证据。

（4）[分钟行情错误边界] `trader_intraday_execution.py` 将分钟接口异常改为稳定数据故障，真实无异常空响应继续保留为`intraday_no_valid_bar`；相关确定性测试同步覆盖九项数据链语义。原因：接口故障不能伪装成合法未触发或无行情。

（5）[technical canonical触发顺序] `analyst_quality.py` 在setup完整性判断前，先按合法`entry_timing_signal + side`生成唯一canonical `entry_trigger`；相关测试覆盖三种technical profile的多空路径、缺失效边界、自由文字不创建profile及条件FAC保护。原因：防止合法technical watch仅因LLM执行文字为空就在进入SCC和PM排名前被错误清成`no_opportunity`。
