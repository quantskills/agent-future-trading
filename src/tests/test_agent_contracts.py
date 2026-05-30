import json
import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.analysis.quality import apply_trade_research_contract
from tools.agent_tools.contracts import (
    build_internal_message_contract,
    build_trade_research_contract,
    validate_artifact_header,
    validate_internal_message_contract,
    validate_trade_research_contract,
)


class AgentContractFixtureTest(unittest.TestCase):
    def test_all_required_agent_contract_fixtures_are_valid(self):
        fixture_path = SRC_ROOT / "tests" / "fixtures" / "agent_contracts" / "contract_fixtures.json"
        fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
        required_agents = {
            "technical",
            "fundamental",
            "commodity_news",
            "portfolio_manager",
            "auditor",
            "trader",
            "accountant",
            "reviewer",
        }
        required_artifact_types = {
            "AnalystSignalArtifact",
            "PMDecisionArtifact",
            "AuditVerdictArtifact",
            "ExecutionArtifact",
            "SettlementArtifact",
            "ReviewerAttributionArtifact",
            "ReviewerLearningArtifact",
            "CausalReviewCandidateArtifact",
        }
        seen_agents = set()
        seen_artifact_types = set()
        for fixture in fixtures:
            header = fixture.get("header") or {}
            seen_agents.add(header.get("agent_name"))
            seen_artifact_types.add(fixture.get("artifact_type"))
            self.assertEqual(validate_artifact_header(header), [], fixture.get("artifact_type"))
            self.assertTrue(fixture.get("artifact_type"))
            self.assertIsInstance(fixture.get("payload"), dict)
            self.assertTrue(header.get("source_artifacts"), fixture.get("artifact_type"))
        self.assertTrue(required_agents.issubset(seen_agents))
        self.assertTrue(required_artifact_types.issubset(seen_artifact_types))

    def test_internal_message_contract_and_trade_research_contract_are_valid(self):
        message = build_internal_message_contract(
            agent="technical",
            trading_date="2025-03-03",
            ticker="BU",
            message_type="AnalystSignalArtifact",
            source_artifacts=["market_data:BU"],
        )
        research = build_trade_research_contract(
            opportunity_type="trend_continuation",
            opportunity_layer="tradeable_setup",
            entry_trigger="breakout confirmation",
            exit_hint="close below invalidation",
            holding_period_hint="short:2 trading day(s)",
            factor_focus=["trend", "volume"],
            current_evidence_conflict=["basis_flat"],
            invalidation_level=3200,
        )

        self.assertEqual(validate_internal_message_contract(message), [])
        self.assertEqual(validate_trade_research_contract(research), [])

    def test_analyst_signal_gets_trade_research_contract(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.BULLISH,
            confidence=0.72,
            trigger_type="breakout_continuation",
            invalidation_level=3200,
            business_quality_score=0.68,
            factor_alignment_score=0.70,
            conflicting_factors=["inventory_conflict"],
        )

        result = apply_trade_research_contract(
            signal,
            {
                "tradeability": "high",
                "market_regime": "trend",
                "indicator_votes": {"details": {"trend": "Bullish", "macd": "Bullish"}},
                "risk_flags": ["high_volatility"],
            },
            analyst="technical",
            trading_date="2025-03-03",
            ticker="BU",
        )

        self.assertEqual(result.opportunity_type, "trend_continuation")
        self.assertEqual(result.opportunity_layer, "tradeable_setup")
        self.assertIn("trade_research_contract", result.metadata)
        self.assertIn("internal_message_contract", result.metadata)
        self.assertIn("trend", result.factor_focus)
        self.assertIn("high_volatility", result.current_evidence_conflict)


if __name__ == "__main__":
    unittest.main()
