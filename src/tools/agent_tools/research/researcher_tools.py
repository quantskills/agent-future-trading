from __future__ import annotations

"""Researcher tools for Phase4 learning and future-memory generation.

The researcher runs only after reviewer validation has established the day's
settled facts. It may call an LLM to produce research hypotheses, but it does
not validate accounting, write transactions, or issue trading instructions.
"""

import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from database.artifact_store import externalize_json_for_db, externalize_text_for_db
from tools.agent_tools.research.learning_contract import CONTRACT_KEY, build_next_round_memory_contract
from util.futures_audit import categorize_no_trade_reason
from util.logger import logger


class CausalReviewLLMOutput(BaseModel):
    primary_cause: str = Field(default="unknown")
    direction_error: bool = Field(default=False)
    horizon_error: bool = Field(default=False)
    entry_error: bool = Field(default=False)
    exit_error: bool = Field(default=False)
    position_sizing_error: bool = Field(default=False)
    auditor_error: bool = Field(default=False)
    pm_error: bool = Field(default=False)
    missed_factors: List[str] = Field(default_factory=list)
    analyst_lessons: List[str] = Field(default_factory=list)
    do_not_trade_reason: str = Field(default="")
    similar_case_key: str = Field(default="")
    confidence_score: float = Field(default=0.0)


class ExploratoryHypothesisItem(BaseModel):
    hypothesis_text: str = Field(default="")
    ticker: str = Field(default="*")
    sector: str = Field(default="*")
    side: str = Field(default="*")
    horizon_class: str = Field(default="*")
    market_regime: str = Field(default="*")
    evidence_summary: str = Field(default="")
    suggested_use: str = Field(default="prompt prior only; validate with future samples")
    entry_timing_hint: str = Field(default="")
    exit_timing_hint: str = Field(default="")
    holding_period_hint: str = Field(default="")
    invalidation_condition: str = Field(default="")
    validation_plan: str = Field(default="")
    confidence_score: float = Field(default=0.0)


class ExploratoryHypothesisLLMOutput(BaseModel):
    hypotheses: List[ExploratoryHypothesisItem] = Field(default_factory=list)
    researcher_note: str = Field(default="")

    @property
    def reviewer_note(self) -> str:
        """Backward-compatible alias for older tests/artifacts."""
        return self.researcher_note


def _reviewer_helpers():
    # Imported lazily to keep the Phase4 reviewer module from owning LLM calls.
    from tools.agent_tools.research import reviewer_tools

    return reviewer_tools


