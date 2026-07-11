"""Run all pre-backtest tests and control checks.

Test logic lives in src/tests/test_*.py. Control logic lives in
tools/agent_tools/control. This script only orchestrates the pre-backtest gate.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


RUN_DIR = Path(__file__).resolve().parent
SRC_ROOT = RUN_DIR.parent
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.control_team.protocol_governor import ProtocolGovernor
from tools.agent_tools.control.pg_pre_backtest_acceptance import run_pre_backtest_acceptance
from util.config_normalizer import normalize_config


PRE_BACKTEST_TEST_MODULES = [
    "src.tests.test_fact_entry_boundaries",
    "src.tests.test_accountant_settlement_formulas",
    "src.tests.test_final_action_semantics",
    "src.tests.test_pm_watch_for_trigger_release",
    "src.tests.test_pm_state_transition_matrix",
    "src.tests.test_analyst_output_landing",
    "src.tests.test_analyst_product_price_behavior_profile",
    "src.tests.test_agent_output_contract_boundary",
    "src.tests.test_evidence_fusion_semantics",
    "src.tests.test_reviewer_transaction_log_readability",
    "src.tests.test_system_invariant_audit",
    "src.tests.test_mechanism_effectiveness_audit",
    "src.tests.test_contract_coverage_audit",
    "src.tests.test_config_parameter_mapping",
    "src.tests.test_pre_backtest_acceptance",
    "src.tests.test_protocol_governor",
]

PM_WORKFLOW_CONTRACT_GATE_MODULES = [
    "src.tests.test_pre_backtest_pm_workflow_contracts",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AgentQuant pre-backtest test gate.")
    parser.add_argument("--config", type=str, required=True, help="Path to YAML config, e.g. config/dev.yaml")
    parser.add_argument("--local-db", action="store_true")
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument("--config-id", type=str, default=None)
    parser.add_argument("--exp-name", type=str, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument(
        "--deepfund-python",
        type=str,
        default=r"C:\ProgramData\miniconda3\envs\deepfund\python.exe",
    )
    parser.add_argument("--check-llm-auth", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _resolve_config_path(config_path: str) -> Path:
    path = Path(config_path)
    if path.is_absolute():
        return path
    for candidate in (SRC_ROOT / path, PROJECT_ROOT / path):
        if candidate.exists():
            return candidate.resolve()
    return (PROJECT_ROOT / path).resolve()


def _load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return normalize_config(raw, config_path)


def _create_pre_backtest_fake_db(db_path: Path, *, exp_name: str) -> Path:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE config (
                id TEXT PRIMARY KEY,
                exp_name TEXT,
                updated_at TEXT
            );
            CREATE TABLE portfolio (
                id TEXT PRIMARY KEY,
                config_id TEXT,
                current_balance REAL DEFAULT 0
            );
            CREATE TABLE futures_recommendation (
                id TEXT,
                config_id TEXT,
                trading_date TEXT,
                effective_trade_date TEXT,
                source_type TEXT,
                underlying_code TEXT,
                action TEXT,
                lots INTEGER,
                status TEXT,
                audit_payload TEXT,
                signal_snapshot TEXT,
                created_at TEXT
            );
            CREATE TABLE futures_transactions (
                id TEXT,
                portfolio_id TEXT,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT,
                ticker TEXT,
                action TEXT,
                lots INTEGER,
                execution_price REAL,
                contract_multiplier REAL,
                margin_rate REAL,
                margin_used REAL,
                audit_payload TEXT,
                created_at TEXT
            );
            CREATE TABLE futures_intraday_decision (
                id TEXT,
                config_id TEXT,
                trading_date TEXT,
                recommendation_id TEXT,
                ticker TEXT,
                decision TEXT,
                trigger_reason TEXT,
                features_json TEXT,
                created_at TEXT
            );
            CREATE TABLE alpha_setup_action_value (
                id TEXT PRIMARY KEY,
                config_id TEXT,
                scope_key TEXT,
                ticker TEXT,
                side TEXT,
                horizon_class TEXT,
                market_regime TEXT,
                setup_type TEXT,
                action_name TEXT,
                sample_count INTEGER,
                reward_sum REAL,
                reward_mean REAL,
                win_rate REAL,
                action_preference TEXT,
                canonical_action_family TEXT,
                reward_source TEXT,
                evidence_scope TEXT,
                action_value_lane TEXT,
                consumer_scope TEXT DEFAULT 'pm_learning',
                learning_lane TEXT,
                memory_side_role TEXT DEFAULT '',
                retrieval_key TEXT,
                fallback_retrieval_key TEXT,
                execution_retrieval_key TEXT,
                last_sample_date TEXT,
                created_at TEXT,
                updated_at TEXT,
                active INTEGER,
                payload_json TEXT
            );
            CREATE TABLE adaptive_policy_state (
                id TEXT PRIMARY KEY,
                config_id TEXT,
                ticker TEXT,
                side TEXT,
                policy_type TEXT,
                policy_action TEXT,
                source_trading_date TEXT,
                active INTEGER,
                payload_json TEXT,
                created_at TEXT
            );
            CREATE TABLE daily_settlement (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT,
                trading_date TEXT,
                daily_pnl REAL,
                commission REAL,
                current_balance REAL,
                current_margin REAL,
                margin_ratio REAL,
                positions_snapshot TEXT,
                created_at TEXT
            );
            CREATE TABLE researcher_llm_notes (
                id TEXT PRIMARY KEY,
                config_id TEXT,
                trading_date TEXT,
                evidence_pack_id TEXT,
                ticker TEXT,
                raw_prompt TEXT,
                raw_response TEXT,
                created_at TEXT,
                payload_json TEXT
            );
            CREATE TABLE trading_day_phase (
                id TEXT PRIMARY KEY,
                config_id TEXT,
                trading_date TEXT,
                phase TEXT,
                status TEXT,
                started_at TEXT,
                completed_at TEXT,
                message TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO config(id, exp_name, updated_at) VALUES ('prebacktest-cfg', ?, 'prebacktest')",
            (exp_name,),
        )
        conn.execute(
            "INSERT INTO portfolio(id, config_id, current_balance) VALUES ('prebacktest-pf', 'prebacktest-cfg', 0)",
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _run_unittest_modules(modules: list[str]) -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    errors = [f"{case}: {message}" for case, message in result.errors]
    failures = [f"{case}: {message}" for case, message in result.failures]
    return {
        "ok": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "errors": errors,
        "failures": failures,
    }


def _run_protocol_preflight(args: argparse.Namespace, config_path: Path, cfg: dict) -> dict:
    governor = ProtocolGovernor()
    capability_result = governor.validate_capability_cards()
    sqlite_paths = []
    if args.local_db:
        sqlite_paths.extend(
            [
                SRC_ROOT / "assets" / "agentquant.db",
                SRC_ROOT / "assets" / "pandaai_market_cache.db",
            ]
        )
    preflight_result = governor.run_preflight(
        repo_root=PROJECT_ROOT,
        sqlite_paths=sqlite_paths,
        writable_dirs=[SRC_ROOT / "assets"],
        required_files=[config_path],
        deepfund_python=Path(args.deepfund_python),
        llm_config=cfg.get("llm") or {},
        check_llm_auth=bool(args.check_llm_auth),
    )
    combined = capability_result.merge(preflight_result)
    return {
        "ok": combined.ok,
        "errors": combined.errors,
        "warnings": combined.warnings,
        "capability_validation": capability_result.to_dict(),
        "preflight": preflight_result.to_dict(),
    }


def main() -> int:
    args = parse_args()
    config_path = _resolve_config_path(args.config)
    cfg = _load_config(config_path)
    temp_db_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.db_path:
        acceptance_db_path = Path(args.db_path)
    else:
        temp_db_dir = tempfile.TemporaryDirectory()
        acceptance_db_path = _create_pre_backtest_fake_db(
            Path(temp_db_dir.name) / "pre_backtest_acceptance.db",
            exp_name=str(cfg.get("exp_name") or "prebacktest"),
        )

    unittest_report = _run_unittest_modules(PRE_BACKTEST_TEST_MODULES)
    pm_workflow_contract_gate = _run_unittest_modules(PM_WORKFLOW_CONTRACT_GATE_MODULES)
    protocol_report = _run_protocol_preflight(args, config_path, cfg)
    acceptance_report = run_pre_backtest_acceptance(
        config_path=config_path,
        db_path=acceptance_db_path,
        repo_root=PROJECT_ROOT,
        assets_dir=SRC_ROOT / "assets",
        deepfund_python=Path(args.deepfund_python),
        config_id=args.config_id,
        exp_name=args.exp_name,
        start_date=args.start_date,
        end_date=args.end_date,
        check_llm_auth=bool(args.check_llm_auth),
    ).to_dict()
    if temp_db_dir is not None:
        temp_db_dir.cleanup()

    report = {
        "agent_name": "protocol_governor",
        "contract_version": "agentquant.pre_backtest_test.v1",
        "ok": bool(
            unittest_report.get("ok")
            and pm_workflow_contract_gate.get("ok")
            and protocol_report.get("ok")
            and acceptance_report.get("ok")
        ),
        "unittest": unittest_report,
        "pm_workflow_contract_gate": pm_workflow_contract_gate,
        "protocol_preflight": protocol_report,
        "pre_backtest_acceptance": acceptance_report,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("AgentQuant pre-backtest test gate")
        print(f"  ok: {report['ok']}")
        for key in (
            "unittest",
            "pm_workflow_contract_gate",
            "protocol_preflight",
            "pre_backtest_acceptance",
        ):
            section = report[key]
            print(f"  {key}: ok={section.get('ok')}")
            for error in section.get("errors") or section.get("failures") or []:
                print(f"    - {error}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
