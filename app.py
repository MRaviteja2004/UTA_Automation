"""Streamlit workflow: upload, review requirements, edit test cases, execute Fill Only."""

from __future__ import annotations

import hashlib
import io

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from ai.client import RequirementAnalysisError
from ai.form_manifest import metadata_is_usable
from ai.requirement_analyzer import analyze_requirements
from ai.supporting_data_generator import attach_supporting_data
from ai.test_case_generator import generate_test_cases
from execution.field_mapping import (
    SUPPORTING_BASELINE_FIELDS,
    FieldMappingError,
    canonical_source_field,
    load_form_mapping,
)
from execution.google_forms_metadata import accepted_viewform_url, extract_google_form_metadata
from execution.google_forms_runner import execute_google_form_fill_only
from ingestion.excel_reader import ExcelIngestionError, read_workbook
from models.analysis import RequirementAnalysis
from models.form_metadata import GoogleFormMetadata
from models.test_cases import TestSuite
from ui.workflow import (
    EXPECTED_RESULTS,
    QUESTION_TYPE_LABELS,
    QuestionGroup,
    apply_requirement_edits,
    case_groups,
    edited_case_ids,
    merge_section_approvals,
    prepare_execution,
    remove_test_case,
    requirement_rows,
    revoke_approvals,
    save_question_section,
    unavailable_requirements,
)

load_dotenv()

st.set_page_config(page_title="Clinical EDC UAT Test Automation", layout="wide")

_STATE_KEYS = (
    "wf_source",
    "wf_workbook_name",
    "wf_analysis",
    "wf_requirements",
    "wf_metadata",
    "wf_metadata_error",
    "wf_suite",
    "wf_notes",
    "wf_saved_suite",
    "wf_approvals",
    "wf_section_messages",
    "wf_section_errors",
    "wf_results",
    "wf_problems",
    "wf_diagnostics",
)


def _reset(*keys: str) -> None:
    if not keys:
        stale = [key for key in st.session_state if str(key).startswith("wf_section_version_")]
        for key in stale:
            st.session_state.pop(key, None)
        keys = _STATE_KEYS
    for key in keys:
        st.session_state.pop(key, None)


def _section_version(title: str) -> int:
    return int(st.session_state.get(f"wf_section_version_{title}", 0))


def _bump_section(title: str) -> None:
    key = f"wf_section_version_{title}"
    st.session_state[key] = _section_version(title) + 1


def _bump(name: str) -> None:
    st.session_state[name] = st.session_state.get(name, 0) + 1


def _diagnose(*details: object) -> None:
    store = st.session_state.setdefault("wf_diagnostics", [])
    for detail in details:
        text = str(detail).strip()
        if text and text not in store:
            store.append(text)


def _upload_signature(uploaded: object) -> str:
    data = uploaded.getvalue()
    return f"{uploaded.name}:{hashlib.sha256(data).hexdigest()}"


def _required_question_titles() -> list[str]:
    """Form questions that need an answer for a case to be filled."""

    mapping = load_form_mapping()
    by_source = {item.source_field: item.form_question_title for item in mapping.mappings}
    return [by_source[field] for field in SUPPORTING_BASELINE_FIELDS if field in by_source]


_MISSING_FORM_URL = "Enter a Google Form URL."
_INVALID_FORM_URL = "Enter a Google Forms link that ends with /viewform."


