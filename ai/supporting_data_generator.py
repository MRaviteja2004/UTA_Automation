"""Add supporting values for required form fields a case does not test.

Stage 1 test cases are copied. This module never writes over their inputs.
A proposed value is kept only when an analyzed requirement permits it.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai.client import (
    AIProviderError,
    InvalidAIResponseError,
    OpenAIRequirementClient,
    RequirementAIClient,
    load_ai_settings,
)
from ai.form_manifest import (
    CHOICE_QUESTION_TYPES,
    FORM_STRUCTURE_RULES,
    form_question_index,
    is_placeholder_option,
    match_key,
    require_captured_metadata,
)
from ai.supporting_data_prompts import SYSTEM_PROMPT, build_user_prompt
from execution.field_mapping import (
    KNOWN_SOURCE_FIELDS,
    SUPPORTING_BASELINE_FIELDS,
    canonical_source_field,
    load_form_mapping,
)
from models.analysis import AnalyzedRequirement, RequirementAnalysis, RequirementParameters
from models.form_metadata import GoogleFormMetadata
from models.test_cases import (
    GeneratedTestCase,
    InputValue,
    SupportingGenerationStatus,
    TestSuite,
)

_CONSENT_FIELD = "informed_consent"
_SUBJECT_FIELD = "subject_id"
_AGE_FIELD = "age"
_GENDER_FIELD = "gender"
DEFAULT_BATCH_SIZE = 4
# The gateway limit is tokens per rolling minute, not per run. Requests wait
# for the window to clear instead of being dropped.
TOKEN_BUDGET = 10_000
RATE_WINDOW_SECONDS = 60.0
MAX_WAIT_SECONDS = 900.0
_OUTPUT_TOKENS_PER_CASE = 250
_REQUEST_OVERHEAD_TOKENS = 100
DEFERRED_NOTE = (
    "Supporting data generation was deferred because the token budget was reached."
)
RATE_LIMIT_NOTE = (
    "Supporting data generation was deferred because the gateway rate limit was reached."
)
FAILED_NOTE = "Supporting data generation failed for this case."
_BatchKind = Literal["completed", "failed", "deferred", "rate_limited"]
_DEFERRED_OUTCOMES = {"deferred", "rate_limited"}


class _ProposedSupport(BaseModel):
    """Fields accepted from the supporting-data model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    test_case_id: str
    supporting_values: dict[str, InputValue]
    supporting_requirement_ids: dict[str, list[str]] = Field(default_factory=dict)


