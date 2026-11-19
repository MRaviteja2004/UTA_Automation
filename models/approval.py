"""CDM approval records kept apart from generated test cases.

A generated case always stays pending review. Approval lives in this module.
Future execution must read the CDM-approved expected behavior here, not the
AI-generated text on the test case.
"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.test_cases import GeneratedTestCase

ApprovalDecision = Literal["approved", "rejected", "changes_requested"]


class ApprovalRecord(BaseModel):
    """One reviewer decision for one version of a generated test case."""

    model_config = ConfigDict(extra="forbid", strict=True)

    test_case_id: str = Field(min_length=1)
    decision: ApprovalDecision
    reviewer_name: str
    reviewer_role: str
    reviewed_at: datetime
    comment: str
    approved_expected_behavior: str
    test_case_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def timezone_required(self) -> Self:
        reviewed_at = self.reviewed_at
        if reviewed_at.tzinfo is None or reviewed_at.tzinfo.utcoffset(reviewed_at) is None:
            raise ValueError("reviewed_at must include a timezone")
        return self


class ApprovalCheck(BaseModel):
    """Whether one approval record authorizes one test case."""

    model_config = ConfigDict(extra="forbid", strict=True)

    accepted: bool
    reasons: list[str]

    @model_validator(mode="after")
    def reasons_match_decision(self) -> Self:
        if self.accepted and self.reasons:
            raise ValueError("An accepted approval cannot include block reasons.")
        if not self.accepted and not self.reasons:
            raise ValueError("A blocked approval needs at least one reason.")
        return self


class ApprovedCase(BaseModel):
    """A generated case paired with the approval that authorizes it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    test_case: GeneratedTestCase
    approval: ApprovalRecord
    execution_expected_behavior: str = Field(min_length=1)

    @model_validator(mode="after")
    def use_cdm_behavior(self) -> Self:
        if self.approval.decision != "approved":
            raise ValueError("Approved cases require an approved decision.")
        if self.execution_expected_behavior != self.approval.approved_expected_behavior:
            raise ValueError("Execution must use the CDM-approved expected behavior.")
        if not self.execution_expected_behavior.strip():
            raise ValueError("Approved expected behavior is missing.")
        return self


class BlockedCase(BaseModel):
    """A generated case that must not be executed, with the reasons."""

    model_config = ConfigDict(extra="forbid", strict=True)

    test_case: GeneratedTestCase
    reasons: list[str] = Field(min_length=1)


class ExecutionPlan(BaseModel):
    """The only input a future browser runner is allowed to accept."""

    model_config = ConfigDict(extra="forbid", strict=True)

    approved_cases: list[ApprovedCase]
    blocked_cases: list[BlockedCase]
