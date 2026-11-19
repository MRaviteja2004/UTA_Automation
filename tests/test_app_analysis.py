"""The four-step Streamlit workflow. Model, form, and browser calls are replaced."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.proto.WidgetStates_pb2 import WidgetState
from streamlit.testing.v1 import AppTest

from models.analysis import AnalyzedRequirement, RequirementAnalysis, RequirementParameters
from models.approval import ApprovedCase
from models.execution import ExecutionResult
from models.form_metadata import ExtractedFormQuestion, GoogleFormMetadata
from models.test_cases import GeneratedTestCase, GenerationMetadata, TestSuite
from ui.workflow import SECTION_SAVE_COMMENT

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = ROOT / "sample_data" / "Sample_Clinical_Study_Protocol_and_Validation_Rules.xlsx"
HASH = "d" * 64
WHEN = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)


def _requirement(**overrides: object) -> AnalyzedRequirement:
    data: dict[str, object] = {
        "requirement_id": "VAL-002",
        "field_name": "Age",
        "rule_type": "range",
        "condition": "Age must be an integer from 18 to 65 inclusive.",
        "expected_behavior": "Accept 18-65.",
        "parameters": RequirementParameters(
            minimum=18,
            maximum=65,
            minimum_inclusive=True,
            maximum_inclusive=True,
        ),
        "source_file": SAMPLE_PATH.name,
        "source_sheet": "Validation_Rules",
        "source_row": 6,
        "confidence": 0.95,
        "needs_human_review": False,
        "source_values": {"Field": "Age"},
    }
    data.update(overrides)
    return AnalyzedRequirement(**data)


def _analysis() -> RequirementAnalysis:
    return RequirementAnalysis(
        source_file=SAMPLE_PATH.name,
        requirements=[
            _requirement(),
            _requirement(
                requirement_id="VAL-006",
                field_name="Gender",
                rule_type="allowed_values",
                condition="Gender must be one of the listed values.",
                expected_behavior="Accept a listed gender.",
                parameters=RequirementParameters(allowed_values=["Male", "Female", "Not Reported"]),
                source_row=10,
                source_values={"Field": "Gender"},
            ),
            _requirement(
                requirement_id="Study_Protocol:12",
                field_name="Screening Visit",
                rule_type="date",
                condition="A screening date must be entered for each demo subject.",
                expected_behavior="A screening date is entered.",
                parameters=RequirementParameters(),
                source_sheet="Study_Protocol",
                source_row=12,
                source_values={"Item": "Screening Visit"},
            ),
        ],
        uninterpreted_sources=[],
        warnings=[],
    )


def _question(position: int, title: str, kind: str = "short_text", **overrides: object) -> ExtractedFormQuestion:
    data: dict[str, object] = {
        "position": position,
        "visible_title": title,
        "accessible_label": title,
        "question_type": kind,
        "type_detection": "detected",
        "required": True,
        "required_detection": "detected",
        "options": [],
        "options_detection": "not_applicable",
    }
    data.update(overrides)
    return ExtractedFormQuestion(**data)


def _metadata(status: str = "captured") -> GoogleFormMetadata:
    questions = [
        _question(1, "Subject ID *"),
        _question(2, "Age *"),
        _question(
            3,
            "Gender *",
            "dropdown",
            options=["Choose", "Male", "Female", "Not Reported"],
            options_detection="detected",
        ),
        _question(
            4,
            "Informed Consent *",
            "multiple_choice",
            options=["Yes", "No"],
            options_detection="detected",
        ),
        _question(5, "Screening Date *", "date"),
    ]
    return GoogleFormMetadata(
        form_url="https://docs.google.com/forms/d/e/example/viewform",
        page_title="Clinical Study Data Collection - UAT Demo",
        captured_at=WHEN,
        status=status,  # type: ignore[arg-type]
        questions=questions if status == "captured" else [],
        metadata_hash=HASH,
        diagnostics=["diagnostic: Google sign-in is required."] if status != "captured" else [],
    )


def _suite() -> TestSuite:
    inputs = {
        "Age": 18,
        "Subject ID": "SYN-001",
        "Gender": "Female",
        "Informed Consent": "Yes",
        "Screening Date": "2026-10-01",
    }
    return TestSuite(
        test_cases=[
            GeneratedTestCase(
                test_case_id="TC-VAL-002-boundary-01",
                requirement_id="VAL-002",
                requirement_ids=["VAL-002"],
                title="Age at inclusive minimum",
                description="Inclusive minimum age.",
                category="boundary",
                input_data=inputs,
                test_data_classification={
                    key: "under_test" if key == "Age" else "supporting" for key in inputs
                },
                supporting_requirement_ids={
                    key: ["VAL-001"] for key in inputs if key != "Age"
                },
                expected_result="Accept",
                expected_behavior="Accept 18-65.",
                source_file=SAMPLE_PATH.name,
                source_sheet="Validation_Rules",
                source_row=6,
                rationale="18 is the stated inclusive minimum.",
                confidence=0.95,
                needs_human_review=False,
            ),
            GeneratedTestCase(
                test_case_id="TC-VAL-006-positive-01",
                requirement_id="VAL-006",
                requirement_ids=["VAL-006"],
                title="Gender Female",
                description="Listed gender.",
                category="positive",
                input_data=inputs,
                test_data_classification={
                    key: "under_test" if key == "Gender" else "supporting" for key in inputs
                },
                supporting_requirement_ids={
                    key: ["VAL-001"] for key in inputs if key != "Gender"
                },
                expected_result="Accept",
                expected_behavior="Accept a listed gender.",
                source_file=SAMPLE_PATH.name,
                source_sheet="Validation_Rules",
                source_row=10,
                rationale="Female is one of the listed values.",
                confidence=0.95,
                needs_human_review=True,
            ),
        ],
        metadata=GenerationMetadata(
            source_file=SAMPLE_PATH.name,
            model=None,
            requirement_count=3,
            test_case_count=2,
            rejected_count=0,
            category_counts={"boundary": 1, "positive": 1},
            warnings=["Requirement Study_Protocol:12 field 'Screening Visit' cannot be represented."],
            form_metadata_hash=HASH,
        ),
    )


VALID_FORM_URL = "https://docs.google.com/forms/d/e/abc123/viewform"
DEMO_FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSe5t_std9cC210P34FUQ88t8w1O7LlHX9qF7lDa_P_WS73YXQ/viewform"
)


class Calls:
    def __init__(self) -> None:
        self.analyze = 0
        self.extract = 0
        self.generate = 0
        self.attach = 0
        self.executed: list[ApprovedCase] = []
        self.form_urls: list[object] = []
        self.executed_urls: list[object] = []


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    record = Calls()
    form_status = {"value": "captured"}

    def analyze(*args: object, **kwargs: object) -> RequirementAnalysis:
        record.analyze += 1
        return _analysis()

    def extract(*args: object, **kwargs: object) -> GoogleFormMetadata:
        record.extract += 1
        record.form_urls.append(args[0] if args else kwargs.get("url"))
        return _metadata(form_status["value"])

    def generate(analysis: RequirementAnalysis, **kwargs: object) -> TestSuite:
        record.generate += 1
        assert kwargs["form_metadata"].metadata_hash == HASH
        return _suite()

    def attach(suite: TestSuite, analysis: RequirementAnalysis, **kwargs: object) -> TestSuite:
        record.attach += 1
        assert kwargs["form_metadata"].metadata_hash == HASH
        return suite

    def execute(case: object, mapping_result: object, **kwargs: object) -> ExecutionResult:
        assert isinstance(case, ApprovedCase)
        record.executed.append(case)
        target = getattr(kwargs.get("config"), "google_form", None)
        record.executed_urls.append(getattr(target, "url", None))
        return ExecutionResult(
            test_case_id=case.test_case.test_case_id,
            execution_status="submitted",
            execution_target="google_form",
            entered_values={"Age": 18},
            observed_values={"Age": "18"},
            expected_behavior=case.execution_expected_behavior,
            started_at=WHEN,
            completed_at=WHEN,
        )

    monkeypatch.setattr("ai.requirement_analyzer.analyze_requirements", analyze)
    monkeypatch.setattr("execution.google_forms_metadata.extract_google_form_metadata", extract)
    monkeypatch.setattr("ai.test_case_generator.generate_test_cases", generate)
    monkeypatch.setattr("ai.supporting_data_generator.attach_supporting_data", attach)
    monkeypatch.setattr("execution.google_forms_runner.execute_google_form_fill_only", execute)
    record.form_status = form_status  # type: ignore[attr-defined]
    return record


def _uploaded_app() -> AppTest:
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30)
    app.run()
    app.file_uploader[0].upload(
        SAMPLE_PATH.name,
        SAMPLE_PATH.read_bytes(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    app.run()
    return app


def _paste_form_url(app: AppTest, url: str = VALID_FORM_URL) -> AppTest:
    app.text_input[0].set_value(url)
    app.run()
    return app


def _submit_workbook(app: AppTest, url: str = VALID_FORM_URL) -> AppTest:
    _paste_form_url(app, url)
    _button(app, "Submit").click()
    app.run()
    return app


def _button(app: AppTest, label: str):
    return next(item for item in app.button if item.label == label)


def _case_tables(app: AppTest):
    return [item for item in app.dataframe if "Actions" in item.value.columns]


def _click_delete(app: AppTest, case_name: str) -> None:
    table = next(item for item in _case_tables(app) if case_name in set(item.value["Test case"]))
    row_index = int(table.value.index[table.value["Test case"] == case_name][0])
    states = app._tree.get_widget_states()
    click = WidgetState()
    click.id = table.proto.button_click_widgets["Actions"]
    click.string_trigger_value.data = json.dumps(
        {"row": row_index, "label": ":material/delete:"}
    )
    states.widgets.append(click)
    app._run(states)


def _texts(app: AppTest) -> str:
    parts = [item.value for item in app.markdown]
    parts += [item.value for item in app.caption]
    parts += [item.value for item in app.info]
    parts += [item.value for item in app.warning]
    parts += [item.value for item in app.error]
    parts += [item.value for item in app.success]
    return "\n".join(str(item) for item in parts)


def test_upload_waits_for_submit(calls: Calls) -> None:
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30)
    app.run()
    assert _button(app, "Submit").disabled is True

    app = _uploaded_app()

    assert not app.exception
    assert f"Selected file: {SAMPLE_PATH.name}" in _texts(app)
    assert app.text_input[0].label == "Google Form URL"
    assert app.text_input[0].placeholder == "Paste your Google Form URL here"
    assert app.text_input[0].value in ("", None)
    assert app.text_input[0].value != DEMO_FORM_URL
    assert _button(app, "Submit").disabled is False
    assert calls.analyze == 0
    assert calls.extract == 0
    assert not any(item.label == "Create Test Cases" for item in app.button)


def test_submit_analyzes_reads_the_form_and_shows_requirements(calls: Calls) -> None:
    app = _submit_workbook(_uploaded_app())

    assert not app.exception
    assert calls.analyze == 1
    assert calls.extract == 1
    assert any(item.value == "2. Review requirements" for item in app.header)
    assert _button(app, "Create Test Cases").disabled is False
    text = _texts(app)
    assert "Screening Visit (Study_Protocol row 12): no form question has this label" in text
    assert HASH not in text
    assert len(app.json) == 0
    assert not any("Capture form metadata" == item.label for item in app.button)
    assert not any(item.label == "Save CDM decision" for item in app.button)


def test_empty_form_url_is_rejected_before_the_workbook_is_read(calls: Calls) -> None:
    app = _uploaded_app()
    _button(app, "Submit").click()
    app.run()

    assert not app.exception
    assert any(item.value == "Enter a Google Form URL." for item in app.error)
    assert calls.analyze == 0
    assert calls.extract == 0
    assert not any(item.value == "2. Review requirements" for item in app.header)
    assert "viewform URL" not in _texts(app)
    assert DEMO_FORM_URL not in _texts(app)


def test_invalid_form_url_is_rejected_before_the_workbook_is_read(calls: Calls) -> None:
    app = _submit_workbook(_uploaded_app(), "https://example.com/not-a-form")

    assert not app.exception
    assert any(
        item.value == "Enter a Google Forms link that ends with /viewform."
        for item in app.error
    )
    assert calls.analyze == 0
    assert calls.extract == 0
    assert not any(item.value == "2. Review requirements" for item in app.header)
    assert "Navigation is restricted" not in _texts(app)
    assert not any(item.label == "Technical details" for item in app.expander)


def test_valid_form_url_is_kept_and_used_for_the_form_and_execution(calls: Calls) -> None:
    app = _submit_workbook(_uploaded_app())

    assert not app.exception
    assert calls.form_urls == [VALID_FORM_URL]
    assert app.text_input[0].value == VALID_FORM_URL
    assert app.session_state["wf_form_url"] == VALID_FORM_URL
    assert any(item.value == "2. Review requirements" for item in app.header)

    _button(app, "Create Test Cases").click()
    app.run()
    _button(app, "Save Age Test Cases").click()
    app.run()
    _button(app, "Execute Test Cases").click()
    app.run()

    assert calls.form_urls == [VALID_FORM_URL, VALID_FORM_URL]
    assert calls.executed_urls == [VALID_FORM_URL]
    assert DEMO_FORM_URL not in calls.executed_urls
    assert app.text_input[0].value == VALID_FORM_URL


def test_form_read_failure_blocks_test_creation(calls: Calls) -> None:
    calls.form_status["value"] = "blocked"  # type: ignore[attr-defined]
    app = _submit_workbook(_uploaded_app())

    assert not app.exception
    assert any("The Google Form could not be read." in item.value for item in app.error)
    assert _button(app, "Create Test Cases").disabled is True
    assert any(item.label == "Read the form again" for item in app.button)
    assert not any("sign-in" in item.value for item in app.error)
    assert any(item.label == "Technical details" for item in app.expander)
    assert calls.generate == 0


def test_create_save_and_execute_by_form_question(calls: Calls) -> None:
    app = _submit_workbook(_uploaded_app())
    _button(app, "Create Test Cases").click()
    app.run()

    assert not app.exception
    assert calls.generate == 1
    assert calls.attach == 1
    assert any(item.value == "3. Test cases by form question" for item in app.header)
    labels = [item.label for item in app.expander]
    assert "Age (1)" in labels
    assert "Gender (1)" in labels
    assert not any(label.startswith("Subject ID") for label in labels)
    assert "Short answer" in _texts(app)
    assert not any(item.value == "4. Execute" for item in app.header)
    button_labels = {item.label for item in app.button}
    assert "Save Age Test Cases" in button_labels
    assert "Save Gender Test Cases" in button_labels
    assert not any(label.startswith("Delete ") for label in button_labels)
    assert "Save test suite" not in button_labels
    tables = _case_tables(app)
    assert {tuple(table.value["Test case"]) for table in tables} == {
        ("Age at inclusive minimum",),
        ("Gender Female",),
    }
    assert all(set(table.value["Actions"]) == {":material/delete:"} for table in tables)
    age_table = next(table for table in tables if table.value.iloc[0]["Test case"] == "Age at inclusive minimum")
    age_columns = json.loads(age_table.proto.columns)
    assert age_columns["Actions"]["help"] == "Delete test case"
    assert age_columns["Actions"]["alignment"] == "right"
    assert age_columns["Actions"]["type_config"]["button_type"] == "tertiary"
    assert not any(item.label == "Saved by" for item in app.text_input)

    _button(app, "Save Age Test Cases").click()
    app.run()

    assert not app.exception
    assert any("Age test cases saved." in item.value for item in app.success)
    assert not any("Gender test cases saved." in item.value for item in app.success)
    assert {button.label for button in app.download_button} == {"Download CSV", "Download Excel"}
    assert any(item.value == "4. Execute" for item in app.header)
    assert "does not mean the clinical validation passed" in _texts(app)
    approvals = app.session_state["wf_approvals"]
    assert set(approvals) == {"TC-VAL-002-boundary-01"}
    assert approvals["TC-VAL-002-boundary-01"].reviewer_name == "Test suite owner"
    assert approvals["TC-VAL-002-boundary-01"].comment == ""
    assert app.session_state["wf_suite"].test_cases[0].review_status == "pending_cdm_review"

    _button(app, "Execute Test Cases").click()
    app.run()

    assert not app.exception
    assert len(calls.executed) == 1
    assert calls.executed[0].test_case.test_case_id == "TC-VAL-002-boundary-01"
    assert calls.executed_urls == [VALID_FORM_URL]
    assert calls.executed[0].execution_expected_behavior == "Expected result: Accept. Accept 18-65."
    frame = next(item.value for item in app.dataframe if "Status" in item.value.columns)
    assert frame.to_dict(orient="records") == [
        {
            "Question": "Age",
            "Test case": "Age at inclusive minimum",
            "Status": "submitted",
            "Details": "The response was submitted. This does not mean the clinical validation passed.",
        }
    ]
    assert len(app.json) == 0


def test_execution_is_blocked_when_the_form_cannot_be_read(calls: Calls) -> None:
    app = _submit_workbook(_uploaded_app())
    _button(app, "Create Test Cases").click()
    app.run()
    _button(app, "Save Age Test Cases").click()
    app.run()
    calls.form_status["value"] = "error"  # type: ignore[attr-defined]
    _button(app, "Execute Test Cases").click()
    app.run()

    assert not app.exception
    assert calls.executed == []
    frame = next(item.value for item in app.dataframe if "Status" in item.value.columns)
    assert frame.iloc[0]["Status"] == "blocked"
    assert frame.iloc[0]["Details"] == "The Google Form could not be read, so no test case was run."
    assert list(frame["Test case"]) == ["Age at inclusive minimum"]


def test_cases_missing_required_answers_are_flagged_and_can_be_completed(
    calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    attach_calls: list[list[str]] = []

    def attach(suite: TestSuite, analysis: RequirementAnalysis, **kwargs: object) -> TestSuite:
        attach_calls.append([case.test_case_id for case in suite.test_cases])
        if len(attach_calls) == 1:
            stripped = [
                case.model_copy(
                    update={
                        "input_data": {"Age": 18},
                        "test_data_classification": {"Age": "under_test"},
                        "supporting_requirement_ids": {},
                    }
                )
                if case.test_case_id == "TC-VAL-002-boundary-01"
                else case
                for case in suite.test_cases
            ]
            return suite.model_copy(update={"test_cases": stripped})
        return _suite().model_copy(
            update={"test_cases": [case for case in _suite().test_cases if case.test_case_id in attach_calls[-1]]}
        )

    monkeypatch.setattr("ai.supporting_data_generator.attach_supporting_data", attach)
    app = _submit_workbook(_uploaded_app())
    _button(app, "Create Test Cases").click()
    app.run()

    assert not app.exception
    assert any("1 test case(s) do not have answers for every required form question" in item.value for item in app.warning)

    _button(app, "Save Age Test Cases").click()
    app.run()
    _button(app, "Execute Test Cases").click()
    app.run()
    frame = next(item.value for item in app.dataframe if "Status" in item.value.columns)
    assert list(frame["Status"]) == ["blocked"]
    assert "Required form question 'Subject ID' cannot receive a value." in frame.iloc[0]["Details"]
    assert calls.executed == []

    _button(app, "Fill in missing answers").click()
    app.run()

    assert not app.exception
    assert attach_calls[-1] == ["TC-VAL-002-boundary-01"]
    assert not any("do not have answers" in item.value for item in app.warning)
    assert "TC-VAL-002-boundary-01" not in app.session_state["wf_approvals"]
    case = next(case for case in app.session_state["wf_suite"].test_cases if case.test_case_id == "TC-VAL-002-boundary-01")
    assert set(case.input_data) == {"Age", "Subject ID", "Gender", "Informed Consent", "Screening Date"}

    _button(app, "Save Age Test Cases").click()
    app.run()
    _button(app, "Execute Test Cases").click()
    app.run()
    assert [item.test_case.test_case_id for item in calls.executed] == ["TC-VAL-002-boundary-01"]


def test_delete_and_section_save_are_independent(calls: Calls) -> None:
    app = _submit_workbook(_uploaded_app())
    _button(app, "Create Test Cases").click()
    app.run()
    _click_delete(app, "Age at inclusive minimum")

    assert not app.exception
    labels = [item.label for item in app.expander]
    assert "Age (0)" in labels
    assert "Gender (1)" in labels
    assert [case.test_case_id for case in app.session_state["wf_suite"].test_cases] == [
        "TC-VAL-006-positive-01"
    ]
    assert not any(label.startswith("Delete ") for label in {item.label for item in app.button})
    remaining = _case_tables(app)
    assert [tuple(table.value["Test case"]) for table in remaining if len(table.value)] == [
        ("Gender Female",)
    ]
    assert not any(item.value == "4. Execute" for item in app.header)

    _button(app, "Save Gender Test Cases").click()
    app.run()

    assert not app.exception
    assert any("Gender test cases saved." in item.value for item in app.success)
    approvals = app.session_state["wf_approvals"]
    assert set(approvals) == {"TC-VAL-006-positive-01"}
    assert approvals["TC-VAL-006-positive-01"].comment == SECTION_SAVE_COMMENT
    assert not any(item.label == "Saved by" for item in app.text_input)

    _button(app, "Execute Test Cases").click()
    app.run()

    assert [case.test_case.test_case_id for case in calls.executed] == ["TC-VAL-006-positive-01"]
    frame = next(item.value for item in app.dataframe if "Status" in item.value.columns)
    assert list(frame["Test case"]) == ["Gender Female"]
    assert list(frame["Status"]) == ["submitted"]
