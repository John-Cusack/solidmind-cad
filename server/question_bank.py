from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from server.constants import MATURITY_LEVELS, SUPPORTED_PROCESSES
from server.paths import data_path


class QuestionBankError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    process: str
    field_paths: tuple[str, ...]
    required: dict[str, bool]
    weight: dict[str, float]
    priority: int
    technical: str
    plain: str
    examples: tuple[str, ...]
    why: str
    depends_on: tuple[str, ...]

    def required_for(self, maturity_level: str) -> bool:
        return bool(self.required.get(maturity_level, False))

    def weight_for(self, maturity_level: str) -> float:
        w = self.weight.get(maturity_level, 0.0)
        try:
            return float(w)
        except (TypeError, ValueError):
            return 0.0


@dataclass(frozen=True, slots=True)
class QuestionBank:
    question_bank_id: str
    questions: tuple[Question, ...]

    def by_id(self) -> dict[str, Question]:
        return {q.id: q for q in self.questions}


def _require_str(d: dict[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise QuestionBankError(f"Question bank field '{key}' must be a non-empty string")
    return v


def _require_list_of_str(d: dict[str, Any], key: str) -> tuple[str, ...]:
    v = d.get(key)
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise QuestionBankError(f"Question bank field '{key}' must be a list of strings")
    return tuple(v)


def _require_required_map(d: dict[str, Any]) -> dict[str, bool]:
    req = d.get("required")
    if not isinstance(req, dict):
        raise QuestionBankError("Question 'required' must be a mapping")
    out: dict[str, bool] = {}
    for ml in MATURITY_LEVELS:
        out[ml] = bool(req.get(ml, False))
    return out


def _require_weight_map(d: dict[str, Any]) -> dict[str, float]:
    w = d.get("weight")
    if not isinstance(w, dict):
        raise QuestionBankError("Question 'weight' must be a mapping")
    out: dict[str, float] = {}
    for ml in MATURITY_LEVELS:
        try:
            out[ml] = float(w.get(ml, 0.0))
        except (TypeError, ValueError):
            out[ml] = 0.0
    return out


def _require_int(d: dict[str, Any], key: str, default: int = 0) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise QuestionBankError(f"Question bank field '{key}' must be an int")
    return v


@lru_cache(maxsize=8)
def load_question_bank(process: str) -> QuestionBank:
    if process not in SUPPORTED_PROCESSES:
        raise QuestionBankError(f"Unsupported process: {process}")

    path = data_path("question_bank", f"{process}.yml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise QuestionBankError("Question bank YAML must be a mapping")

    qb_id = raw.get("question_bank_id") or f"{process}_v1"
    if not isinstance(qb_id, str):
        raise QuestionBankError("question_bank_id must be a string")

    raw_questions = raw.get("questions")
    if not isinstance(raw_questions, list):
        raise QuestionBankError("questions must be a list")

    questions: list[Question] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            raise QuestionBankError("Each question must be a mapping")
        q = Question(
            id=_require_str(item, "id"),
            process=_require_str(item, "process"),
            field_paths=_require_list_of_str(item, "field_paths"),
            required=_require_required_map(item),
            weight=_require_weight_map(item),
            priority=_require_int(item, "priority", default=0),
            technical=_require_str(item, "technical"),
            plain=_require_str(item, "plain"),
            examples=_require_list_of_str(item, "examples"),
            why=_require_str(item, "why"),
            depends_on=_require_list_of_str(item, "depends_on"),
        )
        if q.process != process:
            raise QuestionBankError(f"Question {q.id} has mismatched process: {q.process}")
        questions.append(q)

    return QuestionBank(question_bank_id=qb_id, questions=tuple(questions))


def compute_coverage(spec_draft: dict, question_bank: QuestionBank, maturity_level: str) -> float:
    answered = spec_draft.get("_interview", {}).get("answered", {})
    if not isinstance(answered, dict):
        answered = {}

    total = 0.0
    done = 0.0
    for q in question_bank.questions:
        w = q.weight_for(maturity_level)
        if w <= 0:
            continue
        total += w
        if q.id in answered:
            done += w

    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, done / total))
