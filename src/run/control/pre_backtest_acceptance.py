from __future__ import annotations

"""Run frozen pre-backtest acceptance checks.

This command is a control-team gate before an expensive backtest. It validates
system readiness only; it does not evaluate strategy profitability and does not
participate in trade generation.
"""

import argparse
import json
import sys
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parents[1]
SRC_ROOT = RUN_DIR.parent
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    from tools.agent_tools.control.pre_backtest_acceptance import run_pre_backtest_acceptance
except Exception as import_exc:  # pragma: no cover - exercised through CLI failure.
    run_pre_backtest_acceptance = None  # type: ignore[assignment]
    ACCEPTANCE_IMPORT_ERROR = import_exc
else:
    ACCEPTANCE_IMPORT_ERROR = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AgentQuant pre-backtest acceptance checks.")
    parser.add_argument("--config", type=str, required=True, help="Path to YAML config, e.g. src/config/dev.yaml")
    parser.add_argument("--db-path", type=str, default=str(SRC_ROOT / "assets" / "agentquant.db"))
    parser.add_argument("--config-id", type=str, default=None)
    parser.add_argument("--exp-name", type=str, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument(
        "--deepfund-python",
        type=str,
        default=r"C:\ProgramData\miniconda3\envs\deepfund\python.exe",
        help="Expected deepfund Python executable",
    )
    parser.add_argument("--check-llm-auth", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def resolve_config_path(config_path: str) -> Path:
    path = Path(config_path)
    if path.is_absolute():
        return path
    for candidate in (SRC_ROOT / path, PROJECT_ROOT / path):
        if candidate.exists():
            return candidate.resolve()
    return (PROJECT_ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    if ACCEPTANCE_IMPORT_ERROR is not None or run_pre_backtest_acceptance is None:
        report = {
            "agent_name": "protocol_governor",
            "contract_version": "unknown",
            "ok": False,
            "failed_checks": ["environment_api"],
            "errors": [f"pre_backtest_acceptance_import_failed:{ACCEPTANCE_IMPORT_ERROR}"],
            "warnings": [],
        }
    else:
        try:
            report = run_pre_backtest_acceptance(
                config_path=resolve_config_path(args.config),
                db_path=Path(args.db_path),
                repo_root=PROJECT_ROOT,
                assets_dir=SRC_ROOT / "assets",
                deepfund_python=Path(args.deepfund_python),
                config_id=args.config_id,
                exp_name=args.exp_name,
                start_date=args.start_date,
                end_date=args.end_date,
                check_llm_auth=bool(args.check_llm_auth),
            ).to_dict()
        except Exception as exc:
            report = {
                "agent_name": "protocol_governor",
                "contract_version": "agentquant.pre_backtest_acceptance.v1",
                "ok": False,
                "failed_checks": ["environment_api"],
                "errors": [f"pre_backtest_acceptance_unhandled_error:{type(exc).__name__}:{exc}"],
                "warnings": [],
            }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("AgentQuant pre-backtest acceptance")
        print(f"  ok: {report.get('ok')}")
        if report.get("failed_checks"):
            print("  failed_checks:")
            for check in report.get("failed_checks") or []:
                print(f"    - {check}")
        if report.get("errors"):
            print("  errors:")
            for error in report.get("errors") or []:
                print(f"    - {error}")
        if report.get("warnings"):
            print("  warnings:")
            for warning in report.get("warnings") or []:
                print(f"    - {warning}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
