import sys
import unittest
from pathlib import Path

import yaml


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from llm.prompt import (
    build_futures_commodity_news_prompt,
    build_futures_fundamental_prompt,
    build_futures_technical_prompt,
)
from tools.agent_tools.analysis.analyst_product_price_behavior_profile import (
    PROFILE_CONTRACT_VERSION,
    apply_profile_usage_to_signal,
    build_profile_usage_contract,
    format_profile_for_commodity_news,
    format_profile_for_fundamental,
    format_profile_for_technical,
    get_product_price_behavior_profile,
    load_product_price_behavior_profiles,
)
from agents.analysis_team.commodity_news import _build_no_news_signal
from agents.analysis_team.fundamental import _build_no_fundamental_data_signal
from tools.common.signal_evidence_collection import build_signal_collection_contract


EXPECTED_TICKERS = {"BU", "C", "CF", "EB", "HC", "I", "J", "M", "MA", "P", "PB", "RB", "SR", "TA", "ZN"}
FORBIDDEN_PROFILE_CONSUMERS = {
    "agents/decision_team/portfolio_manager.py",
    "agents/decision_team/auditor.py",
    "agents/execution_team/trader.py",
    "agents/execution_team/accountant.py",
    "tools/agent_tools/execution/trader_futures_execution.py",
    "tools/agent_tools/execution/accountant_futures_settlement.py",
}


def _read(rel_path: str) -> str:
    return (SRC_ROOT / rel_path).read_text(encoding="utf-8-sig")


