import logging
import csv
import json
import os
import sys
from datetime import datetime

from graph.schema import AnalystSignal, FuturesDecision, Portfolio, PositionRisk
from util.text_sanitize import sanitize_visible_text


class AgentQuantLogger:
    """Logger for the AgentQuant application."""

    def __init__(self, log_level: str = "INFO"):
        configured_log_dir = os.getenv("AGENTQUANT_LOG_DIR")
        if configured_log_dir:
            self.log_dir = os.path.abspath(configured_log_dir)
        else:
            self.log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
        self.log_level = log_level
        self.run_id = os.getenv("AGENTQUANT_RUN_ID") or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.log_namespace = os.getenv("AGENTQUANT_LOG_NAMESPACE", "").strip("_")
        self._warning_count = 0
        self._error_count = 0
        self.context = {}

        os.makedirs(self.log_dir, exist_ok=True)

        self.logger = logging.getLogger("agent_quant")
        self.logger.setLevel(self.log_level)
        if self.logger.handlers:
            self.logger.handlers.clear()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        pid = os.getpid()
        file_suffix = f"{self.log_namespace}_{timestamp}_{pid}" if self.log_namespace else f"{timestamp}_{pid}"
        log_file = os.path.join(self.log_dir, f"agentquant_{self.run_id}_{file_suffix}.log")
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(self.log_level)

        console_handler = logging.StreamHandler(sys.stdout)
        if sys.platform == "win32":
            try:
                os.environ.setdefault("PYTHONIOENCODING", "utf-8")
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        console_handler.setLevel(self.log_level)

        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        self.trade_logger = logging.getLogger("trade_log")
        self.trade_logger.setLevel(logging.INFO)
        if self.trade_logger.handlers:
            self.trade_logger.handlers.clear()

        trade_log_file = os.path.join(self.log_dir, f"trade_{self.run_id}_{file_suffix}.log")
        trade_file_handler = logging.FileHandler(trade_log_file, encoding="utf-8")
        trade_file_handler.setLevel(logging.INFO)
        trade_formatter = logging.Formatter("%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        trade_file_handler.setFormatter(trade_formatter)
        self.trade_logger.addHandler(trade_file_handler)

    def debug(self, message: str):
        self.logger.debug(sanitize_visible_text(message))

    def info(self, message: str):
        self.logger.info(sanitize_visible_text(message))

    def warning(self, message: str):
        self._warning_count += 1
        self.logger.warning(sanitize_visible_text(message))

    def error(self, message: str):
        self._error_count += 1
        self.logger.error(sanitize_visible_text(message))

    def set_context(self, **kwargs):
        self.context.update({key: value for key, value in kwargs.items() if value not in (None, "")})

    def get_message_counts(self) -> dict:
        return {
            "warning_count": self._warning_count,
            "error_count": self._error_count,
        }

    def write_daily_summary(self, trading_date: str, payload: dict):
        summary_dir = os.path.join(self.log_dir, "summaries", self.run_id)
        os.makedirs(summary_dir, exist_ok=True)
        summary_payload = {
            "run_id": self.run_id,
            "log_namespace": self.log_namespace or None,
            **self.context,
            **payload,
            **self.get_message_counts(),
            "written_at": datetime.now().isoformat(),
        }
        summary_path = os.path.join(summary_dir, f"{trading_date}.json")
        with open(summary_path, "w", encoding="utf-8") as handle:
            json.dump(summary_payload, handle, ensure_ascii=False, indent=2)
        self._append_daily_summary_csv(summary_dir, summary_payload)
        self.info(f"Wrote daily summary to {summary_path}")

    def _append_daily_summary_csv(self, summary_dir: str, payload: dict) -> None:
        csv_path = os.path.join(summary_dir, "daily_summary.csv")
        row = self._flatten_summary_payload(payload)
        fieldnames = list(row.keys())
        write_header = not os.path.exists(csv_path)

        with open(csv_path, "a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    def _flatten_summary_payload(self, payload: dict) -> dict:
        phases = payload.get("phases") if isinstance(payload.get("phases"), dict) else {}
        recommendation_summary = (
            payload.get("recommendation_summary")
            if isinstance(payload.get("recommendation_summary"), dict)
            else {}
        )
        transaction_summary = (
            payload.get("transaction_summary")
            if isinstance(payload.get("transaction_summary"), dict)
            else {}
        )
        settlement_summary = (
            payload.get("settlement_summary")
            if isinstance(payload.get("settlement_summary"), dict)
            else {}
        )
        return {
            "run_id": payload.get("run_id"),
            "trading_date": payload.get("trading_date"),
            "exp_name": payload.get("exp_name"),
            "phase1_status": phases.get("phase1"),
            "phase2_status": phases.get("phase2"),
            "phase3_status": phases.get("phase3"),
            "phase4_status": phases.get("phase4"),
            "validation_status": payload.get("validation_status"),
            "strategy_count": recommendation_summary.get("strategy_count"),
            "rollover_count": recommendation_summary.get("rollover_count"),
            "phase1_transaction_count": transaction_summary.get("phase1_transaction_count"),
            "phase2_transaction_count": transaction_summary.get("phase2_transaction_count"),
            "settlement_balance": settlement_summary.get("current_balance"),
            "settlement_margin": settlement_summary.get("current_margin"),
            "settlement_pnl": settlement_summary.get("daily_pnl"),
            "settlement_commission": settlement_summary.get("commission"),
            "warning_count": payload.get("warning_count"),
            "error_count": payload.get("error_count"),
        }

    def log_agent_status(self, agent_name: str, ticker: str, status: str):
        if ticker:
            msg = f"Agent: {agent_name} | Ticker: {ticker} | Status: {status}"
        else:
            msg = f"Agent: {agent_name} | Status: {status}"
        self.info(msg)

    def log_decision(self, ticker: str, d: FuturesDecision):
        quantity = d.lots
        quantity_label = "Lots"
        msg = (
            f"Decision for {ticker}: {d.action} | {quantity_label}: {quantity} | "
            f"Price: {d.price} | Justification: {sanitize_visible_text(d.justification)}"
        )
        self.info(msg)
        self.trade_logger.info(f"[DECISION] {ticker}: {d.action} {quantity} {quantity_label.lower()} @ {d.price}")

    def log_signal(self, agent_name: str, ticker: str, s: AnalystSignal):
        msg = (
            f"Agent: {agent_name} | Ticker: {ticker} | Signal: {s.signal} | "
            f"Justification: {sanitize_visible_text(s.justification)}"
        )
        self.info(msg)
        self.trade_logger.info(f"[SIGNAL] {ticker} | {agent_name}: {s.signal}")

    def log_portfolio(self, msg: str, portfolio: Portfolio):
        asset_value = portfolio.cashflow + sum(position.value for position in portfolio.positions.values())
        self.info(f"{msg}: {portfolio} | Total Asset Value: {asset_value:.2f}")
        positions_str = ", ".join(
            f"{ticker}:{position.shares}"
            for ticker, position in portfolio.positions.items()
            if position.shares != 0
        ) or "flat"
        self.trade_logger.info(
            f"[PORTFOLIO] {msg} | Cash: {portfolio.cashflow:.2f} | Positions: {positions_str} | Total: {asset_value:.2f}"
        )

    def log_risk(self, ticker: str, position_risk: PositionRisk):
        msg = (
            f"Risk Control for {ticker}| Optimal Position Ratio: {position_risk.optimal_position_ratio} | "
            f"Justification: {sanitize_visible_text(position_risk.justification)}"
        )
        self.info(msg)
        self.trade_logger.info(f"[RISK] {ticker}: Position Ratio = {position_risk.optimal_position_ratio:.2f}")

    def format_analyst_signals_for_risk_control(self, analyst_signals: list) -> str:
        """Format analyst signals into a compact string for risk-control prompts."""
        if not analyst_signals:
            return "No analyst signals available"

        formatted_lines = []
        for signal in analyst_signals:
            signal_direction = signal.signal.upper()
            signal_value = {"BULLISH": "+1", "BEARISH": "-1", "NEUTRAL": "0"}.get(signal_direction, "0")
            justification = sanitize_visible_text(signal.justification)
            if len(justification) > 150:
                justification = justification[:150] + "..."

            formatted_lines.append(
                f"- [{signal.signal}] {signal_value} | Confidence: {signal.confidence:.2f} | Reasoning: {justification}"
            )

        return "\n".join(formatted_lines)

    def log_futures_decision(self, ticker: str, d: FuturesDecision):
        """Log the futures decision of a ticker."""
        action_display = {
            "open_long": "OPEN LONG",
            "open_short": "OPEN SHORT",
            "close_long": "CLOSE LONG",
            "close_short": "CLOSE SHORT",
            "hold": "HOLD",
        }.get(d.action.value if hasattr(d.action, "value") else str(d.action), str(d.action))

        msg = f"Futures Decision for {ticker}: {action_display} {d.lots} lots @ {d.price} (Settle: {d.settle_price})"
        self.info(msg)
        self.trade_logger.info(f"[FUTURES_DECISION] {ticker}: {d.action} {d.lots} lots @ {d.price} | Settle: {d.settle_price}")

    def log_settlement(self, trading_date, settlement):
        """Log the daily settlement record."""
        self.trade_logger.info("=" * 70)
        self.trade_logger.info(f"DAILY SETTLEMENT - {trading_date}")
        self.trade_logger.info("=" * 70)
        self.trade_logger.info("Cash Available")
        self.trade_logger.info(f"  Previous:      {settlement.previous_balance:>15,.2f}")
        self.trade_logger.info(f"  Current:       {settlement.current_balance:>15,.2f}")
        self.trade_logger.info(f"  Change:        {settlement.current_balance - settlement.previous_balance:>+15,.2f}")
        previous_equity = getattr(
            settlement,
            "previous_account_equity",
            settlement.previous_balance + settlement.previous_margin,
        )
        current_equity = getattr(
            settlement,
            "current_account_equity",
            settlement.current_balance + settlement.current_margin,
        )
        self.trade_logger.info("")
        self.trade_logger.info("Account Equity")
        self.trade_logger.info(f"  Previous:      {previous_equity:>15,.2f}")
        self.trade_logger.info(f"  Current:       {current_equity:>15,.2f}")
        self.trade_logger.info(f"  Change:        {current_equity - previous_equity:>+15,.2f}")
        self.trade_logger.info("")
        self.trade_logger.info("Margin")
        self.trade_logger.info(f"  Previous:      {settlement.previous_margin:>15,.2f}")
        self.trade_logger.info(f"  Current:       {settlement.current_margin:>15,.2f}")
        self.trade_logger.info(f"  Change:        {settlement.current_margin - settlement.previous_margin:>+15,.2f}")
        self.trade_logger.info("")
        self.trade_logger.info("Cash Flow")
        self.trade_logger.info(f"  Daily PnL:     {settlement.daily_pnl:>+15,.2f}")
        self.trade_logger.info(f"  Commission:    {settlement.commission:>15,.2f}")
        self.trade_logger.info(f"  Deposit:       {settlement.deposit:>+15,.2f}")
        self.trade_logger.info(f"  Withdraw:      {settlement.withdraw:>+15,.2f}")
        self.trade_logger.info("")
        self.trade_logger.info("Risk Summary")
        self.trade_logger.info(f"  Margin Ratio:  {settlement.margin_ratio:>14.2%}")
        self.trade_logger.info(f"  Warning:       {'YES' if settlement.is_warning else 'NO'}")
        if settlement.is_liquidation:
            self.trade_logger.info("  Liquidation:   TRIGGERED")
        positions_detail = getattr(settlement, "positions_detail", None) or {}
        if positions_detail:
            self.trade_logger.info("")
            self.trade_logger.info("Per Ticker")
            for ticker, detail in sorted(positions_detail.items()):
                contract_code = detail.get("contract_code") or "-"
                position_type = detail.get("position_type") or "FLAT"
                lots = int(detail.get("lots") or 0)
                settle_price = float(detail.get("settle_price") or 0.0)
                total_pnl = float(detail.get("total_pnl") or 0.0)
                commission = float(detail.get("commission") or 0.0)
                self.trade_logger.info(
                    f"  {ticker}: contract={contract_code} | side={position_type} | "
                    f"lots={lots} | settle={settle_price:,.2f} | "
                    f"pnl={total_pnl:+,.2f} | commission={commission:,.2f}"
                )
        self.trade_logger.info("=" * 70)


logger = AgentQuantLogger()
