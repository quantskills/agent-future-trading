import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


SRC_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from apis.router import APISource, Router
from tools.agent_tools.analysis.analyst_finoview_factors import build_factor_catalog
from tools.agent_tools.analysis.analyst_quality import (
    format_fundamental_summary_for_prompt,
    summarize_news_events,
)
from tools.agent_tools.execution.trader_intraday_execution import (
    resolve_intraday_execution_basis,
)


class _FundamentalAPI:
    def __init__(self, quotes=None):
        self.quotes = list(quotes or [])
        self.calls = []

    def get_continuous_candles(self, **kwargs):
        self.calls.append(dict(kwargs))
        return list(self.quotes)


class _MinuteFailureRouter:
    def get_china_futures_minute_bars(self, **_kwargs):
        raise RuntimeError("provider connection reset with sensitive details")


class _EmptyMinuteRouter:
    def get_china_futures_minute_bars(self, **_kwargs):
        return []


def _router(*, data_dir: Path, api=None, news_dir: Path | None = None) -> Router:
    router = Router.__new__(Router)
    router.market_type = "china_futures"
    router.api = api or _FundamentalAPI()
    router.last_fundamentals_metadata = None
    router.last_news_metadata = None
    router.config = {
        "factor_data": {
            "data_dir": str(data_dir),
            "catalog_path": str(SRC_ROOT / "config" / "finoview_factor_catalog.yaml"),
            "finoview_enabled": True,
            "news": {
                "data_dir": str(news_dir or data_dir),
            },
        }
    }
    return router


