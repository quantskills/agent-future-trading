from __future__ import annotations

"""Classify what retrieved memory is allowed to do.

This is not a new trading gate. It prevents memory/RAG evidence from being
misrepresented: exact real state can support sizing decisions, while similar
or counterfactual evidence remains a prior.
"""

from typing import Any, Dict

from tools.agent_tools.control.schemas import MEMORY_QUALITY_LEVELS


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def classify_memory_quality(memory_payload: Dict[str, Any]) -> str:
    if not isinstance(memory_payload, dict):
        return "unqualified"

    if str(memory_payload.get("no_lookahead_status") or "").lower() == "violation":
        return "stale_or_conflicted_memory"
    if _truthy(memory_payload.get("is_stale")) or _truthy(memory_payload.get("conflicted_memory")):
        return "stale_or_conflicted_memory"
    if _truthy(memory_payload.get("counterfactual_prior_only")) or str(memory_payload.get("reward_source") or "") == "counterfactual":
        return "counterfactual_prior"

    explicit = str(
        memory_payload.get("amplification_scope_quality")
        or memory_payload.get("memory_quality")
        or memory_payload.get("scope_quality")
        or ""
    )
    if explicit in MEMORY_QUALITY_LEVELS:
        return explicit

    # Legacy payloads without explicit scope quality must not become exact alpha.
    return "unqualified"


def allowed_memory_uses(memory_quality: str) -> Dict[str, bool]:
    quality = memory_quality if memory_quality in MEMORY_QUALITY_LEVELS else "unqualified"
    return {
        "can_support_real_budget_entry": quality == "exact_real_state",
        "can_support_scale": quality == "exact_real_state",
        "can_support_probe": quality in {"exact_real_state", "partial_real_state", "similar_sql_prior"},
        "can_inform_analysis": quality
        in {"exact_real_state", "partial_real_state", "similar_sql_prior", "counterfactual_prior"},
        "audit_only": quality in {"stale_or_conflicted_memory", "unqualified"},
    }


def classify_memory_payload(memory_payload: Dict[str, Any]) -> Dict[str, Any]:
    quality = classify_memory_quality(memory_payload)
    return {"memory_quality": quality, "allowed_uses": allowed_memory_uses(quality)}


