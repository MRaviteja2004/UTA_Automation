"""Match analyzed fields to captured Google Form questions.

The manifest describes form structure. It does not create clinical rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ai.client import RequirementAnalysisError
from models.form_metadata import ExtractedFormQuestion, GoogleFormMetadata

FILLABLE_QUESTION_TYPES = frozenset(
    {"short_text", "paragraph", "dropdown", "multiple_choice", "date"}
)
CHOICE_QUESTION_TYPES = frozenset({"dropdown", "multiple_choice", "checkbox"})
FORM_STRUCTURE_RULES = [
    "form_questions describe form structure only. They are not clinical rules, limits, allowed values, or expected outcomes.",
    "A required value of null means required status was not detected. Do not treat null as optional and do not invent a mandatory rule from it.",
    "Placeholder options such as Choose are not valid values.",
    "Do not rename a protocol field to a different form question.",
    "When options are listed, use only those options. When options are not listed, do not invent a form choice.",
]
_PLACEHOLDER_OPTIONS = frozenset(
    {
        "choose",
        "select",
        "please select",
        "please choose",
        "choose an option",
        "select an option",
    }
)
_REQUIRED_SUFFIX = re.compile(
    r"(?:\s*\*|\s+required(?:\s+question)?)\s*$",
    re.IGNORECASE,
)


class FormMetadataError(RequirementAnalysisError):
    """Form-specific generation needs a captured question snapshot."""


@dataclass(frozen=True)
class QuestionBinding:
    """How one protocol field relates to the captured form."""

    title: str
    question_type: str
    type_detection: str
    required: bool | None
    required_detection: str
    options: list[str]
    options_detection: str
    status: str
    reason: str


def metadata_is_usable(document: GoogleFormMetadata) -> bool:
    """A snapshot can guide generation only when questions were captured."""

    return document.status == "captured" and bool(document.questions)


def question_label(text: str) -> str:
    """Collapse whitespace and remove a trailing required marker."""

    collapsed = " ".join(text.split())
    previous = None
    while collapsed and collapsed != previous:
        previous = collapsed
        collapsed = _REQUIRED_SUFFIX.sub("", collapsed).strip()
    return collapsed


def match_key(text: str) -> str:
    """Compare labels without case, spacing, hyphen, or required-marker differences."""

    label = question_label(text).casefold().replace("-", " ")
    return " ".join(label.split())


def is_placeholder_option(value: object) -> bool:
    """True for dropdown text such as Choose. Clinical values are left alone."""

    if not isinstance(value, str):
        return False
    text = " ".join(value.split()).casefold().strip(" .")
    return text in _PLACEHOLDER_OPTIONS


def bind_field(field_name: str, questions: list[ExtractedFormQuestion]) -> QuestionBinding:
    """Match a protocol field to one form question, or explain why it cannot."""

    key = match_key(field_name)
    matches = [
        question
        for question in questions
        if question_label(question.visible_title) and match_key(question.visible_title) == key
    ]
    if len(matches) > 1:
        return _unbound(
            field_name,
            "more than one form question has this label",
            status="ambiguous",
        )
    if not matches:
        return _unbound(field_name, "no form question has this label")
    question = matches[0]
    item = compact_question(question)
    if item is None:
        return _unbound(field_name, "the question label could not be read")
    if item["type_detection"] != "detected":
        return _binding(item, "unrepresentable", "the form question type could not be determined")
    if item["question_type"] not in FILLABLE_QUESTION_TYPES:
        return _binding(
            item,
            "unrepresentable",
            f"the form question type '{item['question_type']}' cannot be used",
        )
    return _binding(item, "matched", "")


def generation_manifest(questions: list[ExtractedFormQuestion]) -> list[dict[str, Any]]:
    """Compact fillable questions. Placeholder choices are omitted."""

    return [
        item
        for question in questions
        if (item := compact_question(question)) is not None
        and item["type_detection"] == "detected"
        and item["question_type"] in FILLABLE_QUESTION_TYPES
    ]


def form_question_index(questions: list[ExtractedFormQuestion]) -> dict[str, dict[str, Any]]:
    """One compact question per label. Labels that collide are left out."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in generation_manifest(questions):
        grouped.setdefault(match_key(str(item["title"])), []).append(item)
    return {key: items[0] for key, items in grouped.items() if len(items) == 1}


def compact_question(question: ExtractedFormQuestion) -> dict[str, Any] | None:
    """One prompt-sized question. Required stays null when it was not detected."""

    title = question_label(question.visible_title)
    if not title:
        return None
    options, options_detection = _published_options(question)
    return {
        "title": title,
        "question_type": question.question_type,
        "type_detection": question.type_detection,
        "required": question.required,
        "required_detection": question.required_detection,
        "options": options,
        "options_detection": options_detection,
    }


def require_captured_metadata(document: GoogleFormMetadata) -> None:
    """Refuse generation from a missing, blocked, or empty snapshot."""

    if metadata_is_usable(document):
        return
    details = [
        "Form metadata was not captured. Test cases were not generated from a previous snapshot."
    ]
    details.extend(document.diagnostics)
    if not document.diagnostics:
        details.append(f"Form metadata status is {document.status}.")
    if document.status == "captured" and not document.questions:
        details.append("Question containers were not found.")
    raise FormMetadataError(details[0], details)


def _published_options(question: ExtractedFormQuestion) -> tuple[list[str], str]:
    if question.question_type not in CHOICE_QUESTION_TYPES:
        return [], "not_applicable"
    if question.options_detection != "detected":
        return [], question.options_detection
    options = [
        option
        for option in question.options
        if option.strip() and not is_placeholder_option(option)
    ]
    if not options:
        return [], "not_detected"
    return options, "detected"


def _binding(item: dict[str, Any], status: str, reason: str) -> QuestionBinding:
    return QuestionBinding(
        title=str(item["title"]),
        question_type=str(item["question_type"]),
        type_detection=str(item["type_detection"]),
        required=item["required"] if isinstance(item["required"], bool) else None,
        required_detection=str(item["required_detection"]),
        options=list(item["options"]),
        options_detection=str(item["options_detection"]),
        status=status,
        reason=reason,
    )


def _unbound(field_name: str, reason: str, status: str = "unrepresentable") -> QuestionBinding:
    return QuestionBinding(
        title=question_label(field_name),
        question_type="unknown",
        type_detection="not_detected",
        required=None,
        required_detection="not_detected",
        options=[],
        options_detection="not_applicable",
        status=status,
        reason=reason,
    )
