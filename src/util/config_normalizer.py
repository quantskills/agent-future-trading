from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

import yaml


ANALYST_KEYS = ("technical", "fundamental", "commodity_news")


def normalize_config(config: Mapping[str, Any] | None, config_path: str | Path | None = None) -> Dict[str, Any]:
    """Expand compact config catalogs into the legacy runtime config shape.

    `dev.yaml` is allowed to stay compact, but runtime code still expects keys
    such as `execution.commission` and `portfolio_manager.sector_weights`.
    This function restores those keys deterministically and validates hard
    business parameters instead of using silent defaults.
    """
    cfg: Dict[str, Any] = deepcopy(dict(config or {}))
    base_path = Path(config_path).resolve() if config_path else None
    catalogs = cfg.get("config_catalogs") or {}
    if not isinstance(catalogs, Mapping):
        raise ValueError("config_catalogs must be a mapping when provided")

    loaded_catalogs: Dict[str, str] = {}
    _apply_analyst_weight_catalog(cfg, catalogs, base_path, loaded_catalogs)
    _apply_data_factor_policy_catalog(cfg, catalogs, base_path, loaded_catalogs)
    _apply_product_price_behavior_profiles(cfg, catalogs, base_path, loaded_catalogs)
    _apply_evidence_fusion_policy_catalog(cfg, catalogs, base_path, loaded_catalogs)
    _apply_portfolio_policy_catalog(cfg, catalogs, base_path, loaded_catalogs)
    _apply_learning_policy_catalog(cfg, catalogs, base_path, loaded_catalogs)
    _apply_rank_score_policy_catalog(cfg, catalogs, base_path, loaded_catalogs)
    _apply_execution_catalogs(cfg, catalogs, base_path, loaded_catalogs)
    _validate_position_budget_policy(cfg)

    if loaded_catalogs:
        cfg["_config_catalogs_loaded"] = loaded_catalogs
    return cfg


def _apply_analyst_weight_catalog(
    cfg: Dict[str, Any],
    catalogs: Mapping[str, Any],
    base_path: Path | None,
    loaded_catalogs: Dict[str, str],
) -> None:
    ref = catalogs.get("analyst_prior_profiles") or catalogs.get("analyst_weight_profiles")
    if not ref:
        return
    payload = _load_catalog(ref, base_path)
    loaded_catalogs["analyst_prior_profiles"] = str(_resolve_catalog_path(ref, base_path))
    if catalogs.get("analyst_weight_profiles") and not catalogs.get("analyst_prior_profiles"):
        loaded_catalogs["analyst_weight_profiles_deprecated_alias"] = str(_resolve_catalog_path(ref, base_path))
    profiles = payload.get("profiles")
    if not isinstance(profiles, Mapping):
        raise ValueError("analyst prior catalog must contain profiles")

    policy = cfg.get("analyst_weight_policy") or {}
    if not isinstance(policy, Mapping):
        raise ValueError("analyst_weight_policy must be a mapping when provided")
    strategic_name = str(policy.get("strategic_profile") or "sector_strategic_view")
    trade_timing_name = str(policy.get("trade_timing_profile") or "daily_trade_timing")

    strategic_profile = _profile_weights(profiles, strategic_name, "strategic_profile")
    trade_profile = _profile_weights(profiles, trade_timing_name, "trade_timing_profile")

    pm_cfg = cfg.setdefault("portfolio_manager", {})
    if not isinstance(pm_cfg, dict):
        raise ValueError("portfolio_manager config must be a mapping")

    # PM's existing fusion path reads `sector_weights`; keep that legacy shape,
    # but the catalog is only a cold-start prior. Final trade authority is
    # decided by action evidence in PM, not by static analyst weights.
    pm_cfg["sector_weights"] = trade_profile
    pm_cfg["strategic_view_weights"] = strategic_profile
    usage_rules = payload.get("usage_rules")
    if isinstance(usage_rules, Mapping):
        policy_cfg = cfg.setdefault("analyst_weight_policy", {})
        if isinstance(policy_cfg, dict):
            policy_cfg["catalog_usage_rules"] = dict(usage_rules)
            policy_cfg.setdefault("mode", "evidence_router")
            policy_cfg.setdefault("static_weights_mode", "prior_only")
            policy_cfg.setdefault("static_weights_can_create_trade_authority", False)
            policy_cfg.setdefault("allow_static_weights_to_open", False)

    applicability_profile = payload.get("applicability_profile")
    if isinstance(applicability_profile, Mapping) and "analyst_applicability_profile" not in cfg:
        cfg["analyst_applicability_profile"] = deepcopy(dict(applicability_profile))

    dynamic_cfg = cfg.setdefault("dynamic_weights", {})
    if isinstance(dynamic_cfg, dict):
        bounds = payload.get("dynamic_bounds") or {}
        if isinstance(bounds, Mapping):
            dynamic_cfg.setdefault("min_weight", bounds.get("min_weight", 0.05))
            dynamic_cfg.setdefault("max_weight", bounds.get("max_weight", 0.70))

    cfg.setdefault("_config_parameter_roles", {})
    roles = cfg["_config_parameter_roles"]
    if isinstance(roles, dict):
        roles["portfolio_manager.sector_weights"] = "cold_start_trade_timing_prior_only_not_trade_authority"
        roles["portfolio_manager.strategic_view_weights"] = "cold_start_direction_context_prior_only_not_trade_authority"
        roles["analyst_applicability_profile"] = "cold_start_applicability_prior_only_not_trade_authority"
        roles["dynamic_weights"] = "runtime_adjustment_plus_database_learning_overlay"
        roles["analyst_weight_policy"] = "action_evidence_router_controls_trade_authority"


