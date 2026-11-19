"""Plain functions behind the four-step Streamlit workflow.

Nothing here opens a browser or calls a model. Edits produce new model
objects, so the original analysis and test suite stay unchanged.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ai.form_manifest import (
    CHOICE_QUESTION_TYPES,
    bind_field,
    form_question_index,
    is_placeholder_option,
    match_key,
    metadata_is_usable,
)
from execution.approval_gate import (
    approve_test_case,
    build_execution_plan,
    generate_test_case_fingerprint,
)
from execution.field_mapping import FieldMappingResult, FormMapping, map_execution_plan
from models.analysis import AnalyzedRequirement, RequirementAnalysis, RequirementParameters
from models.approval import ApprovalRecord, ApprovedCase
from models.form_metadata import GoogleFormMetadata
from models.test_cases import GeneratedTestCase, InputValue, TestSuite

EXPECTED_RESULTS = ("Accept", "Reject", "Not applicable")
RULE_LABELS = {
    "mandatory": "Mandatory",
    "range": "Range",
    "allowed_values": "Allowed values",
    "conditional": "Conditional",
    "cross_field": "Cross-field",
    "date": "Date",
    "uniqueness": "Uniqueness",
    "other": "Other",
}
QUESTION_TYPE_LABELS = {
    "short_text": "Short answer",
    "paragraph": "Paragraph",
    "dropdown": "Dropdown",
    "multiple_choice": "Multiple choice",
    "checkbox": "Checkboxes",
    "date": "Date",
}
ADDED_SHEET = "Added in app"
ADDED_RATIONALE = "Added in the test suite editor."
OWNER_ROLE = "Test suite owner"
SECTION_SAVE_COMMENT = "Saved with this form question."
FORM_CHANGED = (
    "The Google Form changed after the test cases were created. "
    "Create the test cases again."
)
FORM_UNREADABLE = "The Google Form could not be read, so no test case was run."
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
_BOUND = re.compile(
    r"^(minimum|maximum)\s+([+-]?\d+(?:\.\d+)?)(?:\s+(inclusive|exclusive))?$",
    re.IGNORECASE,
)


@dataclass
class QuestionGroup:
    """Test cases that exercise one exact form question."""

    title: str
    question_type: str
    options: list[str]
    rules: dict[str, str]
    related_titles: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PreparedRun:
    """Cases cleared for Fill Only, and cases held back with reasons."""

    ready: list[tuple[ApprovedCase, FieldMappingResult, str]]
    blocked: list[tuple[str, str, list[str]]]


def blank(value: Any) -> bool:
    """True for None, NaN, and whitespace-only text from the editor."""

    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def text(value: Any) -> str:
    return "" if blank(value) else str(value).strip()


def display_value(value: InputValue) -> str:
    if value is None:
        return ""
    return str(value)


def format_rule(requirement: AnalyzedRequirement) -> str:
    label = RULE_LABELS.get(requirement.rule_type, requirement.rule_type)
    values = _format_parameters(
        requirement.parameters,
        bare_values=requirement.rule_type == "allowed_values",
    )
    return f"{label}: {values}" if values else label


def parse_rule(value: str) -> tuple[str, RequirementParameters]:
    """Read text written in the same form ``format_rule`` produces."""

    raw = value.strip()
    if not raw:
        raise ValueError("Validation rule is empty.")
    label, _, rest = raw.partition(":")
    rule_type = _rule_key(label.strip())
    parameters: dict[str, Any] = {}
    for part in [item.strip() for item in rest.split(";") if item.strip()]:
        lowered = part.casefold()
        if lowered.startswith("allowed values"):
            listed = part.partition(":")[2]
            values = [item.strip() for item in listed.split(",") if item.strip()]
            if not values:
                raise ValueError("Allowed values are empty.")
            parameters["allowed_values"] = values
            continue
        if lowered.startswith("related field"):
            related = part.partition(":")[2].strip()
            if not related:
                raise ValueError("Related field is empty.")
            parameters["related_field"] = related
            continue
        match = _BOUND.match(part)
        if match is None and rule_type == "allowed_values" and "allowed_values" not in parameters:
            parameters["allowed_values"] = [
                item.strip() for item in part.split(",") if item.strip()
            ]
            continue
        if match is None:
            raise ValueError(f"'{part}' is not a minimum, maximum, allowed values, or related field.")
        side = match.group(1).casefold()
        parameters[side] = _number(match.group(2))
        inclusivity = match.group(3)
        parameters[f"{side}_inclusive"] = (
            None if inclusivity is None else inclusivity.casefold() == "inclusive"
        )
    return rule_type, RequirementParameters(**parameters)


def form_question_label(
    requirement: AnalyzedRequirement,
    metadata: GoogleFormMetadata | None,
) -> str:
    if metadata is None or not metadata_is_usable(metadata):
        return "Form not read"
    binding = bind_field(requirement.field_name, metadata.questions)
    if binding.status == "matched":
        return binding.title
    return f"Unavailable: {binding.reason}"


def requirement_rows(
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata | None,
) -> list[dict[str, Any]]:
    return [
        {
            "ID": item.requirement_id,
            "Field": item.field_name,
            "Requirement description": item.condition,
            "Validation rule and values": format_rule(item),
            "Source": _source_label(item),
            "Form question": form_question_label(item, metadata),
        }
        for item in analysis.requirements
    ]


def unavailable_requirements(
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata | None,
) -> list[str]:
    if metadata is None or not metadata_is_usable(metadata):
        return []
    labels: list[str] = []
    for item in analysis.requirements:
        binding = bind_field(item.field_name, metadata.questions)
        if binding.status != "matched":
            labels.append(f"{item.field_name} ({_source_label(item)}): {binding.reason}")
    return labels


def apply_requirement_edits(
    analysis: RequirementAnalysis,
    rows: list[dict[str, Any]],
) -> tuple[RequirementAnalysis, list[str]]:
    """Apply table edits. Existing rows keep their source sheet and row."""

    by_id = {item.requirement_id: item for item in analysis.requirements}
    used = set(by_id)
    updated: list[AnalyzedRequirement] = []
    errors: list[str] = []
    added = 0
    for index, row in enumerate(rows, start=1):
        requirement_id = text(row.get("ID"))
        field_name = text(row.get("Field"))
        description = text(row.get("Requirement description"))
        rule_text = text(row.get("Validation rule and values"))
        if not field_name and not description and not rule_text and not requirement_id:
            continue
        label = f"Row {index}"
        if not field_name:
            errors.append(f"{label}: Field is empty.")
            continue
        if not description:
            errors.append(f"{label}: Requirement description is empty.")
            continue
        original = by_id.get(requirement_id)
        if original is not None and (
            field_name == original.field_name
            and description == original.condition
            and rule_text == format_rule(original)
        ):
            updated.append(original)
            continue
        try:
            rule_type, parameters = parse_rule(rule_text or "Other")
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if original is not None:
            updated.append(
                original.model_copy(
                    update={
                        "field_name": field_name,
                        "condition": description,
                        "rule_type": rule_type,
                        "parameters": parameters,
                    }
                )
            )
            continue
        added += 1
        new_id = _next_id(used)
        used.add(new_id)
        updated.append(
            AnalyzedRequirement(
                requirement_id=new_id,
                field_name=field_name,
                rule_type=rule_type,
                condition=description,
                expected_behavior=description,
                parameters=parameters,
                source_file=analysis.source_file,
                source_sheet=ADDED_SHEET,
                source_row=int(new_id.rsplit("-", 1)[1]),
                confidence=None,
                needs_human_review=False,
                source_values={"Field": field_name, "Requirement": description},
            )
        )
    return analysis.model_copy(update={"requirements": updated}), errors


def case_groups(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
    notes: dict[str, str] | None = None,
) -> list[QuestionGroup]:
    """Group cases under the form question their requirement tests."""

    notes = notes or {}
    groups: dict[str, QuestionGroup] = {}
    order: dict[str, int] = {}
    index = form_question_index(metadata.questions)
    for position, question in enumerate(metadata.questions, start=1):
        order.setdefault(match_key(question.visible_title), position)
    for requirement in analysis.requirements:
        group = _group_for(requirement, metadata, index, groups)
        if group is None:
            continue
        label = _unique_label(requirement.condition or requirement.field_name, group.rules)
        group.rules[label] = requirement.requirement_id
        related = _related_title(requirement, metadata)
        if related and related not in group.related_titles:
            group.related_titles.append(related)
    by_id = {item.requirement_id: item for item in analysis.requirements}
    for case in suite.test_cases:
        requirement = by_id.get(case.requirement_id)
        if requirement is None:
            continue
        group = _group_for(requirement, metadata, index, groups)
        if group is None:
            continue
        value, _present = _value_for(case.input_data, group.title)
        row: dict[str, Any] = {
            "ID": case.test_case_id,
            "Test case": case.title,
            "Rule": _rule_label(group, case.requirement_id),
            "Input value": display_value(value),
        }
        for related in group.related_titles:
            related_value, _ = _value_for(case.input_data, related)
            row[related] = display_value(related_value)
        row["Expected result"] = case.expected_result
        row["Needs review"] = "Yes" if case.needs_human_review else "No"
        row["Review note"] = notes.get(case.test_case_id, "")
        group.rows.append(row)
    return sorted(groups.values(), key=lambda item: order.get(match_key(item.title), 0))


def apply_case_edits(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
    edited: dict[str, list[dict[str, Any]]],
) -> tuple[TestSuite, dict[str, str], list[str], list[str]]:
    """Return the edited suite, review notes, errors, and added case ids."""

    groups = {group.title: group for group in case_groups(suite, analysis, metadata)}
    by_case = {case.test_case_id: case for case in suite.test_cases}
    by_requirement = {item.requirement_id: item for item in analysis.requirements}
    kept: dict[str, GeneratedTestCase] = {}
    added: list[GeneratedTestCase] = []
    notes: dict[str, str] = {}
    errors: list[str] = []
    used_ids = set(by_case)
    # Only rows from sections included in this save can be removed. A section
    # that is not included keeps its cases exactly as they are.
    touched: set[str] = set()
    for title, rows in edited.items():
        group = groups.get(title)
        if group is None:
            continue
        touched.update(text(row.get("ID")) for row in group.rows)
        for index, row in enumerate(rows, start=1):
            label = f"{title}, row {index}"
            name = text(row.get("Test case"))
            case_id = text(row.get("ID"))
            if not name and not case_id and not text(row.get("Input value")):
                continue
            if not name:
                errors.append(f"{label}: Test case name is empty.")
                continue
            expected = text(row.get("Expected result"))
            if expected not in EXPECTED_RESULTS:
                errors.append(f"{label}: Expected result must be Accept, Reject, or Not applicable.")
                continue
            requirement_id = group.rules.get(text(row.get("Rule")))
            if requirement_id is None:
                if len(group.rules) == 1:
                    requirement_id = next(iter(group.rules.values()))
                else:
                    errors.append(f"{label}: Choose the rule this test case checks.")
                    continue
            requirement = by_requirement[requirement_id]
            original = by_case.get(case_id)
            base = original or _new_case(requirement, name, expected, used_ids)
            used_ids.add(base.test_case_id)
            inputs = dict(base.input_data)
            classification = dict(base.test_data_classification)
            for question, column in [(title, "Input value"), *[(item, item) for item in group.related_titles]]:
                if column not in row:
                    continue
                _set_input(
                    inputs,
                    classification,
                    question,
                    text(row.get(column)),
                    requirement if question == title else None,
                )
            update: dict[str, Any] = {
                "title": name,
                "expected_result": expected,
                "input_data": inputs,
                "test_data_classification": classification,
            }
            if original is not None and requirement_id != original.requirement_id:
                update.update(_requirement_update(requirement))
            case = GeneratedTestCase.model_validate({**base.model_dump(), **update})
            note = text(row.get("Review note"))
            if note:
                notes[case.test_case_id] = note
            if original is None:
                added.append(case)
            else:
                kept[case.test_case_id] = case
    touched.discard("")
    ordered = [
        kept.get(case.test_case_id, case)
        for case in suite.test_cases
        if case.test_case_id in kept or case.test_case_id not in touched
    ]
    ordered.extend(added)
    return _with_cases(suite, ordered), notes, errors, [case.test_case_id for case in added]


def save_question_section(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
    title: str,
    rows: list[dict[str, Any]],
) -> tuple[TestSuite, list[str], list[str]]:
    """Apply one question's edited rows and leave every other question unchanged.

    Returns the suite, errors, and ids of cases added in this section. Nothing
    is approved here; call ``merge_section_approvals`` after any follow-up
    updates, such as filling the other required answers.
    """

    updated, _notes, errors, added_ids = apply_case_edits(suite, analysis, metadata, {title: rows})
    if errors:
        return suite, errors, []
    return updated, [], added_ids


def merge_section_approvals(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
    title: str,
    approvals: dict[str, ApprovalRecord],
) -> dict[str, ApprovalRecord]:
    """Approve the current cases for one question and keep the other approvals.

    Cases removed from the question lose their approval. An approval for another
    question is kept only while it still matches that case. No reviewer comment
    is required: saving the section is the approval.
    """

    section_ids = {
        case.test_case_id
        for case in suite.test_cases
        if question_for_case(case, analysis, metadata) == title
    }
    merged: dict[str, ApprovalRecord] = {}
    for case in suite.test_cases:
        if case.test_case_id in section_ids:
            merged[case.test_case_id] = _approval_for_section_save(case)
            continue
        record = approvals.get(case.test_case_id)
        if record is None:
            continue
        if record.test_case_fingerprint == generate_test_case_fingerprint(case):
            merged[case.test_case_id] = record
    return merged


def remove_test_case(suite: TestSuite, case_id: str) -> TestSuite:
    """Drop one case. Every remaining case keeps its id and contents."""

    kept = [case for case in suite.test_cases if case.test_case_id != case_id]
    if len(kept) == len(suite.test_cases):
        return suite
    return _with_cases(suite, kept)


def edited_case_ids(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
    title: str,
    rows: list[dict[str, Any]],
) -> list[str]:
    """Cases in one question whose editor rows differ from the saved case.

    A case that disappeared from the editor is included, so it is no longer
    treated as approved. Cases in other questions are ignored.
    """

    group = next((item for item in case_groups(suite, analysis, metadata) if item.title == title), None)
    if group is None:
        return []
    rows_by_id = {text(row.get("ID")): row for row in rows if text(row.get("ID"))}
    changed: list[str] = []
    for case in suite.test_cases:
        if question_for_case(case, analysis, metadata) != title:
            continue
        row = rows_by_id.get(case.test_case_id)
        if row is None or _row_changes_case(case, group, row):
            changed.append(case.test_case_id)
    return changed


def revoke_approvals(
    approvals: dict[str, ApprovalRecord],
    case_ids: list[str] | set[str],
) -> dict[str, ApprovalRecord]:
    """Drop approvals for the given cases and keep every other approval."""

    skipped = set(case_ids)
    return {case_id: record for case_id, record in approvals.items() if case_id not in skipped}


def approve_saved_suite(
    suite: TestSuite,
    reviewer_name: str,
    notes: dict[str, str],
) -> dict[str, ApprovalRecord]:
    """Record the named user's approval of each saved case for Fill Only."""

    return {
        case.test_case_id: approve_test_case(
            case,
            reviewer_name=reviewer_name,
            reviewer_role=OWNER_ROLE,
            decision="approved",
            comment=notes.get(case.test_case_id, ""),
            approved_expected_behavior=_approved_behavior(case),
        )
        for case in suite.test_cases
    }


