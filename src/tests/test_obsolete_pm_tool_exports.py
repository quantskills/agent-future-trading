import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ObsoletePmToolExportsTests(unittest.TestCase):
    def test_removed_pm_tool_functions_are_not_reintroduced(self):
        obsolete_by_path = {
            "src/tools/agent_tools/decision/pm_capital_allocator.py": {
                "has_adaptive_policy_action",
            },
            "src/tools/agent_tools/decision/pm_contextual_rule_calibration.py": {
                "apply_auditor_contextual_calibration",
            },
            "src/tools/agent_tools/decision/pm_contract_self_check.py": {
                "assert_final_action_contract",
            },
            "src/tools/agent_tools/decision/pm_full_market_capital_deployment.py": {
                "_capital_deployment_complete",
                "_lots_action_from_target",
            },
            "src/tools/agent_tools/decision/pm_lifecycle_action_port.py": {
                "build_lifecycle_transition_diagnostic",
                "primary_port_to_contract_lifecycle",
            },
            "src/tools/agent_tools/decision/pm_signal_fusion.py": {
                "build_horizon_scope",
                "dominant_business_quality",
            },
            "src/tools/agent_tools/decision/pm_risk_gate.py": {
                "_infer_horizon_from_payload",
                "_infer_market_regime_from_payload",
            },
            "src/tools/agent_tools/decision/pm_state_transition.py": {
                "classify_new_entry_transition",
            },
            "src/tools/common/alpha_setup.py": {
                "action_value_prompt_line",
                "compact_action_value_for_trace",
            },
            "src/tools/common/contracts.py": {
                "wrap_artifact",
                "attach_snapshot_contract",
            },
            "src/tools/common/final_action_semantics.py": {
                "action_value_matches_contract_memory_requirement",
                "classify_analyst_evidence",
                "contract_has_lot_change",
                "contract_has_trade_intent",
                "derive_execution_requirement",
                "has_active_opportunity_rejection",
                "has_valid_generic_no_change_explanation",
                "rank_capital_layer_contract_complete",
                "validate_signal_collection",
            },
        }
        for relative_path, obsolete_names in obsolete_by_path.items():
            with self.subTest(path=relative_path):
                tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8-sig"))
                defined_names = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.assertFalse(
                    defined_names & obsolete_names,
                    f"obsolete PM tool functions reintroduced: {sorted(defined_names & obsolete_names)}",
                )


if __name__ == "__main__":
    unittest.main()
