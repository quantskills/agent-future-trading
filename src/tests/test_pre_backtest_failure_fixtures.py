import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_pre_backtest_failure_fixtures import (
    FIXTURE_RUNNERS,
    MATRIX_FAILURE_FIXTURE_IDS,
    run_pre_backtest_failure_fixtures,
)


class PreBacktestFailureFixturesTest(unittest.TestCase):
    def test_fixture_gate_covers_matrix_failures(self):
        self.assertEqual(
            set(MATRIX_FAILURE_FIXTURE_IDS),
            {
                "scc_missing",
                "scc_source_agent_boundary_invalid",
                "pm_incomplete_prior_in_formal_action_values",
                "observe_empty_preference",
                "observe_positive_candidate_forbidden",
                "step2_step6_trace_mixed",
                "execution_profile_pollutes_decision_rows",
                "action_family_lane_preference_mismatch",
                "trader_artifact_forbidden_fields",
                "reviewer_artifact_forbidden_fields",
                "researcher_artifact_forbidden_fields",
                "trader_transaction_not_from_final_contract",
                "unfinished_day_enters_learning",
            },
        )
        self.assertEqual(set(FIXTURE_RUNNERS), set(MATRIX_FAILURE_FIXTURE_IDS))

    def test_fixture_gate_passes_when_each_historical_failure_shape_is_blocked(self):
        result = run_pre_backtest_failure_fixtures()

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(
            set(result.metadata["passed_fixture_ids"]),
            set(MATRIX_FAILURE_FIXTURE_IDS),
        )
        self.assertFalse(result.metadata.get("strategy_profitability_checked", False))

    def test_each_fixture_runner_reports_no_internal_gap(self):
        for fixture_id in MATRIX_FAILURE_FIXTURE_IDS:
            with self.subTest(fixture_id=fixture_id):
                errors: list[str] = []
                FIXTURE_RUNNERS[fixture_id](errors)

                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
