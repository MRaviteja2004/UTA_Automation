"""CDM approval gate tests. No live AI or Google API calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from execution.approval_gate import (
    REASON_BEHAVIOR_MISSING,
    REASON_COMMENT,
    REASON_DECISION_CHANGES,
    REASON_DECISION_REJECTED,
    REASON_FINGERPRINT,
    REASON_NO_RECORD,
    REASON_REVIEWER_MISSING,
    approve_test_case,
    build_execution_plan,
    generate_test_case_fingerprint,
    validate_approval,
)
from models.test_cases import GeneratedTestCase

REVIEWED_AT = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
AI_BEHAVIOR = "Accept 18\u201365; validate values below 18 or above 65."
CDM_BEHAVIOR = "CDM confirms: accept an integer age from 18 through 65 inclusive."


def _case(**overrides: object) -> GeneratedTestCase:
    data: dict[str, object] = {
        "test_case_id": "TC-VAL-002-positive-01",
        "requirement_id": "VAL-002",
        "requirement_ids": ["VAL-002"],
        "title": "Age 30",
        "description": "Synthetic age inside the stated range.",
        "category": "positive",
        "input_data": {"Age": 30},
        "expected_result": "Accept",
        "expected_behavior": AI_BEHAVIOR,
        "source_file": "study.xlsx",
        "source_sheet": "Validation_Rules",
        "source_row": 6,
        "rationale": "30 is inside the stated inclusive range.",
        "confidence": 0.95,
        "needs_human_review": False,
    }
    data.update(overrides)
    return GeneratedTestCase(**data)


def _approve(case: GeneratedTestCase, **overrides: object):
    details: dict[str, object] = {
        "reviewer_name": "A. Reviewer",
        "reviewer_role": "Clinical Data Manager",
        "decision": "approved",
        "comment": "Confirmed against the protocol wording.",
        "approved_expected_behavior": CDM_BEHAVIOR,
        "reviewed_at": REVIEWED_AT,
    }
    details.update(overrides)
    return approve_test_case(case, **details)


def test_valid_approval_is_accepted() -> None:
    case = _case()
    before = case.model_dump()
    record = _approve(case)
    check = validate_approval(case, record)
    plan = build_execution_plan([case], [record])

    assert check.accepted is True
    assert check.reasons == []
    assert record.test_case_fingerprint == generate_test_case_fingerprint(case)
    assert record.approved_expected_behavior == CDM_BEHAVIOR
    assert case.expected_behavior == AI_BEHAVIOR
    assert case.review_status == "pending_cdm_review"
    assert case.model_dump() == before
    assert len(plan.approved_cases) == 1
    assert plan.blocked_cases == []
    approved = plan.approved_cases[0]
    assert approved.execution_expected_behavior == CDM_BEHAVIOR
    assert approved.test_case.expected_behavior == AI_BEHAVIOR
    assert generate_test_case_fingerprint(case) == generate_test_case_fingerprint(
        _case(input_data={"Age": 30})
    )


def test_missing_approval_is_blocked() -> None:
    plan = build_execution_plan([_case()], [])

    assert plan.approved_cases == []
    assert plan.blocked_cases[0].reasons == [REASON_NO_RECORD]


def test_rejected_case_is_blocked() -> None:
    case = _case()
    record = _approve(case, decision="rejected")
    plan = build_execution_plan([case], [record])

    assert plan.approved_cases == []
    assert plan.blocked_cases[0].reasons == [REASON_DECISION_REJECTED]


def test_changes_requested_case_is_blocked() -> None:
    case = _case()
    record = _approve(case, decision="changes_requested")
    plan = build_execution_plan([case], [record])

    assert plan.approved_cases == []
    assert plan.blocked_cases[0].reasons == [REASON_DECISION_CHANGES]


def test_missing_reviewer_is_blocked() -> None:
    case = _case()
    record = _approve(case, reviewer_name="  ")
    check = validate_approval(case, record)

    assert check.accepted is False
    assert REASON_REVIEWER_MISSING in check.reasons


def test_missing_approved_expected_behavior_is_blocked() -> None:
    case = _case()
    record = _approve(case, approved_expected_behavior="")
    check = validate_approval(case, record)

    assert check.accepted is False
    assert REASON_BEHAVIOR_MISSING in check.reasons
    assert case.expected_behavior == AI_BEHAVIOR


def test_empty_classification_keeps_the_existing_fingerprint() -> None:
    case = _case()

    assert case.test_data_classification == {}
    assert case.supporting_requirement_ids == {}
    assert (
        generate_test_case_fingerprint(case)
        == "029b413989d7376e64f8aed568a24cb6e36df2d68d76d9a8099952b076540bc7"
    )


def test_supporting_values_and_classification_change_the_fingerprint() -> None:
    case = _case()
    record = _approve(case)
    completed = _case(
        input_data={"Age": 30, "Subject ID": "SYN-001"},
        test_data_classification={"Age": "under_test", "Subject ID": "supporting"},
        supporting_requirement_ids={"Subject ID": ["VAL-001"]},
    )
    approved = _approve(completed)
    changed_value = completed.model_validate(
        {
            **completed.model_dump(),
            "input_data": {"Age": 30, "Subject ID": "SYN-002"},
        }
    )
    changed_role = completed.model_validate(
        {
            **completed.model_dump(),
            "test_data_classification": {
                "Age": "under_test",
                "Subject ID": "under_test",
            },
            "supporting_requirement_ids": {},
        }
    )

    assert generate_test_case_fingerprint(completed) != record.test_case_fingerprint
    assert validate_approval(changed_value, approved).accepted is False
    assert validate_approval(changed_value, approved).reasons == [REASON_FINGERPRINT]
    assert generate_test_case_fingerprint(changed_role) != approved.test_case_fingerprint
    assert completed.review_status == "pending_cdm_review"


def test_modified_test_case_fails_fingerprint_check() -> None:
    case = _case()
    record = _approve(case)
    modified = case.model_validate({**case.model_dump(), "input_data": {"Age": 31}})
    same_content = _case(input_data={"b": 1, "a": 2})
    reordered = _case(input_data={"a": 2, "b": 1})

    check = validate_approval(modified, record)

    assert check.accepted is False
    assert check.reasons == [REASON_FINGERPRINT]
    assert generate_test_case_fingerprint(modified) != record.test_case_fingerprint
    assert generate_test_case_fingerprint(same_content) == generate_test_case_fingerprint(
        reordered
    )


def test_human_review_without_comment_is_blocked() -> None:
    case = _case(needs_human_review=True, review_notes="The upper bound is unclear.")
    missing_comment = _approve(case, comment="  ")
    with_comment = _approve(case, comment="Reviewed the upper bound with the protocol.")

    blocked = validate_approval(case, missing_comment)
    accepted = validate_approval(case, with_comment)

    assert blocked.accepted is False
    assert blocked.reasons == [REASON_COMMENT]
    assert accepted.accepted is True


def test_execution_plan_includes_only_approved_cases() -> None:
    approved_case = _case()
    rejected_case = _case(test_case_id="TC-VAL-002-negative-02", input_data={"Age": 10})
    unreviewed_case = _case(
        test_case_id="TC-VAL-002-boundary-03",
        category="boundary",
        input_data={"Age": 18},
    )
    records = [
        _approve(approved_case),
        _approve(rejected_case, decision="rejected"),
    ]
    plan = build_execution_plan(
        [approved_case, rejected_case, unreviewed_case],
        records,
    )

    assert [item.test_case.test_case_id for item in plan.approved_cases] == [
        "TC-VAL-002-positive-01"
    ]
    assert [item.test_case.test_case_id for item in plan.blocked_cases] == [
        "TC-VAL-002-negative-02",
        "TC-VAL-002-boundary-03",
    ]
    assert plan.approved_cases[0].execution_expected_behavior == CDM_BEHAVIOR
    assert plan.approved_cases[0].test_case.review_status == "pending_cdm_review"


def test_blocked_cases_include_clear_reasons() -> None:
    plain = _case()
    rejected = _case(test_case_id="TC-rejected", input_data={"Age": 10})
    changes = _case(test_case_id="TC-changes", input_data={"Age": 17})
    no_reviewer = _case(test_case_id="TC-reviewer", input_data={"Age": 65})
    no_behavior = _case(test_case_id="TC-behavior", input_data={"Age": 66})
    stale = _case(test_case_id="TC-stale", input_data={"Age": 40})
    flagged = _case(
        test_case_id="TC-flagged",
        input_data={"Age": 18},
        needs_human_review=True,
    )
    unreviewed = _case(test_case_id="TC-missing", input_data={"Age": 64})
    records = [
        _approve(rejected, decision="rejected"),
        _approve(changes, decision="changes_requested"),
        _approve(no_reviewer, reviewer_name=""),
        _approve(no_behavior, approved_expected_behavior=" "),
        _approve(stale),
        _approve(flagged, comment=""),
    ]
    changed = stale.model_validate({**stale.model_dump(), "source_row": 7})
    plan = build_execution_plan(
        [unreviewed, rejected, changes, no_reviewer, no_behavior, changed, flagged],
        records,
    )

    reasons = {
        item.test_case.test_case_id: item.reasons for item in plan.blocked_cases
    }
    assert reasons["TC-missing"] == [REASON_NO_RECORD]
    assert reasons["TC-rejected"] == [REASON_DECISION_REJECTED]
    assert reasons["TC-changes"] == [REASON_DECISION_CHANGES]
    assert reasons["TC-reviewer"] == [REASON_REVIEWER_MISSING]
    assert reasons["TC-behavior"] == [REASON_BEHAVIOR_MISSING]
    assert reasons["TC-stale"] == [REASON_FINGERPRINT]
    assert reasons["TC-flagged"] == [REASON_COMMENT]
    assert plan.approved_cases == []


def test_later_decision_replaces_an_earlier_approval() -> None:
    case = _case()
    earlier = _approve(case, reviewed_at=REVIEWED_AT)
    later = _approve(
        case,
        decision="rejected",
        reviewed_at=REVIEWED_AT + timedelta(hours=1),
        comment="Withdrawn after protocol check.",
    )
    plan = build_execution_plan([case], [earlier, later])

    assert plan.approved_cases == []
    assert plan.blocked_cases[0].reasons == [REASON_DECISION_REJECTED]
