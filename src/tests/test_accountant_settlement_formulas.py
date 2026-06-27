import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from graph.schema import FuturesAction, Portfolio, Position
from tools.agent_tools.execution.futures_settlement import FuturesDailySettlement


def _engine() -> FuturesDailySettlement:
    return object.__new__(FuturesDailySettlement)


class AccountantSettlementFormulaTest(unittest.TestCase):
    def test_open_lots_and_overnight_holding_settlement_formula(self):
        engine = _engine()
        reference = Portfolio(
            id="portfolio-1",
            cashflow=99900.0,
            account_equity=100000.0,
            cash_available=99900.0,
            margin_used=100.0,
            margin_available=99900.0,
            positions={},
        )
        ledgers = {
            "BU": {
                "starting_realized_pnl": 0.0,
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
                        "side": "long",
                        "lots": 1,
                        "daily_reference_price": 100.0,
                        "position_entry_price": 95.0,
                        "position_entry_date": "2025-03-02",
                        "contract_code": "BU2506",
                        "contract_multiplier": 10.0,
                        "margin_rate": 0.10,
                    }
                ],
            }
        }
        engine._apply_transaction(
            ledgers,
            {
                "ticker": "BU",
                "trading_date": "2025-03-03",
                "action": FuturesAction.OPEN_LONG.value,
                "lots": 2,
                "execution_price": 102.0,
                "contract_code": "BU2506",
                "contract_multiplier": 10.0,
                "margin_rate": 0.10,
                "commission": 12.0,
            },
        )

        summary = engine._finalize_ledgers(
            reference,
            ledgers,
            {"BU2506": 105.0},
            engine._normalize_date("2025-03-03"),
        )

        self.assertEqual(summary["daily_pnl"], 110.0)
        self.assertEqual(summary["commission"], 12.0)
        self.assertEqual(summary["current_margin"], 315.0)
        self.assertEqual(summary["previous_account_equity"], 100000.0)
        self.assertEqual(summary["current_account_equity"], 100098.0)
        self.assertEqual(summary["cash_available"], 99783.0)

        detail = summary["positions_detail"]["BU"]
        self.assertEqual(detail["holding_pnl"], 50.0)
        self.assertEqual(detail["new_position_pnl"], 60.0)
        self.assertEqual(detail["close_pnl"], 0.0)
        self.assertEqual(detail["commission"], 12.0)
        self.assertEqual(detail["total_pnl"], 110.0)
        self.assertEqual(detail["lots"], 3)

        position = summary["portfolio"].positions["BU"]
        self.assertEqual(position.shares, 3)
        self.assertEqual(position.margin_used, 315.0)
        self.assertEqual(position.realized_pnl, 0.0)

    def test_close_lots_settlement_formula_releases_margin_and_books_close_pnl(self):
        engine = _engine()
        reference = Portfolio(
            id="portfolio-1",
            cashflow=99700.0,
            account_equity=100000.0,
            cash_available=99700.0,
            margin_used=300.0,
            margin_available=99700.0,
            positions={
                "BU": Position(
                    shares=3,
                    value=3000.0,
                    entry_price=90.0,
                    entry_date="2025-03-01",
                    contract_code="BU2506",
                    settle_price=100.0,
                    margin_used=300.0,
                    margin_rate=0.10,
                    contract_multiplier=10.0,
                    realized_pnl=0.0,
                )
            },
        )
        ledgers = engine._build_initial_ledgers(reference)
        engine._apply_transaction(
            ledgers,
            {
                "ticker": "BU",
                "trading_date": "2025-03-03",
                "action": FuturesAction.CLOSE_LONG.value,
                "lots": 2,
                "execution_price": 103.0,
                "contract_code": "BU2506",
                "contract_multiplier": 10.0,
                "margin_rate": 0.10,
                "commission": 8.0,
            },
        )

        summary = engine._finalize_ledgers(
            reference,
            ledgers,
            {"BU2506": 105.0},
            engine._normalize_date("2025-03-03"),
        )

        self.assertEqual(summary["daily_pnl"], 110.0)
        self.assertEqual(summary["commission"], 8.0)
        self.assertEqual(summary["current_margin"], 105.0)
        self.assertEqual(summary["current_account_equity"], 100102.0)
        self.assertEqual(summary["cash_available"], 99997.0)

        detail = summary["positions_detail"]["BU"]
        self.assertEqual(detail["holding_pnl"], 50.0)
        self.assertEqual(detail["new_position_pnl"], 0.0)
        self.assertEqual(detail["close_pnl"], 60.0)
        self.assertEqual(detail["commission"], 8.0)
        self.assertEqual(detail["lots"], 1)

        position = summary["portfolio"].positions["BU"]
        self.assertEqual(position.shares, 1)
        self.assertEqual(position.margin_used, 105.0)
        self.assertEqual(position.realized_pnl, 260.0)

    def test_accounting_invariant_rejects_wrong_equity_or_cash_split(self):
        engine = _engine()
        with self.assertRaisesRegex(RuntimeError, "Settlement equity invariant failed"):
            engine._assert_accounting_invariants(
                previous_cash_available=99900.0,
                current_cash_available=99783.0,
                previous_reserved_margin=100.0,
                current_reserved_margin=315.0,
                previous_account_equity=100000.0,
                current_account_equity=100000.0,
                daily_pnl=110.0,
                commission=12.0,
            )


if __name__ == "__main__":
    unittest.main()
