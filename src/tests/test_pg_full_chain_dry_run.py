from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_full_chain_dry_run import run_no_llm_full_chain_dry_run
from tools.agent_tools.control.pg_pre_backtest_acceptance import (
    _initialize_formal_temp_database,
    _load_config,
)
from tools.agent_tools.control.pg_system_invariants import audit_system_invariants


class ProtocolGovernorFullChainDryRunTest(unittest.TestCase):
    def test_one_formal_database_runs_real_production_chain_and_next_day_reads(self):
        from tools.agent_tools.control import pg_full_chain_dry_run as module

        cfg = _load_config(SRC_ROOT / "config" / "dev.yaml")
        cfg["exp_name"] = "pg-full-chain-dry-run-test"
        cfg["tickers"] = ["BU"]

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            db_path = root / "agentquant.db"
            artifact_root = root / "artifacts"
            _initialize_formal_temp_database(db_path)

            with patch.object(
                module,
                "build_learning_context",
                wraps=module.build_learning_context,
            ) as analyst_learning_read, patch.object(
                module,
                "retrieve_pm_memory",
                wraps=module.retrieve_pm_memory,
            ) as pm_learning_read:
                result = run_no_llm_full_chain_dry_run(
                    db_path=db_path,
                    cfg=cfg,
                    artifact_root=artifact_root,
                )

            self.assertTrue(result.passed, result.to_dict())
            analyst_learning_read.assert_called()
            pm_learning_read.assert_called_once()

            daily_report = audit_system_invariants(
                db_path=db_path,
                exp_name="protocol-governor-full-chain-dry-run",
                start_date="2025-03-10",
                end_date="2025-03-10",
            )
            self.assertTrue(daily_report.passed, daily_report.to_dict())

            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            try:
                signals = connection.execute(
                    "SELECT id, analyst, ticker, llm_prompt FROM signal ORDER BY analyst"
                ).fetchall()
                self.assertEqual(len(signals), 3)
                self.assertEqual(
                    {row["analyst"] for row in signals},
                    {"technical", "fundamental", "commodity_news"},
                )
                self.assertTrue(all(not row["llm_prompt"] for row in signals))

                recommendation = connection.execute(
                    "SELECT id, signal_snapshot, signal_snapshot_artifact_path, signal_snapshot_sha256 "
                    "FROM futures_recommendation WHERE source_type='strategy'"
                ).fetchone()
                self.assertIsNotNone(recommendation)
                from database.artifact_store import load_externalized_json

                snapshot = load_externalized_json(
                    recommendation["signal_snapshot"],
                    recommendation["signal_snapshot_artifact_path"],
                    recommendation["signal_snapshot_sha256"],
                )
                if isinstance(snapshot, str):
                    snapshot = json.loads(snapshot)
                source_ids = {
                    row["signal_record_id"]
                    for row in snapshot["signal_collection_contract"]["source_contracts"]
                }
                self.assertEqual(source_ids, {row["id"] for row in signals})

                phases = connection.execute(
                    "SELECT phase, status FROM trading_day_phase ORDER BY phase"
                ).fetchall()
                self.assertEqual(
                    {(row["phase"], row["status"]) for row in phases},
                    {
                        ("phase1", "completed"),
                        ("phase2", "completed"),
                        ("phase3", "completed"),
                        ("phase4", "completed"),
                    },
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM daily_settlement").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM learning_event_log "
                        "WHERE event_type='researcher_learning_completed' AND status='applied'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
