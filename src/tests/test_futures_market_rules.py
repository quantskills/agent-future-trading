import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.common.futures_market_rules import (
    check_contract_expiry_guard,
    check_limit_lock,
    normalize_margin_rate,
    parse_contract_delivery_month,
)


class FuturesMarketRulesTest(unittest.TestCase):
    def test_limit_lock_blocks_buy_at_limit_up(self):
        quote = SimpleNamespace(limit_up=3500.0, limit_down=3300.0, trade_date="2025-03-10", ticker="rb2505")
        audit = check_limit_lock(
            action="open_long",
            execution_price=3500.0,
            quote=quote,
            minimum_tick=1.0,
            tolerance_ticks=0,
            enabled=True,
        )
        self.assertTrue(audit["blocked"])
        self.assertEqual(audit["reason"], "limit_locked_no_fill")
        self.assertEqual(audit["side"], "buy_like")

    def test_limit_lock_blocks_sell_at_limit_down(self):
        quote = SimpleNamespace(limit_up=3500.0, limit_down=3300.0, trade_date="2025-03-10", ticker="rb2505")
        audit = check_limit_lock(
            action="open_short",
            execution_price=3300.0,
            quote=quote,
            minimum_tick=1.0,
            tolerance_ticks=0,
            enabled=True,
        )
        self.assertTrue(audit["blocked"])
        self.assertEqual(audit["reason"], "limit_locked_no_fill")
        self.assertEqual(audit["side"], "sell_like")

    def test_expiry_guard_blocks_new_entry_but_not_rollover(self):
        config = {
            "execution": {
                "contract_expiry_guard": {
                    "enabled": True,
                    "block_new_entries_in_delivery_month": True,
                    "near_expiry_days_before_delivery_month": 5,
                    "near_expiry_days_before_last_trade": 5,
                    "apply_to_rollover": False,
                }
            }
        }
        blocked = check_contract_expiry_guard(
            action="open_long",
            contract_code="rb2503",
            trading_date=datetime(2025, 3, 3),
            source_type="strategy",
            config=config,
        )
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["reason"], "near_expiry_new_entry_block")

        rollover = check_contract_expiry_guard(
            action="open_long",
            contract_code="rb2503",
            trading_date=datetime(2025, 3, 3),
            source_type="rollover",
            config=config,
        )
        self.assertFalse(rollover["blocked"])
        self.assertEqual(rollover["status"], "rollover_exempt")

    def test_delivery_month_parser_and_margin_normalization(self):
        self.assertEqual(parse_contract_delivery_month("rb2505", datetime(2025, 1, 2)).isoformat(), "2025-05-01")
        self.assertEqual(parse_contract_delivery_month("cf601", datetime(2025, 10, 1)).isoformat(), "2026-01-01")
        self.assertEqual(normalize_margin_rate(12), 0.12)
        self.assertEqual(normalize_margin_rate(0.12), 0.12)
        self.assertIsNone(normalize_margin_rate(None))


if __name__ == "__main__":
    unittest.main()
