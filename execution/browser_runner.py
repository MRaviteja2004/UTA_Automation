"""Fill a local HTML form with an approved, mapping-ready test case.

The only mode is FILL_ONLY. This module never clicks Submit and never opens
a Google Form or other external site.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from execution.approval_gate import validate_approval
from execution.field_mapping import (
    QUESTION_TYPES,
    REQUIRED_SOURCE_FIELDS,
    FieldMappingResult,
    MappedInput,
)
from models.approval import ApprovedCase, BlockedCase
from models.execution import ExecutionResult, ExecutionStatus
from models.test_cases import GeneratedTestCase, InputValue

FILL_ONLY = "FILL_ONLY"
_NOT_SUBMITTED = "Not submitted"
_DEFAULT_FORM = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample_form.html"
)
_DEFAULT_SCREENSHOT_DIR = Path(__file__).resolve().parents[1] / "execution_runs"


def execute_fill_only(
    case: ApprovedCase | BlockedCase | GeneratedTestCase,
    mapping_result: FieldMappingResult | None = None,
    *,
    form_path: Path | None = None,
    screenshot_dir: Path | None = None,
    headless: bool = True,
) -> ExecutionResult:
    """Enter mapped values into the local fixture and read them back.

    Raw generated cases, blocked plan cases, and mappings that are not ready
    are rejected before a browser starts.
    """

    started = datetime.now(timezone.utc)
    rejection = _rejection_reason(case, mapping_result)
    if rejection is not None:
        return _result(
            _case_id(case),
            "blocked",
            {},
            {},
            _expected_behavior(case),
            None,
            started,
            rejection,
        )

    assert isinstance(case, ApprovedCase)
    assert isinstance(mapping_result, FieldMappingResult)
    target = form_path or _DEFAULT_FORM
    directory = screenshot_dir or _DEFAULT_SCREENSHOT_DIR
    entered = _entered_values(mapping_result)
    try:
        observed, screenshot, submission_state = _fill_form(
            mapping_result,
            target,
            directory,
            case.test_case.test_case_id,
            headless=headless,
        )
    except (PlaywrightError, OSError, RuntimeError, ValueError) as exc:
        return _result(
            case.test_case.test_case_id,
            "error",
            entered,
            {},
            case.execution_expected_behavior,
            None,
            started,
            _error_text(exc),
        )
    if submission_state != _NOT_SUBMITTED:
        return _result(
            case.test_case.test_case_id,
            "error",
            entered,
            observed,
            case.execution_expected_behavior,
            screenshot,
            started,
            "The form was submitted.",
        )
    return _result(
        case.test_case.test_case_id,
        "filled",
        entered,
        observed,
        case.execution_expected_behavior,
        screenshot,
        started,
        None,
    )


def _rejection_reason(
    case: ApprovedCase | BlockedCase | GeneratedTestCase,
    mapping_result: FieldMappingResult | None,
) -> str | None:
    if isinstance(case, GeneratedTestCase):
        return "Raw generated test cases cannot be executed."
    if isinstance(case, BlockedCase):
        return "Blocked execution-plan cases cannot be executed."
    if not isinstance(case, ApprovedCase):
        return "Only an approved execution-plan case can be executed."
    if not isinstance(mapping_result, FieldMappingResult):
        return "A ready field mapping is required."
    if mapping_result.test_case_id != case.test_case.test_case_id:
        return "Mapping result is for a different test case."
    if mapping_result.status != "ready":
        return "Mapping status is blocked."
    check = validate_approval(case.test_case, case.approval)
    if not check.accepted:
        return " ".join(check.reasons)
    present = {item.source_field for item in mapping_result.mapped_inputs}
    for source_field in REQUIRED_SOURCE_FIELDS:
        if source_field not in present:
            return f"Required field '{source_field}' has no ready mapping."
    for item in mapping_result.mapped_inputs:
        if item.form_question_type not in QUESTION_TYPES:
            return f"Unsupported question type '{item.form_question_type}'."
        if not item.form_question_title.strip():
            return "A mapped form question title is empty."
    return None


def _fill_form(
    mapping_result: FieldMappingResult,
    form_path: Path,
    screenshot_dir: Path,
    test_case_id: str,
    *,
    headless: bool,
) -> tuple[dict[str, str], str, str]:
    if not form_path.is_file():
        raise FileNotFoundError(f"Local form fixture was not found: {form_path}")
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot = screenshot_dir / f"{_safe_name(test_case_id)}.png"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.set_default_timeout(5000)
            page.goto(form_path.resolve().as_uri(), wait_until="load")
            for item in mapping_result.mapped_inputs:
                _fill_control(page, item)
            observed = {
                item.form_question_title: _read_control(page, item)
                for item in mapping_result.mapped_inputs
            }
            submission_state = page.locator("#submission-state").inner_text().strip()
            page.screenshot(path=str(screenshot), full_page=True)
        finally:
            browser.close()
    return observed, str(screenshot), submission_state


def _fill_control(page: Page, item: MappedInput) -> None:
    if item.value is None:
        return
    text = str(item.value)
    if item.form_question_type == "dropdown":
        page.get_by_label(item.form_question_title, exact=True).select_option(label=text)
        return
    if item.form_question_type == "multiple_choice":
        page.get_by_role("group", name=item.form_question_title, exact=True).get_by_role(
            "radio",
            name=text,
            exact=True,
        ).check()
        return
    page.get_by_label(item.form_question_title, exact=True).fill(text)


def _read_control(page: Page, item: MappedInput) -> str:
    if item.form_question_type == "multiple_choice":
        checked = page.get_by_role(
            "group",
            name=item.form_question_title,
            exact=True,
        ).get_by_role("radio", checked=True)
        if checked.count() == 0:
            return ""
        return checked.input_value()
    return page.get_by_label(item.form_question_title, exact=True).input_value()


def _entered_values(mapping_result: FieldMappingResult) -> dict[str, InputValue]:
    return {item.form_question_title: item.value for item in mapping_result.mapped_inputs}


def _result(
    test_case_id: str,
    status: ExecutionStatus,
    entered: dict[str, InputValue],
    observed: dict[str, str],
    expected_behavior: str,
    screenshot: str | None,
    started: datetime,
    error_message: str | None,
) -> ExecutionResult:
    return ExecutionResult(
        test_case_id=test_case_id,
        execution_mode=FILL_ONLY,
        execution_status=status,
        entered_values=entered,
        observed_values=observed,
        expected_behavior=expected_behavior,
        screenshot_path=screenshot,
        started_at=started,
        completed_at=datetime.now(timezone.utc),
        error_message=error_message,
    )


def _case_id(case: ApprovedCase | BlockedCase | GeneratedTestCase) -> str:
    if isinstance(case, GeneratedTestCase):
        return case.test_case_id
    return case.test_case.test_case_id


def _expected_behavior(case: ApprovedCase | BlockedCase | GeneratedTestCase) -> str:
    if isinstance(case, ApprovedCase):
        return case.execution_expected_behavior
    return ""


def _safe_name(test_case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", test_case_id)


def _error_text(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if "Target page, context or browser has been closed" in message:
        message = "Chromium closed before the local form could be filled. " + message
    return message[:500]
