from __future__ import annotations

"""Protocol Governor pre-backtest readiness gate.

The gate is deterministic, never calls an LLM, never runs a real backtest, and
never writes the formal trading database. It checks exactly the ten categories
defined in ``docs/agent_pg.md``.
"""

import io
import os
import tempfile
import unittest
import compileall
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml

from tools.agent_tools.control.pg_contract_coverage_audit import audit_contract_coverage
from tools.agent_tools.control.pg_db_schema_contract import audit_db_schema_contract
from tools.agent_tools.control.pg_full_chain_dry_run import run_no_llm_full_chain_dry_run
from tools.agent_tools.control.pg_preflight import run_preflight_checks
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult, ProtocolGovernorReport
from tools.agent_tools.control.pg_unified_field_audit import scan_runtime_field_usage
from util.config_normalizer import normalize_config


PRE_BACKTEST_CHECK_NAMES = (
    "environment_and_entry",
    "config_and_parameter_mapping",
    "field_action_and_role_unification",
    "data_readiness",
    "time_boundary",
    "formal_temporary_database",
    "no_llm_full_chain_dry_run",
    "supported_business_paths",
    "orchestration_state_and_physical_boundary",
    "determination_boundary",
)

_TEST_GROUPS = {
    "config_and_parameter_mapping": (
        "src.tests.test_config_parameter_mapping",
    ),
    "field_action_and_role_unification": (
        "src.tests.test_contract_coverage_audit",
        "src.tests.test_protocol_governor",
        "src.tests.test_unified_field_migration",
    ),
    "supported_business_paths": (
        "src.tests.test_final_action_semantics",
        "src.tests.test_pm_state_transition_matrix",
        "src.tests.test_accountant_settlement_formulas",
        "src.tests.test_trade_path_incremental_repairs",
        "src.tests.test_researcher_lifecycle_contract",
    ),
    "orchestration_state_and_physical_boundary": (
        "src.tests.test_pre_backtest_pm_workflow_contracts",
        "src.tests.test_agent_output_contract_boundary",
        "src.tests.test_ordinary_neutral_aec_flow",
        "src.tests.test_protocol_preflight_cli",
    ),
    "determination_boundary": (
        "src.tests.test_fact_entry_boundaries",
        "src.tests.test_system_invariant_audit",
        "src.tests.test_pre_backtest_acceptance",
    ),
}


def _load_config(config_path: Path) -> Dict[str, Any]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return normalize_config(raw, config_path)


def _failed(check_name: str, codes: Iterable[str], diagnostics: Iterable[str] = ()) -> ProtocolCheckResult:
    return ProtocolCheckResult.fail_result(
        check_name,
        codes,
        diagnostic_codes=diagnostics,
    )


def _passed(check_name: str, diagnostics: Iterable[str] = ()) -> ProtocolCheckResult:
    return ProtocolCheckResult.pass_result(check_name, diagnostic_codes=diagnostics)


def _combine(check_name: str, *checks: ProtocolCheckResult) -> ProtocolCheckResult:
    violations = [code for check in checks for code in check.violation_codes]
    diagnostics = [code for check in checks for code in check.diagnostic_codes]
    if violations:
        return _failed(check_name, violations, diagnostics)
    if checks and all(check.status == "skipped" for check in checks):
        return ProtocolCheckResult.skipped_result(check_name, diagnostic_codes=diagnostics)
    return _passed(check_name, diagnostics)


def _environment_entry_check(
    *,
    repo_root: Path,
    assets_dir: Path,
    config_path: Path,
    deepfund_python: Path,
    llm_config: Dict[str, Any],
) -> ProtocolCheckResult:
    preflight = run_preflight_checks(
        repo_root=repo_root,
        sqlite_paths=(),
        writable_dirs=[assets_dir],
        required_files=[
            config_path,
            repo_root / "src/run/backtest.py",
            repo_root / "src/run/pre_backtest_test.py",
            repo_root / "src/run/backtest_daily_test.py",
        ],
        deepfund_python=deepfund_python,
        llm_config=llm_config,
    )
    compile_result = (
        _passed("environment_and_entry")
        if compileall.compile_dir(str(repo_root / "src"), quiet=1)
        else _failed("environment_and_entry", ["production_python_compile_failed"])
    )
    return _combine("environment_and_entry", preflight, compile_result)


