"""Run the Protocol Governor pre-backtest readiness gate once."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


RUN_DIR = Path(__file__).resolve().parent
SRC_ROOT = RUN_DIR.parent
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.control_team.protocol_governor import ProtocolGovernor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run agent-future-trading pre-backtest readiness checks.")
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--local-db", action="store_true")
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument(
        "--deepfund-python",
        type=str,
        default=r"C:\ProgramData\miniconda3\envs\deepfund\python.exe",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _resolve_config_path(config_path: str) -> Path:
    path = Path(config_path)
    if path.is_absolute():
        return path
    for candidate in (SRC_ROOT / path, PROJECT_ROOT / path):
        if candidate.exists():
            return candidate.resolve()
    return (PROJECT_ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    report = ProtocolGovernor().run_pre_backtest_acceptance(
        config_path=_resolve_config_path(args.config),
        repo_root=PROJECT_ROOT,
        assets_dir=SRC_ROOT / "assets",
        deepfund_python=Path(args.deepfund_python),
        start_date=args.start_date,
        end_date=args.end_date,
        run_test_modules=True,
    ).to_dict()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("agent-future-trading pre-backtest readiness gate")
        print(f"  status: {report['status']}")
        for check in report["checks"]:
            print(f"  {check['check_name']}: {check['status']}")
            for code in check["violation_codes"]:
                print(f"    - {code}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