def question_for_case(
    case: GeneratedTestCase,
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
) -> str:
    by_id = {item.requirement_id: item for item in analysis.requirements}
    requirement = by_id.get(case.requirement_id)
    if requirement is None:
        return ""
    binding = bind_field(requirement.field_name, metadata.questions)
    return binding.title if binding.status == "matched" else ""


def prepare_execution(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    approvals: dict[str, ApprovalRecord],
    saved_metadata: GoogleFormMetadata,
    current_metadata: GoogleFormMetadata,
    mapping: FormMapping,
) -> PreparedRun:
    """Apply every existing safeguard, then check values against the live form."""

    titles = {
        case.test_case_id: question_for_case(case, analysis, saved_metadata)
        for case in suite.test_cases
    }
    if not metadata_is_usable(current_metadata):
        return PreparedRun(
            ready=[],
            blocked=[
                (titles[case.test_case_id], case.title, [FORM_UNREADABLE])
                for case in suite.test_cases
            ],
        )
    if current_metadata.metadata_hash != suite.metadata.form_metadata_hash:
        return PreparedRun(
            ready=[],
            blocked=[
                (titles[case.test_case_id], case.title, [FORM_CHANGED])
                for case in suite.test_cases
            ],
        )
    plan = build_execution_plan(suite.test_cases, list(approvals.values()))
    blocked: list[tuple[str, str, list[str]]] = [
        (titles[item.test_case.test_case_id], item.test_case.title, list(item.reasons))
        for item in plan.blocked_cases
    ]
    index = form_question_index(current_metadata.questions)
    results = {item.test_case_id: item for item in map_execution_plan(plan, mapping)}
    ready: list[tuple[ApprovedCase, FieldMappingResult, str]] = []
    for approved in plan.approved_cases:
        case = approved.test_case
        result = results[case.test_case_id]
        reasons = list(result.validation_errors)
        if result.status == "ready":
            reasons.extend(value_problems(result, index))
        if reasons:
            blocked.append((titles[case.test_case_id], case.title, reasons))
            continue
        ready.append((approved, result, titles[case.test_case_id]))
    return PreparedRun(ready=ready, blocked=blocked)


