from __future__ import annotations

"""Deterministic local preflight checks; this module never calls an LLM."""

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from dotenv import load_dotenv

from llm.inference import llm_audit_metadata
from tools.agent_tools.control.pg_schemas import ProtocolCheckResult


_SUPPORTED_LLM_PROVIDERS = {"codexopenai", "tqxai", "deepseek"}


def _llm_config_codes(llm_config: Dict[str, Any]) -> list[str]:
    codes: list[str] = []
    provider = str(llm_config.get("provider") or "").strip()
    model = str(llm_config.get("model") or "").strip()
    if not provider:
        codes.append("llm_provider_missing")
    if not model:
        codes.append("llm_model_missing")
    if provider.lower() not in _SUPPORTED_LLM_PROVIDERS:
        codes.append("llm_provider_not_supported_by_current_config")
        return codes
    if not provider or not model:
        return codes
    try:
        route = llm_audit_metadata(llm_config)
    except (KeyError, TypeError, ValueError):
        codes.append("llm_provider_config_invalid")
        return codes
    env_name = str(route.get("api_key_env") or "").strip()
    if not env_name:
        codes.append("llm_api_key_env_missing")
    elif not os.getenv(env_name):
        codes.append("llm_api_key_missing")
    if not str(route.get("base_url") or "").strip():
        codes.append("llm_base_url_missing")
    return codes


def run_preflight_checks(
    *,
    repo_root: Optional[Path] = None,
    sqlite_paths: Optional[Iterable[Path]] = None,
    writable_dirs: Optional[Iterable[Path]] = None,
    required_files: Optional[Iterable[Path]] = None,
    deepfund_python: Optional[Path] = None,
    llm_config: Optional[Dict[str, Any]] = None,
) -> ProtocolCheckResult:
    violations: list[str] = []
    diagnostics: list[str] = []

    if repo_root is not None:
        load_dotenv(Path(repo_root) / ".env", override=False)
    if repo_root is not None and not Path(repo_root).exists():
        violations.append("repo_root_missing")
    if deepfund_python is not None:
        python_path = Path(deepfund_python)
        if not python_path.exists():
            violations.append("deepfund_python_missing")
        elif "deepfund" not in str(python_path).lower():
            diagnostics.append("python_path_name_not_deepfund")
    for raw_path in required_files or []:
        if not Path(raw_path).exists():
            violations.append("required_file_missing")
    for raw_dir in writable_dirs or []:
        path = Path(raw_dir)
        if not path.exists() or not path.is_dir() or not os.access(str(path), os.W_OK):
            violations.append("writable_directory_unavailable")
    for raw_db in sqlite_paths or []:
        db_path = Path(raw_db)
        if not db_path.exists():
            diagnostics.append("optional_sqlite_database_missing")
            continue
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                conn.execute("SELECT 1")
        except sqlite3.Error:
            violations.append("sqlite_database_unreadable")
    if llm_config is not None:
        violations.extend(_llm_config_codes(llm_config))

    if violations:
        return ProtocolCheckResult.fail_result(
            "environment_and_entry",
            violations,
            diagnostic_codes=diagnostics,
        )
    return ProtocolCheckResult.pass_result(
        "environment_and_entry",
        diagnostic_codes=diagnostics,
    )
