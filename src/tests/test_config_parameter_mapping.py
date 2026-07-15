from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / "src" / "config"
MATRIX_PATH = PROJECT_ROOT / "docs" / "matrix_field_semantics.md"

CONFIG_TOKEN = re.compile(r"`(src/config/[^`]+\.yaml)::([^`]+)`")
PYTHON_TOKEN = re.compile(r"`(src/[^`]+\.py)::([A-Za-z_][A-Za-z0-9_]*)`")
PYTHON_KEY_READ = re.compile(r"(?:\.get\(|\[)\s*[\"']([^\"']+)[\"']")


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, dict):
        rows: list[str] = []
        for key, child in value.items():
            rows.extend(_leaf_paths(child, (*prefix, str(key))))
        return rows
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        rows = []
        for child in value:
            rows.extend(_leaf_paths(child, (*prefix, "*")))
        return rows
    return [".".join(prefix)] if prefix else []


def _path_matches(pattern: str, path: str) -> bool:
    pattern_parts = pattern.split(".")
    path_parts = path.split(".")

    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        token = pattern_parts[pattern_index]
        if token == "**":
            return any(match(pattern_index + 1, next_index) for next_index in range(path_index, len(path_parts) + 1))
        if path_index >= len(path_parts):
            return False
        if token not in {"*", path_parts[path_index]}:
            return False
        return match(pattern_index + 1, path_index + 1)

    return match(0, 0)


