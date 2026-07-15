import json
import sqlite3
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from database import sqlite_setup
from tests.contract_test_fixtures import build_test_aec, build_test_signal
from tools.agent_tools.control.pg_system_invariants import DAILY_CHECK_NAMES, audit_system_invariants
from tools.common.signal_evidence_collection import build_signal_collection_contract


DAY = "2025-03-10"
CONFIG_ID = "cfg"
PORTFOLIO_ID = "pf"
ANALYSTS = ("technical", "fundamental", "commodity_news")


def _signal_collection_contract() -> dict:
    signals = [
        build_test_signal(
            analyst,
            signal_record_id=f"signal-{analyst}",
            ticker="RB",
            trading_date=DAY,
        )
        for analyst in ANALYSTS
    ]
    return build_signal_collection_contract(
        ticker="RB",
        trading_date=DAY,
        analyst_signals=signals,
        enabled_analysts=ANALYSTS,
    )


def _final_action_contract(
    *,
    current_lots: int = 0,
    target_lots: int = 0,
    contract_code: str = "RB2505",
    conditional: bool = False,
) -> dict:
    if target_lots == current_lots:
        final_action = "hold" if current_lots else "wait"
    elif current_lots == 0:
        final_action = "open_probe"
    elif target_lots == 0:
        final_action = "exit"
    elif abs(target_lots) > abs(current_lots):
        final_action = "increase"
    else:
        final_action = "decrease"
    return {
        "contract_version": "agentquant.final_action.v1",
        "ticker": "RB",
        "contract_code": contract_code,
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": target_lots - current_lots,
        "final_action": final_action,
        "authority_type": "conditional_trigger" if conditional else "not_applicable",
        "invalidation_condition": "close_below_validated_boundary" if target_lots else "",
        "requires_intraday_confirmation": conditional,
        "can_execute_without_intraday_trigger": not conditional,
        "conditional_trigger_authority": conditional,
    }


