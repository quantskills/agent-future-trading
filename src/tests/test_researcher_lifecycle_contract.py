from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from database.artifact_store import externalize_json_for_db, write_artifact_text
from database.sqlite_setup import _ensure_reviewer_learning_schema
from tools.agent_tools.decision.pm_contract_builder import build_final_action_contract
from tools.agent_tools.research.research_memory_writers import (
    _export_template_prior,
    _write_contextual_rule_calibration_state,
    insert_researcher_learning_completion_event,
)


LIFECYCLE_CALIBRATION_DECISIONS = (
    "reduce_failed_new_loss_revalidation",
    "exit_failed_new_loss_revalidation",
    "reduce_horizon_mismatch_losing_hold",
    "exit_horizon_mismatch_losing_hold",
)

FORMAL_LIFECYCLE_FIELDS = {
    "trace_version",
    "current_lots",
    "target_lots",
    "lots_delta",
    "pre_learning_position_ratio",
    "final_target_position_ratio",
    "position_ratio_delta",
    "open_add_rank_score_delta",
    "alpha_setup_multiplier",
    "alpha_setup_expectancy_lane",
    "hold_decision",
    "hold_changes_position",
    "reduce_exit_decision",
    "reduce_exit_changes_position",
    "conditional_monitor_decision",
    "execution_profile_changed",
    "execution_profile_learning_direct_to_rank",
}