def _lookup(payload: dict[str, Any], dotted: str) -> Any:
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _function_nodes(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _semantic_config_keys(
    function_name: str,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    visited: set[str] | None = None,
) -> set[str]:
    visited = set(visited or ())
    if function_name in visited or function_name not in functions:
        return set()
    visited.add(function_name)
    node = functions[function_name]
    keys: set[str] = set()
    local_calls: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                local_calls.add(child.func.id)
            if (
                isinstance(child.func, ast.Attribute)
                and child.func.attr == "get"
                and child.args
                and isinstance(child.args[0], ast.Constant)
                and isinstance(child.args[0].value, str)
            ):
                keys.add(child.args[0].value)
            elif isinstance(child.func, ast.Attribute) and child.func.attr in {"items", "keys", "values"}:
                keys.add("<dynamic_mapping>")
        elif isinstance(child, ast.Subscript):
            slice_value = child.slice
            if isinstance(slice_value, ast.Constant) and isinstance(slice_value.value, str):
                keys.add(slice_value.value)
        elif (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
        ):
            keys.add(child.attr)
    for called in local_calls:
        keys.update(_semantic_config_keys(called, functions, visited=visited))
    return keys


class ConfigParameterMappingContractTest(unittest.TestCase):
    def test_each_fixed_leaf_name_has_a_python_read_or_belongs_to_a_registered_dynamic_map(self):
        matrix = MATRIX_PATH.read_text(encoding="utf-8-sig")
        dynamic_roots: dict[str, list[str]] = {}
        for config_file, pattern in CONFIG_TOKEN.findall(matrix):
            if "*" in pattern:
                dynamic_roots.setdefault(config_file, []).append(pattern.split("*")[0].rstrip("."))

        python_keys: set[str] = set()
        for path in (PROJECT_ROOT / "src").rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            python_keys.update(PYTHON_KEY_READ.findall(path.read_text(encoding="utf-8-sig", errors="ignore")))

        missing: list[str] = []
        for config_path in sorted(CONFIG_ROOT.glob("*.yaml")):
            relative = config_path.relative_to(PROJECT_ROOT).as_posix()
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
            for dotted in _leaf_paths(payload):
                in_dynamic_map = any(
                    dotted.startswith(f"{root}.")
                    for root in dynamic_roots.get(relative, [])
                    if root
                )
                if not in_dynamic_map and dotted.split(".")[-1] not in python_keys:
                    missing.append(f"{relative}::{dotted}")
        self.assertEqual(missing, [], "fixed config leaf has no Python field read")

    def test_every_yaml_parameter_is_registered_with_existing_python_consumer(self):
        matrix_lines = MATRIX_PATH.read_text(encoding="utf-8-sig").splitlines()
        mappings: dict[str, list[tuple[str, str, str]]] = {}
        checked_consumers: set[tuple[str, str]] = set()

        for line in matrix_lines:
            config_tokens = CONFIG_TOKEN.findall(line)
            if not config_tokens:
                continue
            python_tokens = PYTHON_TOKEN.findall(line)
            self.assertTrue(python_tokens, f"config mapping row has no Python consumer: {line}")
            for config_file, pattern in config_tokens:
                self.assertFalse(pattern.startswith("**"), f"root-wide wildcard is forbidden: {config_file}::{pattern}")
                mappings.setdefault(config_file, []).append((pattern, python_tokens[0][0], python_tokens[0][1]))
            checked_consumers.update(python_tokens)

        for config_path in sorted(CONFIG_ROOT.glob("*.yaml")):
            relative = config_path.relative_to(PROJECT_ROOT).as_posix()
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
            patterns = mappings.get(relative, [])
            self.assertTrue(patterns, f"config file has no matrix registration: {relative}")
            missing = [
                path
                for path in _leaf_paths(payload)
                if not any(_path_matches(pattern, path) for pattern, _, _ in patterns)
            ]
            self.assertEqual(missing, [], f"unregistered config parameters in {relative}")

        for relative, function_name in sorted(checked_consumers):
            path = PROJECT_ROOT / relative
            self.assertTrue(path.is_file(), f"registered Python consumer does not exist: {relative}")
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            functions = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            self.assertIn(function_name, functions, f"registered consumer function missing: {relative}::{function_name}")

    def test_each_matrix_mapping_binds_to_the_registered_consumer_ast(self):
        matrix_lines = MATRIX_PATH.read_text(encoding="utf-8-sig").splitlines()
        parsed_modules: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        missing: list[str] = []
        for line in matrix_lines:
            config_tokens = CONFIG_TOKEN.findall(line)
            python_tokens = PYTHON_TOKEN.findall(line)
            if not config_tokens or not python_tokens:
                continue
            consumer_keys: list[tuple[str, str, set[str]]] = []
            for consumer_path, function_name in python_tokens:
                if consumer_path not in parsed_modules:
                    path = PROJECT_ROOT / consumer_path
                    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                    parsed_modules[consumer_path] = _function_nodes(tree)
                consumer_keys.append(
                    (
                        consumer_path,
                        function_name,
                        _semantic_config_keys(
                            function_name,
                            parsed_modules[consumer_path],
                        ),
                    )
                )
            for config_file, pattern in config_tokens:
                fixed_parts = [part for part in pattern.split(".") if part not in {"*", "**"}]
                expected_key = fixed_parts[-1] if fixed_parts else ""
                dynamic_subtree = pattern.endswith(".*") or pattern.endswith(".**")
                bound = (
                    any(keys for _, _, keys in consumer_keys)
                    if dynamic_subtree
                    else any(expected_key in keys for _, _, keys in consumer_keys)
                )
                if expected_key and not bound:
                    consumers = ",".join(
                        f"{path}::{name}" for path, name, _ in consumer_keys
                    )
                    missing.append(
                        f"{config_file}::{pattern} -> {consumers}"
                    )
        self.assertEqual(
            missing,
            [],
            "matrix mapping is not read by its registered Python consumer",
        )

    def test_removed_decorative_parameters_do_not_return(self):
        self.assertFalse((CONFIG_ROOT / "evidence_fusion_policy_catalog.yaml").exists())

        forbidden = {
            "analyst_prior_profiles.yaml": ["usage_rules"],
            "data_factor_policy_catalog.yaml": [
                "pandaai_extra_data.mode",
                "pandaai_extra_data.fail_policy",
                "factor_data.no_lookahead_snapshot",
                "factor_data.freshness_gate",
                "factor_data.unknown_factor_trade_weight",
                "factor_data.coverage_audit_tickers",
                "factor_data.news.require_fundamental_or_pandaai_confirmation_for_large_position",
            ],
            "dev.yaml": [
                "analyst_weight_policy.dynamic_overlay",
                "analyst_weight_policy.enabled",
                "analyst_weight_policy.allow_static_weights_to_open",
                "drawdown_control.recovery_probe_margin_ratio_min",
                "runtime.data_quality_summary",
                "position_budget_policy.basis",
                "position_budget_policy.capital_base",
                "control_governance.enabled",
                "control_governance.preflight",
                "control_governance.cost_budget_audit",
                "control_governance.tool_access_policy",
            ],
            "execution_commission_catalog.yaml": ["commission.basis"],
            "finoview_factor_catalog.yaml": ["version", "description", "coverage_audit_tickers"],
            "learning_policy_catalog.yaml": [
                "learning_gatekeeping_policy",
                "strategy_memory.audit.protected_min_sample_count",
                "strategy_memory.audit.protected_multiplier",
                "strategy_memory.audit.watchlist_min_confirmation_score",
                "strategy_memory.audit.watchlist_cap_multiplier",
                "strategy_memory.audit.weak_block_min_confirmation_score",
                "strategy_memory.audit.weak_block_cap_multiplier",
                "learning.contextual_rule_calibration.relaxed_opening_range_miss",
                "learning.contextual_rule_calibration.tightened_opening_range_miss",
                "learning.contextual_rule_calibration.relaxed_intraday_confirmation_score",
                "learning.contextual_rule_calibration.tightened_intraday_confirmation_score",
                "learning.provisional_policy_state.anomaly_loss_cap_enabled",
                "analyst_business_quality.min_score_for_protected",
                "analyst_business_quality.confidence_capped_by_business_quality",
                "analyst_business_quality.require_primary_driver",
                "analyst_business_quality.require_counter_evidence",
                "analyst_business_quality.require_reward_risk_ratio",
                "analyst_business_quality.require_horizon_match",
                "analyst_business_quality.low_quality_action",
                "learning_retention.delete_expired_inactive",
                "learning_retention.preserve_trade_facts",
            ],
            "portfolio_policy_catalog.yaml": [
                "auditor.mode",
                "auditor.use_llm",
                "auditor.policy_version",
                "auditor.approve_with_warning_allowed",
                "auditor.require_valid_final_action_contract",
                "auditor.require_invalidation_for_new_exposure",
                "auditor.trader_requires_approved_verdict",
                "auditor.audit",
                "portfolio_manager.holding_rebalance_control.position_lifecycle.exploration_reconfirm_layers",
                "portfolio_manager.holding_rebalance_control.mature_alpha_release.allow_learning_mechanism_protect",
            ],
            "product_price_behavior_profiles.yaml": ["version", "authority_boundary"],
            "rank_score_policy.yaml": ["rank_score_policy.contract_version", "rank_score_policy.usage_boundary"],
        }

        for filename, paths in forbidden.items():
            payload = yaml.safe_load((CONFIG_ROOT / filename).read_text(encoding="utf-8-sig")) or {}
            for path in paths:
                self.assertIsNone(_lookup(payload, path), f"decorative parameter still present: {filename}::{path}")


if __name__ == "__main__":
    unittest.main()
