from __future__ import annotations

import sqlite3
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.analysis_team import commodity_news, fundamental, technical
from agents.decision_team import portfolio_manager
from agents.decision_team.signal_collector import signal_collector_agent
from apis.router import Router
from graph.constants import Signal
from graph.schema import (
    AnalystSignal,
    FuturesRecommendation,
    FuturesTransaction,
    Portfolio,
    Position,
    RecommendationSourceType,
    RecommendationStatus,
)
from tools.common.signal_evidence_collection import (
    build_signal_collection_contract,
    validate_action_evidence_contract,
    validate_signal_collection_contract,
)


ANALYSTS = ("technical", "fundamental", "commodity_news")


def _complete_aec(analyst: str, *, data_available: bool = True) -> dict:
    if not data_available:
        source_name = "pandaai_pre_open_reference"
        source_payload = {
            "source": "PandaAI",
            "dataset": "previous_trading_day_main_contract_quote",
            "available": False,
            "used_in_signal": False,
            "pre_open_only": True,
            "info_cutoff": "pre_open",
            "missing_data": ["pre_open_reference_price"],
            "data_quality_flags": ["pre_open_reference_price_unavailable"],
            "reason": "pre_open_reference_price_unavailable",
        }
    else:
        source_name, source, dataset = {
            "technical": ("pandaai_market", "PandaAI", "daily_continuous_candles"),
            "fundamental": (
                "finoview_fundamental",
                "Finoview",
                "local_feather_fundamental",
            ),
            "commodity_news": ("finoview_news_txt", "Finoview", "local_news_txt"),
        }[analyst]
        source_payload = {
            "source": source,
            "dataset": dataset,
            "available": True,
            "used_in_signal": True,
            "pre_open_only": True,
            "info_cutoff": "pre_open",
        }
    return {
        "contract_version": "agentquant.action_evidence.v1",
        "analyst": analyst,
        "sector": "energy",
        "side": "flat",
        "signal": "Neutral",
        "confidence": 0.0,
        "opportunity_type": "no_trade",
        "opportunity_state": "no_opportunity",
        "setup_type": "data_unavailable_no_trade" if not data_available else "no_trade",
        "setup_quality_ok": False,
        "trigger_valid": False,
        "current_trigger_confirmed": False,
        "invalidation_present": False,
        "entry_trigger": "",
        "entry_timing_signal": "",
        "evidence_role": {
            "technical": "entry_timing",
            "fundamental": "direction_context",
            "commodity_news": "event_catalyst",
        }[analyst],
        "exit_hint": "",
        "horizon_class": "flat",
        "expected_horizon_days": 0,
        "market_regime": "data_unavailable" if not data_available else "unknown",
        "evidence_quality": "low",
        "evidence_strength": "weak",
        "evidence_freshness": "missing" if not data_available else "usable",
        "confirmation_requirements": (
            ["valid_pre_open_reference_price"] if not data_available else []
        ),
        "missing_evidence": (
            ["pre_open_reference_price"] if not data_available else []
        ),
        "current_evidence_conflict": [],
        "factor_focus": ["required_market_data"],
        "no_lookahead_status": "ok",
        "data_usage_summary": {
            "ticker": "BU",
            "trading_date": "2025-03-03",
            "analyst": analyst,
            "data_available": data_available,
            "sources": {source_name: source_payload},
        },
        "learning_scope": {},
        "product_profile_evidence": {},
        "fusion_evidence": {
            "evidence_strength": "weak",
            "evidence_freshness": "missing" if not data_available else "usable",
            "confirmation_requirements": (
                ["valid_pre_open_reference_price"] if not data_available else []
            ),
        },
    }


def _signal(analyst: str, signal_record_id: str, *, data_available: bool = True) -> AnalystSignal:
    signal = AnalystSignal(
        agent_name=analyst,
        signal=Signal.NEUTRAL,
        confidence=0.0,
        opportunity_type="no_trade",
        opportunity_state="no_opportunity",
        setup_type="data_unavailable_no_trade" if not data_available else "no_trade",
        data_freshness="missing" if not data_available else "fresh",
        evidence_quality="low",
        evidence_strength="weak",
        evidence_freshness="missing" if not data_available else "usable",
        horizon_class="flat",
        expected_horizon_days=0,
        no_lookahead_status="ok",
    )
    signal.metadata = {
        "signal_record_id": signal_record_id,
        "action_evidence_contract": _complete_aec(
            analyst,
            data_available=data_available,
        ),
    }
    return signal


