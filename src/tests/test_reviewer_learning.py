import json
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.decision_team.auditor import TradeAuditor, TradeAuditorInput
from agents.decision_team.portfolio_manager import (
    _apply_capital_utilization_control,
    _quality_aware_fusion_context,
    _resolve_net_exposure_control,
    get_hard_allocation_margin_ratio,
)
from database.sqlite_setup import _ensure_reviewer_learning_schema, _ensure_strategy_memory_schema
from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.analysis.business_quality import apply_business_quality_enrichment
from tools.agent_tools.contracts import attach_snapshot_contract, validate_artifact_header
from database.artifact_store import load_externalized_json
from tools.agent_tools.research.template_prior import _project_path, classify_template_prior_item, load_template_prior_if_enabled
from tools.agent_tools.analysis.dynamic_weights import calibrate_weights_by_signal_history
from tools.agent_tools.analysis.learning_context import (
    apply_config_learning_overlay,
    build_learning_context,
    clear_learning_context_cache,
)
from tools.agent_tools.analysis.technical_parameter_calibration import apply_technical_parameter_calibration
from tools.agent_tools.research.learning_contract import CONTRACT_KEY
from tools.agent_tools.research.neutral_accountability import (
    build_neutral_accountability_summary,
    classify_neutral_signal,
)
from tools.agent_tools.analysis.quality import build_technical_context, apply_signal_quality_gate
from tools.agent_tools.research.researcher_tools import (
    ExploratoryHypothesisItem,
    ExploratoryHypothesisLLMOutput,
    write_exploratory_hypotheses,
)
from tools.agent_tools.research.reviewer_tools import (
    _export_template_prior,
    _horizon_class,
    _learned_vs_unlearned_trade_performance,
    _backfill_neutral_forward_shadow_tracking,
    _neutral_shadow_tracking_summary,
    _write_trade_episode_memory,
    _write_learned_vs_unlearned_policy_state,
    _write_validated_causal_policy_rules,
    _write_config_overlay,
    _backfill_no_trade_opportunity_shadow_results,
    _write_reviewer_learning_report,
    _write_alpha_promotion_state,
    _write_contextual_rule_calibration_state,
    _write_no_trade_opportunity_memory,
    _write_tail_loss_sentinel_state,
    _write_loss_template_observation_research,
    _validate_phase1_signal_persistence,
    _write_signal_context_history,
)


class _FakeLearningDB:
    def __init__(self):
        self.budgets = []
        self.digest_calls = 0

    def get_analyst_learning_digest(self, **kwargs):
        self.digest_calls += 1
        return [
            {
                "id": f"digest-{idx}",
                "ticker": kwargs["ticker"],
                "horizon_class": kwargs.get("horizon_class") or "short",
                "market_regime": "trend",
                "sample_count": 3 + idx,
                "confidence_score": 0.4,
                "digest_text": "mature observation " + ("x" * 80),
            }
            for idx in range(10)
        ]

    def save_learning_context_budget(self, **kwargs):
        self.budgets.append(kwargs)
        return True


class _ExploratoryLearningDB(_FakeLearningDB):
    def get_analyst_learning_digest(self, **kwargs):
        self.digest_calls += 1
        return []

    def get_trade_episode_memory(self, **kwargs):
        return [
            {
                "id": "episode-1",
                "ticker": kwargs.get("ticker", "BU"),
                "side": "long",
                "horizon_class": "short",
                "market_regime": "trend",
                "signal_template": "long_breakout_short",
                "holding_days": 2,
                "net_pnl": 1250.0,
                "lesson_text": "breakout held while inventory and price trend agreed",
                "payload": {
                    CONTRACT_KEY: {
                        "usable_memory": ["same-scope breakout episode worked"],
                        "usage_boundary": ["same-scope prior only"],
                        "position_impact_conditions": ["current confirmation and invalidation required"],
                    }
                },
            }
        ]

    def get_no_trade_opportunity_memory(self, **kwargs):
        return [
            {
                "id": "no-trade-1",
                "ticker": kwargs.get("ticker", "BU"),
                "side": "long",
                "horizon_class": "short",
                "market_regime": "trend",
                "signal_template": "long_breakout_short",
                "opportunity_type": "trend_continuation",
                "classification": "missed_opportunity",
                "pm_reason": "intraday trigger not met",
                "shadow_results": [{"horizon_days": 3, "shadow_pnl": 1800.0}],
                "payload": {
                    "neutral_opportunity_observations": [
                        {
                            "analyst": "technical",
                            "bucket": "watchlist_trigger",
                            "watchlist_priority": "medium",
                            "trigger_condition": "breakout confirms with volume",
                            "shadow_side": "long",
                        }
                    ],
                    CONTRACT_KEY: {
                        "usable_memory": ["skipped breakout became a missed opportunity"],
                        "usage_boundary": ["watchlist only until current trigger confirms"],
                        "position_impact_conditions": ["no sizing impact without current confirmation"],
                    },
                },
            }
        ]

    def get_exploratory_hypotheses(self, **kwargs):
        return [
            {
                "id": "hypothesis-1",
                "ticker": kwargs.get("ticker", "BU"),
                "sector": kwargs.get("sector", "energy"),
                "side": "long",
                "horizon_class": "short",
                "market_regime": "trend",
                "hypothesis_text": "BU trend probes need current price confirmation plus explicit invalidation.",
                "suggested_use": "analyst_prior",
                "payload": {
                    "entry_timing_hint": "wait for trend confirmation",
                    "exit_timing_hint": "cut if confirmation fails",
                    "holding_period_hint": "short",
                    "invalidation_condition": "price falls back below breakout level",
                    "validation_plan": "track next similar BU trend probes",
                    CONTRACT_KEY: {
                        "usable_memory": ["BU trend probes need confirmation"],
                        "usage_boundary": ["candidate hypothesis cannot control position"],
                        "position_impact_conditions": ["probe only until validated"],
                    },
                },
                "confidence_score": 0.55,
                "sample_count": 3,
                "status": "candidate",
            }
        ]


