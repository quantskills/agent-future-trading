import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from database import sqlite_setup
from tools.agent_tools.control.pg_system_invariants import DAILY_CHECK_NAMES, audit_system_invariants


DAY = "2025-03-10"
CONFIG_ID = "cfg"
PORTFOLIO_ID = "pf"


class DailySystemInvariantAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connections = []
        self.db_path = Path(self.temp.name) / "agentquant.db"
        old_path = sqlite_setup.DB_PATH
        try:
            sqlite_setup.DB_PATH = str(self.db_path)
            sqlite_setup.init_database()
        finally:
            sqlite_setup.DB_PATH = old_path
        self._seed_base()

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.temp.cleanup()

    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        self.connections.append(connection)
        return connection

    def _seed_base(self):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO config(id,exp_name,updated_at,tickers,has_planner,llm_model,llm_provider) VALUES(?,?,?,?,?,?,?)",
                (CONFIG_ID, "daily-pg-test", DAY, '["RB"]', 0, "test", "test"),
            )
            conn.execute(
                "INSERT INTO portfolio(id,config_id,trading_date,cashflow,account_equity,cash_available,total_assets,positions,margin_used,available_cash,daily_settlement_pnl,margin_ratio,risk_status,last_settle_date,is_settled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (PORTFOLIO_ID, CONFIG_ID, DAY, 1000.0, 1000.0, 1000.0, 1000.0, "{}", 0.0, 1000.0, 0.0, 0.0, "NORMAL", DAY, 1),
            )
            for index, phase in enumerate(("phase1", "phase2", "phase3", "phase4"), 1):
                conn.execute(
                    "INSERT INTO trading_day_phase(id,config_id,trading_date,phase,status,started_at,completed_at,message) VALUES(?,?,?,?,?,?,?,?)",
                    (f"{phase}-{DAY}", CONFIG_ID, DAY, phase, "completed", f"{DAY} 0{index}:00:00", f"{DAY} 0{index}:30:00", "ok"),
                )
            conn.execute(
                "INSERT INTO learning_event_log(id,config_id,trading_date,event_type,agent,scope_type,scope_key,evidence_json,action_json,verifier,created_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"researcher_learning_completed:{CONFIG_ID}:{DAY}", CONFIG_ID, DAY, "researcher_learning_completed", "researcher", "trading_day", DAY, "{}", "{}", "deterministic_researcher_entry", DAY, "applied"),
            )
            for analyst in ("technical", "fundamental", "commodity_news"):
                artifact = {
                    "metadata": {
                        "action_evidence_contract": {
                            "contract_version": "agentquant.action_evidence.v1",
                            "analyst": analyst,
                        }
                    }
                }
                conn.execute(
                    "INSERT INTO signal(id,portfolio_id,updated_at,ticker,llm_prompt,analyst,signal,justification,artifact_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"signal-{analyst}", PORTFOLIO_ID, DAY, "RB", "prompt", analyst, "neutral", "test", json.dumps(artifact)),
                )
            self._insert_recommendation(conn)
            self._replace_settlement(conn, daily_pnl=0.0, commission=0.0, previous_equity=1000.0, current_equity=1000.0, previous_margin=0.0, current_margin=0.0)

    def _snapshot(self, *, actual_transactions=None, outcome="executed_without_transaction", transaction_count=0):
        return {
            "signal_collection_contract": {"contract_version": "agentquant.signal_collection.v1"},
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "RB",
                "final_action": "wait",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
            },
            "pm_six_step_trace": {"private_internal_marker": "daily_pg_must_ignore"},
            "auditor": {"audit_verdict": "approve"},
            "execution_result": {
                "outcome": outcome,
                "status": "completed",
                "transaction_count": transaction_count,
                "actual_transactions": list(actual_transactions or []),
                "no_trade_reason": None if actual_transactions else "position_matched",
            },
        }

    def _insert_recommendation(self, conn, *, rec_id="rec", source_type="strategy", verdict="approve", snapshot=None):
        payload = snapshot or self._snapshot()
        payload.setdefault("auditor", {})["audit_verdict"] = verdict
        audit_payload = {"audit_verdict": verdict}
        conn.execute(
            "INSERT INTO futures_recommendation(id,config_id,reference_portfolio_id,trading_date,effective_trade_date,source_type,underlying_code,contract_code,action,lots,signal_snapshot,audit_payload,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec_id, CONFIG_ID, PORTFOLIO_ID, DAY, DAY, source_type, "RB", "RB2505", "hold", 0, json.dumps(payload), json.dumps(audit_payload), "pending", DAY),
        )

    def _insert_transaction(self, conn, *, rec_id="rec", source_type="strategy", action="open_long", lots=1):
        conn.execute(
            "INSERT INTO futures_transactions(id,portfolio_id,config_id,recommendation_id,trading_date,ticker,contract_code,action,lots,execution_price,contract_multiplier,margin_rate,margin_used,commission,source_type,booked_in_settlement,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"tx-{rec_id}-{action}", PORTFOLIO_ID, CONFIG_ID, rec_id, DAY, "RB", "RB2505", action, lots, 3500.0, 10.0, 0.1, 3500.0, 2.0, source_type, 1, DAY),
        )

    def _replace_settlement(self, conn, *, daily_pnl, commission, previous_equity, current_equity, previous_margin, current_margin):
        conn.execute("DELETE FROM daily_settlement WHERE portfolio_id=? AND trading_date=?", (PORTFOLIO_ID, DAY))
        previous_balance = previous_equity - previous_margin
        current_balance = current_equity - current_margin
        conn.execute(
            "INSERT INTO daily_settlement(id,portfolio_id,trading_date,previous_balance,current_balance,previous_account_equity,current_account_equity,cash_available,reserved_margin,previous_margin,current_margin,daily_pnl,commission,margin_ratio,positions_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("settle", PORTFOLIO_ID, DAY, previous_balance, current_balance, previous_equity, current_equity, current_balance, current_margin, previous_margin, current_margin, daily_pnl, commission, 0.0, "{}", DAY),
        )
        conn.execute(
            "UPDATE portfolio SET cashflow=?, account_equity=?, cash_available=?, total_assets=?, margin_used=?, available_cash=?, daily_settlement_pnl=? WHERE id=?",
            (current_balance, current_equity, current_balance, current_equity, current_margin, current_balance, daily_pnl, PORTFOLIO_ID),
        )
        conn.execute("DELETE FROM ticker_daily_pnl WHERE portfolio_id=? AND trading_date=?", (PORTFOLIO_ID, DAY))
        conn.execute(
            "INSERT INTO ticker_daily_pnl(id,portfolio_id,trading_date,ticker,daily_pnl,commission,holding_pnl,new_position_pnl,close_pnl,position_type,lots,entry_price,settle_price,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ticker-pnl", PORTFOLIO_ID, DAY, "RB", daily_pnl, commission, daily_pnl, 0.0, 0.0, "FLAT", 0.0, 0.0, 0.0, DAY),
        )

    def _audit(self):
        return audit_system_invariants(
            db_path=self.db_path,
            config_id=CONFIG_ID,
            start_date=DAY,
            end_date=DAY,
        )

    def _check(self, report, name):
        return next(check for check in report.checks if check.check_name == name)

    def test_clean_legal_no_trade_day_passes_all_seven_checks(self):
        report = self._audit()
        self.assertTrue(report.passed, report.to_dict())
        self.assertEqual([check.check_name for check in report.checks], list(DAILY_CHECK_NAMES))

    def test_missing_phase_is_hard_failure(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM trading_day_phase WHERE phase='phase4'")
        report = self._audit()
        self.assertIn("daily_phase_not_completed", self._check(report, "daily_phase_completion").violation_codes)

    def test_blocked_strategy_recommendation_cannot_trade(self):
        with self._connect() as conn:
            conn.execute("UPDATE futures_recommendation SET audit_payload=? WHERE id='rec'", (json.dumps({"audit_verdict": "block"}),))
            self._insert_transaction(conn)
        report = self._audit()
        self.assertIn("blocked_strategy_recommendation_has_transaction", self._check(report, "audit_release_and_execution_result").violation_codes)

    def test_approved_recommendation_without_trade_is_legal(self):
        report = self._audit()
        check = self._check(report, "audit_release_and_execution_result")
        self.assertEqual(check.status, "passed")
        self.assertIn("approved_strategy_without_transaction", check.diagnostic_codes)

    def test_execution_result_must_match_transaction_fact(self):
        actual = [{"action": "open_long", "lots": 1, "contract_code": "RB2505"}]
        with self._connect() as conn:
            conn.execute("UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec'", (json.dumps(self._snapshot(actual_transactions=actual, outcome="executed", transaction_count=1)),))
            self._insert_transaction(conn, lots=2)
        report = self._audit()
        self.assertIn("execution_result_transaction_fact_mismatch", self._check(report, "execution_and_transaction_fact").violation_codes)

    def test_settlement_formula_is_checked_without_budget_thresholds(self):
        with self._connect() as conn:
            self._replace_settlement(conn, daily_pnl=10.0, commission=0.0, previous_equity=1000.0, current_equity=900.0, previous_margin=0.0, current_margin=0.0)
        report = self._audit()
        self.assertIn("settlement_equity_formula_mismatch", self._check(report, "settlement_and_account_fact").violation_codes)

    def test_rollover_and_forced_risk_use_their_own_legal_sources(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM futures_recommendation")
            rollover_actual = [{"action": "close_long", "lots": 1, "contract_code": "RB2505"}]
            rollover_snapshot = self._snapshot(actual_transactions=rollover_actual, outcome="executed", transaction_count=1)
            rollover_snapshot["rollover_policy"] = {"from_contract": "RB2505", "to_contract": "RB2510"}
            self._insert_recommendation(conn, rec_id="roll", source_type="rollover", snapshot=rollover_snapshot)
            self._insert_transaction(conn, rec_id="roll", source_type="rollover", action="close_long")
            forced_actual = [{"action": "close_long", "lots": 1, "contract_code": "RB2505"}]
            self._insert_recommendation(conn, rec_id="risk", source_type="forced_risk", snapshot=self._snapshot(actual_transactions=forced_actual, outcome="executed", transaction_count=1))
            self._insert_transaction(conn, rec_id="risk", source_type="forced_risk", action="close_long")
            self._replace_settlement(conn, daily_pnl=4.0, commission=4.0, previous_equity=1000.0, current_equity=1000.0, previous_margin=0.0, current_margin=0.0)
        report = self._audit()
        self.assertEqual(self._check(report, "single_trade_fact_source").status, "passed", report.to_dict())

    def test_future_dated_learning_is_rejected_but_learning_is_not_required_per_trade(self):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO alpha_setup_sample(id,config_id,trading_date,ticker,side,sector,horizon_class,market_regime,setup_type,data_combo,scope_key,source_type,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("sample", CONFIG_ID, "2025-03-11", "RB", "long", "ferrous", "daily", "trend", "breakout", "all", "key", "trade", DAY),
            )
        report = self._audit()
        self.assertIn("future_dated_learning_record_detected", self._check(report, "learning_record_landing_boundary").violation_codes)

    def test_report_does_not_expose_internal_details(self):
        payload = self._audit().to_dict()
        self.assertEqual(set(payload), {"contract_version", "source_agent", "status", "checks"})
        serialized = json.dumps(payload)
        self.assertNotIn("pm_six_step_trace", serialized)
        self.assertNotIn("rank", serialized)
        self.assertNotIn("metadata", serialized)


if __name__ == "__main__":
    unittest.main()
