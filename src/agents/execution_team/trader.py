"""Trader agent for futures Phase2 execution.

The trader owns the order-execution role: it translates Phase1 futures
recommendations into Phase2 orders, applies intraday execution confirmation,
records execution audit payloads, and writes futures transactions through the
existing execution tools.
"""

import argparse
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dotenv import load_dotenv

from agents.decision_team.portfolio_manager import (
    RiskLevel,
    check_risk_level,
    get_hard_allocation_margin_ratio,
    get_max_single_position_ratio,
)
from apis.contract_info_cache import FuturesContractInfoCache
from apis.router import APISource, Router
from graph.schema import (
    FuturesAction,
    FuturesDecision,
    Portfolio,
    RecommendationSourceType,
    RecommendationStatus,
    TradingPhase,
)
from tools.agent_tools.execution.futures_execution import FuturesExecutionEngine
from tools.agent_tools.execution.intraday_execution import (
    intraday_confirmation_enabled,
    resolve_intraday_execution_basis,
)
from tools.agent_tools.execution.entry_timing import phase2_entry_audit
from tools.agent_tools.execution.execution_simulator import execution_price_basis
from tools.agent_tools.execution.order_semantics import (
    build_lot_intent_consistency,
    phase2_order_intent_from_lots,
)
from tools.agent_tools.decision.position_lifecycle import cap_signed_lots_by_abs_limit
from tools.common.runtime_setup import (
    ensure_seed_settled_portfolio,
    load_portfolio_config,
    resolve_net_exposure_config,
)
from tools.agent_tools.execution.execution_exit_policy import evaluate_exit_policy
from util.config import ConfigParser
from util.db_helper import db_initialize, get_db
from util.futures_audit import (
    add_rewrite_reason,
    append_translated_order,
    build_execution_learning_trace,
    build_audit_payload,
    categorize_no_trade_reason,
    ensure_execution_translation,
    ensure_signal_snapshot,
    extract_signal_lifecycle,
    infer_no_trade_reason,
    normalize_no_trade_reason,
    set_execution_result,
)
from util.logger import logger


load_dotenv()


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _sort_recommendations_by_ticker_order(recommendations: List[Dict[str, Any]], tickers: List[str]) -> List[Dict[str, Any]]:
    ticker_order = {ticker: index for index, ticker in enumerate(tickers)}
    return sorted(
        recommendations,
        key=lambda item: (
            ticker_order.get(item.get("underlying_code"), len(tickers)),
            item.get("created_at") or "",
        ),
    )


def _signed_position_ratio(position, total_portfolio_value: float) -> float:
    if position is None or total_portfolio_value <= 0:
        return 0.0
    if getattr(position, "shares", 0) == 0:
        return 0.0
    sign = 1.0 if position.shares > 0 else -1.0
    return sign * (float(getattr(position, "value", 0.0) or 0.0) / total_portfolio_value)


def _portfolio_account_equity(portfolio: Portfolio) -> float:
    cash_balance = float(getattr(portfolio, "cashflow", 0.0) or 0.0)
    reserved_margin = float(
        getattr(portfolio, "margin_used", None)
        if getattr(portfolio, "margin_used", None) is not None
        else sum(float(getattr(pos, "margin_used", 0.0) or 0.0) for pos in portfolio.positions.values())
    )
    return cash_balance + reserved_margin


def _current_net_exposure(portfolio: Portfolio, total_portfolio_value: float) -> float:
    if total_portfolio_value <= 0:
        return 0.0

    net_exposure = 0.0
    for position in portfolio.positions.values():
        if getattr(position, "shares", 0) == 0:
            continue
        sign = 1.0 if position.shares > 0 else -1.0
        net_exposure += sign * (float(getattr(position, "value", 0.0) or 0.0) / total_portfolio_value)
    return net_exposure


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


def _signal_invalidation_breached(current_price: Optional[float], target_lots: int, lifecycle: Dict[str, Any]) -> bool:
    if current_price is None or target_lots == 0:
        return False
    invalidation_level = lifecycle.get("invalidation_level")
    if invalidation_level is None:
        return False
    try:
        price = float(current_price)
        level = float(invalidation_level)
    except Exception:
        return False
    if target_lots > 0:
        return price <= level
    if target_lots < 0:
        return price >= level
    return False


def _signal_side(signal_value: Any) -> str:
    value = str(_enum_value(signal_value) or "").strip().lower()
    if value in {"bullish", "long", "buy"}:
        return "long"
    if value in {"bearish", "short", "sell"}:
        return "short"
    return "flat"


