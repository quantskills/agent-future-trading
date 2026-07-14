from __future__ import annotations

"""Registered output schemas shared by Protocol Governor checks."""

from dataclasses import dataclass, field
from typing import Iterable, List


PG_CONTRACT_VERSION = "agentquant.protocol_governor.v1"
PG_SOURCE_AGENT = "protocol_governor"
PG_PASSED = "passed"
PG_FAILED = "failed"
PG_SKIPPED = "skipped"


def _stable_codes(values: Iterable[str] | None) -> List[str]:
    return list(dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip()))


@dataclass(frozen=True)
class ProtocolCheckResult:
    """One registered PG check result.

    The serialized form intentionally contains no catch-all metadata, payload,
    warning, or error containers. Every result uses only fields registered in
    ``matrix_field_semantics.md``.
    """

    check_name: str
    status: str = PG_PASSED
    violation_codes: List[str] = field(default_factory=list)
    diagnostic_codes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in {PG_PASSED, PG_FAILED, PG_SKIPPED}:
            raise ValueError(f"invalid_protocol_check_status:{self.status}")
        object.__setattr__(self, "violation_codes", _stable_codes(self.violation_codes))
        object.__setattr__(self, "diagnostic_codes", _stable_codes(self.diagnostic_codes))
        if self.status == PG_PASSED and self.violation_codes:
            raise ValueError("passed_protocol_check_cannot_have_violation_codes")
        if self.status == PG_FAILED and not self.violation_codes:
            raise ValueError("failed_protocol_check_requires_violation_codes")

    @property
    def passed(self) -> bool:
        return self.status in {PG_PASSED, PG_SKIPPED}

    @classmethod
    def pass_result(
        cls,
        check_name: str,
        *,
        diagnostic_codes: Iterable[str] | None = None,
    ) -> "ProtocolCheckResult":
        return cls(
            check_name=check_name,
            status=PG_PASSED,
            diagnostic_codes=_stable_codes(diagnostic_codes),
        )

    @classmethod
    def fail_result(
        cls,
        check_name: str,
        violation_codes: Iterable[str],
        *,
        diagnostic_codes: Iterable[str] | None = None,
    ) -> "ProtocolCheckResult":
        return cls(
            check_name=check_name,
            status=PG_FAILED,
            violation_codes=_stable_codes(violation_codes),
            diagnostic_codes=_stable_codes(diagnostic_codes),
        )

    @classmethod
    def skipped_result(
        cls,
        check_name: str,
        *,
        diagnostic_codes: Iterable[str] | None = None,
    ) -> "ProtocolCheckResult":
        return cls(
            check_name=check_name,
            status=PG_SKIPPED,
            diagnostic_codes=_stable_codes(diagnostic_codes),
        )

    def to_dict(self) -> dict:
        return {
            "check_name": self.check_name,
            "status": self.status,
            "violation_codes": list(self.violation_codes),
            "diagnostic_codes": list(self.diagnostic_codes),
        }


@dataclass(frozen=True)
class ProtocolGovernorReport:
    checks: List[ProtocolCheckResult]
    contract_version: str = PG_CONTRACT_VERSION
    source_agent: str = PG_SOURCE_AGENT

    @property
    def status(self) -> str:
        return PG_PASSED if all(check.passed for check in self.checks) else PG_FAILED

    @property
    def passed(self) -> bool:
        return self.status == PG_PASSED

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version,
            "source_agent": self.source_agent,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }
