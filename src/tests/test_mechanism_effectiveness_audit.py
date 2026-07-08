import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_mechanism_effectiveness_audit import audit_mechanism_effectiveness


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


class MechanismEffectivenessAuditBoundaryTest(unittest.TestCase):
    def _make_db(self) -> Path:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = Path(tmpdir.name) / "agentquant.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE config (
                    id TEXT PRIMARY KEY,
                    exp_name TEXT NOT NULL
                );
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL
                );
                CREATE TABLE futures_recommendation (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    reference_portfolio_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    effective_trade_date TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    underlying_code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT,
                    signal_snapshot_sha256 TEXT,
                    audit_payload TEXT,
                    audit_payload_artifact_path TEXT,
                    audit_payload_sha256 TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE futures_intraday_decision (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    recommendation_id TEXT,
                    ticker TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    trigger_reason TEXT,
                    features_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE alpha_setup_action_value (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    ticker TEXT NOT NULL DEFAULT '*',
                    side TEXT NOT NULL DEFAULT '*',
                    action_name TEXT NOT NULL,
                    sample_count INTEGER DEFAULT 0,
                    reward_sum REAL DEFAULT 0,
                    action_preference TEXT DEFAULT '',
                    reward_source TEXT DEFAULT '',
                    consumer_scope TEXT DEFAULT 'pm_learning',
                    action_value_lane TEXT DEFAULT '',
                    last_sample_date TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    payload_json TEXT
                );
                CREATE TABLE daily_settlement (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    current_balance REAL NOT NULL,
                    current_margin REAL DEFAULT 0,
                    margin_ratio REAL DEFAULT 0,
                    daily_pnl REAL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE ticker_daily_pnl (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    daily_pnl REAL NOT NULL,
                    new_position_pnl REAL DEFAULT 0,
                    commission REAL DEFAULT 0,
                    lots REAL DEFAULT 0,
                    entry_price REAL DEFAULT 0,
                    settle_price REAL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute("INSERT INTO config(id, exp_name) VALUES ('cfg', 'test-exp')")
            conn.execute("INSERT INTO portfolio(id, config_id) VALUES ('pf', 'cfg')")
            conn.commit()
        finally:
            conn.close()
        return db_path

    def _contract(self, **overrides) -> dict:
        contract = {
            "contract_type": "strategy",
            "ticker": "J",
            "current_lots": 0,
            "target_lots": 0,
            "lots_delta": 0,
            "final_action": "wait",
            "reason_codes": ["non_new_risk_no_capital_rank"],
            "evidence_used": {},
            "capital_deployment": {
                "selected_for_capital_deployment": False,
                "new_risk_rank_required": False,
                "deployment_required": False,
                "capital_allocation_reason": "non_new_risk_no_capital_rank",
                "original_target_lots": 0,
                "deployed_target_lots": 0,
                "deployed_lots_delta": 0,
            },
            "learning_used": {},
        }
        contract.update(overrides)
        return contract

    def _snapshot(
        self,
        contract: dict,
        *,
        signal_contract: dict | None = None,
        pm_self_check_ok: bool = True,
        generation_check_ok: bool = True,
    ) -> dict:
        return {
            "final_action_contract": contract,
            "signal_collection_contract": signal_contract
            if signal_contract is not None
            else {
                "producer": "signal_collector",
                "collector_decision_boundary": "no_trade_authority",
            },
            "pm_six_step_trace": {
                "pm_contract_self_check": {"ok": pm_self_check_ok, "errors": [] if pm_self_check_ok else ["fixture_failure"]},
                "step6_contract_generation_check": {
                    "ok": generation_check_ok,
                    "errors": [] if generation_check_ok else ["fixture_generation_failure"],
                },
            },
        }

    def _insert_recommendation(
        self,
        db_path: Path,
        *,
        rec_id: str = "rec-1",
        ticker: str = "J",
        snapshot: dict,
        audit_payload: dict | None = None,
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date,
                    effective_trade_date, source_type, underlying_code,
                    action, lots, signal_snapshot, audit_payload, status, created_at
                )
                VALUES (?, 'cfg', 'pf', '2025-03-25', '2025-03-25',
                    'strategy', ?, 'hold', 0, ?, ?, 'pending', '2025-03-25T09:00:00')
                """,
                (rec_id, ticker, _dumps(snapshot), _dumps(audit_payload or {})),
            )
            conn.commit()
        finally:
            conn.close()

    def test_accepts_signed_pm_contract_without_rejudging_pm_reason(self):
        db_path = self._make_db()
        self._insert_recommendation(db_path, snapshot=self._snapshot(self._contract()))

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.counts.get("scenarios", {}).get("flat_wait"), 1)

    def test_rejects_missing_signal_collection_contract(self):
        db_path = self._make_db()
        snapshot = self._snapshot(self._contract(), signal_contract={})
        self._insert_recommendation(db_path, snapshot=snapshot)

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("mechanism_signal_collection_contract_missing") for error in report.hard_failures),
            report.to_dict(),
        )

    def test_rejects_invalid_signal_collection_boundary(self):
        db_path = self._make_db()
        snapshot = self._snapshot(
            self._contract(),
            signal_contract={"producer": "portfolio_manager", "collector_decision_boundary": "trade_authority"},
        )
        self._insert_recommendation(db_path, snapshot=snapshot)

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        joined = "\n".join(report.hard_failures)
        self.assertIn("mechanism_signal_collection_contract_invalid_producer", joined)
        self.assertIn("mechanism_signal_collection_contract_invalid_boundary", joined)

    def test_rejects_failed_pm_trace_checks(self):
        db_path = self._make_db()
        snapshot = self._snapshot(self._contract(), pm_self_check_ok=False, generation_check_ok=False)
        self._insert_recommendation(db_path, snapshot=snapshot)

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        joined = "\n".join(report.hard_failures)
        self.assertIn("mechanism_pm_contract_self_check_failed", joined)
        self.assertIn("mechanism_pm_step6_generation_check_failed", joined)

    def test_conditional_contract_requires_trader_intraday_result_when_not_blocked(self):
        db_path = self._make_db()
        contract = self._contract(
            final_action="conditional_probe",
            target_lots=1,
            lots_delta=1,
            requires_intraday_confirmation=True,
            conditional_trigger_authority=True,
            can_execute_without_intraday_trigger=False,
            execution_requirement="intraday_trigger_required",
        )
        self._insert_recommendation(
            db_path,
            rec_id="conditional-1",
            ticker="CU",
            snapshot=self._snapshot(contract),
            audit_payload={"producer": "auditor", "audit_verdict": "approve"},
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("mechanism_conditional_probe_missing_intraday_result") for error in report.hard_failures),
            report.to_dict(),
        )

    def test_conditional_contract_with_intraday_result_passes_chain_check(self):
        db_path = self._make_db()
        contract = self._contract(
            final_action="conditional_probe",
            target_lots=1,
            lots_delta=1,
            requires_intraday_confirmation=True,
            conditional_trigger_authority=True,
            can_execute_without_intraday_trigger=False,
            execution_requirement="intraday_trigger_required",
        )
        self._insert_recommendation(
            db_path,
            rec_id="conditional-1",
            ticker="CU",
            snapshot=self._snapshot(contract),
            audit_payload={"producer": "auditor", "audit_verdict": "approve"},
        )
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO futures_intraday_decision(
                    id, config_id, trading_date, recommendation_id, ticker,
                    decision, trigger_reason, features_json, created_at
                )
                VALUES (
                    'id1', 'cfg', '2025-03-25', 'conditional-1', 'CU',
                    'skip', 'not_triggered', '{}', '2025-03-25T10:00:00'
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())


if __name__ == "__main__":
    unittest.main()
