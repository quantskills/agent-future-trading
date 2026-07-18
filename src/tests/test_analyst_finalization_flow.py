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
    @staticmethod
    def _data_usage(analyst: str, ticker: str) -> dict:
        return build_test_aec(
            analyst,
            ticker=ticker,
            trading_date="2025-03-26",
        )["data_usage_summary"]

    def _finalize_directional(self, signal: AnalystSignal, *, analyst: str, ticker: str, context: dict):
        profile = get_product_price_behavior_profile(ticker)
        usage = build_profile_usage_contract(ticker, analyst, profile)
        return finalize_analyst_signal(
            signal,
            quality_context={"sector": profile.get("sector", ""), **context},
            full_config={"llm": {"provider": "test", "model": "test-model"}},
            analyst=analyst,
            ticker=ticker,
            trading_date="2025-03-26",
            learning_context={},
            product_profile=profile,
            product_profile_usage=usage,
        )

    def test_technical_finalization_replaces_llm_trigger_prose_with_canonical_profile_trigger(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BEARISH,
            confidence=0.62,
            setup_type="range_reversal_setup",
            opportunity_type="short_timing",
            opportunity_state="watch_for_trigger",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="15m bespoke model prose about support and volume",
            exit_hint="close above the invalidation area",
            invalidation_level=102.0,
            factor_focus=["range_reversal", "volume"],
            metadata={
                "data_usage_summary": self._data_usage("technical", "BU"),
                "invalidation_condition": "15m close above 102",
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="technical",
            ticker="BU",
            context={
                "tradeability": "medium",
                "setup_type": "range_reversal_setup",
                "setup_quality_ok": True,
                "market_regime": "range",
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["entry_timing_signal"], "breakout")
        self.assertEqual(
            contract["entry_trigger"],
            "15分钟收盘价向下突破开盘区间下沿且低于VWAP",
        )
        self.assertEqual(contract["setup_type"], "range_reversal_setup")
        self.assertEqual(contract["opportunity_state"], "watch_for_trigger")

    def test_fundamental_finalization_keeps_direction_but_removes_execution_claim(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.6256,
            setup_type="fundamental_timing_setup",
            opportunity_type="medium_fundamental",
            opportunity_state="tradeable_candidate",
            evidence_role="entry_timing",
            entry_timing_signal="breakout",
            entry_trigger="15m close above a model-selected level",
            trigger_valid=True,
            invalidation_level=96.0,
            exit_hint="fundamental thesis invalidated below 96",
            factor_focus=["inventory", "basis"],
            metadata={
                "data_usage_summary": self._data_usage("fundamental", "BU"),
                "invalidation_condition": "fundamental thesis invalidated below 96",
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="fundamental",
            ticker="BU",
            context={
                "tradeability": "high",
                "setup_type": "fundamental_timing_setup",
                "setup_quality_ok": True,
                "fundamental_deployable_confirmed": True,
                "data_quality": {
                    "coverage_ratio": 0.9,
                    "supports_fundamental_trade_setup": True,
                },
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["signal"], "Bullish")
        self.assertEqual(contract["evidence_role"], "direction_context")
        self.assertEqual(contract["opportunity_state"], "no_opportunity")
        self.assertEqual(contract["entry_timing_signal"], "")
        self.assertEqual(contract["entry_trigger"], "")
        self.assertFalse(contract["trigger_valid"])
        self.assertFalse(contract["current_trigger_confirmed"])

    def test_news_immediate_event_uses_only_event_immediate_profile(self):
        signal = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.BEARISH,
            confidence=0.80,
            setup_type="news_event_setup",
            opportunity_type="event_driven",
            opportunity_state="tradeable_candidate",
            evidence_role="event_catalyst",
            entry_timing_signal="event_immediate",
            entry_trigger="current price and volume confirm the event",
            trigger_valid=True,
            event_type="supply_disruption",
            invalidation_level=105.0,
            exit_hint="event impact invalidated above 105",
            factor_focus=["supply_disruption"],
            metadata={
                "data_usage_summary": self._data_usage("commodity_news", "BU"),
                "invalidation_condition": "event impact invalidated above 105",
            },
        )

        finalized = self._finalize_directional(
            signal,
            analyst="commodity_news",
            ticker="BU",
            context={
                "tradeability": "high",
                "setup_type": "news_event_setup",
                "setup_quality_ok": True,
                "tradable_event": True,
                "price_reaction_required": False,
                "price_reaction_confirmed": True,
                "risk_flags": [],
            },
        )
        contract = finalized.metadata["action_evidence_contract"]
        self.assertEqual(contract["entry_timing_signal"], "event_immediate")
        self.assertEqual(
            contract["entry_trigger"],
            "当前事件已满足即时执行边界，使用首根合法1分钟线执行",
        )
        self.assertEqual(contract["opportunity_state"], "probe_candidate")

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
        technical_prompt = build_futures_technical_prompt(
            ticker="RB", signal_results_compact={"trend": "UP"}
        )
        fundamental_prompt = build_futures_fundamental_prompt(
            ticker="RB", fundamentals="inventory: usable"
        )
        news_prompt = build_futures_commodity_news_prompt(
            ticker="RB", instrument_context="rebar", news=[]
        )
        prompts = (technical_prompt, fundamental_prompt, news_prompt)
        for prompt in prompts:
            self.assertNotIn("position_ratio", prompt)
            self.assertNotIn("action_evidence_contract.open", prompt)
            self.assertNotIn("probe/open", prompt)
            self.assertNotIn("metadata.action_evidence_contract:", prompt)
            self.assertIn("opportunity_state", prompt)
        self.assertIn("breakout / pullback / vwap_confirmed", technical_prompt)
        self.assertIn("evidence_role=direction_context", fundamental_prompt)
        self.assertIn("must not output a Trader execution profile", fundamental_prompt)
        self.assertIn("entry_timing_signal=event_immediate", news_prompt)

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
