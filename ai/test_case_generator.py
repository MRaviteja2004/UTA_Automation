"""Generate synthetic test cases from analyzed requirements.

The gateway client proposes cases. This module keeps only cases that cite a
real requirement, copies the Excel source from that requirement, and flags
inputs that the requirement does not support.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai.client import (
    InvalidAIResponseError,
    OpenAIRequirementClient,
    RequirementAIClient,
    load_ai_settings,
)
from ai.form_manifest import (
    CHOICE_QUESTION_TYPES,
    FORM_STRUCTURE_RULES,
    QuestionBinding,
    bind_field,
    generation_manifest,
    is_placeholder_option,
    match_key,
    require_captured_metadata,
)
from ai.test_case_prompts import SYSTEM_PROMPT, build_user_prompt
from models.analysis import AnalyzedRequirement, RequirementAnalysis, RequirementParameters
from models.form_metadata import GoogleFormMetadata
from models.test_cases import GeneratedTestCase, GenerationMetadata, TestSuite

CATEGORIES = (
    "positive",
    "negative",
    "boundary",
    "conditional",
    "cross_field",
    "mandatory",
    "allowed_value",
    "date",
)
_CATEGORY_ALIASES = {
    "allowedvalues": "allowed_value",
    "allowed_values": "allowed_value",
    "crossfield": "cross_field",
    "cross_fields": "cross_field",
}
_RESULT_ALIASES = {
    "accept": "Accept",
    "accepted": "Accept",
    "valid": "Accept",
    "reject": "Reject",
    "rejected": "Reject",
    "invalid": "Reject",
    "not applicable": "Not applicable",
    "not_applicable": "Not applicable",
    "n/a": "Not applicable",
}
_REVIEW_CONFIDENCE = 0.8
InputValue = str | int | float | bool | None


class _ProposedCase(BaseModel):
    """Fields accepted from the model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    requirement_id: str = Field(min_length=1)
    requirement_ids: list[str] | None = None
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    category: str
    input_data: dict[str, InputValue]
    expected_result: str = Field(min_length=1)
    rationale: str = ""
    confidence: float = Field(ge=0, le=1)
    needs_human_review: bool = False

    @field_validator("requirement_ids", mode="before")
    @classmethod
    def empty_ids(cls, value: Any) -> Any:
        if value is None:
            return None
        return value

    @field_validator("category", mode="before")
    @classmethod
    def known_category(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a test category")
        key = value.strip().casefold().replace("-", "_").replace(" ", "_")
        key = _CATEGORY_ALIASES.get(key, key)
        if key not in CATEGORIES:
            raise ValueError(f"unknown test category '{value}'")
        return key

    @field_validator("confidence", mode="before")
    @classmethod
    def numeric_confidence(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number from 0 to 1")
        return float(value)

    @field_validator("input_data", mode="before")
    @classmethod
    def input_object(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("must be an object of field values")
        problem = _nonscalar_input_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


def generate_test_cases(
    analysis: RequirementAnalysis,
    *,
    client: RequirementAIClient | None = None,
    form_metadata: GoogleFormMetadata | None = None,
) -> TestSuite:
    """Propose and validate synthetic test cases for analyzed requirements.

    Pass a client in tests. Otherwise the existing GenAI gateway client is used.
    When form metadata is omitted, generation uses the analyzed requirements
    alone. A captured snapshot limits cases to questions the form can represent.
    """

    requirements = list(analysis.requirements)
    if not requirements:
        return _empty_suite(
            analysis.source_file,
            None,
            ["No analyzed requirements were available for test generation."],
        )

    bindings: dict[str, QuestionBinding] = {}
    related_bindings: dict[str, QuestionBinding] = {}
    form_questions: list[dict[str, Any]] | None = None
    form_hash: str | None = None
    prompt_requirements = requirements
    warnings: list[str] = []
    if form_metadata is not None:
        require_captured_metadata(form_metadata)
        form_hash = form_metadata.metadata_hash
        prompt_requirements = []
        for row in requirements:
            binding = bind_field(row.field_name, form_metadata.questions)
            related = _related_binding(row, form_metadata.questions)
            if binding.status != "matched":
                _append_unique(warnings, _unrepresentable_warning(row, binding.reason))
                continue
            if related is not None and related.status != "matched":
                _append_unique(
                    warnings,
                    _unrepresentable_warning(
                        row,
                        f"related field '{row.parameters.related_field}' {related.reason}",
                    ),
                )
                continue
            bindings[row.requirement_id] = binding
            if related is not None:
                related_bindings[row.requirement_id] = related
            prompt_requirements.append(row)
            if binding.required_detection != "detected":
                _append_unique(
                    warnings,
                    f"Form question '{binding.title}' has no detected required status. "
                    "That was not treated as optional.",
                )
        form_questions = generation_manifest(form_metadata.questions)
        if not prompt_requirements:
            return _empty_suite(
                analysis.source_file,
                None,
                warnings,
                requirement_count=len(requirements),
                form_metadata_hash=form_hash,
            )

    model_name: str | None = None
    if client is None:
        settings = load_ai_settings()
        client = OpenAIRequirementClient(settings)
        model_name = settings.model

    raw_response = client.complete(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(
            [
                _requirement_payload(
                    row,
                    bindings.get(row.requirement_id),
                    related_bindings.get(row.requirement_id),
                )
                for row in prompt_requirements
            ],
            form_questions,
            FORM_STRUCTURE_RULES if form_questions is not None else None,
        ),
    )
    proposed, skipped = _parse_response(raw_response)
    warnings.extend(skipped)
    by_id = {row.requirement_id: row for row in requirements}
    allowed_ids = {row.requirement_id for row in prompt_requirements}
    accepted: list[GeneratedTestCase] = []
    rejected = len(skipped)
    seen: set[tuple[str, ...]] = set()
    sequence: dict[str, int] = defaultdict(int)

    for item in proposed:
        linked = _linked_ids(item)
        missing = [requirement_id for requirement_id in linked if requirement_id not in by_id]
        if missing:
            rejected += 1
            warnings.append(
                "Rejected a test case that cites unknown requirement id(s): "
                + ", ".join(missing)
                + "."
            )
            continue
        if item.requirement_id not in allowed_ids:
            rejected += 1
            warnings.append(
                f"Requirement {item.requirement_id} cannot be represented by the form. "
                "No test case was generated."
            )
            continue
        requirement = by_id[item.requirement_id]
        binding = bindings.get(item.requirement_id)
        if binding is not None:
            item = _align_proposal(
                item,
                [binding.title, related_bindings[item.requirement_id].title]
                if item.requirement_id in related_bindings
                else [binding.title],
            )
            form_problem = _form_value_problem(item, requirement, binding)
            if form_problem is not None:
                rejected += 1
                warnings.append(
                    f"Requirement {item.requirement_id}: {form_problem} "
                    "No test case was generated."
                )
                continue
        duplicate_key = _duplicate_key(item)
        if duplicate_key in seen:
            warnings.append(
                f"Duplicate test case for {item.requirement_id} ({item.category}) was ignored."
            )
            continue
        seen.add(duplicate_key)
        sequence[item.requirement_id] += 1
        accepted.append(
            _build_case(
                requirement,
                item,
                linked,
                sequence[item.requirement_id],
                analysis.source_file,
                binding,
            )
        )

    _warn_missing_branches(accepted, prompt_requirements, warnings)
    metadata = GenerationMetadata(
        source_file=analysis.source_file,
        model=model_name,
        requirement_count=len(requirements),
        test_case_count=len(accepted),
        rejected_count=rejected,
        category_counts=_category_counts(accepted),
        warnings=warnings,
        form_metadata_hash=form_hash,
    )
    return TestSuite(test_cases=accepted, metadata=metadata)


def _parse_response(raw: str) -> tuple[list[_ProposedCase], list[str]]:
    try:
        payload = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise InvalidAIResponseError("The AI response was not valid JSON.") from exc
    if not isinstance(payload, dict) or "test_cases" not in payload:
        raise InvalidAIResponseError(
            "The AI response must be a JSON object with a 'test_cases' list."
        )
    items = payload["test_cases"]
    if not isinstance(items, list):
        raise InvalidAIResponseError("The AI response field 'test_cases' must be a list.")

    parsed: list[_ProposedCase] = []
    skipped: list[str] = []
    errors: list[str] = []
    for index, item in enumerate(items, start=1):
        try:
            parsed.append(_ProposedCase.model_validate(item))
        except ValidationError as exc:
            problem = _nonscalar_validation_problem(exc)
            if problem is None:
                errors.append(f"Item {index}: {_format_validation_error(exc)}")
                continue
            skipped.append(_skipped_input_warning(index, item, problem))
    if errors:
        raise InvalidAIResponseError(
            "The AI response did not match the required test case structure.",
            errors,
        )
    return parsed, skipped


def _is_scalar_input(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return True
    return isinstance(value, (int, float))


def _nonscalar_input_problem(value: dict[Any, Any]) -> str | None:
    problems: list[str] = []
    for key, item in value.items():
        if _is_scalar_input(item):
            continue
        problems.append(
            f"'{key}' is {type(item).__name__} {item!r}, "
            "not a string, integer, float, boolean, or null"
        )
    if not problems:
        return None
    return "; ".join(problems)


def _nonscalar_validation_problem(exc: ValidationError) -> str | None:
    reasons: list[str] = []
    for error in exc.errors():
        message = str(error["msg"])
        marker = "Value error, "
        if marker in message:
            message = message.split(marker, 1)[1]
        if "not a string, integer, float, boolean, or null" not in message:
            return None
        reasons.append(message)
    if not reasons:
        return None
    return "; ".join(reasons)


def _skipped_input_warning(index: int, item: Any, problem: str) -> str:
    label = f"Item {index}"
    if isinstance(item, dict):
        requirement_id = item.get("requirement_id")
        if isinstance(requirement_id, str) and requirement_id.strip():
            label += f" ({requirement_id.strip()})"
    return f"Skipped test case {label} because {problem}."


def _build_case(
    requirement: AnalyzedRequirement,
    item: _ProposedCase,
    linked: list[str],
    sequence: int,
    source_file: str | None,
    binding: QuestionBinding | None = None,
) -> GeneratedTestCase:
    notes = _assessment_notes(requirement, item)
    if (
        binding is not None
        and binding.question_type in CHOICE_QUESTION_TYPES
        and binding.options_detection != "detected"
    ):
        notes.append(
            f"Form choices for '{binding.title}' were not detected, "
            "so this case needs review before execution."
        )
    if requirement.needs_human_review or requirement.ambiguity_notes:
        notes.append("The source requirement is already marked for CDM review.")
    if requirement.ambiguity_notes:
        notes.append(requirement.ambiguity_notes)
    if not requirement.expected_behavior.strip():
        notes.append("The source requirement does not state an expected behavior.")
    if item.needs_human_review:
        notes.append("The model marked this test case for human review.")
    if item.confidence < _REVIEW_CONFIDENCE:
        notes.append("Model confidence is below the review threshold.")
    if requirement.confidence is not None and requirement.confidence < _REVIEW_CONFIDENCE:
        notes.append("The source requirement confidence is below the review threshold.")

    review_notes = _join_notes(*notes)
    return GeneratedTestCase(
        test_case_id=f"TC-{requirement.requirement_id}-{item.category}-{sequence:02d}",
        requirement_id=requirement.requirement_id,
        requirement_ids=linked,
        title=item.title.strip(),
        description=item.description.strip(),
        category=item.category,
        input_data=dict(item.input_data),
        expected_result=_canonical_result(item.expected_result) or item.expected_result.strip(),
        expected_behavior=requirement.expected_behavior,
        source_file=requirement.source_file or source_file,
        source_sheet=requirement.source_sheet,
        source_row=requirement.source_row,
        rationale=item.rationale.strip(),
        confidence=item.confidence,
        needs_human_review=review_notes is not None,
        review_notes=review_notes,
    )


def _assessment_notes(requirement: AnalyzedRequirement, item: _ProposedCase) -> list[str]:
    notes: list[str] = []
    result = _canonical_result(item.expected_result)
    if result is None:
        notes.append(
            "Expected result is not Accept, Reject, or Not applicable, "
            "so it was not treated as a clinical outcome."
        )
        result = item.expected_result.strip()

    value, present = _field_value(item.input_data, requirement.field_name)
    parameters = requirement.parameters
    if item.category == "positive":
        notes.extend(_polarity_notes(requirement, value, present, result, inside=True))
    elif item.category == "negative":
        notes.extend(_polarity_notes(requirement, value, present, result, inside=False))
    elif item.category == "boundary":
        notes.extend(_boundary_notes(parameters, value, present, result))
    elif item.category == "mandatory":
        notes.extend(_mandatory_notes(value, present, result))
    elif item.category == "allowed_value":
        notes.extend(_allowed_notes(parameters, value, present, result))
    elif item.category == "conditional":
        notes.extend(_conditional_notes(requirement, item.input_data, value, result))
    elif item.category == "cross_field":
        notes.extend(_cross_field_notes(requirement, item.input_data, value, result))
    elif item.category == "date":
        notes.extend(_date_notes(requirement, item.input_data, value, present, result))

    extra_fields = _unsupported_fields(requirement, item.input_data)
    if extra_fields:
        notes.append(
            "Input includes fields that are not part of this requirement: "
            + ", ".join(extra_fields)
            + "."
        )
    return notes


def _polarity_notes(
    requirement: AnalyzedRequirement,
    value: InputValue,
    present: bool,
    result: str,
    *,
    inside: bool,
) -> list[str]:
    if not present or _is_blank(value):
        return ["The requirement field has no synthetic value."]
    number = _as_number(value)
    parameters = requirement.parameters
    notes: list[str] = []
    if parameters.allowed_values and not isinstance(value, (int, float)):
        listed = _is_allowed(value, parameters.allowed_values)
        if inside and not listed:
            notes.append(
                f"'{value}' is not one of the stated allowed values, "
                "so it was not accepted as a valid input."
            )
        if not inside and listed:
            notes.append("This negative case uses a stated allowed value.")
    if number is not None and _has_bound(parameters):
        contained = _contains(number, parameters)
        if contained is None:
            notes.append("Range inclusivity is not stated, so the input was not classified.")
        elif inside and not contained:
            notes.append("This positive input is outside the stated range.")
        elif not inside and contained:
            notes.append("This negative input is inside the stated range.")
    if _has_bound(parameters) or parameters.allowed_values:
        if inside and result not in {"Accept"}:
            notes.append("A positive case does not expect Accept.")
        if not inside and result not in {"Reject"}:
            notes.append("A negative case does not expect Reject.")
    return notes


def _boundary_notes(
    parameters: RequirementParameters,
    value: InputValue,
    present: bool,
    result: str,
) -> list[str]:
    if not present or _is_blank(value):
        return ["A boundary case needs the stated bound or its integer neighbor."]
    number = _as_number(value)
    if number is None:
        return ["The boundary input is not a number taken from the stated bounds."]
    points = _boundary_points(parameters)
    if not points:
        return ["Inclusivity or a whole-number bound is not stated, so no boundary was derived."]
    match = _matching_point(number, points)
    if match is None:
        return [f"{number} is not a boundary derived from the stated minimum or maximum."]
    expected = "Accept" if match == "inside" else "Reject"
    if result != expected:
        return [f"This boundary is {match} the stated range, so the expected result does not match."]
    return []


def _mandatory_notes(value: InputValue, present: bool, result: str) -> list[str]:
    notes: list[str] = []
    if present and not _is_blank(value):
        notes.append("The mandatory field is present, so this is not a missing-value case.")
    if result != "Reject":
        notes.append("A missing mandatory value does not expect Reject.")
    return notes


def _allowed_notes(
    parameters: RequirementParameters,
    value: InputValue,
    present: bool,
    result: str,
) -> list[str]:
    if not parameters.allowed_values:
        return ["The requirement does not state an allowed-value list."]
    if not present or _is_blank(value):
        return ["An allowed-value case needs a synthetic value."]
    listed = _is_allowed(value, parameters.allowed_values)
    if listed and result != "Accept":
        return ["A stated allowed value does not expect Accept."]
    if not listed and result != "Reject":
        return [
            "A value outside the stated list does not expect Reject. "
            "It was not added as an allowed value."
        ]
    return []


def _conditional_notes(
    requirement: AnalyzedRequirement,
    inputs: dict[str, InputValue],
    value: InputValue,
    result: str,
) -> list[str]:
    flag = _condition_met(inputs)
    if flag is None:
        return ["Conditional input does not say whether condition_met is true or false."]
    notes: list[str] = []
    if flag is False and _applies_only_if_present(requirement.condition) and result == "Reject":
        notes.append(
            "The source applies this check only when a value is provided, "
            "so a blank value was not treated as a failure."
        )
    if flag is True and _is_blank(value):
        notes.append("condition_met is true but the requirement field is blank.")
    return notes


def _cross_field_notes(
    requirement: AnalyzedRequirement,
    inputs: dict[str, InputValue],
    value: InputValue,
    result: str,
) -> list[str]:
    related_name = requirement.parameters.related_field
    if not related_name:
        return ["The requirement does not name a related field."]
    related, related_present = _field_value(inputs, related_name)
    notes: list[str] = []
    if not related_present:
        notes.append(f"Input does not include related field '{related_name}'.")
    relation = _date_relation(requirement.condition)
    left = _parse_date(value)
    right = _parse_date(related) if related_present else None
    if relation and left and right:
        valid = _dates_match(relation, left, right)
        expected = "Accept" if valid else "Reject"
        if result != expected:
            notes.append(
                "The date combination does not match the relationship stated "
                "in the source condition."
            )
    elif relation and (value is not None or related is not None) and (left is None or right is None):
        notes.append("The cross-field dates could not be read as ISO dates.")
    return notes


def _date_notes(
    requirement: AnalyzedRequirement,
    inputs: dict[str, InputValue],
    value: InputValue,
    present: bool,
    result: str,
) -> list[str]:
    if requirement.parameters.related_field:
        return _cross_field_notes(requirement, inputs, value, result)
    if not present or _is_blank(value):
        if result != "Reject":
            return ["A missing date does not expect Reject."]
        return []
    if _parse_date(value) is None:
        if result != "Reject":
            return ["A non-date value does not expect Reject."]
        return []
    if result != "Accept":
        return ["A synthetic ISO date does not expect Accept when the source only requires a date."]
    return []


def _warn_missing_branches(
    cases: list[GeneratedTestCase],
    requirements: list[AnalyzedRequirement],
    warnings: list[str],
) -> None:
    grouped: dict[tuple[str, str], list[GeneratedTestCase]] = defaultdict(list)
    for case in cases:
        grouped[(case.requirement_id, case.category)].append(case)
    by_id = {row.requirement_id: row for row in requirements}
    for (requirement_id, category), items in grouped.items():
        requirement = by_id[requirement_id]
        if category == "conditional":
            flags = {_condition_met(item.input_data) for item in items}
            if True not in flags or False not in flags:
                warnings.append(
                    f"Conditional requirement {requirement_id} does not cover "
                    "both condition_met true and false."
                )
        if category in {"cross_field", "date"} and requirement.parameters.related_field:
            results = {item.expected_result for item in items}
            if "Accept" not in results or "Reject" not in results:
                label = "Cross-field" if category == "cross_field" else "Date"
                warnings.append(
                    f"{label} requirement {requirement_id} does not cover "
                    "both a valid and an invalid combination."
                )


def _requirement_payload(
    row: AnalyzedRequirement,
    binding: QuestionBinding | None = None,
    related: QuestionBinding | None = None,
) -> dict[str, Any]:
    payload = {
        "requirement_id": row.requirement_id,
        "field_name": row.field_name,
        "rule_type": row.rule_type,
        "condition": row.condition,
        "expected_behavior": row.expected_behavior or None,
        "parameters": row.parameters.model_dump(),
        "needs_human_review": row.needs_human_review,
        "ambiguity_notes": row.ambiguity_notes,
        "source_sheet": row.source_sheet,
        "source_row": row.source_row,
    }
    if binding is None:
        return payload
    payload["form_question_title"] = binding.title
    payload["form_question_type"] = binding.question_type
    payload["form_required"] = binding.required
    payload["form_required_detection"] = binding.required_detection
    if binding.question_type in CHOICE_QUESTION_TYPES:
        payload["form_options"] = list(binding.options)
        payload["form_options_detection"] = binding.options_detection
    if related is not None:
        payload["related_form_question_title"] = related.title
    return payload


def _related_binding(
    row: AnalyzedRequirement,
    questions: list[Any],
) -> QuestionBinding | None:
    name = row.parameters.related_field
    if not name or not str(name).strip():
        return None
    return bind_field(str(name), questions)


def _unrepresentable_warning(row: AnalyzedRequirement, reason: str) -> str:
    return (
        f"Requirement {row.requirement_id} field '{row.field_name}' "
        f"cannot be represented by the form: {reason}. No test case was generated."
    )


def _append_unique(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _align_proposal(item: _ProposedCase, titles: list[str]) -> _ProposedCase:
    by_key = {match_key(title): title for title in titles if match_key(title)}
    aligned: dict[str, InputValue] = {}
    for key, value in item.input_data.items():
        target = by_key.get(match_key(key), key)
        if target in aligned and aligned[target] != value:
            aligned[key] = value
        else:
            aligned[target] = value
    if aligned == item.input_data:
        return item
    return item.model_copy(update={"input_data": aligned})


def _form_value_problem(
    item: _ProposedCase,
    requirement: AnalyzedRequirement,
    binding: QuestionBinding,
) -> str | None:
    names = [binding.title, requirement.field_name]
    if requirement.parameters.related_field:
        names.append(requirement.parameters.related_field)
    for name in names:
        value, present = _field_value(item.input_data, name)
        if present and is_placeholder_option(value):
            return f"'{value}' is a placeholder option and was not kept."
    if (
        binding.question_type not in CHOICE_QUESTION_TYPES
        or binding.options_detection != "detected"
    ):
        return None
    value, present = _field_value(item.input_data, requirement.field_name)
    if (
        _canonical_result(item.expected_result) == "Accept"
        and present
        and not _is_blank(value)
        and not _is_allowed(value, binding.options)
    ):
        return f"'{value}' is not a choice on form question '{binding.title}'."
    return None


def _linked_ids(item: _ProposedCase) -> list[str]:
    linked = [item.requirement_id]
    for requirement_id in item.requirement_ids or []:
        if requirement_id not in linked:
            linked.append(requirement_id)
    return linked


def _duplicate_key(item: _ProposedCase) -> tuple[str, ...]:
    payload = json.dumps(item.input_data, sort_keys=True, default=str)
    result = _canonical_result(item.expected_result) or item.expected_result.strip()
    return (item.requirement_id, item.category, payload, result)


def _boundary_points(parameters: RequirementParameters) -> dict[float, str]:
    points: dict[float, str] = {}
    _add_bound(points, parameters.minimum, parameters.minimum_inclusive, is_minimum=True)
    _add_bound(points, parameters.maximum, parameters.maximum_inclusive, is_minimum=False)
    return points


def _add_bound(
    points: dict[float, str],
    bound: int | float | None,
    inclusive: bool | None,
    *,
    is_minimum: bool,
) -> None:
    if bound is None or inclusive is None or not _is_whole(bound):
        return
    center = float(int(bound))
    if is_minimum:
        if inclusive:
            points[center] = "inside"
            points[center - 1] = "outside"
        else:
            points[center] = "outside"
            points[center + 1] = "inside"
        return
    if inclusive:
        points[center] = "inside"
        points[center + 1] = "outside"
    else:
        points[center] = "outside"
        points[center - 1] = "inside"


def _matching_point(number: int | float, points: dict[float, str]) -> str | None:
    for point, side in points.items():
        if float(number) == point:
            return side
    return None


def _contains(number: int | float, parameters: RequirementParameters) -> bool | None:
    if parameters.minimum is not None:
        if parameters.minimum_inclusive is True and number < parameters.minimum:
            return False
        if parameters.minimum_inclusive is False and number <= parameters.minimum:
            return False
        if parameters.minimum_inclusive is None and number == parameters.minimum:
            return None
        if parameters.minimum_inclusive is None and number < parameters.minimum:
            return False
    if parameters.maximum is not None:
        if parameters.maximum_inclusive is True and number > parameters.maximum:
            return False
        if parameters.maximum_inclusive is False and number >= parameters.maximum:
            return False
        if parameters.maximum_inclusive is None and number == parameters.maximum:
            return None
        if parameters.maximum_inclusive is None and number > parameters.maximum:
            return False
    if parameters.minimum is None and parameters.maximum is None:
        return None
    return True


def _has_bound(parameters: RequirementParameters) -> bool:
    return parameters.minimum is not None or parameters.maximum is not None


def _is_allowed(value: InputValue, allowed: list[str]) -> bool:
    if value is None:
        return False
    text = str(value).strip().casefold()
    return any(text == item.strip().casefold() for item in allowed)


def _field_value(
    inputs: dict[str, InputValue],
    field_name: str,
) -> tuple[InputValue, bool]:
    if field_name in inputs:
        return inputs[field_name], True
    target = _normalize(field_name)
    folded = match_key(field_name)
    for key, value in inputs.items():
        if _normalize(key) == target or match_key(key) == folded:
            return value, True
    return None, False


def _unsupported_fields(
    requirement: AnalyzedRequirement,
    inputs: dict[str, InputValue],
) -> list[str]:
    allowed = [requirement.field_name, "condition_met"]
    if requirement.parameters.related_field:
        allowed.append(requirement.parameters.related_field)
    return [
        key
        for key in inputs
        if not any(_same_label(key, name) for name in allowed)
    ]


def _same_label(left: str, right: str) -> bool:
    return _normalize(left) == _normalize(right) or match_key(left) == match_key(right)


def _condition_met(inputs: dict[str, InputValue]) -> bool | None:
    value, present = _field_value(inputs, "condition_met")
    if not present or not isinstance(value, bool):
        return None
    return value


def _applies_only_if_present(condition: str) -> bool:
    lowered = condition.casefold()
    return any(phrase in lowered for phrase in ("if provided", "if entered", "if present"))


def _date_relation(condition: str) -> str | None:
    lowered = condition.casefold()
    if "on or after" in lowered:
        return "on_or_after"
    if "after" in lowered:
        return "after"
    if "before" in lowered or "earlier" in lowered:
        return "before"
    return None


def _dates_match(relation: str, left: date, right: date) -> bool:
    if relation == "on_or_after":
        return left >= right
    if relation == "after":
        return left > right
    return left < right


def _parse_date(value: InputValue) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _canonical_result(value: str) -> str | None:
    return _RESULT_ALIASES.get(value.strip().casefold())


def _as_number(value: InputValue) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _is_whole(number: int | float) -> bool:
    if isinstance(number, bool):
        return False
    if isinstance(number, int):
        return True
    return isinstance(number, float) and number.is_integer()


def _is_blank(value: InputValue) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _category_counts(cases: list[GeneratedTestCase]) -> dict[str, int]:
    counts = {category: 0 for category in CATEGORIES}
    for case in cases:
        counts[case.category] += 1
    return counts


def _empty_suite(
    source_file: str | None,
    model: str | None,
    warnings: list[str],
    *,
    requirement_count: int = 0,
    form_metadata_hash: str | None = None,
) -> TestSuite:
    return TestSuite(
        test_cases=[],
        metadata=GenerationMetadata(
            source_file=source_file,
            model=model,
            requirement_count=requirement_count,
            test_case_count=0,
            rejected_count=0,
            category_counts=_category_counts([]),
            warnings=warnings,
            form_metadata_hash=form_metadata_hash,
        ),
    )


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _join_notes(*notes: str | None) -> str | None:
    parts: list[str] = []
    for note in notes:
        if note and note.strip() and note.strip() not in parts:
            parts.append(note.strip())
    return " ".join(parts) if parts else None


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "value"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
