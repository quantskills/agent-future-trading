from __future__ import annotations

"""Fixed historical system-failure fixtures for pre-backtest acceptance.

These fixtures are deterministic contract checks. They do not run a backtest,
read market data, write DB records, or evaluate strategy profitability.
"""

from typing import Any, Callable, Dict, Iterable, List, Mapping

from tools.agent_tools.control.pg_schemas import ProtocolCheckResult
from tools.agent_tools.control.pg_system_invariants import (
    _audit_action_values,
    _audit_trading_day_phase_completion,
    _audit_transaction_final_contract_consistency,
)
from tools.agent_tools.decision.pm_contract_self_check import check_final_action_contract
from tools.common.contracts import (
    validate_execution_artifact_boundary,
    validate_researcher_artifact_boundary,
    validate_reviewer_artifact_boundary,
)
from tools.common.final_action_semantics import validate_action_preference_family_consistency


MATRIX_FAILURE_FIXTURE_IDS = (
    "scc_missing",
    "scc_producer_boundary_invalid",
    "pm_incomplete_prior_in_formal_action_values",
    "observe_empty_preference",
    "observe_positive_candidate_forbidden",
    "step2_step6_trace_mixed",
    "execution_profile_pollutes_decision_rows",
    "action_family_lane_preference_mismatch",
    "trader_artifact_forbidden_fields",
    "reviewer_artifact_forbidden_fields",
    "researcher_artifact_forbidden_fields",
    "trader_transaction_not_from_final_contract",
    "unfinished_day_enters_learning",
)


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _scc_errors(contract: Any) -> List[str]:
    payload = contract if isinstance(contract, Mapping) else {}
    errors: List[str] = []
    if not payload:
        return ["signal_collection_contract_missing"]
    if str(payload.get("producer") or "") != "signal_collector":
        errors.append("signal_collection_contract_invalid_producer")
    if str(payload.get("collector_decision_boundary") or "") != "no_trade_authority":
        errors.append("signal_collection_contract_invalid_boundary")
    for field in (
        "final_action",
        "target_lots",
        "lots_delta",
        "opportunity_rank",
        "position_sizing_result",
        "capital_deployment",
        "final_action_contract",
    ):
        if field in payload:
            errors.append(f"signal_collection_contract_contains_pm_field:{field}")
    if not _present(payload.get("source_contracts")):
        errors.append("signal_collection_contract_missing_source_contracts")
    if not _present(payload.get("evidence_items")):
        errors.append("signal_collection_contract_missing_evidence_items")
    return errors


def _base_lifecycle_trace(port: str, decision_lanes: Iterable[str]) -> Dict[str, Any]:
    return {
        "trace_version": "agentquant.pm_lifecycle_learning_trace.v1",
        "contract_lifecycle_port": port,
        "rank_lifecycle": port,
        "used_lanes": list(decision_lanes),
        "accepted_learning_lanes": list(decision_lanes),
        "decision_learning_rows": [
            {"id": f"{lane}-fixture", "learning_lane": lane, "action_value_lane": lane, "action_name": lane}
            for lane in decision_lanes
        ],
        "trigger_profile_learning_rows": [],
        "execution_profile_learning_direct_to_rank": False,
        "trigger_profile_learning_direct_to_rank": False,
        "execution_profile_signal_direct_to_rank": False,
    }


