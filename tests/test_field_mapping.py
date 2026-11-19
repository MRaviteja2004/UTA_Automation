"""Form field mapping tests. No browser, Google API, or live AI calls."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from execution.approval_gate import approve_test_case
from execution.field_mapping import (
    load_form_mapping,
    map_execution_plan,
    map_test_case_inputs,
    validate_field_mapping,
)
from models.approval import ApprovedCase, BlockedCase, ExecutionPlan
from models.test_cases import GeneratedTestCase

REVIEWED_AT = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CDM_BEHAVIOR = "CDM confirms the stated form behavior."


def _payload() -> dict:
    mapping = load_form_mapping()
    return mapping.model_dump()


def _case(test_case_id: str = "TC-VAL-001-mandatory-01", **overrides: object) -> GeneratedTestCase:
    data: dict[str, object] = {
        "test_case_id": test_case_id,
        "requirement_id": "VAL-001",
        "requirement_ids": ["VAL-001"],
        "title": "Synthetic screening case",
        "description": "Synthetic values for the demo form.",
        "category": "positive",
        "input_data": {
            "Subject ID": "SUBJ-001",
            "Age": 30,
            "Gender": "Female",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-01",
            "Follow-up Date": "2026-10-08",
            "Height (cm)": 165,
            "Weight (kg)": 60,
        },
        "expected_result": "Accept",
        "expected_behavior": "Accept a complete synthetic screening record.",
        "source_file": "study.xlsx",
        "source_sheet": "Validation_Rules",
        "source_row": 5,
        "rationale": "Values use the stated fields.",
        "confidence": 0.95,
        "needs_human_review": False,
    }
    data.update(overrides)
    return GeneratedTestCase(**data)


def _approved(case: GeneratedTestCase | None = None) -> ApprovedCase:
    test_case = case or _case()
    record = approve_test_case(
        test_case,
        reviewer_name="A. Reviewer",
        reviewer_role="Clinical Data Manager",
        decision="approved",
        comment="Confirmed for the demo form.",
        approved_expected_behavior=CDM_BEHAVIOR,
        reviewed_at=REVIEWED_AT,
    )
    return ApprovedCase(
        test_case=test_case,
        approval=record,
        execution_expected_behavior=record.approved_expected_behavior,
    )


def test_valid_mapping_accepts_the_demo_form() -> None:
    mapping = load_form_mapping()

    assert validate_field_mapping(mapping) == []
    titles = {item.source_field: item.form_question_title for item in mapping.mappings}
    assert titles == {
        "subject_id": "Subject ID",
        "age": "Age",
        "gender": "Gender",
        "informed_consent": "Informed Consent",
        "screening_date": "Screening Date",
        "follow_up_date": "Follow-up Date",
        "height_cm": "Height (cm)",
        "weight_kg": "Weight (kg)",
    }
    types = {item.source_field: item.form_question_type for item in mapping.mappings}
    assert types["gender"] == "dropdown"
    assert types["informed_consent"] == "multiple_choice"
    assert types["screening_date"] == "date"
    required = {item.source_field: item.required for item in mapping.mappings}
    assert required["age"] is True
    assert required["gender"] is True


def test_missing_required_mapping_is_blocked() -> None:
    payload = _payload()
    payload["mappings"] = [
        item for item in payload["mappings"] if item["source_field"] != "screening_date"
    ]
    disabled = _payload()
    for item in disabled["mappings"]:
        if item["source_field"] == "subject_id":
            item["enabled"] = False

    missing = validate_field_mapping(payload)
    cannot_receive = validate_field_mapping(disabled)

    assert "Required source field 'screening_date' has no mapping." in missing
    assert any("cannot receive a value" in error for error in cannot_receive)
    assert "Required source field 'subject_id' has no mapping." in cannot_receive


def test_duplicate_source_field_is_blocked() -> None:
    payload = _payload()
    payload["mappings"].append(dict(payload["mappings"][0]))

    errors = validate_field_mapping(payload)

    assert "Source field 'subject_id' is mapped more than once." in errors


def test_unknown_input_data_field_is_blocked() -> None:
    payload = _payload()
    payload["mappings"].append(
        {
            "source_field": "blood_pressure",
            "form_question_title": "Blood Pressure",
            "form_question_type": "short_text",
            "required": False,
            "enabled": True,
        }
    )
    case = _case(
        input_data={
            **_case().input_data,
            "Eligibility Status": "Eligible",
        }
    )
    result = map_test_case_inputs(_approved(case), load_form_mapping())

    assert "Mapping references unknown input_data field 'blood_pressure'." in validate_field_mapping(
        payload
    )
    assert result.status == "blocked"
    assert result.unmapped_source_fields == ["Eligibility Status"]
    assert "Unknown input_data field 'Eligibility Status'." in result.validation_errors


def test_empty_question_title_is_blocked() -> None:
    payload = _payload()
    payload["mappings"][0]["form_question_title"] = "   "

    errors = validate_field_mapping(payload)

    assert "Mapping 1 has an empty form question title." in errors


def test_unsupported_question_type_is_blocked() -> None:
    payload = _payload()
    payload["mappings"][1]["form_question_type"] = "file_upload"

    errors = validate_field_mapping(payload)

    assert "Mapping 2 uses unsupported question type 'file_upload'." in errors


def test_none_value_is_preserved() -> None:
    case = _case(
        input_data={
            "Subject ID": None,
            "Age": 30,
            "Gender": "Female",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-01",
            "condition_met": False,
        }
    )
    result = map_test_case_inputs(_approved(case), load_form_mapping())
    subject = next(item for item in result.mapped_inputs if item.source_field == "subject_id")

    assert result.status == "ready"
    assert subject.value is None
    assert subject.form_question_title == "Subject ID"
    assert "condition_met" not in result.unmapped_source_fields
    assert all(item.value is not None or item.source_field == "subject_id" for item in result.mapped_inputs)


def test_approved_case_maps_successfully() -> None:
    result = map_test_case_inputs(_approved(), load_form_mapping())
    mapped = {item.form_question_title: item.value for item in result.mapped_inputs}

    assert result.status == "ready"
    assert result.validation_errors == []
    assert result.unmapped_source_fields == []
    assert mapped["Subject ID"] == "SUBJ-001"
    assert mapped["Age"] == 30
    assert mapped["Gender"] == "Female"
    assert mapped["Informed Consent"] == "Yes"
    assert mapped["Screening Date"] == "2026-10-01"
    assert mapped["Follow-up Date"] == "2026-10-08"
    assert mapped["Height (cm)"] == 165
    assert mapped["Weight (kg)"] == 60


def test_missing_age_or_gender_blocks_mapping() -> None:
    full = _case().input_data
    without_age = {key: value for key, value in full.items() if key != "Age"}
    without_gender = {key: value for key, value in full.items() if key != "Gender"}

    missing_age = map_test_case_inputs(_approved(_case(input_data=without_age)), load_form_mapping())
    missing_gender = map_test_case_inputs(
        _approved(_case("TC-NO-GENDER", input_data=without_gender)),
        load_form_mapping(),
    )

    assert missing_age.status == "blocked"
    assert any(
        "Age" in error and "cannot receive a value" in error
        for error in missing_age.validation_errors
    )
    assert all(item.form_question_title != "Age" for item in missing_age.mapped_inputs)
    assert missing_gender.status == "blocked"
    assert any(
        "Gender" in error and "cannot receive a value" in error
        for error in missing_gender.validation_errors
    )
    assert "Female" not in {item.value for item in missing_gender.mapped_inputs}


def test_blocked_case_cannot_be_mapped() -> None:
    generated = _case()
    blocked = BlockedCase(test_case=generated, reasons=["Decision is rejected."])
    plan = ExecutionPlan(approved_cases=[], blocked_cases=[blocked])

    direct = map_test_case_inputs(blocked, load_form_mapping())
    raw = map_test_case_inputs(generated, load_form_mapping())
    planned = map_execution_plan(plan, load_form_mapping())

    assert direct.status == "blocked"
    assert direct.mapped_inputs == []
    assert "Blocked cases cannot be mapped." in direct.validation_errors[0]
    assert raw.status == "blocked"
    assert raw.mapped_inputs == []
    assert planned == []


def test_multiple_approved_cases_map_independently() -> None:
    ready_case = _approved(_case("TC-ready"))
    other_inputs = dict(_case().input_data)
    other_inputs["Age"] = 17
    other_inputs["Eligibility Status"] = "Eligible"
    blocked_case = _approved(_case("TC-blocked", input_data=other_inputs))
    plan = ExecutionPlan(
        approved_cases=[ready_case, blocked_case],
        blocked_cases=[
            BlockedCase(test_case=_case("TC-not-approved"), reasons=["No approval record exists."])
        ],
    )

    results = map_execution_plan(plan, load_form_mapping())

    assert [item.test_case_id for item in results] == ["TC-ready", "TC-blocked"]
    assert results[0].status == "ready"
    assert {item.form_question_title: item.value for item in results[0].mapped_inputs}["Age"] == 30
    assert results[1].status == "blocked"
    assert "Eligibility Status" in results[1].unmapped_source_fields
    assert results[0].validation_errors == []
    assert json.dumps(results[0].model_dump(mode="json")) != json.dumps(
        results[1].model_dump(mode="json")
    )