def _scc(*, data_available: bool = True) -> dict:
    signals = [
        _signal(analyst, f"signal-{index}", data_available=data_available)
        for index, analyst in enumerate(ANALYSTS, start=1)
    ]
    return build_signal_collection_contract(
        ticker="BU",
        trading_date="2025-03-03",
        analyst_signals=signals,
        enabled_analysts=ANALYSTS,
    )


def _final_action_contract(*, current_lots: int = 0, target_lots: int = 1) -> dict:
    return {
        "contract_version": "agentquant.final_action.v1",
        "ticker": "BU",
        "contract_code": "BU2506",
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": target_lots - current_lots,
        "final_action": "open_probe" if current_lots == 0 and target_lots > 0 else "hold",
        "authority_type": "exploration_probe",
        "invalidation_condition": "close below validated setup boundary",
        "target_margin_ratio_estimate": 0.01,
        "requires_intraday_confirmation": False,
        "can_execute_without_intraday_trigger": True,
    }


def _full_auditor_payload(recommendation_id: str = "rec-1") -> dict:
    return {
        "contract_version": "agentquant.audit_verdict.v1",
        "producer": "auditor",
        "agent_name": "auditor",
        "recommendation_id": recommendation_id,
        "ticker": "BU",
        "trading_date": "2025-03-03",
        "config_id": "cfg",
        "audit_status": "approved",
        "audit_verdict": "approve",
        "audit_reason_codes": [],
        "hard_risk_reasons": [],
        "soft_risk_reasons": [],
        "audited_by": "auditor",
        "audited_at": "2025-03-03T00:00:00+00:00",
        "source": {
            "pm_recommendation_id": recommendation_id,
            "final_action_contract_hash_source": (
                "futures_recommendation.signal_snapshot.final_action_contract"
            ),
        },
        "boundary": {
            "auditor_does_not_modify_final_action_contract": True,
            "auditor_does_not_create_trade_authority": True,
            "trader_requires_approved_audit_verdict": True,
            "research_memory_not_consumed": True,
            "auditor_reads_research_db": False,
        },
        "contract_summary": {
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": 1,
            "lots_delta": 1,
            "requires_intraday_confirmation": False,
            "can_execute_without_intraday_trigger": True,
        },
        "semantic_state": {
            "lifecycle_state": "open",
            "requires_intraday_result": False,
            "hard_block_reasons": [],
            "soft_limit_reasons": [],
            "semantic_errors": [],
        },
    }


