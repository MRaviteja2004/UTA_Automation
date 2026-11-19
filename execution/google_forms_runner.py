"""Fill one fresh Google Form response, read it back, then submit it.

Navigation starts at the configured ``/viewform`` URL. Submit is clicked only
after every mapped value has been read back. A case is ``submitted`` only when
the form's own confirmation message is visible. Submission is not a clinical
pass or fail, and Next is never clicked.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from execution.approval_gate import validate_approval
from execution.field_mapping import (
    QUESTION_TYPES,
    FieldMappingResult,
    FormMapping,
    GoogleFormTarget,
    MappedInput,
    canonical_source_field,
    load_form_mapping,
)
from models.approval import ApprovedCase, BlockedCase
from models.execution import ExecutionResult, ExecutionStatus
from models.test_cases import GeneratedTestCase, InputValue

FILL_ONLY = "FILL_ONLY"
_DEFAULT_SCREENSHOT_DIR = Path(__file__).resolve().parents[1] / "execution_runs"
_MISSING_VALUE = "A missing required value is not invented."
_CONFIRMATION_RE = re.compile(r'\["([^"\\]+)"\],null,null,null,null,null,null,\d+')
_NOT_SUBMITTED = "The form was not submitted."


class _PageStop(Exception):
    """Stop a Google Form run after the page is open."""

    def __init__(
        self,
        status: ExecutionStatus,
        message: str,
        *,
        observed: dict[str, str] | None = None,
        before: str | None = None,
        after: str | None = None,
        notes: list[str] | None = None,
        page_title: str = "",
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.observed = observed or {}
        self.before = before
        self.after = after
        self.notes = notes or []
        self.page_title = page_title
        self.diagnostics = diagnostics or {}


@contextmanager
def _chromium_page(headless: bool) -> Iterator[Page]:
    """Open a new browser context so a case cannot reuse an earlier response."""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        try:
            page = context.new_page()
            page.set_default_timeout(15000)
            yield page
        finally:
            context.close()
            browser.close()


def execute_google_form_fill_only(
    case: ApprovedCase | BlockedCase | GeneratedTestCase,
    mapping_result: FieldMappingResult | None = None,
    *,
    config: FormMapping | None = None,
    screenshot_dir: Path | None = None,
    headless: bool = True,
    page_opener=None,
) -> ExecutionResult:
    """Enter mapped values, read them back, and submit that one form response.

    Raw generated cases, blocked plan cases, and mappings that are not ready
    are rejected before a browser starts. A value that was not supplied is not
    invented. The form's required-question setting is not used as a gate.
    """

    started = datetime.now(timezone.utc)
    form_mapping = config if config is not None else load_form_mapping()
    rejection = _rejection_reason(case, mapping_result, form_mapping)
    if rejection is not None:
        return _result(
            _case_id(case),
            "blocked",
            {},
            {},
            _expected_behavior(case),
            started,
            rejection,
        )

    assert isinstance(case, ApprovedCase)
    assert isinstance(mapping_result, FieldMappingResult)
    assert form_mapping.google_form is not None
    target = form_mapping.google_form
    directory = screenshot_dir or _DEFAULT_SCREENSHOT_DIR
    entered = _entered_values(mapping_result)
    opener = page_opener or _chromium_page
    try:
        with opener(headless) as page:
            observed, before, after, notes, page_title = _fill_page(
                page,
                mapping_result,
                target,
                directory,
                case.test_case.test_case_id,
            )
    except _PageStop as stop:
        evidence = _write_evidence(
            directory,
            case.test_case.test_case_id,
            target,
            stop.status,
            entered,
            stop.observed,
            stop.notes,
            stop.before,
            stop.after,
            stop.page_title,
            form_mapping,
            stop.diagnostics,
        )
        return _result(
            case.test_case.test_case_id,
            stop.status,
            entered,
            stop.observed,
            case.execution_expected_behavior,
            started,
            stop.message,
            screenshot=stop.after or stop.before,
            before_screenshot=stop.before,
            evidence=evidence,
            notes=stop.notes,
            url=target.url,
        )
    except (PlaywrightError, OSError, RuntimeError, ValueError) as exc:
        return _result(
            case.test_case.test_case_id,
            "error",
            entered,
            {},
            case.execution_expected_behavior,
            started,
            _error_text(exc),
            url=target.url,
        )

    evidence = _write_evidence(
        directory,
        case.test_case.test_case_id,
        target,
        "submitted",
        entered,
        observed,
        notes,
        before,
        after,
        page_title,
        form_mapping,
        {},
    )
    return _result(
        case.test_case.test_case_id,
        "submitted",
        entered,
        observed,
        case.execution_expected_behavior,
        started,
        None,
        screenshot=after,
        before_screenshot=before,
        evidence=evidence,
        notes=notes,
        url=target.url,
    )


def _rejection_reason(
    case: ApprovedCase | BlockedCase | GeneratedTestCase,
    mapping_result: FieldMappingResult | None,
    form_mapping: FormMapping,
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
        details = " ".join(mapping_result.validation_errors)
        if details:
            return f"Mapping status is blocked. {details}"
        return "Mapping status is blocked."
    check = validate_approval(case.test_case, case.approval)
    if not check.accepted:
        return " ".join(check.reasons)
    if form_mapping.google_form is None:
        return "Google Form target is not configured."
    url_error = _viewform_error(form_mapping.google_form.url)
    if url_error is not None:
        return url_error
    missing = _missing_required_value(mapping_result, form_mapping, case.test_case)
    if missing is not None:
        return missing
    for item in mapping_result.mapped_inputs:
        if item.form_question_type not in QUESTION_TYPES:
            return f"Unsupported question type '{item.form_question_type}'."
        if not item.form_question_title.strip():
            return "A mapped form question title is empty."
    return None


def _missing_required_value(
    mapping_result: FieldMappingResult,
    form_mapping: FormMapping,
    test_case: GeneratedTestCase,
) -> str | None:
    by_source = {item.source_field: item for item in mapping_result.mapped_inputs}
    blank_under_test = _blank_fields_under_test(test_case)
    for entry in form_mapping.mappings:
        if not entry.enabled or not entry.required:
            continue
        item = by_source.get(entry.source_field)
        if item is None:
            return (
                f"Required form question '{entry.form_question_title}' cannot receive a value. "
                f"{_MISSING_VALUE}"
            )
        if _is_blank(item.value):
            # A case that tests a blank required field leaves only that field blank.
            if item.origin == "test_input" and entry.source_field in blank_under_test:
                continue
            return (
                f"Required field '{entry.form_question_title}' has no value. {_MISSING_VALUE}"
            )
    return None


def _is_blank(value: InputValue) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _blank_fields_under_test(test_case: GeneratedTestCase) -> set[str]:
    """Source fields the case explicitly tests with a blank value."""

    fields: set[str] = set()
    for key, role in test_case.test_data_classification.items():
        if role != "under_test":
            continue
        if _is_blank(test_case.input_data.get(key)):
            fields.add(canonical_source_field(key))
    return fields


def _viewform_error(url: str) -> str | None:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "docs.google.com"
        or not path.startswith("/forms/")
        or not path.endswith("/viewform")
        or "formResponse" in path
    ):
        return "Navigation is restricted to the configured Google Forms viewform URL."
    return None


def _fill_page(
    page: Page,
    mapping_result: FieldMappingResult,
    target: GoogleFormTarget,
    screenshot_dir: Path,
    test_case_id: str,
) -> tuple[dict[str, str], str, str, list[str], str]:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(test_case_id)
    before_path = screenshot_dir / f"{safe}_google_before.png"
    after_path = screenshot_dir / f"{safe}_google_after.png"
    page.goto(target.url, wait_until="load")
    _wait_for_question_containers(page)
    if "accounts.google.com" in page.url.lower():
        page.screenshot(path=str(before_path), full_page=True)
        raise _PageStop(
            "blocked",
            "Google sign-in is required. Credentials are not supplied.",
            before=str(before_path),
            notes=["Submit was not clicked."],
            page_title=_safe_title(page),
        )
    if _viewform_error(page.url) is not None or not _same_viewform(page.url, target.url):
        page.screenshot(path=str(before_path), full_page=True)
        raise _PageStop(
            "error",
            "Navigation left the configured Google Forms viewform URL.",
            before=str(before_path),
            page_title=_safe_title(page),
        )
    blocked = _environment_block(page)
    if blocked is not None:
        page.screenshot(path=str(before_path), full_page=True)
        raise _PageStop(
            "blocked",
            blocked,
            before=str(before_path),
            notes=["Submit was not clicked."],
            page_title=_safe_title(page),
        )
    page_title = page.title()
    if target.expected_title not in page_title:
        page.screenshot(path=str(before_path), full_page=True)
        raise _PageStop(
            "error",
            "Google Form title does not match the configured form.",
            before=str(before_path),
            page_title=page_title,
        )
    try:
        _preflight_questions(page, mapping_result, target)
    except _PageStop as stop:
        page.screenshot(path=str(before_path), full_page=True)
        stop.before = str(before_path)
        if not stop.page_title:
            stop.page_title = page_title
        raise
    page.screenshot(path=str(before_path), full_page=True)
    observed: dict[str, str] = {}
    notes = [
        "This case opened a new blank form.",
        "Entry ids were not used as selectors.",
        "Live Google Forms selectors were not verified by the automated tests.",
    ]
    for item in mapping_result.mapped_inputs:
        try:
            observed[item.form_question_title] = _fill_and_read(page, item)
        except _PageStop as stop:
            notes.append("Submit was not clicked.")
            page.screenshot(path=str(after_path), full_page=True)
            stop.observed = observed
            stop.before = str(before_path)
            stop.after = str(after_path)
            stop.notes = notes
            stop.page_title = page_title
            raise
        if _is_blank(item.value):
            notes.append(f"{item.form_question_title} was left blank.")
        elif observed[item.form_question_title] != _expected_text(item):
            notes.append("Submit was not clicked.")
            page.screenshot(path=str(after_path), full_page=True)
            diagnostics = _question_diagnostics(page, item.form_question_title)
            if item.form_question_type == "date":
                diagnostics["date_question"] = _date_diagnostics(
                    _question(page, item.form_question_title)
                )
            raise _PageStop(
                "error",
                f"Read-back for '{item.form_question_title}' did not match the entered value.",
                observed=observed,
                before=str(before_path),
                after=str(after_path),
                notes=notes,
                page_title=page_title,
                diagnostics=diagnostics,
            )
        else:
            notes.append(f"Read-back matched for {item.form_question_title}.")
    _submit_verified_form(page, notes, observed, before_path, after_path, page_title)
    page.screenshot(path=str(after_path), full_page=True)
    return observed, str(before_path), str(after_path), notes, page_title


def _submit_verified_form(
    page: Page,
    notes: list[str],
    observed: dict[str, str],
    before_path: Path,
    after_path: Path,
    page_title: str,
) -> None:
    """Click Submit only after read-back, then require the form's confirmation."""

    clicked = False
    try:
        expected = _configured_confirmation(page)
        submit = page.get_by_role("button", name="Submit", exact=True)
        if _safe_count(submit) != 1:
            raise _PageStop("error", f"Google Form Submit button was not found. {_NOT_SUBMITTED}")
        try:
            submit.click()
        except PlaywrightError as exc:
            raise _PageStop(
                "error",
                f"Google Form Submit button could not be clicked. {_NOT_SUBMITTED}",
            ) from exc
        clicked = True
        shown = _visible_confirmation(page, expected)
    except _PageStop as stop:
        if clicked:
            notes.append("Submit was clicked, but the confirmation message was not shown.")
        else:
            notes.append("Submit was not clicked.")
        page.screenshot(path=str(after_path), full_page=True)
        stop.observed = observed
        stop.before = str(before_path)
        stop.after = str(after_path)
        stop.notes = notes
        stop.page_title = page_title or stop.page_title
        raise
    notes.append(f"Confirmation shown: {shown}")
    notes.append("Submission does not mean the clinical validation passed.")