def value_problems(
    result: FieldMappingResult,
    index: dict[str, dict[str, Any]],
) -> list[str]:
    """Check mapped values against the captured question types and choices."""

    problems: list[str] = []
    for item in result.mapped_inputs:
        question = index.get(match_key(item.form_question_title))
        if question is None:
            problems.append(f"'{item.form_question_title}' is not a question on the current form.")
            continue
        value = item.value
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if is_placeholder_option(value):
            problems.append(f"'{value}' is a placeholder, not an answer for '{item.form_question_title}'.")
            continue
        question_type = question.get("question_type")
        options = question.get("options") or []
        if (
            question_type in CHOICE_QUESTION_TYPES
            and question.get("options_detection") == "detected"
            and str(value).strip().casefold() not in {str(option).casefold() for option in options}
        ):
            problems.append(f"'{value}' is not a choice for '{item.form_question_title}'.")
            continue
        if question_type == "date" and _iso_date(value) is None:
            problems.append(f"'{value}' is not a date in YYYY-MM-DD form for '{item.form_question_title}'.")
    return problems


def _format_parameters(parameters: RequirementParameters, *, bare_values: bool = False) -> str:
    parts: list[str] = []
    if parameters.allowed_values:
        listed = ", ".join(parameters.allowed_values)
        parts.append(listed if bare_values else f"allowed values: {listed}")
    if parameters.minimum is not None:
        parts.append(_format_bound("minimum", parameters.minimum, parameters.minimum_inclusive))
    if parameters.maximum is not None:
        parts.append(_format_bound("maximum", parameters.maximum, parameters.maximum_inclusive))
    if parameters.related_field:
        parts.append(f"related field: {parameters.related_field}")
    return "; ".join(parts)


