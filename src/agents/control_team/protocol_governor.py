from __future__ import annotations

"""Protocol Governor sidecar entrypoints.

PG never creates or changes business facts. It performs deterministic
pre-backtest readiness checks and read-only daily persisted-result checks.
"""

from pathlib import Path
from typing import Any, Optional

from tools.agent_tools.control.pg_pre_backtest_acceptance import run_pre_backtest_acceptance
from tools.agent_tools.control.pg_schemas import (
    PG_CONTRACT_VERSION,
    PG_SOURCE_AGENT,
    ProtocolGovernorReport,
)
from tools.agent_tools.control.pg_system_invariants import audit_system_invariants


class ProtocolGovernor:
    agent_name = PG_SOURCE_AGENT
    contract_version = PG_CONTRACT_VERSION

    def run_pre_backtest_acceptance(
        self,
        *,
        config_path: str | Path,
        repo_root: str | Path,
        assets_dir: str | Path,
        deepfund_python: str | Path,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        run_test_modules: bool = True,
    ) -> ProtocolGovernorReport:
        return run_pre_backtest_acceptance(
            config_path=config_path,
            repo_root=repo_root,
            assets_dir=assets_dir,
            deepfund_python=deepfund_python,
            start_date=start_date,
            end_date=end_date,
            run_test_modules=run_test_modules,
        )

    def audit_daily_results(
        self,
        *,
        db_path: str | Path,
        config_id: Optional[str] = None,
        exp_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> ProtocolGovernorReport:
        return audit_system_invariants(
            db_path=db_path,
            config_id=config_id,
            exp_name=exp_name,
            start_date=start_date,
            end_date=end_date,
        )


def build_protocol_governor() -> ProtocolGovernor:
    return ProtocolGovernor()


def protocol_governor_agent(**kwargs: Any) -> dict:
    """Run exactly one explicitly selected PG sidecar mode."""

    governor = build_protocol_governor()
    mode = str(kwargs.pop("mode", "")).strip().lower()
    if mode == "pre_backtest":
        return governor.run_pre_backtest_acceptance(**kwargs).to_dict()
    if mode == "daily_post_backtest":
        return governor.audit_daily_results(**kwargs).to_dict()
    raise ValueError("protocol_governor_mode_required")
