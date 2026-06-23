from __future__ import annotations

"""Read-only mechanism effectiveness audit CLI.

This command is run after system invariant audit and before strategy
attribution. It exits non-zero only for hard mechanism disconnects. Diagnostics
are printed but do not stop strategy analysis.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml


RUN_DIR = Path(__file__).resolve().parents[1]
SRC_ROOT = RUN_DIR.parent
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.mechanism_effectiveness_audit import audit_mechanism_effectiveness
from util.config_normalizer import normalize_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AgentQuant mechanism effectiveness audit.")
    parser.add_argument("--config", type=str, required=True, help="Path to YAML config, e.g. src/config/dev.yaml")
    parser.add_argument("--config-id", type=str, default=None, help="Config UUID; defaults to exp_name lookup")
    parser.add_argument("--start-date", type=str, default=None, help="Optional YYYY-MM-DD lower bound")
    parser.add_argument("--end-date", type=str, default=None, help="Optional YYYY-MM-DD upper bound")
    parser.add_argument("--local-db", action="store_true", help="Use local SQLite database")
    parser.add_argument("--db-path", type=str, default=None, help="Override SQLite path")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
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
        return normalize_config(yaml.safe_load(fh), config_path)


def main() -> int:
    args = parse_args()
    config_path = _resolve_config_path(args.config)
    cfg = _load_config(config_path)
    db_path = Path(args.db_path) if args.db_path else SRC_ROOT / "assets" / "agentquant.db"
    report = audit_mechanism_effectiveness(
        db_path=db_path,
        config_id=args.config_id,
        exp_name=cfg.get("exp_name"),
        start_date=args.start_date,
        end_date=args.end_date,
    )
    payload = report.to_dict()
    payload["agent_name"] = "protocol_governor"
    payload["contract_version"] = "agentquant.mechanism_effectiveness_audit.v1"
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("AgentQuant mechanism effectiveness audit")
        print(f"  ok: {report.ok}")
        print(f"  db_path: {db_path}")
        print(f"  counts: {report.counts}")
        if report.hard_failures:
            print("  hard_failures:")
            for error in report.hard_failures:
                print(f"    - {error}")
        if report.diagnostics:
            print("  diagnostics:")
            for diagnostic in report.diagnostics:
                print(f"    - {diagnostic}")
        if report.warnings:
            print("  warnings:")
            for warning in report.warnings:
                print(f"    - {warning}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
