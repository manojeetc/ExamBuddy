from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from split_exam_pdf import ExtractionError, extract_exam_questions, parse_exam_name

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
QUESTION_BANK = BASE_DIR / "QuestionBank"
ANSWER_LOG = BASE_DIR / "AnswerLog"
RESPONSE_DIR = BASE_DIR / "Response"
RESPONSE_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ANSWER_CHOICES = ["A", "B", "C", "D", "E"]

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# A small lock is enough for this local, single-user app and prevents two
# autosave requests from writing the same session file at the same time.
SESSION_LOCK = threading.Lock()
WORKBOOK_CACHE: dict[Path, tuple[int, list[dict[str, Any]]]] = {}
WORKBOOK_CACHE_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def natural_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def safe_session_id(session_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", session_id or ""):
        abort(400, "Invalid session id")
    return session_id


def session_json_path(session_id: str) -> Path:
    return RESPONSE_DIR / f"{safe_session_id(session_id)}.json"


def session_csv_path(session_id: str) -> Path:
    return RESPONSE_DIR / f"{safe_session_id(session_id)}.csv"


def read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(name))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = []
    for si in root.findall("x:si", ns):
        parts = [node.text or "" for node in si.findall(".//x:t", ns)]
        strings.append("".join(parts))
    return strings


def cell_column_index(cell_ref: str) -> int:
    letters = re.match(r"([A-Z]+)", cell_ref or "")
    if not letters:
        return 0
    value = 0
    for ch in letters.group(1):
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value - 1


def parse_xlsx_rows(path: Path) -> list[dict[str, Any]]:
    """Read simple tabular .xlsx data using only the Python standard library.

    This intentionally avoids requiring Excel/openpyxl on the machine that runs
    the practice app. It supports shared strings, inline strings, numbers and
    booleans, which is sufficient for the AnswerLog workbook format.
    """
    rows_out: list[dict[str, Any]] = []
    if not path.exists():
        return rows_out
    signature = path.stat().st_mtime_ns
    with WORKBOOK_CACHE_LOCK:
        cached = WORKBOOK_CACHE.get(path)
        if cached and cached[0] == signature:
            return cached[1]

    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ns = {"x": main_ns, "r": rel_ns}

    with zipfile.ZipFile(path, "r") as zf:
        shared = read_shared_strings(zf)
        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rels = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rel_root.findall(f"{{{pkg_rel_ns}}}Relationship")
        }

        sheet_targets: list[tuple[str, str]] = []
        for sheet in wb_root.findall("x:sheets/x:sheet", ns):
            rid = sheet.attrib.get(f"{{{rel_ns}}}id")
            target = rels.get(rid or "")
            if target:
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
                sheet_targets.append((sheet.attrib.get("name", "Sheet"), target))

        for sheet_name, target in sheet_targets:
            if target not in zf.namelist():
                continue
            root = ET.fromstring(zf.read(target))
            matrix: list[list[Any]] = []
            for row in root.findall(".//x:sheetData/x:row", ns):
                values: dict[int, Any] = {}
                max_idx = -1
                for cell in row.findall("x:c", ns):
                    idx = cell_column_index(cell.attrib.get("r", "A1"))
                    max_idx = max(max_idx, idx)
                    ctype = cell.attrib.get("t")
                    value_node = cell.find("x:v", ns)
                    inline = cell.find("x:is", ns)
                    raw = value_node.text if value_node is not None else None
                    if ctype == "s" and raw is not None:
                        try:
                            value: Any = shared[int(raw)]
                        except (ValueError, IndexError):
                            value = raw
                    elif ctype == "inlineStr" and inline is not None:
                        value = "".join((t.text or "") for t in inline.findall(".//x:t", ns))
                    elif ctype == "b":
                        value = raw == "1"
                    elif ctype in {"str", "e"}:
                        value = raw or ""
                    else:
                        if raw is None:
                            value = ""
                        else:
                            try:
                                number = float(raw)
                                value = int(number) if number.is_integer() else number
                            except ValueError:
                                value = raw
                    values[idx] = value
                if max_idx >= 0:
                    matrix.append([values.get(i, "") for i in range(max_idx + 1)])

            if not matrix:
                continue
            headers: list[str] = []
            header_counts: dict[str, int] = {}
            for raw_header in matrix[0]:
                header = str(raw_header).strip()
                header_counts[header] = header_counts.get(header, 0) + 1
                if header_counts[header] > 1:
                    header = f"{header}_{header_counts[header]}"
                headers.append(header)
            for data_row in matrix[1:]:
                if not any(str(x).strip() for x in data_row):
                    continue
                padded = data_row + [""] * max(0, len(headers) - len(data_row))
                item = {headers[i]: padded[i] for i in range(len(headers)) if headers[i]}
                item["_sheet"] = sheet_name
                rows_out.append(item)
    with WORKBOOK_CACHE_LOCK:
        WORKBOOK_CACHE[path] = (signature, rows_out)
    return rows_out


def clear_workbook_cache() -> None:
    with WORKBOOK_CACHE_LOCK:
        WORKBOOK_CACHE.clear()


