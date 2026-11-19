"""Generated EDC test cases tied to analyzed requirements.

Every case stays pending CDM review. Nothing in this module approves a case.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReviewStatus = Literal["pending_cdm_review"]
FieldRole = Literal["under_test", "supporting"]
SupportingGenerationState = Literal["complete", "partial", "failed", "deferred", "not_needed"]
InputValue = str | int | float | bool | None


class GeneratedTestCase(BaseModel):
    """One synthetic test case for a single analyzed requirement."""

    model_config = ConfigDict(extra="forbid", strict=True)

    test_case_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str
    category: str = Field(min_length=1)
    input_data: dict[str, InputValue]
    test_data_classification: dict[str, FieldRole] = Field(default_factory=dict)
    supporting_requirement_ids: dict[str, list[str]] = Field(default_factory=dict)
    expected_result: str
    expected_behavior: str
    source_file: str | None = None
    source_sheet: str = Field(min_length=1)
    source_row: int = Field(ge=1)
    rationale: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    needs_human_review: bool
    review_notes: str | None = None
    review_status: ReviewStatus = "pending_cdm_review"

    @model_validator(mode="after")
    def classification_matches_inputs(self) -> Self:
        classification = self.test_data_classification
        citations = self.supporting_requirement_ids
        if not classification and not citations:
            return self
        if not classification:
            raise ValueError("Supporting requirement ids require field classifications.")
        for key in classification:
            if _is_condition_flag(key):
                raise ValueError("condition_met is not classified as test data.")
            if key not in self.input_data:
                raise ValueError(f"Classification refers to unknown input '{key}'.")
        for key in self.input_data:
            if _is_condition_flag(key):
                continue
            if key not in classification:
                raise ValueError(f"Input field '{key}' has no classification.")
        for key, role in classification.items():
            cited = citations.get(key, [])
            if role == "supporting" and not cited:
                raise ValueError(f"Supporting field '{key}' needs a requirement id.")
            if role == "under_test" and cited:
                raise ValueError(f"Under-test field '{key}' cannot cite supporting requirements.")
        for key, cited in citations.items():
            if classification.get(key) != "supporting":
                raise ValueError(f"'{key}' is not classified as supporting data.")
            if any(not item.strip() for item in cited):
                raise ValueError(f"Supporting field '{key}' needs a requirement id.")
        return self


class GenerationMetadata(BaseModel):
    """How a test suite was produced."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_file: str | None = None
    model: str | None = None
    requirement_count: int = Field(ge=0)
    test_case_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    category_counts: dict[str, int]
    warnings: list[str]
    form_metadata_hash: str | None = None


class SupportingGenerationStatus(BaseModel):
    """Outcome of the supporting-data step. This is not an approval."""

    model_config = ConfigDict(extra="forbid", strict=True)

    total_cases: int = Field(ge=0)
    batch_size: int = Field(ge=1)
    completed_batches: int = Field(default=0, ge=0)
    pending_batches: int = Field(default=0, ge=0)
    failed_batches: int = Field(default=0, ge=0)
    deferred_batches: int = Field(default=0, ge=0)
    status: SupportingGenerationState
    deferred_case_ids: list[str] = Field(default_factory=list)
    failed_case_ids: list[str] = Field(default_factory=list)
    tokens_estimated: int = Field(default=0, ge=0)


class TestSuite(BaseModel):
    """All generated cases and the metadata for that run."""

    __test__ = False
    model_config = ConfigDict(extra="forbid", strict=True)

    test_cases: list[GeneratedTestCase]
    metadata: GenerationMetadata
    supporting_generation: SupportingGenerationStatus | None = None


def _is_condition_flag(key: str) -> bool:
    return " ".join(key.split()).casefold() in {"condition_met", "condition met"}
