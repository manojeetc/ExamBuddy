# AMC Exam Practice App

A local Flask practice-exam GUI that discovers tests from `QuestionBank`, reads answer keys from `AnswerLog`, autosaves work in `Response`, resumes saved sessions, tracks time per question, and evaluates the finished exam. The launcher displays it in a standalone Flask GUI window using `flaskwebgui`.

## Start it

### macOS
Double-click `START_MAC.command`. On first launch it creates a private `.venv` and installs the packages in `requirements.txt` there, then opens the app in a standalone desktop window.

If macOS blocks it the first time, right-click the file, choose **Open**, then confirm. The app only binds to `127.0.0.1` (your own computer).

### Windows
Double-click `START_WINDOWS.bat`. On first launch it creates a private `.venv` and installs the packages in `requirements.txt` there.

To build a standalone Windows application, run `build_windows.bat` on a
Windows machine with Python 3 installed. The packaged application is created
at `dist\AMCExamPractice\AMCExamPractice.exe`. Distribute the entire
`dist\AMCExamPractice` folder, not only the executable. `START_WINDOWS.bat`
automatically launches that executable when it exists.

### Terminal
```bash
python3 -m pip install -r requirements.txt
python3 launcher.py
```

## Local exam assets

Saved responses are intentionally excluded from Git. The question bank and
canonical `AnswerLog/AMC10.xlsx` answer-key workbook are included so the
shared example configuration works after cloning. Other local workbook
variants remain ignored. Place additional exam data using the following
structure:

- `QuestionBank/AMC10/2025_A/` — 25 question images
- `QuestionBank/AMC10/2025_B/` — 25 question images
- `AnswerLog/AMC10.xlsx` — the uploaded 2025 A/B answer-key workbook

The home screen therefore discovers:

- `AMC10`
  - `AMC10_2025_A`
  - `AMC10_2025_B`

You can use the **Setup** tab to import a PDF into `QuestionBank`, or copy an
additional authorized question bank into the same folder structure.

## How to add exams

### Setup tab PDF import

Open **Setup**, select an exam family, enter a unique exam name such as
`AMC10_2025_A`, and upload the exam PDF. The importer uses PyMuPDF text
coordinates to detect a complete question sequence, renders each question at
the selected DPI, and writes the images under the normal `QuestionBank` folder
layout. Questions that continue onto another page are stitched vertically.

Setup also supports selecting a folder containing multiple PDFs. For batch
imports, the importer first reads the PDF text to identify the exam family,
year, and A/B form. Use descriptive filenames such as
`AMC10_2025_A.pdf` and `AMC10_2025_B.pdf` as a fallback when the PDF title does
not contain enough identifying information. It reports successes and failures
separately.

The importer refuses incomplete or ambiguous question sequences and does not
overwrite an existing exam folder unless **Replace an existing exam folder** is
selected. **Write page detection overlays** creates debug images and every
successful import writes a `manifest.json` beside the question images.

The PDF must contain a usable text layer. Scanned PDFs are rejected rather than
silently importing incorrect question boundaries. For command-line use:

```bash
python split_exam_pdf.py --pdf exam.pdf --exam AMC10_2025_A --questions 25 --dpi 250 --debug
```

### Manual folder import

Use this folder pattern:

```text
QuestionBank/
  AMC10/
    2025_A/
      ...question images...
    2025_B/
      ...question images...
```

Images are sorted naturally by filename. Names such as `...Q01.png`, `...Q02.png`, etc. work best.

### Pull requests and question assets

Do not add exam PDFs, unapproved answer workbooks, or saved responses to a pull
request. The tracked `QuestionBank/` and `AnswerLog/AMC10.xlsx` files are the
intentional shared exam-data set for this repository. Only add exam content
that you are authorized to publish. Other pull-request changes should contain
application code, templates, styles, tests, or documentation.

When reviewing or testing a pull request, use the tracked question bank or add
authorized assets locally using the structure above. If a feature requires a
new reproducible fixture, use synthetic or openly licensed test data instead of
private exam questions.

Put the matching workbook in:

```text
AnswerLog/AMC10.xlsx
```

The workbook can use either the original one-row-per-exam format or the current
one-question-per-row format. The current format uses these headers:

```text
Exam | Year | Section | Q | A | Section | Topic | Difficulty
```

In the current format, each question is a separate row. `Q` is the question
number and `A` is the answer choice. The first `Section` is the exam variant
(`A` or `B`); the second `Section` is the topic category. The parser also
continues to support the original `Answer Key` cell format:

```text
1:E, 2:B, 3:D, ... 25:A
```

Spaces in the Excel `Exam` value are ignored when matching, so `AMC 10` matches the `AMC10` folder.

## Timed and untimed modes

- **Timed:** slider from 5 minutes to 5 hours, default 75 minutes.
- **Untimed:** no deadline, but the app still tracks total active time and time spent on each question.
- Timed sessions pause when you leave the exam and continue from the saved active time when resumed.
- The app autosaves every 15 seconds, on answer selection, and when changing questions.

## Saved responses

Each attempt creates two files in `Response`:

- `.json` — complete resumable session state
- `.csv` — human-readable answers and time spent per question

Question timing is cumulative. Every visit records a start and end timestamp
in the JSON response data, so returning to a question adds the new interval to
its previous time. The Analyze page shows mean time by difficulty and lists
questions that took longer than the overall mean.

After evaluation, the CSV is updated with the correct answer, result, and points for every question.

## Evaluation

For a 25-question `AMC10` or `AMC12` exam the app uses AMC scoring:

- Correct: **6 points**
- Blank: **1.5 points**
- Incorrect: **0 points**
- Maximum: **150 points**

Other exams default to 1 point per correct answer unless you extend `scoring_profile()` in `app.py`.

## Useful controls

- **A–E**: choose an answer
- **Left / Right Arrow**: previous / next question
- Number buttons in the left panel: jump directly to a question
- **Clear**: leave the current response blank
- **Save & exit**: return home while keeping the session available for Resume