def _format_bound(label: str, value: int | float, inclusive: bool | None) -> str:
    if inclusive is True:
        return f"{label} {value} inclusive"
    if inclusive is False:
        return f"{label} {value} exclusive"
    return f"{label} {value}"


def _rule_key(label: str) -> str:
    wanted = label.casefold()
    for key, name in RULE_LABELS.items():
        if wanted in {key, name.casefold()}:
            return key
    raise ValueError(
        f"'{label}' is not a validation rule. Use one of: " + ", ".join(RULE_LABELS.values()) + "."
    )


def _number(value: str) -> int | float:
    return float(value) if "." in value else int(value)


def _source_label(requirement: AnalyzedRequirement) -> str:
    if requirement.source_sheet == ADDED_SHEET:
        return ADDED_SHEET
    return f"{requirement.source_sheet} row {requirement.source_row}"


def _next_id(used: set[str]) -> str:
    number = 1
    while f"USER-{number:03d}" in used:
        number += 1
    return f"USER-{number:03d}"


def _group_for(
    requirement: AnalyzedRequirement,
    metadata: GoogleFormMetadata,
    index: dict[str, dict[str, Any]],
    groups: dict[str, QuestionGroup],
) -> QuestionGroup | None:
    binding = bind_field(requirement.field_name, metadata.questions)
    if binding.status != "matched":
        return None
    if binding.title not in groups:
        question = index.get(match_key(binding.title), {})
        groups[binding.title] = QuestionGroup(
            title=binding.title,
            question_type=str(question.get("question_type", binding.question_type)),
            options=list(question.get("options") or []),
            rules={},
            related_titles=[],
        )
    return groups[binding.title]


