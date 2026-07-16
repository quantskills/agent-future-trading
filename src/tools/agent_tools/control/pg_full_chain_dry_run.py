from __future__ import annotations

"""Isolated, deterministic PG assembly run over one formal SQLite database."""

import json
import os
import sqlite3
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pandas as pd

from agents.decision_team import portfolio_manager
from agents.decision_team.auditor import audit_futures_recommendation
from agents.decision_team.portfolio_manager import (
    finalize_pm_full_market_contracts,
    portfolio_agent_futures,
)
from agents.decision_team.signal_collector import signal_collector_agent
from agents.execution_team.trader import _process_strategy_recommendations
from agents.research_team.researcher import researcher_agent
from database.sqlite_helper import SQLiteDB
from graph.constants import Signal
from graph.schema import (
    AnalystSignal,
    BasePriceSource,
    MorningExecutionBasis,
    Portfolio,
    RecommendationSourceType,
    TradingPhase,
)
from tools.agent_tools.analysis.analyst_data_usage import (
    build_fundamental_data_usage,
    build_news_data_usage,
    build_technical_data_usage,
)
from tools.agent_tools.analysis.analyst_learning_context import build_learning_context
from tools.agent_tools.analysis.analyst_output_finalization import (
    build_required_market_data_unavailable_signal,
    finalize_analyst_signal,
)
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    build_profile_usage_contract,
    get_product_price_behavior_profile,
)
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult
from tools.agent_tools.decision.pm_decision_memory_retrieval import retrieve_pm_memory
from tools.agent_tools.execution.accountant_futures_settlement import FuturesDailySettlement
from tools.agent_tools.execution.trader_futures_execution import FuturesExecutionEngine
from tools.agent_tools.research import research_memory_writers
from tools.agent_tools.research.reviewer_phase4_review import (
    _group_transactions_by_recommendation,
    _review_recommendation_execution_facts,
    run_phase4_review,
)
from tools.common.contracts import (
    validate_accountant_artifact_boundary,
    validate_execution_artifact_boundary,
    validate_final_action_contract,
)
from tools.common.final_action_semantics import (
    validate_action_value_write_consistency,
    validate_final_action_lot_transition,
)
from tools.common.signal_evidence_collection import (
    build_scc_data_quality_summary,
    validate_action_evidence_contract,
    validate_signal_collection_contract,
)
from util.logger import logger


ANALYSTS = ("technical", "fundamental", "commodity_news")
DRY_RUN_DAY = "2025-03-10"


def _date_value(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value or "")[:10]


def _settlement_row(cursor: sqlite3.Cursor, config_id: str, trading_date: str) -> dict[str, Any] | None:
    row = cursor.execute(
        "SELECT ds.* FROM daily_settlement ds JOIN portfolio p ON p.id=ds.portfolio_id "
        "WHERE p.config_id=? AND substr(ds.trading_date,1,10)=? "
        "ORDER BY ds.created_at DESC LIMIT 1",
        (config_id, trading_date),
    ).fetchone()
    return dict(row) if row is not None else None


def _dry_run_config(cfg: dict[str, Any], artifact_root: Path) -> dict[str, Any]:
    value = deepcopy(cfg)
    value["exp_name"] = "protocol-governor-full-chain-dry-run"
    value["market_type"] = "china_futures"
    value["tickers"] = ["BU"]
    value["planner_mode"] = bool(value.get("planner_mode", False))
    value["trading_date"] = datetime.strptime(DRY_RUN_DAY, "%Y-%m-%d")
    value.setdefault("llm", {"provider": "test", "model": "test"})
    value.setdefault("max_total_margin_ratio", 0.20)
    value.setdefault("max_single_margin_ratio", 0.12)
    learning = value.setdefault("learning", {})
    template_prior = learning.setdefault("template_prior", {})
    template_prior["path"] = str(artifact_root / "template_prior.json")
    return value


