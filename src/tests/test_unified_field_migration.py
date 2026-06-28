from __future__ import annotations

import unittest
import sqlite3
import sys
import tempfile
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from database.sqlite_setup import _ensure_reviewer_learning_schema
from tools.agent_tools.control.pg_unified_field_audit import (
    iter_runtime_field_files,
    scan_legacy_field_token_locations,
    scan_runtime_field_usage,
)


def _iter_runtime_files():
    yield from iter_runtime_field_files(SRC_ROOT)


class UnifiedFieldMigrationTests(unittest.TestCase):
    def test_forbidden_runtime_field_tokens_are_absent(self):
        offenders, _checked_files = scan_runtime_field_usage(SRC_ROOT)
        self.assertEqual([], offenders)

    def test_legacy_field_tokens_only_exist_in_explicit_allowlist(self):
        offenders, checked_files, occurrence_count = scan_legacy_field_token_locations(PROJECT_ROOT)
        self.assertGreater(checked_files, 0)
        self.assertGreater(occurrence_count, 0)
        self.assertEqual([], offenders)

    def test_legacy_field_location_scan_rejects_active_runtime_mentions(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            bad_file = repo / "src" / "agents" / "decision_team" / "bad_runtime.py"
            bad_file.parent.mkdir(parents=True, exist_ok=True)
            bad_file.write_text("payload = {'target_lots_estimate': 3}\n", encoding="utf-8")

            offenders, checked_files, occurrence_count = scan_legacy_field_token_locations(repo)

        self.assertGreater(checked_files, 0)
        self.assertGreater(occurrence_count, 0)
        self.assertEqual(["src\\agents\\decision_team\\bad_runtime.py:1:target_lots_estimate"], offenders)

    def test_reviewer_learning_schema_drops_legacy_field_columns(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            _ensure_reviewer_learning_schema(cursor)
            cursor.execute("ALTER TABLE signal_context_history ADD COLUMN final_action_context_json TEXT")
            cursor.execute("ALTER TABLE signal_context_history ADD COLUMN final_action_context_artifact_path TEXT")
            cursor.execute("ALTER TABLE signal_context_history ADD COLUMN final_action_context_sha256 TEXT")
            cursor.execute("ALTER TABLE signal_context_history ADD COLUMN final_action_context_size INTEGER")
            cursor.execute("ALTER TABLE signal_context_history ADD COLUMN final_action_context_summary_json TEXT")
            cursor.execute("ALTER TABLE signal_context_history ADD COLUMN pre_open_plan_json TEXT")
            cursor.execute("ALTER TABLE signal_context_history ADD COLUMN target_lots_estimate INTEGER")
            cursor.execute("ALTER TABLE alpha_setup_action_value ADD COLUMN policy_hint TEXT")
            cursor.execute("ALTER TABLE alpha_setup_action_value ADD COLUMN deprecated_policy_hint_mirror TEXT")
            cursor.execute("ALTER TABLE alpha_setup_profile ADD COLUMN action_bias TEXT")
            cursor.execute("ALTER TABLE alpha_setup_profile ADD COLUMN deprecated_action_bias_mirror TEXT")

            _ensure_reviewer_learning_schema(cursor)

            expected_absent = {
                "signal_context_history": {
                    "final_action_context_json",
                    "final_action_context_artifact_path",
                    "final_action_context_sha256",
                    "final_action_context_size",
                    "final_action_context_summary_json",
                    "pre_open_plan_json",
                    "target_lots_estimate",
                },
                "alpha_setup_action_value": {
                    "policy_hint",
                    "deprecated_policy_hint_mirror",
                },
                "alpha_setup_profile": {
                    "action_bias",
                    "deprecated_action_bias_mirror",
                },
            }
            for table_name, old_columns in expected_absent.items():
                cursor.execute(f"PRAGMA table_info({table_name})")
                columns = {str(row[1]) for row in cursor.fetchall()}
                self.assertFalse(old_columns & columns, f"{table_name} still has {old_columns & columns}")
            cursor.execute("PRAGMA table_info(signal_context_history)")
            columns = {str(row[1]) for row in cursor.fetchall()}
            self.assertIn("final_action_contract_json", columns)
            self.assertIn("final_action_contract_artifact_path", columns)
        finally:
            conn.close()

    def test_reviewer_learning_schema_migrates_old_setup_performance_table(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            cursor.execute(
                """
                CREATE TABLE signal_template_performance (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    signal_template TEXT NOT NULL,
                    horizon_class TEXT,
                    market_regime TEXT,
                    sample_count INTEGER,
                    win_rate REAL,
                    net_pnl REAL,
                    avg_pnl REAL,
                    profit_factor REAL,
                    confidence_score REAL,
                    last_sample_date TEXT,
                    last_updated TEXT,
                    valid_until TEXT,
                    payload_json TEXT
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO signal_template_performance (
                    id, config_id, ticker, side, signal_template, horizon_class,
                    market_regime, sample_count, win_rate, net_pnl, avg_pnl,
                    profit_factor, confidence_score, last_sample_date, last_updated,
                    valid_until, payload_json
                ) VALUES (
                    'old-1', 'cfg', 'RB', 'long', 'trend_breakout_setup', 'short',
                    'trend', 3, 0.67, 1200.0, 400.0, 1.5, 0.7,
                    '2025-03-10', '2025-03-11', '2025-04-01', '{}'
                )
                """
            )

            _ensure_reviewer_learning_schema(cursor)

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signal_template_performance'")
            self.assertIsNone(cursor.fetchone())
            row = cursor.execute(
                """
                SELECT setup_type, sample_count, win_rate, net_pnl
                FROM setup_type_performance
                WHERE id='old-1'
                """
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "trend_breakout_setup")
            self.assertEqual(row[1], 3)
            self.assertAlmostEqual(row[2], 0.67)
            self.assertAlmostEqual(row[3], 1200.0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
