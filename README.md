# agent-future-trading

基于 LangGraph 编排、LangChain 模型适配和多智能体协作的中国期货研究、策略生成与历史回测系统。

> 本项目是 QUANTSKILLS 社区项目，尚未获得官方认证或验证，不构成投资建议，也不承诺收益。

## 项目定位

系统让技术面、基本面和期货新闻面分析师分别形成结构化预测证据，再由信号收集员交给投资组合经理完成候选比较、排序、资金部署和最终交易合约签发；回测与模拟交易复用同一套阶段链路。

当前公开代码主要支持中国期货市场，默认品种包括：

`BU`、`C`、`CF`、`EB`、`HC`、`I`、`J`、`M`、`MA`、`P`、`PB`、`RB`、`SR`、`TA`、`ZN`。

## 工作流

```text
盘前行情/基本面/新闻
        ↓
技术面、基本面、期货新闻面分析师
        ↓
Signal Collector：signal_collection_contract
        ↓
Portfolio Manager：FuturesRecommendation / final_action_contract
        ↓
Auditor：审计最终合约
        ↓
Trader：盘中执行已批准方案
        ↓
Accountant：日终结算、持仓与 PnL
        ↓
Reviewer / Researcher：事实复盘与未来日期学习反馈
```

系统按交易日划分为四个阶段：

1. **Phase1：策略生成**——生成结构化预测证据和最终交易方案；
2. **Phase2：交易执行**——按审计通过的最终合约执行；
3. **Phase3：日终结算**——根据成交和官方结算事实更新账户、持仓和 PnL；
4. **Phase4：复盘与研究**——核对物理事实，并生成只影响未来日期的研究反馈。

只有投资组合经理签发的 `final_action_contract` 可以成为交易员的执行依据。分析师文本、研究结论和排名诊断不能绕过该合约直接下单。

## 智能体职责

| 智能体 | 职责 |
| --- | --- |
| 技术面分析师 | 分析价格行为、趋势、波动率、成交量和技术指标。 |
| 基本面分析师 | 分析供需、库存、基差、仓单和产业链数据。 |
| 期货新闻面分析师 | 分析期货新闻事件的方向、强度、新鲜度和可交易性。 |
| 信号收集员 | 汇总三名分析师的结构化预测证据。 |
| 投资组合经理 | 选择合法候选、进行 Rank 排序、部署资金并签发最终合约。 |
| 审计员 | 检查账户、持仓、保证金、数据质量和合约边界。 |
| 交易员 | 只执行审计通过的最终合约和盘中触发条件。 |
| 会计师 | 记录成交、手续费、保证金、结算、持仓和 PnL。 |
| 复盘员 | 核对策略生成、审计、执行和结算事实是否完整一致。 |
| 研究员 | 将复盘事实转化为供未来交易日使用的结构化学习记录。 |
| 协议管理员 | 检查链路边界、前视风险、字段契约和系统不变量。 |

## 数据与隐私边界

运行时可以接入：

- PandaAI：期货日频、分钟级和衍生行情数据；
- Finoview：本地基本面数据；
- 本地期货新闻文件。

公开仓库不包含真实行情、基本面、新闻、数据库、日志或 API 凭据。请使用自己合法取得的数据，并将凭据写入本地 `.env`，不要提交到 Git。配置模板见 [.env.example](.env.example)。

## 环境安装

```powershell
conda env create -f environment.yml
conda activate deepfund
Copy-Item .env.example .env
```

然后在 `.env` 中填写自己的数据服务和模型服务配置。公开仓库不提供任何可用密钥或私有数据。

## 回测示例

在项目根目录执行：

```powershell
python src/run/pre_backtest_test.py --config src/config/dev.yaml --local-db --json
python src/run/backtest.py --config src/config/dev.yaml --local-db --start-date 2025-07-01 --end-date 2025-07-31
```

正式回测前应先完成数据边界、配置和无未来数据检查。没有合法的本地数据和服务配置时，命令不能直接复现历史结果。

## 开发与测试

```powershell
python -m compileall src
python -m unittest
```

核心实现位于 `src/agents/`、`src/graph/`、`src/tools/`、`src/database/` 和 `src/evaluation/`。工作流由 LangGraph `StateGraph` 编排，模型提供方适配位于 `src/llm/`。

## 当前状态与风险声明

本项目目前以研究和教育为主，Registry 初始声明为 L1 Listed / draft。历史回测结果不代表未来收益；实际结果会受到数据质量、手续费、滑点、保证金、流动性、模型服务和执行条件影响。任何策略、信号或回测输出都不构成投资建议或收益保证。

## 公开发布边界

本地 `docs/`、`data/`、数据库、日志和运行缓存由 `.gitignore` 排除，不随公开仓库上传。根目录的 `AGENTS.md` 是 QUANTSKILLS Agent 声明文件，根目录的 `LICENSE` 说明代码授权范围。

## License

本项目采用 GPL-3.0-only，详见 [LICENSE](LICENSE)。