class FrozenMainlineRegressionTest(unittest.TestCase):
    def test_shared_aec_validator_rejects_incomplete_contract(self):
        incomplete = {
            "contract_version": "agentquant.action_evidence.v1",
            "analyst": "technical",
            "signal": "Neutral",
            "side": "flat",
            "confidence": 0.0,
            "opportunity_state": "no_opportunity",
        }
        with self.assertRaisesRegex(ValueError, "action_evidence_contract_missing_required_field"):
            validate_action_evidence_contract(incomplete, analyst="technical")

    def test_three_analyst_entries_emit_valid_neutral_aec_without_llm(self):
        portfolio = Portfolio(
            id="portfolio-1",
            cashflow=1_000_000.0,
            account_equity=1_000_000.0,
            positions={},
            risk_status="NORMAL",
        )
        state = {
            "ticker": "BU",
            "trading_date": datetime(2025, 3, 3),
            "portfolio": portfolio,
            "market_type": "china_futures",
            "pre_open_only": True,
            "info_cutoff": "pre_open",
            "llm_config": {"provider": "test", "model": "test"},
            "config": {},
            "full_config": {},
            "pre_open_reference_price_unavailable": True,
            "pre_open_reference_price_unavailable_reason": "missing_previous_close",
        }
        for module, entry, analyst in (
            (technical, technical.technical_agent, "technical"),
            (fundamental, fundamental.fundamental_agent, "fundamental"),
            (commodity_news, commodity_news.commodity_news_agent, "commodity_news"),
        ):
            with self.subTest(analyst=analyst), patch.object(
                module,
                "agent_call",
                side_effect=AssertionError("LLM must not run for required market-data gaps"),
            ) as llm_call, patch.object(
                module,
                "Router",
                side_effect=AssertionError("market data must not be re-fetched after the global gap"),
            ):
                output = entry(dict(state))
                self.assertEqual(len(output["analyst_signals"]), 1)
                produced = output["analyst_signals"][0]
                self.assertEqual(produced.agent_name, analyst)
                self.assertEqual(produced.signal, Signal.NEUTRAL)
                contract = validate_action_evidence_contract(
                    produced.metadata["action_evidence_contract"],
                    analyst=analyst,
                )
                self.assertEqual(set(output), {"analyst_signals"})
                self.assertEqual(set(produced.metadata), {"action_evidence_contract"})
                self.assertEqual(contract["opportunity_state"], "no_opportunity")
                self.assertFalse(contract["trigger_valid"])
                self.assertFalse(contract["current_trigger_confirmed"])
                self.assertEqual(contract["entry_trigger"], "")
                llm_call.assert_not_called()

    def test_collector_rejects_internal_metadata_beside_aec_and_record_id(self):
        signals = [
            _signal(analyst, f"signal-{index}")
            for index, analyst in enumerate(ANALYSTS, start=1)
        ]
        signals[0].metadata["internal_state"] = {"unvalidated_tool_result": True}
        with self.assertRaisesRegex(
            ValueError,
            "signal_collection_forbidden_source_metadata",
        ):
            build_signal_collection_contract(
                ticker="BU",
                trading_date="2025-03-03",
                analyst_signals=signals,
                enabled_analysts=ANALYSTS,
            )

    def test_news_aec_data_usage_excludes_local_file_runtime_details(self):
        from tools.agent_tools.analysis.analyst_data_usage import build_news_data_usage

        usage = build_news_data_usage(
            ticker="BU",
            trading_date="2025-03-03",
            news_metadata={
                "file_exists": True,
                "file_path": r"D:\private\Future_news\BU.txt",
                "encoding": "private-codec",
                "raw_block_count": 2,
                "parsed_news_count": 1,
                "selected_news_count": 1,
                "latest_news_date": "2025-03-02",
                "news_cutoff": "<2025-03-03",
            },
            news_context={"freshness_score": 1.0, "relevance_score": 0.8},
        )
        source = usage["sources"]["finoview_news_txt"]
        self.assertNotIn("file_path", source)
        self.assertNotIn("encoding", source)

    def test_shared_aec_validator_rejects_nested_internal_fields(self):
        contract = _complete_aec("technical")
        contract["data_usage_summary"]["sources"]["pandaai_market"][
            "hidden_context"
        ] = "private-state"
        with self.assertRaisesRegex(
            ValueError,
            "action_evidence_contract_forbidden_internal_field",
        ):
            validate_action_evidence_contract(contract, analyst="technical")

    def test_workflow_data_gap_state_uses_stable_reason_not_router_message(self):
        from graph.workflow import AgentWorkflow

        workflow = AgentWorkflow.__new__(AgentWorkflow)
        workflow._build_futures_phase1_state = Mock(return_value={})
        state = workflow._build_futures_phase1_analysis_state(
            "BU",
            Mock(),
            SimpleNamespace(
                base_price=None,
                warning_message="private router/provider diagnostic",
            ),
            list(ANALYSTS),
        )
        self.assertEqual(
            state["pre_open_reference_price_unavailable_reason"],
            "pre_open_reference_price_unavailable",
        )
        self.assertNotIn("pre_open_reference_price_unavailable_warning", state)

    def test_phase2_failure_records_only_stable_boundary_code(self):
        from agents.execution_team import trader

        db = Mock()
        with patch.object(trader.logger, "error") as log_error:
            trader._record_phase2_failure(db, "cfg", "2025-03-03")
        db.complete_trading_day_phase.assert_called_once_with(
            "cfg",
            "2025-03-03",
            trader.TradingPhase.PHASE2,
            "failed",
            "phase2_execution_failed",
        )
        log_error.assert_called_once_with("phase2_execution_failed")

    def test_market_confirmation_does_not_log_internal_result_or_raw_error(self):
        from tools.agent_tools.analysis.analyst_market_confirmation import (
            MarketConfirmationEngine,
        )

        router = Mock()
        router.get_pandaai_futures_extra_snapshot.return_value = {
            "records": {},
            "record_counts": {},
            "feature_status": {},
            "feature_diagnostics": {},
            "errors": [],
        }
        engine = MarketConfirmationEngine(
            {
                "pandaai_extra_data": {"enabled": True, "reference_lag_days": 1},
                "market_confirmation": {"enabled": True},
            },
            router=router,
        )
        module = "tools.agent_tools.analysis.analyst_market_confirmation"
        with patch(f"{module}.get_previous_trading_day", return_value="2025-03-02"), patch(
            f"{module}.logger.info"
        ) as info_log:
            result = engine.evaluate(
                underlying_code="BU",
                trading_date="2025-03-03",
                target_direction="long",
                signal_strength=0.8,
                contract_code="BU2506",
            )
        self.assertTrue(result["enabled"])
        info_log.assert_not_called()

        with patch(
            f"{module}.get_previous_trading_day",
            side_effect=RuntimeError("private-provider-detail"),
        ), patch(f"{module}.logger.warning") as warning_log:
            engine.evaluate(
                underlying_code="BU",
                trading_date="2025-03-03",
                target_direction="long",
                signal_strength=0.8,
                contract_code="BU2506",
            )
        warning_log.assert_called_once_with(
            "BU: pandaai_confirmation_reference_date_unavailable"
        )

    def test_llm_failure_never_returns_default_output_or_leaks_provider_error(self):
        from pydantic import BaseModel
        from llm import inference

        class Output(BaseModel):
            value: str = "default-must-not-be-returned"

        llm = Mock()
        llm.with_structured_output.return_value = llm
        llm.invoke.side_effect = RuntimeError("private-provider-response")
        with patch.object(inference, "get_model", return_value=llm), patch.object(
            inference.logger,
            "info",
        ) as info_log, patch.object(inference.logger, "warning") as warning_log, patch.object(
            inference.logger,
            "error",
        ) as error_log:
            with self.assertRaisesRegex(RuntimeError, "llm_inference_failed:unknown"):
                inference.agent_call(
                    "private prompt",
                    {
                        "provider": "CodexOpenAI",
                        "model": "test-model",
                        "max_retries": 1,
                        "structured_output_method": "json_mode",
                        "failure_policy": {"unknown": "retry_then_raise"},
                    },
                    Output,
                )

        info_log.assert_not_called()
        logged = " ".join(
            str(call.args[0])
            for mock in (warning_log, error_log)
            for call in mock.call_args_list
        )
        self.assertNotIn("private-provider-response", logged)
        self.assertNotIn("private prompt", logged)

    def test_router_exposes_pre_open_concrete_contract_fact(self):
        router = Router.__new__(Router)
        quote = SimpleNamespace(
            close_price=3200.0,
            ticker="BU2506",
            trade_date="2025-02-28",
            exchange_cd="SHFE",
            main_con=1,
        )
        router._resolve_previous_close_quote = Mock(
            return_value=(quote, datetime(2025, 2, 28))
        )
        basis = router.resolve_pre_open_reference_price("BU", "2025-03-03")
        self.assertEqual(basis.contract_code, "BU2506")
        self.assertEqual(basis.contract_facts["as_of_date"], "2025-02-28")
        self.assertEqual(basis.contract_facts["source"], "pandaai_main_contract_quote")

    def test_pm_contract_code_binding_prefers_real_position_then_router_fact(self):
        resolver = getattr(portfolio_manager, "_resolve_phase1_contract_code", None)
        self.assertIsNotNone(resolver, "PM concrete-contract binding helper is missing")
        context = SimpleNamespace(contract_code="BU2506")
        self.assertEqual(
            resolver(Position(shares=2, contract_code="BU2505"), context),
            "BU2505",
        )
        self.assertEqual(
            resolver(Position(shares=0, contract_code="BU2505"), context),
            "BU2506",
        )
        self.assertIsNone(resolver(None, SimpleNamespace(contract_code=None)))

    def test_workflow_passes_all_formal_facts_to_auditor(self):
        from agents.decision_team.auditor import AuditorOutput
        from graph.workflow import AgentWorkflow

        workflow = AgentWorkflow.__new__(AgentWorkflow)
        workflow.config = {
            "max_total_margin_ratio": 0.20,
            "position_budget_policy": {"hard_max_total_margin_ratio": 0.20},
            "rank_score_policy": {"must_not_reach_auditor": True},
        }
        workflow.init_portfolio = Portfolio(
            id="portfolio-1",
            cashflow=900_000.0,
            account_equity=1_000_000.0,
            margin_used=100_000.0,
            margin_ratio=0.10,
            risk_status="LIQUIDATION",
            positions={
                "BU": Position(
                    shares=1,
                    contract_code="BU2506",
                    margin_used=100_000.0,
                    margin_rate=0.10,
                    contract_multiplier=10.0,
                )
            },
        )
        workflow._phase1_contract_states = {
            "BU": {
                "contract_code": "BU2506",
                "underlying_code": "BU",
                "as_of_date": "2025-02-28",
                "source": "portfolio_position",
            }
        }
        workflow.db = SimpleNamespace(
            update_futures_recommendation_status=Mock(return_value=True)
        )
        recommendation = FuturesRecommendation(
            id="rec-1",
            config_id="cfg",
            reference_portfolio_id="portfolio-1",
            trading_date="2025-03-03",
            effective_trade_date="2025-03-03",
            source_type=RecommendationSourceType.STRATEGY,
            underlying_code="BU",
            contract_code="BU2506",
            status=RecommendationStatus.PENDING,
            signal_snapshot={
                "final_action_contract": _final_action_contract(
                    current_lots=1,
                    target_lots=2,
                ),
                "signal_collection_contract": _scc(data_available=True),
            },
        )
        audit_output = AuditorOutput(
            audit_status="blocked",
            audit_verdict="block",
            audit_payload=_full_auditor_payload(),
            audit_reason_codes=["account_liquidation_blocks_new_risk"],
            hard_risk_reasons=["account_liquidation_blocks_new_risk"],
            soft_risk_reasons=[],
            audited_at="2025-03-03T00:00:00+00:00",
        )
        with patch(
            "graph.workflow.audit_futures_recommendation",
            return_value=audit_output,
        ) as audit_call:
            workflow._audit_phase1_strategy_recommendations([("BU", recommendation)])

        kwargs = audit_call.call_args.kwargs
        self.assertEqual(kwargs["account_state"]["risk_status"], "LIQUIDATION")
        self.assertEqual(kwargs["position_state"]["current_lots"], 1)
        self.assertEqual(kwargs["contract_state"]["contract_code"], "BU2506")
        self.assertEqual(kwargs["data_quality"]["status"], "clean")
        self.assertEqual(kwargs["hard_risk_config"], {"max_total_margin_ratio": 0.20})
        self.assertNotIn("full_config", kwargs)

    def test_auditor_blocks_position_and_contract_fact_mismatch(self):
        from agents.decision_team.auditor import audit_futures_recommendation

        recommendation = {
            "id": "rec-1",
            "config_id": "cfg",
            "source_type": "strategy",
            "underlying_code": "BU",
            "trading_date": "2025-03-03",
            "effective_trade_date": "2025-03-03",
            "signal_snapshot": {
                "final_action_contract": _final_action_contract(
                    current_lots=0,
                    target_lots=1,
                )
            },
        }
        output = audit_futures_recommendation(
            recommendation=recommendation,
            hard_risk_config={"max_total_margin_ratio": 0.20},
            account_state={
                "account_equity": 1_000_000.0,
                "margin_used": 0.0,
                "margin_ratio": 0.0,
                "risk_status": "NORMAL",
            },
            position_state={"current_lots": 2, "contract_code": "BU2505"},
            contract_state={
                "contract_code": "BU2507",
                "underlying_code": "BU",
                "as_of_date": "2025-02-28",
                "source": "pandaai_main_contract_quote",
            },
            data_quality={"status": "clean", "flags": []},
        )
        self.assertEqual(output.audit_verdict, "block")
        self.assertIn("position_current_lots_mismatch", output.hard_risk_reasons)
        self.assertIn("contract_state_code_mismatch", output.hard_risk_reasons)

    def test_auditor_blocks_projected_total_margin_above_hard_cap(self):
        from agents.decision_team.auditor import audit_futures_recommendation

        contract = _final_action_contract(current_lots=0, target_lots=1)
        contract["target_margin_ratio_estimate"] = 0.03
        recommendation = {
            "id": "rec-margin-cap",
            "config_id": "cfg",
            "source_type": "strategy",
            "underlying_code": "BU",
            "trading_date": "2025-03-03",
            "effective_trade_date": "2025-03-03",
            "signal_snapshot": {"final_action_contract": contract},
        }
        output = audit_futures_recommendation(
            recommendation=recommendation,
            hard_risk_config={"max_total_margin_ratio": 0.20},
            account_state={
                "account_equity": 1_000_000.0,
                "margin_used": 190_000.0,
                "margin_ratio": 0.19,
                "risk_status": "NORMAL",
            },
            position_state={
                "current_lots": 0,
                "contract_code": None,
                "margin_used": 0.0,
            },
            contract_state={
                "contract_code": "BU2506",
                "underlying_code": "BU",
                "as_of_date": "2025-02-28",
                "source": "pandaai_main_contract_quote",
            },
            data_quality={"status": "clean", "flags": []},
        )
        self.assertEqual(output.audit_verdict, "block")
        self.assertIn("margin_hard_cap_exceeded", output.hard_risk_reasons)
        self.assertAlmostEqual(
            output.audit_payload["contract_summary"]["projected_total_margin_ratio"],
            0.22,
        )

    def test_trader_appends_to_complete_auditor_payload(self):
        from util.futures_audit import build_audit_payload

        original = _full_auditor_payload()
        snapshot = {
            "final_action_contract": _final_action_contract(),
            "auditor": {
                "producer": "auditor",
                "audit_status": "approved",
                "audit_verdict": "approve",
                "audit_reason_codes": [],
                "audited_at": original["audited_at"],
            },
            "execution_translation": {"translated_orders": []},
            "execution_result": {
                "outcome": "not_triggered",
                "status": "skipped",
                "transaction_count": 0,
                "actual_transactions": [],
                "no_trade_reason": "intraday_trigger_not_met",
            },
            "phase2_execution": {"status": "completed"},
        }
        payload = build_audit_payload(
            snapshot,
            original_audit_payload=original,
        )
        for key in (
            "producer",
            "source",
            "boundary",
            "hard_risk_reasons",
            "soft_risk_reasons",
            "contract_summary",
            "semantic_state",
        ):
            self.assertEqual(payload[key], original[key])
        self.assertIn("trade_contract_audit", payload)
        self.assertEqual(payload["execution_result"]["transaction_count"], 0)

    def test_research_writer_rejects_raw_prompt_and_response(self):
        from tools.agent_tools.research import research_memory_writers

        cursor = Mock()
        with self.assertRaisesRegex(ValueError, "researcher_raw_llm_content_forbidden"):
            research_memory_writers.insert_researcher_llm_note(
                cursor,
                note_id="note-1",
                config_id="cfg",
                trading_date="2025-03-03",
                evidence_pack_id="pack-1",
                ticker="BU",
                raw_prompt="secret prompt",
                raw_response="secret response",
                created_at="2025-03-03T00:00:00+00:00",
                payload_json="{}",
                raw_prompt_artifact_path=None,
                raw_prompt_sha256=None,
                raw_prompt_size=0,
                raw_prompt_summary_json=None,
                raw_response_artifact_path=None,
                raw_response_sha256=None,
                raw_response_size=0,
                raw_response_summary_json=None,
                payload_artifact_path=None,
                payload_sha256=None,
                payload_size=2,
                payload_summary_json=None,
            )
        cursor.execute.assert_not_called()

    def test_transaction_contract_does_not_expose_prompt_field(self):
        self.assertNotIn("llm_prompt", FuturesTransaction.model_fields)

    def test_researcher_trace_requires_real_signal_rows_and_allows_no_transaction(self):
        from tools.agent_tools.research.research_learning import (
            _validate_researcher_input_facts,
        )

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE portfolio (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                trading_date TEXT NOT NULL
            );
            CREATE TABLE signal (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                analyst TEXT NOT NULL,
                artifact_json TEXT,
                artifact_json_artifact_path TEXT,
                artifact_json_sha256 TEXT
            );
            """
        )
        cursor.execute(
            "INSERT INTO portfolio(id, config_id, trading_date) VALUES (?, ?, ?)",
            ("portfolio-1", "cfg", "2025-02-28"),
        )
        for index, analyst in enumerate(ANALYSTS, start=1):
            artifact = json.dumps(
                {
                    "metadata": {
                        "action_evidence_contract": _complete_aec(analyst),
                    },
                    "signal_artifact_metadata": {
                        "contract_version": "agentquant.signal_artifact.v1",
                    },
                }
            )
            cursor.execute(
                "INSERT INTO signal(id, portfolio_id, ticker, analyst, artifact_json) VALUES (?, ?, ?, ?, ?)",
                (f"signal-{index}", "portfolio-1", "BU", analyst, artifact),
            )
        recommendation = {
            "id": "rec-1",
            "config_id": "cfg",
            "reference_portfolio_id": "portfolio-1",
            "source_type": "strategy",
            "underlying_code": "BU",
            "trading_date": "2025-03-03",
            "effective_trade_date": "2025-03-03",
            "audit_payload": _full_auditor_payload(),
            "signal_snapshot": {
                "signal_collection_contract": _scc(data_available=True),
                "final_action_contract": _final_action_contract(
                    current_lots=0,
                    target_lots=0,
                ),
                "execution_result": {
                    "outcome": "not_triggered",
                    "status": "skipped",
                    "transaction_count": 0,
                    "actual_transactions": [],
                    "no_trade_reason": "intraday_trigger_not_met",
                },
            },
        }
        _validate_researcher_input_facts(
            cursor=cursor,
            config_id="cfg",
            trading_date="2025-03-03",
            previous_trading_dates_by_ticker={"BU": "2025-02-28"},
            settlement_row={"trading_date": "2025-03-03", "daily_pnl": 0.0},
            strategy_recommendations=[recommendation],
            transactions_by_recommendation={},
        )
        cursor.execute("DELETE FROM signal WHERE id = ?", ("signal-2",))
        with self.assertRaisesRegex(ValueError, "signal_record_not_found:BU:fundamental"):
            _validate_researcher_input_facts(
                cursor=cursor,
                config_id="cfg",
                trading_date="2025-03-03",
                previous_trading_dates_by_ticker={"BU": "2025-02-28"},
                settlement_row={"trading_date": "2025-03-03", "daily_pnl": 0.0},
                strategy_recommendations=[recommendation],
                transactions_by_recommendation={},
            )
        connection.close()

    def test_researcher_accepts_prev_reference_portfolio_for_t_signal_contracts(self):
        from tools.agent_tools.research.research_learning import (
            _validate_researcher_input_facts,
        )

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE portfolio (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                trading_date TEXT NOT NULL
            );
            CREATE TABLE signal (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                analyst TEXT NOT NULL,
                artifact_json TEXT,
                artifact_json_artifact_path TEXT,
                artifact_json_sha256 TEXT
            );
            """
        )
        cursor.execute(
            "INSERT INTO portfolio(id, config_id, trading_date) VALUES (?, ?, ?)",
            ("portfolio-prev", "cfg", "2025-02-28"),
        )
        for index, analyst in enumerate(ANALYSTS, start=1):
            artifact = {
                "metadata": {
                    "action_evidence_contract": _complete_aec(analyst),
                },
                "signal_artifact_metadata": {
                    "contract_version": "agentquant.signal_artifact.v1",
                },
            }
            cursor.execute(
                """
                INSERT INTO signal(
                    id, portfolio_id, ticker, analyst, artifact_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"signal-{index}",
                    "portfolio-prev",
                    "BU",
                    analyst,
                    json.dumps(artifact),
                ),
            )
        recommendation = {
            "id": "rec-t",
            "config_id": "cfg",
            "reference_portfolio_id": "portfolio-prev",
            "source_type": "strategy",
            "underlying_code": "BU",
            "trading_date": "2025-03-03",
            "effective_trade_date": "2025-03-03",
            "audit_payload": _full_auditor_payload("rec-t"),
            "signal_snapshot": {
                "signal_collection_contract": _scc(data_available=True),
                "final_action_contract": _final_action_contract(
                    current_lots=0,
                    target_lots=0,
                ),
                "execution_result": {
                    "outcome": "not_triggered",
                    "status": "skipped",
                    "transaction_count": 0,
                    "actual_transactions": [],
                    "no_trade_reason": "intraday_trigger_not_met",
                },
            },
        }

        _validate_researcher_input_facts(
            cursor=cursor,
            config_id="cfg",
            trading_date="2025-03-03",
            previous_trading_dates_by_ticker={"BU": "2025-02-28"},
            settlement_row={"trading_date": "2025-03-03", "daily_pnl": 0.0},
            strategy_recommendations=[recommendation],
            transactions_by_recommendation={},
        )
        connection.close()

    def test_researcher_rejects_persisted_aec_with_wrong_logical_t_date(self):
        from tools.agent_tools.research.research_learning import (
            _validate_researcher_input_facts,
        )

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE portfolio (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                trading_date TEXT NOT NULL
            );
            CREATE TABLE signal (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                analyst TEXT NOT NULL,
                artifact_json TEXT,
                artifact_json_artifact_path TEXT,
                artifact_json_sha256 TEXT
            );
            """
        )
        cursor.execute(
            "INSERT INTO portfolio(id, config_id, trading_date) VALUES (?, ?, ?)",
            ("portfolio-prev", "cfg", "2025-02-28"),
        )
        for index, analyst in enumerate(ANALYSTS, start=1):
            aec = _complete_aec(analyst)
            if analyst == "technical":
                aec["data_usage_summary"]["trading_date"] = "2025-03-04"
            artifact = {
                "metadata": {"action_evidence_contract": aec},
                "signal_artifact_metadata": {
                    "contract_version": "agentquant.signal_artifact.v1",
                },
            }
            cursor.execute(
                "INSERT INTO signal(id, portfolio_id, ticker, analyst, artifact_json) VALUES (?, ?, ?, ?, ?)",
                (
                    f"signal-{index}",
                    "portfolio-prev",
                    "BU",
                    analyst,
                    json.dumps(artifact),
                ),
            )
        recommendation = {
            "id": "rec-t",
            "config_id": "cfg",
            "reference_portfolio_id": "portfolio-prev",
            "source_type": "strategy",
            "underlying_code": "BU",
            "trading_date": "2025-03-03",
            "effective_trade_date": "2025-03-03",
            "audit_payload": _full_auditor_payload("rec-t"),
            "signal_snapshot": {
                "signal_collection_contract": _scc(data_available=True),
                "final_action_contract": _final_action_contract(
                    current_lots=0,
                    target_lots=0,
                ),
                "execution_result": {
                    "outcome": "not_triggered",
                    "status": "skipped",
                    "transaction_count": 0,
                    "actual_transactions": [],
                    "no_trade_reason": "intraday_trigger_not_met",
                },
            },
        }

        with self.assertRaisesRegex(
            ValueError,
            "signal_record_aec_date_mismatch:BU:technical",
        ):
            _validate_researcher_input_facts(
                cursor=cursor,
                config_id="cfg",
                trading_date="2025-03-03",
                previous_trading_dates_by_ticker={"BU": "2025-02-28"},
                settlement_row={"trading_date": "2025-03-03", "daily_pnl": 0.0},
                strategy_recommendations=[recommendation],
                transactions_by_recommendation={},
            )
        connection.close()

    def test_scc_from_three_persisted_neutral_aecs_remains_valid(self):
        signals = [
            _signal(analyst, f"signal-{index}", data_available=False)
            for index, analyst in enumerate(ANALYSTS, start=1)
        ]
        contract = build_signal_collection_contract(
            ticker="BU",
            trading_date="2025-03-03",
            analyst_signals=signals,
            enabled_analysts=ANALYSTS,
        )
        validated = validate_signal_collection_contract(
            contract,
            ticker="BU",
            trading_date="2025-03-03",
            enabled_analysts=ANALYSTS,
            analyst_signals=signals,
            require_signal_record_ids=True,
        )
        self.assertEqual(len(validated["source_contracts"]), 3)
        self.assertEqual(validated["dominant_side"], "flat")


if __name__ == "__main__":
    unittest.main()
