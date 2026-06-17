from __future__ import annotations

"""Preflight health checks.

The default checks are deterministic and local-only. Expensive or external
checks must be requested explicitly by the CLI/backtest entrypoint so unit tests
do not depend on live provider credentials.
"""

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, Field

from llm.inference import (
    _classify_llm_error,
    _load_llm_env_files,
    _normalize_llm_config,
    get_model,
    llm_audit_metadata,
)
from llm.provider import Provider
from tools.agent_tools.control.schemas import ProtocolCheckResult


class LLMPreflightProbe(BaseModel):
    ok: bool = Field(default=True)


EXPECTED_CODEX_OPENAI_BASE_URL = "http://47.74.0.65/v1"
_PROVIDER_BLOCK_KEYS = {
    Provider.CODEX_OPENAI: "codex_openai",
    Provider.TQXAI: "tqxai",
}
_DISALLOWED_RUNTIME_LLM_BLOCK_KEYS = {"deepseek"}


def _check_writable_dir(path: Path) -> bool:
    return path.exists() and path.is_dir() and os.access(str(path), os.W_OK)


def run_llm_preflight_check(
    llm_config: Dict[str, Any],
    *,
    check_auth: bool = False,
) -> ProtocolCheckResult:
    errors: List[str] = []
    warnings: List[str] = []
    metadata: Dict[str, Any] = {"check_auth": bool(check_auth)}

    try:
        _load_llm_env_files()
        llm_cfg = _normalize_llm_config(llm_config)
        provider = Provider(llm_cfg.provider)
        audit_metadata = llm_audit_metadata(llm_config)
        selected_block = _PROVIDER_BLOCK_KEYS.get(provider)
        for block_key in sorted(_DISALLOWED_RUNTIME_LLM_BLOCK_KEYS):
            if block_key in llm_config:
                errors.append(f"llm_runtime_provider_block_not_allowed:{block_key}")
        for block_provider, block_key in _PROVIDER_BLOCK_KEYS.items():
            if block_key == selected_block:
                continue
            if block_key in llm_config:
                errors.append(f"llm_provider_block_mismatch:{block_key}:selected={provider.value}")
        if provider == Provider.CODEX_OPENAI and audit_metadata.get("base_url") != EXPECTED_CODEX_OPENAI_BASE_URL:
            errors.append(f"llm_codex_gateway_mismatch:{audit_metadata.get('base_url')}")
        metadata.update(
            {
                "provider": provider.value,
                "model": llm_cfg.model,
                "base_url": audit_metadata.get("base_url"),
                "api_key_env": audit_metadata.get("api_key_env"),
            }
        )
        provider_config = provider.config
        if provider_config.requires_api_key:
            env_key = audit_metadata.get("api_key_env")
            if not env_key:
                errors.append(f"llm_api_key_env_missing:{provider.value}")
            elif not os.getenv(str(env_key)):
                errors.append(f"llm_api_key_missing:{provider.value}:{env_key}")
        if check_auth and not errors:
            llm = get_model(llm_cfg)
            method = llm_cfg.structured_output_method or provider_config.structured_output_method
            probe = llm.with_structured_output(LLMPreflightProbe, method=method).invoke(
                "Return JSON only: {\"ok\": true}"
            )
            if isinstance(probe, dict):
                probe = LLMPreflightProbe(**probe)
            if probe is None or not getattr(probe, "ok", False):
                errors.append(f"llm_auth_probe_invalid_response:{provider.value}:{llm_cfg.model}")
    except Exception as exc:
        error_type = _classify_llm_error(exc)
        provider_name = metadata.get("provider", "unknown")
        model_name = metadata.get("model", "unknown")
        errors.append(f"llm_preflight_failed:{provider_name}:{model_name}:{error_type}:{exc}")

    return (
        ProtocolCheckResult.fail_result(errors, warnings=warnings, metadata=metadata)
        if errors
        else ProtocolCheckResult.pass_result(warnings=warnings, metadata=metadata)
    )


def run_preflight_checks(
    *,
    repo_root: Optional[Path] = None,
    sqlite_paths: Optional[Iterable[Path]] = None,
    writable_dirs: Optional[Iterable[Path]] = None,
    required_files: Optional[Iterable[Path]] = None,
    deepfund_python: Optional[Path] = None,
    llm_config: Optional[Dict[str, Any]] = None,
    check_llm_auth: bool = False,
) -> ProtocolCheckResult:
    errors: List[str] = []
    warnings: List[str] = []
    metadata: Dict[str, Any] = {}

    if repo_root is not None and not Path(repo_root).exists():
        errors.append("repo_root_missing")

    if deepfund_python is not None:
        python_path = Path(deepfund_python)
        metadata["deepfund_python"] = str(python_path)
        if not python_path.exists():
            errors.append("deepfund_python_missing")
        elif "deepfund" not in str(python_path).lower():
            warnings.append("python_path_does_not_look_like_deepfund")

    for raw_path in required_files or []:
        path = Path(raw_path)
        if not path.exists():
            errors.append(f"required_file_missing:{path}")

    for raw_dir in writable_dirs or []:
        path = Path(raw_dir)
        if not _check_writable_dir(path):
            errors.append(f"writable_dir_unavailable:{path}")

    for raw_db in sqlite_paths or []:
        db_path = Path(raw_db)
        if not db_path.exists():
            warnings.append(f"sqlite_missing:{db_path}")
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.execute("select 1")
            conn.close()
        except sqlite3.Error as exc:
            errors.append(f"sqlite_unreadable:{db_path}:{exc}")

    result = ProtocolCheckResult.fail_result(errors, warnings=warnings, metadata=metadata) if errors else ProtocolCheckResult.pass_result(
        warnings=warnings,
        metadata=metadata,
    )
    if llm_config is not None:
        result = result.merge(run_llm_preflight_check(llm_config, check_auth=check_llm_auth))
    return result