def _apply_data_factor_policy_catalog(
    cfg: Dict[str, Any],
    catalogs: Mapping[str, Any],
    base_path: Path | None,
    loaded_catalogs: Dict[str, str],
) -> None:
    ref = catalogs.get("data_factor_policy")
    if not ref:
        return
    payload = _load_catalog(ref, base_path)
    loaded_catalogs["data_factor_policy"] = str(_resolve_catalog_path(ref, base_path))

    for key in ("fundamental_quality_control", "pandaai_extra_data", "factor_data"):
        section = payload.get(key)
        if isinstance(section, Mapping):
            existing = cfg.get(key) if isinstance(cfg.get(key), Mapping) else {}
            cfg[key] = _deep_merge(dict(section), dict(existing or {}))

    cfg.setdefault("_config_parameter_roles", {})
    roles = cfg["_config_parameter_roles"]
    if isinstance(roles, dict):
        roles["fundamental_quality_control"] = "data_factor_policy_catalog_runtime_expanded"
        roles["pandaai_extra_data"] = "data_factor_policy_catalog_runtime_expanded"
        roles["factor_data"] = "data_factor_policy_catalog_runtime_expanded"


def _apply_product_price_behavior_profiles(
    cfg: Dict[str, Any],
    catalogs: Mapping[str, Any],
    base_path: Path | None,
    loaded_catalogs: Dict[str, str],
) -> None:
    ref = catalogs.get("product_price_behavior_profiles")
    if not ref:
        return
    payload = _load_catalog(ref, base_path)
    profiles = payload.get("profiles")
    if not isinstance(profiles, Mapping):
        raise ValueError("product price behavior profile catalog must contain profiles")
    cfg["product_price_behavior_profiles"] = payload
    loaded_catalogs["product_price_behavior_profiles"] = str(_resolve_catalog_path(ref, base_path))
    cfg.setdefault("_config_parameter_roles", {})
    roles = cfg["_config_parameter_roles"]
    if isinstance(roles, dict):
        roles["product_price_behavior_profiles"] = (
            "cold_start_analyst_differentiation_profile_only_not_trade_authority"
        )