def _full_auditor_payload(
    rec_id: str,
    fac: dict,
    *,
    verdict: str = "approve",
) -> dict:
    approved = verdict in {"approve", "approve_with_warning"}
    return {
        "contract_version": "agentquant.audit_verdict.v1",
        "producer": "auditor",
        "agent_name": "auditor",
        "recommendation_id": rec_id,
        "ticker": "RB",
        "trading_date": DAY,
        "config_id": CONFIG_ID,
        "audit_status": "approved" if approved else "blocked",
        "audit_verdict": verdict,
        "audit_reason_codes": [],
        "hard_risk_reasons": [],
        "soft_risk_reasons": [],
        "audited_by": "auditor",
        "audited_at": f"{DAY}T01:45:00+00:00",
        "source": {
            "pm_recommendation_id": rec_id,
            "final_action_contract_hash_source": (
                "futures_recommendation.signal_snapshot.final_action_contract"
            ),
            "contract_state_source": "router_contract_state",
            "data_quality_source": "signal_collection_contract.data_quality_summary",
        },
        "boundary": {
            "auditor_does_not_modify_final_action_contract": True,
            "auditor_does_not_create_trade_authority": True,
            "trader_requires_approved_audit_verdict": True,
            "research_memory_not_consumed": True,
            "auditor_reads_research_db": False,
        },
        "contract_summary": {
            "final_action": fac["final_action"],
            "current_lots": fac["current_lots"],
            "target_lots": fac["target_lots"],
            "lots_delta": fac["lots_delta"],
            "contract_code": fac["contract_code"],
            "invalidation_present": bool(fac.get("invalidation_condition")),
            "requires_intraday_confirmation": fac["requires_intraday_confirmation"],
            "can_execute_without_intraday_trigger": fac[
                "can_execute_without_intraday_trigger"
            ],
        },
        "semantic_state": {
            "lifecycle_state": "conditional_monitor" if fac.get("requires_intraday_confirmation") else "ordinary_hold",
            "requires_intraday_result": bool(fac.get("requires_intraday_confirmation")),
            "hard_block_reasons": [],
            "soft_limit_reasons": [],
            "semantic_errors": [],
        },
    }


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
                (f"researcher_learning_completed:{CONFIG_ID}:{DAY}", CONFIG_ID, DAY, "researcher_learning_completed", "researcher", "trading_day", DAY, "{}", "{}", "deterministic_researcher_entry", f"{DAY} 05:00:00", "applied"),
            )
            for analyst in ANALYSTS:
                artifact = {
                    "metadata": {
                        "action_evidence_contract": build_test_aec(
                            analyst,
                            ticker="RB",
                            trading_date=DAY,
                        )
                    }
                }
                conn.execute(
                    "INSERT INTO signal(id,portfolio_id,updated_at,ticker,llm_prompt,analyst,signal,justification,artifact_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"signal-{analyst}", PORTFOLIO_ID, DAY, "RB", "", analyst, "neutral", "", json.dumps(artifact)),
                )
            self._insert_recommendation(conn)
            self._replace_settlement(conn, daily_pnl=0.0, commission=0.0, previous_equity=1000.0, current_equity=1000.0, previous_margin=0.0, current_margin=0.0)

    def _snapshot(
        self,
        *,
        rec_id="rec",
        fac=None,
        actual_transactions=None,
        outcome="executed_without_transaction",
        transaction_count=0,
        verdict="approve",
    ):
        final_contract = deepcopy(fac or _final_action_contract())
        audit_payload = _full_auditor_payload(rec_id, final_contract, verdict=verdict)
        return {
            "signal_collection_contract": _signal_collection_contract(),
            "final_action_contract": final_contract,
            "auditor": deepcopy(audit_payload),
            "execution_result": {
                "outcome": outcome,
                "status": "completed",
                "transaction_count": transaction_count,
                "actual_transactions": list(actual_transactions or []),
                "no_trade_reason": None if actual_transactions else "position_matched",
            },
        }

    def _insert_recommendation(self, conn, *, rec_id="rec", source_type="strategy", verdict="approve", snapshot=None):
        payload = deepcopy(snapshot or self._snapshot(rec_id=rec_id, verdict=verdict))
        fac = payload.get("final_action_contract") or _final_action_contract()
        audit_payload = _full_auditor_payload(rec_id, fac, verdict=verdict)
        payload["auditor"] = deepcopy(audit_payload)
        audit_payload.update(
            {
                "trade_contract_audit": {},
                "execution_translation": {},
                "execution_result": deepcopy(payload.get("execution_result") or {}),
                "phase2_execution": {"status": "completed"},
            }
        )
        conn.execute(
            "INSERT INTO futures_recommendation(id,config_id,reference_portfolio_id,trading_date,effective_trade_date,source_type,underlying_code,contract_code,action,lots,signal_snapshot,audit_payload,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec_id, CONFIG_ID, PORTFOLIO_ID, DAY, DAY, source_type, "RB", "RB2505", "hold", 0, json.dumps(payload), json.dumps(audit_payload), "pending", DAY),
        )

    def _insert_transaction(
        self,
        conn,
        *,
        rec_id="rec",
        source_type="strategy",
        action="open_long",
        lots=1,
        contract_code="RB2505",
    ):
        recommendation = conn.execute(
            "SELECT signal_snapshot FROM futures_recommendation WHERE id=?",
            (rec_id,),
        ).fetchone()
        snapshot = json.loads(recommendation[0]) if recommendation and recommendation[0] else {}
        fac = snapshot.get("final_action_contract") or _final_action_contract()
        audit_payload = _full_auditor_payload(rec_id, fac)
        conn.execute(
            "INSERT INTO futures_transactions(id,portfolio_id,config_id,recommendation_id,trading_date,ticker,contract_code,action,lots,execution_price,contract_multiplier,margin_rate,margin_used,commission,source_type,execution_phase,audit_payload,booked_in_settlement,justification,llm_prompt,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"tx-{rec_id}-{action}", PORTFOLIO_ID, CONFIG_ID, rec_id, DAY, "RB", contract_code, action, lots, 3500.0, 10.0, 0.1, 3500.0, 2.0, source_type, "phase2", json.dumps(audit_payload), 1, "", "", f"{DAY} 02:30:00"),
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

    def _set_strategy_execution(
        self,
        conn,
        *,
        target_lots=1,
        executed_lots=1,
        contract_code="RB2505",
        conditional=False,
    ):
        fac = _final_action_contract(
            target_lots=target_lots,
            contract_code=contract_code,
            conditional=conditional,
        )
        action = "open_long" if target_lots > 0 else "open_short"
        actual = [
            {
                "action": action,
                "lots": executed_lots,
                "contract_code": contract_code,
                "execution_price": 3500.0,
                "execution_phase": "phase2",
            }
        ]
        snapshot = self._snapshot(
            fac=fac,
            actual_transactions=actual,
            outcome="executed",
            transaction_count=1,
        )
        audit_payload = _full_auditor_payload("rec", fac)
        audit_payload.update(
            {
                "trade_contract_audit": {},
                "execution_translation": {},
                "execution_result": deepcopy(snapshot["execution_result"]),
                "phase2_execution": {"status": "completed"},
            }
        )
        conn.execute(
            "UPDATE futures_recommendation SET contract_code=?, action=?, lots=?, signal_snapshot=?, audit_payload=? WHERE id='rec'",
            (
                contract_code,
                action,
                abs(target_lots),
                json.dumps(snapshot),
                json.dumps(audit_payload),
            ),
        )
        self._insert_transaction(
            conn,
            action=action,
            lots=executed_lots,
            contract_code=contract_code,
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
            forced_snapshot = self._snapshot(actual_transactions=forced_actual, outcome="executed", transaction_count=1)
            forced_snapshot["forced_risk_boundary"] = {"reason": "margin_risk_reduction"}
            self._insert_recommendation(conn, rec_id="risk", source_type="forced_risk", snapshot=forced_snapshot)
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

    def test_scc_signal_record_id_must_resolve_to_the_exact_sql_aec(self):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT signal_snapshot FROM futures_recommendation WHERE id='rec'"
            ).fetchone()
            snapshot = json.loads(row[0])
            snapshot["signal_collection_contract"]["source_contracts"][0][
                "signal_record_id"
            ] = "missing-signal-id"
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec'",
                (json.dumps(snapshot),),
            )
        report = self._audit()
        self.assertIn(
            "strategy_scc_signal_record_id_mismatch",
            self._check(report, "physical_result_landing").violation_codes,
        )

    def test_scc_and_aec_are_checked_by_the_shared_validators(self):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT signal_snapshot FROM futures_recommendation WHERE id='rec'"
            ).fetchone()
            snapshot = json.loads(row[0])
            snapshot["signal_collection_contract"]["unregistered_pg_field"] = True
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec'",
                (json.dumps(snapshot),),
            )
        report = self._audit()
        self.assertIn(
            "strategy_signal_collection_contract_invalid",
            self._check(report, "physical_result_landing").violation_codes,
        )

    def test_duplicate_analyst_sql_signal_is_rejected(self):
        with self._connect() as conn:
            artifact = {
                "metadata": {
                    "action_evidence_contract": build_test_aec(
                        "technical",
                        ticker="RB",
                        trading_date=DAY,
                    )
                }
            }
            conn.execute(
                "INSERT INTO signal(id,portfolio_id,updated_at,ticker,llm_prompt,analyst,signal,justification,artifact_json) VALUES(?,?,?,?,?,?,?,?,?)",
                ("duplicate-technical", PORTFOLIO_ID, DAY, "RB", "", "technical", "neutral", "", json.dumps(artifact)),
            )
        report = self._audit()
        self.assertIn(
            "strategy_analyst_signal_duplicate",
            self._check(report, "physical_result_landing").violation_codes,
        )

    def test_persisted_agent_prompt_is_an_information_isolation_failure(self):
        with self._connect() as conn:
            conn.execute(
                "UPDATE signal SET llm_prompt='private prompt' WHERE id='signal-technical'"
            )
        report = self._audit()
        self.assertIn(
            "agent_internal_information_persisted",
            self._check(report, "physical_result_landing").violation_codes,
        )

    def test_transaction_source_type_must_be_explicit(self):
        with self._connect() as conn:
            self._set_strategy_execution(conn)
            conn.execute("UPDATE futures_transactions SET source_type=NULL")
        report = self._audit()
        self.assertIn(
            "transaction_source_type_missing",
            self._check(report, "single_trade_fact_source").violation_codes,
        )

    def test_strategy_transaction_requires_complete_auditor_payload(self):
        with self._connect() as conn:
            self._set_strategy_execution(conn)
            conn.execute(
                "UPDATE futures_recommendation SET audit_payload=? WHERE id='rec'",
                (json.dumps({"audit_verdict": "approve"}),),
            )
        report = self._audit()
        self.assertIn(
            "strategy_auditor_payload_incomplete",
            self._check(report, "audit_release_and_execution_result").violation_codes,
        )

    def test_strategy_execution_cannot_exceed_fac_authorized_lot_change(self):
        with self._connect() as conn:
            self._set_strategy_execution(conn, target_lots=1, executed_lots=2)
        report = self._audit()
        self.assertIn(
            "strategy_execution_exceeds_fac_authorized_lots",
            self._check(report, "execution_and_transaction_fact").violation_codes,
        )

    def test_strategy_execution_contract_and_direction_must_match_fac(self):
        with self._connect() as conn:
            self._set_strategy_execution(conn, target_lots=1, contract_code="RB2505")
            conn.execute(
                "UPDATE futures_transactions SET action='open_short', contract_code='RB2510'"
            )
            row = conn.execute(
                "SELECT signal_snapshot FROM futures_recommendation WHERE id='rec'"
            ).fetchone()
            snapshot = json.loads(row[0])
            snapshot["execution_result"]["actual_transactions"][0]["action"] = "open_short"
            snapshot["execution_result"]["actual_transactions"][0]["contract_code"] = "RB2510"
            conn.execute(
                "UPDATE futures_recommendation SET signal_snapshot=? WHERE id='rec'",
                (json.dumps(snapshot),),
            )
        report = self._audit()
        check = self._check(report, "execution_and_transaction_fact")
        self.assertIn("strategy_execution_direction_not_authorized", check.violation_codes)
        self.assertIn("strategy_execution_contract_not_authorized", check.violation_codes)

    def test_only_conditional_fac_requires_intraday_decision_fact(self):
        with self._connect() as conn:
            fac = _final_action_contract(target_lots=1, conditional=True)
            snapshot = self._snapshot(fac=fac, outcome="not_triggered")
            conn.execute(
                "UPDATE futures_recommendation SET contract_code=?, action='open_long', lots=1, signal_snapshot=?, audit_payload=? WHERE id='rec'",
                (
                    fac["contract_code"],
                    json.dumps(snapshot),
                    json.dumps(_full_auditor_payload("rec", fac)),
                ),
            )
        report = self._audit()
        self.assertIn(
            "conditional_fac_intraday_decision_missing",
            self._check(report, "execution_and_transaction_fact").violation_codes,
        )

    def test_settlement_positions_snapshot_must_match_portfolio_positions(self):
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolio SET positions=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "RB": {
                                "shares": 1,
                                "contract_code": "RB2505",
                                "margin_used": 0.0,
                            }
                        }
                    ),
                    PORTFOLIO_ID,
                ),
            )
        report = self._audit()
        self.assertIn(
            "settlement_positions_snapshot_mismatch",
            self._check(report, "settlement_and_account_fact").violation_codes,
        )

    def test_researcher_completion_must_follow_phase4_completion(self):
        with self._connect() as conn:
            conn.execute(
                "UPDATE learning_event_log SET created_at=? WHERE event_type='researcher_learning_completed'",
                (f"{DAY} 03:00:00",),
            )
        report = self._audit()
        self.assertIn(
            "researcher_completed_before_phase4",
            self._check(report, "daily_phase_completion").violation_codes,
        )

    def test_action_value_rows_use_canonical_action_semantics(self):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO alpha_setup_action_value(id,config_id,scope_key,ticker,side,horizon_class,market_regime,setup_type,data_combo,action_name,canonical_action_family,sample_count,reward_sum,reward_mean,win_rate,confidence_score,action_preference,reward_source,evidence_scope,action_value_lane,consumer_scope,learning_lane,memory_side_role,retrieval_key,last_sample_date,created_at,updated_at,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "action-value",
                    CONFIG_ID,
                    "RB:long:short:trend:breakout",
                    "RB",
                    "long",
                    "short",
                    "trend",
                    "breakout",
                    "all",
                    "open_probe",
                    "exit",
                    1,
                    1.0,
                    1.0,
                    1.0,
                    0.8,
                    "avoid",
                    "settled_trade",
                    "trade",
                    "exit",
                    "pm_learning",
                    "decision",
                    "directional",
                    "key",
                    DAY,
                    f"{DAY} 05:10:00",
                    f"{DAY} 05:10:00",
                    1,
                ),
            )
        report = self._audit()
        self.assertIn(
            "learning_action_value_contract_invalid",
            self._check(report, "learning_record_landing_boundary").violation_codes,
        )

    def test_existing_learning_source_requires_completed_phase4_and_settlement(self):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO alpha_setup_sample(id,config_id,trading_date,ticker,side,sector,horizon_class,market_regime,setup_type,data_combo,scope_key,source_type,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("historical-sample", CONFIG_ID, "2025-03-07", "RB", "long", "ferrous", "short", "trend", "breakout", "all", "key", "trade", f"{DAY} 05:10:00"),
            )
        report = self._audit()
        self.assertIn(
            "learning_source_phase4_or_settlement_missing",
            self._check(report, "learning_record_landing_boundary").violation_codes,
        )


if __name__ == "__main__":
    unittest.main()
