"""Reviewer-supplied values for required form fields a case does not test.

This is not part of the generated test case and it is not a CDM approval.
"""

from pydantic import BaseModel, ConfigDict, Field

from models.test_cases import InputValue


class SupportingData(BaseModel):
    """Synthetic baseline values for one approved test case."""

    model_config = ConfigDict(extra="forbid", strict=True)

    test_case_id: str = Field(min_length=1)
    supporting_values: dict[str, InputValue]
    source: str
    confirmed: bool = False