def _build_dry_run_data_available_neutral_signal(
    *,
    analyst: str,
    ticker: str,
    trading_date: Any,
    full_config: dict[str, Any],
) -> AnalystSignal:
    """Exercise the real finalizer with isolated data-available Neutral input."""
    if analyst == "technical":
        prices = pd.DataFrame(
            {
                "open": [3000.0, 3005.0],
                "high": [3020.0, 3015.0],
                "low": [2990.0, 2995.0],
                "close": [3010.0, 3005.0],
                "volume": [1000.0, 950.0],
                "open_interest": [5000.0, 5050.0],
            },
            index=pd.to_datetime(["2025-03-06", "2025-03-07"]),
        )
        data_usage = build_technical_data_usage(
            ticker=ticker,
            trading_date=trading_date,
            prices_df=prices,
            indicators_used=["trend", "volume", "open_interest"],
        )
        quality_context = {
            "ticker": ticker,
            "sector": "energy",
            "tradeability": "medium",
            "market_regime": "range",
            "dominant_direction": "neutral",
            "indicator_votes": {"details": {}},
            "risk_flags": [],
            "setup_quality_ok": False,
            "features": {},
        }
    elif analyst == "fundamental":
        data_usage = build_fundamental_data_usage(
            ticker=ticker,
            trading_date=trading_date,
            fundamentals_metadata={
                "configured_indicator_count": 2,
                "loaded_indicator_count": 2,
                "coverage_ratio": 1.0,
                "stale_ratio": 0.0,
                "factor_freshness_score": 1.0,
                "no_lookahead_status": "ok",
                "indicator_role_counts": {"inventory": 1, "basis": 1},
                "local_finoview_availability_audit": {
                    "runtime_data_boundary": "local_feather_only",
                    "coverage_status": "ready",
                    "supports_fundamental_trade_setup": True,
                    "no_future_data": True,
                    "not_product_rule": True,
                },
            },
            pandaai_extra_context={},
        )
        quality_context = {
            "ticker": ticker,
            "sector": "energy",
            "tradeability": "medium",
            "market_regime": "range",
            "risk_flags": [],
            "setup_quality_ok": False,
            "factor_group_counts": {"inventory": 1, "basis": 1},
            "data_quality": {
                "coverage_ratio": 1.0,
                "factor_freshness_score": 1.0,
                "supports_fundamental_trade_setup": True,
                "no_lookahead_status": "ok",
            },
        }
    elif analyst == "commodity_news":
        data_usage = build_news_data_usage(
            ticker=ticker,
            trading_date=trading_date,
            news_metadata={
                "file_exists": True,
                "news_cutoff": "before_trading_date",
                "raw_block_count": 1,
                "parsed_news_count": 1,
                "selected_news_count": 1,
                "latest_news_date": "2025-03-07",
            },
            news_context={"freshness_score": 1.0, "relevance_score": 0.6},
        )
        quality_context = {
            "ticker": ticker,
            "sector": "energy",
            "tradeability": "medium",
            "event_regime": "background_news",
            "tradable_event": False,
            "price_reaction_required": True,
            "price_reaction_confirmed": False,
            "direction_counts": {},
            "event_type_counts": {"inventory": 1},
            "risk_flags": [],
            "setup_quality_ok": False,
        }
    else:
        raise ValueError("pg_dry_run_analyst_invalid")
    data_usage["data_available"] = True
    signal = AnalystSignal(
        agent_name=analyst,
        signal=Signal.NEUTRAL,
        confidence=0.4,
        justification="Available evidence is mixed and defines no complete setup.",
        data_cutoff="pre_open",
        no_lookahead_status="ok",
        horizon_class=(
            "event_short"
            if analyst == "commodity_news"
            else "medium"
            if analyst == "fundamental"
            else "short"
        ),
        expected_horizon_days=2,
        market_regime="range",
        setup_type="unknown",
        opportunity_type="no_trade",
        opportunity_state="no_opportunity",
        entry_trigger="",
        exit_hint="",
        trigger_valid=False,
        invalidation_present=False,
        neutral_reason="available evidence has no complete setup",
        missing_evidence=["specific_entry_trigger", "canonical_invalidation"],
        conflicting_factors=[],
        would_change_view_if="new evidence changes the current view",
        neutral_opportunity_bucket="evidence_gap",
        neutral_trigger_condition="",
        counterfactual_side="flat",
        neutral_watchlist_priority="none",
        metadata={"data_usage_summary": data_usage},
    )
    profile = get_product_price_behavior_profile(ticker, full_config)
    usage = build_profile_usage_contract(ticker, analyst, profile)
    return finalize_analyst_signal(
        signal,
        quality_context=quality_context,
        full_config=full_config,
        analyst=analyst,
        ticker=ticker,
        trading_date=trading_date,
        learning_context={},
        product_profile=profile,
        product_profile_usage=usage,
    )


