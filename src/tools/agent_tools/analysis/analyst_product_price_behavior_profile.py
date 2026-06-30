from __future__ import annotations

"""Commodity-specific analysis frame for the three analyst agents.

The profile is a cold-start analysis frame. It does not call an LLM, read the
database, sign a contract, size positions, audit trades, execute orders, or
write accounting/research facts.
"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml

from tools.common.final_action_semantics import FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS


PROFILE_CONTRACT_VERSION = "agentquant.analyst_product_price_behavior_profile.v1"
PRODUCT_PROFILE_VERSION = "agentquant.product_price_behavior.v1"
PROFILE_ANALYSTS = {"technical", "fundamental", "commodity_news"}
REQUIRED_PROFILE_FIELDS = {
    "product_profile_version",
    "ticker",
    "sector",
    "product_context",
    "price_behavior",
    "trend_inertia",
    "volatility_profile",
    "false_breakout_risk",
    "preferred_setups",
    "caution_setups",
    "confirmation_requirements",
    "fundamental_driver_priority",
    "news_catalyst_priority",
    "seasonal_event_window",
    "invalid_profile_use",
}
LIST_PROFILE_FIELDS = {
    "preferred_setups",
    "caution_setups",
    "confirmation_requirements",
    "fundamental_driver_priority",
    "news_catalyst_priority",
    "seasonal_event_window",
    "invalid_profile_use",
}


def _default_profile_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "product_price_behavior_profiles.yaml"


def _resolve_profile_path(full_config: Mapping[str, Any] | None = None) -> Path:
    cfg = full_config if isinstance(full_config, Mapping) else {}
    explicit = cfg.get("product_price_behavior_profiles_path") or cfg.get("analyst_product_price_behavior_profiles_path")
    catalogs = cfg.get("config_catalogs") if isinstance(cfg.get("config_catalogs"), Mapping) else {}
    ref = explicit or catalogs.get("product_price_behavior_profiles")
    if not ref:
        return _default_profile_path()
    path = Path(str(ref))
    if path.is_absolute():
        return path
    config_dir = _default_profile_path().parent
    for candidate in (config_dir / path, config_dir.parent / path, config_dir.parent.parent / path, Path.cwd() / path):
        if candidate.exists():
            return candidate.resolve()
    return (config_dir / path).resolve()


def _walk_forbidden_trade_fields(value: Any, *, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in FORBIDDEN_ANALYST_TRADE_AUTHORITY_KEYS:
                hits.append(child_path)
            hits.extend(_walk_forbidden_trade_fields(child, path=child_path))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            hits.extend(_walk_forbidden_trade_fields(child, path=f"{path}[{idx}]"))
    return hits


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(value)]


def load_product_price_behavior_profiles(full_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Load and validate the product price-behavior profile catalog."""
    cfg = full_config if isinstance(full_config, Mapping) else {}
    embedded = cfg.get("product_price_behavior_profiles")
    if isinstance(embedded, Mapping) and isinstance(embedded.get("profiles"), Mapping):
        payload = deepcopy(dict(embedded))
    else:
        path = _resolve_profile_path(cfg)
        with path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, Mapping):
            raise ValueError(f"product_price_behavior_profiles must be a YAML mapping: {path}")
        payload = dict(loaded)
        payload["_catalog_path"] = str(path)

    if payload.get("profile_contract_version") != PROFILE_CONTRACT_VERSION:
        raise ValueError("product_price_behavior_profiles.profile_contract_version mismatch")
    profiles = payload.get("profiles")
    if not isinstance(profiles, Mapping):
        raise ValueError("product_price_behavior_profiles.profiles must be a mapping")
    required_tickers = [str(item).upper() for item in _as_list(payload.get("required_tickers"))]
    missing_required = sorted(ticker for ticker in required_tickers if ticker not in profiles)
    if missing_required:
        raise ValueError(f"product_price_behavior_profiles missing required tickers: {missing_required}")
    for ticker, profile in profiles.items():
        validate_product_price_behavior_profile(str(ticker), profile)
    return payload


