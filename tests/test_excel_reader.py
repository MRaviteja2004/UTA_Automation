"""Unit tests for study workbook ingestion."""

from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from ingestion.excel_reader import (
    SHEET_COLUMNS,
    DuplicateColumnError,
    InvalidExcelFileError,
    MissingColumnsError,
    MissingSheetError,
    read_workbook,
)

SAMPLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "sample_data"
    / "Sample_Clinical_Study_Protocol_and_Validation_Rules.xlsx"
)

PROTOCOL_ITEMS = [
    (5, "Study Overview", "Study ID"),
    (6, "Study Overview", "Study Title"),
    (7, "Study Overview", "Purpose"),
    (8, "Study Overview", "Population"),
    (9, "Eligibility", "Age"),
    (10, "Eligibility", "Informed Consent"),
    (11, "Eligibility", "Eligibility Status"),
    (12, "Visit Schedule", "Screening Visit"),
    (13, "Visit Schedule", "Follow-up Visit"),
    (14, "Demographics", "Gender"),
    (15, "Demographics", "Height"),
    (16, "Demographics", "Weight"),
    (17, "Data Entry", "Subject ID"),
    (18, "Data Entry", "Missing Data"),
    (19, "Important Note", "Interpretation"),
]

RULE_IDS = [
    (5, "VAL-001", "Subject Details", "Subject ID"),
    (6, "VAL-002", "Demographics", "Age"),
    (7, "VAL-003", "Consent", "Informed Consent"),
    (8, "VAL-004", "Visit Details", "Screening Date"),
    (9, "VAL-005", "Visit Details", "Follow-up Date"),
    (10, "VAL-006", "Demographics", "Gender"),
    (11, "VAL-007", "Vitals", "Height (cm)"),
    (12, "VAL-008", "Vitals", "Weight (kg)"),
    (13, "VAL-009", "Eligibility", "Eligibility Status"),
    (14, "VAL-010", "Subject Details", "Subject ID uniqueness"),
]

SCREENING = date(2026, 10, 1)
TEST_ROWS = [
    (5, "TC-001", "SUBJ-001", 30, "Female", "Yes", SCREENING, date(2026, 10, 8), 165, 60),
    (6, "TC-002", "SUBJ-002", 17, "Male", "Yes", SCREENING, date(2026, 10, 8), 172, 70),
    (7, "TC-003", "SUBJ-003", 66, "Female", "Yes", SCREENING, date(2026, 10, 8), 160, 55),
    (8, "TC-004", "SUBJ-004", 25, "Male", "No", SCREENING, date(2026, 10, 8), 180, 82),
    (9, "TC-005", "SUBJ-005", 40, "Not Reported", "Yes", SCREENING, date(2026, 9, 30), 170, 68),
    (10, "TC-006", None, 28, "Female", "Yes", SCREENING, date(2026, 10, 8), 168, 63),
    (11, "TC-007", "SUBJ-007", 18, "Female", "Yes", SCREENING, date(2026, 10, 1), 155, 50),
    (12, "TC-008", "SUBJ-008", 65, "Male", "Yes", SCREENING, date(2026, 10, 15), 175, 75),
]


def _write_sheets(path: Path, sheets: dict[str, list[list[object]]]) -> None:
    workbook = Workbook()
    default = workbook.active
    first = True
    for name, rows in sheets.items():
        worksheet = default if first else workbook.create_sheet()
        first = False
        worksheet.title = name
        for row_index, row in enumerate(rows, start=1):
            for column_index, value in enumerate(row, start=1):
                if value is not None:
                    worksheet.cell(row_index, column_index, value)
    workbook.save(path)
    workbook.close()


def _minimal_sheets() -> dict[str, list[list[object]]]:
    return {
        "Study_Protocol": [
            list(SHEET_COLUMNS["Study_Protocol"]),
            ["Eligibility", "Age", "Participant age must be from 18 through 65 years inclusive."],
        ],
        "Validation_Rules": [
            list(SHEET_COLUMNS["Validation_Rules"]),
            [
                "VAL-001",
                "Subject Details",
                "Subject ID",
                "Subject ID is mandatory.",
                "Do not allow completion of a screening form when blank; show required-field message.",
                "SUBJ-001",
                "Blank",
            ],
        ],
        "Sample_Test_Data": [
            list(SHEET_COLUMNS["Sample_Test_Data"]),
            ["TC-001", "SUBJ-001", 30, "Female", "Yes", date(2026, 10, 1), date(2026, 10, 8), 165, 68.5],
        ],
    }