def _apply_evidence_fusion_policy_catalog(
    cfg: Dict[str, Any],
    catalogs: Mapping[str, Any],
    base_path: Path | None,
    loaded_catalogs: Dict[str, str],
) -> None:
    ref = catalogs.get("evidence_fusion_policy")
    if not ref:
        return
    payload = _load_catalog(ref, base_path)
    cfg["evidence_fusion_policy"] = payload
    loaded_catalogs["evidence_fusion_policy"] = str(_resolve_catalog_path(ref, base_path))
    cfg.setdefault("_config_parameter_roles", {})
    roles = cfg["_config_parameter_roles"]
    if isinstance(roles, dict):
        roles["evidence_fusion_policy"] = (
            "multidimensional_prediction_evidence_fusion_only_not_trade_authority"
        )


def _apply_portfolio_policy_catalog(
    cfg: Dict[str, Any],
    catalogs: Mapping[str, Any],
    base_path: Path | None,
    loaded_catalogs: Dict[str, str],
) -> None:
    ref = catalogs.get("portfolio_policy")
    if not ref:
        return
    payload = _load_catalog(ref, base_path)
    loaded_catalogs["portfolio_policy"] = str(_resolve_catalog_path(ref, base_path))

    for key in (
        "market_confirmation",
        "directional_override_control",
        "portfolio_manager",
        "pm_risk_gate",
        "auditor",
        "trade_frequency_control",
        "ticker_performance_control",
        "ticker_loss_control",
        "dynamic_weights",
    ):
        section = payload.get(key)
        if isinstance(section, Mapping):
            existing = cfg.get(key) if isinstance(cfg.get(key), Mapping) else {}
            cfg[key] = _deep_merge(dict(section), dict(existing or {}))

    cfg.setdefault("_config_parameter_roles", {})
    roles = cfg["_config_parameter_roles"]
    if isinstance(roles, dict):
        roles["market_confirmation"] = "portfolio_policy_catalog_runtime_expanded"
        roles["directional_override_control"] = "portfolio_policy_catalog_runtime_expanded"
        roles["portfolio_manager"] = "portfolio_policy_catalog_runtime_expanded"
        roles["pm_risk_gate"] = "portfolio_policy_catalog_runtime_expanded"
        roles["auditor"] = "portfolio_policy_catalog_runtime_expanded"
        roles["trade_frequency_control"] = "portfolio_policy_catalog_runtime_expanded"
        roles["ticker_performance_control"] = "portfolio_policy_catalog_runtime_expanded"
        roles["ticker_loss_control"] = "portfolio_policy_catalog_runtime_expanded"
        roles["dynamic_weights"] = "portfolio_policy_catalog_runtime_expanded"


def _apply_learning_policy_catalog(
    cfg: Dict[str, Any],
    catalogs: Mapping[str, Any],
    base_path: Path | None,
    loaded_catalogs: Dict[str, str],
) -> None:
    ref = catalogs.get("learning_policy")
    if not ref:
        return
    payload = _load_catalog(ref, base_path)
    loaded_catalogs["learning_policy"] = str(_resolve_catalog_path(ref, base_path))

    for key in (
        "strategy_memory",
        "learning",
        "analyst_business_quality",
        "signal_quality",
        "learning_context",
        "learning_retention",
        "opportunity_ranking_learning_policy",
    ):
        section = payload.get(key)
        if isinstance(section, Mapping):
            existing = cfg.get(key) if isinstance(cfg.get(key), Mapping) else {}
            cfg[key] = _deep_merge(dict(section), dict(existing or {}))

    cfg.setdefault("_config_parameter_roles", {})
    roles = cfg["_config_parameter_roles"]
    if isinstance(roles, dict):
        if isinstance(payload.get("learning_gatekeeping_policy"), Mapping):
            roles["learning_policy.learning_gatekeeping_policy"] = "learning_action_preference_semantics_not_trade_authority"
        roles["strategy_memory"] = "learning_policy_catalog_runtime_expanded"
        roles["learning"] = "learning_policy_catalog_runtime_expanded"
        roles["analyst_business_quality"] = "learning_policy_catalog_runtime_expanded"
        roles["signal_quality"] = "learning_policy_catalog_runtime_expanded"
        roles["learning_context"] = "learning_policy_catalog_runtime_expanded"
        roles["learning_retention"] = "learning_policy_catalog_runtime_expanded"
        roles["opportunity_ranking_learning_policy"] = "learning_policy_catalog_runtime_expanded"