def _config_mapping_check(cfg: Dict[str, Any], config_path: Path) -> ProtocolCheckResult:
    violations: list[str] = []
    catalogs = cfg.get("config_catalogs") or {}
    if not isinstance(catalogs, dict) or not catalogs:
        violations.append("config_catalog_index_missing")
    for catalog_path in catalogs.values():
        candidate = config_path.parent / str(catalog_path)
        if not candidate.exists():
            violations.append("configured_catalog_missing")
    if not cfg.get("tickers"):
        violations.append("configured_tickers_missing")
    if str(cfg.get("market_type") or "") != "china_futures":
        violations.append("unsupported_market_type")
    hard_margin_parameters = {
        "max_total_margin_ratio": cfg.get("max_total_margin_ratio"),
    }
    for field, raw_value in hard_margin_parameters.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            violations.append("hard_margin_parameter_missing_or_invalid")
            continue
        if value <= 0 or value > 1:
            violations.append("hard_margin_parameter_out_of_range")
    governance = ((cfg.get("control_governance") or {}).get("protocol_governor") or {})
    if any(
        bool(governance.get(field))
        for field in ("may_create_trade_authority", "may_modify_lots_or_margin", "may_execute_orders")
    ):
        violations.append("protocol_governor_business_authority_enabled")
    position_budget = cfg.get("position_budget_policy") or {}
    ordered_pairs = (
        ("probe_margin_ratio", "probe_margin_max_ratio"),
        ("normal_trade_margin_ratio", "normal_trade_margin_max_ratio"),
        ("deployable_margin_ratio", "deployable_margin_max_ratio"),
        ("exceptional_margin_ratio", "exceptional_margin_max_ratio"),
    )
    for lower_name, upper_name in ordered_pairs:
        try:
            if float(position_budget[lower_name]) > float(position_budget[upper_name]):
                violations.append("position_budget_parameter_order_invalid")
        except (KeyError, TypeError, ValueError):
            violations.append("position_budget_parameter_missing_or_invalid")
    return _failed("config_and_parameter_mapping", violations) if violations else _passed(
        "config_and_parameter_mapping"
    )


def _field_action_role_check(repo_root: Path) -> ProtocolCheckResult:
    violations: list[str] = []
    for relative in (
        "docs/matrix_field_semantics.md",
        "docs/matrix_action_canonical.md",
        "docs/workflow.md",
        "docs/agent_pg.md",
    ):
        if not (repo_root / relative).exists():
            violations.append("required_contract_document_missing")
    runtime_offenders, _ = scan_runtime_field_usage(repo_root / "src")
    if runtime_offenders:
        violations.append("deprecated_runtime_field_detected")
    coverage = audit_contract_coverage(repo_root)
    if not coverage.ok:
        violations.append("contract_coverage_incomplete")
    return _failed("field_action_and_role_unification", violations) if violations else _passed(
        "field_action_and_role_unification"
    )


def _parse_window(start_date: Optional[str], end_date: Optional[str]) -> tuple[Optional[datetime], Optional[datetime], list[str]]:
    violations: list[str] = []
    if not start_date and not end_date:
        return None, None, violations
    if not start_date or not end_date:
        return None, None, ["backtest_window_requires_start_and_end_date"]
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return None, None, ["backtest_window_date_invalid"]
    if end < start:
        violations.append("backtest_window_end_before_start")
    return start, end, violations


