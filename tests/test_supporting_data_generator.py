"""Stage 2 supporting values. Stage 1 stays a separate call. No live AI."""

from __future__ import annotations

import json

from ai.client import AIProviderError
from ai.supporting_data_generator import (
    DEFAULT_BATCH_SIZE,
    attach_supporting_data,
    estimate_request_tokens,
)
from ai.supporting_data_prompts import SYSTEM_PROMPT
from ai.test_case_generator import generate_test_cases
from execution.approval_gate import approve_test_case
from execution.field_mapping import load_form_mapping, map_test_case_inputs
from models.analysis import AnalyzedRequirement, RequirementAnalysis, RequirementParameters
from models.approval import ApprovedCase
from models.test_cases import GeneratedTestCase, GenerationMetadata, TestSuite

REQUIRED = ["Subject ID", "Informed Consent", "Screening Date"]
FORM_FIELDS = ["Subject ID", "Age", "Gender", "Informed Consent", "Screening Date"]


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
        "source_values": {"Field": "Age"},
    }
    data.update(overrides)
    return AnalyzedRequirement(**data)


def _subject() -> AnalyzedRequirement:
    return _requirement(
        requirement_id="VAL-001",
        field_name="Subject ID",
        rule_type="mandatory",
        condition="Subject ID is mandatory.",
        expected_behavior="Do not allow completion when blank.",
        parameters=RequirementParameters(),
        source_row=5,
        source_values={"Positive Example": "SUBJ-001", "Negative Example": "Blank"},
    )


def _consent(**overrides: object) -> AnalyzedRequirement:
    data: dict[str, object] = {
        "requirement_id": "VAL-003",
        "field_name": "Informed Consent",
        "rule_type": "mandatory",
        "condition": "Consent must be Yes before screening can proceed.",
        "expected_behavior": "If No or blank, prevent progression.",
        "parameters": RequirementParameters(),
        "source_row": 7,
        "source_values": {"Positive Example": "Yes", "Negative Example": "No or blank"},
    }
    data.update(overrides)
    return _requirement(**data)


def _screening() -> AnalyzedRequirement:
    return _requirement(
        requirement_id="VAL-004",
        field_name="Screening Date",
        rule_type="mandatory",
        condition="Screening date is required.",
        expected_behavior="Show a required-field message when blank.",
        parameters=RequirementParameters(),
        source_row=8,
        source_values={"Positive Example": "2026-10-01", "Negative Example": "Blank"},
    )


def _gender(**overrides: object) -> AnalyzedRequirement:
    data: dict[str, object] = {
        "requirement_id": "VAL-006",
        "field_name": "Gender",
        "rule_type": "allowed_values",
        "condition": "Allowed values: Male, Female, Not Reported.",
        "expected_behavior": "Accept only configured choices.",
        "parameters": RequirementParameters(
            allowed_values=["Male", "Female", "Not Reported"],
        ),
        "source_row": 10,
        "source_values": {"Positive Example": "Female"},
    }
    data.update(overrides)
    return _requirement(**data)


def _follow_up() -> AnalyzedRequirement:
    return _requirement(
        requirement_id="VAL-005",
        field_name="Follow-up Date",
        rule_type="cross_field",
        condition="Follow-up date must be on or after screening date.",
        expected_behavior="Accept the same day or later.",
        parameters=RequirementParameters(related_field="Screening Date"),
        source_row=9,
        source_values={"Field": "Follow-up Date"},
    )


def _analysis(*extra: AnalyzedRequirement) -> RequirementAnalysis:
    return RequirementAnalysis(
        source_file="study.xlsx",
        requirements=[_subject(), _requirement(), _consent(), _screening(), _follow_up(), *extra],
        uninterpreted_sources=[],
        warnings=[],
    )


def _case(**overrides: object) -> GeneratedTestCase:
    data: dict[str, object] = {
        "test_case_id": "TC-VAL-002-boundary-01",
        "requirement_id": "VAL-002",
        "requirement_ids": ["VAL-002"],
        "title": "Age 18",
        "description": "Inclusive minimum age.",
        "category": "boundary",
        "input_data": {"Age": 18},
        "expected_result": "Accept",
        "expected_behavior": "Accept 18-65.",
        "source_file": "study.xlsx",
        "source_sheet": "Validation_Rules",
        "source_row": 6,
        "rationale": "18 is the stated inclusive minimum.",
        "confidence": 0.95,
        "needs_human_review": False,
    }
    data.update(overrides)
    return GeneratedTestCase(**data)


def _suite(*cases: GeneratedTestCase) -> TestSuite:
    return TestSuite(
        test_cases=list(cases),
        metadata=GenerationMetadata(
            source_file="study.xlsx",
            model=None,
            requirement_count=5,
            test_case_count=len(cases),
            rejected_count=0,
            category_counts={"boundary": len(cases)},
            warnings=[],
        ),
    )


def _proposal(
    case_id: str = "TC-VAL-002-boundary-01",
    **overrides: object,
) -> dict[str, object]:
    item: dict[str, object] = {
        "test_case_id": case_id,
        "supporting_values": {
            "Subject ID": "SYN-001",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-20",
        },
        "supporting_requirement_ids": {
            "Subject ID": ["VAL-001"],
            "Informed Consent": ["VAL-003"],
            "Screening Date": ["VAL-004"],
        },
    }
    item.update(overrides)
    return {"supporting_data": [item]}