def _related_title(requirement: AnalyzedRequirement, metadata: GoogleFormMetadata) -> str:
    name = requirement.parameters.related_field
    if not name:
        return ""
    binding = bind_field(name, metadata.questions)
    return binding.title if binding.status == "matched" else ""


def _unique_label(label: str, existing: dict[str, str]) -> str:
    candidate = label
    number = 2
    while candidate in existing:
        candidate = f"{label} ({number})"
        number += 1
    return candidate


def _rule_label(group: QuestionGroup, requirement_id: str) -> str:
    for label, item in group.rules.items():
        if item == requirement_id:
            return label
    return ""


def _value_for(inputs: dict[str, InputValue], title: str) -> tuple[InputValue, bool]:
    for key, value in inputs.items():
        if match_key(key) == match_key(title):
            return value, True
    return None, False


def _set_input(
    inputs: dict[str, InputValue],
    classification: dict[str, str],
    title: str,
    entered: str,
    requirement: AnalyzedRequirement | None,
) -> None:
    key = next((item for item in inputs if match_key(item) == match_key(title)), None)
    if key is None:
        if not entered:
            return
        key = title
        if classification:
            classification[key] = "under_test"
    original = inputs.get(key)
    if key in inputs and entered == display_value(original):
        return
    inputs[key] = _typed_value(entered, original, requirement)


