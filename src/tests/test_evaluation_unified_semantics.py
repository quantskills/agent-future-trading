import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evaluation.analyze_strategy_attribution import build_attribution_report
from evaluation.evaluation import (
    calculate_futures_strategy_quality_metrics,
    calculate_futures_transaction_win_rate,
)


class EvaluationUnifiedSemanticsRegressionTest(unittest.TestCase):
    def _tmp_db(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))
        return db_path

    def _create_transactions_table(self, conn):
        conn.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT,
                created_at TEXT,
                ticker TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                execution_price REAL,
                price REAL,
                contract_multiplier REAL,
                commission REAL,
                source_type TEXT
            )
            """
        )

    def test_transaction_win_rate_excludes_operational_source_types(self):
        db_path = self._tmp_db()
        conn = sqlite3.connect(db_path)
        self._create_transactions_table(conn)
        conn.executemany(
            """
            INSERT INTO futures_transactions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("s-o", "cfg", "rs1", "2025-03-03", "2025-03-03T09:00:00", "M", "m2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("s-c", "cfg", "rs2", "2025-03-04", "2025-03-04T09:00:00", "M", "m2505", "close_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                ("r-o", "cfg", "rr1", "2025-03-05", "2025-03-05T09:00:00", "RB", "rb2505", "open_short", 1, 200.0, 200.0, 10.0, 1.0, "rollover"),
                ("r-c", "cfg", "rr2", "2025-03-06", "2025-03-06T09:00:00", "RB", "rb2505", "close_short", 1, 180.0, 180.0, 10.0, 1.0, "rollover"),
                ("f-o", "cfg", "rf1", "2025-03-07", "2025-03-07T09:00:00", "HC", "hc2505", "open_long", 1, 300.0, 300.0, 10.0, 1.0, "forced_risk"),
                ("f-c", "cfg", "rf2", "2025-03-08", "2025-03-08T09:00:00", "HC", "hc2505", "close_long", 1, 280.0, 280.0, 10.0, 1.0, "forced_risk"),
            ],
        )
        conn.commit()
        conn.close()

        metrics = calculate_futures_transaction_win_rate("cfg", db_path)

        self.assertEqual(metrics["total_trades"], 1)
        self.assertEqual(metrics["winning_trades"], 1)
        self.assertAlmostEqual(metrics["win_rate"], 1.0)
        self.assertEqual(metrics["rollover_transaction_count"], 2)
        self.assertEqual(metrics["forced_risk_transaction_count"], 2)
        self.assertEqual(metrics["operational_transaction_count"], 4)

    def test_strategy_quality_metrics_exclude_operational_pairs(self):
        db_path = self._tmp_db()
        conn = sqlite3.connect(db_path)
        self._create_transactions_table(conn)
        conn.executemany(
            """
            INSERT INTO futures_transactions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("s-o", "cfg", "rs1", "2025-03-03", "2025-03-03T09:00:00", "M", "m2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("s-c", "cfg", "rs2", "2025-03-04", "2025-03-04T09:00:00", "M", "m2505", "close_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                ("f-o", "cfg", "rf1", "2025-03-05", "2025-03-05T09:00:00", "HC", "hc2505", "open_long", 1, 300.0, 300.0, 10.0, 1.0, "forced_risk"),
                ("f-c", "cfg", "rf2", "2025-03-06", "2025-03-06T09:00:00", "HC", "hc2505", "close_long", 1, 250.0, 250.0, 10.0, 1.0, "forced_risk"),
            ],
        )
        conn.commit()
        conn.close()

        metrics = calculate_futures_strategy_quality_metrics("cfg", db_path)

        self.assertGreater(metrics["trade_expectancy"], 0.0)
        self.assertGreater(metrics["long_trade_net_pnl"], 0.0)
        self.assertEqual(metrics["short_trade_net_pnl"], 0.0)
        self.assertEqual(metrics["max_consecutive_losing_trades"], 0)

    def test_attribution_report_strategy_groups_exclude_operational_pairs(self):
        db_path = self._tmp_db()
        conn = sqlite3.connect(db_path)
        self._create_transactions_table(conn)
        conn.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT,
                config_id TEXT,
                trading_date TEXT,
                source_type TEXT,
                status TEXT,
                action TEXT,
                lots INTEGER,
                signal_snapshot TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO futures_recommendation
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("rs1", "cfg", "2025-03-03", "strategy", "executed", "open_long", 1, json.dumps({"final_action_contract": {"final_action": "open_probe", "reason_codes": []}})),
                ("rf1", "cfg", "2025-03-05", "forced_risk", "executed", "close_long", 1, json.dumps({})),
            ],
        )
        conn.executemany(
            """
            INSERT INTO futures_transactions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("s-o", "cfg", "rs1", "2025-03-03", "2025-03-03T09:00:00", "M", "m2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                ("s-c", "cfg", "rs1", "2025-03-04", "2025-03-04T09:00:00", "M", "m2505", "close_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                ("f-o", "cfg", "rf1", "2025-03-05", "2025-03-05T09:00:00", "HC", "hc2505", "open_long", 1, 300.0, 300.0, 10.0, 1.0, "forced_risk"),
                ("f-c", "cfg", "rf1", "2025-03-06", "2025-03-06T09:00:00", "HC", "hc2505", "close_long", 1, 250.0, 250.0, 10.0, 1.0, "forced_risk"),
            ],
        )
        conn.commit()
        conn.close()

        report = build_attribution_report(
            config_id="cfg",
            exp_name="eval_semantics",
            db_path=db_path,
        )

        self.assertEqual(report["overall"]["total_trades"], 2)
        self.assertEqual(report["strategy_only_overall"]["total_trades"], 1)
        self.assertEqual({row["ticker"] for row in report["by_ticker_side"]}, {"M"})
        self.assertEqual(report["forced_risk_summary"]["transaction_count"], 2)


if __name__ == "__main__":
    unittest.main()
