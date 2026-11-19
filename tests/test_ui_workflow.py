"""Workflow helpers behind the Streamlit UI. No browser or model calls."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from execution.approval_gate import REASON_COMMENT, REASON_FINGERPRINT
from ai.form_manifest import form_question_index
from execution.field_mapping import FieldMappingResult, MappedInput, load_form_mapping
from models.analysis import AnalyzedRequirement, RequirementAnalysis, RequirementParameters
from models.form_metadata import ExtractedFormQuestion, GoogleFormMetadata
from models.test_cases import GeneratedTestCase, GenerationMetadata, TestSuite
from ui.workflow import (
    ADDED_SHEET,
    FORM_CHANGED,
    FORM_UNREADABLE,
    SECTION_SAVE_COMMENT,
    apply_case_edits,
    apply_requirement_edits,
    approve_saved_suite,
    case_groups,
    edited_case_ids,
    format_rule,
    merge_section_approvals,
    parse_rule,
    prepare_execution,
    remove_test_case,
    requirement_rows,
    revoke_approvals,
    save_question_section,
    unavailable_requirements,
    value_problems,
)

FORM_URL = "https://docs.google.com/forms/d/e/example/viewform"
HASH = "a" * 64


def _requirement(**overrides: object) -> AnalyzedRequirement:
    data: dict[str, object] = {
        "requirement_id": "VAL-002",
        "field_name": "Age",
        "rule_type": "range",
        "condition": "Age must be an integer from 18 to 65 inclusive.",
        "expected_behavior": "Accept 18-65.",
        "parameters": RequirementParameters(
            minimum=18,
            maximum=65,
            minimum_inclusive=True,
            maximum_inclusive=True,
        ),
        "source_file": "study.xlsx",
        "source_sheet": "Validation_Rules",
        "source_row": 6,
        "confidence": 0.95,
        "needs_human_review": False,
        "source_values": {"Field": "Age"},
    }
    data.update(overrides)
    return AnalyzedRequirement(**data)


def _gender() -> AnalyzedRequirement:
    return _requirement(
        requirement_id="VAL-006",
        field_name="Gender",
        rule_type="allowed_values",
        condition="Gender must be Male, Female, or Not Reported.",
        expected_behavior="Accept a listed gender.",
        parameters=RequirementParameters(allowed_values=["Male", "Female", "Not Reported"]),
        source_row=10,
        source_values={"Field": "Gender"},
    )


def _follow_up() -> AnalyzedRequirement:
    return _requirement(
        requirement_id="VAL-005",
        field_name="Follow-up Date",
        rule_type="cross_field",
        condition="Follow-up Date must be on or after Screening Date.",
        expected_behavior="Reject a follow-up before screening.",
        parameters=RequirementParameters(related_field="Screening Date"),
        source_row=9,
        source_values={"Field": "Follow-up Date"},
    )


def _visit() -> AnalyzedRequirement:
    return _requirement(
        requirement_id="Study_Protocol:12",
        field_name="Screening Visit",
        rule_type="date",
        condition="A screening date must be entered for each demo subject.",
        expected_behavior="A screening date is entered.",
        parameters=RequirementParameters(),
        source_sheet="Study_Protocol",
        source_row=12,
        source_values={"Item": "Screening Visit"},
    )


def _analysis(*requirements: AnalyzedRequirement) -> RequirementAnalysis:
    return RequirementAnalysis(
        source_file="study.xlsx",
        requirements=list(requirements),
        uninterpreted_sources=[],
        warnings=[],
    )


def _question(position: int, title: str, kind: str = "short_text", **overrides: object) -> ExtractedFormQuestion:
    data: dict[str, object] = {
        "position": position,
        "visible_title": title,
        "accessible_label": title,
        "question_type": kind,
        "type_detection": "detected",
        "required": True,
        "required_detection": "detected",
        "options": [],
        "options_detection": "not_applicable",
    }
    data.update(overrides)
    return ExtractedFormQuestion(**data)


def _metadata(metadata_hash: str = HASH, status: str = "captured") -> GoogleFormMetadata:
    questions = [
        _question(1, "Subject ID *"),
        _question(2, "Age *"),
        _question(
            3,
            "Gender *",
            "dropdown",
            options=["Choose", "Male", "Female", "Not Reported"],
            options_detection="detected",
        ),
        _question(
            4,
            "Informed Consent *",
            "multiple_choice",
            options=["Yes", "No"],
            options_detection="detected",
        ),
        _question(5, "Screening Date *", "date"),
        _question(6, "Follow-up Date", "date", required=None, required_detection="not_detected"),
        _question(7, "Height (cm)"),
        _question(8, "Weight (kg)"),
    ]
    return GoogleFormMetadata(
        form_url=FORM_URL,
        page_title="Clinical Study Data Collection - UAT Demo",
        captured_at=datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc),
        status=status,  # type: ignore[arg-type]
        questions=questions if status == "captured" else [],
        metadata_hash=metadata_hash,
    )


def _complete_inputs(**overrides: object) -> dict[str, object]:
    inputs: dict[str, object] = {
        "Age": 18,
        "Subject ID": "SYN-001",
        "Gender": "Female",
        "Informed Consent": "Yes",
        "Screening Date": "2026-10-01",
    }
    inputs.update(overrides)
    return inputs


def _case(case_id: str = "TC-VAL-002-boundary-01", **overrides: object) -> GeneratedTestCase:
    inputs = overrides.pop("input_data", _complete_inputs())
    primary = overrides.pop("primary", "Age")
    classification = {
        key: "under_test" if key == primary else "supporting" for key in inputs
    }
    data: dict[str, object] = {
        "test_case_id": case_id,
        "requirement_id": "VAL-002",
        "requirement_ids": ["VAL-002"],
        "title": "Age 18",
        "description": "Inclusive minimum age.",
        "category": "boundary",
        "input_data": inputs,
        "test_data_classification": classification,
        "supporting_requirement_ids": {
            key: ["VAL-001"] for key, role in classification.items() if role == "supporting"
        },
        "expected_result": "Accept",
        "expected_behavior": "Accept 18-65.",
        "source_file": "study.xlsx",
        "source_sheet": "Validation_Rules",
        "source_row": 6,
        "rationale": "18 is the stated inclusive minimum.",
        "confidence": 0.95,
        "needs_human_review": False,
    }
    data.update(overrides)
    return GeneratedTestCase(**data)


def _suite(*cases: GeneratedTestCase) -> TestSuite:
    return TestSuite(
        test_cases=list(cases),
        metadata=GenerationMetadata(
            source_file="study.xlsx",
            model=None,
            requirement_count=3,
            test_case_count=len(cases),
            rejected_count=0,
            category_counts={"boundary": len(cases)},
            warnings=[],
            form_metadata_hash=HASH,
        ),
    )


def test_rules_round_trip_and_unknown_rules_are_rejected() -> None:
    age = _requirement()
    gender = _gender()
    follow = _follow_up()

    assert format_rule(age) == "Range: minimum 18 inclusive; maximum 65 inclusive"
    assert format_rule(gender) == "Allowed values: Male, Female, Not Reported"
    assert format_rule(follow) == "Cross-field: related field: Screening Date"
    for requirement in (age, gender, follow):
        rule_type, parameters = parse_rule(format_rule(requirement))
        assert rule_type == requirement.rule_type
        assert parameters == requirement.parameters
    assert parse_rule("Mandatory") == ("mandatory", RequirementParameters())
    with pytest.raises(ValueError, match="not a validation rule"):
        parse_rule("Sometimes: whenever")
    with pytest.raises(ValueError, match="not a minimum"):
        parse_rule("Range: around 18")


def test_requirement_rows_show_the_form_question_or_why_it_is_unavailable() -> None:
    analysis = _analysis(_requirement(), _visit())
    rows = requirement_rows(analysis, _metadata())

    assert rows[0]["Field"] == "Age"
    assert rows[0]["Source"] == "Validation_Rules row 6"
    assert rows[0]["Form question"] == "Age"
    assert rows[1]["Form question"] == "Unavailable: no form question has this label"
    assert "Screening Date" not in rows[1]["Form question"]
    assert unavailable_requirements(analysis, _metadata()) == [
        "Screening Visit (Study_Protocol row 12): no form question has this label"
    ]
    assert requirement_rows(analysis, None)[0]["Form question"] == "Form not read"


def test_requirement_edits_keep_traceability_and_add_or_delete_rows() -> None:
    age = _requirement()
    gender = _gender()
    visit = _visit()
    analysis = _analysis(age, gender, visit)
    rows = requirement_rows(analysis, _metadata())
    rows[1]["Validation rule and values"] = "Allowed values: Male, Female"
    del rows[2]
    rows.append(
        {
            "ID": None,
            "Field": "Weight (kg)",
            "Requirement description": "Weight must be greater than 0.",
            "Validation rule and values": "Range: minimum 0 exclusive",
            "Source": None,
            "Form question": None,
        }
    )
    updated, errors = apply_requirement_edits(analysis, rows)
    kept, edited, added = updated.requirements

    assert errors == []
    assert kept is age
    assert edited.requirement_id == "VAL-006"
    assert edited.source_sheet == "Validation_Rules"
    assert edited.source_row == 10
    assert edited.source_values == {"Field": "Gender"}
    assert edited.parameters.allowed_values == ["Male", "Female"]
    assert added.requirement_id == "USER-001"
    assert added.source_sheet == ADDED_SHEET
    assert added.parameters.minimum == 0
    assert added.parameters.minimum_inclusive is False
    assert "Study_Protocol:12" not in {item.requirement_id for item in updated.requirements}
    assert analysis.requirements[1].parameters.allowed_values == ["Male", "Female", "Not Reported"]


def test_invalid_requirement_rows_are_reported() -> None:
    analysis = _analysis(_requirement())
    rows = requirement_rows(analysis, _metadata())
    rows[0]["Validation rule and values"] = "Range: about 18"
    rows.append({"ID": None, "Field": "", "Requirement description": "Missing field"})
    _updated, errors = apply_requirement_edits(analysis, rows)

    assert errors == [
        "Row 1: 'about 18' is not a minimum, maximum, allowed values, or related field.",
        "Row 2: Field is empty.",
    ]


def test_cases_are_grouped_by_exact_question_without_supporting_fields() -> None:
    analysis = _analysis(_requirement(), _gender(), _follow_up(), _visit())
    follow = _case(
        "TC-VAL-005-cross_field-01",
        requirement_id="VAL-005",
        requirement_ids=["VAL-005"],
        title="Follow-up before screening",
        category="cross_field",
        input_data=_complete_inputs(**{"Follow-up Date": "2026-09-01"}),
        primary="Follow-up Date",
        expected_result="Reject",
        source_row=9,
    )
    groups = case_groups(_suite(_case(), follow), analysis, _metadata())
    by_title = {group.title: group for group in groups}

    assert [group.title for group in groups] == ["Age", "Gender", "Follow-up Date"]
    age_row = by_title["Age"].rows[0]
    assert age_row["Input value"] == "18"
    assert "Subject ID" not in age_row
    assert "Gender" not in age_row
    assert by_title["Gender"].options == ["Male", "Female", "Not Reported"]
    assert by_title["Gender"].rows == []
    assert by_title["Follow-up Date"].related_titles == ["Screening Date"]
    assert by_title["Follow-up Date"].rows[0]["Screening Date"] == "2026-10-01"
    assert "Screening Visit" not in by_title


def test_case_edits_keep_types_and_sources_and_support_add_and_delete() -> None:
    analysis = _analysis(_requirement(), _gender())
    first = _case()
    second = _case("TC-VAL-002-boundary-02", title="Age 65", input_data=_complete_inputs(Age=65))
    subject = _requirement(
        requirement_id="VAL-001",
        field_name="Subject ID",
        rule_type="mandatory",
        condition="Subject ID is mandatory.",
        parameters=RequirementParameters(),
        source_row=5,
    )
    analysis = _analysis(_requirement(), _gender(), subject)
    subject_case = _case(
        "TC-VAL-001-positive-01",
        requirement_id="VAL-001",
        requirement_ids=["VAL-001"],
        title="Subject present",
        primary="Subject ID",
        source_row=5,
    )
    suite = _suite(first, second, subject_case)
    groups = {group.title: group for group in case_groups(suite, analysis, _metadata())}
    age_rows = groups["Age"].rows
    age_rows[0]["Input value"] = "17"
    age_rows[0]["Expected result"] = "Reject"
    del age_rows[1]
    gender_rows = [
        {
            "ID": None,
            "Test case": "Gender Male",
            "Rule": None,
            "Input value": "Male",
            "Expected result": "Accept",
            "Review note": None,
        }
    ]
    subject_rows = groups["Subject ID"].rows
    subject_rows[0]["Input value"] = "001"
    edited, notes, errors, added_ids = apply_case_edits(
        suite,
        analysis,
        _metadata(),
        {"Age": age_rows, "Gender": gender_rows, "Subject ID": subject_rows},
    )
    by_id = {case.test_case_id: case for case in edited.test_cases}

    assert errors == []
    assert notes == {}
    assert [case.test_case_id for case in edited.test_cases] == [
        "TC-VAL-002-boundary-01",
        "TC-VAL-001-positive-01",
        "TC-VAL-006-added-01",
    ]
    changed = by_id["TC-VAL-002-boundary-01"]
    assert changed.input_data["Age"] == 17
    assert isinstance(changed.input_data["Age"], int)
    assert changed.input_data["Subject ID"] == "SYN-001"
    assert changed.expected_result == "Reject"
    assert changed.source_row == 6
    assert by_id["TC-VAL-001-positive-01"].input_data["Subject ID"] == "001"
    added = by_id["TC-VAL-006-added-01"]
    assert added_ids == ["TC-VAL-006-added-01"]
    assert added.input_data == {"Gender": "Male"}
    assert added.requirement_id == "VAL-006"
    assert added.source_sheet == "Validation_Rules"
    assert added.source_row == 10
    assert added.review_status == "pending_cdm_review"
    assert edited.metadata.test_case_count == 3
    assert first.input_data["Age"] == 18


def test_case_edit_errors_are_reported() -> None:
    analysis = _analysis(_requirement())
    suite = _suite(_case())
    rows = case_groups(suite, analysis, _metadata())[0].rows
    rows[0]["Expected result"] = "Maybe"
    rows.append({"ID": None, "Test case": "", "Input value": "30", "Expected result": "Accept"})
    _edited, _notes, errors, _added = apply_case_edits(suite, analysis, _metadata(), {"Age": rows})

    assert errors == [
        "Age, row 1: Expected result must be Accept, Reject, or Not applicable.",
        "Age, row 2: Test case name is empty.",
    ]


def test_saved_suite_runs_only_cases_that_pass_every_safeguard() -> None:
    analysis = _analysis(_requirement(), _gender())
    ready = _case()
    flagged = _case("TC-VAL-002-boundary-02", title="Flagged", needs_human_review=True, review_notes="Check.")
    gender = _case(
        "TC-VAL-006-negative-01",
        requirement_id="VAL-006",
        requirement_ids=["VAL-006"],
        title="Unlisted gender",
        input_data=_complete_inputs(Gender="NOT_A_LISTED_VALUE"),
        primary="Gender",
        expected_result="Reject",
        source_row=10,
    )
    placeholder = _case(
        "TC-VAL-006-negative-02",
        requirement_id="VAL-006",
        requirement_ids=["VAL-006"],
        title="Placeholder gender",
        input_data=_complete_inputs(Gender="Choose"),
        primary="Gender",
        expected_result="Reject",
        source_row=10,
    )
    bad_date = _case(
        "TC-VAL-002-boundary-03",
        title="Bad date",
        input_data=_complete_inputs(**{"Screening Date": "yesterday"}),
    )
    suite = _suite(ready, flagged, gender, placeholder, bad_date)
    approvals = approve_saved_suite(suite, "Casey Reviewer", {})
    prepared = prepare_execution(suite, analysis, approvals, _metadata(), _metadata(), load_form_mapping())
    blocked = {name: reasons for _question, name, reasons in prepared.blocked}

    assert [(item[0].test_case.test_case_id, item[2]) for item in prepared.ready] == [
        ("TC-VAL-002-boundary-01", "Age")
    ]
    assert prepared.ready[0][0].execution_expected_behavior == "Expected result: Accept. Accept 18-65."
    assert approvals["TC-VAL-002-boundary-01"].reviewer_name == "Casey Reviewer"
    assert blocked["Flagged"] == [REASON_COMMENT]
    assert blocked["Unlisted gender"] == ["'NOT_A_LISTED_VALUE' is not a choice for 'Gender'."]
    assert blocked["Placeholder gender"] == ["'Choose' is a placeholder, not an answer for 'Gender'."]
    assert blocked["Bad date"] == [
        "'yesterday' is not a date in YYYY-MM-DD form for 'Screening Date'."
    ]

    noted = approve_saved_suite(suite, "Casey Reviewer", {"TC-VAL-002-boundary-02": "Reviewed."})
    rerun = prepare_execution(suite, analysis, noted, _metadata(), _metadata(), load_form_mapping())
    assert "TC-VAL-002-boundary-02" in {item[0].test_case.test_case_id for item in rerun.ready}


def test_blank_date_string_is_a_blank_answer_not_a_bad_date() -> None:
    """Regression: the live 'Screening Date blank' case was blocked as a malformed date."""

    result = FieldMappingResult(
        test_case_id="TC-VAL-004-date-01",
        mapped_inputs=[
            MappedInput(
                source_field="screening_date",
                form_question_title="Screening Date",
                form_question_type="date",
                value="",
            ),
            MappedInput(
                source_field="follow_up_date",
                form_question_title="Follow-up Date",
                form_question_type="date",
                value="soon",
            ),
        ],
        unmapped_source_fields=[],
        validation_errors=[],
        status="ready",
    )

    assert value_problems(result, form_question_index(_metadata().questions)) == [
        "'soon' is not a date in YYYY-MM-DD form for 'Follow-up Date'."
    ]


def test_execution_is_blocked_when_the_form_changed_or_cannot_be_read() -> None:
    analysis = _analysis(_requirement())
    suite = _suite(_case())
    approvals = approve_saved_suite(suite, "Casey Reviewer", {})
    mapping = load_form_mapping()
    changed = prepare_execution(suite, analysis, approvals, _metadata(), _metadata("b" * 64), mapping)
    unreadable = prepare_execution(suite, analysis, approvals, _metadata(), _metadata(status="error"), mapping)
    edited = suite.model_copy(
        update={"test_cases": [_case(input_data=_complete_inputs(Age=19))]}
    )
    stale = prepare_execution(edited, analysis, approvals, _metadata(), _metadata(), mapping)

    assert changed.ready == []
    assert changed.blocked == [("Age", "Age 18", [FORM_CHANGED])]
    assert unreadable.ready == []
    assert unreadable.blocked == [("Age", "Age 18", [FORM_UNREADABLE])]
    assert stale.ready == []
    assert stale.blocked == [("Age", "Age 18", [REASON_FINGERPRINT])]


def test_missing_required_answer_stays_blocked() -> None:
    analysis = _analysis(_requirement())
    inputs = _complete_inputs()
    del inputs["Gender"]
    suite = _suite(_case(input_data=inputs))
    approvals = approve_saved_suite(suite, "Casey Reviewer", {})
    prepared = prepare_execution(suite, analysis, approvals, _metadata(), _metadata(), load_form_mapping())

    assert prepared.ready == []
    assert prepared.blocked == [
        ("Age", "Age 18", ["Required form question 'Gender' cannot receive a value."])
    ]


def _subject() -> AnalyzedRequirement:
    return _requirement(
        requirement_id="VAL-001",
        field_name="Subject ID",
        rule_type="mandatory",
        condition="Subject ID is mandatory.",
        expected_behavior="Accept a subject ID.",
        parameters=RequirementParameters(),
        source_row=5,
    )


def _subject_case(case_id: str, title: str) -> GeneratedTestCase:
    return _case(
        case_id,
        requirement_id="VAL-001",
        requirement_ids=["VAL-001"],
        title=title,
        primary="Subject ID",
        expected_behavior="Accept a subject ID.",
        source_row=5,
    )


def _age_and_subject():
    analysis = _analysis(_requirement(), _gender(), _subject())
    suite = _suite(
        _case(),
        _case("TC-VAL-002-boundary-02", title="Age 65", input_data=_complete_inputs(Age=65)),
        _subject_case("TC-VAL-001-negative-01", "Subject ID blank"),
        _subject_case("TC-VAL-001-negative-02", "Mandatory Subject ID blank"),
    )
    return analysis, suite


def test_saving_one_question_does_not_approve_another() -> None:
    analysis, suite = _age_and_subject()
    groups = {group.title: group for group in case_groups(suite, analysis, _metadata())}
    age_rows = groups["Age"].rows
    age_rows[0]["Input value"] = "17"
    updated, errors, added_ids = save_question_section(
        suite, analysis, _metadata(), "Age", age_rows
    )
    approvals = merge_section_approvals(updated, analysis, _metadata(), "Age", {})

    assert errors == []
    assert added_ids == []
    assert set(approvals) == {"TC-VAL-002-boundary-01", "TC-VAL-002-boundary-02"}
    assert approvals["TC-VAL-002-boundary-01"].comment == ""
    assert approvals["TC-VAL-002-boundary-01"].reviewer_name == "Test suite owner"
    by_id = {case.test_case_id: case for case in updated.test_cases}
    assert by_id["TC-VAL-002-boundary-01"].input_data["Age"] == 17
    assert isinstance(by_id["TC-VAL-002-boundary-01"].input_data["Age"], int)
    assert by_id["TC-VAL-001-negative-01"].input_data["Subject ID"] == "SYN-001"
    assert by_id["TC-VAL-001-negative-02"].title == "Mandatory Subject ID blank"
    prepared = prepare_execution(updated, analysis, approvals, _metadata(), _metadata(), load_form_mapping())
    ready_ids = {item[0].test_case.test_case_id for item in prepared.ready}
    assert ready_ids == {"TC-VAL-002-boundary-01", "TC-VAL-002-boundary-02"}
    assert "TC-VAL-001-negative-01" not in ready_ids

    subject_rows = groups["Subject ID"].rows
    subject_rows[0]["Input value"] = ""
    resaved, subject_errors, _added = save_question_section(
        updated, analysis, _metadata(), "Subject ID", subject_rows
    )
    both = merge_section_approvals(resaved, analysis, _metadata(), "Subject ID", approvals)

    assert subject_errors == []
    assert set(both) == {
        "TC-VAL-002-boundary-01",
        "TC-VAL-002-boundary-02",
        "TC-VAL-001-negative-01",
        "TC-VAL-001-negative-02",
    }
    assert both["TC-VAL-002-boundary-01"].test_case_fingerprint == approvals[
        "TC-VAL-002-boundary-01"
    ].test_case_fingerprint
    saved_age = next(case for case in resaved.test_cases if case.test_case_id == "TC-VAL-002-boundary-01")
    assert saved_age.input_data["Age"] == 17


def test_add_and_delete_stay_inside_one_question() -> None:
    analysis, suite = _age_and_subject()
    metadata = _metadata()
    groups = {group.title: group for group in case_groups(suite, analysis, metadata)}
    age_saved, age_errors, _added = save_question_section(
        suite, analysis, metadata, "Age", groups["Age"].rows
    )
    approvals = merge_section_approvals(age_saved, analysis, metadata, "Age", {})
    subject_rows = [dict(row) for row in groups["Subject ID"].rows]
    subject_rows.append(
        {
            "ID": None,
            "Test case": "Subject ID present",
            "Rule": groups["Subject ID"].rows[0]["Rule"],
            "Input value": "SYN-002",
            "Expected result": "Accept",
        }
    )
    updated, errors, added_ids = save_question_section(
        age_saved, analysis, metadata, "Subject ID", subject_rows
    )
    approvals = merge_section_approvals(updated, analysis, metadata, "Subject ID", approvals)

    assert age_errors == []
    assert errors == []
    assert added_ids == ["TC-VAL-001-added-01"]
    assert [case.test_case_id for case in updated.test_cases] == [
        "TC-VAL-002-boundary-01",
        "TC-VAL-002-boundary-02",
        "TC-VAL-001-negative-01",
        "TC-VAL-001-negative-02",
        "TC-VAL-001-added-01",
    ]
    removed = remove_test_case(updated, "TC-VAL-001-negative-02")
    approvals = revoke_approvals(approvals, ["TC-VAL-001-negative-02"])

    assert [case.test_case_id for case in removed.test_cases] == [
        "TC-VAL-002-boundary-01",
        "TC-VAL-002-boundary-02",
        "TC-VAL-001-negative-01",
        "TC-VAL-001-added-01",
    ]
    assert removed.test_cases[0] is updated.test_cases[0]
    assert removed.test_cases[2] is updated.test_cases[2]
    assert "TC-VAL-001-negative-02" not in approvals
    prepared = prepare_execution(removed, analysis, approvals, metadata, metadata, load_form_mapping())
    ready_ids = {item[0].test_case.test_case_id for item in prepared.ready}
    assert "TC-VAL-001-negative-02" not in ready_ids
    assert {"TC-VAL-002-boundary-01", "TC-VAL-002-boundary-02", "TC-VAL-001-negative-01"} <= ready_ids
    subject = next(group for group in case_groups(removed, analysis, metadata) if group.title == "Subject ID")
    assert [row["ID"] for row in subject.rows] == ["TC-VAL-001-negative-01", "TC-VAL-001-added-01"]
    assert len(next(group for group in case_groups(removed, analysis, metadata) if group.title == "Age").rows) == 2


def test_edited_cases_need_the_section_saved_again_and_no_comment_is_required() -> None:
    analysis = _analysis(_requirement(), _subject())
    flagged = _case("TC-VAL-002-boundary-02", title="Flagged age", needs_human_review=True)
    suite = _suite(_case(), flagged, _subject_case("TC-VAL-001-negative-01", "Subject ID blank"))
    groups = {group.title: group for group in case_groups(suite, analysis, _metadata())}
    saved, errors, _added = save_question_section(
        suite, analysis, _metadata(), "Age", groups["Age"].rows
    )
    approvals = merge_section_approvals(saved, analysis, _metadata(), "Age", {})
    prepared = prepare_execution(saved, analysis, approvals, _metadata(), _metadata(), load_form_mapping())

    assert errors == []
    assert {item[0].test_case.test_case_id for item in prepared.ready} == {
        "TC-VAL-002-boundary-01",
        "TC-VAL-002-boundary-02",
    }
    assert approvals["TC-VAL-002-boundary-02"].comment == SECTION_SAVE_COMMENT
    assert approvals["TC-VAL-002-boundary-01"].comment == ""
    assert "TC-VAL-001-negative-01" not in approvals

    edited_rows = [dict(row) for row in groups["Age"].rows]
    edited_rows[0]["Input value"] = "19"
    changed = edited_case_ids(saved, analysis, _metadata(), "Age", edited_rows)
    assert changed == ["TC-VAL-002-boundary-01"]
    assert edited_case_ids(saved, analysis, _metadata(), "Subject ID", groups["Subject ID"].rows) == []
    revoked = revoke_approvals(approvals, changed)
    rerun = prepare_execution(saved, analysis, revoked, _metadata(), _metadata(), load_form_mapping())
    ready_ids = {item[0].test_case.test_case_id for item in rerun.ready}
    assert "TC-VAL-002-boundary-01" not in ready_ids
    assert "TC-VAL-002-boundary-02" in ready_ids

    resaved, resave_errors, _added = save_question_section(
        saved, analysis, _metadata(), "Age", edited_rows
    )
    restored = merge_section_approvals(resaved, analysis, _metadata(), "Age", revoked)
    final = prepare_execution(resaved, analysis, restored, _metadata(), _metadata(), load_form_mapping())
    assert resave_errors == []
    assert "TC-VAL-002-boundary-01" in {item[0].test_case.test_case_id for item in final.ready}
    assert next(
        case.input_data["Age"]
        for case in resaved.test_cases
        if case.test_case_id == "TC-VAL-002-boundary-01"
    ) == 19