def _optional_data_directories_ready(cfg: Dict[str, Any]) -> tuple[list[str], list[str]]:
    violations: list[str] = []
    diagnostics: list[str] = []
    factor_data = cfg.get("factor_data") or {}
    project_root = Path(__file__).resolve().parents[4]
    finoview_dir = Path(str(factor_data.get("data_dir") or "data/Fundamental_data/Finoview_data"))
    news_dir = Path(str(((factor_data.get("news") or {}).get("data_dir")) or "data/News_data/Future_news"))
    if not finoview_dir.is_absolute():
        finoview_dir = project_root / finoview_dir
    if not news_dir.is_absolute():
        news_dir = project_root / news_dir
    if not finoview_dir.exists():
        violations.append("finoview_data_directory_missing")
    elif not any(finoview_dir.rglob("*.feather")):
        diagnostics.append("finoview_optional_records_unavailable")
    if not news_dir.exists():
        violations.append("news_data_directory_missing")
    elif not any(news_dir.glob("*.txt")):
        diagnostics.append("news_optional_records_unavailable")
    return violations, diagnostics


def _minute_row(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return vars(value) if hasattr(value, "__dict__") else {}


def _data_readiness_check(
    cfg: Dict[str, Any],
    *,
    start: Optional[datetime],
    end: Optional[datetime],
) -> ProtocolCheckResult:
    if start is None or end is None:
        return ProtocolCheckResult.skipped_result(
            "data_readiness",
            diagnostic_codes=["data_readiness_window_not_requested"],
        )

    violations: list[str] = []
    diagnostics: list[str] = []
    try:
        from apis.contract_info_cache import FuturesContractInfoCache
        from apis.router import APISource, Router

        router = Router(
            source=APISource.PANDAAI,
            market_type=str(cfg.get("market_type") or "china_futures"),
            config=cfg,
        )
        tickers = [str(value).strip().upper() for value in cfg.get("tickers") or [] if str(value).strip()]
        anchor_days: set[str] = set()
        for index, ticker in enumerate(tickers):
            info = FuturesContractInfoCache.get_contract_info(ticker)
            if not isinstance(info, dict):
                violations.append("contract_information_missing")
            elif not info.get("contract_multiplier") or not (
                info.get("margin_rate_long") or info.get("margin_rate_short")
            ):
                violations.append("contract_multiplier_or_margin_rate_missing")
            try:
                quotes = router.api.get_futures_daily_candles_optimized(
                    underlying_code=ticker,
                    is_main=1,
                    start_date=start,
                    end_date=end + timedelta(days=1),
                )
            except Exception:
                violations.append("pandaai_daily_data_unreadable")
                continue
            if not quotes:
                violations.append("pandaai_daily_data_missing")
                continue
            quote_days = {
                str(getattr(quote, "trade_date", ""))[:10]
                for quote in quotes
                if getattr(quote, "trade_date", None)
            }
            if index == 0:
                anchor_days = {
                    day
                    for day in quote_days
                    if start.strftime("%Y-%m-%d") <= day <= end.strftime("%Y-%m-%d")
                }
                if not anchor_days:
                    violations.append("configured_window_has_no_trading_day")
            elif anchor_days - quote_days:
                violations.append("pandaai_daily_trading_day_coverage_incomplete")
            for quote in quotes:
                if any(
                    getattr(quote, field, None) is None
                    for field in (
                        "trade_date",
                        "open_price",
                        "highest_price",
                        "lowest_price",
                        "close_price",
                        "settle_price",
                        "turnover_vol",
                        "open_int",
                        "ticker",
                    )
                ):
                    violations.append("pandaai_daily_required_field_missing")
                    break
            sample_quote = next(
                (
                    quote
                    for quote in quotes
                    if start.strftime("%Y-%m-%d")
                    <= str(getattr(quote, "trade_date", ""))[:10]
                    <= end.strftime("%Y-%m-%d")
                ),
                None,
            )
            if sample_quote is None:
                continue
            sample_date = str(getattr(sample_quote, "trade_date", ""))[:10]
            contract_code = str(getattr(sample_quote, "ticker", "") or "").strip().upper()
            if not contract_code:
                violations.append("main_contract_mapping_missing")
            else:
                try:
                    main_quote = router.get_futures_main_contract_quote_on_date(ticker, sample_date)
                    concrete_quote = router.get_futures_contract_quote_on_date(contract_code, sample_date)
                except Exception:
                    violations.append("concrete_contract_fact_interface_unreadable")
                else:
                    main_contract_code = str(getattr(main_quote, "ticker", "") or "").strip().upper()
                    if not main_contract_code or main_contract_code != contract_code:
                        violations.append("main_contract_mapping_mismatch")
                    if concrete_quote is None or getattr(concrete_quote, "settle_price", None) is None:
                        violations.append("concrete_contract_settlement_fact_missing")
            for frequency in ("15m", "1m"):
                try:
                    minute_rows = router.get_china_futures_minute_bars(
                        contract_id=contract_code or None,
                        underlying_code=ticker,
                        is_main=1,
                        start_date=datetime.strptime(sample_date, "%Y-%m-%d"),
                        end_date=datetime.strptime(sample_date, "%Y-%m-%d"),
                        frequency=frequency,
                    )
                except Exception:
                    violations.append("trader_minute_data_interface_unreadable")
                    continue
                if not minute_rows:
                    violations.append("trader_minute_data_missing")
                    continue
                required = {"datetime", "open", "high", "low", "close", "volume"}
                if not required.issubset(_minute_row(minute_rows[0])):
                    violations.append("trader_minute_data_required_field_missing")

            if index == 0:
                try:
                    fundamental = router.get_china_futures_fundamentals(
                        ticker,
                        end.strftime("%Y-%m-%d"),
                    )
                except Exception:
                    violations.append("finoview_read_parse_filter_unreadable")
                else:
                    if not fundamental:
                        diagnostics.append("finoview_optional_records_unavailable")
                try:
                    news = router.get_china_futures_news(
                        ticker,
                        end.strftime("%Y-%m-%d"),
                        news_count=10,
                        pre_open_only=True,
                    )
                except Exception:
                    violations.append("news_read_parse_filter_unreadable")
                else:
                    if not news:
                        diagnostics.append("news_optional_records_unavailable")
    except Exception:
        violations.append("market_data_router_unavailable")

    directory_violations, directory_diagnostics = _optional_data_directories_ready(cfg)
    violations.extend(directory_violations)
    diagnostics.extend(directory_diagnostics)

    if violations:
        return _failed("data_readiness", violations, diagnostics)
    return _passed("data_readiness", diagnostics)


def _runtime_time_boundary_check(
    start: Optional[datetime],
    end: Optional[datetime],
) -> ProtocolCheckResult:
    violations: list[str] = []
    try:
        import dis
        import inspect
        from types import SimpleNamespace

        import pandas as pd

        from agents.analysis_team.technical import _validate_pre_open_price_window
        from agents.execution_team.accountant import accountant_agent
        from run.research.researcher_learning import main as researcher_learning_main
        from tools.agent_tools.analysis.analyst_learning_context import build_learning_context
        from tools.agent_tools.decision.pm_decision_memory_retrieval import retrieve_pm_memory
        from tools.agent_tools.execution.trader_intraday_execution import _normalize_bars
        from util.trading_calendar import map_datetime_to_futures_trading_day

        probe_day = start or datetime(2025, 3, 10)
        previous_day = probe_day - timedelta(days=1)
        prices = pd.DataFrame(
            [{"close": 1.0}],
            index=pd.to_datetime([previous_day.strftime("%Y-%m-%d")]),
        )
        if _validate_pre_open_price_window("PG", probe_day, prices, True) != previous_day.strftime("%Y-%m-%d"):
            violations.append("time_boundary_mechanism_not_operational")

        class CalendarAPI:
            @staticmethod
            def get_futures_daily_candles_optimized(**_kwargs):
                return [SimpleNamespace(trade_date=(probe_day + timedelta(days=1)).strftime("%Y-%m-%d"))]

        calendar_router = SimpleNamespace(api=CalendarAPI())
        night_timestamp = probe_day.replace(hour=21)
        mapped = map_datetime_to_futures_trading_day(
            calendar_router,
            night_timestamp,
            "PG",
        )
        if mapped.strftime("%Y-%m-%d") != (probe_day + timedelta(days=1)).strftime("%Y-%m-%d"):
            violations.append("time_boundary_mechanism_not_operational")

        cutoff = probe_day.replace(hour=9, minute=15)
        normalized = _normalize_bars(
            [
                {"datetime": probe_day.replace(hour=9, minute=1).strftime("%Y-%m-%d %H:%M:%S")},
                {"datetime": probe_day.replace(hour=9, minute=30).strftime("%Y-%m-%d %H:%M:%S")},
            ],
            cutoff_datetime=cutoff,
        )
        if len(normalized) != 1 or normalized[0]["dt"] > cutoff:
            violations.append("time_boundary_mechanism_not_operational")

        def bytecode_names(function: Any) -> set[str]:
            return {str(instruction.argval) for instruction in dis.get_instructions(function)}

        accountant_names = bytecode_names(accountant_agent)
        researcher_names = bytecode_names(researcher_learning_main)
        if not {"get_trading_day_phase", "PHASE2"}.issubset(accountant_names):
            violations.append("time_boundary_consumer_wiring_missing")
        if not {"get_trading_day_phase", "PHASE4"}.issubset(researcher_names):
            violations.append("time_boundary_consumer_wiring_missing")
        for consumer in (build_learning_context, retrieve_pm_memory):
            if "trading_date" not in inspect.signature(consumer).parameters:
                violations.append("time_boundary_consumer_wiring_missing")
    except Exception:
        violations.append("time_boundary_mechanism_not_operational")
    return _failed("time_boundary", violations) if violations else _passed("time_boundary")


def _time_boundary_check(start: Optional[datetime], end: Optional[datetime], parse_violations: list[str]) -> ProtocolCheckResult:
    diagnostics: list[str] = []
    if start is None and end is None and not parse_violations:
        diagnostics.append("time_boundary_window_not_requested")
    parsed = _failed("time_boundary", parse_violations, diagnostics) if parse_violations else _passed(
        "time_boundary", diagnostics
    )
    return _combine("time_boundary", parsed, _runtime_time_boundary_check(start, end))


def _initialize_formal_temp_database(db_path: Path) -> None:
    from database import sqlite_setup

    old_path = sqlite_setup.DB_PATH
    try:
        sqlite_setup.DB_PATH = str(db_path)
        sqlite_setup.init_database()
    finally:
        sqlite_setup.DB_PATH = old_path


def _formal_temporary_database_check(db_path: Path, cfg: Dict[str, Any], artifact_root: Path) -> ProtocolCheckResult:
    violations: list[str] = []
    report = audit_db_schema_contract(db_path)
    if not report.passed:
        violations.append("formal_temporary_database_schema_invalid")
    try:
        from database.artifact_store import externalize_json_for_db, load_externalized_json
        from database.sqlite_helper import SQLiteDB

        helper = SQLiteDB()
        helper.db_path = str(db_path)
        helper_config = {
            "exp_name": "protocol-governor-prebacktest",
            "tickers": list(cfg.get("tickers") or []),
            "planner_mode": bool(cfg.get("planner_mode", False)),
            "llm": dict(cfg.get("llm") or {}),
        }
        config_id = helper.create_config(helper_config)
        if not config_id or not helper.get_config(config_id):
            violations.append("formal_database_helper_roundtrip_failed")

        old_artifact_root = os.getenv("AGENTQUANT_ARTIFACT_ROOT")
        os.environ["AGENTQUANT_ARTIFACT_ROOT"] = str(artifact_root)
        try:
            value = {"contract_version": "agentquant.protocol_governor.v1", "source_agent": "protocol_governor"}
            externalized = externalize_json_for_db(
                value,
                category="protocol_governor",
                record_id="prebacktest",
                field_name="checks",
                config_id=config_id,
                trading_date="prebacktest",
                inline_max_bytes=1,
            )
            loaded = load_externalized_json(
                externalized.inline_value,
                externalized.artifact_path,
                externalized.sha256,
            )
            if loaded != value:
                violations.append("formal_artifact_roundtrip_failed")
        finally:
            if old_artifact_root is None:
                os.environ.pop("AGENTQUANT_ARTIFACT_ROOT", None)
            else:
                os.environ["AGENTQUANT_ARTIFACT_ROOT"] = old_artifact_root
    except Exception:
        violations.append("formal_temporary_database_roundtrip_failed")
    return _failed("formal_temporary_database", violations) if violations else _passed(
        "formal_temporary_database"
    )


def _run_test_group(check_name: str, modules: Iterable[str]) -> ProtocolCheckResult:
    suite = unittest.defaultTestLoader.loadTestsFromNames(list(modules))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if result.wasSuccessful():
        return _passed(check_name)
    violations: list[str] = []
    if result.errors:
        violations.append("pre_backtest_test_module_error")
    if result.failures:
        violations.append("pre_backtest_test_module_failure")
    return _failed(check_name, violations)


def _static_orchestration_check(repo_root: Path) -> ProtocolCheckResult:
    import ast
    from types import SimpleNamespace
    from unittest.mock import patch

    path = repo_root / "src/run/backtest.py"
    if not path.is_file():
        return _failed("orchestration_state_and_physical_boundary", ["backtest_pg_orchestration_missing"])
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return _failed("orchestration_state_and_physical_boundary", ["backtest_pg_orchestration_missing"])
    main_node = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"),
        None,
    )
    if main_node is None:
        return _failed("orchestration_state_and_physical_boundary", ["backtest_pg_orchestration_missing"])
    calls: list[tuple[int, str]] = []
    for node in ast.walk(main_node):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.append((node.lineno, node.func.id))
        elif isinstance(node.func, ast.Attribute):
            calls.append((node.lineno, node.func.attr))
    ordered_names = [name for _, name in sorted(calls)]
    try:
        pre_index = ordered_names.index("run_pre_backtest_test")
        reset_index = ordered_names.index("reset_existing_config_if_requested")
        ordered_names.index("run_backtest_daily_test")
    except ValueError:
        return _failed("orchestration_state_and_physical_boundary", ["backtest_pg_orchestration_missing"])
    if pre_index >= reset_index:
        return _failed("orchestration_state_and_physical_boundary", ["backtest_pg_orchestration_order_invalid"])

    if path.resolve() != (Path(__file__).resolve().parents[4] / "src/run/backtest.py").resolve():
        return _passed("orchestration_state_and_physical_boundary")

    from run import backtest

    trace: list[str] = []
    args = SimpleNamespace(
        config=str(repo_root / "src/config/dev.yaml"),
        start_date="2025-03-10",
        end_date="2025-03-10",
        local_db=True,
        reset_config=False,
        run_eval=False,
        skip_eval=True,
        plot=False,
        plot_no_price=False,
        plot_output_dir=None,
    )

    def run_script(command: list[str], _env: dict[str, str]) -> int:
        trace.append(Path(command[1]).name)
        return 0

    with patch.object(backtest, "parse_args", return_value=args), patch.object(
        backtest,
        "load_yaml_config",
        return_value={"market_type": "china_futures", "exp_name": "pg", "tickers": ["BU"]},
    ), patch.object(
        backtest,
        "run_pre_backtest_test",
        side_effect=lambda *_args: trace.append("pre_backtest_test") or 0,
    ), patch.object(
        backtest,
        "reset_existing_config_if_requested",
        side_effect=lambda *_args: trace.append("reset") or None,
    ), patch.object(
        backtest,
        "resolve_trading_days",
        return_value=["2025-03-10"],
    ), patch.object(
        backtest,
        "get_phase_record",
        return_value=None,
    ), patch.object(
        backtest,
        "run_command",
        side_effect=run_script,
    ), patch.object(
        backtest,
        "run_backtest_daily_test",
        side_effect=lambda *_args: trace.append("backtest_daily_test") or 0,
    ):
        return_code = backtest.main()
    expected = [
        "pre_backtest_test",
        "reset",
        "proposal.py",
        "order.py",
        "settlement.py",
        "validate_phase_flow.py",
        "researcher_learning.py",
        "backtest_daily_test",
    ]
    if return_code != 0 or trace != expected:
        return _failed("orchestration_state_and_physical_boundary", ["backtest_pg_orchestration_order_invalid"])
    return _passed("orchestration_state_and_physical_boundary")


