import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_system_invariants import audit_system_invariants


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


def _auditor_approval_payload():
    return {
        "producer": "auditor",
        "agent_name": "auditor",
        "contract_version": "agentquant.audit_verdict.v1",
        "audit_verdict": "approve",
        "hard_risk_reasons": [],
        "soft_risk_reasons": [],
        "audited_by": "auditor",
        "boundary": {
            "auditor_does_not_modify_final_action_contract": True,
            "auditor_does_not_create_trade_authority": True,
            "trader_requires_approved_audit_verdict": True,
        },
    }


def _with_auditor_approval(payload):
    result = dict(payload or {})
    auditor = _auditor_approval_payload()
    result.setdefault("auditor", dict(auditor))
    result["independent_auditor"] = dict(auditor)
    return result


def _transaction_audit_payload(payload):
    contract = payload.get("final_action_contract") if isinstance(payload, dict) else {}
    audit = payload.get("trade_contract_audit") if isinstance(payload, dict) else {}
    result = {
        "trade_contract_audit": {
            "single_source_of_trade_truth": bool((audit or {}).get("single_source_of_trade_truth", True)),
            "candidate_sources_do_not_bypass_contract": bool((audit or {}).get("candidate_sources_do_not_bypass_contract", True)),
            "execution_requirement": (audit or {}).get("execution_requirement") or (contract or {}).get("execution_requirement"),
            "final_action": (contract or {}).get("final_action"),
            "current_lots": (contract or {}).get("current_lots"),
            "target_lots": (contract or {}).get("target_lots"),
            "lots_delta": (contract or {}).get("lots_delta"),
        }
    }
    translation = payload.get("execution_translation") if isinstance(payload, dict) else None
    if isinstance(translation, dict):
        result["execution_translation"] = translation
    phase2 = payload.get("phase2_execution") if isinstance(payload, dict) else None
    if isinstance(phase2, dict):
        result["phase2_execution"] = phase2
    independent_auditor = payload.get("independent_auditor") if isinstance(payload, dict) else None
    if isinstance(independent_auditor, dict):
        result["independent_auditor"] = dict(independent_auditor)
    return result


