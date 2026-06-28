from __future__ import annotations

"""Shared contract for turning reviewer attribution into usable future memory.

Reviewer attribution should not stop at explaining what happened. Each memory
artifact also states how the next run may use it, where that use is bounded,
and which current-day conditions are required before it can influence position.
"""

from typing import Any, Dict, Iterable, List, Mapping, Optional


CONTRACT_KEY = "next_round_memory_contract"
CONTRACT_VERSION = "next_round_strategy_update_v2"


def _normalize_scope(scope: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    raw = dict(scope or {})
    normalized = {
        "ticker": str(raw.get("ticker") or "*").upper(),
        "sector": str(raw.get("sector") or "*"),
        "side": str(raw.get("side") or "*").lower(),
        "setup_type": str(raw.get("setup_type") or raw.get("template") or "*"),
        "horizon_class": str(raw.get("horizon_class") or raw.get("horizon") or "*"),
        "market_regime": str(raw.get("market_regime") or raw.get("regime") or "*"),
    }
    for key, value in raw.items():
        if key not in normalized and value not in (None, ""):
            normalized[key] = value
    return normalized


def _scope_priority(scope: Mapping[str, Any]) -> str:
    ticker = str(scope.get("ticker") or "*")
    sector = str(scope.get("sector") or "*")
    template = str(scope.get("setup_type") or "*")
    side = str(scope.get("side") or "*")
    if ticker != "*" and side != "*" and template != "*":
        return "ticker_side_template"
    if ticker != "*" and side != "*":
        return "ticker_side"
    if ticker != "*":
        return "ticker"
    if sector != "*" and side != "*":
        return "sector_side"
    if sector != "*":
        return "sector"
    return "global"


def _compact_text(value: Any, max_chars: int = 180) -> str:
    text = str(value or "").strip().replace("\n", " ")
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _compact_list(values: Any, *, max_items: int = 4, max_chars: int = 180) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if isinstance(values, Mapping):
        values = [f"{key}={value}" for key, value in values.items()]
    if not isinstance(values, Iterable):
        values = [values]
    result: List[str] = []
    for item in values:
        text = _compact_text(item, max_chars=max_chars)
        if text:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def _is_candidate_like(memory_type: str, maturity_state: str, status: str = "") -> bool:
    text = f"{memory_type} {maturity_state} {status}".lower()
    return any(
        marker in text
        for marker in (
            "candidate",
            "hypothesis",
            "no_trade",
            "neutral",
            "Counterfactual",
            "diagnostic",
            "pending",
            "episode_case",
            "tracking",
        )
    )


def _is_policy_like(memory_type: str, maturity_state: str) -> bool:
    text = f"{memory_type} {maturity_state}".lower()
    return any(marker in text for marker in ("validated", "adaptive_policy", "policy", "protect", "deployable"))


def _is_risk_reduction_like(memory_type: str, maturity_state: str, status: str = "") -> bool:
    text = f"{memory_type} {maturity_state} {status}".lower()
    return any(marker in text for marker in ("tail_loss", "demote", "weak", "watchlist", "cap", "risk_suppression"))


def _default_position_authority(memory_type: str, maturity_state: str, status: str = "") -> str:
    if _is_candidate_like(memory_type, maturity_state, status):
        return "analysis_or_watchlist_only"
    if _is_risk_reduction_like(memory_type, maturity_state, status):
        return "risk_reduction_conditioned"
    if _is_policy_like(memory_type, maturity_state):
        return "pm_auditor_conditioned"
    if "digest" in f"{memory_type} {maturity_state}".lower():
        return "analysis_calibration_only"
    return "analysis_prior_only"


def _default_max_position_impact(position_authority: str) -> str:
    if position_authority == "analysis_or_watchlist_only":
        return "no_direct_position_impact"
    if position_authority == "analysis_calibration_only":
        return "no_direct_position_impact"
    if position_authority == "risk_reduction_conditioned":
        return "may_reduce_or_cap_only_through_pm_auditor"
    if position_authority == "pm_auditor_conditioned":
        return "may_affect_sizing_only_after_current_confirmation_and_auditor"
    return "no_direct_position_impact"


def _default_data_focus(scope: Mapping[str, Any]) -> List[str]:
    sector = str(scope.get("sector") or "*").lower()
    if sector in {"ferrous"}:
        return ["price trend/volatility", "inventory and demand chain", "basis or spread", "news/policy shock"]
    if sector in {"chemical", "energy"}:
        return ["price trend/volatility", "crude or feedstock linkage", "inventory/operating rate", "news/event shock"]
    if sector in {"agricultural"}:
        return ["price trend/volatility", "seasonality/supply-demand", "import or policy change", "weather/event risk"]
    if sector in {"nonferrous"}:
        return ["price trend/volatility", "inventory and spot premium", "macro/risk appetite", "supply disruption"]
    return ["price trend/volatility", "fundamental evidence", "news/event evidence", "market confirmation"]


def _default_usage_boundary(memory_type: str, maturity_state: str, status: str = "") -> List[str]:
    boundaries = [
        "Use as a rebuttable prior; current-day data, analyst evidence, and market confirmation remain dominant.",
        "Do not generalize outside the stated ticker/sector/side/horizon/regime scope without revalidation.",
        "Do not bypass auditor, hard risk controls, invalidation requirements, or the 20% total margin cap.",
    ]
    if _is_candidate_like(memory_type, maturity_state, status):
        boundaries.append(
            "Candidate, Counterfactual, neutral, and single-episode memories can guide analysis/watchlist/probe only; they cannot by themselves authorize sizing, add-ons, position_matched, or losing-position holds."
        )
    elif _is_policy_like(memory_type, maturity_state):
        boundaries.append(
            "Validated or policy memories may affect sizing only through the existing PM/auditor gates and only while their scope and validity window still match."
        )
    return boundaries


def _default_position_conditions(memory_type: str, maturity_state: str, status: str = "") -> List[str]:
    conditions = [
        "Today's same-side evidence must explicitly confirm the remembered setup and name the contradiction that would invalidate it.",
        "The intended holding horizon must match the memory horizon, or short-term timing confirmation must bridge the mismatch.",
        "A price/ATR stop or structured invalidation boundary must be present before any new position or add-on.",
        "PM must record whether today's evidence confirms, weakens, or contradicts this memory before it affects target lots.",
    ]
    if _is_candidate_like(memory_type, maturity_state, status):
        conditions.append(
            "Before validation, position impact is limited to watchlist/probe consideration; it must not increase size or justify continuing an adverse position."
        )
    elif _is_policy_like(memory_type, maturity_state):
        conditions.append(
            "For sizing impact, sample evidence must remain in-scope and auditor/risk controls must still allow the action."
        )
    return conditions


def build_next_round_memory_contract(
    *,
    memory_type: str,
    scope: Optional[Mapping[str, Any]] = None,
    usable_memory: Any = None,
    analysis_strategy_updates: Any = None,
    trading_strategy_updates: Any = None,
    usage_boundary: Any = None,
    position_impact_conditions: Any = None,
    validation_plan: Any = None,
    data_focus: Any = None,
    analyst_action_items: Any = None,
    pm_action_conditions: Any = None,
    invalidates_when: Any = None,
    position_authority: Optional[str] = None,
    max_position_impact: Optional[str] = None,
    maturity_state: str = "candidate",
    status: str = "",
    sample_count: Optional[int] = None,
    confidence_score: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a compact memory contract shared by learning payloads and events."""
    normalized_scope = _normalize_scope(scope)
    usable = _compact_list(usable_memory, max_items=5, max_chars=220)
    analysis_updates = _compact_list(analysis_strategy_updates, max_items=4, max_chars=180)
    trading_updates = _compact_list(trading_strategy_updates, max_items=4, max_chars=180)
    data_focus_items = _compact_list(data_focus, max_items=5, max_chars=120)
    if not data_focus_items:
        data_focus_items = _default_data_focus(normalized_scope)
    analyst_items = _compact_list(analyst_action_items, max_items=5, max_chars=180)
    if not analyst_items:
        analyst_items = analysis_updates or [
            "Compare today's evidence with this same-scope memory before changing signal confidence.",
            "State which data field confirms, weakens, or contradicts the remembered setup.",
        ]
    pm_items = _compact_list(pm_action_conditions, max_items=5, max_chars=200)
    if not pm_items:
        pm_items = trading_updates or [
            "Translate the memory into entry, add, hold, reduce, or exit logic only after current confirmation.",
            "Record whether the memory is in-scope and whether today's invalidation boundary is explicit.",
        ]
    boundaries = _compact_list(usage_boundary, max_items=5, max_chars=220)
    if not boundaries:
        boundaries = _default_usage_boundary(memory_type, maturity_state, status)
    position_conditions = _compact_list(position_impact_conditions, max_items=6, max_chars=220)
    if not position_conditions:
        position_conditions = _default_position_conditions(memory_type, maturity_state, status)
    invalidation_items = _compact_list(invalidates_when, max_items=4, max_chars=180)
    if not invalidation_items:
        invalidation_items = [
            "Today's same-scope evidence contradicts the remembered setup.",
            "No explicit price/ATR stop or structured invalidation boundary is available for a new/add-on position.",
            "The intended holding horizon no longer matches the memory horizon and short-term timing confirmation is absent.",
        ]
    validation = _compact_list(validation_plan, max_items=4, max_chars=180)
    authority = str(position_authority or _default_position_authority(memory_type, maturity_state, status))
    max_impact = str(max_position_impact or _default_max_position_impact(authority))
    contract: Dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "memory_type": str(memory_type or "unknown"),
        "maturity_state": str(maturity_state or "candidate"),
        "scope": normalized_scope,
        "scope_priority": _scope_priority(normalized_scope),
        "usable_memory": usable,
        "data_focus": data_focus_items,
        "analyst_action_items": analyst_items,
        "analysis_strategy_updates": analysis_updates,
        "pm_action_conditions": pm_items,
        "trading_strategy_updates": trading_updates,
        "invalidates_when": invalidation_items,
        "usage_boundary": boundaries,
        "position_impact_conditions": position_conditions,
        "position_authority": authority,
        "max_position_impact": max_impact,
        "trigger_valid": False,
        "opportunity_state": "watch_for_trigger",
        "requires_invalidation_boundary": True,
        "anti_overfit_guardrails": [
            "ticker scope before sector scope before global scope",
            "candidate and Counterfactual memories cannot directly increase size",
            "no permanent product blacklist or unconditional product boost",
            "future results are used only after settlement/backfill",
        ],
        "validation_plan": validation,
    }
    if sample_count is not None:
        try:
            contract["sample_count"] = int(sample_count)
        except Exception:
            pass
    if confidence_score is not None:
        try:
            contract["confidence_score"] = max(0.0, min(1.0, float(confidence_score)))
        except Exception:
            pass
    return contract


def attach_next_round_memory_contract(
    payload: Dict[str, Any],
    *,
    contract: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Return a copy of payload with the unified memory contract attached."""
    result = dict(payload or {})
    if CONTRACT_KEY not in result:
        result[CONTRACT_KEY] = contract or build_next_round_memory_contract(**kwargs)
    return result


def attach_or_upgrade_next_round_memory_contract(
    payload: Dict[str, Any],
    **kwargs: Any,
) -> Dict[str, Any]:
    """Attach a v2 strategy update contract, preserving any prior contract notes."""
    result = dict(payload or {})
    existing = result.get(CONTRACT_KEY)
    contract = build_next_round_memory_contract(**kwargs)
    if isinstance(existing, Mapping):
        prior_notes = existing.get("usable_memory")
        if prior_notes and not contract.get("usable_memory"):
            contract["usable_memory"] = _compact_list(prior_notes, max_items=5, max_chars=220)
        contract["previous_contract_version"] = existing.get("contract_version")
    result[CONTRACT_KEY] = contract
    return result


def build_event_memory_contract(
    *,
    event_type: str,
    scope_type: str,
    scope_key: str,
    evidence: Optional[Mapping[str, Any]] = None,
    action: Optional[Mapping[str, Any]] = None,
    status: str = "applied",
) -> Dict[str, Any]:
    """Generic fallback contract for learning_event_log rows."""
    evidence = dict(evidence or {})
    action = dict(action or {})
    usable_candidates = [
        action.get("digest"),
        action.get("hypothesis_text"),
        action.get("reason"),
        action.get("diagnosis"),
        action.get("policy_action"),
        evidence.get("dominant_category"),
        evidence.get("summary"),
    ]
    usable_memory = [item for item in usable_candidates if item not in (None, "", {})]
    if not usable_memory:
        usable_memory = [f"{event_type}: {status}"]
    memory_type = str(event_type or "learning_event")
    maturity_state = str(status or "applied")
    if memory_type in {"trade_episode_memory"}:
        maturity_state = "episode_case"
    elif "neutral" in memory_type:
        maturity_state = "tracking_or_digest"
    elif "hypothesis" in memory_type or "candidate" in memory_type:
        maturity_state = "candidate"
    elif "policy" in memory_type or "rule_validation" in memory_type:
        maturity_state = maturity_state or "policy"
    return build_next_round_memory_contract(
        memory_type=memory_type,
        maturity_state=maturity_state,
        status=str(status or ""),
        scope={"scope_type": scope_type, "scope_key": scope_key},
        usable_memory=usable_memory,
        analysis_strategy_updates=[
            "Convert attribution into a testable prior for the next comparable setup.",
            "Compare today's ticker, horizon, signal combo, and market regime before citing it.",
        ],
        trading_strategy_updates=[
            "Use the memory to refine entry, exit, hold, or probe logic only after current confirmation.",
            "If the memory is not validated or in-scope, keep it as analysis guidance rather than trading authority.",
        ],
        validation_plan=[
            "Track future same-scope outcomes before promoting this memory into stronger PM/auditor influence.",
        ],
        sample_count=evidence.get("sample_count") or evidence.get("completed_pairs") or evidence.get("episode_count"),
        confidence_score=evidence.get("confidence_score") or action.get("confidence_score"),
    )


def contract_prompt_line(contract: Any, *, max_chars: int = 320) -> str:
    """Render the contract into one compact prompt line."""
    if not isinstance(contract, Mapping):
        return ""
    usable = _compact_text("; ".join(_compact_list(contract.get("usable_memory"), max_items=2, max_chars=90)), 120)
    analyst = _compact_text("; ".join(_compact_list(contract.get("analyst_action_items"), max_items=2, max_chars=90)), 120)
    pm = _compact_text("; ".join(_compact_list(contract.get("pm_action_conditions"), max_items=2, max_chars=100)), 140)
    boundary = _compact_text("; ".join(_compact_list(contract.get("usage_boundary"), max_items=2, max_chars=100)), 140)
    position = _compact_text(
        "; ".join(_compact_list(contract.get("position_impact_conditions"), max_items=2, max_chars=110)),
        160,
    )
    authority = _compact_text(contract.get("position_authority") or "", 80)
    scope = contract.get("scope") if isinstance(contract.get("scope"), Mapping) else {}
    scope_bits = "/".join(
        str(scope.get(key) or "*")
        for key in ("ticker", "side", "horizon_class", "market_regime")
    )
    parts = []
    if authority:
        parts.append(f"authority={authority}")
    if scope_bits:
        parts.append(f"scope={scope_bits}")
    if usable:
        parts.append(f"use={usable}")
    if analyst:
        parts.append(f"analyst={analyst}")
    if pm:
        parts.append(f"pm={pm}")
    if position:
        parts.append(f"position={position}")
    if boundary:
        parts.append(f"boundary={boundary}")
    if not parts:
        return ""
    return _compact_text("Next-round strategy update: " + " | ".join(parts), max_chars)


