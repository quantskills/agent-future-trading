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
    calculate_futures_trade_win_rate,
)
from evaluation.plot_portfolio import PortfolioCurvePlotter
from util.futures_trade_pairs import build_strategy_originated_trade_pairs


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

    def test_daily_performance_is_after_fee_and_uses_daily_equity_base(self):
        db_path = self._tmp_db()
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE portfolio (id TEXT, config_id TEXT);
            CREATE TABLE daily_settlement (
                portfolio_id TEXT, trading_date TEXT, daily_pnl REAL,
                commission REAL, previous_balance REAL, previous_margin REAL
            );
            """
        )
        conn.execute("INSERT INTO portfolio VALUES ('p1', 'cfg')")
        conn.executemany(
            "INSERT INTO daily_settlement VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("p1", "2025-03-03", 1.0, 2.0, 100.0, 0.0),
                ("p1", "2025-03-04", 10.0, 0.0, 99.0, 0.0),
            ],
        )
        conn.commit()
        conn.close()

        metrics = calculate_futures_trade_win_rate("cfg", db_path)

        self.assertEqual(metrics["winning_days"], 1)
        self.assertEqual(metrics["losing_days"], 1)
        self.assertAlmostEqual(metrics["gross_pnl"], 11.0)
        self.assertAlmostEqual(metrics["total_commission"], 2.0)
        self.assertAlmostEqual(metrics["net_pnl"], 9.0)
        self.assertAlmostEqual(metrics["avg_return_per_day"], (-1.0 / 100.0 + 10.0 / 99.0) / 2.0)

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

    def test_strategy_open_closed_by_operational_actions_enters_strategy_metrics(self):
        db_path = self._tmp_db()
        conn = sqlite3.connect(db_path)
        self._create_transactions_table(conn)
        conn.executemany(
            """
            INSERT INTO futures_transactions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("hc-o", "cfg", "hc-strategy", "2025-03-03", "2025-03-03T09:00:00", "HC", "hc2505", "open_short", 1, 300.0, 300.0, 10.0, 1.0, "strategy"),
                ("hc-c", "cfg", "hc-roll", "2025-03-04", "2025-03-04T09:00:00", "HC", "hc2505", "close_short", 1, 290.0, 290.0, 10.0, 1.0, "rollover"),
                ("c-o", "cfg", "c-strategy", "2025-03-05", "2025-03-05T09:00:00", "C", "c2505", "open_long", 1, 200.0, 200.0, 10.0, 1.0, "strategy"),
                ("c-c", "cfg", "c-risk", "2025-03-06", "2025-03-06T09:00:00", "C", "c2505", "close_long", 1, 205.0, 205.0, 10.0, 1.0, "forced_risk"),
            ],
        )
        conn.commit()
        conn.close()

        win_rate = calculate_futures_transaction_win_rate("cfg", db_path)
        quality = calculate_futures_strategy_quality_metrics("cfg", db_path)

        self.assertEqual(win_rate["total_trades"], 2)
        self.assertEqual(win_rate["winning_trades"], 2)
        self.assertAlmostEqual(win_rate["realized_trade_pnl"], 146.0)
        self.assertEqual(win_rate["rollover_transaction_count"], 1)
        self.assertEqual(win_rate["forced_risk_transaction_count"], 1)
        self.assertAlmostEqual(quality["short_trade_net_pnl"], 98.0)
        self.assertAlmostEqual(quality["long_trade_net_pnl"], 48.0)

    def test_rollover_reopen_transfers_original_strategy_lineage(self):
        transactions = [
            {
                "id": "s-open",
                "recommendation_id": "strategy-rec",
                "trading_date": "2025-03-03",
                "created_at": "2025-03-03T09:00:00",
                "ticker": "C",
                "contract_code": "c2505",
                "action": "open_short",
                "lots": 1,
                "execution_price": 2100.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "strategy",
            },
            {
                "id": "r-close",
                "recommendation_id": "roll-rec",
                "trading_date": "2025-03-10",
                "created_at": "2025-03-10T09:00:00",
                "ticker": "C",
                "contract_code": "c2505",
                "action": "close_short",
                "lots": 1,
                "execution_price": 2090.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "rollover",
            },
            {
                "id": "r-open",
                "recommendation_id": "roll-rec",
                "trading_date": "2025-03-10",
                "created_at": "2025-03-10T09:00:01",
                "ticker": "C",
                "contract_code": "c2509",
                "action": "open_short",
                "lots": 1,
                "execution_price": 2080.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "rollover",
            },
            {
                "id": "s-close",
                "recommendation_id": "strategy-close",
                "trading_date": "2025-03-12",
                "created_at": "2025-03-12T09:00:00",
                "ticker": "C",
                "contract_code": "c2509",
                "action": "close_short",
                "lots": 1,
                "execution_price": 2070.0,
                "contract_multiplier": 10.0,
                "commission": 1.0,
                "source_type": "strategy",
            },
        ]

        pairs = build_strategy_originated_trade_pairs(transactions)

        self.assertEqual(len(pairs), 2)
        self.assertEqual([pair["origin_recommendation_id"] for pair in pairs], ["strategy-rec", "strategy-rec"])
        self.assertEqual(pairs[1]["open_source_type"], "rollover")
        self.assertTrue(pairs[1]["strategy_originated"])
        self.assertTrue(pairs[1]["contains_rollover"])
        self.assertAlmostEqual(sum(pair["net_pnl"] for pair in pairs), 196.0)

    def test_window_replays_inherited_strategy_position_and_uses_close_date(self):
        db_path = self._tmp_db()
        conn = sqlite3.connect(db_path)
        self._create_transactions_table(conn)
        conn.executemany(
            """
            INSERT INTO futures_transactions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("o", "cfg", "strategy-rec", "2025-03-01", "2025-03-01T09:00:00", "M", "m2505", "open_long", 2, 100.0, 100.0, 10.0, 2.0, "strategy"),
                ("c", "cfg", "risk-rec", "2025-03-06", "2025-03-06T09:00:00", "M", "m2505", "close_long", 2, 110.0, 110.0, 10.0, 2.0, "forced_risk"),
                ("late-o", "cfg", "late-rec", "2025-03-07", "2025-03-07T09:00:00", "RB", "rb2505", "open_long", 1, 200.0, 200.0, 10.0, 1.0, "strategy"),
                ("late-c", "cfg", "late-close", "2025-03-11", "2025-03-11T09:00:00", "RB", "rb2505", "close_long", 1, 210.0, 210.0, 10.0, 1.0, "strategy"),
            ],
        )
        conn.commit()
        conn.close()

        metrics = calculate_futures_transaction_win_rate(
            "cfg",
            db_path,
            start_date="2025-03-05",
            end_date="2025-03-10",
        )

        self.assertEqual(metrics["total_trades"], 1)
        self.assertEqual(metrics["winning_trades"], 1)
        self.assertEqual(metrics["unmatched_close_lots"], 0)
        self.assertEqual(metrics["inherited_close_lots"], 2)
        self.assertEqual(metrics["forced_risk_transaction_count"], 1)
        self.assertAlmostEqual(metrics["realized_trade_pnl"], 196.0)

    def test_attribution_window_uses_pre_window_strategy_origin(self):
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
                (
                    "strategy-rec",
                    "cfg",
                    "2025-03-01",
                    "strategy",
                    "executed",
                    "open_short",
                    1,
                    json.dumps(
                        {
                            "final_action_contract": {
                                "final_action": "open_probe",
                                "reason_codes": [],
                            }
                        }
                    ),
                ),
                ("roll-rec", "cfg", "2025-03-06", "rollover", "executed", "close_short", 1, json.dumps({})),
            ],
        )
        conn.executemany(
            """
            INSERT INTO futures_transactions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("o", "cfg", "strategy-rec", "2025-03-01", "2025-03-01T09:00:00", "HC", "hc2505", "open_short", 1, 300.0, 300.0, 10.0, 1.0, "strategy"),
                ("c", "cfg", "roll-rec", "2025-03-06", "2025-03-06T09:00:00", "HC", "hc2505", "close_short", 1, 290.0, 290.0, 10.0, 1.0, "rollover"),
            ],
        )
        conn.commit()
        conn.close()

        report = build_attribution_report(
            config_id="cfg",
            exp_name="window-attribution",
            db_path=db_path,
            start_date="2025-03-05",
            end_date="2025-03-10",
        )

        self.assertEqual(report["strategy_only_overall"]["total_trades"], 1)
        self.assertAlmostEqual(report["strategy_only_overall"]["total_pnl"], 98.0)
        self.assertEqual(report["strategy_only_trade_pairs"][0]["origin_recommendation_id"], "strategy-rec")
        self.assertEqual(report["rollover_summary"]["transaction_count"], 1)

    def test_portfolio_plotter_loads_margin_utilization_for_deployment_review(self):
        db_path = self._tmp_db()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        config_path = Path(config_dir.name) / "dev.yaml"
        config_path.write_text("exp_name: plot-margin-test\n", encoding="utf-8")

        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE config (
                id TEXT,
                exp_name TEXT
            );
            CREATE TABLE portfolio (
                id TEXT,
                config_id TEXT
            );
            CREATE TABLE daily_settlement (
                portfolio_id TEXT,
                trading_date TEXT,
                previous_balance REAL,
                previous_margin REAL,
                current_balance REAL,
                current_margin REAL,
                margin_ratio REAL,
                daily_pnl REAL,
                commission REAL
            );
            """
        )
        conn.execute("INSERT INTO config VALUES (?, ?)", ("cfg", "plot-margin-test"))
        conn.execute("INSERT INTO portfolio VALUES (?, ?)", ("pf", "cfg"))
        conn.executemany(
            "INSERT INTO daily_settlement VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("pf", "2025-03-03", 100000.0, 0.0, 99600.0, 800.0, 0.008, -300.0, 100.0),
                ("pf", "2025-03-04", 99600.0, 800.0, 99400.0, 4000.0, 0.04, 0.0, 200.0),
            ],
        )
        conn.commit()
        conn.close()

        plotter = PortfolioCurvePlotter(str(config_path), output_dir=config_dir.name, db_path=db_path)

        self.assertTrue(plotter.load_config())
        self.assertTrue(plotter.load_settlement_data())
        self.assertIn("margin_ratio", plotter.settlement_data.columns)
        self.assertAlmostEqual(float(plotter.settlement_data["margin_ratio"].iloc[0]), 0.008)
        self.assertAlmostEqual(float(plotter.settlement_data["margin_ratio"].iloc[1]), 0.04)


if __name__ == "__main__":
    unittest.main()
