"""Prompts for supporting values on required fields a case does not test.

The model may only use the requirements and required fields in the user message.
It does not create the value under test.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """You propose synthetic supporting values for required form fields that a test case does not already contain.

Rules you must follow:
- Do not change, replace, or repeat a field that is already in input_data. This includes a field whose value is null.
- Propose values only for the required form fields named in the user message.
- Every supporting value cites one or more requirement_id values copied exactly from the requirements list in this request.
- Do not cite a requirement that is not included in this request.
- Cite only a requirement whose field is the field you are filling.
- Use a value that requirement permits. Do not invent a clinical requirement, limit, allowed value, or consent state.
- Do not assume informed consent is Yes. Use Yes only when the cited requirement states that Yes is the valid consent value.
- For Age, use a number inside the bounds stated by the cited requirement, including whether each bound is inclusive. Do not invent a bound.
- For Gender, use only an allowed value stated by the cited requirement. Do not invent an allowed value.
- Do not fill a field that the test case leaves blank so the expected result is Reject. That includes a mandatory or negative case.
- Do not propose a supporting value that would change the test case's expected result.
- A supporting value cannot be null or blank.
- Subject IDs must be unique across the test cases in this request and must not reuse any value in subject_ids_already_used.
- Dates use YYYY-MM-DD.
- If a required field cannot be justified from the listed requirements, omit it.
- Do not include an approval field. You do not approve test cases.
- Return one JSON object and no markdown.
"""


def build_user_prompt(
    cases: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    required_fields: list[str],
    form_questions: list[dict[str, Any]] | None = None,
    form_structure_rules: list[str] | None = None,
    used_subject_ids: list[str] | None = None,
) -> str:
    """Build the user message for one supporting-data request."""

    payload: dict[str, Any] = {
        "task": (
            "Propose supporting values only for required form fields that "
            "are absent from each test case."
        ),
        "required_form_fields": required_fields,
        "response_schema": {
            "supporting_data": [
                {
                    "test_case_id": "test case id copied from the input",
                    "supporting_values": {"Required field": "synthetic value"},
                    "supporting_requirement_ids": {"Required field": ["requirement id"]},
                }
            ]
        },
        "requirements": requirements,
        "test_cases": cases,
    }
    if form_questions is not None:
        payload["form_questions"] = form_questions
        payload["form_structure_rules"] = list(form_structure_rules or [])
    if used_subject_ids:
        payload["subject_ids_already_used"] = list(used_subject_ids)
        payload["subject_id_rule"] = (
            "Each proposed Subject ID must differ from every value in subject_ids_already_used "
            "and from the other proposals in this response."
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)