def get_product_price_behavior_profile(ticker: str, full_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return one validated ticker profile."""
    ticker_key = str(ticker or "").upper()
    profiles = load_product_price_behavior_profiles(full_config)
    profile = (profiles.get("profiles") or {}).get(ticker_key)
    if not isinstance(profile, Mapping):
        raise KeyError(f"product_price_behavior_profile missing ticker: {ticker_key}")
    return deepcopy(dict(profile))


def assert_profile_has_no_trade_authority(profile: Mapping[str, Any] | None) -> None:
    """Raise if a profile contains PM/Auditor/Trader authority fields."""
    hits = _walk_forbidden_trade_fields(profile if isinstance(profile, Mapping) else {})
    if hits:
        raise ValueError(f"product_price_behavior_profile_forbidden_trade_authority_fields:{sorted(set(hits))}")


def validate_product_price_behavior_profile(ticker: str, profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate one ticker profile and return a deterministic summary."""
    if not isinstance(profile, Mapping):
        raise ValueError(f"product_price_behavior_profile for {ticker} must be a mapping")
    ticker_key = str(ticker or "").upper()
    declared_ticker = str(profile.get("ticker") or "").upper()
    if declared_ticker != ticker_key:
        raise ValueError(f"product_price_behavior_profile ticker mismatch: {ticker_key} != {declared_ticker}")
    missing = sorted(field for field in REQUIRED_PROFILE_FIELDS if field not in profile)
    if missing:
        raise ValueError(f"product_price_behavior_profile missing fields for {ticker_key}: {missing}")
    if profile.get("product_profile_version") != PRODUCT_PROFILE_VERSION:
        raise ValueError(f"product_price_behavior_profile version mismatch for {ticker_key}")
    wrong_list_fields = sorted(field for field in LIST_PROFILE_FIELDS if not isinstance(profile.get(field), list))
    if wrong_list_fields:
        raise ValueError(f"product_price_behavior_profile list fields invalid for {ticker_key}: {wrong_list_fields}")
    assert_profile_has_no_trade_authority(profile)
    return {
        "contract_version": PROFILE_CONTRACT_VERSION,
        "product_profile_id": f"{PRODUCT_PROFILE_VERSION}:{ticker_key}",
        "product_profile_version": str(profile.get("product_profile_version")),
        "ticker": ticker_key,
        "sector": str(profile.get("sector") or ""),
        "profile_valid": True,
        "profile_analysis_boundary": "analysis_evidence_only_no_trade_authority",
    }


def _format_common_profile(ticker: str, profile: Mapping[str, Any], *, analyst: str) -> str:
    validate_product_price_behavior_profile(ticker, profile)
    return "\n".join(
        [
            "=== Product Price Behavior Profile ===",
            f"Profile contract: {PROFILE_CONTRACT_VERSION}",
            f"Analyst: {analyst}",
            f"Ticker: {str(profile.get('ticker') or '').upper()}",
            f"Sector: {profile.get('sector')}",
            f"Product context: {profile.get('product_context')}",
            f"Price behavior: {profile.get('price_behavior')}",
            f"Trend inertia: {profile.get('trend_inertia')}",
            f"Volatility profile: {profile.get('volatility_profile')}",
            f"False breakout risk: {profile.get('false_breakout_risk')}",
            f"Preferred setups: {', '.join(_as_list(profile.get('preferred_setups')))}",
            f"Caution setups: {', '.join(_as_list(profile.get('caution_setups')))}",
            f"Confirmation requirements: {', '.join(_as_list(profile.get('confirmation_requirements')))}",
            "Boundary: use this cold-start analysis frame only to choose evidence emphasis and confirmation discipline. "
            "It is not trade authority and must not create lots, margin, reason_codes, or final_action_contract.",
        ]
    )


def format_profile_for_technical(ticker: str, profile: Mapping[str, Any]) -> str:
    """Format profile guidance for the technical analyst prompt."""
    base = _format_common_profile(ticker, profile, analyst="technical")
    return (
        base
        + "\nTechnical use: adapt setup choice, trend-inertia threshold, volatility caution, false-breakout discipline, "
        "price/volume confirmation, and invalidation expectation to this ticker profile."
    )


def format_profile_for_fundamental(ticker: str, profile: Mapping[str, Any]) -> str:
    """Format profile guidance for the fundamental analyst prompt."""
    base = _format_common_profile(ticker, profile, analyst="fundamental")
    return (
        base
        + "\nFundamental use: prioritize these driver groups: "
        + ", ".join(_as_list(profile.get("fundamental_driver_priority")))
        + ". Check seasonal windows and confirmation requirements before marking a thesis tradeable."
    )


def format_profile_for_commodity_news(ticker: str, profile: Mapping[str, Any]) -> str:
    """Format profile guidance for the commodity-news analyst prompt."""
    base = _format_common_profile(ticker, profile, analyst="commodity_news")
    return (
        base
        + "\nNews use: prioritize these catalysts: "
        + ", ".join(_as_list(profile.get("news_catalyst_priority")))
        + ". Treat profile-invalid uses as noise unless current price, volume, and product-chain evidence confirm them."
    )


def build_profile_usage_contract(ticker: str, analyst: str, profile: Mapping[str, Any]) -> dict[str, Any]:
    """Build the profile evidence trace carried by analyst metadata."""
    analyst_key = str(analyst or "").strip()
    if analyst_key not in PROFILE_ANALYSTS:
        raise ValueError(f"product_price_behavior_profile unsupported analyst: {analyst}")
    validation = validate_product_price_behavior_profile(ticker, profile)
    profile_fields_used = [
        "sector",
        "product_context",
        "price_behavior",
        "trend_inertia",
        "volatility_profile",
        "false_breakout_risk",
        "preferred_setups",
        "caution_setups",
        "confirmation_requirements",
    ]
    if analyst_key == "fundamental":
        profile_fields_used.extend(["fundamental_driver_priority", "seasonal_event_window"])
    if analyst_key == "commodity_news":
        profile_fields_used.extend(["news_catalyst_priority", "seasonal_event_window", "invalid_profile_use"])
    return {
        **validation,
        "analyst": analyst_key,
        "product_profile_used": True,
        "profile_role": "cold_start_analysis_frame",
        "profile_fields_used": profile_fields_used,
        "profile_supported_evidence": [],
        "profile_conflicting_evidence": [],
        "profile_missing_evidence": [],
        "profile_assumption_status": "profile_loaded_current_evidence_required",
        "profile_relevance_score": 1.0,
        "profile_learning_interaction": "static_profile_plus_dynamic_learning_context",
        "profile_invalid_use_flags": [],
        "confirmation_requirements": _as_list(profile.get("confirmation_requirements")),
        "preferred_setups": _as_list(profile.get("preferred_setups")),
        "caution_setups": _as_list(profile.get("caution_setups")),
        "fundamental_driver_priority": _as_list(profile.get("fundamental_driver_priority")),
        "news_catalyst_priority": _as_list(profile.get("news_catalyst_priority")),
        "seasonal_event_window": _as_list(profile.get("seasonal_event_window")),
        "invalid_profile_use": _as_list(profile.get("invalid_profile_use")),
    }


def apply_profile_usage_to_signal(signal: Any, usage_contract: Mapping[str, Any]) -> Any:
    """Attach profile usage evidence to an AnalystSignal-like object."""
    usage = dict(usage_contract)
    metadata = dict(getattr(signal, "metadata", {}) or {})
    metadata["product_profile_evidence"] = usage
    action_contract = dict(metadata.get("action_evidence_contract") or {})
    learning_scope = dict(action_contract.get("learning_scope") or metadata.get("learning_scope") or {})
    learning_scope.update(
        {
            "product_profile_id": usage.get("product_profile_id"),
            "product_profile_version": usage.get("product_profile_version"),
            "product_profile_used": True,
            "product_profile_fields_used": list(usage.get("profile_fields_used") or []),
            "product_profile_learning_interaction": usage.get("profile_learning_interaction"),
            "product_profile_analysis_boundary": usage.get("profile_analysis_boundary"),
        }
    )
    action_contract["learning_scope"] = learning_scope
    action_contract["product_profile_evidence"] = usage
    metadata["action_evidence_contract"] = action_contract
    metadata["learning_scope"] = learning_scope
    strategy_trace = dict(metadata.get("analysis_strategy_trace") or {})
    strategy_trace["product_profile_evidence"] = {
        "product_profile_id": usage.get("product_profile_id"),
        "profile_role": usage.get("profile_role"),
        "profile_learning_interaction": usage.get("profile_learning_interaction"),
        "profile_analysis_boundary": usage.get("profile_analysis_boundary"),
    }
    metadata["analysis_strategy_trace"] = strategy_trace
    signal.metadata = metadata
    focus = list(getattr(signal, "factor_focus", []) or [])
    profile_focus = f"product_profile:{usage.get('ticker')}"
    if profile_focus not in focus:
        focus.append(profile_focus)
    signal.factor_focus = focus
    return signal
