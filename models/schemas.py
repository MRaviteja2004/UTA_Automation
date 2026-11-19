"""Structured records extracted from a study workbook.

Cell values are preserved as read from Excel. This schema checks the shape of
an extracted row. It does not apply clinical validation rules.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Types OpenPyXL returns for ordinary cells. ``bool`` is listed before ``int``
# because ``bool`` is a subclass of ``int``.
CellValue = bool | int | float | str | date | datetime | None


class ExtractedRow(BaseModel):
    """One non-empty worksheet row, with its Excel row number."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sheet_name: str = Field(min_length=1)
    excel_row: int = Field(ge=1)
    values: dict[str, CellValue]


class ProtocolRequirement(ExtractedRow):
    """One row from the Study_Protocol sheet."""

    section: CellValue = None
    item: CellValue = None
    requirement: CellValue = Field(
        default=None,
        description="Value from the 'Sample protocol requirement' column.",
    )


class ValidationRule(ExtractedRow):
    """One row from the Validation_Rules sheet."""

    rule_id: CellValue = None
    form_section: CellValue = Field(
        default=None,
        description="Value from the 'Form / Section' column.",
    )
    field: CellValue = None
    condition: CellValue = Field(
        default=None,
        description="Value from the 'Requirement / Condition' column.",
    )
    expected_edc_behavior: CellValue = Field(
        default=None,
        description="Value from the 'Expected EDC Behavior' column.",
    )
    positive_example: CellValue = None
    negative_example: CellValue = None


class SampleTestCase(ExtractedRow):
    """One row from the Sample_Test_Data sheet."""

    test_case_id: CellValue = None
    subject_id: CellValue = None
    age: CellValue = None
    gender: CellValue = None
    consent: CellValue = None
    screening_date: CellValue = None
    follow_up_date: CellValue = None
    height_cm: CellValue = Field(
        default=None,
        description="Value from the 'Height (cm)' column.",
    )
    weight_kg: CellValue = Field(
        default=None,
        description="Value from the 'Weight (kg)' column.",
    )


class SheetTable(BaseModel):
    """A detected table inside one worksheet."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sheet_name: str = Field(min_length=1)
    header_excel_row: int = Field(ge=1)
    column_names: list[str]
    notes: list[str] = Field(default_factory=list)
    rows: list[Any] = Field(default_factory=list)


class IngestedWorkbook(BaseModel):
    """All three required sheets from one uploaded workbook."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_name: str | None = None
    detected_sheets: list[str]
    study_protocol: SheetTable
    validation_rules: SheetTable
    sample_test_data: SheetTable
    protocol_requirements: list[ProtocolRequirement]
    validation_rule_records: list[ValidationRule]
    sample_test_records: list[SampleTestCase]