def _build_causal_evidence_pack(
    *,
    config_id: str,
    trading_date: str,
    strategy_recommendations: List[Dict[str, Any]],
    settlement_row: Optional[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> Dict[str, Any]:
    reviewer = _reviewer_helpers()
    return {
        "agent_name": "researcher",
        "evidence_pack_id": str(uuid.uuid4()),
        "config_id": config_id,
        "trading_date": trading_date,
        "pre_trade_evidence": [
            {
                "recommendation_id": row.get("id"),
                "ticker": row.get("underlying_code"),
                "action": row.get("action"),
                "lots": row.get("lots"),
                "signal_snapshot": reviewer._recommendation_snapshot(row),
            }
            for row in strategy_recommendations
        ],
        "post_trade_outcome": {
            "daily_pnl": reviewer._safe_float((settlement_row or {}).get("daily_pnl")),
            "commission": reviewer._safe_float((settlement_row or {}).get("commission")),
            "current_margin_ratio": reviewer._safe_float((settlement_row or {}).get("margin_ratio")),
            "no_trade_reasons": dict(no_trade_reason_counter),
            "no_trade_reason_categories": _no_trade_reason_category_counts(no_trade_reason_counter),
        },
    }


def _no_trade_reason_category_counts(no_trade_reason_counter: Counter) -> Dict[str, int]:
    category_counter: Counter = Counter()
    for reason, count in (no_trade_reason_counter or Counter()).items():
        category = categorize_no_trade_reason(reason)["category"]
        category_counter[category] += int(count or 0)
    return {str(key): int(value) for key, value in category_counter.most_common()}


def run_researcher_causal_review(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    settlement_row: Optional[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> int:
    reviewer = _reviewer_helpers()
    review_cfg = ((cfg.get("learning", {}) or {}).get("reviewer_causal_review") or {})
    if not bool(review_cfg.get("enabled", False)):
        return 0
    evidence = _build_causal_evidence_pack(
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
        settlement_row=settlement_row,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    prompt = (
        "You are AgentQuant Researcher doing post-trade causal research. "
        "Use only pre_trade_evidence for ex-ante causes and post_trade_outcome for labels. "
        "Return concise structured lessons, next-round usable memory, usage boundaries, "
        "and validation ideas. Do not provide direct trading authority.\n"
        + reviewer._json_dumps(evidence)[:12000]
    )
    raw_response = ""
    output = CausalReviewLLMOutput()
    if bool(review_cfg.get("use_llm", False)):
        try:
            from llm.inference import agent_call

            output = agent_call(
                prompt=prompt,
                llm_config=cfg.get("llm", {}),
                pydantic_model=CausalReviewLLMOutput,
            )
            raw_response = reviewer._json_dumps(output.model_dump())
        except Exception as exc:
            raw_response = f"llm_causal_research_failed: {exc}"
            logger.warning(f"Researcher LLM causal review failed on {trading_date}: {exc}")
    else:
        raw_response = "llm disabled; deterministic candidate only"

    note_id = str(uuid.uuid4())
    prompt_ext = externalize_text_for_db(
        prompt,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="raw_prompt",
        config_id=config_id,
        trading_date=trading_date,
    )
    response_ext = externalize_text_for_db(
        raw_response,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="raw_response",
        config_id=config_id,
        trading_date=trading_date,
    )
    payload_ext = externalize_json_for_db(
        evidence,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="payload",
        config_id=config_id,
        trading_date=trading_date,
    )
    cursor.execute(
        """
        INSERT INTO reviewer_llm_notes (
            id, config_id, trading_date, evidence_pack_id, ticker,
            raw_prompt, raw_response, created_at, payload_json,
            raw_prompt_artifact_path, raw_prompt_sha256,
            raw_prompt_size, raw_prompt_summary_json,
            raw_response_artifact_path, raw_response_sha256,
            raw_response_size, raw_response_summary_json,
            payload_artifact_path, payload_sha256,
            payload_size, payload_summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id,
            config_id,
            trading_date,
            evidence["evidence_pack_id"],
            "*",
            prompt_ext.inline_value,
            response_ext.inline_value,
            reviewer._utc_now(),
            payload_ext.inline_value,
            prompt_ext.artifact_path,
            prompt_ext.sha256,
            prompt_ext.size_bytes,
            prompt_ext.summary_json,
            response_ext.artifact_path,
            response_ext.sha256,
            response_ext.size_bytes,
            response_ext.summary_json,
            payload_ext.artifact_path,
            payload_ext.sha256,
            payload_ext.size_bytes,
            payload_ext.summary_json,
        ),
    )
    candidate_payload = output.model_dump() if hasattr(output, "model_dump") else {}
    candidate_payload["agent_name"] = "researcher"
    cursor.execute(
        """
        INSERT INTO causal_review_candidate (
            id, config_id, trading_date, evidence_pack_id, ticker, side,
            candidate_type, confidence_score, rule_validation_status,
            created_at, valid_until, payload_json
        ) VALUES (?, ?, ?, ?, '*', '*', ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            config_id,
            trading_date,
            evidence["evidence_pack_id"],
            "post_trade_causal_research",
            reviewer._safe_float(candidate_payload.get("confidence_score"), 0.0),
            "notes_only_pending_rule_validation",
            reviewer._utc_now(),
            (
                datetime.strptime(str(trading_date)[:10], "%Y-%m-%d") + timedelta(days=10)
            ).strftime("%Y-%m-%d"),
            reviewer._json_dumps(candidate_payload),
        ),
    )
    return 1


def _recent_trade_episodes_for_research(
    cursor: sqlite3.Cursor,
    *,
    config_id: str,
    trading_date: str,
    limit: int,
) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, ticker, side, sector, signal_template, horizon_class,
               market_regime, open_date, close_date, holding_days, net_pnl,
               return_on_notional, outcome_label, lesson_text
        FROM trade_episode_memory
        WHERE config_id = ?
          AND (close_date IS NULL OR close_date <= ?)
        ORDER BY ABS(net_pnl) DESC, close_date DESC, created_at DESC
        LIMIT ?
        """,
        (config_id, trading_date, int(limit)),
    )
    return [dict(row) for row in cursor.fetchall()]


def write_exploratory_hypotheses(
    cursor: sqlite3.Cursor,
    *,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
) -> Dict[str, Any]:
    reviewer = _reviewer_helpers()
    learning_cfg = cfg.get("learning", {}) or {}
    research_cfg = learning_cfg.get("exploratory_research", {}) or {}
    if not bool(research_cfg.get("enabled", True)):
        return {"rows": 0, "status": "disabled"}
    episodes = _recent_trade_episodes_for_research(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        limit=int(research_cfg.get("max_episode_samples", 24) or 24),
    )
    min_episodes = int(research_cfg.get("min_episode_samples", 2) or 2)
    if len(episodes) < min_episodes:
        return {"rows": 0, "status": "insufficient_episode_samples", "episode_count": len(episodes)}

    prompt = (
        "You are the AgentQuant Researcher acting as a research memory curator. "
        "Study completed futures trade episodes and propose exploratory trading hypotheses. "
        "The goal is free exploration of commodity-specific trading rules, not rigid constraints. "
        "Do not recommend breaking hard controls: total deployed margin must stay <=20%, no lookahead, "
        "and LLM output is prompt prior only until future samples validate it. "
        "Prefer hypotheses scoped by ticker/sector/side/horizon/regime/indicator family. "
        "Return concise hypotheses with suggested_use such as analyst_prior, pm_prior, or probe_candidate. "
        "For each hypothesis, include entry_timing_hint, exit_timing_hint, holding_period_hint, "
        "invalidation_condition, and validation_plan. These fields are research guidance only; "
        "they must not be written as hard product bans, permanent blacklists, or unconditional sizing rules.\n"
        + reviewer._json_dumps({"trading_date": trading_date, "episodes": episodes})[:12000]
    )
    output = ExploratoryHypothesisLLMOutput()
    raw_response = ""
    if bool(research_cfg.get("use_llm", True)):
        try:
            from llm.inference import agent_call

            output = agent_call(
                prompt=prompt,
                llm_config=cfg.get("llm", {}),
                pydantic_model=ExploratoryHypothesisLLMOutput,
            )
            raw_response = reviewer._json_dumps(output.model_dump())
        except Exception as exc:
            raw_response = f"llm_exploratory_research_failed: {exc}"
            logger.warning(f"Researcher exploratory research failed on {trading_date}: {exc}")
    else:
        raw_response = "llm disabled; no exploratory hypotheses generated"

    note_id = str(uuid.uuid4())
    prompt_ext = externalize_text_for_db(
        prompt,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="raw_prompt",
        config_id=config_id,
        trading_date=trading_date,
    )
    response_ext = externalize_text_for_db(
        raw_response,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="raw_response",
        config_id=config_id,
        trading_date=trading_date,
    )
    evidence = {"agent_name": "researcher", "trading_date": trading_date, "episodes": episodes}
    payload_ext = externalize_json_for_db(
        evidence,
        category="reviewer_llm_notes",
        record_id=note_id,
        field_name="payload",
        config_id=config_id,
        trading_date=trading_date,
    )
    cursor.execute(
        """
        INSERT INTO reviewer_llm_notes (
            id, config_id, trading_date, evidence_pack_id, ticker,
            raw_prompt, raw_response, created_at, payload_json,
            raw_prompt_artifact_path, raw_prompt_sha256,
            raw_prompt_size, raw_prompt_summary_json,
            raw_response_artifact_path, raw_response_sha256,
            raw_response_size, raw_response_summary_json,
            payload_artifact_path, payload_sha256,
            payload_size, payload_summary_json
        ) VALUES (?, ?, ?, ?, '*', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id,
            config_id,
            trading_date,
            f"exploratory:{note_id}",
            prompt_ext.inline_value,
            response_ext.inline_value,
            reviewer._utc_now(),
            payload_ext.inline_value,
            prompt_ext.artifact_path,
            prompt_ext.sha256,
            prompt_ext.size_bytes,
            prompt_ext.summary_json,
            response_ext.artifact_path,
            response_ext.sha256,
            response_ext.size_bytes,
            response_ext.summary_json,
            payload_ext.artifact_path,
            payload_ext.sha256,
            payload_ext.size_bytes,
            payload_ext.summary_json,
        ),
    )

    valid_days = int(research_cfg.get("valid_days", learning_cfg.get("memory_expires_after_days", 30)) or 30)
    valid_until = reviewer._valid_until(trading_date, valid_days)
    now = reviewer._utc_now()
    max_hypotheses = int(research_cfg.get("max_hypotheses_per_day", 5) or 5)
    rows = 0
    for item in (output.hypotheses or [])[:max_hypotheses]:
        payload = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        text = str(payload.get("hypothesis_text") or "").strip()
        if not text:
            continue
        confidence = max(0.0, min(1.0, reviewer._safe_float(payload.get("confidence_score"), 0.0)))
        ticker = str(payload.get("ticker") or "*").upper()
        sector = str(payload.get("sector") or "*")
        side = str(payload.get("side") or "*").lower()
        horizon = str(payload.get("horizon_class") or "*")
        regime = str(payload.get("market_regime") or "*")
        suggested_use = str(payload.get("suggested_use") or "prompt prior only; validate with future samples")
        if "prior" not in suggested_use.lower():
            suggested_use = f"{suggested_use}; prompt prior only until validated"
        hypothesis_contract = build_next_round_memory_contract(
            memory_type="exploratory_hypothesis",
            maturity_state="candidate",
            scope={
                "ticker": ticker,
                "sector": sector,
                "side": side,
                "horizon_class": horizon,
                "market_regime": regime,
            },
            usable_memory=text,
            analysis_strategy_updates=[
                suggested_use,
                "Use this as a question to test against today's indicators, price stage, and analyst evidence.",
            ],
            trading_strategy_updates=[
                "Translate only into entry, exit, holding, or probe considerations after current confirmation.",
                "Do not use a candidate hypothesis to size up, add, position_match, or continue losing exposure.",
            ],
            validation_plan=[
                payload.get("validation_plan") or "Track same-scope future samples before promotion.",
            ],
            sample_count=len(episodes),
            confidence_score=confidence,
        )
        event_id = reviewer._insert_learning_event(
            cursor,
            config_id=config_id,
            trading_date=trading_date,
            event_type="exploratory_hypothesis",
            scope_type="research",
            scope_key=f"{ticker}:{sector}:{side}:{horizon}:{regime}",
            evidence={"episode_count": len(episodes), "note_id": note_id, "agent_name": "researcher"},
            action={
                "hypothesis_text": text,
                "suggested_use": suggested_use,
                "entry_timing_hint": payload.get("entry_timing_hint"),
                "exit_timing_hint": payload.get("exit_timing_hint"),
                "holding_period_hint": payload.get("holding_period_hint"),
                "invalidation_condition": payload.get("invalidation_condition"),
                "validation_plan": payload.get("validation_plan"),
                CONTRACT_KEY: hypothesis_contract,
            },
            status="candidate",
        )
        hypothesis_payload = {
            **payload,
            "agent_name": "researcher",
            "suggested_use": suggested_use,
            "source_note_id": note_id,
            "source_event_id": event_id,
            "hard_constraints": {
                "max_total_margin_ratio": cfg.get("max_total_margin_ratio", 0.20),
                "prompt_prior_only": True,
                "candidate_hypothesis_cannot_control_position": True,
            },
            CONTRACT_KEY: hypothesis_contract,
        }
        hypothesis_id = str(uuid.uuid4())
        hypothesis_ext = externalize_json_for_db(
            hypothesis_payload,
            category="exploratory_hypothesis",
            record_id=hypothesis_id,
            field_name="payload",
            config_id=config_id,
            trading_date=trading_date,
        )
        cursor.execute(
            """
            INSERT INTO exploratory_hypothesis (
                id, config_id, trading_date, scope_type, scope_key, ticker, sector,
                side, horizon_class, market_regime, hypothesis_text,
                evidence_summary, suggested_use, confidence_score, sample_count,
                status, created_at, valid_until, payload_json,
                payload_artifact_path, payload_sha256, payload_size, payload_summary_json
            ) VALUES (?, ?, ?, 'research', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hypothesis_id,
                config_id,
                trading_date,
                f"{ticker}:{sector}:{side}:{horizon}:{regime}",
                ticker,
                sector,
                side,
                horizon,
                regime,
                text,
                str(payload.get("evidence_summary") or ""),
                suggested_use,
                confidence,
                len(episodes),
                now,
                valid_until,
                hypothesis_ext.inline_value,
                hypothesis_ext.artifact_path,
                hypothesis_ext.sha256,
                hypothesis_ext.size_bytes,
                hypothesis_ext.summary_json,
            ),
        )
        rows += 1
    return {"rows": rows, "status": "applied" if rows else "no_hypotheses", "episode_count": len(episodes)}


def apply_researcher_learning(
    *,
    db: Any,
    cursor: sqlite3.Cursor,
    cfg: Dict[str, Any],
    config_id: str,
    trading_date: str,
    settlement_row: Optional[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    strategy_recommendations: List[Dict[str, Any]],
    no_trade_reason_counter: Counter,
) -> Dict[str, Any]:
    """Persist Phase4 learning after reviewer validation passes."""
    reviewer = _reviewer_helpers()
    cursor.execute("PRAGMA foreign_keys = ON")
    if hasattr(db, "_ensure_reviewer_learning_schema"):
        db._ensure_reviewer_learning_schema(cursor)

    context_rows = reviewer._write_signal_context_history(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        recommendations=strategy_recommendations,
    )
    memory_rows = reviewer._write_strategy_memory_history(
        cursor,
        db=db,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    perf_counts = reviewer._write_template_and_analyst_learning(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    episode_rows = reviewer._write_trade_episode_memory(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    no_trade_memory_rows = reviewer._write_no_trade_opportunity_memory(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
    )
    no_trade_shadow_backfill = reviewer._backfill_no_trade_opportunity_shadow_results(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    adaptive_rows = reviewer._write_adaptive_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    tail_loss_sentinel_rows = reviewer._write_tail_loss_sentinel_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    alpha_promotion_rows = reviewer._write_alpha_promotion_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    contextual_rule_calibration_rows = reviewer._write_contextual_rule_calibration_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    loss_template_observation_rows = reviewer._write_loss_template_observation_research(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    learned_benchmark_policy = reviewer._write_learned_vs_unlearned_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    provisional_rows = reviewer._write_provisional_policy_state(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
    )
    overlay_rows = reviewer._write_config_overlay(
        cursor,
        config_id=config_id,
        trading_date=trading_date,
        cfg=cfg,
        settlement_row=settlement_row,
    )
    neutral_accountability = reviewer._write_neutral_accountability_state(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        strategy_recommendations=strategy_recommendations,
    )
    neutral_forward_shadow_backfill = reviewer._backfill_neutral_forward_shadow_tracking(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    capital_state = reviewer._write_capital_deployment_state(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    template_prior_path = reviewer._export_template_prior(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    causal_review_candidates = run_researcher_causal_review(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
        settlement_row=settlement_row,
        strategy_recommendations=strategy_recommendations,
        no_trade_reason_counter=no_trade_reason_counter,
    )
    causal_rule_validation = reviewer._write_validated_causal_policy_rules(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    exploratory_hypotheses = write_exploratory_hypotheses(
        cursor,
        cfg=cfg,
        config_id=config_id,
        trading_date=trading_date,
    )
    return {
        "researcher_agent": "researcher",
        "signal_context_rows": context_rows,
        "strategy_memory_history_rows": memory_rows,
        **perf_counts,
        "trade_episode_rows": episode_rows,
        "no_trade_opportunity_rows": no_trade_memory_rows,
        "no_trade_shadow_backfill": no_trade_shadow_backfill,
        "adaptive_policy_rows": adaptive_rows,
        "tail_loss_sentinel_rows": tail_loss_sentinel_rows,
        "alpha_promotion_rows": alpha_promotion_rows,
        "contextual_rule_calibration_rows": contextual_rule_calibration_rows,
        "loss_template_observation_rows": loss_template_observation_rows,
        "learned_vs_unlearned_policy": learned_benchmark_policy,
        "provisional_policy_rows": provisional_rows,
        "config_overlay_rows": overlay_rows,
        "neutral_accountability": {
            "neutral_ratio": neutral_accountability.get("neutral_ratio", 0.0),
            "accountability_complete_rate": neutral_accountability.get("accountability_complete_rate", 1.0),
            "category_counts": neutral_accountability.get("category_counts", {}),
            "structured_learning_rows": neutral_accountability.get("structured_learning_rows", 0),
            "forward_shadow_backfill": neutral_forward_shadow_backfill,
        },
        "capital_deployment_state": capital_state,
        "template_prior_path": template_prior_path,
        "causal_review_candidates": causal_review_candidates,
        "validated_causal_rules": causal_rule_validation.get("validated_rules", 0),
        "causal_rule_validation_status_counts": causal_rule_validation.get("status_counts", {}),
        "exploratory_hypotheses": exploratory_hypotheses,
    }