def test_sample_workbook_extracts_protocol_requirements() -> None:
    result = read_workbook(SAMPLE_PATH)

    assert result.source_name == SAMPLE_PATH.name
    assert result.detected_sheets == [
        "Study_Protocol",
        "Validation_Rules",
        "Sample_Test_Data",
    ]
    sheet = result.study_protocol
    assert sheet.sheet_name == "Study_Protocol"
    assert sheet.header_excel_row == 4
    assert sheet.column_names == list(SHEET_COLUMNS["Study_Protocol"])
    assert sheet.notes == [
        "Sample Clinical Study Protocol",
        (
            "FICTIONAL DEMO DATA ONLY \u2014 not a real clinical protocol "
            "and not for patient care or an actual clinical trial."
        ),
    ]
    assert [
        (row.excel_row, row.section, row.item) for row in result.protocol_requirements
    ] == PROTOCOL_ITEMS
    age = result.protocol_requirements[4]
    assert age.excel_row == 9
    assert age.requirement == (
        "Participant age must be from 18 through 65 years inclusive."
    )
    assert age.values == {
        "Section": "Eligibility",
        "Item": "Age",
        "Sample protocol requirement": age.requirement,
    }
    assert all(row.sheet_name == "Study_Protocol" for row in sheet.rows)


def test_sample_workbook_extracts_validation_rules_unchanged() -> None:
    result = read_workbook(SAMPLE_PATH)
    sheet = result.validation_rules

    assert sheet.header_excel_row == 4
    assert sheet.column_names == list(SHEET_COLUMNS["Validation_Rules"])
    assert sheet.notes[0] == "EDC Form Validation Rules"
    extracted = [
        (row.excel_row, row.rule_id, row.form_section, row.field)
        for row in result.validation_rule_records
    ]
    assert extracted == RULE_IDS

    age_rule = result.validation_rule_records[1]
    assert age_rule.condition == "Age must be an integer from 18 to 65 inclusive."
    assert age_rule.expected_edc_behavior == (
        "Accept 18\u201365; validate values below 18 or above 65."
    )
    assert age_rule.positive_example == "30"
    assert isinstance(age_rule.positive_example, str)

    weight_rule = result.validation_rule_records[7]
    assert weight_rule.positive_example == "68.5"
    assert isinstance(weight_rule.positive_example, str)
    assert weight_rule.negative_example == "0 or -2"


def test_sample_test_data_preserves_original_values() -> None:
    result = read_workbook(SAMPLE_PATH)
    sheet = result.sample_test_data

    assert sheet.sheet_name == "Sample_Test_Data"
    assert sheet.header_excel_row == 4
    assert sheet.column_names == list(SHEET_COLUMNS["Sample_Test_Data"])
    assert sheet.notes[0] == "Synthetic Demo Test Data"

    extracted = [
        (
            row.excel_row,
            row.test_case_id,
            row.subject_id,
            row.age,
            row.gender,
            row.consent,
            row.screening_date,
            row.follow_up_date,
            row.height_cm,
            row.weight_kg,
        )
        for row in result.sample_test_records
    ]
    assert extracted == TEST_ROWS

    blank_subject = result.sample_test_records[5]
    assert blank_subject.test_case_id == "TC-006"
    assert blank_subject.subject_id is None
    assert blank_subject.values["Subject ID"] is None
    assert isinstance(blank_subject.age, int)
    assert isinstance(blank_subject.screening_date, date)

    earlier_follow_up = result.sample_test_records[4]
    assert earlier_follow_up.follow_up_date == date(2026, 9, 30)
    assert earlier_follow_up.age == 40


def test_read_workbook_accepts_bytes_and_file_objects() -> None:
    payload = SAMPLE_PATH.read_bytes()
    from_bytes = read_workbook(payload, source_name=SAMPLE_PATH.name)
    assert len(from_bytes.protocol_requirements) == 15

    buffer = BytesIO(payload)
    buffer.name = SAMPLE_PATH.name
    from_buffer = read_workbook(buffer)
    assert from_buffer.source_name == SAMPLE_PATH.name
    assert len(from_buffer.validation_rule_records) == 10
    assert len(from_buffer.sample_test_records) == 8


def test_empty_rows_are_skipped_and_excel_row_numbers_are_kept(tmp_path: Path) -> None:
    sheets = _minimal_sheets()
    sheets["Study_Protocol"] = [
        ["Sample Clinical Study Protocol"],
        [None],
        list(SHEET_COLUMNS["Study_Protocol"]),
        ["Eligibility", "Age", "First requirement"],
        [None, None, None],
        ["   ", "   ", "   "],
        ["Demographics", "Gender", "Second requirement"],
    ]
    path = tmp_path / "blank_rows.xlsx"
    _write_sheets(path, sheets)

    result = read_workbook(path)
    rows = result.protocol_requirements
    assert [(row.excel_row, row.item) for row in rows] == [
        (4, "Age"),
        (7, "Gender"),
    ]
    assert result.study_protocol.notes == ["Sample Clinical Study Protocol"]
    assert result.study_protocol.header_excel_row == 3