def _typed_value(
    entered: str,
    original: InputValue,
    requirement: AnalyzedRequirement | None,
) -> InputValue:
    if not entered:
        return None
    if isinstance(original, bool):
        lowered = entered.casefold()
        if lowered in {"true", "yes"}:
            return True
        if lowered in {"false", "no"}:
            return False
        return entered
    numeric = isinstance(original, (int, float))
    if requirement is not None:
        parameters = requirement.parameters
        numeric = numeric or parameters.minimum is not None or parameters.maximum is not None
    if numeric and _NUMBER.fullmatch(entered):
        return _number(entered)
    return entered


def _new_case(
    requirement: AnalyzedRequirement,
    name: str,
    expected: str,
    used: set[str],
) -> GeneratedTestCase:
    number = 1
    while f"TC-{requirement.requirement_id}-added-{number:02d}" in used:
        number += 1
    return GeneratedTestCase(
        test_case_id=f"TC-{requirement.requirement_id}-added-{number:02d}",
        requirement_id=requirement.requirement_id,
        requirement_ids=[requirement.requirement_id],
        title=name,
        description=ADDED_RATIONALE,
        category="added",
        input_data={},
        expected_result=expected,
        expected_behavior=requirement.expected_behavior,
        source_file=requirement.source_file,
        source_sheet=requirement.source_sheet,
        source_row=requirement.source_row,
        rationale=ADDED_RATIONALE,
        confidence=None,
        needs_human_review=False,
    )


def _requirement_update(requirement: AnalyzedRequirement) -> dict[str, Any]:
    return {
        "requirement_id": requirement.requirement_id,
        "requirement_ids": [requirement.requirement_id],
        "expected_behavior": requirement.expected_behavior,
        "source_file": requirement.source_file,
        "source_sheet": requirement.source_sheet,
        "source_row": requirement.source_row,
    }


def _with_cases(suite: TestSuite, cases: list[GeneratedTestCase]) -> TestSuite:
    counts = dict.fromkeys(suite.metadata.category_counts, 0)
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1
    metadata = suite.metadata.model_copy(
        update={"test_case_count": len(cases), "category_counts": counts}
    )
    return suite.model_copy(update={"test_cases": cases, "metadata": metadata})


def _approved_behavior(case: GeneratedTestCase) -> str:
    behavior = case.expected_behavior.strip()
    result = f"Expected result: {case.expected_result}."
    return f"{result} {behavior}" if behavior else result


def _approval_for_section_save(case: GeneratedTestCase) -> ApprovalRecord:
    """Approve one case because its question section was saved.

    The approval gate still requires a note when a case was flagged for human
    review. The section save supplies that note, so the user is not asked.
    """

    return approve_test_case(
        case,
        reviewer_name=OWNER_ROLE,
        reviewer_role=OWNER_ROLE,
        decision="approved",
        comment=SECTION_SAVE_COMMENT if case.needs_human_review else "",
        approved_expected_behavior=_approved_behavior(case),
    )


def _row_changes_case(case: GeneratedTestCase, group: QuestionGroup, row: dict[str, Any]) -> bool:
    if text(row.get("Test case")) != case.title:
        return True
    if text(row.get("Expected result")) != case.expected_result:
        return True
    current, _present = _value_for(case.input_data, group.title)
    if text(row.get("Input value")) != display_value(current):
        return True
    for related in group.related_titles:
        related_value, _present = _value_for(case.input_data, related)
        if text(row.get(related)) != display_value(related_value):
            return True
    rule = text(row.get("Rule"))
    if rule:
        requirement_id = group.rules.get(rule)
        if requirement_id is not None and requirement_id != case.requirement_id:
            return True
    return False


def _iso_date(value: InputValue) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None
