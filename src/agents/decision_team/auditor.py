from __future__ import annotations

"""Independent deterministic Auditor agent for PM final_action_contract.

The Auditor is a decision-team agent between portfolio_manager and trader. It
does not call LLM, does not read research memory, and never rewrites the PM
contract. It produces only audit facts: verdict, reasons, and payload.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from tools.common.contracts import (
    final_action_contract_from_snapshot,
    validate_auditor_artifact_boundary,
    validate_final_action_contract,
)
from tools.common.final_action_semantics import (
    classify_final_action_contract,
    contract_increases_risk_position,
    project_margin_transition,
    validate_final_action_lot_transition,
)


APPROVED_AUDIT_VERDICTS = {"approve", "approve_with_warning"}
BLOCKING_AUDIT_VERDICTS = {"block", "require_review"}


class AuditorInput(BaseModel):
    """Input payload for the independent Auditor agent."""

    recommendation_id: str = ""
    ticker: str = ""
    trading_date: Any = None
    config_id: str = ""
    source_type: str = "strategy"
    final_action_contract: Dict[str, Any] = Field(default_factory=dict)
    account_state: Dict[str, Any] = Field(default_factory=dict)
    position_state: Dict[str, Any] = Field(default_factory=dict)
    contract_state: Dict[str, Any] = Field(default_factory=dict)
    data_quality: Dict[str, Any] = Field(default_factory=dict)
    hard_risk_config: Dict[str, Any] = Field(default_factory=dict)


class AuditorOutput(BaseModel):
    """Audit fact written after PM signs and before Trader can execute."""

    audit_status: str = "blocked"
    audit_verdict: str = "block"
    audit_payload: Dict[str, Any] = Field(default_factory=dict)
    audit_reason_codes: List[str] = Field(default_factory=list)
    hard_risk_reasons: List[str] = Field(default_factory=list)
    soft_risk_reasons: List[str] = Field(default_factory=list)
    audited_by: str = "auditor"
    audited_at: str = ""


def _date_text(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _recommendation_snapshot(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation, dict) else {}
    return snapshot if isinstance(snapshot, dict) else {}


def _final_contract_from_recommendation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    return final_action_contract_from_snapshot(_recommendation_snapshot(recommendation))


def _is_new_or_increasing_exposure(contract: Dict[str, Any]) -> bool:
    return contract_increases_risk_position(contract)


def _hard_margin_limit(config: Dict[str, Any]) -> float:
    hard_cap = _safe_float((config or {}).get("max_total_margin_ratio"))
    return hard_cap if hard_cap is not None and hard_cap > 0 else -1.0


def _contract_margin_estimate(contract: Dict[str, Any]) -> Optional[float]:
    value = _safe_float(contract.get("target_margin_ratio_estimate"))
    if value is not None and value >= 0:
        return value
    evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
    sizing = evidence.get("position_sizing_result")
    if isinstance(sizing, dict):
        value = _safe_float(sizing.get("target_margin_ratio_estimate"))
        if value is not None and value >= 0:
            return value
    return None


def _has_valid_invalidation_boundary(contract: Dict[str, Any]) -> bool:
    for field in ("invalidation_condition", "invalidation"):
        value = contract.get(field)
        if isinstance(value, dict) and value:
            return True
        text = str(value or "").strip().lower()
        if text and text not in {"none", "unknown", "not_applicable"}:
            return True
    for field in ("invalidation_level", "atr_stop_distance"):
        value = _safe_float(contract.get(field))
        if value is not None and value > 0:
            return True
    return False


class Auditor:
    """Independent Auditor agent.

    It verifies a signed PM final_action_contract and writes audit facts. It is
    intentionally narrow: no research reads, no contract mutation, no order
    generation.
    """

    def __init__(self, hard_risk_config: Optional[Dict[str, Any]] = None):
        self.hard_risk_config = hard_risk_config or {}

    def audit(self, payload: AuditorInput) -> AuditorOutput:
        contract = dict(payload.final_action_contract or {})
        validation_errors = validate_final_action_contract(contract)
        transition = validate_final_action_lot_transition(contract)
        hard_reasons: List[str] = []
        soft_reasons: List[str] = []
        margin_projection: Dict[str, Optional[float]] = {}

        if validation_errors:
            hard_reasons.extend(validation_errors)
        hard_reasons.extend(transition.get("errors") or [])

        if str(payload.source_type or "") == "strategy" and not contract:
            hard_reasons.append("missing_pm_final_action_contract")

        current_lots = _safe_int(contract.get("current_lots"))
        position_lots = _safe_int(payload.position_state.get("current_lots"))
        if position_lots is None:
            hard_reasons.append("position_current_lots_missing")
        elif current_lots is None or current_lots != position_lots:
            hard_reasons.append("position_current_lots_mismatch")
        held_contract = str(payload.position_state.get("contract_code") or "").strip().upper()
        signed_contract = str(contract.get("contract_code") or "").strip().upper()
        if position_lots not in (None, 0) and held_contract and signed_contract != held_contract:
            hard_reasons.append("position_contract_code_mismatch")

        if _is_new_or_increasing_exposure(contract):
            if not signed_contract:
                hard_reasons.append("missing_contract_code")
            if not _has_valid_invalidation_boundary(contract):
                hard_reasons.append("missing_invalidation_condition")
            contract_state_code = str(payload.contract_state.get("contract_code") or "").strip().upper()
            if not contract_state_code:
                hard_reasons.append("contract_state_missing")
            elif signed_contract != contract_state_code:
                hard_reasons.append("contract_state_code_mismatch")
            contract_underlying = str(payload.contract_state.get("underlying_code") or "").strip().upper()
            if not contract_underlying or contract_underlying != str(payload.ticker or "").strip().upper():
                hard_reasons.append("contract_state_underlying_mismatch")
            contract_as_of = _date_text(payload.contract_state.get("as_of_date"))
            if not contract_as_of:
                hard_reasons.append("contract_state_as_of_date_missing")
            elif contract_as_of > _date_text(payload.trading_date):
                hard_reasons.append("contract_state_future_dated")
            if not str(payload.contract_state.get("source") or "").strip():
                hard_reasons.append("contract_state_source_missing")

            for field in ("account_equity", "margin_used", "margin_ratio", "risk_status"):
                if payload.account_state.get(field) in (None, ""):
                    hard_reasons.append(f"account_state_missing:{field}")
            target_margin = _contract_margin_estimate(contract)
            hard_cap = _hard_margin_limit(payload.hard_risk_config or self.hard_risk_config)
            account_equity = _safe_float(payload.account_state.get("account_equity"))
            account_margin_used = _safe_float(payload.account_state.get("margin_used"))
            account_margin_ratio = _safe_float(payload.account_state.get("margin_ratio"))
            if account_equity is not None and account_equity <= 0:
                hard_reasons.append("account_equity_invalid")
            if account_margin_used is not None and account_margin_used < 0:
                hard_reasons.append("account_margin_used_invalid")
            if account_margin_ratio is not None and account_margin_ratio < 0:
                hard_reasons.append("account_margin_ratio_invalid")
            if target_margin is None:
                hard_reasons.append("target_margin_ratio_estimate_missing")
            if hard_cap <= 0:
                hard_reasons.append("hard_margin_limit_missing")
            position_margin_used = 0.0
            if position_lots not in (None, 0):
                position_margin_used = _safe_float(payload.position_state.get("margin_used"))
                if position_margin_used is None:
                    hard_reasons.append("position_margin_used_missing")
                elif position_margin_used < 0:
                    hard_reasons.append("position_margin_used_invalid")
            if (
                account_equity is not None
                and account_equity > 0
                and account_margin_used is not None
                and account_margin_used >= 0
                and account_margin_ratio is not None
                and account_margin_ratio >= 0
                and position_margin_used is not None
                and position_margin_used >= 0
                and target_margin is not None
            ):
                calculated_account_margin_ratio = account_margin_used / account_equity
                if abs(calculated_account_margin_ratio - account_margin_ratio) > 1e-6:
                    hard_reasons.append("account_margin_ratio_mismatch")
                current_ticker_margin_ratio = position_margin_used / account_equity
                incremental_margin_ratio, projected_total_margin_ratio = project_margin_transition(
                    current_account_margin=calculated_account_margin_ratio,
                    current_ticker_margin=current_ticker_margin_ratio,
                    target_ticker_margin=target_margin,
                )
                margin_projection = {
                    "account_margin_ratio_before": calculated_account_margin_ratio,
                    "current_ticker_margin_ratio": current_ticker_margin_ratio,
                    "target_ticker_margin_ratio": target_margin,
                    "incremental_margin_ratio": incremental_margin_ratio,
                    "projected_total_margin_ratio": projected_total_margin_ratio,
                    "hard_max_total_margin_ratio": hard_cap if hard_cap > 0 else None,
                }
                if hard_cap > 0 and projected_total_margin_ratio > hard_cap + 1e-12:
                    hard_reasons.append("margin_hard_cap_exceeded")
            if str(payload.account_state.get("risk_status") or "").upper() == "LIQUIDATION":
                hard_reasons.append("account_liquidation_blocks_new_risk")

        data_quality_status = str(payload.data_quality.get("status") or "")
        if data_quality_status == "hard_fail":
            hard_reasons.append("data_quality_hard_fail")
        elif data_quality_status == "warning":
            soft_reasons.append("data_quality_warning")
        elif data_quality_status != "clean":
            hard_reasons.append("data_quality_status_invalid")

        verdict = "approve"
        status = "approved"
        if hard_reasons:
            verdict = "block"
            status = "blocked"
        elif soft_reasons:
            verdict = "approve_with_warning"
            status = "approved"

        audited_at = datetime.now(timezone.utc).isoformat()
        audit_payload = {
            "contract_version": "agentquant.audit_verdict.v1",
            "producer": "auditor",
            "agent_name": "auditor",
            "recommendation_id": payload.recommendation_id,
            "ticker": str(payload.ticker or contract.get("ticker") or "").upper(),
            "trading_date": _date_text(payload.trading_date),
            "config_id": str(payload.config_id or ""),
            "audit_status": status,
            "audit_verdict": verdict,
            "audit_reason_codes": sorted(set(hard_reasons + soft_reasons)),
            "hard_risk_reasons": sorted(set(hard_reasons)),
            "soft_risk_reasons": sorted(set(soft_reasons)),
            "audited_by": "auditor",
            "audited_at": audited_at,
            "source": {
                "pm_recommendation_id": payload.recommendation_id,
                "final_action_contract_hash_source": "futures_recommendation.signal_snapshot.final_action_contract",
                "contract_state_source": payload.contract_state.get("source"),
                "data_quality_source": payload.data_quality.get("source"),
            },
            "boundary": {
                "auditor_does_not_modify_final_action_contract": True,
                "auditor_does_not_create_trade_authority": True,
                "trader_requires_approved_audit_verdict": True,
                "research_memory_not_consumed": True,
                "auditor_reads_research_db": False,
            },
            "contract_summary": {
                "final_action": contract.get("final_action"),
                "current_lots": contract.get("current_lots"),
                "target_lots": contract.get("target_lots"),
                "lots_delta": contract.get("lots_delta"),
                "contract_code": contract.get("contract_code"),
                "invalidation_present": _has_valid_invalidation_boundary(contract),
                "requires_intraday_confirmation": contract.get("requires_intraday_confirmation"),
                "can_execute_without_intraday_trigger": contract.get("can_execute_without_intraday_trigger"),
                **margin_projection,
            },
            "semantic_state": {
                key: value
                for key, value in classify_final_action_contract(contract).items()
                if key
                in {
                    "lifecycle_state",
                    "requires_intraday_result",
                    "hard_block_reasons",
                    "soft_limit_reasons",
                    "semantic_errors",
                }
            },
        }
        validate_auditor_artifact_boundary(audit_payload)
        return AuditorOutput(
            audit_status=status,
            audit_verdict=verdict,
            audit_payload=audit_payload,
            audit_reason_codes=sorted(set(hard_reasons + soft_reasons)),
            hard_risk_reasons=sorted(set(hard_reasons)),
            soft_risk_reasons=sorted(set(soft_reasons)),
            audited_by="auditor",
            audited_at=audited_at,
        )


def audit_futures_recommendation(
    *,
    recommendation: Dict[str, Any],
    hard_risk_config: Dict[str, Any],
    account_state: Optional[Dict[str, Any]] = None,
    position_state: Optional[Dict[str, Any]] = None,
    contract_state: Optional[Dict[str, Any]] = None,
    data_quality: Optional[Dict[str, Any]] = None,
) -> AuditorOutput:
    auditor = Auditor(hard_risk_config)
    final_action_contract = _final_contract_from_recommendation(recommendation)
    return auditor.audit(
        AuditorInput(
            recommendation_id=str(recommendation.get("id") or ""),
            ticker=str(recommendation.get("underlying_code") or ""),
            trading_date=recommendation.get("effective_trade_date") or recommendation.get("trading_date"),
            config_id=str(recommendation.get("config_id") or ""),
            source_type=str(
                getattr(recommendation.get("source_type"), "value", recommendation.get("source_type"))
                or ""
            ),
            final_action_contract=final_action_contract,
            account_state=account_state or {},
            position_state=position_state or {},
            contract_state=contract_state or {},
            data_quality=data_quality or {},
            hard_risk_config=hard_risk_config or {},
        )
    )


def audit_verdict_allows_trader(value: Any) -> bool:
    if isinstance(value, dict):
        verdict = str(value.get("audit_verdict") or value.get("verdict") or "")
    else:
        verdict = str(value or "")
    return verdict in APPROVED_AUDIT_VERDICTS


__all__ = [
    "APPROVED_AUDIT_VERDICTS",
    "BLOCKING_AUDIT_VERDICTS",
    "Auditor",
    "AuditorInput",
    "AuditorOutput",
    "audit_futures_recommendation",
    "audit_verdict_allows_trader",
]
