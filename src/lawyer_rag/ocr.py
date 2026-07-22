from __future__ import annotations

import re
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pdfplumber
from pypdf import PdfReader

from lawyer_rag.chunking import AtomicBlock, paragraph_label
from lawyer_rag.config import Settings


WHITESPACE_RE = re.compile(r"\s+")


class PDFValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PageArtifact:
    page_number: int
    text: str
    width: float
    height: float
    quality_score: float
    quality_warning: str | None
    words: list[dict]


def validate_pdf(path: Path, settings: Settings) -> int:
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise PDFValidationError("Uploaded file is not a PDF")
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise PDFValidationError("Encrypted PDFs are not supported")
        page_count = len(reader.pages)
    except PDFValidationError:
        raise
    except Exception as exc:
        raise PDFValidationError("PDF could not be parsed") from exc
    if page_count < 1:
        raise PDFValidationError("PDF contains no pages")
    if page_count > settings.max_pdf_pages:
        raise PDFValidationError(
            f"PDF contains {page_count} pages; the configured maximum is {settings.max_pdf_pages}"
        )
    return page_count


def run_ocr(
    source: Path,
    destination: Path,
    sidecar: Path,
    settings: Settings,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ocrmypdf",
        "--language",
        "eng",
        "--rotate-pages",
        "--deskew",
        "--skip-text",
        "--output-type",
        "pdf",
        "--sidecar",
        str(sidecar),
        "--jobs",
        "2",
        str(source),
        str(destination),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=settings.ocr_timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "OCR failed").strip()
        raise RuntimeError(message[-2000:])


def _normalize(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def _quality(text: str) -> tuple[float, str | None]:
    if not text.strip():
        return 0.0, "No printed text was recognized on this page"
    printable = sum(char.isprintable() for char in text)
    alphanumeric = sum(char.isalnum() for char in text)
    total = max(len(text), 1)
    score = min(1.0, 0.55 * printable / total + 0.45 * min(alphanumeric / 1200, 1.0))
    if score < 0.45:
        return round(score, 3), "OCR quality appears low; inspect the source page"
    return round(score, 3), None


def _line_groups(words: list[dict]) -> list[list[dict]]:
    ordered = sorted(words, key=lambda word: (float(word["top"]), float(word["x0"])))
    lines: list[list[dict]] = []
    for word in ordered:
        if not lines:
            lines.append([word])
            continue
        current_top = statistics.median(float(item["top"]) for item in lines[-1])
        tolerance = max(3.0, float(word.get("height", 8.0)) * 0.45)
        if abs(float(word["top"]) - current_top) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda word: float(word["x0"]))
    return lines


def _blocks_from_words(
    words: list[dict], page_number: int, width: float, height: float
) -> list[AtomicBlock]:
    lines = _line_groups(words)
    if not lines:
        return []

    line_records: list[tuple[str, tuple[float, float, float, float], float]] = []
    for line in lines:
        text = _normalize(" ".join(str(word["text"]) for word in line))
        if not text:
            continue
        bbox = (
            min(float(word["x0"]) for word in line),
            min(float(word["top"]) for word in line),
            max(float(word["x1"]) for word in line),
            max(float(word["bottom"]) for word in line),
        )
        line_records.append((text, bbox, bbox[3] - bbox[1]))

    paragraphs: list[list[tuple[str, tuple[float, float, float, float], float]]] = []
    for record in line_records:
        if not paragraphs:
            paragraphs.append([record])
            continue
        previous = paragraphs[-1][-1]
        vertical_gap = record[1][1] - previous[1][3]
        starts_numbered = paragraph_label(record[0]) is not None
        if starts_numbered or vertical_gap > max(previous[2], record[2]) * 1.2:
            paragraphs.append([record])
        else:
            paragraphs[-1].append(record)

    blocks: list[AtomicBlock] = []
    for paragraph in paragraphs:
        text = _normalize(" ".join(line[0] for line in paragraph))
        bbox = (
            min(line[1][0] for line in paragraph),
            min(line[1][1] for line in paragraph),
            max(line[1][2] for line in paragraph),
            max(line[1][3] for line in paragraph),
        )
        blocks.append(
            AtomicBlock(
                page=page_number,
                text=text,
                bbox=bbox,
                page_width=width,
                page_height=height,
                paragraph_label=paragraph_label(text),
            )
        )
    return blocks


def extract_pdf(
    path: Path,
    progress: Callable[[int], None] | None = None,
) -> tuple[list[PageArtifact], list[AtomicBlock]]:
    pages: list[PageArtifact] = []
    blocks: list[AtomicBlock] = []
    with pdfplumber.open(str(path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            raw_words = page.extract_words(
                x_tolerance=2,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=True,
            )
            words = [
                {
                    "text": _normalize(str(word["text"])),
                    "x0": round(float(word["x0"]), 3),
                    "x1": round(float(word["x1"]), 3),
                    "top": round(float(word["top"]), 3),
                    "bottom": round(float(word["bottom"]), 3),
                }
                for word in raw_words
                if _normalize(str(word.get("text", "")))
            ]
            text = _normalize(" ".join(word["text"] for word in words))
            quality_score, warning = _quality(text)
            width, height = float(page.width), float(page.height)
            pages.append(
                PageArtifact(
                    page_number=page_number,
                    text=text,
                    width=width,
                    height=height,
                    quality_score=quality_score,
                    quality_warning=warning,
                    words=words,
                )
            )
            blocks.extend(_blocks_from_words(words, page_number, width, height))
            if progress:
                progress(page_number)
    return pages, blocks
