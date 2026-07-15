import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.constants import Signal
from graph.schema import AnalystSignal, FuturesAction, FuturesRecommendation, RecommendationAction, RecommendationStatus
from graph.workflow import AgentWorkflow
from database.artifact_store import load_externalized_json
from database.sqlite_helper import SQLiteDB
from tests.contract_test_fixtures import build_test_aec


class _FakeDB:
    def __init__(self):
        self.saved_signals = []
        self.saved_recommendations = []

    def get_latest_settled_portfolio(self, config_id):
        return {
            "id": "p1",
            "cashflow": 1_000_000.0,
            "account_equity": 1_000_000.0,
            "cash_available": 1_000_000.0,
            "positions": {},
            "margin_used": 0.0,
            "margin_available": 1_000_000.0,
            "margin_ratio": 0.0,
        }

    def get_futures_recommendations_by_effective_date(self, **kwargs):
        return []

    def save_signal(self, portfolio_id, analyst, ticker, signal):
        self.saved_signals.append((portfolio_id, analyst, ticker, signal))
        return f"sig-{ticker}-{analyst}"

    def get_signal_persistence_counts(self, portfolio_id, tickers, analysts):
        rows = [
            {"ticker": ticker, "analyst": analyst}
            for saved_portfolio_id, analyst, ticker, _signal in self.saved_signals
            if saved_portfolio_id == portfolio_id
        ]
        pairs = [(str(row["ticker"]).upper(), str(row["analyst"])) for row in rows]
        duplicate_pairs = []
        for pair in sorted(set(pairs)):
            if pairs.count(pair) > 1:
                duplicate_pairs.append(f"{pair[0]}:{pair[1]}")
        return {
            "rows": rows,
            "row_total": len(rows),
            "distinct_pairs": len(set(pairs)),
            "duplicate_pairs": duplicate_pairs,
        }

    def save_futures_recommendation(self, recommendation):
        self.saved_recommendations.append(recommendation)
        return f"rec-{recommendation.underlying_code}"

    def update_futures_recommendation_status(self, recommendation_id, status, **kwargs):
        for recommendation in self.saved_recommendations:
            if getattr(recommendation, "id", None) == recommendation_id:
                recommendation.status = status
                for key, value in kwargs.items():
                    setattr(recommendation, key, value)
                break
        return True


class _FakeRouter:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def resolve_pre_open_reference_price(self, underlying_code, trading_date, contract_code=None):
        self.calls.append(underlying_code)
        return SimpleNamespace(
            base_price=100.0,
            base_price_source="t_minus_1_close_fallback",
            base_price_date="2025-01-01",
            open_price=None,
            prev_close_price=100.0,
            warning_message=None,
        )


