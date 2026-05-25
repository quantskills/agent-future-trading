import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from util.logger import logger


SRC_ROOT = Path(__file__).resolve().parents[2]


def _project_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return SRC_ROOT.parent / path


def _memory_thresholds(cfg: Dict[str, Any]) -> Dict[str, Any]:
    memory_cfg = (cfg.get("strategy_memory") or {}) if isinstance(cfg, dict) else {}
    return {
        "min_samples_watchlist": int(memory_cfg.get("min_samples_watchlist", 2) or 2),
        "min_samples_weak_block": int(memory_cfg.get("min_samples_weak_block", 4) or 4),
        "min_samples_protected": int(memory_cfg.get("min_samples_protected", 3) or 3),
        "protected_win_rate": float(memory_cfg.get("protected_win_rate", 0.60) or 0.60),
        "protected_total_pnl": float(memory_cfg.get("protected_total_pnl", 1000) or 1000),
        "watchlist_win_rate_below": float(memory_cfg.get("watchlist_win_rate_below", 0.45) or 0.45),
        "watchlist_total_pnl_below": float(memory_cfg.get("watchlist_total_pnl_below", -500) or -500),
        "weak_block_win_rate_below": float(memory_cfg.get("weak_block_win_rate_below", 0.30) or 0.30),
        "weak_block_total_pnl_below": float(memory_cfg.get("weak_block_total_pnl_below", -2500) or -2500),
    }


def classify_template_prior_item(item: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    """Classify exported template evidence with the current strategy-memory thresholds."""
    thresholds = _memory_thresholds(cfg)
    sample_count = int(item.get("sample_count") or 0)
    win_rate = float(item.get("win_rate") or 0.0)
    net_pnl = float(item.get("net_pnl") or 0.0)
    fallback_state = str(item.get("prior_state") or item.get("memory_state") or "recovering").lower()

    if sample_count >= thresholds["min_samples_weak_block"] and (
        win_rate <= thresholds["weak_block_win_rate_below"]
        or net_pnl <= thresholds["weak_block_total_pnl_below"]
    ):
        return "weak_block"
    if sample_count >= thresholds["min_samples_watchlist"] and (
        win_rate <= thresholds["watchlist_win_rate_below"]
        or net_pnl <= thresholds["watchlist_total_pnl_below"]
    ):
        return "watchlist"
    if sample_count >= thresholds["min_samples_protected"] and (
        win_rate >= thresholds["protected_win_rate"]
        and net_pnl >= thresholds["protected_total_pnl"]
    ):
        return "protected"
    if sample_count >= 2 and win_rate >= 0.50 and net_pnl > 0:
        return "recovering"
    return fallback_state


def _template_prior_marker(payload: Dict[str, Any]) -> str:
    marker = payload.get("exported_at_trading_date") or payload.get("exported_at")
    return str(marker or "unversioned")


def _loaded_template_prior_marker(payload_json: Any) -> str:
    try:
        payload = json.loads(payload_json or "{}")
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("source_exported_at_trading_date") or payload.get("exported_at_trading_date") or "")


