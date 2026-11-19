"""Read-only Google Form structure. This is not a clinical requirement set."""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1"
MetadataStatus = Literal["captured", "blocked", "error"]
ExtractedQuestionType = Literal[
    "short_text",
    "paragraph",
    "dropdown",
    "multiple_choice",
    "checkbox",
    "date",
    "unsupported",
    "unknown",
]
DetectionState = Literal["detected", "ambiguous", "not_detected", "not_applicable"]


class DateControlMetadata(BaseModel):
    """One accessible part of a date question."""

    model_config = ConfigDict(extra="forbid", strict=True)

    label: str
    role: str = ""
    input_type: str = ""
    detected: bool


class ExtractedFormQuestion(BaseModel):
    """Structure observed for one form question."""

    model_config = ConfigDict(extra="forbid", strict=True)

    position: int = Field(ge=1)
    visible_title: str
    accessible_label: str
    question_type: ExtractedQuestionType
    type_detection: DetectionState
    required: bool | None = None
    required_detection: DetectionState
    options: list[str] = Field(default_factory=list)
    options_detection: DetectionState
    date_controls: list[DateControlMetadata] = Field(default_factory=list)
    detection_notes: list[str] = Field(default_factory=list)


class GoogleFormMetadata(BaseModel):
    """A versioned snapshot of a Google Form's questions."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"] = SCHEMA_VERSION
    purpose: Literal["form_structure_only"] = "form_structure_only"
    form_url: str
    page_title: str = ""
    captured_at: datetime
    status: MetadataStatus
    questions: list[ExtractedFormQuestion] = Field(default_factory=list)
    metadata_hash: str = Field(min_length=64, max_length=64)
    diagnostics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def captured_at_has_timezone(self) -> Self:
        value = self.captured_at
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("captured_at must include a timezone")
        return self
