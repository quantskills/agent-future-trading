import json
import sqlite3
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

from tools.agent_tools.control.pg_pre_backtest_acceptance import (
    ACCEPTANCE_CHECKS,
    INVARIANT_TO_CHECK,
    INVARIANT_TO_CHECKS,
    run_pre_backtest_acceptance,
)


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


class PreBacktestAcceptanceRegressionTest(unittest.TestCase):
    def _market_quote(
        self,
        *,
        day: str = "2025-03-03",
        ticker: str = "BU2506",
        open_price: float | None = 1.0,
        close_price: float | None = 1.0,
        settle_price: float | None = 1.0,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            trade_date=day,
            ticker=ticker,
            open_price=open_price,
            close_price=close_price,
            settle_price=settle_price,
        )

    def _market_quote_side_effect(self, broken=None):
        broken = dict(broken or {})

        def side_effect(underlying_code, is_main, start_date, end_date):
            rule = broken.get(underlying_code)
            if rule == "missing":
                return []
            if rule == "missing_settle":
                return [self._market_quote(ticker=f"{underlying_code}2506", settle_price=None)]
            if rule == "missing_contract":
                return [self._market_quote(ticker="")]
            if rule == "missing_open":
                return [self._market_quote(ticker=f"{underlying_code}2506", open_price=None)]
            return [self._market_quote(ticker=f"{underlying_code}2506")]

        return side_effect

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
            now = datetime.utcnow().isoformat()
            conn.execute("INSERT INTO config(id, exp_name, updated_at) VALUES (?, ?, ?)", ("cfg", exp_name, now))
            conn.execute("INSERT INTO portfolio(id, config_id, current_balance) VALUES (?, ?, ?)", ("pf", "cfg", 1000000.0))
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

    def _make_db_with_bad_researcher_notes_schema(self) -> str:
        db_path = self._make_db(with_negative_exit_weak_prior=False)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE researcher_llm_notes")
            conn.execute(
                """
                CREATE TABLE researcher_llm_notes (
                    id TEXT PRIMARY KEY,
                    config_id TEXT,
                    source_trading_date TEXT,
                    evidence_pack_id TEXT,
                    ticker TEXT,
                    raw_prompt TEXT,
                    raw_response TEXT,
                    created_at TEXT,
                    payload_json TEXT
                )
                """
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
                "db_schema_contract",
                "data_time_boundary",
                "agent_boundaries",
                "structured_io",
                "contract_coverage",
                "unified_field_semantics",
                "single_trade_exit",
                "evidence_trigger_boundary",
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
        self.assertEqual(
            INVARIANT_TO_CHECK["recommendation_top_level_action_lots_mismatch_final_action_contract"],
            "single_trade_exit",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["recommendation_final_action_contract_lots_delta_mismatch"],
            "single_trade_exit",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["recommendation_final_action_contract_action_mismatch"],
            "single_trade_exit",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["opportunity_ranking_field_used_in_execution_trade_intent"],
            "single_trade_exit",
        )

    def test_acceptance_maps_evidence_trigger_invariants_to_boundary_check(self):
        self.assertEqual(
            INVARIANT_TO_CHECK["trigger_valid_without_current_trigger_confirmed"],
            "evidence_trigger_boundary",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["setup_quality_ok_used_as_current_trigger"],
            "evidence_trigger_boundary",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["opportunity_ranking_field_top_level_trade_authority"],
            "evidence_trigger_boundary",
        )

    def test_acceptance_invariant_mapping_reuses_system_audit_categories(self):
        from tools.agent_tools.control.pg_system_invariants import ERROR_CATEGORY_PREFIXES

        for category, prefixes in ERROR_CATEGORY_PREFIXES.items():
            for prefix in prefixes:
                self.assertIn(category, INVARIANT_TO_CHECKS[prefix])
        self.assertEqual(INVARIANT_TO_CHECK["unified_field_artifact_forbidden_field"], "unified_field_semantics")
        self.assertEqual(
            INVARIANT_TO_CHECK["trigger_valid_without_current_trigger_confirmed"],
            "evidence_trigger_boundary",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["strategy_recommendation_pm_step6_generation_check_missing"],
            "single_trade_exit",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["strategy_recommendation_pm_step6_generation_check_failed"],
            "single_trade_exit",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["strategy_recommendation_pm_legacy_lifecycle_field"],
            "single_trade_exit",
        )

    def test_acceptance_maps_preference_landing_failure_to_learning_landing(self):
        self.assertEqual(
            INVARIANT_TO_CHECK["opportunity_learning_component_used_as_trade_intent"],
            "learning_landing",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["pm_action_value_missing_canonical_fields"],
            "learning_landing",
        )
        self.assertEqual(
            INVARIANT_TO_CHECK["pm_consumed_non_pm_learning_action_value"],
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
            self.assertIn(
                "recommendation_top_level_action_lots_must_match_final_contract",
                report.metadata["protocol_audit_boundaries"],
            )
            self.assertIn(
                "learning_components_remain_diagnostic_not_trade_authority",
                report.checks["learning_landing"].metadata["protocol_audit_boundaries"],
            )
            self.assertIn(
                "final_action_contract_remains_single_trade_truth",
                report.checks["single_trade_exit"].metadata["protocol_audit_boundaries"],
            )
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_fails_fast_on_db_schema_contract_mismatch(self):
        db_path = self._make_db_with_bad_researcher_notes_schema()
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
            self.assertIn("db_schema_contract", report.failed_checks)
            self.assertTrue(
                any(
                    error == "db_schema_contract:schema_missing_required_column:researcher_llm_notes:trading_date"
                    for error in report.errors
                ),
                report.to_dict(),
            )
            self.assertTrue(
                any(
                    error == "audit_explainability:system_invariant_audit_skipped_due_to_schema_contract_failure"
                    for error in report.errors
                ),
                report.to_dict(),
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
            legacy_scan = structured_io.metadata["legacy_field_location_scan"]
            self.assertGreater(legacy_scan["checked_files"], 0)
            self.assertGreater(legacy_scan["legacy_occurrence_count"], 0)
            self.assertEqual(legacy_scan["offender_count"], 0)
            self.assertIn(
                "deprecated_field_tokens_may_exist_only_in_migration_audit_negative_tests_or_archived_history",
                legacy_scan["boundary"],
            )
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

    def test_acceptance_passes_static_backtest_window_without_market_data_read(self):
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
                    start_date="2025-03-01",
                    end_date="2025-03-03",
                    check_llm_auth=False,
                )

            self.assertTrue(report.ok, report.to_dict())
            data_check = report.checks["data_time_boundary"]
            self.assertFalse(data_check.metadata["real_market_data_read"])
            self.assertEqual(data_check.metadata["ticker_count"], 15)
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_acceptance_cli_returns_nonzero_on_invariant_failure(self):
        db_path = self._make_db(with_negative_exit_weak_prior=True)
        try:
            report = run_pre_backtest_acceptance(
                config_path=SRC_ROOT / "config" / "dev.yaml",
                db_path=db_path,
                exp_name="agentquant-test",
                repo_root=PROJECT_ROOT,
                deepfund_python=Path(sys.executable),
                assets_dir=SRC_ROOT / "assets",
                check_llm_auth=False,
            )
            self.assertFalse(report.ok, report.to_dict())
            self.assertIn("learning_landing", str(report.to_dict()))
        finally:
            Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