class DataSourceSemanticsTest(unittest.TestCase):
    def test_finoview_catalog_uses_observed_cadence_and_frequency_visibility_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pd.DataFrame(
                {
                    "tradeDate": pd.to_datetime(
                        ["2025-01-03", "2025-01-10", "2025-01-17", "2025-01-24"]
                    ),
                    "bu_social_stock": [10.0, 11.0, 12.0, 13.0],
                }
            ).to_feather(root / "bu_social_stock.feather")
            pd.DataFrame(
                {
                    "tradeDate": pd.to_datetime(
                        ["2024-11-30", "2024-12-31", "2025-01-31"]
                    ),
                    "macro_cn_pmi": [49.8, 50.1, 49.9],
                }
            ).to_feather(root / "macro_cn_pmi.feather")

            catalog = build_factor_catalog(
                data_dir=root,
                limit_to_tickers=["BU"],
                catalog_config_path=SRC_ROOT / "config" / "finoview_factor_catalog.yaml",
            )

        by_name = {entry["factor_name"]: entry for entry in catalog["BU"]}
        self.assertEqual(by_name["bu_social_stock"]["freq"], "weekly")
        self.assertEqual(by_name["bu_social_stock"]["release_lag_days"], 5)
        self.assertEqual(by_name["bu_social_stock"]["freshness_threshold_days"], 14)
        self.assertEqual(by_name["macro_cn_pmi"]["freq"], "monthly")
        self.assertEqual(by_name["macro_cn_pmi"]["release_lag_days"], 22)
        self.assertEqual(by_name["macro_cn_pmi"]["freshness_threshold_days"], 45)

    def test_fundamental_prompt_contains_values_registered_as_used(self):
        summary = format_fundamental_summary_for_prompt(
            {
                "sector": "ferrous",
                "tradeability": "medium",
                "finoview_factor_attribution": {
                    "used_factors": [
                        {
                            "factor_name": "macro_cn_pmi",
                            "factor_group": "macro_policy",
                            "data_date": "2025-02-28",
                            "latest_value": 49.5,
                            "freshness_status": "fresh",
                        }
                    ]
                },
            }
        )

        self.assertIn("macro_cn_pmi", summary)
        self.assertIn("49.5", summary)

    def test_j_rizhao_spot_factor_is_loaded_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pd.DataFrame(
                {
                    "tradeDate": pd.to_datetime(["2025-01-03"]),
                    "j_spot_price_rizhao": [1750.0],
                }
            ).to_feather(root / "j_spot_price_rizhao.feather")
            router = _router(data_dir=root)
            with patch(
                "tools.agent_tools.analysis.analyst_finoview_factors.get_previous_trading_day",
                return_value=datetime(2025, 1, 3),
            ), patch(
                "apis.router.get_previous_trading_day",
                return_value=datetime(2025, 1, 3),
            ):
                result = router.get_china_futures_fundamentals("J", "2025-01-06")

        self.assertIsNotNone(result)
        self.assertEqual(router.last_fundamentals_metadata["configured_indicator_count"], 10)
        self.assertEqual(router.last_fundamentals_metadata["loaded_indicator_count"], 1)

    def test_local_basis_aligns_spot_and_futures_dates_and_uses_spot_denominator(self):
        dates = [
            "2025-01-03",
            "2025-01-06",
            "2025-01-07",
            "2025-01-08",
            "2025-01-09",
            "2025-01-10",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pd.DataFrame(
                {
                    "tradeDate": pd.to_datetime(dates),
                    "bu_spot_price": [98.0, 100.0, 102.0, 104.0, 106.0, 110.0],
                }
            ).to_feather(root / "bu_spot_price.feather")
            api = _FundamentalAPI(
                [SimpleNamespace(trade_date=date, close=90.0) for date in dates]
            )
            router = _router(data_dir=root, api=api)
            with patch(
                "tools.agent_tools.analysis.analyst_finoview_factors.get_previous_trading_day",
                return_value=datetime(2025, 1, 10),
            ), patch(
                "apis.router.get_previous_trading_day",
                return_value=datetime(2025, 1, 10),
            ):
                router.get_china_futures_fundamentals("BU", "2025-01-13")

        basis = router.last_fundamentals_metadata["basis"]
        self.assertEqual(basis["date"], "2025-01-10")
        self.assertAlmostEqual(basis["latest_pct"], (20.0 / 110.0) * 100.0)
        self.assertAlmostEqual(basis["trend_5d"], 150.0)
        self.assertTrue(api.calls[0]["end_date_inclusive"])

    def test_news_router_filters_product_irrelevant_items_before_latest_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blocks = []
            for index in range(11):
                blocks.append(
                    "\n".join(
                        [
                            "2025-01-03",
                            f"铜矿企业第{index}次公布产量",
                            "铜精矿和铜产量增加",
                            "产业",
                            "测试源",
                        ]
                    )
                )
            blocks.append(
                "\n".join(
                    [
                        "2025-01-03",
                        "螺纹钢库存继续下降",
                        "钢材库存下降且建筑需求改善",
                        "产业",
                        "测试源",
                    ]
                )
            )
            (root / "RB.txt").write_text("\n\n".join(blocks), encoding="utf-8")
            router = _router(data_dir=root, news_dir=root)
            with patch(
                "apis.router.get_previous_trading_day",
                return_value=datetime(2025, 1, 3),
            ):
                news = router.get_china_futures_news(
                    "RB",
                    "2025-01-06",
                    news_count=10,
                    pre_open_only=True,
                )

        self.assertEqual([item.title for item in news], ["螺纹钢库存继续下降"])
        self.assertEqual(router.last_news_metadata["selected_news_count"], 1)

    def test_news_relevance_is_product_specific_not_fixed_for_non_empty_input(self):
        context = summarize_news_events(
            [
                SimpleNamespace(
                    title="铜矿企业公布产量",
                    content="铜精矿供应增加",
                    publish_time="2025-01-03",
                ),
                SimpleNamespace(
                    title="螺纹钢库存下降",
                    content="钢材需求改善",
                    publish_time="2025-01-03",
                ),
            ],
            "RB",
            "2025-01-06",
        )

        self.assertEqual([item["title"] for item in context["events"]], ["螺纹钢库存下降"])
        self.assertEqual(context["relevance_score"], 1.0)

    def test_minute_provider_failure_is_not_reported_as_valid_empty_market(self):
        with self.assertRaisesRegex(RuntimeError, "intraday_market_data_fetch_failed"):
            resolve_intraday_execution_basis(
                router=_MinuteFailureRouter(),
                config={"execution": {"intraday_confirmation": {"enabled": True}}},
                underlying_code="RB",
                trading_date="2025-03-26",
                action="open_long",
                contract_code="rb2505",
            )

    def test_real_empty_minute_response_remains_legal_no_valid_bar(self):
        _basis, selection = resolve_intraday_execution_basis(
            router=_EmptyMinuteRouter(),
            config={"execution": {"intraday_confirmation": {"enabled": True}}},
            underlying_code="RB",
            trading_date="2025-03-26",
            action="open_long",
            contract_code="rb2505",
            decision_context={
                "execution_contract": {
                    "execution_profile": "breakout",
                    "trigger_source": "technical_breakout",
                    "entry_trigger": "15分钟收盘价向上突破开盘区间上沿且高于VWAP",
                }
            },
        )

        self.assertEqual(selection.reason, "intraday_no_valid_bar")


if __name__ == "__main__":
    unittest.main()
