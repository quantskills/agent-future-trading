from __future__ import annotations

"""Run the read-only daily system invariant audit."""

import argparse
import json
import sys
from pathlib import Path

import yaml


RUN_CONTROL_DIR = Path(__file__).resolve().parent
RUN_DIR = RUN_CONTROL_DIR.parent
SRC_ROOT = RUN_DIR.parent
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.control.pg_system_invariants import audit_system_invariants
from util.config_normalizer import normalize_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AgentQuant system invariant audit.")
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--local-db", action="store_true")
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument("--config-id", type=str, default=None)
    parser.add_argument("--exp-name", type=str, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
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


def main() -> int:
    args = parse_args()
    config_path = _resolve_config_path(args.config)
    cfg = _load_config(config_path)
    db_path = Path(args.db_path) if args.db_path else SRC_ROOT / "assets" / "agentquant.db"
    report = audit_system_invariants(
        db_path=db_path,
        config_id=args.config_id,
        exp_name=args.exp_name or cfg.get("exp_name"),
        start_date=args.start_date,
        end_date=args.end_date,
    ).to_dict()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("AgentQuant system invariant audit")
        print(f"  ok: {report.get('ok')}")
        for error in report.get("errors") or []:
            print(f"    - {error}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