def parse_answer_key(value: Any) -> dict[int, str]:
    return {int(q): a.upper() for q, a in re.findall(r"(\d+)\s*:\s*([A-Ea-e])", str(value or ""))}


def parse_variant(category: str, folder_name: str) -> tuple[str | None, str | None, str]:
    # Supports examples such as 2025_A, AMC10_2025_A, 2026-B, etc.
    tokens = [t for t in re.split(r"[_\-\s]+", folder_name) if t]
    year = next((t for t in tokens if re.fullmatch(r"20\d{2}", t)), None)
    section = None
    if year:
        pos = tokens.index(year)
        if pos + 1 < len(tokens):
            section = tokens[pos + 1].upper()
    label = folder_name if normalize(folder_name).startswith(normalize(category)) else f"{category}_{folder_name}"
    return year, section, label


def scan_exams() -> dict[str, list[dict[str, Any]]]:
    exams: dict[str, list[dict[str, Any]]] = {}
    if not QUESTION_BANK.exists():
        return exams

    for category_dir in sorted((p for p in QUESTION_BANK.iterdir() if p.is_dir()), key=lambda p: natural_key(p.name)):
        variants: list[dict[str, Any]] = []
        for variant_dir in sorted((p for p in category_dir.iterdir() if p.is_dir()), key=lambda p: natural_key(p.name)):
            images = sorted(
                [p for p in variant_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS],
                key=lambda p: natural_key(p.name),
            )
            if not images:
                continue
            year, section, label = parse_variant(category_dir.name, variant_dir.name)
            variants.append(
                {
                    "category": category_dir.name,
                    "variant": variant_dir.name,
                    "label": label,
                    "year": year,
                    "section": section,
                    "question_count": len(images),
                    "folder": variant_dir,
                    "images": images,
                }
            )
        if variants:
            exams[category_dir.name] = variants
    return exams


def find_exam(category: str, variant: str) -> dict[str, Any] | None:
    return next(
        (
            exam
            for exam in scan_exams().get(category, [])
            if exam["variant"] == variant
        ),
        None,
    )


def find_answer_row(category: str, year: str | None, section: str | None) -> tuple[dict[str, Any] | None, Path | None]:
    preferred = ANSWER_LOG / f"{category}.xlsx"
    workbooks = [preferred] if preferred.exists() else []
    workbooks += [p for p in sorted(ANSWER_LOG.glob("*.xlsx")) if p != preferred]

    for workbook in workbooks:
        try:
            rows = parse_xlsx_rows(workbook)
        except Exception:
            continue
        for row in rows:
            exam_name = row.get("Exam", "")
            row_year = row.get("Year", "")
            row_section = row.get("Section", "")
            year_match = year is None or str(row_year).split(".")[0] == str(year)
            section_match = section is None or normalize(row_section) == normalize(section)
            if normalize(exam_name) == normalize(category) and year_match and section_match:
                return row, workbook
    return None, None


def answer_key_for_exam(exam: dict[str, Any]) -> tuple[dict[int, str], str | None]:
    row, workbook = find_answer_row(exam["category"], exam["year"], exam["section"])
    if not row:
        return {}, None
    if row.get("Q") is not None and row.get("A") is not None:
        key: dict[int, str] = {}
        for candidate in parse_xlsx_rows(workbook):
            if (
                normalize(candidate.get("Exam")) == normalize(exam["category"])
                and str(candidate.get("Year", "")).split(".")[0] == str(exam["year"])
                and normalize(candidate.get("Section")) == normalize(exam["section"])
            ):
                try:
                    question = int(candidate.get("Q"))
                except (TypeError, ValueError):
                    continue
                answer = str(candidate.get("A", "")).strip().upper()
                if answer in ANSWER_CHOICES:
                    key[question] = answer
        return key, workbook.name if workbook else None
    return parse_answer_key(row.get("Answer Key")), workbook.name if workbook else None


def answer_rows_for_exam(exam: dict[str, Any]) -> list[dict[str, Any]]:
    """Return question metadata rows for an exam's current answer workbook."""
    _, workbook = find_answer_row(exam["category"], exam["year"], exam["section"])
    if not workbook:
        return []
    rows = []
    for row in parse_xlsx_rows(workbook):
        if (
            normalize(row.get("Exam")) == normalize(exam["category"])
            and str(row.get("Year", "")).split(".")[0] == str(exam["year"])
            and normalize(row.get("Section")) == normalize(exam["section"])
            and row.get("Q") is not None
        ):
            rows.append(row)
    return rows


def question_numbers_for_session(session: dict[str, Any]) -> list[int]:
    numbers = session.get("question_numbers")
    if numbers:
        return [int(number) for number in numbers]
    return list(range(1, int(session.get("question_count", 0)) + 1))


def session_question_pool(session: dict[str, Any]) -> list[dict[str, Any]]:
    pool = session.get("question_pool")
    if pool:
        return pool
    return [{"number": number, "category": session["category"], "variant": session["variant"]} for number in question_numbers_for_session(session)]


