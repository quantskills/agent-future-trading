from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from apis.router import APISource, Router
from util.logger import logger
from util.trading_calendar import get_previous_trading_day


def _normalize_date(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _latest_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    values = [dict(row) for row in rows if isinstance(row, dict)]
    if not values:
        return []
    max_date = max(_normalize_date(row.get("date")) for row in values)
    return [row for row in values if _normalize_date(row.get("date")) == max_date]


def _sum_field(rows: Iterable[Dict[str, Any]], field: str) -> float:
    return sum(_coerce_float(row.get(field)) for row in rows if isinstance(row, dict))


def _ratio_value(value: Any) -> float:
    ratio = _coerce_float(value)
    if abs(ratio) > 2:
        return ratio / 100.0
    return ratio


def _direction_from_score(score: float, threshold: float = 0.10) -> str:
    if score > threshold:
        return "long"
    if score < -threshold:
        return "short"
    return "neutral"


def _feature_result(name: str, score: float, value: float, detail: str) -> Dict[str, Any]:
    return {
        "feature": name,
        "score": float(score),
        "direction": _direction_from_score(score),
        "value": float(value),
        "detail": detail,
    }


def _average(values: Iterable[float]) -> float:
    items = [float(value) for value in values]
    if not items:
        return 0.0
    return sum(items) / len(items)


def _signed_rank_score(
    rows: Iterable[Dict[str, Any]],
    *,
    value_fields: tuple[str, ...],
    long_labels: tuple[str, ...] = ("long", "多", "多头"),
    short_labels: tuple[str, ...] = ("short", "空", "空头"),
) -> tuple[float, float, str]:
    latest = _latest_rows(rows)
    if not latest:
        return 0.0, 0.0, ""

    long_total = 0.0
    short_total = 0.0
    untyped_total = 0.0
    has_typed = False
    for row in latest:
        value = 0.0
        for field in value_fields:
            raw = row.get(field)
            if raw is not None:
                value = _coerce_float(raw)
                break
        position_type = str(row.get("position_type") or row.get("type") or "").lower()
        if any(label in position_type for label in long_labels):
            long_total += value
            has_typed = True
        elif any(label in position_type for label in short_labels):
            short_total += value
            has_typed = True
        else:
            untyped_total += value

    if has_typed:
        value = long_total - short_total
        denom = abs(long_total) + abs(short_total) or 1.0
        return max(-1.0, min(1.0, value / denom)), value, "typed_long_minus_short"

    score = max(-1.0, min(1.0, untyped_total / (abs(untyped_total) + 1.0)))
    return score, untyped_total, "signed_untyped_sum"


def score_pandaai_extra_records(records: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Convert PandaAI futures non-market records into directional factor scores.

    Positive scores support long exposure, negative scores support short exposure.
    The scores are deliberately coarse: they are pre-open evidence, not a standalone
    signal generator.
    """
    features: List[Dict[str, Any]] = []

    basis_rows = _latest_rows(records.get("basis", []))
    if basis_rows:
        ratio = _ratio_value(basis_rows[-1].get("basis_ratio"))
        features.append(_feature_result("basis", max(-1.0, min(1.0, ratio / 0.04)), ratio, "basis_ratio"))

    wr_rows = _latest_rows(records.get("warehouse_receipt", []))
    if wr_rows:
        wr_change = _sum_field(wr_rows, "wr_lot_change")
        wr_quantity = abs(_sum_field(wr_rows, "wr_lot_quantity")) or 1.0
        pressure = wr_change / wr_quantity
        # Rising warehouse receipts are treated as supply pressure.
        features.append(_feature_result("warehouse_receipt", -max(-1.0, min(1.0, pressure)), pressure, "wr_lot_change/wr_lot_quantity"))

    net_flow_long_rows = _latest_rows(records.get("net_flow_long", []))
    net_flow_short_rows = _latest_rows(records.get("net_flow_short", []))
    if net_flow_long_rows and net_flow_short_rows:
        long_flow = _sum_field(net_flow_long_rows, "money_flow")
        short_flow = _sum_field(net_flow_short_rows, "money_flow")
        denom = abs(long_flow) + abs(short_flow) or 1.0
        score = (long_flow - short_flow) / denom
        features.append(_feature_result("net_flow", score, long_flow - short_flow, "long_money_flow-short_money_flow"))

    long_change = _sum_field(_latest_rows(records.get("variety_position_rank_long", [])), "change_oi")
    short_change = _sum_field(_latest_rows(records.get("variety_position_rank_short", [])), "change_oi")
    if long_change or short_change:
        denom = abs(long_change) + abs(short_change) or 1.0
        score = (long_change - short_change) / denom
        features.append(_feature_result("variety_position_rank", score, long_change - short_change, "top_long_change_oi-top_short_change_oi"))

    long_symbol_change = _sum_field(_latest_rows(records.get("symbol_position_rank_long", [])), "change_oi")
    short_symbol_change = _sum_field(_latest_rows(records.get("symbol_position_rank_short", [])), "change_oi")
    if long_symbol_change or short_symbol_change:
        denom = abs(long_symbol_change) + abs(short_symbol_change) or 1.0
        score = (long_symbol_change - short_symbol_change) / denom
        features.append(_feature_result("symbol_position_rank", score, long_symbol_change - short_symbol_change, "contract_long_change_oi-contract_short_change_oi"))

    ls_rows = _latest_rows(records.get("ls_ratio", []))
    if ls_rows:
        ratio = _coerce_float(ls_rows[-1].get("ls_ratio"), 1.0)
        score = max(-1.0, min(1.0, ratio - 1.0))
        features.append(_feature_result("ls_ratio", score, ratio, "ls_ratio-1"))

    margin_change = _sum_field(_latest_rows(records.get("broker_net_margin_change", [])), "margin_change")
    if margin_change:
        score = 1.0 if margin_change > 0 else -1.0
        features.append(_feature_result("broker_net_margin_change", score, margin_change, "sum_margin_change"))

    net_margin_score, net_margin_value, net_margin_detail = _signed_rank_score(
        records.get("broker_net_margin", []),
        value_fields=("net_margin",),
    )
    if net_margin_value:
        features.append(_feature_result("broker_net_margin", net_margin_score, net_margin_value, net_margin_detail))

    netposi_score, netposi_value, netposi_detail = _signed_rank_score(
        records.get("netposi_rank", []),
        value_fields=("net_position_change", "net_position"),
    )
    if netposi_value:
        features.append(_feature_result("netposi_rank", netposi_score, netposi_value, netposi_detail))

    net_cap = _sum_field(_latest_rows(records.get("net_cap_change", [])), "net_cap_value")
    if net_cap:
        score = max(-1.0, min(1.0, net_cap / (abs(net_cap) + 1.0)))
        features.append(_feature_result("net_cap_change", score, net_cap, "sum_net_cap_value"))

    contract_indicator_rows = _latest_rows(records.get("contract_daily_indicators", []))
    indicator_scores = []
    for row in contract_indicator_rows:
        ratio = _coerce_float(row.get("ratio"))
        if ratio:
            indicator_scores.append(max(-1.0, min(1.0, _ratio_value(ratio) - 1.0)))
    if indicator_scores:
        score = _average(indicator_scores)
        features.append(_feature_result("contract_daily_indicators", score, score, "avg_ratio_minus_1"))

    rank_rows = _latest_rows(records.get("contract_rank", []))
    long_rank = 0.0
    short_rank = 0.0
    untyped_scores = []
    for row in rank_rows:
        ratio = _ratio_value(row.get("ratio"))
        position_type = str(row.get("position_type") or "").lower()
        if "long" in position_type or "多" in position_type:
            long_rank += ratio
        elif "short" in position_type or "空" in position_type:
            short_rank += ratio
        elif ratio:
            untyped_scores.append(max(-1.0, min(1.0, ratio - 1.0)))
    if long_rank or short_rank:
        denom = abs(long_rank) + abs(short_rank) or 1.0
        features.append(_feature_result("contract_rank", (long_rank - short_rank) / denom, long_rank - short_rank, "rank_long_ratio-short_ratio"))
    elif untyped_scores:
        score = _average(untyped_scores)
        features.append(_feature_result("contract_rank", score, score, "avg_rank_ratio_minus_1"))

    profit = _sum_field(_latest_rows(records.get("broker_variety_profit", [])), "profit")
    if profit:
        score = 0.5 if profit > 0 else -0.5
        features.append(_feature_result("broker_variety_profit", score, profit, "sum_broker_profit"))

    return features


_NET_FLOW_KEYS = {"net_flow_long", "net_flow_short"}
_NET_FLOW_REPLACEMENT_KEYS = {
    "variety_position_rank_long",
    "variety_position_rank_short",
    "symbol_position_rank_long",
    "symbol_position_rank_short",
    "broker_net_margin_change",
    "broker_net_margin",
    "netposi_rank",
    "net_cap_change",
    "contract_daily_indicators",
    "contract_rank",
}


def _is_positive_record_count(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except Exception:
        return False


def _zero_record_keys(record_counts: Dict[str, Any]) -> List[str]:
    missing = []
    for key, count in (record_counts or {}).items():
        if not _is_positive_record_count(count):
            missing.append(key)
    return missing


def _normalized_feature_statuses(
    record_counts: Dict[str, Any],
    feature_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    statuses: Dict[str, str] = {}
    raw_statuses = feature_status or {}
    for key, count in (record_counts or {}).items():
        if _is_positive_record_count(count):
            statuses[key] = "ok"
            continue
        status = str(raw_statuses.get(key) or "no_data").strip() or "no_data"
        statuses[key] = status
    for key, value in raw_statuses.items():
        statuses.setdefault(str(key), str(value or "unknown"))
    return statuses


def _group_feature_statuses(feature_status: Dict[str, str]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for key, status in sorted((feature_status or {}).items()):
        grouped.setdefault(str(status), []).append(key)
    return grouped


def _split_actionable_missing(
    record_counts: Dict[str, Any],
    feature_status: Optional[Dict[str, Any]] = None,
) -> tuple[List[str], List[str], List[str], Dict[str, str]]:
    statuses = _normalized_feature_statuses(record_counts, feature_status)
    unavailable = [key for key, status in statuses.items() if status != "ok"]
    has_net_flow_replacement = any(
        _is_positive_record_count((record_counts or {}).get(key))
        for key in _NET_FLOW_REPLACEMENT_KEYS
    )
    fallback_covered = (
        [key for key in unavailable if key in _NET_FLOW_KEYS]
        if has_net_flow_replacement
        else []
    )
    for key in fallback_covered:
        statuses[key] = "fallback_covered"

    actionable = [
        key for key in unavailable
        if key not in fallback_covered and statuses.get(key) != "unsupported_feature"
    ]
    return actionable, fallback_covered, unavailable, statuses


class MarketConfirmationEngine:
    """PandaAI pre-open confirmation layer for futures signals.

    It does not create standalone trading signals. It only checks whether
    optional PandaAI non-market futures data supports or conflicts with the
    Phase1 target direction.
    """

    def __init__(self, config: Dict[str, Any], router: Optional[Router] = None):
        self.config = config or {}
        self.extra_config = self.config.get("pandaai_extra_data", {}) or {}
        self.confirm_config = self.config.get("market_confirmation", {}) or {}
        self.router = router or Router(APISource.PANDAAI, market_type="china_futures")

    @property
    def enabled(self) -> bool:
        return bool(self.extra_config.get("enabled", False)) and bool(self.confirm_config.get("enabled", True))

    def evaluate(
        self,
        *,
        underlying_code: str,
        trading_date: Any,
        target_direction: str,
        signal_strength: float,
        contract_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "target_direction": target_direction,
                "confirmation_score": 0.0,
                "features": [],
                "confirmations": [],
                "conflicts": [],
                "data_missing": ["pandaai_extra_data disabled"],
                "data_unavailable": ["pandaai_extra_data disabled"],
                "fallback_covered_missing": [],
                "record_counts": {},
                "feature_status": {},
                "feature_diagnostics": {},
                "data_status_groups": {},
                "errors": [],
            }

        try:
            reference_date = trading_date
            for _ in range(max(1, int(self.extra_config.get("reference_lag_days", 1)))):
                reference_date = get_previous_trading_day(
                    router=self.router,
                    trading_date=reference_date,
                    underlying_code=underlying_code,
                )
        except Exception:
            logger.warning(
                f"{underlying_code}: pandaai_confirmation_reference_date_unavailable"
            )
            return {
                "enabled": True,
                "target_direction": target_direction,
                "confirmation_score": 0.0,
                "features": [],
                "confirmations": [],
                "conflicts": [],
                "data_missing": ["reference_date_unavailable"],
                "data_unavailable": ["reference_date_unavailable"],
                "fallback_covered_missing": [],
                "record_counts": {},
                "feature_status": {},
                "feature_diagnostics": {},
                "data_status_groups": {},
                "errors": ["pandaai_confirmation_reference_date_unavailable"],
            }

        snapshot = self.router.get_pandaai_futures_extra_snapshot(
            underlying_code=underlying_code,
            reference_date=reference_date,
            lookback_days=int(self.extra_config.get("lookback_days", 5)),
            contract_id=contract_code,
            features=self.extra_config.get("features", {}),
        )
        records = snapshot.get("records", {}) if isinstance(snapshot, dict) else {}
        features = self._score_features(records)
        confirmations = [
            item for item in features
            if item["direction"] == target_direction and target_direction in {"long", "short"}
        ]
        conflicts = [
            item for item in features
            if item["direction"] in {"long", "short"}
            and target_direction in {"long", "short"}
            and item["direction"] != target_direction
        ]
        directional_features = [
            item for item in features
            if item["direction"] in {"long", "short"}
        ]
        feature_count = len(directional_features)
        raw_score = (
            (len(confirmations) - len(conflicts)) / feature_count
            if feature_count
            else 0.0
        )
        confirmation_score = max(0.0, min(1.0, (raw_score + 1.0) / 2.0)) if feature_count else 0.0
        total_score = sum(float(item["score"]) for item in features)

        record_counts = snapshot.get("record_counts", {}) if isinstance(snapshot, dict) else {}
        raw_feature_status = snapshot.get("feature_status", {}) if isinstance(snapshot, dict) else {}
        data_missing, fallback_covered_missing, data_unavailable, feature_status = _split_actionable_missing(
            record_counts,
            raw_feature_status,
        )
        data_status_groups = _group_feature_statuses(feature_status)

        result = {
            "enabled": True,
            "mode": self.extra_config.get("mode", "confirm_only"),
            "pre_open_only": True,
            "info_cutoff": "T-1_or_earlier",
            "reference_date": _normalize_date(reference_date),
            "target_direction": target_direction,
            "signal_strength": float(signal_strength or 0.0),
            "direction": _direction_from_score(total_score / len(features)) if features else "neutral",
            "raw_score": float(raw_score),
            "confirmation_score": float(confirmation_score),
            "features": features,
            "confirmations": [item["feature"] for item in confirmations],
            "conflicts": [item["feature"] for item in conflicts],
            "data_missing": data_missing,
            "data_unavailable": data_unavailable,
            "fallback_covered_missing": fallback_covered_missing,
            "record_counts": record_counts,
            "feature_status": feature_status,
            "feature_diagnostics": snapshot.get("feature_diagnostics", {}) if isinstance(snapshot, dict) else {},
            "data_status_groups": data_status_groups,
            "parameter_errors": data_status_groups.get("parameter_error", []),
            "no_data": data_status_groups.get("no_data", []),
            "unsupported_features": data_status_groups.get("unsupported_feature", []),
            "errors": snapshot.get("errors", []),
        }
        return result

    def _score_features(self, records: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        return score_pandaai_extra_records(records)