def _safe_lifecycle_float(value: Any) -> Optional[float]:
    if value in (None, "", "unknown", "UNKNOWN"):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _align_signal_lifecycle_to_target(
    snapshot: Dict[str, Any],
    target_lots: int,
    lifecycle: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep execution invalidation aligned with the final target direction.

    Analysts can disagree. A bullish fundamental invalidation boundary is not a
    valid stop for a short order, and vice versa. Without this guard Phase2 can
    incorrectly invalidate a trade before intraday confirmation has a chance to
    evaluate it.
    """
    if target_lots == 0 or not isinstance(snapshot, dict):
        return lifecycle

    target_side = "long" if target_lots > 0 else "short"
    selected = None
    ignored = []
    for analyst in ("technical", "fundamental", "commodity_news"):
        item = snapshot.get(analyst)
        if not isinstance(item, dict):
            continue
        level = _safe_lifecycle_float(item.get("invalidation_level"))
        if level is None:
            continue
        side = _signal_side(item.get("signal"))
        payload = {"analyst": analyst, "side": side, "invalidation_level": level}
        if side == target_side and selected is None:
            selected = payload
        elif side in {"long", "short"} and side != target_side:
            ignored.append(payload)

    aligned = dict(lifecycle or {})
    original_level = aligned.get("invalidation_level")
    if selected is not None:
        aligned["invalidation_level"] = selected["invalidation_level"]
    elif ignored and original_level is not None:
        aligned.pop("invalidation_level", None)

    if ignored or selected is not None:
        ensure_execution_translation(snapshot)["signal_lifecycle_direction_filter"] = {
            "target_side": target_side,
            "selected": selected,
            "ignored_opposing": ignored,
            "original_invalidation_level": original_level,
            "effective_invalidation_level": aligned.get("invalidation_level"),
        }
    return aligned


def _target_return_price(current_price: Optional[float], target_lots: int, lifecycle: Dict[str, Any]) -> Optional[float]:
    if current_price is None or target_lots == 0 or lifecycle.get("target_return") is None:
        return None
    try:
        price = float(current_price)
        target_return = abs(float(lifecycle["target_return"]))
    except Exception:
        return None
    if target_lots > 0:
        return round(price * (1.0 + target_return), 6)
    if target_lots < 0:
        return round(price * (1.0 - target_return), 6)
    return None


def _build_executable_recommendation(
    recommendation: Dict[str, Any],
    decision: FuturesDecision,
    morning_price_context,
) -> Dict[str, Any]:
    executable = dict(recommendation)
    executable["action"] = _enum_value(decision.action)
    executable["lots"] = int(decision.lots)
    executable["contract_code"] = decision.contract_code or recommendation.get("contract_code")
    executable["base_price"] = morning_price_context.base_price
    executable["base_price_source"] = morning_price_context.base_price_source
    executable["base_price_date"] = morning_price_context.base_price_date
    executable["open_price"] = morning_price_context.open_price
    executable["prev_close_price"] = morning_price_context.prev_close_price
    executable["warning_message"] = morning_price_context.warning_message
    executable["justification"] = decision.justification
    executable["status"] = RecommendationStatus.PENDING.value
    return executable


def _record_execution_translation_context(
    snapshot: Dict[str, Any],
    recommendation: Dict[str, Any],
    morning_price_context,
) -> None:
    translation = ensure_execution_translation(snapshot)
    translation["reference_action"] = _enum_value(recommendation.get("action"))
    translation["reference_lots"] = int(recommendation.get("lots", 0) or 0)
    translation["base_price"] = morning_price_context.base_price
    translation["base_price_source"] = _enum_value(morning_price_context.base_price_source)
    translation["base_price_date"] = morning_price_context.base_price_date
    translation["open_price"] = morning_price_context.open_price
    translation["prev_close_price"] = morning_price_context.prev_close_price
    translation["warning_message"] = morning_price_context.warning_message
    lifecycle = extract_signal_lifecycle(snapshot)
    if lifecycle:
        translation["signal_lifecycle"] = lifecycle
    execution_contract = _execution_contract_from_snapshot(snapshot)
    if execution_contract:
        translation["execution_contract"] = execution_contract
    if getattr(morning_price_context, "intraday_audit", None):
        translation["intraday_execution"] = morning_price_context.intraday_audit


def _execution_contract_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    final_contract = _final_action_contract_from_snapshot(snapshot)
    if not isinstance(final_contract, dict) or not final_contract:
        return {}
    plan_keys = {
        "execution_profile",
        "trigger_source",
        "entry_trigger",
        "invalidation",
        "valid_until",
        "requires_intraday_confirmation",
        "can_execute_without_intraday_trigger",
        "authority_type",
        "max_allowed_margin_ratio",
        "reason_codes",
        "execution_action_value_preference",
        "analyst_execution_roles",
    }
    return {key: final_contract.get(key) for key in plan_keys if key in final_contract}


def _recommendation_source_type(recommendation: Dict[str, Any]) -> str:
    return str(_enum_value(recommendation.get("source_type", RecommendationSourceType.STRATEGY.value)))


def _is_strategy_recommendation(recommendation: Dict[str, Any]) -> bool:
    return _recommendation_source_type(recommendation) == RecommendationSourceType.STRATEGY.value


def _raw_action_lots_allowed_source(recommendation: Dict[str, Any]) -> bool:
    return _recommendation_source_type(recommendation) in {
        RecommendationSourceType.ROLLOVER.value,
        RecommendationSourceType.FORCED_RISK.value,
    }


def _final_entry_authority_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    contract = _final_action_contract_from_snapshot(snapshot)
    if not isinstance(contract, dict) or not contract:
        return {}
    reason_codes = contract.get("reason_codes") if isinstance(contract.get("reason_codes"), list) else []
    return {
        "authority_type": contract.get("authority_type") or "not_applicable",
        "authority_decision": contract.get("authority_decision") or "not_applicable",
        "max_allowed_margin_ratio": contract.get("max_allowed_margin_ratio"),
        "reason_codes": list(reason_codes),
        "open_action_evidence": bool(contract.get("open_action_evidence")),
        "strong_current_evidence": bool(contract.get("strong_current_evidence")),
        "watch_for_trigger_block": bool(contract.get("watch_for_trigger_block")),
        "conditional_trigger_authority": bool(contract.get("conditional_trigger_authority")),
        "requires_intraday_confirmation": bool(contract.get("requires_intraday_confirmation")),
        "can_execute_without_intraday_trigger": bool(contract.get("can_execute_without_intraday_trigger")),
        "negative_profile": bool(contract.get("negative_profile")),
        "tradeable_state": bool(contract.get("tradeable_state")),
        "weak_conflict_probe": bool(contract.get("weak_conflict_probe")),
    }


def _authority_signature(authority: Dict[str, Any]) -> Tuple[Any, ...]:
    authority = authority if isinstance(authority, dict) else {}
    max_allowed = authority.get("max_allowed_margin_ratio")
    try:
        max_allowed = round(float(max_allowed), 10)
    except (TypeError, ValueError):
        max_allowed = None
    return (
        authority.get("authority_type"),
        authority.get("authority_decision"),
        bool(authority.get("open_action_evidence")),
        bool(authority.get("strong_current_evidence")),
        max_allowed,
    )


def _final_entry_authority_consistency(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Validate final contract authority fields before Trader opens exposure."""
    if not isinstance(snapshot, dict):
        return {"passed": False, "reason": "missing_signal_snapshot", "selected_authority": {}}

    contract = _final_action_contract_from_snapshot(snapshot)
    if not isinstance(contract, dict) or not contract:
        return {"passed": False, "reason": "missing_final_action_contract", "selected_authority": {}}
    sources: List[Dict[str, Any]] = [{"source": "final_action_contract", "authority": _final_entry_authority_from_snapshot(snapshot)}]

    signatures = {_authority_signature(item["authority"]) for item in sources}
    selected = dict(sources[0]["authority"])
    audit_sources = [
        {
            "source": item["source"],
            "authority_type": item["authority"].get("authority_type"),
            "authority_decision": item["authority"].get("authority_decision"),
            "open_action_evidence": bool(item["authority"].get("open_action_evidence")),
            "strong_current_evidence": bool(item["authority"].get("strong_current_evidence")),
            "max_allowed_margin_ratio": item["authority"].get("max_allowed_margin_ratio"),
        }
        for item in sources
    ]
    if len(signatures) > 1:
        return {
            "passed": False,
            "reason": "final_contract_authority_mismatch",
            "selected_authority": selected,
            "sources": audit_sources,
            "business_boundary": "Trader must not choose among conflicting PM authority mirrors",
        }
    return {
        "passed": True,
        "reason": "final_contract_authority_consistent",
        "selected_authority": selected,
        "sources": audit_sources,
    }


def _requires_entry_authority(current_lots: int, target_lots: int) -> bool:
    current_lots = int(current_lots or 0)
    target_lots = int(target_lots or 0)
    if target_lots == 0 or target_lots == current_lots:
        return False
    if current_lots == 0:
        return True
    if (current_lots > 0) != (target_lots > 0):
        return True
    return abs(target_lots) > abs(current_lots)


def _authority_allows_entry(authority: Dict[str, Any]) -> bool:
    authority_type = str((authority or {}).get("authority_type") or "")
    if authority_type == "real_budget_entry":
        return bool(authority.get("open_action_evidence") and authority.get("strong_current_evidence"))
    if authority_type == "exploration_probe":
        if bool(authority.get("watch_for_trigger_block")):
            return False
        reason_codes = {str(item or "") for item in (authority.get("reason_codes") or [])}
        hard_watchlist_codes = {
            "pm_text_no_trade_blocks_new_entry",
            "pm_text_no_entry_trigger_blocks_new_entry",
            "pm_text_watchlist_only_blocks_new_entry",
            "watch_for_trigger_cannot_open_position",
            "daily_tradeability_watchlist_only",
            "real_probe_qualification_not_met",
        }
        if reason_codes & hard_watchlist_codes:
            return False
        conditional_trigger_authority = bool(
            authority.get("conditional_trigger_authority")
            and authority.get("requires_intraday_confirmation")
            and not authority.get("can_execute_without_intraday_trigger")
        )
        if conditional_trigger_authority:
            return True
        return bool(
            authority.get("open_action_evidence")
            or authority.get("strong_current_evidence")
            or authority.get("technical_confirmation")
            or authority.get("event_catalyst_confirmation")
            or authority.get("executable_setup_confirmation")
            or authority.get("market_confirmation")
        )
    return False


def _target_lots_without_new_entry(current_lots: int, target_lots: int) -> int:
    current_lots = int(current_lots or 0)
    target_lots = int(target_lots or 0)
    if not _requires_entry_authority(current_lots, target_lots):
        return target_lots
    if current_lots == 0:
        return 0
    if (current_lots > 0) != (target_lots > 0):
        return 0
    return current_lots


def _ensure_phase2_execution(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    audit = snapshot.get("phase2_execution")
    if not isinstance(audit, dict):
        audit = {}
    snapshot["phase2_execution"] = audit
    return audit


def _setup_execution_learning_context(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    execution_contract = _execution_contract_from_snapshot(snapshot)
    final_contract = _final_action_contract_from_snapshot(snapshot)
    target_lots = _safe_int(final_contract.get("target_lots"), 0)
    preferred_side = "long" if target_lots > 0 else "short" if target_lots < 0 else "flat"
    analyst_roles = (
        execution_contract.get("analyst_execution_roles")
        if isinstance(execution_contract.get("analyst_execution_roles"), dict)
        else {}
    )
    action_contracts = {
        key: value.get("action_evidence_contract")
        for key, value in analyst_roles.items()
        if isinstance(value, dict) and isinstance(value.get("action_evidence_contract"), dict)
    }
    learning_scopes = {
        key: value.get("learning_scope")
        for key, value in analyst_roles.items()
        if isinstance(value, dict) and isinstance(value.get("learning_scope"), dict)
    }
    return {
        "consumer_scope": "trader_execution_learning",
        "learning_lane": "execution",
        "setup_type": (
            final_contract.get("setup_type")
            or execution_contract.get("setup_type")
            or execution_contract.get("execution_profile")
            or "unknown"
        ),
        "opportunity_state": final_contract.get("opportunity_state") or "unknown",
        "preferred_side": preferred_side or "flat",
        "execution_contract": execution_contract,
        "final_action_contract": final_contract,
        "analyst_action_evidence_contracts": action_contracts,
        "analyst_learning_scopes": learning_scopes,
        "execution_contract_summary": {
            "profile": execution_contract.get("execution_profile"),
            "trigger_source": execution_contract.get("trigger_source"),
            "entry_trigger": execution_contract.get("entry_trigger"),
            "invalidation": execution_contract.get("invalidation"),
            "requires_intraday_confirmation": execution_contract.get("requires_intraday_confirmation"),
            "can_execute_without_intraday_trigger": execution_contract.get("can_execute_without_intraday_trigger"),
            "authority_type": execution_contract.get("authority_type"),
        },
        "learning_boundary": {
            "consumer_scope": "trader_execution_learning",
            "trader_executes_only": True,
            "execution_feedback_future_only": True,
            "not_strategy_creation": True,
            "learning_source": "final_action_contract",
        },
    }


def _attach_setup_execution_learning(
    snapshot: Dict[str, Any],
    *,
    status: str,
    reason: Optional[str],
    selection=None,
) -> None:
    audit = _ensure_phase2_execution(snapshot)
    context = _setup_execution_learning_context(snapshot)
    selection_payload = (
        selection.to_audit_payload()
        if selection is not None and hasattr(selection, "to_audit_payload")
        else {}
    )
    context.update(
        {
            "phase2_status": status,
            "no_trade_reason": reason,
            "intraday_selection": selection_payload,
            "reason_family": (
                "execution_timing"
                if reason and "intraday" in str(reason)
                else "business_execution"
                if reason
                else "executed_or_hold"
            ),
        }
    )
    audit["setup_execution_learning"] = context


def _format_datetime(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else None


def _record_phase2_state(
    snapshot: Dict[str, Any],
    *,
    mode: str,
    status: str,
    recommendation: Dict[str, Any],
    decision: Optional[FuturesDecision] = None,
    selection=None,
    current_lots_before: Optional[int] = None,
    cutoff_datetime: Optional[datetime] = None,
    finalize_untriggered: Optional[bool] = None,
    loop_iteration: Optional[int] = None,
    reason: Optional[str] = None,
) -> None:
    audit = _ensure_phase2_execution(snapshot)
    audit.update(
        {
            "mode": mode,
            "status": status,
            "ticker": recommendation.get("underlying_code"),
            "recommendation_id": recommendation.get("id"),
            "reference_action": _enum_value(recommendation.get("action")),
            "reference_lots": int(recommendation.get("lots", 0) or 0),
            "last_checked_at": datetime.now().isoformat(timespec="seconds"),
            "cutoff_datetime": _format_datetime(cutoff_datetime),
            "finalize_untriggered": finalize_untriggered,
            "loop_iteration": loop_iteration,
            "reason": reason,
        }
    )
    execution_contract = _execution_contract_from_snapshot(snapshot)
    if execution_contract:
        audit["execution_contract"] = execution_contract
    if current_lots_before is not None:
        audit["current_lots_before"] = int(current_lots_before)
    if decision is not None:
        audit["translated_decision"] = {
            "action": _enum_value(decision.action),
            "lots": int(decision.lots or 0),
            "contract_code": decision.contract_code,
            "price": decision.price,
        }
    if selection is not None:
        audit["intraday_selection"] = selection.to_audit_payload()


def _record_phase2_order_plan(
    snapshot: Dict[str, Any],
    *,
    current_lots: int,
    target_lots: int,
    account_equity: float,
    current_price: Optional[float],
    risk_level: Any,
    cashflow_ratio: float,
    current_margin_ratio: float,
    max_total_margin_ratio: float,
    max_single_margin_ratio: float,
    remaining_margin: float,
    decision: FuturesDecision,
    signal_lifecycle: Optional[Dict[str, Any]] = None,
) -> None:
    translation = ensure_execution_translation(snapshot)
    lifecycle = dict(signal_lifecycle or extract_signal_lifecycle(snapshot) or {})
    if lifecycle and target_lots != 0:
        target_price = _target_return_price(current_price, target_lots, lifecycle)
        if target_price is not None:
            lifecycle = dict(lifecycle)
            lifecycle["target_price"] = target_price
    consistency_diagnostics = build_lot_intent_consistency(
        current_lots=current_lots,
        target_lots=target_lots,
        action=decision.action,
        lots=decision.lots,
        mode="phase2_execution",
    )
    translation["phase2_order_plan"] = {
        "current_lots": int(current_lots or 0),
        "target_lots": int(target_lots or 0),
        "action": _enum_value(decision.action),
        "lots": int(decision.lots or 0),
        "contract_code": decision.contract_code,
        "price": decision.price,
        "account_equity": round(float(account_equity or 0.0), 2),
        "current_price": current_price,
        "risk_level": _enum_value(risk_level),
        "cashflow_ratio": round(float(cashflow_ratio or 0.0), 6),
        "current_margin_ratio": round(float(current_margin_ratio or 0.0), 6),
        "max_total_margin_ratio": round(float(max_total_margin_ratio or 0.0), 6),
        "max_single_margin_ratio": round(float(max_single_margin_ratio or 0.0), 6),
        "remaining_margin": round(float(remaining_margin or 0.0), 2),
        "signal_lifecycle": lifecycle,
        "execution_contract": _execution_contract_from_snapshot(snapshot),
        "consistency_diagnostics": consistency_diagnostics,
    }


def _record_intraday_decision(
    db,
    *,
    config_id: str,
    recommendation: Dict[str, Any],
    trading_date_value: str,
    mode: str,
    cutoff_datetime: Optional[datetime],
    selection,
) -> None:
    if not hasattr(db, "save_futures_intraday_decision") or selection is None:
        return
    audit_payload = selection.to_audit_payload()
    db.save_futures_intraday_decision(
        {
            "config_id": config_id,
            "trading_date": trading_date_value,
            "recommendation_id": recommendation.get("id"),
            "ticker": recommendation.get("underlying_code"),
            "contract_code": recommendation.get("contract_code"),
            "slot_datetime": selection.base_datetime or selection.signal_datetime or trading_date_value,
            "mode": mode,
            "cutoff_datetime": cutoff_datetime.isoformat() if cutoff_datetime else None,
            "decision": selection.decision,
            "trigger_reason": selection.reason,
            "base_price": selection.base_price,
            "execution_price_candidate": selection.base_price,
            "features": audit_payload,
        }
    )


def _resolve_phase2_execution_basis(
    *,
    router,
    cfg: Dict[str, Any],
    recommendation: Dict[str, Any],
    decision: FuturesDecision,
    morning_price_context,
    cutoff_datetime: Optional[datetime],
    finalize_untriggered: bool,
) -> Tuple[Any, Optional[Any]]:
    if not intraday_confirmation_enabled(cfg):
        return morning_price_context, None
    action_value = _enum_value(decision.action)
    if action_value == FuturesAction.HOLD.value or int(decision.lots or 0) <= 0:
        return morning_price_context, None
    decision_context = _decision_context_from_recommendation(recommendation, cfg)
    execution_contract = (
        decision_context.get("execution_contract")
        if isinstance(decision_context.get("execution_contract"), dict)
        else {}
    )
    execution_profile = str(execution_contract.get("execution_profile") or decision_context.get("execution_profile") or "")
    force_immediate = bool(
        action_value in {FuturesAction.CLOSE_LONG.value, FuturesAction.CLOSE_SHORT.value}
        or execution_profile == "exit_immediate"
        or (
            execution_profile == "event_immediate"
            and execution_contract.get("can_execute_without_intraday_trigger")
        )
    )
    basis, selection = resolve_intraday_execution_basis(
        router=router,
        config=cfg,
        underlying_code=recommendation["underlying_code"],
        trading_date=cfg["trading_date"],
        action=decision.action,
        contract_code=decision.contract_code or recommendation.get("contract_code"),
        decision_context=decision_context,
        cutoff_datetime=cutoff_datetime,
        finalize_untriggered=finalize_untriggered,
        force_immediate=force_immediate,
    )
    if basis.base_price is None and force_immediate:
        logger.warning(
            f"Intraday basis unavailable for immediate {recommendation['underlying_code']} close; "
            "falling back to morning execution basis."
        )
        return morning_price_context, None
    return basis, selection


def _decision_context_from_recommendation(recommendation: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot") or {}
    execution_contract = _execution_contract_from_snapshot(snapshot if isinstance(snapshot, dict) else {})
    final_contract = _final_action_contract_from_snapshot(snapshot if isinstance(snapshot, dict) else {})
    return {
        "ticker": recommendation.get("underlying_code"),
        "underlying_code": recommendation.get("underlying_code"),
        "horizon_class": final_contract.get("horizon_class") or execution_contract.get("horizon_class"),
        "decision_horizon": final_contract.get("decision_horizon") or execution_contract.get("decision_horizon"),
        "market_regime": final_contract.get("market_regime") or execution_contract.get("market_regime"),
        "execution_contract": execution_contract,
        "execution_profile": execution_contract.get("execution_profile"),
        "entry_trigger": execution_contract.get("entry_trigger"),
        "invalidation": execution_contract.get("invalidation"),
        "trigger_source": execution_contract.get("trigger_source"),
    }


def _mark_intraday_non_execution(
    *,
    db,
    recommendation: Dict[str, Any],
    audit_snapshot: Dict[str, Any],
    selection,
    finalize_untriggered: bool,
    runtime_mode: str,
    cutoff_datetime: Optional[datetime],
    loop_iteration: Optional[int],
) -> None:
    if selection is not None:
        translation = ensure_execution_translation(audit_snapshot)
        translation["intraday_execution"] = selection.to_audit_payload()
        add_rewrite_reason(audit_snapshot, selection.reason)

    if not finalize_untriggered:
        _record_phase2_state(
            audit_snapshot,
            mode=runtime_mode,
            status="waiting_intraday_trigger",
            recommendation=recommendation,
            selection=selection,
            cutoff_datetime=cutoff_datetime,
            finalize_untriggered=finalize_untriggered,
            loop_iteration=loop_iteration,
            reason=selection.reason if selection is not None else "intraday_waiting_for_trigger",
        )
        db.update_futures_recommendation_status(
            recommendation["id"],
            RecommendationStatus.PENDING,
            warning_message=selection.reason if selection is not None else "intraday_waiting_for_trigger",
            signal_snapshot=audit_snapshot,
            audit_payload=build_audit_payload(audit_snapshot),
        )
        return

    no_trade_reason = selection.reason if selection is not None else "intraday_trigger_not_met"
    reason_category = categorize_no_trade_reason(no_trade_reason)
    _record_phase2_state(
        audit_snapshot,
        mode=runtime_mode,
        status="skipped_intraday_trigger_not_met",
        recommendation=recommendation,
        selection=selection,
        cutoff_datetime=cutoff_datetime,
        finalize_untriggered=finalize_untriggered,
        loop_iteration=loop_iteration,
        reason=no_trade_reason,
    )
    set_execution_result(
        audit_snapshot,
        outcome="skipped",
        status=RecommendationStatus.SKIPPED.value,
        transaction_count=0,
        no_trade_reason=no_trade_reason,
        warning_message=no_trade_reason,
    )
    audit_snapshot["execution_result"]["execution_learning_trace"] = build_execution_learning_trace(
        audit_snapshot,
        outcome="skipped",
        status=RecommendationStatus.SKIPPED.value,
        no_trade_reason=no_trade_reason,
        no_trade_reason_category=reason_category,
        transaction_count=0,
        execution_learning_type="intraday_timing_gate",
        turn_into_memory=True,
        extra={
            "timing_strategy_question": (
                "Track whether this skipped setup becomes missed alpha after settlement; "
                "if repeated in the same scope, Researcher may propose timing adjustments "
                "such as pullback, VWAP confirmation, or opening-range calibration."
            ),
        },
    )
    _attach_setup_execution_learning(
        audit_snapshot,
        status="skipped_intraday_trigger_not_met",
        reason=no_trade_reason,
        selection=selection,
    )
    db.update_futures_recommendation_status(
        recommendation["id"],
        RecommendationStatus.SKIPPED,
        warning_message=no_trade_reason,
        signal_snapshot=audit_snapshot,
        audit_payload=build_audit_payload(audit_snapshot),
    )


def _loop_should_finalize(cfg: Dict[str, Any], trading_date_value: str) -> bool:
    intraday_config = ((cfg.get("execution", {}) or {}).get("intraday_confirmation") or {})
    finalize_time = str(intraday_config.get("finalize_after", "15:00:00"))
    now = datetime.now()
    try:
        finalize_at = datetime.strptime(f"{trading_date_value} {finalize_time}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        finalize_at = datetime.strptime(f"{trading_date_value} 15:00:00", "%Y-%m-%d %H:%M:%S")
    return now >= finalize_at


def _new_phase2_summary() -> Dict[str, Any]:
    return {
        "checked": 0,
        "executed": 0,
        "waiting": 0,
        "skipped": 0,
        "holds": 0,
        "actions": Counter(),
        "intraday": Counter(),
        "no_trade_reasons": Counter(),
    }


def _merge_phase2_summary(total: Dict[str, Any], item: Dict[str, Any]) -> None:
    for key in ("checked", "executed", "waiting", "skipped", "holds"):
        total[key] += int(item.get(key, 0) or 0)
    for key in ("actions", "intraday", "no_trade_reasons"):
        total[key].update(item.get(key, Counter()))


def _counter_text(counter: Counter) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in counter.most_common())


def _log_phase2_summary(prefix: str, summary: Dict[str, Any]) -> None:
    logger.info(
        f"{prefix}: checked={summary['checked']}, executed={summary['executed']}, "
        f"waiting={summary['waiting']}, skipped={summary['skipped']}, holds={summary['holds']}, "
        f"actions=[{_counter_text(summary['actions'])}], "
        f"intraday=[{_counter_text(summary['intraday'])}], "
        f"no_trade=[{_counter_text(summary['no_trade_reasons'])}]"
    )


def _execute_pending_forced_risk_before_strategy(
    *,
    execution_engine: FuturesExecutionEngine,
    config_id: str,
    trading_date,
    portfolio: Portfolio,
    cutoff_datetime: Optional[datetime] = None,
) -> Portfolio:
    execution_engine.scan_and_create_intraday_forced_risk_orders(
        config_id=config_id,
        trading_date=trading_date,
        portfolio=portfolio,
        cutoff_datetime=cutoff_datetime,
    )
    return execution_engine.execute_pending_forced_risk_orders(
        config_id=config_id,
        trading_date=trading_date,
        portfolio=portfolio,
        execution_phase=TradingPhase.PHASE2,
    )


def _extract_target_lots_from_recommendation(recommendation: Dict[str, Any]) -> int:
    signal_snapshot = recommendation.get("signal_snapshot") or {}
    if isinstance(signal_snapshot, dict):
        final_contract = _final_action_contract_from_snapshot(signal_snapshot)
        if final_contract.get("target_lots") is not None:
            return int(final_contract.get("target_lots") or 0)

    action_value = _enum_value(recommendation.get("action"))
    direct_lots = int(recommendation.get("lots", 0) or 0)
    if action_value == FuturesAction.OPEN_LONG.value:
        return direct_lots
    if action_value == FuturesAction.OPEN_SHORT.value:
        return -direct_lots
    return 0


def _reconcile_rollover_with_strategy_target(
    rollover_recommendation: Dict[str, Any],
    strategy_recommendation: Dict[str, Any] | None,
    current_lots: int,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    adjusted = dict(rollover_recommendation)
    rollover_config = (config or {}).get("rollover", {}) or {}
    rollover_mode = rollover_config.get("mode", "reconcile_with_strategy")
    if rollover_mode not in {"reconcile_with_strategy", "preserve_exposure"}:
        logger.warning(
            f"Unknown rollover.mode={rollover_mode!r}; falling back to reconcile_with_strategy"
        )
        rollover_mode = "reconcile_with_strategy"

    close_lots = abs(int(current_lots or 0))
    open_lots = close_lots
    policy_reason = "preserve_existing_exposure"

    if current_lots == 0:
        open_lots = 0
        policy_reason = "no_current_position"
    elif rollover_mode == "reconcile_with_strategy" and strategy_recommendation is not None:
        target_lots = _extract_target_lots_from_recommendation(strategy_recommendation)
        same_direction = target_lots != 0 and ((target_lots > 0) == (current_lots > 0))
        open_lots = abs(target_lots) if same_direction else 0
        adjusted["rollover_strategy_target_lots"] = int(target_lots)
        policy_reason = (
            "strategy_target_same_direction"
            if same_direction
            else "strategy_target_flat_or_opposite"
        )
        if open_lots != close_lots:
            logger.info(
                f"Adjusted rollover for {rollover_recommendation['underlying_code']}: "
                f"current_lots={current_lots}, target_lots={target_lots}, "
                f"close_lots={close_lots}, open_lots={open_lots}, "
                f"mode={rollover_mode}, reason={policy_reason}"
            )
    elif rollover_mode == "reconcile_with_strategy":
        policy_reason = "no_strategy_target_preserve_exposure"

    if close_lots > 0 and open_lots > 0:
        execution_type = "full_rollover"
    elif close_lots > 0:
        execution_type = "close_only_rollover"
    else:
        execution_type = "skipped_rollover"

    adjusted["rollover_mode"] = rollover_mode
    adjusted["rollover_policy_reason"] = policy_reason
    adjusted["rollover_execution_type"] = execution_type
    adjusted["rollover_close_lots"] = close_lots
    adjusted["rollover_open_lots"] = open_lots
    return adjusted


def _needs_two_step_reversal(current_lots: int, decision: FuturesDecision) -> bool:
    if current_lots > 0 and decision.action == FuturesAction.CLOSE_LONG and int(decision.lots or 0) > current_lots:
        return True
    if current_lots < 0 and decision.action == FuturesAction.CLOSE_SHORT and int(decision.lots or 0) > abs(current_lots):
        return True
    return False


def _cap_target_lots_by_abs_limit(target_lots: int, abs_limit: int) -> int:
    """Clamp signed target lots without changing direction."""
    return cap_signed_lots_by_abs_limit(target_lots, abs_limit)


def _final_action_contract_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    contract = snapshot.get("final_action_contract")
    if isinstance(contract, dict) and contract:
        return contract
    return {}


def _contract_type_allows_strategy_translation(contract: Dict[str, Any]) -> bool:
    contract_type = str(contract.get("contract_type") or "strategy").strip().lower()
    return contract_type in {"", "strategy"}


def _decision_from_contract_target(
    *,
    ticker: str,
    current_lots: int,
    target_lots: int,
    current_price: float,
    multiplier: float,
    contract_info: Dict[str, Any],
    contract_code: Optional[str],
    justification_prefix: str,
) -> FuturesDecision:
    intent = phase2_order_intent_from_lots(current_lots=current_lots, target_lots=target_lots)
    action = FuturesAction(intent["action"])
    margin_rate = float(
        contract_info["margin_rate_long"]
        if action in {FuturesAction.OPEN_LONG, FuturesAction.CLOSE_LONG, FuturesAction.HOLD}
        else contract_info["margin_rate_short"]
    )
    return FuturesDecision(
        ticker=ticker,
        action=action,
        lots=int(intent["lots"] or 0),
        price=current_price,
        settle_price=current_price,
        margin_rate=margin_rate,
        contract_multiplier=multiplier,
        contract_code=contract_code,
        justification=(
            f"{justification_prefix}: target_lots={target_lots}, current_lots={current_lots}, "
            f"lots_delta={intent['lots_delta']}."
        ),
    )


def _translate_pre_open_recommendation_to_order(
    recommendation: Dict[str, Any],
    portfolio: Portfolio,
    config: Dict[str, Any],
    morning_price_context,
    snapshot: Dict[str, Any],
) -> FuturesDecision:
    ticker = recommendation["underlying_code"]
    signal_snapshot = recommendation.get("signal_snapshot") or {}
    if isinstance(signal_snapshot, dict):
        final_contract = signal_snapshot.get("final_action_contract")
        if isinstance(final_contract, dict) and not isinstance(snapshot.get("final_action_contract"), dict):
            snapshot["final_action_contract"] = dict(final_contract)
        market_confirmation = signal_snapshot.get("market_confirmation")
        if isinstance(market_confirmation, dict) and not isinstance(snapshot.get("market_confirmation"), dict):
            snapshot["market_confirmation"] = dict(market_confirmation)
    signal_lifecycle = extract_signal_lifecycle(snapshot)
    if not signal_lifecycle and isinstance(signal_snapshot, dict):
        signal_lifecycle = extract_signal_lifecycle(signal_snapshot)

    contract_info = FuturesContractInfoCache.get_contract_info(ticker)
    if not contract_info:
        add_rewrite_reason(snapshot, "data_error")
        return FuturesDecision(
            ticker=ticker,
            action=FuturesAction.HOLD,
            lots=0,
            price=0,
            justification=f"Missing contract info for {ticker}; converted to HOLD in open-order phase.",
        )

    current_price = morning_price_context.base_price
    if current_price is None:
        add_rewrite_reason(snapshot, "missing_execution_basis")
        return FuturesDecision(
            ticker=ticker,
            action=FuturesAction.HOLD,
            lots=0,
            price=0,
            justification=f"{ticker} missing open-order execution basis; converted to HOLD.",
        )

    multiplier = float(contract_info["contract_multiplier"])
    account_equity = _portfolio_account_equity(portfolio)
    if account_equity <= 0:
        account_equity = float(portfolio.cashflow or 0.0)
    current_margin_used = sum(float(getattr(pos, "margin_used", 0.0) or 0.0) for pos in portfolio.positions.values())
    current_margin_ratio = current_margin_used / account_equity if account_equity > 0 else 0.0
    max_total_margin_ratio = get_hard_allocation_margin_ratio(config)
    max_allowed_margin = account_equity * max_total_margin_ratio
    remaining_margin = max_allowed_margin - current_margin_used

    risk_level, cashflow_ratio = check_risk_level(portfolio, config)
    max_single_margin_ratio = get_max_single_position_ratio(risk_level, config)
    single_cap_mode = "base"
    max_single_margin = account_equity * max_single_margin_ratio
    force_reduce_only = current_margin_ratio >= max_total_margin_ratio

    current_position = portfolio.positions.get(ticker)
    current_lots = int(getattr(current_position, "shares", 0) or 0)
    contract_code = getattr(current_position, "contract_code", None) or recommendation.get("contract_code")
    final_action_contract = _final_action_contract_from_snapshot(snapshot)

    if (
        _is_strategy_recommendation(recommendation)
        and final_action_contract
        and not _contract_type_allows_strategy_translation(final_action_contract)
    ):
        add_rewrite_reason(snapshot, "unsupported_final_action_contract_type")
        _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
            "passed": False,
            "reason": "unsupported_final_action_contract_type",
            "required_for": "strategy_recommendation",
            "source_type": _recommendation_source_type(recommendation),
            "contract_type": final_action_contract.get("contract_type"),
            "current_lots": int(current_lots),
            "target_lots_after_validation": int(current_lots),
            "business_boundary": (
                "strategy_recommendation_requires_strategy_final_action_contract; "
                "Trader must not fall back to raw action/lots"
            ),
        }
        decision = FuturesDecision(
            ticker=ticker,
            action=FuturesAction.HOLD,
            lots=0,
            price=current_price,
            settle_price=current_price,
            margin_rate=float(contract_info["margin_rate_long"]),
            contract_multiplier=multiplier,
            contract_code=contract_code,
            justification=(
                f"{ticker} strategy recommendation has unsupported final_action_contract "
                "type; converted to HOLD so Trader cannot translate raw action/lots."
            ),
        )
        _record_phase2_order_plan(
            snapshot,
            current_lots=current_lots,
            target_lots=current_lots,
            account_equity=account_equity,
            current_price=current_price,
            risk_level=risk_level,
            cashflow_ratio=cashflow_ratio,
            current_margin_ratio=current_margin_ratio,
            max_total_margin_ratio=max_total_margin_ratio,
            max_single_margin_ratio=max_single_margin_ratio,
            remaining_margin=remaining_margin,
            decision=decision,
            signal_lifecycle=signal_lifecycle,
        )
        return decision

    if (
        _is_strategy_recommendation(recommendation)
        and final_action_contract
        and _contract_type_allows_strategy_translation(final_action_contract)
    ):
        contract_current_lots = _safe_int(final_action_contract.get("current_lots"), current_lots)
        if contract_current_lots != current_lots:
            add_rewrite_reason(snapshot, "final_action_contract_current_lots_mismatch")
            _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
                "passed": False,
                "reason": "final_action_contract_current_lots_mismatch",
                "source_type": _recommendation_source_type(recommendation),
                "contract_current_lots": int(contract_current_lots),
                "actual_current_lots": int(current_lots),
                "business_boundary": "trader_must_execute_final_action_contract_current_lots",
            }
            target_lots = current_lots
        else:
            target_lots = _safe_int(final_action_contract.get("target_lots"), current_lots)
            lots_delta = _safe_int(final_action_contract.get("lots_delta"), target_lots - current_lots)
            if lots_delta != target_lots - current_lots:
                add_rewrite_reason(snapshot, "final_action_contract_lots_delta_mismatch")
                target_lots = current_lots
                _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
                    "passed": False,
                    "reason": "final_action_contract_lots_delta_mismatch",
                    "source_type": _recommendation_source_type(recommendation),
                    "contract_lots_delta": int(lots_delta),
                    "expected_lots_delta": int(_safe_int(final_action_contract.get("target_lots"), current_lots) - current_lots),
                    "business_boundary": "trader_must_execute_self_consistent_final_action_contract",
                }
            else:
                _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
                    "passed": True,
                    "reason": "final_action_contract_present",
                    "source_type": _recommendation_source_type(recommendation),
                    "current_lots": int(current_lots),
                    "target_lots": int(target_lots),
                    "final_action_contract": final_action_contract,
                    "business_boundary": "strategy_trade_target_lots_come_only_from_final_action_contract",
                }

        ensure_execution_translation(snapshot)["final_action_contract_source"] = {
            "source": "final_action_contract",
            "contract_type": final_action_contract.get("contract_type") or "strategy",
            "final_action": final_action_contract.get("final_action"),
            "current_lots": int(current_lots),
            "target_lots": int(target_lots),
            "lots_delta": int(target_lots - current_lots),
        }
        signal_lifecycle = _align_signal_lifecycle_to_target(snapshot, target_lots, signal_lifecycle)
        ensure_execution_translation(snapshot)["signal_lifecycle"] = dict(signal_lifecycle or {})
        signal_invalidation_observed = _signal_invalidation_breached(
            current_price,
            target_lots,
            signal_lifecycle,
        )
        exit_policy_result = evaluate_exit_policy(
            ticker=ticker,
            current_price=float(current_price),
            current_lots=current_lots,
            target_lots=target_lots,
            lifecycle=signal_lifecycle,
            current_position=current_position,
            trading_date=recommendation.get("effective_trade_date") or recommendation.get("trading_date"),
            config=config,
        )
        phase2_execution = _ensure_phase2_execution(snapshot)
        phase2_execution["contract_execution_observation"] = {
            "signal_invalidation_observed": bool(signal_invalidation_observed),
            "exit_policy_required": bool(exit_policy_result.get("exit_required")),
            "exit_policy_reason": exit_policy_result.get("reason"),
            "business_boundary": (
                "Trader records execution observations but does not rewrite "
                "strategy final_action_contract target_lots; PM/Auditor must "
                "encode reduce/exit/hold before Phase2."
            ),
        }

        if _requires_entry_authority(current_lots, target_lots):
            authority_consistency = _final_entry_authority_consistency(snapshot)
            final_authority = authority_consistency.get("selected_authority") or {}
            if not authority_consistency.get("passed") or not _authority_allows_entry(final_authority):
                original_target_lots = int(target_lots or 0)
                target_lots = int(current_lots)
                block_reason = (
                    str(authority_consistency.get("reason") or "final_contract_authority_not_met")
                    if not authority_consistency.get("passed")
                    else "final_contract_authority_not_met"
                )
                add_rewrite_reason(snapshot, block_reason)
                _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
                    "passed": False,
                    "reason": block_reason,
                    "source_type": _recommendation_source_type(recommendation),
                    "current_lots": int(current_lots),
                    "original_target_lots": int(original_target_lots),
                    "target_lots_after_validation": int(target_lots),
                    "contract_authority_audit": final_authority,
                    "authority_consistency": authority_consistency,
                    "business_boundary": (
                        "invalid final_action_contract authority cannot be partially "
                        "retranslated by Trader; no strategy transaction is emitted"
                    ),
                }

        phase2_execution["exit_policy"] = exit_policy_result
        phase2_execution["entry_timing"] = phase2_entry_audit(
            target_lots=target_lots,
            current_lots=current_lots,
            price_context=morning_price_context,
        )
        phase2_execution["execution_simulation"] = execution_price_basis(morning_price_context)

        if risk_level == RiskLevel.DANGER and current_lots == 0 and target_lots != 0:
            target_lots = int(current_lots)
            add_rewrite_reason(snapshot, "danger_zone_ban")
        if risk_level == RiskLevel.EMERGENCY and current_lots == 0 and target_lots != 0:
            target_lots = int(current_lots)
            add_rewrite_reason(snapshot, "reduce_only")

        target_margin_rate = float(contract_info["margin_rate_long"] if target_lots >= 0 else contract_info["margin_rate_short"])
        margin_required = current_price * abs(target_lots) * multiplier * target_margin_rate
        if margin_required > remaining_margin and abs(target_lots) > 0 and current_price > 0:
            if _requires_entry_authority(current_lots, target_lots):
                target_lots = int(current_lots)
                add_rewrite_reason(snapshot, "margin_adjustment_to_no_new_entry")

        decision = _decision_from_contract_target(
            ticker=ticker,
            current_lots=current_lots,
            target_lots=target_lots,
            current_price=current_price,
            multiplier=multiplier,
            contract_info=contract_info,
            contract_code=contract_code,
            justification_prefix="Final-action-contract translation",
        )
        if decision.action == FuturesAction.HOLD and decision.lots == 0:
            reason_codes = final_action_contract.get("reason_codes") if isinstance(final_action_contract.get("reason_codes"), list) else []
            add_rewrite_reason(snapshot, normalize_no_trade_reason(reason_codes[-1] if reason_codes else "") or "position_matched")

        _record_phase2_order_plan(
            snapshot,
            current_lots=current_lots,
            target_lots=target_lots,
            account_equity=account_equity,
            current_price=current_price,
            risk_level=risk_level,
            cashflow_ratio=cashflow_ratio,
            current_margin_ratio=current_margin_ratio,
            max_total_margin_ratio=max_total_margin_ratio,
            max_single_margin_ratio=max_single_margin_ratio,
            remaining_margin=remaining_margin,
            decision=decision,
            signal_lifecycle=signal_lifecycle,
        )
        return decision

    if _is_strategy_recommendation(recommendation) and not final_action_contract:
        add_rewrite_reason(snapshot, "missing_final_action_contract")
        _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
            "passed": False,
            "reason": "missing_final_action_contract",
            "required_for": "strategy_recommendation",
            "source_type": _recommendation_source_type(recommendation),
            "current_lots": int(current_lots),
            "target_lots_after_validation": int(current_lots),
            "business_boundary": "strategy_recommendation_requires_final_action_contract",
        }
        decision = FuturesDecision(
            ticker=ticker,
            action=FuturesAction.HOLD,
            lots=0,
            price=current_price,
            settle_price=current_price,
            margin_rate=float(contract_info["margin_rate_long"]),
            contract_multiplier=multiplier,
            contract_code=contract_code,
            justification=(
                f"{ticker} strategy recommendation missing final_action_contract; "
                "converted to HOLD so Trader cannot translate PM drafts or raw lots."
            ),
        )
        _record_phase2_order_plan(
            snapshot,
            current_lots=current_lots,
            target_lots=current_lots,
            account_equity=account_equity,
            current_price=current_price,
            risk_level=risk_level,
            cashflow_ratio=cashflow_ratio,
            current_margin_ratio=current_margin_ratio,
            max_total_margin_ratio=max_total_margin_ratio,
            max_single_margin_ratio=max_single_margin_ratio,
            remaining_margin=remaining_margin,
            decision=decision,
            signal_lifecycle=signal_lifecycle,
        )
        return decision

    if not _raw_action_lots_allowed_source(recommendation):
        add_rewrite_reason(snapshot, "unsupported_raw_action_source_type")
        _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
            "passed": False,
            "reason": "unsupported_raw_action_source_type",
            "source_type": _recommendation_source_type(recommendation),
            "current_lots": int(current_lots),
            "target_lots_after_validation": int(current_lots),
            "business_boundary": (
                "raw recommendation action/lots translation is only allowed for "
                "rollover or forced_risk operational orders"
            ),
        }
        decision = FuturesDecision(
            ticker=ticker,
            action=FuturesAction.HOLD,
            lots=0,
            price=current_price,
            settle_price=current_price,
            margin_rate=float(contract_info["margin_rate_long"]),
            contract_multiplier=multiplier,
            contract_code=contract_code,
            justification=(
                f"{ticker} recommendation source_type={_recommendation_source_type(recommendation)} "
                "cannot use raw action/lots translation; converted to HOLD."
            ),
        )
        _record_phase2_order_plan(
            snapshot,
            current_lots=current_lots,
            target_lots=current_lots,
            account_equity=account_equity,
            current_price=current_price,
            risk_level=risk_level,
            cashflow_ratio=cashflow_ratio,
            current_margin_ratio=current_margin_ratio,
            max_total_margin_ratio=max_total_margin_ratio,
            max_single_margin_ratio=max_single_margin_ratio,
            remaining_margin=remaining_margin,
            decision=decision,
            signal_lifecycle=signal_lifecycle,
        )
        return decision

    action_value = _enum_value(recommendation.get("action"))
    direct_lots = int(recommendation.get("lots", 0) or 0)
    if action_value == FuturesAction.OPEN_LONG.value:
        target_lots = direct_lots
    elif action_value == FuturesAction.OPEN_SHORT.value:
        target_lots = -direct_lots
    elif action_value == FuturesAction.CLOSE_LONG.value:
        target_lots = max(0, current_lots - direct_lots)
    elif action_value == FuturesAction.CLOSE_SHORT.value:
        target_lots = min(0, current_lots + direct_lots)
    else:
        target_lots = current_lots

    signal_lifecycle = _align_signal_lifecycle_to_target(snapshot, target_lots, signal_lifecycle)
    ensure_execution_translation(snapshot)["signal_lifecycle"] = dict(signal_lifecycle or {})
    if _signal_invalidation_breached(current_price, target_lots, signal_lifecycle):
        target_lots = 0
        add_rewrite_reason(snapshot, "signal_invalidation_level")

    exit_policy_result = evaluate_exit_policy(
        ticker=ticker,
        current_price=float(current_price),
        current_lots=current_lots,
        target_lots=target_lots,
        lifecycle=signal_lifecycle,
        current_position=current_position,
        trading_date=recommendation.get("effective_trade_date") or recommendation.get("trading_date"),
        config=config,
    )
    if exit_policy_result.get("exit_required"):
        target_lots = int(exit_policy_result.get("target_lots") or 0)
        add_rewrite_reason(snapshot, str(exit_policy_result.get("reason") or "exit_policy"))

    if _is_strategy_recommendation(recommendation) and _requires_entry_authority(current_lots, target_lots):
        authority_consistency = _final_entry_authority_consistency(snapshot)
        final_authority = authority_consistency.get("selected_authority") or {}
        if not authority_consistency.get("passed"):
            original_target_lots = int(target_lots or 0)
            target_lots = _target_lots_without_new_entry(current_lots, target_lots)
            block_reason = str(authority_consistency.get("reason") or "final_contract_authority_not_met")
            add_rewrite_reason(snapshot, block_reason)
            _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
                "passed": False,
                "reason": block_reason,
                "source_type": _recommendation_source_type(recommendation),
                "current_lots": int(current_lots),
                "original_target_lots": int(original_target_lots),
                "target_lots_after_validation": int(target_lots),
                "contract_authority_audit": final_authority,
                "authority_consistency": authority_consistency,
                "business_boundary": "strategy_new_entry_requires_pm_final_trade_authority",
            }
        elif not _authority_allows_entry(final_authority):
            original_target_lots = int(target_lots or 0)
            target_lots = _target_lots_without_new_entry(current_lots, target_lots)
            add_rewrite_reason(snapshot, "final_contract_authority_not_met")
            _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
                "passed": False,
                "reason": "final_contract_authority_not_met",
                "source_type": _recommendation_source_type(recommendation),
                "current_lots": int(current_lots),
                "original_target_lots": int(original_target_lots),
                "target_lots_after_validation": int(target_lots),
                "contract_authority_audit": final_authority,
                "authority_consistency": authority_consistency,
                "business_boundary": "strategy_new_entry_requires_pm_final_trade_authority",
            }
        else:
            _ensure_phase2_execution(snapshot)["pm_plan_validation"] = {
                "passed": True,
                "reason": "final_trade_authority_present",
                "source_type": _recommendation_source_type(recommendation),
                "current_lots": int(current_lots),
                "target_lots": int(target_lots),
                "contract_authority_audit": final_authority,
                "authority_consistency": authority_consistency,
            }
    phase2_execution = _ensure_phase2_execution(snapshot)
    phase2_execution["exit_policy"] = exit_policy_result
    phase2_execution["entry_timing"] = phase2_entry_audit(
        target_lots=target_lots,
        current_lots=current_lots,
        price_context=morning_price_context,
    )
    phase2_execution["execution_simulation"] = execution_price_basis(morning_price_context)

    max_target_notional = account_equity * max_single_margin_ratio
    if current_price > 0 and multiplier > 0 and max_target_notional > 0:
        max_abs_target_lots = int(max_target_notional / (current_price * multiplier))
        capped_target_lots = _cap_target_lots_by_abs_limit(target_lots, max_abs_target_lots)
        if capped_target_lots != target_lots:
            target_lots = capped_target_lots
            add_rewrite_reason(snapshot, "base_sizing_anchor_cap")

    if risk_level == RiskLevel.DANGER and current_lots == 0 and target_lots != 0:
        target_lots = 0
        add_rewrite_reason(snapshot, "danger_zone_ban")

    if risk_level == RiskLevel.EMERGENCY:
        if current_lots > 0:
            add_rewrite_reason(snapshot, "reduce_only")
            decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.CLOSE_LONG,
                lots=abs(current_lots),
                price=current_price,
                settle_price=current_price,
                margin_rate=float(contract_info["margin_rate_long"]),
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"EMERGENCY risk level at open: flatten long position in {ticker}.",
            )
            _record_phase2_order_plan(
                snapshot,
                current_lots=current_lots,
                target_lots=0,
                account_equity=account_equity,
                current_price=current_price,
                risk_level=risk_level,
                cashflow_ratio=cashflow_ratio,
                current_margin_ratio=current_margin_ratio,
                max_total_margin_ratio=max_total_margin_ratio,
                max_single_margin_ratio=max_single_margin_ratio,
                remaining_margin=remaining_margin,
                decision=decision,
                signal_lifecycle=signal_lifecycle,
            )
            return decision
        if current_lots < 0:
            add_rewrite_reason(snapshot, "reduce_only")
            decision = FuturesDecision(
                ticker=ticker,
                action=FuturesAction.CLOSE_SHORT,
                lots=abs(current_lots),
                price=current_price,
                settle_price=current_price,
                margin_rate=float(contract_info["margin_rate_short"]),
                contract_multiplier=multiplier,
                contract_code=contract_code,
                justification=f"EMERGENCY risk level at open: flatten short position in {ticker}.",
            )
            _record_phase2_order_plan(
                snapshot,
                current_lots=current_lots,
                target_lots=0,
                account_equity=account_equity,
                current_price=current_price,
                risk_level=risk_level,
                cashflow_ratio=cashflow_ratio,
                current_margin_ratio=current_margin_ratio,
                max_total_margin_ratio=max_total_margin_ratio,
                max_single_margin_ratio=max_single_margin_ratio,
                remaining_margin=remaining_margin,
                decision=decision,
                signal_lifecycle=signal_lifecycle,
            )
            return decision
        add_rewrite_reason(snapshot, "reduce_only")
        decision = FuturesDecision(
            ticker=ticker,
            action=FuturesAction.HOLD,
            lots=0,
            price=current_price,
            settle_price=current_price,
            margin_rate=float(contract_info["margin_rate_long"]),
            contract_multiplier=multiplier,
            contract_code=contract_code,
            justification=f"EMERGENCY risk level at open: block new position in {ticker}.",
        )
        _record_phase2_order_plan(
            snapshot,
            current_lots=current_lots,
            target_lots=0,
            account_equity=account_equity,
            current_price=current_price,
            risk_level=risk_level,
            cashflow_ratio=cashflow_ratio,
            current_margin_ratio=current_margin_ratio,
            max_total_margin_ratio=max_total_margin_ratio,
            max_single_margin_ratio=max_single_margin_ratio,
            remaining_margin=remaining_margin,
            decision=decision,
            signal_lifecycle=signal_lifecycle,
        )
        return decision

    target_margin_rate = float(contract_info["margin_rate_long"] if target_lots >= 0 else contract_info["margin_rate_short"])
    margin_required = current_price * abs(target_lots) * multiplier * target_margin_rate
    if margin_required > remaining_margin and abs(target_lots) > 0 and current_price > 0:
        max_lots = int(remaining_margin / (current_price * multiplier * target_margin_rate)) if remaining_margin > 0 else 0
        target_lots = max_lots if target_lots >= 0 else -max_lots
        add_rewrite_reason(snapshot, "margin_adjustment" if max_lots > 0 else "margin_adjustment_to_zero")

    if abs(target_lots - current_lots) > 0:
        lots_to_trade = abs(target_lots - current_lots)
        if target_lots > current_lots:
            action_type = FuturesAction.OPEN_LONG if current_lots >= 0 else FuturesAction.CLOSE_SHORT
        elif target_lots < current_lots:
            action_type = FuturesAction.CLOSE_LONG if current_lots > 0 else FuturesAction.OPEN_SHORT
        else:
            action_type = FuturesAction.HOLD
    else:
        lots_to_trade = 0
        action_type = FuturesAction.HOLD

    margin_rate = float(
        contract_info["margin_rate_long"]
        if action_type in {FuturesAction.OPEN_LONG, FuturesAction.CLOSE_LONG, FuturesAction.HOLD}
        else contract_info["margin_rate_short"]
    )
    decision = FuturesDecision(
        ticker=ticker,
        action=action_type,
        lots=lots_to_trade,
        price=current_price,
        settle_price=current_price,
        margin_rate=margin_rate,
        contract_multiplier=multiplier,
        contract_code=contract_code,
        justification=(
            f"Open-order translation from pre-open plan: target_lots={target_lots}, current_lots={current_lots}, "
            f"account_equity_ratio={cashflow_ratio:.2%}, reduce_only={force_reduce_only}."
        ),
    )

    if decision.action != FuturesAction.HOLD and decision.lots <= 0:
        decision.action = FuturesAction.HOLD
        decision.lots = 0
        decision.justification += " [Converted to HOLD because tradable lots <= 0.]"
        add_rewrite_reason(snapshot, "hold_or_zero_lots")

    decision_margin_required = abs(decision.lots) * decision.price * multiplier * decision.margin_rate
    if decision_margin_required > remaining_margin and decision.action in {FuturesAction.OPEN_LONG, FuturesAction.OPEN_SHORT}:
        adjusted_lots = int(remaining_margin / (decision.price * multiplier * decision.margin_rate)) if remaining_margin > 0 else 0
        if adjusted_lots > 0:
            decision.lots = adjusted_lots
            decision.justification += f" [Margin adjustment: reduced lots to {adjusted_lots}.]"
            add_rewrite_reason(snapshot, "margin_adjustment")
        else:
            decision.action = FuturesAction.HOLD
            decision.lots = 0
            decision.justification += " [Margin constraint: converted to HOLD.]"
            add_rewrite_reason(snapshot, "margin_insufficient")

    single_position_margin = abs(decision.lots) * decision.price * multiplier * decision.margin_rate
    if (
        single_position_margin > max_single_margin
        and decision.action in {FuturesAction.OPEN_LONG, FuturesAction.OPEN_SHORT}
    ):
        max_lots_for_single = int(max_single_margin / (decision.price * multiplier * decision.margin_rate)) if max_single_margin > 0 else 0
        if max_lots_for_single > 0:
            decision.lots = min(decision.lots, max_lots_for_single)
            decision.justification += f" [Base sizing anchor adjustment: reduced lots to {decision.lots}.]"
            add_rewrite_reason(snapshot, "base_sizing_anchor_cap")
        else:
            decision.action = FuturesAction.HOLD
            decision.lots = 0
            decision.justification += " [Base sizing anchor: converted to HOLD.]"
            add_rewrite_reason(snapshot, "base_sizing_anchor_cap")
    if force_reduce_only and decision.action in {FuturesAction.OPEN_LONG, FuturesAction.OPEN_SHORT}:
        if current_lots > 0 and decision.action == FuturesAction.OPEN_SHORT:
            decision.action = FuturesAction.CLOSE_LONG
        elif current_lots < 0 and decision.action == FuturesAction.OPEN_LONG:
            decision.action = FuturesAction.CLOSE_SHORT
        else:
            decision.action = FuturesAction.HOLD
            decision.lots = 0
        decision.justification += " [Reduce-only mode: blocked new opening trade.]"
        add_rewrite_reason(snapshot, "reduce_only")

    if decision.action == FuturesAction.OPEN_LONG and current_lots < 0:
        decision.action = FuturesAction.CLOSE_SHORT
        decision.lots = min(decision.lots, abs(current_lots))
        decision.justification += " [Converted open_long to close_short against existing short position.]"
        add_rewrite_reason(snapshot, "existing_position_netting")
    elif decision.action == FuturesAction.OPEN_SHORT and current_lots > 0:
        decision.action = FuturesAction.CLOSE_LONG
        decision.lots = min(decision.lots, current_lots)
        decision.justification += " [Converted open_short to close_long against existing long position.]"
        add_rewrite_reason(snapshot, "existing_position_netting")

    if decision.action != FuturesAction.HOLD and decision.lots <= 0:
        decision.action = FuturesAction.HOLD
        decision.lots = 0
        decision.justification += " [Final zero-lot guard: converted to HOLD.]"
        add_rewrite_reason(snapshot, "hold_or_zero_lots")

    if decision.action == FuturesAction.HOLD and decision.lots == 0:
        plan_reason = None
        existing_reasons = ensure_execution_translation(snapshot).get("rewrite_reasons") or []
        if current_lots == target_lots and not plan_reason and not existing_reasons:
            plan_reason = plan_reason or "position_matched"
        elif not plan_reason and not existing_reasons:
            plan_reason = "hold_or_zero_lots"
        if plan_reason:
            add_rewrite_reason(snapshot, plan_reason)

    _record_phase2_order_plan(
        snapshot,
        current_lots=current_lots,
        target_lots=target_lots,
        account_equity=account_equity,
        current_price=current_price,
        risk_level=risk_level,
        cashflow_ratio=cashflow_ratio,
        current_margin_ratio=current_margin_ratio,
        max_total_margin_ratio=max_total_margin_ratio,
        max_single_margin_ratio=max_single_margin_ratio,
        remaining_margin=remaining_margin,
        decision=decision,
        signal_lifecycle=signal_lifecycle,
    )
    return decision


