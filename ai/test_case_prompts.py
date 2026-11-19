"""Prompts for proposing test cases from analyzed requirements.

The model may only use the requirements in the user message.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """You propose synthetic EDC test cases for requirements that were already analyzed.

Rules you must follow:
- Use only the requirement IDs, fields, limits, allowed values, and conditions in the user message.
- Do not add a clinical requirement, limit, allowed value, or expected clinical outcome.
- Every test case cites one requirement_id copied exactly from the input.
- expected_result must be exactly one of: Accept, Reject, Not applicable.
- input_data uses the requirement field name. Values are synthetic.
- Do not copy a positive or negative example as if it were the only legal value.
- Do not include an approval field. You do not approve test cases.
- Return one JSON object and no markdown.

Categories and what to return:
- positive: a value inside a stated inclusive range, above an exclusive minimum, below an exclusive maximum, or one stated allowed value. expected_result is Accept.
- negative: a value outside the stated range, or a synthetic value that is not in the allowed list. expected_result is Reject.
- boundary: only for whole-number bounds whose inclusivity is stated. Inclusive minimum M uses M (Accept) and M-1 (Reject). Inclusive maximum M uses M (Accept) and M+1 (Reject). Exclusive minimum M uses M (Reject) and M+1 (Accept). Exclusive maximum M uses M (Reject) and M-1 (Accept). If inclusivity is null, or the bound is not a whole number, do not invent a neighbor.
- conditional: two cases, with input_data.condition_met true and false. When the source says the check applies only if a value is provided, the false case uses a blank field and expected_result Not applicable.
- cross_field: two cases that include both the requirement field and related_field. One combination should Accept and one should Reject, following the source wording such as "on or after".
- mandatory: the required field is null. expected_result is Reject.
- allowed_value: one case whose value is in allowed_values (Accept) and one synthetic value that is not in that list (Reject). The invalid value is not a new allowed value.
- date: a blank date (Reject) and one synthetic ISO date YYYY-MM-DD (Accept) when the source only says the date is required. For a comparison, follow the cross_field rules and use category date.

If the source requirement is marked ambiguous, still propose cases only from stated values and set needs_human_review to true.
"""


def build_user_prompt(
    requirements: list[dict[str, Any]],
    form_questions: list[dict[str, Any]] | None = None,
    form_structure_rules: list[str] | None = None,
) -> str:
    """Build the user message from analyzed requirements."""

    payload: dict[str, Any] = {
        "task": "Propose synthetic test cases for these analyzed requirements only.",
        "response_schema": {
            "test_cases": [
                {
                    "requirement_id": "requirement id copied from the input",
                    "title": "short synthetic case title",
                    "description": "what the synthetic input checks",
                    "category": "positive | negative | boundary | conditional | cross_field | mandatory | allowed_value | date",
                    "input_data": {"Field name": "synthetic value"},
                    "expected_result": "Accept | Reject | Not applicable",
                    "rationale": "why this input follows the stated requirement",
                    "confidence": 0.0,
                    "needs_human_review": False,
                }
            ]
        },
        "requirements": requirements,
    }
    if form_questions is not None:
        payload["form_questions"] = form_questions
        payload["form_structure_rules"] = list(form_structure_rules or [])
    return json.dumps(payload, ensure_ascii=False, indent=2)