def _apply_rank_score_policy_catalog(
    cfg: Dict[str, Any],
    catalogs: Mapping[str, Any],
    base_path: Path | None,
    loaded_catalogs: Dict[str, str],
) -> None:
    ref = catalogs.get("rank_score_policy")
    if not ref:
        return
    payload = _load_catalog(ref, base_path)
    loaded_catalogs["rank_score_policy"] = str(_resolve_catalog_path(ref, base_path))
    policy = payload.get("rank_score_policy")
    if not isinstance(policy, Mapping):
        raise ValueError("rank score policy catalog must contain rank_score_policy")
    existing = cfg.get("rank_score_policy") if isinstance(cfg.get("rank_score_policy"), Mapping) else {}
    cfg["rank_score_policy"] = _deep_merge(dict(policy), dict(existing or {}))

    cfg.setdefault("_config_parameter_roles", {})
    roles = cfg["_config_parameter_roles"]
    if isinstance(roles, dict):
        roles["rank_score_policy"] = (
            "full_market_rank_score_weight_catalog_not_trade_authority_not_position_size"
        )


def _apply_execution_catalogs(
    cfg: Dict[str, Any],
    catalogs: Mapping[str, Any],
    base_path: Path | None,
    loaded_catalogs: Dict[str, str],
) -> None:
    execution_cfg = cfg.setdefault("execution", {})
    if not isinstance(execution_cfg, dict):
        raise ValueError("execution config must be a mapping")

    commission_ref = catalogs.get("execution_commission")
    if commission_ref:
        payload = _load_catalog(commission_ref, base_path)
        loaded_catalogs["execution_commission"] = str(_resolve_catalog_path(commission_ref, base_path))
        commission = payload.get("commission")
        if not isinstance(commission, Mapping):
            raise ValueError("execution commission catalog must contain commission")
        existing = execution_cfg.get("commission") if isinstance(execution_cfg.get("commission"), Mapping) else {}
        execution_cfg["commission"] = _deep_merge(dict(commission), dict(existing or {}))
        _validate_explicit_underlying_map(
            cfg.get("tickers") or [],
            execution_cfg["commission"].get("by_underlying") or {},
            "commission",
            required=bool(execution_cfg["commission"].get("require_explicit_rule_for_all_tickers", False)),
        )

    slippage_ref = catalogs.get("execution_slippage")
    if slippage_ref:
        payload = _load_catalog(slippage_ref, base_path)
        loaded_catalogs["execution_slippage"] = str(_resolve_catalog_path(slippage_ref, base_path))
        slippage = payload.get("slippage")
        if not isinstance(slippage, Mapping):
            raise ValueError("execution slippage catalog must contain slippage")
        inline_ticks = execution_cfg.get("slippage_ticks_by_underlying")
        for key in ("slippage_model", "default_slippage_ticks"):
            execution_cfg.setdefault(key, slippage.get(key))
        ticks = dict(slippage.get("slippage_ticks_by_underlying") or {})
        if isinstance(inline_ticks, Mapping):
            ticks.update(inline_ticks)
        execution_cfg["slippage_ticks_by_underlying"] = ticks
        _validate_explicit_underlying_map(
            cfg.get("tickers") or [],
            ticks,
            "slippage_ticks_by_underlying",
            required=bool(slippage.get("require_explicit_rule_for_all_tickers", True)),
        )

    exit_ref = catalogs.get("execution_exit_policy")
    if exit_ref:
        payload = _load_catalog(exit_ref, base_path)
        loaded_catalogs["execution_exit_policy"] = str(_resolve_catalog_path(exit_ref, base_path))
        exit_policy = payload.get("exit_policy")
        if not isinstance(exit_policy, Mapping):
            raise ValueError("execution exit policy catalog must contain exit_policy")
        existing_exit = execution_cfg.get("exit_policy") if isinstance(execution_cfg.get("exit_policy"), Mapping) else {}
        execution_cfg["exit_policy"] = _deep_merge(dict(exit_policy), dict(existing_exit or {}))

    cfg.setdefault("_config_parameter_roles", {})
    roles = cfg["_config_parameter_roles"]
    if isinstance(roles, dict):
        roles["execution.commission"] = "manual_hard_cost_catalog_not_learned"
        roles["execution.slippage_ticks_by_underlying"] = "manual_execution_cost_catalog_not_learned"
        roles["execution.exit_policy"] = "manual_business_default_catalog_with_learning_feedback"