def _connection(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _ensure_reviewer_learning_schema(conn.cursor())
    conn.commit()
    return conn


def _lots_for_decision(decision: str) -> tuple[int, int]:
    if decision == "skip_horizon_mismatch_new_entry":
        return 0, 0
    if decision.startswith("exit_"):
        return 1, 0
    return 2, 1


def _canonical_recommendation(
    decision: str,
    *,
    current_lots: int | None = None,
    target_lots: int | None = None,
    control_diagnostics: dict | None = None,
) -> dict:
    default_current, default_target = _lots_for_decision(decision)
    current_lots = default_current if current_lots is None else current_lots
    target_lots = default_target if target_lots is None else target_lots
    if control_diagnostics is None:
        control_diagnostics = {
            "holding_rebalance_control": {
                "decision": decision,
                "raw_target_ratio": 0.04 if current_lots else 0.0,
                "final_target_ratio": 0.02 if target_lots else 0.0,
            }
        }
    final_contract = build_final_action_contract(
        ticker="BU",
        current_lots=current_lots,
        target_lots=target_lots,
        position_ratio=0.02 if target_lots else 0.0,
        margin_required=2000.0 if target_lots else 0.0,
        account_equity=100000.0,
        lots_to_trade=abs(target_lots - current_lots),
        lots_to_trade_reason=decision,
        recommendation_intent={"action": "hold", "lots": abs(target_lots - current_lots)},
        final_entry_authority={},
        control_reasons=[decision],
        control_diagnostics=control_diagnostics,
        opportunity_scorecard={},
        market_confirmation={},
        alpha_setup_action_values=[],
        contract_code="BU2506",
        final_contract_scope={
            "setup_type": "trend_following",
            "horizon_class": "short",
            "market_regime": "trend",
        },
    )
    return {
        "id": f"rec-{decision}",
        "underlying_code": "BU",
        "action": final_contract["final_action"],
        "signal_snapshot": {
            "final_action_contract": final_contract,
            "signal_collection_contract": {"source_contracts": []},
        },
    }


class ResearcherLifecycleContractTest(unittest.TestCase):
    def _write(self, recommendation: dict) -> tuple[sqlite3.Connection, int]:
        conn = _connection()
        rows = _write_contextual_rule_calibration_state(
            conn.cursor(),
            config_id="cfg",
            trading_date="2025-03-10",
            cfg={
                "learning": {
                    "contextual_rule_calibration": {
                        "enabled": True,
                        "max_rows_per_day": 10,
                    }
                }
            },
            strategy_recommendations=[recommendation],
            no_trade_reason_counter=Counter(),
        )
        return conn, rows

    def test_all_formal_pm_lifecycle_results_reach_researcher_calibration(self):
        for decision in LIFECYCLE_CALIBRATION_DECISIONS:
            with self.subTest(decision=decision):
                conn = None
                try:
                    recommendation = _canonical_recommendation(decision)
                    impact = recommendation["signal_snapshot"]["final_action_contract"]["learning_used"][
                        "pm_lifecycle_learning_impact_delta"
                    ]
                    self.assertIsNone(impact["hold_decision"])
                    self.assertEqual(impact["reduce_exit_decision"], decision)
                    self.assertIsNone(impact["conditional_monitor_decision"])

                    conn, rows = self._write(recommendation)

                    self.assertEqual(rows, 1)
                    row = conn.execute(
                        "SELECT evidence_json FROM learning_event_log "
                        "WHERE event_type='contextual_rule_calibration'"
                    ).fetchone()
                    self.assertIsNotNone(row)
                    evidence = json.loads(row["evidence_json"])
                    self.assertEqual(
                        evidence["source"],
                        "final_action_contract.learning_used.pm_lifecycle_learning_impact_delta",
                    )
                    persisted = evidence["pm_lifecycle_learning_impact_delta"]
                    self.assertIsNone(persisted["hold_decision"])
                    self.assertEqual(persisted["reduce_exit_decision"], decision)
                    self.assertTrue(set(persisted).issubset(FORMAL_LIFECYCLE_FIELDS))
                    self.assertNotIn("holding_rebalance_control", evidence)
                    self.assertNotIn("action_candidates", evidence)
                finally:
                    if conn is not None:
                        conn.close()

    def test_wait_and_unrelated_lifecycle_decisions_do_not_calibrate(self):
        cases = (
            ("skip_horizon_mismatch_new_entry", 0, 0),
            ("continue_hold", 2, 2),
            ("reduce_exposure", 2, 1),
            ("exit_position", 1, 0),
            ("not_applicable", 0, 0),
        )
        for decision, current_lots, target_lots in cases:
            with self.subTest(decision=decision):
                recommendation = _canonical_recommendation(
                    decision,
                    current_lots=current_lots,
                    target_lots=target_lots,
                )
                conn = None
                try:
                    conn, rows = self._write(recommendation)
                    self.assertEqual(rows, 0)
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM learning_event_log "
                            "WHERE event_type='contextual_rule_calibration'"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    if conn is not None:
                        conn.close()

    def test_reduce_exit_uses_the_control_that_actually_changed_final_ratio(self):
        cases = (
            (
                {
                    "winning_template_continuation": {
                        "decision": "protective_reduce_no_continuation",
                        "pre_control_ratio": 0.06,
                        "final_ratio": 0.03,
                    },
                    "holding_rebalance_control": {
                        "decision": "allow_same_side_rebalance",
                        "raw_target_ratio": 0.03,
                        "final_target_ratio": 0.03,
                    },
                },
                "protective_reduce_no_continuation",
            ),
            (
                {
                    "winning_template_continuation": {
                        "decision": "protective_reduce_no_continuation",
                        "pre_control_ratio": 0.06,
                        "final_ratio": 0.04,
                    },
                    "holding_rebalance_control": {
                        "decision": "cap_same_side_reduction",
                        "raw_target_ratio": 0.04,
                        "final_target_ratio": 0.03,
                    },
                },
                "cap_same_side_reduction",
            ),
        )
        for diagnostics, expected_decision in cases:
            with self.subTest(expected_decision=expected_decision):
                recommendation = _canonical_recommendation(
                    expected_decision,
                    current_lots=8,
                    target_lots=3,
                    control_diagnostics=diagnostics,
                )
                contract = recommendation["signal_snapshot"]["final_action_contract"]
                trace = contract["learning_used"]["pm_lifecycle_learning_trace"]
                impact = contract["learning_used"]["pm_lifecycle_learning_impact_delta"]

                self.assertEqual(trace["contract_lifecycle_port"], "reduce_exit")
                self.assertEqual(
                    trace["reduce_exit_learning_decision"]["decision"],
                    expected_decision,
                )
                self.assertEqual(trace["hold_learning_decision"], {})
                self.assertEqual(trace["open_add_learning_decision"], {})
                self.assertEqual(trace["conditional_monitor_learning_decision"], {})
                self.assertEqual(impact["reduce_exit_decision"], expected_decision)
                self.assertIsNone(impact["hold_decision"])
                self.assertIsNone(impact["conditional_monitor_decision"])


class _ResearcherEntryDB:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)

    def get_config_id_by_name(self, _name: str) -> str:
        return "cfg"

    def get_trading_day_phase(self, _config_id: str, _trading_date: str, _phase) -> dict:
        return {"status": "completed"}

    def get_futures_recommendations_by_effective_date(self, _config_id: str, _trading_date: str) -> list:
        return []

    def get_futures_transactions_by_date(self, *_args, **_kwargs) -> list:
        return []

    def _ensure_reviewer_learning_schema(self, cursor: sqlite3.Cursor) -> None:
        _ensure_reviewer_learning_schema(cursor)