def _entered_form_url() -> str | None:
    url = st.session_state.get("wf_form_url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _form_url_problem(url: str) -> str | None:
    if not url.strip():
        return _MISSING_FORM_URL
    if accepted_viewform_url(url) is None:
        return _INVALID_FORM_URL
    return None


def _read_form() -> GoogleFormMetadata | None:
    url = _entered_form_url()
    if url is None:
        return None
    try:
        document = extract_google_form_metadata(url)
    except Exception as exc:
        _diagnose(f"{type(exc).__name__}: {exc}"[:500])
        return None
    _diagnose(*document.diagnostics)
    return document if metadata_is_usable(document) else None


def _submit(uploaded: object, form_url: str) -> None:
    problem = _form_url_problem(form_url)
    if problem is not None:
        st.session_state["wf_problems"] = [problem]
        return
    _reset()
    st.session_state["wf_form_url"] = accepted_viewform_url(form_url)
    _bump("wf_requirements_version")
    st.session_state["wf_source"] = _upload_signature(uploaded)
    try:
        with st.spinner("Reading the workbook..."):
            workbook = read_workbook(uploaded, source_name=uploaded.name)
    except ExcelIngestionError as exc:
        _diagnose(*exc.details)
        st.session_state["wf_problems"] = ["The workbook could not be read. Check that it has the expected sheets."]
        return
    except Exception as exc:
        _diagnose(f"{type(exc).__name__}: {exc}"[:500])
        st.session_state["wf_problems"] = ["The workbook could not be read."]
        return
    st.session_state["wf_workbook_name"] = workbook.source_name or uploaded.name
    try:
        with st.spinner("Analyzing the protocol requirements..."):
            analysis = analyze_requirements(
                workbook.protocol_requirements,
                workbook.validation_rule_records,
                source_file=workbook.source_name,
            )
    except RequirementAnalysisError as exc:
        _diagnose(*exc.details)
        st.session_state["wf_problems"] = ["The requirements could not be analyzed. Try again in a moment."]
        return
    _diagnose(*analysis.warnings)
    st.session_state["wf_analysis"] = analysis
    st.session_state["wf_requirements"] = analysis
    with st.spinner("Reading the Google Form questions..."):
        metadata = _read_form()
    if metadata is None:
        st.session_state["wf_metadata_error"] = True
    else:
        st.session_state["wf_metadata"] = metadata


def _create_test_cases(analysis: RequirementAnalysis, metadata: GoogleFormMetadata) -> None:
    _reset("wf_suite", "wf_notes", "wf_saved_suite", "wf_approvals", "wf_results", "wf_problems")
    try:
        with st.spinner("Creating test cases..."):
            suite = generate_test_cases(analysis, form_metadata=metadata)
    except RequirementAnalysisError as exc:
        _diagnose(*exc.details)
        st.session_state["wf_problems"] = ["Test cases could not be created. Try again in a moment."]
        return
    _diagnose(*suite.metadata.warnings)
    try:
        suite = _attach_with_progress(suite, analysis, metadata)
    except (RequirementAnalysisError, FieldMappingError) as exc:
        _diagnose(*exc.details)
        st.session_state["wf_problems"] = [
            "Some test cases could not be completed with the other required form answers. "
            "Those cases cannot be executed yet."
        ]
    _diagnose(*suite.metadata.warnings)
    st.session_state["wf_suite"] = suite
    st.session_state["wf_notes"] = {}
    _bump("wf_cases_version")


def _attach_with_progress(
    suite: TestSuite,
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
    reserved_subject_ids: list[str] | None = None,
) -> TestSuite:
    """Fill the other required answers, showing progress while the rate limit paces requests."""

    note = st.empty()

    def show(status: object) -> None:
        total = status.completed_batches + status.failed_batches + status.deferred_batches + status.pending_batches
        done = total - status.pending_batches
        if total:
            note.info(
                f"Completing the other required answers: {done} of {total} groups done. "
                "This waits for the AI gateway's per-minute limit, so it can take a few minutes."
            )

    with st.spinner("Completing the other required answers..."):
        completed = attach_supporting_data(
            suite,
            analysis,
            required_fields=_required_question_titles(),
            form_metadata=metadata,
            progress=show,
            reserved_subject_ids=reserved_subject_ids or [],
        )
    note.empty()
    return completed


def _subject_ids_outside(suite: TestSuite, case_ids: set[str]) -> list[str]:
    """Subject IDs held by cases that a partial Stage 2 run must not reuse."""

    held: list[str] = []
    for case in suite.test_cases:
        if case.test_case_id in case_ids:
            continue
        for key, value in case.input_data.items():
            if canonical_source_field(key) == "subject_id" and isinstance(value, str) and value.strip():
                held.append(value.strip())
    return held


def _incomplete_case_ids(suite: TestSuite) -> list[str]:
    """Cases that still lack an answer for a required form question."""

    titles = _required_question_titles()
    missing: list[str] = []
    for case in suite.test_cases:
        keys = {canonical_source_field(key) for key, value in case.input_data.items()}
        needed = {canonical_source_field(title) for title in titles}
        if not needed <= keys:
            missing.append(case.test_case_id)
    return missing


def _fill_missing_answers() -> None:
    suite = st.session_state["wf_suite"]
    analysis = st.session_state["wf_requirements"]
    metadata = st.session_state["wf_metadata"]
    missing = set(_incomplete_case_ids(suite))
    subset = suite.model_copy(
        update={"test_cases": [case for case in suite.test_cases if case.test_case_id in missing]}
    )
    try:
        completed = _attach_with_progress(
            subset, analysis, metadata, _subject_ids_outside(suite, missing)
        )
    except (RequirementAnalysisError, FieldMappingError) as exc:
        _diagnose(*exc.details)
        st.session_state["wf_problems"] = ["The other required answers could not be completed. Try again in a moment."]
        return
    _diagnose(*completed.metadata.warnings)
    by_id = {case.test_case_id: case for case in completed.test_cases}
    updated = suite.model_copy(
        update={"test_cases": [by_id.get(case.test_case_id, case) for case in suite.test_cases]}
    )
    st.session_state["wf_suite"] = updated
    st.session_state["wf_approvals"] = {
        case_id: record
        for case_id, record in st.session_state.get("wf_approvals", {}).items()
        if case_id not in by_id
    }
    _bump("wf_cases_version")
    _reset("wf_results")


def _complete_added_cases(
    suite: TestSuite,
    added_ids: list[str],
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
) -> TestSuite:
    if not added_ids:
        return suite
    subset = suite.model_copy(
        update={"test_cases": [case for case in suite.test_cases if case.test_case_id in added_ids]}
    )
    try:
        completed = _attach_with_progress(
            subset, analysis, metadata, _subject_ids_outside(suite, set(added_ids))
        )
    except (RequirementAnalysisError, FieldMappingError) as exc:
        _diagnose(*exc.details)
        st.warning(
            "Added test cases were saved, but the other required form answers "
            "could not be completed for them. They cannot be executed yet."
        )
        return suite
    _diagnose(*completed.metadata.warnings)
    by_id = {case.test_case_id: case for case in completed.test_cases}
    return suite.model_copy(
        update={"test_cases": [by_id.get(case.test_case_id, case) for case in suite.test_cases]}
    )


def _frame(rows: list[dict[str, object]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame.from_records(rows, columns=columns).astype(object)


def _show_upload() -> None:
    st.header("1. Upload workbook")
    form_url = st.text_input(
        "Google Form URL",
        placeholder="Paste your Google Form URL here",
        key="google_form_url",
    )
    uploaded = st.file_uploader(
        "Study workbook (.xlsx)",
        type=["xlsx", "xlsm"],
        help="The workbook needs the Study_Protocol, Validation_Rules, and Sample_Test_Data sheets.",
    )
    if uploaded is not None:
        st.write(f"Selected file: {uploaded.name}")
        if (
            st.session_state.get("wf_source")
            and st.session_state["wf_source"] != _upload_signature(uploaded)
        ):
            _reset()
    elif st.session_state.get("wf_source"):
        _reset()
    if st.button("Submit", type="primary", disabled=uploaded is None):
        _submit(uploaded, str(form_url or ""))


def _show_requirements() -> None:
    analysis = st.session_state.get("wf_requirements")
    if analysis is None:
        return
    metadata = st.session_state.get("wf_metadata")
    st.header("2. Review requirements")
    if st.session_state.get("wf_metadata_error"):
        st.error(
            "The Google Form could not be read. Test cases cannot be created "
            "until the form questions are available."
        )
        if st.button("Read the form again"):
            _reset("wf_metadata_error")
            with st.spinner("Reading the Google Form questions..."):
                document = _read_form()
            if document is None:
                st.session_state["wf_metadata_error"] = True
            else:
                st.session_state["wf_metadata"] = document
                metadata = document
                st.rerun()
    st.caption(
        "Requirements come from the workbook. Edit a row, add a row, or delete a row. "
        "Write rules as, for example, 'Range: minimum 18 inclusive; maximum 65 inclusive' "
        "or 'Allowed values: Male, Female'."
    )
    columns = [
        "ID",
        "Field",
        "Requirement description",
        "Validation rule and values",
        "Source",
        "Form question",
    ]
    edited = st.data_editor(
        _frame(requirement_rows(analysis, metadata), columns),
        key=f"requirements_editor_{st.session_state.get('wf_requirements_version', 0)}",
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        disabled=["Source", "Form question"],
        column_config={"ID": None},
    )
    unavailable = unavailable_requirements(analysis, metadata)
    if unavailable:
        st.warning(
            "These requirements are unavailable for test generation because the form "
            "has no matching question:\n\n" + "\n".join(f"- {item}" for item in unavailable)
        )
    ready = metadata is not None and metadata_is_usable(metadata)
    if st.button("Create Test Cases", type="primary", disabled=not ready):
        updated, errors = apply_requirement_edits(
            analysis,
            edited.to_dict(orient="records"),
        )
        if errors:
            st.error("Fix these requirement rows first:\n\n" + "\n".join(f"- {item}" for item in errors))
            return
        st.session_state["wf_requirements"] = updated
        _bump("wf_requirements_version")
        _create_test_cases(updated, metadata)
        st.rerun()


def _delete_case(case_id: str, title: str) -> None:
    suite = remove_test_case(st.session_state["wf_suite"], case_id)
    st.session_state["wf_suite"] = suite
    st.session_state["wf_approvals"] = revoke_approvals(st.session_state.get("wf_approvals", {}), [case_id])
    saved = st.session_state.get("wf_saved_suite")
    if saved is not None:
        st.session_state["wf_saved_suite"] = remove_test_case(saved, case_id)
    _bump_section(title)
    _reset("wf_results")


def _save_section(title: str, rows: list[dict[str, object]]) -> None:
    suite = st.session_state["wf_suite"]
    analysis = st.session_state["wf_requirements"]
    metadata = st.session_state["wf_metadata"]
    updated, errors, added_ids = save_question_section(suite, analysis, metadata, title, rows)
    errors_by_section = st.session_state.setdefault("wf_section_errors", {})
    if errors:
        errors_by_section[title] = errors
        return
    errors_by_section.pop(title, None)
    updated = _complete_added_cases(updated, added_ids, analysis, metadata)
    st.session_state["wf_suite"] = updated
    st.session_state["wf_approvals"] = merge_section_approvals(
        updated,
        analysis,
        metadata,
        title,
        st.session_state.get("wf_approvals", {}),
    )
    st.session_state["wf_saved_suite"] = updated
    st.session_state.setdefault("wf_section_messages", {})[title] = f"{title} test cases saved."
    _bump_section(title)
    _reset("wf_results")


def _show_test_cases() -> None:
    suite = st.session_state.get("wf_suite")
    analysis = st.session_state.get("wf_requirements")
    metadata = st.session_state.get("wf_metadata")
    if suite is None or analysis is None or metadata is None:
        return
    st.header("3. Test cases by form question")
    st.caption(
        "Each test case checks one form question. Edit the input or the expected result, "
        "or add a row to add a test case. The delete icon in Actions removes that test case. "
        "Save a question to mark its test cases ready. Saving one question leaves the others unchanged."
    )
    version = st.session_state.get("wf_cases_version", 0)
    groups = case_groups(suite, analysis, metadata, st.session_state.get("wf_notes", {}))
    if not groups:
        st.info("No form question is covered by the analyzed requirements.")
        return
    incomplete = _incomplete_case_ids(suite)
    if incomplete:
        st.warning(
            f"{len(incomplete)} test case(s) do not have answers for every required form "
            "question yet, so they cannot be executed. Fill in the missing answers, then save "
            "those questions again."
        )
        if st.button("Fill in missing answers", key=f"fill_missing_{version}"):
            _fill_missing_answers()
            st.rerun()
    for group in groups:
        with st.expander(f"{group.title} ({len(group.rows)})", expanded=True):
            _show_question_section(group, analysis, metadata, version)
    if st.session_state.get("wf_approvals"):
        _show_downloads(suite, analysis, metadata)


_DELETE_ICON = ":material/delete:"


def _case_ids_in_display_order(group: QuestionGroup, editor_key: str) -> list[str]:
    """Case ids in the order the Actions column shows them, including pending edits."""

    ids = [str(row.get("ID") or "") for row in group.rows]
    state = st.session_state.get(editor_key)
    if isinstance(state, dict):
        deleted = {int(index) for index in state.get("deleted_rows") or []}
        ids = [case_id for index, case_id in enumerate(ids) if index not in deleted]
        ids.extend(str(row.get("ID") or "") for row in state.get("added_rows") or [])
    return ids


def _on_delete_case(title: str, editor_key: str, click_key: str) -> None:
    """Delete the case for the clicked Actions icon. Other questions stay as they are."""

    click = st.session_state.get(click_key)
    suite = st.session_state.get("wf_suite")
    analysis = st.session_state.get("wf_requirements")
    metadata = st.session_state.get("wf_metadata")
    if click is None or suite is None or analysis is None or metadata is None:
        return
    groups = case_groups(suite, analysis, metadata, st.session_state.get("wf_notes", {}))
    group = next((item for item in groups if item.title == title), None)
    if group is None:
        return
    row_index = int(click["row"])
    displayed = _case_ids_in_display_order(group, editor_key)
    if row_index < 0 or row_index >= len(displayed) or not displayed[row_index]:
        return
    _delete_case(displayed[row_index], title)


def _show_question_section(
    group: QuestionGroup,
    analysis: RequirementAnalysis,
    metadata: GoogleFormMetadata,
    version: int,
) -> None:
    details = QUESTION_TYPE_LABELS.get(group.question_type, group.question_type)
    if group.options:
        details += " · Choices: " + ", ".join(group.options)
    st.caption(details)
    columns = [
        "ID",
        "Test case",
        "Rule",
        "Input value",
        *group.related_titles,
        "Expected result",
        "Actions",
    ]
    section_version = _section_version(group.title)
    editor_key = f"cases_editor_{version}_{section_version}_{group.title}"
    click_key = f"delete_case_{version}_{section_version}_{group.title}"
    table_rows = [{**row, "Actions": _DELETE_ICON} for row in group.rows]
    frame = st.data_editor(
        _frame(table_rows, columns),
        key=editor_key,
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        column_config={
            "ID": None,
            "Rule": st.column_config.SelectboxColumn(
                "Rule",
                options=list(group.rules),
                default=next(iter(group.rules), None),
            ),
            "Expected result": st.column_config.SelectboxColumn(
                "Expected result",
                options=list(EXPECTED_RESULTS),
            ),
            "Actions": st.column_config.ButtonColumn(
                "Actions",
                width="small",
                help="Delete test case",
                alignment="right",
                type="tertiary",
                on_click=_on_delete_case,
                args=(group.title, editor_key, click_key),
                key=click_key,
            ),
        },
    )
    rows = frame.to_dict(orient="records")
    for row in rows:
        row.pop("Actions", None)
    if st.button(f"Save {group.title} Test Cases", type="primary", key=f"save_{version}_{group.title}"):
        _save_section(group.title, rows)
        st.rerun()
    changed = edited_case_ids(
        st.session_state["wf_suite"],
        analysis,
        metadata,
        group.title,
        rows,
    )
    if changed:
        st.session_state["wf_approvals"] = revoke_approvals(
            st.session_state.get("wf_approvals", {}),
            changed,
        )
        st.session_state.get("wf_section_messages", {}).pop(group.title, None)
        st.caption("These edits are not saved. Save this question again before running them.")
    errors = st.session_state.get("wf_section_errors", {}).get(group.title, [])
    if errors:
        st.error("Fix these test cases first:\n\n" + "\n".join(f"- {item}" for item in errors))
    elif not changed:
        message = st.session_state.get("wf_section_messages", {}).get(group.title)
        if message:
            st.success(message)


def _show_downloads(suite: TestSuite, analysis: RequirementAnalysis, metadata: GoogleFormMetadata) -> None:
    rows = [
        {
            "Form question": group.title,
            "Test case": row["Test case"],
            "Input value": row["Input value"],
            "Expected result": row["Expected result"],
        }
        for group in case_groups(suite, analysis, metadata)
        for row in group.rows
    ]
    frame = pd.DataFrame(rows, columns=["Form question", "Test case", "Input value", "Expected result"])
    workbook = io.BytesIO()
    frame.to_excel(workbook, index=False, sheet_name="Test Cases")
    columns = st.columns(2)
    columns[0].download_button(
        "Download CSV",
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name="test_suite.csv",
        mime="text/csv",
    )
    columns[1].download_button(
        "Download Excel",
        data=workbook.getvalue(),
        file_name="test_suite.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _show_execution() -> None:
    suite = st.session_state.get("wf_suite")
    analysis = st.session_state.get("wf_requirements")
    metadata = st.session_state.get("wf_metadata")
    approvals = st.session_state.get("wf_approvals") or {}
    if suite is None or analysis is None or metadata is None or not approvals:
        return
    approved_ids = set(approvals)
    st.header("4. Execute")
    groups = case_groups(suite, analysis, metadata)
    saved_rows = [row for group in groups for row in group.rows if row["ID"] in approved_ids]
    saved_questions = {group.title for group in groups if any(row["ID"] in approved_ids for row in group.rows)}
    columns = st.columns(2)
    columns[0].metric("Form questions", len(saved_questions))
    columns[1].metric("Test cases", len(saved_rows))
    st.info(
        "Execution opens a new form for each test case, enters the values, and reads them back. "
        "Submit is clicked only after that read-back matches. A status of submitted means the "
        "form's confirmation message was shown. It does not mean the clinical validation passed."
    )
    if st.button("Execute Test Cases", type="primary"):
        approved = [case for case in suite.test_cases if case.test_case_id in approved_ids]
        _run(suite.model_copy(update={"test_cases": approved}), analysis, metadata)
    results = st.session_state.get("wf_results")
    if results is None:
        return
    st.dataframe(
        pd.DataFrame(results["rows"], columns=["Question", "Test case", "Status", "Details"]),
        width="stretch",
        hide_index=True,
    )
    if results["evidence"]:
        with st.expander("Evidence details"):
            for item in results["evidence"]:
                st.markdown(f"**{item['question']} — {item['case']}**")
                for path, caption in item["images"]:
                    st.image(path, caption=caption)
                if item["file"]:
                    st.caption(f"Evidence file: {item['file']}")


def _mapping_for_entered_url(mapping):
    """Use the URL from Step 1. Question mappings stay as configured."""

    url = _entered_form_url()
    target = mapping.google_form
    if url is None or target is None or target.url == url:
        return mapping
    return mapping.model_copy(update={"google_form": target.model_copy(update={"url": url})})


def _run(suite: TestSuite, analysis: RequirementAnalysis, metadata: GoogleFormMetadata) -> None:
    if _entered_form_url() is None:
        st.error("Enter a Google Form URL in step 1, then submit the workbook again.")
        return
    try:
        mapping = _mapping_for_entered_url(load_form_mapping())
    except FieldMappingError as exc:
        _diagnose(*exc.details)
        st.error("The form configuration could not be loaded, so no test case was run.")
        return
    with st.spinner("Checking the Google Form..."):
        current = _read_form()
    if current is None:
        current = metadata.model_copy(update={"status": "error", "questions": []})
    prepared = prepare_execution(
        suite,
        analysis,
        st.session_state.get("wf_approvals", {}),
        metadata,
        current,
        mapping,
    )
    rows: list[dict[str, str]] = []
    evidence: list[dict[str, object]] = []
    for question, name, reasons in prepared.blocked:
        rows.append(
            {"Question": question, "Test case": name, "Status": "blocked", "Details": " ".join(reasons)}
        )
    for approved, mapping_result, question in prepared.ready:
        with st.spinner(f"Filling {question}: {approved.test_case.title}..."):
            result = execute_google_form_fill_only(approved, mapping_result, config=mapping)
        if result.execution_status == "submitted":
            detail = (
                "The response was submitted. "
                "This does not mean the clinical validation passed."
            )
        else:
            detail = result.error_message or "Values were entered and read back. The form was not submitted."
        rows.append(
            {
                "Question": question,
                "Test case": approved.test_case.title,
                "Status": result.execution_status,
                "Details": detail,
            }
        )
        images = [
            (path, caption)
            for path, caption in (
                (result.before_screenshot_path, "Before fill"),
                (
                    result.screenshot_path,
                    "After submission."
                    if result.execution_status == "submitted"
                    else "After fill. The form was not submitted.",
                ),
            )
            if path
        ]
        if images or result.evidence_path:
            evidence.append(
                {
                    "question": question,
                    "case": approved.test_case.title,
                    "images": images,
                    "file": result.evidence_path,
                }
            )
        _diagnose(*result.verification_notes)
    order = {group.title: index for index, group in enumerate(case_groups(suite, analysis, metadata))}
    rows.sort(key=lambda row: order.get(row["Question"], len(order)))
    st.session_state["wf_results"] = {"rows": rows, "evidence": evidence}


def _show_problems() -> None:
    for problem in st.session_state.get("wf_problems", []):
        st.error(problem)


def _show_diagnostics() -> None:
    details = st.session_state.get("wf_diagnostics")
    if not details:
        return
    with st.expander("Technical details"):
        for item in details:
            st.text(item)


st.title("Clinical EDC UAT Test Automation")
st.caption(
    "Upload the study workbook, review the requirements, check the test cases for each "
    "form question, then fill and submit the Google Form. "
    "Submission is not a clinical validation result."
)
_show_upload()
_show_problems()
_show_requirements()
_show_test_cases()
_show_execution()
_show_diagnostics()