def _profile_weights(profiles: Mapping[str, Any], profile_name: str, role: str) -> Dict[str, Dict[str, float]]:
    profile = profiles.get(profile_name)
    if not isinstance(profile, Mapping):
        raise ValueError(f"analyst weight catalog missing {role}: {profile_name}")
    sector_weights = profile.get("sector_weights")
    if not isinstance(sector_weights, Mapping):
        raise ValueError(f"analyst weight profile {profile_name} must contain sector_weights")
    return {
        str(sector): _normalize_weight_row(weights, f"{profile_name}.{sector}")
        for sector, weights in sector_weights.items()
    }


def _normalize_weight_row(weights: Any, label: str) -> Dict[str, float]:
    if not isinstance(weights, Mapping):
        raise ValueError(f"analyst weight row {label} must be a mapping")
    row = {key: float(weights.get(key, 0.0) or 0.0) for key in ANALYST_KEYS}
    if any(value < 0 for value in row.values()):
        raise ValueError(f"analyst weight row {label} cannot contain negative weights")
    total = sum(row.values())
    if total <= 0:
        raise ValueError(f"analyst weight row {label} must have positive total weight")
    return {key: value / total for key, value in row.items()}


def _validate_explicit_underlying_map(
    tickers: Iterable[Any],
    mapping: Mapping[str, Any],
    label: str,
    *,
    required: bool,
) -> None:
    if not required:
        return
    configured = {str(key).upper() for key in mapping.keys()}
    missing = [str(ticker).upper() for ticker in tickers if str(ticker).upper() not in configured]
    if missing:
        raise RuntimeError(f"{label} catalog missing explicit rules for tickers: {missing}")


def _validate_position_budget_policy(cfg: Dict[str, Any]) -> None:
    policy = cfg.get("position_budget_policy") or {}
    if not isinstance(policy, Mapping):
        raise ValueError("position_budget_policy must be a mapping when provided")
    max_total = float(cfg.get("max_total_margin_ratio") or 0.20)
    hard_max = float(policy.get("hard_max_total_margin_ratio") or max_total)
    if hard_max - max_total > 1e-12:
        raise ValueError("position_budget_policy.hard_max_total_margin_ratio cannot exceed max_total_margin_ratio")
    min_ratio = float(policy.get("min_real_trade_margin_ratio") or 0.0)
    max_ratio = float(policy.get("hard_max_total_margin_ratio") or max_total)
    if min_ratio < 0 or max_ratio <= 0:
        raise ValueError("position_budget_policy margin ratios must be non-negative and have positive hard max")
    if min_ratio >= max_ratio:
        raise ValueError("position_budget_policy.min_real_trade_margin_ratio must be below hard max")


def _load_catalog(ref: Any, base_path: Path | None) -> Dict[str, Any]:
    path = _resolve_catalog_path(ref, base_path)
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Catalog must be a YAML mapping: {path}")
    return payload


def _resolve_catalog_path(ref: Any, base_path: Path | None) -> Path:
    path = Path(str(ref))
    if path.is_absolute():
        if path.exists():
            return path
        raise FileNotFoundError(f"Catalog file not found: {path}")

    candidates = []
    if base_path is not None:
        config_dir = base_path.parent
        src_root = config_dir.parent
        project_root = src_root.parent
        candidates.extend([config_dir / path, src_root / path, project_root / path])
    candidates.append(Path.cwd() / path)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"Catalog file not found: {ref}")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
