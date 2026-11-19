"""Read the study protocol workbook into structured records.

The sample workbook places a title and a note above each table. The header
row is detected by its column names, and Excel row numbers from that table
are kept on every extracted record. Clinical wording is copied as stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook
from pydantic import BaseModel, ValidationError

from models.schemas import (
    CellValue,
    IngestedWorkbook,
    ProtocolRequirement,
    SampleTestCase,
    SheetTable,
    ValidationRule,
)

WorkbookSource = str | Path | bytes | bytearray | BinaryIO


class ExcelIngestionError(Exception):
    """A workbook could not be ingested."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        self.details = details if details is not None else [message]
        super().__init__(message)


class InvalidExcelFileError(ExcelIngestionError):
    """The file is missing, empty, or not a readable .xlsx workbook."""


class MissingSheetError(ExcelIngestionError):
    """One or more required worksheets are absent."""

    def __init__(self, missing_sheets: list[str], detected_sheets: list[str]) -> None:
        self.missing_sheets = list(missing_sheets)
        self.detected_sheets = list(detected_sheets)
        missing = ", ".join(f"'{name}'" for name in missing_sheets)
        detected = ", ".join(detected_sheets) if detected_sheets else "(none)"
        message = (
            f"Missing required sheet(s): {missing}. Detected sheets: {detected}."
        )
        super().__init__(message)


class MissingColumnsError(ExcelIngestionError):
    """A required sheet does not contain all required column names."""

    def __init__(
        self,
        sheet_name: str,
        missing_columns: list[str],
        found_columns: list[str],
        header_excel_row: int | None = None,
    ) -> None:
        self.sheet_name = sheet_name
        self.missing_columns = list(missing_columns)
        self.found_columns = list(found_columns)
        self.header_excel_row = header_excel_row
        missing = ", ".join(missing_columns)
        found = ", ".join(found_columns) if found_columns else "(none)"
        location = f" on row {header_excel_row}" if header_excel_row else ""
        message = (
            f"Sheet '{sheet_name}' is missing required columns: {missing}. "
            f"Found columns{location}: {found}."
        )
        super().__init__(message)


class DuplicateColumnError(ExcelIngestionError):
    """A header row repeats a column name."""

    def __init__(self, sheet_name: str, column_name: str, excel_row: int) -> None:
        self.sheet_name = sheet_name
        self.column_name = column_name
        self.excel_row = excel_row
        message = (
            f"Sheet '{sheet_name}' has duplicate column '{column_name}' "
            f"on row {excel_row}."
        )
        super().__init__(message)


class RowValidationError(ExcelIngestionError):
    """A data row could not be converted into a structured record."""

    def __init__(self, details: list[tuple[str, int, str]]) -> None:
        self.row_errors = list(details)
        lines = [
            f"Sheet '{sheet_name}' row {excel_row}: {message}"
            for sheet_name, excel_row, message in details
        ]
        super().__init__("Workbook has rows that could not be read.", lines)


@dataclass(frozen=True)
class _ColumnSpec:
    name: str
    field: str


