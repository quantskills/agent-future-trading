"""Role-specific structured output models for the three analyst LLM calls."""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from graph.constants import Signal
from graph.schema import AnalystSignal
from tools.agent_tools.analysis.analyst_quality import (
    has_analyst_invalidation_boundary,
)
from tools.common.signal_evidence_collection import has_concrete_entry_trigger


ANALYST_EXECUTION_PROFILE_MISSING = "analyst_execution_profile_missing"
_EXECUTABLE_TECHNICAL_STATES = frozenset(
    {"watch_for_trigger", "probe_candidate", "tradeable_candidate"}
)
_EXECUTABLE_NEWS_STATES = frozenset({"probe_candidate", "tradeable_candidate"})
_TECHNICAL_PROFILES = frozenset({"breakout", "pullback", "vwap_confirmed"})
_NEWS_PROFILES = frozenset({"event_immediate"})


def _profile_contract_error() -> PydanticCustomError:
    return PydanticCustomError(
        ANALYST_EXECUTION_PROFILE_MISSING,
        ANALYST_EXECUTION_PROFILE_MISSING,
    )


def _has_direction(signal: AnalystSignal) -> bool:
    value = getattr(signal.signal, "value", signal.signal)
    if str(value) in {Signal.BULLISH.value, Signal.BEARISH.value}:
        return True
    return str(getattr(signal, "counterfactual_side", "") or "").strip().lower() in {
        "long",
        "short",
    }


def _declares_complete_executable_setup(
    signal: AnalystSignal,
    *,
    executable_states: frozenset[str],
) -> bool:
    state = str(getattr(signal, "opportunity_state", "") or "").strip().lower()
    return bool(
        state in executable_states
        and _has_direction(signal)
        and has_concrete_entry_trigger(getattr(signal, "entry_trigger", ""))
        and has_analyst_invalidation_boundary(signal)
    )


class TechnicalAnalystOutput(AnalystSignal):
    """Technical output: empty for no-opportunity, otherwise one Trader profile."""

    entry_timing_signal: str = Field(
        default="",
        description=(
            "Required for a complete technical watch/probe/tradeable setup; "
            "one of breakout, pullback, vwap_confirmed, or empty for no-opportunity"
        ),
        json_schema_extra={"enum": ["", "breakout", "pullback", "vwap_confirmed"]},
    )

    @model_validator(mode="after")
    def validate_execution_profile(self):
        profile = str(self.entry_timing_signal or "").strip().lower()
        state = str(self.opportunity_state or "").strip().lower()
        if profile and profile not in _TECHNICAL_PROFILES:
            raise _profile_contract_error()
        if profile and state not in _EXECUTABLE_TECHNICAL_STATES | {
            "risk_reduction_candidate"
        }:
            raise _profile_contract_error()
        if _declares_complete_executable_setup(
            self,
            executable_states=_EXECUTABLE_TECHNICAL_STATES,
        ) and not profile:
            raise _profile_contract_error()
        self.entry_timing_signal = profile
        return self


class FundamentalAnalystOutput(AnalystSignal):
    """Fundamental output supplies direction context and no Trader profile."""

    entry_timing_signal: str = Field(
        default="",
        description="Fundamental has no Trader execution profile and must return an empty string",
        json_schema_extra={"enum": [""]},
    )

    @model_validator(mode="after")
    def reject_execution_profile(self):
        if str(self.entry_timing_signal or "").strip():
            raise ValueError("fundamental_execution_profile_forbidden")
        self.entry_timing_signal = ""
        return self


class CommodityNewsAnalystOutput(AnalystSignal):
    """News output may execute only an already-confirmed immediate event."""

    entry_timing_signal: str = Field(
        default="",
        description=(
            "event_immediate for a complete current event candidate, otherwise empty"
        ),
        json_schema_extra={"enum": ["", "event_immediate"]},
    )

    @model_validator(mode="after")
    def validate_execution_profile(self):
        profile = str(self.entry_timing_signal or "").strip().lower()
        state = str(self.opportunity_state or "").strip().lower()
        if profile and profile not in _NEWS_PROFILES:
            raise _profile_contract_error()
        if profile and state not in _EXECUTABLE_NEWS_STATES | {
            "risk_reduction_candidate"
        }:
            raise _profile_contract_error()
        if _declares_complete_executable_setup(
            self,
            executable_states=_EXECUTABLE_NEWS_STATES,
        ) and not profile:
            raise _profile_contract_error()
        self.entry_timing_signal = profile
        return self