class _FallbackLearningDB:
    def __init__(self):
        self.calls = []
        self.budgets = []

    def get_analyst_learning_digest(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("horizon_class") != "*":
            return []
        return [
            {
                "id": "digest-short",
                "ticker": kwargs["ticker"],
                "horizon_class": "short",
                "market_regime": "*",
                "sample_count": 4,
                "confidence_score": 0.7,
                "digest_text": "short-horizon mature observation remains useful as a fallback prior",
            }
        ]

    def save_learning_context_budget(self, **kwargs):
        self.budgets.append(kwargs)
        return True


class _SectorFallbackLearningDB:
    def __init__(self):
        self.calls = []
        self.budgets = []

    def get_analyst_learning_digest(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("ticker") != "*" or kwargs.get("sector") != "ferrous":
            return []
        return [
            {
                "id": "sector-digest",
                "ticker": "HC",
                "sector": "ferrous",
                "horizon_class": "medium",
                "market_regime": "*",
                "sample_count": 5,
                "confidence_score": 0.65,
                "digest_text": "ferrous fundamental evidence worked when inventory and margin agreed",
            }
        ]

    def save_learning_context_budget(self, **kwargs):
        self.budgets.append(kwargs)
        return True


class _FakeOverlayDB:
    def get_config_learning_overlay(self, **kwargs):
        return [
            {
                "id": "ok",
                "param_key": "capital_utilization_control.target_margin_ratio_min",
                "learned_value": 0.16,
                "source": "reviewer",
                "confidence_score": 0.9,
            },
            {
                "id": "blocked",
                "param_key": "llm.model",
                "learned_value": "should-not-apply",
                "source": "reviewer",
                "confidence_score": 0.9,
            },
        ]


class _PriorBootstrapDB:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_strategy_memory_schema(self, cursor):
        _ensure_strategy_memory_schema(cursor)


class ReviewerLearningContextTest(unittest.TestCase):
    def tearDown(self):
        clear_learning_context_cache()

    def test_learning_context_is_bounded_and_budget_logged(self):
        clear_learning_context_cache()
        db = _FakeLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {"enabled": True, "max_items_per_prompt": 3, "max_chars_per_prompt": 260},
            },
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="technical",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="short",
        )

        self.assertTrue(context["enabled"])
        self.assertLessEqual(len(context["selected_ids"]), 3)
        self.assertLessEqual(len(context["text"]), 520)
        self.assertEqual(len(db.budgets), 1)
        self.assertGreater(db.budgets[0]["dropped_count"], 0)
        self.assertEqual(db.budgets[0]["digest_count"], len(context["selected_ids"]))
        self.assertEqual(db.budgets[0]["trade_episode_count"], 0)
        self.assertEqual(db.budgets[0]["hypothesis_count"], 0)
        self.assertGreater(db.budgets[0]["total_context_chars"], 0)

    def test_learning_context_cache_reuses_same_day_same_scope_result(self):
        clear_learning_context_cache()
        db = _FakeLearningDB()
        full_config = {
            "learning": {"enabled": True},
            "learning_context": {
                "enabled": True,
                "cache": {"enabled": True},
                "max_items_per_prompt": 3,
                "max_chars_per_prompt": 260,
            },
        }
        first = build_learning_context(
            db=db,
            full_config=full_config,
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="technical",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="short",
        )
        second = build_learning_context(
            db=db,
            full_config=full_config,
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="technical",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="short",
        )

        self.assertEqual(first["selected_ids"], second["selected_ids"])
        self.assertEqual(db.digest_calls, 1)
        self.assertEqual(len(db.budgets), 1)

    def test_learning_context_falls_back_when_requested_horizon_is_missing(self):
        db = _FallbackLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {"enabled": True, "max_items_per_prompt": 3, "max_chars_per_prompt": 500},
            },
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="fundamental",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="medium",
        )

        self.assertTrue(context["enabled"])
        self.assertIn("digest-short", context["selected_ids"])
        self.assertEqual(context["requested_horizon_class"], "medium")
        self.assertIn("short", context["matched_horizon_classes"])
        self.assertIn("any_horizon", context["retrieval_scopes"])

    def test_learning_context_can_use_same_sector_digest_after_ticker_miss(self):
        db = _SectorFallbackLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {
                    "enabled": True,
                    "max_items_per_prompt": 3,
                    "max_chars_per_prompt": 500,
                    "allow_cross_ticker_sector_fallback": True,
                },
            },
            config_id="cfg",
            trading_date="2025-02-10",
            analyst="fundamental",
            ticker="RB",
            context={"sector": "ferrous", "market_regime": "trend"},
            horizon_class="medium",
        )

        self.assertEqual(context["selected_ids"], ["sector-digest"])
        self.assertIn("same_sector", ",".join(context["retrieval_scopes"]))

    def test_learning_context_includes_exploratory_memory_as_prior_only(self):
        db = _ExploratoryLearningDB()
        context = build_learning_context(
            db=db,
            full_config={
                "learning": {"enabled": True},
                "learning_context": {
                    "enabled": True,
                    "max_items_per_prompt": 3,
                    "max_chars_per_prompt": 500,
                    "exploratory_memory": {
                        "enabled": True,
                        "max_episode_items": 2,
                        "max_episode_chars": 900,
                        "max_hypothesis_items": 2,
                        "max_hypothesis_chars": 900,
                    },
                },
            },
            config_id="cfg",
            trading_date="2025-03-12",
            analyst="technical",
            ticker="BU",
            context={"sector": "energy", "market_regime": "trend"},
            horizon_class="short",
        )

        self.assertIn("Similar completed trade episodes", context["text"])
        self.assertIn("No-trade opportunity memories", context["text"])
        self.assertIn("neutral=technical:watchlist_trigger/medium", context["text"])
        self.assertIn("trigger=breakout confirms with volume", context["text"])
        self.assertIn("Exploratory hypotheses under validation", context["text"])
        self.assertIn("not trading authority", context["text"])
        self.assertIn("cannot size, add, justify position_matched", context["text"])
        self.assertIn("Next-round strategy update", context["text"])
        self.assertIn("position=current confirmation and invalidation required", context["text"])
        self.assertIn("position=no sizing impact without current confirmati", context["text"])
        self.assertIn("position=probe only until validated", context["text"])
        self.assertIn("rebuttable priors", context["text"])
        self.assertIn("confirms or contradicts", context["text"])
        self.assertIn("entry=wait for trend confirmation", context["text"])
        self.assertIn("invalidation=price falls back below breakout level", context["text"])
        self.assertEqual(len(context["trade_episode_items"]), 1)
        self.assertEqual(len(context["no_trade_opportunity_items"]), 1)
        self.assertEqual(len(context["hypothesis_items"]), 1)
        self.assertEqual(context["candidate_hypothesis_count"], 1)
        self.assertEqual(db.budgets[0]["trade_episode_count"], 1)
        self.assertEqual(db.budgets[0]["hypothesis_count"], 1)
        self.assertGreater(db.budgets[0]["total_context_chars"], db.budgets[0]["selected_chars"])

    def test_config_overlay_uses_allowlist(self):
        config = apply_config_learning_overlay(
            {"learning": {"enabled": True, "config_overlay": {"enabled": True}}, "llm": {"model": "keep"}},
            db=_FakeOverlayDB(),
            config_id="cfg",
            trading_date="2025-02-10",
        )

        self.assertEqual(config["capital_utilization_control"]["target_margin_ratio_min"], 0.16)
        self.assertEqual(config["llm"]["model"], "keep")
        self.assertEqual(len(config["runtime_learning_overlay"]["applied"]), 1)
        self.assertEqual(len(config["runtime_learning_overlay"]["skipped"]), 1)

    def test_reviewer_writes_trade_episode_memory_from_closed_trade(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                reference_portfolio_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                effective_trade_date TEXT NOT NULL,
                source_type TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                from_contract TEXT,
                to_contract TEXT,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                base_price REAL,
                base_price_source TEXT,
                base_price_date TEXT,
                open_price REAL,
                prev_close_price REAL,
                slippage_model TEXT,
                slippage_ticks INTEGER,
                slippage_amount REAL,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                signal_snapshot_artifact_path TEXT,
                signal_snapshot_sha256 TEXT,
                audit_payload TEXT,
                audit_payload_artifact_path TEXT,
                audit_payload_sha256 TEXT,
                warning_message TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                price REAL,
                execution_price REAL NOT NULL,
                settle_price REAL,
                contract_multiplier REAL NOT NULL,
                margin_rate REAL NOT NULL,
                margin_used REAL NOT NULL,
                daily_pnl REAL DEFAULT 0,
                commission REAL DEFAULT 0,
                source_type TEXT,
                execution_phase TEXT,
                audit_payload TEXT,
                warning_message TEXT,
                booked_in_settlement BOOLEAN DEFAULT 0,
                justification TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        snapshot = {
            "technical": {"signal": "Bullish", "confidence": 0.72, "template_name": "breakout", "horizon_class": "short"},
            "fundamental": {"signal": "Neutral", "confidence": 0.40, "horizon_class": "medium"},
            "commodity_news": {"signal": "Bullish", "confidence": 0.61, "horizon_class": "event_short"},
            "pre_open_plan": {"invalidation_level": 3180, "market_regime": "trend"},
            "market_confirmation": {"confirmation_score": 0.74},
        }
        cursor.execute(
            """
            INSERT INTO futures_recommendation (
                id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                source_type, underlying_code, contract_code, action, lots, execution_price,
                justification, signal_snapshot, status, created_at
            ) VALUES (?, 'cfg', 'pf', '2025-03-10', '2025-03-10',
                'strategy', 'BU', 'bu2506', 'open_long', 1, 3200,
                'open long', ?, 'pending', '2025-03-10T09:00:00')
            """,
            ("rec-open", json.dumps(snapshot, ensure_ascii=False)),
        )
        cursor.executemany(
            """
            INSERT INTO futures_transactions (
                id, portfolio_id, config_id, recommendation_id, trading_date, ticker,
                contract_code, action, lots, price, execution_price, settle_price,
                contract_multiplier, margin_rate, margin_used, daily_pnl,
                commission, source_type, execution_phase, created_at
            ) VALUES (?, 'pf', 'cfg', ?, ?, 'BU', 'bu2506', ?, 1, ?, ?, ?,
                10, 0.1, 3200, 0, 1, 'strategy', 'phase2', ?)
            """,
            [
                ("tx-open", "rec-open", "2025-03-10", "open_long", 3200.0, 3200.0, 3200.0, "2025-03-10T09:30:00"),
                ("tx-close", "rec-close", "2025-03-12", "close_long", 3340.0, 3340.0, 3340.0, "2025-03-12T14:30:00"),
            ],
        )

        rows = _write_trade_episode_memory(
            cursor,
            cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-12",
        )

        self.assertEqual(rows, 1)
        cursor.execute("SELECT * FROM trade_episode_memory WHERE config_id='cfg'")
        item = dict(cursor.fetchone())
        self.assertEqual(item["ticker"], "BU")
        self.assertEqual(item["side"], "long")
        self.assertEqual(item["trading_date"], "2025-03-12")
        self.assertEqual(item["episode_date"], "2025-03-12")
        self.assertTrue(item["first_seen_at"])
        self.assertTrue(item["last_reviewed_at"])
        self.assertEqual(item["outcome_label"], "winner")
        self.assertIn("BU long winner", item["lesson_text"])
        payload = load_externalized_json(item["payload_json"])
        contract = payload[CONTRACT_KEY]
        self.assertEqual(contract["memory_type"], "trade_episode_memory")
        self.assertEqual(contract["contract_version"], "next_round_strategy_update_v2")
        self.assertEqual(contract["scope_priority"], "ticker_side_template")
        self.assertIn("analyst_action_items", contract)
        self.assertIn("pm_action_conditions", contract)
        self.assertIn("invalidates_when", contract)
        self.assertEqual(contract["position_authority"], "analysis_or_watchlist_only")
        self.assertEqual(contract["max_position_impact"], "no_direct_position_impact")
        self.assertIn("position_impact_conditions", contract)
        self.assertIn("current-day data", " ".join(contract["usage_boundary"]))
        event = cursor.execute(
            "SELECT action_json FROM learning_event_log WHERE event_type='trade_episode_memory'"
        ).fetchone()
        self.assertIn(CONTRACT_KEY, json.loads(event["action_json"]))
        conn.close()

    def test_trade_episode_memory_keeps_original_review_date_on_refresh(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                reference_portfolio_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                effective_trade_date TEXT NOT NULL,
                source_type TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                price REAL,
                execution_price REAL NOT NULL,
                settle_price REAL,
                contract_multiplier REAL NOT NULL,
                margin_rate REAL NOT NULL,
                margin_used REAL NOT NULL,
                daily_pnl REAL DEFAULT 0,
                commission REAL DEFAULT 0,
                source_type TEXT,
                execution_phase TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        cursor.execute(
            """
            INSERT INTO futures_recommendation (
                id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                source_type, underlying_code, contract_code, action, lots, execution_price,
                justification, signal_snapshot, status, created_at
            ) VALUES ('rec-open', 'cfg', 'pf', '2025-03-10', '2025-03-10',
                'strategy', 'BU', 'bu2506', 'open_long', 1, 3200,
                'open long', '{"pre_open_plan":{"market_regime":"trend"}}', 'pending',
                '2025-03-10T09:00:00')
            """
        )
        cursor.executemany(
            """
            INSERT INTO futures_transactions (
                id, portfolio_id, config_id, recommendation_id, trading_date, ticker,
                contract_code, action, lots, price, execution_price, settle_price,
                contract_multiplier, margin_rate, margin_used, daily_pnl,
                commission, source_type, execution_phase, created_at
            ) VALUES (?, 'pf', 'cfg', ?, ?, 'BU', 'bu2506', ?, 1, ?, ?, ?,
                10, 0.1, 3200, 0, 1, 'strategy', 'phase2', ?)
            """,
            [
                ("tx-open", "rec-open", "2025-03-10", "open_long", 3200.0, 3200.0, 3200.0, "2025-03-10T09:30:00"),
                ("tx-close", "rec-close", "2025-03-12", "close_long", 3340.0, 3340.0, 3340.0, "2025-03-12T14:30:00"),
            ],
        )

        _write_trade_episode_memory(
            cursor,
            cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-12",
        )
        cursor.execute("SELECT trading_date, first_seen_at FROM trade_episode_memory WHERE config_id='cfg'")
        first = dict(cursor.fetchone())
        _write_trade_episode_memory(
            cursor,
            cfg={"learning": {"trade_episode_memory": {"enabled": True}}},
            config_id="cfg",
            trading_date="2025-03-20",
        )
        cursor.execute(
            "SELECT trading_date, episode_date, first_seen_at, last_reviewed_at FROM trade_episode_memory WHERE config_id='cfg'"
        )
        refreshed = dict(cursor.fetchone())
        self.assertEqual(refreshed["trading_date"], "2025-03-12")
        self.assertEqual(refreshed["episode_date"], "2025-03-12")
        self.assertEqual(refreshed["first_seen_at"], first["first_seen_at"])
        self.assertTrue(refreshed["last_reviewed_at"])
        conn.close()

    def test_loss_template_observation_is_candidate_memory_only(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                reference_portfolio_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                effective_trade_date TEXT NOT NULL,
                source_type TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                from_contract TEXT,
                to_contract TEXT,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                base_price REAL,
                base_price_source TEXT,
                base_price_date TEXT,
                open_price REAL,
                prev_close_price REAL,
                slippage_model TEXT,
                slippage_ticks INTEGER,
                slippage_amount REAL,
                execution_price REAL,
                justification TEXT,
                signal_snapshot TEXT,
                signal_snapshot_artifact_path TEXT,
                signal_snapshot_sha256 TEXT,
                audit_payload TEXT,
                audit_payload_artifact_path TEXT,
                audit_payload_sha256 TEXT,
                warning_message TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                contract_code TEXT,
                action TEXT NOT NULL,
                lots INTEGER NOT NULL,
                price REAL,
                execution_price REAL NOT NULL,
                settle_price REAL,
                contract_multiplier REAL NOT NULL,
                margin_rate REAL NOT NULL,
                margin_used REAL NOT NULL,
                daily_pnl REAL DEFAULT 0,
                commission REAL DEFAULT 0,
                source_type TEXT,
                execution_phase TEXT,
                audit_payload TEXT,
                warning_message TEXT,
                booked_in_settlement BOOLEAN DEFAULT 0,
                justification TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_reviewer_learning_schema(cursor)
        snapshot = {
            "technical": {
                "signal": "Bearish",
                "confidence": 0.62,
                "template_name": "breakdown",
                "horizon_class": "short",
                "metadata": {
                    "data_usage_summary": {
                        "pandaai_daily": {"available": True, "used_in_signal": True}
                    }
                },
            },
            "fundamental": {"signal": "Neutral", "confidence": 0.45, "horizon_class": "medium"},
            "commodity_news": {"signal": "Bearish", "confidence": 0.58, "horizon_class": "event_short"},
            "pre_open_plan": {"market_regime": "range"},
        }
        cursor.execute(
            """
            INSERT INTO futures_recommendation (
                id, config_id, reference_portfolio_id, trading_date, effective_trade_date,
                source_type, underlying_code, contract_code, action, lots, execution_price,
                justification, signal_snapshot, status, created_at
            ) VALUES (?, 'cfg', 'pf', '2025-01-06', '2025-01-06',
                'strategy', 'TA', 'ta2505', 'open_short', 1, 5800,
                'open short', ?, 'pending', '2025-01-06T09:00:00')
            """,
            ("rec-ta-open", json.dumps(snapshot, ensure_ascii=False)),
        )
        cursor.executemany(
            """
            INSERT INTO futures_transactions (
                id, portfolio_id, config_id, recommendation_id, trading_date, ticker,
                contract_code, action, lots, price, execution_price, settle_price,
                contract_multiplier, margin_rate, margin_used, daily_pnl,
                commission, source_type, execution_phase, created_at
            ) VALUES (?, 'pf', 'cfg', ?, ?, 'TA', 'ta2505', ?, 1, ?, ?, ?,
                5, 0.1, 2900, 0, 1, 'strategy', 'phase2', ?)
            """,
            [
                ("tx-ta-open", "rec-ta-open", "2025-01-06", "open_short", 5800.0, 5800.0, 5800.0, "2025-01-06T09:30:00"),
                ("tx-ta-close", "rec-ta-close", "2025-01-08", "close_short", 5900.0, 5900.0, 5900.0, "2025-01-08T14:30:00"),
            ],
        )

        rows = _write_loss_template_observation_research(
            cursor,
            cfg={
                "learning": {
                    "loss_template_observation": {
                        "enabled": True,
                        "lookback_days": 30,
                        "min_loss_samples": 1,
                        "min_cumulative_loss_abs": 1,
                        "max_rows_per_day": 2,
                    }
                }
            },
            config_id="cfg",
            trading_date="2025-01-08",
        )

        self.assertEqual(rows, 1)
        item = cursor.execute("SELECT * FROM exploratory_hypothesis WHERE config_id='cfg'").fetchone()
        self.assertEqual(item["ticker"], "TA")
        self.assertEqual(item["status"], "candidate")
        payload = load_externalized_json(
            item["payload_json"],
            item["payload_artifact_path"],
            item["payload_sha256"],
        )
        contract = payload[CONTRACT_KEY]
        self.assertEqual(contract["memory_type"], "loss_template_observation")
        self.assertEqual(contract["position_authority"], "analysis_or_watchlist_only")
        self.assertEqual(contract["max_position_impact"], "no_direct_position_impact")
        self.assertTrue(payload["observation_only"])
        event = cursor.execute(
            "SELECT action_json FROM learning_event_log WHERE event_type='loss_template_observation'"
        ).fetchone()
        self.assertIn(CONTRACT_KEY, json.loads(event["action_json"]))
        conn.close()

    def test_reviewer_exploratory_hypotheses_are_candidate_priors(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
        _ensure_reviewer_learning_schema(cursor)
        cursor.execute(
            """
            INSERT INTO trade_episode_memory (
                id, config_id, trading_date, ticker, side, sector, signal_template,
                signal_combo, horizon_class, market_regime, open_date, close_date,
                holding_days, net_pnl, return_on_notional, outcome_label,
                lesson_text, payload_json, created_at
            ) VALUES
                ('e1', 'cfg', '2025-03-11', 'BU', 'long', 'energy', 'long_breakout_short',
                 '["Bullish","Neutral","Bullish"]', 'short', 'trend', '2025-03-10',
                 '2025-03-11', 1, 1200, 0.02, 'winner', 'BU breakout worked', '{}', 'now'),
                ('e2', 'cfg', '2025-03-12', 'BU', 'long', 'energy', 'long_breakout_short',
                 '["Bullish","Neutral","Bullish"]', 'short', 'trend', '2025-03-11',
                 '2025-03-12', 1, -400, -0.01, 'loser', 'late breakout failed', '{}', 'now')
            """
        )

        def fake_agent_call(**kwargs):
            return ExploratoryHypothesisLLMOutput(
                hypotheses=[
                    ExploratoryHypothesisItem(
                        ticker="BU",
                        sector="energy",
                        side="long",
                        horizon_class="short",
                        market_regime="trend",
                        hypothesis_text="BU long breakouts require current confirmation and explicit invalidation.",
                        evidence_summary="two recent BU episodes",
                        suggested_use="probe_candidate",
                        entry_timing_hint="wait for price confirmation",
                        exit_timing_hint="exit if confirmation disappears",
                        holding_period_hint="short",
                        invalidation_condition="breakout fails before close",
                        validation_plan="validate with future BU trend samples",
                        confidence_score=0.52,
                    )
                ]
            )

        with patch("llm.inference.agent_call", side_effect=fake_agent_call):
            summary = write_exploratory_hypotheses(
                cursor,
                cfg={
                    "llm": {"model": "unit-test"},
                    "max_total_margin_ratio": 0.20,
                    "learning": {
                        "memory_expires_after_days": 30,
                        "exploratory_research": {
                            "enabled": True,
                            "use_llm": True,
                            "min_episode_samples": 2,
                            "max_episode_samples": 4,
                            "max_hypotheses_per_day": 3,
                        },
                    },
                },
                config_id="cfg",
                trading_date="2025-03-12",
            )

        self.assertEqual(summary["rows"], 1)
        cursor.execute("SELECT * FROM exploratory_hypothesis WHERE config_id='cfg'")
        item = dict(cursor.fetchone())
        self.assertEqual(item["status"], "candidate")
        self.assertIn("prompt prior only", item["suggested_use"])
        self.assertIn("explicit invalidation", item["hypothesis_text"])
        payload = load_externalized_json(item["payload_json"], item["payload_artifact_path"], item["payload_sha256"])
        self.assertTrue(payload["hard_constraints"]["prompt_prior_only"])
        self.assertTrue(payload["hard_constraints"]["candidate_hypothesis_cannot_control_position"])
        self.assertAlmostEqual(payload["hard_constraints"]["max_total_margin_ratio"], 0.20)
        contract = payload[CONTRACT_KEY]
        self.assertEqual(contract["contract_version"], "next_round_strategy_update_v2")
        self.assertEqual(contract["position_authority"], "analysis_or_watchlist_only")
        self.assertEqual(contract["max_position_impact"], "no_direct_position_impact")
        self.assertIn("pm_action_conditions", contract)
        self.assertEqual(payload["entry_timing_hint"], "wait for price confirmation")
        self.assertEqual(payload["agent_name"], "researcher")
        cursor.execute("SELECT raw_prompt FROM reviewer_llm_notes WHERE config_id='cfg'")
        note = dict(cursor.fetchone())
        self.assertIn("AgentQuant Researcher", note["raw_prompt"])
        self.assertNotIn("AgentQuant Reviewer acting as a research memory curator", note["raw_prompt"])
        self.assertEqual(payload["invalidation_condition"], "breakout fails before close")
        conn.close()


class AdaptivePolicyAuditorTest(unittest.TestCase):
    def test_auditor_applies_reviewer_adaptive_cap(self):
        auditor = TradeAuditor(
            {
                "trade_auditor": {
                    "enabled": True,
                    "policy_version": "test",
                    "quality_gate": {"enabled": False},
                    "attribution_feedback": {"enabled": False},
                },
                "market_confirmation": {"enabled": False},
                "learning": {"adaptive_policy": {"min_policy_confidence": 0.30}},
            }
        )
        output = auditor.plan(
            TradeAuditorInput(
                ticker="BU",
                raw_position_ratio=0.10,
                current_position_ratio=0.0,
                market_confirmation={"confirmation_score": 0.8},
                adaptive_policy_state=[
                    {
                        "policy_action": "cap",
                        "multiplier": 0.5,
                        "confidence_score": 0.6,
                        "reason": "weak mature template",
                    }
                ],
                full_config={"learning": {"adaptive_policy": {"min_policy_confidence": 0.30}}},
            )
        )

        self.assertEqual(output.decision, "scale_down")
        self.assertAlmostEqual(output.position_ratio_multiplier, 0.5)
        self.assertIn("adaptive_policy_cap", output.reasons)

    def test_learning_overlay_cannot_raise_portfolio_hard_margin_cap(self):
        self.assertAlmostEqual(
            get_hard_allocation_margin_ratio(
                {
                    "max_total_margin_ratio": 0.20,
                    "capital_utilization_control": {"max_margin_ratio_after_scaling": 0.35},
                }
            ),
            0.20,
        )
        self.assertAlmostEqual(
            get_hard_allocation_margin_ratio(
                {
                    "max_total_margin_ratio": 0.20,
                    "capital_utilization_control": {"max_margin_ratio_after_scaling": 0.12},
                }
            ),
            0.12,
        )
        self.assertAlmostEqual(
            get_hard_allocation_margin_ratio(
                {
                    "max_total_margin_ratio": 0.20,
                    "capital_utilization_control": {"max_margin_ratio_after_scaling": "bad"},
                }
            ),
            0.20,
        )

    def test_capital_utilization_scales_protected_memory_template(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.02,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.50},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.16,
                    "target_margin_ratio_max": 0.20,
                    "target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "memory_protected_min_confirmation_score": 0.45,
                    "disable_scaling_when_weak_combo": True,
                    "protected_min_sample_count_for_scaling": 4,
                },
                "trade_frequency_control": {
                    "weak_signal_combos": [["Bullish", "Bullish", "Neutral"]],
                },
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "combo": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 4,
                    "win_rate": 0.75,
                    "net_pnl": 3200.0,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        self.assertGreater(ratio * 0.10, 0.08)
        self.assertLess(ratio * 0.10, 0.18)
        self.assertIn("capital_utilization_guard", reasons)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertIn("capital_utilization_learning", diagnostics)

    def test_repeatedly_validated_alpha_gets_more_budget_without_fixed_single_name_cap(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.03,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.45,
                    "dynamic_concentration_enabled": True,
                    "other_opportunity_reserve_fraction_of_tradable_capital": 0.10,
                    "validated_min_fraction_of_remaining_capacity": 0.35,
                    "validated_max_fraction_of_remaining_capacity": 0.90,
                    "confirmation_allocation_power": 1.25,
                    "allow_memory_protected_scaling": True,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 5,
                    "win_rate": 0.8,
                    "net_pnl": 6000,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        # This is not a fixed single-name cap: budget is a function of remaining
        # capacity and evidence strength, while reserving capacity for others.
        self.assertGreater(ratio * 0.10, 0.08)
        self.assertLess(ratio * 0.10, 0.17)
        self.assertGreater(ratio, 0.12)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "alpha_release_boost")
        self.assertEqual(diagnostics["capital_utilization_target"]["alpha_release_tier"], "boost")
        self.assertTrue(diagnostics["capital_utilization_target"]["high_quality_memory"])
        self.assertTrue(diagnostics["capital_utilization_target"]["base_position_anchor_lifted"])
        self.assertEqual(diagnostics["capital_utilization_target"]["dynamic_allocation_tier"], "validated_with_stop")
        budget_diagnostics = diagnostics["capital_utilization_target"]["dynamic_budget_diagnostics"]
        self.assertEqual(budget_diagnostics["reserved_for_other_opportunities"], 0.0)
        self.assertGreater(budget_diagnostics["usable_after_reserve"], 0.0)

    def test_wildcard_protected_memory_cannot_trigger_strong_scaling(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="TA",
            trading_date="2025-02-13",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.037,
            margin_rate=0.07,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.64},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                    "protected_min_win_rate_for_scaling": 0.60,
                    "protected_min_net_pnl_for_scaling": 1000,
                    "require_specific_signal_combo_for_strong_scaling": True,
                    "require_stop_protection_for_strong_scaling": True,
                }
            },
            signal_combo=("Bullish", "Neutral", "Bullish"),
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "*",
                    "sample_count": 5,
                    "win_rate": 0.8,
                    "net_pnl": 2174.0,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=5100.0, atr_stop_distance=None),
            ],
        )

        self.assertLess(ratio * 0.07, 0.08)
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")
        rejected = diagnostics.get("capital_utilization_learning", {}).get("protected_evidence_rejected", {})
        self.assertFalse(rejected.get("specific_signal_combo"))

    def test_missing_stop_protection_cannot_trigger_strong_scaling(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="TA",
            trading_date="2025-02-13",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.037,
            margin_rate=0.07,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.70},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                    "protected_min_win_rate_for_scaling": 0.60,
                    "protected_min_net_pnl_for_scaling": 1000,
                    "require_specific_signal_combo_for_strong_scaling": True,
                    "require_stop_protection_for_strong_scaling": True,
                }
            },
            signal_combo=("Bullish", "Neutral", "Bullish"),
            strategy_memory={
                "combo": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Neutral|Bullish",
                    "sample_count": 8,
                    "win_rate": 0.75,
                    "net_pnl": 8000.0,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[],
        )

        self.assertLess(ratio * 0.07, 0.08)
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")
        rejected = diagnostics.get("capital_utilization_learning", {}).get("protected_evidence_rejected", {})
        self.assertEqual(rejected.get("reason"), "missing_pretrade_invalidation")
        self.assertEqual(rejected.get("alpha_release_tier"), "probe")

    def test_protected_memory_does_not_scale_when_current_combo_is_weak(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="M",
            trading_date="2025-02-20",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.00,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "block_scaling_on_conflicting_weak_memory": True,
                    "protected_min_sample_count_for_scaling": 5,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "combo": {
                    "memory_state": "watchlist",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 3,
                    "win_rate": 0.0,
                    "net_pnl": -109208,
                },
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "Neutral|Bullish|Neutral",
                    "sample_count": 6,
                    "win_rate": 0.83,
                    "net_pnl": 12000,
                },
            },
            adaptive_policy_state=[],
        )

        self.assertAlmostEqual(ratio, 0.03)
        self.assertEqual(reasons, [])
        self.assertEqual(diagnostics["capital_utilization_skip"], "conflicting_weak_memory")
        self.assertIn("conflicting_weak_memory", diagnostics["capital_utilization_learning"])

    def test_protected_memory_requires_sufficient_samples_before_strong_scaling(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-20",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.03,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                }
            },
            signal_combo=("Bullish", "Neutral", "Neutral"),
            strategy_memory={
                "combo": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Neutral|Neutral",
                    "sample_count": 3,
                    "win_rate": 1.0,
                    "net_pnl": 8000,
                }
            },
            adaptive_policy_state=[],
        )

        self.assertLess(ratio * 0.10, 0.08)
        self.assertIn("capital_utilization_guard", reasons)
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")
        self.assertIn(
            "protected_evidence_rejected",
            diagnostics.get("capital_utilization_learning", {}),
        )

    def test_adaptive_protect_requires_sufficient_samples_before_strong_scaling(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-20",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.03,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                    "protected_min_win_rate_for_scaling": 0.60,
                    "protected_min_net_pnl_for_scaling": 1000,
                }
            },
            signal_combo=("Bullish", "Neutral", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[
                {
                    "policy_action": "protect",
                    "sample_count": 3,
                    "win_rate": 1.0,
                    "net_pnl": 8000,
                    "confidence_score": 0.9,
                }
            ],
        )

        self.assertLess(ratio * 0.10, 0.08)
        self.assertNotIn("capital_utilization_memory_protected", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")
        self.assertIn(
            "protected_evidence_rejected",
            diagnostics.get("capital_utilization_learning", {}),
        )

    def test_stop_protected_validated_alpha_gets_more_budget_but_not_all_in(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.00,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.82},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.45,
                    "dynamic_concentration_enabled": True,
                    "other_opportunity_reserve_fraction_of_tradable_capital": 0.10,
                    "validated_min_fraction_of_remaining_capacity": 0.35,
                    "validated_max_fraction_of_remaining_capacity": 0.90,
                    "confirmation_allocation_power": 1.25,
                    "stop_protection_allocation_bonus": 0.15,
                    "allow_memory_protected_scaling": True,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 5,
                    "win_rate": 0.8,
                    "net_pnl": 6000,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        self.assertGreater(ratio * 0.10, 0.16)
        self.assertLess(ratio * 0.10, 0.20)
        self.assertIn("capital_utilization_memory_protected", reasons)
        self.assertTrue(diagnostics["capital_utilization_target"]["base_position_anchor_lifted"])
        self.assertEqual(diagnostics["capital_utilization_target"]["dynamic_allocation_tier"], "validated_with_stop")
        self.assertTrue(diagnostics["capital_utilization_target"]["stop_protected"])
        self.assertGreater(
            diagnostics["capital_utilization_target"]["dynamic_budget_diagnostics"]["reserved_for_other_opportunities"],
            0.0,
        )

    def test_exceptional_validated_alpha_can_take_most_capacity_but_reserves_some(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-20",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.00,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.92},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "strong_opportunity_target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "memory_protected_min_confirmation_score": 0.60,
                    "dynamic_concentration_enabled": True,
                    "other_opportunity_reserve_fraction_of_tradable_capital": 0.15,
                    "validated_min_fraction_of_remaining_capacity": 0.25,
                    "validated_max_fraction_of_remaining_capacity": 0.65,
                    "confirmation_allocation_power": 1.75,
                    "stop_protection_allocation_bonus": 0.15,
                    "allow_memory_protected_scaling": True,
                    "protected_min_sample_count_for_scaling": 5,
                    "protected_min_win_rate_for_scaling": 0.60,
                    "protected_min_net_pnl_for_scaling": 1000,
                    "exceptional_validated_enabled": True,
                    "exceptional_validated_requires_stop_protection": True,
                    "exceptional_validated_min_confirmation_score": 0.85,
                    "exceptional_validated_min_sample_count": 8,
                    "exceptional_validated_min_win_rate": 0.70,
                    "exceptional_validated_min_net_pnl": 5000,
                    "exceptional_other_opportunity_reserve_fraction_of_tradable_capital": 0.05,
                    "exceptional_validated_min_fraction_of_remaining_capacity": 0.75,
                    "exceptional_validated_max_fraction_of_remaining_capacity": 0.95,
                    "exceptional_confirmation_allocation_power": 1.00,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "side_memory": {
                    "memory_state": "protected",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 9,
                    "win_rate": 0.78,
                    "net_pnl": 12000,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        margin_ratio = ratio * 0.10
        self.assertGreater(margin_ratio, 0.17)
        self.assertLessEqual(margin_ratio, 0.20)
        self.assertIn("capital_utilization_memory_protected", reasons)
        target = diagnostics["capital_utilization_target"]
        self.assertEqual(target["dynamic_allocation_tier"], "exceptional_validated_with_stop")
        self.assertTrue(target["dynamic_budget_diagnostics"]["exceptional_validated"])
        self.assertGreaterEqual(
            target["dynamic_budget_diagnostics"]["reserved_for_other_opportunities"],
            0.01,
        )

    def test_alpha_release_can_use_configured_net_exposure_weak_param(self):
        max_net, symmetric, mode = _resolve_net_exposure_control(
            {
                "net_exposure_control": {
                    "max_net_exposure": 0.50,
                    "strong_opportunity_max_net_exposure": 2.00,
                    "symmetric_scaling": True,
                }
            },
            {
                "capital_utilization_target": {
                    "target_mode": "alpha_release_boost",
                    "high_quality_memory": True,
                }
            },
        )

        self.assertEqual(max_net, 2.00)
        self.assertTrue(symmetric)
        self.assertEqual(mode, "alpha_release")

    def test_unproven_signal_keeps_base_net_exposure_weak_param(self):
        max_net, symmetric, mode = _resolve_net_exposure_control(
            {
                "net_exposure_control": {
                    "max_net_exposure": 0.50,
                    "strong_opportunity_max_net_exposure": 2.00,
                    "symmetric_scaling": True,
                }
            },
            {
                "capital_utilization_target": {
                    "target_mode": "confirmed_observation",
                    "high_quality_memory": False,
                }
            },
        )

        self.assertEqual(max_net, 0.50)
        self.assertTrue(symmetric)
        self.assertEqual(mode, "base")

    def test_capital_utilization_uses_observation_band_for_unproven_confirmed_signal(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            position_ratio=0.03,
            current_ratio=0.0,
            current_margin_ratio=0.03,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.72},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.06,
                    "target_margin_ratio_max": 0.08,
                    "target_margin_ratio_confirmed": 0.07,
                    "strong_opportunity_target_margin_ratio_min": 0.16,
                    "strong_opportunity_target_margin_ratio_max": 0.20,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[],
        )

        self.assertLess(ratio * 0.10, 0.04)
        self.assertGreater(ratio * 0.10, 0.0)
        self.assertIn("capital_utilization_guard", reasons)
        self.assertEqual(diagnostics["capital_utilization_target"]["target_mode"], "confirmed_observation")

    def test_capital_utilization_adds_to_matched_high_quality_same_side_position(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-11",
            position_ratio=0.03,
            current_ratio=0.03,
            current_margin_ratio=0.04,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.50},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.16,
                    "target_margin_ratio_max": 0.20,
                    "target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "min_confirmation_score_for_scaling": 0.60,
                    "allow_memory_protected_scaling": True,
                    "memory_protected_min_confirmation_score": 0.45,
                    "allow_confirmed_same_side_add_on": True,
                    "protected_min_sample_count_for_scaling": 4,
                }
            },
            signal_combo=("Bullish", "Bullish", "Neutral"),
            strategy_memory={
                "combo": {
                    "memory_state": "deployable",
                    "signal_combo": "Bullish|Bullish|Neutral",
                    "sample_count": 4,
                    "win_rate": 0.75,
                    "net_pnl": 3200.0,
                }
            },
            adaptive_policy_state=[],
            analyst_signals=[
                SimpleNamespace(invalidation_level=3200.0, atr_stop_distance=None),
            ],
        )

        self.assertGreater(ratio * 0.10, 0.08)
        self.assertLess(ratio * 0.10, 0.17)
        self.assertIn("capital_utilization_same_side_add_on", reasons)
        self.assertIn("capital_utilization_same_side_add_on", diagnostics)

    def test_capital_utilization_does_not_expand_matched_unproven_position(self):
        ratio, reasons, notes, diagnostics = _apply_capital_utilization_control(
            db=None,
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-11",
            position_ratio=0.03,
            current_ratio=0.03,
            current_margin_ratio=0.04,
            margin_rate=0.10,
            max_position_ratio=0.12,
            market_confirmation={"confirmation_score": 0.80},
            full_config={
                "capital_utilization_control": {
                    "enabled": True,
                    "target_margin_ratio_min": 0.16,
                    "target_margin_ratio_max": 0.20,
                    "target_margin_ratio_confirmed": 0.18,
                    "max_margin_ratio_after_scaling": 0.20,
                    "allow_memory_protected_scaling": True,
                    "allow_confirmed_same_side_add_on": True,
                }
            },
            signal_combo=("Bullish", "Neutral", "Neutral"),
            strategy_memory={},
            adaptive_policy_state=[],
        )

        self.assertAlmostEqual(ratio, 0.03)
        self.assertEqual(reasons, [])
        self.assertNotIn("capital_utilization_same_side_add_on", diagnostics)