@dataclass(frozen=True)
class _SheetSpec:
    name: str
    model: type[BaseModel]
    columns: tuple[_ColumnSpec, ...]

    @property
    def required_columns(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


_PROTOCOL = _SheetSpec(
    name="Study_Protocol",
    model=ProtocolRequirement,
    columns=(
        _ColumnSpec("Section", "section"),
        _ColumnSpec("Item", "item"),
        _ColumnSpec("Sample protocol requirement", "requirement"),
    ),
)
_RULES = _SheetSpec(
    name="Validation_Rules",
    model=ValidationRule,
    columns=(
        _ColumnSpec("Rule ID", "rule_id"),
        _ColumnSpec("Form / Section", "form_section"),
        _ColumnSpec("Field", "field"),
        _ColumnSpec("Requirement / Condition", "condition"),
        _ColumnSpec("Expected EDC Behavior", "expected_edc_behavior"),
        _ColumnSpec("Positive Example", "positive_example"),
        _ColumnSpec("Negative Example", "negative_example"),
    ),
)
_TEST_DATA = _SheetSpec(
    name="Sample_Test_Data",
    model=SampleTestCase,
    columns=(
        _ColumnSpec("Test Case ID", "test_case_id"),
        _ColumnSpec("Subject ID", "subject_id"),
        _ColumnSpec("Age", "age"),
        _ColumnSpec("Gender", "gender"),
        _ColumnSpec("Consent", "consent"),
        _ColumnSpec("Screening Date", "screening_date"),
        _ColumnSpec("Follow-up Date", "follow_up_date"),
        _ColumnSpec("Height (cm)", "height_cm"),
        _ColumnSpec("Weight (kg)", "weight_kg"),
    ),
)

SHEET_SPECS: tuple[_SheetSpec, ...] = (_PROTOCOL, _RULES, _TEST_DATA)
SHEET_COLUMNS: dict[str, tuple[str, ...]] = {
    spec.name: spec.required_columns for spec in SHEET_SPECS
}
REQUIRED_SHEETS: tuple[str, ...] = tuple(SHEET_COLUMNS)


def read_workbook(
    source: WorkbookSource,
    source_name: str | None = None,
) -> IngestedWorkbook:
    """Read Study_Protocol, Validation_Rules, and Sample_Test_Data.

    ``source`` may be a filesystem path, raw bytes, or a binary file object
    such as a Streamlit upload. Date-formatted cells are returned as
    ``datetime.date``. Other cell values are returned as OpenPyXL read them.
    """

    workbook, resolved_name = _open_workbook(source, source_name)
    try:
        return _extract_workbook(workbook, resolved_name)
    finally:
        workbook.close()


def _open_workbook(
    source: WorkbookSource,
    source_name: str | None,
) -> tuple[Workbook, str | None]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists() or not path.is_file():
            raise InvalidExcelFileError(f"Excel file not found: {path}")
        try:
            workbook = load_workbook(path, data_only=False)
        except (InvalidFileException, BadZipFile, KeyError, ValueError) as exc:
            raise InvalidExcelFileError(
                "The file is not a valid Excel workbook (.xlsx)."
            ) from exc
        except OSError as exc:
            raise InvalidExcelFileError(
                f"The Excel file could not be read: {exc}"
            ) from exc
        return workbook, source_name or path.name

    if isinstance(source, (bytes, bytearray)):
        payload = bytes(source)
        resolved_name = source_name
    else:
        read = getattr(source, "read", None)
        if not callable(read):
            raise InvalidExcelFileError(
                "Unsupported workbook source. Provide a .xlsx path, bytes, "
                "or a binary file object."
            )
        payload = read()
        if isinstance(payload, str):
            raise InvalidExcelFileError(
                "The workbook source must be binary, not text."
            )
        payload = bytes(payload)
        seek = getattr(source, "seek", None)
        if callable(seek):
            seek(0)
        if source_name is None:
            name = getattr(source, "name", None)
            resolved_name = name if isinstance(name, str) else None
        else:
            resolved_name = source_name

    if not payload:
        raise InvalidExcelFileError("The Excel file is empty.")

    try:
        workbook = load_workbook(BytesIO(payload), data_only=False)
    except (InvalidFileException, BadZipFile, KeyError, ValueError) as exc:
        raise InvalidExcelFileError(
            "The file is not a valid Excel workbook (.xlsx)."
        ) from exc
    return workbook, resolved_name


def _extract_workbook(workbook: Workbook, source_name: str | None) -> IngestedWorkbook:
    detected = list(workbook.sheetnames)
    missing = [name for name in REQUIRED_SHEETS if name not in detected]
    if missing:
        raise MissingSheetError(missing, detected)

    tables: dict[str, SheetTable] = {}
    row_errors: list[tuple[str, int, str]] = []
    for spec in SHEET_SPECS:
        table, errors = _extract_sheet(workbook[spec.name], spec)
        tables[spec.name] = table
        row_errors.extend(errors)
    if row_errors:
        raise RowValidationError(row_errors)

    protocol = tables["Study_Protocol"]
    rules = tables["Validation_Rules"]
    test_data = tables["Sample_Test_Data"]
    return IngestedWorkbook(
        source_name=source_name,
        detected_sheets=detected,
        study_protocol=protocol,
        validation_rules=rules,
        sample_test_data=test_data,
        protocol_requirements=list(protocol.rows),
        validation_rule_records=list(rules.rows),
        sample_test_records=list(test_data.rows),
    )


def _extract_sheet(
    worksheet: Any,
    spec: _SheetSpec,
) -> tuple[SheetTable, list[tuple[str, int, str]]]:
    raw_rows = _worksheet_rows(worksheet)
    header = _locate_header(spec, raw_rows)
    notes: list[str] = []
    rows: list[BaseModel] = []
    errors: list[tuple[str, int, str]] = []
    column_names = list(header.column_names)

    for excel_row, values in raw_rows:
        if excel_row < header.excel_row:
            for value in values:
                note = _note_text(value)
                if note is not None:
                    notes.append(note)
            continue
        if excel_row == header.excel_row:
            continue

        record, extra_columns = _row_record(header, values)
        for extra in extra_columns:
            if extra not in column_names:
                column_names.append(extra)
        if all(value is None for value in record.values()):
            continue
        try:
            rows.append(_build_row(spec, worksheet.title, excel_row, record, header))
        except ValidationError as exc:
            errors.append((worksheet.title, excel_row, _format_validation_error(exc)))

    table = SheetTable(
        sheet_name=worksheet.title,
        header_excel_row=header.excel_row,
        column_names=column_names,
        notes=notes,
        rows=rows,
    )
    return table, errors


@dataclass(frozen=True)
class _Header:
    excel_row: int
    column_names: tuple[str, ...]
    index_to_name: dict[int, str]
    canonical_to_actual: dict[str, str]


def _locate_header(spec: _SheetSpec, raw_rows: list[tuple[int, list[CellValue]]]) -> _Header:
    required = {
        _normalize(column): column for column in spec.required_columns
    }
    best: dict[str, Any] | None = None

    for excel_row, values in raw_rows:
        labels, duplicate = _header_labels(values)
        found: dict[str, str] = {}
        for item in labels:
            if item is None:
                continue
            key, label = item
            found[key] = label
        overlap = [required[key] for key in required if key in found]
        candidate = {
            "excel_row": excel_row,
            "labels": labels,
            "found": found,
            "overlap": overlap,
            "duplicate": duplicate,
        }
        if len(overlap) == len(required):
            if duplicate:
                raise DuplicateColumnError(spec.name, duplicate, excel_row)
            return _header_from_labels(excel_row, labels, required)
        if best is None or len(overlap) > len(best["overlap"]):
            best = candidate

    if best and best["duplicate"] and best["overlap"]:
        raise DuplicateColumnError(spec.name, best["duplicate"], best["excel_row"])

    found_columns: list[str] = []
    header_row: int | None = None
    if best and best["overlap"]:
        found_columns = [label for _key, label in best["found"].items()]
        header_row = best["excel_row"]
        missing = [
            column
            for column in spec.required_columns
            if _normalize(column) not in best["found"]
        ]
    else:
        densest: tuple[int, list[str]] | None = None
        for excel_row, values in raw_rows:
            labels = [
                value.strip()
                for value in values
                if isinstance(value, str) and value.strip()
            ]
            if densest is None or len(labels) > densest[0]:
                densest = (len(labels), labels)
                header_row = excel_row if labels else None
        found_columns = densest[1] if densest else []
        missing = list(spec.required_columns)

    raise MissingColumnsError(spec.name, missing, found_columns, header_row)


def _header_from_labels(
    excel_row: int,
    labels: list[tuple[str, str] | None],
    required: dict[str, str],
) -> _Header:
    index_to_name: dict[int, str] = {}
    canonical_to_actual: dict[str, str] = {}
    column_names: list[str] = []
    for index, item in enumerate(labels):
        if item is None:
            continue
        key, label = item
        index_to_name[index] = label
        column_names.append(label)
        if key in required:
            canonical_to_actual[required[key]] = label
    return _Header(excel_row, tuple(column_names), index_to_name, canonical_to_actual)


def _header_labels(
    values: list[CellValue],
) -> tuple[list[tuple[str, str] | None], str | None]:
    labels: list[tuple[str, str] | None] = []
    seen: set[str] = set()
    duplicate: str | None = None
    for value in values:
        if not isinstance(value, str) or not value.strip():
            labels.append(None)
            continue
        label = value.strip()
        key = _normalize(label)
        if key in seen and duplicate is None:
            duplicate = label
        seen.add(key)
        labels.append((key, label))
    return labels, duplicate


def _row_record(
    header: _Header,
    values: list[CellValue],
) -> tuple[dict[str, CellValue], list[str]]:
    record: dict[str, CellValue] = {name: None for name in header.column_names}
    for index, name in header.index_to_name.items():
        record[name] = values[index] if index < len(values) else None

    extra_columns: list[str] = []
    for index, value in enumerate(values):
        if index in header.index_to_name or value is None:
            continue
        extra_name = f"Column {get_column_letter(index + 1)}"
        record[extra_name] = value
        extra_columns.append(extra_name)
    return record, extra_columns


def _build_row(
    spec: _SheetSpec,
    sheet_name: str,
    excel_row: int,
    record: dict[str, CellValue],
    header: _Header,
) -> BaseModel:
    payload: dict[str, Any] = {
        "sheet_name": sheet_name,
        "excel_row": excel_row,
        "values": record,
    }
    for column in spec.columns:
        actual_name = header.canonical_to_actual[column.name]
        payload[column.field] = record.get(actual_name)
    return spec.model.model_validate(payload)


def _worksheet_rows(worksheet: Any) -> list[tuple[int, list[CellValue]]]:
    max_row = worksheet.max_row or 1
    max_column = worksheet.max_column or 1
    rows: list[tuple[int, list[CellValue]]] = []
    for row in worksheet.iter_rows(
        min_row=1,
        max_row=max_row,
        max_col=max_column,
    ):
        rows.append((row[0].row, [_cell_value(cell) for cell in row]))
    return rows


def _cell_value(cell: Any) -> CellValue:
    """Return the cell value without changing clinical text.

    Excel stores a calendar date as a serial number. OpenPyXL already turns
    date-formatted cells into midnight datetimes; those become ``datetime.date``.
    """

    value = cell.value
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return None if value.strip() == "" else value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None and value.time() == time.min:
            return value.date()
        return value
    if isinstance(value, date):
        return value
    return str(value)


def _note_text(value: CellValue) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _normalize(label: str) -> str:
    return " ".join(label.split()).casefold()


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "value"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
