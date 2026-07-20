from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Deque, Dict, Iterable, List, Tuple


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _date_key(value: Any) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]


def _parse_date(value: Any) -> datetime | None:
    text = _date_key(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _execution_price(row: Dict[str, Any]) -> float:
    value = row.get("execution_price")
    if value is None:
        value = row.get("price")
    return float(value or 0.0)


def _holding_days(open_date: Any, close_date: Any) -> int | None:
    start = _parse_date(open_date)
    end = _parse_date(close_date)
    if start is None or end is None:
        return None
    return max(0, (end - start).days)


def _sorted_transactions(transactions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        [dict(item) for item in transactions],
        key=lambda row: (
            _date_key(row.get("trading_date")),
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        ),
    )


def _is_rollover_transaction(row: Dict[str, Any]) -> bool:
    return str(_enum_value(row.get("source_type")) or "").lower() == "rollover"


def _source_type(row: Dict[str, Any]) -> str:
    return str(_enum_value(row.get("source_type")) or "strategy").lower()


def _is_non_strategy_transaction(row: Dict[str, Any]) -> bool:
    return _source_type(row) != "strategy"


def build_completed_trade_pairs(
    transactions: Iterable[Dict[str, Any]],
    *,
    include_rollover: bool = True,
) -> List[Dict[str, Any]]:
    """Build FIFO completed futures round trips from transaction rows.

    The function is intentionally read-only and schema-light. It only requires
    the columns already written by `futures_transactions`, and it supports
    partial closes by splitting an opening lot block as needed.

    Set include_rollover=False when the caller wants a strategy-signal-only
    view. That mode excludes all non-strategy operational transactions,
    including rollover and forced_risk. Account-level evaluation should
    normally keep the default True, because operational transactions are part
    of the realized account path.
    """

    open_books: Dict[tuple[str, str, str], Deque[Dict[str, Any]]] = defaultdict(deque)
    pairs: List[Dict[str, Any]] = []

    for row in _sorted_transactions(transactions):
        if not include_rollover and _is_non_strategy_transaction(row):
            continue

        action = _enum_value(row.get("action"))
        if action not in {"open_long", "open_short", "close_long", "close_short"}:
            continue

        ticker = str(row.get("ticker") or row.get("underlying_code") or "").upper()
        contract = str(row.get("contract_code") or ticker).lower()
        lots = abs(int(row.get("lots") or 0))
        if not ticker or lots <= 0:
            continue

        price = _execution_price(row)
        multiplier = float(row.get("contract_multiplier") or 1.0)
        commission = float(row.get("commission") or 0.0)
        commission_per_lot = commission / lots if lots else 0.0

        if action in {"open_long", "open_short"}:
            side = "long" if action == "open_long" else "short"
            open_books[(ticker, contract, side)].append(
                {
                    "transaction_id": row.get("id"),
                    "recommendation_id": row.get("recommendation_id"),
                    "trading_date": _date_key(row.get("trading_date")),
                    "created_at": row.get("created_at"),
                    "price": price,
                    "lots_remaining": lots,
                    "lots_original": lots,
                    "commission_per_lot": commission_per_lot,
                    "contract_multiplier": multiplier,
                    "source_type": _source_type(row),
                }
            )
            continue

        side = "long" if action == "close_long" else "short"
        book_key = (ticker, contract, side)
        remaining_to_close = lots

        while remaining_to_close > 0 and open_books[book_key]:
            open_lot = open_books[book_key][0]
            paired_lots = min(remaining_to_close, int(open_lot["lots_remaining"]))
            open_price = float(open_lot["price"] or 0.0)
            close_price = price
            lot_multiplier = float(open_lot.get("contract_multiplier") or multiplier or 1.0)

            if side == "long":
                gross_pnl = (close_price - open_price) * paired_lots * lot_multiplier
            else:
                gross_pnl = (open_price - close_price) * paired_lots * lot_multiplier

            open_commission = float(open_lot.get("commission_per_lot") or 0.0) * paired_lots
            close_commission = commission_per_lot * paired_lots
            net_pnl = gross_pnl - open_commission - close_commission
            notional = open_price * paired_lots * lot_multiplier

            pairs.append(
                {
                    "ticker": ticker,
                    "contract_code": contract,
                    "side": side,
                    "lots": paired_lots,
                    "open_transaction_id": open_lot.get("transaction_id"),
                    "close_transaction_id": row.get("id"),
                    "open_recommendation_id": open_lot.get("recommendation_id"),
                    "close_recommendation_id": row.get("recommendation_id"),
                    "open_source_type": open_lot.get("source_type"),
                    "close_source_type": _source_type(row),
                    "contains_rollover": bool(
                        open_lot.get("source_type") == "rollover" or _is_rollover_transaction(row)
                    ),
                    "contains_forced_risk": bool(
                        open_lot.get("source_type") == "forced_risk" or _source_type(row) == "forced_risk"
                    ),
                    "contains_non_strategy": bool(
                        open_lot.get("source_type") != "strategy" or _is_non_strategy_transaction(row)
                    ),
                    "open_date": open_lot.get("trading_date"),
                    "close_date": _date_key(row.get("trading_date")),
                    "holding_days": _holding_days(open_lot.get("trading_date"), row.get("trading_date")),
                    "open_price": open_price,
                    "close_price": close_price,
                    "contract_multiplier": lot_multiplier,
                    "gross_pnl": gross_pnl,
                    "commission": open_commission + close_commission,
                    "net_pnl": net_pnl,
                    "return_on_notional": (net_pnl / notional) if notional else 0.0,
                }
            )

            open_lot["lots_remaining"] = int(open_lot["lots_remaining"]) - paired_lots
            remaining_to_close -= paired_lots
            if int(open_lot["lots_remaining"]) <= 0:
                open_books[book_key].popleft()

    return pairs


def build_strategy_originated_trade_pairs_with_diagnostics(
    transactions: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build completed FIFO pairs for positions opened by a strategy action.

    All transaction sources participate in matching.  A rollover or
    forced-risk close therefore realizes the result of the strategy position
    it actually closes.  When one rollover recommendation closes an old
    contract and opens a new contract, the strategy origin is transferred to
    the new contract without changing either transaction's source type.

    The returned diagnostics describe physical close lots that cannot be
    matched after the supplied history has been replayed.  Callers evaluating
    a date window must therefore supply all transactions through the window
    end and apply the window to pair close dates afterwards.
    """

    open_books: Dict[tuple[str, str, str], Deque[Dict[str, Any]]] = defaultdict(deque)
    rollover_transfers: Dict[tuple[str, str, str], Deque[Dict[str, Any]]] = defaultdict(deque)
    strategy_pairs: List[Dict[str, Any]] = []
    unmatched_closes: List[Dict[str, Any]] = []

    def append_open_lot(
        *,
        row: Dict[str, Any],
        ticker: str,
        contract: str,
        side: str,
        lots: int,
        price: float,
        multiplier: float,
        commission_per_lot: float,
        origin: Dict[str, Any] | None = None,
    ) -> None:
        source_type = _source_type(row)
        origin_payload = origin or {
            "source_type": source_type,
            "recommendation_id": row.get("recommendation_id"),
            "transaction_id": row.get("id"),
            "trading_date": _date_key(row.get("trading_date")),
            "contains_rollover": source_type == "rollover",
            "contains_forced_risk": source_type == "forced_risk",
            "contains_non_strategy": source_type != "strategy",
        }
        open_books[(ticker, contract, side)].append(
            {
                "transaction_id": row.get("id"),
                "recommendation_id": row.get("recommendation_id"),
                "trading_date": _date_key(row.get("trading_date")),
                "created_at": row.get("created_at"),
                "price": price,
                "lots_remaining": lots,
                "commission_per_lot": commission_per_lot,
                "contract_multiplier": multiplier,
                "source_type": source_type,
                "origin_source_type": origin_payload.get("source_type"),
                "origin_recommendation_id": origin_payload.get("recommendation_id"),
                "origin_transaction_id": origin_payload.get("transaction_id"),
                "origin_open_date": origin_payload.get("trading_date"),
                "lineage_contains_rollover": bool(
                    origin_payload.get("contains_rollover") or source_type == "rollover"
                ),
                "lineage_contains_forced_risk": bool(
                    origin_payload.get("contains_forced_risk") or source_type == "forced_risk"
                ),
                "lineage_contains_non_strategy": bool(
                    origin_payload.get("contains_non_strategy") or source_type != "strategy"
                ),
            }
        )

    for row in _sorted_transactions(transactions):
        action = _enum_value(row.get("action"))
        if action not in {"open_long", "open_short", "close_long", "close_short"}:
            continue

        ticker = str(row.get("ticker") or row.get("underlying_code") or "").upper()
        contract = str(row.get("contract_code") or ticker).lower()
        lots = abs(int(row.get("lots") or 0))
        if not ticker or lots <= 0:
            continue

        price = _execution_price(row)
        multiplier = float(row.get("contract_multiplier") or 1.0)
        commission = float(row.get("commission") or 0.0)
        commission_per_lot = commission / lots
        source_type = _source_type(row)
        recommendation_id = str(row.get("recommendation_id") or "")

        if action in {"open_long", "open_short"}:
            side = "long" if action == "open_long" else "short"
            remaining_to_open = lots
            transfer_key = (recommendation_id, ticker, side)
            transfer_queue = rollover_transfers[transfer_key]

            if source_type == "rollover" and recommendation_id:
                while remaining_to_open > 0 and transfer_queue:
                    transfer = transfer_queue[0]
                    inherited_lots = min(remaining_to_open, int(transfer["lots_remaining"]))
                    append_open_lot(
                        row=row,
                        ticker=ticker,
                        contract=contract,
                        side=side,
                        lots=inherited_lots,
                        price=price,
                        multiplier=multiplier,
                        commission_per_lot=commission_per_lot,
                        origin=transfer,
                    )
                    transfer["lots_remaining"] = int(transfer["lots_remaining"]) - inherited_lots
                    remaining_to_open -= inherited_lots
                    if int(transfer["lots_remaining"]) <= 0:
                        transfer_queue.popleft()

            if remaining_to_open > 0:
                append_open_lot(
                    row=row,
                    ticker=ticker,
                    contract=contract,
                    side=side,
                    lots=remaining_to_open,
                    price=price,
                    multiplier=multiplier,
                    commission_per_lot=commission_per_lot,
                )
            continue

        side = "long" if action == "close_long" else "short"
        book_key = (ticker, contract, side)
        remaining_to_close = lots

        while remaining_to_close > 0 and open_books[book_key]:
            open_lot = open_books[book_key][0]
            paired_lots = min(remaining_to_close, int(open_lot["lots_remaining"]))
            open_price = float(open_lot["price"] or 0.0)
            lot_multiplier = float(open_lot.get("contract_multiplier") or multiplier or 1.0)
            if side == "long":
                gross_pnl = (price - open_price) * paired_lots * lot_multiplier
            else:
                gross_pnl = (open_price - price) * paired_lots * lot_multiplier

            open_commission = float(open_lot.get("commission_per_lot") or 0.0) * paired_lots
            close_commission = commission_per_lot * paired_lots
            net_pnl = gross_pnl - open_commission - close_commission
            notional = open_price * paired_lots * lot_multiplier
            origin_source_type = str(open_lot.get("origin_source_type") or "")
            pair = {
                "ticker": ticker,
                "contract_code": contract,
                "side": side,
                "lots": paired_lots,
                "open_transaction_id": open_lot.get("transaction_id"),
                "close_transaction_id": row.get("id"),
                "open_recommendation_id": open_lot.get("recommendation_id"),
                "close_recommendation_id": row.get("recommendation_id"),
                "open_source_type": open_lot.get("source_type"),
                "close_source_type": source_type,
                "origin_source_type": origin_source_type,
                "origin_recommendation_id": open_lot.get("origin_recommendation_id"),
                "origin_open_transaction_id": open_lot.get("origin_transaction_id"),
                "origin_open_date": open_lot.get("origin_open_date"),
                "strategy_originated": origin_source_type == "strategy",
                "contains_rollover": bool(
                    open_lot.get("lineage_contains_rollover") or source_type == "rollover"
                ),
                "contains_forced_risk": bool(
                    open_lot.get("lineage_contains_forced_risk") or source_type == "forced_risk"
                ),
                "contains_non_strategy": bool(
                    open_lot.get("lineage_contains_non_strategy") or source_type != "strategy"
                ),
                "open_date": open_lot.get("trading_date"),
                "close_date": _date_key(row.get("trading_date")),
                "holding_days": _holding_days(open_lot.get("trading_date"), row.get("trading_date")),
                "open_price": open_price,
                "close_price": price,
                "contract_multiplier": lot_multiplier,
                "gross_pnl": gross_pnl,
                "commission": open_commission + close_commission,
                "net_pnl": net_pnl,
                "return_on_notional": (net_pnl / notional) if notional else 0.0,
            }
            if pair["strategy_originated"]:
                strategy_pairs.append(pair)

            if source_type == "rollover" and recommendation_id:
                rollover_transfers[(recommendation_id, ticker, side)].append(
                    {
                        "lots_remaining": paired_lots,
                        "source_type": origin_source_type,
                        "recommendation_id": open_lot.get("origin_recommendation_id"),
                        "transaction_id": open_lot.get("origin_transaction_id"),
                        "trading_date": open_lot.get("origin_open_date"),
                        "contains_rollover": True,
                        "contains_forced_risk": bool(open_lot.get("lineage_contains_forced_risk")),
                        "contains_non_strategy": True,
                    }
                )

            open_lot["lots_remaining"] = int(open_lot["lots_remaining"]) - paired_lots
            remaining_to_close -= paired_lots
            if int(open_lot["lots_remaining"]) <= 0:
                open_books[book_key].popleft()

        if remaining_to_close > 0:
            unmatched_closes.append(
                {
                    "ticker": ticker,
                    "contract_code": contract,
                    "side": side,
                    "lots": remaining_to_close,
                    "close_date": _date_key(row.get("trading_date")),
                    "close_source_type": source_type,
                    "close_transaction_id": row.get("id"),
                }
            )

    return strategy_pairs, {"unmatched_closes": unmatched_closes}


def build_strategy_originated_trade_pairs(
    transactions: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return completed pairs whose opening position originated in strategy."""

    pairs, _ = build_strategy_originated_trade_pairs_with_diagnostics(transactions)
    return pairs


def summarize_trade_pairs(pairs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(pairs)
    total = len(rows)
    wins = sum(1 for row in rows if float(row.get("net_pnl") or 0.0) > 0)
    losses = sum(1 for row in rows if float(row.get("net_pnl") or 0.0) < 0)
    flats = total - wins - losses
    total_pnl = sum(float(row.get("net_pnl") or 0.0) for row in rows)
    total_commission = sum(float(row.get("commission") or 0.0) for row in rows)
    avg_return = (
        sum(float(row.get("return_on_notional") or 0.0) for row in rows) / total
        if total
        else 0.0
    )
    return {
        "total_trades": total,
        "winning_trades": wins,
        "losing_trades": losses,
        "flat_trades": flats,
        "win_rate": wins / total if total else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / total if total else 0.0,
        "total_commission": total_commission,
        "avg_commission": total_commission / total if total else 0.0,
        "avg_return": avg_return,
    }
