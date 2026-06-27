"""Run all daily post-backtest tests and control checks.

Test logic lives in src/tests/test_*.py. Control logic lives in
tools/agent_tools/control. This script only orchestrates the daily backtest gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

import yaml


RUN_DIR = Path(__file__).resolve().parent
SRC_ROOT = RUN_DIR.parent
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.mechanism_effectiveness_audit import audit_mechanism_effectiveness
from tools.agent_tools.control.system_invariants import audit_system_invariants
from util.config_normalizer import normalize_config


BACKTEST_DAILY_TEST_MODULES = [
    "tests.test_system_invariant_audit",
    "tests.test_mechanism_effectiveness_audit",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AgentQuant daily backtest test gate.")
    parser.add_argument("--config", type=str, required=True, help="Path to YAML config, e.g. config/dev.yaml")
    parser.add_argument("--config-id", type=str, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--local-db", action="store_true")
    parser.add_argument("--db-path", type=str, default=None)
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


def _load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return normalize_config(raw, config_path)


def _run_unittest_modules(modules: list[str]) -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    errors = [f"{case}: {message}" for case, message in result.errors]
    failures = [f"{case}: {message}" for case, message in result.failures]
    return {
        "ok": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "errors": errors,
        "failures": failures,
    }


def main() -> int:
    args = parse_args()
    config_path = _resolve_config_path(args.config)
    cfg = _load_config(config_path)
    db_path = Path(args.db_path) if args.db_path else SRC_ROOT / "assets" / "agentquant.db"

    unittest_report = _run_unittest_modules(BACKTEST_DAILY_TEST_MODULES)
    invariant_report = audit_system_invariants(
        db_path=db_path,
        config_id=args.config_id,
        exp_name=cfg.get("exp_name"),
        start_date=args.start_date,
        end_date=args.end_date,
    ).to_dict()
    mechanism_report = audit_mechanism_effectiveness(
        db_path=db_path,
        config_id=args.config_id,
        exp_name=cfg.get("exp_name"),
        start_date=args.start_date,
        end_date=args.end_date,
    ).to_dict()

    report = {
        "agent_name": "protocol_governor",
        "contract_version": "agentquant.backtest_daily_test.v1",
        "ok": bool(
            unittest_report.get("ok")
            and invariant_report.get("ok")
            and mechanism_report.get("ok")
        ),
        "unittest": unittest_report,
        "system_invariants": invariant_report,
        "mechanism_effectiveness": mechanism_report,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("AgentQuant daily backtest test gate")
        print(f"  ok: {report['ok']}")
        for key in ("unittest", "system_invariants", "mechanism_effectiveness"):
            section = report[key]
            print(f"  {key}: ok={section.get('ok')}")
            for error in section.get("errors") or section.get("failures") or section.get("hard_failures") or []:
                print(f"    - {error}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
