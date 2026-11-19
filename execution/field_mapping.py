"""Map approved test-case inputs onto Google Form question titles.

This module does not open a browser. It only accepts cases that already
belong to an execution plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from execution.approval_gate import validate_approval
from models.approval import ApprovedCase, BlockedCase
from models.supporting import SupportingData
from models.test_cases import GeneratedTestCase, InputValue

QuestionType = Literal["short_text", "paragraph", "dropdown", "multiple_choice", "date"]
MappingStatus = Literal["ready", "blocked"]
ValueOrigin = Literal["test_input", "supporting", "generated_supporting"]
QUESTION_TYPES = ("short_text", "paragraph", "dropdown", "multiple_choice", "date")
KNOWN_SOURCE_FIELDS = frozenset(
    {
        "subject_id",
        "age",
        "gender",
        "informed_consent",
        "screening_date",
        "follow_up_date",
        "height_cm",
        "weight_kg",
    }
)
# These questions are required by the validation rules: Subject ID is mandatory,
# blank consent blocks progression, and screening date is required.
REQUIRED_SOURCE_FIELDS = ("subject_id", "informed_consent", "screening_date")
# Stage 2 may propose these fields when the case does not already contain them
# and an analyzed requirement permits the value. It does not invent bounds or
# allowed values, and it does not replace an original input.
SUPPORTING_BASELINE_FIELDS = (
    "subject_id",
    "age",
    "gender",
    "informed_consent",
    "screening_date",
)
_IGNORED_INPUT_FIELDS = frozenset({"condition_met"})
_SOURCE_ALIASES = {"consent": "informed_consent"}
_DEFAULT_MAPPING_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "form_mappings" / "google_form_demo.json"
)


class FieldMappingError(Exception):
    """A form mapping file could not be used."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        self.details = details if details is not None else [message]
        super().__init__("\n".join(self.details))


class FieldMapping(BaseModel):
    """One source field bound to one visible form question."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_field: str = Field(min_length=1)
    form_question_title: str = Field(min_length=1)
    form_question_type: QuestionType
    required: bool
    enabled: bool = True

    @field_validator("form_question_title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("form question title is empty")
        return value


class GoogleQuestionTarget(BaseModel):
    """Published metadata for one Google Form question."""

    model_config = ConfigDict(extra="forbid", strict=True)

    entry_id: str = Field(min_length=1)
    choices: list[str] = Field(default_factory=list)


class GoogleFormTarget(BaseModel):
    """The public fill-only Google Form. This is not a submission endpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(min_length=1)
    expected_title: str = Field(min_length=1)
    questions: dict[str, GoogleQuestionTarget]


class FormMapping(BaseModel):
    """The field mapping for one demo form."""

    model_config = ConfigDict(extra="forbid", strict=True)

    form_name: str = Field(min_length=1)
    mappings: list[FieldMapping]
    google_form: GoogleFormTarget | None = None