def _configured_confirmation(page: Page) -> str:
    """Read the confirmation sentence configured on this form."""

    try:
        html = page.content()
    except PlaywrightError as exc:
        raise _PageStop(
            "error",
            f"The form's confirmation message could not be identified. {_NOT_SUBMITTED}",
        ) from exc
    found = list(dict.fromkeys(item.strip() for item in _CONFIRMATION_RE.findall(html) if item.strip()))
    if len(found) != 1:
        raise _PageStop(
            "error",
            f"The form's confirmation message could not be identified. {_NOT_SUBMITTED}",
        )
    return found[0]


def _visible_confirmation(page: Page, expected: str) -> str:
    locator = page.get_by_text(expected, exact=True)
    try:
        locator.wait_for(state="visible", timeout=15_000)
    except PlaywrightError as exc:
        raise _PageStop(
            "error",
            "The confirmation message was not shown, so this case is not reported as submitted.",
        ) from exc
    shown = locator.inner_text().strip()
    if shown != expected:
        raise _PageStop(
            "error",
            "The confirmation message did not match the form, so this case is not reported as submitted.",
        )
    return shown


def _preflight_questions(
    page: Page,
    mapping_result: FieldMappingResult,
    target: GoogleFormTarget,
) -> None:
    for item in mapping_result.mapped_inputs:
        question = _question(page, item.form_question_title)
        if question.count() == 0:
            diagnostics = _question_diagnostics(page, item.form_question_title)
            raise _PageStop(
                "error",
                (
                    f"Google Form question '{item.form_question_title}' was not found. "
                    "The form structure does not match the mapping. "
                    "See execution evidence diagnostics for attempted selectors."
                ),
                page_title=_safe_title(page),
                diagnostics=diagnostics,
            )
        if _control_count(question, item) == 0:
            diagnostics = _question_diagnostics(page, item.form_question_title)
            if item.form_question_type == "date":
                diagnostics["date_question"] = _date_diagnostics(question)
            raise _PageStop(
                "error",
                (
                    f"Google Form question '{item.form_question_title}' does not match "
                    f"the configured type '{item.form_question_type}'. "
                    "The form structure does not match the mapping. "
                    "See execution evidence diagnostics for attempted selectors."
                ),
                page_title=_safe_title(page),
                diagnostics=diagnostics,
            )
        if _is_blank(item.value):
            continue
        choices = target.questions.get(item.source_field)
        if choices is not None and choices.choices and str(item.value) not in choices.choices:
            raise _PageStop(
                "error",
                (
                    f"{item.form_question_title} value '{item.value}' is not one of "
                    "the configured choices."
                ),
                page_title=_safe_title(page),
            )


