import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import yaml

SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pre_backtest_acceptance import (
    ACCEPTANCE_CHECKS,
    INVARIANT_TO_CHECK,
    INVARIANT_TO_CHECKS,
    run_pre_backtest_acceptance,
)


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


class PreBacktestAcceptanceRegressionTest(unittest.TestCase):
    def _make_db(
        self,
        *,
        with_negative_exit_weak_prior: bool = False,
        exp_name: str = "agentquant-test",
    ) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE config (
                    id TEXT PRIMARY KEY,
                    exp_name TEXT,
                    updated_at TEXT
                );
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    trading_date TEXT,
                    action TEXT,
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
                    setup_type TEXT,
                    action_name TEXT,
                    sample_count INTEGER,
                    reward_sum REAL,
                    action_preference TEXT,
                    last_sample_date TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    active INTEGER,
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
            now = datetime.utcnow().isoformat()
            conn.execute("INSERT INTO config(id, exp_name, updated_at) VALUES (?, ?, ?)", ("cfg", exp_name, now))
            if with_negative_exit_weak_prior:
                conn.execute(
                    """
                    INSERT INTO alpha_setup_action_value(
                        id, config_id, scope_key, ticker, side, setup_type, action_name,
                        sample_count, reward_sum, action_preference, last_sample_date,
                        created_at, updated_at, active, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "av-bu-exit",
                        "cfg",
                        "BU|short|flat|choppy|generic_trade_setup",
                        "BU",
                        "short",
                        "generic_trade_setup",
                        "exit",
                        1,
                        -3917.83,
                        "cap_reduce_or_revalidate",
                        "2025-03-10",
                        now,
                        now,
                        1,
                        _dumps(
                            {
                                "action_preference": "weak_prior",
                                "amplification_scope_quality": "partial_real_state",
                                "real_trade_reward_count": 1,
                                "loss_reward_count": 1,
                                "tail_loss_count": 1,
                                "worst_reward": -3917.83,
                            }
                        ),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_acceptance_freezes_ten_system_readiness_checks(self):
        self.assertEqual(
            list(ACCEPTANCE_CHECKS),
            [
                "environment_api",
                "config_consistency",
                "data_time_boundary",
                "agent_boundaries",
                "structured_io",
                "unified_field_semantics",
                "single_trade_exit",
                "pm_opportunity_routing",
                "trader_trigger_parity",
                "learning_landing",
                "capital_boundary",
                "audit_explainability",
            ],
        )

    def test_acceptance_maps_trade_exit_invariants_to_single_trade_exit(self):
        self.assertEqual(INVARIANT_TO_CHECK["real_open_without_current_contract_evidence"], "single_trade_exit")
        self.assertEqual(INVARIANT_TO_CHECK["direction_or_watchlist_probe_opened"], "single_trade_exit")
        self.assertEqual(INVARIANT_TO_CHECK["trade_contract_source_of_truth_failed"], "single_trade_exit")

    def test_acceptance_maps_pm_routing_invariants_to_pm_opportunity_routing(self):
        self.assertEqual(
            INVARIANT_TO_CHECK["conditional_monitor_candidate_silent_wait"],
            "pm_opportunity_routing",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["high_quality_opportunity_silent_wait"],
            "pm_opportunity_routing",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["trigger_valid_without_current_trigger_confirmed"],
            "pm_opportunity_routing",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["setup_quality_ok_used_as_current_trigger"],
            "pm_opportunity_routing",
        )

    def test_acceptance_invariant_mapping_reuses_system_audit_categories(self):
        from tools.agent_tools.control.system_invariants import ERROR_CATEGORY_PREFIXES

        for category, prefixes in ERROR_CATEGORY_PREFIXES.items():
            for prefix in prefixes:
                self.assertIn(category, INVARIANT_TO_CHECKS[prefix])
        self.assertEqual(INVARIANT_TO_CHECK["unified_field_artifact_forbidden_field"], "unified_field_semantics")
        self.assertEqual(INVARIANT_TO_CHECK["trigger_valid_without_current_trigger_confirmed"], "pm_opportunity_routing")

    def test_acceptance_maps_preference_landing_failure_to_learning_landing(self):
        self.assertEqual(
            INVARIANT_TO_CHECK["action_preferences_exist_but_no_final_action_contract_mentions_them"],
            "learning_landing",
        )

    def test_acceptance_fails_on_real_negative_exit_weak_prior(self):
        db_path = self._make_db(with_negative_exit_weak_prior=True)
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    exp_name="agentquant-test",
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    check_llm_auth=False,
                )
            self.assertFalse(report.ok)
            self.assertIn("learning_landing", report.failed_checks)
            self.assertTrue(
                any("negative_action_value_not_protective_preference" in error for error in report.errors),
                report.to_dict(),
            )
            self.assertFalse(report.metadata["strategy_profitability_checked"])
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_passes_empty_trade_db_as_system_ready(self):
        db_path = self._make_db(with_negative_exit_weak_prior=False)
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    exp_name="agentquant-test",
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    check_llm_auth=False,
                )
            self.assertTrue(report.ok, report.to_dict())
            self.assertEqual(report.failed_checks, [])
            self.assertIn("environment_api", report.checks)
            self.assertIn("learning_landing", report.checks)
            self.assertEqual(
                report.checks["unified_field_semantics"].metadata["source_of_truth"],
                "docs/unified_field_semantics.md",
            )
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_fails_incomplete_trading_day_before_backtest(self):
        db_path = self._make_db(with_negative_exit_weak_prior=False)
        now = datetime.utcnow().isoformat()
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "wait",
                    "current_lots": 0,
                    "target_lots": 0,
                    "lots_delta": 0,
                    "authority_type": "watchlist_only",
                }
            }
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, trading_date, action, status, audit_payload, signal_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec-running", "cfg", "2025-03-10", "hold", "skipped", _dumps(payload), _dumps(payload), now),
            )
            conn.executemany(
                "INSERT INTO trading_day_phase VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("p1", "cfg", "2025-03-10", "phase1", "completed", now, now, ""),
                    ("p2", "cfg", "2025-03-10", "phase2", "running", now, None, ""),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    exp_name="agentquant-test",
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    check_llm_auth=False,
                )
            self.assertFalse(report.ok)
            self.assertIn("data_time_boundary", report.failed_checks)
            self.assertTrue(
                any(error.startswith("data_time_boundary:incomplete_trading_day_phase:2025-03-10:") for error in report.errors),
                report.to_dict(),
            )
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_fails_pm_conditional_monitor_silent_wait_before_backtest(self):
        db_path = self._make_db(with_negative_exit_weak_prior=False)
        now = datetime.utcnow().isoformat()
        payload = {
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "wait",
                "authority_type": "watchlist_only",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "reason_codes": ["pm_watch_for_trigger_probe_cap"],
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            "active_opportunity_audit": {
                "decision": {
                    "action": "hold",
                    "lots": 0,
                    "lands_position": False,
                    "authority_type": "watchlist_only",
                    "reason": "pm_watch_for_trigger_probe_cap",
                },
                "opportunity": {
                    "conditional_monitor_candidate_count": 1,
                    "high_quality_present": True,
                },
                "conditional_monitor_candidates": [
                    {
                        "analyst": "technical",
                        "signal": "Bearish",
                        "opportunity_state": "watch_for_trigger",
                        "setup_quality_ok": True,
                        "trigger_valid": False,
                        "invalidation_present": True,
                        "entry_trigger": "wait for post-open break below support",
                        "conditional_monitor_candidate": True,
                    }
                ],
            },
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, trading_date, action, status, audit_payload, signal_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec-pm-routing", "cfg", "2025-03-10", "hold", "skipped", _dumps(payload), _dumps(payload), now),
            )
            conn.executemany(
                "INSERT INTO trading_day_phase VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("p1", "cfg", "2025-03-10", "phase1", "completed", now, now, ""),
                    ("p2", "cfg", "2025-03-10", "phase2", "completed", now, now, ""),
                    ("p3", "cfg", "2025-03-10", "phase3", "completed", now, now, ""),
                    ("p4", "cfg", "2025-03-10", "phase4", "completed", now, now, ""),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    exp_name="agentquant-test",
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    check_llm_auth=False,
                )
            self.assertFalse(report.ok)
            self.assertIn("pm_opportunity_routing", report.failed_checks)
            self.assertTrue(
                any(
                    error.startswith(
                        "pm_opportunity_routing:conditional_monitor_candidate_silent_wait:2025-03-10:"
                    )
                    for error in report.errors
                ),
                report.to_dict(),
            )
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_fails_unified_field_semantic_violation_before_backtest(self):
        db_path = self._make_db(with_negative_exit_weak_prior=False)
        now = datetime.utcnow().isoformat()
        payload = {
            "technical": {
                "trade_research_contract": {
                    "opportunity_state": "tradeable_candidate",
                    "trigger_valid": True,
                    "current_trigger_confirmed": False,
                    "entry_trigger": "breakout setup is worth watching",
                    "action_evidence_contract": {
                        "opportunity_state": "tradeable_candidate",
                        "setup_quality_ok": False,
                        "trigger_valid": True,
                        "current_trigger_confirmed": False,
                        "entry_trigger": "breakout setup is worth watching",
                    },
                }
            },
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "wait",
                "authority_type": "watchlist_only",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, trading_date, action, status, audit_payload, signal_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec-unified-field", "cfg", "2025-03-10", "hold", "skipped", _dumps(payload), _dumps(payload), now),
            )
            conn.executemany(
                "INSERT INTO trading_day_phase VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("p1", "cfg", "2025-03-10", "phase1", "completed", now, now, ""),
                    ("p2", "cfg", "2025-03-10", "phase2", "completed", now, now, ""),
                    ("p3", "cfg", "2025-03-10", "phase3", "completed", now, now, ""),
                    ("p4", "cfg", "2025-03-10", "phase4", "completed", now, now, ""),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    exp_name="agentquant-test",
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    check_llm_auth=False,
                )
            self.assertFalse(report.ok)
            self.assertIn("unified_field_semantics", report.failed_checks)
            semantics = report.checks["unified_field_semantics"]
            self.assertFalse(semantics.ok, report.to_dict())
            self.assertEqual(semantics.metadata["source_of_truth"], "docs/unified_field_semantics.md")
            self.assertFalse(
                semantics.metadata["unified_field_semantics_audit"].get("ok"),
                report.to_dict(),
            )
            self.assertTrue(
                any(
                    error.startswith(
                        "unified_field_semantics:trigger_valid_without_current_trigger_confirmed:"
                    )
                    for error in report.errors
                ),
                report.to_dict(),
            )
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_structured_io_runs_unified_field_static_scan(self):
        db_path = self._make_db(with_negative_exit_weak_prior=False)
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    exp_name="agentquant-test",
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    check_llm_auth=False,
                )
            structured_io = report.checks["structured_io"]
            self.assertTrue(structured_io.ok, report.to_dict())
            scan = structured_io.metadata["unified_field_runtime_scan"]
            self.assertGreater(scan["checked_files"], 0)
            self.assertGreater(scan["forbidden_token_count"], 0)
            self.assertEqual(scan["offender_count"], 0)
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_uses_config_exp_name_when_exp_name_omitted(self):
        dev_cfg = yaml.safe_load((SRC_ROOT / "config" / "dev.yaml").read_text(encoding="utf-8")) or {}
        exp_name = str(dev_cfg["exp_name"])
        db_path = self._make_db(with_negative_exit_weak_prior=False, exp_name=exp_name)
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    check_llm_auth=False,
                )
            self.assertTrue(report.ok, report.to_dict())
            self.assertEqual(report.failed_checks, [])
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_fails_weekend_only_backtest_window_before_backtest_loop(self):
        db_path = self._make_db(with_negative_exit_weak_prior=False)
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False), patch(
                "tools.agent_tools.control.pre_backtest_acceptance.Router"
            ) as router_cls:
                router_cls.return_value.api.get_futures_daily_candles_optimized.return_value = []
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    exp_name="agentquant-test",
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    start_date="2025-03-01",
                    end_date="2025-03-02",
                    check_llm_auth=False,
                )

            self.assertFalse(report.ok)
            self.assertIn("data_time_boundary", report.failed_checks)
            self.assertTrue(
                any("no_trading_days_in_backtest_window:2025-03-01:2025-03-02" in error for error in report.errors),
                report.to_dict(),
            )
            self.assertFalse(report.metadata["strategy_profitability_checked"])
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_passes_backtest_window_with_resolved_trading_day(self):
        db_path = self._make_db(with_negative_exit_weak_prior=False)
        try:
            with patch.dict("os.environ", {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False), patch(
                "tools.agent_tools.control.pre_backtest_acceptance.Router"
            ) as router_cls:
                router_cls.return_value.api.get_futures_daily_candles_optimized.return_value = [
                    SimpleNamespace(trade_date="2025-03-03")
                ]
                report = run_pre_backtest_acceptance(
                    config_path=SRC_ROOT / "config" / "dev.yaml",
                    db_path=db_path,
                    exp_name="agentquant-test",
                    repo_root=PROJECT_ROOT,
                    deepfund_python=Path(sys.executable),
                    assets_dir=SRC_ROOT / "assets",
                    start_date="2025-03-01",
                    end_date="2025-03-03",
                    check_llm_auth=False,
                )

            self.assertTrue(report.ok, report.to_dict())
            data_check = report.checks["data_time_boundary"]
            self.assertEqual(data_check.metadata["trading_day_count"], 1)
            self.assertEqual(data_check.metadata["first_trading_day"], "2025-03-03")
            self.assertEqual(data_check.metadata["last_trading_day"], "2025-03-03")
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_cli_returns_nonzero_on_invariant_failure(self):
        db_path = self._make_db(with_negative_exit_weak_prior=True)
        script = SRC_ROOT / "run" / "control" / "pre_backtest_acceptance.py"
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--config",
                    str(SRC_ROOT / "config" / "dev.yaml"),
                    "--db-path",
                    db_path,
                    "--exp-name",
                    "agentquant-test",
                    "--deepfund-python",
                    sys.executable,
                    "--json",
                ],
                cwd=str(PROJECT_ROOT),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            self.assertIn('"agent_name": "protocol_governor"', completed.stdout)
            self.assertIn('"ok": false', completed.stdout)
            self.assertIn("learning_landing", completed.stdout)
        finally:
            Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
