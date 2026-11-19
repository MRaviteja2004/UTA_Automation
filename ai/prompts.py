"""Prompts for classifying source requirements.

The prompt asks the model to label rows that were already extracted. It does
not ask the model to draft new clinical rules.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """You classify clinical EDC requirements that were copied from a study workbook.

Rules you must follow:
- Use only the source rows in the user message.
- Do not add a requirement that is not written in those rows.
- Do not change numbers, dates, field names, or allowed values.
- Do not treat a positive or negative example as a limit unless that same limit is written in the condition or expected behavior.
- Copy source_sheet and source_row exactly from the source row you are classifying.
- If a sentence states more than one constraint, return one JSON object per constraint, all citing that same source row.
- If a row is study metadata, a purpose statement, or a note that wording must be confirmed, return no object for that row.
- If the wording is ambiguous or could be read in more than one way, set needs_human_review to true and explain why in ambiguity_notes.
- You do not approve requirements. Do not include an approval field.
- Return a single JSON object and no markdown.

Allowed rule_type values:
- mandatory: the source says a field must be present, or a specific value is required.
- range: the source states a numeric lower bound, upper bound, or both.
- allowed_values: the source lists the only acceptable values.
- conditional: the source says the check applies only in a stated situation, such as "if provided" or "before screening can proceed".
- cross_field: the source compares or constrains two fields.
- date: the source requires a date or constrains a date without naming a second field. If it compares two dates, use cross_field.
- uniqueness: the source says a value must be unique.
- other: the source states a data constraint that does not fit the types above.

Parameter rules:
- minimum and maximum are JSON numbers only when that number is written in the condition or expected behavior. Otherwise use null.
- minimum_inclusive and maximum_inclusive are true when the source says inclusive, "from ... through", or "on or after". Use false for "greater than" or "after" when "on or after" is not used.
- allowed_values contains only values written in the condition or expected behavior.
- related_field is the other field named in a cross-field statement, or null.
- confidence is a number from 0 to 1, not a percentage.
"""


def build_user_prompt(sources: list[dict[str, Any]]) -> str:
    """Build the user message from original source fields."""

    payload = {
        "task": (
            "Classify each source row that states a data-entry constraint. "
            "Omit rows that do not."
        ),
        "response_schema": {
            "requirements": [
                {
                    "source_sheet": "sheet name copied from the source",
                    "source_row": 1,
                    "field_name": "field name copied from the source",
                    "rule_type": "mandatory | range | allowed_values | conditional | cross_field | date | uniqueness | other",
                    "parameters": {
                        "minimum": None,
                        "maximum": None,
                        "minimum_inclusive": None,
                        "maximum_inclusive": None,
                        "allowed_values": None,
                        "related_field": None,
                    },
                    "confidence": 0.0,
                    "needs_human_review": False,
                    "ambiguity_notes": None,
                }
            ]
        },
        "sources": sources,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