def load_template_prior_if_enabled(cfg: Dict[str, Any], db, config_id: str) -> int:
    """Load reviewer-exported template prior into strategy_memory at run start.

    Template prior is a cold-start bootstrap, not a permanent override: rows are
    marked with source=template_prior and get a fresh validity window for this
    backtest/run. Reviewer attribution can still expire, replace, or contradict
    them as new trades settle.
    """
    if cfg.get("market_type") != "china_futures":
        return 0
    learning_cfg = cfg.get("learning", {}) or {}
    prior_cfg = learning_cfg.get("template_prior", {}) or {}
    if not bool(prior_cfg.get("enabled", False)) or not bool(prior_cfg.get("load_on_backtest_start", False)):
        return 0
    if not hasattr(db, "_get_connection") or not hasattr(db, "_ensure_strategy_memory_schema"):
        logger.warning("Template prior load skipped: current DB backend does not expose local strategy_memory schema")
        return 0

    prior_path = _project_path(str(prior_cfg.get("path") or "src/logs/attribution/template_prior.json"))
    if not prior_path.exists():
        logger.info(f"Template prior not found, cold-start memory bootstrap skipped: {prior_path}")
        return 0

    try:
        payload = json.loads(prior_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Template prior load skipped: failed to parse {prior_path}: {exc}")
        return 0

    templates = payload.get("templates") if isinstance(payload, dict) else None
    if not isinstance(templates, list) or not templates:
        logger.info(f"Template prior has no templates, skipped: {prior_path}")
        return 0
    source_marker = _template_prior_marker(payload)
    reclassify_on_load = bool(prior_cfg.get("reclassify_on_load", True))

    trading_day_value = (
        cfg["trading_date"].strftime("%Y-%m-%d")
        if hasattr(cfg.get("trading_date"), "strftime")
        else str(cfg.get("trading_date") or "")[:10]
    )
    valid_days = int(prior_cfg.get("valid_days") or learning_cfg.get("memory_expires_after_days") or 30)
    valid_until = (datetime.strptime(trading_day_value, "%Y-%m-%d") + timedelta(days=max(1, valid_days))).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    allowed_states = {"protected", "deployable", "weak_block", "watchlist", "recovering"}

    inserted = 0
    conn = None
    try:
        conn = db._get_connection()
        cursor = conn.cursor()
        db._ensure_strategy_memory_schema(cursor)
        cursor.execute(
            '''
            SELECT payload_json
            FROM strategy_memory
            WHERE config_id = ? AND source = 'template_prior'
            ORDER BY updated_at DESC
            LIMIT 1
            ''',
            (config_id,),
        )
        existing_row = cursor.fetchone()
        loaded_marker = _loaded_template_prior_marker(existing_row["payload_json"] if existing_row else None)
        if loaded_marker == source_marker:
            logger.info(
                f"Template prior already loaded for config {config_id[:8]}... "
                f"marker={source_marker}, skipped"
            )
            return 0
        cursor.execute(
            "DELETE FROM strategy_memory WHERE config_id = ? AND source = 'template_prior'",
            (config_id,),
        )
        for item in templates:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").upper()
            side = str(item.get("side") or "").lower()
            original_state = str(item.get("prior_state") or item.get("memory_state") or "").lower()
            state = classify_template_prior_item(item, cfg) if reclassify_on_load else original_state
            if not ticker or side not in {"long", "short"} or state not in allowed_states:
                continue
            sample_count = int(item.get("sample_count") or 0)
            win_rate = float(item.get("win_rate") or 0.0)
            net_pnl = float(item.get("net_pnl") or 0.0)
            avg_pnl = float(item.get("avg_pnl") or 0.0)
            confidence = float(item.get("confidence_score") or min(1.0, sample_count / 10.0 + abs(net_pnl) / 50000.0))
            template_name = str(item.get("signal_template") or item.get("template_name") or "unknown")
            horizon_class = str(item.get("horizon_class") or "unknown")
            row_payload = {
                "source_file": str(prior_path),
                "loaded_at_trading_date": trading_day_value,
                "ticker": ticker,
                "side": side,
                "signal_template": template_name,
                "horizon_class": horizon_class,
                "prior_state": state,
                "original_prior_state": original_state,
                "sample_count": sample_count,
                "win_rate": win_rate,
                "net_pnl": net_pnl,
                "source_config_id": payload.get("config_id"),
                "source_exported_at_trading_date": source_marker,
                "loader_reclassified": reclassify_on_load and state != original_state,
                "payload": item.get("payload") or {},
            }
            cursor.execute(
                '''
                INSERT OR REPLACE INTO strategy_memory (
                    id, config_id, ticker, side, signal_combo, memory_state,
                    sample_count, win_rate, net_pnl, avg_pnl, confidence_score,
                    source, reason, updated_at, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, '*', ?, ?, ?, ?, ?, ?, 'template_prior', ?, ?, ?, ?)
                ''',
                (
                    str(uuid.uuid4()),
                    config_id,
                    ticker,
                    side,
                    state,
                    sample_count,
                    win_rate,
                    net_pnl,
                    avg_pnl,
                    confidence,
                    f"template_prior:{template_name}:{horizon_class}",
                    now,
                    valid_until,
                    json.dumps(row_payload, ensure_ascii=False),
                ),
            )
            inserted += 1
        conn.commit()
        if inserted:
            logger.info(f"Loaded {inserted} template prior rows into strategy_memory from {prior_path}")
        return inserted
    except Exception as exc:
        logger.warning(f"Template prior load skipped: {exc}")
        return 0
    finally:
        if conn:
            conn.close()
