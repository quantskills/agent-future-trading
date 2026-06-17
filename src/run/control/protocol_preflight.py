from __future__ import annotations

"""Run deterministic protocol-governor preflight checks.

This command is intentionally separate from backtest.py. It helps catch local
environment, artifact, and protocol-shape issues before a costly backtest, but
it does not participate in strategy generation or trading decisions.
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

try:
    from agents.control_team.protocol_governor import ProtocolGovernor
    from util.config_normalizer import normalize_config
except Exception as import_exc:  # pragma: no cover - exercised via CLI failure path.
    ProtocolGovernor = None  # type: ignore[assignment]
    normalize_config = None  # type: ignore[assignment]
    PROTOCOL_GOVERNOR_IMPORT_ERROR = import_exc
else:
    PROTOCOL_GOVERNOR_IMPORT_ERROR = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AgentQuant protocol preflight checks.")
    parser.add_argument("--config", type=str, required=True, help="Path to YAML config, e.g. src/config/dev.yaml")
    parser.add_argument("--local-db", action="store_true", help="Check local SQLite database paths")
    parser.add_argument(
        "--deepfund-python",
        type=str,
        default=r"C:\ProgramData\miniconda3\envs\deepfund\python.exe",
        help="Expected deepfund Python executable",
    )
    parser.add_argument(
        "--check-llm-auth",
        action="store_true",
        help="Run a live, lightweight structured-output probe against the configured LLM provider",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser.parse_args()


def resolve_config_path(config_path: str) -> Path:
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
    return normalize_config(raw, config_path) if normalize_config is not None else raw


def main() -> int:
    args = parse_args()
    if PROTOCOL_GOVERNOR_IMPORT_ERROR is not None or ProtocolGovernor is None:
        report = {
            "agent_name": "protocol_governor",
            "contract_version": "unknown",
            "ok": False,
            "errors": [f"protocol_governor_import_failed:{PROTOCOL_GOVERNOR_IMPORT_ERROR}"],
            "warnings": [],
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("AgentQuant protocol preflight")
            print("  ok: False")
            print("  errors:")
            print(f"    - {report['errors'][0]}")
        return 1
    config_path = resolve_config_path(args.config)
    try:
        cfg = _load_config(config_path)
        governor = ProtocolGovernor()
        capability_result = governor.validate_capability_cards()

        sqlite_paths = []
        if args.local_db:
            sqlite_paths.extend(
                [
                    SRC_ROOT / "assets" / "agentquant.db",
                    SRC_ROOT / "assets" / "pandaai_market_cache.db",
                ]
            )

        preflight_result = governor.run_preflight(
            repo_root=PROJECT_ROOT,
            sqlite_paths=sqlite_paths,
            writable_dirs=[SRC_ROOT / "assets"],
            required_files=[config_path],
            deepfund_python=Path(args.deepfund_python),
            llm_config=cfg.get("llm") or {},
            check_llm_auth=bool(args.check_llm_auth),
        )
        combined = capability_result.merge(preflight_result)
        report = {
            "agent_name": governor.agent_name,
            "contract_version": governor.contract_version,
            "capability_validation": capability_result.to_dict(),
            "preflight": preflight_result.to_dict(),
            "ok": combined.ok,
            "errors": combined.errors,
            "warnings": combined.warnings,
        }
    except Exception as exc:
        report = {
            "agent_name": "protocol_governor",
            "contract_version": "unknown",
            "ok": False,
            "errors": [f"protocol_preflight_unhandled_error:{type(exc).__name__}:{exc}"],
            "warnings": [],
        }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("AgentQuant protocol preflight")
        print(f"  config: {config_path}")
        print(f"  ok: {combined.ok}")
        if combined.errors:
            print("  errors:")
            for error in combined.errors:
                print(f"    - {error}")
        if combined.warnings:
            print("  warnings:")
            for warning in combined.warnings:
                print(f"    - {warning}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
