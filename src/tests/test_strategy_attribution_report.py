import unittest
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evaluation.analyze_strategy_attribution import (
    _action_value_summary_from_recommendations,
    _attach_open_recommendation_context,
    _learning_component_summary,
    _readable_weak_suggestion,
    _rebalance_summary_from_snapshot,
    _release_block_summary_from_recommendations,
)


class StrategyAttributionReportRegressionTest(unittest.TestCase):
    def test_rebalance_summary_does_not_infer_missing_lots_delta(self):
        summary = _rebalance_summary_from_snapshot(
            {
                "final_action_contract": {
                    "final_action": "hold",
                    "current_lots": -4,
                    "target_lots": -4,
                    "reason_codes": ["position_matched"],
                    "single_source_of_trade_truth": True,
                }
            }
        )

        self.assertIsNone(summary["lots_delta"])
        self.assertEqual(summary["expected_lots_delta"], 0)
        self.assertIn("missing_lots_delta", summary["contract_field_issues"])
        self.assertTrue(summary["single_source_of_trade_truth"])

    def test_weak_side_suggestion_is_review_candidate_not_trade_instruction(self):
        suggestion = _readable_weak_suggestion(
            {"ticker": "RB", "side": "short", "win_rate": 0.20, "total_pnl": -3000.0},
            scope="side",
        )

        lowered = suggestion.lower()
        self.assertIn("review", lowered)
        self.assertIn("researcher", lowered)
        self.assertNotIn("block or hard-cap", lowered)
        self.assertNotIn("soft-cap", lowered)

    def test_release_and_action_value_summaries_are_read_only_attribution(self):
        recommendations = [
            {
                "source_type": "strategy",
                "trading_date": "2025-03-17",
                "signal_snapshot": {
                    "release_block_diagnostics": {
                        "contract_version": "agentquant.release_block_diagnostics.v1",
                        "ticker": "SR",
                        "observation_only": True,
                        "does_not_modify_trade_authority": True,
                        "primary_block_reason": "market_confirmation_conflict",
                        "blocking_category": "current_confirmation_missing",
                        "next_evidence_needed": ["current_price_or_volume_confirmation"],
                        "evidence_snapshot": {
                            "preferred_side": "long",
                            "preferred_side_state": "tradeable_candidate",
                            "current_evidence_present": True,
                            "invalidation_present": True,
                            "watch_for_trigger_block": False,
                        },
                    },
                    "final_action_contract": {
                        "learning_used": {
                            "alpha_setup_action_values": [
                                {
                                    "ticker": "SR",
                                    "side": "long",
                                    "action_name": "open",
                                    "action_preference": "positive_candidate_open",
                                    "amplification_scope_quality": "exact_real_state",
                                    "reward_source": "trade_episode",
                                    "sample_count": 2,
                                    "reward_mean": 1200.0,
                                }
                            ]
                        }
                    },
                },
            }
        ]

        release = _release_block_summary_from_recommendations(recommendations)
        action_value = _action_value_summary_from_recommendations(recommendations)

        self.assertEqual(release["total"], 1)
        self.assertEqual(release["observation_only_violations"], 0)
        self.assertEqual(release["category_counts"]["current_confirmation_missing"], 1)
        self.assertEqual(release["evidence_counts"]["current_evidence_present"], 1)
        self.assertIn("read_only_attribution", release["audit_boundary"])

        self.assertEqual(action_value["total"], 1)
        self.assertEqual(action_value["lane_counts"]["open"], 1)
        self.assertEqual(action_value["action_preference_counts"]["positive_candidate_open"], 1)
        self.assertEqual(action_value["scope_quality_counts"]["exact_real_state"], 1)
        self.assertEqual(action_value["reward_source_counts"]["trade_episode"], 1)
        self.assertIn("diagnostic_not_trade_authority", action_value["audit_boundary"])

    def test_opportunity_learning_component_summary_is_read_only_attribution(self):
        pairs = [
            {
                "open_recommendation_id": "rec1",
                "pnl": 1200.0,
                "total_pnl": 1200.0,
                "net_pnl": 1200.0,
            }
        ]
        recommendations = {
            "rec1": {
                "signal_snapshot": {
                    "final_action_contract": {
                        "evidence_used": {
                            "opportunity_score": 0.72,
                            "opportunity_rank": 1,
                            "capital_allocation_reason": "ranked_deployable_candidate",
                            "opportunity_score_components": {
                                "positive_learning": 0.12,
                                "negative_learning": 0.0,
                                "execution_profile_learning": -0.03,
                                "recent_tail_loss_penalty": 0.0,
                            },
                        },
                        "learning_used": {"learning_adjustment_summary": {"not_trade_authority": True}},
                    }
                }
            }
        }

        enriched = _attach_open_recommendation_context(pairs, recommendations)
        summary = _learning_component_summary(enriched)
        buckets = {
            (row["learning_component"], row["learning_component_bucket"]): row
            for row in summary
        }

        self.assertEqual(enriched[0]["positive_learning_bucket"], "positive")
        self.assertEqual(enriched[0]["execution_profile_learning_bucket"], "negative")
        self.assertIn(("positive_learning", "positive"), buckets)
        self.assertIn(("execution_profile_learning", "negative"), buckets)
        self.assertEqual(buckets[("positive_learning", "positive")]["total_trades"], 1)


if __name__ == "__main__":
    unittest.main()


