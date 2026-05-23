import json
import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.contracts import validate_artifact_header


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


if __name__ == "__main__":
    unittest.main()
