from __future__ import annotations

"""Unified field-name audit helpers.

This module is a control/audit rule list only. The deprecated names below must
not drive runtime analysis, PM decisions, Trader execution, accounting, review,
or learning. They are named here so pre-backtest and post-backtest audits can
fail fast if any old semantic field leaks back into production paths.
"""

import ast
import re
from pathlib import Path
from typing import Any, Iterable, List, Sequence, Tuple

import yaml


FORBIDDEN_RUNTIME_FIELD_TOKENS = {
    "final_action_contract_authority",
    "requires_final_action_contract_authority",
    "final_new_entry_trade_authority",
    "requires_final_new_entry_trade_authority",
    "can_open_real_position",
    "can_apply_min_real_floor",
    "tradable_lots_if_executed_now",
    "tradable_lots_reason",
    "target_lots_estimate",
    "current_trade_setup",
    "trend_continuation_tradeable",
    "range_reversal_tradeable",
    "volatility_breakout_candidate",
    "pm_layer_hint",
    "technical_trigger_valid",
    "conditional_trigger_pending",
    "current_trigger_status",
    "requires_current_confirmation",
    "requires_current_confirmation_for_trade",
    "trade_permission",
    "none_without_current_confirmation",
    "pre_open_plan",
    "execution_plan",
    "policy_hint",
    "action_bias",
    "deprecated_policy_hint_mirror",
    "deprecated_action_bias_mirror",
    "final_action_context",
    "opportunity_layer",
    "signal_template",
    "trigger_type",
    "entry_type",
    "neutral_shadow_side",
    "trade_trigger",
    "position_horizon",
    "direction_only",
    "tradeable_setup",
    "deployable_alpha",
    "shadow_",
}

RUNTIME_FIELD_SCAN_ROOTS = (
    "agents",
    "tools",
    "util",
    "database",
    "evaluation",
    "run",
    "llm",
    "config",
    "graph",
)

RUNTIME_FIELD_ALLOWED_FILES = {
    Path("database/sqlite_setup.py"),
    Path("tools/agent_tools/control/unified_field_audit.py"),
}

LEGACY_FIELD_LOCATION_ALLOWED_FILES = {
    Path("docs/release_baseline_2026-06-17.md"),
    Path("docs/work_log.md"),
    Path("src/database/sqlite_setup.py"),
    Path("src/tools/agent_tools/control/contract_coverage_audit.py"),
    Path("src/tools/agent_tools/control/unified_field_audit.py"),
    Path("src/tests/test_system_invariant_audit.py"),
    Path("src/tests/test_unified_field_migration.py"),
}

LEGACY_FIELD_LOCATION_SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".md"}

LEGACY_FIELD_LOCATION_EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    "assets",
    "logs",
}


def iter_runtime_field_files(
    src_root: str | Path,
    *,
    roots: Sequence[str] = RUNTIME_FIELD_SCAN_ROOTS,
    allowed_files: Iterable[Path] = RUNTIME_FIELD_ALLOWED_FILES,
) -> Iterable[Tuple[Path, Path]]:
    """Yield production files that must not contain deprecated runtime fields."""
    src_root = Path(src_root)
    allowed = {Path(item) for item in allowed_files}
    for root_name in roots:
        root = src_root / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".py", ".yaml", ".yml"}:
                continue
            rel = path.relative_to(src_root)
            if rel in allowed:
                continue
            if "__pycache__" in rel.parts:
                continue
            yield path, rel


def scan_runtime_field_usage(
    src_root: str | Path,
    *,
    tokens: Iterable[str] = FORBIDDEN_RUNTIME_FIELD_TOKENS,
) -> tuple[List[str], int]:
    """Return deprecated field keys found in production runtime files.

    This scans structured field positions only: Python dict keys, subscript
    string keys, `.get("key")`, class annotations, and YAML mapping keys.
    Comments, reason text, log text, and explanatory prose are intentionally
    ignored; they cannot create a machine-readable semantic path.
    """
    offenders: List[str] = []
    checked_files = 0
    token_set = set(tokens)
    for path, rel in iter_runtime_field_files(src_root):
        checked_files += 1
        keys = _structured_field_keys(path)
        for key in sorted(keys):
            if any(_field_key_matches_token(key, token) for token in token_set):
                offenders.append(f"{rel}:{key}")
    return offenders, checked_files


