"""Read-only Google Form metadata tests. No live Google access."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from execution.google_forms_metadata import _EXTRACT_SCRIPT, extract_google_form_metadata

FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSe5t_std9cC210P34FUQ88t8w1O7LlHX9qF7lDa_P_WS73YXQ/viewform"
)
CAPTURED = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
SOURCE = Path(__file__).resolve().parents[1] / "execution" / "google_forms_metadata.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "google_form_questions.html"


class FakeLocator:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def inner_text(self) -> str:
        return self.page.body


class FakePage:
    def __init__(
        self,
        snapshot: dict | None = None,
        *,
        title: str = "Clinical Study Data Collection - UAT Demo",
        body: str = "Synthetic demo form",
        redirect_url: str | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot or {"listitem_count": 0, "questions": []}
        self._title = title
        self.body = body
        self.redirect_url = redirect_url
        self.fail = fail
        self.url = ""
        self.evaluated = False

    def goto(self, url: str, wait_until: str | None = None) -> None:
        del wait_until
        if self.fail is not None:
            raise self.fail
        self.url = self.redirect_url or url

    def title(self) -> str:
        return self._title

    def locator(self, selector: str) -> FakeLocator:
        del selector
        return FakeLocator(self)

    def wait_for_function(self, expression: str, timeout: int | None = None) -> None:
        del expression, timeout

    def evaluate(self, expression: str) -> dict:
        del expression
        self.evaluated = True
        return self.snapshot


def _open(page: FakePage):
    @contextmanager
    def opener(headless: bool):
        del headless
        yield page

    return opener


def _extract(page: FakePage, captured_at: datetime = CAPTURED):
    return extract_google_form_metadata(
        FORM_URL,
        captured_at=captured_at,
        page_opener=_open(page),
    )


def _question(**overrides: object) -> dict:
    raw = {
        "visible_title": "Subject ID",
        "accessible_label": "Subject ID",
        "aria_required": "",
        "controls": [
            {
                "tag": "input",
                "role": "textbox",
                "type": "text",
                "aria_label": "Subject ID",
                "aria_required": "",
                "data_value": "",
                "text": "",
            }
        ],
    }
    raw.update(overrides)
    return raw


def _snapshot(*questions: dict) -> dict:
    return {"listitem_count": len(questions), "questions": list(questions)}


def test_fixture_uses_accessible_question_structure() -> None:
    html = FIXTURE.read_text(encoding="utf-8")

    assert 'role="listitem"' in html
    assert "Screening Date" in html
    assert 'aria-label="Month"' in html
    assert 'type="submit"' not in html


def test_text_dropdown_choice_required_and_date_metadata() -> None:
    page = FakePage(
        _snapshot(
            _question(aria_required="true"),
            _question(
                visible_title="Comments",
                accessible_label="Comments",
                controls=[
                    {
                        "tag": "textarea",
                        "role": "textbox",
                        "type": "",
                        "aria_label": "Comments",
                        "aria_required": "",
                        "data_value": "",
                        "text": "",
                    }
                ],
            ),
            _question(
                visible_title="Gender",
                accessible_label="Gender",
                controls=[
                    {"tag": "div", "role": "listbox", "type": "", "aria_label": "Gender", "aria_required": "", "data_value": "", "text": ""},
                    {"tag": "div", "role": "option", "type": "", "aria_label": "", "aria_required": "", "data_value": "Male", "text": "Male"},
                    {"tag": "div", "role": "option", "type": "", "aria_label": "", "aria_required": "", "data_value": "Female", "text": "Female"},
                    {"tag": "div", "role": "option", "type": "", "aria_label": "", "aria_required": "", "data_value": "Not Reported", "text": "Not Reported"},
                ],
            ),
            _question(
                visible_title="Informed Consent",
                accessible_label="Informed Consent Required",
                controls=[
                    {"tag": "div", "role": "radio", "type": "", "aria_label": "Yes", "aria_required": "true", "data_value": "Yes", "text": ""},
                    {"tag": "div", "role": "radio", "type": "", "aria_label": "No", "aria_required": "", "data_value": "No", "text": ""},
                ],
            ),
            _question(
                visible_title="Screening Date",
                accessible_label="Screening Date",
                controls=[
                    {"tag": "input", "role": "textbox", "type": "text", "aria_label": "Month", "aria_required": "", "data_value": "", "text": ""},
                    {"tag": "input", "role": "textbox", "type": "text", "aria_label": "Day", "aria_required": "", "data_value": "", "text": ""},
                    {"tag": "input", "role": "spinbutton", "type": "text", "aria_label": "Year", "aria_required": "", "data_value": "", "text": ""},
                ],
            ),
        )
    )
    document = _extract(page)
    kinds = {item.visible_title: item for item in document.questions}

    assert document.status == "captured"
    assert document.schema_version == "1"
    assert document.purpose == "form_structure_only"
    assert document.form_url == FORM_URL
    assert document.page_title == "Clinical Study Data Collection - UAT Demo"
    assert document.captured_at == CAPTURED
    assert kinds["Subject ID"].question_type == "short_text"
    assert kinds["Subject ID"].required is True
    assert kinds["Subject ID"].required_detection == "detected"
    assert kinds["Comments"].question_type == "paragraph"
    assert kinds["Gender"].question_type == "dropdown"
    assert kinds["Gender"].options == ["Male", "Female", "Not Reported"]
    assert kinds["Gender"].options_detection == "detected"
    assert kinds["Informed Consent"].question_type == "multiple_choice"
    assert kinds["Informed Consent"].options == ["Yes", "No"]
    assert kinds["Informed Consent"].required is True
    screening = kinds["Screening Date"]
    assert screening.question_type == "date"
    assert [(item.label, item.role, item.input_type, item.detected) for item in screening.date_controls] == [
        ("Month", "textbox", "text", True),
        ("Day", "textbox", "text", True),
        ("Year", "spinbutton", "text", True),
    ]
    assert "no clinical rule" in document.diagnostics[0]


def test_closed_dropdown_and_checkbox_options_are_distinguished() -> None:
    page = FakePage(
        _snapshot(
            _question(
                visible_title="Gender",
                accessible_label="Gender",
                controls=[
                    {"tag": "div", "role": "listbox", "type": "", "aria_label": "Gender", "aria_required": "", "data_value": "", "text": ""}
                ],
            ),
            _question(
                visible_title="Symptoms",
                accessible_label="Symptoms",
                controls=[
                    {"tag": "div", "role": "checkbox", "type": "", "aria_label": "Headache", "aria_required": "", "data_value": "", "text": ""},
                    {"tag": "div", "role": "checkbox", "type": "", "aria_label": "Fatigue", "aria_required": "", "data_value": "", "text": ""},
                ],
            ),
        )
    )
    document = _extract(page)
    gender, symptoms = document.questions

    assert gender.question_type == "dropdown"
    assert gender.options == []
    assert gender.options_detection == "not_detected"
    assert "without interaction" in " ".join(gender.detection_notes)
    assert symptoms.question_type == "checkbox"
    assert symptoms.options == ["Headache", "Fatigue"]


def test_missing_label_incomplete_date_and_unsupported_control_are_marked() -> None:
    page = FakePage(
        _snapshot(
            _question(visible_title="", accessible_label="", aria_required="", controls=[]),
            _question(
                visible_title="Screening Date",
                accessible_label="Screening Date",
                controls=[
                    {"tag": "input", "role": "textbox", "type": "text", "aria_label": "Month", "aria_required": "", "data_value": "", "text": ""}
                ],
            ),
            _question(
                visible_title="Attachment",
                accessible_label="Attachment",
                controls=[
                    {"tag": "input", "role": "", "type": "file", "aria_label": "", "aria_required": "", "data_value": "", "text": ""}
                ],
            ),
            _question(
                visible_title="Mixed",
                accessible_label="Mixed",
                controls=[
                    {"tag": "div", "role": "radio", "type": "", "aria_label": "Yes", "aria_required": "", "data_value": "", "text": ""},
                    {"tag": "div", "role": "checkbox", "type": "", "aria_label": "Confirmed", "aria_required": "", "data_value": "", "text": ""},
                ],
            ),
        )
    )
    document = _extract(page)
    blank, partial, attachment, mixed = document.questions

    assert blank.visible_title == ""
    assert blank.question_type == "unknown"
    assert blank.type_detection == "ambiguous"
    assert "Question label could not be read." in blank.detection_notes
    assert blank.required is None
    assert blank.required_detection == "not_detected"
    assert partial.question_type == "unknown"
    assert partial.date_controls[0].detected is True
    assert partial.date_controls[2].detected is False
    assert "Date controls were incomplete." in partial.detection_notes
    assert attachment.question_type == "unsupported"
    assert mixed.question_type == "unknown"
    assert mixed.type_detection == "ambiguous"
    assert mixed.options == []


def test_metadata_hash_ignores_capture_time_and_changes_with_structure() -> None:
    snapshot = _snapshot(_question(visible_title="Age *", accessible_label="Age"))
    first = _extract(FakePage(snapshot), CAPTURED)
    second = _extract(
        FakePage(snapshot),
        datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc),
    )
    changed = _extract(
        FakePage(_snapshot(_question(visible_title="Age *", accessible_label="Age Required")))
    )

    assert first.metadata_hash == second.metadata_hash
    assert first.captured_at != second.captured_at
    assert changed.metadata_hash != first.metadata_hash
    assert len(first.metadata_hash) == 64
    assert first.questions[0].visible_title == "Age *"
    assert first.questions[0].required is True


def test_navigation_and_inaccessible_forms_do_not_extract_questions() -> None:
    unused = FakePage()
    rejected = extract_google_form_metadata(
        "https://docs.google.com/forms/d/e/abc/formResponse",
        captured_at=CAPTURED,
        page_opener=_open(unused),
    )
    sign_in_page = FakePage(redirect_url="https://accounts.google.com/signin")
    sign_in = _extract(sign_in_page)
    missing = _extract(FakePage())
    captcha_page = FakePage(body="Please solve the reCAPTCHA challenge")
    captcha = _extract(captcha_page)

    assert rejected.status == "error"
    assert "viewform" in rejected.diagnostics[0]
    assert rejected.questions == []
    assert unused.url == ""
    assert sign_in.status == "blocked"
    assert "sign-in" in sign_in.diagnostics[0]
    assert sign_in.questions == []
    assert sign_in_page.evaluated is False
    assert missing.status == "error"
    assert "Question containers were not found." in missing.diagnostics
    assert captcha.status == "blocked"
    assert captcha.questions == []
    assert captcha_page.evaluated is False


def test_extractor_does_not_fill_or_submit() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert ".click(" not in source
    assert ".fill(" not in source
    assert ".check(" not in source
    assert "form.submit" not in source
    assert 'get_by_role("button"' not in source
    assert "click" not in _EXTRACT_SCRIPT
    assert "submit" not in _EXTRACT_SCRIPT.lower()
