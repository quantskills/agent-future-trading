from __future__ import annotations

from typing import Any, Dict, List, Optional

from apis.contract_info_cache import FuturesContractInfoCache
from apis.router import APISource, Router
from tools.agent_tools.execution.futures_commission import (
    calculate_commission,
    classify_offset_scope,
    resolve_commission_rule,
)
from tools.agent_tools.execution.intraday_execution import (
    intraday_confirmation_enabled,
    resolve_intraday_execution_basis,
)
from tools.agent_tools.execution.futures_market_rules import (
    check_contract_expiry_guard,
    check_limit_lock,
    normalize_margin_rate,
)
from graph.schema import (
    FuturesAction,
    FuturesTransaction,
    Position,
    RecommendationAction,
    RecommendationSourceType,
    RecommendationStatus,
    TradingPhase,
)
from util.futures_audit import (
    add_rewrite_reason,
    build_actual_transactions,
    build_audit_payload,
    calculate_margin_audit,
    categorize_no_trade_reason,
    ensure_execution_translation,
    ensure_signal_snapshot,
    extract_signal_lifecycle,
    infer_no_trade_reason,
    set_execution_result,
)
from util.logger import logger


class ExecutionBlocked(RuntimeError):
    """Execution skipped by market/business rules before a transaction is booked."""

    def __init__(self, reason: str, message: str, audit_payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.reason = reason
        self.audit_payload = audit_payload or {}
        self.warning_message = message


class FuturesExecutionEngine:
    """Execute phase2 open-order recommendations and update the intraday portfolio."""

    def __init__(self, config: Dict[str, Any], db):
        self.config = config
        self.db = db
        self.router = Router(APISource.PANDAAI, market_type="china_futures", config=config)
        self.execution_config = config.get("execution", {})
        self._execution_quote_cache: Dict[tuple, Any] = {}
        self._contract_detail_cache: Dict[tuple, Optional[Dict[str, Any]]] = {}
        self._dynamic_margin_cache: Dict[tuple, Dict[str, Any]] = {}

    def execute_pending_rollovers(self, config_id: str, trading_date, portfolio, execution_phase: TradingPhase):
        recommendations = self.db.get_futures_recommendations_by_effective_date(
            config_id=config_id,
            effective_trade_date=trading_date,
            source_type=RecommendationSourceType.ROLLOVER.value,
            status=RecommendationStatus.PENDING.value,
        )

        if recommendations:
            logger.info(
                f"Executing {len(recommendations)} pending rollover recommendation(s) for "
                f"{self._normalize_date_value(trading_date)}"
            )

        for recommendation in recommendations:
            recommendation_id = recommendation.get("id")
            portfolio = self.execute_recommendation(
                recommendation_id=recommendation_id,
                recommendation=recommendation,
                portfolio=portfolio,
                trading_date=trading_date,
                execution_phase=execution_phase,
            )

        return portfolio

    def execute_recommendation(
        self,
        recommendation_id: str,
        recommendation: Any,
        portfolio,
        trading_date,
        execution_phase: TradingPhase,
    ):
        recommendation_dict = self._to_dict(recommendation)
        recommendation_dict["id"] = recommendation_id
        snapshot = ensure_signal_snapshot(recommendation_dict.get("signal_snapshot"))
        signal_lifecycle = extract_signal_lifecycle(snapshot)
        if signal_lifecycle:
            ensure_execution_translation(snapshot)["signal_lifecycle"] = signal_lifecycle
        action_value = self._enum_value(recommendation_dict.get("action"))
        warning_message = recommendation_dict.get("warning_message")

        if action_value == RecommendationAction.HOLD.value or recommendation_dict.get("lots", 0) == 0:
            no_trade_reason = infer_no_trade_reason(
                snapshot,
                warning_message=warning_message,
                default="hold_or_zero_lots",
            )
            set_execution_result(
                snapshot,
                outcome="executed_without_transaction",
                status=RecommendationStatus.SKIPPED.value,
                transaction_count=0,
                no_trade_reason=no_trade_reason,
                warning_message=warning_message,
            )
            self.db.update_futures_recommendation_status(
                recommendation_id,
                RecommendationStatus.SKIPPED.value,
                execution_price=None,
                warning_message=no_trade_reason or warning_message,
                signal_snapshot=snapshot,
                audit_payload=build_audit_payload(snapshot),
            )
            self._log_non_transaction_execution(
                recommendation=recommendation_dict,
                execution_phase=execution_phase,
                outcome="executed_without_transaction",
                reason=no_trade_reason or "hold_or_zero_lots",
            )
            return portfolio

        if self._enum_value(recommendation_dict.get("source_type")) == RecommendationSourceType.ROLLOVER.value:
            snapshot["rollover_policy"] = {
                "mode": recommendation_dict.get("rollover_mode", "reconcile_with_strategy"),
                "reason": recommendation_dict.get("rollover_policy_reason"),
                "execution_type": recommendation_dict.get("rollover_execution_type"),
                "strategy_target_lots": recommendation_dict.get("rollover_strategy_target_lots"),
                "close_lots": recommendation_dict.get("rollover_close_lots"),
                "open_lots": recommendation_dict.get("rollover_open_lots"),
                "from_contract": recommendation_dict.get("from_contract"),
                "to_contract": recommendation_dict.get("to_contract"),
            }
            try:
                transactions = self._expand_rollover_recommendation(recommendation_dict, portfolio, trading_date, execution_phase)
            except ExecutionBlocked as exc:
                return self._mark_execution_blocked(
                    recommendation_id=recommendation_id,
                    recommendation=recommendation_dict,
                    portfolio=portfolio,
                    snapshot=snapshot,
                    execution_phase=execution_phase,
                    blocked=exc,
                )
        else:
            try:
                transactions = [self._build_transaction(recommendation_dict, portfolio, trading_date, execution_phase)]
            except ExecutionBlocked as exc:
                return self._mark_execution_blocked(
                    recommendation_id=recommendation_id,
                    recommendation=recommendation_dict,
                    portfolio=portfolio,
                    snapshot=snapshot,
                    execution_phase=execution_phase,
                    blocked=exc,
                )
            except RuntimeError as exc:
                if "Pending rollover required for" not in str(exc):
                    raise
                warning_message = str(exc)
                logger.warning(warning_message)
                set_execution_result(
                    snapshot,
                    outcome="skipped",
                    status=RecommendationStatus.SKIPPED.value,
                    transaction_count=0,
                    no_trade_reason="pending_rollover_required",
                    warning_message=warning_message,
                )
                self.db.update_futures_recommendation_status(
                    recommendation_id,
                    RecommendationStatus.SKIPPED.value,
                    warning_message=warning_message,
                    signal_snapshot=snapshot,
                    audit_payload=build_audit_payload(snapshot),
                )
                self._log_non_transaction_execution(
                    recommendation=recommendation_dict,
                    execution_phase=execution_phase,
                    outcome="skipped",
                    reason=warning_message,
                )
                return portfolio

        if not transactions:
            if warning_message:
                logger.warning(
                    f"Phase2 skipped {recommendation_dict.get('underlying_code')} because no executable basis is available: "
                    f"{warning_message}"
                )
            no_trade_reason = infer_no_trade_reason(
                snapshot,
                warning_message=warning_message,
                default="missing_execution_basis" if warning_message else "no_executable_basis",
            )
            set_execution_result(
                snapshot,
                outcome="skipped",
                status=RecommendationStatus.SKIPPED.value,
                transaction_count=0,
                no_trade_reason=no_trade_reason,
                warning_message=warning_message,
            )
            self.db.update_futures_recommendation_status(
                recommendation_id,
                RecommendationStatus.SKIPPED.value,
                warning_message=warning_message,
                signal_snapshot=snapshot,
                audit_payload=build_audit_payload(snapshot),
            )
            self._log_non_transaction_execution(
                recommendation=recommendation_dict,
                execution_phase=execution_phase,
                outcome="skipped",
                reason=no_trade_reason or warning_message or "no_executable_basis",
            )
            return portfolio

        last_execution_price = None
        for transaction in transactions:
            self._attach_margin_audit(transaction, portfolio)
            transaction_id = self.db.save_futures_transaction(transaction)
            if not transaction_id:
                raise RuntimeError(f"Failed to save futures transaction for recommendation {recommendation_id}")

            last_execution_price = transaction.execution_price
            portfolio = self.apply_transaction_to_portfolio(portfolio, transaction)
            self._log_transaction_execution(transaction, execution_phase)

        recommendation_transactions = self._get_recommendation_transactions(
            config_id=recommendation_dict["config_id"],
            recommendation_id=recommendation_id,
            trading_date=trading_date,
            execution_phase=execution_phase,
        )
        execution_basis = self._final_execution_basis_payload(
            recommendation=recommendation_dict,
            transactions=recommendation_transactions,
            last_execution_price=last_execution_price,
        )
        ensure_execution_translation(snapshot)["final_execution_basis"] = execution_basis
        set_execution_result(
            snapshot,
            outcome="executed",
            status=RecommendationStatus.EXECUTED.value,
            transaction_count=len(recommendation_transactions),
            actual_transactions=build_actual_transactions(recommendation_transactions),
            warning_message=warning_message,
        )
        self.db.update_futures_recommendation_status(
            recommendation_id,
            RecommendationStatus.EXECUTED.value,
            execution_price=last_execution_price,
            signal_snapshot=snapshot,
            audit_payload=build_audit_payload(snapshot),
            base_price=recommendation_dict.get("base_price"),
            base_price_source=recommendation_dict.get("base_price_source"),
            base_price_date=recommendation_dict.get("base_price_date"),
            open_price=recommendation_dict.get("open_price"),
            prev_close_price=recommendation_dict.get("prev_close_price"),
            slippage_model=recommendation_dict.get("slippage_model") or self.execution_config.get("slippage_model", "tick"),
            slippage_ticks=self._get_slippage_ticks(recommendation_dict.get("underlying_code")),
            slippage_amount=execution_basis.get("slippage_amount"),
        )
        return portfolio

    def _final_execution_basis_payload(
        self,
        *,
        recommendation: Dict[str, Any],
        transactions: List[Dict[str, Any]],
        last_execution_price: Optional[float],
    ) -> Dict[str, Any]:
        transaction = transactions[-1] if transactions else {}
        return {
            "base_price": recommendation.get("base_price"),
            "base_price_source": self._enum_value(recommendation.get("base_price_source")),
            "base_price_date": recommendation.get("base_price_date"),
            "open_price": recommendation.get("open_price"),
            "prev_close_price": recommendation.get("prev_close_price"),
            "execution_price": last_execution_price,
            "execution_price_basis": transaction.get("execution_price_basis"),
            "slippage_model": transaction.get("slippage_model") or self.execution_config.get("slippage_model", "tick"),
            "slippage_ticks": transaction.get("slippage_ticks"),
            "slippage_amount": transaction.get("slippage_amount"),
            "intraday_execution": (
                (recommendation.get("signal_snapshot") or {})
                .get("execution_translation", {})
                .get("intraday_execution")
                if isinstance(recommendation.get("signal_snapshot"), dict)
                else None
            ),
            "execution_learning_fields": self._execution_learning_fields(
                (
                    (recommendation.get("signal_snapshot") or {})
                    .get("execution_translation", {})
                    .get("intraday_execution")
                    if isinstance(recommendation.get("signal_snapshot"), dict)
                    else None
                )
            ),
            "signal_lifecycle": extract_signal_lifecycle(
                recommendation.get("signal_snapshot") if isinstance(recommendation.get("signal_snapshot"), dict) else {}
            ),
        }

    @staticmethod
    def _execution_learning_fields(intraday_audit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        audit = intraday_audit if isinstance(intraday_audit, dict) else {}
        return {
            "trigger_checked": bool(audit.get("trigger_checked")),
            "trigger_passed": bool(audit.get("trigger_passed")),
            "price_chase_check": audit.get("price_chase_check") if isinstance(audit.get("price_chase_check"), dict) else {},
            "execution_failure_reason": str(audit.get("execution_failure_reason") or ""),
            "missed_opportunity_flag": bool(audit.get("missed_opportunity_flag")),
        }

    def _expand_rollover_recommendation(
        self,
        recommendation: Dict[str, Any],
        portfolio,
        trading_date,
        execution_phase: TradingPhase,
    ) -> List[FuturesTransaction]:
        underlying_code = recommendation["underlying_code"]
        from_contract = recommendation.get("from_contract")
        to_contract = recommendation.get("to_contract")
        lots = int(recommendation.get("lots", 0) or 0)
        close_lots = int(recommendation.get("rollover_close_lots", lots) or 0)
        open_lots = int(recommendation.get("rollover_open_lots", lots) or 0)

        if not from_contract or not to_contract or close_lots <= 0:
            return []

        close_action = RecommendationAction.CLOSE_LONG if (
            portfolio.positions.get(underlying_code) is not None
            and portfolio.positions.get(underlying_code).shares > 0
        ) else RecommendationAction.CLOSE_SHORT
        open_action = RecommendationAction.OPEN_LONG if close_action == RecommendationAction.CLOSE_LONG else RecommendationAction.OPEN_SHORT

        close_basis = self._resolve_phase2_basis(
            underlying_code=underlying_code,
            trading_date=trading_date,
            contract_code=from_contract,
            action=close_action,
            force_immediate=True,
        )
        open_basis = None
        if open_lots > 0:
            open_basis = self._resolve_phase2_basis(
                underlying_code=underlying_code,
                trading_date=trading_date,
                contract_code=to_contract,
                action=open_action,
                force_immediate=True,
            )

        if close_basis.base_price is None or (open_basis is not None and open_basis.base_price is None):
            logger.warning(
                f"Phase2 rollover skipped for {underlying_code}: "
                f"close_basis={close_basis.warning_message}, "
                f"open_basis={open_basis.warning_message if open_basis is not None else 'not_required'}"
            )
            return []

        position = portfolio.positions.get(underlying_code)
        if position is None or position.shares == 0:
            return []

        close_recommendation = recommendation.copy()
        close_recommendation["contract_code"] = from_contract
        close_recommendation["action"] = close_action
        close_recommendation["lots"] = close_lots
        close_recommendation["base_price"] = close_basis.base_price
        close_recommendation["base_price_source"] = close_basis.base_price_source
        close_recommendation["base_price_date"] = close_basis.base_price_date
        close_recommendation["open_price"] = close_basis.open_price
        close_recommendation["prev_close_price"] = close_basis.prev_close_price
        close_recommendation["warning_message"] = close_basis.warning_message
        close_recommendation["rollover_leg"] = "close_old_contract"

        transactions = [
            self._build_transaction(close_recommendation, portfolio, trading_date, execution_phase),
        ]

        if open_lots > 0 and open_basis is not None:
            open_recommendation = recommendation.copy()
            open_recommendation["contract_code"] = to_contract
            open_recommendation["action"] = open_action
            open_recommendation["lots"] = open_lots
            open_recommendation["base_price"] = open_basis.base_price
            open_recommendation["base_price_source"] = open_basis.base_price_source
            open_recommendation["base_price_date"] = open_basis.base_price_date
            open_recommendation["open_price"] = open_basis.open_price
            open_recommendation["prev_close_price"] = open_basis.prev_close_price
            open_recommendation["warning_message"] = open_basis.warning_message
            open_recommendation["rollover_leg"] = "open_new_contract"
            transactions.append(
                self._build_transaction(open_recommendation, portfolio, trading_date, execution_phase)
            )

        return transactions

    def _build_transaction(
        self,
        recommendation: Dict[str, Any],
        portfolio,
        trading_date,
        execution_phase: TradingPhase,
    ) -> FuturesTransaction:
        underlying_code = recommendation["underlying_code"]
        action_value = self._enum_value(recommendation.get("action"))
        contract_code = self._resolve_transaction_contract_code(
            underlying_code=underlying_code,
            action_value=action_value,
            recommendation=recommendation,
            portfolio=portfolio,
            trading_date=trading_date,
        )

        contract_info = FuturesContractInfoCache.get_contract_info(underlying_code)
        if not contract_info:
            raise RuntimeError(f"Missing contract info for {underlying_code}")

        self._guard_against_contract_mixing(
            underlying_code=underlying_code,
            contract_code=contract_code,
            action_value=action_value,
            recommendation=recommendation,
            portfolio=portfolio,
        )

        is_buy_like = action_value in {
            RecommendationAction.OPEN_LONG.value,
            RecommendationAction.CLOSE_SHORT.value,
        }

        base_price = recommendation.get("base_price")
        if base_price is None:
            raise RuntimeError(f"Recommendation {recommendation.get('id')} has no base price")

        warning_message = recommendation.get("warning_message")
        if warning_message:
            logger.warning(f"Phase2 execution basis warning for {underlying_code}: {warning_message}")

        lots = int(recommendation.get("lots", 0))
        slippage_ticks = self._get_slippage_ticks(underlying_code)
        slippage_amount = self._calculate_slippage_amount(contract_info, slippage_ticks)
        execution_price = base_price + slippage_amount if is_buy_like else base_price - slippage_amount
        execution_quote = self._get_execution_quote(contract_code, trading_date)
        contract_detail = self._get_contract_detail(contract_code, trading_date)
        market_rules_audit = self._build_market_rules_audit(
            recommendation=recommendation,
            action_value=action_value,
            contract_code=contract_code,
            trading_date=trading_date,
            execution_price=execution_price,
            contract_info=contract_info,
            execution_quote=execution_quote,
            contract_detail=contract_detail,
        )
        self._raise_if_market_rule_blocked(underlying_code, market_rules_audit)

        margin_rate, margin_source_audit = self._resolve_dynamic_margin_rate(
            action_value=action_value,
            contract_code=contract_code,
            contract_info=contract_info,
            trading_date=trading_date,
            contract_detail=contract_detail,
        )

        futures_action = self._to_futures_action(action_value)
        intraday_close = self._is_intraday_close(
            underlying_code=underlying_code,
            action_value=action_value,
            portfolio=portfolio,
            trading_date=trading_date,
        )
        commission = self._calculate_commission(
            underlying_code=underlying_code,
            action_value=action_value,
            execution_price=execution_price,
            lots=lots,
            contract_multiplier=contract_info["contract_multiplier"],
            intraday_close=intraday_close,
        )

        return FuturesTransaction(
            portfolio_id=portfolio.id,
            config_id=recommendation["config_id"],
            recommendation_id=recommendation.get("id"),
            trading_date=self._normalize_date_value(trading_date),
            ticker=underlying_code,
            contract_code=contract_code,
            action=futures_action,
            lots=lots,
            execution_price=execution_price,
            settle_price=None,
            contract_multiplier=contract_info["contract_multiplier"],
            margin_rate=margin_rate,
            margin_used=execution_price * lots * contract_info["contract_multiplier"] * margin_rate,
            daily_pnl=0.0,
            commission=commission,
            source_type=RecommendationSourceType(self._enum_value(recommendation.get("source_type", RecommendationSourceType.STRATEGY.value))),
            execution_phase=execution_phase,
            execution_price_basis=f"base_price {'+' if is_buy_like else '-'} slippage",
            base_price=base_price,
            base_price_source=recommendation.get("base_price_source"),
            base_price_date=recommendation.get("base_price_date"),
            open_price=recommendation.get("open_price"),
            prev_close_price=recommendation.get("prev_close_price"),
            slippage_model=self.execution_config.get("slippage_model", "tick"),
            slippage_ticks=slippage_ticks,
            slippage_amount=slippage_amount,
            warning_message=warning_message,
            booked_in_settlement=False,
            justification=recommendation.get("justification", ""),
            audit_payload=self._merge_audit_payload(
                recommendation.get("audit_payload"),
                {
                    "market_rules": market_rules_audit,
                    "dynamic_margin": margin_source_audit,
                    "rollover_execution": self._build_rollover_execution_audit(recommendation),
                },
            ),
        )

    def _resolve_phase2_basis(
        self,
        *,
        underlying_code: str,
        trading_date,
        contract_code: Optional[str],
        action: Any,
        force_immediate: bool = False,
    ):
        if intraday_confirmation_enabled(self.config):
            basis, _ = resolve_intraday_execution_basis(
                router=self.router,
                config=self.config,
                underlying_code=underlying_code,
                trading_date=trading_date,
                action=action,
                contract_code=contract_code,
                finalize_untriggered=True,
                force_immediate=force_immediate,
            )
            if basis.base_price is not None or not force_immediate:
                return basis
            logger.warning(
                f"Intraday basis unavailable for immediate {underlying_code} execution; "
                "falling back to morning execution basis."
            )
            return self.router.resolve_morning_execution_base_price(
                underlying_code=underlying_code,
                trading_date=trading_date,
                contract_code=contract_code,
            )
        return self.router.resolve_morning_execution_base_price(
            underlying_code=underlying_code,
            trading_date=trading_date,
            contract_code=contract_code,
        )

    def _resolve_transaction_contract_code(
        self,
        underlying_code: str,
        action_value: str,
        recommendation: Dict[str, Any],
        portfolio,
        trading_date,
    ) -> str:
        contract_code = recommendation.get("contract_code")
        if contract_code:
            return contract_code

        existing_position = portfolio.positions.get(underlying_code)
        if action_value in {
            RecommendationAction.CLOSE_LONG.value,
            RecommendationAction.CLOSE_SHORT.value,
        }:
            if existing_position and getattr(existing_position, "shares", 0) != 0 and getattr(existing_position, "contract_code", None):
                return existing_position.contract_code

        main_quote = self.router.get_futures_main_contract_quote_on_date(underlying_code, trading_date)
        return main_quote.ticker if main_quote is not None else underlying_code

    def _guard_against_contract_mixing(
        self,
        underlying_code: str,
        contract_code: str,
        action_value: str,
        recommendation: Dict[str, Any],
        portfolio,
    ) -> None:
        if self._enum_value(recommendation.get("source_type")) == RecommendationSourceType.ROLLOVER.value:
            return

        existing_position = portfolio.positions.get(underlying_code)
        if existing_position is None:
            return
        if getattr(existing_position, "shares", 0) == 0:
            return

        existing_contract = getattr(existing_position, "contract_code", None)
        if not existing_contract or existing_contract == contract_code:
            return

        if action_value in {
            RecommendationAction.CLOSE_LONG.value,
            RecommendationAction.CLOSE_SHORT.value,
        }:
            return

        raise RuntimeError(
            f"Pending rollover required for {underlying_code}: existing contract "
            f"{existing_contract}, incoming contract {contract_code}"
        )

    def apply_transaction_to_portfolio(self, portfolio, transaction: FuturesTransaction):
        ticker = transaction.ticker
        if ticker not in portfolio.positions:
            portfolio.positions[ticker] = Position(shares=0, value=0)

        position = portfolio.positions[ticker]
        if getattr(position, "contract_code", None) is None:
            position.contract_code = transaction.contract_code
        if getattr(position, "contract_multiplier", None) is None:
            position.contract_multiplier = transaction.contract_multiplier
        if getattr(position, "margin_rate", None) in (None, 0):
            position.margin_rate = transaction.margin_rate
        if getattr(position, "realized_pnl", None) is None:
            position.realized_pnl = 0.0
        if getattr(position, "unrealized_pnl", None) is None:
            position.unrealized_pnl = 0.0
        if getattr(position, "margin_used", None) is None:
            position.margin_used = 0.0
        if getattr(position, "entry_price", None) is None:
            position.entry_price = None
        if getattr(position, "entry_date", None) is None:
            position.entry_date = None

        lots = int(transaction.lots)
        multiplier = transaction.contract_multiplier
        trading_day_value = self._normalize_date_value(transaction.trading_date)
        previous_portfolio_margin = sum(
            float(getattr(pos, "margin_used", 0.0) or 0.0)
            for pos in portfolio.positions.values()
        )
        cash_delta = 0.0
        commission = float(transaction.commission or 0.0)

        if transaction.action == FuturesAction.OPEN_LONG:
            prior_shares = max(position.shares, 0)
            total_shares = prior_shares + lots
            if total_shares > 0:
                if position.entry_price is None or prior_shares == 0:
                    position.entry_price = transaction.execution_price
                    position.entry_date = trading_day_value
                else:
                    position.entry_price = (
                        (position.entry_price * prior_shares) + (transaction.execution_price * lots)
                    ) / total_shares
            position.shares += lots
            position.margin_used = (
                transaction.post_trade_margin_used
                if transaction.post_trade_margin_used is not None
                else position.margin_used + transaction.margin_used
            )
            cash_delta -= commission
        elif transaction.action == FuturesAction.OPEN_SHORT:
            prior_shares = abs(min(position.shares, 0))
            total_shares = prior_shares + lots
            if total_shares > 0:
                if position.entry_price is None or prior_shares == 0:
                    position.entry_price = transaction.execution_price
                    position.entry_date = trading_day_value
                else:
                    position.entry_price = (
                        (position.entry_price * prior_shares) + (transaction.execution_price * lots)
                    ) / total_shares
            position.shares -= lots
            position.margin_used = (
                transaction.post_trade_margin_used
                if transaction.post_trade_margin_used is not None
                else position.margin_used + transaction.margin_used
            )
            cash_delta -= commission
        elif transaction.action == FuturesAction.CLOSE_LONG:
            available_lots = max(position.shares, 0)
            if available_lots < lots:
                raise RuntimeError(
                    f"Cannot close {lots} long lot(s) for {ticker}; only {available_lots} lot(s) are available"
                )
            realized_pnl = 0.0
            if position.entry_price is not None:
                realized_pnl = (transaction.execution_price - position.entry_price) * lots * multiplier
                position.realized_pnl += realized_pnl
            cash_delta += realized_pnl - commission
            position.shares -= lots
            position.margin_used = max(
                0.0,
                transaction.post_trade_margin_used
                if transaction.post_trade_margin_used is not None
                else position.margin_used - self._release_margin(position.margin_used, lots, available_lots),
            )
        elif transaction.action == FuturesAction.CLOSE_SHORT:
            available_lots = abs(min(position.shares, 0))
            if available_lots < lots:
                raise RuntimeError(
                    f"Cannot close {lots} short lot(s) for {ticker}; only {available_lots} lot(s) are available"
                )
            realized_pnl = 0.0
            if position.entry_price is not None:
                realized_pnl = (position.entry_price - transaction.execution_price) * lots * multiplier
                position.realized_pnl += realized_pnl
            cash_delta += realized_pnl - commission
            position.shares += lots
            position.margin_used = max(
                0.0,
                transaction.post_trade_margin_used
                if transaction.post_trade_margin_used is not None
                else position.margin_used - self._release_margin(position.margin_used, lots, available_lots),
            )

        if position.shares == 0:
            position.value = 0.0
            position.margin_used = 0.0
            position.contract_code = None
            position.entry_price = None
            position.entry_date = None
            position.unrealized_pnl = 0.0
        else:
            position.value = abs(position.shares) * transaction.execution_price * multiplier
            position.contract_code = transaction.contract_code
            position.contract_multiplier = multiplier
            position.margin_rate = transaction.margin_rate
            position.unrealized_pnl = 0.0

        portfolio.margin_used = sum(getattr(pos, "margin_used", 0.0) for pos in portfolio.positions.values())
        margin_delta = float(portfolio.margin_used or 0.0) - previous_portfolio_margin
        portfolio.cashflow += cash_delta - margin_delta
        portfolio.cash_available = portfolio.cashflow
        portfolio.account_equity = portfolio.cashflow + portfolio.margin_used
        portfolio.margin_available = portfolio.cashflow
        denominator = portfolio.account_equity
        portfolio.margin_ratio = portfolio.margin_used / denominator if denominator > 0 else 0.0
        return portfolio

    def _get_slippage_ticks(self, underlying_code: str) -> int:
        slippage_by_underlying = self.execution_config.get("slippage_ticks_by_underlying", {})
        if underlying_code in slippage_by_underlying:
            return int(slippage_by_underlying[underlying_code])
        return int(self.execution_config.get("default_slippage_ticks", 1))

    def _calculate_slippage_amount(self, contract_info: Dict[str, Any], slippage_ticks: int) -> float:
        minimum_tick = contract_info.get("minimum_tick", 0.0) or 0.0
        return minimum_tick * slippage_ticks

    def _calculate_commission(
        self,
        underlying_code: str,
        action_value: str,
        execution_price: float,
        lots: int,
        contract_multiplier: float,
        intraday_close: bool = False,
    ) -> float:
        commission_rule = resolve_commission_rule(self.execution_config, underlying_code)
        offset_scope = classify_offset_scope(action_value, intraday_close=intraday_close)
        rounding = float(self.execution_config.get("commission", {}).get("rounding", 0.01))
        return calculate_commission(
            rule=commission_rule,
            execution_price=execution_price,
            lots=lots,
            contract_multiplier=contract_multiplier,
            offset_scope=offset_scope,
            rounding=rounding,
        )

    def _is_intraday_close(
        self,
        underlying_code: str,
        action_value: str,
        portfolio,
        trading_date,
    ) -> bool:
        if action_value not in {RecommendationAction.CLOSE_LONG.value, RecommendationAction.CLOSE_SHORT.value}:
            return False

        position = portfolio.positions.get(underlying_code)
        if position is None:
            return False

        entry_date = getattr(position, "entry_date", None)
        if not entry_date:
            return False

        return str(entry_date)[:10] == self._normalize_date_value(trading_date)

    def _release_margin(self, current_margin_used: float, closed_lots: int, previous_lots: int) -> float:
        if previous_lots <= 0:
            return current_margin_used
        return current_margin_used * (closed_lots / previous_lots)

    def _get_execution_quote(self, contract_code: str, trading_date) -> Any:
        date_value = self._normalize_date_value(trading_date)
        key = (str(contract_code or "").lower(), date_value)
        if key not in self._execution_quote_cache:
            try:
                self._execution_quote_cache[key] = self.router.get_futures_contract_quote_on_date(contract_code, trading_date)
            except Exception as exc:
                logger.warning(
                    f"Execution quote unavailable for {contract_code} on {date_value}: {exc}"
                )
                self._execution_quote_cache[key] = None
        return self._execution_quote_cache[key]

    def _get_contract_detail(self, contract_code: str, trading_date) -> Optional[Dict[str, Any]]:
        date_value = self._normalize_date_value(trading_date)
        key = (str(contract_code or "").lower(), date_value[:7])
        if key not in self._contract_detail_cache:
            api = getattr(self.router, "api", None)
            detail = None
            if api is not None and hasattr(api, "get_futures_contract_detail"):
                try:
                    detail = api.get_futures_contract_detail(contract_code, reference_date=trading_date)
                except Exception as exc:
                    logger.warning(
                        f"Contract detail unavailable for {contract_code} on {date_value}: {exc}"
                    )
            self._contract_detail_cache[key] = detail if isinstance(detail, dict) else None
        return self._contract_detail_cache[key]

    def _build_market_rules_audit(
        self,
        *,
        recommendation: Dict[str, Any],
        action_value: str,
        contract_code: str,
        trading_date,
        execution_price: float,
        contract_info: Dict[str, Any],
        execution_quote: Any,
        contract_detail: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        limit_cfg = self.execution_config.get("limit_lock", {})
        limit_audit = check_limit_lock(
            action=action_value,
            execution_price=execution_price,
            quote=execution_quote,
            minimum_tick=float(contract_info.get("minimum_tick", 0.0) or 0.0),
            tolerance_ticks=int(limit_cfg.get("tolerance_ticks", 0) or 0),
            enabled=bool(limit_cfg.get("enabled", True)),
        )
        expiry_audit = check_contract_expiry_guard(
            action=action_value,
            contract_code=contract_code,
            trading_date=trading_date,
            source_type=recommendation.get("source_type"),
            config=self.config,
            contract_detail=contract_detail,
        )
        return {
            "limit_lock": limit_audit,
            "contract_expiry_guard": expiry_audit,
        }

    def _raise_if_market_rule_blocked(self, underlying_code: str, market_rules_audit: Dict[str, Any]) -> None:
        for rule_name, audit in market_rules_audit.items():
            if not isinstance(audit, dict) or not audit.get("blocked"):
                continue
            reason = audit.get("reason") or rule_name
            message = (
                f"Phase2 execution skipped for {underlying_code}: {reason} "
                f"({rule_name}, audit={audit})"
            )
            raise ExecutionBlocked(reason=reason, message=message, audit_payload=market_rules_audit)

    def _resolve_dynamic_margin_rate(
        self,
        *,
        action_value: str,
        contract_code: str,
        contract_info: Dict[str, Any],
        trading_date,
        contract_detail: Optional[Dict[str, Any]] = None,
    ) -> tuple[float, Dict[str, Any]]:
        side = "long" if action_value in {
            RecommendationAction.OPEN_LONG.value,
            RecommendationAction.CLOSE_LONG.value,
        } else "short"
        static_rate = float(
            contract_info["margin_rate_long"] if side == "long" else contract_info["margin_rate_short"]
        )
        cfg = self.execution_config.get("dynamic_margin", {})
        audit: Dict[str, Any] = {
            "enabled": bool(cfg.get("enabled", False)),
            "provider": cfg.get("provider", "pandaai"),
            "contract_code": contract_code,
            "side": side,
            "static_margin_rate": static_rate,
            "selected_margin_rate": static_rate,
            "source": "static_contract_cache",
            "status": "static_disabled",
        }
        if not audit["enabled"]:
            return static_rate, audit

        date_value = self._normalize_date_value(trading_date)
        key = (str(contract_code or "").lower(), date_value, side)
        if key in self._dynamic_margin_cache:
            cached = dict(self._dynamic_margin_cache[key])
            return float(cached.get("selected_margin_rate", static_rate)), cached

        provider_rate = self._extract_provider_margin_rate(contract_detail, side)
        audit["provider_payload_available"] = contract_detail is not None
        if provider_rate is not None:
            audit.update(
                {
                    "selected_margin_rate": provider_rate,
                    "source": "pandaai_future_detail",
                    "status": "ok",
                }
            )
            self._dynamic_margin_cache[key] = dict(audit)
            return provider_rate, audit

        allow_static_fallback = bool(cfg.get("fallback_to_static_contract_cache", True))
        if not allow_static_fallback:
            audit.update(
                {
                    "status": "provider_margin_missing",
                    "source": "pandaai_future_detail",
                    "selected_margin_rate": None,
                    "fallback_to_static_contract_cache": False,
                }
            )
            self._dynamic_margin_cache[key] = dict(audit)
            raise RuntimeError(
                "Dynamic margin is enabled but PandaAI contract margin is unavailable "
                f"for {contract_code} on {date_value}; static contract-cache fallback is disabled."
            )

        audit.update(
            {
                "status": "fallback_static_no_provider_margin",
                "fallback_to_static_contract_cache": True,
            }
        )

        self._dynamic_margin_cache[key] = dict(audit)
        return static_rate, audit

    def _extract_provider_margin_rate(self, provider_payload: Any, side: str) -> Optional[float]:
        if provider_payload is None:
            return None
        if isinstance(provider_payload, dict):
            candidates = [
                provider_payload.get("long_margin_rate" if side == "long" else "short_margin_rate"),
                provider_payload.get("margin_rate"),
            ]
        else:
            candidates = [
                getattr(provider_payload, "long_margin_rate" if side == "long" else "short_margin_rate", None),
                getattr(provider_payload, "margin_rate", None),
            ]
        for candidate in candidates:
            rate = normalize_margin_rate(candidate)
            if rate is not None:
                return rate
        return None

    def _build_rollover_execution_audit(self, recommendation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self._enum_value(recommendation.get("source_type")) != RecommendationSourceType.ROLLOVER.value:
            return None
        return {
            "mode": recommendation.get("rollover_mode", self.config.get("rollover", {}).get("mode")),
            "leg": recommendation.get("rollover_leg"),
            "from_contract": recommendation.get("from_contract"),
            "to_contract": recommendation.get("to_contract"),
            "close_lots": recommendation.get("rollover_close_lots"),
            "open_lots": recommendation.get("rollover_open_lots"),
            "execution_type": recommendation.get("rollover_execution_type"),
            "cost_components": ["slippage", "commission"],
        }

    def _merge_audit_payload(self, base_payload: Any, additions: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(base_payload) if isinstance(base_payload, dict) else {}
        for key, value in additions.items():
            if value is not None:
                payload[key] = value
        return payload

    def _attach_margin_audit(self, transaction: FuturesTransaction, portfolio) -> None:
        existing_payload = transaction.audit_payload if isinstance(transaction.audit_payload, dict) else {}
        current_position = portfolio.positions.get(transaction.ticker)
        current_shares = int(getattr(current_position, "shares", 0) or 0)
        current_margin_used = float(getattr(current_position, "margin_used", 0.0) or 0.0)
        audit = calculate_margin_audit(
            action=transaction.action,
            lots=transaction.lots,
            current_shares=current_shares,
            current_margin_used=current_margin_used,
        )
        if transaction.action in {FuturesAction.OPEN_LONG, FuturesAction.OPEN_SHORT}:
            audit["post_trade_margin_used"] = current_margin_used + float(transaction.margin_used or 0.0)
            audit["margin_delta"] = audit["post_trade_margin_used"] - current_margin_used
            if audit["post_trade_shares"] == 0:
                audit["post_trade_margin_used"] = 0.0
                audit["margin_delta"] = -current_margin_used

        transaction.released_margin = audit["released_margin"]
        transaction.margin_delta = audit["margin_delta"]
        transaction.post_trade_margin_used = audit["post_trade_margin_used"]
        transaction.warning_message = self._append_warning_message(
            transaction.warning_message,
            self._business_rule_warning(transaction.audit_payload),
        )
        transaction.audit_payload = {
            **existing_payload,
            "margin_audit": audit,
            **audit,
            "execution_phase": self._enum_value(transaction.execution_phase),
            "source_type": self._enum_value(transaction.source_type),
        }

    def _business_rule_warning(self, audit_payload: Any) -> Optional[str]:
        if not isinstance(audit_payload, dict):
            return None
        dynamic_margin = audit_payload.get("dynamic_margin")
        warnings: List[str] = []
        if isinstance(dynamic_margin, dict) and dynamic_margin.get("status", "").startswith("fallback_static"):
            warnings.append("dynamic_margin_fallback_static")
        market_rules = audit_payload.get("market_rules")
        if isinstance(market_rules, dict):
            limit_lock = market_rules.get("limit_lock")
            if isinstance(limit_lock, dict) and limit_lock.get("status") == "no_quote":
                warnings.append("limit_price_unavailable")
            expiry_guard = market_rules.get("contract_expiry_guard")
            if isinstance(expiry_guard, dict) and expiry_guard.get("status") == "delivery_month_unavailable":
                warnings.append("delivery_month_inferred_unavailable")
        return "; ".join(warnings) if warnings else None

    def _append_warning_message(self, current: Optional[str], extra: Optional[str]) -> Optional[str]:
        if not extra:
            return current
        if not current:
            return extra
        if extra in current:
            return current
        return f"{current}; {extra}"

    def _mark_execution_blocked(
        self,
        *,
        recommendation_id: str,
        recommendation: Dict[str, Any],
        portfolio,
        snapshot: Dict[str, Any],
        execution_phase: TradingPhase,
        blocked: ExecutionBlocked,
    ):
        warning_message = blocked.warning_message
        translation = ensure_execution_translation(snapshot)
        translation["market_rule_block"] = blocked.audit_payload
        add_rewrite_reason(snapshot, blocked.reason)
        reason_category = categorize_no_trade_reason(blocked.reason)
        set_execution_result(
            snapshot,
            outcome="skipped",
            status=RecommendationStatus.SKIPPED.value,
            transaction_count=0,
            no_trade_reason=blocked.reason,
            warning_message=warning_message,
        )
        snapshot["execution_result"]["execution_learning_trace"] = {
            "no_trade_reason": blocked.reason,
            "no_trade_reason_category": reason_category,
            "execution_learning_type": "market_rule_or_execution_block",
            "turn_into_memory": True,
            "timing_strategy_question": (
                "If same-scope shadow results later show missed alpha, test whether earlier entry, "
                "pullback entry, or lower chase tolerance would have improved execution without using future data."
            ),
            "not_direction_evidence": True,
        }
        self.db.update_futures_recommendation_status(
            recommendation_id,
            RecommendationStatus.SKIPPED.value,
            warning_message=warning_message,
            signal_snapshot=snapshot,
            audit_payload=build_audit_payload(snapshot),
        )
        self._log_non_transaction_execution(
            recommendation=recommendation,
            execution_phase=execution_phase,
            outcome="skipped",
            reason=blocked.reason,
        )
        return portfolio

    def _get_recommendation_transactions(
        self,
        config_id: str,
        recommendation_id: str,
        trading_date,
        execution_phase: TradingPhase,
    ) -> List[Dict[str, Any]]:
        transactions = self.db.get_futures_transactions_by_date(
            config_id=config_id,
            trading_date=trading_date,
            execution_phase=execution_phase,
        )
        return [
            transaction
            for transaction in transactions
            if transaction.get("recommendation_id") == recommendation_id
        ]

    def _to_futures_action(self, action_value: str) -> FuturesAction:
        mapping = {
            RecommendationAction.OPEN_LONG.value: FuturesAction.OPEN_LONG,
            RecommendationAction.OPEN_SHORT.value: FuturesAction.OPEN_SHORT,
            RecommendationAction.CLOSE_LONG.value: FuturesAction.CLOSE_LONG,
            RecommendationAction.CLOSE_SHORT.value: FuturesAction.CLOSE_SHORT,
            RecommendationAction.HOLD.value: FuturesAction.HOLD,
        }
        return mapping[action_value]

    def _log_non_transaction_execution(
        self,
        recommendation: Dict[str, Any],
        execution_phase: TradingPhase,
        outcome: str,
        reason: str = "",
    ) -> None:
        underlying_code = recommendation.get("underlying_code") or recommendation.get("ticker") or "UNKNOWN"
        action_value = self._enum_value(recommendation.get("action"))
        lots = int(recommendation.get("lots", 0) or 0)
        phase_value = self._enum_value(execution_phase)
        suffix = f" | reason={reason}" if reason else ""
        logger.trade_logger.info(
            f"[EXECUTION] {underlying_code} | phase={phase_value} | outcome={outcome} | "
            f"action={action_value} | lots={lots}{suffix}"
        )

    def _log_transaction_execution(self, transaction: FuturesTransaction, execution_phase: TradingPhase) -> None:
        phase_value = self._enum_value(execution_phase)
        action_value = self._enum_value(transaction.action)
        basis_source = self._enum_value(transaction.base_price_source)
        base_price_text = f"{float(transaction.base_price):.2f}" if transaction.base_price is not None else "NA"
        execution_price_text = f"{float(transaction.execution_price):.2f}"
        slippage_text = f"{float(transaction.slippage_amount or 0.0):.2f}"
        commission_text = f"{float(transaction.commission or 0.0):.2f}"
        released_margin_text = f"{float(transaction.released_margin or 0.0):.2f}"
        margin_delta_text = f"{float(transaction.margin_delta or 0.0):+.2f}"
        post_trade_margin_text = f"{float(transaction.post_trade_margin_used or 0.0):.2f}"
        logger.trade_logger.info(
            f"[EXECUTION] {transaction.ticker} | phase={phase_value} | action={action_value} | "
            f"lots={transaction.lots} | contract={transaction.contract_code} | "
            f"basis={basis_source} | base={base_price_text} | exec={execution_price_text} | "
            f"slippage={slippage_text} | commission={commission_text} | "
            f"released_margin={released_margin_text} | margin_delta={margin_delta_text} | "
            f"post_trade_margin={post_trade_margin_text}"
        )

    def _enum_value(self, value: Any) -> Any:
        return value.value if hasattr(value, "value") else value

    def _normalize_date_value(self, value: Any) -> str:
        return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)

    def _to_dict(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return dict(value)
