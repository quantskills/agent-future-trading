---
name: agent-future-trading
description: A LangGraph-orchestrated multi-agent futures research and backtesting workflow. Use when you need structured market analysis, portfolio decisions, historical replay, execution checks, settlement, and research feedback.
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

# Public project declaration

`agent-future-trading` is a QUANTSKILLS community project. It is not an
official, certified, verified, endorsed, or production-ready QUANTSKILLS
project, and it is not investment advice.

## Scope

This repository contains the public source for a multi-agent futures research
and backtesting workflow. It is intended for research, education, and local
experimentation with legally obtained data.

The public release contains source code, configuration templates, tests, and
setup instructions. Private credentials, local databases, market-data files,
logs, and internal research documents are not part of the public release.

## Operating boundaries

- Analysts produce structured technical, fundamental, and futures-news evidence.
- The signal collector packages analyst evidence for the portfolio manager.
- The portfolio manager is the only component that signs the final action contract.
- The auditor checks the signed contract; the trader executes only approved actions.
- The accountant records settlement and PnL facts.
- The reviewer and researcher create future-facing feedback after the trading day.
- Historical replay and live-style execution use the same phase boundaries.

Research feedback is allowed to affect future trading dates only. The system
must not use future data, private credentials, or research conclusions to
rewrite past trading facts.

## Upstream

- Organization: https://github.com/quantskills
- Repository: https://github.com/quantskills/agent-future-trading