def test_header_on_first_row_and_float_values_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "header_row_one.xlsx"
    _write_sheets(path, _minimal_sheets())

    result = read_workbook(path)
    assert result.study_protocol.header_excel_row == 1
    record = result.sample_test_records[0]
    assert record.weight_kg == 68.5
    assert isinstance(record.weight_kg, float)
    assert record.screening_date == date(2026, 10, 1)
    assert record.values["Weight (kg)"] == 68.5
    assert record.values["Screening Date"] == date(2026, 10, 1)


def test_partial_row_keeps_blank_cells(tmp_path: Path) -> None:
    sheets = _minimal_sheets()
    sheets["Study_Protocol"].append(["Data Entry", None, None])
    path = tmp_path / "partial.xlsx"
    _write_sheets(path, sheets)

    result = read_workbook(path)
    partial = result.protocol_requirements[-1]
    assert partial.excel_row == 3
    assert partial.section == "Data Entry"
    assert partial.item is None
    assert partial.requirement is None
    assert partial.values["Item"] is None


def test_extra_column_and_extra_sheet_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "extra.xlsx"
    _write_sheets(path, _minimal_sheets())
    workbook = load_workbook(path)
    workbook["Study_Protocol"].cell(1, 4, "Notes")
    workbook["Study_Protocol"].cell(2, 4, "Demo note")
    workbook.create_sheet("Notes")
    workbook.save(path)
    workbook.close()

    result = read_workbook(path)
    assert result.detected_sheets[-1] == "Notes"
    assert "Notes" not in SHEET_COLUMNS
    assert result.study_protocol.column_names[-1] == "Notes"
    assert result.protocol_requirements[0].values["Notes"] == "Demo note"
    assert len(result.validation_rule_records) == 1


def test_missing_sheet_reports_detected_sheets(tmp_path: Path) -> None:
    sheets = _minimal_sheets()
    del sheets["Validation_Rules"]
    path = tmp_path / "missing_sheet.xlsx"
    _write_sheets(path, sheets)

    with pytest.raises(MissingSheetError) as caught:
        read_workbook(path)

    assert caught.value.missing_sheets == ["Validation_Rules"]
    assert caught.value.detected_sheets == ["Study_Protocol", "Sample_Test_Data"]
    assert "Validation_Rules" in str(caught.value)


def test_missing_required_columns(tmp_path: Path) -> None:
    sheets = _minimal_sheets()
    sheets["Study_Protocol"] = [
        ["Section", "Sample protocol requirement"],
        ["Eligibility", "Age text"],
    ]
    path = tmp_path / "missing_column.xlsx"
    _write_sheets(path, sheets)

    with pytest.raises(MissingColumnsError) as caught:
        read_workbook(path)

    assert caught.value.sheet_name == "Study_Protocol"
    assert caught.value.missing_columns == ["Item"]
    assert "Item" in str(caught.value)


def test_duplicate_header_is_rejected(tmp_path: Path) -> None:
    sheets = _minimal_sheets()
    sheets["Validation_Rules"][0] = [
        "Rule ID",
        "Form / Section",
        "Field",
        "Field",
        "Requirement / Condition",
        "Expected EDC Behavior",
        "Positive Example",
        "Negative Example",
    ]
    path = tmp_path / "duplicate.xlsx"
    _write_sheets(path, sheets)

    with pytest.raises(DuplicateColumnError) as caught:
        read_workbook(path)

    assert caught.value.sheet_name == "Validation_Rules"
    assert caught.value.column_name == "Field"
    assert "duplicate column" in str(caught.value)


def test_invalid_excel_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.xlsx"
    path.write_text("this is not a workbook", encoding="utf-8")

    with pytest.raises(InvalidExcelFileError, match="not a valid Excel workbook"):
        read_workbook(path)


def test_empty_upload_is_rejected() -> None:
    with pytest.raises(InvalidExcelFileError, match="empty"):
        read_workbook(b"", source_name="empty.xlsx")

    with pytest.raises(InvalidExcelFileError, match="empty"):
        read_workbook(BytesIO(b""))


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "absent.xlsx"
    with pytest.raises(InvalidExcelFileError, match="not found"):
        read_workbook(missing)


def test_header_only_sheet_returns_no_data_rows(tmp_path: Path) -> None:
    sheets = _minimal_sheets()
    sheets["Sample_Test_Data"] = [list(SHEET_COLUMNS["Sample_Test_Data"])]
    path = tmp_path / "header_only.xlsx"
    _write_sheets(path, sheets)

    result = read_workbook(path)
    assert result.sample_test_records == []
    assert result.sample_test_data.column_names == list(
        SHEET_COLUMNS["Sample_Test_Data"]
    )