def _audit_and_persist(
    *,
    db: SQLiteDB,
    recommendation: Any,
    portfolio: Portfolio,
    cfg: dict[str, Any],
) -> None:
    snapshot = recommendation.signal_snapshot if isinstance(recommendation.signal_snapshot, dict) else {}
    scc = snapshot["signal_collection_contract"]
    fac = snapshot["final_action_contract"]
    output = audit_futures_recommendation(
        recommendation=recommendation.model_dump(),
        hard_risk_config={"max_total_margin_ratio": float(cfg["max_total_margin_ratio"])},
        account_state={
            "account_equity": portfolio.account_equity,
            "margin_used": portfolio.margin_used,
            "margin_ratio": portfolio.margin_ratio,
            "risk_status": portfolio.risk_status,
        },
        position_state={
            "ticker": "BU",
            "current_lots": 0,
            "contract_code": None,
            "margin_used": 0.0,
            "margin_rate": None,
            "contract_multiplier": None,
        },
        contract_state={
            "contract_code": fac.get("contract_code"),
            "underlying_code": "BU",
            "as_of_date": DRY_RUN_DAY,
            "source": "isolated_canonical_input",
        },
        data_quality=build_scc_data_quality_summary(scc),
    )
    snapshot["auditor"] = {
        "producer": "auditor",
        "audit_status": output.audit_status,
        "audit_verdict": output.audit_verdict,
        "audit_reason_codes": list(output.audit_reason_codes),
        "audited_at": output.audited_at,
        "independent_auditor_agent": True,
        "pm_risk_gate_is_not_auditor": True,
    }
    recommendation.signal_snapshot = snapshot
    recommendation.audit_payload = output.audit_payload
    if not db.update_futures_recommendation_status(
        recommendation.id,
        recommendation.status,
        action=recommendation.action,
        lots=recommendation.lots,
        signal_snapshot=snapshot,
        audit_payload=output.audit_payload,
    ):
        raise RuntimeError("pg_dry_run_auditor_persistence_failed")