class StrictCompletionRegressionTest(unittest.TestCase):
    def test_technical_tradeability_is_evidence_driven_not_product_watchlist_driven(self):
        signal_results = {
            "trend": Signal.BULLISH,
            "macd": Signal.BULLISH,
            "adx": Signal.BULLISH,
            "settlement_trend": Signal.BULLISH,
        }
        features = {"trend_strength": 24.0, "volatility": 0.12, "volume_ratio": 1.05}

        former_watchlist_context = build_technical_context("MA", signal_results, features)
        control_context = build_technical_context("SR", signal_results, features)

        self.assertEqual(former_watchlist_context["tradeability"], control_context["tradeability"])
        self.assertEqual(former_watchlist_context["dominant_direction"], "bullish")
        self.assertNotIn("long_watchlist_requires_stronger_trend", former_watchlist_context["risk_flags"])
        self.assertNotIn("watchlist_long_weak_trend", former_watchlist_context["risk_flags"])
        self.assertNotIn("high_caution_ticker", former_watchlist_context["risk_flags"])

    def test_technical_tradeability_tightens_on_evidence_quality(self):
        signal_results = {
            "trend": Signal.BULLISH,
            "macd": Signal.BULLISH,
            "adx": Signal.BULLISH,
            "settlement_trend": Signal.BULLISH,
        }

        context = build_technical_context(
            "SR",
            signal_results,
            {"trend_strength": 21.0, "volatility": 0.36, "volume_ratio": 1.0},
        )

        self.assertEqual(context["tradeability"], "medium")
        self.assertIn("high_volatility", context["risk_flags"])
        self.assertIn("high_volatility_requires_extra_alignment", context["risk_flags"])

    def test_analyst_signal_has_explicit_context_fields(self):
        signal = AnalystSignal(
            signal=Signal.BULLISH,
            horizon_class="short",
            expected_horizon_days=2,
            market_regime="trending",
            trend_stage="early_trend",
            price_percentile=0.25,
            trigger_type="reversal_confirmed",
            entry_type="initial",
            invalidation_level=3200.0,
        )

        self.assertEqual(signal.horizon_class, "short")
        self.assertEqual(signal.expected_horizon_days, 2)
        self.assertEqual(signal.trigger_type, "reversal_confirmed")

    def test_analyst_applicability_profile_changes_weights(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7, horizon_class="short", market_regime="trending"),
            AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.7, horizon_class="medium", market_regime="trending"),
            AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.3, horizon_class="event_short", market_regime="event_driven"),
        ]
        context = _quality_aware_fusion_context(
            ticker="BU",
            analyst_signals=signals,
            dynamic_weights={"technical": 1 / 3, "fundamental": 1 / 3, "commodity_news": 1 / 3},
            full_config={
                "analyst_applicability_profile": {
                    "enabled": True,
                    "technical": {"horizon_multipliers": {"short": 1.5}},
                    "fundamental": {"horizon_multipliers": {"medium": 1.2}},
                    "commodity_news": {"horizon_multipliers": {"event_short": 0.8}},
                }
            },
        )

        self.assertIn("technical", context["analyst_applicability_profile"])
        self.assertGreater(context["quality_adjusted_weights"]["technical"], 0.0)

    def test_analyst_applicability_profile_preserves_horizon_sector_regime_dimensions(self):
        signals = [
            AnalystSignal(agent_name="technical", signal=Signal.BULLISH, confidence=0.7, horizon_class="short", market_regime="trending"),
            AnalystSignal(agent_name="fundamental", signal=Signal.BULLISH, confidence=0.7, horizon_class="medium", market_regime="ranging"),
            AnalystSignal(agent_name="commodity_news", signal=Signal.NEUTRAL, confidence=0.4, horizon_class="event_short", market_regime="event_driven", expected_horizon_days=1),
        ]
        context = _quality_aware_fusion_context(
            ticker="RB",
            analyst_signals=signals,
            dynamic_weights={"technical": 1 / 3, "fundamental": 1 / 3, "commodity_news": 1 / 3},
            full_config={
                "analyst_applicability_profile": {
                    "enabled": True,
                    "technical": {
                        "horizon_multipliers": {"short": 1.2},
                        "sector_multipliers": {"ferrous": 1.1},
                        "market_regime_multipliers": {"trending": 1.2},
                    },
                    "fundamental": {
                        "horizon_multipliers": {"medium": 1.2},
                        "sector_multipliers": {"ferrous": 1.15},
                        "market_regime_multipliers": {"ranging": 1.1},
                    },
                    "commodity_news": {
                        "event_window_days": 3,
                        "outside_event_window_multiplier": 0.5,
                        "horizon_multipliers": {"event_short": 1.25},
                        "sector_multipliers": {"ferrous": 0.95},
                        "market_regime_multipliers": {"event_driven": 1.2},
                    },
                }
            },
        )

        adjustments = context["analyst_applicability_profile"]
        self.assertEqual(context["sector"], "ferrous")
        self.assertEqual(adjustments["technical"]["horizon_class"], "short")
        self.assertEqual(adjustments["technical"]["sector"], "ferrous")
        self.assertEqual(adjustments["technical"]["market_regime"], "trending")
        self.assertEqual(adjustments["fundamental"]["horizon_class"], "medium")
        self.assertEqual(adjustments["commodity_news"]["horizon_class"], "event_short")
        self.assertGreater(adjustments["fundamental"]["multiplier"], 1.0)

    def test_neutral_signal_is_allowed_but_accountable(self):
        signal = AnalystSignal(
            agent_name="technical",
            signal=Signal.NEUTRAL,
            confidence=0.42,
            horizon_class="short",
            market_regime="range",
        )
        enriched = apply_business_quality_enrichment(
            signal,
            quality_context={
                "tradeability": "medium",
                "market_regime": "range",
                "risk_flags": ["conflicting_indicators"],
                "features": {"trend_strength": 0.2, "volume_ratio": 0.9},
            },
            full_config={"llm": {"provider": "test"}, "analyst_llm": {"cloud_model": "mock-model"}},
            analyst="technical",
        )

        self.assertEqual(enriched.signal, Signal.NEUTRAL)
        self.assertTrue(enriched.neutral_reason)
        self.assertTrue(enriched.missing_evidence)
        self.assertTrue(enriched.would_change_view_if)
        self.assertLessEqual(enriched.business_quality_score, 0.56)
        self.assertIn("business_quality", enriched.metadata)
        self.assertIn("neutral_opportunity_contract", enriched.metadata)
        self.assertEqual(enriched.neutral_opportunity_bucket, "conflict_avoidance")
        self.assertEqual(enriched.neutral_watchlist_priority, "low")

    def test_neutral_conditional_watchlist_is_structured_not_trade_permission(self):
        snapshot = {
            "technical": {
                "signal": "Neutral",
                "neutral_reason": "waiting for breakout confirmation",
                "missing_evidence": [],
                "conflicting_factors": [],
                "would_change_view_if": "price breaks above 3200 with volume",
                "neutral_opportunity_bucket": "watchlist_trigger",
                "neutral_trigger_condition": "price breaks above 3200 with volume",
                "neutral_shadow_side": "long",
                "neutral_watchlist_priority": "medium",
                "metadata": {
                    "neutral_opportunity_contract": {
                        "bucket": "watchlist_trigger",
                        "trigger_condition": "price breaks above 3200 with volume",
                        "shadow_side": "long",
                        "watchlist_priority": "medium",
                        "tracking_only": True,
                        "trade_permission": "none_without_current_confirmation",
                    }
                },
            },
            "fundamental": {"signal": "Neutral", "confidence": 0.5},
            "commodity_news": {"signal": "Neutral", "confidence": 0.5},
        }

        item = classify_neutral_signal(
            analyst="technical",
            payload=snapshot["technical"],
            snapshot=snapshot,
            cfg={},
        )
        summary = build_neutral_accountability_summary(
            [{"id": "rec-1", "underlying_code": "BU", "signal_snapshot": snapshot}],
            {},
        )

        self.assertEqual(item["category"], "conditional_watchlist")
        contract = item["neutral_opportunity_contract"]
        self.assertTrue(contract["tracking_only"])
        self.assertEqual(contract["trade_permission"], "none_without_current_confirmation")
        self.assertEqual(summary["category_counts"]["conditional_watchlist"], 1)
        self.assertEqual(summary["by_analyst"]["technical"]["conditional_watchlist_count"], 1)

    def test_stale_fundamental_direction_is_forced_to_accountable_neutral(self):
        signal = AnalystSignal(
            agent_name="fundamental",
            signal=Signal.BULLISH,
            confidence=0.82,
            justification="inventory and demand look supportive",
            horizon_class="medium",
        )

        gated = apply_signal_quality_gate(
            signal,
            quality_context={
                "tradeability": "medium",
                "risk_flags": ["stale_fundamental_inputs"],
                "data_quality": {
                    "stale_ratio": 0.42,
                    "factor_freshness_score": 0.30,
                },
            },
            full_config={
                "analyst_llm": {
                    "force_neutral_stale_fundamental": True,
                    "cap_stale_fundamental_confidence": 0.30,
                }
            },
            analyst="fundamental",
        )

        self.assertEqual(gated.signal, Signal.NEUTRAL)
        self.assertLessEqual(gated.confidence, 0.30)
        self.assertIn("fresh supply-demand anchor", gated.missing_evidence)
        self.assertIn("stale_fundamental_direction_block", gated.metadata["risk_flags"])
        self.assertTrue(gated.metadata["quality_gate"]["stale_fundamental_direction_block"])

    def test_neutral_accountability_distinguishes_risk_avoidance_from_evidence_gap(self):
        snapshot = {
            "technical": {
                "signal": "Neutral",
                "neutral_reason": "conflicting indicators and low reward/risk",
                "missing_evidence": ["volume/open-interest confirmation"],
                "conflicting_factors": ["conflicting_indicators"],
                "would_change_view_if": "breakout confirms with volume",
                "reward_risk_ratio": 0.8,
                "metadata": {"risk_flags": ["conflicting_indicators"], "tradeability": "low"},
            },
            "fundamental": {
                "signal": "Neutral",
                "neutral_reason": "insufficient evidence from stale supply-demand data",
                "missing_evidence": ["fresh supply-demand anchor"],
                "conflicting_factors": [],
                "would_change_view_if": "fresh inventory and basis data align",
                "data_coverage_score": 0.20,
                "metadata": {"data_quality": {"coverage_ratio": 0.20, "stale_ratio": 0.50}},
            },
            "commodity_news": {
                "signal": "Bullish",
                "confidence": 0.70,
                "metadata": {"business_quality": {"score": 0.70}},
            },
        }

        risk_item = classify_neutral_signal(
            analyst="technical",
            payload=snapshot["technical"],
            snapshot=snapshot,
            cfg={},
        )
        gap_item = classify_neutral_signal(
            analyst="fundamental",
            payload=snapshot["fundamental"],
            snapshot=snapshot,
            cfg={},
        )
        summary = build_neutral_accountability_summary(
            [{"id": "rec-1", "underlying_code": "BU", "signal_snapshot": snapshot}],
            {},
        )

        self.assertEqual(risk_item["category"], "reasonable_avoidance")
        self.assertEqual(gap_item["category"], "evidence_gap_conservative")
        self.assertEqual(summary["category_counts"]["reasonable_avoidance"], 1)
        self.assertEqual(summary["category_counts"]["evidence_gap_conservative"], 1)
        self.assertAlmostEqual(summary["accountability_complete_rate"], 1.0)

    def test_snapshot_contract_adds_required_audit_header(self):
        snapshot = attach_snapshot_contract(
            {
                "technical": {"signal": "Bullish"},
                "pre_open_plan": {"target_position_ratio": 0.05},
            },
            trading_date="2025-02-10",
            ticker="BU",
            config_id="cfg",
            source_artifacts=["technical:BU:2025-02-10"],
        )

        self.assertIn("artifact_contract", snapshot)
        self.assertEqual(validate_artifact_header(snapshot["artifact_contract"]), [])

    def test_reviewer_horizon_prefers_decision_scope_over_short_technical(self):
        snapshot = {
            "horizon_scope": {
                "decision_horizon": "medium",
                "analyst_horizons": {
                    "technical": {"analyst_horizon": "short"},
                    "fundamental": {"analyst_horizon": "medium"},
                    "commodity_news": {"analyst_horizon": "event_short"},
                },
            },
            "pre_open_plan": {"expected_horizon_days": 2},
        }

        self.assertEqual(_horizon_class(2, snapshot), "medium")

    def test_template_prior_loads_into_strategy_memory_at_backtest_start(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            prior_path = tmp_path / "template_prior.json"
            prior_path.write_text(
                json.dumps(
                    {
                        "templates": [
                            {
                                "ticker": "I",
                                "side": "long",
                                "signal_template": "long_breakout_continuation_medium",
                                "horizon_class": "medium",
                                "prior_state": "protected",
                                "sample_count": 4,
                                "win_rate": 0.75,
                                "net_pnl": 12000.0,
                                "avg_pnl": 3000.0,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            db_path = tmp_path / "agentquant.db"
            db = _PriorBootstrapDB(db_path)

            count = load_template_prior_if_enabled(
                {
                    "market_type": "china_futures",
                    "trading_date": "2025-01-02",
                    "learning": {
                        "memory_expires_after_days": 30,
                        "template_prior": {
                            "enabled": True,
                            "load_on_backtest_start": True,
                            "path": str(prior_path),
                        },
                    },
                },
                db,
                "cfg",
            )
            second_count = load_template_prior_if_enabled(
                {
                    "market_type": "china_futures",
                    "trading_date": "2025-01-03",
                    "learning": {
                        "memory_expires_after_days": 30,
                        "template_prior": {
                            "enabled": True,
                            "load_on_backtest_start": True,
                            "path": str(prior_path),
                        },
                    },
                },
                db,
                "cfg",
            )

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT * FROM strategy_memory WHERE config_id = ?", ("cfg",)).fetchone()
            finally:
                conn.close()

        self.assertEqual(count, 1)
        self.assertEqual(second_count, 0)
        self.assertEqual(row["ticker"], "I")
        self.assertEqual(row["side"], "long")
        self.assertEqual(row["memory_state"], "protected")
        self.assertEqual(row["source"], "template_prior")

    def test_template_prior_refreshes_when_source_marker_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            prior_path = tmp_path / "template_prior.json"
            db_path = tmp_path / "agentquant.db"
            db = _PriorBootstrapDB(db_path)
            cfg = {
                "market_type": "china_futures",
                "trading_date": "2025-01-26",
                "strategy_memory": {"weak_block_total_pnl_below": -2500},
                "learning": {
                    "memory_expires_after_days": 30,
                    "template_prior": {
                        "enabled": True,
                        "load_on_backtest_start": True,
                        "path": str(prior_path),
                    },
                },
            }
            prior_path.write_text(
                json.dumps(
                    {
                        "exported_at_trading_date": "2025-01-24",
                        "templates": [
                            {
                                "ticker": "P",
                                "side": "long",
                                "prior_state": "recovering",
                                "sample_count": 4,
                                "win_rate": 0.25,
                                "net_pnl": -7400,
                                "avg_pnl": -1850,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            first_count = load_template_prior_if_enabled(cfg, db, "cfg")
            same_count = load_template_prior_if_enabled(cfg, db, "cfg")
            prior_path.write_text(
                json.dumps(
                    {
                        "exported_at_trading_date": "2025-01-25",
                        "templates": [
                            {
                                "ticker": "BU",
                                "side": "long",
                                "prior_state": "protected",
                                "sample_count": 4,
                                "win_rate": 1.0,
                                "net_pnl": 9650,
                                "avg_pnl": 2412,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            refreshed_count = load_template_prior_if_enabled(cfg, db, "cfg")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT ticker, side, memory_state, payload_json FROM strategy_memory WHERE config_id = ?",
                    ("cfg",),
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(first_count, 1)
        self.assertEqual(same_count, 0)
        self.assertEqual(refreshed_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "BU")
        self.assertEqual(rows[0]["memory_state"], "protected")
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["source_exported_at_trading_date"], "2025-01-25")

    def test_template_prior_reclassifies_with_strategy_memory_thresholds(self):
        item = {
            "ticker": "MA",
            "side": "long",
            "prior_state": "recovering",
            "sample_count": 5,
            "win_rate": 0.40,
            "net_pnl": -8800,
        }

        state = classify_template_prior_item(
            item,
            {
                "strategy_memory": {
                    "min_samples_weak_block": 4,
                    "weak_block_win_rate_below": 0.30,
                    "weak_block_total_pnl_below": -2500,
                }
            },
        )

        self.assertEqual(state, "weak_block")

    def test_template_prior_loader_uses_project_root_for_relative_path(self):
        path = _project_path("src/logs/attribution/template_prior.json")

        self.assertEqual(path, SRC_ROOT / "logs" / "attribution" / "template_prior.json")
        self.assertNotIn("src\\src", str(path))
        self.assertNotIn("src/src", str(path))

    def test_template_prior_export_uses_project_relative_path_without_src_duplication(self):
        temp_parent = SRC_ROOT / "logs"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as tmpdir:
            tmp_path = Path(tmpdir)
            project_root = SRC_ROOT.parent
            export_file = tmp_path / "template_prior.json"
            relative_export_path = export_file.relative_to(project_root)
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO config(id) VALUES ('cfg')")
            _ensure_reviewer_learning_schema(cursor)
            cursor.execute(
                """
                INSERT INTO signal_template_performance (
                    id, config_id, ticker, side, signal_template, horizon_class,
                    market_regime, sample_count, win_rate, net_pnl, avg_pnl,
                    profit_factor, confidence_score, last_updated, payload_json
                ) VALUES (
                    'perf', 'cfg', 'BU', 'long', 'long_breakout_short', 'short',
                    'trend', 3, 0.67, 3000, 1000, 1.5, 0.7, 'now', '{}'
                )
                """
            )

            prior_path = _export_template_prior(
                cursor,
                cfg={
                    "learning": {
                        "template_prior": {
                            "enabled": True,
                            "export_on_backtest_end": True,
                            "path": str(relative_export_path),
                        }
                    }
                },
                config_id="cfg",
                trading_date="2025-03-20",
            )
            conn.close()

            self.assertTrue(prior_path)
            self.assertNotIn("src\\src", prior_path)
            self.assertNotIn("src/src", prior_path)
            self.assertTrue(Path(prior_path).exists())


class ReviewerLearningPersistenceRegressionTest(unittest.TestCase):
    def _connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_reviewer_learning_schema(conn.cursor())
        return conn

    def _create_trade_tables(self, cursor):
        cursor.execute(
            """
            CREATE TABLE futures_transactions (
                id TEXT,
                config_id TEXT,
                recommendation_id TEXT,
                trading_date TEXT,
                created_at TEXT,
                ticker TEXT,
                contract_code TEXT,
                action TEXT,
                lots INTEGER,
                execution_price REAL,
                price REAL,
                contract_multiplier REAL,
                commission REAL,
                source_type TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE futures_recommendation (
                id TEXT,
                config_id TEXT,
                signal_snapshot TEXT
            )
            """
        )

    def _signal_snapshot(self, *, learned: bool = False, learning_reason: str = "adaptive_policy_protect"):
        plan = {
            "analyst_signal_combo": ["Bullish", "Bullish", "Neutral"],
            "decision_horizon": "short",
            "market_regime": "trend",
            "target_position_ratio": 0.08,
        }
        if learned:
            plan["trade_auditor"] = {
                "decision": "allow",
                "reasons": [learning_reason],
                "diagnostics": {"adaptive_policy_applied": [{"policy_type": "causal_review_rule"}]},
            }
        return {
            "technical": {
                "signal": "Bullish",
                "template_name": "reversal_confirmed",
                "horizon_class": "short",
            },
            "fundamental": {"signal": "Bullish"},
            "commodity_news": {"signal": "Neutral"},
            "pre_open_plan": plan,
        }

    def test_notes_only_causal_candidate_becomes_validated_policy_after_samples_mature(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            cursor.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?)",
                [
                    ("r1", "cfg", json.dumps(self._signal_snapshot())),
                    ("r2", "cfg", json.dumps(self._signal_snapshot())),
                ],
            )
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("o1", "cfg", "r1", "2025-02-03", "2025-02-03T09:00:00", "BU", "bu2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                    ("c1", "cfg", "c1", "2025-02-04", "2025-02-04T14:55:00", "BU", "bu2505", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                    ("o2", "cfg", "r2", "2025-02-05", "2025-02-05T09:00:00", "BU", "bu2505", "open_long", 1, 110.0, 110.0, 10.0, 1.0, "strategy"),
                    ("c2", "cfg", "c2", "2025-02-06", "2025-02-06T14:55:00", "BU", "bu2505", "close_long", 1, 130.0, 130.0, 10.0, 1.0, "strategy"),
                ],
            )
            cursor.execute(
                """
                INSERT INTO reviewer_llm_notes (
                    id, config_id, trading_date, evidence_pack_id, ticker,
                    raw_prompt, raw_response, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "note-1",
                    "cfg",
                    "2025-02-06",
                    "pack-1",
                    "*",
                    "",
                    "",
                    "now",
                    json.dumps(
                        {
                            "pre_trade_evidence": [
                                {"ticker": "BU", "action": "open_long", "signal_snapshot": self._signal_snapshot()}
                            ]
                        }
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO causal_review_candidate (
                    id, config_id, trading_date, evidence_pack_id, ticker, side,
                    candidate_type, confidence_score, rule_validation_status,
                    created_at, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cand-1",
                    "cfg",
                    "2025-02-06",
                    "pack-1",
                    "*",
                    "*",
                    "post_trade_causal_review",
                    0.80,
                    "notes_only_pending_rule_validation",
                    "now",
                    "2025-03-01",
                    json.dumps({"confidence_score": 0.80, "primary_cause": "validated positive template"}),
                ),
            )

            summary = _write_validated_causal_policy_rules(
                cursor,
                cfg={
                    "learning": {
                        "enabled": True,
                        "memory_expires_after_days": 30,
                        "reviewer_causal_review": {
                            "enabled": True,
                            "rule_validation": {"min_samples": 2, "min_candidate_confidence": 0.35},
                        },
                    }
                },
                config_id="cfg",
                trading_date="2025-02-06",
            )

            cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = 'causal_review_rule'
                """,
                ("cfg",),
            )
            policy = dict(cursor.fetchone())
            cursor.execute("SELECT rule_validation_status, payload_json FROM causal_review_candidate WHERE id = ?", ("cand-1",))
            candidate = dict(cursor.fetchone())

            self.assertEqual(summary["validated_rules"], 1)
            self.assertEqual(policy["ticker"], "BU")
            self.assertEqual(policy["side"], "long")
            self.assertEqual(policy["policy_action"], "protect")
            self.assertEqual(policy["sample_count"], 2)
            policy_payload = json.loads(policy["payload_json"])
            self.assertEqual(policy_payload[CONTRACT_KEY]["contract_version"], "next_round_strategy_update_v2")
            self.assertEqual(policy_payload[CONTRACT_KEY]["position_authority"], "pm_auditor_conditioned")
            self.assertIn("may_support_alpha_scaling", policy_payload[CONTRACT_KEY]["max_position_impact"])
            self.assertEqual(candidate["rule_validation_status"], "validated_rule_applied")
            self.assertEqual(json.loads(candidate["payload_json"])["rule_validation"]["applied_rule_count"], 1)
        finally:
            conn.close()

    def test_learned_vs_unlearned_trade_performance_splits_completed_pairs(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            cursor.executemany(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?)",
                [
                    ("learned-r", "cfg", json.dumps(self._signal_snapshot(learned=True))),
                    ("plain-r", "cfg", json.dumps(self._signal_snapshot(learned=False))),
                ],
            )
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("lo", "cfg", "learned-r", "2025-02-03", "2025-02-03T09:00:00", "BU", "bu2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                    ("lc", "cfg", "lc", "2025-02-04", "2025-02-04T14:55:00", "BU", "bu2505", "close_long", 1, 120.0, 120.0, 10.0, 1.0, "strategy"),
                    ("uo", "cfg", "plain-r", "2025-02-05", "2025-02-05T09:00:00", "BU", "bu2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                    ("uc", "cfg", "uc", "2025-02-06", "2025-02-06T14:55:00", "BU", "bu2505", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                ],
            )

            summary = _learned_vs_unlearned_trade_performance(
                cursor,
                config_id="cfg",
                trading_date="2025-02-06",
            )

            self.assertEqual(summary["learned"]["total_trades"], 1)
            self.assertEqual(summary["unlearned"]["total_trades"], 1)
            self.assertGreater(summary["learned"]["net_pnl"], 0)
            self.assertLess(summary["unlearned"]["net_pnl"], 0)
            self.assertEqual(summary["learned_reason_counts"]["adaptive_policy"], 1)
            self.assertEqual(summary["learned_effect_counts"]["alpha_release"], 1)
            self.assertEqual(summary["learned_effect_summary"]["alpha_release"]["total_trades"], 1)
        finally:
            conn.close()

    def test_learned_underperformance_writes_scoped_alpha_demote_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(3):
                rec_id = f"learned-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=True))))
                transactions.extend(
                    [
                        (f"lo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"lc-{idx}", "cfg", f"lc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                    ]
                )
            for idx in range(3):
                rec_id = f"plain-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=False))))
                transactions.extend(
                    [
                        (f"uo-{idx}", "cfg", rec_id, f"2025-02-2{idx}", f"2025-02-2{idx}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"uc-{idx}", "cfg", f"uc-{idx}", f"2025-02-2{idx}", f"2025-02-2{idx}T14:55:00", "ZZ", "zz2505", "close_long", 1, 115.0, 115.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learned_vs_unlearned_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={"learning": {"learned_vs_unlearned_policy": {"enabled": True}}},
            )

            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learned_vs_unlearned"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "ZZ")
            self.assertEqual(row["side"], "long")
            self.assertEqual(row["signal_template"], "long_reversal_confirmed_short")
            self.assertEqual(row["policy_action"], "demote")
            self.assertEqual(result["status"], "scoped_demote_applied")
        finally:
            conn.close()

    def test_learned_underperformance_writes_scoped_risk_suppression_demote_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(3):
                rec_id = f"learned-risk-{idx}"
                recommendations.append(
                    (
                        rec_id,
                        "cfg",
                        json.dumps(
                            self._signal_snapshot(
                                learned=True,
                                learning_reason="strategy_memory_watchlist_cap",
                            )
                        ),
                    )
                )
                transactions.extend(
                    [
                        (f"lro-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"lrc-{idx}", "cfg", f"lrc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                    ]
                )
            for idx in range(3):
                rec_id = f"plain-risk-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=False))))
                transactions.extend(
                    [
                        (f"uro-{idx}", "cfg", rec_id, f"2025-02-2{idx}", f"2025-02-2{idx}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"urc-{idx}", "cfg", f"urc-{idx}", f"2025-02-2{idx}", f"2025-02-2{idx}T14:55:00", "ZZ", "zz2505", "close_long", 1, 115.0, 115.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learned_vs_unlearned_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={"learning": {"learned_vs_unlearned_policy": {"enabled": True}}},
            )

            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learned_vs_unlearned"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "ZZ")
            self.assertEqual(row["policy_action"], "demote")
            self.assertIn("risk_suppression", row["reason"])
            self.assertEqual(result["status"], "scoped_demote_applied")
        finally:
            conn.close()

    def test_global_learned_underperformance_without_scoped_alpha_is_diagnostic_only(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(3):
                rec_id = f"learned-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=True))))
                ticker = f"Z{idx}"
                transactions.extend(
                    [
                        (f"lo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", ticker, "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"lc-{idx}", "cfg", f"lc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", ticker, "zz2505", "close_long", 1, 90.0, 90.0, 10.0, 1.0, "strategy"),
                    ]
                )
            for idx in range(3):
                rec_id = f"plain-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=False))))
                transactions.extend(
                    [
                        (f"uo-{idx}", "cfg", rec_id, f"2025-02-2{idx}", f"2025-02-2{idx}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"uc-{idx}", "cfg", f"uc-{idx}", f"2025-02-2{idx}", f"2025-02-2{idx}T14:55:00", "ZZ", "zz2505", "close_long", 1, 115.0, 115.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learned_vs_unlearned_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={"learning": {"learned_vs_unlearned_policy": {"enabled": True}}},
            )

            self.assertEqual(result["status"], "global_underperformance_diagnostic_only")
            rows = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learned_vs_unlearned"),
            ).fetchall()
            self.assertEqual(rows, [])
        finally:
            conn.close()

    def test_scoped_learned_self_loss_demotes_without_benchmark(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._create_trade_tables(cursor)
            recommendations = []
            transactions = []
            for idx in range(3):
                rec_id = f"learned-self-loss-{idx}"
                recommendations.append((rec_id, "cfg", json.dumps(self._signal_snapshot(learned=True))))
                transactions.extend(
                    [
                        (f"slo-{idx}", "cfg", rec_id, f"2025-02-0{idx + 1}", f"2025-02-0{idx + 1}T09:00:00", "ZZ", "zz2505", "open_long", 1, 100.0, 100.0, 10.0, 1.0, "strategy"),
                        (f"slc-{idx}", "cfg", f"slc-{idx}", f"2025-02-1{idx + 1}", f"2025-02-1{idx + 1}T14:55:00", "ZZ", "zz2505", "close_long", 1, 50.0, 50.0, 10.0, 1.0, "strategy"),
                    ]
                )
            cursor.executemany("INSERT INTO futures_recommendation VALUES (?, ?, ?)", recommendations)
            cursor.executemany(
                """
                INSERT INTO futures_transactions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transactions,
            )

            result = _write_learned_vs_unlearned_policy_state(
                cursor,
                config_id="cfg",
                trading_date="2025-02-28",
                cfg={
                    "learning": {
                        "learned_vs_unlearned_policy": {
                            "enabled": True,
                            "min_scoped_alpha_samples": 3,
                            "allow_self_loss_demote_without_benchmark": True,
                            "min_self_loss_net_pnl": -1000,
                        }
                    }
                },
            )

            row = cursor.execute(
                """
                SELECT *
                FROM adaptive_policy_state
                WHERE config_id = ? AND policy_type = ?
                """,
                ("cfg", "learned_vs_unlearned"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(result["status"], "scoped_demote_applied")
            payload = load_externalized_json(row["payload_json"])
            self.assertEqual(
                payload["scoped_underperformance"]["comparison_status"],
                "same_scope_self_loss_without_benchmark",
            )
        finally:
            conn.close()

    def test_neutral_shadow_tracking_records_missed_opportunity_without_policy_action(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE portfolio (
                    id TEXT,
                    config_id TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            cursor.execute("INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?)", ("p1", "2025-02-10", "ZZ", 1200.0))

            summary = _neutral_shadow_tracking_summary(
                cursor,
                config_id="cfg",
                trading_date="2025-02-10",
                recommendations=[
                    {
                        "id": "rec-neutral",
                        "underlying_code": "ZZ",
                        "signal_snapshot": {
                            "technical": {
                                "signal": "Neutral",
                                "neutral_reason": "needs confirmation",
                                "missing_evidence": ["volume"],
                                "conflicting_factors": [],
                                "would_change_view_if": "breakout confirms",
                            },
                            "fundamental": {"signal": "Bullish", "confidence": 0.70},
                            "commodity_news": {"signal": "Bullish", "confidence": 0.65},
                        },
                    }
                ],
            )

            self.assertEqual(summary["observation_count"], 1)
            self.assertEqual(summary["missed_opportunity_count"], 1)
            self.assertGreater(summary["total_shadow_pnl"], 0)
            event = cursor.execute(
                "SELECT action_json FROM learning_event_log WHERE event_type = ?",
                ("neutral_shadow_tracking",),
            ).fetchone()
            self.assertTrue(json.loads(event["action_json"])["tracking_only"])
        finally:
            conn.close()

    def test_neutral_shadow_tracking_uses_only_settled_forward_window(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute(
                """
                CREATE TABLE daily_settlement (
                    portfolio_id TEXT,
                    trading_date TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            cursor.executemany(
                "INSERT INTO daily_settlement VALUES (?, ?)",
                [
                    ("p1", "2025-02-11"),
                    ("p1", "2025-02-12"),
                    ("p1", "2025-02-13"),
                    ("p1", "2025-02-14"),
                ],
            )
            cursor.executemany(
                "INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?)",
                [
                    ("p1", "2025-02-11", "ZZ", 100.0),
                    ("p1", "2025-02-12", "ZZ", 200.0),
                    ("p1", "2025-02-13", "ZZ", 300.0),
                    ("p1", "2025-02-14", "ZZ", 5000.0),
                ],
            )

            summary = _neutral_shadow_tracking_summary(
                cursor,
                cfg={"llm_signal_quality": {"neutral_accountability": {"shadow_forward_days": 3}}},
                config_id="cfg",
                trading_date="2025-02-10",
                recommendations=[
                    {
                        "id": "rec-neutral",
                        "underlying_code": "ZZ",
                        "signal_snapshot": {
                            "technical": {"signal": "Neutral"},
                            "fundamental": {"signal": "Bullish", "confidence": 0.70},
                            "commodity_news": {"signal": "Bullish", "confidence": 0.65},
                        },
                    }
                ],
                write_event=False,
            )

            self.assertEqual(summary["forward_status"], "applied")
            self.assertEqual(summary["forward_window_dates"], ["2025-02-11", "2025-02-12", "2025-02-13"])
            self.assertEqual(summary["forward_observation_count"], 1)
            self.assertEqual(summary["forward_total_shadow_pnl"], 600.0)
            self.assertEqual(summary["forward_missed_opportunity_count"], 1)
        finally:
            conn.close()

    def test_neutral_forward_shadow_backfill_waits_for_future_settlements(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute("CREATE TABLE daily_settlement (portfolio_id TEXT, trading_date TEXT)")
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    trading_date TEXT,
                    source_type TEXT,
                    underlying_code TEXT,
                    signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT,
                    signal_snapshot_sha256 TEXT,
                    created_at TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            cursor.executemany(
                "INSERT INTO daily_settlement VALUES (?, ?)",
                [
                    ("p1", "2025-02-10"),
                    ("p1", "2025-02-11"),
                    ("p1", "2025-02-12"),
                    ("p1", "2025-02-13"),
                ],
            )
            cursor.executemany(
                "INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?)",
                [
                    ("p1", "2025-02-11", "ZZ", 100.0),
                    ("p1", "2025-02-12", "ZZ", 200.0),
                    ("p1", "2025-02-13", "ZZ", 300.0),
                ],
            )
            cursor.execute(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "rec-neutral",
                    "cfg",
                    "2025-02-10",
                    "strategy",
                    "ZZ",
                    json.dumps(
                        {
                            "technical": {"signal": "Neutral"},
                            "fundamental": {"signal": "Bullish", "confidence": 0.70},
                            "commodity_news": {"signal": "Bullish", "confidence": 0.65},
                        }
                    ),
                    None,
                    None,
                    "now",
                ),
            )

            pending = _backfill_neutral_forward_shadow_tracking(
                cursor,
                cfg={"llm_signal_quality": {"neutral_accountability": {"shadow_forward_days": 3}}},
                config_id="cfg",
                trading_date="2025-02-12",
            )
            applied = _backfill_neutral_forward_shadow_tracking(
                cursor,
                cfg={"llm_signal_quality": {"neutral_accountability": {"shadow_forward_days": 3}}},
                config_id="cfg",
                trading_date="2025-02-13",
            )

            self.assertEqual(pending["rows"], 0)
            self.assertEqual(applied["rows"], 1)
            row = cursor.execute(
                "SELECT evidence_json FROM learning_event_log WHERE event_type = ?",
                ("neutral_forward_shadow_tracking",),
            ).fetchone()
            evidence = json.loads(row["evidence_json"])
            self.assertEqual(evidence["forward_window_dates"], ["2025-02-11", "2025-02-12", "2025-02-13"])
            self.assertEqual(evidence["forward_total_shadow_pnl"], 600.0)
        finally:
            conn.close()

    def test_no_trade_opportunity_memory_backfills_shadow_only_after_settlement(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute("CREATE TABLE daily_settlement (portfolio_id TEXT, trading_date TEXT)")
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    reference_portfolio_id TEXT,
                    trading_date TEXT,
                    effective_trade_date TEXT,
                    source_type TEXT,
                    underlying_code TEXT,
                    contract_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    base_price REAL,
                    execution_price REAL,
                    open_price REAL,
                    prev_close_price REAL,
                    signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT,
                    signal_snapshot_sha256 TEXT,
                    warning_message TEXT,
                    created_at TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            snapshot = {
                "technical": {
                    "signal": "Bullish",
                    "confidence": 0.7,
                    "template_name": "breakout",
                    "horizon_class": "short",
                    "opportunity_type": "trend_continuation",
                    "opportunity_layer": "tradeable_setup",
                    "factor_focus": ["trend"],
                    "current_evidence_conflict": [],
                },
                "fundamental": {"signal": "Bullish", "confidence": 0.6},
                "commodity_news": {
                    "signal": "Neutral",
                    "confidence": 0.3,
                    "neutral_reason": "news impact needs price confirmation",
                    "missing_evidence": [],
                    "conflicting_factors": [],
                    "would_change_view_if": "price confirms upside event follow-through",
                    "neutral_opportunity_bucket": "watchlist_trigger",
                    "neutral_trigger_condition": "price confirms upside event follow-through",
                    "neutral_shadow_side": "long",
                    "neutral_watchlist_priority": "medium",
                    "metadata": {
                        "neutral_opportunity_contract": {
                            "bucket": "watchlist_trigger",
                            "trigger_condition": "price confirms upside event follow-through",
                            "shadow_side": "long",
                            "watchlist_priority": "medium",
                            "tracking_only": True,
                            "trade_permission": "none_without_current_confirmation",
                        }
                    },
                },
                "pre_open_plan": {
                    "analyst_signal_combo": ["Bullish", "Bullish", "Neutral"],
                    "tradable_lots_reason": "intraday_trigger_not_met",
                    "decision_horizon": "short",
                    "market_regime": "trend",
                },
                "trade_research_contracts": {
                    "technical": {
                        "opportunity_type": "trend_continuation",
                        "opportunity_layer": "tradeable_setup",
                        "factor_focus": ["trend"],
                        "current_evidence_conflict": [],
                    }
                },
            }
            cursor.executemany(
                "INSERT INTO daily_settlement VALUES (?, ?)",
                [("p1", "2025-03-04"), ("p1", "2025-03-05"), ("p1", "2025-03-06")],
            )
            cursor.executemany(
                """
                INSERT INTO futures_recommendation VALUES (?, 'cfg', 'p1', ?, ?, 'strategy', 'BU', 'bu2506',
                    ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?)
                """,
                [
                    ("rec-nt", "2025-03-03", "2025-03-03", "hold", 0, 3200.0, json.dumps(snapshot), "2025-03-03T09:00:00"),
                    ("rec-p1", "2025-03-04", "2025-03-04", "hold", 0, 3210.0, "{}", "2025-03-04T09:00:00"),
                    ("rec-p2", "2025-03-05", "2025-03-05", "hold", 0, 3220.0, "{}", "2025-03-05T09:00:00"),
                    ("rec-p3", "2025-03-06", "2025-03-06", "hold", 0, 3230.0, "{}", "2025-03-06T09:00:00"),
                ],
            )

            rows = _write_no_trade_opportunity_memory(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-03",
                strategy_recommendations=[
                    {
                        "id": "rec-nt",
                        "config_id": "cfg",
                        "underlying_code": "BU",
                        "action": "hold",
                        "lots": 0,
                        "base_price": 3200.0,
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
            )
            pending = _backfill_no_trade_opportunity_shadow_results(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True, "shadow_forward_days": [3]}}},
                config_id="cfg",
                trading_date="2025-03-05",
            )
            applied = _backfill_no_trade_opportunity_shadow_results(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True, "shadow_forward_days": [3]}}},
                config_id="cfg",
                trading_date="2025-03-06",
            )

            self.assertEqual(rows, 1)
            self.assertEqual(pending["status"], "no_ready_rows")
            self.assertEqual(applied["updated_rows"], 1)
            item = cursor.execute("SELECT * FROM no_trade_opportunity_memory").fetchone()
            results = json.loads(item["shadow_results_json"])
            payload = load_externalized_json(item["payload_json"])
            self.assertEqual(results[0]["horizon_days"], 3)
            self.assertEqual(item["classification"], "missed_opportunity")
            self.assertEqual(payload["neutral_opportunity_observations"][0]["bucket"], "watchlist_trigger")
            self.assertEqual(payload["no_trade_reason"], "intraday_trigger_not_met")
            self.assertEqual(payload["no_trade_reason_category"]["category"], "timing")
            self.assertEqual(payload["no_trade_reason_category"]["category_label"], "择时")
            self.assertEqual(payload[CONTRACT_KEY]["memory_type"], "no_trade_opportunity_memory")
            self.assertIn("position_impact_conditions", payload[CONTRACT_KEY])
            self.assertIn("not increase size", " ".join(payload[CONTRACT_KEY]["position_impact_conditions"]))
            self.assertIn("neutral_condition=commodity_news:watchlist_trigger", item["evidence_summary"])
            self.assertIn("no_trade_category=择时:timing", item["evidence_summary"])
            event_row = cursor.execute(
                "SELECT evidence_json FROM learning_event_log WHERE event_type = ?",
                ("no_trade_opportunity_memory",),
            ).fetchone()
            event_evidence = json.loads(event_row["evidence_json"])
            self.assertEqual(event_evidence["no_trade_reason_categories"], {"timing": 1})
        finally:
            conn.close()

    def test_limit_locked_skip_writes_timing_opportunity_memory(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            snapshot = {
                "technical": {
                    "signal": "Bullish",
                    "template_name": "breakout_continuation",
                    "horizon_class": "short",
                },
                "fundamental": {"signal": "Bullish"},
                "commodity_news": {"signal": "Neutral"},
                "pre_open_plan": {
                    "analyst_signal_combo": ["Bullish", "Bullish", "Neutral"],
                    "signal_direction": "long",
                    "decision_horizon": "short",
                    "market_regime": "trend",
                },
                "execution_translation": {
                    "market_rule_block": {
                        "limit_lock": {
                            "blocked": True,
                            "reason": "limit_locked_no_fill",
                            "side": "buy_like",
                            "execution_price": 3500.0,
                            "limit_price": 3500.0,
                            "limit_up": 3500.0,
                        }
                    }
                },
                "execution_result": {
                    "outcome": "skipped",
                    "status": "skipped",
                    "transaction_count": 0,
                    "no_trade_reason": "limit_locked_no_fill",
                },
            }

            rows = _write_no_trade_opportunity_memory(
                cursor,
                cfg={"learning": {"no_trade_opportunity_memory": {"enabled": True}}},
                config_id="cfg",
                trading_date="2025-03-10",
                strategy_recommendations=[
                    {
                        "id": "rec-limit",
                        "config_id": "cfg",
                        "underlying_code": "RB",
                        "action": "open_long",
                        "lots": 2,
                        "base_price": 3499.0,
                        "execution_price": 3500.0,
                        "signal_snapshot": json.dumps(snapshot),
                    }
                ],
            )

            self.assertEqual(rows, 1)
            item = cursor.execute("SELECT * FROM no_trade_opportunity_memory").fetchone()
            payload = load_externalized_json(item["payload_json"], item["payload_artifact_path"], item["payload_sha256"])
            contract = payload[CONTRACT_KEY]
            self.assertEqual(item["execution_reason"], "limit_locked_no_fill")
            self.assertEqual(item["candidate_lots"], 2)
            self.assertEqual(item["shadow_lots"], 1)
            self.assertEqual(payload["execution_no_trade_reason"], "limit_locked_no_fill")
            self.assertEqual(payload["no_trade_reason_category"]["category"], "execution")
            self.assertEqual(payload["no_trade_reason_category"]["category_label"], "执行")
            self.assertEqual(payload["limit_lock_audit"]["reason"], "limit_locked_no_fill")
            self.assertIn("execution_timing_case=limit_locked_no_fill", item["evidence_summary"])
            self.assertIn("no_trade_category=执行:execution", item["evidence_summary"])
            self.assertIn("limit_locked_no_fill timing_case", " ".join(contract["usable_memory"]))
            self.assertIn("No-trade category=execution", " ".join(contract["analysis_strategy_updates"]))
            self.assertIn("entry/exit timing research question", " ".join(contract["analysis_strategy_updates"]))
            self.assertIn("Do not chase at the limit price", " ".join(contract["trading_strategy_updates"]))
            self.assertIn("not increase size", " ".join(contract["position_impact_conditions"]))
        finally:
            conn.close()

    def test_tail_loss_sentinel_and_alpha_promotion_write_adaptive_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE portfolio (id TEXT, config_id TEXT)")
            cursor.execute(
                """
                CREATE TABLE daily_settlement (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    current_account_equity REAL,
                    current_balance REAL,
                    created_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE ticker_daily_pnl (
                    portfolio_id TEXT,
                    trading_date TEXT,
                    ticker TEXT,
                    daily_pnl REAL,
                    new_position_pnl REAL,
                    position_type TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    effective_trade_date TEXT,
                    source_type TEXT,
                    underlying_code TEXT,
                    action TEXT,
                    lots INTEGER,
                    signal_snapshot TEXT,
                    signal_snapshot_artifact_path TEXT,
                    signal_snapshot_sha256 TEXT,
                    created_at TEXT
                )
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES (?, ?)", ("p1", "cfg"))
            cursor.execute("INSERT INTO daily_settlement VALUES (?, ?, ?, ?, ?)", ("p1", "2025-03-07", 5000000.0, 4850000.0, "now"))
            cursor.execute("INSERT INTO ticker_daily_pnl VALUES (?, ?, ?, ?, ?, ?)", ("p1", "2025-03-07", "TA", -36000.0, -36000.0, "long"))
            snapshot = {
                "technical": {"signal": "Bullish", "template_name": "breakout", "horizon_class": "short"},
                "pre_open_plan": {"analyst_signal_combo": ["Bullish", "Neutral", "Neutral"], "decision_horizon": "short", "market_regime": "trend"},
            }
            cursor.execute(
                "INSERT INTO futures_recommendation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("rec-ta", "cfg", "2025-03-07", "strategy", "TA", "open_long", 1, json.dumps(snapshot), None, None, "now"),
            )
            cursor.execute(
                """
                INSERT INTO signal_template_performance (
                    id, config_id, ticker, side, signal_template, horizon_class, market_regime,
                    sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
                    confidence_score, last_updated, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tpl-alpha", "cfg", "BU", "long", "long_breakout_short", "short", "trend", 5, 0.8, 6000, 1200, 2.1, 0.85, "now", "2025-04-01", "{}"),
            )

            tail_rows = _write_tail_loss_sentinel_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-07",
                cfg={"learning": {"tail_loss_sentinel": {"enabled": True, "min_abs_loss": 25000, "valid_days": 5}}},
            )
            alpha_rows = _write_alpha_promotion_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-07",
                cfg={"learning": {"alpha_promotion": {"enabled": True, "min_sample_count": 5, "min_win_rate": 0.6, "min_net_pnl": 1000}}},
            )

            self.assertEqual(tail_rows, 1)
            self.assertEqual(alpha_rows, 1)
            rows = cursor.execute("SELECT policy_type, policy_action, payload_json FROM adaptive_policy_state ORDER BY policy_type").fetchall()
            self.assertEqual([(row["policy_type"], row["policy_action"]) for row in rows], [("alpha_promotion", "protect"), ("tail_loss_sentinel", "cap")])
            alpha_payload = load_externalized_json(rows[0]["payload_json"])
            tail_payload = load_externalized_json(rows[1]["payload_json"])
            self.assertEqual(alpha_payload[CONTRACT_KEY]["position_authority"], "pm_auditor_conditioned")
            self.assertEqual(tail_payload[CONTRACT_KEY]["position_authority"], "risk_reduction_conditioned")
        finally:
            conn.close()

    def test_contextual_rule_calibration_writes_intraday_policy_from_missed_timing_shadow(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            payload = {"rule": "timing sample"}
            cursor.execute(
                """
                INSERT INTO no_trade_opportunity_memory (
                    id, config_id, trading_date, ticker, side, sector, signal_template,
                    signal_combo, horizon_class, market_regime, opportunity_type,
                    opportunity_layer, candidate_lots, shadow_lots, shadow_entry_price,
                    pm_reason, auditor_reason, execution_reason, evidence_summary,
                    status, classification, shadow_results_json, payload_json,
                    created_at, last_reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "nt-1",
                    "cfg",
                    "2025-03-03",
                    "BU",
                    "long",
                    "energy",
                    "long_breakout_short",
                    json.dumps(["Bullish", "Neutral", "Neutral"]),
                    "short",
                    "trend",
                    "probe",
                    "tradeable_setup",
                    1,
                    1,
                    3500.0,
                    "intraday_trigger_not_met",
                    "",
                    "intraday_trigger_not_met",
                    "timing miss",
                    "closed",
                    "missed_opportunity",
                    json.dumps([{"horizon_days": 3, "shadow_pnl": 2500.0}]),
                    json.dumps(payload),
                    "now",
                    "now",
                ),
            )

            rows = _write_contextual_rule_calibration_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-10",
                cfg={
                    "learning": {
                        "contextual_rule_calibration": {
                            "enabled": True,
                            "valid_days": 5,
                            "min_shadow_pnl_for_relaxation": 1000,
                            "relaxed_opening_range_miss": 0.003,
                            "relaxed_intraday_confirmation_score": 0.65,
                        }
                    }
                },
                strategy_recommendations=[],
                no_trade_reason_counter=Counter(),
            )

            self.assertEqual(rows, 1)
            row = cursor.execute(
                """
                SELECT policy_type, ticker, side, horizon_class, market_regime, payload_json
                FROM adaptive_policy_state
                WHERE policy_type = 'contextual_rule_calibration:intraday_confirmation'
                """
            ).fetchone()
            self.assertEqual(row["ticker"], "BU")
            self.assertEqual(row["side"], "long")
            saved_payload = load_externalized_json(row["payload_json"])
            self.assertEqual(saved_payload["rule_group"], "intraday_confirmation")
            self.assertIn("intraday_confirmation", saved_payload["rule_adjustments"])
            self.assertEqual(
                saved_payload["rule_adjustments"]["intraday_confirmation"]["confirmed_memory_min_market_confirmation_score"],
                0.65,
            )
        finally:
            conn.close()

    def test_contextual_rule_calibration_writes_technical_parameter_policy(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO analyst_performance (
                    id, config_id, analyst, ticker, sector, horizon_class, signal_side,
                    sample_count, hit_rate, avg_pnl, net_pnl, confidence_score,
                    last_updated, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ap-technical",
                    "cfg",
                    "technical",
                    "BU",
                    "energy",
                    "short",
                    "long",
                    4,
                    0.75,
                    1200.0,
                    4800.0,
                    0.55,
                    "now",
                    "2025-03-30",
                    json.dumps({"sample_count": 4}),
                ),
            )

            rows = _write_contextual_rule_calibration_state(
                cursor,
                config_id="cfg",
                trading_date="2025-03-10",
                cfg={
                    "learning": {
                        "contextual_rule_calibration": {
                            "enabled": True,
                            "valid_days": 5,
                            "max_rows_per_day": 10,
                            "min_analyst_samples": 3,
                            "min_analyst_confidence": 0.35,
                            "technical_positive_hit_rate": 0.60,
                            "technical_weak_hit_rate": 0.40,
                        }
                    }
                },
                strategy_recommendations=[],
                no_trade_reason_counter=Counter(),
            )

            self.assertGreaterEqual(rows, 1)
            row = cursor.execute(
                """
                SELECT policy_type, ticker, side, horizon_class, market_regime, sample_count, payload_json
                FROM adaptive_policy_state
                WHERE policy_type = 'contextual_rule_calibration:technical_parameters'
                """
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ticker"], "BU")
            self.assertEqual(row["side"], "*")
            self.assertEqual(row["horizon_class"], "short")
            payload = load_externalized_json(row["payload_json"])
            self.assertIn("technical_parameters", payload["rule_adjustments"])
            self.assertIn("trend_short_multiplier", payload["rule_adjustments"]["technical_parameters"])
        finally:
            conn.close()

    def test_technical_parameter_calibration_applies_bounded_adjustments(self):
        params = {
            "trend": {"short": 8, "medium": 21, "long": 55},
            "rsi": {"period": 14, "bullish": 30, "bearish": 70},
            "mean_reversion": {"bollinger_std": 2.0, "bollinger_window": 20, "rolling_window": 50},
        }
        row = {
            "id": "policy-1",
            "ticker": "BU",
            "side": "*",
            "horizon_class": "short",
            "market_regime": "*",
            "policy_type": "contextual_rule_calibration:technical_parameters",
            "confidence_score": 0.55,
            "sample_count": 4,
            "reason": "test",
            "payload": {
                "rule_adjustments": {
                    "technical_parameters": {
                        "trend_short_multiplier": 0.50,
                        "trend_long_multiplier": 2.0,
                        "rsi_bullish_shift": -20,
                        "rsi_bearish_shift": 20,
                        "bollinger_std_multiplier": 1.50,
                    }
                }
            },
        }

        adjusted, diagnostics = apply_technical_parameter_calibration(
            params,
            [row],
            ticker="BU",
            horizon_class="short",
            market_regime="trend",
        )

        self.assertEqual(adjusted["trend"]["short"], 7)
        self.assertEqual(adjusted["trend"]["long"], 63)
        self.assertEqual(adjusted["rsi"]["bullish"], 25)
        self.assertEqual(adjusted["rsi"]["bearish"], 75)
        self.assertEqual(adjusted["mean_reversion"]["bollinger_std"], 2.2)
        self.assertEqual(len(diagnostics["applied"]), 1)

    def test_phase1_signal_persistence_uses_recommendation_reference_portfolio(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.executescript(
                """
                CREATE TABLE portfolio (
                    id TEXT PRIMARY KEY,
                    config_id TEXT,
                    trading_date TEXT
                );
                CREATE TABLE signal (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT,
                    ticker TEXT,
                    analyst TEXT
                );
                """
            )
            cursor.execute("INSERT INTO portfolio VALUES ('phase1-p', 'cfg', '2025-01-01')")
            cursor.execute("INSERT INTO portfolio VALUES ('settled-p', 'cfg', '2025-01-02')")
            for ticker in ("BU", "RB"):
                for analyst in ("commodity_news", "fundamental", "technical"):
                    cursor.execute(
                        "INSERT INTO signal VALUES (?, 'phase1-p', ?, ?)",
                        (f"{ticker}-{analyst}", ticker, analyst),
                    )
            recommendations = []
            for ticker in ("BU", "RB"):
                recommendations.append(
                    {
                        "underlying_code": ticker,
                        "reference_portfolio_id": "phase1-p",
                        "signal_snapshot": {
                            "commodity_news": {"signal": "Neutral"},
                            "fundamental": {"signal": "Neutral"},
                            "technical": {"signal": "Neutral"},
                        },
                    }
                )
            errors = []
            warnings = []

            audit = _validate_phase1_signal_persistence(
                cursor,
                config_id="cfg",
                trading_date="2025-01-02",
                strategy_recommendations=recommendations,
                expected_tickers=2,
                expected_analysts=("commodity_news", "fundamental", "technical"),
                errors=errors,
                warnings=warnings,
            )

            self.assertEqual(errors, [])
            self.assertTrue(audit["verified"])
            self.assertEqual(audit["db_pairs"], 6)
            self.assertEqual(audit["reference_portfolio_ids"], ["phase1-p"])
        finally:
            conn.close()

    def test_signal_context_history_persists_explicit_lifecycle_fields(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            rows = _write_signal_context_history(
                cursor,
                cfg={},
                config_id="cfg",
                trading_date="2025-02-10",
                recommendations=[
                    {
                        "id": "rec-1",
                        "underlying_code": "BU",
                        "signal_snapshot": {
                            "technical": {
                                "signal": "Bullish",
                                "horizon_class": "short",
                                "expected_horizon_days": 2,
                                "trend_stage": "low_position_reversal",
                                "price_percentile": 0.24,
                                "trigger_type": "reversal_confirmed",
                                "entry_type": "initial",
                                "invalidation_level": 3220.0,
                            },
                            "pre_open_plan": {
                                "target_position_ratio": 0.08,
                                "target_return": 0.035,
                            },
                        },
                    }
                ],
            )

            cursor.execute("SELECT * FROM signal_context_history WHERE recommendation_id = ?", ("rec-1",))
            row = dict(cursor.fetchone())
            self.assertEqual(rows, 1)
            self.assertEqual(row["ticker"], "BU")
            self.assertAlmostEqual(row["price_percentile"], 0.24)
            self.assertAlmostEqual(row["invalidation_level"], 3220.0)
            self.assertAlmostEqual(row["target_return"], 0.035)
        finally:
            conn.close()

    def test_config_overlay_persists_previous_and_rollback_values(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            inserted = _write_config_overlay(
                cursor,
                config_id="cfg",
                trading_date="2025-02-10",
                cfg={
                    "learning": {"config_overlay": {"enabled": True}, "overlay_expires_after_days": 5},
                    "capital_utilization_control": {
                        "target_margin_ratio_min": 0.16,
                        "target_margin_ratio_max": 0.20,
                        "target_margin_ratio_confirmed": 0.18,
                    },
                },
                settlement_row={"margin_ratio": 0.08, "current_margin": 400000.0},
            )

            cursor.execute(
                """
                SELECT *
                FROM config_learning_overlay
                WHERE param_key = ?
                """,
                ("capital_utilization_control.target_margin_ratio_min",),
            )
            row = dict(cursor.fetchone())
            self.assertEqual(inserted, 3)
            self.assertEqual(json.loads(row["learned_value_json"]), 0.16)
            self.assertEqual(json.loads(row["previous_value_json"]), 0.16)
            self.assertEqual(json.loads(row["rollback_value_json"]), 0.16)
            self.assertTrue(row["source_event_id"])
        finally:
            conn.close()

    def test_reviewer_learning_report_writes_markdown_and_json(self):
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO signal_template_performance (
                    id, config_id, ticker, side, signal_template, horizon_class, market_regime,
                    sample_count, win_rate, net_pnl, avg_pnl, profit_factor,
                    confidence_score, last_updated, valid_until, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tpl-1",
                    "cfg",
                    "BU",
                    "long",
                    "long_reversal_confirmed_trend",
                    "short",
                    "trend",
                    4,
                    0.75,
                    2200.0,
                    550.0,
                    2.5,
                    0.8,
                    "2025-02-10T00:00:00Z",
                    "2025-03-01",
                    "{}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO capital_deployment_state (
                    id, config_id, trading_date, current_margin_ratio,
                    target_margin_ratio_min, target_margin_ratio_max, reason_bucket, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("cap-1", "cfg", "2025-02-10", 0.08, 0.16, 0.20, "high_score_signal_shortage", "now"),
            )
            cursor.execute(
                """
                INSERT INTO learning_event_log (
                    id, config_id, trading_date, event_type, scope_type, scope_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("evt-1", "cfg", "2025-02-10", "performance_attribution", "daily", "2025-02-10", "now"),
            )
            cursor.execute(
                """
                CREATE TABLE futures_recommendation (
                    id TEXT,
                    config_id TEXT,
                    trading_date TEXT,
                    source_type TEXT,
                    underlying_code TEXT,
                    created_at TEXT,
                    signal_snapshot TEXT
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO futures_recommendation
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rec-neutral",
                    "cfg",
                    "2025-02-10",
                    "strategy",
                    "BU",
                    "now",
                    json.dumps(
                        {
                            "technical": {
                                "signal": "Neutral",
                                "neutral_reason": "conflicting indicators",
                                "missing_evidence": ["volume confirmation"],
                                "conflicting_factors": ["range_bound"],
                                "would_change_view_if": "breakout confirms",
                                "metadata": {"risk_flags": ["conflicting_indicators"]},
                            }
                        }
                    ),
                ),
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                paths = _write_reviewer_learning_report(
                    cursor=cursor,
                    cfg={"exp_name": "unit"},
                    config_id="cfg",
                    trading_date="2025-02-10",
                    learning_summary={"template_rows": 1, "config_overlay_rows": 0},
                    output_root=Path(temp_dir),
                    run_id="test-run",
                )
                markdown = Path(paths["markdown"])
                payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))

                self.assertTrue(markdown.exists())
                markdown_text = markdown.read_text(encoding="utf-8")
                self.assertIn("Positive Templates", markdown_text)
                self.assertIn("Neutral Accountability", markdown_text)
                self.assertEqual(payload["positive_templates"][0]["signal_template"], "long_reversal_confirmed_trend")
                self.assertEqual(payload["neutral_accountability"]["neutral_count"], 1)
        finally:
            conn.close()


class _FakeReviewerWeightDB:
    def get_signal_history(self, **kwargs):
        return []

    def get_analyst_performance(self, **kwargs):
        return []

    def get_signal_template_performance(self, **kwargs):
        return [
            {
                "horizon_class": "short",
                "signal_template": "long_reversal_confirmed_trend",
                "sample_count": 4,
                "win_rate": 0.75,
                "net_pnl": 1800.0,
                "confidence_score": 0.8,
            }
        ]

    def get_adaptive_policy_state(self, **kwargs):
        return [
            {
                "horizon_class": "medium",
                "signal_template": "long_late_chase_range",
                "policy_action": "cap",
                "multiplier": 0.5,
                "confidence_score": 0.8,
            }
        ]


class ReviewerDynamicWeightsRegressionTest(unittest.TestCase):
    def test_dynamic_weights_consume_template_performance_and_adaptive_policy(self):
        weights = calibrate_weights_by_signal_history(
            db=_FakeReviewerWeightDB(),
            config_id="cfg",
            ticker="BU",
            trading_date="2025-02-10",
            current_weights={"technical": 1 / 3, "fundamental": 1 / 3, "commodity_news": 1 / 3},
        )

        self.assertGreater(weights["technical"], 1 / 3)
        self.assertLess(weights["fundamental"], 1 / 3)
        self.assertAlmostEqual(sum(weights.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
