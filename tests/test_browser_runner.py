"""Local fill-only Playwright tests. No Google API, network, or live AI calls."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from execution.approval_gate import approve_test_case
from execution.browser_runner import execute_fill_only
from execution.field_mapping import (
    FieldMappingResult,
    MappedInput,
    load_form_mapping,
    map_test_case_inputs,
)
from models.approval import ApprovedCase, BlockedCase
from models.execution import ExecutionResult
from models.test_cases import GeneratedTestCase

FORM = Path(__file__).resolve().parent / "fixtures" / "sample_form.html"
REVIEWED_AT = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CDM_BEHAVIOR = "CDM confirms the stated form behavior."
RUNNER_SOURCE = Path(__file__).resolve().parents[1] / "execution" / "browser_runner.py"


def _case(test_case_id: str = "TC-FILL-01", **overrides: object) -> GeneratedTestCase:
    data: dict[str, object] = {
        "test_case_id": test_case_id,
        "requirement_id": "VAL-001",
        "requirement_ids": ["VAL-001"],
        "title": "Synthetic screening case",
        "description": "Synthetic values for the local form.",
        "category": "positive",
        "input_data": {
            "Subject ID": "SUBJ-001",
            "Age": 30,
            "Gender": "Female",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-01",
            "Follow-up Date": "2026-10-08",
            "Height (cm)": 165,
            "Weight (kg)": 60,
        },
        "expected_result": "Accept",
        "expected_behavior": "AI text that execution must not treat as the approved behavior.",
        "source_file": "study.xlsx",
        "source_sheet": "Validation_Rules",
        "source_row": 5,
        "rationale": "Synthetic.",
        "confidence": 0.95,
        "needs_human_review": False,
    }
    data.update(overrides)
    return GeneratedTestCase(**data)


def _approved(case: GeneratedTestCase | None = None) -> ApprovedCase:
    test_case = case or _case()
    record = approve_test_case(
        test_case,
        reviewer_name="A. Reviewer",
        reviewer_role="Clinical Data Manager",
        decision="approved",
        comment="Confirmed for the local form.",
        approved_expected_behavior=CDM_BEHAVIOR,
        reviewed_at=REVIEWED_AT,
    )
    return ApprovedCase(
        test_case=test_case,
        approval=record,
        execution_expected_behavior=record.approved_expected_behavior,
    )


def _ready(case: ApprovedCase | None = None) -> tuple[ApprovedCase, FieldMappingResult]:
    approved = case or _approved()
    mapping = map_test_case_inputs(approved, load_form_mapping())
    assert mapping.status == "ready", mapping.validation_errors
    return approved, mapping


@pytest.fixture(scope="module")
def filled_result(tmp_path_factory: pytest.TempPathFactory) -> ExecutionResult:
    approved, mapping = _ready()
    return execute_fill_only(
        approved,
        mapping,
        form_path=FORM,
        screenshot_dir=tmp_path_factory.mktemp("fill"),
    )


def test_local_fixture_loads_successfully() -> None:
    html = FORM.read_text(encoding="utf-8")
    assert "http://" not in html
    assert "https://" not in html
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(FORM.resolve().as_uri(), wait_until="load")
            assert page.get_by_role("heading", name="Clinical EDC UAT Demo Form").is_visible()
            for label in (
                "Subject ID",
                "Age",
                "Gender",
                "Screening Date",
                "Follow-up Date",
                "Height (cm)",
                "Weight (kg)",
            ):
                assert page.get_by_label(label, exact=True).is_visible()
            assert page.get_by_role("radio", name="Yes", exact=True).is_visible()
            assert page.get_by_role("button", name="Submit").is_visible()
            assert page.locator("#submission-state").inner_text() == "Not submitted"
        finally:
            browser.close()


def _require_filled(result: ExecutionResult) -> None:
    assert result.execution_status == "filled", result.error_message


def test_text_fields_are_filled(filled_result: ExecutionResult) -> None:
    _require_filled(filled_result)
    assert filled_result.observed_values["Subject ID"] == "SUBJ-001"


def test_number_fields_are_filled(filled_result: ExecutionResult) -> None:
    _require_filled(filled_result)
    assert filled_result.observed_values["Age"] == "30"
    assert filled_result.observed_values["Height (cm)"] == "165"
    assert filled_result.observed_values["Weight (kg)"] == "60"


def test_dropdown_value_is_selected(filled_result: ExecutionResult) -> None:
    _require_filled(filled_result)
    assert filled_result.entered_values["Gender"] == "Female"
    assert filled_result.observed_values["Gender"] == "Female"


def test_radio_button_is_selected(filled_result: ExecutionResult) -> None:
    _require_filled(filled_result)
    assert filled_result.entered_values["Informed Consent"] == "Yes"
    assert filled_result.observed_values["Informed Consent"] == "Yes"


def test_date_values_are_filled(filled_result: ExecutionResult) -> None:
    _require_filled(filled_result)
    assert filled_result.observed_values["Screening Date"] == "2026-10-01"
    assert filled_result.observed_values["Follow-up Date"] == "2026-10-08"


def test_values_can_be_read_back(filled_result: ExecutionResult) -> None:
    _require_filled(filled_result)
    assert set(filled_result.entered_values) == set(filled_result.observed_values)
    assert filled_result.observed_values["Subject ID"] == filled_result.entered_values["Subject ID"]
    assert filled_result.expected_behavior == CDM_BEHAVIOR


def test_screenshot_is_created(filled_result: ExecutionResult) -> None:
    _require_filled(filled_result)
    assert filled_result.screenshot_path is not None
    screenshot = Path(filled_result.screenshot_path)
    assert screenshot.is_file()
    assert screenshot.stat().st_size > 0
    assert filled_result.completed_at >= filled_result.started_at


def test_submit_button_is_never_clicked(filled_result: ExecutionResult) -> None:
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    assert ".click(" not in source
    assert "get_by_role(\"button\", name=\"Submit\")" not in source
    _require_filled(filled_result)
    assert filled_result.error_message is None
    assert filled_result.execution_mode == "FILL_ONLY"


def test_none_values_remain_blank(tmp_path: Path) -> None:
    case = _case(
        "TC-BLANK",
        input_data={
            "Subject ID": None,
            "Age": 30,
            "Gender": "Female",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-01",
        },
    )
    approved, mapping = _ready(_approved(case))
    result = execute_fill_only(
        approved,
        mapping,
        form_path=FORM,
        screenshot_dir=tmp_path,
    )

    assert result.execution_status == "filled", result.error_message
    assert result.entered_values["Subject ID"] is None
    assert result.observed_values["Subject ID"] == ""
    assert result.entered_values["Age"] == 30
    assert result.observed_values["Gender"] == "Female"
    assert result.observed_values["Informed Consent"] == "Yes"


def test_raw_generated_case_is_rejected(tmp_path: Path) -> None:
    approved, mapping = _ready()
    result = execute_fill_only(
        approved.test_case,
        mapping,
        form_path=FORM,
        screenshot_dir=tmp_path,
    )

    assert result.execution_status == "blocked"
    assert result.error_message == "Raw generated test cases cannot be executed."
    assert result.screenshot_path is None
    assert list(tmp_path.glob("*.png")) == []


def test_blocked_case_is_rejected(tmp_path: Path) -> None:
    generated = _case("TC-BLOCKED")
    blocked = BlockedCase(test_case=generated, reasons=["Decision is rejected."])
    approved, mapping = _ready()
    result = execute_fill_only(
        blocked,
        mapping,
        form_path=FORM,
        screenshot_dir=tmp_path,
    )

    assert result.execution_status == "blocked"
    assert result.error_message == "Blocked execution-plan cases cannot be executed."
    assert result.screenshot_path is None


def test_blocked_mapping_is_rejected(tmp_path: Path) -> None:
    case = _case(
        "TC-UNKNOWN",
        input_data={
            "Subject ID": "SUBJ-001",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-01",
            "Eligibility Status": "Eligible",
        },
    )
    approved = _approved(case)
    mapping = map_test_case_inputs(approved, load_form_mapping())
    assert mapping.status == "blocked"
    result = execute_fill_only(approved, mapping, form_path=FORM, screenshot_dir=tmp_path)

    assert result.execution_status == "blocked"
    assert result.error_message == "Mapping status is blocked."
    assert result.expected_behavior == CDM_BEHAVIOR
    assert result.screenshot_path is None


def test_mismatched_case_id_is_rejected(tmp_path: Path) -> None:
    approved, mapping = _ready()
    other = _approved(_case("TC-OTHER"))
    result = execute_fill_only(other, mapping, form_path=FORM, screenshot_dir=tmp_path)

    assert result.execution_status == "blocked"
    assert result.error_message == "Mapping result is for a different test case."


def test_missing_required_mapping_is_rejected(tmp_path: Path) -> None:
    approved = _approved()
    incomplete = FieldMappingResult(
        test_case_id=approved.test_case.test_case_id,
        mapped_inputs=[
            MappedInput(
                source_field="age",
                form_question_title="Age",
                form_question_type="short_text",
                value=30,
            )
        ],
        unmapped_source_fields=[],
        validation_errors=[],
        status="ready",
    )
    result = execute_fill_only(approved, incomplete, form_path=FORM, screenshot_dir=tmp_path)

    assert result.execution_status == "blocked"
    assert "subject_id" in (result.error_message or "")
    assert result.screenshot_path is None


def test_browser_errors_are_recorded(tmp_path: Path) -> None:
    approved, mapping = _ready()
    result = execute_fill_only(
        approved,
        mapping,
        form_path=tmp_path / "missing-form.html",
        screenshot_dir=tmp_path,
    )

    assert result.execution_status == "error"
    assert result.error_message
    assert "missing-form.html" in result.error_message
    assert result.expected_behavior == CDM_BEHAVIOR
    assert result.entered_values["Subject ID"] == "SUBJ-001"
    assert result.screenshot_path is None
    assert result.execution_mode == "FILL_ONLY"


def test_result_cannot_be_marked_passed() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        ExecutionResult(
            test_case_id="TC-FILL-01",
            execution_mode="FILL_ONLY",
            execution_status="passed",
            entered_values={},
            observed_values={},
            expected_behavior=CDM_BEHAVIOR,
            started_at=now,
            completed_at=now,
        )
