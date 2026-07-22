from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
NUMBERED_PARAGRAPH_RE = re.compile(r"^\s*(?:\(?\d+[A-Za-z]?\)?[.)-]?|[A-Z][.)])\s+")


def token_count(text: str) -> int:
    return len(TOKEN_RE.findall(text))


@dataclass(frozen=True)
class AtomicBlock:
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    page_width: float
    page_height: float
    paragraph_label: str | None = None


@dataclass(frozen=True)
class ChunkDraft:
    sequence: int
    text: str
    parent_text: str
    page_start: int
    page_end: int
    paragraph_label: str | None
    coordinates: dict[str, Any]
    token_count: int


def _is_heading(block: AtomicBlock) -> bool:
    text = block.text.strip()
    if not text or len(text) > 140:
        return False
    letters = [char for char in text if char.isalpha()]
    uppercase = bool(letters) and sum(char.isupper() for char in letters) / len(letters) > 0.8
    legal_heading = text.lower().rstrip(":") in {
        "facts",
        "grounds",
        "prayer",
        "arguments",
        "findings",
        "order",
        "judgment",
        "relief",
    }
    return uppercase or legal_heading


def _sections(blocks: list[AtomicBlock], parent_max: int) -> list[list[AtomicBlock]]:
    sections: list[list[AtomicBlock]] = []
    current: list[AtomicBlock] = []
    current_tokens = 0
    for block in blocks:
        block_tokens = token_count(block.text)
        if current and (_is_heading(block) or current_tokens + block_tokens > parent_max):
            sections.append(current)
            current = []
            current_tokens = 0
        current.append(block)
        current_tokens += block_tokens
    if current:
        sections.append(current)
    return sections


def _coordinates(blocks: list[AtomicBlock]) -> dict[str, Any]:
    pages: dict[str, dict[str, Any]] = {}
    for block in blocks:
        entry = pages.setdefault(
            str(block.page),
            {"width": block.page_width, "height": block.page_height, "boxes": []},
        )
        entry["boxes"].append([round(value, 3) for value in block.bbox])
    return {"pages": pages}


def build_chunks(
    blocks: list[AtomicBlock],
    *,
    target_tokens: int = 350,
    max_tokens: int = 500,
    parent_max_tokens: int = 1500,
    overlap_max_tokens: int = 80,
) -> list[ChunkDraft]:
    if not blocks:
        return []

    drafts: list[ChunkDraft] = []
    sequence = 0
    for section in _sections(blocks, parent_max_tokens):
        parent_text = "\n\n".join(block.text.strip() for block in section if block.text.strip())
        child: list[AtomicBlock] = []
        child_tokens = 0

        def emit() -> None:
            nonlocal child, child_tokens, sequence
            if not child:
                return
            text = "\n\n".join(block.text.strip() for block in child if block.text.strip())
            if not text:
                child = []
                child_tokens = 0
                return
            sequence += 1
            drafts.append(
                ChunkDraft(
                    sequence=sequence,
                    text=text,
                    parent_text=parent_text,
                    page_start=min(block.page for block in child),
                    page_end=max(block.page for block in child),
                    paragraph_label=next(
                        (block.paragraph_label for block in child if block.paragraph_label), None
                    ),
                    coordinates=_coordinates(child),
                    token_count=token_count(text),
                )
            )
            overlap = child[-1:] if token_count(child[-1].text) <= overlap_max_tokens else []
            child = overlap
            child_tokens = sum(token_count(block.text) for block in child)

        for block in section:
            size = token_count(block.text)
            if child and child_tokens + size > max_tokens:
                emit()
            child.append(block)
            child_tokens += size
            if child_tokens >= target_tokens:
                emit()
        emit()

    return drafts


def paragraph_label(text: str) -> str | None:
    match = NUMBERED_PARAGRAPH_RE.match(text)
    return match.group(0).strip() if match else None