def _base_contract(
    *,
    final_action: str,
    current_lots: int,
    target_lots: int,
    lifecycle_port: str,
    decision_lanes: Iterable[str],
    learning_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    lots_delta = target_lots - current_lots
    trace = _base_lifecycle_trace(lifecycle_port, decision_lanes)
    return {
        "contract_version": "agentquant.final_action_contract.v1",
        "contract_type": "strategy",
        "ticker": "HC",
        "final_action": final_action,
        "authority_type": "exploration_probe" if final_action == "open_probe" else "not_applicable",
        "current_lots": current_lots,
        "target_lots": target_lots,
        "lots_delta": lots_delta,
        "reason_codes": ["pre_backtest_failure_fixture"],
        "execution_profile": "fixture_execution_profile",
        "entry_trigger": "fixture_entry_trigger",
        "invalidation": "fixture_invalidation",
        "learning_used": {
            "alpha_setup_action_values": list(learning_rows),
            "pm_lifecycle_learning_trace": dict(trace),
            "pm_lifecycle_learning_impact_delta": {
                "trace_version": "agentquant.pm_lifecycle_learning_impact.v1",
                "execution_profile_learning_direct_to_rank": False,
                "trigger_profile_learning_direct_to_rank": False,
            },
        },
        "evidence_used": {
            "lifecycle_learning_trace": {
                **trace,
                "pm_final_contract_lifecycle_trace": dict(trace),
            },
            "pm_lifecycle_learning_trace": dict(trace),
            "learning_impact_delta": {
                "trace_version": "agentquant.pm_lifecycle_learning_impact.v1",
                "execution_profile_learning_direct_to_rank": False,
                "trigger_profile_learning_direct_to_rank": False,
            },
            "pm_lifecycle_learning_impact_delta": {
                "trace_version": "agentquant.pm_lifecycle_learning_impact.v1",
                "execution_profile_learning_direct_to_rank": False,
                "trigger_profile_learning_direct_to_rank": False,
            },
        },
        "capital_deployment": {
            "selected_for_capital_deployment": False,
            "new_risk_rank_required": False,
            "capital_allocation_reason": "non_new_risk_no_capital_rank",
        },
        "position_sizing_result": {
            "sizing_method": "pre_backtest_failure_fixture",
            "current_lots": current_lots,
            "target_lots": target_lots,
            "lots_delta": lots_delta,
        },
    }


def _canonical_learning_row(*, lane: str, family: str, preference: str) -> Dict[str, Any]:
    return {
        "id": f"{lane}-canonical-fixture",
        "ticker": "HC",
        "side": "long",
        "setup_type": "news_event_setup",
        "action_name": lane,
        "canonical_action_value": True,
        "canonical_action_family": family,
        "action_value_lane": lane,
        "learning_lane": lane,
        "action_preference": preference,
        "consumer_scope": "pm_learning",
    }


def _action_value_row(
    *,
    action_name: str,
    family: str,
    lane: str,
    preference: str,
    reward_sum: float,
) -> Dict[str, Any]:
    payload = {
        "canonical_action_family": family,
        "action_value_lane": lane,
        "learning_lane": lane,
        "action_preference": preference,
        "reward_source": "trade_episode",
        "real_trade_reward_count": 1,
        "amplification_scope_quality": "exact_real_state",
    }
    return {
        "ticker": "PB",
        "side": "long",
        "setup_type": "news_event_setup",
        "action_name": action_name,
        "sample_count": 1,
        "reward_sum": reward_sum,
        "reward_source": "trade_episode",
        "canonical_action_family": family,
        "action_value_lane": lane,
        "learning_lane": lane,
        "action_preference": preference,
        "consumer_scope": "pm_learning",
        "last_sample_date": "2025-03-26",
        "payload": payload,
    }


def _expect_prefix(errors: List[str], fixture_id: str, actual_errors: Iterable[str], prefix: str) -> None:
    if not any(str(error).startswith(prefix) for error in actual_errors):
        errors.append(f"pre_backtest_failure_fixture_not_blocked:{fixture_id}:{prefix}")


def _expect_clean(errors: List[str], fixture_id: str, actual_errors: Iterable[str]) -> None:
    actual = list(actual_errors)
    if actual:
        errors.append(f"pre_backtest_failure_fixture_false_positive:{fixture_id}:{actual}")


def _fixture_scc_missing(errors: List[str]) -> None:
    _expect_prefix(errors, "scc_missing", _scc_errors({}), "signal_collection_contract_missing")


def _fixture_scc_producer_boundary_invalid(errors: List[str]) -> None:
    actual = _scc_errors(
        {
            "producer": "portfolio_manager",
            "collector_decision_boundary": "trade_authority",
            "source_contracts": [{"id": "signal-1"}],
            "evidence_items": [{"id": "evidence-1"}],
        }
    )
    _expect_prefix(errors, "scc_producer_boundary_invalid", actual, "signal_collection_contract_invalid_producer")
    _expect_prefix(errors, "scc_producer_boundary_invalid", actual, "signal_collection_contract_invalid_boundary")


def _fixture_pm_incomplete_prior(errors: List[str]) -> None:
    contract = _base_contract(
        final_action="hold",
        current_lots=1,
        target_lots=1,
        lifecycle_port="hold",
        decision_lanes=["hold"],
        learning_rows=[
            {
                "id": "weak-prior",
                "action_name": "open",
                "action_value_lane": "open",
                "learning_lane": "open",
                "canonical_action_value": False,
                "canonical_action_value_source": "incomplete_trace_not_for_pm_scoring",
                "evidence_scope": "similar_sql_prior",
                "consumer_scope": "pm_learning",
            }
        ],
    )
    result = check_final_action_contract(contract)
    _expect_prefix(errors, "pm_incomplete_prior_in_formal_action_values", result.get("errors") or [], "alpha_setup_action_value_not_canonical")


def _fixture_observe_empty_preference(errors: List[str]) -> None:
    actual_errors: List[str] = []
    _audit_action_values(
        [
            _action_value_row(
                action_name="observe",
                family="observe",
                lane="hold",
                preference="",
                reward_sum=100.0,
            )
        ],
        actual_errors,
        [],
    )
    _expect_clean(errors, "observe_empty_preference", actual_errors)


def _fixture_observe_positive_candidate_forbidden(errors: List[str]) -> None:
    actual_errors: List[str] = []
    _audit_action_values(
        [
            _action_value_row(
                action_name="observe",
                family="observe",
                lane="hold",
                preference="positive_candidate_open",
                reward_sum=100.0,
            )
        ],
        actual_errors,
        [],
    )
    _expect_prefix(errors, "observe_positive_candidate_forbidden", actual_errors, "observe_action_value_positive_preference_forbidden")


def _fixture_step2_step6_trace_mixed(errors: List[str]) -> None:
    contract = _base_contract(
        final_action="open_probe",
        current_lots=0,
        target_lots=1,
        lifecycle_port="open_add_new_risk",
        decision_lanes=["hold"],
        learning_rows=[
            _canonical_learning_row(lane="hold", family="hold", preference="positive_candidate_hold")
        ],
    )
    result = check_final_action_contract(contract)
    _expect_prefix(errors, "step2_step6_trace_mixed", result.get("errors") or [], "open_rank_mixed_forbidden_learning_lanes")


def _fixture_execution_profile_pollutes_decision_rows(errors: List[str]) -> None:
    contract = _base_contract(
        final_action="exit",
        current_lots=2,
        target_lots=0,
        lifecycle_port="reduce_exit",
        decision_lanes=["execution"],
        learning_rows=[
            _canonical_learning_row(lane="exit", family="reduce_exit", preference="positive_candidate_exit")
        ],
    )
    result = check_final_action_contract(contract)
    _expect_prefix(errors, "execution_profile_pollutes_decision_rows", result.get("errors") or [], "reduce_exit_lifecycle_mixed_forbidden_learning_lanes")


def _fixture_action_family_lane_preference_mismatch(errors: List[str]) -> None:
    actual = validate_action_preference_family_consistency(
        {
            "canonical_action_family": "reduce_exit",
            "action_value_lane": "exit",
            "learning_lane": "exit",
            "action_preference": "positive_candidate_open",
        }
    )
    _expect_prefix(errors, "action_family_lane_preference_mismatch", actual.get("errors") or [], "positive_open_family_mismatch")


def _fixture_trader_artifact_forbidden_fields(errors: List[str]) -> None:
    try:
        validate_execution_artifact_boundary({"execution_result": {"learning_used": {"alpha_setup_action_values": []}}})
    except ValueError as exc:
        if "execution_artifact_forbidden_pm_fields" in str(exc):
            return
    errors.append("pre_backtest_failure_fixture_not_blocked:trader_artifact_forbidden_fields")


def _fixture_reviewer_artifact_forbidden_fields(errors: List[str]) -> None:
    try:
        validate_reviewer_artifact_boundary({"alpha_setup_action_value": {"action_preference": "positive_candidate_open"}})
    except ValueError as exc:
        if "reviewer_artifact_forbidden_research_or_mutation_fields" in str(exc):
            return
    errors.append("pre_backtest_failure_fixture_not_blocked:reviewer_artifact_forbidden_fields")


def _fixture_researcher_artifact_forbidden_fields(errors: List[str]) -> None:
    try:
        validate_researcher_artifact_boundary({"modified_daily_settlement": {"daily_pnl": 100.0}})
    except ValueError as exc:
        if "researcher_artifact_forbidden_trade_fact_mutation" in str(exc):
            return
    errors.append("pre_backtest_failure_fixture_not_blocked:researcher_artifact_forbidden_fields")


def _fixture_trader_transaction_not_from_final_contract(errors: List[str]) -> None:
    actual_errors: List[str] = []
    recommendation = {
        "id": "rec1",
        "source_type": "strategy",
        "trading_date": "2025-03-26",
        "underlying_code": "HC",
        "signal_snapshot": {
            "final_action_contract": {
                "contract_type": "strategy",
                "final_action": "open_probe",
                "authority_type": "exploration_probe",
                "current_lots": 0,
                "target_lots": 1,
                "lots_delta": 1,
            }
        },
        "audit_payload": {},
    }
    transaction = {
        "id": "tx1",
        "recommendation_id": "rec1",
        "source_type": "strategy",
        "trading_date": "2025-03-26",
        "ticker": "HC",
        "action": "close_long",
        "lots": 1,
        "audit_payload": {},
    }
    _audit_transaction_final_contract_consistency(
        [transaction],
        {"rec1": recommendation},
        actual_errors,
        [],
    )
    _expect_prefix(errors, "trader_transaction_not_from_final_contract", actual_errors, "transaction_not_derived_from_final_action_contract")


def _fixture_unfinished_day_enters_learning(errors: List[str]) -> None:
    actual_errors: List[str] = []
    _audit_trading_day_phase_completion(
        [
            {"trading_date": "2025-03-26", "phase": "phase1", "status": "completed"},
            {"trading_date": "2025-03-26", "phase": "phase2", "status": "running"},
        ],
        {"rec1": {"id": "rec1", "trading_date": "2025-03-26"}},
        [],
        [],
        [
            {
                "id": "av1",
                "last_sample_date": "2025-03-26",
            }
        ],
        actual_errors,
    )
    _expect_prefix(errors, "unfinished_day_enters_learning", actual_errors, "incomplete_trading_day_phase")


FIXTURE_RUNNERS: Dict[str, Callable[[List[str]], None]] = {
    "scc_missing": _fixture_scc_missing,
    "scc_producer_boundary_invalid": _fixture_scc_producer_boundary_invalid,
    "pm_incomplete_prior_in_formal_action_values": _fixture_pm_incomplete_prior,
    "observe_empty_preference": _fixture_observe_empty_preference,
    "observe_positive_candidate_forbidden": _fixture_observe_positive_candidate_forbidden,
    "step2_step6_trace_mixed": _fixture_step2_step6_trace_mixed,
    "execution_profile_pollutes_decision_rows": _fixture_execution_profile_pollutes_decision_rows,
    "action_family_lane_preference_mismatch": _fixture_action_family_lane_preference_mismatch,
    "trader_artifact_forbidden_fields": _fixture_trader_artifact_forbidden_fields,
    "reviewer_artifact_forbidden_fields": _fixture_reviewer_artifact_forbidden_fields,
    "researcher_artifact_forbidden_fields": _fixture_researcher_artifact_forbidden_fields,
    "trader_transaction_not_from_final_contract": _fixture_trader_transaction_not_from_final_contract,
    "unfinished_day_enters_learning": _fixture_unfinished_day_enters_learning,
}


def run_pre_backtest_failure_fixtures() -> ProtocolCheckResult:
    errors: List[str] = []
    passed: List[str] = []
    for fixture_id in MATRIX_FAILURE_FIXTURE_IDS:
        before = len(errors)
        FIXTURE_RUNNERS[fixture_id](errors)
        if len(errors) == before:
            passed.append(fixture_id)
    metadata = {
        "source_of_truth": "docs/matrix_chain_contract.md",
        "fixture_ids": list(MATRIX_FAILURE_FIXTURE_IDS),
        "passed_fixture_ids": passed,
        "strategy_profitability_checked": False,
        "boundary": "pre_backtest_system_contract_failures_only_no_strategy_profitability",
    }
    if errors:
        return ProtocolCheckResult.fail_result(errors, metadata=metadata)
    return ProtocolCheckResult.pass_result(metadata=metadata)
