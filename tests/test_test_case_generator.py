"""Unit tests for AI test case generation. No live gateway calls."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ai.client import InvalidAIResponseError
from ai.test_case_generator import generate_test_cases
from models.analysis import (
    AnalyzedRequirement,
    RequirementAnalysis,
    RequirementParameters,
    UninterpretedSource,
)
from models.test_cases import GeneratedTestCase


class FakeClient:
    def __init__(self, payload: dict | str) -> None:
        self.payload = payload if isinstance(payload, str) else json.dumps(payload)
        self.system_prompt = ""
        self.user_prompt = ""
        self.calls = 0

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.payload


def _requirement(**overrides: object) -> AnalyzedRequirement:
    data: dict[str, object] = {
        "requirement_id": "VAL-002",
        "field_name": "Age",
        "rule_type": "range",
        "condition": "Age must be an integer from 18 to 65 inclusive.",
        "expected_behavior": "Accept 18\u201365; validate values below 18 or above 65.",
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
        "ambiguity_notes": None,
        "source_values": {"Field": "Age"},
    }
    data.update(overrides)
    return AnalyzedRequirement(**data)


def _analysis(*requirements: AnalyzedRequirement, **overrides: object) -> RequirementAnalysis:
    data: dict[str, object] = {
        "source_file": "study.xlsx",
        "requirements": list(requirements),
        "uninterpreted_sources": [],
        "warnings": [],
    }
    data.update(overrides)
    return RequirementAnalysis(**data)


def _case(requirement_id: str = "VAL-002", **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "requirement_id": requirement_id,
        "title": "Synthetic case",
        "description": "Synthetic input for the stated requirement.",
        "category": "positive",
        "input_data": {"Age": 30},
        "expected_result": "Accept",
        "rationale": "30 is inside the stated inclusive range.",
        "confidence": 0.95,
        "needs_human_review": False,
    }
    item.update(overrides)
    return item


def _generate(
    analysis: RequirementAnalysis,
    payload: dict | str,
) -> tuple[object, FakeClient]:
    client = FakeClient(payload)
    suite = generate_test_cases(analysis, client=client)
    return suite, client


def test_positive_case_inside_inclusive_range_is_kept() -> None:
    requirement = _requirement()
    before = requirement.model_dump()
    suite, client = _generate(
        _analysis(requirement),
        {"test_cases": [_case()]},
    )

    assert client.calls == 1
    assert len(suite.test_cases) == 1
    case = suite.test_cases[0]
    assert case.category == "positive"
    assert case.input_data == {"Age": 30}
    assert case.expected_result == "Accept"
    assert case.needs_human_review is False
    assert case.review_status == "pending_cdm_review"
    assert case.review_notes is None
    assert case.requirement_id == "VAL-002"
    assert case.test_case_id == "TC-VAL-002-positive-01"
    assert requirement.model_dump() == before


def test_negative_case_outside_range_is_kept() -> None:
    suite, _client = _generate(
        _analysis(_requirement()),
        {
            "test_cases": [
                _case(
                    category="negative",
                    input_data={"Age": 10},
                    expected_result="Reject",
                    rationale="10 is below the stated minimum.",
                )
            ]
        },
    )

    case = suite.test_cases[0]
    assert case.category == "negative"
    assert case.input_data["Age"] == 10
    assert case.expected_result == "Reject"
    assert case.needs_human_review is False


def test_positive_value_outside_the_stated_range_is_flagged_and_not_rewritten() -> None:
    suite, _client = _generate(
        _analysis(_requirement()),
        {"test_cases": [_case(input_data={"Age": 10})]},
    )

    case = suite.test_cases[0]
    assert case.input_data["Age"] == 10
    assert case.needs_human_review is True
    assert case.review_notes is not None
    assert "outside the stated range" in case.review_notes


def test_inclusive_boundary_points_accept_the_bound_and_reject_the_neighbor() -> None:
    suite, _client = _generate(
        _analysis(_requirement()),
        {
            "test_cases": [
                _case(category="boundary", input_data={"Age": 18}, expected_result="Accept"),
                _case(category="boundary", input_data={"Age": 17}, expected_result="Reject"),
                _case(category="boundary", input_data={"Age": 65}, expected_result="Accept"),
                _case(category="boundary", input_data={"Age": 66}, expected_result="Reject"),
                _case(category="boundary", input_data={"Age": 30}, expected_result="Accept"),
            ]
        },
    )

    by_age = {case.input_data["Age"]: case for case in suite.test_cases}
    assert by_age[18].needs_human_review is False
    assert by_age[17].needs_human_review is False
    assert by_age[65].needs_human_review is False
    assert by_age[66].needs_human_review is False
    assert by_age[18].expected_result == "Accept"
    assert by_age[17].expected_result == "Reject"
    assert by_age[30].needs_human_review is True
    assert "not a boundary" in (by_age[30].review_notes or "")


def test_exclusive_minimum_boundary_rejects_the_bound_and_accepts_the_next_integer() -> None:
    requirement = _requirement(
        requirement_id="VAL-HEIGHT",
        field_name="Height",
        rule_type="range",
        condition="If provided, height must be a positive number.",
        expected_behavior="Accept a number greater than zero.",
        parameters=RequirementParameters(minimum=0, minimum_inclusive=False),
        source_row=8,
    )
    suite, _client = _generate(
        _analysis(requirement),
        {
            "test_cases": [
                _case(
                    "VAL-HEIGHT",
                    category="boundary",
                    input_data={"Height": 0},
                    expected_result="Reject",
                ),
                _case(
                    "VAL-HEIGHT",
                    category="boundary",
                    input_data={"Height": 1},
                    expected_result="Accept",
                ),
            ]
        },
    )

    by_value = {case.input_data["Height"]: case for case in suite.test_cases}
    assert by_value[0].expected_result == "Reject"
    assert by_value[0].needs_human_review is False
    assert by_value[1].expected_result == "Accept"
    assert by_value[1].needs_human_review is False


def test_conditional_cases_cover_true_and_false_without_synthesizing_a_branch() -> None:
    requirement = _requirement(
        requirement_id="VAL-HEIGHT",
        field_name="Height",
        rule_type="conditional",
        condition="If provided, height must be a positive number.",
        expected_behavior="Accept a number greater than zero.",
        parameters=RequirementParameters(minimum=0, minimum_inclusive=False),
    )
    suite, _client = _generate(
        _analysis(requirement),
        {
            "test_cases": [
                _case(
                    "VAL-HEIGHT",
                    category="conditional",
                    input_data={"Height": 170, "condition_met": True},
                    expected_result="Accept",
                )
            ]
        },
    )

    assert len(suite.test_cases) == 1
    assert suite.test_cases[0].input_data["condition_met"] is True
    assert any("condition_met true and false" in warning for warning in suite.metadata.warnings)


def test_conditional_false_branch_is_not_a_reject_when_the_check_applies_only_if_provided() -> None:
    requirement = _requirement(
        requirement_id="VAL-HEIGHT",
        field_name="Height",
        rule_type="conditional",
        condition="If provided, height must be a positive number.",
        expected_behavior="Accept a number greater than zero.",
        parameters=RequirementParameters(minimum=0, minimum_inclusive=False),
    )
    suite, _client = _generate(
        _analysis(requirement),
        {
            "test_cases": [
                _case(
                    "VAL-HEIGHT",
                    category="conditional",
                    input_data={"Height": 170, "condition_met": True},
                    expected_result="Accept",
                ),
                _case(
                    "VAL-HEIGHT",
                    category="conditional",
                    input_data={"Height": None, "condition_met": False},
                    expected_result="Reject",
                ),
            ]
        },
    )

    false_case = next(case for case in suite.test_cases if case.input_data["condition_met"] is False)
    assert false_case.needs_human_review is True
    assert "only when a value is provided" in (false_case.review_notes or "")
    assert not any("condition_met true and false" in warning for warning in suite.metadata.warnings)


def test_cross_field_date_combinations_keep_valid_and_invalid_pairs() -> None:
    requirement = _requirement(
        requirement_id="VAL-005",
        field_name="Follow-up Date",
        rule_type="cross_field",
        condition="Follow-up date must be on or after the screening date.",
        expected_behavior="Reject a follow-up date earlier than the screening date.",
        parameters=RequirementParameters(related_field="Screening Date"),
        source_row=9,
    )
    suite, _client = _generate(
        _analysis(requirement),
        {
            "test_cases": [
                _case(
                    "VAL-005",
                    category="cross_field",
                    input_data={
                        "Follow-up Date": "2026-10-08",
                        "Screening Date": "2026-10-01",
                    },
                    expected_result="Accept",
                ),
                _case(
                    "VAL-005",
                    category="cross_field",
                    input_data={
                        "Follow-up Date": "2026-09-30",
                        "Screening Date": "2026-10-01",
                    },
                    expected_result="Reject",
                ),
                _case(
                    "VAL-005",
                    category="cross_field",
                    input_data={
                        "Follow-up Date": "2026-09-30",
                        "Screening Date": "2026-10-01",
                    },
                    expected_result="Accept",
                    title="Swapped polarity",
                ),
            ]
        },
    )

    valid = suite.test_cases[0]
    invalid = suite.test_cases[1]
    swapped = suite.test_cases[2]
    assert valid.needs_human_review is False
    assert invalid.needs_human_review is False
    assert swapped.needs_human_review is True
    assert "does not match the relationship" in (swapped.review_notes or "")
    assert not any("does not cover" in warning for warning in suite.metadata.warnings)


def test_cross_field_case_without_the_related_field_is_flagged() -> None:
    requirement = _requirement(
        requirement_id="VAL-005",
        field_name="Follow-up Date",
        rule_type="cross_field",
        condition="Follow-up date must be on or after the screening date.",
        expected_behavior="Reject a follow-up date earlier than the screening date.",
        parameters=RequirementParameters(related_field="Screening Date"),
    )
    suite, _client = _generate(
        _analysis(requirement),
        {
            "test_cases": [
                _case(
                    "VAL-005",
                    category="cross_field",
                    input_data={"Follow-up Date": "2026-10-08"},
                    expected_result="Accept",
                )
            ]
        },
    )

    assert suite.test_cases[0].needs_human_review is True
    assert "Screening Date" in (suite.test_cases[0].review_notes or "")
    assert any("valid and an invalid combination" in warning for warning in suite.metadata.warnings)


def test_missing_mandatory_value_expects_reject() -> None:
    requirement = _requirement(
        requirement_id="VAL-001",
        field_name="Subject ID",
        rule_type="mandatory",
        condition="Subject ID is mandatory.",
        expected_behavior="Do not allow completion of a screening form when blank.",
        parameters=RequirementParameters(),
        source_row=5,
    )
    suite, _client = _generate(
        _analysis(requirement),
        {
            "test_cases": [
                _case(
                    "VAL-001",
                    category="mandatory",
                    input_data={"Subject ID": None},
                    expected_result="Reject",
                ),
                _case(
                    "VAL-001",
                    category="mandatory",
                    input_data={"Subject ID": "SUBJ-001"},
                    expected_result="Reject",
                    title="Value is present",
                ),
            ]
        },
    )

    missing, present = suite.test_cases
    assert missing.input_data["Subject ID"] is None
    assert missing.expected_result == "Reject"
    assert missing.needs_human_review is False
    assert present.needs_human_review is True
    assert "not a missing-value case" in (present.review_notes or "")


def test_allowed_value_cases_do_not_add_the_invalid_value_to_the_list() -> None:
    allowed = ["Male", "Female", "Not Reported"]
    requirement = _requirement(
        requirement_id="VAL-006",
        field_name="Gender",
        rule_type="allowed_values",
        condition="Gender must be Male, Female, or Not Reported.",
        expected_behavior="Accept only the stated gender values.",
        parameters=RequirementParameters(allowed_values=allowed),
        source_row=10,
    )
    suite, _client = _generate(
        _analysis(requirement),
        {
            "test_cases": [
                _case(
                    "VAL-006",
                    category="allowed_value",
                    input_data={"Gender": "Female"},
                    expected_result="Accept",
                ),
                _case(
                    "VAL-006",
                    category="allowed_value",
                    input_data={"Gender": "NOT_A_LISTED_VALUE"},
                    expected_result="Reject",
                ),
                _case(
                    "VAL-006",
                    category="positive",
                    input_data={"Gender": "NOT_A_LISTED_VALUE"},
                    expected_result="Accept",
                    title="Unsupported positive value",
                ),
            ]
        },
    )

    listed, unknown, unsupported = suite.test_cases
    assert listed.needs_human_review is False
    assert unknown.needs_human_review is False
    assert unknown.input_data["Gender"] == "NOT_A_LISTED_VALUE"
    assert unsupported.needs_human_review is True
    assert requirement.parameters.allowed_values == allowed


def test_string_subject_id_and_integer_age_stay_scalars() -> None:
    subject = _requirement(
        requirement_id="VAL-001",
        field_name="Subject ID",
        rule_type="mandatory",
        condition="Subject ID is mandatory.",
        expected_behavior="Reject a blank subject id.",
        parameters=RequirementParameters(),
        source_row=5,
        source_values={"Field": "Subject ID"},
    )
    age = _requirement()
    suite, _client = _generate(
        _analysis(subject, age),
        {
            "test_cases": [
                _case(
                    "VAL-001",
                    category="positive",
                    input_data={"Subject ID": "SUBJ-001"},
                    expected_result="Accept",
                ),
                _case(input_data={"Age": 18}),
            ]
        },
    )

    subject_case, age_case = suite.test_cases
    assert subject_case.input_data == {"Subject ID": "SUBJ-001"}
    assert isinstance(subject_case.input_data["Subject ID"], str)
    assert subject_case.requirement_id == "VAL-001"
    assert subject_case.source_sheet == "Validation_Rules"
    assert subject_case.source_row == 5
    assert age_case.input_data == {"Age": 18}
    assert isinstance(age_case.input_data["Age"], int)
    assert age_case.requirement_id == "VAL-002"
    assert age_case.source_row == 6
    assert suite.metadata.rejected_count == 0
    assert suite.metadata.warnings == []


def test_object_and_array_subject_ids_are_skipped_with_the_rejected_value() -> None:
    subject = _requirement(
        requirement_id="VAL-001",
        field_name="Subject ID",
        rule_type="mandatory",
        condition="Subject ID is mandatory.",
        expected_behavior="Reject a blank subject id.",
        parameters=RequirementParameters(),
        source_row=5,
        source_values={"Field": "Subject ID"},
    )
    nested = {"value": "SUBJ-001"}
    sequence = ["SUBJ-001"]
    suite, _client = _generate(
        _analysis(subject, _requirement()),
        {
            "test_cases": [
                _case(input_data={"Age": 65}, title="Before"),
                _case(
                    "VAL-001",
                    input_data={"Subject ID": nested},
                    title="Malformed object",
                ),
                _case(input_data={"Age": 18}, title="After"),
                _case(
                    "VAL-001",
                    input_data={"Subject ID": sequence},
                    title="Malformed array",
                ),
            ]
        },
    )

    assert [case.input_data for case in suite.test_cases] == [{"Age": 65}, {"Age": 18}]
    assert [case.title for case in suite.test_cases] == ["Before", "After"]
    assert [isinstance(case.input_data["Age"], int) for case in suite.test_cases] == [True, True]
    assert suite.test_cases[0].source_row == 6
    assert suite.metadata.rejected_count == 2
    assert suite.metadata.warnings == [
        "Skipped test case Item 2 (VAL-001) because 'Subject ID' is dict "
        "{'value': 'SUBJ-001'}, not a string, integer, float, boolean, or null.",
        "Skipped test case Item 4 (VAL-001) because 'Subject ID' is list "
        "['SUBJ-001'], not a string, integer, float, boolean, or null.",
    ]
    assert nested == {"value": "SUBJ-001"}
    assert sequence == ["SUBJ-001"]


def test_invalid_ai_output_is_rejected() -> None:
    analysis = _analysis(_requirement())

    with pytest.raises(InvalidAIResponseError):
        generate_test_cases(analysis, client=FakeClient("not json"))

    with pytest.raises(InvalidAIResponseError):
        generate_test_cases(analysis, client=FakeClient({"test_cases": {"Age": 30}}))

    with pytest.raises(InvalidAIResponseError, match="category"):
        generate_test_cases(
            analysis,
            client=FakeClient({"test_cases": [_case(category="exploratory")]}),
        )

    with pytest.raises(InvalidAIResponseError, match="confidence"):
        generate_test_cases(
            analysis,
            client=FakeClient({"test_cases": [_case(confidence=1.5)]}),
        )

    approved = _case()
    approved["approved"] = True
    with pytest.raises(InvalidAIResponseError, match="approved"):
        generate_test_cases(analysis, client=FakeClient({"test_cases": [approved]}))


def test_unknown_requirement_id_is_rejected() -> None:
    suite, _client = _generate(
        _analysis(_requirement()),
        {"test_cases": [_case("VAL-999"), _case()]},
    )

    assert suite.metadata.rejected_count == 1
    assert [case.requirement_id for case in suite.test_cases] == ["VAL-002"]
    assert any("VAL-999" in warning for warning in suite.metadata.warnings)


def test_duplicate_test_cases_are_dropped() -> None:
    suite, _client = _generate(
        _analysis(_requirement()),
        {
            "test_cases": [
                _case(),
                _case(title="Same input again"),
                _case(input_data={"Age": 40}, title="Different input"),
            ]
        },
    )

    assert [case.input_data["Age"] for case in suite.test_cases] == [30, 40]
    assert suite.test_cases[0].test_case_id == "TC-VAL-002-positive-01"
    assert suite.test_cases[1].test_case_id == "TC-VAL-002-positive-02"
    assert any("Duplicate" in warning for warning in suite.metadata.warnings)


def test_source_requirement_traceability_is_copied_from_the_analysis() -> None:
    behavior = "Accept 18\u201365; validate values below 18 or above 65."
    requirement = _requirement(expected_behavior=behavior)
    suite, client = _generate(_analysis(requirement), {"test_cases": [_case()]})
    case = suite.test_cases[0]

    assert case.source_file == "study.xlsx"
    assert case.source_sheet == "Validation_Rules"
    assert case.source_row == 6
    assert case.expected_behavior == behavior
    assert case.requirement_ids == ["VAL-002"]
    assert "VAL-002" in client.user_prompt
    assert requirement.condition in client.user_prompt
    assert "USSO" not in client.user_prompt
    assert "sk-" not in client.system_prompt


def test_ambiguous_requirement_flags_every_generated_case() -> None:
    requirement = _requirement(
        needs_human_review=True,
        ambiguity_notes="The upper bound is unclear.",
        confidence=0.4,
    )
    suite, _client = _generate(_analysis(requirement), {"test_cases": [_case()]})
    case = suite.test_cases[0]

    assert case.needs_human_review is True
    assert case.review_status == "pending_cdm_review"
    assert "already marked for CDM review" in (case.review_notes or "")
    assert "upper bound is unclear" in (case.review_notes or "")
    assert case.expected_behavior == requirement.expected_behavior


def test_required_date_accepts_an_iso_date_and_rejects_a_blank_or_non_date() -> None:
    requirement = _requirement(
        requirement_id="VAL-004",
        field_name="Screening Date",
        rule_type="date",
        condition="Screening date is required.",
        expected_behavior="Show a required-field message when the screening date is blank.",
        parameters=RequirementParameters(),
        source_row=8,
    )
    suite, _client = _generate(
        _analysis(requirement),
        {
            "test_cases": [
                _case(
                    "VAL-004",
                    category="date",
                    input_data={"Screening Date": None},
                    expected_result="Reject",
                ),
                _case(
                    "VAL-004",
                    category="date",
                    input_data={"Screening Date": "2026-10-01"},
                    expected_result="Accept",
                ),
                _case(
                    "VAL-004",
                    category="date",
                    input_data={"Screening Date": "yesterday"},
                    expected_result="Reject",
                ),
                _case(
                    "VAL-004",
                    category="date",
                    input_data={"Screening Date": "yesterday"},
                    expected_result="Accept",
                    title="Non-date accepted",
                ),
            ]
        },
    )

    blank, iso, invalid, unsupported = suite.test_cases
    assert blank.needs_human_review is False
    assert iso.needs_human_review is False
    assert invalid.needs_human_review is False
    assert unsupported.needs_human_review is True
    assert "non-date" in (unsupported.review_notes or "")


def test_empty_analysis_does_not_call_the_model() -> None:
    client = FakeClient({"test_cases": []})
    analysis = _analysis(
        uninterpreted_sources=[
            UninterpretedSource(
                requirement_id="Study_Protocol:5",
                field_name="Title",
                condition="Study title text.",
                expected_behavior="",
                source_sheet="Study_Protocol",
                source_row=5,
                note="No validation rule was stated.",
                source_values={"Item": "Title"},
            )
        ]
    )

    suite = generate_test_cases(analysis, client=client)

    assert client.calls == 0
    assert suite.test_cases == []
    assert suite.metadata.requirement_count == 0
    assert suite.metadata.category_counts["positive"] == 0


def test_category_counts_include_every_category() -> None:
    suite, _client = _generate(
        _analysis(_requirement()),
        {"test_cases": [_case(), _case(category="negative", input_data={"Age": 10}, expected_result="Reject")]},
    )

    assert suite.metadata.category_counts["positive"] == 1
    assert suite.metadata.category_counts["negative"] == 1
    assert suite.metadata.category_counts["boundary"] == 0
    assert suite.metadata.test_case_count == 2
    assert all(case.review_status == "pending_cdm_review" for case in suite.test_cases)


def test_generated_case_cannot_be_marked_approved() -> None:
    suite, _client = _generate(_analysis(_requirement()), {"test_cases": [_case()]})
    payload = suite.test_cases[0].model_dump()
    payload["review_status"] = "cdm_approved"
    with pytest.raises(ValidationError):
        GeneratedTestCase.model_validate(payload)
    with pytest.raises(ValidationError):
        GeneratedTestCase(
            test_case_id="TC-1",
            requirement_id="VAL-002",
            requirement_ids=["VAL-002"],
            title="Approved",
            description="Should not validate.",
            category="positive",
            input_data={"Age": 30},
            expected_result="Accept",
            expected_behavior="Accept 18-65.",
            source_sheet="Validation_Rules",
            source_row=6,
            rationale="",
            confidence=0.9,
            needs_human_review=False,
            review_status="cdm_approved",
        )