class SystemInvariantAuditRegressionTest(unittest.TestCase):
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
                    exp_name TEXT NOT NULL,
                    updated_at TEXT,
                    tickers TEXT NOT NULL,
                    has_planner INTEGER DEFAULT 0,
                    llm_model TEXT,
                    llm_provider TEXT
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
                CREATE TABLE futures_transactions (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    config_id TEXT,
                    recommendation_id TEXT,
                    trading_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    contract_code TEXT,
                    action TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    execution_price REAL NOT NULL,
                    contract_multiplier REAL NOT NULL,
                    margin_rate REAL NOT NULL,
                    margin_used REAL NOT NULL,
                    audit_payload TEXT,
                    audit_payload_artifact_path TEXT,
                    audit_payload_sha256 TEXT,
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
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    current_balance REAL DEFAULT 0
                );
                CREATE TABLE daily_settlement (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    daily_pnl REAL DEFAULT 0,
                    commission REAL DEFAULT 0,
                    current_balance REAL DEFAULT 0,
                    current_margin REAL DEFAULT 0,
                    margin_ratio REAL DEFAULT 0,
                    positions_snapshot TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE alpha_setup_action_value (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    ticker TEXT NOT NULL DEFAULT '*',
                    side TEXT NOT NULL DEFAULT '*',
                    horizon_class TEXT NOT NULL DEFAULT '*',
                    market_regime TEXT NOT NULL DEFAULT '*',
                    setup_type TEXT NOT NULL DEFAULT '*',
                    data_combo TEXT NOT NULL DEFAULT '*',
                    action_name TEXT NOT NULL,
                    sample_count INTEGER DEFAULT 0,
                    reward_sum REAL DEFAULT 0,
                    reward_mean REAL DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    confidence_score REAL DEFAULT 0,
                    action_preference TEXT DEFAULT '',
                    reward_source TEXT DEFAULT '',
                    evidence_scope TEXT DEFAULT '',
                    action_value_lane TEXT DEFAULT '',
                    consumer_scope TEXT DEFAULT 'pm_learning',
                    learning_lane TEXT DEFAULT '',
                    retrieval_key TEXT DEFAULT '',
                    fallback_retrieval_key TEXT DEFAULT '',
                    execution_retrieval_key TEXT DEFAULT '',
                    max_position_impact REAL DEFAULT 0,
                    last_sample_date TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    valid_until TEXT,
                    active INTEGER DEFAULT 1,
                    payload_json TEXT
                );
                CREATE TABLE adaptive_policy_state (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    ticker TEXT,
                    side TEXT,
                    setup_type TEXT,
                    horizon_class TEXT,
                    market_regime TEXT,
                    policy_type TEXT,
                    policy_action TEXT,
                    multiplier REAL DEFAULT 1,
                    confidence_score REAL DEFAULT 0,
                    sample_count INTEGER DEFAULT 0,
                    reason TEXT,
                    source_event_id TEXT,
                    source_trading_date TEXT,
                    valid_until TEXT,
                    active INTEGER DEFAULT 1,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE researcher_llm_notes (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    evidence_pack_id TEXT,
                    ticker TEXT,
                    raw_prompt TEXT,
                    raw_response TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE trading_day_phase (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    message TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO config(id, exp_name, updated_at, tickers, llm_model, llm_provider) VALUES (?, ?, ?, ?, ?, ?)",
                ("cfg", "agentquant-test", datetime.utcnow().isoformat(), _dumps(["RB"]), "fake", "fake"),
            )
            conn.execute(
                "INSERT INTO portfolio(id, config_id, current_balance) VALUES (?, ?, ?)",
                ("pf", "cfg", 1000000.0),
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def _insert_good_open(self, db_path: Path):
        contract = {
            "contract_type": "strategy",
            "final_action": "open_real",
            "authority_type": "real_budget_entry",
            "authority_decision": "allow_real_new_entry",
            "open_action_evidence": True,
            "strong_current_evidence": True,
            "tradeable_state": True,
            "watch_for_trigger_block": False,
            "negative_profile": False,
            "current_lots": 0,
            "target_lots": -2,
            "lots_delta": -2,
            "execution_requirement": "intraday_trigger_required",
            "reason_codes": ["positive_candidate_open"],
            "evidence_used": {
                "opportunity_score_components": {
                    "positive_learning": 0.12,
                    "negative_learning": 0.0,
                    "execution_profile_learning": 0.0,
                    "recent_tail_loss_penalty": 0.0,
                }
            },
            "learning_used": {
                "alpha_setup_action_values": [
                    {
                        "action_preference": "positive_candidate_open",
                        "reward_sum": 1200.0,
                        "reward_mean": 1200.0,
                        "win_rate": 1.0,
                        "reward_source": "trade_episode",
                        "evidence_scope": "exact_real_state",
                        "action_value_lane": "open",
                        "consumer_scope": "pm_learning",
                        "learning_lane": "open",
                        "retrieval_match_level": "exact_state",
                    }
                ]
            },
        }
        payload = {
            "final_action_contract": contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "execution_requirement": "intraday_trigger_required",
            },
            "execution_translation": {"intraday_execution": {"trigger_passed": True}},
        }
        payload = _with_auditor_approval(payload)
        transaction_payload = {
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "execution_requirement": "intraday_trigger_required",
                "final_action": "open_real",
                "current_lots": 0,
                "target_lots": -2,
                "lots_delta": -2,
            },
            "execution_translation": {"intraday_execution": {"trigger_passed": True}},
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec1", "cfg", "pf", "2025-03-03", "2025-03-03", "strategy", "RB", "open_short", 2, _dumps(payload), _dumps(payload), "executed", datetime.utcnow().isoformat()),
            )
            conn.execute(
                """
                INSERT INTO futures_transactions(
                    id, portfolio_id, config_id, recommendation_id, trading_date, ticker, action, lots,
                    execution_price, contract_multiplier, margin_rate, margin_used, audit_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tx1", "pf", "cfg", "rec1", "2025-03-03", "RB", "open_short", 2, 3300.0, 10.0, 0.1, 6600.0, _dumps(transaction_payload), datetime.utcnow().isoformat()),
            )
            conn.execute(
                """
                INSERT INTO futures_intraday_decision(
                    id, config_id, trading_date, recommendation_id, ticker, decision, trigger_reason, features_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("intra1", "cfg", "2025-03-03", "rec1", "RB", "execute", "intraday_trigger_confirmed", _dumps({"trigger_passed": True}), datetime.utcnow().isoformat()),
            )
            conn.execute(
                """
                INSERT INTO alpha_setup_action_value(
                    id, config_id, scope_key, ticker, side, setup_type, action_name, sample_count,
                    reward_sum, action_preference, last_sample_date, created_at, updated_at, active, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "av1",
                    "cfg",
                    "RB|short|trend_breakout",
                    "RB",
                    "short",
                    "trend_breakout",
                    "open",
                    1,
                    1200.0,
                    "positive_candidate_open",
                    "2025-03-03",
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat(),
                    1,
                    _dumps({"action_preference": "positive_candidate_open", "amplification_scope_quality": "exact_real_state", "reward_source": "trade_episode"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_system_invariant_audit_reports_schema_contract_errors_without_sql_crash(self):
        db_path = self._make_db()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE researcher_llm_notes")
            conn.execute(
                """
                CREATE TABLE researcher_llm_notes (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    source_trading_date TEXT,
                    ticker TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "schema_missing_required_column:researcher_llm_notes:trading_date",
            report.errors,
            report.to_dict(),
        )
        self.assertIn("data_time_boundary", report.metadata["failed_categories"])

    def _mutate_recommendation_payload(self, db_path: Path, mutator):
        conn = sqlite3.connect(db_path)
        try:
            payload = json.loads(conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()[0])
            mutator(payload)
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

    def test_system_invariant_audit_accepts_authorized_triggered_open(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.counts["open_transactions"], 1)
        self.assertIn(
            "recommendation_top_level_action_lots_must_match_final_contract",
            report.metadata["pm_learning_ranking_audit_boundaries"],
        )

    def test_system_invariant_audit_accepts_empty_database_before_fresh_backtest(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = Path(tmpdir.name) / "agentquant.db"
        sqlite3.connect(db_path).close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertTrue(report.ok, report.to_dict())
        self.assertIn("empty_db_no_invariant_records_to_audit", report.metadata.get("record_boundary", ""))
        self.assertTrue(any(item.startswith("config_not_found_empty_db:") for item in report.warnings))

    def test_runtime_execution_learning_trace_writes_use_contract_builder(self):
        production_files = [
            SRC_ROOT / "util" / "futures_audit.py",
            SRC_ROOT / "agents" / "execution_team" / "trader.py",
            SRC_ROOT / "tools" / "agent_tools" / "execution" / "trader_futures_execution.py",
            SRC_ROOT / "tools" / "agent_tools" / "research" / "reviewer_phase4_review.py",
        ]

        for path in production_files:
            text = path.read_text(encoding="utf-8")
            self.assertIn("build_execution_learning_trace", text, str(path))
            self.assertNotIn('["execution_learning_trace"] = {', text, str(path))
            self.assertNotIn('"execution_learning_trace": {', text, str(path))

    def test_system_invariant_audit_rejects_pm_action_value_trace_missing_reward_scope(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = json.loads(conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()[0])
            payload["final_action_contract"]["learning_used"]["alpha_setup_action_values"] = [
                {"action_preference": "positive_candidate_open"}
            ]
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("pm_action_value_missing_canonical_fields") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_pm_consuming_trader_execution_learning(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_real",
                    "authority_type": "real_budget_entry",
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "execution_requirement": "intraday_trigger_required",
                    "evidence_used": {
                        "opportunity_score_components": {
                            "positive_learning": 0.12,
                            "negative_learning": 0.0,
                            "execution_profile_learning": 0.0,
                            "recent_tail_loss_penalty": 0.0,
                        }
                    },
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "action_preference": "positive_candidate_execution",
                                "reward_sum": 1200.0,
                                "reward_mean": 1200.0,
                                "win_rate": 1.0,
                                "reward_source": "trade_episode",
                                "evidence_scope": "exact_real_state",
                                "action_value_lane": "execution",
                                "consumer_scope": "trader_execution_learning",
                            }
                        ]
                    },
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(
            any(error.startswith("pm_consumed_non_pm_learning_action_value") for error in report.errors),
            report.to_dict(),
        )
        self.assertIn(
            "learning_components_only_inside_opportunity_score_components",
            report.metadata["pm_learning_ranking_audit_boundaries"],
        )

    def test_system_invariant_audit_rejects_bare_execution_learning_trace(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = json.loads(conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()[0])
            payload["execution_result"] = {
                "status": "skipped",
                "outcome": "skipped",
                "transaction_count": 0,
                "no_trade_reason": "hold_or_zero_lots",
                "execution_learning_trace": {
                    "no_trade_reason": "hold_or_zero_lots",
                    "turn_into_memory": True,
                },
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("trader_execution_learning_trace_missing_scope") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_accepts_execution_result_without_learning_trace(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = json.loads(conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()[0])
            payload["execution_result"] = {
                "status": "skipped",
                "outcome": "skipped",
                "transaction_count": 0,
                "no_trade_reason": "hold_or_zero_lots",
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(
            not any(error.startswith("trader_execution_learning_trace_missing_scope") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_learning_signal_without_contract_effect_or_reason(self):
        db_path = self._make_db()
        payload = {
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "wait",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "authority_type": "not_applicable",
                "reason_codes": ["learning_signal_seen"],
                "evidence_used": {
                    "opportunity_score_components": {
                        "positive_learning": 0.12,
                        "negative_learning": 0.0,
                        "execution_profile_learning": 0.0,
                        "recent_tail_loss_penalty": 0.0,
                    }
                },
                "learning_used": {
                    "learning_adjustment_summary": {
                        "positive_learning_signal": 0.12,
                        "negative_learning_signal": 0.0,
                        "execution_profile_learning_signal": 0.0,
                        "recent_tail_loss_signal": 0.0,
                    }
                },
            }
        }
        payload = _with_auditor_approval(payload)
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec-learning-no-effect", "cfg", "pf", "2025-03-04", "2025-03-04", "strategy", "RB", "hold", 0, _dumps(payload), _dumps(payload), "skipped", now),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("pm_learning_signal_without_contract_effect_or_explanation") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_accepts_learning_signal_no_lot_change_with_capital_reason(self):
        db_path = self._make_db()
        payload = {
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "wait",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "authority_type": "not_applicable",
                "reason_codes": ["capital_queue_not_selected"],
                "evidence_used": {
                    "capital_allocation_reason": "capital_queue_not_selected_after_full_market_ranking",
                    "opportunity_score_components": {
                        "positive_learning": 0.12,
                        "negative_learning": 0.0,
                        "execution_profile_learning": 0.0,
                        "recent_tail_loss_penalty": 0.0,
                    },
                },
                "capital_deployment": {
                    "selected_for_capital_deployment": False,
                    "capital_allocation_reason": "capital_queue_not_selected_after_full_market_ranking",
                    "original_target_lots": 1,
                    "deployed_target_lots": 0,
                    "deployed_lots_delta": 0,
                },
                "learning_used": {
                    "learning_adjustment_summary": {
                        "positive_learning_signal": 0.12,
                        "negative_learning_signal": 0.0,
                        "execution_profile_learning_signal": 0.0,
                        "recent_tail_loss_signal": 0.0,
                    }
                },
            }
        }
        payload = _with_auditor_approval(payload)
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec-learning-explained", "cfg", "pf", "2025-03-04", "2025-03-04", "strategy", "RB", "hold", 0, _dumps(payload), _dumps(payload), "skipped", now),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_rejects_rank_without_contract_effect_or_reason(self):
        db_path = self._make_db()
        payload = {
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "wait",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "authority_type": "not_applicable",
                "reason_codes": ["pm_full_market_capital_deployment"],
                "evidence_used": {
                    "opportunity_rank": 1,
                    "opportunity_score_components": {
                        "positive_learning": 0.08,
                        "negative_learning": 0.0,
                        "execution_profile_learning": 0.0,
                        "recent_tail_loss_penalty": 0.0,
                    },
                },
                "capital_deployment": {
                    "selected_for_capital_deployment": True,
                    "opportunity_rank": 1,
                    "original_target_lots": 0,
                    "deployed_target_lots": 0,
                    "deployed_lots_delta": 0,
                },
                "learning_used": {
                    "learning_adjustment_summary": {
                        "positive_learning_signal": 0.08,
                    }
                },
            }
        }
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec-rank-no-effect", "cfg", "pf", "2025-03-04", "2025-03-04", "strategy", "RB", "hold", 0, _dumps(payload), _dumps(payload), "skipped", now),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("pm_rank_changed_without_contract_effect") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_hold_exit_learning_without_effect_or_reason(self):
        db_path = self._make_db()
        payload = {
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "hold",
                "current_lots": -2,
                "target_lots": -2,
                "lots_delta": 0,
                "authority_type": "position_lifecycle",
                "reason_codes": ["position_matched_without_learning_detail"],
                "evidence_used": {
                    "opportunity_score_components": {
                        "positive_learning": 0.0,
                        "negative_learning": -0.12,
                        "execution_profile_learning": 0.0,
                        "recent_tail_loss_penalty": -0.08,
                    }
                },
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "action_preference": "tail_loss_protect",
                            "reward_sum": -2400.0,
                            "reward_mean": -1200.0,
                            "win_rate": 0.0,
                            "reward_source": "trade_episode",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "exit",
                        }
                    ],
                    "learning_adjustment_summary": {
                        "negative_learning_signal": -0.12,
                        "recent_tail_loss_signal": -0.08,
                    },
                },
            }
        }
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec-hold-exit-no-effect", "cfg", "pf", "2025-03-04", "2025-03-04", "strategy", "RB", "hold", 0, _dumps(payload), _dumps(payload), "skipped", now),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("pm_hold_exit_learning_without_contract_effect_or_explanation") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_accepts_hold_exit_learning_landed_in_exit_contract(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "contract_type": "strategy",
            "ticker": "M",
            "final_action": "exit",
            "current_lots": 4,
            "target_lots": 0,
            "lots_delta": -4,
            "authority_type": "position_lifecycle",
            "reason_codes": [
                "flat_target",
                "pm_full_market_capital_deployment",
                "position_lifecycle_loss_revalidation_failed",
            ],
            "execution_requirement": "position_management_or_wait",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
            "evidence_used": {
                "opportunity_score_components": {
                    "positive_learning": 0.0,
                    "negative_learning": 0.0,
                    "execution_profile_learning": 0.0,
                    "recent_tail_loss_penalty": 0.0,
                }
            },
            "learning_used": {
                "alpha_setup_action_values": [
                    {
                        "scope_key": "M|long|trend",
                        "ticker": "M",
                        "side": "long",
                        "action_name": "exit",
                        "action_preference": "tail_loss_protect",
                        "reward_source": "real_trade",
                        "evidence_scope": "exact_real_state",
                        "consumer_scope": "pm_learning",
                        "action_value_lane": "exit",
                        "learning_lane": "exit",
                        "reward_sum": -3721.0,
                        "reward_mean": -3721.0,
                        "sample_count": 1,
                        "win_rate": 0.0,
                        "last_sample_date": "2025-03-03",
                    }
                ]
            },
        }
        payload = {
            "final_action_contract": contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "final_action": "exit",
                "current_lots": 4,
                "target_lots": 0,
                "lots_delta": -4,
            },
        }
        payload = _with_auditor_approval(payload)
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                UPDATE futures_recommendation
                SET underlying_code=?, action=?, lots=?, trading_date=?, effective_trade_date=?,
                    signal_snapshot=?, audit_payload=?
                WHERE id='rec1'
                """,
                ("M", "close_long", 4, "2025-03-06", "2025-03-06", _dumps(payload), _dumps(payload)),
            )
            conn.execute(
                """
                UPDATE futures_transactions
                SET ticker=?, action=?, lots=?, trading_date=?, audit_payload=?
                WHERE id='tx1'
                """,
                ("M", "close_long", 4, "2025-03-06", _dumps(_transaction_audit_payload(payload))),
            )
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET scope_key=?, ticker=?, side=?, setup_type=?, action_name=?, reward_sum=?,
                    reward_mean=?, win_rate=?, action_preference=?, reward_source=?, evidence_scope=?,
                    action_value_lane=?, consumer_scope=?, learning_lane=?, last_sample_date=?, updated_at=?,
                    payload_json=?
                WHERE id='av1'
                """,
                (
                    "M|long|trend",
                    "M",
                    "long",
                    "trend",
                    "exit",
                    -3721.0,
                    -3721.0,
                    0.0,
                    "tail_loss_protect",
                    "real_trade",
                    "exact_real_state",
                    "exit",
                    "pm_learning",
                    "exit",
                    "2025-03-03",
                    now,
                    _dumps(
                        {
                            "action_preference": "tail_loss_protect",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "exit",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_fails_incomplete_trading_day_phase(self):
        db_path = self._make_db()
        payload = {
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "wait",
                "current_lots": 0,
                "target_lots": 0,
                "lots_delta": 0,
                "authority_type": "not_applicable",
                "reason_codes": ["neutral_signal_no_trade"],
            }
        }
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("rec-phase", "cfg", "pf", "2025-03-10", "2025-03-10", "strategy", "RB", "hold", 0, _dumps(payload), _dumps(payload), "skipped", now),
            )
            rows = [
                ("p1", "cfg", "2025-03-10", "phase1", "completed", now, now, ""),
                ("p2", "cfg", "2025-03-10", "phase2", "running", now, None, ""),
            ]
            conn.executemany("INSERT INTO trading_day_phase VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("incomplete_trading_day_phase:2025-03-10:") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_forbidden_old_field_keys_in_recommendation_artifact(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()
            payload = json.loads(row[0])
            payload["action_evidence_contract"] = {
                "opportunity_layer": "tradeable_setup",
                "trigger_valid": True,
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec1'",
                (_dumps(payload),),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("unified_field_artifact_forbidden_field:2025-03-03:RB:rec1:signal_snapshot") for error in report.errors),
            report.to_dict(),
        )
        self.assertTrue(any("action_evidence_contract.opportunity_layer" in error for error in report.errors))
        self.assertIn("unified_field_semantics", report.metadata.get("failed_categories", []))
        semantics = report.metadata.get("unified_field_semantics_audit", {})
        self.assertFalse(semantics.get("ok"), report.to_dict())
        self.assertTrue(
            any(error.startswith("unified_field_artifact_forbidden_field:") for error in semantics.get("errors", [])),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_pending_trigger_marked_valid(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()
            payload = json.loads(row[0])
            payload["technical"] = {
                "trade_research_contract": {
                    "opportunity_state": "probe_candidate",
                    "trigger_valid": True,
                    "entry_trigger": (
                        "In the current range regime, bearish entry timing requires a confirmed break "
                        "below support after the open; without that confirmation, remain on watch."
                    ),
                    "action_evidence_contract": {
                        "opportunity_state": "probe_candidate",
                        "trigger_valid": True,
                        "entry_trigger": (
                            "In the current range regime, bearish entry timing requires a confirmed break "
                            "below support after the open; without that confirmation, remain on watch."
                        ),
                    },
                }
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec1'",
                (_dumps(payload),),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("action_evidence_contract_pending_trigger_marked_valid:") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_research_and_action_trigger_mismatch(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()
            payload = json.loads(row[0])
            payload["technical"] = {
                "trade_research_contract": {
                    "opportunity_state": "watch_for_trigger",
                    "trigger_valid": True,
                    "entry_trigger": "current breakout above opening range is confirmed by volume expansion",
                    "action_evidence_contract": {
                        "opportunity_state": "watch_for_trigger",
                        "trigger_valid": False,
                        "entry_trigger": "current breakout above opening range is confirmed by volume expansion",
                    },
                }
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec1'",
                (_dumps(payload),),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("trade_research_action_evidence_trigger_valid_mismatch:") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_setup_quality_used_as_trigger(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()
            payload = json.loads(row[0])
            payload["technical"] = {
                "trade_research_contract": {
                    "opportunity_state": "tradeable_candidate",
                    "setup_quality_ok": True,
                    "trigger_valid": True,
                    "entry_trigger": "trend_breakout setup below range floor with volume expansion",
                    "action_evidence_contract": {
                        "opportunity_state": "tradeable_candidate",
                        "setup_quality_ok": True,
                        "trigger_valid": True,
                        "entry_trigger": "trend_breakout setup below range floor with volume expansion",
                    },
                }
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec1'",
                (_dumps(payload),),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("setup_quality_ok_used_as_current_trigger:") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_trigger_valid_without_current_confirmation(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()
            payload = json.loads(row[0])
            payload["technical"] = {
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
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec1'",
                (_dumps(payload),),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("trigger_valid_without_current_trigger_confirmed:") for error in report.errors),
            report.to_dict(),
        )
        self.assertIn("unified_field_semantics", report.metadata.get("failed_categories", []))
        self.assertIn("pm_opportunity_routing", report.metadata.get("failed_categories", []))

    def test_system_invariant_audit_fails_conditional_monitor_silent_wait(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
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
            conn.execute(
                "UPDATE futures_recommendation SET action='hold', lots=0, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.execute("DELETE FROM futures_transactions")
            conn.execute("DELETE FROM futures_intraday_decision")
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("conditional_monitor_candidate_silent_wait:") for error in report.errors),
            report.to_dict(),
        )
        self.assertIn("pm_opportunity_routing", report.metadata.get("failed_categories", []))
        self.assertTrue(
            any(
                error.startswith("conditional_monitor_candidate_silent_wait:")
                for error in report.metadata.get("error_categories", {}).get("pm_opportunity_routing", [])
            ),
            report.to_dict(),
        )

    def test_system_invariant_audit_accepts_conditional_monitor_contract(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_probe",
                    "authority_type": "exploration_probe",
                    "authority_decision": "allow_exploration_probe",
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "conditional_trigger_authority": True,
                    "requires_intraday_confirmation": True,
                    "can_execute_without_intraday_trigger": False,
                    "watch_for_trigger_block": False,
                    "reason_codes": ["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                },
                "active_opportunity_audit": {
                    "decision": {
                        "action": "open_short",
                        "lots": 1,
                        "lands_position": True,
                        "authority_type": "exploration_probe",
                        "reason": "conditional_trigger_authority",
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
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            payload = _with_auditor_approval(payload)
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(_transaction_audit_payload(payload)),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_accepts_observation_only_release_block_diagnostics(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_real",
                    "authority_type": "real_budget_entry",
                    "authority_decision": "allow_real_new_entry",
                    "open_action_evidence": True,
                    "strong_current_evidence": True,
                    "tradeable_state": True,
                    "watch_for_trigger_block": False,
                    "negative_profile": False,
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "execution_requirement": "intraday_trigger_required",
                    "reason_codes": ["positive_candidate_open"],
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                },
                "release_block_diagnostics": {
                    "contract_version": "agentquant.release_block_diagnostics.v1",
                    "observation_only": True,
                    "does_not_modify_trade_authority": True,
                    "single_source_of_trade_truth_remains": "final_action_contract",
                    "primary_block_reason": "market_confirmation_below_release_threshold",
                    "blocking_category": "current_confirmation_missing",
                    "evidence_snapshot": {
                        "preferred_side": "short",
                        "preferred_side_layer": "tradeable_candidate",
                        "market_confirmation_score": 0.54,
                    },
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            payload = _with_auditor_approval(payload)
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(_transaction_audit_payload(payload)),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_fails_release_diagnostics_with_trade_action_fields(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_real",
                    "authority_type": "real_budget_entry",
                    "authority_decision": "allow_real_new_entry",
                    "open_action_evidence": True,
                    "strong_current_evidence": True,
                    "tradeable_state": True,
                    "watch_for_trigger_block": False,
                    "negative_profile": False,
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "execution_requirement": "intraday_trigger_required",
                    "reason_codes": ["positive_candidate_open"],
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                },
                "release_block_diagnostics": {
                    "contract_version": "agentquant.release_block_diagnostics.v1",
                    "observation_only": True,
                    "does_not_modify_trade_authority": True,
                    "single_source_of_trade_truth_remains": "final_action_contract",
                    "target_lots": -2,
                    "evidence_snapshot": {"authority_type": "real_budget_entry"},
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(_transaction_audit_payload(payload)),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("release_block_diagnostics_contains_trade_action_fields") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_open_without_authority(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            bad_payload = {
                "final_action_contract": {"final_action": "wait", "authority_type": "watchlist_only"},
                "trade_contract_audit": {"single_source_of_trade_truth": True, "candidate_sources_do_not_bypass_contract": True},
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(bad_payload), _dumps(bad_payload)),
            )
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(_transaction_audit_payload(bad_payload)),))
            conn.commit()
        finally:
            conn.close()
        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(any(error.startswith("open_transaction_without_open_final_action") for error in report.errors))
        self.assertTrue(any(error.startswith("open_transaction_without_open_authority") for error in report.errors))

    def test_system_invariant_audit_requires_top_level_final_action_contract(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            stale_snapshot = {
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(stale_snapshot), _dumps(stale_snapshot)),
            )
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(stale_snapshot),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertFalse(report.ok)
        self.assertTrue(any(error.startswith("open_transaction_without_open_final_action") for error in report.errors))
        self.assertTrue(any(error.startswith("open_transaction_without_open_authority") for error in report.errors))

    def test_system_invariant_audit_fails_watch_for_trigger_probe_opened(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            bad_payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_probe",
                    "authority_type": "exploration_probe",
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "open_action_evidence": True,
                    "strong_current_evidence": True,
                    "tradeable_state": True,
                    "watch_for_trigger_block": True,
                    "execution_requirement": "intraday_trigger_required",
                    "reason_codes": ["pm_watch_for_trigger_probe_cap", "real_probe_positive_or_strong_confirmation_release"],
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            conn.execute("UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'", (_dumps(bad_payload), _dumps(bad_payload)))
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(bad_payload),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(any(error.startswith("direction_or_watchlist_probe_opened") for error in report.errors))

    def test_system_invariant_audit_rejects_opportunity_score_as_contract_authority(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = json.loads(conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()[0])
            payload["final_action_contract"]["opportunity_score"] = 0.88
            payload["final_action_contract"]["opportunity_rank"] = 1
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("opportunity_ranking_field_top_level_trade_authority") for error in report.errors)
        )

    def test_system_invariant_audit_rejects_learning_component_as_trade_intent(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = json.loads(conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()[0])
            payload["execution_translation"] = {
                "intraday_execution": {
                    "action": "open_short",
                    "lots": 2,
                    "positive_learning": 0.12,
                }
            }
            payload["final_action_contract"]["evidence_used"] = {
                "opportunity_score_components": {
                    "positive_learning": 0.12,
                    "negative_learning": 0.0,
                    "execution_profile_learning": 0.03,
                    "recent_tail_loss_penalty": 0.0,
                }
            }
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("opportunity_learning_component_used_as_trade_intent") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_transaction_payload_pm_explanation_trade_intent(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_real",
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "position_sizing_result": {
                        "target_lots": -2,
                        "capital_allocation_reason": {"rank_is_not_trade_authority": True},
                    },
                    "learning_used": {"alpha_setup_action_values": [{"action_preference": "positive_candidate_open"}]},
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(payload),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("opportunity_ranking_field_used_in_execution_trade_intent") for error in report.errors),
            report.to_dict(),
        )
        self.assertTrue(
            any(error.startswith("transaction_audit_payload_forbidden_pm_contract_mirror") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_pm_artifact_downstream_fact(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)

        def mutate(payload):
            payload["final_action_contract"]["execution_result"] = {"status": "filled"}

        self._mutate_recommendation_payload(db_path, mutate)

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("pm_artifact_forbidden_downstream_field") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_auditor_artifact_contract_mutation(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)

        def mutate(payload):
            payload["audit_verdict"] = {
                "verdict": "pass",
                "new_final_action_contract": {"final_action": "open_real", "target_lots": -9},
            }

        self._mutate_recommendation_payload(db_path, mutate)

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("auditor_artifact_forbidden_contract_mutation") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_trader_artifact_pm_explanation_fields(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)

        def mutate(payload):
            payload["phase2_execution"] = {
                "pm_plan_validation": {
                    "target_lots": -2,
                    "position_sizing_result": {"capital_allocation_reason": "ranked first"},
                }
            }

        self._mutate_recommendation_payload(db_path, mutate)

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("trader_artifact_forbidden_pm_explanation") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_accountant_artifact_learning_and_trade_mutation(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO daily_settlement(
                    id, portfolio_id, trading_date, daily_pnl, commission, current_balance,
                    current_margin, margin_ratio, positions_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "set1",
                    "pf",
                    "2025-03-03",
                    100.0,
                    10.0,
                    1000100.0,
                    6600.0,
                    0.0066,
                    _dumps(
                        {
                            "positions": [],
                            "learning_used": {"alpha_setup_action_values": []},
                            "final_action_contract": {"target_lots": -2},
                        }
                    ),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("accountant_artifact_forbidden_learning_field") for error in report.errors),
            report.to_dict(),
        )
        self.assertTrue(
            any(error.startswith("accountant_artifact_forbidden_trade_action_mutation") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_reviewer_artifact_research_write(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)

        def mutate(payload):
            payload["phase4_review"] = {
                "validation_status": "completed",
                "alpha_setup_action_value": {"action_preference": "positive_candidate_open"},
                "adaptive_policy_state": {"policy_type": "open_amplification"},
            }

        self._mutate_recommendation_payload(db_path, mutate)

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("reviewer_artifact_forbidden_action_value_write") for error in report.errors),
            report.to_dict(),
        )
        self.assertTrue(
            any(error.startswith("reviewer_artifact_forbidden_research_state_write") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_rejects_researcher_artifact_trade_fact_mutation(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE alpha_setup_action_value SET payload_json=? WHERE id='av1'",
                (_dumps({"trade_fact_mutation": {"modified_final_action_contract": {"target_lots": -9}}}),),
            )
            conn.execute(
                """
                INSERT INTO adaptive_policy_state(
                    id, config_id, ticker, side, policy_type, source_trading_date, active, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "aps1",
                    "cfg",
                    "RB",
                    "short",
                    "analyst_calibration",
                    "2025-03-03",
                    1,
                    _dumps({"settlement_override": {"daily_pnl": 999.0}}),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO researcher_llm_notes(id, config_id, trading_date, evidence_pack_id, ticker, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "note1",
                    "cfg",
                    "2025-03-03",
                    "pack1",
                    "RB",
                    _dumps({"rewritten_final_action_contract": {"target_lots": -9}}),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertGreaterEqual(
            sum(error.startswith("researcher_artifact_forbidden_trade_fact_mutation") for error in report.errors),
            3,
            report.to_dict(),
        )

    def test_system_invariant_audit_allows_conditional_trigger_probe_after_intraday_confirmation(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = json.loads(conn.execute("SELECT signal_snapshot FROM futures_recommendation WHERE id='rec1'").fetchone()[0])
            contract = payload["final_action_contract"]
            contract.update(
                {
                    "final_action": "open_probe",
                    "authority_type": "exploration_probe",
                    "authority_decision": "allow_exploration_probe",
                    "open_action_evidence": False,
                    "strong_current_evidence": False,
                    "tradeable_state": True,
                    "conditional_trigger_authority": True,
                    "requires_intraday_confirmation": True,
                    "can_execute_without_intraday_trigger": False,
                    "watch_for_trigger_block": False,
                    "execution_requirement": "intraday_trigger_required",
                    "reason_codes": ["pm_watch_for_trigger_probe_cap", "conditional_trigger_authority"],
                }
            )
            payload["trade_contract_audit"]["execution_requirement"] = "intraday_trigger_required"
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                (_dumps(payload), _dumps(payload)),
            )
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(_transaction_audit_payload(payload)),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")

        self.assertTrue(report.ok, report.errors)

    def test_system_invariant_audit_fails_missing_intraday_record(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_real",
                    "authority_type": "real_budget_entry",
                    "open_action_evidence": True,
                    "strong_current_evidence": True,
                    "tradeable_state": True,
                    "watch_for_trigger_block": False,
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "execution_requirement": "intraday_trigger_required",
                    "reason_codes": ["positive_candidate_open"],
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": False}},
            }
            conn.execute("DELETE FROM futures_intraday_decision WHERE id='intra1'")
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(_transaction_audit_payload(payload)),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(any(error.startswith("open_transaction_without_intraday_trigger") for error in report.errors))

    def test_system_invariant_audit_fails_trade_contract_audit_source_of_truth(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_real",
                    "authority_type": "real_budget_entry",
                    "open_action_evidence": True,
                    "strong_current_evidence": True,
                    "tradeable_state": True,
                    "watch_for_trigger_block": False,
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "execution_requirement": "intraday_trigger_required",
                    "reason_codes": ["positive_candidate_open"],
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": False,
                    "candidate_sources_do_not_bypass_contract": False,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(_transaction_audit_payload(payload)),))
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(any(error.startswith("trade_contract_source_of_truth_failed") for error in report.errors))

    def test_system_invariant_audit_fails_hold_contract_with_close_transaction(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "BU",
            "contract_type": "strategy",
            "final_action": "hold",
            "current_lots": -10,
            "target_lots": -10,
            "lots_delta": 0,
            "authority_type": "not_applicable",
            "reason_codes": ["position_matched"],
            "execution_requirement": "position_management_or_wait",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        payload = {
            "final_action_contract": contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "final_action": "hold",
                "current_lots": -10,
                "target_lots": -10,
                "lots_delta": 0,
            },
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE futures_recommendation SET underlying_code=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("BU", "hold", 0, _dumps(payload), _dumps(payload)),
            )
            conn.execute(
                "UPDATE futures_transactions SET ticker=?, action=?, lots=?, audit_payload=? WHERE id='tx1'",
                ("BU", "close_short", 1, _dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("transaction_not_derived_from_final_action_contract") for error in report.errors),
            report.to_dict(),
        )

    def test_transaction_payload_final_contract_is_not_authoritative_source(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        recommendation_contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "BU",
            "contract_type": "strategy",
            "final_action": "hold",
            "current_lots": -10,
            "target_lots": -10,
            "lots_delta": 0,
            "authority_type": "not_applicable",
            "reason_codes": ["position_matched"],
            "execution_requirement": "position_management_or_wait",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        recommendation_payload = {
            "final_action_contract": recommendation_contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "final_action": "hold",
                "current_lots": -10,
                "target_lots": -10,
                "lots_delta": 0,
            },
        }
        rogue_transaction_payload = {
            "final_action_contract": {
                "contract_version": "agentquant.final_action.v1",
                "ticker": "BU",
                "contract_type": "strategy",
                "final_action": "exit",
                "current_lots": -10,
                "target_lots": 0,
                "lots_delta": 10,
                "authority_type": "not_applicable",
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "final_action": "exit",
                "current_lots": -10,
                "target_lots": 0,
                "lots_delta": 10,
            },
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE futures_recommendation SET underlying_code=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("BU", "hold", 0, _dumps(recommendation_payload), _dumps(recommendation_payload)),
            )
            conn.execute(
                "UPDATE futures_transactions SET ticker=?, action=?, lots=?, audit_payload=? WHERE id='tx1'",
                ("BU", "close_short", 10, _dumps(rogue_transaction_payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("transaction_not_derived_from_final_action_contract") for error in report.errors),
            report.to_dict(),
        )
        self.assertTrue(
            any(error.startswith("transaction_audit_payload_forbidden_pm_contract_mirror") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_accepts_reduce_transaction_from_contract_delta(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "BU",
            "contract_type": "strategy",
            "final_action": "reduce",
            "current_lots": -10,
            "target_lots": -7,
            "lots_delta": 3,
            "authority_type": "not_applicable",
            "reason_codes": ["protective_reduce"],
            "execution_requirement": "position_management_or_wait",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        payload = {
            "final_action_contract": contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "final_action": "reduce",
                "current_lots": -10,
                "target_lots": -7,
                "lots_delta": 3,
            },
        }
        payload = _with_auditor_approval(payload)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE futures_recommendation SET underlying_code=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("BU", "close_short", 3, _dumps(payload), _dumps(payload)),
            )
            conn.execute(
                "UPDATE futures_transactions SET ticker=?, action=?, lots=?, audit_payload=? WHERE id='tx1'",
                ("BU", "close_short", 3, _dumps(_transaction_audit_payload(payload))),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_accepts_exit_to_zero_target_lots(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "ZN",
            "contract_type": "strategy",
            "final_action": "exit",
            "current_lots": -4,
            "target_lots": 0,
            "lots_delta": 4,
            "authority_type": "not_applicable",
            "reason_codes": ["stop_loss_exit"],
            "execution_requirement": "position_management_or_wait",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        payload = {
            "final_action_contract": contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
                "final_action": "exit",
                "current_lots": -4,
                "target_lots": 0,
                "lots_delta": 4,
            },
        }
        payload = _with_auditor_approval(payload)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE futures_recommendation SET underlying_code=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("ZN", "close_short", 4, _dumps(payload), _dumps(payload)),
            )
            conn.execute(
                "UPDATE futures_transactions SET ticker=?, action=?, lots=?, audit_payload=? WHERE id='tx1'",
                ("ZN", "close_short", 4, _dumps(_transaction_audit_payload(payload))),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_fails_recommendation_contract_delta_mismatch_before_transaction(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "ZN",
            "contract_type": "strategy",
            "final_action": "hold",
            "current_lots": -4,
            "target_lots": -4,
            "lots_delta": 4,
            "authority_type": "not_applicable",
            "reason_codes": ["position_matched"],
            "execution_requirement": "position_management_or_wait",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        payload = {
            "final_action_contract": contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE futures_recommendation SET underlying_code=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("ZN", "hold", 0, _dumps(payload), _dumps(payload)),
            )
            conn.execute("DELETE FROM futures_transactions")
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("recommendation_final_action_contract_lots_delta_mismatch") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_recommendation_contract_action_mismatch_before_transaction(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "BU",
            "contract_type": "strategy",
            "final_action": "reduce",
            "current_lots": -10,
            "target_lots": -10,
            "lots_delta": 0,
            "authority_type": "not_applicable",
            "reason_codes": ["position_matched"],
            "execution_requirement": "position_management_or_wait",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        payload = {
            "final_action_contract": contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE futures_recommendation SET underlying_code=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("BU", "hold", 0, _dumps(payload), _dumps(payload)),
            )
            conn.execute("DELETE FROM futures_transactions")
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("recommendation_final_action_contract_action_mismatch") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_recommendation_top_level_not_synced_to_contract(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "ticker": "BU",
            "contract_type": "strategy",
            "final_action": "open_probe",
            "current_lots": 0,
            "target_lots": -2,
            "lots_delta": -2,
            "authority_type": "conditional_trigger_authority",
            "reason_codes": ["conditional_monitor_probe_seed"],
            "execution_requirement": "intraday_trigger_required",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        payload = {
            "final_action_contract": contract,
            "trade_contract_audit": {
                "single_source_of_trade_truth": True,
                "candidate_sources_do_not_bypass_contract": True,
            },
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE futures_recommendation SET underlying_code=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("BU", "hold", 0, _dumps(payload), _dumps(payload)),
            )
            conn.execute("DELETE FROM futures_transactions")
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(
                error.startswith("recommendation_top_level_action_lots_mismatch_final_action_contract")
                for error in report.errors
            ),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_strategy_recommendation_with_non_strategy_contract(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        contract = {
            "contract_version": "agentquant.final_action.v1",
            "contract_type": "operational_rollover",
            "ticker": "RB",
            "final_action": "open_real",
            "current_lots": 0,
            "target_lots": -2,
            "lots_delta": -2,
            "authority_type": "real_budget_entry",
            "reason_codes": ["positive_candidate_open"],
            "execution_requirement": "intraday_trigger_required",
            "single_source_of_trade_truth": True,
            "candidate_sources_do_not_bypass_contract": True,
        }
        payload = {
            "final_action_contract": contract,
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE futures_recommendation SET source_type=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("strategy", _dumps(payload), _dumps(payload)),
            )
            conn.execute("DELETE FROM futures_transactions")
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("strategy_recommendation_non_strategy_final_action_contract") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_accepts_forced_risk_close_without_strategy_contract(self):
        db_path = self._make_db()
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            audit_payload = {
                "operation_reason": "margin_liquidation",
                "forced_risk_execution": {
                    "source_type": "forced_risk",
                    "operation_reason": "margin_liquidation",
                    "strategy_learning_boundary": "excluded_from_strategy_action_value",
                },
            }
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "risk-rec",
                    "cfg",
                    "pf",
                    "2025-03-03",
                    "2025-03-03",
                    "forced_risk",
                    "RB",
                    "close_short",
                    1,
                    _dumps({}),
                    _dumps(audit_payload),
                    "executed",
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO futures_transactions(
                    id, portfolio_id, config_id, recommendation_id, trading_date, ticker, action, lots,
                    execution_price, contract_multiplier, margin_rate, margin_used, audit_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "risk-tx",
                    "pf",
                    "cfg",
                    "risk-rec",
                    "2025-03-03",
                    "RB",
                    "close_short",
                    1,
                    3300.0,
                    10.0,
                    0.1,
                    0.0,
                    _dumps(audit_payload),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_fails_same_day_rollover_effective_date(self):
        db_path = self._make_db()
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rollover-same-day",
                    "cfg",
                    "pf",
                    "2025-03-03",
                    "2025-03-03",
                    "rollover",
                    "RB",
                    "rollover",
                    2,
                    _dumps({}),
                    _dumps({}),
                    "pending",
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("rollover_effective_trade_date_not_after_detection") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_forced_risk_open(self):
        db_path = self._make_db()
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date, effective_trade_date, source_type,
                    underlying_code, action, lots, signal_snapshot, audit_payload, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("risk-open-rec", "cfg", "pf", "2025-03-03", "2025-03-03", "forced_risk", "RB", "open_short", 1, _dumps({}), _dumps({}), "executed", now),
            )
            conn.execute(
                """
                INSERT INTO futures_transactions(
                    id, portfolio_id, config_id, recommendation_id, trading_date, ticker, action, lots,
                    execution_price, contract_multiplier, margin_rate, margin_used, audit_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("risk-open-tx", "pf", "cfg", "risk-open-rec", "2025-03-03", "RB", "open_short", 1, 3300.0, 10.0, 0.1, 3300.0, _dumps({}), now),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("forced_risk_recommendation_cannot_open") for error in report.errors),
            report.to_dict(),
        )
        self.assertTrue(
            any(error.startswith("forced_risk_open_transaction_not_allowed") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_generic_positive_open_action_value(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE alpha_setup_action_value SET action_preference=?, payload_json=? WHERE id='av1'",
                ("observe_or_probe", _dumps({"amplification_scope_quality": "exact_real_state", "reward_source": "trade_episode"})),
            )
            conn.commit()
        finally:
            conn.close()
        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "action_value_missing_action_preference:RB:short:trend_breakout:open:2025-03-03:missing_action_preference",
            report.errors,
        )
        self.assertIn(
            "positive_open_action_value_not_open_preference:RB:short:trend_breakout:open:2025-03-03:missing_action_preference",
            report.errors,
        )

    def test_system_invariant_audit_fails_missing_payload_action_preference(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET action_preference=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "",
                    _dumps(
                        {
                            "amplification_scope_quality": "exact_real_state",
                            "reward_source": "trade_episode",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("action_value_missing_action_preference") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_action_preference_column_payload_drift(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET action_preference=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "negative_revalidate",
                    _dumps(
                        {
                            "action_preference": "positive_candidate_open",
                            "amplification_scope_quality": "exact_real_state",
                            "reward_source": "trade_episode",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("action_preference_column_payload_mismatch") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_warns_unlanded_preference_without_final_contract(self):
        db_path = self._make_db()
        conn = sqlite3.connect(db_path)
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO alpha_setup_action_value(
                    id, config_id, scope_key, ticker, side, setup_type, action_name, sample_count,
                    reward_sum, action_preference, last_sample_date, created_at, updated_at, active, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "av-unlanded",
                    "cfg",
                    "RB|short|trend_breakout",
                    "RB",
                    "short",
                    "trend_breakout",
                    "open",
                    1,
                    1200.0,
                    "positive_candidate_open",
                    "2025-03-03",
                    now,
                    now,
                    1,
                    _dumps(
                        {
                            "action_preference": "positive_candidate_open",
                            "amplification_scope_quality": "exact_real_state",
                            "reward_source": "trade_episode",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())
        self.assertTrue(
            any(warning.startswith("action_preferences_exist_but_no_downstream_final_action_contract_yet") for warning in report.warnings),
            report.to_dict(),
        )

    def test_system_invariant_audit_fails_unlanded_preference_with_downstream_final_contract(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "contract_type": "strategy",
                    "final_action": "open_real",
                    "authority_type": "real_budget_entry",
                    "authority_decision": "allow_real_new_entry",
                    "open_action_evidence": True,
                    "strong_current_evidence": True,
                    "tradeable_state": True,
                    "watch_for_trigger_block": False,
                    "negative_profile": False,
                    "current_lots": 0,
                    "target_lots": -2,
                    "lots_delta": -2,
                    "execution_requirement": "intraday_trigger_required",
                    "reason_codes": ["tradeable_candidate"],
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                    "execution_requirement": "intraday_trigger_required",
                },
                "execution_translation": {"intraday_execution": {"trigger_passed": True}},
            }
            conn.execute("UPDATE futures_recommendation SET trading_date=?, effective_trade_date=? WHERE id='rec1'", ("2025-03-04", "2025-03-04"))
            conn.execute("UPDATE futures_transactions SET trading_date=? WHERE id='tx1'", ("2025-03-04",))
            conn.execute("UPDATE futures_intraday_decision SET trading_date=? WHERE id='intra1'", ("2025-03-04",))
            conn.execute("UPDATE futures_recommendation SET signal_snapshot=?, audit_payload=? WHERE id='rec1'", (_dumps(payload), _dumps(payload)))
            conn.execute("UPDATE futures_transactions SET audit_payload=? WHERE id='tx1'", (_dumps(payload),))
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET action_name=?, reward_sum=?, action_preference=?, last_sample_date=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "exit",
                    -1200.0,
                    "tail_loss_protect",
                    "2025-03-03",
                    _dumps(
                        {
                            "action_preference": "tail_loss_protect",
                            "amplification_scope_quality": "partial_real_state",
                            "reward_source": "trade_episode",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("action_preferences_exist_but_no_final_action_contract_mentions_them") for error in report.errors),
            report.to_dict(),
        )

    def test_system_invariant_audit_does_not_require_weak_prior_to_land(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET reward_sum=?, action_preference=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    0.0,
                    "weak_prior",
                    _dumps(
                        {
                            "prior_role": "weak_prior_not_action_preference",
                            "amplification_scope_quality": "partial_real_state",
                            "reward_source": "similar_sql_prior",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_does_not_require_open_weak_prior_to_be_action_preference(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET ticker=?, side=?, scope_key=?, setup_type=?, action_name=?,
                    reward_sum=?, action_preference=?, last_sample_date=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "BU",
                    "short",
                    "BU|short|flat|choppy|fundamental_timing_setup",
                    "fundamental_timing_setup",
                    "open",
                    85.0,
                    "weak_prior",
                    "2025-03-17",
                    _dumps(
                        {
                            "prior_role": "weak_prior_not_action_preference",
                            "amplification_scope_quality": "similar_sql_prior",
                            "reward_source": "similar_sql_prior",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_accepts_protective_landing_terms(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            payload = {
                "final_action_contract": {
                    "final_action": "reduce",
                    "authority_type": "position_lifecycle",
                    "current_lots": -2,
                    "target_lots": -1,
                    "lots_delta": 1,
                    "reason_codes": ["protective_reduce_after_tail_loss"],
                    "evidence_used": {
                        "opportunity_score_components": {
                            "positive_learning": 0.0,
                            "negative_learning": -0.12,
                            "execution_profile_learning": 0.0,
                            "recent_tail_loss_penalty": -0.08,
                        }
                    },
                    "learning_used": {
                        "alpha_setup_action_values": [
                            {
                                "action_preference": "tail_loss_protect",
                                "reward_sum": -1200.0,
                                "reward_mean": -1200.0,
                                "win_rate": 0.0,
                                "reward_source": "trade_episode",
                                "evidence_scope": "partial_real_state",
                                "action_value_lane": "exit",
                            }
                        ]
                    },
                },
                "trade_contract_audit": {
                    "single_source_of_trade_truth": True,
                    "candidate_sources_do_not_bypass_contract": True,
                },
            }
            payload = _with_auditor_approval(payload)
            conn.execute(
                "UPDATE futures_recommendation SET trading_date=?, effective_trade_date=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec1'",
                ("2025-03-04", "2025-03-04", "close_short", 1, _dumps(payload), _dumps(payload)),
            )
            conn.execute("DELETE FROM futures_transactions")
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET action_name=?, reward_sum=?, action_preference=?, last_sample_date=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "exit",
                    -1200.0,
                    "tail_loss_protect",
                    "2025-03-03",
                    _dumps(
                        {
                            "action_preference": "tail_loss_protect",
                            "amplification_scope_quality": "partial_real_state",
                            "reward_source": "trade_episode",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())

    def test_system_invariant_audit_fails_positive_open_from_partial_scope(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE alpha_setup_action_value SET payload_json=? WHERE id='av1'",
                (
                    _dumps(
                        {
                            "action_preference": "positive_candidate_open",
                            "amplification_scope_quality": "partial_real_state",
                            "reward_source": "trade_episode",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "positive_open_from_non_exact_scope:RB:short:trend_breakout:open:2025-03-03:partial_real_state",
            report.errors,
        )

    def test_system_invariant_audit_fails_positive_open_without_real_reward_source(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE alpha_setup_action_value SET payload_json=? WHERE id='av1'",
                (
                    _dumps(
                        {
                            "action_preference": "positive_candidate_open",
                            "amplification_scope_quality": "exact_real_state",
                            "reward_source": "counterfactual_prior",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "positive_open_from_non_real_reward_source:RB:short:trend_breakout:open:2025-03-03:counterfactual_prior",
            report.errors,
        )

    def test_system_invariant_audit_accepts_legacy_real_reward_count_source(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE alpha_setup_action_value SET payload_json=? WHERE id='av1'",
                (
                    _dumps(
                        {
                            "action_preference": "positive_candidate_open",
                            "amplification_scope_quality": "exact_real_state",
                            "real_trade_reward_count": 1,
                            "exact_state_real_trade_sample_count": 1,
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.errors)

    def test_system_invariant_audit_fails_negative_exit_weak_prior(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET ticker=?, side=?, scope_key=?, setup_type=?, action_name=?,
                    reward_sum=?, action_preference=?, last_sample_date=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "BU",
                    "short",
                    "BU|short|flat|choppy|generic_trade_setup",
                    "generic_trade_setup",
                    "exit",
                    -3917.83,
                    "cap_reduce_or_revalidate",
                    "2025-03-10",
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

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "action_value_unknown_action_preference:BU:short:generic_trade_setup:exit:2025-03-10:weak_prior",
            report.errors,
        )
        self.assertIn(
            "negative_action_value_not_protective_preference:BU:short:generic_trade_setup:exit:2025-03-10:weak_prior",
            report.errors,
        )

    def test_system_invariant_audit_fails_positive_exit_weak_prior_from_real_reward(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET ticker=?, side=?, scope_key=?, setup_type=?, action_name=?,
                    reward_sum=?, reward_mean=?, win_rate=?, action_preference=?,
                    last_sample_date=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "SR",
                    "long",
                    "SR|long|flat|trend|fundamental_timing_setup",
                    "fundamental_timing_setup",
                    "exit",
                    235.0,
                    235.0,
                    1.0,
                    "observe_or_probe",
                    "2025-03-07",
                    _dumps(
                        {
                            "action_preference": "weak_prior",
                            "amplification_scope_quality": "partial_real_state",
                            "real_trade_reward_count": 1,
                            "reward_source": "real_trade",
                            "sample_source": "real_trade",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "action_value_unknown_action_preference:SR:long:fundamental_timing_setup:exit:2025-03-07:weak_prior",
            report.errors,
        )
        self.assertIn(
            "positive_exit_action_value_not_exit_preference:SR:long:fundamental_timing_setup:exit:2025-03-07:weak_prior",
            report.errors,
        )

    def test_system_invariant_audit_fails_positive_execution_weak_prior_from_real_reward(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET ticker=?, side=?, scope_key=?, setup_type=?, action_name=?,
                    reward_sum=?, reward_mean=?, win_rate=?, action_preference=?,
                    last_sample_date=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "SR",
                    "long",
                    "SR|long|flat|trend|execution_exit_immediate_setup",
                    "execution_exit_immediate_setup",
                    "execution",
                    235.0,
                    235.0,
                    1.0,
                    "observe_or_probe",
                    "2025-03-07",
                    _dumps(
                        {
                            "action_preference": "weak_prior",
                            "amplification_scope_quality": "partial_real_state",
                            "real_trade_reward_count": 1,
                            "reward_source": "real_trade",
                            "sample_source": "real_trade",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "action_value_unknown_action_preference:SR:long:execution_exit_immediate_setup:execution:2025-03-07:weak_prior",
            report.errors,
        )

    def test_system_invariant_audit_fails_exit_action_value_allowed_to_open_amplify(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET ticker=?, side=?, scope_key=?, setup_type=?, action_name=?,
                    reward_sum=?, reward_mean=?, win_rate=?, action_preference=?,
                    last_sample_date=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "SR",
                    "long",
                    "SR|long|flat|trend|fundamental_timing_setup",
                    "fundamental_timing_setup",
                    "exit",
                    235.0,
                    235.0,
                    1.0,
                    "positive_candidate_exit",
                    "2025-03-07",
                    _dumps(
                        {
                            "action_preference": "positive_candidate_exit",
                            "amplification_scope_quality": "partial_real_state",
                            "reward_source": "real_trade",
                            "usage_boundary": {
                                "usable_by": ["portfolio_manager"],
                                "allowed_effects": ["real_budget_entry"],
                                "forbidden_effects": ["direct_trade_authority"],
                            },
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "action_value_usage_boundary_forbids_exit_as_open_amplifier:SR:long:fundamental_timing_setup:exit:2025-03-07:real_budget_entry",
            report.errors,
        )

    def test_system_invariant_audit_fails_execution_action_value_allowed_to_change_lots(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE alpha_setup_action_value
                SET ticker=?, side=?, scope_key=?, setup_type=?, action_name=?,
                    reward_sum=?, reward_mean=?, win_rate=?, action_preference=?,
                    last_sample_date=?, payload_json=?
                WHERE id='av1'
                """,
                (
                    "SR",
                    "long",
                    "SR|long|flat|trend|execution_exit_immediate_setup",
                    "execution_exit_immediate_setup",
                    "execution",
                    235.0,
                    235.0,
                    1.0,
                    "positive_candidate_execution",
                    "2025-03-07",
                    _dumps(
                        {
                            "action_preference": "positive_candidate_execution",
                            "amplification_scope_quality": "partial_real_state",
                            "reward_source": "real_trade",
                            "usage_boundary": {
                                "usable_by": ["trader"],
                                "allowed_effects": ["change_lots"],
                                "forbidden_effects": ["direct_trade_authority"],
                            },
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertIn(
            "action_value_usage_boundary_forbids_execution_changing_trade_intent:SR:long:execution_exit_immediate_setup:execution:2025-03-07:change_lots",
            report.errors,
        )

    def test_system_invariant_audit_fails_candidate_adaptive_policy_release(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adaptive_policy_state (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    setup_type TEXT NOT NULL,
                    horizon_class TEXT NOT NULL,
                    market_regime TEXT NOT NULL,
                    policy_type TEXT NOT NULL,
                    policy_action TEXT NOT NULL,
                    multiplier REAL DEFAULT 1,
                    confidence_score REAL DEFAULT 0,
                    sample_count INTEGER DEFAULT 0,
                    reason TEXT,
                    source_event_id TEXT,
                    source_trading_date TEXT,
                    created_at TEXT NOT NULL,
                    valid_until TEXT,
                    payload_json TEXT,
                    active INTEGER DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                INSERT INTO adaptive_policy_state (
                    id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                    policy_type, policy_action, multiplier, confidence_score, sample_count,
                    reason, source_trading_date, created_at, payload_json, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "pol1",
                    "cfg",
                    "RB",
                    "long",
                    "trend_breakout_setup",
                    "short",
                    "trend",
                    "alpha_promotion",
                    "protect",
                    1.0,
                    0.9,
                    9,
                    "candidate alpha not validated",
                    "2025-03-03",
                    datetime.utcnow().isoformat(),
                    _dumps(
                        {
                            "status": "candidate",
                            "next_round_memory_contract": {
                                "status": "candidate",
                                "maturity_state": "candidate",
                                "position_authority": "analysis_or_watchlist_only",
                                "max_position_impact": "no_direct_position_impact",
                            },
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertFalse(report.ok)
        self.assertTrue(
            any(error.startswith("adaptive_policy_release_not_validated:") for error in report.errors),
            report.errors,
        )

    def test_system_invariant_audit_allows_validated_contextual_calibrate_policy(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adaptive_policy_state (
                    id TEXT PRIMARY KEY,
                    config_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    setup_type TEXT NOT NULL,
                    horizon_class TEXT NOT NULL,
                    market_regime TEXT NOT NULL,
                    policy_type TEXT NOT NULL,
                    policy_action TEXT NOT NULL,
                    multiplier REAL DEFAULT 1,
                    confidence_score REAL DEFAULT 0,
                    sample_count INTEGER DEFAULT 0,
                    reason TEXT,
                    source_event_id TEXT,
                    source_trading_date TEXT,
                    created_at TEXT NOT NULL,
                    valid_until TEXT,
                    payload_json TEXT,
                    active INTEGER DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                INSERT INTO adaptive_policy_state (
                    id, config_id, ticker, side, setup_type, horizon_class, market_regime,
                    policy_type, policy_action, multiplier, confidence_score, sample_count,
                    reason, source_trading_date, created_at, payload_json, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "pol-calibrate",
                    "cfg",
                    "M",
                    "long",
                    "long_trend_breakout_setup_short",
                    "short",
                    "choppy",
                    "contextual_rule_calibration:portfolio_manager",
                    "calibrate",
                    1.0,
                    0.8,
                    5,
                    "validated contextual PM calibration",
                    "2025-03-05",
                    datetime.utcnow().isoformat(),
                    _dumps(
                        {
                            "rule_validation_status": "validated_rule_applied",
                            "next_round_memory_contract": {
                                "status": "validated",
                                "maturity_state": "validated_policy",
                                "position_authority": "bounded_contextual_calibration",
                                "max_position_impact": "no_direct_trade_authority",
                            },
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_system_invariants(db_path=db_path, exp_name="agentquant-test")
        self.assertTrue(report.ok, report.to_dict())
        self.assertFalse(
            any(error.startswith("adaptive_policy_unknown_action:") for error in report.errors),
            report.errors,
        )

    def test_system_invariant_audit_cli_returns_nonzero_on_violation(self):
        db_path = self._make_db()
        self._insert_good_open(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE futures_intraday_decision SET decision=?, trigger_reason=?, features_json=? WHERE id='intra1'", ("wait", "intraday_waiting_for_trigger", _dumps({"trigger_passed": False})))
            conn.commit()
        finally:
            conn.close()
        report = audit_system_invariants(db_path=db_path, config_id="cfg")
        self.assertFalse(report.ok, report.to_dict())
        self.assertIn("intraday_trigger_audit_mirror_mismatch", "\n".join(report.errors))


if __name__ == "__main__":
    unittest.main()

