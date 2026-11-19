"""Results of a browser run.

Filling or submitting a form is not a clinical pass or fail. The approved
expected behavior is kept so it can be compared with observed form behavior.
"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.test_cases import InputValue

ExecutionMode = Literal["FILL_ONLY"]
ExecutionStatus = Literal["filled", "submitted", "blocked", "error"]
ExecutionTarget = Literal["local_fixture", "google_form"]


class ExecutionResult(BaseModel):
    """What one fill-only run entered, read back, and captured."""

    model_config = ConfigDict(extra="forbid", strict=True)

    test_case_id: str = Field(min_length=1)
    execution_mode: ExecutionMode = "FILL_ONLY"
    execution_status: ExecutionStatus
    execution_target: ExecutionTarget = "local_fixture"
    target_url: str | None = None
    entered_values: dict[str, InputValue]
    observed_values: dict[str, str]
    expected_behavior: str
    screenshot_path: str | None = None
    before_screenshot_path: str | None = None
    evidence_path: str | None = None
    verification_notes: list[str] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime
    error_message: str | None = None

    @model_validator(mode="after")
    def timezone_and_order(self) -> Self:
        for label, value in ("started_at", self.started_at), ("completed_at", self.completed_at):
            if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
                raise ValueError(f"{label} must include a timezone")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at is earlier than started_at")
        return self
