"""Form metadata guides generation. No live model or Google Forms calls."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ai.form_manifest import FORM_STRUCTURE_RULES, FormMetadataError
from ai.supporting_data_generator import attach_supporting_data
from ai.test_case_generator import generate_test_cases
from ai.test_case_prompts import SYSTEM_PROMPT, build_user_prompt
from models.analysis import AnalyzedRequirement, RequirementAnalysis, RequirementParameters
from models.form_metadata import ExtractedFormQuestion, GoogleFormMetadata
from models.test_cases import GeneratedTestCase, GenerationMetadata, TestSuite

FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSe5t_std9cC210P34FUQ88t8w1O7LlHX9qF7lDa_P_WS73YXQ/viewform"
)
CAPTURED = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, payload: dict | str) -> None:
        self.payload = payload if isinstance(payload, str) else json.dumps(payload)
        self.system_prompt = ""
        self.user_prompt = ""
        self.calls = 0

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.payload


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
        "source_file": "study.xlsx",
        "source_sheet": "Validation_Rules",
        "source_row": 6,
        "confidence": 0.95,
        "needs_human_review": False,
        "ambiguity_notes": None,
        "source_values": {"Field": "Age"},
    }
    data.update(overrides)
    return AnalyzedRequirement(**data)


def _analysis(*requirements: AnalyzedRequirement) -> RequirementAnalysis:
    return RequirementAnalysis(
        source_file="study.xlsx",
        requirements=list(requirements),
        uninterpreted_sources=[],
        warnings=[],
    )


def _question(**overrides: object) -> ExtractedFormQuestion:
    data: dict[str, object] = {
        "position": 1,
        "visible_title": "Age",
        "accessible_label": "Age",
        "question_type": "short_text",
        "type_detection": "detected",
        "required": True,
        "required_detection": "detected",
        "options": [],
        "options_detection": "not_applicable",
    }
    data.update(overrides)
    return ExtractedFormQuestion(**data)


def _metadata(*questions: ExtractedFormQuestion, status: str = "captured") -> GoogleFormMetadata:
    return GoogleFormMetadata(
        form_url=FORM_URL,
        page_title="Clinical Study Data Collection - UAT Demo",
        captured_at=CAPTURED,
        status=status,  # type: ignore[arg-type]
        questions=list(questions),
        metadata_hash="a" * 64,
        diagnostics=["Form metadata describes structure only."],
    )


def _case(requirement_id: str = "VAL-002", **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "requirement_id": requirement_id,
        "title": "Synthetic case",
        "description": "Synthetic input for the stated requirement.",
        "category": "positive",
        "input_data": {"Age": 30},
        "expected_result": "Accept",
        "rationale": "The value follows the stated requirement.",
        "confidence": 0.95,
        "needs_human_review": False,
    }
    item.update(overrides)
    return item


def _prompt(client: FakeClient) -> dict:
    return json.loads(client.user_prompt)


def test_stage_one_receives_the_form_manifest_and_keeps_protocol_identity() -> None:
    requirement = _requirement()
    metadata = _metadata(
        _question(visible_title="Age *", accessible_label="Age Required", required=None, required_detection="not_detected")
    )
    client = FakeClient({"test_cases": [_case(input_data={"Age *": 30})]})
    suite = generate_test_cases(_analysis(requirement), client=client, form_metadata=metadata)
    prompt = _prompt(client)
    case = suite.test_cases[0]

    assert client.system_prompt == SYSTEM_PROMPT
    assert prompt["form_questions"][0]["title"] == "Age"
    assert prompt["form_questions"][0]["required"] is None
    assert prompt["form_structure_rules"] == FORM_STRUCTURE_RULES
    assert prompt["requirements"][0]["requirement_id"] == "VAL-002"
    assert prompt["requirements"][0]["source_row"] == 6
    assert prompt["requirements"][0]["form_question_title"] == "Age"
    assert prompt["requirements"][0]["form_required"] is None
    assert "Do not treat null as optional" in " ".join(prompt["form_structure_rules"])
    assert case.input_data == {"Age": 30}
    assert case.requirement_id == "VAL-002"
    assert case.source_sheet == "Validation_Rules"
    assert case.source_row == 6
    assert case.review_status == "pending_cdm_review"
    assert suite.metadata.form_metadata_hash == "a" * 64
    assert any("not treated as optional" in warning for warning in suite.metadata.warnings)


def test_generation_without_metadata_keeps_the_existing_prompt() -> None:
    requirement = _requirement()
    client = FakeClient({"test_cases": [_case()]})
    suite = generate_test_cases(_analysis(requirement), client=client)
    expected = build_user_prompt(
        [
            {
                "requirement_id": "VAL-002",
                "field_name": "Age",
                "rule_type": "range",
                "condition": requirement.condition,
                "expected_behavior": requirement.expected_behavior,
                "parameters": requirement.parameters.model_dump(),
                "needs_human_review": False,
                "ambiguity_notes": None,
                "source_sheet": "Validation_Rules",
                "source_row": 6,
            }
        ]
    )

    assert client.calls == 1
    assert client.system_prompt == SYSTEM_PROMPT
    assert client.user_prompt == expected
    assert "form_questions" not in _prompt(client)
    assert suite.metadata.form_metadata_hash is None
    assert suite.test_cases[0].input_data == {"Age": 30}
    assert suite.test_cases[0].requirement_ids == ["VAL-002"]


def test_boundary_negative_and_cross_field_cases_remain_when_the_form_matches() -> None:
    age = _requirement()
    follow_up = _requirement(
        requirement_id="VAL-005",
        field_name="Follow-up Date",
        rule_type="date",
        condition="Follow-up Date must be on or after Screening Date.",
        expected_behavior="Reject a follow-up before screening.",
        parameters=RequirementParameters(related_field="Screening Date"),
        source_row=9,
        source_values={"Field": "Follow-up Date"},
    )
    metadata = _metadata(
        _question(),
        _question(position=2, visible_title="Screening Date", question_type="date"),
        _question(position=3, visible_title="Follow-up Date", question_type="date", required=False),
    )
    client = FakeClient(
        {
            "test_cases": [
                _case(category="boundary", input_data={"Age": 18}, expected_result="Accept"),
                _case(category="boundary", input_data={"Age": 17}, expected_result="Reject"),
                _case(category="negative", input_data={"Age": 10}, expected_result="Reject"),
                _case(
                    requirement_id="VAL-005",
                    category="cross_field",
                    input_data={"Follow-up Date": "2026-10-08", "Screening Date": "2026-10-01"},
                    expected_result="Accept",
                ),
                _case(
                    requirement_id="VAL-005",
                    category="cross_field",
                    input_data={"Follow-up Date": "2026-09-01", "Screening Date": "2026-10-01"},
                    expected_result="Reject",
                ),
            ]
        }
    )
    suite = generate_test_cases(_analysis(age, follow_up), client=client, form_metadata=metadata)
    by_id = {}
    for case in suite.test_cases:
        by_id.setdefault(case.requirement_id, []).append(case)

    assert [case.input_data["Age"] for case in by_id["VAL-002"]] == [18, 17, 10]
    assert [case.expected_result for case in by_id["VAL-002"]] == ["Accept", "Reject", "Reject"]
    assert {case.expected_result for case in by_id["VAL-005"]} == {"Accept", "Reject"}
    assert by_id["VAL-005"][0].source_row == 9
    assert "Screening Visit" not in _prompt(client)["requirements"][1]["parameters"].values()


def test_unknown_labels_are_not_mapped_and_unrepresentable_fields_are_not_generated() -> None:
    visit = _requirement(
        requirement_id="Study_Protocol:12",
        field_name="Screening Visit",
        rule_type="date",
        condition="A screening date must be entered for each demo subject.",
        expected_behavior="A screening date is entered.",
        parameters=RequirementParameters(),
        source_sheet="Study_Protocol",
        source_row=12,
        source_values={"Item": "Screening Visit"},
    )
    height = _requirement(
        requirement_id="VAL-007",
        field_name="Height",
        rule_type="range",
        condition="Height must be greater than 0 when provided.",
        parameters=RequirementParameters(minimum=0, minimum_inclusive=False),
        source_row=11,
        source_values={"Field": "Height"},
    )
    eligibility = _requirement(
        requirement_id="VAL-009",
        field_name="Eligibility Status",
        rule_type="mandatory",
        condition="Eligibility status is required.",
        parameters=RequirementParameters(),
        source_row=13,
        source_values={"Field": "Eligibility Status"},
    )
    duplicate_age = _requirement(requirement_id="Study_Protocol:9", source_row=9)
    metadata = _metadata(
        _question(position=2, visible_title="Screening Date", question_type="date"),
        _question(position=3, visible_title="Height (cm)"),
        _question(
            position=4,
            visible_title="Eligibility Status",
            question_type="unsupported",
            options_detection="not_applicable",
        ),
        _question(position=5, visible_title="Age"),
        _question(position=6, visible_title="Age"),
    )
    client = FakeClient({"test_cases": [_case()]})
    suite = generate_test_cases(
        _analysis(visit, height, eligibility, duplicate_age),
        client=client,
        form_metadata=metadata,
    )
    text = " ".join(suite.metadata.warnings)

    assert client.calls == 0
    assert suite.test_cases == []
    assert "Screening Visit" in text
    assert "no form question has this label" in text
    assert "Screening Date" not in text
    assert "Height" in text
    assert "Eligibility Status" in text
    assert "cannot be used" in text
    assert "more than one form question" in text
    assert suite.metadata.form_metadata_hash == "a" * 64


def test_placeholder_choices_are_excluded_and_accept_cases_must_use_real_choices() -> None:
    gender = _requirement(
        requirement_id="VAL-006",
        field_name="Gender",
        rule_type="allowed_value",
        condition="Gender must be Male, Female, or Not Reported.",
        expected_behavior="Accept a listed gender.",
        parameters=RequirementParameters(allowed_values=["Male", "Female", "Not Reported"]),
        source_row=10,
        source_values={"Field": "Gender"},
    )
    metadata = _metadata(
        _question(
            visible_title="Gender",
            question_type="dropdown",
            options=["Choose", "Male", "Female", "Not Reported"],
            options_detection="detected",
            required=None,
            required_detection="not_detected",
        )
    )
    client = FakeClient(
        {
            "test_cases": [
                _case(
                    requirement_id="VAL-006",
                    category="allowed_value",
                    input_data={"Gender": "Choose"},
                    expected_result="Reject",
                ),
                _case(
                    requirement_id="VAL-006",
                    category="allowed_value",
                    input_data={"Gender": "Female"},
                    expected_result="Accept",
                ),
                _case(
                    requirement_id="VAL-006",
                    category="negative",
                    input_data={"Gender": "NOT_A_LISTED_VALUE"},
                    expected_result="Reject",
                ),
                _case(
                    requirement_id="VAL-006",
                    category="positive",
                    input_data={"Gender": "Other"},
                    expected_result="Accept",
                ),
            ]
        }
    )
    suite = generate_test_cases(_analysis(gender), client=client, form_metadata=metadata)
    prompt = _prompt(client)
    kept = {(case.input_data["Gender"], case.expected_result) for case in suite.test_cases}

    assert prompt["form_questions"][0]["options"] == ["Male", "Female", "Not Reported"]
    assert prompt["form_questions"][0]["required"] is None
    assert "Choose" not in prompt["form_questions"][0]["options"]
    assert kept == {("Female", "Accept"), ("NOT_A_LISTED_VALUE", "Reject")}
    assert any("placeholder" in warning for warning in suite.metadata.warnings)
    assert any("not a choice" in warning for warning in suite.metadata.warnings)


def test_failed_metadata_is_not_used_for_generation() -> None:
    client = FakeClient({"test_cases": [_case()]})
    with pytest.raises(FormMetadataError, match="not captured"):
        generate_test_cases(
            _analysis(_requirement()),
            client=client,
            form_metadata=_metadata(status="blocked"),
        )
    assert client.calls == 0


def _generated_case() -> GeneratedTestCase:
    return GeneratedTestCase(
        test_case_id="TC-VAL-002-boundary-01",
        requirement_id="VAL-002",
        requirement_ids=["VAL-002"],
        title="Age 18",
        description="Inclusive minimum age.",
        category="boundary",
        input_data={"Age": 18},
        expected_result="Accept",
        expected_behavior="Accept 18-65.",
        source_file="study.xlsx",
        source_sheet="Validation_Rules",
        source_row=6,
        rationale="18 is the stated inclusive minimum.",
        confidence=0.95,
        needs_human_review=False,
    )


def _support_analysis() -> RequirementAnalysis:
    return _analysis(
        _requirement(),
        _requirement(
            requirement_id="VAL-001",
            field_name="Subject ID",
            rule_type="mandatory",
            condition="Subject ID is mandatory.",
            expected_behavior="Reject a blank subject id.",
            parameters=RequirementParameters(),
            source_row=5,
            source_values={"Field": "Subject ID"},
        ),
        _requirement(
            requirement_id="VAL-006",
            field_name="Gender",
            rule_type="allowed_value",
            condition="Gender must be Male, Female, or Not Reported.",
            expected_behavior="Accept a listed gender.",
            parameters=RequirementParameters(allowed_values=["Male", "Female", "Not Reported"]),
            source_row=10,
            source_values={"Field": "Gender"},
        ),
        _requirement(
            requirement_id="VAL-004",
            field_name="Screening Date",
            rule_type="date",
            condition="A screening date is required.",
            expected_behavior="Reject a blank screening date.",
            parameters=RequirementParameters(),
            source_row=8,
            source_values={"Field": "Screening Date"},
        ),
    )


def _support_metadata() -> GoogleFormMetadata:
    return _metadata(
        _question(position=1, visible_title="Subject ID"),
        _question(required=None, required_detection="not_detected"),
        _question(
            position=3,
            visible_title="Gender",
            question_type="dropdown",
            options=["Choose", "Male", "Female", "Not Reported"],
            options_detection="detected",
            required=None,
            required_detection="not_detected",
        ),
        _question(position=4, visible_title="Screening Date", question_type="date"),
        _question(position=5, visible_title="Height (cm)"),
    )


def _suite() -> TestSuite:
    return TestSuite(
        test_cases=[_generated_case()],
        metadata=GenerationMetadata(
            source_file="study.xlsx",
            model=None,
            requirement_count=4,
            test_case_count=1,
            rejected_count=0,
            category_counts={
                "positive": 0,
                "negative": 0,
                "boundary": 1,
                "conditional": 0,
                "cross_field": 0,
                "mandatory": 0,
                "allowed_value": 0,
                "date": 0,
            },
            warnings=[],
        ),
    )


def test_stage_two_receives_relevant_form_metadata_and_skips_placeholders() -> None:
    client = FakeClient(
        {
            "supporting_data": [
                {
                    "test_case_id": "TC-VAL-002-boundary-01",
                    "supporting_values": {
                        "Subject ID": "SYN-001",
                        "Gender": "Female",
                        "Screening Date": "2026-10-01",
                        "Screening Visit": "2026-10-01",
                    },
                    "supporting_requirement_ids": {
                        "Subject ID": ["VAL-001"],
                        "Gender": ["VAL-006"],
                        "Screening Date": ["VAL-004"],
                        "Screening Visit": ["VAL-004"],
                    },
                }
            ]
        }
    )
    completed = attach_supporting_data(
        _suite(),
        _support_analysis(),
        client=client,
        required_fields=["Subject ID", "Gender", "Screening Date", "Screening Visit"],
        form_metadata=_support_metadata(),
    )
    prompt = _prompt(client)
    stored = completed.test_cases[0].input_data
    questions = {item["title"]: item for item in prompt["form_questions"]}

    assert prompt["required_form_fields"] == ["Subject ID", "Gender", "Screening Date"]
    assert set(questions) == {"Subject ID", "Gender", "Screening Date"}
    assert "Height (cm)" not in questions
    assert questions["Gender"]["options"] == ["Male", "Female", "Not Reported"]
    assert questions["Gender"]["required"] is None
    assert "Choose" not in json.dumps(prompt["form_questions"])
    assert "Do not treat null as optional" in " ".join(prompt["form_structure_rules"])
    assert stored["Age"] == 18
    assert stored["Gender"] == "Female"
    assert stored["Subject ID"] == "SYN-001"
    assert stored["Screening Date"] == "2026-10-01"
    assert "Screening Visit" not in stored
    assert any(
        "Screening Visit" in warning and "not mapped to another question" in warning
        for warning in completed.metadata.warnings
    )
    assert not any("Screening Date" in warning and "Screening Visit" in warning for warning in completed.metadata.warnings)


def test_stage_two_rejects_a_placeholder_and_ignores_metadata_when_omitted() -> None:
    placeholder = FakeClient(
        {
            "supporting_data": [
                {
                    "test_case_id": "TC-VAL-002-boundary-01",
                    "supporting_values": {"Gender": "Choose"},
                    "supporting_requirement_ids": {"Gender": ["VAL-006"]},
                }
            ]
        }
    )
    rejected = attach_supporting_data(
        _suite(),
        _support_analysis(),
        client=placeholder,
        required_fields=["Gender"],
        form_metadata=_support_metadata(),
    )
    plain = FakeClient({"supporting_data": []})
    unchanged = attach_supporting_data(
        _suite(),
        _support_analysis(),
        client=plain,
        required_fields=["Subject ID"],
    )

    assert "Gender" not in rejected.test_cases[0].input_data
    assert "placeholder option" in (rejected.test_cases[0].review_notes or "")
    assert "form_questions" not in json.loads(plain.user_prompt)
    assert unchanged.test_cases[0].input_data == {"Age": 18}
