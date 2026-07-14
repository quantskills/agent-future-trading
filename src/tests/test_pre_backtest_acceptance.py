import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_pre_backtest_acceptance import (
    PRE_BACKTEST_CHECK_NAMES,
    _data_readiness_check,
    run_pre_backtest_acceptance,
)


class PreBacktestAcceptanceTest(unittest.TestCase):
    def _run(self, **overrides):
        kwargs = {
            "config_path": SRC_ROOT / "config" / "dev.yaml",
            "repo_root": PROJECT_ROOT,
            "assets_dir": SRC_ROOT / "assets",
            "deepfund_python": Path(sys.executable),
            "run_test_modules": False,
        }
        kwargs.update(overrides)
        with patch.dict(os.environ, {"CODEX_OPENAI_API_KEY": "test-key"}, clear=False):
            return run_pre_backtest_acceptance(**kwargs)

    def test_report_has_exactly_ten_pre_backtest_categories(self):
        report = self._run()
        self.assertEqual([check.check_name for check in report.checks], list(PRE_BACKTEST_CHECK_NAMES))
        self.assertEqual(len(report.checks), 10)

    def test_without_window_data_check_is_explicitly_skipped(self):
        report = self._run()
        data_check = next(check for check in report.checks if check.check_name == "data_readiness")
        self.assertEqual(data_check.status, "skipped")
        self.assertIn("data_readiness_window_not_requested", data_check.diagnostic_codes)

    def test_invalid_window_fails_time_boundary(self):
        report = self._run(start_date="2025-02-02", end_date="2025-01-01")
        check = next(check for check in report.checks if check.check_name == "time_boundary")
        self.assertEqual(check.status, "failed")
        self.assertIn("backtest_window_end_before_start", check.violation_codes)

    def test_missing_deepfund_fails_environment_check(self):
        report = self._run(deepfund_python=PROJECT_ROOT / "missing" / "python.exe")
        check = next(check for check in report.checks if check.check_name == "environment_and_entry")
        self.assertIn("deepfund_python_missing", check.violation_codes)

    def test_formal_temporary_database_is_created_by_sqlite_setup(self):
        from tools.agent_tools.control import pg_pre_backtest_acceptance as module

        original = module._initialize_formal_temp_database
        calls = []

        def wrapped(path):
            calls.append(path)
            return original(path)

        with patch.object(module, "_initialize_formal_temp_database", side_effect=wrapped):
            report = self._run()
        self.assertTrue(calls)
        check = next(check for check in report.checks if check.check_name == "formal_temporary_database")
        self.assertEqual(check.status, "passed", check.to_dict())

    def test_optional_fundamental_and_news_absence_are_diagnostics_not_daily_completeness_rules(self):
        source = Path(_data_readiness_check.__code__.co_filename).read_text(encoding="utf-8")
        self.assertIn("finoview_optional_records_unavailable", source)
        self.assertIn("news_optional_records_unavailable", source)
        self.assertNotIn("fundamental_news_daily_coverage", source)

    def test_precheck_uses_no_formal_database_argument(self):
        with self.assertRaises(TypeError):
            self._run(db_path=SRC_ROOT / "assets" / "agentquant.db")


if __name__ == "__main__":
    unittest.main()