class MappedInput(BaseModel):
    """A form question and the value taken from the approved test case."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_field: str = Field(min_length=1)
    form_question_title: str = Field(min_length=1)
    form_question_type: QuestionType
    value: InputValue
    origin: ValueOrigin = "test_input"


class FieldMappingResult(BaseModel):
    """Mapping outcome for one approved test case."""

    model_config = ConfigDict(extra="forbid", strict=True)

    test_case_id: str = Field(min_length=1)
    mapped_inputs: list[MappedInput]
    unmapped_source_fields: list[str]
    validation_errors: list[str]
    status: MappingStatus
    original_inputs: dict[str, InputValue] = Field(default_factory=dict)
    supporting_inputs: dict[str, InputValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ready_has_no_errors(self) -> "FieldMappingResult":
        if self.status == "ready" and self.validation_errors:
            raise ValueError("A ready mapping cannot include validation errors.")
        if self.status == "blocked" and not self.validation_errors:
            raise ValueError("A blocked mapping needs at least one validation error.")
        return self


def load_form_mapping(path: Path | None = None) -> FormMapping:
    """Load and validate a form mapping JSON file."""

    mapping_path = path or _DEFAULT_MAPPING_PATH
    try:
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FieldMappingError(f"Form mapping file was not found: {mapping_path}") from exc
    except json.JSONDecodeError as exc:
        raise FieldMappingError("Form mapping file is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise FieldMappingError("Form mapping file must contain a JSON object.")
    errors = validate_field_mapping(payload)
    if errors:
        raise FieldMappingError("The form mapping is not valid.", errors)
    return FormMapping.model_validate(payload)


def validate_field_mapping(mapping: FormMapping | Mapping[str, Any]) -> list[str]:
    """Return configuration errors. An empty list means the mapping is usable."""

    payload = mapping.model_dump() if isinstance(mapping, FormMapping) else mapping
    items = payload.get("mappings") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return ["Form mapping must include a list of field mappings."]

    errors: list[str] = []
    seen: set[str] = set()
    enabled: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f"Mapping {index} must be an object.")
            continue
        source = item.get("source_field")
        title = item.get("form_question_title")
        question_type = item.get("form_question_type")
        required = item.get("required")
        enabled_flag = item.get("enabled", True)
        source_key = source.strip() if isinstance(source, str) else ""
        title_text = title.strip() if isinstance(title, str) else ""
        if not source_key:
            errors.append(f"Mapping {index} has an empty source field.")
        else:
            if source_key not in KNOWN_SOURCE_FIELDS:
                errors.append(f"Mapping references unknown input_data field '{source_key}'.")
            if source_key in seen:
                errors.append(f"Source field '{source_key}' is mapped more than once.")
            seen.add(source_key)
        if not title_text:
            errors.append(f"Mapping {index} has an empty form question title.")
        if question_type not in QUESTION_TYPES:
            errors.append(f"Mapping {index} uses unsupported question type '{question_type}'.")
        if required is True and enabled_flag is False:
            label = title_text or source_key or f"mapping {index}"
            errors.append(f"Required form question '{label}' cannot receive a value.")
        if (
            source_key in KNOWN_SOURCE_FIELDS
            and enabled_flag is True
            and question_type in QUESTION_TYPES
            and title_text
        ):
            enabled.add(source_key)
    for field in REQUIRED_SOURCE_FIELDS:
        if field not in enabled:
            errors.append(f"Required source field '{field}' has no mapping.")
    if not errors:
        try:
            parsed = mapping if isinstance(mapping, FormMapping) else FormMapping.model_validate(payload)
        except ValidationError as exc:
            errors.extend(_format_validation_error(exc))
        else:
            if not parsed.form_name.strip():
                errors.append("Form name is empty.")
    return errors


def map_test_case_inputs(
    case: ApprovedCase | BlockedCase | GeneratedTestCase,
    mapping: FormMapping | None = None,
    supporting_data: SupportingData | None = None,
) -> FieldMappingResult:
    """Map one execution-plan case onto form questions.

    Raw generated cases and blocked plan cases are refused. ``None`` input
    values stay ``None``. Confirmed supporting values fill required form
    fields the case does not test, and never replace an original input.
    """

    if isinstance(case, GeneratedTestCase):
        return _blocked(case.test_case_id, ["Only an approved execution-plan case can be mapped."])
    if isinstance(case, BlockedCase):
        return _blocked(
            case.test_case.test_case_id,
            ["Blocked cases cannot be mapped. Only approved execution-plan cases are eligible."],
        )
    if not isinstance(case, ApprovedCase):
        return _blocked("unknown", ["Only an approved execution-plan case can be mapped."])

    original_inputs = dict(case.test_case.input_data)
    form_mapping = mapping if mapping is not None else load_form_mapping()
    config_errors = validate_field_mapping(form_mapping)
    if config_errors:
        return _blocked(case.test_case.test_case_id, config_errors, original_inputs)

    approval_check = validate_approval(case.test_case, case.approval)
    if not approval_check.accepted:
        return _blocked(
            case.test_case.test_case_id,
            list(approval_check.reasons),
            original_inputs,
        )

    by_source = {item.source_field: item for item in form_mapping.mappings if item.enabled}
    mapped: list[MappedInput] = []
    unmapped: list[str] = []
    errors: list[str] = []
    present: set[str] = set()
    original_by_source: dict[str, InputValue] = {}
    for key, value in original_inputs.items():
        source_field = _canonical_source_field(key)
        if source_field in _IGNORED_INPUT_FIELDS:
            continue
        if source_field in present:
            unmapped.append(key)
            errors.append(f"Source field '{source_field}' appears more than once in input_data.")
            continue
        entry = by_source.get(source_field)
        if entry is None:
            unmapped.append(key)
            errors.append(f"Unknown input_data field '{key}'.")
            continue
        present.add(source_field)
        original_by_source[source_field] = value
        mapped.append(
            MappedInput(
                source_field=entry.source_field,
                form_question_title=entry.form_question_title,
                form_question_type=entry.form_question_type,
                value=value,
                origin=_input_origin(case.test_case, key),
            )
        )

    supporting_inputs: dict[str, InputValue] = {}
    if supporting_data is not None:
        errors.extend(_supporting_errors(case, supporting_data))
        if not errors:
            supporting_inputs, support_errors = _supporting_values(
                supporting_data,
                original_by_source,
                by_source,
            )
            errors.extend(support_errors)
            for source_field, value in supporting_inputs.items():
                entry = by_source[source_field]
                present.add(source_field)
                mapped.append(
                    MappedInput(
                        source_field=entry.source_field,
                        form_question_title=entry.form_question_title,
                        form_question_type=entry.form_question_type,
                        value=value,
                        origin="supporting",
                    )
                )
            supporting_inputs = {
                by_source[source_field].form_question_title: value
                for source_field, value in supporting_inputs.items()
            }

    for entry in form_mapping.mappings:
        if entry.enabled and entry.required and entry.source_field not in present:
            errors.append(
                f"Required form question '{entry.form_question_title}' cannot receive a value."
            )
    return FieldMappingResult(
        test_case_id=case.test_case.test_case_id,
        mapped_inputs=mapped,
        unmapped_source_fields=unmapped,
        validation_errors=errors,
        status="ready" if not errors else "blocked",
        original_inputs=original_inputs,
        supporting_inputs=supporting_inputs,
    )


def map_execution_plan(
    plan: Any,
    mapping: FormMapping | None = None,
    supporting_records: list[SupportingData] | None = None,
) -> list[FieldMappingResult]:
    """Map approved plan cases. Blocked plan cases are not included."""

    form_mapping = mapping if mapping is not None else load_form_mapping()
    grouped: dict[str, list[SupportingData]] = {}
    for record in supporting_records or []:
        grouped.setdefault(record.test_case_id, []).append(record)
    results: list[FieldMappingResult] = []
    for case in plan.approved_cases:
        records = grouped.get(case.test_case.test_case_id, [])
        if len(records) > 1:
            results.append(
                _blocked(
                    case.test_case.test_case_id,
                    ["More than one supporting baseline record exists for this test case."],
                    dict(case.test_case.input_data),
                )
            )
            continue
        supporting = records[0] if records else None
        results.append(map_test_case_inputs(case, form_mapping, supporting))
    return results


def _blocked(
    test_case_id: str,
    errors: list[str],
    original_inputs: dict[str, InputValue] | None = None,
) -> FieldMappingResult:
    return FieldMappingResult(
        test_case_id=test_case_id,
        mapped_inputs=[],
        unmapped_source_fields=[],
        validation_errors=errors,
        status="blocked",
        original_inputs=dict(original_inputs or {}),
    )


def _supporting_errors(case: ApprovedCase, supporting_data: SupportingData) -> list[str]:
    errors: list[str] = []
    if supporting_data.test_case_id != case.test_case.test_case_id:
        errors.append("Supporting data is for a different test case.")
    if not supporting_data.confirmed:
        errors.append("Supporting baseline data has not been confirmed by a reviewer.")
    if not supporting_data.source.strip():
        errors.append("Supporting baseline data has no explanation.")
    return errors


def _supporting_values(
    supporting_data: SupportingData,
    original_by_source: dict[str, InputValue],
    by_source: dict[str, FieldMapping],
) -> tuple[dict[str, InputValue], list[str]]:
    applied: dict[str, InputValue] = {}
    errors: list[str] = []
    seen: set[str] = set()
    for key, value in supporting_data.supporting_values.items():
        source_field = _canonical_source_field(key)
        if source_field not in KNOWN_SOURCE_FIELDS or source_field not in by_source:
            errors.append(f"Supporting data references unknown field '{key}'.")
            continue
        if source_field in seen:
            errors.append(f"Supporting data repeats field '{source_field}'.")
            continue
        seen.add(source_field)
        if source_field in original_by_source:
            if original_by_source[source_field] != value:
                errors.append(
                    f"Supporting value for '{key}' conflicts with the original test input."
                )
            continue
        if _blank_support_value(value):
            continue
        applied[source_field] = value
    return applied, errors


def _blank_support_value(value: InputValue) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def canonical_source_field(label: str) -> str:
    """Normalize a field label to a source field id."""

    return _canonical_source_field(label)


def _input_origin(test_case: GeneratedTestCase, key: str) -> ValueOrigin:
    role = test_case.test_data_classification.get(key)
    if role is None:
        target = _canonical_source_field(key)
        for name, item_role in test_case.test_data_classification.items():
            if _canonical_source_field(name) == target:
                role = item_role
                break
    if role == "supporting":
        return "generated_supporting"
    return "test_input"


def _canonical_source_field(label: str) -> str:
    text = label.strip().casefold().replace("-", " ")
    text = text.replace("(", " ").replace(")", " ")
    characters = [character if character.isalnum() else " " for character in text]
    slug = "_".join(part for part in "".join(characters).split() if part)
    return _SOURCE_ALIASES.get(slug, slug)


def _format_validation_error(exc: ValidationError) -> list[str]:
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "value"
        messages.append(f"{location}: {error['msg']}")
    return messages
