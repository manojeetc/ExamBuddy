"""Extract individual exam questions from text-based PDFs.

The normal path uses PyMuPDF word coordinates. It detects a complete sequential
question-number sequence, renders each question's page slice, stitches slices
that cross page boundaries, and writes a manifest. OCR is intentionally not
used implicitly: scanned PDFs raise a clear error so they cannot be imported
with incorrect boundaries.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError as exc:  # pragma: no cover - exercised by installation, not tests
    raise RuntimeError("PyMuPDF is required. Install dependencies with: pip install -r requirements.txt") from exc

from PIL import Image, ImageChops, ImageDraw


class ExtractionError(RuntimeError):
    """Raised when a PDF cannot be safely converted into question images."""


@dataclass(frozen=True)
class ExamParts:
    exam_type: str
    year: str
    section: str | None


@dataclass(frozen=True)
class QuestionMarker:
    question: int
    page: int
    y: float
    x: float


def parse_exam_name(exam_name: str) -> ExamParts:
    match = re.fullmatch(r"(AMC(?:8|10|12))[_\-](20\d{2})(?:[_\-]([A-Za-z]))?", exam_name.strip(), re.I)
    if not match:
        raise ExtractionError("Exam name must look like AMC10_2025_A, AMC8_2023, or AMC12_2022_A.")
    return ExamParts(match.group(1).upper(), match.group(2), match.group(3).upper() if match.group(3) else None)


def extract_page_text_blocks(page: Any) -> list[tuple[float, float, float, float, str]]:
    """Return words as x0, y0, x1, y1, text tuples."""
    return [(float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])) for w in page.get_text("words")]


def _group_lines(words: list[tuple[float, float, float, float, str]], tolerance: float = 3.0) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (item[1], item[0])):
        target = next((line for line in reversed(lines) if abs(word[1] - line["y"]) <= tolerance), None)
        if target is None:
            target = {"y": word[1], "words": []}
            lines.append(target)
        target["words"].append(word)
    for line in lines:
        line["words"].sort(key=lambda item: item[0])
        line["text"] = " ".join(item[4] for item in line["words"])
        line["x"] = line["words"][0][0]
    return sorted(lines, key=lambda line: (line["y"], line["x"]))


def detect_question_markers(page: Any, expected_questions: int) -> list[QuestionMarker]:
    words = extract_page_text_blocks(page)
    markers: list[QuestionMarker] = []
    for line in _group_lines(words):
        match = re.match(r"^(\d{1,2})\.(?:\s|$)", line["text"])
        if not match:
            continue
        number = int(match.group(1))
        if 1 <= number <= expected_questions and line["x"] <= page.rect.width * 0.28:
            markers.append(QuestionMarker(number, page.number, line["y"], line["x"]))
    return markers


def _select_sequence(candidates: list[QuestionMarker], expected_questions: int) -> list[QuestionMarker]:
    by_number: dict[int, list[QuestionMarker]] = {}
    for marker in candidates:
        by_number.setdefault(marker.question, []).append(marker)
    for start in by_number.get(1, []):
        selected = [start]
        previous = start
        for number in range(2, expected_questions + 1):
            options = [candidate for candidate in by_number.get(number, []) if (candidate.page, candidate.y) > (previous.page, previous.y)]
            if not options:
                break
            previous = options[0]
            selected.append(previous)
        if len(selected) == expected_questions:
            return selected
    detected = sorted({marker.question for marker in candidates})
    raise ExtractionError(f"Could not detect a complete sequence 1..{expected_questions}; detected {detected}.")


def validate_question_sequence(markers: list[QuestionMarker], expected_questions: int) -> None:
    numbers = [marker.question for marker in markers]
    expected = list(range(1, expected_questions + 1))
    if numbers != expected:
        raise ExtractionError(f"Question sequence is invalid. Expected {expected}, detected {numbers}.")


def analyze_pdf(pdf_path: str | Path, expected_questions: int = 25) -> tuple[Any, list[QuestionMarker]]:
    document = fitz.open(str(pdf_path))
    all_markers: list[QuestionMarker] = []
    text_word_count = 0
    for page in document:
        words = extract_page_text_blocks(page)
        text_word_count += len(words)
        all_markers.extend(detect_question_markers(page, expected_questions))
    if text_word_count < expected_questions:
        document.close()
        raise ExtractionError("PDF has little or no usable text. OCR fallback is not enabled for this upload.")
    try:
        markers = _select_sequence(sorted(all_markers, key=lambda marker: (marker.page, marker.y, marker.x)), expected_questions)
        validate_question_sequence(markers, expected_questions)
    except Exception:
        document.close()
        raise
    return document, markers


def _render_slice(page: Any, top: float, bottom: float, dpi: int, margin: int = 18) -> Image.Image:
    rect = page.rect
    crop = fitz.Rect(max(0, rect.x0), max(0, top - margin), rect.x1, min(rect.height, bottom + margin))
    zoom = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=crop, alpha=False)
    return Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)


def stitch_vertical(images: list[Image.Image], gap: int = 20) -> Image.Image:
    width = max(image.width for image in images)
    height = sum(image.height for image in images) + gap * (len(images) - 1)
    output = Image.new("RGB", (width, height), "white")
    y = 0
    for image in images:
        output.paste(image, ((width - image.width) // 2, y))
        y += image.height + gap
    return output


def trim_whitespace(image: Image.Image, padding: int = 14) -> Image.Image:
    background = Image.new("RGB", image.size, "white")
    diff = ImageChops.difference(image, background).convert("L")
    bbox = diff.point(lambda value: 255 if value > 8 else 0).getbbox()
    if not bbox:
        return image
    left = max(0, bbox[0] - padding)
    top = max(0, bbox[1] - padding)
    right = min(image.width, bbox[2] + padding)
    bottom = min(image.height, bbox[3] + padding)
    return image.crop((left, top, right, bottom))


def build_question_segments(markers: list[QuestionMarker], document: Any, content_top_ratio: float = 0.05, content_bottom_ratio: float = 0.94) -> list[list[tuple[int, float, float]]]:
    segments: list[list[tuple[int, float, float]]] = []
    for index, marker in enumerate(markers):
        next_marker = markers[index + 1] if index + 1 < len(markers) else None
        pieces: list[tuple[int, float, float]] = []
        if next_marker and next_marker.page == marker.page:
            pieces.append((marker.page, marker.y, next_marker.y))
        else:
            page = document[marker.page]
            pieces.append((marker.page, marker.y, page.rect.height * content_bottom_ratio))
            end_page = next_marker.page if next_marker else len(document) - 1
            for page_number in range(marker.page + 1, end_page):
                page = document[page_number]
                pieces.append((page_number, page.rect.height * content_top_ratio, page.rect.height * content_bottom_ratio))
            if next_marker:
                next_page = document[next_marker.page]
                pieces.append((next_marker.page, next_page.rect.height * content_top_ratio, next_marker.y))
        segments.append(pieces)
    return segments


def _debug_pages(document: Any, markers: list[QuestionMarker], output: Path, dpi: int) -> None:
    by_page: dict[int, list[QuestionMarker]] = {}
    for marker in markers:
        by_page.setdefault(marker.page, []).append(marker)
    output.mkdir(parents=True, exist_ok=True)
    for page_number, page in enumerate(document):
        zoom = dpi / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
        draw = ImageDraw.Draw(image)
        for marker in by_page.get(page_number, []):
            y = int(marker.y * zoom)
            draw.line((0, y, image.width, y), fill="red", width=3)
            draw.text((8, max(2, y - 22)), f"Q{marker.question}", fill="red")
        image.save(output / f"page_{page_number + 1:02d}_detected.png")


def extract_exam_questions(pdf_path: str | Path, exam_name: str, output_root: str | Path = "QuestionBank", expected_questions: int = 25, dpi: int = 250, debug: bool = False, overwrite: bool = False) -> dict[str, Any]:
    parts = parse_exam_name(exam_name)
    pdf_path = Path(pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise ExtractionError("A readable .pdf file is required.")
    variant = f"{parts.year}_{parts.section}" if parts.section else parts.year
    final_directory = Path(output_root) / parts.exam_type / variant
    if final_directory.exists() and any(final_directory.iterdir()) and not overwrite:
        raise ExtractionError(f"Refusing to overwrite existing exam folder: {final_directory}. Use overwrite explicitly.")
    staging_parent = final_directory.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{variant}_", dir=staging_parent))
    document = None
    try:
        document, markers = analyze_pdf(pdf_path, expected_questions)
        segments = build_question_segments(markers, document)
        files = []
        manifest_questions = []
        for number, pieces in enumerate(segments, start=1):
            images = []
            for page_number, top, bottom in pieces:
                page = document[page_number]
                images.append(_render_slice(page, top, bottom, dpi))
            image = trim_whitespace(stitch_vertical(images))
            filename = f"{exam_name}_Q{number:02d}.png"
            image.save(staging / filename, "PNG", optimize=True)
            files.append(str(final_directory / filename))
            manifest_questions.append({"question": number, "file": filename, "start_page": pieces[0][0] + 1, "end_page": pieces[-1][0] + 1})
        if debug:
            _debug_pages(document, markers, staging / "debug", dpi)
        manifest = {"exam": exam_name, "source_pdf": pdf_path.name, "question_count": len(files), "questions": manifest_questions}
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if final_directory.exists() and overwrite:
            shutil.rmtree(final_directory)
        staging.rename(final_directory)
        return {"success": True, "exam": exam_name, "output_directory": str(final_directory), "questions_created": len(files), "files": files, "manifest": manifest}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if document is not None:
            document.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract individual exam questions from a text-based PDF.")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--exam", required=True)
    parser.add_argument("--questions", type=int, default=25)
    parser.add_argument("--dpi", type=int, default=250)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        result = extract_exam_questions(args.pdf, args.exam, expected_questions=args.questions, dpi=args.dpi, debug=args.debug, overwrite=args.overwrite)
    except (ExtractionError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
