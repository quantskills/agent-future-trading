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
)


APPROVED_AUDIT_VERDICTS = {"approve", "approve_with_warning"}
BLOCKING_AUDIT_VERDICTS = {"block", "require_review"}


class AuditorInput(BaseModel):
    """Input payload for the independent Auditor agent."""

    recommendation_id: str = ""
    ticker: str = ""
    trading_date: Any = None
    config_id: str = ""
    recommendation: Dict[str, Any] = Field(default_factory=dict)
    account_state: Dict[str, Any] = Field(default_factory=dict)
    position_state: Dict[str, Any] = Field(default_factory=dict)
    contract_state: Dict[str, Any] = Field(default_factory=dict)
    data_quality: Dict[str, Any] = Field(default_factory=dict)
    full_config: Dict[str, Any] = Field(default_factory=dict)


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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _recommendation_snapshot(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot") if isinstance(recommendation, dict) else {}
    return snapshot if isinstance(snapshot, dict) else {}


def _final_contract_from_recommendation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    return final_action_contract_from_snapshot(_recommendation_snapshot(recommendation))


def _is_new_or_increasing_exposure(contract: Dict[str, Any]) -> bool:
    return contract_increases_risk_position(contract)


def _hard_margin_limit(config: Dict[str, Any]) -> float:
    hard_cap = _safe_float((config or {}).get("max_total_margin_ratio"), 0.20)
    return hard_cap if hard_cap > 0 else 0.20


def _contract_margin_estimate(contract: Dict[str, Any]) -> float:
    for key in (
        "target_margin_ratio_estimate",
        "target_margin_ratio",
        "max_allowed_margin_ratio",
    ):
        value = _safe_float(contract.get(key), -1.0)
        if value >= 0:
            return value
    evidence = contract.get("evidence_used") if isinstance(contract.get("evidence_used"), dict) else {}
    sizing = evidence.get("position_sizing_result")
    if isinstance(sizing, dict):
        value = _safe_float(sizing.get("target_margin_ratio_estimate"), -1.0)
        if value >= 0:
            return value
    return 0.0


class Auditor:
    """Independent Auditor agent.

    It verifies a signed PM final_action_contract and writes audit facts. It is
    intentionally narrow: no research reads, no contract mutation, no order
    generation.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.audit_config = (self.config.get("auditor") or {}) if isinstance(self.config, dict) else {}
        self.enabled = bool(self.audit_config.get("enabled", True))

    def audit(self, payload: AuditorInput) -> AuditorOutput:
        recommendation = payload.recommendation or {}
        contract = _final_contract_from_recommendation(recommendation)
        validation_errors = validate_final_action_contract(contract)
        hard_reasons: List[str] = []
        soft_reasons: List[str] = []

        if not self.enabled:
            hard_reasons.append("auditor_disabled")
        if validation_errors:
            hard_reasons.extend(validation_errors)

        if _enum_value(recommendation.get("source_type")) == "strategy" and not contract:
            hard_reasons.append("missing_pm_final_action_contract")

        if _is_new_or_increasing_exposure(contract):
            if not contract.get("contract_code"):
                hard_reasons.append("missing_contract_code")
            if not any(
                contract.get(field) not in (None, "")
                for field in (
                    "invalidation_condition",
                    "invalidation",
                    "invalidation_level",
                    "atr_stop_distance",
                )
            ):
                hard_reasons.append("missing_invalidation_condition")
            target_margin = _contract_margin_estimate(contract)
            hard_cap = _hard_margin_limit(payload.full_config or self.config)
            if target_margin > hard_cap + 1e-12:
                hard_reasons.append("margin_hard_cap_exceeded")
            if str(payload.account_state.get("risk_status") or "").upper() == "LIQUIDATION":
                hard_reasons.append("account_liquidation_blocks_new_risk")

        if payload.data_quality:
            status = str(payload.data_quality.get("status") or payload.data_quality.get("quality_status") or "")
            if status in {"invalid", "hard_fail", "future_leak"}:
                hard_reasons.append(f"data_quality_{status}")
            elif status in {"warning", "degraded"}:
                soft_reasons.append(f"data_quality_{status}")

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
            "recommendation_id": payload.recommendation_id or recommendation.get("id"),
            "ticker": str(payload.ticker or recommendation.get("underlying_code") or contract.get("ticker") or "").upper(),
            "trading_date": _date_text(payload.trading_date or recommendation.get("effective_trade_date") or recommendation.get("trading_date")),
            "config_id": str(payload.config_id or recommendation.get("config_id") or ""),
            "audit_status": status,
            "audit_verdict": verdict,
            "audit_reason_codes": sorted(set(hard_reasons + soft_reasons)),
            "hard_risk_reasons": sorted(set(hard_reasons)),
            "soft_risk_reasons": sorted(set(soft_reasons)),
            "audited_by": "auditor",
            "audited_at": audited_at,
            "source": {
                "pm_recommendation_id": payload.recommendation_id or recommendation.get("id"),
                "final_action_contract_hash_source": "futures_recommendation.signal_snapshot.final_action_contract",
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
                "requires_intraday_confirmation": contract.get("requires_intraday_confirmation"),
                "can_execute_without_intraday_trigger": contract.get("can_execute_without_intraday_trigger"),
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
    full_config: Dict[str, Any],
    account_state: Optional[Dict[str, Any]] = None,
    position_state: Optional[Dict[str, Any]] = None,
    contract_state: Optional[Dict[str, Any]] = None,
    data_quality: Optional[Dict[str, Any]] = None,
) -> AuditorOutput:
    auditor = Auditor(full_config)
    return auditor.audit(
        AuditorInput(
            recommendation_id=str(recommendation.get("id") or ""),
            ticker=str(recommendation.get("underlying_code") or ""),
            trading_date=recommendation.get("effective_trade_date") or recommendation.get("trading_date"),
            config_id=str(recommendation.get("config_id") or ""),
            recommendation=dict(recommendation or {}),
            account_state=account_state or {},
            position_state=position_state or {},
            contract_state=contract_state or {},
            data_quality=data_quality or {},
            full_config=full_config or {},
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
