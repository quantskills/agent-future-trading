import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.agent_tools.analysis.finoview_factors import _latest_visible_row
from tools.agent_tools.analysis.market_confirmation import MarketConfirmationEngine, score_pandaai_extra_records


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
    def test_finoview_pre_open_lag_includes_t_minus_one_and_excludes_t_day(self):
        df = pd.DataFrame(
            {
                "date": ["2025-01-08", "2025-01-09", "2025-01-10"],
                "value": [8, 9, 10],
            }
        )

        row, data_date, status = _latest_visible_row(
            df,
            date_column="date",
            trade_date="2025-01-10",
            release_lag_days=1,
        )

        self.assertEqual(status, "ok")
        self.assertEqual(row["value"], 9)
        self.assertEqual(data_date.strftime("%Y-%m-%d"), "2025-01-09")

        row, data_date, status = _latest_visible_row(
            df,
            date_column="date",
            trade_date="2025-01-10",
            release_lag_days=0,
        )

        self.assertEqual(status, "ok")
        self.assertEqual(row["value"], 9)
        self.assertEqual(data_date.strftime("%Y-%m-%d"), "2025-01-09")

    def test_extended_pandaai_records_are_scored_as_factor_evidence(self):
        records = {
            "contract_daily_indicators": [
                {"date": "2025-01-03", "line": "bull_bear", "ratio": 1.25},
            ],
            "contract_rank": [
                {"date": "2025-01-03", "position_type": "long", "ratio": 0.70},
                {"date": "2025-01-03", "position_type": "short", "ratio": 0.20},
            ],
            "netposi_rank": [
                {"date": "2025-01-03", "position_type": "long", "net_position_change": 30},
                {"date": "2025-01-03", "position_type": "short", "net_position_change": 10},
            ],
        }

        features = score_pandaai_extra_records(records)
        by_name = {item["feature"]: item for item in features}

        self.assertIn("contract_daily_indicators", by_name)
        self.assertIn("contract_rank", by_name)
        self.assertIn("netposi_rank", by_name)
        self.assertEqual(by_name["contract_rank"]["direction"], "long")
        self.assertEqual(by_name["netposi_rank"]["direction"], "long")

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
            "tools.agent_tools.analysis.market_confirmation.get_previous_trading_day",
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
        self.assertEqual(result["feature_status"]["net_flow_long"], "fallback_covered")
        self.assertEqual(result["feature_status"]["net_flow_short"], "fallback_covered")
        self.assertEqual(
            sorted(result["data_status_groups"]["fallback_covered"]),
            ["net_flow_long", "net_flow_short"],
        )
        self.assertIn("variety_position_rank", [item["feature"] for item in result["features"]])

    def test_market_confirmation_uses_t_minus_one_reference_date(self):
        records = {
            "basis": [{"date": "2025-01-03", "basis_ratio": 0.03}],
        }
        snapshot = {
            "records": records,
            "record_counts": {key: len(value) for key, value in records.items()},
            "errors": [],
        }
        router = _SnapshotRouter(snapshot)

        with patch(
            "tools.agent_tools.analysis.market_confirmation.get_previous_trading_day",
            return_value=datetime(2025, 1, 3),
        ) as previous_day:
            result = MarketConfirmationEngine(_config(), router=router).evaluate(
                underlying_code="BU",
                trading_date="2025-01-06",
                target_direction="long",
                signal_strength=0.5,
                contract_code="bu2503",
            )

        previous_day.assert_called_once()
        self.assertEqual(router.calls[0]["reference_date"].strftime("%Y-%m-%d"), "2025-01-03")
        self.assertEqual(result["reference_date"], "2025-01-03")
        self.assertEqual(result["info_cutoff"], "T-1_or_earlier")
        self.assertEqual(result["confirmations"], ["basis"])

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
            "tools.agent_tools.analysis.market_confirmation.get_previous_trading_day",
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

    def test_feature_status_distinguishes_parameter_no_data_and_unsupported(self):
        records = {
            "contract_rank": [],
            "warehouse_receipt": [],
            "symbol_position_rank_long": [],
            "net_flow_long": [],
            "net_flow_short": [],
            "netposi_rank": [{"date": "2025-01-03", "position_type": "long", "net_position_change": 3}],
        }
        snapshot = {
            "records": records,
            "record_counts": {key: len(value) for key, value in records.items()},
            "feature_status": {
                "contract_rank": "parameter_error",
                "warehouse_receipt": "no_data",
                "symbol_position_rank_long": "unsupported_feature",
                "net_flow_long": "no_data",
                "net_flow_short": "no_data",
                "netposi_rank": "ok",
            },
            "feature_diagnostics": {
                "contract_rank": {
                    "status": "parameter_error",
                    "reason": "required_parameter_missing",
                    "error": "rank_type参数不能为空",
                },
                "warehouse_receipt": {
                    "status": "no_data",
                    "reason": "empty_response",
                },
                "symbol_position_rank_long": {
                    "status": "unsupported_feature",
                    "reason": "provider_or_symbol_not_supported",
                },
            },
            "errors": ["contract_rank: rank_type参数不能为空"],
        }

        with patch(
            "tools.agent_tools.analysis.market_confirmation.get_previous_trading_day",
            return_value=datetime(2025, 1, 3),
        ):
            result = MarketConfirmationEngine(_config(), router=_SnapshotRouter(snapshot)).evaluate(
                underlying_code="BU",
                trading_date="2025-01-06",
                target_direction="long",
                signal_strength=0.5,
                contract_code="bu2503",
            )

        self.assertIn("contract_rank", result["parameter_errors"])
        self.assertIn("warehouse_receipt", result["no_data"])
        self.assertIn("symbol_position_rank_long", result["unsupported_features"])
        self.assertNotIn("symbol_position_rank_long", result["data_missing"])
        self.assertEqual(result["feature_diagnostics"]["contract_rank"]["reason"], "required_parameter_missing")


if __name__ == "__main__":
    unittest.main()