def _attach(
    suite: TestSuite,
    payload: dict | str,
    analysis: RequirementAnalysis | None = None,
    required_fields: list[str] | None = REQUIRED,
) -> tuple[TestSuite, FakeClient]:
    client = FakeClient(payload)
    completed = attach_supporting_data(
        suite,
        analysis or _analysis(),
        client=client,
        required_fields=required_fields,
    )
    return completed, client


def test_age_boundary_keeps_its_value_and_gains_justified_supporting_data() -> None:
    original = _case()
    before = original.model_dump()
    completed, client = _attach(_suite(original), _proposal())
    case = completed.test_cases[0]

    assert client.calls == 1
    assert "Do not assume informed consent is Yes" in client.system_prompt
    assert '"Age": 18' in client.user_prompt
    assert original.model_dump() == before
    assert case.input_data["Age"] == 18
    assert case.input_data["Subject ID"] == "SYN-001"
    assert case.input_data["Informed Consent"] == "Yes"
    assert case.input_data["Screening Date"] == "2026-10-20"
    assert case.test_data_classification == {
        "Age": "under_test",
        "Subject ID": "supporting",
        "Informed Consent": "supporting",
        "Screening Date": "supporting",
    }
    assert case.supporting_requirement_ids["Informed Consent"] == ["VAL-003"]
    assert case.requirement_id == "VAL-002"
    assert case.requirement_ids == ["VAL-002"]
    assert case.source_sheet == "Validation_Rules"
    assert case.source_row == 6
    assert case.expected_behavior == "Accept 18-65."
    assert case.review_status == "pending_cdm_review"
    assert case.needs_human_review is False
    assert case.review_notes is None


def test_stage_one_case_is_not_changed_before_supporting_data_is_added() -> None:
    class StageOne:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            return json.dumps(
                {
                    "test_cases": [
                        {
                            "requirement_id": "VAL-002",
                            "title": "Age 18",
                            "description": "Inclusive minimum age.",
                            "category": "boundary",
                            "input_data": {"Age": 18},
                            "expected_result": "Accept",
                            "rationale": "18 is the stated inclusive minimum.",
                            "confidence": 0.95,
                            "needs_human_review": False,
                        }
                    ]
                }
            )

    stage_one = StageOne()
    generated = generate_test_cases(_analysis(_requirement()), client=stage_one)
    before = generated.test_cases[0].model_dump()
    completed, stage_two = _attach(generated, _proposal())

    assert stage_one.calls == 1
    assert stage_two.calls == 1
    assert generated.test_cases[0].model_dump() == before
    assert generated.test_cases[0].input_data == {"Age": 18}
    assert completed.test_cases[0].input_data["Age"] == 18
    assert completed.test_cases[0].test_data_classification["Age"] == "under_test"