def run_pre_backtest_acceptance(
    *,
    config_path: str | Path,
    repo_root: str | Path,
    assets_dir: str | Path,
    deepfund_python: str | Path,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    run_test_modules: bool = True,
) -> ProtocolGovernorReport:
    config_path = Path(config_path)
    repo_root = Path(repo_root)
    assets_dir = Path(assets_dir)
    deepfund_python = Path(deepfund_python)
    cfg = _load_config(config_path)

    checks: dict[str, ProtocolCheckResult] = {
        "environment_and_entry": _environment_entry_check(
            repo_root=repo_root,
            assets_dir=assets_dir,
            config_path=config_path,
            deepfund_python=deepfund_python,
            llm_config=cfg.get("llm") or {},
        ),
        "config_and_parameter_mapping": _config_mapping_check(cfg, config_path),
        "field_action_and_role_unification": _field_action_role_check(repo_root),
    }
    parsed_start, parsed_end, parse_violations = _parse_window(start_date, end_date)
    checks["data_readiness"] = (
        ProtocolCheckResult.skipped_result(
            "data_readiness",
            diagnostic_codes=["data_readiness_window_invalid"],
        )
        if parse_violations
        else _data_readiness_check(cfg, start=parsed_start, end=parsed_end)
    )
    checks["time_boundary"] = _time_boundary_check(parsed_start, parsed_end, parse_violations)

    with tempfile.TemporaryDirectory(prefix="agentquant_pg_prebacktest_") as temp_dir:
        temp_root = Path(temp_dir)
        temp_db = temp_root / "agentquant.db"
        try:
            _initialize_formal_temp_database(temp_db)
            checks["formal_temporary_database"] = _formal_temporary_database_check(
                temp_db,
                cfg,
                temp_root / "artifacts",
            )
            checks["no_llm_full_chain_dry_run"] = run_no_llm_full_chain_dry_run(
                db_path=temp_db,
                cfg=cfg,
                artifact_root=temp_root / "full_chain_artifacts",
            )
        except Exception:
            checks["formal_temporary_database"] = _failed(
                "formal_temporary_database",
                ["formal_temporary_database_initialization_failed"],
            )
            checks["no_llm_full_chain_dry_run"] = _failed(
                "no_llm_full_chain_dry_run",
                ["formal_no_llm_full_chain_dry_run_failed"],
            )

    if run_test_modules:
        for check_name, modules in _TEST_GROUPS.items():
            tested = _run_test_group(check_name, modules)
            checks[check_name] = _combine(check_name, checks.get(check_name, _passed(check_name)), tested)
    else:
        for check_name in _TEST_GROUPS:
            checks[check_name] = ProtocolCheckResult.skipped_result(
                check_name,
                diagnostic_codes=["pre_backtest_test_modules_not_requested"],
            )

    static_orchestration = _static_orchestration_check(repo_root)
    if not static_orchestration.passed:
        checks["orchestration_state_and_physical_boundary"] = static_orchestration

    return ProtocolGovernorReport(checks=[checks[name] for name in PRE_BACKTEST_CHECK_NAMES])
