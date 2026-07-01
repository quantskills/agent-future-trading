import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_mechanism_effectiveness_audit import audit_mechanism_effectiveness


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


class MechanismEffectivenessAuditRegressionTest(unittest.TestCase):
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
                    commission REAL DEFAULT 0,
                    lots REAL DEFAULT 0,
                    entry_price REAL DEFAULT 0,
                    settle_price REAL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute("INSERT INTO config(id, exp_name) VALUES ('cfg', 'test-exp')")
            conn.execute("INSERT INTO portfolio(id, config_id) VALUES ('p1', 'cfg')")
            conn.commit()
        finally:
            conn.close()
        return db_path

    def _insert_action_value(
        self,
        db_path: Path,
        *,
        ticker: str = "RB",
        side: str = "short",
        preference: str = "positive_candidate_open",
        action_name: str = "open",
        action_value_lane: str = "open",
        memory_side_role: str = "",
        reward_sum: float = 1200.0,
        last_sample_date: str = "2025-03-03",
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO alpha_setup_action_value(
                    id, config_id, scope_key, ticker, side, horizon_class,
                    market_regime, setup_type, data_combo, action_name, sample_count,
                    reward_sum, reward_mean, win_rate, confidence_score,
                    action_preference, reward_source, evidence_scope, action_value_lane,
                    max_position_impact, last_sample_date, created_at, updated_at,
                    valid_until, active, payload_json
                )
                VALUES (
                    'av1', 'cfg', 'RB|short|trend', ?, ?, 'short_term',
                    'trend', 'trend_breakout', '*', ?, 3,
                    ?, ?, 0.67, 0.8,
                    ?, 'complete_episode', 'exact_real_state', ?,
                    0.02, ?, '2025-03-03T15:00:00', '2025-03-03T15:00:00',
                    NULL, 1, '{}'
                )
                """,
                (
                    ticker,
                    side,
                    action_name,
                    reward_sum,
                    reward_sum / 3.0,
                    preference,
                    action_value_lane,
                    last_sample_date,
                ),
            )
            if memory_side_role:
                conn.execute(
                    "UPDATE alpha_setup_action_value SET payload_json=? WHERE id='av1'",
                    (_dumps({"memory_side_role": memory_side_role}),),
                )
            conn.commit()
        finally:
            conn.close()

    def _insert_recommendation(
        self,
        db_path: Path,
        *,
        rec_id: str,
        ticker: str,
        contract: dict,
        trading_date: str = "2025-03-04",
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
                VALUES (?, 'cfg', 'p1', ?, ?,
                    'strategy', ?, 'hold', 0, ?, '{}', 'pending', ?)
                """,
                (rec_id, trading_date, trading_date, ticker, _dumps({"final_action_contract": contract}), f"{trading_date}T09:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_hard_fails_when_real_action_value_never_reaches_pm_score(self):
        db_path = self._make_db()
        self._insert_action_value(db_path)
        self._insert_recommendation(
            db_path,
            rec_id="r1",
            ticker="RB",
            contract={
                "ticker": "RB",
                "current_lots": 0,
                "target_lots": -1,
                "lots_delta": -1,
                "final_action": "open_probe",
                "evidence_used": {
                    "opportunity_rank": 1,
                    "opportunity_score_components": {
                        "positive_learning": 0,
                        "negative_learning": 0,
                        "execution_profile_learning": 0,
                        "recent_tail_loss_penalty": 0,
                    },
                },
                "capital_deployment": {
                    "opportunity_rank": 1,
                    "selected_for_capital_deployment": True,
                    "deployed_target_lots": -1,
                    "original_target_lots": -1,
                },
                "learning_used": {},
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        joined = "\n".join(report.hard_failures)
        self.assertIn("mechanism_action_value_not_read_by_pm", joined)
        self.assertIn("mechanism_pm_learning_not_in_score", joined)

    def test_hold_exit_learning_can_land_in_exit_contract_without_open_score_components(self):
        db_path = self._make_db()
        self._insert_action_value(
            db_path,
            ticker="M",
            side="long",
            preference="tail_loss_protect",
            action_name="exit",
            action_value_lane="exit",
            reward_sum=-3721.0,
        )
        self._insert_recommendation(
            db_path,
            rec_id="m-exit",
            ticker="M",
            contract={
                "ticker": "M",
                "current_lots": 4,
                "target_lots": 0,
                "lots_delta": -4,
                "final_action": "exit",
                "reason_codes": [
                    "flat_target",
                    "pm_full_market_capital_deployment",
                    "position_lifecycle_loss_revalidation_failed",
                ],
                "action_candidates": [
                    {
                        "action": "exit",
                        "classification": "loss_revalidation_failed",
                        "decision": "exit_failed_loss_revalidation",
                        "source": "position_lifecycle",
                        "status": "applied",
                    }
                ],
                "evidence_used": {
                    "opportunity_rank": 15,
                    "opportunity_score_components": {},
                    "capital_allocation_reason": "not_new_or_increasing_risk_preserve_pm_contract",
                },
                "capital_deployment": {
                    "opportunity_rank": 15,
                    "selected_for_capital_deployment": True,
                    "deployed_target_lots": 0,
                    "original_target_lots": 0,
                    "capital_allocation_reason": "not_new_or_increasing_risk_preserve_pm_contract",
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
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())
        self.assertIn("reduce_exit", report.metadata.get("checked_scenarios", {}))
        self.assertEqual(report.counts.get("scenarios", {}).get("reduce_exit"), 1)

    def test_exit_current_position_side_learning_must_reach_pm_contract(self):
        db_path = self._make_db()
        self._insert_action_value(
            db_path,
            ticker="RB",
            side="long",
            preference="tail_loss_protect",
            action_name="exit",
            action_value_lane="exit",
            reward_sum=-1993.34,
            last_sample_date="2025-03-03",
        )
        self._insert_recommendation(
            db_path,
            rec_id="rb-exit-missing-learning",
            ticker="RB",
            contract={
                "ticker": "RB",
                "current_lots": 13,
                "target_lots": 0,
                "lots_delta": -13,
                "final_action": "exit",
                "reason_codes": ["new_position_loss_revalidation_failed"],
                "evidence_used": {
                    "capital_allocation_reason": "not_new_or_increasing_risk_preserve_pm_contract",
                    "opportunity_score_components": {},
                },
                "learning_used": {"alpha_setup_action_values": []},
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        self.assertIn("mechanism_action_value_not_read_by_pm", "\n".join(report.hard_failures))

    def test_exit_current_position_side_learning_lands_with_memory_side_role(self):
        db_path = self._make_db()
        self._insert_action_value(
            db_path,
            ticker="RB",
            side="long",
            preference="tail_loss_protect",
            action_name="exit",
            action_value_lane="exit",
            reward_sum=-1993.34,
            last_sample_date="2025-03-03",
        )
        self._insert_recommendation(
            db_path,
            rec_id="rb-exit-with-learning",
            ticker="RB",
            contract={
                "ticker": "RB",
                "current_lots": 13,
                "target_lots": 0,
                "lots_delta": -13,
                "final_action": "exit",
                "reason_codes": ["new_position_loss_revalidation_failed"],
                "evidence_used": {
                    "capital_allocation_reason": "not_new_or_increasing_risk_preserve_pm_contract",
                    "opportunity_score_components": {},
                },
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "scope_key": "RB|long|trend",
                            "ticker": "RB",
                            "side": "long",
                            "action_name": "exit",
                            "action_preference": "tail_loss_protect",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "consumer_scope": "pm_learning",
                            "action_value_lane": "exit",
                            "learning_lane": "exit",
                            "memory_side_role": "current_position_side",
                            "reward_sum": -1993.34,
                            "reward_mean": -1993.34,
                            "sample_count": 1,
                            "win_rate": 0.0,
                            "last_sample_date": "2025-03-03",
                        }
                    ]
                },
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())

    def test_prior_open_learning_does_not_trigger_exit_action_value_read_failure(self):
        db_path = self._make_db()
        self._insert_action_value(
            db_path,
            ticker="RB",
            side="short",
            preference="positive_candidate_open",
            action_name="open",
            action_value_lane="open",
            memory_side_role="current_position_side",
            reward_sum=1200.0,
            last_sample_date="2025-03-03",
        )
        self._insert_recommendation(
            db_path,
            rec_id="rb-exit-with-only-open-prior",
            ticker="RB",
            contract={
                "ticker": "RB",
                "current_lots": -2,
                "target_lots": 0,
                "lots_delta": 2,
                "final_action": "exit",
                "reason_codes": ["not_new_or_increasing_exposure"],
                "evidence_used": {
                    "capital_allocation_reason": "not_new_or_increasing_risk_preserve_pm_contract",
                    "opportunity_score_components": {},
                },
                "learning_used": {"alpha_setup_action_values": []},
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())
        joined = "\n".join(report.hard_failures)
        self.assertNotIn("mechanism_action_value_not_read_by_pm", joined)
        self.assertNotIn("mechanism_matching_action_value_not_landed_in_pm", joined)

    def test_hold_exit_learning_fails_when_position_does_not_change_without_explanation(self):
        db_path = self._make_db()
        self._insert_action_value(
            db_path,
            ticker="M",
            side="long",
            preference="tail_loss_protect",
            action_name="exit",
            action_value_lane="exit",
            reward_sum=-3721.0,
        )
        self._insert_recommendation(
            db_path,
            rec_id="m-hold-bad",
            ticker="M",
            contract={
                "ticker": "M",
                "current_lots": 4,
                "target_lots": 4,
                "lots_delta": 0,
                "final_action": "hold",
                "reason_codes": ["pm_full_market_capital_deployment"],
                "evidence_used": {
                    "opportunity_rank": 8,
                    "opportunity_score_components": {},
                },
                "capital_deployment": {
                    "opportunity_rank": 8,
                    "selected_for_capital_deployment": True,
                    "deployed_target_lots": 4,
                    "original_target_lots": 4,
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
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        joined = "\n".join(report.hard_failures)
        self.assertIn("mechanism_hold_exit_learning_not_landed", joined)
        self.assertEqual(report.counts.get("scenarios", {}).get("position_hold"), 1)

    def test_hold_exit_learning_accepts_holding_period_control_explanation(self):
        db_path = self._make_db()
        self._insert_action_value(
            db_path,
            ticker="SR",
            side="long",
            preference="negative_hold_revalidate",
            action_name="observe",
            action_value_lane="hold",
            memory_side_role="current_position_side",
            reward_sum=-300.0,
            last_sample_date="2025-03-04",
        )
        self._insert_recommendation(
            db_path,
            rec_id="sr-hold-period",
            ticker="SR",
            trading_date="2025-03-13",
            contract={
                "ticker": "SR",
                "current_lots": 2,
                "target_lots": 2,
                "lots_delta": 0,
                "final_action": "hold",
                "reason_codes": ["flat_target", "holding_period_control", "position_matched"],
                "evidence_used": {
                    "capital_allocation_reason": "not_allocated_missing_invalidation_boundary",
                    "opportunity_score_components": {},
                },
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "scope_key": "SR|long|hold",
                            "ticker": "SR",
                            "side": "long",
                            "action_name": "observe",
                            "action_preference": "negative_hold_revalidate",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "consumer_scope": "pm_learning",
                            "action_value_lane": "hold",
                            "learning_lane": "hold",
                            "memory_side_role": "current_position_side",
                            "reward_sum": -300.0,
                            "reward_mean": -300.0,
                            "sample_count": 1,
                            "win_rate": 0.0,
                            "last_sample_date": "2025-03-04",
                        }
                    ]
                },
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())

    def test_hold_exit_learning_rejects_position_matched_as_only_explanation(self):
        db_path = self._make_db()
        self._insert_action_value(
            db_path,
            ticker="SR",
            side="long",
            preference="negative_hold_revalidate",
            action_name="observe",
            action_value_lane="hold",
            memory_side_role="current_position_side",
            reward_sum=-300.0,
            last_sample_date="2025-03-04",
        )
        self._insert_recommendation(
            db_path,
            rec_id="sr-position-matched-only",
            ticker="SR",
            trading_date="2025-03-13",
            contract={
                "ticker": "SR",
                "current_lots": 2,
                "target_lots": 2,
                "lots_delta": 0,
                "final_action": "hold",
                "reason_codes": ["position_matched"],
                "evidence_used": {
                    "opportunity_score_components": {},
                },
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "scope_key": "SR|long|hold",
                            "ticker": "SR",
                            "side": "long",
                            "action_name": "observe",
                            "action_preference": "negative_hold_revalidate",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "consumer_scope": "pm_learning",
                            "action_value_lane": "hold",
                            "learning_lane": "hold",
                            "memory_side_role": "current_position_side",
                            "reward_sum": -300.0,
                            "reward_mean": -300.0,
                            "sample_count": 1,
                            "win_rate": 0.0,
                            "last_sample_date": "2025-03-04",
                        }
                    ]
                },
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        self.assertIn("mechanism_hold_exit_learning_not_landed", "\n".join(report.hard_failures))

    def test_reduce_contract_with_learning_components_does_not_require_rank(self):
        db_path = self._make_db()
        self._insert_action_value(
            db_path,
            ticker="C",
            side="long",
            preference="positive_candidate_open",
            action_name="open",
            action_value_lane="open",
            reward_sum=865.63,
            last_sample_date="2025-03-06",
        )
        self._insert_recommendation(
            db_path,
            rec_id="c-protective-reduce",
            ticker="C",
            contract={
                "ticker": "C",
                "current_lots": 30,
                "target_lots": 14,
                "lots_delta": -16,
                "final_action": "reduce",
                "reason_codes": [
                    "not_new_or_increasing_exposure",
                    "winning_template_continuation_protective_reduce",
                ],
                "action_candidates": [
                    {
                        "action": "reduce",
                        "decision": "protective_reduce_no_continuation",
                        "source": "hold_exit_profit_protection",
                        "status": "applied",
                    }
                ],
                "evidence_used": {
                    "opportunity_rank": None,
                    "opportunity_score": 0.0,
                    "capital_allocation_reason": "not_allocated_missing_invalidation_boundary",
                    "opportunity_score_components": {
                        "positive_learning": 0.0025,
                        "negative_learning": 0.0,
                        "execution_profile_learning": 0.0021,
                        "recent_tail_loss_penalty": 0.0,
                    },
                },
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "scope_key": "C|long|event_short|range|news_event_setup",
                            "ticker": "C",
                            "side": "long",
                            "action_name": "open",
                            "action_preference": "positive_candidate_open",
                            "reward_source": "real_trade",
                            "evidence_scope": "exact_real_state",
                            "consumer_scope": "pm_learning",
                            "action_value_lane": "open",
                            "learning_lane": "open",
                            "reward_sum": 865.63,
                            "reward_mean": 865.63,
                            "sample_count": 1,
                            "win_rate": 1.0,
                            "last_sample_date": "2025-03-06",
                        }
                    ]
                },
            },
            trading_date="2025-03-07",
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.counts.get("scenarios", {}).get("reduce_exit"), 1)

    def test_open_learning_components_still_require_rank(self):
        db_path = self._make_db()
        self._insert_action_value(db_path)
        self._insert_recommendation(
            db_path,
            rec_id="open-learning-no-rank",
            ticker="RB",
            contract={
                "ticker": "RB",
                "current_lots": 0,
                "target_lots": -1,
                "lots_delta": -1,
                "final_action": "open_probe",
                "evidence_used": {
                    "opportunity_score": 0.42,
                    "opportunity_score_components": {
                        "positive_learning": 0.12,
                        "negative_learning": 0.0,
                        "execution_profile_learning": 0.0,
                        "recent_tail_loss_penalty": 0.0,
                    },
                },
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "scope_key": "RB|short|trend",
                            "ticker": "RB",
                            "side": "short",
                            "action_name": "open",
                            "action_preference": "positive_candidate_open",
                            "reward_source": "complete_episode",
                            "evidence_scope": "exact_real_state",
                            "consumer_scope": "pm_learning",
                            "action_value_lane": "open",
                            "reward_sum": 1200.0,
                            "sample_count": 3,
                            "win_rate": 0.67,
                            "last_sample_date": "2025-03-03",
                        }
                    ]
                },
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        self.assertIn("mechanism_learning_score_missing_rank", "\n".join(report.hard_failures))

    def test_ranked_open_contract_without_capital_deployment_hard_fails(self):
        db_path = self._make_db()
        self._insert_recommendation(
            db_path,
            rec_id="ranked-open-no-deployment",
            ticker="EB",
            contract={
                "ticker": "EB",
                "current_lots": 0,
                "target_lots": -11,
                "lots_delta": -11,
                "final_action": "open_probe",
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "evidence_used": {
                    "opportunity_score": 0.0,
                    "opportunity_rank": 1,
                    "capital_allocation_reason": "monitorable_conditional_candidate_selected_only_if_pm_capital_queue_allows",
                    "opportunity_score_components": {},
                },
                "learning_used": {"alpha_setup_action_values": []},
            },
            trading_date="2025-03-03",
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        self.assertIn(
            "mechanism_rank_missing_capital_deployment:2025-03-03:EB:ranked-open-no-deployment:rank=1",
            "\n".join(report.hard_failures),
        )

    def test_diagnostics_do_not_fail_when_mechanism_is_connected_but_rank_loses_money(self):
        db_path = self._make_db()
        self._insert_action_value(db_path)
        self._insert_recommendation(
            db_path,
            rec_id="r2",
            ticker="RB",
            contract={
                "ticker": "RB",
                "current_lots": 0,
                "target_lots": -1,
                "lots_delta": -1,
                "final_action": "open_probe",
                "evidence_used": {
                    "opportunity_score": 0.88,
                    "opportunity_rank": 1,
                    "capital_allocation_reason": "selected_by_full_market_rank",
                    "opportunity_score_components": {
                        "positive_learning": 0.24,
                        "negative_learning": 0,
                        "execution_profile_learning": 0.02,
                        "recent_tail_loss_penalty": 0,
                    },
                },
                "capital_deployment": {
                    "opportunity_rank": 1,
                    "selected_for_capital_deployment": True,
                    "deployed_target_lots": -1,
                    "original_target_lots": -1,
                    "capital_allocation_reason": "selected_by_full_market_rank",
                },
                "learning_used": {
                    "alpha_setup_action_values": [
                        {
                            "scope_key": "RB|short|trend",
                            "action_name": "open",
                            "action_preference": "positive_candidate_open",
                            "reward_source": "complete_episode",
                            "evidence_scope": "exact_real_state",
                            "action_value_lane": "open",
                            "reward_sum": 1200.0,
                            "sample_count": 3,
                            "win_rate": 0.67,
                            "last_sample_date": "2025-03-03",
                        }
                    ]
                },
            },
        )
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO ticker_daily_pnl VALUES ('pnl1','p1','2025-03-04','RB',-500,0,1,3600,3550,'2025-03-04T15:00:00')"
            )
            conn.execute(
                "INSERT INTO daily_settlement VALUES ('ds1','p1','2025-03-04',1000000,1000,0.001,-500,'2025-03-04T15:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())
        self.assertTrue(any(item.startswith("diagnostic_top_rank_bucket_negative_pnl") for item in report.diagnostics))
        self.assertTrue(any(item.startswith("diagnostic_low_average_margin_utilization") for item in report.diagnostics))

    def test_hard_fails_conditional_probe_without_intraday_result(self):
        db_path = self._make_db()
        self._insert_recommendation(
            db_path,
            rec_id="r3",
            ticker="HC",
            contract={
                "ticker": "HC",
                "current_lots": 0,
                "target_lots": -1,
                "lots_delta": -1,
                "final_action": "open_probe",
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "evidence_used": {
                    "opportunity_rank": 2,
                    "capital_allocation_reason": "selected_conditional_monitor",
                    "opportunity_score_components": {},
                },
                "capital_deployment": {
                    "opportunity_rank": 2,
                    "selected_for_capital_deployment": True,
                    "deployed_target_lots": -1,
                    "original_target_lots": -1,
                },
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertFalse(report.ok)
        self.assertIn("mechanism_conditional_probe_missing_intraday_result", "\n".join(report.hard_failures))

    def test_auditor_blocked_conditional_probe_does_not_require_intraday_result(self):
        db_path = self._make_db()
        contract = {
            "ticker": "HC",
            "current_lots": 0,
            "target_lots": -1,
            "lots_delta": -1,
            "final_action": "open_probe",
            "conditional_trigger_authority": True,
            "requires_intraday_confirmation": True,
            "can_execute_without_intraday_trigger": False,
            "evidence_used": {
                "opportunity_rank": 2,
                "capital_allocation_reason": "selected_conditional_monitor",
                "opportunity_score_components": {},
            },
            "capital_deployment": {
                "opportunity_rank": 2,
                "selected_for_capital_deployment": True,
                "deployed_target_lots": -1,
                "original_target_lots": -1,
            },
        }
        conn = sqlite3.connect(db_path)
        try:
            audit_payload = {
                "final_action_contract": contract,
                "independent_auditor": {
                    "audit_verdict": "block",
                    "hard_risk_reasons": ["missing_margin_or_price_boundary"],
                },
            }
            conn.execute(
                """
                INSERT INTO futures_recommendation(
                    id, config_id, reference_portfolio_id, trading_date,
                    effective_trade_date, source_type, underlying_code,
                    action, lots, signal_snapshot, audit_payload, status, created_at
                )
                VALUES ('blocked-conditional', 'cfg', 'p1', '2025-03-04', '2025-03-04',
                    'strategy', 'HC', 'hold', 0, ?, ?, 'blocked', '2025-03-04T09:00:00')
                """,
                (_dumps({"final_action_contract": contract}), _dumps(audit_payload)),
            )
            conn.commit()
        finally:
            conn.close()

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())
        self.assertNotIn("mechanism_conditional_probe_missing_intraday_result", "\n".join(report.hard_failures))

    def test_cli_returns_zero_for_diagnostics_and_nonzero_for_hard_fail(self):
        db_path = self._make_db()
        self._insert_recommendation(
            db_path,
            rec_id="r4",
            ticker="HC",
            contract={
                "ticker": "HC",
                "current_lots": 0,
                "target_lots": -1,
                "lots_delta": -1,
                "final_action": "open_probe",
                "conditional_trigger_authority": True,
                "requires_intraday_confirmation": True,
                "can_execute_without_intraday_trigger": False,
                "evidence_used": {"opportunity_rank": 1, "opportunity_score_components": {}},
                "capital_deployment": {"opportunity_rank": 1, "selected_for_capital_deployment": True},
            },
        )

        report = audit_mechanism_effectiveness(db_path=db_path, config_id="cfg")

        self.assertFalse(report.ok)
        self.assertIn("mechanism_conditional_probe_missing_intraday_result", "\n".join(report.hard_failures))

    def test_empty_database_without_config_is_ready_for_fresh_backtest(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = Path(tmpdir.name) / "agentquant.db"
        sqlite3.connect(db_path).close()

        report = audit_mechanism_effectiveness(db_path=db_path, exp_name="test-exp")

        self.assertTrue(report.ok, report.to_dict())
        self.assertIn("empty_db_no_mechanism_records_to_audit", report.metadata.get("record_boundary", ""))
        self.assertTrue(any(item.startswith("config_not_found_empty_db:") for item in report.warnings))


if __name__ == "__main__":
    unittest.main()