class _TokenPacer:
    """Keep estimated tokens within ``budget`` for any rolling ``window`` seconds."""

    def __init__(
        self,
        budget: int,
        *,
        window: float = RATE_WINDOW_SECONDS,
        max_wait: float = MAX_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.budget = max(budget, 0)
        self.window = window
        self.max_wait = max_wait
        self.clock = clock
        self.sleep = sleep
        self.waited = 0.0
        self._sent: deque[tuple[float, int]] = deque()

    def fits(self, estimate: int) -> bool:
        return 0 < estimate <= self.budget

    def reserve(self, estimate: int) -> bool:
        """Wait until ``estimate`` fits in the window, then record it."""

        if not self.fits(estimate):
            return False
        while True:
            now = self.clock()
            while self._sent and now - self._sent[0][0] >= self.window:
                self._sent.popleft()
            used = sum(tokens for _, tokens in self._sent)
            if used + estimate <= self.budget:
                self._sent.append((now, estimate))
                return True
            delay = self.window - (now - self._sent[0][0])
            if not self._wait(delay):
                return False

    def cool_down(self) -> bool:
        """After a rate-limit response, wait one full window before retrying."""

        if not self._wait(self.window):
            return False
        self._sent.clear()
        return True

    def _wait(self, delay: float) -> bool:
        delay = max(delay, 0.0)
        if self.waited + delay > self.max_wait:
            return False
        self.sleep(delay)
        self.waited += delay
        return True


@dataclass
class _Draft:
    case: GeneratedTestCase
    original: dict[str, InputValue]
    applied: dict[str, InputValue] = field(default_factory=dict)
    citations: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def estimate_request_tokens(system_prompt: str, user_prompt: str, case_count: int) -> int:
    """Conservative input-plus-output estimate for one supporting-data request.

    Input text is counted as one token per three characters. Output reserves
    a fixed allowance for every case in the request.
    """

    characters = len(system_prompt) + len(user_prompt)
    input_tokens = max(1, (characters + 2) // 3)
    output_tokens = _OUTPUT_TOKENS_PER_CASE * max(case_count, 1)
    return input_tokens + output_tokens + _REQUEST_OVERHEAD_TOKENS


def attach_supporting_data(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    *,
    client: RequirementAIClient | None = None,
    required_fields: list[str] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    token_budget: int = TOKEN_BUDGET,
    progress: Callable[[SupportingGenerationStatus], None] | None = None,
    form_metadata: GoogleFormMetadata | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
    reserved_subject_ids: Iterable[str] = (),
) -> TestSuite:
    """Return a new suite with validated supporting values added.

    ``reserved_subject_ids`` are Subject IDs held by cases outside ``suite``.
    Proposals never reuse them.

    ``token_budget`` is the gateway's tokens-per-minute limit. Batches wait for
    the rolling minute to clear instead of being dropped. A batch is deferred
    only when it alone exceeds the limit, the gateway still refuses it after one
    full-minute wait, or the total wait would exceed ``max_wait_seconds``.

    The cases already in ``suite`` are not modified. Requests are sent in
    order, ``batch_size`` cases at a time, and only with the requirements
    that can justify a missing field. ``required_fields`` uses the form
    question titles. When it is omitted, Subject ID, Age, Gender, Informed
    Consent, and Screening Date are requested. Age and Gender are accepted
    only when the cited requirement states the bounds or allowed values.
    An empty list classifies the original inputs and does not ask for
    supporting values. Captured form metadata constrains choices and labels.
    It does not add clinical rules, and a missing required status stays unknown.
    """

    if not suite.test_cases:
        return suite
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    form_index = _captured_form_index(form_metadata)
    titles = _required_titles(required_fields)
    by_id = {row.requirement_id: row for row in analysis.requirements}
    required_by_source = {canonical_source_field(title): title for title in titles}
    reserved = [item.strip() for item in reserved_subject_ids if item and item.strip()]
    used_subject_ids = {item.casefold(): item for item in reserved}
    for case in suite.test_cases:
        original = _stored_value(case.input_data, _SUBJECT_FIELD)
        if isinstance(original, str) and original.strip():
            used_subject_ids.setdefault(original.strip().casefold(), original.strip())
    proposals, proposal_warnings, outcomes, generation = _request_supporting_data(
        suite,
        analysis,
        titles,
        required_by_source,
        client,
        batch_size=batch_size,
        pacer=_TokenPacer(token_budget, max_wait=max_wait_seconds, clock=clock, sleep=sleep),
        progress=progress,
        form_index=form_index,
        used_subject_ids=used_subject_ids,
    )
    drafts = [
        _draft_case(
            case,
            None if case.test_case_id in outcomes else proposals.get(case.test_case_id),
            by_id,
            required_by_source,
            analysis,
            unavailable=outcomes.get(case.test_case_id),
            form_index=form_index,
        )
        for case in suite.test_cases
    ]
    _drop_duplicate_subject_ids(drafts, reserved)
    for draft in drafts:
        _note_missing_required(draft, titles)
    warnings = list(suite.metadata.warnings)
    unknown_cases = [
        case.test_case_id for case in suite.test_cases if _unknown_input_fields(case)
    ]
    if unknown_cases:
        warnings.append(
            "Supporting data was not generated because an input field is not a form question: "
            + ", ".join(unknown_cases)
            + "."
        )
    warnings.extend(_unmatched_form_fields(titles, form_index))
    warnings.extend(proposal_warnings)
    warnings.extend(_unknown_case_warnings(suite, proposals))
    if generation.deferred_case_ids:
        warnings.append(
            "Supporting data generation was deferred for: "
            + ", ".join(generation.deferred_case_ids)
            + "."
        )
    if generation.failed_case_ids:
        warnings.append(
            "Supporting data generation failed for: "
            + ", ".join(generation.failed_case_ids)
            + "."
        )
    completed = [_finish(draft) for draft in drafts]
    metadata = suite.metadata.model_copy(update={"warnings": warnings})
    return suite.model_copy(
        update={
            "test_cases": completed,
            "metadata": metadata,
            "supporting_generation": generation,
        }
    )


def _required_titles(required_fields: list[str] | None) -> list[str]:
    if required_fields is not None:
        return list(required_fields)
    mapping = load_form_mapping()
    by_source = {item.source_field: item.form_question_title for item in mapping.mappings}
    return [
        by_source[field]
        for field in SUPPORTING_BASELINE_FIELDS
        if field in by_source and by_source[field]
    ]


def _request_supporting_data(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    required_fields: list[str],
    required_by_source: dict[str, str],
    client: RequirementAIClient | None,
    *,
    batch_size: int,
    pacer: _TokenPacer,
    progress: Callable[[SupportingGenerationStatus], None] | None,
    form_index: dict[str, dict[str, Any]] | None = None,
    used_subject_ids: dict[str, str] | None = None,
) -> tuple[dict[str, _ProposedSupport], list[str], dict[str, str], SupportingGenerationStatus]:
    used = used_subject_ids if used_subject_ids is not None else {}
    needy = [
        case
        for case in suite.test_cases
        if not _unknown_input_fields(case)
        and _missing_titles(case.input_data, required_by_source)
    ]
    batches = [needy[index:index + batch_size] for index in range(0, len(needy), batch_size)]
    grouped: dict[str, _ProposedSupport] = {}
    warnings: list[str] = []
    outcomes: dict[str, str] = {}
    completed_batches = 0
    failed_batches = 0
    deferred_batches = 0
    spent = 0
    gateway = client

    def snapshot(pending: int) -> SupportingGenerationStatus:
        return _generation_status(
            total_cases=len(suite.test_cases),
            batch_size=batch_size,
            completed_batches=completed_batches,
            pending_batches=pending,
            failed_batches=failed_batches,
            deferred_batches=deferred_batches,
            deferred_case_ids=[
                case.test_case_id
                for case in needy
                if outcomes.get(case.test_case_id) in _DEFERRED_OUTCOMES
            ],
            failed_case_ids=[
                case.test_case_id for case in needy if outcomes.get(case.test_case_id) == "failed"
            ],
            tokens_estimated=spent,
            needy_count=len(needy),
        )

    def emit(pending: int) -> None:
        if progress is not None:
            progress(snapshot(pending))

    if not required_fields or not batches:
        status = snapshot(0)
        emit(0)
        return grouped, warnings, outcomes, status

    emit(len(batches))
    for index, batch in enumerate(batches):
        if any(case.test_case_id in outcomes for case in batch):
            continue
        user_prompt, send = _batch_prompt(batch, analysis, required_fields, form_index, used)
        if not send:
            continue
        estimate = estimate_request_tokens(SYSTEM_PROMPT, user_prompt, len(batch))
        if not pacer.fits(estimate):
            _defer_remaining([batch], outcomes, grouped)
            deferred_batches += 1
            continue
        if gateway is None:
            gateway = OpenAIRequirementClient(load_ai_settings())
        kind, spent = _send_batch(
            gateway,
            user_prompt,
            batch,
            analysis,
            required_fields,
            grouped,
            warnings,
            outcomes,
            pacer,
            spent,
            form_index,
            used,
        )
        if kind == "completed":
            completed_batches += 1
        elif kind == "failed":
            failed_batches += 1
        else:
            deferred_batches += 1
            reason = "rate_limited" if kind == "rate_limited" else "deferred"
            _defer_remaining(batches[index + 1:], outcomes, grouped, reason)
            deferred_batches += len(batches) - index - 1
            break
        emit(len(batches) - index - 1)
    status = snapshot(0)
    emit(0)
    return grouped, warnings, outcomes, status


def _parse_response(raw: str) -> list[_ProposedSupport]:
    try:
        payload = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise InvalidAIResponseError("The AI response was not valid JSON.") from exc
    if not isinstance(payload, dict) or "supporting_data" not in payload:
        raise InvalidAIResponseError(
            "The AI response must be a JSON object with a 'supporting_data' list."
        )
    items = payload["supporting_data"]
    if not isinstance(items, list):
        raise InvalidAIResponseError("The AI response field 'supporting_data' must be a list.")

    parsed: list[_ProposedSupport] = []
    errors: list[str] = []
    for index, item in enumerate(items, start=1):
        try:
            parsed.append(_ProposedSupport.model_validate(item))
        except ValidationError as exc:
            errors.append(f"Item {index}: {_format_validation_error(exc)}")
    if errors:
        raise InvalidAIResponseError(
            "The AI response did not match the required supporting-data structure.",
            errors,
        )
    return parsed


def _draft_case(
    case: GeneratedTestCase,
    proposal: _ProposedSupport | None,
    by_id: dict[str, AnalyzedRequirement],
    required_by_source: dict[str, str],
    analysis: RequirementAnalysis,
    unavailable: str | None = None,
    form_index: dict[str, dict[str, Any]] | None = None,
) -> _Draft:
    draft = _Draft(case=case, original=dict(case.input_data))
    unknown = _unknown_input_fields(case)
    if unknown:
        draft.notes.append(_unknown_field_note(case, unknown))
        return draft
    if unavailable == "deferred":
        draft.notes.append(DEFERRED_NOTE)
        return draft
    if unavailable == "rate_limited":
        draft.notes.append(RATE_LIMIT_NOTE)
        return draft
    if unavailable == "failed":
        draft.notes.append(FAILED_NOTE)
        return draft
    if proposal is None:
        if _missing_titles(draft.original, required_by_source):
            draft.notes.append("Supporting data was not returned for this test case.")
        return draft

    seen: set[str] = set()
    for key, value in proposal.supporting_values.items():
        source = canonical_source_field(key)
        title = required_by_source.get(source, key)
        if source in seen:
            draft.notes.append(f"Supporting data repeats field '{title}'.")
            continue
        seen.add(source)
        if _present(draft.original, source):
            if _stored_value(draft.original, source) != value:
                draft.notes.append(
                    f"Supporting value for '{key}' was not applied because "
                    "the original test input already contains that field."
                )
            continue
        if source not in required_by_source:
            draft.notes.append(
                f"Supporting value for '{key}' was not applied because "
                "it is not a required form field."
            )
            continue
        if _intentionally_blank(case, source, by_id):
            draft.notes.append(
                f"Supporting value for '{title}' was not applied because "
                "the original test leaves that field blank."
            )
            continue
        if _blank(value):
            draft.notes.append(f"Supporting value for '{title}' cannot be blank.")
            continue
        form_note = _form_support_block(title, value, form_index)
        if form_note is not None:
            draft.notes.append(form_note)
            continue
        cited = _citations(proposal, key, title)
        if not cited:
            draft.notes.append(
                f"Supporting value for '{title}' was not applied because "
                "it has no requirement citation."
            )
            continue
        unknown = [item for item in cited if item not in by_id]
        if unknown:
            draft.notes.append(
                f"Supporting value for '{title}' cites unknown requirement id(s): "
                + ", ".join(unknown)
                + "."
            )
            continue
        matched = [by_id[item] for item in cited if _same_field(by_id[item].field_name, title)]
        mismatched = [item for item in cited if item not in {row.requirement_id for row in matched}]
        if mismatched:
            draft.notes.append(
                f"Supporting value for '{title}' cites "
                + ", ".join(mismatched)
                + ", which does not cover that field."
            )
            continue
        ambiguous = next(
            (
                row.requirement_id
                for row in matched
                if row.needs_human_review or (row.ambiguity_notes or "").strip()
            ),
            None,
        )
        if ambiguous is not None:
            draft.notes.append(
                f"Supporting value for '{title}' was not applied because "
                f"{ambiguous} is marked for human review."
            )
            continue
        if not any(_value_permitted(title, value, row) for row in matched):
            draft.notes.append(
                f"Supporting value for '{title}' is not permitted by "
                + ", ".join(row.requirement_id for row in matched)
                + "."
            )
            continue
        conflict = _cross_field_conflict(title, value, draft.original, case.expected_result, analysis)
        if conflict is not None:
            draft.notes.append(conflict)
            continue
        draft.applied[title] = value
        draft.citations[title] = list(cited)
    return draft


def _drop_duplicate_subject_ids(drafts: list[_Draft], reserved: Iterable[str] = ()) -> None:
    seen: dict[str, str] = {item.casefold(): "another test case" for item in reserved}
    for draft in drafts:
        original = _stored_value(draft.original, _SUBJECT_FIELD)
        if isinstance(original, str) and original.strip():
            seen.setdefault(original.strip().casefold(), draft.case.test_case_id)
    for draft in drafts:
        applied = _stored_value(draft.applied, _SUBJECT_FIELD)
        if not isinstance(applied, str) or not applied.strip():
            continue
        key = applied.strip().casefold()
        owner = seen.get(key)
        if owner is not None:
            title = _applied_title(draft, _SUBJECT_FIELD)
            draft.applied.pop(title, None)
            draft.citations.pop(title, None)
            draft.notes.append(
                f"Supporting Subject ID '{applied}' duplicates {owner} and was not applied."
            )
            continue
        seen[key] = draft.case.test_case_id


def _note_missing_required(draft: _Draft, titles: list[str]) -> None:
    if _unknown_input_fields(draft.case):
        return
    combined = {**draft.original, **draft.applied}
    for title in _missing_titles(combined, {canonical_source_field(item): item for item in titles}):
        draft.notes.append(f"Required field '{title}' has no justified supporting value.")


def _finish(draft: _Draft) -> GeneratedTestCase:
    classification: dict[str, str] = {}
    for key in draft.original:
        if _is_condition(key):
            continue
        classification[key] = "under_test"
    for key in draft.applied:
        classification[key] = "supporting"
    notes = _join_notes(draft.case.review_notes, draft.notes)
    return draft.case.model_copy(
        update={
            "input_data": {**draft.original, **draft.applied},
            "test_data_classification": classification,
            "supporting_requirement_ids": draft.citations,
            "needs_human_review": draft.case.needs_human_review or notes is not None,
            "review_notes": notes,
        }
    )


def _value_permitted(title: str, value: InputValue, requirement: AnalyzedRequirement) -> bool:
    source = canonical_source_field(title)
    parameters = requirement.parameters
    if source == _AGE_FIELD:
        number = _as_number(value)
        return (
            _has_bounds(parameters)
            and number is not None
            and _inside(number, parameters) is True
        )
    if source == _GENDER_FIELD:
        return bool(parameters.allowed_values) and _is_allowed(value, parameters.allowed_values)
    if parameters.allowed_values:
        return _is_allowed(value, parameters.allowed_values)
    if _has_bounds(parameters):
        number = _as_number(value)
        return number is not None and _inside(number, parameters) is True
    if source == _CONSENT_FIELD:
        return _consent_permitted(value, requirement)
    if "date" in source:
        return _parse_date(value) is not None and _date_requirement(requirement)
    return _identifier_permitted(value, requirement)


def _consent_permitted(value: InputValue, requirement: AnalyzedRequirement) -> bool:
    if not isinstance(value, str):
        return False
    wanted = value.strip().casefold()
    if wanted not in {"yes", "no"}:
        return False
    positive = _example(requirement, "Positive Example", "positive example")
    if positive is not None and positive.casefold() == wanted:
        return True
    if _is_allowed(value, requirement.parameters.allowed_values or []):
        return True
    condition = requirement.condition.casefold()
    if wanted == "yes" and re.search(r"\bmust be yes\b|\brecorded as yes\b", condition):
        return True
    return False


def _identifier_permitted(value: InputValue, requirement: AnalyzedRequirement) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    negative = _example(requirement, "Negative Example", "negative example")
    if negative is not None and value.strip().casefold() == negative.casefold():
        return False
    condition = requirement.condition.casefold()
    if not any(word in condition for word in ("mandatory", "required", "unique")):
        return False
    return True


def _date_requirement(requirement: AnalyzedRequirement) -> bool:
    condition = requirement.condition.casefold()
    return "required" in condition or "must be entered" in condition


def _cross_field_conflict(
    title: str,
    value: InputValue,
    original: dict[str, InputValue],
    expected_result: str,
    analysis: RequirementAnalysis,
) -> str | None:
    if expected_result not in {"Accept", "Reject"}:
        return None
    for requirement in analysis.requirements:
        related_name = requirement.parameters.related_field
        relation = _date_relation(requirement.condition)
        if not related_name or relation is None:
            continue
        if _same_field(title, requirement.field_name):
            other, present = _lookup(original, related_name)
            left = _parse_date(value)
            right = _parse_date(other) if present else None
        elif _same_field(title, related_name):
            other, present = _lookup(original, requirement.field_name)
            left = _parse_date(other) if present else None
            right = _parse_date(value)
        else:
            continue
        if left is None or right is None:
            continue
        valid = _dates_match(relation, left, right)
        expected_valid = expected_result == "Accept"
        if valid != expected_valid:
            return (
                f"Supporting value for '{title}' was not applied because "
                f"it conflicts with {requirement.requirement_id}."
            )
    return None


def _citations(proposal: _ProposedSupport, key: str, title: str) -> list[str]:
    for name, cited in proposal.supporting_requirement_ids.items():
        if name == key or name == title or canonical_source_field(name) == canonical_source_field(key):
            return [item for item in cited if item.strip()]
    return []


def _unknown_input_fields(case: GeneratedTestCase) -> list[str]:
    unknown: list[str] = []
    for key in case.input_data:
        if _is_condition(key):
            continue
        if canonical_source_field(key) not in KNOWN_SOURCE_FIELDS:
            unknown.append(key)
    return unknown


def _unknown_field_note(case: GeneratedTestCase, fields: list[str]) -> str:
    quoted = ", ".join(f"'{name}'" for name in fields)
    label = "field" if len(fields) == 1 else "fields"
    verb = "is" if len(fields) == 1 else "are"
    kind = "a form question" if len(fields) == 1 else "form questions"
    return (
        f"Input {label} {quoted} ({case.source_sheet} row {case.source_row}) "
        f"{verb} not {kind}. Supporting data was not generated. "
        "CDM review is required."
    )


def _intentionally_blank(
    case: GeneratedTestCase,
    source: str,
    by_id: dict[str, AnalyzedRequirement],
) -> bool:
    requirement = by_id.get(case.requirement_id)
    if requirement is None or canonical_source_field(requirement.field_name) != source:
        return False
    value, present = _lookup(case.input_data, requirement.field_name)
    if present and not _blank(value):
        return False
    if case.category in {"mandatory", "negative"}:
        return True
    return case.category == "date" and case.expected_result == "Reject"


def _missing_titles(values: dict[str, InputValue], required_by_source: dict[str, str]) -> list[str]:
    present = {canonical_source_field(key) for key in values if not _is_condition(key)}
    return [title for source, title in required_by_source.items() if source not in present]


def _present(values: dict[str, InputValue], source: str) -> bool:
    return any(canonical_source_field(key) == source for key in values if not _is_condition(key))


def _stored_value(values: dict[str, InputValue], source: str) -> InputValue:
    for key, value in values.items():
        if canonical_source_field(key) == source:
            return value
    return None


def _applied_title(draft: _Draft, source: str) -> str:
    for key in draft.applied:
        if canonical_source_field(key) == source:
            return key
    return ""


def _lookup(values: dict[str, InputValue], field_name: str) -> tuple[InputValue, bool]:
    source = canonical_source_field(field_name)
    for key, value in values.items():
        if canonical_source_field(key) == source:
            return value, True
    return None, False


def _same_field(left: str, right: str) -> bool:
    return canonical_source_field(left) == canonical_source_field(right)


def _unknown_case_warnings(
    suite: TestSuite,
    proposals: dict[str, _ProposedSupport],
) -> list[str]:
    known = {case.test_case_id for case in suite.test_cases}
    return [
        f"Supporting data for unknown test case id {test_case_id} was ignored."
        for test_case_id in proposals
        if test_case_id not in known
    ]


def _case_payload(case: GeneratedTestCase) -> dict[str, Any]:
    return {
        "test_case_id": case.test_case_id,
        "requirement_id": case.requirement_id,
        "requirement_ids": list(case.requirement_ids),
        "category": case.category,
        "input_data": dict(case.input_data),
        "expected_result": case.expected_result,
        "source_file": case.source_file,
        "source_sheet": case.source_sheet,
        "source_row": case.source_row,
    }


def _requirement_payload(row: AnalyzedRequirement) -> dict[str, Any]:
    """Compact requirement context for one Stage 2 request."""

    return {
        "requirement_id": row.requirement_id,
        "field_name": row.field_name,
        "condition": row.condition,
        "parameters": row.parameters.model_dump(),
        "needs_human_review": row.needs_human_review,
        "ambiguity_notes": row.ambiguity_notes,
        "positive_example": _example(row, "Positive Example", "positive example"),
        "negative_example": _example(row, "Negative Example", "negative example"),
        "source_sheet": row.source_sheet,
        "source_row": row.source_row,
    }


def _batch_prompt(
    cases: list[GeneratedTestCase],
    analysis: RequirementAnalysis,
    required_fields: list[str],
    form_index: dict[str, dict[str, Any]] | None = None,
    used_subject_ids: dict[str, str] | None = None,
) -> tuple[str, bool]:
    requested = _missing_field_titles(cases, required_fields)
    form_questions = None
    if form_index is not None:
        requested, form_questions = _matched_form_questions(requested, form_index)
    relevant = _relevant_requirements(cases, analysis, requested)
    asks_subject = any(canonical_source_field(title) == _SUBJECT_FIELD for title in requested)
    prompt = build_user_prompt(
        [_case_payload(case) for case in cases],
        [_requirement_payload(row) for row in relevant],
        requested,
        form_questions,
        FORM_STRUCTURE_RULES if form_questions is not None else None,
        sorted((used_subject_ids or {}).values()) if asks_subject else None,
    )
    return prompt, form_index is None or bool(requested)


def _relevant_requirements(
    cases: list[GeneratedTestCase],
    analysis: RequirementAnalysis,
    required_fields: list[str],
) -> list[AnalyzedRequirement]:
    required_sources = {canonical_source_field(title) for title in required_fields}
    present_by_case = [
        {canonical_source_field(key) for key in case.input_data if not _is_condition(key)}
        for case in cases
    ]
    missing = {
        source
        for source in required_sources
        if any(source not in present for present in present_by_case)
    }
    selected: list[AnalyzedRequirement] = []
    seen: set[str] = set()
    for row in analysis.requirements:
        if row.requirement_id in seen:
            continue
        field_name = canonical_source_field(row.field_name)
        related_name = row.parameters.related_field
        related = canonical_source_field(related_name) if related_name else None
        include = field_name in missing
        if not include and related and _date_relation(row.condition):
            paired_present = any(field_name in present for present in present_by_case)
            related_present = any(related in present for present in present_by_case)
            if field_name in missing and related_present:
                include = True
            elif related in missing and paired_present:
                include = True
        if include:
            selected.append(row)
            seen.add(row.requirement_id)
    return selected


def _captured_form_index(
    document: GoogleFormMetadata | None,
) -> dict[str, dict[str, Any]] | None:
    if document is None:
        return None
    require_captured_metadata(document)
    return form_question_index(document.questions)


def _matched_form_questions(
    titles: list[str],
    form_index: dict[str, dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    matched_titles: list[str] = []
    questions: list[dict[str, Any]] = []
    for title in titles:
        item = form_index.get(match_key(title))
        if item is None or match_key(str(item["title"])) != match_key(title):
            continue
        matched_titles.append(str(item["title"]))
        questions.append(item)
    return matched_titles, questions


def _unmatched_form_fields(
    titles: list[str],
    form_index: dict[str, dict[str, Any]] | None,
) -> list[str]:
    if form_index is None:
        return []
    return [
        (
            f"Required field '{title}' does not match a form question. "
            "It was not mapped to another question, and supporting data was not requested for it."
        )
        for title in titles
        if match_key(title) not in form_index
    ]


def _form_support_block(
    title: str,
    value: InputValue,
    form_index: dict[str, dict[str, Any]] | None,
) -> str | None:
    if form_index is None:
        return None
    item = form_index.get(match_key(title))
    if item is None:
        return (
            f"Supporting value for '{title}' was not applied because "
            "that field is not a form question."
        )
    if is_placeholder_option(value):
        return (
            f"Supporting value for '{title}' was not applied because "
            f"'{value}' is a placeholder option."
        )
    options = item.get("options")
    if (
        item.get("question_type") in CHOICE_QUESTION_TYPES
        and item.get("options_detection") == "detected"
        and isinstance(options, list)
        and not _is_allowed(value, [str(option) for option in options])
    ):
        return (
            f"Supporting value for '{title}' was not applied because "
            "it is not a form choice."
        )
    return None


def _missing_field_titles(
    cases: list[GeneratedTestCase],
    required_fields: list[str],
) -> list[str]:
    titles: list[str] = []
    for title in required_fields:
        source = canonical_source_field(title)
        if any(not _present(case.input_data, source) for case in cases):
            titles.append(title)
    return titles


def _send_batch(
    gateway: RequirementAIClient,
    user_prompt: str,
    batch: list[GeneratedTestCase],
    analysis: RequirementAnalysis,
    required_fields: list[str],
    grouped: dict[str, _ProposedSupport],
    warnings: list[str],
    outcomes: dict[str, str],
    pacer: _TokenPacer,
    spent: int,
    form_index: dict[str, dict[str, Any]] | None = None,
    used: dict[str, str] | None = None,
) -> tuple[_BatchKind, int]:
    state, parsed, spent = _attempt(gateway, user_prompt, len(batch), spent, pacer)
    if state == "ok" and parsed is not None:
        _store_proposals(parsed, grouped, warnings, used)
        return "completed", spent
    if state == "rate":
        _defer_remaining([batch], outcomes, grouped, "rate_limited")
        return "rate_limited", spent
    if state == "budget":
        _defer_remaining([batch], outcomes, grouped, "deferred")
        return "deferred", spent
    if state == "provider":
        retry_state, parsed, spent = _attempt(
            gateway,
            user_prompt,
            len(batch),
            spent,
            pacer,
        )
        if retry_state == "ok" and parsed is not None:
            _store_proposals(parsed, grouped, warnings, used)
            return "completed", spent
        if retry_state == "rate":
            _defer_remaining([batch], outcomes, grouped, "rate_limited")
            return "rate_limited", spent
        if retry_state == "budget":
            _defer_remaining([batch], outcomes, grouped, "deferred")
            return "deferred", spent
    return _retry_individuals(
        gateway,
        batch,
        analysis,
        required_fields,
        grouped,
        warnings,
        outcomes,
        pacer,
        spent,
        form_index,
        used,
    )


def _retry_individuals(
    gateway: RequirementAIClient,
    batch: list[GeneratedTestCase],
    analysis: RequirementAnalysis,
    required_fields: list[str],
    grouped: dict[str, _ProposedSupport],
    warnings: list[str],
    outcomes: dict[str, str],
    pacer: _TokenPacer,
    spent: int,
    form_index: dict[str, dict[str, Any]] | None = None,
    used: dict[str, str] | None = None,
) -> tuple[_BatchKind, int]:
    failed = False
    for index, case in enumerate(batch):
        if case.test_case_id in grouped or case.test_case_id in outcomes:
            continue
        prompt, send = _batch_prompt([case], analysis, required_fields, form_index, used)
        if not send:
            continue
        state, parsed, spent = _attempt(gateway, prompt, 1, spent, pacer)
        if state in {"invalid", "provider"}:
            state, parsed, spent = _attempt(gateway, prompt, 1, spent, pacer)
        if state == "ok" and parsed is not None:
            _store_proposals(parsed, grouped, warnings, used)
            continue
        if state == "rate":
            _defer_remaining([batch[index:]], outcomes, grouped, "rate_limited")
            return "rate_limited", spent
        if state == "budget":
            _defer_remaining([batch[index:]], outcomes, grouped, "deferred")
            return "deferred", spent
        outcomes[case.test_case_id] = "failed"
        failed = True
    if any(outcomes.get(case.test_case_id) == "deferred" for case in batch):
        return "deferred", spent
    if failed or any(outcomes.get(case.test_case_id) == "failed" for case in batch):
        return "failed", spent
    return "completed", spent


def _attempt(
    gateway: RequirementAIClient,
    user_prompt: str,
    case_count: int,
    spent: int,
    pacer: _TokenPacer,
    *,
    retry_rate_limit: bool = True,
) -> tuple[str, list[_ProposedSupport] | None, int]:
    estimate = estimate_request_tokens(SYSTEM_PROMPT, user_prompt, case_count)
    if not pacer.reserve(estimate):
        return "budget", None, spent
    spent += estimate
    try:
        parsed = _parse_response(
            gateway.complete(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt)
        )
    except InvalidAIResponseError:
        return "invalid", None, spent
    except AIProviderError as exc:
        if _is_rate_limit(exc):
            if retry_rate_limit and pacer.cool_down():
                return _attempt(
                    gateway,
                    user_prompt,
                    case_count,
                    spent,
                    pacer,
                    retry_rate_limit=False,
                )
            return "rate", None, spent
        return "provider", None, spent
    return "ok", parsed, spent


def _store_proposals(
    parsed: list[_ProposedSupport],
    grouped: dict[str, _ProposedSupport],
    warnings: list[str],
    used: dict[str, str] | None = None,
) -> None:
    for item in parsed:
        if item.test_case_id in grouped:
            warnings.append(
                f"Duplicate supporting data for {item.test_case_id} was ignored."
            )
            continue
        grouped[item.test_case_id] = item
        if used is None:
            continue
        subject = _stored_value(item.supporting_values, _SUBJECT_FIELD)
        if isinstance(subject, str) and subject.strip():
            used.setdefault(subject.strip().casefold(), subject.strip())


def _defer_remaining(
    batches: list[list[GeneratedTestCase]],
    outcomes: dict[str, str],
    grouped: dict[str, _ProposedSupport],
    reason: str = "deferred",
) -> None:
    for batch in batches:
        for case in batch:
            if case.test_case_id in grouped or case.test_case_id in outcomes:
                continue
            outcomes[case.test_case_id] = reason


def _is_rate_limit(exc: AIProviderError) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in ("429", "rate limit", "tokens per minute", "tpm"))


def _generation_status(
    *,
    total_cases: int,
    batch_size: int,
    completed_batches: int,
    pending_batches: int,
    failed_batches: int,
    deferred_batches: int,
    deferred_case_ids: list[str],
    failed_case_ids: list[str],
    tokens_estimated: int,
    needy_count: int,
) -> SupportingGenerationStatus:
    if needy_count == 0:
        label = "not_needed"
    elif not failed_batches and not deferred_batches:
        label = "complete"
    elif completed_batches or (failed_batches and deferred_batches):
        label = "partial"
    elif failed_batches:
        label = "failed"
    else:
        label = "deferred"
    return SupportingGenerationStatus(
        total_cases=total_cases,
        batch_size=batch_size,
        completed_batches=completed_batches,
        pending_batches=pending_batches,
        failed_batches=failed_batches,
        deferred_batches=deferred_batches,
        status=label,
        deferred_case_ids=list(deferred_case_ids),
        failed_case_ids=list(failed_case_ids),
        tokens_estimated=tokens_estimated,
    )


def _example(requirement: AnalyzedRequirement, *labels: str) -> str | None:
    wanted = {label.casefold() for label in labels}
    for key, value in requirement.source_values.items():
        if key.casefold() not in wanted or value is None:
            continue
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _date_relation(condition: str) -> str | None:
    lowered = condition.casefold()
    if "on or after" in lowered:
        return "on_or_after"
    if "after" in lowered:
        return "after"
    if "before" in lowered or "earlier" in lowered:
        return "before"
    return None


def _dates_match(relation: str, left: date, right: date) -> bool:
    if relation == "on_or_after":
        return left >= right
    if relation == "after":
        return left > right
    return left < right


def _parse_date(value: InputValue) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _inside(number: int | float, parameters: RequirementParameters) -> bool | None:
    if parameters.minimum is not None:
        if parameters.minimum_inclusive is True and number < parameters.minimum:
            return False
        if parameters.minimum_inclusive is False and number <= parameters.minimum:
            return False
        if parameters.minimum_inclusive is None:
            return None
    if parameters.maximum is not None:
        if parameters.maximum_inclusive is True and number > parameters.maximum:
            return False
        if parameters.maximum_inclusive is False and number >= parameters.maximum:
            return False
        if parameters.maximum_inclusive is None:
            return None
    if parameters.minimum is None and parameters.maximum is None:
        return None
    return True


def _has_bounds(parameters: RequirementParameters) -> bool:
    return parameters.minimum is not None or parameters.maximum is not None


def _is_allowed(value: InputValue, allowed: list[str]) -> bool:
    if value is None:
        return False
    text = str(value).strip().casefold()
    return any(text == item.strip().casefold() for item in allowed)


def _as_number(value: InputValue) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _blank(value: InputValue) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_condition(key: str) -> bool:
    return " ".join(key.split()).casefold() in {"condition_met", "condition met"}


def _join_notes(existing: str | None, notes: list[str]) -> str | None:
    parts: list[str] = []
    if existing and existing.strip():
        parts.append(existing.strip())
    for note in notes:
        if note not in parts:
            parts.append(note)
    return " ".join(parts) if parts else None


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "value"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