def practice_question_stats(category: str, variant: str) -> tuple[set[int], set[int]]:
    right: set[int] = set()
    wrong: set[int] = set()
    for path in RESPONSE_DIR.glob("*.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if session.get("status") != "evaluated" or session.get("category") != category or session.get("variant") != variant:
            continue
        for detail in session.get("evaluation", {}).get("details", []):
            try:
                question = int(detail["question"])
            except (KeyError, TypeError, ValueError):
                continue
            if detail.get("result") == "Correct":
                right.add(question)
            elif detail.get("result") == "Incorrect":
                wrong.add(question)
    return right, wrong


def practice_exam_options(exam: dict[str, Any]) -> dict[str, Any]:
    rows = answer_rows_for_exam(exam)
    metadata = {}
    for row in rows:
        try:
            number = int(row["Q"])
        except (KeyError, TypeError, ValueError):
            continue
        metadata[number] = {
            "section": str(row.get("Section_2") or "Uncategorized").strip(),
            "topic": str(row.get("Topic") or "Uncategorized").strip(),
            "difficulty": str(row.get("Difficulty") or "Unknown").strip(),
        }
    available = sorted(set(metadata) or set(range(1, exam["question_count"] + 1)))
    right, wrong = practice_question_stats(exam["category"], exam["variant"])
    return {
        "category": exam["category"],
        "variant": exam["variant"],
        "sections": sorted({item["section"] for item in metadata.values()}),
        "topics": sorted({item["topic"] for item in metadata.values()}),
        "metadata": metadata,
        "available": available,
        "right": sorted(right),
        "wrong": sorted(wrong),
    }


def family_metadata(category: str) -> dict[int, dict[str, str]]:
    metadata: dict[int, dict[str, str]] = {}
    for exam in scan_exams().get(category, []):
        for row in answer_rows_for_exam(exam):
            try:
                number = int(row["Q"])
            except (KeyError, TypeError, ValueError):
                continue
            metadata.setdefault(
                number,
                {
                    "section": str(row.get("Section_2") or "Uncategorized").strip(),
                    "topic": str(row.get("Topic") or "Uncategorized").strip(),
                    "difficulty": str(row.get("Difficulty") or "Unknown").strip(),
                },
            )
    return metadata


def practice_family_options(category: str) -> dict[str, Any]:
    exams = scan_exams().get(category, [])
    fallback_metadata = family_metadata(category)
    pool = []
    for exam in exams:
        options = practice_exam_options(exam)
        for number in options["available"]:
            metadata = options["metadata"].get(number) or fallback_metadata.get(number, {})
            pool.append({
                "number": number, "category": category, "variant": exam["variant"],
                "section": metadata.get("section", "Uncategorized"),
                "topic": metadata.get("topic", "Uncategorized"),
                "right": number in options["right"], "wrong": number in options["wrong"],
            })
    return {"pool": pool, "sections": sorted({item["section"] for item in pool}), "topics": sorted({item["topic"] for item in pool})}


def scoring_profile(category: str, question_count: int) -> dict[str, Any]:
    # Official AMC 10/12 scoring: 6 points correct, 1.5 blank, 0 incorrect.
    # Keeping this in one place makes it easy to add other exam-specific rules.
    if normalize(category) in {"amc10", "amc12"} and question_count == 25:
        return {
            "name": "AMC scoring",
            "correct": 6.0,
            "blank": 1.5,
            "wrong": 0.0,
            "maximum": 150.0,
        }
    return {
        "name": "Standard scoring",
        "correct": 1.0,
        "blank": 0.0,
        "wrong": 0.0,
        "maximum": float(question_count),
    }


def make_session_id(label: str) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{safe_label}_{stamp}_{uuid.uuid4().hex[:6]}"


def load_session(session_id: str) -> dict[str, Any]:
    path = session_json_path(session_id)
    if not path.exists():
        abort(404, "Saved session not found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        abort(500, "Saved session file is damaged")


def save_session(session: dict[str, Any]) -> None:
    session["updated_at"] = now_iso()
    json_path = session_json_path(session["session_id"])
    tmp_path = json_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(session, indent=2), encoding="utf-8")
    os.replace(tmp_path, json_path)
    write_session_csv(session)


def write_session_csv(session: dict[str, Any]) -> None:
    evaluated = session.get("status") == "evaluated"
    fields = [
        "Practice Name", "Exam Family", "Exam", "Section", "Topic", "Mode",
        "Question", "Response", "Review", "Note", "Time Spent (seconds)",
    ]
    if evaluated:
        fields += ["Correct Answer", "Result", "Points"]
    with session_csv_path(session["session_id"]).open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for q in question_numbers_for_session(session):
            response = session["responses"].get(str(q), {})
            row = {
                "Practice Name": session["exam_label"],
                "Exam Family": session.get("exam_family", session.get("category", "")),
                "Exam": session.get("exam", session.get("variant", "All exams")),
                "Section": session.get("content_section", ""),
                "Topic": session.get("topic", ""),
                "Mode": session.get("mode", ""),
                "Question": q,
                "Response": response.get("answer") or "",
                "Review": response.get("review_status") or ("Needs review" if response.get("review") else "Not flagged"),
                "Note": response.get("note", ""),
                "Time Spent (seconds)": round(float(response.get("time_spent_seconds", 0.0)), 1),
            }
            if evaluated:
                detail = next((d for d in session.get("evaluation", {}).get("details", []) if d["question"] == q), None)
                if detail:
                    row.update(
                        {
                            "Correct Answer": detail.get("correct_answer") or "",
                            "Result": detail.get("result", ""),
                            "Points": detail.get("points", 0),
                        }
                    )
            writer.writerow(row)


def list_saved_sessions() -> list[dict[str, Any]]:
    items = []
    for path in RESPONSE_DIR.glob("*.json"):
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append(
            {
                "session_id": s.get("session_id", path.stem),
                "exam_label": s.get("exam_label", "Unknown exam"),
                "exam_family": s.get("exam_family", s.get("category", "")),
                "exam": s.get("exam", s.get("variant", "")),
                "content_section": s.get("content_section", ""),
                "topic": s.get("topic", ""),
                "mode": s.get("mode", "untimed"),
                "duration_minutes": s.get("duration_minutes"),
                "current_index": s.get("current_index", 0),
                "question_count": s.get("question_count", 0),
                "answered": sum(1 for r in s.get("responses", {}).values() if r.get("answer")),
                "status": s.get("status", "in_progress"),
                "score": s.get("evaluation", {}).get("score"),
                "maximum": s.get("evaluation", {}).get("maximum"),
                "updated_at": s.get("updated_at", ""),
                "review_count": sum(1 for response in s.get("responses", {}).values() if (response.get("review_status") or ("needs_review" if response.get("review") else "not_flagged")) == "needs_review"),
            }
        )
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return items


def evaluate_session(session: dict[str, Any]) -> dict[str, Any]:
    scoring = scoring_profile(session["category"], session["question_count"])
    correct = wrong = blank = 0
    score = 0.0
    details = []
    keys: dict[tuple[str, str, int], str] = {}
    for item in session_question_pool(session):
        source_exam = find_exam(item["category"], item["variant"])
        if source_exam:
            source_key, _ = answer_key_for_exam(source_exam)
            if item["number"] in source_key:
                keys[(item["category"], item["variant"], item["number"])] = source_key[item["number"]]
    if not keys:
        raise ValueError(f"No answer key found for {session['exam_label']}.")

    for q in question_numbers_for_session(session):
        response = session["responses"].get(str(q), {})
        selected = (response.get("answer") or "").upper() or None
        item = session_question_pool(session)[q - 1]
        actual = keys.get((item["category"], item["variant"], item["number"]))
        if not selected:
            result = "Blank"
            points = scoring["blank"]
            blank += 1
        elif actual and selected == actual:
            result = "Correct"
            points = scoring["correct"]
            correct += 1
        else:
            result = "Incorrect"
            points = scoring["wrong"]
            wrong += 1
        score += points
        details.append(
            {
                "question": q,
                "response": selected,
                "correct_answer": actual,
                "result": result,
                "points": points,
                "time_spent_seconds": round(float(response.get("time_spent_seconds", 0.0)), 1),
            }
        )

    return {
        "score": round(score, 1),
        "maximum": scoring["maximum"],
        "correct": correct,
        "wrong": wrong,
        "blank": blank,
        "scoring_name": scoring["name"],
        "answer_file": "AnswerLog/*.xlsx",
        "details": details,
        "evaluated_at": now_iso(),
    }


@app.get("/")
def home():
    exams = scan_exams()
    serializable = {
        category: [
            {**{
                "category": e["category"],
                "variant": e["variant"],
                "label": e["label"],
                "year": e["year"],
                "section": e["section"],
                "question_count": e["question_count"],
            }, **practice_exam_options(e)}
            for e in variants
        ]
        for category, variants in exams.items()
    }
    return render_template(
        "index.html",
        exams=serializable,
        saved_sessions=list_saved_sessions(),
        practice_prefill={
            "category": request.args.get("category", "").strip(),
            "variant": request.args.get("variant", "").strip(),
            "section": request.args.get("content_section", "").strip(),
            "topic": request.args.get("topic", "").strip(),
            "selection_mode": request.args.get("selection_mode", "all").strip(),
            "question_count": request.args.get("question_count", "").strip(),
            "exam_name": request.args.get("exam_name", "").strip(),
        },
    )


@app.route("/setup", methods=["GET", "POST"])
def setup():
    families = sorted(set(scan_exams()) | {"AMC8", "AMC10", "AMC12"})
    result = None
    error = None
    if request.method == "POST":
        family = request.form.get("family", "").strip().upper()
        exam_name = request.form.get("exam_name", "").strip()
        pdf = request.files.get("pdf")
        try:
            if family not in families:
                raise ExtractionError("Choose a valid exam family.")
            if not exam_name:
                raise ExtractionError("Exam name is required.")
            parts = parse_exam_name(exam_name)
            if parts.exam_type != family:
                raise ExtractionError(f"Exam name belongs to {parts.exam_type}; choose that exam family.")
            if not pdf or not pdf.filename:
                raise ExtractionError("Choose a PDF exam file.")
            if not pdf.filename.lower().endswith(".pdf"):
                raise ExtractionError("Only PDF files are supported.")
            safe_name = secure_filename(pdf.filename) or "exam.pdf"
            with tempfile.NamedTemporaryFile(prefix="exam_", suffix=".pdf", delete=False) as temporary:
                temporary_path = Path(temporary.name)
            try:
                pdf.save(temporary_path)
                result = extract_exam_questions(
                    temporary_path,
                    exam_name,
                    output_root=QUESTION_BANK,
                    expected_questions=max(1, int(request.form.get("expected_questions", "25"))),
                    dpi=max(150, min(400, int(request.form.get("dpi", "250")))),
                    debug=request.form.get("debug") == "on",
                    overwrite=request.form.get("overwrite") == "on",
                )
                result["uploaded_file"] = safe_name
            finally:
                temporary_path.unlink(missing_ok=True)
        except (ExtractionError, OSError, ValueError) as exc:
            error = str(exc)
    return render_template("setup.html", families=families, result=result, error=error)


@app.get("/analyze")
def analyze():
    selected_family = request.args.get("family", "").strip()
    selected_section = request.args.get("section", "").strip()
    families = sorted({category for category in scan_exams()})
    family_pool = []
    if selected_family:
        family_pool = practice_family_options(selected_family)["pool"]
    else:
        for family in families:
            family_pool.extend(practice_family_options(family)["pool"])
    available_sections = sorted({item["section"] for item in family_pool})
    if selected_section not in available_sections:
        selected_section = ""
    evaluated_sessions = []
    section_totals: dict[str, dict[str, int]] = {}
    topic_totals: dict[str, dict[str, int]] = {}
    difficulty_totals: dict[str, dict[str, Any]] = {}
    timed_questions: list[dict[str, Any]] = []
    active_sessions = []
    rollup = {"attempts": 0, "correct": 0, "wrong": 0, "blank": 0, "questions": 0}

    for item in family_pool:
        collections = [(section_totals, item["section"])]
        if not selected_section or item["section"] == selected_section:
            collections.append((topic_totals, item["topic"]))
        for collection, key in collections:
            stats = collection.setdefault(key, {"available": 0, "evaluated": 0, "correct": 0})
            stats["available"] += 1

    for path in RESPONSE_DIR.glob("*.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if session.get("status") == "in_progress" and (not selected_family or session.get("category") == selected_family):
            active_sessions.append(
                {
                    "session_id": session.get("session_id", path.stem),
                    "exam_label": session.get("exam_label", "Unknown exam"),
                    "answered": sum(1 for response in session.get("responses", {}).values() if response.get("answer")),
                    "question_count": session.get("question_count", 0),
                    "updated_at": session.get("updated_at", ""),
                }
            )
        if session.get("status") != "evaluated":
            continue
        if selected_family and session.get("category") != selected_family:
            continue
        exam = find_exam(session.get("category", ""), session.get("variant", ""))
        if not exam:
            continue
        fallback_metadata = family_metadata(exam["category"])
        metadata = {}
        for row in answer_rows_for_exam(exam):
            try:
                metadata[int(row["Q"])] = {
                    "section": str(row.get("Section_2") or "Uncategorized").strip(),
                    "topic": str(row.get("Topic") or "Uncategorized").strip(),
                    "difficulty": str(row.get("Difficulty") or "Unknown").strip(),
                }
            except (TypeError, ValueError):
                continue

        details = {int(d["question"]): d for d in session.get("evaluation", {}).get("details", [])}
        evaluation = session.get("evaluation", {})
        rollup["attempts"] += 1
        rollup["correct"] += int(evaluation.get("correct", 0))
        rollup["wrong"] += int(evaluation.get("wrong", 0))
        rollup["blank"] += int(evaluation.get("blank", 0))
        rollup["questions"] += len(details)
        for question, detail in details.items():
            info = metadata.get(question)
            if not info:
                info = fallback_metadata.get(question)
            if not info:
                continue
            for collection, key in ((section_totals, info["section"]), (topic_totals, info["topic"])):
                if collection is topic_totals and selected_section and info["section"] != selected_section:
                    continue
                stats = collection.setdefault(key, {"available": 0, "evaluated": 0, "correct": 0})
                stats["evaluated"] += 1
                stats["correct"] += detail.get("result") == "Correct"

            response = session.get("responses", {}).get(str(question), {})
            time_spent = round(float(response.get("time_spent_seconds", detail.get("time_spent_seconds", 0.0))), 1)
            difficulty = str(info.get("difficulty") or "Unknown").strip() or "Unknown"
            difficulty_stats = difficulty_totals.setdefault(difficulty, {"total_seconds": 0.0, "questions": 0})
            difficulty_stats["total_seconds"] += time_spent
            difficulty_stats["questions"] += 1
            timed_questions.append(
                {
                    "exam_label": session.get("exam_label", "Unknown exam"),
                    "session_id": session.get("session_id", path.stem),
                    "question": question,
                    "section": info.get("section", "Uncategorized"),
                    "topic": info.get("topic", "Uncategorized"),
                    "difficulty": difficulty,
                    "seconds": time_spent,
                    "result": detail.get("result", ""),
                }
            )

        evaluated_sessions.append(
            {
                "exam_label": session.get("exam_label", "Unknown exam"),
                "score": session.get("evaluation", {}).get("score", 0),
                "maximum": session.get("evaluation", {}).get("maximum", 0),
                "evaluated_at": session.get("evaluation", {}).get("evaluated_at", ""),
            }
        )

    def progress_rows(values: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
        rows = []
        for name, stats in values.items():
            evaluated = stats["evaluated"]
            available = stats["available"]
            rows.append(
                {
                    "name": name,
                    "available": available,
                    "evaluated": evaluated,
                    "correct": stats["correct"],
                    "accuracy": round(stats["correct"] / evaluated * 100) if evaluated else 0,
                    "percent": round(stats["correct"] / available * 100) if available else 0,
                }
            )
        return sorted(rows, key=lambda row: (-row["percent"], row["name"]))

    sections = progress_rows(section_totals)
    topics = progress_rows(topic_totals)
    total_timed_questions = len(timed_questions)
    mean_question_seconds = round(
        sum(item["seconds"] for item in timed_questions) / total_timed_questions, 1
    ) if total_timed_questions else 0.0
    difficulty_rows = [
        {
            "name": name,
            "questions": stats["questions"],
            "mean_seconds": round((stats["total_seconds"] / stats["questions"]) * 2) / 2,
        }
        for name, stats in difficulty_totals.items()
    ]
    difficulty_rows.sort(key=lambda row: (-row["mean_seconds"], row["name"]))
    above_mean_questions = sorted(
        (
            item for item in timed_questions
            if item["seconds"] > mean_question_seconds
            and item["result"] in {"Incorrect", "Blank"}
        ),
        key=lambda item: (-item["seconds"], item["exam_label"], item["question"]),
    )
    def slow_groups(field: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in above_mean_questions:
            grouped.setdefault(item[field], []).append(item)
        return sorted(
            (
                {
                    "name": name,
                    "questions": sorted(items, key=lambda item: (-item["seconds"], item["exam_label"], item["question"])),
                    "mean_seconds": round(sum(item["seconds"] for item in items) / len(items), 1),
                }
                for name, items in grouped.items()
            ),
            key=lambda row: (-row["mean_seconds"], row["name"]),
        )
    slow_sections = slow_groups("section")
    slow_topics = slow_groups("topic")
    active_sessions.sort(key=lambda session: session["updated_at"], reverse=True)
    evaluated_sessions.sort(key=lambda session: session["evaluated_at"], reverse=True)
    rollup["accuracy"] = round(rollup["correct"] / rollup["questions"] * 100) if rollup["questions"] else 0
    rollup["available"] = len(family_pool)
    return render_template(
        "analyze.html",
        sections=sections,
        sections_available=sum(row["available"] for row in sections),
        topics=topics,
        topics_available=sum(row["available"] for row in topics),
        available_sections=available_sections,
        selected_section=selected_section,
        evaluated_sessions=evaluated_sessions,
        families=families,
        selected_family=selected_family,
        rollup=rollup,
        difficulty_rows=difficulty_rows,
        mean_question_seconds=mean_question_seconds,
        above_mean_questions=above_mean_questions,
        slow_sections=slow_sections,
        slow_topics=slow_topics,
        active_sessions=active_sessions,
    )


@app.post("/start")
def start_exam():
    category = request.form.get("category", "")
    variant = request.form.get("variant", "")
    mode = request.form.get("mode", "untimed")
    exam_name = request.form.get("exam_name", "").strip()
    selected_section = request.form.get("content_section", "").strip()
    selected_topic = request.form.get("topic", "").strip()
    selection_mode = request.form.get("selection_mode", "all")
    if not exam_name:
        return render_template("error.html", message="Practice name is required. Enter a name before starting."), 400
    try:
        duration_minutes = max(5, min(300, int(request.form.get("duration_minutes", "75"))))
    except ValueError:
        duration_minutes = 75
    if mode not in {"timed", "untimed"}:
        mode = "untimed"

    exam = find_exam(category, variant) if variant else None
    if not exam and variant:
        abort(400, "Exam not found")

    if exam:
        options = practice_exam_options(exam)
        pool = [
            {"number": number, "category": category, "variant": variant,
             "section": options["metadata"].get(number, {}).get("section", "Uncategorized"),
             "topic": options["metadata"].get(number, {}).get("topic", "Uncategorized"),
             "right": number in options["right"], "wrong": number in options["wrong"]}
            for number in options["available"]
        ]
    else:
        options = practice_family_options(category)
        pool = options["pool"]
    pool = [item for item in pool if (not selected_section or normalize(item["section"]) == normalize(selected_section)) and (not selected_topic or normalize(item["topic"]) == normalize(selected_topic))]
    if selection_mode == "incorrect":
        pool = [item for item in pool if item["wrong"]]
    elif selection_mode == "incorrect_unattempted":
        pool = [item for item in pool if not item["right"]]
    try:
        requested_count = max(1, int(request.form.get("question_count", str(len(pool)))))
    except ValueError:
        requested_count = len(pool)
    pool = pool[:requested_count]
    question_numbers = list(range(1, len(pool) + 1))
    if not pool:
        return render_template(
            "error.html",
            message="No questions match the selected section, topic, and practice mode. Choose a different topic or practice mode.",
        ), 200

    answer_key, answer_file = answer_key_for_exam(exam) if exam else ({}, None)
    session_id = make_session_id(exam_name)
    session = {
        "session_id": session_id,
        "category": category,
        "variant": variant,
        "exam_family": category,
        "exam": variant or "All exams in family",
        "exam_label": exam_name,
        "year": exam["year"] if exam else None,
        "section": exam["section"] if exam else None,
        "question_count": len(question_numbers),
        "question_numbers": question_numbers,
        "question_pool": pool,
        "selection_mode": selection_mode,
        "content_section": selected_section,
        "topic": selected_topic,
        "mode": mode,
        "duration_minutes": duration_minutes if mode == "timed" else None,
        "active_elapsed_seconds": 0.0,
        "current_index": 0,
        "status": "in_progress",
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "answer_key_available": bool(answer_key),
        "answer_file": answer_file,
        "responses": {
            str(i): {"answer": None, "time_spent_seconds": 0.0, "time_segments": []}
            for i in question_numbers
        },
    }
    with SESSION_LOCK:
        save_session(session)
    return redirect(url_for("exam_view", session_id=session_id))


@app.get("/exam/<session_id>")
def exam_view(session_id: str):
    session = load_session(session_id)
    reference_mode = request.args.get("reference") == "1"
    if session.get("status") == "evaluated" and not reference_mode:
        return redirect(url_for("result_view", session_id=session_id))
    exam = find_exam(session["category"], session["variant"]) if session.get("variant") else None
    if not exam and not session.get("question_pool"):
        abort(404, "Exam folder not found")

    requested_question = request.args.get("question", "")
    try:
        requested_index = question_numbers_for_session(session).index(int(requested_question))
    except (ValueError, TypeError):
        requested_index = session.get("current_index", 0)
    client_session = {
        "session_id": session["session_id"],
        "exam_label": session["exam_label"],
        "question_count": session["question_count"],
        "question_numbers": question_numbers_for_session(session),
        "mode": session["mode"],
        "duration_minutes": session.get("duration_minutes"),
        "active_elapsed_seconds": session.get("active_elapsed_seconds", 0.0),
        "current_index": requested_index,
        "responses": session.get("responses", {}),
        "reference_mode": reference_mode,
    }
    return render_template("exam.html", session=client_session, answer_choices=ANSWER_CHOICES)


@app.get("/question/<session_id>/<int:question_number>")
def question_image(session_id: str, question_number: int):
    session = load_session(session_id)
    pool = session_question_pool(session)
    if not 1 <= question_number <= len(pool):
        abort(404)
    item = pool[question_number - 1]
    exam = find_exam(item["category"], item["variant"])
    if not exam:
        abort(404)
    return send_file(exam["images"][int(item["number"]) - 1])


@app.post("/api/session/<session_id>/save")
def api_save(session_id: str):
    payload = request.get_json(silent=True) or {}
    with SESSION_LOCK:
        session = load_session(session_id)
        evaluated = session.get("status") == "evaluated"

        try:
            question = int(payload.get("question"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid question"}), 400
        if question not in question_numbers_for_session(session):
            return jsonify({"ok": False, "error": "Invalid question"}), 400

        answer_supplied = "answer" in payload
        answer = payload.get("answer")
        if answer_supplied:
            if answer in ("", None):
                answer = None
            elif str(answer).upper() in ANSWER_CHOICES:
                answer = str(answer).upper()
            else:
                return jsonify({"ok": False, "error": "Invalid answer"}), 400

        review = payload.get("review")
        review_status = payload.get("review_status")
        note = payload.get("note")
        question_started_at = payload.get("question_started_at")
        question_ended_at = payload.get("question_ended_at")
        if review is not None and not isinstance(review, bool):
            return jsonify({"ok": False, "error": "Invalid review state"}), 400
        if review_status is not None and review_status not in {"not_flagged", "needs_review", "completed"}:
            return jsonify({"ok": False, "error": "Invalid review status"}), 400
        if note is not None and not isinstance(note, str):
            return jsonify({"ok": False, "error": "Invalid note"}), 400
        if question_started_at is not None and not isinstance(question_started_at, str):
            return jsonify({"ok": False, "error": "Invalid question start time"}), 400
        if question_ended_at is not None and not isinstance(question_ended_at, str):
            return jsonify({"ok": False, "error": "Invalid question end time"}), 400

        try:
            delta = float(payload.get("delta_seconds", 0.0))
        except (TypeError, ValueError):
            delta = 0.0
        # Cap a single save delta to avoid accidental runaway values after a
        # sleeping computer wakes up. Autosave runs frequently in the UI.
        delta = max(0.0, min(delta, 120.0))

        response = session["responses"].setdefault(str(question), {"answer": None, "time_spent_seconds": 0.0})
        if not evaluated:
            if answer_supplied:
                response["answer"] = answer
            response["time_spent_seconds"] = round(float(response.get("time_spent_seconds", 0.0)) + delta, 2)
            session["active_elapsed_seconds"] = round(float(session.get("active_elapsed_seconds", 0.0)) + delta, 2)
            if question_started_at:
                response["question_started_at"] = question_started_at
            if question_ended_at:
                response["question_ended_at"] = question_ended_at
                response.setdefault("time_segments", []).append(
                    {"started_at": question_started_at or response.get("question_started_at"), "ended_at": question_ended_at, "seconds": round(delta, 2)}
                )
        if review is not None:
            response["review"] = review
            response["review_status"] = "needs_review" if review else "not_flagged"
        if review_status is not None:
            response["review_status"] = review_status
            response["review"] = review_status == "needs_review"
        if note is not None:
            response["note"] = note.strip()[:2000]

        try:
            idx = int(payload.get("current_index", question - 1))
            session["current_index"] = max(0, min(session["question_count"] - 1, idx))
        except (TypeError, ValueError):
            pass

        save_session(session)
        remaining = None
        if session["mode"] == "timed":
            remaining = max(0.0, session["duration_minutes"] * 60 - session["active_elapsed_seconds"])
        return jsonify(
            {
                "ok": True,
                "active_elapsed_seconds": session["active_elapsed_seconds"],
                "remaining_seconds": remaining,
                "question_time_seconds": response["time_spent_seconds"],
            }
        )


@app.get("/review")
def review_view():
    query = request.args.get("q", "").strip().lower()
    show_completed = request.args.get("completed", "") == "1"
    items = []
    for path in RESPONSE_DIR.glob("*.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for question, response in session.get("responses", {}).items():
            review_status = response.get("review_status") or ("needs_review" if response.get("review") else "not_flagged")
            if review_status == "not_flagged":
                continue
            if review_status == "completed" and not show_completed:
                continue
            number = str(question)
            pool_item = next((item for item in session_question_pool(session) if str(item.get("number")) == number), {})
            haystack = " ".join(
                str(value or "")
                for value in (
                    session.get("exam_label"), session.get("category"), session.get("variant"),
                    session.get("content_section"), session.get("topic"), number, response.get("note"),
                )
            ).lower()
            if query and query not in haystack:
                continue
            items.append(
                {
                    "session_id": session.get("session_id", path.stem),
                    "exam_label": session.get("exam_label", "Unknown exam"),
                    "category": session.get("category", ""),
                    "variant": session.get("variant", "All exams"),
                    "section": session.get("content_section") or pool_item.get("section", ""),
                    "topic": session.get("topic") or pool_item.get("topic", ""),
                    "question": number,
                    "note": response.get("note", ""),
                    "review": review_status == "needs_review",
                    "review_status": review_status,
                    "status": session.get("status", "in_progress"),
                }
            )
    items.sort(key=lambda item: (item["review"] is False, item["exam_label"], int(item["question"])))
    return render_template("review.html", items=items, query=request.args.get("q", ""), show_completed=show_completed)


@app.post("/finish/<session_id>")
def finish_exam(session_id: str):
    with SESSION_LOCK:
        session = load_session(session_id)
        if session.get("status") != "evaluated":
            try:
                evaluation = evaluate_session(session)
            except ValueError as exc:
                return render_template("error.html", message=str(exc)), 400
            session["evaluation"] = evaluation
            session["status"] = "evaluated"
            save_session(session)
            clear_workbook_cache()
    return redirect(url_for("result_view", session_id=session_id))


@app.get("/result/<session_id>")
def result_view(session_id: str):
    session = load_session(session_id)
    if session.get("status") != "evaluated":
        return redirect(url_for("exam_view", session_id=session_id))
    return render_template("result.html", session=session, evaluation=session["evaluation"])


@app.post("/retake/<session_id>")
def retake(session_id: str):
    old = load_session(session_id)
    exam = find_exam(old["category"], old["variant"]) if old.get("variant") else None
    if not exam and not old.get("question_pool"):
        abort(404, "Exam folder not found")
    new_id = make_session_id(old["exam_label"])
    new_session = {
        "session_id": new_id,
        "category": old["category"],
        "variant": old["variant"],
        "exam_label": old["exam_label"],
        "year": old.get("year"),
        "section": old.get("section"),
        "question_count": old["question_count"],
        "question_numbers": question_numbers_for_session(old),
        "question_pool": old.get("question_pool"),
        "mode": old["mode"],
        "duration_minutes": old.get("duration_minutes"),
        "active_elapsed_seconds": 0.0,
        "current_index": 0,
        "status": "in_progress",
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "answer_key_available": old.get("answer_key_available", False),
        "answer_file": old.get("answer_file"),
        "responses": {
            str(i): {"answer": None, "time_spent_seconds": 0.0, "time_segments": []}
            for i in question_numbers_for_session(old)
        },
    }
    with SESSION_LOCK:
        save_session(new_session)
    return redirect(url_for("exam_view", session_id=new_id))


@app.post("/delete/<session_id>")
def delete_session(session_id: str):
    safe_session_id(session_id)
    with SESSION_LOCK:
        for path in (session_json_path(session_id), session_csv_path(session_id)):
            if path.exists():
                path.unlink()
    return redirect(url_for("home"))


@app.get("/health")
def health():
    return jsonify({"ok": True, "exams": sum(len(v) for v in scan_exams().values())})


if __name__ == "__main__":
    # For direct execution. launcher.py is nicer because it opens the browser.
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
