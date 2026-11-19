"""Turn extracted protocol and validation rows into structured interpretations.

Source wording stays on the result. Parameters that are not written in the
condition or expected behavior are rejected and sent for CDM review.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai.client import (
    InvalidAIResponseError,
    OpenAIRequirementClient,
    RequirementAIClient,
    load_ai_settings,
)
from ai.prompts import SYSTEM_PROMPT, build_user_prompt
from models.analysis import (
    AnalyzedRequirement,
    RequirementAnalysis,
    RequirementParameters,
    UninterpretedSource,
)
from models.schemas import CellValue, ProtocolRequirement, ValidationRule

SourceRow = ProtocolRequirement | ValidationRule

# Below this score the interpretation is marked for human review.
REVIEW_CONFIDENCE_THRESHOLD = 0.8

_RULE_TYPES = {
    "mandatory",
    "range",
    "allowed_values",
    "conditional",
    "cross_field",
    "date",
    "uniqueness",
    "other",
}
_RULE_ALIASES = {
    "allowedvalues": "allowed_values",
    "allowed_value": "allowed_values",
    "crossfield": "cross_field",
    "cross_fields": "cross_field",
}


class _ModelInterpretation(BaseModel):
    """The only fields accepted from the model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_sheet: str = Field(min_length=1)
    source_row: int = Field(ge=1)
    field_name: str = Field(min_length=1)
    rule_type: str
    parameters: RequirementParameters = Field(default_factory=RequirementParameters)
    confidence: float = Field(ge=0, le=1)
    needs_human_review: bool = False
    ambiguity_notes: str | None = None

    @field_validator("parameters", mode="before")
    @classmethod
    def empty_parameters(cls, value: Any) -> Any:
        if value is None:
            return {}
        return value

    @field_validator("rule_type", mode="before")
    @classmethod
    def known_rule_type(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a rule type string")
        key = value.strip().casefold().replace("-", "_").replace(" ", "_")
        key = _RULE_ALIASES.get(key, key)
        if key not in _RULE_TYPES:
            raise ValueError(f"unknown rule type '{value}'")
        return key

    @field_validator("confidence", mode="before")
    @classmethod
    def numeric_confidence(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number from 0 to 1")
        return float(value)

    @field_validator("ambiguity_notes", mode="before")
    @classmethod
    def blank_note(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be text or null")
        stripped = value.strip()
        return stripped or None


def analyze_requirements(
    protocol_rows: list[ProtocolRequirement],
    validation_rules: list[ValidationRule],
    *,
    source_file: str | None = None,
    client: RequirementAIClient | None = None,
) -> RequirementAnalysis:
    """Classify extracted rows.

    A client can be injected in tests. When no client is provided, the
    OpenAI-compatible client is built from environment settings.
    """

    rows: list[SourceRow] = [*protocol_rows, *validation_rules]
    if client is None:
        client = OpenAIRequirementClient(load_ai_settings())

    raw_response = client.complete(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt([_source_payload(row) for row in rows]),
    )
    interpretations = _parse_response(raw_response)
    index = {(row.sheet_name, row.excel_row): row for row in rows}
    grouped: dict[tuple[str, int], list[_ModelInterpretation]] = defaultdict(list)
    for item in interpretations:
        key = (item.source_sheet, item.source_row)
        if key not in index:
            raise InvalidAIResponseError(
                "The AI response cites "
                f"'{item.source_sheet}' row {item.source_row}, "
                "which is not one of the extracted rows."
            )
        grouped[key].append(item)

    requirements: list[AnalyzedRequirement] = []
    uninterpreted: list[UninterpretedSource] = []
    warnings: list[str] = []
    rejected_parameter = False

    for row in rows:
        matches = _unique_matches(grouped.get((row.sheet_name, row.excel_row), []))
        if not matches:
            uninterpreted.append(_uninterpreted(row, source_file))
            continue
        for offset, match in enumerate(matches, start=1):
            suffix = offset if len(matches) > 1 else None
            requirement, rejected = _from_model(row, match, source_file, suffix)
            rejected_parameter = rejected_parameter or rejected
            requirements.append(requirement)

    if len(grouped) and any(len(items) != len(_unique_matches(items)) for items in grouped.values()):
        warnings.append("Duplicate model interpretations for the same source row were ignored.")

    conflict_warning = _flag_conflicts(requirements)
    if conflict_warning:
        warnings.append(conflict_warning)
    if rejected_parameter:
        warnings.append(
            "Some model parameters were not accepted because they are not "
            "stated in the source text. See review notes."
        )
    if uninterpreted:
        warnings.append(
            f"{len(uninterpreted)} source row(s) had no interpretation and "
            "are listed for CDM review."
        )

    return RequirementAnalysis(
        source_file=source_file,
        requirements=requirements,
        uninterpreted_sources=uninterpreted,
        warnings=warnings,
    )


def _parse_response(raw: str) -> list[_ModelInterpretation]:
    try:
        payload = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise InvalidAIResponseError("The AI response was not valid JSON.") from exc
    if not isinstance(payload, dict) or "requirements" not in payload:
        raise InvalidAIResponseError(
            "The AI response must be a JSON object with a 'requirements' list."
        )
    items = payload["requirements"]
    if not isinstance(items, list):
        raise InvalidAIResponseError(
            "The AI response field 'requirements' must be a list."
        )

    parsed: list[_ModelInterpretation] = []
    errors: list[str] = []
    for index, item in enumerate(items, start=1):
        try:
            parsed.append(_ModelInterpretation.model_validate(item))
        except ValidationError as exc:
            errors.append(f"Item {index}: {_format_validation_error(exc)}")
    if errors:
        raise InvalidAIResponseError(
            "The AI response did not match the required structure.",
            errors,
        )
    return parsed


def _from_model(
    row: SourceRow,
    item: _ModelInterpretation,
    source_file: str | None,
    suffix: int | None,
) -> tuple[AnalyzedRequirement, bool]:
    condition = _condition_text(row)
    expected_behavior = _expected_behavior(row)
    source_field = _field_name(row)
    corpus = _source_corpus(row)
    parameters, parameter_note = _ground_parameters(item.parameters, corpus)
    notes = [item.ambiguity_notes, parameter_note]
    if not _same_label(item.field_name, source_field):
        notes.append(
            f"Model field name '{item.field_name}' does not match source "
            f"field '{source_field}'. The source field name was kept."
        )
    if not condition.strip():
        notes.append("The source row has no requirement text.")

    note = _join_notes(*notes)
    needs_review = (
        item.needs_human_review
        or item.confidence < REVIEW_CONFIDENCE_THRESHOLD
        or note is not None
    )
    requirement_id = _requirement_id(row)
    if suffix is not None:
        requirement_id = f"{requirement_id}#{suffix}"

    requirement = AnalyzedRequirement(
        requirement_id=requirement_id,
        field_name=source_field,
        rule_type=item.rule_type,
        condition=condition,
        expected_behavior=expected_behavior,
        parameters=parameters,
        source_file=source_file,
        source_sheet=row.sheet_name,
        source_row=row.excel_row,
        confidence=item.confidence,
        needs_human_review=needs_review,
        ambiguity_notes=note,
        source_values=dict(row.values),
    )
    return requirement, parameter_note is not None


def _unique_matches(items: list[_ModelInterpretation]) -> list[_ModelInterpretation]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[_ModelInterpretation] = []
    for item in items:
        key = (
            _normalize_label(item.field_name),
            item.rule_type,
            item.parameters.model_dump_json(),
            item.confidence,
            item.needs_human_review,
            item.ambiguity_notes,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _uninterpreted(row: SourceRow, source_file: str | None) -> UninterpretedSource:
    return UninterpretedSource(
        requirement_id=_requirement_id(row),
        field_name=_field_name(row),
        condition=_condition_text(row),
        expected_behavior=_expected_behavior(row),
        source_file=source_file,
        source_sheet=row.sheet_name,
        source_row=row.excel_row,
        note="The model did not return an interpretation for this source row.",
        source_values=dict(row.values),
    )


def _flag_conflicts(requirements: list[AnalyzedRequirement]) -> str | None:
    groups: dict[tuple[str, str], list[AnalyzedRequirement]] = defaultdict(list)
    for requirement in requirements:
        groups[(_normalize_label(requirement.field_name), requirement.rule_type)].append(
            requirement
        )

    conflict_fields: list[str] = []
    for (_field_name, rule_type), group in groups.items():
        signatures = {_parameter_signature(item.parameters) for item in group}
        if len(signatures) < 2:
            continue
        conflict_fields.append(f"{group[0].field_name} ({rule_type})")
        for item in group:
            item.needs_human_review = True
            item.ambiguity_notes = _join_notes(
                item.ambiguity_notes,
                "Conflicting interpretations were returned for this field and "
                "rule type. CDM review is required.",
            )
    if not conflict_fields:
        return None
    return (
        "Conflicting interpretations need CDM review: "
        + "; ".join(conflict_fields)
        + "."
    )


def _ground_parameters(
    parameters: RequirementParameters,
    corpus: str,
) -> tuple[RequirementParameters, str | None]:
    notes: list[str] = []
    minimum = parameters.minimum
    maximum = parameters.maximum
    if minimum is not None and not _number_stated(minimum, corpus):
        notes.append(
            f"Minimum {minimum} is not stated in the source text and was not accepted."
        )
        minimum = None
    if maximum is not None and not _number_stated(maximum, corpus):
        notes.append(
            f"Maximum {maximum} is not stated in the source text and was not accepted."
        )
        maximum = None

    allowed: list[str] = []
    rejected: list[str] = []
    for value in parameters.allowed_values or []:
        if _phrase_stated(value, corpus):
            allowed.append(value)
        else:
            rejected.append(value)
    if rejected:
        notes.append(
            "Allowed values not stated in the source text were not accepted: "
            + ", ".join(rejected)
            + "."
        )

    related = parameters.related_field
    if related and not _phrase_stated(related, corpus):
        notes.append(
            f"Related field '{related}' is not stated in the source text "
            "and was not accepted."
        )
        related = None

    grounded = RequirementParameters(
        minimum=minimum,
        maximum=maximum,
        minimum_inclusive=parameters.minimum_inclusive if minimum is not None else None,
        maximum_inclusive=parameters.maximum_inclusive if maximum is not None else None,
        allowed_values=allowed or None,
        related_field=related,
    )
    return grounded, _join_notes(*notes)


def _source_payload(row: SourceRow) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_sheet": row.sheet_name,
        "source_row": row.excel_row,
        "requirement_id": _requirement_id(row),
        "field_name": _field_name(row),
        "condition": _condition_text(row),
        "expected_behavior": _expected_behavior(row) or None,
    }
    if isinstance(row, ProtocolRequirement):
        payload["section"] = _as_source_text(row.section) or None
    else:
        payload["form_section"] = _as_source_text(row.form_section) or None
        payload["positive_example"] = _as_source_text(row.positive_example) or None
        payload["negative_example"] = _as_source_text(row.negative_example) or None
    return payload


def _requirement_id(row: SourceRow) -> str:
    if isinstance(row, ValidationRule) and isinstance(row.rule_id, str) and row.rule_id.strip():
        return row.rule_id
    return f"{row.sheet_name}:{row.excel_row}"


def _field_name(row: SourceRow) -> str:
    value = row.field if isinstance(row, ValidationRule) else row.item
    text = _as_source_text(value)
    return text if text else ""


def _condition_text(row: SourceRow) -> str:
    value = row.condition if isinstance(row, ValidationRule) else row.requirement
    return _as_source_text(value)


def _expected_behavior(row: SourceRow) -> str:
    if isinstance(row, ValidationRule):
        return _as_source_text(row.expected_edc_behavior)
    return ""


def _source_corpus(row: SourceRow) -> str:
    return "\n".join(
        part
        for part in (_condition_text(row), _expected_behavior(row))
        if part
    )


def _as_source_text(value: CellValue) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _number_stated(number: int | float, text: str) -> bool:
    tokens = {str(number)}
    if isinstance(number, float) and number.is_integer():
        tokens.add(str(int(number)))
    if number == 0:
        tokens.add("zero")
    for token in tokens:
        if token == "zero":
            if re.search(r"\bzero\b", text, flags=re.IGNORECASE):
                return True
            continue
        if re.search(rf"(?<!\d){re.escape(token)}(?!\d)", text):
            return True
    return False


def _phrase_stated(phrase: str, text: str) -> bool:
    pattern = rf"(?<!\w){re.escape(phrase.strip())}(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _same_label(left: str, right: str) -> bool:
    return _normalize_label(left) == _normalize_label(right)


def _normalize_label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _parameter_signature(parameters: RequirementParameters) -> tuple[Any, ...]:
    allowed = tuple(parameters.allowed_values or [])
    return (
        parameters.minimum,
        parameters.maximum,
        parameters.minimum_inclusive,
        parameters.maximum_inclusive,
        allowed,
        parameters.related_field,
    )


def _join_notes(*notes: str | None) -> str | None:
    parts = [note.strip() for note in notes if note and note.strip()]
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