def scan_legacy_field_token_locations(
    repo_root: str | Path,
    *,
    tokens: Iterable[str] = FORBIDDEN_RUNTIME_FIELD_TOKENS,
    allowed_files: Iterable[Path] = LEGACY_FIELD_LOCATION_ALLOWED_FILES,
) -> tuple[List[str], int, int]:
    """Return legacy field-token mentions outside the explicit allowlist.

    This is stricter than `scan_runtime_field_usage`: it scans active text files
    for old field names anywhere, then permits them only in migration code,
    control/audit rule lists, negative tests, and archived history. The goal is
    not to parse business semantics here; it is to make old-field survival
    locations explicit and reviewable.
    """
    repo_root = Path(repo_root)
    token_set = set(tokens)
    allowed = {Path(item) for item in allowed_files}
    offenders: List[str] = []
    occurrence_count = 0
    checked_files = 0
    for path in _iter_legacy_field_text_files(repo_root):
        checked_files += 1
        rel = path.relative_to(repo_root)
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), 1):
            for token in sorted(token_set):
                if not _line_contains_legacy_token(line, token):
                    continue
                occurrence_count += 1
                if rel not in allowed:
                    offenders.append(f"{rel}:{line_no}:{token}")
    return offenders, checked_files, occurrence_count


def _iter_legacy_field_text_files(repo_root: Path) -> Iterable[Path]:
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in LEGACY_FIELD_LOCATION_SCAN_SUFFIXES:
            continue
        if any(part in LEGACY_FIELD_LOCATION_EXCLUDED_PARTS for part in path.parts):
            continue
        yield path


def _line_contains_legacy_token(line: str, token: str) -> bool:
    if token.endswith("_"):
        return token in line
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])"
    return re.search(pattern, line) is not None


def _structured_field_keys(path: Path) -> set[str]:
    if path.suffix.lower() == ".py":
        return _python_field_keys(path)
    if path.suffix.lower() in {".yaml", ".yml"}:
        return _yaml_field_keys(path)
    return set()


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _python_field_keys(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                value = _literal_string(key) if key is not None else None
                if value:
                    keys.add(value)
        elif isinstance(node, ast.Subscript):
            value = _literal_string(node.slice)
            if value:
                keys.add(value)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "setdefault", "pop"}:
                if node.args:
                    value = _literal_string(node.args[0])
                    if value:
                        keys.add(value)
        elif isinstance(node, (ast.AnnAssign, ast.Assign)):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
            for target in targets:
                if isinstance(target, ast.Name):
                    keys.add(target.id)
    return keys


def _yaml_field_keys(path: Path) -> set[str]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return set()
    keys: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add(str(key))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(parsed)
    return keys


def _field_key_matches_token(field_key: str, token: str) -> bool:
    if token.endswith("_"):
        return field_key.startswith(token)
    return field_key == token


def find_forbidden_artifact_field_keys(
    value: Any,
    *,
    tokens: Iterable[str] = FORBIDDEN_RUNTIME_FIELD_TOKENS,
    prefix: str = "",
) -> List[str]:
    """Find deprecated field keys inside persisted runtime artifacts.

    Values are not scanned. Reason codes and human explanations may mention old
    words while describing a failure; only JSON field names can create a second
    machine-readable semantic path.
    """
    token_set = set(tokens)
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if any(_field_key_matches_token(key_text, token) for token in token_set):
                found.append(path)
            found.extend(find_forbidden_artifact_field_keys(item, tokens=token_set, prefix=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            found.extend(find_forbidden_artifact_field_keys(item, tokens=token_set, prefix=path))
    return found
