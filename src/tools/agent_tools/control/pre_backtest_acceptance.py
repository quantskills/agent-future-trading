from __future__ import annotations

"""Frozen pre-backtest acceptance checks.

This module is an orchestration layer for control-team checks. It does not
decide trades, change sizing, write learning records, or evaluate strategy
profitability. Its job is to answer one question before an expensive backtest:
is the system chain ready enough that the next run is a strategy PnL test rather
than another bug-discovery pass?
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from apis.router import APISource, Router
from tools.agent_tools.control.agent_cards import build_default_agent_cards, validate_agent_capability
from tools.agent_tools.control.preflight import run_preflight_checks
from tools.agent_tools.control.schemas import ProtocolCheckResult
from tools.agent_tools.control.system_invariants import audit_system_invariants
from tools.agent_tools.control.tool_access_policy import (
    build_default_tool_access_policy,
    validate_tool_policy_against_capabilities,
)
from tools.agent_tools.control.unified_field_audit import (
    FORBIDDEN_RUNTIME_FIELD_TOKENS,
    scan_runtime_field_usage,
)
from util.config_normalizer import normalize_config


ACCEPTANCE_CHECKS = (
    "environment_api",
    "config_consistency",
    "data_time_boundary",
    "agent_boundaries",
    "structured_io",
    "single_trade_exit",
    "trader_trigger_parity",
    "learning_landing",
    "capital_boundary",
    "audit_explainability",
)

INVARIANT_TO_CHECK = {
    "open_transaction_without_open_final_action": "single_trade_exit",
    "open_transaction_without_open_authority": "single_trade_exit",
    "open_transaction_with_blocking_authority": "single_trade_exit",
    "real_open_without_current_contract_evidence": "single_trade_exit",
    "direction_or_watchlist_probe_opened": "single_trade_exit",
    "trade_contract_source_of_truth_failed": "single_trade_exit",
    "open_transaction_without_trigger": "trader_trigger_parity",
    "open_transaction_without_intraday_trigger": "trader_trigger_parity",
    "intraday_trigger_audit_mirror_mismatch": "trader_trigger_parity",
    "open_transaction_without_intraday_record": "trader_trigger_parity",
    "action_value_missing_action_preference": "learning_landing",
    "action_value_unknown_action_preference": "learning_landing",
    "positive_open_action_value_not_open_preference": "learning_landing",
    "positive_exit_action_value_not_exit_preference": "learning_landing",
    "negative_action_value_not_protective_preference": "learning_landing",
    "positive_open_from_non_exact_scope": "learning_landing",
    "positive_open_from_non_real_reward_source": "learning_landing",
    "action_preferences_exist_but_no_final_action_contract_mentions_them": "learning_landing",
    "unified_field_artifact_forbidden_field": "structured_io",
    "incomplete_trading_day_phase": "data_time_boundary",
}


@dataclass
class AcceptanceCheck:
    name: str
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }


@dataclass
class PreBacktestAcceptanceReport:
    ok: bool
    checks: Dict[str, AcceptanceCheck]
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    agent_name: str = "protocol_governor"
    contract_version: str = "agentquant.pre_backtest_acceptance.v1"

    @property
    def failed_checks(self) -> List[str]:
        return [name for name, check in self.checks.items() if not check.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "contract_version": self.contract_version,
            "ok": self.ok,
            "failed_checks": self.failed_checks,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "checks": {name: check.to_dict() for name, check in self.checks.items()},
            "metadata": dict(self.metadata),
        }


def _load_config(config_path: Path) -> Dict[str, Any]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return normalize_config(raw, config_path)


def _check_from_result(
    name: str,
    result: ProtocolCheckResult,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> AcceptanceCheck:
    merged_metadata = dict(result.metadata)
    merged_metadata.update(metadata or {})
    return AcceptanceCheck(
        name=name,
        ok=result.ok,
        errors=list(result.errors),
        warnings=list(result.warnings),
        metadata=merged_metadata,
    )


def _pass_check(name: str, *, warnings: Iterable[str] = (), metadata: Optional[Dict[str, Any]] = None) -> AcceptanceCheck:
    return AcceptanceCheck(name=name, ok=True, warnings=list(warnings), metadata=dict(metadata or {}))


def _fail_check(name: str, errors: Iterable[str], *, warnings: Iterable[str] = (), metadata: Optional[Dict[str, Any]] = None) -> AcceptanceCheck:
    return AcceptanceCheck(name=name, ok=False, errors=list(errors), warnings=list(warnings), metadata=dict(metadata or {}))


def _merge_acceptance_checks(name: str, *checks: AcceptanceCheck) -> AcceptanceCheck:
    errors: List[str] = []
    warnings: List[str] = []
    metadata: Dict[str, Any] = {}
    for check in checks:
        errors.extend(check.errors)
        warnings.extend(check.warnings)
        metadata.update(check.metadata)
    return AcceptanceCheck(
        name=name,
        ok=not errors,
        errors=errors,
        warnings=warnings,
        metadata=metadata,
    )


def _config_consistency_check(cfg: Dict[str, Any]) -> AcceptanceCheck:
    errors: List[str] = []
    warnings: List[str] = []
    metadata: Dict[str, Any] = {}

    llm_cfg = cfg.get("llm") or {}
    provider = str(llm_cfg.get("provider") or "")
    model = str(llm_cfg.get("model") or "")
    metadata["llm_provider"] = provider
    metadata["llm_model"] = model
    if provider != "CodexOpenAI":
        errors.append(f"active_llm_provider_not_codex:{provider}")
    if model != "gpt-5.5":
        errors.append(f"active_llm_model_not_gpt_5_5:{model}")
    if "deepseek" in llm_cfg:
        errors.append("runtime_deepseek_block_not_allowed_in_dev_config")
    if "tqxai" in llm_cfg:
        errors.append("backup_tqxai_block_must_be_commented_not_active")

    position_budget = cfg.get("position_budget_policy") or {}
    try:
        probe_floor = float(position_budget.get("probe_margin_ratio"))
        probe_cap = float(position_budget.get("probe_margin_max_ratio"))
        hard_cap = float(cfg.get("max_total_margin_ratio"))
    except Exception:
        errors.append("position_budget_parameters_unreadable")
    else:
        metadata.update(
            {
                "probe_margin_ratio": probe_floor,
                "probe_margin_max_ratio": probe_cap,
                "max_total_margin_ratio": hard_cap,
            }
        )
        if probe_floor != 0.008:
            errors.append(f"probe_margin_ratio_drift:{probe_floor}")
        if probe_cap != 0.015:
            errors.append(f"probe_margin_max_ratio_drift:{probe_cap}")
        if hard_cap != 0.20:
            errors.append(f"max_total_margin_ratio_drift:{hard_cap}")
        if probe_floor > probe_cap:
            errors.append("probe_floor_above_probe_cap")

    governance = cfg.get("control_governance") or {}
    protocol = governance.get("protocol_governor") or {}
    if bool(protocol.get("may_create_trade_authority")):
        errors.append("protocol_governor_may_create_trade_authority")
    if bool(protocol.get("may_modify_lots_or_margin")):
        errors.append("protocol_governor_may_modify_lots_or_margin")
    if bool(protocol.get("may_execute_orders")):
        errors.append("protocol_governor_may_execute_orders")

    return _fail_check("config_consistency", errors, warnings=warnings, metadata=metadata) if errors else _pass_check(
        "config_consistency",
        warnings=warnings,
        metadata=metadata,
    )


def _capability_checks() -> tuple[AcceptanceCheck, AcceptanceCheck]:
    cards = build_default_agent_cards()
    result = ProtocolCheckResult.pass_result(metadata={"agent_count": len(cards)})
    for key, card in cards.items():
        result = result.merge(validate_agent_capability(card))
        if key != card.agent_name:
            result = result.merge(ProtocolCheckResult.fail_result([f"capability_card_key_mismatch:{key}"]))
    tool_result = validate_tool_policy_against_capabilities(cards, policy=build_default_tool_access_policy())
    combined = result.merge(tool_result)
    agent_boundaries = _check_from_result(
        "agent_boundaries",
        combined,
        metadata={"boundary": "agents_do_not_cross_create_execute_settle_learn_roles"},
    )
    structured_io = _check_from_result(
        "structured_io",
        result,
        metadata={"boundary": "artifact_contracts_and_capability_cards_present"},
    )
    return agent_boundaries, structured_io


def _runtime_field_unification_check(repo_root: Path) -> AcceptanceCheck:
    offenders, checked_files = scan_runtime_field_usage(repo_root / "src")
    metadata = {
        "unified_field_runtime_scan": {
            "checked_files": checked_files,
            "forbidden_token_count": len(FORBIDDEN_RUNTIME_FIELD_TOKENS),
            "offender_count": len(offenders),
            "allowed_legacy_locations": [
                "src/database/sqlite_setup.py",
                "src/tools/agent_tools/control/unified_field_audit.py",
                "src/tests/test_unified_field_migration.py",
            ],
            "boundary": "production_runtime_must_not_read_or_write_deprecated_semantic_fields",
        }
    }
    if offenders:
        return _fail_check(
            "structured_io",
            [f"runtime_forbidden_field_token:{item}" for item in offenders],
            metadata=metadata,
        )
    return _pass_check("structured_io", metadata=metadata)


def _parse_window_date(value: str, field_name: str, errors: List[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d")
    except Exception:
        errors.append(f"invalid_backtest_date:{field_name}:{value}")
        return None


def _trading_window_check(cfg: Dict[str, Any], start_date: Optional[str], end_date: Optional[str]) -> AcceptanceCheck:
    metadata: Dict[str, Any] = {
        "covered_by": [
            "preflight_artifact_config_presence",
            "system_invariant_learning_dates",
            "runtime_data_cutoff_contracts",
            "trading_day_window_resolution",
        ],
        "strategy_profitability_checked": False,
        "date_window_checked": bool(start_date or end_date),
    }
    errors: List[str] = []

    if not start_date and not end_date:
        return _pass_check("data_time_boundary", metadata=metadata)
    if not start_date or not end_date:
        return _fail_check(
            "data_time_boundary",
            ["backtest_window_requires_start_and_end_date"],
            metadata=metadata,
        )

    parsed_start = _parse_window_date(start_date, "start_date", errors)
    parsed_end = _parse_window_date(end_date, "end_date", errors)
    if errors or parsed_start is None or parsed_end is None:
        return _fail_check("data_time_boundary", errors, metadata=metadata)
    if parsed_end < parsed_start:
        return _fail_check(
            "data_time_boundary",
            [f"backtest_window_end_before_start:{start_date}:{end_date}"],
            metadata=metadata,
        )

    tickers = cfg.get("tickers") or []
    if not tickers:
        return _fail_check("data_time_boundary", ["no_tickers_for_trading_day_check"], metadata=metadata)

    anchor_ticker = str(tickers[0])
    market_type = str(cfg.get("market_type") or "china_futures")
    metadata.update({"anchor_ticker": anchor_ticker, "market_type": market_type})
    if market_type != "china_futures":
        return _fail_check(
            "data_time_boundary",
            [f"unsupported_market_type_for_trading_day_check:{market_type}"],
            metadata=metadata,
        )

    try:
        router = Router(source=APISource.PANDAAI, market_type=market_type)
        quotes = router.api.get_futures_daily_candles_optimized(
            underlying_code=anchor_ticker,
            is_main=1,
            start_date=parsed_start,
            end_date=parsed_end + timedelta(days=1),
        )
    except Exception as exc:
        return _fail_check(
            "data_time_boundary",
            [f"trading_day_window_check_failed:{type(exc).__name__}:{exc}"],
            metadata=metadata,
        )

    trade_days = sorted(
        {
            str(getattr(quote, "trade_date", ""))[:10]
            for quote in (quotes or [])
            if getattr(quote, "trade_date", None)
        }
    )
    trade_days = [day for day in trade_days if start_date <= day <= end_date]
    metadata.update(
        {
            "trading_day_count": len(trade_days),
            "first_trading_day": trade_days[0] if trade_days else None,
            "last_trading_day": trade_days[-1] if trade_days else None,
        }
    )
    if not trade_days:
        return _fail_check(
            "data_time_boundary",
            [f"no_trading_days_in_backtest_window:{start_date}:{end_date}:anchor={anchor_ticker}"],
            metadata=metadata,
        )

    return _pass_check("data_time_boundary", metadata=metadata)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _resolve_config_id_from_db(
    conn: sqlite3.Connection,
    *,
    config_id: Optional[str],
    exp_name: Optional[str],
) -> Optional[str]:
    if config_id:
        return str(config_id)
    if not exp_name or not _table_exists(conn, "config"):
        return None
    row = conn.execute("SELECT id FROM config WHERE exp_name = ?", (exp_name,)).fetchone()
    return str(row[0]) if row else None


def _incomplete_trading_day_phase_check(
    *,
    db_path: Path,
    config_id: Optional[str],
    exp_name: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
) -> AcceptanceCheck:
    metadata: Dict[str, Any] = {
        "strategy_profitability_checked": False,
        "boundary": "existing_phase_records_must_be_completed_before_new_backtest",
    }
    if not db_path.exists():
        return _pass_check("data_time_boundary", metadata=metadata)

    conn = sqlite3.connect(str(db_path))
    try:
        if not _table_exists(conn, "trading_day_phase"):
            metadata["trading_day_phase_table"] = "missing"
            return _pass_check("data_time_boundary", metadata=metadata)
        resolved_config_id = _resolve_config_id_from_db(conn, config_id=config_id, exp_name=exp_name)
        if not resolved_config_id:
            metadata["resolved_config_id"] = None
            return _pass_check("data_time_boundary", metadata=metadata)
        metadata["resolved_config_id"] = resolved_config_id

        parts = ["config_id = ?"]
        params: List[Any] = [resolved_config_id]
        if start_date:
            parts.append("substr(trading_date, 1, 10) >= ?")
            params.append(start_date)
        if end_date:
            parts.append("substr(trading_date, 1, 10) <= ?")
            params.append(end_date)
        rows = conn.execute(
            f"""
            SELECT trading_date, phase, status
            FROM trading_day_phase
            WHERE {' AND '.join(parts)}
            ORDER BY trading_date ASC, phase ASC
            """,
            tuple(params),
        ).fetchall()
    except Exception as exc:
        return _fail_check(
            "data_time_boundary",
            [f"trading_day_phase_check_failed:{type(exc).__name__}:{exc}"],
            metadata=metadata,
        )
    finally:
        conn.close()

    by_day: Dict[str, Dict[str, str]] = {}
    for trading_date, phase, status in rows:
        day = str(trading_date or "")[:10]
        phase_name = str(phase or "")
        if not day or not phase_name:
            continue
        by_day.setdefault(day, {})[phase_name] = str(status or "")

    errors: List[str] = []
    for day, phase_status in sorted(by_day.items()):
        non_completed = {
            phase: status
            for phase, status in sorted(phase_status.items())
            if status != "completed"
        }
        if non_completed:
            encoded = ",".join(f"{phase}={status}" for phase, status in non_completed.items())
            errors.append(f"incomplete_trading_day_phase:{day}:{encoded}")

    metadata["trading_day_phase_days_checked"] = len(by_day)
    if errors:
        return _fail_check("data_time_boundary", errors, metadata=metadata)
    return _pass_check("data_time_boundary", metadata=metadata)


def _checks_from_invariants(
    *,
    db_path: Path,
    config_id: Optional[str],
    exp_name: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
) -> Dict[str, AcceptanceCheck]:
    invariant_report = audit_system_invariants(
        db_path=db_path,
        config_id=config_id,
        exp_name=exp_name,
        start_date=start_date,
        end_date=end_date,
    )
    per_check_errors: Dict[str, List[str]] = {name: [] for name in ACCEPTANCE_CHECKS}
    general_warnings = list(invariant_report.warnings)
    for error in invariant_report.errors:
        matched = False
        for prefix, check_name in INVARIANT_TO_CHECK.items():
            if error.startswith(prefix):
                per_check_errors.setdefault(check_name, []).append(error)
                matched = True
                break
        if not matched:
            per_check_errors.setdefault("audit_explainability", []).append(error)

    checks: Dict[str, AcceptanceCheck] = {}
    for name in (
        "single_trade_exit",
        "trader_trigger_parity",
        "learning_landing",
        "audit_explainability",
    ):
        errors = per_check_errors.get(name, [])
        checks[name] = AcceptanceCheck(
            name=name,
            ok=not errors,
            errors=errors,
            warnings=general_warnings if name == "audit_explainability" else [],
            metadata={
                "system_invariant_counts": dict(invariant_report.counts),
                "db_path": str(db_path),
                "strategy_profitability_checked": False,
            },
        )
    return checks


def run_pre_backtest_acceptance(
    *,
    config_path: str | Path,
    db_path: str | Path,
    repo_root: str | Path,
    assets_dir: str | Path,
    deepfund_python: str | Path,
    config_id: Optional[str] = None,
    exp_name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    check_llm_auth: bool = False,
) -> PreBacktestAcceptanceReport:
    config_path = Path(config_path)
    db_path = Path(db_path)
    repo_root = Path(repo_root)
    assets_dir = Path(assets_dir)
    deepfund_python = Path(deepfund_python)
    cfg = _load_config(config_path)
    if config_id is None and exp_name is None:
        exp_name = str(cfg.get("exp_name") or "").strip() or None

    checks: Dict[str, AcceptanceCheck] = {name: _pass_check(name) for name in ACCEPTANCE_CHECKS}

    preflight = run_preflight_checks(
        repo_root=repo_root,
        sqlite_paths=[db_path],
        writable_dirs=[assets_dir],
        required_files=[config_path],
        deepfund_python=deepfund_python,
        llm_config=cfg.get("llm") or {},
        check_llm_auth=check_llm_auth,
    )
    checks["environment_api"] = _check_from_result(
        "environment_api",
        preflight,
        metadata={"check_llm_auth": bool(check_llm_auth), "does_not_check_strategy_pnl": True},
    )
    checks["config_consistency"] = _config_consistency_check(cfg)
    agent_boundaries, structured_io = _capability_checks()
    checks["agent_boundaries"] = agent_boundaries
    checks["structured_io"] = _merge_acceptance_checks(
        "structured_io",
        structured_io,
        _runtime_field_unification_check(repo_root),
    )

    if db_path.exists():
        checks.update(
            _checks_from_invariants(
                db_path=db_path,
                config_id=config_id,
                exp_name=exp_name,
                start_date=start_date,
                end_date=end_date,
            )
        )
    else:
        checks["audit_explainability"] = _pass_check(
            "audit_explainability",
            warnings=[f"sqlite_missing:{db_path}"],
            metadata={"strategy_profitability_checked": False},
        )

    checks["data_time_boundary"] = _merge_acceptance_checks(
        "data_time_boundary",
        _trading_window_check(cfg, start_date, end_date),
        _incomplete_trading_day_phase_check(
            db_path=db_path,
            config_id=config_id,
            exp_name=exp_name,
            start_date=start_date,
            end_date=end_date,
        ),
    )
    checks["capital_boundary"] = _pass_check(
        "capital_boundary",
        metadata={
            "probe_margin_ratio": (cfg.get("position_budget_policy") or {}).get("probe_margin_ratio"),
            "probe_margin_max_ratio": (cfg.get("position_budget_policy") or {}).get("probe_margin_max_ratio"),
            "max_total_margin_ratio": cfg.get("max_total_margin_ratio"),
            "does_not_adjust_parameters": True,
        },
    )

    errors: List[str] = []
    warnings: List[str] = []
    for check in checks.values():
        errors.extend(f"{check.name}:{error}" for error in check.errors)
        warnings.extend(f"{check.name}:{warning}" for warning in check.warnings)
    ok = not errors
    metadata = {
        "config_path": str(config_path),
        "db_path": str(db_path),
        "acceptance_checks": list(ACCEPTANCE_CHECKS),
        "strategy_profitability_checked": False,
        "decision": "ready_for_strategy_backtest" if ok else "not_ready_for_backtest",
        "boundary": "system_readiness_only_not_strategy_profitability",
    }
    return PreBacktestAcceptanceReport(
        ok=ok,
        checks={name: checks[name] for name in ACCEPTANCE_CHECKS},
        errors=errors,
        warnings=warnings,
        metadata=metadata,
    )


def dumps_acceptance_report(report: PreBacktestAcceptanceReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
