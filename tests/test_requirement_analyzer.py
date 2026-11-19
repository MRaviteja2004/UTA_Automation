"""Unit tests for AI requirement analysis. No live API calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai.client import (
    AIConfigurationError,
    AIProviderError,
    AISettings,
    InvalidAIResponseError,
    OpenAIRequirementClient,
    load_ai_settings,
)
from ai.requirement_analyzer import analyze_requirements
from ingestion.excel_reader import read_workbook
from models.schemas import ProtocolRequirement, ValidationRule

SAMPLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "sample_data"
    / "Sample_Clinical_Study_Protocol_and_Validation_Rules.xlsx"
)


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


def _rule(**overrides: object) -> ValidationRule:
    data: dict[str, object] = {
        "sheet_name": "Validation_Rules",
        "excel_row": 5,
        "rule_id": "VAL-001",
        "form_section": "Subject Details",
        "field": "Subject ID",
        "condition": "Subject ID is mandatory.",
        "expected_edc_behavior": (
            "Do not allow completion of a screening form when blank; "
            "show required-field message."
        ),
        "positive_example": "SUBJ-001",
        "negative_example": "Blank",
    }
    data.update(overrides)
    data["values"] = overrides.get(
        "values",
        {
            "Rule ID": data["rule_id"],
            "Form / Section": data["form_section"],
            "Field": data["field"],
            "Requirement / Condition": data["condition"],
            "Expected EDC Behavior": data["expected_edc_behavior"],
            "Positive Example": data["positive_example"],
            "Negative Example": data["negative_example"],
        },
    )
    return ValidationRule(**data)


def _protocol(**overrides: object) -> ProtocolRequirement:
    data: dict[str, object] = {
        "sheet_name": "Study_Protocol",
        "excel_row": 9,
        "section": "Eligibility",
        "item": "Age",
        "requirement": "Participant age must be from 18 through 65 years inclusive.",
    }
    data.update(overrides)
    data["values"] = overrides.get(
        "values",
        {
            "Section": data["section"],
            "Item": data["item"],
            "Sample protocol requirement": data["requirement"],
        },
    )
    return ProtocolRequirement(**data)


def _item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "source_sheet": "Validation_Rules",
        "source_row": 5,
        "field_name": "Subject ID",
        "rule_type": "mandatory",
        "parameters": {},
        "confidence": 0.95,
        "needs_human_review": False,
        "ambiguity_notes": None,
    }
    item.update(overrides)
    return item


def _analyze(rows: list[ValidationRule] | list[ProtocolRequirement], payload: dict | str, *, protocol: list[ProtocolRequirement] | None = None, rules: list[ValidationRule] | None = None):
    client = FakeClient(payload)
    if protocol is None and rules is None:
        rules = rows  # type: ignore[assignment]
        protocol = []
    result = analyze_requirements(
        protocol or [],
        rules or [],
        source_file="study.xlsx",
        client=client,
    )
    return result, client


def test_mandatory_requirement_keeps_source_text() -> None:
    row = _rule()
    result, _client = _analyze(
        [row],
        {"requirements": [_item()]},
    )

    assert len(result.requirements) == 1
    analyzed = result.requirements[0]
    assert analyzed.requirement_id == "VAL-001"
    assert analyzed.field_name == "Subject ID"
    assert analyzed.rule_type == "mandatory"
    assert analyzed.condition == row.condition
    assert analyzed.expected_behavior == row.expected_edc_behavior
    assert analyzed.parameters.minimum is None
    assert analyzed.source_file == "study.xlsx"
    assert analyzed.source_sheet == "Validation_Rules"
    assert analyzed.source_row == 5
    assert analyzed.confidence == 0.95
    assert analyzed.needs_human_review is False
    assert analyzed.review_status == "pending_cdm_review"
    assert analyzed.source_values["Requirement / Condition"] == row.condition
    assert row.condition == "Subject ID is mandatory."


def test_range_requirement_accepts_limits_written_in_the_source() -> None:
    row = _rule(
        excel_row=6,
        rule_id="VAL-002",
        form_section="Demographics",
        field="Age",
        condition="Age must be an integer from 18 to 65 inclusive.",
        expected_edc_behavior="Accept 18\u201365; validate values below 18 or above 65.",
        positive_example="30",
        negative_example="17 or 66",
    )
    result, client = _analyze(
        [row],
        {
            "requirements": [
                _item(
                    source_row=6,
                    field_name="age",
                    rule_type="range",
                    parameters={
                        "minimum": 18,
                        "maximum": 65,
                        "minimum_inclusive": True,
                        "maximum_inclusive": True,
                    },
                )
            ]
        },
    )

    analyzed = result.requirements[0]
    assert analyzed.requirement_id == "VAL-002"
    assert analyzed.field_name == "Age"
    assert analyzed.rule_type == "range"
    assert analyzed.parameters.minimum == 18
    assert analyzed.parameters.maximum == 65
    assert analyzed.parameters.minimum_inclusive is True
    assert analyzed.condition == "Age must be an integer from 18 to 65 inclusive."
    assert analyzed.expected_behavior == row.expected_edc_behavior
    assert "18\u201365" in analyzed.expected_behavior
    assert analyzed.needs_human_review is False
    assert row.condition in client.user_prompt
    assert row.expected_edc_behavior in client.user_prompt


def test_conditional_requirement_accepts_zero_when_the_source_says_zero() -> None:
    row = _rule(
        excel_row=11,
        rule_id="VAL-007",
        form_section="Vitals",
        field="Height (cm)",
        condition="If provided, height must be a positive number.",
        expected_edc_behavior=(
            "Accept numeric values greater than zero; reject zero, "
            "negative or non-numeric values."
        ),
        positive_example="170",
        negative_example="0 or -5",
    )
    result, _client = _analyze(
        [row],
        {
            "requirements": [
                _item(
                    source_row=11,
                    field_name="Height (cm)",
                    rule_type="conditional",
                    parameters={"minimum": 0, "minimum_inclusive": False},
                )
            ]
        },
    )

    analyzed = result.requirements[0]
    assert analyzed.rule_type == "conditional"
    assert analyzed.parameters.minimum == 0
    assert analyzed.parameters.minimum_inclusive is False
    assert analyzed.condition == row.condition
    assert analyzed.needs_human_review is False


def test_date_and_cross_field_requirements() -> None:
    screening = _rule(
        excel_row=8,
        rule_id="VAL-004",
        form_section="Visit Details",
        field="Screening Date",
        condition="Screening date is required.",
        expected_edc_behavior="Show required-field validation if no date is entered.",
        positive_example="2026-10-01",
        negative_example="Blank",
    )
    follow_up = _rule(
        excel_row=9,
        rule_id="VAL-005",
        form_section="Visit Details",
        field="Follow-up Date",
        condition="Follow-up date must be on or after screening date.",
        expected_edc_behavior="Accept same day or later; flag earlier dates.",
        positive_example="2026-10-08; screening 2026-10-01",
        negative_example="2026-09-30; screening 2026-10-01",
    )
    result, _client = _analyze(
        [screening, follow_up],
        {
            "requirements": [
                _item(
                    source_row=8,
                    field_name="Screening Date",
                    rule_type="date",
                ),
                _item(
                    source_row=9,
                    field_name="Follow-up Date",
                    rule_type="cross-field",
                    parameters={"related_field": "Screening Date"},
                ),
            ]
        },
    )

    by_id = {item.requirement_id: item for item in result.requirements}
    assert by_id["VAL-004"].rule_type == "date"
    assert by_id["VAL-004"].condition == "Screening date is required."
    assert by_id["VAL-004"].parameters.related_field is None
    assert by_id["VAL-005"].rule_type == "cross_field"
    assert by_id["VAL-005"].parameters.related_field == "Screening Date"
    assert by_id["VAL-005"].condition == follow_up.condition
    assert by_id["VAL-005"].source_row == 9


def test_allowed_values_not_in_the_source_are_rejected() -> None:
    row = _rule(
        excel_row=10,
        rule_id="VAL-006",
        form_section="Demographics",
        field="Gender",
        condition="Allowed values: Male, Female, Not Reported.",
        expected_edc_behavior="Accept only configured choices.",
        positive_example="Female",
        negative_example="Unlisted free-text value",
    )
    result, _client = _analyze(
        [row],
        {
            "requirements": [
                _item(
                    source_row=10,
                    field_name="Gender",
                    rule_type="allowed_values",
                    parameters={
                        "allowed_values": ["Male", "Female", "Not Reported", "Unknown"]
                    },
                )
            ]
        },
    )

    analyzed = result.requirements[0]
    assert analyzed.parameters.allowed_values == ["Male", "Female", "Not Reported"]
    assert analyzed.needs_human_review is True
    assert analyzed.ambiguity_notes is not None
    assert "Unknown" in analyzed.ambiguity_notes
    assert analyzed.condition == row.condition
    assert any("not accepted" in warning for warning in result.warnings)


def test_ambiguous_requirement_stays_unapproved() -> None:
    row = _rule(
        excel_row=6,
        rule_id="VAL-002",
        field="Age",
        condition="Age should be appropriate for the study.",
        expected_edc_behavior="Validate age according to the protocol.",
        positive_example="30",
        negative_example="17 or 66",
    )
    result, _client = _analyze(
        [row],
        {
            "requirements": [
                _item(
                    source_row=6,
                    field_name="Age",
                    rule_type="other",
                    confidence=0.42,
                    needs_human_review=True,
                    ambiguity_notes="No numeric limit is stated.",
                )
            ]
        },
    )

    analyzed = result.requirements[0]
    assert analyzed.condition == "Age should be appropriate for the study."
    assert analyzed.confidence == 0.42
    assert analyzed.needs_human_review is True
    assert analyzed.review_status == "pending_cdm_review"
    assert analyzed.ambiguity_notes == "No numeric limit is stated."
    assert analyzed.parameters.minimum is None
    assert analyzed.parameters.maximum is None


def test_limits_missing_from_the_source_are_not_accepted() -> None:
    row = _rule(
        excel_row=6,
        rule_id="VAL-002",
        field="Age",
        condition="Age must be an integer from 18 to 65 inclusive.",
        expected_edc_behavior="Accept 18\u201365; validate values below 18 or above 65.",
        positive_example="30",
        negative_example="17 or 66",
    )
    original_condition = row.condition
    result, _client = _analyze(
        [row],
        {
            "requirements": [
                _item(
                    source_row=6,
                    field_name="Age",
                    rule_type="range",
                    parameters={"minimum": 18, "maximum": 66},
                    confidence=0.99,
                )
            ]
        },
    )

    analyzed = result.requirements[0]
    assert analyzed.parameters.minimum == 18
    assert analyzed.parameters.maximum is None
    assert analyzed.needs_human_review is True
    assert analyzed.ambiguity_notes is not None
    assert "66" in analyzed.ambiguity_notes
    assert analyzed.condition == original_condition
    assert row.condition == original_condition


def test_low_confidence_requires_human_review() -> None:
    row = _rule(
        excel_row=6,
        rule_id="VAL-002",
        field="Age",
        condition="Age must be an integer from 18 to 65 inclusive.",
        expected_edc_behavior="Accept 18 to 65.",
        positive_example="30",
        negative_example="17 or 66",
    )
    result, _client = _analyze(
        [row],
        {
            "requirements": [
                _item(
                    source_row=6,
                    field_name="Age",
                    rule_type="range",
                    parameters={"minimum": 18, "maximum": 65},
                    confidence=0.55,
                    needs_human_review=False,
                )
            ]
        },
    )

    analyzed = result.requirements[0]
    assert analyzed.confidence == 0.55
    assert analyzed.parameters.maximum == 65
    assert analyzed.needs_human_review is True
    assert analyzed.review_status == "pending_cdm_review"


def test_conflicting_ranges_are_flagged_and_both_kept() -> None:
    younger = _rule(
        excel_row=6,
        rule_id="VAL-002",
        field="Age",
        condition="Age must be from 18 to 65 inclusive.",
        expected_edc_behavior="Accept 18 to 65.",
    )
    older = _rule(
        excel_row=7,
        rule_id="VAL-002B",
        field="Age",
        condition="Age must be from 21 to 65 inclusive.",
        expected_edc_behavior="Accept 21 to 65.",
    )
    result, _client = _analyze(
        [younger, older],
        {
            "requirements": [
                _item(
                    source_row=6,
                    field_name="Age",
                    rule_type="range",
                    parameters={"minimum": 18, "maximum": 65},
                ),
                _item(
                    source_row=7,
                    field_name="Age",
                    rule_type="range",
                    parameters={"minimum": 21, "maximum": 65},
                ),
            ]
        },
    )

    by_row = {item.source_row: item for item in result.requirements}
    assert by_row[6].condition == younger.condition
    assert by_row[7].condition == older.condition
    assert by_row[6].parameters.minimum == 18
    assert by_row[7].parameters.minimum == 21
    assert by_row[6].needs_human_review is True
    assert by_row[7].needs_human_review is True
    assert all(item.review_status == "pending_cdm_review" for item in result.requirements)
    assert any("Conflicting interpretations" in warning for warning in result.warnings)


def test_skipped_source_row_is_listed_without_an_invented_rule() -> None:
    kept = _rule()
    skipped = _protocol()
    result, _client = _analyze(
        [],
        {"requirements": [_item()]},
        protocol=[skipped],
        rules=[kept],
    )

    assert len(result.requirements) == 1
    assert result.requirements[0].requirement_id == "VAL-001"
    assert len(result.uninterpreted_sources) == 1
    missing = result.uninterpreted_sources[0]
    assert missing.requirement_id == "Study_Protocol:9"
    assert missing.field_name == "Age"
    assert missing.condition == skipped.requirement
    assert missing.source_sheet == "Study_Protocol"
    assert missing.source_row == 9
    assert missing.needs_human_review is True
    assert missing.review_status == "pending_cdm_review"
    assert "did not return an interpretation" in missing.note


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(InvalidAIResponseError, match="not valid JSON"):
        _analyze([_rule()], "this is not json")


def test_json_fence_is_accepted() -> None:
    payload = json.dumps({"requirements": [_item()]})
    result, _client = _analyze([_rule()], f"```json\n{payload}\n```")
    assert result.requirements[0].requirement_id == "VAL-001"


def test_invalid_rule_type_and_confidence_are_rejected() -> None:
    row = _rule()
    with pytest.raises(InvalidAIResponseError, match="rule_type"):
        _analyze([row], {"requirements": [_item(rule_type="approval")]})

    with pytest.raises(InvalidAIResponseError, match="confidence"):
        _analyze([row], {"requirements": [_item(confidence=1.5)]})

    with pytest.raises(InvalidAIResponseError, match="requirements"):
        _analyze([row], ["not", "an", "object"])


def test_extra_approval_field_is_rejected() -> None:
    item = _item()
    item["approved"] = True
    with pytest.raises(InvalidAIResponseError, match="approved"):
        _analyze([_rule()], {"requirements": [item]})


def test_unknown_source_row_is_rejected() -> None:
    with pytest.raises(InvalidAIResponseError, match="row 99"):
        _analyze([_rule()], {"requirements": [_item(source_row=99)]})


def test_real_workbook_preserves_excel_references() -> None:
    workbook = read_workbook(SAMPLE_PATH)
    age = next(row for row in workbook.validation_rule_records if row.rule_id == "VAL-002")
    original_condition = age.condition
    client = FakeClient(
        {
            "requirements": [
                _item(
                    source_row=age.excel_row,
                    field_name="Age",
                    rule_type="range",
                    parameters={
                        "minimum": 18,
                        "maximum": 65,
                        "minimum_inclusive": True,
                        "maximum_inclusive": True,
                    },
                    confidence=0.93,
                )
            ]
        }
    )
    result = analyze_requirements(
        workbook.protocol_requirements,
        workbook.validation_rule_records,
        source_file=workbook.source_name,
        client=client,
    )

    analyzed = result.requirements[0]
    assert analyzed.requirement_id == "VAL-002"
    assert analyzed.condition == original_condition
    assert analyzed.expected_behavior == age.expected_edc_behavior
    assert analyzed.source_file == SAMPLE_PATH.name
    assert analyzed.source_sheet == "Validation_Rules"
    assert analyzed.source_row == age.excel_row
    assert analyzed.source_values["Field"] == "Age"
    assert age.condition == original_condition
    assert original_condition in client.user_prompt
    interpreted_rows = {
        (item.source_sheet, item.source_row) for item in result.requirements
    }
    assert ("Validation_Rules", age.excel_row) not in {
        (item.source_sheet, item.source_row) for item in result.uninterpreted_sources
    }
    assert len(result.requirements) + len(result.uninterpreted_sources) == (
        len(workbook.protocol_requirements) + len(workbook.validation_rule_records)
    )
    overview = workbook.protocol_requirements[0]
    listed = next(
        item
        for item in result.uninterpreted_sources
        if item.source_sheet == overview.sheet_name and item.source_row == overview.excel_row
    )
    assert listed.condition == overview.requirement
    assert interpreted_rows == {("Validation_Rules", age.excel_row)}


def _clear_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("USSO_TOKEN", "GENAI_BASE_URL", "GENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_settings_do_not_expose_the_usso_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    with pytest.raises(AIConfigurationError, match="USSO_TOKEN") as caught:
        load_ai_settings()
    assert "usso-test-token" not in str(caught.value)

    monkeypatch.setenv("USSO_TOKEN", "usso-test-token")
    monkeypatch.setenv("GENAI_BASE_URL", "https://genai.example.test/v1")
    monkeypatch.setenv("GENAI_MODEL", "test-model")
    settings = load_ai_settings()
    assert settings.model == "test-model"
    assert settings.base_url == "https://genai.example.test/v1"
    assert "usso-test-token" not in repr(settings)
    assert "[redacted]" in repr(settings)


def test_openai_client_redacts_gateway_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> None:
            captured["create"] = kwargs
            raise RuntimeError("rejected usso-test-token")

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured["init"] = kwargs
            self.chat = FakeChat()

    monkeypatch.setattr("ai.client.OpenAI", FakeOpenAI)
    client = OpenAIRequirementClient(
        AISettings(
            usso_token="usso-test-token",
            base_url="https://genai.example.test/v1",
            model="test-model",
        )
    )
    with pytest.raises(AIProviderError, match="redacted") as caught:
        client.complete(system_prompt="system text", user_prompt="user text")

    assert "usso-test-token" not in str(caught.value)
    assert captured["init"] == {
        "api_key": "usso-test-token",
        "base_url": "https://genai.example.test/v1",
    }
    create_kwargs = captured["create"]
    assert isinstance(create_kwargs, dict)
    assert create_kwargs["model"] == "test-model"
    assert create_kwargs["messages"][0]["content"] == "system text"
