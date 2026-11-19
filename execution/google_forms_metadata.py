"""Read the structure of the configured Google Form without changing it.

The extractor does not fill controls, open dropdowns, or click Submit or Next.
Its result describes form structure only and does not create clinical rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from execution.field_mapping import load_form_mapping
from models.form_metadata import (
    SCHEMA_VERSION,
    DateControlMetadata,
    ExtractedFormQuestion,
    GoogleFormMetadata,
    MetadataStatus,
)

_DATE_LABELS = ("Month", "Day", "Year")
_UNSUPPORTED_TYPES = {"file", "range", "color"}
_UNSUPPORTED_ROLES = {"slider"}
_EXTRACT_SCRIPT = """
() => {
  const textOf = (element) => {
    if (!element) return "";
    return (element.innerText || element.textContent || "").replace(/\\s+/g, " ").trim();
  };
  const controls = (item) => Array.from(item.querySelectorAll(
    "input, textarea, select, option, [role='textbox'], [role='listbox'], [role='radio'], [role='checkbox'], [role='option'], [role='spinbutton'], [role='slider']"
  )).map((element) => ({
    tag: element.tagName.toLowerCase(),
    role: element.getAttribute("role") || "",
    type: element.getAttribute("type") || "",
    aria_label: element.getAttribute("aria-label") || "",
    aria_required: element.getAttribute("aria-required") || "",
    data_value: element.getAttribute("data-value") || "",
    text: textOf(element)
  }));
  const items = Array.from(document.querySelectorAll("[role='listitem']"));
  return {
    listitem_count: items.length,
    body_text: textOf(document.body).slice(0, 2000),
    questions: items.map((item) => {
      const heading = item.querySelector("[role='heading']");
      return {
        visible_title: textOf(heading),
        accessible_label: (heading && heading.getAttribute("aria-label")) || textOf(heading),
        aria_required: (heading && heading.getAttribute("aria-required"))
          || item.getAttribute("aria-required")
          || "",
        controls: controls(item)
      };
    })
  };
}
"""


@contextmanager
def _chromium_page(headless: bool) -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.set_default_timeout(15000)
            yield page
        finally:
            browser.close()


def extract_google_form_metadata(
    url: str | None = None,
    *,
    captured_at: datetime | None = None,
    headless: bool = True,
    page_opener=None,
) -> GoogleFormMetadata:
    """Capture question structure from a Google Forms ``/viewform`` URL.

    When ``url`` is omitted, the configured public demo form URL is used.
    The mapping file is only read. It is not updated.
    """

    moment = captured_at or datetime.now(timezone.utc)
    target = url if url is not None else _configured_url()
    if target is None:
        return _document(
            form_url="",
            page_title="",
            captured_at=moment,
            status="error",
            questions=[],
            diagnostics=["Google Form target is not configured."],
        )
    url_error = _viewform_error(target)
    if url_error is not None:
        return _document(
            form_url=target,
            page_title="",
            captured_at=moment,
            status="error",
            questions=[],
            diagnostics=[url_error],
        )

    opener = page_opener or _chromium_page
    try:
        with opener(headless) as page:
            page.goto(target, wait_until="load")
            current = page.url
            if "accounts.google.com" in current.lower():
                return _document(
                    form_url=current,
                    page_title=_safe_title(page),
                    captured_at=moment,
                    status="blocked",
                    questions=[],
                    diagnostics=["Google sign-in is required. Credentials are not supplied."],
                )
            if _viewform_error(current) is not None or not _same_viewform(current, target):
                return _document(
                    form_url=current,
                    page_title=_safe_title(page),
                    captured_at=moment,
                    status="error",
                    questions=[],
                    diagnostics=["Navigation left the configured Google Forms viewform URL."],
                )
            body = _body_text(page).lower()
            if "recaptcha" in body or "captcha" in body:
                return _document(
                    form_url=current,
                    page_title=_safe_title(page),
                    captured_at=moment,
                    status="blocked",
                    questions=[],
                    diagnostics=["A CAPTCHA is present. It is not bypassed."],
                )
            if "you need permission" in body or "request access" in body:
                return _document(
                    form_url=current,
                    page_title=_safe_title(page),
                    captured_at=moment,
                    status="blocked",
                    questions=[],
                    diagnostics=["The form returned a permission error."],
                )
            try:
                page.wait_for_function(
                    "() => document.querySelectorAll('[role=\"listitem\"]').length > 0",
                    timeout=10_000,
                )
            except (AttributeError, PlaywrightError):
                pass
            snapshot = page.evaluate(_EXTRACT_SCRIPT)
            page_title = _safe_title(page)
    except (PlaywrightError, OSError, RuntimeError, ValueError) as exc:
        return _document(
            form_url=target,
            page_title="",
            captured_at=moment,
            status="error",
            questions=[],
            diagnostics=[f"Navigation failed: {type(exc).__name__}: {exc}"[:500]],
        )
    return _from_snapshot(
        snapshot if isinstance(snapshot, dict) else {},
        form_url=target,
        page_title=page_title,
        captured_at=moment,
    )


def _from_snapshot(
    snapshot: dict[str, Any],
    *,
    form_url: str,
    page_title: str,
    captured_at: datetime,
) -> GoogleFormMetadata:
    raw_questions = snapshot.get("questions")
    count = snapshot.get("listitem_count")
    if not isinstance(raw_questions, list) or count == 0 or not raw_questions:
        return _document(
            form_url=form_url,
            page_title=page_title,
            captured_at=captured_at,
            status="error",
            questions=[],
            diagnostics=[
                "Question containers were not found.",
                f"List item count: {count if isinstance(count, int) else 0}.",
            ],
        )
    questions = [
        _classify_question(item, position)
        for position, item in enumerate(raw_questions, start=1)
        if isinstance(item, dict)
    ]
    return _document(
        form_url=form_url,
        page_title=page_title,
        captured_at=captured_at,
        status="captured",
        questions=questions,
        diagnostics=["Form metadata describes structure only and states no clinical rule."],
    )


def _classify_question(raw: dict[str, Any], position: int) -> ExtractedFormQuestion:
    visible = _clean(raw.get("visible_title"))
    accessible = _clean(raw.get("accessible_label")) or visible
    controls = [item for item in raw.get("controls") or [] if isinstance(item, dict)]
    notes: list[str] = []
    if not visible and not accessible:
        notes.append("Question label could not be read.")
    required, required_detection = _required_state(raw, visible, accessible, controls)
    if required_detection == "not_detected":
        notes.append("Required status was not detected.")

    date_controls = _date_controls(controls)
    detected_dates = [item for item in date_controls if item.detected]
    roles = {_role(item) for item in controls}
    types = {_clean(item.get("type")).lower() for item in controls}
    radios = _choices(controls, "radio")
    checks = _choices(controls, "checkbox")
    dropdown_options = _choices(controls, "option")
    has_dropdown = "listbox" in roles or any(_clean(item.get("tag")) == "select" for item in controls)
    has_textarea = any(_clean(item.get("tag")) == "textarea" for item in controls)
    text_controls = [
        item
        for item in controls
        if _role(item) in {"textbox", "spinbutton"} or _clean(item.get("tag")) in {"input", "textarea"}
    ]
    unsupported = bool(types & _UNSUPPORTED_TYPES or roles & _UNSUPPORTED_ROLES)

    question_type = "unknown"
    type_detection = "ambiguous"
    options: list[str] = []
    options_detection = "not_applicable"
    if len(detected_dates) == 3 or any(_clean(item.get("type")).lower() == "date" for item in controls):
        question_type = "date"
        type_detection = "detected"
        if any(_clean(item.get("type")).lower() == "date" for item in controls) and len(detected_dates) != 3:
            date_controls = [
                DateControlMetadata(
                    label=_clean(item.get("aria_label")) or "Date",
                    role=_role(item),
                    input_type="date",
                    detected=True,
                )
                for item in controls
                if _clean(item.get("type")).lower() == "date"
            ]
    elif unsupported and not has_dropdown and not radios and not checks and not has_textarea:
        question_type = "unsupported"
        type_detection = "detected"
        notes.append("The question uses a control this extractor does not fill.")
    elif radios and checks:
        notes.append("Both radio and checkbox controls were present.")
    elif has_dropdown:
        question_type = "dropdown"
        type_detection = "detected"
        options = dropdown_options
        options_detection = "detected" if options else "not_detected"
        if not options:
            notes.append("Dropdown options were not visible without interaction.")
    elif radios:
        question_type = "multiple_choice"
        type_detection = "detected"
        options = radios
        options_detection = "detected" if options else "not_detected"
    elif checks:
        question_type = "checkbox"
        type_detection = "detected"
        options = checks
        options_detection = "detected" if options else "not_detected"
    elif has_textarea:
        question_type = "paragraph"
        type_detection = "detected"
    elif detected_dates:
        notes.append("Date controls were incomplete.")
    elif len(text_controls) == 1:
        question_type = "short_text"
        type_detection = "detected"
    else:
        notes.append("Question type could not be determined from accessible controls.")

    return ExtractedFormQuestion(
        position=position,
        visible_title=visible,
        accessible_label=accessible,
        question_type=question_type,
        type_detection=type_detection,
        required=required,
        required_detection=required_detection,
        options=options,
        options_detection=options_detection,
        date_controls=date_controls if question_type == "date" or detected_dates else [],
        detection_notes=notes,
    )


def _required_state(
    raw: dict[str, Any],
    visible: str,
    accessible: str,
    controls: list[dict[str, Any]],
) -> tuple[bool | None, str]:
    markers = [_clean(raw.get("aria_required")).lower()]
    markers.extend(_clean(item.get("aria_required")).lower() for item in controls)
    if "true" in markers:
        return True, "detected"
    if re.search(r"\brequired\b", accessible, flags=re.IGNORECASE) or re.search(r"\*\s*$", visible):
        return True, "detected"
    return None, "not_detected"


def _date_controls(controls: list[dict[str, Any]]) -> list[DateControlMetadata]:
    found: dict[str, dict[str, Any]] = {}
    for control in controls:
        label = _clean(control.get("aria_label")) or _clean(control.get("text"))
        for expected in _DATE_LABELS:
            if label.casefold() == expected.casefold() and expected not in found:
                found[expected] = control
    if not found:
        return []
    return [
        DateControlMetadata(
            label=expected,
            role=_role(found[expected]) if expected in found else "",
            input_type=_clean(found[expected].get("type")) if expected in found else "",
            detected=expected in found,
        )
        for expected in _DATE_LABELS
    ]


def _choices(controls: list[dict[str, Any]], role: str) -> list[str]:
    labels: list[str] = []
    for control in controls:
        if _role(control) != role and not (_clean(control.get("tag")) == "option" and role == "option"):
            continue
        label = _clean(control.get("aria_label")) or _clean(control.get("data_value")) or _clean(control.get("text"))
        if label and label not in labels:
            labels.append(label)
    return labels


def _role(control: dict[str, Any]) -> str:
    role = _clean(control.get("role")).lower()
    if role:
        return role
    tag = _clean(control.get("tag"))
    if tag == "textarea":
        return "textbox"
    if tag == "select":
        return "listbox"
    if tag == "option":
        return "option"
    return ""


def _document(
    *,
    form_url: str,
    page_title: str,
    captured_at: datetime,
    status: MetadataStatus,
    questions: list[ExtractedFormQuestion],
    diagnostics: list[str],
) -> GoogleFormMetadata:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "purpose": "form_structure_only",
        "form_url": form_url,
        "page_title": page_title,
        "questions": [item.model_dump(mode="json") for item in questions],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return GoogleFormMetadata(
        form_url=form_url,
        page_title=page_title,
        captured_at=captured_at,
        status=status,
        questions=questions,
        metadata_hash=digest,
        diagnostics=diagnostics,
    )


def accepted_viewform_url(url: str) -> str | None:
    """Return a trimmed Google Forms viewform URL, or None when it is not one."""

    text = url.strip()
    if not text or _viewform_error(text) is not None:
        return None
    return text


def _configured_url() -> str | None:
    target = load_form_mapping().google_form
    if target is None or not target.url.strip():
        return None
    return target.url


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


def _same_viewform(current: str, configured: str) -> bool:
    current_parts = urlparse(current)
    configured_parts = urlparse(configured)
    return (
        current_parts.scheme == configured_parts.scheme
        and current_parts.netloc == configured_parts.netloc
        and current_parts.path.rstrip("/") == configured_parts.path.rstrip("/")
    )


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text()
    except (AttributeError, PlaywrightError):
        return ""


def _safe_title(page: Page) -> str:
    try:
        return page.title()
    except PlaywrightError:
        return ""


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\xa0", " ").split())
