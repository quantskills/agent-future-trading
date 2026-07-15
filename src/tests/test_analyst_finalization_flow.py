import inspect
import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.analysis_team.commodity_news import _build_no_news_signal
from agents.analysis_team.fundamental import _build_no_fundamental_data_signal
from graph.constants import Signal
from graph.schema import AnalystSignal
from llm.prompt import (
    build_futures_commodity_news_prompt,
    build_futures_fundamental_prompt,
    build_futures_technical_prompt,
)
from tools.agent_tools.analysis.analyst_output_finalization import (
    finalize_analyst_signal,
    resolve_analyst_llm_config,
)
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    apply_profile_usage_to_signal,
    build_profile_usage_contract,
    get_product_price_behavior_profile,
)
from tools.common.signal_evidence_collection import build_signal_collection_contract
from tests.contract_test_fixtures import build_test_aec


def _finalize_data_gap(signal, *, analyst, ticker):
    profile = get_product_price_behavior_profile(ticker)
    usage = build_profile_usage_contract(ticker, analyst, profile)
    return finalize_analyst_signal(
        signal,
        quality_context={
            "sector": str(profile.get("sector") or ""),
            "tradeability": "low",
            "risk_flags": [f"{analyst}_data_unavailable"],
            "data_quality": {
                "coverage_ratio": 0.0,
                "factor_freshness_score": 0.0,
                "no_lookahead_status": "ok",
            },
        },
        full_config={"llm": {"provider": "test", "model": "test-model"}},
        analyst=analyst,
        ticker=ticker,
        trading_date="2025-03-05",
        learning_context={},
        product_profile=profile,
        product_profile_usage=usage,
    )


