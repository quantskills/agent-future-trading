import sys
import tempfile
import unittest
import ast
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.control_team.protocol_governor import ProtocolGovernor, protocol_governor_agent
from llm.prompt import (
    ACTION_VALUE_USAGE_BOUNDARY,
    CONTROL_GOVERNANCE_OUTPUT_BOUNDARY,
    build_futures_commodity_news_prompt,
    build_futures_fundamental_prompt,
    build_futures_technical_prompt,
    build_researcher_causal_review_prompt,
    build_researcher_exploratory_prompt,
)
from tools.agent_tools.control.agent_cards import build_default_agent_cards
from tools.agent_tools.control.artifact_lineage import build_protocol_artifact_header
from tools.agent_tools.control.cost_budget_audit import CostBudgetLimits, assert_cost_audit_is_non_trading


class ProtocolGovernorRegressionTest(unittest.TestCase):
    def setUp(self):
        self.governor = ProtocolGovernor()

    def test_protocol_governor_has_no_trade_authority(self):
        cards = build_default_agent_cards()
        governor_card = cards["protocol_governor"]
        self.assertFalse(governor_card.may_call_llm)
        self.assertFalse(governor_card.may_create_trade_authority)
        self.assertFalse(governor_card.may_modify_lots_or_margin)
        self.assertFalse(governor_card.may_execute_orders)
        self.assertFalse(governor_card.may_write_settlement)

        result = self.governor.validate_capability_cards()
        self.assertTrue(result.ok, result.to_dict())
        tool_policy_result = self.governor.validate_tool_policy()
        self.assertTrue(tool_policy_result.ok, tool_policy_result.to_dict())

    def test_non_llm_agents_do_not_import_or_call_llm(self):
        forbidden_agents = {
            "signal_collector": SRC_ROOT / "agents" / "decision_team" / "signal_collector.py",
            "portfolio_manager": SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py",
            "auditor": SRC_ROOT / "agents" / "decision_team" / "auditor.py",
            "trader": SRC_ROOT / "agents" / "execution_team" / "trader.py",
            "accountant": SRC_ROOT / "agents" / "execution_team" / "accountant.py",
            "reviewer": SRC_ROOT / "agents" / "research_team" / "reviewer.py",
        }
        llm_call_names = {"agent_call", "get_model", "with_structured_output"}
        for agent_name, path in forbidden_agents.items():
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                    self.assertFalse(
                        any(name == "llm" or name.startswith("llm.") for name in imported),
                        f"{agent_name} imports LLM module: {imported}",
                    )
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertFalse(
                        module == "llm" or module.startswith("llm."),
                        f"{agent_name} imports from LLM module: {module}",
                    )
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                    self.assertNotIn(name, llm_call_names, f"{agent_name} calls LLM entrypoint: {name}")

    def test_reviewer_does_not_dispatch_researcher_learning(self):
        reviewer_path = SRC_ROOT / "tools" / "agent_tools" / "research" / "phase4_review.py"
        tree = ast.parse(reviewer_path.read_text(encoding="utf-8-sig"), filename=str(reviewer_path))
        forbidden_names = {
            "researcher_agent",
            "apply_researcher_learning",
            "run_researcher_causal_review",
            "write_exploratory_hypotheses",
            "agent_call",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names = {alias.name for alias in node.names}
                self.assertFalse(
                    forbidden_names & imported_names,
                    f"phase4 reviewer imports researcher/LLM callable: {forbidden_names & imported_names}",
                )
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                self.assertNotIn(name, forbidden_names, f"phase4 reviewer calls forbidden callable: {name}")

    def test_reviewer_phase4_main_does_not_write_research_memory(self):
        reviewer_path = SRC_ROOT / "tools" / "agent_tools" / "research" / "phase4_review.py"
        tree = ast.parse(reviewer_path.read_text(encoding="utf-8-sig"), filename=str(reviewer_path))
        reviewer_text = reviewer_path.read_text(encoding="utf-8-sig")
        self.assertNotIn("memory_config=", reviewer_text)
        self.assertNotIn("retention_config=", reviewer_text)
        run_phase4 = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run_phase4_review"
        )
        forbidden_calls = {
            "_write_opportunity_ranking_learning_events",
            "_write_signal_context_history",
            "_write_template_and_analyst_learning",
            "_write_trade_episode_memory",
            "_write_research_position_feedback",
            "_write_loss_template_observation_research",
            "_write_fast_loss_sentinel_state",
            "_write_no_trade_opportunity_memory",
            "_write_missed_alpha_accountability_state",
            "_write_validated_causal_policy_rules",
            "_write_learning_mechanism_policy_state",
            "_write_learned_vs_unlearned_policy_state",
            "_write_strategy_memory_history",
            "_write_adaptive_policy_state",
            "_write_tail_loss_sentinel_state",
            "_write_alpha_promotion_state",
            "_write_contextual_rule_calibration_state",
            "_write_config_overlay",
            "_write_neutral_accountability_state",
            "_write_neutral_accountability_digests",
            "_write_capital_deployment_state",
            "_write_provisional_policy_state",
            "_export_template_prior",
        }
        allowed_report_calls = {"_write_daily_transaction_report"}
        for node in ast.walk(run_phase4):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            self.assertNotIn(name, forbidden_calls, f"Phase4 reviewer must not call research writer {name}")
            if name.startswith("_write_"):
                self.assertIn(
                    name,
                    allowed_report_calls,
                    f"Phase4 reviewer may only write daily transaction reports, not {name}",
                )

    def test_phase4_review_module_does_not_define_research_memory_writers(self):
        reviewer_path = SRC_ROOT / "tools" / "agent_tools" / "research" / "phase4_review.py"
        writer_path = SRC_ROOT / "tools" / "agent_tools" / "research" / "research_memory_writers.py"
        reviewer_text = reviewer_path.read_text(encoding="utf-8-sig")
        writer_text = writer_path.read_text(encoding="utf-8-sig")
        tree = ast.parse(reviewer_text, filename=str(reviewer_path))
        definitions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        forbidden_definitions = {
            "_insert_learning_event",
            "_ensure_research_learning_schema",
            "_deactivate_adaptive_policy_state",
            "_deactivate_adaptive_policy_state_from_counterfactual_reversal",
            "_write_opportunity_ranking_learning_events",
            "_write_signal_context_history",
            "_write_template_and_analyst_learning",
            "_write_trade_episode_memory",
            "_write_research_position_feedback",
            "_write_loss_template_observation_research",
            "_write_fast_loss_sentinel_state",
            "_write_no_trade_opportunity_memory",
            "_backfill_no_trade_opportunity_counterfactual_results",
            "_write_missed_alpha_accountability_state",
            "_write_validated_causal_policy_rules",
            "_write_learning_mechanism_policy_state",
            "_write_learned_vs_unlearned_policy_state",
            "_write_strategy_memory_history",
            "_write_adaptive_policy_state",
            "_write_tail_loss_sentinel_state",
            "_write_alpha_promotion_state",
            "_insert_contextual_rule_calibration",
            "_write_contextual_rule_calibration_state",
            "_write_config_overlay",
            "_write_neutral_accountability_state",
            "_backfill_neutral_forward_counterfactual_tracking",
            "_write_neutral_accountability_digests",
            "_write_capital_deployment_state",
            "_write_provisional_policy_state",
            "_export_template_prior",
        }
        self.assertFalse(
            definitions & forbidden_definitions,
            f"phase4_review must not define research writer functions: {definitions & forbidden_definitions}",
        )

        forbidden_sql = (
            "INSERT INTO learning_event_log",
            "INSERT INTO adaptive_policy_state",
            "UPDATE adaptive_policy_state",
            "INSERT INTO strategy_memory",
            "INSERT INTO analyst_learning_digest",
            "INSERT INTO config_learning_overlay",
            "INSERT INTO capital_deployment_state",
            "INSERT INTO provisional_policy_state",
            "INSERT INTO signal_context_history",
            "INSERT INTO trade_episode_memory",
            "INSERT INTO no_trade_opportunity_memory",
        )
        for token in forbidden_sql:
            self.assertNotIn(token, reviewer_text, f"phase4_review must not write research table via {token}")

        self.assertIn("def _insert_learning_event", writer_text)
        self.assertIn("def _write_adaptive_policy_state", writer_text)
        self.assertIn("def _write_capital_deployment_state", writer_text)

    def test_phase_completion_has_no_learning_side_effects(self):
        db_path = SRC_ROOT / "database" / "sqlite_helper.py"
        tree = ast.parse(db_path.read_text(encoding="utf-8-sig"), filename=str(db_path))
        complete_phase = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SQLiteDB"
        )
        complete_phase_func = next(
            node
            for node in complete_phase.body
            if isinstance(node, ast.FunctionDef) and node.name == "complete_trading_day_phase"
        )
        forbidden_calls = {
            "_refresh_strategy_memory_with_cursor",
            "_cleanup_learning_retention_with_cursor",
            "refresh_strategy_memory",
        }
        for node in ast.walk(complete_phase_func):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            self.assertNotIn(name, forbidden_calls, f"phase completion must not call learning side effect {name}")

    def test_fixed_workflow_does_not_enable_legacy_llm_planner(self):
        workflow_path = SRC_ROOT / "graph" / "workflow.py"
        tree = ast.parse(workflow_path.read_text(encoding="utf-8-sig"), filename=str(workflow_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported_names = {alias.name for alias in node.names}
                self.assertFalse(
                    module == "agents.control_team.planner" or "planner_agent" in imported_names,
                    "fixed workflow must not import the legacy LLM planner",
                )
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                self.assertNotEqual(name, "planner_agent", "fixed workflow must not call the legacy LLM planner")

        text = workflow_path.read_text(encoding="utf-8-sig")
        self.assertIn("planner_mode is disabled by the fixed multi-agent workflow", text)
        planner_path = SRC_ROOT / "agents" / "control_team" / "planner.py"
        self.assertTrue(planner_path.exists(), "legacy planner is retained as a dormant development component")

    def test_pm_research_memory_uses_single_decision_tool_entrypoint(self):
        pm_path = SRC_ROOT / "agents" / "decision_team" / "portfolio_manager.py"
        text = pm_path.read_text(encoding="utf-8-sig")
        forbidden = (
            "get_futures_transaction_memory",
            "build_learning_context",
            "get_similar_alpha_setup_action_values",
            "get_strategy_memory",
            "get_adaptive_policy_state",
            "get_provisional_policy_state",
        )
        for token in forbidden:
            self.assertNotIn(token, text, f"PM must not directly read research memory via {token}")
        self.assertIn("retrieve_pm_memory(", text)

    def test_auditor_does_not_consume_research_memory_fields(self):
        auditor_path = SRC_ROOT / "agents" / "decision_team" / "auditor.py"
        text = auditor_path.read_text(encoding="utf-8-sig")
        forbidden = (
            "strategy_memory:",
            "adaptive_policy_state:",
            "payload.strategy_memory",
            "payload.adaptive_policy_state",
            "_evaluate_strategy_memory_rule",
            "strategy_memory_weak_block",
            "strategy_memory_watchlist_cap",
            "filter_adaptive_policy_state_for_pm",
        )
        for token in forbidden:
            self.assertNotIn(token, text, f"auditor must not consume research memory token {token}")
        self.assertIn("auditor_does_not_consume_research_records", text)

    def test_trader_reads_execution_profile_only_through_final_contract(self):
        cards = build_default_agent_cards()
        trader_card = cards["trader"]

        self.assertIn("final_action_contract", trader_card.reads)
        self.assertIn("audit_verdict", trader_card.reads)
        self.assertIn("intraday_market_data", trader_card.reads)
        self.assertIn("portfolio_margin_state", trader_card.reads)
        self.assertIn("forced_risk_operational_recommendation", trader_card.outputs)
        self.assertNotIn("execution_action_value", trader_card.reads)
        self.assertFalse(trader_card.may_create_trade_authority)
        self.assertFalse(trader_card.may_modify_lots_or_margin)
        self.assertTrue(trader_card.may_execute_orders)

    def test_trader_execution_layer_has_no_research_memory_entrypoint(self):
        trader_text = (SRC_ROOT / "agents" / "execution_team" / "trader.py").read_text(encoding="utf-8-sig")
        intraday_text = (SRC_ROOT / "tools" / "agent_tools" / "execution" / "intraday_execution.py").read_text(encoding="utf-8-sig")
        forbidden = (
            "strategy_memory=",
            "adaptive_policy_state=",
            "strategy_memory:",
            "adaptive_policy_state:",
            "_strategy_memory_from_recommendation",
            "_adaptive_policy_state_from_recommendation",
            "allow_confirmed_memory_vwap_fallback",
            "intraday_confirmed_memory_vwap_fallback",
            "confirmed_memory_min_",
            "apply_intraday_contextual_calibration",
        )
        for token in forbidden:
            self.assertNotIn(token, trader_text, f"trader must not expose research-memory execution token {token}")
            self.assertNotIn(token, intraday_text, f"intraday execution must not expose research-memory execution token {token}")

    def test_dev_config_keeps_control_governance_audit_only(self):
        config_path = SRC_ROOT / "config" / "dev.yaml"
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        self.assertFalse(cfg.get("planner_mode"), "legacy LLM planner must stay disabled")
        governance = cfg.get("control_governance") or {}
        self.assertTrue(governance.get("enabled"))

        governor = governance.get("protocol_governor") or {}
        self.assertEqual(governor.get("role"), "audit_sidecar_only")
        self.assertFalse(governor.get("llm_enabled"))
        self.assertFalse(governor.get("may_create_trade_authority"))
        self.assertFalse(governor.get("may_modify_lots_or_margin"))
        self.assertFalse(governor.get("may_execute_orders"))
        self.assertFalse(governor.get("may_write_settlement"))
        self.assertEqual(governor.get("final_trade_truth"), "final_action_contract")

        cost_audit = governance.get("cost_budget_audit") or {}
        self.assertEqual(cost_audit.get("role"), "resource_observation_only")
        self.assertFalse(cost_audit.get("can_change_strategy"))
        self.assertFalse(cost_audit.get("can_emit_trade_action_fields"))

        tool_policy = governance.get("tool_access_policy") or {}
        self.assertEqual(tool_policy.get("violation_effect"), "protocol_error_only")
        self.assertEqual(tool_policy.get("protocol_governor_allowed_namespaces"), ["control", "database"])

        budget = cfg.get("position_budget_policy") or {}
        self.assertEqual(float(budget.get("probe_margin_ratio")), 0.008)
        self.assertEqual(float(budget.get("probe_margin_max_ratio")), 0.015)
        self.assertEqual(float(cfg.get("max_total_margin_ratio")), 0.20)

    def test_central_prompt_boundary_keeps_control_governance_non_trading(self):
        self.assertIn("CONTROL-GOVERNANCE BOUNDARY", CONTROL_GOVERNANCE_OUTPUT_BOUNDARY)
        self.assertIn("audit metadata only", CONTROL_GOVERNANCE_OUTPUT_BOUNDARY)
        self.assertIn("Do not transform control-governance metadata into authority_type", CONTROL_GOVERNANCE_OUTPUT_BOUNDARY)

        researcher_review = build_researcher_causal_review_prompt("{}")
        researcher_exploration = build_researcher_exploratory_prompt(trading_date="2025-03-03", episodes_json="[]")

        self.assertIn("Control-governance metadata can support chain-health audit only", researcher_review)
        self.assertIn("chain-health audit inputs only", researcher_exploration)

    def test_central_prompt_boundary_keeps_action_value_usage_scoped_by_agent(self):
        self.assertIn("ACTION-VALUE USAGE BOUNDARY", ACTION_VALUE_USAGE_BOUNDARY)
        self.assertIn("consumer_scope", ACTION_VALUE_USAGE_BOUNDARY)
        self.assertIn("Analysts may read only signal_calibration", ACTION_VALUE_USAGE_BOUNDARY)
        self.assertIn("execution action-value may inform PM's execution_profile", ACTION_VALUE_USAGE_BOUNDARY)
        self.assertIn("consumer_scope=trader_execution_learning", ACTION_VALUE_USAGE_BOUNDARY)
        self.assertIn("only final_action_contract execution fields / execution_profile", ACTION_VALUE_USAGE_BOUNDARY)
        self.assertIn("must not directly change direction", ACTION_VALUE_USAGE_BOUNDARY)
        self.assertIn("target_lots, margin_ratio, or authority", ACTION_VALUE_USAGE_BOUNDARY)

        researcher_review = build_researcher_causal_review_prompt("{}")

        self.assertIn("ACTION-VALUE USAGE BOUNDARY", researcher_review)
        self.assertIn("signal_calibration", researcher_review)
        self.assertIn("exit/reduce action-value", researcher_review)
        self.assertIn("execution action-value", researcher_review)

        self.assertIn("open rewards evaluate the full episode result", researcher_review)
        self.assertIn("PM via matching open/hold/exit/execution lane", researcher_review)
        self.assertIn("Trader only through final_action_contract execution fields", researcher_review)

    def test_enabled_analyst_prompts_request_learning_explainability_summaries(self):
        technical_prompt = build_futures_technical_prompt(
            ticker="RB",
            signal_results_compact={"trend": "Bullish", "macd": "Bullish", "adx": "Bullish"},
            technical_summary="tradeability=high; market_regime=trend",
            features={"volatility": 0.02, "trend_strength": 28, "price_range": 0.05, "volume_ratio": 1.2},
            llm_path="cloud_only",
        )
        fundamental_prompt = build_futures_fundamental_prompt(
            ticker="RB",
            fundamentals="inventory down; profit improving",
            learning_context_text="Reviewer Learning Context",
        )
        news_prompt = build_futures_commodity_news_prompt(
            ticker="RB",
            instrument_context="RB rebar futures",
            news=["policy demand catalyst"],
            news_summary="fresh policy news",
            llm_path="cloud_only",
            learning_context_text="Reviewer Learning Context",
        )

        for prompt in (technical_prompt, fundamental_prompt, news_prompt):
            self.assertIn("learning_impact_summary", prompt)
            self.assertIn("historical_support", prompt)
            self.assertIn("historical_contradiction", prompt)
            self.assertIn("current_evidence_confirmed", prompt)
            self.assertIn("current_evidence_missing", prompt)
            self.assertIn("opportunity_state_reason", prompt)
            self.assertIn("Do not include lots, margin, final_action", prompt)

        self.assertIn("factor_calibration_summary", fundamental_prompt)
        self.assertIn("effective_factors", fundamental_prompt)
        self.assertIn("stale_or_conflicting_factors", fundamental_prompt)
        self.assertIn("factors_requiring_price_confirmation", fundamental_prompt)
        self.assertIn("event_calibration_summary", news_prompt)
        self.assertIn("effective_catalysts", news_prompt)
        self.assertIn("background_noise", news_prompt)
        self.assertIn("price_volume_confirmation_required", news_prompt)

    def test_learning_policy_catalog_documents_action_value_usage_boundary(self):
        config_path = SRC_ROOT / "config" / "learning_policy_catalog.yaml"
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        policy = cfg.get("learning_gatekeeping_policy") or {}

        self.assertEqual(policy.get("research_output_contract_version"), "agentquant.research_action_value.v1")
        self.assertEqual(policy.get("analyst_allowed_action_value_view"), "signal_calibration_only")
        self.assertEqual(policy.get("pm_allowed_consumer_scope"), "pm_learning")
        self.assertEqual(policy.get("pm_allowed_action_value_lanes"), ["open", "hold", "exit", "execution"])
        self.assertEqual(policy.get("analyst_allowed_consumer_scope"), "analyst_calibration")
        self.assertFalse(policy.get("trader_direct_research_consumption_allowed"))
        self.assertNotIn("trader_allowed_consumer_scope", policy)
        self.assertNotIn("trader_allowed_action_value_lanes", policy)
        self.assertEqual(
            policy.get("pm_action_value_retrieval_order"),
            ["exact_state", "same_ticker_side_horizon", "same_ticker_side", "weak_prior"],
        )
        self.assertEqual(policy.get("similar_sql_rag_role"), "weak_prior_not_trade_authority")
        self.assertIn("exit_as_open_amplifier", policy.get("forbidden_cross_action_uses") or [])
        self.assertIn("execution_changes_lots", policy.get("forbidden_cross_action_uses") or [])

    def test_agent_cards_keep_business_boundaries_explicit(self):
        cards = build_default_agent_cards()
        self.assertTrue(cards["portfolio_manager"].may_create_trade_authority)
        self.assertTrue(cards["portfolio_manager"].may_modify_lots_or_margin)
        self.assertFalse(cards["portfolio_manager"].may_execute_orders)
        self.assertTrue(cards["trader"].may_execute_orders)
        self.assertFalse(cards["trader"].may_create_trade_authority)
        self.assertTrue(cards["researcher"].may_write_future_learning)
        self.assertFalse(cards["researcher"].may_create_trade_authority)
        self.assertFalse(cards["technical"].may_create_trade_authority)

    def test_task_lifecycle_validates_order_and_task_scope(self):
        events = [
            self.governor.create_task_event(
                trading_date="2025-03-03",
                ticker="BU",
                config_id="cfg",
                phase="data_ready",
                agent_name="data",
            ),
            self.governor.create_task_event(
                trading_date="2025-03-03",
                ticker="BU",
                config_id="cfg",
                phase="analyst_done",
                agent_name="technical",
            ),
            self.governor.create_task_event(
                trading_date="2025-03-03",
                ticker="BU",
                config_id="cfg",
                phase="final_action_ready",
                agent_name="portfolio_manager",
            ),
            self.governor.create_task_event(
                trading_date="2025-03-03",
                ticker="BU",
                config_id="cfg",
                phase="audited",
                agent_name="auditor",
            ),
            self.governor.create_task_event(
                trading_date="2025-03-03",
                ticker="BU",
                config_id="cfg",
                phase="skipped",
                agent_name="trader",
            ),
            self.governor.create_task_event(
                trading_date="2025-03-03",
                ticker="BU",
                config_id="cfg",
                phase="settled",
                agent_name="accountant",
            ),
        ]
        self.assertTrue(self.governor.validate_task_lifecycle(events).ok)

        bad_events = [events[2], events[1]]
        result = self.governor.validate_task_lifecycle(bad_events)
        self.assertFalse(result.ok)
        self.assertIn("phase_regression:analyst_done", result.errors)

    def test_protocol_artifact_requires_task_context_and_phase(self):
        header = build_protocol_artifact_header(
            task_id="cfg:2025-03-03:BU",
            context_id="cfg:2025-03-03",
            phase="pm_candidate",
            contract_version="agentquant.protocol.test.v1",
            agent_name="portfolio_manager",
            trading_date="2025-03-03",
            ticker="BU",
            config_id="cfg",
            source_artifacts=["analyst:technical:BU"],
        )
        artifact = {"header": header, "payload": {"final_action": "hold"}}
        self.assertTrue(self.governor.validate_artifact_lineage(artifact).ok)

        broken = deepcopy(artifact)
        broken["header"].pop("task_id")
        result = self.governor.validate_artifact_lineage(broken)
        self.assertFalse(result.ok)
        self.assertIn("missing_protocol_task_id", result.errors)

    def test_memory_quality_controls_allowed_uses_without_new_trade_logic(self):
        exact = self.governor.classify_memory_quality(
            {"amplification_scope_quality": "exact_real_state", "reward_source": "real_trade"}
        )
        self.assertTrue(exact["allowed_uses"]["can_support_real_budget_entry"])
        self.assertTrue(exact["allowed_uses"]["can_support_scale"])

        similar = self.governor.classify_memory_quality({"amplification_scope_quality": "similar_sql_prior"})
        self.assertFalse(similar["allowed_uses"]["can_support_real_budget_entry"])
        self.assertTrue(similar["allowed_uses"]["can_support_probe"])

        counterfactual = self.governor.classify_memory_quality({"counterfactual_prior_only": True})
        self.assertFalse(counterfactual["allowed_uses"]["can_support_probe"])
        self.assertTrue(counterfactual["allowed_uses"]["can_inform_analysis"])

        preference_only = self.governor.classify_memory_quality({"action_preference": "positive_candidate_open"})
        self.assertEqual(preference_only["memory_quality"], "unqualified")
        self.assertFalse(preference_only["allowed_uses"]["can_support_real_budget_entry"])

    def test_action_preference_audit_is_observational(self):
        pm_snapshot = {
            "final_action_contract": {
                "authority_type": "exploration_probe",
                "target_lots": 1,
                "lots_delta": 1,
                "max_allowed_margin_ratio": 0.012,
                "reason_codes": ["positive_candidate_open"],
                "learning_used": ["positive_candidate_open"],
            }
        }
        original = deepcopy(pm_snapshot)
        report = self.governor.audit_action_preference_landing(
            research_preferences=[{"action_preference": "positive_candidate_open"}],
            pm_snapshot=pm_snapshot,
            trader_snapshot={"execution_status": "triggered"},
            settlement_snapshot={"pnl": 1200.0},
        )
        self.assertTrue(report["researcher_wrote_preference"])
        self.assertTrue(report["pm_read_preference"])
        self.assertTrue(report["pm_changed_trade_terms"])
        self.assertTrue(report["trader_triggered"])
        self.assertTrue(report["reward_observed"])
        self.assertFalse(report["input_mutated"])
        self.assertEqual(pm_snapshot, original)

    def test_preflight_reports_local_health_without_real_api_call(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            required = tmp / "config.yaml"
            required.write_text("ok: true", encoding="utf-8")
            fake_python = tmp / "deepfund" / "python.exe"
            fake_python.parent.mkdir()
            fake_python.write_text("", encoding="utf-8")

            result = self.governor.run_preflight(
                repo_root=SRC_ROOT.parent,
                writable_dirs=[tmp],
                required_files=[required],
                deepfund_python=fake_python,
            )
            self.assertTrue(result.ok, result.to_dict())

            missing = self.governor.run_preflight(required_files=[tmp / "missing.yaml"])
            self.assertFalse(missing.ok)
            self.assertIn(f"required_file_missing:{tmp / 'missing.yaml'}", missing.errors)

    def test_preflight_can_fail_fast_on_live_llm_auth_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_python = tmp / "deepfund" / "python.exe"
            fake_python.parent.mkdir()
            fake_python.write_text("", encoding="utf-8")
            llm_config = {
                "provider": "TQXAI",
                "model": "claude-opus-4-6-1",
                "structured_output_method": "json_mode",
                "tqxai": {"api_key_env": "TQX_LLM_API_KEY", "api_key_env_fallbacks": []},
            }
            with patch.dict("os.environ", {"TQX_LLM_API_KEY": "bad-token"}, clear=False), patch(
                "tools.agent_tools.control.preflight.get_model",
                side_effect=RuntimeError("Error code: 401 - Invalid token"),
            ):
                result = self.governor.run_preflight(
                    repo_root=SRC_ROOT.parent,
                    writable_dirs=[tmp],
                    deepfund_python=fake_python,
                    llm_config=llm_config,
                    check_llm_auth=True,
                )
        self.assertFalse(result.ok)
        self.assertTrue(any(error.startswith("llm_preflight_failed:TQXAI:claude-opus-4-6-1:auth_error") for error in result.errors))

    def test_preflight_reports_missing_llm_key_before_backtest(self):
        llm_config = {
            "provider": "TQXAI",
            "model": "claude-opus-4-6-1",
            "tqxai": {"api_key_env": "AGENTQUANT_TEST_MISSING_LLM_KEY", "api_key_env_fallbacks": []},
        }
        with patch.dict("os.environ", {}, clear=True):
            result = self.governor.run_preflight(llm_config=llm_config, check_llm_auth=False)
        self.assertFalse(result.ok)
        self.assertIn("llm_api_key_missing:TQXAI:AGENTQUANT_TEST_MISSING_LLM_KEY", result.errors)

    def test_backtest_simulation_parity_detects_mismatch(self):
        backtest = {
            "final_action": "open_short",
            "authority_type": "real_budget_entry",
            "current_lots": 0,
            "target_lots": 2,
            "lots_delta": 2,
            "entry_trigger": "breakout",
        }
        simulation = dict(backtest)
        self.assertTrue(
            self.governor.compare_backtest_simulation_parity(
                backtest_contract=backtest,
                simulation_contract=simulation,
            ).ok
        )
        simulation["entry_trigger"] = "vwap"
        result = self.governor.compare_backtest_simulation_parity(
            backtest_contract=backtest,
            simulation_contract=simulation,
        )
        self.assertFalse(result.ok)
        self.assertIn("parity_mismatch:entry_trigger", result.errors)

    def test_exploration_audit_classifies_probe_purpose(self):
        self.assertEqual(
            self.governor.classify_exploration(
                {"authority_type": "exploration_probe", "reason_codes": ["positive_candidate_open"]}
            ),
            "positive_alpha_promotion",
        )
        self.assertEqual(
            self.governor.classify_exploration(
                {"authority_type": "exploration_probe", "reason_codes": ["tail_loss_protect"]}
            ),
            "negative_setup_revalidation",
        )
        self.assertEqual(
            self.governor.classify_exploration(
                {"authority_type": "watchlist_only", "reason_codes": ["counterfactual_prior"]}
            ),
            "counterfactual_no_trade_observation",
        )

    def test_protocol_governor_agent_report_is_sidecar(self):
        report = protocol_governor_agent(
            memory_payload={"amplification_scope_quality": "partial_real_state"},
            contracts=[{"authority_type": "exploration_probe", "reason_codes": ["new_setup"]}],
            cost_events=[
                {
                    "agent_name": "researcher",
                    "trading_date": "2025-03-03",
                    "ticker": "BU",
                    "setup_type": "trend_breakout",
                    "llm_calls": 1,
                    "reflection_calls": 1,
                    "total_tokens": 1200,
                }
            ],
            tool_events=[
                {
                    "agent_name": "technical",
                    "tool": "tools.agent_tools.analysis.quality.apply_trade_research_contract",
                }
            ],
        )
        self.assertEqual(report["agent_name"], "protocol_governor")
        self.assertTrue(report["capability_validation"]["ok"])
        self.assertTrue(report["tool_policy_validation"]["ok"])
        self.assertEqual(report["memory_quality"]["memory_quality"], "partial_real_state")
        self.assertEqual(report["exploration_summary"]["new_setup_exploration"], 1)
        self.assertTrue(report["cost_budget_audit"]["ok"])
        self.assertEqual(report["cost_budget_audit"]["totals"]["llm_calls"], 1)
        self.assertTrue(report["tool_access_audit"]["ok"])

    def test_cost_budget_audit_warns_without_trade_actions(self):
        report = self.governor.audit_cost_budget(
            [
                {
                    "agent_name": "researcher",
                    "trading_date": "2025-03-03",
                    "ticker": "P",
                    "setup_type": "trend_breakout",
                    "llm_calls": 3,
                    "retry_calls": 1,
                    "reflection_calls": 2,
                    "sql_rag_queries": 4,
                    "total_tokens": 9000,
                },
                {
                    "agent_name": "commodity_news",
                    "trading_date": "2025-03-03",
                    "ticker": "P",
                    "setup_type": "trend_breakout",
                    "llm_calls": 2,
                    "pandaai_calls": 1,
                    "reflection_calls": 1,
                    "total_tokens": 3000,
                },
            ],
            limits=CostBudgetLimits(
                max_llm_calls_per_day=4,
                max_llm_calls_per_agent=2,
                max_reflection_calls_per_scope=2,
                max_total_tokens_per_day=10000,
            ),
        )
        self.assertTrue(report["ok"])
        self.assertIn("llm_calls_per_day_over_budget:5>4", report["warnings"])
        self.assertIn("llm_calls_for_agent:researcher_over_budget:3>2", report["warnings"])
        self.assertIn("reflection_calls_for_scope:2025-03-03:P:trend_breakout_over_budget:3>2", report["warnings"])
        self.assertIn("total_tokens_per_day_over_budget:12000>10000", report["warnings"])
        self.assertEqual(assert_cost_audit_is_non_trading(report), [])
        self.assertNotIn("authority_type", report)
        self.assertNotIn("target_lots", report)

    def test_cost_budget_audit_rejects_trade_action_fields(self):
        report = self.governor.audit_cost_budget(
            [{"agent_name": "researcher", "llm_calls": 1, "authority_type": "no_opportunity"}]
        )
        self.assertFalse(report["ok"])
        self.assertIn("cost_audit_event_contains_trade_action_field", report["errors"])

    def test_tool_access_policy_allows_business_line_tools(self):
        result = self.governor.audit_tool_access(
            [
                {
                    "agent_name": "technical",
                    "tool": "tools.agent_tools.analysis.quality.apply_trade_research_contract",
                },
                {
                    "agent_name": "portfolio_manager",
                    "tool": "tools.agent_tools.decision.capital_deployment_policy.allocate_position",
                },
                {
                    "agent_name": "trader",
                    "tool": "tools.agent_tools.execution.intraday_execution.check_intraday_trigger",
                },
                {
                    "agent_name": "researcher",
                    "tool": "tools.agent_tools.research.alpha_setup.upsert_alpha_setup_action_value",
                },
                {
                    "agent_name": "protocol_governor",
                    "tool": "tools.agent_tools.control.cost_budget_audit.audit_cost_budget",
                },
            ]
        )
        self.assertTrue(result.ok, result.to_dict())

    def test_mechanism_effectiveness_audit_is_protocol_governor_read_only_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.governor.audit_mechanism_effectiveness(
                db_path=Path(tmpdir) / "missing-agentquant.db",
                exp_name="missing-exp",
            )

        report = result.to_dict()
        self.assertTrue(report["ok"])
        self.assertIn("sqlite_missing", "\n".join(report.get("warnings") or []))
        metadata = report.get("metadata") or {}
        self.assertIn("mechanism_effectiveness_only", metadata.get("audit_boundary", ""))
        self.assertIn("action_value_to_pm", metadata.get("checked_chain") or [])
        self.assertNotIn("final_action", report)
        self.assertNotIn("target_lots", report)
        self.assertNotIn("lots_delta", report)

    def test_tool_access_policy_rejects_cross_business_line_drift(self):
        result = self.governor.audit_tool_access(
            [
                {
                    "agent_name": "trader",
                    "tool": "tools.agent_tools.decision.capital_deployment_policy.allocate_position",
                },
                {
                    "agent_name": "protocol_governor",
                    "tool": "tools.agent_tools.execution.intraday_execution.check_intraday_trigger",
                },
            ]
        )
        self.assertFalse(result.ok)
        self.assertIn(
            "tool_access_denied:trader:decision:tools.agent_tools.decision.capital_deployment_policy.allocate_position",
            result.errors,
        )
        self.assertIn(
            "tool_access_denied:protocol_governor:execution:tools.agent_tools.execution.intraday_execution.check_intraday_trigger",
            result.errors,
        )


if __name__ == "__main__":
    unittest.main()