def _wait_for_question_containers(page: Page) -> None:
    """Wait for semantic question containers, rather than a fixed delay."""

    try:
        page.wait_for_function(
            "() => document.querySelectorAll('[role=\"listitem\"]').length > 0",
            timeout=10_000,
        )
    except (AttributeError, PlaywrightError):
        # Preflight below records the resulting structure mismatch with diagnostics.
        return


def _question(page: Page, title: str):
    """Find a Google Forms question by its accessible title or labelled control.

    Google may append an asterisk or "Required" to a heading's accessible
    name. A textbox may also be the accessible element carrying the question
    title. Both forms remain constrained to a semantic listitem container.
    """

    pattern = _accessible_title_pattern(title)
    containers = page.get_by_role("listitem")
    heading_match = containers.filter(
        has=page.get_by_role("heading", name=pattern),
    )
    if heading_match.count():
        return heading_match
    return containers.filter(
        has=page.get_by_role("textbox", name=pattern),
    )


def _accessible_title_pattern(title: str) -> re.Pattern[str]:
    escaped = re.escape(title.strip())
    return re.compile(
        rf"^\s*{escaped}\s*(?:\*|(?:required(?:\s+question)?))?\s*$",
        re.IGNORECASE,
    )


def _question_diagnostics(page: Page, title: str) -> dict[str, object]:
    """Capture safe structure-only locator evidence; never record form values."""

    pattern = _accessible_title_pattern(title)
    return {
        "page_title": _safe_title(page),
        "page_url": page.url,
        "visible_question_titles": _all_inner_texts(page.get_by_role("heading")),
        "question_container_count": _safe_count(page.get_by_role("listitem")),
        "attempted_selectors": [
            (
                "role=listitem containing role=heading with accessible name "
                f"matching {pattern.pattern!r}"
            ),
            (
                "role=listitem containing role=textbox with accessible name "
                f"matching {pattern.pattern!r}"
            ),
        ],
        "heading_match_count": _safe_count(
            page.get_by_role("heading", name=pattern),
        ),
        "question_container_heading_match_count": _safe_count(
            page.get_by_role("listitem").filter(
                has=page.get_by_role("heading", name=pattern),
            ),
        ),
        "age_accessibility": _age_accessibility(page, pattern),
    }


