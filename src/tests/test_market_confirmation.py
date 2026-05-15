import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.market_confirmation import MarketConfirmationEngine


class _SnapshotRouter:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def get_pandaai_futures_extra_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return self.snapshot


def _config():
    return {
        "pandaai_extra_data": {
            "enabled": True,
            "mode": "confirm_only",
            "reference_lag_days": 1,
            "lookback_days": 5,
            "features": {},
        },
        "market_confirmation": {
            "enabled": True,
        },
    }


class MarketConfirmationDataQualityTest(unittest.TestCase):
    def test_net_flow_empty_is_covered_when_replacement_features_exist(self):
        records = {
            "net_flow_long": [],
            "net_flow_short": [],
            "variety_position_rank_long": [{"date": "2025-01-03", "change_oi": 12}],
            "variety_position_rank_short": [{"date": "2025-01-03", "change_oi": -4}],
            "broker_net_margin_change": [{"date": "2025-01-03", "margin_change": 1000}],
        }
        snapshot = {
            "records": records,
            "record_counts": {key: len(value) for key, value in records.items()},
            "errors": [],
        }

        with patch(
            "tools.agent_tools.market_confirmation.get_previous_trading_day",
            return_value=datetime(2025, 1, 3),
        ):
            result = MarketConfirmationEngine(_config(), router=_SnapshotRouter(snapshot)).evaluate(
                underlying_code="BU",
                trading_date="2025-01-06",
                target_direction="long",
                signal_strength=0.5,
                contract_code="bu2503",
            )

        self.assertNotIn("net_flow_long", result["data_missing"])
        self.assertNotIn("net_flow_short", result["data_missing"])
        self.assertEqual(
            sorted(result["fallback_covered_missing"]),
            ["net_flow_long", "net_flow_short"],
        )
        self.assertIn("net_flow_long", result["data_unavailable"])
        self.assertIn("net_flow_short", result["data_unavailable"])
        self.assertIn("variety_position_rank", [item["feature"] for item in result["features"]])

    def test_net_flow_empty_remains_missing_without_replacement_features(self):
        records = {
            "net_flow_long": [],
            "net_flow_short": [],
        }
        snapshot = {
            "records": records,
            "record_counts": {key: len(value) for key, value in records.items()},
            "errors": [],
        }

        with patch(
            "tools.agent_tools.market_confirmation.get_previous_trading_day",
            return_value=datetime(2025, 1, 3),
        ):
            result = MarketConfirmationEngine(_config(), router=_SnapshotRouter(snapshot)).evaluate(
                underlying_code="BU",
                trading_date="2025-01-06",
                target_direction="long",
                signal_strength=0.5,
                contract_code="bu2503",
            )

        self.assertIn("net_flow_long", result["data_missing"])
        self.assertIn("net_flow_short", result["data_missing"])
        self.assertEqual(result["fallback_covered_missing"], [])


if __name__ == "__main__":
    unittest.main()