def test_original_input_is_not_overwritten() -> None:
    completed, _client = _attach(
        _suite(_case()),
        _proposal(
            supporting_values={
                "Age": 65,
                "Subject ID": "SYN-001",
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
            },
            supporting_requirement_ids={
                "Age": ["VAL-002"],
                "Subject ID": ["VAL-001"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        ),
    )
    case = completed.test_cases[0]

    assert case.input_data["Age"] == 18
    assert case.test_data_classification["Age"] == "under_test"
    assert case.needs_human_review is True
    assert "already contains that field" in (case.review_notes or "")
    assert case.input_data["Subject ID"] == "SYN-001"


def test_intentional_none_is_not_replaced() -> None:
    attempted, _client = _attach(
        _suite(_case(input_data={"Age": 18, "Subject ID": None})),
        _proposal(),
    )
    respected, _other = _attach(
        _suite(_case(input_data={"Age": 18, "Subject ID": None})),
        _proposal(
            supporting_values={
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
            },
            supporting_requirement_ids={
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        ),
    )

    assert attempted.test_cases[0].input_data["Subject ID"] is None
    assert attempted.test_cases[0].test_data_classification["Subject ID"] == "under_test"
    assert "already contains that field" in (attempted.test_cases[0].review_notes or "")
    assert respected.test_cases[0].input_data["Subject ID"] is None
    assert respected.test_cases[0].needs_human_review is False
    assert respected.test_cases[0].input_data["Informed Consent"] == "Yes"


def test_non_required_supporting_field_is_flagged_and_not_stored() -> None:
    completed, _client = _attach(
        _suite(_case()),
        _proposal(
            supporting_values={
                "Subject ID": "SYN-001",
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
                "Gender": "Female",
            },
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
                "Gender": ["VAL-006"],
            },
        ),
    )
    case = completed.test_cases[0]

    assert "Gender" not in case.input_data
    assert case.needs_human_review is True
    assert "not a required form field" in (case.review_notes or "")


def test_missing_or_unknown_citation_blocks_the_supporting_value() -> None:
    missing, _client = _attach(
        _suite(_case()),
        _proposal(
            supporting_values={"Subject ID": "SYN-001"},
            supporting_requirement_ids={},
        ),
    )
    unknown, _other = _attach(
        _suite(_case()),
        _proposal(
            supporting_values={"Subject ID": "SYN-001"},
            supporting_requirement_ids={"Subject ID": ["VAL-999"]},
        ),
    )

    assert "Subject ID" not in missing.test_cases[0].input_data
    assert "no requirement citation" in (missing.test_cases[0].review_notes or "")
    assert "unknown requirement id" in (unknown.test_cases[0].review_notes or "")
    assert "Subject ID" not in unknown.test_cases[0].input_data


def test_consent_yes_requires_a_requirement_that_permits_yes() -> None:
    without_rule, _client = _attach(
        _suite(_case()),
        _proposal(
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Informed Consent": ["VAL-002"],
                "Screening Date": ["VAL-004"],
            }
        ),
        analysis=RequirementAnalysis(
            source_file="study.xlsx",
            requirements=[_subject(), _requirement(), _screening()],
            uninterpreted_sources=[],
            warnings=[],
        ),
    )
    no_value, _other = _attach(
        _suite(_case()),
        _proposal(
            supporting_values={
                "Subject ID": "SYN-001",
                "Informed Consent": "No",
                "Screening Date": "2026-10-20",
            }
        ),
    )
    case = without_rule.test_cases[0]

    assert "Informed Consent" not in case.input_data
    assert case.needs_human_review is True
    assert "does not cover that field" in (case.review_notes or "")
    assert no_value.test_cases[0].input_data.get("Informed Consent") != "No"
    assert "is not permitted by VAL-003" in (no_value.test_cases[0].review_notes or "")


def test_ambiguous_requirement_does_not_supply_a_supporting_value() -> None:
    analysis = _analysis()
    analysis.requirements[2] = _consent(
        needs_human_review=True,
        ambiguity_notes="The consent wording is ambiguous.",
    )
    completed, _client = _attach(_suite(_case()), _proposal(), analysis=analysis)
    case = completed.test_cases[0]

    assert "Informed Consent" not in case.input_data
    assert "marked for human review" in (case.review_notes or "")
    assert case.needs_human_review is True


def test_supporting_date_that_breaks_a_cross_field_rule_is_not_applied() -> None:
    case = _case(
        test_case_id="TC-VAL-005-cross_field-01",
        requirement_id="VAL-005",
        requirement_ids=["VAL-005"],
        category="cross_field",
        input_data={"Follow-up Date": "2026-10-08"},
        source_row=9,
    )
    completed, _client = _attach(
        _suite(case),
        _proposal(case_id=case.test_case_id),
    )
    stored = completed.test_cases[0]

    assert stored.input_data["Follow-up Date"] == "2026-10-08"
    assert "Screening Date" not in stored.input_data
    assert "conflicts with VAL-005" in (stored.review_notes or "")
    assert stored.needs_human_review is True


def test_missing_required_supporting_value_is_flagged() -> None:
    completed, _client = _attach(
        _suite(_case()),
        _proposal(
            supporting_values={"Subject ID": "SYN-001"},
            supporting_requirement_ids={"Subject ID": ["VAL-001"]},
        ),
    )
    notes = completed.test_cases[0].review_notes or ""

    assert completed.test_cases[0].needs_human_review is True
    assert "Informed Consent" in notes and "no justified supporting value" in notes
    assert "Screening Date" in notes and "no justified supporting value" in notes


def test_duplicate_supporting_subject_ids_are_not_both_applied() -> None:
    first = _case()
    second = _case(test_case_id="TC-VAL-002-boundary-02", input_data={"Age": 65})
    payload = {
        "supporting_data": [
            _proposal()["supporting_data"][0],
            _proposal(case_id=second.test_case_id)["supporting_data"][0],
        ]
    }
    completed, _client = _attach(_suite(first, second), payload)

    assert completed.test_cases[0].input_data["Subject ID"] == "SYN-001"
    assert "Subject ID" not in completed.test_cases[1].input_data
    assert "duplicates" in (completed.test_cases[1].review_notes or "")
    assert completed.test_cases[1].needs_human_review is True


def test_later_batches_are_told_which_subject_ids_are_already_used() -> None:
    """Regression: live batches reused SUBJ-101 to SUBJ-104, so four cases lost Subject ID."""

    cases = [
        _case(test_case_id=f"TC-{index:02d}", input_data={"Age": 20 + index})
        for index in range(5)
    ]
    client = _EchoClient()
    attach_supporting_data(
        _suite(*cases),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
        batch_size=4,
    )

    first, second = (json.loads(prompt) for prompt in client.prompts)
    assert "subject_ids_already_used" not in first
    assert second["subject_ids_already_used"] == ["SYN-001", "SYN-002", "SYN-003", "SYN-004"]
    assert "must differ" in second["subject_id_rule"]
    assert "subject_ids_already_used" in SYSTEM_PROMPT


def test_reserved_subject_id_from_another_case_is_not_applied() -> None:
    client = FakeClient(_proposal())
    completed = attach_supporting_data(
        _suite(_case()),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
        reserved_subject_ids=["SYN-001"],
    )
    case = completed.test_cases[0]

    assert json.loads(client.user_prompt)["subject_ids_already_used"] == ["SYN-001"]
    assert "Subject ID" not in case.input_data
    assert "duplicates another test case" in (case.review_notes or "")
    assert case.input_data["Informed Consent"] == "Yes"


def test_empty_required_field_list_does_not_call_the_model() -> None:
    client = FakeClient(_proposal())
    completed = attach_supporting_data(
        _suite(_case()),
        _analysis(),
        client=client,
        required_fields=[],
    )

    assert client.calls == 0
    assert completed.test_cases[0].input_data == {"Age": 18}
    assert completed.test_cases[0].test_data_classification == {"Age": "under_test"}
    assert completed.test_cases[0].needs_human_review is False


def test_invalid_supporting_response_is_retried_and_not_marked_successful() -> None:
    original = _case()
    before = original.model_dump()
    completed, client = _attach(_suite(original), "{not json")
    case = completed.test_cases[0]

    assert client.calls == 3
    assert original.model_dump() == before
    assert case.input_data == {"Age": 18}
    assert "Subject ID" not in case.input_data
    assert case.needs_human_review is True
    assert "Supporting data generation failed for this case." in (case.review_notes or "")
    assert case.review_status == "pending_cdm_review"
    assert case.requirement_id == "VAL-002"
    status = completed.supporting_generation
    assert status is not None
    assert status.status == "failed"
    assert status.failed_case_ids == ["TC-VAL-002-boundary-01"]
    assert status.pending_batches == 0


def test_supporting_prompt_does_not_approve_cases() -> None:
    assert "do not approve" in SYSTEM_PROMPT.casefold()
    assert "not included in this request" in SYSTEM_PROMPT
    assert "Do not invent a bound." in SYSTEM_PROMPT
    assert "Do not invent an allowed value." in SYSTEM_PROMPT
    assert "leaves blank" in SYSTEM_PROMPT


def _requirements_in(prompt: str) -> list[dict]:
    return json.loads(prompt)["requirements"]


def _case_ids_in(prompt: str) -> list[str]:
    return [item["test_case_id"] for item in json.loads(prompt)["test_cases"]]


class _EchoClient:
    def __init__(self) -> None:
        self.calls = 0
        self.sequence = 0
        self.prompts: list[str] = []

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        self.prompts.append(user_prompt)
        items = []
        for case in json.loads(user_prompt)["test_cases"]:
            self.sequence += 1
            items.append(
                {
                    "test_case_id": case["test_case_id"],
                    "supporting_values": {
                        "Subject ID": f"SYN-{self.sequence:03d}",
                        "Informed Consent": "Yes",
                        "Screening Date": "2026-10-20",
                    },
                    "supporting_requirement_ids": {
                        "Subject ID": ["VAL-001"],
                        "Informed Consent": ["VAL-003"],
                        "Screening Date": ["VAL-004"],
                    },
                }
            )
        return json.dumps({"supporting_data": items})


def test_stage_two_sends_only_relevant_compact_requirements() -> None:
    _completed, client = _attach(_suite(_case()), _proposal())
    requirements = _requirements_in(client.user_prompt)
    identifiers = [item["requirement_id"] for item in requirements]

    assert identifiers == ["VAL-001", "VAL-003", "VAL-004"]
    assert "source_values" not in json.dumps(requirements)
    assert "expected_behavior" not in json.dumps(requirements)
    for item in requirements:
        assert set(item) == {
            "requirement_id",
            "field_name",
            "condition",
            "parameters",
            "needs_human_review",
            "ambiguity_notes",
            "positive_example",
            "negative_example",
            "source_sheet",
            "source_row",
        }
    assert requirements[0]["positive_example"] == "SUBJ-001"
    assert requirements[0]["source_sheet"] == "Validation_Rules"
    assert requirements[0]["source_row"] == 5


def test_cross_field_requirement_is_included_only_when_the_paired_field_is_present() -> None:
    age_only, age_client = _attach(_suite(_case()), _proposal())
    follow_up = _case(
        test_case_id="TC-VAL-005-cross_field-01",
        requirement_id="VAL-005",
        requirement_ids=["VAL-005"],
        category="cross_field",
        input_data={"Follow-up Date": "2026-10-08"},
        source_row=9,
    )
    _completed, follow_client = _attach(
        _suite(follow_up),
        _proposal(case_id=follow_up.test_case_id),
    )

    assert "VAL-005" not in [item["requirement_id"] for item in _requirements_in(age_client.user_prompt)]
    assert "VAL-002" not in [item["requirement_id"] for item in _requirements_in(follow_client.user_prompt)]
    assert "VAL-005" in [item["requirement_id"] for item in _requirements_in(follow_client.user_prompt)]
    assert age_only.test_cases[0].input_data["Age"] == 18


def test_batches_keep_case_order_and_use_four_case_groups_by_default() -> None:
    cases = [
        _case(test_case_id=f"TC-{index:02d}", input_data={"Age": 18 + index})
        for index in range(5)
    ]
    client = _EchoClient()
    completed = attach_supporting_data(
        _suite(*cases),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
        batch_size=2,
    )

    assert DEFAULT_BATCH_SIZE == 4
    assert client.calls == 3
    assert _case_ids_in(client.prompts[0]) == ["TC-00", "TC-01"]
    assert _case_ids_in(client.prompts[1]) == ["TC-02", "TC-03"]
    assert _case_ids_in(client.prompts[2]) == ["TC-04"]
    assert [case.test_case_id for case in completed.test_cases] == [
        "TC-00",
        "TC-01",
        "TC-02",
        "TC-03",
        "TC-04",
    ]
    assert [case.input_data["Age"] for case in completed.test_cases] == [18, 19, 20, 21, 22]
    assert completed.supporting_generation is not None
    assert completed.supporting_generation.completed_batches == 3
    assert completed.supporting_generation.pending_batches == 0
    assert completed.supporting_generation.status == "complete"


def test_token_budget_defers_later_batches_without_marking_them_successful() -> None:
    import ai.supporting_data_generator as generator

    cases = [
        _case(test_case_id="TC-01", input_data={"Age": 18}),
        _case(test_case_id="TC-02", input_data={"Age": 65}),
    ]
    estimates = {"calls": 0}
    original = generator.estimate_request_tokens

    def _limited(system_prompt: str, user_prompt: str, case_count: int) -> int:
        estimates["calls"] += 1
        return 100 if estimates["calls"] <= 2 else 9_000

    generator.estimate_request_tokens = _limited
    client = _EchoClient()
    try:
        completed = attach_supporting_data(
            _suite(*cases),
            _analysis(),
            client=client,
            required_fields=REQUIRED,
            batch_size=1,
            token_budget=250,
        )
    finally:
        generator.estimate_request_tokens = original

    assert client.calls == 1
    assert completed.test_cases[0].input_data["Subject ID"] == "SYN-001"
    assert completed.test_cases[1].input_data == {"Age": 65}
    assert "deferred because the token budget" in (completed.test_cases[1].review_notes or "")
    assert completed.test_cases[1].needs_human_review is True
    status = completed.supporting_generation
    assert status is not None
    assert status.status == "partial"
    assert status.completed_batches == 1
    assert status.deferred_batches == 1
    assert status.deferred_case_ids == ["TC-02"]
    assert status.pending_batches == 0
    assert estimate_request_tokens("a", "b", 1) > 0


def test_zero_token_budget_defers_every_case_without_a_gateway_call() -> None:
    original = _case()
    before = original.model_dump()
    client = _EchoClient()
    completed = attach_supporting_data(
        _suite(original),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
        token_budget=0,
    )

    assert client.calls == 0
    assert original.model_dump() == before
    assert completed.test_cases[0].input_data == {"Age": 18}
    assert "deferred because the token budget" in (completed.test_cases[0].review_notes or "")
    status = completed.supporting_generation
    assert status is not None
    assert status.status == "deferred"
    assert status.deferred_case_ids == [original.test_case_id]


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limit_defers_the_batch_after_one_full_minute_retry() -> None:
    class _RateLimited:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            raise AIProviderError("HTTP 429 TPM limit exceeded")

    original = _case()
    before = original.model_dump()
    client = _RateLimited()
    clock = _FakeClock()
    completed = attach_supporting_data(
        _suite(original),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
        clock=clock,
        sleep=clock.sleep,
    )

    assert client.calls == 2
    assert clock.sleeps == [60.0]
    assert original.model_dump() == before
    assert completed.test_cases[0].input_data == {"Age": 18}
    assert "gateway rate limit was reached" in (completed.test_cases[0].review_notes or "")
    assert "Subject ID" not in completed.test_cases[0].input_data
    status = completed.supporting_generation
    assert status is not None
    assert status.status == "deferred"
    assert status.failed_case_ids == []


def test_per_minute_limit_paces_every_batch_instead_of_dropping_later_cases() -> None:
    """Regression: 36 live cases needed 9 batches of about 4,900 tokens each.

    The limit was applied to the whole run, so only two batches were sent and
    every later case, including both Subject ID cases, reached execution with
    no answers for the other required questions.
    """

    import ai.supporting_data_generator as generator

    cases = [
        _case(test_case_id=f"TC-{index:02d}", input_data={"Age": 18 + index % 40})
        for index in range(36)
    ]
    original = generator.estimate_request_tokens
    generator.estimate_request_tokens = lambda system, user, count: 4_900
    clock = _FakeClock()
    client = _EchoClient()
    try:
        completed = attach_supporting_data(
            _suite(*cases),
            _analysis(),
            client=client,
            required_fields=REQUIRED,
            clock=clock,
            sleep=clock.sleep,
        )
    finally:
        generator.estimate_request_tokens = original

    assert client.calls == 9
    for case in completed.test_cases:
        assert {"Age", "Subject ID", "Informed Consent", "Screening Date"} <= set(case.input_data)
        assert "deferred" not in (case.review_notes or "")
    assert len({case.input_data["Subject ID"] for case in completed.test_cases}) == 36
    status = completed.supporting_generation
    assert status is not None
    assert status.deferred_case_ids == []
    assert status.completed_batches == 9
    assert status.deferred_batches == 0
    assert len(clock.sleeps) == 4
    assert all(seconds <= 60.0 for seconds in clock.sleeps)


def test_rate_limit_waits_one_minute_and_then_completes_the_batch() -> None:
    class _LimitedOnce:
        def __init__(self) -> None:
            self.calls = 0
            self.inner = _EchoClient()

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise AIProviderError("HTTP 429 TPM limit exceeded")
            return self.inner.complete(system_prompt=system_prompt, user_prompt=user_prompt)

    client = _LimitedOnce()
    clock = _FakeClock()
    completed = attach_supporting_data(
        _suite(_case()),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
        clock=clock,
        sleep=clock.sleep,
    )

    assert client.calls == 2
    assert clock.sleeps == [60.0]
    assert completed.test_cases[0].input_data["Subject ID"] == "SYN-001"
    assert completed.supporting_generation is not None
    assert completed.supporting_generation.status == "complete"


def test_waiting_past_the_limit_defers_the_remaining_cases() -> None:
    import ai.supporting_data_generator as generator

    cases = [_case(test_case_id=f"TC-{index:02d}", input_data={"Age": 30}) for index in range(3)]
    original = generator.estimate_request_tokens
    generator.estimate_request_tokens = lambda system, user, count: 6_000
    clock = _FakeClock()
    client = _EchoClient()
    try:
        completed = attach_supporting_data(
            _suite(*cases),
            _analysis(),
            client=client,
            required_fields=REQUIRED,
            batch_size=1,
            clock=clock,
            sleep=clock.sleep,
            max_wait_seconds=30.0,
        )
    finally:
        generator.estimate_request_tokens = original

    assert client.calls == 1
    status = completed.supporting_generation
    assert status is not None
    assert status.deferred_case_ids == ["TC-01", "TC-02"]
    assert "deferred because the token budget" in (completed.test_cases[1].review_notes or "")
    assert clock.sleeps == []


def test_provider_failure_retries_the_batch_without_regenerating_stage_one() -> None:
    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise AIProviderError("temporary gateway timeout")
            return json.dumps(_proposal()["supporting_data"] and {"supporting_data": _proposal()["supporting_data"]})

    original = _case()
    before = original.model_dump()
    client = _Flaky()
    completed = attach_supporting_data(
        _suite(original),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
    )

    assert client.calls == 2
    assert original.model_dump() == before
    assert completed.test_cases[0].input_data["Age"] == 18
    assert completed.test_cases[0].input_data["Subject ID"] == "SYN-001"
    assert completed.test_cases[0].requirement_ids == ["VAL-002"]
    assert completed.supporting_generation is not None
    assert completed.supporting_generation.status == "complete"


def test_malformed_batch_can_be_recovered_by_an_individual_retry() -> None:
    class _MalformedThenValid:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return "{not json"
            return json.dumps(_proposal())

    original = _case()
    client = _MalformedThenValid()
    completed = attach_supporting_data(
        _suite(original),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
    )

    assert client.calls == 2
    assert completed.test_cases[0].input_data["Age"] == 18
    assert completed.test_cases[0].input_data["Informed Consent"] == "Yes"
    assert completed.supporting_generation is not None
    assert completed.supporting_generation.status == "complete"
    assert completed.supporting_generation.failed_case_ids == []


def test_complete_case_is_not_sent_for_supporting_data() -> None:
    case = _case(
        input_data={
            "Age": 18,
            "Subject ID": "SYN-001",
            "Informed Consent": "Yes",
            "Screening Date": "2026-10-01",
        }
    )
    client = _EchoClient()
    completed = attach_supporting_data(
        _suite(case),
        _analysis(),
        client=client,
        required_fields=REQUIRED,
    )

    assert client.calls == 0
    assert completed.test_cases[0].input_data["Age"] == 18
    assert completed.test_cases[0].test_data_classification["Age"] == "under_test"
    assert completed.test_cases[0].test_data_classification["Subject ID"] == "under_test"
    assert completed.supporting_generation is not None
    assert completed.supporting_generation.status == "not_needed"


def _mapping(case: GeneratedTestCase):
    record = approve_test_case(
        case,
        reviewer_name="A. Reviewer",
        reviewer_role="Clinical Data Manager",
        decision="approved",
        comment="Confirmed for the demo form.",
        approved_expected_behavior="CDM confirms the stated form behavior.",
    )
    approved = ApprovedCase(
        test_case=case,
        approval=record,
        execution_expected_behavior=record.approved_expected_behavior,
    )
    return map_test_case_inputs(approved, load_form_mapping())


def test_age_boundary_receives_valid_gender_and_supporting_values() -> None:
    completed, client = _attach(
        _suite(_case()),
        _proposal(
            supporting_values={
                "Subject ID": "SYN-001",
                "Gender": "Female",
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
            },
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Gender": ["VAL-006"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        ),
        _analysis(_gender()),
        FORM_FIELDS,
    )
    case = completed.test_cases[0]
    rejected, _ = _attach(
        _suite(_case()),
        _proposal(
            supporting_values={
                "Subject ID": "SYN-001",
                "Gender": "Other",
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
            },
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Gender": ["VAL-006"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        ),
        _analysis(_gender()),
        FORM_FIELDS,
    )

    assert "Do not invent an allowed value." in client.system_prompt
    assert case.input_data["Age"] == 18
    assert case.input_data["Gender"] == "Female"
    assert case.input_data["Subject ID"] == "SYN-001"
    assert case.input_data["Informed Consent"] == "Yes"
    assert case.input_data["Screening Date"] == "2026-10-20"
    assert case.test_data_classification["Age"] == "under_test"
    assert case.test_data_classification["Gender"] == "supporting"
    assert case.supporting_requirement_ids["Gender"] == ["VAL-006"]
    assert case.requirement_id == "VAL-002"
    assert _mapping(case).status == "ready"
    assert "Gender" not in rejected.test_cases[0].input_data
    assert "not permitted" in (rejected.test_cases[0].review_notes or "")


def test_gender_case_receives_valid_age_and_supporting_values() -> None:
    gender_case = _case(
        test_case_id="TC-VAL-006-allowed_value-01",
        requirement_id="VAL-006",
        requirement_ids=["VAL-006"],
        title="Gender Female",
        description="A stated allowed gender.",
        category="allowed_value",
        input_data={"Gender": "Female"},
        expected_result="Accept",
        expected_behavior="Accept only configured choices.",
        source_row=10,
        rationale="Female is a stated allowed value.",
    )
    completed, _client = _attach(
        _suite(gender_case),
        _proposal(
            "TC-VAL-006-allowed_value-01",
            supporting_values={
                "Subject ID": "SYN-010",
                "Age": 30,
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
            },
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Age": ["VAL-002"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        ),
        _analysis(_gender()),
        FORM_FIELDS,
    )
    outside, _ = _attach(
        _suite(gender_case),
        _proposal(
            "TC-VAL-006-allowed_value-01",
            supporting_values={
                "Subject ID": "SYN-010",
                "Age": 17,
                "Informed Consent": "Yes",
                "Screening Date": "2026-10-20",
            },
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Age": ["VAL-002"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        ),
        _analysis(_gender()),
        FORM_FIELDS,
    )
    case = completed.test_cases[0]

    assert case.input_data["Gender"] == "Female"
    assert case.input_data["Age"] == 30
    assert case.input_data["Subject ID"] == "SYN-010"
    assert case.test_data_classification["Gender"] == "under_test"
    assert case.test_data_classification["Age"] == "supporting"
    assert case.supporting_requirement_ids["Age"] == ["VAL-002"]
    assert _mapping(case).status == "ready"
    assert "Age" not in outside.test_cases[0].input_data
    assert "not permitted" in (outside.test_cases[0].review_notes or "")


def test_missing_consent_remains_blank() -> None:
    case = _case(
        test_case_id="TC-VAL-003-mandatory-01",
        requirement_id="VAL-003",
        requirement_ids=["VAL-003"],
        title="Consent blank",
        description="Informed consent is blank.",
        category="mandatory",
        input_data={
            "Informed Consent": None,
            "Age": 30,
            "Gender": "Female",
            "Screening Date": "2026-10-01",
        },
        expected_result="Reject",
        expected_behavior="If No or blank, prevent progression.",
        source_row=7,
        rationale="A blank consent value is the mandatory case.",
    )
    completed, _client = _attach(
        _suite(case),
        _proposal(
            "TC-VAL-003-mandatory-01",
            supporting_values={
                "Subject ID": "SYN-003",
                "Informed Consent": "Yes",
            },
            supporting_requirement_ids={
                "Subject ID": ["VAL-001"],
                "Informed Consent": ["VAL-003"],
            },
        ),
        _analysis(_gender()),
        FORM_FIELDS,
    )
    updated = completed.test_cases[0]

    assert updated.input_data["Informed Consent"] is None
    assert updated.input_data["Subject ID"] == "SYN-003"
    assert "Yes" not in updated.input_data.values()
    assert "already contains that field" in (updated.review_notes or "")


def test_missing_screening_date_remains_blank() -> None:
    case = _case(
        test_case_id="TC-VAL-004-date-01",
        requirement_id="VAL-004",
        requirement_ids=["VAL-004"],
        title="Screening date blank",
        description="Screening date is blank.",
        category="date",
        input_data={
            "Screening Date": None,
            "Age": 30,
            "Gender": "Female",
            "Subject ID": "SYN-004",
            "Informed Consent": "Yes",
        },
        expected_result="Reject",
        expected_behavior="Show a required-field message when blank.",
        source_row=8,
        rationale="A blank screening date is the date case.",
    )
    completed, client = _attach(
        _suite(case),
        _proposal(
            "TC-VAL-004-date-01",
            supporting_values={"Screening Date": "2026-10-20"},
            supporting_requirement_ids={"Screening Date": ["VAL-004"]},
        ),
        _analysis(_gender()),
        FORM_FIELDS,
    )
    updated = completed.test_cases[0]

    assert client.calls == 0
    assert updated.input_data["Screening Date"] is None
    assert "2026-10-20" not in updated.input_data.values()


def test_screening_visit_case_is_skipped_without_adding_screening_date() -> None:
    visit = _case(
        test_case_id="TC-Study_Protocol:12-mandatory-01",
        requirement_id="Study_Protocol:12",
        requirement_ids=["Study_Protocol:12"],
        title="Blank screening visit",
        description="The protocol item is Screening Visit.",
        category="mandatory",
        input_data={"Screening Visit": None},
        expected_result="Reject",
        expected_behavior="A screening date must be entered.",
        source_sheet="Study_Protocol",
        source_row=12,
        rationale="The source names Screening Visit.",
    )
    age = _case()
    completed, client = _attach(
        _suite(visit, age),
        {
            "supporting_data": [
                {
                    "test_case_id": visit.test_case_id,
                    "supporting_values": {
                        "Subject ID": "SYN-012",
                        "Informed Consent": "Yes",
                        "Screening Date": "2026-10-01",
                    },
                    "supporting_requirement_ids": {
                        "Subject ID": ["VAL-001"],
                        "Informed Consent": ["VAL-003"],
                        "Screening Date": ["VAL-004"],
                    },
                },
                _proposal()["supporting_data"][0],
            ]
        },
        _analysis(
            _requirement(
                requirement_id="Study_Protocol:12",
                field_name="Screening Visit",
                rule_type="date",
                condition="A screening date must be entered for each demo subject.",
                expected_behavior="",
                parameters=RequirementParameters(),
                source_sheet="Study_Protocol",
                source_row=12,
            )
        ),
        FORM_FIELDS,
    )
    skipped = completed.test_cases[0]
    mapping = _mapping(skipped)

    assert visit.test_case_id not in client.user_prompt
    assert skipped.input_data == {"Screening Visit": None}
    assert "Screening Date" not in skipped.input_data
    assert "Input field 'Screening Visit' (Study_Protocol row 12)" in (
        skipped.review_notes or ""
    )
    assert "not a form question" in (skipped.review_notes or "")
    assert mapping.status == "blocked"
    assert "Unknown input_data field 'Screening Visit'." in mapping.validation_errors
    assert "Screening Date" not in {
        item.form_question_title: item.value for item in mapping.mapped_inputs
    }


def test_existing_age_and_gender_are_not_overwritten() -> None:
    case = _case(
        input_data={
            "Age": 18,
            "Gender": "Female",
            "Subject ID": "SYN-001",
            "Informed Consent": "Yes",
        }
    )
    completed, _client = _attach(
        _suite(case),
        _proposal(
            supporting_values={
                "Age": 65,
                "Gender": "Male",
                "Subject ID": "SYN-999",
                "Informed Consent": "No",
                "Screening Date": "2026-10-20",
            },
            supporting_requirement_ids={
                "Age": ["VAL-002"],
                "Gender": ["VAL-006"],
                "Subject ID": ["VAL-001"],
                "Informed Consent": ["VAL-003"],
                "Screening Date": ["VAL-004"],
            },
        ),
        _analysis(_gender()),
        FORM_FIELDS,
    )
    updated = completed.test_cases[0]

    assert updated.input_data["Age"] == 18
    assert updated.input_data["Gender"] == "Female"
    assert updated.input_data["Subject ID"] == "SYN-001"
    assert updated.input_data["Informed Consent"] == "Yes"
    assert updated.input_data["Screening Date"] == "2026-10-20"
    assert "already contains that field" in (updated.review_notes or "")


def test_ambiguous_age_and_gender_requirements_do_not_supply_values() -> None:
    completed, _client = _attach(
        _suite(_case(input_data={})),
        _proposal(
            supporting_values={"Age": 30, "Gender": "Female"},
            supporting_requirement_ids={
                "Age": ["Study_Protocol:9"],
                "Gender": ["Study_Protocol:14"],
            },
        ),
        _analysis(
            _requirement(
                requirement_id="Study_Protocol:9",
                source_sheet="Study_Protocol",
                source_row=9,
                needs_human_review=True,
                ambiguity_notes="The protocol age wording needs CDM review.",
            ),
            _gender(
                requirement_id="Study_Protocol:14",
                source_sheet="Study_Protocol",
                source_row=14,
                needs_human_review=True,
                ambiguity_notes="The protocol gender list needs CDM review.",
            ),
        ),
        FORM_FIELDS,
    )
    case = completed.test_cases[0]

    assert "Age" not in case.input_data
    assert "Gender" not in case.input_data
    assert "marked for human review" in (case.review_notes or "")
    assert case.supporting_requirement_ids == {}


def test_required_form_fields_stay_enforced_when_gender_is_not_justified() -> None:
    completed, _client = _attach(
        _suite(_case()),
        _proposal(),
        _analysis(_gender()),
        FORM_FIELDS,
    )
    case = completed.test_cases[0]
    mapping = _mapping(case)
    required = {item.source_field: item.required for item in load_form_mapping().mappings}

    assert case.input_data["Age"] == 18
    assert "Gender" not in case.input_data
    assert "Required field 'Gender' has no justified supporting value." in (
        case.review_notes or ""
    )
    assert required["age"] is True
    assert required["gender"] is True
    assert mapping.status == "blocked"
    assert any(
        "Gender" in error and "cannot receive a value" in error
        for error in mapping.validation_errors
    )