def _process_strategy_recommendations(
    *,
    cfg: Dict[str, Any],
    db,
    config_id: str,
    router,
    execution_engine: FuturesExecutionEngine,
    portfolio: Portfolio,
    strategy_recommendations: List[Dict[str, Any]],
    trading_date_value: str,
    runtime_mode: str,
    cutoff_datetime: Optional[datetime],
    finalize_untriggered: bool,
    loop_iteration: Optional[int],
) -> Tuple[Portfolio, Dict[str, Any]]:
    summary = _new_phase2_summary()
    for recommendation in strategy_recommendations:
        summary["checked"] += 1
        ticker = recommendation["underlying_code"]
        audit_snapshot = ensure_signal_snapshot(recommendation.get("signal_snapshot"))
        morning_price_context = router.resolve_morning_execution_base_price(
            underlying_code=ticker,
            trading_date=cfg["trading_date"],
        )
        _record_execution_translation_context(audit_snapshot, recommendation, morning_price_context)

        if morning_price_context.base_price is None:
            no_trade_reason = infer_no_trade_reason(
                audit_snapshot,
                warning_message=morning_price_context.warning_message,
                default="missing_execution_basis",
            )
            _record_phase2_state(
                audit_snapshot,
                mode=runtime_mode,
                status="skipped_missing_execution_basis",
                recommendation=recommendation,
                cutoff_datetime=cutoff_datetime,
                finalize_untriggered=finalize_untriggered,
                loop_iteration=loop_iteration,
                reason=no_trade_reason,
            )
            set_execution_result(
                audit_snapshot,
                outcome="skipped",
                status=RecommendationStatus.SKIPPED.value,
                transaction_count=0,
                no_trade_reason=no_trade_reason,
                warning_message=morning_price_context.warning_message,
            )
            _attach_setup_execution_learning(
                audit_snapshot,
                status="skipped_missing_execution_basis",
                reason=no_trade_reason,
            )
            db.update_futures_recommendation_status(
                recommendation["id"],
                RecommendationStatus.SKIPPED,
                warning_message=morning_price_context.warning_message,
                signal_snapshot=audit_snapshot,
                audit_payload=build_audit_payload(audit_snapshot),
            )
            logger.warning(
                f"Open-order skipped {ticker}: no executable basis is available. "
                f"{morning_price_context.warning_message}"
            )
            summary["skipped"] += 1
            summary["no_trade_reasons"][no_trade_reason or "missing_execution_basis"] += 1
            continue

        current_position = portfolio.positions.get(ticker)
        current_lots_before = int(getattr(current_position, "shares", 0) or 0)
        decision = _translate_pre_open_recommendation_to_order(
            recommendation=recommendation,
            portfolio=portfolio,
            config=cfg,
            morning_price_context=morning_price_context,
            snapshot=audit_snapshot,
        )
        _record_phase2_state(
            audit_snapshot,
            mode=runtime_mode,
            status="translated",
            recommendation=recommendation,
            decision=decision,
            current_lots_before=current_lots_before,
            cutoff_datetime=cutoff_datetime,
            finalize_untriggered=finalize_untriggered,
            loop_iteration=loop_iteration,
        )

        execution_price_context = morning_price_context
        intraday_selection = None
        if intraday_confirmation_enabled(cfg):
            execution_price_context, intraday_selection = _resolve_phase2_execution_basis(
                router=router,
                cfg=cfg,
                recommendation=recommendation,
                decision=decision,
                morning_price_context=morning_price_context,
                cutoff_datetime=cutoff_datetime,
                finalize_untriggered=finalize_untriggered,
            )
            _record_intraday_decision(
                db,
                config_id=config_id,
                recommendation=recommendation,
                trading_date_value=trading_date_value,
                mode=runtime_mode,
                cutoff_datetime=cutoff_datetime,
                selection=intraday_selection,
            )
            if intraday_selection is not None:
                summary["intraday"][f"{intraday_selection.decision}:{intraday_selection.reason}"] += 1
            if intraday_selection is not None and not intraday_selection.should_execute:
                _mark_intraday_non_execution(
                    db=db,
                    recommendation=recommendation,
                    audit_snapshot=audit_snapshot,
                    selection=intraday_selection,
                    finalize_untriggered=finalize_untriggered,
                    runtime_mode=runtime_mode,
                    cutoff_datetime=cutoff_datetime,
                    loop_iteration=loop_iteration,
                )
                logger.info(
                    f"Intraday execution gate held {ticker}: "
                    f"decision={intraday_selection.decision}, reason={intraday_selection.reason}"
                )
                if finalize_untriggered:
                    summary["skipped"] += 1
                    summary["no_trade_reasons"][intraday_selection.reason] += 1
                else:
                    summary["waiting"] += 1
                continue
            if execution_price_context.base_price is not None:
                _record_execution_translation_context(audit_snapshot, recommendation, execution_price_context)
                decision = _translate_pre_open_recommendation_to_order(
                    recommendation=recommendation,
                    portfolio=portfolio,
                    config=cfg,
                    morning_price_context=execution_price_context,
                    snapshot=audit_snapshot,
                )
                _record_phase2_state(
                    audit_snapshot,
                    mode=runtime_mode,
                    status="translated_with_intraday_basis",
                    recommendation=recommendation,
                    decision=decision,
                    selection=intraday_selection,
                    current_lots_before=current_lots_before,
                    cutoff_datetime=cutoff_datetime,
                    finalize_untriggered=finalize_untriggered,
                    loop_iteration=loop_iteration,
                )

        needs_two_step_reversal = _needs_two_step_reversal(current_lots_before, decision)
        if needs_two_step_reversal:
            first_leg_lots = abs(current_lots_before)
            if decision.action == FuturesAction.CLOSE_LONG:
                decision.lots = first_leg_lots
            elif decision.action == FuturesAction.CLOSE_SHORT:
                decision.lots = first_leg_lots
            decision.justification += (
                f" [Split reversal step 1/2: flatten existing position of {first_leg_lots} lot(s) first.]"
            )
            add_rewrite_reason(audit_snapshot, "two_step_reversal")
            logger.info(
                f"Open-order reversal split for {ticker}: current_lots={current_lots_before}, "
                f"first_leg={_enum_value(decision.action)} {first_leg_lots}"
            )
            _ensure_phase2_execution(audit_snapshot)["two_step_reversal"] = True

        append_translated_order(
            audit_snapshot,
            action=decision.action,
            lots=decision.lots,
            contract_code=decision.contract_code,
            price=decision.price,
            stage="phase2_leg1" if needs_two_step_reversal else "phase2",
        )
        executable_recommendation = _build_executable_recommendation(
            recommendation=recommendation,
            decision=decision,
            morning_price_context=execution_price_context,
        )
        action_value = _enum_value(decision.action)
        final_status = "executed_without_transaction" if action_value == FuturesAction.HOLD.value or int(decision.lots or 0) <= 0 else "executed"
        _record_phase2_state(
            audit_snapshot,
            mode=runtime_mode,
            status=final_status,
            recommendation=recommendation,
            decision=decision,
            selection=intraday_selection,
            current_lots_before=current_lots_before,
            cutoff_datetime=cutoff_datetime,
            finalize_untriggered=finalize_untriggered,
            loop_iteration=loop_iteration,
            reason=infer_no_trade_reason(audit_snapshot) if final_status == "executed_without_transaction" else None,
        )
        _attach_setup_execution_learning(
            audit_snapshot,
            status=final_status,
            reason=infer_no_trade_reason(audit_snapshot) if final_status == "executed_without_transaction" else None,
            selection=intraday_selection,
        )
        executable_recommendation["signal_snapshot"] = audit_snapshot
        executable_recommendation["audit_payload"] = build_audit_payload(audit_snapshot)
        portfolio = execution_engine.execute_recommendation(
            recommendation_id=recommendation["id"],
            recommendation=executable_recommendation,
            portfolio=portfolio,
            trading_date=cfg["trading_date"],
            execution_phase=TradingPhase.PHASE2,
        )
        summary["actions"][action_value] += 1
        if final_status == "executed_without_transaction":
            summary["holds"] += 1
            summary["no_trade_reasons"][infer_no_trade_reason(audit_snapshot) or "hold_or_zero_lots"] += 1
        else:
            summary["executed"] += 1
        if needs_two_step_reversal:
            follow_up_decision = _translate_pre_open_recommendation_to_order(
                recommendation=recommendation,
                portfolio=portfolio,
                config=cfg,
                morning_price_context=execution_price_context,
                snapshot=audit_snapshot,
            )
            append_translated_order(
                audit_snapshot,
                action=follow_up_decision.action,
                lots=follow_up_decision.lots,
                contract_code=follow_up_decision.contract_code,
                price=follow_up_decision.price,
                stage="phase2_leg2",
            )
            follow_up_recommendation = _build_executable_recommendation(
                recommendation=recommendation,
                decision=follow_up_decision,
                morning_price_context=execution_price_context,
            )
            follow_up_recommendation["signal_snapshot"] = audit_snapshot
            follow_up_recommendation["audit_payload"] = build_audit_payload(audit_snapshot)
            portfolio = execution_engine.execute_recommendation(
                recommendation_id=recommendation["id"],
                recommendation=follow_up_recommendation,
                portfolio=portfolio,
                trading_date=cfg["trading_date"],
                execution_phase=TradingPhase.PHASE2,
            )
            follow_up_action = _enum_value(follow_up_decision.action)
            summary["actions"][follow_up_action] += 1
    return portfolio, summary