def _run_researcher(
    *,
    db: SQLiteDB,
    cfg: dict[str, Any],
    config_id: str,
    trading_date: str,
) -> None:
    recommendations = db.get_futures_recommendations_by_effective_date(config_id, trading_date)
    strategy_recommendations = [
        row for row in recommendations if row.get("source_type") == RecommendationSourceType.STRATEGY.value
    ]
    transactions = db.get_futures_transactions_by_date(
        config_id,
        trading_date,
        execution_phase=TradingPhase.PHASE2,
    )
    transactions_by_recommendation = _group_transactions_by_recommendation(transactions)
    no_trade_reasons: Counter = _review_recommendation_execution_facts(
        recommendations,
        transactions_by_recommendation,
        [],
    )
    connection = sqlite3.connect(db.db_path)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.cursor()
        with patch(
            "tools.agent_tools.research.research_learning.run_researcher_causal_review",
            return_value=[],
        ), patch(
            "tools.agent_tools.research.research_learning.write_exploratory_hypotheses",
            return_value={"rows": 0, "status": "no_hypotheses", "episode_count": 0},
        ), patch(
            "llm.inference.agent_call",
            side_effect=AssertionError("PG dry run must not call an LLM"),
        ) as llm_call:
            researcher_agent(
                db=db,
                cursor=cursor,
                cfg=cfg,
                config_id=config_id,
                trading_date=trading_date,
                settlement_row=_settlement_row(cursor, config_id, trading_date),
                recommendations=recommendations,
                strategy_recommendations=strategy_recommendations,
                no_trade_reason_counter=no_trade_reasons,
                transactions_by_recommendation=transactions_by_recommendation,
            )
        if llm_call.called:
            raise RuntimeError("pg_dry_run_llm_call_detected")
        research_memory_writers.insert_researcher_learning_completion_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _verify_physical_chain(
    *,
    db: SQLiteDB,
    config_id: str,
    signal_ids: set[str],
) -> None:
    connection = sqlite3.connect(db.db_path)
    connection.row_factory = sqlite3.Row
    try:
        signal_count = connection.execute(
            "SELECT COUNT(*) FROM signal WHERE portfolio_id IN "
            "(SELECT id FROM portfolio WHERE config_id=?)",
            (config_id,),
        ).fetchone()[0]
        if signal_count != 3:
            raise RuntimeError("pg_dry_run_signal_count_invalid")
        recommendations = db.get_futures_recommendations_by_effective_date(config_id, DRY_RUN_DAY)
        if len(recommendations) != 1:
            raise RuntimeError("pg_dry_run_recommendation_count_invalid")
        snapshot = recommendations[0].get("signal_snapshot") or {}
        scc = validate_signal_collection_contract(
            snapshot.get("signal_collection_contract"),
            ticker="BU",
            trading_date=DRY_RUN_DAY,
            enabled_analysts=ANALYSTS,
            require_signal_record_ids=True,
        )
        for source in scc.get("source_contracts") or []:
            aec = source.get("action_evidence_contract") if isinstance(source, dict) else {}
            if (
                not isinstance(aec, dict)
                or aec.get("signal") != "Neutral"
                or aec.get("opportunity_state") != "no_opportunity"
                or (aec.get("data_usage_summary") or {}).get("data_available") is not True
            ):
                raise RuntimeError("pg_dry_run_ordinary_neutral_aec_invalid")
        persisted_ids = {
            str(source.get("signal_record_id") or "")
            for source in scc.get("source_contracts") or []
            if isinstance(source, dict)
        }
        if persisted_ids != signal_ids:
            raise RuntimeError("pg_dry_run_signal_lineage_invalid")
        fac = snapshot.get("final_action_contract") or {}
        if validate_final_action_contract(fac) or not validate_final_action_lot_transition(fac).get("ok"):
            raise RuntimeError("pg_dry_run_fac_invalid")
        validate_execution_artifact_boundary(snapshot)
        phases = connection.execute(
            "SELECT phase,status FROM trading_day_phase WHERE config_id=? AND trading_date=?",
            (config_id, DRY_RUN_DAY),
        ).fetchall()
        if {(row["phase"], row["status"]) for row in phases} != {
            ("phase1", "completed"),
            ("phase2", "completed"),
            ("phase3", "completed"),
            ("phase4", "completed"),
        }:
            raise RuntimeError("pg_dry_run_phase_order_invalid")
        if connection.execute(
            "SELECT COUNT(*) FROM daily_settlement ds JOIN portfolio p ON p.id=ds.portfolio_id "
            "WHERE p.config_id=? AND substr(ds.trading_date,1,10)=?",
            (config_id, DRY_RUN_DAY),
        ).fetchone()[0] != 1:
            raise RuntimeError("pg_dry_run_settlement_missing")
        settlement = connection.execute(
            "SELECT ds.* FROM daily_settlement ds JOIN portfolio p ON p.id=ds.portfolio_id "
            "WHERE p.config_id=? AND substr(ds.trading_date,1,10)=? LIMIT 1",
            (config_id, DRY_RUN_DAY),
        ).fetchone()
        validate_accountant_artifact_boundary(dict(settlement))
        for row in connection.execute(
            "SELECT * FROM alpha_setup_action_value WHERE config_id=?",
            (config_id,),
        ).fetchall():
            action_value = dict(row)
            action_value["payload"] = json.loads(action_value.get("payload_json") or "{}")
            if not validate_action_value_write_consistency(action_value).get("ok"):
                raise RuntimeError("pg_dry_run_action_value_invalid")
        if connection.execute(
            "SELECT COUNT(*) FROM learning_event_log WHERE config_id=? AND trading_date=? "
            "AND event_type='researcher_learning_completed' AND status='applied'",
            (config_id, DRY_RUN_DAY),
        ).fetchone()[0] != 1:
            raise RuntimeError("pg_dry_run_research_event_missing")
    finally:
        connection.close()


