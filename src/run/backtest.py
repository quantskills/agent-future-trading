import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import yaml


RUN_DIR = Path(__file__).resolve().parent
SRC_ROOT = RUN_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from util.config_normalizer import normalize_config


@dataclass
class ScriptResult:
    trading_date: str
    script_name: str
    return_code: int
    skipped: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run historical futures backtests day by day with "
            "run/proposal.py, run/order.py, run/settlement.py, run/validate_phase_flow.py, "
            "and run/research/researcher_learning.py."
        )
    )
    parser.add_argument("--config", type=str, required=True, help="Path to YAML config, e.g. config/dev.yaml")
    parser.add_argument("--start-date", type=str, required=True, help="Backtest start date in YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, required=True, help="Backtest end date in YYYY-MM-DD")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    parser.add_argument(
        "--reset-config",
        action="store_true",
        help="Pass --reset-config to proposal.py on the first trading day only",
    )
    parser.add_argument(
        "--run-eval",
        action="store_true",
        help="Deprecated compatibility flag; evaluation now runs by default after the backtest window finishes",
    )
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="Skip the default evaluate_config.py --update step after the backtest window finishes",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Run run/plot_config.py after the backtest window finishes",
    )
    parser.add_argument(
        "--plot-no-price",
        action="store_true",
        help="When --plot is set, skip PandaAI price loading for ticker charts",
    )
    parser.add_argument(
        "--plot-output-dir",
        type=str,
        default=None,
        help="When --plot is set, directory for generated chart images",
    )
    return parser.parse_args()


def load_yaml_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as fh:
        return normalize_config(yaml.safe_load(fh), config_path)


def parse_day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def get_phase_record(exp_name: str, trading_date: str, phase_name: str, use_local_db: bool) -> Optional[dict]:
    from util.db_helper import db_initialize, get_db

    db_initialize(use_local_db=use_local_db)
    db = get_db()
    config_id = db.get_config_id_by_name(exp_name)
    if not config_id:
        return None
    return db.get_trading_day_phase(config_id, trading_date, phase_name)


def resolve_trading_days(config: dict, start_date: datetime, end_date: datetime) -> List[str]:
    from apis.router import APISource, Router

    tickers = config.get("tickers") or []
    if not tickers:
        raise ValueError("No tickers found in config.")

    anchor_ticker = tickers[0]
    market_type = config.get("market_type", "china_futures")
    if market_type != "china_futures":
        raise ValueError("backtest.py currently supports china_futures only.")

    router = Router(source=APISource.PANDAAI, market_type=market_type)
    quotes = router.api.get_futures_daily_candles_optimized(
        underlying_code=anchor_ticker,
        is_main=1,
        start_date=start_date,
        end_date=end_date + timedelta(days=1),
    )

    trade_days = sorted(
        {
            str(getattr(quote, "trade_date", ""))[:10]
            for quote in (quotes or [])
            if getattr(quote, "trade_date", None)
        }
    )
    trade_days = [day for day in trade_days if start_date.strftime("%Y-%m-%d") <= day <= end_date.strftime("%Y-%m-%d")]

    if not trade_days:
        raise RuntimeError(
            f"No trading days resolved from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} "
            f"for anchor ticker {anchor_ticker}."
        )

    return trade_days


def build_command(
    script_name: str,
    config_arg: str,
    trading_date: str = None,
    local_db: bool = False,
    reset: bool = False,
) -> List[str]:
    command = [sys.executable, script_name, "--config", config_arg]
    if trading_date is not None:
        command.extend(["--trading-date", trading_date])
    if local_db:
        command.append("--local-db")
    if reset:
        command.append("--reset-config")
    return command


def run_command(command: List[str], env: dict) -> int:
    completed = subprocess.run(command, cwd=str(SRC_ROOT), env=env)
    return completed.returncode


def run_protocol_preflight(config_arg: str, local_db: bool) -> int:
    command = [
        sys.executable,
        str(RUN_DIR / "control" / "protocol_preflight.py"),
        "--config",
        config_arg,
        "--json",
        "--check-llm-auth",
    ]
    if local_db:
        command.append("--local-db")
    print("[backtest] Running control/protocol_preflight.py")
    return run_command(command, os.environ.copy())


def run_contract_coverage_audit() -> int:
    command = [
        sys.executable,
        str(RUN_DIR / "control" / "contract_coverage_audit.py"),
        "--repo-root",
        str(SRC_ROOT.parent),
        "--json",
    ]
    print("[backtest] Running control/contract_coverage_audit.py")
    return run_command(command, os.environ.copy())


