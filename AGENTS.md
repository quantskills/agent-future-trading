---
name: agent-future-trading
description: 基于 LangGraph 编排的多智能体期货研究与回测工作流，适用于结构化市场分析、组合决策、历史复刻、执行检查、结算和研究反馈。
quantSkills:
  schema_version: "2.0.0"
  organization: quantskills
  organization_url: https://github.com/quantskills
  repository: agent-future-trading
  repository_url: https://github.com/quantskills/agent-future-trading
  project_type: agent
  license: GPL-3.0-only
  maintainer: agent-future-trading-contributors
  collection: futures-trading
  catalog:
    category: "09"
    subcategory: 09.workflow-orchestration-agent
  workflow:
    primary_stage: orchestration
    workflow_stages:
      - data-ingestion
      - data-quality
      - modeling
      - portfolio-construction
      - backtesting
      - evaluation
      - risk
      - execution
      - reporting
      - orchestration
  tags:
    - futures
    - multi-agent
    - langgraph
    - langchain
    - backtesting
    - portfolio-construction
    - research-loop
  summary_zh: 多智能体期货研究、策略生成、历史回测与研究反馈工作流
  summary_en: Multi-agent futures research, strategy generation, backtesting, and research feedback workflow
  status: draft
  validation_level: listed
  maintainer_type: community
  platforms:
    - cursor
    - claude-code
    - codex
    - hermes
    - openclaw
  interface:
    mode: natural-language
---

# 公共项目声明

`agent-future-trading` 是 QUANTSKILLS 社区项目，尚未获得官方认证、验证、背书或生产就绪声明，也不构成投资建议。

## 项目范围

本仓库提供多智能体期货研究与回测工作流的公开源代码，面向研究、教学和使用合法取得数据的本地实验。

公共版本包含源代码、配置模板、测试和安装说明；私有凭据、本地数据库、行情文件、日志和内部研究文档不属于公共发布内容。

## 工作边界

- 三名分析师分别生成结构化的技术面、基本面和期货新闻面证据。
- 信号收集员把分析师证据整理成统一证据包，交给投资组合经理。
- 只有投资组合经理可以签发最终交易行动契约。
- 审计员检查已签发的契约，交易员只执行审计通过的行动。
- 会计师记录成交、结算、账户权益、手续费和盈亏事实。
- 复盘员与研究员在交易日结束后生成面向未来交易日的反馈。
- 历史回测与模拟盘执行使用相同的阶段边界。

研究反馈只能影响未来交易日，系统不得使用未来数据、私有凭据或研究结论改写已经发生的交易事实。

## 上游项目

- 组织：https://github.com/quantskills
- 仓库：https://github.com/quantskills/agent-future-trading