def _safe_count(locator) -> int | None:
    try:
        return locator.count()
    except PlaywrightError:
        return None


def _all_inner_texts(locator) -> list[str]:
    try:
        return [text.strip() for text in locator.all_inner_texts() if text.strip()]
    except (AttributeError, PlaywrightError):
        return []


def _age_accessibility(page: Page, pattern: re.Pattern[str]) -> dict[str, object]:
    """Report semantic roles and ARIA labels relevant to Age question discovery."""

    result: dict[str, object] = {
        "heading_accessible_match_count": _safe_count(
            page.get_by_role("heading", name=pattern),
        ),
        "textbox_accessible_match_count": _safe_count(
            page.get_by_role("textbox", name=pattern),
        ),
        "textbox_count": _safe_count(page.get_by_role("textbox")),
    }
    try:
        result["textbox_labels"] = page.get_by_role("textbox").evaluate_all(
            """elements => elements.map(element => ({
                role: element.getAttribute('role') || element.tagName.toLowerCase(),
                aria_label: element.getAttribute('aria-label'),
                aria_labelledby: element.getAttribute('aria-labelledby'),
                name: element.getAttribute('name')
            }))"""
        )
    except (AttributeError, PlaywrightError):
        result["textbox_labels"] = []
    return result


def _date_diagnostics(question) -> dict[str, object]:
    """Return safe accessibility metadata for date controls in one container."""

    components: dict[str, object] = {}
    for component in ("Month", "Day", "Year"):
        pattern = _accessible_title_pattern(component)
        components[component] = {
            "textbox_match_count": _safe_count(
                question.get_by_role("textbox", name=pattern)
            ),
            "spinbutton_match_count": _safe_count(
                question.get_by_role("spinbutton", name=pattern)
            ),
            "label_match_count": _label_count(question, component),
        }
    try:
        inputs = question.locator("input").evaluate_all(
            """elements => elements.map(element => ({
                type: element.getAttribute('type'),
                role: element.getAttribute('role') || element.tagName.toLowerCase(),
                aria_label: element.getAttribute('aria-label'),
                aria_labelledby: element.getAttribute('aria-labelledby'),
                name: element.getAttribute('name')
            }))"""
        )
    except (AttributeError, PlaywrightError):
        inputs = []
    return {
        "visible_component_labels": ["Month", "Day", "Year"],
        "attempted_selectors": [
            "role=textbox with accessible component label",
            "role=spinbutton with accessible component label",
            "get_by_label(component) within the date question container",
            'single input[type="date"] within the date question container',
        ],
        "component_match_counts": components,
        "input_attributes": inputs,
    }


