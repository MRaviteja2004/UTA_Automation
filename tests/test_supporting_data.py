"""Supporting baseline values for required form fields. No browser or live AI calls."""

from __future__ import annotations

from datetime import datetime, timezone

from execution.approval_gate import approve_test_case, generate_test_case_fingerprint
from execution.field_mapping import load_form_mapping, map_test_case_inputs
from models.approval import ApprovedCase
from models.supporting import SupportingData
from models.test_cases import GeneratedTestCase

REVIEWED_AT = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CDM_BEHAVIOR = "CDM confirms the age boundary behavior."
EXPLANATION = "Synthetic values for required form fields that this case does not test."


def _case(**overrides: object) -> GeneratedTestCase:
    data: dict[str, object] = {
        "test_case_id": "TC-VAL-002-boundary-01",
        "requirement_id": "VAL-002",
        "requirement_ids": ["VAL-002"],
        "title": "Age 18",
        "description": "Inclusive minimum age.",
        "category": "boundary",
        "input_data": {"Age": 18},
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


def _approved(case: GeneratedTestCase | None = None) -> ApprovedCase:
    test_case = case or _case()
    record = approve_test_case(
        test_case,
        reviewer_name="A. Reviewer",
        reviewer_role="Clinical Data Manager",
        decision="approved",
        comment="Boundary confirmed.",
        approved_expected_behavior=CDM_BEHAVIOR,
        reviewed_at=REVIEWED_AT,
    )
    return ApprovedCase(
        test_case=test_case,
        approval=record,
        execution_expected_behavior=record.approved_expected_behavior,
    )


def _support(case: ApprovedCase, **overrides: object) -> SupportingData:
    data: dict[str, object] = {
        "test_case_id": case.test_case.test_case_id,
        "supporting_values": {
            "Subject ID": "SUBJ-001",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-20",
        },
        "source": EXPLANATION,
        "confirmed": True,
    }
    data.update(overrides)
    return SupportingData(**data)


def _values(result) -> dict[str, object]:
    return {item.form_question_title: item.value for item in result.mapped_inputs}


def test_age_only_case_stays_blocked_when_gender_is_missing() -> None:
    approved = _approved()
    result = map_test_case_inputs(approved, load_form_mapping(), _support(approved))

    assert result.status == "blocked"
    assert _values(result)["Age"] == 18
    assert _values(result)["Subject ID"] == "SUBJ-001"
    assert any(
        "Gender" in error and "cannot receive a value" in error
        for error in result.validation_errors
    )
    assert "Female" not in _values(result).values()
    assert result.original_inputs == {"Age": 18}


def test_case_with_age_and_gender_is_ready_with_confirmed_supporting_values() -> None:
    approved = _approved(_case(input_data={"Age": 18, "Gender": "Female"}))
    result = map_test_case_inputs(approved, load_form_mapping(), _support(approved))
    origins = {item.form_question_title: item.origin for item in result.mapped_inputs}

    assert result.status == "ready"
    assert result.validation_errors == []
    assert _values(result)["Age"] == 18
    assert _values(result)["Gender"] == "Female"
    assert _values(result)["Subject ID"] == "SUBJ-001"
    assert _values(result)["Informed Consent"] == "Yes"
    assert _values(result)["Screening Date"] == "2026-10-20"
    assert origins["Age"] == "test_input"
    assert origins["Gender"] == "test_input"
    assert origins["Subject ID"] == "supporting"
    assert result.original_inputs == {"Age": 18, "Gender": "Female"}


def test_original_test_input_takes_precedence() -> None:
    approved = _approved()
    support = _support(
        approved,
        supporting_values={
            "Age": 65,
            "Subject ID": "SUBJ-001",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-20",
        },
    )
    result = map_test_case_inputs(approved, load_form_mapping(), support)

    assert result.status == "blocked"
    assert _values(result)["Age"] == 18
    assert any("conflicts with the original test input" in error for error in result.validation_errors)
    assert all(item.origin != "supporting" or item.form_question_title != "Age" for item in result.mapped_inputs)


def test_intentional_none_remains_blank() -> None:
    approved = _approved(_case(input_data={"Age": 18, "Gender": "Female", "Subject ID": None}))
    omitted = _support(
        approved,
        supporting_values={
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-20",
        },
    )
    attempted = _support(approved)
    kept = map_test_case_inputs(approved, load_form_mapping(), omitted)
    blocked = map_test_case_inputs(approved, load_form_mapping(), attempted)

    assert kept.status == "ready"
    assert _values(kept)["Subject ID"] is None
    assert blocked.status == "blocked"
    assert _values(blocked)["Subject ID"] is None
    assert "SUBJ-001" not in _values(blocked).values()
    assert any("conflicts with the original test input" in error for error in blocked.validation_errors)


def test_missing_baseline_values_block_mapping() -> None:
    approved = _approved()
    support = _support(approved, supporting_values={"Subject ID": "SUBJ-001"})
    result = map_test_case_inputs(approved, load_form_mapping(), support)

    assert result.status == "blocked"
    assert any("Informed Consent" in error and "cannot receive a value" in error for error in result.validation_errors)
    assert any("Screening Date" in error and "cannot receive a value" in error for error in result.validation_errors)


def test_baseline_for_another_case_is_rejected() -> None:
    approved = _approved()
    support = _support(approved, test_case_id="TC-OTHER")
    result = map_test_case_inputs(approved, load_form_mapping(), support)

    assert result.status == "blocked"
    assert "Supporting data is for a different test case." in result.validation_errors
    assert "SUBJ-001" not in _values(result).values()


def test_unconfirmed_baseline_is_rejected() -> None:
    approved = _approved()
    support = _support(approved, confirmed=False)
    result = map_test_case_inputs(approved, load_form_mapping(), support)

    assert result.status == "blocked"
    assert "Supporting baseline data has not been confirmed by a reviewer." in result.validation_errors
    assert result.supporting_inputs == {}
    assert "SUBJ-001" not in _values(result).values()


def test_generated_supporting_values_map_without_reviewer_baseline() -> None:
    approved = _approved(
        _case(
            input_data={
                "Age": 18,
                "Gender": "Female",
                "Subject ID": "SYN-001",
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
            },
            test_data_classification={
                "Age": "under_test",
                "Gender": "under_test",
                "Subject ID": "supporting",
                "Informed Consent": "supporting",
                "Screening Date": "supporting",
            },
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        )
    )
    result = map_test_case_inputs(approved, load_form_mapping())
    origins = {item.form_question_title: item.origin for item in result.mapped_inputs}

    assert result.status == "ready"
    assert origins["Age"] == "test_input"
    assert origins["Subject ID"] == "generated_supporting"
    assert origins["Informed Consent"] == "generated_supporting"
    assert _values(result)["Age"] == 18


def test_reviewer_baseline_cannot_replace_generated_supporting_value() -> None:
    approved = _approved(
        _case(
            input_data={
                "Age": 18,
                "Gender": "Female",
                "Subject ID": "SYN-001",
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
            },
            test_data_classification={
                "Age": "under_test",
                "Gender": "under_test",
                "Subject ID": "supporting",
                "Informed Consent": "supporting",
                "Screening Date": "supporting",
            },
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        )
    )
    support = _support(
        approved,
        supporting_values={
            "Subject ID": "SUBJ-999",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-20",
        },
    )
    result = map_test_case_inputs(approved, load_form_mapping(), support)

    assert result.status == "blocked"
    assert _values(result)["Subject ID"] == "SYN-001"
    assert any("conflicts with the original test input" in error for error in result.validation_errors)
    assert all(
        item.origin != "supporting" or item.form_question_title != "Subject ID"
        for item in result.mapped_inputs
    )


def test_approval_fingerprint_and_expected_behavior_stay_unchanged() -> None:
    approved = _approved(_case(input_data={"Age": 18, "Gender": "Female"}))
    before_case = approved.test_case.model_dump()
    before_fingerprint = generate_test_case_fingerprint(approved.test_case)
    before_behavior = approved.execution_expected_behavior
    before_record = approved.approval.model_dump()

    result = map_test_case_inputs(approved, load_form_mapping(), _support(approved))

    assert result.status == "ready"
    assert approved.test_case.model_dump() == before_case
    assert generate_test_case_fingerprint(approved.test_case) == before_fingerprint
    assert approved.approval.test_case_fingerprint == before_fingerprint
    assert approved.execution_expected_behavior == before_behavior
    assert approved.approval.approved_expected_behavior == CDM_BEHAVIOR
    assert approved.approval.model_dump() == before_record
    assert approved.test_case.review_status == "pending_cdm_review"