def trader_agent(argv: Optional[List[str]] = None) -> None:
    """Run the futures trader agent from CLI-style arguments."""
    parser = argparse.ArgumentParser(description="Run AgentQuant futures trader agent for Phase2 execution")
    parser.add_argument("--config", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--trading-date", type=str, required=True, help="Trading date in format YYYY-MM-DD")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    parser.add_argument("--loop", action="store_true", help="Keep Phase2 running for paper trading until intraday triggers or finalization time")
    parser.add_argument("--check-interval-seconds", type=int, default=None, help="Paper-trading loop sleep interval override")
    args = parser.parse_args(argv)

    cfg = ConfigParser(args).get_config()
    if cfg.get("market_type") != "china_futures":
        raise RuntimeError("trader agent only supports china_futures")
    if not args.local_db:
        raise RuntimeError("china_futures open-order phase currently requires --local-db")

    db_initialize(use_local_db=args.local_db)
    db = get_db()
    config_id = load_portfolio_config(cfg, db, reset_portfolio=False)
    ensure_seed_settled_portfolio(cfg, db, config_id)

    trading_date_value = cfg["trading_date"].strftime("%Y-%m-%d")
    logger.set_context(exp_name=cfg["exp_name"], trading_date=trading_date_value, phase=TradingPhase.PHASE2.value)
    phase1_record = db.get_trading_day_phase(config_id, trading_date_value, TradingPhase.PHASE1)
    if not phase1_record or phase1_record.get("status") != "completed":
        raise RuntimeError(f"Phase1 is not completed for {cfg['exp_name']} on {trading_date_value}")

    phase2_record = db.get_trading_day_phase(config_id, trading_date_value, TradingPhase.PHASE2)
    if phase2_record and phase2_record.get("status") == "completed":
        raise RuntimeError(f"Phase2 already completed for {cfg['exp_name']} on {trading_date_value}")

    logger.info(f"Phase2 started for {cfg['exp_name']} on {trading_date_value}")
    db.start_trading_day_phase(config_id, trading_date_value, TradingPhase.PHASE2)

    try:
        portfolio_dict = db.get_latest_settled_portfolio(config_id)
        if not portfolio_dict:
            raise RuntimeError(f"Missing settled portfolio for {cfg['exp_name']}")
        portfolio = Portfolio(**portfolio_dict)

        router = Router(APISource.PANDAAI, market_type="china_futures")
        execution_engine = FuturesExecutionEngine(cfg, db)
        portfolio = _execute_pending_forced_risk_before_strategy(
            execution_engine=execution_engine,
            config_id=config_id,
            trading_date=cfg["trading_date"],
            portfolio=portfolio,
            cutoff_datetime=None,
        )

        strategy_recommendations = db.get_futures_recommendations_by_effective_date(
            config_id=config_id,
            effective_trade_date=cfg["trading_date"],
            source_type=RecommendationSourceType.STRATEGY,
            status=RecommendationStatus.PENDING,
        )
        strategy_recommendations = _sort_recommendations_by_ticker_order(strategy_recommendations, cfg.get("tickers", []))
        strategy_recommendations_by_ticker = {
            recommendation["underlying_code"]: recommendation
            for recommendation in strategy_recommendations
        }

        pending_rollovers = db.get_futures_recommendations_by_effective_date(
            config_id=config_id,
            effective_trade_date=cfg["trading_date"],
            source_type=RecommendationSourceType.ROLLOVER,
            status=RecommendationStatus.PENDING,
        )
        pending_rollovers = _sort_recommendations_by_ticker_order(pending_rollovers, cfg.get("tickers", []))
        for rollover_recommendation in pending_rollovers:
            ticker = rollover_recommendation["underlying_code"]
            current_position = portfolio.positions.get(ticker)
            current_lots = int(getattr(current_position, "shares", 0) or 0)
            adjusted_rollover = _reconcile_rollover_with_strategy_target(
                rollover_recommendation=rollover_recommendation,
                strategy_recommendation=strategy_recommendations_by_ticker.get(ticker),
                current_lots=current_lots,
                config=cfg,
            )
            portfolio = execution_engine.execute_recommendation(
                recommendation_id=rollover_recommendation["id"],
                recommendation=adjusted_rollover,
                portfolio=portfolio,
                trading_date=cfg["trading_date"],
                execution_phase=TradingPhase.PHASE2,
            )

        runtime_mode = "paper_loop" if args.loop else "backtest_replay"
        loop_interval = int(
            args.check_interval_seconds
            or (((cfg.get("execution", {}) or {}).get("intraday_confirmation") or {}).get("loop_check_interval_seconds", 300))
        )
        loop_iteration = 0
        total_summary = _new_phase2_summary()

        while True:
            loop_iteration += 1
            portfolio = _execute_pending_forced_risk_before_strategy(
                execution_engine=execution_engine,
                config_id=config_id,
                trading_date=cfg["trading_date"],
                portfolio=portfolio,
                cutoff_datetime=datetime.now() if args.loop else None,
            )
            strategy_recommendations = db.get_futures_recommendations_by_effective_date(
                config_id=config_id,
                effective_trade_date=cfg["trading_date"],
                source_type=RecommendationSourceType.STRATEGY,
                status=RecommendationStatus.PENDING,
            )
            strategy_recommendations = _sort_recommendations_by_ticker_order(
                strategy_recommendations,
                cfg.get("tickers", []),
            )
            cutoff_datetime = datetime.now() if args.loop else None
            finalize_untriggered = True if not args.loop else _loop_should_finalize(cfg, trading_date_value)
            logger.info(
                f"Phase2 {runtime_mode} check #{loop_iteration}: "
                f"pending_recommendations={len(strategy_recommendations)}, "
                f"cutoff_datetime={_format_datetime(cutoff_datetime) or 'full_day'}, "
                f"finalize_untriggered={finalize_untriggered}"
            )
            portfolio, iteration_summary = _process_strategy_recommendations(
                cfg=cfg,
                db=db,
                config_id=config_id,
                router=router,
                execution_engine=execution_engine,
                portfolio=portfolio,
                strategy_recommendations=strategy_recommendations,
                trading_date_value=trading_date_value,
                runtime_mode=runtime_mode,
                cutoff_datetime=cutoff_datetime,
                finalize_untriggered=finalize_untriggered,
                loop_iteration=loop_iteration,
            )
            _merge_phase2_summary(total_summary, iteration_summary)
            _log_phase2_summary(f"Phase2 {runtime_mode} check #{loop_iteration} summary", iteration_summary)

            pending_after = db.get_futures_recommendations_by_effective_date(
                config_id=config_id,
                effective_trade_date=cfg["trading_date"],
                source_type=RecommendationSourceType.STRATEGY,
                status=RecommendationStatus.PENDING,
            )
            pending_forced_risk_after = db.get_futures_recommendations_by_effective_date(
                config_id=config_id,
                effective_trade_date=cfg["trading_date"],
                source_type=RecommendationSourceType.FORCED_RISK,
                status=RecommendationStatus.PENDING,
            )
            if not args.loop or (not pending_after and not pending_forced_risk_after) or finalize_untriggered:
                break
            logger.info(
                f"Phase2 paper loop waiting: pending_recommendations={len(pending_after)}, "
                f"pending_forced_risk={len(pending_forced_risk_after)}, "
                f"sleep_seconds={loop_interval}"
            )
            time.sleep(max(1, loop_interval))

        phase2_transactions = db.get_futures_transactions_by_date(
            config_id=config_id,
            trading_date=cfg["trading_date"],
            execution_phase=TradingPhase.PHASE2,
        )
        db.complete_trading_day_phase(
            config_id,
            trading_date_value,
            TradingPhase.PHASE2,
            "completed",
            f"transactions={len(phase2_transactions)}",
        )
        logger.info(
            f"Phase2 completed for {cfg['exp_name']} on {trading_date_value}: "
            f"transactions={len(phase2_transactions)}"
        )
        _log_phase2_summary("Phase2 total execution summary", total_summary)
    except Exception as exc:
        db.complete_trading_day_phase(config_id, trading_date_value, TradingPhase.PHASE2, "failed", str(exc))
        logger.error(f"Phase2 trader execution failed: {exc}")
        raise


def main(argv: Optional[List[str]] = None) -> None:
    trader_agent(argv)


if __name__ == "__main__":
    main()