def _label_count(question, label: str) -> int | None:
    try:
        return _safe_count(question.get_by_label(label, exact=True))
    except AttributeError:
        return None


def _control_count(question, item: MappedInput) -> int:
    if item.form_question_type in {"short_text", "paragraph"}:
        return question.get_by_role("textbox").count()
    if item.form_question_type == "dropdown":
        return question.get_by_role("listbox").count()
    if item.form_question_type == "multiple_choice":
        return question.get_by_role("radio").count()
    if item.form_question_type == "date":
        if _date_controls(question) is not None:
            return 1
        return 1 if _native_date_control(question) is not None else 0
    return 0


def _fill_and_read(page: Page, item: MappedInput) -> str:
    if _is_blank(item.value):
        return ""
    question = _question(page, item.form_question_title)
    text = str(item.value)
    if item.form_question_type in {"short_text", "paragraph"}:
        box = question.get_by_role("textbox")
        box.fill(text)
        return box.input_value()
    if item.form_question_type == "dropdown":
        question.get_by_role("listbox").click()
        _wait_for_listbox(question, "true", item.form_question_title)
        page.get_by_role("option", name=text, exact=True).click()
        _wait_for_listbox(question, "false", item.form_question_title)
        return _selected_option(question.get_by_role("listbox"))
    if item.form_question_type == "multiple_choice":
        try:
            question.get_by_role("radio", name=text, exact=True).check()
        except PlaywrightError:
            # Google Forms sets aria-checked after the click, so check() can report no change.
            if not _radio_became_checked(question):
                raise
        checked = question.get_by_role("radio", checked=True)
        if checked.count() == 0:
            return ""
        label = checked.get_attribute("aria-label")
        if label and label.strip():
            return label.strip()
        return checked.inner_text().strip()
    if item.form_question_type == "date":
        parsed = _iso_date(text, item.form_question_title)
        controls = _date_controls(question)
        native = None if controls is not None else _native_date_control(question)
        if native is not None:
            native.fill(parsed.date().isoformat())
            return _read_native_date(native, item.form_question_title)
        if controls is None:
            raise _PageStop(
                "error",
                (
                    f"Google Form date controls for '{item.form_question_title}' "
                    "could not be reliably identified."
                ),
            )
        month, day, year = controls
        month.fill(str(parsed.month))
        day.fill(str(parsed.day))
        year.fill(str(parsed.year))
        return _read_iso_date(
            month,
            day,
            year,
            item.form_question_title,
        )
    raise ValueError(f"Unsupported question type '{item.form_question_type}'.")


