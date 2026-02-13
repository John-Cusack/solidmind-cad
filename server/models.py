from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any, Literal


class Severity(str, Enum):
    BLOCK = "block"
    WARN = "warn"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    severity: Severity
    message: str
    field: str | None = None  # JSON Pointer (RFC 6901)
    question_id: str | None = None
    priority: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "field": self.field,
            "question_id": self.question_id,
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "details": self.details,
        }


UserExpertise = Literal["novice", "intermediate", "expert", "unknown"]
LanguagePreference = Literal["plain", "technical", "auto"]


@dataclass(frozen=True, slots=True)
class ConversationSignals:
    user_expertise: UserExpertise = "unknown"
    language_preference: LanguagePreference = "auto"
    previous_question_id: str | None = None
    allow_revisit_skipped: bool = False


@dataclass(frozen=True, slots=True)
class ValidatorResult:
    """Return type for individual validator functions."""
    name: str
    status: str          # "pass", "warn", "fail"
    message: str
    measured: dict[str, Any] = dc_field(default_factory=dict)
    priority: int = 500


@dataclass(frozen=True, slots=True)
class ValidatorInfo:
    """Metadata about a validator, exposed via me.list_validators."""
    name: str
    description: str
    reads: tuple[str, ...]      # field paths this validator reads
    thresholds: dict[str, Any]  # threshold name → default description
    priority: int

