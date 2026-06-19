from __future__ import annotations

"""Unified field-name audit helpers.

This module is a control/audit rule list only. The deprecated names below must
not drive runtime analysis, PM decisions, Trader execution, accounting, review,
or learning. They are named here so pre-backtest and post-backtest audits can
fail fast if any old semantic field leaks back into production paths.
"""

from pathlib import Path
from typing import Any, Iterable, List, Sequence, Tuple


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
    """Return deprecated field tokens found in production runtime files."""
    offenders: List[str] = []
    checked_files = 0
    sorted_tokens = sorted(set(tokens))
    for path, rel in iter_runtime_field_files(src_root):
        checked_files += 1
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in sorted_tokens:
            if token in text:
                offenders.append(f"{rel}:{token}")
    return offenders, checked_files


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
