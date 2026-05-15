"""DeepAnalyze client for enhanced market and fundamental interpretation."""

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from util.logger import logger
except ImportError:
    class SimpleLogger:
        def info(self, msg):
            print(f"[INFO] {msg}")

        def warning(self, msg):
            print(f"[WARNING] {msg}")

        def error(self, msg):
            print(f"[ERROR] {msg}")

        def debug(self, msg):
            print(f"[DEBUG] {msg}")

    logger = SimpleLogger()


class DeepAnalyzeClient:
    """Client wrapper around the local or cloud DeepAnalyze endpoint."""

    def __init__(self, config_path: str = None):
        import os
        import yaml

        if config_path is None:
            project_root = Path(__file__).parent.parent.parent
            config_path = project_root / "src" / "config" / "dev.yaml"

        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        deepanalyze_config = config.get("deepanalyze", {})
        mode = deepanalyze_config.get("mode", "local")

        if mode == "cloud":
            cloud_config = deepanalyze_config.get("cloud", {})
            self.base_url = None
            self.api_endpoint = cloud_config.get("api_url")
            self.model_name = cloud_config.get("model_name", "deepanalyze-8b")
            self.timeout = cloud_config.get("timeout", 240)
            self.max_retries = cloud_config.get("max_retries", 2)
            self.mode = "cloud"
            self.api_key = os.getenv("DEEPANALYZE_API_KEY")
            if not self.api_key:
                raise ValueError("Missing environment variable DEEPANALYZE_API_KEY")
            logger.info("DeepAnalyze cloud mode enabled")
        else:
            local_config = deepanalyze_config.get("local", {})
            self.base_url = local_config.get("base_url", "http://localhost:8000")
            self.api_endpoint = f"{self.base_url}/v1/chat/completions"
            self.model_name = local_config.get("model_name", "DeepAnalyze-8B")
            self.timeout = local_config.get("timeout", 240)
            self.max_retries = local_config.get("max_retries", 2)
            self.mode = "local"
            self.api_key = None
            logger.info(f"DeepAnalyze local mode | {self.base_url}")

    def _call_api(self, prompt: str, max_tokens: int = 512, retry_count: int = 0) -> Optional[str]:
        headers = {"Content-Type": "application/json"}
        if self.mode == "cloud" and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a financial market data-analysis expert."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }

        start_time = time.time()
        try:
            response = requests.post(
                self.api_endpoint,
                headers=headers,
                data=json.dumps(payload),
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            elapsed_time = time.time() - start_time
            logger.info(
                f"DeepAnalyze API success | elapsed={elapsed_time:.2f}s | "
                f"prompt_chars={len(prompt)} | max_tokens={max_tokens}"
            )
            return result["choices"][0]["message"]["content"]

        except requests.exceptions.Timeout:
            elapsed_time = time.time() - start_time
            logger.warning(
                f"DeepAnalyze API timeout | elapsed={elapsed_time:.2f}s | timeout={self.timeout}s"
            )
            if retry_count < self.max_retries:
                logger.info(f"Retrying DeepAnalyze request ({retry_count + 1}/{self.max_retries})")
                time.sleep(1)
                return self._call_api(prompt, max_tokens, retry_count + 1)
            return None

        except requests.exceptions.ConnectionError:
            target = self.base_url or self.api_endpoint
            logger.error(f"DeepAnalyze service unavailable or unreachable: {target}")
            logger.error(
                "Please check whether the local LLM service is running, e.g. "
                "'python -m vllm.entrypoints.openai.api_server'."
            )
            return None

        except Exception as exc:
            elapsed_time = time.time() - start_time
            logger.error(f"DeepAnalyze API failure | elapsed={elapsed_time:.2f}s | error={exc}")
            return None

    def analyze_market_state(
        self,
        price_df: pd.DataFrame,
        ticker: str,
        technical_indicators: dict = None,
    ) -> str:
        """Generate a free-form market-state report for futures analysis."""
        data_summary = self._prepare_price_summary(price_df, ticker)
        if technical_indicators:
            indicators_summary = self._prepare_indicators_summary(technical_indicators)
            data_summary += f"\n\nTechnical indicator snapshot:\n{indicators_summary}"

        prompt = f"""Analyze the following futures market snapshot.

Ticker: {ticker}

{data_summary}

Please provide a concise English report covering:
1. Market state: trending / ranging / reversal
2. Trend direction: up / down / sideways
3. Volatility level: high / medium / low
4. Parameter guidance for technical trading under the current regime
5. Best-fit strategy family: trend-following / mean-reversion / hybrid
6. Confidence score between 0 and 1

Keep the report under 300 words.
"""

        report = self._call_api(prompt, max_tokens=512)
        if not report:
            logger.warning(f"{ticker}: DeepAnalyze market-state call failed; using fallback report")
            return (
                f"{ticker} market state fallback: ranging market, sideways trend, "
                "medium volatility, conservative settings, confidence 0.30."
            )

        logger.info(f"{ticker}: DeepAnalyze market report generated")
        return report

    def _prepare_indicators_summary(self, indicators: dict) -> str:
        signal_map = {
            "BULLISH": "bullish",
            "BEARISH": "bearish",
            "NEUTRAL": "neutral",
        }

        primary_signals = []
        secondary_signals = []

        for name, value in indicators.items():
            if hasattr(value, "name"):
                signal_label = signal_map.get(value.name, "unknown")
                token = f"{name.upper()[:4]}:{signal_label}"
                if name in {"trend", "macd", "adx"}:
                    primary_signals.append(token)
                elif name not in {"gap_analysis", "futures_volatility", "turnover_value", "price_levels"}:
                    secondary_signals.append(token)

        summary_lines = []
        if primary_signals:
            summary_lines.append(f"Primary: {' '.join(primary_signals)}")
        if secondary_signals:
            summary_lines.append(f"Secondary: {' '.join(secondary_signals)}")
        return "\n".join(summary_lines)

    def _prepare_price_summary(self, price_df: pd.DataFrame, ticker: str) -> str:
        if price_df.empty:
            return f"Ticker: {ticker}\nData: unavailable"

        latest = price_df.iloc[-1]
        returns = price_df["close"].pct_change()

        summary = f"""Ticker: {ticker}
Rows: {len(price_df)}
Date range: {price_df.index[0]} to {price_df.index[-1]}

Latest bar:
- Open: {latest['open']:.2f}
- High: {latest['high']:.2f}
- Low: {latest['low']:.2f}
- Close: {latest['close']:.2f}
- Volume: {latest['volume']:,.0f}

Recent features:
- 5-day return: {returns.tail(5).sum() * 100:.2f}%
- 20-day return: {returns.tail(20).sum() * 100:.2f}%
- 20-day volatility: {returns.tail(20).std() * 100:.2f}%
"""

        if "open_interest" in price_df.columns:
            summary += f"- Open interest: {latest['open_interest']:,.0f}\n"
        if "settle_price" in price_df.columns:
            summary += f"- Settlement price: {latest['settle_price']:.2f}\n"

        return summary

    def _parse_json_response(self, response: str, default_result: Dict[str, Any]) -> Dict[str, Any]:
        import re

        logger.debug(f"DeepAnalyze raw response preview: {response[:500]}")

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except Exception:
                pass

        json_match = re.search(r"\{.*\}", response, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except Exception:
                pass

        cleaned = response.strip()
        cleaned = re.sub(r"```json\s*", "", cleaned)
        cleaned = re.sub(r"```\s*", "", cleaned)
        first_brace = cleaned.find("{")
        if first_brace > 0:
            cleaned = cleaned[first_brace:]
        last_brace = cleaned.rfind("}")
        if last_brace >= 0:
            cleaned = cleaned[: last_brace + 1]

        try:
            return json.loads(cleaned)
        except Exception:
            logger.warning("All JSON parsing attempts failed; using default DeepAnalyze result")
            logger.warning(f"Full response:\n{response}")
            logger.warning(f"Cleaned response:\n{cleaned}")
            return default_result

    def _get_default_market_state(self) -> Dict[str, Any]:
        return {
            "market_state": "ranging",
            "trend_direction": "sideways",
            "volatility_level": "medium",
            "confidence": 0.3,
            "reasoning": "DeepAnalyze API failed; using conservative fallback market state.",
        }

    def analyze_fundamental_trends(self, fundamentals_text: str, ticker: str) -> Dict[str, Any]:
        """Generate structured futures-fundamental interpretation."""
        prompt = f"""Analyze the following futures fundamental data.

Ticker: {ticker}
{fundamentals_text}

Please provide a concise English report covering:
- Inventory trend: accelerating decline / stable decline / slight decline / stable / slight increase / increase
- Inventory outlook: expect decline / stabilize / recover
- Profit status: high / normal / low / loss
- Profit trend: improving / stable / deteriorating
- Supply-demand balance: tight / slightly tight / balanced / slightly loose / loose
- Key drivers
- Confidence score between 0 and 1

Keep the report concise and explicit.
"""

        report = self._call_api(prompt, max_tokens=1024)
        if not report:
            logger.warning(f"{ticker}: DeepAnalyze fundamental call failed; using fallback values")
            return self._get_default_fundamental_analysis()

        logger.info(f"{ticker}: DeepAnalyze fundamental report generated")
        result = self._extract_fields_from_report(report, ticker)
        self._validate_and_log_result("fundamental", ticker, result)
        return result

    def _extract_fields_from_report(self, report: str, ticker: str) -> Dict[str, Any]:
        import re

        lower_report = report.lower()

        supply_map = {
            "slightly tight": "slightly_tight",
            "slightly loose": "slightly_loose",
            "tight": "tight",
            "balanced": "balanced",
            "loose": "loose",
            "紧缺": "tight",
            "偏紧": "slightly_tight",
            "平衡": "balanced",
            "偏松": "slightly_loose",
            "过剩": "loose",
        }
        inventory_map = {
            "accelerating decline": "accelerating_decline",
            "stable decline": "stable_decline",
            "slight decline": "slight_decline",
            "slight increase": "slight_increase",
            "increase": "increase",
            "stable": "stable",
            "加速下降": "accelerating_decline",
            "稳定下降": "stable_decline",
            "小幅下降": "slight_decline",
            "小幅上升": "slight_increase",
            "上升": "increase",
            "稳定": "stable",
        }
        profit_map = {
            "loss": "loss",
            "high": "high",
            "normal": "normal",
            "low": "low",
            "亏损": "loss",
            "高": "high",
            "正常": "normal",
            "低": "low",
        }

        supply_demand = "balanced"
        for keyword, value in supply_map.items():
            if keyword in lower_report or keyword in report:
                supply_demand = value
                break

        inventory_trend = "stable"
        for keyword, value in inventory_map.items():
            if keyword in lower_report or keyword in report:
                inventory_trend = value
                break

        profit_status = "normal"
        for keyword, value in profit_map.items():
            if keyword in lower_report or keyword in report:
                profit_status = value
                break

        if "expect decline" in lower_report or "further decline" in lower_report or "继续下降" in report:
            inventory_outlook = "expect_decline"
        elif "recover" in lower_report or "rebound" in lower_report or "恢复" in report:
            inventory_outlook = "recover"
        else:
            inventory_outlook = "stabilize"

        if "improving" in lower_report or "改善" in report:
            profit_trend = "improving"
        elif "deteriorating" in lower_report or "恶化" in report:
            profit_trend = "deteriorating"
        else:
            profit_trend = "stable"

        confidence_match = re.search(r"confidence\s*[:=]?\s*([0-9.]+)", lower_report)
        if confidence_match:
            try:
                confidence = float(confidence_match.group(1))
                confidence = min(max(confidence, 0.3), 0.95)
            except ValueError:
                confidence = 0.7
        else:
            confidence = 0.7

        key_drivers = []
        driver_patterns = [
            r"key drivers?\s*[:：]\s*([^\n]+)",
            r"drivers?\s*[:：]\s*([^\n]+)",
            r"关键驱动\s*[:：]\s*([^\n]+)",
        ]
        for pattern in driver_patterns:
            matches = re.findall(pattern, report, re.IGNORECASE)
            for match in matches:
                parts = [part.strip(" -.;") for part in re.split(r"[,;，；]", match) if part.strip()]
                key_drivers.extend(parts)
            if key_drivers:
                break

        reasoning = report[:300] if len(report) > 300 else report
        result = {
            "inventory_trend": inventory_trend,
            "inventory_outlook": inventory_outlook,
            "profit_margin_status": profit_status,
            "profit_margin_trend": profit_trend,
            "supply_demand_balance": supply_demand,
            "key_drivers": key_drivers[:3],
            "confidence": confidence,
            "reasoning": reasoning,
        }

        logger.info(f"  Inventory trend: {result['inventory_trend']}")
        logger.info(f"  Supply-demand balance: {result['supply_demand_balance']}")
        logger.info(f"  Profit status: {result['profit_margin_status']}")
        logger.info(f"  Confidence: {result['confidence']:.2f}")
        return result

    def _get_default_fundamental_analysis(self) -> Dict[str, Any]:
        return {
            "inventory_trend": "stable",
            "inventory_outlook": "stabilize",
            "profit_margin_status": "normal",
            "profit_margin_trend": "stable",
            "supply_demand_balance": "balanced",
            "key_drivers": [],
            "confidence": 0.30,
            "reasoning": "DeepAnalyze API failed; using conservative fallback fundamentals.",
        }

    def _validate_and_log_result(self, analysis_type: str, ticker: str, result: Dict[str, Any]) -> None:
        confidence = result.get("confidence", 0.0)

        if confidence <= 0.31:
            logger.warning(
                f"{ticker}: {analysis_type} analysis confidence is very low ({confidence:.2f}); "
                "fallback values may have been used"
            )
            logger.warning(f"  Details: {result}")
        elif confidence >= 0.70:
            logger.info(f"{ticker}: {analysis_type} analysis completed with high confidence ({confidence:.2f})")
        else:
            logger.info(f"{ticker}: {analysis_type} analysis completed with medium confidence ({confidence:.2f})")

        if analysis_type == "fundamental":
            logger.info(f"  Inventory trend: {result.get('inventory_trend')}")
            logger.info(f"  Supply-demand balance: {result.get('supply_demand_balance')}")
        elif analysis_type == "market_state":
            logger.info(f"  Market state: {result.get('market_state')}")
            logger.info(f"  Trend direction: {result.get('trend_direction')}")

    def extract_market_state_from_text(self, report: str, ticker: str) -> Dict[str, Any]:
        """Extract structured market-state labels from the free-form report."""
        import re

        lower_report = report.lower()

        market_state_map = {
            "trending": "trending",
            "ranging": "ranging",
            "reversal": "reversal",
            "趋势市": "trending",
            "震荡市": "ranging",
            "反转市": "reversal",
        }
        trend_map = {
            "sideways": "sideways",
            "down": "down",
            "up": "up",
            "横盘": "sideways",
            "下跌": "down",
            "上涨": "up",
        }
        volatility_map = {
            "medium": "medium",
            "high": "high",
            "low": "low",
            "中": "medium",
            "高": "high",
            "低": "low",
        }

        market_state = "ranging"
        for keyword, value in market_state_map.items():
            if keyword in lower_report or keyword in report:
                market_state = value
                break

        trend_direction = "sideways"
        for keyword, value in trend_map.items():
            if keyword in lower_report or keyword in report:
                trend_direction = value
                break

        volatility_level = "medium"
        for keyword, value in volatility_map.items():
            if keyword in lower_report or keyword in report:
                volatility_level = value
                break

        confidence_match = re.search(r"confidence\s*[:=]?\s*([0-9.]+)", lower_report)
        if confidence_match:
            try:
                confidence = float(confidence_match.group(1))
                confidence = max(0.3, min(confidence, 0.95))
            except ValueError:
                confidence = 0.7
        else:
            confidence = 0.7

        reasoning = report[:200] if len(report) > 200 else report
        result = {
            "market_state": market_state,
            "trend_direction": trend_direction,
            "volatility_level": volatility_level,
            "confidence": confidence,
            "reasoning": reasoning,
        }

        logger.info(
            f"  Extracted market state: {market_state} | "
            f"trend={trend_direction} | volatility={volatility_level}"
        )
        logger.info(f"  Analysis confidence: {confidence:.2f}")
        return result