def _wait_for_listbox(question, expanded: str, title: str) -> None:
    """Options are hidden until Google Forms finishes opening the listbox."""

    state = "open" if expanded == "true" else "close"
    try:
        question.locator(f'[role="listbox"][aria-expanded="{expanded}"]').wait_for(
            state="attached",
            timeout=5_000,
        )
    except PlaywrightError as exc:
        raise _PageStop(
            "error",
            f"Google Form dropdown for '{title}' did not {state}.",
        ) from exc


def _radio_became_checked(question) -> bool:
    try:
        question.locator('[role="radio"][aria-checked="true"]').wait_for(
            state="attached",
            timeout=5_000,
        )
    except PlaywrightError:
        return False
    return True


def _selected_option(listbox) -> str:
    """Read the selected choice; the listbox text lists every option."""

    values = listbox.locator('[role="option"][aria-selected="true"]').evaluate_all(
        "elements => elements.map(element => element.getAttribute('data-value') || '')"
    )
    chosen = {value.strip() for value in values if value and value.strip()}
    return chosen.pop() if len(chosen) == 1 else ""


def _date_controls(question):
    """Return Month, Day, Year controls from one question container.

    Published Google Forms date questions normally expose three accessible
    controls. The DOM input type may be text, number, or browser-specific, so
    type attributes are diagnostic evidence only; they are not the selector.
    """

    controls = tuple(
        _accessible_date_component(question, component)
        for component in ("Month", "Day", "Year")
    )
    if any(control is None for control in controls):
        return None
    return controls


def _accessible_date_component(question, component: str):
    pattern = _accessible_title_pattern(component)
    candidates = [
        question.get_by_role("textbox", name=pattern),
        question.get_by_role("spinbutton", name=pattern),
    ]
    try:
        candidates.append(question.get_by_label(component, exact=True))
    except AttributeError:
        pass
    for locator in candidates:
        if _safe_count(locator) == 1:
            return locator
    return None


def _native_date_control(question):
    """Return the single browser date input Google Forms renders in Chromium."""

    try:
        locator = question.locator('input[type="date"]')
    except AttributeError:
        return None
    return locator if _safe_count(locator) == 1 else None


