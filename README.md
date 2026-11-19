# Clinical EDC UAT Test Automation

This app turns a clinical study workbook into synthetic UAT test cases and enters them on a Google Form. It reads the protocol and validation rules, drafts one case per requirement, fills in the other answers those cases need, and then submits each saved case on a fresh form response.

A status of `submitted` means the form showed its confirmation message. It does not mean the clinical rule passed. The app does not invent clinical limits, allowed values, or consent states. Those come from the workbook text.

## How to run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
streamlit run app.py
```

The model client reads these environment variables:

| Variable | Role |
| --- | --- |
| `USSO_TOKEN` | Gateway token. It is never printed or included in errors. |
| `GENAI_BASE_URL` | Gateway base URL. |
| `GENAI_MODEL` | Optional. Defaults to `gpt-5.5`. |

Run the tests from the project root:

```bash
.venv/bin/pytest
```

The automated tests replace the model, the live form, and the browser. They do not call the gateway or submit the form.

## What you do in the app

1. **Upload workbook.** Paste a Google Forms link that ends with `/viewform`, choose the study workbook, and click Submit. The field starts empty. An empty or invalid link stops the run before the workbook is read. The same link is reused for form reading and later execution.
2. **Review requirements.** The workbook rows are shown as editable requirements, each matched to a form question when the labels agree. You can edit, add, or delete rows. A field with no matching question is listed as unavailable.
3. **Test cases by form question.** Cases are grouped by the question they test. Each section has its own Save button and a delete icon on each row. Saving a section marks only that section's cases ready to run. Editing a saved row drops that approval until the section is saved again. Other sections stay as they were. Cases that are still missing a required answer can be completed with **Fill in missing answers**.
4. **Execute.** The live form is read again. Each saved case opens a new blank response, fills the mapped values, and reads them back. Submit is clicked only when every read-back matches. The result table shows `submitted`, `blocked`, or `error`, with before and after screenshots.

Create Test Cases waits on the gateway's per-minute token limit, so a full workbook can take several minutes.

## How a case is built

1. `ingestion/excel_reader.py` copies the `Study_Protocol`, `Validation_Rules`, and `Sample_Test_Data` sheets. Excel row numbers stay on each record. The wording is not rewritten.
2. `ai/requirement_analyzer.py` asks the model to label those rows (mandatory, range, allowed values, date, conditional, and so on). A number or allowed value is kept only when the source sentence states it. Low confidence or ambiguous wording is marked for review.
3. `execution/google_forms_metadata.py` opens the pasted `/viewform` link and records question titles, control types, choices, and whether a required mark was detected. It does not fill or submit the form. The snapshot is hashed so a later run can tell when the form changed.
4. `ai/test_case_generator.py` drafts synthetic cases from the analyzed requirements and the captured questions. Each case cites a real requirement and keeps that requirement's sheet and row. Placeholder choices such as "Choose" are not treated as answers.
5. `ai/supporting_data_generator.py` fills the other required answers the case does not test, in batches of four. A supporting value is stored only when a cited requirement permits it. Subject IDs already used by other cases are not reused. Requests wait for the gateway's per-minute limit instead of dropping later cases. A value the case is testing, including a deliberate blank, is left unchanged.

Saving a question section in the UI records an approval for those cases. `execution/approval_gate.py` hashes the case and refuses execution when the case changed after approval. Execution uses the behavior stored on that approval, not the model's original wording.

`execution/field_mapping.py` binds each input to a question title and control type from `config/form_mappings/google_form_demo.json`. The pasted URL replaces the demo form address at execution time. The question list in that file stays as configured.

## What execution does

`execution/google_forms_runner.py` is the path behind **Execute Test Cases**.

- It opens a new browser context and the pasted `/viewform` URL, so one case cannot reuse another case's answers.
- It finds each question by its accessible title inside a form list item. Entry ids in the mapping file are stored as evidence and are not used as selectors.
- Short text and paragraphs are filled as text boxes. A dropdown is opened, the exact option is clicked, and the selected value is read back. A multiple-choice answer is checked and read back after Google marks it selected. A date is entered in Month, Day, and Year boxes, or in one `input type="date"` when that is what the form renders.
- A blank is left blank only when that field is the one the case is testing. A required supporting answer that was never supplied blocks the case, and Submit is not clicked.
- After every read-back matches, the runner clicks the form's Submit button. It does not post to the form response URL itself.
- The case is `submitted` only when the confirmation sentence configured on that form is visible. On the demo form that sentence is "Your synthetic test response has been recorded." Any other page is an error.
- Screenshots and a JSON evidence file are written under `execution_runs/`.

`execution/browser_runner.py` is a separate fill-only runner for the local HTML fixture in `tests/fixtures/sample_form.html`. It never clicks Submit and never opens Google. The Streamlit app does not use it.

Sign-in walls, CAPTCHA, and permission errors stop the run. The runner does not try to get past them.

## File guide

### App

| File | What it does |
| --- | --- |
| `app.py` | Streamlit pages for the four steps. Holds the pasted form URL, calls analysis, test-case creation, section save and delete, and execution. User-facing errors stay short. Technical notes sit in an expander. |
| `ui/workflow.py` | The logic under those pages: requirement editing, grouping cases by question, section save and delete, approval merge, and the checks that block a case before the browser starts. It does not call the model or open a browser. |
| `ui/__init__.py` | Package marker. |

### Workbook

| File | What it does |
| --- | --- |
| `ingestion/excel_reader.py` | Finds the header row on each required sheet and returns structured rows with Excel row numbers. |
| `ingestion/__init__.py` | Exports the reader and its errors. |
| `sample_data/Sample_Clinical_Study_Protocol_and_Validation_Rules.xlsx` | Demo workbook with the three sheets the reader expects. |

### Model calls

| File | What it does |
| --- | --- |
| `ai/client.py` | OpenAI-compatible gateway client. Loads settings from the environment and keeps the token out of logs and error text. |
| `ai/prompts.py` | Instructions for classifying workbook rows. The model may only label text that is already in the row. |
| `ai/requirement_analyzer.py` | Sends those rows, checks the JSON, and builds `AnalyzedRequirement` records. |
| `ai/test_case_prompts.py` | Instructions for drafting cases from analyzed requirements and form structure. |
| `ai/test_case_generator.py` | Keeps only cases that cite a real requirement and whose inputs that requirement allows. |
| `ai/supporting_data_prompts.py` | Instructions for the other required answers. The value under test is off limits. |
| `ai/supporting_data_generator.py` | Requests, checks, and attaches those answers, including the per-minute wait and unique Subject IDs. |
| `ai/form_manifest.py` | Matches requirement field names to captured questions. Form structure is not treated as a clinical rule. |
| `ai/__init__.py` | Exports the client, the analyzer, and the test-case generator. |

### Execution

| File | What it does |
| --- | --- |
| `execution/approval_gate.py` | Builds and checks the approval record and the case fingerprint. A changed case no longer matches. |
| `execution/field_mapping.py` | Loads `google_form_demo.json` and maps case fields onto question titles and control types. |
| `execution/google_forms_metadata.py` | Reads the live form's questions and computes the structure hash. Also checks that a URL is an `https` Google Forms `/viewform` link. |
| `execution/google_forms_runner.py` | Fills one fresh response, reads it back, clicks Submit, and requires the confirmation message. |
| `execution/browser_runner.py` | Fills the local HTML fixture only. Does not submit. |
| `execution/__init__.py` | Exports approval, mapping, and both runners. |
| `config/form_mappings/google_form_demo.json` | Demo form name, expected page title, question types, and entry ids. The app overrides the URL with the one pasted in Step 1. |

### Data shapes

| File | What it does |
| --- | --- |
| `models/schemas.py` | Workbook rows: protocol lines, validation rules, and sample test data. Shape only. No clinical checks. |
| `models/analysis.py` | One interpreted requirement, its parameters, and its source row. |
| `models/test_cases.py` | A generated case, which fields are under test, which are supporting, and the suite around them. |
| `models/form_metadata.py` | The captured form: questions, types, choices, and the structure hash. |
| `models/approval.py` | The reviewer decision, kept separate from the generated case. |
| `models/supporting.py` | Reviewer-supplied baseline values. The generator's path stores supporting answers on the case itself. |
| `models/execution.py` | One run's status, entered values, observed values, screenshots, and notes. Statuses are `filled`, `submitted`, `blocked`, and `error`. |
| `models/__init__.py` | Re-exports those models. |

`filled` is the local fixture runner's success status. `submitted` is the Google Form runner's success status. Neither one is a clinical pass.

### Tests and fixtures

| File | What it covers |
| --- | --- |
| `tests/test_excel_reader.py` | Workbook sheets, headers, and row numbers. |
| `tests/test_requirement_analyzer.py` | Requirement classification and rejected parameters. |
| `tests/test_genai_client.py` | Gateway settings and redacted errors. |
| `tests/test_test_case_generator.py` | Cases stay tied to real requirements. |
| `tests/test_form_guided_generation.py` | Generation uses captured questions and refuses invented choices. |
| `tests/test_supporting_data_generator.py` | Supporting answers, citations, the per-minute wait, and unique Subject IDs. |
| `tests/test_supporting_data.py` | The supporting-data model. |
| `tests/test_approval_gate.py` | Approval and fingerprint checks. |
| `tests/test_field_mapping.py` | Mapping a case onto form questions. |
| `tests/test_google_forms_metadata.py` | Question extraction from a saved form page. |
| `tests/test_google_forms_runner.py` | Fill, read-back, Submit, confirmation, and a fresh form per case. The browser is fake. |
| `tests/test_browser_runner.py` | The local HTML fixture, which is never submitted. |
| `tests/test_ui_workflow.py` | Editing, section save and delete, and execution safeguards. |
| `tests/test_app_analysis.py` | The Streamlit pages, including an empty, invalid, and valid form URL. |
| `tests/fixtures/google_form_questions.html` | A saved form page for metadata tests. |
| `tests/fixtures/sample_form.html` | The local form used by `browser_runner.py`. |

`execution_runs/` is output from real runs: a before screenshot, an after screenshot, and a JSON evidence file per case. It is not source.

## Dependencies

`requirements.txt` installs Streamlit, pandas, openpyxl, Pydantic, python-dotenv, pytest, the OpenAI client, and Playwright. Chromium is installed separately with `playwright install chromium`.