class AnalystProductPriceBehaviorProfileTest(unittest.TestCase):
    def test_profile_catalog_covers_all_backtest_tickers_and_has_no_trade_authority(self):
        cfg = yaml.safe_load((SRC_ROOT / "config" / "dev.yaml").read_text(encoding="utf-8")) or {}
        payload = load_product_price_behavior_profiles(cfg)

        self.assertEqual(payload["profile_contract_version"], PROFILE_CONTRACT_VERSION)
        self.assertEqual(set(payload["required_tickers"]), EXPECTED_TICKERS)
        self.assertEqual(set(payload["profiles"].keys()), EXPECTED_TICKERS)
        for ticker in EXPECTED_TICKERS:
            profile = get_product_price_behavior_profile(ticker, cfg)
            usage = build_profile_usage_contract(ticker, "technical", profile)
            self.assertTrue(usage["product_profile_used"])
            self.assertEqual(usage["profile_analysis_boundary"], "analysis_evidence_only_no_trade_authority")

    def test_profile_prompt_context_is_present_for_three_analysts(self):
        rb_profile = get_product_price_behavior_profile("RB")
        tech_context = format_profile_for_technical("RB", rb_profile)
        fund_context = format_profile_for_fundamental("RB", rb_profile)
        news_context = format_profile_for_commodity_news("RB", rb_profile)

        technical_prompt = build_futures_technical_prompt(
            ticker="RB",
            signal_results_compact={"trend": "UP"},
            product_profile_context=tech_context,
        )
        fundamental_prompt = build_futures_fundamental_prompt(
            ticker="RB",
            fundamentals="inventory: improving",
            product_profile_context=fund_context,
        )
        news_prompt = build_futures_commodity_news_prompt(
            ticker="RB",
            instrument_context="RB rebar",
            news=[],
            product_profile_context=news_context,
        )

        for prompt in (technical_prompt, fundamental_prompt, news_prompt):
            self.assertIn("Product Price Behavior Profile", prompt)
            self.assertIn("analysis frame", prompt)
            self.assertIn("not trade authority", prompt)
        for profile_context in (tech_context, fund_context, news_context):
            self.assertNotIn("target_lots", profile_context)

    def test_profile_usage_lands_in_analyst_metadata_and_learning_scope(self):
        profile = get_product_price_behavior_profile("TA")
        usage = build_profile_usage_contract("TA", "fundamental", profile)
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.67,
            metadata={
                "action_evidence_contract": {
                    "opportunity_state": "watch_for_trigger",
                    "trigger_valid": False,
                    "invalidation_present": True,
                    "learning_scope": {"setup_family": "fundamental_timing_setup"},
                }
            },
        )

        updated = apply_profile_usage_to_signal(signal, usage)
        metadata = updated.metadata
        self.assertEqual(metadata["product_profile_evidence"]["product_profile_id"], "agentquant.product_price_behavior.v1:TA")
        self.assertEqual(
            metadata["action_evidence_contract"]["learning_scope"],
            {"setup_family": "fundamental_timing_setup"},
        )
        scope = metadata["learning_scope"]
        self.assertTrue(scope["product_profile_used"])
        self.assertEqual(scope["product_profile_analysis_boundary"], "analysis_evidence_only_no_trade_authority")
        self.assertIn("product_profile:TA", updated.factor_focus)

    def test_signal_collector_preserves_profile_evidence_without_interpreting_it(self):
        profile = get_product_price_behavior_profile("SR")
        usage = build_profile_usage_contract("SR", "commodity_news", profile)
        signal = AnalystSignal(
            agent_name="commodity_news",
            signal=Signal.NEUTRAL,
            confidence=0.42,
            metadata={
                "product_profile_evidence": usage,
                "action_evidence_contract": {
                    "contract_version": "agentquant.action_evidence.v1",
                    "analyst": "commodity_news",
                    "side": "flat",
                    "confidence": 0.42,
                    "opportunity_state": "watch_for_trigger",
                    "trigger_valid": False,
                    "current_trigger_confirmed": False,
                    "setup_type": "news_event_setup",
                    "evidence_quality": "medium",
                    "invalidation_present": True,
                    "product_profile_evidence": usage,
                },
            },
        )

        contract = build_signal_collection_contract(
            ticker="SR",
            trading_date="2025-03-05",
            analyst_signals=[signal],
            enabled_analysts=["technical", "fundamental", "commodity_news"],
        )

        self.assertEqual(contract["collector_decision_boundary"], "no_trade_authority")
        self.assertEqual(contract["source_contracts"][0]["product_profile_evidence"]["product_profile_id"], usage["product_profile_id"])
        self.assertEqual(contract["evidence_items"][0]["product_profile_id"], usage["product_profile_id"])
        for forbidden in ("final_action_contract", "target_lots", "lots_delta", "authority_type", "reason_codes"):
            self.assertNotIn(forbidden, contract)

    def test_data_gap_analyst_signals_still_land_profile_evidence(self):
        rb_profile = get_product_price_behavior_profile("RB")
        fundamental_usage = build_profile_usage_contract("RB", "fundamental", rb_profile)
        fundamental_signal = _build_no_fundamental_data_signal(
            ticker="RB",
            trading_date="2025-03-05",
            agent_name="fundamental",
            metadata={},
            pre_open_only=True,
            info_cutoff="pre_open",
        )
        fundamental_signal = apply_profile_usage_to_signal(fundamental_signal, fundamental_usage)
        self.assertEqual(
            fundamental_signal.metadata["product_profile_evidence"]["product_profile_id"],
            "agentquant.product_price_behavior.v1:RB",
        )

        sr_profile = get_product_price_behavior_profile("SR")
        news_usage = build_profile_usage_contract("SR", "commodity_news", sr_profile)
        news_signal = _build_no_news_signal(
            ticker="SR",
            trading_date="2025-03-05",
            agent_name="commodity_news",
            news_metadata={},
            pre_open_only=True,
            info_cutoff="pre_open",
        )
        news_signal = apply_profile_usage_to_signal(news_signal, news_usage)
        self.assertEqual(
            news_signal.metadata["product_profile_evidence"]["product_profile_id"],
            "agentquant.product_price_behavior.v1:SR",
        )

    def test_profile_is_not_directly_read_by_pm_auditor_trader_or_accountant(self):
        for rel_path in FORBIDDEN_PROFILE_CONSUMERS:
            source = _read(rel_path)
            self.assertNotIn("analyst_product_price_behavior_profile", source, rel_path)
            self.assertNotIn("product_price_behavior_profiles.yaml", source, rel_path)
            self.assertNotIn("get_product_price_behavior_profile", source, rel_path)

    def test_three_analysts_and_signal_collector_are_the_only_direct_runtime_readers(self):
        expected_runtime_readers = {
            "agents/analysis_team/technical.py",
            "agents/analysis_team/fundamental.py",
            "agents/analysis_team/commodity_news.py",
            "tools/agent_tools/analysis/analyst_product_price_behavior_profile.py",
            "tools/agent_tools/analysis/analyst_output_finalization.py",
        }
        actual = set()
        for path in (SRC_ROOT / "agents").rglob("*.py"):
            text = path.read_text(encoding="utf-8-sig")
            if "get_product_price_behavior_profile" in text or "analyst_product_price_behavior_profile" in text:
                actual.add(path.relative_to(SRC_ROOT).as_posix())
        for path in (SRC_ROOT / "tools").rglob("*.py"):
            rel = path.relative_to(SRC_ROOT).as_posix()
            if rel.startswith("tools/agent_tools/control/"):
                continue
            text = path.read_text(encoding="utf-8-sig")
            if "get_product_price_behavior_profile" in text or "analyst_product_price_behavior_profile" in text:
                actual.add(rel)
        self.assertEqual(expected_runtime_readers, actual)


if __name__ == "__main__":
    unittest.main()