def run_pre_backtest_acceptance(config_arg: str, start_date: str, end_date: str, local_db: bool) -> int:
    command = [
        sys.executable,
        str(RUN_DIR / "control" / "pre_backtest_acceptance.py"),
        "--config",
        config_arg,
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--json",
        "--check-llm-auth",
    ]
    if local_db:
        command.extend(["--db-path", str(SRC_ROOT / "assets" / "agentquant.db")])
    print("[backtest] Running control/pre_backtest_acceptance.py")
    return run_command(command, os.environ.copy())


def run_system_invariant_audit(config_arg: str, start_date: str, end_date: str, local_db: bool) -> int:
    command = [
        sys.executable,
        str(RUN_DIR / "control" / "system_invariant_audit.py"),
        "--config",
        config_arg,
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--json",
    ]
    if local_db:
        command.append("--local-db")
    print("[backtest] Running control/system_invariant_audit.py")
    return run_command(command, os.environ.copy())


def run_mechanism_effectiveness_audit(config_arg: str, start_date: str, end_date: str, local_db: bool) -> int:
    command = [
        sys.executable,
        str(RUN_DIR / "control" / "mechanism_effectiveness_audit.py"),
        "--config",
        config_arg,
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--json",
    ]
    if local_db:
        command.append("--local-db")
    print("[backtest] Running control/mechanism_effectiveness_audit.py")
    return run_command(command, os.environ.copy())


def run_daily_cumulative_system_invariant_audit(
    config_arg: str,
    start_date: str,
    trading_day: str,
    local_db: bool,
) -> int:
    print(f"[backtest] Running control/system_invariant_audit.py through {trading_day}")
    return run_system_invariant_audit(config_arg, start_date, trading_day, local_db)


def run_daily_cumulative_mechanism_effectiveness_audit(
    config_arg: str,
    start_date: str,
    trading_day: str,
    local_db: bool,
) -> int:
    print(f"[backtest] Running control/mechanism_effectiveness_audit.py through {trading_day}")
    return run_mechanism_effectiveness_audit(config_arg, start_date, trading_day, local_db)


