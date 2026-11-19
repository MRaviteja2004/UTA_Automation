"""Mocked Google Forms fill-only tests. No live Google access."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from execution.approval_gate import approve_test_case
from execution.field_mapping import (
    FieldMappingResult,
    MappedInput,
    load_form_mapping,
    map_test_case_inputs,
)
from execution.google_forms_runner import execute_google_form_fill_only
from models.approval import ApprovedCase, BlockedCase
from models.test_cases import GeneratedTestCase

REVIEWED_AT = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CDM_BEHAVIOR = "CDM confirms the stated form behavior."
RUNNER_SOURCE = Path(__file__).resolve().parents[1] / "execution" / "google_forms_runner.py"
FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSe5t_std9cC210P34FUQ88t8w1O7LlHX9qF7lDa_P_WS73YXQ/viewform"
)
CONFIRMATION = "Your synthetic test response has been recorded."
NATIVE_DATE_SELECTOR = 'input[type="date"]'


class FakeLocator:
    def __init__(
        self,
        page: FakePage,
        role: str | None = None,
        name: str | re.Pattern[str] | None = None,
        question: str | None = None,
        checked: bool | None = None,
        filter_role: str | None = None,
    ) -> None:
        self.page = page
        self.role = role
        self.name = name
        self.question = question
        self.checked = checked
        self.filter_role = filter_role

    def _component_name(self) -> str | None:
        if isinstance(self.name, str):
            return self.name
        if isinstance(self.name, re.Pattern):
            return next(
                (
                    component
                    for component in ("Month", "Day", "Year")
                    if self.name.search(component)
                ),
                None,
            )
        return None

    def filter(self, has: FakeLocator | None = None) -> FakeLocator:
        title = None if has is None else has.name
        if isinstance(title, re.Pattern):
            title = next(
                (
                    candidate
                    for candidate in self.page.question_titles
                    if title.search(candidate)
                    or title.search(f"{candidate} *")
                    or title.search(f"{candidate} Required")
                ),
                None,
            )
        return FakeLocator(
            self.page,
            role="listitem",
            name=title,
            question=title,
            filter_role=None if has is None else has.role,
        )

    def get_by_role(self, role: str, name: str | None = None, exact: bool | None = None, checked: bool | None = None) -> FakeLocator:
        del exact
        scoped_name = self.question if name is None else name
        if isinstance(scoped_name, re.Pattern) and role in {"textbox", "spinbutton"}:
            scoped_name = next(
                (
                    component
                    for component in ("Month", "Day", "Year")
                    if scoped_name.search(component)
                ),
                scoped_name,
            )
        return FakeLocator(
            self.page,
            role=role,
            name=scoped_name,
            question=self.question,
            checked=checked,
        )

    def count(self) -> int:
        if self.role == "button" and self.name == "Submit" and self.page.submit_missing:
            return 0
        if self.role == "text":
            return 1 if self.name and self.name in self.page.body else 0
        if self.role == "input" and self.name == NATIVE_DATE_SELECTOR:
            return 1 if self.question in self.page.native_date_questions else 0
        if self.role == "listitem" and self.question is None:
            return len(self.page.question_titles)
        if self.role == "listitem" and self.question in self.page.missing_questions:
            return 0
        if (
            self.role == "listitem"
            and self.filter_role == "heading"
            and self.question in self.page.heading_missing_questions
        ):
            return 0
        if (
            self.role == "heading"
            and isinstance(self.name, re.Pattern)
            and not any(
                self.name.search(candidate)
                or self.name.search(f"{candidate} *")
                or self.name.search(f"{candidate} Required")
                for candidate in self.page.question_titles
                if candidate not in self.page.heading_missing_questions
            )
        ):
            return 0
        component = self._component_name()
        if (
            self.role == "spinbutton"
            and (self.question, component) not in self.page.spinbutton_components
        ):
            return 0
        if (self.question, self.role, component) in self.page.absent_controls:
            return 0
        if (self.question, self.role) in self.page.absent_roles:
            return 0
        if self.role == "radio" and self.checked:
            return 1 if self.page.checked.get(self.question) else 0
        return 1

    def fill(self, value: str) -> None:
        key = (self.question, self.role, self.name)
        self.page.fills.append((*key, value))
        self.page.values[key] = value

    def click(self) -> None:
        self.page.clicks.append((self.question, self.role, self.name))
        if self.role == "listbox":
            self.page.last_listbox = self.question
        if self.role == "option":
            self.page.listbox_text[self.page.last_listbox] = self.name or ""
        if self.role == "button" and self.name == "Submit":
            self.page.body = (
                ""
                if self.page.hide_confirmation
                else self.page.shown_confirmation or self.page.expected_confirmation
            )

    def check(self) -> None:
        self.page.checks.append((self.question, self.name))
        if self.question in self.page.ignored_radios:
            raise PlaywrightError("Clicking the checkbox did not change its state")
        self.page.checked[self.question] = self.name or ""
        if self.question in self.page.late_radios:
            raise PlaywrightError("Clicking the checkbox did not change its state")

    def input_value(self) -> str:
        key = (self.question, self.role, self.name)
        if key in self.page.readback:
            return self.page.readback[key]
        return self.page.values.get(key, "")

    def inner_text(self) -> str:
        if self.name == "body":
            return self.page.body
        if self.role == "listbox":
            override = self.page.readback.get((self.question, "listbox", self.name))
            if override is not None:
                return override
            return self.page.listbox_text.get(self.question, "")
        if self.role == "radio":
            return self.page.checked.get(self.question, "")
        if self.role == "text" and self.name and self.name in self.page.body:
            return str(self.name)
        return ""

    def get_attribute(self, name: str) -> str | None:
        if name == "aria-label" and self.role == "radio":
            return self.page.checked.get(self.question)
        return None

    def all_inner_texts(self) -> list[str]:
        if self.role == "heading":
            return [
                self.page.heading_labels.get(title, title)
                for title in self.page.question_titles
                if title not in self.page.heading_missing_questions
            ]
        return []

    def evaluate_all(self, expression: str) -> list:
        del expression
        if self.role == "selected_option":
            override = self.page.readback.get((self.question, "listbox", self.question))
            chosen = override if override is not None else self.page.listbox_text.get(self.question, "")
            return [chosen, ""]
        if self.role == "textbox":
            return [
                {
                    "role": "textbox",
                    "aria_label": self.page.textbox_labels.get(title, title),
                    "aria_labelledby": None,
                    "name": None,
                }
                for title in self.page.question_titles
            ]
        if self.role == "input" and self.question == "Screening Date":
            return [
                {
                    "type": "text",
                    "role": "textbox",
                    "aria_label": component,
                    "aria_labelledby": None,
                    "name": None,
                }
                for component in ("Month", "Day", "Year")
            ]
        return []

    def locator(self, selector: str) -> FakeLocator:
        if self.role == "listbox":
            return FakeLocator(self.page, role="selected_option", question=self.question)
        return FakeLocator(self.page, role="input", question=self.question, name=selector)

    def wait_for(self, state: str | None = None, timeout: float | None = None) -> None:
        del state, timeout
        self.page.waits.append((self.question, self.name))
        if self.question in self.page.unopened_listboxes and 'aria-expanded="true"' in str(self.name):
            raise PlaywrightError("Timeout 5000ms exceeded.")
        if 'aria-checked="true"' in str(self.name) and not self.page.checked.get(self.question):
            raise PlaywrightError("Timeout 5000ms exceeded.")
        if self.role == "text" and (not self.name or self.name not in self.page.body):
            raise PlaywrightError("Timeout 15000ms exceeded.")


class FakePage:
    def __init__(
        self,
        *,
        title: str = "Clinical Study Data Collection - UAT Demo",
        body: str = "Synthetic demo form",
        redirect_url: str | None = None,
    ) -> None:
        self._title = title
        self.body = body
        self.redirect_url = redirect_url
        self.url = ""
        self.missing_questions: set[str] = set()
        self.heading_missing_questions: set[str] = set()
        self.absent_roles: set[tuple[str | None, str | None]] = set()
        self.absent_controls: set[tuple[str | None, str | None, str | None]] = set()
        self.spinbutton_components: set[tuple[str | None, str | None]] = set()
        self.native_date_questions: set[str] = set()
        self.unopened_listboxes: set[str] = set()
        self.late_radios: set[str] = set()
        self.ignored_radios: set[str] = set()
        self.waits: list[tuple] = []
        self.form_body = body
        self.expected_confirmation = CONFIRMATION
        self.shown_confirmation: str | None = None
        self.hide_confirmation = False
        self.submit_missing = False
        self.confirmation_html: str | None = None
        self.visits: list[str] = []
        self.question_titles = [
            "Subject ID",
            "Age",
            "Gender",
            "Informed Consent",
            "Screening Date",
            "Follow-up Date",
            "Height (cm)",
            "Weight (kg)",
        ]
        self.heading_labels: dict[str, str] = {}
        self.textbox_labels: dict[str, str] = {}
        self.fills: list[tuple] = []
        self.clicks: list[tuple] = []
        self.checks: list[tuple] = []
        self.values: dict[tuple, str] = {}
        self.readback: dict[tuple, str] = {}
        self.checked: dict[str | None, str] = {}
        self.listbox_text: dict[str | None, str] = {}
        self.last_listbox: str | None = None
        self.screenshots: list[str] = []

    def set_default_timeout(self, timeout: int) -> None:
        del timeout

    def goto(self, url: str, wait_until: str | None = None) -> None:
        del wait_until
        self.visits.append(url)
        self.url = self.redirect_url or url
        self.values.clear()
        self.checked.clear()
        self.listbox_text.clear()
        self.body = self.form_body

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        if self.confirmation_html is not None:
            return self.confirmation_html
        if not self.expected_confirmation:
            return "<html></html>"
        return f'["{self.expected_confirmation}"],null,null,null,null,null,null,1'

    def get_by_text(self, text: str, exact: bool | None = None) -> FakeLocator:
        del exact
        return FakeLocator(self, role="text", name=text)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, name=selector)

    def get_by_role(self, role: str, name: str | None = None, exact: bool | None = None, checked: bool | None = None) -> FakeLocator:
        del exact
        return FakeLocator(self, role=role, name=name, checked=checked)

    def screenshot(self, path: str, full_page: bool = False) -> None:
        del full_page
        Path(path).write_bytes(b"png")
        self.screenshots.append(path)


def _case(test_case_id: str = "TC-GOOGLE-01", **overrides: object) -> GeneratedTestCase:
    data: dict[str, object] = {
        "test_case_id": test_case_id,
        "requirement_id": "VAL-001",
        "requirement_ids": ["VAL-001"],
        "title": "Synthetic screening case",
        "description": "Synthetic values for the demo form.",
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
        comment="Confirmed for the demo form.",
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


def _opener(page: FakePage):
    @contextmanager
    def _open(headless: bool):
        del headless
        page.opened = True
        yield page

    return _open


def _run(tmp_path: Path, page: FakePage | None = None, case: ApprovedCase | None = None, mapping: FieldMappingResult | None = None, **kwargs):
    approved, ready = _ready(case)
    return execute_google_form_fill_only(
        approved if case is None else case,
        ready if mapping is None else mapping,
        screenshot_dir=tmp_path,
        page_opener=None if page is None else _opener(page),
        **kwargs,
    )


def test_mapping_marks_age_and_gender_required() -> None:
    required = {item.source_field: item.required for item in load_form_mapping().mappings}

    assert required["age"] is True
    assert required["gender"] is True


def test_short_answer_dropdown_radio_and_date_are_read_back(tmp_path: Path) -> None:
    page = FakePage()
    result = _run(tmp_path, page)

    assert result.execution_status == "submitted"
    assert result.execution_mode == "FILL_ONLY"
    assert result.execution_target == "google_form"
    assert result.execution_status != "passed"
    assert result.expected_behavior == CDM_BEHAVIOR
    assert result.entered_values["Subject ID"] == "SUBJ-001"
    assert result.observed_values["Subject ID"] == "SUBJ-001"
    assert result.observed_values["Age"] == "30"
    assert result.observed_values["Gender"] == "Female"
    assert result.observed_values["Informed Consent"] == "Yes"
    assert result.observed_values["Screening Date"] == "2026-10-01"
    assert result.observed_values["Follow-up Date"] == "2026-10-08"
    assert ("Subject ID", "textbox", "Subject ID", "SUBJ-001") in page.fills
    assert ("Age", "textbox", "Age", "30") in page.fills
    assert (None, "option", "Female") in page.clicks
    assert (None, "option", "Male") not in page.clicks
    assert ("Informed Consent", "Yes") in page.checks
    assert ("Screening Date", "textbox", "Month", "10") in page.fills
    assert ("Screening Date", "textbox", "Day", "1") in page.fills
    assert ("Screening Date", "textbox", "Year", "2026") in page.fills
    assert page.visits == [FORM_URL]
    assert "Read-back matched for Age." in result.verification_notes
    assert f"Confirmation shown: {CONFIRMATION}" in result.verification_notes
    assert "Submission does not mean the clinical validation passed." in result.verification_notes
    assert (None, "button", "Submit") in page.clicks
    assert Path(result.before_screenshot_path or "").is_file()
    assert Path(result.screenshot_path or "").is_file()
    evidence = json.loads(Path(result.evidence_path or "").read_text(encoding="utf-8"))
    assert evidence["submitted"] is True
    assert evidence["execution_status"] == "submitted"
    assert evidence["target_url"] == FORM_URL
    assert evidence["configured_entry_ids"]["age"] == "1636364263"
    assert "Entry ids" in evidence["selector_strategy"]


def test_age_uses_labelled_textbox_when_required_heading_is_not_exposed(
    tmp_path: Path,
) -> None:
    page = FakePage()
    page.heading_missing_questions.add("Age")
    page.textbox_labels["Age"] = "Age Required"

    result = _run(tmp_path, page)

    assert result.execution_status == "submitted", result.error_message
    assert ("Age", "textbox", "Age", "30") in page.fills


def test_date_components_are_filled_month_day_year_and_read_back_as_iso(
    tmp_path: Path,
) -> None:
    page = FakePage()
    result = _run(tmp_path, page)
    screening_fills = [
        fill for fill in page.fills if fill[0] == "Screening Date"
    ]

    assert screening_fills == [
        ("Screening Date", "textbox", "Month", "10"),
        ("Screening Date", "textbox", "Day", "1"),
        ("Screening Date", "textbox", "Year", "2026"),
    ]
    assert result.observed_values["Screening Date"] == "2026-10-01"
    assert result.observed_values["Follow-up Date"] == "2026-10-08"


def test_date_question_accepts_accessible_spinbutton_components(tmp_path: Path) -> None:
    page = FakePage()
    page.absent_controls.update(
        {
            ("Screening Date", "textbox", "Month"),
            ("Screening Date", "textbox", "Day"),
            ("Screening Date", "textbox", "Year"),
        }
    )
    page.spinbutton_components.update(
        {
            ("Screening Date", "Month"),
            ("Screening Date", "Day"),
            ("Screening Date", "Year"),
        }
    )

    result = _run(tmp_path, page)

    assert result.execution_status == "submitted", result.error_message
    assert ("Screening Date", "spinbutton", "Month", "10") in page.fills
    assert result.observed_values["Screening Date"] == "2026-10-01"


def test_date_question_rendered_as_one_native_date_input_is_filled_in_iso(
    tmp_path: Path,
) -> None:
    page = FakePage()
    for title in ("Screening Date", "Follow-up Date"):
        page.native_date_questions.add(title)
        page.absent_controls.update(
            {(title, role, part) for role in ("textbox", "spinbutton") for part in ("Month", "Day", "Year")}
        )

    result = _run(tmp_path, page)

    assert result.execution_status == "submitted", result.error_message
    assert ("Screening Date", "input", NATIVE_DATE_SELECTOR, "2026-10-01") in page.fills
    assert result.observed_values["Screening Date"] == "2026-10-01"
    assert result.observed_values["Follow-up Date"] == "2026-10-08"
    assert page.clicks == [
        ("Gender", "listbox", "Gender"),
        (None, "option", "Female"),
        (None, "button", "Submit"),
    ]


def test_dropdown_waits_for_the_listbox_to_open_and_reads_the_selected_option(
    tmp_path: Path,
) -> None:
    page = FakePage()

    result = _run(tmp_path, page)

    assert result.execution_status == "submitted", result.error_message
    assert result.observed_values["Gender"] == "Female"
    assert page.waits == [
        ("Gender", '[role="listbox"][aria-expanded="true"]'),
        ("Gender", '[role="listbox"][aria-expanded="false"]'),
        (None, CONFIRMATION),
    ]


def test_dropdown_that_does_not_open_is_an_error_without_choosing(tmp_path: Path) -> None:
    page = FakePage()
    page.unopened_listboxes.add("Gender")

    result = _run(tmp_path, page)

    assert result.execution_status == "error"
    assert "dropdown for 'Gender' did not open" in (result.error_message or "")
    assert (None, "option", "Female") not in page.clicks
    assert result.execution_status != "passed"


def test_radio_that_google_marks_checked_after_the_click_is_read_back(tmp_path: Path) -> None:
    page = FakePage()
    page.late_radios.add("Informed Consent")

    result = _run(tmp_path, page)

    assert result.execution_status == "submitted", result.error_message
    assert result.observed_values["Informed Consent"] == "Yes"
    assert ("Informed Consent", '[role="radio"][aria-checked="true"]') in page.waits


def test_radio_that_never_becomes_checked_is_an_error(tmp_path: Path) -> None:
    page = FakePage()
    page.ignored_radios.add("Informed Consent")

    result = _run(tmp_path, page)

    assert result.execution_status == "error"
    assert "did not change its state" in (result.error_message or "")
    assert "Informed Consent" not in result.observed_values


def _blank_subject_id_case(age: int | None) -> GeneratedTestCase:
    supporting = ["Age", "Gender", "Informed Consent", "Screening Date"]
    return _case(
        "TC-VAL-001-mandatory-01",
        category="mandatory",
        title="Mandatory Subject ID blank",
        expected_result="Reject",
        input_data={
            "Subject ID": None,
            "Age": age,
            "Gender": "Female",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-01",
        },
        test_data_classification={
            "Subject ID": "under_test",
            **{field: "supporting" for field in supporting},
        },
        supporting_requirement_ids={field: ["VAL-002"] for field in supporting},
    )


def test_blank_subject_id_under_test_is_left_blank_and_other_answers_are_filled(
    tmp_path: Path,
) -> None:
    approved, mapping = _ready(_approved(_blank_subject_id_case(31)))
    page = FakePage()

    result = execute_google_form_fill_only(
        approved,
        mapping,
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "submitted", result.error_message
    assert result.observed_values["Subject ID"] == ""
    assert "Subject ID was left blank." in result.verification_notes
    assert not any(fill[0] == "Subject ID" for fill in page.fills)
    assert ("Age", "textbox", "Age", "31") in page.fills
    assert ("Informed Consent", "Yes") in page.checks
    assert f"Confirmation shown: {CONFIRMATION}" in result.verification_notes
    assert (None, "button", "Submit") in page.clicks


def test_blank_screening_date_string_under_test_is_left_blank(tmp_path: Path) -> None:
    supporting = ["Subject ID", "Age", "Gender", "Informed Consent"]
    case = _case(
        "TC-VAL-004-date-01",
        category="date",
        title="Screening Date blank",
        expected_result="Reject",
        input_data={
            "Screening Date": "",
            "Subject ID": "SUBJ-1015",
            "Age": 30,
            "Gender": "Not Reported",
            "Informed Consent": "Yes",
        },
        test_data_classification={
            "Screening Date": "under_test",
            **{field: "supporting" for field in supporting},
        },
        supporting_requirement_ids={field: ["VAL-002"] for field in supporting},
    )
    approved, mapping = _ready(_approved(case))
    page = FakePage()

    result = execute_google_form_fill_only(
        approved,
        mapping,
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "submitted", result.error_message
    assert result.observed_values["Screening Date"] == ""
    assert "Screening Date was left blank." in result.verification_notes
    assert not any(fill[0] == "Screening Date" for fill in page.fills)


def test_blank_required_supporting_value_is_still_blocked(tmp_path: Path) -> None:
    approved, mapping = _ready(_approved(_blank_subject_id_case(None)))
    page = FakePage()

    result = execute_google_form_fill_only(
        approved,
        mapping,
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "blocked"
    assert "'Age' has no value" in (result.error_message or "")
    assert page.fills == []


def test_missing_date_component_blocks_with_date_diagnostics(tmp_path: Path) -> None:
    page = FakePage()
    page.absent_controls.add(("Screening Date", "textbox", "Year"))

    result = _run(tmp_path, page)

    assert result.execution_status == "error"
    assert "Screening Date" in (result.error_message or "")
    assert "configured type 'date'" in (result.error_message or "")
    evidence = json.loads(Path(result.evidence_path or "").read_text(encoding="utf-8"))
    diagnostics = evidence["diagnostics"]["date_question"]
    assert diagnostics["visible_component_labels"] == ["Month", "Day", "Year"]
    assert diagnostics["component_match_counts"]["Year"]["textbox_match_count"] == 0
    assert diagnostics["input_attributes"][0]["aria_label"] == "Month"
    assert len(diagnostics["attempted_selectors"]) == 4


def test_date_readback_mismatch_is_an_error_with_diagnostics(tmp_path: Path) -> None:
    page = FakePage()
    page.readback[("Screening Date", "textbox", "Year")] = "2025"

    result = _run(tmp_path, page)

    assert result.execution_status == "error"
    assert result.observed_values["Screening Date"] == "2025-10-01"
    assert "Read-back for 'Screening Date' did not match" in (result.error_message or "")
    evidence = json.loads(Path(result.evidence_path or "").read_text(encoding="utf-8"))
    assert "date_question" in evidence["diagnostics"]


def test_none_optional_value_stays_blank(tmp_path: Path) -> None:
    page = FakePage()
    approved, mapping = _ready(
        _approved(
            _case(
                input_data={
                    "Subject ID": "SUBJ-001",
                    "Age": 30,
                    "Gender": "Female",
                    "Informed Consent": "Yes",
                    "Screening Date": "2026-10-01",
                    "Height (cm)": None,
                }
            )
        )
    )
    result = execute_google_form_fill_only(
        approved,
        mapping,
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "submitted", result.error_message
    assert result.entered_values["Height (cm)"] is None
    assert result.observed_values["Height (cm)"] == ""
    assert all(item[0] != "Height (cm)" for item in page.fills)


def test_read_back_mismatch_is_an_error(tmp_path: Path) -> None:
    page = FakePage()
    page.readback[("Age", "textbox", "Age")] = "31"
    result = _run(tmp_path, page)

    assert result.execution_status == "error"
    assert result.error_message == "Read-back for 'Age' did not match the entered value."
    assert result.observed_values["Age"] == "31"
    assert result.execution_status != "passed"
    assert (None, "button", "Submit") not in page.clicks
    assert "Submit was not clicked." in result.verification_notes
    evidence = json.loads(Path(result.evidence_path or "").read_text(encoding="utf-8"))
    assert evidence["submitted"] is False


def test_missing_age_blocks_without_opening_a_browser(tmp_path: Path) -> None:
    approved, mapping = _ready()
    trimmed = mapping.model_copy(
        update={
            "mapped_inputs": [
                item for item in mapping.mapped_inputs if item.source_field != "age"
            ]
        }
    )
    page = FakePage()

    result = execute_google_form_fill_only(
        approved,
        trimmed,
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "blocked"
    assert "Age" in (result.error_message or "")
    assert "not invented" in (result.error_message or "")
    assert page.url == ""
    assert result.entered_values == {}


def test_missing_gender_blocks_without_inventing_a_value(tmp_path: Path) -> None:
    approved, mapping = _ready()
    trimmed = mapping.model_copy(
        update={
            "mapped_inputs": [
                item for item in mapping.mapped_inputs if item.source_field != "gender"
            ]
        }
    )
    page = FakePage()
    result = execute_google_form_fill_only(
        approved,
        trimmed,
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "blocked"
    assert "Gender" in (result.error_message or "")
    assert "Female" not in (result.error_message or "")
    assert page.fills == []


def test_blank_required_age_is_not_populated(tmp_path: Path) -> None:
    approved, mapping = _ready()
    mapped = []
    for item in mapping.mapped_inputs:
        if item.source_field == "age":
            mapped.append(item.model_copy(update={"value": None}))
        else:
            mapped.append(item)
    page = FakePage()
    result = execute_google_form_fill_only(
        approved,
        mapping.model_copy(update={"mapped_inputs": mapped}),
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "blocked"
    assert "Age" in (result.error_message or "")
    assert "has no value" in (result.error_message or "")
    assert page.fills == []


def test_raw_blocked_and_unready_cases_are_rejected(tmp_path: Path) -> None:
    approved, mapping = _ready()
    page = FakePage()
    opener = _opener(page)
    raw = execute_google_form_fill_only(
        approved.test_case,
        mapping,
        screenshot_dir=tmp_path,
        page_opener=opener,
    )
    blocked = execute_google_form_fill_only(
        BlockedCase(test_case=approved.test_case, reasons=["Decision is rejected."]),
        mapping,
        screenshot_dir=tmp_path,
        page_opener=opener,
    )
    other = mapping.model_copy(update={"test_case_id": "TC-OTHER"})
    mismatch = execute_google_form_fill_only(
        approved,
        other,
        screenshot_dir=tmp_path,
        page_opener=opener,
    )
    not_ready = mapping.model_copy(
        update={
            "status": "blocked",
            "validation_errors": ["Required form question 'Gender' cannot receive a value."],
        }
    )
    mapping_blocked = execute_google_form_fill_only(
        approved,
        not_ready,
        screenshot_dir=tmp_path,
        page_opener=opener,
    )

    assert raw.error_message == "Raw generated test cases cannot be executed."
    assert blocked.error_message == "Blocked execution-plan cases cannot be executed."
    assert mismatch.error_message == "Mapping result is for a different test case."
    assert mapping_blocked.execution_status == "blocked"
    assert "Gender" in (mapping_blocked.error_message or "")
    assert page.url == ""


def test_navigation_is_limited_to_the_configured_viewform(tmp_path: Path) -> None:
    approved, mapping = _ready()
    config = load_form_mapping()
    assert config.google_form is not None
    rejected = config.model_copy(
        update={
            "google_form": config.google_form.model_copy(
                update={"url": "https://docs.google.com/forms/d/e/abc/formResponse"}
            )
        }
    )
    page = FakePage()
    result = execute_google_form_fill_only(
        approved,
        mapping,
        config=rejected,
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "blocked"
    assert "viewform" in (result.error_message or "")
    assert page.url == ""


def test_redirect_away_from_viewform_is_an_error(tmp_path: Path) -> None:
    page = FakePage(redirect_url="https://accounts.google.com/v3/signin")
    result = _run(tmp_path, page)

    assert result.execution_status == "blocked"
    assert "sign-in" in (result.error_message or "")
    assert page.fills == []
    assert Path(result.screenshot_path or "").is_file()


def test_captcha_and_permission_stop_before_fill(tmp_path: Path) -> None:
    captcha = FakePage(body="Please solve the reCAPTCHA challenge")
    permission = FakePage(body="You need permission to access this form")
    captcha_result = _run(tmp_path, captcha)
    permission_result = _run(tmp_path, permission)

    assert captcha_result.execution_status == "blocked"
    assert "CAPTCHA" in (captcha_result.error_message or "")
    assert captcha.fills == []
    assert permission_result.execution_status == "blocked"
    assert "permission" in (permission_result.error_message or "")
    assert permission.fills == []


def test_title_question_and_type_mismatches_are_errors(tmp_path: Path) -> None:
    title_page = FakePage(title="Another form")
    missing = FakePage()
    missing.missing_questions.add("Age")
    wrong_type = FakePage()
    wrong_type.absent_roles.add(("Gender", "listbox"))

    title_result = _run(tmp_path, title_page)
    missing_result = _run(tmp_path, missing)
    type_result = _run(tmp_path, wrong_type)

    assert title_result.execution_status == "error"
    assert "title" in (title_result.error_message or "")
    assert title_page.fills == []
    assert missing_result.execution_status == "error"
    assert "Age" in (missing_result.error_message or "")
    assert "structure" in (missing_result.error_message or "")
    missing_evidence = json.loads(
        Path(missing_result.evidence_path or "").read_text(encoding="utf-8")
    )
    diagnostics = missing_evidence["diagnostics"]
    assert diagnostics["page_title"] == "Clinical Study Data Collection - UAT Demo"
    assert diagnostics["page_url"] == FORM_URL
    assert "Age" in diagnostics["visible_question_titles"]
    assert diagnostics["question_container_count"] == 8
    assert diagnostics["heading_match_count"] == 1
    assert diagnostics["question_container_heading_match_count"] >= 0
    assert diagnostics["age_accessibility"]["textbox_count"] >= 1
    assert len(diagnostics["attempted_selectors"]) == 2
    assert type_result.execution_status == "error"
    assert "Gender" in (type_result.error_message or "")
    assert "dropdown" in (type_result.error_message or "")


def test_choice_outside_the_configured_list_is_not_entered(tmp_path: Path) -> None:
    approved, mapping = _ready()
    mapped = []
    for item in mapping.mapped_inputs:
        if item.source_field == "gender":
            mapped.append(item.model_copy(update={"value": "Other"}))
        else:
            mapped.append(item)
    page = FakePage()
    result = execute_google_form_fill_only(
        approved,
        mapping.model_copy(update={"mapped_inputs": mapped}),
        screenshot_dir=tmp_path,
        page_opener=_opener(page),
    )

    assert result.execution_status == "error"
    assert "configured choices" in (result.error_message or "")
    assert page.clicks == []


def test_submit_clicks_the_button_and_does_not_post_the_response(tmp_path: Path) -> None:
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    clicks = [line.strip() for line in source.splitlines() if ".click(" in line]

    assert clicks == [
        "submit.click()",
        'question.get_by_role("listbox").click()',
        'page.get_by_role("option", name=text, exact=True).click()',
    ]
    assert "form.submit" not in source
    assert "/formResponse" not in source
    assert "request.post" not in source
    page = FakePage()
    result = _run(tmp_path, page)
    assert result.execution_status == "submitted"
    assert result.execution_status != "passed"
    evidence = json.loads(Path(result.evidence_path or "").read_text(encoding="utf-8"))
    assert evidence["submitted"] is True


def test_missing_confirmation_is_not_reported_as_submitted(tmp_path: Path) -> None:
    page = FakePage()
    page.hide_confirmation = True

    result = _run(tmp_path, page)

    assert result.execution_status == "error"
    assert result.execution_status != "submitted"
    assert "not reported as submitted" in (result.error_message or "")
    assert (None, "button", "Submit") in page.clicks
    evidence = json.loads(Path(result.evidence_path or "").read_text(encoding="utf-8"))
    assert evidence["submitted"] is False


def test_unexpected_confirmation_is_not_reported_as_submitted(tmp_path: Path) -> None:
    page = FakePage()
    page.shown_confirmation = "Thanks for your time."

    result = _run(tmp_path, page)

    assert result.execution_status == "error"
    assert "not reported as submitted" in (result.error_message or "")
    evidence = json.loads(Path(result.evidence_path or "").read_text(encoding="utf-8"))
    assert evidence["submitted"] is False
    assert evidence["execution_status"] != "submitted"


def test_unidentified_confirmation_message_prevents_submission(tmp_path: Path) -> None:
    page = FakePage()
    page.expected_confirmation = ""

    result = _run(tmp_path, page)

    assert result.execution_status == "error"
    assert "could not be identified" in (result.error_message or "")
    assert "Submit was not clicked." in result.verification_notes
    assert (None, "button", "Submit") not in page.clicks


def test_each_test_case_opens_its_own_blank_form(tmp_path: Path) -> None:
    opened: list[FakePage] = []

    @contextmanager
    def opener(headless: bool):
        del headless
        page = FakePage()
        opened.append(page)
        yield page

    first, first_mapping = _ready()
    second, second_mapping = _ready(
        _approved(
            _case(
                "TC-GOOGLE-02",
                input_data={
                    "Subject ID": "SUBJ-002",
                    "Age": 31,
                    "Gender": "Male",
                    "Informed Consent": "No",
                    "Screening Date": "2026-10-02",
                    "Follow-up Date": "2026-10-09",
                    "Height (cm)": 170,
                    "Weight (kg)": 70,
                },
            )
        )
    )

    first_result = execute_google_form_fill_only(
        first,
        first_mapping,
        screenshot_dir=tmp_path,
        page_opener=opener,
    )
    second_result = execute_google_form_fill_only(
        second,
        second_mapping,
        screenshot_dir=tmp_path,
        page_opener=opener,
    )

    assert first_result.execution_status == "submitted"
    assert second_result.execution_status == "submitted"
    assert len(opened) == 2
    assert opened[0] is not opened[1]
    assert opened[0].visits == [FORM_URL]
    assert opened[1].visits == [FORM_URL]
    assert "SUBJ-001" in opened[0].values.values()
    assert "SUBJ-002" in opened[1].values.values()
    assert "SUBJ-001" not in opened[1].values.values()
    assert opened[1].body == CONFIRMATION


def test_hand_built_mapping_keeps_required_metadata() -> None:
    item = MappedInput(
        source_field="age",
        form_question_title="Age",
        form_question_type="short_text",
        value=30,
    )
    assert item.value == 30