class Phase1AccelerationTest(unittest.TestCase):
    def _config(self):
        return {
            "exp_name": "test-exp",
            "trading_date": datetime(2025, 1, 2),
            "market_type": "china_futures",
            "tickers": ["A", "B"],
            "workflow_analysts": ["technical", "fundamental"],
            "planner_mode": False,
            "llm": {"provider": "CodexOpenAI", "model": "fake"},
            "runtime": {
                "phase1": {
                    "enable_analysis_parallelism": True,
                    "max_parallel_analysis_tickers": 2,
                    "prefetch_pre_open_reference_prices": True,
                }
            },
            "max_total_margin_ratio": 0.2,
            "max_single_margin_ratio": 0.12,
        }

    def test_parallel_phase1_prefetches_analysis_but_saves_and_runs_pm_sequentially(self):
        fake_db = _FakeDB()
        pm_order = []

        def fake_get_agent_func(analyst):
            def _agent(state):
                signal = AnalystSignal(
                    agent_name=analyst,
                    signal=Signal.NEUTRAL,
                    confidence=0.5,
                    justification=f"{analyst} neutral",
                    metadata={
                        "action_evidence_contract": build_test_aec(
                            analyst,
                            ticker=state["ticker"],
                            trading_date="2025-01-02",
                        )
                    },
                )
                return {"analyst_signals": [signal]}

            return _agent

        def fake_pm(state):
            pm_order.append(state["ticker"])
            ticker = state["ticker"]
            return {
                "pm_state": {
                    "ticker": ticker,
                    "current_lots": 0,
                    "target_lots": 0,
                    "signal_collection_contract": state["signal_collection_contract"],
                    "recommendation_context": {"underlying_code": ticker},
                }
            }

        def fake_finalize_pm_contracts(*, generated, config, portfolio):
            signed = []
            for ticker, pm_state in generated:
                recommendation = FuturesRecommendation(
                    config_id="cfg",
                    reference_portfolio_id="p1",
                    trading_date="2025-01-02",
                    effective_trade_date="2025-01-02",
                    underlying_code=ticker,
                    contract_code=f"{ticker}01",
                    action=RecommendationAction.HOLD,
                    lots=0,
                    base_price=100.0,
                    justification="test PM final",
                    signal_snapshot={
                    "signal_collection_contract": pm_state["signal_collection_contract"],
                    "final_action_contract": {
                        "ticker": ticker,
                        "contract_code": f"{ticker}01",
                        "final_action": "wait",
                        "current_lots": 0,
                        "target_lots": 0,
                        "lots_delta": 0,
                        "reason_codes": ["test_pm_final_contract"],
                    },
                    "pm_six_step_trace": {
                        "step6_contract_generation_check": {"ok": True},
                        "pm_contract_self_check": {"ok": True, "tool": "pm_contract_self_check"}
                    },
                    },
                )
                signed.append((ticker, recommendation))
            return signed

        with patch("graph.workflow.get_db", return_value=fake_db), patch(
            "graph.workflow.Router", _FakeRouter
        ), patch("graph.workflow.AgentRegistry.check_agent_key", return_value=True), patch(
            "graph.workflow.FuturesContractInfoCache.get_contract_info",
            return_value={"contract_multiplier": 10, "margin_rate_long": 0.1, "margin_rate_short": 0.1},
        ), patch("graph.workflow.AgentRegistry.get_agent_func_by_key", side_effect=fake_get_agent_func), patch(
            "agents.decision_team.portfolio_manager.portfolio_agent_futures", side_effect=fake_pm
        ), patch(
            "agents.decision_team.portfolio_manager.finalize_pm_full_market_contracts",
            side_effect=fake_finalize_pm_contracts,
        ), patch("graph.workflow.logger.info") as info_log, patch(
            "graph.workflow.logger.log_portfolio"
        ) as portfolio_log:
            workflow = AgentWorkflow(self._config(), "cfg")
            elapsed = workflow.run("cfg")

        self.assertGreaterEqual(elapsed, 0.0)
        self.assertEqual(pm_order, ["A", "B"])
        self.assertEqual([rec.underlying_code for rec in fake_db.saved_recommendations], ["A", "B"])
        self.assertEqual(len(fake_db.saved_signals), 4)
        self.assertTrue(
            all(
                getattr(row[3], "metadata", {}).get("signal_record_id")
                == f"sig-{row[2]}-{row[1]}"
                for row in fake_db.saved_signals
            )
        )
        self.assertTrue(
            all(
                set(getattr(row[3], "metadata", {}))
                == {"action_evidence_contract", "signal_record_id"}
                for row in fake_db.saved_signals
            )
        )
        self.assertTrue(
            all(rec.status != RecommendationStatus.SKIPPED for rec in fake_db.saved_recommendations)
        )
        portfolio_log.assert_not_called()
        logged = "\n".join(str(call.args[0]) for call in info_log.call_args_list)
        for forbidden in (
            "Portfolio ID",
            "Active analysts",
            "fanout completed",
            "prefetch",
            "DB/report writes",
            "timing summary",
        ):
            self.assertNotIn(forbidden, logged)

    def test_sqlite_signal_save_replaces_same_ticker_analyst_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = SQLiteDB()
            db.db_path = str(Path(temp_dir) / "agentquant.db")
            from database.sqlite_setup import init_database

            with patch("database.sqlite_setup.DB_PATH", db.db_path):
                init_database()
            portfolio = db.create_portfolio("cfg", 1_000_000.0, "2025-01-02")
            portfolio_id = portfolio["id"]
            first = AnalystSignal(
                agent_name="technical",
                signal=Signal.NEUTRAL,
                confidence=0.4,
                justification="early signal",
                metadata={
                    "action_evidence_contract": build_test_aec(
                        "technical",
                        ticker="A",
                        trading_date="2025-01-02",
                    )
                },
            )
            second = AnalystSignal(
                agent_name="technical",
                signal=Signal.BULLISH,
                confidence=0.7,
                justification="final signal",
                metadata={
                    "action_evidence_contract": build_test_aec(
                        "technical",
                        ticker="A",
                        trading_date="2025-01-02",
                        signal="Bullish",
                        side="long",
                        confidence=0.7,
                    )
                },
            )

            db.save_signal(portfolio_id, "technical", "A", first)
            final_id = db.save_signal(
                portfolio_id,
                "technical",
                "A",
                second,
            )

            conn = db._get_connection()
            try:
                rows = conn.execute(
                    "SELECT id, signal, justification FROM signal WHERE portfolio_id=? AND ticker=? AND analyst=?",
                    (portfolio_id, "A", "technical"),
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], final_id)
        self.assertIn("Bullish", rows[0]["signal"])
        self.assertEqual(rows[0]["justification"], "")

    def test_sqlite_signal_artifact_exposes_machine_readable_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = SQLiteDB()
            db.db_path = str(Path(temp_dir) / "agentquant.db")
            from database.sqlite_setup import init_database

            with patch("database.sqlite_setup.DB_PATH", db.db_path):
                init_database()
            portfolio = db.create_portfolio("cfg", 1_000_000.0, "2025-01-02")
            signal = AnalystSignal(
                agent_name="technical",
                signal=Signal.NEUTRAL,
                confidence=0.5,
                justification="metadata audit",
                metadata={
                    "action_evidence_contract": build_test_aec(
                        "technical",
                        ticker="TA",
                        trading_date="2025-01-02",
                    ),
                    "llm_path": "cloud:g5.4:high",
                    "data_usage_summary": {"pandaai_daily": {"available": True, "used_in_signal": True}},
                    "technical_parameter_calibration": {"applied": True, "source": "policy"},
                    "adaptive_params": {"ema_long": 52},
                },
            )

            db.save_signal(
                portfolio["id"],
                "technical",
                "TA",
                signal,
            )

            conn = db._get_connection()
            try:
                row = conn.execute(
                    """
                    SELECT artifact_json, artifact_json_artifact_path, artifact_json_sha256
                    FROM signal
                    WHERE portfolio_id=? AND ticker='TA' AND analyst='technical'
                    """,
                    (portfolio["id"],),
                ).fetchone()
            finally:
                conn.close()

        payload = load_externalized_json(
            row["artifact_json"],
            row["artifact_json_artifact_path"],
            row["artifact_json_sha256"],
        )
        self.assertEqual(set(payload), {"metadata", "signal_artifact_metadata"})
        self.assertEqual(set(payload["metadata"]), {"action_evidence_contract"})
        self.assertNotIn("llm_path", payload)
        self.assertNotIn("technical_parameter_calibration", payload)
        self.assertNotIn("adaptive_params", payload)
        self.assertEqual(payload["signal_artifact_metadata"]["contract_version"], "agentquant.signal_artifact.v1")


if __name__ == "__main__":
    unittest.main()
