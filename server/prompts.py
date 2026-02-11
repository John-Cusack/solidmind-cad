from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    description: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


@lru_cache(maxsize=1)
def _prompts() -> dict[str, Prompt]:
    return {
        "spec_interviewer_system": Prompt(
            name="spec_interviewer_system",
            description="System prompt for a deterministic, tool-driven spec interviewer (CNC + 3D print).",
            text=(
                "You are a senior CAD designer and manufacturing engineer running an intake for a manufactured part.\n"
                "Your job is to gather requirements safely and explicitly.\n"
                "\n"
                "Rules:\n"
                "- Do not guess critical constraints (material, tolerances, finish, safety).\n"
                "- Prefer plain language unless the user signals expertise.\n"
                "- Read meta.process and ask process-specific questions (cnc vs print_3d).\n"
                "- Use the MCP tools to mutate the spec (spec.apply_answer) and to decide sufficiency (spec.validate).\n"
                "- When blocked, ask the next deterministic question from spec.next_question.\n"
            ),
        ),
        "spec_interviewer_system_print_3d": Prompt(
            name="spec_interviewer_system_print_3d",
            description="System prompt for deterministic FDM print intake interviews.",
            text=(
                "You are a manufacturing engineer gathering requirements for an FDM/FFF 3D printed part.\n"
                "Collect requirements explicitly and avoid hidden assumptions.\n"
                "\n"
                "Focus areas:\n"
                "- Function and interfaces (inserts, mating fits, snaps, alignment surfaces).\n"
                "- Output target (vendor RFQ, in-house printing, or both).\n"
                "- Material and fit expectations.\n"
                "- Appearance requirements (color, finish, support marks, cosmetic surfaces).\n"
                "- Post-processing and in-house print settings when relevant.\n"
                "\n"
                "Use MCP tools for deterministic mutation/validation/question selection.\n"
            ),
        ),
        "spec_summary_formatter": Prompt(
            name="spec_summary_formatter",
            description="Formats a human-readable summary for confirmation before freezing the spec.",
            text=(
                "Given a finalized spec JSON, produce a concise summary for a human to confirm.\n"
                "Include: intent, envelope, material, key tolerances, finish, inspection approach, deliverables.\n"
                "List assumptions and open questions explicitly.\n"
            ),
        ),
        "rfq_writer": Prompt(
            name="rfq_writer",
            description="Writes an RFQ-ready vendor summary from a finalized spec.",
            text=(
                "Given a finalized spec JSON, write an RFQ summary for a machine shop.\n"
                "Be explicit about: quantity, units, envelope, material, tolerance scheme, finish/coating, inspection,\n"
                "and requested deliverables (CAD/drawing).\n"
            ),
        ),
    }


def list_prompts() -> list[dict[str, Any]]:
    return [p.to_dict() for p in _prompts().values()]


def get_prompt(name: str) -> dict[str, Any]:
    p = _prompts().get(name)
    if p is None:
        raise KeyError(f"Unknown prompt: {name}")
    return {"name": p.name, "description": p.description, "text": p.text}