def main() -> int:
    args = parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (SRC_ROOT / config_path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    config_arg = str(config_path)

    start_date = parse_day(args.start_date)
    end_date = parse_day(args.end_date)
    if end_date < start_date:
        raise ValueError("end-date must be greater than or equal to start-date.")

    config = load_yaml_config(config_path)
    if config.get("market_type", "china_futures") != "china_futures":
        raise ValueError("backtest.py currently supports china_futures only.")
    if not args.local_db:
        raise ValueError("backtest.py requires --local-db for china_futures.")

    contract_coverage_return_code = run_contract_coverage_audit()
    if contract_coverage_return_code != 0:
        print(f"[backtest] contract_coverage_audit.py failed with exit code {contract_coverage_return_code}")
        return contract_coverage_return_code

    acceptance_return_code = run_pre_backtest_acceptance(
        config_arg,
        args.start_date,
        args.end_date,
        args.local_db,
    )
    if acceptance_return_code != 0:
        print(f"[backtest] pre_backtest_acceptance.py failed with exit code {acceptance_return_code}")
        return acceptance_return_code

    trading_days = resolve_trading_days(config, start_date, end_date)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    print(
        f"[backtest] Running {len(trading_days)} trading day(s) from "
        f"{trading_days[0]} to {trading_days[-1]} for exp_name={config.get('exp_name')} "
        f"(run_id={run_id})"
    )

    results: List[ScriptResult] = []

    for index, trading_day in enumerate(trading_days):
        print(f"[backtest] === {trading_day} ===")
        reset_today = args.reset_config and index == 0

        phase1_record = None if reset_today else get_phase_record(config["exp_name"], trading_day, "phase1", args.local_db)
        phase2_record = None if reset_today else get_phase_record(config["exp_name"], trading_day, "phase2", args.local_db)
        phase3_record = None if reset_today else get_phase_record(config["exp_name"], trading_day, "phase3", args.local_db)
        phase4_record = None if reset_today else get_phase_record(config["exp_name"], trading_day, "phase4", args.local_db)

        proposal_cmd = build_command(
            str(RUN_DIR / "proposal.py"),
            config_arg,
            trading_date=trading_day,
            local_db=args.local_db,
            reset=reset_today,
        )
        order_cmd = build_command(
            str(RUN_DIR / "order.py"),
            config_arg,
            trading_date=trading_day,
            local_db=args.local_db,
        )
        settlement_cmd = build_command(
            str(RUN_DIR / "settlement.py"),
            config_arg,
            trading_date=trading_day,
            local_db=args.local_db,
        )
        validate_cmd = build_command(
            str(RUN_DIR / "validate_phase_flow.py"),
            config_arg,
            trading_date=trading_day,
            local_db=args.local_db,
        )
        research_cmd = build_command(
            str(RUN_DIR / "research" / "researcher_learning.py"),
            config_arg,
            trading_date=trading_day,
            local_db=args.local_db,
        )

        for script_name, command, should_skip in (
            ("proposal.py", proposal_cmd, phase1_record is not None and phase1_record.get("status") == "completed"),
            ("order.py", order_cmd, phase2_record is not None and phase2_record.get("status") == "completed"),
            ("settlement.py", settlement_cmd, phase3_record is not None and phase3_record.get("status") == "completed"),
            ("validate_phase_flow.py", validate_cmd, phase4_record is not None and phase4_record.get("status") == "completed"),
            ("researcher_learning.py", research_cmd, False),
        ):
            if should_skip:
                print(f"[backtest] Skipping {script_name} for {trading_day}: already completed")
                results.append(
                    ScriptResult(
                        trading_date=trading_day,
                        script_name=script_name,
                        return_code=0,
                        skipped=True,
                    )
                )
                continue

            print(f"[backtest] Running {script_name} for {trading_day}")
            command_env = os.environ.copy()
            command_env["AGENTQUANT_RUN_ID"] = run_id
            command_env["AGENTQUANT_EXP_NAME"] = str(config.get("exp_name") or "")
            command_env["AGENTQUANT_LOG_NAMESPACE"] = f"{trading_day}_{Path(script_name).stem}"
            return_code = run_command(command, command_env)
            results.append(ScriptResult(trading_date=trading_day, script_name=script_name, return_code=return_code))
            if return_code != 0:
                print(
                    f"[backtest] Stopped on {trading_day}: {script_name} failed with exit code {return_code}"
                )
                return return_code

        invariant_return_code = run_daily_cumulative_system_invariant_audit(
            config_arg,
            trading_days[0],
            trading_day,
            args.local_db,
        )
        if invariant_return_code != 0:
            print(
                "[backtest] Stopped on "
                f"{trading_day}: system_invariant_audit.py failed with exit code {invariant_return_code}"
            )
            return invariant_return_code

        mechanism_return_code = run_daily_cumulative_mechanism_effectiveness_audit(
            config_arg,
            trading_days[0],
            trading_day,
            args.local_db,
        )
        if mechanism_return_code != 0:
            print(
                "[backtest] Stopped on "
                f"{trading_day}: mechanism_effectiveness_audit.py failed with exit code {mechanism_return_code}"
            )
            return mechanism_return_code

    if args.run_eval and args.skip_eval:
        raise ValueError("--run-eval and --skip-eval cannot be used together.")

    if not args.skip_eval:
        eval_command = [sys.executable, str(RUN_DIR / "evaluate_config.py"), "--config", config_arg]
        if args.local_db:
            eval_command.append("--local-db")
        eval_command.append("--update")
        print("[backtest] Running evaluate_config.py --update")
        eval_env = os.environ.copy()
        eval_env["AGENTQUANT_RUN_ID"] = run_id
        eval_env["AGENTQUANT_EXP_NAME"] = str(config.get("exp_name") or "")
        eval_env["AGENTQUANT_LOG_NAMESPACE"] = "evaluate_config"
        eval_return_code = run_command(eval_command, eval_env)
        if eval_return_code != 0:
            print(f"[backtest] evaluate_config.py failed with exit code {eval_return_code}")
            return eval_return_code

    invariant_return_code = run_system_invariant_audit(
        config_arg,
        trading_days[0],
        trading_days[-1],
        args.local_db,
    )
    if invariant_return_code != 0:
        print(f"[backtest] system_invariant_audit.py failed with exit code {invariant_return_code}")
        return invariant_return_code

    mechanism_return_code = run_mechanism_effectiveness_audit(
        config_arg,
        trading_days[0],
        trading_days[-1],
        args.local_db,
    )
    if mechanism_return_code != 0:
        print(f"[backtest] mechanism_effectiveness_audit.py failed with exit code {mechanism_return_code}")
        return mechanism_return_code

    if args.plot:
        plot_command = [sys.executable, str(RUN_DIR / "plot_config.py"), "--config", config_arg]
        if args.plot_no_price:
            plot_command.append("--no-price")
        if args.plot_output_dir:
            plot_command.extend(["--output-dir", args.plot_output_dir])

        print("[backtest] Running plot_config.py")
        plot_env = os.environ.copy()
        plot_env["AGENTQUANT_RUN_ID"] = run_id
        plot_env["AGENTQUANT_EXP_NAME"] = str(config.get("exp_name") or "")
        plot_env["AGENTQUANT_LOG_NAMESPACE"] = "plot_config"
        plot_return_code = run_command(plot_command, plot_env)
        if plot_return_code != 0:
            print(f"[backtest] plot_config.py failed with exit code {plot_return_code}")
            return plot_return_code

    print("[backtest] Backtest loop completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
