from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from apis.contract_info_cache import FuturesContractInfoCache
from apis.pandaai.api_model import FuturesSettlementRecord
from apis.router import APISource, Router
from graph.schema import (
    FuturesAction,
    FuturesRecommendation,
    Portfolio,
    Position,
    RecommendationAction,
    RecommendationSourceType,
    RecommendationStatus,
    TradingPhase,
)
from util.db_helper import get_db
from util.logger import logger
from util.trading_calendar import get_next_trading_day
from tools.common.futures_market_rules import check_limit_lock


class FuturesDailySettlement:
    """Futures daily settlement engine for the post-order settlement phase."""

    def __init__(self, market_type: str = "china_futures", config: Optional[Dict[str, Any]] = None):
        self.market_type = market_type
        self.config = config or {}
        self.db = get_db()
        self.router = Router(APISource.PANDAAI, market_type=market_type, config=self.config)

    def daily_settlement(self, portfolio: Portfolio, trading_date: datetime, use_main_contract: bool = False):
        raise RuntimeError("Legacy futures settlement flow is disabled. Use run_phase3(config_id, trading_date).")

    def run_phase3(self, config_id: str, trading_date: Any) -> FuturesSettlementRecord:
        trading_dt = self._normalize_date(trading_date)
        reference_portfolio = self._load_reference_portfolio(config_id)
        transactions = self._load_phase2_transactions(config_id, trading_dt)
        quote_cache = self._load_contract_quotes_for_audit(reference_portfolio, transactions, trading_dt)
        self._validate_transaction_limit_prices(transactions, quote_cache, trading_dt)

        ledgers = self._build_initial_ledgers(reference_portfolio)
        for transaction in transactions:
            self._apply_transaction(ledgers, transaction)

        settle_prices = self._fetch_contract_settle_prices(ledgers, transactions, trading_dt, quote_cache=quote_cache)
        settlement_summary = self._finalize_ledgers(reference_portfolio, ledgers, settle_prices, trading_dt)

        official_portfolio = self._persist_official_portfolio(
            config_id=config_id,
            reference_portfolio=reference_portfolio,
            portfolio=settlement_summary["portfolio"],
            trading_date=trading_dt,
        )

        settlement_record = FuturesSettlementRecord(
            trading_date=trading_dt.strftime("%Y-%m-%d"),
            previous_balance=reference_portfolio.cashflow,
            current_balance=official_portfolio.cashflow,
            previous_account_equity=settlement_summary["previous_account_equity"],
            current_account_equity=settlement_summary["current_account_equity"],
            cash_available=settlement_summary["cash_available"],
            reserved_margin=settlement_summary["reserved_margin"],
            previous_margin=reference_portfolio.margin_used,
            current_margin=official_portfolio.margin_used,
            margin_as_asset_prev=0.0,
            margin_as_asset_curr=0.0,
            daily_pnl=settlement_summary["daily_pnl"],
            deposit=0.0,
            withdraw=0.0,
            commission=settlement_summary["commission"],
            margin_ratio=official_portfolio.margin_ratio,
            is_warning=official_portfolio.risk_status == "WARNING",
            is_liquidation=official_portfolio.risk_status == "LIQUIDATION",
            positions_detail=settlement_summary["positions_detail"],
        )

        if not self.db.save_daily_settlement(official_portfolio.id, settlement_record):
            raise RuntimeError(f"Failed to save daily settlement for {trading_dt.strftime('%Y-%m-%d')}")

        self._save_ticker_daily_pnl(official_portfolio.id, trading_dt, settlement_summary["positions_detail"])
        settlement_updates = self._build_transaction_settle_price_updates(transactions, settle_prices)
        if not self.db.update_futures_transactions_settle_prices(settlement_updates):
            raise RuntimeError(
                f"Failed to backfill transaction settle prices for {trading_dt.strftime('%Y-%m-%d')}"
            )
        self.db.mark_futures_transactions_booked([transaction["id"] for transaction in transactions])
        self._detect_rollover_recommendations(config_id, official_portfolio, trading_dt)

        logger.info(
            f"Phase3 settlement completed for {trading_dt.strftime('%Y-%m-%d')}: "
            f"cash_available={official_portfolio.cashflow:,.2f}, "
            f"account_equity={settlement_summary['current_account_equity']:,.2f}, "
            f"pnl={settlement_summary['daily_pnl']:+,.2f}, "
            f"commission={settlement_summary['commission']:.2f}, "
            f"margin={settlement_summary['previous_margin']:,.2f}->{settlement_summary['current_margin']:,.2f}"
        )
        return settlement_record

    def run_phase2(self, config_id: str, trading_date: Any) -> FuturesSettlementRecord:
        """Compatibility alias for historical callers."""
        return self.run_phase3(config_id=config_id, trading_date=trading_date)

    def _normalize_date(self, trading_date: Any) -> datetime:
        if isinstance(trading_date, datetime):
            return trading_date
        return datetime.strptime(str(trading_date), "%Y-%m-%d")

    def _load_reference_portfolio(self, config_id: str) -> Portfolio:
        portfolio_dict = self.db.get_latest_settled_portfolio(config_id)
        if not portfolio_dict:
            raise RuntimeError(f"Missing settled portfolio for config {config_id}")
        return Portfolio(**portfolio_dict)

    def _load_phase2_transactions(self, config_id: str, trading_date: datetime) -> List[Dict[str, Any]]:
        transactions = self.db.get_futures_transactions_by_date(
            config_id=config_id,
            trading_date=trading_date,
            execution_phase=TradingPhase.PHASE2,
            booked_in_settlement=False,
        )
        logger.info(
            f"Loaded {len(transactions)} phase2 open-order transactions for "
            f"{trading_date.strftime('%Y-%m-%d')}"
        )
        return transactions

    def _build_initial_ledgers(self, portfolio: Portfolio) -> Dict[str, Dict[str, Any]]:
        ledgers: Dict[str, Dict[str, Any]] = {}

        for ticker, position in portfolio.positions.items():
            if position.shares == 0:
                continue

            contract_info = FuturesContractInfoCache.get_contract_info(ticker)
            if not contract_info:
                raise RuntimeError(f"Missing contract info for {ticker}")
            if not position.contract_code:
                raise RuntimeError(f"Missing contract_code in settled portfolio for {ticker}")

            side = "long" if position.shares > 0 else "short"
            margin_rate = position.margin_rate or (
                contract_info["margin_rate_long"] if side == "long" else contract_info["margin_rate_short"]
            )
            contract_multiplier = position.contract_multiplier or contract_info["contract_multiplier"]
            reference_price = position.settle_price or position.entry_price
            if reference_price is None:
                raise RuntimeError(f"Missing settle_price/entry_price in settled portfolio for {ticker}")

            ledgers[ticker] = {
                "starting_realized_pnl": getattr(position, "realized_pnl", 0.0) or 0.0,
                "realized_from_cost_today": 0.0,
                "breakdown": {
                    "holding_pnl": 0.0,
                    "new_position_pnl": 0.0,
                    "close_pnl": 0.0,
                    "commission": 0.0,
                },
                "batches": [
                    {
                        "origin": "overnight",
                        "side": side,
                        "lots": abs(position.shares),
                        "daily_reference_price": reference_price,
                        "position_entry_price": position.entry_price or reference_price,
                        "position_entry_date": position.entry_date,
                        "contract_code": position.contract_code,
                        "contract_multiplier": contract_multiplier,
                        "margin_rate": margin_rate,
                    }
                ],
            }

        return ledgers

    def _apply_transaction(self, ledgers: Dict[str, Dict[str, Any]], transaction: Dict[str, Any]) -> None:
        ticker = transaction["ticker"]
        ledger = ledgers.setdefault(
            ticker,
            {
                "starting_realized_pnl": 0.0,
                "realized_from_cost_today": 0.0,
                "breakdown": {
                    "holding_pnl": 0.0,
                    "new_position_pnl": 0.0,
                    "close_pnl": 0.0,
                    "commission": 0.0,
                },
                "batches": [],
            },
        )
        ledger["breakdown"]["commission"] += float(transaction.get("commission") or 0.0)

        action = self._action_value(transaction.get("action"))
        if action == FuturesAction.OPEN_LONG.value:
            self._append_open_batch(ledger, transaction, side="long")
        elif action == FuturesAction.OPEN_SHORT.value:
            self._append_open_batch(ledger, transaction, side="short")
        elif action == FuturesAction.CLOSE_LONG.value:
            self._consume_close(ledger, transaction, side="long")
        elif action == FuturesAction.CLOSE_SHORT.value:
            self._consume_close(ledger, transaction, side="short")

    def _append_open_batch(self, ledger: Dict[str, Any], transaction: Dict[str, Any], side: str) -> None:
        lots = abs(int(transaction.get("lots") or 0))
        if lots == 0:
            return

        remaining_side = self._remaining_side(ledger["batches"])
        if remaining_side and remaining_side != side:
            raise RuntimeError("Phase3 settlement replay detected opposite-direction position before open transaction")

        contract_code = transaction.get("contract_code")
        contract_multiplier = float(transaction.get("contract_multiplier") or 0.0)
        margin_rate = float(transaction.get("margin_rate") or 0.0)
        if not contract_code or contract_multiplier <= 0 or margin_rate <= 0:
            raise RuntimeError("Phase3 settlement replay requires contract_code, contract_multiplier, and margin_rate")

        execution_price = float(transaction["execution_price"])
        raw_trading_date = transaction.get("trading_date")
        if hasattr(raw_trading_date, "strftime"):
            entry_date = raw_trading_date.strftime("%Y-%m-%d")
        elif raw_trading_date:
            entry_date = str(raw_trading_date)[:10]
        else:
            entry_date = None
        ledger["batches"].append(
            {
                "origin": "today",
                "side": side,
                "lots": lots,
                "daily_reference_price": execution_price,
                "position_entry_price": execution_price,
                "position_entry_date": entry_date,
                "contract_code": contract_code,
                "contract_multiplier": contract_multiplier,
                "margin_rate": margin_rate,
            }
        )

    def _consume_close(self, ledger: Dict[str, Any], transaction: Dict[str, Any], side: str) -> None:
        lots_to_close = abs(int(transaction.get("lots") or 0))
        if lots_to_close == 0:
            return

        execution_price = float(transaction["execution_price"])
        new_batches: List[Dict[str, Any]] = []

        for batch in ledger["batches"]:
            if lots_to_close == 0:
                new_batches.append(batch)
                continue

            if batch["side"] != side:
                new_batches.append(batch)
                continue

            matched_lots = min(batch["lots"], lots_to_close)
            if matched_lots > 0:
                multiplier = float(batch["contract_multiplier"])
                if side == "long":
                    close_pnl = (execution_price - float(batch["daily_reference_price"])) * matched_lots * multiplier
                    realized_cost_pnl = (
                        execution_price - float(batch["position_entry_price"])
                    ) * matched_lots * multiplier
                else:
                    close_pnl = (float(batch["daily_reference_price"]) - execution_price) * matched_lots * multiplier
                    realized_cost_pnl = (
                        float(batch["position_entry_price"]) - execution_price
                    ) * matched_lots * multiplier

                ledger["breakdown"]["close_pnl"] += close_pnl
                ledger["realized_from_cost_today"] += realized_cost_pnl
                batch["lots"] -= matched_lots
                lots_to_close -= matched_lots

            if batch["lots"] > 0:
                new_batches.append(batch)

        if lots_to_close != 0:
            raise RuntimeError("Phase3 settlement replay detected close transaction larger than position size")

        ledger["batches"] = new_batches

    def _fetch_contract_settle_prices(
        self,
        ledgers: Dict[str, Dict[str, Any]],
        transactions: List[Dict[str, Any]],
        trading_date: datetime,
        quote_cache: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, float]:
        settle_prices: Dict[str, float] = {}
        quote_cache = quote_cache or {}

        contract_codes = set()
        for ledger in ledgers.values():
            for batch in ledger["batches"]:
                contract_code = batch.get("contract_code")
                if contract_code:
                    contract_codes.add(contract_code)

        for transaction in transactions:
            contract_code = transaction.get("contract_code")
            if contract_code:
                contract_codes.add(contract_code)

        for contract_code in contract_codes:
            if contract_code in settle_prices:
                continue

            quote = quote_cache.get(contract_code)
            if quote is None:
                quote = self.router.get_futures_contract_quote_on_date(contract_code, trading_date)
            if quote is None or quote.settle_price is None or float(quote.settle_price) <= 0:
                raise RuntimeError(
                    "Missing official same-day settlePrice for contract "
                    f"{contract_code} on {trading_date.strftime('%Y-%m-%d')}"
                )
            settle_prices[contract_code] = float(quote.settle_price)

        return settle_prices

    def _load_contract_quotes_for_audit(
        self,
        reference_portfolio: Portfolio,
        transactions: List[Dict[str, Any]],
        trading_date: datetime,
    ) -> Dict[str, Any]:
        contract_codes = set()
        for position in reference_portfolio.positions.values():
            contract_code = getattr(position, "contract_code", None)
            if contract_code:
                contract_codes.add(contract_code)
        for transaction in transactions:
            contract_code = transaction.get("contract_code")
            if contract_code:
                contract_codes.add(contract_code)

        quotes: Dict[str, Any] = {}
        for contract_code in contract_codes:
            try:
                quotes[contract_code] = self.router.get_futures_contract_quote_on_date(contract_code, trading_date)
            except Exception as exc:
                logger.warning(
                    f"Phase3 quote audit unavailable for {contract_code} on {trading_date.strftime('%Y-%m-%d')}: "
                    f"{self._short_error(exc)}"
                )
                quotes[contract_code] = None
        return quotes

    def _validate_transaction_limit_prices(
        self,
        transactions: List[Dict[str, Any]],
        quote_cache: Dict[str, Any],
        trading_date: datetime,
    ) -> None:
        limit_cfg = (self.config.get("execution") or {}).get("limit_lock") or {}
        if not bool(limit_cfg.get("enabled", True)):
            return
        for transaction in transactions:
            action = transaction.get("action")
            contract_code = transaction.get("contract_code")
            quote = quote_cache.get(contract_code)
            contract_info = FuturesContractInfoCache.get_contract_info(transaction.get("ticker"))
            minimum_tick = float((contract_info or {}).get("minimum_tick", 0.0) or 0.0)
            audit = check_limit_lock(
                action=action,
                execution_price=float(transaction.get("execution_price") or 0.0),
                quote=quote,
                minimum_tick=minimum_tick,
                tolerance_ticks=int(limit_cfg.get("tolerance_ticks", 0) or 0),
                enabled=True,
            )
            if audit.get("blocked"):
                raise RuntimeError(
                    "Phase3 limit audit detected impossible execution: "
                    f"transaction={transaction.get('id')} contract={contract_code} "
                    f"date={trading_date.strftime('%Y-%m-%d')} audit={audit}"
                )

    def _short_error(self, exc: Exception, limit: int = 280) -> str:
        text = str(exc).replace("\r", " ").replace("\n", " ")
        if len(text) > limit:
            return text[:limit] + "..."
        return text

    def _finalize_ledgers(
        self,
        reference_portfolio: Portfolio,
        ledgers: Dict[str, Dict[str, Any]],
        settle_prices: Dict[str, float],
        trading_date: datetime,
    ) -> Dict[str, Any]:
        positions_detail: Dict[str, Dict[str, Any]] = {}
        final_positions: Dict[str, Position] = {}
        total_daily_pnl = 0.0
        total_commission = 0.0
        total_margin = 0.0

        for ticker, ledger in ledgers.items():
            breakdown = deepcopy(ledger["breakdown"])
            remaining_batches = [batch for batch in ledger["batches"] if batch["lots"] > 0]
            remaining_lots = sum(batch["lots"] for batch in remaining_batches)
            position_side = self._remaining_side(remaining_batches)
            settle_price = None
            entry_price = None
            entry_date = None
            contract_code = None
            margin_rate = 0.0
            multiplier = 0.0

            if remaining_batches:
                contract_codes = {batch["contract_code"] for batch in remaining_batches}
                if len(contract_codes) != 1:
                    raise RuntimeError(f"Phase3 settlement cannot persist multiple contracts under one ticker key: {ticker}")
                contract_code = next(iter(contract_codes))
                settle_price = settle_prices[contract_code]
                margin_rate = float(remaining_batches[0]["margin_rate"])
                multiplier = float(remaining_batches[0]["contract_multiplier"])

                total_entry_value = 0.0
                total_entry_lots = 0
                for batch in remaining_batches:
                    if batch["origin"] == "overnight":
                        pnl_value = self._mark_to_market_pnl(
                            side=batch["side"],
                            start_price=float(batch["daily_reference_price"]),
                            end_price=settle_price,
                            lots=batch["lots"],
                            multiplier=float(batch["contract_multiplier"]),
                        )
                        breakdown["holding_pnl"] += pnl_value
                    else:
                        pnl_value = self._mark_to_market_pnl(
                            side=batch["side"],
                            start_price=float(batch["daily_reference_price"]),
                            end_price=settle_price,
                            lots=batch["lots"],
                            multiplier=float(batch["contract_multiplier"]),
                        )
                        breakdown["new_position_pnl"] += pnl_value

                    total_entry_value += float(batch["position_entry_price"]) * batch["lots"]
                    total_entry_lots += batch["lots"]

                entry_price = total_entry_value / total_entry_lots if total_entry_lots else None
                entry_dates = [
                    str(batch.get("position_entry_date"))[:10]
                    for batch in remaining_batches
                    if batch.get("position_entry_date")
                ]
                entry_date = min(entry_dates) if entry_dates else None
                shares = remaining_lots if position_side == "long" else -remaining_lots
                margin_used = settle_price * remaining_lots * multiplier * margin_rate
                total_margin += margin_used

                final_positions[ticker] = Position(
                    shares=shares,
                    value=remaining_lots * settle_price * multiplier,
                    entry_price=entry_price,
                    contract_code=contract_code,
                    settle_price=settle_price,
                    current_settle_price=settle_price,
                    margin_used=margin_used,
                    margin_rate=margin_rate,
                    contract_multiplier=multiplier,
                    entry_date=entry_date,
                    unrealized_pnl=0.0,
                    realized_pnl=ledger["starting_realized_pnl"] + ledger["realized_from_cost_today"],
                )
            else:
                ref_position = reference_portfolio.positions.get(ticker)
                if ref_position:
                    multiplier = float(ref_position.contract_multiplier or 0.0)

            ticker_total_pnl = breakdown["holding_pnl"] + breakdown["new_position_pnl"] + breakdown["close_pnl"]
            total_daily_pnl += ticker_total_pnl
            total_commission += breakdown["commission"]

            positions_detail[ticker] = {
                "ticker": ticker,
                "contract_code": contract_code,
                "position_type": position_side.upper() if position_side else "FLAT",
                "lots": remaining_lots,
                "entry_price": entry_price or 0.0,
                "settle_price": settle_price or 0.0,
                "holding_pnl": breakdown["holding_pnl"],
                "new_position_pnl": breakdown["new_position_pnl"],
                "close_pnl": breakdown["close_pnl"],
                "commission": breakdown["commission"],
                "total_pnl": ticker_total_pnl,
                "contract_multiplier": multiplier,
            }

        previous_balance = float(reference_portfolio.cashflow or 0.0)
        previous_margin = float(reference_portfolio.margin_used or 0.0)
        current_margin = float(total_margin)
        previous_account_equity = previous_balance + previous_margin
        current_account_equity = previous_account_equity + total_daily_pnl - total_commission
        current_balance = current_account_equity - current_margin
        cash_available = current_balance
        reserved_margin = current_margin
        margin_ratio = (
            reserved_margin / current_account_equity
            if current_account_equity > 0
            else (1.0 if reserved_margin > 0 else 0.0)
        )
        risk_status = "LIQUIDATION" if margin_ratio >= 0.85 else "WARNING" if margin_ratio >= 0.70 else "NORMAL"

        self._assert_accounting_invariants(
            previous_cash_available=previous_balance,
            current_cash_available=current_balance,
            previous_reserved_margin=previous_margin,
            current_reserved_margin=current_margin,
            previous_account_equity=previous_account_equity,
            current_account_equity=current_account_equity,
            daily_pnl=total_daily_pnl,
            commission=total_commission,
        )

        portfolio = Portfolio(
            id=reference_portfolio.id,
            cashflow=round(current_balance, 2),
            account_equity=round(current_account_equity, 2),
            cash_available=round(cash_available, 2),
            positions=final_positions,
            margin_used=round(total_margin, 2),
            margin_available=round(cash_available, 2),
            margin_ratio=margin_ratio,
            daily_settlement_pnl=round(total_daily_pnl, 2),
            risk_status=risk_status,
            last_settle_date=trading_date.strftime("%Y-%m-%d"),
            is_settled=True,
        )

        return {
            "portfolio": portfolio,
            "daily_pnl": round(total_daily_pnl, 2),
            "commission": round(total_commission, 2),
            "previous_margin": round(previous_margin, 2),
            "current_margin": round(current_margin, 2),
            "previous_account_equity": round(previous_account_equity, 2),
            "current_account_equity": round(current_account_equity, 2),
            "cash_available": round(cash_available, 2),
            "reserved_margin": round(reserved_margin, 2),
            "positions_detail": positions_detail,
        }

    def _assert_accounting_invariants(
        self,
        *,
        previous_cash_available: float,
        current_cash_available: float,
        previous_reserved_margin: float,
        current_reserved_margin: float,
        previous_account_equity: float,
        current_account_equity: float,
        daily_pnl: float,
        commission: float,
    ) -> None:
        net_pnl = float(daily_pnl or 0.0) - float(commission or 0.0)
        equity_change = float(current_account_equity or 0.0) - float(previous_account_equity or 0.0)
        cash_change = float(current_cash_available or 0.0) - float(previous_cash_available or 0.0)
        margin_change = float(current_reserved_margin or 0.0) - float(previous_reserved_margin or 0.0)
        expected_cash_change = net_pnl - margin_change

        if abs(equity_change - net_pnl) > 0.01:
            raise RuntimeError(
                "Settlement equity invariant failed: "
                f"equity_change={equity_change:.2f}, pnl_minus_commission={net_pnl:.2f}"
            )
        if abs(cash_change - expected_cash_change) > 0.01:
            raise RuntimeError(
                "Settlement cash invariant failed: "
                f"cash_change={cash_change:.2f}, expected={expected_cash_change:.2f}"
            )
        if abs((current_cash_available + current_reserved_margin) - current_account_equity) > 0.01:
            raise RuntimeError(
                "Settlement account split invariant failed: "
                f"cash_available={current_cash_available:.2f}, "
                f"reserved_margin={current_reserved_margin:.2f}, "
                f"account_equity={current_account_equity:.2f}"
            )

    def _persist_official_portfolio(
        self,
        config_id: str,
        reference_portfolio: Portfolio,
        portfolio: Portfolio,
        trading_date: datetime,
    ) -> Portfolio:
        portfolio_row = self.db.get_or_create_portfolio_for_date(
            config_id=config_id,
            portfolio=reference_portfolio.model_dump(),
            trading_date=trading_date,
        )
        if not portfolio_row:
            raise RuntimeError(f"Failed to create official portfolio for {trading_date.strftime('%Y-%m-%d')}")

        portfolio_dict = portfolio.model_dump()
        portfolio_dict["id"] = portfolio_row["id"]
        if not self.db.update_portfolio(config_id, portfolio_dict, trading_date):
            raise RuntimeError(f"Failed to update official portfolio for {trading_date.strftime('%Y-%m-%d')}")
        return Portfolio(**portfolio_dict)

    def _save_ticker_daily_pnl(
        self,
        portfolio_id: str,
        trading_date: datetime,
        positions_detail: Dict[str, Dict[str, Any]],
    ) -> None:
        trading_date_value = trading_date.strftime("%Y-%m-%d")

        for ticker, detail in positions_detail.items():
            record = {
                "portfolio_id": portfolio_id,
                "trading_date": trading_date_value,
                "ticker": ticker,
                "daily_pnl": detail["total_pnl"],
                "commission": detail["commission"],
                "holding_pnl": detail.get("holding_pnl", 0.0),
                "new_position_pnl": detail.get("new_position_pnl", 0.0),
                "close_pnl": detail.get("close_pnl", 0.0),
                "position_type": detail["position_type"],
                "lots": detail["lots"],
                "entry_price": detail["entry_price"],
                "settle_price": detail["settle_price"],
            }
            self.db.save_ticker_daily_pnl(record)

    def _build_transaction_settle_price_updates(
        self,
        transactions: List[Dict[str, Any]],
        settle_prices: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        updates: List[Dict[str, Any]] = []
        for transaction in transactions:
            contract_code = transaction.get("contract_code")
            settle_price = settle_prices.get(contract_code)
            if not contract_code or settle_price is None:
                continue
            updates.append(
                {
                    "id": transaction.get("id"),
                    "settle_price": settle_price,
                }
            )
        return updates

    def _detect_rollover_recommendations(
        self,
        config_id: str,
        portfolio: Portfolio,
        trading_date: datetime,
    ) -> None:
        existing_rollovers_cache: Dict[str, set] = {}

        for ticker, position in portfolio.positions.items():
            if position.shares == 0 or not position.contract_code:
                continue

            try:
                next_trading_date = get_next_trading_day(
                    router=self.router,
                    trading_date=trading_date,
                    underlying_code=ticker,
                ).strftime("%Y-%m-%d")
            except Exception as exc:
                logger.warning(
                    f"Rollover detection skipped for {ticker} on {trading_date.strftime('%Y-%m-%d')}: "
                    f"next trading day provider unavailable: {self._short_error(exc)}"
                )
                continue
            if next_trading_date not in existing_rollovers_cache:
                existing_rollovers = self.db.get_futures_recommendations_by_effective_date(
                    config_id=config_id,
                    effective_trade_date=next_trading_date,
                    source_type=RecommendationSourceType.ROLLOVER,
                    status=RecommendationStatus.PENDING,
                )
                existing_rollovers_cache[next_trading_date] = {
                    (
                        item.get("underlying_code"),
                        item.get("from_contract"),
                        item.get("to_contract"),
                    )
                    for item in existing_rollovers
                }

            try:
                main_quote = self.router.get_futures_main_contract_quote_on_date(ticker, trading_date)
            except Exception as exc:
                logger.warning(
                    f"Rollover detection skipped for {ticker} on {trading_date.strftime('%Y-%m-%d')}: "
                    f"main-contract provider unavailable: {self._short_error(exc)}"
                )
                continue
            if main_quote is None or not main_quote.ticker:
                continue
            if main_quote.ticker == position.contract_code:
                continue

            dedupe_key = (ticker, position.contract_code, main_quote.ticker)
            if dedupe_key in existing_rollovers_cache[next_trading_date]:
                continue

            recommendation = FuturesRecommendation(
                config_id=config_id,
                reference_portfolio_id=portfolio.id,
                trading_date=trading_date.strftime("%Y-%m-%d"),
                effective_trade_date=next_trading_date,
                source_type=RecommendationSourceType.ROLLOVER,
                underlying_code=ticker,
                from_contract=position.contract_code,
                to_contract=main_quote.ticker,
                action=RecommendationAction.ROLLOVER,
                lots=abs(position.shares),
                slippage_model="tick",
                slippage_ticks=0,
                slippage_amount=0.0,
                justification=(
                    f"Main contract rollover scheduled: {position.contract_code} -> {main_quote.ticker}"
                ),
                status=RecommendationStatus.PENDING,
            )
            recommendation_id = self.db.save_futures_recommendation(recommendation)
            if not recommendation_id:
                raise RuntimeError(
                    f"Failed to save rollover recommendation for {ticker} on {next_trading_date}"
                )
            existing_rollovers_cache[next_trading_date].add(dedupe_key)
            logger.info(
                f"Created rollover recommendation for {ticker}: {position.contract_code} -> "
                f"{main_quote.ticker}, effective_trade_date={next_trading_date}"
            )

    def _remaining_side(self, batches: List[Dict[str, Any]]) -> Optional[str]:
        for batch in batches:
            if batch["lots"] > 0:
                return batch["side"]
        return None

    def _mark_to_market_pnl(
        self,
        side: str,
        start_price: float,
        end_price: float,
        lots: int,
        multiplier: float,
    ) -> float:
        if side == "long":
            return (end_price - start_price) * lots * multiplier
        return (start_price - end_price) * lots * multiplier

    def _action_value(self, action: Any) -> str:
        return action.value if hasattr(action, "value") else str(action)