class AnalystFinalizationFlowTest(unittest.TestCase):
    def test_fundamental_data_gap_forms_strict_scc_compatible_contract(self):
        signal = _build_no_fundamental_data_signal(
            ticker="RB",
            trading_date="2025-03-05",
            agent_name="fundamental",
            metadata={},
            pre_open_only=True,
            info_cutoff="pre_open",
        )
        signal = _finalize_data_gap(signal, analyst="fundamental", ticker="RB")

        contract = signal.metadata["action_evidence_contract"]
        self.assertEqual(contract["contract_version"], "agentquant.action_evidence.v1")
        self.assertEqual(contract["signal"], "Neutral")
        self.assertEqual(contract["side"], "flat")
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["current_trigger_confirmed"])
        self.assertEqual(signal.validation_errors, [])
        scc = build_signal_collection_contract(
            ticker="RB",
            trading_date="2025-03-05",
            analyst_signals=[signal],
            enabled_analysts=["fundamental"],
        )
        self.assertEqual(scc["source_contracts"][0]["action_evidence_contract"], contract)

    def test_news_data_gap_forms_strict_scc_compatible_contract(self):
        signal = _build_no_news_signal(
            ticker="SR",
            trading_date="2025-03-05",
            agent_name="commodity_news",
            news_metadata={},
            pre_open_only=True,
            info_cutoff="pre_open",
        )
        signal = _finalize_data_gap(signal, analyst="commodity_news", ticker="SR")

        contract = signal.metadata["action_evidence_contract"]
        self.assertEqual(contract["signal"], "Neutral")
        self.assertEqual(contract["side"], "flat")
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertFalse(contract["trigger_valid"])
        scc = build_signal_collection_contract(
            ticker="SR",
            trading_date="2025-03-05",
            analyst_signals=[signal],
            enabled_analysts=["commodity_news"],
        )
        self.assertEqual(
            scc["source_contracts"][0]["action_evidence_contract"]["product_profile_evidence"],
            contract["product_profile_evidence"],
        )

    def test_profile_is_applied_before_unique_formal_contract_and_stays_in_sync(self):
        profile = get_product_price_behavior_profile("RB")
        usage = build_profile_usage_contract("RB", "technical", profile)
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            horizon_class="short",
            setup_type="trend_breakout_setup",
            entry_trigger="price breakout confirmed by volume and open interest",
            would_change_view_if="price closes below the confirmed breakout level",
            factor_focus=["trend", "volume", "open_interest", "rebar_inventory_confirmation"],
            evidence_quality="high",
            data_freshness="fresh",
            metadata={
                "data_usage_summary": build_test_aec(
                    "technical",
                    ticker="RB",
                    trading_date="2025-03-05",
                )["data_usage_summary"]
            },
        )
        pre_contract = apply_profile_usage_to_signal(signal.model_copy(deep=True), usage)
        self.assertNotIn("action_evidence_contract", pre_contract.metadata)

        finalized = finalize_analyst_signal(
            signal,
            quality_context={
                "ticker": "RB",
                "sector": profile["sector"],
                "tradeability": "high",
                "setup_type": "trend_breakout_setup",
                "setup_quality_ok": True,
                "dominant_direction": "bullish",
                "market_regime": "trend",
                "risk_flags": [],
            },
            full_config={"llm": {"provider": "test", "model": "test-model"}},
            analyst="technical",
            ticker="RB",
            trading_date="2025-03-05",
            learning_context={},
            product_profile=profile,
            product_profile_usage=usage,
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(set(finalized.metadata), {"action_evidence_contract"})
        self.assertEqual(contract["factor_focus"], finalized.factor_focus)
        self.assertIn("product_profile:RB", finalized.factor_focus)
        self.assertTrue(contract["product_profile_evidence"]["profile_supported_evidence"])
        for legacy in ("open", "hold", "exit", "execution", "state_permissions", "money_objective", "has_invalidation"):
            self.assertNotIn(legacy, contract)

    def test_learning_calibration_precedes_quality_profile_and_contract_generation(self):
        source = inspect.getsource(finalize_analyst_signal)
        ordered = [
            "calibrate_signal_with_learning_context",
            "apply_signal_quality_gate",
            "apply_business_quality_enrichment",
            "evaluate_profile_usage_contract",
            "apply_profile_usage_to_signal",
            "apply_trade_research_contract",
            "analyst_output_landing_violations",
        ]
        positions = [source.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))

    def test_main_llm_config_switch_is_passed_through_without_analyst_override(self):
        state = {
            "llm_config": {
                "provider": "AlternateProvider",
                "model": "alternate-model",
                "alternate_provider": {"reasoning_effort": "high"},
            },
            "full_config": {"analyst_llm": {"cloud_model": "stale-private-model"}},
        }
        resolved = resolve_analyst_llm_config(state)
        self.assertEqual(resolved, state["llm_config"])
        self.assertEqual(resolved["provider"], "AlternateProvider")
        self.assertEqual(resolved["model"], "alternate-model")

    def test_runtime_prompts_request_evidence_not_formal_contract_or_trade_actions(self):
        prompts = (
            build_futures_technical_prompt(ticker="RB", signal_results_compact={"trend": "UP"}),
            build_futures_fundamental_prompt(ticker="RB", fundamentals="inventory: usable"),
            build_futures_commodity_news_prompt(ticker="RB", instrument_context="rebar", news=[]),
        )
        for prompt in prompts:
            self.assertNotIn("position_ratio", prompt)
            self.assertNotIn("action_evidence_contract.open", prompt)
            self.assertNotIn("probe/open", prompt)
            self.assertNotIn("metadata.action_evidence_contract:", prompt)
            self.assertIn("opportunity_state", prompt)

    def test_three_analysts_use_shared_finalizer_and_no_news_product_map(self):
        for relative in (
            "agents/analysis_team/technical.py",
            "agents/analysis_team/fundamental.py",
            "agents/analysis_team/commodity_news.py",
        ):
            source = (SRC_ROOT / relative).read_text(encoding="utf-8-sig")
            self.assertIn("resolve_analyst_llm_config", source, relative)
            self.assertIn("finalize_analyst_signal", source, relative)
            self.assertIn("build_required_market_data_unavailable_signal", source, relative)
            self.assertNotIn("persist_analyst_signal", source, relative)
            self.assertNotIn('"prompt": prompt', source, relative)
            self.assertNotIn("llm_config = state[\"llm_config\"]", source, relative)
        news_source = (SRC_ROOT / "agents/analysis_team/commodity_news.py").read_text(encoding="utf-8-sig")
        self.assertNotIn("FUTURES_INSTRUMENT_CONTEXT", news_source)


if __name__ == "__main__":
    unittest.main()
