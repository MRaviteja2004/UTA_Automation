"""Structured interpretations of source protocol and validation rows.

These models describe an AI reading of text that already exists in the
workbook. They do not replace that text, and they are never an approval.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from models.schemas import CellValue

ReviewStatus = Literal["pending_cdm_review"]


class RequirementParameters(BaseModel):
    """Limits and coded values taken from a source sentence."""

    model_config = ConfigDict(extra="forbid", strict=True)

    minimum: int | float | None = None
    maximum: int | float | None = None
    minimum_inclusive: bool | None = None
    maximum_inclusive: bool | None = None
    allowed_values: list[str] | None = None
    related_field: str | None = None


class AnalyzedRequirement(BaseModel):
    """One interpretation tied to a single Excel row."""

    model_config = ConfigDict(extra="forbid", strict=True)

    requirement_id: str = Field(min_length=1)
    field_name: str
    rule_type: str = Field(min_length=1)
    condition: str
    expected_behavior: str
    parameters: RequirementParameters
    source_file: str | None = None
    source_sheet: str = Field(min_length=1)
    source_row: int = Field(ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    needs_human_review: bool
    review_status: ReviewStatus = "pending_cdm_review"
    ambiguity_notes: str | None = None
    source_values: dict[str, CellValue]


class UninterpretedSource(BaseModel):
    """A source row the model did not classify.

    The original wording is kept. No rule type is assigned.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    requirement_id: str = Field(min_length=1)
    field_name: str
    condition: str
    expected_behavior: str
    source_file: str | None = None
    source_sheet: str = Field(min_length=1)
    source_row: int = Field(ge=1)
    needs_human_review: bool = True
    review_status: ReviewStatus = "pending_cdm_review"
    note: str
    source_values: dict[str, CellValue]


class RequirementAnalysis(BaseModel):
    """The full interpretation of one workbook's protocol and validation rows."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_file: str | None = None
    requirements: list[AnalyzedRequirement]
    uninterpreted_sources: list[UninterpretedSource]
    warnings: list[str]