def _read_native_date(control, title: str) -> str:
    value = control.input_value().strip()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise _PageStop(
            "error",
            (
                f"Google Form date control for '{title}' could not be read "
                "back as a valid date."
            ),
        ) from exc


def _read_iso_date(month, day, year, title: str) -> str:
    values = {
        "month": month.input_value().strip(),
        "day": day.input_value().strip(),
        "year": year.input_value().strip(),
    }
    try:
        return date(
            int(values["year"]),
            int(values["month"]),
            int(values["day"]),
        ).isoformat()
    except (TypeError, ValueError) as exc:
        raise _PageStop(
            "error",
            (
                f"Google Form date controls for '{title}' could not be read "
                "back as a valid date."
            ),
        ) from exc


def _iso_date(value: str, title: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise _PageStop(
            "error",
            f"{title} value '{value}' is not an ISO date.",
        ) from exc


def _expected_text(item: MappedInput) -> str:
    if _is_blank(item.value):
        return ""
    if item.form_question_type == "date":
        parsed = datetime.strptime(str(item.value), "%Y-%m-%d")
        return parsed.date().isoformat()
    return str(item.value)


def _environment_block(page: Page) -> str | None:
    current = page.url.lower()
    if "accounts.google.com" in current:
        return "Google sign-in is required. Credentials are not supplied."
    text = page.locator("body").inner_text().lower()
    if "recaptcha" in text or "captcha" in text:
        return "A CAPTCHA is present. It is not bypassed."
    if "you need permission" in text or "request access" in text:
        return "The form returned a permission error."
    return None


def _same_viewform(current: str, configured: str) -> bool:
    current_parts = urlparse(current)
    configured_parts = urlparse(configured)
    return (
        current_parts.scheme == configured_parts.scheme
        and current_parts.netloc == configured_parts.netloc
        and current_parts.path.rstrip("/") == configured_parts.path.rstrip("/")
    )


def _safe_title(page: Page) -> str:
    try:
        return page.title()
    except PlaywrightError:
        return ""


def _write_evidence(
    directory: Path,
    test_case_id: str,
    target: GoogleFormTarget,
    status: ExecutionStatus,
    entered: dict[str, InputValue],
    observed: dict[str, str],
    notes: list[str],
    before: str | None,
    after: str | None,
    page_title: str,
    form_mapping: FormMapping,
    diagnostics: dict[str, object],
) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_name(test_case_id)}_google_evidence.json"
    payload = {
        "test_case_id": test_case_id,
        "execution_mode": FILL_ONLY,
        "execution_status": status,
        "execution_target": "google_form",
        "target_url": target.url,
        "expected_title": target.expected_title,
        "page_title": page_title,
        "submitted": status == "submitted",
        "entered_values": entered,
        "observed_values": observed,
        "verification_notes": notes,
        "before_screenshot": before,
        "after_screenshot": after,
        "configured_entry_ids": {
            source: item.entry_id for source, item in target.questions.items()
        },
        "selector_strategy": (
            "Accessible question titles and labelled controls inside semantic "
            "question containers. Entry ids are metadata only."
        ),
        "diagnostics": diagnostics,
        "form_name": form_mapping.form_name,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _entered_values(mapping_result: FieldMappingResult) -> dict[str, InputValue]:
    return {item.form_question_title: item.value for item in mapping_result.mapped_inputs}


def _result(
    test_case_id: str,
    status: ExecutionStatus,
    entered: dict[str, InputValue],
    observed: dict[str, str],
    expected_behavior: str,
    started: datetime,
    error_message: str | None,
    *,
    screenshot: str | None = None,
    before_screenshot: str | None = None,
    evidence: str | None = None,
    notes: list[str] | None = None,
    url: str | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        test_case_id=test_case_id,
        execution_mode=FILL_ONLY,
        execution_status=status,
        execution_target="google_form",
        target_url=url,
        entered_values=entered,
        observed_values=observed,
        expected_behavior=expected_behavior,
        screenshot_path=screenshot,
        before_screenshot_path=before_screenshot,
        evidence_path=evidence,
        verification_notes=notes or [],
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
    return message[:500]