class ResearcherArtifactAtomicityTest(unittest.TestCase):
    def test_researcher_failure_rolls_back_database_and_artifact_files(self):
        from run.research import researcher_learning as entry

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            db_path = root / "agentquant.db"
            artifact_root = root / "artifacts"
            conn = _connection(str(db_path))
            conn.close()

            cfg = {
                "market_type": "china_futures",
                "exp_name": "atomicity-test",
                "trading_date": "2025-03-10",
            }
            fake_db = _ResearcherEntryDB(db_path)
            original_payload = {"state": "valid-before-failed-run", "padding": "x" * 256}
            replacement_payload = {"state": "failed-run-overwrite", "padding": "y" * 256}
            new_payload = {"state": "failed-run-new", "padding": "z" * 256}

            with patch.dict(os.environ, {"AGENTQUANT_ARTIFACT_ROOT": str(artifact_root)}, clear=False):
                existing = externalize_json_for_db(
                    original_payload,
                    category="signal_context",
                    record_id="existing",
                    field_name="payload",
                    config_id="cfg",
                    trading_date="2025-03-10",
                    inline_max_bytes=1,
                )
                existing_path = Path(existing.artifact_path)
                if not existing_path.is_absolute():
                    existing_path = SRC_ROOT.parent / existing_path
                original_bytes = existing_path.read_bytes()
                created_paths: list[Path] = []

                def write_researcher_artifacts(**kwargs):
                    externalize_json_for_db(
                        replacement_payload,
                        category="signal_context",
                        record_id="existing",
                        field_name="payload",
                        config_id="cfg",
                        trading_date="2025-03-10",
                        inline_max_bytes=1,
                    )
                    created = externalize_json_for_db(
                        new_payload,
                        category="no_trade_opportunity_memory",
                        record_id="new",
                        field_name="payload",
                        config_id="cfg",
                        trading_date="2025-03-10",
                        inline_max_bytes=1,
                    )
                    created_path = Path(created.artifact_path)
                    if not created_path.is_absolute():
                        created_path = SRC_ROOT.parent / created_path
                    created_paths.append(created_path)
                    template_prior_path = root / "attribution" / "template_prior.json"
                    _export_template_prior(
                        kwargs["cursor"],
                        cfg={
                            "learning": {
                                "template_prior": {
                                    "enabled": True,
                                    "export_on_backtest_end": True,
                                    "path": str(template_prior_path),
                                }
                            }
                        },
                        config_id="cfg",
                        trading_date="2025-03-10",
                    )
                    created_paths.append(template_prior_path)
                    return {"injected_learning_rows": 1}

                def write_snapshot(**_kwargs):
                    snapshot_path = root / "reviewer" / "run" / "2025-03-10.json"
                    write_artifact_text(snapshot_path, '{"snapshot": true}')
                    created_paths.append(snapshot_path)
                    return {"json": str(snapshot_path)}

                def fail_after_completion_insert(cursor, **kwargs):
                    insert_researcher_learning_completion_event(cursor, **kwargs)
                    raise RuntimeError("injected_researcher_failure")

                parser = SimpleNamespace(get_config=lambda: cfg)
                with patch.object(entry, "ConfigParser", return_value=parser), patch.object(
                    entry, "db_initialize", return_value=None
                ), patch.object(entry, "get_db", return_value=fake_db), patch.object(
                    entry, "Router", return_value=SimpleNamespace()
                ), patch.object(entry, "_fetchone", return_value={}), patch.object(
                    entry, "_review_recommendation_execution_facts", return_value=Counter()
                ), patch.object(
                    entry, "researcher_agent", side_effect=write_researcher_artifacts
                ), patch.object(
                    entry, "_write_historical_learning_snapshot_report", side_effect=write_snapshot
                ), patch.object(
                    entry.research_memory_writers,
                    "insert_researcher_learning_completion_event",
                    side_effect=fail_after_completion_insert,
                ), patch.object(
                    sys,
                    "argv",
                    [
                        "researcher_learning.py",
                        "--config",
                        "src/config/dev.yaml",
                        "--trading-date",
                        "2025-03-10",
                        "--local-db",
                    ],
                ):
                    with self.assertRaisesRegex(RuntimeError, "injected_researcher_failure"):
                        entry.main()

                verify = sqlite3.connect(db_path)
                try:
                    completion_count = verify.execute(
                        "SELECT COUNT(*) FROM learning_event_log "
                        "WHERE event_type='researcher_learning_completed'"
                    ).fetchone()[0]
                finally:
                    verify.close()

                self.assertEqual(completion_count, 0)
                self.assertEqual(existing_path.read_bytes(), original_bytes)
                self.assertEqual(len(created_paths), 3)
                self.assertTrue(all(not path.exists() for path in created_paths))


if __name__ == "__main__":
    unittest.main()
