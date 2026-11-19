"""Decide which generated test cases are allowed into a future execution plan.

This module does not run a browser and does not change generated test cases.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from models.approval import (
    ApprovalCheck,
    ApprovalDecision,
    ApprovalRecord,
    ApprovedCase,
    BlockedCase,
    ExecutionPlan,
)
from models.test_cases import GeneratedTestCase

REASON_NO_RECORD = "No approval record exists."
REASON_DECISION_REJECTED = "Decision is rejected."
REASON_DECISION_CHANGES = "Decision is changes requested."
REASON_REVIEWER_MISSING = "Reviewer name is missing."
REASON_BEHAVIOR_MISSING = "Approved expected behavior is missing."
REASON_FINGERPRINT = "Test case fingerprint does not match the approval record."
REASON_COMMENT = "Human review was required and the reviewer comment is missing."
REASON_WRONG_CASE = "Approval record is for a different test case."

_DECISION_REASONS = {
    "rejected": REASON_DECISION_REJECTED,
    "changes_requested": REASON_DECISION_CHANGES,
}


def generate_test_case_fingerprint(test_case: GeneratedTestCase) -> str:
    """Hash the fields that define this exact version of a test case."""

    payload = {
        "requirement_id": test_case.requirement_id,
        "requirement_ids": list(test_case.requirement_ids),
        "category": test_case.category,
        "input_data": test_case.input_data,
        "expected_result": test_case.expected_result,
        "expected_behavior": test_case.expected_behavior,
        "source_file": test_case.source_file,
        "source_sheet": test_case.source_sheet,
        "source_row": test_case.source_row,
    }
    # Empty classifications are omitted so cases generated before supporting
    # data existed keep the same fingerprint.
    if test_case.test_data_classification:
        payload["test_data_classification"] = test_case.test_data_classification
    if test_case.supporting_requirement_ids:
        payload["supporting_requirement_ids"] = test_case.supporting_requirement_ids
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def approve_test_case(
    test_case: GeneratedTestCase,
    *,
    reviewer_name: str,
    reviewer_role: str,
    decision: ApprovalDecision,
    comment: str,
    approved_expected_behavior: str,
    reviewed_at: datetime | None = None,
) -> ApprovalRecord:
    """Record a CDM decision without changing the generated test case.

    ``approved_expected_behavior`` is stored as the reviewer entered it. It is
    not copied back onto the test case.
    """

    return ApprovalRecord(
        test_case_id=test_case.test_case_id,
        decision=decision,
        reviewer_name=reviewer_name,
        reviewer_role=reviewer_role,
        reviewed_at=reviewed_at or datetime.now(timezone.utc),
        comment=comment,
        approved_expected_behavior=approved_expected_behavior,
        test_case_fingerprint=generate_test_case_fingerprint(test_case),
    )


def validate_approval(
    test_case: GeneratedTestCase,
    approval_record: ApprovalRecord,
) -> ApprovalCheck:
    """Return whether this record authorizes this exact test case."""

    reasons: list[str] = []
    if approval_record.test_case_id != test_case.test_case_id:
        reasons.append(REASON_WRONG_CASE)
    decision_reason = _DECISION_REASONS.get(approval_record.decision)
    if decision_reason is not None:
        reasons.append(decision_reason)
    if not approval_record.reviewer_name.strip():
        reasons.append(REASON_REVIEWER_MISSING)
    if not approval_record.approved_expected_behavior.strip():
        reasons.append(REASON_BEHAVIOR_MISSING)
    if approval_record.test_case_fingerprint != generate_test_case_fingerprint(test_case):
        reasons.append(REASON_FINGERPRINT)
    if test_case.needs_human_review and not approval_record.comment.strip():
        reasons.append(REASON_COMMENT)
    if reasons:
        return ApprovalCheck(accepted=False, reasons=reasons)
    return ApprovalCheck(accepted=True, reasons=[])


def build_execution_plan(
    test_cases: list[GeneratedTestCase],
    approval_records: list[ApprovalRecord],
) -> ExecutionPlan:
    """Split cases into approved and blocked. Blocked cases stay out of execution.

    When several records share a test case ID, the latest ``reviewed_at`` wins.
    A tie uses the later record in ``approval_records``.
    """

    grouped: dict[str, list[ApprovalRecord]] = {}
    for record in approval_records:
        grouped.setdefault(record.test_case_id, []).append(record)

    approved: list[ApprovedCase] = []
    blocked: list[BlockedCase] = []
    for test_case in test_cases:
        matches = grouped.get(test_case.test_case_id, [])
        if not matches:
            blocked.append(BlockedCase(test_case=test_case, reasons=[REASON_NO_RECORD]))
            continue
        record = _latest_record(matches)
        check = validate_approval(test_case, record)
        if check.accepted:
            approved.append(
                ApprovedCase(
                    test_case=test_case,
                    approval=record,
                    execution_expected_behavior=record.approved_expected_behavior,
                )
            )
            continue
        blocked.append(BlockedCase(test_case=test_case, reasons=list(check.reasons)))
    return ExecutionPlan(approved_cases=approved, blocked_cases=blocked)


def _latest_record(records: list[ApprovalRecord]) -> ApprovalRecord:
    return max(enumerate(records), key=lambda item: (item[1].reviewed_at, item[0]))[1]
