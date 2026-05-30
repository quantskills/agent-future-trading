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
from tools.agent_tools.execution.order_sizing import cap_target_lots_by_phase1_plan, lots_from_target_ratio
from tools.agent_tools.execution.order_semantics import build_lot_intent_consistency
from tools.agent_tools.decision.position_lifecycle import cap_signed_lots_by_abs_limit
from tools.agent_tools.runtime_setup import (
    ensure_seed_settled_portfolio,
    load_portfolio_config,
    resolve_net_exposure_config,
)
from tools.agent_tools.execution.trader_exit_policy import evaluate_exit_policy
from util.config import ConfigParser
from util.db_helper import db_initialize, get_db
from util.futures_audit import (
    add_rewrite_reason,
    append_translated_order,
    build_audit_payload,
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


def _phase1_capital_diagnostics(pre_open_plan: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = (
        ((pre_open_plan or {}).get("strategy_controls") or {})
        .get("diagnostics", {})
        .get("capital_utilization_target", {})
    )
    if not isinstance(diagnostics, dict):
        return {}
    return diagnostics


def _phase1_dynamic_margin_cap(
    pre_open_plan: Dict[str, Any],
    base_single_margin_ratio: float,
    margin_rate: float,
) -> tuple[float, str]:
    diagnostics = _phase1_capital_diagnostics(pre_open_plan)
    if not diagnostics:
        return base_single_margin_ratio, "base"

    target_cap = _safe_float(diagnostics.get("dynamic_opportunity_margin_ratio_budget"), 0.0)
    if target_cap <= 0:
        target_cap = _safe_float(diagnostics.get("dynamic_opportunity_margin_ratio_cap"), 0.0)
    target_estimate = _safe_float((pre_open_plan or {}).get("target_margin_ratio_estimate"), 0.0)
    if target_cap <= 0 and bool(diagnostics.get("high_quality_memory")):
        target_cap = target_estimate
    if target_cap <= 0:
        return base_single_margin_ratio, "base"
    if margin_rate > 0:
        target_cap = target_cap / margin_rate
    return target_cap, str(diagnostics.get("dynamic_allocation_tier") or "dynamic")


def _phase1_dynamic_net_exposure_cap(pre_open_plan: Dict[str, Any], base_max_net_exposure: float) -> tuple[float, str]:
    control = (
        ((pre_open_plan or {}).get("strategy_controls") or {})
        .get("diagnostics", {})
        .get("net_exposure_control", {})
    )
    capital = _phase1_capital_diagnostics(pre_open_plan)
    if (
        isinstance(control, dict)
        and str(control.get("cap_mode") or "") in {"strong_opportunity", "alpha_release"}
        and bool(capital.get("high_quality_memory"))
    ):
        max_net = _safe_float(control.get("max_net_exposure"), base_max_net_exposure)
        return max(base_max_net_exposure, max_net), str(control.get("cap_mode") or "alpha_release")
    return base_max_net_exposure, "base"


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
    if getattr(morning_price_context, "intraday_audit", None):
        translation["intraday_execution"] = morning_price_context.intraday_audit


def _ensure_phase2_execution(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    audit = snapshot.get("phase2_execution")
    if not isinstance(audit, dict):
        audit = {}
    snapshot["phase2_execution"] = audit
    return audit


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
) -> None:
    translation = ensure_execution_translation(snapshot)
    lifecycle = extract_signal_lifecycle(snapshot)
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
    pre_open_plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
    phase1_consistency = pre_open_plan.get("recommendation_position_consistency")
    if isinstance(phase1_consistency, dict):
        translation["phase1_recommendation_position_consistency"] = phase1_consistency
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
    force_immediate = action_value in {FuturesAction.CLOSE_LONG.value, FuturesAction.CLOSE_SHORT.value}
    basis, selection = resolve_intraday_execution_basis(
        router=router,
        config=cfg,
        underlying_code=recommendation["underlying_code"],
        trading_date=cfg["trading_date"],
        action=decision.action,
        contract_code=decision.contract_code or recommendation.get("contract_code"),
        market_confirmation=_market_confirmation_from_recommendation(recommendation),
        strategy_memory=_strategy_memory_from_recommendation(recommendation),
        adaptive_policy_state=_adaptive_policy_state_from_recommendation(recommendation),
        decision_context=_decision_context_from_recommendation(recommendation, cfg),
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


def _market_confirmation_from_recommendation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot") or {}
    if not isinstance(snapshot, dict):
        return {}
    confirmation = snapshot.get("market_confirmation")
    if isinstance(confirmation, dict):
        return confirmation
    plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
    confirmation = plan.get("market_confirmation") if isinstance(plan, dict) else None
    return confirmation if isinstance(confirmation, dict) else {}


def _strategy_memory_from_recommendation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot") or {}
    if not isinstance(snapshot, dict):
        return {}
    plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
    if not isinstance(plan, dict):
        return {}
    controls = plan.get("strategy_controls") if isinstance(plan.get("strategy_controls"), dict) else {}
    diagnostics = controls.get("diagnostics") if isinstance(controls.get("diagnostics"), dict) else {}
    trade_auditor = plan.get("trade_auditor") or plan.get("decision_planner") or {}
    auditor_diag = (
        trade_auditor.get("diagnostics")
        if isinstance(trade_auditor, dict) and isinstance(trade_auditor.get("diagnostics"), dict)
        else {}
    )
    memory = auditor_diag.get("strategy_memory") if isinstance(auditor_diag.get("strategy_memory"), dict) else {}
    if memory:
        return memory
    learning = (
        diagnostics.get("capital_utilization_learning")
        if isinstance(diagnostics.get("capital_utilization_learning"), dict)
        else {}
    )
    records = [
        row
        for row in (learning.get("protected_memory"), learning.get("recovering_memory"))
        if isinstance(row, dict) and row
    ]
    return {"records": records} if records else {}


def _adaptive_policy_state_from_recommendation(recommendation: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshot = recommendation.get("signal_snapshot") or {}
    if not isinstance(snapshot, dict):
        return []
    plan = snapshot.get("pre_open_plan") if isinstance(snapshot.get("pre_open_plan"), dict) else {}
    trade_auditor = plan.get("trade_auditor") or plan.get("decision_planner") or {}
    diagnostics = (
        trade_auditor.get("diagnostics")
        if isinstance(trade_auditor, dict) and isinstance(trade_auditor.get("diagnostics"), dict)
        else {}
    )
    rows = diagnostics.get("adaptive_policy_state")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _decision_context_from_recommendation(recommendation: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snapshot = recommendation.get("signal_snapshot") or {}
    plan = snapshot.get("pre_open_plan") if isinstance(snapshot, dict) and isinstance(snapshot.get("pre_open_plan"), dict) else {}
    return {
        "ticker": recommendation.get("underlying_code"),
        "underlying_code": recommendation.get("underlying_code"),
        "horizon_class": plan.get("decision_horizon") or plan.get("horizon_class"),
        "decision_horizon": plan.get("decision_horizon"),
        "market_regime": plan.get("market_regime"),
        "contextual_min_confidence": (((cfg or {}).get("learning") or {}).get("contextual_rule_calibration") or {}).get("min_confidence"),
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


def _extract_target_lots_from_recommendation(recommendation: Dict[str, Any]) -> int:
    signal_snapshot = recommendation.get("signal_snapshot") or {}
    pre_open_plan = signal_snapshot.get("pre_open_plan") if isinstance(signal_snapshot, dict) else None
    if isinstance(pre_open_plan, dict) and pre_open_plan.get("target_lots_estimate") is not None:
        return int(pre_open_plan.get("target_lots_estimate") or 0)

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


def _cap_target_lots_by_phase1_plan(target_lots: int, pre_open_plan: Dict[str, Any]) -> int:
    """Do not let phase2 price translation enlarge the phase1 target lot estimate."""
    return cap_target_lots_by_phase1_plan(target_lots, pre_open_plan)


def _translate_pre_open_recommendation_to_order(
    recommendation: Dict[str, Any],
    portfolio: Portfolio,
    config: Dict[str, Any],
    morning_price_context,
    snapshot: Dict[str, Any],
) -> FuturesDecision:
    ticker = recommendation["underlying_code"]
    signal_snapshot = recommendation.get("signal_snapshot") or {}
    pre_open_plan = signal_snapshot.get("pre_open_plan") if isinstance(signal_snapshot, dict) else None
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
    target_margin_rate_for_cap = float(contract_info["margin_rate_long"])
    if pre_open_plan and float(pre_open_plan.get("target_position_ratio") or 0.0) < 0:
        target_margin_rate_for_cap = float(contract_info["margin_rate_short"])
    max_single_margin_ratio, single_cap_mode = _phase1_dynamic_margin_cap(
        pre_open_plan or {},
        max_single_margin_ratio,
        target_margin_rate_for_cap,
    )
    max_single_margin = account_equity * max_single_margin_ratio
    force_reduce_only = current_margin_ratio >= max_total_margin_ratio

    current_position = portfolio.positions.get(ticker)
    current_lots = int(getattr(current_position, "shares", 0) or 0)
    contract_code = getattr(current_position, "contract_code", None) or recommendation.get("contract_code")

    if pre_open_plan:
        plan_reason = normalize_no_trade_reason(pre_open_plan.get("tradable_lots_reason"))
        if plan_reason == "cooling_period":
            target_lots = current_lots
            add_rewrite_reason(snapshot, "cooling_period")
        else:
            target_position_ratio = float(pre_open_plan.get("target_position_ratio") or 0.0)

            net_exposure_config, _ = resolve_net_exposure_config(config)
            max_net_exposure = float(net_exposure_config.get("max_net_exposure", 0.50))
            symmetric_scaling = bool(net_exposure_config.get("symmetric_scaling", True))
            max_net_exposure, net_cap_mode = _phase1_dynamic_net_exposure_cap(
                pre_open_plan or {},
                max_net_exposure,
            )

            current_net_exposure = _current_net_exposure(portfolio, account_equity)
            current_ticker_ratio = _signed_position_ratio(current_position, account_equity)
            projected_net_exposure = current_net_exposure - current_ticker_ratio + target_position_ratio

            if projected_net_exposure > max_net_exposure and target_position_ratio > 0:
                add_rewrite_reason(snapshot, "net_exposure_limit")
                allowed_ratio = max_net_exposure - (current_net_exposure - current_ticker_ratio)
                if symmetric_scaling:
                    target_position_ratio = max(0.0, allowed_ratio)
                else:
                    target_position_ratio = min(target_position_ratio, max(0.0, allowed_ratio))
            elif projected_net_exposure < -max_net_exposure and target_position_ratio < 0:
                add_rewrite_reason(snapshot, "net_exposure_limit")
                allowed_ratio = -max_net_exposure - (current_net_exposure - current_ticker_ratio)
                if symmetric_scaling:
                    target_position_ratio = min(0.0, allowed_ratio)
                else:
                    target_position_ratio = max(target_position_ratio, min(0.0, allowed_ratio))
            if net_cap_mode != "base":
                ensure_execution_translation(snapshot)["dynamic_net_exposure_control"] = {
                    "mode": net_cap_mode,
                    "max_net_exposure": max_net_exposure,
                }

            target_lots = lots_from_target_ratio(
                account_equity=account_equity,
                target_position_ratio=target_position_ratio,
                current_price=current_price,
                multiplier=multiplier,
            )
            capped_target_lots = _cap_target_lots_by_phase1_plan(target_lots, pre_open_plan)
            if capped_target_lots != target_lots:
                target_lots = capped_target_lots
                add_rewrite_reason(snapshot, "phase1_target_lots_cap")
    else:
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
    phase2_execution = _ensure_phase2_execution(snapshot)
    phase2_execution["exit_policy"] = exit_policy_result
    phase2_execution["entry_timing"] = phase2_entry_audit(
        target_lots=target_lots,
        current_lots=current_lots,
        price_context=morning_price_context,
        pre_open_plan=pre_open_plan,
    )
    phase2_execution["execution_simulation"] = execution_price_basis(morning_price_context)

    phase1_plan_available = isinstance(pre_open_plan, dict) and bool(pre_open_plan)
    max_target_notional = account_equity * max_single_margin_ratio
    if not phase1_plan_available and current_price > 0 and multiplier > 0 and max_target_notional > 0:
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
        not phase1_plan_available
        and single_position_margin > max_single_margin
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
    if single_cap_mode != "base":
        translation = ensure_execution_translation(snapshot)
        diagnostics = _phase1_capital_diagnostics(pre_open_plan or {})
        payload = {
            "mode": single_cap_mode,
            "max_single_margin_ratio": max_single_margin_ratio,
            "dynamic_opportunity_margin_ratio_budget": _safe_float(
                diagnostics.get("dynamic_opportunity_margin_ratio_budget"),
                _safe_float(diagnostics.get("dynamic_opportunity_margin_ratio_cap"), 0.0),
            ),
        }
        translation["dynamic_opportunity_budget_control"] = payload
        translation["dynamic_concentration_control"] = payload

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
        plan_reason = (
            normalize_no_trade_reason(pre_open_plan.get("tradable_lots_reason"))
            if isinstance(pre_open_plan, dict)
            else None
        )
        if current_lots == target_lots:
            plan_reason = plan_reason or "position_matched"
        add_rewrite_reason(snapshot, plan_reason or "hold_or_zero_lots")

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
            if not args.loop or not pending_after or finalize_untriggered:
                break
            logger.info(
                f"Phase2 paper loop waiting: pending_recommendations={len(pending_after)}, "
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