def run_no_llm_full_chain_dry_run(
    *,
    db_path: str | Path,
    cfg: dict[str, Any],
    artifact_root: str | Path,
) -> ProtocolCheckResult:
    """Run one legal no-trade day through real production functions and persistence APIs."""
    db_path = Path(db_path)
    artifact_root = Path(artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)
    old_artifact_root = os.getenv("AGENTQUANT_ARTIFACT_ROOT")
    old_report_root = os.getenv("AGENTQUANT_TRANSACTION_REPORT_DIR")
    old_log_dir = logger.log_dir
    os.environ["AGENTQUANT_ARTIFACT_ROOT"] = str(artifact_root)
    os.environ["AGENTQUANT_TRANSACTION_REPORT_DIR"] = str(artifact_root / "reports")
    logger.log_dir = str(artifact_root / "logs")
    trace: list[str] = []
    try:
        dry_cfg = _dry_run_config(cfg, artifact_root)
        db = SQLiteDB()
        db.db_path = str(db_path)
        config_id = db.create_config(dry_cfg)
        if not config_id:
            raise RuntimeError("pg_dry_run_config_persistence_failed")
        portfolio_row = db.create_portfolio(
            config_id,
            float(dry_cfg.get("initial_cash") or 1_000_000.0),
            dry_cfg["trading_date"],
        )
        if not portfolio_row:
            raise RuntimeError("pg_dry_run_portfolio_persistence_failed")
        portfolio = Portfolio(**portfolio_row)

        db.start_trading_day_phase(config_id, DRY_RUN_DAY, TradingPhase.PHASE1)
        analyst_signals = []
        signal_ids: set[str] = set()
        for analyst in ANALYSTS:
            unavailable_signal = build_required_market_data_unavailable_signal(
                analyst=analyst,
                ticker="BU",
                trading_date=dry_cfg["trading_date"],
                full_config=dry_cfg,
            )
            unavailable_aec = validate_action_evidence_contract(
                unavailable_signal.metadata["action_evidence_contract"],
                analyst=analyst,
            )
            if (
                unavailable_aec.get("opportunity_state") != "no_opportunity"
                or (unavailable_aec.get("data_usage_summary") or {}).get("data_available") is not False
            ):
                raise RuntimeError("pg_dry_run_data_unavailable_neutral_aec_invalid")
            trace.append(f"aec_data_unavailable_validation:{analyst}")

            signal = _build_dry_run_data_available_neutral_signal(
                analyst=analyst,
                ticker="BU",
                trading_date=dry_cfg["trading_date"],
                full_config=dry_cfg,
            )
            signal_id = db.save_signal(portfolio.id, analyst, "BU", signal)
            if not signal_id:
                raise RuntimeError("pg_dry_run_signal_persistence_failed")
            signal.metadata = dict(signal.metadata or {})
            signal.metadata["signal_record_id"] = signal_id
            analyst_signals.append(signal)
            signal_ids.add(signal_id)
            trace.append(f"aec:{analyst}")

        morning_context = SimpleNamespace(
            base_price=3005.0,
            base_price_source="t_minus_1_close_fallback",
            base_price_date="2025-03-07",
            open_price=None,
            prev_close_price=3010.0,
            warning_message=None,
            contract_code="BU2506",
            contract_facts={
                "contract_code": "BU2506",
                "underlying_code": "BU",
                "as_of_date": "2025-03-07",
                "source": "isolated_canonical_input",
            },
        )
        state = {
            "portfolio": portfolio,
            "ticker": "BU",
            "trading_date": dry_cfg["trading_date"],
            "analyst_signals": analyst_signals,
            "enabled_analysts": list(ANALYSTS),
            "num_tickers": 1,
            "config_id": config_id,
            "phase": TradingPhase.PHASE1,
            "morning_price_context": morning_context,
            "config": dry_cfg,
            "full_config": dry_cfg,
            "router": None,
        }
        state.update(signal_collector_agent(state))
        trace.append("scc")
        with patch.object(portfolio_manager, "get_db", return_value=db):
            pm_result = portfolio_agent_futures(state)
        trace.append("pm_steps_1_4")
        signed = finalize_pm_full_market_contracts(
            generated=[("BU", pm_result["pm_state"])],
            config=dry_cfg,
            portfolio=portfolio,
        )
        trace.append("pm_steps_5_6_fac")
        recommendation = signed[0][1]
        recommendation_id = db.save_futures_recommendation(recommendation)
        if not recommendation_id:
            raise RuntimeError("pg_dry_run_recommendation_persistence_failed")
        recommendation.id = recommendation_id
        _audit_and_persist(db=db, recommendation=recommendation, portfolio=portfolio, cfg=dry_cfg)
        trace.append("auditor")
        db.complete_trading_day_phase(config_id, DRY_RUN_DAY, TradingPhase.PHASE1, "completed")

        db.start_trading_day_phase(config_id, DRY_RUN_DAY, TradingPhase.PHASE2)
        persisted_recommendations = db.get_futures_recommendations_by_effective_date(
            config_id,
            DRY_RUN_DAY,
            source_type=RecommendationSourceType.STRATEGY,
        )
        execution_engine = FuturesExecutionEngine(dry_cfg, db)
        execution_basis = MorningExecutionBasis(
            base_price=3012.0,
            base_price_source=BasePriceSource.T_OPEN,
            base_price_date=DRY_RUN_DAY,
            open_price=3012.0,
            prev_close_price=3010.0,
            contract_code="BU2506",
            contract_facts=morning_context.contract_facts,
        )
        execution_router = SimpleNamespace(
            resolve_morning_execution_base_price=lambda **_kwargs: execution_basis,
        )
        portfolio, _summary = _process_strategy_recommendations(
            cfg=dry_cfg,
            db=db,
            config_id=config_id,
            router=execution_router,
            execution_engine=execution_engine,
            portfolio=portfolio,
            strategy_recommendations=persisted_recommendations,
            trading_date_value=DRY_RUN_DAY,
            runtime_mode="backtest_replay",
            cutoff_datetime=None,
            finalize_untriggered=True,
            loop_iteration=1,
        )
        trace.append("trader")
        db.complete_trading_day_phase(config_id, DRY_RUN_DAY, TradingPhase.PHASE2, "completed")

        db.start_trading_day_phase(config_id, DRY_RUN_DAY, TradingPhase.PHASE3)
        settlement_engine = FuturesDailySettlement.__new__(FuturesDailySettlement)
        settlement_engine.market_type = "china_futures"
        settlement_engine.config = dry_cfg
        settlement_engine.db = db
        settlement_engine.router = SimpleNamespace()
        settlement_engine.run_phase3(config_id=config_id, trading_date=dry_cfg["trading_date"])
        trace.append("accountant")
        db.complete_trading_day_phase(config_id, DRY_RUN_DAY, TradingPhase.PHASE3, "completed")

        run_phase4_review(
            cfg=dry_cfg,
            db=db,
            config_id=config_id,
            trading_date=DRY_RUN_DAY,
        )
        trace.append("reviewer")
        _run_researcher(db=db, cfg=dry_cfg, config_id=config_id, trading_date=DRY_RUN_DAY)
        trace.append("researcher")

        next_day = _date_value(dry_cfg["trading_date"] + timedelta(days=1))
        build_learning_context(
            db=db,
            full_config=dry_cfg,
            config_id=config_id,
            trading_date=next_day,
            analyst="technical",
            ticker="BU",
            context={},
            horizon_class="short",
        )
        trace.append("next_day_analyst_learning_read")
        retrieve_pm_memory(
            db=db,
            config_id=config_id,
            ticker="BU",
            side="long",
            trading_date=next_day,
            full_config=dry_cfg,
        )
        trace.append("next_day_pm_learning_read")

        _verify_physical_chain(db=db, config_id=config_id, signal_ids=signal_ids)
        expected_trace = [
            "aec_data_unavailable_validation:technical",
            "aec:technical",
            "aec_data_unavailable_validation:fundamental",
            "aec:fundamental",
            "aec_data_unavailable_validation:commodity_news",
            "aec:commodity_news",
            "scc",
            "pm_steps_1_4",
            "pm_steps_5_6_fac",
            "auditor",
            "trader",
            "accountant",
            "reviewer",
            "researcher",
            "next_day_analyst_learning_read",
            "next_day_pm_learning_read",
        ]
        if trace != expected_trace:
            raise RuntimeError("pg_dry_run_call_order_invalid")
        return ProtocolCheckResult.pass_result("no_llm_full_chain_dry_run")
    except Exception:
        return ProtocolCheckResult.fail_result(
            "no_llm_full_chain_dry_run",
            ["formal_no_llm_full_chain_dry_run_failed"],
        )
    finally:
        if old_artifact_root is None:
            os.environ.pop("AGENTQUANT_ARTIFACT_ROOT", None)
        else:
            os.environ["AGENTQUANT_ARTIFACT_ROOT"] = old_artifact_root
        if old_report_root is None:
            os.environ.pop("AGENTQUANT_TRANSACTION_REPORT_DIR", None)
        else:
            os.environ["AGENTQUANT_TRANSACTION_REPORT_DIR"] = old_report_root
        logger.log_dir = old_log_dir
