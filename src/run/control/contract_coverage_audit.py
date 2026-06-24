from __future__ import annotations

"""Run the read-only contract coverage audit.

This is a version-level control-team gate. It checks that core runtime
contracts have production, consumption, audit, and real-path test coverage.
It does not read strategy PnL, write the database, or modify trade authority.
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
    from tools.agent_tools.control.contract_coverage_audit import audit_contract_coverage
except Exception as import_exc:  # pragma: no cover - exercised through CLI failure.
    audit_contract_coverage = None  # type: ignore[assignment]
    CONTRACT_COVERAGE_IMPORT_ERROR = import_exc
else:
    CONTRACT_COVERAGE_IMPORT_ERROR = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AgentQuant contract coverage audit.")
    parser.add_argument("--repo-root", type=str, default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if CONTRACT_COVERAGE_IMPORT_ERROR is not None or audit_contract_coverage is None:
        report = {
            "agent_name": "protocol_governor",
            "contract_version": "unknown",
            "ok": False,
            "errors": [f"contract_coverage_import_failed:{CONTRACT_COVERAGE_IMPORT_ERROR}"],
            "warnings": [],
            "matrix": [],
        }
    else:
        try:
            report = audit_contract_coverage(Path(args.repo_root)).to_dict()
        except Exception as exc:
            report = {
                "agent_name": "protocol_governor",
                "contract_version": "agentquant.contract_coverage_audit.v1",
                "ok": False,
                "errors": [f"contract_coverage_unhandled_error:{type(exc).__name__}:{exc}"],
                "warnings": [],
                "matrix": [],
            }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("AgentQuant contract coverage audit")
        print(f"  ok: {report.get('ok')}")
        if report.get("errors"):
            print("  errors:")
            for error in report.get("errors") or []:
                print(f"    - {error}")
        if report.get("warnings"):
            print("  warnings:")
            for warning in report.get("warnings") or []:
                print(f"    - {warning}")
        if report.get("matrix"):
            print("  contracts:")
            for row in report.get("matrix") or []:
                print(f"    - {row.get('contract')}: ok={row.get('ok')}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
